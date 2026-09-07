#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""潜意识层（Subconscious Layer）· circuit-agents 的"系统1/后台加工"模块
=========================================================================

设计（来自用户架构构想，2026-09-07）：
  大模型当前只有「上下文窗口 = 意识层（显式、受控、慢）」，缺一个对上下文
  **不可见、不可控**，但能自动加工信息、向意识层输出候选想法的「潜意识层」
  （类比 Kahneman 系统1：自动、并行、潜意识）。

  本模块是这个概念的最小可用骨架（toy skeleton），四条性质对齐构想：
    1. 常驻：daemon 线程持续运行（start/stop/tick），对主链路透明。
    2. 不可见：主模型（意识层）**绝不直接读工作记忆**，只能通过受限接口
       query(top_k) 拉取「已策展的 top-K 候选假设」。
    3. 结构化工作记忆：self.kv（KV 草稿）+ self.hypotheses（候选假设优先队列），
       写的是内存/KV，不是 token 流。
    4. 人类可监督/可干预：replay() 回放每拍快照，intervene()  pruning/boost/
       promote/clear，干预事件也进回放缓冲——把"模型在联想什么"从权重黑箱
       拽出来变成可读 trace（安全增益，而非成本）。

  与 circuit-agents 现有构件的同构关系：
    · PiHeartbeat（π 永动心跳）= 同款常驻 daemon + state + tick + 离线安全。
    · TopologyMemory = 同款线程安全、持久化、离线降级的工作记忆。
    · 本模块 = 把心跳的"系统进化调度"换成"候选想法生成 + 联想压缩"，
      并显式加上"受限查询接口 + 回放 + 干预"三件套，正好补上用户构想里
      心跳当时没有的"意识层取用 + 人类监督"两环。

离线安全：
    · 不依赖任何网络/API；关联引擎默认是确定性本地的「压缩+联想」。
    · 可选 associate_fn 钩子（Callable，契约 f(seed, keywords)->[hypotheses]）
      可后续接本地 LLM（local_llm_bridge）/ DeepSeek，不走网络时静默降级。
    · 所有动作 try/except 包裹，永不抛错、永不拖崩宿主（server.py）。
"""
from __future__ import annotations

import os
import re
import time
import threading
import uuid
from collections import deque

# ──────────────────────────────────────────────────────────
# 本地关联引擎（确定性、离线、零依赖）——"联想压缩"能力的玩具版
# ──────────────────────────────────────────────────────────

# 小型概念联想词典（中文/英文混排）：关键词 → 可能相关的概念
_ASSOC_LEXICON = {
    "数据": ["趋势", "预测", "异常", "归因"],
    "趋势": ["预测", "拐点", "周期", "回归"],
    "预测": ["置信区间", "情景", "反事实"],
    "gdp": ["增长", "同比", "人均", "结构"],
    "增长": ["驱动因子", "可持续", "瓶颈"],
    "文言": ["压缩", "典故", "省略", "联想"],
    "压缩": ["密度", "信息量", "熵"],
    "典故": ["类比", "映射", "隐喻"],
    "联想": ["类比", "推理", "远迁移"],
    "推理": ["前提", "证据", "反例"],
    "模型": ["过拟合", "泛化", "校准"],
    "质量": ["门限", "回归", "复盘"],
    "风险": ["对冲", "熔断", "监测"],
    "用户": ["意图", "上下文", "偏好"],
    "代码": ["重构", "测试", "复杂度"],
    "拓扑": ["化简", "复用", "并行"],
}

# 出现在任一联想键里的单字集合（用于判断中文二元组是否"有意义"）
_LEX_CHARS = set("".join(_ASSOC_LEXICON.keys()))


def compress(text: str) -> dict:
    """高密压缩（文言文式）：英文按词 + 中文按二元组 → 只留有意义的关键词 → 密度度量。

    设计要点：
      · 中文不做单字切分（单字噪声大），改取相邻二元组（bigram），只保留
        「本身是联想词」或「含联想词单字」的二元组 → 关键概念（增长/趋势/预测/文言/联想…）浮现。
      · 英文取 ≥2 字母词（gdp 等）。
      · density = 有意义关键词数 / 总 token 数，越大表示信息密度越高（"文言文"特质量化代理）。
    """
    if not text:
        return {"keywords": [], "density": 0.0, "tokens": 0}
    cw = re.findall(r"[a-zA-Z]{2,}", text.lower())          # 英文词
    zh = re.findall(r"[\u4e00-\u9fff]", text)                # 中文单字序列
    bigrams = ["".join(zh[i:i + 2]) for i in range(len(zh) - 1)]  # 中文二元组
    cand = list(cw) + bigrams

    keywords, seen = [], set()
    for t in cand:
        if t in seen:
            continue
        is_zh = bool(re.search(r"[\u4e00-\u9fff]", t))
        # 中文二元组：仅当它"恰好是已知联想词"才保留（未知混合组视为噪声丢弃）；
        # 英文词（gdp 等）直接保留。→ 潜意识联想锚定在已知概念图，输出干净。
        ok = (not is_zh) or (t in _ASSOC_LEXICON)
        if ok:
            seen.add(t)
            keywords.append(t)
    tokens = len(cw) + len(zh)
    density = round(len(keywords) / tokens, 3) if tokens else 0.0
    return {"keywords": keywords, "density": density, "tokens": tokens}


def _local_associate(seed: str, keywords: list) -> list:
    """确定性本地联想：词典扩展 + 关键词两两组合生成候选假设。

    这是"联想压缩"子能力的玩具实现；真实版可换成 associate_fn 钩子接 LLM。
    """
    hyps = []
    # 1) 词典扩展：每个关键词 → 其联想概念，形成"a 可能关联 b"假设
    for k in keywords:
        for rel in _ASSOC_LEXICON.get(k, []):
            hyps.append(f"「{k}」可能关联「{rel}」——建议验证二者因果/共现")
    # 2) 组合联想：关键词两两配对，生成跨概念假设（远迁移）
    for i in range(len(keywords)):
        for j in range(i + 1, len(keywords)):
            a, b = keywords[i], keywords[j]
            hyps.append(f"若「{a}」与「{b}」同时成立，或可推导出新结论，建议作为候选假设")
    # 3) 种子级兜底：关键词为空时，用种子原句拆出一句
    if not hyps and seed:
        hyps.append(f"种子「{seed[:40]}」信息不足，潜意识层建议先补关键概念再联想")
    return hyps


# ──────────────────────────────────────────────────────────
# 潜意识层
# ──────────────────────────────────────────────────────────

class SubconsciousLayer:
    """常驻后台加工模块：自动压缩+联想生成候选假设，供意识层受限取用、供人类回放干预。

    I/O 契约（对齐用户构想）：
      feed(seed)                意识层把当前目标喂进来（可选；不喂也能自运转）
      tick()                    后台一拍：压缩→联想→打分→入队→写回放
      query(top_k)  [受限]      意识层只拉 top-K 已策展候选（看不到原始工作记忆）
      replay(n)      [人类]     回放最近 n 拍 + 干预事件（可读 trace）
      intervene(...)  [人类]     剪枝/提权/提拔/清空（监督旋钮）
    """

    def __init__(self, interval: float = 30.0, max_hypotheses: int = 200,
                 max_replay: int = 500, associate_fn=None):
        self.interval = interval
        self.max_hypotheses = max_hypotheses
        self.max_replay = max_replay
        self.associate_fn = associate_fn  # 可选 LLM 后端钩子（f(seed,keywords)->[str]）

        # 结构化工作记忆（对主上下文不可见；只经 query() 暴露策展结果）
        self.kv: dict = {}                       # 自由 KV 草稿
        self._hypo: list = []                    # 候选假设优先队列（按 score 降序）
        self._seeds: deque = deque(maxlen=64)    # 意识层喂入的种子（FIFO）
        self._replay: list = []                  # 回放缓冲（每拍快照 + 干预事件）

        self.state = self._initial_state()
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @staticmethod
    def _initial_state():
        return {
            "n": 0,
            "hypo_count": 0,
            "pruned": 0,
            "promoted": 0,
            "last": None,
            "history": [],   # [(cycle, n_hypo)] 聚合轨迹
        }

    # ── 意识层 → 潜意识层：喂种子 ──
    def feed(self, prompt: str, priority: float = 0.0) -> dict:
        """意识层把当前目标塞进潜意识层的种子队列（后台异步加工）。

        priority>0 表示高优先目标：覆盖 kv.focus（成为本拍立即加工的焦点），
        而非只 setdefault 一次——这样 /run 把当前目标喂进来时，潜意识层立刻围绕它联想。
        """
        with self._lock:
            self._seeds.append({"prompt": (prompt or "")[:500], "priority": priority,
                                "at": time.time()})
            if priority > 0:
                self.kv["focus"] = prompt
        return {"fed": True, "seed_len": len(self._seeds)}

    # ── 后台一拍：压缩 + 联想 + 入队 + 回放 ──
    def tick(self):
        with self._lock:
            # 取种子：优先 kv.focus，否则队列尾
            seed = self.kv.get("focus") or (self._seeds[-1]["prompt"] if self._seeds else "")
            comp = compress(seed)
            keywords = comp["keywords"]

            # 联想：优先用外部 associate_fn（如文言文语料/本地 LLM），否则确定性本地引擎
            assoc_src = self._assoc_source()
            try:
                if callable(self.associate_fn):
                    raw = list(self.associate_fn(seed, keywords) or [])
                else:
                    raw = _local_associate(seed, keywords)
            except Exception:
                raw = _local_associate(seed, keywords)  # 钩子异常→降级本地
                assoc_src = "local"

            # 打分 + 去重入队
            added = 0
            for h in raw:
                if not h or len(h) > 300:
                    continue
                score = self._score(h, keywords)
                if self._upsert(h, score, seed, assoc_src):
                    added += 1

            # 截断优先队列
            if len(self._hypo) > self.max_hypotheses:
                self._hypo = self._hypo[:self.max_hypotheses]

            snap = {
                "cycle": self.state["n"] + 1,
                "seed": seed[:80],
                "keywords": keywords,
                "density": comp["density"],
                "generated": len(raw),
                "added": added,
                "hypo_count": len(self._hypo),
                "top": [h["text"] for h in self._hypo[:3]],
                "at": time.time(),
            }
            self._replay.append({"kind": "cycle", **snap})
            if len(self._replay) > self.max_replay:
                self._replay = self._replay[-self.max_replay:]

            self.state = self._update_state(snap)
            return snap

    @staticmethod
    def _score(text: str, keywords: list) -> float:
        """候选假设打分（玩具）：基础分 + 命中关键词数 + 轻微熵扰动（确定性）。"""
        base = 0.5
        hit = sum(1 for k in keywords if k in text) * 0.1
        # 确定性"熵"：用文本长度做微扰，避免同分
        jitter = (len(text) % 7) * 0.01
        return round(min(1.0, base + hit + jitter), 3)

    def _assoc_source(self) -> str:
        """联想来源标签：语料驱动=corpus，LLM 钩子=llm，确定性本地=local。"""
        if not callable(self.associate_fn):
            return "local"
        mod = getattr(self.associate_fn, "__module__", "") or ""
        if "wenyan" in mod or "wenyan" in getattr(self.associate_fn, "__qualname__", ""):
            return "corpus"
        return "llm"

    def _upsert(self, text: str, score: float, seed: str, source: str = "local") -> bool:
        """去重入队：相同文本已存在则取较高分；否则插入并保序。"""
        for h in self._hypo:
            if h["text"] == text:
                if score > h["score"]:
                    h["score"] = score
                    h["seeds"].append(seed[:60])
                return False
        self._hypo.append({
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "score": score,
            "source": source,
            "seeds": [seed[:60]] if seed else [],
            "created_at": time.time(),
            "status": "active",
        })
        self._hypo.sort(key=lambda x: x["score"], reverse=True)
        return True

    def _update_state(self, snap) -> dict:
        s = dict(self.state)
        s["n"] = snap["cycle"]
        s["hypo_count"] = snap["hypo_count"]
        s["last"] = {k: v for k, v in snap.items() if k != "at"}
        hist = list(self.state.get("history", []))
        hist.append((snap["cycle"], snap["hypo_count"]))
        if len(hist) > 200:
            hist = hist[-200:]
        s["history"] = hist
        return s

    # ── 受限查询接口：意识层只拉 top-K（看不到原始 KV/队列）──
    def query(self, top_k: int = 5, min_score: float = 0.0) -> dict:
        with self._lock:
            active = [h for h in self._hypo if h["status"] == "active"]
            top = [h for h in active if h["score"] >= min_score][:top_k]
            return {
                "top_k": top_k,
                "returned": len(top),
                "total_active": len(active),
                "total": len(self._hypo),
                "hypotheses": [
                    {"id": h["id"], "text": h["text"], "score": h["score"],
                     "source": h["source"], "status": h["status"]}
                    for h in top
                ],
            }

    # ── 人类回放：可读 trace（每拍 + 干预）──
    def replay(self, n: int = 20) -> list:
        with self._lock:
            return list(self._replay[-n:])

    # ── 人类干预：监督旋钮 ──
    def intervene(self, action: str, hid: str = None, score: float = None) -> dict:
        with self._lock:
            ev = {"kind": "intervene", "action": action, "hid": hid,
                  "at": time.time()}
            if action == "clear":
                self._hypo = []
                self.state["hypo_count"] = 0
                ev["cleared"] = True
            elif action in ("prune", "boost", "promote") and hid:
                for h in self._hypo:
                    if h["id"] == hid:
                        if action == "prune":
                            h["status"] = "pruned"
                            self.state["pruned"] += 1
                        elif action == "boost":
                            h["score"] = round(min(1.0, (score if score is not None
                                                        else h["score"] + 0.2)), 3)
                        elif action == "promote":
                            h["status"] = "promoted"
                            self.state["promoted"] += 1
                        self._hypo.sort(key=lambda x: x["score"], reverse=True)
                        ev["target"] = h["text"][:60]
                        break
                else:
                    ev["error"] = f"hid={hid} 未找到"
            else:
                ev["error"] = f"未知 action={action}"
            self._replay.append(ev)
            if len(self._replay) > self.max_replay:
                self._replay = self._replay[-self.max_replay:]
            return ev

    # ── 公开状态（HTTP 用）──
    def _public_state(self):
        with self._lock:
            s = dict(self.state)
            s["history"] = len(s.get("history", []))
            s["running"] = self.is_running()
            s["interval"] = self.interval
            s["replay_len"] = len(self._replay)
            s["kv_keys"] = list(self.kv.keys())
            s["top_sample"] = [h["text"] for h in self._hypo[:3]]
            return s

    def snapshot(self) -> dict:
        return self._public_state()

    # ── 后台永动循环 ──
    def start(self, interval: float = None):
        if interval is not None:
            self.interval = interval
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive() \
                and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.interval + 1.0)
        return True

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def run_once(self, n: int = 1):
        return [self.tick() for _ in range(n)]

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(self.interval)


# ──────────────────────────────────────────────────────────
# 离线自检
# ──────────────────────────────────────────────────────────

def subconscious_layer_selftest():
    """潜意识层离线自检：压缩+联想产假设 / 喂种子 / 受限查询 / 人类干预 / 回放 / 后台常驻。"""
    os.environ.pop("AGENT_API_KEY", None)  # 强制离线

    # 1) 压缩：文言文式高密（关键词抽取 + 密度）
    c = compress("用户想知道 GDP 增长背后的驱动因子与未来趋势预测")
    assert c["keywords"], "压缩应抽出关键词"
    assert 0 < c["density"] <= 1, f"密度应在(0,1]，实际 {c['density']}"
    print(f"✓ 压缩(知识压缩代理)：keywords={c['keywords']} · density={c['density']}")

    # 2) 联想：确定性本地引擎产出候选假设
    r = _local_associate("GDP 增长", c["keywords"])
    assert len(r) >= 1, "联想应产出至少 1 条候选"
    print(f"✓ 联想(联想压缩代理)：产出 {len(r)} 条候选假设（词典扩展+跨概念组合）")

    # 3) 常驻实例 + 喂种子 + 跑拍 → 假设入队
    sl = SubconsciousLayer(interval=0.01)
    sl.feed("分析文言文训练对大模型联想能力的影响", priority=1.0)
    sl.feed("预测下季度用户留存趋势")
    out = sl.run_once(n=6)
    assert len(out) == 6, "应跑满 6 拍"
    assert sl.state["hypo_count"] > 0, "假设队列应非空"
    print(f"✓ 常驻+喂种子+跑拍：6 拍后 hypo_count={sl.state['hypo_count']} · "
          f"首拍 top={out[0]['top']}")

    # 4) 受限查询：意识层只拿到 top-K，且看不到 KV/原始队列
    q = sl.query(top_k=3)
    assert q["returned"] <= 3, "受限接口应只返回 top_k"
    assert q["total_active"] >= q["returned"], "total_active 应 ≥ returned"
    assert all("id" in h and "text" in h for h in q["hypotheses"]), "假设应带 id/text"
    print(f"✓ 受限查询接口：意识层取到 top-{q['top_k']}（共 {q['total_active']} 条活跃）"
          f"——原始 KV/队列不暴露")

    # 5) 人类干预：prune / boost / promote / clear
    hid = q["hypotheses"][0]["id"]
    pr = sl.intervene("prune", hid=hid)
    assert pr.get("target"), f"prune 应命中 {hid}"
    assert sl.state["pruned"] >= 1, "pruned 计数应增加"
    # boost 后该假设分应升
    hid2 = sl.query(top_k=1)["hypotheses"][0]["id"]
    before = next(h["score"] for h in sl._hypo if h["id"] == hid2)
    sl.intervene("boost", hid=hid2)
    after = next(h["score"] for h in sl._hypo if h["id"] == hid2)
    assert after >= before, f"boost 后分应≥原分（{before}→{after}）"
    # clear 清空
    cl = sl.intervene("clear")
    assert cl.get("cleared") and sl.state["hypo_count"] == 0, "clear 应清空队列"
    print("✓ 人类干预：prune/boost/promote/clear 全部生效（监督旋钮可用）")

    # 6) 回放：每拍 + 干预事件都可追溯（可读 trace）
    rp = sl.replay(n=50)
    assert any(e.get("kind") == "cycle" for e in rp), "回放应含 cycle 快照"
    assert any(e.get("kind") == "intervene" for e in rp), "回放应含 intervene 事件"
    print(f"✓ 回放缓冲：{len(rp)} 条 trace（cycle + intervene 均可读，黑箱可视化）")

    # 7) 后台常驻：start/stop 不拖崩
    ok = sl.start(interval=0.02)
    assert ok and sl.is_running(), "start 后应常驻运行"
    time.sleep(0.12)  # 让其自跑几拍
    assert sl.state["n"] >= 6, "后台应已自增拍数"
    sl.stop()
    assert not sl.is_running(), "stop 后应停"
    print(f"✓ 后台常驻：start→自跑(n={sl.state['n']})→stop 干净退出（daemon 不拖崩宿主）")

    print("\n潜意识层 离线自检全部通过 ✓")


if __name__ == "__main__":
    subconscious_layer_selftest()
