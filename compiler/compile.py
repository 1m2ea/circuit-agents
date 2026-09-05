"""
circuit-agents · compiler.compile
================================
M0→M1 流水线编排：Goal → Binder(选型) → Netlister(降低) → Circuit DSL 网表。

把"自动选型 + 降低"串成一步，供 demo / M2 Router / M3 Optimizer 复用。
"""
from __future__ import annotations

from .binder import Binder
from .goal import Goal
from .model_selector import ModelSelector
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


def _weave_lessons(spec: dict, les_texts: list) -> None:
    """② 第二圈：把教训文本织进 spec 与每个电阻组件。

    comp["memory_lessons"] 由后端 _lessons_block 转成提示词块——教训因此真正
    进入模型上下文，而非只躺在 spec 里机器可读。最多取前 3 条。
    """
    texts = [str(t).strip() for t in (les_texts or []) if str(t).strip()][:3]
    if not texts:
        return
    spec["memory_lessons"] = texts
    for c in (spec.get("components") or {}).values():
        if isinstance(c, dict) and c.get("type") == "resistor":
            c["memory_lessons"] = texts


def _ensure_hetero_verify(spec: dict) -> dict:
    """③ VERIFY_* 已配置时，在终端 adc 前自动插 verify#quality 电阻节点。

    质量门由此走独立后端（真异构）——runtime._backend_for 按 label 前缀
    `verify` 路由到 verify_backend。幂等（已有 verify 节点则跳过）；
    未配置 VERIFY_* / 无 adc / 异常 → 原样返回（零回归）。
    """
    try:
        from .backend_llm import resolve_verify_backend
        if resolve_verify_backend() is None:
            return spec
    except Exception:
        return spec
    try:
        comps = spec.get("components") or {}
        if any(isinstance(c, dict) and str(c.get("label", "")).split("#")[0] == "verify"
               for c in comps.values()):
            return spec  # 已有异构校验节点
        if "adc" not in comps:
            return spec
        prevs = [w[0] for w in spec.get("wires", [])
                 if len(w) == 2 and w[1] == "adc" and w[0] != "adc"]
        if not prevs:
            return spec
        kept = [w for w in spec.get("wires", [])
                if not (len(w) == 2 and w[1] == "adc" and w[0] != "adc")]
        comps["vq"] = {"type": "resistor", "label": "verify#quality",
                       "model": "tool", "recovery": 0.0,
                       "required_inputs": [],
                       "produced_outputs": ["verify_verdict"]}
        # 教训织入发生在本函数之前（compile_goal 先 weave 后插节点），
        # vq 是新节点须显式继承，否则校验节点反而看不到历史踩坑。
        if spec.get("memory_lessons"):
            comps["vq"]["memory_lessons"] = list(spec["memory_lessons"])
        for p in prevs:
            kept.append([p, "vq"])
        kept.append(["vq", "adc"])
        spec["wires"] = kept
        spec["hetero_verify"] = True   # 观察窗/复盘可见
    except Exception:
        pass
    return spec


def compile_goal(goal: Goal, auto_bind: bool = True, route: bool = False,
                 no_adapters: bool = False, memory_enabled: bool = True,
                 auto_select_models: bool = False) -> dict:
    """返回可直接被 runtime.py 加载的 spec dict；附带 binder_report。

    route=True 时走 M2 Router（依赖分层 + 并联布线 + 可选格式适配器），
    否则走 M0 Netlister（线性串联）。no_adapters=True 关闭第二层②格式适配器。
    memory_enabled=True 时（C 记忆与学习）：编译前查 TopologyMemory，
    命中成功且高质量的历史拓扑 → 直接复用（标注 memory_hit），跳过重新编译。
    auto_select_models=True 时（③ 智能模型选型）：编译后用 ModelSelector
    按复杂度/历史/约束为每个电阻推荐 (tier, skills)，覆盖 Binder 的静态映射。
    """
    # C 记忆与学习：编译前查记忆，命中则复用
    if memory_enabled:
        try:
            from .topology_memory import TopologyMemory
            mem = TopologyMemory()
            hit = mem.recall(goal.description)
            if hit is not None:
                spec = dict(hit["spec"])
                # 让本次复用运行以「当前 goal」落库，便于后续相似任务召回
                spec["goal_desc"] = goal.description
                spec["memory_hit"] = {
                    "score": hit["score"],
                    "original_goal": hit["original_goal"],
                    "quality": hit["quality"],
                }
                # ② 第二圈：教训随拓扑带出并织进电阻组件（提示词将携带）
                _weave_lessons(spec, [l.get("text", "") for l in (hit.get("lessons") or [])
                                      if isinstance(l, dict)])
                # ③ VERIFY_* 已配置 → 自动插真异构校验节点（幂等）
                _ensure_hetero_verify(spec)
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
    # ② 第二圈：新编译同样召回教训、织进电阻组件（提示词将携带）
    if memory_enabled:
        try:
            from .topology_memory import TopologyMemory
            _les = [l.get("text", "")
                    for l in TopologyMemory().recall_lessons(goal.description)]
        except Exception:
            _les = []
        _weave_lessons(spec, _les)
    # ③ VERIFY_* 已配置 → 自动插真异构校验节点（幂等、未配置零回归）
    _ensure_hetero_verify(spec)
    # D 人机协同：目标含"人工/人审/需确认/需审核"→ spec 标 human_intervention=True
    import re as _re
    if _re.search(r"(人工|人审|需确认|需审核|人工介入|human.{0,4}review)", goal.description or ""):
        spec["human_intervention"] = True
    # ③ 智能模型选型：编译后按复杂度/历史/约束微调每个电阻的 model/skills
    if auto_select_models:
        try:
            mem = None
            if memory_enabled:
                from .topology_memory import TopologyMemory
                mem = TopologyMemory()
            ms = ModelSelector(memory=mem)
            ms.apply_to_spec(spec)
        except Exception:
            pass  # 选型失败 → 沿用 Binder 结果（零回归）
    spec["goal_desc"] = goal.description
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
