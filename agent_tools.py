#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_tools — 桌面端「工具型智能体」本地工具层 v2（纯标准库）。

工具（OpenAI function-calling schema）：
  list_dir / read_file / write_file   —— 受限文件操作（默认根 CA_AGENT_ROOT，默认 D:\\dev\\projects）
  run_command                         —— PowerShell 执行：默认进「审批队列」，由用户允许后才真执行；
                                         高危命令（删除/格式化/关机/强杀进程等）直接阻止。
  web_search                          —— Bing RSS 网页搜索（无需 key）
  circuit_plan                        —— 自然语言 → 电路拓扑规划摘要（离线确定）

安全约定：
  · 文件操作只允许落在工作区内（CA_AGENT_ROOT，可改）；根外路径一律拒绝。
  · run_command 一律需要审批：run() 返回 need_approval，用户 allow 后经 decide() 执行。
  · 高危命令即使 allow 也阻止（auto-block）。
  · 绝不把 API key 传进任何工具。
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

AGENT_ROOT = os.path.abspath(os.environ.get("CA_AGENT_ROOT") or r"D:\dev\projects")
_READ_CAP = 60000
_RUN_TIMEOUT = 120.0
_SEARCH_HITS = 6
_SNIP = 300
_PENDING_TTL = 1800.0   # 审批项 30 分钟过期

# ---------------- 审批队列 ----------------
_PENDING = {}            # id -> {"cmd", "ts", "desc"}


def _new_id():
    return "apv%d%s" % (int(time.time() * 1000) % 10 ** 12,
                        os.urandom(3).hex())


def _prune_pending():
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v.get("ts", 0) > _PENDING_TTL]:
        _PENDING.pop(k, None)


def pending_info(id_):
    return _PENDING.get(id_)


def decide(id_, allow: bool):
    """审批决定：allow=True 执行该命令；否则拒绝。返回工具结果 dict。"""
    _prune_pending()
    item = _PENDING.pop(id_, None)
    if item is None:
        return {"error": "审批不存在或已过期"}
    if not allow:
        return {"denied": True, "command": item["cmd"]}
    try:
        return _run_command_impl(item["cmd"])
    except Exception as e:  # pragma: no cover
        return {"error": "%s: %s" % (type(e).__name__, e)}


_RISKY_PATTERNS = [
    (r"(?i)\brm\s+(-rf|-fr|-r\s+-f)\b", "递归删除"),
    (r"(?i)\b(?:del|erase)\b[^\n]*/\s*s", "目录删除"),
    (r"(?i)\bremove-item\b", "删除类命令"),
    (r"(?i)\brmdir\b[^\n]*/\s*s", "递归删除目录"),
    (r"(?i)\bformat\s+[a-z]:", "格式化磁盘"),
    (r"(?i)\bdiskpart\b", "磁盘分区操作"),
    (r"(?i)\bshutdown\b", "关机/重启"),
    (r"(?i)\breg\s+delete\b", "删除注册表项"),
    (r"(?i)\btaskkill\b[^\n]*/\s*f\b", "强制结束进程"),
    (r"(?i)\bclear-recyclebin\b", "清空回收站"),
    (r"(?i)\bRemove-Item\b[^\n]*(-Recurse|-Force)", "强制删除"),
    (r"(?i)\bStart-BitsTransfer\b", "下载执行类"),
]


def risky_reason(cmd: str):
    for pat, why in _RISKY_PATTERNS:
        if re.search(pat, cmd):
            return why
    return None


def status():
    return {"root": AGENT_ROOT, "tools": [t["function"]["name"] for t in TOOLS]}


def _dec(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except Exception:
        pass
    try:
        return b.decode("gb18030")
    except Exception:
        return b.decode("utf-8", "replace")


def _resolve(p: str):
    raw = os.path.expanduser(str(p or "").strip())
    # 根目录的各种等价写法（"" / "." / "/" / "\\"）都映射到工作区根
    if raw in ("", ".", "/", "\\"):
        return AGENT_ROOT
    if not raw:
        return None
    ap = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(AGENT_ROOT, raw))
    apn = os.path.normcase(ap)
    rootn = os.path.normcase(AGENT_ROOT)
    if apn != rootn and not apn.startswith(rootn + os.sep):
        return None
    return ap


# ---------------- 工具实现 ----------------
def _list_dir(p: str):
    d = _resolve(p)
    if d is None:
        return {"error": "路径不在工作区内：" + str(p)}
    if not os.path.isdir(d):
        return {"error": "不是目录：" + str(p)}
    try:
        items = sorted(os.listdir(d))
    except OSError as e:
        return {"error": str(e)}
    out = []
    for it in items[:400]:
        full = os.path.join(d, it)
        try:
            kind = "dir" if os.path.isdir(full) else "file"
        except OSError:
            kind = "?"
        out.append({"name": it, "kind": kind})
    rel = os.path.relpath(d, AGENT_ROOT)
    return {"path": ("/" + rel.replace("\\", "/")) if rel != "." else "/",
            "count": len(items), "items": out}


def _read_file(p: str):
    f = _resolve(p)
    if f is None:
        return {"error": "路径不在工作区内：" + str(p)}
    if not os.path.isfile(f):
        return {"error": "不是文件：" + str(p)}
    try:
        with open(f, "rb") as fh:
            data = fh.read(_READ_CAP + 1)
    except OSError as e:
        return {"error": str(e)}
    if len(data) > _READ_CAP:
        return {"error": "文件超过 %d 字符，请缩小范围（当前仅支持文本预览）" % _READ_CAP}
    return {"path": os.path.relpath(f, AGENT_ROOT), "content": _dec(data)}


def _write_file(p: str, content: str):
    f = _resolve(p)
    if f is None:
        return {"error": "路径不在工作区内：" + str(p)}
    try:
        os.makedirs(os.path.dirname(f) or ".", exist_ok=True)
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(str(content or ""))
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "path": os.path.relpath(f, AGENT_ROOT),
            "chars": len(str(content or ""))}


def _run_command_impl(cmd: str):
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            cwd=AGENT_ROOT, capture_output=True, timeout=_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "命令超时（%ss）" % _RUN_TIMEOUT}
    except OSError as e:
        return {"error": str(e)}
    out = _dec(proc.stdout) + (("\n[stderr] " + _dec(proc.stderr)) if proc.stderr else "")
    if len(out) > 50000:
        out = out[:50000] + "\n…[输出过长已截断]"
    return {"exit_code": proc.returncode, "output": out}


def _web_search(q: str):
    q = str(q or "").strip()
    if not q:
        return {"error": "query 为空"}
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(q)
           + "&format=rss&count=" + str(_SEARCH_HITS))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"error": "搜索失败：" + str(e)[:200]}
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        def _g(tag):
            mm = re.search("<" + tag + r">(.*?)</" + tag + ">", m.group(1), re.S)
            if not mm:
                return ""
            return _html.unescape(re.sub(r"<[^>]+>", "", mm.group(1))).strip()
        title, link, desc = _g("title"), _g("link"), _g("description")
        if title:
            items.append({"title": title[:_SNIP], "link": link, "snippet": desc[:_SNIP]})
        if len(items) >= _SEARCH_HITS:
            break
    return {"query": q, "results": items}


def _circuit_plan(goal: str):
    try:
        from compiler.nl_parser import GoalParser
        from compiler.compile import compile_goal
        spec = compile_goal(GoalParser().parse(str(goal or "").strip()),
                            auto_bind=True, route=True, memory_enabled=True,
                            auto_select_models=False)
    except Exception as e:
        return {"error": "编译失败：" + str(e)[:300]}
    comps = spec.get("components") or {}
    by_type = {}
    steps = []
    for cid, c in comps.items():
        if not isinstance(c, dict):
            continue
        by_type[c.get("type", "?")] = by_type.get(c.get("type", "?"), 0) + 1
        steps.append({"id": cid, "type": c.get("type", "?"),
                      "capability": c.get("capability") or c.get("label") or ""})
    return {"name": spec.get("name", ""), "components_total": len(comps),
            "by_type": by_type, "steps": steps[:60]}


# ---------------- 注册表 ----------------
TOOLS = [
    {"type": "function", "function": {"name": "list_dir",
        "description": "列出工作区内某个目录的内容（dir 可省/写 . 或 / 表示工作区根）",
        "parameters": {"type": "object", "properties": {"dir": {"type": "string",
            "description": "目录路径（相对工作区或绝对，根目录用 . 或 /）"}}, "required": ["dir"]}}},
    {"type": "function", "function": {"name": "read_file",
        "description": "读取工作区内文本文件内容（最多 6 万字符）",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file",
        "description": "写/覆盖工作区内文件（UTF-8；仅工作区内）",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "run_command",
        "description": "在 Windows PowerShell 执行命令（真实运行）。该工具会请求用户批准后才执行；高危命令会被阻止。请先用安全只读命令探查，不要编造输出。",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "web_search",
        "description": "联网搜索网页，返回标题/链接/摘要（无需 key）",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "circuit_plan",
        "description": "把自然语言任务编译成电路拓扑并返回规划摘要（离线、快）",
        "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}}},
]

_EXEC = {
    "list_dir": lambda a: _list_dir(a.get("dir", ".")),
    "read_file": lambda a: _read_file(a.get("path", "")),
    "write_file": lambda a: _write_file(a.get("path", ""), a.get("content", "")),
    "web_search": lambda a: _web_search(a.get("query", "")),
    "circuit_plan": lambda a: _circuit_plan(a.get("goal", "")),
}


def run(name: str, args: dict):
    """统一入口：run_command 走审批门；其余直接执行。"""
    if name == "run_command":
        cmd = str((args or {}).get("command", "")).strip()
        if not cmd:
            return {"error": "命令为空"}
        why = risky_reason(cmd)
        if why:
            return {"blocked": True, "reason": why,
                    "error": "该命令属于高危操作（%s），已被阻止，未执行。" % why}
        _prune_pending()
        pid = _new_id()
        _PENDING[pid] = {"cmd": cmd, "ts": time.time(),
                         "desc": cmd[:200]}
        return {"status": "need_approval", "approval_id": pid,
                "command": cmd, "desc": "PowerShell 执行：" + cmd[:200]}
    fn = _EXEC.get(name)
    if fn is None:
        return {"error": "未知工具：" + str(name)}
    try:
        return fn(args or {})
    except Exception as e:  # pragma: no cover
        return {"error": "%s: %s" % (type(e).__name__, e)}


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False))
    print(json.dumps(run("run_command", {"command": "echo hi"}), ensure_ascii=False)[:300])
