#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exec_loop.py — circuit-planner 的「自驱动执行循环」驱动器。

吃 plan.py --execute 产出的 runbook JSON，维护执行状态，每次调用吐出
「下一步该做什么」的指令（action directive）。agent（WorkBuddy 运行时）照做后，
把结果用 --record 写回，再调一次推进，直到 done / failed。

关键边界：本脚本只做**调度 + 状态管理**，不调用任何真实工具
（WebFetch/Read/Write/LLM）。真实工具调用在 agent 侧——这是既定架构
（circuit-agents=规划内核，WorkBuddy=运行时）。脚本把"每步该调哪个工具、喂什么"
机械地算出来，消除 agent 重新理解 runbook 的模糊，从而"自驱动"。

状态文件：与 runbook 同目录、同名 `<name>_state.json`（--reset 可清空重来）。

用法:
  python exec_loop.py runbooks/<name>_runbook.json                 # 看当前下一步
  python exec_loop.py runbooks/<name>_runbook.json --reset        # 清空状态
  python exec_loop.py runbooks/<name>_runbook.json --record="1:本步产出摘要/落盘路径"
  python exec_loop.py runbooks/<name>_runbook.json --record="gate:pass"   # 质量门通过
  python exec_loop.py runbooks/<name>_runbook.json --record="gate:fail"   # 质量门不达标
  python exec_loop.py runbooks/<name>_runbook.json --record="ms:pass"     # 锁相环里程碑校验通过
  python exec_loop.py runbooks/<name>_runbook.json --record="ms:fail"     # 里程碑漂移→纠偏重跑上游
"""
from __future__ import annotations

import os
import sys
import json
import re


def _state_path(runbook_path):
    d = os.path.dirname(os.path.abspath(runbook_path))
    base = os.path.basename(runbook_path)
    name = re.sub(r"_runbook\.json$", "", base)
    return os.path.join(d, name + "_state.json")


def load_runbook(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def init_state(rb):
    return {
        "name": rb.get("name", ""),
        "artifacts": {},          # step(int as str) -> 本步产出摘要/落盘路径
        "gate": None,             # None / "pass" / "fail"
        "iter": 1,
        "status": "running",      # running / done / failed
        "feedback_max_iter": (rb.get("feedback") or {}).get("max_iter"),
        "milestones": {},         # 里程碑 id -> "pass" / "fail"（锁相环，第二层④）
        "corrections": {},        # 里程碑 id -> 已纠偏次数（防无限循环）
    }


def save_state(path, st):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _art(st, n):
    return st["artifacts"].get(str(n)) or st["artifacts"].get(int(n)) or ""


def _resolve_input(st, step, rb):
    """从 step 的 input_context 解析上游步骤产物，串成输入上下文。"""
    info = next((s for s in rb["steps"] if s["step"] == step), None)
    if not info:
        return ""
    parts = []
    for ctx in info.get("input_context", []):
        m = re.search(r"步骤(\d+)\s*\[([^\]]+)\]", ctx)
        if m:
            art = _art(st, int(m.group(1)))
            if art:
                parts.append(f"[{m.group(2)} 步骤{m.group(1)}产出]\n{art}")
        else:
            parts.append(ctx)  # 原始任务输入(源) 等
    return "\n\n".join(parts)


def next_directive(rb, st):
    """根据状态计算下一步指令。"""
    if st["status"] in ("done", "failed"):
        return {"action": st["status"], "status": st["status"], "iter": st["iter"],
                "hint": "闭环结束（done=成功交付；failed=超过反馈环重试上限仍不达标）"}

    steps = rb.get("steps", [])
    ms_list = rb.get("milestones") or []
    MAX_CORR = 2  # 单里程碑最多纠偏次数，超则放行继续（防卡死）

    # ---- 锁相环（第二层④）：执行中途的轻量校验 + 漂移纠偏 ----
    # ① 待校验里程碑（after_steps 全完成且未检）→ 阻塞并吐 milestone_check
    for m in ms_list:
        if st.get("milestones", {}).get(m["id"]):
            continue
        if all(str(s) in st["artifacts"] for s in m["after_steps"]):
            return {
                "action": "milestone_check",
                "milestone": m["id"],
                "after_steps": m["after_steps"],
                "check": m.get("check", "轻量校验（关键词/存在性）"),
                "on_fail": m.get("on_fail", "retry_upstream"),
                "hint": (f"里程碑 {m['id']} 到达：本层步骤 {m['after_steps']} 均已完成，"
                         f"请做轻量校验（关键词/存在性，无需模型）。达标 --record=\"ms:pass\"；"
                         f"漂移 --record=\"ms:fail\"（将触发纠偏重跑上游）。"),
            }
    # ② 失败且未超上限 → 纠偏：清空上游+下游，重跑（retry_upstream）
    corr = st.get("corrections", {})
    for m in ms_list:
        if (st.get("milestones", {}).get(m["id"]) == "fail"
                and m.get("on_fail") == "retry_upstream" and corr.get(m["id"], 0) < MAX_CORR):
            after = m["after_steps"]
            max_after = max(after)
            # 清空上游步骤及其所有下游（step 号更大者），让纠偏后重新消费
            for s in steps:
                if s["step"] in after or s["step"] > max_after:
                    st["artifacts"].pop(str(s["step"]), None)
            corr[m["id"]] = corr.get(m["id"], 0) + 1
            st["corrections"] = corr
            # 关键：清掉该里程碑的 fail 标记，让上游重跑后重新触发 milestone_check，
            # 否则会卡在 fail 态反复 correct 而非重新校验。
            st.setdefault("milestones", {}).pop(m["id"], None)
            return {
                "action": "correct",
                "milestone": m["id"],
                "rerun_steps": after,
                "hint": (f"里程碑 {m['id']} 校验失败 → 纠偏：清空上游步骤 {after} 及其下游，"
                         f"请【重新检索/补足】目标关键项后 --record 回写，再走一次里程碑校验。"
                         f"（已纠偏 {corr[m['id']]}/{MAX_CORR} 次）"),
            }
    # ③ 正常推进（parallel / execute_step / quality_check / retry_chain）

    # 找所有还没产出的步骤，按层分组；**总是先处理最早一层**，该层 ≥2 步则成批吐出（真并行）。
    incomplete = [s for s in steps if str(s["step"]) not in st["artifacts"]]
    if incomplete:
        # 缺 layer 字段时以 step 号当层，保证回退安全
        def _lyr(s):
            return s.get("layer", s["step"])
        min_layer = min(_lyr(s) for s in incomplete)
        frontier = [s for s in incomplete if _lyr(s) == min_layer]
        if len(frontier) >= 2:
            directives = []
            for s in frontier:
                resolved = _resolve_input(st, s["step"], rb)
                directives.append({
                    "step": s["step"],
                    "capability": s["capability"],
                    "tool": s["tool"],
                    "tier": s.get("tier"),
                    "produces": s.get("produces", ""),
                    "input_context": resolved or "（无上游 → 吃原始任务输入，即用户最初的目标）",
                })
            return {
                "action": "parallel",
                "layer": min_layer,
                "steps": [d["step"] for d in directives],
                "directives": directives,
                "hint": (f"同一并行层(层{min_layer})有 {len(directives)} 个互不依赖的步骤，"
                         f"请【并发】执行它们——WorkBuddy 运行时本就能一次性发出多个工具调用"
                         f"（如多条 WebFetch）。各步完成后分别 --record，再调一次推进。"),
            }
        # 单步（该层只有 1 步）→ 串行回退
        s = frontier[0]
        resolved = _resolve_input(st, s["step"], rb)
        return {
            "action": "execute_step",
            "step": s["step"],
            "capability": s["capability"],
            "tool": s["tool"],
            "tier": s.get("tier"),
            "produces": s.get("produces", ""),
            "parallel_with": s.get("parallel_with", []),
            "input_context": resolved or "（无上游 → 吃原始任务输入，即用户最初的目标）",
            "hint": (f"用「{s['tool']}」完成第 {s['step']} 步 [{s['capability']}]，"
                     f"产出写回 --record=\"{s['step']}:<你的产出摘要/落盘路径>\""
                     + (f"；可与步骤 {s['parallel_with']} 并行" if s.get("parallel_with") else "")),
        }

    # 所有步骤完成 → 质量门自检（adc）
    if st["gate"] is None:
        return {
            "action": "quality_check",
            "gates": rb.get("quality_gates", []),
            "iter": st["iter"],
            "max_iter": st["feedback_max_iter"],
            "hint": ("对照质量门自检交付物：达标 --record=\"gate:pass\"；不达标 --record=\"gate:fail\""
                     + (f"（将整链重试，当前第 {st['iter']} 次，上限 {st['feedback_max_iter']}）"
                        if st["feedback_max_iter"] else "（无反馈环，不自动重试）")),
        }

    # gate 已有结论
    if st["gate"] == "pass":
        st["status"] = "done"
        return {"action": "done", "status": "done", "iter": st["iter"],
                "hint": "质量门通过，闭环完成，交付物见各步产物"}
    # gate == fail
    if st["feedback_max_iter"] and st["iter"] < st["feedback_max_iter"]:
        st["iter"] += 1
        st["artifacts"] = {}
        st["gate"] = None
        return {"action": "retry_chain", "iter": st["iter"], "max_iter": st["feedback_max_iter"],
                "hint": f"整链重试第 {st['iter']} 次（刷新上下文，从步骤1重跑）"}
    st["status"] = "failed"
    return {"action": "failed", "status": "failed", "iter": st["iter"],
            "hint": f"已超过反馈环重试上限（{st['feedback_max_iter']}），闭环失败"}


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(
            "usage: exec_loop.py <runbook.json> [--reset] "
            "[--record=\"STEP:TEXT\" | --record=\"gate:pass|fail\"]\n")
        return 1
    rb_path = argv[1]
    if not os.path.isfile(rb_path):
        sys.stderr.write(f"[exec_loop] 找不到 runbook: {rb_path}\n")
        return 2
    rb = load_runbook(rb_path)
    st_path = _state_path(rb_path)
    st = init_state(rb)
    if os.path.isfile(st_path) and "--reset" not in argv:
        with open(st_path, encoding="utf-8") as f:
            st = json.load(f)

    if "--reset" in argv:
        save_state(st_path, st)  # 已是 init 状态，等同清空

    # 处理 --record=...
    for a in argv[2:]:
        if not a.startswith("--record="):
            continue
        rec = a[len("--record="):]
        if rec.startswith("gate:"):
            val = rec[5:].strip().lower()
            st["gate"] = "pass" if val in ("pass", "p") else "fail"
        elif rec.startswith("ms:"):
            val = rec[3:].strip().lower()
            # 应用到"当前待校验里程碑"（after_steps 完成且未检的第一个）
            ms_list = rb.get("milestones") or []
            pending = next((m for m in ms_list
                            if not st.get("milestones", {}).get(m["id"])
                            and all(str(s) in st["artifacts"] for s in m["after_steps"])), None)
            if pending:
                st.setdefault("milestones", {})[pending["id"]] = "pass" if val in ("pass", "p") else "fail"
            else:
                sys.stderr.write("[exec_loop] 当前没有待校验的里程碑，忽略该 ms: 记录\n")
        else:
            if ":" not in rec:
                sys.stderr.write("[exec_loop] --record 需 STEP:TEXT 或 gate:pass/fail\n")
                return 1
            idx = rec.find(":")
            step = rec[:idx].strip()
            text = rec[idx + 1:]
            if not step.isdigit():
                sys.stderr.write("[exec_loop] --record 步骤号需为整数\n")
                return 1
            st["artifacts"][step] = text

    directive = next_directive(rb, st)
    save_state(st_path, st)

    print("=" * 60)
    print(f"[exec_loop] 任务: {rb.get('name', '')}  | 状态: {st['status']}  | 迭代: {st['iter']}"
          + (f" / {st['feedback_max_iter']}" if st["feedback_max_iter"] else ""))
    print("=" * 60)
    print(json.dumps(directive, ensure_ascii=False, indent=2))

    # ---- 自驱动便捷：给出可直接回贴的下一步命令 ----
    # 诚实边界：本脚本在 Bash 沙箱里调不动 WorkBuddy 原生工具（Read/WebFetch/Write/Bash 属
    # agent 运行时），故"更强执行器"= 让 agent 的机械循环零摩擦——脚本把下一步该跑的命令
    # 直接拼好，agent/人工照贴即可，不必手工构造 --record。真·工具调用仍在 agent 侧。
    act = directive.get("action")
    if act == "execute_step":
        n = directive.get("step")
        print("\n▶ 下一步命令（照跑，把 <本步产出> 换成真实摘要/落盘路径）:")
        print(f'  python exec_loop.py "{rb_path}" --record="{n}:<本步产出摘要/落盘路径>"')
    elif act == "parallel":
        steps = directive.get("steps", [])
        recs = " ".join(f'--record="{n}:<步骤{n}产出>"' for n in steps)
        print(f"\n▶ 并行层命令（{len(steps)} 步并发完成后，一次性把各步产出写回；"
              "也可拆成多条分跑）：")
        print(f'  python exec_loop.py "{rb_path}" {recs}')
    elif act == "quality_check":
        print("\n▶ 质量门命令（达标/不达标二选一照跑）:")
        print(f'  python exec_loop.py "{rb_path}" --record="gate:pass"')
        print(f'  python exec_loop.py "{rb_path}" --record="gate:fail"')
    elif act == "milestone_check":
        print("\n▶ 里程碑校验命令（达标/漂移二选一照跑）:")
        print(f'  python exec_loop.py "{rb_path}" --record="ms:pass"')
        print(f'  python exec_loop.py "{rb_path}" --record="ms:fail"  # 触发纠偏重跑上游')
    elif act == "correct":
        steps = directive.get("rerun_steps", [])
        recs = " ".join(f'--record="{n}:<重检索产出>"' for n in steps)
        print(f"\n▶ 纠偏重跑命令（重新检索/补足后回写；也可拆成多条分跑）:")
        print(f'  python exec_loop.py "{rb_path}" {recs}')
    elif act == "retry_chain":
        print(f"\n▶ 整链重试已触发（第 {directive.get('iter')} 次）：上下文已清空，回到步骤1，"
              "下一轮照 execute_step 命令重跑。")
    elif act in ("done", "failed"):
        print(f"\n▶ 闭环结束（{act}）。交付物见各步 --record 写入的产出。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
