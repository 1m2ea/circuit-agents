"""Phase 2 · 第三层范式进化 ① —— 强化学习优化拓扑（RLOptimizer）

问题：现有编译器产出的拓扑是「规则拍出来的」——串/并联度、模型档位、要不要加校验
节点，全靠 Router/ModelSelector 的启发式。启发式不知道「这个具体任务上，把 B 换成
tool 档、再砍掉那个多余的 verify」能省 40% 成本而质量不掉。

思路（离线搜索 + 真实 reward）：
  · 动作空间 = 5 类拓扑变异算子（换档 / 加校验 / 删冗余 / 并联化 / 加汇合）。
  · reward = 加权(质量, -成本, -延迟)，**由真实 CircuitExecutor 执行结果算出**，
    不是模拟打分——这是它区别于 optimizer.py（纯解析式估算）的关键。
  · 策略 = UCB1 多臂老虎机选算子 + 爬山接受（只保留 reward 更优的拓扑）。
    离线样本少，bandit 比 Q-learning 收敛快得多，且每步都可解释。
  · 收敛后把最优拓扑连同 reward 沉淀进 TopologyMemory，下次同类任务直接 recall。

与既有模块的分工：
  · optimizer.py（Optimizer）：编译**前**在 Goal 层面选档位，靠解析式估算，不执行。
  · rl_optimizer.py（本模块）：编译**后**在 Spec 层面改结构，靠真实执行反馈，会执行。
  · self_evolution（⑭）：跨任务蒸馏 motif 模板；本模块产出的最优拓扑正是它的上游素材。

离线安全：全程 SimBackend，无 key、无网络。
"""

from __future__ import annotations

import json
import math
import random
from typing import Optional


# ──────────────────────────────────────────────────────────
# 拓扑变异算子（动作空间）
# ──────────────────────────────────────────────────────────

_TIERS = ("small", "large", "tool")


def _dc(spec):
    return json.loads(json.dumps(spec))


def _preds(spec, cid):
    return [a for a, b in spec.get("wires", []) if b == cid]


def _succs(spec, cid):
    return [b for a, b in spec.get("wires", []) if a == cid]


def _resistors(spec):
    return [c for c, v in spec.get("components", {}).items()
            if v.get("type") == "resistor"]


def _has_cycle(spec):
    """Kahn 判环：入度归零法跑不完 → 有环。"""
    comps = list(spec.get("components", {}))
    indeg = {c: 0 for c in comps}
    for a, b in spec.get("wires", []):
        if b in indeg:
            indeg[b] += 1
    queue = [c for c in comps if indeg[c] == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for s in _succs(spec, cur):
            if s in indeg:
                indeg[s] -= 1
                if indeg[s] == 0:
                    queue.append(s)
    return seen != len(comps)


def _is_valid(spec):
    """变异后拓扑合法性：非空 / 无环 / 无自环 / 无孤立节点（单节点图除外）。"""
    comps = spec.get("components", {})
    if not comps:
        return False
    wires = spec.get("wires", [])
    if any(a == b for a, b in wires):
        return False
    if _has_cycle(spec):
        return False
    if len(comps) > 1:
        touched = {c for w in wires for c in w}
        if any(c not in touched for c in comps):
            return False     # 孤立节点 → 数据流断裂
    return True


def act_swap_model(spec, rng):
    """算子1 换档：随机挑一个电阻换 model 档位（小/大/工具）。"""
    rs = _resistors(spec)
    if not rs:
        return None
    new = _dc(spec)
    cid = rng.choice(rs)
    cur = new["components"][cid].get("model", "small")
    cand = [t for t in _TIERS if t != cur]
    new["components"][cid]["model"] = rng.choice(cand)
    return {"op": "swap_model", "node": cid, "spec": new,
            "detail": f"{cur}→{new['components'][cid]['model']}"}


def act_add_verify(spec, rng):
    """算子2 加校验：给某电阻挂一个下游 verify 节点（提质量、增成本）。"""
    rs = [c for c in _resistors(spec)
          if not any(spec["components"].get(s, {}).get("type") == "verify"
                     for s in _succs(spec, c))]
    if not rs:
        return None
    new = _dc(spec)
    cid = rng.choice(rs)
    vid = f"{cid}__verify"
    if vid in new["components"]:
        return None
    old_succs = _succs(new, cid)
    new["components"][vid] = {"type": "verify", "label": f"verify_{cid}",
                              "threshold": 0.6}
    new["wires"] = [w for w in new["wires"] if w[0] != cid]
    new["wires"].append([cid, vid])
    for s in old_succs:
        new["wires"].append([vid, s])
    return {"op": "add_verify", "node": cid, "spec": new, "detail": f"+{vid}"}


def act_drop_verify(spec, rng):
    """算子3 删冗余：摘掉一个 verify/adc 节点（省成本延迟，可能掉质量）。"""
    cands = [c for c, v in spec.get("components", {}).items()
             if v.get("type") in ("verify", "adc")]
    if not cands:
        return None
    new = _dc(spec)
    cid = rng.choice(cands)
    ps, ss = _preds(new, cid), _succs(new, cid)
    for p in ps:                            # 前驱直连后继，保数据流
        for s in ss:
            if [p, s] not in new["wires"]:
                new["wires"].append([p, s])
    del new["components"][cid]
    new["wires"] = [w for w in new["wires"] if cid not in w]
    return {"op": "drop_verify", "node": cid, "spec": new, "detail": f"-{cid}"}


def act_parallelize(spec, rng):
    """算子4 并联化：把 A→B 的串联拆成 A、B 共享上游并行（降延迟）。

    仅当 B 不真实依赖 A 的产出（required_inputs 与 A 的 produced_outputs 无交集）
    时才安全——否则会造出「线性关系不满足」的坏拓扑，reward 自然会惩罚它。
    """
    new = _dc(spec)
    cands = []
    for a, b in new.get("wires", []):
        ca, cb = new["components"].get(a, {}), new["components"].get(b, {})
        if ca.get("type") != "resistor" or cb.get("type") != "resistor":
            continue
        need = set(cb.get("required_inputs") or [])
        give = set(ca.get("produced_outputs") or [])
        if need & give:
            continue                        # 真实依赖，不能拆
        if len(_preds(new, a)) == 0:
            continue                        # A 没上游，B 无处可挂
        cands.append((a, b))
    if not cands:
        return None
    a, b = rng.choice(cands)
    new["wires"] = [w for w in new["wires"] if w != [a, b]]
    for p in _preds(new, a):
        if [p, b] not in new["wires"]:
            new["wires"].append([p, b])
    return {"op": "parallelize", "node": b, "spec": new, "detail": f"{a}∥{b}"}


def act_add_capacitor(spec, rng):
    """算子5 加汇合：给多入度节点前插电容做汇合（提完整性）。"""
    cands = [c for c in spec.get("components", {})
             if len(_preds(spec, c)) >= 2
             and spec["components"][c].get("type") == "resistor"]
    if not cands:
        return None
    new = _dc(spec)
    cid = rng.choice(cands)
    mid = f"{cid}__merge"
    if mid in new["components"]:
        return None
    ps = _preds(new, cid)
    new["components"][mid] = {"type": "capacitor", "label": f"merge_{cid}",
                              "mode": "all"}
    new["wires"] = [w for w in new["wires"] if w[1] != cid]
    for p in ps:
        new["wires"].append([p, mid])
    new["wires"].append([mid, cid])
    return {"op": "add_capacitor", "node": cid, "spec": new, "detail": f"+{mid}"}


ACTIONS = {
    "swap_model": act_swap_model,
    "add_verify": act_add_verify,
    "drop_verify": act_drop_verify,
    "parallelize": act_parallelize,
    "add_capacitor": act_add_capacitor,
}


# ──────────────────────────────────────────────────────────
# RL 优化器
# ──────────────────────────────────────────────────────────

class RLOptimizer:
    """UCB1 多臂老虎机 + 爬山：用真实执行 reward 搜索更优拓扑。

    用法::

        opt = RLOptimizer(seed=0)
        res = opt.optimize(spec, episodes=24)
        res["best_spec"]      # 最优拓扑
        res["improvement"]    # reward 相对基线的提升
        res["arm_stats"]      # 每个算子的平均收益（可解释：哪类改动真有用）
    """

    DEFAULT_WEIGHTS = {"quality": 1.0, "cost": 0.35, "latency": 0.25}

    def __init__(self, weights: Optional[dict] = None, seed: int = 0,
                 memory=None, exec_seed: int = 0, c_ucb: float = 1.4):
        self.w = dict(self.DEFAULT_WEIGHTS)
        if weights:
            self.w.update(weights)
        self.rng = random.Random(seed)
        self.memory = memory
        self.exec_seed = exec_seed      # 执行 seed 固定 → 同拓扑 reward 可复现
        self.c_ucb = c_ucb
        self.arms = {k: {"n": 0, "total": 0.0, "best": 0.0} for k in ACTIONS}
        self._base = None               # 基线 (cost, latency) 用于归一化

    # ---- reward ----
    def reward(self, result: dict) -> float:
        """reward = w_q·质量 − w_c·相对成本 − w_l·相对延迟；失败重罚。

        成本/延迟按**基线**归一，避免不同任务量纲不可比。
        """
        if not result:
            return -1.0
        q = float(result.get("final_quality") or 0.0)
        cost = float(result.get("total_cost") or 0.0)
        lat = float(result.get("total_latency_ms") or 0.0)
        bc, bl = (self._base or (cost or 1e-6, lat or 1e-6))
        r = (self.w["quality"] * q
             - self.w["cost"] * (cost / max(bc, 1e-6))
             - self.w["latency"] * (lat / max(bl, 1e-6)))
        if not result.get("success"):
            r -= 0.5                    # 跑不通的拓扑再便宜也没意义
        return r

    # ---- 真实执行评估 ----
    def evaluate(self, spec: dict) -> dict:
        """真跑一遍 CircuitExecutor（SimBackend，固定 seed → 可复现）。"""
        from runtime import Circuit, CircuitExecutor, SimBackend
        try:
            circ = Circuit(spec, SimBackend(random.Random(self.exec_seed)))
            res = CircuitExecutor(circ, memory_enabled=False,
                                  auto_select_models=False).run()
        except Exception as e:
            return {"success": False, "final_quality": 0.0, "total_cost": 0.0,
                    "total_latency_ms": 0.0, "error": str(e)}
        return res

    # ---- UCB1 选臂 ----
    def _select_arm(self, t: int) -> str:
        untried = [k for k, v in self.arms.items() if v["n"] == 0]
        if untried:
            return self.rng.choice(untried)     # 先把每个算子都试一次
        best, best_u = None, -1e9
        for k, v in self.arms.items():
            mean = v["total"] / v["n"]
            u = mean + self.c_ucb * math.sqrt(math.log(max(t, 2)) / v["n"])
            if u > best_u:
                best, best_u = k, u
        return best

    # ---- 主搜索 ----
    def optimize(self, spec_or_goal, episodes: int = 24,
                 patience: int = 12) -> dict:
        """搜索更优拓扑。返回 best_spec / baseline / improvement / history / arm_stats。"""
        spec = self._to_spec(spec_or_goal)
        base_res = self.evaluate(spec)
        self._base = (max(float(base_res.get("total_cost") or 0.0), 1e-6),
                      max(float(base_res.get("total_latency_ms") or 0.0), 1e-6))
        base_r = self.reward(base_res)

        cur_spec, cur_r = _dc(spec), base_r
        best_spec, best_r, best_res = _dc(spec), base_r, base_res
        history, no_improve = [], 0

        for t in range(1, episodes + 1):
            arm = self._select_arm(t)
            mutated = ACTIONS[arm](cur_spec, self.rng)
            if mutated is None or not _is_valid(mutated["spec"]):
                self.arms[arm]["n"] += 1        # 不适用也算一次尝试（避免死循环选它）
                self.arms[arm]["total"] += -0.2
                history.append({"episode": t, "op": arm, "applied": False,
                                "reason": "不适用或拓扑非法"})
                continue

            res = self.evaluate(mutated["spec"])
            r = self.reward(res)
            gain = r - cur_r
            self.arms[arm]["n"] += 1
            self.arms[arm]["total"] += gain
            self.arms[arm]["best"] = max(self.arms[arm]["best"], gain)

            accepted = r > cur_r
            if accepted:                        # 爬山：只接受更优
                cur_spec, cur_r = mutated["spec"], r
            if r > best_r:
                best_spec, best_r, best_res = _dc(mutated["spec"]), r, res
                no_improve = 0
            else:
                no_improve += 1

            history.append({
                "episode": t, "op": arm, "applied": True,
                "node": mutated.get("node"), "detail": mutated.get("detail"),
                "reward": round(r, 4), "gain": round(gain, 4),
                "accepted": accepted, "quality": round(
                    float(res.get("final_quality") or 0.0), 3),
                "cost": round(float(res.get("total_cost") or 0.0), 4),
            })
            if no_improve >= patience:
                break                           # 收敛：连续 N 轮无提升

        arm_stats = {k: {"tried": v["n"],
                         "avg_gain": round(v["total"] / v["n"], 4) if v["n"] else 0.0,
                         "best_gain": round(v["best"], 4)}
                     for k, v in self.arms.items()}
        out = {
            "baseline_reward": round(base_r, 4),
            "best_reward": round(best_r, 4),
            "improvement": round(best_r - base_r, 4),
            "improved": best_r > base_r + 1e-9,
            "baseline": {"quality": round(float(base_res.get("final_quality") or 0), 3),
                         "cost": round(float(base_res.get("total_cost") or 0), 4),
                         "latency_ms": round(float(base_res.get("total_latency_ms") or 0), 1),
                         "nodes": len(spec.get("components", {}))},
            "best": {"quality": round(float(best_res.get("final_quality") or 0), 3),
                     "cost": round(float(best_res.get("total_cost") or 0), 4),
                     "latency_ms": round(float(best_res.get("total_latency_ms") or 0), 1),
                     "nodes": len(best_spec.get("components", {}))},
            "best_spec": best_spec,
            "episodes_run": len(history),
            "converged": no_improve >= patience,
            "arm_stats": arm_stats,
            "history": history,
        }
        return out

    # ---- 沉淀 ----
    def distill(self, opt_result: dict, goal_desc: str = "rl_optimized") -> Optional[dict]:
        """把搜到的最优拓扑写进 TopologyMemory，供后续同类任务 recall 复用。"""
        if not opt_result.get("improved"):
            return None                     # 没变好就不污染记忆
        try:
            from .topology_memory import TopologyMemory
            mem = self.memory or TopologyMemory()
            b = opt_result["best"]
            return mem.record(goal_desc, opt_result["best_spec"], {
                "success": True,
                "final_quality": b["quality"],
                "total_cost": b["cost"],
                "total_latency_ms": b["latency_ms"],
                "components": {},
            })
        except Exception:
            return None

    # ---- 输入归一 ----
    @staticmethod
    def _to_spec(spec_or_goal):
        if isinstance(spec_or_goal, dict) and "components" in spec_or_goal:
            return spec_or_goal
        import os
        os.environ.pop("AGENT_API_KEY", None)   # 强制离线规则解析
        from .nl_parser import GoalParser
        from .compile import compile_goal
        goal = GoalParser().parse(str(spec_or_goal))
        return compile_goal(goal, auto_bind=True, route=True, memory_enabled=False)


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def rl_optimizer_selftest():
    """Phase 2 第三层① RL 优化拓扑 离线自检（无 key/无网，真实执行 reward）。"""
    import os
    os.environ.pop("AGENT_API_KEY", None)

    base_spec = {
        "name": "rl_demo",
        "components": {
            "src":  {"type": "power", "label": "task"},
            "A":    {"type": "resistor", "label": "research", "model": "large",
                     "yield": 1.0, "produced_outputs": ["a"]},
            "B":    {"type": "resistor", "label": "analyze", "model": "large",
                     "yield": 1.0, "required_inputs": ["a"], "produced_outputs": ["b"]},
            "V":    {"type": "verify", "label": "verify_b", "threshold": 0.5},
            "C":    {"type": "resistor", "label": "summarize", "model": "large",
                     "yield": 1.0, "required_inputs": ["b"]},
        },
        "wires": [["src", "A"], ["A", "B"], ["B", "V"], ["V", "C"]],
    }

    # 1) 变异算子：各自产出合法拓扑（或明确返回 None 表示不适用）
    rng = random.Random(0)
    applied = {}
    for name, fn in ACTIONS.items():
        r = fn(base_spec, random.Random(1))
        if r is not None:
            assert _is_valid(r["spec"]), f"{name} 产出非法拓扑"
            applied[name] = r["detail"]
    assert len(applied) >= 3, f"至少 3 个算子应适用于该拓扑，实际 {list(applied)}"
    print(f"✓ RL① 变异算子: {len(applied)}/5 适用且产出合法拓扑 · {applied}")

    # 2) 合法性守卫：造一个带环的拓扑，必须被拒
    bad = _dc(base_spec)
    bad["wires"].append(["C", "A"])          # C→A 成环
    assert _has_cycle(bad) and not _is_valid(bad), "带环拓扑应被判非法"
    bad2 = _dc(base_spec)
    bad2["components"]["ORPHAN"] = {"type": "resistor", "label": "orphan"}
    assert not _is_valid(bad2), "孤立节点应被判非法"
    print("✓ RL① 合法性守卫: 环 / 孤立节点 均被拒（不会搜出坏拓扑）")

    # 3) reward 可复现 + 真实执行（同 spec 同 seed → 同 reward）
    opt = RLOptimizer(seed=0, exec_seed=0)
    r1 = opt.evaluate(base_spec)
    opt._base = (max(r1["total_cost"], 1e-6), max(r1["total_latency_ms"], 1e-6))
    v1 = opt.reward(r1)
    opt2 = RLOptimizer(seed=0, exec_seed=0)
    r2 = opt2.evaluate(base_spec)
    opt2._base = opt._base
    v2 = opt2.reward(r2)
    assert abs(v1 - v2) < 1e-9, f"同 seed reward 应可复现: {v1} vs {v2}"
    assert r1["final_quality"] > 0, "应真实执行出质量分（非模拟打分）"
    print(f"✓ RL① 真实 reward: 由 CircuitExecutor 真跑得出 "
          f"quality={r1['final_quality']} cost={r1['total_cost']} → reward={v1:.4f}（可复现）")

    # 4) 搜索确有提升（全 large 档的浪费拓扑 → 应被搜出更优结构）
    opt3 = RLOptimizer(seed=7, exec_seed=0)
    res = opt3.optimize(base_spec, episodes=30, patience=15)
    assert res["improved"], f"应搜到更优拓扑，improvement={res['improvement']}"
    assert res["best_reward"] > res["baseline_reward"], "最优 reward 应高于基线"
    assert res["best"]["cost"] <= res["baseline"]["cost"] * 1.05 or \
        res["best"]["quality"] > res["baseline"]["quality"], \
        "提升应来自省成本或提质量"
    print(f"✓ RL① 搜索有效: reward {res['baseline_reward']}→{res['best_reward']} "
          f"(+{res['improvement']}) · 成本 {res['baseline']['cost']}→{res['best']['cost']} "
          f"· 质量 {res['baseline']['quality']}→{res['best']['quality']} "
          f"· {res['episodes_run']} 轮{'(收敛)' if res['converged'] else ''}")

    # 5) 可解释：arm_stats 给出「哪类改动真有用」
    stats = res["arm_stats"]
    assert set(stats) == set(ACTIONS), "每个算子都应有统计"
    tried = sum(v["tried"] for v in stats.values())
    assert tried == res["episodes_run"], "尝试次数应与轮数一致"
    top = max(stats.items(), key=lambda kv: kv[1]["best_gain"])
    print(f"✓ RL① 可解释: 5 算子收益统计齐全 · 最大单步收益算子={top[0]} "
          f"(+{top[1]['best_gain']})")

    # 6) 沉淀到记忆（用临时库，不污染真实记忆）
    import tempfile
    from .topology_memory import TopologyMemory
    tmp = tempfile.mktemp(suffix=".json")
    mem = TopologyMemory(path=tmp)
    opt4 = RLOptimizer(seed=7, exec_seed=0, memory=mem)
    entry = opt4.distill(res, goal_desc="RL 优化后的分析拓扑")
    assert entry is not None and mem.stats()["total"] == 1, "最优拓扑应沉淀进记忆"
    hit = mem.recall("RL 优化后的分析拓扑", min_quality=0.0, min_similarity=0.1)
    assert hit is not None, "沉淀后应能被 recall 复用"
    # 未提升时不写库（不污染）
    noop = RLOptimizer(seed=1, memory=mem).distill({"improved": False})
    assert noop is None and mem.stats()["total"] == 1, "未提升不应写入记忆"
    print(f"✓ RL① 沉淀复用: 最优拓扑写入 TopologyMemory 并可 recall · 未提升不污染库")
    try:
        os.unlink(tmp)
    except OSError:
        pass

    # 7) goal 字符串路径（端到端：自然语言 → 编译 → 搜索）
    opt5 = RLOptimizer(seed=3, exec_seed=0)
    res5 = opt5.optimize("分析两份报告并总结要点", episodes=10, patience=6)
    assert res5["best_spec"].get("components"), "goal 路径应产出拓扑"
    assert res5["episodes_run"] >= 1, "goal 路径应真实搜索"
    print(f"✓ RL① goal 端到端: 自然语言→编译→搜索 {res5['episodes_run']} 轮 · "
          f"reward {res5['baseline_reward']}→{res5['best_reward']}")

    print("\nPhase 2 第三层① RL 优化拓扑 离线自检全部通过 ✓")


if __name__ == "__main__":
    rl_optimizer_selftest()
