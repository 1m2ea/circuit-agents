"""指挥中⼼ 真机端到端验证：通过真实 HTTP 端口 8765 跑通
① 节点工作报告 ② 主动提问→裁决 ③ 人工编辑学习库。"""
import json, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:8765"


def call(path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


spec = {
    "name": "cc_live",
    "components": {
        "src": {"type": "power", "label": "任务", "task": "x"},
        "r1": {"type": "resistor", "label": "检索", "model": "small"},
        "r2": {"type": "resistor", "label": "抽取", "model": "small"},
        "sum": {"type": "resistor", "label": "合并", "model": "small"},
        # ambiguous_band=1.0 强制落入灰色地带 → 主动提问
        "adc": {"type": "adc", "label": "质量门", "threshold": 0.8, "ambiguous_band": 1.0},
    },
    "wires": [["src", "r1"], ["src", "r2"], ["r1", "sum"], ["r2", "sum"], ["sum", "adc"]],
}

print("== 1) 建会话（每节点 200ms，制造可暂停窗口）==")
r = call("/topology/session", {"spec": spec, "seed": 5, "node_delay_ms": 200})
sid = r["session_id"]
print("  session:", sid, "state:", r["state"]["state"])

print("== 2) 轮询直到 adc 主动提问（pending_question）==")
pq = None
for _ in range(400):
    st = call(f"/topology/state/{sid}")
    if st.get("pending_question"):
        pq = st["pending_question"]
        break
    time.sleep(0.02)
assert pq, "应出现 pending_question"
print(f"  主动提问：节点 {pq['label']} score={pq['score']} 选项数={len(pq['options'])}")
print("  选项：", [o["label"] for o in pq["options"]])

print("== 3) ① 节点工作报告（此时 adc 已执行，应有 trace）==")
rep = call(f"/topology/node/{sid}/adc")
t = rep["report"]
print(f"  adc: 模型={t['model']} 质量={t['quality']} 耗时={t['latency_ms']}ms 输出={str(t['output'])[:40]}")

print("== 4) ③ 人工编辑学习库：暂停态下改 sum 模型 + 调质量门 → 应记库 ==")
call(f"/topology/state/{sid}")  # 已在 paused（因提问而暂停）
call(f"/topology/edit/{sid}", {"op": "replace", "cid": "sum",
                               "comp": {"type": "resistor", "label": "合并_v2", "model": "large"}})
call(f"/topology/edit/{sid}", {"op": "set_gate", "cid": "adc", "threshold": 0.6})
ln = call(f"/topology/learnings/{sid}")
print(f"  学习库条目数={len(ln['learnings'])}:", [(l['op'], l['target']) for l in ln['learnings']])
assert len(ln["learnings"]) == 2, "两次编辑应记 2 条"

print("== 5) ② 前序已暂停（提问态）→ 老板作答 high ==")
ans = call(f"/topology/answer/{sid}?choice=high")
print("  answered:", ans["answered"])

print("== 6) 等执行完成 ==")
for _ in range(400):
    st = call(f"/topology/state/{sid}")
    if st.get("done"):
        break
    time.sleep(0.02)
res = st.get("result", {})
print(f"  最终 state={st['state']} success={res.get('success')} 质量={res.get('final_quality')}")
tr = st["node_traces"]["adc"]
print(f"  adc 人类裁决={tr.get('human_verdict')} → ok={res['components']['adc']['ok']}")
assert st["state"] == "done"
assert tr.get("human_verdict") == "high"
assert res["components"]["adc"]["ok"] is True
print("\n✅ 指挥中⼼ 真机端到端全部通过：透明决策报告 / 主动提问裁决 / 人工编辑学习库")
