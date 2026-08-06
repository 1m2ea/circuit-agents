# circuit-agents

> 把任务规划成**电路拓扑**、用多档本地/远程模型**闭环执行**的 Agent 框架。
> 奥卡姆剃刀化简 · 多后端并行 · 零 API 费本地推理 · 便携 U 盘工作站。

---

## 这是什么

Circuit-Agents 用「电路设计」的思想来编排 Agent 工作流：**用确定性的拓扑结构去驾驭概率性的模型输出**。

- 一个任务被描述成一张 **拓扑图（DSL）**：`电源(Power)`=目标，`电阻(Resistor)`=原子 Agent（小/大/工具模型），`运放(OpAmp)`=调度器，`电容(Capacitor)`=上下文汇合，`二极管(Diode)`=格式校验，`ADC`=质量打分器，`反馈环`=带看门狗的自校正重试。
- 运行时按拓扑**分层并行执行**，同层节点并发、层延迟取最慢支路；把模糊的 Agent 输出量化成质量电平，据此路由与终止。
- 配套一套编译器，可把自然语言目标编译成拓扑，再交给运行时执行；并提供 FastAPI 服务、CLI、Web 控制台三套入口。

完整元件语义与指标定义见 [`SPEC.md`](SPEC.md)。

## 能力地图（已全部落地）

**第一层 · 能力深化**
| # | 方向 | 状态 |
|---|---|---|
| ① | 更多原子能力（compare / predict / decompose） | ✅ |
| ② | 更丰富的技能包（draw_chart / send_email / query_database） | ✅ |
| ③ | 更智能的模型选型（复杂度/历史/约束 + 跨供应商路由 + 多目标再平衡） | ✅ |
| ④ | 多模态输入（真视听觉转录 + 离线降级占位） | ✅ |
| ⑤ | 记忆与学习（TopologyMemory 拓扑记忆） | ✅ |

**第二层 · 边界扩展**
| # | 方向 | 状态 |
|---|---|---|
| ⑥ | 多任务并行（BatchExecutor 并发 + 资源隔离 + 汇聚） | ✅ |
| ⑦ | 长周期任务（断点续跑 + 心跳 + 暂停/恢复 + 休眠唤醒） | ✅ |
| ⑧ | 人机协同（关键决策点 proceed/skip/abort 三态） | ✅ |
| ⑨ | 多机器人协同（共享黑板编排 + agent 级拓扑序） | ✅ |
| ⑩ | 安全与权限（PermissionGate 越权校验 + 执行期拦截） | ✅ |

**第三层 · 范式升级**
| # | 方向 | 状态 |
|---|---|---|
| ⑪ | 自适应拓扑（CircuitMutator 增删/重连 + auto_heal 自愈） | ✅ |
| ⑫ | 跨平台部署（DeploymentExporter 导出 Dockerfile/runner） | ✅ |
| ⑬ | 电路图共享生态（TopologyShare 发布/拉取 + 本地仓库） | ✅ |
| ⑭ | 自我进化（SelfEvolution 历史蒸馏为可复用拓扑模板） | ✅ |

**第四层 · 运行时智能（Phase 2 增强）**
| 方向 | 状态 |
|---|---|
| 更细粒度质量门 / 技能注册表 / 跨语言编译(C++/Rust/JS) / 分布式执行 / 联邦学习 / 自主发现新元件 / 形式化验证 / 在线调参(UCB1) / 编译成零 LLM 静态图 / 执行历史因果分析 / 异构硬件后端(Ollama 本地模型) / 奥卡姆剃刀化简 Pass | ✅ 全量 |

## 架构

```
compiler/     自然语言 → Goal → 拓扑；各类后端(Backend)实现
runtime.py     拓扑运行时：分层并行、反馈环、质量门、记忆
server.py      FastAPI 服务（默认端口 8765）
run.py         CLI：跑单个拓扑，可多轮平均
console.html   Web 控制台（可视化 + SSE 实时监控）
examples/      示例拓扑 JSON + 本地模型演示
```

## 快速开始

> 核心运行时仅依赖 Python 标准库；起服务需 `fastapi/uvicorn/pydantic`；接本地模型需 `torch/transformers/modelscope`。

```bash
git clone https://github.com/1m2ea/circuit-agents.git
cd circuit-agents

# 1) 离线对照（stdlib 即可，无需联网/key）
python run.py examples/parallel.json --runs 100 --seed 42

# 2) 起 HTTP 服务（需 fastapi/uvicorn/pydantic）
python server.py --port 8765
#    浏览器打开 http://127.0.0.1:8765/ 即 Web 控制台（console.html）

# 3) 自然语言规划（经 circuit-planner 技能把目标编译成拓扑后执行）
python run.py examples/feedback.json --backend auto
```

`--backend` 取值：`auto`(有 key 走真模型否则 SimBackend) / `real`(强制真模型) / `local`(本地 transformers/Ollama 桥) / `mock`·`sim`(强制 SimBackend 离线对照)。

## 本地模型：零 API 费、零联网

无需 Ollama，用一层 OpenAI 兼容 HTTP 桥把本机 `transformers` 模型（如 Qwen2.5-1.5B）接进框架：

```bash
# 终端 A：起桥（在装好 torch/transformers/modelscope 的 venv 中）
python local_llm_bridge.py --offline --port 8000

# 终端 B：跑真实本地推理
python examples/local_model_demo.py          # 一键演示
python run.py examples/feedback.json --backend local
```

桥忽略请求里的模型名、固定用已加载的本地模型生成；全链路 `OllamaBackend(openai)→HTTP→桥→模型`，**零 API 费用、零外网依赖**。详见 [`PORTABLE.md`](PORTABLE.md) 第 6 节。

## 便携 U 盘工作站

把 `circuit-agents` + 模型 + 便携 Python 打进 U 盘，插任意 ≥16GB 内存的电脑即插即用、拔掉不留痕迹。见 [`PORTABLE.md`](PORTABLE.md) 与 `portable_launch.py`。

## HTTP API 速览

服务启动后（`POST /run` 触发后台执行，经 SSE `GET /run/{id}/stream` 实时观测）：

| 分组 | 端点 |
|---|---|
| 执行核心 | `GET /` · `GET /health` · `POST /run` · `GET /run/{id}` · `GET /api/history` · `GET /run/{id}/stream` |
| ⑥ 多任务并行 | `POST /batch` |
| ⑦ 长周期 | `POST /longtask` · `GET /longtask/{id}` · `POST /longtask/{id}/pause` · `/resume` · `/wake` |
| ⑨ 多机器人 | `POST /multirobot` |
| ⑩ 权限 | `POST /permission` |
| ⑪ 自适应拓扑 | `POST /topology/mutate` |
| ⑫ 部署 | `POST /deploy` |
| ⑬ 共享生态 | `POST /topology/publish` · `GET /topology/repo` · `POST /topology/pull` |
| ⑭ 自我进化 | `POST /evolve` · `POST /evolve/suggest` |
| 质量门/技能/选型 | `POST /quality/report` · `POST /skills` · `POST /skills/resolve` · `POST /models` · `POST /models/select` |
| 多模态/分布式/决策 | `POST /transcribe` · `POST /cluster` · `POST /decision` |
| RL/联邦/元件挖掘 | `POST /rl/optimize` · `POST /federated/round` · `POST /components/discover` · `GET /components/library` |
| 跨语言/验证/调参 | `POST /codegen` · `POST /verify` · `POST /tune` |
| 静态图/因果/异构 | `POST /static-graph/compile` · `POST /causal/analyze` · `POST /ollama/run` · `POST /ollama/health` |
| 奥卡姆剃刀 | `POST /simplify` |

## 自测

```bash
python -c "import runtime; runtime.selftest()"   # 内核自检
python server.py --selftest                       # 全量 S1–S29 离线自检（零回归）
```

## 文档索引

- [`SPEC.md`](SPEC.md) — 电路 DSL 规范（元件库、执行语义、指标）
- [`COMPILER.md`](COMPILER.md) — 自然语言 → Goal → 拓扑 编译
- [`CIRCUIT_EXECUTOR_DESIGN.md`](CIRCUIT_EXECUTOR_DESIGN.md) — 执行器架构设计
- [`RESULTS.md`](RESULTS.md) — 实测结果记录
- [`PORTABLE.md`](PORTABLE.md) — 便携 U 盘工作站方案

## 状态

核心框架与四层能力均已落地并通过离线自检。真实 LLM 后端需联网 + API key；本地模型桥接与便携方案已真机端到端验证。许可证暂未指定。

---

*项目演进：从「命名漂移符号映射表 + 参数化 CI 冒烟工具」起步，逐步长成完整的电路拓扑 Agent 框架。*
