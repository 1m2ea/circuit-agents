#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""circuit-agents MCP stdio server — 标准库实现（零三方依赖）。

把 circuit-agents（github.com/1m2ea/circuit-agents）以 MCP 工具形式暴露给
Cursor / Trae / Coze / CodeBuddy / Claude Desktop 等客户端。

传输：MCP stdio = 换行分隔 JSON-RPC 2.0。
工具：selftest | simulate | plan

环境变量（均可选）：
  CIRCUIT_AGENTS_REPO    代码根，默认 D:\\dev\\projects\\666\\circuit-agents
  CIRCUIT_AGENTS_PYTHON  子进程 python，默认 sys.executable
  CIRCUIT_PLANNER_PY     plan.py 路径，默认在 ~/.dsh 与 ~/.workbuddy 技能里查找
"""
import json
import os
import re
import subprocess
import sys
import time

SERVER_NAME = "circuit-agents-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2024-11-05"

REPO = os.environ.get("CIRCUIT_AGENTS_REPO") or r"D:\dev\projects\666\circuit-agents"
PY = os.environ.get("CIRCUIT_AGENTS_PYTHON") or sys.executable

# 潜意识层（Subconscious Layer）：常驻后台加工 daemon，对主链路不可见，
# 经受限 query(top_k) 给意识层候选假设。离线零依赖；加载失败则静默降级为 None。
_SUBCONSCIOUS = None
try:
    if REPO and os.path.isdir(REPO) and REPO not in sys.path:
        sys.path.insert(0, REPO)
    from compiler.subconscious_layer import SubconsciousLayer  # noqa: E402
    try:
        from compiler.wenyan_corpus import wenyan_associate_fn as _WENYAN_FN  # noqa: E402
    except Exception:  # pragma: no cover
        _WENYAN_FN = None
    _SUBCONSCIOUS = (SubconsciousLayer(interval=30.0, associate_fn=_WENYAN_FN)
                     if _WENYAN_FN else SubconsciousLayer(interval=30.0))
except Exception:  # pragma: no cover
    _SUBCONSCIOUS = None


def _first_existing(paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return ""


PLAN_PY = _first_existing([
    os.environ.get("CIRCUIT_PLANNER_PY") or "",
    os.path.join(os.path.expanduser("~"), ".dsh", "skills", "circuit-planner", "scripts", "plan.py"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", "circuit-planner", "scripts", "plan.py"),
])

# ---- 治理执行驱动器(exec_loop): 复用技能里的状态机, DSH agent 经 circuit_exec 驱动 ----
EXEC_LOOP_PY = _first_existing([
    os.environ.get("CIRCUIT_EXEC_LOOP_PY") or "",
    os.path.join(os.path.dirname(PLAN_PY), "exec_loop.py") if PLAN_PY else "",
    os.path.join(os.path.expanduser("~"), ".dsh", "skills", "circuit-planner", "scripts", "exec_loop.py"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", "circuit-planner", "scripts", "exec_loop.py"),
])
_EL = None  # lazily-loaded exec_loop module
_EL_SESSIONS = {}  # runbook_path(abspath) -> state dict, 引擎进程内常驻
_EL_SPECS = {}     # runbook_path(abspath) -> topology spec (事实真相源, mutate/rebuild 用)


def _exec_loop_module():
    """加载 exec_loop 模块(惰性, 缓存)。失败返回 None 供工具说明。"""
    global _EL
    if _EL is not None:
        return _EL
    if not EXEC_LOOP_PY or not os.path.isfile(EXEC_LOOP_PY):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("circuit_exec_loop", EXEC_LOOP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _EL = mod
    return _EL


def _run(cmd, cwd=None, timeout=600):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace",
                          env=env)
    out = (proc.stdout or "")
    if proc.stderr:
        out += "\n[stderr] " + proc.stderr
    return out, proc.returncode == 0


def tool_selftest(args):
    code = ("import os, sys; os.chdir(sys.argv[1]); sys.path.insert(0, sys.argv[1]); "
            "import runtime; print('SELFTEST-OK'); runtime.selftest()")
    return _run([PY, "-c", code, REPO], cwd=REPO)


def tool_simulate(args):
    a = args or {}
    topo = str(a.get("topology", "examples/parallel.json"))
    runs = int(a.get("runs", 20))
    seed = int(a.get("seed", 42))
    topo_path = topo
    if topo.lstrip().startswith(("{", "[")):
        topo_path = os.path.join(REPO, "_mcp_topo.json")
        with open(topo_path, "w", encoding="utf-8") as fh:
            fh.write(topo)
    if not os.path.isabs(topo_path):
        topo_path = os.path.join(REPO, topo_path)
    return _run([PY, "run.py", topo_path, "--runs", str(runs), "--seed", str(seed)], cwd=REPO)


def tool_plan(args):
    a = args or {}
    goal = str(a.get("goal", "")).strip()
    if not goal:
        return "error: 'goal' 不能为空", False
    if not PLAN_PY:
        return "error: 未找到 plan.py（可用 CIRCUIT_PLANNER_PY 显式指定）", False
    cmd = [PY, PLAN_PY, goal]
    if a.get("optimize"):
        cmd.append("--optimize")
    if not a.get("draw", True):
        cmd.append("--no-draw")
    out, ok = _run(cmd, cwd=os.path.dirname(PLAN_PY), timeout=1200)
    if len(out) > 20000:
        out = out[:20000] + "\n...[截断]"
    # ---- Step2 · 仿真先行预演: 从 plan 输出提取 runbook 路径与仿真指标, 供决策 ----
    rb_path = ""
    m = re.search(r"机读 runbook:\s*(\S+)", out)
    if m:
        rb_path = m.group(1)
    sim = re.search(r"仿真实测:\s*success=(\S+)\s*iterations=(\S+)\s*cost=(\S+)\s*latency_ms=(\S+)\s*final_quality=(\S+)", out)
    if sim or rb_path:
        preview = ["", "### [SIM-PREVIEW] 仿真先行 · 预演结果(非真实表现, 供决策)"]
        if sim:
            succ, it, cost, lat, fq = sim.groups()
            try:
                succ_f = 1.0 if succ.lower() in ("true", "pass", "1") else (0.0 if succ.lower() in ("false", "fail", "0") else float(succ))
            except Exception:
                succ_f = 0.0
            try:
                fq_f = float(fq)
            except Exception:
                fq_f = 0.0
            preview.append(f"[质量门通过?] {succ} · iterations={it}  avg_cost={cost}  avg_latency_ms={lat}  final_quality={fq}")
            if succ_f < 0.5 and fq_f < 0.8:
                preview.append("⚠ 仿真未过质量门/最终质量偏低 → 建议: ① 让 plan 加反馈环/加质量校验, ② 明确接受低成功风险再真执行, ③ 补真实数据源。")
            else:
                preview.append("✅ 仿真结果可接受 → 可 proceed 真执行(circuit_exec)。")
        if rb_path:
            preview.append(f"runbook 路径: {rb_path}  (exec 用: circuit_exec action=start runbook={rb_path})")
        out += "\n" + "\n".join(preview)
    return out, ok


def _sub_json(obj):
    """把潜意识层对象路由为 UTF-8 JSON 文本（decode 可靠且含原文，避免控制台编码丢失）。"""
    return json.dumps(obj, ensure_ascii=False)


def _pending_path():
    """待验证记忆区（未通过验证门的教训先落这里, 不参与 plan 自动织入）。"""
    return os.path.join(REPO, ".topology_memory_pending.json")


def _pending_load():
    try:
        with open(_pending_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("lessons"), list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"lessons": []}


def _pending_save(data):
    try:
        data["lessons"] = data.get("lessons", [])[-200:]  # FIFO 上限, 与长期记忆一致
        with open(_pending_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def _norm_lesson(t):
    return re.sub(r"\s+", "", str(t or ""))


def tool_circuit_record_outcome(args):
    """记忆互灌 + 【Data-RSI 验证门】(借 MetaRSI: 只把"已验证"经验合成为长期记忆)。

    分级规则(零回归: 不传新参数时 lesson_type 默认 case → 落待验证区, 不再污染长期记忆):
      · 长期记忆(.topology_memory.json, 会被 plan 自动织入): 需 verified 且 lesson_type=governance
        其中 verified := 显式 verified=true 或 (quality>=gate_threshold 且 有 evidence)
      · 待验证区(.topology_memory_pending.json): 其余情形, 仅显式 recall 可见
      · 晋升: 曾在待验证区的同文本教训, 本次满足长期条件 → 写入长期并从待验证区移除
    执行记录仍进 ExecutionStore(executions.db)。全程零回归: 失败静默返回说明。
    """
    a = args or {}
    goal = str(a.get("goal") or "").strip()
    status = str(a.get("status") or "unknown").strip()
    lessons = a.get("lessons") or []
    if isinstance(lessons, str):
        lessons = [lessons]
    evidence = str(a.get("evidence") or "").strip()
    lesson_type = str(a.get("lesson_type") or "case").strip().lower()  # governance | case
    explicit_verified = bool(a.get("verified"))
    try:
        gate_thr = float(a.get("gate_threshold", 0.8))
    except Exception:  # noqa: BLE001
        gate_thr = 0.8
    try:
        quality = float(a.get("quality")) if a.get("quality") is not None else None
    except Exception:  # noqa: BLE001
        quality = None
    # 验证判定(数据来自质量门/里程碑/人工)
    is_verified = explicit_verified or (quality is not None and quality >= gate_thr and bool(evidence))
    to_longterm = is_verified and lesson_type == "governance"
    written = {"lessons": 0, "lesson_texts": [], "execution": None,
               "verified": is_verified, "lesson_type": lesson_type,
               "routed_to": "longterm" if to_longterm else "pending",
               "longterm": [], "pending": [], "promoted": []}
    # 1) 教训 → 分级写入
    try:
        if REPO and os.path.isdir(REPO) and REPO not in sys.path:
            sys.path.insert(0, REPO)
        from compiler.topology_memory import TopologyMemory  # noqa: E402
        mem = TopologyMemory()
        pend = _pending_load()
        pend_norm = {_norm_lesson(l.get("text")) for l in pend.get("lessons", [])}
        for lsn in lessons:
            txt = str(lsn or "").strip()
            if not txt:
                continue
            norm = _norm_lesson(txt)
            if to_longterm:
                mem.record_lesson(txt, tags=[status, lesson_type, "verified"]
                                  + ([goal[:24]] if goal else []))
                written["lessons"] += 1
                written["lesson_texts"].append(txt[:120])
                written["longterm"].append(txt[:120])
                if norm in pend_norm:  # 晋升: 从待验证区移除
                    pend["lessons"] = [l for l in pend["lessons"]
                                       if _norm_lesson(l.get("text")) != norm]
                    pend_norm.discard(norm)
                    written["promoted"].append(txt[:120])
            else:
                if norm not in pend_norm:
                    pend.setdefault("lessons", []).append({
                        "text": txt, "tags": [status, lesson_type],
                        "ts": int(time.time() * 1000), "goal": goal[:80],
                        "quality": quality, "verified": is_verified,
                        "evidence": evidence[:200], "gate_threshold": gate_thr,
                    })
                    pend_norm.add(norm)
                written["pending"].append(txt[:120])
        _pending_save(pend)
    except Exception as exc:  # noqa: BLE001
        written["lessons_error"] = f"{type(exc).__name__}: {exc}"
    # 2) 执行记录 → ExecutionStore.save
    try:
        if goal:
            from execution_store import ExecutionStore  # noqa: E402
            store = ExecutionStore()
            run_id = f"mcp_{int(time.time() * 1000)}"
            spec = a.get("spec") or {}
            result = a.get("result") or {"quality": a.get("quality"), "status": status}
            store.save(run_id, goal, status, spec, [], result)
            written["execution"] = {"run_id": run_id, "status": status}
    except Exception as exc:  # noqa: BLE001
        written["execution_error"] = f"{type(exc).__name__}: {exc}"
    return _sub_json({"written": written, "goal": goal[:80], "status": status}), True


def _el_session(runbook):
    """取/建某 runbook 的执行状态(引擎进程常驻; runbook 需为绝对路径)。"""
    rb = os.path.abspath(runbook)
    if rb not in _EL_SESSIONS:
        raise ValueError(f"runbook 未 start: {runbook}")
    return rb, _EL_SESSIONS[rb]


_PLAN_MODS = {}  # plan.py path -> module (惰性缓存)


def _front_old_new(old_rb, new_rb):
    """把旧 runbook 步骤映射到新 runbook 步骤（返回 {old_step: new_step}, old, new）。

    匹配优先级（可靠性从高到低）：
      1) node 名相同 —— step.node 即拓扑 component id，是"同一事实节点"的最稳锚点；
         rebuild/mutate 后同名节点就是同一执行单元（能力标签反而可能因重排改变）；
      2) capability 相同 —— 能力标签（retrieve/extract/...）相同作后备；
      3) base 能力相同 —— 去掉 #N 多重集后缀后同名（retrieve#2 -> retrieve）再兜底。
    每个新步骤至多被消耗一次；按旧步骤次序取最先未配对者，保证连线稳定。
    """
    old = sorted(old_rb.get("steps", []), key=lambda s: s.get("step", 0))
    new = sorted(new_rb.get("steps", []), key=lambda s: s.get("step", 0))

    def _base(cap):
        return re.sub(r"#\d+$", "", str(cap or "")).strip()

    new_by_node = {}
    for ni, n in enumerate(new):
        new_by_node.setdefault(str(n.get("node")), []).append(ni)
    mapping = {}
    used = set()
    # 1) node 名匹配（主路径：node 是 spec component id，演化后最稳）
    rest = []
    for o in old:
        ni = next((x for x in new_by_node.get(str(o.get("node")), []) if x not in used), None)
        if ni is not None:
            mapping[o.get("step")] = new[ni].get("step")
            used.add(ni)
        else:
            rest.append(o)
    # 2) capability 匹配（后备）
    rest2 = []
    for o in rest:
        cap = o.get("capability")
        ni = next((i for i, n in enumerate(new) if i not in used
                   and n.get("capability") == cap), None)
        if ni is not None:
            mapping[o.get("step")] = new[ni].get("step")
            used.add(ni)
        else:
            rest2.append(o)
    # 3) base 能力兜底（retrieve#2 -> retrieve）
    for o in rest2:
        cap = _base(o.get("capability"))
        ni = next((i for i, n in enumerate(new) if i not in used
                   and _base(n.get("capability")) == cap), None)
        if ni is not None:
            mapping[o.get("step")] = new[ni].get("step")
            used.add(ni)
    return mapping, old, new


def _carry_artifacts(old_art, old_rb, new_rb):
    """把旧 runbook 已完成步骤的产出续接到新 runbook 对应步骤，返回 {new_step: art}。

    优先按 node 名映射（新旧 step.node 相同 = 同一事实节点，其产出直接续接），
    capability 匹配作为后备——目标：mutate/rebuild 后不重跑已成功步骤。
    """
    mapping, old, _ = _front_old_new(old_rb, new_rb)
    carry = {}
    for o in old:
        ost = o.get("step")
        art = old_art.get(str(ost)) or old_art.get(ost)
        if art is None:
            continue
        nst = mapping.get(ost)
        if nst is not None:
            carry[str(nst)] = art
    return carry


def _reinit_state_for(new_rb, carry, keepgate=None):
    """为新 runbook 初始化执行状态并续接已完成步骤产出; gate 沿用旧结论。"""
    st = {"name": new_rb.get("name", ""), "artifacts": dict(carry),
          "gate": keepgate, "iter": 1, "status": "running",
          "feedback_max_iter": (new_rb.get("feedback") or {}).get("max_iter"),
          "milestones": {}, "corrections": {}}
    return st


def _spec_from_runbook(rb):
    """从 runbook 步骤反推最小可用 spec（mutate 无 _EL_SPECS 缓存时的操作面）。

    注意不能用空壳 spec（components={}）——那样单点 op（escalate/remove/insert 等）
    找不到节点、直接报废。这里至少为 runbook 每个 step.node 建一个 resistor 节点
    （保留能力标签/型号档），并按 input_context 的「步骤N」行连出依赖 wires、
    给无上游的根步骤接一个 src 源节点；parallel_with 表示同层并行（互不依赖），
    不建边。仅为 mutate 提供可用操作面，rebuild/后续 mutate 会产出并缓存真实 spec。
    """
    steps = sorted((rb or {}).get("steps", []), key=lambda s: s.get("step", 0))
    comps, wires = {}, []
    node_of_step = {}
    for s in steps:
        node = str(s.get("node") or "").strip() or ("step_%s" % s.get("step"))
        comps[node] = {"type": "resistor",
                       "label": s.get("capability") or node,
                       "model": s.get("tier") or "tool"}
        node_of_step[int(s.get("step", 0))] = node
    roots = set(node_of_step.values())
    for s in steps:
        node = str(s.get("node") or "").strip() or ("step_%s" % s.get("step"))
        for line in s.get("input_context") or []:
            mo = re.search(r"步骤(\d+)", str(line))
            if mo and int(mo.group(1)) in node_of_step:
                prod = node_of_step[int(mo.group(1))]
                if [prod, node] not in wires:
                    wires.append([prod, node])
                roots.discard(node)  # 有上游依赖 → 不是根步骤
    if roots:
        comps.setdefault("src", {"type": "power", "label": "task"})
        for rn in sorted(roots):
            if ["src", rn] not in wires:
                wires.append(["src", rn])
    return {"name": rb.get("name", "evolved"), "task": rb.get("name", ""),
            "components": comps, "wires": wires,
            "feedback": rb.get("feedback"),
            "quality_gates": rb.get("quality_gates"),
            "milestones": rb.get("milestones")}


def _write_new_runbook(new_rb, goal_name=""):
    """把新 runbook 写到 runbooks 目录, 返回规范化绝对路径。"""
    import tempfile
    base = re.sub(r"\W+", "_", goal_name or new_rb.get("name", "evolved"))[:40] or "evolved"
    outdir = os.path.join(os.path.dirname(PLAN_PY), "runbooks") if PLAN_PY else tempfile.gettempdir()
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, base + "_evolved_runbook.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(new_rb, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)  # 规范化为全反斜杠，与 _EL_SESSIONS/_EL_SPECS 的查询键一致


def _plan_module():
    """惰性加载计划脚本模块(供 _runbook / compile_goal 用)。"""
    if PLAN_PY and os.path.isfile(PLAN_PY):
        import importlib.util
        if PLAN_PY not in _PLAN_MODS:
            spec = importlib.util.spec_from_file_location("ca_plan", PLAN_PY)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PLAN_MODS[PLAN_PY] = mod
        return _PLAN_MODS[PLAN_PY]
    return None


def tool_circuit_evolve(args):
    """拓扑事实驱动演化: 执行中事实变化时让 runbook 就地演化, 而非死扛旧图。
    两个动作:
      rebuild: 到 plan 受修正后的目标(goal)或约束(patch)重新编译 -> 新 spec+runbook, 续接已完成产出。
      mutate:  就地改/插/删拓扑节点(op=insert|remove|reroute|escalate) -> 重生 runbook 续接。
    续接规则: 新旧 runbook 步骤优先按 node(拓扑 component id) 同名续接, capability 作后备,
    让已完成步骤产出尽量带进新 runbook、不重跑已成功。
    mutate 无缓存 spec 时(直接 start 的 plan runbook)自动从 runbook 步骤重建最小可用
    spec 作为操作面(spec_source=runbook_synthesized); 完整拓扑请先 rebuild。
    更新触发可来自: 节点失败/质检不达 / agent 发现新事实 / 用户中途改需求。
    """
    a = args or {}
    action = str(a.get("action") or "rebuild").strip()
    runbook = os.path.abspath(str(a.get("runbook") or "").strip())
    if not runbook or not os.path.isfile(runbook):
        return "error: 需要有效的 runbook 路径", False
    rb = _EL_SESSIONS.get(runbook)
    if rb is None:
        return f"error: runbook 未 start: {runbook}（先 circuit_exec action=start）", False
    # 会话状态(artifacts/gate)在 _EL_SESSIONS；但旧步骤结构(steps/node)需从 runbook 文件取
    # —— 状态 dict 只存 name/artifacts 等执行字段，不携带 steps，直接用它做映射会拿不到旧步骤。
    try:
        with open(runbook, encoding="utf-8") as fh:
            rb_json = json.load(fh)
    except Exception:  # noqa: BLE001
        rb_json = None
    plan = _plan_module()
    if plan is None:
        return "error: plan 模块不可用", False
    try:
        if action == "rebuild":
            # 重新编译: 接受修正目标/约束说明, 产出新 spec, 经 _runbook 生成新 runbook(续接 old artifacts)
            new_goal = str(a.get("goal") or "").strip()
            if not new_goal:
                # 无新目标则沿用原 goal(spec.task)
                old_spec = _EL_SPECS.get(runbook)
                new_goal = (old_spec or {}).get("task") or rb.get("name", "")
            from compiler.nl_parser import GoalParser  # noqa: E402
            from compiler.compile import compile_goal  # noqa: E402
            gp = GoalParser()
            goal = gp.parse(new_goal)
            new_spec = compile_goal(goal, route=True, memory_enabled=True)
            new_rb = plan._runbook(new_spec, new_goal)
            # 续接: 保留已完成步骤的产出(旧 runbook 与新优先按 node 名匹配的步骤)
            old_art = dict(rb.get("artifacts") or {})
            carry = _carry_artifacts(old_art, rb_json or rb, new_rb)
            new_state = _reinit_state_for(new_rb, carry, keepgate=rb.get("gate"))
            new_path = _write_new_runbook(new_rb, new_goal)
            _EL_SPECS[new_path] = new_spec
            _EL_SESSIONS[new_path] = new_state
            if os.path.abspath(new_path) != runbook:
                _EL_SPECS.pop(runbook, None); _EL_SESSIONS.pop(runbook, None)
            el_mod = _exec_loop_module()
            directive = None
            if el_mod is not None:
                try:
                    directive = el_mod.next_directive(new_rb, new_state)
                except Exception:  # noqa: BLE001
                    directive = None
            _log_op(runbook, "rebuild", new_goal[:60])
            return json.dumps({"action": "rebuild", "new_runbook": new_path,
                               "carried_steps": sorted(carry.keys()),
                               "state": new_state.get("status"),
                               "directive": directive}, ensure_ascii=False), True
        if action == "mutate":
            op = str(a.get("op") or "insert").strip()
            new_cid = str(a.get("cid") or "x_new").strip()
            # 拓扑事实真相源在 _EL_SPECS（rebuild/mutate 产出后已缓存）。没有缓存时
            # （如直接 circuit_exec start 的 plan runbook）从 runbook 步骤重建最小可用
            # spec —— 不能用空壳 spec（components={}），否则单点 op 找不到节点报废。
            current_spec = _EL_SPECS.get(runbook)
            spec_source = "cached_spec"
            if current_spec is None:
                current_spec = _spec_from_runbook(rb_json or rb)
                spec_source = "runbook_synthesized"
            comps = current_spec.get("components", {}) or {}
            avail = ", ".join(sorted(comps.keys())) or "无"

            def _no_node(why):
                return (f"error: mutate {why} 目标节点不在当前可用拓扑中（可用节点: {avail}）。"
                        f"提示：完整拓扑需先对该 runbook 执行 rebuild（真实 spec 会缓存）；"
                        f"cid 应取 runbook 步骤里的 node。"), False

            from runtime import CircuitMutator  # noqa: E402
            heal_report = None
            if op == "insert":
                pred = str(a.get("pred") or "").strip()
                succ = str(a.get("succ") or "").strip()
                comp = a.get("comp") or {"type": "resistor", "label": new_cid, "model": "tool"}
                for ref, tag in ((pred, "pred"), (succ, "succ")):
                    if ref and ref not in comps:
                        return _no_node(f"insert {tag}={ref} 的")
                if new_cid in comps:
                    return f"error: insert 节点 {new_cid} 已存在（改拓扑请用其它 op/cid）", False
                new_spec = CircuitMutator.insert_node(current_spec, new_cid, comp,
                                                      [pred] if pred else [], [succ] if succ else [])
            elif op == "remove":
                if new_cid not in comps:
                    return _no_node(f"remove {new_cid} 的")
                new_spec = CircuitMutator.remove_node(current_spec, new_cid)
            elif op == "reroute":
                new_spec = CircuitMutator.reroute(current_spec, str(a.get("old") or "").strip(),
                                                  str(a.get("new") or "").strip())
            elif op == "escalate":
                if new_cid not in comps:
                    return _no_node(f"escalate {new_cid} 的")
                if comps.get(new_cid, {}).get("type") not in (None, "resistor"):
                    return (f"error: escalate 仅作用于 resistor 节点，{new_cid} 是 "
                            f"{comps.get(new_cid, {}).get('type')}", False)
                # auto_heal_topology 返回 (new_spec, report) 元组，须解包
                new_spec, heal_report = CircuitMutator.auto_heal_topology(current_spec, [new_cid])
                if not heal_report:
                    return f"error: escalate 未产生冗余分支（节点 {new_cid}）", False
            else:
                return f"error: 未知 mutate op {op}", False
            task = (current_spec or {}).get("task") or rb.get("name", "")
            new_rb = plan._runbook(new_spec, task)
            carry = _carry_artifacts(dict(rb.get("artifacts") or {}), rb_json or rb, new_rb)
            new_state = _reinit_state_for(new_rb, carry, keepgate=rb.get("gate"))
            new_path = _write_new_runbook(new_rb, task)
            _EL_SPECS[new_path] = new_spec
            _EL_SESSIONS[new_path] = new_state
            if os.path.abspath(new_path) != runbook:
                _EL_SPECS.pop(runbook, None); _EL_SESSIONS.pop(runbook, None)
            resp = {"action": "mutate", "op": op, "new_runbook": new_path,
                    "carried_steps": sorted(carry.keys()),
                    "state": new_state.get("status"),
                    "spec_source": spec_source}
            if heal_report is not None:
                resp["heal"] = heal_report
            _log_op(runbook, "mutate/" + op, new_cid)
            return json.dumps(resp, ensure_ascii=False), True
        return f"error: 未知 action {action}（rebuild|mutate）", False
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}", False



def tool_circuit_exec(args):
    """治理执行闭环驱动: 复用 exec_loop.py 状态机(里程碑/分层并行/质量门/整链重试)。
    action 决定行为: start 初始化并返回首条指令; next 推进; record 记录步骤产出;
    record_gate/record_ms 记质量门/里程碑; reset 清空; status 查状态。
    """
    a = args or {}
    el = _exec_loop_module()
    if el is None:
        return "error: exec_loop 驱动器不可用（未找到 exec_loop.py，可设 CIRCUIT_EXEC_LOOP_PY）", False
    action = str(a.get("action") or "next").strip()
    runbook = str(a.get("runbook") or "").strip()
    if not runbook:
        return "error: 需要 runbook 路径", False
    rb = os.path.abspath(runbook)
    try:
        if action == "start" or action == "reset":
            if not os.path.isfile(rb):
                return f"error: runbook 不存在: {rb}", False
            rb_spec = el.load_runbook(rb)
            st = el.init_state(rb_spec)
            if action == "start" and rb in _EL_SESSIONS:
                pass  # 保留已有状态? start 语义=从当前状态推进; reset 才清空
            if action == "reset":
                _EL_SESSIONS[rb] = st
            else:
                _EL_SESSIONS.setdefault(rb, st)
            directive = el.next_directive(rb_spec, _EL_SESSIONS[rb])
            _log_op(rb, action)
            return json.dumps({"action": action, "state": _EL_SESSIONS[rb].get("status"),
                               "directive": directive}, ensure_ascii=False), True
        # 后续动作需已有 session
        if rb not in _EL_SESSIONS:
            return f"error: runbook 尚未 start: {runbook}（先 action=start）", False
        st = _EL_SESSIONS[rb]
        _log_op(rb, action, str(a.get("step") or a.get("milestone") or ""))
        if action == "next":
            directive = el.next_directive(el.load_runbook(rb), st)
            return json.dumps({"action": "next", "state": st.get("status"),
                               "directive": directive}, ensure_ascii=False), True
        if action == "record":
            step = str(a.get("step") or "").strip()
            artifact = str(a.get("artifact") or "").strip()
            if not step.isdigit():
                return "error: record 需要整数 step", False
            st.setdefault("artifacts", {})[step] = artifact
            directive = el.next_directive(el.load_runbook(rb), st)
            return json.dumps({"action": "record", "step": step, "state": st.get("status"),
                               "directive": directive}, ensure_ascii=False), True
        if action == "record_gate":
            ok = str(a.get("pass") or a.get("ok") or "").strip().lower() in ("pass", "p", "true", "1", "yes")
            st["gate"] = "pass" if ok else "fail"
            directive = el.next_directive(el.load_runbook(rb), st)
            return json.dumps({"action": "record_gate", "pass": ok, "state": st.get("status"),
                               "directive": directive}, ensure_ascii=False), True
        if action == "record_ms":
            mid = str(a.get("milestone") or "").strip()
            ok = str(a.get("pass") or a.get("ok") or "").strip().lower() in ("pass", "p", "true", "1", "yes")
            rb_spec = el.load_runbook(rb)
            ms_list = rb_spec.get("milestones") or []
            if mid:
                st.setdefault("milestones", {})[mid] = "pass" if ok else "fail"
            else:
                pending = next((m for m in ms_list
                                if not st.get("milestones", {}).get(m["id"])
                                and all(str(s) in st.get("artifacts", {}) for s in m["after_steps"])), None)
                if pending:
                    st.setdefault("milestones", {})[pending["id"]] = "pass" if ok else "fail"
            directive = el.next_directive(rb_spec, st)
            return json.dumps({"action": "record_ms", "state": st.get("status"),
                               "directive": directive}, ensure_ascii=False), True
        if action == "status":
            return json.dumps({"status": st.get("status"), "iter": st.get("iter"),
                               "artifacts": list(st.get("artifacts", {}).keys()),
                               "gate": st.get("gate")}, ensure_ascii=False), True
        return f"error: 未知 action {action}（start|reset|next|record|record_gate|record_ms|status）", False
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}", False


_EL_OPLOG = {}  # runbook -> [{op, detail, ts}] 算子调用证据(供调度器读取)


def _log_op(runbook, op, detail=""):
    """记录一次算子调用(供 circuit_schedule 读取"每算子证据")。失败静默。"""
    try:
        rb = os.path.abspath(runbook)
        _EL_OPLOG.setdefault(rb, []).append(
            {"op": str(op), "detail": str(detail)[:120], "ts": int(time.time() * 1000)})
        _EL_OPLOG[rb] = _EL_OPLOG[rb][-50:]
    except Exception:  # noqa: BLE001
        pass


def _policy_path():
    """纵向轴策略文件(跨任务生效; 需显式 apply 才写入——不自我授权)。"""
    return os.path.join(REPO, ".operator_policy.json")


def _policy_load():
    try:
        with open(_policy_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _policy_save(d):
    try:
        with open(_policy_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def tool_circuit_schedule(args):
    """【两轴调度】(借 MetaRSI RSI² Agent): 读执行前缀+信号+算子证据+剩余预算,
    决定"下一步该调哪个算子"(横向, 产出类型化改进程序) 与 "该不该改算子本身"(纵向: 只提议, 需显式 apply)。

    横向程序每步带: operator / purpose / bound_edge / evidence / budget_share;
    整块带 evaluation(判定标准) 与 release_criteria(何时停)。
    可采纳性(admissibility): 无 spec 时禁止 mutate; 同一算子连续≥2 次无进展则禁止再用;
    预算耗尽只允许收口。规划与执行分离——本工具只出程序, 执行仍由 circuit_exec/circuit_evolve 落。
    """
    a = args or {}
    runbook = os.path.abspath(str(a.get("runbook") or "").strip())
    if not runbook or not os.path.isfile(runbook):
        return "error: 需要有效的 runbook 路径", False
    if runbook not in _EL_SESSIONS:
        return f"error: runbook 未 start: {runbook}（先 circuit_exec action=start）", False
    st = _EL_SESSIONS[runbook]
    spec = _EL_SPECS.get(runbook)
    ops = _EL_OPLOG.get(runbook, [])
    rb_spec = None
    el = _exec_loop_module()
    if el is not None:
        try:
            rb_spec = el.load_runbook(runbook)
        except Exception:  # noqa: BLE001
            rb_spec = None
    # ---------------- 采集 signal ----------------
    status = st.get("status")
    it = int(st.get("iter") or 1)
    max_iter = st.get("feedback_max_iter")
    gate = st.get("gate")
    arts = st.get("artifacts") or {}
    ms = st.get("milestones") or {}
    corrections = st.get("corrections") or {}
    steps = (rb_spec or {}).get("steps") or []
    ms_list = (rb_spec or {}).get("milestones") or []
    ms_fail = [k for k, v in ms.items() if v == "fail"]
    ms_pending = [m.get("id") for m in ms_list
                  if not ms.get(m.get("id"))
                  and all(str(s) in arts for s in (m.get("after_steps") or []))]
    op_counts = {}
    for o in ops:
        op_counts[o["op"]] = op_counts.get(o["op"], 0) + 1
    # 连续同算子(无进展信号): 末尾连续相同 op 的次数
    streak_op, streak_n = None, 0
    for o in ops:
        if o["op"] == streak_op:
            streak_n += 1
        else:
            streak_op, streak_n = o["op"], 1
    budget_left = (max_iter - it + 1) if max_iter else None
    signal = {
        "status": status, "iter": it, "max_iter": max_iter, "budget_left": budget_left,
        "gate": gate, "steps_total": len(steps), "steps_done": len(arts),
        "milestones_fail": ms_fail, "milestones_pending": ms_pending,
        "corrections": corrections, "ops_used": op_counts,
        "tail_streak": {"op": streak_op, "n": streak_n},
        "has_spec": spec is not None,
    }
    # ---------------- 可采纳性(转移图约束) ----------------
    admissibility = []
    if spec is None:
        admissibility.append("mutate 不可采纳: 无 spec 缓存(需先 rebuild)")
    if budget_left is not None and budget_left <= 0:
        admissibility.append("预算耗尽: 仅允许收口(report), 禁止再演化/重试")
    if streak_n >= 2:
        admissibility.append(f"算子 {streak_op} 已连续 {streak_n} 次: 本轮禁止再用(需换算子)")
    # ---------------- 横向: 生成类型化改进程序 ----------------
    program = []
    evaluation = "质量门(adc)达标 且 里程碑全 pass"
    release = "done / 预算耗尽 / 连续 2 次同算子无改善则收口"
    if status in ("done", "failed"):
        program = []
        release = "已终态, 无需动作"
    elif budget_left is not None and budget_left <= 0:
        program = [{"step": 1, "operator": "report", "purpose": "预算耗尽收口交付当前最佳",
                    "bound_edge": "-", "evidence": f"iter={it}/{max_iter}",
                    "budget_share": "0(仅收口)"}]
    elif ms_pending:
        program = [{"step": 1, "operator": "milestone_check", "purpose": "到达里程碑, 需轻量校验",
                    "bound_edge": ms_pending[0], "evidence": f"after_steps 已完成",
                    "budget_share": "0(本地校验)"},
                   {"step": 2, "operator": "record_ms", "purpose": "回写里程碑结论(exec)",
                    "bound_edge": ms_pending[0], "evidence": "上一步校验结果",
                    "budget_share": "0"}]
    elif ms_fail and any(corrections.get(m, 0) < 2 for m in ms_fail):
        program = [{"step": 1, "operator": "correct", "purpose": "里程碑 fail → 纠偏重跑上游",
                    "bound_edge": ms_fail[0],
                    "evidence": f"milestone {ms_fail[0]} fail", "budget_share": "1 次纠偏"}]
    elif len(arts) < len(steps):
        program = [{"step": 1, "operator": "execute", "purpose": "按拓扑序完成剩余步骤",
                    "bound_edge": "frontier layer",
                    "evidence": f"已完成 {len(arts)}/{len(steps)} 步",
                    "budget_share": "剩余步数"}]
    elif gate is None:
        program = [{"step": 1, "operator": "gate_check", "purpose": "全部步骤完成, 走质量门自检",
                    "bound_edge": "adc", "evidence": "steps 全完成", "budget_share": "0"},
                   {"step": 2, "operator": "record_gate", "purpose": "回写质量门结论",
                    "bound_edge": "adc", "evidence": "自检结果", "budget_share": "0"}]
    else:  # gate == fail 且预算尚存: 在"重试/换法"之间选, 避免重复同一算子
        cand = []
        if op_counts.get("rebuild", 0) == 0:
            cand.append({"operator": "rebuild", "purpose": "质量门未过 → 换法重编译(改目标/加校验)",
                         "bound_edge": "整链", "evidence": f"gate=fail iter={it}/{max_iter}",
                         "budget_share": "1 次重编译"})
        if spec is not None and op_counts.get("mutate", 0) == 0:
            cand.append({"operator": "mutate", "purpose": "局部加固: 对最弱节点插冗余/升档",
                         "bound_edge": "最弱 resistor", "evidence": "gate=fail",
                         "budget_share": "1 次 auto_heal"})
        if not cand:
            cand.append({"operator": "retry_chain", "purpose": "已换过法仍不过 → 整链重试(exec 自动)",
                         "bound_edge": "整链", "evidence": f"rebuild/mutate 已用尽",
                         "budget_share": "1 轮重试"})
        if streak_n >= 2 and cand and cand[0]["operator"] == streak_op and len(cand) > 1:
            cand = cand[1:]  # 换算子
        program = [dict(c, step=i + 1) for i, c in enumerate(cand)]
    # ---------------- 纵向: 只提议, 需显式 apply ----------------
    vertical = []
    esc_cnt = sum(1 for o in ops if "escalate" in o.get("op", ""))
    if esc_cnt >= 2:
        vertical.append({
            "target": "resistor(反复低质的能力节点)",
            "contract": "默认 tier 升一档(small->tool)",
            "attribution": f"escalate 已用 {esc_cnt} 次(同任务反复加固)",
            "objective": "降低该类节点失败率, 减少运行时返工",
            "callback": "写入 .operator_policy.json, 下次 plan 生效",
            "status": "proposed(需 apply=true 才落盘)",
        })
    applied = []
    if a.get("apply") and vertical:
        pol = _policy_load()
        pol.setdefault("operators", {})
        for v in vertical:
            pol["operators"][v["target"]] = {"contract": v["contract"], "ts": int(time.time() * 1000)}
            applied.append(v["target"])
        _policy_save(pol)
        for v in vertical:
            v["status"] = "applied"
    return json.dumps({
        "signal": signal,
        "admissibility": admissibility,
        "axis": "horizontal",
        "program": program,
        "evaluation": evaluation,
        "release_criteria": release,
        "vertical": vertical,
        "vertical_applied": applied,
        "note": "本工具只产出程序(规划与执行分离); 执行由 circuit_exec / circuit_evolve 落。",
    }, ensure_ascii=False), True


def tool_subconscious(args):
    """潜意识层受限接口。action 决定行为，返回 JSON 文本。离线零依赖；实例不可用则说明。"""
    a = args or {}
    if _SUBCONSCIOUS is None:
        return "error: 潜意识层不可用（SubconsciousLayer 未加载）", False
    action = str(a.get("action") or "query").strip()
    try:
        if action == "feed":
            prompt = str(a.get("prompt") or "").strip()
            if not prompt:
                return "error: feed 需要 prompt", False
            r = _SUBCONSCIOUS.feed(prompt, priority=float(a.get("priority", 0.0)))
            return _sub_json({"fed": True, "seed_len": len(prompt), "focus": prompt,
                              "detail": r}), True
        if action == "start":
            interval = float(a.get("interval", 30.0))
            ok = _SUBCONSCIOUS.start(interval=interval)
            return _sub_json({"started": ok, "running": _SUBCONSCIOUS.is_running(),
                              "interval": _SUBCONSCIOUS.interval}), True
        if action == "stop":
            _SUBCONSCIOUS.stop()
            return _sub_json({"running": _SUBCONSCIOUS.is_running()}), True
        if action == "tick":
            n = int(a.get("n", 1))
            ticks = _SUBCONSCIOUS.run_once(n=n)
            return _sub_json({"ticks": len(ticks)}), True
        if action == "query":
            top_k = int(a.get("top_k", 5))
            min_score = float(a.get("min_score", 0.0))
            # UX: 若后台还没加工出候选(种子刚 feed 或空池), 先自动跑一拍再取,
            # 让"意识层随手取用"一步到位, 免去模型额外 tick。
            try:
                if not _SUBCONSCIOUS.query(top_k=1, min_score=0.0).get("hypotheses"):
                    _SUBCONSCIOUS.run_once(n=3)
            except Exception:  # noqa: BLE001
                pass
            r = _SUBCONSCIOUS.query(top_k=top_k, min_score=min_score)
            return _sub_json(r), True
        if action == "replay":
            n = int(a.get("n", 20))
            return _sub_json({"replay": _SUBCONSCIOUS.replay(n=n)}), True
        if action == "intervene":
            act = str(a.get("act") or "prune").strip()
            hid = a.get("hid")
            score = a.get("score")
            r = _SUBCONSCIOUS.intervene(action=act, hid=hid, score=score)
            return _sub_json({"intervene": act, "result": r}), True
        if action == "state":
            return _sub_json(_SUBCONSCIOUS.snapshot()), True
        return f"error: 未知 action {action}（feed|query|start|stop|tick|replay|intervene|state）", False
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}", False


TOOLS = {
    "selftest": {
        "description": "运行 circuit-agents 内核离线自检（runtime.selftest，无需 key/网络）。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_selftest,
    },
    "simulate": {
        "description": (
            "离线仿真执行电路拓扑(SimBackend, 无需 key/网络): 输入 plan 产出的拓扑(或 examples/*.json / 内联 JSON), "
            "返回 success_rate/cost/latency/quality 汇总。用途: 在真实执行前快速预估方案成功率与成本; "
            "与 plan 搭配——拿到 runbook/拓扑后如需验证可 simulate。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "topology": {"type": "string", "description": "拓扑 JSON 路径（相对 repo 或绝对）或内联 JSON"},
            "runs": {"type": "integer", "description": "仿真次数，默认 20"},
            "seed": {"type": "integer", "description": "随机种子，默认 42"},
        }, "required": ["topology"]},
        "fn": tool_simulate,
    },
    "plan": {
        "description": (
            "【自主决策·复杂任务规划器】把自然语言目标编译成'电路拓扑'(原子步骤/依赖/并行/质量门/反馈环)并产出执行 runbook。"
            "何时应主动使用(无需用户明说'电路'): 任务是多步骤/需拆解、有先后依赖或可并行、要求高可靠/需质量校验、"
            "或你想在动手前先把'做哪些步骤/顺序/重试策略'结构化时。判断信号: 用户任务含多个动作('检索并分析后综述'"
            "'总结并核对数字''对比三个方案后给出建议')、含质量词('务必/高可靠/严格/都要核对')、或含糊大目标需要先拆解。"
            "何时不要用: 单步简单问答、闲聊、翻译一句话、只查一个事实——直接用其他工具即可。"
            "用法: 先 plan 拿到拓扑+runbook; 需要离线估成功率/成本可再 simulate; 实际执行按 runbook 用你的常规工具(读文件/命令/搜索)完成。"
            "返回编译报告与 runbook 路径。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "自然语言目标，如：检索行业数据并分析后综述；总结并核对文档数字"},
            "optimize": {"type": "boolean", "description": "是否跑 M3 Pareto 优化，默认 false"},
            "draw": {"type": "boolean", "description": "是否生成拓扑 SVG，默认 true"},
        }, "required": ["goal"]},
        "fn": tool_plan,
    },
    "circuit_exec": {
        "description": (
            "【治理执行闭环·runbook 驱动器】按电路拓扑 runbook 机械化推进执行(复用 exec_loop 状态机: "
            "里程碑锁相环/分层并行/质量门/整链重试/熔断)。action 决定行为: "
            "start(runbook)初始化并返回首条指令 · next 推进 · record(step,artifact)记录一步产出 · "
            "record_gate(pass)质量门判定 · record_ms(milestone,pass)里程碑 · reset 清空 · status 查状态。"
            "用法: plan 拿到 runbook 路径后 action=start → 循环(读 directive → 按 capability 用你的常规工具真执行 → "
            "record 回写 → 无 step 则 record_gate 判质量门)直到 directive 的 action=done/failed。"
            "为何用: 让'谁先谁后/能否并行/失败重试几次'由状态机裁决而非你临场发挥 → 治理闭环自动化。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "start|reset|next|record|record_gate|record_ms|status"},
            "runbook": {"type": "string", "description": "runbook json 绝对路径"},
            "step": {"type": "string", "description": "record 用：步骤号"},
            "artifact": {"type": "string", "description": "record 用：本步产出摘要/落盘路径"},
            "pass": {"type": "boolean", "description": "record_gate/record_ms 用：是否通过"},
            "milestone": {"type": "string", "description": "record_ms 用：里程碑 id，可空"},
        }, "required": ["action", "runbook"]},
        "fn": tool_circuit_exec,
    },
    "circuit_record_outcome": {
        "description": (
            "【记忆互灌 + 验证门】把一次真实执行的教训/结果回写 circuit-agents 记忆(执行完后调用)。"
            "验证门(Data-RSI 纪律): 只有【已验证】且【治理/约束类】的教训才进长期记忆(会被 plan 自动织入拓扑); "
            "其余落『待验证区』(仅显式 recall 可见, 不污染后续任务)。"
            "参数: goal(任务描述), status(ok/failed/partial), "
            "quality(可选 0-1), evidence(验证依据, 如'质量门 adc>=0.8'/'里程碑 ms_l2 pass'), "
            "verified(可选 bool, 显式声明已验证), lesson_type(governance=治理/约束类[可自动织入] | case=个案现象[默认]), "
            "lessons(教训文本数组), gate_threshold(可选, 默认 0.8)。"
            "判定: verified := verified=true 或 (quality>=gate_threshold 且 有 evidence); 长期=verified 且 governance。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "任务描述"},
            "status": {"type": "string", "description": "ok|failed|partial"},
            "quality": {"type": "number", "description": "可选，执行质量 0-1"},
            "evidence": {"type": "string", "description": "验证依据（质量门/里程碑/人工确认）"},
            "verified": {"type": "boolean", "description": "显式声明已验证"},
            "lesson_type": {"type": "string", "description": "governance|case（默认 case）"},
            "lessons": {"type": "array", "items": {"type": "string"}, "description": "教训文本列表"},
            "gate_threshold": {"type": "number", "description": "可选，验证阈值，默认 0.8"},
        }, "required": ["goal", "status"]},
        "fn": tool_circuit_record_outcome,
    },
    "circuit_evolve": {
        "description": (
            "【拓扑事实驱动演化】执行中现实与静态图不符时, 让 runbook 就地演化, 而非死扛旧图。"
            "两个 action: rebuild(重编译) 与 mutate(就地改)。更新触发可来自: 节点失败/质检不达/agent 发现新事实/用户中途改需求。"
            "rebuild(goal=修正后的目标/约束说明): 重新 plan 出新 spec+runbook, 已完成步骤产出自动续接进新 runbook(不重跑已成功)。"
            "mutate(op=insert|remove|reroute|escalate, pred/succ/cid/old/new): 就地增删改拓扑节点(插新检索/换数据源/降级升档/插冗余分支), 重生 runbook 续接。"
            "返回: new_runbook(新路径, 后续 circuit_exec 用该路径继续). carried_steps(已续接的完成步骤)。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "rebuild|mutate"},
            "runbook": {"type": "string", "description": "当前 runbook 绝对路径(须已 start)"},
            "goal": {"type": "string", "description": "rebuild 用：修正后的目标/约束说明"},
            "op": {"type": "string", "description": "mutate 用：insert|remove|reroute|escalate"},
            "cid": {"type": "string", "description": "mutate 用：目标节点 id"},
            "pred": {"type": "string", "description": "insert 用：前驱节点 id"},
            "succ": {"type": "string", "description": "insert 用：后继节点 id"},
            "old": {"type": "string", "description": "reroute 用：原节点 id"},
            "new": {"type": "string", "description": "reroute 用：新节点 id"},
        }, "required": ["action", "runbook"]},
        "fn": tool_circuit_evolve,
    },
    "circuit_schedule": {
        "description": (
            "【两轴调度·该往哪演化】读当前执行状态(执行前缀+信号+算子证据+剩余预算), "
            "决定下一步该调哪个算子(横向: 输出类型化改进程序 program, 每步带 purpose/bound_edge/evidence/budget_share, "
            "整块带 evaluation 与 release_criteria), 以及该不该改算子本身的默认策略(纵向: vertical 提议, 需 apply=true 才落盘)。"
            "何时用: 里程碑 fail / 质量门 fail / 反复同一算子无进展 / 预算将尽——先问调度器再动手, 避免盲目重试。"
            "可采纳性(admissibility): 无 spec 时禁 mutate; 同一算子连续≥2 次无进展则本轮禁用; 预算耗尽只允许收口。"
            "只产出程序(规划与执行分离), 执行仍由 circuit_exec / circuit_evolve 落。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "runbook": {"type": "string", "description": "当前 runbook 绝对路径（须已 start）"},
            "apply": {"type": "boolean", "description": "可选：是否落盘纵向策略（默认 false 仅提议）"},
        }, "required": ["runbook"]},
        "fn": tool_circuit_schedule,
    },
    "circuit_subconscious": {
        "description": (
            "【潜意识层·后台联想】让常驻后台加工 daemon(系统1/潜意识, 对主上下文不可见)浮出候选假设, "
            "供你在推理时作启发(采纳/证伪皆可, 不强制)。action 决定行为: "
            "feed(prompt=任务/关键词, 让后台开始加工) · query(top_k)取策展后的 top-K 候选(意识层受限取用) · "
            "start/stop/tick 控制常驻 · intervene(act=prune|boost|promote|clear, 人类监督) · replay 回放 · state 快照。"
            "何时用: 复杂推理/多选题/需另辟蹊径看类比时, 先 feed 当前任务再 query 拿候选假设当额外视角; "
            "简单任务不必用。全程离线零依赖, 无 key 可用。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "feed|query|start|stop|tick|replay|intervene|state"},
            "prompt": {"type": "string", "description": "feed 用：任务/种子关键词"},
            "top_k": {"type": "integer", "description": "query 用：返回候选数，默认 5"},
            "min_score": {"type": "number", "description": "query 用：最低分，默认 0"},
            "interval": {"type": "number", "description": "start 用：tick 间隔秒，默认 30"},
            "n": {"type": "integer", "description": "tick/replay 用：次数"},
            "act": {"type": "string", "description": "intervene 用：prune|boost|promote|clear"},
            "hid": {"type": "string", "description": "intervene 用：候选 id"},
            "score": {"type": "number", "description": "intervene 用：boost 分数"},
        }, "required": ["action"]},
        "fn": tool_subconscious,
    },
}


def make_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


_CALL_LOG = []  # 最近工具调用 [(tool, args-key)], 供退化检测(CVM 轻量版)


def _degradation_advisory(name, args):
    """【认知护栏·退化检测】(借天枢 CVM Layer 4 的最小可落地版, 只提示不阻断)。

    检测三类退化信号并在工具返回末尾追加 advisory:
      · 因果坍缩: 同一 (tool, args) 连续重复 ≥3 次
      · 无进展重试: runbook 质量门 fail 仍在重复调用
      · 预算将尽: iter 达 feedback.max_iter
    返回 '' 表示无信号(零回归)。
    """
    try:
        key = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)[:200]
        _CALL_LOG.append((str(name), key))
        del _CALL_LOG[:-12]
        adv = []
        n = 0
        for t, k in reversed(_CALL_LOG):
            if t == str(name) and k == key:
                n += 1
            else:
                break
        if n >= 3:
            adv.append(f"⚠ 因果坍缩信号：同一调用({name}) 连续重复 {n} 次 → "
                       f"建议换路/换算子(先问 circuit_schedule) 或 circuit_evolve 演化拓扑")
        rb = str((args or {}).get("runbook") or "").strip()
        if rb:
            st = _EL_SESSIONS.get(os.path.abspath(rb))
            if isinstance(st, dict):
                it = int(st.get("iter") or 1)
                mx = st.get("feedback_max_iter")
                if st.get("gate") == "fail":
                    adv.append("⚠ 质量门未过：建议先用 circuit_schedule 决定换算子(rebuild/mutate)，"
                               "而非重复重试同一路径")
                if mx and it >= mx:
                    adv.append(f"⚠ 预算将尽({it}/{mx})：建议收口交付当前最佳，或显式申请加预算")
        return ("\n\n" + "\n".join(adv)) if adv else ""
    except Exception:  # noqa: BLE001
        return ""


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        tools = [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                 for n, t in TOOLS.items()]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool:
            return make_error(mid, -32602, "未知工具: %s" % name)
        try:
            out, ok = tool["fn"](args)
            # 认知护栏: 退化信号只提示不阻断(零回归: 无信号时 out 不变)
            try:
                _adv = _degradation_advisory(name, args)
                if _adv and isinstance(out, str):
                    out = out + _adv
            except Exception:  # noqa: BLE001
                pass
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": out}],
                "isError": not ok,
            }}
        except Exception as exc:  # noqa: BLE001
            return make_error(mid, -32603, "%s: %s" % (type(exc).__name__, exc))
    return make_error(mid, -32601, "未知方法: %s" % method)


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print(json.dumps(make_error(None, -32700, "parse error"), ensure_ascii=False))
            sys.stdout.flush()
            continue
        if not isinstance(msg, dict):
            continue
        resp = handle(msg)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
