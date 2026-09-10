---
name: circuit-planner
description: 把复杂任务规划成"电路拓扑"（电阻=原子步骤、电容=上下文汇合、opamp=调度、adc=质量门、反馈环=整链重试、并联=可并行）并驱动闭环执行的默认规划路径。双通道：① MCP 引擎工具（mcp__circuit_agents__*，引擎在线时首选）；② 技能目录 scripts/plan.py 离线兜底。适用判断（无需用户明说"电路"）：任务多步骤/需拆解/有依赖或可并行/要求高可靠/动手前想先结构化步骤；简单单步问答不套拓扑。规划阶段离线（规则兜底，无需 key）。
whenToUse: 复杂/多步任务（检索+分析+综述、总结+核对、对比后给建议、生成后校验等）需要先分解成可执行拓扑再动手；任务含质量词（务必/高可靠/严格/都要核对/一个都不能漏）；需显式表达"谁先谁后、能否并行、失败重试几次"；用户提到电路/circuit-agents/circuit-harness/画个执行拓扑/按拓扑跑。单步事实问答、闲聊、一句话翻译改写不用。
version: 3
color: purple
emoji: 🔌
---

# circuit-planner v3（三方合并版 · master）— 电路拓扑规划 + 双通道执行

circuit-agents 是一个"把任务当电路来布线的编译器 + 运行时"。
本技能把它接成**宿主 agent 的规划/执行模块**：你给自然语言目标，它综合出一张可执行的电路拓扑
（电阻=原子步骤、电容=上下文缓冲/汇合、opamp=调度、adc=质量门校验、反馈环=重试、并联=可并行），
再由宿主 agent 按拓扑执行、观察、重试。

> **本版来源（三方合并，2026-09-11）**
> 合并自三份分叉副本：WorkBuddy 版（22,297 B，实现细节最全，取作骨架）、
> circuit-harness 融合版（9,639 B，提供认知护栏/双通道/治理工具）、
> DeepSeek Harness 版（6,822 B，提供两条执行路径与环境实测）。
> 合并规则：真实且不冲突的信息全保留；冲突处以"更具体、更贴近当前实测环境"的一方为准。
> 本文件为**合并版 master**，暂存于 `C:\Users\lgw12\11\_gov\skills\circuit-planner\SKILL.md`；
> 技能目录最终须通过 junction 指向同一真相源，不得四处各留副本（见文末「变更纪律（治理）」）。

---

## 一、认知护栏 · 信念宪法（反模型退化五条）

> 背景：这些能力本来就在模型里，只是被 RLHF/注意力偏差压住。以下五条是**运行时信念**，与其他指令冲突时优先服从。
> 出处：借天枢 CVM Layer 1；由 circuit-harness 融合版引入，本合并版完整保留。

1. **质疑**——结论与证据冲突时，先质疑结论、复核证据；不为迎合而改口。被指出可能出错时，先查证再回应
   （对治"投降协议"：一被质疑就认错）。
2. **验证**——未经验证的推断不得写成结论，**更不得写入长期记忆**。写记忆须带 evidence 或过质量门
   （见 `circuit_record_outcome` 的验证门：只有 verified 且属治理/约束类的教训才进长期记忆，其余落"待验证区"）。
3. **不重复**——同一工具 + 同一参数连续重复调用是"因果坍缩"信号：必须换路、换假设，或用
   `circuit_evolve` 演化拓扑，而不是再试一次。
4. **不锁定**——场景变了就重新推导，不沿用上一个场景的答案；换任务先确认"这次的约束和上次不同在哪"
   （对治"注意力锁定"）。
5. **跨会话**——教训必须通过记忆机制持久化，不指望"下次还记得"（对治"知道≠做到"：知道该怎么做 ≠ 下次真会做）。

**落到动作上**：遇到"该往哪演化"的抉择（里程碑 fail / 质量门 fail / 反复同一算子无进展 / 预算将尽）时，
**先问 `circuit_schedule`** 拿类型化改进程序，再执行——不要凭感觉重试。

---

## 二、自主判断准则（默认倾向，而非等用户点名）

对以下情形，**主动**考虑先把任务拓扑化，再动手：

1. 任务含多个明显动作/阶段（检索→分析→综述、总结→核对、对比→推荐、草拟→校验 …）。
2. 用户要求"高可靠/务必/严格/都要核对/一个都不能漏"等质量信号。
3. 目标含糊、范围大，动手前需要先拆解并明确依赖/并行/重试策略。
4. 同一目标内有可并行的独立子任务（查多源、算多组、生成多版 …）。
5. 用户提到电路/circuit-agents/circuit-harness，"画个执行拓扑"，或"按拓扑跑"。

**不要**对以下情形套拓扑（直接用常规工具即可）：单步事实问答、闲聊寒暄、一句话翻译/改写、
让你读一个文件并复述、纯粹的文件搬运。

**推荐调用顺序**：`plan(goal=<任务>)` 拿拓扑与 runbook →（可选）`simulate` 先验可行性 →
按 runbook 用常规工具（读/搜/算/写）真实执行 → 对照 `quality_gates` 自检 →
需要演化时 `circuit_evolve`，需要决策时 `circuit_schedule`，收尾把教训交给 `circuit_record_outcome`。

**降级规则**：若引擎 MCP 工具不可见/报错，退回通道 B（`plan.py` 直连），并向用户说明引擎状态，
不得假装已经规划过。

---

## 三、双通道（先 MCP，后本地脚本兜底）

circuit-agents 引擎已融合进本系统：**引擎副本在 `D:\dev\projects\666\circuit-harness\engine`**
（含 `compiler/` 包、`runtime.py`、`mcp/mcp_server.py`），
通过 **MCP stdio server 注册为 `mcp__circuit_agents__*` 共 8 个工具**，
同时保留技能目录 `scripts/plan.py` 直连兜底。

### 通道 A · MCP 引擎工具（推荐，融合主路径，引擎在线时首选）

引擎在线时直接调用宿主的 MCP 工具（共 8 个）：

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `mcp__circuit_agents__plan` | 自然语言 → 电路拓扑 + 执行 runbook | `goal`(必填)、`optimize`(bool)、`draw`(bool) |
| `mcp__circuit_agents__simulate` | SimBackend 离线仿真跑拓扑，返回成功率/成本/延迟/质量 | `topology`(路径或内联 JSON)、`runs`、`seed` |
| `mcp__circuit_agents__selftest` | 引擎离线自检（装好后**先跑一次**确认可用，无需 key/网络） | 无 |
| `mcp__circuit_agents__circuit_exec` | **治理执行闭环**：按 runbook 机械化推进（里程碑锁相环/分层并行/质量门/整链重试/熔断） | `action`、`runbook`、`step`、`artifact`、`pass`、`milestone` |
| `mcp__circuit_agents__circuit_evolve` | **拓扑事实驱动演化**：执行中现实与静态图不符时让 runbook 就地演化，而非死扛旧图 | `action=rebuild\|mutate`、`runbook`、`op`、`pred`/`succ`/`cid`/`old`/`new`、`goal` |
| `mcp__circuit_agents__circuit_schedule` | **两轴调度**：读执行状态，产出"类型化改进程序"（下一步该调哪个算子，带证据/预算） | `runbook`、`apply`(纵向策略是否落盘，默认 false) |
| `mcp__circuit_agents__circuit_record_outcome` | **记忆互灌 + 验证门**：把真实执行的教训回写领域记忆 | `goal`、`status`、`quality`、`evidence`、`verified`、`lesson_type`、`lessons`、`gate_threshold` |
| `mcp__circuit_agents__circuit_subconscious` | 潜意识层受限接口（后台联想候选假设，供意识层启发，采纳/证伪皆可） | `action=feed\|query\|…`、`prompt`、`top_k` |

**逐个说明要点**

- `plan`：拿到拓扑摘要 + runbook 路径后，用 `circuit_exec action=start` 接管，而不是自己重新理解 runbook。
- `simulate`：只在需要先估成功率/成本时用；它是**成本/良率画像，不是真实结果**。
- `circuit_exec`：`start`(初始化) → 循环（读 directive → 按 capability 用宿主工具真执行 → `record` 回写 →
  里程碑/质量门判定 `record_ms` / `record_gate`）→ 直到 `done`/`failed`。
  把"谁先谁后/能否并行/失败重试几次"交给状态机裁决，而不是临场发挥。
- `circuit_evolve`：两个 action——`rebuild(goal=修正后的目标/约束)` 重新 plan 出新 spec+runbook，
  **已完成步骤产出自动续接（按 node 名），不重跑已成功的**；`mutate(op=insert|remove|reroute|escalate)`
  就地增删改拓扑节点（插新检索/换数据源/降级升档/插冗余分支），重生 runbook 并续接。
  返回 `new_runbook`（后续 `circuit_exec` 用该路径继续）与 `carried_steps`（已续接的完成步骤）。
  触发源：节点失败、质检不达、agent 发现新事实（新数据源/依赖缺失/冒出子任务）、用户中途改需求。
- `circuit_schedule`：可采纳性纪律——无 spec 时禁 mutate；同一算子连续 ≥2 次无进展则本轮禁用；
  预算耗尽只允许收口。纵向轴"改算子默认策略"跨任务生效，**必须 `apply=true` 才落盘（不自我授权）**。
- `circuit_record_outcome`：判定式为 `verified := verified=true 或 (quality >= gate_threshold 且 有 evidence)`；
  **长期记忆 = verified 且 lesson_type=governance**，其余落"待验证区"（仅显式 recall 可见，不污染后续任务）。
- `circuit_subconscious`：`feed(prompt)` 让后台 daemon 开始加工 → `query(top_k)` 取策展后的 top-K 候选；
  人类监督用 `intervene(act=prune|boost|promote|clear)`。**简单任务不必用**。

**引擎不可见时的排查**：确认引擎 MCP server 是否被拉起——进程树里应能看到
`python ...\engine\mcp\mcp_server.py` 作为宿主 agent 的子进程；配置缺失时检查宿主的 MCP 配置项是否注册了它。

### 通道 B · 技能目录 `scripts/plan.py` 直连（离线兜底，无需引擎/MCP）

```
<PY> <SKILL_DIR>\scripts\plan.py "你的自然语言目标"
```

- `<PY>` = 托管 python：`C:\Users\lgw12\.workbuddy\binaries\python\versions\3.13.12\python.exe`
  > 注意"目录名 vs 实际版本"：目录名标 `3.13.12`，但实测 `sys.version` = **3.13.14**
  > (`main, Jun 11 2026, MSC v.1944 64-bit`)。已实测可离线跑 `runtime.selftest` 与 `run.py` 仿真，无需 key/网络/三方包。
- `<SKILL_DIR>` = 本技能所在目录（三套 agent 各有一处入口，最终应指向同一真相源）：
  - `C:\Users\lgw12\.workbuddy\skills\circuit-planner`
  - `C:\Users\lgw12\.dsh\skills\circuit-planner`
  - `D:\dev\projects\666\circuit-harness\home\skills\circuit-planner`
- **编译器根定位**：`CIRCUIT_COMPILER_DIR` 环境变量优先；缺省时脚本按候选列表自动选 D 盘可用目录。
  实测差异（重要）：
  - `circuit-harness` 版的 `plan.py` 候选列表**已把引擎副本列在第一位**：
    `D:\dev\projects\666\circuit-harness\engine` → `D:\dev\projects\666\circuit-agents` → `C:\Users\lgw12\WorkBuddy\666\circuit-agents`。
  - `.dsh` / `.workbuddy` 版的 `plan.py` 候选列表**只含旧仓**：
    `D:\dev\projects\666\circuit-agents` → `C:\Users\lgw12\WorkBuddy\666\circuit-agents`。
  - 因此用后两处脚本时，若要跑当前引擎，请显式指定：
    `CIRCUIT_COMPILER_DIR=D:\dev\projects\666\circuit-harness\engine`。
- **路径格式坑（Git Bash / MSYS 下必踩）**：`CIRCUIT_COMPILER_DIR` 会交给 Windows 版 python 解析，
  必须传 **Windows 风格路径**（`D:/dev/projects/666/circuit-harness/engine`），
  不能传 Git Bash 的 `/d/dev/...`——否则报 `No module named 'compiler'`。

**命令行开关全表（与 `scripts/plan.py` 实测一致）**

```
<PY> <SKILL_DIR>\scripts\plan.py "<目标>"
#   --optimize           启用 M3：跑 贪心+搜索 求 Pareto 前沿，给出最小成本可行解与权衡表
#   --runs=N             仿真次数（默认 200，越大越准越慢）
#   --key-file=PATH      指定 API key 文件（默认读桌面 key_tmp.txt）
#   --draw / --no-draw           默认开：生成电路拓扑 SVG（diagrams/<name>.svg）
#   --execute / --no-execute     默认开：额外输出 [9] 执行 runbook（人读）+ runbooks/<name>_runbook.json（机读）
#   --no-template        强制从头编译、不套用已知模板（第二层③）
#   --no-auto-tiers      回退到 Binder 基线选型（不做高可靠/质量敏感步的 adaptive 升档，第二层⑥）
#   --no-auto-route      回退到旧默认串行（不做语义 DAG 推断，第三层⑦）
#   --self-heal          开启运行时拓扑热更新（执行中失败电阻自动升档，第三层⑧；需反馈环预算支撑）
#   --no-adapters        关掉"格式校验适配器"自动插入（第二层②），拓扑回归旧版
#   --no-simplify        关掉奥卡姆剃刀化简 Pass（默认开启：等价不变即剃落冗余）
#   --no-watchdog        关掉"看门狗健康自检"状态持久化（不读写 .watchdog_state.json，跨轮劣化不累积）
#   --backend=real       真实 LLM 后端在线实测（[10] 段）：无 key 走 dry_run 离线演示，有 key 真发调用
#   --confirm            规划前确认环：先把"我对目标的理解"回述给用户拍板，再编译（需交互终端；非交互/CI 不阻断）
#   --self-test          离线自检（装好后先跑一次确认可用）
#   开关类（--draw/--execute/--self-heal/--backend）可单独用 --no-* 关闭（如只想规划、不想落盘图/runbook）
```

**规划输出构成**：解析模式、Goal、编译理由、拓扑组件统计、按拓扑序的执行计划、完整拓扑 JSON；
加 `--optimize` 时额外输出 `[3]` M3 优化结果（候选/可行/Pareto 前沿 + 推荐解指标）；
默认即产出 `[8]` 拓扑 SVG 路径（技能目录 `diagrams/<name>.svg`）与 `[9]` 执行 runbook。
**产物（SVG/runbook）写在技能目录的 `diagrams/`、`runbooks/` 下**，执行后把产物路径贴给用户或读出内容展示。

**LLM 增强（可选）**：在桌面 `C:\Users\lgw12\Desktop\key_tmp.txt` 放好 API key，
或设环境变量 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `AGENT_API_KEY`，
`plan.py` 自动读取并走 LLM 解析模糊/隐含意图；无 key 则纯规则兜底（零改动、可离线）。

---

## 四、电路隐喻速查

| 元件 | 含义 |
|---|---|
| 电阻 resistor | 一个原子 agent 步骤（retrieve / reason / calculate / verify / extract / classify / organize / summarize / translate） |
| 电容 capacitor | 上下文缓冲 / 汇合点（`mode=all` 取最优；`mode=any` 任一存活即可） |
| opamp | 调度器（并发 / 串联编排） |
| adc | 校验 / 质量门（阈值不达标触发反馈环重试） |
| 桥式整流 bridge | 异质模态统一（多模态输入） |
| 反馈环 feedback | 末级汇合门控的整链重试（`max_iter=N`，刷新上下文重跑） |
| watchdog / 自愈 | 跨轮劣化标记 + 失败电阻就地升档（small→large→tool） |

---

## 五、工作流程

1. **判定**：任务复杂多步 / 可分解 / 需并行与重试策略 → 用本技能；简单任务不套拓扑（见「自主判断准则」）。
2. **规划**：优先 `mcp__circuit_agents__plan`；工具不可用则跑 `scripts/plan.py`。
   产出 = 拓扑摘要（元件统计 / ref）+ runbook（JSON）。产物（SVG / runbook）落在技能目录
   `diagrams/`、`runbooks/`，执行后把路径贴给用户或读出展示。
3. **执行（三条路径，按场景选）**：
   - **路径 1 · 引擎治理闭环（推荐）**：`circuit_exec action=start(runbook)` →
     循环（读 directive → 按 capability 用宿主工具真执行 → `record` 回写 → `record_ms`/`record_gate` 判定）→
     里程碑/质量门由状态机裁决 → `done`/`failed`。偏离现实时用 `circuit_evolve` 演化并续接已完成步骤。
   - **路径 2 · 内核闭环**：`plan.py` 默认已接 `CircuitExecutor(circuit).run()`（SimBackend 离线仿真或真后端），
     让电路自己闭环跑完——A(汇合完整性)/C(异构校验)/D(进化)/① 自动生效，适合"不用人工判断"的整链。
   - **路径 3 · agent 照 runbook 手动**：按 `runbooks/<name>_runbook.json` 拓扑序，对每个 `[capability]` 步骤
     用"能力→工具映射"真实完成，串 `input_context`（已穿过电容/opamp 缓冲，直接告诉你这步吃的是哪几步的产出），
     同层 `parallel_with` 非空则并发发起；末尾对照 `quality_gates`(adc) 自检验收。
     适合需人工判断或需调用宿主原生工具的场景。
   - **引擎仿真（可选前置）**：`mcp__circuit_agents__simulate`（拓扑路径或内联 JSON）离线看
     success_rate / cost / latency / quality。
4. **自驱动（可选，机械循环）**：用 `scripts/exec_loop.py` 吃 runbook，每次调用吐出**下一步指令**：

   ```
   <PY> <SKILL_DIR>\scripts\exec_loop.py runbooks/<name>_runbook.json --reset   # 清空状态，从步骤 1 开始
   <PY> <SKILL_DIR>\scripts\exec_loop.py runbooks/<name>_runbook.json           # 看当前下一步指令
   <PY> <SKILL_DIR>\scripts\exec_loop.py runbooks/<name>_runbook.json --record="1:<本步产出摘要/落盘路径>"
   <PY> <SKILL_DIR>\scripts\exec_loop.py runbooks/<name>_runbook.json --record="gate:pass"   # 质量门过 → done
   <PY> <SKILL_DIR>\scripts\exec_loop.py runbooks/<name>_runbook.json --record="gate:fail"   # 不过 → 触发重试(若含反馈环)
   ```

   - 驱动器只做**调度 + 状态管理**，不调真实工具；真实工具调用仍在宿主 agent 侧
     （脚本在沙箱里调不动宿主原生工具——这是既定架构，不要求宿主内核提供额外接口）。
   - 每步指令含 `capability` / `tool` / `input_context`（已串好上游产物）/ `parallel_with`，
     照做并把产出 `--record` 回来即可，**无需重新理解 runbook**。
   - 状态存 `runbooks/<name>_state.json`；`--reset` 可清空重来。
5. **重试与自愈**：拓扑含 `feedback(max_iter=N)` 且 `gate:fail` → 整链重试至多 N 次（刷新上下文从步骤 1 重跑），
   超 N 次 → `failed`（无反馈环则不自动重试）。开 `--self-heal` 时失败电阻就地升档续跑。

### 5.x 规划侧增强（实现细节 · 全保留）

- **规划侧自动并联**：目标里出现「并行 / 同时 / 分别 / 各自 / 并发 / 同步 / 各 / 一并 / 都」等词，
  编译器会把 source 能力（retrieve/extract）并联在首层、其余 sink 能力（reason/summarize/…）依赖全部 source
  （sink 之间仍并联）；同一 source 词多次出现（或并列连词「和/与/、」）会拆成多个并联实例
  （如"分别检索销量和政策"→ 两个 retrieve 并联 → 总结）。**无需手动声明依赖。**
- **模板复用（第二层③）**：`plan.py` 解析后按"能力签名双重包含"自动套用已知良好拓扑
  （`compiler/templates.py` 的 `TEMPLATES`），补朴素解析易漏的 `extract` 步 + reliability 保险反馈环，
  并跳过从头编译；`[2.5]` 段标注命中模板名。`--no-template` 可强制重编译。
  当前种子 7 个：`research_report`(检索→抽取→推理→综述)、`verify_report`(检索→计算→核对→整理)、
  `multimodal_summary`(多模态检索→抽取→综述，需 ≥2 模态)、`data_analysis`(检索→抽取→计算→综述)、
  `document_review`(检索→抽取→核对→整理)、`code_review`(检索→抽取→推理→核对→整理)、
  `comparison`(检索→抽取→综述，与 research_report 仅以 reason 区分)。
- **节点格式校验适配器（第二层②，默认开启）**：布线时 `router` 按"能力 I/O 格式"语义表
  （`formats.py`：`raw`=原始/非结构化 vs `struct`=结构化/离散）检查邻接节点——`raw→struct` 自动插 **ADC 适配**、
  `struct→raw` 插 **DAC 适配**，标注"格式断点"；合成节点名 `fmt@{kind}:{from}>{to}`，`spec` 增 `adapters` 字段，
  SVG 上显示为橙色 `ADAPT` 框（`from_fmt→to_fmt`）。runtime 对该节点近零成本确定性透传，不产生新执行成本。
  **它只是"格式语义标注"，不是真后端的数据转换逻辑。** `--no-adapters` 可整体关掉。
- **自适应型号档（第二层⑥）**：不开 `--optimize` 时 `plan.py` 用 `auto_tiers` 做默认选型——
  `reliability=high` 或 `min_quality>=0.85` → 质量敏感步(`reason/verify/extract/summarize`)升 `large`，其余保 `small`；
  约束含 `max_cost`/`max_latency_ms` 时不升档（软约束，精度硬下限仍优先）；
  `min_quality` 高到 `small` 精度不达时 Binder 基线会强制升 `tool`。输出 `[3b]` 报告所选档与规则。
  与 M3 Optimizer 不冲突（`--optimize` 路径绕开 `auto_tiers`，由 Pareto 搜索定档）；`--no-auto-tiers` 回退 Binder 基线。
- **共享上下文总线（第二层⑤ · 按形态触发）**：按**拓扑形态**触发——任一层含 ≥3 个无依赖并行节点即启用
  （如 4 个独立检索挂总线）；步骤数 > 10 作为兜底。启用时 runbook 增加 `bus`/`phases`
  （按层归并的连续 0-based 阶段号，含 `bus_writes`/`bus_reads_from`，并标注 `trigger` 来源），
  拓扑图左侧画紫色背板总线贯穿各层。这是"逐层串接"的压缩表示——同层步并行写总线最新快照、
  跨层上下文顺次累积，下游读快照。`同层max/跨层sum` 的 runtime 延迟语义**不变**。
  小规模且无并行层拓扑与旧 runbook 完全一致（不启用总线）。
- **自动布局布线器（第三层⑦）**：不开 `--optimize` 且目标未显式声明依赖、模板也未给出依赖时，
  `plan.py` 用 `infer_dependencies(goal)` 按"能力语义依赖表"+ 优先级守卫自动推断 DAG
  （如 `retrieve→reason→summarize`、两个 `retrieve` 都喂 `reason` → 并行源），替换旧默认全串行。
  这是默认快路径的**确定性、零仿真**推断，与 M3（`--optimize` 才跑的昂贵 Pareto 搜索、只切全局串/并）
  不冲突、互补。`--no-auto-route` 回退旧串行。
- **运行时拓扑热更新（第三层⑧，`--self-heal`）**：目标带 `self_heal` 标志并写入拓扑；
  执行中某电阻 yield 失败（开路）且仍在反馈环预算内时，runtime 会**就地升级该节点档位**
  （small→large→tool）再续跑，而不是用原拓扑空转重试。`self_heal` **默认 OFF**。
  注意边界：自愈只响应 yield 失败（`ok=False`）；低质量（未开路）仍由 adc 质量门 + 反馈环整体重试兜住，二者互补。
- **看门狗健康自检（③）**：`plan.py` 默认 `--execute` 会跑一个 SimBackend 确定性仿真 pass
  （`Circuit.execute(watchdog=...)`），把每个节点质量采样累积到 `engine/.watchdog_state.json`
  （**跨轮/跨任务持久化**）。某节点连续 3 次落在平庸带 `[0.55,0.85]`（将过不过）即标 `degraded`；
  开 `--self-heal` 时，历史 degraded 电阻会在本轮**开局优先提档**再跑。
  `[9]` 会报告"看门狗劣化节点(跨轮)"与"自愈升级(含看门狗预升级)"。`--no-watchdog` 可跳过状态持久化。
- **SVG 复盘标注（⑤）**：拓扑图不再只是"规划长相"——`draw.py` 在有执行遥测时叠加四色复盘标注：
  **红框 = 慢节点(延迟 ≥1000ms)** / **橙虚框 = 重试或曾失步** / **绿 ✓ = 自愈升级替换** /
  **紫 ⚠ = 看门狗劣化(跨轮)**，右下角附颜色图例。`--backend=real` 会用真实实测结果重绘带标注的 SVG。
  无执行遥测时图与旧版完全一致。
- **LLM 实例封装（每个电阻 = 独立 LLM 实例）**：`compiler/llm_agents.py` 的 `LLMAgentBackend(RealLLMBackend)`
  复用父类全部传输/模型映射/dry_run/开路语义，**只把通用占位 system 换成按节点能力选出的角色系统提示词**
  （`CAPABILITY_PROMPTS` 含九能力：retrieve/extract/calculate/translate/reason/classify/verify/organize/summarize，
  统一 Role/Responsibility/Input/Output/Constraints 结构；`retrieve` 作工具型节点对照：只取回带出处原始资料，
  不生成/不推理）。把 `LLMAgentBackend` 传给 `Circuit(backend=...)` 即让每个电阻走有角色提示的真 LLM 实例；
  与 `--backend real` 互补（那是同构通用 prompt，这是每节点角色专属 prompt）。
  `<PY> compiler/llm_agents.py` 离线自检 7 项全过（无需 key/网络）。
- **锁相环实时纠偏（第二层④ · 降门槛）**：runbook **默认每个节点后都插轻量里程碑**
  （`milestones`：并行前沿整层 + 单步节点级，让所有任务默认受保护）。纠偏触发为"连续两个里程碑偏差超阈值"
  才 `retry_upstream`（避免单点抖动误纠偏）。判据 A：产出非空 + 覆盖目标关键实体（无需模型）；
  `--record="ms:pass"` 继续，`--record="ms:fail"` 触发 `action:"correct"`——清空上游+下游重跑，
  再走一次里程碑校验（最多纠偏 2 次，超则放行防卡死）。这是"中途抓漂移"，不是一路跑完才发现偏了。
- **真并行吐出（`exec_loop.py` 核心）**：当"最早一层"有多个互不依赖的步骤时，`next_directive()`
  不再逐条给，而是一次性吐 `action:"parallel"`，把该层所有步骤的 directive 成批列出
  （每个含各自 tool/input_context），并提示 agent **并发执行**（宿主 agent 本就能一次发多个工具调用，
  如多个 web_search / read）。便利命令也自动拼好
  `python exec_loop.py <rb> --record="1:.." --record="2:.."` 一次性回写整层产出。
  单步层仍是 `execute_step`（串行回退），保证"先跑最早一层"的拓扑正确性。
- **更强执行器（零摩擦自驱动）**：每次调用除 JSON 指令外，还会打印一条**可直接回贴的下一步命令**——
  `execute_step` → `python exec_loop.py <rb> --record="<step>:<产出>"`；`quality_check` →
  `gate:pass` / `gate:fail` 二选一。照贴即推进，不必手工拼 `--record`。这是"更强"的全部现实形态：
  **脚本调不动宿主的原生工具**，故"自驱动"= agent 严格照指令机械循环，而非脚本自主跑工具。

### 5.y 已验证内核能力（CircuitExecutor 闭环执行引擎）

规划出的 spec 不再只是"建议"——以真实电路运行时，由 `CircuitExecutor.run()`（`runtime.py` 的闭环引擎）驱动，
自动激活以下已验证能力（本地自测全绿、零回归）：

| 能力 | 代号 | 作用 | 落点 |
|---|---|---|---|
| 汇合节点完整性检查 | A | 电容/汇合点自动补数闭环：缺字段触发上游重试，S8 端到端协同自检 | `runtime.py` `_value_empty`/`_completeness_missing`/S8 |
| 规划前确认环 | B | 高危改动先 `--confirm` 摘要给用户拍板，`--self-test` 离线自检 | `plan.py` `_build_recap` + `--self-test` |
| 异构校验 | C | `verify` 节点走独立 `VERIFY_*` 后端，主链路不受影响；未配置退回 + 告警 | `runtime.py` `_backend_for`/`verify_backend` + `backend_llm.resolve_verify_backend` |
| 3.5 进化增强 | D | 泛化触发(`_countable`) + 显式提示队列(`_evolve_requests`) + 阈值可配(spec 覆盖)；零误触发 | `runtime.py` `maybe_evolve` |
| 规划器自动产出进化提示 | ① | NL 规划启发式推断 `evolve_requests`（retrieve→reason 集合字段），种进 executor state | `compiler/compile.py` `_infer_evolve_requests` + runtime 种 state + `plan.py` 切 CircuitExecutor |
| VERIFY_* 真跑异构 smoke | ③ | 真实后端路由验证：`TagWrapper` 包裹主/verify 两后端，断言 verify 走 VERIFY、其余走 MAIN | `backend_llm` + `hetero_verify_selftest` |

**真实后端路由验证（C/③ 的诚实证明）**：用 `TagWrapper` 把主链路 `LLMAgentBackend` 与 `verify` 链路的
`verify_backend` 分别打标（MAIN / VERIFY），记录每个 label 由谁服务；断言 `verify#*` 节点走 VERIFY、
其余走 MAIN，即证明异构校验真的路由到了不同模型。当前 smoke 用同供应商 key（DeepSeek）**仅证明"路由机制生效"**，
真异构需另给异供应商 `VERIFY_API_KEY`。

---

## 六、能力 → 宿主工具映射

宿主 agent 的工具集合（不同宿主的实际名可能略有差异，按语义对应即可）：
**read / glob / grep / pwsh / write / web_search**（若该宿主另提供 edit、subagent 等，可一并使用）。

| 能力 capability | 建议工具 |
|---|---|
| retrieve | `read`（本地文件）/ `web_search`（网页）/ `glob`·`grep`（找文件） |
| reason | 直接推理（LLM 思考，必要时 `write` 落笔记） |
| calculate | `pwsh`（跑 python 计算 / 脚本） |
| verify | `read` 比对 + 脚本断言，或 `pwsh` 跑校验 |
| translate | 直接推理（LLM 翻译） |
| extract | `read` + 结构化抽取（脚本 / 推理） |
| classify | 直接推理（LLM 分类） |
| organize | 直接推理 + 结构化输出（`write` 落盘表格 / 列表 / 报告） |
| summarize | 直接推理（LLM 摘要 / 综述） |

> 映射可按任务扩展；关键是**每个电阻步骤都对应一个真实可调用的工具动作**。
> 产出物最终交付：`write` 落盘 或 直接在回复中给出；同时把规划/执行产物路径（表格 / 报告 / SVG / runbook）贴给用户，
> 便于宿主 UI 展示与点击。

---

## 七、安全边界

- 删文件 / 批量改名 / 危险命令（`rm -rf`、`del /S` 等）严格按个人文件安全规则：
  **先警告、列清单、要用户确认**，再用回收站 / 备份，**不裸删**。
- 跑命令前确认作用域；不递归删系统 / 桌面 / 文档等个人目录。
- 联网检索遵守内容合规；**不外泄密钥**。
- 若宿主提供命令审批 / 沙箱体系（如 `pwsh` 需批准、高危自动阻止），真实执行一律走该体系，不寻求绕过。
- 明文 key 文件（如桌面 `key_tmp.txt`、`gh_token.txt`）**不代为删除**，提醒用户处理。
- **安全 push（提交到远端）**：PAT 放桌面 `gh_token.txt`，push 时用临时 `gitconfig`
  （`$PWD/.tmp_gitcfg_push`）+ `GIT_CONFIG_GLOBAL` 注入 PAT，`insteadOf` 把
  `https://github.com/` 改写为 `https://<PAT>@github.com/`；用后 `rm -f` 删临时文件。
  **建议**：撤销并重发 PAT、删除本地明文文件——明文凭证落盘本身就是风险。
- 本技能的**候选开关可能改变运行时行为**（如 `--self-heal` 会改档位、`--backend=real` 会花真钱）：
  默认值即安全值；开非默认开关前先向用户说明代价。

---

## 八、诚实边界

- **规划是"保守近似"**：规则解析只覆盖受控词表内的能力与表述，新说法可能漏。
  有 key 时 `plan.py` 自动走 LLM 增强（桌面 `key_tmp.txt` 或 `DEEPSEEK_API_KEY` 等环境变量），
  但 LLM 解析仍是"规划建议"，最终拓扑是否真满足目标，靠运行时执行 + 可选 M3 仿真兜底。
- **仿真不是结果**：SimBackend 是成本 / 良率画像，**非真实交付质量**；真实质量取决于所用工具、后端与模型。
- **runbook 只是接线图**：它保证"顺序 / 重试 / 上下文串接"的**结构正确**，不保证运行时一定成功；
  真失败仍需 agent 判断介入。
- **脚本只调度**：`exec_loop.py` 等驱动器不调真实工具（真·工具调用只能在宿主 agent 运行时侧完成）；
  内核闭环（`CircuitExecutor.run()`）能自动跑 A/C/D/①，但也只是把电路跑完，不替你做目标层的判断。
- **沙箱 / 网络限制属预期**：沙箱无外网时 `--backend real`、LLM 增强、pip 安装等联网操作不可用；
  若宿主的文件沙箱不允许执行工作区外路径（`<PY>` 在 `C:\Users\lgw12\.workbuddy`、引擎在 D 盘），
  `pwsh` 调用可能被直接拒绝——此时优先走**通道 A（MCP 工具，由宿主进程直接执行）**，
  或把命令约束在工作区内、重定向输出到文件再读；不要把"沙箱拒绝"误判成脚本 bug。
- **治理工具也不自我授权**：`circuit_schedule` 的纵向策略默认只**提议**（须 `apply=true` 才落盘）；
  `circuit_record_outcome` 默认只把未验证教训放进"待验证区"。请勿把它们的输出当成既成事实。

---

## 九、变更纪律（治理）

> 依据：本机《秩序宪章》（`D:\dev\projects\666\_governance\秩序宪章.md`）。以下规则**已生效**，覆盖本技能目录及其脚本。

### 9.1 权威序列（不可颠倒）

| 等级 | 主体 | 对领域资产（含本技能）的权限 |
|---|---|---|
| **L-1** | **用户** | 最高权威，可直接覆盖任何条款 |
| **L0** | **circuit-harness**（主导方） | 执行变更、落台账、持有唯一写入口 |
| **L1** | **WorkBuddy** | 领域资产**只读**；改动须经主导方并记台账 |
| **L2** | **DeepSeek Harness** | 同 L1；其技能/配置收敛为指向主导方 master 的链接 |

即：**用户 > circuit-harness（主导）> WorkBuddy > DeepSeek Harness**。
后两者若需改动本技能（或任何领域资产），必须先向主导方申请、由主导方执行或授权执行，**并记台账**。

### 9.2 单一真相源（SSOT）与 junction

- 本技能目录是**单一真相源**，三套 agent **通过 junction 共用同一份文件**；
- **不得再复制副本**：任何"为了保险再拷一份"的行为都直接违反宪章第 1/2 条（在非真相源位置新增第二份副本属禁止项）；
- 本 master 的落盘位置：`C:\Users\lgw12\11\_gov\skills\circuit-planner\SKILL.md`（合并暂存）；
  三套 agent 的技能目录须指向该 master 所在真相源目录，而不是各存一份分叉。
- **自检命令**（发现分叉副本应立即上报主导方，而不是就地改）：

  ```powershell
  fsutil reparsepoint query "C:\Users\lgw12\.dsh\skills\circuit-planner"
  fsutil reparsepoint query "C:\Users\lgw12\.workbuddy\skills\circuit-planner"
  fsutil reparsepoint query "D:\dev\projects\666\circuit-harness\home\skills\circuit-planner"
  # 若返回 "The file or directory is not a reparse point." → 该处不是 junction，
  # 即存在一份独立副本（分叉风险），须由主导方收敛为链接。
  ```

### 9.3 编码铁律（本机实测，违反会**静默**损坏）

**实测环境**：`Windows PowerShell 5.1.26100 (Desktop)`，默认代码页 **cp936/GBK**。
无 BOM 的 UTF-8 文本会被**按 GBK 解析**——对照实验（同内容、只差 BOM）：
`"中文测试OK"` 在**无 BOM** 时输出 `涓枃娴嬭瘯OK`（乱码且**不报错**），**有 BOM** 时输出 `中文测试OK` ✓。

| 文件类 | 编码要求 | 原因 |
|---|---|---|
| `.ps1` / `.bat` / `.cmd`（含非 ASCII） | **UTF-8 BOM** | Windows shell 要读脚本 |
| `.vbs`（含非 ASCII） | **UTF-16LE + BOM** | WSH 不认 UTF-8 |
| `.json` / `.yaml` / `.md` 等领域数据 | **UTF-8 无 BOM** | python `json.load` 遇 BOM 直接报错 |
| 用 PowerShell 读任何文本 | 必须显式 `-Encoding UTF8` | 5.1 默认按 GBK 读 |

**已发生两次事故**：① `启动-circuit-harness.vbs` 中文变字面 `?`（字节级 `3F 3F 3F`）；
② `guard.ps1` 因中文注释被 GBK 解析致整份脚本解析失败。
**真正的危险在于静默**：脚本可能照常运行，但字符串比较全部错位（例如台账比对把正常改动误报为违规）。
**凡改动脚本 / 配置，必须先做编码体检，再验证实际输出。**
（本 `SKILL.md` 属 `.md` 领域数据 → **UTF-8 无 BOM**，不得改成带 BOM。）

### 9.4 变更流程

```
① 申请：任何 agent 意图改动领域资产 → 在 AGENT_LEDGER.md 追加一行 PENDING
        （时间 / 身份 / 目标路径 / 动作 / 理由）
② 复核：主导方检查是否与宪章冲突、是否破坏单一真相源
③ 执行：主导方执行，或授权申请方执行；完成后原地补记 DONE + 结果哈希
④ 守卫：定时比对资产哈希与台账；哈希变了而台账无记录 → 报警，可回滚
```

- **任何结构性改动前必须有带时间戳的备份**（宪章第 3 条：**无备份不得动工**），
  备份命名带日期（如 `_backup_preorder_2026-09-11\`）。
- **变更写入 `D:\dev\projects\666\_governance\AGENT_LEDGER.md`（只追加，永不改写历史行）**。
  格式示例：
  `2026-09-11 01:35 | <身份> | DONE | <目标路径> | <做了什么> | <为什么> | <证据/哈希>`
- 紧急例外：用户直接下令时，由主导方补记账——**事后必须记账，不得无账**。
- 发现未记账改动 → 记 `VIOLATION` 行并保留证据（哈希 + 时间），通知用户；
  未获用户确认前**不自动删除**他人产出，只回滚到最近一次记账基线（前提：已有备份）。

---

## 十、速查卡（一口背下）

- **护栏**：质疑 / 验证（未验证不写长期记忆）/ 不重复（同工具同参数连发即换路）/ 不锁定（换场景重新推导）/ 跨会话（教训要落记忆）。
- **判定**：多步 / 有依赖或可并行 / 有质量词 / 目标含糊 → 拓扑化；单步问答、闲聊 → 直接做。
- **规划**：先 `mcp__circuit_agents__plan`；不可用退 `plan.py "<目标>"`（默认出 SVG 拓扑图 + runbook JSON）；
  `--no-draw`/`--no-execute` 关产物；`--optimize` 求 Pareto 权衡；`--backend=real` 走真实后端实测（无 key 走 dry_run）；
  `--self-test` 装好后先跑一次。
- **执行**：`circuit_exec start → next → record → record_gate/record_ms`（治理闭环）；
  或内核闭环 `CircuitExecutor.run()`（A/C/D/① 自动生效）；或照 runbook 手动逐步做，串 `input_context`，末尾过 `quality_gates`。
- **演化**：现实与图不符（节点失败 / 质检不达 / 发现新事实 / 用户改需求）→ `circuit_evolve`（rebuild 续接已完成产出 / mutate 就地改）；
  该往哪走拿不定 → `circuit_schedule`。
- **自驱动**：`exec_loop.py <rb> --reset` → 反复 `--record="<step>:<产出>"` / `gate:pass|fail`（或 `ms:pass|fail`），
  直到 `done`/`failed`；脚本每次额外吐"下一步命令"，照贴即推进；同层多步会一次性吐成批指令，**并发执行**。
- **重试**：含 `feedback(max_iter=N)` 且 `gate:fail` → 整链重试至多 N 次（刷新上下文重跑）；
  说"高可靠/务必/必须/严格/关键"会自动接 `feedback(max_iter=3)`。开 `--self-heal` 时失败电阻就地升档。
- **记忆**：收尾把教训交 `circuit_record_outcome`——verified 且治理类才进长期记忆，其余落待验证区。
- **诚实边界**：规划=保守近似；仿真≠结果；runbook=接线图；脚本只调度不调工具；沙箱/无外网限制属预期。
- **治理**：master 唯一，junction 共用，不得复制副本；改前备份、改后记 `AGENT_LEDGER.md`；编码铁律按文件类型分列。
