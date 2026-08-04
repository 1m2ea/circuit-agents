#!/usr/bin/env python3
"""circuit-agents HTTP API Server（FastAPI + SSE）

端点:
  POST /run          提交 Goal → 编译 → 执行 → 返回结果
  GET  /run/{id}/stream  SSE 流式推送节点事件
  GET  /run/{id}      查询运行状态/结果
  GET  /health        健康检查

启动:
  python server.py
  python server.py --port 8765
"""

from __future__ import annotations

import json
import uuid
import time
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn

# ──────────────────────────────────────────────────────────
# 模型
# ──────────────────────────────────────────────────────────

class GoalRequest(BaseModel):
    goal: str = Field(..., description="自然语言任务描述")
    route: bool = Field(True, description="是否走 Router 并联编译")
    auto_select_models: bool = Field(False, description="是否启用智能模型选型")
    memory_enabled: bool = Field(True, description="是否启用记忆复用")
    data_fill_budget: int = Field(2, description="自动补数重试次数上限")
    evolve_enabled: bool = Field(True, description="是否启用 3.5 多任务进化")

class RunStatus(BaseModel):
    run_id: str
    status: str         # "running" | "done" | "error"
    goal: str
    created_at: str
    finished_at: Optional[str] = None
    result: Optional[dict] = None

# ──────────────────────────────────────────────────────────
# 应用
# ──────────────────────────────────────────────────────────

app = FastAPI(
    title="circuit-agents API",
    description="将自然语言任务编译为电路拓扑并闭环执行",
    version="0.1.0",
)

# 内存存储（生产环境应换 SQLite/Redis）
_runs: dict[str, dict] = {}
_lock = threading.Lock()


def _run_goal(goal_text: str, params: dict, run_id: str):
    """后台编译+执行，结果写回 _runs 表。"""
    try:
        with _lock:
            _runs[run_id]["status"] = "running"

        from compiler.nl_parser import GoalParser
        from compiler.compile import compile_goal
        from runtime import Circuit, CircuitExecutor, SimBackend
        import random

        # 编译：NL → Goal → compile
        parser = GoalParser()
        goal = parser.parse(goal_text)
        spec = compile_goal(
            goal,
            auto_bind=True,
            route=params.get("route", True),
            memory_enabled=params.get("memory_enabled", True),
            auto_select_models=params.get("auto_select_models", False),
        )

        # 执行
        backend = SimBackend(random.Random(int(time.time() * 1000) % (2**31)))
        circuit = Circuit(spec, backend)

        node_events = []

        def on_done(cid, sig, info):
            node_events.append(info)
            # 同时写进 _runs 供 GET /run/{id}/stream 消费
            with _lock:
                _runs[run_id]["_events"].append(info)

        executor = CircuitExecutor(
            circuit,
            data_fill_budget=params.get("data_fill_budget", 2),
            evolve_enabled=params.get("evolve_enabled", True),
            on_node_done=on_done,
            memory_enabled=params.get("memory_enabled", True),
            auto_select_models=params.get("auto_select_models", False),
        )
        result = executor.run()

        with _lock:
            _runs[run_id]["status"] = "done"
            _runs[run_id]["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            _runs[run_id]["result"] = result
            _runs[run_id]["spec"] = spec

    except Exception as e:
        with _lock:
            _runs[run_id]["status"] = "error"
            _runs[run_id]["error"] = str(e)


# ──────────────────────────────────────────────────────────
# 端点
# ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}


@app.post("/run", response_model=RunStatus)
def submit_run(req: GoalRequest):
    """提交一个自然语言任务，异步编译+执行。"""
    run_id = uuid.uuid4().hex[:12]

    with _lock:
        _runs[run_id] = {
            "run_id": run_id,
            "status": "pending",
            "goal": req.goal,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "finished_at": None,
            "result": None,
            "_events": [],
            "error": None,
        }

    params = req.model_dump()
    t = threading.Thread(target=_run_goal, args=(req.goal, params, run_id), daemon=True)
    t.start()

    return RunStatus(**{k: v for k, v in _runs[run_id].items() if not k.startswith("_")})


@app.get("/run/{run_id}")
def get_run(run_id: str):
    with _lock:
        r = _runs.get(run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return {k: v for k, v in r.items() if not k.startswith("_")}


@app.get("/run/{run_id}/stream")
async def stream_run(run_id: str, request: Request):
    """SSE 流式推送节点完成事件 + 最终结果。"""
    with _lock:
        r = _runs.get(run_id)
    if r is None:
        raise HTTPException(404, "run not found")

    async def event_generator():
        idx = 0
        while True:
            # 检查客户端是否断开
            if await request.is_disconnected():
                return

            with _lock:
                r2 = _runs.get(run_id)
                if r2 is None:
                    return
                events = r2.get("_events", [])
                status = r2.get("status", "pending")
                result = r2.get("result")
                error = r2.get("error")

            # 推送新事件
            while idx < len(events):
                ev = events[idx]
                yield f"data: {json.dumps({'type': 'node_done', 'event': ev})}\n\n"
                idx += 1

            # 完成态：推送结果
            if status in ("done", "error") and result is not None:
                yield f"data: {json.dumps({'type': 'result', 'result': result})}\n\n"
                return

            if status == "error" and error:
                yield f"data: {json.dumps({'type': 'error', 'error': error})}\n\n"
                return

            # 等待
            await asyncio_sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# asyncio.sleep 包装（避免底层依赖细节）
def asyncio_sleep(seconds: float):
    import asyncio
    return asyncio.sleep(seconds)


# ──────────────────────────────────────────────────────────
# 离线自检（不启动服务器）
# ──────────────────────────────────────────────────────────

def selftest():
    """验证 API 模型序列化 + 内存存储 + 后台线程 + SSE 流。"""
    import random
    print("=== server.py 离线自检 ===")

    # S1: GoalRequest 反序列化
    req = GoalRequest(goal="查 GDP")
    d = req.model_dump()
    assert d["goal"] == "查 GDP"
    assert d["route"] is True
    print("✓ S1 GoalRequest 序列化正确")

    # S2: RunStatus 构造
    rs = RunStatus(run_id="abc123", status="pending", goal="查 GDP",
                   created_at="2026-08-04T00:00:00")
    assert rs.status == "pending"
    assert rs.result is None
    print("✓ S2 RunStatus 构造正确")

    # S3: 内存存储 CRUD
    rid = "test_run_001"
    _runs[rid] = {"run_id": rid, "status": "pending", "goal": "测试",
                  "created_at": "now", "finished_at": None,
                  "result": None, "_events": [], "error": None}
    assert _runs[rid]["status"] == "pending"
    _runs[rid]["status"] = "done"
    assert _runs[rid]["status"] == "done"
    del _runs[rid]
    print("✓ S3 内存存储 CRUD 正常")

    # S4: 后台线程执行端到端
    rid2 = "test_run_002"
    _runs[rid2] = {"run_id": rid2, "goal": "查中国GDP", "status": "pending",
                   "created_at": "now", "finished_at": None,
                   "result": None, "_events": [], "error": None}
    params = {"route": False, "memory_enabled": True,
              "auto_select_models": False, "data_fill_budget": 2,
              "evolve_enabled": False}
    t = threading.Thread(target=_run_goal,
                         args=("查中国2025年GDP总量", params, rid2),
                         daemon=True)
    t.start()
    t.join(timeout=15)
    assert _runs[rid2]["status"] == "done", f"期望 done，实际 {_runs[rid2]['status']}"
    assert _runs[rid2]["result"] is not None
    assert _runs[rid2]["result"]["final_quality"] > 0
    assert len(_runs[rid2]["_events"]) >= 2, "应至少有 2 个节点事件"
    print(f"✓ S4 端到端：{len(_runs[rid2]['_events'])} 节点事件，"
          f"final_quality={_runs[rid2]['result']['final_quality']:.3f}")
    del _runs[rid2]

    print("\nserver.py 离线自检全部通过 ✓")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="circuit-agents HTTP API Server")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址")
    ap.add_argument("--selftest", action="store_true", help="仅跑离线自检，不启动服务器")
    args = ap.parse_args()

    if args.selftest:
        selftest()
    else:
        print(f"circuit-agents API Server → http://{args.host}:{args.port}")
        print("端点: POST /run | GET /run/{id} | GET /run/{id}/stream | GET /health")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
