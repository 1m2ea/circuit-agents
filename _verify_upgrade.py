"""②③ 升级离线验证：教训进提示词 + VERIFY 自动插节点。

零网络（除真异构 smoke 已另行验证）；断言式。
"""
import json
import sys

sys.path.insert(0, r"D:\dev\projects\666\circuit-agents")

# ---------- A: 教训注入提示词 ----------
from compiler.backend_llm import RealLLMBackend  # noqa: E402
from runtime import Signal  # noqa: E402

captured = {}


def fake_post(url, headers, body):
    captured["messages"] = body["messages"]
    return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


b = RealLLMBackend(dry_run=False, http_post=fake_post)
comp = {"type": "resistor", "label": "retrieve", "model": "small",
        "memory_lessons": ["retrieve 必须先 query_db 读真实源码，否则幻觉 Redis",
                           "工具循环达上限必须强制收口"]}
sig = b.run(comp, [Signal(value="ctx", quality=1.0, ok=True)])
user_text = "\n".join(m["content"] for m in captured["messages"] if m["role"] == "user")
assert "历史教训" in user_text and "query_db" in user_text, \
    f"提示词应携带教训块，实际:\n{user_text[:300]}"
assert "强制收口" in user_text
print("PASS A: 教训块已进入模型提示词（含 2 条教训文本）")

comp0 = {"type": "resistor", "label": "reason", "model": "small"}
b.run(comp0, [Signal(value="ctx", quality=1.0, ok=True)])
user0 = "\n".join(m["content"] for m in captured["messages"] if m["role"] == "user")
assert "历史教训" not in user0, "无教训组件不应出现教训块"
print("PASS A2: 无教训时不注入（零污染）")

# ---------- B: VERIFY_* 自动插节点 ----------
import os  # noqa: E402
from compiler.compile import compile_goal  # noqa: E402
from compiler.goal import Goal  # noqa: E402

g = Goal(name="t", description="检索并整理项目存储说明",
         capabilities=["retrieve", "reason"],
         tiers={"retrieve": "tool", "reason": "tool"})

# B1: 未配置 VERIFY_* → 不插
os.environ.pop("VERIFY_API_KEY", None)
os.environ.pop("VERIFY_DEEPSEEK_API_KEY", None)
spec_plain = compile_goal(g, memory_enabled=False)
assert "vq" not in spec_plain.get("components", {}), "未配置时不应插节点"
assert not spec_plain.get("hetero_verify")
print("PASS B1: 未配置 VERIFY_* → 拓扑不变（零回归）")

# B2: 配置 VERIFY_* → adc 前插入 vq，布线正确
os.environ["VERIFY_API_KEY"] = "local"
os.environ["VERIFY_API_BASE"] = "http://127.0.0.1:8001/v1"
spec_hv = compile_goal(g, memory_enabled=False)
comps = spec_hv["components"]
assert "vq" in comps, f"配置后应自动插 verify 节点，实际 {list(comps)}"
assert comps["vq"]["label"] == "verify#quality"
wires = spec_hv["wires"]
assert ["vq", "adc"] in wires, "vq 应串在 adc 前"
into_vq = [w for w in wires if w[1] == "vq"]
assert into_vq, "原上游应改接 vq"
assert not any(w[1] == "adc" and w[0] != "vq" for w in wires), "不应再有绕过 vq 直连 adc 的线"
assert spec_hv.get("hetero_verify") is True
print("PASS B2: VERIFY_* 配置 → adc 前自动插 verify#quality，布线改接正确")

# B3: 幂等
spec_hv2 = compile_goal(g, memory_enabled=False)
assert sum(1 for c in spec_hv2["components"].values()
           if str(c.get("label", "")).startswith("verify")) == 1, "重复编译不应叠加节点"
print("PASS B3: 幂等——重复编译不叠加 verify 节点")

# B4: 教训随 compile 织进组件（真实记忆库已种教训；goal 文本须与教训词重叠过阈值）
g_les = Goal(name="t2", description="retrieve 节点检索真实源码，避免幻觉出不存在的存储后端",
             capabilities=["retrieve", "reason"],
             tiers={"retrieve": "tool", "reason": "tool"})
spec_les = compile_goal(g_les, memory_enabled=True)
assert spec_les.get("memory_lessons"), f"应召回教训，实际 {spec_les.get('memory_lessons')}"
res_comp = [c for c in spec_les["components"].values()
            if isinstance(c, dict) and c.get("type") == "resistor"]
assert all(c.get("memory_lessons") for c in res_comp), "每个电阻都应携带教训"
print(f"PASS B4: 编译期教训召回并织进 {len(res_comp)} 个电阻组件")

print("\n=== ②③ 升级离线验证全部通过 ===")
