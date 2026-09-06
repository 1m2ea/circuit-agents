"""P2 教训闭环自动化 · 离线端到端验证（零外部依赖、零网络）。

验证链：
  V1 失败 run → _auto_lesson 自动沉淀教训（runtime 真实执行路径，SimBackend）
  V2 成功 run → 不制造教训（防教训库污染）
  V3 重复失败 → 去重（教训不重复入库）
  V4 常驻注入：语义召回零命中 → compile 仍织入最近教训（教训库不失联）
  V5 提示词真正携带教训（backend_llm._lessons_block → dry_run messages）
"""
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

# 隔离记忆文件：绝不污染真实 .topology_memory.json
_tmpdir = tempfile.mkdtemp(prefix="p2verify_")
_mem_path = os.path.join(_tmpdir, "mem.json")
os.environ.setdefault("TZ", "Asia/Shanghai")

# 把 TopologyMemory 默认路径劫持到隔离文件（ monkeypatch 类默认构造）
_orig_init = TopologyMemory.__init__


def _patched_init(self, path=None):
    _orig_init(self, path if path is not None else _mem_path)


TopologyMemory.__init__ = _patched_init

FAIL_SPEC = {
    "name": "fail_flow",
    "components": {
        "src": {"type": "power", "label": "task", "produced_outputs": ["task_in"]},
        "r1": {"type": "resistor", "label": "retrieve", "model": "small",
               "required_inputs": ["task_in"], "produced_outputs": ["data"]},
        "r2": {"type": "resistor", "label": "reason", "model": "small",
               "required_inputs": ["data"], "produced_outputs": ["analysis"]},
        "adc": {"type": "adc", "threshold": 0.5},
    },
    "wires": [["src", "r1"], ["r1", "r2"], ["r2", "adc"]],
    "goal_desc": "检索数据并深度分析（P2 验证专用）",
}


def make_fail_executor():
    """构造一个注定失败的执行器：reason 节点在 SimBackend 下被强制开路。

    用 monkeypatch 让 SimBackend 对 label=reason 的电阻返回 ok=False——
    走的是 runtime 真实执行/记录路径，非伪造 result。
    """
    orig = rt.SimBackend.run

    def fail_run(self, comp, inputs):
        sig = orig(self, comp, inputs)
        if comp.get("type") == "resistor" and str(comp.get("label", "")).startswith("reason"):
            return rt.Signal(value=None, quality=0.0, ok=False, cost=0.0,
                             latency_ms=1.0, meta={"open": "p2_test_forced"})
        return sig

    rt.SimBackend.run = fail_run
    return orig


def run_once(fail: bool):
    orig = make_fail_executor() if fail else None
    try:
        ex = CircuitExecutor(Circuit(FAIL_SPEC, rt.SimBackend(random.Random(0))),
                             memory_enabled=True)
        return ex.run()
    finally:
        if orig is not None:
            rt.SimBackend.run = orig


def main():
    mem = TopologyMemory(path=_mem_path)

    # V1 失败 run → 自动沉淀
    res_fail = run_once(fail=True)
    assert res_fail["success"] is False, "注入故障后 run 应失败"
    mem = TopologyMemory(path=_mem_path)
    lessons = [l["text"] for l in mem._store.get("lessons", [])]
    assert lessons, f"失败 run 应自动沉淀教训，实际 {lessons}"
    les = lessons[-1]
    assert "reason(r2)失败" in les and "p2_test_forced" in les, \
        f"教训应含真实失败事实，实际: {les}"
    assert "P2 验证专用" in les, "教训应含任务上下文"
    print(f"✓ V1 失败自动沉淀: {les[:70]}…")

    # V2 成功 run → 不制造教训
    n_before = len(TopologyMemory(path=_mem_path)._store["lessons"])
    res_ok = run_once(fail=False)
    assert res_ok["success"] is True
    n_after = len(TopologyMemory(path=_mem_path)._store["lessons"])
    assert n_after == n_before, f"成功 run 不应制造教训（{n_before}→{n_after}）"
    print("✓ V2 成功不沉淀: 教训库不被无意义成功记录污染")

    # V3 重复失败 → 去重
    run_once(fail=True)
    run_once(fail=True)
    n_dedup = len(TopologyMemory(path=_mem_path)._store["lessons"])
    assert n_dedup == n_before, f"重复失败应去重（预期 {n_before}，实际 {n_dedup}）"
    print("✓ V3 重复去重: 同一失败重复发生只记一次教训")

    # V4 常驻注入：goal 与教训毫无词面重叠 → compile 仍织入最近教训
    spec = compile_goal(Goal(capabilities=["translate"],
                             description="翻译一篇冰岛诗歌并押韵"),
                        memory_enabled=True)
    assert spec.get("memory_lessons"), \
        f"召回零命中也应常驻织入最近教训，实际 {spec.get('memory_lessons')}"
    woven = [c.get("memory_lessons") for c in spec["components"].values()
             if c.get("type") == "resistor"]
    assert all(woven), f"每个电阻都应携带教训，实际 {woven}"
    print(f"✓ V4 常驻注入: 零命中 goal 仍织入 {len(spec['memory_lessons'])} 条最近教训")

    # V5 提示词真正携带（dry_run 看 messages）
    from compiler.backend_llm import RealLLMBackend
    dry = RealLLMBackend(rng=random.Random(0), dry_run=True)
    comp = next(c for c in spec["components"].values()
                if c.get("type") == "resistor")
    sig = dry.run(comp, [rt.Signal(value="ctx", quality=0.9, ok=True)])
    user_msg = next(m["content"] for m in sig.meta["messages"] if m["role"] == "user")
    assert "历史教训" in user_msg and "踩坑" in user_msg, \
        f"提示词应含教训块，实际: {user_msg[:200]}"
    print("✓ V5 提示词携带: _lessons_block 把教训织进真实 prompt")

    print("\nP2 教训闭环自动化 · 端到端验证全部通过 ✓")


if __name__ == "__main__":
    main()
