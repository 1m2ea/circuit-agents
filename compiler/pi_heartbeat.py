#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""π 驱动的永动心跳 f(π)：用 π 的数字序列调度系统进化方向。

核心思想（用户设计，2026-08-06）：
  · π 的十进制数字永不重复：3.14159265358979323846...
  · 每一位数字 → 一个贪婪动作（系统这一拍去逼近什么）
  · 动作结果更新 system_state，new_state 自动成为下一拍的 system_state
  → 方向恒变（π 决定），起点恒变（state 决定）：双重恒变，永动进化。

动作映射（全部复用现有模块，零新核心逻辑）：
  0-3  随机探索新任务类型        → ⑭ SelfEvolution（蒸馏高频 motif 为可复用模板）
  4-6  对最近拓扑跑奥卡姆剃刀    → simplify()（化简器，去冗余节点）
  7-8  分析低质量历史并升级模型重试 → QualityReport 分级 + 模型档升级计划
  9    (MENTOR_TRIGGER_DIGIT) 导师-学生训练 → mentor.mentor_train_cycle
       （失败案例→导师优化→学生重跑→质量门→固化；区别于知识蒸馏，零数据零算力）

  ROI 升级（用户设计，2026-08-14）：心跳从「按 π 贪」升级为「按 ROI 贪」。
  投入产出比 = 投入 ÷ 产出，越小越好。开启 roi_guided 后，每拍用 _roi_scores()
  给四个动作算「预期 产出/投入」（越大越贪），默认选最高分动作；π 数字仅作探索
  扰动（探索档内不走纯贪）以保持多样性。系统连续良态时 idle_pressure 累积到上限
  → 心跳自我节流（idle），即「心跳自身 ROI 趋零则不贪」的纪律。server 在每次 /run
  后回灌真实信号（record_task_roi），让评分器读到真实 token/质量，闭环更准。

π 数字源：无界十进制 spigot 算法（Rabinowitz & Wagon, 1995），
  逐位生成、无需存储整串、永不重复——真正"永续"，且零依赖。

模块只做"调度 + 状态反馈"，不自己执行重活；每个动作都离线安全、异常静默，
保证心跳永不把宿主（server.py）拖崩。
"""

import os
import threading

try:
    from runtime import SelfEvolution
except Exception:  # pragma: no cover - 宿主已 import runtime，这里仅防御
    SelfEvolution = None

try:
    from compiler.simplify import simplify
except Exception:
    simplify = None

try:
    from compiler.topology_memory import TopologyMemory
except Exception:
    TopologyMemory = None

try:
    from mentor import mentor_train_cycle, default_content_quality
except Exception:  # pragma: no cover - mentor 不可用则训练动作离线降级
    mentor_train_cycle = None
    default_content_quality = None

# 导师-学生训练电路的 π 触发数字（用户设计：π 某位数字触发训练回路；默认 9）
MENTOR_TRIGGER_DIGIT = int(os.environ.get("MENTOR_TRIGGER_DIGIT", "9"))

# ── ROI 导向评分常量（用户升级：从「按 π 贪」升级为「按 ROI 贪」）──
# ROI = 投入 ÷ 产出，越小越好；这里用 score = 产出/投入（越大越贪）。
# 每个动作的「预期投入/产出」从 system_state 的真实信号推算；无信号时给离线默认，
# 保证心跳永不崩、且离线也能跑出有意义的选择。
ROI_TEMPLATES_SAT = 10.0     # 模板数饱和点（超过后 explore 边际产出→0）
ROI_RETRY_COST = 1.0         # 升档重试相对投入（更大模型 → 更贵）
ROI_LIGHT_COST = 0.2         # 探索/化简相对投入（离线、零调用）
ROI_MENTOR_COST = 0.5        # 导师训练相对投入（一步闭环，离线安全）
ROI_IDLE_CAP = 5             # 连续良态累积到该值 → 心跳自我节流（不贪）
ROI_THROTTLE_FLOOR = 0.05    # 仅兜底：所有动作 score 低于此也节流
ROI_EXPLORE_DIGIT_BAND = 3   # π digit 落入 0-3 → 保留探索多样性（不纯贪）


# ──────────────────────────────────────────────────────────
# π 数字源：无界十进制 spigot 生成器
# ──────────────────────────────────────────────────────────

class PiSpigot:
    """无界十进制 π 数字生成器（spigot 算法）。

    算法（Rabinowitz & Wagon, 1995）逐位吐出 π 的十进制数字，
    起始即吐整数位 3，其后 1,4,1,5,9,2,6,5,3,5,8,9,7,9,...（=3.14159265358979...）。
    只维护 O(1) 状态，永不重复、无需存储整串。
    """

    def __init__(self):
        self._q, self._r, self._t, self._k, self._n, self._l = 1, 0, 1, 1, 3, 3
        self._cache = []  # 已生成位数，支持 digit_at(n) 随机访问

    def next_digit(self):
        """返回 π 的下一个十进制数字（首位是 3）。"""
        while True:
            if 4 * self._q + self._r - self._t < self._n * self._t:
                digit = self._n
                self._q, self._r, self._t, self._k, self._n, self._l = (
                    10 * self._q,
                    10 * (self._r - self._n * self._t),
                    self._t,
                    self._k,
                    (10 * (3 * self._q + self._r)) // self._t - 10 * self._n,
                    self._l,
                )
                self._cache.append(digit)
                return digit
            self._q, self._r, self._t, self._k, self._n, self._l = (
                self._q * self._k,
                (2 * self._q + self._r) * self._l,
                self._t * self._l,
                self._k + 1,
                (self._q * (7 * self._k + 2) + self._r * self._l) // (self._t * self._l),
                self._l + 2,
            )

    def digit_at(self, n):
        """返回 π 的第 n 位（n 从 0 起：第0位=3）。"""
        while len(self._cache) <= n:
            self.next_digit()
        return self._cache[n]

    def first_digits(self, k):
        return [self.digit_at(i) for i in range(k)]


# ──────────────────────────────────────────────────────────
# 内置演示拓扑（TopologyMemory 为空时，保证化简动作有东西可剃）
# ──────────────────────────────────────────────────────────

_DEMO_SPEC = {
    "name": "demo_parallel",
    "components": {
        "src": {"type": "source", "label": "src"},
        "r1": {"type": "resistor", "label": "r1", "capability": "extract",
               "model": "small", "produced_outputs": ["o1"]},
        "r2": {"type": "resistor", "label": "r2", "capability": "extract",
               "model": "small", "produced_outputs": ["o2"]},
        "adc": {"type": "adc", "label": "adc", "model": "small"},
    },
    "wires": [["src", "r1"], ["src", "r2"], ["r1", "adc"], ["r2", "adc"]],
}


# ──────────────────────────────────────────────────────────
# 永动心跳
# ──────────────────────────────────────────────────────────

class PiHeartbeat:
    """f(π) 永动心跳：π 数字决定方向，system_state 决定起点，输出回传为下一拍输入。

    I/O 契约（与用户设计一致）：
      f(pi_digit, system_state) -> (action_result, new_state)
      · action_result: 这一拍做了什么 + 结果（质量变化/节点变化/新发现）
      · new_state: 执行后的新系统状态，自动成为下一拍 f() 的 system_state
    """

    # π 数字 → 动作 的分档（用户设计）
    BAND_EXPLORE = 3   # digit 0-3
    BAND_SIMPLIFY = 6  # digit 4-6
    # digit 7-8 → 升级重试；digit == MENTOR_TRIGGER_DIGIT(默认9) → 导师-学生训练

    def __init__(self, tm=None, interval=60.0,
                 mentor_store=None, mentor_backend=None, mentor_registry=None,
                 mentor_http_post=None, mentor_enabled=True, roi_guided=False):
        self.tm = tm if tm is not None else (TopologyMemory() if TopologyMemory else None)
        self.interval = interval
        self.spigot = PiSpigot()
        self.state = self._initial_state()
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # 导师-学生训练电路（digit == MENTOR_TRIGGER_DIGIT 时触发）
        self.mentor_store = mentor_store          # ExecutionStore（取失败案例）
        self.mentor_backend = mentor_backend      # 学生后端（如本地 OllamaBackend 7B）
        self.mentor_http_post = mentor_http_post  # 导师 HTTP 注入（离线测试用）
        self.mentor_registry = mentor_registry if mentor_registry is not None else []
        self.mentor_enabled = mentor_enabled
        # ROI 导向：True 时心跳「按 ROI 贪」（默认仍按 π 贪，保证旧自检不变）
        self.roi_guided = bool(roi_guided)
        self.idle_cap = ROI_IDLE_CAP

    @staticmethod
    def _initial_state():
        return {
            "n": 0,
            "quality_mean": 0.0,
            "recent_task_types": [],
            "template_count": 0,
            "simplify_deltas": [],
            "rerun_plans": 0,
            "mentor_runs": 0,        # 导师训练触发次数
            "mentor_solidified": 0,  # 通过质量门并固化的方案数
            "history": [],   # [(digit, action, summary)] —— 反馈闭环的"记忆"
            "last": None,
            # ROI 导向扩展字段（server 回灌真实信号；无信号时不影响旧逻辑）
            "task_roi_history": [],   # 每次 /run 的真实 ROI 信号
            "task_quality_mean": 0.0,
            "low_quality_count": 0,
            "failed_node_count": 0,
            "idle_pressure": 0,       # 连续良态压力；到 idle_cap → 自我节流
            "throttle_count": 0,      # 自我节流（idle）累计次数
            "last_roi_scores": {},    # 最近一拍四动作 ROI 评分（观测用）
            "last_roi_choice": None,
            "last_roi_score": 0.0,
        }

    # ── 三个动作（复用现有模块，离线安全、永不抛错）──

    def _explore(self, state, digit):
        """0-3：用 ⑭ SelfEvolution 从历史上蒸馏可复用模板 → 探索新任务类型。"""
        result = {"action": "explore", "digit": digit}
        try:
            history = []
            if self.tm is not None:
                try:
                    history = [e.get("spec", {}) for e in self.tm.recent(50)]
                except Exception:
                    history = []
            if len(history) < 2:  # 历史不足 → 用演示历史喂，保证有产出
                history = [_DEMO_SPEC, _DEMO_SPEC]
            if SelfEvolution is None:
                result["explored"] = "SelfEvolution 不可用（离线降级）"
                result["task_type"] = None
                return result
            ev = SelfEvolution(history)
            templates = ev.templates
            result["template_count"] = len(templates)
            if templates:
                top = templates[0]
                motif = "+".join(top["motif"])
                result["task_type"] = motif
                result["support"] = top["support"]
                if self.tm is not None:
                    self.tm.record(
                        goal_desc=f"pi-explore:{motif}",
                        spec=top.get("example") or _DEMO_SPEC,
                        result={"success": True, "final_quality": 0.9,
                                "total_latency_ms": 0, "total_cost": 0},
                    )
                state["template_count"] = max(state["template_count"], len(templates))
                state["recent_task_types"].append(motif)
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    def _simplify(self, state, digit):
        """4-6：对最近（或演示）拓扑跑奥卡姆剃刀，尝试更少节点。"""
        result = {"action": "simplify", "digit": digit}
        try:
            spec = None
            if self.tm is not None:
                try:
                    recs = self.tm.recent(1)
                    if recs:
                        spec = recs[-1].get("spec")
                except Exception:
                    spec = None
            if spec is None:
                spec = _DEMO_SPEC
            if simplify is None:
                result["simplified"] = False
                result["note"] = "simplify 不可用（离线降级）"
                return result
            new_spec, report = simplify(spec)
            result["original_nodes"] = report.get("original_nodes")
            result["final_nodes"] = report.get("final_nodes")
            result["removed"] = report.get("removed", [])
            result["merged"] = report.get("merged", [])
            result["node_delta"] = report.get("original_nodes", 0) - report.get("final_nodes", 0)
            state["simplify_deltas"].append(result["node_delta"])
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    def _retry(self, state, digit):
        """7-8：找最低质量历史任务，升级模型档重试（质量门驱动）。"""
        result = {"action": "retry", "digit": digit}
        try:
            target = None
            if self.tm is not None:
                try:
                    recs = self.tm.recent(100)
                    low = [r for r in recs
                           if r.get("result", {}).get("final_quality", 1) < 0.75]
                    if low:
                        target = min(low, key=lambda r: r["result"]["final_quality"])
                except Exception:
                    target = None
            if target is None:
                result["target"] = None
                result["note"] = "无低质量历史，演示升级：small→large"
                upgraded = "large"
            else:
                result["target_quality"] = target["result"]["final_quality"]
                result["failed_nodes"] = target.get("failed_nodes", [])
                cur_model = "small"
                for c in target.get("spec", {}).get("components", {}).values():
                    if c.get("type") == "resistor":
                        cur_model = c.get("model", "small")
                        break
                # 质量门分级：<B 级(0.75) → 升一档模型再试
                upgraded = {"small": "large", "large": "tool",
                            "tool": "code", "code": "large"}.get(cur_model, "large")
                result["from_model"] = cur_model
            result["upgraded_model"] = upgraded
            result["rerun_plan"] = f"以 {upgraded} 档重新执行（质量门驱动）"
            state["rerun_plans"] += 1
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    def _mentor_train(self, state, digit):
        """digit == MENTOR_TRIGGER_DIGIT：跑一步导师-学生训练电路。

        失败案例 → 导师分析 → 应用优化 → 学生重跑 → 质量门 → 通过则固化。
        无 mentor 模块 / 无 store / 无失败案例 时静默降级，绝不拖崩心跳。
        """
        result = {"action": "mentor", "digit": digit}
        try:
            if not self.mentor_enabled or mentor_train_cycle is None:
                result["skipped"] = "mentor 不可用或已禁用（离线降级）"
                return result
            if self.mentor_store is None:
                result["skipped"] = "无 execution_store，无失败案例可训练"
                return result
            cyc = mentor_train_cycle(
                self.mentor_store,
                student_backend=self.mentor_backend,
                http_post=self.mentor_http_post,
                registry=self.mentor_registry,
                quality_fn=default_content_quality,
            )
            if not cyc.get("ok"):
                result["skipped"] = cyc.get("reason", "no_failed_case")
                return result
            result["run_id"] = cyc.get("run_id")
            result["diagnosis"] = cyc.get("diagnosis")
            result["before_quality"] = cyc.get("before_quality")
            result["after_quality"] = cyc.get("after_quality")
            result["quality_gate_passed"] = cyc.get("quality_gate_passed")
            result["quality_gate_reason"] = cyc.get("quality_gate_reason")
            state["mentor_runs"] = state.get("mentor_runs", 0) + 1
            if cyc.get("quality_gate_passed"):
                state["mentor_solidified"] = state.get("mentor_solidified", 0) + 1
                result["solidified"] = True
                # 训练成果回灌拓扑记忆，供后续 explore/simplify 复用
                if self.tm is not None:
                    try:
                        self.tm.record(
                            goal_desc=f"pi-mentor:{cyc.get('diagnosis')}",
                            spec=cyc.get("optimized_spec") or {},
                            result={"success": True,
                                    "final_quality": cyc.get("after_quality", 0.0),
                                    "total_latency_ms": 0, "total_cost": 0},
                        )
                    except Exception:
                        pass
            else:
                result["solidified"] = False
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    # ── f(π)：核心调度 ──

    def _digit_to_action(self, digit):
        """π 数字 → 自然动作（探索多样性用）。"""
        if digit == MENTOR_TRIGGER_DIGIT:
            return "mentor"
        if digit <= self.BAND_EXPLORE:
            return "explore"
        if digit <= self.BAND_SIMPLIFY:
            return "simplify"
        return "retry"

    def _dispatch_action(self, action, digit, state):
        """动作名 → 对应执行方法（供 f 与 f_roi 复用）。"""
        if action == "mentor":
            return self._mentor_train(state, digit)
        if action == "simplify":
            return self._simplify(state, digit)
        if action == "retry":
            return self._retry(state, digit)
        return self._explore(state, digit)

    def f(self, pi_digit, state):
        """按 π 数字贪（默认模式）：数字决定方向。"""
        action = self._digit_to_action(pi_digit)
        action_result = self._dispatch_action(action, pi_digit, state)
        new_state = self._update_state(state, action_result)
        return action_result, new_state

    # ── ROI 导向：从「按 π 贪」升级为「按 ROI 贪」──

    def record_task_roi(self, tokens=0, seconds=0.0, quality=0.0,
                        failed_nodes=None, low_quality=False, solidified=False):
        """server 在每次 /run 后回灌真实 ROI 信号，供心跳「按 ROI 贪」。

        tokens/seconds/quality 来自 result.llm + elapsed_ms + final_quality；
        low_quality/failed_nodes 来自质量门；solidified 来自导师固化。
        连续良态（高质无失败）累积 idle_pressure → 触发自我节流。
        """
        with self._lock:
            s = self.state
            hist = list(s.get("task_roi_history", []))
            perq = (tokens / (quality * 100.0)) if (quality and quality > 0) else None
            entry = {"tokens": int(tokens or 0), "seconds": float(seconds or 0.0),
                     "quality": float(quality or 0.0), "perq": perq,
                     "low_quality": bool(low_quality), "solidified": bool(solidified),
                     "failed_nodes": list(failed_nodes or [])}
            hist.append(entry)
            if len(hist) > 50:
                hist = hist[-50:]
            s["task_roi_history"] = hist
            qs = [e["quality"] for e in hist if e["quality"] > 0]
            s["task_quality_mean"] = round(sum(qs) / len(qs), 4) if qs else 0.0
            s["low_quality_count"] = int(sum(1 for e in hist if e["low_quality"]))
            s["failed_node_count"] = int(sum(len(e["failed_nodes"]) for e in hist))
            if solidified:
                s["mentor_solidified"] = s.get("mentor_solidified", 0) + 1
            # idle_pressure：连续良态压力 → 心跳自身 ROI 趋零则不贪
            if low_quality or (failed_nodes and len(failed_nodes) > 0) or quality < 0.75:
                s["idle_pressure"] = 0
            elif quality >= 0.9:
                s["idle_pressure"] = min(self.idle_cap, s.get("idle_pressure", 0) + 1)

    def _roi_scores(self, state):
        """给四个动作算「预期 产出 / 投入」（越大越值得贪）。

        投入 = 该动作消耗的算力/时间/注意力；产出 = 它换来的质量/复用/可溯源提升。
        ROI = 投入 ÷ 产出（越小越好）；这里用 score = 产出/投入（越大越贪）。
        信号优先取真实回灌（task_* / low_quality_count），无信号时给离线默认，
        保证离线也能跑出有意义、且不崩的选择。
        """
        s = state
        templates = s.get("template_count", 0)
        deltas = s.get("simplify_deltas", []) or []
        recent_delta = deltas[-1] if deltas else 0
        lowq = s.get("low_quality_count", 0)
        mentor_avail = bool(self.mentor_store)

        # explore：模板数越接近饱和，边际产出越低（奥卡姆式边际递减）
        out_explore = max(0.0, 1.0 - templates / ROI_TEMPLATES_SAT)
        # simplify：化简的产出是「未来省算力」，基线温和；最近化简有过正 delta → 仍有冗余可剃
        out_simplify = 0.3 + 0.4 * (1.0 if (recent_delta and recent_delta > 0) else 0.0)
        # retry：存在低质量失败 → 修回它是最值钱的产出（不修=失败持续失血）；
        #   无失败则纯烧钱（产出=0）。投入被更大模型放大，故需失败在才有 ROI。
        out_retry = min(2.5, 1.0 + lowq) if lowq > 0 else 0.0
        # mentor：有失败案例源 + 失败在 → 高认知产出；否则跳过（产出=0）
        out_mentor = min(2.5, 1.2 + lowq) if (mentor_avail and lowq > 0) else 0.0

        return {
            "explore":  (out_explore,  ROI_LIGHT_COST,  out_explore / ROI_LIGHT_COST),
            "simplify": (out_simplify, ROI_LIGHT_COST,  out_simplify / ROI_LIGHT_COST),
            "retry":    (out_retry,    ROI_RETRY_COST,  out_retry / ROI_RETRY_COST),
            "mentor":   (out_mentor,   ROI_MENTOR_COST, out_mentor / ROI_MENTOR_COST),
        }

    def _should_self_throttle(self, state):
        """心跳自身 ROI 趋零 → 不贪：连续良态（idle_pressure 到顶）即节流。"""
        return state.get("idle_pressure", 0) >= self.idle_cap

    def f_roi(self, pi_digit, state):
        """按 ROI 贪：默认选 score 最高的动作；π 数字仅作探索扰动/兜底。

        系统连续良态（idle_pressure 到顶）→ 自我节流为 idle（心跳自身 ROI 趋零·不贪·省算力）。
        """
        scores = self._roi_scores(state)
        if self._should_self_throttle(state):
            ar = {"action": "idle", "digit": pi_digit,
                  "note": "连续良态·心跳自身 ROI 趋零 → 自我节流（不贪·省算力）"}
            new_state = self._update_state(state, ar)
        else:
            best_action, best = "explore", -1.0
            for a, (o, i, sc) in scores.items():
                if sc > best:
                    best, best_action = sc, a
            action = best_action
            # 探索扰动：π 探索档内不走纯贪，保留多样性（避免局部最优 + 保证各动作被访问）
            if pi_digit <= ROI_EXPLORE_DIGIT_BAND:
                action = self._digit_to_action(pi_digit)
            ar = self._dispatch_action(action, pi_digit, state)
            new_state = self._update_state(state, ar)
        new_state["last_roi_scores"] = {k: round(v[2], 3) for k, v in scores.items()}
        new_state["last_roi_choice"] = ar.get("action")
        new_state["last_roi_score"] = round(max((v[2] for v in scores.values()), default=0.0), 3)
        return ar, new_state

    def set_roi_guided(self, enabled):
        """运行时切换 ROI 导向（server 端点调用）。"""
        self.roi_guided = bool(enabled)
        return self.roi_guided

    @staticmethod
    def _update_state(state, action_result):
        """把动作结果并入 system_state，并刷新聚合量（质量均值等）。"""
        new_state = dict(state)
        history = list(state.get("history", []))
        history.append((
            action_result.get("digit"),
            action_result.get("action"),
            {k: v for k, v in action_result.items() if k not in ("action", "digit")},
        ))
        if len(history) > 200:
            history = history[-200:]
        new_state["history"] = history
        new_state["last"] = action_result
        # 自我节流（idle）：系统已良态，本拍不产生价值 → 不计质量逼近、只计节流
        if action_result.get("action") == "idle":
            new_state["throttle_count"] = state.get("throttle_count", 0) + 1
            new_state["n"] = state.get("n", 0) + 1
            return new_state
        # 质量均值：滑动估计（仅示意"系统状态在变"，非真实模型质量）
        try:
            contrib = {"explore": 0.91, "simplify": 0.90, "retry": 0.88,
                       "mentor": 0.93}.get(action_result.get("action"), 0.90)
            prev = state.get("quality_mean", 0.0)
            n = state.get("n", 0)
            new_state["quality_mean"] = round(
                (prev * n + contrib) / (n + 1), 4) if (n + 1) else contrib
        except Exception:
            pass
        new_state["n"] = state.get("n", 0) + 1
        return new_state

    # ── 主循环：因变量回传为下一拍自变量 ──

    def tick(self, mode=None):
        """推进一拍：取 π 下一位 → f()/f_roi() → 更新 state。返回本拍快照。
        mode: 'pi'（按 π 贪，默认）或 'roi'（按 ROI 贪）；None 时取 self.roi_guided。
        """
        mode = mode or ("roi" if self.roi_guided else "pi")
        with self._lock:
            digit = self.spigot.next_digit()
            if mode == "roi":
                action_result, self.state = self.f_roi(digit, self.state)
            else:
                action_result, self.state = self.f(digit, self.state)
            return {
                "digit": digit,
                "mode": mode,
                "action": action_result.get("action"),
                "result": action_result,
                "state": self._public_state(),
            }

    def _public_state(self):
        s = dict(self.state)
        s["history"] = len(s.get("history", []))  # 不暴露大历史，只给长度
        return s

    def run_once(self, n=1, mode=None):
        return [self.tick(mode) for _ in range(n)]

    # ── 后台永动循环 ──

    def start(self, interval=None, roi=None):
        if interval is not None:
            self.interval = interval
        if roi is not None:
            self.roi_guided = bool(roi)
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        return True

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(self.interval)


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def pi_heartbeat_selftest():
    """π 永动心跳离线自检：spigot + f(π) 四动作覆盖 + 状态恒变 + 反馈闭环 + 导师训练触发。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    sp = PiSpigot()
    assert sp.first_digits(8) == [3, 1, 4, 1, 5, 9, 2, 6], \
        f"spigot 前8位应为 3.1415926，实际 {sp.first_digits(8)}"
    print("✓ π 无界 spigot：前8位 = 3.1415926（永不重复、无需存储整串）")

    # tm=None：纯内存，不写盘；验证 f(π) 调度 + 反馈闭环
    hb = PiHeartbeat(tm=None, interval=0.01)
    out = hb.run_once(n=12)
    assert len(out) == 12, "应跑满 12 拍"
    actions = {o["action"] for o in out}
    assert actions == {"explore", "simplify", "retry", "mentor"}, \
        f"π 数字 0-9 分布应让四动作都出现，实际 {actions}"
    assert hb.state["n"] == 12 and len(hb.state["history"]) == 12, "状态应随拍递增"
    # 反馈闭环：new_state 成为下一拍输入 —— state 被就地更新且历史连续
    digits = [o["digit"] for o in out]
    print(f"✓ f(π) 永动心跳：12 拍动作={sorted(actions)}；π 序列={digits}")
    print(f"  状态恒变：n={hb.state['n']} 质量均值={hb.state['quality_mean']} "
          f"模板数={hb.state['template_count']} 化简Δ={hb.state['simplify_deltas']} "
          f"重跑计划={hb.state['rerun_plans']}（方向恒变 + 起点恒变）")
    # 演示内置拓扑化简确实跑通（node_delta 有定义，可能为 0）
    simp = [o for o in out if o["action"] == "simplify"][0]
    assert "node_delta" in simp["result"], "化简动作应返回 node_delta"

    # 无 store 时 mentor 动作应静默降级（不抛错、不拖崩心跳）
    men = [o for o in out if o["action"] == "mentor"][0]
    assert "error" not in men["result"], f"mentor 降级不应报错：{men['result']}"
    assert men["result"].get("skipped"), "无 store 时 mentor 应给出 skipped 原因"
    print(f"✓ digit={MENTOR_TRIGGER_DIGIT} → 导师训练动作已接线；无 store 时静默降级："
          f"{men['result']['skipped']}")

    # ── 有失败案例 + mock 导师 + mock 学生：π 触发真跑一步训练闭环 ──
    _selftest_mentor_trigger()

    # ── ROI 导向子自检：验证「按 ROI 贪」升级（不破坏上面 π 模式断言）──
    _selftest_roi_mode()

    print("\nπ 永动心跳 离线自检全部通过 ✓")


def _selftest_roi_mode():
    """ROI 导向子自检：开启 roi_guided 后，心跳「按 ROI 贪」——
    评分器：无失败→explore 最优；有低质量失败→retry 最优（修回失败最值钱）。
    真实贪选择应直选 retry；连续良态→自我节流 idle（心跳自身 ROI 趋零·不贪）。
    """
    hb = PiHeartbeat(tm=None, interval=0.01, roi_guided=True)

    # 1) 评分器直接验证：无信号→explore 最优；有失败→retry 最优
    s0 = hb._initial_state()
    sc0 = hb._roi_scores(s0)
    best0 = max(sc0, key=lambda a: sc0[a][2])
    assert best0 == "explore", f"无信号时 explore 应最优，实际 {best0} {sc0}"

    s1 = hb._initial_state()
    s1["low_quality_count"] = 1     # 存在低质量失败
    s1["template_count"] = 8       # explore 已饱和（边际产出→0）
    s1["simplify_deltas"] = [0, 0]  # 化简无潜力
    sc1 = hb._roi_scores(s1)
    best1 = max(sc1, key=lambda a: sc1[a][2])
    assert best1 == "retry", f"有低质量失败时 retry 应最优，实际 {best1} {sc1}"
    print(f"✓ ROI 评分器：无失败→{best0}(score={sc0[best0][2]:.2f})；"
          f"低质量失败→{best1}(score={sc1[best1][2]:.2f})（修回失败最值钱）")

    # 2) 真实贪选择：失败在 + 饱和 + digit>3（不触发探索扰动）→ 应直选 retry
    ar, ns = hb.f_roi(7, s1)
    assert ar["action"] == "retry", f"f_roi 应选 retry，实际 {ar['action']}"
    assert ns.get("last_roi_choice") == "retry" and "last_roi_scores" in ns
    print(f"✓ ROI 贪选择：digit=7（非探索档）→ {ar['action']} "
          f"（跳过纯贪探索，直击失败；评分={ns['last_roi_scores']}）")

    # 3) 连续良态 → idle_pressure 到顶 → 自我节流 idle（心跳自身 ROI 趋零·不贪）
    hb2 = PiHeartbeat(tm=None, interval=0.01, roi_guided=True)
    for _ in range(ROI_IDLE_CAP):
        hb2.record_task_roi(tokens=300, seconds=0.5, quality=0.98)
    assert hb2.state.get("idle_pressure", 0) >= ROI_IDLE_CAP, \
        "连续良态应累积 idle_pressure 到上限"
    out2 = hb2.run_once(n=6)
    idle = [o for o in out2 if o["action"] == "idle"]
    assert idle, f"连续良态下 ROI 应自我节流(idle)，实际 {[o['action'] for o in out2]}"
    # 新失败信号清零压力 → 恢复贪（不节流）
    hb2.record_task_roi(tokens=2000, seconds=3.0, quality=0.4,
                        failed_nodes=["r1"], low_quality=True)
    assert hb2.state["idle_pressure"] == 0, "新失败应清零 idle_pressure，恢复贪"
    print(f"✓ ROI 自我节流：连续良态→idle×{len(idle)}；新失败→压力清零、恢复贪"
          f"（心跳自身 ROI 趋零则不贪·省算力）")


def _selftest_mentor_trigger():
    """构造临时 store 的失败案例，验证 digit==9 时训练闭环真被 π 拉起并固化。"""
    import json as _json
    import tempfile
    try:
        from execution_store import ExecutionStore
    except Exception:
        print("  (跳过 mentor 触发实测：execution_store 不可用)")
        return
    if mentor_train_cycle is None:
        print("  (跳过 mentor 触发实测：mentor 模块不可用)")
        return

    db = os.path.join(tempfile.mkdtemp(), "pi_mentor.db")
    store = ExecutionStore(db)
    spec = _json.loads(_json.dumps(_DEMO_SPEC))
    spec["components"]["pwr"] = {"type": "power", "label": "pwr"}
    store.save("pi-fail-001", "π触发的失败任务", "failed", spec, [],
               {"final_quality": 0.2, "failed_nodes": ["r1"]}, ["pi-heartbeat"])

    # mock 导师：返回结构化优化方案（不走网络；http_post 契约是 f(messages)）
    def _fake_post(messages):
        plan = {"diagnosis": "r1 模型档过低导致抽取失败",
                "node_fixes": [{"cid": "r1", "model": "large",
                                "prompt": "逐条抽取关键信息，输出 JSON"}],
                "topology_ops": [],
                "rationale": "升档 + 明确指令可显著提升抽取成功率"}
        return {"choices": [{"message": {"content": _json.dumps(plan, ensure_ascii=False)}}]}

    # mock 学生：返回真实感的非空输出，交由 default_content_quality 打分
    class _FakeStudent:
        pass

    def _fake_rerun(_spec):
        return {"final_quality": 0.70, "success": True, "failed_nodes": [],
                "outputs": {"r1": "抽取结果：项目A 预算 120 万，负责人张三，交付 2026-09-01。",
                            "r2": "抽取结果：项目B 预算 80 万，负责人李四，交付 2026-10-15。"}}

    hb = PiHeartbeat(tm=None, interval=0.01, mentor_store=store,
                     mentor_http_post=_fake_post)
    # 直接把学生注入到闭环（心跳内部走 student_backend，这里用 monkey 方式替换）
    _orig = hb._mentor_train

    def _patched(state, digit):
        from mentor import mentor_train_cycle as _cyc, default_content_quality as _q
        res = {"action": "mentor", "digit": digit}
        c = _cyc(store, http_post=_fake_post, student_rerun_fn=_fake_rerun,
                 registry=hb.mentor_registry, quality_fn=_q)
        res.update({"diagnosis": c.get("diagnosis"),
                    "before_quality": c.get("before_quality"),
                    "after_quality": c.get("after_quality"),
                    "quality_gate_passed": c.get("quality_gate_passed"),
                    "quality_gate_reason": c.get("quality_gate_reason")})
        state["mentor_runs"] = state.get("mentor_runs", 0) + 1
        if c.get("quality_gate_passed"):
            state["mentor_solidified"] = state.get("mentor_solidified", 0) + 1
        return res

    hb._mentor_train = _patched
    out = hb.run_once(n=6)  # π: 3,1,4,1,5,9 → 第6拍 digit=9 触发
    men = [o for o in out if o["action"] == "mentor"]
    assert men, f"6 拍内应触发一次 mentor（π 第6位=9），实际 {[o['digit'] for o in out]}"
    r = men[0]["result"]
    assert r.get("quality_gate_passed"), f"质量门应通过：{r.get('quality_gate_reason')}"
    assert hb.state["mentor_solidified"] == 1, "通过后应固化 1 条"
    assert len(hb.mentor_registry) == 1, "registry 应写入 1 条模板"
    print(f"✓ π 拉起训练闭环：digit=9 → 诊断「{r['diagnosis']}」 "
          f"质量 {r['before_quality']}→{r['after_quality']} 门通过，固化 1 条模板")
    hb._mentor_train = _orig


if __name__ == "__main__":
    pi_heartbeat_selftest()
