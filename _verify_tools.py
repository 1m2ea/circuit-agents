"""① + ③ 离线验证（零网络、零成本）。

③ 用注入式 fake_post 验证工具循环：第1轮返回 tool_calls(query_db) →
   本地 execute_skill 真实执行（本地 grep，无网络）→ 回灌 tool 消息 → 第2轮给最终回答。
① 用 SimBackend 验证声明 skills 的节点执行前真实派发，以及 netlister 编译期自动挂载。
"""
import json
import random
import sys

sys.path.insert(0, r"D:\dev\projects\666\circuit-agents")
from compiler.backend_llm import RealLLMBackend  # noqa: E402
from runtime import Signal, Circuit, CircuitExecutor  # noqa: E402
import runtime as rt  # noqa: E402

# ---------- ③ 工具循环（离线，注入假响应） ----------
calls = {"n": 0}


def fake_post(url, headers, body):
    calls["n"] += 1
    if calls["n"] == 1:
        return {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "query_db",
                                         "arguments": json.dumps(
                                             {"query": "ShareRepo"},
                                             ensure_ascii=False)}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    roles = [m["role"] for m in body["messages"]]
    assert "tool" in roles, f"第2轮请求应含回灌的 tool 消息, roles={roles}"
    tool_msg = [m for m in body["messages"] if m["role"] == "tool"][0]
    assert "topology_repo" in tool_msg["content"] or "share" in tool_msg["content"].lower() \
        or len(tool_msg["content"]) > 0, "tool 消息应携带真实 grep 结果"
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "最终回答：基于真实源码……"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8}}


b = RealLLMBackend(dry_run=False, http_post=fake_post)
comp = {"type": "resistor", "label": "retrieve", "model": "small"}
ins = [Signal(value="任务上下文", quality=1.0, ok=True)]
sig = b.run(comp, ins)
print("[③] tool_rounds:", sig.meta.get("tool_rounds"),
      " tools_used:", sig.meta.get("tools_used"),
      " ok:", sig.ok)
assert sig.ok and sig.meta.get("tool_rounds") == 1, "应完成 1 轮工具调用"
assert sig.meta["tools_used"] == ["query_db"], "应记录真实调用的技能名"
print("PASS ③ 工具循环：模型决定调 query_db → 本地执行 → 回灌 → 最终回答\n")

# ---------- ① 静态派发（SimBackend，零网络） ----------
spec = {
    "name": "t", "task": "静态派发测试",
    "components": {
        "src": {"type": "power", "label": "t", "produced_outputs": ["task_in"]},
        "cap_0": {"type": "resistor", "label": "retrieve", "model": "small",
                  "produced_outputs": ["out1"],
                  "skills": [{"skill": "query_db",
                              "args": {"query": "ShareRepo"}}]},
    },
    "wires": [["src", "cap_0"]],
}
ex = CircuitExecutor(Circuit(spec, rt.SimBackend(random.Random(0))),
                     skills_enabled=True)
res = ex.run()
print("[①] _skills_used:", ex.state["_skills_used"])
assert "query_db" in ex.state["_skills_used"], "声明的技能应在执行前被派发"
print("PASS ① 静态派发：节点声明 skills → 执行前真实派发 query_db\n")

# ---------- ① 编译期自动挂载 ----------
from compiler.netlister import Netlister  # noqa: E402
import inspect  # noqa: E402
from compiler.goal import Goal  # noqa: E402

try:
    g = Goal(name="g", description="检索项目存储层设计真实 API",
             capabilities=["retrieve", "reason"],
             tiers={"retrieve": "small", "reason": "small"})
except TypeError:
    sigs = str(inspect.signature(Goal.__init__))
    raise SystemExit(f"Goal 构造签名不匹配: {sigs}")
spec2 = Netlister().compile(g)
sk = spec2["components"]["cap_0"].get("skills")
print("[①] netlister 对 retrieve 挂载:", sk)
assert sk and sk[0]["skill"] == "query_db", "retrieve 节点应自动挂 query_db"
assert "skills" not in spec2["components"]["cap_1"], "reason 节点不应挂技能"
print("PASS ① 编译期自动挂载：retrieve→query_db，reason 不挂")

# ---------- ③ 强制收口：达轮数上限仍想调工具 → 必须给出最终交付 ----------
calls2 = {"n": 0}


def fake_post_cap(url, headers, body):
    calls2["n"] += 1
    if "tools" in body:   # 前 5 次：带 tools 的请求都返回 tool_calls（模型贪读不止）
        return {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{calls2['n']}", "type": "function",
                            "function": {"name": "query_db",
                                         "arguments": json.dumps({"query": "x"})}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    assert calls2["n"] == 6, f"第6次请求应为强制收口（无 tools），实际第{calls2['n']}次"
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "最终交付内容（强制收口后）"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 9}}


b2 = RealLLMBackend(dry_run=False, http_post=fake_post_cap)
sig2 = b2.run({"type": "resistor", "label": "retrieve", "model": "small"},
              [Signal(value="ctx", quality=1.0, ok=True)])
print("[③] 强制收口: rounds =", sig2.meta.get("tool_rounds"),
      " value =", sig2.value)
assert sig2.value == "最终交付内容（强制收口后）", "达上限后必须拿到最终交付而非中间独白"
assert sig2.meta["tool_rounds"] == 5, "4 轮正常 + 1 轮挂起调用执行"
print("PASS ③ 强制收口：不再把『Let me read...』中间独白当成品返回\n")

print("\n=== 全部离线验证通过 ===")
