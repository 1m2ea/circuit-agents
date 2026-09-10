"""
circuit-agents · compiler.holdout
=================================
不可写面（non-writable surface）holdout 基准 —— 治 MetaRSI 定律四。

定律四（可信度由不可写面度量）：
    在闭环内部，「能力真的变强」和「成功标准被放宽」会得到完全相同的分数，
    而且这种偏差从内部无法察觉，也无法靠统计纠正。
    唯一解药：引入一个**闭环写不到的**外部基准。

本模块做的事
------------
1. 维护一组固定的 holdout 任务（只读；optimize 过程绝不修改它）
2. 把 optimize 在某任务上搜出的「算子改动序列」**重放**到这批 holdout 任务上
3. 用【绝对分】（`final_quality`，不做 self 归一）衡量这套改法是否真的泛化

为什么是「重放算子序列」而不是「跑同一个 spec」
----------------------------------------------
optimize 产出的是针对某个具体任务拓扑的改动，直接把 spec 搬到别的任务上没有意义。
真正值得检验的是「这套改法（哪几类算子、什么顺序）是不是通用有效」——
这正是「改进的是不是改进本身」该被验证的粒度。

重放按**算子类型**而非节点 id：不同 spec 的节点 id 不同，
我们检验的是「改法类别」的泛化性，不是某个具体节点的运气。

诚实边界（2026-09-10 实测，务必读）
--------------------------------
· **离线辨别力有限**：在 SimBackend 下实测各算子重放 6 个 holdout 任务的 mean_delta——
    `swap_model` +0.0118（5/6 为正）| `add_verify` 0.0 | `drop_verify` 0.0
    `parallelize` 全部不适用 | `add_capacitor` 全部不适用
  即：**模拟质量只建模了「模型档位」的影响，加/删校验节点对 final_quality 完全无影响**。
  所以离线状态下本审计只能分辨「换档类」改动，对「结构类」改动零辨别力。
  这不是本模块的缺陷，而是 SimBackend 的建模缺口——反过来恰好印证定律四的担忧：
  **RL 基于该 reward 得出的「加校验有用/没用」结论本身就不可信**。
· ⚠️ **真后端实证（2026-09-10，DeepSeek 真跑）推翻原预期**：换上真 LLM 后端后，
  `add_verify` 重放 mean_delta 仍≈0（smoke 实测 −0.055，n=1；full 5 算子×3 任务见报告 §10）。
  根因在 `compiler/backend_llm.py:382`：`quality = tier-cap 先验`，**不是对 LLM 实际产出的度量**；
  `ok` 仅是「回了非空内容」。所以「换真后端就有真实辨别力」在当前质量模型下**不成立**——
  辨别力天花板由质量信号本身决定：只要 quality 是 loop 能写的先验，换什么后端都救不了结构辨别力。
· 要真·不可写面辨别力，必须让质量由**独立校验器**量真实正确率：
  即 `resolve_verify_backend()`（VERIFY_API_KEY / VERIFY_API_BASE 异构校验路径）或外部评判，
  用与生产者不同的模型/供应商给 final_quality 打分。这才是定律四要求的「闭环写不到的外部基准」。
· 本审计的正确定位（修正版）：它是**诚实的度量框架**——离线可分辨换档类改动，
  真后端接上异构校验器后可分辨结构类改动；但**单靠换真后端（同构质量先验）救不了辨别力**。
· 本模块只读、只审计，绝不修改任何被优化对象；失败一律静默降级，不拖崩主流程。
· 基线分走缓存是**语义正确**（基准本就不该变），不是性能优化的偷懒；
  换后端或改任务集后请调 `clear_cache()`。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC_CACHE_PATH = os.path.join(_PROJECT_ROOT, ".holdout_spec_cache.json")
# 编译逻辑有变更时手工 bump，避免吃到陈旧 spec
CACHE_VERSION = "v1"

# 缓存：holdout 任务集是固定的，「基准」本就不该变——缓存它是语义正确而非偷懒
#   · _SPEC_CACHE  task -> spec（编译一次；重放时 deepcopy，不污染缓存）
#   · _BASE_CACHE  (task, seed) -> (quality, success)，仅内存（受后端影响，不落盘）
# 清缓存：clear_cache()
_SPEC_CACHE: dict = {}
_BASE_CACHE: dict = {}
_SPEC_CACHE_LOADED = False


def _tasks_sig() -> str:
    return hashlib.md5(
        (CACHE_VERSION + "\n" + "\n".join(HOLDOUT_TASKS)).encode("utf-8")
    ).hexdigest()[:12]


def _load_spec_cache():
    """从磁盘载入编译缓存（首次 ~15s 的编译开销 → 复用后 ~0s）。"""
    global _SPEC_CACHE_LOADED
    if _SPEC_CACHE_LOADED:
        return
    _SPEC_CACHE_LOADED = True
    try:
        with open(_SPEC_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("sig") == _tasks_sig():
            _SPEC_CACHE.update(d.get("specs", {}))
    except (OSError, ValueError):
        pass


def _save_spec_cache():
    try:
        with open(_SPEC_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"sig": _tasks_sig(), "specs": _SPEC_CACHE},
                      f, ensure_ascii=False)
    except OSError:
        pass


def clear_cache():
    """清空编译/基线缓存（换后端、改任务集或 bump CACHE_VERSION 后调用）。"""
    _SPEC_CACHE.clear()
    _BASE_CACHE.clear()
    global _SPEC_CACHE_LOADED
    _SPEC_CACHE_LOADED = False
    try:
        os.unlink(_SPEC_CACHE_PATH)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# 不可写面：固定 holdout 任务集
# ---------------------------------------------------------------------------
# 设计约束（改动前请读）：
#   · 覆盖不同能力组合，避免单一模板让审计失真
#   · 目标文本保持【普通自然语言】，不写死模板名——让编译器自己选
#   · 语义稳定：任何一次改动都会让跨时间的审计结果不可比，原则上只增不改
HOLDOUT_TASKS: tuple = (
    "检索行业数据并分析后综述",
    "核对两份文档的差异并整理结论",
    "计算季度增长率并核对结果",
    "抽取合同关键条款并分类整理",
    "检索政策文件并抽取要点后总结",
    "分析实验数据并给出可验证结论",
)

# 判定阈值
EPS_GAIN = 1e-3          # 小于此值视为「没有变化」
POSITIVE_RATE_MIN = 0.5  # 至少一半 holdout 任务为正才算泛化


def load_tasks() -> list:
    """返回 holdout 任务副本（只读语义：返回新 list，调用方改不到模块常量）。"""
    return list(HOLDOUT_TASKS)


# ---------------------------------------------------------------------------
# 编译与执行
# ---------------------------------------------------------------------------
def _to_spec(goal_or_spec):
    """goal 字符串 / spec dict → spec（离线规则解析，与 RLOptimizer._to_spec 同策略）。"""
    if isinstance(goal_or_spec, dict) and "components" in goal_or_spec:
        return goal_or_spec
    import os
    os.environ.pop("AGENT_API_KEY", None)      # 强制离线规则解析
    from .nl_parser import GoalParser
    from .compile import compile_goal
    goal = GoalParser().parse(str(goal_or_spec))
    return compile_goal(goal, auto_bind=True, route=True, memory_enabled=False)


def _cached_spec(task: str) -> dict:
    """编译结果缓存（任务集固定 → 只需编译一次）。返回深拷贝，调用方改不到缓存。"""
    _load_spec_cache()
    if task not in _SPEC_CACHE:
        _SPEC_CACHE[task] = _to_spec(task)
        _save_spec_cache()
    return json.loads(json.dumps(_SPEC_CACHE[task]))


def _cached_baseline(task: str, seed: int, tag: str = "sim",
                     backend=None, use_cache: bool = True) -> tuple:
    """基线分：(quality, success)。

    缓存语义（重要）：
      · SimBackend（确定性）→ 缓存，基准固定不变，缓存它就是「基准」的本义
      · 真 backend（有波动）→ **不缓存**，每次重跑。
        否则会拿「一次采样」当基准去比「另一次采样」，把噪声当成 delta
    """
    key = (task, seed, tag)
    if use_cache and key in _BASE_CACHE:
        return _BASE_CACHE[key]
    res = _execute(_cached_spec(task), seed, backend)
    val = (_q(res), bool(res.get("success")))
    if use_cache:
        _BASE_CACHE[key] = val
    return val


def _execute(spec: dict, seed: int, backend=None) -> dict:
    """真跑一遍（与 RLOptimizer.evaluate 同路径：CircuitExecutor）。

    backend=None → SimBackend（默认，确定性、离线）
    backend 传入   → 用它（真后端 / 注入的假后端），可测真实辨别力
    """
    from runtime import Circuit, CircuitExecutor, SimBackend
    try:
        be = backend if backend is not None else SimBackend(random.Random(seed))
        circ = Circuit(spec, be)
        return CircuitExecutor(circ, memory_enabled=False,
                               auto_select_models=False).run()
    except Exception as e:
        return {"success": False, "final_quality": 0.0, "total_cost": 0.0,
                "total_latency_ms": 0.0, "error": f"{type(e).__name__}: {e}"}


def apply_ops(spec: dict, op_names: list, rng: random.Random) -> Optional[dict]:
    """把一串算子按类型重放到 spec 上（不按节点 id —— 检验的是改法类别）。

    返回最终 spec；某算子不适用（返回 None）则跳过，全部不适用则返回 None。
    """
    from .rl_optimizer import ACTIONS          # 延迟导入，避免与 rl_optimizer 循环依赖
    cur, applied = spec, []
    for name in op_names:
        fn = ACTIONS.get(name)
        if fn is None:
            continue
        try:
            mutated = fn(cur, rng)
        except Exception:
            mutated = None
        if mutated is None:
            continue
        cur = mutated["spec"]
        applied.append(name)
    return {"spec": cur, "applied": applied} if applied else None


# ---------------------------------------------------------------------------
# 审计主入口
# ---------------------------------------------------------------------------
def replay_on_holdout(op_names: list,
                      tasks: Optional[list] = None,
                      seed: int = 0,
                      max_tasks: int = 0,
                      backend=None) -> dict:
    """把算子序列重放到 holdout 任务集，用绝对分检验是否泛化。

    返回::

        {"n": 任务数, "per_task": [{"task","baseline_quality","replayed_quality",
                                    "delta","applied_ops"}],
         "mean_delta", "median_delta", "positive_rate", "error"}

    绝对分 = `final_quality`（不做任何 self 归一）——这是「不可写面」的关键：
    一旦允许按自身基线归一，闭环就能通过抬高基线制造进步的假象。

    backend：默认 None（SimBackend，确定性离线）。传入真后端即可测**真实辨别力**
    —— 离线状态下 add_verify/drop_verify 的 delta 恒为 0（模拟质量不建模 verify），
    只有换上真后端，这几类结构改动才可能显出信号。真后端有波动 → 基线不缓存。
    """
    out = {"n": 0, "per_task": [], "mean_delta": 0.0, "median_delta": 0.0,
           "positive_rate": 0.0, "error": None}
    if not op_names:
        out["error"] = "空算子序列（无可重放改动）"
        return out
    task_list = list(tasks) if tasks else load_tasks()
    if max_tasks and max_tasks > 0:
        task_list = task_list[:max_tasks]
    if not task_list:
        out["error"] = "holdout 任务集为空"
        return out

    deltas = []
    real = backend is not None
    tag = "real" if real else "sim"
    use_cache = not real          # 真后端有波动 → 基线每次重跑，不拿单次采样当基准
    try:
        for i, task in enumerate(task_list):
            try:
                base_spec = _cached_spec(task)
            except Exception as e:
                out["per_task"].append(
                    {"task": task, "baseline_quality": None,
                     "replayed_quality": None, "delta": None,
                     "applied_ops": [], "error": f"编译失败: {type(e).__name__}"})
                continue
            bq, bok = _cached_baseline(task, seed, tag, backend, use_cache)
            r = apply_ops(base_spec, op_names, random.Random(seed))
            if r is None:
                out["per_task"].append(
                    {"task": task, "baseline_quality": round(bq, 4),
                     "replayed_quality": None, "delta": None,
                     "applied_ops": [], "error": "算子序列在该任务上全部不适用"})
                continue
            rres = _execute(r["spec"], seed)
            rq = _q(rres)
            delta = round(rq - bq, 4)
            deltas.append(delta)
            out["per_task"].append(
                {"task": task, "baseline_quality": round(bq, 4),
                 "replayed_quality": round(rq, 4), "delta": delta,
                 "applied_ops": r["applied"],
                 "baseline_success": bok,
                 "replayed_success": bool(rres.get("success"))})
    except Exception as e:                      # 兜底：审计绝不能拖崩主流程
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    scored = [d for d in deltas if d is not None]
    out["n"] = len(out["per_task"])
    out["backend"] = tag
    out["baseline_cached"] = use_cache
    if scored:
        out["mean_delta"] = round(statistics.fmean(scored), 4)
        out["median_delta"] = round(statistics.median(scored), 4)
        out["positive_rate"] = round(
            sum(1 for d in scored if d > EPS_GAIN) / len(scored), 3)
    else:
        out["error"] = "holdout 全部任务未能产出可比分数"
    return out


def verdict(audit: dict, self_improvement: float) -> str:
    """对照不可写面给出裁决。

    · NO_DATA     —— 审计没跑成（空算子/编译失败/无分数）；不妄下结论
    · GENERALIZES —— 自报提升 + holdout 也涨 → 真改进（可写面与不可写面一致）
    · OVERFIT     —— 自报提升 但 holdout 不涨 → **定律四命中**：标准可能被放宽
    · MIXED       —— holdout 部分为正但未过半，证据不足
    · NO_CLAIM    —— 自报没提升，无所谓是否泛化
    """
    if not audit or audit.get("error") or not audit.get("n"):
        return "NO_DATA"
    if self_improvement is None or self_improvement <= EPS_GAIN:
        return "NO_CLAIM"
    md = audit.get("mean_delta") or 0.0
    pr = audit.get("positive_rate") or 0.0
    if md > EPS_GAIN and pr >= POSITIVE_RATE_MIN:
        return "GENERALIZES"
    if md <= EPS_GAIN:
        return "OVERFIT"
    return "MIXED"


def _q(res: dict) -> float:
    """取绝对质量分；未跑通记 0（跑不通的拓扑再便宜也没意义）。"""
    try:
        return float(res.get("final_quality") or 0.0)
    except (TypeError, ValueError):
        return 0.0
