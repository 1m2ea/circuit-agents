# -*- coding: utf-8 -*-
"""tool_circuit_evolve 修复验证（rebuild / mutate escalate / mutate insert /
无缓存 spec 的 mutate / node 名优先续接）。逐项断言，失败会输出 FAIL 并 exit 1。"""
import os
import sys
import json
import importlib.util

os.environ["CIRCUIT_AGENTS_REPO"] = r"D:/dev/projects/666/circuit-harness/engine"
os.environ["CIRCUIT_PLANNER_PY"] = r"D:/dev/projects/666/circuit-harness/home/skills/circuit-planner/scripts/plan.py"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ENG = r"D:/dev/projects/666/circuit-harness/engine"
if ENG not in sys.path:
    sys.path.insert(0, ENG)

MCP = r"D:/dev/projects/666/circuit-harness/engine/mcp/mcp_server.py"
spec = importlib.util.spec_from_file_location("ca_mcp_test", MCP)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

RB = r"D:/dev/projects/666/circuit-harness/home/skills/circuit-planner/scripts/runbooks/readme_verify_runbook.json"
fail = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fail.append(msg)


def call(fn, args, tag):
    out, ok = fn(args)
    print(f"---- {tag}  ok={ok}")
    if ok:
        try:
            print("      ->", json.dumps(json.loads(out), ensure_ascii=False)[:500])
        except Exception:
            print("      ->", out[:500])
    else:
        print("      -> ERROR:", out[:500])
    return out, ok


def load_rb(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# 0) start（只加载 runbook，此时 _EL_SPECS 里没有 spec —— 用于 bug2 路径）
out, ok = call(m.tool_circuit_exec, {"action": "start", "runbook": RB}, "circuit_exec start")
check(ok, "start 原始 runbook 成功")

# 记录一个已完成步骤，供 rebuild 续接
out, ok = call(m.tool_circuit_exec, {"action": "record", "runbook": RB, "step": "1",
                                     "artifact": "步骤1产出：已读取 README 全文"}, "record step1")
check(ok, "record 步骤1 产出成功")

# 1) rebuild -> 应 ok=True + carried_steps 非空
out, ok = call(m.tool_circuit_evolve, {"action": "rebuild", "runbook": RB,
                                       "goal": "总结项目文档并核对数字"}, "evolve rebuild")
rb1 = None
if ok:
    r = json.loads(out)
    rb1 = r["new_runbook"]
    check(len(r.get("carried_steps", [])) > 0,
          f"rebuild 续接 carried_steps 非空 -> {r.get('carried_steps')}")
    check(rb1 in m._EL_SPECS, "rebuild 后真实 spec 已缓存进 _EL_SPECS")

# 2) mutate escalate（真实缓存 spec）-> ok=True + 冗余分支
node = None
if rb1:
    rb1j = load_rb(rb1)
    pick = next((s for s in rb1j["steps"]
                 if str(s.get("capability", "")).split("#")[0] in ("retrieve", "extract")), None)
    node = (pick or rb1j["steps"][0])["node"]
    print(f"escalate 目标 node = {node}")
rb2 = None
if node:
    out, ok = call(m.tool_circuit_evolve, {"action": "mutate", "op": "escalate",
                                           "runbook": rb1, "cid": node}, f"mutate escalate {node}")
    if ok:
        r = json.loads(out)
        rb2 = r["new_runbook"]
        check(r.get("spec_source") == "cached_spec", "escalate 用缓存真实 spec")
        check(bool(r.get("heal")), f"escalate 有 heal 报告 -> {r.get('heal')}")
        check(len(r.get("carried_steps", [])) > 0, "escalate 续接非空（不重跑已完成）")
        rb2_spec = m._EL_SPECS.get(rb2)
        comps2 = (rb2_spec or {}).get("components", {}) or {}
        rb2j = load_rb(rb2)
        check(node + "__redundant" in comps2 or any(s.get("node") == node + "__redundant" for s in rb2j["steps"]),
              "拓扑/runbook 含该节点的冗余分支")

# 3) mutate insert（pred=src）-> ok=True + 新 runbook 含 r_extra
if rb2:
    out, ok = call(m.tool_circuit_evolve,
                   {"action": "mutate", "op": "insert", "runbook": rb2, "cid": "r_extra",
                    "pred": "src",
                    "comp": {"type": "resistor", "label": "r_extra", "model": "tool"}},
                   "mutate insert r_extra (pred=src)")
    if ok:
        r3 = json.loads(out)
        rb3j = load_rb(r3["new_runbook"])
        check(any(s.get("node") == "r_extra" for s in rb3j["steps"]),
              "insert 后新 runbook 含 r_extra 步骤")

# 4) bug2：再次 start（仍无 spec）直接 mutate escalate -> 不再崩/不再空壳
out, ok = call(m.tool_circuit_exec, {"action": "start", "runbook": RB}, "circuit_exec start #2")
orig_steps = load_rb(RB)["steps"]
n0 = next((s for s in orig_steps if str(s.get("capability", "")).split("#")[0] in ("retrieve", "extract")),
          orig_steps[0])["node"]
out, ok = call(m.tool_circuit_evolve, {"action": "mutate", "op": "escalate",
                                       "runbook": RB, "cid": n0}, "无缓存spec直接 escalate")
if ok:
    r = json.loads(out)
    check(r.get("spec_source") == "runbook_synthesized",
          f"无缓存 spec 时标注 runbook_synthesized -> {r.get('spec_source')}")
    check(bool(r.get("heal")), "无缓存 spec 时 escalate 也产出冗余分支")
    rb4 = load_rb(r["new_runbook"])
    check(len(rb4["steps"]) > len(orig_steps),
          f"步骤数增加（原 {len(orig_steps)} -> {len(rb4['steps'])}）")

print()
if fail:
    print(f"DONE FAILED ({len(fail)} 项失败)")
    sys.exit(1)
print("DONE ALL-OK")
