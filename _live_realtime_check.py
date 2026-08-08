"""实时交互（极致=连续不中断）活体验证。

验证两件事，都走真实 HTTP（不是 selftest 的进程内订阅）：
A. GET /topology/stream/{sid} 真的把执行事件经 SSE 推到客户端
   —— 收到 snapshot + node_done + done，证明交互流无刷新空洞（不靠轮询）。
B. 运行中随时 edit（不暂停）也能落地，且主进度不卡死
   —— 印证「人类随时介入不造成系统停滞」。

用法: python _live_realtime_check.py
依赖: 仅标准库（urllib / subprocess / threading）。
"""
import subprocess, sys, os, json, time, tempfile, threading, urllib.request, urllib.error

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def _req(method, path, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_json(path, payload, timeout=10):
    return _req("POST", path, payload, timeout)


def get_json(path, timeout=10):
    return _req("GET", path, None, timeout)


def sse_collect(path, timeout=15):
    """打开 SSE 流，逐行解析 data: 帧，收集事件直到收到 done 或超时。"""
    collected = []
    stop = threading.Event()

    def _read():
        try:
            req = urllib.request.Request(BASE + path)
            with urllib.request.urlopen(req, timeout=timeout + 5) as r:
                for raw in r:
                    line = raw.decode(errors="replace")
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload:
                            try:
                                ev = json.loads(payload)
                            except Exception:
                                continue
                            collected.append(ev)
                            if ev.get("type") == "done":
                                break
                    if stop.is_set():
                        break
        except Exception as e:
            collected.append({"_error": str(e)})

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    stop.set()
    return collected


_SPEC = {
    "name": "rt_live",
    "components": {
        "s1": {"type": "source", "label": "S1", "produced_outputs": ["x"]},
        "r1": {"type": "resistor", "label": "R1", "model": "small", "produced_outputs": ["y"]},
        "a1": {"type": "adc", "label": "A1", "threshold": 0.8, "produced_outputs": ["z"]},
        "out": {"type": "sink", "label": "OUT"},
    },
    "wires": [["s1", "r1"], ["r1", "a1"], ["a1", "out"]],
}


def main():
    tmp = tempfile.mkdtemp(prefix="ca_rt_live_")
    env = dict(os.environ)
    env["ROOMS_FILE"] = os.path.join(tmp, "rooms.json")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 等健康检查
        up = False
        for _ in range(60):
            try:
                get_json("/health")
                up = True
                break
            except Exception:
                time.sleep(0.2)
        assert up, "服务器未在预期时间内就绪"

        # ── A. 真实 HTTP SSE 流 ──
        sess = post_json("/topology/session",
                         {"spec": _SPEC, "seed": 3, "node_delay_ms": 60})
        sid = sess["session_id"]
        events = sse_collect(f"/topology/stream/{sid}", timeout=15)
        types = {e.get("type") for e in events}
        assert "snapshot" in types, "A: 应收到 snapshot（初始现状）"
        assert "node_done" in types, "A: 应收到 node_done（连续事件流，无轮询空洞）"
        assert "done" in types, "A: 应收到 done（流正常收尾）"
        print(f"  A 真实HTTP SSE: 收到 {len(events)} 帧, 含 snapshot/node_done/done ✓")

        # ── B. 运行中干预不卡死主进度 ──
        sess2 = post_json("/topology/session",
                          {"spec": _SPEC, "seed": 4, "node_delay_ms": 80})
        sid2 = sess2["session_id"]
        # 立即在运行中下发编辑（不暂停）
        ed = post_json(f"/topology/edit/{sid2}",
                        {"op": "set_gate", "cid": "a1", "threshold": 0.5})
        assert ed["edit"].get("applied") is True, "B: 运行中干预应成功应用"
        # 等执行自然结束
        st = {}
        for _ in range(100):
            st = get_json(f"/topology/state/{sid2}")
            if st.get("done"):
                break
            time.sleep(0.2)
        assert st.get("done"), "B: 运行中干预后主进度不应卡死"
        assert st.get("error") is None, "B: 运行中干预不应引发异常"
        print("  B 运行中干预: 已应用 + 主进度自然结束 ✓")

        print("✓ _live_realtime_check.py 全过（真实HTTP SSE + 连续干预不卡死）")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
