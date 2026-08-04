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
import os
import uuid
import time
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
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
    quality_threshold: Optional[float] = Field(None, description="质量门阈值，注入 adc/verify 节点，默认 0.8")
    images: Optional[list] = Field(None, description="④ 多模态：图片附件路径/名称列表")
    audio: Optional[list] = Field(None, description="④ 多模态：语音附件路径/名称列表")

class RunStatus(BaseModel):
    run_id: str
    status: str         # "running" | "done" | "error"
    goal: str
    created_at: str
    finished_at: Optional[str] = None
    result: Optional[dict] = None

class BatchRequest(BaseModel):
    """⑥ 多任务并行：一次提交多个自然语言任务，并发执行并统一汇聚。"""
    goals: list[str] = Field(..., description="自然语言任务列表（每个元素一项任务）")
    max_workers: Optional[int] = Field(None, description="并发线程数上限，默认 min(目标数, 8)")
    route: bool = Field(True, description="是否走 Router 并联编译")
    auto_select_models: bool = Field(False, description="是否启用智能模型选型")
    memory_enabled: bool = Field(True, description="是否启用记忆复用（⑥ 并发写已加线程锁）")
    data_fill_budget: int = Field(2, description="自动补数重试次数上限")
    evolve_enabled: bool = Field(True, description="是否启用 3.5 多任务进化")
    quality_threshold: Optional[float] = Field(None, description="质量门阈值，注入 adc/verify 节点")


class LongTaskRequest(BaseModel):
    """⑦ 长周期任务：提交一个可暂停/可恢复的长任务。"""
    goal: str = Field(..., description="自然语言任务描述")
    route: bool = Field(True, description="是否走 Router 并联编译")
    auto_select_models: bool = Field(False, description="是否启用智能模型选型")
    memory_enabled: bool = Field(True, description="是否启用记忆复用")
    data_fill_budget: int = Field(2, description="自动补数重试次数上限")
    evolve_enabled: bool = Field(False, description="长任务默认关闭 3.5 进化（聚焦续跑/心跳）")
    quality_threshold: Optional[float] = Field(None, description="质量门阈值")
    ttl_ms: int = Field(60000, description="心跳超时阈值(ms)，超则判停滞可触发恢复")


class AgentSpec(BaseModel):
    """⑨ 多机器人协同：单个 agent 的描述。"""
    name: str = Field(..., description="agent 名称（唯一）")
    spec: dict = Field(..., description="该 agent 的电路拓扑 Spec")
    provides: list[str] = Field(default_factory=list, description="本 agent 产出的产物(artifact)名")
    needs: list[str] = Field(default_factory=list, description="本 agent 依赖的上游产物名")
    entry: Optional[str] = Field(None, description="注入黑板产物的入口节点 id（默认拓扑第0层首节点）")


class MultiRobotRequest(BaseModel):
    """⑨ 多机器人协同：多个 agent 共享黑板协作。"""
    agents: list[AgentSpec] = Field(..., description="参与协作的 agent 列表")
    seed_base: int = Field(0, description="各 agent 后端 rng 种子基（资源隔离+可复现）")

# ──────────────────────────────────────────────────────────
# 应用
# ──────────────────────────────────────────────────────────

app = FastAPI(
    title="circuit-agents API",
    description="将自然语言任务编译为电路拓扑并闭环执行",
    version="0.1.0",
)

# CORS（开发用，生产需收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 内存存储（生产环境应换 SQLite/Redis）
_runs: dict[str, dict] = {}
_longtasks: dict[str, dict] = {}   # ⑦ 长周期任务实例表：id -> {task, status, goal, result, checkpoint, error}
_lock = threading.Lock()

# 静态文件根目录
_HERE = Path(__file__).parent


def _run_goal(goal_text: str, params: dict, run_id: str):
    """后台编译+执行，结果写回 _runs 表。"""
    try:
        with _lock:
            _runs[run_id]["status"] = "running"

        from compiler.nl_parser import GoalParser
        from compiler.compile import compile_goal
        from runtime import Circuit, CircuitExecutor, SimBackend
        import random

        # 编译：NL → Goal → compile（④ 多模态：有附件走 parse_multimodal）
        parser = GoalParser()
        images = params.get("images")
        audio = params.get("audio")
        if images or audio:
            goal = parser.parse_multimodal(goal_text, images=images, audio=audio)
        else:
            goal = parser.parse(goal_text)
        spec = compile_goal(
            goal,
            auto_bind=True,
            route=params.get("route", True),
            memory_enabled=params.get("memory_enabled", True),
            auto_select_models=params.get("auto_select_models", False),
        )

        # 质量门阈值注入（前端可调，默认 0.8）：写到 adc/verify 节点的 threshold
        qt = params.get("quality_threshold")
        if qt is not None:
            for comp in spec.get("components", {}).values():
                if comp.get("type") in ("adc", "verify"):
                    comp["threshold"] = float(qt)

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
        # ④ 多模态：把真实输入模态透传到结果（前端可显示）
        result["modality"] = getattr(goal, "attachment_type", "text")
        result["attachments"] = getattr(goal, "attachments", [])

        with _lock:
            _runs[run_id]["status"] = "done"
            _runs[run_id]["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            _runs[run_id]["result"] = result
            _runs[run_id]["spec"] = spec

        # ④ 持久化：自动保存到 SQLite
        try:
            from execution_store import ExecutionStore
            store = ExecutionStore("executions.db")
            store.save(run_id, goal_text, "done", spec,
                       node_events + list(executor._events), result)
        except Exception:
            pass  # 持久化失败不影响主流程

    except Exception as e:
        with _lock:
            _runs[run_id]["status"] = "error"
            _runs[run_id]["error"] = str(e)


# ──────────────────────────────────────────────────────────
# 端点
# ──────────────────────────────────────────────────────────

@app.get("/")
def index():
    """Live Console 前端界面。"""
    console_path = _HERE / "console.html"
    if console_path.exists():
        return HTMLResponse(console_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>circuit-agents API</h1><p>console.html not found</p>")

@app.get("/health")
def health():
    return {"status": "ok", "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}


@app.post("/batch")
def submit_batch(req: BatchRequest):
    """⑥ 多任务并行：一次提交多个任务，并发编译+执行，返回汇聚结果。

    每个 goal 独立编译进独立电路、独立后端与 state（资源隔离），由 ThreadPoolExecutor
    并发执行；返回 {total, succeeded, failed, 每 goal 结果, 墙钟/串行耗时, 加速比, 聚合质量}。
    """
    from runtime import BatchExecutor
    be = BatchExecutor(
        max_workers=req.max_workers,
        route=req.route,
        auto_select_models=req.auto_select_models,
        memory_enabled=req.memory_enabled,
        data_fill_budget=req.data_fill_budget,
        evolve_enabled=req.evolve_enabled,
        quality_threshold=req.quality_threshold,
    )
    agg = be.run(req.goals)
    return agg


# ──────────────────────────────────────────────────────────
# ⑦ 长周期任务：start / pause / resume / status
# ──────────────────────────────────────────────────────────

def _run_longtask(task_id: str, goal_text: str, params: dict):
    """后台线程：编译+执行长任务（分层 + 每层层间落盘 checkpoint，支持暂停/续跑）。"""
    try:
        from compiler.nl_parser import GoalParser
        from compiler.compile import compile_goal
        from runtime import LongTask, SimBackend
        import random

        parser = GoalParser()
        goal = parser.parse(goal_text)
        spec = compile_goal(
            goal, auto_bind=True, route=params.get("route", True),
            memory_enabled=params.get("memory_enabled", True),
            auto_select_models=params.get("auto_select_models", False),
        )
        qt = params.get("quality_threshold")
        if qt is not None:
            for comp in spec.get("components", {}).values():
                if comp.get("type") in ("adc", "verify"):
                    comp["threshold"] = float(qt)

        cp = f"longtask_{task_id}.json"
        lt = LongTask(
            spec,
            backend=SimBackend(random.Random(int(time.time() * 1000) % (2 ** 31))),
            checkpoint_path=cp,
            ttl_ms=params.get("ttl_ms", 60000),
            goal_id=task_id,
        )
        with _lock:
            _longtasks[task_id]["task"] = lt
            _longtasks[task_id]["status"] = "running"
            _longtasks[task_id]["checkpoint"] = cp
        res = lt.run()
        with _lock:
            _longtasks[task_id]["status"] = res["status"]
            _longtasks[task_id]["result"] = res
    except Exception as e:
        with _lock:
            _longtasks[task_id]["status"] = "error"
            _longtasks[task_id]["error"] = str(e)


@app.post("/longtask")
def submit_longtask(req: LongTaskRequest):
    """⑦ 提交一个长周期任务，后台分层执行；返回 task_id 供 pause/resume/status。"""
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _longtasks[task_id] = {"task": None, "status": "pending", "goal": req.goal,
                               "result": None, "checkpoint": None, "error": None}
    params = req.model_dump()
    t = threading.Thread(target=_run_longtask, args=(task_id, req.goal, params), daemon=True)
    t.start()
    return {"task_id": task_id, "status": "pending"}


@app.get("/longtask/{task_id}")
def get_longtask(task_id: str):
    """查询长任务实时状态：心跳年龄、已完成层数、是否停滞、结果。"""
    with _lock:
        rec = _longtasks.get(task_id)
    if rec is None:
        raise HTTPException(404, "longtask not found")
    lt = rec.get("task")
    st = lt.status() if lt is not None else {"status": rec.get("status")}
    return {
        "task_id": task_id,
        "status": st.get("status"),
        "heartbeat_age_ms": st.get("heartbeat_age_ms"),
        "done_layers": st.get("done_layers"),
        "total_layers": st.get("total_layers"),
        "stalled": st.get("stalled"),
        "result": rec.get("result"),
        "error": rec.get("error"),
    }


@app.post("/longtask/{task_id}/pause")
def pause_longtask(task_id: str):
    """请求暂停：当前层完成后停并落盘（paused）。"""
    with _lock:
        rec = _longtasks.get(task_id)
    if rec is None:
        raise HTTPException(404, "longtask not found")
    lt = rec.get("task")
    if lt is None:
        raise HTTPException(409, "task not started yet")
    lt.request_pause()
    return {"task_id": task_id, "paused_requested": True}


@app.post("/longtask/{task_id}/resume")
def resume_longtask(task_id: str):
    """从断点恢复：读取 checkpoint 继续剩余层（不重跑已完成层）。"""
    with _lock:
        rec = _longtasks.get(task_id)
    if rec is None:
        raise HTTPException(404, "longtask not found")
    lt = rec.get("task")
    if lt is None:
        raise HTTPException(409, "task not started yet")

    def _resume():
        try:
            res = lt.resume()
            with _lock:
                _longtasks[task_id]["status"] = res["status"]
                _longtasks[task_id]["result"] = res
        except Exception as e:
            with _lock:
                _longtasks[task_id]["status"] = "error"
                _longtasks[task_id]["error"] = str(e)

    threading.Thread(target=_resume, daemon=True).start()
    return {"task_id": task_id, "resume_requested": True}


# ──────────────────────────────────────────────────────────
# ⑨ 多机器人协同：multirobot（共享黑板编排）
# ──────────────────────────────────────────────────────────

@app.post("/multirobot")
def submit_multirobot(req: MultiRobotRequest):
    """⑨ 多机器人协同：多个 agent 共享黑板协作，中间产物跨 agent 流转。

    按 needs→provides 拓扑序启动各 agent（每个 agent 独立 Circuit + 独立后端，资源隔离），
    上游产物经黑板注入下游 agent 入口节点，实现子电路间协作。返回协作序、黑板快照、各 agent 结果。
    """
    from runtime import MultiRobotCoordinator
    agents = [a.model_dump() for a in req.agents]
    coord = MultiRobotCoordinator(agents, seed_base=req.seed_base)
    return coord.run()


# ──────────────────────────────────────────────────────────
# ⑩ 安全与权限：permission（越权校验 + 执行期拦截）
# ──────────────────────────────────────────────────────────

class PermissionRequest(BaseModel):
    """⑩ 安全与权限：提交一份电路拓扑与已获权限，校验越权并模拟拦截执行。"""
    spec: dict = Field(..., description="电路拓扑 Spec（节点可声明 required_permissions）")
    granted: list[str] = Field(default_factory=list, description="本次会话已获权限集合")


@app.post("/permission")
def check_permission(req: PermissionRequest):
    """⑩ 校验整图权限：返回 authorized / denied（越权节点+缺失权限），并模拟『带拦截』执行。

    越权节点经 guard_backend 返回开路信号（gate=permission_denied）不实际执行；
    授权节点正常；据此暴露最小权限下的真实执行结果。
    """
    from runtime import PermissionGate, Circuit, SimBackend
    import random
    gate = PermissionGate(set(req.granted))
    spec = req.spec
    guarded = gate.guard_backend(SimBackend(random.Random(0)), spec)
    out, _, _ = Circuit(spec, guarded).propagate()
    components = {c: {"ok": s.ok, "quality": round(s.quality, 3),
                     "gate": s.meta.get("gate")}
                  for c, s in out.items()}
    return {
        "authorized": gate.authorize(spec),
        "denied": gate.denied(spec),
        "components": components,
    }


# ──────────────────────────────────────────────────────────
# ⑪ 自适应拓扑：topology/mutate（运行时增删/重连/自愈）
# ──────────────────────────────────────────────────────────

class MutateOp(BaseModel):
    """⑪ 单条拓扑变更操作。"""
    op: str = Field(..., description="remove | insert | reroute | auto_heal")
    cid: Optional[str] = None
    comp: Optional[dict] = None
    preds: list[str] = Field(default_factory=list)
    succs: list[str] = Field(default_factory=list)
    old: Optional[list] = None
    new: Optional[list] = None
    failed_cids: list[str] = Field(default_factory=list)


class MutateRequest(BaseModel):
    """⑪ 自适应拓扑：提交初始拓扑 + 一串变更操作，返回变更后的拓扑。"""
    spec: dict = Field(..., description="初始电路拓扑 Spec")
    ops: list[MutateOp] = Field(default_factory=list, description="依次应用的变更操作")


@app.post("/topology/mutate")
def mutate_topology(req: MutateRequest):
    """⑪ 依次应用拓扑变更（不就地改原图）：remove/insert/reroute/auto_heal。

    返回最终 mutated spec 与每步报告（auto_heal 含冗余分支/汇合节点信息）。"""
    from runtime import CircuitMutator
    spec = req.spec
    reports = []
    for op in req.ops:
        if op.op == "remove":
            spec = CircuitMutator.remove_node(spec, op.cid)
        elif op.op == "insert":
            spec = CircuitMutator.insert_node(spec, op.cid, op.comp, op.preds, op.succs)
        elif op.op == "reroute":
            spec = CircuitMutator.reroute(spec, op.old, op.new)
        elif op.op == "auto_heal":
            spec, rep = CircuitMutator.auto_heal_topology(spec, op.failed_cids)
            reports.append(rep)
        else:
            raise HTTPException(400, f"未知 op: {op.op}")
    return {"spec": spec, "reports": reports}


# ──────────────────────────────────────────────────────────
# ⑫ 跨平台部署：deploy（导出 Dockerfile / runner 预览）
# ──────────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    """⑫ 跨平台部署：提交拓扑，生成可部署产物预览。"""
    spec: dict = Field(..., description="电路拓扑 Spec")
    mode: str = Field("server", description="runner 模式：server | cli")
    name: str = Field("circuit-app", description="导出包名称")
    port: int = Field(8000, description="服务端口")


@app.post("/deploy")
def deploy(req: DeployRequest):
    """⑫ 导出可部署产物：Dockerfile / requirements / runner（内嵌 Spec）。

    返回文本预览（不落盘）；完整落盘见 compiler/deploy.DeploymentExporter.export_bundle。
    """
    from compiler.deploy import DeploymentExporter
    return {
        "dockerfile": DeploymentExporter.generate_dockerfile(
            port=req.port, entry=f"{req.name}_runner:app"),
        "requirements": DeploymentExporter.generate_requirements(),
        "runner": DeploymentExporter.generate_runner(
            req.spec, mode=req.mode, port=req.port, name=req.name),
    }


# ──────────────────────────────────────────────────────────
# ⑬ 电路图共享生态：topology/publish · repo · pull
# ──────────────────────────────────────────────────────────

SHARE_REPO_PATH = ".topology_repo.json"   # ⑬ 本地共享仓库默认路径


class SharePublishRequest(BaseModel):
    """⑬ 发布一份电路拓扑到共享仓库。"""
    spec: dict = Field(..., description="电路拓扑 Spec")
    author: str = Field("anonymous", description="作者")
    tags: list[str] = Field(default_factory=list, description="标签")
    name: Optional[str] = Field(None, description="拓扑名（默认取 spec.name）")


class SharePullRequest(BaseModel):
    """⑬ 从共享仓库拉取拓扑。"""
    name: str = Field(..., description="拓扑名")


@app.post("/topology/publish")
def publish_topology(req: SharePublishRequest):
    """⑬ 发布电路拓扑到本地共享仓库，返回拓扑名。"""
    from compiler.share import ShareRepo
    repo = ShareRepo(SHARE_REPO_PATH)
    name = repo.publish(req.spec, author=req.author, tags=req.tags, name=req.name)
    return {"name": name, "published": True}


@app.get("/topology/repo")
def list_topology_repo():
    """⑬ 列出共享仓库中所有拓扑（名称/作者/标签/校验和）。"""
    from compiler.share import ShareRepo
    return {"items": ShareRepo(SHARE_REPO_PATH).list()}


@app.post("/topology/pull")
def pull_topology(req: SharePullRequest):
    """⑬ 从共享仓库拉取拓扑，还原为电路 Spec。"""
    from compiler.share import ShareRepo
    try:
        return {"name": req.name, "spec": ShareRepo(SHARE_REPO_PATH).pull(req.name)}
    except KeyError:
        raise HTTPException(404, f"仓库中无此拓扑：{req.name}")


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


@app.get("/api/history")
def api_history(limit: int = Query(30, ge=1, le=100)):
    """返回最近 N 条执行记录（内存 + SQLite 合并）。"""
    items = []
    # 先从内存取
    with _lock:
        for rid, r in _runs.items():
            items.append({
                "run_id": rid,
                "goal": r.get("goal", ""),
                "status": r.get("status", "unknown"),
                "created_at": r.get("created_at", ""),
                "finished_at": r.get("finished_at"),
                "result": r.get("result"),
            })
    # 再从 SQLite 取（去重）
    try:
        from execution_store import ExecutionStore
        store = ExecutionStore("executions.db")
        for rec in store.list_recent(limit):
            if rec["run_id"] not in {i["run_id"] for i in items}:
                items.append(rec)
    except Exception:
        pass
    # 按 created_at 倒序
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items[:limit]


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

    # S5: ⑥ 批量执行（离线：强制 GoalParser 走规则解析，避免真实 LLM 依赖）
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import BatchExecutor
    be = BatchExecutor(max_workers=2, memory_enabled=False, evolve_enabled=False)
    bagg = be.run(["批量任务一", "批量任务二", "批量任务三"])
    assert bagg["total"] == 3, f"S5: 应有 3 个目标，实际 {bagg['total']}"
    assert bagg["succeeded"] == 3, f"S5: 应全部成功，实际 {bagg['succeeded']}"
    assert "batch_id" in bagg and "results" in bagg
    assert set(bagg["results"].keys()) == {"g0", "g1", "g2"}
    print(f"✓ S5 ⑥ 批量执行(BatchExecutor): {bagg['total']} 目标并行 → "
          f"成功 {bagg['succeeded']} · speedup={bagg['speedup']} · "
          f"聚合质量={bagg['aggregate_final_quality']:.3f}")

    # S6: ⑦ 长周期任务（离线：强制规则解析）
    import threading as _th
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import LongTask
    tid = "selftest_lt"
    _longtasks[tid] = {"task": None, "status": "pending", "goal": "长任务测试",
                       "result": None, "checkpoint": None, "error": None}
    _run_longtask(tid, "分析一份很长的报告并总结要点",
                  {"route": False, "memory_enabled": False,
                   "auto_select_models": False, "ttl_ms": 60000})
    # _run_longtask 是同步函数（非线程），直接读结果
    assert _longtasks[tid]["status"] == "done", \
        f"S6: 应跑完，实际 {_longtasks[tid]['status']}: {_longtasks[tid].get('error')}"
    assert _longtasks[tid]["result"]["done_layers"] >= 1
    # 状态查询
    st = _longtasks[tid]["task"].status()
    assert st["status"] == "done" and not st["stalled"]
    # 断点续跑：暂停→恢复
    tid2 = "selftest_lt2"
    _longtasks[tid2] = {"task": None, "status": "pending", "goal": "长任务续跑",
                        "result": None, "checkpoint": None, "error": None}
    # 用共享 LongTask 实例：先 pause 后 run，再 resume
    from runtime import SimBackend
    import random as _rnd
    lt_inst = LongTask({"name": "lt", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small", "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "B", "model": "small",
              "required_inputs": ["a"], "produced_outputs": ["b"]},
    }, "wires": [["src", "A"], ["A", "B"]]}, backend=SimBackend(_rnd.Random(0)),
        checkpoint_path=f"longtask_{tid2}.json", goal_id=tid2)
    lt_inst.request_pause()
    r_p = lt_inst.run()
    assert r_p["status"] == "paused" and r_p["done_layers"] == 1
    r_r = lt_inst.resume()
    assert r_r["status"] == "done" and r_r["done_layers"] == 3
    print(f"✓ S6 ⑦ 长周期任务: 后台执行 done · 心跳正常 · 暂停→resume 续跑完成"
          f"（done_layers 1→3）")

    # S7: ⑨ 多机器人协同（离线：显式 agent spec，无 LLM 依赖）
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import MultiRobotCoordinator
    agents = [
        {"name": "researcher", "provides": ["plan"], "needs": [],
         "spec": {"name": "research", "components": {
             "src": {"type": "power", "label": "src"},
             "A": {"type": "resistor", "label": "A", "model": "small",
                   "produced_outputs": ["plan"]}},
             "wires": [["src", "A"]]}},
        {"name": "writer", "provides": ["draft"], "needs": ["plan"],
         "spec": {"name": "write", "components": {
             "src2": {"type": "power", "label": "src2"},
             "W": {"type": "resistor", "label": "W", "model": "small",
                   "required_inputs": ["plan"], "produced_outputs": ["draft"]}},
             "wires": [["src2", "W"]]}},
        {"name": "reviewer", "provides": ["verdict"], "needs": ["draft"],
         "spec": {"name": "review", "components": {
             "src3": {"type": "power", "label": "src3"},
             "R": {"type": "resistor", "label": "R", "model": "small",
                   "required_inputs": ["draft"], "produced_outputs": ["verdict"]}},
             "wires": [["src3", "R"]]}},
    ]
    coord = MultiRobotCoordinator(agents)
    cres = coord.run()
    assert cres["order"] == ["researcher", "writer", "reviewer"], \
        f"S7: 协作序错误 {cres['order']}"
    assert set(cres["blackboard"].keys()) == {"plan", "draft", "verdict"}, \
        f"S7: 黑板产物缺失 {set(cres['blackboard'].keys())}"
    assert cres["agents"]["writer"]["success"], "S7: writer 应协作成功"
    print(f"✓ S7 ⑨ 多机器人协同(MultiRobotCoordinator): {cres['agent_count']} agent 经"
          f"黑板流转 → 序={cres['order']} · 黑板={list(cres['blackboard'].keys())}")

    # S8: ⑩ 安全与权限（离线：显式 spec，无 LLM 依赖）
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import PermissionGate
    psec = {"name": "secure", "components": {
        "src": {"type": "power", "label": "src"},
        "mail": {"type": "resistor", "label": "mail", "model": "tool",
                 "required_permissions": ["email:send"], "produced_outputs": ["sent"]},
        "db": {"type": "resistor", "label": "db", "model": "tool",
               "required_permissions": ["db:query"], "produced_outputs": ["rows"]},
        "safe": {"type": "resistor", "label": "safe", "model": "small",
                 "produced_outputs": ["ok"]}},
        "wires": [["src", "mail"], ["src", "db"], ["src", "safe"]]}
    # 未授权 → mail/db 越权；授权 db:query → mail 仍越权、db 正常
    pr1 = check_permission(PermissionRequest(spec=psec, granted=[]))
    assert not pr1["authorized"] and "mail" in pr1["denied"] and "db" in pr1["denied"]
    pr2 = check_permission(PermissionRequest(spec=psec, granted=["db:query"]))
    assert pr2["components"]["mail"]["gate"] == "permission_denied", "mail 应被拦截"
    assert pr2["components"]["db"]["ok"], "db 已授权应正常"
    assert pr2["components"]["safe"]["ok"], "safe 无权限声明应正常"
    print(f"✓ S8 ⑩ 安全与权限(PermissionGate): 越权识别+拦截生效 "
          f"（denied={pr1['denied']}）")

    # S9: ⑪ 自适应拓扑（离线：显式 spec，无 LLM 依赖）
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import CircuitMutator
    base_spec = {"name": "chain", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small",
              "produced_outputs": ["x"]},
        "B": {"type": "resistor", "label": "B", "model": "small",
              "required_inputs": ["x"], "produced_outputs": ["x"]},
        "C": {"type": "resistor", "label": "C", "model": "small",
              "required_inputs": ["x"], "produced_outputs": ["y"]}},
        "wires": [["src", "A"], ["A", "B"], ["B", "C"]]}
    mr = mutate_topology(MutateRequest(
        spec=base_spec,
        ops=[MutateOp(op="remove", cid="B"),
             MutateOp(op="insert", cid="D",
                      comp={"type": "resistor", "label": "D", "model": "small",
                            "required_inputs": ["x"], "produced_outputs": ["x"]},
                      preds=["A"], succs=["C"]),
             MutateOp(op="auto_heal", failed_cids=["D"])]))
    mspec = mr["spec"]
    assert "B" not in mspec["components"], "S9: B 应被删除"
    assert "D__redundant" in mspec["components"], "S9: auto_heal 应插入 D 冗余分支"
    assert "D__merge" in mspec["components"], "S9: auto_heal 应插入汇合电容"
    print(f"✓ S9 ⑪ 自适应拓扑(CircuitMutator): remove+insert+auto_heal 链式生效 "
          f"（节点数 {len(mspec['components'])}）")

    # S10: ⑫ 跨平台部署（离线：纯文本生成，无 Docker 依赖）
    os.environ.pop("AGENT_API_KEY", None)
    dspec = {"name": "demo", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small",
              "produced_outputs": ["x"]}},
        "wires": [["src", "A"]]}
    dresp = deploy(DeployRequest(spec=dspec, mode="server", name="demo", port=8000))
    assert "FROM python" in dresp["dockerfile"] and "uvicorn" in dresp["dockerfile"]
    assert "CircuitExecutor" in dresp["runner"]
    assert any(r.startswith("fastapi") for r in dresp["requirements"])
    print("✓ S10 ⑫ 跨平台部署(DeploymentExporter): /deploy 返回 Dockerfile+runner+requirements 预览")

    # S11: ⑬ 电路图共享生态（离线：写入临时仓库，避免污染工作区）
    os.environ.pop("AGENT_API_KEY", None)
    import tempfile as _tf
    _repo_file = _tf.mktemp(suffix=".json")
    SHARE_REPO_PATH = _repo_file
    sspec = {"name": "shared_demo", "components": {
        "src": {"type": "power", "label": "src"},
        "A": {"type": "resistor", "label": "A", "model": "small",
              "produced_outputs": ["x"]}},
        "wires": [["src", "A"]]}
    pname = publish_topology(SharePublishRequest(spec=sspec, author="carol",
                                                 tags=["shared"]))["name"]
    items = list_topology_repo().get("items", [])
    assert any(it["name"] == pname for it in items), "仓库应含已发布拓扑"
    pulled = pull_topology(SharePullRequest(name=pname))["spec"]
    assert pulled == sspec, "pull 应还原原 spec"
    if os.path.exists(_repo_file):
        os.unlink(_repo_file)
    print(f"✓ S11 ⑬ 电路图共享生态(ShareRepo): 发布→列表→拉取 往返成功（{pname}）")

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
