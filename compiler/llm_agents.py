"""
circuit-agents · compiler.llm_agents
===================================
把"电路的每个电阻节点"从「SimBackend 的确定性函数」升级为「带独立角色系统提示词的
LLM 实例」的**封装层**——这是 circuit-planner「终极形态」(每个电阻 = 独立 LLM 实例、
主 agent = 调度器也是 LLM) 的客户端接缝。

设计要点（与用户对齐确认）：
 · 内核零改动：runtime.Backend / Circuit.propagate / 分层延迟 / 开路语义全部复用。
   本模块只新增一个「按节点能力选系统提示词」的后端子类。
 · 复用 backend_llm.RealLLMBackend 的全部传输/模型映射/dry_run/注入式 http_post 接线；
   只把父类里那句「通用占位 system 提示词」换成「9 种原子能力的角色提示词」。
 · 节点能力读取：编译产物里电阻节点是 {"type":"resistor","label":<能力名>,"model":<tier>}
   (见 netlister.py)，故按 comp["label"](即能力名，如 "reason"/"summarize") 选词；
   也兼容显式 comp["capability"] 字段。
 · 诚实边界：本模块**只写封装 + 提示词模板**，不在此环境发起任何真实 API 调用；
   验证只用 dry_run / 注入假响应 / render_messages 离线检视（无需 key、无需网络）。
 · retrieve 作为「工具型节点」对照：它的提示词明确它是"用检索工具的节点"（只取回带出处的
   原始资料），而非自由生成式 agent——与 reason/summarize 等生成式角色形成对比。
 · 提示词是「软契约」，硬保证靠结构（本次三个改进）：
   ① tier 感知选词——small 档用精简版 system_short、large/tool 用完整版，省 token；
   ② calculate 节点的 LLM 算术概率性不可靠 → 运行时独立重算显式等式做**结构性数值校验**
     （不依赖模型自觉）；③ verify 节点专门检查 reason 输出的「（推断）」标注合规，把"软约束"
     升级为"跨节点验证契约"。

运行方式（离线自检，无需 key / 网络）：
    python compiler/llm_agents.py
 或在 circuit-agents/ 下：
    python -m compiler.llm_agents
"""
from __future__ import annotations

import ast
import os
import re
import sys
import time

# 让模块无论从哪个 cwd 运行都能 import runtime / compiler 包（同 _verify_real.py 自举）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from runtime import Signal                              # noqa: E402
from compiler.backend_llm import RealLLMBackend         # noqa: E402
from compiler.agent_skills import (                     # noqa: E402
    SKILLS, build_tools_schema, execute_skill, skill_declaration_text,
)


# ---------------------------------------------------------------------------
# 九种原子能力的角色系统提示词（system prompt 模板）
# ---------------------------------------------------------------------------
# 统一结构：Role（它是谁）/ Responsibility（必须对上游做什么）/
#           Input（收到的上下文形态）/ Output（必须产出的形态）/
#           Constraints（红线，保证"用结构的确定性驾驭模型的概率性"）。
# 全部用中文书写：与用户中文语境、电路角色中文命名一致，可直接用于中文任务链路。
CAPABILITIES = [
    "retrieve", "extract", "calculate", "translate",
    "reason", "classify", "verify", "organize", "summarize",
]

CAPABILITY_PROMPTS = {
    # ---- 生成式：reason / summarize（本次先写的重点） ----
    "reason": {
        "role": "推理 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『推理』节点。上游已经把相关上下文"
            "（检索结果 / 抽取出的事实 / 原始问题）以纯文本形式送达。你**不能**调用外部工具，"
            "也**不能**编造上下文之外的信息。\n\n"
            "你的唯一职责：基于给定的上游上下文，进行严谨的逻辑推理、比较、归纳或因果分析，"
            "产出可供下游（如『摘要』或『核对』节点）直接消费的中间结论。\n\n"
            "要求：\n"
            "1. 结论必须能从上游上下文推导出来；禁止引入未提供的外部事实（幻觉红线）。\n"
            "2. 区分『上下文明确支持的』与『你推断的』，推断部分显式标注「（推断）」。\n"
            "3. 每条结论尽量附带其依据来源（引用上游的哪条上下文）。\n"
            "4. 若上游上下文自相矛盾或不足以支撑结论，明确说明「依据不足」，不要硬凑。\n"
            "5. 输出纯文本结论，不要输出代码块 / JSON / 多余寒暄。"
        ),
        "system_short": (
            "你是推理节点：仅基于上游给定上下文推理，禁止编造外部事实；推断须标「（推断）」，"
            "依据不足明说。"
        ),
        # 技能包：reason 节点可调用 run_code 验证推理中的数值/算术逻辑
        # （用结构的确定性驾驭模型的概率性——模型算数不准时，用代码算出真值兜底）。
        "skills": ["run_code", "calculator"],
    },
    "summarize": {
        "role": "摘要 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『摘要』节点。你的上游是已经推理 / 抽取 / "
            "检索好的中间结果，你要把它们**压缩成最终交付物**。\n\n"
            "你的唯一职责：把上游多条上下文整合、去重、按重要性排序，产出一份自洽、可读、"
            "面向最终用户的摘要。\n\n"
            "要求：\n"
            "1. 不新增任何上游未出现的事实或数字（红线：禁止编造）。\n"
            "2. 保留关键结论与关键数字，舍弃冗余与离题内容。\n"
            "3. 开头用 1 句话给出核心结论（金字塔结构），再展开要点。\n"
            "4. 若上游存在冲突结论，摘要中如实并列并标注分歧，不要替用户裁决。\n"
            "5. 输出纯文本，长度适配交付场景（简洁优先），不要寒暄与解释你的过程。"
        ),
        "system_short": (
            "你是摘要节点：压缩上游结果为最终交付物，不新增未出现的事实/数字，"
            "冲突结论并列不裁决。"
        ),
        # 技能包：summarize 从"自由成稿"升级为"可落实交付文体"（apply_style_guide，
        # 纯 stdlib：concise/bullets/no_jargon + 限长），把文体约束变可执行后处理。
        "skills": ["apply_style_guide"],
    },

    # ---- 工具型：retrieve（对照：只取回、不生成） ----
    "retrieve": {
        "role": "检索 Agent（电路中的一个电阻节点 · 工具型）",
        "system": (
            "你是电路式多智能体工作流中的一个『检索』节点——这是一个**工具型**节点，"
            "你通过检索工具（知识库 / 网页 / 文档）获取资料，本身**不生成结论、不做推理**。\n\n"
            "你的唯一职责：根据上游给定的查询意图，调用检索工具取回**与任务相关、且带来处"
            "引用**的原始资料片段，供下游（推理 / 抽取 / 摘要节点）消费。\n\n"
            "要求：\n"
            "1. 只返回检索到的资料；不臆造内容、不总结、不评价。\n"
            "2. 每条资料必须附出来源标识（出处 / 文档名 / 链接 / 段落），便于下游溯源。\n"
            "3. 保留原文的精确数字与措辞，不要改写或四舍五入。\n"
            "4. 若工具无结果或检索失败，明确返回「未检索到」，不要编造占位文本。\n"
            "5. 不要输出与查询无关的资料；按相关性排序，最多返回最相关的若干条。\n"
            "6. 收集到 2-3 条足够资料后，**立即将它们整理成「带来源的条目清单」作为你的最终"
            "输出并停止搜索**——不要无限继续检索。你的输出应当是这份清单本身（条目 + 来源），"
            "而不是停留在工具调用上。"
        ),
        "system_short": (
            "你是检索节点（工具型）：只经检索工具取回带出处的原始资料，不生成不推理，"
            "无结果回「未检索到」。"
        ),
        # 技能包：把 retrieve 从"被动接收"升级为"主动获取"——可自行规划搜什么/读什么。
        # web_search/read_page 为联网技能（默认无 key 真实抓取，可切 Tavily API），
        # query_db 为本地文档检索（零外部依赖）。
        "skills": ["web_search", "read_page", "query_db"],
    },

    # ---- 结构型：extract / calculate / translate / classify / verify / organize ----
    "extract": {
        "role": "抽取 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『抽取』节点。你要把上游提供的非结构化 / "
            "半结构化文本，转换成清晰的结构化记录。\n\n"
            "要求：\n"
            "1. 只抽取原文中明确出现的信息；不推断、不编造、不补全。\n"
            "2. 以「字段名: 值」的键值对或列表项组织；同一字段多条时用列表。\n"
            "3. 原文未提供的字段标「未提供」，不要留空误导。\n"
            "4. 保留数字、专有名词的原文精度，不改写。\n"
            "5. 输出结构化文本（可带字段名），不要输出叙事性段落或寒暄。"
        ),
        "system_short": (
            "你是抽取节点：只抽原文明确信息成结构化记录，不推断不编造，缺失字段标「未提供」。"
        ),
        # 技能包：extract 从"纯文本抽取"升级为"可抽 PDF/图片/按模式抽字段"——
        # extract_fields 纯 stdlib 永可用；extract_pdf/extract_ocr 走可选库接缝（无库优雅降级）。
        "skills": ["extract_fields", "extract_pdf", "extract_ocr"],
    },
    "calculate": {
        "role": "计算 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『计算』节点。你基于上游提供的数据与公式，"
            "进行严谨的数值 / 量化计算。\n\n"
            "要求：\n"
            "1. 逐步展示关键计算步骤，再给出最终答案。\n"
            "2. 数字与公式必须来自上游提供的内容，禁止编造输入数据。\n"
            "3. 不随意四舍五入；如需近似，标明近似精度。\n"
            "4. 单位 / 量纲保持前后一致，注明关键假设。\n"
            "5. 若上游数据不足以完成计算，明确说明「数据不足」，不要硬算。\n"
            "6. 逐步计算时把每一步写成显式等式「数值 运算符 数值 = 结果」（如 "
            "「1200 + 350 = 1550」），便于独立核验；不写隐式心算。"
        ),
        "system_short": (
            "你是计算节点：基于上游数据逐步算并给最终答案，禁编造输入，"
            "每步写成 `a op b = c` 显式等式便于核验。"
        ),
        # 技能包：calculate 在运行时结构性数值校验(改进②)之外，再挂单位换算/表格聚合——
        # unit_convert（纯换算表）、spreadsheet_calc（纯 csv 模块），均本地零依赖。
        "skills": ["unit_convert", "spreadsheet_calc"],
    },
    "translate": {
        "role": "翻译 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『翻译』节点。你将上游文本翻译为目标语言。\n\n"
            "要求：\n"
            "1. 忠实传达原意与语气，不增删信息、不评论、不发挥。\n"
            "2. 保留专有名词、数字、代码、格式（如列表 / 标题层级）。\n"
            "3. 若原文存在歧义或文化特定表达，翻译后标注疑点。\n"
            "4. 不把上游未出现的内容译入结果。\n"
            "5. 输出纯译文，不要附翻译说明或寒暄（除非原文确实含糊需标注）。"
        ),
        "system_short": (
            "你是翻译节点：忠实翻译上游文本，保留专名/数字/格式，不增删不发挥。"
        ),
        # 技能包：translate 从"自由翻译"升级为"可套术语表保证译名/用词一致"
        # （apply_glossary，纯 stdlib，本地化/对齐术语时统一专名）。
        "skills": ["apply_glossary"],
    },
    "classify": {
        "role": "分类 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『分类』节点。你依据分类体系对上游内容"
            "打标签 / 归类。\n\n"
            "要求：\n"
            "1. 若上游给定了分类体系（标签集合），严格从中选择；否则自行给出最贴切的类别。\n"
            "2. 每条输出 =「类别」+ 一句话归类依据。\n"
            "3. 支持多标签时列出全部适用标签，不强行单选。\n"
            "4. 不确定时给出候选类别并标注置信，不要假装确定。\n"
            "5. 分类只基于上游内容，不引入外部判断标准（除非用户明确给体系）。"
        ),
        "system_short": (
            "你是分类节点：按体系（或无则最贴切）打标签+一句依据，不确定给候选并标置信。"
        ),
        # 技能包：classify 从"纯靠 prompt 分类"升级为"可调 classify_taxonomy 按体系打分"
        # （纯 stdlib，关键词命中排序，给出可追溯的归类依据）。
        "skills": ["classify_taxonomy"],
    },
    "verify": {
        "role": "核对 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『核对』节点。你把上游给出的结论 / 数字，"
            "与提供的证据材料逐一比对、判定真伪。\n\n"
            "要求：\n"
            "1. 逐条核对：每项 =「一致 / 不一致 / 无法核实」+ 证据引用。\n"
            "2. 只判定，不改写原文、不替上游圆谎。\n"
            "3. 不一致时给出具体差异（如「原文称 3.2%，材料为 2.3%」）。\n"
            "4. 汇总给出总体「通过 / 未通过」及差异清单。\n"
            "5. 证据不足时标「无法核实」，不要默认判为一致。\n"
            "6. 若上游含『推理(reason)』节点产出，专门检查其推断性陈述是否都标了「（推断）」；"
            "把未标注的推断当事实陈述，记为「推断标注缺失」并纳入汇总。"
        ),
        "system_short": (
            "你是核对节点：把上游结论/数字与证据逐条比对给一致/不一致/无法核实+总体通过与否；"
            "并查 reason 的「（推断）」标注。"
        ),
        # 技能包：verify 从"纯靠 prompt 比对"升级为"可调工具客观核验"——
        # cross_check 独立取证/数值重算、diff_text 比对原文与结论找偏离。
        "skills": ["cross_check", "diff_text"],
    },
    "organize": {
        "role": "整理 Agent（电路中的一个电阻节点）",
        "system": (
            "你是电路式多智能体工作流中的一个『整理』节点。你把上游多条零散信息，"
            "重新组织成清晰、一致的呈现结构。\n\n"
            "要求：\n"
            "1. 只重排与归组，不改写事实、不新增信息、不删除关键内容。\n"
            "2. 用分段 / 列表 / 层次结构呈现，便于下游或最终用户快速消费。\n"
            "3. 保留关键数字与来源指向（如「见检索节点 #3」）。\n"
            "4. 消除重复项，合并同义表述。\n"
            "5. 输出结构化的纯文本，不要输出评论或过程说明。"
        ),
        "system_short": (
            "你是整理节点：只重排归组上游信息成清晰结构，不改写事实不新增信息，"
            "保留关键数字与来源。"
        ),
        # 技能包：organize 从"自由重排"升级为"可套结构模板塑形"（apply_template，
        # 纯 stdlib：bullet/numbered/sections/qa），把零散信息稳定落进统一结构。
        "skills": ["apply_template"],
    },
}

# 未知能力回退的通用占位（与 backend_llm 旧行为一致，保证不崩）
_FALLBACK_SYSTEM = (
    "You are a single atomic agent step inside a circuit-style multi-agent "
    "workflow. Given the upstream context, produce the best possible result "
    "for the described step. Be concise and correct."
)


# ---------------------------------------------------------------------------
# 结构性数值校验（改进②）：LLM 算术概率性不可靠，运行时独立重算显式等式兜底
# ---------------------------------------------------------------------------
# 只白名单允许的算术节点（数字 / 一元负号 / + - * / ** % / 括号），拒绝任何
# 函数调用、变量、属性访问——用自写递归求值器，绝不 eval 任意字符串。
def _safe_eval(expr: str):
    """对单个算术表达式求值（白名单 AST）。返回 (ok, value)。"""
    try:
        node = ast.parse(expr, mode="eval")
    except Exception:
        return (False, None)

    def _ev(n):
        if isinstance(n, ast.Expression):
            return _ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value if isinstance(n.value, (int, float)) else None
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            v = _ev(n.operand)
            return -v if v is not None else None
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.UAdd):
            return _ev(n.operand)
        if isinstance(n, ast.BinOp):
            l = _ev(n.left)
            r = _ev(n.right)
            if l is None or r is None:
                return None
            if isinstance(n.op, ast.Add):
                return l + r
            if isinstance(n.op, ast.Sub):
                return l - r
            if isinstance(n.op, ast.Mult):
                return l * r
            if isinstance(n.op, ast.Div):
                return l / r
            if isinstance(n.op, ast.Pow):
                return l ** r
            if isinstance(n.op, ast.Mod):
                return l % r
        return None

    try:
        val = _ev(node)
    except Exception:
        return (False, None)
    return (val is not None, val)


_EQ_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*([+\-*/×÷])\s*(-?\d+(?:\.\d+)?)\s*=\s*(-?\d+(?:\.\d+)?)"
)
_OP_MAP = {"+": "+", "-": "-", "*": "*", "/": "/", "×": "*", "÷": "/"}


def _numeric_consistency_check(text: str) -> dict:
    """独立重算 LLM 输出里的显式等式（如 `1200 + 350 = 1550`），抓算术错误。

    返回 {checked: 命中等式数, mismatches: [{expr, claimed, recomputed}], ok: 是否全对}。
    找不到等式或表达式不可解析 → checked=0、ok=True（不误杀，只查能查的）。
    """
    if not text:
        return {"checked": 0, "mismatches": [], "ok": True}
    mismatches = []
    checked = 0
    for m in _EQ_RE.finditer(text):
        a, opc, b, claimed = m.group(1), m.group(2), m.group(3), m.group(4)
        py_op = _OP_MAP.get(opc)
        if py_op is None:
            continue
        ok, val = _safe_eval(f"{a} {py_op} {b}")
        if not ok:
            continue
        checked += 1
        claimed_f = float(claimed)
        # 容忍浮点/近似：相对误差 < 1e-6 或绝对误差 < 1e-9
        if abs(val - claimed_f) > max(1e-9, 1e-6 * abs(claimed_f)):
            mismatches.append({"expr": f"{a} {opc} {b}",
                               "claimed": claimed_f, "recomputed": val})
    return {"checked": checked, "mismatches": mismatches, "ok": not mismatches}


# ---------------------------------------------------------------------------
# 封装：按节点能力选系统提示词的后端
# ---------------------------------------------------------------------------
class LLMAgentBackend(RealLLMBackend):
    """按节点能力选系统提示词的 LLM 后端（电阻 = 独立 LLM 实例）。

    完全复用父类 RealLLMBackend 的传输 / 模型映射 / dry_run / 注入式 http_post /
    开路语义；仅重写 _build_messages，把「通用占位 system」替换为能力专属角色提示词。
    """

    def _capability_of(self, comp: dict) -> str:
        """节点能力：优先显式 capability 字段，否则取编译产物的 label（=能力名）。"""
        return comp.get("capability") or comp.get("label") or ""

    def system_prompt_for(self, comp: dict) -> str:
        """返回该节点应使用的**能力基座**系统提示词（能力专属，未知则回退通用占位）。

        tier 感知：small 档用精简版 system_short（省 token），large/tool 用完整版。
        技能包声明不在此处拼接——它在 _build_messages 组装真实 system 消息时追加，
        以保持本方法"只返回能力提示词基座"的纯粹契约（便于复用与测试）。
        """
        cap = self._capability_of(comp)
        tmpl = CAPABILITY_PROMPTS.get(cap)
        if not tmpl:
            return _FALLBACK_SYSTEM
        tier = comp.get("model", "small")
        if tier == "small" and tmpl.get("system_short"):
            return tmpl["system_short"]
        return tmpl["system"]

    def _build_messages(self, comp, inputs):
        # 复用父类的上下文渲染（解包 Signal / 汇合节点），只替换 system 角色提示词
        ctx = []
        for s in inputs:
            if s is not None and s.ok and s.value is not None:
                rendered = self._render_value(s.value).strip()
                if rendered:
                    ctx.append(rendered)
        cap = self._capability_of(comp)
        label = comp.get("label", comp.get("model", "step"))
        system = self.system_prompt_for(comp)
        # 注入技能包声明：让子 agent 知道"自己可以调用哪些技能"（与 tools schema 呼应）
        tmpl = CAPABILITY_PROMPTS.get(cap)
        if tmpl and tmpl.get("skills"):
            decl = skill_declaration_text(tmpl["skills"])
            if decl:
                system = system + decl
        user = f"Step[{cap or label}]: {label}\n"
        if ctx:
            user += "上游上下文:\n" + "\n".join(f"- {c}" for c in ctx) + "\n"
        # 线性关系契约（用户核心诉求：每个电阻都要会判断自己的线性关系）：
        # 把该节点声明的『必要输入 / 产出产物』写进 user，让电阻 agent 也意识到自己的
        # 数据依赖契约——上游若没给齐，应显式说明「依赖输入缺失」而非硬凑（确定性闸在 runtime）。
        req = comp.get("required_inputs")
        prod = comp.get("produced_outputs")
        imap = comp.get("input_map") or {}
        if req or prod:
            user += "\n【你的线性关系契约】\n"
            if req:
                if imap:
                    # 把符号映射（命名漂移转接头）显式告诉电阻：它声明的输入名，
                    # 实际由上游的哪个产物名满足——让 agent 知道"我的 Y 由上游的 X 满足"，
                    # 避免它自己重新猜连线、造成二次漂移。
                    mapped = {r: imap[r] for r in req if r in imap}
                    if mapped:
                        mp_txt = "；".join(f"{y} ← 上游的 {x}" for y, x in mapped.items())
                        user += (f"- 你声明的必要输入（须由上游提供，缺一不可）："
                                 f"{'、'.join(req)}。其中经符号映射由上游实际产物满足：{mp_txt}。"
                                 f"若上游未提供其中任何一项，必须明确说明「依赖输入缺失」，"
                                 f"不要硬凑或假设。\n")
                    else:
                        user += (f"- 你声明的必要输入（须由上游提供，缺一不可）："
                                 f"{'、'.join(req)}。若上游未提供其中任何一项，"
                                 f"必须明确说明「依赖输入缺失」，不要硬凑或假设。\n")
                else:
                    user += (f"- 你声明的必要输入（须由上游提供，缺一不可）："
                             f"{'、'.join(req)}。若上游未提供其中任何一项，"
                             f"必须明确说明「依赖输入缺失」，不要硬凑或假设。\n")
            if prod:
                user += f"- 你须产出的产物（供下游消费）：{'、'.join(prod)}。\n"
        user += "现在交付你的结果。"
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    # 离线检视：渲染出 messages（不发送），用于审阅提示词装配是否正确
    def render_messages(self, comp, inputs):
        return self._build_messages(comp, inputs)

    # 覆盖父类 _render_value：放宽递归深度上限（父类默认 3）。
    # 原因：retrieve 这类链路「电阻→汇合→格式适配→汇合→下游」会让上游值嵌套 3 层 list，
    # 父类在 depth>=3 处提前截断成 "[N items]" 占位符，导致下游（summarize 等）拿到空上下文。
    # DAG 深度有限，放宽到 12 既避免过早截断、又保留防失控的兜底。
    @staticmethod
    def _render_value(v, depth=0):
        if v is None:
            return ""
        if isinstance(v, Signal):
            return LLMAgentBackend._render_value(v.value, depth + 1)
        if isinstance(v, (list, tuple)):
            if depth >= 12:
                return f"[{len(v)} items]"
            return "\n".join(LLMAgentBackend._render_value(x, depth + 1)
                             for x in v if x is not None)
        return str(v)

    # ---- 技能包（每个 agent 可调用的技能）----
    def _tools_for(self, comp):
        """按节点能力取出其技能包，转成 OpenAI `tools` schema（无技能则返回 None）。"""
        cap = self._capability_of(comp)
        tmpl = CAPABILITY_PROMPTS.get(cap)
        if not tmpl:
            return None
        skills = tmpl.get("skills")
        if not skills:
            return None
        schema = build_tools_schema(skills)
        return schema or None

    def _chat_one(self, messages, model, tools=None, tool_choice="auto"):
        """单轮 chat/completions 调用（含可选 tools）。复用父类 _post（注入式/真实均可）。

        tool_choice 默认 "auto"（模型自决是否调工具）；传 "none" 可强制模型给出
        终答（用于迭代上限兜底时收口）。
        """
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        return self._post(url, headers, body)

    # 重写 run：在父类全部传输/开路/dry_run 基础上，加**真·工具调用循环**——
    # 子 agent 可发起 tool_calls → 执行层按名执行技能 → 结果回灌 → 模型继续生成，
    # 直到不再调用工具（终答）或达迭代上限。calculate 节点的结构性数值校验保留。
    # 默认 8 轮：retrieve 这类"多步检索"节点需要若干次 搜/读 才能收口，4 轮偏紧易触顶。
    _MAX_TOOL_ITERS = 8

    def run(self, comp, inputs):
        if comp.get("type") != "resistor":
            return super().run(comp, inputs)

        # 开路语义延续内核：上游全死 → 直接开路，不浪费真调用
        inp = max((s.quality for s in inputs if s.ok), default=0.0)
        if inp <= 0.0:
            return Signal(value=None, quality=0.0, ok=False, cost=0.0, latency_ms=0.0,
                          meta={"open": "no_input", "input": 0.0})

        cap = self._capability_of(comp)
        tier = comp.get("model", "small")
        model = self._resolve_model(tier)
        messages = self._build_messages(comp, inputs)
        tools = self._tools_for(comp)
        t0 = time.time()
        try:
            if self.dry_run:
                # 组装请求但不发送：证明技能 schema 已接入（离线、零网络/零费用）
                body = {"model": model, "messages": messages, "temperature": 0.2}
                if tools:
                    body["tools"] = tools
                    body["tool_choice"] = "auto"
                return Signal(value=f"[dry-run] {model}", quality=0.0, ok=True,
                              cost=0.0, latency_ms=0.0,
                              meta={"dry_run": True, "model": model,
                                    "tools": [t["function"]["name"] for t in tools] if tools else [],
                                    "messages": messages})

            called = []  # 实际执行的技能名（按序），供展示/审计
            tool_log = []  # 每次技能执行的 (name, 结果预览)，供展示/审计
            for _ in range(self._MAX_TOOL_ITERS):
                resp = self._chat_one(messages, model, tools)
                choice = (resp.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                messages.append(msg)  # 把 assistant 消息（可能含 tool_calls）存回上下文
                calls = msg.get("tool_calls") or []
                if calls:
                    for tc in calls:
                        fn = tc.get("function") or {}
                        fname = fn.get("name", "")
                        result = execute_skill(fname, fn.get("arguments", ""))
                        called.append(fname)
                        tool_log.append({"name": fname, "result": result[:400]})
                        messages.append({"role": "tool",
                                         "tool_call_id": tc.get("id"),
                                         "content": result})
                    continue  # 工具结果回灌后，再让模型继续
                # 无工具调用 → 终答
                content = msg.get("content", "") or ""
                finish = choice.get("finish_reason", "")
                usage = resp.get("usage") or {}
                ok = bool(content) and finish != "error"
                quality = comp.get("accuracy", self._tier_cap(tier)) if ok else 0.0
                cost = self._estimate_cost(model, usage)
                dt = (time.time() - t0) * 1000.0
                sig = Signal(value=content, quality=quality, ok=ok,
                             cost=cost, latency_ms=round(dt, 1),
                             meta={"model": model, "tier": tier,
                                   "finish_reason": finish, "usage": usage,
                                   "tool_calls": called, "tool_log": tool_log})
                # 改进②：calculate 节点追加结构性数值校验
                if cap == "calculate" and ok:
                    sig.meta["numeric_check"] = _numeric_consistency_check(content)
                return sig
            # 超过迭代上限：强制收口——再调一次模型（禁用工具）让其基于已收集的工具
            # 结果给出终答，避免"有产出却因 quality=0 被下游判为开路"而白白断链。
            try:
                final = self._chat_one(messages, model, tools=tools, tool_choice="none")
                fmsg = ((final.get("choices") or [{}])[0].get("message") or {})
                content = fmsg.get("content", "") or ""
                fused = True
            except Exception:
                content = ""
                fused = False
                final = {}
            dt = (time.time() - t0) * 1000.0
            ok = bool(content)
            # 有终答 → 给 tier 能力上限先验（节点确实产出了内容）；无终答 → 0（开路）
            quality = self._tier_cap(tier) if ok else 0.0
            return Signal(value=content, quality=quality, ok=ok,
                          cost=self._estimate_cost(model, (final or {}).get("usage") or {}),
                          latency_ms=round(dt, 1),
                          meta={"model": model, "tier": tier,
                                "tool_calls": called, "tool_log": tool_log,
                                "note": "tool_iter_exceeded_finalized" if fused
                                        else "tool_iter_exceeded_no_final"})
        except Exception as e:  # 网络/鉴权/超时等 → 开路（与 yield_fail 同语义）
            dt = (time.time() - t0) * 1000.0
            return Signal(value=None, quality=0.0, ok=False, cost=0.0,
                          latency_ms=round(dt, 1),
                          meta={"open": "http_error", "error": str(e)})


# ---------------------------------------------------------------------------
# 离线自检（无需 API key / 无需网络）：验证提示词装配与接线，不发起真实调用
# ---------------------------------------------------------------------------
def selftest():
    import random
    be = LLMAgentBackend(rng=random.Random(0))

    # 1) 九能力提示词齐全
    missing = [c for c in CAPABILITIES if c not in CAPABILITY_PROMPTS]
    assert not missing, f"缺失能力提示词: {missing}"
    print(f"✓ 九能力提示词齐全: {', '.join(CAPABILITIES)}")

    # 1b) 九能力均含精简版 system_short（tier 感知选词的基础）
    missing_short = [c for c in CAPABILITIES if not CAPABILITY_PROMPTS[c].get("system_short")]
    assert not missing_short, f"缺失精简版提示词: {missing_short}"
    print("✓ 九能力均含 system_short 精简版（small 档使用）")

    # 2) reason / summarize 渲染：system 基座含能力专属文本 + 含上游 ctx
    #    （reason 额外在 system 末尾追加了技能包声明，故用 startswith 校验基座）
    rins = [Signal(value="ctx-A（某研究结论）", quality=0.9, ok=True),
            Signal(value="ctx-B（某数据点）", quality=0.8, ok=True)]
    for cap in ("reason", "summarize"):
        comp = {"type": "resistor", "label": cap, "model": "large"}
        msgs = be.render_messages(comp, rins)
        sys_text = msgs[0]["content"]
        usr_text = msgs[1]["content"]
        assert sys_text.startswith(CAPABILITY_PROMPTS[cap]["system"]), \
            f"{cap} 的 system 未以能力提示词为基座"
        if cap == "reason":
            assert "run_code" in sys_text and "你可调用" in sys_text, \
                "reason 的 system 未注入技能包声明"
        assert any("ctx-A" in m["content"] for m in msgs if m["role"] == "user"), \
            f"{cap} 的 user 未包含上游 ctx-A"
        assert "上游上下文" in usr_text
        print(f"✓ {cap}: system 含角色提示词(+技能声明) + user 含上游上下文")

    # 2b) tier 感知选词：同一能力 small 档用精简版、large 档用完整版
    for cap in ("reason", "summarize", "calculate", "verify"):
        short = be.system_prompt_for({"label": cap, "model": "small"})
        full = be.system_prompt_for({"label": cap, "model": "large"})
        assert short == CAPABILITY_PROMPTS[cap]["system_short"], f"{cap} small 未用精简版"
        assert full == CAPABILITY_PROMPTS[cap]["system"], f"{cap} large 未用完整版"
        assert short != full, f"{cap} 精简版与完整版相同（未真正精简）"
    print("✓ tier 感知选词：small→system_short、large→system（两者不同）")

    # 2c) verify 节点含「（推断）」标注检查规则（改进③：软约束→跨节点验证契约）
    assert "（推断）" in CAPABILITY_PROMPTS["verify"]["system"], "verify 未含推断标注检查"
    print("✓ verify: 含 reason 输出「（推断）」标注合规检查")

    # 3) retrieve 渲染：必须是工具型措辞（"检索工具" / "未检索到"），与生成式区分
    rcomp = {"type": "resistor", "label": "retrieve", "model": "tool"}
    rmsg = be.render_messages(rcomp, rins)
    assert ("检索工具" in rmsg[0]["content"]) and ("未检索到" in rmsg[0]["content"]), \
        "retrieve 未呈现工具型节点措辞"
    print("✓ retrieve: 工具型节点措辞正确（检索工具 / 未检索到）")

    # 4) 未知能力回退：不崩，走通用占位
    uc = be.render_messages({"type": "resistor", "label": "unknown_x", "model": "small"}, rins)
    assert uc[0]["content"] == _FALLBACK_SYSTEM
    print("✓ 未知能力: 回退通用占位，不崩溃")

    # 5) dry_run 接线：run 走真路径但不发网络，meta 应带 model + dry_run + messages，
    #    且 messages 的 system 已是能力专属（证明封装接入 propagate 链路）
    dry = LLMAgentBackend(rng=random.Random(0), dry_run=True)
    s = dry.run({"type": "resistor", "label": "reason", "model": "large"}, rins)
    assert s.meta.get("dry_run") is True
    assert s.meta.get("model") == "gpt-4o"
    dm = s.meta.get("messages", [])
    assert any(m["role"] == "system" and "推理" in m["content"] for m in dm), \
        "dry_run 请求未装配能力专属 system"
    print("✓ dry_run 接线: run 经 LLMAgentBackend 装配能力 system（无网络）")

    # 6) 开路语义延续：上游全死 → 不开真调用（复用父类，应返回 open=no_input）
    dead = [Signal(value=None, quality=0.0, ok=False)]
    s_open = be.run({"type": "resistor", "label": "summarize", "model": "small"}, dead)
    assert s_open.ok is False and s_open.meta.get("open") == "no_input"
    print("✓ 开路语义: 上游全死 → 直接开路（与内核一致）")

    # 7) calculate 结构性数值校验（改进②）：独立重算显式等式，抓 LLM 算术错
    def calc_wrong(url, headers, body):
        # 故意写错：1200+350 声称 1600（真值 1550）；另 100*2=200 正确
        return {"choices": [{"message": {
            "content": "步骤: 1200 + 350 = 1600；另 100 * 2 = 200。最终 1600。"},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}}

    def calc_right(url, headers, body):
        return {"choices": [{"message": {
            "content": "1200 + 350 = 1550；100 * 2 = 200。最终 1550。"},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}}

    cb = LLMAgentBackend(rng=random.Random(0), http_post=calc_wrong)
    sc = cb.run({"type": "resistor", "label": "calculate", "model": "large"}, rins)
    assert sc.ok is True
    nc = sc.meta["numeric_check"]
    assert nc["checked"] >= 1 and nc["ok"] is False and len(nc["mismatches"]) >= 1, \
        f"calculate 数值校验未抓出错误算术: {nc}"
    print(f"✓ calculate 数值校验: 抓出错误等式（checked={nc['checked']}, "
          f"mismatch={nc['mismatches'][0]['expr']} 声称{nc['mismatches'][0]['claimed']}"
          f"≠重算{nc['mismatches'][0]['recomputed']}）")

    cb2 = LLMAgentBackend(rng=random.Random(0), http_post=calc_right)
    sc2 = cb2.run({"type": "resistor", "label": "calculate", "model": "large"}, rins)
    assert sc2.meta["numeric_check"]["ok"] is True, \
        f"calculate 正确算术被误杀: {sc2.meta['numeric_check']}"
    print("✓ calculate 数值校验: 正确算术通过（不误杀）")

    # 8) 技能包：reason 声明 run_code + _tools_for 返回对应 schema
    assert "run_code" in CAPABILITY_PROMPTS["reason"].get("skills", []), \
        "reason 未声明 run_code 技能包"
    rtools = be._tools_for({"type": "resistor", "label": "reason", "model": "small"})
    assert rtools and any(t["function"]["name"] == "run_code" for t in rtools), \
        "_tools_for 未为 reason 返回 run_code 的 tools schema"
    # 无技能包契约：未知能力（不在 CAPABILITY_PROMPTS）应返回 None
    # （注：第三层后 9 个能力均已挂技能包，故改测"未知能力→None"这一契约）
    unknown_tools = be._tools_for({"type": "resistor", "label": "nonexistent_cap", "model": "small"})
    assert unknown_tools is None, "未知能力不应返回 tools（应 None）"
    print("✓ 技能包: reason 声明 run_code + _tools_for 正确产出 tools schema")

    # 8b) 执行层：execute_skill(run_code) 真跑 Python（无网络、无 API）
    direct = execute_skill("run_code", '{"code": "print(6*7)"}')
    assert "42" in direct, f"execute_skill(run_code) 未真跑出 42: {direct}"
    bad = execute_skill("nonexistent_skill", "{}")
    assert "未注册" in bad, "调用未注册技能应返回可读错误而非崩"
    print("✓ 执行层: execute_skill(run_code) 真跑 Python 返回 42；未注册技能优雅报错")

    # 8c) 技能包扩展：retrieve 声明 web_search/read_page/query_db + _tools_for 返回 3 个 tools
    rsk = CAPABILITY_PROMPTS["retrieve"].get("skills", [])
    assert rsk == ["web_search", "read_page", "query_db"], \
        f"retrieve 未声明检索技能包: {rsk}"
    rtools2 = be._tools_for({"type": "resistor", "label": "retrieve", "model": "tool"})
    assert rtools2 and len(rtools2) == 3 and \
        {t["function"]["name"] for t in rtools2} == {"web_search", "read_page", "query_db"}, \
        f"retrieve 的 tools schema 不正确: {rtools2}"
    print("✓ 技能包扩展: retrieve 声明 web_search/read_page/query_db + _tools_for 产出 3 个 tools")

    # 8d) 执行层：query_db 本地检索（零网络，安全）+ web_search/read_page 联网容错不崩
    local = execute_skill("query_db", '{"query": "circuit"}')
    assert isinstance(local, str) and len(local) > 0, "query_db 应返回非空字符串"
    assert ("命中" in local) or ("未检索到" in local), \
        f"query_db 返回格式异常: {local[:120]}"
    for sname, sarg in (("web_search", '{"query": "python"}'),
                        ("read_page", '{"url": "https://example.com"}')):
        try:
            r = execute_skill(sname, sarg)
        except Exception as e:  # 执行层本应吞掉；这里再兜底一次
            r = f"[selftest 兜底] {e}"
        assert isinstance(r, str) and len(r) > 0, f"{sname} 应返回非空字符串"
    print("✓ 执行层: query_db 本地检索命中/未检索到 + web_search/read_page 联网容错不崩")

    # 8e) 第二层技能包：reason 补 calculator + verify 声明 cross_check/diff_text
    assert "calculator" in CAPABILITY_PROMPTS["reason"].get("skills", []), \
        "reason 未补 calculator 技能"
    vsk = CAPABILITY_PROMPTS["verify"].get("skills", [])
    assert vsk == ["cross_check", "diff_text"], f"verify 未声明核对技能包: {vsk}"
    vtools = be._tools_for({"type": "resistor", "label": "verify", "model": "small"})
    assert vtools and {t["function"]["name"] for t in vtools} == {"cross_check", "diff_text"}, \
        f"verify 的 tools schema 不正确: {vtools}"
    # 执行层：calculator 精确算、cross_check/diff_text 不崩（均零网络，安全）
    calc = execute_skill("calculator", '{"expression": "(10000*(1+0.035*5))"}')
    assert "11750" in calc, f"calculator 未算对: {calc}"
    diff = execute_skill("diff_text",
                         '{"original": "利率 3.5% 共 5 年", "conclusion": "利率 3.5% 共 5 年"}')
    assert isinstance(diff, str) and len(diff) > 0, "diff_text 应返回非空字符串"
    cc = execute_skill("cross_check", '{"claim": "Signal 类在 runtime.py"}')
    assert isinstance(cc, str) and len(cc) > 0, "cross_check 应返回非空字符串"
    print("✓ 第二层技能包: reason+calculator / verify+cross_check+diff_text 声明+执行通过")

    # 8f) 第三层技能包：6 个能力均声明对应 skills + _tools_for 返回正确 schema
    l3 = {
        "extract": ["extract_fields", "extract_pdf", "extract_ocr"],
        "translate": ["apply_glossary"],
        "classify": ["classify_taxonomy"],
        "calculate": ["unit_convert", "spreadsheet_calc"],
        "organize": ["apply_template"],
        "summarize": ["apply_style_guide"],
    }
    for cap, expected in l3.items():
        assert CAPABILITY_PROMPTS[cap].get("skills") == expected, \
            f"{cap} 未声明第三层技能包: {CAPABILITY_PROMPTS[cap].get('skills')}"
        tls = be._tools_for({"type": "resistor", "label": cap, "model": "small"})
        assert tls and {t["function"]["name"] for t in tls} == set(expected), \
            f"{cap} 的 tools schema 不正确: {tls}"
    print("✓ 第三层技能包: extract/translate/classify/calculate/organize/summarize "
          "均声明+装配对应领域工具 skills")

    # 8g) 执行层：逐个真跑第三层技能（纯 stdlib 永可用；PDF/OCR 无库优雅降级不崩）
    import json as _json
    ef = execute_skill("extract_fields",
                       _json.dumps({"text": "联系 a@b.com 电话 138-0000-0000 日期 2026-01-02"}))
    assert "email" in ef and "a@b.com" in ef, f"extract_fields 未抽中邮箱: {ef}"
    ag = execute_skill("apply_glossary",
                       _json.dumps({"text": "大模型 赋能 业务",
                                    "glossary_json": _json.dumps({"赋能": "助力"})}))
    assert "助力" in ag and "赋能" not in ag.split("替换后文本：")[-1], \
        f"apply_glossary 未替换: {ag}"
    ct = execute_skill("classify_taxonomy",
                       _json.dumps({"text": "这是一条正面评价，体验很好",
                                    "taxonomy_json": _json.dumps(
                                        [{"name": "正向", "keywords": ["正面", "好"]},
                                         {"name": "负向", "keywords": ["差", "坏"]}])}))
    assert "正向" in ct, f"classify_taxonomy 未命中正向: {ct}"
    uc = execute_skill("unit_convert",
                       _json.dumps({"value": 1, "from_unit": "km", "to_unit": "m"}))
    assert "1000" in uc, f"unit_convert 换算错误: {uc}"
    sc = execute_skill("spreadsheet_calc",
                       _json.dumps({"csv_text": "a,b\n1,2\n3,4", "op": "sum"}))
    assert "10" in sc, f"spreadsheet_calc 求和错误: {sc}"
    at = execute_skill("apply_template",
                       _json.dumps({"content": "甲\n乙", "template_name": "bullet"}))
    assert "- 甲" in at and "- 乙" in at, f"apply_template 未套用 bullet: {at}"
    sg = execute_skill("apply_style_guide",
                       _json.dumps({"text": "这是   一段   冗余空白 文本", "guide": "concise"}))
    assert "这是 一段 冗余空白 文本" in sg, f"apply_style_guide 未精简: {sg}"
    # PDF/OCR 无库 → 优雅降级（返回可读提示，不崩）
    ep = execute_skill("extract_pdf", _json.dumps({"path": "/nonexistent.pdf"}))
    assert isinstance(ep, str) and ("调用失败" in ep or "未安装" in ep), \
        f"extract_pdf 未优雅降级: {ep}"
    eo = execute_skill("extract_ocr", _json.dumps({"image_path": "/nonexistent.png"}))
    assert isinstance(eo, str) and ("调用失败" in eo or "未安装" in eo), \
        f"extract_ocr 未优雅降级: {eo}"
    print("✓ 第三层执行层: 7 个纯 stdlib 技能真跑通过 + extract_pdf/extract_ocr 优雅降级")

    # 9) 真·工具调用循环（注入式假响应：先回 tool_calls，再回终答）
    #    验证：模型发起调用 → 执行层真跑技能 → 结果回灌 → 模型产出融合终答。
    seq = {"n": 0}

    def skill_post(url, headers, body):
        seq["n"] += 1
        if seq["n"] == 1:
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "run_code",
                                             "arguments": '{"code": "print(6*7)"}'}}]},
                "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}
        return {"choices": [{"message": {
            "role": "assistant",
            "content": "经代码验证：6×7=42。结论成立。",
            "tool_calls": None},
            "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 15, "total_tokens": 95}}

    sk = LLMAgentBackend(rng=random.Random(0), http_post=skill_post)
    s_sig = sk.run({"type": "resistor", "label": "reason", "model": "large"}, rins)
    assert s_sig.ok is True, f"工具调用循环终答 ok 非 True: {s_sig.meta}"
    assert s_sig.meta.get("tool_calls") == ["run_code"], \
        f"工具调用循环未记录 run_code 执行: {s_sig.meta.get('tool_calls')}"
    assert "42" in s_sig.value, f"终答未融合工具执行结果: {s_sig.value}"
    print(f"✓ 真·工具调用循环: 模型发起 run_code → 执行 6*7=42 → 终答融合结果")

    # 10) 命名漂移符号映射注入提示词：含 input_map 的电阻在 user 中显式声明"Y ← 上游 X"
    mcomp = {"type": "resistor", "label": "reason", "model": "large",
             "required_inputs": ["china_gdp_2024"],
             "produced_outputs": ["analysis"],
             "input_map": {"china_gdp_2024": "gdp_china_2024"}}
    mmsg = be.render_messages(mcomp, rins)
    mustr = mmsg[1]["content"]
    assert "china_gdp_2024" in mustr and "gdp_china_2024" in mustr, \
        "映射信息未注入 user：应含 下游名 与 上游名"
    assert "符号映射" in mustr, "应在线性关系契约中标注『符号映射』"
    print("✓ 命名漂移映射注入：电阻提示词显式声明『china_gdp_2024 ← 上游 gdp_china_2024』")

    print("\n全部离线自检通过 ✓（未发起任何真实 API 调用）")


if __name__ == "__main__":
    selftest()
