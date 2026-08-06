"""
circuit-agents · compiler.topology_memory
=========================================
"记忆与学习"：记录成功拓扑 + 失败节点 + 执行统计，下次遇到类似任务直接复用。

设计要点（第一层能力深化 · C）：
 · 持久化到 JSON 文件（默认 circuit-agents/.topology_memory.json），跨会话生效。
 · record(goal_desc, spec, result) 记录一次完整执行：拓扑、成功/失败、质量、延迟、失败节点。
 · recall(goal_desc) 模糊匹配历史任务（Jaccard 关键词重叠），返回最优（成功+高质量）的 spec。
 · 零回归：记忆文件不存在/损坏 → 空表，不影响正常编译/执行；record 失败 → 静默跳过。
 · 隐私：只存 goal 描述文本 + spec 结构 + 执行统计，不存任何 API key / 用户隐私。

诚实边界：
 · 模糊匹配是"保守近似"（关键词 Jaccard），不保证语义等价；最低相似度阈值 0.3。
 · 只推荐成功且质量 ≥ min_quality 的拓扑；失败记录仅供分析，不直接复用。
 · 记忆表上限 100 条（FIFO），避免无限增长。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

# ⑥ 多任务并行：record/recall 可能被多个线程同时调用（BatchExecutor 并发执行）。
# 该锁保护「读 _store + 写文件」的临界区，避免并发 record 互相覆盖丢数据、
# 以及 recall 读到半写的 _store。细粒度（仅临界区），不影响单线程性能。
_MEM_LOCK = threading.Lock()


class TopologyMemory:
    """持久化成功拓扑 + 失败节点 + 执行统计，供类似任务复用。"""

    def __init__(self, path: str | None = None):
        if path is None:
            # 默认存在 circuit-agents 项目根目录
            here = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(os.path.dirname(here), ".topology_memory.json")
        self.path = path
        self._store = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "entries" in data:
                        return data
            except Exception:
                pass
        return {"entries": []}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._store, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不影响执行

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简易分词：英文按词（≥2 字母）、中文按单字。小写化。"""
        if not text:
            return []
        return re.findall(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]", text.lower())

    def record(self, goal_desc: str, spec: dict, result: dict) -> dict | None:
        """记录一次执行：goal 描述 + spec 拓扑 + 执行结果。

        返回 entry dict（或 None 表示记录失败）。零回归：任何异常静默吞掉。
        """
        try:
            # ⑥ 线程安全：临界区内「重新加载 → 追加 → 写回」，
            # 避免 BatchExecutor 并发执行时各实例 _store 相互独立、互相覆盖丢数据。
            with _MEM_LOCK:
                self._store = self._load()
                # 提取电阻节点的能力标签
                components = spec.get("components", {})
                caps = [c.get("label", "") for c in components.values()
                        if c.get("type") == "resistor"]

                entry = {
                    "goal_desc": (goal_desc or "")[:500],  # 截断防膨胀
                    "spec_name": spec.get("name", ""),
                    "capabilities": caps,
                    "n_nodes": len(components),
                    "spec": spec,
                    "result": {
                        "success": result.get("success", False),
                        "final_quality": result.get("final_quality", 0),
                        "total_latency_ms": result.get("total_latency_ms", 0),
                        "total_cost": result.get("total_cost", 0),
                    },
                    "failed_nodes": [c for c, v in (result.get("components") or {}).items()
                                     if not v.get("ok")],
                    "timestamp": time.time(),
                }
                self._store["entries"].append(entry)
                # FIFO 上限 100 条
                if len(self._store["entries"]) > 100:
                    self._store["entries"] = self._store["entries"][-100:]
                self._save()
            return entry
        except Exception:
            return None

    def recall(self, goal_desc: str, min_quality: float = 0.7,
               min_similarity: float = 0.3) -> dict | None:
        """模糊匹配历史任务，返回最优（相似度最高 + 成功 + 质量达标）的 spec。

        返回 {"spec": ..., "score": ..., "original_goal": ..., "quality": ...} 或 None。
        """
        goal_words = set(self._tokenize(goal_desc))
        if not goal_words:
            return None

        # ⑥ 加锁 + 锁内重载：既防止读到并发 record 半写的 _store，也读到最新提交记录
        with _MEM_LOCK:
            self._store = self._load()
            best = None
            best_score = 0.0
            for entry in self._store.get("entries", []):
                # 只推荐成功的、质量达标的
                r = entry.get("result", {})
                if not r.get("success") or r.get("final_quality", 0) < min_quality:
                    continue

                entry_words = set(self._tokenize(entry.get("goal_desc", "")))
                if not entry_words:
                    continue

                # Jaccard 相似度
                overlap = len(goal_words & entry_words)
                union = len(goal_words | entry_words)
                score = overlap / union if union else 0.0

                if score > best_score:
                    best_score = score
                    best = entry

            if best and best_score >= min_similarity:
                return {
                    "spec": best.get("spec", {}),
                    "score": round(best_score, 3),
                    "original_goal": best.get("goal_desc", ""),
                    "quality": best.get("result", {}).get("final_quality", 0),
                }
            return None

    def stats(self) -> dict:
        """返回记忆表统计（条数、成功率、平均质量）。"""
        entries = self._store.get("entries", [])
        if not entries:
            return {"total": 0, "success_rate": 0, "avg_quality": 0}
        n_success = sum(1 for e in entries if e.get("result", {}).get("success"))
        qualities = [e.get("result", {}).get("final_quality", 0) for e in entries]
        return {
            "total": len(entries),
            "success_rate": round(n_success / len(entries), 3),
            "avg_quality": round(sum(qualities) / len(qualities), 3),
        }

    def recent(self, n: int = 5) -> list:
        """返回最近 n 条执行记录（dict 列表，含 spec/result），供心跳/复盘。

        加锁 + 锁内重载，避免读到并发 record 半写的 _store。任何异常静默返回 []。
        """
        try:
            with _MEM_LOCK:
                self._store = self._load()
                return list(self._store.get("entries", []))[-n:]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# 离线自检（无需外部依赖）
# ---------------------------------------------------------------------------
def selftest():
    import tempfile

    # 用临时文件做隔离测试
    tmp = tempfile.mktemp(suffix=".json")
    mem = TopologyMemory(path=tmp)

    # 1) 空表 recall → None
    assert mem.recall("任意任务") is None, "空表应返回 None"
    print("✓ 空表 recall: 返回 None（零回归）")

    # 2) record → recall 全链路
    spec1 = {
        "name": "test_circuit",
        "components": {
            "n1": {"type": "resistor", "label": "retrieve"},
            "n2": {"type": "resistor", "label": "reason"},
        },
        "wires": [["n1", "n2"]],
    }
    result1 = {
        "success": True,
        "final_quality": 0.9,
        "total_latency_ms": 1500,
        "total_cost": 0.01,
        "components": {"n1": {"ok": True}, "n2": {"ok": True}},
    }
    entry = mem.record("检索GDP数据并分析趋势", spec1, result1)
    assert entry is not None, "record 应返回 entry"
    assert entry["result"]["success"] is True
    assert entry["capabilities"] == ["retrieve", "reason"]
    print("✓ record: 记录成功拓扑 + 执行统计 + 能力标签")

    # 3) recall 精确匹配
    hit = mem.recall("检索GDP数据并分析趋势")
    assert hit is not None, "应命中"
    assert hit["spec"]["name"] == "test_circuit"
    assert hit["quality"] == 0.9
    assert hit["score"] == 1.0, f"完全匹配 score 应为 1.0，实际 {hit['score']}"
    print("✓ recall: 精确匹配命中（score=1.0, quality=0.9）")

    # 4) recall 模糊匹配（部分关键词重叠）
    hit2 = mem.recall("检索GDP数据然后对比")
    assert hit2 is not None, "部分重叠应命中"
    assert hit2["score"] > 0.3, f"相似度应 >0.3，实际 {hit2['score']}"
    print(f"✓ recall: 模糊匹配命中（score={hit2['score']}，部分关键词重叠）")

    # 5) recall 不匹配 → None
    hit3 = mem.recall("翻译一篇日文文章")
    assert hit3 is None, "完全不相关应返回 None"
    print("✓ recall: 不相关任务返回 None（min_similarity 阈值生效）")

    # 6) 失败记录不推荐
    spec2 = {"name": "fail_circuit", "components": {}, "wires": []}
    result2 = {"success": False, "final_quality": 0.3,
               "total_latency_ms": 100, "total_cost": 0,
               "components": {}}
    mem.record("某个失败的任务", spec2, result2)
    hit4 = mem.recall("某个失败的任务")
    assert hit4 is None, "失败记录不应被推荐"
    print("✓ 失败记录: 不被 recall 推荐（只推成功+质量达标）")

    # 7) 低质量不推荐
    spec3 = {"name": "low_q", "components": {}, "wires": []}
    result3 = {"success": True, "final_quality": 0.5,
               "total_latency_ms": 100, "total_cost": 0,
               "components": {}}
    mem.record("低质量但成功的任务xyz", spec3, result3)
    hit5 = mem.recall("低质量但成功的任务xyz")
    assert hit5 is None, "低质量（<0.7）不应被推荐"
    print("✓ 低质量记录: quality < min_quality 不被推荐")

    # 8) stats 统计
    s = mem.stats()
    assert s["total"] == 3, f"应有 3 条记录，实际 {s['total']}"
    assert s["success_rate"] == round(2 / 3, 3), f"成功率 2/3，实际 {s['success_rate']}"
    print(f"✓ stats: total={s['total']} success_rate={s['success_rate']} avg_quality={s['avg_quality']}")

    # 9) 持久化：重新加载能读到记录
    mem2 = TopologyMemory(path=tmp)
    assert len(mem2._store["entries"]) == 3, "重新加载应有 3 条"
    hit6 = mem2.recall("检索GDP数据并分析趋势")
    assert hit6 is not None, "重新加载后应仍能命中"
    print("✓ 持久化: 重新加载后记忆不丢失")

    # 10) FIFO 上限
    mem3 = TopologyMemory(path=tempfile.mktemp(suffix=".json"))
    for i in range(105):
        mem3.record(f"task_{i}", {"name": f"c{i}", "components": {}, "wires": []},
                    {"success": True, "final_quality": 0.9,
                     "total_latency_ms": 1, "total_cost": 0, "components": {}})
    assert len(mem3._store["entries"]) == 100, \
        f"FIFO 上限 100，实际 {len(mem3._store['entries'])}"
    print("✓ FIFO: 超过 100 条自动淘汰旧记录")

    # 清理
    for p in (tmp,):
        try:
            os.unlink(p)
        except Exception:
            pass

    print("\ntopology_memory 离线自检全部通过 ✓")


if __name__ == "__main__":
    selftest()
