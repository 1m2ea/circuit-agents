# circuit-agents MCP server

把 circuit-agents（github.com/1m2ea/circuit-agents）包装成 MCP（Model Context Protocol）
stdio server，供 Cursor / Trae / Coze / CodeBuddy / Claude Desktop 等支持 MCP 的客户端调用。
纯标准库实现（零 pip 安装），离线可用。

## 工具
| name | 作用 | 参数 |
|---|---|---|
| selftest | 内核离线自检 | （无） |
| simulate | SimBackend 离线仿真跑拓扑 | topology（examples/*.json 相对路径或内联 JSON）、runs、seed |
| plan | 自然语言 → 电路拓扑 + 执行 runbook（离线规则） | goal、optimize、draw |

## 启动命令（给客户端填）
```
命令:  C:\Users\lgw12\.workbuddy\binaries\python\versions\3.13.12\python.exe
参数:  D:\dev\projects\666\circuit-agents\mcp\mcp_server.py
```
其他 Python 也可（需 3.9+，无需任何三方包）；用 sys.executable 启动时甚至不需要指定绝对 python。

## 环境变量（可选）
- CIRCUIT_AGENTS_REPO   代码根，默认 D:\dev\projects\666\circuit-agents
- CIRCUIT_AGENTS_PYTHON 子进程 python（跑 run.py/plan.py 用），默认当前 python
- CIRCUIT_PLANNER_PY    plan.py 路径，默认自动找 ~/.dsh/skills 与 ~/.workbuddy/skills

## 各客户端接入示例
Cursor（~/.cursor/mcp.json 或项目 .cursor/mcp.json）:
```json
{ "mcpServers": { "circuit-agents": {
    "command": "C:\\Users\\lgw12\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe",
    "args": ["D:\\dev\\projects\\666\\circuit-agents\\mcp\\mcp_server.py"] } } }
```
Claude Desktop（claude_desktop_config.json）: 同上结构。
Trae / CodeBuddy / Coze（MCP 设置 → 命令行）: command/args 填上面启动命令。
注意 Windows 路径里的反斜杠在 JSON 中要写成 \\。

## 自测
```powershell
Get-Content mcp\_probe.jsonl | & python mcp_server.py   # 或任意能喂 stdin 的方式
```
`_probe.jsonl` 是协议握手样例；预期依次返回 initialize / tools/list / 三个工具结果。
