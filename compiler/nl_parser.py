"""
circuit-agents · compiler.nl_parser
=================================
M4（NL → 结构化目标）：把自然语言目标解析成 `compiler.goal.Goal`（符合 GOAL_JSON_SCHEMA）。

设计（混合路线，与用户确认）：
 · **受控能力词表（cell library）**：canonical 能力 → 触发词（中英文）。无论规则还是 LLM，
   吐出的 capabilities 都限制在这张表内，不乱造（对应 COMPILER.md「规划所需能力需要 eval 兜底」）。
 · **规则解析（兜底，离线可跑）**：关键词→能力、正则→约束/模态/可靠性；文档类目标（含 pdf/image…）
   自动补 `retrieve`（要先读源才能处理）。默认路径，无需 key。
 · **LLM 增强（默认尝试，env→key 文件）**：解析到 key（环境变量 DEEPSEEK/OPENAI/AGENT > ~/Desktop/key_tmp.txt）时走 DeepSeek（OpenAI-compatible）按 schema 出结构化 JSON。
   LLM **只做它擅长的**——理解自然语言、把任务拆成子任务、诚实声明每个子任务的 inputs/outputs
   （"吃什么/产什么"）；**依赖/并联判定不再由模型猜**，而是由下游规则引擎
   `router_auto.dependencies_from_subtasks` 据 input/output 做**确定性数据依赖分析**（netlist 式）：
   子任务 X 的 output 被子任务 Y 的 input 引用 → Y 依赖 X（串联）；互不引用 → 并联（拓扑自然涌现）。
   命名漂移（input 名无对应 output）→ 告警而非静默误并。LLM 仍可用 `dependencies` 表达
   "非数据但须先后"的软依赖（覆盖合并）。LLM 失败/无 key/输出非法 → 自动回退规则。
 · 默认尝试真模型：解析到 key（环境变量 DEEPSEEK/OPENAI/AGENT > ~/Desktop/key_tmp.txt）即走 DeepSeek 规划；无 key 则整条 M0→M4 流水线离线即可演示（不触网）。即『规划默认调用 apikey，除非没有 apikey』。

诚实边界：
 · 规则解析是"保守近似"，遇新说法/新能力会漏（这正是文档说 NL→netlist 最难的原因）。
 · LLM 解析仍只是"规划建议"，最终拓扑是否真满足目标要靠 M3 Optimizer + runtime 仿真/Evaluator 兜底。
 · 本模块只负责"NL→Goal"这一跳；下游 compile_goal / Circuit 不变。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .goal import Goal
from .router_auto import dependencies_from_subtasks, _analyze_dependencies
from .backend_llm import resolve_api_key


# ---------------------------------------------------------------------------
# 受控能力词表（cell library）：canonical 能力 → 触发词（中英文，小写匹配）
# ---------------------------------------------------------------------------
CAPABILITY_VOCAB = {
    "retrieve": ["检索", "查找", "搜索", "查", "找", "读取", "读", "获取", "搜",
                 "fetch", "search", "read", "lookup"],
    "reason":   ["推理", "分析", "思考", "推导", "总结", "归纳", "概述",
                 "reason", "analyze", "infer", "summarize", "think"],
    "calculate":["计算", "算", "统计", "核算", "数",
                 "calculate", "compute", "calc"],
    "verify":   ["核对", "验证", "检查", "校验", "确认",
                 "verify", "check", "validate", "confirm"],
    "translate":["翻译", "译", "translate"],
    "extract":  ["提取", "抽取", "抽出", "extract"],
    "classify": ["分类", "归类", "打标签", "classify", "categorize"],
    # 结构化输出类（2026-08-02 增补：修「整理成表格」漏抓的实测短板）
    "organize": ["整理", "编排", "排版", "梳理", "汇整", "做成", "列成", "列表", "陈列",
                 "organize", "tabulate", "arrange", "compile", "structure"],
    "summarize": ["摘要", "综述", "概括", "提炼", "abstract", "recap", "overview", "synopsis"],
}

# 模态词表
MODALITY_VOCAB = {
    "pdf":    ["pdf", "文档", "报告", "论文"],
    "image":  ["图片", "图像", "照片", "图", "image", "picture"],
    "table":  ["表格", "表", "table"],
    "text":   ["文本", "文字", "text"],
    "audio":  ["音频", "语音", "audio"],
    "video":  ["视频", "video"],
}


# 中文数字 → int（支持 0-99 常见写法；非中文数字原样回退）
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s):
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if s in _CN:
        return _CN[s]
    if "十" in s:  # 十几 / 几十 / 几十几
        a, _, b = s.partition("十")
        tens = _CN.get(a, 1) if a else 1
        ones = _CN.get(b, 0) if b else 0
        return tens * 10 + ones
    return None


def _count_retrieve_sources(nl: str) -> int:
    """数出『并列检索』应拆成几个 source 实例：按「并列对象数量」，而非触发词出现次数。

    修正前：occ = 触发词(搜/查…)出现次数 + 顿号 bump，导致『同时搜四个榜单 A、B、C、D』
    只拆成 2 路 retrieve（触发词出现 1 次 + 有顿号→2），看不到真实 4 路并行。
    修正后：优先显式数量词（搜4个/搜四个），否则取检索动词后、到下一个 sink 动词/
    连接词之间的并列块，按分隔符切分出并列项数（取最多的一段）。
    封顶 6；parallel_intent 下调用方保证下限 ≥2。
    """
    # 1) 显式数量词优先：搜4个 / 搜四个 / 查三份
    m = re.search(r"(?:搜|查|检索|查找|获取|找|搜索)\s*([0-9一二三四五六七八九十两]+)\s*(?:个|项|类|份|张|种|条)", nl)
    if m:
        n = _cn_to_int(m.group(1))
        if n and n >= 2:
            return min(n, 6)
    # 2) 否则：取检索动词后并列块的最大项数
    SINK_CUT = r"(?:分析|总结|归纳|概括|提炼|比对|对比|然后|再|最后|其中|并分析|并对比|并找|找.{0,8}并)"
    best = 1
    for mv in re.finditer(r"(?:搜|查|检索|查找|获取|找|搜索)", nl):
        seg = nl[mv.end():]
        seg = re.split(SINK_CUT, seg)[0]  # 截断到首个 sink 动词 / 连接词
        items = [x for x in re.split(r"[、，,，/和 与 以及 及：:；;]+", seg) if x.strip()]
        best = max(best, len(items))
    return min(best, 6)


class GoalParser:
    """NL → Goal 的混合解析器（规则兜底 + 可选 LLM 增强）。"""

    def __init__(self, api_key=None, base_url=None, model=None,
                 timeout=60.0, http_post=None):
        self.api_key = resolve_api_key(api_key)
        self.base_url = (base_url or os.environ.get("AGENT_API_BASE")
                         or "https://api.deepseek.com/v1").rstrip("/")
        self.model = model or "deepseek-chat"
        self.timeout = timeout
        self._http_post = http_post  # 注入式：离线测试用假响应 / 计数

    # ---- 对外入口 ----
    def parse(self, nl: str) -> Goal:
        """自然语言 → Goal。有 key 先试 LLM，失败/无 key 回退规则。"""
        if self.api_key:
            try:
                g = self._parse_llm(nl)
            except Exception:
                # LLM 失败 / 输出非法 / 网络异常 → 保守回退规则
                g = self._parse_rule(nl)
        else:
            g = self._parse_rule(nl)
        # 「高可靠」语义补全：要求高可靠 ⇒ 自动接一条反馈环（整链重试）作可靠性保险，
        # 除非目标已显式声明 feedback（尊重显式意图，不覆盖）。属 M4 规划层增强，
        # 让真实高可靠任务能跑出"质量不过就整链重试"的闭环，而非只有显式 feedback 才有环。
        if g.reliability == "high" and not g.feedback:
            g.feedback = {"max_iter": 3}
        return g

    # ---- 规则解析（兜底，离线） ----
    def _parse_rule(self, nl: str) -> Goal:
        nl_low = nl.lower()
        caps = []
        for cap, words in CAPABILITY_VOCAB.items():
            if any(w.lower() in nl_low for w in words):
                caps.append(cap)
        # 去重保序
        seen, uniq = set(), []
        for c in caps:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        caps = uniq

        # ---- 并行意图（M4 增强 · 真并行执行）：显式"并行/同时/分别/各自/并发/同步/各" →
        # 进入"并联优先的分层 DAG"：所有 source 能力(retrieve/extract) 并联在首层，其余
        # sink 能力(reason/summarize/…)依赖全部 source（sink 之间仍并联）。纯 source 或纯
        # sink 且无分层时退化为全并联(dependencies=[])。属用户路线第一层「真正并行执行」的规划侧。
        SOURCE_CAPS = {"retrieve", "extract"}
        parallel_intent = bool(re.search(
            r"(并行|同时|分别|各自|并发|一起做|同步|各|一并|都)", nl))
        dependencies = None
        if parallel_intent:
            # 多重集(Step C)：同一 source 能力在句中多次出现 → 拆成多个并联实例。
            # 触发词计数 ≥2，或用并列连词(和/与/、/以及) 且并行意图明确 → 至少 2 个。上限 4。
            expanded = []
            for c in caps:
                if c in SOURCE_CAPS and c == "retrieve":
                    # 修正 Bug2：按『并列检索对象数量』拆，而非触发词出现次数
                    occ = _count_retrieve_sources(nl)
                    occ = min(occ, 6)
                    if occ >= 2:
                        expanded.append(c)  # 首实例保持原名
                        for k in range(2, occ + 1):
                            expanded.append(f"{c}#{k}")
                    else:
                        expanded.append(c)
                else:
                    expanded.append(c)
            caps = expanded
            # 注意：多重集扩展出的 "retrieve#2" 等带后缀名，必须按基础名归类，
            # 否则会被误判为 sink，反被 retrieve 喂 → 多出错误边、分层错乱。
            def _base(c):
                return re.sub(r"#\d+$", "", c)
            sources = [c for c in caps if _base(c) in SOURCE_CAPS]
            sinks = [c for c in caps if _base(c) not in SOURCE_CAPS]
            edges = []
            if sources and sinks:
                for s in sinks:
                    for src in sources:
                        edges.append([src, s])
            dependencies = edges if edges else []

        constraints: dict = {}
        m = re.search(r"(?:延迟|耗时|响应|时间).*?(\d+)\s*(?:ms|毫秒|亳秒)", nl)
        if m:
            constraints["max_latency_ms"] = int(m.group(1))
        m = re.search(r"(?:成本|预算|花费|价钱|价格).*?([\d.]+)", nl)
        if m:
            constraints["max_cost"] = float(m.group(1))
        m = re.search(r"(?:质量|准确率|精度|正确率).*?([\d.]+)\s*%?", nl)
        if m:
            q = float(m.group(1))
            constraints["min_quality"] = q / 100.0 if q > 1 else q

        # 字数/篇幅约束（独立字段 max_chars；绝不映射成 max_cost，
        # 以免被 auto_tiers 的『成本受限保 small』误伤高可靠升档）。
        m = re.search(r"([0-9]+)\s*字(?:以内|内|封顶)?", nl)
        if not m:
            m = re.search(r"字数?\s*(?:不超过|不多于|少于|小于|限|仅限|在)\s*([0-9]+)", nl)
        if m:
            constraints["max_chars"] = int(m.group(1))

        modalities = [mod for mod, words in MODALITY_VOCAB.items()
                      if any(w.lower() in nl_low for w in words)]

        reliability = "normal"
        if re.search(r"(?:高可靠|务必|必须|严格|关键|重要|可靠)", nl):
            reliability = "high"
        elif re.search(r"(?:随便|不必|低要求|宽松|无关紧要)", nl):
            reliability = "low"

        # 规划启发：文档/媒体类目标 → 必须先 retrieve（读源）才能处理
        if modalities and "retrieve" not in caps:
            caps.insert(0, "retrieve")

        if not caps:
            caps = ["reason"]  # 最终兜底，保证非空

        return Goal.from_dict({
            "name": "nl_goal",
            "description": nl,
            "capabilities": caps,
            "constraints": constraints,
            "modalities": modalities,
            "reliability": reliability,
            "dependencies": dependencies,
        })

    # ---- LLM 解析（可选，opt-in） ----
    def _build_messages(self, nl: str):
        cap_keys = "、".join(CAPABILITY_VOCAB.keys())
        system = (
            "你是把自然语言目标转换成结构化 JSON 的解析器。\n"
            "你只做你擅长的事：理解自然语言、把任务拆成子任务、诚实声明每个子任务"
            "『吃什么(input)和产什么(output)』。\n"
            "**不要**自己判断子任务之间谁先谁后——依赖关系由下游规则引擎根据 input/output "
            "自动算出（数据依赖分析 / netlist 式生成）。\n\n"
            f"capabilities 只能从这张受控词表里选：{cap_keys}。\n\n"
            "只输出 JSON，不要任何解释或 markdown 代码块。字段：\n"
            "{\n"
            '  "name": "简短英文名(可选)",\n'
            '  "description": "原任务句",\n'
            '  "subtasks": [\n'
            '    {"id":"A","capability":"retrieve","description":"查中国GDP总量","inputs":[],"outputs":["gdp_total"]},\n'
            '    {"id":"B","capability":"retrieve","description":"查人均GDP","inputs":[],"outputs":["gdp_per_capita"]},\n'
            '    {"id":"C","capability":"reason","description":"对比分析","inputs":["gdp_total","gdp_per_capita"],"outputs":["analysis"]}\n'
            "  ],\n"
            '  "constraints": {"max_latency_ms":3000,"min_quality":0.9,"max_chars":200},\n'
            '  "modalities": ["pdf"],\n'
            '  "reliability": "low/normal/high",\n'
            '  "dependencies": []\n'
            "}\n\n"
            "规则：\n"
            "· 每个 subtask 必须有唯一 id、capability(受控词表)、description、"
            "inputs(它需要的产物名列表)、outputs(它产出的产物名列表)。\n"
            "· inputs 必须如实填：若某 subtask 需要另一个 subtask 的产出，就把那个产出的 output 名"
            "**原样**写进自己的 inputs（不要改写写法，否则规则引擎匹配不上）。\n"
            "· outputs 用简短语义名（中英文均可），能体现产物内容。\n"
            "· 不依赖任何内部 subtask 产出的 inputs 留空 []。\n"
            "· 多个并列/独立对象（『分析A、分析B』『搜X和Y』『分别总结三篇』）→ "
            "各自一个 subtask、id 不同、inputs 都空 → 自动并联。\n"
            "· 省略 dependencies 即可；只有『非数据、纯顺序』的软依赖才填 dependencies"
            "（如 [[前置id,后置id]]）。\n"
        )
        few_shot = (
            '示例1：\n输入："总结一篇 PDF 并核对里面的数字，要求高可靠，延迟不超过 3000ms"\n'
            '输出：{"name":"pdf_check","description":"总结一篇 PDF 并核对里面的数字，要求高可靠，延迟不超过 3000ms",'
            '"subtasks":['
            '{"id":"T1","capability":"retrieve","description":"读取PDF原文","inputs":[],"outputs":["pdf_text"]},'
            '{"id":"T2","capability":"reason","description":"总结PDF","inputs":["pdf_text"],"outputs":["summary"]},'
            '{"id":"T3","capability":"calculate","description":"核算数字","inputs":["pdf_text"],"outputs":["numbers"]},'
            '{"id":"T4","capability":"verify","description":"核对数字","inputs":["summary","numbers"],"outputs":["verdict"]}'
            '],"constraints":{"max_latency_ms":3000},"modalities":["pdf"],"reliability":"high"}\n'
            '示例2：\n输入："分析A公司财报，分析B公司财报，最后汇总"，要求高可靠\n'
            '输出：{"name":"ab_compare","description":"分析A公司财报，分析B公司财报，最后汇总",'
            '"subtasks":['
            '{"id":"A","capability":"reason","description":"分析A公司财报","inputs":[],"outputs":["report_a"]},'
            '{"id":"B","capability":"reason","description":"分析B公司财报","inputs":[],"outputs":["report_b"]},'
            '{"id":"C","capability":"summarize","description":"汇总对比","inputs":["report_a","report_b"],"outputs":["comparison"]}'
            '],"reliability":"high"}'
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": few_shot + "\n\n现在解析：\n" + nl},
        ]

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError("无法从 LLM 响应中提取 JSON")

    def _chat(self, url, headers, body):
        if self._http_post:
            return self._http_post(url, headers, body)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _parse_llm(self, nl: str) -> Goal:
        messages = self._build_messages(nl)
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        resp = self._chat(url, headers, body)
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        data = self._extract_json(content)
        # 新 schema：LLM 声明 subtasks(inputs/outputs) → 引擎确定性算依赖；
        # 旧 schema / 非法 → 走 Goal.from_dict（校验失败抛 ValueError → 回退规则）。
        if isinstance(data, dict) and data.get("subtasks"):
            return self._goal_from_subtasks(data)
        return Goal.from_dict(data)

    @staticmethod
    def _goal_from_subtasks(data: dict) -> "Goal":
        """把 LLM 的 subtasks 转成 Goal：分配节点名 + 引擎算依赖 + 翻译 + 校验。

        · 每个 subtask 分配节点名 = 能力名 + 实例序号（复用 #N 约定，兼容 compile_goal 的 _base 剥离）；
        · 依赖边由 _analyze_dependencies 据 inputs/outputs 确定性算出（subtask id 对），
          再翻译成节点名；LLM 显式 dependencies 作为软依赖覆盖合并；
          命名漂移（下游输入名≠上游产出名但语义等价）由同函数一并算成符号映射表
          {下游输入: 上游实际产物}，写入 component_io[node].input_map（转接头，零回归）；
        · 最终交给 Goal.from_dict 做边/环校验，保证下游拓扑合法。
        """
        subs = data.get("subtasks") or []
        if not subs:
            raise ValueError("subtasks 为空")
        node_of: dict = {}
        cap_count: dict = {}
        component_io: dict = {}   # 节点名 → {required_inputs, produced_outputs, input_map}（线性关系落点）
        for st in subs:
            sid = st.get("id")
            cap = (st.get("capability") or "").strip()
            if not cap:
                raise ValueError(f"子任务 {sid!r} 缺 capability")
            cap_count[cap] = cap_count.get(cap, 0) + 1
            n = cap_count[cap]
            node = cap if n == 1 else f"{cap}#{n}"
            node_of[sid] = node
            component_io[node] = {
                "required_inputs": list(st.get("inputs") or []),
                "produced_outputs": list(st.get("outputs") or []),
            }
        capabilities = [node_of[st.get("id")] for st in subs]
        # 确定性数据依赖分析：返回 (边, 符号映射表)。
        # 映射表仅含『命名漂移但语义等价』的非同名对 {下游输入: 上游实际产物}，
        # 注入 component_io[node].input_map，供 router 透传 + runtime 按映射核对线性关系。
        raw_edges, input_maps = _analyze_dependencies(
            subs, override_deps=data.get("dependencies"))
        for sid, mp in input_maps.items():
            node = node_of.get(sid)
            if node and node in component_io and mp:
                component_io[node]["input_map"] = dict(mp)
        edges = [[node_of[a], node_of[b]] for a, b in raw_edges]
        goal_dict = {
            "name": data.get("name", "nl_goal"),
            "description": data.get("description", ""),
            "capabilities": capabilities,
            "constraints": data.get("constraints", {}) or {},
            "modalities": data.get("modalities", []) or [],
            "reliability": data.get("reliability", "normal"),
            "dependencies": edges,
            "subtasks": subs,
            "component_io": component_io,
        }
        return Goal.from_dict(goal_dict)


# ---------------------------------------------------------------------------
# 离线自检（无需 API key）
# ---------------------------------------------------------------------------
def selftest():
    # 1) 规则解析：文档类自动补 retrieve、约束/模态/可靠性识别
    p = GoalParser()
    g = p.parse("总结一篇PDF并核对里面的数字，要求高可靠，延迟不超过3000ms")
    assert "reason" in g.capabilities and "verify" in g.capabilities
    assert "retrieve" in g.capabilities, "文档类目标应自动补 retrieve"
    assert "calculate" in g.capabilities, "『数字』应映射到 calculate"
    assert "pdf" in g.modalities
    assert g.reliability == "high"
    assert g.constraints.get("max_latency_ms") == 3000
    assert g.feedback and g.feedback.get("max_iter") == 3, "高可靠应自动接反馈环(max_iter=3)"
    print("✓ rule-based: NL→Goal 解析正确（文档类自动补 retrieve + 约束/模态/可靠性）")
    print("✓ 高可靠语义补全：reliability=high ⇒ 自动接 feedback(max_iter=3)")

    # 2) LLM 注入假响应：结构化 JSON 正确映射
    fake = {"choices": [{"message": {"content":
        '{"capabilities":["retrieve","reason","verify"],'
        '"constraints":{"min_quality":0.9},"modalities":["pdf"],"reliability":"high"}'}}]}

    def fake_post(url, headers, body):
        assert url.endswith("/chat/completions")
        return fake

    p2 = GoalParser(api_key="fake", http_post=fake_post)
    g2 = p2.parse("随便一句话")
    assert g2.capabilities == ["retrieve", "reason", "verify"]
    assert g2.constraints.get("min_quality") == 0.9
    print("✓ LLM 注入：结构化 JSON 正确映射为 Goal")

    # 3) LLM 非法响应 → 自动回退规则
    def bad_post(url, headers, body):
        return {"choices": [{"message": {"content": "这个任务太难了，我没法结构化。"}}]}

    p3 = GoalParser(api_key="fake", http_post=bad_post)
    g3 = p3.parse("总结报告并验证")
    assert "reason" in g3.capabilities and "verify" in g3.capabilities
    print("✓ LLM 非法响应 → 自动回退规则解析")

    # 4) 无 key → 直接规则
    p4 = GoalParser()
    g4 = p4.parse("把这段英文翻译成中文")
    assert "translate" in g4.capabilities
    print("✓ 无 key：直接规则解析")

    # 5) 字数约束独立字段（Bug1 修复）：不超过200字 → max_chars=200，且不污染 max_cost
    p5 = GoalParser()
    g5 = p5.parse("写一段不超过200字的小学生科普文，要求高可靠")
    assert g5.constraints.get("max_chars") == 200, "字数约束应解析到 max_chars"
    assert "max_cost" not in g5.constraints, "字数约束绝不能映射成 max_cost"
    print("✓ 字数约束：独立 max_chars 字段，不污染 max_cost（Bug1 修复）")

    # 6) 多并列检索按并列对象数量拆（Bug2 修复）：同时搜四个榜单 → 4 路 retrieve
    p6 = GoalParser()
    g6 = p6.parse("同时搜四个榜单：GDP总量、人均GDP、幸福指数、创新指数，然后分析")
    retrieves = [c for c in g6.capabilities if c.startswith("retrieve")]
    assert len(retrieves) == 4, f"应拆出 4 个 retrieve 实例，实际 {retrieves}"
    assert g6.dependencies and len(g6.dependencies) >= 4, "4 路 retrieve 应并联在源层"
    print("✓ 多并列检索：按并列对象数量拆为 4 路 retrieve（Bug2 修复）")

    # 7) LLM 默认并联：独立多子任务（无『并行』词）LLM 回 dependencies=[] ⇒ 并联而非串行
    fake_par = {"choices": [{"message": {"content":
        '{"capabilities":["reason","reason#2","summarize"],'
        '"dependencies":[],"reliability":"normal"}'}}]}

    def fake_post_par(url, headers, body):
        return fake_par

    p7 = GoalParser(api_key="fake", http_post=fake_post_par)
    g7 = p7.parse("分析A公司的财报，分析B公司的财报，最后汇总")
    assert g7.dependencies == [], f"LLM 默认应判并联([])，实际 {g7.dependencies}"
    assert len([c for c in g7.capabilities if c.startswith("reason")]) == 2, "应拆出 2 个 reason 实例"
    print("✓ LLM 默认并联：独立多子任务(无并行词) ⇒ dependencies=[] 并联（接好 key 即自动判并）")

    # 8) LLM 显式先后依赖仍尊重：带『先X再Y』⇒ 产出依赖边（不强行并联）
    fake_seq = {"choices": [{"message": {"content":
        '{"capabilities":["calculate","reason"],'
        '"dependencies":[["calculate","reason"]],"reliability":"normal"}'}}]}

    def fake_post_seq(url, headers, body):
        return fake_seq

    p8 = GoalParser(api_key="fake", http_post=fake_post_seq)
    g8 = p8.parse("先算出总营收，再据此分析利润率")
    assert g8.dependencies == [["calculate", "reason"]], f"应保留显式依赖边，实际 {g8.dependencies}"
    print("✓ LLM 显式依赖：带先后关系 ⇒ 保留依赖边（不强行并联）")

    # 9) 子任务分解：LLM 声明 inputs/outputs → 引擎确定性算出并联拓扑（不再让模型猜依赖）
    fake_sub = {"choices": [{"message": {"content":
        '{"subtasks":['
        '{"id":"A","capability":"retrieve","inputs":[],"outputs":["gdp_total"]},'
        '{"id":"B","capability":"retrieve","inputs":[],"outputs":["gdp_per_capita"]},'
        '{"id":"C","capability":"reason","inputs":["gdp_total","gdp_per_capita"],"outputs":["analysis"]}'
        '],"reliability":"normal"}'}}]}
    def fake_post_sub(url, headers, body):
        return fake_sub
    p9 = GoalParser(api_key="fake", http_post=fake_post_sub)
    g9 = p9.parse("查中国GDP总量和人均GDP，再对比分析")
    # A→retrieve, B→retrieve#2, C→reason；引擎应算出 [A,C] 与 [B,C] 两条边
    assert g9.capabilities == ["retrieve", "retrieve#2", "reason"], g9.capabilities
    assert ["retrieve", "reason"] in g9.dependencies and ["retrieve#2", "reason"] in g9.dependencies, g9.dependencies
    assert len(g9.dependencies) == 2, f"应 2 条边（A,B 并联共喂 C），实际 {g9.dependencies}"
    print("✓ 子任务分解：LLM 声明 inputs/outputs → 引擎确定性算出并联拓扑（A,B 并联共喂 C）")

    # 10) 子任务分解：未满足 input（命名漂移）→ 不静默误并，仅告警、拓扑退为并联
    fake_sub2 = {"choices": [{"message": {"content":
        '{"subtasks":['
        '{"id":"X","capability":"retrieve","inputs":[],"outputs":["x"]},'
        '{"id":"Y","capability":"reason","inputs":["ghost_name"],"outputs":["y"]}'
        '],"reliability":"normal"}'}}]}
    def fake_post_sub2(url, headers, body):
        return fake_sub2
    p10 = GoalParser(api_key="fake", http_post=fake_post_sub2)
    g10 = p10.parse("检索X，再分析Y（输入名故意写错）")
    assert g10.dependencies == [], g10.dependencies  # ghost_name 无产出 → 不建边 → X,Y 并联
    assert set(g10.capabilities) == {"retrieve", "reason"}, g10.capabilities
    print("✓ 子任务分解：未满足 input 不静默误并（命名漂移告警，拓扑退为并联）")

    # 11) 子任务分解 + 命名漂移符号映射：下游输入名漂移但等价 → 建边 + input_map 注入 component_io
    fake_sub3 = {"choices": [{"message": {"content":
        '{"subtasks":['
        '{"id":"A","capability":"retrieve","inputs":[],"outputs":["gdp_china_2024"]},'
        '{"id":"B","capability":"retrieve","inputs":[],"outputs":["gdp_per_capita"]},'
        '{"id":"C","capability":"reason","inputs":["china_gdp_2024","gdp_per_capita"],"outputs":["analysis"]}'
        '],"reliability":"normal"}'}}]}
    def fake_post_sub3(url, headers, body):
        return fake_sub3
    p11 = GoalParser(api_key="fake", http_post=fake_post_sub3)
    g11 = p11.parse("查中国GDP总量和人均GDP，再对比分析")
    # A→retrieve, B→retrieve#2, C→reason；C.china_gdp_2024 漂移匹配 A.gdp_china_2024
    assert g11.capabilities == ["retrieve", "retrieve#2", "reason"], g11.capabilities
    assert ["retrieve", "reason"] in g11.dependencies and ["retrieve#2", "reason"] in g11.dependencies, g11.dependencies
    io_c = g11.component_io.get("reason", {})
    assert io_c.get("input_map") == {"china_gdp_2024": "gdp_china_2024"}, io_c
    print("✓ 子任务分解+命名漂移映射：下游 china_gdp_2024←上游 gdp_china_2024 建边 + input_map 注入 component_io")

    print("\nM4 nl_parser 离线自检全部通过 ✓")


if __name__ == "__main__":
    selftest()
