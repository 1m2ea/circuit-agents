"""
circuit-agents · compiler.model_selector
========================================
M? 阶段：智能模型选型 —— 基于任务复杂度、历史执行数据、成本/延迟约束，
为每个电阻节点自动推荐 (tier, model, skills)。

选型策略（多因子加权，可在运行时关闭以回到静态映射）：
  - 复杂度评估：能力数 + 约束密度 → complexity score (0–1)
  - 历史适配：TopologyMemory 查同类任务历史上哪个 tier 成功率高 → 偏向它
  - 成本约束：spec.constraints.max_cost/max_latency_ms → 自动降档
  - 最低质量：spec.constraints.min_quality → 强制不低于某个 tier

与 Binder 的关系：
  - Binder 做 per-capability 的贪心选型（按 min_quality 取最便宜达标档）
  - ModelSelector 在此基础上叠加 per-node 的复杂度/历史/成本微调
  - 默认关闭（auto_select_models=False），零回归

依赖：runtime._TIERS（档位画像）、compiler.llm_agents.CAPABILITY_PROMPTS（技能映射）、
      compiler.topology_memory.TopologyMemory（历史记录）
"""
from __future__ import annotations

import json
import os
import threading

import runtime


class ModelMetrics:
    """Phase 2 ③ 模型选型再平衡的真实历史指标存储。

    记录每个 (capability, tier) 的：调用次数、成功次数/成功率、累计/平均延迟、累计/平均成本。
    这是原 ③ 的短板——`_history_stats` 里 success_rate 是硬编码 1.0、并不真正追踪每层表现。
    本类用真实记录替代假数据，使 ModelSelector 能基于「历史成功率 / 延迟 / 成本」做多目标再平衡。

    设计：
     · JSON 文件持久化（默认 CWD 下 `.model_metrics.json`），跨运行累积、逐步变聪明；
       也可传 path=None 走纯内存模式（离线自检用，无副作用）。
     · 并发安全：用模块级锁保护文件读写（BatchExecutor / 多机器人协同会并发记录）。
     · 离线安全：不依赖网络/LLM，纯本地文件 + dict。
    """

    DEFAULT_PATH = ".model_metrics.json"

    def __init__(self, path: "Optional[str]" = None):
        """
        Parameters
        ----------
        path : str or None
            持久化 JSON 路径；None → 纯内存模式（不落盘，适合自检）。
        """
        self._path = path
        self._lock = threading.Lock()
        self._data = {}   # {capability: {tier: {count, success, total_latency, total_cost}}}

    # ── 记录 ────────────────────────────────────────────────
    def record(self, capability: str, tier: str, success: bool,
               latency_ms: float, cost: float):
        """记录一次 (capability, tier) 的执行结果，更新累计统计。线程安全。"""
        with self._lock:
            cap = self._data.setdefault(capability, {})
            t = cap.setdefault(tier, {"count": 0, "success": 0,
                                      "total_latency": 0.0, "total_cost": 0.0})
            t["count"] += 1
            t["success"] += 1 if success else 0
            t["total_latency"] += float(latency_ms)
            t["total_cost"] += float(cost)
            if self._path:
                self._save()

    # ── 查询 ────────────────────────────────────────────────
    def stats(self, capability: str) -> dict:
        """返回某 capability 下各 tier 的统计（含派生 success_rate/avg_latency/avg_cost）。"""
        with self._lock:
            raw = self._data.get(capability, {})
            return {t: self._derive(v) for t, v in raw.items()}

    def global_stats(self) -> dict:
        """返回全部 capability 的统计（派生后）。"""
        with self._lock:
            return {cap: {t: self._derive(v) for t, v in tiers.items()}
                    for cap, tiers in self._data.items()}

    @staticmethod
    def _derive(v: dict) -> dict:
        c = max(v.get("count", 0), 0)
        succ = v.get("success", 0)
        return {
            "count": c,
            "success": succ,
            "success_rate": (succ / c) if c > 0 else 0.0,
            "avg_latency": (v.get("total_latency", 0.0) / c) if c > 0 else 0.0,
            "avg_cost": (v.get("total_cost", 0.0) / c) if c > 0 else 0.0,
            "total_cost": v.get("total_cost", 0.0),
        }

    # ── 持久化 ──────────────────────────────────────────────
    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不影响主流程（内存态仍可用）

    def load(self):
        """从文件载入已有统计（若文件存在且合法）。"""
        if not self._path:
            return self
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
        except Exception:
            pass
        return self


class ModelSelector:
    """自动为每个电路节点选择最优 (tier, skills) 组合。

    用法
    ----
    >>> ms = ModelSelector(memory=TopologyMemory())
    >>> recs = ms.select(spec)  # {node_id: {"tier": "large", "skills": [...], "model": "..."}}
    >>> # 然后在 compile_goal 或 CircuitExecutor 中用 recs 覆盖节点的 model/skills 字段
    """

    # 档位顺序（用于比较"哪个档更高"）
    _TIER_ORDER = {"small": 0, "large": 1, "tool": 2}

    # 档位→供应商默认路由（跨供应商路由的基础，可被 providers 参数覆盖）
    # small 走本地免费模型，large 走 OpenAI，tool 走 Anthropic（最高端能力）
    _PROVIDERS_DEFAULT = {"small": "local", "large": "openai", "tool": "anthropic"}

    def __init__(self, memory=None, providers=None, metrics=None,
                 weights=None):
        """
        Parameters
        ----------
        memory : TopologyMemory or None
            如果提供且未提供 metrics，查历史记录做"只升不降"的兼容式推荐；None 则只靠复杂度+约束。
        providers : dict or None
            tier→provider 路由映射（如 {"small":"local","large":"openai","tool":"anthropic"}）。
            None 用默认路由。供 CircuitExecutor/backend 解析实际模型端点。
        metrics : ModelMetrics or None
            Phase 2 ③ 真实历史指标存储。提供后，逐节点选型改用「成功率/延迟/成本多目标再平衡」
            （取代原仅"历史升档"逻辑），使选型随真实表现自适应。
        weights : dict or None
            多目标再平衡权重 {"quality":, "latency":, "cost":}，默认 {0.6, 0.2, 0.2}。
            通过 set_weights 可在运行时调（如离线场景拉高 latency/cost 权重）。
        """
        self.memory = memory
        self.metrics = metrics
        # 多目标再平衡权重（quality=历史成功率/固有精度；latency=速度；cost=成本）
        self._weights = dict(weights) if weights else {
            "quality": 0.6, "latency": 0.2, "cost": 0.2}
        # 从 runtime 复用档位画像（与 Binder 同源，保证一致）
        self._tiers = runtime.SimBackend._TIERS
        # 供应商路由（跨供应商路由的基础）
        self.providers = dict(self._PROVIDERS_DEFAULT)
        if providers:
            self.providers.update(providers)

    def set_weights(self, quality: float = None, latency: float = None,
                    cost: float = None):
        """运行时调整多目标再平衡权重（归一化由调用方决定；建议三者之和=1）。"""
        if quality is not None:
            self._weights["quality"] = float(quality)
        if latency is not None:
            self._weights["latency"] = float(latency)
        if cost is not None:
            self._weights["cost"] = float(cost)
        return dict(self._weights)

    # ── 公开入口 ──────────────────────────────────────────────

    def select(self, spec: dict, optimize_for: "Optional[str]" = None) -> dict:
        """返回 {node_id: {tier, model, provider, skills, reason}} 的推荐字典。

        spec 格式 = compile_goal 的产物（含 capabilities/constraints/components/description）。
        components 是 {cid: {type, label, model, capability, ...}, ...} 的字典。

        Parameters
        ----------
        optimize_for : "cost" | "latency" | "quality" | None
            在硬约束（min_quality/max_cost/max_latency）满足的前提下，选最优档：
              - "cost"    选最便宜
              - "latency" 选最低延迟
              - "quality" 选最高精度
              - None      沿用复杂度驱动的 base_tier（原行为，约束只做升/降修正）
        """
        caps = spec.get("capabilities", [])
        constraints = spec.get("constraints", {}) or spec.get("goal", {}).get("constraints", {})
        goal_desc = spec.get("description", "") or spec.get("name", "")
        # components 是 dict，只取 type=="resistor" 的条目
        all_components = spec.get("components", {})
        resistors = [(cid, comp) for cid, comp in all_components.items()
                     if comp.get("type") == "resistor"]

        # 1) 复杂度评分
        complexity = self._complexity_score(caps, constraints)

        # 2) 历史查档（同类任务各能力的历史成功率）
        history = self._history_stats(goal_desc)

        # 3) 逐节点选型
        result = {}
        for cid, comp in resistors:
            cap = comp.get("capability") or comp.get("label", "")
            rec = self._select_for_node(cap, complexity, constraints,
                                        history.get(cap), optimize_for,
                                        self.metrics, self._weights)
            result[cid] = rec

        return result

    def apply_to_spec(self, spec: dict) -> dict:
        """直接修改 spec.components 中每个电阻的 model/skills 字段（就地修改 + 返回）。"""
        recs = self.select(spec)
        for cid, comp in spec.get("components", {}).items():
            rec = recs.get(cid)
            if rec and comp.get("type") == "resistor":
                comp["model"] = rec["tier"]
                if rec.get("provider"):
                    comp["provider"] = rec["provider"]
                if rec.get("skills"):
                    comp["skills"] = rec["skills"]
                comp["_model_reason"] = rec.get("reason", "")
        return spec

    # ── 复杂度评估 ────────────────────────────────────────────

    def _complexity_score(self, caps: list, constraints: dict) -> float:
        """0–1 评分：越高 = 任务越复杂 → 倾向 higher tier。

        因子：
          - 能力数量（0–0.4）
          - 约束数量（0–0.3）
          - min_quality 紧度（0–0.2）
          - max_latency 紧度（0–0.1）
        """
        score = 0.0

        # 能力数：1 个=0, 10+=0.4
        n = len(caps)
        score += min(n / 10.0, 1.0) * 0.4

        # 约束数
        c_keys = ["max_latency_ms", "max_cost", "min_quality", "max_chars"]
        n_c = sum(1 for k in c_keys if constraints.get(k) is not None)
        score += (n_c / len(c_keys)) * 0.3

        # min_quality 紧度
        q = constraints.get("min_quality", 0.0)
        if q > 0.8:
            score += 0.2
        elif q > 0.5:
            score += 0.1

        # max_latency 紧度（越短越难）
        lat = constraints.get("max_latency_ms")
        if lat is not None:
            if lat < 500:
                score += 0.1
            elif lat < 1000:
                score += 0.05

        return min(score, 1.0)

    # ── 历史查档 ──────────────────────────────────────────────

    def _history_stats(self, goal_desc: str) -> dict:
        """从 TopologyMemory 查同类任务的历史模型选型。

        返回 {capability_name: {"success_rate": float, "best_tier": str}}。
        基于 recall() 的最相似条目 + 内存文件全量扫（若可用）。
        """
        if not self.memory:
            return {}

        stats = {}

        # 方式1：recall 返回最相似的一条成功记录
        recalled = self.memory.recall(goal_desc)
        if recalled:
            spec = recalled.get("spec", {})
            components = spec.get("components", {})
            quality = recalled.get("quality", 0.5)
            for cid, node in components.items():
                if node.get("type") != "resistor":
                    continue
                cap = node.get("capability") or node.get("label", "")
                tier = node.get("model", "small")
                if cap not in stats:
                    stats[cap] = {"success_rate": 1.0, "best_tier": tier,
                                  "quality": quality}
                elif quality > stats[cap].get("quality", 0):
                    stats[cap] = {"success_rate": 1.0, "best_tier": tier,
                                  "quality": quality}

        # 方式2：扫文件内所有成功条目（若 _path 可用）
        try:
            mem_path = getattr(self.memory, '_path', None)
            if mem_path:
                import json, os
                if os.path.exists(mem_path):
                    with open(mem_path, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                    entries = raw.get("entries", [])
                    for entry in entries:
                        if not entry.get("result", {}).get("success"):
                            continue
                        spec = entry.get("spec", {})
                        components = spec.get("components", {})
                        for cid, node in components.items():
                            if node.get("type") != "resistor":
                                continue
                            cap = node.get("capability") or node.get("label", "")
                            tier = node.get("model", "small")
                            if cap not in stats:
                                stats[cap] = {"success_rate": 1.0, "best_tier": tier,
                                              "quality": entry.get("result", {}).get("quality", 0.5)}
        except Exception:
            pass

        return stats

    # ── 逐节点选型 ────────────────────────────────────────────

    def _select_for_node(self, cap: str, complexity: float,
                         constraints: dict, history: dict,
                         optimize_for: "Optional[str]" = None,
                         metrics=None, weights=None) -> dict:
        """为一个节点推荐 (tier, model, skills, reason)。"""
        reasons = []

        # 1) 复杂度驱动的基础 tier
        if complexity < 0.25:
            base_tier = "small"
            reasons.append(f"复杂度低({complexity:.2f})→small")
        elif complexity < 0.55:
            base_tier = "large"
            reasons.append(f"复杂度中({complexity:.2f})→large")
        else:
            base_tier = "tool"
            reasons.append(f"复杂度高({complexity:.2f})→tool")

        tier = base_tier

        # 2) min_quality 约束（Binder 已做，这里做二次校验）
        q_min = constraints.get("min_quality", 0.0)
        if q_min > 0:
            # 找到满足 min_quality 的最低 tier
            candidates = sorted(self._tiers.items(),
                                key=lambda x: (x[1]["cost"], -x[1]["yld"]))
            for t, d in candidates:
                if d["accuracy"] >= q_min:
                    if self._TIER_ORDER.get(t, 0) > self._TIER_ORDER.get(tier, 0):
                        tier = t
                        reasons.append(f"min_quality={q_min}需≥{t}")
                    break

        # 3) 成本约束降档
        max_cost = constraints.get("max_cost")
        if max_cost is not None and max_cost > 0:
            node_count = max(1, len(constraints.get("capabilities", [])))
            per_node_budget = max_cost / node_count
            current_cost = self._tiers.get(tier, {}).get("cost", 0.001)
            if current_cost > per_node_budget:
                # 降档到满足预算的最高 tier
                for t in ["small", "large", "tool"]:
                    tc = self._tiers.get(t, {}).get("cost", 0.001)
                    if tc <= per_node_budget:
                        tier = t
                        reasons.append(f"max_cost={max_cost}→{t}")
                        break

        # 4) 延迟约束降档
        max_lat = constraints.get("max_latency_ms")
        if max_lat is not None and max_lat > 0:
            node_count = max(1, len(constraints.get("capabilities", [])))
            per_node_lat = max_lat / node_count
            current_lat = self._tiers.get(tier, {}).get("latency", 200)
            if current_lat > per_node_lat:
                for t in ["small", "large", "tool"]:
                    tl = self._tiers.get(t, {}).get("latency", 200)
                    if tl <= per_node_lat:
                        tier = t
                        reasons.append(f"max_lat={max_lat}ms→{t}")
                        break

        # 5) 历史驱动选型（Phase 2 ③ 多目标再平衡；无 metrics 时回退 legacy 只升不降）
        ncap = max(1, len(constraints.get("capabilities", [])))
        if metrics is not None:
            feasible = self._feasible_tiers(constraints, q_min, max_cost, max_lat, ncap)
            if feasible:
                scored = [(self._rebalance_score(t, cap, metrics, weights), t)
                          for t in feasible]
                scored.sort(reverse=True)
                best_score, best_tier = scored[0]
                if best_tier != tier:
                    tier = best_tier
                    reasons.append(f"再平衡(权重q{weights['quality']:.2f}/"
                                   f"l{weights['latency']:.2f}/c{weights['cost']:.2f})"
                                   f"→{tier}(分{best_score:.2f})")
                else:
                    reasons.append(f"再平衡维持{tier}(分{best_score:.2f})")
        elif history:
            # legacy：历史最佳 tier 升档（只升不降），保 ③ 既有行为零回归
            hist_tier = history.get("best_tier", "small")
            success_rate = history.get("success_rate", 1.0)
            if self._TIER_ORDER.get(hist_tier, 0) > self._TIER_ORDER.get(tier, 0):
                if success_rate >= 0.7:
                    tier = hist_tier
                    reasons.append(f"历史最佳={hist_tier}(成功率{success_rate:.0%})")

        # 5.5) 多目标再平衡（仅当显式 optimize_for）
        if optimize_for in ("cost", "latency", "quality"):
            ncap = max(1, len(constraints.get("capabilities", [])))
            feasible = []
            for t in ["small", "large", "tool"]:
                d = self._tiers.get(t, {})
                ok = True
                if q_min > 0 and d.get("accuracy", 0) < q_min:
                    ok = False
                if max_cost and max_cost > 0 and d.get("cost", 0) > max_cost / ncap:
                    ok = False
                if max_lat and max_lat > 0 and d.get("latency", 1e9) > max_lat / ncap:
                    ok = False
                if ok:
                    feasible.append(t)
            if feasible:
                if optimize_for == "cost":
                    tier = min(feasible, key=lambda t: self._tiers[t].get("cost", 1e9))
                    reasons.append(f"optimize=cost→{tier}")
                elif optimize_for == "latency":
                    tier = min(feasible, key=lambda t: self._tiers[t].get("latency", 1e9))
                    reasons.append(f"optimize=latency→{tier}")
                elif optimize_for == "quality":
                    tier = max(feasible, key=lambda t: self._tiers[t].get("accuracy", 0))
                    reasons.append(f"optimize=quality→{tier}")

        # 6) 技能推荐
        skills = self._skills_for(cap, tier)

        # 7) 跨供应商路由建议
        provider = self.providers.get(tier, self._PROVIDERS_DEFAULT.get(tier, "local"))

        return {
            "tier": tier,
            "model": tier,  # 实际 model 名由 backend._resolve_model(tier) 解析
            "provider": provider,  # 跨供应商路由建议（small→local/large→openai/tool→anthropic）
            "skills": skills,
            "reason": " | ".join(reasons) if reasons else "默认静态映射",
        }

    # ── 多目标再平衡（Phase 2 ③）──────────────────────────────

    def _feasible_tiers(self, constraints: dict, q_min, max_cost, max_lat, ncap) -> list:
        """返回通过硬约束（min_quality / max_cost / max_latency）的 tier 列表。"""
        feasible = []
        for t in ["small", "large", "tool"]:
            d = self._tiers.get(t, {})
            if q_min and q_min > 0 and d.get("accuracy", 0) < q_min:
                continue
            if max_cost and max_cost > 0 and d.get("cost", 0) > max_cost / ncap:
                continue
            if max_lat and max_lat > 0 and d.get("latency", 1e9) > max_lat / ncap:
                continue
            feasible.append(t)
        return feasible

    def _rebalance_score(self, tier: str, cap: str, metrics, weights: dict) -> float:
        """多目标再平衡评分（0–1）：quality(历史成功率/固有精度) + latency(速度) + cost(成本)。

        - quality：优先用 ModelMetrics 真实成功率；无历史则退化用档位固有 accuracy。
        - latency/cost：越快/越便宜越好，用倒数后在档位画像上归一化（small 为 1.0 基准）。
        - 权重 w 由 set_weights 决定（默认 quality0.6/latency0.2/cost0.2）。
        返回加权合成分，分高者优先。
        """
        w = weights or self._weights
        d = self._tiers.get(tier, {})
        # quality 维度
        m = metrics.stats(cap).get(tier) if metrics else None
        if m and m.get("count", 0) > 0:
            quality = m["success_rate"]
        else:
            quality = d.get("accuracy", 0.70)
        # latency / cost 维度（倒数归一化，small 基准=1.0）
        lat = max(d.get("latency", 200), 1.0)
        cost = max(d.get("cost", 0.001), 1e-9)
        lat_n = min((1.0 / lat) / (1.0 / 200.0), 1.0)     # small(200)→1.0, large(1500)→0.133
        cost_n = min((1.0 / cost) / (1.0 / 0.001), 1.0)   # small(0.001)→1.0, large(0.020)→0.05
        return (w.get("quality", 0.6) * quality
                + w.get("latency", 0.2) * lat_n
                + w.get("cost", 0.2) * cost_n)

    def record_outcomes(self, components: dict, out: dict,
                        total_latency_ms: float, total_cost: float):
        """Phase 2 ③ 反馈闭环：执行后把每个电阻节点的真实表现回填到 ModelMetrics。
        无 metrics / 无电阻节点时安全跳过；异常吞掉，零回归。"""
        if not self.metrics:
            return
        try:
            resistors = [(cid, c) for cid, c in components.items()
                        if c.get("type") == "resistor"]
            n = max(1, len(resistors))
            lat = total_latency_ms / n
            cost = total_cost / n
            for cid, comp in resistors:
                sig = out.get(cid)
                if sig is None:
                    continue
                cap = comp.get("capability") or comp.get("label", "")
                tier = comp.get("model", "small")
                self.metrics.record(cap, tier, bool(sig.ok), lat, cost)
        except Exception:
            pass

    # ── 技能匹配 ──────────────────────────────────────────────

    @staticmethod
    def _skills_for(cap: str, tier: str) -> list:
        """从 CAPABILITY_PROMPTS 获取该能力在当前档位应绑定的技能列表。

        当前直接返回提示词模板中声明的技能（与 LLMAgentBackend._tools_for 同源）。
        未来可在此处叠加复杂度/历史过滤。
        """
        try:
            from .llm_agents import CAPABILITY_PROMPTS
        except ImportError:
            return []
        tmpl = CAPABILITY_PROMPTS.get(cap, {})
        return list(tmpl.get("skills", []))


# ── 离线自检 ──────────────────────────────────────────────────

def selftest():
    """离线验证：选型逻辑合理、零回归路径安全。"""
    import random as _rng
    rng = _rng.Random(42)

    # ---- 1) 低复杂度 → small ----
    spec = {
        "capabilities": ["summarize"],
        "constraints": {},
        "description": "简单摘要",
        "components": {
            "summarize": {"type": "resistor", "label": "summarize",
                          "capability": "summarize", "model": "small"},
        },
    }
    ms = ModelSelector(memory=None)
    recs = ms.select(spec)
    assert recs["summarize"]["tier"] == "small", \
        f"低复杂度应选 small，实际: {recs['summarize']['tier']}"
    print("✓ 低复杂度 → small")

    # ---- 2) 高复杂度 → tool ----
    spec["capabilities"] = ["retrieve", "extract", "reason", "calculate",
                            "verify", "classify", "compare", "organize", "summarize"]
    spec["description"] = "综合研究报告+对比分析+预测+分解"
    spec["components"] = {
        f"n{i}": {"type": "resistor", "label": c, "capability": c, "model": "small"}
        for i, c in enumerate(spec["capabilities"])
    }
    recs = ms.select(spec)
    first_node = spec["capabilities"][0]
    first_nid = "n0"
    assert recs[first_nid]["tier"] in ("large", "tool"), \
        f"高复杂度应选 large/tool，实际: {recs[first_nid]['tier']}"
    print("✓ 高复杂度 → large/tool")

    # ---- 3) min_quality 约束 → 强制高 tier ----
    spec2 = {
        "capabilities": ["reason"],
        "constraints": {"min_quality": 0.95},
        "description": "高精度推理",
        "components": {
            "reason": {"type": "resistor", "label": "reason",
                       "capability": "reason", "model": "small"},
        },
    }
    recs = ms.select(spec2)
    # tool accuracy=0.99 ≥ 0.95, large=0.92 < 0.95 → 应升到 tool
    assert recs["reason"]["tier"] == "tool", \
        f"min_quality=0.95 应升到 tool，实际: {recs['reason']['tier']}"
    print("✓ min_quality=0.95 → tool")

    # ---- 4) max_cost 约束 → 降档 ----
    spec3 = {
        "capabilities": ["reason", "calculate"],
        "constraints": {"max_cost": 0.002},  # very tight budget
        "description": "极低预算推理",
        "components": {
            "reason": {"type": "resistor", "label": "reason",
                       "capability": "reason", "model": "large"},
            "calculate": {"type": "resistor", "label": "calculate",
                          "capability": "calculate", "model": "large"},
        },
    }
    recs = ms.select(spec3)
    # 每个节点预算 = 0.001，small cost=0.001 刚好
    assert recs["reason"]["tier"] == "small", \
        f"max_cost=0.002/2节点 → 应降 small，实际: {recs['reason']['tier']}"
    print("✓ max_cost 约束 → 降档到 small")

    # ---- 5) max_latency_ms 约束 → 降档 ----
    spec4 = {
        "capabilities": ["reason", "calculate"],
        "constraints": {"max_latency_ms": 500},
        "description": "超低延迟推理",
        "components": {
            "reason": {"type": "resistor", "label": "reason",
                       "capability": "reason", "model": "large"},
            "calculate": {"type": "resistor", "label": "calculate",
                          "capability": "calculate", "model": "large"},
        },
    }
    recs = ms.select(spec4)
    # 每个节点预算 250ms，small latency=200 刚好
    assert recs["reason"]["tier"] == "small", \
        f"max_lat=500ms/2节点 → 应降 small，实际: {recs['reason']['tier']}"
    print("✓ max_latency 约束 → 降档到 small")

    # ---- 6) skills 推荐不空 ----
    spec5 = {
        "capabilities": ["retrieve"],
        "constraints": {},
        "description": "搜索任务",
        "components": {
            "retrieve": {"type": "resistor", "label": "retrieve",
                         "capability": "retrieve", "model": "small"},
        },
    }
    recs = ms.select(spec5)
    assert "skills" in recs["retrieve"], "应含 skills 字段"
    assert isinstance(recs["retrieve"]["skills"], list), "skills 应为列表"
    print(f"✓ skills 推荐: {recs['retrieve']['skills']}")

    # ---- 7) apply_to_spec 就地修改 ----
    spec6 = {
        "capabilities": ["reason"],
        "constraints": {"min_quality": 0.95},
        "description": "",
        "components": {
            "reason": {"type": "resistor", "label": "reason",
                       "capability": "reason", "model": "small"},
            "cap_0": {"type": "capacitor", "label": "merge"},
        },
    }
    ms.apply_to_spec(spec6)
    assert spec6["components"]["reason"]["model"] == "tool", \
        f"apply_to_spec 应修改电阻 model，实际: {spec6['components']['reason']['model']}"
    # 非电阻节点（capacitor）不应被改
    assert "model" not in spec6["components"]["cap_0"], \
        "非电阻节点不应被改 model"
    assert "_model_reason" in spec6["components"]["reason"], "应有 _model_reason"
    print("✓ apply_to_spec 仅修改电阻节点 + 留 reason 痕迹")

    # ---- 8) 零回归：无 memory 不崩溃 ----
    ms_none = ModelSelector(memory=None)
    spec7 = {
        "capabilities": ["summarize"],
        "constraints": {},
        "description": "test",
        "components": {
            "s": {"type": "resistor", "label": "summarize",
                  "capability": "summarize", "model": "small"},
        },
    }
    recs = ms_none.select(spec7)
    assert recs["s"]["tier"] == "small", "无 memory 应正常选型"
    print("✓ 无 memory 不崩溃（零回归）")

    # ---- 9) 空节点列表不崩 ----
    spec8 = {
        "capabilities": ["reason"],
        "constraints": {},
        "description": "test",
        "components": {
            "cap_0": {"type": "capacitor"},
        },
    }
    recs = ms_none.select(spec8)
    assert recs == {}, "无电阻节点应返回空字典"
    print("✓ 无电阻节点 → 空字典")

    # ---- 10) 与 TopologyMemory 集成 ----
    from compiler.topology_memory import TopologyMemory
    import tempfile, os
    tmp = os.path.join(tempfile.mkdtemp(), "test_mem.json")
    mem = TopologyMemory(path=tmp)
    # 记录一条成功历史：reason 用 large 成功
    mem.record(
        "分析GDP数据并预测趋势",
        {
            "capabilities": ["retrieve", "reason", "predict"],
            "components": {
                "reason": {"type": "resistor", "label": "reason", "model": "large"},
            },
        },
        {
            "success": True,
            "final_quality": 0.95,
            "components": {"reason": {"ok": True}},
        }
    )
    ms_hist = ModelSelector(memory=mem)
    spec10 = {
        "capabilities": ["retrieve", "reason"],
        "constraints": {},
        "description": "分析GDP数据并预测趋势",
        "components": {
            "reason": {"type": "resistor", "label": "reason",
                       "capability": "reason", "model": "small"},
        },
    }
    recs = ms_hist.select(spec10)
    # 历史显示 large 成功 → 应升到 large
    assert recs["reason"]["tier"] == "large", \
        f"历史最佳=large → 应升档，实际: {recs['reason']['tier']}"
    print("✓ TopologyMemory 历史驱动升档")

    # ---- 11) 跨供应商路由（provider 字段）----
    spec11 = {
        "capabilities": ["summarize"],
        "constraints": {},
        "description": "简单",
        "components": {
            "s": {"type": "resistor", "label": "summarize",
                  "capability": "summarize", "model": "small"},
        },
    }
    recs11 = ms.select(spec11)
    assert recs11["s"]["provider"] == "local", \
        f"small 应路由 local，实际 {recs11['s']['provider']}"
    # 高复杂度（复用 spec，8 能力）→ tool/large → openai/anthropic
    recs11b = ms.select(spec)
    assert recs11b["n0"]["provider"] in ("openai", "anthropic"), \
        f"高复杂度应路由 openai/anthropic，实际 {recs11b['n0']['provider']}"
    # 自定义 providers 覆盖
    ms_custom = ModelSelector(memory=None,
                              providers={"small": "my-local", "large": "my-openai",
                                         "tool": "my-anthropic"})
    recs11c = ms_custom.select(spec11)
    assert recs11c["s"]["provider"] == "my-local", \
        f"自定义 provider 未生效，实际 {recs11c['s']['provider']}"
    print(f"✓ 跨供应商路由: small→{recs11['s']['provider']}, "
          f"高复杂度→{recs11b['n0']['provider']}, 自定义覆盖✓")

    # ---- 12) optimize_for 多目标再平衡 ----
    spec12 = {
        "capabilities": ["reason", "calculate"],
        "constraints": {"max_cost": 1.0},  # 宽松，每节点预算 0.5
        "description": "再平衡测试",
        "components": {
            "reason": {"type": "resistor", "label": "reason",
                       "capability": "reason", "model": "small"},
            "calculate": {"type": "resistor", "label": "calculate",
                          "capability": "calculate", "model": "small"},
        },
    }
    recs_cost = ms.select(spec12, optimize_for="cost")
    recs_qual = ms.select(spec12, optimize_for="quality")
    # cost 最优=small(0.001)，quality 最优=tool(0.99)，均 ≤0.5 预算
    assert recs_cost["reason"]["tier"] == "small", \
        f"optimize=cost 应 small，实际 {recs_cost['reason']['tier']}"
    assert recs_qual["reason"]["tier"] == "tool", \
        f"optimize=quality 应 tool，实际 {recs_qual['reason']['tier']}"
    print(f"✓ optimize_for: cost→{recs_cost['reason']['tier']}, "
          f"quality→{recs_qual['reason']['tier']}")

    # 清理临时文件
    try:
        os.unlink(tmp)
        os.rmdir(os.path.dirname(tmp))
    except Exception:
        pass

    # Phase 2 ③ 多目标再平衡（真实历史指标）
    model_rebalance_selftest()

    print("\nmodel_selector 离线自检全部通过 ✓")


def model_rebalance_selftest():
    """Phase 2 ③ 多目标再平衡离线自检：真实历史成功率/延迟/成本驱动选型。

    核心验证：原 ③ 的 _history_stats 把 success_rate 硬编码为 1.0（假数据）；
    本测试用 ModelMetrics 写入真实分层成功率，确认选型随真实表现自适应。
    """
    # ---- 1) 真实历史：small 在该能力上经常失败，large 稳定成功 ----
    mm = ModelMetrics(path=None)  # 内存模式，无副作用
    cap = "reason"
    for _ in range(10):
        mm.record(cap, "small", success=False, latency_ms=200, cost=0.001)
    for _ in range(10):
        mm.record(cap, "large", success=True, latency_ms=1500, cost=0.020)

    ms = ModelSelector(memory=None, metrics=mm)
    spec = {
        "capabilities": [cap],
        "constraints": {},
        "description": "需要稳定推理",
        "components": {
            cap: {"type": "resistor", "label": cap, "capability": cap, "model": "small"},
        },
    }
    recs = ms.select(spec)
    # small 成功率 0% → 再平衡必须避开 small，改选更优档（large 或 tool 均可，复合分更高）
    assert recs[cap]["tier"] != "small", \
        f"真实历史下 small 失败率高(0%)应避开，实际仍选: {recs[cap]['tier']}"
    assert "再平衡" in recs[cap]["reason"], "reason 应标注再平衡"
    print(f"✓ 多目标再平衡：真实历史 small 失败率高(0%) → 避坑改选 {recs[cap]['tier']}（{recs[cap]['reason']}）")

    # ---- 2) 无历史 → 退化为复杂度/约束驱动（零回归）----
    ms0 = ModelSelector(memory=None, metrics=None)
    recs0 = ms0.select(spec)
    # 单能力、无约束 → 复杂度低 → small
    assert recs0[cap]["tier"] == "small", \
        f"无历史应退化为复杂度驱动(低→small)，实际: {recs0[cap]['tier']}"
    print("✓ 无 ModelMetrics → 退化为复杂度/约束驱动（零回归）")

    # ---- 3) set_weights 改变偏好：拉高 cost 权重 → 倾向便宜档 ----
    ms.set_weights(quality=0.2, latency=0.2, cost=0.6)
    recs_cheap = ms.select(spec)
    # 即便 large 成功率高，cost 权重拉满后 small(0.001) 便宜分更高 → 回退 small
    assert recs_cheap[cap]["tier"] == "small", \
        f"cost 权重拉满应回退便宜档 small，实际: {recs_cheap[cap]['tier']}"
    print(f"✓ set_weights(cost=0.6) → 偏好便宜档: {recs_cheap[cap]['tier']}")

    # 复位权重（不影响后续，单测隔离）
    ms.set_weights(quality=0.6, latency=0.2, cost=0.2)

    # ---- 4) 约束仍优先：min_quality 高于所有档 → 强制最高档 ----
    spec_hq = {
        "capabilities": [cap],
        "constraints": {"min_quality": 0.99},
        "description": "极高精度",
        "components": {
            cap: {"type": "resistor", "label": cap, "capability": cap, "model": "small"},
        },
    }
    recs_hq = ms.select(spec_hq)
    assert recs_hq[cap]["tier"] == "tool", \
        f"min_quality=0.99 应强制 tool，实际: {recs_hq[cap]['tier']}"
    print("✓ 硬约束 min_quality 仍优先于再平衡（约束>历史）")

    # ---- 5) 全局统计可查 ----
    g = mm.global_stats()
    assert cap in g and "small" in g[cap] and "large" in g[cap], "global_stats 应含两档"
    assert abs(g[cap]["small"]["success_rate"] - 0.0) < 1e-9, "small 成功率应记 0%"
    assert abs(g[cap]["large"]["success_rate"] - 1.0) < 1e-9, "large 成功率应记 100%"
    print("✓ ModelMetrics.global_stats 真实成功率记录正确（small=0%, large=100%）")

    print("\nPhase 2 ③ 模型选型多目标再平衡 离线自检全部通过 ✓")


if __name__ == "__main__":
    selftest()
