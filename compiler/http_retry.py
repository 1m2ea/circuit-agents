"""HTTP 重试的共享实现 —— 供各真后端复用，避免退避逻辑多处实现而逐渐分叉。

背景：circuit-agents 的并行会把瞬时请求数放大成「批量路数 × 层内节点数」，
真 LLM 链路撞 RPM 上限会被整体锁接口冷却（瓶颈在频率、不在 token 总量）。
因此每个真后端都必须做到两件事：

    ① 发请求前过全局双闸门 RATE_LIMITER（并发闸门压峰值 + RPM 令牌桶压频率）
    ② 撞 429 / 5xx 时按 Retry-After 或指数退避重试

② 是本模块的职责，抽成一处供 RealLLMBackend(backend_llm.py) 与
OllamaBackend(ollama_backend.py) 共用 —— 这两个后端是**并列的两个类**
（OllamaBackend 直接继承 SimBackend，不走 backend_llm），若各自实现重试，
行为迟早分叉成两个版本的限流语义。
"""
from __future__ import annotations

import time
import urllib.error

from runtime import retry_wait, RL_MAX_RETRIES


def post_with_retry(post_fn, url, headers, body,
                    max_retries=None, sleep_fn=None):
    """带 429/5xx 退避重试的 POST 包装。

    · 429（限流）与 5xx（服务端抖动）才重试；其他 4xx 属请求本身有问题，重试无益。
    · 优先尊重服务端 Retry-After（封顶，防超大值拖死）；缺失或非法则指数退避。
    · URLError（网络层：掉线 / DNS / 拒绝连接）快速失败不重试 —— 把断网当限流
      去空等是纯粹浪费，应尽快开路让上层质量门 / 反馈环接管。

    post_fn : (url, headers, body) -> dict，由调用方注入（兼容测试用 _http_post 注入）。
    sleep_fn: 可注入的睡眠函数，默认 time.sleep（自检注入以断言退避时长、不真等待）。
    """
    tries = RL_MAX_RETRIES if max_retries is None else int(max_retries)
    _sleep = sleep_fn if sleep_fn is not None else time.sleep
    last = None
    for attempt in range(tries + 1):
        try:
            return post_fn(url, headers, body)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                raise
            if attempt >= tries:
                raise
            _sleep(retry_wait(attempt, (e.headers or {}).get("Retry-After")))
        except urllib.error.URLError:
            # HTTPError 是 URLError 的子类，已在上一分支处理，此处仅剩真网络错误
            raise
    raise last


def http_retry_selftest():
    """退避重试离线自检：伪造 HTTPError，不发真实请求、不真等待。"""
    slept: list = []
    calls = {"n": 0}

    def _http_error(code, headers=None):
        return urllib.error.HTTPError("http://test", code, "boom",
                                      headers or {}, None)

    # ① 429 + Retry-After：退避后重试至成功，等待时长取服务端给定值
    def post_429_then_ok(url, headers, body):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429, {"Retry-After": "0"})
        return {"ok": True}

    calls["n"] = 0
    slept.clear()
    out = post_with_retry(post_429_then_ok, "http://x", {}, {}, sleep_fn=slept.append)
    assert out == {"ok": True}, "429 重试后应返回成功响应"
    assert calls["n"] == 3, f"应调用 3 次（2 次失败 + 1 次成功），实际 {calls['n']}"
    assert slept == [0.0, 0.0], f"等待时长应取自 Retry-After，实际 {slept}"
    print(f"✓ 退避重试：429 连撞 2 次后第 3 次成功（等待 {slept}，取自 Retry-After）")

    # ② 4xx（非 429）立即失败不重试：请求本身有问题，重试纯浪费
    calls["n"] = 0

    def post_404(url, headers, body):
        calls["n"] += 1
        raise _http_error(404)

    try:
        post_with_retry(post_404, "http://x", {}, {}, sleep_fn=slept.append)
        raise AssertionError("404 不应被静默吞掉")
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"应原样抛出 404，实际 {e.code}"
    assert calls["n"] == 1, f"404 不该重试，实际调用 {calls['n']} 次"
    print("✓ 退避重试：404 立即失败不重试（仅 1 次调用）")

    # ③ URLError（网络层）快速失败：断网不当限流空等
    calls["n"] = 0

    def post_netfail(url, headers, body):
        calls["n"] += 1
        raise urllib.error.URLError("connection refused")

    try:
        post_with_retry(post_netfail, "http://x", {}, {}, sleep_fn=slept.append)
        raise AssertionError("网络错误不应被静默吞掉")
    except urllib.error.URLError:
        pass
    assert calls["n"] == 1, f"网络错误不该重试，实际调用 {calls['n']} 次"
    print("✓ 退避重试：URLError 快速失败不重试（断网不当限流空等）")

    # ④ 重试次数耗尽 → 抛出最后一次错误，不静默降级
    calls["n"] = 0

    def post_always_429(url, headers, body):
        calls["n"] += 1
        raise _http_error(429, {"Retry-After": "0"})

    try:
        post_with_retry(post_always_429, "http://x", {}, {}, sleep_fn=slept.append)
        raise AssertionError("重试耗尽后应抛出错误")
    except urllib.error.HTTPError as e:
        assert e.code == 429, f"应抛出最后一次 429，实际 {e.code}"
    assert calls["n"] == RL_MAX_RETRIES + 1, \
        f"应共调用 1+{RL_MAX_RETRIES}={RL_MAX_RETRIES + 1} 次，实际 {calls['n']}"
    print(f"✓ 退避重试：重试 {RL_MAX_RETRIES} 次耗尽后抛出（共 {calls['n']} 次调用）")


if __name__ == "__main__":
    http_retry_selftest()
    print("http_retry 离线自检全部通过 ✓")
