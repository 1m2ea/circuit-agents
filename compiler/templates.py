"""
circuit-agents · compiler.templates
=================================
M4 增强：常见任务模式的「已知良好」拓扑模板，供 plan.py 按能力签名自动套用，
跳过从头编译、复用经实战验证的布局（含朴素 NL 解析容易漏的增强：补 extract 抽取步、
显式并联依赖、 reliability 保险反馈环等）。

设计（与用户确认）：
 · 匹配是「超集」语义：模板的 match_caps 必须 ⊇ 用户 Goal 的 capabilities，即模板在用户
   意图之上"补强"（多塞 extract / 并联 / 反馈），而非另起炉灶。命中最具体（caps 最多）者。
 · 仅当模板「带来增值」才命中：模板 caps 多于用户，或模板自带 feedback / dependencies。
 · 模态可选约束：match_mods 给出允许模态集合（用户模态必须⊆它）；min_modalities 要求
   用户模态数下限（如 multimodal_summary 要求 ≥2 模态）。
 · 纯数据 + 匹配函数，无副作用；不覆盖 runtime，不动 nl_parser / router。
"""
from __future__ import annotations

from typing import Optional


# 每个模板：
#   name         模板名（[3] 段展示用）
#   match_caps   触发所需能力（用户 capabilities 必须 ⊆ match_caps 才候选）
#   match_mods   允许模态集合（None=不限制；用户模态必须 ⊆ 它）
#   min_modalities 用户模态数下限（默认 0）
#   goal         套用时实际编译用的 Goal 字典（capabilities/dependencies/feedback/tiers 等）
TEMPLATES = [
    {
        # 研究+综述：在"检索→推理→综述"上补 extract 结构化抽取，并把推理/综述串成 DAG，
        # 同时默认接 reliability 保险反馈环（研究类易错，重试闭环划算）。
        "name": "research_report",
        "match_caps": ["retrieve", "reason", "summarize"],
        "match_mods": None,
        "min_modalities": 0,
        "goal": {
            "capabilities": ["retrieve", "extract", "reason", "summarize"],
            "dependencies": [["retrieve", "reason"], ["extract", "reason"],
                             ["reason", "summarize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
    {
        # 核对报告：在"检索→计算→核对"上补 organize 结构化输出，串行 DAG + 反馈环。
        "name": "verify_report",
        "match_caps": ["retrieve", "calculate", "verify"],
        "match_mods": None,
        "min_modalities": 0,
        "goal": {
            "capabilities": ["retrieve", "calculate", "verify", "organize"],
            "dependencies": [["retrieve", "calculate"], ["calculate", "verify"],
                             ["verify", "organize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
    {
        # 多模态综述：需 ≥2 模态；在"检索→综述"上补 extract，retrieve∥extract → summarize。
        "name": "multimodal_summary",
        "match_caps": ["retrieve", "summarize"],
        "match_mods": ["pdf", "image", "table", "text", "audio", "video"],
        "min_modalities": 2,
        "goal": {
            "capabilities": ["retrieve", "extract", "summarize"],
            "dependencies": [["retrieve", "extract"], ["extract", "summarize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
    {
        # 数据分析报告：在"检索→计算→综述"上补 extract 抽取指标，extract→calculate→summarize。
        # 与 verify_report 区分：本模板以"计算+综述"为核心、不强制核对。
        "name": "data_analysis",
        "match_caps": ["retrieve", "calculate", "summarize"],
        "match_mods": None,
        "min_modalities": 0,
        "goal": {
            "capabilities": ["retrieve", "extract", "calculate", "summarize"],
            "dependencies": [["retrieve", "extract"], ["extract", "calculate"],
                             ["calculate", "summarize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
    {
        # 文档审校：在"检索→核对"上补 extract 结构化（原文→结构），extract→verify→organize。
        "name": "document_review",
        "match_caps": ["retrieve", "verify"],
        "match_mods": None,
        "min_modalities": 0,
        "goal": {
            "capabilities": ["retrieve", "extract", "verify", "organize"],
            "dependencies": [["retrieve", "extract"], ["extract", "verify"],
                             ["verify", "organize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
    {
        # 代码审查：检索代码→抽取结构→推理问题→核对→组织修订建议，问题分级 + 修复。
        "name": "code_review",
        "match_caps": ["retrieve", "reason", "verify"],
        "match_mods": None,
        "min_modalities": 0,
        "goal": {
            "capabilities": ["retrieve", "extract", "reason", "verify", "organize"],
            "dependencies": [["retrieve", "extract"], ["extract", "reason"],
                             ["reason", "verify"], ["verify", "organize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
    {
        # 多方案对比：跨多源"检索→抽取→综述"的对比型模板（不含 reason/verify，与
        # research_report 仅以 reason 区分、与 multimodal_summary 仅以模态数区分）。
        # 用于"比较/对比 N 个对象"类目标，按维度生成对比表。
        "name": "comparison",
        "match_caps": ["retrieve", "summarize"],
        "match_mods": None,
        "min_modalities": 0,
        "goal": {
            "capabilities": ["retrieve", "extract", "summarize"],
            "dependencies": [["retrieve", "extract"], ["extract", "summarize"]],
            "feedback": {"max_iter": 3},
            "tiers": {},
        },
    },
]


def match_template(goal) -> Optional[dict]:
    """按双重包含语义匹配最具体的增值模板；无命中返回 None。

    命中条件（goal 为 compiler.goal.Goal 实例）：
      · trigger ⊆ 用户 caps：用户必须含模板的触发能力（否则主题不对）；
      · 用户 caps ⊆ 模板 goal.caps（full）：模板必须覆盖用户全部意图，替换不丢步；
      · 模态 ≤ 模板允许集合，且达到 min_modalities 下限；
      · 必须增值：模板 full 比用户多能力，或自带 feedback / dependencies。
    取 full 能力最多的候选（最具体）。
    """
    caps = set(goal.capabilities)
    mods = set(goal.modalities)
    best = None
    for t in TEMPLATES:
        trigger = set(t["match_caps"])
        full = set(t["goal"]["capabilities"])
        if not trigger <= caps:        # 用户须含触发能力
            continue
        if not caps <= full:           # 模板须覆盖用户全部意图（不丢步）
            continue
        tmods = t.get("match_mods")
        if tmods is not None and not mods <= set(tmods):
            continue
        if len(mods) < t.get("min_modalities", 0):
            continue
        tg = t["goal"]
        adds_value = (len(full) > len(caps)) or tg.get("feedback") or tg.get("dependencies")
        if not adds_value:
            continue
        if best is None or len(full) > len(set(best["goal"]["capabilities"])):
            best = t
    return best


def build_goal_from_template(tpl: dict, user_goal) -> dict:
    """用模板 Goal 覆盖拓扑字段，但保留用户的 description/name/reliability，
    feedback 以用户显式声明优先（用户没说才用模板的）。返回可直接 Goal.from_dict 的字典。"""
    tg = dict(tpl["goal"])
    tg["name"] = user_goal.name
    tg["description"] = user_goal.description
    tg["reliability"] = user_goal.reliability
    if user_goal.feedback:            # 用户显式 feedback 优先
        tg["feedback"] = user_goal.feedback
    if user_goal.constraints:         # 用户约束（延迟/成本/质量）也带上
        tg["constraints"] = dict(user_goal.constraints)
    return tg


if __name__ == "__main__":
    # 轻量自检
    class _G:
        def __init__(self, caps, mods=(), rel="normal", fb=None, cons=None):
            self.capabilities = list(caps)
            self.modalities = list(mods)
            self.reliability = rel
            self.feedback = fb
            self.constraints = cons or {}
            self.name = "t"
            self.description = "t"
    g = _G(["retrieve", "reason", "summarize"])
    m = match_template(g)
    assert m and m["name"] == "research_report", m
    g2 = _G(["retrieve", "calculate", "verify", "organize"])
    assert match_template(g2)["name"] == "verify_report"
    g3 = _G(["retrieve", "summarize"], mods=["pdf", "image"])
    assert match_template(g3)["name"] == "multimodal_summary"
    g4 = _G(["translate"])            # 不应命中任何
    assert match_template(g4) is None
    g5 = _G(["retrieve", "reason"])   # 缺 summarize → 不触发 research_report
    assert match_template(g5) is None
    g6 = _G(["retrieve", "reason", "summarize", "organize"])  # organize 不在 research_report full → 不丢步，故不命中
    assert match_template(g6) is None
    # 新增 4 模板命中校验
    g7 = _G(["retrieve", "calculate", "summarize"])
    assert match_template(g7)["name"] == "data_analysis", match_template(g7)
    g8 = _G(["retrieve", "verify"])
    assert match_template(g8)["name"] == "document_review", match_template(g8)
    g9 = _G(["retrieve", "reason", "verify"])
    assert match_template(g9)["name"] == "code_review", match_template(g9)
    g10 = _G(["retrieve", "summarize"])
    assert match_template(g10)["name"] == "comparison", match_template(g10)
    # comparison 不与 research_report 冲突：用户含 reason 时 research_report 更具体应胜出
    g11 = _G(["retrieve", "reason", "summarize"])
    assert match_template(g11)["name"] == "research_report", match_template(g11)
    print("✓ templates 匹配自检通过（双重包含 + 最具体 + 增值 + 多模态下限 + 不丢步 + 4 新模板）")
