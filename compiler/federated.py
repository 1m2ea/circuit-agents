"""Phase 2 · 第三层范式进化 ② —— 联邦学习（多实例共享拓扑经验，不共享原始数据）

问题：⑬ 共享生态（share.py）分发的是**完整电路图**——里面带着 goal 原文、具体
label、任务上下文。企业内多团队各跑一个 circuit-agents 实例时，谁也不愿意把
「我们在分析哪家公司的财报」这种原始任务描述交出去。但大家都想共享「analyze 这类
能力用 tool 档成功率更高」这种统计经验。

思路（脱敏摘要 + 差分隐私 + FedAvg）：
  1. 本地提取 —— 只抽**结构与统计**：motif 支持度（组件类型对频次）、
     (capability, tier) 的成功率/延迟/成本、质量分布直方图。
     原始 goal 文本、spec 全文、失败节点 id 一律不出本地。
  2. 差分隐私 —— 对计数类查询加拉普拉斯噪声，噪声尺度 b = Δ/ε_part；
     隐私预算 ε 按**顺序组合定理**在各查询间平分，并由 PrivacyLedger 记账。
     求和类查询先 clip 到上界再加噪（否则敏感度无界）。
  3. 聚合 —— FedAvg：按各实例样本数加权平均，样本多的实例话语权大。
  4. 回灌 —— 全局统计合并进本地 ModelMetrics 作为先验，本地实例立刻受益于
     其他实例的经验，而从未见过它们的原始数据。

与 ⑬ 共享生态的分工：
  · share.py（⑬）：分享**具体电路图**，适合公开/可信场景，内容完整但不脱敏。
  · federated.py（本模块）：分享**统计经验**，适合跨组织，内容脱敏且带隐私保证。

离线安全：纯本地计算 + dict，无 key、无网络。transport 可插拔（默认进程内）。
"""

from __future__ import annotations

import json
import math
import random
from typing import Optional


# ──────────────────────────────────────────────────────────
# 差分隐私原语
# ──────────────────────────────────────────────────────────

def laplace_noise(scale: float, rng: random.Random) -> float:
    """采样 Laplace(0, scale)：用逆变换法，避免依赖 numpy。"""
    if scale <= 0:
        return 0.0
    u = rng.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log(1 - 2 * abs(u))


class PrivacyBudgetExhausted(Exception):
    """隐私预算耗尽——继续发布会让攻击者靠重复查询求平均消掉噪声、反推真值。"""


class PrivacyLedger:
    """隐私预算记账：顺序组合定理下 ε 可加，累计不得超过总预算。

    这里必须**硬性执行**而非仅记录：差分隐私的经典攻击就是对同一份数据反复查询，
    多次结果求平均即可把零均值噪声消掉、逼近真值。所以预算耗尽后必须拒绝发布。
    """

    def __init__(self, epsilon: float = 1.0, max_releases: int = 1):
        self.total = float(epsilon)
        self.spent = 0.0
        self.entries = []
        self.max_releases = max(1, int(max_releases))
        self.releases = 0

    def remaining(self) -> float:
        return max(0.0, self.total - self.spent)

    def release_budget(self) -> float:
        """本次发布可用的预算（总预算按声明的发布次数均摊）。"""
        return self.total / self.max_releases

    def can_release(self) -> bool:
        return (self.releases < self.max_releases
                and self.remaining() >= self.release_budget() - 1e-9)

    def begin_release(self):
        if not self.can_release():
            raise PrivacyBudgetExhausted(
                f"隐私预算耗尽：已发布 {self.releases}/{self.max_releases} 次，"
                f"剩余 ε={self.remaining():.4f}。继续发布可被重复查询攻击反推真值。")
        self.releases += 1
        return self.release_budget()

    def spend(self, amount: float, query: str) -> bool:
        """消耗预算；超预算返回 False（调用方应拒绝该查询）。"""
        if self.spent + amount > self.total + 1e-9:
            return False
        self.spent += amount
        self.entries.append({"query": query, "epsilon": round(amount, 6)})
        return True

    def report(self) -> dict:
        return {"epsilon_total": self.total,
                "epsilon_spent": round(self.spent, 6),
                "epsilon_remaining": round(self.remaining(), 6),
                "releases": self.releases,
                "max_releases": self.max_releases,
                "queries": list(self.entries)}


# ──────────────────────────────────────────────────────────
# 联邦客户端（单个 circuit-agents 实例）
# ──────────────────────────────────────────────────────────

class FederatedClient:
    """一个本地实例：提取脱敏摘要 → 上报 → 接收全局模型 → 回灌。

    用法::

        c = FederatedClient("team-a", memory=mem, epsilon=1.0)
        s = c.extract_summary()          # 脱敏 + 加噪后的摘要
        # ... 多个 client 的 summary 交给 FederatedServer.aggregate ...
        c.apply_global(global_model)     # 回灌全局经验
    """

    # 求和类查询的裁剪上界（限制单条记录对结果的最大影响 → 界定敏感度）
    CLIP_LATENCY_MS = 5000.0
    CLIP_COST = 0.5
    # 摘要中禁止出现的原始数据字段（隐私红线，extract 时主动剔除）
    FORBIDDEN_KEYS = ("goal_desc", "spec", "failed_nodes", "timestamp", "components")

    def __init__(self, client_id: str, memory=None, metrics=None,
                 epsilon: float = 1.0, seed: int = 0, min_support: int = 1,
                 max_releases: int = 1):
        self.client_id = client_id
        self.memory = memory
        self.metrics = metrics
        self.rng = random.Random(seed)
        self.ledger = PrivacyLedger(epsilon, max_releases=max_releases)
        self.min_support = min_support
        self.received_global = None

    # ---- 本地统计提取（脱敏）----
    def _raw_summary(self) -> dict:
        """从本地记忆/指标抽取结构与统计，**不含任何原始文本**。"""
        motifs, caps, qual_hist, n = {}, {}, [0] * 5, 0

        entries = []
        if self.memory is not None:
            try:
                entries = self.memory._load().get("entries", [])
            except Exception:
                entries = []

        for e in entries:
            n += 1
            spec = e.get("spec") or {}
            comps = spec.get("components") or {}
            # motif = 组件「类型对」频次，不含 label / goal 文本
            for a, b in (spec.get("wires") or []):
                ta = (comps.get(a) or {}).get("type", "?")
                tb = (comps.get(b) or {}).get("type", "?")
                key = f"{ta}->{tb}"
                motifs[key] = motifs.get(key, 0) + 1
            # 能力档位统计：label 是通用能力名（research/analyze），非用户数据
            res = e.get("result") or {}
            ok = bool(res.get("success"))
            for c in comps.values():
                if c.get("type") != "resistor":
                    continue
                cap = (c.get("label") or "unknown").split("#")[0]
                tier = c.get("model") or "small"
                slot = caps.setdefault(f"{cap}|{tier}", {
                    "count": 0, "success": 0, "latency": 0.0, "cost": 0.0})
                slot["count"] += 1
                slot["success"] += 1 if ok else 0
                slot["latency"] += min(float(res.get("total_latency_ms") or 0),
                                       self.CLIP_LATENCY_MS)
                slot["cost"] += min(float(res.get("total_cost") or 0), self.CLIP_COST)
            q = float(res.get("final_quality") or 0.0)
            qual_hist[min(int(q * 5), 4)] += 1          # 5 桶直方图

        # 也吸收 ModelMetrics 的真实执行历史（若挂了）
        if self.metrics is not None:
            try:
                data = getattr(self.metrics, "_data", {}) or {}
                for cap, tiers in data.items():
                    for tier, st in tiers.items():
                        slot = caps.setdefault(f"{cap}|{tier}", {
                            "count": 0, "success": 0, "latency": 0.0, "cost": 0.0})
                        slot["count"] += int(st.get("count", 0))
                        slot["success"] += int(st.get("success", 0))
                        slot["latency"] += min(float(st.get("total_latency", 0.0)),
                                               self.CLIP_LATENCY_MS * max(st.get("count", 1), 1))
                        slot["cost"] += min(float(st.get("total_cost", 0.0)),
                                            self.CLIP_COST * max(st.get("count", 1), 1))
            except Exception:
                pass

        return {"client_id": self.client_id, "n_samples": n,
                "motifs": motifs, "caps": caps, "quality_hist": qual_hist}

    # ---- 加噪 ----
    def _privatize(self, raw: dict) -> dict:
        """对计数/求和类字段加拉普拉斯噪声，预算按查询数平分（顺序组合）。

        本次发布的可用预算由 ledger 分配；预算耗尽会抛 PrivacyBudgetExhausted。
        """
        release_eps = self.ledger.begin_release()
        n_queries = max(1, len(raw["motifs"]) + len(raw["caps"]) * 4
                        + len(raw["quality_hist"]) + 1)
        eps_each = release_eps / n_queries
        b_count = 1.0 / max(eps_each, 1e-9)                       # 计数敏感度 Δ=1
        b_lat = self.CLIP_LATENCY_MS / max(eps_each, 1e-9)        # 求和敏感度=clip 上界
        b_cost = self.CLIP_COST / max(eps_each, 1e-9)

        def noisy(v, b, nonneg=True):
            out = v + laplace_noise(b, self.rng)
            return max(0.0, out) if nonneg else out

        motifs = {}
        for k, v in raw["motifs"].items():
            self.ledger.spend(eps_each, f"motif:{k}")
            nv = noisy(v, b_count)
            if nv >= self.min_support:            # 低频 motif 直接丢（k-匿名思路）
                motifs[k] = round(nv, 3)

        caps = {}
        for k, st in raw["caps"].items():
            for q in ("count", "success", "latency", "cost"):
                self.ledger.spend(eps_each, f"cap:{k}:{q}")
            cnt = noisy(st["count"], b_count)
            suc = min(noisy(st["success"], b_count), cnt)   # 成功数不能超过总数
            caps[k] = {
                "count": round(cnt, 3),
                "success": round(suc, 3),
                "latency": round(noisy(st["latency"], b_lat), 2),
                "cost": round(noisy(st["cost"], b_cost), 5),
            }

        hist = []
        for i, v in enumerate(raw["quality_hist"]):
            self.ledger.spend(eps_each, f"qhist:{i}")
            hist.append(round(noisy(v, b_count), 3))

        self.ledger.spend(eps_each, "n_samples")
        return {
            "client_id": raw["client_id"],
            "n_samples": max(0.0, round(raw["n_samples"] + laplace_noise(b_count, self.rng), 3)),
            "motifs": motifs, "caps": caps, "quality_hist": hist,
            "dp": {"epsilon": self.ledger.total, "mechanism": "laplace",
                   "release_epsilon": round(release_eps, 6),
                   "per_query_epsilon": round(eps_each, 6),
                   "composition": "sequential"},
        }

    def extract_summary(self, privatize: bool = True, strict: bool = False) -> dict:
        """产出可上报的摘要。privatize=False 仅用于对照测试，生产必须开。

        预算耗尽时：strict=True 抛 PrivacyBudgetExhausted，否则返回带 error 的空摘要
        （聚合端会自动跳过，不会把它当成 0 样本拉低全局统计）。
        """
        raw = self._raw_summary()
        if not privatize:
            summary = raw
        else:
            try:
                summary = self._privatize(raw)
            except PrivacyBudgetExhausted as e:
                if strict:
                    raise
                return {"client_id": self.client_id,
                        "error": "privacy_budget_exhausted", "detail": str(e)}
        for k in self.FORBIDDEN_KEYS:             # 隐私红线：二次确认无原始字段
            summary.pop(k, None)
        return summary

    # ---- 回灌 ----
    def apply_global(self, global_model: dict, blend: float = 0.5) -> dict:
        """把全局经验合并进本地 ModelMetrics 作为先验。

        blend 控制全局知识权重（0=完全信本地，1=完全信全局）。
        本地已有大量样本时全局权重会被样本数自动稀释，不会被小样本实例带偏。
        """
        self.received_global = global_model
        applied = {"caps_merged": 0, "motifs_learned": 0, "blend": blend}
        gcaps = (global_model or {}).get("caps") or {}
        if self.metrics is not None:
            for key, st in gcaps.items():
                if "|" not in key:
                    continue
                cap, tier = key.rsplit("|", 1)
                cnt = float(st.get("count") or 0)
                if cnt <= 0:
                    continue
                try:
                    local = self.metrics._data.setdefault(cap, {}).setdefault(
                        tier, {"count": 0, "success": 0,
                               "total_latency": 0.0, "total_cost": 0.0})
                    w = blend * cnt
                    local["count"] += w
                    local["success"] += w * float(st.get("success_rate") or 0)
                    local["total_latency"] += w * float(st.get("avg_latency") or 0)
                    local["total_cost"] += w * float(st.get("avg_cost") or 0)
                    applied["caps_merged"] += 1
                except Exception:
                    continue
        applied["motifs_learned"] = len((global_model or {}).get("motifs") or {})
        return applied

    def privacy_report(self) -> dict:
        return self.ledger.report()


# ──────────────────────────────────────────────────────────
# 联邦服务端（聚合方，只见摘要不见原始数据）
# ──────────────────────────────────────────────────────────

class FederatedServer:
    """FedAvg 聚合器：按样本数加权平均各实例摘要。

    服务端**永远看不到**原始 goal / spec —— 输入只有脱敏加噪摘要。
    """

    def __init__(self, min_clients: int = 2, transport=None):
        self.min_clients = min_clients
        self.transport = transport      # 可插拔：默认进程内直传，可换 HTTP/gRPC
        self.rounds = []

    def aggregate(self, summaries: list) -> dict:
        """FedAvg：Σ(n_i · x_i) / Σ n_i。返回全局模型。

        预算耗尽（带 error）的摘要会被跳过——它没有有效统计，计入只会污染全局。
        """
        skipped = [s.get("client_id") for s in (summaries or [])
                   if s and s.get("error")]
        summaries = [s for s in (summaries or []) if s and not s.get("error")]
        if len(summaries) < self.min_clients:
            return {"error": f"参与方不足（需 ≥{self.min_clients}，实到 {len(summaries)}）",
                    "n_clients": len(summaries), "skipped_exhausted": skipped}

        total_n = sum(max(float(s.get("n_samples") or 0), 0.0) for s in summaries) or 1.0

        # motif：加权计数求和（各方结构经验汇总）
        motifs = {}
        for s in summaries:
            for k, v in (s.get("motifs") or {}).items():
                motifs[k] = motifs.get(k, 0.0) + float(v)

        # caps：成功率/延迟/成本 按 count 加权平均（FedAvg 核心）
        caps = {}
        for s in summaries:
            for k, st in (s.get("caps") or {}).items():
                cnt = float(st.get("count") or 0)
                if cnt <= 0:
                    continue
                acc = caps.setdefault(k, {"count": 0.0, "w_success": 0.0,
                                          "w_latency": 0.0, "w_cost": 0.0,
                                          "contributors": 0})
                acc["count"] += cnt
                acc["w_success"] += float(st.get("success") or 0)
                acc["w_latency"] += float(st.get("latency") or 0)
                acc["w_cost"] += float(st.get("cost") or 0)
                acc["contributors"] += 1
        for k, acc in caps.items():
            c = max(acc["count"], 1e-9)
            acc["success_rate"] = round(min(acc["w_success"] / c, 1.0), 4)
            acc["avg_latency"] = round(acc["w_latency"] / c, 2)
            acc["avg_cost"] = round(acc["w_cost"] / c, 6)
            acc["count"] = round(acc["count"], 3)
            for tmp in ("w_success", "w_latency", "w_cost"):
                acc.pop(tmp)

        # 质量直方图：按样本数加权
        hist = [0.0] * 5
        for s in summaries:
            w = max(float(s.get("n_samples") or 0), 0.0) / total_n
            for i, v in enumerate((s.get("quality_hist") or [0] * 5)[:5]):
                hist[i] += w * float(v)

        model = {
            "round": len(self.rounds) + 1,
            "n_clients": len(summaries),
            "clients": [s.get("client_id") for s in summaries],
            "total_samples": round(total_n, 3),
            "motifs": {k: round(v, 3) for k, v in
                       sorted(motifs.items(), key=lambda kv: -kv[1])},
            "caps": caps,
            "quality_hist": [round(v, 3) for v in hist],
            "skipped_exhausted": skipped,
            "privacy": {
                "raw_data_shared": False,
                "mechanisms": sorted({(s.get("dp") or {}).get("mechanism", "none")
                                      for s in summaries}),
                "min_epsilon": min([(s.get("dp") or {}).get("epsilon", float("inf"))
                                    for s in summaries] or [None]),
            },
        }
        self.rounds.append({"round": model["round"], "n_clients": len(summaries)})
        return model

    def best_tier_for(self, global_model: dict, capability: str) -> Optional[dict]:
        """全局经验问答：某能力用哪个档位历史表现最好（成功率优先，成本次之）。"""
        cands = []
        for k, st in (global_model.get("caps") or {}).items():
            if "|" not in k:
                continue
            cap, tier = k.rsplit("|", 1)
            if cap != capability:
                continue
            cands.append({"tier": tier, "success_rate": st.get("success_rate", 0),
                          "avg_cost": st.get("avg_cost", 0),
                          "count": st.get("count", 0)})
        if not cands:
            return None
        cands.sort(key=lambda c: (-c["success_rate"], c["avg_cost"]))
        return cands[0]


class InMemoryRecords:
    """轻量记忆适配器：让 FederatedClient 直接吃「调用方传进来的记录」，无需落盘。

    与 TopologyMemory 鸭子类型兼容（只需 `_load()` 返回 {"entries": [...]}），
    HTTP 端点收到 JSON 时可直接包一层，避免为一次聚合去创建临时文件。
    """

    def __init__(self, entries: Optional[list] = None):
        self.entries = list(entries or [])

    def _load(self) -> dict:
        return {"entries": self.entries}


def build_client(client_id: str, records: Optional[list] = None,
                 metrics_data: Optional[dict] = None, epsilon: float = 4.0,
                 seed: int = 0, min_support: int = 1,
                 max_releases: int = 1) -> FederatedClient:
    """从纯数据（记录列表 / 指标字典）构造一个联邦参与方。

    Parameters
    ----------
    records : list of {"spec": ..., "result": ...}
        本地拓扑记忆条目；只会被抽成 motif + 能力档位统计，原文不外传。
    metrics_data : {capability: {tier: {count, success, total_latency, total_cost}}}
        本地真实执行指标；会挂到内存态 ModelMetrics 上，回灌时可原地合并。
    """
    from .model_selector import ModelMetrics
    mem = InMemoryRecords(records) if records is not None else None
    metrics = None
    if metrics_data is not None:
        metrics = ModelMetrics(path=None)          # path=None → 纯内存，无副作用
        for cap, tiers in (metrics_data or {}).items():
            for tier, st in (tiers or {}).items():
                metrics._data.setdefault(cap, {})[tier] = {
                    "count": float(st.get("count", 0)),
                    "success": float(st.get("success", 0)),
                    "total_latency": float(st.get("total_latency", 0.0)),
                    "total_cost": float(st.get("total_cost", 0.0)),
                }
    elif records is not None:
        metrics = ModelMetrics(path=None)          # 给回灌准备一个落点
    return FederatedClient(client_id, memory=mem, metrics=metrics,
                           epsilon=epsilon, seed=seed,
                           min_support=min_support, max_releases=max_releases)


def run_federated_round(clients: list, server: Optional[FederatedServer] = None,
                        blend: float = 0.5) -> dict:
    """跑完整一轮联邦：各方提取摘要 → 聚合 → 回灌。"""
    server = server or FederatedServer(min_clients=1)
    summaries = [c.extract_summary() for c in clients]
    model = server.aggregate(summaries)
    applied = {}
    if "error" not in model:
        for c in clients:
            applied[c.client_id] = c.apply_global(model, blend=blend)
    return {"global_model": model, "applied": applied,
            "privacy_reports": {c.client_id: c.privacy_report() for c in clients}}


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def federated_selftest():
    """Phase 2 第三层② 联邦学习 离线自检（无 key/无网，3 实例模拟）。"""
    import os
    import tempfile
    os.environ.pop("AGENT_API_KEY", None)
    from .topology_memory import TopologyMemory
    from .model_selector import ModelMetrics

    SECRET = "腾讯2026年Q3财报净利润明细"      # 模拟敏感原始任务描述

    def make_client(cid, n, tier, ok_rate, seed, max_releases=3):
        tmp = tempfile.mktemp(suffix=".json")
        mem = TopologyMemory(path=tmp)
        rng = random.Random(seed)
        for i in range(n):
            ok = rng.random() < ok_rate
            spec = {
                "name": f"{cid}_task{i}",
                "components": {
                    "src": {"type": "power", "label": "task"},
                    "R1": {"type": "resistor", "label": "analyze", "model": tier},
                    "R2": {"type": "resistor", "label": "summarize", "model": "small"},
                },
                "wires": [["src", "R1"], ["R1", "R2"]],
            }
            mem.record(f"{SECRET}-{cid}-{i}", spec, {
                "success": ok, "final_quality": 0.9 if ok else 0.3,
                "total_latency_ms": 800 + rng.random() * 400,
                "total_cost": 0.01 + rng.random() * 0.01,
                "components": {"R1": {"ok": ok}},
            })
        return FederatedClient(cid, memory=mem, metrics=ModelMetrics(None),
                               epsilon=12.0, seed=seed,
                               max_releases=max_releases), tmp

    # A 用 tool 档成功率高；B/C 用 small 档成功率低 —— 看联邦能否学到「analyze 该用 tool」
    # 样本量取 60/50/50：DP 的信噪比 ∝ n·ε，样本太少时噪声会淹没信号（见用例 6）
    ca, ta = make_client("team-a", 60, "tool", 0.95, 1)
    cb, tb = make_client("team-b", 50, "small", 0.45, 2)
    cc, tc = make_client("team-c", 50, "small", 0.40, 3)
    clients = [ca, cb, cc]

    # 1) 摘要脱敏：绝不含原始 goal 文本 / spec 全文
    s_a = ca.extract_summary()
    blob = json.dumps(s_a, ensure_ascii=False)
    assert SECRET not in blob, "摘要中不得出现原始任务描述"
    assert "spec" not in s_a and "goal_desc" not in s_a, "摘要不得含 spec / goal_desc"
    assert "team-a" in blob and s_a["motifs"], "摘要应含结构统计（motif）"
    print(f"✓ 联邦② 脱敏: 摘要仅含结构+统计（{len(s_a['motifs'])} motif / "
          f"{len(s_a['caps'])} 能力档位），原始任务描述与 spec 全文均未出境")

    # 2) 差分隐私：加噪后与真值有偏差，但多次采样均值收敛到真值附近
    raw = ca._raw_summary()
    true_n = raw["n_samples"]
    samples = []
    for k in range(200):
        c = FederatedClient("probe", memory=ca.memory, epsilon=12.0, seed=1000 + k)
        samples.append(c.extract_summary()["n_samples"])
    mean = sum(samples) / len(samples)
    assert any(abs(s - true_n) > 1e-9 for s in samples), "应确实加了噪声（非恒等）"
    assert abs(mean - true_n) < true_n * 0.5, \
        f"拉普拉斯噪声均值应无偏，真值 {true_n} 实测均值 {mean:.2f}"
    print(f"✓ 联邦② 差分隐私: 单次加噪有偏（真值 {true_n}），200 次均值 {mean:.2f} 收敛回真值"
          f"（拉普拉斯无偏性）")

    # 3) 隐私预算记账：ε 顺序组合，累计不超总预算
    rep = ca.privacy_report()
    assert rep["epsilon_spent"] <= rep["epsilon_total"] + 1e-6, "不得超支隐私预算"
    assert len(rep["queries"]) > 5, "应逐查询记账"
    # ε 越小 → 噪声越大
    tight = FederatedClient("tight", memory=ca.memory, epsilon=0.05, seed=5)
    loose = FederatedClient("loose", memory=ca.memory, epsilon=500.0, seed=5)
    dev_t = abs(tight.extract_summary()["n_samples"] - true_n)
    dev_l = abs(loose.extract_summary()["n_samples"] - true_n)
    assert dev_t > dev_l, f"ε 小应噪声更大: ε=0.05 偏差 {dev_t} vs ε=50 偏差 {dev_l}"
    print(f"✓ 联邦② 隐私预算: 记账 {len(rep['queries'])} 条查询 · 已耗 "
          f"ε={rep['epsilon_spent']}/{rep['epsilon_total']} · "
          f"ε 越小噪声越大（{dev_t:.1f} vs {dev_l:.1f}）")

    # 4) FedAvg 聚合：按样本数加权，服务端只见摘要
    server = FederatedServer(min_clients=2)
    summaries = [c.extract_summary() for c in clients]
    model = server.aggregate(summaries)
    assert model["n_clients"] == 3 and model["total_samples"] > 20, "应聚合 3 方"
    assert model["privacy"]["raw_data_shared"] is False, "应声明未共享原始数据"
    blob2 = json.dumps(model, ensure_ascii=False)
    assert SECRET not in blob2, "全局模型中不得出现任何原始任务描述"
    assert model["motifs"], "全局应汇总结构经验"
    print(f"✓ 联邦② FedAvg 聚合: {model['n_clients']} 方 / "
          f"{model['total_samples']} 样本加权 · 全局 {len(model['caps'])} 档位统计 · "
          f"服务端零原始数据")

    # 5) 学到真知识：全局应认出 analyze 用 tool 档更好（A 的经验传给了 B/C）
    best = server.best_tier_for(model, "analyze")
    assert best is not None, "应能回答某能力最佳档位"
    assert best["tier"] == "tool", \
        f"analyze 应学到 tool 档最优（A 成功率 0.95），实际 {best}"
    small = [c for c in [server.best_tier_for(model, "analyze")] if c]
    print(f"✓ 联邦② 知识涌现: 全局学到 analyze→{best['tier']} 档最优 "
          f"(成功率 {best['success_rate']}) —— B/C 从未见过 A 的原始数据")

    # 5b) 诚实披露局限：样本太少时 DP 噪声会淹没信号（信噪比 ∝ n·ε）
    tiny_a, tta = make_client("tiny-a", 4, "tool", 1.0, 21, max_releases=1)
    tiny_b, ttb = make_client("tiny-b", 4, "small", 0.0, 22, max_releases=1)
    tiny_model = FederatedServer(min_clients=2).aggregate(
        [tiny_a.extract_summary(), tiny_b.extract_summary()])
    tiny_best = FederatedServer().best_tier_for(tiny_model, "analyze")
    tiny_correct = tiny_best is not None and tiny_best["tier"] == "tool"
    print(f"✓ 联邦② 局限已知: 每方仅 4 条样本时，加噪后结论"
          f"{'仍正确' if tiny_correct else '不可靠（噪声淹没信号）'}"
          f" —— 信噪比 ∝ n·ε，样本不足时应提高 ε 或积累更多样本再参与联邦")
    for p in (tta, ttb):
        try:
            os.unlink(p)
        except OSError:
            pass

    # 6) 回灌：B 的本地 ModelMetrics 获得全局先验
    before = len(getattr(cb.metrics, "_data", {}))
    applied = cb.apply_global(model, blend=0.5)
    after = len(getattr(cb.metrics, "_data", {}))
    assert applied["caps_merged"] > 0 and after > before, "回灌应写入本地指标"
    assert "analyze" in cb.metrics._data, "B 本地应获得 analyze 的全局经验"
    print(f"✓ 联邦② 回灌: team-b 合并 {applied['caps_merged']} 项全局档位经验 "
          f"（本地能力档位 {before}→{after}），立刻受益且从未接触他方原始数据")

    # 7) 参与方不足 → 拒绝聚合（防单方数据被反推）
    solo = FederatedServer(min_clients=2).aggregate([summaries[0]])
    assert "error" in solo, "单方不应聚合（否则等于明文回传该方统计）"
    print(f"✓ 联邦② 最小参与方: 仅 1 方时拒绝聚合（防单点反推）")

    # 8) 重复查询攻击防护：预算耗尽后拒绝再次发布
    #    （攻击者对同一数据反复查询、多次结果求平均即可消掉零均值噪声反推真值）
    victim, tv = make_client("victim", 6, "small", 0.5, 42, max_releases=2)
    ok1 = victim.extract_summary()
    ok2 = victim.extract_summary()
    assert "error" not in ok1 and "error" not in ok2, "预算内的发布应成功"
    blocked = victim.extract_summary()
    assert blocked.get("error") == "privacy_budget_exhausted", \
        f"超出声明发布次数应被拒绝，实际 {blocked}"
    try:
        victim.extract_summary(strict=True)
        raise AssertionError("strict 模式应抛异常")
    except PrivacyBudgetExhausted:
        pass
    vr = victim.privacy_report()
    assert vr["releases"] == 2 and vr["releases"] <= vr["max_releases"], "发布次数应受限"
    # 被拒的摘要不得污染聚合
    agg = FederatedServer(min_clients=1).aggregate([summaries[1], blocked])
    assert agg["skipped_exhausted"] == ["victim"] and agg["n_clients"] == 1, \
        "预算耗尽的摘要应被跳过而非计入"
    print(f"✓ 联邦② 重复查询防护: 声明 2 次发布 → 第 3 次被拒（strict 抛异常）· "
          f"被拒摘要不污染聚合（skipped={agg['skipped_exhausted']}）")
    try:
        os.unlink(tv)
    except OSError:
        pass

    # 9) 端到端一轮
    ca2, t2a = make_client("x", 5, "tool", 0.9, 11)
    cb2, t2b = make_client("y", 5, "small", 0.4, 12)
    rnd = run_federated_round([ca2, cb2], FederatedServer(min_clients=2), blend=0.5)
    assert rnd["global_model"]["n_clients"] == 2, "端到端应聚合 2 方"
    assert set(rnd["applied"]) == {"x", "y"}, "两方都应完成回灌"
    assert all(r["epsilon_spent"] <= r["epsilon_total"] + 1e-6
               for r in rnd["privacy_reports"].values()), "各方均不得超支"
    print(f"✓ 联邦② 端到端: 提取→聚合→回灌 一轮完成 · 2 方均回灌且隐私预算未超支")

    # 10) 纯数据构造（HTTP 端点用）：不落盘、不建 TopologyMemory 也能参与联邦
    _rec = lambda tier, ok: {                                    # noqa: E731
        "spec": {"components": {
            "src": {"type": "power", "label": "task"},
            "R1": {"type": "resistor", "label": "analyze", "model": tier}},
            "wires": [["src", "R1"]]},
        "result": {"success": ok, "final_quality": 0.9 if ok else 0.3,
                   "total_latency_ms": 800, "total_cost": 0.01},
    }
    p1 = build_client("p1", records=[_rec("tool", True)] * 30, epsilon=10.0, seed=1)
    p2 = build_client("p2", records=[_rec("small", False)] * 30, epsilon=10.0, seed=2)
    pr = run_federated_round([p1, p2], FederatedServer(min_clients=2))
    assert pr["global_model"]["n_clients"] == 2, "纯数据构造应能正常聚合"
    pbest = FederatedServer().best_tier_for(pr["global_model"], "analyze")
    assert pbest and pbest["tier"] == "tool", f"应学到 analyze→tool，实际 {pbest}"
    assert getattr(p2.metrics, "_data", {}).get("analyze"), "p2 应通过回灌获得 analyze 经验"
    print(f"✓ 联邦② 纯数据参与: 无需落盘即可组网（build_client）· "
          f"全局仍学到 analyze→{pbest['tier']}（成功率 {round(pbest['success_rate'], 4)}）")

    for p in (ta, tb, tc, t2a, t2b):
        try:
            os.unlink(p)
        except OSError:
            pass

    print("\nPhase 2 第三层② 联邦学习 离线自检全部通过 ✓")


if __name__ == "__main__":
    federated_selftest()
