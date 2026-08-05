"""compiler/simplify.py — 奥卡姆剃刀化简 Pass（OckhamsRazor）。

把"如无必要，勿增实体"固化成编译器优化 pass：对已生成的拓扑做结构精简，
用最小的必要结构拿到等价的结果。它**不预判任务简不简单**，只对每节点/每边
追问"删掉它，结果会变吗？"，用（去噪确定性）真实执行对比判定等价——删掉
结果不变即剃落，不确定/会变/伤完整性即保留。

复杂任务的并行支路、多重验证、反馈环结构本就不冗余，等价判定自然保留它们；
简单任务里的冗余 adc、重复 retrieve、空转 organize 被一扫而空。

等价判定采用**去噪确定性模拟**（复制 SimBackend 质量语义但去掉 random 噪声 +
yield 随机开路），使删前/删后两版在"逻辑结果"层面可比，不被仿真噪声伪影干扰。
这正是奥卡姆剃刀要判定的"结构必要性"，而非"仿真实现细节是否一致"。
"""

import os
import sys
import copy

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # circuit-agents 根目录
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from runtime import Circuit, SimBackend, Signal

# 复用 SimBackend 的档位表（small/large/tool 的 accuracy/cost/latency）
_TIERS = SimBackend._TIERS


# ──────────────────────────────────────────────────────────
# 去噪确定性模拟（仅用于等价判定）
# ──────────────────────────────────────────────────────────

def _quiet_run(comp, inputs):
    """去噪版 SimBackend 语义：复制质量计算逻辑，但去掉 uniform(-0.03, 0.03)
    随机噪声 + yield 随机开路，使"结构等价"的两版拓扑在质量层面完全可比。

    质量语义（与 SimBackend 一致）：
      - power/source         : 产出固定质量（1.0 / 节点 quality）
      - resistor             : q = min(上游质量, 自身精度上限 cap)，开路则 q=0
      - adc                  : 透传质量，按 threshold 给 high/low 电平
      - capacitor(any/all)   : 取 max 质量 + 任一/全部 ok
      - 其余透传元件          : 透传质量 + 全部 ok
    """
    t = comp.get("type")
    q_in = max((s.quality for s in inputs), default=0.0)
    all_ok = all(s.ok for s in inputs) if inputs else True
    if t == "power":
        return Signal(value=comp.get("task", ""), quality=1.0, ok=True)
    if t == "source":
        return Signal(value=comp.get("label"), quality=comp.get("quality", 0.9), ok=True)
    if t == "resistor":
        cap = _TIERS.get(comp.get("model", "small"), _TIERS["small"])["accuracy"]
        inp = max((s.quality for s in inputs if s.ok), default=0.0)
        if inp <= 0:
            return Signal(value=None, quality=0.0, ok=False)  # 开路必须保持开路
        return Signal(value="result", quality=min(inp, cap), ok=True)
    if t == "adc":
        thr = comp.get("threshold", 0.8)
        return Signal(value=q_in, quality=q_in, ok=(q_in >= thr))
    if t == "capacitor":
        ok = any(s.ok for s in inputs) if comp.get("mode") == "any" else all_ok
        return Signal(value=None, quality=q_in, ok=ok)
    # opamp / diode / format_adapter / watchdog / bridge_rectifier / logic_gate：透传
    return Signal(value=None, quality=q_in, ok=all_ok)


def _safe_circuit(spec):
    """构造仅用于拓扑分层的 Circuit。

    Circuit.__init__ 要求 spec["feedback"] 是 {from,to} 格式（反馈边）。
    对无 from/to 的格式（如 {max_iter:N}），临时忽略 feedback 键，避免 KeyError——
    分层只关心前向边，等价判定不依赖反馈环的具体节点。
    """
    import random
    fb = spec.get("feedback")
    if fb and ("from" not in fb or "to" not in fb):
        safe = {k: v for k, v in spec.items() if k != "feedback"}
        return Circuit(safe, SimBackend(random.Random(0)))
    return Circuit(spec, SimBackend(random.Random(0)))


def _simulate_quiet(spec):
    """去噪确定性模拟：返回 {final_quality, terminals:{cid:(q,ok)}, q:{cid:q}, ok:{cid:bool}}。

    final_quality 对齐 CircuitExecutor.run：所有**无后继**终端节点的最大质量。
    """
    comps = spec["components"]
    wires = spec.get("wires", [])
    preds = {c: [] for c in comps}
    for f, t in wires:
        if f in comps and t in comps:
            preds[t].append(f)
    circ = _safe_circuit(spec)  # backend 仅用于 layers() 拓扑分层
    layers = circ.layers()
    out = {}
    for layer in layers:
        for cid in layer:
            ins = [out[p] for p in preds[cid] if p in out]
            out[cid] = _quiet_run(comps[cid], ins)
    terminals = [c for c in comps if not circ.succ[c]]
    fq = max((out[c].quality for c in terminals), default=0.0)
    return {
        "final_quality": fq,
        "terminals": {c: (out[c].quality, out[c].ok) for c in terminals},
        "q": {c: out[c].quality for c in out},
        "ok": {c: out[c].ok for c in out},
    }


# ──────────────────────────────────────────────────────────
# 等价判定 + 拓扑变换（纯函数，深拷贝，可回滚）
# ──────────────────────────────────────────────────────────

def _preds_succs(spec):
    comps = spec["components"]
    preds = {c: [] for c in comps}
    succ = {c: [] for c in comps}
    for f, t in spec.get("wires", []):
        if f in comps and t in comps:
            preds[t].append(f)
            succ[f].append(t)
    return preds, succ


def _equivalent(a, b, tol=1e-6):
    """删前/删后两版是否等价：final_quality 一致 + 终端集合一致 + 终端 ok/质量一致。"""
    sa, sb = _simulate_quiet(a), _simulate_quiet(b)
    if abs(sa["final_quality"] - sb["final_quality"]) > tol:
        return False, "final_quality 变化"
    if set(sa["terminals"]) != set(sb["terminals"]):
        return False, "终端集合变化"
    for c, (qa, oka) in sa["terminals"].items():
        qb, okb = sb["terminals"][c]
        if oka != okb or abs(qa - qb) > tol:
            return False, f"终端 {c} 质量/ok 变化"
    return True, "等价"


def _remove_node(spec, nid, up):
    """纯函数：删节点 nid，其下游改接其上游 up（串联删除）。返回新 spec。"""
    new = copy.deepcopy(spec)
    comps = new["components"]
    succ = [w[1] for w in new["wires"] if w[0] == nid]
    new["wires"] = [w for w in new["wires"] if nid not in (w[0], w[1])]
    for d in succ:
        if [up, d] not in new["wires"]:
            new["wires"].append([up, d])
    del comps[nid]
    return new


def _merge_node(spec, bid, aid):
    """纯函数：删 bid，其下游改接 aid（并联合并：统一用 aid 的产出）。返回新 spec。"""
    new = copy.deepcopy(spec)
    comps = new["components"]
    succ = [w[1] for w in new["wires"] if w[0] == bid]
    new["wires"] = [w for w in new["wires"] if bid not in (w[0], w[1])]
    for d in succ:
        if [aid, d] not in new["wires"]:
            new["wires"].append([aid, d])
    del comps[bid]
    return new


# ──────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────

def simplify(spec, tol=1e-6, max_rounds=50):
    """奥卡姆剃刀化简：返回 (new_spec, report)。

    候选生成（不预判简单/复杂，靠等价判定说了算）：
      1. 并联合并候选：同 type + 同 capability + 同输入 的两节点 → 删其一、下游接另一
      2. 串联删除候选：单输入、非起点、非终端节点 → 删之、下游接其上游

    每轮应用第一个通过等价判定的候选；直到一轮无候选通过 → 幂等收敛。
    保留规则（自然由候选条件保证）：起点(preds=0) / 终端(succ=0) / 多输入汇合(preds>1)
    不产生候选；更有"删了结果会变差"的节点由等价判定自动保留（并行支路/多重验证/反馈环）。
    """
    report = {
        "simplified": False,
        "original_nodes": len(spec["components"]),
        "final_nodes": len(spec["components"]),
        "removed": [],
        "merged": [],
        "steps": [],
    }
    cur = copy.deepcopy(spec)
    for _ in range(max_rounds):
        preds, succ = _preds_succs(cur)
        comps = cur["components"]
        done = False

        # 1) 并联合并候选（仅确定性抽取节点：retrieve/extract/source）
        #    输入相同 → 产物内容确定相同，才是真冗余。推理分支(reason/summarize 等)
        #    虽同输入但真实执行有多样性、并行汇合有信息价值，不算重复，保留。
        cids = list(comps)
        for i in range(len(cids)):
            for j in range(i + 1, len(cids)):
                a, b = cids[i], cids[j]
                ca, cb = comps[a], comps[b]
                if ca.get("type") != cb.get("type"):
                    continue
                if ca.get("capability") != cb.get("capability"):
                    continue
                if not (ca.get("type") == "source"
                        or ca.get("capability") in ("retrieve", "extract")):
                    continue  # 非确定性抽取 → 不合并（保护并行推理支路）
                if set(preds[a]) != set(preds[b]):
                    continue
                if not preds[a]:            # 两者都是起点 → 跳过
                    continue
                if not succ[a] and not succ[b]:
                    continue                # 都没有下游 → 无合并意义
                cand = _merge_node(cur, b, a)
                ok, why = _equivalent(cur, cand, tol)
                if ok:
                    cur = cand
                    report["merged"].append({"removed": b, "into": a})
                    report["steps"].append({
                        "type": "merge", "node": b, "into": a,
                        "reason": f"并联同输入等价节点，合并到 {a}",
                    })
                    done = True
                    break
            if done:
                break
        if done:
            continue

        # 2) 串联删除候选（仅透传型结构节点：adc/capacitor/opamp/diode/format_adapter 等）
        #    不删 resistor：resistor 是变换/推理/抽取步骤，去噪模型下其精度上限封顶，
        #    删一个并行分支下游质量仍封顶不变 → 会误判等价而误删。奥卡姆剃刀对"不确定
        #    就保留"——推理/抽取支路一律保护；确定性抽取的"重复"由上方并联合并处理。
        for nid in list(comps):
            p = preds[nid]
            if len(p) != 1:
                continue                # 多输入（汇合/起点）→ 不删
            if not succ[nid]:
                continue                # 终端 → 不删（删了丢输出）
            _t = comps[nid].get("type")
            if _t in ("power", "source", "resistor"):
                continue                # 起点 / 变换节点 → 不删（保护推理与抽取支路）
            cand = _remove_node(cur, nid, p[0])
            ok, why = _equivalent(cur, cand, tol)
            if ok:
                cur = cand
                report["removed"].append(nid)
                report["steps"].append({
                    "type": "remove", "node": nid,
                    "reason": f"删掉等价不变（{why}）",
                })
                done = True
                break

        if not done:
            break

    report["simplified"] = bool(report["removed"] or report["merged"])
    report["final_nodes"] = len(cur["components"])
    return cur, report


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def simplify_selftest():
    # 1) 冗余中间 adc 被剃（删掉等价不变）
    spec_redundant = {
        "name": "redundant_adc",
        "components": {
            "src": {"type": "power", "label": "task"},
            "ret": {"type": "resistor", "label": "retrieve", "model": "small", "capability": "retrieve"},
            "mid": {"type": "adc", "threshold": 0.5},
            "org": {"type": "resistor", "label": "organize", "model": "small", "capability": "organize"},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "ret"], ["ret", "mid"], ["mid", "org"], ["org", "adc"]],
    }
    new, rep = simplify(spec_redundant)
    assert "mid" not in new["components"], "冗余中间 adc 应被剃落"
    assert rep["simplified"], "应标记 simplified"
    print("✓ ① 冗余中间 adc 被剃落（删掉等价不变）")

    # 2) 重复 retrieve 被合并（并联同输入等价节点）
    spec_dup = {
        "name": "dup_retrieve",
        "components": {
            "src": {"type": "power", "label": "task"},
            "r1": {"type": "resistor", "label": "retrieve", "model": "small", "capability": "retrieve"},
            "r2": {"type": "resistor", "label": "retrieve", "model": "small", "capability": "retrieve"},
            "sum": {"type": "resistor", "label": "summarize", "model": "large", "capability": "summarize"},
        },
        "wires": [["src", "r1"], ["src", "r2"], ["r1", "sum"], ["r2", "sum"]],
    }
    new, rep = simplify(spec_dup)
    kept = {"r1", "r2"} & set(new["components"])
    assert len(kept) == 1, "重复 retrieve 应合并为一个"
    print("✓ ② 重复 retrieve 被合并（并联同输入等价）")

    # 3) 复杂任务：并行支路 + 反馈环 完整保留（等价判定自然保留必要结构）
    spec_complex = {
        "name": "complex",
        "components": {
            "src": {"type": "power", "label": "task"},
            "a": {"type": "resistor", "label": "a", "model": "large", "capability": "reason"},
            "b": {"type": "resistor", "label": "b", "model": "large", "capability": "reason"},
            "c": {"type": "resistor", "label": "c", "model": "large", "capability": "reason"},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "a"], ["src", "b"], ["a", "c"], ["b", "c"], ["c", "adc"], ["adc", "src"]],
        "feedback": {"from": "adc", "to": "src", "max_iter": 3},
    }
    new, rep = simplify(spec_complex)
    assert set(new["components"].keys()) == set(spec_complex["components"].keys()), \
        "复杂任务结构应完整保留"
    assert "feedback" in new, "反馈环应保留"
    print("✓ ③ 复杂任务（并行支路+反馈环）完整保留，剃刀不伤必要结构")

    # 4) 幂等收敛：对已化简 spec 再次 simplify 无变化
    new2, rep2 = simplify(new)
    assert not rep2["simplified"], "二次化简应无变化（幂等）"
    print("✓ ④ 幂等收敛（二次化简无变化）")

    # 5) 等价判定正确：删掉会改变终端质量的节点应被拒绝（而非误判等价）
    #    说明：去噪模型下 resistor 精度封顶，删『并行分支』下游质量不变 → 判等价
    #    （这是 simplify 排除 resistor 候选的原因；保守保留推理/抽取支路）。
    #    这里测的是『链路删除导致终端质量真正变化』的场景，等价判定须拒绝。
    spec_chain = {
        "name": "chain",
        "components": {
            "src": {"type": "power", "label": "task"},
            "a": {"type": "resistor", "label": "a", "model": "small", "capability": "reason"},
            "b": {"type": "resistor", "label": "b", "model": "small", "capability": "reason"},
            "adc1": {"type": "adc", "threshold": 0.5},
            "adc2": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "a"], ["src", "b"], ["a", "adc1"], ["b", "adc2"]],
    }
    cand = _remove_node(spec_chain, "a", "src")  # 删 a → adc1 直收 src(1.0) 而非 a(0.92)
    ok, why = _equivalent(spec_chain, cand)
    assert not ok, "删 a 后终端质量应变化 → 判不等价（保留）"
    # 反向：simplify 对含并行支路的复杂任务不应删 resistor（已在 ③ 验证完整保留）
    print("✓ ⑤ 等价判定正确拒绝『删后终端质量变化』的删除")

    print("\nsimplify.py 离线自检全部通过 ✓")
    return True


if __name__ == "__main__":
    simplify_selftest()
