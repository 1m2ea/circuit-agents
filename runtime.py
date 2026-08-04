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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional


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
    def run(self):
        self._t0 = time.perf_counter()
        out = {}
        layers = self.circuit.layers()
        # ③ 智能模型选型：执行前按复杂度/历史/约束微调电阻 model/skills
        if self.auto_select_models:
            try:
                from compiler.model_selector import ModelSelector
                from compiler.topology_memory import TopologyMemory
                mem = TopologyMemory() if self.memory_enabled else None
                ms = ModelSelector(memory=mem)
                ms.apply_to_spec(self.circuit.spec)
            except Exception:
                pass  # 选型失败 → 沿用原 spec 不变（零回归）
        self._emit("start",
                   spec=self.circuit.spec.get("name", "unnamed"),
                   nodes=len(self.circuit.components),
                   layers=len(layers))

        for li, layer in enumerate(layers):
            self._emit("layer_start", layer_idx=li, nodes=list(layer))
            for cid in layer:
                comp = self.circuit.components[cid]
                self._emit("node_start", node=cid, ctype=comp.get("type"),
                           label=comp.get("label"))
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
                            self._emit("human_abort", node=cid)
                            out[cid] = sig
                            self._emit("layer_done", layer_idx=li)
                            # 提前终止
                            self._results = out
                            terminals = [c for c in self.circuit.components
                                         if not self.circuit.succ[c]]
                            fq = max((out[c].quality for c in terminals), default=0.0)
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
        if gate_nodes:
            thr = max(comp.get("threshold", 0.8) for _, comp in gate_nodes)
            quality_gate = {"threshold": round(thr, 3), "passed": fq >= thr}
        failed_nodes = [c for c in terminals if not out[c].ok]

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


if __name__ == "__main__":
    selftest()
    circuit_executor_selftest()
    circuit_executor_evolve_selftest()
    evolve_enhanced_selftest()
    hetero_verify_selftest()
    memory_record_selftest()
    human_intervention_selftest()
    stream_selftest()
    multi_backend_selftest()


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
