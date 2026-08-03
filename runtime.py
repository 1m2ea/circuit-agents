"""
circuit-agents · runtime
=======================
Execute an agent-workflow topology described in the Circuit DSL (see SPEC.md).

Design mirrors the circuit metaphor:
  - components = circuit elements (power / opamp / resistor / capacitor /
    diode / adc / watchdog / bridge_rectifier / logic_gate / source)
  - wires      = ideal conductors (deterministic data flow)
  - feedback   = declared retry loop, bounded by a watchdog

Default backend is SimBackend: every agent is a *stochastic resistor* with
cost / latency / accuracy / yield. Swap in a real LLM backend by implementing
the Backend interface and passing it to Circuit(backend=...).
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any


# ---- 看门狗（健康自检）常量 ----
# 平庸带：质量落在此区间即"将过不过 / 弱但不死"，连续 N 次 → 判定劣化。
WATCHDOG_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watchdog_state.json")
DEGRADED_BAND = (0.55, 0.85)
DEGRADED_CONSEC = 3
_WD_SAMPLES_CAP = 20


@dataclass
class Signal:
    """The 'current' flowing through a wire: a structured, typed message."""
    value: Any = None
    quality: float = 0.0       # analog "voltage level" in [0,1]
    ok: bool = True            # usable output? (False == open circuit)
    cost: float = 0.0          # $ consumed
    latency_ms: float = 0.0    # simulated time
    meta: dict = field(default_factory=dict)


def aggregate(inputs):
    """Merge multiple upstream signals (used at a merge / capacitor node)."""
    inputs = [s for s in inputs if s is not None]
    if not inputs:
        return Signal(value=None, quality=0.0, ok=False)
    ok = all(s.ok for s in inputs)
    quality = max(s.quality for s in inputs)
    return Signal(value=inputs, quality=quality, ok=ok)


class Backend:
    def run(self, comp: dict, inputs: list) -> Signal:
        raise NotImplementedError


class SimBackend(Backend):
    """Stochastic simulation. Deterministic given the rng instance."""

    _TIERS = {
        "small": dict(cost=0.001, latency=200,  accuracy=0.70, yld=0.95),
        "large": dict(cost=0.020, latency=1500, accuracy=0.92, yld=0.90),
        "tool":  dict(cost=0.005, latency=800,  accuracy=0.99, yld=0.98),
    }

    def __init__(self, rng):
        self.rng = rng

    def run(self, comp, inputs):
        t = comp.get("type")
        agg = aggregate(inputs)

        if t == "power":
            return Signal(value=comp.get("task", comp.get("label", "")),
                          quality=1.0, ok=True)
        if t == "source":
            return Signal(value=comp.get("label"),
                          quality=comp.get("quality", 0.9), ok=True,
                          cost=comp.get("cost", 0.0),
                          latency_ms=comp.get("latency", 0.0))
        if t == "opamp":                        # scheduler
            clarify = comp.get("spec_clarify", False)
            return Signal(value=agg.value, quality=1.0, ok=True,
                          cost=0.002 if clarify else 0.0,
                          latency_ms=50 if clarify else 5,
                          meta={"clarify": clarify})
        if t == "resistor":                     # atomic agent (TRANSFORMER)
            tier = comp.get("model", "small")
            d = self._TIERS.get(tier, self._TIERS["small"])
            cost = comp.get("cost", d["cost"])
            lat = comp.get("latency", d["latency"])
            cap = comp.get("accuracy", d["accuracy"])   # intrinsic capability ceiling
            yld = comp.get("yield", d["yld"])
            # input quality feeds in from upstream (transformer, NOT a generator)
            inp = max((s.quality for s in inputs if s.ok), default=0.0)
            # 无任何可用上游信号 → 直接开路，不再叠加噪声
            # （避免把 0 顶成微弱正信号向后传播；开路必须保持开路）
            if inp <= 0.0:
                return Signal(value=None, quality=0.0, ok=False,   # open circuit
                              cost=cost, latency_ms=lat,
                              meta={"open": "no_input", "input": 0.0})
            if self.rng.random() < yld:
                # output cannot exceed min(input, capability): a weak upstream
                # caps even a strong agent; a strong agent can't beat its ceiling.
                base = min(inp, cap)
                # 补强#1 recovery 系数 η∈[0,1]：强 agent 可部分挽救"弱但存活"的输入。
                # 仅当 cap>inp（输入弱于本 agent 上限）时生效，把输出抬升到
                # inp + η·(cap−inp)（仍不超过 cap）。开路(inp<=0) 已在上方处理，
                # recovery 绝不 revive 死输入 —— 延续"开路必须保持开路"内核。η=0 即旧行为。
                eta = comp.get("recovery", 0.0)
                if eta and cap > inp:
                    q = inp + eta * (cap - inp)
                else:
                    q = base
                q = max(0.0, min(1.0, q)) + self.rng.uniform(-0.03, 0.03)
                q = max(0.0, min(1.0, q))
                return Signal(value=f"result({tier})", quality=q, ok=True,
                              cost=cost, latency_ms=lat,
                              meta={"input": round(inp, 3), "cap": cap, "recovery": round(eta, 2)})
            return Signal(value=None, quality=0.0, ok=False,   # open circuit
                          cost=cost, latency_ms=lat,
                          meta={"open": "yield_fail", "input": round(inp, 3)})
        if t == "capacitor":                    # context merge / buffer
            # mode="any"（标准单元#5 冗余汇合）：任一输入 ok 即 ok（一支开路不影响其余）；
            # 默认 mode="all"（向后兼容）：必须全部输入 ok。quality 始终取 max。
            if comp.get("mode") == "any":
                ok = any(s.ok for s in inputs)
            else:
                ok = agg.ok
            return Signal(value=agg.value, quality=agg.quality, ok=ok,
                          cost=comp.get("cost", 0.001),
                          latency_ms=comp.get("latency", 30))
        if t == "diode":                        # one-way validation
            return Signal(value=agg.value, quality=agg.quality, ok=agg.ok,
                          cost=comp.get("cost", 0.001),
                          latency_ms=comp.get("latency", 40),
                          meta={"rectified": agg.ok})
        if t == "adc":                          # evaluator -> digital level
            thr = comp.get("threshold", 0.8)
            score = agg.quality
            level = "high" if score >= thr else "low"
            return Signal(value=score, quality=score, ok=(level == "high"),
                          cost=comp.get("cost", 0.01),
                          latency_ms=comp.get("latency", 200),
                          meta={"level": level, "threshold": thr})
        if t == "format_adapter":               # 第二层②：格式适配节点（ADC/DAC/transcode）
            # 近零成本确定性透传：raw↔struct 的格式转换由 agent 语义隐含，这里只做信号
            # 透传 + 标注，不改变 quality/ok（与 SimBackend 其余结构件一致，无 LLM 调用）。
            return Signal(value=agg.value, quality=agg.quality, ok=agg.ok,
                          cost=comp.get("cost", 0.0),
                          latency_ms=comp.get("latency", 5),
                          meta={"adapter": comp.get("kind", "transcode"),
                                "from_fmt": comp.get("from_fmt"),
                                "to_fmt": comp.get("to_fmt")})
        if t == "watchdog":                     # loop bound marker
            return Signal(value=agg.value, quality=agg.quality, ok=agg.ok,
                          cost=0.0, latency_ms=1.0)
        if t == "bridge_rectifier":             # multimodal unify
            q = min((s.quality for s in inputs), default=0.0)
            ok = all(s.ok for s in inputs)
            return Signal(value="unified", quality=q, ok=ok,
                          cost=comp.get("cost", 0.003),
                          latency_ms=comp.get("latency", 100))
        if t == "logic_gate":
            return Signal(value=agg.value, quality=agg.quality, ok=agg.ok,
                          cost=comp.get("cost", 0.0),
                          latency_ms=comp.get("latency", 1.0))
        return Signal(value=agg.value, quality=agg.quality, ok=agg.ok)


class Watchdog:
    """跨轮 / 跨任务的健康自检计数器。

    记录每个节点（按 ``circuit_id::node`` 索引）的质量采样，识别"持续平庸"的劣化节点：
    当某节点的质量连续 ``DEGRADED_CONSEC`` 次落在平庸带 ``DEGRADED_BAND`` 内，
    标 ``degraded=True``。状态持久化到 ``.watchdog_state.json``，使劣化标记能跨
    ``execute()`` 调用 / 跨任务累积——即"跨轮劣化标记"。

    用法::

        wd = Watchdog()
        res = circuit.execute(self_heal=True, watchdog=wd)
        # res["watchdog"] 含各节点 {samples, band_consec, degraded}
        # degraded 节点在后续 execute（self_heal 开）时开局即被优先提档。
    """

    def __init__(self, state_path=None):
        self.path = state_path or WATCHDOG_STATE_PATH
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @staticmethod
    def _key(circuit_id, node):
        return f"{circuit_id}::{node}"

    def record(self, circuit_id, node, quality):
        """记录一次质量采样，返回该节点当前累计的『连续落带次数』。"""
        k = self._key(circuit_id, node)
        rec = self.data.setdefault(
            k, {"samples": [], "band_consec": 0, "degraded": False})
        lo, hi = DEGRADED_BAND
        rec["samples"].append(round(quality, 3))
        if len(rec["samples"]) > _WD_SAMPLES_CAP:
            rec["samples"] = rec["samples"][-_WD_SAMPLES_CAP:]
        if lo <= quality < hi:
            rec["band_consec"] += 1
        else:
            rec["band_consec"] = 0
        # degraded 表示"当前正处于连续平庸带中"（动态，非粘滞）：
        # 节点一旦被升级并产出高质量，下一采样会清零 band_consec → degraded 自动解除。
        rec["degraded"] = (rec["band_consec"] >= DEGRADED_CONSEC)
        return rec["band_consec"]

    def is_degraded(self, circuit_id, node):
        k = self._key(circuit_id, node)
        return self.data.get(k, {}).get("degraded", False)

    def snapshot(self, circuit_id):
        """返回某电路下各节点的看门狗摘要（供 execute 返回 / 打印）。"""
        out = {}
        prefix = circuit_id + "::"
        for k, rec in self.data.items():
            if k.startswith(prefix):
                out[k[len(prefix):]] = {
                    "samples": rec["samples"],
                    "band_consec": rec["band_consec"],
                    "degraded": rec["degraded"],
                }
        return out


class Circuit:
    def __init__(self, spec, backend):
        self.spec = spec
        self.backend = backend
        self.components = spec["components"]
        self.feedback = spec.get("feedback")
        # forward wires only (exclude the declared feedback edge)
        self.forward = [w for w in spec["wires"]
                        if not (self.feedback
                                and w == [self.feedback["from"], self.feedback["to"]])]
        self.succ = {c: [] for c in self.components}
        self.pred = {c: [] for c in self.components}
        for a, b in self.forward:
            self.succ[a].append(b)
            self.pred[b].append(a)

    def layers(self):
        indeg = {c: 0 for c in self.components}
        for a, b in self.forward:
            indeg[b] += 1
        ready = [c for c in self.components if indeg[c] == 0]
        out = []
        while ready:
            out.append(ready)
            nxt = []
            for n in ready:
                for m in self.succ[n]:
                    indeg[m] -= 1
                    if indeg[m] == 0:
                        nxt.append(m)
            ready = nxt
        return out

    def propagate(self):
        out = {}
        total_cost = 0.0
        total_lat = 0.0

        def _run_one(cid):
            """跑单个组件：先做『线性关系自测』(若有声明输入)，再调后端，
            并给产出信号打 produced_outputs 标供下游核对。

            线性关系自测（用户核心诉求：每个电阻都要会判断线性关系）：
            有 required_inputs 的电阻，核对它声明的每个产物名是否都被某条
            上游(直接前驱)信号的 produced_outputs 覆盖——覆盖即『该线性关系成立』，
            缺一个即『依赖未满足』→ 短路返回 gate:fail_linear（不调后端，
            天然喂进反馈环/adc 重试用）。无 required_inputs 的节点跳过（零回归）。
            """
            comp = self.components[cid]
            ins = [out[p] for p in self.pred[cid] if p in out]
            req = comp.get("required_inputs")
            if req:
                input_map = comp.get("input_map") or {}   # 命名漂移符号映射表（转接头）
                available = set()
                for p in self.pred[cid]:
                    s = out.get(p)
                    if s is not None and s.ok:
                        available.update(s.meta.get("produced_outputs") or [])
                # 每个声明输入经映射表翻译为上游『实际产物名』后再核对（仍报下游原名便于人读）。
                # 映射表只来自编译期确定性规则判定的等价对，故翻译是安全的、零回归的。
                missing = []
                for r in req:
                    actual = input_map.get(r, r)
                    if actual not in available:
                        missing.append(r)
                if missing:
                    return Signal(value=None, quality=0.0, ok=False,
                                  cost=0.0, latency_ms=0.0,
                                  meta={"gate": "fail_linear",
                                        "missing": missing,
                                        "required": list(req),
                                        "node": cid})
            sig = self.backend.run(comp, ins)
            # 给产出信号打 produced_outputs 标：自身声明产出 ∪ 所有上游产出（累积透传）。
            # 关键：汇合(capacitor)/适配(format_adapter)等中间节点会把上游产物名继续向
            # 下游转发，于是下游核对线性关系时只需看「直接前驱信号的 produced_outputs」即可，
            # 不必追溯整条上游链（Router 会在 producer→consumer 间插电容汇合/适配器）。
            upstream_outputs = set()
            for s in ins:
                if s is not None and s.ok:
                    upstream_outputs.update(s.meta.get("produced_outputs") or [])
            own = comp.get("produced_outputs") or []
            combined = list(dict.fromkeys(list(own) + list(upstream_outputs)))
            if combined:
                sig.meta["produced_outputs"] = combined
            return sig

        for layer in self.layers():
            layer_cost = 0.0
            layer_lat = 0.0
            if len(layer) <= 1:
                # 单节点层：直接串行（无并发必要）
                for cid in layer:
                    sig = _run_one(cid)
                    out[cid] = sig
                    layer_cost += sig.cost
                    layer_lat = max(layer_lat, sig.latency_ms)
            else:
                # 同层并联节点 → 真并发（线程池），缩短墙钟时间。
                # 安全前提：DAG 分层保证同层节点互不依赖（pred 皆在前层、已算完），
                # 本层内只向 out 写各自独立的 key，无竞态；_run_one 仅读前层 out、
                # 写本节点独立 key，无共享写竞争。
                with ThreadPoolExecutor(max_workers=len(layer)) as ex:
                    fut = {cid: ex.submit(_run_one, cid) for cid in layer}
                    for cid, f in fut.items():
                        sig = f.result()
                        out[cid] = sig
                        layer_cost += sig.cost
                        layer_lat = max(layer_lat, sig.latency_ms)
            total_cost += layer_cost
            total_lat += layer_lat
        return out, total_lat, total_cost

    def execute(self, self_heal=None, watchdog=None):
        # self_heal: None→读 spec 标志（默认 False，向后兼容）；显式 bool 可覆盖。
        # watchdog: 可选 Watchdog 实例，用于跨轮健康自检（记录每节点质量采样、识别劣化节点）。
        if self_heal is None:
            self_heal = bool(self.spec.get("self_heal", False))
        circuit_id = self.spec.get("name") or "unnamed"
        # 跨轮预升级：若某电阻在过往轮次已被看门狗标为 degraded，本轮开局优先提档
        # （small→large→tool），即"优先替换/升级"——让历史劣化节点在当前任务直接受益。
        pre_escalated = {}
        if self_heal and watchdog:
            rank = {"small": 0, "large": 1, "tool": 2}
            order = ["small", "large", "tool"]
            for cid, comp in self.components.items():
                if comp.get("type") != "resistor":
                    continue
                if not watchdog.is_degraded(circuit_id, cid):
                    continue
                cur = comp.get("model", "small")
                if rank.get(cur, 0) < 2:
                    nxt = order[rank[cur] + 1]
                    comp["model"] = nxt
                    pre_escalated[cid] = nxt
        max_iter = (self.feedback or {}).get("max_iter", 1)
        adc_id = (self.feedback or {}).get("from")
        total_cost = 0.0
        total_lat = 0.0
        iterations = 0
        success = False
        final = None
        healed = {}  # cid -> 升级后的档位（仅 self_heal 且确有升级时非空）
        for _ in range(max_iter):
            out, lat, cost = self.propagate()
            total_cost += cost
            total_lat += lat
            iterations += 1
            final = out
            # 看门狗：记录本轮各节点质量采样（跨轮累积，用于劣化识别）
            if watchdog:
                for cid, sig in out.items():
                    watchdog.record(circuit_id, cid, sig.quality)
            if adc_id and out.get(adc_id) and out[adc_id].ok:
                success = True
                break
            if not adc_id:
                success = True
                break
            # 未达标：若开启自愈且仍有反馈预算 → 对失败电阻热升级档位（small→large→tool），
            # 下一轮用升级后的拓扑续跑（运行时拓扑热更新，不重启整链）。
            if self_heal and _ < max_iter - 1:
                self._escalate_failed(out, healed)
        if adc_id and final.get(adc_id):
            fq = final[adc_id].quality
        else:
            terminals = [c for c in self.components if not self.succ[c]]
            fq = max((final[c].quality for c in terminals), default=0.0)
        # 看门狗预升级的节点也并入 healed，便于统一标记"自愈升级"
        if pre_escalated:
            healed.update(pre_escalated)
        res = {
            "success": success,
            "iterations": iterations,
            "total_cost": round(total_cost, 4),
            "total_latency_ms": total_lat,
            "final_quality": round(fq, 3),
            "watchdog_tripped": (not success) and bool(self.feedback),
            "components": {c: {"ok": s.ok, "quality": round(s.quality, 3),
                               "cost": round(s.cost, 4),
                               "latency_ms": s.latency_ms}
                           for c, s in final.items()},
        }
        if healed:
            res["self_healed"] = healed
        if pre_escalated:
            res["watchdog_pre_escalated"] = pre_escalated
        if watchdog:
            res["watchdog"] = watchdog.snapshot(circuit_id)
            watchdog.save()
        return res

    def _escalate_failed(self, out, healed):
        """对当前轮 yield 失败(ok=False)的电阻，档位逐级升一档（small→large→tool）。

        就地修改 self.components（运行时拓扑热更新）；返回是否有节点被升级。
        幂等：已到 tool 的节点不再动。
        """
        rank = {"small": 0, "large": 1, "tool": 2}
        order = ["small", "large", "tool"]
        changed = False
        for cid, sig in out.items():
            comp = self.components.get(cid)
            if comp is None or comp.get("type") != "resistor":
                continue
            if sig.ok:
                continue
            cur = comp.get("model", "small")
            if rank.get(cur, 0) < 2:
                nxt = order[rank[cur] + 1]
                comp["model"] = nxt
                healed[cid] = nxt
                changed = True
        return changed


def selftest():
    """离线自检（无需 key/网络）：验证每个电阻的『线性关系自测』。

    场景：
      S1 满足：上游 A 正常产出 x，下游 B 声明需 x → B 通过（不 gate:fail）。
      S2 上游死：A yield 失败(ok=False) → B 的 required 未满足 → gate:fail_linear。
      S3 命名漂移：A 产出 x，B 声明需 y(名不一致) → 即便 A 正常，B 仍 gate:fail_linear。
    另验：产出信号带 produced_outputs 标。
    """
    class _PlanBackend(Backend):
        """按 label 返回预定信号的测试后端（不调真模型）。"""
        def __init__(self, plan, calls=None):
            self.plan = plan          # label -> {value, quality, ok}
            self.calls = calls or {}  # label -> 调用次数
        def run(self, comp, inputs):
            label = comp.get("label")
            self.calls[label] = self.calls.get(label, 0) + 1
            p = self.plan.get(label)
            if p is None:
                return Signal(value=f"[{label}]", quality=0.9, ok=True)
            return Signal(value=p.get("value", f"[{label}]"),
                          quality=p.get("quality", 0.9), ok=p.get("ok", True))

    base_spec = {
        "name": "lincheck",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "B"]],
    }

    # S1 满足
    b1 = _PlanBackend({"A": {"value": "x-data", "quality": 0.9, "ok": True}}, {})
    c1 = Circuit(base_spec, b1)
    out1, _, _ = c1.propagate()
    assert out1["A"].ok and out1["A"].meta.get("produced_outputs") == ["x"], \
        f"S1: A 应正常产出且带 produced_outputs 标: {out1['A'].meta}"
    assert out1["B"].ok, f"S1: B 的 required(x) 已满足，不应 gate:fail: {out1['B'].meta}"
    assert out1["B"].meta.get("gate") is None, f"S1: B 不应有 gate 标记: {out1['B'].meta}"
    print("✓ S1 线性关系满足：上游产出 x → 下游 B 通过（无 gate:fail）")

    # S2 上游死
    b2 = _PlanBackend({"A": {"value": None, "quality": 0.0, "ok": False}}, {})
    c2 = Circuit(base_spec, b2)
    out2, _, _ = c2.propagate()
    assert not out2["B"].ok, "S2: 上游 A 死 → B 应 ok=False"
    assert out2["B"].meta.get("gate") == "fail_linear", \
        f"S2: B 应 gate:fail_linear: {out2['B'].meta}"
    assert "x" in (out2["B"].meta.get("missing") or []), \
        f"S2: 缺失项应含 x: {out2['B'].meta}"
    assert b2.calls.get("B", 0) == 0, \
        f"S2: B 应被短路（不调后端），实际调用 {b2.calls.get('B')} 次"
    print("✓ S2 上游失败：A 死 → B 线性关系未满足 → gate:fail_linear（短路不调后端）")

    # S3 命名漂移
    drift_spec = dict(base_spec)
    drift_spec = json.loads(json.dumps(base_spec))
    drift_spec["components"]["B"]["required_inputs"] = ["y"]  # 名不一致
    b3 = _PlanBackend({"A": {"value": "x-data", "quality": 0.9, "ok": True}}, {})
    c3 = Circuit(drift_spec, b3)
    out3, _, _ = c3.propagate()
    assert not out3["B"].ok and out3["B"].meta.get("gate") == "fail_linear", \
        f"S3: 命名漂移(y≠x) 应 gate:fail_linear: {out3['B'].meta}"
    assert "y" in (out3["B"].meta.get("missing") or []), \
        f"S3: 缺失项应含 y: {out3['B'].meta}"
    print("✓ S3 命名漂移：A 产出 x / B 需 y（名不一致）→ gate:fail_linear（抓出依赖误判）")

    # S4/S5：汇合节点透传（Router 会在 producer→consumer 间插电容汇合）
    # 验证 produced_outputs 经汇合累积透传，下游只需看直接前驱即可核对线性关系。
    merge_spec = {
        "name": "s4",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]},
            "M": {"type": "capacitor", "label": "汇合"},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "M"], ["M", "B"]],
    }
    b4 = _PlanBackend({"A": {"value": "x-data", "quality": 0.9, "ok": True}}, {})
    c4 = Circuit(merge_spec, b4)
    out4, _, _ = c4.propagate()
    assert out4["M"].meta.get("produced_outputs") == ["x"], \
        f"S4: 汇合 M 应透传上游产出 x: {out4['M'].meta}"
    assert out4["B"].ok and out4["B"].meta.get("gate") is None, \
        f"S4: B 经汇合收到 x，应通过（无 gate:fail）: {out4['B'].meta}"
    print("✓ S4 汇合透传：A→电容M→B，produced_outputs 经汇合累积，B 正确判定线性关系满足")

    merge_spec5 = json.loads(json.dumps(merge_spec))
    merge_spec5["components"]["B"]["required_inputs"] = ["y"]  # 经汇合漂移
    b5 = _PlanBackend({"A": {"value": "x-data", "quality": 0.9, "ok": True}}, {})
    c5 = Circuit(merge_spec5, b5)
    out5, _, _ = c5.propagate()
    assert not out5["B"].ok and out5["B"].meta.get("gate") == "fail_linear", \
        f"S5: 经汇合漂移(y≠x) 应 gate:fail_linear: {out5['B'].meta}"
    print("✓ S5 汇合漂移：A→M→B，B 需 y 经汇合仍被抓出 → gate:fail_linear")

    # S6 命名漂移被符号映射消解：A 产出 x / B 声明需 y，但 input_map{y:x} → B 通过（转接头生效）
    map_spec6 = json.loads(json.dumps(base_spec))
    map_spec6["components"]["B"]["required_inputs"] = ["y"]
    map_spec6["components"]["B"]["input_map"] = {"y": "x"}
    b6 = _PlanBackend({"A": {"value": "x-data", "quality": 0.9, "ok": True}}, {})
    c6 = Circuit(map_spec6, b6)
    out6, _, _ = c6.propagate()
    assert out6["B"].ok and out6["B"].meta.get("gate") is None, \
        f"S6: 符号映射应消解命名漂移，B 不应 gate:fail: {out6['B'].meta}"
    print("✓ S6 命名漂移符号映射：A 产出 x / B 需 y，input_map{y:x} → B 通过（转接头生效）")

    # S7 映射在、但上游真没产出映射目标 → 仍诚实 gate:fail（防静默误判掩盖缺数据）
    map_spec7 = json.loads(json.dumps(base_spec))
    map_spec7["components"]["A"]["produced_outputs"] = ["z"]   # A 实际产 z，不是 x
    map_spec7["components"]["B"]["required_inputs"] = ["y"]
    map_spec7["components"]["B"]["input_map"] = {"y": "x"}     # 映射指向 x，但上游没有 x
    b7 = _PlanBackend({"A": {"value": "z-data", "quality": 0.9, "ok": True}}, {})
    c7 = Circuit(map_spec7, b7)
    out7, _, _ = c7.propagate()
    assert not out7["B"].ok and out7["B"].meta.get("gate") == "fail_linear", \
        f"S7: 映射目标 x 实际缺失应 gate:fail: {out7['B'].meta}"
    assert "y" in (out7["B"].meta.get("missing") or []), \
        f"S7: 缺失项应含下游声明名 y: {out7['B'].meta}"
    print("✓ S7 映射目标真实缺失：input_map{y:x} 但上游无 x → 仍 gate:fail（诚实不掩盖）")

    print("\nruntime 线性关系自测全过 ✓（无 key / 无网络）")


if __name__ == "__main__":
    selftest()


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
