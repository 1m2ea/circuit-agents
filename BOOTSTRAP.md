# BOOTSTRAP.md — circuit-agents 冷启动引导（合并定稿）

> **新会话 / 新 agent 第一分钟必读。** 以下全部信息来自本仓库真实代码与文档（README.md / SPEC.md / COMPILER.md / AGENTS.md / RESULTS.md / MEMORY_ARCHITECTURE.md / runtime.py / compiler/topology_memory.py），无编造文件名或接口签名。

---

## ① 项目是什么

**一句话**：把任务规划成电路拓扑、用多档本地/远程模型闭环执行的 Agent 框架。

**一段话**：circuit-agents 用「电路设计」思想编排 Agent 工作流——**用确定性的拓扑结构去驾驭概率性的模型输出**。一个任务被描述成一张拓扑图（DSL）：电源=目标、电阻=原子 Agent、运放=调度器、电容=上下文汇合、二极管=格式校验、ADC=质量打分器、反馈环=带看门狗的自校正重试。运行时按拓扑**分层并行执行**（同层并发、层延迟取最慢支路），把模糊的 Agent 输出量化成质量电平，据此路由与终止。配套编译器把自然语言目标编译成拓扑，并提供 FastAPI 服务（端口 8765）、CLI（`run.py`）、Web 控制台（`console.html`）三套入口。核心运行时仅依赖 Python 标准库。

---

## ② 核心概念速查（电路元件隐喻对照表）

来源：`SPEC.md` §1 核心隐喻表。

| 电路元件 | Agent 工作流角色 | 关键参数 |
|---|---|---|
| **电源 Power** | 初始任务 / 目标 | `task` |
| **运放 OpAmp** | 调度器（高 Zin 读任务、低 Zout 驱动支路） | `spec_clarify`（扇出前先澄清规格） |
| **电阻 Resistor** | 原子 Agent（small/large/tool 三档）· **变换器**：`output=min(输入质量,自身能力)` | `model`、`cost`、`latency`、`accuracy`、`yield` |
| **电容 Capacitor** | 上下文汇合 / 缓冲 | `cost`、`latency` |
| **二极管 Diode** | 单向格式校验（防错误回流） | `cost`、`latency` |
| **ADC** | 评估打分器（把模糊输出量化成 high/low 电平） | `threshold` |
| **看门狗 Watchdog** | 反馈环迭代上限（防无限重试烧钱） | 由 `feedback.max_iter` 控制 |
| **桥式整流 Bridge** | 多模态输入对齐成标准格式 | `cost`、`latency` |
| **逻辑门 / 开关 LogicGate** | 条件路由（按 ADC 电平选支路） | — |
| **源 Source** | 外部原始信号（文本/图像/表格） | `quality` |
| **导线 Wire** | 确定性结构化数据传递 | — |

**开关 / 熔断（来自真实讨论与示例）**：
- **开关（switch）**：条件路由 / 隔离支路——按 ADC 量化电平决定走哪条支路（见 `diagrams/主备双支路·开关自动换路（switch 演示）.svg`、`examples/switch_isolation.json`）。
- **熔断（fuse）**：良率监视 / 防反复烧钱——电阻支路有 `yield` 可能「开路」（幻觉/拒答）却照样 `cost`，熔断器在反复失败时切断重试，防成本失控（见 `diagrams/熔断器保护·防反复烧钱（fuse 演示）.svg`、`examples/fuse_blow.json`）。

**校准过的语义（SPEC.md 讨论结论，务必内化）**：
1. 电阻**不是确定性**的——有 `yield`（良率），可能「开路」（幻觉/拒答）却照样 `cost`，故需熔断/良率监视。
2. 电阻是**变换器不是生成器**：`output = min(input, capability)`，输出质量受上游输入约束。
3. 逻辑门/开关需 **ADC 先量化**：Agent 输出是连续置信分布，不是干净电平。
4. 并联只抗「执行」方差，**不抗「规格」方差**——故 opamp 的 `spec_clarify` 在扇出前先澄清规格。
5. 反馈环是**离散的、每轮都烧钱**，必须配 watchdog 迭代上限，否则成本失控。

---

## ③ 三条最快上手路径

**路径 1：跑自检（验证环境可用，零依赖零联网）**
```bash
python -c "import runtime; runtime.selftest()"   # 内核自检
python server.py --selftest                       # 全量 S1–S30 离线自检
python -m compiler.backend_llm                    # backend_llm 模块自检（5 用例）
```

**路径 2：用 plan.py 编译执行一个真实目标**
```bash
python <codex-skills>/circuit-planner/scripts/plan.py "<目标>" [--backend=real]
```
- 真实形态：`plan.py` 产 **runbook**（人读 + 机读 JSON）→ agent 按拓扑序用对应工具真实执行、串上下文、并行同发、反馈环。`plan.py --execute` 产出 `scripts/runbooks/<name>_runbook.json`，由 `scripts/exec_loop.py` 消费维护状态。
- 例：`plan.py --execute --draw "总结一篇PDF并核对里面的数字，要求高可靠"` → 解析出 `[retrieve,reason,calculate,verify]`+pdf+high → 4 电阻链 + adc 质量门，runbook + SVG 落盘。
- 直接跑拓扑：`python run.py examples/parallel.json --runs 100 --seed 42`（离线对照）；`--backend` 取值 `auto`/`real`/`local`/`mock`·`sim`。

**路径 3：必读的三个文件**
1. **`SPEC.md`** — 电路 DSL 规范（元件库、执行语义、指标）——**先读这个**
2. **`COMPILER.md`** — 自然语言 → Goal → 拓扑 编译（六段流水线 + 标准单元库）
3. **`README.md`** — 项目全貌、能力地图、快速开始、HTTP API 速览

---

## ④ 关键约定（血泪教训，务必遵守）

**1. 实测必须加 `--backend=real`**
默认 `SimBackend` 只是**随机模拟**（给定 seed 可复现），用于验证拓扑*相对*行为，**不是真值**。真实 LLM 后端需联网 + API key（`DEEPSEEK_API_KEY` 或桌面 `key_tmp.txt`）。`--backend` 取值：`auto`(有 key 走真模型否则 SimBackend) / `real`(强制真模型) / `local`(本地 transformers/Ollama 桥) / `mock`·`sim`(强制 SimBackend)。**结论若要对外宣称「实测」，必须 real 后端跑过**——否则库里仍是估计值（垃圾进→垃圾拓扑）。注意：真模型烧 key/出网，快速验证建议 `--runs 1` 或移除 key 文件退回离线。

**2. 质量门与 VERIFY 异构校验**
- 质量门（quality gate）是拓扑里的 `adc` 节点——把模糊 Agent 输出量化成 `high/low` 电平，据此路由与终止；`success` 取 `adc.ok`，`final_quality` 取 adc 质量分。
- `verify` 是独立能力节点（`compiler/codegen.py` 提到 `adc/verify 校验`），与 `adc` 打分是**异构校验**——adc 量化打分、verify 做独立核对，二者不互相替代。**不要用同一模型既执行又自评**。
- 高可靠任务应同时布 adc 质量门 + verify 校验节点（见 COMPILER.md 走查示例：`reason` 与 `calculate` 并联、`calculate` 外包反馈环 adc+watchdog）。

**3. topology_memory 的拓扑复用与教训库**
- **拓扑复用**：`compiler/topology_memory.py`（数据落 `.topology_memory.json`）。`plan.py` 编译前自动 `topology_memory.recall(goal)`，命中则直接复用成功拓扑（闭合正反馈环：节点质量↑→总线经验↑→下次 recall 检索更优→首跑质量↑）。`compile_goal` 带出 `goal_desc` 供 recall 匹配。配套 `.topology_repo.json` 为共享仓库（TopologyShare 发布/拉取）。
- **教训库**：`.topology_memory.json` 里存有真实教训，例如：「LLM 无工具检索时凭参数知识硬编答案：曾幻觉出项目里根本不存在的 Redis 后端（正文 26 次），而真实存储 topology_memory/executions.db/ShareRepo 零提及。**retrieve 类节点必须先 query_db 读真实源码再写设计**。」——写任何设计/文档前，先查真实代码，别凭记忆编造文件名或接口。
- 并发写入 TopologyMemory 已加线程锁（`topology_memory._MEM_LOCK`）。

**4. 其他约定**
- 解释器：基础 stdlib 用 `~/.workbuddy/binaries/python/versions/3.13.12/python.exe`；服务（fastapi/uvicorn）用 `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe`。
- 沙箱限制：禁止 `Remove-Item`/`Start-Process`；写本目录需按轮次授权。
- 服务默认端口 8765；`POST /run` 触发后台执行，经 SSE `GET /run/{id}/stream` 实时观测。

---

## ⑤ 当前已知边界与诚实声明

- **组件画像（`_TIERS` 库）是估计值**：真实值要靠 `--backend real` 实测回填；未接真后端前，优化器/仿真结果只是估计（COMPILER.md §6/§8「库反哺」前提是接上真实后端）。
- **真实 LLM 后端需联网 + API key**；无 key 时 `auto` 会静默退回 SimBackend——**这是最容易踩的坑**，务必显式 `--backend=real` 才代表真实验证。
- **NL → netlist（规划「所需能力」）是最难的 AI 环节**：COMPILER 明言这是 planning 问题，需 eval 兜底而非拍脑袋；结构化目标是务实第一目标。
- **拓扑空间组合爆炸**：必须靠标准单元库（5 个已验证模板）剪枝，不能暴力枚举。
- **经验库薄**：`topology_memory` 用 FIFO 100 + Jaccard 关键词匹配，无语义/向量检索；mentor 只在失败案例上训练（MEMORY_ARCHITECTURE.md 第 58 行）。
- **调度器（opamp）仍是规则/LLM 规划**：与「主 agent 也是 LLM 实例」的更完整形态仍有距离（那步需改 `plan.py` 让调度器也输出 DAG JSON）。
- **`plan.py` 是 Bash 沙箱 Python 脚本，调不动 WorkBuddy 原生工具**（WebFetch/Read/Write/Bash）——「执行模式」真实形态 = plan.py 产 runbook → agent 按拓扑序用对应工具真实执行。
- **身份文件（SOUL/IDENTITY/USER/BOOTSTRAP）此前未落盘**（RESULTS.md 记录，未动）——本文件即补此缺。
- **许可证暂未指定**；本地模型桥接（`local_llm_bridge.py` + `PORTABLE.md`）依赖 `torch/transformers/modelscope` 等重依赖，需在专用 venv 中运行。
- 本文件是**身份引导，非设计文档**：元件语义细节以 `SPEC.md` 为准，编译流程以 `COMPILER.md` 为准，实测记录见 `RESULTS.md`。

---

*本文件由 retrieve#5 依据仓库真实代码与文档生成，未编造文件名或接口签名。*