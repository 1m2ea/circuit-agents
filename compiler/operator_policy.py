"""
circuit-agents · compiler.operator_policy
=========================================
P1 纵轴优化（MetaRSI）：把算子内部策略从硬编码常量提升为【可写面】。

背景
----
MetaRSI 的两条编排轴：
  · **横轴** horizontal orchestration —— 决定「用哪个算子、什么顺序」。
    circuit-agents 本来就有：UCB1 选臂 + 爬山接受（见 rl_optimizer）。
  · **纵轴** vertical optimization —— 决定「算子内部怎么改」，即改**策略本身**。
    circuit-agents 原本**完全没有**：`ACTIONS` 里全是硬编码常量——
    `add_verify` 的 `threshold=0.6`、`add_capacitor` 的 `mode="all"`、
    `swap_model` 的候选档位 `_TIERS`。

本模块补的就是纵轴：
  · `OperatorPolicy`   —— 算子的可写参数，含冻结集与边界夹紧
  · `VerticalSubAgent` —— 规则式改写策略（按历史收益调参，全程可审计）

为什么有些参数要冻结
--------------------
`add_capacitor.mode` 这类离散语义参数（all=全部到达 / any=任一存活）
一改就改变**数据流完整性语义**，影响远大于收益，且不可逆推。
纵轴改写只放开**连续、可夹紧、语义单调**的参数，其余标 FROZEN 留给人工。

诚实边界（务必读）
------------------
· 规则是**启发式**的，不是学出来的。方向基于语义常识：
  收益好 → 适度加严（把有效手段用足）；收益差 → 放宽（止损）。
· **离线状态下规则无法被效果验证** —— SimBackend 的模拟质量对
  `verify threshold` 类参数不敏感（实测见 holdout.py 的辨别力表：
  add_verify / drop_verify 的 mean_delta 恒为 0.0）。
  本模块保证的是「机制接线正确、改动可审计、可夹紧、可回滚」，
  **不是「一定涨分」**。接真后端后其收益才可被 holdout 度量。
· 一切改动写进 `OperatorPolicy.history`，可回放、可复核。

⚠️ 实测警告（2026-09-10）：默认关闭，开启后当前是**净负收益**
----------------------------------------------------------------
用同一 spec 跑 4 个 seed 的 A/B（RLOptimizer，episodes=30/patience=15）：

| seed | 纵轴 OFF | 纵轴 ON | 结果 |
|------|---------|---------|------|
| 7    | 0.175   | 0.0875  | 更差 |
| 3    | 0.1975  | 0.1015  | 更差 |
| 11   | 0.1975  | 0.1975  | 持平 |
| 42   | 0.4492  | 0.0875  | 大幅更差 |

**3 差 1 平 0 好**。全部由 `swap_model:tiers` 收缩触发。

根因：`avg_gain` 取的是**累计均值**（`arm.total / arm.n`），
每臂样本量极小（个位数）时，早期一两次负收益就把均值拉负 →
MIN_TRIED=3 的护栏根本不够 → 过早砍掉 `tool` 档 → 搜索空间收窄 →
搜不到真正更省的档位组合。

结论：规则式纵轴**在反馈信号不可靠时会帮倒忙**（MetaRSI 定律五的现场版——
循环不创造能力，没有可靠外部信号时它只会把噪声固化成策略）。
故 `RLOptimizer(vertical=...)` 默认 `False`；机制完整保留，
待接真后端后用 `holdout` 度量其收益再决定是否开启。
届时若开启，建议先把 MIN_TRIED 提到 8+ 或改用中位数/置信下界替代均值。
"""
from __future__ import annotations

import json
from typing import Optional

# ---------------------------------------------------------------------------
# 可写面：算子内部策略
# ---------------------------------------------------------------------------
DEFAULT_POLICY: dict = {
    "swap_model":    {"tiers": ["small", "large", "tool"]},
    "add_verify":    {"threshold": 0.6},
    "drop_verify":   {},
    "parallelize":   {},
    "add_capacitor": {"mode": "all"},
}

# 冻结参数：语义影响大 / 离散不可逆 → 绝不自动改写，留给人工
FROZEN: dict = {
    "add_capacitor": ("mode",),
}

# 连续参数的夹紧边界与步长 (lo, hi, step)：防止策略跑飞
BOUNDS: dict = {
    "add_verify": {"threshold": (0.3, 0.9, 0.05)},
}

# 触发纵轴改写的历史收益阈值（与 OperatorPolicy 同处一室便于调参）
MIN_TRIED = 3          # 样本太少不动（避免单点抖动误调）
CONTRACT_BELOW = -0.01  # 平均收益低于此 → 收缩/放宽
EXPAND_ABOVE = 0.03     # 平均收益高于此 → 加严/放开


class OperatorPolicy:
    """算子的可写策略。默认等价于改前的硬编码常量（零回归的前提）。"""

    def __init__(self, policy: Optional[dict] = None):
        self.p = json.loads(json.dumps(policy or DEFAULT_POLICY))
        self.history: list = []          # 审计轨迹：每次改写留痕

    # ---- 读 ----
    def get(self, op: str, key: str, default=None):
        return (self.p.get(op) or {}).get(key, default)

    def for_op(self, op: str) -> dict:
        """传给 ACTIONS 算子的参数字典（算子侧只认 dict，解耦本模块）。"""
        return dict(self.p.get(op) or {})

    def is_frozen(self, op: str, key: str) -> bool:
        return key in FROZEN.get(op, ())

    # ---- 写（带冻结检查 + 边界夹紧）----
    def set(self, op: str, key: str, value, reason: str = "",
            round_no: int = 0) -> bool:
        """写入策略参数。返回是否真的改动（冻结/无变化 → False）。"""
        if self.is_frozen(op, key):
            self.history.append({"op": op, "param": key, "action": "rejected",
                                 "reason": f"FROZEN 参数不自动改（{reason}）",
                                 "round": round_no})
            return False
        slot = self.p.setdefault(op, {})
        old = slot.get(key)
        b = (BOUNDS.get(op) or {}).get(key)
        if b is not None and isinstance(value, (int, float)):
            lo, hi, _step = b
            value = round(max(lo, min(hi, float(value))), 4)
        if old == value:
            return False
        slot[key] = value
        self.history.append({"op": op, "param": key, "action": "set",
                             "from": old, "to": value, "reason": reason,
                             "round": round_no})
        return True

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.p))


class VerticalSubAgent:
    """纵轴 Sub-Agent：接受 RSI² Agent 指令，**改写算子内部的 RSI 规则**。

    与横轴的区别（一句话）：
        横轴改「选谁」(UCB1)，纵轴改「被选中后怎么干」(本模块)。
    """

    def __init__(self, min_tried: int = MIN_TRIED,
                 contract_below: float = CONTRACT_BELOW,
                 expand_above: float = EXPAND_ABOVE):
        self.min_tried = int(min_tried)
        self.contract_below = float(contract_below)
        self.expand_above = float(expand_above)

    def revise(self, policy: OperatorPolicy, arm_stats: dict,
               round_no: int = 0) -> list:
        """按各算子历史收益改写策略。返回本次实际改动列表（可审计）。"""
        changes: list = []
        for op, st in (arm_stats or {}).items():
            if not isinstance(st, dict):
                continue
            if int(st.get("tried", 0) or 0) < self.min_tried:
                continue                       # 样本不足，不妄动
            g = float(st.get("avg_gain", 0.0) or 0.0)
            if g < self.contract_below:
                changes += self._contract(policy, op, g, round_no)
            elif g > self.expand_above:
                changes += self._expand(policy, op, g, round_no)
        return changes

    # ---- 收益差 → 收缩/放宽（止损）----
    def _contract(self, policy: OperatorPolicy, op: str, g: float,
                  r: int) -> list:
        if op == "add_verify":
            old = policy.get("add_verify", "threshold", 0.6)
            _lo, _hi, step = BOUNDS["add_verify"]["threshold"]
            if policy.set("add_verify", "threshold", old - step,
                          reason=f"avg_gain={g} 为负 → 放宽校验门止损", round_no=r):
                return [{"op": op, "action": "contract", "param": "threshold",
                         "from": old, "to": policy.get("add_verify", "threshold"),
                         "avg_gain": g, "round": r}]
        if op == "swap_model":
            tiers = list(policy.get("swap_model", "tiers") or [])
            if "tool" in tiers and len(tiers) > 2:
                new = [t for t in tiers if t != "tool"]
                if policy.set("swap_model", "tiers", new,
                              reason=f"avg_gain={g} 为负 → 去掉最贵档 tool 控成本",
                              round_no=r):
                    return [{"op": op, "action": "contract", "param": "tiers",
                             "from": tiers, "to": new, "avg_gain": g, "round": r}]
        return []

    # ---- 收益好 → 加严/放开（把有效手段用足）----
    def _expand(self, policy: OperatorPolicy, op: str, g: float,
                r: int) -> list:
        if op == "add_verify":
            old = policy.get("add_verify", "threshold", 0.6)
            _lo, _hi, step = BOUNDS["add_verify"]["threshold"]
            if policy.set("add_verify", "threshold", old + step,
                          reason=f"avg_gain={g} 为正 → 适度加严校验提质量",
                          round_no=r):
                return [{"op": op, "action": "expand", "param": "threshold",
                         "from": old, "to": policy.get("add_verify", "threshold"),
                         "avg_gain": g, "round": r}]
        if op == "swap_model":
            tiers = list(policy.get("swap_model", "tiers") or [])
            if "tool" not in tiers:
                new = list(DEFAULT_POLICY["swap_model"]["tiers"])
                if policy.set("swap_model", "tiers", new,
                              reason=f"avg_gain={g} 为正 → 恢复全档位探索空间",
                              round_no=r):
                    return [{"op": op, "action": "expand", "param": "tiers",
                             "from": tiers, "to": new, "avg_gain": g, "round": r}]
        return []
