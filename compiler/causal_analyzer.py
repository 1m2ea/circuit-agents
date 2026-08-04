"""执行历史因果分析（Phase 2+ 第四层③）：反事实推理定位瓶颈节点。

核心思想——**反事实推理（counterfactual reasoning）**：
  "如果节点 X 的质量是 1.0（完美），最终质量会是多少？"

对每个节点做反事实推演：
1. 从真实执行结果中提取每个节点的实际质量
2. 假设目标节点质量 = 1.0，沿 DAG 下游传播（用 SimBackend 质量传播公式做符号推演）
3. 因果贡献 = 反事实最终质量 - 真实最终质量
4. 贡献最大的节点 = 瓶颈节点（提升它能带来最大全局收益）

传播公式（与 SimBackend.run() 一致）：
- resistor:  q_out = min(q_in, cap)  （recovery 时 q_out = q_in + η·(cap−q_in)）
- capacitor: q_out = max(q_inputs)
- diode:     q_out = q_in
- adc:       q_out = q_in
- opamp:     q_out = 1.0
- power:     q_out = 1.0
- source:    q_out = given
- 其他:      q_out = q_in (透传)

不需要重新执行电路——纯分析推演，O(V+E) per counterfactual。
"""

import json
import os
from collections import deque


def _topo_sort(components, wires):
    """Kahn 拓扑序。返回 (order, preds, succs)。"""
    indeg = {c: 0 for c in components}
    succ = {c: [] for c in components}
    preds = {c: [] for c in components}
    for a, b in wires:
        if a in components and b in components:
            succ[a].append(b)
            preds[b].append(a)
            indeg[b] += 1
    ready = deque(c for c in components if indeg[c] == 0)
    order = []
    while ready:
        n = ready.popleft()
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    # 兜底：环中剩余节点
    rest = [c for c in components if c not in order]
    order.extend(rest)
    return order, preds, succ


def _propagate_quality(components, preds, succ, order, quality_map, override_node=None,
                      override_value=1.0):
    """沿 DAG 传播质量。如果 override_node 不为 None，则该节点质量被强制设为 override_value，
    并**仅重算其下游节点**（其余节点信任 quality_map 中的真实值）。

    返回完整质量映射 {cid: quality}。
    """
    q = dict(quality_map)  # 从真实质量起步
    if override_node is None:
        return q  # 无 override → 信任真实执行结果

    q[override_node] = override_value

    # 找到 override 的所有下游节点（BFS）
    downstream = set()
    queue = deque(succ.get(override_node, []))
    while queue:
        n = queue.popleft()
        if n not in downstream:
            downstream.add(n)
            queue.extend(succ.get(n, []))

    # 只重算下游节点（按拓扑序）
    for cid in order:
        if cid not in downstream:
            continue
        comp = components[cid]
        t = comp.get("type", "resistor")
        upstream = [q[p] for p in preds.get(cid, []) if p in q]

        if t in ("power",):
            q[cid] = 1.0
        elif t == "source":
            q[cid] = comp.get("quality", 0.9)
        elif t == "opamp":
            q[cid] = 1.0
        elif t == "resistor":
            if not upstream:
                q[cid] = 0.0
                continue
            inp = max(upstream)
            cap = comp.get("accuracy", 0.7)
            eta = comp.get("recovery", 0.0)
            if eta and cap > inp:
                q[cid] = min(1.0, inp + eta * (cap - inp))
            else:
                q[cid] = min(inp, cap)
        elif t == "capacitor":
            q[cid] = max(upstream) if upstream else 0.0
        elif t in ("diode", "adc", "watchdog", "format_adapter", "logic_gate"):
            q[cid] = max(upstream) if upstream else 0.0
        elif t == "bridge_rectifier":
            q[cid] = min(upstream) if upstream else 0.0
        else:
            q[cid] = max(upstream) if upstream else 0.0

    return q


def _find_terminal(components, succ):
    """找到终端节点（无后继的非 adc 节点，或 adc 节点）。"""
    # 优先返回 adc 节点
    for cid, c in components.items():
        if c.get("type") == "adc":
            return cid
    # 否则返回无后继的节点
    for cid in components:
        if not succ.get(cid):
            return cid
    return list(components.keys())[-1] if components else None


class CausalAnalyzer:
    """反事实因果分析器：定位质量瓶颈节点。

    用法：
        analyzer = CausalAnalyzer()
        report = analyzer.analyze(spec, execution_result)
        # report["bottlenecks"] = [{"node": "r1", "impact": 0.15, ...}, ...]
    """

    def __init__(self):
        pass

    def analyze(self, spec, execution_result):
        """分析执行结果，返回因果瓶颈报告。

        Args:
            spec: 拓扑 spec dict（含 components + wires）
            execution_result: CircuitExecutor.run() 的返回 dict，或
                              {cid: {"quality": float, "ok": bool, ...}} 映射

        Returns:
            {
                "actual_final_quality": float,
                "bottlenecks": [
                    {"node": cid, "label": str, "type": str,
                     "actual_quality": float,
                     "counterfactual_quality": float,
                     "impact": float,
                     "rank": int},
                    ...  # 按 impact 降序
                ],
                "bottleneck_node": str,  # impact 最大的节点
                "max_impact": float,
                "analysis": str,  # 人类可读摘要
            }
        """
        comps = spec.get("components", {})
        wires = spec.get("wires", [])
        order, preds, succ = _topo_sort(comps, wires)

        # 从 execution_result 提取每个节点的实际质量
        actual_q = {}
        if isinstance(execution_result, dict) and "components" in execution_result:
            # CircuitExecutor.run() 格式: {"components": {cid: {"quality": ..., ...}}}
            for cid, info in execution_result.get("components", {}).items():
                if isinstance(info, dict):
                    actual_q[cid] = info.get("quality", 0.0)
        elif isinstance(execution_result, dict):
            # 直接的 {cid: {"quality": ...}} 映射
            for cid, info in execution_result.items():
                if isinstance(info, dict) and "quality" in info:
                    actual_q[cid] = info["quality"]
                elif isinstance(info, (int, float)):
                    actual_q[cid] = info

        # 补全：对 execution_result 中缺失的节点，用传播公式推算
        full_q = _propagate_quality(comps, preds, succ, order, actual_q)

        # 终端节点
        terminal = _find_terminal(comps, succ)
        if terminal is None:
            return {"error": "no nodes in spec", "bottlenecks": []}

        actual_final = full_q.get(terminal, 0.0)

        # 对每个节点做反事实推演
        bottlenecks = []
        for cid in comps:
            comp = comps[cid]
            t = comp.get("type", "resistor")

            # power/source/opamp 质量固定，无需反事实
            if t in ("power", "source", "opamp", "watchdog"):
                continue

            actual_node_q = full_q.get(cid, 0.0)

            # 反事实：该节点质量 = 1.0
            cf_q = _propagate_quality(comps, preds, succ, order, full_q,
                                      override_node=cid, override_value=1.0)
            cf_final = cf_q.get(terminal, 0.0)
            impact = cf_final - actual_final

            bottlenecks.append({
                "node": cid,
                "label": comp.get("label", cid),
                "type": t,
                "actual_quality": round(actual_node_q, 6),
                "counterfactual_final_quality": round(cf_final, 6),
                "impact": round(impact, 6),
            })

        # 按 impact 降序排序
        bottlenecks.sort(key=lambda b: b["impact"], reverse=True)
        for i, b in enumerate(bottlenecks):
            b["rank"] = i + 1

        # 分析摘要
        max_impact = bottlenecks[0]["impact"] if bottlenecks else 0.0
        bn = bottlenecks[0] if bottlenecks else None
        if bn and max_impact > 0.001:
            analysis = (
                f"瓶颈节点: {bn['label']}({bn['node']}) · "
                f"类型={bn['type']} · "
                f"实际质量={bn['actual_quality']:.3f} · "
                f"反事实最终质量={bn['counterfactual_final_quality']:.3f} · "
                f"因果贡献=+{bn['impact']:.3f} · "
                f"若将该节点质量提升至 1.0，最终质量可从 {actual_final:.3f} 提升至 {bn['counterfactual_final_quality']:.3f}"
            )
        elif bottlenecks:
            analysis = f"无明显瓶颈（最大因果贡献={max_impact:.4f}）。当前最终质量={actual_final:.3f}。"
        else:
            analysis = "无可分析节点。"

        return {
            "actual_final_quality": round(actual_final, 6),
            "bottlenecks": bottlenecks,
            "bottleneck_node": bn["node"] if bn else None,
            "bottleneck_label": bn["label"] if bn else None,
            "max_impact": round(max_impact, 6),
            "terminal_node": terminal,
            "analysis": analysis,
        }

    def analyze_batch(self, spec, execution_results):
        """对多次执行结果做聚合因果分析。

        Returns: 每个节点的平均因果贡献 + 跨执行稳定性。
        """
        all_reports = [self.analyze(spec, r) for r in execution_results]
        if not all_reports:
            return {"error": "no results"}

        # 聚合每个节点的 impact
        node_impacts = {}
        for report in all_reports:
            for b in report.get("bottlenecks", []):
                node_impacts.setdefault(b["node"], []).append(b["impact"])

        avg_impacts = []
        for cid, impacts in node_impacts.items():
            comp = spec.get("components", {}).get(cid, {})
            avg_impacts.append({
                "node": cid,
                "label": comp.get("label", cid),
                "type": comp.get("type", "resistor"),
                "avg_impact": round(sum(impacts) / len(impacts), 6),
                "min_impact": round(min(impacts), 6),
                "max_impact": round(max(impacts), 6),
                "std_impact": round(
                    (sum((x - sum(impacts) / len(impacts)) ** 2 for x in impacts)
                     / len(impacts)) ** 0.5, 6),
                "n_executions": len(impacts),
            })

        avg_impacts.sort(key=lambda b: b["avg_impact"], reverse=True)
        for i, b in enumerate(avg_impacts):
            b["rank"] = i + 1

        return {
            "n_executions": len(all_reports),
            "avg_final_quality": round(
                sum(r["actual_final_quality"] for r in all_reports) / len(all_reports), 6),
            "aggregated_bottlenecks": avg_impacts,
            "bottleneck_node": avg_impacts[0]["node"] if avg_impacts else None,
            "analysis": (
                f"跨 {len(all_reports)} 次执行聚合："
                f"瓶颈={avg_impacts[0]['label']}({avg_impacts[0]['node']}) · "
                f"平均因果贡献=+{avg_impacts[0]['avg_impact']:.3f}"
                if avg_impacts else "无可分析节点"
            ),
        }


# ============================================================================
# 离线自检
# ============================================================================

def causal_analyzer_selftest():
    os.environ.pop("AGENT_API_KEY", None)

    analyzer = CausalAnalyzer()

    # 测试 1：简单串联——中间节点是瓶颈
    spec1 = {
        "name": "series_causal",
        "components": {
            "src":  {"type": "power", "label": "task"},
            "r1":   {"type": "resistor", "label": "retrieve", "model": "small", "accuracy": 0.70},
            "r2":   {"type": "resistor", "label": "reason", "model": "large", "accuracy": 0.92},
            "r3":   {"type": "resistor", "label": "summarize", "model": "small", "accuracy": 0.50},
        },
        "wires": [["src", "r1"], ["r1", "r2"], ["r2", "r3"]],
    }

    # 模拟执行结果：r1 质量低 = 瓶颈
    exec_result1 = {
        "components": {
            "src": {"quality": 1.0, "ok": True},
            "r1":  {"quality": 0.50, "ok": True},   # 低质量
            "r2":  {"quality": 0.50, "ok": True},   # 被 r1 限制
            "r3":  {"quality": 0.50, "ok": True},   # 被 r2 限制
        }
    }
    report1 = analyzer.analyze(spec1, exec_result1)
    assert report1["actual_final_quality"] == 0.5, f"实际最终质量应为 0.5: {report1['actual_final_quality']}"
    assert report1["bottleneck_node"] == "r3", f"瓶颈应为 r3（cap=0.50 限制全局）: {report1['bottleneck_node']}"
    # r1 的反事实：r1=1.0 → r2=min(1.0, 0.92)=0.92 → r3=min(0.92, 0.50)=0.50
    # r3 的 cap=0.50 限制了最终质量，所以 r1 的 impact 应该是 0.0（因为 r3 cap 更低）
    # 实际上 r1 impact 应该是 0，因为 r3 的 cap=0.50 是真正的瓶颈
    # 让我重新计算：
    # r1=1.0 → r2=min(1.0, 0.92)=0.92 → r3=min(0.92, 0.50)=0.50
    # 所以 r1 的 impact = 0.50 - 0.50 = 0.0
    # r3 的反事实：r3=1.0 → final=1.0, impact = 1.0 - 0.5 = 0.5
    # 所以 r3 才是真正的瓶颈
    r3_bn = [b for b in report1["bottlenecks"] if b["node"] == "r3"][0]
    assert r3_bn["impact"] == 0.5, f"r3 的 impact 应为 0.5: {r3_bn['impact']}"
    assert r3_bn["rank"] == 1, f"r3 应排名第 1: {r3_bn['rank']}"
    print(f"✓ 串联瓶颈定位：r3(summarize) cap=0.50 是真正瓶颈 · impact=+{r3_bn['impact']:.3f}")

    # 测试 2：并联拓扑——多支路贡献分析
    spec2 = {
        "name": "parallel_causal",
        "components": {
            "src":  {"type": "power", "label": "task"},
            "r1":   {"type": "resistor", "label": "branch_A", "model": "small", "accuracy": 0.70},
            "r2":   {"type": "resistor", "label": "branch_B", "model": "large", "accuracy": 0.92},
            "cap":  {"type": "capacitor", "mode": "any"},
            "adc":  {"type": "adc", "threshold": 0.6},
        },
        "wires": [["src", "r1"], ["src", "r2"], ["r1", "cap"], ["r2", "cap"], ["cap", "adc"]],
    }

    exec_result2 = {
        "components": {
            "src": {"quality": 1.0, "ok": True},
            "r1":  {"quality": 0.60, "ok": True},
            "r2":  {"quality": 0.88, "ok": True},
            "cap": {"quality": 0.88, "ok": True},  # max(0.60, 0.88)
            "adc": {"quality": 0.88, "ok": True},
        }
    }
    report2 = analyzer.analyze(spec2, exec_result2)
    assert report2["actual_final_quality"] == 0.88, f"并联最终质量应为 0.88: {report2['actual_final_quality']}"
    # r1=1.0 → cap=max(1.0, 0.88)=1.0 → adc=1.0, impact=0.12
    # r2=1.0 → cap=max(0.60, 1.0)=1.0 → adc=1.0, impact=0.12
    # 两个支路 impact 相同
    r1_bn = [b for b in report2["bottlenecks"] if b["node"] == "r1"][0]
    r2_bn = [b for b in report2["bottlenecks"] if b["node"] == "r2"][0]
    assert abs(r1_bn["impact"] - 0.12) < 1e-6, f"r1 impact 应为 0.12: {r1_bn['impact']}"
    assert abs(r2_bn["impact"] - 0.12) < 1e-6, f"r2 impact 应为 0.12: {r2_bn['impact']}"
    print(f"✓ 并联因果分析：r1 impact=+{r1_bn['impact']:.3f} · r2 impact=+{r2_bn['impact']:.3f}（对称贡献）")

    # 测试 3：弱节点拖累全局——recovery 系数
    spec3 = {
        "name": "recovery_causal",
        "components": {
            "src": {"type": "power", "label": "task"},
            "r1":  {"type": "resistor", "label": "weak_link", "model": "small", "accuracy": 0.40, "recovery": 0.0},
            "r2":  {"type": "resistor", "label": "strong", "model": "large", "accuracy": 0.95, "recovery": 0.5},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "r1"], ["r1", "r2"], ["r2", "adc"]],
    }

    exec_result3 = {
        "components": {
            "src": {"quality": 1.0, "ok": True},
            "r1":  {"quality": 0.30, "ok": True},   # 很弱
            "r2":  {"quality": 0.625, "ok": True},  # 0.30 + 0.5*(0.95-0.30) = 0.625
            "adc": {"quality": 0.625, "ok": True},
        }
    }
    report3 = analyzer.analyze(spec3, exec_result3)
    # r1=1.0 → r2: cap=0.95, inp=1.0, recovery=0.5 → 1.0 (capped) → adc=1.0
    # r1 impact = 1.0 - 0.625 = 0.375
    # r2=1.0 → adc=1.0, impact = 1.0 - 0.625 = 0.375
    # 但 r2 的实际质量是 0.625，r1 的实际质量是 0.30
    r1_bn3 = [b for b in report3["bottlenecks"] if b["node"] == "r1"][0]
    r2_bn3 = [b for b in report3["bottlenecks"] if b["node"] == "r2"][0]
    assert r1_bn3["impact"] > 0, "r1 应有正因果贡献"
    assert r2_bn3["impact"] > 0, "r2 应有正因果贡献"
    print(f"✓ recovery 因果：r1(weak) impact=+{r1_bn3['impact']:.3f} · r2(strong) impact=+{r2_bn3['impact']:.3f}")

    # 测试 4：与真实 CircuitExecutor 集成
    import random as _rnd
    from runtime import Circuit, SimBackend, CircuitExecutor

    spec4 = {
        "name": "real_exec_causal",
        "components": {
            "src":  {"type": "power", "label": "task"},
            "ret":  {"type": "resistor", "label": "retrieve", "model": "small", "accuracy": 0.70, "recovery": 0.3},
            "rsn":  {"type": "resistor", "label": "reason", "model": "large", "accuracy": 0.92, "recovery": 0.1},
            "sum":  {"type": "resistor", "label": "summarize", "model": "small", "accuracy": 0.65, "recovery": 0.0},
            "adc":  {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "ret"], ["ret", "rsn"], ["rsn", "sum"], ["sum", "adc"]],
    }
    be = SimBackend(_rnd.Random(42))
    circ = Circuit(spec4, be)
    real_result = CircuitExecutor(circ).run()
    report4 = analyzer.analyze(spec4, real_result)
    assert report4["bottlenecks"], "应有瓶颈分析结果"
    assert report4["bottleneck_node"] is not None, "应定位瓶颈节点"
    assert report4["max_impact"] >= 0, "因果贡献应非负"
    print(f"✓ CircuitExecutor 集成：最终质量={report4['actual_final_quality']:.3f} · "
          f"瓶颈={report4['bottleneck_label']} · max_impact=+{report4['max_impact']:.3f}")

    # 测试 5：批量聚合分析
    results_batch = []
    for s in range(5):
        be = SimBackend(_rnd.Random(s))
        circ = Circuit(spec4, be)
        results_batch.append(CircuitExecutor(circ).run())
    batch_report = analyzer.analyze_batch(spec4, results_batch)
    assert batch_report["n_executions"] == 5, "应聚合 5 次执行"
    assert batch_report["aggregated_bottlenecks"], "应有聚合瓶颈列表"
    assert batch_report["bottleneck_node"] is not None, "应定位聚合瓶颈"
    # 验证稳定性指标
    bn0 = batch_report["aggregated_bottlenecks"][0]
    assert "std_impact" in bn0 and "min_impact" in bn0 and "max_impact" in bn0
    print(f"✓ 批量聚合：5 次执行 · 瓶颈={bn0['label']} · "
          f"avg=+{bn0['avg_impact']:.3f} · std={bn0['std_impact']:.3f}")

    # 测试 6：无瓶颈场景（所有节点质量已 = 1.0）
    spec6 = {
        "name": "no_bottleneck",
        "components": {
            "src": {"type": "power", "label": "task"},
            "r1":  {"type": "resistor", "label": "perfect", "model": "tool", "accuracy": 1.0},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
    }
    exec_result6 = {
        "components": {
            "src": {"quality": 1.0, "ok": True},
            "r1":  {"quality": 1.0, "ok": True},
            "adc": {"quality": 1.0, "ok": True},
        }
    }
    report6 = analyzer.analyze(spec6, exec_result6)
    assert report6["max_impact"] == 0.0, f"完美质量应无瓶颈: {report6['max_impact']}"
    assert "无明显瓶颈" in report6["analysis"], "分析应说明无明显瓶颈"
    print(f"✓ 无瓶颈场景：所有节点质量=1.0 · max_impact=0 · 正确识别无瓶颈")

    print("\ncausal_analyzer 离线自检全部通过 ✓")


if __name__ == "__main__":
    causal_analyzer_selftest()
