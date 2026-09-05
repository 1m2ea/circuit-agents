"""离线验证 A 修复的抽取逻辑（用 SimBackend，零成本、零网络）。

验证三件事：
1. CircuitExecutor 跑完后 ex._results 是否真的持有各节点 Signal；
2. _extract_node_values 能否从中抽出 by_node / by_output；
3. 抽出内容非空（证明"正文一直在，只是没往外拿"）。
不发起任何真实 API 调用。
"""
import importlib.util
import random
import sys

sys.path.insert(0, r"D:\dev\projects\666\circuit-agents")
import runtime as rt  # noqa: E402
from runtime import Circuit, SimBackend  # noqa: E402

spec = {
    "name": "plumb_test",
    "task": "测试正文抽取",
    "components": {
        "src": {"type": "power", "label": "plumb_test", "task": "测试正文抽取",
                "ref": "P1"},
        "cap_0": {"type": "resistor", "label": "reason", "model": "small",
                  "recovery": 0.0, "required_inputs": [],
                  "produced_outputs": ["design_doc"]},
        "cap_1": {"type": "resistor", "label": "organize", "model": "small",
                  "recovery": 0.0, "required_inputs": ["design_doc"],
                  "produced_outputs": ["final_doc"]},
        "adc": {"type": "adc", "label": "质量评估", "threshold": 0.5},
    },
    "wires": [["src", "cap_0"], ["cap_0", "cap_1"], ["cap_1", "adc"]],
}

circuit = Circuit(spec, SimBackend(random.Random(0)))
ex = rt.CircuitExecutor(circuit)
res = ex.run()

results = getattr(ex, "_results", None) or {}
print("[1] ex._results 存在:", hasattr(ex, "_results"), "节点数:", len(results))
print("    result['success']:", res.get("success"),
      " final_quality:", res.get("final_quality"))

# 载入 plan.py 里的抽取函数（plan.py 有 __main__ 守卫，import 不会执行 main）
p = r"C:\Users\lgw12\.workbuddy\skills\circuit-planner\scripts\plan.py"
sp = importlib.util.spec_from_file_location("planmod", p)
plan = importlib.util.module_from_spec(sp)
sp.loader.exec_module(plan)

nv = plan._extract_node_values(results)
print("[2] by_node 键:", list(nv["by_node"].keys()))
print("    by_output 键:", list(nv["by_output"].keys()))

print("[3] 抽出的正文（截断预览）:")
for k, v in nv["by_output"].items():
    s = " ".join(str(v).split())
    print(f"    ▸ {k}: {s[:150]}{' …' if len(s) > 150 else ''}")

ok = bool(results) and bool(nv["by_output"])
print("\n结论:", "PASS 抽取逻辑可用（正文确实一直在，只是没往外拿）"
      if ok else "FAIL 抽取为空，需继续排查")
sys.exit(0 if ok else 1)
