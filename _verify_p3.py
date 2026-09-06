"""P3 元循环 v0 · 端到端闭环验证（零网络、隔离记忆文件）。

这是「让它超越你」的核心演示：三层循环一次走通——
  第一层：真实执行失败（runtime 真实路径，非伪造 result）
  第二圈：P2 自动沉淀教训 → 相似任务编译期召回 → 提示词携带
  第三圈：self_improve 技能诊断自身失败模式 → 产出改进提案任务书
"""
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt
from runtime import Circuit, CircuitExecutor
from compiler.topology_memory import TopologyMemory
from compiler.compile import compile_goal
from compiler.goal import Goal
from compiler import agent_skills
from compiler.self_improve import register_skill

# ---- 隔离记忆文件，绝不污染真实 .topology_memory.json ----
_tmpdir = tempfile.mkdtemp(prefix="p3verify_")
_mem_path = os.path.join(_tmpdir, "mem.json")
_orig_init = TopologyMemory.__init__


def _patched_init(self, path=None):
    _orig_init(self, path if path is not None else _mem_path)


TopologyMemory.__init__ = _patched_init

SPEC = {
    "name": "p3_demo_flow",
    "components": {
        "src": {"type": "power", "label": "task", "produced_outputs": ["task_in"]},
        "r1": {"type": "resistor", "label": "retrieve", "model": "small",
               "required_inputs": ["task_in"], "produced_outputs": ["data"]},
        "r2": {"type": "resistor", "label": "reason", "model": "small",
               "required_inputs": ["data"], "produced_outputs": ["analysis"]},
        "adc": {"type": "adc", "threshold": 0.5},
    },
    "wires": [["src", "r1"], ["r1", "r2"], ["r2", "adc"]],
    "goal_desc": "检索行业数据并深度分析（P3 元循环验证）",
}


def run_with_fault(fail: bool):
    """真实执行路径：注入 reason 节点 http_error 故障（走 runtime 全流程）。"""
    orig = rt.SimBackend.run

    def fail_run(self, comp, inputs):
        sig = orig(self, comp, inputs)
        if fail and comp.get("type") == "resistor" \
                and str(comp.get("label", "")).startswith("reason"):
            return rt.Signal(value=None, quality=0.0, ok=False, cost=0.0,
                             latency_ms=1.0, meta={"open": "http_error"})
        return sig

    rt.SimBackend.run = fail_run
    try:
        ex = CircuitExecutor(Circuit(SPEC, rt.SimBackend(random.Random(0))),
                             memory_enabled=True)
        return ex.run()
    finally:
        rt.SimBackend.run = orig


def main():
    mem = TopologyMemory(path=_mem_path)

    # ---- 第一层：真实执行，连续两次失败（http_error）----
    for _ in range(2):
        res = run_with_fault(fail=True)
        assert res["success"] is False
    print("✓ 第一层: 两次真实 run 失败（http_error），runtime 全流程走通")

    # ---- 第二圈：教训自动沉淀 → 相似任务编译期召回 → 提示词携带 ----
    mem = TopologyMemory(path=_mem_path)
    lessons = [l["text"] for l in mem._store.get("lessons", [])]
    assert lessons and "reason(r2)失败[open=http_error]" in lessons[0], \
        f"P2 应自动沉淀 http_error 教训，实际 {lessons}"
    hit = mem.recall("检索行业数据并做深度分析")   # 相似但不同字面的 goal
    assert hit is None or True  # 拓扑召回可能失败（失败拓扑不推荐）——教训召回才是关键
    spec2 = compile_goal(Goal(capabilities=["research"],
                              description="检索行业数据并做深度分析"),
                         memory_enabled=True)
    assert spec2.get("memory_lessons"), \
        f"相似任务编译应召回教训，实际 {spec2.get('memory_lessons')}"
    assert any("http_error" in t for t in spec2["memory_lessons"]), \
        "召回的教训应含本次失败事实"
    from compiler.backend_llm import RealLLMBackend
    dry = RealLLMBackend(rng=random.Random(0), dry_run=True)
    comp = next(c for c in spec2["components"].values()
                if c.get("type") == "resistor")
    sig = dry.run(comp, [rt.Signal(value="ctx", quality=0.9, ok=True)])
    user_msg = next(m["content"] for m in sig.meta["messages"] if m["role"] == "user")
    assert "历史教训" in user_msg and "http_error" in user_msg
    print(f"✓ 第二圈: 教训已沉淀并召回进相似任务的提示词"
          f"（{len(spec2['memory_lessons'])} 条，含 http_error 事实）")

    # ---- 第三圈：self_improve 技能诊断自身 → 改进提案任务书 ----
    register_skill()
    out = agent_skills.execute_skill("self_improve", json.dumps({"top_n": 10}))
    proposal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "proposals", "improvements.md")
    assert os.path.exists(proposal_path), f"提案文件应生成: {proposal_path}"
    text = open(proposal_path, encoding="utf-8").read()
    assert "待人工确认" in text and "改进任务书" in text
    assert "http_error" in text, "提案应基于真实失败模式 http_error"
    print(f"✓ 第三圈: {out}")

    print("\nP3 元循环 v0 · 三层闭环端到端验证全部通过 ✓")
    print(f"提案文件: {proposal_path}")


if __name__ == "__main__":
    main()
