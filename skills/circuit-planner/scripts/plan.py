#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
circuit-planner · plan.py — NL → Goal → 编译 → 可读执行计划
============================================================

把自然语言目标交给 circuit-agents 编译器，产出电路拓扑 + 一份
agent（WorkBuddy）可按拓扑序执行的计划。**不执行任何步骤，只规划。**

用法:
  python plan.py "总结一篇PDF并核对里面的数字，要求高可靠"
  echo "翻译这段英文" | python plan.py
  CIRCUIT_COMPILER_DIR=/path/to/circuit-agents python plan.py "..."

依赖: 纯 stdlib。编译器路径默认 C:\\Users\\lgw12\\WorkBuddy\\666\\circuit-agents，
可用环境变量 CIRCUIT_COMPILER_DIR 覆盖（指向含 compiler/ 包的目录）。
"""
from __future__ import annotations

import os
import sys
import json
import re
import random
from collections import Counter

# 真工作区已迁至 D 盘；C 盘为历史遗留副本（A 线迁移后废弃，仅作兜底，勿在其上改动）。
# 仍可用 CIRCUIT_COMPILER_DIR 环境变量显式覆盖。
_COMPILER_DIR_CANDIDATES = [
    r"D:\dev\projects\666\circuit-harness\engine",
    r"D:\dev\projects\666\circuit-agents",
    r"C:\Users\lgw12\WorkBuddy\666\circuit-agents",
]
DEFAULT_COMPILER_DIR = next(
    (d for d in _COMPILER_DIR_CANDIDATES if os.path.isdir(d)),
    _COMPILER_DIR_CANDIDATES[-1],
)
COMPILER_DIR = os.environ.get("CIRCUIT_COMPILER_DIR", DEFAULT_COMPILER_DIR)
if os.path.isdir(COMPILER_DIR) and COMPILER_DIR not in sys.path:
    sys.path.insert(0, COMPILER_DIR)

try:
    from compiler.nl_parser import GoalParser
    from compiler.compile import compile_goal, optimize_goal
    from compiler.goal import Goal
    from compiler.binder import Binder
    from compiler.router_auto import infer_dependencies
    from compiler.templates import match_template, build_goal_from_template
except Exception as e:  # pragma: no cover
    sys.stderr.write(
        f"[circuit-planner] 无法加载编译器（CIRCUIT_COMPILER_DIR={COMPILER_DIR}）: {e}\n"
        f"请设置 CIRCUIT_COMPILER_DIR 指向含 compiler/ 包的目录。\n"
    )
    sys.exit(2)

# runtime 提供 SimBackend 仿真执行 + Watchdog 健康自检（CIRCUIT_COMPILER_DIR 已入 sys.path）
try:
    import runtime as rt
except Exception:  # pragma: no cover
    rt = None


# capability → 建议使用的 WorkBuddy 工具（与 SKILL.md 一致；此处仅作计划内提示）
SUGGESTED_TOOL = {
    "retrieve": "Read / WebFetch（读取本地文件或抓取网页）",
    "reason": "直接推理（LLM 思考 / 笔记）",
    "calculate": "Bash（python 计算 / 脚本）",
    "verify": "Read + 比对 / 或脚本断言",
    "translate": "直接推理（LLM 翻译）",
    "extract": "Read + 结构化抽取（脚本/LLM）",
    "classify": "直接推理（LLM 分类）",
    "organize": "直接推理 + 结构化输出（表格/列表/报告）；可 `Write` 落盘",
    "summarize": "直接推理（LLM 摘要/综述），或 `Write` 输出",
}


def _tool_for(cap: str) -> str:
    """能力名可能带多重集/冗余后缀（如 retrieve#2、retrieve#1），归一回基础名查工具提示。"""
    base = re.sub(r"#\d+$", "", cap)
    return SUGGESTED_TOOL.get(base, "按能力选择工具")


# ---- 第二层⑥：自适应型号档（不开 --optimize 时的默认选型）----
_QUALITY_SENSITIVE = {"reason", "verify", "extract", "summarize"}
_TIER_RANK = {"small": 0, "large": 1, "tool": 2}
# 步骤数超过该阈值 → 引入"共享上下文总线"表示（第二层⑤）
BUS_THRESHOLD = 10

# ---- 第四步：预估耗时模型（heuristic，单位 ms，仅规划层估算）----
# 按"能力基础名 × 型号档"给每个步骤一个粗略延迟；large 比 small 重（更强模型/更长推理），
# tool 档按 large 估算（常含外部工具调用）。并联层取 max，串联层累加（同 runtime 分层语义）。
_LATENCY_MODEL = {
    ("retrieve", "small"): 900, ("retrieve", "large"): 1400,
    ("reason", "small"): 600, ("reason", "large"): 1600,
    ("calculate", "small"): 300, ("calculate", "large"): 700,
    ("verify", "small"): 400, ("verify", "large"): 900,
    ("extract", "small"): 500, ("extract", "large"): 1100,
    ("summarize", "small"): 500, ("summarize", "large"): 1300,
    ("translate", "small"): 400, ("translate", "large"): 900,
    ("classify", "small"): 400, ("classify", "large"): 900,
    ("organize", "small"): 500, ("organize", "large"): 1100,
}
_LAT_FALLBACK = {"small": 500, "large": 1200, "tool": 1200}


def _est_latency(cap: str, tier: str) -> int:
    base = re.sub(r"#\d+$", "", cap)
    return _LATENCY_MODEL.get((base, tier), _LAT_FALLBACK.get(tier, 800))


def _build_recap(nl, goal, mode):
    """B. 规划前确认环：把规划器对目标的理解回述成可读摘要（纯函数，便于离线自检）。

    覆盖：能力步骤 / 依赖布线(并联·串联·DAG) / 约束 / 反馈环 / 子任务 IO。
    在编译前打印，用户确认后再继续——避免「编译错方向」。
    """
    L = []
    L.append("我理解你的目标是：")
    L.append(f"  「{nl}」")
    L.append("")
    caps = goal.capabilities or []
    L.append(f"解析出的能力步骤（{len(caps)}）: {caps}")
    deps = goal.dependencies
    if deps is None:
        pat = "未指定 → 将按默认串行/并联推断"
    elif deps == []:
        pat = "并联（各步相互独立，可同时跑）"
    elif isinstance(deps, list):
        pat = f"DAG（{len(deps)} 条依赖边）"
    else:
        pat = "串联"
    L.append(f"依赖/布线: {pat}")
    c = goal.constraints or {}
    cons = []
    if goal.reliability:
        cons.append(f"reliability={goal.reliability}")
    if "min_quality" in c:
        cons.append(f"min_quality={c['min_quality']}")
    if "max_cost" in c:
        cons.append(f"max_cost={c['max_cost']}")
    if "max_latency_ms" in c:
        cons.append(f"max_latency_ms={c['max_latency_ms']}")
    if cons:
        L.append("约束: " + ", ".join(cons))
    if getattr(goal, "subtasks", None) is not None:
        L.append(f"子任务 IO: {len(goal.subtasks)} 个（含 inputs/outputs，将走数据依赖分析）")
    if goal.feedback:
        fb = goal.feedback
        L.append(f"反馈环: from={fb.get('from')} → to={fb.get('to')} max_iter={fb.get('max_iter')}")
    return "\n".join(L)


def auto_tiers(goal: "Goal") -> dict:
    """不开 --optimize 时的自适应默认档（第二层⑥）。

    与 Binder(M1) 输出同构（capability->tier dict），可直接喂
    compile_goal(goal, auto_bind=False, route=True)。规则：
      1) 精度硬下限：借用 Binder 的"达标最低成本"基线，保证 min_quality 不被破坏；
      2) 高可靠(reliability=high) 或 高质诉求(min_quality>=0.85) → 质量敏感步
         (reason/verify/extract/summarize) 升 large；但成本/延迟硬受限时保 small；
      3) 其余步维持该能力精度下限对应的档（默认 small）。
    """
    c = goal.constraints
    q_min = c.get("min_quality", 0.0)
    high_q = goal.reliability == "high" or q_min >= 0.85
    cost_limited = "max_cost" in c
    lat_limited = "max_latency_ms" in c
    # 1) 精度下限基线（cheapest tier meeting q_min）
    floor = Binder().bind(goal)
    tiers: dict = {}
    for cap in goal.capabilities:
        base = re.sub(r"#\d+$", "", cap)
        want = ("large" if (high_q and base in _QUALITY_SENSITIVE
                            and not (cost_limited or lat_limited)) else "small")
        f = floor.get(cap, "small")
        tiers[cap] = f if _TIER_RANK[f] >= _TIER_RANK[want] else want
    return tiers


def _load_key(key_file=None):
    """取 API key：优先环境变量，否则读 key 文件（默认桌面 key_tmp.txt）。无则返回空串（规则兜底）。"""
    for v in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AGENT_API_KEY"):
        if os.environ.get(v):
            return os.environ[v].strip()
    p = key_file or os.path.join(os.path.expanduser("~"), "Desktop", "key_tmp.txt")
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass
    return ""


def _topo_order(spec):
    """对 spec 组件按 wires 做拓扑排序，返回有序节点名列表。"""
    comps = spec.get("components", {})
    wires = spec.get("wires", [])
    indeg = {n: 0 for n in comps}
    adj = {n: [] for n in comps}
    for a, b in wires:
        if a in adj and b in indeg:
            adj[a].append(b)
            indeg[b] += 1
    ready = [n for n, d in indeg.items() if d == 0]
    order = []
    while ready:
        ready.sort()
        n = ready.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    for n in comps:  # 防线圈兜底
        if n not in order:
            order.append(n)
    return order


def _plan(spec):
    comps = spec.get("components", {})
    order = _topo_order(spec)
    steps = []
    for name in order:
        c = comps.get(name, {})
        t = c.get("type")
        if t == "resistor":
            cap = c.get("label", name)
            steps.append({
                "node": name,
                "kind": "agent-step",
                "capability": cap,
                "tier": c.get("model", "?"),
                "tool": _tool_for(cap),
            })
        elif t == "capacitor":
            steps.append({"node": name, "kind": "buffer",
                          "label": c.get("label", name),
                          "mode": c.get("mode", "all")})
        elif t == "adc":
            steps.append({"node": name, "kind": "validate",
                          "label": c.get("label", name),
                          "threshold": c.get("threshold")})
        elif t == "format_adapter":
            steps.append({"node": name, "kind": "adapter",
                          "label": c.get("label", name),
                          "from_fmt": c.get("from_fmt"),
                          "to_fmt": c.get("to_fmt"),
                          "kind_adp": c.get("kind")})
        else:
            steps.append({"node": name, "kind": t or "other",
                          "label": c.get("label", name)})
    return steps


def _indent_block(text, prefix="  "):
    return prefix + text.replace("\n", "\n" + prefix)


def _layers(spec):
    """按最长路径给组件分层；同层节点可并行。返回 [[node,...],...]。"""
    comps = spec.get("components", {})
    wires = spec.get("wires", [])
    adj = {n: [] for n in comps}
    for a, b in wires:
        if a in adj and b in comps:
            adj[a].append(b)
    layer = {}
    for n in _topo_order(spec):
        if n not in layer:
            layer[n] = 0
        for m in adj[n]:
            layer[m] = max(layer.get(m, 0), layer[n] + 1)
    if not layer:
        return []
    maxl = max(layer.values())
    layers = [[] for _ in range(maxl + 1)]
    for n, l in layer.items():
        layers[l].append(n)
    return layers


def _runbook(spec, goal_name=""):
    """生成 agent 运行时执行用的 runbook：有序步骤 + 上下文依赖 + 并行/重试策略。

    注意：本函数只「规划」执行顺序，真实执行由 WorkBuddy 运行时（agent 用 Read/WebFetch/
    Write/Bash 等工具）完成——脚本不自带工具调用能力。
    """
    comps = spec.get("components", {})
    wires = spec.get("wires", [])
    order = _topo_order(spec)
    up = {n: [] for n in comps}
    for a, b in wires:
        if a in up and b in up:
            up[b].append(a)

    layers = _layers(spec)
    node_layer = {}
    for li, layer in enumerate(layers):
        for n in layer:
            node_layer[n] = li

    steps = []
    idx = 0
    for n in order:
        c = comps.get(n, {})
        if c.get("type") == "resistor":
            idx += 1
            cap = c.get("label", n)
            steps.append({
                "step": idx, "node": n, "capability": cap,
                "tier": c.get("model", "?"),
                "tool": _tool_for(cap),
                "produces": f"「{cap}」结果",
                "layer": node_layer.get(n, 0),
            })
    node_to_step = {s["node"]: s["step"] for s in steps}

    # 解析上游「生产者」：穿过电容/opamp 等缓冲，落到真正产出数据的 resistor/source
    def producer_ancestors(n, seen=None):
        if seen is None:
            seen = set()
        res = []
        for u in up.get(n, []):
            if u in seen:
                continue
            seen.add(u)
            t = comps.get(u, {}).get("type")
            if t == "resistor":
                res.append((node_to_step.get(u), u))
            elif t == "source":
                res.append((None, u))
            else:  # capacitor / opamp / adc 等只是管道，继续向上找
                res.extend(producer_ancestors(u, seen))
        return res

    for s in steps:
        anc = producer_ancestors(s["node"])
        ctx = []
        for stp, u in anc:
            if stp is not None:
                ctx.append(f"步骤{stp} [{comps[u].get('label', u)}] 产出")
            else:
                ctx.append("原始任务输入(源)")
        s["input_context"] = ctx

    # 并行步骤（同层内的其它 resistor 步骤）
    by_layer = {}
    for s in steps:
        by_layer.setdefault(s["layer"], []).append(s["step"])
    for s in steps:
        s["parallel_with"] = [x for x in by_layer.get(s["layer"], []) if x != s["step"]]

    # 锁相环里程碑（第二层④ · 降门槛改进）：默认每个节点后都插轻量里程碑，
    # 检查当前产出是否偏离原始目标（关键词/存在性，无需模型）；并行前沿(同层≥2步)
    # 仍保留整层里程碑（更省校验点）。纠偏触发改为"连续两个里程碑偏差超阈值"才 retry_upstream，
    # 避免单点抖动误纠偏——让锁相环从"只有复杂拓扑才生效"变成"所有任务默认保护"。
    milestones = []
    for li, layer_steps in by_layer.items():
        if len(layer_steps) >= 2:
            milestones.append({
                "id": f"ms_l{li}",
                "after_steps": layer_steps,
                "scope": "layer",
                "check": ("轻量校验(关键词/存在性，无需模型)：本层各步产出非空，且覆盖了目标里的"
                          "关键实体/数据项（如'销量''政策'等并列对象）；任一为空或缺失关键项 "
                          "→ 判 fail。"),
                "trigger_policy": "consecutive_fail>=2",
                "on_fail": "retry_upstream",
            })
        else:
            # 单层单步：给该步插独立里程碑（降门槛核心：2 步线性链等简单任务也受保护）
            s = layer_steps[0]
            milestones.append({
                "id": f"ms_s{s}",
                "after_steps": [s],
                "scope": "node",
                "check": ("轻量校验(关键词/存在性，无需模型)：该步产出非空且未偏离原始目标关键意图；"
                          "偏离 → 判 fail。"),
                "trigger_policy": "consecutive_fail>=2",
                "on_fail": "retry_upstream",
            })

    # 共享上下文总线（第二层⑤ · 按形态触发改进）：除旧的大规模阈值(步数>BUS_THRESHOLD)兜底外，
    # 新增"拓扑形态"触发——任一层含 ≥3 个无依赖并行步骤(节点)即启用总线模式表示。
    # 这比死板的步数阈值更贴真实结构（如 C 任务：4 个独立检索挂在同一总线上）。
    # 语义不变：同层步并行写总线、跨层上下文顺次累积，下游读总线最新快照。纯规划/表示层增强。
    bus = None
    phases = None
    parallel_bus_eligible = any(len(v) >= 3 for v in by_layer.values())
    bus_trigger = None
    if len(steps) > BUS_THRESHOLD:
        bus_trigger = f"scale(步骤数 {len(steps)}>阈值{BUS_THRESHOLD})"
    elif parallel_bus_eligible:
        bus_trigger = "topology(并行层≥3节点)"
    if bus_trigger:
        # 按 step 所在 layer 分组（同层并行），按 layer 升序给连续阶段号(0-based)
        ordered_layers = sorted(by_layer.keys())
        stage_of_layer = {li: si for si, li in enumerate(ordered_layers)}
        phases = []
        for li in ordered_layers:
            ls = sorted(by_layer[li])
            si = stage_of_layer[li]
            phases.append({
                "stage": si,
                "spec_layer": li,                       # 回溯到真实 spec 层号
                "steps": ls,
                "bus_writes": ls,                       # 本阶段各步并行写总线
                "bus_reads_from": [si - 1] if si > 0 else [],  # 跨层读上一阶段快照
            })
        bus = {
            "type": "shared_context_bus",
            "trigger": bus_trigger,
            "stages": len(phases),
            "convention": ("同层步并行写入总线最新快照；跨层上下文顺次累积；"
                           "下游阶段读取总线最新快照作为输入，等价逐层串接的压缩表示。"),
            "note": "纯规划层抽象，不改变 runtime 执行语义（同层max/跨层sum 不变）。",
        }

    # 预估耗时（第四步）：每个步骤按"能力×型号档"估延迟；并联层(同层)取 max，
    # 串联层(跨层)累加——与 runtime 分层语义一致（同层 max / 跨层 sum）。
    for s in steps:
        s["estimated_latency_ms"] = _est_latency(s["capability"], s["tier"])
    step_est = {s["step"]: s["estimated_latency_ms"] for s in steps}
    layer_max = {li: max(step_est[st] for st in stps) for li, stps in by_layer.items()}
    estimated_total_ms = sum(layer_max.values())
    longest_parallel_ms = max(layer_max.values(), default=0)
    rest_serial_ms = estimated_total_ms - longest_parallel_ms
    estimated_breakdown = {
        "total_ms": estimated_total_ms,
        "longest_parallel_layer_ms": longest_parallel_ms,
        "rest_serial_ms": rest_serial_ms,
        "per_layer_max_ms": layer_max,
    }

    fb = spec.get("feedback")
    adc_nodes = [n for n, c in comps.items() if c.get("type") == "adc"]
    return {
        "name": spec.get("name", goal_name),
        "steps": steps,
        "feedback": fb,
        "quality_gates": adc_nodes,
        "milestones": milestones,
        "phases": phases,
        "bus": bus,
        "estimated_total_ms": estimated_total_ms,
        "estimated_breakdown": estimated_breakdown,
    }


def _extract_node_values(results):
    """把 CircuitExecutor._results（cid -> Signal）抽成可读的产出字典。

    修复前这些正文只活在内存里：[10] 打印完分数后进程退出，内容即永久丢失
    （花真钱、零产出）。这里同时给出两种视图：
      by_node   : 节点 id -> 产出值
      by_output : 产出字段名（如 interface_design）-> 产出值（更可读，优先用它）
    """
    by_cid, by_output = {}, {}
    for cid, sig in (results or {}).items():
        val = getattr(sig, "value", sig)
        if not isinstance(val, (str, int, float, bool, type(None), list, dict)):
            val = str(val)
        by_cid[cid] = val
        for name in ((getattr(sig, "meta", {}) or {}).get("produced_outputs") or []):
            by_output.setdefault(name, val)
    return {"by_node": by_cid, "by_output": by_output}


def _run_real(spec, api_key, base_url):
    """补强#3：用真实 LLM 后端跑一遍拓扑，产出成本/延迟/质量实测。

    无 API key 时自动切 dry_run（仅组装请求不发起真调用），保证离线安全、可演示；
    有 key 时真·在线调用（base_url 指向 OpenAI-compatible endpoint，检测到 deepseek 自动套模型）。
    """
    import runtime as rt
    from compiler.backend_llm import RealLLMBackend
    if not api_key:
        backend = RealLLMBackend(rng=random.Random(0), dry_run=True)
        mode = "dry_run（未检测到 API key，仅组装请求不发起真调用）"
    else:
        # 未显式给 base_url 时：若设了 DEEPSEEK_API_KEY 则默认走 DeepSeek 兼容端点
        # （RealLLMBackend 会据 base_url 自动套 deepseek 模型映射），否则默认 OpenAI。
        if not base_url and os.environ.get("DEEPSEEK_API_KEY"):
            base_url = "https://api.deepseek.com/v1"
        backend = RealLLMBackend(api_key=api_key, base_url=base_url, rng=random.Random(0))
        mode = f"真实在线（base_url={base_url or '默认 OpenAI'}）"
    from compiler.backend_llm import resolve_verify_backend
    circuit = rt.Circuit(spec, backend, verify_backend=resolve_verify_backend())
    # ① 走 CircuitExecutor（闭环执行引擎）：激活 A 自动补数 + C 异构校验 + D 进化，
    #    并消费 spec.evolve_requests（规划器自动产出的显式进化提示）。
    ex = rt.CircuitExecutor(circuit, verify_backend=circuit.verify_backend)
    res = ex.run()
    # A 修复：把各节点真实产出正文带出（ex._results = {cid: Signal}），
    # 否则正文随进程退出丢失（花真钱零产出）。
    res["node_values"] = _extract_node_values(getattr(ex, "_results", None) or {})
    return res, mode


def main(argv):
    # ---- 解析开关（与自由文本 NL 分离）----
    opts = {"optimize": False, "runs": 200, "key_file": None, "draw": True, "execute": True,
            "no_template": False, "no_auto_tiers": False, "no_auto_route": False,
            "self_heal": False, "backend": "sim", "no_adapters": False, "watchdog": True,
            "confirm": False, "self_test": False, "simplify": True}
    nl_parts = []
    for a in argv[1:]:
        if a == "--optimize":
            opts["optimize"] = True
        elif a.startswith("--runs="):
            try:
                opts["runs"] = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a.startswith("--key-file="):
            opts["key_file"] = a.split("=", 1)[1]
        elif a == "--no-template":
            opts["no_template"] = True
        elif a == "--no-auto-tiers":
            opts["no_auto_tiers"] = True
        elif a == "--no-auto-route":
            opts["no_auto_route"] = True
        elif a == "--self-heal":
            opts["self_heal"] = True
        elif a.startswith("--backend="):
            opts["backend"] = a.split("=", 1)[1]
        elif a == "--no-adapters":
            opts["no_adapters"] = True
        elif a == "--no-simplify":
            opts["simplify"] = False
        elif a == "--no-watchdog":
            opts["watchdog"] = False
        elif a == "--confirm":
            opts["confirm"] = True
        elif a == "--self-test":
            opts["self_test"] = True
        elif a == "--draw":
            opts["draw"] = True
        elif a == "--no-draw":
            opts["draw"] = False
        elif a == "--execute":
            opts["execute"] = True
        elif a == "--no-execute":
            opts["execute"] = False
        else:
            nl_parts.append(a)
    nl = " ".join(nl_parts) if nl_parts else sys.stdin.read().strip()
    if (not nl) and not opts.get("self_test"):
        sys.stderr.write(
            "[circuit-planner] 未提供目标。用法: python plan.py \"<自然语言目标>\" "
            "[--optimize] [--runs=N] [--key-file=PATH] [--no-template] [--no-auto-tiers] "
            "[--no-auto-route] [--self-heal] [--backend=real] [--no-adapters] "
            "[--simplify|--no-simplify] [--draw|--no-draw] [--execute|--no-execute]\n"
            "  （--draw / --execute 现已【默认开启】：每次规划自动出拓扑图 + 执行 runbook；"
            "用 --no-draw / --no-execute 可关闭对应产物；--no-template 强制从头编译、不套用已知模板；"
            "--no-auto-tiers 回退到 Binder 基线选型，不做高可靠/质量敏感步的 adaptive 升档；"
            "--no-auto-route 回退到旧默认串行（不做语义 DAG 推断）；"
            "--self-heal 开启运行时拓扑热更新（执行中失败电阻档位自动升级，需反馈环预算支撑）；"
            "--backend=real 接真实 LLM 后端在线实测拓扑（需 API key；无 key 时自动 dry_run 仅组装请求）；"
            "--no-adapters 关闭第二层②格式校验适配器（默认自动插入 ADC/DAC 桥接格式断点）；"
            "--no-simplify 关闭奥卡姆剃刀化简 Pass，保留原始编译拓扑（默认开启：等价不变即剃落冗余））\n")
        return 1

    # ---- B. 规划前确认环离线自检（无需 TTY / 网络）----
    if opts.get("self_test"):
        g = Goal(name="self_test", capabilities=["retrieve", "reason"],
                 dependencies=[["retrieve", "reason"]],
                 constraints={"min_quality": 0.8}, reliability="high")
        txt = _build_recap("测试目标", g, "规则兜底")
        assert "retrieve" in txt and "reason" in txt, "recap 应含能力步骤"
        assert "DAG" in txt, "依赖边应显示为 DAG"
        assert "min_quality" in txt and "high" in txt, "recap 应含约束"
        print("✓ B 规划前确认环: _build_recap 正确回述能力/依赖/约束（离线）")
        return 0

    # ---- 解析（有 key 走 LLM 增强，否则规则兜底）----
    api_key = _load_key(opts["key_file"])
    parser = GoalParser(api_key=api_key)
    mode = "LLM 增强" if api_key else "规则兜底（离线，无需 key）"
    goal = parser.parse(nl)

    # ---- B. 规划前确认环：回述理解，用户确认后再编译（--confirm + 交互终端）----
    # 默认（无 --confirm，或非交互终端/CI/常驻自动模式）不阻断，保持原自动规划行为。
    if opts.get("confirm") and sys.stdin.isatty():
        print("\n" + _build_recap(nl, goal, mode))
        ans = input("确认以上理解无误、继续编译?(y/N) ").strip().lower()
        if ans not in ("y", "yes", "是", "确认", "ok"):
            print("[circuit-planner] 已取消，未编译。")
            return 0

    # ---- 模板复用（M4 增强 · 第二层③）：能力签名超集匹配已知良好拓扑 ----
    used_template = None
    if not opts["no_template"]:
        tpl = match_template(goal)
        if tpl:
            goal = Goal.from_dict(build_goal_from_template(tpl, goal))
            used_template = tpl["name"]

    # 第二层⑧：运行时拓扑热更新开关（--self-heal）；先落到 goal，编译时写入 spec。
    if opts["self_heal"]:
        goal.self_heal = True

    auto_routed = False
    if opts["optimize"]:
        opt = optimize_goal(goal.to_dict(), runs=opts["runs"], seed=7)
        spec = opt["spec"]
    else:
        # 第二层⑥：不开 --optimize 时，用自适应默认档（auto_tiers）做选型，
        # 再关掉 compile_goal 内部的 Binder 自动绑定（Binder 仍被 auto_tiers 当作
        # 精度下限基线复用，代码不变）。--no-auto-tiers 则回退到 Binder 基线。
        if not opts["no_auto_tiers"]:
            goal.tiers = auto_tiers(goal)
        # 第二层⑦：默认串行(None)时套用语义 DAG 推断（不动已测的并行意图路径/模板显式 DAG）。
        # --no-auto-route 回退旧默认串行。
        dataflow_routed = False
        if not opts["no_auto_route"] and goal.dependencies is None:
            inferred = infer_dependencies(goal)
            if inferred:
                goal.dependencies = inferred
                auto_routed = True
            else:
                # 无任何语义依赖边 ⇒ 视为相互独立的子任务，默认并联（接好的 LLM / 规则都走并联优先；
                # 仅当检测到明确先后/数据依赖时才串行）。--no-auto-route 仍可回退旧默认串行。
                goal.dependencies = []
                auto_routed = True
        elif (not opts["no_auto_route"]
              and getattr(goal, "subtasks", None) is not None
              and goal.dependencies is not None):
            # 子任务已含 inputs/outputs：dependencies 由引擎(数据依赖分析)算出，不再套角色推断。
            auto_routed = True
            dataflow_routed = True
    spec = compile_goal(goal, auto_bind=False, route=True,
                        no_adapters=opts["no_adapters"])

    # 奥卡姆剃刀化简 Pass（默认开启）：对已编译拓扑做结构精简——删掉等价不变即剃落，
    # 不确定/会变/伤完整性则保留。复杂任务的并行支路/反馈环/多重验证本就不冗余，自然保留。
    # --no-simplify 可关闭（保留原始编译产物，用于对照/调试）。等价判定用「去噪确定性模拟」，
    # 不受 SimBackend 随机噪声干扰，纯结构必要性比对。
    _simplify_report = None
    if opts.get("simplify", True):
        try:
            from compiler.simplify import simplify as _simplify
            spec, _simplify_report = _simplify(spec)
        except Exception as e:  # 化简失败不阻断规划，保留原拓扑
            print(f"\n[奥卡姆剃刀] 化简异常（保留原拓扑）: {e}")
            _simplify_report = None

    # 统一基准名：作为 SVG 文件名与看门狗跨轮键，保证多轮/多任务下命名稳定
    base = (goal.name if goal.name not in ("nl_goal", "unnamed-goal")
            else (re.sub(r"\W+", "_", nl)[:24] or "plan"))
    spec["name"] = base

    print("=" * 64)
    print("circuit-planner · 执行计划")
    print("=" * 64)
    print(f"\n[0] 解析模式: {mode}" + ("  · M3 Optimizer 已启用" if opts["optimize"] else ""))

    print("\n[1] 自然语言目标")
    print("  " + nl)

    print("\n[2] 解析出的结构化 Goal")
    print(_indent_block(json.dumps(goal.to_dict(), ensure_ascii=False, indent=2)))

    if opts["optimize"]:
        fin, sf = opt["final"], opt["search"]
        print("\n[3] M3 优化结果（权衡 延迟/成本/质量）")
        print(f"  候选={len(sf['candidates'])}  可行={len(sf['feasible'])}  Pareto前沿={len(sf['front'])}")
        print(f"  推荐解(最小成本可行): feasible={fin['feasible']}")
        pat = '并联' if fin['dependencies'] == [] else ('串联' if fin['dependencies'] is None else 'DAG')
        print(f"    tiers={fin['tiers']}")
        print(f"    布线={pat}  反馈环={fin['feedback']}  冗余={fin['redundancy']}")
        print(f"    指标: 成本 {fin['avg_cost']:.4f} | 延迟 {fin['avg_latency']:.0f}ms | "
              f"质量 {fin['avg_quality']:.3f} | 产出率 {fin['out_rate']:.2f} | 全交付率 {fin['all_fired_rate']:.2f}")
        print("  Pareto 前沿（按成本升序，非支配解）:")
        for i, f in enumerate(sf["front"][:8], 1):
            p = '并联' if f['dependencies'] == [] else ('串联' if f['dependencies'] is None else 'DAG')
            print(f"    {i}. 成本 {f['avg_cost']:.4f} | 延迟 {f['avg_latency']:.0f}ms | "
                  f"质量 {f['avg_quality']:.3f} | {p} | tiers={f['tiers']}")
        print("\n[4] 编译理由 (rationale)")
        print("  " + spec.get("rationale", ""))
    else:
        if used_template:
            print(f"\n[2.5] 模板复用：命中「{used_template}」已知良好拓扑（套用，跳过从头编译；"
                  "加 --no-template 可强制重编译）")
        if dataflow_routed:
            print(f"\n[2.6] 数据依赖分析（netlist 式）：从子任务 input/output 由规则引擎确定性算出"
                  f" {len(goal.dependencies)} 条依赖边（无需 LLM 判先后，并联自然涌现）")
        elif auto_routed:
            print(f"\n[2.6] 自动布局布线（第二层⑦）：默认串行 → 语义 DAG 推断 / 无依赖则并联优先，"
                  f"{len(goal.dependencies)} 条边（--no-auto-route 可回退串行）")
        if goal.self_heal:
            print(f"\n[2.7] 运行时拓扑热更新（第二层⑧）：已开启 self_heal，执行中失败电阻将自动升级档位"
                  f"（small→large→tool，需反馈环预算支撑）")
        if spec.get("adapters"):
            kinds = ", ".join(v["kind"].upper() for v in spec["adapters"].values())
            print(f"\n[2.8] 格式校验适配器（第二层②）：检测到 {len(spec['adapters'])} 处格式断点，"
                  f"已自动插入适配节点（{kinds}）桥接（--no-adapters 可关闭）")
        print("\n[3] 编译理由 (rationale)")
        print("  " + spec.get("rationale", ""))
        if not opts["no_auto_tiers"]:
            q_min = goal.constraints.get("min_quality", 0.0)
            high_q = goal.reliability == "high" or q_min >= 0.85
            rule = ("高可靠/高质诉求 → 质量敏感步(reason/verify/extract/summarize)升 large；"
                    "成本/延迟硬受限则保 small") if high_q else \
                   "常规可靠 → 默认 small（精度下限由 min_quality 决定）"
            print("\n[3b] 自适应型号档（第二层⑥ · 默认选型，--no-auto-tiers 可回退 Binder 基线）")
            print(f"  规则: {rule}")
            print(f"  tiers={goal.tiers}")

    if _simplify_report:
        print("\n[3c] 奥卡姆剃刀化简（默认开启，--no-simplify 关闭）")
        if _simplify_report["simplified"]:
            print(f"  原节点 {_simplify_report['original_nodes']} → 精简后 "
                  f"{_simplify_report['final_nodes']}（剃落 {len(_simplify_report['removed'])} "
                  f"+ 合并 {len(_simplify_report['merged'])}）")
            for st in _simplify_report["steps"]:
                if st["type"] == "remove":
                    print(f"    ✂ 剃落节点 {st['node']}：{st['reason']}")
                else:
                    print(f"    ⚡ 合并节点 {st['node']} → {st['into']}：{st['reason']}")
        else:
            print("  拓扑已最简，无需化简（复杂任务的并行支路/反馈环/多重验证均保留）")

    print("\n[5] 拓扑组件")
    comps = spec.get("components", {})
    cnt = Counter(c.get("type") for c in comps.values())
    print("  " + ", ".join(f"{k}={v}" for k, v in cnt.items()))
    fb = spec.get("feedback")
    if fb:
        print(f"  反馈环: from={fb['from']} → to={fb['to']} max_iter={fb['max_iter']}（整链重试）")

    print("\n[6] 执行顺序（按拓扑序）")
    steps = _plan(spec)
    idx = 0
    for s in steps:
        if s["kind"] == "agent-step":
            idx += 1
            print(f"  {idx}. [{s['capability']}] tier={s['tier']}  建议工具: {s['tool']}")
        elif s["kind"] == "buffer":
            print(f"     · 缓冲/汇合: {s['node']} ({s['label']}, mode={s['mode']})")
        elif s["kind"] == "validate":
            print(f"     · 校验: {s['node']} ({s['label']}, 阈值={s['threshold']})")
        elif s["kind"] == "adapter":
            print(f"     · 格式适配: {s['node']} ({s['label']}, "
                  f"{s['from_fmt']}→{s['to_fmt']})")
        else:
            print(f"     · {s['node']} ({s['kind']})")

    print("\n[7] 完整拓扑 JSON（供 agent 精确映射 wires）")
    print(_indent_block(json.dumps(spec, ensure_ascii=False, indent=2)))

    # ---- 执行 pass（默认 SimBackend 仿真，确定性、零成本）----
    # 产出执行遥测，供 [8] SVG 复盘标注 与 [9] runbook 实测引用。
    # --backend=real 时由 [10] 在线实测，这里跳过（[10] 结束会再用真实结果重绘带标注的图）。
    exec_result = None
    if opts.get("execute") and opts.get("backend") != "real":
        try:
            wd = rt.Watchdog() if (rt and opts.get("watchdog", True)) else None
            sim_backend = rt.SimBackend(random.Random(0))
            sim_circuit = rt.Circuit(spec, sim_backend)
            exec_result = sim_circuit.execute(self_heal=opts["self_heal"], watchdog=wd)
        except Exception as e:
            exec_result = None
            print(f"\n[sim-exec] 仿真执行失败: {e}")

    if opts.get("draw"):
        try:
            import draw
            diag_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
            os.makedirs(diag_dir, exist_ok=True)
            out_path = os.path.join(diag_dir, base + ".svg")
            draw.draw(spec, out_path, exec_result=exec_result)
            print("\n[8] 电路拓扑图 (SVG):", out_path)
        except Exception as e:
            print(f"\n[8] 电路图生成失败: {e}")

    # C 修复：默认 backend=sim，仿真结果极易被误读成真实模型输出——展示前显式告警
    if opts.get("backend") in ("sim", "mock"):
        print("\n⚠ 后端 = 仿真(SimBackend)：以下 success / quality / cost / latency "
              "均为【模拟值】，不代表真实模型表现。"
              "要真实在线实测请加 --backend=real（会消耗真实 token）。")

    if opts.get("execute"):
        rb = _runbook(spec, goal.name)
        print("\n[9] 执行 runbook（agent 运行时据此真实执行，非脚本自跑）")
        adp_n = len(spec.get("adapters") or {})
        print(f"  步骤数={len(rb['steps'])}  "
              f"反馈环={'有(max_iter=%d)' % rb['feedback']['max_iter'] if rb['feedback'] else '无'}  "
              f"质量门={rb['quality_gates'] or '无'}  "
              f"锁相环里程碑={len(rb.get('milestones', []))}  "
              f"格式适配器={adp_n or '无'}")
        if rb.get("bus"):
            print(f"  共享上下文总线: 启用（触发={rb['bus']['trigger']} · {rb['bus']['stages']} 阶段；"
                  f"纯拓扑压缩表示，runtime 语义不变）")
        for s in rb["steps"]:
            par = ("  ‖并行步骤:" + str(s["parallel_with"])) if s["parallel_with"] else ""
            ctx = "、".join(s["input_context"]) if s["input_context"] else "（任务源 / 无上游）"
            est = s.get("estimated_latency_ms", "?")
            print(f"  {s['step']}. [{s['capability']}] tier={s['tier']}  工具: {s['tool']}  est≈{est}ms")
            print(f"       输入上下文 ← {ctx}{par}")
        eb = rb.get("estimated_breakdown")
        if eb:
            print(f"  预估总耗时: {eb['total_ms']/1000:.1f}s（最长并行层取max={eb['longest_parallel_layer_ms']/1000:.1f}s，"
                  f"其余串联累加={eb['rest_serial_ms']/1000:.1f}s）[heuristic]")
        if exec_result:
            print(f"  仿真实测: success={exec_result['success']} iterations={exec_result['iterations']} "
                  f"cost={exec_result['total_cost']} latency_ms={exec_result['total_latency_ms']} "
                  f"final_quality={exec_result['final_quality']}")
            if "self_healed" in exec_result:
                print(f"  自愈升级(含看门狗预升级): {exec_result['self_healed']}")
            if exec_result.get("watchdog"):
                deg = [c for c, v in exec_result["watchdog"].items() if v.get("degraded")]
                print(f"  看门狗劣化节点(跨轮): {deg or '无'}")
            print("  SVG 复盘标注: 红框=慢节点 / 橙虚框=重试或曾失步 / 绿✓=自愈升级 / 紫⚠=看门狗劣化")
        if rb["feedback"]:
            print(f"  重试策略: 反馈环 from={rb['feedback']['from']} → to={rb['feedback']['to']}，"
                  f"整链重试最多 {rb['feedback']['max_iter']} 次（刷新上下文后重跑）")
        rb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runbooks")
        os.makedirs(rb_dir, exist_ok=True)
        base = spec.get("name") or (re.sub(r"\W+", "_", nl)[:24] or "plan")
        rb_path = os.path.join(rb_dir, base + "_runbook.json")
        with open(rb_path, "w", encoding="utf-8") as f:
            json.dump(rb, f, ensure_ascii=False, indent=2)
        print(f"  机读 runbook: {rb_path}")

    # ---- 补强#3：真实 LLM 后端在线实测（--backend=real）----
    if opts.get("backend") == "real":
        base_url = (os.environ.get("AGENT_API_BASE")
                    or os.environ.get("OPENAI_BASE_URL"))
        res, mode = _run_real(spec, api_key, base_url)
        print(f"\n[10] 真实 LLM 后端实测（补强#3 · {mode}）")
        print(f"  success={res['success']}  iterations={res['iterations']}  "
              f"cost={res['total_cost']}  latency_ms={res['total_latency_ms']}  "
              f"final_quality={res['final_quality']}")
        print("  各组件实测:")
        for c, s in res["components"].items():
            print(f"    {c}: ok={s['ok']} q={s['quality']} cost={s.get('cost', 'n/a')} "
                  f"lat={s.get('latency_ms', 'n/a')}")
        if "self_healed" in res:
            print(f"  self_healed={res['self_healed']}")
        # ---- A 修复：实测正文回显 + 落盘（修复前：花真钱生成后直接丢弃，零产出）----
        # res["state"]["_fetched"] 即各节点真实产出正文（runtime 一直返回，只是从未往外取）
        nv = res.get("node_values") or {}
        fetched = (nv.get("by_output") or nv.get("by_node") or {})
        if fetched:
            print("  各节点产出正文（A 修复前：生成后直接丢弃）:")
            for k, v in fetched.items():
                sv = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                             default=str)
                sv = " ".join(str(sv).split())
                print(f"    ▸ {k}: {sv[:400]}{' …(略)' if len(sv) > 400 else ''}")
        try:
            out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
            os.makedirs(out_dir, exist_ok=True)
            base = spec.get("name") or (re.sub(r"\W+", "_", nl)[:24] or "plan")
            out_path = os.path.join(out_dir, base + "_outputs.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"goal": nl, "spec_name": base, "mode": mode,
                     "summary": {k: v for k, v in res.items() if k != "state"},
                     "node_outputs": fetched},
                    f, ensure_ascii=False, indent=2, default=str,
                )
            print(f"  ↳ 节点正文已落盘: {out_path}"
                  f"（{len(fetched)} 个产出字段，"
                  f"共 {sum(len(str(v)) for v in fetched.values())} 字符）")
        except Exception as e:
            print(f"  ↳ 正文落盘失败: {e}")
        if not res["success"]:
            print("  ⚠ 本次实测未达标（多为网络不可达 / key 无效 / base_url 不匹配导致开路）。")
            print("    真·在线实测需三者齐备：① API key（DEEPSEEK_API_KEY 或 OPENAI_API_KEY）；"
                  "② base_url 指向对应 OpenAI-compatible 端点（DeepSeek 默认已自动识别）；③ 网络可达。")
        if not api_key:
            print("  ⚠ 配置 API key + base_url 后即为真·在线实测；示例：")
            print("    AGENT_API_BASE=https://api.deepseek.com/v1 DEEPSEEK_API_KEY=xxx "
                  "python plan.py \"<目标>\" --backend=real")

        # 用真实实测结果重绘带复盘标注的 SVG（覆盖 [8] 的仿真标注版）
        if opts.get("draw"):
            try:
                import draw
                base = spec.get("name") or (re.sub(r"\W+", "_", nl)[:24] or "plan")
                diag_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
                os.makedirs(diag_dir, exist_ok=True)
                out_path = os.path.join(diag_dir, base + ".svg")
                draw.draw(spec, out_path, exec_result=res)
                print(f"  ↳ 已用真实实测结果重绘 SVG 复盘标注: {out_path}")
            except Exception as e:
                print(f"  ↳ 真实标注重绘失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

