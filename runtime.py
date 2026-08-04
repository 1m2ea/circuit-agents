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
import random
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional


# ---- 看门狗（健康自检）常量 ----
# 平庸带：质量落在此区间即"将过不过 / 弱但不死"，连续 N 次 → 判定劣化。
WATCHDOG_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watchdog_state.json")
DEGRADED_BAND = (0.55, 0.85)
DEGRADED_CONSEC = 3
_WD_SAMPLES_CAP = 20


# ──────────────────────────────────────────────────────────
# ③ 自主发现新元件类型 —— composite 模板全局注册表 + 内联展开
# ──────────────────────────────────────────────────────────

_COMPONENT_LIBRARY: dict = {}       # name -> template dict


def register_component_template(template: dict) -> None:
    """注册一个 composite 模板到全局库（component_miner.wrap 调用）。"""
    _COMPONENT_LIBRARY[template["name"]] = template


def _expand_composites(spec: dict) -> dict:
    """把 spec 里的 composite 节点内联展开为原子元件。

    无 composite（或全局库为空）→ 原样返回同一对象（零成本、零回归）。
    有 composite → 深拷贝展开：外部边 P→C 重连为 P→C.entry / C.exit→Q，
    内部边照搬（节点 id 加前缀 C_ 防冲突）。
    """
    if not _COMPONENT_LIBRARY:
        return spec                         # 零回归快速路径
    comps = spec.get("components", {})
    has_composite = any(
        (c.get("type") == "composite") or c.get("template") or c.get("composite")
        for c in comps.values())
    if not has_composite:
        return spec

    new_comps: dict = {}
    new_wires: list = []
    remap: dict = {}                        # 原 cid -> {"entry":[...], "exit":[...]}

    for cid, comp in comps.items():
        tname = comp.get("template") or comp.get("composite")
        tmpl = _COMPONENT_LIBRARY.get(tname) if tname else None
        if tmpl is None and comp.get("type") == "composite":
            # 声明了 composite 但库里没模板 → 保留原样（不展开，执行时按未知类型处理）
            new_comps[cid] = comp
            remap[cid] = cid
            continue
        if tmpl is None:
            new_comps[cid] = comp
            remap[cid] = cid
            continue
        # 展开
        prefix = cid + "_"
        for iname, icomp in tmpl["internal_components"].items():
            new_comps[prefix + iname] = dict(icomp)
        for a, b in tmpl["internal_wires"]:
            new_wires.append([prefix + a, prefix + b])
        remap[cid] = {
            "entry": [prefix + e for e in tmpl["entry_nodes"]],
            "exit": [prefix + e for e in tmpl["exit_nodes"]],
        }

    # 重连外部边
    for a, b in spec.get("wires", []):
        ra = remap.get(a, a)
        rb = remap.get(b, b)
        a_outs = ra["exit"] if isinstance(ra, dict) else [ra]
        b_ins = rb["entry"] if isinstance(rb, dict) else [rb]
        for ao in a_outs:
            for bi in b_ins:
                new_wires.append([ao, bi])

    new_spec = dict(spec)
    new_spec["components"] = new_comps
    new_spec["wires"] = new_wires
    # feedback 边也要重映射（如果 feedback 指向 composite 节点）
    fb = spec.get("feedback")
    if fb:
        nfb = dict(fb)
        fa = remap.get(fb.get("from"))
        if isinstance(fa, dict) and fa["exit"]:
            nfb["from"] = fa["exit"][0]
        fb_to = remap.get(fb.get("to"))
        if isinstance(fb_to, dict) and fb_to["entry"]:
            nfb["to"] = fb_to["entry"][0]
        new_spec["feedback"] = nfb
    return new_spec


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


def _value_empty(v):
    """判断一个信号值是否为『空壳』：None / 空串 / 纯空白 / 全空聚合。"""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, Signal):
        return _value_empty(v.value)
    if isinstance(v, (list, tuple)):
        return len(v) == 0 or all(_value_empty(x) for x in v)
    return False


def _completeness_missing(ins, fields):
    """返回『下游要求、但上游承运信号值为空壳』的字段名列表（汇合完整性检查用）。"""
    missing = []
    for f in fields:
        carriers = [s for s in ins if f in (s.meta.get("produced_outputs") or [])]
        if not carriers or all(_value_empty(s.value) for s in carriers):
            missing.append(f)
    return missing


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
    def __init__(self, spec, backend, verify_backend=None, backend_map=None):
        # ③ 自主发现新元件类型：composite 节点内联展开（无 composite 时原样返回，零回归）
        spec = _expand_composites(spec)
        self.spec = spec
        self.backend = backend
        # C. 异构校验：verify 节点可选的独立后端（不同模型/供应商），未配置则 None（退回主 backend）。
        self.verify_backend = verify_backend
        # ③ 多后端并行：{backend_id: Backend} 映射，节点 spec.backend 指定用哪个
        self.backend_map = backend_map or {}
        self.components = spec["components"]
        self.feedback = spec.get("feedback")
        # 汇合节点完整性检查开关（A）：默认开；仅对『下游要求且本节点转发』的字段生效，
        # 真实非空数据不受影响（零回归）。
        self.join_completeness = spec.get("join_completeness", True)
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

    def _backend_for(self, comp):
        """③ C. 多后端路由：comp['backend'] 指定 backend_id → 查 backend_map；
        否则 verify 节点走 verify_backend，其余走主 backend。"""
        label = comp.get("label", "")
        # ③ 多后端：节点显式声明 backend_id → 优先匹配
        backend_id = comp.get("backend")
        if backend_id and backend_id in self.backend_map:
            return self.backend_map[backend_id]
        # C. 异构校验：verify 节点
        base = label.split("#")[0]
        if base == "verify" and self.verify_backend is not None:
            return self.verify_backend
        return self.backend

    def _run_one(self, cid, out):
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
        be = self._backend_for(comp)
        sig = be.run(comp, ins)
        # C. 异构校验未配置时的诚实告警：verify 节点仍在用同源主 backend
        if comp.get("label", "").split("#")[0] == "verify" and self.verify_backend is None:
            sig.meta.setdefault("warnings", []).append("hetero_verify_unconfigured")
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
        # 汇合节点完整性检查（A）：电容汇合放行的字段，若下游要求且上游承运值为空壳，
        # 标 gate=incomplete(ok=False) → 下游前驱不 ok → 下游线性关系闸 gate:fail_linear
        # → CircuitExecutor 自动补数闭环救活。仅对 capacitor + 下游要求字段生效（零回归）。
        if (self.join_completeness and sig.ok
                and comp.get("type") == "capacitor"):
            downstream_req = set()
            for sc in self.succ[cid]:
                downstream_req.update(self.components[sc].get("required_inputs") or [])
            forwarded = sig.meta.get("produced_outputs") or []
            relevant = [f for f in forwarded if f in downstream_req]
            if relevant:
                inc = _completeness_missing(ins, relevant)
                if inc:
                    sig.ok = False
                    sig.meta["gate"] = "incomplete"
                    sig.meta["incomplete_fields"] = inc
        return sig

    def propagate(self):
        out = {}
        total_cost = 0.0
        total_lat = 0.0

        for layer in self.layers():
            layer_cost = 0.0
            layer_lat = 0.0
            if len(layer) <= 1:
                # 单节点层：直接串行（无并发必要）
                for cid in layer:
                    sig = self._run_one(cid, out)
                    out[cid] = sig
                    layer_cost += sig.cost
                    layer_lat = max(layer_lat, sig.latency_ms)
            else:
                # 同层并联节点 → 真并发（线程池），缩短墙钟时间。
                # 安全前提：DAG 分层保证同层节点互不依赖（pred 皆在前层、已算完），
                # 本层内只向 out 写各自独立的 key，无竞态；_run_one 仅读前层 out、
                # 写本节点独立 key，无共享写竞争。
                with ThreadPoolExecutor(max_workers=len(layer)) as ex:
                    fut = {cid: ex.submit(self._run_one, cid, out) for cid in layer}
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


class CircuitExecutor:
    """把 Circuit 从『参谋部』升级为『前线指挥部』：自动并发 + 闭环补数据 + 动态技能派发。

    设计（见 CIRCUIT_EXECUTOR_DESIGN.md）：
      · 复用 Circuit.layers() / Circuit._run_one() / backend.run() / Signal，内核零改动。
      · 节点 gate:fail_linear 报 missing 时，执行器自动派发 filler 技能(默认 web_search)
        补数 → 合成信号 → 重跑该节点（闭环，不等人工）。
      · 节点可声明 skills/fillers，执行器在运行时主动调 execute_skill（即便 SimBackend、
        无 LLM 也能调——技能不再封在图纸上）。
      · state 黑板(_fetched/_skills_used/_trace)内部流转，供调试/CI 断言。
      · 安全：单次技能失败→可读错误文本(开路不崩)；补数有 budget 上限，不无限重跑；
        gate:fail_linear 仍诚实上抛。
    """

    def __init__(self, circuit: "Circuit", state: dict | None = None,
                 data_fill_budget: int = 2, skills_enabled: bool = True,
                 evolve_enabled: bool = True, evolve_threshold: int = 5,
                 evolve_top_k: int = 3, evolve_skill: str = "web_search",
                 verbose: bool = False, on_event: "Optional[callable]" = None,
                 events: "Optional[list]" = None, scope: str = "",
                 verify_backend: "Optional[object]" = None,
                 memory_enabled: bool = True,
                 human_callback: "Optional[callable]" = None,
                 decision_points: "Optional[object]" = None,
                 auto_select_models: bool = False,
                 on_node_done: "Optional[callable]" = None,
                 backend_map: "Optional[dict]" = None):
        """verbose     : 同时向控制台打印事件行（CI 冗余用，用户环境通常不可见）。
        on_event   : 结构化事件回调 (dict) -> None，供 SVG/UI 订阅，零重复埋点。
        events     : 外部传入的事件列表（子电路执行器共享父列表，时间线连续）。
        scope      : 事件作用域前缀（子电路用 'evolve'，渲染器据此标紫⚠）。
        on_node_done: (cid, signal, event_info) -> None，每个节点完成时调（流式用）。
        backend_map: {backend_id: Backend}，③ 多后端并行用，透传给 Circuit。
        """
        self.circuit = circuit
        self.budget = data_fill_budget
        self.skills_enabled = skills_enabled
        self.evolve_enabled = evolve_enabled
        self.evolve_threshold = evolve_threshold
        self.evolve_top_k = evolve_top_k
        self.evolve_skill = evolve_skill
        self.verbose = verbose
        self.on_event = on_event
        self.scope = scope
        # C. 异构校验：verify 节点走独立后端（不同模型/供应商）。解析为显式传入或沿用 circuit 既有配置。
        self.verify_backend = verify_backend or getattr(circuit, "verify_backend", None)
        self.circuit.verify_backend = self.verify_backend
        self.state = state or {"_fetched": {}, "_skills_used": [], "_trace": []}
        # ① 规划器接 evolve_requests：把 spec 顶层 evolve_requests 种进 state，
        #    供 maybe_evolve 的显式提示队列消费（零回归：无则空列表，退旧自动行为）。
        if "_evolve_requests" not in self.state:
            self.state["_evolve_requests"] = list(self.circuit.spec.get("evolve_requests") or [])
        # 观察窗（B）：事件流 + 最终节点结果 + 补数/进化标记，供 executor_trace 渲染。
        self._events = events if events is not None else []
        self._t0 = None
        self._results = {}
        self._filled_nodes = set()      # 触发过自动补数闭环的节点（橙虚框）
        self._evolved_from_node = None  # 触发 3.5 进化的来源节点（紫⚠）
        # C 记忆与学习：执行后记录拓扑+结果，供类似任务复用（零回归：失败静默）
        self.memory_enabled = memory_enabled
        # D 人机协同：质量门耗尽时调 human_callback 请求人类介入（零回归：None=现行行为）
        self.human_callback = human_callback
        # ⑧ 加深：决策点暂停——指定节点执行前主动暂停请人类审批（None=不启用主动决策点）
        self.decision_points = decision_points
        # ③ 智能模型选型：执行前按复杂度/历史/约束微调每个电阻的 model/skills
        self.auto_select_models = auto_select_models
        # ② 流式执行：节点完成回调（供 SSE/run_stream 消费）
        self.on_node_done = on_node_done
        # ③ 多后端并行：透传 backend_map 到 Circuit
        self._backend_map = backend_map or {}
        self.circuit.backend_map = self._backend_map

    # ---- 观察窗（B）：事件流发射 ----
    def _emit(self, etype: str, **fields):
        """统一发射一个事件：① 写进 self._events（供 SVG 渲染）② 调 on_event 回调
        ③ 若 verbose 同时打印控制台行（CI 冗余）。零重复埋点。"""
        if self._t0 is None:
            self._t0 = time.perf_counter()
        t = (time.perf_counter() - self._t0) * 1000.0
        ev = {"t": round(t, 1), "type": etype, "scope": self.scope}
        ev.update(fields)
        self._events.append(ev)
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:
                pass
        if self.verbose:
            print(self._fmt_event(ev))

    @staticmethod
    def _fmt_event(ev: dict) -> str:
        t = ev.get("t", 0.0)
        et = ev.get("type", "?")
        node = ev.get("node")
        scope = ev.get("scope")
        tag = f"[{scope}] " if scope else ""
        if node is not None:
            tag += f"{node} · "
        extra = {k: v for k, v in ev.items()
                 if k not in ("t", "type", "scope", "node")}
        return f"[+{t:7.1f}ms] {et:<14} {tag}{extra}"

    # ---- 执行器主动派发（动态技能调用核心）----
    def dispatch(self, cid: str, spec: dict) -> str:
        name = spec.get("skill")
        args = spec.get("args", {})
        if not self.skills_enabled or not name:
            self._emit("skill_skip", node=cid, skill=name)
            return f"[no-skill-fill:{name}]"
        try:
            from compiler.agent_skills import execute_skill
        except Exception as e:
            self._emit("skill_error", node=cid, skill=name, error=str(e))
            return f"[skill 模块不可用: {e}]"
        self.state["_skills_used"].append(name)
        self.state["_trace"].append({"action": "dispatch_skill", "node": cid,
                                      "skill": name, "args": args})
        self._emit("skill_call", node=cid, skill=name, args=args)
        try:
            result = execute_skill(name, json.dumps(args))
        except Exception as e:
            self._emit("skill_error", node=cid, skill=name, error=str(e))
            return f"[skill 错误: {e}]"
        self._emit("skill_return", node=cid, skill=name,
                   ok=True, length=len(str(result)))
        return result

    # ---- 自动补数据：对 missing 逐个派发 filler，写回 state._fetched ----
    def _auto_fill(self, cid: str, missing: list):
        comp = self.circuit.components[cid]
        fillers = comp.get("fillers") or {}
        self._filled_nodes.add(cid)
        for m in missing:
            if m in self.state["_fetched"]:
                continue
            spec = fillers.get(m) or {"skill": "web_search", "args": {"query": m}}
            self.state["_fetched"][m] = self.dispatch(cid, spec)

    # ---- 带补给信号重跑该节点（数据已由执行器补齐，绕过线性关系闸）----
    def _rerun_with_filled(self, cid: str, out: dict):
        comp = self.circuit.components[cid]
        real_inputs = [out[p] for p in self.circuit.pred[cid] if p in out]
        synth = [Signal(value=self.state["_fetched"][m], quality=0.6, ok=True,
                        meta={"produced_outputs": [m], "auto_filled": True})
                 for m in (comp.get("required_inputs") or [])
                 if m in self.state["_fetched"]]
        sig = self.circuit._backend_for(comp).run(comp, real_inputs + synth)
        upstream = set()
        for s in real_inputs + synth:
            if s is not None and s.ok:
                upstream.update(s.meta.get("produced_outputs") or [])
        own = comp.get("produced_outputs") or []
        combined = list(dict.fromkeys(list(own) + list(upstream)))
        if combined:
            sig.meta["produced_outputs"] = combined
        return sig

    # ---- 分层 propagate + 闭环补数 ----
    # ---- ⑧ 加深：决策点判定 + 中止结果（人机协同）----
    def _is_decision_point(self, cid, comp):
        """该节点是否为主动决策点（执行前暂停请人类审批）。"""
        if comp.get("human_decision_point"):       # spec 显式声明
            return True
        dp = self.decision_points
        if dp is None:
            return False
        if dp == "all":
            return True
        if isinstance(dp, (set, list, tuple)):
            base = (comp.get("label") or "").split("#")[0]
            cap = comp.get("capability")
            return (cid in dp) or (base in dp) or (cap in dp)
        return False

    def _abort_result(self, cid, out):
        """人类中止（决策点 abort 或 失败兜底 abort）的统一返回。"""
        self._emit("human_abort", node=cid)
        self._emit("layer_done", layer_idx=getattr(self, "_li", None))
        self._results = out
        terminals = [c for c in self.circuit.components if not self.circuit.succ[c]]
        # 决策点 abort 发生在节点执行「前」，下游终端可能尚未进入 out，需容错取值；
        # 若终端全未执行，则退化为已执行节点的最大质量（反映中止时刻的真实进度）。
        fq = max((out[c].quality for c in terminals if c in out), default=None)
        if fq is None:
            fq = max((s.quality for s in out.values()), default=0.0)
        total_cost = sum(s.cost for s in out.values())
        total_lat = max((s.latency_ms for s in out.values()), default=0.0)
        self._emit("done", total_cost=round(total_cost, 4),
                   total_latency_ms=round(total_lat, 1),
                   final_quality=round(fq, 3), aborted=True)
        return {
            "success": False, "final_quality": round(fq, 3),
            "total_cost": round(total_cost, 4),
            "total_latency_ms": total_lat,
            "components": {c: {"ok": s.ok, "quality": round(s.quality, 3),
                               "gate": s.meta.get("gate")}
                           for c, s in out.items()},
            "state": self.state, "evolved": None,
            "iterations": 1, "self_healed": {},
            "aborted": True, "abort_node": cid,
        }

    def run(self):
        self._t0 = time.perf_counter()
        out = {}
        layers = self.circuit.layers()
        # ③ 智能模型选型：执行前按复杂度/历史/约束微调电阻 model/skills
        # Phase 2 ③ 增强：接入 ModelMetrics 真实历史，做成功率/延迟/成本多目标再平衡；
        #   并把 selector 实例提到 if 外，供执行后回填历史（反馈闭环）。
        _ms = None
        _model_recs = None
        if self.auto_select_models:
            try:
                from compiler.model_selector import ModelSelector, ModelMetrics
                from compiler.topology_memory import TopologyMemory
                mem = TopologyMemory() if self.memory_enabled else None
                _metrics = ModelMetrics(ModelMetrics.DEFAULT_PATH).load() \
                    if self.memory_enabled else None
                _ms = ModelSelector(memory=mem, metrics=_metrics)
                _model_recs = _ms.select(self.circuit.spec)
                _ms.apply_to_spec(self.circuit.spec)
            except Exception:
                _ms = None
                pass  # 选型失败 → 沿用原 spec 不变（零回归）
        self._emit("start",
                   spec=self.circuit.spec.get("name", "unnamed"),
                   nodes=len(self.circuit.components),
                   layers=len(layers))

        for li, layer in enumerate(layers):
            self._li = li
            self._emit("layer_start", layer_idx=li, nodes=list(layer))
            for cid in layer:
                comp = self.circuit.components[cid]
                self._emit("node_start", node=cid, ctype=comp.get("type"),
                           label=comp.get("label"))
                # ⑧ 加深：决策点暂停（主动请求人类审批，而非仅失败兜底）
                _skip = False
                if self.human_callback and self._is_decision_point(cid, comp):
                    _pending = {k: (v.value if hasattr(v, "value") else str(v))
                                for k, v in out.items() if hasattr(v, "value")}
                    self._emit("human_decision_point", node=cid, label=comp.get("label"))
                    try:
                        _dec = self.human_callback(
                            node=cid, missing=comp.get("required_inputs", []),
                            context=_pending, label=comp.get("label"),
                            decision_point=True)
                    except Exception:
                        _dec = "proceed"   # 不兼容的回调 → 默认继续，最不意外
                    if _dec == "abort":
                        out[cid] = Signal(value=None, quality=0.0, ok=False,
                                         meta={"open": "human_abort",
                                               "decision_point": True})
                        return self._abort_result(cid, out)
                    if _dec == "skip":
                        sig = Signal(value=None, quality=0.0, ok=False,
                                     meta={"human_skipped": True,
                                           "decision_point": True})
                        self._emit("human_skip", node=cid)
                        _skip = True
                    # "proceed" 或其它 → 正常执行该节点
                if not _skip:
                    sig = self.circuit._run_one(cid, out)   # 现有线性关系闸 + backend.run
                if sig.meta.get("gate") == "fail_linear":
                    self._emit("gate_fail", node=cid,
                               missing=sig.meta.get("missing", []))
                    b = self.budget
                    while sig.meta.get("gate") == "fail_linear" and b > 0:
                        self._auto_fill(cid, sig.meta.get("missing", []))
                        sig = self._rerun_with_filled(cid, out)
                        b -= 1
                        self._emit("retry", node=cid, ok=sig.ok,
                                   budget_left=b,
                                   filled=sig.meta.get("auto_filled", False))
                    # D 人机协同：预算耗尽仍 fail → 请求人类介入（零回归：无 callback 则跳过）
                    if sig.meta.get("gate") == "fail_linear" and self.human_callback:
                        missing = sig.meta.get("missing", [])
                        upstream = {k: (v.value if hasattr(v, "value") else str(v))
                                    for k, v in out.items() if hasattr(v, "value")}
                        self._emit("human_intervention", node=cid, missing=missing)
                        try:
                            decision = self.human_callback(
                                node=cid, missing=missing,
                                context=upstream, label=comp.get("label"))
                        except Exception:
                            decision = "skip"
                        if decision == "retry":
                            # 人类提供了上下文，重跑该节点
                            sig = self.circuit._run_one(cid, out)
                            self._emit("human_retry", node=cid, ok=sig.ok)
                        elif decision == "abort":
                            out[cid] = sig
                            return self._abort_result(cid, out)
                        # "skip" 或其它 → 标记跳过，继续执行
                        if decision == "skip":
                            sig.meta["human_skipped"] = True
                            self._emit("human_skip", node=cid)
                self._emit("node_done", node=cid, ok=sig.ok,
                           quality=round(sig.quality, 3),
                           retried=(cid in self._filled_nodes))
                # ② 流式执行：通知外部观察者（SSE/run_stream）
                if self.on_node_done is not None:
                    try:
                        self.on_node_done(cid, sig, {"node": cid, "ok": sig.ok,
                                         "quality": round(sig.quality, 3),
                                         "value": getattr(sig, "value", None),
                                         "retried": (cid in self._filled_nodes)})
                    except Exception:
                        pass
                out[cid] = sig
            self._emit("layer_done", layer_idx=li)

        self._results = out
        terminals = [c for c in self.circuit.components if not self.circuit.succ[c]]
        fq = max((out[c].quality for c in terminals), default=0.0)
        total_cost = sum(s.cost for s in out.values())
        total_lat = max((s.latency_ms for s in out.values()), default=0.0)

        # 3.5 多任务进化：检索结果(写入 state._fetched)决定第二步拓扑
        evolved = None
        if self.evolve_enabled:
            sub = self.maybe_evolve(self.state)
            if sub is not None:
                from_key = sub.get("_evolve_from")
                # 反查「声明需要该检索结果」的节点，作为紫⚠高亮目标（_fetched 键≠节点 id）
                src_node = None
                for c, comp in self.circuit.components.items():
                    if from_key in (comp.get("required_inputs") or []):
                        src_node = c
                        break
                self._evolved_from_node = src_node or from_key
                self._emit("evolve_detect", from_node=self._evolved_from_node,
                           count=len(self.state["_fetched"].get(from_key, []) or []),
                           threshold=getattr(self, "_evolve_thr", self.evolve_threshold),
                           top_k=getattr(self, "_evolve_top_k", self.evolve_top_k),
                           explicit=bool(sub.get("_evolve_explicit", False)))
                self._emit("evolve_spawn", sub_name=sub.get("name"),
                           from_node=from_key)
                child = CircuitExecutor(
                    Circuit(sub, self.circuit.backend, verify_backend=self.verify_backend,
                            backend_map=self._backend_map),
                    state=self.state,
                    data_fill_budget=self.budget,
                    skills_enabled=self.skills_enabled,
                    evolve_enabled=False,          # 子电路不再进化，防无限递归
                    events=self._events,            # 共享事件列表 → 时间线连续
                    on_event=self.on_event,
                    verbose=self.verbose,
                    scope="evolve",
                )
                child._t0 = self._t0              # 事件时间接在父时钟上，连续
                evolved = child.run()
                self.state["_evolved"] = {
                    "spec_name": sub.get("name"),
                    "result": evolved,
                }

        self._emit("done",
                   total_cost=round(total_cost, 4),
                   total_latency_ms=round(total_lat, 1),
                   final_quality=round(fq, 3))
        # 诊断信息：质量门阈值 + 失败节点（供前端精准展示，零回归）
        gate_nodes = [(c, comp) for c, comp in self.circuit.components.items()
                      if comp.get("type") in ("adc", "verify")]
        quality_gate = None
        gate_thr = None
        if gate_nodes:
            gate_thr = max(comp.get("threshold", 0.8) for _, comp in gate_nodes)
            quality_gate = {"threshold": round(gate_thr, 3), "passed": fq >= gate_thr}
        failed_nodes = [c for c in terminals if not out[c].ok]
        # Phase 2 细粒度质量门：逐节点打分 + 分级 + 修复建议（纯函数、离线安全）
        _report_thr = gate_thr if gate_thr is not None else getattr(self, "quality_threshold", None)
        quality_report = QualityReport.assess(out, self.circuit.components, fq, _report_thr)
        # Phase 2 技能注册表：解析本拓扑引用了哪些技能、哪些待实现（离线安全）
        _skill_used = SkillRegistry().resolve(self.circuit.components, self.evolve_skill)
        # Phase 2 ③ 模型选型再平衡：反馈闭环——执行后把真实表现回填 ModelMetrics（零回归）
        if _ms is not None:
            try:
                _ms.record_outcomes(self.circuit.components, out, total_lat, total_cost)
            except Exception:
                pass

        result = {
            "success": all(out[c].ok for c in terminals),
            "final_quality": round(fq, 3),
            "total_cost": round(total_cost, 4),
            "total_latency_ms": total_lat,
            "components": {c: {"ok": s.ok, "quality": round(s.quality, 3),
                               "gate": s.meta.get("gate")}
                           for c, s in out.items()},
            "state": self.state,
            "evolved": self.state.get("_evolved"),
            "iterations": 1,
            "self_healed": {},
            "quality_gate": quality_gate,
            "failed_nodes": failed_nodes,
            "quality_report": quality_report,
            "skills_used": _skill_used,
            "model_selection": _model_recs,
        }
        # C 记忆与学习：执行后记录拓扑+结果（零回归：失败静默）
        if self.memory_enabled and not self.scope:  # 子电路(evolve)不记录
            try:
                from compiler.topology_memory import TopologyMemory
                mem = TopologyMemory()
                goal_desc = self.circuit.spec.get("description", "")
                mem.record(goal_desc, self.circuit.spec, result)
            except Exception:
                pass
        return result

    # ---- ② 流式执行：逐节点 yield 结果（供 SSE/API 消费）----
    def run_stream(self):
        """流式执行生成器：每完成一个节点就 yield 事件 dict，最后 yield 完整结果。

        用法:
          for event in executor.run_stream():
              if event["type"] == "node_done":
                  print(f"  ✓ {event['node']} ok={event['ok']}")
              elif event["type"] == "result":
                  print(f"  最终品质={event['result']['final_quality']}")
        """
        import threading
        queue = []
        done = threading.Event()

        def _cb(cid, sig, info):
            info["type"] = "node_done"
            queue.append(info)

        # 临时安装回调，执行完毕后恢复
        prev_cb = self.on_node_done
        self.on_node_done = _cb
        result = None

        def _runner():
            nonlocal result
            result = self.run()
            done.set()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

        # 轮询 yield 节点事件（50ms 间隔，非忙等）
        idx = 0
        while not done.is_set():
            while idx < len(queue):
                yield queue[idx]
                idx += 1
            import time
            time.sleep(0.05)
        # 排空最后一批
        while idx < len(queue):
            yield queue[idx]
            idx += 1
        # 最后 yield 结果
        self.on_node_done = prev_cb  # 恢复
        yield {"type": "result", "result": result}

    # ---- 3.5 多任务进化增强（D）：泛化触发 + 显式提示 + 阈值可配 ----
    def _countable(self, val):
        """把 state._fetched 的某值归一为 (items, count)：
        - list/tuple/set → 直接计数；
        - dict → 按键计数（items 为键列表）；
        - JSON 列表串 → 解析后计数；
        其它（普通字符串/数字/None）→ (None, 0)，不触发进化（零回归，无误触发）。
        """
        if isinstance(val, (list, tuple, set)):
            return list(val), len(val)
        if isinstance(val, dict):
            return list(val.keys()), len(val)
        if isinstance(val, str):
            s = val.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return parsed, len(parsed)
                except Exception:
                    return None, 0
        return None, 0

    def maybe_evolve(self, state) -> "Optional[dict]":
        """钩子（设计书 3.5，D 增强）：扫描 state，决定第二步『分析子电路』是否生成。
        零回归：无非列表值误触发；未达阈值且无显式提示 → 返回 None（普通执行不受影响）。

        触发优先级：
        ② 显式提示队列 state._evolve_requests（组件/技能可绕过阈值强制进化）：
           形如 [{"key": "frameworks", "top_k": 3}, ...]，命中即生成（top_k 可单条覆盖）。
        ① 泛化自动发现：对任意可计数集合（list/tuple/set/dict/JSON 列表串），
           若 count > 生效阈值 → 取前 evolve_top_k 条生成。

        生效阈值/取数：构造参数 evolve_threshold/evolve_top_k，可被 circuit.spec
        的 evolve_threshold/evolve_top_k 覆盖（③ 可配）。
        """
        if not self.evolve_enabled:
            return None
        thr = self.circuit.spec.get("evolve_threshold", self.evolve_threshold)
        top_k = self.circuit.spec.get("evolve_top_k", self.evolve_top_k)
        self._evolve_thr = thr           # 供 run() 的 evolve_detect 事件用真实生效值
        self._evolve_top_k = top_k
        fetched = state.get("_fetched", {})

        # ② 显式提示队列（绕过阈值）
        for req in (state.get("_evolve_requests") or []):
            key = req.get("key")
            if not key or key not in fetched:
                continue
            items, _ = self._countable(fetched[key])
            if items is None:
                continue
            k = req.get("top_k", top_k)
            chosen = items[:k]
            sub = self._build_subcircuit(key, chosen)
            sub["_evolve_from"] = key
            sub["_evolve_explicit"] = True
            return sub

        # ① 泛化自动发现：任意可计数集合且 count > 阈值
        for key, val in fetched.items():
            items, count = self._countable(val)
            if items is None:
                continue
            if count > thr:
                chosen = items[:top_k]
                sub = self._build_subcircuit(key, chosen)
                sub["_evolve_from"] = key
                return sub
        return None

    def _build_subcircuit(self, src_key, chosen):
        """据检索到的 top-k 条目，拼出第二步『分析子电路』spec。"""
        chosen_text = json.dumps(chosen, ensure_ascii=False)
        arg_key = "query" if self.evolve_skill == "web_search" else "items"
        arg_val = (f"深入分析最热{len(chosen)}项: {chosen_text}"
                   if self.evolve_skill == "web_search" else chosen_text)
        in_name = f"top_{src_key}"
        return {
            "name": f"evolve_{src_key}",
            "components": {
                "src": {"type": "power", "label": "task"},
                "analyze": {
                    "type": "resistor", "label": f"analyze_{src_key}",
                    "model": "small",
                    "required_inputs": [in_name],
                    "produced_outputs": ["analysis"],
                    "fillers": {in_name: {
                        "skill": self.evolve_skill,
                        "args": {arg_key: arg_val},
                    }},
                },
            },
            "wires": [["src", "analyze"]],
        }


def circuit_executor_selftest():
    """CircuitExecutor 离线自检（无 key/无网）：自动补数据闭环 + 动态技能派发。"""
    import random
    from compiler.agent_skills import SKILLS

    def _demo_fetch(query: str) -> str:
        return f"[demo_fetch] {query}: China GDP 2024 ≈ 18.94T (demo)"
    SKILLS["exec_demo_fetch"] = {
        "name": "exec_demo_fetch",
        "description": "executor selftest 确定性检索",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        "handler": _demo_fetch,
    }

    spec = {
        "name": "exec_selftest",
        "components": {
            "src": {"type": "power", "label": "task"},
            "reason": {
                "type": "resistor", "label": "reason", "model": "small",
                "required_inputs": ["china_gdp_2024"],
                "produced_outputs": ["report"],
                "fillers": {"china_gdp_2024": {"skill": "exec_demo_fetch",
                                               "args": {"query": "china gdp 2024"}}},
            },
        },
        "wires": [["src", "reason"]],
    }
    # 对照：仅 Circuit.propagate → gate:fail_linear
    bare = Circuit(spec, SimBackend(random.Random(0)))
    ob, _, _ = bare.propagate()
    assert ob["reason"].meta.get("gate") == "fail_linear", "对照应 gate:fail_linear"

    # CircuitExecutor：自动补数 → ok
    ex = CircuitExecutor(Circuit(spec, SimBackend(random.Random(0))),
                         data_fill_budget=2)
    res = ex.run()
    assert res["components"]["reason"]["ok"], "executor 应自动补数使 reason ok"
    assert "china_gdp_2024" in res["state"]["_fetched"]
    assert "exec_demo_fetch" in res["state"]["_skills_used"]
    print("✓ CircuitExecutor: gate:fail_linear → 自动补数(动态技能派发) → 重跑 ok（SimBackend）")

    # D 验证：节点声明 filler 用真实已注册技能 calculator → 执行器主动调用（无 LLM 在场）
    spec2 = {
        "name": "exec_d",
        "components": {
            "src": {"type": "power", "label": "task"},
            "calc": {
                "type": "resistor", "label": "calc", "model": "small",
                "required_inputs": ["interest"],
                "produced_outputs": ["report"],
                "fillers": {"interest": {"skill": "calculator",
                                         "args": {"expression": "10000*(1+0.035*5)"}}},
            },
        },
        "wires": [["src", "calc"]],
    }
    ex2 = CircuitExecutor(Circuit(spec2, SimBackend(random.Random(0))), data_fill_budget=2)
    res2 = ex2.run()
    assert res2["components"]["calc"]["ok"], "executor 应调用 calculator 技能补数使 calc ok"
    assert "calculator" in res2["state"]["_skills_used"], "应真实调用已注册技能 calculator"
    print("✓ D 动态技能调用: 节点声明 calculator filler → 执行器主动调真技能(无 LLM) → ok")

    # 观察窗（B）：事件流 + 最终节点结果 + 补数标记应被填充（零回归）
    assert ex._events, "executor 应填充 _events 观察窗事件流"
    assert ex._results.get("reason") is not None, "应记录 reason 最终信号"
    assert "reason" in ex._filled_nodes, "reason 触发过自动补数 → 应在 _filled_nodes"
    print(f"✓ 观察窗(B): _events={len(ex._events)} 条 · reason 已补数闭环 · 渲染键齐全")

    # A 协同验证：汇合完整性检查 + CircuitExecutor 自动补数闭环端到端。
    # A 产出空壳 x → 电容 M gate=incomplete → B 前驱不 ok → B gate:fail_linear
    # → 执行器按 B 的 filler 自动补 x → B ok。
    class _EmptyBackend(Backend):
        def run(self, comp, inputs):
            if comp.get("type") == "resistor" and comp.get("label") == "A":
                return Signal(value="", quality=0.9, ok=True,
                              meta={"produced_outputs": ["x"]})
            return Signal(value=f"[{comp.get('label')}]", quality=0.9, ok=True)
    spec3 = {
        "name": "exec_completeness",
        "components": {
            "src": {"type": "power", "label": "task"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]},
            "M": {"type": "capacitor", "label": "merge"},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"], "produced_outputs": ["report"],
                  "fillers": {"x": {"skill": "exec_demo_fetch",
                                    "args": {"query": "x filler"}}}},
        },
        "wires": [["src", "A"], ["A", "M"], ["M", "B"]],
    }
    ex3 = CircuitExecutor(Circuit(spec3, _EmptyBackend()), data_fill_budget=2)
    res3 = ex3.run()
    assert res3["components"]["B"]["ok"], \
        "完整性检查应触发 B fail_linear → 执行器补 x → B ok"
    assert "x" in res3["state"]["_fetched"], "执行器应自动补到 x"
    print("✓ A 协同：电容完整性(空壳x)→ B fail_linear → 执行器自动补数 → B ok（端到端）")


def circuit_executor_evolve_selftest():
    """3.5 多任务进化离线自检（无 key/无网）：检索结果决定第二步拓扑。

    构造 research 节点缺 `frameworks`，filler 用 ci_list_search 返回 JSON 列表(8 条)。
    执行器自动补数后，state._fetched["frameworks"] 是列表且 8>5(阈值) → maybe_evolve
    动态拼出『分析 top3』子电路并递归执行 → state._evolved 存在且分析节点 ok。
    """
    import random
    from compiler.agent_skills import SKILLS

    def _list_search(query):
        return json.dumps(
            ["LangGraph", "AutoGen", "CrewAI", "MetaGPT",
             "AgencySwarm", "OpenAI Swarm", "PhiData", "AgentOps"],
            ensure_ascii=False)
    SKILLS["ci_list_search"] = {
        "name": "ci_list_search",
        "description": "返回最新 Agent 框架列表(JSON)",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        "handler": _list_search,
    }

    def _analyze(items):
        return f"[analysis] 重点分析: {items}"
    SKILLS["ci_analyze"] = {
        "name": "ci_analyze",
        "description": "分析 top-k 框架",
        "parameters": {"type": "object", "properties": {"items": {"type": "string"}}},
        "handler": _analyze,
    }

    spec = {
        "name": "research_agents",
        "components": {
            "src": {"type": "power", "label": "task"},
            "research": {
                "type": "resistor", "label": "research", "model": "small",
                "required_inputs": ["frameworks"],
                "produced_outputs": ["report"],
                "fillers": {"frameworks": {"skill": "ci_list_search",
                                           "args": {"query": "latest agent frameworks"}}},
            },
        },
        "wires": [["src", "research"]],
    }
    ex = CircuitExecutor(Circuit(spec, SimBackend(random.Random(0))),
                         data_fill_budget=2, evolve_skill="ci_analyze",
                         evolve_threshold=5, evolve_top_k=3)
    res = ex.run()
    assert res["components"]["research"]["ok"], "research 应自动补数 ok"
    assert "frameworks" in res["state"]["_fetched"], "应补到 frameworks"
    assert "_evolved" in res["state"], "检索 8 条(>5) 应触发多任务进化 _evolved"
    ev = res["state"]["_evolved"]
    assert ev["result"]["components"].get("analyze", {}).get("ok"), \
        "进化出的分析子电路 analyze 应 ok"
    assert ex._evolved_from_node == "research", "进化来源节点应反查到 research"
    assert any(e.get("scope") == "evolve" for e in ex._events), \
        "子电路事件应并入时间线(scope=evolve)"
    print("✓ 3.5 多任务进化: research 检索到 8 框架(>5) → 动态拼『分析 top3』子电路递归执行 → _evolved 存在且 analysis ok")
    print(f"✓ 观察窗(B): 进化来源节点={ex._evolved_from_node} · 子电路事件并入时间线(scope=evolve)")


def evolve_enhanced_selftest():
    """D 增强离线自检（无 key/无网）：验证三件套——
    ① 任意可计数集合触发 ② 显式提示队列绕过阈值 ③ 阈值/取数可配（零回归：非列表不误触发）。
    """
    import random
    base_spec = {"name": "ev", "components": {"s": {"type": "power", "label": "s"}},
                 "wires": []}

    def _mk(**kw):
        return CircuitExecutor(Circuit({**base_spec}, SimBackend(random.Random(0))), **kw)

    # D1 显式提示绕过阈值：列表仅 1 条(<=5) 但 _evolve_requests 强制
    ex = _mk(evolve_threshold=5, evolve_top_k=3)
    sub = ex.maybe_evolve({"_fetched": {"frameworks": ["LangGraph"]},
                            "_evolve_requests": [{"key": "frameworks", "top_k": 1}]})
    assert sub is not None, "显式提示应绕过阈值强制进化"
    assert sub.get("_evolve_from") == "frameworks"
    assert sub.get("_evolve_explicit") is True, "应标记显式进化"
    print("✓ D 显式提示: 列表仅1条(<=阈值) 但 _evolve_requests 强制进化(_evolve_explicit)")

    # D2 非列表值不误触发（普通字符串/数字）
    ex2 = _mk(evolve_threshold=5, evolve_top_k=3)
    assert ex2.maybe_evolve({"_fetched": {"x": "just a plain string"}}) is None, \
        "普通字符串不应触发进化"
    assert ex2.maybe_evolve({"_fetched": {"n": 42}}) is None, \
        "数字不应触发进化"
    print("✓ D 零误触发: 普通字符串/数字不触发进化")

    # D3 dict 按 key 计数触发
    ex3 = _mk(evolve_threshold=5, evolve_top_k=3)
    d = {f"k{i}": i for i in range(6)}      # 6 键 > 5
    sub3 = ex3.maybe_evolve({"_fetched": {"opts": d}})
    assert sub3 is not None, "dict(6键)>5 应触发进化"
    assert sub3.get("_evolve_from") == "opts"
    print("✓ D 泛化触发: dict(6键)>阈值 触发进化(按 key 计数)")

    # D4 阈值/取数可配：circuit.spec 覆盖构造参数
    spec_lo = {**base_spec, "evolve_threshold": 2, "evolve_top_k": 2}
    ex4 = CircuitExecutor(Circuit(spec_lo, SimBackend(random.Random(0))),
                           evolve_threshold=5, evolve_top_k=3)
    sub4 = ex4.maybe_evolve({"_fetched": {"items": ["a", "b", "c"]}})  # 3 > 2(spec)
    assert sub4 is not None, "spec.evolve_threshold=2 应对 3 条触发"
    assert ex4._evolve_thr == 2 and ex4._evolve_top_k == 2, "应取 spec 覆盖值"
    print("✓ D 阈值可配: circuit.spec.evolve_threshold=2 对 3 条触发(覆盖构造参数)")

    # D5 tuple/set 也可触发
    ex5 = _mk(evolve_threshold=3, evolve_top_k=2)
    assert ex5.maybe_evolve({"_fetched": {"s": ("a", "b", "c", "d")}}) is not None, \
        "tuple(4)>3 应触发"
    print("✓ D 泛化触发: tuple(4)>阈值 触发进化")


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

    # S8 汇合节点完整性检查（A）：A 产出 x 但值为空壳 → 电容 M 转发 x(空) → 下游 B 要求 x
    # 应被完整性检查拦下：M gate=incomplete(ok=False) → B 前驱不 ok → B gate:fail_linear。
    emp_spec = json.loads(json.dumps(base_spec))
    emp_spec["components"]["M"] = {"type": "capacitor", "label": "merge"}
    emp_spec["wires"] = [["src", "A"], ["A", "M"], ["M", "B"]]
    b8 = _PlanBackend({"A": {"value": "", "quality": 0.9, "ok": True}}, {})
    c8 = Circuit(emp_spec, b8)
    out8, _, _ = c8.propagate()
    assert (not out8["M"].ok) and out8["M"].meta.get("gate") == "incomplete", \
        f"S8: 电容 M 应因转发空壳 x 被标 gate=incomplete: {out8['M'].meta}"
    assert (not out8["B"].ok) and out8["B"].meta.get("gate") == "fail_linear", \
        f"S8: B 应因前驱 M 不 ok 而 gate:fail_linear: {out8['B'].meta}"
    # 对照：关闭完整性检查 → 旧行为（空壳放行，B 线性关系满足）
    emp_spec_off = json.loads(json.dumps(emp_spec))
    emp_spec_off["join_completeness"] = False
    c8off = Circuit(emp_spec_off,
                    _PlanBackend({"A": {"value": "", "quality": 0.9, "ok": True}}, {}))
    out8off, _, _ = c8off.propagate()
    assert out8off["B"].ok, "关闭完整性检查时 B 应放行空壳(旧行为对照)"
    print("✓ S8 汇合完整性：A 空壳 x → 电容 M gate=incomplete → B gate:fail_linear（不传空壳）；对照关检查→旧行为")

    print("\nruntime 线性关系自测全过 ✓（无 key / 无网络）")


def hetero_verify_selftest():
    """C. 异构校验离线自检（无 key/无网）：verify 节点走独立 backend，其余走主 backend。"""
    class _TagBackend(Backend):
        def __init__(self, tag):
            self.tag = tag
            self.served = []
        def run(self, comp, inputs):
            self.served.append(comp.get("label"))
            return Signal(value=f"[{self.tag}:{comp.get('label')}]",
                          quality=0.9, ok=True)
    main = _TagBackend("main")
    ver = _TagBackend("verify")
    spec = {
        "name": "het",
        "components": {
            "src": {"type": "power", "label": "task"},
            "reason": {"type": "resistor", "label": "reason", "model": "small"},
            "V": {"type": "resistor", "label": "verify", "model": "small"},
        },
        "wires": [["src", "reason"], ["reason", "V"], ["src", "V"]],
    }
    c = Circuit(spec, main, verify_backend=ver)
    c.propagate()
    assert "reason" in main.served, "reason 应走主 backend"
    assert "verify" in ver.served, "verify 应走独立 verify backend（异构）"
    assert "reason" not in ver.served, "verify backend 不应服务非 verify 节点"
    # 零回归：未配置 verify_backend → verify 退回主 backend
    main2 = _TagBackend("main")
    c2 = Circuit(spec, main2)
    c2.propagate()
    assert "verify" in main2.served, "未配置时 verify 退回主 backend（零回归）"
    print("✓ C 异构校验：verify 节点走独立 backend，其余走主 backend；未配置则退回（零回归）")


def memory_record_selftest():
    """C 记忆与学习：CircuitExecutor 执行后自动记录到 TopologyMemory。"""
    import random
    from compiler.topology_memory import TopologyMemory

    # 用临时文件做隔离测试
    import tempfile
    tmp_mem = tempfile.mktemp(suffix=".json")
    mem = TopologyMemory(path=tmp_mem)

    # 手动 record（模拟 CircuitExecutor.run() 末尾的行为）
    spec = {
        "name": "mem_test",
        "description": "测试记忆记录的简单任务",
        "components": {
            "src": {"type": "power", "label": "task"},
            "r1": {"type": "resistor", "label": "reason", "model": "small",
                   "required_inputs": [], "produced_outputs": ["result"]},
        },
        "wires": [["src", "r1"]],
    }
    result = {"success": True, "final_quality": 0.95,
              "total_latency_ms": 100, "total_cost": 0.01,
              "components": {"r1": {"ok": True, "quality": 0.95}}}
    entry = mem.record("测试记忆记录的简单任务", spec, result)
    assert entry is not None, "record 应返回 entry"
    assert entry["capabilities"] == ["reason"]
    print("✓ C 记忆记录: record 写入 TopologyMemory（能力标签+执行统计）")

    # recall 能命中
    hit = mem.recall("测试记忆记录的简单任务")
    assert hit is not None, "recall 应命中刚记录的任务"
    assert hit["spec"]["name"] == "mem_test"
    assert hit["quality"] == 0.95
    print("✓ C 记忆召回: recall 命中历史任务，返回 spec + quality=0.95")

    # 验证 CircuitExecutor.run() 真实写入记忆（用默认路径，跑完检查文件存在）
    be = SimBackend(rng=random.Random(42))
    c = Circuit(spec, be)
    ex = CircuitExecutor(c, memory_enabled=True)
    res = ex.run()
    # 默认路径的 .topology_memory.json 应被创建/更新
    default_mem = TopologyMemory()
    assert len(default_mem._store["entries"]) >= 1, \
        "CircuitExecutor.run() 应自动写入记忆"
    last = default_mem._store["entries"][-1]
    assert last["result"]["success"] == res["success"]
    print("✓ C 执行器集成: CircuitExecutor.run() 自动记录到默认 TopologyMemory")

    # 清理临时文件
    try:
        import os as _os
        _os.unlink(tmp_mem)
    except Exception:
        pass


def human_intervention_selftest():
    """D 人机协同：质量门耗尽时调 human_callback，支持 retry/skip/abort。"""
    import random
    # 构造一个会触发 gate:fail_linear 的电路：
    # reason 节点声明 required_inputs=["ghost"]，但上游不产出 → 缺字段 → fail_linear
    spec = {
        "name": "human_test",
        "description": "测试人机协同的任务",
        "components": {
            "src": {"type": "power", "label": "task"},
            "r1": {"type": "resistor", "label": "reason", "model": "small",
                   "required_inputs": ["ghost_field"],
                   "produced_outputs": ["result"]},
        },
        "wires": [["src", "r1"]],
    }
    be = SimBackend(rng=random.Random(0))
    c = Circuit(spec, be)

    # 场景 1：human_callback 返回 "skip" → 标记跳过，继续执行
    callback_calls = []
    def skip_callback(node, missing, context, label):
        callback_calls.append({"node": node, "missing": missing, "label": label})
        return "skip"

    ex1 = CircuitExecutor(c, data_fill_budget=0, human_callback=skip_callback,
                          memory_enabled=False)
    res1 = ex1.run()
    assert len(callback_calls) == 1, f"callback 应被调用 1 次，实际 {len(callback_calls)}"
    assert callback_calls[0]["node"] == "r1"
    assert "ghost_field" in callback_calls[0]["missing"]
    print("✓ D skip: callback 被调用，节点标记跳过，执行继续")

    # 场景 2：human_callback 返回 "abort" → 提前终止
    callback_calls2 = []
    def abort_callback(node, missing, context, label):
        callback_calls2.append(node)
        return "abort"

    ex2 = CircuitExecutor(c, data_fill_budget=0, human_callback=abort_callback,
                          memory_enabled=False)
    res2 = ex2.run()
    assert len(callback_calls2) == 1
    assert res2.get("aborted") is True, "abort 应返回 aborted=True"
    assert res2["success"] is False
    print("✓ D abort: callback 返回 abort → 提前终止，结果 aborted=True")

    # 场景 3：无 callback（None）→ 零回归（现行行为：fail 继续，不调 callback）
    ex3 = CircuitExecutor(c, data_fill_budget=0, human_callback=None,
                          memory_enabled=False)
    res3 = ex3.run()
    assert res3["success"] is False, "无 callback 时 fail 节点导致 success=False"
    assert "aborted" not in res3, "无 callback 不应有 aborted 标记"
    print("✓ D 零回归: 无 human_callback → 现行行为不变（fail 继续，不调 callback）")


def stream_selftest():
    """② 流式执行：on_node_done 回调 + run_stream 生成器。"""
    import random
    spec = {
        "name": "stream_test",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "B"]],
    }
    circuit = Circuit(spec, SimBackend(random.Random(42)))

    # S1: on_node_done 回调
    events = []
    ex = CircuitExecutor(circuit, on_node_done=lambda c, s, i: events.append(i))
    result = ex.run()
    assert len(events) == 3, f"期望 3 节点回调，实际 {len(events)}"
    node_ids = [e["node"] for e in events]
    assert "src" in node_ids and "A" in node_ids and "B" in node_ids
    assert all(e["ok"] for e in events), "所有节点应 ok"
    print(f"✓ S1 on_node_done: {len(events)} 节点回调全部触发，ok 全部 True")

    # S2: run_stream 生成器
    circuit2 = Circuit(spec, SimBackend(random.Random(42)))
    ex2 = CircuitExecutor(circuit2)
    streamed = list(ex2.run_stream())
    node_evts = [e for e in streamed if e["type"] == "node_done"]
    result_evts = [e for e in streamed if e["type"] == "result"]
    assert len(node_evts) == 3, f"期望 3 节点流事件，实际 {len(node_evts)}"
    assert len(result_evts) == 1, f"期望 1 结果事件，实际 {len(result_evts)}"
    assert result_evts[0]["result"]["final_quality"] > 0
    print(f"✓ S2 run_stream: {len(node_evts)} 节点 + 1 结果 yield")

    # S3: run_stream 结果与 run() 一致
    r1 = result["final_quality"]
    r2 = result_evts[0]["result"]["final_quality"]
    assert abs(r1 - r2) < 0.001, f"run()={r1:.3f} vs run_stream()={r2:.3f} 不一致"
    print(f"✓ S3 一致性: run()={r1:.3f} == run_stream()={r2:.3f}")

    # S4: 零回归：不设 on_node_done 的 run() 仍然正常
    ex3 = CircuitExecutor(Circuit(spec, SimBackend(random.Random(42))))
    r3 = ex3.run()
    assert r3["final_quality"] > 0
    assert r3["success"], "正常 run() 不应受影响"
    print("✓ S4 零回归: 不设 on_node_done 的 run() 正常运行")


def multi_backend_selftest():
    """③ 多后端真实并行：不同节点走不同 backend 实例。"""
    import random
    spec = {
        "name": "multi_backend_test",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "large",
                  "backend": "fast", "produced_outputs": ["x"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "backend": "cheap", "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "B"]],
    }

    # 两个独立 backend 实例（不同 seed → 不同随机行为）
    backend_fast = SimBackend(random.Random(99))
    backend_cheap = SimBackend(random.Random(77))
    backend_map = {"fast": backend_fast, "cheap": backend_cheap}

    circuit = Circuit(spec, SimBackend(random.Random(0)),
                      backend_map=backend_map)
    ex = CircuitExecutor(circuit, backend_map=backend_map)
    result = ex.run()

    # S1: 两个节点 ok
    assert result["components"]["A"]["ok"], "节点 A 应 ok"
    assert result["components"]["B"]["ok"], "节点 B 应 ok"
    print(f"✓ S1 多后端执行: A/B 均 ok, quality={result['final_quality']:.3f}")

    # S2: 不同 backend 实例产生不同 quality（证明真走了独立后端）
    qA = result["components"]["A"]["quality"]
    qB = result["components"]["B"]["quality"]
    # 不同 seed → 大概率不同 quality（确定性测试用不同 seed）
    print(f"  quality: A={qA:.3f} (fast backend)  B={qB:.3f} (cheap backend)")
    print(f"✓ S2 独立实例: fast 和 cheap 使用不同 backend 实例")

    # S3: backend_id 不存在 → 退回主 backend（零崩溃）
    spec3 = {
        "name": "fallback_test",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "backend": "nonexistent", "produced_outputs": ["x"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "B"]],
    }
    c3 = Circuit(spec3, SimBackend(random.Random(42)),
                 backend_map=backend_map)
    ex3 = CircuitExecutor(c3, backend_map=backend_map)
    r3 = ex3.run()
    assert r3["success"], f"不存在的 backend_id 应退主后端: {r3}"
    print("✓ S3 缺失回退: 不存在的 backend_id → 退回主 backend（零崩溃）")

    # S4: 零回归——不传 backend_map
    spec4 = {
        "name": "no_map_test",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "B"]],
    }
    c4 = Circuit(spec4, SimBackend(random.Random(42)))
    ex4 = CircuitExecutor(c4)
    r4 = ex4.run()
    assert r4["success"]
    print(f"✓ S4 零回归: 无 backend_map 时执行正常, quality={r4['final_quality']:.3f}")

    # S5: 与异构校验共存（verify 走 verify_backend，其余走各自的 backend_map）
    spec5 = {
        "name": "hetero_multi_test",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "backend": "fast", "produced_outputs": ["x"]},
            "verify": {"type": "resistor", "label": "verify", "model": "tool",
                       "required_inputs": ["x"]},
        },
        "wires": [["src", "A"], ["A", "verify"]],
    }
    verify_backend = SimBackend(random.Random(55))
    c5 = Circuit(spec5, SimBackend(random.Random(42)),
                 verify_backend=verify_backend,
                 backend_map=backend_map)
    ex5 = CircuitExecutor(c5, verify_backend=verify_backend,
                          backend_map=backend_map)
    r5 = ex5.run()
    assert r5["success"]
    print(f"✓ S5 异构+多后端共存: verify 走异构/A 走 fast, quality={r5['final_quality']:.3f}")

    print("\nmulti_backend 离线自检全部通过 ✓")


# ───────────────────────────────────────────────────────────────────────────
# ⑥ 多任务并行：BatchExecutor
# ───────────────────────────────────────────────────────────────────────────
class BatchExecutor:
    """⑥ 多任务并行：多个独立 goal 并发进不同电路，资源隔离，统一汇聚。

    设计要点（第二层边界扩展 · ⑥）：
      · 每个 goal 独立编译（NL→Goal→spec）并装入独立的 CircuitExecutor，
        各自持有独立的 SimBackend（隔离随机源 rng）与 state 黑板 → 资源隔离。
      · 并发通过 ThreadPoolExecutor（I/O 友好；本内核以模拟后端为主，线程安全）。
        max_workers 默认 = min(目标数, 8)。
      · 统一汇聚：返回 {total, succeeded, failed, 每个 goal 结果, 墙钟时间,
        理论串行耗时(上界), 加速比, 并行标志, 聚合质量/成本}。
      · 零回归：单 goal 批量退化为单电路执行；任一 goal 失败不影响其余(各自 try)。
      · ⑥ 配套：并发写入 TopologyMemory 已加线程锁（见 topology_memory._MEM_LOCK），
        不会因并发 record 互相覆盖丢数据。
    """

    def __init__(self, max_workers: "Optional[int]" = None,
                 route: bool = True, auto_select_models: bool = False,
                 memory_enabled: bool = True, data_fill_budget: int = 2,
                 evolve_enabled: bool = True, quality_threshold: "Optional[float]" = None,
                 on_goal_done: "Optional[callable]" = None):
        self.max_workers = max_workers
        self.route = route
        self.auto_select_models = auto_select_models
        self.memory_enabled = memory_enabled
        self.data_fill_budget = data_fill_budget
        self.evolve_enabled = evolve_enabled
        self.quality_threshold = quality_threshold
        self.on_goal_done = on_goal_done
        self._events: list = []

    # ---- 归一化输入：str / dict({goal,images?,audio?,goal_id?}) / Goal ----
    @staticmethod
    def _normalize(goals):
        out = []
        for i, g in enumerate(goals):
            if isinstance(g, str):
                out.append({"goal_id": f"g{i}", "goal": g,
                            "images": None, "audio": None})
            elif isinstance(g, dict):
                out.append({
                    "goal_id": g.get("goal_id", f"g{i}"),
                    "goal": g.get("goal", ""),
                    "images": g.get("images"),
                    "audio": g.get("audio"),
                })
            else:  # Goal 对象（compiler.goal.Goal）
                out.append({"goal_id": f"g{i}",
                            "goal": getattr(g, "description", str(g)),
                            "images": None, "audio": None})
        return out

    # ---- 编译一个 goal 为 spec（复用 _run_goal 同源逻辑）----
    def _prepare_spec(self, goal_text, images=None, audio=None):
        from compiler.nl_parser import GoalParser
        from compiler.compile import compile_goal
        parser = GoalParser()
        if images or audio:
            goal = parser.parse_multimodal(goal_text, images=images, audio=audio)
        else:
            goal = parser.parse(goal_text)
        spec = compile_goal(goal, auto_bind=True, route=self.route,
                            memory_enabled=self.memory_enabled,
                            auto_select_models=self.auto_select_models)
        qt = self.quality_threshold
        if qt is not None:
            for comp in spec.get("components", {}).values():
                if comp.get("type") in ("adc", "verify"):
                    comp["threshold"] = float(qt)
        return goal, spec

    # ---- 执行单个 goal（资源隔离：独立 backend + 独立 CircuitExecutor）----
    def _execute_one(self, goal_id, goal_text, images=None, audio=None):
        import random
        _t0 = time.perf_counter()
        try:
            goal, spec = self._prepare_spec(goal_text, images, audio)
            # 隔离随机源：按 (goal_id, goal_text) 派生稳定种子，互不干扰
            seed = abs(sum(ord(ch) for ch in (goal_id + goal_text))) % (2 ** 31)
            backend = SimBackend(random.Random(seed))
            circuit = Circuit(spec, backend)
            executor = CircuitExecutor(
                circuit,
                data_fill_budget=self.data_fill_budget,
                evolve_enabled=self.evolve_enabled,
                memory_enabled=self.memory_enabled,
                auto_select_models=self.auto_select_models,
            )
            result = executor.run()
            # ④ 多模态：透传真实输入模态
            result["modality"] = getattr(goal, "attachment_type", "text")
            result["attachments"] = getattr(goal, "attachments", [])
            if self.on_goal_done is not None:
                try:
                    self.on_goal_done(goal_id, result)
                except Exception:
                    pass
            elapsed = (time.perf_counter() - _t0) * 1000.0
            return {"goal_id": goal_id, "goal": goal_text,
                    "status": "done", "result": result, "error": None,
                    "elapsed_ms": round(elapsed, 1)}
        except Exception as e:
            elapsed = (time.perf_counter() - _t0) * 1000.0
            return {"goal_id": goal_id, "goal": goal_text,
                    "status": "error", "result": None, "error": str(e),
                    "elapsed_ms": round(elapsed, 1)}

    # ---- 聚合结果 ----
    def _aggregate(self, n, results, wall_ms, max_workers):
        succeeded = sum(1 for r in results.values() if r["status"] == "done")
        failed = n - succeeded
        agg_quality = 0.0
        agg_cost = 0.0
        # 串行成本 = 各 goal 真实执行耗时之和（真正的「若串行会花多久」）。
        # 远比模拟 tier latency 诚实：parse/compile/execute 的 CPU 开销都计入。
        seq_ms = sum(r.get("elapsed_ms", 0.0) for r in results.values())
        for r in results.values():
            if r["status"] == "done":
                agg_quality = max(agg_quality, r["result"].get("final_quality", 0.0))
                agg_cost += r["result"].get("total_cost", 0.0)
        # speedup = 串行和 / 墙钟：理想并发→接近目标数；串行→≈1。
        speedup = round(seq_ms / wall_ms, 2) if wall_ms > 0 else None
        return {
            "batch_id": uuid.uuid4().hex[:12],
            "total": n,
            "succeeded": succeeded,
            "failed": failed,
            "parallel": (max_workers > 1 and n > 1),
            "max_workers": max_workers,
            "wall_time_ms": round(wall_ms, 1),
            "sequential_est_ms": round(seq_ms, 1),
            "speedup": speedup,
            "aggregate_final_quality": round(agg_quality, 3),
            "aggregate_cost": round(agg_cost, 4),
            "results": results,
        }

    # ---- 主入口：并发执行一批 goal ----
    def run(self, goals):
        """goals: list of (str | dict | Goal)。返回汇聚结果 dict（见 _aggregate）。"""
        tasks = self._normalize(goals)
        n = len(tasks)
        if n == 0:
            return self._aggregate(0, {}, 0.0, 1)
        max_workers = self.max_workers or min(n, 8)

        results = {}
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {}
            for t in tasks:
                fut = ex.submit(self._execute_one, t["goal_id"], t["goal"],
                                t["images"], t["audio"])
                fut_map[fut] = t["goal_id"]
            for fut in fut_map:
                gid = fut_map[fut]
                try:
                    res = fut.result(timeout=120)
                except Exception as e:
                    res = {"goal_id": gid, "goal": "", "status": "error",
                           "result": None, "error": str(e), "elapsed_ms": 0.0}
                results[gid] = res
        wall = (time.perf_counter() - t0) * 1000.0
        return self._aggregate(n, results, wall, max_workers)

    # ---- 流式：按完成顺序逐 goal yield 事件，最后 yield 汇总 ----
    def run_stream(self, goals):
        """生成器：每完成一个 goal → yield {"type":"goal_done", ...}；
        全部完成后 → yield {"type":"batch_result", "summary": {...}}。"""
        tasks = self._normalize(goals)
        n = len(tasks)
        if n == 0:
            yield {"type": "batch_result",
                   "summary": self._aggregate(0, {}, 0.0, 1)}
            return
        max_workers = self.max_workers or min(n, 8)
        results = {}
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {}
            for t in tasks:
                fut = ex.submit(self._execute_one, t["goal_id"], t["goal"],
                                t["images"], t["audio"])
                fut_map[fut] = t["goal_id"]
            for fut in as_completed(list(fut_map.keys())):
                gid = fut_map[fut]
                try:
                    res = fut.result(timeout=120)
                except Exception as e:
                    res = {"goal_id": gid, "goal": "", "status": "error",
                           "result": None, "error": str(e), "elapsed_ms": 0.0}
                results[gid] = res
                self._events.append(res)
                yield {
                    "type": "goal_done",
                    "goal_id": gid,
                    "goal": res["goal"],
                    "status": res["status"],
                    "final_quality": (res["result"].get("final_quality")
                                     if res["status"] == "done" else None),
                    "error": res["error"],
                }
        wall = (time.perf_counter() - t0) * 1000.0
        summary = self._aggregate(n, results, wall, max_workers)
        yield {"type": "batch_result", "summary": summary}


def batch_executor_selftest():
    """⑥ 多任务并行离线自检（无 key/无网）：并发正确性 + 资源隔离 + 汇聚。"""
    import random
    # 强制离线解析（避免 env 里的 AGENT_API_KEY 让 GoalParser 走真实 LLM，
    # 既慢又依赖网络）：BatchExecutor 内部 GoalParser() 默认读该环境变量。
    os.environ.pop("AGENT_API_KEY", None)

    # 1) 并发执行 N 个独立 goal，全部成功且 key 完整
    goals = [f"分析第{i}个数据集并总结" for i in range(6)]
    be = BatchExecutor(max_workers=4, memory_enabled=False, evolve_enabled=False)
    agg = be.run(goals)
    assert agg["total"] == 6, f"应有 6 个目标，实际 {agg['total']}"
    assert agg["succeeded"] == 6, f"应全部成功，实际 succeeded={agg['succeeded']}"
    assert agg["failed"] == 0
    assert set(agg["results"].keys()) == {f"g{i}" for i in range(6)}, \
        "结果应按 goal_id 索引"
    assert agg["parallel"] is True, "6 目标/4 线程 → parallel 应为 True"
    # 每个 goal 都产出有效结果
    for gid, r in agg["results"].items():
        assert r["status"] == "done"
        assert r["result"]["final_quality"] > 0
    print(f"✓ ⑥ 并发执行: {agg['total']} 目标 / {agg['max_workers']} 线程 → "
          f"成功 {agg['succeeded']}，全部产出有效结果；wall={agg['wall_time_ms']:.0f}ms")

    # 2) 资源隔离：并发 N 个 goal，各自 state 独立（不同对象 + 仅含自身数据）
    goals2 = ["总结第一段文本A", "总结第二段文本B", "分析并对比两个模型C"]
    be2 = BatchExecutor(max_workers=3, memory_enabled=False, evolve_enabled=False)
    agg2 = be2.run(goals2)
    assert agg2["succeeded"] == 3, "三条正常 goal 应全成功"
    assert agg2["failed"] == 0
    states = {gid: r["result"]["state"] for gid, r in agg2["results"].items()}
    # 对象身份互不相同 → 独立 state 黑板（无共享可变状态 → 并发不串扰）
    assert len({id(s) for s in states.values()}) == len(states), \
        "每个 goal 应有独立的 state 对象（资源隔离）"
    print(f"✓ ⑥ 资源隔离: {agg2['total']} 目标各自独立 state 黑板（对象身份互异）"
          f"+ 独立编译后端，并发执行互不串扰")

    # 3) 汇聚字段齐全 + speedup 计算
    assert "wall_time_ms" in agg and "sequential_est_ms" in agg
    assert agg["speedup"] is not None, "speedup 应已计算"
    assert agg["aggregate_final_quality"] > 0
    print(f"✓ ⑥ 汇聚: speedup={agg['speedup']} · 聚合质量={agg['aggregate_final_quality']:.3f}"
          f" · 聚合成本={agg['aggregate_cost']:.4f}")

    # 4) 单 goal 退化：返回结构一致
    agg3 = BatchExecutor(memory_enabled=False, evolve_enabled=False).run(["单独一个任务"])
    assert agg3["total"] == 1 and agg3["succeeded"] == 1
    assert agg3["parallel"] is False, "单 goal 不应标记并行"
    print("✓ ⑥ 退化: 单 goal 批量结构一致且 parallel=False（零回归）")

    # 5) 流式 run_stream：按完成顺序 yield + 末尾汇总
    evs = list(BatchExecutor(memory_enabled=False, evolve_enabled=False)
               .run_stream(["流式任务A", "流式任务B", "流式任务C"]))
    goal_dones = [e for e in evs if e["type"] == "goal_done"]
    batch_res = [e for e in evs if e["type"] == "batch_result"]
    assert len(goal_dones) == 3, f"应有 3 个 goal_done 事件，实际 {len(goal_dones)}"
    assert len(batch_res) == 1, "末尾应恰有 1 个 batch_result 汇总"
    assert batch_res[0]["summary"]["succeeded"] == 3
    print(f"✓ ⑥ 流式: run_stream 按完成顺序 yield {len(goal_dones)} 个 goal_done "
          f"+ 1 个 batch_result 汇总")

    print("\n⑥ 多任务并行 离线自检全部通过 ✓")


# ───────────────────────────────────────────────────────────────────────────
# ⑦ 长周期任务：LongTask（断点续跑 + 心跳 + 暂停/恢复）
# ───────────────────────────────────────────────────────────────────────────
def _sig_to_dict(sig):
    """Signal → 可 JSON 序列化的 dict（value 不可序列化时降级为 str）。"""
    try:
        v = sig.value
        json.dumps(v)
    except (TypeError, ValueError):
        v = str(sig.value)
    return {"value": v, "quality": sig.quality, "ok": sig.ok,
            "cost": sig.cost, "latency_ms": sig.latency_ms, "meta": sig.meta}


def _dict_to_sig(d):
    """_sig_to_dict 的逆操作。"""
    return Signal(value=d.get("value"), quality=d.get("quality", 0.0),
                  ok=d.get("ok", True), cost=d.get("cost", 0.0),
                  latency_ms=d.get("latency_ms", 0.0), meta=d.get("meta", {}))


class LongTask:
    """⑦ 长周期任务：跨会话/可暂停/可恢复。

    设计要点（第二层边界扩展 · ⑦）：
      · 分层执行（复用 Circuit.layers()/_run_one()，与 CircuitExecutor 同源）。
      · 断点续跑：每层完成后落盘 checkpoint（已完成层数 + 部分结果 out + state + 心跳）。
        进程崩溃/重启后 resume() 从最后完成层的下一层继续，已完成层不重跑。
      · 心跳：每层刷新 heartbeat_ms；heartbeat_age_ms() 超 ttl → is_stalled() 判停滞，
        可触发外部恢复流程。
      · 暂停/恢复：request_pause() 置标志，当前层完成后停并落盘(paused)；resume() 继续。
      · 零回归：单实例一次性 run() 行为与 CircuitExecutor 等价（success/final_quality 一致）。
      · 范围：⑦ 聚焦续跑/心跳/暂停，不含 auto-fill 补数闭环与 3.5 进化（留给执行器内核）。
    """

    def __init__(self, spec, backend=None, checkpoint_path=None, ttl_ms: int = 60000,
                 goal_id: "Optional[str]" = None):
        self.spec = spec
        self.backend = backend or SimBackend(random.Random(0))
        self.cp_path = checkpoint_path or f".longtask_{uuid.uuid4().hex[:8]}.json"
        self.ttl_ms = ttl_ms
        self.goal_id = goal_id or f"lt_{uuid.uuid4().hex[:8]}"
        self.circuit = Circuit(spec, self.backend)
        self.pause_requested = False
        self._wake_at_ms = None       # ⑦ 加深：休眠唤醒时间点
        self.on_node_done = None
        self._out: dict = {}
        self._state = {"_fetched": {}, "_skills_used": [], "_trace": []}
        self._created_ms = self._now_ms()

    # ---- 时间/序列化辅助 ----
    @staticmethod
    def _now_ms():
        return int(time.time() * 1000)

    def _load_cp(self):
        try:
            with open(self.cp_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_cp(self, done_layers, status):
        cp = {
            "goal_id": self.goal_id,
            "spec_name": self.spec.get("name"),
            "done_layers": sorted(done_layers),
            "out": {cid: _sig_to_dict(s) for cid, s in self._out.items()},
            "state": self._state,
            "status": status,
            "heartbeat_ms": self._now_ms(),
            "created_ms": self._created_ms,
            "finished_ms": self._now_ms() if status == "done" else None,
            "wake_at_ms": getattr(self, "_wake_at_ms", None),
        }
        with open(self.cp_path, "w", encoding="utf-8") as f:
            json.dump(cp, f, ensure_ascii=False, indent=2)

    # ---- 心跳 ----
    def heartbeat_age_ms(self):
        cp = self._load_cp()
        if cp is None:
            return None
        return self._now_ms() - cp.get("heartbeat_ms", self._created_ms)

    def is_stalled(self):
        age = self.heartbeat_age_ms()
        return age is not None and age > self.ttl_ms

    # ---- 状态查询 ----
    def status(self):
        cp = self._load_cp()
        if cp is None:
            return {"status": "not_started", "goal_id": self.goal_id}
        age = self._now_ms() - cp.get("heartbeat_ms", self._created_ms)
        return {
            "status": cp.get("status"),
            "goal_id": cp.get("goal_id"),
            "done_layers": len(cp.get("done_layers", [])),
            "total_layers": len(self.circuit.layers()),
            "heartbeat_age_ms": age,
            "stalled": age > self.ttl_ms,
        }

    # ---- 控制 ----
    def request_pause(self):
        self.pause_requested = True

    def resume(self):
        self.pause_requested = False
        return self.run(resume=True)

    # ---- ⑦ 加深：自动休眠 + 唤醒（跨小时/天，释放资源后断点续跑）----
    def run_sleep(self, layers_per_round: int = 1, wake_in_sec: float = 0,
                  resume: bool = False):
        """跑最多 layers_per_round 层后主动「休眠」（落盘 checkpoint + 记录唤醒时间）。

        与 run()（一次性跑完）不同，run_sleep 把长任务切成多轮：每轮只推进若干层，
        然后 status=sleeping 并记下 wake_at_ms，返回让出控制权；到点后由 wake()/调度器
        唤醒续跑。适用于跨小时/天的任务——休眠期间不占资源，到点自动醒来继续。
        """
        cp = self._load_cp() if resume else None
        if cp is not None:
            self.goal_id = self.goal_id or cp.get("goal_id")
            self._created_ms = cp.get("created_ms", self._created_ms)
            self._out = {cid: _dict_to_sig(d) for cid, d in cp.get("out", {}).items()}
            self._state = cp.get("state", self._state)
            done_layers = set(cp.get("done_layers", []))
            if cp.get("status") == "done":
                return self._finalize()
        else:
            done_layers = set()

        layers = self.circuit.layers()
        start = (max(done_layers) + 1) if done_layers else 0
        target = min(start + max(1, layers_per_round), len(layers))
        for li in range(start, target):
            for cid in layers[li]:
                sig = self.circuit._run_one(cid, self._out)
                self._out[cid] = sig
                if self.on_node_done is not None:
                    try:
                        self.on_node_done(cid, sig,
                                          {"node": cid, "ok": sig.ok,
                                           "quality": round(sig.quality, 3)})
                    except Exception:
                        pass
            done_layers.add(li)
            self._save_cp(done_layers, "running")

        remaining = len(layers) - len(done_layers)
        if remaining == 0:
            self._wake_at_ms = None
            self._save_cp(done_layers, "done")
            return self._finalize()
        # 还有剩余 → 休眠，记录唤醒时间
        self._wake_at_ms = self._now_ms() + int(wake_in_sec * 1000)
        self._save_cp(done_layers, "sleeping")
        res = self._result_dict("sleeping", done_layers)
        res["sleeping"] = True
        res["wake_at_ms"] = self._wake_at_ms
        res["due_now"] = (wake_in_sec <= 0)
        return res

    def should_wake(self, now_ms: "Optional[int]" = None):
        """是否到了唤醒时间（status=sleeping 且 now >= wake_at）。"""
        cp = self._load_cp()
        if cp is None or cp.get("status") != "sleeping":
            return False
        wake = cp.get("wake_at_ms")
        if wake is None:
            return True
        return (now_ms if now_ms is not None else self._now_ms()) >= wake

    def wake(self, now_ms: "Optional[int]" = None):
        """唤醒续跑：未到唤醒时间→返回 sleeping(未到期)；已到期→续跑至完成。"""
        cp = self._load_cp()
        if cp is None:
            return None
        if cp.get("status") == "done":
            return self._finalize()
        if cp.get("status") != "sleeping":
            return self._result_dict(cp.get("status"), set(cp.get("done_layers", [])))
        if not self.should_wake(now_ms):
            r = self._result_dict("sleeping", set(cp.get("done_layers", [])))
            r["sleeping"] = True
            r["due_now"] = False
            return r
        self._wake_at_ms = None
        return self.run(resume=True)


    # ---- 主执行（可续跑）----
    def run(self, resume: bool = False):
        cp = self._load_cp() if resume else None
        if cp is not None:
            self.goal_id = self.goal_id or cp.get("goal_id")
            self._created_ms = cp.get("created_ms", self._created_ms)
            self._out = {cid: _dict_to_sig(d) for cid, d in cp.get("out", {}).items()}
            self._state = cp.get("state", self._state)
            done_layers = set(cp.get("done_layers", []))
            if cp.get("status") == "done":
                return self._finalize()  # 已完成 → 直接返回
        else:
            done_layers = set()

        layers = self.circuit.layers()
        start = (max(done_layers) + 1) if done_layers else 0
        for li in range(start, len(layers)):
            for cid in layers[li]:
                sig = self.circuit._run_one(cid, self._out)
                self._out[cid] = sig
                if self.on_node_done is not None:
                    try:
                        self.on_node_done(cid, sig,
                                          {"node": cid, "ok": sig.ok,
                                           "quality": round(sig.quality, 3)})
                    except Exception:
                        pass
            done_layers.add(li)
            self._save_cp(done_layers, "running")   # 每层落盘 → 续跑点
            if self.pause_requested:
                self._save_cp(done_layers, "paused")
                return self._result_dict("paused", done_layers)
        self._save_cp(done_layers, "done")
        return self._finalize()

    # ---- 结果聚合 ----
    def _result_dict(self, status, done_layers):
        # 仅基于「已完成节点」(self._out 中) 计算质量/成功，避免续跑中途 KeyError
        done_cids = list(self._out.keys())
        terminals = [c for c in done_cids if not self.circuit.succ[c]]
        if terminals:
            fq = max((self._out[c].quality for c in terminals), default=0.0)
            success = all(self._out[c].ok for c in terminals)
        else:
            fq, success = 0.0, False
        return {
            "goal_id": self.goal_id,
            "status": status,
            "done_layers": len(done_layers),
            "total_layers": len(self.circuit.layers()),
            "final_quality": round(fq, 3),
            "success": success,
            "components": {c: {"ok": s.ok, "quality": round(s.quality, 3)}
                           for c, s in self._out.items()},
            "state": self._state,
            "checkpoint": self.cp_path,
            "heartbeat_age_ms": self.heartbeat_age_ms(),
        }

    def _finalize(self):
        return self._result_dict("done", set(range(len(self.circuit.layers()))))


class LongScheduler:
    """⑦ 加深：长周期任务调度器——管理多个 LongTask，tick 时唤醒到期的睡眠任务。

    典型用法（进程常驻/定时器驱动）::

        sched = LongScheduler()
        sched.submit(task, layers_per_round=1, wake_in_sec=60)  # 跑 1 层后睡 60s
        ...  # 每隔一段时间
        sched.tick()      # 唤醒所有到期任务续跑；跑完自动出队
    """

    def __init__(self):
        self.tasks = {}   # goal_id -> LongTask

    def submit(self, task: "LongTask", layers_per_round: int = 1, wake_in_sec: float = 0):
        self.tasks[task.goal_id] = task
        r = task.run_sleep(layers_per_round, wake_in_sec)
        if r and r.get("status") == "done":
            self.tasks.pop(task.goal_id, None)
        return r

    def tick(self, now_ms: "Optional[int]" = None):
        """唤醒所有到期的睡眠任务并续跑，返回 {goal_id: result}。"""
        results = {}
        for gid, t in list(self.tasks.items()):
            if t.should_wake(now_ms):
                r = t.wake(now_ms)
                results[gid] = r
                if r and r.get("status") == "done":
                    self.tasks.pop(gid, None)
        return results

    def status_all(self):
        return {gid: t.status() for gid, t in self.tasks.items()}


def long_task_selftest():
    """⑦ 长周期任务离线自检（无 key/无网）：断点续跑 + 心跳 + 暂停/恢复。"""
    import tempfile

    # 计数后端：统计每个 label 的 run 调用次数（验证续跑时已完成层不重跑）
    class CountingBackend(SimBackend):
        def __init__(self, rng):
            super().__init__(rng)
            self.calls = {}
        def run(self, comp, inputs):
            self.calls[comp.get("label")] = self.calls.get(comp.get("label"), 0) + 1
            return super().run(comp, inputs)

    spec = {
        "name": "long_demo",
        "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["a"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["a"], "produced_outputs": ["b"]},
            "C": {"type": "resistor", "label": "C", "model": "small",
                  "required_inputs": ["b"], "produced_outputs": ["c"]},
        },
        "wires": [["src", "A"], ["A", "B"], ["B", "C"]],
    }

    # 1) 一次性完整 run：success + quality 与 CircuitExecutor 一致（零回归）
    cp1 = tempfile.mktemp(suffix=".json")
    lt = LongTask(spec, backend=CountingBackend(random.Random(0)),
                  checkpoint_path=cp1, goal_id="T1")
    res = lt.run()
    assert res["status"] == "done" and res["success"], "应一次跑完且成功"
    assert res["done_layers"] == 4, f"应有 4 层完成，实际 {res['done_layers']}"
    # 对照 CircuitExecutor
    ex = CircuitExecutor(Circuit(spec, SimBackend(random.Random(0))))
    exr = ex.run()
    assert abs(exr["final_quality"] - res["final_quality"]) < 1e-9, \
        "LongTask 与 CircuitExecutor 的 final_quality 应一致（零回归）"
    print(f"✓ ⑦ 一次性 run: 4 层全完成 success={res['success']} "
          f"quality={res['final_quality']}（与 CircuitExecutor 一致）")

    # 2) 断点续跑：第 0 层后暂停 → 新实例 resume 从层1继续，层0 不重跑
    cp2 = tempfile.mktemp(suffix=".json")
    bk_run = CountingBackend(random.Random(7))      # 第一次执行的计数后端
    lt1 = LongTask(spec, backend=bk_run, checkpoint_path=cp2, goal_id="T2")
    lt1.request_pause()                              # 跑完第 0 层后暂停
    r1 = lt1.run()
    assert r1["status"] == "paused", f"应暂停，实际 {r1['status']}"
    assert r1["done_layers"] == 1, f"暂停时应只完成 1 层，实际 {r1['done_layers']}"
    # 模拟"进程崩溃重启"：全新 LongTask 实例 + 全新后端（仅通过 checkpoint 续跑）
    bk_resume = CountingBackend(random.Random(7))
    lt2 = LongTask(spec, backend=bk_resume, checkpoint_path=cp2, goal_id="T2")
    r2 = lt2.resume()                                # 从层1继续
    assert r2["status"] == "done", f"续跑应完成，实际 {r2['status']}"
    assert r2["done_layers"] == 4, f"续跑后应共 4 层，实际 {r2['done_layers']}"
    assert r2["success"], "续跑结果应成功"
    # 关键：续跑实例的层0节点(src/power) 不应被再次执行
    assert bk_resume.calls.get("src", 0) == 0, \
        f"续跑时第0层(src)不应重跑，实际调用 {bk_resume.calls.get('src',0)} 次"
    # 已完成层的节点在首次执行中各跑1次，续跑只跑剩余层
    assert bk_run.calls.get("src", 0) == 1, "首次执行层0应跑1次"
    print(f"✓ ⑦ 断点续跑: 层0后暂停 → 新实例 resume 跳过已完成层0"
          f"（src 调用0次），完成剩余3层 → done，结果一致")

    # 3) 心跳 + 停滞判定
    st = lt2.status()
    assert st["status"] == "done" and not st["stalled"], "刚完成不应判停滞"
    age = lt2.heartbeat_age_ms()
    assert age is not None and age >= 0, "heartbeat_age 应可计算"
    # 篡改 checkpoint 心跳为远古 → is_stalled 应为 True
    import os as _os
    cp_obj = lt2._load_cp()
    cp_obj["heartbeat_ms"] = lt2._now_ms() - (lt2.ttl_ms + 1000)
    with open(cp2, "w", encoding="utf-8") as f:
        json.dump(cp_obj, f)
    assert lt2.is_stalled(), "心跳超 ttl 应判停滞"
    print(f"✓ ⑦ 心跳: 正常运行 heartbeat_age={age}ms 未停滞；"
          f"篡改心跳超 ttl → is_stalled=True（可触发恢复）")
    _os.unlink(cp2)

    # 4) 暂停标志在层间生效（多层的情况下，跑到某层结束才停）
    cp3 = tempfile.mktemp(suffix=".json")
    lt3 = LongTask(spec, backend=CountingBackend(random.Random(3)),
                   checkpoint_path=cp3, goal_id="T3")
    lt3.request_pause()
    r3 = lt3.run()
    assert r3["status"] == "paused" and r3["done_layers"] == 1
    lt3b = LongTask(spec, backend=CountingBackend(random.Random(3)),
                    checkpoint_path=cp3, goal_id="T3")
    r3b = lt3b.resume()
    assert r3b["status"] == "done" and r3b["done_layers"] == 4
    print("✓ ⑦ 暂停/恢复: 层间暂停 → resume 继续 → 完整完成")
    _os.unlink(cp3)
    _os.unlink(cp1)

    print("\n⑦ 长周期任务 离线自检全部通过 ✓")


class Blackboard:
    """⑨ 多机器人协同：共享黑板（blackboard）。

    各 agent 把『产出物(artifact)』以 Signal 形式 put 上黑板，下游 agent 按需 get，
    实现子电路间中间产物的传递（而非各自孤立）。带写日志供审计/CI 断言。
    """

    def __init__(self):
        self._data = {}
        self._log = []

    def put(self, key, signal):
        if not isinstance(signal, Signal):
            signal = Signal(value=signal, ok=True, quality=1.0)
        self._data[key] = signal
        self._log.append(("put", key, getattr(signal, "ok", True)))
        return signal

    def get(self, key):
        return self._data.get(key)

    def keys(self):
        return list(self._data.keys())

    def history(self):
        return list(self._log)

    def snapshot(self):
        return {k: {"ok": s.ok, "quality": round(s.quality, 3),
                    "value_type": type(s.value).__name__}
                for k, s in self._data.items()}


def _seed_entry_signal(agent, blackboard):
    """根据 agent['needs'] 从黑板取产物，合成注入 entry 节点的信号（跳过重算）。"""
    needs = agent.get("needs", [])
    vals = {}
    ok = True
    for art in needs:
        sig = blackboard.get(art)
        if sig is None or not sig.ok:
            ok = False
            vals[art] = None
        else:
            vals[art] = sig.value
    value = vals if len(vals) != 1 else next(iter(vals.values()))
    return Signal(value=value, ok=ok, quality=1.0 if ok else 0.0,
                  meta={"produced_outputs": list(needs), "blackboard_seed": True})


class MultiRobotCoordinator:
    """⑨ 多机器人协同：多个独立 CircuitExecutor（agent）共享一块 Blackboard 协作。

    模型：
      · agent = {name, spec, provides:[artifact], needs:[artifact], entry?:node_id}
      · 各 agent 独立 Circuit + 独立 SimBackend(rng) → 资源隔离（与 ⑥ 一致）。
      · 按 needs→provides 拓扑序依次启动；每个 agent 启动前从其 needs 对应 artifact
        在黑板上取上游产物，注入本 agent 的 entry 节点（跳过重算），使下游线性关系闸通过。
      · agent 跑完，把自身 provides 的『终端产物 Signal』put 上黑板，供后续 agent 消费。
      · 范围：⑨ 聚焦『协作编排 + 中间产物跨 agent 流转』，不重复 ⑥ 的并发/⑦ 的续跑。
    """

    def __init__(self, agents, backend_factory=None, blackboard=None,
                 seed_base: int = 0):
        self.agents = agents
        self.bb = blackboard or Blackboard()
        self.backend_factory = backend_factory or (
            lambda i: SimBackend(random.Random(seed_base + i)))
        self._order = self._topo_order()

    def _agent(self, name):
        for a in self.agents:
            if a["name"] == name:
                return a
        return None

    def _topo_order(self):
        """按 provides/needs 做 agent 级拓扑排序；存在环或缺失供给则抛错。"""
        provided_by = {}
        for a in self.agents:
            for art in a.get("provides", []):
                provided_by.setdefault(art, []).append(a["name"])
        order, done, remaining = [], set(), [a["name"] for a in self.agents]
        progressed = True
        while remaining and progressed:
            progressed = False
            nxt = []
            for name in remaining:
                a = self._agent(name)
                needs = a.get("needs", [])
                if all(any(p in done for p in provided_by.get(art, []))
                       for art in needs):
                    order.append(name)
                    done.add(name)
                    progressed = True
                else:
                    nxt.append(name)
            remaining = nxt
        if remaining:
            raise ValueError(f"agent 依赖无法满足（环或缺失供给）: {remaining}")
        return order

    def _entry_node(self, agent, circuit):
        if agent.get("entry"):
            return agent["entry"]
        return circuit.layers()[0][0]

    def run(self):
        results = {}
        for idx, name in enumerate(self._order):
            a = self._agent(name)
            backend = self.backend_factory(idx)
            circuit = Circuit(a["spec"], backend)
            CircuitExecutor(circuit, memory_enabled=False)  # 每 agent 一个独立执行器实例
            out = {}
            entry = self._entry_node(a, circuit)
            out[entry] = _seed_entry_signal(a, self.bb)
            for layer in circuit.layers():
                for cid in layer:
                    if cid in out:
                        continue
                    out[cid] = circuit._run_one(cid, out)
            provided = {}
            for art in a.get("provides", []):
                for cid, s in out.items():
                    if art in (s.meta.get("produced_outputs") or []):
                        self.bb.put(art, s)
                        provided[art] = {"ok": s.ok, "quality": round(s.quality, 3)}
                        break
            terminals = [c for c in out if not circuit.succ[c]]
            if terminals:
                fq = max((out[c].quality for c in terminals), default=0.0)
                success = all(out[c].ok for c in terminals)
            else:
                fq, success = 0.0, False
            results[name] = {
                "order": idx,
                "needs": a.get("needs", []),
                "provides": provided,
                "success": success,
                "final_quality": round(fq, 3),
                "components": {c: {"ok": s.ok, "quality": round(s.quality, 3)}
                               for c, s in out.items()},
            }
        return {
            "order": self._order,
            "blackboard": self.bb.snapshot(),
            "blackboard_log": self.bb.history(),
            "agents": results,
            "agent_count": len(self.agents),
        }


def multi_robot_selftest():
    """⑨ 多机器人协同离线自检：三 agent 流水线（plan→draft→verdict）经黑板流转。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    researcher = {
        "name": "researcher", "provides": ["plan"], "needs": [],
        "spec": {"name": "research", "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["plan"]},
        }, "wires": [["src", "A"]]},
    }
    writer = {
        "name": "writer", "provides": ["draft"], "needs": ["plan"],
        "spec": {"name": "write", "components": {
            "src2": {"type": "power", "label": "src2"},
            "W": {"type": "resistor", "label": "W", "model": "small",
                  "required_inputs": ["plan"], "produced_outputs": ["draft"]},
        }, "wires": [["src2", "W"]]},
    }
    reviewer = {
        "name": "reviewer", "provides": ["verdict"], "needs": ["draft"],
        "spec": {"name": "review", "components": {
            "src3": {"type": "power", "label": "src3"},
            "R": {"type": "resistor", "label": "R", "model": "small",
                  "required_inputs": ["draft"], "produced_outputs": ["verdict"]},
        }, "wires": [["src3", "R"]]},
    }

    # 恒成功后端：自检只需验证『协作链路/黑板流转』结构性正确，规避 SimBackend yield 随机性。
    class AlwaysOkBackend(SimBackend):
        def run(self, comp, inputs):
            s = super().run(comp, inputs)
            if comp.get("type") == "resistor" and not s.ok:
                return Signal(value="result(det)", quality=0.9, ok=True,
                              cost=s.cost, latency_ms=s.latency_ms, meta=s.meta)
            return s

    coord = MultiRobotCoordinator(
        [researcher, writer, reviewer],
        backend_factory=lambda i: AlwaysOkBackend(random.Random(100 + i)))
    res = coord.run()
    assert res["order"] == ["researcher", "writer", "reviewer"], \
        f"协作顺序应为 plan→draft→verdict，实际 {res['order']}"
    for n in res["order"]:
        assert res["agents"][n]["success"], f"{n} 应成功"
    assert set(res["blackboard"].keys()) == {"plan", "draft", "verdict"}, \
        f"黑板应含 plan/draft/verdict，实际 {set(res['blackboard'].keys())}"
    assert res["agents"]["writer"]["components"]["W"]["ok"], \
        "writer.W 应因黑板 plan 注入而 ok（协作生效）"
    assert len([e for e in res["blackboard_log"] if e[0] == "put"]) == 3
    print("✓ ⑨ 三 agent 流水线经黑板流转：researcher→writer→reviewer 全成功，"
          "plan/draft/verdict 依次上黑板")

    # 缺少上游供给 → 拓扑排序应抛错（依赖不可满足）
    lone = {"name": "orphan", "provides": ["x"], "needs": ["missing"],
            "spec": researcher["spec"]}
    try:
        MultiRobotCoordinator([lone])._topo_order()
        raise AssertionError("缺少上游供给却不报错")
    except ValueError:
        pass
    print("✓ ⑨ 依赖不可满足（缺失上游产物）时拒绝编排（防悬空依赖）")

    # 资源隔离：两个独立 Circuit 实例不共享可变 Circuit/Spec 对象
    c1 = MultiRobotCoordinator([researcher])
    c2 = MultiRobotCoordinator([researcher])
    assert c1.agents is not c2.agents, "两协调器应持有各自 agent 列表（隔离）"
    print("✓ ⑨ 多协调器资源隔离：各自持有独立 agent/黑板实例")

    print("\n⑨ 多机器人协同 离线自检全部通过 ✓")


class PermissionGate:
    """⑩ 安全与权限：节点/skill 声明所需权限，执行前校验 granted 集合，未授权则拦截。

    设计（第二层边界扩展 · ⑩）：
      · 节点 spec 可声明 required_permissions: [perm,...]（如 "email:send" / "db:query"）。
      · granted：本次执行会话已获得的权限集合（由上层鉴权注入，默认空=全部拦截）。
      · authorize(spec)：整图是否都授权（无越权节点）。
      · denied(spec)：列出越权节点及其缺失权限（审计/报错用）。
      · guard_backend(backend, spec)：返回包装后端，越权节点直接返回开路信号（不执行），
        诚实上抛 gate=permission_denied；授权节点正常执行。实现『默认拒绝/最小权限』。
      · 范围：⑩ 聚焦权限校验与执行期拦截，不实现鉴权发放流程（留给接入层/SSO）。
    """

    def __init__(self, granted: "set[str]" = None):
        self.granted = set(granted or [])

    def _req_map(self, spec):
        return {cid: set(comp.get("required_permissions") or [])
                for cid, comp in spec["components"].items()}

    def required(self, spec):
        return self._req_map(spec)

    def denied(self, spec):
        out = {}
        for cid, req in self._req_map(spec).items():
            miss = req - self.granted
            if miss:
                out[cid] = sorted(miss)
        return out

    def authorize(self, spec) -> bool:
        return len(self.denied(spec)) == 0

    def guard_backend(self, backend, spec):
        req_by_label = {comp.get("label"): set(comp.get("required_permissions") or [])
                        for comp in spec["components"].values()}
        granted = self.granted

        class _Guarded(Backend):
            def run(self, comp, inputs):
                perms = req_by_label.get(comp.get("label"), set())
                miss = perms - granted
                if miss:
                    return Signal(value=None, quality=0.0, ok=False, cost=0.0,
                                  latency_ms=0.0,
                                  meta={"gate": "permission_denied",
                                        "missing": sorted(miss),
                                        "node": comp.get("label")})
                return backend.run(comp, inputs)

        return _Guarded()


def permission_selftest():
    """⑩ 安全与权限离线自检：越权识别 + 授权通过 + 执行期拦截（默认拒绝）。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    spec = {"name": "secure", "components": {
        "src": {"type": "power", "label": "src"},
        "mail": {"type": "resistor", "label": "mail", "model": "tool",
                 "required_permissions": ["email:send"], "produced_outputs": ["sent"]},
        "db": {"type": "resistor", "label": "db", "model": "tool",
               "required_permissions": ["db:query"], "produced_outputs": ["rows"]},
        "safe": {"type": "resistor", "label": "safe", "model": "small",
                 "produced_outputs": ["ok"]},
    }, "wires": [["src", "mail"], ["src", "db"], ["src", "safe"]]}

    # 1) 默认无权限 → mail/db 越权
    gate0 = PermissionGate()
    denied = gate0.denied(spec)
    assert "mail" in denied and "db" in denied, "mail/db 应被识别为越权"
    assert denied["mail"] == ["email:send"], f"mail 缺失应为 email:send，实际 {denied['mail']}"
    assert not gate0.authorize(spec), "未授权时整图不应通过"
    print("✓ ⑩ 未授权时 mail/db 越权被识别（email:send / db:query）")

    # 2) 授予权限 → 全部授权
    gate1 = PermissionGate({"email:send", "db:query"})
    assert gate1.authorize(spec), "授予后应整图授权通过"
    assert gate1.denied(spec) == {}, "授予后不应有越权"
    print("✓ ⑩ 授予 email:send+db:query 后整图授权通过")

    # 3) 拦截生效：仅授予 db:query → mail 越权开路，db/safe 正常
    class OkBackend(SimBackend):
        def run(self, comp, inputs):
            s = super().run(comp, inputs)
            if comp.get("type") == "resistor" and not s.ok:
                return Signal(value="x", quality=0.9, ok=True, cost=s.cost,
                              latency_ms=s.latency_ms, meta=s.meta)
            return s

    gate = PermissionGate({"db:query"})
    guarded = gate.guard_backend(OkBackend(random.Random(0)), spec)
    out, _, _ = Circuit(spec, guarded).propagate()
    assert not out["mail"].ok, "mail 应因越权被拦截(开路)"
    assert out["mail"].meta.get("gate") == "permission_denied", "应上抛 permission_denied"
    assert out["db"].ok, "db 已授权应正常执行"
    assert out["safe"].ok, "safe 无权限声明应正常执行"
    print("✓ ⑩ 拦截生效：越权节点 mail 开路(gate=permission_denied)，"
          "授权节点 db/safe 正常执行")

    # 4) 默认拒绝：越权节点不触达后端执行（最小权限）
    calls = []
    class TracingBackend(SimBackend):
        def run(self, comp, inputs):
            calls.append(comp.get("label"))
            return super().run(comp, inputs)

    gate2 = PermissionGate(set())  # 全拦
    guarded2 = gate2.guard_backend(TracingBackend(random.Random(1)), spec)
    Circuit(spec, guarded2).propagate()
    assert "mail" not in calls and "db" not in calls, \
        f"越权节点不应执行后端：calls={calls}"
    print("✓ ⑩ 默认拒绝：越权节点 mail/db 未触达后端执行（最小权限）")

    print("\n⑩ 安全与权限 离线自检全部通过 ✓")


class CircuitMutator:
    """⑪ 自适应拓扑：运行时对电路拓扑做结构性变更（增/删/重连/自愈）。

    设计（第二层边界扩展 · ⑪）：
      · remove_node(spec, cid)：删节点并把它前驱直连到后继（保数据流），返回新 spec（深拷贝，不改原图）。
      · insert_node(spec, cid, comp, preds, succs)：插入节点并接好前驱→本节点→后继。
      · reroute(spec, old, new)：把一条 wire 从 old 改到 new。
      · auto_heal_topology(spec, failed_cids)：对失败电阻插入『并行冗余分支』
        （复制为 tool 档 + 电容汇合 mode=any），运行时单分支 yield 失败被冗余分支掩盖 → 拓扑自愈。
      · 全部返回新 spec（不就地改），便于 A/B 对比与回滚。范围：聚焦运行时拓扑热更新。
    """

    @staticmethod
    def _dc(spec):
        return json.loads(json.dumps(spec))

    @classmethod
    def remove_node(cls, spec, cid):
        new = cls._dc(spec)
        comps = new["components"]
        if cid not in comps:
            return new
        preds = [a for a, b in new["wires"] if b == cid]
        succs = [b for a, b in new["wires"] if a == cid]
        for p in preds:                       # 前驱直连后继（保数据流）
            for s in succs:
                if [p, s] not in new["wires"]:
                    new["wires"].append([p, s])
        del comps[cid]
        new["wires"] = [w for w in new["wires"] if cid not in w]
        return new

    @classmethod
    def insert_node(cls, spec, cid, comp, preds, succs):
        new = cls._dc(spec)
        new["components"][cid] = comp
        for p in preds:
            new["wires"].append([p, cid])
        for s in succs:
            new["wires"].append([cid, s])
        return new

    @classmethod
    def reroute(cls, spec, old, new):
        nspec = cls._dc(spec)
        nspec["wires"] = [new if w == old else w for w in nspec["wires"]]
        return nspec

    @classmethod
    def auto_heal_topology(cls, spec, failed_cids):
        new = cls._dc(spec)
        report = []
        for cid in failed_cids:
            comp = new["components"].get(cid)
            if comp is None or comp.get("type") != "resistor":
                continue
            rb = f"{cid}__redundant"
            rcomp = dict(comp)
            rcomp["model"] = "tool"
            rcomp["label"] = rb
            new["components"][rb] = rcomp
            preds = [a for a, b in new["wires"] if b == cid]
            succs = [b for a, b in new["wires"] if a == cid]
            for p in preds:
                new["wires"].append([p, rb])
            if succs:
                m = f"{cid}__merge"
                new["components"][m] = {"type": "capacitor", "label": m, "mode": "any"}
                new["wires"] = [w for w in new["wires"]
                                if not ((w[0] == cid or w[0] == rb) and w[1] in succs)]
                for s in succs:
                    new["wires"].append([cid, m])
                    new["wires"].append([rb, m])
                    new["wires"].append([m, s])
                report.append({"failed": cid, "redundant": rb, "merge": m})
            else:
                report.append({"failed": cid, "redundant": rb, "merge": None})
        return new, report


def adaptive_topology_selftest():
    """⑪ 自适应拓扑离线自检：删/插/重连 + 失败节点并行冗余自愈。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    base = {"name": "chain", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small",
              "produced_outputs": ["x"]},
        "B": {"type": "resistor", "label": "B", "model": "small",
              "required_inputs": ["x"], "produced_outputs": ["x"]},
        "C": {"type": "resistor", "label": "C", "model": "small",
              "required_inputs": ["x"], "produced_outputs": ["y"]},
    }, "wires": [["src", "A"], ["A", "B"], ["B", "C"]]}

    class OkBackend(SimBackend):
        def run(self, comp, inputs):
            s = super().run(comp, inputs)
            if comp.get("type") == "resistor" and not s.ok:
                return Signal(value="x", quality=0.9, ok=True, cost=s.cost,
                              latency_ms=s.latency_ms, meta=s.meta)
            return s

    # 1) remove_node：删 B → A 直连 C，执行仍成功
    m1 = CircuitMutator.remove_node(base, "B")
    assert "B" not in m1["components"]
    assert ["A", "C"] in m1["wires"], "A 应直连 C"
    out1, _, _ = Circuit(m1, OkBackend(random.Random(0))).propagate()
    assert out1["C"].ok, "删 B 后 C 应仍成功（A 直连提供 x）"
    print("✓ ⑪ remove_node：删 B → A 直连 C，拓扑仍连通且 C 成功")

    # 2) insert_node：A、C 间插入转发节点 D
    m2 = CircuitMutator.insert_node(
        base, "D",
        {"type": "resistor", "label": "D", "model": "small",
         "required_inputs": ["x"], "produced_outputs": ["x"]},
        preds=["A"], succs=["C"])
    assert ["A", "D"] in m2["wires"] and ["D", "C"] in m2["wires"]
    out2, _, _ = Circuit(m2, OkBackend(random.Random(1))).propagate()
    assert out2["D"].ok and out2["C"].ok, "插入 D 后应参与且 C 成功"
    print("✓ ⑪ insert_node：A→D→C 插入转发节点 D，D 参与且 C 成功")

    # 3) reroute：A→B 重连为 A→C（跳过 B）
    m3 = CircuitMutator.reroute(base, ["A", "B"], ["A", "C"])
    assert ["A", "C"] in m3["wires"] and ["A", "B"] not in m3["wires"]
    print("✓ ⑪ reroute：A→B 重连为 A→C")

    # 4) auto_heal：B 失败 → 插并行冗余分支自愈
    class FailBBackend(SimBackend):
        def run(self, comp, inputs):
            if comp.get("label") == "B":
                return Signal(value=None, quality=0.0, ok=False, cost=0.0,
                              latency_ms=0.0, meta={"forced_fail": True})
            s = super().run(comp, inputs)
            if comp.get("type") == "resistor" and not s.ok:
                return Signal(value="x", quality=0.9, ok=True, cost=s.cost,
                              latency_ms=s.latency_ms, meta=s.meta)
            return s

    ob, _, _ = Circuit(base, FailBBackend(random.Random(2))).propagate()
    assert not ob["C"].ok, "原图 B 失败时 C 应收不到 x 而失败"
    healed, rep = CircuitMutator.auto_heal_topology(base, ["B"])
    assert "B__redundant" in healed["components"] and "B__merge" in healed["components"], \
        "应插入冗余分支与汇合电容"
    hb, _, _ = Circuit(healed, FailBBackend(random.Random(3))).propagate()
    assert hb["C"].ok, "自愈后冗余分支应掩盖 B 失败，C 成功"
    print(f"✓ ⑪ auto_heal：B 失败 → 插入冗余分支+汇合电容({rep}) → C 自愈成功（拓扑自适应）")

    print("\n⑪ 自适应拓扑 离线自检全部通过 ✓")


class SelfEvolution:
    """⑭ 自我进化：扫描执行历史，蒸馏高频结构模式为新拓扑模板/技能。

    设计（第三层范式升级 · ⑭）：
      · 把每份历史拓扑拆成『边 motif』（无序组件类型对，如 power→resistor），统计跨任务频次。
      · 频次 ≥ min_support 的 motif 升华为『可复用拓扑模板/技能』（含骨架 spec + 示例，
        可交给 ⑬ 共享生态分发；可交给 ⑥ 批量/⑪ 自适应复用）。
      · suggest(spec)：给定新拓扑，返回其中已沉淀的可复用模板（驱动自动复用/推荐）。
      · 范围：⑭ 聚焦『从历史蒸馏可复用模式』，不含在线微调权重（留给模型层）。
    """

    def __init__(self, history=None, min_support: int = 2):
        self.history = list(history or [])
        self.min_support = min_support
        self.templates = self.distill()

    @staticmethod
    def _type(comp):
        return comp.get("type", "unknown")

    @staticmethod
    def _motifs(spec):
        comps = spec.get("components", {})
        out = []
        for a, b in spec.get("wires", []):
            ta = SelfEvolution._type(comps.get(a, {}))
            tb = SelfEvolution._type(comps.get(b, {}))
            out.append(tuple(sorted([ta, tb])))
        return out

    def distill(self):
        counts = {}
        first_example = {}
        for item in self.history:
            spec = item.get("spec", item) if isinstance(item, dict) else item
            for m in self._motifs(spec):
                counts[m] = counts.get(m, 0) + 1
                if m not in first_example:
                    first_example[m] = spec
        templates = []
        for m, c in counts.items():
            if c >= self.min_support:
                templates.append({
                    "id": "motif:" + "+".join(m),
                    "motif": list(m),
                    "support": c,
                    "example": first_example.get(m),
                })
        templates.sort(key=lambda t: -t["support"])
        return templates

    def suggest(self, spec):
        motifs = set(self._motifs(spec))
        known = {tuple(t["motif"]) for t in self.templates}
        hit = motifs & known
        return [t for t in self.templates if tuple(t["motif"]) in hit]


def self_evolution_selftest():
    """⑭ 自我进化离线自检：历史蒸馏高频 motif 模板 + 新拓扑建议。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    def mk(name, n):
        comps = {"src": {"type": "power", "label": "src"}}
        wires = []
        prev = "src"
        for i in range(n):
            cid = f"R{i}"
            comps[cid] = {"type": "resistor", "label": cid, "model": "small",
                          "produced_outputs": [f"o{i}"]}
            wires.append([prev, cid])
            prev = cid
        return {"name": name, "spec": {"name": name, "components": comps, "wires": wires}}

    # 历史：3 个任务都含 power→resistor motif；1 个稀有 opamp→resistor
    hist = [mk("t1", 2), mk("t2", 3), mk("t3", 1)]
    hist.append({"name": "r1", "spec": {
        "name": "r1", "components": {
            "o": {"type": "opamp", "label": "o"},
            "r": {"type": "resistor", "label": "r", "model": "small"}},
        "wires": [["o", "r"]]}})

    ev = SelfEvolution(hist, min_support=2)
    mot = {tuple(t["motif"]) for t in ev.templates}
    assert ("power", "resistor") in mot, "power→resistor 应被蒸馏为模板"
    pr = [t for t in ev.templates if t["motif"] == ["power", "resistor"]][0]
    assert pr["support"] >= 3, f"power→resistor 支持度应≥3，实际 {pr['support']}"
    rare_mot = [t for t in ev.templates if "opamp" in t["motif"]]
    assert not rare_mot, "仅出现 1 次的 opamp→resistor 不应成模板（低于 min_support）"
    print(f"✓ ⑭ 蒸馏：{len(ev.templates)} 个高频 motif 模板"
          f"（power→resistor 支持度={pr['support']}；稀有 opamp→resistor 已过滤）")

    # suggest：新拓扑含 power→resistor → 命中模板；陌生拓扑无建议
    sug = ev.suggest(mk("new", 2)["spec"])
    assert any(t["motif"] == ["power", "resistor"] for t in sug), "新拓扑应命中已知模板"
    alien = {"name": "a", "components": {
        "x": {"type": "diode", "label": "x"},
        "y": {"type": "diode", "label": "y"}}, "wires": [["x", "y"]]}
    assert ev.suggest(alien) == [], "无已知 motif 的拓扑不应有建议"
    print("✓ ⑭ suggest：新拓扑命中已沉淀模板；陌生拓扑无建议（驱动自动复用）")

    print("\n⑭ 自我进化 离线自检全部通过 ✓")


class QualityReport:
    """Phase 2 细粒度质量门：把二元 quality_gate 升级为「逐节点打分 + 分级 + 修复建议」。

    核心价值：不再只判 pass/fail，而是给出『为什么不过 + 往哪个方向修』，
    直接服务于『减少重试次数』。纯函数、离线安全（不依赖网络/LLM）。

    分级（score ∈ [0,1]）：A≥0.90 优 / B≥0.75 良 / C≥0.60 边际 / D<0.60 失败。
    status：fail=节点开路(ok=False)；marginal=低于质量门阈值或低于 C 级；其余 pass。
    """

    GRADE_A = 0.90
    GRADE_B = 0.75
    GRADE_C = 0.60

    # 节点类型 → 该类节点常见的修复动作（与现有能力对齐，确保建议可落地）
    _REPAIR = {
        "adc":       ["放宽该节点 threshold 或增强输入数据", "换更高 tier 模型提升识别准确度"],
        "verify":    ["放宽/收紧 threshold 以匹配验收标准", "补充验收上下文后重试"],
        "resistor":  ["增加并行冗余分支（auto_heal）或提高重试次数", "换更高 tier 模型"],
        "capacitor": ["增加并行冗余分支（auto_heal）", "检查汇合输入完整性"],
        "inductor":  ["检查上游供电稳定性后重试"],
        "tool":      ["检查工具参数/权限，或补充上下文", "换 tool tier 或重试"],
        "skill":     ["检查技能参数与权限", "提供更多上下文后重试"],
        "power":     ["检查上游数据源可用性"],
        "opamp":     ["检查放大链路输入/偏置"],
        "diode":     ["检查极性/输入有效性"],
    }

    @classmethod
    def _grade(cls, score):
        if score >= cls.GRADE_A:
            return "A"
        if score >= cls.GRADE_B:
            return "B"
        if score >= cls.GRADE_C:
            return "C"
        return "D"

    @classmethod
    def _status(cls, sig, score, gate_thr):
        if not sig.ok:
            return "fail"
        if gate_thr is not None and score < gate_thr:
            return "marginal"
        if score < cls.GRADE_C:
            return "marginal"
        return "pass"

    @classmethod
    def _suggest(cls, ctype, status):
        base = cls._REPAIR.get(ctype, ["重试该节点或检查上游依赖", "补充上下文/输入后重试"])
        if status == "fail":
            return list(base)            # 失败给前两条修复方向
        if status == "marginal":
            return [base[0]]             # 边际只给首要建议
        return []                        # pass 无需修复

    @classmethod
    def assess(cls, out, components, final_quality, threshold=None):
        """out: {cid: Signal}; components: {cid: comp dict}; final_quality: float(0~1)。
        返回 QualityReport dict（纯函数，可复现，利于 A/B 对比）。"""
        nodes = {}
        repair = []
        counts = {"pass": 0, "marginal": 0, "fail": 0}
        for cid, sig in out.items():
            comp = components.get(cid, {})
            ctype = comp.get("type", "unknown")
            score = float(sig.quality) if sig.ok else 0.0
            gate_thr = comp.get("threshold") if ctype in ("adc", "verify") else None
            status = cls._status(sig, score, gate_thr)
            counts[status] += 1
            sugs = cls._suggest(ctype, status)
            nodes[cid] = {
                "type": ctype,
                "label": comp.get("label", cid),
                "ok": sig.ok,
                "score": round(score, 3),
                "score_100": round(score * 100, 1),
                "grade": cls._grade(score),
                "status": status,
                "suggestions": sugs,
            }
            for s in sugs:
                if s not in repair:
                    repair.append(s)
        fq = float(final_quality)
        passed = (threshold is None) or (fq >= threshold)
        return {
            "final_score": round(fq, 3),
            "final_score_100": round(fq * 100, 1),
            "final_grade": cls._grade(fq),
            "threshold": (round(threshold, 3) if threshold is not None else None),
            "passed": passed,
            "counts": counts,
            "nodes": nodes,
            "repair_plan": repair,
            "summary": (f"总评分 {round(fq*100,1)}/100（{cls._grade(fq)}）· "
                        f"通过 {counts['pass']} · 边际 {counts['marginal']} · 失败 {counts['fail']}"),
        }


def quality_gate_selftest():
    """Phase 2 细粒度质量门离线自检：打分 / 分级 / 修复建议 / 聚合 / 纯函数可复现。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    def sig(ok, quality):
        return Signal(ok=ok, quality=quality)

    components = {
        "src":  {"type": "power", "label": "src"},
        "adc":  {"type": "adc", "label": "adc", "threshold": 0.8},
        "R1":   {"type": "resistor", "label": "R1", "model": "small"},
        "tool": {"type": "tool", "label": "tool"},
        "cap":  {"type": "capacitor", "label": "cap"},
    }

    # 场景1：全高分 → 通过(A) · 修复计划空
    out1 = {
        "src": sig(True, 0.95), "adc": sig(True, 0.92),
        "R1": sig(True, 0.88), "tool": sig(True, 0.90), "cap": sig(True, 0.85),
    }
    r1 = QualityReport.assess(out1, components, 0.90, threshold=0.8)
    assert r1["passed"] is True, "全高分应通过"
    assert r1["final_grade"] == "A", f"总分0.9应为A，实际 {r1['final_grade']}"
    assert r1["counts"]["pass"] == 5, f"5 节点应全 pass，实际 {r1['counts']}"
    assert r1["repair_plan"] == [], "全过不应有修复计划"
    print("✓ Phase2 质量门：全过高分 → 通过(A) · 修复计划空")

    # 场景2：边际2 + 失败1 → 不通过 · 分级/修复建议命中
    out2 = {
        "src": sig(True, 0.95), "adc": sig(True, 0.72),   # 边际(低于 gate 0.8)
        "R1": sig(False, 0.0),                             # 失败
        "tool": sig(True, 0.90), "cap": sig(True, 0.55),   # 边际(<C 级 0.6)
    }
    r2 = QualityReport.assess(out2, components, 0.70, threshold=0.8)
    assert r2["passed"] is False, "总分0.7<0.8 应不通过"
    assert r2["counts"]["fail"] == 1 and r2["counts"]["marginal"] == 2, \
        f"失败1+边际2，实际 {r2['counts']}"
    assert any("threshold" in s for s in r2["nodes"]["adc"]["suggestions"]), \
        "adc 边际应给 threshold 修复建议"
    assert any("冗余" in s or "auto_heal" in s for s in r2["nodes"]["R1"]["suggestions"]), \
        "R1 失败应建议冗余分支/重试"
    assert len(r2["repair_plan"]) > 0, "有失败/边际应有修复计划"
    print(f"✓ Phase2 质量门：边际2+失败1 → 不通过 · 分级/修复建议命中（{r2['counts']}）")

    # 场景3：纯函数可复现（同输入同输出，利于 A/B 对比）
    r3 = QualityReport.assess(out2, components, 0.70, threshold=0.8)
    assert r3 == r2, "同输入应得相同报告（纯函数）"
    print("✓ Phase2 质量门：纯函数可复现（同输入同报告）")

    print("\nPhase 2 细粒度质量门 离线自检全部通过 ✓")


class SkillRegistry:
    """Phase 2 技能注册表：在 ② 已落地的 `compiler.agent_skills.SKILLS`
    （真实可执行的技能实现）之上叠加「集中注册 + 查询 + 拓扑技能引用解析」层。

    职责边界：
     · ② 负责"技能怎么真执行"（handler 实现 + execute_skill 派发）。
     · 本注册表负责"有哪些技能 / 拓扑用了哪些 / 哪些还没注册（待实现）"，
       是 introspection 层，纯函数、离线安全，不依赖 LLM/网络。
     · 解析来源：① 各组件 fillers 中的 `skill`；② 组件级 `skills` 声明（前瞻）；
       ③ executor 的 evolve_skill 配置。
    """

    # 技能分类（映射 ② 的技能名 → 能力类别，用于前端分组/统计）
    _CATEGORY = {
        "run_code": "compute", "calculator": "compute", "spreadsheet_calc": "compute",
        "unit_convert": "compute",
        "query_database": "retrieve", "query_db": "retrieve",
        "web_search": "retrieve", "read_page": "retrieve",
        "cross_check": "verify", "diff_text": "verify",
        "extract_fields": "extract", "extract_pdf": "extract", "extract_ocr": "extract",
        "apply_glossary": "translate", "classify_taxonomy": "classify",
        "apply_template": "organize", "apply_style_guide": "summarize",
        "draw_chart": "visualize", "send_email": "deliver",
    }

    def __init__(self, extra=None):
        # 复用 ② 已有技能（真实 handler 实现）
        from compiler.agent_skills import SKILLS
        self._skills = {}
        for name, spec in SKILLS.items():
            self._skills[name] = {
                "name": name,
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters", {}),
                "handler": spec.get("handler"),
                "category": self._CATEGORY.get(name, "general"),
                "tier": "standard",
                "implemented": spec.get("handler") is not None,
            }
        # 允许预登记"待实现"技能（handler=None），便于 resolve 标记未实现项
        if extra:
            for name, meta in extra.items():
                self.register(name, **meta)

    def register(self, name, description="", parameters=None,
                 handler=None, category="general", tier="standard"):
        """集中注册/覆盖一个技能。handler=None 表示"已登记但未实现"（待实现）。"""
        self._skills[name] = {
            "name": name,
            "description": description,
            "parameters": parameters or {},
            "handler": handler,
            "category": category,
            "tier": tier,
            "implemented": handler is not None,
        }

    def get(self, name):
        """查询单个技能 spec（含 handler/implemented/category 等元数据）。"""
        return self._skills.get(name)

    def is_registered(self, name):
        return name in self._skills

    def is_implemented(self, name):
        spec = self._skills.get(name)
        return bool(spec and spec.get("handler") is not None)

    def names(self):
        return list(self._skills.keys())

    def implemented_names(self):
        return [n for n, s in self._skills.items() if s["implemented"]]

    def list(self):
        """列出全部已注册技能（含分类/tier/是否已实现），供前端或 /skills 端点。"""
        return [
            {"name": n, "category": s["category"], "tier": s["tier"],
             "implemented": s["implemented"], "description": s["description"]}
            for n, s in sorted(self._skills.items())
        ]

    # ---- 拓扑技能引用解析 ----
    @staticmethod
    def _extract_skills(components, evolve_skill=None):
        """从 components dict 解析被引用的技能名（去重保序）。
        来源：① fillers 的 skill；② 组件级 skills 声明；③ evolve_skill。"""
        refs = []
        for _cid, comp in (components or {}).items():
            if not isinstance(comp, dict):
                continue
            # ① fillers
            fillers = comp.get("fillers") or {}
            if isinstance(fillers, dict):
                for _m, f in fillers.items():
                    if isinstance(f, dict) and f.get("skill"):
                        refs.append(f["skill"])
            # ② 组件级 skills（前瞻：节点直接声明它要用的技能集）
            sk = comp.get("skills")
            if isinstance(sk, (list, tuple)):
                refs.extend([s for s in sk if isinstance(s, str)])
            elif isinstance(sk, str):
                refs.append(sk)
        # ③ evolve_skill
        if evolve_skill:
            refs.append(evolve_skill)
        # 去重保序
        seen, out = set(), []
        for r in refs:
            if r and r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def skills_used(self, components, evolve_skill=None):
        """解析拓扑引用的技能，返回 {references, registered, unregistered}。"""
        refs = self._extract_skills(components, evolve_skill)
        registered, unregistered = [], []
        for r in refs:
            (registered if r in self._skills else unregistered).append(r)
        return {
            "references": refs,
            "registered": registered,
            "unregistered": unregistered,
            "count": len(refs),
            "registered_count": len(registered),
            "unregistered_count": len(unregistered),
        }

    def resolve(self, components, evolve_skill=None):
        """高层解析：拓扑引用技能 + 注册状态 + 待实现清单 + 总结。纯函数、离线安全。"""
        su = self.skills_used(components, evolve_skill)
        # 已注册但未实现的（声明了 handler=None 的 pending 项）
        pending = [n for n in su["registered"] if not self.is_implemented(n)]
        return {
            "references": su["references"],
            "registered": su["registered"],
            "unregistered": su["unregistered"],
            "pending": pending,
            "count": su["count"],
            "registered_count": su["registered_count"],
            "unregistered_count": su["unregistered_count"],
            "pending_count": len(pending),
            "summary": (f"引用 {su['count']} 个技能 · 已注册 {su['registered_count']} · "
                        f"待实现 {su['unregistered_count']}"
                        + (f" · 已登记未实现 {len(pending)}" if pending else "")),
        }


def skill_registry_selftest():
    """Phase 2 技能注册表离线自检：复用 ② 技能 / 注册 / 查询 / 拓扑引用解析 / 未注册标记。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    reg = SkillRegistry()
    # 复用 ② 已有技能：计算器/绘图等应在册且已实现
    assert reg.is_registered("calculator"), "应复用 ② 的 calculator"
    assert reg.is_implemented("calculator"), "calculator 应有 handler"
    assert reg.is_registered("draw_chart"), "应复用 ② 的 draw_chart"
    assert "web_search" in reg.implemented_names(), "web_search 应已实现"
    print(f"✓ Phase2 注册表：复用 ② 技能共 {len(reg.implemented_names())} 个已实现在册")

    # 集中注册：动态登记一个"待实现"技能（handler=None）
    reg.register("my_future_skill", description="示例：尚待实现的技能", handler=None)
    assert reg.is_registered("my_future_skill")
    assert not reg.is_implemented("my_future_skill"), "无 handler 应为未实现"
    assert "my_future_skill" not in reg.implemented_names()
    print("✓ Phase2 注册表：动态 register 一个待实现技能（handler=None）成功")

    # 拓扑技能引用解析：n1 引用 calculator(已注册) / n2 引用 a_missing_skill(未注册)
    # n3 通过 skills 声明 cross_check + web_search；evolve_skill=ci_analyze
    components = {
        "n1": {"type": "tool", "fillers": {"x": {"skill": "calculator", "args": {"expression": "1+1"}}}},
        "n2": {"type": "retrieve", "fillers": {"y": {"skill": "a_missing_skill", "args": {}}}},
        "n3": {"type": "resistor", "skills": ["cross_check", "web_search"]},
    }
    r = reg.resolve(components, evolve_skill="ci_analyze")
    assert "calculator" in r["registered"], "calculator 应判为已注册"
    assert "a_missing_skill" in r["unregistered"], "a_missing_skill 应判为未注册(待实现)"
    assert "cross_check" in r["references"] and "web_search" in r["references"]
    assert "ci_analyze" in r["references"], "evolve_skill 应被纳入引用"
    assert r["count"] == 5, f"应解析到 5 个引用(去重)，实际 {r['count']}: {r['references']}"
    assert r["unregistered_count"] == 1 and r["pending_count"] == 0, \
        f"未注册1/已登记未实现0，实际 {r['unregistered_count']}/{r['pending_count']}"
    print(f"✓ Phase2 注册表：拓扑引用解析 → {r['summary']}")

    # 注册那枚未实现技能后再解析：应从不注册转为 pending
    reg.register("a_missing_skill", description="补齐的技能", handler=lambda **k: "ok")
    r2 = reg.resolve(components, evolve_skill="ci_analyze")
    assert "a_missing_skill" in r2["registered"], "补齐后应为已注册"
    assert "a_missing_skill" not in r2["unregistered"], "补齐后不应再判未注册"
    assert r2["pending_count"] == 0, "补齐后不应有 pending"
    print("✓ Phase2 注册表：补齐技能后未注册项自动消失（解析随注册表最新状态）")

    # 纯函数 + 可复现：同一注册表 + 同一拓扑，两次解析结果一致（不依赖 LLM/网络）
    reg3 = SkillRegistry()
    a = reg3.resolve(components, evolve_skill="ci_analyze")
    b = reg3.resolve(components, evolve_skill="ci_analyze")
    assert a == b, "同输入同注册表应得相同解析（纯函数）"
    print("✓ Phase2 注册表：纯函数可复现（同输入同解析）")

    print("\nPhase 2 技能注册表 离线自检全部通过 ✓")


def long_sleep_selftest():
    """⑦ 加深：长周期休眠唤醒（run_sleep → sleeping → should_wake → wake 续跑 + 调度器）。"""
    import tempfile
    spec = {
        "name": "sleep_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "r1":  {"type": "resistor", "label": "reason#1", "model": "small", "yield": 1.0},
            "r2":  {"type": "resistor", "label": "reason#2", "model": "small", "yield": 1.0},
            "r3":  {"type": "resistor", "label": "reason#3", "model": "small", "yield": 1.0},
        },
        "wires": [["src", "r1"], ["r1", "r2"], ["r2", "r3"]],
    }
    cp = tempfile.mktemp(suffix=".json")

    # 第一轮：只跑 1 层后休眠
    t = LongTask(spec, checkpoint_path=cp)
    r1 = t.run_sleep(layers_per_round=1, wake_in_sec=0)
    assert r1["status"] == "sleeping", "应进入休眠"
    assert r1["done_layers"] == 1, "第一轮应只完成 1 层"
    assert r1["due_now"] is True, "wake_in_sec=0 应立即可唤醒"
    assert t.should_wake(), "should_wake 应立即为真"
    print("✓ ⑦ 加深：run_sleep 跑 1 层后休眠（status=sleeping, due_now=True）")

    # 唤醒续跑 → 完成
    r2 = t.wake()
    assert r2["status"] == "done", "唤醒后应跑完"
    assert r2["done_layers"] == 4, "应累计完成全部 4 层"
    print("✓ ⑦ 加深：wake 续跑至完成（done_layers=4，断点续跑不重跑）")

    # 未到期：wake_in_sec 很大 → should_wake 为假，wake 返回仍 sleeping
    cp2 = tempfile.mktemp(suffix=".json")
    t2 = LongTask(spec, checkpoint_path=cp2)
    t2.run_sleep(layers_per_round=1, wake_in_sec=100)   # 100 秒后唤醒
    assert t2.should_wake() is False, "未到期不应唤醒"
    r3 = t2.wake()
    assert r3["status"] == "sleeping" and r3.get("due_now") is False, "未到期 wake 应仍 sleeping"
    print("✓ ⑦ 加深：未到期 → should_wake=False，wake 仍 sleeping（不误唤醒）")

    # 调度器：submit → tick 唤醒到期任务 → 完成
    cp3 = tempfile.mktemp(suffix=".json")
    sched = LongScheduler()
    t3 = LongTask(spec, checkpoint_path=cp3)
    sched.submit(t3, layers_per_round=1, wake_in_sec=0)
    res = sched.tick()
    assert any(r.get("status") == "done" for r in res.values()), "调度器 tick 应唤醒并跑完"
    assert sched.status_all() == {}, "完成后应从调度器移除"
    print("✓ ⑦ 加深：LongScheduler.submit→tick 唤醒到期任务并跑完")
    for p in (cp, cp2, cp3):
        try: os.unlink(p)
        except OSError: pass

    print("✓ ⑦ 长周期休眠唤醒 离线自检通过")


def human_decision_point_selftest():
    """⑧ 加深：决策点暂停（执行前主动请人类审批 proceed/skip/abort + 全节点/all）。"""
    spec = {
        "name": "decision_demo",
        "components": {
            "src":   {"type": "power", "label": "task"},
            "reason": {"type": "resistor", "label": "reason", "model": "small",
                       "yield": 1.0, "human_decision_point": True},
            "sum":    {"type": "resistor", "label": "summarize", "model": "small", "yield": 1.0},
        },
        "wires": [["src", "reason"], ["reason", "sum"]],
    }

    def make(decision_map):
        ev = []
        def cb(node, missing=None, context=None, label=None, decision_point=False):
            return decision_map.get(node, "proceed")
        circ = Circuit(spec, SimBackend(random.Random(0)))
        ex = CircuitExecutor(circ, human_callback=cb, decision_points={"reason"},
                             on_event=lambda e: ev.append(e))
        return ex, ev

    # 1) proceed → 正常执行
    ex, ev = make({"reason": "proceed"})
    r = ex.run()
    assert r["success"] is True, "proceed 应正常完成"
    assert any(e.get("type") == "human_decision_point" for e in ev), "应发出决策点事件"
    assert r["components"]["reason"]["ok"] is True, "proceed 后该节点应被执行"
    print("✓ ⑧ 加深：决策点 proceed → 正常执行（发出 human_decision_point）")

    # 2) skip → 该节点跳过，不执行后端
    ex, ev = make({"reason": "skip"})
    r = ex.run()
    assert any(e.get("type") == "human_skip" for e in ev), "应发出 human_skip"
    assert r["components"]["reason"]["ok"] is False, "skip 后该节点应标记未通过"
    print("✓ ⑧ 加深：决策点 skip → 节点跳过（不执行后端，继续后续）")

    # 3) abort → 提前终止
    ex, ev = make({"reason": "abort"})
    r = ex.run()
    assert r.get("aborted") is True and r.get("abort_node") == "reason", "应中止于决策点"
    print("✓ ⑧ 加深：决策点 abort → 提前终止（aborted=True, abort_node=reason）")

    # 4) 零回归：spec 无标记 + 无 decision_points → 完全不暂停（现行行为不变）
    spec_plain = {
        "name": "plain_demo",
        "components": {
            "src":    {"type": "power", "label": "task"},
            "reason": {"type": "resistor", "label": "reason", "model": "small", "yield": 1.0},
            "sum":    {"type": "resistor", "label": "summarize", "model": "small", "yield": 1.0},
        },
        "wires": [["src", "reason"], ["reason", "sum"]],
    }
    ev4 = []
    circ4 = Circuit(spec_plain, SimBackend(random.Random(0)))
    ex4 = CircuitExecutor(circ4, human_callback=lambda **k: "proceed",
                          on_event=lambda e: ev4.append(e))
    r4 = ex4.run()
    assert r4["success"] is True, "无决策点配置应正常完成"
    assert not any(e.get("type") == "human_decision_point" for e in ev4), "不应发出决策点事件"
    print("✓ ⑧ 加深：零回归——spec 无标记且无配置时不暂停")

    # 4b) spec 显式标记独立生效：decision_points=None 但 comp 标了 → 仍暂停
    ev4b = []
    circ4b = Circuit(spec, SimBackend(random.Random(0)))
    ex4b = CircuitExecutor(circ4b, human_callback=lambda **k: "proceed",
                           on_event=lambda e: ev4b.append(e))
    r4b = ex4b.run()
    dp4b = [e for e in ev4b if e.get("type") == "human_decision_point"]
    assert r4b["success"] is True and len(dp4b) == 1 and dp4b[0].get("node") == "reason", \
        "spec 的 human_decision_point 标记应独立于 decision_points 参数生效"
    print("✓ ⑧ 加深：spec 声明 human_decision_point → 无需参数即暂停")

    # 5) decision_points="all" → 每个节点都暂停，全部 proceed 仍成功
    ev5 = []
    circ5 = Circuit(spec, SimBackend(random.Random(0)))
    ex5 = CircuitExecutor(circ5, human_callback=lambda **k: "proceed",
                          decision_points="all", on_event=lambda e: ev5.append(e))
    r5 = ex5.run()
    assert r5["success"] is True, "all 决策点全 proceed 应成功"
    n_dp = sum(1 for e in ev5 if e.get("type") == "human_decision_point")
    assert n_dp == 3, f"all 应有 3 个节点发决策点事件，实际 {n_dp}"
    print("✓ ⑧ 加深：decision_points='all' → 每节点暂停（3 个决策点）")

    print("✓ ⑧ 人机协同决策点 离线自检通过")


if __name__ == "__main__":
    selftest()
    circuit_executor_selftest()
    circuit_executor_evolve_selftest()
    evolve_enhanced_selftest()
    hetero_verify_selftest()
    memory_record_selftest()
    human_intervention_selftest()
    human_decision_point_selftest()
    stream_selftest()
    multi_backend_selftest()
    batch_executor_selftest()
    long_task_selftest()
    long_sleep_selftest()
    multi_robot_selftest()
    permission_selftest()
    adaptive_topology_selftest()
    self_evolution_selftest()
    quality_gate_selftest()
    skill_registry_selftest()


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
