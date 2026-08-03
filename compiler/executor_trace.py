"""
circuit-agents · executor_trace
===========================
把 CircuitExecutor 的「观察窗（B）」事件流 (+ 拓扑) 渲染成可打开查看的 HTML 文件，
让用户在控制台不可见的环境下，也能看清「技能在干什么」。

产物：examples/executor_trace.html
  · 拓扑图：节点按最终状态着色（绿✓完成 / 红失败 / 橙虚框=补数闭环 / 紫⚠=触发 3.5 进化）
  · 时间线面板：每个事件一行（带相对时间戳 + 类型色点 + 详情）
  · 走查动画：播放/暂停 + 倍速 + 进度条，按事件发生顺序高亮对应节点（橙/紫脉冲）

复用 circuit-planner 的四色复盘约定：
  绿 = 完成/自愈升级   红 = 失败/慢   橙虚 = 重试或曾失步   紫⚠ = 看门狗/进化劣化
"""
from __future__ import annotations

import html
import os

# ---- 事件类型 → 中文标签 / 色类（四色约定）----
_EVENT_META = {
    "start":          ("开始执行",            "green"),
    "layer_start":    ("层开始",              "blue"),
    "node_start":     ("节点开始",            "blue"),
    "gate_fail":      ("线性关系闸未过(缺数据)", "red"),
    "skill_call":     ("技能调用",            "orange"),
    "skill_return":   ("技能返回",            "green"),
    "skill_error":    ("技能错误",            "red"),
    "skill_skip":     ("技能跳过",            "orange"),
    "retry":          ("补数后重试",          "orange"),
    "node_done":      ("节点完成",            "green"),
    "layer_done":     ("层完成",              "blue"),
    "evolve_detect":  ("检测可进化(检索结果)",  "purple"),
    "evolve_spawn":   ("生成分析子电路",       "purple"),
    "done":           ("执行完成",            "green"),
}

# 布局常量
_COL_W = 230
_ROW_H = 96
_M_X = 60
_M_Y = 44
_N_W = 156
_N_H = 58


def _esc(s):
    return html.escape(str(s), quote=True)


def _node_status(executor, cid):
    sig = executor._results.get(cid)
    if sig is None:
        return "neutral", None
    if sig.ok:
        return "ok", sig
    return "fail", sig


def _layout(executor):
    layers = executor.circuit.layers()
    max_rows = max((len(l) for l in layers), default=1)
    pos = {}
    for li, layer in enumerate(layers):
        offset = (max_rows - len(layer)) / 2.0
        for yi, cid in enumerate(layer):
            pos[cid] = (li, yi + offset)
    return layers, pos, max_rows


def _event_detail(ev):
    """把事件字段拼成一行可读详情。"""
    parts = []
    for k, v in ev.items():
        if k in ("t", "type", "scope", "node", "ctype", "label"):
            continue
        if k == "args":
            v = _trunc(str(v), 60)
        elif k == "missing":
            v = ", ".join(v) if isinstance(v, list) else v
        elif k == "skill":
            v = f"@{v}"
        else:
            v = _trunc(str(v), 40)
        parts.append(f"{k}={v}")
    return "  ".join(parts)


def _trunc(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def render_executor_trace(executor, title=None, out_path=None):
    """渲染执行追踪产物 HTML。返回写出路径；out_path 缺省为 examples/executor_trace.html。"""
    spec_name = executor.circuit.spec.get("name", "unnamed")
    title = title or f"Executor 观察窗 · {spec_name}"

    layers, pos, max_rows = _layout(executor)
    evolved_from = executor._evolved_from_node

    # ---- 画布尺寸 ----
    width = _M_X * 2 + max(0, len(layers) - 1) * _COL_W + _N_W
    height = _M_Y * 2 + max(0, max_rows - 1) * _ROW_H + _N_H

    # ---- 连线 ----
    wires = []
    for a, b in executor.circuit.forward:
        if a not in pos or b not in pos:
            continue
        ax = _M_X + pos[a][0] * _COL_W + _N_W
        ay = _M_Y + pos[a][1] * _ROW_H + _N_H / 2
        bx = _M_X + pos[b][0] * _COL_W
        by = _M_Y + pos[b][1] * _ROW_H + _N_H / 2
        wires.append(f'<line class="wire" x1="{ax:.0f}" y1="{ay:.0f}" '
                     f'x2="{bx:.0f}" y2="{by:.0f}" marker-end="url(#arrow)"/>')

    # ---- 节点 ----
    nodes_svg = []
    for cid, (li, yi) in pos.items():
        x = _M_X + li * _COL_W
        y = _M_Y + yi * _ROW_H
        status, sig = _node_status(executor, cid)
        cls = ["node", status]
        if cid in executor._filled_nodes:
            cls.append("filled")
        if cid == evolved_from:
            cls.append("evolved")
        comp = executor.circuit.components[cid]
        label = _esc(comp.get("label") or cid)
        ctype = _esc(comp.get("type", ""))
        if status == "ok":
            mark = "✓"
            qtxt = f"ok · q={sig.quality:.2f}"
        elif status == "fail":
            mark = "✗"
            qtxt = "失败"
        else:
            mark = "·"
            qtxt = "—"
        # 触发进化的节点加紫⚠角标
        badge = ('<text class="badge" x="{x:.0f}" y="{y:.0f}">⚠</text>'.format(
            x=x + _N_W - 6, y=y - 4)) if cid == evolved_from else ""
        nodes_svg.append(
            f'<g id="node-{_esc(cid)}" class="{" ".join(cls)}">'
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{_N_W}" height="{_N_H}" rx="9"/>'
            f'<text class="nlabel" x="{x + 12:.0f}" y="{y + 23:.0f}">{label}</text>'
            f'<text class="nmeta" x="{x + 12:.0f}" y="{y + 42:.0f}">{ctype} · {_esc(qtxt)}</text>'
            f'<text class="nmark" x="{x + _N_W - 16:.0f}" y="{y + 24:.0f}">{mark}</text>'
            f'{badge}'
            f'</g>'
        )

    # ---- 时间线行 ----
    timeline_rows = []
    max_t = 0.0
    for ev in executor._events:
        t = ev.get("t", 0.0)
        max_t = max(max_t, t)
        et = ev.get("type", "?")
        label, color = _EVENT_META.get(et, (et, "blue"))
        scope = ev.get("scope", "")
        scope_tag = f'<span class="scope">[{_esc(scope)}]</span> ' if scope else ""
        node = ev.get("node")
        highlight_node = evolved_from if scope == "evolve" else node
        detail = _event_detail(ev)
        detail_html = f'<span class="detail">{_esc(detail)}</span>' if detail else ""
        dot = f'<span class="dot {color}"></span>'
        timeline_rows.append(
            f'<div class="ev" data-t="{t:.1f}" data-node="{_esc(highlight_node or "")}" '
            f'data-scope="{_esc(scope)}">'
            f'{dot}<span class="t">+{t:.0f}ms</span> '
            f'{scope_tag}<span class="etype">{_esc(label)}</span> {detail_html}</div>'
        )

    total_cost = round(sum(s.cost for s in executor._results.values()), 4)
    total_lat = round(max((s.latency_ms for s in executor._results.values()), default=0.0), 1)
    evolved = executor.state.get("_evolved")
    summary = (f'<span class="k">总耗时</span> {max_t:.0f}ms &nbsp;·&nbsp; '
               f'<span class="k">总成本</span> ${total_cost:.4f} &nbsp;·&nbsp; '
               f'<span class="k">末级延迟</span> {total_lat}ms &nbsp;·&nbsp; '
               f'<span class="k">3.5 进化</span> '
               f'{"是 (" + _esc(evolved["spec_name"]) + ")" if evolved else "否"}')

    out_path = out_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "examples", "executor_trace.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    from string import Template
    html_doc = Template(_HTML_TEMPLATE).substitute(
        title=_esc(title),
        summary=summary,
        width=width, height=height,
        wires="\n".join(wires),
        nodes="\n".join(nodes_svg),
        timeline="\n".join(timeline_rows),
        max_t=max_t + 300,
        evolved_from=_esc(evolved_from or ""),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out_path


# ---- HTML 模板（自包含、离线可用、浅色卡片风格；用 $ 占位以兼容 CSS/JS 花括号）----
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root {
    --green:#2e7d32; --red:#c62828; --orange:#ef6c00; --purple:#6a1b9a;
    --blue:#1565c0; --ink:#1f2933; --muted:#66707a; --line:#d7dde3;
    --bg:#f5f7f9; --card:#ffffff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 -apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif; }
  header { padding:18px 24px; background:var(--card); border-bottom:1px solid var(--line); }
  h1 { margin:0 0 6px; font-size:18px; }
  .summary { color:var(--muted); font-size:13px; }
  .summary .k { color:var(--ink); font-weight:600; }
  .wrap { display:flex; gap:16px; padding:16px 24px; align-items:flex-start; flex-wrap:wrap; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px; }
  .topo-card { flex:1 1 460px; min-width:340px; overflow:auto; }
  .tl-card { flex:1 1 380px; min-width:320px; max-height:78vh; display:flex; flex-direction:column; }
  h2 { font-size:13px; margin:2px 6px 10px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  svg { display:block; }
  /* 节点状态色 */
  .node rect { fill:#eef2f5; stroke:#9aa5b1; stroke-width:1.5; transition:stroke .2s, stroke-width .2s, filter .2s; }
  .node.ok rect { fill:#e8f5e9; stroke:var(--green); }
  .node.fail rect { fill:#ffebee; stroke:var(--red); }
  .node.filled rect { stroke:var(--orange); stroke-width:2.5; stroke-dasharray:5 3; }
  .node.evolved rect { stroke:var(--purple); stroke-width:2.5; }
  .node.active rect { stroke:var(--blue); stroke-width:4; filter:drop-shadow(0 0 6px rgba(21,101,192,.5)); }
  .node.evolve-flash rect { stroke:var(--purple); stroke-width:4; filter:drop-shadow(0 0 8px rgba(106,27,154,.6)); }
  .nlabel { font-size:13px; font-weight:600; fill:var(--ink); }
  .nmeta { font-size:10.5px; fill:var(--muted); }
  .nmark { font-size:16px; font-weight:700; fill:var(--ink); text-anchor:middle; }
  .node.ok .nmark { fill:var(--green); }
  .node.fail .nmark { fill:var(--red); }
  .badge { font-size:15px; fill:var(--purple); font-weight:700; }
  .wire { stroke:#b0b8c1; stroke-width:1.6; }
  /* 时间线 */
  .controls { display:flex; gap:8px; align-items:center; padding:4px 6px 10px; }
  .controls button { background:var(--blue); color:#fff; border:0; border-radius:7px;
                     padding:6px 14px; font-size:13px; cursor:pointer; }
  .controls button.sec { background:#eef2f5; color:var(--ink); }
  .controls select { padding:5px 8px; border-radius:7px; border:1px solid var(--line); }
  .controls input[type=range] { flex:1; }
  #tl { overflow:auto; padding-right:4px; }
  .ev { padding:5px 8px; border-left:3px solid transparent; border-radius:6px; margin:2px 0;
        font-size:12.5px; opacity:.45; transition:opacity .15s, background .15s; }
  .ev.played { opacity:1; background:#f0f6ff; border-left-color:var(--blue); }
  .ev .t { color:var(--muted); font-variant-numeric:tabular-nums; margin-right:6px; }
  .ev .scope { color:var(--purple); font-weight:600; }
  .ev .etype { font-weight:600; }
  .ev .detail { color:var(--muted); }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:middle; }
  .dot.green { background:var(--green); } .dot.red { background:var(--red); }
  .dot.orange { background:var(--orange); } .dot.purple { background:var(--purple); }
  .dot.blue { background:var(--blue); }
  .legend { display:flex; gap:14px; flex-wrap:wrap; padding:6px 8px 0; font-size:12px; color:var(--muted); }
  .legend span { display:inline-flex; align-items:center; gap:5px; }
  .legend i { width:11px; height:11px; border-radius:3px; display:inline-block; }
</style>
</head>
<body>
<header>
  <h1>$title</h1>
  <div class="summary">$summary</div>
</header>
<div class="wrap">
  <div class="card topo-card">
    <h2>拓扑 · 节点状态</h2>
    <svg id="topo" width="$width" height="$height" viewBox="0 0 $width $height">
      <defs>
        <marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5"
                orient="auto" markerUnits="userSpaceOnUse">
          <path d="M0,0 L9,4.5 L0,9 Z" fill="#b0b8c1"/>
        </marker>
      </defs>
      $wires
      $nodes
    </svg>
    <div class="legend">
      <span><i style="background:#e8f5e9;border:1.5px solid var(--green)"></i>完成 ✓</span>
      <span><i style="background:#ffebee;border:1.5px solid var(--red)"></i>失败 ✗</span>
      <span><i style="background:#eef2f5;border:2.5px dashed var(--orange)"></i>补数闭环</span>
      <span><i style="background:#eef2f5;border:2.5px solid var(--purple)"></i>触发 3.5 进化 ⚠</span>
    </div>
  </div>
  <div class="card tl-card">
    <h2>时间线 · 走查</h2>
    <div class="controls">
      <button id="play">▶ 播放</button>
      <button id="reset" class="sec">↺ 重置</button>
      <select id="speed">
        <option value="0.5">0.5×</option>
        <option value="1" selected>1×</option>
        <option value="2">2×</option>
        <option value="4">4×</option>
      </select>
      <input id="seek" type="range" min="0" max="$max_t" value="0" step="10">
    </div>
    <div id="tl">$timeline</div>
  </div>
</div>
<script>
  const rows = Array.from(document.querySelectorAll('#tl .ev'));
  const maxT = $max_t;
  const evolvedFrom = "$evolved_from";
  let vit = 0, playing = false, speed = 1, timer = null;

  function nodeEl(cid) { return cid ? document.getElementById('node-'+cid) : null; }
  function clearHi() {
    document.querySelectorAll('.node.active').forEach(n=>n.classList.remove('active'));
    document.querySelectorAll('.node.evolve-flash').forEach(n=>n.classList.remove('evolve-flash'));
  }
  function applyTo(t) {
    let last = null;
    rows.forEach(r=>{
      const rt = parseFloat(r.dataset.t);
      if (rt <= t) { r.classList.add('played'); last = r; }
      else { r.classList.remove('played'); }
    });
    clearHi();
    if (last) {
      const scope = last.dataset.scope;
      if (scope === 'evolve' && evolvedFrom) {
        const el = nodeEl(evolvedFrom); if (el) el.classList.add('evolve-flash');
      } else {
        const el = nodeEl(last.dataset.node); if (el) el.classList.add('active');
      }
      last.scrollIntoView({block:'nearest'});
    }
  }
  function setSeek(v) { vit = v; document.getElementById('seek').value = v; applyTo(vit); }
  function tick() { vit += 40*speed; if (vit >= maxT) { vit = maxT; pause(); } setSeek(vit); }
  function play() { if (playing) return; if (vit >= maxT) setSeek(0); playing = true;
    document.getElementById('play').textContent = '⏸ 暂停';
    timer = setInterval(tick, 40); }
  function pause() { playing = false; clearInterval(timer); timer = null;
    document.getElementById('play').textContent = '▶ 播放'; }
  document.getElementById('play').onclick = () => playing ? pause() : play();
  document.getElementById('reset').onclick = () => { pause(); setSeek(0); };
  document.getElementById('speed').onchange = e => speed = parseFloat(e.target.value);
  document.getElementById('seek').oninput = e => { pause(); setSeek(parseFloat(e.target.value)); };
  applyTo(0);
</script>
</body>
</html>
"""
