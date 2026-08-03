"""M3 · Optimizer（布局布线优化器）

闭包（与 COMPILER.md §5 同构）：
    生成候选(config) → Evaluator(runtime.py 仿真) → 打分 → 贪心/搜索改 config → 再仿

搜索空间（4 旋钮，靠标准单元库剪枝，不暴力枚举）：
    ① pattern   : 串联(None) / 并联([]) / DAG
    ② tiers     : 每能力 small/large/tool（Binder 给最小达标档，可升降）
    ③ redundancy: {cap: K} 副本，由 capacitor(mode=any) 收口
    ④ feedback  : on/off + max_iter（末级汇合门控整链重试）

目标函数：min cost
约束：latency ≤ L_max ∧ quality ≥ Q_min ∧ ¬watchdog_tripped（即三项约束全过）

两阶段：
    ① 贪心：Binder 最小档 + 并联 → hill-climb 修约束（升级档/加反馈/加冗余/降档省成本）
    ② 搜索：枚举 候选 × 收集可行 → Pareto 前沿 → 选 min-cost 可行解

Evaluator 与 demo(D/E/F) 共用同一套种子派生（random.Random(seed)→逐轮 random.Random(rng.random())），
保证指标可比。runtime 一行未改。
"""

import json
import os
import random

import runtime

from .binder import Binder
from .goal import Goal
from .router import Router

_TIER_ORDER = {"small": 0, "large": 1, "tool": 2}


class Optimizer:
    def __init__(self, runs: int = 200, seed: int = 7):
        self.runs = runs
        self.seed = seed
        self._cache = {}  # config-key -> metrics，避免重复仿真

    # ---------------------------------------------------------------- Evaluator
    def evaluate(self, goal: Goal, tiers=None, dependencies=None,
                 redundancy=None, feedback=None) -> dict:
        """在给定 config 下构建 spec，跑 N 次 runtime 仿真，返回可比指标。"""
        g = Goal.from_dict(goal.to_dict())  # 深拷贝，互不污染
        if tiers is not None:
            g.tiers = dict(tiers)
        if dependencies is not None:
            g.dependencies = dependencies
        if redundancy is not None:
            g.redundancy = dict(redundancy)
        if feedback is not None:
            g.feedback = dict(feedback) if feedback else None

        key = json.dumps(
            {"t": g.tiers, "d": g.dependencies, "r": g.redundancy,
             "f": g.feedback}, sort_keys=True, ensure_ascii=False)
        if key in self._cache:
            return self._cache[key]

        spec = Router().route(g)  # Router 已涵盖 series/并联/反馈/冗余全部情形

        n = self.runs
        rng = random.Random(self.seed)
        cost = lat = q = 0.0
        n_out = 0       # 产出非空（final_quality>0）轮次
        n_all = 0       # 全能力交付（pmerge.ok）轮次
        for _ in range(n):
            r = random.Random(rng.random())
            circ = runtime.Circuit(spec, runtime.SimBackend(r))
            res = circ.execute()
            cost += res["total_cost"]
            lat += res["total_latency_ms"]
            q += res["final_quality"]
            if res["final_quality"] > 0:
                n_out += 1
            comps = res["components"]
            if comps.get("pmerge", {}).get("ok", False):
                n_all += 1

        c = goal.constraints
        avg_cost, avg_lat, avg_q = cost / n, lat / n, q / n
        max_lat = c.get("max_latency_ms")
        max_cost = c.get("max_cost")
        min_q = c.get("min_quality", 0.0)
        feasible = ((max_lat is None or avg_lat <= max_lat)
                    and (max_cost is None or avg_cost <= max_cost)
                    and avg_q >= min_q)
        metrics = {
            "tiers": dict(g.tiers), "dependencies": g.dependencies,
            "redundancy": dict(g.redundancy), "feedback": g.feedback,
            "avg_cost": avg_cost, "avg_latency": avg_lat, "avg_quality": avg_q,
            "out_rate": n_out / n, "all_fired_rate": n_all / n,
            "feasible": feasible, "spec": spec,
            "violation": self._violation(avg_cost, avg_lat, avg_q, c),
        }
        self._cache[key] = metrics
        return metrics

    @staticmethod
    def _violation(cost, lat, q, c) -> float:
        """不可行时，约束违反量（越小越好）；约束键缺失视为无该限制。"""
        v = 0.0
        if "max_latency_ms" in c:
            v += max(0.0, lat - c["max_latency_ms"]) / max(1.0, c["max_latency_ms"])
        if "max_cost" in c:
            v += max(0.0, cost - c["max_cost"]) / max(1e-6, c["max_cost"])
        if "min_quality" in c:
            v += max(0.0, c["min_quality"] - q)
        return v

    @staticmethod
    def _better(a, b) -> bool:
        """a 是否优于 b（可行性优先，再比 cost/latency/quality）。"""
        if a["feasible"] and not b["feasible"]:
            return True
        if not a["feasible"] and b["feasible"]:
            return False
        if a["feasible"] and b["feasible"]:
            if abs(a["avg_cost"] - b["avg_cost"]) > 1e-9:
                return a["avg_cost"] < b["avg_cost"]
            if abs(a["avg_latency"] - b["avg_latency"]) > 1e-9:
                return a["avg_latency"] < b["avg_latency"]
            return a["avg_quality"] > b["avg_quality"]
        return a["violation"] < b["violation"]

    # ---------------------------------------------------------------- 贪心
    def _moves(self, goal: Goal, cfg: dict):
        """相对当前 config 生成邻域候选（标准单元库剪枝，不暴力枚举）。"""
        caps = goal.capabilities
        tiers = dict(cfg["tiers"])
        moves = []
        # 升级 / 降级 单个能力档
        for cap in caps:
            cur = tiers.get(cap, "small")
            o = _TIER_ORDER[cur]
            if o < 2:
                nt = {**tiers, cap: ["small", "large", "tool"][o + 1]}
                moves.append(("升档:" + cap, {**cfg, "tiers": nt}))
            if o > 0:
                nt = {**tiers, cap: ["small", "large", "tool"][o - 1]}
                moves.append(("降档:" + cap, {**cfg, "tiers": nt}))
        # 反馈环 开/关
        if cfg["feedback"] is None:
            moves.append(("开反馈", {**cfg, "feedback": {"max_iter": 3}}))
        else:
            moves.append(("关反馈", {**cfg, "feedback": None}))
        # 冗余 加/减（仅对关键能力，避免组合爆炸）
        red = dict(cfg["redundancy"])
        for cap in caps:
            if red.get(cap, 1) < 2:
                moves.append((f"冗余:{cap}", {**cfg, "redundancy": {**red, cap: 2}}))
            else:
                rd = {k: v for k, v in red.items() if k != cap}
                moves.append((f"去冗余:{cap}", {**cfg, "redundancy": rd}))
        # 布线 串联/并联 互转
        if cfg["dependencies"] is not None:
            moves.append(("改串联", {**cfg, "dependencies": None}))
        else:
            moves.append(("改并联", {**cfg, "dependencies": []}))
        return moves

    def greedy(self, goal: Goal, max_iter: int = 10) -> dict:
        """贪心：Binder 最小档 + 并联 起手，hill-climb 修约束。"""
        tiers0 = Binder().bind(goal)
        cur = dict(tiers=tiers0, dependencies=[], redundancy={}, feedback=None)
        cur_m = self.evaluate(goal, **cur)
        best = (dict(cur), cur_m)
        for _ in range(max_iter):
            if cur_m["feasible"]:
                break
            improved = None
            for _mv, cfg in self._moves(goal, cur):
                m = self.evaluate(goal, **cfg)
                if self._better(m, cur_m) and (improved is None
                                               or self._better(m, improved[1])):
                    improved = (cfg, m)
            if improved is None:
                break
            cur, cur_m = improved
            if self._better(cur_m, best[1]):
                best = (dict(cur), cur_m)
        # 若已可行，再做一轮"省成本"微调（在保持可行的邻域里挑更便宜的）
        if cur_m["feasible"]:
            for _mv, cfg in self._moves(goal, cur):
                m = self.evaluate(goal, **cfg)
                if m["feasible"] and self._better(m, cur_m):
                    cur, cur_m = cfg, m
        return {
            "config": cur, "metrics": cur_m,
            "best_feasible": best[1]["feasible"],
        }

    # ---------------------------------------------------------------- 搜索
    @staticmethod
    def _pareto(feas):
        """在可行解上求 Pareto 前沿（min cost / min latency / max quality）。"""
        front = []
        for a in feas:
            dominated = False
            for b in feas:
                if a is b:
                    continue
                ba = (b["avg_cost"] <= a["avg_cost"] and b["avg_latency"] <= a["avg_latency"]
                      and b["avg_quality"] >= a["avg_quality"]
                      and (b["avg_cost"] < a["avg_cost"] or b["avg_latency"] < a["avg_latency"]
                           or b["avg_quality"] > a["avg_quality"]))
                if ba:
                    dominated = True
                    break
            if not dominated:
                front.append(a)
        front.sort(key=lambda x: (x["avg_cost"], x["avg_latency"]))
        return front

    def search(self, goal: Goal) -> dict:
        """枚举 候选 × 收集可行 → Pareto 前沿 → 选 min-cost 可行解。"""
        tier_sets = [
            Binder().bind(goal),
            {c: "tool" for c in goal.capabilities},
            {c: "large" for c in goal.capabilities},
        ]
        patterns = [None, []]                       # 串联 / 并联
        feeds = [None, {"max_iter": 3}]
        reds = [{}]
        if goal.capabilities:
            reds.append({c: 2 for c in goal.capabilities})   # 全冗余
            if "verify" in goal.capabilities:
                reds.append({"verify": 2})                  # 仅关键能力冗余
        candidates = []
        for t in tier_sets:
            for p in patterns:
                for f in feeds:
                    for r in reds:
                        cfg = dict(tiers=t, dependencies=p,
                                   redundancy=r, feedback=f)
                        m = self.evaluate(goal, **cfg)
                        candidates.append(m)
        feas = [c for c in candidates if c["feasible"]]
        front = self._pareto(feas)
        best = min(feas, key=lambda x: x["avg_cost"]) if feas else None
        return {"candidates": candidates, "feasible": feas,
                "front": front, "best": best}

    # ---------------------------------------------------------------- 总入口
    def optimize(self, goal_dict: dict) -> dict:
        goal = Goal.from_dict(goal_dict)
        g = self.greedy(goal)
        s = self.search(goal)
        # 最终解：搜索得到的 min-cost 可行解（若存在），否则贪心最好解
        final = s["best"] if s["best"] is not None else g["metrics"]
        return {
            "goal": goal,
            "greedy": g,
            "search": s,
            "final": final,
            "spec": final["spec"],
        }
