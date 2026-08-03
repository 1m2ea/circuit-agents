"""
circuit-agents · compiler.goal
===========================
结构化目标（Goal）schema —— 布局布线编译器的 M0 输入。

Goal 是"要做什么"的结构化描述，比 Circuit DSL 高一层：
NL 目标 → (M4) → Goal → (Netlister) → Circuit DSL 网表 → (runtime) 仿真。

设计取舍：M0 先做"结构化目标"（规则即可跑），NL→Goal 留到 M4。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

VALID_RELIABILITY = ("low", "normal", "high")
VALID_TIERS = ("small", "large", "tool")
CONSTRAINT_KEYS = ("max_latency_ms", "max_cost", "min_quality", "max_chars")


# JSON-Schema 风格的描述，用于文档 + 校验。
GOAL_JSON_SCHEMA = {
    "type": "object",
    "required": ["capabilities"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "capabilities": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "所需功能节点（必需功能 DAG），如 retrieve/reason/calculate/verify",
        },
        "constraints": {
            "type": "object",
            "properties": {
                "max_latency_ms": {"type": "number", "minimum": 0},
                "max_cost": {"type": "number", "minimum": 0},
                "min_quality": {"type": "number", "minimum": 0, "maximum": 1},
                "max_chars": {"type": "integer", "minimum": 1,
                              "description": "篇幅/字数上限（独立字段，不视为成本约束，不会压低型号档）"},
            },
        },
        "modalities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "输入模态，如 text/image/pdf/table；>1 自动走整流桥",
        },
        "reliability": {"enum": list(VALID_RELIABILITY)},
        "tiers": {
            "type": "object",
            "description": "可选：capability→型号档(small/large/tool) 覆盖；默认由 Binder(M1) 决定",
            "additionalProperties": {"enum": list(VALID_TIERS)},
        },
        "dependencies": {
            "type": "array",
            "description": "可选能力依赖 DAG：None=线性串联(向后兼容)，[]=全并联，[[pre,post],...]=DAG",
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
        },
        "subtasks": {
            "type": "array",
            "description": ("可选子任务分解（数据依赖分析用）：每项 "
                            "{id, capability, description, inputs:[产物名], outputs:[产物名]}；"
                            "dependencies 由下游规则引擎据 inputs/outputs 自动算出，无需 LLM 判先后"),
        },
        "component_io": {
            "type": "object",
            "description": ("可选：节点名 → {required_inputs:[产物名], produced_outputs:[产物名]}。"
                            "由 _goal_from_subtasks 据 subtasks 自动生成，供 runtime 在"
                            "每个电阻跑前核对『线性关系(声明输入)是否被上游产出覆盖』；"
                            "缺省/为空则不触发该自测（向后兼容）。"),
        },
        "feedback": {
            "type": "object",
            "description": "可选反馈环(标准单元#3)：{\"max_iter\": N}(N>=1) → 末级汇合门控整链重试",
            "properties": {"max_iter": {"type": "integer", "minimum": 1}},
        },
        "redundancy": {
            "type": "object",
            "description": "可选冗余(标准单元#5)：{能力名: K}(K>=1 副本数，K>=2 即冗余) → 复制并联+any汇合",
            "additionalProperties": {"type": "integer", "minimum": 1},
        },
        "recovery": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "可选恢复系数 η[0,1]：强 agent 部分挽救弱输入(output=inp+η·(cap−inp))，缺省 0=旧行为",
        },
    },
}


@dataclass
class Goal:
    capabilities: list
    constraints: dict = field(default_factory=dict)
    modalities: list = field(default_factory=list)
    reliability: str = "normal"
    name: str = "unnamed-goal"
    description: str = ""
    tiers: dict = field(default_factory=dict)
    # 可选能力依赖 DAG：
    #   None  → 线性串联（M0/M1 行为，向后兼容）
    #   []    → 显式空列表 = 所有能力互不依赖 → 全并联（延迟=最慢支路）
    #   [[pre,post],...] → 声明依赖边，Router 按 Kahn 分层（同层并联/跨层串联）
    dependencies: Optional[list] = None
    # 可选反馈环（标准单元 #3）：{"max_iter": N}(N>=1) → 末级汇合(all-fired)门控整链重试；
    # None = 无环。runtime 原生仅支持单环，故门控点固定为末级汇合点（详见 router.py）。
    feedback: Optional[dict] = None
    # 可选冗余（标准单元 #5）：{能力名: K}(K>=1 总副本数，K>=2 即冗余) → 该能力复制 K 份
    # 并联，由 capacitor(mode="any") 收口（任一副本存活即 ok）。None = 无冗余。
    # 注：mode="any" 是 runtime 为冗余单元新增的最小开关，默认 all，现有拓扑零变化。
    redundancy: Optional[dict] = None
    # 可选运行时自愈开关（第二层⑧）：执行中电阻 yield 失败(开路)且仍在反馈预算内时，
    # 热升级该节点档位(small→large→tool)后重试，而不是用原拓扑空转重试。缺省 False（关闭，
    # 行为同旧版）。仅 runtime.Circuit.execute(self_heal=True) 时生效；plan.py 用 --self-heal 开启。
    self_heal: bool = False
    # 可选恢复系数（补强#1）：η∈[0,1]，缺省 0 = 旧行为（无恢复）。
    # 语义：上游 ok 且 quality<cap（弱但存活的输入）时，强 agent 把输出从 min(inp,cap)
    # 部分抬升到 inp+η·(cap−inp)（仍不超过 cap）；上游开路(ok=False) 时严格保持开路
    # （recovery 绝不 revive，延续"开路必须保持开路"内核规则）。
    recovery: float = 0.0
    # 可选子任务分解（数据依赖分析用）：LLM 诚实声明的 inputs/outputs，
    # 下游规则引擎据此确定性算出 dependencies（拓扑）。仅作结构化透传，不强制校验内部字段。
    subtasks: Optional[list] = None
    # 可选组件 IO 映射（数据依赖分析的落点）：节点名(cap#N) →
    # {"required_inputs":[产物名], "produced_outputs":[产物名]}。由 _goal_from_subtasks
    # 据 subtasks 自动生成；runtime.propagate 在每个电阻跑前核对 required_inputs 是否被
    # 上游 produced_outputs 覆盖（线性关系自测），缺则 gate:fail。None/空 = 不触发自测。
    component_io: Optional[dict] = None

    # ---- 校验 + 构造 ----
    @staticmethod
    def from_dict(d: dict) -> "Goal":
        if not isinstance(d, dict):
            raise TypeError("goal 必须是 dict/JSON 对象")
        caps = d.get("capabilities")
        if (not isinstance(caps, list) or not caps
                or not all(isinstance(c, str) for c in caps)):
            raise ValueError("goal.capabilities 必须是非空字符串列表")
        constraints = d.get("constraints", {}) or {}
        if not isinstance(constraints, dict):
            raise ValueError("goal.constraints 必须是对象")
        for k, v in constraints.items():
            if k not in CONSTRAINT_KEYS:
                raise ValueError(f"未知约束键 {k!r}（允许：{CONSTRAINT_KEYS}）")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"约束 {k} 必须是 >=0 的数值")
        if "min_quality" in constraints and not (0 <= constraints["min_quality"] <= 1):
            raise ValueError("min_quality 必须在 [0,1]")
        modalities = d.get("modalities", []) or []
        if not isinstance(modalities, list) or not all(isinstance(m, str) for m in modalities):
            raise ValueError("goal.modalities 必须是字符串列表")
        reliability = d.get("reliability", "normal")
        if reliability not in VALID_RELIABILITY:
            raise ValueError(f"reliability 必须是 {VALID_RELIABILITY} 之一")
        tiers = d.get("tiers", {}) or {}
        if not isinstance(tiers, dict):
            raise ValueError("goal.tiers 必须是对象")
        for c, t in tiers.items():
            if t not in VALID_TIERS:
                raise ValueError(f"tiers[{c}] 必须是 {VALID_TIERS}")
        # --- dependencies（可选能力依赖 DAG）---
        raw_deps = d.get("dependencies", None)
        deps = None
        if raw_deps is not None:
            if not isinstance(raw_deps, list):
                raise ValueError("goal.dependencies 必须是边的列表")
            idx = {c: i for i, c in enumerate(caps)}
            pairs = []
            for e in raw_deps:
                if (not isinstance(e, (list, tuple)) or len(e) != 2
                        or not all(isinstance(x, str) for x in e)):
                    raise ValueError("dependencies 每条必须是 [前置, 后置] 能力名对")
                pre, post = e[0], e[1]
                if pre not in idx or post not in idx:
                    raise ValueError(f"dependencies 边 {list(e)} 引用了未声明的能力")
                if pre == post:
                    raise ValueError(f"dependencies 存在自环: {list(e)}")
                pairs.append([pre, post])
            # 环检测：Kahn 能否排完所有节点
            adj = {c: [] for c in caps}
            indeg = {c: 0 for c in caps}
            for pre, post in pairs:
                adj[pre].append(post)
                indeg[post] += 1
            indeg = dict(indeg)
            ready = [c for c in caps if indeg[c] == 0]
            seen = 0
            while ready:
                u = ready.pop(0)
                seen += 1
                for v in adj[u]:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        ready.append(v)
            if seen != len(caps):
                raise ValueError("dependencies 存在环，无法拓扑排序")
            deps = pairs
        # --- feedback（可选反馈环）---
        raw_fb = d.get("feedback", None)
        fb = None
        if raw_fb is not None:
            if not isinstance(raw_fb, dict):
                raise ValueError("goal.feedback 必须是对象")
            mi = raw_fb.get("max_iter", 1)
            if not isinstance(mi, int) or isinstance(mi, bool) or mi < 1:
                raise ValueError("goal.feedback.max_iter 必须是 >=1 的整数")
            fb = {"max_iter": mi}
        # --- redundancy（可选冗余单元 #5）---
        raw_red = d.get("redundancy", None)
        red = None
        if raw_red is not None:
            if not isinstance(raw_red, dict):
                raise ValueError("goal.redundancy 必须是 {能力名: 副本数} 对象")
            red = {}
            for c, k in raw_red.items():
                if c not in caps:
                    raise ValueError(f"redundancy 键 {c!r} 不是已声明的能力")
                if not isinstance(k, int) or isinstance(k, bool) or k < 1:
                    raise ValueError(f"redundancy[{c}] 必须是 >=1 的整数(副本数)")
                red[c] = k
        # --- recovery（可选恢复系数 #1）---
        raw_rec = d.get("recovery", 0.0)
        if isinstance(raw_rec, bool) or not isinstance(raw_rec, (int, float)) or not (0 <= raw_rec <= 1):
            raise ValueError("goal.recovery 必须是 [0,1] 区间内的数值")
        rec = float(raw_rec)
        # --- self_heal（可选运行时自愈开关 #⑧）---
        raw_sh = d.get("self_heal", False)
        if not isinstance(raw_sh, bool):
            raise ValueError("goal.self_heal 必须是布尔值")
        sh = bool(raw_sh)
        # --- subtasks（可选子任务分解，数据依赖分析用；仅作结构化透传，不强制校验内部字段）---
        raw_sub = d.get("subtasks", None)
        sub = None
        if raw_sub is not None:
            if not isinstance(raw_sub, list):
                raise ValueError("goal.subtasks 必须是子任务列表")
            sub = raw_sub
        # --- component_io（可选：节点 IO 映射；仅作结构化透传，不强制校验内部字段）---
        raw_io = d.get("component_io", None)
        comp_io = None
        if raw_io is not None:
            if not isinstance(raw_io, dict):
                raise ValueError("goal.component_io 必须是对象")
            comp_io = raw_io
        return Goal(
            capabilities=list(caps),
            constraints=dict(constraints),
            modalities=list(modalities),
            reliability=reliability,
            name=d.get("name", "unnamed-goal"),
            description=d.get("description", ""),
            tiers=dict(tiers),
            dependencies=deps,
            feedback=fb,
            redundancy=red,
            recovery=rec,
            self_heal=sh,
            subtasks=sub,
            component_io=comp_io,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "constraints": self.constraints,
            "modalities": self.modalities,
            "reliability": self.reliability,
            "tiers": self.tiers,
            "dependencies": self.dependencies,
            "feedback": self.feedback,
            "redundancy": self.redundancy,
            "recovery": self.recovery,
            "self_heal": self.self_heal,
            "subtasks": self.subtasks,
            "component_io": self.component_io,
        }
