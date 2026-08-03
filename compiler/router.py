"""
circuit-agents · compiler.router
==============================
M2 阶段：Router（布局布线）—— 把"绑定图"按依赖关系分层，
套用标准单元拓扑模板做成完整、可被 runtime.py 直接执行的 Circuit DSL。

本步落地：标准单元 #2「并联分流 + 电容汇合」
  - dependencies 缺失(None) → 退化成线性串联（M0/M1 行为，向后兼容）
  - dependencies = []（显式空列表）→ 所有能力互不依赖 → 同层全并联
  - dependencies = [[pre, post], ...] → 按 DAG 做 Kahn 分层（同层并联 / 跨层串联）

延迟语义完全交给 runtime.py 的 layer 化 propagate()：
  · 同层 → 延迟取 max（并联，互不等待）
  · 跨层 → 延迟求和（串联，逐级等待）
因此 Router 的"布线"本身就决定了系统级延迟，runtime 一行不用改。

反馈环(标准单元 #3) 与 冗余(标准单元 #5) 均已实现：
  · 反馈环：末级汇合(all-fired)门控整链重试（runtime 原生单环）。
  · 冗余：capability 可声明 K>=2 副本并联，由 capacitor(mode="any") 收口
    （runtime 为该汇合加最小 mode 开关，默认 all，现有拓扑零变化）。
"""
from __future__ import annotations

from .goal import VALID_TIERS, Goal
from .formats import infer_adapters, PREFIX
from .netlister import Netlister


class Router:
    def __init__(self, default_tier: str = "small"):
        if default_tier not in VALID_TIERS:
            raise ValueError("default_tier 必须是 small/large/tool")
        self.default_tier = default_tier

    # ---- DAG 构建 ----
    @staticmethod
    def _build_dag_from(caps: list, deps):
        """返回 (adj, indeg)，节点用 capability 下标索引。
        deps=None → 线性串联（向后兼容）；[] → 全并联（无内部边）；list → DAG 边。"""
        n = len(caps)
        idx = {c: i for i, c in enumerate(caps)}
        adj = {i: [] for i in range(n)}
        indeg = {i: 0 for i in range(n)}
        if deps is None:
            for i in range(n - 1):
                adj[i].append(i + 1)
                indeg[i + 1] += 1
        else:
            for pre, post in deps:
                a, b = idx[pre], idx[post]
                adj[a].append(b)
                indeg[b] += 1
        return adj, indeg

    @staticmethod
    def _kahn(n, adj, indeg):
        """拓扑分层：返回 [[层0下标...], [层1下标...], ...]。
        环检测交给 Goal.from_dict；此处仅兜底。"""
        indeg = dict(indeg)
        ready = sorted([i for i in range(n) if indeg[i] == 0])
        layers = []
        while ready:
            layers.append(ready)
            nxt = []
            for u in ready:
                for v in adj[u]:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        nxt.append(v)
            ready = sorted(nxt)
        if sum(len(l) for l in layers) != n:
            raise ValueError("capability DAG 存在环，无法拓扑分层")
        return layers

    # ---- 主入口 ----
    def route(self, goal: Goal, tiers=None, no_adapters: bool = False) -> dict:
        if tiers:
            goal.tiers = dict(tiers)
        nl = Netlister(default_tier=self.default_tier)
        comps, wires, head = nl._prefix(goal)   # 复用前缀：src/sched/模态统一
        comp_io = goal.component_io or {}        # 子任务 IO 映射（线性关系自测用）

        # 第二层②：格式校验适配器 —— 规划层扫描有效边，格式断点自动插 ADC/DAC 节点。
        # 返回增强后的 caps/deps（在原能力之间插入合成适配器能力名）与 adapters 记录。
        if no_adapters:
            caps = list(goal.capabilities)
            aug_deps = (None if goal.dependencies is None
                        else [list(e) for e in goal.dependencies])
            adapters = {}
        else:
            caps, aug_deps, adapters = infer_adapters(goal)
        n = len(caps)
        adj, indeg = self._build_dag_from(caps, aug_deps)
        layers = self._kahn(n, adj, indeg)

        cap_id = lambda i: f"cap_{i}"
        prev_merge = head
        red = goal.redundancy or {}
        for li, layer in enumerate(layers):
            # 本层输出汇合到电容；最后一层用 pmerge，中间层用 lmerge_{li}
            merge_id = "pmerge" if li == len(layers) - 1 else f"lmerge_{li}"
            comps[merge_id] = {
                "type": "capacitor",
                "label": ("并联汇合" if len(layers) == 1 else f"并行汇合L{li}"),
            }
            for i in layer:
                cap = caps[i]
                # 第二层②：合成格式适配节点（ADC/DAC/transcode）—— 确定性透传，
                # 不占 LLM 档位/成本，仅桥接相邻节点的格式断点。
                if cap.startswith(PREFIX):
                    info = adapters[cap]
                    cid = cap_id(i)
                    comps[cid] = {
                        "type": "format_adapter",
                        "label": f"{info['kind'].upper()} 适配",
                        "from_fmt": info["from_fmt"],
                        "to_fmt": info["to_fmt"],
                        "kind": info["kind"],
                    }
                    wires.append([prev_merge, cid])
                    wires.append([cid, merge_id])
                    continue
                k = red.get(cap, 1)
                io = comp_io.get(cap)   # 子任务 IO 映射（线性关系自测用），无则 None
                if k >= 2:
                    # 标准单元 #5：复制 K 份并联，由 mode="any" 电容收口
                    # （任一副本存活即 ok，一支开路不影响其余；延迟取 max 不增）
                    for r in range(1, k + 1):
                        rid = f"cap_{i}_{r}"
                        comps[rid] = {
                            "type": "resistor",
                            "label": f"{cap}#{r}",
                            "model": goal.tiers.get(cap, self.default_tier),
                            "recovery": goal.recovery,
                        }
                        if io:
                            comps[rid]["required_inputs"] = list(io.get("required_inputs", []))
                            comps[rid]["produced_outputs"] = list(io.get("produced_outputs", []))
                            im = io.get("input_map")
                            if im:
                                comps[rid]["input_map"] = dict(im)
                        wires.append([prev_merge, rid])
                    rmid = f"rmerge_{i}"
                    comps[rmid] = {
                        "type": "capacitor",
                        "label": f"冗余汇合({cap}×{k})",
                        "mode": "any",
                    }
                    for r in range(1, k + 1):
                        wires.append([f"cap_{i}_{r}", rmid])
                    wires.append([rmid, merge_id])
                else:
                    # 单份能力：直连本层汇合（all 语义）
                    cid = cap_id(i)
                    comps[cid] = {
                        "type": "resistor",
                        "label": cap,
                        "model": goal.tiers.get(cap, self.default_tier),
                        "recovery": goal.recovery,
                    }
                    if io:
                        comps[cid]["required_inputs"] = list(io.get("required_inputs", []))
                        comps[cid]["produced_outputs"] = list(io.get("produced_outputs", []))
                        im = io.get("input_map")
                        if im:
                            comps[cid]["input_map"] = dict(im)
                    wires.append([prev_merge, cid])
                    wires.append([cid, merge_id])
            prev_merge = merge_id

        # --- 终端：adc 质量评估 ---
        thr = goal.constraints.get("min_quality", 0.8)
        comps["adc"] = {"type": "adc", "label": "质量评估", "threshold": thr}
        wires.append([prev_merge, "adc"])

        # --- 可选反馈环（标准单元 #3）：末级汇合点(all-fired)门控整链重试 ---
        # 门控点必须取"末级汇合(prev_merge)"而非终端 adc：并行下终端 adc 只读 max 支路质量，
        # 单支路开路仍 ≈0.95 不会触发重试；而电容汇合的 .ok = 所有末级支路均 fired，
        # 才能真实捕获单点 yield 失败。runtime 原生单环：from=末级汇合、to=进入首能力的
        # head，max_iter 次内不达标整链重跑（刷新 rng）。不改 runtime。
        if goal.feedback:
            spec_feedback = {
                "from": prev_merge,
                "to": head,
                "max_iter": goal.feedback["max_iter"],
            }
        else:
            spec_feedback = None

        spec = {
            "name": goal.name,
            "task": goal.description or "自动编译目标",
            "components": comps,
            "wires": wires,
            "feedback": spec_feedback,
            "self_heal": bool(goal.self_heal),
            "adapters": adapters,
            "rationale": self._rationale(goal, layers, caps, adapters),
        }
        return spec

    # ---- 设计理由（可解释性）----
    @staticmethod
    def _rationale(goal, layers, caps=None, adapters=None):
        caps = caps if caps is not None else goal.capabilities
        adapters = adapters or {}
        bits = []
        if goal.dependencies is None:
            bits.append("dependencies 缺省→线性串联(向后兼容)")
        elif not goal.dependencies:
            bits.append("dependencies=[]→全并联(假设能力互不依赖)")
        else:
            bits.append(f"dependencies={goal.dependencies}→DAG 分层")
        bits.append("分层(同层并联/跨层串联): "
                    + " | ".join("[" + ",".join(caps[i] for i in L) + "]"
                                 for L in layers))
        bits.append(f"终端 adc 阈值=min_quality={goal.constraints.get('min_quality', 0.8)}")
        if goal.feedback:
            bits.append(f"反馈环(标准单元#3): 末级汇合(all-fired)门控整链重试 max_iter="
                        f"{goal.feedback['max_iter']}（runtime 原生单环）")
        if goal.redundancy:
            bits.append(f"冗余(标准单元#5): {goal.redundancy} → 复制并联+any汇合"
                        f"（runtime capacitor mode=any，默认 all 不变）")
        else:
            bits.append("注：延迟由 runtime layer 化 propagate 给出(同层max/跨层sum)；"
                        "M2 五标准单元(串联/并联/反馈/桥式/冗余)均已就位")
        if adapters:
            bits.append("格式适配器(②): 插入"
                        + str(len(adapters)) + "个("
                        + ", ".join(f"{v['kind'].upper()}({v['from_fmt']}→{v['to_fmt']})"
                                    for v in adapters.values()) + ")")
        if goal.component_io:
            mapped = sum(1 for io in goal.component_io.values()
                         if isinstance(io, dict) and io.get("input_map"))
            if mapped:
                bits.append(f"线性关系自测(每个电阻跑前核对 required_inputs⊆上游produced_outputs)"
                            f" + 命名漂移符号映射表({mapped} 个节点已注入 input_map 转接头)")
            else:
                bits.append("线性关系自测(每个电阻跑前核对 required_inputs⊆上游produced_outputs)")
        return " | ".join(bits)
