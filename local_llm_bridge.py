#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 transformers / modelscope 模型的 OpenAI 兼容桥。

让 circuit-agents 的 ``OllamaBackend(api_mode="openai")`` 能通过 HTTP 驱动
**纯 transformers 部署**的本地模型（无 Ollama、无 vLLM、无 API key）。
本脚本跑在装有 torch/transformers/modelscope 的 venv 里（本机即
``~/.workbuddy/binaries/python/envs/llm``）；circuit-agents 自己
跑在另一个 venv，二者通过 127.0.0.1 上的 HTTP 解耦。

端点（OpenAI 兼容）：
    POST /v1/chat/completions   {"model","messages","temperature","max_tokens",...}
    GET  /v1/models
    GET  /health

用法：
    python local_llm_bridge.py \
        --model-path "~/llm/models/models/Qwen--Qwen2.5-1.5B-Instruct" \
        --host 127.0.0.1 --port 8000 --offline

说明：
    - 桥忽略请求里的 ``model`` 字段，永远用启动时加载的模型生成（这样
      circuit-agents 把 small/large/tool/code 都映射成同一个本地模型也能跑）。
    - 仅用标准库 http.server 暴露服务，不引入 fastapi/uvicorn 等额外依赖。
    - ``--offline`` 设置 TRANSFORMERS_OFFLINE / HF_HUB_OFFLINE，避免任何联网。
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MODEL_PATH = os.path.expanduser("~/llm/models/models/Qwen--Qwen2.5-1.5B-Instruct")
MODEL_ID = "qwen2.5-1.5b"  # 对外暴露的模型名（与请求里的 model 字段无关）

# 运行时被 load_model 填充
MODEL = None
TOK = None
DEV = "cpu"
_MAX_NEW_TOKENS = 512


def _resolve_model_dir(model_path):
    """modelscope 缓存布局：<repo>/snapshots/<hash>/ 才含真实文件（tokenizer.json 等）。
    若传入的是仓库顶层目录（只有 snapshots/），自动下钻到第一个快照子目录。"""
    if os.path.exists(os.path.join(model_path, "tokenizer.json")) or \
       os.path.exists(os.path.join(model_path, "config.json")):
        return model_path
    snaps = os.path.join(model_path, "snapshots")
    if os.path.isdir(snaps):
        sub = [d for d in os.listdir(snaps)
               if os.path.isdir(os.path.join(snaps, d))]
        if sub:
            return os.path.join(snaps, sub[0])
    return model_path


def load_model(model_path, offline):
    """加载本地 transformers 模型。重依赖延迟导入，便于 --help 不加载 torch。"""
    if offline:
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    model_path = _resolve_model_dir(model_path)
    from transformers import AutoModelForCausalLM, AutoTokenizer
    print(f"[bridge] loading {model_path} ...", flush=True)
    tok = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_path, torch_dtype="auto", trust_remote_code=True
    )
    model.eval()
    try:
        import torch
        if torch.cuda.is_available():
            model.to("cuda")
            dev = "cuda"
        else:
            dev = "cpu"
    except Exception:
        dev = "cpu"
    print(f"[bridge] model loaded on {dev}", flush=True)
    return model, tok, dev


def generate(messages, temperature=0.2, max_new_tokens=512):
    """用 Qwen chat 模板拼装并生成。返回纯文本。"""
    import torch
    text = TOK.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = TOK(text, return_tensors="pt").to(DEV)
    do_sample = temperature > 1e-3
    gen_kwargs = dict(
        max_new_tokens=max_new_tokens,
        do_sample=do_sample,
        top_p=0.9,
        pad_token_id=TOK.eos_token_id,
    )
    if do_sample:
        gen_kwargs["temperature"] = temperature
    with torch.no_grad():
        out = MODEL.generate(**inputs, **gen_kwargs)
    gen = out[0][inputs["input_ids"].shape[1]:]
    return TOK.decode(gen, skip_special_tokens=True).strip()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *a):
        pass  # 静默

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {"status": "ok", "model": MODEL_ID, "device": DEV})
        elif path == "/v1/models":
            self._send(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}],
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw or b"{}")
        except Exception:
            self._send(400, {"error": "bad request"})
            return

        messages = req.get("messages", []) or []
        temperature = float(req.get("temperature", 0.2) or 0.2)
        max_new = int(req.get("max_tokens", req.get("max_new_tokens", _MAX_NEW_TOKENS))
                      or _MAX_NEW_TOKENS)
        try:
            content = generate(messages, temperature, max_new)
        except Exception as e:  # 生成失败 → 500，让 circuit-agents 走降级
            self._send(500, {"error": f"generation failed: {e}"})
            return

        self._send(200, {
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "created": 0,
            "model": MODEL_ID,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


def main():
    ap = argparse.ArgumentParser(description="本地 transformers 模型的 OpenAI 兼容桥")
    ap.add_argument("--model-path", default=DEFAULT_MODEL_PATH,
                    help="本地模型目录（modelscope/transformers 缓存路径）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--max-new-tokens", type=int, default=512)
    ap.add_argument("--offline", action="store_true",
                    help="设置 TRANSFORMERS_OFFLINE/HF_HUB_OFFLINE，禁止任何联网")
    args = ap.parse_args()

    global MODEL, TOK, DEV, _MAX_NEW_TOKENS
    _MAX_NEW_TOKENS = args.max_new_tokens
    MODEL, TOK, DEV = load_model(args.model_path, args.offline)

    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"[bridge] {MODEL_ID} ready on {DEV} -> "
          f"http://{args.host}:{args.port}/v1/chat/completions", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down", flush=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
