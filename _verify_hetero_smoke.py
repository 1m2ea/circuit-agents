"""③ 真·异构校验 smoke（零回归断言式）。

主后端   = DeepSeek 云端（deepseek-chat）
verify   = 本地 Qwen2.5-7B-Instruct-Q4（127.0.0.1:8001，local_llm_bridge，OpenAI 兼容）
不同供应商 + 不同模型族 + 不同基础设施 = 真异构（此前只有同供应商 smoke）。

断言：verify# 前缀节点由 VERIFY 后端服务，其余节点由 MAIN 服务；
      verify_backend 未配置的对照场景会打 hetero_verify_unconfigured 告警。
"""
import os
import random
import sys

sys.path.insert(0, r"D:\dev\projects\666\circuit-agents")
import runtime as rt  # noqa: E402
from runtime import Circuit, CircuitExecutor  # noqa: E402
from compiler.backend_llm import resolve_verify_backend  # noqa: E402
from compiler.llm_agents import LLMAgentBackend  # noqa: E402

vb = resolve_verify_backend()
assert vb is not None, "VERIFY_API_KEY 未配置——应经环境变量传入"
vb.timeout = 600.0          # 本地 7B CPU 推理慢，放宽传输超时
vb.enable_tools = False     # 本地桥不玩 function calling，省一轮降级
print("[0] verify 后端:", type(vb).__name__, "base =", vb.base_url)


class TagWrap:
    """记录每个 label 由哪个后端服务（仅 smoke 用，不改 runtime）。"""

    def __init__(self, inner, tag, served):
        self.inner, self.tag, self.served = inner, tag, served

    def run(self, comp, inputs):
        self.served[comp.get("label", "?")] = self.tag
        return self.inner.run(comp, inputs)


served = {}
main = LLMAgentBackend(api_key=os.environ["DEEPSEEK_API_KEY"],
                       base_url="https://api.deepseek.com/v1",
                       enable_tools=False)          # smoke 只验路由，关掉工具控变量
main.timeout = 120.0

spec = {
    "name": "hetero_smoke",
    "task": "给一句话结论：本地检索优于凭记忆硬编",
    "components": {
        "src": {"type": "power", "label": "hetero_smoke",
                "task": "给一句话结论：本地检索优于凭记忆硬编", "ref": "P1"},
        "cap_0": {"type": "resistor", "label": "reason", "model": "tool",
                  "recovery": 0.0, "required_inputs": [],
                  "produced_outputs": ["claim"]},
        "vq": {"type": "resistor", "label": "verify#quality", "model": "tool",
               "recovery": 0.0, "required_inputs": ["claim"],
               "produced_outputs": ["verdict"]},
        "adc": {"type": "adc", "label": "质量评估", "threshold": 0.8},
    },
    "wires": [["src", "cap_0"], ["cap_0", "vq"], ["vq", "adc"]],
}
spec["goal_desc"] = spec["task"]

circuit = Circuit(spec, TagWrap(main, "MAIN", served),
                  verify_backend=TagWrap(vb, "VERIFY", served))
ex = rt.CircuitExecutor(circuit, verify_backend=circuit.verify_backend)
res = ex.run()

print("\n[1] 各节点由谁服务:", served)
print("[2] success =", res.get("success"),
      " final_quality =", res.get("final_quality"),
      " cost =", res.get("total_cost"),
      " latency_ms =", res.get("total_latency_ms"))

assert served.get("reason") == "MAIN", f"reason 应走 MAIN，实际 {served}"
assert served.get("verify#quality") == "VERIFY", \
    f"verify#quality 应走 VERIFY 独立后端，实际 {served}"
assert res.get("success") is True, "链路应成功"

warned = any("hetero_verify_unconfigured" in str(e)
             for e in getattr(ex, "_events", []))
print("[3] 本轮无未配置告警（hetero_verify_unconfigured =", warned, "应为 False）")
assert not warned

print("\n=== 真·异构校验路由 PASS：云端 DeepSeek 主链 + 本地 Qwen 独立校验 ===")
