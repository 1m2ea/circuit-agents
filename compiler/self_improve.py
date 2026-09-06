"""
circuit-agents · compiler.self_improve
======================================
P3 元循环 v0：让系统能"对自身说话"——诊断最近 run 的真实失败模式，
产出结构化改进提案（任务书格式），写入 proposals/improvements.md，
人工确认后由外部执行者实施。

这是第三层循环的雏形：
  第一层 = 单次 run（推理→工具→核验）；
  第二层 = 跨会话（失败自动沉淀教训 → 下次编译期召回，见 P2）；
  第三层 = 元循环（本模块：系统诊断【自身代码】的缺陷并提议如何改系统）。

诚实边界（必须读）：
 · v0 提案是【规则式】的：从固定提案目录按失败模式匹配，填入真实事实
   （失败节点/原因/频次/证据）。它不发明新方案——只把"系统已知的改法"
   格式化成可执行任务书。这是能力边界，不是缺陷伪装成智能。
 · 提案绝不自动实施：只写文件，状态永远"待人工确认"。
 · 诊断数据只来自 TopologyMemory（真实执行记录 + P2 自动沉淀教训），零编造。
 · 无失败/无数据 → 返回"无可诊断内容"，不硬凑提案。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter

# 项目根目录（circuit-agents/）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROPOSALS_PATH = os.path.join(_PROJECT_ROOT, "proposals", "improvements.md")


# ---------------------------------------------------------------------------
# 诊断：从 TopologyMemory 提取真实失败模式
# ---------------------------------------------------------------------------
def diagnose(mem=None, top_n: int = 20) -> dict:
    """扫描最近 top_n 次 run 的执行记录，聚合失败模式。

    返回 {"runs", "failed_runs", "patterns": [{"label","reason","count",
    "evidence":[...]}], "lessons": [自动沉淀教训文本]}。
    零编造：只统计 entries 里真实存在的 failed_nodes + 真实 reason。
    """
    try:
        if mem is None:
            from .topology_memory import TopologyMemory
            mem = TopologyMemory()
        entries = mem.recent(top_n)
        lessons = [l.get("text", "") for l in mem.recent_lessons(50)]
        runs = len(entries)
        failed_runs = 0
        # (label, reason_kind) -> {"count": n, "evidence": [goal_desc...]}
        agg: dict = {}
        for e in entries:
            res = e.get("result", {}) or {}
            if res.get("success"):
                continue
            failed_runs += 1
            spec = e.get("spec", {}) or {}
            comps = spec.get("components", {}) or {}
            for cid in (e.get("failed_nodes") or []):
                comp = comps.get(cid, {}) or {}
                label = str(comp.get("label", cid)).split("#")[0] or cid
                key = (label, "node_fail")
                _bump(agg, key, e.get("goal_desc", ""))
            qg = res.get("quality_gate")
            if isinstance(qg, dict) and qg.get("passed") is False:
                _bump(agg, ("quality_gate", "gate_fail"), e.get("goal_desc", ""))
        # 从 P2 自动沉淀教训里补抓 reason（教训文本含 [open=xxx]/[error=xxx]）
        for les in lessons:
            m = re.search(r"([a-zA-Z_]+)\([a-zA-Z0-9_]+\)失败\[(open|error)=([^\]]+)\]", les)
            if m:
                kind = re.sub(r"[:=].*$", "", m.group(3)).strip() or m.group(2)
                _bump(agg, (m.group(1), kind), les[:60])
        patterns = sorted(
            ({"label": k[0], "reason": k[1], "count": v["count"],
              "evidence": v["evidence"][:3]} for k, v in agg.items()),
            key=lambda p: -p["count"])
        return {"runs": runs, "failed_runs": failed_runs,
                "patterns": patterns, "lessons": lessons[:5]}
    except Exception as e:
        return {"runs": 0, "failed_runs": 0, "patterns": [],
                "lessons": [], "error": f"{type(e).__name__}: {e}"}


def _bump(agg: dict, key, evidence: str):
    slot = agg.setdefault(key, {"count": 0, "evidence": []})
    slot["count"] += 1
    if evidence and evidence not in slot["evidence"]:
        slot["evidence"].append(evidence)


# ---------------------------------------------------------------------------
# 提案目录：失败模式 → 已知改法（规则式，诚实声明"不发明新方案"）
# ---------------------------------------------------------------------------
def _proposal_for(pattern: dict) -> dict | None:
    """按失败模式匹配提案目录。返回任务书 dict 或 None（未收录则不硬凑）。"""
    label, reason, count = pattern["label"], pattern["reason"], pattern["count"]
    if reason == "http_error":
        return {
            "title": f"网络层加固（{label} 节点 http_error ×{count}）",
            "file": "compiler/backend_llm.py / compiler/http_retry.py",
            "how": ("审查退避参数（重试次数/退避曲线）与 timeout 配置；"
                    "对超时率最高的 tier 考虑提高 VERIFY_TIMEOUT/请求超时，"
                    "或为该节点配置重试预算。"),
            "accept": ("离线：http_retry 自检通过；在线：同类任务 http_error "
                       "频次下降（用本模块 diagnose 复测对比）。"),
            "risk": "重试预算加大会推高延迟与成本——需同时观察 total_latency_ms。",
        }
    if reason == "no_input":
        return {
            "title": f"上游开路传导（{label} 因无输入开路 ×{count}）",
            "file": "runtime.py（data_fill 预算 / _auto_fill 路径）",
            "how": ("排查上游检索节点为何没产出：预算是否耗尽、检索技能是否"
                    "返回空。必要时提高 data_fill_budget 或为该 label 配置兜底源。"),
            "accept": "同类任务不再出现 no_input 开路；diagnose 复测该模式归零。",
            "risk": "放宽预算会增加调用量——控制在预算上限内。",
        }
    if reason == "gate_fail" or label == "quality_gate":
        return {
            "title": f"质量门未过 ×{count}：为高频失败能力加异构校验/拆步",
            "file": "compiler/compile.py（_ensure_hetero_verify / _weave_lessons）",
            "how": ("对失败最集中的能力 label 前插 verify#<label> 异构校验节点"
                    "（复用既有 _ensure_hetero_verify 机制），并把相关历史教训"
                    "定向织入该节点。"),
            "accept": "配置 VERIFY_* 后同类任务质量门通过；selftest 全过。",
            "risk": "多一跳校验增加延迟——verify 节点用 tool 档模型控成本。",
        }
    if reason in ("p2_test_forced",):  # 测试注入的故障不属于系统缺陷
        return None
    return {
        "title": f"失败模式待人工分析（{label}/{reason} ×{count}）",
        "file": "（需人工定位）",
        "how": ("该失败模式未收录进规则式提案目录。请结合 evidence 中的任务"
                "描述人工定位根因；定位后把已知改法补进 _proposal_for，"
                "让下次自动提案覆盖它。"),
        "accept": "根因明确并录入提案目录。",
        "risk": "无（只读诊断，不改代码）。",
    }


# ---------------------------------------------------------------------------
# 提案产出：proposals/improvements.md（任务书格式）
# ---------------------------------------------------------------------------
def propose(diag: dict | None = None, mem=None, path: str | None = None) -> dict:
    """诊断 → 生成提案 → 追加写入 proposals/improvements.md。

    返回 {"path", "written": bool, "proposal": dict|None, "diag": diag}。
    无失败/未收录模式 → written=False，诚实说明，不硬凑。
    """
    if diag is None:
        diag = diagnose(mem)
    out_path = path or PROPOSALS_PATH
    result = {"path": out_path, "written": False,
              "proposal": None, "diag": diag}
    if not diag.get("patterns"):
        return result
    top, prop = None, None
    # 第一轮：优先选目录里有【具体改法】的模式（如 http_error/gate_fail），
    # 泛化"待人工分析"提案让位给可执行的具体提案。
    for p in diag["patterns"]:
        cand = _proposal_for(p)
        if cand is not None and "待人工分析" not in cand["title"]:
            top, prop = p, cand
            break
    # 第二轮：全是泛化/不可提案模式 → 用最频模式的泛化提案
    if prop is None:
        for p in diag["patterns"]:
            cand = _proposal_for(p)
            if cand is not None:
                top, prop = p, cand
                break
    if prop is None:
        return result

    n = _next_issue_no(out_path)
    ts = time.strftime("%Y-%m-%d %H:%M")
    facts = "\n".join(
        f"- `{p['label']}` × {p['count']}（reason={p['reason']}；"
        f"证据: {' / '.join(p['evidence'][:2]) or '无'}）"
        for p in diag["patterns"][:3])
    lessons_txt = "\n".join(f"- {l[:120]}" for l in diag.get("lessons", [])[:3]) \
        or "- （无自动沉淀教训）"
    md = f"""

---

# 自我改进提案 #{n:03d} · {ts}

> ⚠️ 状态：**待人工确认**。本提案由系统规则式自诊断产出，未经确认不得实施。

## 1. 诊断事实（最近 {diag.get('runs', '?')} 次 run，失败 {diag.get('failed_runs', '?')} 次）

{facts}

### 自动沉淀教训（P2 闭环产物）

{lessons_txt}

## 2. 最优先失败模式

`{top['label']}` / `{top['reason']}`（{top['count']} 次）

## 3. 改进任务书

| 项 | 内容 |
|---|---|
| 改哪个文件 | {prop['file']} |
| 怎么改 | {prop['how']} |
| 验收标准 | {prop['accept']} |
| 风险/回滚 | {prop['risk']} |

*规则式提案声明：以上改法来自系统已收录的提案目录（`compiler/self_improve.py` → `_proposal_for`），事实部分来自真实执行记录，方案部分不含臆造。*
"""
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(md)
        result["written"] = True
        result["proposal"] = {"no": n, **prop, "pattern": top}
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _next_issue_no(path: str) -> int:
    """读现有提案文件，返回下一个编号（无文件从 1 开始）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            nums = [int(m) for m in re.findall(r"#(\d{3})", f.read())]
        return max(nums) + 1 if nums else 1
    except OSError:
        return 1


# ---------------------------------------------------------------------------
# SKILLS 注册（function-calling 可用）+ CLI
# ---------------------------------------------------------------------------
def _skill_self_improve(top_n: int = 20) -> str:
    r = propose(diagnose(top_n=int(top_n or 20)))
    d = r["diag"]
    if r.get("written"):
        p = r["proposal"]
        return (f"诊断 {d['runs']} 次 run（失败 {d['failed_runs']}），"
                f"最优先模式 {p['pattern']['label']}/{p['pattern']['reason']}"
                f"×{p['pattern']['count']}。提案 #{p['no']:03d} 已写入 {r['path']}"
                f"（待人工确认，不得自动实施）。")
    if not d.get("patterns"):
        return (f"诊断 {d['runs']} 次 run（失败 {d['failed_runs']}）："
                "无失败模式，无可提案内容。")
    return "最优先失败模式未收录进提案目录，需人工分析（详见 diagnose 输出）。"


def register_skill():
    """把 self_improve 注册进 agent_skills.SKILLS（幂等）。"""
    from . import agent_skills
    if "self_improve" in agent_skills.SKILLS:
        return
    agent_skills.SKILLS["self_improve"] = {
        "name": "self_improve",
        "description": (
            "元循环自诊断：扫描最近运行的执行记录，聚合真实失败模式，"
            "产出自我改进提案（任务书格式）写入 proposals/improvements.md。"
            "提案只写文件、绝不自动实施。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer",
                           "description": "诊断窗口：最近多少次 run（默认 20）"},
            },
            "required": [],
        },
        "handler": _skill_self_improve,
    }


def selftest():
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="p3test_"), "mem.json")
    from .topology_memory import TopologyMemory
    mem = TopologyMemory(path=tmp)
    spec = {"name": "t", "components": {
        "r1": {"type": "resistor", "label": "retrieve"},
        "r2": {"type": "resistor", "label": "reason"}}, "wires": []}

    def run_spec(success, quality, failed):
        mem.record("测试任务A", spec, {
            "success": success, "final_quality": quality,
            "total_latency_ms": 1, "total_cost": 0,
            "components": {c: {"ok": c not in failed} for c in ("r1", "r2")},
            "failed_nodes": failed,
            "quality_gate": {"threshold": 0.8, "passed": quality >= 0.8},
        })

    # 1) 无失败 → 不硬凑提案
    run_spec(True, 0.9, [])
    d0 = diagnose(mem)
    assert d0["failed_runs"] == 0 and d0["patterns"] == []
    p0 = propose(d0, mem, path=os.path.join(tempfile.mkdtemp(), "imp.md"))
    assert not p0["written"]
    print("✓ 无失败 run: diagnose 零模式、propose 不硬凑（诚实边界）")

    # 2) 失败聚合 + 提案任务书
    run_spec(False, 0.3, ["r2"])
    run_spec(False, 0.3, ["r2"])
    run_spec(True, 0.9, [])
    d1 = diagnose(mem)
    assert d1["runs"] == 4 and d1["failed_runs"] == 2
    top = d1["patterns"][0]
    assert top["label"] == "reason" and top["count"] == 2, f"应聚合 reason×2, got {top}"
    out_md = os.path.join(tempfile.mkdtemp(prefix="p3t_"), "imp.md")
    p1 = propose(d1, mem, path=out_md)
    assert p1["written"] and p1["proposal"]["no"] == 1
    text = open(out_md, encoding="utf-8").read()
    for must in ("待人工确认", "诊断事实", "改进任务书", "验收标准",
                 "规则式提案声明", "reason"):
        assert must in text, f"提案文件缺关键节: {must}"
    print(f"✓ 失败聚合+提案: reason×2 → 提案 #001 写入文件（任务书格式完整）")

    # 3) 编号递增（幂等再写一份）
    p2 = propose(d1, mem, path=out_md)
    assert p2["proposal"]["no"] == 2, "同一文件再次提案应递增编号"
    print("✓ 编号递增: improvements.md 多次提案编号连续")

    # 4) 技能注册 + execute_skill 可用
    register_skill()
    from .agent_skills import SKILLS, execute_skill
    assert "self_improve" in SKILLS
    out = execute_skill("self_improve", json.dumps({"top_n": 5}))
    assert "诊断" in out, f"execute_skill(self_improve) 应返回诊断摘要, got {out}"
    print(f"✓ 技能注册: SKILLS['self_improve'] 可经 execute_skill 调用")

    print("\nself_improve 元循环 v0 离线自检全部通过 ✓")


if __name__ == "__main__":
    selftest()
