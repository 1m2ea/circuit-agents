#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端示例：用 circuit-agents 驱动本地 transformers 模型（真实推理，非 SimBackend）。

前置：先在另一个终端、装有 torch/transformers 的 venv 里起桥：
    python local_llm_bridge.py --offline
（默认监听 127.0.0.1:8000；模型路径见 local_llm_bridge.py 顶部 DEFAULT_MODEL_PATH）

本脚本在 circuit-agents 自己的 venv 里运行，通过 HTTP 把 resistor 步交给本地
Qwen2.5-1.5B 真实推理，并打印模型产出。整条链路零 API 费用、零联网。

    python examples/local_model_demo.py
    python examples/local_model_demo.py --host 127.0.0.1 --port 8000
"""

import argparse
import sys

sys.path.insert(0, ".")

from runtime import Circuit, CircuitExecutor
from compiler.ollama_backend import OllamaBackend


def main():
    ap = argparse.ArgumentParser(description="本地 transformers 模型端到端示例")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="单次本地推理超时（CPU 较慢，默认 180s）")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    # 最小拓扑：source(确定性) -> resistor(真实本地模型) -> adc(终端评估)
    spec = {
        "name": "local_demo",
        "components": {
            "src": {
                "type": "source", "model": "input", "capability": "input",
                "label": "今天天气不错，但项目进度有点滞后。"
                         "请用一句话总结这句话的核心信息。",
                "quality": 0.9,
            },
            "r1": {
                "type": "resistor", "model": "small", "capability": "summarize",
                "label": "总结要点",
                "required_inputs": ["text"], "produced_outputs": ["summary"],
            },
            "adc": {
                "type": "adc", "model": "evaluate", "capability": "evaluate",
                "threshold": 0.5,
            },
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
        "feedback": {},
    }

    # 关键接线：OllamaBackend 走 openai 模式，指向本地桥；
    # 模型名统一映射成占位 "local"（桥忽略该字段，永远用加载的 Qwen）。
    backend = OllamaBackend(
        host=base, api_mode="openai",
        model_map={"small": "local", "large": "local",
                   "tool": "local", "code": "local"},
        timeout=args.timeout,
    )

    captured = {}

    def on_node_done(cid, sig, info):
        captured[cid] = sig
        tag = "REAL-LLM" if sig.meta.get("backend") == "ollama" else "sim"
        val = sig.value
        preview = (val[:160] + "…") if isinstance(val, str) and len(val) > 160 else val
        print(f"  [{cid}] ({tag}) ok={sig.ok} q={sig.quality:.3f} :: {preview!r}",
              flush=True)

    print(f"[demo] 拓扑: src -> r1(resistor, 真实本地模型) -> adc")
    print(f"[demo] 桥地址: {base}/v1/chat/completions  (模型 Qwen2.5-1.5B, CPU)\n")

    ex = CircuitExecutor(Circuit(spec, backend), on_node_done=on_node_done)
    ex.run()

    st = backend.stats()
    print("\n[stats] backend:", st)
    r1 = captured.get("r1")
    if r1 is not None and r1.meta.get("backend") == "ollama":
        print("\n✅ 成功：本地 Qwen2.5-1.5B 真实推理驱动了 resistor 步。")
        print("—— 模型原始产出 ——")
        print(r1.value)
    elif st["failures"] > 0:
        print("\n⚠️  桥不可达（resistor 调用失败）。请确认 local_llm_bridge.py 已启动。")
        print("     错误:", r1.meta.get("error") if r1 else "n/a")


if __name__ == "__main__":
    main()
