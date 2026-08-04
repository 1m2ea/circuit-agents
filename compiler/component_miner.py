"""Phase 2 · 第三层范式进化 ③ —— 自主发现新元件类型（挖掘频繁子图 + 自动封装 composite）

问题：⑭ SelfEvolution 只挖**边级 motif**（无序类型对，如 power↔resistor），粒度太粗——
它只能建议「你的拓扑里有高频边」，不能把「research→verify→summarize」这种**反复出现
的完整子链**当成一个可复用元件。每次遇到这类三步链都得重新展开成 3 个节点，既冗余
又容易写错。

思路（挖掘 + 自动封装）：
  1. 挖掘 —— 从历史拓扑枚举 2~4 节点连通诱导子图，用排列枚举法算**同构规范化标记**
     （节点类型序列 + 有向边集），统计跨任务频次。支持度 ≥ min_support 入选。
  2. 封装 —— 达标子图包装成 composite 模板：
     · entry_nodes = 子图中无内部前驱的节点（外部入边接到这里）
     · exit_nodes  = 子图中无内部后继的节点（外部出边从这里出）
     · required_inputs  = entry 节点声明的 required_inputs 并集
     · produced_outputs = exit  节点声明的 produced_outputs 并集
     注册到 runtime 全局 ComponentLibrary。
  3. 展开 —— Circuit.__init__ 检测到 composite 类型 → 内联展开为内部原子元件，
     外部边重连到 entry/exit → runtime 其余逻辑零改动（分层/执行/反馈全不用改）。

与 ⑭ SelfEvolution 的分工：
  · SelfEvolution（⑭）：边级 motif，输出「建议」（suggest），不改 spec。
  · 本模块（③）：子图级，输出「新元件类型」并注册可执行，compile 可直接引用。

离线安全：纯本地计算，无 key、无网络。排列枚举 k≤4 → k!≤24，完全可行。
"""

from __future__ import annotations

import hashlib
import itertools
from typing import Optional


# ──────────────────────────────────────────────────────────
# 连通性 & 同构规范化
# ──────────────────────────────────────────────────────────

def _is_connected(nodes: list, adj: dict) -> bool:
    """BFS 判断 nodes 在 adj（无向邻接表）上是否连通。"""
    if len(nodes) <= 1:
        return True
    ns = set(nodes)
    visited = set()
    stack = [nodes[0]]
    while stack:
        n = stack.pop()
        if n in visited:
            continue
        visited.add(n)
        for m in adj.get(n, ()):
            if m in ns and m not in visited:
                stack.append(m)
    return len(visited) == len(ns)


def _canonical_label(nodes: list, edges: list, types: dict) -> tuple:
    """k 节点连通子图的同构规范化标记（枚举 k! 排列取字典序最小）。

    标记 = (节点类型序列, 有向边集)，两者都随排列重编号。
    两个子图同构 ⟺ 存在排列使标记相等 ⟺ 最小标记相等。
    k≤4 → k!≤24，开销可接受。
    """
    k = len(nodes)
    idx = {n: i for i, n in enumerate(nodes)}
    tps = tuple(types.get(n, "?") for n in nodes)
    dir_edges = sorted((idx[a], idx[b]) for a, b in edges
                       if a in idx and b in idx)
    best = None
    for perm in itertools.permutations(range(k)):
        node_sig = tuple(tps[perm[i]] for i in range(k))
        esig = tuple(sorted((perm[a], perm[b]) for a, b in dir_edges))
        sig = (node_sig, esig)
        if best is None or sig < best:
            best = sig
    return best


# ──────────────────────────────────────────────────────────
# 挖掘器
# ──────────────────────────────────────────────────────────

def mine(history: list, min_support: int = 3, max_size: int = 4) -> list:
    """从历史拓扑列表挖掘频繁连通子图。

    Parameters
    ----------
    history : list of {"spec": {...}} 或直接 spec dict
    min_support : 跨任务出现次数下限
    max_size : 子图节点数上限（2~max_size）

    Returns
    -------
    list of {label, support, size, nodes, edges, types, spec}
      按 (support↓, size↓) 排序。
    """
    counts: dict = {}       # canonical_label -> count
    examples: dict = {}     # canonical_label -> example record

    for item in history:
        spec = item.get("spec", item) if isinstance(item, dict) else item
        comps = spec.get("components", {})
        wires = spec.get("wires", [])
        types = {cid: c.get("type", "?") for cid, c in comps.items()}
        # 无向邻接表（连通性判断用）
        adj = {cid: set() for cid in comps}
        for a, b in wires:
            if a in adj and b in adj:
                adj[a].add(b)
                adj[b].add(a)
        node_list = list(comps.keys())
        for k in range(2, min(max_size, len(node_list)) + 1):
            for combo in itertools.combinations(node_list, k):
                sub = list(combo)
                sub_edges = [(a, b) for a, b in wires
                             if a in combo and b in combo]
                if not sub_edges:
                    continue                    # 无边不算子图
                if not _is_connected(sub, adj):
                    continue
                label = _canonical_label(sub, sub_edges, types)
                counts[label] = counts.get(label, 0) + 1
                if label not in examples:
                    examples[label] = {
                        "label": label, "support": 0, "size": k,
                        "nodes": sub, "edges": sub_edges,
                        "types": dict(types), "spec": spec,
                    }

    frequent = []
    for label, cnt in counts.items():
        if cnt >= min_support:
            rec = dict(examples[label])
            rec["support"] = cnt
            frequent.append(rec)
    frequent.sort(key=lambda x: (-x["support"], -x["size"]))
    return frequent


# ──────────────────────────────────────────────────────────
# 封装器
# ──────────────────────────────────────────────────────────

def wrap(motif: dict, register: bool = True) -> dict:
    """把频繁子图封装成 composite 模板，可选注册到 runtime 全局库。

    模板用占位内部 id（n0, n1, ...），展开时按宿主节点 id 重命名。
    """
    nodes = motif["nodes"]
    edges = motif["edges"]
    types = motif["types"]
    spec = motif["spec"]
    comps = spec["components"]

    # 内部前驱/后继 → entry/exit
    internal_preds = {n: set() for n in nodes}
    internal_succs = {n: set() for n in nodes}
    for a, b in edges:
        if a in internal_succs:
            internal_succs[a].add(b)
        if b in internal_preds:
            internal_preds[b].add(a)
    entry_nodes = [n for n in nodes if not internal_preds[n]]
    exit_nodes = [n for n in nodes if not internal_succs[n]]

    # 推导 composite 的对外接口
    required_inputs = []
    for n in entry_nodes:
        required_inputs.extend(comps[n].get("required_inputs") or [])
    produced_outputs = []
    for n in exit_nodes:
        produced_outputs.extend(comps[n].get("produced_outputs") or [])

    # 占位 id
    ni = {n: f"n{i}" for i, n in enumerate(nodes)}
    name = "composite_" + hashlib.md5(
        repr(motif["label"]).encode()).hexdigest()[:8]
    template = {
        "name": name,
        "internal_components": {ni[n]: dict(comps[n]) for n in nodes},
        "internal_wires": [[ni[a], ni[b]] for a, b in edges],
        "entry_nodes": [ni[n] for n in entry_nodes],
        "exit_nodes": [ni[n] for n in exit_nodes],
        "required_inputs": list(dict.fromkeys(required_inputs)),
        "produced_outputs": list(dict.fromkeys(produced_outputs)),
        "support": motif["support"],
        "source_types": [types[n] for n in nodes],
        "label": repr(motif["label"]),
    }
    if register:
        # 延迟 import 避免循环依赖（runtime 不 import compiler，反过来可以）
        import runtime as _rt
        _rt.register_component_template(template)
    return template


def discover_and_register(history: list, min_support: int = 3,
                          max_size: int = 4) -> list:
    """挖掘 + 封装 + 注册 全流程，返回模板列表。"""
    motifs = mine(history, min_support=min_support, max_size=max_size)
    templates = [wrap(m) for m in motifs]
    return templates


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def component_miner_selftest():
    """Phase 2 第三层③ 自主发现新元件类型 离线自检。"""
    import os
    os.environ.pop("AGENT_API_KEY", None)
    import runtime as _rt
    _rt._COMPONENT_LIBRARY.clear()          # 干净起点

    def mk_chain(name, n, tier="small"):
        """造一条 power→R0→R1→...→R{n-1} 链。"""
        comps = {"src": {"type": "power", "label": "task"}}
        wires = []
        prev = "src"
        for i in range(n):
            cid = f"R{i}"
            comps[cid] = {"type": "resistor", "label": f"step{i}",
                          "model": tier, "yield": 1.0,
                          "required_inputs": ([f"o{i-1}"] if i > 0 else []),
                          "produced_outputs": [f"o{i}"]}
            wires.append([prev, cid])
            prev = cid
        return {"name": name, "spec": {"name": name,
                "components": comps, "wires": wires}}

    # 1) 挖掘：3 条含 research→verify→summarize 链的历史 → 挖出 3-node motif
    SECRET = "某公司财报明细"
    def mk_rvs(name, tier="small"):
        """research→verify→summarize 三步链。"""
        return {"name": name, "spec": {
            "name": name,
            "components": {
                "src": {"type": "power", "label": "task"},
                "R": {"type": "resistor", "label": "research", "model": tier,
                      "yield": 1.0, "produced_outputs": ["raw"]},
                "V": {"type": "verify", "label": "verify", "threshold": 0.5},
                "S": {"type": "resistor", "label": "summarize", "model": tier,
                      "yield": 1.0, "required_inputs": ["raw"],
                      "produced_outputs": ["summary"]},
            },
            "wires": [["src", "R"], ["R", "V"], ["V", "S"]],
        }}
    hist = [mk_rvs("t1"), mk_rvs("t2"), mk_rvs("t3", tier="large")]
    motifs = mine(hist, min_support=3, max_size=4)
    assert motifs, "应挖出至少一个频繁子图"
    top = motifs[0]
    assert top["support"] >= 3, f"顶级 motif 支持度应≥3，实际 {top['support']}"
    assert top["size"] >= 2, "顶级 motif 至少 2 节点"
    print(f"✓ 新元件③ 挖掘: {len(motifs)} 个频繁子图 · 顶级 "
          f"size={top['size']} support={top['support']}")

    # 2) 规范化正确：不同 id 但同构的子图归为同一 motif
    m2 = mine([mk_rvs("a"), mk_rvs("b"), mk_rvs("c")], min_support=3)
    # research→verify→summarize 三节点链应被归为同一个 motif（support=3）
    three_node = [m for m in m2 if m["size"] == 3]
    assert len(three_node) >= 1, "应挖到 3 节点子图"
    # 同一结构的三个历史应归为同一 label（support=3，而非三个 support=1）
    assert any(m["support"] >= 3 for m in three_node), \
        f"同构子图应合并，实际 support 分布 {[m['support'] for m in three_node]}"
    print(f"✓ 新元件③ 同构合并: 3 条同构链归为 1 个 motif "
          f"(label support={max(m['support'] for m in three_node)})")

    # 3) min_support 过滤：设为 5 → 3 次出现的 motif 不入选
    m_strict = mine(hist, min_support=5)
    assert not m_strict, "min_support=5 时 3 次的 motif 不应入选"
    print("✓ 新元件③ 支持度过滤: min_support=5 → 3 次的 motif 被过滤")

    # 4) 封装 + 注册：wrap 推导 entry/exit + required/produced
    _rt._COMPONENT_LIBRARY.clear()
    tmpl = wrap(top, register=True)
    assert tmpl["entry_nodes"] and tmpl["exit_nodes"], "应有 entry/exit"
    assert "raw" in tmpl["produced_outputs"] or tmpl["produced_outputs"], \
        f"produced_outputs 应从 exit 推导，实际 {tmpl['produced_outputs']}"
    assert tmpl["name"] in _rt._COMPONENT_LIBRARY, "应注册到全局库"
    print(f"✓ 新元件③ 封装: {tmpl['name']} · entry={tmpl['entry_nodes']} "
          f"exit={tmpl['exit_nodes']} · required={tmpl['required_inputs']} "
          f"produced={tmpl['produced_outputs']} · 已注册")

    # 5) 内联展开 + 真执行：composite 节点 → Circuit 展开为原子 → 跑通
    #    手写等价原子 spec 做对照
    from runtime import Circuit, SimBackend
    import random
    be = SimBackend(random.Random(42))
    atomic_spec = mk_rvs("atomic")["spec"]
    circ_atomic = Circuit(atomic_spec, be)
    from runtime import CircuitExecutor
    res_atomic = CircuitExecutor(circ_atomic).run()

    # 用 composite 类型写新 spec：一个节点代替 R→V→S
    composite_spec = {
        "name": "composite_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "C": {"type": "composite", "template": tmpl["name"],
                  "label": "rvs_macro"},
        },
        "wires": [["src", "C"]],
    }
    # 展开后应等价于原子版
    expanded = _rt._expand_composites(composite_spec)
    assert any(c.get("type") == "resistor" for c in expanded["components"].values()), \
        "展开后应含原子 resistor"
    assert len(expanded["components"]) > len(composite_spec["components"]), \
        "展开后节点数应增加"
    # 真跑
    be2 = SimBackend(random.Random(42))     # 同种子 → 可复现
    circ_comp = Circuit(composite_spec, be2)
    res_comp = CircuitExecutor(circ_comp).run()
    assert res_comp["success"] == res_atomic["success"], \
        f"composite 展开后执行结果应与原子版一致 " \
        f"(composite={res_comp['success']} atomic={res_atomic['success']})"
    assert abs(res_comp["final_quality"] - res_atomic["final_quality"]) < 1e-9, \
        f"质量应一致 (composite={res_comp['final_quality']} " \
        f"atomic={res_atomic['final_quality']})"
    print(f"✓ 新元件③ 内联展开+真执行: composite→{len(expanded['components'])}节点 "
          f"· success={res_comp['success']} · quality={round(res_comp['final_quality'],4)} "
          f"· 与原子版一致 ✓")

    # 6) 零回归：library 为空时 spec 原样通过
    _rt._COMPONENT_LIBRARY.clear()
    raw_spec = {"name": "x", "components": {
        "src": {"type": "power", "label": "t"},
        "A": {"type": "resistor", "label": "a", "model": "small"}},
        "wires": [["src", "A"]]}
    assert _rt._expand_composites(raw_spec) is raw_spec, \
        "library 为空时应原样返回同一对象（零成本零回归）"
    print("✓ 新元件③ 零回归: library 为空 → spec 原样返回（零成本）")

    # 7) discover_and_register 一键流程
    _rt._COMPONENT_LIBRARY.clear()
    tlist = discover_and_register(hist, min_support=3)
    assert tlist, "一键流程应产出模板"
    assert all(t["name"] in _rt._COMPONENT_LIBRARY for t in tlist), "全部应注册"
    print(f"✓ 新元件③ 一键流程: discover_and_register 产出 {len(tlist)} 个模板并注册")

    _rt._COMPONENT_LIBRARY.clear()          # 清理，不污染后续测试
    print("\nPhase 2 第三层③ 自主发现新元件类型 离线自检全部通过 ✓")


if __name__ == "__main__":
    component_miner_selftest()
