#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
circuit-agents 桌面壳 (PyWebView)
=================================
把「FastAPI 后端 + 拓扑编辑器网页」打包成一个原生桌面程序：
  · 程序内启动 server.py (uvicorn, 127.0.0.1:8765)
  · 等端口就绪后，用系统 WebView2 原生窗口加载最简的 Live Console（/，输入任务即可用）
  · 关闭窗口即停后端，单实例、零浏览器标签、无黑框控制台

用法：
  circuit-agents.exe            # 正常启动：原生窗口
  circuit-agents.exe --selftest # 无界面自检：启动后端 + 探测关键端点，验证打包后模块齐全
"""
from __future__ import annotations

import os
import sys
import time
import threading
import json
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
PORT = int(os.environ.get("CA_PORT", "8765"))
BASE = f"http://{HOST}:{PORT}"
EDITOR_URL = f"{BASE}/"  # 默认开最简 Live Console（新手友好；高级用户在页面内切「完整」模式或访问 /topology/editor）

_uv = None  # uvicorn.Server 实例


def _start_server():
    """在后台线程里跑 FastAPI（与 webview 同进程，便于单实例/统一生命周期）。"""
    global _uv
    import uvicorn
    import server  # 同源目录，打包后位于 MEI 临时目录
    cfg = uvicorn.Config(server.app, host=HOST, port=PORT, log_level="warning")
    _uv = uvicorn.Server(cfg)
    _uv.run()


def _wait_ready(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/", timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _http(method: str, path: str, data=None, timeout: float = 10.0):
    url = BASE + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw


def _stop_server():
    global _uv
    if _uv is not None:
        _uv.should_exit = True
    time.sleep(0.4)


def _selftest() -> int:
    """无界面自检：验证打包后后端关键链路（资源/协作者池/运行时导入/DSL 解析）齐全。"""
    fails = []

    # 1) 编辑器页面（静态资源随 exe 打包）
    try:
        st, body = _http("GET", "/topology/editor")
        ok = st == 200 and isinstance(body, str) and "<html" in body.lower()
        print(("PASS" if ok else "FAIL"), "editor 页面", st)
        if not ok:
            fails.append("editor 页面")
    except Exception as e:
        print("FAIL editor 页面", repr(e))
        fails.append("editor 页面")

    # 2) 协作者池（人机协同·极致 的 CollaboratorRegistry 导入链）
    try:
        st, body = _http("GET", "/collaborators")
        ok = st == 200 and isinstance(body, (list, dict))
        print(("PASS" if ok else "FAIL"), "协作者池", st)
        if not ok:
            fails.append("协作者池")
    except Exception as e:
        print("FAIL 协作者池", repr(e))
        fails.append("协作者池")

    # 3) 新建会话（runtime.Circuit + CircuitExecutor + SimBackend + DSL 解析导入链）
    spec = {
        "name": "dt",
        "components": {
            "src": {"type": "power", "label": "src"},
            "r1": {"type": "resistor", "label": "r1"},
        },
        "wires": [["src", "r1"]],
    }
    sid = None
    try:
        st, body = _http("POST", "/topology/session", {"spec": spec, "seed": 1})
        ok = st == 200 and isinstance(body, dict) and "session_id" in body
        print(("PASS" if ok else "FAIL"), "新建会话", st)
        if not ok:
            fails.append("新建会话")
        sid = body.get("session_id") if ok else None
    except Exception as e:
        print("FAIL 新建会话", repr(e))
        fails.append("新建会话")

    # 4) 会话状态（执行器后端可用）
    if sid:
        try:
            st, body = _http("GET", f"/topology/state/{sid}")
            ok = st == 200
            print(("PASS" if ok else "FAIL"), "会话状态", st)
            if not ok:
                fails.append("会话状态")
        except Exception as e:
            print("FAIL 会话状态", repr(e))
            fails.append("会话状态")

    print("\n=== SELFTEST", "ALL PASS ===" if not fails else f"FAILED: {fails} ===")
    return 0 if not fails else 1


def _open_window():
    import webview
    webview.create_window(
        "Circuit Agents · 智能任务台",
        EDITOR_URL,
        width=1440,
        height=900,
        min_size=(960, 600),
        text_select=True,
        confirm_close=False,
    )
    webview.start()


def main() -> int:
    if "--selftest" in sys.argv:
        t = threading.Thread(target=_start_server, daemon=True)
        t.start()
        if not _wait_ready():
            print("FAIL 后端未启动（端口 %d 可能被占用）" % PORT)
            return 1
        rc = _selftest()
        _stop_server()
        return rc

    # GUI 模式：运行期产物落地到 %LOCALAPPDATA%/circuit-agents，避免污染启动目录
    data_dir = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "circuit-agents")
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)

    t = threading.Thread(target=_start_server, daemon=True)
    t.start()
    if not _wait_ready():
        print("后端启动失败，请检查端口 %d 是否被占用。" % PORT)
        return 1
    try:
        _open_window()
    finally:
        _stop_server()
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
