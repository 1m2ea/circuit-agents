# -*- coding: utf-8 -*-
"""实景验证 B：真实 DeepSeek-R1 导师 + 真实本地 Qwen2.5-7B 学生，跑完整训练闭环。

  失败案例(真实 store) → DeepSeek-R1 分析(真调 api.deepseek.com)
  → 应用优化 → 本地 7B 重跑(真调 127.0.0.1:11434) → 质量门 → 固化

用法: python _live_mentor_deepseek.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from execution_store import ExecutionStore
from mentor import (MentorAgent, mentor_train_cycle, make_ollama_student,
                    default_content_quality, MENTOR_MODEL, MENTOR_BASE)

FAILED_SPEC = {
    "name": "report_extract",
    "components": {
        "pwr": {"type": "power", "label": "pwr"},
        "src": {"type": "source", "label": "读取季度经营简报",
                "produced_outputs": ["raw"]},
        "ext": {"type": "resistor", "label": "抽取关键指标", "capability": "extract",
                "model": "small", "required_inputs": ["raw"],
                "produced_outputs": ["metrics"]},
        "sum": {"type": "resistor", "label": "生成结论摘要", "capability": "summarize",
                "model": "small", "required_inputs": ["metrics"],
                "produced_outputs": ["summary"]},
        "adc": {"type": "adc", "label": "质量门", "model": "small",
                "required_inputs": ["summary"]},
    },
    "wires": [["pwr", "src"], ["src", "ext"], ["ext", "sum"], ["sum", "adc"]],
}


def main():
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("✗ 未设置 DEEPSEEK_API_KEY，无法做真实导师调用")
        return 1
    print(f"导师: {MENTOR_MODEL} @ {MENTOR_BASE}  key={key[:8]}...{key[-4:]}")

    student = make_ollama_student()
    if student is None:
        print("✗ 本地 Ollama 学生不可达（127.0.0.1:11434 / qwen2.5:7b）")
        return 1
    print("学生: Ollama qwen2.5:7b @ http://127.0.0.1:11434  ✓ 可达")

    db = os.path.join(tempfile.mkdtemp(), "live_mentor.db")
    store = ExecutionStore(db)
    store.save("live-fail-001", "从季度经营简报中抽取关键指标并生成结论摘要",
               "failed", FAILED_SPEC, [],
               {"final_quality": 0.28, "failed_nodes": ["ext", "sum"],
                "note": "抽取节点漏项、摘要空泛，质量门未过"},
               ["live", "deepseek-mentor"])
    print(f"失败案例已入库: {db}\n")

    print("── 调用真实 DeepSeek-R1 导师分析中（推理模型，可能 30~120s）……")
    t0 = time.time()
    mentor = MentorAgent(timeout=300)
    try:
        res = mentor_train_cycle(
            store, mentor=mentor, student_backend=student,
            quality_fn=default_content_quality, quality_threshold=0.8,
        )
    except Exception as e:
        print(f"✗ 闭环异常: {type(e).__name__}: {e}")
        return 1
    dt = time.time() - t0

    if not res.get("ok"):
        print(f"✗ 闭环失败: {res.get('reason')}")
        return 1

    plan = res.get("plan", {})
    print(f"\n── 导师方案（耗时 {dt:.1f}s）")
    print(f"  诊断    : {plan.get('diagnosis')}")
    print(f"  理由    : {plan.get('rationale')}")
    for fx in plan.get("node_fixes", []) or []:
        p = (fx.get("prompt") or "")[:80]
        print(f"  节点修复: {fx.get('cid')} → model={fx.get('model')} prompt={p!r}")
    for op in plan.get("topology_ops", []) or []:
        print(f"  拓扑操作: {op.get('op')} after={op.get('after')} "
              f"node={(op.get('node') or {}).get('label')}")
    if plan.get("error"):
        print(f"  ⚠ JSON 解析问题: {plan.get('error')}  raw={str(plan.get('raw'))[:200]}")

    orig_n = len(res["original_spec"].get("components", {}))
    opt_n = len(res["optimized_spec"].get("components", {}))
    print(f"\n── 应用优化: 节点 {orig_n} → {opt_n}")

    print("\n── 学生（本地 7B）重跑结果")
    print(f"  质量: {res.get('before_quality')} → {res.get('after_quality')}")
    print(f"  成功: {res.get('student_success')}  失败节点: {res.get('failed_nodes')}")
    print(f"  质量门: {'通过 ✓' if res.get('quality_gate_passed') else '未过 ✗'}"
          f"  ({res.get('quality_gate_reason')})")
    comps = res["optimized_spec"].get("components", {})
    for cid, val in (res.get("student_outputs") or {}).items():
        if (comps.get(cid) or {}).get("type") == "resistor":
            print(f"  [{cid}] {str(val)[:120]}")
    if res.get("solidified"):
        print(f"  已固化模板: {res['solidified'].get('diagnosis')}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_live_mentor_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
