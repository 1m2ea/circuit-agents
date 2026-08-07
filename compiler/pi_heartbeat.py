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
                 mentor_http_post=None, mentor_enabled=True):
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

    def f(self, pi_digit, state):
        if pi_digit == MENTOR_TRIGGER_DIGIT:
            action_result = self._mentor_train(state, pi_digit)
        elif pi_digit <= self.BAND_EXPLORE:
            action_result = self._explore(state, pi_digit)
        elif pi_digit <= self.BAND_SIMPLIFY:
            action_result = self._simplify(state, pi_digit)
        else:
            action_result = self._retry(state, pi_digit)
        new_state = self._update_state(state, action_result)
        return action_result, new_state

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

    def tick(self):
        """推进一拍：取 π 下一位 → f() → 更新 state。返回本拍快照。"""
        with self._lock:
            digit = self.spigot.next_digit()
            action_result, self.state = self.f(digit, self.state)
            return {
                "digit": digit,
                "action": action_result.get("action"),
                "result": action_result,
                "state": self._public_state(),
            }

    def _public_state(self):
        s = dict(self.state)
        s["history"] = len(s.get("history", []))  # 不暴露大历史，只给长度
        return s

    def run_once(self, n=1):
        return [self.tick() for _ in range(n)]

    # ── 后台永动循环 ──

    def start(self, interval=None):
        if interval is not None:
            self.interval = interval
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

    print("\nπ 永动心跳 离线自检全部通过 ✓")


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
