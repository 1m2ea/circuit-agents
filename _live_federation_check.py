#!/usr/bin/env python3
"""深化④ 跨房间联邦 —— 真·多房间活体验证。

单进程内完成（避免跨 Bash 调用子进程被回收）：
  ① 起服务 → 建房间 A、B
  ② B 沉淀知识（published + templates）
  ③ GET /federation → 目录列出 A、B（带知识量摘要）
  ④ A 从 B 联邦拉取 → A 拿到 B 的知识；B 不被反向污染
  ⑤ 源房间不存在 → 404
  ⑥ 验证联邦动作写入 A 的 activity（跨房间协作可见）
"""
import os, sys, time, json, tempfile, subprocess
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
BASE = "http://127.0.0.1:8765"
ROOMS_FILE = os.path.join(tempfile.mkdtemp(prefix="ca_fed_"), "rooms.json")
_spec = {"name": "fed", "components": {
    "src": {"type": "power", "label": "task", "task": "x"},
    "a": {"type": "resistor", "label": "A", "model": "small",
          "required_inputs": ["x"], "produced_outputs": ["y"]},
}, "wires": [["src", "a"]]}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, e.code


def start_server():
    env = dict(os.environ); env["ROOMS_FILE"] = ROOMS_FILE
    proc = subprocess.Popen([PY, "server.py", "--port", "8765"], cwd=REPO, env=env,
                            stdout=open(os.path.join(tempfile.gettempdir(), "ca_fed.log"), "w"),
                            stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            api("GET", "/health"); return proc
        except Exception:
            time.sleep(0.5)
    raise SystemExit("✗ 服务未启动")


def stop_server(proc):
    try: proc.terminate(); proc.wait(timeout=10)
    except Exception:
        try: proc.kill()
        except Exception: pass


def main():
    passed, failed = [], []
    def check(c, m):
        (passed if c else failed).append(m); print(("  ✓ " if c else "  ✗ ") + m)

    print("[1] 起服务 + 建两个房间 A/B")
    proc = start_server()
    try:
        rA = api("POST", "/rooms", {"spec": _spec, "owner_id": "fa", "name": "A", "room_id": "live_A"})[0]
        rB = api("POST", "/rooms", {"spec": _spec, "owner_id": "fb", "name": "B", "room_id": "live_B"})[0]
        check(rA and rA.get("room_id") == "live_A", "房间 A 创建")
        check(rB and rB.get("room_id") == "live_B", "房间 B 创建")
        # B 沉淀知识：发布一条 + 用显式历史蒸馏出模板（user_id 走 body）
        api("POST", "/rooms/live_B/memory/publish", {"user_id": "fb", "name": "kb_B1"})
        api("POST", "/rooms/live_B/memory/distill",
            {"user_id": "fb", "min_support": 1,
             "history": [{"name": "h1", "spec": _spec}, {"name": "h2", "spec": _spec}]})

        # ③ 联邦目录
        print("[2] 联邦目录列出 A/B")
        fed, _ = api("GET", "/federation")
        ids = {e["room_id"] for e in fed["rooms"]}
        check(fed["count"] >= 2 and {"live_A", "live_B"} <= ids, "联邦目录列出 A、B")
        b_ent = next(e for e in fed["rooms"] if e["room_id"] == "live_B")
        check(b_ent["published"] >= 1, "目录带源房间知识量摘要(published)")
        check(b_ent["templates"] >= 1, "目录带源房间知识量摘要(templates)")

        # ④ A 从 B 联邦拉取
        print("[3] A 从 B 拉取知识")
        fp, code = api("POST", "/rooms/live_A/memory/pull-from/live_B", {"user_id": "fa"})
        check(code is None and fp is not None, "联邦拉取端点 200")
        check(fp and "kb_B1" in fp["published"], "A 拉到 B 的已发布知识")
        check(fp and fp["imported_templates"] >= 1, "A 拉到 B 的模板")
        # ⑤ B 不被反向污染
        memB, _ = api("GET", "/rooms/live_B/memory?user_id=fb")
        check(memB and "kb_B1" in memB["published"], "源房间 B 知识未被改动（单向汇入）")
        # ⑥ 联邦动作写入 A 的 activity
        act, _ = api("GET", "/rooms/live_A/activity?user_id=fa")
        check(act and any(a["action"] == "federate_pull" for a in act["activities"]), "联邦拉取入 activity")
        # ⑦ 源不存在 → 404
        print("[4] 源房间不存在 → 404")
        _, code2 = api("POST", "/rooms/live_A/memory/pull-from/nope", {"user_id": "fa"})
        check(code2 == 404, "源房间不存在应 404")
    finally:
        stop_server(proc)
    try: os.remove(ROOMS_FILE)
    except Exception: pass

    print("\n" + "=" * 56)
    print(f"深化④ 活体联邦验证：{len(passed)} 项通过 / {len(failed)} 项失败")
    if failed:
        for f in failed: print("  - " + f)
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
