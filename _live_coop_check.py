"""人机协同（极致 = 人类与「包括机器但不限于机器」协同）活体验证。

全部走真实 HTTP，验证「协作者不分人机、机器主动招募」五件事：

A. **协作者一等公民**：人类专家 / 其他 agent / 外部知识源 / 外部系统 注册进同一个池，
   机器按能力标签择优，而不是只会问操作者。
B. **外部系统真的被拉进电路**：本脚本另起一个真实 HTTP 服务当「MES 外部系统」，
   机器卡住时主动 POST 求援 → 外部系统作答 → 答案回灌电路节点输出。
C. **主动点名真人**：机器把问题投进人类专家的待办箱，另一个"人"（独立线程 + 独立 HTTP
   客户端）轮询 /help/pending 看到点名、调 /help/{hid}/respond 作答 → 答案回灌。
D. **自动升级不吊死**：首选协作者不应答 → 超时后自动顺位下一位接管；
   全员无解则诚实上报 unanswered，电路照常收敛。
E. **协同过程实时可见**：SSE 订阅者能实时看到 机器在找谁 / 谁接了 / 答案何时生效。

用法: python _live_coop_check.py
依赖: 仅标准库。
"""
import subprocess, sys, os, json, time, tempfile, threading
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8802          # circuit-agents server
SYS_PORT = 8803      # 假扮"外部系统"的真实 HTTP 服务（MES）
BASE = f"http://127.0.0.1:{PORT}"


def _req(method, path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_json(path, payload=None, timeout=30):
    return _req("POST", path, payload if payload is not None else {}, timeout)


def get_json(path, timeout=30):
    return _req("GET", path, None, timeout)


# ── 真实的「外部系统」协作者：一个独立进程内 HTTP 服务，机器会真的 POST 过来 ──
_SYS_HITS = []


class _MESHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _SYS_HITS.append(body)
        payload = json.dumps({"value": "MES: 6061-T6 在库 137 件, 批次 B2409",
                              "confidence": 0.95}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class SSEWatcher(threading.Thread):
    """真实 SSE 订阅者：看机器求援的全过程。"""

    def __init__(self, path, budget=25):
        super().__init__(daemon=True)
        self.path, self.budget = path, budget
        self.events, self.error = [], None
        self._stop = threading.Event()

    def run(self):
        try:
            req = urllib.request.Request(BASE + self.path)
            with urllib.request.urlopen(req, timeout=self.budget + 5) as r:
                deadline = time.time() + self.budget
                for raw in r:
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

    def types(self):
        return [e.get("type") for e in self.events]


def _spec(label, needs, kinds=None, timeout=None):
    c = {"type": "resistor", "label": label, "model": "small",
         "needs": needs, "help_threshold": 0.99}
    if kinds:
        c["help_kinds"] = kinds
    if timeout is not None:
        c["help_timeout"] = timeout
    return {"name": f"coop_{label}",
            "components": {"src": {"type": "source", "value": "任务"}, "r1": c},
            "wires": [["src", "r1"]]}


def main():
    tmp = tempfile.mkdtemp(prefix="ca_coop_live_")
    env = dict(os.environ)
    env["ROOMS_FILE"] = os.path.join(tmp, "rooms.json")
    mes = ThreadingHTTPServer(("127.0.0.1", SYS_PORT), _MESHandler)
    threading.Thread(target=mes.serve_forever, daemon=True).start()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    watcher = None
    try:
        up = False
        for _ in range(80):
            try:
                get_json("/health"); up = True; break
            except Exception:
                time.sleep(0.2)
        assert up, "服务器未就绪"

        # ── A 协作者一等公民：四类同池 ──
        for c in (
            {"id": "expert_zhang", "name": "张工(工艺)", "kind": "human",
             "skills": ["process", "material"], "trust": 0.92, "latency_ms": 60000},
            {"id": "expert_wang", "name": "王工(备份)", "kind": "human",
             "skills": ["process"], "trust": 0.5, "latency_ms": 60000},
            {"id": "kb_std", "name": "国标知识库", "kind": "knowledge",
             "skills": ["material", "spec"], "trust": 0.6, "latency_ms": 150},
            {"id": "mes", "name": "MES系统", "kind": "system",
             "skills": ["inventory"], "trust": 0.85, "latency_ms": 300,
             "channel": {"type": "http", "url": f"http://127.0.0.1:{SYS_PORT}/help"}},
            {"id": "agent_calc", "name": "算力agent", "kind": "agent",
             "skills": ["math"], "trust": 0.7},
        ):
            post_json("/collaborators", c)
        pool = get_json("/collaborators")["collaborators"]
        kinds = sorted({c["kind"] for c in pool})
        assert kinds == ["agent", "human", "knowledge", "system"], kinds
        m_mat = get_json("/collaborators?need=material&top=3")["match"]
        m_inv = get_json("/collaborators?need=inventory&top=3")["match"]
        assert m_mat[0]["id"] == "expert_zhang", m_mat
        assert m_inv[0]["id"] == "mes", m_inv
        print(f"A 协作者一等公民：{len(pool)} 位同池 {kinds}；"
              f"material→{m_mat[0]['name']} · inventory→{m_inv[0]['name']} ✓")

        # ── B 外部系统真的被拉进电路（真实跨进程 HTTP 求援）──
        s = post_json("/topology/session",
                      {"spec": _spec("库存核对", ["inventory"]), "seed": 1,
                       "coop_timeout": 8.0})
        sid_b = s["session_id"]
        t0 = time.time()
        log_b = None
        while time.time() - t0 < 20:
            log_b = get_json(f"/help/log/{sid_b}")
            if log_b["coop_log"] and log_b["coop_log"][0]["status"] != "pending":
                break
            time.sleep(0.15)
        cl = log_b["coop_log"]
        assert cl and cl[0]["status"] == "answered", cl
        assert cl[0]["assignee"] == "mes", cl
        assert "137 件" in (cl[0]["answer"] or ""), cl
        assert _SYS_HITS, "外部系统未收到真实 HTTP 求援"
        st_b = get_json(f"/topology/state/{sid_b}")
        assert st_b["node_traces"]["r1"]["helped_by"] == "mes"
        assert st_b["node_traces"]["r1"]["output"] == cl[0]["answer"]
        print(f"B 外部系统入环：机器主动 POST 到 MES（真实跨进程），"
              f"答案「{cl[0]['answer']}」已回灌节点输出 ✓")

        # ── C 主动点名真人 + E 过程实时可见 ──
        s = post_json("/topology/session",
                      {"spec": _spec("工艺判定", ["process"], kinds=["human"],
                                     timeout=12.0),
                       "seed": 1, "coop_timeout": 12.0})
        sid_c = s["session_id"]
        watcher = SSEWatcher(f"/topology/stream/{sid_c}", budget=22)
        watcher.start()

        seen = {}

        def _human_side():
            """另一个"人"：独立 HTTP 客户端，轮询自己的待办箱并作答。"""
            for _ in range(400):
                try:
                    p = get_json("/help/pending?assignee=expert_zhang")["pending"]
                except Exception:
                    p = []
                if p:
                    seen["hid"] = p[0]["hid"]
                    seen["q"] = p[0]["question"]
                    post_json(f"/help/{p[0]['hid']}/respond",
                              {"value": "改用 6061-T6，回火 175℃×8h",
                               "by": "expert_zhang", "confidence": 0.96})
                    return
                time.sleep(0.05)

        th = threading.Thread(target=_human_side, daemon=True)
        th.start()
        t0 = time.time()
        while time.time() - t0 < 25:
            log_c = get_json(f"/help/log/{sid_c}")
            if log_c["coop_log"] and log_c["coop_log"][0]["status"] == "answered":
                break
            time.sleep(0.15)
        th.join(timeout=3)
        cl = log_c["coop_log"]
        assert seen.get("hid"), "人类待办箱里没收到机器的点名"
        assert cl and cl[0]["status"] == "answered" and cl[0]["assignee"] == "expert_zhang", cl
        st_c = get_json(f"/topology/state/{sid_c}")
        assert st_c["node_traces"]["r1"]["helped_by"] == "expert_zhang"
        assert st_c["node_traces"]["r1"]["output"] == "改用 6061-T6，回火 175℃×8h"
        print(f"C 主动点名真人：机器发问「{seen['q'][:28]}…」→ 张工在待办箱作答 → "
              f"答案回灌节点、质量转正 ✓")

        # ── D 自动升级 + 无人可解不吊死 ──
        get_json("/collaborators")  # noop
        s = post_json("/topology/session",
                      {"spec": _spec("工艺升级", ["process"], kinds=["human"],
                                     timeout=0.6),
                       "seed": 1, "coop_timeout": 0.6})
        sid_d = s["session_id"]
        t0 = time.time()
        while time.time() - t0 < 20:
            log_d = get_json(f"/help/log/{sid_d}")
            if log_d["coop_log"] and log_d["coop_log"][0]["status"] != "pending":
                break
            time.sleep(0.15)
        cl = log_d["coop_log"]
        tried = [d["to"] for d in cl[0]["dispatch_log"]]
        assert tried[0] == "expert_zhang", tried
        assert "expert_wang" in tried, f"首选无响应应顺位到备份专家，实际 {tried}"
        assert cl[0]["status"] == "unanswered", cl
        s = post_json("/topology/session",
                      {"spec": _spec("炼金", ["quantum_alchemy"]), "seed": 1,
                       "coop_timeout": 0.4})
        sid_d2 = s["session_id"]
        t0 = time.time()
        ok_done = False
        while time.time() - t0 < 20:
            st = get_json(f"/topology/state/{sid_d2}")
            if st["state"] == "done":
                ok_done = True
                break
            time.sleep(0.15)
        cl2 = get_json(f"/help/log/{sid_d2}")["coop_log"]
        assert ok_done, "无人可解时电路应照常收敛，不得吊死"
        assert cl2 and cl2[0]["status"] == "unanswered", cl2
        print(f"D 自动升级不吊死：{tried[0]}→{tried[1]} 顺位接管；"
              f"无人可解诚实上报 unanswered，电路仍收敛到 done ✓")

        # ── E 实时可见（SSE）──
        time.sleep(0.6)
        watcher.stop()
        watcher.join(timeout=6)
        ts = watcher.types()
        for need in ("help_open", "help_dispatch", "help_waiting_human",
                     "help_answered", "help_applied"):
            assert need in ts, f"SSE 未收到 {need}，实收 {sorted(set(ts))}"
        dispatch = [e for e in watcher.events if e.get("type") == "help_dispatch"]
        applied = [e for e in watcher.events if e.get("type") == "help_applied"]
        print(f"E 过程实时可见：SSE 收到 {len(watcher.events)} 帧，"
              f"含 派单→{dispatch[0].get('name')} · 生效 by={applied[0].get('by')} "
              f"(kind={applied[0].get('kind')}) ✓")

        # ── 信任自校准（答得上的↑、掉链子的↓）──
        pool = {c["id"]: c for c in get_json("/collaborators")["collaborators"]}
        print(f"  信任自校准：mes {0.85}→{pool['mes']['trust']} · "
              f"expert_zhang {0.92}→{pool['expert_zhang']['trust']} · "
              f"expert_wang {0.5}→{pool['expert_wang']['trust']}")
        assert pool["mes"]["trust"] > 0.85
        assert pool["expert_wang"]["trust"] < 0.5

        print("\n✓ _live_coop_check.py 全过：人机协同已达极致——"
              "协作者不分人机，机器主动引入资源与人。")
        return 0
    finally:
        if watcher:
            watcher.stop()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        mes.shutdown()


if __name__ == "__main__":
    sys.exit(main())
