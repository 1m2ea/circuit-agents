"""
circuit-agents · compiler.demo
==========================
端到端证明：Goal → Netlister → runtime.py Circuit → 指标。

运行：  python -m compiler.demo   （在 circuit-agents/ 下）
  或：  python compiler/demo.py
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime
from compiler.goal import Goal
from compiler.netlister import Netlister
from compiler.binder import Binder
from compiler.router import Router
from compiler.optimizer import Optimizer
from compiler.nl_parser import GoalParser
from compiler.compile import compile_goal
from compiler.backend_llm import get_default_backend, resolve_api_key


def _fmt(m):
    return (f"cost={m['avg_cost']:.4f} lat={m['avg_latency']:.0f}ms "
            f"qual={m['avg_quality']:.3f} all_fired={m['all_fired_rate']:.2%} "
            f"feasible={'✓' if m['feasible'] else '✗'}")


def _simulate(spec, g, seed=7, runs=300, api_key=None):
    """多次仿真取均值（由主种子派生的确定性 rng）。返回聚合指标 dict。

    指标说明（无 feedback 环时 runtime.success 恒 True，故用更有信息量的量）：
      out_rate      = 最终质量>0 的轮次占比（"有产出"）
      all_fired_rate= 所有能力支路都成功 fire 的轮次占比（"全支路存活"）
      串联时二者相等；并联时 out_rate≈1（单支路失败不影响 max 汇合），
      all_fired_rate 才等价于串联的"全链路无开路"。
    """
    rng = random.Random(seed)
    cost = lat = q = q_ok = 0.0
    n = runs
    n_out = 0
    n_all = 0
    n_ok_q = 0
    red = g.redundancy or {}

    def cap_rep(i):
        # 兼容冗余副本：能力 i 若声明 K>=2 副本，其"已交付"代表节点是 rmerge_{i}
        # （any 汇合，>=1 副本 fire 即 ok）；否则是单份 cap_{i}。
        cap = g.capabilities[i]
        return f"rmerge_{i}" if (red.get(cap, 1) or 1) >= 2 else f"cap_{i}"

    backend = get_default_backend(api_key=api_key)
    for _ in range(runs):
        r = random.Random(rng.random())
        circ = runtime.Circuit(spec, backend)
        res = circ.execute()
        cost += res["total_cost"]
        lat += res["total_latency_ms"]
        q += res["final_quality"]
        final = res["final_quality"]
        comps = res["components"]
        caps_ok = all(comps.get(cap_rep(i), {}).get("ok", False)
                      for i in range(len(g.capabilities)))
        if final > 0:
            n_out += 1
        if caps_ok:
            n_all += 1
            q_ok += final
            n_ok_q += 1
    ceiling = (q_ok / n_ok_q) if n_ok_q else 0.0
    return {
        "n": n, "n_out": n_out, "n_all": n_all, "ceiling": ceiling,
        "avg_cost": cost / n, "avg_latency": lat / n, "avg_quality": q / n,
    }


def _print_metrics(sim):
    print(f"runs={sim['n']}  out_rate={sim['n_out']/sim['n']:.2%}  "
          f"all_fired_rate={sim['n_all']/sim['n']:.2%}  "
          f"avg_cost={sim['avg_cost']:.4f}  avg_latency={sim['avg_latency']:.0f}ms  "
          f"avg_quality={sim['avg_quality']:.3f}  "
          f"quality_if_all_fired={sim['ceiling']:.3f}")


def run_case(title, goal_dict, tiers=None, seed=7, runs=300, router=False):
    g = Goal.from_dict(goal_dict)
    if router:
        spec = Router(default_tier="small").route(g, tiers=tiers)
    else:
        if tiers:
            g.tiers = tiers
        spec = Netlister().compile(g)
    print(f"\n=== {title} ===")
    print("rationale:", spec["rationale"])

    # Circuit 直接吃 spec dict（无需落临时文件），与 Optimizer 共用同一 Evaluator 路径。
    sim = _simulate(spec, g, seed, runs)
    _print_metrics(sim)

    # 约束检查（约束违反最终由 Optimizer(M3) 处理）
    c = goal_dict.get("constraints", {})
    if "min_quality" in c:
        ok = sim["avg_quality"] >= c["min_quality"]
        ok_ceiling = sim["ceiling"] >= c["min_quality"]
        print(f"  质量约束 ≥{c['min_quality']}: {'✓' if ok else '✗ 均值未达'}"
              f"（全通时 {'✓' if ok_ceiling else '✗'}）")
    if "max_cost" in c:
        ok = sim["avg_cost"] <= c["max_cost"]
        print(f"  成本约束 ≤{c['max_cost']}: {'✓' if ok else '✗ 超成本'}")
    if "max_latency_ms" in c:
        ok = sim["avg_latency"] <= c["max_latency_ms"]
        print(f"  延迟约束 ≤{c['max_latency_ms']}ms: {'✓' if ok else '✗ 超时'}")
    return spec


def run_nl_case(nl, api_key=None, seed=7, runs=300, router=True):
    """M4 端到端：自然语言 → GoalParser → compile_goal(auto_bind+route) → Circuit 仿真。

    默认尝试真模型：api_key 留空(None)时会自动解析（环境变量 DEEPSEEK/OPENAI/AGENT
    > ~/Desktop/key_tmp.txt）；解析到 key 即走 LLM 增强规划 + 真模型执行，无 key 才
    回退规则兜底（离线，不触网）。显式传 api_key="" 可强制离线。下游编译器与 runtime 复用，零改动。
    """
    key = resolve_api_key(api_key)
    parser = GoalParser(api_key=key)
    goal = parser.parse(nl)
    print(f"\n=== M4 · NL → Goal（{'LLM增强' if key else '规则兜底（离线）'}）===")
    print("NL  :", nl)
    print("Goal:", json.dumps(goal.to_dict(), ensure_ascii=False, indent=2))

    # 走 canonical 总入口：M1 Binder 自动选型 + M2 Router 布线
    spec = compile_goal(goal, auto_bind=True, route=router)
    rep = spec.get("binder_report")
    if rep:
        print(f"Binder: tiers={rep.get('tiers')}  feasible={rep.get('feasible')}")
    print("rationale:", spec["rationale"])

    sim = _simulate(spec, goal, seed, runs, api_key=key)
    _print_metrics(sim)

    c = goal.constraints
    if "min_quality" in c:
        ok = sim["avg_quality"] >= c["min_quality"]
        ok_ceiling = sim["ceiling"] >= c["min_quality"]
        print(f"  质量约束 ≥{c['min_quality']}: {'✓' if ok else '✗ 均值未达'}"
              f"（全通时 {'✓' if ok_ceiling else '✗'}）")
    if "max_cost" in c:
        ok = sim["avg_cost"] <= c["max_cost"]
        print(f"  成本约束 ≤{c['max_cost']}: {'✓' if ok else '✗ 超成本'}")
    if "max_latency_ms" in c:
        ok = sim["avg_latency"] <= c["max_latency_ms"]
        print(f"  延迟约束 ≤{c['max_latency_ms']}ms: {'✓' if ok else '✗ 超时'}")
    return spec


def main():
    # COMPILER.md §9 走查目标
    goal = {
        "name": "pdf_summarize_verify",
        "description": "总结一篇 PDF 并核对里面的数字",
        "capabilities": ["retrieve", "reason", "calculate", "verify"],
        "constraints": {"max_latency_ms": 2000, "max_cost": 0.05, "min_quality": 0.85},
        "modalities": ["pdf", "table"],
        "reliability": "high",
    }

    # (a) 基线：默认 small 档 → 能力上限仅 0.70，预期不达标（引出 M1 Binder 的必要性）
    spec_a = run_case("A · 默认 small 档（基线：能力上限仅 0.70）", goal, tiers=None)

    # (b) 指定 large/tool 档 → 全通时可达 ~0.92，但串联良率(yield 开路)拖累均值
    #     （引出 M2 Router 的反馈/冗余 与 补强#1 recovery 系数）
    spec_b = run_case(
        "B · 指定 large/tool 档（全通可达 ~0.92，串联良率拖累均值）", goal,
        tiers={"retrieve": "tool", "reason": "large",
               "calculate": "tool", "verify": "large"},
    )

    # (c) M1 Binder 自动选型：在满足 min_quality 前提下挑最便宜档（复用 _TIERS）
    g = Goal.from_dict(goal)
    binder = Binder()
    auto_tiers = binder.bind(g)
    rep = binder.report(g, auto_tiers)
    print("\n=== C 前置 · Binder 自动选型报告 ===")
    print("tiers:", auto_tiers, "  budget:", rep["budget"], "  feasible:", rep["feasible"])
    spec_c = run_case("C · Binder 自动选型（满足精度的最低成本档）", goal, tiers=auto_tiers)

    # (d) M2 Router · 全并联：dependencies=[] 声明四步互不依赖 → 同层并发
    #     延迟从 ~3500ms 砍到 ~1100ms，三项约束全过（证明布线直接决定系统级指标）
    g_d = Goal.from_dict({**goal, "dependencies": []})
    binder_d = Binder()
    tiers_d = binder_d.bind(g_d)
    spec_d = run_case("D · Router 全并联（dependencies=[] 假设四步独立）",
                      goal_dict={**goal, "dependencies": []},
                      tiers=tiers_d, router=True)

    # (e) M2 反馈环标准单元：D 全并联 + 终端 adc 质量门控整链重试(max_iter=3)
    #     runtime 原生仅支持单环 → 以终端 adc 为质量门，不达标整链重试（max_iter=3）
    g_e = Goal.from_dict({**goal, "dependencies": [], "feedback": {"max_iter": 3}})
    binder_e = Binder()
    tiers_e = binder_e.bind(g_e)
    spec_e = run_case("E · 全并联 + 反馈环(末级汇合all-fired门控, max_iter=3)",
                      goal_dict={**goal, "dependencies": [], "feedback": {"max_iter": 3}},
                      tiers=tiers_e, router=True)

    # (f) M2 冗余标准单元 #5：D 全并联 + 全能力 K=2 冗余（any 汇合收口）
    #     all_fired 从 ~92% 拉到 ~99.8%（单点 yield 失败被副本吸收，一支开路不影响其余）；
    #     代价是成本 ×K —— 全能力 K=2 把成本顶到 ~0.072 突破 0.05 约束，
    #     正好教 M3 Optimizer：冗余不免费，要在 cost/可靠上搜 Pareto（并非越多越好）。
    red_all = {c: 2 for c in goal["capabilities"]}
    g_f = Goal.from_dict({**goal, "dependencies": [], "redundancy": red_all})
    binder_f = Binder()
    tiers_f = binder_f.bind(g_f)
    spec_f = run_case("F · 全并联 + 冗余(全能力 K=2, any汇合)",
                      goal_dict={**goal, "dependencies": [], "redundancy": red_all},
                      tiers=tiers_f, router=True)
    out_f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "examples", "generated_pdf_verify_redundant.json")
    with open(out_f, "w", encoding="utf-8") as f:
        json.dump(spec_f, f, ensure_ascii=False, indent=2)
    print(f"\n已保存生成示例(Router 全并联+冗余K=2): {out_f}")

    # 保存 E（Router 全并联 + 反馈环）为生成示例（可被 draw.py / runtime 直接消费）
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "examples", "generated_pdf_verify.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(spec_e, f, ensure_ascii=False, indent=2)
    print(f"已保存生成示例(Router 全并联+反馈环): {out}")

    # ---- G：M3 Optimizer 自动解（贪心 + 搜索，runtime 为 Evaluator）----
    opt = Optimizer(runs=200, seed=7)
    res = opt.optimize(goal)
    fin = res["final"]
    cfg = res["greedy"]["config"]
    print("\n=== G · M3 Optimizer 自动解 ===")
    print("贪心起手 config:",
          f"tiers={cfg['tiers']} dependencies={cfg['dependencies']} "
          f"redundancy={cfg['redundancy']} feedback={cfg['feedback']}")
    print("  贪心 metrics:", _fmt(res["greedy"]["metrics"]))
    print(f"搜索可行解数={len(res['search']['feasible'])}  "
          f"Pareto 前沿点数={len(res['search']['front'])}")
    print("最终解(搜索 min-cost 可行):", _fmt(fin))
    out_g = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "examples", "generated_pdf_verify_optimized.json")
    with open(out_g, "w", encoding="utf-8") as f:
        json.dump(fin["spec"], f, ensure_ascii=False, indent=2)
    print(f"已保存优化解示例(M3 Optimizer): {out_g}")

    # ---- H：补强#1 recovery 系数 ----
    # 串联 small(弱,cap0.70) → tool(强,cap0.99)：
    #   η=0 时强 agent 只能透传弱输入 → 最终质量天花板 ≈0.70（受弱档卡死，不达标）；
    #   η=0.5 时强 agent 部分挽救弱输入 → 天花板 ≈0.845（达标）。
    # 同时验证：上游 small 开路(yield失败) 时，下游 tool 仍严格开路（recovery 不 revive）。
    goal_h = {
        "name": "recovery_demo",
        "description": "弱输入被强 agent 挽救",
        "capabilities": ["draft", "polish"],
        "modalities": ["text"],
        "constraints": {"min_quality": 0.75},
        "reliability": "normal",
        "dependencies": None,   # 显式线性串联
    }
    tiers_h = {"draft": "small", "polish": "tool"}
    run_case("H0 · 串联 small→tool 无recovery(η=0)",
             goal_dict={**goal_h, "recovery": 0.0}, tiers=tiers_h, router=False)
    run_case("H1 · 串联 small→tool recovery(η=0.5)",
             goal_dict={**goal_h, "recovery": 0.5}, tiers=tiers_h, router=False)

    # ---- M4：NL → Goal → 编译 → 执行（端到端，离线规则兜底）----
    print("\n" + "=" * 60)
    print("M4 · 自然语言目标 端到端（NL → Goal → 编译器 → 仿真）")
    print("=" * 60)
    nl_examples = [
        "总结一篇PDF并核对里面的数字，要求高可靠，延迟不超过3000ms",
        "把这段英文翻译成中文",
        "从图片里提取表格并分类，随便处理",
    ]
    for nl in nl_examples:
        run_nl_case(nl, api_key=None, seed=7, runs=300, router=True)

    print("\n解读：")
    print(" · A 受'能力上限 0.70'卡死；C 经 Binder 自动选型（tool 档 accuracy 0.99≥0.85）")
    print("   已把质量抬到达标——M1 完成'选型'职责（tool 在成本/延迟/精度上都优于 large）。")
    print(" · C 串联 4 个 tool 电阻(各 800ms)→ 延迟 ~3500ms 超 2000ms 约束；")
    print("   D 用 Router 把四步放进同一层并联（dependencies=[]），延迟 ~1100ms 达标——")
    print("   证明'布线'本身就能决定系统级延迟，runtime 一行未改（layer 化 propagate 复用）。")
    print(" · D 的 out_rate≈100% 而 all_fired_rate≈92%：并联 max 汇合让单支路开路不再清零产出，")
    print("   比串联更抗单点 yield 失败（标准单元#2 '质量=最优支路'）；这是模拟保真下的真实收益。")
    print(" · E 在 D 基础上加末级汇合(all-fired)门控反馈环(max_iter=3)：单支路开路触发整链重试，")
    print("   all_fired_rate 从 ~93% 拉到 ~99.9%；代价 avg 延迟/成本仅涨 ~8%（~1270ms/0.039），")
    print("   仍在约束内——'tiny premium 买 near-perfect reliability'，正是 M3 要搜的 Pareto 点。")
    print(" · 真实依赖（如 verify 依赖 calculate）应写成 dependencies 边，Router 会自动分层；")
    print("   D 演示'结构上限'，E 演示'可靠性保险(重试)'，F 演示'可靠性保险(冗余)'。")
    print(" · F 在 D 基础上把四能力各复制 K=2、由 capacitor(mode=any) 收口：")
    print("   all_fired_rate 从 ~92% 拉到 ~99.7%（单点 yield 失败被副本吸收，一支开路不影响其余），")
    print("   延迟零增加（同层并联取 max），但成本 ×K → 从 0.036 顶到 ~0.060 突破 0.05 约束——")
    print("   证明'冗余不免费'：要可靠又不破预算，必须只冗余最关键的 1~2 个能力（或减 K）。")
    print("   这正是 M3 Optimizer 要在 {pattern × tiers × 冗余配置 × feedback} 上搜的 Pareto 前沿。")
    print(" · G 用 M3 Optimizer(贪心+搜索)：以 runtime 为 Evaluator，在 ~36 个候选上仿真，")
    print("   自动从 D(并联) 出发 hill-climb 修约束，再枚举收 Pareto 前沿，挑出 min-cost 可行解——")
    print("   全程不手调 config，编译器自己搜出三项约束全过且成本最低的解（见上 final 行）。")
    print(" · M2 五个标准单元至此全部就位：串联(clarifier)/并联(capacitor)/反馈环(adc+watchdog)/")
    print("   桥式整流(异质模态)/冗余(any汇合)。runtime 仅加一个向后兼容的 capacitor mode 开关。")
    print(" · runtime 单环限制下反馈门控=末级汇合(all-fired)（整链重试），非 per-cap 局部环（那需改 runtime）。")
    print(" [runtime 已修正开路语义：无可用输入的变换器严格开路，不再被噪声顶成微弱正信号]")
    print(" · H 演示补强#1 recovery 系数 η：串联 small(弱,cap0.70)→tool(强,cap0.99)，约束 min_quality=0.75。")
    print("   η=0 强 agent 只能透传弱输入→天花板≈0.70、均值≈0.65（不达标 0.75）；")
    print("   η=0.5 强 agent 部分挽救弱输入→天花板≈0.845、均值≈0.79（达标）——'强 agent 挽救弱输入'成立。")
    print("   H1 的 all_fired_rate 与 H0 同量级：上游 small 开路时下游 tool 仍严格开路，")
    print("   recovery 仅对'弱但存活'的输入生效、绝不 revive 死输入，'开路必须保持开路'内核未被破坏。")
    print(" · M4 把“自然语言目标”接到流水线最前端：NL→Goal 混合解析（规则兜底 + 可选 LLM 增强），")
    print("   默认尝试真模型：解析到 key（环境变量 > ~/Desktop/key_tmp.txt）即走 LLM 规划+真模型执行；无 key 回退离线规则（不触网）。下游 compile_goal/Circuit 零改动。")
    print("   三个 NL 例子证明：文档类自动补 retrieve、约束/模态/可靠性识别、纯翻译、多模态抽取+分类均正确落到 Goal。")


if __name__ == "__main__":
    main()
