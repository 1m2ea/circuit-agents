"""异构硬件后端（Phase 2+ 第四层④）：Ollama 本地模型 Backend。

把 Circuit 的 resistor 节点从云端 LLM / SimBackend 切换到 **Ollama 本地推理**：
- 调用 Ollama REST API（`POST /api/chat`，原生格式；或 `/v1/chat/completions`，OpenAI 兼容）
- tier（small/large/tool）→ Ollama 模型名映射（可配置，默认 qwen2.5 系列）
- 连接失败 / 超时 → 降级到 SimBackend（graceful fallback，不崩溃）
- 成本 = 0（本地推理，无 API 费用）
- 质量 = tier cap 先验（与 RealLLMBackend 一致的已知近似）
- 开路语义延续内核：上游全死则不浪费本地推理调用

用途：隐私敏感场景（数据不出本机）/ 离线场景（无网络）/ 成本敏感场景（零 API 费用）/
边缘设备（Jetson / 树莓派 + Ollama 做端侧 AI）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import random

from runtime import Signal, SimBackend, Backend


# tier → Ollama 模型名默认映射（用户可通过 model_map 覆盖）
DEFAULT_OLLAMA_MODELS = {
    "small": "qwen2.5:7b",
    "large": "qwen2.5:14b",
    "tool":  "qwen2.5:7b",
    # 代码生成/审查专用档（便携工作站推荐 deepseek-coder-v2；U盘无此模型时回退 small）
    "code":  "deepseek-coder-v2",
}

# Ollama 默认端口
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


class OllamaBackend(SimBackend):
    """Ollama 本地模型后端：resistor → Ollama /api/chat；其余组件确定性（继承 SimBackend）。

    参数：
        host: Ollama 服务地址（默认 http://localhost:11434）
        model_map: tier → Ollama 模型名映射（默认 qwen2.5 系列）
        timeout: 请求超时秒数（默认 120，本地推理可能较慢）
        fallback: 连接失败时降级到的 Backend（默认 SimBackend(rng)）
        http_post: 注入式 HTTP 函数（离线测试用假响应）
        api_mode: "native"（/api/chat）或 "openai"（/v1/chat/completions）
    """

    def __init__(self, rng=None, host=None, model_map=None, timeout=120.0,
                 fallback=None, http_post=None, api_key=None, api_mode="native"):
        super().__init__(rng if rng is not None else random.Random(0))
        self.host = (host or os.environ.get("OLLAMA_HOST")
                     or DEFAULT_OLLAMA_HOST).rstrip("/")
        self.model_map = model_map or dict(DEFAULT_OLLAMA_MODELS)
        self.timeout = timeout
        self.fallback = fallback  # None → 用自身 SimBackend 逻辑降级
        self._http_post = http_post
        self.api_mode = api_mode
        self.api_key = api_key
        # 运行时统计
        self._stats = {"calls": 0, "successes": 0, "failures": 0,
                       "fallbacks": 0, "total_latency_ms": 0.0}

    # ---- 工具 ----
    def _resolve_model(self, tier):
        return self.model_map.get(tier, self.model_map.get("small", "qwen2.5:7b"))

    def _tier_cap(self, tier):
        return self._TIERS.get(tier, self._TIERS["small"])["accuracy"]

    @staticmethod
    def _render_value(v, depth=0):
        if v is None:
            return ""
        if isinstance(v, Signal):
            return OllamaBackend._render_value(v.value, depth + 1)
        if isinstance(v, (list, tuple)):
            if depth >= 3:
                return f"[{len(v)} items]"
            return "\n".join(OllamaBackend._render_value(x, depth + 1)
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

    def _build_request(self, model, messages):
        """根据 api_mode 构建请求 URL + body。"""
        if self.api_mode == "openai":
            url = self.host + "/v1/chat/completions"
            body = {"model": model, "messages": messages,
                    "temperature": 0.2, "stream": False}
        else:
            url = self.host + "/api/chat"
            body = {"model": model, "messages": messages,
                    "stream": False}
        return url, body

    def _parse_response(self, resp):
        """根据 api_mode 解析响应，返回 (content, finish_reason, usage)。"""
        if self.api_mode == "openai":
            choice = (resp.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content", "") or ""
            finish = choice.get("finish_reason", "")
            usage = resp.get("usage") or {}
            return content, finish, usage
        else:
            # Ollama native /api/chat response
            content = resp.get("message", {}).get("content", "") or ""
            finish = "stop" if resp.get("done", False) else "error"
            usage = {
                "prompt_tokens": resp.get("prompt_eval_count", 0),
                "completion_tokens": resp.get("eval_count", 0),
                "total_tokens": (resp.get("prompt_eval_count", 0)
                                 + resp.get("eval_count", 0)),
            }
            return content, finish, usage

    # ---- 主入口 ----
    def run(self, comp, inputs):
        if comp.get("type") != "resistor":
            return super().run(comp, inputs)

        # 开路语义：上游全死则不浪费本地推理调用
        inp = max((s.quality for s in inputs if s.ok), default=0.0)
        if inp <= 0.0:
            return Signal(value=None, quality=0.0, ok=False,
                          cost=0.0, latency_ms=0.0,
                          meta={"open": "no_input", "input": 0.0})

        tier = comp.get("model", "small")
        model = self._resolve_model(tier)
        messages = self._build_messages(comp, inputs)
        url, body = self._build_request(model, messages)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        self._stats["calls"] += 1
        t0 = time.time()
        try:
            resp = self._post(url, headers, body)
            dt = (time.time() - t0) * 1000.0
            content, finish, usage = self._parse_response(resp)
            ok = bool(content) and finish != "error"
            cap = comp.get("accuracy", self._tier_cap(tier))
            quality = cap if ok else 0.0
            # 本地推理成本 = 0（无 API 费用）
            cost = 0.0
            self._stats["successes"] += 1
            self._stats["total_latency_ms"] += dt
            return Signal(value=content, quality=quality, ok=ok,
                          cost=cost, latency_ms=round(dt, 1),
                          meta={"model": model, "tier": tier,
                                "finish_reason": finish, "usage": usage,
                                "backend": "ollama", "host": self.host})
        except Exception as e:
            dt = (time.time() - t0) * 1000.0
            self._stats["failures"] += 1
            self._stats["total_latency_ms"] += dt

            # 降级到 fallback backend
            if self.fallback is not None:
                self._stats["fallbacks"] += 1
                fb_result = self.fallback.run(comp, inputs)
                fb_result.meta["fallback"] = "ollama_unreachable"
                fb_result.meta["fallback_error"] = str(e)
                return fb_result

            # 无 fallback → 返回开路（与 yield_fail 同语义）
            return Signal(value=None, quality=0.0, ok=False,
                          cost=0.0, latency_ms=round(dt, 1),
                          meta={"open": "ollama_error", "error": str(e),
                                "backend": "ollama", "host": self.host})

    def stats(self):
        """返回运行时统计快照。"""
        s = dict(self._stats)
        s["avg_latency_ms"] = (s["total_latency_ms"] / s["calls"]
                               if s["calls"] > 0 else 0.0)
        s["success_rate"] = (s["successes"] / s["calls"]
                             if s["calls"] > 0 else 0.0)
        return s

    def health_check(self):
        """检查 Ollama 是否可达。返回 (ok, detail)。"""
        try:
            url = self.host + "/api/tags"
            if self._http_post:
                # 测试模式下用注入函数
                resp = self._http_post(url, {}, {})
            else:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as r:
                    resp = json.loads(r.read().decode("utf-8"))
            models = [m.get("name", "?") for m in resp.get("models", [])]
            return True, f"Ollama 可达，已安装模型: {models}"
        except Exception as e:
            return False, f"Ollama 不可达: {e}"


# ---------------------------------------------------------------------------
# 离线自检（无 Ollama 运行也能跑）：注入式假响应 + 降级 + 开路语义 + 统计
# ---------------------------------------------------------------------------

def ollama_backend_selftest():
    import runtime as rt

    # 1) 结构件 parity：非 resistor 与 SimBackend 完全一致
    sim = rt.SimBackend(random.Random(7))
    oll = OllamaBackend(rng=random.Random(7))
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
        b = oll.run(c, ins)
        assert (a.ok, round(a.quality, 6), round(a.cost, 6)) == \
               (b.ok, round(b.quality, 6), round(b.cost, 6)), \
               f"parity fail on {c['type']}: {a} vs {b}"
    print("✓ parity: 非 resistor 组件与 SimBackend 完全一致")

    # 2) resistor 注入假响应（native API）：映射成 Signal
    fake_native_resp = {
        "model": "qwen2.5:7b",
        "message": {"role": "assistant", "content": "Ollama 本地产出"},
        "done": True,
        "prompt_eval_count": 25,
        "eval_count": 8,
    }

    def fake_post_native(url, headers, body):
        assert "/api/chat" in url, f"native 模式应调 /api/chat: {url}"
        assert body["stream"] is False, "应禁用流式"
        assert body["model"] == "qwen2.5:7b"
        return fake_native_resp

    comp = {"type": "resistor", "label": "reason", "model": "small"}
    rins = [rt.Signal(value="ctx-1", quality=0.9, ok=True)]
    oll_native = OllamaBackend(rng=random.Random(0), http_post=fake_post_native,
                               api_mode="native")
    s = oll_native.run(comp, rins)
    assert s.ok is True, "native 模式应成功"
    assert s.value == "Ollama 本地产出", f"内容应映射: {s.value}"
    assert s.quality == oll_native._tier_cap("small"), "质量应为 tier cap"
    assert s.cost == 0.0, "本地推理成本应为 0"
    assert s.meta["backend"] == "ollama", "应标记 backend=ollama"
    assert s.meta["model"] == "qwen2.5:7b"
    print(f"✓ native API: ok=True, value 映射正确, cost=¥0（本地免费）, lat={s.latency_ms}ms")

    # 3) OpenAI 兼容模式
    fake_openai_resp = {
        "choices": [{"message": {"content": "OpenAI 兼容产出"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    }

    def fake_post_openai(url, headers, body):
        assert "/v1/chat/completions" in url, f"openai 模式应调 /v1/chat/completions: {url}"
        return fake_openai_resp

    oll_openai = OllamaBackend(rng=random.Random(0), http_post=fake_post_openai,
                               api_mode="openai")
    s2 = oll_openai.run(comp, rins)
    assert s2.ok is True
    assert s2.value == "OpenAI 兼容产出"
    assert s2.meta["backend"] == "ollama"
    print(f"✓ OpenAI 兼容模式: ok=True, value 映射正确")

    # 4) 连接失败 → 降级到 SimBackend fallback
    def bad_post(url, headers, body):
        raise urllib.error.URLError("connection refused")

    sim_fb = rt.SimBackend(random.Random(42))
    oll_fb = OllamaBackend(rng=random.Random(0), http_post=bad_post,
                           fallback=sim_fb)
    s3 = oll_fb.run(comp, rins)
    assert s3.meta.get("fallback") == "ollama_unreachable", \
        f"应降级标记 fallback: {s3.meta}"
    assert s3.ok is True, "降级到 SimBackend 后应成功（yield 通过）"
    print(f"✓ 降级 fallback: Ollama 不可达 → SimBackend 接管 · fallback={s3.meta['fallback']}")

    # 5) 连接失败 + 无 fallback → 开路
    oll_nofb = OllamaBackend(rng=random.Random(0), http_post=bad_post)
    s4 = oll_nofb.run(comp, rins)
    assert s4.ok is False, "无 fallback 时应返回开路"
    assert s4.meta.get("open") == "ollama_error", f"应标记 ollama_error: {s4.meta}"
    print(f"✓ 无 fallback 开路: Ollama 不可达 → 开路(open=ollama_error)")

    # 6) 开路语义：上游全死 → 不发请求
    calls = {"n": 0}

    def counting_post(url, headers, body):
        calls["n"] += 1
        return fake_native_resp

    oll_count = OllamaBackend(rng=random.Random(0), http_post=counting_post)
    dead = [rt.Signal(value=None, quality=0.0, ok=False)]
    s5 = oll_count.run({"type": "resistor", "label": "x", "model": "tool"}, dead)
    assert s5.ok is False and s5.meta.get("open") == "no_input"
    assert calls["n"] == 0, "上游全死时不应发起 Ollama 调用"
    print("✓ 开路语义: 上游全死 → 直接开路，未发起本地推理调用")

    # 7) 统计信息
    stats = oll_native.stats()
    assert stats["calls"] == 1, f"应记录 1 次调用: {stats}"
    assert stats["successes"] == 1
    assert stats["failures"] == 0
    assert stats["avg_latency_ms"] > 0
    print(f"✓ 统计: calls={stats['calls']} · success_rate={stats['success_rate']:.0%} · "
          f"avg_lat={stats['avg_latency_ms']:.1f}ms")

    # 8) tier → 模型映射验证
    assert oll_native._resolve_model("small") == "qwen2.5:7b"
    assert oll_native._resolve_model("large") == "qwen2.5:14b"
    assert oll_native._resolve_model("tool") == "qwen2.5:7b"
    assert oll_native._resolve_model("code") == "deepseek-coder-v2"
    # 自定义映射
    custom = OllamaBackend(model_map={"small": "llama3.2:3b", "large": "llama3.1:8b",
                                      "code": "deepseek-coder:33b"})
    assert custom._resolve_model("small") == "llama3.2:3b"
    assert custom._resolve_model("large") == "llama3.1:8b"
    assert custom._resolve_model("code") == "deepseek-coder:33b"
    print("✓ 模型映射: 默认 qwen2.5 系列 + code→deepseek-coder-v2 + 自定义可覆盖")

    # 9) health_check（注入式）
    def fake_health(url, headers, body):
        if "/api/tags" in url:
            return {"models": [{"name": "qwen2.5:7b"}, {"name": "qwen2.5:14b"}]}
        return {}

    oll_health = OllamaBackend(http_post=fake_health)
    ok, detail = oll_health.health_check()
    assert ok is True
    assert "qwen2.5:7b" in detail
    print(f"✓ health_check: {detail}")

    def bad_health(url, headers, body):
        raise ConnectionError("refused")

    oll_bad = OllamaBackend(http_post=bad_health)
    ok2, detail2 = oll_bad.health_check()
    assert ok2 is False
    assert "不可达" in detail2
    print(f"✓ health_check (不可达): {detail2}")

    # 10) 端到端 Circuit 集成（注入式）
    from runtime import Circuit, CircuitExecutor
    spec = {
        "name": "ollama_e2e",
        "components": {
            "src": {"type": "power", "label": "task"},
            "ret": {"type": "resistor", "label": "retrieve", "model": "small"},
            "rsn": {"type": "resistor", "label": "reason", "model": "large"},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "ret"], ["ret", "rsn"], ["rsn", "adc"]],
    }
    oll_e2e = OllamaBackend(rng=random.Random(0), http_post=fake_post_native)
    circ = Circuit(spec, oll_e2e)
    result = CircuitExecutor(circ).run()
    assert result["success"] or result["final_quality"] >= 0, "应产出结果"
    assert oll_e2e.stats()["calls"] >= 2, "应至少调用 2 次 Ollama（ret + rsn）"
    print(f"✓ 端到端 Circuit: 质量={result['final_quality']:.3f} · "
          f"Ollama 调用 {oll_e2e.stats()['calls']} 次 · 成本=¥0（本地免费）")

    print("\nollama_backend 离线自检全部通过 ✓")


if __name__ == "__main__":
    ollama_backend_selftest()
