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
 · 模糊匹配是"保守近似"（P1：中文 bigram + 英文词的 TF-IDF 余弦，比早期
   单字 Jaccard 多了词序与词频信息，但仍是词面相似、非真正语义等价）；
   recall 最低相似度阈值 0.3。
 · 只推荐成功且质量 ≥ min_quality 的拓扑；失败记录仅供分析，不直接复用。
 · 记忆表上限 100 条（FIFO），教训库上限 200 条，避免无限增长。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter

# ⑥ 多任务并行：record/recall 可能被多个线程同时调用（BatchExecutor 并发执行）。
# RLock（可重入）而非 Lock：recall() 持锁期间会再调 recall_lessons()（② 教训召回），
# 普通 Lock 同线程重入会死锁。细粒度（仅临界区），不影响单线程性能。
_MEM_LOCK = threading.RLock()


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
        return {"entries": [], "lessons": []}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._store, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不影响执行

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简易分词：英文按词（≥2 字母）、中文按单字。小写化。

        （P1 起召回改用 _ngram_tokenize + TF-IDF，本方法保留供调试/兼容。）
        """
        if not text:
            return []
        return re.findall(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]", text.lower())

    @staticmethod
    def _ngram_tokenize(text: str) -> list[str]:
        """P1 语义召回分词：英文按词（≥2 字母），中文按字 bigram（带边界符）。

        · 中文 bigram（"检索GDP"→ ^检 检索 索G + gdp）比单字 Jaccard 更接近
          语义单元，"幻觉组件" 与 "组件幻觉" 能共享 bigram 而单字集完全相同
          的问题也得到缓解（顺序信息进入特征）。
        · 边界符 ^/$ 让短查询的首尾字有区分度。
        · 零依赖：纯正则 + Counter，不引入任何外部包。
        """
        if not text:
            return []
        text = text.lower()
        tokens = re.findall(r"[a-zA-Z]{2,}", text)
        zh = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        padded = "^" + zh + "$"
        tokens.extend(padded[i:i + 2] for i in range(len(padded) - 1))
        return tokens

    def _tfidf_cosine_all(self, query: str, doc_texts: dict) -> dict:
        """P1 核心评分：query 对多个文档的 TF-IDF 余弦相似度，一次算完。

        doc_texts: {key: text}。返回 {key: cosine∈[0,1]}（异常全 0，零回归）。
        · idf 语料 = 当前全部候选文档（查询词不在语料中的项跳过——反正匹配不到）。
        · 权重 = (1+ln(tf)) * idf，sublinear tf 压低高频词、idf 提升区分词。
        """
        empty = {k: 0.0 for k in doc_texts}
        try:
            q_tokens = self._ngram_tokenize(query)
            if not q_tokens:
                return empty
            doc_tokens = {k: self._ngram_tokenize(t) for k, t in doc_texts.items()}
            df = Counter()
            for toks in doc_tokens.values():
                df.update(set(toks))
            n_docs = max(len(doc_tokens), 1)

            def idf(t: str) -> float:
                return math.log((n_docs + 1) / (df.get(t, 0) + 1)) + 1.0

            # 查询向量：只保留语料中出现过的词（其余项对点积无贡献）
            q_tf = Counter(q_tokens)
            q_weights = {t: (1 + math.log(c)) * idf(t)
                         for t, c in q_tf.items() if t in df}
            if not q_weights:
                return empty
            q_norm = math.sqrt(sum(w * w for w in q_weights.values()))
            if not q_norm:
                return empty

            scores = {}
            for k, toks in doc_tokens.items():
                if not toks:
                    scores[k] = 0.0
                    continue
                tf = Counter(toks)
                d_weights = {t: (1 + math.log(c)) * idf(t)
                             for t, c in tf.items() if t in df}
                d_norm = math.sqrt(sum(w * w for w in d_weights.values()))
                if not d_norm:
                    scores[k] = 0.0
                    continue
                dot = sum(qw * dw for t, dw in d_weights.items()
                          if (qw := q_weights.get(t)))
                scores[k] = dot / (q_norm * d_norm)
            return scores
        except Exception:
            return empty

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
        goal_words = set(self._ngram_tokenize(goal_desc))
        if not goal_words:
            return None

        # ⑥ 加锁 + 锁内重载：既防止读到并发 record 半写的 _store，也读到最新提交记录
        with _MEM_LOCK:
            self._store = self._load()
            # P1 语义召回：只对"成功+质量达标"的候选算 TF-IDF 余弦（一次批量算完）
            candidates = {}
            for i, entry in enumerate(self._store.get("entries", [])):
                r = entry.get("result", {})
                if r.get("success") and r.get("final_quality", 0) >= min_quality \
                        and entry.get("goal_desc"):
                    candidates[i] = entry

            if not candidates:
                return None
            scores = self._tfidf_cosine_all(
                goal_desc, {i: e.get("goal_desc", "") for i, e in candidates.items()})

            best_i, best_score = None, 0.0
            for i, s in scores.items():
                if s > best_score:
                    best_score = s
                    best_i = i

            if best_i is not None and best_score >= min_similarity:
                best = candidates[best_i]
                return {
                    "spec": best.get("spec", {}),
                    "score": round(best_score, 3),
                    "original_goal": best.get("goal_desc", ""),
                    "quality": best.get("result", {}).get("final_quality", 0),
                    # ② 第二圈：教训随拓扑一起召回——复用历史成功经验的同时
                    #    提醒执行方"这类任务踩过什么坑"。
                    "lessons": self.recall_lessons(goal_desc),
                }
            return None

    # ---- ② 第二圈：教训库（不只存拓扑，还存"踩过的坑"，让错误不随进程蒸发）----
    def record_lesson(self, text: str, tags: list | None = None) -> dict | None:
        """记录一条经验教训（跨 run 持久化）。

        与拓扑记录（entries，FIFO 100）分开存放：教训价值不随时间衰减，
        故独立 FIFO 上限 200 条。P2 自动沉淀会产生重复文本 → 归一化后
        完全相同的教训跳过（不再重复入库）。零回归：异常静默返回 None。
        """
        try:
            norm = re.sub(r"\s+", "", str(text or ""))
            if not norm:
                return None
            with _MEM_LOCK:
                self._store = self._load()
                # 去重：归一化文本完全相同 → 跳过（返回已有条目，行为可预期）
                for les in self._store.get("lessons", []):
                    if re.sub(r"\s+", "", les.get("text", "")) == norm:
                        return les
                lesson = {
                    "text": str(text).strip()[:500],
                    "tags": [str(t) for t in (tags or [])][:8],
                    "timestamp": time.time(),
                }
                self._store.setdefault("lessons", []).append(lesson)
                if len(self._store["lessons"]) > 200:
                    self._store["lessons"] = self._store["lessons"][-200:]
                self._save()
            return lesson
        except Exception:
            return None

    def recent_lessons(self, n: int = 2) -> list:
        """P2 常驻兜底：返回最近 n 条教训（不按相似度，保证教训永不失联）。

        用途：compile 织入时若语义召回无命中，仍把最近的坑带进提示词——
        否则"命中才注入"会让教训库形同虚设。零回归：异常/空表返回 []。
        """
        try:
            with _MEM_LOCK:
                self._store = self._load()
                out = []
                for les in reversed(self._store.get("lessons", [])):
                    if les.get("text", "").strip():
                        out.append({"text": les.get("text", ""),
                                    "tags": les.get("tags", []),
                                    "score": None})
                    if len(out) >= max(0, n):
                        break
                return out
        except Exception:
            return []

    def recall_lessons(self, query: str, min_score: float = 0.1,
                       top_k: int = 3) -> list:
        """按语义相似度召回相关教训（P1：TF-IDF 余弦 over 正文+tags），最相关的在前。

        返回 [{"text", "tags", "score"}]，无命中返回 []。零回归：异常静默 []。
        """
        try:
            if not self._ngram_tokenize(query):
                return []
            with _MEM_LOCK:
                self._store = self._load()
                lessons = [les for les in self._store.get("lessons", [])
                           if les.get("text", "").strip()
                           or les.get("tags")]
                if not lessons:
                    return []
                # 正文 + tags 合并为一个文档参与评分（tags 词权重等同正文词）
                doc_texts = {i: (les.get("text", "") + " "
                                 + " ".join(les.get("tags", [])))
                             for i, les in enumerate(lessons)}
            # 评分放锁外（纯计算，不碰 _store）
            scores = self._tfidf_cosine_all(query, doc_texts)
            scored = [{"text": lessons[i].get("text", ""),
                       "tags": lessons[i].get("tags", []),
                       "score": round(s, 3)}
                      for i, s in scores.items() if s >= min_score]
            scored.sort(key=lambda x: -x["score"])
            return scored[:top_k]
        except Exception:
            return []

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

    # 11) ② 教训库：record_lesson + recall_lessons + recall 附带 lessons
    les = mem.record_lesson("retrieve 节点必须先检索真实源码再写设计，"
                            "否则会幻觉出不存在的组件如 Redis",
                            tags=["hallucination", "grounding"])
    assert les is not None, "record_lesson 应返回 lesson"
    got = mem.recall_lessons("retrieve 节点会不会幻觉出组件")
    assert got and got[0]["score"] > 0.1, f"应召回教训，got {got}"
    assert "幻觉" in got[0]["text"]
    print(f"✓ 教训库: record_lesson + recall_lessons 命中（score={got[0]['score']}）")

    hit7 = mem.recall("检索GDP数据并分析趋势")
    assert hit7 is not None and isinstance(hit7.get("lessons"), list), \
        "recall 结果应附带 lessons 字段（可为空列表）"
    print(f"✓ 教训随召回: recall() 结果携带 lessons 字段（本次 {len(hit7['lessons'])} 条）")

    # 12) P1 语义召回：bigram 词序信息让相似度可区分（Jaccard 单字集做不到）
    mem4 = TopologyMemory(path=tempfile.mktemp(suffix=".json"))
    mem4.record_lesson("检索节点必须先取真实源码，不能凭空编造接口", tags=["grounding"])
    mem4.record_lesson("番茄钟后台被杀后通知失效，要用本地通知插件保活", tags=["notify"])
    s_data = mem4.recall_lessons("检索节点编造接口怎么办")
    s_notify = mem4.recall_lessons("番茄钟通知失效怎么保活")
    assert s_data and s_data[0]["tags"] == ["grounding"], \
        f"检索类查询应命中 grounding 教训，got {s_data}"
    assert s_notify and s_notify[0]["tags"] == ["notify"], \
        f"通知类查询应命中 notify 教训，got {s_notify}"
    assert s_data[0]["score"] > 0.3 and s_notify[0]["score"] > 0.3, \
        f"语义相关的两条都应显著命中（{s_data[0]['score']} / {s_notify[0]['score']}）"
    print(f"✓ P1 语义召回: 两条教训各自被正确区分命中 "
          f"(grounding={s_data[0]['score']}, notify={s_notify[0]['score']})")

    # 13) P1 语序鲁棒：词序打乱仍应命中同一教训（Jaccard 单字集本就相同，
    #     bigram 靠共享 bigram 仍保持高相似）
    s_rev = mem4.recall_lessons("接口编造不能源码真实取先节点检索")
    assert s_rev and s_rev[0]["tags"] == ["grounding"], \
        f"乱序查询应仍命中 grounding，got {s_rev}"
    print(f"✓ P1 语序鲁棒: 乱序查询仍命中 (score={s_rev[0]['score']})")

    # 14) P2 自动沉淀去重：归一化相同的教训只入一次
    before = len(mem4._store["lessons"])
    dup1 = mem4.record_lesson("检索节点编造接口")   # 新教训 → 入库
    assert dup1 is not None
    assert len(mem4._store["lessons"]) == before + 1, "新教训应入库一次"
    dup2 = mem4.record_lesson("检索节点  编造\n接口")   # 空白不同、归一化相同 → 去重
    assert dup2 is not None
    assert len(mem4._store["lessons"]) == before + 1, "重复教训不应重复入库"
    assert dup2["text"] == dup1["text"], "重复记录应返回已有条目"
    print("✓ P2 去重: 归一化相同文本的教训只入一次库")

    # 15) P2 recent_lessons：不按相似度的常驻兜底
    got_rec = mem4.recent_lessons(1)
    assert len(got_rec) == 1 and got_rec[0]["text"], "recent_lessons 应返回最近 1 条"
    assert got_rec[0]["score"] is None, "recent_lessons 非相似度召回，score=None"
    empty_mem = TopologyMemory(path=tempfile.mktemp(suffix=".json"))
    assert empty_mem.recent_lessons(2) == [], "空表 recent_lessons 应返回 []"
    print("✓ P2 recent_lessons: 常驻兜底可用（最近优先、空表安全）")

    # 清理
    for p in (tmp,):
        try:
            os.unlink(p)
        except Exception:
            pass

    print("\ntopology_memory 离线自检全部通过 ✓")


if __name__ == "__main__":
    selftest()
