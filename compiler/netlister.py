"""
circuit-agents · compiler.netlister
=================================
M0 阶段：Goal → Circuit DSL 网表（components + wires + rationale）。

这是"功能级"降低，刻意保持简单、保证 *可运行*：
  - power + opamp(调度器, spec_clarify 依 reliability)
  - 模态 → source 节点；>1 模态用 bridge_rectifier 统一，
           单模态用 capacitor(上下文缓冲) 汇合
  - capabilities → 一串串联的 resistor(transformer) 智能体
  - 终端 adc 质量评估（阈值 = min_quality）

需要*优化*的结构决策（并行 / 反馈环 / 冗余 / 精确型号档绑定）留给
Router(M2) + Binder(M1) + Optimizer(M3)。Netlister 只保证产出
一份合法、可被 runtime.py 直接 load/execute 的网表。
"""
from __future__ import annotations

from .goal import VALID_TIERS, Goal


class Netlister:
    def __init__(self, default_tier: str = "small"):
        if default_tier not in VALID_TIERS:
            raise ValueError("default_tier 必须是 small/large/tool")
        self.default_tier = default_tier

    # ---- 公共前缀：power + 调度器 + 模态统一 ----
    # Router(M2) 复用这一段，保证"前置块"只有一份真相源、零重复。
    def _prefix(self, goal: Goal):
        """返回 (components, wires, head)。
        head = 进入第一条能力的节点（sched / cmerge / bridge）。"""
        comps: dict = {}
        wires: list = []
        # 把任务的"真·描述"挂到电源节点上：runtime.SimBackend.run 的 power 分支
        # 返回 comp.get("task", comp.get("label",""))，而 propagate() 只读组件与连线、
        # 从不读 spec["task"] 顶层字段——所以必须让电源节点自身带 task，否则任务描述
        # 永远到不了下游电阻（历史 bug：电阻只收到 goal.name 兜底串 "unnamed-goal"）。
        comps["src"] = {"type": "power", "label": goal.name or "任务",
                        "task": goal.description or "自动编译目标"}
        clarify = goal.reliability in ("high", "normal")
        comps["sched"] = {"type": "opamp", "label": "调度器", "spec_clarify": clarify}
        wires.append(["src", "sched"])

        modalities = goal.modalities
        if not modalities:
            head = "sched"
        elif len(modalities) == 1:
            m = modalities[0]
            comps["mod_in"] = {"type": "source", "label": f"输入[{m}]", "quality": 0.95}
            comps["cmerge"] = {"type": "capacitor", "label": "上下文汇合"}
            wires.append(["sched", "cmerge"])
            wires.append(["mod_in", "cmerge"])
            head = "cmerge"
        else:
            comps["bridge"] = {"type": "bridge_rectifier", "label": "整流桥"}
            wires.append(["sched", "bridge"])
            for m in modalities:
                sid = f"mod_{m}"
                comps[sid] = {"type": "source", "label": f"输入[{m}]", "quality": 0.95}
                wires.append([sid, "bridge"])
            head = "bridge"
        return comps, wires, head

    def compile(self, goal: Goal) -> dict:
        comps, wires, head = self._prefix(goal)
        clarify = goal.reliability in ("high", "normal")

        # --- 能力 → 串联 transformer 电阻 ---
        prev = head
        for i, cap in enumerate(goal.capabilities):
            rid = f"cap_{i}"
            tier = goal.tiers.get(cap, self.default_tier)
            comps[rid] = {"type": "resistor", "label": cap, "model": tier,
                          "recovery": goal.recovery}
            wires.append([prev, rid])
            prev = rid

        # --- 终端：adc 质量评估 ---
        thr = goal.constraints.get("min_quality", 0.8)
        comps["adc"] = {"type": "adc", "label": "质量评估", "threshold": thr}
        wires.append([prev, "adc"])

        spec = {
            "name": goal.name,
            "task": goal.description or "自动编译目标",
            "components": comps,
            "wires": wires,
            "rationale": self._rationale(goal, clarify, goal.modalities),
        }
        return spec

    @staticmethod
    def _rationale(goal, clarify, modalities) -> str:
        bits = [f"调度器 spec_clarify={clarify}(reliability={goal.reliability})"]
        if not modalities:
            bits.append("无模态输入，直接从调度器进入能力链")
        elif len(modalities) == 1:
            bits.append(f"单模态 {modalities[0]} → 电容汇合")
        else:
            bits.append(f"多模态 {modalities} → 整流桥统一")
        bits.append("能力链(串联): " + " → ".join(goal.capabilities))
        bits.append(f"终端 adc 阈值=min_quality={goal.constraints.get('min_quality', 0.8)}")
        bits.append("注：型号档由 Binder(M1) 优化；并行/反馈/冗余由 Router(M2) 决定")
        return " | ".join(bits)
