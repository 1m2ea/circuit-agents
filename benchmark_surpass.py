"""circuit-agents · 「超越」纵向基准（benchmark_surpass）
=================================================
目的：把"它能超越你(assistant)"从口号变成可复现的数字。

设计（诚实边界写在脸上）：
 · 任务类 = 翻译（它的强项：术语/风格 lessons 最容易累积）。
 · 同一批固定任务，跑 k 轮，双条件对照：
     WITH    = memory_enabled=True，跨轮共享持久记忆（教训逐轮累积、注入）
     WITHOUT = memory_enabled=False，每轮全新（等价于"每轮清零的我"）
 · 每轮采集：任务覆盖率(术语命中) + 可用 lesson 数。
 · 裁决：WITH 曲线是否越过 WITHOUT（"相信临界点"），且抬升能否归因到 lesson 数。

三种 backend（--backend）：
 · lesson-sim（默认，沙箱安全）：继承 RealLLMBackend，**复用其真实的 lessons 注入路径**
     （_build_messages → _lessons_block），只把"模型推理"换成确定性模拟——
     读到注入的历史教训就应用正确术语，否则朴素漏术语。
     ⚠ 这是"一个称职模型会消费提示"的**模型**，不是真值；仅用于验证循环接线正确、曲线形状成立。
 · sim：纯 SimBackend。预期两条都平 → 证明"没有 lesson 消费就没有免费午餐"。
 · real：真 LLM（DeepSeek/OpenAI）。需本机 DEEPSEEK_API_KEY；产出才是真数字。

运行：
   python benchmark_surpass.py --backend lesson-sim
   python benchmark_surpass.py --backend real --rounds 6      # 用户本机，有 key
产物：benchmark_results.csv / benchmark_report.md / benchmark_curve.html
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt
from runtime import Circuit, CircuitExecutor, Signal
from compiler.backend_llm import RealLLMBackend
from compiler.compile import compile_goal
from compiler.goal import Goal
from compiler.topology_memory import TopologyMemory


# ---------------------------------------------------------------------------
# 固定任务集（翻译强项；gold_term 为必须命中的关键术语，评分独立校验）
# ---------------------------------------------------------------------------
TRANSLATION_TASKS = [
    {"id": "t1", "src": "The resistor limits current in the circuit.",
     "gold_term": "电阻", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t2", "src": "The capacitor stores electric charge temporarily.",
     "gold_term": "电容", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t3", "src": "The inductor opposes changes in current.",
     "gold_term": "电感", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t4", "src": "The diode allows current to flow in one direction.",
     "gold_term": "二极管", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t5", "src": "The transistor amplifies the input signal.",
     "gold_term": "晶体管", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t6", "src": "The oscillator generates a periodic waveform.",
     "gold_term": "振荡器", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t7", "src": "The transformer couples energy between coils.",
     "gold_term": "变压器", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
    {"id": "t8", "src": "The gate drives the power switch with pwm.",
     "gold_term": "栅极", "goal": "翻译 AI/电路领域英文句子为中文，使用准确术语"},
]

ADC_THRESHOLD = 0.8  # 质量门：未命中关键术语(质量 0.3) → 失败 → 沉淀教训


# ---------------------------------------------------------------------------
# lesson-sim 后端：复用真实 lessons 注入路径，只替换"推理"
# ---------------------------------------------------------------------------
class LessonAwareSimBackend(RealLLMBackend):
    """电阻节点：复用父类 _build_messages（含 _lessons_block 真实注入），
    但不再触网——按"是否注入了历史教训"决定输出是否应用正确术语。
    这是模型，不是真值；仅用于验证循环接线与曲线形状。"""

    def run(self, comp, inputs):
        if comp.get("type") != "resistor":
            return super().run(comp, inputs)  # 结构件走确定性实现，零改动
        inp = max((s.quality for s in inputs if s.ok), default=0.0)
        if inp <= 0.0:
            return Signal(value=None, quality=0.0, ok=False,
                          cost=0.0, latency_ms=0.0, meta={"open": "no_input"})
        # —— 真实注入路径：教训已织进 comp["memory_lessons"]（由 _weave_lessons 写入）——
        lessons = comp.get("memory_lessons") or []
        _ = self._build_messages(comp, inputs)  # 走真实组装，证明接线可用（结果不发送）
        has_lesson = bool(lessons)
        gold = comp.get("gold_term") or ""
        src = comp.get("src_text") or ""
        if has_lesson and gold:
            value = f"[正确译文] 对『{src}』的译文中准确落实了领域术语约定（{gold}）"
            ok, quality = True, 1.0
        else:
            value = f"[朴素译文] 对『{src}』仅做字面直译，未落实领域术语约定"
            ok, quality = False, 0.3
        meta = {"lesson_aware": has_lesson, "gold": gold,
                "model": "lesson-sim"}
        if not ok:
            meta["open"] = "term_miss"  # 供 _auto_lesson 规则式沉淀
        return Signal(value=value, quality=quality, ok=ok,
                      cost=0.0, latency_ms=1.0, meta=meta)


def make_backend(mode: str) -> object:
    if mode == "lesson-sim":
        return LessonAwareSimBackend(rng=random.Random(0))
    if mode == "sim":
        return rt.SimBackend(random.Random(0))
    if mode == "real":
        key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("real 模式需要 DEEPSEEK_API_KEY / OPENAI_API_KEY")
        return RealLLMBackend(api_key=key, base_url=os.environ.get("AGENT_API_BASE"))
    raise SystemExit(f"未知 backend: {mode}")


# ---------------------------------------------------------------------------
# 单任务执行：编译(可记忆) → 打 tag → CircuitExecutor.run → 捕获译文评分
# ---------------------------------------------------------------------------
def run_task(task: dict, backend, memory_enabled: bool, mem_path: str,
             captured: dict) -> dict:
    goal = Goal(capabilities=["translate"], description=task["goal"])
    spec = compile_goal(goal, memory_enabled=memory_enabled)
    # 后处理：把测试元数据与质量门钉死
    for c in spec.get("components", {}).values():
        if isinstance(c, dict) and c.get("type") == "resistor":
            c["gold_term"] = task["gold_term"]
            c["src_text"] = task["src"]
        if isinstance(c, dict) and c.get("type") == "adc":
            c["threshold"] = ADC_THRESHOLD
    spec["goal_desc"] = task["goal"]

    ex = CircuitExecutor(
        Circuit(spec, backend),
        memory_enabled=memory_enabled,
        on_node_done=lambda cid, sig, info: captured.update(
            {cid: sig}) if sig is not None else None,
    )
    res = ex.run()
    # —— 独立评分：从捕获的译文文本重新算术语覆盖率（不盲信后端自报质量）——
    cover = 0.0
    for cid, sig in captured.items():
        comp = spec.get("components", {}).get(cid, {})
        if isinstance(comp, dict) and comp.get("type") == "resistor":
            val = getattr(sig, "value", None) or ""
            cover = 1.0 if task["gold_term"] in str(val) else 0.0
    return {
        "success": res.get("success"),
        "final_quality": res.get("final_quality"),
        "coverage": cover,
    }


def lesson_count(mem_path: str) -> int:
    try:
        return len(TopologyMemory(path=mem_path)._store.get("lessons", []))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="lesson-sim",
                    choices=["lesson-sim", "sim", "real"])
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--out", default="benchmark_out")
    ap.add_argument("--seed-mem", action="store_true",
                    help="WITH 条件开跑前先种入人工术语偏好（模拟'已被人教过一次'）")
    args = ap.parse_args()

    tmpd = tempfile.mkdtemp(prefix="bench_")
    mem_path = os.path.join(tmpd, "mem.json")
    # 隔离记忆：把 TopologyMemory 默认路径指到临时文件（WITH 用，跨轮持久）
    _orig_init = TopologyMemory.__init__

    def _patched(self, path=None):
        _orig_init(self, path if path is not None else mem_path)

    TopologyMemory.__init__ = _patched

    os.makedirs(args.out, exist_ok=True)
    rows = []
    with_lessons = []   # 每轮开始时 WITH 可用 lesson 数
    with_cov, without_cov = [], []

    # 可选：开跑前种入人工术语偏好（WITH 专属）
    if args.seed_mem:
        m0 = TopologyMemory(path=mem_path)
        for t in TRANSLATION_TASKS:
            m0.record_lesson(
                f"翻译约定：领域句『{t['src']}』中关键术语『{t['gold_term']}』"
                f"必须准确译出，不得泛化", tags=["human", "term"])

    for r in range(1, args.rounds + 1):
        with_lessons.append(lesson_count(mem_path))
        wc_r, woc_r = [], []
        for task in TRANSLATION_TASKS:
            cap_w, cap_wo = {}, {}
            rw = run_task(task, make_backend(args.backend), True, mem_path, cap_w)
            # WITHOUT 不依赖记忆文件（memory_enabled=False 不读写），用全新后端
            ro = run_task(task, make_backend(args.backend), False, mem_path, cap_wo)
            wc_r.append(rw["coverage"])
            woc_r.append(ro["coverage"])
            rows.append({"round": r, "task": task["id"],
                         "with_cov": rw["coverage"], "without_cov": ro["coverage"]})
        with_cov.append(sum(wc_r) / len(wc_r))
        without_cov.append(sum(woc_r) / len(woc_r))
        print(f"轮 {r}: WITH 覆盖={with_cov[-1]:.2f} "
              f"WITHOUT 覆盖={without_cov[-1]:.2f} "
              f"可用lessons={with_lessons[-1]}")

    # —— 相关性：可用 lesson 数 vs WITH-WITHOUT 抬升 ——
    lift = [w - o for w, o in zip(with_cov, without_cov)]
    corr = pearson(with_lessons, lift)

    # —— 裁决 ——
    crossed = any(w > o for w, o in zip(with_cov, without_cov))
    verdict = "通过" if (crossed and max(lift) > 0.1) else "未通过"
    note = ("lesson 注入产生可量化质量增益；真 LLM 下即为超越潜力"
            if args.backend == "lesson-sim" else
            "真实质量增益，可直接作为'超越'证据"
            if args.backend == "real" else
            "纯 SimBackend 无 lesson 消费 → 不应有抬升（符合预期）")

    # —— 落盘 ——
    csv_path = os.path.join(args.out, "benchmark_results.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["round", "task", "with_cov", "without_cov"])
        w.writeheader()
        for row in rows:
            w.writerow(row)

    md_path = os.path.join(args.out, "benchmark_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 超越基准报告（backend={args.backend}）\n\n")
        f.write(f"- 任务类：翻译（{len(TRANSLATION_TASKS)} 句，固定）\n")
        f.write(f"- 轮数：{args.rounds}\n")
        f.write(f"- 条件：WITH(记忆开) vs WITHOUT(记忆关，等价于每轮清零)\n")
        f.write(f"- lesson 数→抬升 相关(Pearson)：{corr:.3f}\n")
        f.write(f"- 最大抬升：{max(lift):.2f}\n")
        f.write(f"- 裁决：**{verdict}**\n")
        f.write(f"- 说明：{note}\n\n")
        f.write("| 轮 | WITH覆盖 | WITHOUT覆盖 | 可用lessons |\n")
        f.write("|---|---|---|---|\n")
        for i in range(args.rounds):
            f.write(f"| {i+1} | {with_cov[i]:.2f} | {without_cov[i]:.2f} "
                    f"| {with_lessons[i]} |\n")
        f.write(f"\n> 诚实边界：lesson-sim 是'称职模型消费提示'的模型而非真值；"
                f"真数字请用 `--backend real`（本机 DEEPSEEK_API_KEY）。\n")

    html_path = os.path.join(args.out, "benchmark_curve.html")
    render_html(html_path, with_cov, without_cov, with_lessons, args.backend,
                corr, verdict)

    print(f"\n裁决: {verdict} | Pearson(lesson→lift)={corr:.3f} | 最大抬升={max(lift):.2f}")
    print(f"产物: {csv_path}\n       {md_path}\n       {html_path}")


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy)


def render_html(path, with_cov, without_cov, lessons, backend, corr, verdict):
    R, W, H = 6, 680, 380
    n = len(with_cov)
    x0, x1, y0, y1 = 70, 630, 70, 300
    ymin, ymax = 0.0, 1.0

    def px(i):
        return x0 + (x1 - x0) * (i / (n - 1)) if n > 1 else (x0 + x1) / 2

    def py(v):
        return y1 - (v - ymin) / (ymax - ymin) * (y1 - y0)

    with_pts = " ".join(f"{px(i):.1f},{py(with_cov[i]):.1f}" for i in range(n))
    wout_pts = " ".join(f"{px(i):.1f},{py(without_cov[i]):.1f}" for i in range(n))
    lmax = max(lessons) if lessons else 1
    bars = ""
    for i in range(n):
        bh = (lessons[i] / lmax) * 40 if lmax else 0
        bars += (f'<rect x="{px(i)-6:.1f}" y="{y1+10-bh:.1f}" width="12" height="{bh:.1f}" '
                 f'fill="#BA7517" opacity="0.55"/>')

    legend = (f"backend={backend} · Pearson(lesson→lift)={corr:.3f} · 裁决={verdict}")
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>超越基准曲线</title></head><body style="font-family:sans-serif;margin:24px">
<h2>质量覆盖率随重复轮次（WITH 记忆 vs WITHOUT 记忆）</h2>
<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:760px">
<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#2C2C2A"/>
<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#2C2C2A"/>
<text x="{x0-8}" y="{py(1.0)+4}" font-size="11" fill="#5F5E5A">1.0</text>
<text x="{x0-8}" y="{py(0.0)+4}" font-size="11" fill="#5F5E5A">0.0</text>
{bars}
<polyline points="{with_pts}" fill="none" stroke="#3B6D11" stroke-width="2.5"/>
<polyline points="{wout_pts}" fill="none" stroke="#185FA5" stroke-width="2.5"/>
<text x="{px(0)}" y="{y1+34}" font-size="11" fill="#3B6D11">WITH(记忆)</text>
<text x="{px(n-1)-90}" y="{y1+34}" font-size="11" fill="#185FA5">WITHOUT(清零)</text>
<text x="{x0}" y="{H-8}" font-size="11" fill="#444441">横轴=轮次 橙条=可用lessons数</text>
</svg>
<p style="color:#444441;font-size:13px">{legend}</p>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
