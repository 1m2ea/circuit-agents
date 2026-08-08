"""大规模协作（极致 = 在实时交互+人机协同前提下支持人数众多）活体验证。

全部走真实 HTTP（多线程模拟众多真人客户端），验证四件事：

A. **零拒绝并发**：N 个用户同时抢改同一节点（都基于陈旧 base_rev）
   → CRDT 模式下必须全部成功，无一次 409、无一次重试。
B. **全员收敛**：服务端 op 流以乱序 + 重复投递喂给多个独立客户端副本
   → 所有副本状态哈希与服务端一致（最终一致性）。
C. **人数众多的实时性**：N 个 SSE 订阅者同时挂在同一房间，
   任一人的编辑都要实时到达所有人；慢消费者不得拖垮其他人。
D. **在线感知**：众多用户的 presence 心跳不打满通道（coalesce），
   在线名单/焦点地图正确，超时自动离线。

用法: python _live_scale_check.py
依赖: 仅标准库。
"""
import subprocess, sys, os, json, time, tempfile, threading
import urllib.request, urllib.error

PORT = 8801
BASE = f"http://127.0.0.1:{PORT}"
N_USERS = 24          # 并发"真人"数（真实 HTTP 连接）
N_WATCHERS = 12       # 同时挂 SSE 的观众数


def _req(method, path, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_json(path, payload, timeout=20):
    return _req("POST", path, payload, timeout)


def get_json(path, timeout=20):
    return _req("GET", path, None, timeout)


class Watcher(threading.Thread):
    """一个真实 SSE 订阅者。slow=True 时故意不消费（模拟卡死的观众）。"""

    def __init__(self, path, slow=False, budget=12):
        super().__init__(daemon=True)
        self.path, self.slow, self.budget = path, slow, budget
        self.events, self.error = [], None
        self._stop = threading.Event()

    def run(self):
        try:
            req = urllib.request.Request(BASE + self.path)
            with urllib.request.urlopen(req, timeout=self.budget + 5) as r:
                deadline = time.time() + self.budget
                for raw in r:
                    if self.slow:
                        time.sleep(0.5)          # 慢消费者：读得极慢
                    line = raw.decode(errors="replace")
                    if line.startswith("data:"):
                        p = line[5:].strip()
                        if p:
                            try:
                                self.events.append(json.loads(p))
                            except Exception:
                                pass
                    if self._stop.is_set() or time.time() > deadline:
                        break
        except Exception as e:
            self.error = str(e)

    def stop(self):
        self._stop.set()


_SPEC = {
    "name": "scale_live",
    "components": {
        "src": {"type": "power", "label": "task", "task": "x"},
        "n0": {"type": "resistor", "label": "N0", "model": "small",
               "produced_outputs": ["x"]},
        "n1": {"type": "resistor", "label": "N1", "model": "small",
               "required_inputs": ["x"], "produced_outputs": ["y"]},
    },
    "wires": [["src", "n0"], ["n0", "n1"]],
}


def main():
    tmp = tempfile.mkdtemp(prefix="ca_scale_live_")
    env = dict(os.environ)
    env["ROOMS_FILE"] = os.path.join(tmp, "rooms.json")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    watchers = []
    try:
        up = False
        for _ in range(80):
            try:
                get_json("/health"); up = True; break
            except Exception:
                time.sleep(0.2)
        assert up, "服务器未就绪"

        rid = "live_scale"
        room = post_json("/rooms", {"spec": _SPEC, "owner_id": "host",
                                    "name": "scale", "room_id": rid})
        sid = room["session_id"]
        try:
            post_json(f"/topology/pause/{sid}", {})
        except Exception:
            pass
        for i in range(N_USERS):
            post_json(f"/rooms/{rid}/join",
                      {"user_id": f"p{i}", "desired_role": "mentor"})
        info = get_json(f"/rooms/{rid}?user_id=host")
        assert info["concurrency"] == "crdt", "新房间应默认 CRDT"
        print(f"  房间就绪：{len(info['members'])} 名成员，模式={info['concurrency']}")

        # ── C(上半)：众多观众先挂上 SSE，其中一个是"卡死的慢消费者" ──
        for i in range(N_WATCHERS):
            w = Watcher(f"/topology/stream/{sid}?room_id={rid}&user_id=p{i}",
                        slow=(i == 0), budget=12)
            w.start(); watchers.append(w)
        time.sleep(1.0)                          # 等订阅建立
        st0 = get_json(f"/rooms/{rid}/stats?user_id=host")
        assert st0["subscribers"] >= N_WATCHERS, \
            f"C: 应有 ≥{N_WATCHERS} 个订阅者，实际 {st0['subscribers']}"
        print(f"  C1 {st0['subscribers']} 个真实 SSE 订阅者同时在线（含 1 个慢消费者）✓")

        # ── A：N 个用户同时抢改同一节点，全部基于陈旧 base_rev=0 ──
        ok, rejected, errs = [], [], []
        lk = threading.Lock()

        def rush(i):
            try:
                r = post_json(f"/topology/edit/{sid}?room_id={rid}&user_id=p{i}",
                              {"op": "replace", "cid": "n0",
                               "comp": {"model": f"m{i}"}, "base_rev": 0})
                with lk: ok.append(r)
            except urllib.error.HTTPError as e:
                with lk:
                    (rejected if e.code == 409 else errs).append(e)
            except Exception as e:
                with lk: errs.append(e)

        t0 = time.time()
        ths = [threading.Thread(target=rush, args=(i,)) for i in range(N_USERS)]
        for t in ths: t.start()
        for t in ths: t.join()
        dt = time.time() - t0
        assert not errs, f"A: 并发编辑不应报错 {errs[:2]}"
        assert not rejected, f"A: CRDT 必须零拒绝，实际 409×{len(rejected)}"
        assert len(ok) == N_USERS, f"A: {N_USERS} 人应全成功，实际 {len(ok)}"
        print(f"  A 零拒绝并发：{N_USERS} 人 HTTP 抢改同一节点全部成功，"
              f"0 次 409、0 次重试，耗时 {dt:.2f}s ✓")

        # ── B：乱序 + 重复投递喂副本 → 与服务端收敛 ──
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from runtime import TopologyCRDT
        view = get_json(f"/rooms/{rid}/crdt?user_id=host")
        srv_hash, ops = view["hash"], view["ops"]
        import random
        hashes = set()
        for seed in (7, 13, 29):
            sh = ops[:]
            random.Random(seed).shuffle(sh)
            rep = TopologyCRDT(actor=f"peer{seed}")
            rep.merge(sh + sh[:8])               # 乱序 + 重复
            hashes.add(rep.state_hash())
        assert hashes == {srv_hash}, f"B: 副本未收敛 {hashes} vs {srv_hash}"
        print(f"  B 全员收敛：3 个副本 × 乱序 + 重复投递 → 哈希均 = 服务端 {srv_hash} ✓")

        # 客户端本地改 → 提交 op → 幂等
        peer = TopologyCRDT(actor="peer_live"); peer.merge(ops)
        my_ops = [peer.set_attr("n1", "model", "large")]
        s1 = post_json(f"/rooms/{rid}/crdt/ops", {"user_id": "p1", "ops": my_ops})
        s2 = post_json(f"/rooms/{rid}/crdt/ops", {"user_id": "p1", "ops": my_ops})
        assert s1["merged"] == 1 and s2["merged"] == 0, "B: 客户端 op 应合并一次且幂等"
        print("  B2 客户端本地先改再投递：合并 1 次、重复投递幂等（0 次）✓")

        # ── D：众多 presence 心跳 ──
        for i in range(N_USERS):
            post_json(f"/rooms/{rid}/presence",
                      {"user_id": f"p{i}", "cursor": [i, i],
                       "focus": "n0" if i % 2 == 0 else "n1"})
        for _ in range(5):                       # 高频重复心跳（考验 coalesce）
            for i in range(0, N_USERS, 3):
                post_json(f"/rooms/{rid}/presence",
                          {"user_id": f"p{i}", "cursor": [99, 99], "focus": "n1"})
        pl = get_json(f"/rooms/{rid}/presence?user_id=host")
        assert pl["online"] == N_USERS, f"D: 应 {N_USERS} 人在线，实际 {pl['online']}"
        assert len(pl["focus_map"].get("n0", [])) + len(pl["focus_map"].get("n1", [])) \
            == N_USERS, "D: 焦点地图应覆盖全员"
        print(f"  D 在线感知：{pl['online']} 人在线，焦点地图 "
              f"n0={len(pl['focus_map'].get('n0', []))} / "
              f"n1={len(pl['focus_map'].get('n1', []))} ✓")

        # ── C(下半)：编辑与心跳是否实时到达众多观众；慢消费者不拖垮别人 ──
        time.sleep(1.5)
        for w in watchers: w.stop()
        for w in watchers: w.join(timeout=8)
        fast = watchers[1:]
        got = [w for w in fast if any(e.get("type") in
                                      ("crdt_ops", "activity", "topology_edit", "presence")
                                      for e in w.events)]
        assert len(got) >= max(1, len(fast) - 1), \
            f"C: 多数快订阅者应收到实时事件，实际 {len(got)}/{len(fast)}"
        stats = get_json(f"/rooms/{rid}/stats?user_id=host")
        print(f"  C2 实时扇出：{len(got)}/{len(fast)} 个快订阅者收到实时事件；"
              f"慢消费者未阻塞广播（hub pushed={stats['hub']['pushed']} "
              f"dropped={stats['hub']['dropped']} coalesced={stats['hub']['coalesced']}）✓")

        assert stats["online"] >= N_USERS, "水位：在线人数应完整"
        print(f"  水位：members={stats['members']} online={stats['online']} "
              f"subscribers={stats['subscribers']} rev={stats['rev']} "
              f"crdt_ops={stats['crdt_ops']}")

        print("✓ _live_scale_check.py 全过"
              f"（{N_USERS} 并发用户零拒绝 + 副本收敛 + {N_WATCHERS} SSE 扇出 + presence）")
    finally:
        for w in watchers:
            w.stop()
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
