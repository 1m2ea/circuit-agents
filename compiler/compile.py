"""
circuit-agents · compiler.compile
================================
M0→M1 流水线编排：Goal → Binder(选型) → Netlister(降低) → Circuit DSL 网表。

把"自动选型 + 降低"串成一步，供 demo / M2 Router / M3 Optimizer 复用。
"""
from __future__ import annotations

from .binder import Binder
from .goal import Goal
from .netlister import Netlister
from .optimizer import Optimizer
from .router import Router


# ---- ① 规划器自动产出显式进化提示（D 增强接规划器）----
# 启发式：集合类输出(枚举/列表/候选/选项)的「检索/研究」类子任务，若存在下游
# 「分析/推理」类子任务消费该集合字段 → 自动发 evolve_requests，让 3.5 进化
# 在「检索一堆→深挖 top-k」场景主动触发（即便检索条数未超阈值）。
_RESEARCH_CAPS = {"retrieve", "search", "research", "enumerate", "list",
                  "gather", "collect", "scan", "fetch"}
_ANALYZE_CAPS = {"reason", "analyze", "compare", "synthesize", "evaluate",
                 "summarize", "review", "predict", "decompose"}
_COLLECT_TOKENS = {"list", "options", "candidates", "frameworks", "items",
                   "results", "papers", "sources", "alternatives", "choices",
                   "top", "set", "findings", "catalog"}


def _cap_of(comp):
    return (comp.get("label") or "").split("#")[0].strip().lower()


def _infer_evolve_requests(spec):
    """扫描 spec 的组件/连线，返回 evolve_requests 列表 [{"key":字段名,"top_k":N}, ...]。
    零回归：无匹配返回 []（maybe_evolve 退旧自动行为）。
    """
    comps = spec.get("components", {})
    out, seen = [], set()
    for a, b in spec.get("wires", []):
        ca, cb = comps.get(a), comps.get(b)
        if not ca or not cb:
            continue
        a_cap, b_cap = _cap_of(ca), _cap_of(cb)
        a_research = (a_cap in _RESEARCH_CAPS) or any(
            tok in o.lower() for o in (ca.get("produced_outputs") or []) for tok in _COLLECT_TOKENS)
        b_analyze = (b_cap in _ANALYZE_CAPS) or any(
            tok in o.lower() for o in (cb.get("produced_outputs") or []) for tok in _COLLECT_TOKENS)
        if not (a_research and b_analyze):
            continue
        a_fields = set(ca.get("produced_outputs") or []) | set(ca.get("required_inputs") or [])
        flow = [f for f in (cb.get("required_inputs") or []) if f in a_fields]
        for f in flow:
            if f in seen:
                continue
            seen.add(f)
            out.append({"key": f, "top_k": 3})
    return out


def compile_goal(goal: Goal, auto_bind: bool = True, route: bool = False,
                 no_adapters: bool = False, memory_enabled: bool = True) -> dict:
    """返回可直接被 runtime.py 加载的 spec dict；附带 binder_report。

    route=True 时走 M2 Router（依赖分层 + 并联布线 + 可选格式适配器），
    否则走 M0 Netlister（线性串联）。no_adapters=True 关闭第二层②格式适配器。
    memory_enabled=True 时（C 记忆与学习）：编译前查 TopologyMemory，
    命中成功且高质量的历史拓扑 → 直接复用（标注 memory_hit），跳过重新编译。
    """
    # C 记忆与学习：编译前查记忆，命中则复用
    if memory_enabled:
        try:
            from .topology_memory import TopologyMemory
            mem = TopologyMemory()
            hit = mem.recall(goal.description)
            if hit is not None:
                spec = dict(hit["spec"])
                spec["memory_hit"] = {
                    "score": hit["score"],
                    "original_goal": hit["original_goal"],
                    "quality": hit["quality"],
                }
                spec["binder_report"] = None
                # 仍重新推断 evolve_requests（记忆里的可能过时）
                spec["evolve_requests"] = _infer_evolve_requests(spec)
                return spec
        except Exception:
            pass  # 记忆查询失败 → 正常编译（零回归）

    report = None
    if auto_bind:
        binder = Binder()
        tiers = binder.bind(goal)
        goal.tiers = tiers
        report = binder.report(goal, tiers)
    if route:
        spec = Router(default_tier="small").route(goal, no_adapters=no_adapters)
    else:
        spec = Netlister().compile(goal)
    spec["binder_report"] = report
    spec["evolve_requests"] = _infer_evolve_requests(spec)  # ① 规划器自动产出
    # D 人机协同：目标含"人工/人审/需确认/需审核"→ spec 标 human_intervention=True
    import re as _re
    if _re.search(r"(人工|人审|需确认|需审核|人工介入|human.{0,4}review)", goal.description or ""):
        spec["human_intervention"] = True
    return spec


def optimize_goal(goal_dict: dict, runs: int = 200, seed: int = 7) -> dict:
    """M3 总入口：对结构化目标跑 贪心 + 搜索，返回优化后的 spec 与 Pareto 前沿。"""
    return Optimizer(runs=runs, seed=seed).optimize(goal_dict)


def _planner_evolve_selftest():
    """① 离线自检：规划器推断 evolve_requests + CircuitExecutor 种进 state。"""
    import random
    import runtime as rt
    from runtime import Circuit, CircuitExecutor
    spec = {
        "name": "research_plan",
        "components": {
            "src": {"type": "power", "label": "task", "produced_outputs": ["task_in"]},
            "research": {"type": "resistor", "label": "retrieve", "model": "small",
                         "required_inputs": ["frameworks"], "produced_outputs": ["report"]},
            "analyze": {"type": "resistor", "label": "reason", "model": "small",
                        "required_inputs": ["frameworks"], "produced_outputs": ["analysis"]},
        },
        "wires": [["src", "research"], ["research", "analyze"]],
    }
    er = _infer_evolve_requests(spec)
    assert er == [{"key": "frameworks", "top_k": 3}], f"应推断 evolve_requests, got {er}"
    spec["evolve_requests"] = er   # 真实流程中由 compile_goal 赋值
    print("✓ ① 规划器推断: retrieve→reason + 集合字段 frameworks → evolve_requests=[{key:frameworks,top_k:3}]")
    # 种子校验：CircuitExecutor 把 spec.evolve_requests 种进 state
    ex = CircuitExecutor(Circuit(spec, rt.SimBackend(random.Random(0))))
    assert ex.state.get("_evolve_requests") == [{"key": "frameworks", "top_k": 3}], \
        "CircuitExecutor 应把 spec.evolve_requests 种进 state"
    print("✓ ① 种子: CircuitExecutor 把 spec.evolve_requests 种进 state._evolve_requests")


if __name__ == "__main__":
    _planner_evolve_selftest()
