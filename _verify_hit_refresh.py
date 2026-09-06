"""memory-hit 串味 bug · 根治回归验证（离线、零网络、隔离记忆）。

缺陷（2026-09-06 real 基准首跑定位）：
  goal 共享前缀时 mem.recall 跨任务命中缓存拓扑，缓存 src 电源节点烘焙的
  旧任务 task 文本原样喂给 LLM（且浅拷贝使改写泄漏回记忆库）。

修复（compiler/compile.py hit 路径）：deepcopy 缓存 spec + 当前 goal 重参数化
  power 节点 task/label。

本验证四断言：
  R1 hit 路径被触发（memory_hit 标注存在）
  R2 复用 spec 的 src.task == 当前 goal 文本（旧任务文本已刷新）
  R3 电源节点实际发出的 Signal.value == 当前 goal 文本（LLM 上下游不再串味）
  R4 记忆库未被污染（历史条目的 src.task 仍是旧任务文本、电阻未被织入新教训）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt
from compiler.compile import compile_goal
from compiler.goal import Goal
from compiler.topology_memory import TopologyMemory

_tmpd = tempfile.mkdtemp(prefix="hitfix_")
_mem_path = os.path.join(_tmpd, "mem.json")
_orig_init = TopologyMemory.__init__
TopologyMemory.__init__ = lambda self, path=None: _orig_init(
    self, path if path is not None else _mem_path)

A_DESC = "翻译 AI/电路领域英文句子为中文，使用准确术语｜The resistor limits current in the circuit."
B_DESC = "翻译 AI/电路领域英文句子为中文，使用准确术语｜The capacitor stores electric charge temporarily."


def main():
    # ---- 任务 A：正常编译 + 高质成功落库（制造可被命中的缓存拓扑）----
    spec_a = compile_goal(Goal(capabilities=["translate"], description=A_DESC),
                          memory_enabled=False)
    spec_a["goal_desc"] = A_DESC
    mem = TopologyMemory()
    mem.record(A_DESC, spec_a, {"success": True, "final_quality": 0.9,
                                "total_latency_ms": 1.0, "total_cost": 0.0,
                                "components": {}})

    # ---- 任务 B：共享前缀 → 应命中 A 的缓存拓扑并复用 ----
    spec_b = compile_goal(Goal(capabilities=["translate"], description=B_DESC),
                          memory_enabled=True)

    # R1 hit 路径被触发
    assert spec_b.get("memory_hit"), "R1 失败：共享前缀应命中 memory-hit 路径"

    # R2 复用 spec 的 src.task 已刷新为当前 goal（不再有 A 的句子）
    src_b = next(c for c in spec_b["components"].values()
                 if c.get("type") == "power")
    assert src_b.get("task") == B_DESC, \
        f"R2 失败：src.task 未刷新，仍是：{src_b.get('task')!r}"
    assert A_DESC not in str(src_b.get("task")), "R2 失败：src.task 残留旧任务文本"

    # R3 电源节点实际发出的信号值 == 当前 goal 文本
    from runtime import SimBackend
    out = SimBackend(__import__("random").Random(0)).run(src_b, [])
    assert out.value == B_DESC, \
        f"R3 失败：power 节点发出的仍是旧任务文本：{out.value!r}"

    # R4 记忆库未被污染：历史条目 spec 的 src.task 仍是 A 的文本、
    #    电阻组件没有被本次 compile 织入的 memory_lessons
    fresh = TopologyMemory()
    entry = fresh._store["entries"][0]
    src_stored = next(c for c in entry["spec"]["components"].values()
                      if c.get("type") == "power")
    assert src_stored.get("task") == A_DESC, \
        f"R4 失败：记忆库被本次编译污染，src.task 变成：{src_stored.get('task')!r}"
    res_stored = next(c for c in entry["spec"]["components"].values()
                      if c.get("type") == "resistor")
    assert not res_stored.get("memory_lessons"), \
        "R4 失败：织入教训泄漏进了记忆库的历史条目"

    print("✓ R1 hit 路径触发（memory_hit 标注存在）")
    print("✓ R2 复用 spec 的 src.task == 当前 goal（旧任务文本已刷新）")
    print("✓ R3 power 节点实际发出当前 goal 文本（上下游不串味）")
    print("✓ R4 记忆库未被污染（历史条目 spec 原样）")
    print("\nmemory-hit 串味 bug · 根治回归验证全部通过 ✓")


if __name__ == "__main__":
    main()
