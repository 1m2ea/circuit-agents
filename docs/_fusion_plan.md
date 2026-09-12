# circuit-agents x DeepSeek Harness 融合项目 - 工作笔记(进行中)

> 会话目标(goal-4c09cd7c): 融合成一个"扬长避短"的新系统;
> 交付物 = 一个新系统 + DeepSeek Harness 原样 + circuit-agents 原样。
> 改动前全量备份。用户已确认: 备份目录 D:\dev\projects\666\_backup_<日期>; 新系统放新建独立目录。

## 用户原话(要点)
- 把"你的内核"用 circuit-agents 项目替换 -> 澄清后 = 两者彻底融合, 非单向替换。
- "各自取长补短、扬弃, 好的留下放到新的里面, 坏的就丢掉。"
- "所有修改先备份确保风险可控。最后结果 = 一个全新的 + 两个各自旧的。"

## 已确认事实

### A. DeepSeek Harness(DSH / dsh) - "我"这套系统
- 桌面版 = Electron 壳(resources\app\main.js, 249 行) + 子进程 node.exe dsh web --host 127.0.0.1 --port 3080(cwd=resources\runtime\dsh); GUI=BrowserWindow 加载该 URL。
- dsh 内核 = cordis 插件体系(npm 发布版, 编译后 lib/*.js, 本机无 TS 源码); profile 装配: .dsh\profiles\web\package.json 的 dsh.profile.bundles(@deepseek-ai/dsh-base, dsh-web-app) + cordis.patch.yml 追加 bundle。
- 扩展缝(官方机制): skills(.dsh\skills\<name>\SKILL.md, 已有 circuit-planner); profile 插件 bundle/cordis.patch.yml; client-ui 插件(dsh-client-ui-*, 如 plugin-marketplace); MCP 客户端 @deepseek-ai/dsh-mcp-client 已存在。
- 用户数据/会话: .dsh\sessions\<workdir-encoded>\session-*.jsonl.zstd(dsh-session 库)。
- 本机: 无外网(npm registry 不可达)、无 pnpm、git 2.55、node 22.22.2(含 zstd)。

### B. circuit-agents(D:\dev\projects\666\circuit-agents) - 纯 Python 框架(调研报告已回)
- 规模 83 py + 9 html ~3.64 万行; runtime.py 5.7k / server.py 4.7k 行(巨型单文件)。
- 四入口: runtime.py(拓扑执行内核)/ compiler/(NL->Goal->拓扑+多后端)/ server.py(FastAPI ~92 端点 127.0.0.1:8765)/ run.py(CLI)。前端 3 手写 HTML(console/chat/topology_editor)。
- 桌面: desktop_launcher.py = PyWebView + 进程内 uvicorn(import server 库式嵌入), 默认开 /chat。
- 对话层 chat_agent.py: deepseek/openai/local/offline 四档; function-calling 循环 <=8 步; run_command 审批队列 needApproval/approve; SYSTEM_PROMPT 自称"能力对齐 DeepSeek Harness 助手"。
- 工具 agent_tools.py: 6 工具(list_dir/read_file/write_file/run_command/web_search/circuit_plan); CA_AGENT_ROOT 工作区沙箱 + 高危 auto-block + 审批。compiler/agent_skills.py 另 20 技能。
- 拓扑执行: DAG 分层真并发、adc 质量门、feedback 反馈环、watchdog 持久化、switch 跳闸/fuse 熔断自愈、self_heal、CircuitExecutor(人工决策/暂停/运行中编辑/SSE)。
- 记忆: TopologyMemory + 教训回流 + executions.db(SQLite+replay) + Watchdog + chat_sessions.json。
- 接缝: mcp/ stdio MCP server(selftest/simulate/plan); local_llm_bridge OpenAI 兼容 /v1/chat/completions; 库式嵌入范式; env 体系(CA_PORT/CA_AGENT_ROOT/CA_LLM_BASE/DEEPSEEK_API_KEY/桌面 key_tmp.txt)。
- git: HEAD 42c227e; 工作树脏 - 融合相关新代码(agent_tools/chat_agent/chat.html/mcp/)未入库(M3 + ??~20), 融合前应先入库/快照。

## 融合方向(草案, 待 DSH 报告确认后定稿)
1. DSH 宿主 + circuit-agents 引擎(倾向推荐): 新系统=独立目录, 以 dsh web+cordis 为壳与 UX, circuit-agents 作为 MCP server 接入(dsh-mcp-client -> mcp_server.py) 和/或 本量子服务(库式 uvicorn / Electron main 拉起 python 子进程)。不动 dsh 内核二进制。
2. circuit-agents 宿主 + DSH 壳: 改造重、收益低, 不推荐。

## 备份方案(位置已确认, 执行需批准写 D 盘)
- D:\dev\projects\666\_backup_2026-09-07\ 下: circuit-agents\(58MB 含 .git/dist/未提交) + DeepSeek Harness\(682MB) + .dsh\(13MB)
- 校验: 比对文件数与总字节; hash 抽查。

## 执行顺序
1. 已确认方向与备份位置
2. DSH 内核调研报告(dsh 扩展点/plugin API) - 等待中
3. 产出并确认《融合蓝图》
4. 全量备份(批准写 D 盘)
5. 建新项目目录分步实施
6. 端到端验证 + 交付(新系统 + 两个旧版可运行)

### A. 补充(直接读 dsh lib/bin.js 确认)
- `dsh web` = `dsh --profile web`; profile 位于 $DSH_HOME/profiles(即 C:\Users\lgw12\.dsh\profiles\web)。
- **支持 `--patch <path>`(可重复) 在 profile 层之后追加补丁 overlay**; `--dump-config` 可打印合成后的 profile 树。
- 含义: 融合可以 = 独立新 profile 或 --patch overlay 层(挂 mcp/技能/工具), 不改原安装目录; 也支持 DSH_HOME 指向别处以完全隔离。

## 调研完成(两份报告要点已并入上文)

### C. 关键架构结论
- DSH = Cordis 插件树宿主 + 事件溯源会话 + 前后端双 Cordis 树; "内核"是一个可当库嵌入的 boot 生态(@deepseek-ai/dsh-app-boot boot()), 也可按 profile/patch 层增量扩展, 不改安装目录。
- DSH 扩展三路(实证): profile 插件目录+用户 patch 层(桌面已用 dsh-balance 等 5 包示范) / 技能根(.dsh\skills, circuit-planner 已生效) / client 插件(dsh.client 声明+exports ./client 构建产物, 需源码构建链) / MCP 客户端 dsh-mcp-client(stdio/streamable-http, 把远端工具桥成 mcp__<srv>__<tool>)。
- circuit-agents 可整库嵌入(desktop_launcher 范式: import server + 线程 uvicorn) 或 MCP stdio 接入(selftest/simulate/plan)。
- 会话互操作: decodeStorageRecord/packChunkRuns + node:zlib zstd, Node22 现成。
- 本机无外网(npm registry 不通) -> 代码级重建 client-plugin bundle / 重编 dist 在离线环境风险高, 行级/配置级扩展零重装可行。

## 《融合蓝图 v0.1》(待用户拍板)

### 可行性结论
"彻底融合成一个新系统、且保留两个旧版各自可运行" —— 技术上可行, 但**正确姿势不是互相替换内核**, 而是分层合并:
- 两边不是同一层的东西: DSH 提供"宿主平台"(会话/GUI/审批/沙箱/工具/子代理/goal/技能生态, 成熟强大); circuit-agents 提供"一套方法论引擎"(电路拓扑规划/分层并发执行/质量门/自愈/记忆/离线仿真/本地模型桥, 独特且有工程价值) + 一套较弱的 Python 对话壳。
- 把 circuit-agents 当 DSH 的"内核"替换 = 丢掉 DSH 全部成熟能力去换一个更弱的对话壳, 不可取; 反之把 DSH 塞进 circuit-agents 壳 = 高成本低收益。
- 推荐: **以 DSH(桌面体验)为壳与宿主, 把 circuit-agents 作为"规划/执行引擎 + 方法论内核"接入**, 产物叫一个新名字(如 circuit-harness), 独立目录, 两旧原样。

### 目标形态(方案一, 推荐)
新目录(独立, 自包含):
- dsh 运行时闭包拷贝(可复用原安装 npm 闭包) + 独立 DSH_HOME(数据/会话/技能隔离, 不碰 C:\Users\lgw12\.dsh)
- circuit-agents 代码拷贝(引擎)
- 融合胶水:
  1. patch 层(cordis.patch.yml) 注册 circuit-agents 的 MCP stdio server -> DSH 侧直接出现 mcp__circuit_agents__plan/simulate/selftest 工具
  2. 技能 circuit-planner v2: 让"把复杂任务交给拓扑引擎规划/执行"成为 DSH agent 的默认思维路径之一(规划离线兜底, 执行真后端/仿真)
  3. 模型档整合: DSH 模型选择可含 circuit-agents 提供的本地通道(local_llm_bridge/Ollama), 离线零 API 费
  4. 启动器: 一键同时拉起 dsh web + (可选)circuit-agents 子服务
- 交付时三件套各自可运行: 新系统 exe/脚本 + 原 DeepSeek Harness(未动) + 原 circuit-agents(未动)

### 扬弃清单(草案)
留下(扬):
- DSH: 会话/存档/多工作区, goal/subagent/workflow/jobs/ralph, 审批+沙箱+权限体系, 技能系统, client 插件生态, 成熟 UI, MCP client, session 互操作库
- circuit-agents: 电路 DSL 编译链, runtime 分层并发+质量门+反馈环+watchdog+开关/熔断自愈, 记忆/教训回流/自我改进, 离线 SimBackend, 本地模型桥, MCP 封装, mentor/π-heartbeat 进化闭环
丢掉(弃, 待确认):
- circuit-agents: 手写三 HTML 作为"主 UI"的角色(chat/console 简化为新系统里的辅助视图或退役), 巨型单文件结构(runtime.py/server.py 在融合中按引擎模块引用而非继续膨胀), 两套安全标准中偏弱的 agent_skills.run_code(统一并入 DSH 审批+沙箱), 死代码端点/过时文档
- DSH 侧无明显需丢(保持原样即可); 融合新增物去重两边重复的"对话壳/规划接线"
新增(新):
- 统一密钥/工作区/数据目录约定; 拓扑执行的一等公民入口; 一键启动器; 融合版文档

### 分步计划(每步可回滚, 改动前都有备份)
- P0 备份(用户已确认位置 D:\dev\projects\666\_backup_<日期>)  -> 写 D 盘需提权批准
- P1 POC: 新目录 = dsh 副本(独立 DSH_HOME) + circuit-agents 副本 + MCP 注册, 在 DSH 会话里真实调起 mcp__circuit_agents__selftest/plan 验证闭环
- P2 技能 circuit-planner v2(规划入口默认化 + 执行器接通)
- P3 启动器 + 模型档整合(本地通道) + 数据约定统一
- P4 (可选/探索) client 插件 UI 融合 —— 离线无 npm 源, 需先评估能否离线产出 lib/client.js
- 交付: 三件套验证清单 + 使用说明

### 风险与回滚
- 全程不写 C:\Users\lgw12\.dsh 与 D:\dev\projects\666\circuit-agents 与安装目录; 新系统全部自包含 -> 回滚=删除新目录即可
- 唯一外部副作用 = P0 备份写入 D:\dev\projects\666\_backup_* (只增不改)
- 离线限制: UI 代码级融合与 npm 安装可能受阻, 列为 P4 探索项, 不阻塞 P1-P3

## P0/P1 完成记录(2026-09-07)
- P0 备份: D:\dev\projects\666\_backup_2026-09-07\{circuit-agents(907f,58MB), DeepSeek Harness(33834f,682MB), dsh-userdata(37f)} 校验通过(活跃会话文件除外,预期内)。
- P1 circuit-harness 骨架:
  - D:\dev\projects\666\circuit-harness\app\  = DSH 安装副本(33834 文件, 自包含)
  - D:\dev\projects\666\circuit-harness\engine\ = circuit-agents 副本(907 文件)
  - D:\dev\projects\666\circuit-harness\home\  = 独立 DSH_HOME(web profile: dsh-base+dsh-web-app bundles + 用户 patch 层)
  - home\profiles\web\cordis.patch.yml: insert mcp-circuit-agents 行(@deepseek-ai/dsh-mcp-client, stdio, serverName=circuit_agents, command=python mcp_server.py, cwd=engine, failOnStartupError=true)
- 验证: dump-config 519 行合成含 MCP 行; web boot 于 127.0.0.1:3091 HTTP200 前端完整; 进程树确认 dsh node(5328) fork python mcp_server.py(29136) => MCP client 激活并连接成功。
- 修复: engine\mcp\mcp_server.py _run() 子进程注入 PYTHONIOENCODING=utf-8/PYTHONUTF8=1 修复 GBK 打印崩溃(原项目未动, 只改 engine 副本)。
- 关键事实: DSH_HOME 环境变量可完全隔离; web profile 缺失时按模板自动初始化; bundle 从安装闭包解析(profile 无需自带 node_modules)。

## P2 完成记录
- 新 home 技能根 home\skills\circuit-planner 就位(copy 自原 .dsh 技能; 原 .dsh 未动)。
- SKILL.md v2: 双通道(① MCP 工具 mcp__circuit_agents__plan/simulate/selftest, ② plan.py 直连兜底) + engine 路径 + 拓扑执行接 DSH 工具链映射。
- plan.py 副本: _COMPILER_DIR_CANDIDATES 加 engine 优先(自包含)。
- MCP patch env 补 CIRCUIT_PLANNER_PY -> 新 home 技能 plan.py(不再依赖 ~/.dsh)。
- 验证: plan.py v2 + engine 编译器, "总结文档并核对数字要求高可靠" -> DAG 分层拓扑(retrieve→reason/calculate并联→verify→adc 0.8), 自动 feedback max_iter=3, memory_lessons 回流, 无 key 离线。
- web 3092 重启: HTTP200, 进程树 node->python mcp_server 确认; $DSH_HOME/skills(user-dsh rank400, chokidar watch)确认技能自动发现。

## P3 完成记录
- 启动器: circuit-harness\启动-circuit-harness.bat(默认桌面端=复用 app\DeepSeek Harness.exe + 注入独立 DSH_HOME; /web 纯浏览器模式端口3093)。
- 关键: 启动器 set DSH_HOME 只对自身进程树生效, 不污染原系统(已验证原 DSH 3080 与原 CA 8765/exe 全程不受影响)。
- 验证: 复刻启动器环境拉起 3093 -> HTTP200; 进程树 node->python mcp_server 自动随启(MCP 通道); 三件套并行事实确认(原 DSH PID17900/33764 + 原 CA exe WebView2 + circuit-harness 3093)。
- 注意: 桌面模式 exe 副本 = 原 Electron 壳(标题仍 DeepSeek Harness), 但数据/技能/引擎全在 circuit-harness home; 将来可换 main.js 标题/图标成品牌化(可选)。

## 交付完成记录(2026-09-07)
- 使用说明: D:\dev\projects\666\circuit-harness\使用说明.md
- 启动器: D:\dev\projects\666\circuit-harness\启动-circuit-harness.bat + 桌面快捷方式 circuit-harness.lnk
- 最终冷启动验证(3094): HTTP200, MCP python 子进程自动拉起, 技能在根。
- 三件套最终状态: 原 DSH 3080 HTTP200 / 原 CA 8765 ok / circuit-harness 冷启动通过。
- 隔离确认: 原 DSH 安装目录 vs 备份 0 差异; 原 CA 项目 vs 备份 0 差异。
- 交付物清单: 新系统(circuit-harness) + 两个旧版(原样) + 全量备份(_backup_2026-09-07) + 使用说明 + 启动器。

## 潜意识层移植完成(2026-09-08)
- 上游 657b3ce 的 subconscious layer 已移植到主仓(commit cd94761)与 engine 副本(文件hash一致)
- 验证: selftest 8项过 / runtime 零回归 / /subconscious HTTP 冒烟过
- 桌面功能(agent_tools/chat_agent/mcp)保留未动; 备份: 主仓 _subconscious_bak_20260908 + engine _fusion_bak_20260907


## 拓扑事实驱动实时更新（进行中, 2026-09-08）
需求: 拓扑不该是 plan 时定死的静态图, 应根据事实实时更新.
现状: exec_loop 驱动的 runbook 是静态的(只有失败回同图重跑), engine 虽有 auto_heal/换路/熔断但作用在 spec 且只在 runtime 引擎生效.
方案(已批准 mutate+rebuild, 扩展现有 circuit_exec):
  给 circuit_exec 加 circuit_evolve 工具, 两个 action:
    rebuild: 携修正目标重新 plan -> 新 spec+runbook, 续接已完成产出
    mutate: 就地改拓扑(insert/remove/reroute/escalate/auto_heal), 重生 runbook 续接
  触发器: 节点失败/质检不达 / agent 发现新事实 / 用户中途改需求
实现进展: circuit_evolve 工具已加 + 注册(7工具全部注册); rebuild 已验证(新 runbook 生成+spec缓存);
    mutate 的 escalate 有 bug(auto_heal_topology 返回元组需解包), mutator 需真实 spec(rebuild 后才可用) -> 交子agent打磨中.


## 整链闭环端到端验证通过(2026-09-09)
模拟 agent 从一句话到完成全链跑通: plan(含SIM-PREVIEW+runbook) -> circuit_exec(start/record) -> circuit_evolve rebuild(发现需交叉验证, carried=['1','3']续接不重跑) -> circuit_record_outcome(回写2条教训进记忆, lessons 6->8).
证明设计文档 §2 目标形态总流程=一个可用的完整闭环; 记忆落盘已验证, 下次plan会自动织入教训.

