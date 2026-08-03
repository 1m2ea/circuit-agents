"""
circuit-agents · compiler.formats
================================
补强#②：节点格式校验适配器 —— 在规划层(Router)对相邻能力节点做 I/O 格式兼容校验，
不兼容时自动插入 ADC/DAC（或通用 transcode）格式适配节点，把"格式断点"显式桥接。

设计（已与用户确认）：
 · 纯规划/拓扑增强：只增节点、不改 runtime 内核语义（format_adapter 在 SimBackend 里
   是近零成本确定性透传；RealLLMBackend 走 super 同样确定性——结构与 cost/latency 不变）。
 · 格式维度：raw（原始/非结构化，类比 analog）vs struct（结构化/离散，类比 digital）。
   每种能力声明 (consumes, produces)；边上有 producer.produces != consumer.consumes
   则插适配器：raw→struct = ADC，struct→raw = DAC，其余 = transcode。
 · 只检查"能力 DAG 有效边"（含默认串行展开）；并行([]) 无连接故无适配器；
   安全网定位：填补模板/用户未显式写 extract 时的格式断点（如 retrieve→calculate 之间）。
 · 向后兼容：spec 仅新增 "adapters" 字段记录插入了哪些适配器；runtime 行为不变。
"""
from __future__ import annotations

import re

RAW, STRUCT = "raw", "struct"

# 基础能力 → (consumes, produces)。用基础名（去 #冗余后缀）。
CAP_FORMAT = {
    "source":    (RAW, RAW),
    "power":     (RAW, RAW),
    "retrieve":  (RAW, RAW),
    "extract":   (RAW, STRUCT),
    "calculate": (STRUCT, STRUCT),
    "reason":    (STRUCT, STRUCT),
    "verify":    (STRUCT, STRUCT),
    "classify":  (STRUCT, STRUCT),
    "translate": (STRUCT, STRUCT),
    "organize":  (STRUCT, STRUCT),
    "summarize": (STRUCT, STRUCT),
}

# 合成适配器能力名前缀：Router 据此前缀识别 format_adapter 节点（区别于真实能力电阻）。
PREFIX = "fmt@"


def _base(cap: str) -> str:
    return re.sub(r"#\d+$", "", cap)


def produces(cap: str) -> str:
    return CAP_FORMAT.get(_base(cap), (RAW, RAW))[1]


def consumes(cap: str) -> str:
    return CAP_FORMAT.get(_base(cap), (RAW, RAW))[0]


def adapter_kind(src: str, dst: str):
    """raw→struct = ADC（analog→digital），struct→raw = DAC，其余 = transcode；同格式无适配。"""
    if src == dst:
        return None
    if src == RAW and dst == STRUCT:
        return "adc"
    if src == STRUCT and dst == RAW:
        return "dac"
    return "transcode"


def effective_edges(goal) -> list:
    """返回用于格式校验的"有效边"([pre, post] 能力名对)：
    · dependencies=None → 线性串联链；[] → 无（全并联）；list → 声明的 DAG 边。"""
    caps = goal.capabilities
    deps = goal.dependencies
    if deps is None:
        return [[caps[i], caps[i + 1]] for i in range(len(caps) - 1)]
    if deps == []:
        return []
    return [[a, b] for a, b in deps]


def infer_adapters(goal):
    """扫描有效边，找出格式断点并返回 (aug_caps, aug_deps, adapters_dict)。

    aug_caps : 在 goal.capabilities 基础上，每个格式断点插入一个合成适配器能力名
               （形如 "fmt@adc:retrieve>calculate"，含方向，确保唯一）。
    aug_deps : 把原边 [pre,post] 拆成 [pre,adapter] + [adapter,post]。
    adapters : {合成能力名: {"from_fmt","to_fmt","kind"}}，供 Router 渲染 format_adapter。
    """
    edges = effective_edges(goal)
    aug_caps = list(goal.capabilities)
    aug_deps = []
    adapters = {}
    for pre, post in edges:
        src = produces(pre)
        dst = consumes(post)
        kind = adapter_kind(src, dst)
        if kind is None:
            aug_deps.append([pre, post])
            continue
        token = f"{PREFIX}{kind}:{_base(pre)}>{_base(post)}"
        if token not in adapters:
            adapters[token] = {"from_fmt": src, "to_fmt": dst, "kind": kind}
            aug_caps.append(token)
        aug_deps.append([pre, token])
        aug_deps.append([token, post])
    return aug_caps, aug_deps, adapters


def selftest():
    class _G:
        def __init__(self, caps, deps=None):
            self.capabilities = list(caps)
            self.dependencies = deps

    # 1) 串行 retrieve→calculate 存在 raw→struct 断点 → 插 1 个 ADC
    g = _G(["retrieve", "calculate"])
    aug_caps, aug_deps, adapters = infer_adapters(g)
    assert len(adapters) == 1 and list(adapters.values())[0]["kind"] == "adc", adapters
    assert any(c.startswith(PREFIX) for c in aug_caps)
    print("✓ 串行断点: retrieve(raw)→calculate(struct) 自动插入 ADC 适配器")

    # 2) 良好 DAG retrieve→extract→reason 无断点（extract 已桥接）→ 0 适配器
    g2 = _G(["retrieve", "extract", "reason"],
             deps=[["retrieve", "extract"], ["extract", "reason"]])
    _, _, ad2 = infer_adapters(g2)
    assert len(ad2) == 0, ad2
    print("✓ 良好 DAG: retrieve→extract→reason 无格式断点（0 适配器）")

    # 3) 全并联 [] → 无适配器
    g3 = _G(["retrieve", "calculate"], deps=[])
    _, _, ad3 = infer_adapters(g3)
    assert len(ad3) == 0
    print("✓ 全并联 []: 无连接 → 0 适配器")

    # 4) 多重集后缀归一（retrieve#1）不影响 base 判定
    g4 = _G(["retrieve#1", "calculate#1"])
    _, _, ad4 = infer_adapters(g4)
    assert len(ad4) == 1 and list(ad4.values())[0]["kind"] == "adc"
    print("✓ 多重集后缀归一: retrieve#1→calculate#1 仍正确识别断点")

    # 5) 已存在 extract 桥接的模板（verify_report 风格）仍可能触发 adapter：
    #    retrieve→calculate 是 raw→struct，模板未含 extract 时由适配器补位
    g5 = _G(["retrieve", "calculate", "verify", "organize"],
            deps=[["retrieve", "calculate"], ["calculate", "verify"], ["verify", "organize"]])
    _, _, ad5 = infer_adapters(g5)
    assert len(ad5) == 1 and list(ad5.values())[0]["kind"] == "adc"
    print("✓ 安全网: retrieve→calculate 缺 extract 时由 ADC 适配器补位")

    print("\nformats 自检通过 ✓")


if __name__ == "__main__":
    selftest()
