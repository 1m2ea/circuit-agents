# -*- coding: utf-8 -*-
"""验证 topology_editor.html 依赖的完整 HTTP 链路（与前端 api() 同契约）。"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8772"

def call(path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"}, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

PRESET = {
    "name": "双检索合并",
    "components": {
        "src": {"type": "power", "label": "任务"},
        "r1": {"type": "resistor", "label": "检索A", "model": "small", "produced_outputs": ["a"]},
        "r2": {"type": "resistor", "label": "检索B", "model": "small", "produced_outputs": ["b"]},
        "sum": {"type": "resistor", "label": "合并摘要", "model": "small", "produced_outputs": ["s"]},
        "adc": {"type": "adc", "label": "质量门", "threshold": 0.8}
    },
    "wires": [["src","r1"],["src","r2"],["r1","sum"],["r2","sum"],["sum","adc"]]
}

print("[1] 启动会话（每节点 600ms）...")
r = call("/topology/session", {"spec": PRESET, "seed": 1, "node_delay_ms": 600})
sid = r["session_id"]
print("    sid =", sid)

print("[2] 轮询一次，确认 done_nodes/current_layer 字段...")
st = call(f"/topology/state/{sid}")
assert "done_nodes" in st and "current_layer" in st, "缺少仪表盘字段"
print("    state=%s done_nodes=%s current_layer=%s" % (st["state"], st["done_nodes"], st["current_layer"]))

print("[3] 暂停...")
print("    pause ->", call(f"/topology/pause/{sid}")["paused"])

print("[4] 四类编辑（与仪表盘按钮一一对应）...")
# 替换：把 r1 升 large
e1 = call(f"/topology/edit/{sid}", {"op": "replace", "cid": "r1", "comp": {"model": "large", "label": "检索A"}})
print("    replace r1 ->", e1["edit"]["applied"], "components=", e1["state"]["components"])
# 插入：在 src->r1 之间插入校验节点
e2 = call(f"/topology/edit/{sid}", {"op": "insert", "u": "src", "v": "r1", "new_cid": "ins_verify", "comp": {"type": "verify", "label": "校验", "threshold": 0.8}})
print("    insert ins_verify src->r1 ->", e2["edit"]["applied"], "wires has src->ins_verify:", ["src","ins_verify"] in e2["state"]["wires"])
# 追加并行支路：在 r2 后
e3 = call(f"/topology/edit/{sid}", {"op": "append_parallel", "cid": "r2", "new_cid": "par_1", "comp": {"type": "resistor", "label": "并行支路", "model": "small", "produced_outputs": ["o"]}})
print("    append_parallel par_1 after r2 ->", e3["edit"]["applied"], "par_1 in components:", "par_1" in e3["state"]["components"])
# 调质量门
e4 = call(f"/topology/edit/{sid}", {"op": "set_gate", "cid": "adc", "threshold": 0.5})
print("    set_gate adc -> 0.5 ->", e4["edit"]["applied"])

print("[5] 恢复执行，等待完成...")
call(f"/topology/resume/{sid}")
for _ in range(200):
    st = call(f"/topology/state/{sid}")
    if st.get("done"):
        break
    time.sleep(0.1)
res = st.get("result", {})
print("    完成 state=%s success=%s final_quality=%s failed=%s" % (
    st["state"], res.get("success"), res.get("final_quality"), res.get("failed_nodes")))
assert st["state"] == "done", "应执行完成"
assert res.get("success") is True, "编辑后的拓扑应成功"
print("[6] 编辑器路由返回 HTML 校验...")
html = urllib.request.urlopen(BASE + "/topology/editor", timeout=10).read().decode()
assert "<html" in html.lower() and "人在回路" in html, "编辑器页面内容缺失"
print("    /topology/editor 返回 %d 字节 HTML（含『人在回路』）✓" % len(html))
print("\n仪表盘依赖链路全部验证通过 ✓")
