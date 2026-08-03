"""
circuit-agents · compiler._demo_llm_agents_run
============================================
把 LLMAgentBackend 真正接进 Circuit(backend=...)，跑一次真实模型（或离线装配请求）。

这是"每个电阻 = 独立 LLM 实例"的**真·接线实证**：编译一个小目标 →
`Circuit(spec, backend=LLMAgentBackend)` → `propagate()`，让每个电阻节点带上
各自能力的角色系统提示词去调真实模型。

安全约定（沿用 _verify_real.py）：
 · key 只从本地文件读入（默认 ~/Desktop/key_tmp.txt），明文**绝不 print**、绝不进对话/命令。
 · base_url 默认 https://api.deepseek.com/v1（与历史真跑一致）；--base-url 可覆盖。
 · 运行：
     python compiler/_demo_llm_agents_run.py [--goal "..."] [--base-url URL] [--dry-run]
   默认走真·live（产生真实 API 费用）；--dry-run 只组装请求不发送（离线、零网络、零费用）。
 · 沙箱若屏蔽出网，调用会如实抛网络错误，不会伪造结果。
 · 注：retrieve 是"工具型节点"，真实环境应由 runtime 给它挂检索工具；本 demo 仅证明
   "按能力选 system 提示词 + 每个电阻调真模型"的接线，retrieve 节点在无工具时由模型自行应答。
"""
from __future__ import annotations

import argparse
import os
import sys

# 让脚本无论从哪个 cwd 运行都能 import runtime / compiler 包
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from runtime import Circuit                                # noqa: E402
from compiler.goal import Goal                             # noqa: E402
from compiler.compile import compile_goal                  # noqa: E402
from compiler.llm_agents import LLMAgentBackend            # noqa: E402


KEY_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "key_tmp.txt")
DEEPSEEK_BASE = "https://api.deepseek.com/v1"


def _load_key(path: str) -> str:
    """从本地文件读 key（明文不print、不进对话）。文件不存在/空 → 返回空串。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", default="",
                    help="任务描述；留空则使用各 --demo 模式的默认任务")
    ap.add_argument("--demo", default="reason",
                    choices=["reason", "retrieve", "verify", "layer3"],
                    help="演示模式：reason（验证 run_code+calculator） / "
                         "retrieve（验证 web_search/read_page/query_db） / "
                         "verify（验证 cross_check+diff_text） / "
                         "layer3（验证第三层技能包：extract→classify→organize→summarize）")
    ap.add_argument("--base-url", default=DEEPSEEK_BASE)
    ap.add_argument("--key-file", default=KEY_PATH,
                    help="API key 文件路径（明文不进对话）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只组装请求不发送（离线，零网络/零费用）")
    args = ap.parse_args()

    key = _load_key(args.key_file)
    if not args.dry_run and not key:
        print(f"[提示] 未读到 key（{args.key_file} 为空/不存在），自动改跑 --dry-run 实证。")
        args.dry_run = True

    # 演示目标：按 --demo 选择能力链路与默认任务。
    if args.demo == "retrieve":
        # 检索优先的目标：让 retrieve 节点真正调用 web_search / read_page / query_db。
        # 目标特意指向**本地项目文档里确实存在的确切事实**，确保 query_db（本地检索，
        # 零外部依赖）稳定命中并产出"带来源"的答案——即便沙箱出网受限、DuckDuckGo 不可达，
        # 也能干净地证明"每个 agent 可调用技能"的接线（web_search 失败会被模型优雅降级）。
        goal_default = ("请使用检索技能（优先用 query_db 查本地项目文档）整理以下事实，"
                        "并每条标注来源文件路径：\n"
                        "1) circuit-agents 项目里 runtime.py 的 Signal 类包含哪些字段？\n"
                        "2) _TIERS 定义了哪几档（small/large/tool），各自 accuracy 是多少？")
        capabilities = ["retrieve", "summarize"]
        goal_name = "live-retrieve"
    elif args.demo == "verify":
        # 核对优先的目标：让 verify 节点真正调用 cross_check（本地取证）+ diff_text。
        # 目标给一条关于本项目的可核验结论，模型用 cross_check 查本地文档取证并给 verdict。
        goal_default = ("请使用核对技能独立取证，核验这条结论是否与本项目一致："
                        "「circuit-agents 在 runtime.py 中用 Signal 类在节点间传递消息，"
                        "且 _TIERS 定义了 small / large / tool 三档型号。」"
                        "给出「一致 / 不符 / 无法核实」的结论与证据。")
        capabilities = ["verify"]
        goal_name = "live-verify"
    elif args.demo == "layer3":
        # 第三层技能包实证：让 extract/classify/organize/summarize 节点各自调用其领域工具。
        # 任务给一段"用户反馈"原文，引导每个节点用专属技能（纯 stdlib 的字段抽取/分类打分/
        # 模板塑形/文体约束），即便无 key 也能在 dry-run 证明 schema 已接线。
        goal_default = (
            "请按以下流程处理这条用户反馈，并让每个环节使用其专属技能：\n"
            "【原始反馈】\n"
            "「这家餐厅环境很好，服务态度正面，菜品性价比高，但上菜速度有点慢。"
            "联系电话 138-0013-8000，邮箱 good@restaurant.com，发布日期 2026-03-15。」\n\n"
            "1) extract 环节：用 extract_fields 技能从反馈中抽取 电话/邮箱/日期 等字段；\n"
            "2) classify 环节：用 classify_taxonomy 技能按情感分类（正向/负向/中性）；\n"
            "3) organize 环节：用 apply_template 技能（bullet 模板）把要点整理成清单；\n"
            "4) summarize 环节：用 apply_style_guide 技能（concise）产出简洁总结。"
        )
        capabilities = ["extract", "classify", "organize", "summarize"]
        goal_name = "live-layer3"
    else:
        # reason → summarize：任务直接喂给 reason，能触发 run_code 数值技能产出真实内容。
        goal_default = ("一笔 10000 元本金，年利率 3.5%，存 5 年：分别算出单利和复利"
                        "的最终金额，并解释两者的差异。")
        capabilities = ["reason", "summarize"]
        goal_name = "live-reason-summarize"

    # 注：min_quality=0.6 是因为 LLM 后端的质量数仍是「tier 能力上限先验」(small=0.70)，
    # 不是真实测得的输出质量；设为 0.6 让 ADC 门控反映「各节点都已成功产出」而非卡在先验上。
    # 生产环境应由真实质量估计（长度/格式校验 或 下游 evaluate 节点）替换该先验。
    goal = Goal(capabilities=capabilities,
                description=args.goal or goal_default, reliability="normal",
                name=goal_name,
                constraints={"min_quality": 0.6})
    spec = compile_goal(goal, auto_bind=True, route=True)
    print(f"[编译] name={spec['name']} 组件数={len(spec['components'])} "
          f"边={len(spec['wires'])}")

    be = LLMAgentBackend(api_key=key or None, base_url=args.base_url,
                         dry_run=args.dry_run)
    circuit = Circuit(spec, backend=be)
    out, lat, cost = circuit.propagate()

    print(f"\n[执行] total_latency={lat:.0f}ms total_cost={cost:.4f} "
          f"dry_run={args.dry_run}\n")
    for cid, comp in spec["components"].items():
        sig = out.get(cid)
        if comp.get("type") == "resistor":
            cap = comp.get("label")
            print(f"  • {cid} [{cap}] ok={sig.ok} q={sig.quality:.2f} "
                  f"cost={sig.cost:.4f} lat={sig.latency_ms:.0f}ms "
                  f"model={sig.meta.get('model')}")
            val = sig.value
            if isinstance(val, str) and val and not args.dry_run:
                print(f"      产出: {val[:240]}")
            elif args.dry_run:
                print(f"      产出: [dry-run] {val}")
                tools = sig.meta.get("tools")
                if tools:
                    print(f"      挂接技能(tools): {tools}")
            # 技能调用证据：这个 agent 真正调了哪些技能（每个 agent 可调技能的体现）
            tcalls = sig.meta.get("tool_calls")
            if tcalls:
                print(f"      技能调用: {tcalls}")
                for tl in sig.meta.get("tool_log", []):
                    print(f"        └─ {tl['name']} 结果: {tl.get('result', '')[:300]}")
        else:
            print(f"  • {cid} [{comp.get('type')}] ok={sig.ok} q={sig.quality:.2f}")

    # dry_run：额外把第一个电阻节点的"已装配真实请求"打出来，证明按能力选词 +
    # 技能包声明 + 上游上下文接入（reason 模式看 run_code、retrieve 模式看检索技能）
    if args.dry_run:
        for cid, comp in spec["components"].items():
            if comp.get("type") == "resistor":
                cap = comp.get("label")
                msgs = out[cid].meta.get("messages", [])
                print(f"\n[装配实证] {cap} 节点的真实请求 messages：")
                for m in msgs:
                    body = m["content"]
                    print(f"  ── {m['role']} ──\n{body[:600]}")
                break


if __name__ == "__main__":
    main()
