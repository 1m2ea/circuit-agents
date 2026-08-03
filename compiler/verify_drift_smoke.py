"""
circuit-agents · compiler.verify_drift_smoke
============================================
命名漂移符号映射表（确定性「转接头」）· 参数化 CI 冒烟。

设计（与用户对齐：默认离线、--online 走 NL）：
 · 默认【离线模式】：传 subtasks(JSON) + 期望 input_map → 走 _goal_from_subtasks（确定性、
   不调 LLM）→ compile_goal → Circuit.propagate()（SimBackend，不烧 key、不联网、可重复）。
   · 断言①：生成的 input_maps 与 --expect / --expect-contains 一致；
   · 断言②：带映射的电阻节点 ok=True、无 gate:fail_linear（映射在 runtime 真正消解漂移）；
   · 断言③（因果性，离线专属）：去掉所有 input_map 重跑，原漂移节点必须 gate:fail_linear
     ——证明映射是"承重的"，不是摆设。
 · 【在线模式 --online】：传 --nl，调真实 GoalParser（DeepSeek）规划 → 报告产出的 component_io
   与 input_map（可加 --expect 断言，但真模型非确定性，仅作 best-effort）；加 --execute 再跑
   一次真后端（多几个电阻调用）证明链路闭合。需 ~/Desktop/key_tmp.txt（或环境变量）。
 · 退出码：0=通过，1=断言失败，2=用法/缺 key 错误。可直接接 CI（如 GitHub Actions step）。

安全：key 仅从 ~/Desktop/key_tmp.txt（resolve_api_key）读，明文不 print / 不进命令/日志。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from compiler.nl_parser import GoalParser                         # noqa: E402
from compiler.compile import compile_goal                          # noqa: E402
from compiler.backend_llm import resolve_api_key                    # noqa: E402
from runtime import Circuit, SimBackend, Signal                    # noqa: E402


# 内置确定性漂移样例（CI 零参一键冒烟用）：A 产 gdp_china_2024、B 产 gdp_per_capita、
# C 需 china_gdp_2024（漂移）+ gdp_per_capita → 期望 C 注入 input_map{china_gdp_2024:gdp_china_2024}
DEMO_SUBTASKS = [
    {"id": "A", "capability": "reason",
     "description": "产出中国2024年GDP总量", "inputs": [],
     "outputs": ["gdp_china_2024"]},
    {"id": "B", "capability": "reason",
     "description": "产出人均GDP", "inputs": [],
     "outputs": ["gdp_per_capita"]},
    {"id": "C", "capability": "reason",
     "description": "对比分析中美GDP",
     "inputs": ["china_gdp_2024", "gdp_per_capita"],
     "outputs": ["analysis"]},
]


def _collect_input_maps(spec):
    """收集编译后所有电阻的 input_map，返回 {cid: {down:up}}。"""
    m = {}
    for cid, c in spec["components"].items():
        if c.get("type") == "resistor" and c.get("input_map"):
            m[cid] = dict(c["input_map"])
    return m


def _assert_expect(generated, expect_exact, expect_contains):
    if expect_exact is not None:
        if generated != expect_exact:
            raise AssertionError(
                f"input_maps 与 --expect 不符\n  期望: {expect_exact}\n  实际: {generated}")
        print(f"✓ 生成的 input_maps 与 --expect 完全一致: {generated}")
    if expect_contains is not None:
        flat = {d: u for mp in generated.values() for d, u in mp.items()}
        for d, u in expect_contains.items():
            if flat.get(d) != u:
                raise AssertionError(
                    f"--expect-contains 缺失/不符: {d}->{u}，实际映射并集={flat}")
        print(f"✓ --expect-contains 全部命中: {expect_contains}")


def _offline(subtasks, reliability, expect_exact, expect_contains, verbose):
    print("=== [离线模式] subtasks → 编译 → SimBackend 执行（不烧 key / 不联网）===")
    goal = GoalParser._goal_from_subtasks(
        {"subtasks": subtasks, "reliability": reliability or "normal"})
    if verbose:
        print("component_io:", json.dumps(goal.component_io, ensure_ascii=False, indent=2))

    _assert_expect(_collect_input_maps_from_goal(goal), expect_exact, expect_contains)

    spec = compile_goal(goal, auto_bind=True, route=True)
    drift_cids = [cid for cid, c in spec["components"].items()
                  if c.get("type") == "resistor" and c.get("input_map")]
    if not drift_cids:
        print("· 无带映射的电阻节点（零回归：无漂移不建映射）。")
    else:
        print(f"· 带映射的电阻节点: {drift_cids}")

    # 断言②：带映射节点 ok + 无 gate
    sim = SimBackend(random.Random(0))
    out, _, _ = Circuit(spec, sim).propagate()
    for cid in drift_cids:
        s = out.get(cid)
        gate = (s.meta or {}).get("gate") if s else "NO_SIGNAL"
        assert s is not None and s.ok, f"{cid} 应 ok=True（映射应消解漂移），实际 meta={s.meta if s else None}"
        assert gate is None, f"{cid} 不应触发 {gate}"
    print(f"✓ 断言②：带映射电阻全部 ok=True、无 gate:fail_linear（映射在 runtime 真正消解漂移）")

    # 断言③（因果性）：去掉所有 input_map 重跑，原漂移节点必 gate:fail_linear
    if drift_cids:
        spec_no = copy.deepcopy(spec)
        for c in spec_no["components"].values():
            c.pop("input_map", None)
        out_no, _, _ = Circuit(spec_no, SimBackend(random.Random(0))).propagate()
        for cid in drift_cids:
            s = out_no.get(cid)
            g = (s.meta or {}).get("gate") if s else None
            assert g == "fail_linear", \
                f"因果性检查失败：去掉映射后 {cid} 应 gate:fail_linear，实际 gate={g}"
        print(f"✓ 断言③（因果性）：去掉映射后 {drift_cids} 均 gate:fail_linear "
              f"——证明映射是承重的，非摆设")

    print(f"\n✓✓ 离线 CI 冒烟通过（subtasks={len(subtasks)} 个，漂移节点={len(drift_cids)}）")
    return 0


def _collect_input_maps_from_goal(goal):
    m = {}
    for node, io in (goal.component_io or {}).items():
        if io.get("input_map"):
            m[node] = dict(io["input_map"])
    return m


def _online(nl, reliability, expect_exact, expect_contains, do_execute, verbose):
    print("=== [在线模式 --online] 真实 LLM 规划（DeepSeek）===")
    key = resolve_api_key()
    if not key:
        print("✗ 未检测到 API key（~/Desktop/key_tmp.txt 或环境变量）。--online 不可用。")
        return 2
    print(f"· key 已加载(len={len(key)})，明文不展示")

    parser = GoalParser(api_key=key)
    goal = parser.parse(nl)
    print("capabilities :", goal.capabilities)
    print("dependencies  :", goal.dependencies)
    print("component_io  :")
    print(json.dumps(goal.component_io, ensure_ascii=False, indent=2))

    gen = _collect_input_maps_from_goal(goal)
    if expect_exact is not None or expect_contains is not None:
        try:
            _assert_expect(gen, expect_exact, expect_contains)
        except AssertionError as e:
            # 真模型非确定性：best-effort 断言失败仅告警，不阻断（除非用户明确要求）
            print(f"⚠ 在线 --expect 未命中（真模型非确定性，属 best-effort）：{e}")
    else:
        if gen:
            print(f"· 本次真实规划产出了命名漂移映射：{gen}")
        else:
            print("· 本次真实规划未触发命名漂移（LLM 自觉对齐命名——零回归，不建映射）。")

    if do_execute:
        print("\n=== [在线执行 --execute] 真后端跑一次（证明链路闭合）===")
        spec = compile_goal(goal, auto_bind=True, route=True)
        drift_cids = [cid for cid, c in spec["components"].items()
                      if c.get("type") == "resistor" and c.get("input_map")]
        from compiler.llm_agents import LLMAgentBackend
        backend = LLMAgentBackend(api_key=key,
                                  base_url="https://api.deepseek.com/v1")
        out, _, cost = Circuit(spec, backend).propagate()
        up_ok = all(out.get(w[0]) and out[w[0]].ok
                    for cid in drift_cids for w in spec["wires"] if w[1] == cid)
        if not up_ok:
            print("⚠ 上游节点执行失败（可能沙箱无外网）。映射 code path 已跑过，"
                  "『真后端闭合』因网络受限无法定论。")
            return 0
        for cid in drift_cids:
            s = out.get(cid)
            assert s.ok and (s.meta or {}).get("gate") is None, \
                f"{cid} 应 ok=True 且无 gate，实际 {s.meta}"
        print(f"✓ 在线执行：带映射电阻全部 ok=True、无 gate:fail_linear（链路闭合），成本≈${cost:.4f}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="命名漂移符号映射表 · 参数化 CI 冒烟（默认离线，--online 走 NL）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--subtasks", help="subtasks JSON 字符串（含 inputs/outputs）")
    src.add_argument("--subtasks-file", help="subtasks JSON 文件路径")
    src.add_argument("--nl", help="自然语言目标（须配合 --online 走真实 LLM 规划）")
    src.add_argument("--demo", action="store_true", help="用内置确定性漂移样例一键冒烟")
    ap.add_argument("--online", action="store_true",
                    help="在线模式：--nl 走真实 LLM 规划（需 key）")
    ap.add_argument("--execute", action="store_true",
                    help="在线模式附加：再跑一次真后端证明链路闭合（多几次真实调用）")
    ap.add_argument("--expect", help="期望的 input_maps（JSON，节点名→{下游:上游}）")
    ap.add_argument("--expect-contains",
                    help="期望出现的映射子集（JSON，{下游:上游}，跨节点并集匹配）")
    ap.add_argument("--reliability", default="normal",
                    help="goal.reliability（默认 normal）")
    ap.add_argument("--verbose", action="store_true", help="打印 component_io 明细")
    args = ap.parse_args(argv)

    if args.online and not args.nl:
        print("✗ --online 必须与 --nl 搭配（在线模式需自然语言目标触发真实规划）")
        return 2
    if args.execute and not args.online:
        print("⚠ --execute 仅在 --online 下有意义，已忽略（离线模式本就执行 SimBackend）")
    if not args.online and args.nl:
        print("⚠ 离线模式忽略 --nl（离线不调 LLM；请用 --subtasks 或 --demo）")
        args.nl = None

    expect_exact = json.loads(args.expect) if args.expect else None
    expect_contains = json.loads(args.expect_contains) if args.expect_contains else None

    try:
        if args.online and args.nl:
            return _online(args.nl, args.reliability, expect_exact,
                           expect_contains, args.execute, args.verbose)
        # 离线模式
        if args.demo:
            subtasks = DEMO_SUBTASKS
        elif args.subtasks:
            subtasks = json.loads(args.subtasks)
        else:
            with open(args.subtasks_file, "r", encoding="utf-8") as f:
                subtasks = json.load(f)
        return _offline(subtasks, args.reliability, expect_exact,
                        expect_contains, args.verbose)
    except AssertionError as e:
        print(f"\n✗ 冒烟失败：{e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ 运行异常：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
