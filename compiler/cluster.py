"""分布式执行（Phase 2 ③ 新增）：多 circuit-agents 实例组成集群协同。

设计（与项目一致，离线安全、内核零改动优先）：
- **分片（partition）**：把编译出的拓扑按「弱连通分量(WCC)」切开——互相没有连线的
  子图天然独立、可完全并行、无需跨 worker 数据交接。若 WCC 数 > 请求 worker 数，
  则贪心合并最小的若干 WCC 直至 ≤ n_workers（合并的子图仍互不相连，安全）。
- **派发（dispatch）**：每个分片交给一个 worker 实例（独立 SimBackend + 独立
  CircuitExecutor + 独立 state 黑板 → 资源隔离），线程并发执行。
- **聚合（aggregate）**：合并各 worker 结果，final_quality 按分片节点数加权均值，
  总成本/延迟求和，failed_nodes/quality_gate 合并；原始 per_worker 结果完整保留。
- **可插拔 transport**：`ClusterCoordinator(transport=...)` 接受任意实现了
  `execute(worker_id, sub_spec, seed) -> result_dict` 的对象。默认 None → 进程内线程
  worker（离线可跑）。真实远程 transport（HTTP/RPC 起多个 server 实例）留作后端注入，
  不在此实现联网逻辑——本模块只定义协调协议。

与 ⑥ BatchExecutor 的区别：Batch 是「多个独立 goal 各自跑完整电路」；Cluster 是
「同一个 goal 的拓扑被切成子图、分散到多 worker 协同算、再聚合」——更贴近
『一个任务分布式执行』的语义。
"""

import os
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor


# ---------------------------------------------------------------------------
# 分片：弱连通分量（union-find）
# ---------------------------------------------------------------------------

def _weak_components(components: dict, wires: list):
    """返回组件列表的弱连通分量（每个分量是 cid 列表）。"""
    parent = {c: c for c in components}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in wires:
        if a in parent and b in parent:
            union(a, b)
    groups = {}
    for c in components:
        groups.setdefault(find(c), []).append(c)
    return list(groups.values())


def _merge_to_n(groups: list, n_workers: int):
    """WCC 数 > n_workers 时，贪心合并最小的分量直到数量 ≤ n_workers。"""
    gs = sorted(groups, key=len)
    while len(gs) > n_workers and len(gs) > 1:
        a = gs.pop(0)
        b = gs.pop(0)
        gs.append(a + b)
        gs.sort(key=len)
    return gs


# ---------------------------------------------------------------------------
# 集群协调器
# ---------------------------------------------------------------------------

class ClusterCoordinator:
    """把一个目标（goal 或已编译 spec）分布式执行到多个 worker 并聚合结果。

    transport=None → 进程内线程 worker（离线安全）。注入真实 transport 即可走向
    多实例/多机集群，而协调协议不变。
    """

    def __init__(self, transport=None, n_workers: int = 2, seed_base: int = 0):
        self.transport = transport
        self.n_workers = max(1, int(n_workers))
        self.seed_base = seed_base

    # ---- 分片 ----
    def partition(self, spec: dict, n_workers: int = None):
        """把 spec 切成若干独立子 spec（无跨片连线）。返回子 spec 列表。"""
        comps = spec.get("components", {})
        wires = spec.get("wires", [])
        n = n_workers or self.n_workers
        groups = _weak_components(comps, wires)
        if n < len(groups):
            groups = _merge_to_n(groups, n)
        shards = []
        for i, g in enumerate(groups):
            sub = {
                "name": f"shard_{i}",
                "description": spec.get("description", ""),
                "components": {c: comps[c] for c in g},
                "wires": [w for w in wires if w[0] in g and w[1] in g],
            }
            shards.append(sub)
        return shards

    # ---- 单分片执行（可被子类/transport 覆盖）----
    def _execute_shard(self, worker_id: int, sub_spec: dict, seed: int) -> dict:
        if self.transport is not None:
            return self.transport.execute(worker_id, sub_spec, seed)
        # 默认：进程内线程 worker（独立后端 + 独立执行器 + 独立黑板）
        from runtime import Circuit, CircuitExecutor, SimBackend
        ex = CircuitExecutor(
            Circuit(sub_spec, SimBackend(random.Random(seed))),
            memory_enabled=False,
            auto_select_models=False,
        )
        return ex.run()

    # ---- 对外入口 ----
    def run(self, goal_or_spec, n_workers: int = None, route: bool = True,
            memory_enabled: bool = False, auto_select_models: bool = False,
            seed_base: int = None, quality_threshold=None):
        spec = self._to_spec(goal_or_spec, route=route,
                             memory_enabled=memory_enabled,
                             auto_select_models=auto_select_models,
                             quality_threshold=quality_threshold)
        shards = self.partition(spec, n_workers or self.n_workers)
        sb = seed_base if seed_base is not None else self.seed_base
        results = {}
        lock = threading.Lock()

        def _work(wid, sub, seed):
            try:
                r = self._execute_shard(wid, sub, seed)
            except Exception as e:  # worker 容错：单分片失败不影响整体聚合
                r = {"success": False, "error": str(e),
                     "final_quality": 0.0, "total_cost": 0.0,
                     "total_latency_ms": 0.0, "components": {}, "state": {}}
            with lock:
                results[wid] = r

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max(1, len(shards))) as pool:
            for wid, sub in enumerate(shards):
                pool.submit(_work, wid, sub, sb + wid + 1)
        wall = (time.perf_counter() - t0) * 1000.0
        return self._aggregate(spec, shards, results, wall)

    # ---- goal → spec ----
    def _to_spec(self, goal_or_spec, route, memory_enabled, auto_select_models,
                 quality_threshold):
        if isinstance(goal_or_spec, dict):
            spec = goal_or_spec
        else:
            os.environ.pop("AGENT_API_KEY", None)  # 强制离线规则解析
            from compiler.compile import compile_goal
            from compiler.nl_parser import GoalParser
            goal = GoalParser().parse(goal_or_spec)
            spec = compile_goal(goal, auto_bind=True, route=route,
                                memory_enabled=memory_enabled,
                                auto_select_models=auto_select_models)
        if quality_threshold is not None:
            for c, comp in spec.get("components", {}).items():
                if comp.get("type") in ("adc", "verify"):
                    try:
                        comp["threshold"] = float(quality_threshold)
                    except (TypeError, ValueError):
                        pass
        return spec

    # ---- 聚合 ----
    def _aggregate(self, spec, shards, results, wall):
        per_worker = {}
        all_ok = True
        total_cost = 0.0
        total_lat = 0.0
        q_wsum = 0.0
        comp_count = 0
        merged_components = {}
        merged_state = {}
        failed_nodes = []
        quality_gate = None
        for wid, sub in enumerate(shards):
            r = results.get(wid, {})
            per_worker[f"worker_{wid}"] = {
                "shard": sub.get("name"),
                "nodes": list(sub["components"].keys()),
                "result": r,
            }
            if not r.get("success", False):
                all_ok = False
            total_cost += float(r.get("total_cost", 0.0) or 0.0)
            total_lat += float(r.get("total_latency_ms", 0.0) or 0.0)
            n = len(sub["components"])
            q = float(r.get("final_quality", 0.0) or 0.0)
            q_wsum += q * n
            comp_count += n
            merged_components.update(r.get("components", {}) or {})
            merged_state[f"worker_{wid}"] = r.get("state", {})
            for fn in (r.get("failed_nodes", []) or []):
                failed_nodes.append(fn)
            qg = r.get("quality_gate")
            if qg and (quality_gate is None
                       or qg.get("threshold", 0) > quality_gate.get("threshold", 0)):
                quality_gate = qg
        agg_q = (q_wsum / comp_count) if comp_count else 0.0
        return {
            "success": all_ok,
            "final_quality": round(agg_q, 3),
            "worker_count": len(shards),
            "shards": [list(s["components"].keys()) for s in shards],
            "total_cost": round(total_cost, 4),
            "total_latency_ms": round(total_lat, 1),
            "cluster_wall_ms": round(wall, 1),
            "components": merged_components,
            "state": merged_state,
            "failed_nodes": failed_nodes,
            "quality_gate": quality_gate,
            "per_worker": per_worker,
        }


# ============================================================================
# 离线自检
# ============================================================================

def cluster_selftest():
    # 构造一个含两个独立子图（弱连通分量）的拓扑：链 A(src→r1) / 链 B(src→r2)
    spec = {
        "name": "cluster_demo",
        "components": {
            "a_src": {"type": "power", "label": "task"},
            "a_r":   {"type": "resistor", "label": "reason#1", "model": "small", "yield": 1.0},
            "b_src": {"type": "power", "label": "task"},
            "b_r":   {"type": "resistor", "label": "reason#2", "model": "small", "yield": 1.0},
        },
        "wires": [["a_src", "a_r"], ["b_src", "b_r"]],
    }
    coord = ClusterCoordinator(n_workers=2)
    shards = coord.partition(spec)
    assert len(shards) == 2, f"应切成 2 个独立分片，实际 {len(shards)}"

    # 无跨片连线：每条 wire 的两端必在同一分片
    cid_shard = {}
    for i, s in enumerate(shards):
        for c in s["components"]:
            cid_shard[c] = i
    for a, b in spec["wires"]:
        assert cid_shard[a] == cid_shard[b], "wire 不应跨分片"
    print("✓ 分片：弱连通分量切分，2 个独立分片，无跨片连线")

    # 执行：2 worker 并发，全部成功，结果聚合
    res = coord.run(spec, n_workers=2)
    assert res["worker_count"] == 2, "应派发 2 个 worker"
    assert res["success"] is True, "两分片都应成功"
    assert res["final_quality"] > 0, "聚合质量应 > 0"
    assert len(res["per_worker"]) == 2, "per_worker 应含 2 项"
    assert res["total_cost"] > 0 and res["cluster_wall_ms"] >= 0
    print("✓ 执行：2 worker 并发，全部成功，质量/成本/延迟聚合正确")

    # 聚合一致性：final_quality == 按分片节点数加权均值（从 per_worker 重算）
    q_wsum, n = 0.0, 0
    for w in res["per_worker"].values():
        k = len(w["nodes"])
        q_wsum += w["result"].get("final_quality", 0.0) * k
        n += k
    expected = round(q_wsum / n, 3) if n else 0.0
    assert res["final_quality"] == expected, "聚合质量应等于加权均值"
    print("✓ 聚合一致性：final_quality == 分片加权均值")

    # WCC 数 < n_workers 时：链数=1 请求 4 worker → 只起 1 个 worker（不空转）
    single = {
        "name": "single",
        "components": {
            "s": {"type": "power", "label": "task"},
            "r": {"type": "resistor", "label": "reason", "model": "small", "yield": 1.0},
        },
        "wires": [["s", "r"]],
    }
    res2 = coord.run(single, n_workers=4)
    assert res2["worker_count"] == 1, "单链只应起 1 个 worker"
    print("✓ worker 数自适应：单链请求 4 worker → 实际 1 worker（不空转）")

    # 可插拔 transport：注入假 transport，验证协调协议走它
    calls = []
    class FakeTransport:
        def execute(self, worker_id, sub_spec, seed):
            calls.append((worker_id, sorted(sub_spec["components"].keys())))
            # 返回最小合法结果，验证聚合不依赖真实后端
            return {"success": True, "final_quality": 0.9, "total_cost": 0.01,
                    "total_latency_ms": 5.0, "components": {}, "state": {}}
    coord_t = ClusterCoordinator(transport=FakeTransport(), n_workers=2)
    res3 = coord_t.run(spec, n_workers=2)
    assert len(calls) == 2, "假 transport 应被调用 2 次"
    assert res3["final_quality"] == 0.9, "走假 transport 时聚合质量应=0.9"
    print("✓ 可插拔 transport：假 transport 被调用 2 次，协调协议走它")

    # goal 字符串路径：编译后分布式执行（离线规则解析，不崩溃）
    res4 = coord.run("查中国GDP总量并预测明年趋势", n_workers=2)
    assert "per_worker" in res4 and res4["worker_count"] >= 1, \
        "goal 字符串路径应编译并分布式执行"
    print("✓ goal 字符串路径：编译→分布式执行，worker_count≥1")

    print("\ncluster 离线自检全部通过 ✓")


if __name__ == "__main__":
    cluster_selftest()
