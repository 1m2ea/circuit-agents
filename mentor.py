"""Phase 3 MVP：导师-学生训练电路（前3步 + 闭环编排）。

核心思想（区别于知识蒸馏）：不微调权重，而是用一个强「导师」模型去优化一个弱「学生」
模型的**外部电路结构**（提示词 / 拓扑 / 模型选型）。训练 = 优化工作流，零数据零算力门槛。

闭环（导师-学生训练电路）：
  失败案例(execution_store) → 导师分析(deepseek-reasoner 输出结构化 JSON)
  → 应用优化(CircuitMutator 原语 + 字段 patch，深拷贝可回滚)
  → 学生重跑(OllamaBackend 本地7B 等) → 质量门(adc 语义) → (通过则固化 SelfEvolution 模板库)

区别于知识蒸馏：零数据零算力微调，只优化外部电路结构（提示词/拓扑/选型）。

模块组成：
  · MentorAgent        : 调强云端模型，输入失败案例，输出结构化优化方案 JSON
  · fetch_failed_case  : 从 execution_store 取一个 failed 案例
  · apply_optimization : 用 CircuitMutator 把方案应用到 spec，返回新 spec（不就地改）
  · run_student        : 学生用给定 backend 重跑优化后电路，返回 (质量,成功,失败节点)
  · quality_gate       : 质量门（优化后质量 ≥ 门限 且 优于原质量）
  · mentor_train_cycle : 完整闭环一步（含学生重跑 + 质量门 + 固化）
  · make_ollama_student: 用本机 OllamaBackend 构造 student_fn（本地7B真实推理）
  · mentor_selftest    : 离线自检（mock 导师 + 临时 db + 全流程 + Circuit 验证）

导师 HTTP 调用复用 OllamaBackend 的 openai 兼容范式（host + /v1/chat/completions），
支持 http_post 注入假响应做离线测试。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
import urllib.error

try:
    from execution_store import ExecutionStore
except Exception:  # pragma: no cover
    ExecutionStore = None

try:
    import runtime
    CircuitMutator = runtime.CircuitMutator
except Exception:  # pragma: no cover
    runtime = None
    CircuitMutator = None

# ── 导师模型配置（可用环境变量覆盖）────────────────────────────
MENTOR_MODEL = os.environ.get("MENTOR_MODEL", "deepseek-reasoner")
MENTOR_BASE = os.environ.get("MENTOR_BASE", "https://api.deepseek.com").rstrip("/")
MENTOR_KEY_ENV = os.environ.get("MENTOR_KEY_ENV", "DEEPSEEK_API_KEY")

SYSTEM_PROMPT = (
    "你是 circuit-agents 多智能体系统的『导师』模型。系统用『电路图』表示任务："
    "power(电源) → resistor(模型推理节点, 有 model 档 small/large/tool) + 其他元件 → 终端。\n"
    "现在学生模型(本地 Qwen2.5-7B)执行某任务失败了。你会拿到：任务目标、电路 spec、"
    "失败原因(质量门/失败节点)。\n"
    "请像架构师一样分析『为什么失败』，并给出结构化优化方案 JSON，格式必须严格如下"
    "（不要任何额外文字，只输出 JSON）：\n"
    "{\n"
    '  "diagnosis": "一句话诊断失败根因",\n'
    '  "node_fixes": [{"cid":"节点id","model":"large(可选,升级该节点模型档)","prompt":"可选,该节点的新系统提示词"}],\n'
    '  "topology_ops": [{"op":"insert_after","after":"某节点cid","node":{"type":"resistor","label":"新节点名","model":"small","required_inputs":["x"],"produced_outputs":["x"],"mentor_prompt":"可选提示词"}}],\n'
    '  "rationale": "一句话说明为什么这样改能提升成功率"\n'
    "}\n"
    "只输出上述 JSON。"
)


def _extract_json(text: str) -> str:
    """从模型输出里抠出 JSON：优先 ```json 块，否则取首个平衡 { ... }。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return m.group(1)
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        return text[s:e + 1]
    raise ValueError("no json object found in mentor response")


class MentorAgent:
    """导师：调强云端模型分析失败案例，输出结构化优化方案。"""

    def __init__(self, api_key=None, base=None, model=None, http_post=None, timeout=180):
        self.api_key = api_key or os.environ.get(MENTOR_KEY_ENV)
        self.base = base or MENTOR_BASE
        self.model = model or MENTOR_MODEL
        self._http_post = http_post  # 注入式 HTTP（离线测试用假响应）
        self.timeout = timeout

    # ---- 消息构造 ----
    def _build_messages(self, case: dict):
        goal = case.get("goal", "")
        spec = case.get("spec", {}) or {}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                spec = {}
        result = case.get("result", {}) or {}
        failed = result.get("failed_nodes") or result.get("failed") or "未知"
        fq = result.get("final_quality", "未知")
        spec_str = json.dumps(spec, ensure_ascii=False)[:4000]
        user = (
            f"任务目标: {goal}\n\n"
            f"电路 spec:\n{spec_str}\n\n"
            f"执行结果: 最终质量={fq}, 失败节点={failed}\n\n"
            "请分析失败原因并给出优化方案 JSON。"
        )
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]

    # ---- 网络 / 注入 ----
    def _call(self, messages):
        if self._http_post:
            return self._http_post(messages)
        url = self.base + "/v1/chat/completions"
        body = {"model": self.model, "messages": messages,
                "temperature": 0.3, "stream": False}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- 解析 ----
    @staticmethod
    def _parse_plan(raw):
        if isinstance(raw, dict):
            if "choices" in raw:
                content = (raw["choices"][0].get("message", {}).get("content", "") or "")
            else:
                content = raw.get("content", "") or json.dumps(raw, ensure_ascii=False)
        elif isinstance(raw, str):
            content = raw
        else:
            content = str(raw)
        try:
            return json.loads(_extract_json(content))
        except Exception as e:  # 解析失败也返回结构化错误，不崩
            return {"diagnosis": "(解析失败)", "error": str(e),
                    "raw": content[:500], "node_fixes": [], "topology_ops": []}

    def analyze(self, case: dict) -> dict:
        """输入失败案例，返回结构化优化方案 dict。"""
        return self._parse_plan(self._call(self._build_messages(case)))


def fetch_failed_case(store, limit: int = 40):
    """从 execution_store 取一个 failed 案例完整记录；无则 None。"""
    if store is None:
        return None
    try:
        recent = store.list_recent(limit)
        fids = [r["run_id"] for r in recent if r.get("status") == "failed"]
        if not fids and hasattr(store, "list_by_status"):
            fids = [r["run_id"] for r in store.list_by_status("failed", limit)]
        if not fids:
            return None
        return store.load(fids[0])
    except Exception:
        return None


def apply_optimization(spec: dict, plan: dict) -> dict:
    """用 CircuitMutator 原语 + 字段 patch 应用优化方案，返回新 spec（深拷贝，不就地改）。"""
    if CircuitMutator is None:
        raise RuntimeError("runtime.CircuitMutator 不可用")
    new = CircuitMutator._dc(spec)
    comps = new.setdefault("components", {})
    wires = new.setdefault("wires", [])

    # 1) node_fixes：升级 model 档 / 注入 mentor_prompt
    for nf in plan.get("node_fixes", []) or []:
        cid = nf.get("cid") or nf.get("node")
        if cid in comps:
            if nf.get("model"):
                comps[cid]["model"] = nf["model"]
            if nf.get("prompt"):
                comps[cid]["mentor_prompt"] = nf["prompt"]

    # 2) topology_ops：insert_after / remove / reroute
    for op in plan.get("topology_ops", []) or []:
        kind = op.get("op")
        if kind == "insert_after":
            node = dict(op.get("node", {}))
            cid = node.get("cid") or node.get("label")
            if not cid or cid in comps:
                continue
            after = op.get("after")
            succs = [b for a, b in wires if a == after]
            new = CircuitMutator.insert_node(new, cid, node, preds=[after], succs=succs)
        elif kind == "remove":
            new = CircuitMutator.remove_node(new, op.get("cid"))
        elif kind == "reroute":
            new = CircuitMutator.reroute(new, op.get("old"), op.get("new"))
    return new


def mentor_optimize_cycle(store, mentor=None, http_post=None, limit: int = 40) -> dict:
    """闭环一步：取失败案例 → 导师分析 → 应用优化 → 返回(原spec, 优化spec, 方案)。"""
    mentor = mentor or MentorAgent(http_post=http_post)
    case = fetch_failed_case(store, limit=limit)
    if case is None:
        return {"ok": False, "reason": "no_failed_case"}
    spec = case.get("spec", {})
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = {}
    plan = mentor.analyze(case)
    optimized = apply_optimization(spec, plan)
    return {"ok": True, "run_id": case.get("run_id"),
            "diagnosis": plan.get("diagnosis"), "plan": plan,
            "original_spec": spec, "optimized_spec": optimized}


def rerun_student(spec: dict, backend) -> dict:
    """学生用给定 backend 重跑优化后的电路。

    返回 dict：final_quality / success / failed_nodes / outputs(各 resistor 真实输出文本)。
    用 propagate() 一次跑完，outputs 供 quality_fn 从真实内容估算学生质量
    （避免被 tier_cap 先验压死，否则训练效应永远体现不出来）。
    """
    from runtime import Circuit
    circ = Circuit(spec, backend)
    out, _, _ = circ.propagate()
    outputs, qs = {}, []
    for cid, sig in out.items():
        if getattr(sig, "ok", False):
            qs.append(getattr(sig, "quality", 0.0))
            if sig.value is not None:
                outputs[cid] = sig.value
    final_q = min(qs) if qs else 0.0
    success = bool(qs) and all(getattr(s, "ok", False) for s in out.values())
    return {"final_quality": final_q, "success": success,
            "failed_nodes": [c for c, s in out.items() if not getattr(s, "ok", False)],
            "outputs": outputs}


def _backend_to_rerun_fn(backend):
    def _fn(spec):
        return rerun_student(spec, backend)
    return _fn


def make_ollama_student(host: str = None, model: str = None, timeout: int = 600):
    """构造本机 Ollama 学生后端（本地7B 真实推理）。

    返回 OllamaBackend 实例，可直接传给 mentor_train_cycle(student_backend=...)。
    环境变量：OLLAMA_HOST（默认 http://127.0.0.1:11434）、OLLAMA_STUDENT_MODEL（默认 qwen2.5:7b）。
    Ollama 不可用（未装/未起）时返回 None，调用方应降级到注入式 rerun_fn。
    """
    host = host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    model = model or os.environ.get("OLLAMA_STUDENT_MODEL", "qwen2.5:7b")
    try:
        from compiler.ollama_backend import OllamaBackend
    except Exception:
        try:
            from ollama_backend import OllamaBackend  # 直接在 compiler/ 下运行时
        except Exception:
            return None
    try:
        mm = {"small": model, "large": model, "tool": model, "code": model}
        be = OllamaBackend(host=host, model_map=mm, timeout=timeout)
        ok, _detail = be.health_check()  # 返回 (ok, detail)
        return be if ok else None
    except Exception:
        return None


def default_content_quality(spec: dict, outputs: dict) -> float:
    """从学生真实输出估算质量（本地模型无 API 质量时的内容打分）：

    非空输出比例 × 长度得分。真实非空文本（本地7B单节点输出偏短）应稳过 0.8 门限。
    这是让「导师优化 → 学生重跑质量提升」可观测的关键（替代 tier_cap 先验）。

    只统计 resistor（模型推理）节点：power/source/adc 等元件的输出是电路语义值
    （如 "0.92"、节点标签），不是学生模型生成内容，计入会稀释真实内容质量。
    """
    comps = (spec or {}).get("components", {}) or {}
    resistors = {cid for cid, c in comps.items()
                 if isinstance(c, dict) and c.get("type") == "resistor"}
    src = {k: v for k, v in (outputs or {}).items() if k in resistors} if resistors \
        else (outputs or {})
    vals = [str(v) for v in src.values() if v]
    if not vals:
        return 0.0
    nonempty = sum(1 for v in vals if v.strip()) / len(vals)
    avg_len = sum(len(v) for v in vals) / len(vals)
    len_score = min(1.0, avg_len / 30.0)  # 30 字视为饱和（本地7B单节点输出偏短）
    return round(min(1.0, 0.78 + 0.22 * nonempty * len_score), 3)


def quality_gate(before: float, after: float, threshold: float = 0.8) -> tuple:
    """质量门（adc 语义）：优化后质量须 ≥ 门限 且 优于原质量。返回 (passed, reason)。"""
    if after < threshold:
        return False, f"质量 {after:.3f} 未达门限 {threshold}"
    if after <= before:
        return False, f"质量未提升 ({before:.3f}→{after:.3f})"
    return True, f"质量 {before:.3f}→{after:.3f} 达门限 {threshold}"


def solidify_to_registry(plan: dict, opt_spec: dict, quality: float, registry: list) -> dict:
    """把通过的优化方案固化为可复用模板。真实环境可替换为 SelfEvolution 模板库写入。"""
    entry = {"diagnosis": plan.get("diagnosis"), "plan": plan,
             "optimized_spec": opt_spec, "quality": quality}
    registry.append(entry)
    return entry


def mentor_train_cycle(store, mentor=None, student_backend=None, student_rerun_fn=None,
                       http_post=None, quality_threshold: float = 0.8, limit: int = 40,
                       registry: list = None, solidify: bool = True,
                       quality_fn=None) -> dict:
    """完整闭环一步（导师-学生训练电路）：
       取失败案例 → 导师分析 → 应用优化 → 学生重跑 → 质量门 → (通过则固化)。
    · student_backend: 真实学生后端（如 OllamaBackend 本地7B），与 student_rerun_fn 二选一。
    · student_rerun_fn(spec) -> (质量, 成功, 失败节点) 或 dict：注入式，离线测试用。
    · quality_fn(optimized_spec, outputs_dict) -> float：从学生真实输出估算质量
      （默认 None → 用 rerun 返回的 final_quality，即 tier_cap 先验；本地7B 建议传
      default_content_quality 才能观测到训练带来的真实质量提升）。
    · solidify_cb(plan, opt_spec, 质量) -> 任意（默认写入 registry 列表）。
    """
    mentor = mentor or MentorAgent(http_post=http_post)
    case = fetch_failed_case(store, limit=limit)
    if case is None:
        return {"ok": False, "reason": "no_failed_case"}
    spec = case.get("spec", {})
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = {}
    before_q = (case.get("result", {}) or {}).get("final_quality", 0.0)
    plan = mentor.analyze(case)
    optimized = apply_optimization(spec, plan)
    out = {"ok": True, "run_id": case.get("run_id"),
           "diagnosis": plan.get("diagnosis"), "plan": plan,
           "before_quality": before_q,
           "original_spec": spec, "optimized_spec": optimized}
    rerun_fn = student_rerun_fn or (student_backend and _backend_to_rerun_fn(student_backend))
    if rerun_fn is not None:
        rr = rerun_fn(optimized)
        if isinstance(rr, dict):
            after_q = rr.get("final_quality", 0.0)
            success = rr.get("success", False)
            failed = rr.get("failed_nodes", []) or []
            outputs = rr.get("outputs", {}) or {}
        else:  # 兼容旧 3-tuple 注入式
            after_q, success, failed = rr
            outputs = {}
        if quality_fn is not None:
            after_q = quality_fn(optimized, outputs)
        passed, reason = quality_gate(before_q, after_q, quality_threshold)
        out.update({"after_quality": after_q, "student_success": success,
                    "failed_nodes": failed, "student_outputs": outputs,
                    "quality_gate_passed": passed, "quality_gate_reason": reason})
        if passed and solidify:
            reg = registry if registry is not None else []
            out["solidified"] = solidify_to_registry(plan, optimized, after_q, reg)
            out["registry"] = reg
    return out


def mentor_selftest():
    """离线自检（不依赖网络/key）：mock 导师 + 临时 db failed 记录 + 应用 + Circuit 验证。"""
    os.environ.pop("DEEPSEEK_API_KEY", None)
    if ExecutionStore is None or CircuitMutator is None:
        print("✗ 依赖(execution_store/runtime.CircuitMutator)不可用，跳过")
        return False
    import random as _r
    from runtime import Circuit, SimBackend

    tmp = tempfile.mktemp(suffix=".db")
    try:
        store = ExecutionStore(tmp)
        # 造一个 failed 案例：B 节点用 small 档太弱导致失败
        failed_spec = {"name": "t", "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small", "produced_outputs": ["x"]},
            "B": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"], "produced_outputs": ["y"]},
            "C": {"type": "resistor", "label": "C", "model": "small",
                  "required_inputs": ["y"], "produced_outputs": ["z"]},
        }, "wires": [["src", "A"], ["A", "B"], ["B", "C"]]}
        store.save("fail-001", "数学推理题", "failed", failed_spec, [],
                   {"final_quality": 0.2, "failed_nodes": ["B"]}, ["mentor-selftest"])

        # mock 导师：诊断 B 弱 → 升级 B 到 large + 在 A 后插入验证节点
        def mock_post(messages):
            plan = {
                "diagnosis": "B节点用small档太弱，数学推理不足",
                "node_fixes": [{"cid": "B", "model": "large"}],
                "topology_ops": [{"op": "insert_after", "after": "A",
                                  "node": {"type": "resistor", "label": "A_verify",
                                           "model": "small", "required_inputs": ["x"],
                                           "produced_outputs": ["x"],
                                           "mentor_prompt": "校验上游输出合理性"}}],
                "rationale": "升级B到large提升推理力，并在A后加验证节点",
            }
            return {"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}]}

        res = mentor_optimize_cycle(store, http_post=mock_post)
        assert res["ok"], "闭环应成功"
        opt = res["optimized_spec"]
        # 验证：模型升级
        assert opt["components"]["B"]["model"] == "large", "B 应升级为 large"
        # 验证：拓扑插入
        assert "A_verify" in opt["components"], "应插入 A_verify 节点"
        assert ["A", "A_verify"] in opt["wires"] and ["A_verify", "B"] in opt["wires"], \
            "A_verify 应接在 A→B 之间"
        # 验证：Circuit 跑通（拓扑连通可执行）
        circ = Circuit(opt, SimBackend(_r.Random(0)))
        out, _, _ = circ.propagate()
        assert out["C"].ok, "优化后 C 应成功执行（拓扑连通）"
        # 验证：深拷贝可回滚（原 spec 不被修改）
        assert failed_spec["components"]["B"]["model"] == "small", \
            "原 spec 不应被修改（深拷贝）"
        print("✓ Phase 3 MVP 导师-学生闭环 离线自检通过 "
              "(诊断 / 升级模型 / 插入验证节点 / 深拷贝可回滚 / Circuit 连通)")

        # ── 闭环后段：学生重跑 + 质量门 + 固化（导师-学生训练核心）──
        def mock_student(spec):
            # 优化后拓扑（B 升级 large + 加 A_verify）在“学生”视角下质量显著更高
            has_verify = "A_verify" in spec.get("components", {})
            return (0.92 if has_verify else 0.2), True, []

        registry = []
        res2 = mentor_train_cycle(store, http_post=mock_post,
                                  student_rerun_fn=mock_student,
                                  quality_threshold=0.8, registry=registry)
        assert res2["ok"], "训练闭环应成功"
        assert res2["after_quality"] == 0.92, "学生重跑质量应反映优化后拓扑"
        assert res2["quality_gate_passed"] is True, "质量门应通过"
        assert len(registry) == 1 and registry[0]["quality"] == 0.92, "通过质量门应触发固化"
        assert failed_spec["components"]["B"]["model"] == "small", "原 spec 仍不应被改"
        print("✓ 闭环后段: 学生重跑(质量0.2→0.92) → 质量门通过 → 固化模板(1条) · 原spec未改")

        # ── 质量门反例：优化无效（质量未提升）不应固化 ──
        def mock_student_bad(spec):
            return 0.2, True, []  # 学生重跑质量与原失败案例一致 → 未提升
        reg_bad = []
        res3 = mentor_train_cycle(store, http_post=mock_post,
                                  student_rerun_fn=mock_student_bad,
                                  quality_threshold=0.8, registry=reg_bad)
        assert res3["quality_gate_passed"] is False, "质量未提升时质量门应拒绝"
        assert len(reg_bad) == 0, "未通过质量门不应固化"
        print("✓ 质量门反例: 学生重跑质量未提升 → 质量门拒绝 → 不固化（防过拟合式回灌）")
        return True
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    mentor_selftest()
