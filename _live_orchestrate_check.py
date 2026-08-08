"""大规模协作 维度③ 多 agent 编排 —— 端到端 HTTP 校验。

前置：server.py 已在 8765 运行（含 #186 端点）。
用法：python _live_orchestrate_check.py
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
        "name": "orch_demo",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "r1": {"type": "resistor", "label": "a", "model": "small", "produced_outputs": ["x"]},
            "r2": {"type": "resistor", "label": "b", "model": "small",
                   "required_inputs": ["x"], "produced_outputs": ["y"]},
            "adc": {"type": "adc", "label": "质量门", "threshold": 0.5},
        },
        "wires": [["src", "r1"], ["r1", "r2"], ["r2", "adc"]],
    }
    rid = "orche_demo"

    # 1) heidi 建房间
    st, cr = _req("POST", "/rooms", {"spec": spec, "owner_id": "heidi", "name": "orch", "room_id": rid})
    _ok(st == 200 and cr["room_id"] == rid, "多agent① heidi 建编排房间")

    # 2) heidi(owner) 发起编排（mock 确定性）
    st, orc = _req("POST", f"/rooms/{rid}/orchestrate", {"user_id": "heidi", "mock": True})
    _ok(st == 200, "多agent② heidi 发起编排")
    _t = orc["trace"]
    _ok(_t["mentor"]["plan"]["node_fixes"], "多agent②b mentor 提出优化")
    _ok(_t["reviewer"]["verdict"] == "approve", "多agent②c reviewer 裁决通过")
    _ok(_t["student"]["quality_gate_passed"] is True, "多agent②d student 执行过质量门")

    # 3) 编排事件写入 room activity（所有人可见）
    st, act = _req("GET", f"/rooms/{rid}/activity?user_id=heidi")
    _acts = act["activities"]
    _ok(any(a["action"] == "orchestrate" for a in _acts), "多agent③ 有 orchestrate 事件")
    _ok(any(a["actor"] == "student" for a in _acts), "多agent③b student 执行对房间可见")

    # 4) 通过则固化进房间知识库
    st, mem = _req("GET", f"/rooms/{rid}/memory?user_id=heidi")
    _ok(mem["templates"], "多agent④ 编排通过固化进知识库模板")

    # 5) ivan 加入 reviewer（有 adjudicate 但无 control）
    st, join = _req("POST", f"/rooms/{rid}/join", {"user_id": "ivan", "desired_role": "reviewer"})
    _ok(st == 200 and join["role"] == "reviewer", "多agent⑤ ivan 加入 reviewer")

    # 6) ivan 无 control → 发起编排被 403
    st, _ = _req("POST", f"/rooms/{rid}/orchestrate", {"user_id": "ivan", "mock": True})
    _ok(st == 403, "多agent⑥ reviewer 无 control 发起编排被 403 拦截")

    # 7) ivan 仍能看到编排结果（协作可见）
    st, act2 = _req("GET", f"/rooms/{rid}/activity?user_id=ivan")
    _ok(any(a["action"] == "orchestrate" for a in act2["activities"]),
        "多agent⑦ reviewer 仍能看到编排事件")

    print("\n全部通过 ✓ 多 agent 编排（维度③）端到端 OK")


if __name__ == "__main__":
    main()
