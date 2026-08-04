"""多模态真视听觉转录层（Phase 2 ② 加深 ④）。

把图片 / 语音附件转写为文本，使后续编译能基于**真实内容**路由（而非占位标记）。

设计原则（与项目一致）：
- **离线安全**：无任何 key / 网络时，所有模态回退到占位描述，绝不抛异常、不阻塞主流程。
- **可插拔后端**：默认注册 `offline`（占位）后端；真实后端（多模态 LLM 做视觉、STT 做听觉）
  通过 `register(modality, name, fn)` 注入，并用 `set_default` 切换；缺失/异常自动降级占位。
- **纯函数 + 线程友好**：`transcribe` 不持有可变共享状态；后端表为实例级。

真实后端如何接（留给用户配 key 后启用，不在离线自检里跑）：
    from compiler.multimodal import MultimodalTranscriber, llm_vision_backend
    tr = MultimodalTranscriber()
    tr.register("image", "vision", llm_vision_backend(api_key=KEY, model="gpt-4o-mini"))
    tr.set_default("image", "vision")
    # audio 同理 register("audio", "stt", stt_backend(...))
"""

import os
import base64
import json
import urllib.request


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".aiff", ".oga"}


def _modality_of(path: str):
    ext = os.path.splitext(path or "")[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return None


class MultimodalTranscriber:
    """把附件（图片/语音）转写为文本的可插拔转录器。

    每个模态（image/audio）持有独立的后端注册表 + 一个默认后端名。
    `transcribe` / `transcribe_all` 永不抛异常：后端缺失或执行出错都降级为占位描述。
    """

    def __init__(self, default_backend: dict = None):
        self._backends = {"image": {}, "audio": {}}
        # 内置离线占位后端（始终可用）
        self.register("image", "offline", self._offline_image)
        self.register("audio", "offline", self._offline_audio)
        self._default = {"image": "offline", "audio": "offline"}
        if default_backend:
            for mod, name in (default_backend or {}).items():
                if mod in self._default:
                    self._default[mod] = name

    # ---- 后端注册 / 切换 ----
    def register(self, modality: str, name: str, fn):
        """登记一个后端。fn(name_or_path: str) -> str（返回描述/转录文本）。"""
        if modality not in self._backends:
            raise ValueError(f"未知模态 {modality!r}（应为 image/audio）")
        self._backends[modality][name] = fn

    def set_default(self, modality: str, name: str):
        """把某模态的默认后端切换为已登记的 name。"""
        if modality not in self._backends:
            raise ValueError(f"未知模态 {modality!r}")
        if name not in self._backends[modality]:
            raise KeyError(f"后端 {name!r} 未在模态 {modality!r} 注册")
        self._default[modality] = name

    def backends(self, modality: str = None):
        if modality:
            return list(self._backends.get(modality, {}).keys())
        return {m: list(b.keys()) for m, b in self._backends.items()}

    # ---- 转录 ----
    def transcribe(self, attachment, backend: str = None) -> dict:
        """转录单个附件（{type?, name}）。返回带 transcription/backend/offline 的附件 dict。

        绝不抛异常：后端缺失/出错 → 占位描述 + offline=True。
        """
        if isinstance(attachment, str):
            attachment = {"name": attachment}
        att = dict(attachment) if isinstance(attachment, dict) else {"name": str(attachment)}
        name = att.get("name") or ""
        modality = att.get("type") or _modality_of(name)
        if modality not in ("image", "audio"):
            return {**att, "transcription": f"[未知附件类型: {name}]",
                    "backend": "offline", "offline": True}
        bname = backend or self._default.get(modality, "offline")
        fn = self._backends[modality].get(bname) or self._backends[modality].get("offline")
        try:
            text = fn(name) if callable(fn) else ""
            if not text:
                text = f"[{modality} 待识别: {name}]"
            return {**att, "transcription": text, "backend": bname,
                    "offline": (bname == "offline")}
        except Exception:
            ph = f"[{modality} 识别失败(已降级占位): {name}]"
            return {**att, "transcription": ph, "backend": bname, "offline": True}

    def transcribe_all(self, attachments) -> list:
        """批量转录（忽略 None / 空列表）。"""
        if not attachments:
            return []
        return [self.transcribe(a) for a in attachments]

    # ---- 内置离线占位后端 ----
    @staticmethod
    def _offline_image(name: str) -> str:
        return f"[图片: {name} 描述待识别(离线占位)]"

    @staticmethod
    def _offline_audio(name: str) -> str:
        return f"[语音: {name} 转录待识别(离线占位)]"


# ============================================================================
# 真实后端工厂（离线自检不调用；用户配 key / 本地模型后启用）
# ============================================================================

def llm_vision_backend(api_key=None, base_url=None, model=None,
                        http_post=None, timeout=60.0):
    """多模态 LLM 视觉后端工厂：图片（本地文件 base64 内联）→ 文本描述。

    返回 callable(name) -> str。离线 / 无 key 时调用会抛异常，由 transcriber 自动降级占位。
    仅支持本地存在的图片文件（读取后 base64，避免外链）。
    """
    import os as _os
    base = (base_url or os.environ.get("AGENT_API_BASE")
            or "https://api.openai.com/v1").rstrip("/")
    mdl = model or "gpt-4o-mini"

    def _call(name: str) -> str:
        if not _os.path.exists(name):
            raise FileNotFoundError(f"图片文件不存在: {name}")
        with open(name, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        ext = _os.path.splitext(name)[1].lower().lstrip(".")
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
            "svg": "image/svg+xml",
        }.get(ext, "image/png")
        url = base + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": mdl,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请简要描述这张图片的内容，用于后续任务规划。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "max_tokens": 512,
        }
        if http_post:
            resp = http_post(url, headers, body)
        else:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode("utf-8"))
        return (resp.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

    return _call


def stt_backend(api_key=None, base_url=None, model=None,
                http_post=None, timeout=120.0):
    """语音转写（STT）后端工厂：返回 callable(name) -> str。

    通用 ASR 端点模式：把音频文件以 multipart/form-data 上传，解析 {text} 字段。
    未配置 / 不支持的格式 → 抛异常，由 transcriber 降级占位。**真实 STT 服务需用户自备**。
    """
    import os as _os
    base = (base_url or "").rstrip("/")

    def _call(name: str) -> str:
        if not base:
            raise RuntimeError("未配置 STT 端点（base_url 为空）→ 无法做真实语音转录")
        if not _os.path.exists(name):
            raise FileNotFoundError(f"音频文件不存在: {name}")
        # 简易 multipart 上传（标准库实现，无第三方依赖）
        boundary = "----circuitagentssttboundary"
        fname = _os.path.basename(name)
        with open(name, "rb") as f:
            payload = f.read()
        crlf = b"\r\n"
        body = (
            b"--" + boundary.encode() + crlf
            + f'Content-Disposition: form-data; name="file"; filename="{fname}"'.encode()
            + crlf
            + b"Content-Type: application/octet-stream" + crlf + crlf
            + payload + crlf
            + b"--" + boundary.encode() + b"--" + crlf
        )
        url = base + "/transcribe"
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if http_post:
            resp = http_post(url, headers, body)
        else:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode("utf-8"))
        if isinstance(resp, dict):
            return (resp.get("text") or resp.get("transcript")
                    or resp.get("results", {}).get("transcript") or "").strip()
        return str(resp).strip()

    return _call


# ============================================================================
# 离线自检
# ============================================================================

def multimodal_selftest():
    # 1) 离线占位：图片/语音都回退，不抛异常
    tr = MultimodalTranscriber()
    r1 = tr.transcribe({"type": "image", "name": "chart.png"})
    assert r1["offline"] is True and "待识别" in r1["transcription"], "图片离线应占位"
    r2 = tr.transcribe({"type": "audio", "name": "meeting.wav"})
    assert r2["offline"] is True and "待识别" in r2["transcription"], "语音离线应占位"
    print("✓ 离线占位：图片/语音均回退描述，transcription 非空且 offline=True")

    # 2) 扩展名自动识别模态
    r3 = tr.transcribe({"name": "photo.JPG"})
    assert "图片" in r3["transcription"], "扩展名应识别为 image"
    r4 = tr.transcribe({"name": "voice.MP3"})
    assert "语音" in r4["transcription"], "扩展名应识别为 audio"
    print("✓ 扩展名自动识别模态（.JPG→image / .MP3→audio）")

    # 3) 可插拔后端：注册真实后端并切换默认，transcribe 走真实后端
    called = {}
    def _fake_vision(name):
        called["img"] = name
        return f"这是一张关于 {name} 的柱状图"
    tr.register("image", "vision", _fake_vision)
    tr.set_default("image", "vision")
    r5 = tr.transcribe({"type": "image", "name": "sales.png"})
    assert r5["backend"] == "vision" and "柱状图" in r5["transcription"], "应走 vision 后端"
    assert called.get("img") == "sales.png", "vision 后端应被调用"
    print("✓ 可插拔后端：注册 vision 并 set_default 后走真实后端")

    # 4) 后端异常自动降级（不抛）
    def _boom(name):
        raise RuntimeError("model down")
    tr.register("audio", "stt", _boom)
    tr.set_default("audio", "stt")
    r6 = tr.transcribe({"type": "audio", "name": "call.wav"})
    assert r6["offline"] is True and "降级" in r6["transcription"], "后端异常应降级占位"
    print("✓ 后端异常自动降级（不抛异常，回退占位）")

    # 5) transcribe_all 批量
    batch = tr.transcribe_all([{"name": "a.png"}, {"name": "b.wav"}])
    assert len(batch) == 2 and all("transcription" in x for x in batch), "批量应逐条转录"
    print("✓ transcribe_all 批量转录")

    # 6) 工厂可构造（不调用即不联网）：llm_vision_backend / stt_backend 返回 callable
    vb = llm_vision_backend(api_key="x")
    sb = stt_backend()
    assert callable(vb) and callable(sb), "工厂应返回可调用后端"
    print("✓ 真实后端工厂可构造（llm_vision_backend / stt_backend）")

    print("\nmultimodal 离线自检全部通过 ✓")


if __name__ == "__main__":
    multimodal_selftest()
