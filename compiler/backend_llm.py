"""
circuit-agents · compiler.backend_llm
====================================
补强#2（= M5 的"接真后端"）：RealLLMBackend —— 把"原子 agent 步骤(resistor)"
从随机模拟换成真实的 OpenAI-compatible LLM 调用；其余结构件仍走 SimBackend 的
确定性实现（电容/调度器/ADC 不该也不该用 LLM 去"跑"）。

设计要点（诚实边界）：
 · 抽象接缝已在 runtime.Backend / Circuit(backend=) 就位，本类只是加一个子类，
   传播逻辑 / 分层延迟逻辑零改动 —— 内核稳定。
 · 默认行为已翻转（2026-08-03）：提供 get_default_backend() 工厂——解析到 key
   （显式参数 > 环境变量 DEEPSEEK/OPENAI/AGENT > ~/Desktop/key_tmp.txt）就走真模型，
   无 key 才退回 SimBackend。即『技能默认调用 apikey，除非没有 apikey』。
   若你显式传 SimBackend，仍强制离线对照。
 · 无 key 也能"离线验证"：用注入式 _http_post 假响应 + dry_run（组装 prompt/请求但不发）
   证明接线正确；真·在线调用用解析到的 key（优先 DEEPSEEK_API_KEY / AGENT_API_KEY / AGENT_API_BASE）。
 · LLM 输出质量无法被自动精确度量 —— quality 用 per-tier 能力上限(cap) 作先验，
   这是已知近似，不是模拟漏洞。
 · 开路语义延续内核：若上游全开路(inp<=0)，直接返回开路，不浪费真调用。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import random

from runtime import Signal, SimBackend, Backend


# tier → 真实模型（OpenAI-compatible；可自行覆盖，也覆盖自托管 endpoint 的模型名）
DEFAULT_MODEL_MAP = {
    "small": "gpt-4o-mini",
    "large": "gpt-4o",
    "tool":  "gpt-4o-mini",   # 工具/函数调用档；如需 tool-calling 换成对应模型
}

# 检测到 DeepSeek 基址时自动套用的模型映射（自托管/国产兼容 endpoint 无需手填 model_map）
DEFAULT_DEEPSEEK_MAP = {
    "small": "deepseek-chat",
    "large": "deepseek-reasoner",   # 高质诉求档用推理模型
    "tool":  "deepseek-chat",
}


def _auto_model_map(base_url):
    """按 base_url 自动选默认模型映射；未知 endpoint 回退 OpenAI 默认。"""
    if base_url and "deepseek" in base_url.lower():
        return dict(DEFAULT_DEEPSEEK_MAP)
    return dict(DEFAULT_MODEL_MAP)


# 默认 API key 文件位置（沿用 _demo_llm_agents_run.py / _verify_real.py 约定）：
# 用户在 ~/Desktop/key_tmp.txt 放明文 key。**绝不 print、绝不进对话/命令/日志**。
KEY_FILE_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "key_tmp.txt")


def resolve_api_key(api_key=None):
    """统一解析 API key：显式参数 > 环境变量(DEEPSEEK/OPENAI/AGENT) > 本地 key 文件。

    返回非空字符串表示『有 key』（默认走真实 LLM 后端）；空串表示『无 key』
    （调用方应回退离线/规则兜底，不触网）。结果去 BOM 与首尾空白。
    """
    if api_key is not None:
        return str(api_key).strip().lstrip("\ufeff")
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AGENT_API_KEY"):
        v = os.environ.get(env)
        if v:
            return v.strip().lstrip("\ufeff")
    try:
        with open(KEY_FILE_PATH, "r", encoding="utf-8") as f:
            return f.read().strip().lstrip("\ufeff")
    except OSError:
        return ""


def get_default_backend(api_key=None, rng=None, **kw):
    """默认后端工厂：解析到 key 就返回真 LLM 后端(LLMAgentBackend)，否则退回 SimBackend(mock)。

    对应『技能默认调用 apikey，除非没有 apikey』——有 key 才烧真模型，无 key 不触网。
    延迟 import LLMAgentBackend 以避免与 llm_agents 的循环依赖。
    """
    key = resolve_api_key(api_key)
    if key:
        from compiler.llm_agents import LLMAgentBackend
        kw.pop("api_key", None)
        return LLMAgentBackend(api_key=key, **kw)
    return SimBackend(rng or random.Random(0))

# 每 1K token 的近似单价（USD）：仅用于成本估计，非账单级精确
_PER_1K_COST = {
    "gpt-4o-mini": (0.00015, 0.00060),   # (输入价, 输出价)
    "gpt-4o":      (0.00250, 0.01000),
}


class RealLLMBackend(SimBackend):
    """真实 LLM 后端：resistor → OpenAI-compatible chat/completions；其余组件确定性。"""

    def __init__(self, rng=None, api_key=None, base_url=None, model_map=None,
                 timeout=60.0, dry_run=False, http_post=None):
        super().__init__(rng if rng is not None else random.Random(0))
        self.api_key = resolve_api_key(api_key)
        self.base_url = (base_url or os.environ.get("AGENT_API_BASE")
                         or os.environ.get("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model_map = model_map or _auto_model_map(base_url)
        self.timeout = timeout
        self.dry_run = dry_run
        self._http_post = http_post   # 注入式：离线测试用假响应 / 计数

    # ---- 工具 ----
    def _resolve_model(self, tier):
        return self.model_map.get(tier, self.model_map["small"])

    def _tier_cap(self, tier):
        return self._TIERS.get(tier, self._TIERS["small"])["accuracy"]

    @staticmethod
    def _estimate_cost(model, usage):
        if usage and ("prompt_tokens" in usage or "total_tokens" in usage):
            pt = usage.get("prompt_tokens", 0) or 0
            ct = usage.get("completion_tokens", 0) or 0
            in_rate, out_rate = _PER_1K_COST.get(model, (0.001, 0.002))
            return round((pt / 1000.0) * in_rate + (ct / 1000.0) * out_rate, 6)
        return 0.0

    @staticmethod
    def _render_value(v, depth=0):
        """把上游信号值递归展开成可读文本（汇合节点的 value 是 Signal 列表，
        直接 str() 会得到嵌套 Signal(...)，喂给 LLM 是垃圾——这里解包。"""
        if v is None:
            return ""
        if isinstance(v, Signal):
            return RealLLMBackend._render_value(v.value, depth + 1)
        if isinstance(v, (list, tuple)):
            if depth >= 3:
                return f"[{len(v)} items]"
            return "\n".join(RealLLMBackend._render_value(x, depth + 1)
                             for x in v if x is not None)
        return str(v)

    def _build_messages(self, comp, inputs):
        ctx = []
        for s in inputs:
            if s is not None and s.ok and s.value is not None:
                rendered = self._render_value(s.value).strip()
                if rendered:
                    ctx.append(rendered)
        label = comp.get("label", comp.get("model", "step"))
        system = ("You are a single atomic agent step inside a circuit-style "
                  "multi-agent workflow. Given the upstream context, produce the "
                  "best possible result for the described step. Be concise and correct.")
        user = f"Step: {label}\n"
        if ctx:
            user += "Upstream context:\n" + "\n".join(f"- {c}" for c in ctx) + "\n"
        user += "Deliver the result now."
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _post(self, url, headers, body):
        if self._http_post:
            return self._http_post(url, headers, body)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- 主入口 ----
    def run(self, comp, inputs):
        if comp.get("type") != "resistor":
            # 结构件：电容/调度器/ADC/桥… 走确定性实现，不动
            return super().run(comp, inputs)

        # ---- resistor：真实 LLM 路径 ----
        # 开路语义延续内核：上游全死则不浪费真调用，直接开路
        inp = max((s.quality for s in inputs if s.ok), default=0.0)
        if inp <= 0.0:
            return Signal(value=None, quality=0.0, ok=False,
                          cost=0.0, latency_ms=0.0,
                          meta={"open": "no_input", "input": 0.0})

        tier = comp.get("model", "small")
        model = self._resolve_model(tier)
        messages = self._build_messages(comp, inputs)
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": model, "messages": messages, "temperature": 0.2}

        if self.dry_run:
            # 组装请求但不发送：返回带请求信息的 Signal，便于离线检视 prompt/形态
            return Signal(value=f"[dry-run] {model}", quality=0.0, ok=True,
                          cost=0.0, latency_ms=0.0,
                          meta={"dry_run": True, "model": model, "url": url,
                                "request": body, "messages": messages})

        t0 = time.time()
        try:
            resp = self._post(url, headers, body)
            dt = (time.time() - t0) * 1000.0
            choice = (resp.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content", "") or ""
            finish = choice.get("finish_reason", "")
            usage = resp.get("usage") or {}
            ok = bool(content) and finish != "error"
            cap = comp.get("accuracy", self._tier_cap(tier))
            quality = cap if ok else 0.0   # 已知近似：LLM 输出质量无法自动精确度量
            cost = self._estimate_cost(model, usage)
            return Signal(value=content, quality=quality, ok=ok,
                          cost=cost, latency_ms=round(dt, 1),
                          meta={"model": model, "tier": tier,
                                "finish_reason": finish, "usage": usage})
        except Exception as e:  # 网络/鉴权/超时等 → 开路（与 yield_fail 同语义）
            dt = (time.time() - t0) * 1000.0
            return Signal(value=None, quality=0.0, ok=False,
                          cost=0.0, latency_ms=round(dt, 1),
                          meta={"open": "http_error", "error": str(e)})


# ---------------------------------------------------------------------------
# 离线自检（无 key 也能跑）：parity / dry_run / 注入假响应 / 错误路径 / 开路语义
# ---------------------------------------------------------------------------
def selftest():
    import runtime as rt
    sim = rt.SimBackend(random.Random(7))
    real = RealLLMBackend(rng=random.Random(7))

    # 1) 结构件 parity：同种子下 RealLLMBackend 非 resistor 与 SimBackend 完全一致
    struct_comps = [
        {"type": "power", "label": "task"},
        {"type": "opamp", "spec_clarify": True},
        {"type": "source", "quality": 0.95},
        {"type": "capacitor", "label": "merge"},
        {"type": "capacitor", "mode": "any", "label": "rmerge"},
        {"type": "adc", "threshold": 0.8},
        {"type": "format_adapter", "from_fmt": "raw", "to_fmt": "struct", "kind": "adc"},
        {"type": "bridge_rectifier", "label": "bridge"},
    ]
    ins = [rt.Signal(value="x", quality=0.9, ok=True)]
    for c in struct_comps:
        a = sim.run(c, ins)
        b = real.run(c, ins)
        assert (a.ok, round(a.quality, 6), round(a.cost, 6), round(a.latency_ms, 6)) == \
               (b.ok, round(b.quality, 6), round(b.cost, 6), round(b.latency_ms, 6)), \
               f"parity fail on {c['type']}: {a} vs {b}"
    print("✓ parity: 非 resistor 组件与 SimBackend 完全一致")

    # 2) resistor dry_run：组装正确请求但不发送
    comp = {"type": "resistor", "label": "reason", "model": "large"}
    rins = [rt.Signal(value="ctx-1", quality=0.9, ok=True),
            rt.Signal(value="ctx-2", quality=0.8, ok=True)]
    dry = RealLLMBackend(rng=random.Random(0), dry_run=True)
    s = dry.run(comp, rins)
    assert s.meta["dry_run"] is True
    assert s.meta["model"] == "gpt-4o"
    assert any("ctx-1" in m["content"] for m in s.meta["messages"]
               if m["role"] == "user")
    assert s.meta["request"]["model"] == "gpt-4o"
    print("✓ dry_run: 请求形态正确（model=gpt-4o，prompt 含上游 ctx）")

    # 3) resistor 注入假响应：映射成 Signal
    fake_resp = {
        "choices": [{"message": {"content": "LLM 产出文本"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    }

    def fake_post(url, headers, body):
        assert url.endswith("/chat/completions")
        assert headers.get("Authorization") is None  # 无 key 时不应带鉴权头
        return fake_resp

    llm = RealLLMBackend(rng=random.Random(0), api_key="", http_post=fake_post)
    s2 = llm.run(comp, rins)
    assert s2.ok is True
    assert s2.value == "LLM 产出文本"
    assert s2.quality == llm._tier_cap("large")   # cap 先验
    assert s2.cost > 0
    print(f"✓ 注入假响应(无 key): ok=True, value 映射正确, cost={s2.cost}, lat={s2.latency_ms}ms")

    # 3b) 有 key 时应带鉴权头（默认行为：解析到 key 即走真实后端）
    def fake_post2(url, headers, body):
        assert headers.get("Authorization", "").startswith("Bearer "), \
            "提供 key 时请求必须带 Bearer Authorization"
        return fake_resp

    llm_key = RealLLMBackend(rng=random.Random(0), api_key="dummy-key", http_post=fake_post2)
    llm_key.run(comp, rins)
    print("✓ 鉴权头(有 key): 提供 key 时请求带 Bearer Authorization")

    # 4) resistor 错误路径（HTTP 异常）→ 开路
    def bad_post(url, headers, body):
        raise urllib.error.URLError("connection refused")

    llm_err = RealLLMBackend(rng=random.Random(0), http_post=bad_post)
    s3 = llm_err.run(comp, rins)
    assert s3.ok is False and s3.meta.get("open") == "http_error"
    print("✓ 错误路径: HTTP 异常 → 开路(open=http_error)")

    # 5) 开路语义：上游全死 → 不开真调用（用计数验证）
    calls = {"n": 0}

    def counting_post(url, headers, body):
        calls["n"] += 1
        return fake_resp

    llm2 = RealLLMBackend(rng=random.Random(0), http_post=counting_post)
    dead = [rt.Signal(value=None, quality=0.0, ok=False)]
    s4 = llm2.run({"type": "resistor", "label": "x", "model": "tool"}, dead)
    assert s4.ok is False and s4.meta.get("open") == "no_input"
    assert calls["n"] == 0, "上游全死时不应发起真调用"
    print("✓ 开路语义: 上游全死 → 直接开路，未发起真 LLM 调用")

    print("\n全部离线自检通过 ✓（无需 API key）")


if __name__ == "__main__":
    selftest()
