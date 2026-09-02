"""draw.py — render a Circuit DSL topology as a schematic SVG.

Usage:
    python draw.py                 # draws all 4 examples into diagrams/
    python draw.py examples/parallel.json
"""
import os
import random
import sys

from runtime import load, Circuit, SimBackend
from compiler.refs import build_ref_index

NODE_W, NODE_H = 150, 80
VX, VY = 190, 150
MARGIN_X, MARGIN_TOP, MARGIN_BOT = 60, 60, 90
# 第二层⑤：电阻(步骤)数超过该阈值 → 启用"共享上下文总线"表示
BUS_THRESHOLD = 10
# 第五层(⑤) 复盘标注：节点延迟 ≥ 该阈值(ms) 视为"慢节点"（large 档 1500 / tool 档 800）
SLOW_THRESHOLD = 1000

TYPE_COLOR = {
    "power": "#c0392b", "opamp": "#2980b9", "resistor": "#27ae60",
    "capacitor": "#8e44ad", "diode": "#d35400", "adc": "#16a085",
    "watchdog": "#7f8c8d", "bridge_rectifier": "#2c3e50",
    "source": "#e67e22", "logic_gate": "#34495e",
    "format_adapter": "#f39c12", "switch": "#95a5a6",
}
TYPE_LABEL = {
    "power": "POWER", "opamp": "OP-AMP", "resistor": "RESISTOR",
    "capacitor": "CAPACITOR", "diode": "DIODE", "adc": "ADC",
    "watchdog": "WATCHDOG", "bridge_rectifier": "BRIDGE",
    "source": "SOURCE", "logic_gate": "LOGIC GATE",
    "format_adapter": "ADAPT", "switch": "SWITCH",
}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def glyph(t, cx, cy, comp=None):
    g = TYPE_COLOR.get(t, "#333")
    if t == "power":                       # battery
        out = ""
        for i, w in enumerate([26, 14, 26, 14]):
            yy = cy - 13 + i * 9
            out += f'<line x1="{cx-w/2}" y1="{yy}" x2="{cx+w/2}" y2="{yy}" stroke="{g}" stroke-width="3"/>'
        return out
    if t == "opamp":                       # triangle + -,+
        return (f'<polygon points="{cx-16},{cy-16} {cx-16},{cy+16} {cx+18},{cy}" '
                f'fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx-13}" y="{cy-4}" font-size="11" fill="{g}">+</text>'
                f'<text x="{cx-13}" y="{cy+14}" font-size="11" fill="{g}">&#8722;</text>')
    if t == "resistor":                    # zigzag
        pts = []
        for i in range(7):
            x = cx - 22 + 44 * i / 6
            y = cy + (8 if i % 2 == 0 else -8)
            pts.append(f"{x:.1f},{y:.1f}")
        return f'<polyline points="{" ".join(pts)}" fill="none" stroke="{g}" stroke-width="2.5"/>'
    if t == "capacitor":                   # two plates
        return (f'<line x1="{cx}" y1="{cy-16}" x2="{cx}" y2="{cy-3}" stroke="{g}" stroke-width="2.5"/>'
                f'<line x1="{cx-18}" y1="{cy-3}" x2="{cx+18}" y2="{cy-3}" stroke="{g}" stroke-width="3"/>'
                f'<line x1="{cx-18}" y1="{cy+3}" x2="{cx+18}" y2="{cy+3}" stroke="{g}" stroke-width="3"/>'
                f'<line x1="{cx}" y1="{cy+3}" x2="{cx}" y2="{cy+16}" stroke="{g}" stroke-width="2.5"/>')
    if t == "diode":                       # triangle + bar
        return (f'<polygon points="{cx-16},{cy-14} {cx-16},{cy+14} {cx+14},{cy}" '
                f'fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<line x1="{cx+14}" y1="{cy-14}" x2="{cx+14}" y2="{cy+14}" stroke="{g}" stroke-width="3"/>')
    if t == "adc":
        return (f'<rect x="{cx-22}" y="{cy-16}" width="44" height="32" rx="4" fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx}" y="{cy+5}" font-size="13" fill="{g}" text-anchor="middle" font-weight="bold">ADC</text>')
    if t == "watchdog":
        return (f'<rect x="{cx-22}" y="{cy-16}" width="44" height="32" rx="4" fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx}" y="{cy+5}" font-size="12" fill="{g}" text-anchor="middle" font-weight="bold">WD</text>')
    if t == "bridge_rectifier":
        return (f'<rect x="{cx-26}" y="{cy-16}" width="52" height="32" rx="4" fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx}" y="{cy+5}" font-size="10" fill="{g}" text-anchor="middle" font-weight="bold">BRIDGE</text>')
    if t == "source":
        return (f'<circle cx="{cx}" cy="{cy}" r="17" fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx}" y="{cy+5}" font-size="14" fill="{g}" text-anchor="middle" font-weight="bold">&#9673;</text>')
    if t == "logic_gate":
        return (f'<rect x="{cx-20}" y="{cy-14}" width="40" height="28" rx="3" fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx}" y="{cy+5}" font-size="14" fill="{g}" text-anchor="middle">&amp;</text>')
    if t == "format_adapter":                  # 第二层②：格式适配节点（ADC/DAC/transcode）
        kind = comp.get("kind", "").upper()
        return (f'<rect x="{cx-26}" y="{cy-14}" width="52" height="28" rx="5" fill="none" stroke="{g}" stroke-width="2.5"/>'
                f'<text x="{cx}" y="{cy+5}" font-size="11" fill="{g}" text-anchor="middle" '
                f'font-weight="bold">{kind or "ADAPT"}</text>')
    if t == "switch":                          # 开关：触点 + 拨杆（on=接通 / off=断开）
        state = (comp or {}).get("state", "on")
        if state == "off":
            return (f'<line x1="{cx-24}" y1="{cy}" x2="{cx-3}" y2="{cy}" stroke="{g}" stroke-width="2.5"/>'
                    f'<line x1="{cx+3}" y1="{cy}" x2="{cx+24}" y2="{cy}" stroke="{g}" stroke-width="2.5"/>'
                    f'<line x1="{cx-3}" y1="{cy}" x2="{cx+8}" y2="{cy-10}" stroke="{g}" stroke-width="2.5"/>'
                    f'<circle cx="{cx-3}" cy="{cy}" r="3" fill="{g}"/>'
                    f'<circle cx="{cx+3}" cy="{cy}" r="3" fill="{g}"/>')
        return (f'<line x1="{cx-24}" y1="{cy}" x2="{cx+24}" y2="{cy}" stroke="{g}" stroke-width="2.5"/>'
                f'<circle cx="{cx-3}" cy="{cy}" r="3" fill="{g}"/>'
                f'<circle cx="{cx+3}" cy="{cy}" r="3" fill="{g}"/>')
    return ""


def param_text(comp):
    t = comp.get("type")
    if t == "resistor":
        return f'model={comp.get("model","?")}'
    if t == "adc":
        return f'thr={comp.get("threshold","?")}'
    if t == "source":
        return f'q={comp.get("quality","?")}'
    if t == "opamp":
        return "clarify" if comp.get("spec_clarify") else "pass"
    if t == "format_adapter":
        return f'{comp.get("from_fmt","?")}→{comp.get("to_fmt","?")}'
    if t == "switch":
        return "ON" if comp.get("state", "on") != "off" else "OFF"
    return ""


def draw(spec, out_path, exec_result=None):
    circ = Circuit(spec, SimBackend(random.Random(0)))
    layers = circ.layers()
    comps = circ.components

    pos = {}
    for li, layer in enumerate(layers):
        y = MARGIN_TOP + li * VY + NODE_H / 2
        for i, cid in enumerate(layer):
            pos[cid] = (i * VX, y)

    max_nodes = max(len(l) for l in layers)
    # 第二层⑤改进：总线按"拓扑形态"触发——任一层含 ≥3 个并行电阻(步骤)节点即启用总线表示；
    # 同时保留旧的大规模阈值(步数>BUS_THRESHOLD)作为兜底。
    resistor_count = sum(1 for c in comps.values() if c.get("type") == "resistor")
    parallel_bus = any(
        sum(1 for cid in layer if comps.get(cid, {}).get("type") == "resistor") >= 3
        for layer in layers
    )
    use_bus = resistor_count > BUS_THRESHOLD or parallel_bus
    BUS_OFFSET = 52 if use_bus else 0

    width = max(max_nodes * VX, VX) + MARGIN_X * 2 + BUS_OFFSET
    height = (len(layers) - 1) * VY + NODE_H + MARGIN_TOP + MARGIN_BOT
    x_shift = width / 2 - ((max_nodes - 1) * VX) / 2
    for cid in pos:
        x, y = pos[cid]
        pos[cid] = (x + x_shift + BUS_OFFSET, y)

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">']
    p.append('''<defs>
      <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
        <path d="M0,0 L8,3 L0,6 Z" fill="#444"/></marker>
      <marker id="arrowF" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
        <path d="M0,0 L8,3 L0,6 Z" fill="#c0392b"/></marker>
    </defs>''')
    p.append(f'<rect width="{width}" height="{height}" fill="#fdfdfd"/>')

    # forward wires
    for a, b in circ.forward:
        if a in pos and b in pos:
            x1, y1 = pos[a]; x2, y2 = pos[b]
            sx, sy, ex, ey = x1, y1 + NODE_H / 2, x2, y2 - NODE_H / 2
            midy = (sy + ey) / 2
            p.append(f'<path d="M{sx},{sy} C{sx},{midy} {ex},{midy} {ex},{ey}" '
                     f'fill="none" stroke="#444" stroke-width="1.6" marker-end="url(#arrow)"/>')

    # feedback loop
    fb = spec.get("feedback")
    if fb and fb["from"] in pos and fb["to"] in pos:
        x1, y1 = pos[fb["from"]]; x2, y2 = pos[fb["to"]]
        sx, sy, ex, ey = x1 - NODE_W / 2, y1, x2 - NODE_W / 2, y2
        left = min(sx, ex) - 70
        p.append(f'<path d="M{sx},{sy} C{left},{sy} {left},{ey} {ex},{ey}" '
                 f'fill="none" stroke="#c0392b" stroke-width="2" stroke-dasharray="6 4" '
                 f'marker-end="url(#arrowF)"/>')
        p.append(f'<text x="{left-6}" y="{(sy+ey)/2}" font-size="12" fill="#c0392b" '
                 f'text-anchor="middle" transform="rotate(-90 {left-6} {(sy+ey)/2})">retry &#215;{fb["max_iter"]}</text>')

    # nodes —— 图纸式标注：每元件标「编号 R1/C1 + 功能名」，同类型多实例一目了然
    refs = build_ref_index(comps)
    for cid, (x, y) in pos.items():
        comp = comps[cid]
        t = comp.get("type")
        col = TYPE_COLOR.get(t, "#333")
        bx, by = x - NODE_W / 2, y - NODE_H / 2
        p.append(f'<rect x="{bx}" y="{by}" width="{NODE_W}" height="{NODE_H}" rx="10" '
                 f'fill="#ffffff" stroke="{col}" stroke-width="2.5"/>')
        p.append(f'<text x="{x}" y="{by+16}" font-size="10" fill="{col}" text-anchor="middle" '
                 f'font-weight="bold" letter-spacing="1">{TYPE_LABEL.get(t, t.upper())}</text>')
        p.append(glyph(t, x, y - 4, comp))
        ref = refs.get(cid, "")
        label = esc(comp.get("label", ""))[:12]
        if ref:
            p.append(f'<text x="{x}" y="{by+NODE_H-14}" font-size="13" fill="{col}" '
                     f'text-anchor="middle">'
                     f'<tspan font-weight="bold" fill="{col}">{ref}</tspan>'
                     f'<tspan fill="#222" dx="6">{label}</tspan></text>')
        else:
            p.append(f'<text x="{x}" y="{by+NODE_H-14}" font-size="12" fill="#222" '
                     f'text-anchor="middle">{label or esc(cid)}</text>')
        pt = param_text(comp)
        if pt:
            p.append(f'<text x="{x}" y="{by+NODE_H-2}" font-size="9" fill="#888" '
                     f'text-anchor="middle">{esc(pt)}</text>')

    p.append(f'<text x="{width/2}" y="24" font-size="15" fill="#222" text-anchor="middle" '
             f'font-weight="bold">{esc(spec.get("name",""))}</text>')

    # 共享上下文总线（第二层⑤ · 按形态触发）：任一层含 ≥3 个并行电阻即画一条贯穿各层的背板总线，
    # 每个阶段用虚线短桩挂到总线——纯拓扑压缩表示，不改 runtime 语义。
    # 并行层(≥3电阻)的每个电阻都挂到总线，更真实反映"多元件挂在同一总线上"。
    if use_bus:
        bus_x = 28
        top_y = min(y for _, y in pos.values())
        bot_y = max(y for _, y in pos.values())
        p.append(f'<line x1="{bus_x}" y1="{top_y}" x2="{bus_x}" y2="{bot_y}" '
                 f'stroke="#8e44ad" stroke-width="4" stroke-linecap="round"/>')
        for layer in layers:
            rnodes = [cid for cid in layer if comps.get(cid, {}).get("type") == "resistor"]
            if len(rnodes) >= 3:
                for cid in rnodes:
                    x, y = pos[cid]
                    p.append(f'<line x1="{bus_x}" y1="{y}" x2="{x - NODE_W/2}" y2="{y}" '
                             f'stroke="#8e44ad" stroke-width="1.6" stroke-dasharray="3 3"/>')
            else:
                cid = layer[0]
                x, y = pos[cid]
                p.append(f'<line x1="{bus_x}" y1="{y}" x2="{x - NODE_W/2}" y2="{y}" '
                         f'stroke="#8e44ad" stroke-width="1.6" stroke-dasharray="3 3"/>')
        p.append(f'<text x="{bus_x}" y="{top_y - 14}" font-size="11" fill="#8e44ad" '
                 f'text-anchor="middle" transform="rotate(-90 {bus_x} {top_y - 14})">共享上下文总线</text>')

    # ---- 第五层(⑤) 复盘标注：依据真实/仿真执行遥测叠加 ----
    # 慢节点=红框(latency≥阈值) / 重试或曾失步=橙虚框(ok=False 或在 healed 中) /
    # 自愈升级=绿✓标(在 self_healed 中) / 看门狗劣化=紫⚠标(watchdog.degraded)。
    # 无 exec_result 时（纯规划/离线）不画任何标注，行为与旧版一致。
    if exec_result:
        comps_ex = exec_result.get("components", {})
        healed = exec_result.get("self_healed", {}) or {}
        wd = exec_result.get("watchdog", {}) or {}
        for cid, (x, y) in pos.items():
            s = comps_ex.get(cid)
            if not s:
                continue
            bx, by = x - NODE_W / 2, y - NODE_H / 2
            lat = s.get("latency_ms", 0)
            ok = s.get("ok", True)
            if lat >= SLOW_THRESHOLD:           # 慢节点：红框
                p.append(f'<rect x="{bx-5}" y="{by-5}" width="{NODE_W+10}" height="{NODE_H+10}" '
                         f'rx="12" fill="none" stroke="#c0392b" stroke-width="3"/>')
            if (not ok) or (cid in healed):     # 重试/曾失步或自愈升级：橙虚框
                p.append(f'<rect x="{bx-3}" y="{by-3}" width="{NODE_W+6}" height="{NODE_H+6}" '
                         f'rx="11" fill="none" stroke="#e67e22" stroke-width="2.5" '
                         f'stroke-dasharray="5 3"/>')
            if cid in healed:                   # 自愈升级：右上角绿✓
                p.append(f'<circle cx="{bx+NODE_W-9}" cy="{by+9}" r="8" fill="#27ae60"/>')
                p.append(f'<text x="{bx+NODE_W-9}" y="{by+13}" font-size="11" fill="#fff" '
                         f'text-anchor="middle" font-weight="bold">&#10003;</text>')
            if wd.get(cid, {}).get("degraded"):  # 看门狗劣化：左上角紫⚠
                p.append(f'<text x="{bx+11}" y="{by+15}" font-size="16" fill="#8e44ad" '
                         f'font-weight="bold" text-anchor="middle">&#9888;</text>')
        # 颜色图例（右下角）
        lx = width - 232
        ly = height - 70
        p.append(f'<rect x="{lx-8}" y="{ly-16}" width="224" height="80" rx="6" '
                 f'fill="#ffffff" stroke="#ccc" stroke-width="1"/>')
        legend = [
            ("#c0392b", f"红框 = 慢节点(延迟≥{SLOW_THRESHOLD}ms)"),
            ("#e67e22", "橙虚框 = 重试/曾失步"),
            ("#27ae60", "绿✓ = 自愈升级替换"),
            ("#8e44ad", "紫⚠ = 看门狗劣化(跨轮)"),
        ]
        for i, (col, txt) in enumerate(legend):
            ry = ly + i * 18
            p.append(f'<rect x="{lx}" y="{ry-10}" width="13" height="13" rx="2" fill="{col}"/>')
            p.append(f'<text x="{lx+20}" y="{ry}" font-size="11" fill="#333">{txt}</text>')

    p.append('</svg>')

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(p))
    return out_path


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "diagrams")
    os.makedirs(out_dir, exist_ok=True)
    if len(sys.argv) > 1:
        paths = [sys.argv[1]]
    else:
        paths = [os.path.join(here, "examples", f) for f in
                 ["series.json", "parallel.json", "feedback.json", "bridge_rectifier.json"]]
    for path in paths:
        spec = load(path)
        name = spec.get("name", os.path.splitext(os.path.basename(path))[0])
        out = os.path.join(out_dir, name + ".svg")
        draw(spec, out)
        print("wrote", out)


if __name__ == "__main__":
    main()
