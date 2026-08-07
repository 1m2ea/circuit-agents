# -*- coding: utf-8 -*-
"""在线拓扑编辑（人在回路）真实 HTTP 端到端演示。
走 /topology/session|pause|edit|resume|state 五个端点，模拟人在任务跑一半时介入。
"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8771"


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


spec = {
    "name": "research_live",
    "components": {
        "src": {"type": "power", "label": "task", "task": "调研"},
        "r1": {"type": "resistor", "label": "检索", "model": "small"},
        "r2": {"type": "resistor", "label": "抽取", "model": "small"},
        "adc": {"type": "adc", "label": "质量门", "threshold": 0.8},
    },
    "wires": [["src", "r1"], ["r1", "r2"], ["r2", "adc"]],
}

print("== 1) 创建会话（每节点 300ms 延迟，制造可暂停窗口）==")
s = _post("/topology/session", {"spec": spec, "node_delay_ms": 300})
sid = s["session_id"]
print("   session_id =", sid, "  初始状态 =", s["state"]["state"])

print("== 2) 立即请求暂停（完成当前并行层后生效）==")
print("   pause ->", _post(f"/topology/pause/{sid}")["paused"])
for _ in range(100):
    if _get(f"/topology/state/{sid}")["state"] == "paused":
        break
    time.sleep(0.02)
st = _get(f"/topology/state/{sid}")
print("   已暂停于层后，活图节点 =", st["components"])

print("== 3) 人在回路：四类运行时安全编辑 ==")
print("   INSERT  在 r1->r2 上插 verify 节点:")
print("    ", _post(f"/topology/edit/{sid}",
      {"op": "insert", "u": "r1", "v": "r2", "new_cid": "v1",
       "comp": {"type": "verify", "label": "核验"}}))
print("   REPLACE r2 升 large 档:")
print("    ", _post(f"/topology/edit/{sid}",
      {"op": "replace", "cid": "r2", "comp": {"model": "large", "label": "抽取_v2"}}))
print("   APPEND  在 r1 后追加并行支路 par1:")
print("    ", _post(f"/topology/edit/{sid}",
      {"op": "append_parallel", "cid": "r1", "new_cid": "par1",
       "comp": {"type": "resistor", "label": "并行抽取", "model": "small"}}))
print("   GATE    把质量门阈值调低到 0.5:")
print("    ", _post(f"/topology/edit/{sid}",
      {"op": "set_gate", "cid": "adc", "threshold": 0.5}))
st = _get(f"/topology/state/{sid}")
print("   编辑后活图节点 =", sorted(st["components"]))
print("   adc 阈值 =", st["components"])  # 仅打印对照

print("== 4) 恢复执行，等待完成 ==")
print("   resume ->", _post(f"/topology/resume/{sid}")["resumed"])
for _ in range(200):
    stt = _get(f"/topology/state/{sid}")
    if stt.get("done"):
        break
    time.sleep(0.05)
res = stt["result"]
print("   最终 success =", res["success"], "  最终质量 =", res["final_quality"])
print("   已执行节点 =", sorted(res["components"].keys()))
print("   v1(插入) 是否执行 =", "v1" in res["components"],
      "  par1(并行) 是否执行 =", "par1" in res["components"])
print("\n✓ HTTP 端到端：人在回路窗口内完成 插入/替换/并行/调阈值，恢复后新节点产出汇入下游。")
