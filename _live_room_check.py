import json, urllib.request, urllib.error, sys

BASE = "http://127.0.0.1:8765"

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def get(path):
    try:
        r = urllib.request.urlopen(BASE + path, timeout=15)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

spec = {"name": "demo",
        "components": {
            "src": {"type": "power", "label": "t", "task": "x"},
            "r1": {"type": "resistor", "label": "a", "model": "small"},
            "adc": {"type": "adc", "label": "g", "threshold": 0.5}},
        "wires": [["src", "r1"], ["r1", "adc"]]}

print("== 大规模协作 Phase0 在线验证 ==")
st, body = post("/rooms", {"spec": spec, "owner_id": "alice", "name": "demo"})
assert st == 200, f"create_room 应 200, got {st}: {body}"
rid, sid = body["room_id"], body["session_id"]
print(f"[1] create_room OK  room={rid} session={sid}")

st, body = post(f"/rooms/{rid}/join", {"user_id": "bob", "desired_role": "observer"})
assert st == 200 and body["role"] == "observer", f"join 应 observer, got {st}: {body}"
print(f"[2] bob join  OK  role={body['role']}")

st, _ = get(f"/topology/state/{sid}?room_id={rid}&user_id=bob")
assert st == 200, f"observer 读状态应 200, got {st}"
print(f"[3] observer 读状态 OK (http={st})")

st, _ = post(f"/topology/edit/{sid}?room_id={rid}&user_id=bob",
             {"op": "set_gate", "cid": "adc", "threshold": 0.6})
assert st == 403, f"observer 编辑应 403, got {st}"
print(f"[4] observer 编辑被拦截 OK (http={st})")

st, _ = post(f"/topology/edit/{sid}?room_id={rid}&user_id=alice",
             {"op": "set_gate", "cid": "adc", "threshold": 0.7})
assert st == 200, f"owner 编辑应 200, got {st}"
print(f"[5] owner 编辑 OK (http={st})")

st, body = get(f"/rooms/{rid}/activity?user_id=alice")
acts = [a["action"] for a in body.get("activities", [])]
assert st == 200 and {"create_room", "join", "edit"} <= set(acts), f"activity 应含关键动作, got {acts}"
print(f"[6] activity 流 OK  actions={acts}")

st, body = get(f"/rooms/{rid}?user_id=alice")
assert st == 200 and body["owner"] == "alice", f"room_info 异常: {body}"
print(f"[7] room_info OK  members={body['members']}")
print("\n✅ 大规模协作 Phase0 在线全链路通过")
