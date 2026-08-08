#!/usr/bin/env python3
"""深化③ 房间持久化 —— 真·重启恢复活体验证。

流程（单个 Python 进程内完成，避免跨 Bash 调用子进程被回收）：
  ① 起服务（ROOMS_FILE 指向临时 .rooms.json）→ 建房间 + 加成员 + 编辑（rev→1）
  ② 杀掉服务（模拟宕机/重启）
  ③ 用同一 ROOMS_FILE 重启服务（模拟『服务重启』）
  ④ 断言：房间仍在、owner/成员/rev/spec/ops/activity 全部保留、且能继续编辑（rev→2）

全程不依赖 git，专门验证『重启不丢房间/记忆』这一承诺。
"""
import os, sys, time, json, tempfile, subprocess, signal
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
BASE = "http://127.0.0.1:8765"
ROOMS_FILE = os.path.join(tempfile.mkdtemp(prefix="ca_persist_"), "rooms.json")
LOG = os.path.join(tempfile.gettempdir(), "ca_persist_server.log")

_spec = {"name": "live_persist", "components": {
    "src": {"type": "power", "label": "task", "task": "x"},
    "a": {"type": "resistor", "label": "A", "model": "small",
          "required_inputs": ["x"], "produced_outputs": ["y"]},
}, "wires": [["src", "a"]]}


def api(method, path, body=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, e.code


def start_server():
    env = dict(os.environ)
    env["ROOMS_FILE"] = ROOMS_FILE
    proc = subprocess.Popen([PY, "server.py", "--port", "8765"],
                            cwd=REPO, env=env,
                            stdout=open(LOG, "w"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            api("GET", "/health")
            return proc
        except Exception:
            time.sleep(0.5)
    raise SystemExit("✗ 服务未能启动")


def stop_server(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    passed, failed = [], []
    def check(cond, msg):
        (passed if cond else failed).append(msg)
        print(("  ✓ " if cond else "  ✗ ") + msg)

    # ── ① 起服务 + 建房间 + 编辑 ──
    print("[1] 启动服务并建房间、加成员、编辑")
    proc = start_server()
    try:
        rid = "live_persist_room"
        r1, _ = api("POST", "/rooms", {"spec": _spec, "owner_id": "alice",
                                       "name": "persist", "room_id": rid})
        check(r1 and r1.get("room_id") == rid, "房间创建成功")
        sid = r1["session_id"]
        api("POST", f"/rooms/{rid}/join", {"user_id": "bob", "desired_role": "mentor"})
        e1, _ = api("POST", f"/topology/edit/{sid}?room_id={rid}&user_id=alice",
                    {"op": "replace", "cid": "a", "comp": {"model": "large"}, "base_rev": 0})
        check(e1 and e1.get("rev") == 1, "编辑推进 rev=1")
        info1, _ = api("GET", f"/rooms/{rid}?user_id=alice")
        check(info1 and info1.get("rev") == 1, "房间 rev=1 已落库")
    finally:
        stop_server(proc)

    # ── ③ 用同一 store 重启 ──
    print("[2] 杀掉服务，用同一 ROOMS_FILE 重启（模拟宕机恢复）")
    check(os.path.exists(ROOMS_FILE), "落盘文件 .rooms.json 存在")
    proc2 = start_server()
    try:
        # ── ④ 断言恢复 ──
        print("[3] 重启后断言房间/记忆全部保留")
        info2, _ = api("GET", f"/rooms/{rid}?user_id=alice")
        check(info2 is not None, "重启后房间仍可被查询（未丢失）")
        check(info2["owner"] == "alice", "owner 保留")
        check("bob" in info2["members"] and info2["members"]["bob"] == "mentor", "成员/角色保留")
        check(info2["rev"] == 1, "rev 保留(=1)")
        check(info2["spec"]["components"]["a"]["model"] == "large", "编辑后的拓扑保留")
        ops, _ = api("GET", f"/rooms/{rid}/ops?user_id=alice&since=0")
        check(ops and ops["count"] >= 1 and ops["ops"][0]["actor"] == "alice", "op-log 保留")
        act, _ = api("GET", f"/rooms/{rid}/activity?user_id=alice")
        check(act and act["total"] >= 2, "activity 流保留(创建+加成员+编辑)")
        # 重启后房间仍是『活的』：能继续编辑
        e2, _ = api("POST", f"/topology/edit/{info2['session_id']}?room_id={rid}&user_id=bob",
                    {"op": "replace", "cid": "a", "comp": {"model": "small"}, "base_rev": 1})
        check(e2 and e2.get("rev") == 2, "重启后房间仍能继续编辑(rev→2)")
    finally:
        stop_server(proc2)

    # 清理临时落盘
    try:
        os.remove(ROOMS_FILE)
    except Exception:
        pass

    print("\n" + "=" * 56)
    print(f"深化③ 活体持久化验证：{len(passed)} 项通过 / {len(failed)} 项失败")
    if failed:
        print("失败项：")
        for f in failed:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
