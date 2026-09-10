# circuit-agents MCP server

把 circuit-agents（github.com/1m2ea/circuit-agents）包装成 MCP（Model Context Protocol）
stdio server，供 Cursor / Trae / Coze / CodeBuddy / Claude Desktop / WorkBuddy / DeepSeek Harness
等支持 MCP 的客户端调用。纯标准库实现（零 pip 安装），离线可用。

> 本文件所在仓库是**唯一真相源（SSOT）**。按《秩序宪章》（`D:\dev\projects\666\_governance\秩序宪章.md`）：
> 三方 agent 的规划/执行/记忆写入都必须经本引擎；旧副本 `circuit-harness\engine` 已改建为
> 指向本仓库的 junction，不得再各自保留副本。

## 工具（8 个）
| name | 作用 | 参数 |
|---|---|---|
| selftest | 内核离线自检（S1–S8 线性关系断言） | （无） |
| simulate | SimBackend 离线仿真跑拓扑 | topology（examples/*.json 相对路径或内联 JSON）、runs、seed |
| plan | 自然语言 → 电路拓扑 + 执行 runbook（含仿真预演 SIM-PREVIEW） | goal、optimize、draw |
| circuit_exec | **治理执行闭环**：里程碑锁相环 / 分层并行 / 质量门 / 整链重试 | action(start/next/record/record_gate/record_ms/status/reset)、runbook、step、artifact、pass、milestone |
| circuit_evolve | **拓扑事实驱动演化**：rebuild（重编译）/ mutate（就地增删改节点） | action、runbook、goal、op、pred、succ、cid、old、new |
| circuit_schedule | **两轴调度**：产出类型化改进程序 + 纵向策略提议（需 apply=true 才落盘） | runbook、apply |
| circuit_record_outcome | **记忆互灌 + 验证门**：仅"已验证且治理类"教训进长期记忆 | goal、status、quality、evidence、verified、lesson_type、lessons、gate_threshold |
| circuit_subconscious | 潜意识层受限接口：feed 播种 → query 取候选假设 | action(feed/query/start/stop/tick/replay/intervene/state)、prompt、top_k、min_score |

## 启动命令（给客户端填）
```
命令:  C:\Users\lgw12\.workbuddy\binaries\python\versions\3.13.12\python.exe
参数:  D:\dev\projects\666\circuit-agents\mcp\mcp_server.py
```
其他 Python 也可（需 3.9+，无需任何三方包）；用 sys.executable 启动时甚至不需要指定绝对 python。

## 环境变量（可选）
- CIRCUIT_AGENTS_REPO    代码根，默认 `D:\dev\projects\666\circuit-agents`（即本仓库）
- CIRCUIT_AGENTS_PYTHON  子进程 python（跑 run.py/plan.py 用），默认当前 python
- CIRCUIT_PLANNER_PY     `plan.py` 路径；未设时自动依次找 `~/.dsh/skills/…`、`~/.workbuddy/skills/…`
                         （这两处按宪章已 junction 到技能 master）
- CIRCUIT_EXEC_LOOP_PY   `exec_loop.py` 路径；`circuit_exec` / `circuit_evolve` 的 runbook 状态机全靠它
- CIRCUIT_COMPILER_DIR   编译器根（含 `compiler/` 包的目录），供 plan.py 定位

## 各客户端接入示例
Cursor（~/.cursor/mcp.json 或项目 .cursor/mcp.json）:
```json
{ "mcpServers": { "circuit-agents": {
    "command": "C:\\Users\\lgw12\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe",
    "args": ["D:\\dev\\projects\\666\\circuit-agents\\mcp\\mcp_server.py"] } } }
```
Claude Desktop（claude_desktop_config.json）: 同上结构。
Trae / CodeBuddy / Coze（MCP 设置 → 命令行）: command/args 填上面启动命令。
WorkBuddy（`~/.workbuddy/mcp.json`）: 已注册 `circuit_agents`（另有 `env` 段显式指定上述路径）。
DeepSeek Harness 系（`<DSH_HOME>/profiles/web/cordis.patch.yml`）: 经 `@deepseek-ai/dsh-mcp-client` 注册为
`mcp__circuit_agents__*` 工具；circuit-harness 与本机原版 DSH 均已接入。
注意 Windows 路径里的反斜杠在 JSON 中要写成 \\。

## 自测
```powershell
Get-Content mcp\_probe.jsonl | & python mcp_server.py   # 或任意能喂 stdin 的方式
```
`_probe.jsonl` 是**握手样例**（含 initialize 与 tools/list 请求）；按行喂入后应看到对应响应。
若要逐个验证工具，直接调用即可（无需协议握手），例如：

```python
import importlib.util
spec = importlib.util.spec_from_file_location("m", r"D:\dev\projects\666\circuit-agents\mcp\mcp_server.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(len(m.TOOLS))          # 8
print(m.tool_selftest({}))   # SELFTEST-OK + S1..S8
```

## 版本说明
本 server 由 circuit-harness（主导方）于 2026-09-11 从融合副本收编入库（commit `5edc819`），
取代此前的 3 工具旧版（旧版保留于 git 历史）。收编范围**仅此一个文件**：
经 blob 级取证，副本侧其余未提交项均与本仓库已有内容逐字节重复或属产物，故未带入。
