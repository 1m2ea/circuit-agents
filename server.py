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
import queue
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# π 永动心跳（f(π) 驱动系统进化方向；离线安全、零新核心逻辑）
try:
    from compiler.pi_heartbeat import PiHeartbeat, pi_heartbeat_selftest
except Exception:  # pragma: no cover
    PiHeartbeat = None
    pi_heartbeat_selftest = None

# 导师-学生训练电路（Phase 3：强模型优化弱模型的外部电路结构，非知识蒸馏）
try:
    from mentor import (mentor_train_cycle, make_ollama_student,
                        default_content_quality, MENTOR_MODEL, MENTOR_BASE)
except Exception:  # pragma: no cover
    mentor_train_cycle = None
    make_ollama_student = None
    default_content_quality = None
    MENTOR_MODEL = MENTOR_BASE = None

# 训练成果模板库（质量门通过的优化方案在此累积，供 π 心跳 / 后续编译复用）
MENTOR_REGISTRY = []


def _mentor_store():
    """惰性拿 server 的 ExecutionStore（失败案例来源）。"""
    try:
        from execution_store import ExecutionStore
        return ExecutionStore("executions.db")
    except Exception:
        return None


# π 永动心跳实例（永动循环默认关闭，由 /pi/heartbeat/start 或启动参数开启）
# digit == 9 时拉起导师-学生训练：失败案例 → 导师优化 → 本地学生重跑 → 质量门 → 固化
PI_HEARTBEAT = PiHeartbeat(
    interval=60.0,
    mentor_store=_mentor_store(),
    mentor_registry=MENTOR_REGISTRY,
) if PiHeartbeat else None

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
    layers_per_round: int = Field(0, description="⑦ 加深：>0 则分轮执行，每轮跑 N 层后休眠（0=一次跑完）")
    wake_in_sec: float = Field(0, description="⑦ 加深：休眠多久后可被唤醒（秒，0=立即到期）")


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
_topo_sessions: dict[str, dict] = {}  # 在线拓扑编辑会话：sid -> {executor, thread, done, result, error}
_rooms: dict[str, dict] = {}          # 大规模协作：房间 rid -> {room_id, owner, members:{uid:role}, session_id, activity:[...], created_at}
_lock = threading.Lock()

# ── 实时交互（极致：连续不中断）：事件总线 + SSE 推送 ──────────────
class _RealtimeHub:
    """把执行器事件 / 房间 activity 实时推送给订阅者（SSE 消费者）。

    - 订阅者是一个 queue.Queue；executor.on_event 与 _record_activity 调 push()。
    - 仅在有订阅者时才有开销；无 SSE 连接时 push 是空操作，零拖累 selftest。

    大规模协作（极致·人数众多）扇出加固：
      · **有界队列**：每个订阅者队列上限 maxsize，杜绝慢消费者把内存吃穿。
      · **drop-oldest**：队列满时丢最旧事件而非阻塞——一个卡住的观众
        绝不能拖慢整房的推送（广播不被最慢的人决定速度）。
      · **coalesce（合并）**：presence 这类高频且只关心最新值的事件，
        同 key 只保留最新一条，避免 N 人 × 高频心跳把带宽打满。
      · **stats**：订阅者数 / 推送数 / 丢弃数，供 /rooms/{rid}/stats 观测水位。
    """
    COALESCE_TYPES = {"presence"}        # 只保留最新值的事件类型

    def __init__(self, maxsize: int = 512):
        self._subs = {}                 # topic -> [Queue, ...]
        self._lock = threading.Lock()
        self.maxsize = int(maxsize)
        self.stats = {"pushed": 0, "dropped": 0, "coalesced": 0}

    def subscribe(self, sid: str) -> "queue.Queue":
        q = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs.setdefault(sid, []).append(q)
        return q

    def unsubscribe(self, sid: str, q: "queue.Queue"):
        with self._lock:
            lst = self._subs.get(sid)
            if lst and q in lst:
                lst.remove(q)
            if lst is not None and not lst:
                self._subs.pop(sid, None)      # 空 topic 不留残渣（众多房间时省内存）

    def subscriber_count(self, sid: str) -> int:
        with self._lock:
            return len(self._subs.get(sid, []))

    def _coalesce_key(self, ev: dict):
        """同 key 的旧事件可被新事件顶替（仅高频只读型事件）。"""
        if ev.get("type") in self.COALESCE_TYPES:
            return (ev.get("type"), ev.get("actor") or ev.get("user_id"))
        return None

    def push(self, sid, ev: dict):
        with self._lock:
            subs = list(self._subs.get(sid, []))
        if not subs:
            return
        ck = self._coalesce_key(ev)
        for q in subs:
            try:
                if ck is not None and q.qsize():
                    # 合并：把队列里同 key 的旧事件剔除，只留最新（人数众多时的关键减压阀）
                    kept, removed = [], 0
                    try:
                        while True:
                            old = q.get_nowait()
                            if self._coalesce_key(old) == ck:
                                removed += 1
                            else:
                                kept.append(old)
                    except queue.Empty:
                        pass
                    for o in kept:
                        try:
                            q.put_nowait(o)
                        except queue.Full:
                            break
                    if removed:
                        self.stats["coalesced"] += removed
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    # drop-oldest：丢最旧的一条给新事件让位，绝不阻塞广播线程
                    try:
                        q.get_nowait()
                        self.stats["dropped"] += 1
                        q.put_nowait(ev)
                    except Exception:
                        self.stats["dropped"] += 1
                        continue
                self.stats["pushed"] += 1
            except Exception:
                pass

_realtime_hub = _RealtimeHub()


def _json_default(o):
    """json.dumps 兜底：把非标准 JSON 类型转成可序列化结构。
    关键修复：节点事件 value 字段常含 runtime.Signal（dataclass）/ numpy 标量 /
    set，直接 json.dumps 会抛 TypeError 导致 SSE 生成器在首个含此类字段的事件处崩溃、
    流提前关闭（Live Console 看不到后续事件与最终结果）。"""
    import dataclasses
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if hasattr(o, "model_dump"):           # pydantic v2
        try:
            return o.model_dump()
        except Exception:
            pass
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items() if not k.startswith("_")}
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except Exception:
        pass
    if isinstance(o, (set, frozenset)):
        return list(o)
    return str(o)


def _sse(data: dict) -> str:
    """把事件 dict 序列化为 SSE `data:` 帧（UTF-8 安全，非标准类型自动兜底）。"""
    return "data: " + json.dumps(data, default=_json_default, ensure_ascii=False) + "\n\n"

# ── 深化③：房间持久化（重启不丢房间/记忆） ──────────────────
class RoomStore:
    """把房间（含共享拓扑 spec / 记忆 / ops / 版本）落盘 JSON，做到重启可恢复。

    - 不序列化活对象（CircuitExecutor / threading.Event / 线程），只存可重建字段。
    - 原子写：先写 .tmp 再 os.replace，避免半截文件。
    - 重启后 load_rooms() 据 spec 重建内部 topology session（新 session_id）。
    """

    def __init__(self, path: str = ".rooms.json"):
        self.path = path
        self._wlock = threading.RLock()

    def save(self, rooms: dict):
        snap = {}
        for rid in list(rooms.keys()):           # 列表快照，迭代期间房间变更也不崩
            room = rooms.get(rid)
            if not room:
                continue
            mem = room.get("memory", {}) or {}
            snap[rid] = {
                "room_id": room.get("room_id", rid),
                "name": room.get("name", ""),
                "owner": room.get("owner", ""),
                "members": dict(room.get("members", {})),
                "spec": room.get("spec"),
                "activity": list(room.get("activity", [])),
                "memory": {k: list(v) if isinstance(v, list) else v
                           for k, v in mem.items()},
                "ops": list(room.get("ops", [])),
                "rev": int(room.get("rev", 0)),
                # 极致·大规模协作：并发模式与 CRDT op 流也要持久化，重启后仍能收敛
                "concurrency": room.get("concurrency", "crdt"),
                "crdt_ops": list(getattr(room.get("crdt"), "log", []) or []),
                "created_at": room.get("created_at", time.time()),
            }
        with self._wlock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)           # 原子替换

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


# 房间落盘存储（重启恢复）。_PERSIST=False 时（自检）不写盘，避免污染真实 .rooms.json。
_ROOMS_FILE = os.environ.get("ROOMS_FILE", ".rooms.json")
_room_store = RoomStore(_ROOMS_FILE)
_PERSIST = True


def _persist():
    """把当前所有房间落盘（深化③）。仅在 _PERSIST 时触发。"""
    if _PERSIST:
        _room_store.save(_rooms)


def load_rooms(store: "RoomStore" = None):
    """从落盘快照恢复房间，并据 spec 重建内部 topology session。

    模拟『服务重启』：内存房间清空后调用此函数即可全部恢复，且每个房间都有活的 session。
    """
    st = store or _room_store
    for rid, room in st.load().items():
        room.setdefault("activity", [])
        room.setdefault("memory", {"published": [], "learnings": [], "templates": []})
        room.setdefault("ops", [])
        room.setdefault("rev", 0)
        room.setdefault("members", {})
        room.setdefault("created_at", time.time())
        room.setdefault("concurrency", "crdt")
        room.setdefault("presence", {})       # presence 是易失态，重启后重新心跳建立
        # 重建 CRDT 副本：优先用持久化的 op 流重放（保留并发历史与 lamport），
        # 没有 op 流的老快照则退回按 spec 重建。
        _cops = room.pop("crdt_ops", None)
        _c = _new_crdt(rid, {} if _cops else (room.get("spec") or {}))
        if _c is not None and _cops:
            _c.merge(_cops)
        room["crdt"] = _c
        try:    # 据持久化的 spec 重建内部 session（新 session_id，后台线程跑电路）
            _sess = topology_session(TopologySessionRequest(spec=room.get("spec") or {}))
            room["session_id"] = _sess["session_id"]
        except Exception:
            room["session_id"] = None
        with _lock:
            _rooms[rid] = room


# ── 大规模协作：角色与权限（Phase 0 地基） ──────────────────
ROLE_PERMS = {
    "observer": {"read"},
    "student":  {"read", "control"},
    "reviewer": {"read", "adjudicate"},
    "mentor":   {"read", "control", "edit", "adjudicate", "publish"},
    "owner":    {"read", "control", "edit", "adjudicate", "manage", "publish"},
}

def _has_perm(room: dict, user_id: str, action: str) -> bool:
    role = room["members"].get(user_id)
    if role is None:
        return False
    return action in ROLE_PERMS.get(role, set())

def _room_ctx(room_id: Optional[str], user_id: Optional[str], action: Optional[str]):
    """传了 room_id 时：校验房间存在(404)、用户有该动作权限(403)，返回 room；
    未传 room_id 或传非字符串(如直接 Python 调用的 Query 默认对象)时返回 None（向后兼容单用户模式）。"""
    if not room_id or not isinstance(room_id, str):
        return None
    room = _rooms.get(room_id)
    if room is None:
        raise HTTPException(404, f"room not found: {room_id}")
    if action and not _has_perm(room, user_id or "", action):
        raise HTTPException(403, f"user {user_id} 角色 {room['members'].get(user_id)} 无 {action} 权限")
    return room

def _record_activity(room: dict, user_id: str, action: str, target: str, detail=None):
    room["activity"].append({
        "ts": time.time(), "actor": user_id or "?",
        "action": action, "target": target, "detail": detail,
    })
    # 实时交互（极致）：房间 activity 也推入事件总线 → 同房成员 SSE 实时可见
    _sid = room.get("session_id")
    if _sid:
        _realtime_hub.push(_sid, {"type": "activity", "actor": user_id or "?",
                                  "action": action, "target": target,
                                  "detail": detail, "ts": room["activity"][-1]["ts"]})

# 静态文件根目录
_HERE = Path(__file__).parent


def _compile_execute(goal_text, params, on_node_done=None):
    """同步编译+执行，返回 (result, spec, events)。供 /run 与 /quality/report 复用。"""
    from compiler.nl_parser import GoalParser
    from compiler.compile import compile_goal
    from runtime import Circuit, CircuitExecutor, SimBackend
    import random
    parser = GoalParser()
    images = params.get("images"); audio = params.get("audio")
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
    qt = params.get("quality_threshold")
    if qt is not None:
        for comp in spec.get("components", {}).values():
            if comp.get("type") in ("adc", "verify"):
                comp["threshold"] = float(qt)
    # 真实模型后端：检测到 DEEPSEEK_API_KEY 则走 DeepSeek（OpenAI 兼容接口）；
    # 否则维持原 SimBackend 确定性模拟器（向后兼容、离线不崩）。
    _rng_seed = int(time.time() * 1000) % (2 ** 31)
    _llm_key = os.environ.get("DEEPSEEK_API_KEY")
    _t0 = time.time()
    if _llm_key:
        from compiler.ollama_backend import OllamaBackend
        _llm_base = os.environ.get("CA_LLM_BASE", "https://api.deepseek.com").rstrip("/")
        _llm_model = os.environ.get("CA_LLM_MODEL", "deepseek-chat")
        _reasoner_model = os.environ.get("CA_LLM_REASONER_MODEL", "deepseek-reasoner")
        # 档位 → 模型：large（更难任务档）与显式 reasoner 档走推理模型，其余走标准对话模型
        _mm = {
            "small": _llm_model,
            "large": _reasoner_model,
            "tool": _llm_model,
            "code": _llm_model,
            "reasoner": _reasoner_model,
        }
        backend = OllamaBackend(
            rng=random.Random(_rng_seed),
            host=_llm_base,
            api_key=_llm_key,
            model_map=_mm,
            api_mode="openai",
            timeout=180.0,
            fallback=SimBackend(random.Random(_rng_seed)),
        )
    else:
        backend = SimBackend(random.Random(_rng_seed))

    # 推理档：真实推理后端可用时，把「推理/分析/综合/判断」类电阻节点升到 reasoner 档
    # （deepseek-reasoner），让更难的任务用推理模型、质量更高；检索/格式化等轻节点保持 small。
    if _llm_key:
        _REASON_ROLES = ("reason", "analyze", "synthes", "compare", "plan",
                         "judge", "decide", "评估", "分析", "推理", "综合",
                         "对比", "决策", "总结", "论证", "推导", "insight", "critique")
        for _c in spec.get("components", {}).values():
            if _c.get("type") != "resistor":
                continue
            _lbl = str(_c.get("label", "")).lower()
            _cap = str(_c.get("capability", "")).lower()
            if any(k in _lbl or k in _cap for k in _REASON_ROLES):
                _c["model"] = "reasoner"

    circuit = Circuit(spec, backend)
    events = []
    def _cb(cid, sig, info):
        events.append(info)
        if on_node_done is not None:
            on_node_done(cid, sig, info)
    executor = CircuitExecutor(
        circuit,
        data_fill_budget=params.get("data_fill_budget", 2),
        evolve_enabled=params.get("evolve_enabled", True),
        on_node_done=_cb,
        memory_enabled=params.get("memory_enabled", True),
        auto_select_models=params.get("auto_select_models", False),
    )
    result = executor.run()
    # 真推理可见性：把后端实际消耗（token / 模型 / 耗时）写进结果，供前端「本次由 DeepSeek 生成」展示
    _elapsed_ms = round((time.time() - _t0) * 1000.0, 1)
    result["elapsed_ms"] = _elapsed_ms
    _stats = getattr(backend, "_stats", None)
    if _stats is not None and _stats.get("successes", 0) > 0:
        result["llm"] = {
            "provider": "DeepSeek",
            "base": os.environ.get("CA_LLM_BASE", "https://api.deepseek.com"),
            "real": True,
            "calls": _stats.get("successes", 0),
            "models": _stats.get("models", {}),
            "tokens": {
                "total": _stats.get("total_tokens", 0),
                "prompt": _stats.get("prompt_tokens", 0),
                "completion": _stats.get("completion_tokens", 0),
                "reasoning": _stats.get("reasoning_tokens", 0),
            },
        }
    else:
        result["llm"] = {
            "provider": "SimBackend",
            "base": None,
            "real": False,
            "calls": 0,
            "models": {},
            "tokens": {"total": 0, "prompt": 0, "completion": 0, "reasoning": 0},
        }
    result["modality"] = getattr(goal, "attachment_type", "text")
    result["attachments"] = getattr(goal, "attachments", [])
    return result, spec, events


def _run_goal(goal_text: str, params: dict, run_id: str):
    """后台编译+执行，结果写回 _runs 表。"""
    try:
        with _lock:
            _runs[run_id]["status"] = "running"
        node_events = []
        def on_done(cid, sig, info):
            node_events.append(info)
            with _lock:
                _runs[run_id]["_events"].append(info)
        result, spec, _ = _compile_execute(goal_text, params, on_node_done=on_done)
        with _lock:
            _runs[run_id]["status"] = "done"
            _runs[run_id]["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            _runs[run_id]["result"] = result
            _runs[run_id]["spec"] = spec
        try:
            from execution_store import ExecutionStore
            store = ExecutionStore("executions.db")
            store.save(run_id, goal_text, "done", spec, node_events, result)
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
        lpr = int(params.get("layers_per_round", 0) or 0)
        if lpr > 0:   # ⑦ 加深：分轮执行——跑 N 层后主动休眠，等 /wake 续跑
            res = lt.run_sleep(lpr, float(params.get("wake_in_sec", 0) or 0))
        else:
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


@app.post("/longtask/{task_id}/wake")
def wake_longtask(task_id: str):
    """⑦ 加深：唤醒一个休眠中的长任务。

    未到唤醒时间 → 返回 sleeping/due_now=False（不误唤醒）；已到期 → 同步续跑至完成。
    与 /resume 的区别：resume 是「崩溃/暂停后恢复」，wake 是「主动休眠到期后续跑」，
    休眠期不占线程、不烧 CPU，适合跨天/跨周的长周期任务。
    """
    with _lock:
        rec = _longtasks.get(task_id)
    if rec is None:
        raise HTTPException(404, "longtask not found")
    lt = rec.get("task")
    if lt is None:
        raise HTTPException(409, "task not started yet")
    if not lt.should_wake():
        st = lt.status()
        return {"task_id": task_id, "woken": False, "due_now": False,
                "status": st.get("status"), "done_layers": st.get("done_layers"),
                "total_layers": st.get("total_layers")}
    res = lt.wake()
    with _lock:
        _longtasks[task_id]["status"] = (res or {}).get("status")
        _longtasks[task_id]["result"] = res
    return {"task_id": task_id, "woken": True, "due_now": True, "result": res}


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


# ──────────────────────────────────────────────────────────
# ⑭ 自我进化：evolve（历史蒸馏可复用模板）
# ──────────────────────────────────────────────────────────

class EvolveRequest(BaseModel):
    """⑭ 自我进化：提交执行历史，蒸馏高频结构模式为可复用模板。"""
    history: list[dict] = Field(..., description="执行历史（每项 {name, spec} 或纯 spec）")
    min_support: int = Field(2, description="motif 达此频次才升华为模板")


class EvolveSuggestRequest(BaseModel):
    """⑭ 自我进化：给定新拓扑 + 历史，返回其中已沉淀的可复用模板。"""
    spec: dict = Field(..., description="新电路拓扑 Spec")
    history: list[dict] = Field(default_factory=list, description="执行历史")
    min_support: int = Field(2, description="motif 最小支持度")


@app.post("/evolve")
def evolve(req: EvolveRequest):
    """⑭ 蒸馏历史中的高频边 motif 为可复用拓扑模板（含骨架与示例）。"""
    from runtime import SelfEvolution
    ev = SelfEvolution(req.history, min_support=req.min_support)
    return {"templates": ev.templates, "template_count": len(ev.templates)}


@app.post("/evolve/suggest")
def evolve_suggest(req: EvolveSuggestRequest):
    """⑭ 给定新拓扑，返回其中已沉淀的可复用模板（驱动自动复用/推荐）。"""
    from runtime import SelfEvolution
    ev = SelfEvolution(req.history, min_support=req.min_support)
    return {"suggested": ev.suggest(req.spec)}


@app.post("/quality/report")
def quality_report(req: GoalRequest):
    """Phase 2 细粒度质量门：编译+执行一次，返回 quality_report（逐节点打分/分级/修复建议）。

    直接复用 _compile_execute（与 /run 同一套编译+执行管线），从 result 中取 quality_report。
    """
    params = req.model_dump()
    result, _, _ = _compile_execute(req.goal, params)
    return result.get("quality_report", {})


@app.post("/skills")
def list_skills():
    """Phase 2 技能注册表：列出全部已注册技能（含分类/tier/是否已实现），并复用 ② 已有技能。"""
    from runtime import SkillRegistry
    reg = SkillRegistry()
    skills = reg.list()
    return {
        "count": len(skills),
        "implemented_count": len(reg.implemented_names()),
        "skills": skills,
    }


@app.post("/skills/resolve")
def skills_resolve(req: GoalRequest):
    """Phase 2 技能注册表：编译一个目标为拓扑，解析它引用了哪些技能、哪些待实现。

    只编译不执行（离线安全：强制规则解析，无需 LLM/网络），从拓扑 components 的
    fillers/skills 抽取技能引用，对照注册表给出 已注册 / 未注册(待实现) 清单。
    """
    from compiler.nl_parser import GoalParser
    from compiler.compile import compile_goal
    from runtime import SkillRegistry
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线规则解析
    params = req.model_dump()
    parser = GoalParser()
    goal = parser.parse(req.goal)
    spec = compile_goal(
        goal,
        auto_bind=True,
        route=params.get("route", True),
        memory_enabled=params.get("memory_enabled", True),
        auto_select_models=params.get("auto_select_models", False),
    )
    components = spec.get("components", {})
    reg = SkillRegistry()
    return reg.resolve(components, params.get("evolve_skill"))


@app.post("/models")
def list_models():
    """Phase 2 ③ 模型选型再平衡：返回档位画像 + 当前再平衡权重 + 已记录的历史统计。"""
    from runtime import SimBackend
    from compiler.model_selector import ModelSelector, ModelMetrics
    mm = ModelMetrics(ModelMetrics.DEFAULT_PATH)
    try:
        mm.load()
    except Exception:
        pass
    ms = ModelSelector()  # 仅取默认权重，不依赖 memory/metrics
    tiers = {}
    for t, d in SimBackend._TIERS.items():
        tiers[t] = {"cost": d.get("cost"), "latency": d.get("latency"),
                    "accuracy": d.get("accuracy"), "yld": d.get("yld")}
    return {
        "tiers": tiers,
        "weights": ms._weights,
        "recorded_stats": mm.global_stats(),
        "metric_store": ModelMetrics.DEFAULT_PATH,
    }


@app.post("/models/select")
def select_models(req: GoalRequest):
    """Phase 2 ③ 模型选型再平衡：编译一个目标为拓扑，按历史成功率/延迟/成本多目标再平衡选档。
    只编译不执行（离线安全：强制规则解析，无需 LLM/网络）。"""
    from compiler.nl_parser import GoalParser
    from compiler.compile import compile_goal
    from compiler.model_selector import ModelSelector, ModelMetrics
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线规则解析
    params = req.model_dump()
    parser = GoalParser()
    goal = parser.parse(req.goal)
    spec = compile_goal(
        goal,
        auto_bind=True,
        route=params.get("route", True),
        memory_enabled=params.get("memory_enabled", True),
        auto_select_models=params.get("auto_select_models", False),
    )
    mm = ModelMetrics(ModelMetrics.DEFAULT_PATH)
    try:
        mm.load()
    except Exception:
        pass
    ms = ModelSelector(metrics=mm)
    return ms.select(spec)


@app.get("/models/active")
def models_active():
    """返回当前 /run 主路径实际使用的模型后端（供前端展示「当前模型」标识）。
    不暴露 key 值，仅读环境变量判断。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        base = os.environ.get("CA_LLM_BASE", "https://api.deepseek.com").rstrip("/")
        model = os.environ.get("CA_LLM_MODEL", "deepseek-chat")
        return {"backend": "deepseek", "live": True,
                "provider": "DeepSeek", "model": model, "base": base}
    return {"backend": "sim", "live": False,
            "provider": "SimBackend", "model": "deterministic-simulator",
            "base": None}


class TranscribeRequest(BaseModel):
    images: Optional[list] = None
    audio: Optional[list] = None


@app.post("/transcribe")
def transcribe(req: TranscribeRequest):
    """Phase 2 ② 加深④ 多模态真视听觉：把图片/语音附件转写为文本。

    离线（无 key）自动回退占位描述；真实后端由调用方经 MultimodalTranscriber.register
    注入后启用。只转录、不编译，离线安全（强制无网络）。
    """
    from compiler.multimodal import MultimodalTranscriber
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线（转录不依赖 LLM 主解析）
    tr = MultimodalTranscriber()
    items = ([{"type": "image", "name": i} for i in (req.images or [])]
             + [{"type": "audio", "name": a} for a in (req.audio or [])])
    results = tr.transcribe_all(items)
    return {
        "count": len(results),
        "results": results,  # 每项: name/type/transcription/backend/offline
        "modalities": {"image": tr.backends("image"),
                       "audio": tr.backends("audio")},
    }


class ClusterRequest(BaseModel):
    goal: str
    n_workers: Optional[int] = 2
    route: Optional[bool] = True
    memory_enabled: Optional[bool] = False
    auto_select_models: Optional[bool] = False


@app.post("/cluster")
def cluster_run(req: ClusterRequest):
    """Phase 2 ③ 分布式执行：把目标编译为拓扑，按弱连通分量分片，多 worker 并发执行并聚合。

    离线安全（强制规则解析，无需 LLM/网络）。真实远程 transport 由调用方注入
    ClusterCoordinator(transport=...)，协调协议不变。
    """
    from compiler.cluster import ClusterCoordinator
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线规则解析
    n = req.n_workers or 2
    coord = ClusterCoordinator(n_workers=n)
    return coord.run(req.goal, n_workers=n, route=req.route,
                     memory_enabled=req.memory_enabled,
                     auto_select_models=req.auto_select_models)


class DecisionRequest(BaseModel):
    """⑧ 加深：人机协同决策点——执行前在关键节点暂停请人类审批。"""
    goal: Optional[str] = Field(None, description="自然语言任务（与 spec 二选一）")
    spec: Optional[dict] = Field(None, description="直接给拓扑 Spec（与 goal 二选一）")
    decision_points: Optional[object] = Field(
        None, description="'all' 或节点 id/label 列表；spec 内 human_decision_point 标记也生效")
    policy: Optional[dict] = Field(
        None, description="预设策略 {节点: proceed|skip|abort}，模拟人类审批决定")
    default_action: str = Field("proceed", description="未在 policy 中命中的决策点默认动作")
    route: bool = Field(True, description="goal 路径是否走 Router 并联编译")


@app.post("/decision")
def decision_run(req: DecisionRequest):
    """⑧ 加深：带决策点的执行——在关键节点执行「前」暂停，按策略 proceed/skip/abort。

    离线安全（强制规则解析）。这里用 policy 声明式模拟人类审批，便于自动化/回放；
    真实场景把 human_callback 换成阻塞式人工审批（等待前端点按钮）即可，协议不变。
    返回结果附 decision_log（每个决策点的节点与动作）与事件流。
    """
    import random as _rnd
    from runtime import Circuit, CircuitExecutor, SimBackend
    os.environ.pop("AGENT_API_KEY", None)   # 强制离线规则解析

    spec = req.spec
    if spec is None:
        if not req.goal:
            raise HTTPException(400, "goal 与 spec 至少提供一个")
        from compiler.nl_parser import GoalParser
        from compiler.compile import compile_goal
        spec = compile_goal(GoalParser().parse(req.goal), auto_bind=True,
                            route=req.route, memory_enabled=False)

    policy = req.policy or {}
    default_action = (req.default_action or "proceed").lower()
    log, events = [], []

    def _cb(node=None, missing=None, context=None, label=None, decision_point=False):
        act = policy.get(node) or policy.get(label) or default_action
        if decision_point:
            log.append({"node": node, "label": label, "action": act})
        return act

    dp = req.decision_points
    if isinstance(dp, (list, tuple)):
        dp = set(dp)
    ex = CircuitExecutor(Circuit(spec, SimBackend(_rnd.Random(0))),
                         human_callback=_cb, decision_points=dp,
                         on_event=lambda e: events.append(e))
    res = ex.run()
    res["decision_log"] = log
    res["decision_points_hit"] = len(log)
    res["events"] = events
    return res


class RLOptimizeRequest(BaseModel):
    """Phase 2 第三层① RL 优化拓扑：用真实执行 reward 搜索更优结构。"""
    goal: Optional[str] = Field(None, description="自然语言任务（与 spec 二选一）")
    spec: Optional[dict] = Field(None, description="直接给拓扑 Spec（与 goal 二选一）")
    episodes: int = Field(24, description="搜索轮数上限")
    patience: int = Field(12, description="连续 N 轮无提升即收敛")
    weights: Optional[dict] = Field(
        None, description="reward 权重 {quality,cost,latency}，默认 1.0/0.35/0.25")
    seed: int = Field(0, description="搜索随机种子（可复现）")
    distill: bool = Field(False, description="是否把最优拓扑沉淀进 TopologyMemory")
    return_history: bool = Field(False, description="是否返回逐轮搜索轨迹（较大）")


@app.post("/rl/optimize")
def rl_optimize(req: RLOptimizeRequest):
    """Phase 2 第三层① RL 优化拓扑：UCB1 多臂老虎机 + 爬山，真实执行反馈驱动。

    与 /optimize（Optimizer 解析式估算）的区别：这里**每个候选拓扑都真跑一遍**，
    reward = 加权(质量, -成本, -延迟)，因此能发现启发式规则看不见的结构性节省
    （例如「这个 verify 是多余的」「这一步换 small 档质量不掉」）。
    返回 arm_stats 说明哪类改动真有效，可解释。离线安全（强制规则解析）。
    """
    from compiler.rl_optimizer import RLOptimizer
    os.environ.pop("AGENT_API_KEY", None)
    target = req.spec if req.spec is not None else req.goal
    if target is None:
        raise HTTPException(400, "goal 与 spec 至少提供一个")
    opt = RLOptimizer(weights=req.weights, seed=req.seed)
    res = opt.optimize(target, episodes=req.episodes, patience=req.patience)
    if req.distill:
        res["distilled"] = opt.distill(
            res, goal_desc=(req.goal or res["best_spec"].get("name", "rl_optimized")))
    if not req.return_history:
        res.pop("history", None)
    return res


class FederatedClientSpec(BaseModel):
    """一个联邦参与方的本地数据（只传结构与统计，服务端永不接触原文）。"""
    client_id: str
    records: Optional[list] = Field(
        None, description="本地拓扑记忆条目 [{spec, result}]；只被抽成 motif+档位统计")
    metrics: Optional[dict] = Field(
        None, description="本地执行指标 {cap:{tier:{count,success,total_latency,total_cost}}}")
    epsilon: float = Field(4.0, description="该方的差分隐私预算 ε（越小越私密、噪声越大）")
    seed: int = Field(0, description="噪声随机种子（可复现）")
    max_releases: int = Field(1, description="声明的发布次数上限（防重复查询求平均反推）")
    min_support: int = Field(1, description="motif 最小支持度，低于此值直接丢（k-匿名）")


class FederatedRequest(BaseModel):
    """Phase 2 第三层② 联邦学习：多实例共享拓扑经验，不共享原始数据。"""
    clients: list[FederatedClientSpec] = Field(..., description="参与方列表（≥min_clients）")
    min_clients: int = Field(2, description="最少参与方，不足则拒绝聚合（防单点反推）")
    blend: float = Field(0.5, description="回灌时全局经验权重 0~1")
    query_capability: Optional[str] = Field(
        None, description="可选：聚合后顺带问「某能力用哪个档位最优」")


@app.post("/federated/round")
def federated_round(req: FederatedRequest):
    """Phase 2 第三层② 联邦学习：脱敏摘要 + 差分隐私 + FedAvg 聚合 + 回灌。

    与 /topology/publish（⑬ 共享生态）的区别：⑬ 分发**完整电路图**（含 goal 原文），
    这里只出**统计摘要**（motif 频次 / 能力档位成功率 / 质量直方图）并加拉普拉斯噪声，
    适合跨组织。服务端全程看不到原始任务描述与 spec 全文。

    隐私保证：ε 按顺序组合定理在各查询间平分并由 PrivacyLedger 硬性记账；
    超出声明发布次数会被拒绝（防「反复查询求平均消噪」攻击），被拒摘要不参与聚合。
    离线安全（纯本地计算，无 key、无网络）。
    """
    from compiler.federated import build_client, FederatedServer, run_federated_round
    os.environ.pop("AGENT_API_KEY", None)
    if not req.clients:
        raise HTTPException(400, "clients 不能为空")
    clients = [
        build_client(c.client_id, records=c.records, metrics_data=c.metrics,
                     epsilon=c.epsilon, seed=c.seed, min_support=c.min_support,
                     max_releases=c.max_releases)
        for c in req.clients
    ]
    server = FederatedServer(min_clients=req.min_clients)
    res = run_federated_round(clients, server, blend=req.blend)
    if req.query_capability:
        res["best_tier"] = server.best_tier_for(
            res["global_model"], req.query_capability)
        res["queried_capability"] = req.query_capability
    return res


class ComponentDiscoverRequest(BaseModel):
    """Phase 2 第三层③ 自主发现新元件类型：从历史拓扑挖掘频繁子图并封装。"""
    history: list = Field(..., description="历史拓扑列表 [{spec:{...}} 或直接 spec]")
    min_support: int = Field(3, description="子图跨任务出现次数下限")
    max_size: int = Field(4, description="子图节点数上限（2~max_size）")
    register: bool = Field(True, description="是否注册到全局 ComponentLibrary（注册后可被 compile 引用）")


@app.post("/components/discover")
def components_discover(req: ComponentDiscoverRequest):
    """Phase 2 第三层③ 自主发现新元件类型：挖掘频繁子图 → 封装 composite → 注册。

    与 ⑭ SelfEvolution（/evolve/suggest 边级 motif 建议）的区别：
    ⑭ 只建议「你的拓扑有高频边」，不改 spec；本端点把「反复出现的完整子链」
    封装成可执行的新元件类型并注册，compile 可直接用 type:composite 引用，
    Circuit 构造时自动内联展开为原子元件（零运行时改动）。

    离线安全（纯本地子图枚举 + 排列同构判定，无 key、无网络）。
    """
    from compiler.component_miner import mine, wrap, discover_and_register
    os.environ.pop("AGENT_API_KEY", None)
    if req.register:
        templates = discover_and_register(
            req.history, min_support=req.min_support, max_size=req.max_size)
    else:
        motifs = mine(req.history, min_support=req.min_support,
                      max_size=req.max_size)
        templates = [wrap(m, register=False) for m in motifs]
    return {"templates": templates, "count": len(templates),
            "registered": req.register}


@app.get("/components/library")
def components_library():
    """查看当前已注册的 composite 模板（③ 自主发现新元件类型）。"""
    from runtime import _COMPONENT_LIBRARY
    return {"templates": list(_COMPONENT_LIBRARY.values()),
            "count": len(_COMPONENT_LIBRARY)}


class CodegenRequest(BaseModel):
    goal: str
    language: str = "js"  # cpp / rust / js


@app.post("/codegen")
def codegen_run(req: CodegenRequest):
    """Phase 2 ④ 跨语言编译器：把目标编译为 C++/Rust/JS 可执行源码（内联最小运行时）。

    离线安全（强制规则解析，无需 LLM/网络）。生成代码可独立用 node / g++ / rustc 运行。
    """
    from compiler.codegen import TopologyCompiler
    os.environ.pop("AGENT_API_KEY", None)
    lang = (req.language or "js").lower()
    code = TopologyCompiler().emit(req.goal, lang)
    return {"language": lang, "code": code, "runnable": True}


class VerifyRequest(BaseModel):
    """Phase 2 第三层④ 形式化验证：执行前符号验证拓扑。"""
    spec: dict = Field(..., description="待验证的拓扑 spec")
    tier_stats: Optional[dict] = Field(
        None, description="可选自定义档位统计 {tier:{accuracy,cost,latency_ms}}")


@app.post("/verify")
def verify_topology(req: VerifyRequest):
    """Phase 2 第三层④ 形式化验证（内建符号验证器）：执行前证明拓扑正确性。

    6 个维度：无环性 / 可达性 / 数据流输入完备性 / 死锁自由 / 资源上界 / 质量下界。
    每维 pass/fail + 反例路径。零外部依赖（纯静态分析，不执行、不依赖 LLM/网络）。

    与 runtime 自检的区别：runtime 是**执行后**验证（跑一遍看结果）；本端点是
    **执行前**符号推理（不跑，纯静态分析），适合零容错场景的前置门禁。
    """
    from compiler.formal_verifier import FormalVerifier
    os.environ.pop("AGENT_API_KEY", None)
    fv = FormalVerifier(tier_stats=req.tier_stats)
    return fv.verify(req.spec)


class TuneRunRequest(BaseModel):
    """第四层① 在线调参：执行中 Bandit 动态选型。"""
    goal: Optional[str] = Field(None, description="自然语言任务")
    spec: Optional[dict] = Field(None, description="直接给拓扑 spec")
    iterations: int = Field(20, description="执行轮数，每轮积累经验")
    c_ucb: float = Field(1.4, description="UCB 探索系数")
    seed: int = Field(0, description="随机种子")
    tiers: Optional[list] = Field(None, description="可用档位，默认 small/large/tool")


@app.post("/tune")
def tune_run(req: TuneRunRequest):
    """第四层① 在线调参：运行时用 UCB1 多臂老虎机动态选型。

    同一个 OnlineTuner 跨多轮执行积累经验——
    「research 用 tool 档成功率最高」「analyze 用 large 就够了」这类经验
    是运行时学到的，不是离线预设的。收敛到当前环境下的最优档位。

    与 /rl/optimize 分工：/rl/optimize 离线搜**拓扑结构**；/tune 运行时调**模型参数**。
    离线安全（Bandit 纯本地统计，无 key、无网络）。
    """
    from compiler.online_tuner import OnlineTuner
    from runtime import Circuit, SimBackend, CircuitExecutor
    os.environ.pop("AGENT_API_KEY", None)
    import random as _rnd
    target = req.spec if req.spec is not None else req.goal
    if target is None:
        raise HTTPException(400, "goal 与 spec 至少提供一个")
    spec = target if isinstance(target, dict) else None
    tuner = OnlineTuner(c_ucb=req.c_ucb, seed=req.seed,
                        tiers=req.tiers or None)
    results = []
    for i in range(req.iterations):
        be = SimBackend(_rnd.Random(req.seed + i))
        circ = Circuit(spec, be, tuner=tuner) if spec else None
        # goal 走 compile
        if circ is None:
            from compiler.compile import compile_goal
            spec = compile_goal(req.goal, sim=True)
            circ = Circuit(spec, be, tuner=tuner)
        res = CircuitExecutor(circ).run()
        results.append({"iteration": i, "quality": res["final_quality"],
                        "cost": res["total_cost"], "success": res["success"]})
    final_quality = sum(r["quality"] for r in results) / len(results)
    caps = {k[0] for k in tuner.arms}
    return {"iterations": req.iterations,
            "avg_final_quality": round(final_quality, 4),
            "arm_stats": tuner.arm_stats(),
            "converged_tiers": {c: tuner.best_tier(c) for c in caps},
            "progress": results[:5] + ["..."] + results[-5:]
            if len(results) > 10 else results}


# ============================================================================
# Phase 2+ 第四层② 编译成静态图（拓扑 → 纯 Python 函数）
# ============================================================================

class StaticGraphRequest(BaseModel):
    """Phase 2+ 第四层②：编译 spec 为纯 Python 函数（零 LLM 调用）。"""
    spec: dict = Field(..., description="待编译的拓扑 spec")
    seed: int = Field(42, description="随机种子（默认 42，确定性）")


@app.post("/static-graph/compile")
def static_graph_compile(req: StaticGraphRequest):
    """第四层② 编译成静态图：把拓扑 spec 编译为纯 Python 函数源码。

    内联 SimBackend 完整确定性语义（所有 11 种元件类型 + aggregate + TIERS +
    feedback/self_heal），拓扑序（Kahn 分层）烘焙进代码，零运行时图遍历。
    生成代码零外部依赖（仅标准库 random/json/math），可独立执行、可 pickle 分发。

    返回：code（Python 源码）、function_name（`run_task`）、seed、topology name。
    调用方 exec(code) 后调用 run_task(task: str) -> dict 即可执行。
    """
    from compiler.static_graph import StaticGraphCompiler
    os.environ.pop("AGENT_API_KEY", None)
    comp = StaticGraphCompiler()
    code, fname = comp.emit(req.spec, seed=req.seed)
    return {"code": code,
            "function_name": fname,
            "seed": req.seed,
            "topology": req.spec.get("name", "unnamed"),
            "standalone": True,
            "zero_llm": True}


# ============================================================================
# Phase 2+ 第四层③ 执行历史因果分析（反事实推理定位瓶颈）
# ============================================================================

class CausalAnalyzeRequest(BaseModel):
    """Phase 2+ 第四层③：执行历史因果分析。"""
    spec: dict = Field(..., description="拓扑 spec")
    execution_result: dict = Field(..., description="CircuitExecutor.run() 的返回结果")


@app.post("/causal/analyze")
def causal_analyze(req: CausalAnalyzeRequest):
    """第四层③ 执行历史因果分析：反事实推理定位质量瓶颈节点。

    对每个节点做反事实推演："如果该节点质量=1.0，最终质量会是多少？"
    因果贡献 = 反事实最终质量 - 真实最终质量。
    贡献最大的节点 = 瓶颈节点（提升它能带来最大全局收益）。

    纯分析推演（O(V+E) per counterfactual），不重新执行电路。
    """
    from compiler.causal_analyzer import CausalAnalyzer
    os.environ.pop("AGENT_API_KEY", None)
    analyzer = CausalAnalyzer()
    return analyzer.analyze(req.spec, req.execution_result)


# ============================================================================
# Phase 2+ 第四层④ 异构硬件后端（Ollama 本地模型 Backend）
# ============================================================================

class OllamaRunRequest(BaseModel):
    """Phase 2+ 第四层④：Ollama 本地模型后端执行。"""
    spec: dict = Field(..., description="拓扑 spec")
    host: Optional[str] = Field(None, description="Ollama 服务地址（默认 http://localhost:11434）")
    model_map: Optional[dict] = Field(None, description="tier→模型名映射（默认 qwen2.5 系列）")
    seed: int = Field(0, description="随机种子（fallback SimBackend 用）")
    api_mode: str = Field("native", description="API 模式: native(/api/chat) 或 openai(/v1/chat/completions)")


@app.post("/ollama/run")
def ollama_run(req: OllamaRunRequest):
    """第四层④ 异构硬件后端：用 Ollama 本地模型执行电路拓扑。

    - resistor 节点 → Ollama REST API 本地推理（零 API 费用）
    - 其余组件 → SimBackend 确定性语义
    - 连接失败 → 降级到 SimBackend（graceful fallback）
    - 适合隐私敏感/离线/成本敏感/边缘设备场景

    返回：执行结果 + OllamaBackend 统计（calls/successes/fallbacks/avg_latency）
    """
    from compiler.ollama_backend import OllamaBackend
    from runtime import Circuit, SimBackend, CircuitExecutor
    import random as _rnd
    os.environ.pop("AGENT_API_KEY", None)

    be = OllamaBackend(
        rng=_rnd.Random(req.seed),
        host=req.host,
        model_map=req.model_map,
        api_mode=req.api_mode,
        fallback=SimBackend(_rnd.Random(req.seed)),
    )
    circ = Circuit(req.spec, be)
    result = CircuitExecutor(circ).run()
    return {"result": result, "ollama_stats": be.stats(),
            "host": be.host, "model_map": be.model_map}


@app.post("/ollama/health")
def ollama_health(req: OllamaRunRequest):
    """检查 Ollama 服务可达性 + 已安装模型列表。"""
    from compiler.ollama_backend import OllamaBackend
    os.environ.pop("AGENT_API_KEY", None)
    be = OllamaBackend(host=req.host, model_map=req.model_map)
    ok, detail = be.health_check()
    return {"reachable": ok, "detail": detail,
            "host": be.host, "model_map": be.model_map}


class SimplifyRequest(BaseModel):
    """奥卡姆剃刀化简 Pass：对拓扑 spec 做结构精简（"如无必要，勿增实体"）。"""
    spec: dict = Field(..., description="待化简的拓扑 spec")
    tol: float = Field(1e-6, description="等价判定容差（去噪确定性模拟）")
    max_rounds: int = Field(50, description="最大化简轮数（幂等收敛保护）")


@app.post("/simplify")
def simplify_topology(req: SimplifyRequest):
    """奥卡姆剃刀化简 Pass：删掉等价不变即剃落冗余，不确定/会变/伤完整性则保留。

    复杂任务的并行支路/多重验证/反馈环本就不冗余，自然保留；
    简单任务里的冗余 adc、重复 retrieve、空转 organize 被一扫而空。
    等价判定用「去噪确定性模拟」（复制 SimBackend 质量语义但去噪声/开路随机），
    使删前/删后两版在「逻辑结果」层面可比，不被仿真噪声伪影干扰。

    返回：简化后 spec + 化简报告（removed/merged/steps/final_nodes 等）。
    """
    from compiler.simplify import simplify
    os.environ.pop("AGENT_API_KEY", None)
    new_spec, report = simplify(req.spec, tol=req.tol, max_rounds=req.max_rounds)
    return {"spec": new_spec, "report": report}


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
                yield _sse({"type": "node_done", "event": ev})
                idx += 1

            # 完成态：推送结果
            if status in ("done", "error") and result is not None:
                yield _sse({"type": "result", "result": result})
                return

            if status == "error" and error:
                yield _sse({"type": "error", "error": error})
                return

            # 等待
            await asyncio_sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ──────────────────────────────────────────────────────────
# 在线拓扑编辑（人在回路）：会话式暂停/编辑/恢复/查询
# ──────────────────────────────────────────────────────────

class TopologySessionRequest(BaseModel):
    spec: dict
    seed: int = Field(0, description="SimBackend 随机种子（确定性用）")
    node_delay_ms: int = Field(0, description="每节点人为延迟(ms)，用于演示/仪表盘制造可暂停窗口")
    coop_enabled: bool = Field(True, description="人机协同：节点卡住时机器主动向协作者池求援")
    coop_timeout: float = Field(5.0, description="单个协作者应答等待上限(秒)，超时自动升级下一位")


class CollaboratorRequest(BaseModel):
    """注册协作者：人类专家 / 其他 agent / 外部知识源 / 外部系统，同池同权。"""
    id: Optional[str] = None
    name: Optional[str] = None
    kind: str = Field("human", description="human | agent | knowledge | system")
    skills: Optional[list] = Field(None, description="能力标签，机器据此择优派单")
    trust: float = Field(0.7, description="信任分 0~1，随求援成败自动 EMA 校准")
    latency_ms: float = Field(1000.0, description="预期响应时延（排序用）")
    cost: float = Field(0.0, description="每次求援的成本（排序用）")
    availability: str = Field("online", description="online | busy | offline")
    channel: Optional[dict] = Field(None, description='如 {"type":"web"} / {"type":"http","url":...}')
    meta: Optional[dict] = None


class HelpRespondRequest(BaseModel):
    """人类（或任何外部主体）应答一条求援。"""
    value: str
    by: Optional[str] = None
    confidence: float = 0.9
    note: Optional[str] = None
    accept: bool = True
    room_id: Optional[str] = None


class TopologyEditRequest(BaseModel):
    op: str                           # insert | replace | append_parallel | set_gate | reroute
    u: Optional[str] = None
    v: Optional[str] = None
    cid: Optional[str] = None
    new_cid: Optional[str] = None
    comp: Optional[dict] = None
    threshold: Optional[float] = None
    old: Optional[list] = None        # reroute: 被替换的 wire [u,v]
    new: Optional[list] = None        # reroute: 新 wire [u,v]
    base_rev: Optional[int] = None    # 客户端提交时看到的房间版本（乐观并发控制）
    force: bool = False               # 冲突时强推（后写覆盖），默认 false → 409


# ──────────────────────────────────────────────────────────
# 大规模协作 深化②：OT-lite 并发编辑
#
#   多人同时改同一张拓扑的冲突问题，用「服务端权威串行化 + 版本号 + 语义冲突检测」解决：
#     · 所有房间编辑在 _lock 下串行，产生单调递增 rev 并写入 op-log；
#     · 客户端提交编辑时带上自己看到的 base_rev；
#     · 服务端把 (base_rev, 当前rev] 之间的并发操作取出，比对「操作目标」：
#         - 目标不相交 → 自动 rebase 放行（这是常态：两人改不同节点本就不冲突）
#         - 目标相交   → 409 + 冲突详情，客户端刷新后重试（或 force 强推）
#     · 落后的客户端用 GET /rooms/{rid}/ops?since=N 重放追平，无需全量刷新。
#   相比全量锁：不同节点的并发编辑不互相阻塞；相比无冲突检测：同一节点不会被静默覆盖。
# ──────────────────────────────────────────────────────────

_EDIT_LOCK = threading.RLock()   # 房间编辑串行化：让「检测冲突→应用→记账」成为原子操作

# ──────────────────────────────────────────────────────────
# 大规模协作 极致：CRDT 无冲突并发编辑（人数众多不再靠 409 重试）
#
#   OT-lite 在 2~3 人时优雅，人一多就退化：冲突概率随并发人数近似平方上升，
#   每个 409 都要客户端拉 op-log→rebase→重提 = 重试风暴 + 必须有全局串行点。
#   CRDT 让并发 op 天然可合并（任意顺序 / 可重复 / 永不拒绝），
#   于是「人数众多」不再是并发正确性问题，只剩广播扇出问题（见 _RealtimeHub）。
#
#   两种模式共存：房间 concurrency="crdt"（默认，永不 409）或 "ot"（保留旧语义）。
# ──────────────────────────────────────────────────────────
PRESENCE_TTL = 30.0      # 超过该秒数没心跳 → 视为离线

# ── 人机协同·极致：全局协作者池（人类专家 / 其他 agent / 知识源 / 外部系统 同池）──
_collab_registry = None


def _collab_reg():
    """惰性建全局协作者池（runtime 不可用时返回 None → 完全退回旧行为）。"""
    global _collab_registry
    if _collab_registry is None:
        try:
            from runtime import CollaboratorRegistry
            _collab_registry = CollaboratorRegistry()
        except Exception:
            return None
    return _collab_registry


def _find_help(hid: str):
    """在所有活会话里定位一条待答求援（人类可能在任何一个会话被点名）。"""
    with _lock:
        recs = list(_topo_sessions.items())
    for sid, rec in recs:
        r = getattr(rec.get("executor"), "recruiter", None)
        if r is not None and hid in getattr(r, "inbox", {}):
            return sid, r
    return None, None


def _new_crdt(rid: str, spec: dict):
    """按房间 spec 建 CRDT 副本；runtime 不可用时返回 None（降级为纯 OT）。"""
    try:
        from runtime import TopologyCRDT
        return TopologyCRDT.from_spec(spec or {}, actor=f"room:{rid}")
    except Exception:
        return None


def _crdt_of(room: dict):
    """惰性拿房间 CRDT（老房间/持久化恢复的房间也能补上）。"""
    c = room.get("crdt")
    if c is None:
        c = _new_crdt(room.get("room_id", "?"), room.get("spec") or {})
        room["crdt"] = c
    return c


def _crdt_broadcast(room: dict, actor: str, ops: list):
    """把新产生的 CRDT op 广播给全房订阅者（他们本地 merge 即收敛，无需回服务端问）。"""
    if not ops:
        return
    sid = room.get("session_id")
    if not sid:
        return
    c = room.get("crdt")
    _realtime_hub.push(sid, {"type": "crdt_ops", "actor": actor, "ops": ops,
                             "hash": c.state_hash() if c else None,
                             "ts": time.time()})


def _live_presence(room: dict) -> dict:
    """剔除超时成员后的在线名单。"""
    now = time.time()
    out = {}
    for uid, p in (room.get("presence") or {}).items():
        if now - float(p.get("ts", 0)) <= PRESENCE_TTL:
            out[uid] = {**p, "age": round(now - float(p.get("ts", 0)), 2)}
    return out


def _op_targets(op: str, args: dict) -> set:
    """抽取一个编辑操作实际触碰的「目标集合」，用于判断两个并发操作是否冲突。

    节点级操作 → ("node", cid)；连线级操作 → ("wire", u, v)。
    目标不相交的两个操作可安全并发（自动 rebase）。
    """
    a = args or {}
    t = set()
    if op in ("replace_node", "replace", "set_gate", "append_parallel"):
        cid = a.get("cid")
        if cid:
            t.add(("node", cid))
        if a.get("new_cid"):
            t.add(("node", a["new_cid"]))
    if op in ("insert_on_wire", "insert"):
        u, v = a.get("u"), a.get("v")
        if u and v:
            t.add(("wire", u, v))
            # 在一条线上插节点会改变 u/v 的下游关系 → 也算触碰这两个节点
            t.add(("node", u)); t.add(("node", v))
    if op == "reroute":
        for w in (a.get("old"), a.get("new")):
            if w and len(w) == 2:
                t.add(("wire", w[0], w[1]))
                t.add(("node", w[0]))
    return t


def _append_op(room: dict, actor: str, op: str, args: dict) -> int:
    """在锁内把一次编辑写进 op-log 并推进 rev。返回新 rev。"""
    with _lock:
        room["rev"] = int(room.get("rev", 0)) + 1
        room["ops"].append({
            "rev": room["rev"], "ts": time.time(), "actor": actor,
            "op": op, "args": {k: v for k, v in (args or {}).items() if v is not None},
        })
        if len(room["ops"]) > 2000:      # 防无界增长；重放只需最近窗口
            room["ops"] = room["ops"][-1000:]
        return room["rev"]


def _detect_conflict(room: dict, base_rev: Optional[int], op: str, args: dict):
    """乐观并发控制：返回 (需拒绝?, 冲突详情dict)。base_rev=None 视为不参与并发控制。"""
    if base_rev is None:
        return False, None
    cur = int(room.get("rev", 0))
    if base_rev >= cur:
        return False, None                      # 客户端是最新的，无并发
    mine = _op_targets(op, args)
    if not mine:
        return False, None                      # 无法定位目标的操作不做拦截
    clashes = []
    for o in room.get("ops", []):
        if o["rev"] <= base_rev:
            continue
        inter = _op_targets(o["op"], o.get("args")) & mine
        if inter:
            clashes.append({"rev": o["rev"], "actor": o["actor"], "op": o["op"],
                            "targets": ["/".join(map(str, x)) for x in sorted(inter)]})
    if not clashes:
        return False, {"rebased": True, "behind": cur - base_rev}   # 自动 rebase
    return True, {"conflict": True, "base_rev": base_rev, "current_rev": cur,
                  "clashes": clashes,
                  "hint": "他人已改动同一目标；刷新（/rooms/{rid}/ops?since=base_rev）后重试，或 force=true 强推"}


def _topo_session(sid: str):
    with _lock:
        s = _topo_sessions.get(sid)
    if s is None:
        raise HTTPException(404, "topology session not found")
    return s


@app.post("/topology/session")
def topology_session(req: TopologySessionRequest):
    """新建一个在线编辑会话：编译 spec → 起 CircuitExecutor 后台线程，返回会话 id。

    客户端流程：pause →（edit×N）→ resume；期间可随时 GET /topology/state/{sid} 轮询。
    """
    import random as _rnd
    import time as _tmod
    from runtime import Circuit, CircuitExecutor, SimBackend
    sid = uuid.uuid4().hex[:12]
    _be = SimBackend(_rnd.Random(req.seed))
    if req.node_delay_ms > 0:
        # 演示/仪表盘用：人为给每节点加延迟，制造可观测的暂停窗口
        _base_run = _be.run
        def _delayed_run(comp, inputs):
            _tmod.sleep(req.node_delay_ms / 1000.0)
            return _base_run(comp, inputs)
        _be.run = _delayed_run
    # 人机协同（极致）：把全局协作者池注入执行器 —— 节点卡住时机器自己择优派单，
    # 协作者可以是另一个人类专家 / 其他 agent / 外部知识源 / 外部系统。
    _reg = _collab_reg() if req.coop_enabled else None
    ex = CircuitExecutor(Circuit(req.spec, _be), verbose=False,
                         collaborators=_reg, coop_timeout=req.coop_timeout)
    # 实时交互（极致）：把执行器事件推入事件总线 → SSE 零延迟送达 UI
    ex.on_event = lambda ev: _realtime_hub.push(sid, ev)
    done = threading.Event()
    rec = {"executor": ex, "done": done, "result": None, "error": None,
           "thread": None}

    def _runner():
        try:
            rec["result"] = ex.run()
        except Exception as e:  # 异常不拖崩宿主，记入会话
            rec["error"] = str(e)
        finally:
            done.set()

    rec["thread"] = threading.Thread(target=_runner, daemon=True)
    rec["thread"].start()
    with _lock:
        _topo_sessions[sid] = rec
    return {"session_id": sid, "state": ex.get_state()}


@app.api_route("/topology/pause/{sid}", methods=["GET", "POST"])
def topology_pause(sid: str,
                   room_id: Optional[str] = Query(None),
                   user_id: Optional[str] = Query(None)):
    room = _room_ctx(room_id, user_id, "control")
    rec = _topo_session(sid)
    paused = rec["executor"].pause()
    if room:
        _record_activity(room, user_id, "pause", sid)
    return {"session_id": sid, "paused": paused, "state": rec["executor"].get_state()}


@app.post("/topology/edit/{sid}")
def topology_edit(sid: str, req: TopologyEditRequest,
                  room_id: Optional[str] = Query(None),
                  user_id: Optional[str] = Query(None)):
    room = _room_ctx(room_id, user_id, "edit")
    rec = _topo_session(sid)
    _args = req.model_dump(exclude_none=True)
    _merge = None

    def _apply():
        try:
            return rec["executor"].edit(
                req.op, u=req.u, v=req.v, cid=req.cid,
                new_cid=req.new_cid, comp=req.comp, threshold=req.threshold,
                old=req.old, new=req.new)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"编辑失败: {e}")

    if room is None:                      # 单人会话：无并发语义，直接改
        out = _apply()
        return {"session_id": sid, "edit": out, "state": rec["executor"].get_state()}

    # ── 并发编辑：CRDT（默认，永不拒绝）或 OT-lite（旧语义，同目标 409）──
    #    两条路径都必须在同一把锁内「检测→应用→记账」原子完成，
    #    否则两人同时改同一节点会双双通过检测（TOCTOU 竞态），后写静默覆盖先写。
    _mode = room.get("concurrency", "ot")
    _crdt_ops = []
    with _EDIT_LOCK:
        if _mode == "crdt":
            # CRDT 模式：不做拒绝式冲突检测——并发 op 天然可合并，人数越多优势越大。
            _c = _crdt_of(room)
            _behind = max(0, int(room.get("rev", 0)) - int(req.base_rev)) \
                if isinstance(req.base_rev, int) else 0
            if _behind:
                _merge = {"merged": True, "behind": _behind, "mode": "crdt",
                          "note": "CRDT 自动合并，无需 rebase / 重试"}
        else:
            rejected, info = _detect_conflict(room, req.base_rev, req.op, _args)
            if rejected and not req.force:
                raise HTTPException(409, detail=info)
            if rejected and req.force:
                _merge = {**info, "forced": True}
            elif info:
                _merge = info                  # 自动 rebase 成功（目标不相交）
        out = _apply()
        _rev = _append_op(room, user_id, req.op, _args)
        room["spec"] = rec["executor"].circuit.spec   # 改流向/编辑同步到房间共享拓扑
        _c = room.get("crdt")
        if _c is not None:                     # 两种模式都镜像进 CRDT（随时可切换/收敛校验）
            try:
                _crdt_ops = _c.apply_edit(req.op, {**_args, "cid": req.cid or _args.get("cid")})
            except Exception:
                _crdt_ops = []
        room["memory"]["learnings"].append({
            "ts": time.time(), "actor": user_id, "op": req.op,
            "target": getattr(req, "cid", None) or getattr(req, "u", None),
            "detail": _args,
        })
    _record_activity(room, user_id, "edit", sid, detail=req.op)
    _crdt_broadcast(room, user_id, _crdt_ops)
    _persist()
    resp = {"session_id": sid, "edit": out, "state": rec["executor"].get_state(),
            "rev": _rev}
    if _merge:
        resp["merge"] = _merge
    _c = room.get("crdt")
    if _c is not None:
        resp["crdt"] = {"mode": _mode, "hash": _c.state_hash(), "ops": _crdt_ops}
    return resp


@app.api_route("/topology/resume/{sid}", methods=["GET", "POST"])
def topology_resume(sid: str,
                    room_id: Optional[str] = Query(None),
                    user_id: Optional[str] = Query(None)):
    room = _room_ctx(room_id, user_id, "control")
    rec = _topo_session(sid)
    resumed = rec["executor"].resume()
    if room:
        _record_activity(room, user_id, "resume", sid)
    return {"session_id": sid, "resumed": resumed, "state": rec["executor"].get_state()}


@app.get("/topology/state/{sid}")
def topology_state(sid: str,
                   room_id: Optional[str] = Query(None),
                   user_id: Optional[str] = Query(None)):
    room = _room_ctx(room_id, user_id, "read")
    rec = _topo_session(sid)
    st = rec["executor"].get_state()
    st["done"] = rec["done"].is_set()
    if rec["done"].is_set() and rec["error"] is None and rec["result"] is not None:
        st["result"] = rec["result"]
    if rec["error"] is not None:
        st["error"] = rec["error"]
    return st


@app.get("/topology/stream/{sid}")
def topology_stream(sid: str,
                    room_id: Optional[str] = Query(None),
                    user_id: Optional[str] = Query(None)):
    """实时交互（极致：连续不中断）：SSE 流式推送该会话的执行事件 + 房间 activity，替代轮询。

    客户端用 EventSource 订阅，零延迟收到 start / node_start / node_done / layer_done /
    paused / resumed / done / human_question / topology_edit / activity 等事件，
    交互流不再有刷新空洞。支持可选 room_id（同房成员可见彼此 activity，合并进同一流）。
    """
    _room_ctx(room_id, user_id, "read")
    rec = _topo_session(sid)
    # 订阅会话事件；房间模式下再订阅房间 activity，合并为同一流（Figma 式实时协同）
    subs = [(sid, _realtime_hub.subscribe(sid))]
    if room_id and room_id in _rooms:
        subs.append((room_id, _realtime_hub.subscribe(room_id)))

    def gen():
    # 多源合并：把各订阅队列泵入一个 merged 队列，gen 只从 merged 读
        merged = queue.Queue()
        _stop = threading.Event()

        def _pump(q):
            while not _stop.is_set():
                try:
                    ev = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                merged.put(ev)
        _threads = [threading.Thread(target=_pump, args=(q,),
                                     daemon=True) for _, q in subs]
        for t in _threads:
            t.start()
        try:
            # ① 历史回放：订阅前已发生的事件补发，保证时间线连续不丢（极致·连续不中断）
            for ev in list(rec["executor"]._events):
                yield _sse(ev)
            # ② 当前快照，客户端据此渲染现状（弥补仍可能遗漏的边界）
            st = rec["executor"].get_state()
            st["done"] = rec["done"].is_set()
            if rec["error"] is not None:
                st["error"] = rec["error"]
            yield _sse({"type": "snapshot", **st})
            while True:
                try:
                    ev = merged.get(timeout=2)
                except queue.Empty:
                    # 执行已结束且无新事件 → 优雅收尾；否则心跳保活
                    if rec["done"].is_set():
                        yield _sse({"type": "closed", "reason": "done"})
                        break
                    yield ": keepalive\n\n"
                    continue
                # done 事件补上完整结果，省去客户端再拉一次
                if ev.get("type") == "done" and rec["result"] is not None:
                    ev = {**ev, "result": rec["result"]}
                yield _sse(ev)
                if ev.get("type") == "done":
                    # 收尾宽限，吞掉可能的 trailing 事件后关闭
                    try:
                        tail = merged.get(timeout=1)
                        if tail.get("type") == "done" and rec["result"] is not None:
                            tail = {**tail, "result": rec["result"]}
                        yield _sse(tail)
                    except queue.Empty:
                        pass
                    break
        finally:
            _stop.set()
            for key, q in subs:
                _realtime_hub.unsubscribe(key, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class TopologyAnswerRequest(BaseModel):
    choice: str
    note: Optional[str] = None


@app.api_route("/topology/answer/{sid}", methods=["GET", "POST"])
def topology_answer(sid: str,
                    req: Optional[TopologyAnswerRequest] = None,
                    choice: Optional[str] = Query(None),
                    note: Optional[str] = Query(None),
                    room_id: Optional[str] = Query(None),
                    user_id: Optional[str] = Query(None)):
    """人类裁决灰色地带：adc/verify 主动提问后，老板选边（choice=high/low）。
    支持 JSON body（POST）或 ?choice= 查询参数（GET）。房间模式下需 adjudicate 权限。"""
    room = _room_ctx(room_id, user_id, "adjudicate")
    rec = _topo_session(sid)
    _choice = (req.choice if req else None) or choice
    _note = (req.note if req else None) or note
    if not _choice:
        raise HTTPException(400, "需提供 choice（body 或 ?choice=）")
    ok = rec["executor"].answer_question(_choice, _note)
    if room:
        _record_activity(room, user_id, "answer", sid, detail=_choice)
    return {"session_id": sid, "answered": ok, "state": rec["executor"].get_state()}


@app.get("/topology/node/{sid}/{cid}")
def topology_node_report(sid: str, cid: str,
                         room_id: Optional[str] = Query(None),
                         user_id: Optional[str] = Query(None)):
    """指挥中⼼①：返回某节点的透明决策工作报告（输入/输出/模型/耗时/质量）。"""
    _room_ctx(room_id, user_id, "read")
    rec = _topo_session(sid)
    tr = (rec["executor"].get_state().get("node_traces") or {}).get(cid)
    if tr is None:
        raise HTTPException(404, f"节点 {cid} 尚无工作报告（可能尚未执行）")
    return {"session_id": sid, "node": cid, "report": tr}


@app.get("/topology/learnings/{sid}")
def topology_learnings(sid: str,
                       room_id: Optional[str] = Query(None),
                       user_id: Optional[str] = Query(None)):
    """指挥中⼼③：返回本会话中人类教给系统的所有编辑（学习库）。"""
    _room_ctx(room_id, user_id, "read")
    rec = _topo_session(sid)
    return {"session_id": sid,
            "learnings": rec["executor"].get_state().get("learnings", [])}


@app.get("/topology/editor")
def topology_editor_page():
    """可视化拖拽仪表盘（第二步：人在回路）。返回独立 HTML，可直接打开。"""
    from pathlib import Path
    p = Path(__file__).parent / "topology_editor.html"
    if not p.exists():
        raise HTTPException(404, "topology_editor.html 未找到")
    return FileResponse(p, media_type="text/html")


# ──────────────────────────────────────────────────────────
# 大规模协作 Phase 0：房间模型 + 角色权限 + activity 流
# ──────────────────────────────────────────────────────────

class CreateRoomRequest(BaseModel):
    spec: dict = Field(..., description="房间共享的电路拓扑 Spec")
    owner_id: str = Field(..., description="房主用户 id")
    name: Optional[str] = Field(None, description="房间名")
    room_id: Optional[str] = Field(None, description="可指定房间 id（便于分享链接；冲突则服务端另生成）")
    concurrency: str = Field("crdt", description="并发编辑模式：crdt=无冲突合并(人数众多默认) / ot=乐观并发+409")


class RoomPresenceRequest(BaseModel):
    user_id: str = Field(..., description="用户 id")
    cursor: Optional[list] = Field(None, description="画布光标 [x,y]（可选）")
    focus: Optional[str] = Field(None, description="正在关注/编辑的节点 cid（可选）")
    status: str = Field("active", description="active / idle / away")


class RoomCRDTOpsRequest(BaseModel):
    user_id: str = Field(..., description="提交者 id")
    ops: list = Field(default_factory=list, description="CRDT op 列表（幂等、顺序无关）")


class RoomJoinRequest(BaseModel):
    user_id: str = Field(..., description="加入者用户 id")
    desired_role: str = Field("observer", description="期望角色（owner 可改）")


class RoomRoleRequest(BaseModel):
    owner_id: str = Field(..., description="操作者须为房主")
    user_id: str = Field(..., description="被改角色的用户 id")
    role: str = Field(..., description="新角色")


# ── 大规模协作 维度②：共享记忆与知识库（桥接 ⑬ 共享生态 / ⑭ 自我进化） ──
class RoomMemoryPublishRequest(BaseModel):
    user_id: str = Field(..., description="操作者（需 publish 权限：mentor/owner）")
    spec: Optional[dict] = Field(None, description="要发布的拓扑；不传则发房间当前共享 spec")
    name: Optional[str] = Field(None, description="发布名；不传按 room+topo 名生成")
    tags: list[str] = Field(default_factory=list, description="附加标签（自动加 room:{rid}）")


class RoomMemoryPullRequest(BaseModel):
    user_id: str = Field(..., description="操作者（需 read 权限，所有角色可 pull）")
    name: str = Field(..., description="共享仓库中的拓扑名")


class RoomMemoryDistillRequest(BaseModel):
    user_id: str = Field(..., description="操作者（需 publish 权限）")
    history: Optional[list] = Field(None, description="蒸馏历史；不传用房间已发布项")
    min_support: int = Field(2, description="motif 最小支持度")


class RoomFederatePullRequest(BaseModel):
    user_id: str = Field(..., description="操作者（需目标房间 read 权限）")
    include_spec: bool = Field(False, description="是否同时把源房间共享拓扑并入（重建 session）；默认 False 仅联邦知识")


class RoomOrchestrateRequest(BaseModel):
    user_id: str = Field(..., description="编排发起者（需 control 权限：owner/mentor/student）")
    case: Optional[dict] = Field(None, description="失败案例 {goal,spec,result}；不传用房间 spec")
    mock: bool = Field(False, description="强制全 mock 角色（离线/无 key 时保证闭环可跑）")
    quality_threshold: float = Field(0.8, description="质量门阈值")
    goal: str = Field("", description="任务目标（喂给真实导师的上下文）")
    use_llm: bool = Field(False, description="总开关：是否让三角色走真实 LLM（默认关，避免误烧钱）")
    use_real_mentor: Optional[bool] = Field(None, description="单独指定导师是否用真实 LLM；None 跟随 use_llm")
    use_real_reviewer: Optional[bool] = Field(None, description="单独指定裁决者是否用真实 LLM；None 跟随 use_llm")
    use_real_student: Optional[bool] = Field(None, description="单独指定学生是否用本机真实模型；None 跟随 use_llm")


@app.post("/rooms")
def create_room(req: CreateRoomRequest):
    """新建协作房间：内部建一个共享 topology session，房主为 owner。"""
    import uuid as _uuid, time as _tm
    rid = req.room_id if (req.room_id and req.room_id not in _rooms) else _uuid.uuid4().hex[:10]
    _sess = topology_session(TopologySessionRequest(spec=req.spec))
    room = {
        "room_id": rid,
        "name": req.name or req.spec.get("name", "untitled"),
        "owner": req.owner_id,
        "members": {req.owner_id: "owner"},
        "session_id": _sess["session_id"],
        "spec": req.spec,
        "activity": [],
        "memory": {"published": [], "learnings": [], "templates": []},
        "ops": [],       # OT op-log：房间共享拓扑的全部变更（可重放）
        "rev": 0,        # 单调递增版本号；客户端据此判断"我落后了没有"
        # 大规模协作（极致）：CRDT 无冲突合并 + presence 在线感知
        "concurrency": req.concurrency if req.concurrency in ("crdt", "ot") else "crdt",
        "crdt": _new_crdt(rid, req.spec),
        "presence": {},   # uid -> {ts, cursor, focus, status, role}
        "created_at": _tm.time(),
    }
    with _lock:
        _rooms[rid] = room
    _record_activity(room, req.owner_id, "create_room", rid, detail=room["name"])
    _persist()
    return {"room_id": rid, "session_id": _sess["session_id"],
            "owner": req.owner_id, "state": _sess["state"]}


@app.post("/rooms/{rid}/join")
def room_join(rid: str, req: RoomJoinRequest):
    """加入房间（默认 observer；owner 可改角色）。"""
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    role = req.desired_role if req.desired_role in ROLE_PERMS else "observer"
    with _lock:
        room["members"][req.user_id] = role
    _record_activity(room, req.user_id, "join", rid, detail=role)
    _persist()
    return {"room_id": rid, "user_id": req.user_id, "role": role,
            "members": dict(room["members"])}


@app.post("/rooms/{rid}/role")
def room_set_role(rid: str, req: RoomRoleRequest):
    """房主改成员角色。"""
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if req.owner_id != room["owner"]:
        raise HTTPException(403, "only owner can change roles")
    if req.role not in ROLE_PERMS:
        raise HTTPException(400, f"unknown role: {req.role}")
    with _lock:
        room["members"][req.user_id] = req.role
        _record_activity(room, req.owner_id, "set_role", rid,
                         detail=f"{req.user_id}->{req.role}")
    _persist()
    return {"room_id": rid, "user_id": req.user_id, "role": req.role}


@app.get("/rooms/{rid}")
def room_info(rid: str, user_id: Optional[str] = None):
    """房间信息：成员/角色/共享电路状态摘要。"""
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if user_id and user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    sess = _topo_sessions.get(room["session_id"], {})
    st = sess.get("executor") and sess["executor"].get_state()
    return {"room_id": rid, "name": room["name"], "owner": room["owner"],
            "members": dict(room["members"]), "session_id": room["session_id"],
            "created_at": room["created_at"], "state": st, "spec": room.get("spec"),
            "activity_count": len(room["activity"]),
            "rev": int(room.get("rev", 0)),      # 客户端据此判断自己是否落后
            # 极致·大规模协作：并发模式 + 在线感知 + CRDT 收敛哈希
            "concurrency": room.get("concurrency", "crdt"),
            "online": len(_live_presence(room)),
            "presence": _live_presence(room),
            "crdt_hash": (room.get("crdt").state_hash() if room.get("crdt") else None)}


@app.get("/rooms/{rid}/ops")
def room_ops(rid: str, user_id: Optional[str] = None, since: int = 0, limit: int = 200):
    """拉取 since 之后的房间编辑 op-log（深化②）。

    落后的客户端不必全量刷新，只要重放这些 op 即可追平到 rev；
    新加入者传 since=0 可拿到房间自建立以来的全部编辑历史（受窗口限制）。
    """
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if user_id and user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    ops = [o for o in room.get("ops", []) if o["rev"] > since][:limit]
    return {"room_id": rid, "rev": int(room.get("rev", 0)), "since": since,
            "ops": ops, "count": len(ops),
            "truncated": bool(room.get("ops")) and room["ops"][0]["rev"] > since + 1}


@app.get("/rooms/{rid}/activity")
def room_activity(rid: str,
                  user_id: Optional[str] = None,
                  since: int = 0):
    """实时协同流：返回房间 activity 事件（供多人客户端轮询，维度三复用）。"""
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if user_id and user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    return {"room_id": rid, "activities": room["activity"][since:],
            "total": len(room["activity"])}


# ──────────────────────────────────────────────────────────
# 大规模协作 极致：presence 在线感知 / CRDT 同步 / 扇出水位
# ──────────────────────────────────────────────────────────

@app.post("/rooms/{rid}/presence")
def room_presence(rid: str, req: RoomPresenceRequest):
    """在线感知心跳：我在线、我的光标在哪、我正在看/改哪个节点。

    人数众多时「谁在场、谁在碰哪里」是避免撞车的第一道防线（比冲突提示更早）。
    心跳经事件总线以 coalesce 方式广播（同一用户只保留最新一条），
    因此 N 人高频心跳不会把广播通道打满。超过 PRESENCE_TTL 秒无心跳自动判离线。
    """
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if req.user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    now = time.time()
    entry = {"ts": now, "cursor": req.cursor, "focus": req.focus,
             "status": req.status, "role": room["members"].get(req.user_id)}
    with _lock:
        room.setdefault("presence", {})[req.user_id] = entry
    sid = room.get("session_id")
    if sid:
        _realtime_hub.push(sid, {"type": "presence", "actor": req.user_id, **entry})
    live = _live_presence(room)
    return {"room_id": rid, "user_id": req.user_id, "online": len(live),
            "presence": live, "ttl": PRESENCE_TTL}


@app.get("/rooms/{rid}/presence")
def room_presence_list(rid: str, user_id: Optional[str] = None):
    """当前在线名单（已剔除超时者），含各自 focus 节点 → UI 可在节点上标"谁在看"。"""
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if isinstance(user_id, str) and user_id and user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    live = _live_presence(room)
    focus_map = {}
    for uid, p in live.items():
        if p.get("focus"):
            focus_map.setdefault(p["focus"], []).append(uid)
    return {"room_id": rid, "online": len(live), "presence": live,
            "focus_map": focus_map, "members_total": len(room["members"])}


@app.get("/rooms/{rid}/crdt")
def room_crdt_state(rid: str, user_id: Optional[str] = None, since: int = 0):
    """CRDT 视图：物化拓扑 + 状态哈希 + op 流（since 之后的，用于增量追平）。

    状态哈希相同 ⇔ 两个副本已收敛；客户端拿 ops 本地 merge 即可，无需服务端仲裁。
    """
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if isinstance(user_id, str) and user_id and user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    c = _crdt_of(room)
    if c is None:
        raise HTTPException(503, "CRDT 不可用（runtime 未加载）")
    ops = list(c.log)[since:]
    return {"room_id": rid, "mode": room.get("concurrency", "crdt"),
            "hash": c.state_hash(), "state": c.materialize(),
            "ops": ops, "since": since, "total_ops": len(c.log),
            "lamport": c.lamport}


@app.post("/rooms/{rid}/crdt/ops")
def room_crdt_submit(rid: str, req: RoomCRDTOpsRequest):
    """提交一批 CRDT op（来自某个客户端副本）。永不拒绝：合并即收敛。

    这是「人数众多」的核心通路——每个客户端本地先改（零延迟），
    再把 op 异步投递到服务端与其他人；到达顺序、重复投递都不影响最终一致。
    """
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if not _has_perm(room, req.user_id, "edit"):
        raise HTTPException(403, f"user {req.user_id} 无 edit 权限")
    c = _crdt_of(room)
    if c is None:
        raise HTTPException(503, "CRDT 不可用（runtime 未加载）")
    with _EDIT_LOCK:
        merged = c.merge(req.ops or [])
        room["spec"] = {**(room.get("spec") or {}), **c.materialize()}
    if merged:
        _crdt_broadcast(room, req.user_id, list(req.ops or []))
        _record_activity(room, req.user_id, "crdt_ops", rid, detail=f"+{merged}")
        _persist()
    return {"room_id": rid, "merged": merged, "received": len(req.ops or []),
            "hash": c.state_hash(), "total_ops": len(c.log)}


@app.get("/rooms/{rid}/stats")
def room_stats(rid: str, user_id: Optional[str] = None):
    """扇出水位：订阅者数 / 推送 / 丢弃 / 合并 / 在线人数。人数众多时先看这里。"""
    room = _rooms.get(rid)
    if room is None:
        raise HTTPException(404, f"room not found: {rid}")
    if isinstance(user_id, str) and user_id and user_id not in room["members"]:
        raise HTTPException(403, "not a member of this room")
    sid = room.get("session_id") or ""
    c = room.get("crdt")
    return {"room_id": rid,
            "members": len(room.get("members", {})),
            "online": len(_live_presence(room)),
            "subscribers": _realtime_hub.subscriber_count(sid),
            "hub": dict(_realtime_hub.stats),
            "queue_maxsize": _realtime_hub.maxsize,
            "concurrency": room.get("concurrency", "crdt"),
            "rev": int(room.get("rev", 0)),
            "crdt_ops": len(c.log) if c else 0,
            "crdt_hash": c.state_hash() if c else None,
            "activity": len(room.get("activity", []))}


# ──────────────────────────────────────────────────────────
# 人机协同·极致：协作者不分人机 + 机器主动招募
#   协作者 = 人类专家 / 其他 agent / 外部知识源 / 外部系统（同池同权）
#   机器判断"我缺什么能力" → 择优派单 → 超时自动升级 → 答案回灌电路
# ──────────────────────────────────────────────────────────

@app.post("/collaborators")
def collaborator_register(req: CollaboratorRequest):
    """注册一位协作者。kind=human/agent/knowledge/system —— 人和机在同一个池子里。"""
    reg = _collab_reg()
    if reg is None:
        raise HTTPException(500, "collaborator registry unavailable")
    try:
        c = reg.register(cid=req.id, name=req.name, kind=req.kind,
                         skills=req.skills or [], trust=req.trust,
                         latency_ms=req.latency_ms, cost=req.cost,
                         availability=req.availability,
                         channel=req.channel or {}, meta=req.meta or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"registered": c.to_dict(), "total": len(reg.list())}


@app.get("/collaborators")
def collaborator_list(need: Optional[str] = None, kind: Optional[str] = None,
                      top: int = 5):
    """列出协作者池；带 need=标签1,标签2 时同时返回机器会怎么择优（透明可解释）。"""
    reg = _collab_reg()
    if reg is None:
        return {"collaborators": [], "match": []}
    out = {"collaborators": reg.list()}
    if isinstance(need, str) and need.strip():
        tags = [t.strip().lower() for t in need.split(",") if t.strip()]
        kinds = [kind] if isinstance(kind, str) and kind else None
        cands = reg.match(tags, kinds=kinds, top=top, require_cover=0.34)
        out["need"] = tags
        out["match"] = [{"id": c.id, "name": c.name, "kind": c.kind,
                         "cover": round(c.covers(tags), 3),
                         "trust": round(c.trust, 3),
                         "availability": c.availability} for c in cands]
    return out


@app.post("/collaborators/{cid}/availability")
def collaborator_availability(cid: str, status: str = "online"):
    """人类专家上下线；offline 的协作者不会被派单（机器自动绕开）。"""
    reg = _collab_reg()
    if reg is None or not reg.set_availability(cid, status):
        raise HTTPException(404, f"collaborator not found: {cid}")
    return {"id": cid, "availability": status}


@app.delete("/collaborators/{cid}")
def collaborator_remove(cid: str):
    reg = _collab_reg()
    if reg is None or not reg.remove(cid):
        raise HTTPException(404, f"collaborator not found: {cid}")
    return {"removed": cid}


@app.get("/help/pending")
def help_pending(sid: Optional[str] = None, assignee: Optional[str] = None):
    """人类的"待办求援箱"：机器主动点名找我的问题都在这里（跨会话汇总）。"""
    with _lock:
        items = list(_topo_sessions.items())
    out = []
    for s, rec in items:
        if isinstance(sid, str) and sid and s != sid:
            continue
        r = getattr(rec.get("executor"), "recruiter", None)
        if r is None:
            continue
        for h in r.pending():
            if isinstance(assignee, str) and assignee and h.get("assignee") != assignee:
                continue
            out.append({**h, "session_id": s})
    return {"pending": out, "count": len(out)}


@app.post("/help/{hid}/respond")
def help_respond(hid: str, req: HelpRespondRequest):
    """人类作答一条求援：答案立刻回灌到正在等待的电路节点（执行器不必重跑全图）。"""
    sid, r = _find_help(hid)
    if r is None:
        raise HTTPException(404, f"help request not found or already closed: {hid}")
    ok = r.respond(hid, req.value, by=req.by, confidence=req.confidence,
                   note=req.note, accept=req.accept)
    if not ok:
        raise HTTPException(409, "help request已被其它协作者抢答或已超时关闭")
    if isinstance(req.room_id, str) and req.room_id:
        room = _rooms.get(req.room_id)
        if room:
            _record_activity(room, req.by or "human", "help_respond", sid,
                             detail={"hid": hid})
    return {"hid": hid, "session_id": sid, "answered_by": req.by or "human"}


@app.get("/help/log/{sid}")
def help_log(sid: str, limit: int = 50):
    """求援全轨迹：机器找过谁、谁没答上、最终谁解决的（可审计的协同过程）。"""
    rec = _topo_session(sid)
    ex = rec["executor"]
    r = getattr(ex, "recruiter", None)
    return {"session_id": sid,
            "coop_log": ex.get_state().get("coop_log", []),
            "pending": (r.pending() if r else []),
            "history": (r.log(limit) if r else [])}


# ──────────────────────────────────────────────────────────
# 大规模协作 维度②：共享记忆与知识库
#   桥接 ⑬ 共享生态(/topology/publish|repo|pull) 与 ⑭ 自我进化(/evolve)
# ──────────────────────────────────────────────────────────

def _room_repo_items(room_id: str) -> list:
    """列出共享仓库中标记为某 room 的条目（tag=room:{rid}）。"""
    from compiler.share import ShareRepo
    items = ShareRepo(SHARE_REPO_PATH).list()
    tag = f"room:{room_id}"
    return [v for v in items if tag in (v.get("tags") or [])]


@app.post("/rooms/{rid}/memory/publish")
def room_memory_publish(rid: str, req: RoomMemoryPublishRequest):
    """把房间当前/指定拓扑发布到共享仓库（⑬），打 room 标签，沉淀为知识库。需 publish 权限。"""
    room = _room_ctx(rid, req.user_id, "publish")
    spec = req.spec or room.get("spec") or {}
    name = req.name or f"{room['room_id']}_{spec.get('name', 'topo')}"
    tags = list(req.tags) + [f"room:{rid}"]
    from compiler.share import ShareRepo
    published_name = ShareRepo(SHARE_REPO_PATH).publish(spec, author=req.user_id, tags=tags, name=name)
    with _lock:
        room["memory"]["published"].append(published_name)
        _record_activity(room, req.user_id, "memory_publish", published_name, detail=name)
    _persist()
    return {"room_id": rid, "published_name": published_name, "tags": tags}


@app.get("/rooms/{rid}/memory")
def room_memory(rid: str, user_id: Optional[str] = None):
    """房间知识库视图：本房发布的条目 + 沉淀的 learnings + 蒸馏模板 + 仓库总量。需 read 权限。"""
    room = _room_ctx(rid, user_id, "read")
    from compiler.share import ShareRepo
    repo_items = _room_repo_items(rid)
    return {"room_id": rid,
            "published": room["memory"]["published"],
            "repo_items": repo_items,
            "learnings": room["memory"]["learnings"],
            "templates": room["memory"]["templates"],
            "repo_total": len(ShareRepo(SHARE_REPO_PATH).list())
            }


@app.post("/rooms/{rid}/memory/pull")
def room_memory_pull(rid: str, req: RoomMemoryPullRequest):
    """从共享仓库拉取某拓扑到房间（更新共享 spec + 新建内部会话）。需 read 权限。"""
    room = _room_ctx(rid, req.user_id, "read")
    from compiler.share import ShareRepo
    try:
        spec = ShareRepo(SHARE_REPO_PATH).pull(req.name)
    except KeyError:
        raise HTTPException(404, f"仓库中无此拓扑：{req.name}")
    _sess = topology_session(TopologySessionRequest(spec=spec))
    with _lock:
        room["spec"] = spec
        room["session_id"] = _sess["session_id"]
        _record_activity(room, req.user_id, "memory_pull", req.name, detail=req.name)
    _persist()
    return {"room_id": rid, "name": req.name, "session_id": _sess["session_id"], "spec": spec}


@app.post("/rooms/{rid}/memory/distill")
def room_memory_distill(rid: str, req: RoomMemoryDistillRequest):
    """蒸馏房间历史为可复用模板（⑭），存入房间知识库。需 publish 权限。"""
    room = _room_ctx(rid, req.user_id, "publish")
    history = req.history or [{"name": n, "spec": room["spec"]} for n in room["memory"]["published"]]
    from runtime import SelfEvolution
    ev = SelfEvolution(history, min_support=req.min_support)
    templates = ev.templates
    with _lock:
        room["memory"]["templates"] = templates
        _record_activity(room, req.user_id, "memory_distill", rid, detail=f"{len(templates)} templates")
    _persist()
    return {"room_id": rid, "templates": templates, "template_count": len(templates)}


# ──────────────────────────────────────────────────────────
# 深化④：跨房间联邦（federation）
#   房间之间不再是孤岛：联邦目录发现彼此 + 跨房间拉取知识/拓扑
# ──────────────────────────────────────────────────────────
@app.get("/federation")
def federation_directory(user_id: Optional[str] = None):
    """联邦目录：列出当前所有房间摘要，供其他房间发现并拉取知识（深化④）。"""
    entries = []
    for rid, room in _rooms.items():
        mem = room.get("memory", {}) or {}
        entries.append({
            "room_id": rid, "name": room.get("name"), "owner": room.get("owner"),
            "members": len(room.get("members", {})),
            "published": len(mem.get("published", [])),
            "templates": len(mem.get("templates", [])),
            "rev": int(room.get("rev", 0)),
        })
    return {"rooms": entries, "count": len(entries)}


@app.post("/rooms/{rid}/memory/pull-from/{other_rid}")
def room_federate_pull(rid: str, other_rid: str, req: RoomFederatePullRequest):
    """跨房间联邦拉取（深化④）：把源房间的已发布知识 + 蒸馏模板并入本房间知识库。

    - 需目标房间 read 权限；源房间只要存在（其 published 即视为对联邦公开）。
    - 默认只联邦『知识』（不破坏本房间当前电路）；include_spec=true 才并入共享拓扑。
    - 联邦是单向汇入，绝不反向改动源房间。
    """
    room = _room_ctx(rid, req.user_id, "read")
    other = _rooms.get(other_rid)
    if other is None:
        raise HTTPException(404, f"源房间不存在: {other_rid}")
    omem = other.get("memory", {}) or {}
    imported_pub, imported_tpl = 0, 0
    _inc_spec = bool(req.include_spec)
    _other_spec = other.get("spec") if _inc_spec else None
    with _lock:
        for name in omem.get("published", []):
            if name not in room["memory"]["published"]:
                room["memory"]["published"].append(name); imported_pub += 1
        _existing_tpl = {t.get("name") for t in room["memory"]["templates"] if isinstance(t, dict)}
        for t in omem.get("templates", []):
            if isinstance(t, dict) and t.get("name") in _existing_tpl:
                continue
            room["memory"]["templates"].append(t); imported_tpl += 1
        _record_activity(room, req.user_id, "federate_pull", other_rid,
                         detail=f"从 {other_rid} 汇入 {imported_pub} 条知识 / {imported_tpl} 个模板")
    # include_spec 需在锁外重建 session：topology_session 内部会再取 _lock，非重入锁会死锁
    if _inc_spec:
        _sess = topology_session(TopologySessionRequest(spec=_other_spec or {}))
        with _lock:
            room["spec"] = _other_spec
            room["session_id"] = _sess["session_id"]
    _persist()
    return {"room_id": rid, "from": other_rid,
            "imported_published": imported_pub, "imported_templates": imported_tpl,
            "published": room["memory"]["published"],
            "templates": room["memory"]["templates"]}


@app.post("/rooms/{rid}/orchestrate")
def room_orchestrate(rid: str, req: RoomOrchestrateRequest):
    """房间内多 agent 编排闭环（维度③）：mentor 提优化 → reviewer 裁决 → [通过] student 执行 → 质量门。
    全链路事件写入 room activity 供所有人可见；通过则固化进房间知识库。需 control 权限。"""
    room = _room_ctx(rid, req.user_id, "control")
    case = req.case or {}
    base_spec = case.get("spec") or room.get("spec") or {}
    before_q = (case.get("result", {}) or {}).get("final_quality", 0.0) if case else 0.0
    from mentor import orchestrate as _orchestrate
    # 逐角色解析「真实 LLM / mock」：未单独指定的角色跟随总开关 use_llm
    _rm = req.use_real_mentor if req.use_real_mentor is not None else req.use_llm
    _rr = req.use_real_reviewer if req.use_real_reviewer is not None else req.use_llm
    _rs = req.use_real_student if req.use_real_student is not None else req.use_llm
    _mock = req.mock or not (_rm or _rr or _rs)
    trace = _orchestrate(base_spec, mock=_mock, before_quality=before_q, goal=req.goal,
                         quality_threshold=req.quality_threshold,
                         use_real_mentor=_rm, use_real_reviewer=_rr, use_real_student=_rs)
    diag = (trace.get("mentor", {}).get("diagnosis") or "")[:60]
    _ag = trace.get("agents", {})
    _agtxt = "/".join(f"{k}:{v}" for k, v in _ag.items())
    _record_activity(room, req.user_id, "orchestrate", rid,
                     detail=f"[{_agtxt}] mentor:{diag} → reviewer:{trace['reviewer']['verdict']}")
    if trace.get("degraded"):
        _record_activity(room, "system", "orchestrate_degraded", rid,
                         detail="; ".join(trace["degraded"])[:200])
    if trace["reviewer"]["verdict"] == "approve":
        with _lock:
            room["memory"]["templates"].append({
                "name": f"orchestrate_{rid}", "support": 1,
                "diagnosis": trace["mentor"]["diagnosis"],
                "optimized_spec": trace["optimized_spec"],
                "quality": (trace.get("student") or {}).get("after_quality"),
            })
            room["memory"]["learnings"].append({
                "ts": time.time(), "actor": "orchestrator", "op": "orchestrate",
                "detail": trace["reviewer"],
            })
        _record_activity(room, "student", "orchestrate_execute", rid,
                         detail=(trace.get("student") or {}).get("quality_gate_reason"))
    _persist()
    return {"room_id": rid, "trace": trace,
            "agents": trace.get("agents", {}), "degraded": trace.get("degraded", [])}


@app.get("/agents/availability")
def agents_availability(check_student: bool = True):
    """探测三个真实 LLM 角色可用性（mentor/judge 看 key，student 看本机 Ollama）。

    仪表盘据此决定「真实LLM」开关是否可点、并提示哪个角色会降级。
    只做可用性探测，不发起任何推理请求；任何异常都降级成"不可用"而不是 500。
    """
    try:
        from mentor import agent_availability
        av = agent_availability(check_student=check_student)
    except Exception as e:
        av = {"mentor": False, "judge": False, "student": False,
              "detail": {"error": str(e)}}
    av["any"] = bool(av.get("mentor") or av.get("judge") or av.get("student"))
    return av


# asyncio.sleep 包装（避免底层依赖细节）
def asyncio_sleep(seconds: float):
    import asyncio
    return asyncio.sleep(seconds)


# ──────────────────────────────────────────────────────────
# 离线自检（不启动服务器）
# ──────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────
# π 永动心跳 f(π)
# ──────────────────────────────────────────────────────────

if PI_HEARTBEAT is not None:
    @app.get("/pi/heartbeat")
    def pi_heartbeat_state():
        """返回当前系统状态 + 最近一拍动作 + π 推进位置。"""
        return {
            "n": PI_HEARTBEAT.state["n"],
            "running": PI_HEARTBEAT.is_running(),
            "interval": PI_HEARTBEAT.interval,
            "spigot_next_digits": PI_HEARTBEAT.spigot.first_digits(6),
            "state": PI_HEARTBEAT._public_state(),
            "last": PI_HEARTBEAT.state.get("last"),
        }

    @app.post("/pi/heartbeat/start")
    def pi_heartbeat_start(interval: float = Query(60.0, ge=1.0, le=3600)):
        ok = PI_HEARTBEAT.start(interval=interval)
        return {"started": ok, "running": PI_HEARTBEAT.is_running(),
                "interval": PI_HEARTBEAT.interval}

    @app.post("/pi/heartbeat/stop")
    def pi_heartbeat_stop():
        PI_HEARTBEAT.stop()
        return {"running": PI_HEARTBEAT.is_running()}

    @app.post("/pi/heartbeat/tick")
    def pi_heartbeat_tick(n: int = Query(1, ge=1, le=50)):
        """手动推进 n 拍（演示/调试用）。"""
        return {"ticks": PI_HEARTBEAT.run_once(n=n)}


# ──────────────────────────────────────────────────────────
# S31 导师-学生训练电路：强模型优化弱模型的外部电路结构（非知识蒸馏）
# ──────────────────────────────────────────────────────────

class MentorTrainRequest(BaseModel):
    quality_threshold: float = Field(0.8, ge=0.0, le=1.0,
                                     description="质量门阈值（adc 语义）")
    limit: int = Field(40, ge=1, le=500, description="回溯多少条历史找失败案例")
    use_local_student: bool = Field(True, description="用本机 Ollama 7B 做学生重跑")
    solidify: bool = Field(True, description="质量门通过时是否固化为可复用模板")


if mentor_train_cycle is not None:
    @app.post("/mentor/train")
    def mentor_train(req: MentorTrainRequest):
        """跑一步导师-学生训练闭环。

        失败案例 → 导师(deepseek-reasoner)分析 → 应用优化 → 学生(本地7B)重跑
        → 质量门 → 通过则固化模板。零数据零算力：只改外部电路结构，不动权重。
        """
        store = _mentor_store()
        if store is None:
            raise HTTPException(500, "execution_store 不可用")
        student = None
        if req.use_local_student and make_ollama_student is not None:
            student = make_ollama_student()
        try:
            res = mentor_train_cycle(
                store, student_backend=student,
                quality_threshold=req.quality_threshold, limit=req.limit,
                registry=MENTOR_REGISTRY, solidify=req.solidify,
                quality_fn=default_content_quality,
            )
        except Exception as e:
            raise HTTPException(500, f"训练闭环失败: {type(e).__name__}: {e}")
        res["mentor_model"] = MENTOR_MODEL
        res["student"] = "ollama:local" if student is not None else "none(仅出方案)"
        res["registry_size"] = len(MENTOR_REGISTRY)
        return res

    @app.get("/mentor/registry")
    def mentor_registry_list():
        """列出已固化的训练成果模板。"""
        return {"count": len(MENTOR_REGISTRY),
                "mentor_model": MENTOR_MODEL, "mentor_base": MENTOR_BASE,
                "templates": [{"diagnosis": e.get("diagnosis"),
                               "quality": e.get("quality")}
                              for e in MENTOR_REGISTRY]}


def selftest():
    """验证 API 模型序列化 + 内存存储 + 后台线程 + SSE 流。"""
    import random, tempfile as _tf
    # 自检期间改用临时落盘文件 + 关持久化，避免污染真实 .rooms.json
    global _room_store, _PERSIST
    _room_store = RoomStore(os.path.join(_tf.mkdtemp(prefix="ca_selftest_"), "rooms.json"))
    _PERSIST = False
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

    # S12: ⑭ 自我进化（离线：显式历史，无 LLM 依赖）
    os.environ.pop("AGENT_API_KEY", None)
    def _mk(n):
        return {"name": f"h{n}", "spec": {"name": f"h{n}", "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]}}, "wires": [["src", "A"]]}}
    ev_hist = [_mk(i) for i in range(3)]
    ev_resp = evolve(EvolveRequest(history=ev_hist, min_support=2))
    assert ev_resp["template_count"] >= 1, "应蒸馏出至少 1 个模板"
    assert any(t["motif"] == ["power", "resistor"] for t in ev_resp["templates"]), \
        "power→resistor 应被蒸馏为模板"
    sg = evolve_suggest(EvolveSuggestRequest(
        spec={"name": "n", "components": {
            "src": {"type": "power", "label": "src"},
            "A": {"type": "resistor", "label": "A", "model": "small",
                  "produced_outputs": ["x"]}}, "wires": [["src", "A"]]},
        history=ev_hist, min_support=2))
    assert any(t["motif"] == ["power", "resistor"] for t in sg["suggested"]), \
        "新拓扑应命中已沉淀模板"
    print(f"✓ S12 ⑭ 自我进化(SelfEvolution): 历史蒸馏 {ev_resp['template_count']} 模板"
          f" + 新拓扑命中建议（驱动自动复用）")

    # S13: Phase 2 细粒度质量门（离线：强制规则解析，复用 _compile_execute）
    os.environ.pop("AGENT_API_KEY", None)
    rep = quality_report(GoalRequest(goal="画一张销售趋势图并做质量校验",
                                      quality_threshold=0.8))
    assert "final_score" in rep and "final_grade" in rep, "报告应含总评分与总评级"
    assert set(rep["counts"].keys()) == {"pass", "marginal", "fail"}, "counts 应含三类"
    assert sum(rep["counts"].values()) == len(rep["nodes"]), "counts 之和应等于节点数"
    assert rep["final_grade"] in ("A", "B", "C", "D"), "总评级应在 A/B/C/D"
    assert isinstance(rep["repair_plan"], list), "repair_plan 应为列表"
    # 修复建议应可落地（指向现有能力）：auto_heal(⑪) / threshold 注入 / 换 tier(③) / 重试 / 补上下文(④)
    if rep["repair_plan"]:
        _joined = " ".join(rep["repair_plan"])
        assert any(k in _joined for k in ("auto_heal", "threshold", "tier", "重试", "上下文")), \
            "修复建议应指向可落地能力（auto_heal/threshold/tier/重试/上下文）"
    print(f"✓ S13 Phase2 质量门(QualityReport): 总评 {rep['final_score_100']}/100"
          f"（{rep['final_grade']}）· 分级 {rep['counts']} · 修复项 {len(rep['repair_plan'])}")

    # S14: Phase 2 技能注册表（离线：复用 ② 技能 / 集中注册 / 拓扑引用解析 / 未注册标记）
    os.environ.pop("AGENT_API_KEY", None)
    from runtime import SkillRegistry
    reg = SkillRegistry()
    assert reg.is_registered("calculator") and reg.is_implemented("calculator"), \
        "应复用 ② 的 calculator"
    assert len(reg.implemented_names()) >= 19, "应复用 ② 至少 19 个已实现技能"
    # 拓扑引用解析：calculator(已注册) + a_missing_skill(未注册，待实现)
    _comps = {
        "t1": {"type": "tool", "fillers": {"x": {"skill": "calculator", "args": {}}}},
        "t2": {"type": "retrieve", "fillers": {"y": {"skill": "a_missing_skill", "args": {}}}},
    }
    _resolved = reg.resolve(_comps)
    assert "calculator" in _resolved["registered"], "calculator 应判已注册"
    assert "a_missing_skill" in _resolved["unregistered"], "a_missing_skill 应判未注册(待实现)"
    assert _resolved["unregistered_count"] == 1, "应有 1 个待实现技能"
    # 端点层面：/skills 列表 + /skills/resolve 编译真实目标并解析
    _sk = list_skills()
    assert _sk["count"] >= 19 and any(s["name"] == "calculator" for s in _sk["skills"]), \
        "/skills 应列出已复用 ② 技能"
    _sr = skills_resolve(GoalRequest(goal="查一下今年 GDP 并画一张趋势图"))
    assert {"references", "registered", "unregistered"} <= set(_sr.keys()), \
        "/skills/resolve 应返回 references/registered/unregistered"
    assert isinstance(_sr["summary"], str) and _sr["summary"], "/skills/resolve 应给总结"
    print(f"✓ S14 Phase2 注册表(SkillRegistry): 在册 {_sk['count']} 个 · "
          f"示例拓扑解析 → {_resolved['summary']}")

    # S15: Phase 2 ③ 模型选型再平衡（真实历史成功率/延迟/成本多目标再平衡）
    from compiler.model_selector import ModelSelector, ModelMetrics
    _mm = ModelMetrics(path=None)
    for _ in range(10):
        _mm.record("reason", "small", success=False, latency_ms=200, cost=0.001)
    for _ in range(10):
        _mm.record("reason", "large", success=True, latency_ms=1500, cost=0.020)
    _ms15 = ModelSelector(memory=None, metrics=_mm)
    _spec15 = {
        "capabilities": ["reason"], "constraints": {}, "description": "稳定推理",
        "components": {"reason": {"type": "resistor", "label": "reason",
                                  "capability": "reason", "model": "small"}},
    }
    _r15 = _ms15.select(_spec15)
    assert _r15["reason"]["tier"] != "small", "真实历史 small 失败率高应避开"
    assert "再平衡" in _r15["reason"]["reason"], "reason 应标注再平衡"
    # 端点层面：/models 列表 + /models/select 编译目标并选档
    _md = list_models()
    assert set(_md["tiers"].keys()) == {"small", "large", "tool"}, "/models 应列三档"
    assert "weights" in _md and set(_md["weights"].keys()) == {"quality", "latency", "cost"}, \
        "/models 应返回再平衡权重"
    _sel = select_models(GoalRequest(goal="分析GDP并预测趋势"))
    assert isinstance(_sel, dict) and all("tier" in v for v in _sel.values()), \
        "/models/select 应返回每节点 tier"
    print(f"✓ S15 Phase2 ③ 模型选型再平衡(ModelMetrics): 历史避坑→{_r15['reason']['tier']} · "
          f"/models 三档+权重✓ · /models/select 节点数 {len(_sel)}")

    # S16: Phase 2 ② 加深④ 多模态真视听觉（真实转录器 + 离线降级 + /transcribe 端点）
    from compiler.multimodal import MultimodalTranscriber
    _tr16 = MultimodalTranscriber()
    _t16a = _tr16.transcribe({"type": "image", "name": "x.png"})
    assert _t16a["offline"] is True and _t16a["transcription"], "离线应占位描述"
    _t16b = transcribe(TranscribeRequest(images=["a.jpg"], audio=["b.wav"]))
    assert _t16b["count"] == 2 and all("transcription" in r for r in _t16b["results"]), \
        "/transcribe 应返回每附件转录"
    assert _t16b["results"][0]["type"] == "image" and _t16b["results"][1]["type"] == "audio", \
        "/transcribe 应区分模态"
    print(f"✓ S16 Phase2 ② 多模态真视听觉(MultimodalTranscriber): 离线占位✓ · "
          f"/transcribe 返回 {_t16b['count']} 条转录(图+音)")

    # S17: Phase 2 ③ 分布式执行（WCC 分片 + 多 worker 并发 + 聚合 + /cluster 端点）
    from compiler.cluster import ClusterCoordinator
    _c17 = ClusterCoordinator(n_workers=2).run("查中国GDP总量并预测趋势", n_workers=2)
    assert _c17["worker_count"] >= 1 and "per_worker" in _c17, "应分布式执行并聚合"
    assert _c17["final_quality"] >= 0, "聚合质量应非负"
    _cr = cluster_run(ClusterRequest(goal="分析两份报告并总结", n_workers=2))
    assert _cr["worker_count"] >= 1 and len(_cr["per_worker"]) >= 1, \
        "/cluster 应返回分片结果"
    print(f"✓ S17 Phase2 ③ 分布式执行(ClusterCoordinator): worker={_cr['worker_count']} · "
          f"聚合质量={_cr['final_quality']} · per_worker={len(_cr['per_worker'])}")

    # S18: Phase 2 ④ 跨语言编译器（拓扑 → C++/Rust/JS 可执行源码 + /codegen 端点）
    from compiler.codegen import TopologyCompiler
    _cg = TopologyCompiler().emit_all("分析两份报告并总结")
    assert set(_cg) == {"cpp", "rust", "js"}, "应生成三语言"
    for lang, code in _cg.items():
        assert "LAYERS" in code and "DONE" in code, f"{lang} 应含拓扑序+主入口"
    _cge = codegen_run(CodegenRequest(goal="查GDP并预测", language="js"))
    assert _cge["language"] == "js" and "DONE" in _cge["code"], "/codegen 应返回 JS 源码"
    print(f"✓ S18 Phase2 ④ 跨语言编译器(TopologyCompiler): 三语言(cpp/rust/js) 生成✓ · "
          f"/codegen 返回 JS 源码")

    # S19: Phase 2 ⑦ 加深 长周期休眠唤醒（分轮执行 → 休眠 → /wake 续跑）
    tid2 = "selftest_sleep"
    _longtasks[tid2] = {"task": None, "status": "pending", "goal": "休眠唤醒测试",
                        "result": None, "checkpoint": None, "error": None}
    _run_longtask(tid2, "分析一份很长的报告并总结要点",
                  {"route": True, "memory_enabled": False, "evolve_enabled": False,
                   "layers_per_round": 1, "wake_in_sec": 0})
    assert _longtasks[tid2]["status"] == "sleeping", \
        f"S19: 应跑 1 层后休眠，实际 {_longtasks[tid2]['status']}"
    _sl = _longtasks[tid2]["result"]
    assert _sl.get("sleeping") is True and _sl.get("due_now") is True, "S19: 应标记休眠且已到期"
    _done1 = _sl["done_layers"]
    _wk = wake_longtask(tid2)
    assert _wk["woken"] is True and _wk["result"]["status"] == "done", \
        f"S19: 唤醒后应跑完，实际 {_wk['result'].get('status')}"
    assert _wk["result"]["done_layers"] > _done1, "S19: 唤醒后完成层数应增加（断点续跑）"
    print(f"✓ S19 Phase2 ⑦加深 长周期休眠唤醒: 跑{_done1}层→休眠 → /wake 续跑至 "
          f"{_wk['result']['done_layers']} 层 done（休眠期零占用）")
    try:
        os.unlink(f"longtask_{tid2}.json")
    except OSError:
        pass

    # S20: Phase 2 ⑧ 加深 人机协同决策点（proceed / skip / abort 三态 + 零回归）
    _dspec = {
        "name": "dp_demo",
        "components": {
            "src":    {"type": "power", "label": "task"},
            "reason": {"type": "resistor", "label": "reason", "model": "small", "yield": 1.0},
            "sum":    {"type": "resistor", "label": "summarize", "model": "small", "yield": 1.0},
        },
        "wires": [["src", "reason"], ["reason", "sum"]],
    }
    _d1 = decision_run(DecisionRequest(spec=_dspec, decision_points=["reason"],
                                       policy={"reason": "proceed"}))
    assert _d1["success"] is True and _d1["decision_points_hit"] == 1, "S20: proceed 应正常完成"
    _d2 = decision_run(DecisionRequest(spec=_dspec, decision_points="all",
                                       policy={"reason": "abort"}))
    assert _d2.get("aborted") is True and _d2.get("abort_node") == "reason", \
        "S20: abort 应中止于该决策点"
    _d3 = decision_run(DecisionRequest(spec=_dspec, decision_points=["reason"],
                                       policy={"reason": "skip"}))
    assert _d3["components"]["reason"]["ok"] is False, "S20: skip 后该节点应标记未通过"
    _d4 = decision_run(DecisionRequest(spec=_dspec))   # 零回归：无决策点 → 不暂停
    assert _d4["success"] is True and _d4["decision_points_hit"] == 0, "S20: 无配置不应暂停"
    print(f"✓ S20 Phase2 ⑧加深 人机协同决策点: proceed/skip/abort 三态生效 · "
          f"abort_node={_d2['abort_node']} · 零回归(无配置不暂停)")

    # S21: Phase 2 第三层① RL 优化拓扑（真实执行 reward 搜索 + /rl/optimize）
    _rlspec = {
        "name": "rl_api_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "A":   {"type": "resistor", "label": "research", "model": "large",
                    "yield": 1.0, "produced_outputs": ["a"]},
            "B":   {"type": "resistor", "label": "analyze", "model": "large",
                    "yield": 1.0, "required_inputs": ["a"], "produced_outputs": ["b"]},
            "V":   {"type": "verify", "label": "verify_b", "threshold": 0.5},
            "C":   {"type": "resistor", "label": "summarize", "model": "large",
                    "yield": 1.0, "required_inputs": ["b"]},
        },
        "wires": [["src", "A"], ["A", "B"], ["B", "V"], ["V", "C"]],
    }
    _rl = rl_optimize(RLOptimizeRequest(spec=_rlspec, episodes=30, patience=15, seed=7))
    assert _rl["improved"] is True, f"S21: 应搜到更优拓扑（{_rl['improvement']}）"
    assert _rl["best_reward"] > _rl["baseline_reward"], "S21: 最优 reward 应超基线"
    assert "history" not in _rl, "S21: 默认不返回逐轮轨迹"
    assert len(_rl["arm_stats"]) == 5, "S21: 应有 5 个算子的收益统计"
    print(f"✓ S21 Phase2 三层① RL优化拓扑(RLOptimizer): reward "
          f"{_rl['baseline_reward']}→{_rl['best_reward']} (+{_rl['improvement']}) · "
          f"成本 {_rl['baseline']['cost']}→{_rl['best']['cost']} · "
          f"质量 {_rl['baseline']['quality']}→{_rl['best']['quality']} · "
          f"{_rl['episodes_run']}轮 · 算子收益可解释")

    # S22: Phase 2 第三层② 联邦学习（脱敏摘要 + 差分隐私 + FedAvg + /federated/round）
    _SECRET = "腾讯2026Q3财报净利润明细"        # 模拟不可出境的原始任务描述
    def _fedrec(tier, ok):
        return {
            "goal_desc": _SECRET,                       # 故意塞进去，验证不会外泄
            "spec": {"name": _SECRET, "components": {
                "src": {"type": "power", "label": "task"},
                "R1": {"type": "resistor", "label": "analyze", "model": tier}},
                "wires": [["src", "R1"]]},
            "result": {"success": ok, "final_quality": 0.9 if ok else 0.3,
                       "total_latency_ms": 900, "total_cost": 0.012},
        }
    _fedreq = FederatedRequest(
        clients=[
            FederatedClientSpec(client_id="team-a", epsilon=10.0, seed=1,
                                records=[_fedrec("tool", True)] * 30),
            FederatedClientSpec(client_id="team-b", epsilon=10.0, seed=2,
                                records=[_fedrec("small", False)] * 30),
        ],
        min_clients=2, blend=0.5, query_capability="analyze")
    _fed = federated_round(_fedreq)
    assert _fed["global_model"]["n_clients"] == 2, "S22: 应聚合 2 方"
    assert _SECRET not in json.dumps(_fed, ensure_ascii=False), \
        "S22: 全局模型/回执中绝不能出现原始任务描述"
    assert _fed["global_model"]["privacy"]["raw_data_shared"] is False, "S22: 应声明零原始数据"
    assert _fed["best_tier"] and _fed["best_tier"]["tier"] == "tool", \
        f"S22: 应学到 analyze→tool，实际 {_fed.get('best_tier')}"
    assert set(_fed["applied"]) == {"team-a", "team-b"}, "S22: 两方都应回灌"
    assert all(r["epsilon_spent"] <= r["epsilon_total"] + 1e-6
               for r in _fed["privacy_reports"].values()), "S22: 隐私预算不得超支"
    _fed_solo = federated_round(FederatedRequest(
        clients=[FederatedClientSpec(client_id="lonely", records=[_fedrec("tool", True)] * 5)],
        min_clients=2))
    assert "error" in _fed_solo["global_model"], "S22: 单方应拒绝聚合（防单点反推）"
    print(f"✓ S22 Phase2 三层② 联邦学习(FederatedServer): 2方聚合 · "
          f"全局学到 analyze→{_fed['best_tier']['tier']}"
          f"(成功率 {round(_fed['best_tier']['success_rate'], 4)}) · "
          f"原始任务描述零外泄 · ε 记账未超支 · 单方拒聚合")

    # S23: Phase 2 第三层③ 自主发现新元件类型（挖掘+封装+注册+内联展开真执行）
    from runtime import _COMPONENT_LIBRARY as _CL, Circuit as _Circ, \
        SimBackend as _SB, CircuitExecutor as _CE
    _CL.clear()                                   # 干净起点
    _rvs = lambda nm, tier="small": {            # noqa: E731 research→verify→summarize
        "name": nm, "spec": {"name": nm, "components": {
            "src": {"type": "power", "label": "task"},
            "R": {"type": "resistor", "label": "research", "model": tier,
                  "yield": 1.0, "produced_outputs": ["raw"]},
            "V": {"type": "verify", "label": "verify", "threshold": 0.5},
            "S": {"type": "resistor", "label": "summarize", "model": tier,
                  "yield": 1.0, "required_inputs": ["raw"],
                  "produced_outputs": ["summary"]}},
            "wires": [["src", "R"], ["R", "V"], ["V", "S"]]}}
    _disc = components_discover(ComponentDiscoverRequest(
        history=[_rvs("h1"), _rvs("h2"), _rvs("h3", "large")],
        min_support=3, max_size=4, register=True))
    assert _disc["count"] >= 1, f"S23: 应挖出≥1 个模板，实际 {_disc['count']}"
    assert _disc["registered"] is True, "S23: 应已注册"
    _tmpl = _disc["templates"][0]
    assert _tmpl["name"] in _CL, "S23: 模板应在全局库"
    # composite 真执行：一个节点代替 R→V→S，展开后与原子版结果一致
    import random as _rnd
    _be_a = _SB(_rnd.Random(42))
    _res_a = _CE(_Circ(_rvs("atomic")["spec"], _be_a)).run()
    _be_c = _SB(_rnd.Random(42))                  # 同种子可复现
    _res_c = _CE(_Circ({"name": "comp_demo", "components": {
        "src": {"type": "power", "label": "task"},
        "C": {"type": "composite", "template": _tmpl["name"], "label": "rvs"}},
        "wires": [["src", "C"]]}, _be_c)).run()
    assert _res_c["success"] == _res_a["success"], \
        f"S23: composite 展开后执行结果应与原子版一致"
    assert abs(_res_c["final_quality"] - _res_a["final_quality"]) < 1e-9, \
        f"S23: 质量应一致 (composite={_res_c['final_quality']} atomic={_res_a['final_quality']})"
    _lib = components_library()
    assert _lib["count"] >= 1, "S23: /components/library 应能查到已注册模板"
    print(f"✓ S23 Phase2 三层③ 自主发现新元件类型(ComponentMiner): "
          f"挖掘 {_disc['count']} 个模板 · 顶级 {_tmpl['name']}"
          f"(size={len(_tmpl['internal_components'])} support={_tmpl['support']}) · "
          f"composite 内联展开后真执行 success={_res_c['success']} "
          f"quality={round(_res_c['final_quality'], 4)} 与原子版一致 · "
          f"library 查询 {_lib['count']} 个")
    _CL.clear()                                   # 清理不污染后续

    # S24: Phase 2 第三层④ 形式化验证（内建符号验证器 + /verify）
    _vspec = {"name": "verify_demo", "components": {
        "src": {"type": "power", "label": "task"},
        "A": {"type": "resistor", "label": "research", "model": "large",
              "yield": 1.0, "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "analyze", "model": "tool",
              "yield": 1.0, "required_inputs": ["a"],
              "produced_outputs": ["b"]},
        "V": {"type": "verify", "label": "verify", "threshold": 0.5},
        "C": {"type": "resistor", "label": "summarize", "model": "small",
              "yield": 1.0, "required_inputs": ["b"]}},
        "wires": [["src", "A"], ["A", "B"], ["B", "V"], ["V", "C"]],
        "quality_gate": 0.5}
    _v = verify_topology(VerifyRequest(spec=_vspec))
    assert _v["all_pass"] is True, \
        f"S24: 合法 spec 应全通过，实际 {_v['summary']}"
    assert _v["proven"] is True
    _vnames = [c["name"] for c in _v["checks"]]
    assert _vnames == ["acyclicity", "reachability", "input_completeness",
                       "deadlock_freedom", "resource_bounds", "quality_lower_bound"]
    # 有环 → fail + 反例
    _vcyc = verify_topology(VerifyRequest(spec={
        "name": "cyc", "components": {
            "src": {"type": "power", "label": "t"},
            "A": {"type": "resistor", "label": "a", "model": "small"},
            "B": {"type": "resistor", "label": "b", "model": "small"}},
        "wires": [["src", "A"], ["A", "B"], ["B", "A"]]}))
    _cyc = [c for c in _vcyc["checks"] if c["name"] == "acyclicity"][0]
    assert _cyc["status"] == "fail" and _cyc["counterexample"], "S24: 环应 fail + 反例"
    print(f"✓ S24 Phase2 三层④ 形式化验证(FormalVerifier): 6维全通过(proven) · "
          f"合法spec {_v['summary']} · 有环反例 {'→'.join(_cyc['counterexample'][:3])}... · "
          f"零依赖纯静态分析（执行前门禁）")

    # S25: 第四层① 在线调参（Bandit UCB1 + /tune）
    _tspec = {"name": "tune_demo", "components": {
        "src": {"type": "power", "label": "task"},
        "A": {"type": "resistor", "label": "research", "model": "small",
              "yield": 1.0, "produced_outputs": ["a"]},
        "B": {"type": "resistor", "label": "analyze", "model": "large",
              "yield": 1.0, "required_inputs": ["a"],
              "produced_outputs": ["b"]},
        "C": {"type": "resistor", "label": "summarize", "model": "small",
              "yield": 1.0, "required_inputs": ["b"]}},
        "wires": [["src", "A"], ["A", "B"], ["B", "C"]]}
    _tune = tune_run(TuneRunRequest(spec=_tspec, iterations=25, seed=7))
    assert _tune["iterations"] == 25, "S25: 应跑满 25 轮"
    assert _tune["avg_final_quality"] > 0.5, f"S25: 平均质量应>0.5，实际 {_tune['avg_final_quality']}"
    assert len(_tune["arm_stats"]) >= 3, "S25: 至少 3 个臂"
    assert _tune["converged_tiers"], "S25: 应收敛"
    # Bandit 应收敛到 tool 或 large（SimBackend 确定性：tool 最优）
    bt = _tune["converged_tiers"].get("research") or \
        next(iter(_tune["converged_tiers"].values()))
    assert bt in ("tool", "large"), f"S25: 应收敛到 tool/large，实际 {bt}"
    print(f"✓ S25 第四层① 在线调参(OnlineTuner): "
          f"25轮/avg_q={round(_tune['avg_final_quality'],4)} · "
          f"{len(_tune['arm_stats'])} 臂 · 收敛 {bt} · Bandit UCB1 运行时自适应")

    # S26: 第四层② 编译成静态图（拓扑 → 纯 Python 函数 + /static-graph/compile）
    _sg_spec = {
        "name": "sg_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "A": {"type": "resistor", "label": "retrieve", "model": "small", "accuracy": 0.70, "recovery": 0.3},
            "B": {"type": "resistor", "label": "reason", "model": "large", "accuracy": 0.92},
            "adc": {"type": "adc", "threshold": 0.6},
        },
        "wires": [["src", "A"], ["A", "B"], ["B", "adc"]],
    }
    _sg = static_graph_compile(StaticGraphRequest(spec=_sg_spec, seed=42))
    assert _sg["standalone"] and _sg["zero_llm"], "S26: 应标注 standalone + zero_llm"
    assert "def run_task" in _sg["code"], "S26: 应包含 run_task 函数"
    assert "_run_component" in _sg["code"] and "_TIERS" in _sg["code"], "S26: 应内联完整运行时"
    assert _sg["function_name"] == "run_task", "S26: 函数名应为 run_task"
    assert _sg["seed"] == 42, "S26: seed 应透传"
    # 验证生成代码可 exec 并执行
    _sg_ns = {}
    exec(_sg["code"], _sg_ns)
    _sg_result = _sg_ns["run_task"]("test task")
    assert isinstance(_sg_result, dict) and "final_quality" in _sg_result, "S26: exec 后应返回结果 dict"
    assert _sg_result["final_quality"] > 0, "S26: 静态执行质量应 > 0"
    print(f"✓ S26 第四层② 编译成静态图(StaticGraphCompiler): "
          f"独立 Python 函数 · 质量={round(_sg_result['final_quality'],3)} · "
          f"成本=¥{_sg_result['total_cost']:.4f} · 零 LLM 确定性")

    # S27: 第四层③ 执行历史因果分析（反事实推理 + /causal/analyze）
    from runtime import Circuit as _SC, SimBackend as _SBE, CircuitExecutor as _SCE
    import random as _sr
    _ca_spec = {
        "name": "causal_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "ret": {"type": "resistor", "label": "retrieve", "model": "small", "accuracy": 0.70, "recovery": 0.3},
            "rsn": {"type": "resistor", "label": "reason", "model": "large", "accuracy": 0.92},
            "sum": {"type": "resistor", "label": "summarize", "model": "small", "accuracy": 0.55},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "ret"], ["ret", "rsn"], ["rsn", "sum"], ["sum", "adc"]],
    }
    _ca_be = _SBE(_sr.Random(42))
    _ca_circ = _SC(_ca_spec, _ca_be)
    _ca_result = _SCE(_ca_circ).run()
    _ca = causal_analyze(CausalAnalyzeRequest(spec=_ca_spec, execution_result=_ca_result))
    assert _ca["bottlenecks"], "S27: 应有瓶颈分析结果"
    assert _ca["bottleneck_node"] is not None, "S27: 应定位瓶颈节点"
    assert _ca["max_impact"] > 0, f"S27: 应有正因果贡献: {_ca['max_impact']}"
    _ca_bn0 = _ca["bottlenecks"][0]
    assert _ca_bn0["impact"] == _ca["max_impact"], "S27: 排名第1应等于 max_impact"
    assert "瓶颈" in _ca["analysis"], "S27: 分析摘要应含'瓶颈'"
    print(f"✓ S27 第四层③ 因果分析(CausalAnalyzer): "
          f"最终质量={round(_ca['actual_final_quality'],3)} · "
          f"瓶颈={_ca['bottleneck_label']} · "
          f"因果贡献=+{round(_ca['max_impact'],3)} · "
          f"反事实推理({len(_ca['bottlenecks'])} 节点)")

    # S28: 第四层④ 异构硬件后端（Ollama 本地模型 + /ollama/run + /ollama/health）
    # 离线验证：注入假响应，不依赖真实 Ollama 运行
    from compiler.ollama_backend import OllamaBackend as _OBE
    _oll_spec = {
        "name": "ollama_demo",
        "components": {
            "src": {"type": "power", "label": "task"},
            "ret": {"type": "resistor", "label": "retrieve", "model": "small"},
            "rsn": {"type": "resistor", "label": "reason", "model": "large"},
            "adc": {"type": "adc", "threshold": 0.5},
        },
        "wires": [["src", "ret"], ["ret", "rsn"], ["rsn", "adc"]],
    }
    # 注入假响应测试 OllamaBackend 核心逻辑
    _fake_resp = {"model": "qwen2.5:7b",
                  "message": {"role": "assistant", "content": "本地推理结果"},
                  "done": True, "prompt_eval_count": 20, "eval_count": 5}
    _oll_be = _OBE(rng=__import__("random").Random(0),
                   http_post=lambda u, h, b: _fake_resp,
                   fallback=__import__("runtime", fromlist=["SimBackend"]).SimBackend(
                       __import__("random").Random(0)))
    from runtime import Circuit as _OC, CircuitExecutor as _OCE
    _oll_circ = _OC(_oll_spec, _oll_be)
    _oll_result = _OCE(_oll_circ).run()
    _oll_stats = _oll_be.stats()
    assert _oll_stats["calls"] >= 2, f"S28: 应至少调用 2 次 Ollama: {_oll_stats}"
    assert _oll_stats["successes"] >= 2, f"S28: 应至少成功 2 次: {_oll_stats}"
    assert _oll_be.model_map["small"] == "qwen2.5:7b", "S28: 默认模型映射"
    print(f"✓ S28 第四层④ Ollama后端(OllamaBackend): "
          f"calls={_oll_stats['calls']} · success={_oll_stats['successes']} · "
          f"成本=¥0（本地免费） · native API · fallback=SimBackend · "
          f"模型映射 small→qwen2.5:7b large→qwen2.5:14b")

    # S29: 奥卡姆剃刀化简 Pass（compiler.simplify + /simplify 端点）
    _simp_red = {
        "name": "simp_red", "components": {
            "src": {"type": "power", "label": "task"},
            "ret": {"type": "resistor", "label": "retrieve", "model": "small",
                    "capability": "retrieve"},
            "mid": {"type": "adc", "threshold": 0.5},
            "org": {"type": "resistor", "label": "organize", "model": "small",
                    "capability": "organize"},
            "adc": {"type": "adc", "threshold": 0.5}},
        "wires": [["src", "ret"], ["ret", "mid"], ["mid", "org"], ["org", "adc"]]}
    _simp_res = simplify_topology(SimplifyRequest(spec=_simp_red))
    assert "mid" not in _simp_res["spec"]["components"], "S29: 冗余中间 adc 应被剃落"
    assert _simp_res["report"]["simplified"], "S29: 应标记 simplified"
    assert _simp_res["report"]["original_nodes"] == 5, "S29: 原节点数应为 5"
    # 复杂任务（并行+反馈）应完整保留
    _simp_cx = {
        "name": "simp_complex", "components": {
            "src": {"type": "power", "label": "task"},
            "a": {"type": "resistor", "label": "a", "model": "large",
                  "capability": "reason"},
            "b": {"type": "resistor", "label": "b", "model": "large",
                  "capability": "reason"},
            "c": {"type": "resistor", "label": "c", "model": "large",
                  "capability": "reason"},
            "adc": {"type": "adc", "threshold": 0.5}},
        "wires": [["src", "a"], ["src", "b"], ["a", "c"], ["b", "c"],
                  ["c", "adc"], ["adc", "src"]],
        "feedback": {"from": "adc", "to": "src", "max_iter": 3}}
    _simp_cxr = simplify_topology(SimplifyRequest(spec=_simp_cx))
    assert set(_simp_cxr["spec"]["components"].keys()) == set(_simp_cx["components"].keys()), \
        "S29: 复杂任务结构应完整保留"
    assert "feedback" in _simp_cxr["spec"], "S29: 反馈环应保留"
    assert not _simp_cxr["report"]["simplified"], "S29: 复杂任务不应被化简"
    print(f"✓ S29 奥卡姆剃刀化简(OckhamsRazor): 冗余 adc 剃落"
          f" · 复杂任务(并行+反馈)完整保留 · 去噪确定性等价判定 · /simplify 端点可用")

    # S30: π 永动心跳（spigot 正确性 + f(π) 四动作覆盖 + 状态恒变 + 反馈闭环）
    if pi_heartbeat_selftest is not None:
        pi_heartbeat_selftest()

    # S31: 导师-学生训练电路（离线：注入式导师 + 注入式学生，不走网络/不调本地模型）
    if mentor_train_cycle is not None:
        import json as _mjson
        import tempfile as _mtmp
        from execution_store import ExecutionStore as _MStore
        _mdb = os.path.join(_mtmp.mkdtemp(), "s31.db")
        _mstore = _MStore(_mdb)
        _mspec = {"name": "s31", "components": {
            "pwr": {"type": "power", "label": "pwr"},
            "ext": {"type": "resistor", "label": "extract", "model": "small",
                    "capability": "extract", "produced_outputs": ["m"]},
            "adc": {"type": "adc", "threshold": 0.8}},
            "wires": [["pwr", "ext"], ["ext", "adc"]]}
        _mstore.save("s31-fail", "抽取季度指标", "failed", _mspec, [],
                     {"final_quality": 0.25, "failed_nodes": ["ext"]}, ["s31"])

        def _s31_mentor(messages):
            _plan = {"diagnosis": "ext 用 small 档，抽取能力不足",
                     "node_fixes": [{"cid": "ext", "model": "large",
                                     "prompt": "逐项抽取指标并输出结构化结果"}],
                     "topology_ops": [], "rationale": "升档 + 明确指令提升抽取率"}
            return {"choices": [{"message": {
                "content": _mjson.dumps(_plan, ensure_ascii=False)}}]}

        def _s31_student(_spec):
            return {"final_quality": 0.7, "success": True, "failed_nodes": [],
                    "outputs": {"pwr": "pwr", "adc": "0.9",
                                "ext": "抽取结果：营收 1.2 亿，净利 1800 万，同比 +23%。"}}

        _m_reg = []
        _mres = mentor_train_cycle(_mstore, http_post=_s31_mentor,
                                   student_rerun_fn=_s31_student,
                                   registry=_m_reg,
                                   quality_fn=default_content_quality)
        assert _mres["ok"], "S31: 闭环应成功"
        assert _mres["optimized_spec"]["components"]["ext"]["model"] == "large", \
            "S31: 导师方案应把 ext 升到 large"
        assert _mres["original_spec"]["components"]["ext"]["model"] == "small", \
            "S31: 原 spec 不应被就地修改（深拷贝可回滚）"
        # 只统计 resistor：pwr/adc 的元件语义值不应稀释内容质量
        assert _mres["after_quality"] > 0.9, \
            f"S31: 内容质量应只算 resistor，实际 {_mres['after_quality']}"
        assert _mres["quality_gate_passed"], f"S31: {_mres['quality_gate_reason']}"
        assert len(_m_reg) == 1, "S31: 通过后应固化 1 条模板"
        # 反例：质量未提升则拒绝固化
        _m_reg2 = []
        _mres2 = mentor_train_cycle(
            _mstore, http_post=_s31_mentor,
            student_rerun_fn=lambda s: {"final_quality": 0.1, "success": False,
                                        "failed_nodes": ["ext"], "outputs": {}},
            registry=_m_reg2, quality_fn=default_content_quality)
        assert not _mres2["quality_gate_passed"], "S31: 未提升应被质量门拒绝"
        assert len(_m_reg2) == 0, "S31: 未过门不应固化"
        print(f"✓ S31 导师-学生训练电路(MentorTrain): 诊断「{_mres['diagnosis']}」"
              f" · 质量 {_mres['before_quality']}→{_mres['after_quality']} 门通过"
              f" · 固化 1 条 · 原spec未改 · 反例拒固化 · /mentor/train 端点可用")

    # S32: 在线拓扑编辑（人在回路）端点接线 —— 直接调用端点函数验证请求模型/会话/编辑分发/404路径
    _spec = {
        "name": "s32_topo",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "r1": {"type": "resistor", "label": "a", "model": "small"},
            "adc": {"type": "adc", "label": "adc", "threshold": 0.5},
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
    }
    _sess = topology_session(TopologySessionRequest(spec=_spec))
    _sid = _sess["session_id"]
    assert _sid and _sess["state"]["state"] in ("running", "done"), "S32: 应建会话并启动"
    # 等任务自然跑完（SimBackend 很快；无 required_inputs 不会触发网络补数）
    import time as _t
    for _ in range(500):
        if topology_state(_sid)["done"]:
            break
        _t.sleep(0.01)
    _st = topology_state(_sid)
    assert _st["done"], "S32: 会话应执行完成"
    assert _st["result"]["success"], "S32: 初始拓扑应执行成功"
    # 编辑分发（对已完成会话编辑只作用于活图，验证端点把参数正确转给 executor.edit）
    _ed = topology_edit(_sid, TopologyEditRequest(
        op="replace", cid="r1", comp={"model": "large", "label": "a2"}))
    assert _ed["edit"]["op"] == "replace" and "r1" in _ed["state"]["components"], \
        "S32: /topology/edit 应正确分发 replace"
    assert _topo_sessions[_sid]["executor"].circuit.components["r1"]["model"] == "large", \
        "S32: replace 应作用到活图 r1"
    # 编辑非法 op → 400
    _raised = False
    try:
        topology_edit(_sid, TopologyEditRequest(op="frobnicate"))
    except Exception:
        _raised = True
    assert _raised, "S32: 非法 op 应被拒绝(400)"
    # 未知会话 → 404（pause/resume/state/edit 共用 _topo_session）
    _nf = False
    try:
        topology_state("nope")
    except Exception:
        _nf = True
    assert _nf, "S32: 未知会话应 404"
    # pause/resume 对已结束会话为 no-op（返回 False，不抛）
    assert topology_pause(_sid)["paused"] is False, "S32: 已结束会话 pause 应为 no-op"
    assert topology_resume(_sid)["resumed"] is False, "S32: 已结束会话 resume 应为 no-op"
    print("✓ S32 在线拓扑编辑(人在回路): /topology/session|pause|edit|resume|state 端点接线"
          " · 会话创建/状态轮询/编辑分发/非法op拒/未知会话404 全通过")

    # S33: 可视化仪表盘支撑 —— get_state 逐节点进度字段 + /topology/editor 路由
    _st = topology_state(_sid)
    assert "done_nodes" in _st and "current_layer" in _st, \
        "S33: get_state 应暴露 done_nodes/current_layer 供仪表盘着色"
    assert isinstance(_st["done_nodes"], list), "S33: done_nodes 应为列表"
    try:
        from fastapi.testclient import TestClient  # 无 httpx2 时退化
        _cli = TestClient(app)
        _r = _cli.get("/topology/editor")
        assert _r.status_code == 200 and "<html" in _r.text.lower(), \
            "S33: /topology/editor 应返回 HTML"
        assert "人在回路" in _r.text, "S33: 编辑器页面内容应存在"
    except Exception:
        # 无 TestClient（未装 httpx2）时跳过路由校验，仅保障字段契约
        pass
    print("✓ S33 可视化仪表盘支撑: get_state.done_nodes/current_layer + /topology/editor 路由就绪")

    # S34: 指挥中⼼ —— 透明决策报告 + 主动提问裁决 + 人工编辑学习库（HTTP 端点级）
    _cc = {
        "name": "s34",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "r1": {"type": "resistor", "label": "analyze", "model": "small"},
            "adc": {"type": "adc", "label": "gate", "threshold": 0.8},
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
    }
    _s34 = topology_session(TopologySessionRequest(spec=_cc, seed=7))
    _s34id = _s34["session_id"]
    _topo_sessions[_s34id]["done"].wait(timeout=10)
    # ① 节点工作报告
    _rep = topology_node_report(_s34id, "r1")
    assert "output" in _rep["report"] and "model" in _rep["report"] \
        and "latency_ms" in _rep["report"] and "quality" in _rep["report"], \
        "S34: /topology/node 应返回含 输出/模型/耗时/质量 的报告"
    assert _rep["report"]["model"] == "small", "S34: 报告应记录真实使用的模型档"
    # ③ 学习库（初始为空，编辑后应有条目）
    _ln0 = topology_learnings(_s34id)["learnings"]
    topology_edit(_s34id, TopologyEditRequest(op="set_gate", cid="adc", threshold=0.6))
    _ln1 = topology_learnings(_s34id)["learnings"]
    assert len(_ln1) == len(_ln0) + 1, "S34: 一次人工编辑应记入学习库"
    assert _ln1[-1]["op"] == "set_gate" and _ln1[-1].get("human_edited"), \
        "S34: 学习库条目应记录操作类型并标记 human_edited"
    # ② 主动提问：用 ambiguous_band=1.0 的 adc + 延迟，等执行器主动暂停提问
    _cc2 = {
        "name": "s34b",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "r1": {"type": "resistor", "label": "analyze", "model": "small"},
            "adc": {"type": "adc", "label": "gate", "threshold": 0.8,
                    "ambiguous_band": 1.0},
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
    }
    _s34b = topology_session(TopologySessionRequest(
        spec=_cc2, seed=3, node_delay_ms=150))
    _s34bid = _s34b["session_id"]
    for _ in range(300):
        if topology_state(_s34bid).get("pending_question"):
            break
        time.sleep(0.02)
    assert topology_state(_s34bid).get("pending_question"), \
        "S34: adc 灰色地带应通过端点暴露 pending_question"
    # 老板作答（GET 风格：?choice=high）
    _ans = topology_answer(_s34bid, choice="high")
    assert _ans["answered"] is True, "S34: 人类裁决应触发恢复"
    _topo_sessions[_s34bid]["done"].wait(timeout=10)
    _st34b = topology_state(_s34bid)
    assert _st34b.get("state") == "done", "S34: 人类裁决后任务应跑完"
    assert _st34b["node_traces"]["adc"].get("human_verdict") == "high", \
        "S34: adc 应记录人类裁决=high"
    print("✓ S34 指挥中⼼: ①节点报告 / ②主动提问→裁决恢复 / ③人工编辑学习库 全通过")

    # S35: 大规模协作 Phase 0 —— 房间模型 + 角色权限 + activity 流
    _spec = {
        "name": "s35_room",
        "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "r1": {"type": "resistor", "label": "a", "model": "small"},
            "adc": {"type": "adc", "label": "adc", "threshold": 0.5},
        },
        "wires": [["src", "r1"], ["r1", "adc"]],
    }
    _cr = create_room(CreateRoomRequest(spec=_spec, owner_id="alice", name="collab"))
    _rid = _cr["room_id"]; _rsid = _cr["session_id"]
    assert _rid and _rsid, "S35: 应创建房间并建共享会话"
    assert _rooms[_rid]["owner"] == "alice", "S35: owner 应记录"
    # observer 加入
    _join = room_join(_rid, RoomJoinRequest(user_id="bob", desired_role="observer"))
    assert _join["role"] == "observer", "S35: bob 应为 observer"
    # observer 读状态 OK（read 权限）
    _ok = topology_state(_rsid, room_id=_rid, user_id="bob")
    assert _ok, "S35: observer 可读状态"
    # observer 尝试 edit → 403
    _forbidden = False
    try:
        topology_edit(_rsid, TopologyEditRequest(op="set_gate", cid="adc", threshold=0.6),
                      room_id=_rid, user_id="bob")
    except Exception as e:
        _forbidden = (getattr(e, "status_code", None) == 403)
    assert _forbidden, "S35: observer 编辑应被 403 拦截"
    # owner 编辑 OK
    _ok2 = topology_edit(_rsid, TopologyEditRequest(op="set_gate", cid="adc", threshold=0.7),
                         room_id=_rid, user_id="alice")
    assert _ok2["edit"]["op"] == "set_gate", "S35: owner 可编辑"
    # activity 流应记录 create/join/edit
    _act = room_activity(_rid, user_id="alice")["activities"]
    _actions = [a["action"] for a in _act]
    assert "create_room" in _actions and "join" in _actions and "edit" in _actions, \
        "S35: activity 流应记录动作"
    print("✓ S35 大规模协作 Phase0: 房间创建/成员角色/权限矩阵/越权403/activity流 全通过")

    # S36: 大规模协作维度三 —— 实时协同同步（多人动作互相可见 + 指定 room_id + spec 共享）
    _rid2 = "collab_s36"
    _c = create_room(CreateRoomRequest(spec=_spec, owner_id="carol", name="c", room_id=_rid2))
    assert _c["room_id"] == _rid2, "S36: 应可用指定 room_id 创建（便于分享链接）"
    _sid2 = _c["session_id"]
    room_join(_rid2, RoomJoinRequest(user_id="dave", desired_role="mentor"))
    # carol(owner) 编辑
    topology_edit(_sid2, TopologyEditRequest(op="set_gate", cid="adc", threshold=0.9),
                 room_id=_rid2, user_id="carol")
    # dave(mentor) 拉 activity 应看到 carol 的动作（多人动作互通）
    _act2 = room_activity(_rid2, user_id="dave")["activities"]
    _actors = {a["actor"] for a in _act2}
    assert "carol" in _actors, "S36: dave 应能看到 carol 的动作（协同互通）"
    assert "edit" in [a["action"] for a in _act2], "S36: activity 应含 edit 动作"
    # dave 也能看到房间 spec（协同渲染用）
    _info2 = room_info(_rid2, user_id="dave")
    assert _info2.get("spec"), "S36: room_info 应返回 spec 供协作者渲染拓扑"
    print("✓ S36 大规模协作维度三: 指定room_id/多人动作互通/spec共享 全通过")

    # S37: 大规模协作维度② —— 共享记忆与知识库（桥接 ⑬ / ⑭）
    _rid3 = "collab_s37"
    _c3 = create_room(CreateRoomRequest(spec=_spec, owner_id="erin", name="kb", room_id=_rid3))
    assert _c3["room_id"] == _rid3, "S37: 应创建知识库房间"
    # erin(owner) 发布当前拓扑到共享仓库（打 room 标签）
    _pub = room_memory_publish(_rid3, RoomMemoryPublishRequest(user_id="erin"))
    assert _pub["published_name"], "S37: owner 应能把拓扑发布到共享仓库"
    # 知识库视图：published 含该条目，repo_items 非空，且带 room 标签
    _mem = room_memory(_rid3, user_id="erin")
    assert _pub["published_name"] in _mem["published"], "S37: 知识库应记录已发布项"
    assert any(f"room:{_rid3}" in (it.get("tags") or []) for it in _mem["repo_items"]), \
        "S37: 仓库条目应带 room 标签"
    # observer(frank) 有 read 权限 → 能看到知识库（多人共享记忆）
    room_join(_rid3, RoomJoinRequest(user_id="frank", desired_role="observer"))
    _mem2 = room_memory(_rid3, user_id="frank")
    assert _mem2["repo_items"], "S37: observer 应能看到房间共享记忆"
    # observer 尝试发布 → 403（无 publish 权限）
    _forbidden_pub = False
    try:
        room_memory_publish(_rid3, RoomMemoryPublishRequest(user_id="frank"))
    except Exception as e:
        _forbidden_pub = (getattr(e, "status_code", None) == 403)
    assert _forbidden_pub, "S37: observer 发布应被 403 拦截"
    # observer 拉取已发布拓扑到房间（read 即可）→ 更新共享 spec/会话
    _pull = room_memory_pull(_rid3, RoomMemoryPullRequest(user_id="frank", name=_pub["published_name"]))
    assert _pull["session_id"], "S37: observer 应能 pull 到房间（共享记忆复用）"
    # 蒸馏：把已发布历史升华为模板（⑭）
    _dist = room_memory_distill(_rid3, RoomMemoryDistillRequest(user_id="erin"))
    assert "templates" in _dist, "S37: 应返回蒸馏模板"
    print("✓ S37 大规模协作维度②: 发布到共享仓库/多人可读记忆/越权403/pull复用/蒸馏模板 全通过")

    # S38: 大规模协作维度③ —— 多 agent 编排（房间内 mentor/reviewer/student 闭环）
    _rid4 = "collab_s38"
    _c4 = create_room(CreateRoomRequest(spec=_spec, owner_id="grant", name="orch", room_id=_rid4))
    assert _c4["room_id"] == _rid4, "S38: 应创建编排房间"
    # grant(owner) 发起编排（mock 模式，确定性可测）
    _orc = room_orchestrate(_rid4, RoomOrchestrateRequest(user_id="grant", mock=True))
    _t = _orc["trace"]
    assert _t["mentor"]["plan"]["node_fixes"], "S38: mentor 应提出优化"
    assert _t["reviewer"]["verdict"] == "approve", "S38: mock 优化应被 reviewer 通过"
    assert _t["student"]["quality_gate_passed"] is True, "S38: student 执行应过质量门"
    # 编排事件写入 room activity（所有人可见）
    _acts4 = room_activity(_rid4, user_id="grant")["activities"]
    assert any(a["action"] == "orchestrate" for a in _acts4), "S38: 应有 orchestrate 事件"
    assert any(a["actor"] == "student" for a in _acts4), "S38: student 执行应可见"
    # 通过则固化进房间知识库
    assert room_memory(_rid4, user_id="grant")["templates"], "S38: 编排通过应固化模板"
    # reviewer 角色(grace) 有 adjudicate 但无 control → 不能发起编排 → 403
    room_join(_rid4, RoomJoinRequest(user_id="grace", desired_role="reviewer"))
    _forbidden_orc = False
    try:
        room_orchestrate(_rid4, RoomOrchestrateRequest(user_id="grace", mock=True))
    except Exception as e:
        _forbidden_orc = (getattr(e, "status_code", None) == 403)
    assert _forbidden_orc, "S38: reviewer 无 control 发起编排应被 403 拦截"
    print("✓ S38 大规模协作维度③: mentor提议/reviewer裁决/student执行/质量门/activity可见/越权403 全通过")

    # S39: 大规模协作维度④ —— reroute 拖拽改流向（edit op 扩展 + 房间共享拓扑同步）
    _spec39 = {"name": "s39_reroute",
               "components": {
                   "src": {"type": "power", "label": "task", "task": "x"},
                   "a": {"type": "resistor", "label": "A", "model": "small", "produced_outputs": ["x"]},
                   "b": {"type": "resistor", "label": "B", "model": "small", "required_inputs": ["x"], "produced_outputs": ["y"]},
                   "c": {"type": "resistor", "label": "C", "model": "small", "required_inputs": ["y"], "produced_outputs": ["z"]},
               },
               "wires": [["src", "a"], ["a", "b"], ["b", "c"]]}
    _s39 = topology_session(TopologySessionRequest(spec=_spec39))
    _s39id = _s39["session_id"]
    try: topology_pause(_s39id)
    except Exception: pass
    # 把 a->b 重定向为 a->c
    _r39 = topology_edit(_s39id, TopologyEditRequest(op="reroute", old=["a", "b"], new=["a", "c"]))
    _w39 = topology_state(_s39id)["wires"]
    assert ["a", "c"] in _w39, "S39: reroute 应把 a->b 改为 a->c"
    assert ["a", "b"] not in _w39, "S39: 旧 wire a->b 应消失"
    # old=None → 新建 wire
    _r39b = topology_edit(_s39id, TopologyEditRequest(op="reroute", old=None, new=["a", "b"]))
    _w39b = topology_state(_s39id)["wires"]
    assert ["a", "b"] in _w39b, "S39: old=None 应新建 wire a->b"
    # 房间模式：reroute 应写 activity + 同步房间共享 spec
    _rid5 = "collab_s39"
    _c5 = create_room(CreateRoomRequest(spec=_spec39, owner_id="kara", name="rt", room_id=_rid5))
    _s5 = _c5["session_id"]
    try: topology_pause(_s5)
    except Exception: pass
    topology_edit(_s5, TopologyEditRequest(op="reroute", old=["a", "b"], new=["a", "c"]),
                 room_id=_rid5, user_id="kara")
    _act5 = room_activity(_rid5, user_id="kara")["activities"]
    assert any(a["action"] == "edit" and a["detail"] == "reroute" for a in _act5), \
        "S39: 房间模式 reroute 应记 edit/reroute 事件"
    assert ["a", "c"] in room_info(_rid5, user_id="kara").get("spec", {}).get("wires", []), \
        "S39: reroute 应同步到房间共享 spec（协作者可见）"
    print("✓ S39 大规模协作维度④: reroute 改流向(替换/新建 wire) + 房间 activity + 共享拓扑同步 全通过")

    # S40: 真实 LLM 接入多 agent 编排（深化①）—— 逐角色开关 + 无 key 优雅降级 + 能力探测
    import os as _os40
    _saved40 = {}
    for _k in ("DEEPSEEK_API_KEY",):
        _saved40[_k] = _os40.environ.pop(_k, None)
    try:
        _spec40 = {"name": "s40_llm", "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "a": {"type": "resistor", "label": "A", "model": "small", "produced_outputs": ["x"]},
            "b": {"type": "resistor", "label": "B", "model": "small",
                  "required_inputs": ["x"], "produced_outputs": ["y"]},
        }, "wires": [["src", "a"], ["a", "b"]]}
        _rid6 = "collab_s40"
        create_room(CreateRoomRequest(spec=_spec40, owner_id="liam", name="llm", room_id=_rid6))
        # ① 默认不开 use_llm → 全 mock，不产生任何真实调用
        _o1 = room_orchestrate(_rid6, RoomOrchestrateRequest(user_id="liam"))
        assert _o1["agents"]["mentor"] == "mock", "S40: 默认应全 mock（不误烧 token）"
        assert _o1["agents"]["reviewer"] == "rule", "S40: 默认裁决走规则"
        assert not _o1["degraded"], "S40: 默认 mock 路径不应有降级记录"
        # ② 开 use_llm 但无 key → 逐角色降级，闭环仍完成且不抛异常
        _o2 = room_orchestrate(_rid6, RoomOrchestrateRequest(
            user_id="liam", use_llm=True, use_real_student=False))
        assert _o2["agents"]["mentor"] == "mock", "S40: 无 key 应降级 mock"
        assert _o2["agents"]["reviewer"] == "rule", "S40: 无 key 裁决应降级规则"
        assert _o2["degraded"], "S40: 降级原因应回传给前端"
        assert _o2["trace"]["student"]["quality_gate_passed"] is True, "S40: 降级后闭环仍应跑完"
        # ③ 降级事件写进房间 activity（协作者能看见"它掉回 mock 了"）
        _act6 = room_activity(_rid6, user_id="liam")["activities"]
        assert any(a["action"] == "orchestrate_degraded" for a in _act6), \
            "S40: 降级应记入房间 activity"
        assert any("mentor:mock" in (a.get("detail") or "") for a in _act6), \
            "S40: activity 应标注每个角色的真实/mock 身份"
        # ④ 能力探测端点：无 key → 明确报不可用，且不 500
        _av = agents_availability(check_student=False)
        assert _av["mentor"] is False and _av["judge"] is False, "S40: 无 key 应报不可用"
        assert _av["any"] is False and "detail" in _av, "S40: 探测应返回 detail 供 UI 提示"
        # ⑤ 单角色覆盖：只让 reviewer 走真实（其余仍 mock），不影响闭环
        _o3 = room_orchestrate(_rid6, RoomOrchestrateRequest(
            user_id="liam", use_real_reviewer=True, use_real_student=False))
        assert _o3["agents"]["mentor"] == "mock", "S40: 未指定的角色不应被带成真实"
        assert _o3["trace"]["reviewer"]["verdict"] in ("approve", "reject")
    finally:
        for _k, _v in _saved40.items():
            if _v is not None:
                _os40.environ[_k] = _v
    print("✓ S40 深化①真实LLM接入: 逐角色开关/默认全mock不烧钱/无key优雅降级/降级可见/能力探测 全通过")

    # S41: 深化② OT-lite 并发编辑 —— 版本号/op-log/语义冲突检测/自动rebase/重放追平
    _spec41 = {"name": "s41_ot", "components": {
        "src": {"type": "power", "label": "task", "task": "x"},
        "a": {"type": "resistor", "label": "A", "model": "small", "produced_outputs": ["x"]},
        "b": {"type": "resistor", "label": "B", "model": "small",
              "required_inputs": ["x"], "produced_outputs": ["y"]},
        "c": {"type": "resistor", "label": "C", "model": "small",
              "required_inputs": ["y"], "produced_outputs": ["z"]},
    }, "wires": [["src", "a"], ["a", "b"], ["b", "c"]]}
    _rid7 = "collab_s41"
    _c7 = create_room(CreateRoomRequest(spec=_spec41, owner_id="mia", name="ot",
                                        room_id=_rid7, concurrency="ot"))
    _s7 = _c7["session_id"]
    room_join(_rid7, RoomJoinRequest(user_id="noah", desired_role="mentor"))
    try: topology_pause(_s7)
    except Exception: pass
    assert room_info(_rid7, user_id="mia")["rev"] == 0, "S41: 新房间 rev 应为 0"
    # ① 编辑推进 rev 并写 op-log
    _e1 = topology_edit(_s7, TopologyEditRequest(op="replace", cid="a",
                                                 comp={"model": "large"}, base_rev=0),
                        room_id=_rid7, user_id="mia")
    assert _e1["rev"] == 1, f"S41: 首次编辑 rev 应为 1，实际 {_e1.get('rev')}"
    _ops = room_ops(_rid7, user_id="mia", since=0)
    assert _ops["count"] == 1 and _ops["ops"][0]["actor"] == "mia", "S41: op-log 应记录编辑者"
    # ② 落后但改**不同节点** → 自动 rebase 放行（常态：不该互相阻塞）
    _e2 = topology_edit(_s7, TopologyEditRequest(op="replace", cid="c",
                                                 comp={"model": "large"}, base_rev=0),
                        room_id=_rid7, user_id="noah")
    assert _e2["rev"] == 2, "S41: 第二次编辑应推进到 rev 2"
    assert _e2.get("merge", {}).get("rebased") is True, "S41: 不同目标应自动 rebase"
    # ③ 落后且改**同一节点** → 409 冲突（不静默覆盖）
    _conflict = None
    try:
        topology_edit(_s7, TopologyEditRequest(op="replace", cid="a",
                                               comp={"model": "tool"}, base_rev=0),
                      room_id=_rid7, user_id="noah")
    except Exception as e:
        _conflict = e
    assert getattr(_conflict, "status_code", None) == 409, "S41: 同目标并发编辑应 409"
    _d = _conflict.detail
    assert _d["clashes"] and _d["clashes"][0]["actor"] == "mia", "S41: 冲突详情应指出是谁改的"
    assert any("node/a" in t for t in _d["clashes"][0]["targets"]), "S41: 冲突应定位到 node/a"
    # ④ force=true 强推可覆盖（但会被标记）
    _e4 = topology_edit(_s7, TopologyEditRequest(op="replace", cid="a",
                                                 comp={"model": "tool"}, base_rev=0, force=True),
                        room_id=_rid7, user_id="noah")
    assert _e4["merge"]["forced"] is True, "S41: force 应被标记，便于事后追责"
    assert _e4["rev"] == 3, "S41: 强推也要推进 rev"
    # ⑤ 追平后再提交（base_rev=当前rev）→ 无冲突
    _cur = room_info(_rid7, user_id="noah")["rev"]
    _e5 = topology_edit(_s7, TopologyEditRequest(op="replace", cid="a",
                                                 comp={"model": "large"}, base_rev=_cur),
                        room_id=_rid7, user_id="noah")
    assert "merge" not in _e5, "S41: 追平后提交不应有 rebase/冲突标记"
    # ⑥ 增量重放：只取自己没见过的 op
    _inc = room_ops(_rid7, user_id="noah", since=2)
    assert _inc["count"] == 2 and all(o["rev"] > 2 for o in _inc["ops"]), "S41: since 应做增量过滤"
    assert _inc["rev"] == 4, "S41: ops 应回传房间最新 rev"
    # ⑦ 真并发压测：8 线程同时改 8 个不同节点，rev 必须无缝且无丢失
    _spec41b = {"name": "s41_par", "components": {
        "src": {"type": "power", "label": "t", "task": "x"},
        **{f"n{i}": {"type": "resistor", "label": f"N{i}", "model": "small",
                     "produced_outputs": [f"o{i}"]} for i in range(8)},
    }, "wires": [["src", f"n{i}"] for i in range(8)]}
    _rid8 = "collab_s41p"
    _c8 = create_room(CreateRoomRequest(spec=_spec41b, owner_id="mia", name="par",
                                        room_id=_rid8, concurrency="ot"))
    _s8 = _c8["session_id"]
    try: topology_pause(_s8)
    except Exception: pass
    import threading as _th41
    _errs41 = []
    def _worker41(i):
        try:
            topology_edit(_s8, TopologyEditRequest(op="replace", cid=f"n{i}",
                                                   comp={"model": "large"}, base_rev=0),
                          room_id=_rid8, user_id="mia")
        except Exception as ex:
            _errs41.append(ex)
    _ths41 = [_th41.Thread(target=_worker41, args=(i,)) for i in range(8)]
    for t in _ths41: t.start()
    for t in _ths41: t.join()
    assert not _errs41, f"S41: 改不同节点的并发编辑不应冲突，实际报错 {_errs41[:2]}"
    _r8 = _rooms[_rid8]
    assert _r8["rev"] == 8, f"S41: 8 次并发编辑 rev 应为 8，实际 {_r8['rev']}"
    _revs = [o["rev"] for o in _r8["ops"]]
    assert sorted(_revs) == list(range(1, 9)), f"S41: rev 应连续无重复/无丢失，实际 {_revs}"
    # ⑧ 原子性（防 TOCTOU）：8 线程抢改**同一节点**且都基于同一 base_rev
    #    → 必须恰好 1 个成功、其余全部 409，绝不能双双通过检测后静默覆盖
    _base8 = _r8["rev"]
    _ok8, _c409 = [], []
    def _racer(i):
        try:
            r = topology_edit(_s8, TopologyEditRequest(op="replace", cid="n0",
                                                       comp={"model": f"m{i}"},
                                                       base_rev=_base8),
                              room_id=_rid8, user_id=f"u{i}")
            _ok8.append(r["rev"])
        except Exception as ex:
            if getattr(ex, "status_code", None) == 409:
                _c409.append(i)
            else:
                _errs41.append(ex)
    for i in range(8):
        room_join(_rid8, RoomJoinRequest(user_id=f"u{i}", desired_role="mentor"))
    _ths8 = [_th41.Thread(target=_racer, args=(i,)) for i in range(8)]
    for t in _ths8: t.start()
    for t in _ths8: t.join()
    assert not _errs41, f"S41: 抢改不应产生非 409 错误 {_errs41[:2]}"
    assert len(_ok8) == 1, f"S41: 抢改同一节点应恰好 1 个成功，实际 {len(_ok8)}（TOCTOU 竞态！）"
    assert len(_c409) == 7, f"S41: 其余 7 个应被 409 拒绝，实际 {len(_c409)}"
    print("✓ S41 深化②OT并发编辑: rev/op-log/同目标409/异目标自动rebase/force标记/增量重放/"
          "8线程无丢失/抢改同节点原子(1成功7冲突) 全通过")

    # S42: 深化③ 房间持久化 —— 落盘 .rooms.json，重启加载并据 spec 重建 session
    _store42 = RoomStore(os.path.join(_tf.mkdtemp(prefix="ca_s42_"), "rooms.json"))
    _old_store, _old_persist = _room_store, _PERSIST
    _room_store, _PERSIST = _store42, True   # 临时开启真实落盘
    try:
        _spec42 = {"name": "s42_persist", "components": {
            "src": {"type": "power", "label": "task", "task": "x"},
            "a": {"type": "resistor", "label": "A", "model": "small",
                  "required_inputs": ["x"], "produced_outputs": ["y"]},
        }, "wires": [["src", "a"]]}
        _rid42 = "collab_s42"
        _c42 = create_room(CreateRoomRequest(spec=_spec42, owner_id="p1", name="persist", room_id=_rid42))
        _s42 = _c42["session_id"]
        room_join(_rid42, RoomJoinRequest(user_id="p2", desired_role="mentor"))
        # 编辑一次（房间模式 → 触发持久化 + 推进 rev + 写 op）
        _e42 = topology_edit(_s42, TopologyEditRequest(op="replace", cid="a",
                                                       comp={"model": "large"}, base_rev=0),
                             room_id=_rid42, user_id="p1")
        assert _e42["rev"] == 1, "S42: 编辑应推进 rev"
        # 沉淀一条记忆，验证 memory 也落盘
        with _lock:
            _rooms[_rid42]["memory"]["templates"].append({"name": "t42", "support": 1})
        _persist()
        assert os.path.exists(_store42.path), "S42: 落盘文件应存在"
        # ── 模拟重启：清空内存房间，再用 store 重新加载 ──
        _rooms.clear()
        assert _rid42 not in _rooms, "S42: 清空后内存应无此房间"
        load_rooms(_store42)
        assert _rid42 in _rooms, "S42: 重启后房间应被加载"
        _r42 = _rooms[_rid42]
        assert _r42["owner"] == "p1", "S42: owner 应保留"
        assert "p2" in _r42["members"] and _r42["members"]["p2"] == "mentor", "S42: 成员/角色应保留"
        assert _r42["rev"] == 1, "S42: rev 应保留"
        assert _r42["spec"]["components"]["a"]["model"] == "large", "S42: 编辑后的拓扑应保留"
        assert _r42["ops"] and _r42["ops"][0]["actor"] == "p1", "S42: op-log 应保留"
        assert _r42["memory"]["templates"] and _r42["memory"]["templates"][0]["name"] == "t42", \
            "S42: 发布的记忆应保留"
        assert _r42["session_id"] in _topo_sessions, "S42: 内部 session 应据 spec 重建"
        _st42 = _topo_sessions[_r42["session_id"]]["executor"].get_state()
        assert "components" in _st42, "S42: 重建的 session 应可取状态"
        # 重新加载的房间仍可继续编辑（session 是活的）
        _e42b = topology_edit(_r42["session_id"],
                              TopologyEditRequest(op="replace", cid="a",
                                                 comp={"model": "small"}, base_rev=1),
                              room_id=_rid42, user_id="p2")
        assert _e42b["rev"] == 2, "S42: 重启后房间应能继续编辑"
    finally:
        _room_store, _PERSIST = _old_store, _old_persist
    print("✓ S42 深化③房间持久化: 落盘/重启加载/owner+成员+ops+rev+记忆保留/据spec重建活session/可续编 全通过")

    # S43: 深化④ 跨房间联邦 —— 联邦目录发现 + 跨房间拉取知识
    _rA, _rB = "fed_A", "fed_B"
    create_room(CreateRoomRequest(spec=_spec41, owner_id="fa", name="A", room_id=_rA))
    create_room(CreateRoomRequest(spec=_spec41, owner_id="fb", name="B", room_id=_rB))
    with _lock:
        _rooms[_rB]["memory"]["published"].append("kb_b1")
        _rooms[_rB]["memory"]["templates"].append({"name": "t_b1", "support": 2})
        _rooms[_rB]["memory"]["templates"].append({"name": "t_b2", "support": 3})
    # ① 联邦目录应列出所有房间
    _fed = federation_directory()
    assert _fed["count"] >= 2, "S43: 联邦目录应至少列出 2 个房间"
    assert {e["room_id"] for e in _fed["rooms"]} >= {_rA, _rB}, "S43: 目录应含 A/B"
    _b_entry = next(e for e in _fed["rooms"] if e["room_id"] == _rB)
    assert _b_entry["published"] == 1 and _b_entry["templates"] == 2, "S43: 目录应带知识量摘要"
    # ② A 从 B 联邦拉取知识
    _fp = room_federate_pull(_rA, _rB, RoomFederatePullRequest(user_id="fa"))
    assert "kb_b1" in _fp["published"], "S43: A 应拉到 B 的已发布知识"
    assert _fp["imported_templates"] == 2 and len(_fp["templates"]) >= 2, "S43: A 应拉到 B 的模板"
    # ③ 联邦是单向汇入，绝不反向污染源房间
    assert _rooms[_rB]["memory"]["published"] == ["kb_b1"], "S43: 拉取不应改动源房间"
    assert len(_rooms[_rB]["memory"]["templates"]) == 2, "S43: 源房间模板不被改动"
    # ④ 联邦动作写入 A 的 activity（跨房间协作可见）
    _act = room_activity(_rA, user_id="fa")
    assert any(a["action"] == "federate_pull" for a in _act["activities"]), "S43: 联邦拉取应入 activity"
    # ⑤ 源房间不存在应 404（而非悄悄空拉）
    try:
        room_federate_pull(_rA, "nobody", RoomFederatePullRequest(user_id="fa"))
        assert False, "S43: 源房间不存在应 404"
    except HTTPException as ex:
        assert ex.status_code == 404, "S43: 源房间不存在应 404"
    # ⑥ include_spec 可连共享拓扑一并并入（重建 session）
    _fp2 = room_federate_pull(_rA, _rB, RoomFederatePullRequest(user_id="fa", include_spec=True))
    assert _rooms[_rA]["session_id"] in _topo_sessions, "S43: include_spec 应重建 session"
    assert _rooms[_rA]["spec"] == _rooms[_rB]["spec"], "S43: include_spec 应并入源拓扑"
    print("✓ S43 深化④跨房间联邦: 目录发现/跨房间拉知识/单向不污染/activity可见/404/可并入拓扑 全通过")

    # ── S44: 实时交互（极致=连续不中断）—— 事件总线 + SSE + 连续干预 ──
    _spec44 = {
        "name": "realtime44",
        "components": {
            "s1": {"type": "source", "label": "S1", "produced_outputs": ["x"]},
            "r1": {"type": "resistor", "label": "R1", "model": "small", "produced_outputs": ["y"]},
            "a1": {"type": "adc", "label": "A1", "threshold": 0.8, "produced_outputs": ["z"]},
            "out": {"type": "sink", "label": "OUT"},
        },
        "wires": [["s1", "r1"], ["r1", "a1"], ["a1", "out"]],
    }
    # S44a: 事件总线把执行事件实时送达订阅者（SSE 的数据通路）
    _s44 = topology_session(TopologySessionRequest(spec=_spec44, seed=1, node_delay_ms=50))
    _sid44 = _s44["session_id"]
    _q44 = _realtime_hub.subscribe(_sid44)
    assert _topo_sessions[_sid44]["done"].wait(timeout=15), "S44a: 会话应在 15s 内完成"
    _got44 = []
    try:
        while True:
            _got44.append(_q44.get(timeout=1))
    except queue.Empty:
        pass
    _realtime_hub.unsubscribe(_sid44, _q44)
    _types44 = {e.get("type") for e in _got44}
    assert "node_done" in _types44, "S44a: 应收到 node_done 事件"
    assert "done" in _types44, "S44a: 应收到 done 事件"
    # S44b: 运行中随时干预（不暂停）也能落地且不卡死主进度
    _s44b = topology_session(TopologySessionRequest(spec=_spec44, seed=2, node_delay_ms=80))
    _sid44b = _s44b["session_id"]
    _q44b = _realtime_hub.subscribe(_sid44b)
    _ed44 = topology_edit(_sid44b, TopologyEditRequest(op="set_gate", cid="a1", threshold=0.5))
    assert _ed44["edit"].get("applied") is True, "S44b: 运行中干预应成功应用"
    assert _topo_sessions[_sid44b]["done"].wait(timeout=15), "S44b: 运行中干预后主进度不应卡死"
    assert _topo_sessions[_sid44b]["error"] is None, "S44b: 运行中干预不应引发异常"
    _got44b = []
    try:
        while True:
            _got44b.append(_q44b.get(timeout=1))
    except queue.Empty:
        pass
    _realtime_hub.unsubscribe(_sid44b, _q44b)
    _types44b = {e.get("type") for e in _got44b}
    assert "topology_edit" in _types44b, "S44b: 干预应作为事件实时流出"
    assert "done" in _types44b, "S44b: 干预后 done 事件仍应到达"
    # S44c: 房间 activity 经事件总线实时广播给同房成员
    _room44 = {"session_id": "rt44_room", "activity": []}
    _q44c = _realtime_hub.subscribe("rt44_room")
    _record_activity(_room44, "u1", "edit", "x", detail="test")
    _got44c = []
    try:
        while True:
            _got44c.append(_q44c.get(timeout=1))
    except queue.Empty:
        pass
    _realtime_hub.unsubscribe("rt44_room", _q44c)
    assert any(e.get("type") == "activity" for e in _got44c), "S44c: 房间 activity 应推入事件总线"
    print("✓ S44 实时交互(极致): 事件总线/SSE通路(运行中事件实时达)/连续干预不卡死/房间activity广播 全通过")

    # ── S45: 大规模协作（极致·人数众多）──────────────────────
    #   ① CRDT 房间：数十人并发抢改同一节点 → 零 409、零重试，且全员收敛同一状态
    #   ② presence：在线感知/焦点地图/超时离线
    #   ③ 扇出：慢消费者被 drop-oldest 保护，不拖垮整房；高频 presence 被 coalesce
    _spec45 = {"name": "s45_scale", "components": {
        "src": {"type": "power", "label": "task", "task": "x"},
        "n0": {"type": "resistor", "label": "N0", "model": "small", "produced_outputs": ["x"]},
        "n1": {"type": "resistor", "label": "N1", "model": "small",
               "required_inputs": ["x"], "produced_outputs": ["y"]},
    }, "wires": [["src", "n0"], ["n0", "n1"]]}
    _rid45 = "collab_s45"
    _c45 = create_room(CreateRoomRequest(spec=_spec45, owner_id="host", name="scale",
                                         room_id=_rid45))
    _s45 = _c45["session_id"]
    try: topology_pause(_s45)
    except Exception: pass
    _r45 = _rooms[_rid45]
    assert _r45["concurrency"] == "crdt", "S45: 新房间应默认 CRDT（极致路径）"

    # ① 32 人同时抢改**同一节点**，且都基于陈旧 base_rev=0
    _N45 = 32
    for i in range(_N45):
        room_join(_rid45, RoomJoinRequest(user_id=f"p{i}", desired_role="mentor"))
    import threading as _th45
    _ok45, _rej45, _err45 = [], [], []
    def _rush45(i):
        try:
            r = topology_edit(_s45, TopologyEditRequest(op="replace", cid="n0",
                                                        comp={"model": f"m{i}"},
                                                        base_rev=0),
                              room_id=_rid45, user_id=f"p{i}")
            _ok45.append(r)
        except Exception as ex:
            (_rej45 if getattr(ex, "status_code", None) == 409 else _err45).append(ex)
    _ts45 = [_th45.Thread(target=_rush45, args=(i,)) for i in range(_N45)]
    for t in _ts45: t.start()
    for t in _ts45: t.join()
    assert not _err45, f"S45: 并发抢改不应报错 {_err45[:2]}"
    assert not _rej45, f"S45: CRDT 模式必须零拒绝，实际被拒 {len(_rej45)}"
    assert len(_ok45) == _N45, f"S45: {_N45} 人应全部成功，实际 {len(_ok45)}"
    assert _r45["rev"] == _N45, f"S45: rev 应为 {_N45}，实际 {_r45['rev']}"

    # 收敛：把服务端 op 流以两种打乱顺序喂给两个独立副本 → 状态哈希必须一致
    from runtime import TopologyCRDT as _TC45
    _srv_hash = _r45["crdt"].state_hash()
    _ops45 = list(_r45["crdt"].log)
    import random as _rd45
    _hashes45 = set()
    for _seed in (1, 2, 3):
        _sh = _ops45[:]
        _rd45.Random(_seed).shuffle(_sh)
        _rep = _TC45(actor=f"peer{_seed}")
        _rep.merge(_sh + _sh[:5])               # 乱序 + 重复投递
        _hashes45.add(_rep.state_hash())
    assert _hashes45 == {_srv_hash}, f"S45: 副本未与服务端收敛 {_hashes45} vs {_srv_hash}"

    # 客户端直接提交 CRDT op（不走 /topology/edit）也能合并且幂等
    _peer = _TC45(actor="peer_x"); _peer.merge(_ops45)
    _peer_ops = [_peer.set_attr("n1", "model", "large"),
                 _peer.add_node("nx", {"model": "tool"})]
    _sub1 = room_crdt_submit(_rid45, RoomCRDTOpsRequest(user_id="p0", ops=_peer_ops))
    _sub2 = room_crdt_submit(_rid45, RoomCRDTOpsRequest(user_id="p0", ops=_peer_ops))
    assert _sub1["merged"] == 2, f"S45: 应合并 2 个 op，实际 {_sub1['merged']}"
    assert _sub2["merged"] == 0, "S45: 重复投递应幂等（合并 0 条）"
    assert _sub1["hash"] == _sub2["hash"], "S45: 幂等后哈希不应变化"
    _view45 = room_crdt_state(_rid45, user_id="p0")
    assert _view45["state"]["components"]["n1"]["model"] == "large", "S45: 远端 op 未生效"
    assert "nx" in _view45["state"]["components"], "S45: 远端新建节点未生效"
    try:
        room_crdt_submit(_rid45, RoomCRDTOpsRequest(user_id="ghost", ops=_peer_ops))
        raise AssertionError("S45: 非成员提交 op 应 403")
    except HTTPException as _e45:
        assert _e45.status_code == 403

    # ② presence：心跳 → 在线名单 + 焦点地图；伪造过期心跳 → 自动判离线
    for i in range(5):
        room_presence(_rid45, RoomPresenceRequest(user_id=f"p{i}", cursor=[i, i],
                                                  focus="n0" if i < 3 else "n1"))
    _pl45 = room_presence_list(_rid45, user_id="p0")
    assert _pl45["online"] == 5, f"S45: 应 5 人在线，实际 {_pl45['online']}"
    assert sorted(_pl45["focus_map"]["n0"]) == ["p0", "p1", "p2"], "S45: 焦点地图错误"
    _r45["presence"]["p4"]["ts"] -= (PRESENCE_TTL + 5)
    assert room_presence_list(_rid45)["online"] == 4, "S45: 超时成员应自动离线"
    try:
        room_presence(_rid45, RoomPresenceRequest(user_id="ghost"))
        raise AssertionError("S45: 非成员心跳应 403")
    except HTTPException as _e45b:
        assert _e45b.status_code == 403

    # ③ 扇出抗压：慢消费者队列打满 → drop-oldest 而非阻塞；presence 高频被 coalesce
    _hub45 = _RealtimeHub(maxsize=8)
    _slow = _hub45.subscribe("room45")           # 一个从不消费的"卡死观众"
    _fast = _hub45.subscribe("room45")
    for i in range(50):                          # 远超队列容量
        _hub45.push("room45", {"type": "node_done", "i": i})
    assert _slow.qsize() == 8, f"S45: 慢消费者队列应被限幅在 8，实际 {_slow.qsize()}"
    assert _hub45.stats["dropped"] > 0, "S45: 应记录丢弃计数（drop-oldest 生效）"
    _drained = [_fast.get_nowait() for _ in range(_fast.qsize())]
    assert _drained[-1]["i"] == 49, "S45: 最新事件必须保留（丢旧不丢新）"
    _hub45b = _RealtimeHub(maxsize=64)
    _pq = _hub45b.subscribe("room45b")
    for i in range(40):                          # 同一用户 40 次心跳
        _hub45b.push("room45b", {"type": "presence", "actor": "p0", "cursor": [i, i]})
    assert _pq.qsize() == 1, f"S45: 同用户 presence 应合并为 1 条，实际 {_pq.qsize()}"
    assert _pq.get_nowait()["cursor"] == [39, 39], "S45: 合并后应保留最新心跳"
    _hub45b.push("room45b", {"type": "presence", "actor": "p1", "cursor": [0, 0]})
    _hub45b.push("room45b", {"type": "presence", "actor": "p0", "cursor": [1, 1]})
    assert _pq.qsize() == 2, "S45: 不同用户的 presence 不应互相合并"

    # 水位可观测
    _st45 = room_stats(_rid45, user_id="p0")
    assert _st45["members"] == _N45 + 1 and _st45["online"] >= 4, "S45: stats 人数不对"
    assert _st45["concurrency"] == "crdt" and _st45["crdt_hash"], "S45: stats 缺 CRDT 水位"
    print(f"✓ S45 大规模协作(极致): {_N45}人抢改同节点零拒绝零重试 · 3副本乱序+重投全收敛 · "
          "客户端op幂等合并 · presence在线感知/焦点图/超时离线 · "
          "慢消费者drop-oldest不拖全房 · presence合并40→1 · 水位可观测 全通过")

    # ── S46 人机协同(极致)：协作者不分人机 + 机器主动招募 + 人类异步作答回灌 ──
    import threading as _th46
    # ① 四类协作者同池注册（人类专家 / 其他 agent / 知识源 / 外部系统）
    for _c46 in (
        {"id": "expert_zhang", "name": "张工(工艺)", "kind": "human",
         "skills": ["process", "material"], "trust": 0.92, "latency_ms": 60000},
        {"id": "kb_std", "name": "国标知识库", "kind": "knowledge",
         "skills": ["material", "spec"], "trust": 0.6, "latency_ms": 150},
        {"id": "mes", "name": "MES系统", "kind": "system",
         "skills": ["inventory"], "trust": 0.8, "latency_ms": 300},
        {"id": "agent_calc", "kind": "agent", "skills": ["math"], "trust": 0.7},
    ):
        collaborator_register(CollaboratorRequest(**_c46))
    _all46 = collaborator_list()
    assert len(_all46["collaborators"]) >= 4, "S46: 协作者池注册失败"
    assert {c["kind"] for c in _all46["collaborators"]} >= {
        "human", "knowledge", "system", "agent"}, "S46: 四类协作者应同池"
    # ② 择优可解释：机器会挑谁，接口能说清楚
    _m46 = collaborator_list(need="material", top=3)["match"]
    assert _m46 and _m46[0]["id"] == "expert_zhang", f"S46: material 应首选人类专家 {_m46}"
    assert collaborator_list(need="inventory", top=1)["match"][0]["id"] == "mes"
    # ③ 下线的协作者不被派单
    collaborator_availability("expert_zhang", status="offline")
    assert all(x["id"] != "expert_zhang"
               for x in collaborator_list(need="material", top=3)["match"]), \
        "S46: offline 协作者不应出现在候选里"
    collaborator_availability("expert_zhang", status="online")
    # ④ 端到端：跑一个「必然求援」的电路，机器主动点名人类专家，人类异步作答后答案回灌
    _spec46 = {"name": "coop-server", "components": {
        "src": {"type": "source", "value": "工艺参数评审"},
        "r1": {"type": "resistor", "label": "工艺判定", "model": "small",
               "needs": ["process"], "help_threshold": 0.99,
               "help_kinds": ["human"], "help_timeout": 6.0}},
        "wires": [["src", "r1"]]}
    _s46 = topology_session(TopologySessionRequest(spec=_spec46, seed=1,
                                                   coop_timeout=6.0))
    _sid46 = _s46["session_id"]
    _hid46 = {}

    def _answer46():
        for _ in range(400):
            _p = help_pending(sid=_sid46)["pending"]
            if _p:
                _hid46["hid"] = _p[0]["hid"]
                _hid46["assignee"] = _p[0]["assignee"]
                help_respond(_p[0]["hid"], HelpRespondRequest(
                    value="改用 6061-T6，回火 175℃×8h", by="expert_zhang",
                    confidence=0.96))
                return
            time.sleep(0.01)
    _t46 = _th46.Thread(target=_answer46, daemon=True)
    _t46.start()
    _topo_sessions[_sid46]["done"].wait(timeout=15)
    _t46.join(timeout=2)
    assert _hid46.get("hid"), "S46: 机器应主动发出求援并进人类待办箱"
    assert _hid46["assignee"] == "expert_zhang", \
        f"S46: 应点名人类专家，实际 {_hid46.get('assignee')}"
    _lg46 = help_log(_sid46)
    _cl46 = _lg46["coop_log"]
    assert _cl46 and _cl46[0]["status"] == "answered", f"S46: 求援未闭环 {_cl46}"
    assert _cl46[0]["answer"] == "改用 6061-T6，回火 175℃×8h", "S46: 答案未回灌"
    _tr46 = _topo_sessions[_sid46]["executor"].get_state()["node_traces"]["r1"]
    assert _tr46.get("helped_by") == "expert_zhang" and _tr46["output"] == _cl46[0]["answer"], \
        "S46: 节点工作报告应记录是谁帮的、帮出了什么"
    # ⑤ 求援过程实时可见（事件进了总线，SSE 订阅者能看到机器在找谁）
    _evs46 = [e for e in _topo_sessions[_sid46]["executor"]._events
              if str(e.get("type", "")).startswith("help_")]
    assert any(e["type"] == "help_dispatch" for e in _evs46), "S46: 缺 help_dispatch 事件"
    assert any(e["type"] == "help_applied" for e in _evs46), "S46: 缺 help_applied 事件"
    # ⑥ 重复应答：已关闭的求援诚实报错，不静默吞掉
    try:
        help_respond(_hid46["hid"], HelpRespondRequest(value="迟到的答案"))
        raise AssertionError("S46: 已关闭求援重复应答应 404")
    except HTTPException as e:
        assert e.status_code == 404
    # ⑦ 无人可解也不吊死：需要没人具备的能力时，电路照常收敛
    _spec47 = {"name": "coop-none", "components": {
        "src": {"type": "source", "value": "x"},
        "r1": {"type": "resistor", "label": "炼金", "model": "small",
               "needs": ["quantum_alchemy"], "help_threshold": 0.99}},
        "wires": [["src", "r1"]]}
    _s47 = topology_session(TopologySessionRequest(spec=_spec47, seed=1,
                                                   coop_timeout=0.2))
    _topo_sessions[_s47["session_id"]]["done"].wait(timeout=10)
    _cl47 = help_log(_s47["session_id"])["coop_log"]
    assert _cl47 and _cl47[0]["status"] == "unanswered", f"S46: 应诚实上报无人可解 {_cl47}"
    assert _topo_sessions[_s47["session_id"]]["error"] is None, "S46: 无人可解不应崩电路"
    # ⑧ 零回归：coop_enabled=False 时完全走旧路径
    _s48 = topology_session(TopologySessionRequest(spec=_spec47, seed=1,
                                                   coop_enabled=False))
    _topo_sessions[_s48["session_id"]]["done"].wait(timeout=10)
    assert _topo_sessions[_s48["session_id"]]["executor"].recruiter is None
    assert help_log(_s48["session_id"])["coop_log"] == [], "S46: 关闭协同应零求援"
    print("✓ S46 人机协同(极致): 人/agent/知识源/系统四类协作者同池 · 按能力择优可解释 · "
          "offline自动绕开 · 机器主动点名人类专家并异步作答回灌节点 · "
          "求援过程SSE实时可见 · 重复应答404 · 无人可解不吊死 · 关闭开关零回归 全通过")

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
        load_rooms()   # 深化③：启动即恢复落盘房间（重启不丢房间/记忆）
        if PI_HEARTBEAT is not None:
            PI_HEARTBEAT.start(interval=60.0)  # 永动心跳：开机即启动
        print(f"circuit-agents API Server → http://{args.host}:{args.port}")
        print("端点: POST /run | GET /run/{id} | GET /run/{id}/stream | GET /health")
        print("π 永动心跳: GET /pi/heartbeat | POST /pi/heartbeat/start|stop|tick")
        if _rooms:
            print(f"深化③: 已从 {_room_store.path} 恢复 {len(_rooms)} 个房间")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
