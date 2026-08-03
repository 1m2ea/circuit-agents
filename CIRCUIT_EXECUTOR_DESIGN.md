# CircuitExecutor 架构设计书（命名漂移加固 · 后续演进）

> 状态：草图（待确认后再改 `runtime.py`）。本文件是 A 阶段交付物，B/C/D 的实现均以此为准。
> 作者：circuit-agents 协作。落款 2026-08-03。

---

## 0. 一句话定位

把 `circuit-agents` 从一个"规划参谋部"（产出 Runbook / 拓扑，靠人手或 LLM 自助执行）
升级为"前线指挥部"（`CircuitExecutor`：自动并发、闭环反馈、动态调技能、状态内部流转）。

**诚实声明（基于 2026-08-03 代码实测）**：执行内核 `runtime.py` 与技能执行层
`compiler/agent_skills.py` 并非空白——它们已经能"跑拓扑、并发层、重试、档位升级、LLM
在场时调技能"。所以 CircuitExecutor 不是从零造，而是**补齐三处真缺口 + 把已有能力串成闭环**。

---

## 1. 现状盘点（Ground Truth，非想象）

### 1.1 已经有的（复用，不重造）

| 能力 | 位置 | 说明 |
|---|---|---|
| 状态信号 | `runtime.Signal` (`value/quality/ok/cost/latency_ms/meta`) | 全链路数据载体 |
| 拓扑分层 + 并发 | `Circuit.layers()` + `propagate()` 内 `ThreadPoolExecutor` | **并联层已真并发**（同层节点线程池并行） |
| 线性关系自测闸 | `Circuit._run_one()` 的 `required_inputs` vs `produced_outputs`，`input_map` 命名漂移转接头 | `gate:fail_linear` 已能抓"缺数据/漂移" |
| 反馈重试 | `Circuit.execute(self_heal, watchdog)` + `_escalate_failed()` | 失败电阻**档位升级**（small→large→tool）后重跑 |
| 跨轮健康 | `runtime.Watchdog` | 连续平庸带 → `degraded`，预升级 |
| 技能注册+执行 | `agent_skills.SKILLS` / `execute_skill(name, json)` | 真执行 web_search / run_code / query_db / calculator… |
| LLM 在场时的工具循环 | `LLMAgentBackend.run()` 内 `_MAX_TOOL_ITERS=8` 工具回灌 | 模型自发 `tool_calls` → `execute_skill` → 续生成 |

### 1.2 真正的缺口（CircuitExecutor 要补的）

1. **反馈环是"升级档位"，不是"自动补数据"**。
   节点 `gate:fail_linear` 报 `missing=[x]` 时，执行器只做"升级这个电阻的 tier 再跑一次"——
   但 tier 升级救不了"上游根本没产出 x"这种**数据缺失**。理想态：发现缺 `x` →
   **自动派发一个检索/技能动作去产出 x** → 合成信号喂回 → 自动重跑该节点。
   （这正是你说的"推理节点卡住→就没然后了"的根因。）

2. **技能调用依赖 LLM 在场**。
   `execute_skill` 是纯函数，谁都能调；但当前只有 `LLMAgentBackend`（真 LLM）会在 `run()` 里调它。
   用 `SimBackend` 或静态拓扑时，节点**声明了需要 web_search 也不会被触发**。
   应让**执行器**在运行时按节点声明主动派发技能，不依赖 LLM。

3. **无跨节点共享状态总线 / 无动态拓扑**。
   `Signal.meta` 只在前驱→后继间透传"产物名"，没有一块"全局黑板上写了什么"供任意节点查询；
   也没有"第一个检索步骤结果决定第二步拓扑"的多任务进化能力。

---

## 2. 设计原则（守住不破）

- **零回归**：现有 `Circuit.propagate/execute` 语义、Signal、开路不崩、命名漂移转接头全部保留。
- **增量封装**：新增 `CircuitExecutor` 类**包装** `Circuit`，不动 `Signal`/`Circuit` 核心；
  老代码 `Circuit(...).execute()` 继续可用。
- **SimBackend 兜底**：任何真工具/技能调用都必须有确定性 fallback（无 key / 无网 / 无 LLM 也能跑通主干）。
- **不开路就崩**：单次技能/检索失败 → 返回可读错误文本（沿用 `execute_skill` 的"开路但不崩"），
  不炸整链；只有"关键数据反复补不到"才上抛 `gate:fail_linear`。
- **安全**：`run_code` 执行模型生成 Python 的现状（临时目录 + 10s 超时，无完整沙箱）保留并标注；
  CircuitExecutor 不扩大该攻击面。

---

## 3. 五大块设计

### 3.1 状态总线（State Bus）—— 显式化

复用 `Signal.meta` 透传，新增 `CircuitExecutor.state: dict`（运行期黑板）：

```
executor.state = {
  "_fetched": { "china_gdp_2024": "<检索到的文本>" },  # 自动补数据写这里
  "_skills_used": ["web_search"],
  "_trace": [ ... ]   # 每步动作日志，供调试/CI 断言
}
```

- 节点运行时可读 `state`（如 `_fetched` 里有没有自己缺的数据）。
- 自动补数据 / 技能结果统一写回 `state._fetched` 与 `_trace`。

### 3.2 自动并发（保留并增强）

- **保留**：`propagate()` 已有 `ThreadPoolExecutor` 并联层并发 → 直接复用，不改。
- **增强（可选，C 阶段）**：把"层内节点"的执行从线程池升级为 `concurrent.futures` +
  真 I/O 并发（LLM/检索是网络 bound，线程池已够；若后续要 asyncio 真并行再演进）。
  本设计**不强制**改并发模型，先把"补数据闭环"做对。

### 3.3 闭环反馈 · 自动补数据（核心新增）

在 `CircuitExecutor.run()` 中，对 `gate:fail_linear` 节点做**数据补全循环**（区别于现有
tier 升级）：

```
for node in 拓扑(分层):
    sig = _run_one(node)                 # 现有线性关系闸
    budget = DATA_FILL_BUDGET            # 如 2 次
    while sig.meta.gate == "fail_linear" and budget > 0:
        missing = sig.meta["missing"]    # 如 ["china_gdp_2024"]
        for m in missing:
            if m not in executor.state["_fetched"]:
                result = executor.dispatch_fill(m, node)   # 见 3.4
                executor.state["_fetched"][m] = result
        # 用补给信号构造"虚拟前驱"，重跑该节点
        sig = _run_one_with_injected(node, executor.state["_fetched"])
        budget -= 1
    if sig.meta.gate == "fail_linear":
        # 补不到 → 仍然诚实 fail（沿用现有语义，不掩盖）
        ...
```

`dispatch_fill(missing, node)`：
- 先看节点是否声明了"如何去取 `missing`"（如 `node["fillers"][missing] = {"skill":"web_search","args":{"query":...}}`）；
- 否则用默认检索策略（web_search 该 missing 名）；
- 返回文本，写 `state._fetched`，合成 `Signal(value=文本, quality=0.6, ok=True)` 注入。

> 这就是你描述的"推理节点发现缺数据 → 自动触发检索 → 拿回 → 自动继续"。**闭环在
> 执行器内部完成，不再等人工判断。**

### 3.4 动态技能调用（Dynamic Skill Dispatch）—— 执行器主动派发

节点 spec 新增可选字段（不写则退化为现状）：

```
{
  "type": "resistor", "label": "reason", "model": "large",
  "required_inputs": ["x"],
  "produced_outputs": ["y"],
  "skills": ["web_search", "run_code"],     # 本节点可主动调用的技能
  "fillers": { "x": {"skill": "web_search",  # 缺 x 时怎么补
                     "args": {"query": "china gdp 2024"}} }
}
```

`CircuitExecutor.dispatch(node, skill_name, args)`：
- 调 `agent_skills.execute_skill(skill_name, json.dumps(args))`；
- 或调真实工具（`web_fetch` 经 `urllib` / 真实 WebFetch 接缝）；
- 结果写 `state._fetched` / `_trace`，返回 `Signal`。

**关键**：即便用 `SimBackend`（无 LLM），只要节点声明了 `skills`/`fillers`，执行器也会
真实调用技能——技能不再是"封在图纸上"，而是执行器手里随时能发的"手"。

### 3.5 多任务进化（Stretch，D 之后）

基于 `state._fetched` / 终节点的中间结果，动态生成"下一步拓扑"：
- 例："研究最新 AI Agent 框架，若发现 >5 个，重点分析最热 3 个" → 第一步检索结果写
  `state`，`CircuitExecutor` 据计数动态拼出第二步子电路（分析 top3）并入队执行。
- 实现：在 `run()` 末尾加 `maybe_evolve(state) -> Optional[sub_spec]`，非空则递归执行。
- **已实现（最小可用，见 runtime.py `CircuitExecutor.maybe_evolve` / `_as_list` / `_build_subcircuit`）**：
  `maybe_evolve` 默认扫描 `state._fetched` 中「JSON 列表且长度 > `evolve_threshold`(默认5)」的检索结果，
  取前 `evolve_top_k`(默认3) 条动态拼出第二步「分析子电路」并递归执行（子电路 `evolve_enabled=False` 防无限递归），
  结果存 `state["_evolved"]`。离线自检 `circuit_executor_evolve_selftest` 已验证
  「research 检索到 8 框架(>5) → 拼『分析 top3』子电路递归 → `_evolved` 存在且 analysis ok」。
  接入点：`compiler/demo.py --executor`（末尾跑 `run_executor_showcase` 演示 ① 补数闭环 ② 多任务进化）；
  `compiler/verify_drift_smoke.py --executor`（剥映射→执行器自动补数救活节点，CI 闭环冒烟）。

---

## 4. 接口草图（落到代码长这样）

```python
# runtime.py 新增（不破坏 Circuit）
class CircuitExecutor:
    def __init__(self, circuit: Circuit, state: dict | None = None,
                 data_fill_budget: int = 2, skills_enabled: bool = True):
        self.circuit = circuit
        self.state = state or {"_fetched": {}, "_skills_used": [], "_trace": []}
        self.budget = data_fill_budget
        self.skills_enabled = skills_enabled

    def run(self):
        """分层 propagate + 自动补数据闭环 + 动态技能派发。返回与 execute() 同构的 res。"""
        out, lat, cost = self._propagate_with_fill()   # 复用 circuit.layers()
        return self._summarize(out, lat, cost)

    def _propagate_with_fill(self):
        out = {}
        for layer in self.circuit.layers():
            for cid in layer:                      # 并联层这里走 circuit 的线程池
                out[cid] = self._run_node_filled(cid, out)
        return out, 0.0, 0.0

    def _run_node_filled(self, cid, out):
        sig = self.circuit._run_one(cid)          # 现有闸 + backend.run
        b = self.budget
        while sig.meta.get("gate") == "fail_linear" and b > 0:
            self._auto_fill(sig.meta["missing"], cid)   # 写 state._fetched
            sig = self._rerun_with_filled(cid, out)      # 注入补给信号重跑
            b -= 1
        return sig

    def _auto_fill(self, missing, cid):
        comp = self.circuit.components[cid]
        fillers = comp.get("fillers") or {}
        for m in missing:
            if m in self.state["_fetched"]:
                continue
            spec = fillers.get(m) or {"skill": "web_search", "args": {"query": m}}
            self.state["_fetched"][m] = self.dispatch(cid, spec)

    def dispatch(self, cid, spec) -> str:
        """执行器主动派发：调 execute_skill 或真实工具。返回文本结果。"""
        name = spec.get("skill")
        args = spec.get("args", {})
        if self.skills_enabled and name:
            self.state["_skills_used"].append(name)
            return execute_skill(name, json.dumps(args))   # agent_skills
        # 无技能时的兜底检索（确定性 dry 文本，不开路）
        return f"[no-skill-fill:{name}]"
```

> 复用点：`circuit.layers()` / `circuit._run_one()` / `agent_skills.execute_skill()`
> 全部原样复用，**CircuitExecutor 只是把它们用"补数据闭环"串起来**。

---

## 5. 与现有代码的关系

| 文件 | 改动 |
|---|---|
| `runtime.py` | 新增 `CircuitExecutor` 类（包装 `Circuit`）；`Circuit`/`Signal` 不动 |
| `compiler/agent_skills.py` | 不改（仅被调用）；如需"真实 WebFetch 工具"可加一个 `web_fetch` 技能 |
| `compiler/llm_agents.py` | 不改；`LLMAgentBackend` 的工具循环与执行器派发**并存**（LLM 在场时模型也能自调） |
| `verify_drift_smoke.py` | 扩展断言③：去掉 input_map 后，新执行器应**自动补数据**使节点 ok（验证闭环） |
| `demo.py` / `run.py` | 加 `--executor` 开关演示 CircuitExecutor |

---

## 6. 验收（对应 B/C/D）

- **B（PoC）**：新建 `executor_poc.py`，构造"reason 节点缺 `china_gdp_2024`"拓扑，
  用真实 `execute_skill("web_search", ...)`（或 dry 文本兜底）+ `CircuitExecutor` 跑通：
  节点先 `gate:fail_linear` → 执行器自动检索补数 → 重跑 ok。控制台打印 `_trace` 证明闭环。
- **C（重构）**：`runtime.py` 落地 `CircuitExecutor`，`demo.py` 加 `--executor` 跑通，
  旧 `Circuit.execute()` 行为不变（回归测试）。
- **D（动态技能）**：节点声明 `skills`/`fillers`，用 `SimBackend`（无 LLM）也能触发
  `web_search`/`run_code`，证明"技能不再封在图纸上"。
- **CI 冒烟**：`verify_drift_smoke.py` 加 `--executor` 分支，断言自动补数据闭环。

---

## 7. 风险与边界

- **安全**：`run_code` 执行模型生成 Python 仍仅靠临时目录+10s 超时，无完整沙箱；
  CircuitExecutor 不扩大该面，但在设计文档显式标注，并在 `fillers` 里禁止默认启用
  `run_code`（只默认 `web_search`/`read_page` 这类只读检索）。
- **成本**：自动补数据会多发检索/调用，默认 `data_fill_budget=2` 封顶；真 LLM 调用仍
  受 `resolve_api_key` 控制（无 key 走 SimBackend/ dry 文本）。
- **循环风险**：补数据闭环有 `budget` 上限，不会无限重跑；`gate:fail_linear` 仍诚实上抛。
- **多任务（3.5）**：stretch，先做接口与 PoC 钩子，不全量实现以免范围爆炸。

---

## 8. 观察窗（B）：可视化执行追踪产物

**痛点**：执行过程对使用者是黑箱——只能看到技能被调用，看不到「技能在干什么」；
且用户环境不显示 Python stdout（控制台日志不可见）。故把"观察"从控制台改为
**可 `present_files` 打开的 HTML 视觉文件**。

**双通道埋点（`CircuitExecutor.__init__`）**：
- `verbose: bool=False` —— 同时向控制台打印事件行（CI 冗余，用户环境通常不可见）。
- `on_event: Callable=None` —— 结构化事件回调 `(dict)->None`，供 SVG/UI 订阅，零重复埋点。
- 内部 `self._events: list` —— **默认始终填充**（不受 verbose/on_event 影响），是渲染数据源。
- `events` / `scope` 参数 —— 子电路执行器共享父事件列表 + 打 `evolve` 作用域前缀，
  使时间线连续、子电路事件可识别（紫⚠）。

**事件流覆盖**：`start` → `layer_start`/`layer_done` → `node_start` →
（`gate_fail` → `skill_call`/`skill_return` → `retry`）→ `node_done` →
`evolve_detect`/`evolve_spawn`（3.5）→ 子电路递归 → `done`。节点最终状态写 `self._results`，
补数节点记 `self._filled_nodes`，进化来源节点反查记 `self._evolved_from_node`。

**渲染器 `compiler/trace_renderer.py`（仓库内唯一源码）**：
`compiler/executor_trace.py` 现仅作向后兼容重导出（`from .trace_renderer import render_executor_trace`），调用方零改动。
`render_executor_trace(executor, title, out_path)` 把事件流 + 拓扑渲染成自包含 HTML：
- 拓扑图：节点按最终状态四色着色（绿✓完成 / 红✗失败 / 橙虚框=补数闭环 / 紫⚠=触发 3.5 进化），
  复用 circuit-planner 的复盘配色约定。
- 时间线面板：每个事件一行（相对时间戳 + 类型色点 + 详情）。
- 走查动画：播放/暂停 + 倍速(0.5/1/2/4×) + 进度条，按事件发生顺序高亮对应节点（蓝激活 / 紫脉冲）。

**接入点**：`compiler/demo.py --executor` 末尾 `run_executor_showcase()` 跑完两个演示拓扑，
自动生成 `examples/executor_trace.html`（3.5 多任务进化）与 `examples/executor_trace_fill.html`
（自动补数闭环）并提示用 `present_files` 打开。CI 仍可由 `circuit_executor_selftest()` /
`circuit_executor_evolve_selftest()` 的 `_events`/`_filled_nodes`/`_evolved_from_node` 断言做冗余校验。

