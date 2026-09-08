#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat_agent — circuit-agents 桌面端「工具型智能体」连接层 v3。

· 资源自动选档：deepseek/openai（key：env > 桌面 key_tmp.txt）/ local / offline。
· agent_chat：DeepSeek function-calling 工具循环，≤ MAX_TOOL_STEPS 步；
  run_command 由 agent_tools 进审批队列 → 循环暂停并返回 needApproval；
  approve(id, allow) 批准/拒绝后续跑；高危命令由 agent_tools 直接阻止。
· 失败回退普通 chat()。
key 约定沿用 compiler/backend_llm.py；绝不把 key 打进回复/日志/命令/工具参数。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

SYSTEM_PROMPT = (
    "你是 circuit-agents 桌面端里的智能体助手（能力对齐 DeepSeek Harness 助手）。"
    "中文简洁回答。你能：读写工作区文件、运行 PowerShell 命令（需用户批准）、联网搜索、"
    "把任务编译成电路拓扑规划/执行。需要动手时先用工具查证再回答，不要臆造文件内容或命令结果。"
    "诚实边界：不确定就说不确定；删除/危险操作要提前说明。run_command 会请求批准——批准前它不会执行。"
)

KEY_FILE_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "key_tmp.txt")
MAX_TOOL_STEPS = 8
_LOOPS = {}
_LOOP_LOCK = threading.Lock()
_LOOP_TTL = 3600.0


def _loop_cleanup():
    now = time.time()
    with _LOOP_LOCK:
        for k in [k for k, v in _LOOPS.items() if now - v.get("ts", 0) > _LOOP_TTL]:
            _LOOPS.pop(k, None)


def resolve_key_info():
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AGENT_API_KEY"):
        v = os.environ.get(env)
        if v:
            return v.strip().lstrip("\ufeff"), env
    try:
        with open(KEY_FILE_PATH, "r", encoding="utf-8") as f:
            k = f.read().strip().lstrip("\ufeff")
        if k:
            return k, "key_tmp.txt"
    except OSError:
        pass
    return "", None


def _pick_base(source, local_hint):
    if local_hint:
        return local_hint
    for e in ("AGENT_API_BASE", "CA_LLM_BASE"):
        b = os.environ.get(e)
        if b:
            return b.strip().rstrip("/")
    if source == "OPENAI_API_KEY":
        return "https://api.openai.com/v1"
    return "https://api.deepseek.com"


def detect_mode():
    key, source = resolve_key_info()
    local_hint = (os.environ.get("CA_CHAT_BASE") or "").strip().rstrip("/")
    base = _pick_base(source, local_hint)
    if key:
        model = os.environ.get("CA_CHAT_MODEL") or (
            "gpt-4o-mini" if source == "OPENAI_API_KEY" else "deepseek-chat")
        return {"mode": "openai" if source == "OPENAI_API_KEY" else "deepseek",
                "model": model, "base": base, "key_source": source, "online": True}
    if local_hint or os.environ.get("AGENT_API_BASE") or os.environ.get("CA_LLM_BASE"):
        model = (os.environ.get("CA_CHAT_MODEL") or os.environ.get("CA_LLM_MODEL")
                 or "local-model")
        return {"mode": "local", "model": model, "base": base,
                "key_source": None, "online": True}
    return {"mode": "offline", "model": None, "base": None,
            "key_source": None, "online": False}


def _post_json(url, payload, key, timeout=180.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)


def _trim(messages, max_history=14, max_chars=16000):
    out = []
    budget = 0
    for m in list(messages)[-max_history:]:
        c = (m.get("content") or "")[:3000]
        budget += len(c) + 80
        if budget > max_chars:
            break
        out.append({"role": "user" if m.get("role") != "assistant" else "assistant",
                    "content": c})
    return out


def _at():
    import agent_tools
    return agent_tools


def _offline_reply(messages):
    t = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            t = (m.get("content") or "").lower()
            break
    if any(k in t for k in ("你好", "hi", "hello", "在吗")):
        return ("你好！我在（离线模式）。接入真模型后我能像 DeepSeek 助手一样：读写工作区文件、"
                "运行命令（需你批准）、联网搜索、把任务编译成电路拓扑。把 DeepSeek key 放到桌面 "
                "key_tmp.txt（或设 DEEPSEEK_API_KEY）后重启应用即可；离线时电路规划与仿真仍可用。")
    if any(k in t for k in ("谢谢", "感谢", "thanks")):
        return "不客气！放好 key 后就能让我真正动手干活了。"
    return ("当前是离线模式（未检测到 API key 或本地端点），只能做确定性的电路规划/仿真，"
            "不能调用文件/命令/搜索等智能工具。启用：桌面 key_tmp.txt 放 DeepSeek key，"
            "或设 DEEPSEEK_API_KEY / CA_CHAT_BASE，然后重启。\n试试对我说“你好”或"
            "“规划：检索行业数据并分析后综述”。")


def _error_reply(ex):
    return "调用模型出错了（%s）。可稍后重试，或检查 key/端点配置。" % str(ex)[:200]


def chat(messages, max_history=12, max_chars=12000):
    """无工具普通对话（本地模型/兜底用）。"""
    meta0 = detect_mode()
    if not meta0["online"]:
        return {"text": _offline_reply(messages), "meta": meta0}
    payload = {"model": meta0["model"],
               "messages": [{"role": "system", "content": SYSTEM_PROMPT}]
               + _trim(messages, max_history, max_chars),
               "stream": False, "temperature": 0.7, "max_tokens": 1600}
    try:
        key, _src = resolve_key_info()
        resp = _post_json(meta0["base"] + "/chat/completions", payload, key=key)
        try:
            text = resp["choices"][0]["message"]["content"]
        except Exception:
            text = json.dumps(resp, ensure_ascii=False)[:4000]
        return {"text": (text or "").strip(), "meta": dict(meta0, ok=True)}
    except Exception as ex:
        return {"text": _error_reply(ex), "meta": dict(meta0, ok=False, error=str(ex)[:300])}


def _finish_limit(state):
    return {"text": "（已达工具步数上限 %d，先按已有结果收尾）" % state["max_steps"],
            "meta": dict(state["meta0"], ok=True, tool_steps=state["steps"]),
            "tools": state["log"]}


def _loop(state):
    """驱动工具循环；run_command 需批准时存档并返回 needApproval。"""
    at = _at()
    meta0, key = state["meta0"], state["key"]
    url = state["base"].rstrip("/") + "/chat/completions"
    while True:
        calls = state.get("pcalls")
        idx = state.get("pidx", 0)
        if calls is None:
            if state["steps"] >= state["max_steps"]:
                return _finish_limit(state)
            payload = {"model": meta0["model"], "messages": state["msgs"],
                       "tools": at.TOOLS, "tool_choice": "auto", "stream": False,
                       "temperature": 0.7, "max_tokens": 2000}
            resp = _post_json(url, payload, key=key)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"text": ((msg.get("content") or "")
                                 or json.dumps(resp, ensure_ascii=False)[:4000]).strip(),
                        "meta": dict(meta0, ok=True, tool_steps=state["steps"]),
                        "tools": state["log"]}
            state["msgs"].append({"role": "assistant",
                                  "content": msg.get("content") or "",
                                  "tool_calls": calls})
        while idx < len(calls):
            if state["steps"] >= state["max_steps"]:
                state["pcalls"] = None
                return _finish_limit(state)
            call = calls[idx]
            idx += 1
            state["steps"] += 1
            fn = (call.get("function") or {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            res = at.run(name, args) if name else {"error": "空工具名"}
            if isinstance(res, dict) and res.get("status") == "need_approval":
                state["pcalls"] = calls
                state["pidx"] = idx
                aid = res.get("approval_id")
                _loop_cleanup()
                with _LOOP_LOCK:
                    _LOOPS[aid] = {"state": state, "ts": time.time()}
                return {"needApproval": {"id": aid,
                                         "command": res.get("command", ""),
                                         "desc": res.get("desc", "")},
                        "meta": dict(meta0, ok=True, tool_steps=state["steps"]),
                        "tools": state["log"]}
            out = json.dumps(res, ensure_ascii=False)[:6000]
            state["msgs"].append({"role": "tool",
                                  "tool_call_id": call.get("id", ""),
                                  "content": out})
            state["log"].append({"name": name, "args": args,
                                 "ok": not res.get("error"),
                                 "out": out[:180]})
        state["pcalls"] = None
        state["pidx"] = 0


def agent_chat(messages, max_steps=None):
    """在线工具智能体入口；可能返回 needApproval 暂停。"""
    meta0 = detect_mode()
    if not meta0["online"]:
        return {"text": _offline_reply(messages), "meta": meta0, "tools": []}
    try:
        _at().TOOLS
    except Exception as e:
        return {"text": "（agent_tools 未加载：%s）" % e,
                "meta": dict(meta0, ok=False), "tools": []}
    key, _src = resolve_key_info()
    state = {"msgs": [{"role": "system", "content": SYSTEM_PROMPT}] + _trim(messages),
             "model": meta0["model"], "base": meta0["base"], "key": key,
             "meta0": meta0, "steps": 0,
             "max_steps": max_steps or MAX_TOOL_STEPS,
             "log": [], "pcalls": None, "pidx": 0}
    try:
        return _loop(state)
    except Exception as ex:
        fb = chat(messages)
        fb["tools"] = state["log"]
        fb["meta"] = dict(fb.get("meta", {}), tools_error=str(ex)[:200])
        return fb


def approve(approval_id: str, allow: bool):
    """用户对 run_command 的批准/拒绝；随后续跑循环。返回与 agent_chat 相同形状。"""
    _loop_cleanup()
    with _LOOP_LOCK:
        ent = _LOOPS.pop(approval_id, None)
    if ent is None:
        return {"error": "审批不存在或已过期", "meta": {"online": False}, "tools": []}
    state = ent["state"]
    at = _at()
    res = at.decide(approval_id, allow)
    if isinstance(res, dict) and res.get("denied"):
        outcome = {"denied": True, "message": "用户拒绝了该命令，未执行。请改用只读命令或向用户说明。"}
    else:
        outcome = res
    st = state
    st["msgs"].append({"role": "tool", "tool_call_id": "", "content":
                       json.dumps(outcome, ensure_ascii=False)[:6000]})
    st["log"].append({"name": "run_command",
                      "args": {"command": outcome.get("command", "")},
                      "ok": not (isinstance(outcome, dict)
                                 and (outcome.get("error") or outcome.get("denied"))),
                      "out": json.dumps(outcome, ensure_ascii=False)[:180]})
    try:
        return _loop(st)
    except Exception as ex:
        return {"text": _error_reply(ex),
                "meta": dict(st["meta0"], ok=False, tools_error=str(ex)[:200]),
                "tools": st["log"]}


if __name__ == "__main__":
    print(json.dumps(detect_mode(), ensure_ascii=False))

def chat_stream(messages, max_history=12, max_chars=12000):
    """普通对话 SSE 生成器：yield {"type":"delta","text":...} / {"type":"done","text":...} / {"type":"error",...}。"""
    import urllib.error
    meta0 = detect_mode()
    if not meta0["online"]:
        yield {"type": "done", "text": _offline_reply(messages)}
        return
    payload = {"model": meta0["model"],
               "messages": [{"role": "system", "content": SYSTEM_PROMPT}]
               + _trim(messages, max_history, max_chars),
               "stream": True, "temperature": 0.7, "max_tokens": 1600}
    url = meta0["base"].rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    key, _src = resolve_key_info()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            buf = ""
            while True:
                chunk = r.read(1024)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        continue
                    try:
                        ev = json.loads(d)
                    except Exception:
                        continue
                    try:
                        delta = ev["choices"][0]["delta"].get("content")
                    except Exception:
                        delta = None
                    if delta:
                        yield {"type": "delta", "text": delta}
    except Exception as ex:
        yield {"type": "error", "message": str(ex)[:300]}
    else:
        yield {"type": "done", "text": ""}
