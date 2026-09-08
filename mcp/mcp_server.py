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
import subprocess
import sys

SERVER_NAME = "circuit-agents-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2024-11-05"

REPO = os.environ.get("CIRCUIT_AGENTS_REPO") or r"D:\dev\projects\666\circuit-agents"
PY = os.environ.get("CIRCUIT_AGENTS_PYTHON") or sys.executable


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


def _run(cmd, cwd=None, timeout=600):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
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
    return out, ok


TOOLS = {
    "selftest": {
        "description": "运行 circuit-agents 内核离线自检（runtime.selftest，无需 key/网络）。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_selftest,
    },
    "simulate": {
        "description": "用 SimBackend 离线仿真执行电路拓扑：传 examples/*.json 相对路径或内联拓扑 JSON 字符串，返回 success_rate/cost/latency/quality 汇总。",
        "inputSchema": {"type": "object", "properties": {
            "topology": {"type": "string", "description": "拓扑 JSON 路径（相对 repo 或绝对）或内联 JSON"},
            "runs": {"type": "integer", "description": "仿真次数，默认 20"},
            "seed": {"type": "integer", "description": "随机种子，默认 42"},
        }, "required": ["topology"]},
        "fn": tool_simulate,
    },
    "plan": {
        "description": "把自然语言目标规划成电路拓扑并产出执行 runbook（离线规则，无需 key）。返回编译报告与 runbook 路径。",
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "自然语言目标，如：检索行业数据并分析后综述"},
            "optimize": {"type": "boolean", "description": "是否跑 M3 Pareto 优化，默认 false"},
            "draw": {"type": "boolean", "description": "是否生成拓扑 SVG，默认 true"},
        }, "required": ["goal"]},
        "fn": tool_plan,
    },
}


def make_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


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
