"""
circuit-agents · compiler.refs
===============================
图纸式元件编号 —— 让电路像真电路图一样按「类型字母 + 序号」寻址每个元件。

真实电路图规范：同一类型的元件不止一个时，必须用元件字母加数字序号区分，
例如 灯泡 L1/L2、电阻 R1/R2、开关 S1/S2…… 同类元件不能用完全相同的符号表示。

本模块给任意 spec 的元件分配确定性图纸编号（ref）：
  - resistor  → R1, R2, …   （多个电阻并联/级联时按序编号）
  - capacitor → C1, C2, …
  - diode     → D1, D2, …
  - opamp     → U1, U2, …   （电子惯例：运放/IC 用 U）
  - adc       → ADC1, …     （质量门沿用项目命名）
  - power     → P1, …       （电源/任务源）
  - source    → SRC1, …     （信号源，避免与开关 S 冲突）
  - watchdog  → W1, …   format_adapter → F1, …   switch → S1, …  lamp → L1, …

分配规则（确定性、幂等）：
  - 按 spec 中元件的出现顺序对同类型递增，从 1 开始；
  - 不同类型各自独立计数（R1 与 C1 互不干扰）；
  - 未知类型退化：取类型名前 2 个大写字母（如 verify → VE1），仍保证可寻址。

接入点（只做展示/寻址增强，不改变执行语义）：
  - draw.py 电路图元件标注（ref 优先 + label 辅助）
  - server SSE 节点事件 / topology 输出端点附 ref
  - 拓扑编辑器与结果展示用 ref 指示元件身份
"""
from __future__ import annotations

import copy

# 元件类型 → 图纸字母前缀
TYPE_PREFIX = {
    "power": "P",            # 电源 / 任务源
    "source": "SRC",         # 信号源（外部数据），避免与 switch 的 S 冲突
    "opamp": "U",            # 运算放大器 / 调度器（电子惯例 U/IC）
    "resistor": "R",         # 电阻 / 原子步骤
    "capacitor": "C",        # 电容 / 汇合·缓冲
    "diode": "D",            # 二极管 / 单向校验
    "adc": "ADC",            # 模数转换 / 质量门（沿用项目命名，直观）
    "watchdog": "W",         # 看门狗
    "format_adapter": "F",   # 格式适配
    "switch": "S",           # 开关
    "lamp": "L",             # 灯泡 / 输出指示
    "logic_gate": "G",       # 逻辑门
    "verify": "V",           # 校验
    "buzzer": "BZ",          # 蜂鸣器 / 告警
    "transformer": "T",      # 变压器 / 电平变换
}


def _items(components):
    """统一 dict{id: comp} 与 list[comp] 两种表达为 (cid, comp) 序列（保持原顺序）。"""
    if isinstance(components, dict):
        return list(components.items())
    out = []
    for c in components or []:
        if isinstance(c, dict):
            cid = c.get("id") or c.get("name")
            out.append((cid, c))
    return out


def ref_prefix(ctype):
    """类型 → 字母前缀；未知类型退化为类型名前 2 大写（保证唯一可寻址）。"""
    if not ctype:
        return "X"
    return TYPE_PREFIX.get(ctype, ctype[:2].upper())


def build_ref_index(components):
    """给同类型元件按出现顺序编号：返回 {comp_id: "R1"}。

    确定性：同一 spec 多次调用结果一致；同类型从 1 递增、跨类型独立。
    """
    counts = {}
    index = {}
    for cid, comp in _items(components):
        if cid is None:
            continue
        prefix = ref_prefix((comp or {}).get("type"))
        counts[prefix] = counts.get(prefix, 0) + 1
        index[cid] = f"{prefix}{counts[prefix]}"
    return index


def with_refs(spec):
    """返回 spec 的浅副本，每个元件补上图纸编号字段 ref（已有 ref 则保留，幂等）。

    不改动原 spec / wires / 执行语义，仅作展示与寻址标注。
    """
    if not isinstance(spec, dict):
        return spec
    comps = spec.get("components")
    if comps is None:
        return spec
    out = copy.deepcopy(spec)
    index = build_ref_index(out.get("components"))
    if isinstance(out["components"], dict):
        for cid, comp in out["components"].items():
            if isinstance(comp, dict) and "ref" not in comp and cid in index:
                comp["ref"] = index[cid]
    else:
        for comp in out["components"]:
            if isinstance(comp, dict):
                cid = comp.get("id") or comp.get("name")
                if "ref" not in comp and cid in index:
                    comp["ref"] = index[cid]
    return out


# ---------------------------------------------------------------- selftest
def refs_selftest():
    """图纸式编号离线自检：同类型递增 / 跨类型独立 / 确定性 / fallback / with_refs 幂等。"""
    spec = {
        "components": {
            "src":   {"type": "power"},
            "sched": {"type": "opamp"},
            "a":     {"type": "resistor"},      # 初稿
            "b":     {"type": "resistor"},      # 深析 —— 同类型第二个电阻！
            "c":     {"type": "resistor"},      # 第三电阻（并联三路）
            "merge": {"type": "capacitor"},
            "d":     {"type": "diode"},
            "adc":   {"type": "adc"},
            "wg":    {"type": "watchdog"},
        },
        "wires": [["src", "a"], ["a", "merge"], ["b", "merge"], ["c", "merge"]],
    }
    idx = build_ref_index(spec["components"])
    assert idx["src"] == "P1", f"power 应 P1，实际 {idx['src']}"
    assert idx["sched"] == "U1", f"opamp 应 U1，实际 {idx['sched']}"
    assert idx["a"] == "R1" and idx["b"] == "R2" and idx["c"] == "R3", \
        f"三个电阻应 R1/R2/R3，实际 {idx['a']}/{idx['b']}/{idx['c']}"
    assert idx["merge"] == "C1", f"capacitor 应 C1，实际 {idx['merge']}"
    assert idx["d"] == "D1" and idx["adc"] == "ADC1" and idx["wg"] == "W1", \
        f"diode/adc/watchdog 编号错误: {idx['d']}/{idx['adc']}/{idx['wg']}"
    print("✓ 元件编号：同类型多实例递增（a/b/c → R1/R2/R3）+ 跨类型独立（P1/U1/C1/D1/ADC1/W1）")

    # 确定性：两次调用一致
    assert build_ref_index(spec["components"]) == idx, "编号应确定性一致"
    print("✓ 元件编号：同输入多次调用结果一致（确定性）")

    # list 形态 + 未知类型 fallback
    comps2 = [
        {"id": "x", "type": "mystery_thing"},
        {"id": "y", "type": "resistor"},
        {"id": "z", "type": "resistor"},
    ]
    idx2 = build_ref_index(comps2)
    assert idx2["x"] == "MY1", f"未知类型应退化取前 2 大写+序号（MY1），实际 {idx2['x']}"
    assert idx2["y"] == "R1" and idx2["z"] == "R2", f"list 形态编号错误 {idx2}"
    print("✓ 元件编号：未知类型 fallback（mystery_thing→MY1）+ list 形态支持")

    # with_refs：补 ref 且不污染原 spec / wires
    decorated = with_refs(spec)
    assert decorated["components"]["b"]["ref"] == "R2"
    assert "ref" not in spec["components"]["b"], "with_refs 不应改动原 spec"
    assert decorated["wires"] == spec["wires"], "with_refs 不应改动 wires"
    again = with_refs(decorated)
    assert again["components"]["b"]["ref"] == "R2", "重复 with_refs 应幂等"
    print("✓ 元件编号：with_refs 展示副本补 ref、不改原 spec/wires、幂等")

    print("compiler.refs 图纸式编号 离线自检全部通过 ✓")
    return idx


if __name__ == "__main__":
    refs_selftest()
