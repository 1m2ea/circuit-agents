# circuit-agents — 任务规划与执行引擎

独立、可部署的通用任务规划/执行引擎：编译器(compiler/) + 运行时(runtime.py) + FastAPI 服务(server.py, 端口 8765) + CLI(run.py) + Web 控制台(console.html)。

## 核心文档
- SPEC.md — 电路 DSL 规范（元件库、执行语义、指标）
- COMPILER.md — NL → Goal → 拓扑编译
- CIRCUIT_EXECUTOR_DESIGN.md — 执行器设计
- RESULTS.md — 实测结果记录

## 常用操作
- 规划：`python <codex-skills>/circuit-planner/scripts/plan.py "<目标>" [--backend=real]`
- 执行：`python run.py examples/<spec>.json --runs N [--backend auto|real|sim]`
- 服务：`python server.py --port 8765`（用 envs\default 解释器）
- 自测：`python -c "import runtime; runtime.selftest()"`

## 解释器
- 基础（stdlib）：`~/.workbuddy/binaries/python/versions/3.13.12/python.exe`
- 服务（fastapi/uvicorn）：`~/.workbuddy/binaries/python/envs/default/Scripts/python.exe`

## 注意
- 真实 LLM 后端需联网 + API key（DEEPSEEK_API_KEY 或桌面 key_tmp.txt）。
- 沙箱：禁止 Remove-Item/Start-Process；写本目录需按轮次授权。
