"""Phase 2 · 第三层范式进化 ④ —— 形式化验证（内建符号验证器，零外部依赖）

问题：电路拓扑越来越复杂（自适应变更 ⑪、RL 搜索 ①、composite 展开 ③），人工
肉眼已无法保证「这个拓扑一定跑得通 / 不会死锁 / 成本有上界 / 质量达标」。
零容错场景（金融、医疗、安全）需要**执行前**的形式化证明，而非执行后才发现挂了。

思路（内建符号验证器，6 个维度）：
  1. 无环性 —— 排除声明 feedback 边后，拓扑必须是 DAG。三色 DFS 检测环，给反例路径。
  2. 可达性 —— 每个节点都从 power 源可达（无孤儿/断片）。BFS。
  3. 数据流输入完备性 —— 每个 required_input 都有上游 produced_outputs 覆盖。
     沿拓扑序模拟 produced_outputs 累积透传（与 runtime._run_one 逻辑一致）。
  4. 死锁自由 —— feedback 环必须有 watchdog 重试上限，否则无界重试 = 死锁风险。
  5. 资源上界 —— worst-case cost/latency 可证明有界（Σ 单次 × max_retries）。
  6. 质量下界传播 —— 沿 DAG 传播质量下界（resistor = min(上游) × accuracy × yield），
     最终 terminal 下界 ≥ 声明 quality_gate。

每个维度返回 {name, status, detail, counterexample?}。fail 时给反例路径/数据。
零外部依赖（不 import runtime，内联保守 tier 统计）。

与 runtime 自检的区别：runtime 自检是**执行后**验证（跑一遍看结果对不对）；
本模块是**执行前**符号推理（不跑，纯静态分析），适合零容错场景的前置门禁。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


# 保守档位统计（与 SimBackend._TIERS 对齐但独立维护，确保零依赖）
_TIER_STATS = {
    "small":  {"accuracy": 0.70, "cost": 0.001, "latency_ms": 200},
    "large":  {"accuracy": 0.92, "cost": 0.020, "latency_ms": 1500},
    "tool":   {"accuracy": 0.99, "cost": 0.005, "latency_ms": 800},
}
_DEFAULT_STATS = {"accuracy": 0.50, "cost": 0.10, "latency_ms": 2000}


class CheckResult:
    """单维度验证结果。"""
    __slots__ = ("name", "status", "detail", "counterexample")

    def __init__(self, name: str, status: str, detail: str = "",
                 counterexample=None):
        self.name = name
        self.status = status            # "pass" | "fail"
        self.detail = detail
        self.counterexample = counterexample

    def to_dict(self) -> dict:
        d = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.counterexample is not None:
            d["counterexample"] = self.counterexample
        return d

    def __repr__(self):
        return f"CheckResult({self.name}={self.status})"


def _forward_wires(spec: dict) -> list:
    """排除声明 feedback 边后的前向边列表。"""
    fb = spec.get("feedback")
    if not fb:
        return list(spec.get("wires", []))
    fb_edge = [fb.get("from"), fb.get("to")]
    return [w for w in spec.get("wires", []) if [w[0], w[1]] != fb_edge]


def _topo_sort(comps: dict, fwd: list) -> Optional[list]:
    """Kahn 拓扑排序。有环返回 None。"""
    indeg = {c: 0 for c in comps}
    succ = defaultdict(list)
    for a, b in fwd:
        if a in comps and b in comps:
            succ[a].append(b)
            indeg[b] += 1
    ready = [c for c in comps if indeg[c] == 0]
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    return order if len(order) == len(comps) else None


class FormalVerifier:
    """内建符号验证器：对拓扑 spec 做执行前形式化验证。零外部依赖。"""

    def __init__(self, tier_stats: Optional[dict] = None):
        self.tier_stats = tier_stats or _TIER_STATS

    # ---- 主入口 ----

    def verify(self, spec: dict) -> dict:
        """全维度验证。返回 {checks, all_pass, summary, proven}。"""
        checks = [
            self.check_acyclicity(spec),
            self.check_reachability(spec),
            self.check_input_completeness(spec),
            self.check_deadlock_freedom(spec),
            self.check_resource_bounds(spec),
            self.check_quality_lower_bound(spec),
        ]
        npass = sum(1 for c in checks if c.status == "pass")
        return {
            "checks": [c.to_dict() for c in checks],
            "all_pass": npass == len(checks),
            "summary": f"{npass}/{len(checks)} 通过",
            "proven": npass == len(checks),
        }

    # ---- 1. 无环性 ----

    def check_acyclicity(self, spec: dict) -> CheckResult:
        """排除声明 feedback 边后必须是 DAG。三色 DFS，给环路径反例。"""
        comps = spec.get("components", {})
        fwd = _forward_wires(spec)
        succ = defaultdict(list)
        for a, b in fwd:
            if a in comps and b in comps:
                succ[a].append(b)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {c: WHITE for c in comps}
        cycle_path = []

        def dfs(n, stack):
            color[n] = GRAY
            stack.append(n)
            for m in succ[n]:
                if color[m] == GRAY:
                    idx = stack.index(m)
                    cycle_path.extend(stack[idx:] + [m])
                    return True
                if color[m] == WHITE and dfs(m, stack):
                    return True
            stack.pop()
            color[n] = BLACK
            return False

        for c in comps:
            if color[c] == WHITE and dfs(c, []):
                return CheckResult("acyclicity", "fail",
                    f"检测到环: {' → '.join(cycle_path)}",
                    counterexample=cycle_path)
        return CheckResult("acyclicity", "pass",
            "DAG 验证通过（声明 feedback 边已排除）")

    # ---- 2. 可达性 ----

    def check_reachability(self, spec: dict) -> CheckResult:
        """每个节点从 power 源可达（无孤儿/断片）。"""
        comps = spec.get("components", {})
        fwd = _forward_wires(spec)
        sources = [c for c, v in comps.items() if v.get("type") == "power"]
        if not sources:
            return CheckResult("reachability", "fail", "无 power 源节点")
        succ = defaultdict(list)
        for a, b in fwd:
            succ[a].append(b)
        visited = set()
        queue = list(sources)
        while queue:
            n = queue.pop(0)
            if n in visited:
                continue
            visited.add(n)
            queue.extend(succ[n])
        orphans = [c for c in comps if c not in visited]
        if orphans:
            return CheckResult("reachability", "fail",
                f"孤儿节点（从 power 不可达）: {orphans}",
                counterexample=orphans)
        return CheckResult("reachability", "pass",
            f"全部 {len(comps)} 节点从 power 可达")

    # ---- 3. 数据流输入完备性 ----

    def check_input_completeness(self, spec: dict) -> CheckResult:
        """每个 required_input 都有上游 produced_outputs 覆盖。

        沿拓扑序模拟 produced_outputs 累积透传（与 runtime._run_one 一致）。
        """
        comps = spec.get("components", {})
        fwd = _forward_wires(spec)
        pred = defaultdict(list)
        for a, b in fwd:
            pred[b].append(a)
        order = _topo_sort(comps, fwd)
        if order is None:
            return CheckResult("input_completeness", "fail",
                "拓扑有环，无法做数据流分析")
        # 累积透传 produced_outputs
        node_outputs: dict = {}
        for cid in order:
            comp = comps[cid]
            own = set(comp.get("produced_outputs") or [])
            upstream = set()
            for p in pred[cid]:
                upstream |= node_outputs.get(p, set())
            node_outputs[cid] = own | upstream
        # 检查每个 required_input
        failures = []
        for cid in order:
            comp = comps[cid]
            req = comp.get("required_inputs") or []
            if not req:
                continue
            input_map = comp.get("input_map") or {}
            available = set()
            for p in pred[cid]:
                available |= node_outputs.get(p, set())
            for r in req:
                actual = input_map.get(r, r)
                if actual not in available:
                    failures.append({"node": cid, "input": r,
                        "actual": actual, "available": sorted(available),
                        "predecessors": pred[cid]})
        if failures:
            return CheckResult("input_completeness", "fail",
                f"{len(failures)} 个声明输入无上游 producer",
                counterexample=failures)
        return CheckResult("input_completeness", "pass",
            "所有 required_input 都有上游 produced_outputs 覆盖")

    # ---- 4. 死锁自由 ----

    def check_deadlock_freedom(self, spec: dict) -> CheckResult:
        """feedback 环必须有 watchdog 重试上限，否则无界重试 = 死锁风险。"""
        fb = spec.get("feedback")
        if not fb:
            return CheckResult("deadlock_freedom", "pass",
                "无 feedback 环，天然无死锁")
        wd = spec.get("watchdog") or {}
        max_ret = (wd.get("max_retries") or fb.get("max_retries")
                   or spec.get("max_retries"))
        if not max_ret:
            return CheckResult("deadlock_freedom", "fail",
                f"feedback 环 {fb.get('from')}→{fb.get('to')} "
                f"无 watchdog 重试上限约束（无界重试风险）",
                counterexample=[fb.get("from"), fb.get("to")])
        return CheckResult("deadlock_freedom", "pass",
            f"feedback 环有 watchdog 约束 (max_retries={max_ret})")

    # ---- 5. 资源上界 ----

    def check_resource_bounds(self, spec: dict) -> CheckResult:
        """worst-case cost/latency 可证明有界。"""
        comps = spec.get("components", {})
        fb = spec.get("feedback")
        wd = spec.get("watchdog") or {}
        if fb:
            max_ret = (wd.get("max_retries") or fb.get("max_retries")
                       or spec.get("max_retries"))
            if not max_ret:
                return CheckResult("resource_bounds", "fail",
                    "有 feedback 但无重试上限 → cost/latency 无界",
                    counterexample="unbounded_retries")
            max_ret = int(max_ret)
        else:
            max_ret = 1

        total_cost = 0.0
        total_lat = 0.0
        detail_parts = []
        for cid, comp in comps.items():
            ctype = comp.get("type", "")
            if ctype in ("power", "capacitor", "diode"):
                c, l = 0.0, 0.0
            elif ctype == "resistor":
                tier = comp.get("model", "default")
                st = self.tier_stats.get(tier, _DEFAULT_STATS)
                c, l = st["cost"], st["latency_ms"]
            else:
                tier = comp.get("model", "default")
                st = self.tier_stats.get(tier, _DEFAULT_STATS)
                c, l = st["cost"], st["latency_ms"]
            total_cost += c
            total_lat += l
            detail_parts.append(f"{cid}({ctype}:{c}/{l}ms)")
        worst_cost = total_cost * max_ret
        worst_lat = total_lat * max_ret        # 保守串行上界
        return CheckResult("resource_bounds", "pass",
            f"cost≤{round(worst_cost, 5)} · latency≤{round(worst_lat)}ms "
            f"· max_retries={max_ret} · Σ单次({round(total_cost,5)}/{round(total_lat)}ms)")

    # ---- 6. 质量下界传播 ----

    def check_quality_lower_bound(self, spec: dict) -> CheckResult:
        """沿 DAG 传播质量下界，最终 terminal ≥ 声明 quality_gate。"""
        comps = spec.get("components", {})
        fwd = _forward_wires(spec)
        pred = defaultdict(list)
        succ = defaultdict(list)
        for a, b in fwd:
            pred[b].append(a)
            succ[a].append(b)
        order = _topo_sort(comps, fwd)
        if order is None:
            return CheckResult("quality_lower_bound", "fail",
                "拓扑有环，无法做质量传播分析")

        q_lb: dict = {}
        for cid in order:
            comp = comps[cid]
            ctype = comp.get("type", "")
            preds_q = [q_lb.get(p, 1.0) for p in pred[cid]]
            base = min(preds_q) if preds_q else 1.0
            if ctype == "power":
                q_lb[cid] = 1.0
            elif ctype == "resistor":
                tier = comp.get("model", "default")
                acc = self.tier_stats.get(tier, _DEFAULT_STATS)["accuracy"]
                yld = float(comp.get("yield", 1.0))
                q_lb[cid] = base * acc * yld
            elif ctype in ("verify", "capacitor", "diode", "adc"):
                q_lb[cid] = base       # 过滤/汇合不降质量下界
            else:
                q_lb[cid] = base * 0.5  # 未知类型保守

        terminals = [c for c in comps if not succ[c]]
        min_final = min((q_lb.get(c, 0.0) for c in terminals), default=0.0)
        gate = spec.get("quality_gate")
        detail = (f"最终质量下界 {round(min_final, 4)}"
                  + (f" ≥ 阈值 {gate}" if gate is not None else "（未声明阈值）")
                  + f" · terminal下界={{{', '.join(f'{c}:{round(q_lb.get(c,0),3)}' for c in terminals)}}}")
        if gate is not None and min_final < float(gate):
            return CheckResult("quality_lower_bound", "fail", detail,
                counterexample={"min_final": round(min_final, 4),
                                "gate": gate,
                                "terminal_bounds": {c: round(q_lb.get(c, 0), 4)
                                                    for c in terminals}})
        return CheckResult("quality_lower_bound", "pass", detail)


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def formal_verifier_selftest():
    """Phase 2 第三层④ 形式化验证 离线自检。"""
    import os
    os.environ.pop("AGENT_API_KEY", None)

    fv = FormalVerifier()

    # 1) 合法 spec → all_pass
    good = {"name": "ok", "components": {
        "src": {"type": "power", "label": "task"},
        "A": {"type": "resistor", "label": "research", "model": "large",
              "yield": 1.0, "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "analyze", "model": "tool",
              "yield": 1.0, "required_inputs": ["a"],
              "produced_outputs": ["b"]},
        "V": {"type": "verify", "label": "verify", "threshold": 0.5},
        "C": {"type": "resistor", "label": "summarize", "model": "small",
              "yield": 1.0, "required_inputs": ["b"],
              "produced_outputs": ["summary"]}},
        "wires": [["src", "A"], ["A", "B"], ["B", "V"], ["V", "C"]],
        "quality_gate": 0.5}
    r_good = fv.verify(good)
    assert r_good["all_pass"] is True, \
        f"合法 spec 应全通过，实际 {r_good['summary']}: " \
        f"{[c for c in r_good['checks'] if c['status']!='pass']}"
    assert r_good["proven"] is True
    print(f"✓ 形式化④ 合法: 6/6 通过 · "
          f"质量下界 {[c for c in r_good['checks'] if c['name']=='quality_lower_bound'][0]['detail']}")

    # 2) 有环 → acyclicity fail + 反例路径
    cyclic = {"name": "cyc", "components": {
        "src": {"type": "power", "label": "t"},
        "A": {"type": "resistor", "label": "a", "model": "small"},
        "B": {"type": "resistor", "label": "b", "model": "small"},
        "C": {"type": "resistor", "label": "c", "model": "small"}},
        "wires": [["src", "A"], ["A", "B"], ["B", "C"], ["C", "A"]]}
    r_cyc = fv.verify(cyclic)
    ac = [c for c in r_cyc["checks"] if c["name"] == "acyclicity"][0]
    assert ac["status"] == "fail", "有环应 fail"
    assert ac["counterexample"], "应给反例路径"
    print(f"✓ 形式化④ 环检测: 反例 {' → '.join(ac['counterexample'])}")

    # 3) 孤儿节点 → reachability fail
    orphan = {"name": "orph", "components": {
        "src": {"type": "power", "label": "t"},
        "A": {"type": "resistor", "label": "a", "model": "small"},
        "X": {"type": "resistor", "label": "x", "model": "small"}},  # 孤儿
        "wires": [["src", "A"]]}
    r_orph = fv.verify(orphan)
    rc = [c for c in r_orph["checks"] if c["name"] == "reachability"][0]
    assert rc["status"] == "fail" and "X" in rc["counterexample"], \
        f"孤儿 X 应被检出，实际 {rc}"
    print(f"✓ 形式化④ 孤儿检测: {rc['counterexample']}")

    # 4) required_input 无 producer → input_completeness fail
    missing = {"name": "miss", "components": {
        "src": {"type": "power", "label": "t"},
        "A": {"type": "resistor", "label": "a", "model": "small",
              "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "b", "model": "small",
              "required_inputs": ["nonexistent"]}},  # 无 producer
        "wires": [["src", "A"], ["A", "B"]]}
    r_miss = fv.verify(missing)
    ic = [c for c in r_miss["checks"] if c["name"] == "input_completeness"][0]
    assert ic["status"] == "fail", "缺 producer 应 fail"
    assert ic["counterexample"][0]["input"] == "nonexistent", \
        f"反例应指 nonexistent，实际 {ic['counterexample']}"
    print(f"✓ 形式化④ 输入完备: 反例 {ic['counterexample'][0]}")

    # 5) feedback 无 watchdog → deadlock_freedom fail
    no_wd = {"name": "nowd", "components": {
        "src": {"type": "power", "label": "t"},
        "A": {"type": "resistor", "label": "a", "model": "small",
              "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "b", "model": "small",
              "required_inputs": ["a"], "produced_outputs": ["b"]}},
        "wires": [["src", "A"], ["A", "B"], ["B", "A"]],
        "feedback": {"from": "B", "to": "A"}}
    r_nwd = fv.verify(no_wd)
    dl = [c for c in r_nwd["checks"] if c["name"] == "deadlock_freedom"][0]
    assert dl["status"] == "fail", "无 watchdog 的 feedback 应 fail"
    print(f"✓ 形式化④ 死锁检测: {dl['detail']}")

    # 6) feedback 有 watchdog → deadlock_freedom pass
    with_wd = dict(no_wd)
    with_wd["watchdog"] = {"max_retries": 3}
    r_wd = fv.verify(with_wd)
    dl2 = [c for c in r_wd["checks"] if c["name"] == "deadlock_freedom"][0]
    assert dl2["status"] == "pass", "有 watchdog 应 pass"
    rb = [c for c in r_wd["checks"] if c["name"] == "resource_bounds"][0]
    assert rb["status"] == "pass" and "max_retries=3" in rb["detail"]
    print(f"✓ 形式化④ 有界重试: {dl2['detail']} · {rb['detail']}")

    # 7) 质量下界 < quality_gate → fail
    low_q = {"name": "lowq", "components": {
        "src": {"type": "power", "label": "t"},
        "A": {"type": "resistor", "label": "a", "model": "small",
              "yield": 0.3, "produced_outputs": ["a"]}},  # acc 0.7 × yld 0.3 = 0.21
        "wires": [["src", "A"]],
        "quality_gate": 0.5}
    r_lq = fv.verify(low_q)
    ql = [c for c in r_lq["checks"] if c["name"] == "quality_lower_bound"][0]
    assert ql["status"] == "fail", \
        f"质量下界 0.21 < 0.5 应 fail，实际 {ql}"
    assert ql["counterexample"]["min_final"] < 0.5
    print(f"✓ 形式化④ 质量下界: {ql['detail']} → fail（可证不达标）")

    # 8) 资源上界可证明
    r_bound = [c for c in r_good["checks"] if c["name"] == "resource_bounds"][0]
    assert r_bound["status"] == "pass" and "cost≤" in r_bound["detail"]
    print(f"✓ 形式化④ 资源上界: {r_bound['detail']}")

    print("\nPhase 2 第三层④ 形式化验证 离线自检全部通过 ✓")


if __name__ == "__main__":
    formal_verifier_selftest()
