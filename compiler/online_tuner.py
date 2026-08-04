"""Phase 2+ · 第四层运行时智能 ① —— 参数化拓扑在线调参（UCB1 多臂老虎机）

问题：RL 离线优化（① old）解决「设计时最优」——在历史数据上搜到最好的拓扑结构，
但它无法响应**运行时变化**——同一任务不同时刻的负载、数据质量、模型可用性都不同。
离线优化告诉你「large 档平均最好」，在线调参告诉你「这一刻用 small 就够了」。

思路（UCB1 多臂老虎机，执行中微调模型选型）：
  1. 每个 (capability_label, tier) 是一个臂，初始每个臂试一次（探索）。
  2. 之后每次电阻执行时，用 UCB1 公式选 arm（mean + c*sqrt(log(total)/count)），
     平衡 exploration（试少用的）与 exploitation（用最好的）。
  3. 执行后 feedback(quality, cost, latency) 更新该臂的累计 reward。
  4. 随着执行轮数增加，收敛到当前环境下的最优档位。

与 RL 离线优化（① old）的分工：
  · rl_optimizer.py：离线搜**拓扑结构**（哪种元件组合最好），跑 N 轮选最优 spec。
  · online_tuner.py：运行时调**模型参数**（这一刻用哪个 tier），每步自适应。

集成：Circuit.__init__ 接受可选 tuner → _run_one 在执行前选 tier、执行后反馈。
若无 tuner → 用 spec 声明的 model（零回归）。

离线安全：Bandit 纯本地统计，无 key、无网络。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────────────────
# Bandit 数据结构
# ──────────────────────────────────────────────────────────

@dataclass
class BanditArm:
    """一个 (capability, tier) 臂的累计统计。"""
    count: int = 0
    reward_sum: float = 0.0
    quality_sum: float = 0.0
    cost_sum: float = 0.0
    latency_sum: float = 0.0

    @property
    def mean_reward(self) -> float:
        return self.reward_sum / self.count if self.count > 0 else 0.0

    @property
    def avg_quality(self) -> float:
        return self.quality_sum / self.count if self.count > 0 else 0.0

    @property
    def avg_cost(self) -> float:
        return self.cost_sum / self.count if self.count > 0 else 0.0

    @property
    def avg_latency(self) -> float:
        return self.latency_sum / self.count if self.count > 0 else 0.0


DEFAULT_WEIGHTS = {"quality": 1.0, "cost": 0.35, "latency": 0.25}
DEFAULT_TIERS = ["small", "large", "tool"]


class OnlineTuner:
    """运行时在线调参器：UCB1 多臂老虎机动态选型。

    用法::

        tuner = OnlineTuner()
        circuit = Circuit(spec, backend, tuner=tuner)
        result = CircuitExecutor(circuit).run()
        # 同一 tuner 跨多次执行积累经验
    """

    def __init__(self, weights: Optional[dict] = None, c_ucb: float = 1.4,
                 seed: int = 0, tiers: Optional[list] = None):
        """
        Parameters
        ----------
        weights : {quality, cost, latency} 权重，默认 1.0/0.35/0.25
        c_ucb : UCB 探索系数，越大越爱探索
        seed : 随机种子，可复现
        tiers : 可用档位列表，默认 ["small", "large", "tool"]
        """
        self.weights = weights or DEFAULT_WEIGHTS
        self.c_ucb = c_ucb
        self.rng = random.Random(seed)
        self.tiers = tiers or list(DEFAULT_TIERS)
        self.arms: dict = {}            # (capability, tier) -> BanditArm
        self.total_rounds = 0
        self.total_feedbacks = 0

    # ---- 选型 ----

    def select_for(self, cid: str, capability: str, ins: list,
                   _components: Optional[dict] = None) -> str:
        """为指定能力的 resistor 节点选择当前最优 tier。

        探索（try-each-once）→ 利用（UCB1）。返回 tier 名。
        """
        self.total_rounds += 1
        arm_keys = [(capability, t) for t in self.tiers]
        for k in arm_keys:
            if k not in self.arms:
                self.arms[k] = BanditArm()
        # 1) 未试过的臂优先（try-each-once）
        untried = [t for c, t in arm_keys if self.arms[(c, t)].count == 0]
        if untried:
            return self.rng.choice(untried)
        # 2) UCB1 选择
        best_key, best_score = None, -float("inf")
        log_total = math.log(max(1, self.total_rounds))
        for k in arm_keys:
            arm = self.arms[k]
            mean = arm.mean_reward
            ucb = mean + self.c_ucb * math.sqrt(log_total / max(1, arm.count))
            if ucb > best_score:
                best_score = ucb
                best_key = k
        return best_key[1] if best_key else self.tiers[0]

    # ---- 反馈 ----

    def feedback(self, cid: str, capability: str, tier: str,
                 quality: float, cost: float, latency_ms: float):
        """执行后反馈，更新该臂的累计统计。"""
        key = (capability, tier)
        if key not in self.arms:
            self.arms[key] = BanditArm()
        arm = self.arms[key]
        arm.count += 1
        arm.quality_sum += max(0.0, min(1.0, quality))
        arm.cost_sum += max(0.0, cost)
        arm.latency_sum += max(0.0, latency_ms)
        # reward = 加权（与 RL 离线优化同公式）
        r = (self.weights["quality"] * max(0.0, quality)
             - self.weights["cost"] * cost * 10        # scale 使与 quality 同量级
             - self.weights["latency"] * latency_ms / 2000)
        arm.reward_sum += r
        self.total_feedbacks += 1

    # ---- 查询 ----

    def arm_stats(self) -> dict:
        """返回所有臂的统计（可解释/可视化）。"""
        out = {}
        for (cap, tier), arm in sorted(self.arms.items()):
            out[f"{cap}|{tier}"] = {
                "count": arm.count,
                "mean_reward": round(arm.mean_reward, 5),
                "avg_quality": round(arm.avg_quality, 4),
                "avg_cost": round(arm.avg_cost, 5),
                "avg_latency_ms": round(arm.avg_latency, 1),
                "ucb": round(arm.mean_reward + (
                    self.c_ucb * math.sqrt(math.log(max(1, self.total_rounds))
                                           / max(1, arm.count))
                    if arm.count > 0 else float("inf")), 5),
            }
        return out

    def best_tier(self, capability: str) -> Optional[str]:
        """查询某能力当前最优档位（按 mean_reward）。"""
        best_t, best_r = None, -float("inf")
        for t in self.tiers:
            arm = self.arms.get((capability, t))
            if arm and arm.count > 0:
                if arm.mean_reward > best_r:
                    best_r = arm.mean_reward
                    best_t = t
        return best_t

    def summary(self) -> dict:
        return {
            "total_rounds": self.total_rounds,
            "total_feedbacks": self.total_feedbacks,
            "arms": len(self.arms),
            "arm_stats": self.arm_stats(),
        }


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def online_tuner_selftest():
    """Phase 2+ ① 在线调参 离线自检。"""
    import os
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import Circuit, SimBackend, CircuitExecutor

    # 1) 每个臂先探索再利用
    tuner = OnlineTuner(seed=0)
    # 构造一个多节点 spec 让 tuner 有足够轮次收敛
    spec = {"name": "tune_demo", "components": {
        "src": {"type": "power", "label": "task"},
        "A": {"type": "resistor", "label": "research", "model": "small",
              "yield": 1.0, "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "analyze", "model": "large",
              "yield": 1.0, "required_inputs": ["a"],
              "produced_outputs": ["b"]},
        "C": {"type": "resistor", "label": "summarize", "model": "small",
              "yield": 1.0, "required_inputs": ["b"]}},
        "wires": [["src", "A"], ["A", "B"], ["B", "C"]]}
    # 跑多轮积累经验
    for _ in range(30):
        be = SimBackend(random.Random(_))
        circ = Circuit(spec, be, tuner=tuner)
        CircuitExecutor(circ).run()
    stats = tuner.arm_stats()
    assert len(stats) >= 3, f"至少 3 个臂，实际 {len(stats)}"
    # 所有臂都应被试过
    for arm in stats.values():
        assert arm["count"] > 0, f"所有臂都应被选中过，实际 {arm}"
    print(f"✓ 在线调参① 探索+利用: {len(stats)} 臂 · "
          f"总轮 {tuner.total_rounds} · 总反馈 {tuner.total_feedbacks}")

    # 2) 最佳 tier 应该学到 tool>large>small（SimBackend 确定性的）
    bt = tuner.best_tier("research")
    # tool 档 accuracy 0.99 > large 0.92 > small 0.70 → tool 最优
    # 但由于 feedback 含 cost/latency 惩罚，large 可能更平衡
    assert bt and bt in ("tool", "large"), \
        f"应学到 tool 或 large 最优，实际 {bt}"
    print(f"✓ 在线调参① 收敛: research 最佳档位 → {bt} "
          f"(tool=0.99acc/0.005cost large=0.92acc/0.02cost)")

    # 3) 零回归：无 tuner 时用 spec 声明的 model
    be2 = SimBackend(random.Random(99))
    circ2 = Circuit(spec, be2)              # 无 tuner
    res2 = CircuitExecutor(circ2).run()
    assert res2["success"], "无 tuner 应正常执行"
    print("✓ 在线调参① 零回归: 无 tuner 时用 spec 声明的 model")

    # 4) tuner 归零后重新收敛
    tuner2 = OnlineTuner(seed=42)
    for _ in range(20):
        be = SimBackend(random.Random(_))
        CircuitExecutor(Circuit(spec, be, tuner=tuner2)).run()
    bt2 = tuner2.best_tier("research")
    assert bt2 and bt2 in ("tool", "large"), \
        f"独立 tuner 也应收敛，实际 {bt2}"
    print(f"✓ 在线调参① 重现: 独立 tuner 收敛到 {bt2} "
          f"(总反馈 {tuner2.total_feedbacks})")

    # 5) summary 可解释
    summ = tuner.summary()
    assert "arm_stats" in summ and summ["total_rounds"] == tuner.total_rounds
    print(f"✓ 在线调参① 可解释性: summary 含 {summ['arms']} 臂统计")

    print("\nPhase 2+ 第四层① 在线调参 离线自检全部通过 ✓")


if __name__ == "__main__":
    online_tuner_selftest()
