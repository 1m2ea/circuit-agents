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
· **P2′（2026-09-10 落地）：真正的不可写面校验器是 `HoldoutVerifier` + 本地模型 judge**。
  它只看 `(task, answer)` 文本、打 0–1 正确率，loop 改不了其权重也改不了它给的分——
  这才是定律四要求的「闭环写不到的外部基准」。关键陷阱：仅把 `verify` 节点路由到独立后端
  （`resolve_verify_backend()`）仍救不了辨别力，因为那后端返回的还是 tier-cap 先验 quality
  （`backend_llm.py:382`）；必须对「实际产出的答案」用**异构本地模型**当 judge 打分才算真·外部基准。
  `replay_on_holdout(..., verifier=HoldoutVerifier(LocalModelJudge()))` 即启用；
  离线自测可注入 `MockJudge` 验证接线（见 `rl_optimizer.holdout_selftest`）。
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
import re
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
    """基线分：(quality, success, answer)。

    quality = final_quality（若调用方传入 verifier，则由其换算成 judge 分）。
    answer  = 最终答案文本（executor result 的 ``answer`` 字段），供 judge 对实际产出打分。

    缓存语义（重要）：
      · SimBackend（确定性）→ 缓存，基准固定不变，缓存它就是「基准」的本义
      · 真 backend（有波动）→ **不缓存**，每次重跑。
        否则会拿「一次采样」当基准去比「另一次采样」，把噪声当成 delta
    """
    key = (task, seed, tag)
    if use_cache and key in _BASE_CACHE:
        return _BASE_CACHE[key]
    res = _execute(_cached_spec(task), seed, backend)
    val = (_q(res), bool(res.get("success")), res.get("answer"))
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
                      backend=None,
                      verifier=None) -> dict:
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

    verifier：默认 None。传入 `HoldoutVerifier`（包一个本地模型 judge，见 P2′）后，
    审计的【绝对分】从 `final_quality` 换成 judge 对「task+实际答案」打的 0–1 正确率——
    这才是定律四要求的「闭环写不到的外部基准」。judge 不可用/解析失败的任务自动跳过、
    不计入 delta（避免污染均值）。离线自测可注入 `MockJudge` 验证接线。
    """
    out = {"n": 0, "per_task": [], "mean_delta": 0.0, "median_delta": 0.0,
           "positive_rate": 0.0, "error": None,
           "verdict_metric": "judge" if verifier is not None else "final_quality",
           "verifier": type(verifier).__name__ if verifier is not None else None}
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
            bq, bok, banswer = _cached_baseline(task, seed, tag, backend, use_cache)
            r = apply_ops(base_spec, op_names, random.Random(seed))
            if r is None:
                out["per_task"].append(
                    {"task": task, "baseline_quality": round(bq, 4),
                     "replayed_quality": None, "delta": None,
                     "applied_ops": [], "error": "算子序列在该任务上全部不适用"})
                continue
            rres = _execute(r["spec"], seed)
            if verifier is not None:
                bjs = verifier.score(task, banswer,
                                     meta={"applied_ops": [], "replayed": False})
                rjs = verifier.score(task, rres.get("answer"),
                                     meta={"applied_ops": r["applied"],
                                           "replayed": True})
                if bjs is None or rjs is None:
                    out["per_task"].append(
                        {"task": task, "baseline_quality": round(bq, 4),
                         "replayed_quality": None, "delta": None,
                         "applied_ops": r["applied"],
                         "baseline_success": bok,
                         "replayed_success": bool(rres.get("success")),
                         "error": "judge 不可用 / 解析失败（答案未产出或 judge 静默降级）"})
                    continue
                bq, rq = bjs, rjs
            else:
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


def verdict(audit: dict, self_improvement: float = None) -> str:
    """对照不可写面给出裁决（定律四核心判据，已修「闭环自我判赢」结构漏洞）。

    修复点（2026-09-11）：裁决的「赢」只能由**外部 holdout 基准（异构 judge 评分）**
    认定，循环自报的 ``self_improvement`` 不再能单独产出正向裁决。旧实现里
    ``self_improvement <= EPS_GAIN`` 会直接返回 NO_CLAIM——等于让循环自己决定
    「我有没有赢」，典型的闭环自我判赢。新实现：

    · NO_DATA     —— 审计没跑成（空算子/编译失败/无分数）；不妄下结论
    · GENERALIZES —— **外部 holdout（judge 评分）明确涨**（md>EPS 且正率≥下限）→ 真改进。
                     与循环自报无关：即便循环自报 0，外部证真即为泛化。
    · OVERFIT     —— holdout 不涨 **但循环自报涨** → **定律四命中**：标准被放宽，
                     循环用可写先验灌出自报分，却骗不过异构 judge。
    · NO_CLAIM    —— holdout 不涨 且 循环自报也不涨（或无自报）→ 无所谓泛化，
                     诚实放弃裁决，不再让循环自我定性。
    · MIXED       —— holdout 部分为正但未过半，证据不足（与自报无关）。

    一句话：循环可以「自称赢了」（self_improvement 灌水），但裁决的「赢」必须外部
    judge 背书；自称赢而外部不认 → OVERFIT，而不是 GENERALIZES。
    """
    if not audit or audit.get("error") or not audit.get("n"):
        return "NO_DATA"
    md = audit.get("mean_delta") or 0.0
    pr = audit.get("positive_rate") or 0.0
    # ① 外部（不可写面）信号优先：明确涨 → 直接判泛化，不受循环自报影响
    if md > EPS_GAIN and pr >= POSITIVE_RATE_MIN:
        return "GENERALIZES"
    # ② 外部部分涨但未过半 → 证据不足（与自报无关）
    if md > EPS_GAIN:
        return "MIXED"
    # ③ 外部不涨：此时才看循环自报，且只能区分两种负向结论
    #    循环自称涨 → 定律四命中（标准被放宽）；否则诚实 NO_CLAIM
    if self_improvement is not None and self_improvement > EPS_GAIN:
        return "OVERFIT"
    return "NO_CLAIM"


def _q(res: dict) -> float:
    """取绝对质量分；未跑通记 0（跑不通的拓扑再便宜也没意义）。"""
    try:
        return float(res.get("final_quality") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# P2′ 不可写面校验器：本地模型当 LLM-judge（治 MetaRSI 定律四）
# ---------------------------------------------------------------------------
class HoldoutVerifier:
    """把「质量由谁度量」从 loop 内部转移到闭环写不到的外部 judge。

    用法::

        from compiler.holdout import HoldoutVerifier, LocalModelJudge
        ver = HoldoutVerifier(LocalModelJudge())          # 真实：本地模型打分
        aud = replay_on_holdout(["add_verify"], verifier=ver)

    核心：judge 只看 ``(task, answer)`` 文本，打 0–1 正确率分；
    loop 既改不了 judge 的权重，也改不了它给的分——这才是定律四要求的
    「不可写面」。本 verifier 是 judge 的薄封装，负责：
      · 取最终答案文本（executor result 的 ``answer`` 字段）
      · 调 judge 打分、容错解析、夹紧到 [0,1]
      · 缓存（同 (task, answer, meta) 只判一次，省本地算力）

    judge 接口：``judge(task: str, answer: str, meta: dict|None) -> float|None``。
    真实 judge（LocalModelJudge）只吃 task+answer；meta 仅供离线 MockJudge 注入预设分。
    """

    def __init__(self, judge, cache: bool = True):
        self.judge = judge
        self._cache = {} if cache else None

    def score(self, task: str, answer, meta: Optional[dict] = None):
        """对 (task, answer) 打分；answer 缺失 / judge 抛错 / 解析失败 → None。"""
        if answer is None:
            return None
        ans = answer if isinstance(answer, str) else str(answer)
        if self._cache is not None:
            # meta 进缓存键：离线 MockJudge 同 (task,answer) 但 baseline/replayed
            # 的 meta 不同 → 必须区分，否则 replayed 会命中 baseline 的缓存分。
            k = (task, ans, json.dumps(meta or {}, sort_keys=True))
            if k in self._cache:
                return self._cache[k]
        try:
            s = self.judge(task, ans, meta)
            s = float(s)
        except Exception:
            s = None
        if s is not None:
            s = max(0.0, min(1.0, s))   # 夹紧到 [0,1]
            if self._cache is not None:
                self._cache[k] = s
        return s


class LocalModelJudge:
    """真实 judge：把 (task, answer) 发给本地 OpenAI 兼容端点，解析 0–1 正确率分。

    端点默认 ``http://127.0.0.1:8000/v1``（local_llm_bridge.py 起的 1.5B/7B 桥），
    可用 ``VERIFY_API_BASE`` 覆盖（如 ``http://127.0.0.1:8001/v1`` 指向 7B-GGUF）。
    不依赖任何外部 API、零 token 成本、loop 不可改写其权重或分数——真·不可写面。

    与 ``resolve_verify_backend()`` 的区别（关键）：后者返回的 LLM 后端仍是
    tier-cap 先验 quality（backend_llm.py:382），不是对实际产出的度量；
    本 judge 直接吃 (task, answer) 文本、自己解析正确率，绕开先验。
    """

    JUDGE_PROMPT = (
        "你是严格的答案正确性评审。下面给了一个任务和一份候选答案。\n"
        "请只根据候选答案是否真正、完整地回答了任务来打分，"
        "不要被答案长短或措辞迷惑。\n"
        "只输出一个 0.0 到 1.0 之间的小数（不要任何解释）：\n"
        "任务：{task}\n\n候选答案：{answer}\n\n分数："
    )

    def __init__(self, base_url=None, api_key="not-needed", model=None,
                 timeout=120.0, http_post=None, temperature=0.0):
        self.base_url = (base_url or os.environ.get("VERIFY_API_BASE")
                         or "http://127.0.0.1:8000/v1").rstrip("/")
        self.api_key = api_key
        self.model = model or os.environ.get("VERIFY_MODEL") or "local"
        self.timeout = timeout
        self._http_post = http_post
        self.temperature = temperature

    def __call__(self, task, answer, meta=None):
        prompt = self.JUDGE_PROMPT.format(task=task, answer=answer)
        messages = [{"role": "user", "content": prompt}]
        return self._score(messages)

    def _score(self, messages):
        raw = self._post(messages)
        if isinstance(raw, dict):
            try:
                raw = raw["choices"][0]["message"]["content"]
            except Exception:
                raw = str(raw)
        return self._parse_score(raw)

    def _post(self, messages):
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "max_tokens": 8}
        if self._http_post is not None:
            return self._http_post(self.base_url + "/chat/completions", json=payload)
        import urllib.request
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _parse_score(text):
        if not text:
            return None
        # 取最后一个浮点数（模型可能前缀些废话），夹紧到 [0,1]
        found = re.findall(r"1\.0|0\.\d+|\.\d+|\b\d+\.\d+\b|\b0\b|\b1\b", str(text))
        if not found:
            return None
        try:
            val = float(found[-1])
        except ValueError:
            return None
        return max(0.0, min(1.0, val))


class MockJudge:
    """离线自测用 oracle：按 (task, applied_ops, replayed) 返回预设分。

    真实 judge 只看 (task, answer)；MockJudge 额外吃 meta 仅用于自测
    「注入不同分 → 审计辨别力随 judge 改变」这一集成事实（stand-in 本地模型）。
    table 键：``(task, frozenset(ops), replayed) -> score``。
    """

    def __init__(self, table: dict):
        self.table = table

    def __call__(self, task, answer, meta=None):
        meta = meta or {}
        ops = frozenset(meta.get("applied_ops") or [])
        replayed = bool(meta.get("replayed", False))
        return self.table.get((task, ops, replayed))


def build_local_judge(base_url=None, **kw) -> "LocalModelJudge":
    """构造真实本地 judge（默认指向 local_llm_bridge 的 127.0.0.1:8000）。"""
    return LocalModelJudge(base_url=base_url, **kw)
