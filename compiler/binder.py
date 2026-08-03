"""
circuit-agents · compiler.binder
==============================
M1 阶段：Binder（选型）—— 为每个功能节点选择型号档。

复用 runtime.SimBackend._TIERS（small/large/tool）作为元件库画像，
不重复定义，保证"选型"与"仿真"用同一套数据。

选型策略（贪心基线；M3 Optimizer 再做搜索）：
  - 为满足 min_quality（能力上限约束），每个 capability 必须绑到
    accuracy >= min_quality 的档；
  - 在满足精度的候选里挑 cost 最小者（并列再看 yield 高者）。
  - 若没有任何档的 accuracy >= min_quality → 标记 quality 维度不可行，
    仍退绑最高精度档（tool），交由上游/M3 决策。

cost / latency 的*全局*可行性由 M3 Optimizer 在 runtime 仿真上搜；
Binder 此处只做"精度可达的最低成本选型"，并顺手给一个近似预算评估。
"""
from __future__ import annotations

import runtime
from .goal import Goal

_TIERS = runtime.SimBackend._TIERS  # 复用现有元件库画像


class Binder:
    def __init__(self):
        # 候选按 cost 升序、再 yield 降序排好，便于贪心取"最便宜达标档"
        self._ranked = sorted(
            _TIERS.items(),
            key=lambda kv: (kv[1]["cost"], -kv[1]["yld"]),
        )

    def bind(self, goal: Goal) -> dict:
        """返回 capability -> tier 的绑定字典。"""
        q_min = goal.constraints.get("min_quality", 0.0)
        tiers: dict = {}
        for cap in goal.capabilities:
            chosen = None
            for tier, d in self._ranked:        # cost 最小优先
                if d["accuracy"] >= q_min:
                    chosen = tier
                    break
            if chosen is None:                   # 精度不可达：退绑最高精度档
                chosen = max(_TIERS, key=lambda t: _TIERS[t]["accuracy"])
            tiers[cap] = chosen
        return tiers

    def report(self, goal: Goal, tiers: dict) -> dict:
        """近似预算评估（series 结构；精确指标留给 runtime/Evaluator）。"""
        q_min = goal.constraints.get("min_quality", 0.0)
        caps = [_TIERS[tiers[c]]["accuracy"] for c in goal.capabilities]
        quality_ok = (min(caps) >= q_min) if caps else True
        # 仅估电阻本身的累计成本/延迟；结构开销（opamp/bridge/cap/adc）由 runtime 精算
        cost = sum(_TIERS[tiers[c]]["cost"] for c in goal.capabilities)
        lat = sum(_TIERS[tiers[c]]["latency"] for c in goal.capabilities)
        c = goal.constraints
        budget = {
            "quality_ok": quality_ok,
            "cost_ok": (cost <= c["max_cost"]) if "max_cost" in c else True,
            "latency_ok": (lat <= c["max_latency_ms"]) if "max_latency_ms" in c else True,
        }
        return {
            "tiers": tiers,
            "est_resistor_cost": round(cost, 4),
            "est_resistor_latency_ms": lat,
            "budget": budget,
            "feasible": all(budget.values()),
        }
