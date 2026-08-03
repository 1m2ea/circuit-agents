"""
circuit-agents · executor_poc.py  (B 阶段：最小闭环 PoC，独立文件，不改 runtime.py)
================================================================================
证明 CircuitExecutor 的"手和眼"：
  节点 reason 声明需要 china_gdp_2024，但上游根本没产出它 → 第一次跑 gate:fail_linear
  → 执行器自动派发 filler 技能（execute_skill）去"检索"补数 → 合成信号 → 自动重跑 → ok。

全程用 SimBackend（无 LLM、无 key），证明"技能不再封在图纸上"——执行器主动调，
不依赖 LLM 在场。补数闭环发生在执行器内部，不等人工判断。
"""
from __future__ import annotations

import json
import sys
import os

# 仓库根在 sys.path（从根目录 python executor_poc.py 运行）
from runtime import Circuit, Signal, SimBackend
from compiler.agent_skills import SKILLS, execute_skill


# ---------------------------------------------------------------------------
# 演示用 filler 技能：确定性"检索"（真实 execute_skill 调用路径，避免依赖联网）
# 注册进 SKILLS，证明执行器派发的是货真价实的技能，而非假动作。
# ---------------------------------------------------------------------------
def _demo_web(query: str) -> str:
    # 真实场景这里会是 web_search；PoC 用确定性文本，保证离线可复现、可断言。
    return f"[demo_web 检索结果] {query} → China nominal GDP 2024 ≈ 18.94 trillion USD (demo source)"


SKILLS["demo_web"] = {
    "name": "demo_web",
    "description": "演示用确定性检索（PoC 替代真实 web_search，离线可复现）",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    "handler": _demo_web,
}


class CircuitExecutor:
    """B 阶段最小实现：包装 Circuit，补"自动补数据闭环 + 动态技能派发"。

    复用：circuit.layers() / circuit._run_one() / circuit.backend.run() /
         agent_skills.execute_skill()。不触碰 Signal / Circuit 核心语义。
    """

    def __init__(self, circuit: Circuit, data_fill_budget: int = 2, skills_enabled: bool = True):
        self.circuit = circuit
        self.budget = data_fill_budget
        self.skills_enabled = skills_enabled
        self.state = {"_fetched": {}, "_skills_used": [], "_trace": []}

    # ---- 执行器主动派发（D 的动态技能调用核心）----
    def dispatch(self, cid: str, spec: dict) -> str:
        name = spec.get("skill")
        args = spec.get("args", {})
        if self.skills_enabled and name:
            self.state["_skills_used"].append(name)
            self.state["_trace"].append({"action": "dispatch_skill", "node": cid,
                                          "skill": name, "args": args})
            return execute_skill(name, json.dumps(args))
        return f"[no-skill-fill:{name}]"

    # ---- 自动补数据：对 missing 列表逐个派发 filler，写回 state._fetched ----
    def _auto_fill(self, cid: str, missing: list):
        comp = self.circuit.components[cid]
        fillers = comp.get("fillers") or {}
        for m in missing:
            if m in self.state["_fetched"]:
                continue
            spec = fillers.get(m) or {"skill": "demo_web", "args": {"query": m}}
            self.state["_fetched"][m] = self.dispatch(cid, spec)

    # ---- 带补给信号重跑该节点（绕过线性关系闸，因为数据已由执行器补齐）----
    def _rerun_with_filled(self, cid: str, out: dict):
        comp = self.circuit.components[cid]
        real_inputs = [out[p] for p in self.circuit.pred[cid] if p in out]
        # 把已补到的数据合成"虚拟前驱信号"，携带 produced_outputs=缺失名
        synth = [Signal(value=self.state["_fetched"][m], quality=0.6, ok=True,
                        meta={"produced_outputs": [m], "auto_filled": True})
                 for m in (comp.get("required_inputs") or [])
                 if m in self.state["_fetched"]]
        sig = self.circuit.backend.run(comp, real_inputs + synth)
        # 复刻 _run_one 的 produced_outputs 盖章（便于下游核对）
        upstream = set()
        for s in real_inputs + synth:
            if s is not None and s.ok:
                upstream.update(s.meta.get("produced_outputs") or [])
        own = comp.get("produced_outputs") or []
        combined = list(dict.fromkeys(list(own) + list(upstream)))
        if combined:
            sig.meta["produced_outputs"] = combined
        return sig

    # ---- 忠实复刻 runtime.Circuit.propagate 内的 _run_one 线性关系闸 ----
    # （B 阶段内联；C 阶段该逻辑会被提升为 Circuit 的正式方法，此处即可直接复用）
    def _check_and_run(self, cid, out):
        comp = self.circuit.components[cid]
        ins = [out[p] for p in self.circuit.pred[cid] if p in out]
        req = comp.get("required_inputs")
        if req:
            input_map = comp.get("input_map") or {}        # 命名漂移符号映射表
            available = set()
            for p in self.circuit.pred[cid]:
                s = out.get(p)
                if s is not None and s.ok:
                    available.update(s.meta.get("produced_outputs") or [])
            missing = []
            for r in req:
                actual = input_map.get(r, r)
                if actual not in available:
                    missing.append(r)
            if missing:
                return Signal(value=None, quality=0.0, ok=False,
                              cost=0.0, latency_ms=0.0,
                              meta={"gate": "fail_linear", "missing": missing,
                                    "required": list(req), "node": cid})
        sig = self.circuit.backend.run(comp, ins)
        upstream = set()
        for s in ins:
            if s is not None and s.ok:
                upstream.update(s.meta.get("produced_outputs") or [])
        own = comp.get("produced_outputs") or []
        combined = list(dict.fromkeys(list(own) + list(upstream)))
        if combined:
            sig.meta["produced_outputs"] = combined
        return sig

    # ---- 分层 propagate + 闭环补数 ----
    def run(self):
        out = {}
        for layer in self.circuit.layers():
            for cid in layer:
                sig = self._check_and_run(cid, out)        # 线性关系闸 + backend.run
                b = self.budget
                while sig.meta.get("gate") == "fail_linear" and b > 0:
                    self._auto_fill(cid, sig.meta.get("missing", []))
                    sig = self._rerun_with_filled(cid, out)
                    b -= 1
                    self.state["_trace"].append(
                        {"action": "retry_after_fill", "node": cid,
                         "ok": sig.ok, "budget_left": b})
                out[cid] = sig
        # 汇总（与 Circuit.execute 同构的精简版）
        terminals = [c for c in self.circuit.components if not self.circuit.succ[c]]
        fq = max((out[c].quality for c in terminals), default=0.0)
        return {
            "success": all(out[c].ok for c in terminals),
            "final_quality": round(fq, 3),
            "components": {c: {"ok": s.ok, "quality": round(s.quality, 3),
                               "gate": s.meta.get("gate")} for c, s in out.items()},
            "state": self.state,
        }


def _build_spec():
    return {
        "name": "poc_drift_fill",
        "components": {
            "src": {"type": "power", "label": "task"},
            # reason 需要 china_gdp_2024，但没有任何上游产出它 → 必 gate:fail_linear
            "reason": {
                "type": "resistor", "label": "reason", "model": "small",
                "required_inputs": ["china_gdp_2024"],
                "produced_outputs": ["report"],
                # 执行器据此知道"缺 china_gdp_2024 时去 demo_web 检索"
                "fillers": {"china_gdp_2024": {"skill": "demo_web",
                                               "args": {"query": "china gdp 2024"}}},
            },
        },
        "wires": [["src", "reason"]],
    }


def main():
    spec = _build_spec()
    backend = SimBackend(__import__("random").Random(0))

    # --- 对照：不用执行器（仅 Circuit.propagate），节点应 gate:fail_linear ---
    bare = Circuit(spec, backend)
    out_bare, _, _ = bare.propagate()
    assert not out_bare["reason"].ok, "对照：无执行器时 reason 应失败"
    assert out_bare["reason"].meta.get("gate") == "fail_linear", "对照：应 gate:fail_linear"
    print("✓ 对照（无 CircuitExecutor）：reason 缺 china_gdp_2024 → gate:fail_linear（证明缺口存在）")

    # --- 用 CircuitExecutor：自动补数闭环 ---
    ex = CircuitExecutor(Circuit(spec, SimBackend(__import__("random").Random(0))),
                         data_fill_budget=2)
    res = ex.run()
    assert res["components"]["reason"]["ok"], "CircuitExecutor 应自动补数使 reason ok"
    assert "china_gdp_2024" in res["state"]["_fetched"], "state._fetched 应有补到的数据"
    assert "demo_web" in res["state"]["_skills_used"], "应真实调用了 execute_skill(demo_web)"
    print("✓ CircuitExecutor：reason 先 gate:fail_linear → 执行器自动 demo_web 补数 → 重跑 ok")
    print(f"  final_quality={res['final_quality']}  补到的数据={res['state']['_fetched']}")
    print(f"  技能调用链={res['state']['_skills_used']}")
    print(f"  trace={json.dumps(res['state']['_trace'], ensure_ascii=False)}")

    print("\n[PoC B] 闭环反馈·自动补数据 + 动态技能调用 验证通过 ✓（SimBackend，无 LLM/无 key）")


if __name__ == "__main__":
    main()
