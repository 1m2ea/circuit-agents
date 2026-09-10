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

# 不可写面审计（MetaRSI 定律四）——导入失败则静默降级，绝不拖崩搜索主流程
try:
    from .holdout import replay_on_holdout as _replay_on_holdout
    from .holdout import verdict as _holdout_verdict
    _HOLDOUT_OK = True
except Exception:                                   # pragma: no cover
    _replay_on_holdout = None
    _holdout_verdict = None
    _HOLDOUT_OK = False

# 纵轴优化（MetaRSI）：算子内部策略的可写面 —— 导入失败同样静默降级
try:
    from .operator_policy import OperatorPolicy as _OperatorPolicy
    from .operator_policy import VerticalSubAgent as _VerticalSubAgent
    _VERTICAL_OK = True
except Exception:                                   # pragma: no cover
    _OperatorPolicy = None
    _VerticalSubAgent = None
    _VERTICAL_OK = False


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


def act_swap_model(spec, rng, policy=None):
    """算子1 换档：随机挑一个电阻换 model 档位（小/大/工具）。

    policy（纵轴可写面，可选）：{"tiers": [...]} 收窄/放开候选档位。
    不传 → 用全量 _TIERS，行为与改前完全一致（零回归）。
    """
    rs = _resistors(spec)
    if not rs:
        return None
    new = _dc(spec)
    cid = rng.choice(rs)
    cur = new["components"][cid].get("model", "small")
    tiers = (policy or {}).get("tiers") or _TIERS
    cand = [t for t in tiers if t != cur] or [t for t in _TIERS if t != cur]
    new["components"][cid]["model"] = rng.choice(cand)
    return {"op": "swap_model", "node": cid, "spec": new,
            "detail": f"{cur}→{new['components'][cid]['model']}"}


def act_add_verify(spec, rng, policy=None):
    """算子2 加校验：给某电阻挂一个下游 verify 节点（提质量、增成本）。

    policy（纵轴可写面，可选）：{"threshold": 0.3~0.9} 校验通过阈值。
    不传 → 0.6，行为与改前完全一致（零回归）。
    """
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
                              "threshold": (policy or {}).get("threshold", 0.6)}
    new["wires"] = [w for w in new["wires"] if w[0] != cid]
    new["wires"].append([cid, vid])
    for s in old_succs:
        new["wires"].append([vid, s])
    return {"op": "add_verify", "node": cid, "spec": new, "detail": f"+{vid}"}


def act_drop_verify(spec, rng, policy=None):
    """算子3 删冗余：摘掉一个 verify/adc 节点（省成本延迟，可能掉质量）。

    无可写参数（policy 仅占位，保持与其余算子一致的调用接口）。
    """
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


def act_parallelize(spec, rng, policy=None):
    """算子4 并联化：把 A→B 的串联拆成 A、B 共享上游并行（降延迟）。

    无可写参数（policy 仅占位，保持与其余算子一致的调用接口）。

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


def act_add_capacitor(spec, rng, policy=None):
    """算子5 加汇合：给多入度节点前插电容做汇合（提完整性）。

    policy（纵轴可写面，可选）：{"mode": "all"}。
    注意：mode 属 FROZEN 参数——OperatorPolicy.set 会拒绝自动改写，
    这里仍接受显式传入，仅供人工/调用方指定。不传 → "all"，零回归。
    """
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
                              "mode": (policy or {}).get("mode", "all")}
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
                 memory=None, exec_seed: int = 0, c_ucb: float = 1.4,
                 holdout: bool = True, holdout_tasks=None,
                 holdout_max_tasks: int = 0,
                 vertical: bool = False, policy=None,
                 revise_every: int = 8, holdout_backend=None):
        self.w = dict(self.DEFAULT_WEIGHTS)
        if weights:
            self.w.update(weights)
        self.rng = random.Random(seed)
        self.memory = memory
        self.exec_seed = exec_seed      # 执行 seed 固定 → 同拓扑 reward 可复现
        self.c_ucb = c_ucb
        self.arms = {k: {"n": 0, "total": 0.0, "best": 0.0} for k in ACTIONS}
        self._base = None               # 基线 (cost, latency) 用于归一化
        # 不可写面审计（MetaRSI 定律四）：默认开，失败静默降级，不影响搜索
        self.holdout = bool(holdout)
        self.holdout_tasks = holdout_tasks
        self.holdout_max_tasks = int(holdout_max_tasks or 0)
        # 传真后端 → holdout 才有真实辨别力（离线 add_verify/drop_verify delta 恒为 0）
        self.holdout_backend = holdout_backend
        # 纵轴优化（MetaRSI）：改写算子**内部策略**（横轴只决定「选哪个算子」）
        # 默认 OFF —— 实测（2026-09-10，4 seed A/B）开启后 3 差 1 平 0 好：
        #   seed 7: 0.175→0.0875 | 3: 0.1975→0.1015 | 11: 持平 | 42: 0.4492→0.0875
        # 根因：UCB1 的 avg_gain 是累计均值，早期负样本即拉负 → MIN_TRIED=3 过低
        #   → 过早砍掉 tool 档 → 搜索空间收窄 → 搜不到更省的档位组合。
        # 机制已完整可启用（vertical=True），但**默认不启用**：
        #   离线 SimBackend 无法验证其收益，接真后端后用 holdout 度量再决定。
        self.vertical = bool(vertical) and _VERTICAL_OK
        self.policy = policy if policy is not None else (
            _OperatorPolicy() if _VERTICAL_OK else None)
        self.revise_every = int(revise_every or 0)
        self.sub_agent = _VerticalSubAgent() if _VERTICAL_OK else None
        self.vertical_changes: list = []     # 纵轴改写审计轨迹

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
            mutated = ACTIONS[arm](cur_spec, self.rng,
                                   self.policy.for_op(arm) if self.policy else None)
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
            # 纵轴优化（MetaRSI）：每 revise_every 轮按历史收益改写算子**内部策略**
            # —— 横轴改「选谁」，纵轴改「被选中后怎么干」
            if self.vertical and self.revise_every and t % self.revise_every == 0:
                self._vertical_revise(t)

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
            "policy": self.policy.snapshot() if self.policy else None,
            "vertical_changes": list(self.vertical_changes),
            "vertical_enabled": self.vertical,
        }
        out.update(self._audit_holdout(history, out["improvement"]))
        return out

    # ---- 纵轴优化（MetaRSI）：改写算子内部策略 ----
    def _vertical_revise(self, round_no: int):
        """按各算子历史收益改写策略（规则式，全程留痕，失败静默）。"""
        if not self.vertical or self.sub_agent is None or self.policy is None:
            return
        try:
            stats = {k: {"tried": v["n"],
                         "avg_gain": (v["total"] / v["n"]) if v["n"] else 0.0}
                     for k, v in self.arms.items()}
            changes = self.sub_agent.revise(self.policy, stats, round_no)
            if changes:
                self.vertical_changes.extend(changes)
        except Exception:
            pass                                # 纵轴失败不影响搜索主流程

    # ---- 不可写面审计（MetaRSI 定律四）----
    def _audit_holdout(self, history: list, improvement: float) -> dict:
        """把本轮被接受的算子序列重放到 holdout 任务集，检验是否真的泛化。

        为什么需要它：闭环内部「真变强」和「标准被放宽」得分相同且不可自察。
        holdout 任务不参与搜索，只用来验证「这套改法在别的任务上是否也涨分」。
        全程 try/except —— 审计失败只写 error，绝不拖崩 optimize。
        """
        if not self.holdout or not _HOLDOUT_OK:
            return {"holdout_audit": {"n": 0, "error": "holdout 未启用或模块不可用",
                                      "verdict": "NO_DATA", "per_task": []}}
        ops = [h.get("op") for h in history
               if h.get("applied") and h.get("accepted")]
        try:
            if not ops:
                audit = {"n": 0, "error": "本轮没有被接受的算子改动（无可重放序列）",
                         "mean_delta": 0.0, "median_delta": 0.0,
                         "positive_rate": 0.0, "per_task": []}
            else:
                audit = _replay_on_holdout(
                    ops, tasks=self.holdout_tasks, seed=self.exec_seed,
                    max_tasks=self.holdout_max_tasks,
                    backend=self.holdout_backend)
        except Exception as e:
            audit = {"n": 0, "error": f"{type(e).__name__}: {e}",
                     "mean_delta": 0.0, "median_delta": 0.0,
                     "positive_rate": 0.0, "per_task": []}
        try:
            v = _holdout_verdict(audit, improvement)
        except Exception:
            v = "NO_DATA"
        return {"holdout_audit": {**audit, "verdict": v,
                                  "replayed_ops": ops,
                                  "self_reported_improvement": round(improvement, 4)}}

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


def holdout_selftest():
    """P0 不可写面 holdout 审计 离线自检（MetaRSI 定律四）。"""
    import os
    os.environ.pop("AGENT_API_KEY", None)
    from .holdout import (HOLDOUT_TASKS, load_tasks, replay_on_holdout,
                          verdict, EPS_GAIN, HoldoutVerifier, MockJudge)

    # 1) 任务集只读语义：load_tasks 返回副本，改不到模块常量
    t = load_tasks()
    assert t == list(HOLDOUT_TASKS), "load_tasks 应返回任务集内容"
    t.append("污染测试")
    assert len(load_tasks()) == len(HOLDOUT_TASKS), "任务集应不可被外部修改（只读语义）"
    print(f"✓ 不可写面: holdout 任务集 {len(HOLDOUT_TASKS)} 个，外部改不到模块常量")

    # 2) 重放跑通：跑 2 个任务（控耗时），产出可比绝对分
    a1 = replay_on_holdout(["add_verify"], max_tasks=2, seed=0)
    assert a1["n"] == 2, f"应覆盖 2 个 holdout 任务, got {a1['n']}"
    assert not a1.get("error"), f"重放不应出错: {a1.get('error')}"
    scored = [p["delta"] for p in a1["per_task"] if p.get("delta") is not None]
    assert scored, "应有可比的绝对分 delta"
    print(f"✓ 重放跑通: {a1['n']} 任务 · mean_delta={a1['mean_delta']} "
          f"· positive_rate={a1['positive_rate']} · per_task={[p['delta'] for p in a1['per_task']]}")

    # 3) 确定性：同参两次 → 同一结果（审计本身可复现，否则无从谈"基准"）
    a2 = replay_on_holdout(["add_verify"], max_tasks=2, seed=0)
    assert a2["mean_delta"] == a1["mean_delta"], "同 seed 审计结果应可复现"
    assert [p["delta"] for p in a2["per_task"]] == [p["delta"] for p in a1["per_task"]]
    print("✓ 审计可复现: 同 seed 两次 mean_delta 完全一致")

    # 4) 降级：空算子 / 不适用算子 → 只报 error，不抛异常
    e1 = replay_on_holdout([], max_tasks=2)
    assert e1.get("error") and e1["n"] == 0, "空算子序列应诚实报 error"
    e2 = replay_on_holdout(["__不存在的算子__"], max_tasks=2)
    assert e2.get("error") or all(p.get("delta") is None for p in e2["per_task"]), \
        "不适用算子应降级而非崩溃"
    print("✓ 降级不拖崩: 空算子/不适用算子均只写 error，不抛异常")

    # 5) verdict 裁决逻辑（定律四的核心判据）
    gen = {"n": 4, "error": None, "mean_delta": 0.12, "positive_rate": 0.75}
    over = {"n": 4, "error": None, "mean_delta": -0.05, "positive_rate": 0.25}
    mixed = {"n": 4, "error": None, "mean_delta": 0.05, "positive_rate": 0.4}
    assert verdict(gen, 0.5) == "GENERALIZES", "自报涨+holdout 涨 → 真改进"
    assert verdict(over, 0.5) == "OVERFIT", "自报涨但 holdout 跌 → 定律四命中"
    assert verdict(mixed, 0.5) == "MIXED", "半数以下为正 → 证据不足"
    # 修复验证：裁决的「赢」只能由外部 holdout(judge) 认定，循环自报不能否决/自证
    assert verdict(gen, 0.0) == "GENERALIZES", "外部 judge 证真即泛化，循环自报 0 不能否决（修自我判赢）"
    assert verdict(over, 0.0) == "NO_CLAIM", "自报没涨且外部不涨 → 诚实 NO_CLAIM，而非 OVERFIT"
    assert verdict(over, None) == "NO_CLAIM", "循环无自报且外部不涨 → NO_CLAIM"
    assert verdict({"n": 0, "error": "x"}, 0.5) == "NO_DATA", "审计没跑成 → 不妄下结论"
    print("✓ 裁决逻辑: GENERALIZES / OVERFIT / MIXED / NO_CLAIM / NO_DATA 五态齐全")

    # 6) 集成：optimize 结果自带审计，且可关闭
    base_spec = {
        "name": "rl_holdout",
        "components": {
            "src": {"type": "power", "label": "task"},
            "A": {"type": "resistor", "label": "research", "model": "large",
                  "yield": 1.0, "produced_outputs": ["a"]},
            "B": {"type": "resistor", "label": "analyze", "model": "large",
                  "yield": 1.0, "required_inputs": ["a"], "produced_outputs": ["b"]},
            "V": {"type": "verify", "label": "verify_b", "threshold": 0.5},
            "C": {"type": "resistor", "label": "summarize", "model": "large",
                  "yield": 1.0, "required_inputs": ["b"]},
        },
        "wires": [["src", "A"], ["A", "B"], ["B", "V"], ["V", "C"]],
    }
    opt = RLOptimizer(seed=7, exec_seed=0, holdout_max_tasks=2)
    res = opt.optimize(base_spec, episodes=12, patience=8)
    aud = res.get("holdout_audit")
    assert aud is not None, "optimize 应自带 holdout_audit"
    assert aud["verdict"] in ("GENERALIZES", "OVERFIT", "MIXED",
                              "NO_CLAIM", "NO_DATA"), f"verdict 越界: {aud['verdict']}"
    assert res["improved"], "接入审计不应影响搜索有效性（零回归）"
    print(f"✓ 集成: improvement={res['improvement']} → verdict={aud['verdict']} "
          f"(mean_delta={aud.get('mean_delta')}, 重放 {len(aud.get('replayed_ops') or [])} 个算子)")

    opt_off = RLOptimizer(seed=7, exec_seed=0, holdout=False)
    res_off = opt_off.optimize(base_spec, episodes=12, patience=8)
    assert res_off["holdout_audit"]["verdict"] == "NO_DATA", "关闭后不应跑审计"
    print("✓ 可关闭: holdout=False 时 verdict=NO_DATA，主流程不受影响")

    # 8) 真后端接线：dry_run 证明注入通路正确（无 key / 无网也能验）
    try:
        from .backend_llm import RealLLMBackend
        _dry = RealLLMBackend(rng=random.Random(0), dry_run=True)
        _ad = replay_on_holdout(["swap_model"], max_tasks=2, seed=0,
                                backend=_dry)
        assert _ad["n"] == 2, f"真后端应覆盖 2 任务, got {_ad['n']}"
        assert _ad["backend"] == "real", "传入 backend 应标记为 real"
        assert _ad["baseline_cached"] is False, \
            "真后端有波动 → 基线不应缓存（否则拿单次采样当基准）"
        print(f"✓ 真后端接线: dry_run 跑通 · backend={_ad['backend']} · "
              f"基线不缓存 · mean_delta={_ad['mean_delta']} "
              f"（传真后端即可测真实辨别力）")
    except Exception as e:                      # 后端不可用不拖崩自检
        print(f"  (跳过真后端 dry_run 验证: {type(e).__name__}: {e})")

    # 9) P2′ judge 路径：注入 oracle（本地模型的 stand-in），证明审计改用 judge 分后
    #    辨别力随 judge 改变（add_verify 在 final_quality 下恒为 0，在真 judge 下应转正）
    #    oracle：基准 0.5；加了 add_verify 的答案判到 0.8（模拟本地模型认为校验后更正确）
    oracle = MockJudge({
        (HOLDOUT_TASKS[0], frozenset(), False): 0.5,
        (HOLDOUT_TASKS[0], frozenset(["add_verify"]), True): 0.8,
        (HOLDOUT_TASKS[1], frozenset(), False): 0.5,
        (HOLDOUT_TASKS[1], frozenset(["add_verify"]), True): 0.8,
    })
    a9 = replay_on_holdout(["add_verify"], max_tasks=2, seed=0,
                           verifier=HoldoutVerifier(oracle))
    assert a9["n"] == 2, f"judge 审计应覆盖 2 任务, got {a9['n']}"
    assert a9.get("verdict_metric") == "judge", "应标记 metric=judge"
    assert a9.get("verifier") == "HoldoutVerifier", "应记录所用 verifier 类型"
    scored = [p["delta"] for p in a9["per_task"] if p.get("delta") is not None]
    assert scored and all(d > EPS_GAIN for d in scored), \
        f"真 judge 下 add_verify 应转正 delta, got {scored}"
    assert a9["mean_delta"] > 0.2, f"judge 应给出明显正 delta, got {a9['mean_delta']}"
    print(f"✓ P2′ judge 路径: 注入 oracle 后 add_verify mean_delta="
          f"{a9['mean_delta']}（final_quality 下恒为 0）→ 辨别力随 judge 改变")

    print("\nP0 不可写面 holdout 审计 离线自检全部通过 ✓")


def vertical_selftest():
    """P1 纵轴优化（算子内部策略可写面）离线自检。"""
    import os
    os.environ.pop("AGENT_API_KEY", None)
    from .operator_policy import (DEFAULT_POLICY, OperatorPolicy,
                                  VerticalSubAgent, BOUNDS)

    # 1) 默认策略 == 改前的硬编码常量（零回归的前提）
    p0 = OperatorPolicy()
    assert p0.snapshot() == DEFAULT_POLICY, "默认策略应等价于原硬编码常量"
    assert p0.get("add_verify", "threshold") == 0.6, "默认 threshold 应为 0.6"
    print("✓ 零回归前提: 默认策略 == 原硬编码常量（threshold=0.6 / mode=all / 三档）")

    # 2) 算子真的读策略（不是摆设）
    _spec = {"name": "v", "components": {
        "src": {"type": "power", "label": "task"},
        "A": {"type": "resistor", "label": "reason", "model": "small",
              "yield": 1.0, "produced_outputs": ["a"]},
        "C": {"type": "resistor", "label": "summarize", "model": "small",
              "yield": 1.0, "required_inputs": ["a"]}},
        "wires": [["src", "A"], ["A", "B" if False else "C"]]}
    r_hi = ACTIONS["add_verify"](_spec, random.Random(1), {"threshold": 0.85})
    assert r_hi is not None, "add_verify 应适用"
    _vid = r_hi["detail"].lstrip("+")
    assert r_hi["spec"]["components"][_vid]["threshold"] == 0.85, \
        "算子应读 policy 的 threshold"
    r_lo = ACTIONS["add_verify"](_spec, random.Random(1), {"threshold": 0.35})
    assert r_lo["spec"]["components"][_vid]["threshold"] == 0.35
    r_no = ACTIONS["add_verify"](_spec, random.Random(1))
    assert r_no["spec"]["components"][_vid]["threshold"] == 0.6, \
        "不传 policy 应回落默认 0.6（零回归）"
    # swap_model 收窄档位生效
    r_t = ACTIONS["swap_model"](_spec, random.Random(1), {"tiers": ["small", "large"]})
    assert r_t["spec"]["components"][r_t["node"]]["model"] in ("small", "large")
    print("✓ 算子读策略: threshold 0.85/0.35 生效 · 不传回落 0.6 · "
          "swap_model 档位收窄生效")

    # 3) FROZEN 参数绝不自动改
    p1 = OperatorPolicy()
    changed = p1.set("add_capacitor", "mode", "any", reason="测试")
    assert changed is False and p1.get("add_capacitor", "mode") == "all", \
        "FROZEN 参数不应被改写"
    assert any(h.get("action") == "rejected" for h in p1.history), \
        "被拒也应留痕（可审计）"
    print("✓ FROZEN 生效: capacitor.mode 自动改写被拒且留痕（语义不可逆，留人工）")

    # 4) 边界夹紧：策略不会跑飞
    lo, hi, _step = BOUNDS["add_verify"]["threshold"]
    p2 = OperatorPolicy()
    p2.set("add_verify", "threshold", 99.0)
    assert p2.get("add_verify", "threshold") == hi, "上界应夹紧"
    p2.set("add_verify", "threshold", -99.0)
    assert p2.get("add_verify", "threshold") == lo, "下界应夹紧"
    print(f"✓ 边界夹紧: threshold 越界被夹回 [{lo}, {hi}]（策略跑不飞）")

    # 5) 纵轴改写：收益差 → 收缩；收益好 → 加严；样本不足 → 不动
    sub = VerticalSubAgent()
    p3 = OperatorPolicy()
    c = sub.revise(p3, {"add_verify": {"tried": 5, "avg_gain": -0.5}}, round_no=8)
    assert c and c[0]["action"] == "contract", f"负收益应收缩, got {c}"
    assert p3.get("add_verify", "threshold") < 0.6, "收缩后 threshold 应下降"
    print(f"✓ 纵轴收缩: add_verify avg_gain=-0.5 → threshold "
          f"0.6→{p3.get('add_verify','threshold')}（放宽止损）")

    p4 = OperatorPolicy()
    e = sub.revise(p4, {"add_verify": {"tried": 5, "avg_gain": 0.5}}, round_no=8)
    assert e and e[0]["action"] == "expand", f"正收益应加严, got {e}"
    assert p4.get("add_verify", "threshold") > 0.6, "加严后 threshold 应上升"
    print(f"✓ 纵轴加严: add_verify avg_gain=+0.5 → threshold "
          f"0.6→{p4.get('add_verify','threshold')}（把有效手段用足）")

    p5 = OperatorPolicy()
    n = sub.revise(p5, {"add_verify": {"tried": 1, "avg_gain": -9.0}}, round_no=8)
    assert n == [] and p5.get("add_verify", "threshold") == 0.6, \
        "样本不足不应妄动（防单点抖动误调）"
    print("✓ 样本不足不动: tried=1 即使收益极差也不改（MIN_TRIED=3 护栏）")

    # 6) swap_model 档位收缩/恢复
    p6 = OperatorPolicy()
    c6 = sub.revise(p6, {"swap_model": {"tried": 5, "avg_gain": -0.5}}, round_no=8)
    assert c6 and "tool" not in p6.get("swap_model", "tiers"), "负收益应去掉最贵档"
    sub.revise(p6, {"swap_model": {"tried": 9, "avg_gain": 0.5}}, round_no=16)
    assert "tool" in p6.get("swap_model", "tiers"), "正收益应恢复全档探索"
    print("✓ 纵轴档位: swap_model 负收益去 tool 档控成本 → 正收益恢复全档")

    # 7) 默认关闭（实测离线有害）+ 显式开启可用
    assert RLOptimizer(seed=7).vertical is False, \
        "默认应关闭纵轴（实测 4 seed 3 差 1 平，见 __init__ 注释）"
    print("✓ 默认关闭: 纵轴默认 OFF（实测净负收益，机制保留待真后端度量）")

    _vs = {
        "name": "vertical_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "A": {"type": "resistor", "label": "research", "model": "large",
                  "yield": 1.0, "produced_outputs": ["a"]},
            "B": {"type": "resistor", "label": "analyze", "model": "large",
                  "yield": 1.0, "required_inputs": ["a"], "produced_outputs": ["b"]},
            "V": {"type": "verify", "label": "verify_b", "threshold": 0.5},
            "C": {"type": "resistor", "label": "summarize", "model": "large",
                  "yield": 1.0, "required_inputs": ["b"]},
        },
        "wires": [["src", "A"], ["A", "B"], ["B", "V"], ["V", "C"]],
    }
    opt = RLOptimizer(seed=7, exec_seed=0, holdout_max_tasks=2,
                      revise_every=8, vertical=True)
    res = opt.optimize(_vs, episodes=16, patience=10)
    assert res["vertical_enabled"] is True, "显式开启后应生效"
    assert isinstance(res["policy"], dict) and res["policy"], "应返回最终策略快照"
    assert isinstance(res["vertical_changes"], list), "应返回纵轴改写轨迹"
    assert res["improved"], "接入纵轴不应破坏搜索有效性（零回归）"
    print(f"✓ 集成: improvement={res['improvement']} · 纵轴改写 "
          f"{len(res['vertical_changes'])} 次 · 最终 threshold="
          f"{res['policy'].get('add_verify', {}).get('threshold')}")

    opt_off = RLOptimizer(seed=7, exec_seed=0, holdout=False, vertical=False)
    res_off = opt_off.optimize(_vs, episodes=16, patience=10)
    assert res_off["vertical_enabled"] is False
    assert res_off["vertical_changes"] == [], "关闭纵轴不应有改写"
    assert res_off["policy"]["add_verify"]["threshold"] == 0.6, \
        "关闭纵轴时策略保持默认（零回归）"
    print("✓ 可关闭: vertical=False → 零改写 · 策略保持默认 0.6（零回归）")

    print("\nP1 纵轴优化（算子内部策略可写面）离线自检全部通过 ✓")


if __name__ == "__main__":
    rl_optimizer_selftest()
    print()
    holdout_selftest()
    print()
    vertical_selftest()
