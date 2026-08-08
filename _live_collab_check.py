import json, urllib.request, urllib.error, time

BASE = "http://127.0.0.1:8765"
ROOM = "collab_demo"

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

# 等 server 起来
for _ in range(40):
    try:
        get("/rooms/" + ROOM)
        break
    except urllib.error.URLError:
        time.sleep(0.5)

print("== 实时协同同步 在线验证 ==")
# 1) alice 用指定 room_id 创建房间（owner）
st, body = post("/rooms", {"spec": spec, "owner_id": "alice", "name": "demo", "room_id": ROOM})
assert st == 200 and body["room_id"] == ROOM, f"alice 创建房间应 200 且 room_id={ROOM}, got {st}: {body}"
sid = body["session_id"]
print(f"[1] alice 创建房间 OK  room={ROOM} session={sid}")

# 2) bob 加入（observer）
st, body = post(f"/rooms/{ROOM}/join", {"user_id": "bob", "desired_role": "observer"})
assert st == 200 and body["role"] == "observer", f"bob 加入应 observer, got {st}: {body}"
print(f"[2] bob 加入 OK  role={body['role']}")

# 3) alice 编辑（owner 权限内）
st, body = post(f"/topology/edit/{sid}?room_id={ROOM}&user_id=alice",
                {"op": "set_gate", "cid": "adc", "threshold": 0.9})
assert st == 200, f"alice 编辑应 200, got {st}"
print(f"[3] alice 编辑 OK (http={st})")

# 4) bob 拉房间动态 → 应看到 alice 的 edit（多人动作互通）
st, body = get(f"/rooms/{ROOM}/activity?user_id=bob")
acts = body.get("activities", [])
actors = {a["actor"] for a in acts}
assert "alice" in actors, f"bob 应能看到 alice 的动作, actors={actors}"
assert "edit" in [a["action"] for a in acts], f"activity 应含 edit"
print(f"[4] bob 看到 alice 的动作 OK  actions={[a['action'] for a in acts]}")

# 5) bob 拿到的 spec 与 alice 创建时一致（拓扑共享渲染）
st, body = get(f"/rooms/{ROOM}?user_id=bob")
assert st == 200 and body.get("spec"), "bob 应拿到房间 spec"
assert body["spec"]["wires"] == spec["wires"], "bob 拿到的拓扑应与 alice 一致"
print(f"[5] bob 拿到共享拓扑 spec OK  成员={body['members']}")

# 6) 成员互见
assert body["members"].get("alice") == "owner" and body["members"].get("bob") == "observer", \
    f"成员应含 alice(owner)+bob(observer), got {body['members']}"
print(f"[6] 成员互见 OK  members={body['members']}")

print("\n✅ 实时协同同步 在线全链路通过（多人房间/动作互通/拓扑共享/角色互见）")
