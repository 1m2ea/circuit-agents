"""
circuit-agents · compiler.router_auto
====================================
第二层⑦「自动布局布线器」的核心：能力语义依赖推断。

给定 capability 集合，按一张"生产者→消费者"语义邻接表自动推断 DAG 边，
让"能并行的天然并行、该串的依赖串"——而不是默认全串行，也不是 M3 那种
昂贵 Pareto 搜索的全局串/并切换。

与 M3 Optimizer 的区别：
  - M3（开 --optimize）枚举 {串联/并联}×tiers×冗余×反馈 跑 ~7200 次仿真求 Pareto；
  - 本模块是默认快路径的**确定性、零仿真**语义推断，即时给出"够用"的布线。
  - 二者不冲突：不开 --optimize 走本推断；开了 --optimize 由搜索定拓扑。

接入方式（plan.py）：仅当 goal.dependencies is None（默认串行）时套用，
已测试的并行意图路径（nl_parser 的 source/sink 粗分）与模板显式 DAG 不动。
可用 --no-auto-route 回退到旧默认串行。
"""
from __future__ import annotations

import re

# 能力"角色优先级"：仅允许从低优先级流向严格更高优先级（守卫，保证 DAG 无环）。
_CAP_PRIORITY = {
    "retrieve": 0, "extract": 1, "calculate": 1, "translate": 1,
    "reason": 2, "classify": 2,
    "verify": 3, "organize": 4, "summarize": 5,
}

# 语义邻接（启发式）：producer 的潜在消费者。实际边还要过优先级守卫。
_SEMANTIC_EDGES = {
    "retrieve":   ["extract", "calculate", "translate", "reason", "classify",
                   "verify", "organize", "summarize"],
    "extract":    ["reason", "classify", "verify", "organize", "summarize"],
    "calculate":  ["reason", "verify", "organize", "summarize"],
    "translate":  ["reason", "classify", "verify", "organize", "summarize"],
    "reason":     ["verify", "organize", "summarize"],
    "classify":   ["verify", "organize", "summarize"],
    "verify":     ["organize", "summarize"],
    "organize":   ["summarize"],
    "summarize":  [],
}


def _base(cap: str) -> str:
    """能力名可能带多重集/冗余后缀（retrieve#2、cap#r），归一回基础名。"""
    return re.sub(r"#\d+$", "", cap)


def infer_dependencies(goal) -> list:
    """推断 capability 间的 DAG 依赖边（[[producer, consumer], ...]）。

    - 按语义邻接表 + 优先级守卫生成边，保证无环、无自环；
    - 多重集能力（retrieve 与 retrieve#2）按基础名匹配，两个实例都正确喂下游；
    - 无边（如单能力、或彼此无语义关系的组合）返回 None（保持默认串行更直观）。
    返回的边可直接喂 Router（dependencies 字段）。
    """
    caps = goal.capabilities
    edges = []
    seen = set()
    for producer in caps:
        bp = _base(producer)
        consumers = _SEMANTIC_EDGES.get(bp, [])
        pp = _CAP_PRIORITY.get(bp, 9)
        for consumer in caps:
            if consumer == producer:
                continue
            bc = _base(consumer)
            if bc not in consumers:
                continue
            # 优先级守卫：仅保留 producer 严格早于 consumer 的边（保证无环）
            if pp < _CAP_PRIORITY.get(bc, 9):
                key = (producer, consumer)
                if key not in seen:
                    seen.add(key)
                    edges.append([producer, consumer])
    return edges if edges else None


def _norm_token(s: str) -> str:
    """归一化产物名/输入名：小写、去空白与标点，用于抗命名漂移。

    例："GDP 总量" / "gdp_total" / "GDP总量" → 不同写法也会被尽量归并；
    但差异过大（"gdp_total" vs "china_gdp_total"）仍会判为不匹配并告警，
    而非静默误并——这正是『混合对齐』设计要兜住的边界。
    """
    s = (s or "").lower().strip()
    s = re.sub(r"[\s_\-./:：，,；;（）()\"'']+", "", s)
    return s


# ---------------------------------------------------------------------------
# 命名漂移符号映射（确定性「转接头」）
# ---------------------------------------------------------------------------
# 背景：上游产出 gdp_china_2024 / 下游输入 china_gdp_2024 —— 连线在、信号在，
# 只是标签没对上，导致"接触不良"（依赖被误判为未满足 → 误并联 + 运行期 gate:fail_linear）。
# 本组函数是确定性编译步骤：在 netlist 生成期，用纯规则判断「名不同但语义等价」的
# 变量对 {下游输入: 上游产物}，把映射表注入拓扑，下游按映射取数——不靠更聪明的 AI。
# 零回归：规则判不出（或歧义多解）→ 维持现状（告警 + 不建边退并联 + 运行期 gate:fail_linear）。

# 可选同义词表：把同义 token 归一为同一 canonical token（跨写法/跨语言漂移兜底）。
# 命中即视为同一成分；为空也安全（纯 token 集合比较照样抓词序漂移）。
SYNONYMS = {
    "国内生产总值": "gdp", "国民生产总值": "gdp", "总产值": "gdp",
    "人均": "percapita", "总量": "total", "总额": "total", "合计": "total",
    "grossdomesticproduct": "gdp",
}


def _tokens(s: str) -> frozenset:
    """把变量名拆成『成分 token 集合』（小写；CJK/字母/数字各自成 token；
    同义词 token 归一为 canonical）。用于抗词序漂移与同义漂移。

    例：gdp_china_2024 → {gdp, china, 2024}；china_gdp_2024 → {china, gdp, 2024}
        → 集合相等，判定等价（词序不同而已）。
    """
    s = (s or "").lower()
    raw = re.findall(r"[a-z]+|[0-9]+|[一-鿿]+", s)
    return frozenset(SYNONYMS.get(t, t) for t in raw)


def _lev_ratio(a: str, b: str) -> float:
    """归一化字符串的 Levenshtein 相似度（difflib.SequenceMatcher.ratio，纯 stdlib）。"""
    if a == b:
        return 1.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _equiv_drift(y: str, x: str) -> bool:
    """判定下游输入名 y 与上游产物名 x 是否『命名漂移但语义等价』。

    纯确定性规则（零 LLM 成本）：
      ① token 集合相等（含同义词归一）→ 词序/同义不同即等价；
      ② Levenshtein 比 ≥0.85 且非包含子串关系 → 轻微拼写/编码漂移
         （非包含子串防 base/qualifier 误并，如 gdp_total vs gdp_total_2）。
    命中任一即等价。
    """
    if _tokens(y) == _tokens(x):                      # 规则①：成分相同
        return True
    ny, nx = _norm_token(y), _norm_token(x)
    if ny and nx and _lev_ratio(ny, nx) >= 0.85:      # 规则②：近重复且非包含
        if ny not in nx and nx not in ny:
            return True
    return False


def _analyze_dependencies(subs, override_deps=None):
    """数据依赖分析：返回 (edges, input_maps)。

    edges: [[producer_subtask_id, consumer_subtask_id], ...]（确定性 netlist）。
    input_maps: {consumer_subtask_id: {下游输入名: 上游实际产物名}}，
        仅含『经符号映射判定等价、但名称不一致』的对（精确同名匹配不进表，已是同一变量）。

    规则（编译器式，零 LLM 参与）：
      · 扫描所有 outputs 建立「归一化产物名 → 生产方」与全量 (原名, 生产方) 列表；
      · 下游每个 input：
          - 归一化后精确命中某 output → 建边、不进映射表（同一变量）；
          - 否则进『命名漂移符号映射』：用确定性规则找出等价的上游产物；
              恰好 1 个候选 → 建边 + 记 {输入名: 产出名} 映射；
              0 个或 ≥2 个（歧义）→ 维持现状（仅告警、不建边、退并联，运行期诚实 gate:fail）。
      · override_deps：LLM 显式『非数据软依赖』合并进边（去重）。
    """
    subs = subs or []
    by_id = {}
    all_outputs = []          # (原名, 生产方 subtask id)
    produced = {}             # norm(output) -> 生产方 subtask id（精确匹配用）
    for st in subs:
        sid = st.get("id")
        by_id[sid] = st
        for o in (st.get("outputs") or []):
            no = _norm_token(o)
            if not no:
                continue
            all_outputs.append((o, sid))
            produced[no] = sid     # 同 norm 取最后（与旧行为一致）

    edges = []
    seen = set()
    input_maps = {}
    for st in subs:
        sid = st.get("id")
        for inp in (st.get("inputs") or []):
            ni = _norm_token(inp)
            if not ni:
                continue
            if ni in produced:
                # 精确匹配：同一变量，直接建边、不进映射表
                prod = produced[ni]
                if prod == sid:
                    continue
                key = (prod, sid)
                if key not in seen:
                    seen.add(key)
                    edges.append([prod, sid])
            else:
                # 命名漂移：用确定性规则找等价上游产物
                candidates = [(orig, psid) for (orig, psid) in all_outputs
                              if psid != sid and _equiv_drift(inp, orig)]
                if len(candidates) == 1:
                    X, prod = candidates[0]
                    key = (prod, sid)
                    if key not in seen:
                        seen.add(key)
                        edges.append([prod, sid])
                    input_maps.setdefault(sid, {})[inp] = X
                    print(f"[router] 符号映射(命名漂移修复): 子任务 {sid} 的 {inp!r} "
                          f"← 上游 {prod} 的 {X!r}")
                else:
                    # 0 或歧义多解 → 维持现状：仅告警、不建边、退并联
                    reason = ("歧义多解，已放弃自动映射" if candidates
                              else "无任何 subtask 产出该名")
                    print(f"[router] 注意：子任务 {sid} 需要 {inp!r}，但{reason}；"
                          f"视为外部/已满足输入，不建依赖边"
                          f"（若应为内部依赖，请让上游 outputs 复用此名，或显式声明 dependencies）")
    for e in (override_deps or []):
        if (isinstance(e, (list, tuple)) and len(e) == 2
                and e[0] in by_id and e[1] in by_id and e[0] != e[1]):
            key = (e[0], e[1])
            if key not in seen:
                seen.add(key)
                edges.append([e[0], e[1]])
    return edges, input_maps


def dependencies_from_subtasks(subtasks: list, override_deps: list = None) -> list:
    """兼容签名：只返回依赖边（与历史行为一致）。

    需要符号映射表时请改用 :func:`_analyze_dependencies`（返回 (edges, input_maps)）。
    """
    """从子任务 input/output 确定性算出依赖边（数据依赖分析 / netlist 生成）。

    规则（编译器式，零 LLM 参与）：
      · 扫所有 subtask 的 outputs，建立「归一化产物名 → 生产方 subtask id」映射；
      · 对每个 subtask Y 的每个 input 名，若归一化后命中某 X 的 output → 边 [X.id, Y.id]
        （Y 依赖 X，串联在 X 之后）；互不引用 → 并联（拓扑自然涌现）。
      · 未满足的 input（无任何 subtask 产出该名）→ 视为外部/已满足输入，**仅告警、不建边**，
        避免「命名漂移」导致静默误并（混合对齐的核心兜底）。
      · override_deps：LLM 显式给的『非数据软依赖』[[pre_id, post_id]]，合并进结果（去重）。

    返回边列表（subtask id 对），确定性的；subtask→节点名翻译在调用方（nl_parser）完成。
    """
    subs = subtasks or []
    by_id = {}
    produced = {}          # norm(output) -> producer subtask id
    for st in subs:
        sid = st.get("id")
        by_id[sid] = st
        for o in (st.get("outputs") or []):
            no = _norm_token(o)
            if not no:
                continue
            if no in produced and produced[no] != sid:
                print(f"[router] 警告：产物名 {o!r} 被多个子任务产出"
                      f"（{produced[no]} 与 {sid}），依出现顺序取 {sid}")
            produced[no] = sid

    edges = []
    seen = set()
    for st in subs:
        sid = st.get("id")
        for inp in (st.get("inputs") or []):
            ni = _norm_token(inp)
            if not ni:
                continue
            if ni in produced:
                prod = produced[ni]
                if prod == sid:
                    continue  # 自依赖（input 引用自己的 output）→ 忽略
                key = (prod, sid)
                if key not in seen:
                    seen.add(key)
                    edges.append([prod, sid])
            else:
                # 未满足 input：无 subtask 产出该名 → 外部依赖，告警不建边
                print(f"[router] 注意：子任务 {sid} 需要 {inp!r}，但无子任务产出该名；"
                      f"视为外部/已满足输入，不建依赖边"
                      f"（若应为内部依赖，请让上游 outputs 复用此名）")
    # 合并 LLM 软依赖覆盖（非数据、纯顺序的显式边）
    for e in (override_deps or []):
        if (isinstance(e, (list, tuple)) and len(e) == 2
                and e[0] in by_id and e[1] in by_id and e[0] != e[1]):
            key = (e[0], e[1])
            if key not in seen:
                seen.add(key)
                edges.append([e[0], e[1]])
    return edges


def selftest():
    """离线自检：DAG 无环 / 多重集 / 并行源 / 单能力回 None。"""
    from .goal import Goal

    # 1) 基础推断：[retrieve, reason, summarize] → retrieve→reason, retrieve→summarize, reason→summarize
    g = Goal(capabilities=["retrieve", "reason", "summarize"])
    e = infer_dependencies(g)
    assert e is not None, "应有推断边"
    assert ["retrieve", "reason"] in e and ["reason", "summarize"] in e, e
    assert ["summarize", "reason"] not in e, "不应有反向边"

    # 2) 多重集并行源：[retrieve, retrieve#2, reason] → 两个 retrieve 都喂 reason（并行源）
    g2 = Goal(capabilities=["retrieve", "retrieve#2", "reason"])
    e2 = infer_dependencies(g2)
    assert ["retrieve", "reason"] in e2 and ["retrieve#2", "reason"] in e2, e2
    assert ["retrieve", "retrieve#2"] not in e2, "不应有 retrieve 自连"

    # 3) 单能力 → None（保持默认串行）
    g3 = Goal(capabilities=["summarize"])
    assert infer_dependencies(g3) is None, "单能力应回 None"

    # 4) 无环：全能力组合推断后仍可被 Goal.from_dict 接受（Kahn 排得完）
    all_caps = ["retrieve", "extract", "calculate", "translate",
                "reason", "classify", "verify", "organize", "summarize"]
    g4 = Goal(capabilities=all_caps)
    e4 = infer_dependencies(g4)
    g4.dependencies = e4  # 直接赋值（绕过 from_dict 也行，这里验证 from_dict 不报错）
    _ = Goal.from_dict(g4.to_dict())

    # 5) 数据依赖分析：A/B 互不依赖(并联) → 共同喂 C（串联在 C 前）
    subs = [
        {"id": "A", "capability": "retrieve", "inputs": [], "outputs": ["gdp_total"]},
        {"id": "B", "capability": "retrieve", "inputs": [], "outputs": ["gdp_per_capita"]},
        {"id": "C", "capability": "reason", "inputs": ["gdp_total", "gdp_per_capita"], "outputs": ["analysis"]},
    ]
    e5 = dependencies_from_subtasks(subs)
    assert e5 == [["A", "C"], ["B", "C"]], e5
    print("✓ 数据依赖分析：input/output 自动算出拓扑（A,B 并联 → 共喂 C）")

    # 6) 未满足 input：命名漂移不静默误并，仅告警、不建边 → 两子任务并联
    subs2 = [
        {"id": "X", "capability": "retrieve", "inputs": [], "outputs": ["x"]},
        {"id": "Y", "capability": "reason", "inputs": ["ghost_name"], "outputs": ["y"]},
    ]
    e6 = dependencies_from_subtasks(subs2)
    assert e6 == [], e6  # ghost_name 无产出 → 不建边 → X,Y 并联
    print("✓ 未满足 input：命名漂移仅告警不误并（混合对齐兜底）")

    # 7) LLM 软依赖覆盖合并（非数据、纯顺序的显式边）
    subs3 = [
        {"id": "A", "capability": "retrieve", "inputs": [], "outputs": ["p"]},
        {"id": "B", "capability": "reason", "inputs": [], "outputs": ["q"]},
    ]
    e7 = dependencies_from_subtasks(subs3, override_deps=[["A", "B"]])
    assert e7 == [["A", "B"]], e7
    print("✓ 软依赖覆盖：LLM 显式 [[A,B]] 合并进结果（确定性，零 LLM 重判）")

    # 8) 命名漂移符号映射：词序不同 china_gdp_2024 ← gdp_china_2024 → 建边 + 映射表
    subs8 = [
        {"id": "A", "capability": "retrieve", "inputs": [], "outputs": ["gdp_china_2024"]},
        {"id": "B", "capability": "reason", "inputs": ["china_gdp_2024"], "outputs": ["analysis"]},
    ]
    e8, m8 = _analyze_dependencies(subs8)
    assert e8 == [["A", "B"]], e8
    assert m8 == {"B": {"china_gdp_2024": "gdp_china_2024"}}, m8
    print("✓ 命名漂移符号映射：china_gdp_2024 ← gdp_china_2024 建边 + 映射表（词序漂移修复）")

    # 9) Levenshtein 近重复（编码/拼写漂移）：gdp_t0tal ← gdp_total → 映射
    subs9 = [
        {"id": "A", "capability": "retrieve", "inputs": [], "outputs": ["gdp_total"]},
        {"id": "B", "capability": "reason", "inputs": ["gdp_t0tal"], "outputs": ["analysis"]},
    ]
    e9, m9 = _analyze_dependencies(subs9)
    assert e9 == [["A", "B"]], e9
    assert m9 == {"B": {"gdp_t0tal": "gdp_total"}}, m9
    print("✓ 命名漂移符号映射：gdp_t0tal ← gdp_total（Levenshtein 近重复）建边 + 映射")

    # 10) 判不出 → 维持现状：gdp_total_typo 与 gdp_total 规则判不出 → 不映射不建边（退并联）
    subs10 = [
        {"id": "A", "capability": "retrieve", "inputs": [], "outputs": ["gdp_total"]},
        {"id": "B", "capability": "reason", "inputs": ["gdp_total_typo"], "outputs": ["y"]},
    ]
    e10, m10 = _analyze_dependencies(subs10)
    assert e10 == [], e10          # 维持现状：不建边 → 并联
    assert m10 == {}, m10          # 无映射
    print("✓ 命名漂移判不出：gdp_total_typo vs gdp_total 维持现状（不误并、退并联，运行期诚实 gate）")

    # 11) 歧义多解 → 维持现状：下游输入同时漂移匹配两个不同上游产物 → 放弃自动映射（防静默误并）
    subs11 = [
        {"id": "A", "capability": "retrieve", "inputs": [], "outputs": ["gdp_total"]},
        {"id": "C", "capability": "retrieve", "inputs": [], "outputs": ["gdptotal"]},
        {"id": "B", "capability": "reason", "inputs": ["gdp_t0tal"], "outputs": ["y"]},
    ]
    e11, m11 = _analyze_dependencies(subs11)
    # B 的 gdp_t0tal 漂移匹配 A(gdp_total) 与 C(gdptotal) 两个候选 → 歧义 → 不映射不建边
    assert e11 == [], e11
    assert m11 == {}, m11
    print("✓ 歧义多解：gdp_t0tal 同时漂移匹配两上游 → 放弃自动映射（防静默误并，维持现状）")

    print("router_auto.selftest OK  (用例1/2/3/4/5/6/7/8/9/10/11 全过)")
    return True


if __name__ == "__main__":
    selftest()
