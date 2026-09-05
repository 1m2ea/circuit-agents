# MemoryBus 统一抽象层设计（对照真实代码版）

> 目标：把三套各自为政的存储统一到一个读写接口下。
> 本文所有接口签名均**逐字取自当前代码**（2026-09-05 实测导出），不是示意。
> 背景与缺口来源：`MEMORY_ARCHITECTURE.md` §4.3 第 3 条。

---

## 0. 先说结论：三套存储不能压进同一个 key 空间

它们的**主键语义根本不同**，强行扁平化会立刻出错：

| 维度 | `TopologyMemory` | `ExecutionStore` | `ShareRepo` |
|---|---|---|---|
| 文件 | `compiler/topology_memory.py` | `execution_store.py` | `compiler/share.py` |
| 介质 | JSON 文件 | SQLite (`executions.db`) | JSON 文件 (`.topology_repo.json`) |
| 主键 | `goal_desc`（**模糊·语义匹配**） | `run_id`（精确） | `name`（精确） |
| 检索 | Jaccard 相似度 | 精确 id / status 过滤 | 精确 name |
| 单条删除 | ❌ **无**（仅 FIFO 淘汰） | ✅ `delete` | ✅ `remove` |
| 容量策略 | FIFO 100 | 无上限 | 无上限 |
| 并发控制 | ❌ 无（`_load`/`_save` 全量读写） | SQLite 事务 | ❌ 无 |

**设计决策**：`MemoryBus` 按 **`kind` 分区路由**，不做全局扁平 key。
`kind` ∈ `topology` | `execution` | `shared`。

---

## 1. 真实 API 签名（实测导出，非推测）

```python
# compiler/topology_memory.py
class TopologyMemory:
    def __init__(self, path: str | None = None)
    def recall(self, goal_desc: str, min_quality: float = 0.7,
               min_similarity: float = 0.3) -> dict | None
    def record(self, goal_desc: str, spec: dict, result: dict) -> dict | None
    def recent(self, n: int = 5) -> list
    def stats(self) -> dict

# execution_store.py
class ExecutionStore:
    def __init__(self, db_path: str = "executions.db")
    def save(self, run_id: str, goal: str, status: str, spec: dict,
             events: list, result: dict, tags: Optional[list] = None) -> str
    def load(self, run_id: str) -> Optional[dict]
    def list_recent(self, limit: int = 20) -> list[dict]
    def list_by_status(self, status: str, limit: int = 20) -> list[dict]
    def replay(self, run_id: str) -> Optional[dict]
    def update_status(self, run_id: str, status: str, result: Optional[dict] = None)
    def delete(self, run_id: str) -> bool
    def count(self) -> int

# compiler/share.py
class ShareRepo:
    def __init__(self, path: str = ".topology_repo.json")
    def publish(self, spec, author: str = "anonymous",
                tags: Optional[list] = None, name: Optional[str] = None) -> str
    def fetch(self, name: str)
    def pull(self, name: str)
    def list(self)
    def remove(self, name: str) -> bool
```

---

## 2. 统一接口

```python
@dataclass
class MemoryRecord:
    kind: str                  # "topology" | "execution" | "shared"
    id: str                    # 各后端原生主键
    payload: Any               # spec / result / record 本体
    meta: dict = field(default_factory=dict)   # quality, created_at, tags, goal_desc…


class MemoryBus:
    def read(self, kind: str, id: str) -> MemoryRecord | None: ...
    def write(self, record: MemoryRecord) -> str: ...
    def recall(self, query: str, kind: str = "topology",
               min_quality: float = 0.7,
               min_similarity: float = 0.3) -> MemoryRecord | None: ...
    def search(self, kind: str, **filters) -> list[MemoryRecord]: ...
    def forget(self, kind: str, id: str) -> bool: ...
```

---

## 3. 三个适配器（纯转发，不动原类）

### 3.1 `TopologyMemoryAdapter`（kind=`topology`）

| MemoryBus | 真实调用 | 备注 |
|---|---|---|
| `recall` | `TopologyMemory.recall(goal_desc, min_quality, min_similarity)` | **唯一原生语义检索** |
| `write` | `TopologyMemory.record(goal_desc, spec, result)` | FIFO 100 自动淘汰 |
| `search` | `recent(n)` + `stats()` | 只能按最近 N 条，无条件过滤 |
| `read` | 无原生按 id 读 | → 走 `recent()` 后客户端过滤（诚实标注） |
| `forget` | ❌ **无** | 见风险 R2 |

### 3.2 `ExecutionStoreAdapter`（kind=`execution`）

| MemoryBus | 真实调用 |
|---|---|
| `read` | `load(run_id)` |
| `write` | `save(run_id, goal, status, spec, events, result, tags)` |
| `search` | `list_recent(limit)` / `list_by_status(status, limit)` |
| `forget` | ✅ `delete(run_id)` |
| （额外）`replay` | `replay(run_id)` |

### 3.3 `ShareRepoAdapter`（kind=`shared`）

| MemoryBus | 真实调用 |
|---|---|
| `read` | `fetch(name)` |
| `write` | `publish(spec, author, tags, name)` |
| `search` | `list()` |
| `forget` | ✅ `remove(name)` |

---

## 4. 迁移风险（对着代码说，不编）

| # | 风险 | 证据 | 处置 |
|---|---|---|---|
| R1 | **主键语义冲突** | topology 走 Jaccard 模糊匹配，另两套走精确匹配 | 按 `kind` 分区，禁止跨 kind 统一 key |
| R2 | **topology 无单条删除** | 类中只有 `record/recall/recent/stats`，无 delete | 新增 `delete(goal_desc)`（纯新增），或重写 JSON 文件 |
| R3 | **两个 JSON 无并发控制** | `_load()` 全量读 + `_save()` 全量写 | 引入文件锁，或长期迁 SQLite |
| R4 | **`recall` 未在规划期自动调用** | §4.3 原文 | **已闭环**：3d00fe0 让 `compile_goal` 带出 `goal_desc`，`plan.py` 编译前 `recall` |
| R5 | **`ExecutionStore` 无遗忘策略** | 只有 `delete`，无 TTL/归档 | 加 TTL 归档任务 |
| R6 | **`fetch` 与 `pull` 语义重复** | `ShareRepo` 两个取方法并存 | 适配前需先澄清语义，否则 adapter 会选错 |
| R7 | **质量信号未跨库打通** | topology 存 `quality`，executions 存 `result`，互不可见 | `MemoryRecord.meta` 统一承载 `quality` |

---

## 5. 向后兼容

- **只新增、不改原类**：三个 Adapter 是纯转发壳，符合项目一贯的
  「内核零改动、只扩封装层」原则。
- 现有调用点全部保持不动：`compile.py` 的 `recall`、`runtime.py` 的
  `mem.record(...)`、`server.py` 的 `ExecutionStore` —— 逐个自愿迁移，无强制。
- 未配置时 `MemoryBus` 直接退化为对三个原类的直接调用（零回归路径）。

---

## 6. 最小落地步骤

1. **新建 `memory_bus.py`**：`MemoryRecord` + `MemoryBus` 抽象基类（约 60 行）。
2. **三个 Adapter**，每个约 40 行纯转发，不动原类。
3. **给 `TopologyMemory` 补 `delete(goal_desc)`** —— 全流程唯一必须改原类的地方，且是纯新增。
4. **接一个调用点验证**：把 `compile.py` 里的 `recall` 改走 `bus.recall(...)`，
   跑 `python -m compiler.compile` 的 selftest + `python runtime.py` 全量，确认零回归。

---

## 附：为什么不用"系统自动产出"的那一版

同一次实测里，circuit-agents 自己也生成了一版 15,327 字符的 MemoryBus 设计。
**不可用**，原因三条，均可复核（产物见
`~/.workbuddy/skills/circuit-planner/scripts/outputs/memorybus_design_outputs.json`）：

1. **幻觉后端**：设计中的适配器是 `Redis / SQLite / FS-JSON`。
   Redis 在**生成正文里出现 26 次**（storage_specs 10 / risk_analysis 5 /
   implementation_steps 5 / interface_design 4 / adaptation_plan 2）。
   而 Redis 在本项目中的唯一痕迹是 `server.py:153` 的一行注释
   「内存存储（生产环境应换 SQLite/Redis）」——**是未来可选项，不是现有存储**。
2. **未点名真实存储**：`topology_memory` / `executions.db` / `ShareRepo`
   三个真名在**生成正文里出现 0 次**，只写「现有存储1/2/3」这类占位符。
   （产出 JSON 里这三个名字各出现过 1 次，但那是 `goal` 字段对输入目标的回显，
   非模型生成内容——对账时务必区分，否则会得出错误结论。）
3. **质量门失效**：该次 `final_quality=0.99` —— 但拓扑里**没有 `verify` 节点**，
   唯一的 `adc` 门与生成内容是**同一个 DeepSeek 后端**，即模型自评。
   0.99 分完全没发现上述两处硬伤。

→ 结论：**过程指标（success / quality）不能作为交付是否可用的证据**，必须人工对账代码。
