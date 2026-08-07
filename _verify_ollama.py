"""circuit-agents 走真实 OllamaBackend 本地推理验证（端到端）。

前置：ollama serve 已在 localhost:11434 运行，且已有名为 qwen2.5:7b 的本地模型。
目的：证明 circuit-agents 的 resistor 节点能经 OllamaBackend 调本机 Ollama 真推理（零 API 费用）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from compiler.ollama_backend import OllamaBackend
import runtime as rt

HOST = "http://localhost:11434"


def main():
    backend = OllamaBackend(host=HOST, api_mode="native", timeout=600)

    # 1) 健康度
    ok, detail = backend.health_check()
    print("[health]", ok, detail)
    if not ok:
        print("[!] Ollama 未就绪，退出"); sys.exit(2)

    # 2) 单 resistor 节点真机推理（small → qwen2.5:7b）
    comp = {"type": "resistor", "label": "reason", "model": "small"}
    inp = [rt.Signal(
        value="电路拓扑是一种把复杂任务分解为节点与连线的多智能体编排方法。",
        quality=0.9, ok=True)]

    t0 = time.time()
    s = backend.run(comp, inp)
    print("[result]", repr(s.value))
    print("[quality]", s.quality, "[ok]", s.ok, "[cost]", s.cost)
    print("[latency_ms]", s.latency_ms)
    print("[meta]", {k: v for k, v in s.meta.items() if k != "usage"})
    print("TOTAL %.1fs DONE" % (time.time() - t0))

    # 3) 端到端 Circuit（retrieve→reason→adc）
    spec = {
        "name": "ollama_e2e_live",
        "components": {
            "src": {"type": "power", "label": "task"},
            "ret": {"type": "resistor", "label": "retrieve", "model": "small"},
            "rsn": {"type": "resistor", "label": "reason", "model": "large"},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "ret"], ["ret", "rsn"], ["rsn", "adc"]],
    }
    from runtime import Circuit, CircuitExecutor
    circ = Circuit(spec, backend)
    res = CircuitExecutor(circ).run()
    print("[e2e] final_quality=%.3f success=%s ollama_calls=%d"
          % (res["final_quality"], res["success"], backend.stats()["calls"]))
    if s.ok and res["success"]:
        print("VERIFY OK: circuit-agents 真实本地 Ollama 推理通过")
    else:
        print("VERIFY FAIL")


if __name__ == "__main__":
    main()
