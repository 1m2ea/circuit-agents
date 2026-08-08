"""活体验证：深化② OT-lite 并发编辑（真实 HTTP + 真实多线程并发）。

验证「多人同时改拓扑不冲突」：
  · 改不同节点 → 全部成功、rev 连续无丢失（不该互相阻塞）
  · 改同一节点且落后 → 409 冲突并指出是谁改的（不静默覆盖）
  · 冲突后重放追平 → 重试成功
  · force 强推可覆盖但被标记（可追责）
用法：python _live_concurrent_check.py
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
ok_n = fail_n = 0
_p = threading.Lock()


def call(path, body=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:200]


def check(name, cond, extra=""):
    global ok_n, fail_n
    with _p:
        if cond:
            ok_n += 1
            print(f"  ✓ {name}" + (f"  · {extra}" if extra else ""))
        else:
            fail_n += 1
            print(f"  ✗ {name}  << 失败 {extra}")


N = 10
SPEC = {"name": "ot_live", "components": {
    "src": {"type": "power", "label": "task", "task": "并发编辑压测"},
    **{f"n{i}": {"type": "resistor", "label": f"N{i}", "model": "small",
                 "produced_outputs": [f"o{i}"]} for i in range(N)},
}, "wires": [["src", f"n{i}"] for i in range(N)]}

RID = "live_ot_" + str(int(time.time()))[-6:]
print("=" * 66)
print("活体验证 · 深化② OT-lite 并发编辑（真实 HTTP + 多线程）")
print("=" * 66)

print("\n[1] 建房并暂停（编辑须在 paused 态）")
s, room = call("/rooms", {"spec": SPEC, "owner_id": "u0", "name": "并发房", "room_id": RID})
check("房间创建成功", s == 200, RID)
SID = room["session_id"]
call(f"/topology/pause/{SID}")
s, info = call(f"/rooms/{RID}?user_id=u0")
check("新房间 rev=0", info.get("rev") == 0)

print(f"\n[2] {N} 个用户同时改 {N} 个**不同**节点（都自称 base_rev=0）")
for i in range(1, N):
    call(f"/rooms/{RID}/join", {"user_id": f"u{i}", "desired_role": "mentor"})
results, errors = [None] * N, []


def worker(i):
    st, r = call(f"/topology/edit/{SID}?room_id={RID}&user_id=u{i}",
                 {"op": "replace", "cid": f"n{i}", "comp": {"model": "large"},
                  "base_rev": 0})
    results[i] = (st, r)
    if st != 200:
        errors.append((i, st, r))


t0 = time.time()
ths = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in ths:
    t.start()
for t in ths:
    t.join()
dt = time.time() - t0
check(f"{N} 个并发编辑全部成功（改不同节点不该互相阻塞）", not errors,
      f"{dt:.2f}s" + (f" 错误={errors[:1]}" if errors else ""))
revs = sorted(r[1]["rev"] for r in results if r and r[0] == 200)
check("rev 连续无重复无丢失", revs == list(range(1, N + 1)), f"revs={revs}")
s, info = call(f"/rooms/{RID}?user_id=u0")
check(f"房间 rev 推进到 {N}", info.get("rev") == N, f"rev={info.get('rev')}")
comps = info["spec"]["components"]
upgraded = sum(1 for i in range(N) if comps[f"n{i}"].get("model") == "large")
check(f"{N} 处改动全部落到共享拓扑（无覆盖丢失）", upgraded == N, f"{upgraded}/{N} 已升级")

print(f"\n[2b] 原子性压测：{N} 个用户**同时抢改同一个节点** n0（同一 base_rev）")
s, info = call(f"/rooms/{RID}?user_id=u0")
base = info["rev"]
race_ok, race_409, race_other = [], [], []


def racer(i):
    st, r = call(f"/topology/edit/{SID}?room_id={RID}&user_id=u{i}",
                 {"op": "replace", "cid": "n0", "comp": {"model": f"m{i}"},
                  "base_rev": base})
    if st == 200:
        race_ok.append((i, r["rev"]))
    elif st == 409:
        race_409.append(i)
    else:
        race_other.append((i, st, r))


ths = [threading.Thread(target=racer, args=(i,)) for i in range(N)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("抢改同一节点：恰好 1 人成功（无 TOCTOU 静默覆盖）", len(race_ok) == 1,
      f"成功 {len(race_ok)} 人 {race_ok}")
check(f"其余 {N-1} 人被 409 拒绝（提示先同步）", len(race_409) == N - 1,
      f"409 {len(race_409)} 人")
check("无非预期错误", not race_other, str(race_other[:1]))

print("\n[2c] 落后但改不同节点 → 自动 rebase（不该被拦）")
s, info = call(f"/rooms/{RID}?user_id=u0")
stale = info["rev"] - 1                                # 假装落后一个版本
st, r = call(f"/topology/edit/{SID}?room_id={RID}&user_id=u3",
             {"op": "replace", "cid": "n3", "comp": {"model": "tool"},
              "base_rev": stale})
check("落后但异目标 → 放行", st == 200, f"status={st}")
check("并标记为自动 rebase", st == 200 and r.get("merge", {}).get("rebased") is True,
      str(r.get("merge"))[:60])

print("\n[3] 两人同时改**同一个**节点 → 后到者应 409（不静默覆盖）")
s2, r2 = call(f"/topology/edit/{SID}?room_id={RID}&user_id=u1",
              {"op": "replace", "cid": "n0", "comp": {"model": "tool"}, "base_rev": 0})
check("同目标并发编辑被 409 拦截", s2 == 409, f"status={s2}")
det = r2.get("detail", r2) if isinstance(r2, dict) else {}
check("冲突详情指出是谁改的", bool(det.get("clashes")) and det["clashes"][0]["actor"] == "u0",
      str(det.get("clashes", [{}])[0])[:80])
check("冲突详情定位到具体目标",
      any("node/n0" in t for t in det.get("clashes", [{}])[0].get("targets", [])))

print("\n[4] 重放追平 → 重试成功（这才是『不冲突』的正解）")
s3, ops = call(f"/rooms/{RID}/ops?user_id=u1&since=0")
check("可增量拉取 op-log 追平", s3 == 200 and ops["count"] >= N, f"{ops['count']} 条 op")
check("op-log 记录了每次改动的作者", all(o.get("actor") for o in ops["ops"]))
cur = ops["rev"]
s4, r4 = call(f"/topology/edit/{SID}?room_id={RID}&user_id=u1",
              {"op": "replace", "cid": "n0", "comp": {"model": "tool"}, "base_rev": cur})
check("追平后重试成功", s4 == 200, f"rev={r4.get('rev') if s4 == 200 else r4}")
check("追平后提交无冲突标记", s4 == 200 and "merge" not in r4)

print("\n[5] force 强推：可覆盖但被标记（可追责）")
s5, r5 = call(f"/topology/edit/{SID}?room_id={RID}&user_id=u2",
              {"op": "replace", "cid": "n0", "comp": {"model": "small"},
               "base_rev": 0, "force": True})
check("force 强推成功", s5 == 200, f"status={s5}")
check("强推被标记 forced（事后可追责）",
      s5 == 200 and r5.get("merge", {}).get("forced") is True, str(r5.get("merge"))[:70])

print("\n[6] 全程改动都进了 activity 流（协作者可见）")
s6, act = call(f"/rooms/{RID}/activity?user_id=u0")
edits = [a for a in act["activities"] if a["action"] == "edit"]
check("activity 记录了所有编辑", len(edits) >= N + 2, f"{len(edits)} 条 edit 事件")
actors = {a["actor"] for a in edits}
check("能看出是哪些人在改", len(actors) >= 3, f"参与者 {sorted(actors)[:5]}")

print("\n" + "=" * 66)
print(f"结果：{ok_n} 项通过 / {fail_n} 项失败")
print("=" * 66)
sys.exit(1 if fail_n else 0)
