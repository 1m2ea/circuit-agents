"""大规模协作 维度④ reroute 拖拽改流向 —— 端到端 HTTP 校验。

前置：server.py 已在 8765 运行（含 #187 端点扩展）。
用法：python _live_reroute_check.py
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8765"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _ok(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        raise SystemExit(f"FAILED: {msg}")


def main():
    spec = {
        "name": "rt_demo",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "a": {"type": "resistor", "label": "A", "model": "small", "produced_outputs": ["x"]},
            "b": {"type": "resistor", "label": "B", "model": "small", "required_inputs": ["x"], "produced_outputs": ["y"]},
            "c": {"type": "resistor", "label": "C", "model": "small", "required_inputs": ["y"], "produced_outputs": ["z"]},
        },
        "wires": [["src", "a"], ["a", "b"], ["b", "c"]],
    }
    rid = "reroute_demo"

    # 1) kara 建房间
    st, cr = _req("POST", "/rooms", {"spec": spec, "owner_id": "kara", "name": "rt", "room_id": rid})
    _ok(st == 200 and cr["room_id"] == rid, "reroute① kara 建房间")
    sid = cr["session_id"]
    # 先暂停以便编辑
    _req("POST", f"/topology/pause/{sid}", {"room_id": rid, "user_id": "kara"})

    # 2) kara(owner) reroute：a->b 重定向为 a->c
    st, r = _req("POST", f"/topology/edit/{sid}?room_id={rid}&user_id=kara",
                 {"op": "reroute", "old": ["a", "b"], "new": ["a", "c"]})
    _ok(st == 200, "reroute② kara 发起改流向")
    wires = r["state"]["wires"]
    _ok(["a", "c"] in wires, "reroute②b 新 wire a->c 生效")
    _ok(["a", "b"] not in wires, "reroute②c 旧 wire a->b 消失")

    # 3) 房间共享拓扑同步（协作者可见）
    st, info = _req("GET", f"/rooms/{rid}?user_id=kara")
    _ok(["a", "c"] in info["spec"]["wires"], "reroute③ 改流向同步到房间共享 spec")

    # 4) 房间 activity 记录 edit/reroute
    st, act = _req("GET", f"/rooms/{rid}/activity?user_id=kara")
    _ok(any(a["action"] == "edit" and a["detail"] == "reroute" for a in act["activities"]),
        "reroute④ activity 记录 edit/reroute")

    # 5) lee 加入 observer
    st, join = _req("POST", f"/rooms/{rid}/join", {"user_id": "lee", "desired_role": "observer"})
    _ok(st == 200 and join["role"] == "observer", "reroute⑤ lee 加入 observer")

    # 6) lee(无 edit 权限) reroute → 403
    st, _ = _req("POST", f"/topology/edit/{sid}?room_id={rid}&user_id=lee",
                 {"op": "reroute", "old": ["a", "c"], "new": ["a", "b"]})
    _ok(st == 403, "reroute⑥ observer 改流向被 403 拦截")

    # 7) 新建 wire（old=None）：kara 从 a 新建到 b
    _req("POST", f"/topology/pause/{sid}", {"room_id": rid, "user_id": "kara"})
    st, r2 = _req("POST", f"/topology/edit/{sid}?room_id={rid}&user_id=kara",
                  {"op": "reroute", "old": None, "new": ["a", "b"]})
    _ok(st == 200 and ["a", "b"] in r2["state"]["wires"], "reroute⑦ old=None 新建 wire a->b")

    print("\n全部通过 ✓ reroute 拖拽改流向（维度④）端到端 OK")


if __name__ == "__main__":
    main()
