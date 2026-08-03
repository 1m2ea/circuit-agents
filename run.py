"""CLI: run a Circuit DSL spec with the SimBackend.

Examples
--------
  python run.py examples/parallel.json --runs 400 --seed 42
  python run.py examples/feedback.json --runs 400 --seed 7
"""
import argparse
import json
import random
import statistics
import sys

from runtime import Circuit, SimBackend, load
from compiler.backend_llm import get_default_backend, resolve_api_key


def _build_backend(mode, rng):
    """按 --backend 模式构造后端。

    auto（默认）= 解析到 key 走真模型，否则 SimBackend；
    real = 强制真模型（无 key 报错退出）；
    mock/sim = 强制 SimBackend 离线对照。
    """
    if mode in ("mock", "sim"):
        return SimBackend(rng)
    if mode == "real":
        key = resolve_api_key()
        if not key:
            print("✗ 未检测到 API key，无法使用 --backend real"
                  "（设 DEEPSEEK_API_KEY 或放 ~/Desktop/key_tmp.txt）")
            sys.exit(2)
        from compiler.llm_agents import LLMAgentBackend
        return LLMAgentBackend(api_key=key)
    # auto（默认）
    return get_default_backend(rng=rng)


def main():
    ap = argparse.ArgumentParser(description="Run a Circuit DSL topology.")
    ap.add_argument("spec", help="path to a topology JSON")
    ap.add_argument("--runs", type=int, default=1,
                    help="number of stochastic runs to average over")
    ap.add_argument("--seed", type=int, default=None,
                    help="base seed (each run gets seed+i)")
    ap.add_argument("--backend", choices=["auto", "real", "mock", "sim"],
                    default="auto",
                    help="auto=有 key 走真模型否则 SimBackend(默认)；"
                         "real=强制真模型(无 key 报错)；mock/sim=强制 SimBackend 离线对照")
    args = ap.parse_args()

    spec = load(args.spec)

    if args.runs <= 1:
        rng = random.Random(args.seed)
        res = Circuit(spec, _build_backend(args.backend, rng)).execute()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    costs, lats, qs, succ = [], [], [], []
    for i in range(args.runs):
        seed = None if args.seed is None else args.seed + i
        rng = random.Random(seed)
        res = Circuit(spec, _build_backend(args.backend, rng)).execute()
        costs.append(res["total_cost"])
        lats.append(res["total_latency_ms"])
        qs.append(res["final_quality"])
        succ.append(1 if res["success"] else 0)

    print(json.dumps({
        "spec": spec.get("name"),
        "runs": args.runs,
        "success_rate": round(statistics.mean(succ), 3),
        "avg_cost": round(statistics.mean(costs), 4),
        "avg_latency_ms": round(statistics.mean(lats), 1),
        "avg_final_quality": round(statistics.mean(qs), 3),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
