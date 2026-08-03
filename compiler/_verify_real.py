"""
circuit-agents · compiler._verify_real
=====================================
真·在线验证 RealLLMBackend 接 DeepSeek（OpenAI-compatible）。

安全约定（与用户约定，避免 key 泄露到对话）：
 · 本脚本只读环境变量 DEEPSEEK_API_KEY（退回 OPENAI_API_KEY / AGENT_API_KEY）；
 · key 明文绝不在脚本里硬编码，也绝不 print（只打印长度）。
 · AGENT_API_BASE 缺省默认 https://api.deepseek.com/v1。
 · 运行方式（key 通过文件注入，明文不进命令/对话）：
     DEEPSEEK_API_KEY="$(cat /c/Users/lgw12/key_tmp.txt)" \
       python circuit-agents/compiler/_verify_real.py
   （沙箱不继承本机全局环境变量，故必须文件注入；cat 读文件，明文只存在于进程内存。）
"""
from __future__ import annotations

import argparse
import os
import random
import sys

# 让脚本无论从哪个 cwd 运行都能 import runtime / compiler 包
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from compiler.goal import Goal                 # noqa: E402
from compiler.compile import compile_goal       # noqa: E402
from compiler.backend_llm import RealLLMBackend  # noqa: E402
from runtime import Circuit                     # noqa: E402


DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL_MAP = {
    "small": "deepseek-chat",
    "tool":  "deepseek-chat",
    "large": "deepseek-reasoner",
}


def main():
    parser = argparse.ArgumentParser(description="DeepSeek 在线验证 RealLLMBackend")
    parser.add_argument("--large-cap", default=None,
                        help="把某能力显式绑 large(→deepseek-reasoner)，如 --large-cap reason")
    parser.add_argument("--task", default=None,
                        help="给 power 节点注入真实任务内容，让整链有真实上下文可处理")
    args = parser.parse_args()

    api_key = ((os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("AGENT_API_KEY")) or "").strip().lstrip("\ufeff")
    base_url = os.environ.get("AGENT_API_BASE") or DEEPSEEK_BASE

    if not api_key:
        print("✗ 未检测到 API key。请通过环境变量注入（DEEPSEEK_API_KEY），例如：")
        print('    DEEPSEEK_API_KEY="$(cat /c/Users/lgw12/key_tmp.txt)" '
              "python circuit-agents/compiler/_verify_real.py [--large-cap reason]")
        sys.exit(2)

    print(f"· base_url = {base_url}")
    print(f"· model_map = {DEEPSEEK_MODEL_MAP}")
    print(f"· api_key 已加载 (len={len(api_key)})，明文不展示")

    # 真实可跑的小目标：retrieve → reason → verify（链式依赖，含 1 次反馈环）
    goal = Goal.from_dict({
        "name": "deepseek_live_smoke",
        "description": "在线冒烟：DeepSeek 真模型跑 retrieve→reason→verify 链",
        "capabilities": ["retrieve", "reason", "verify"],
        "constraints": {"min_quality": 0.6},
        "reliability": "normal",
        "dependencies": [["retrieve", "reason"], ["reason", "verify"]],
        "feedback": {"max_iter": 1},
    })

    # 默认：Binder 自动选最便宜达标档（全 small → deepseek-chat）。
    # --large-cap <能力>：关 auto_bind，手动把该能力绑 large → deepseek-reasoner（验证推理模型接线）。
    if args.large_cap:
        if args.large_cap not in goal.capabilities:
            print(f"✗ --large-cap 必须是指定能力之一：{goal.capabilities}")
            sys.exit(2)
        goal.tiers = {c: ("large" if c == args.large_cap else "small")
                      for c in goal.capabilities}
        spec = compile_goal(goal, auto_bind=False, route=True)
        print(f"· 强制 {args.large_cap} → large (deepseek-reasoner)")
    else:
        spec = compile_goal(goal, auto_bind=True, route=True)
    print(f"· 编译出 {len(spec['components'])} 个组件，"
          f"反馈环 max_iter={spec.get('feedback', {}).get('max_iter')}")

    # --task：给 power 节点注入真实任务内容（让 retrieve→reason→verify 有真实上下文）
    if args.task:
        for _cid, _comp in spec["components"].items():
            if _comp.get("type") == "power":
                _comp["task"] = args.task
                _comp["label"] = args.task
                print(f"· 注入真实任务到 power: {args.task}")
                break

    backend = RealLLMBackend(
        rng=random.Random(0),
        api_key=api_key,
        base_url=base_url,
        model_map=DEEPSEEK_MODEL_MAP,
        timeout=60.0,
    )
    circuit = Circuit(spec, backend)

    # 单次 propagate（max_iter=1 等价于 execute 的一次前向），同时取 meta 用于展示真实输出
    out, total_lat, total_cost = circuit.propagate()

    # 复刻 execute() 的成败判定
    fb = circuit.feedback
    adc_id = (fb or {}).get("from")
    if adc_id and out.get(adc_id):
        fq = out[adc_id].quality
        success = out[adc_id].ok
    else:
        terminals = [c for c in circuit.components if not circuit.succ[c]]
        fq = max((out[c].quality for c in terminals), default=0.0)
        success = all(out[c].ok for c in terminals)

    print("\n=== 在线执行结果 ===")
    print(f"success        : {success}")
    print(f"final_quality  : {round(fq, 3)}")
    print(f"total_cost     : {round(total_cost, 4)}")
    print(f"total_latency  : {round(total_lat, 1)} ms")

    print("\n组件明细:")
    for cid, comp in spec["components"].items():
        s = out.get(cid)
        if s is None:
            continue
        tag = comp.get("type")
        print(f"  {cid:>10} [{tag:<14}] ok={s.ok}  q={round(s.quality,3)}  "
              f"cost={round(s.cost,4)}  lat={round(s.latency_ms,1)}ms")

    # 各 resistor 的真实 LLM 输出（截断），证明真模型在跑（非模拟）
    print("\n=== 各 resistor 的真实 LLM 输出（截断 200 字）===")
    shown = 0
    for cid, comp in spec["components"].items():
        if comp.get("type") != "resistor":
            continue
        s = out.get(cid)
        if s is None or s.value is None:
            print(f"  [{cid}] (无输出, ok={s.ok if s else '?'})")
            shown += 1
            continue
        val = s.value if isinstance(s.value, str) else repr(s.value)
        snippet = val[:200].replace("\n", " ")
        model = s.meta.get("model", "?")
        finish = s.meta.get("finish_reason", "?")
        print(f"  [{cid}] model={model} finish={finish} ok={s.ok} -> {snippet}")
        shown += 1
    if shown == 0:
        print("  (无 resistor 输出)")

    print("\n✓ 在线验证完成（真模型已实际调用）")


if __name__ == "__main__":
    main()
