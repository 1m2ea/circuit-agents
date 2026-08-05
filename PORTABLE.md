# circuit-agents 便携 AI 工作站（U 盘版）

把 circuit-agents 整套装进一个 U 盘，插上任意一台 **≥16GB 内存**的电脑即可本地驱动电路引擎，
**零 API 成本**、**拔掉不留痕迹**。本文件是组装 + 使用说明（"纸面方案"，U 盘修好/换新即可照做）。

---

## 1. 目录结构（U 盘根下建 `AI/`）

```
U盘根目录/
└── AI/
    ├── launch.py            # 启动器（本仓库的 portable_launch.py 拷过来）
    ├── ollama/              # Ollama 程序 + 模型存储
    │   ├── ollama(.exe)     # Ollama 二进制（Windows 为 ollama.exe）
    │   └── models/          # 模型文件（OLLAMA_MODELS 指向这里）
    ├── circuit-agents/      # 本项目（本仓库内容）
    └── python-portable/     # 便携 Python（可选，电脑没装 Python 时用）
```

> 本 `PORTABLE.md` 位于仓库根，组装时拷到 `U盘/AI/README.md` 即可。

---

## 2. 模型清单（三档按用途分工，非单个超大模型）

| 用途 | 模型 | 占用 | 映射到 tier |
|---|---|---|---|
| 日常 / 中文 | `qwen2.5:7b` | 4–5 GB | `small` / `tool` |
| 代码生成 / 审查 | `deepseek-coder-v2` | ~5 GB | `code` |
| 重度推理 | `qwen2.5:14b`（或 `llama3.1:8b`） | 8–10 GB | `large` |

容量测算（装三个模型）：Ollama~1GB + 模型~20GB + circuit-agents~10MB + 便携 Python~200MB ≈ **25 GB**，
100G+ U 盘绰绰有余，剩余空间可放其它东西。

---

## 3. 组装步骤

1. U 盘根建 `AI/`，并在其下建 `ollama/`、`ollama/models/`、`circuit-agents/`、`python-portable/`。
2. 下载 Ollama 便携版（或把已安装的 `ollama` / `ollama.exe` 拷到 `AI/ollama/`）。
3. 把本仓库全部内容拷到 `AI/circuit-agents/`。
4. 把本仓库的 `portable_launch.py` 拷为 `AI/launch.py`。
5. （可选）放一个便携 Python 到 `AI/python-portable/`：
   - 若是目标电脑都没装 Python，用 **embeddable Python** 或 **Miniconda-portable**；
   - 并在其 venv 里装依赖：`pip install fastapi uvicorn`（server 模式需要）。
6. 首次在有网的电脑上预拉模型（避免每次现场拉）：
   ```
   set OLLAMA_MODELS=U盘\AI\ollama\models
   ollama pull qwen2.5:7b
   ollama pull deepseek-coder-v2
   ollama pull qwen2.5:14b
   ```

---

## 4. 启动

插上 U 盘，进 `AI/` 目录：

```bash
# 干跑自检：只校验路径/环境，不启动进程（推荐先跑一次）
python launch.py --check

# 正式启动：起 Ollama + 起 circuit-agents server
python launch.py
```

- 启动后访问 **http://localhost:8765**（Live Console）。
- 启动器会：设 `OLLAMA_MODELS`/`OLLAMA_HOST` 指向 U 盘 → 起 `ollama serve` →
  按需 `ollama pull` 缺失模型 → 起 `circuit-agents/server.py`。
- **关闭**：`Ctrl+C`，启动器会终止 Ollama 与 server，本机不留残留。
- 常用参数：`--host`（Ollama 地址）、`--models`（要拉的模型）、`--no-pull`（跳过拉取）、
  `--python`（指定 Python）、`--server-port`（server 端口）。

> 没有便携 Python 且目标电脑已装 Python+依赖时，`launch.py` 会用 `sys.executable` 直接跑 server。

---

## 5. `code` 档路由说明（让代码任务走 deepseek-coder-v2）

`OllamaBackend` 已内置映射 `code → deepseek-coder-v2`（见 `compiler/ollama_backend.py`
的 `DEFAULT_OLLAMA_MODELS`）。要让某步真正走这个模型，需在该 resistor 节点把 `model` 字段设为 `"code"`：

```json
{ "type": "resistor", "label": "generate_code", "model": "code" }
```

- 当前 `auto_tiers` 默认只发 `small`/`large`/`tool` 三档；**手动**把代码类 resistor 标 `model:"code"`
  即可立即生效（映射已存在）。
- 若想**自动**把"代码生成/审查"类步骤路由到 `code` 档，需给 `auto_tiers`/`ModelMetrics` 增加 code 档判定
  （属后续增强，不在本纸面方案内）。
- 运行时若 U 盘没有 `deepseek-coder-v2`，`code` 档会回退到 `small`（不影响可用性，只是不是专用代码模型）。

---

## 6. 注意事项

- 目标电脑内存 **≥16GB**（14b 模型推理约占 10G+，7b 约 5G；同机只跑一个模型更稳）。
- 启动器默认监听 `127.0.0.1`，仅本机访问；如需局域网共享，改 `--host 0.0.0.0`（注意安全风险）。
- Ollama 模型文件很大，**第一次**建议在在家有网时预拉好，现场用 `--no-pull` 启动。
- 拔盘前务必 `Ctrl+C` 关闭启动器，否则 Ollama 进程可能仍占用 U 盘文件。
- 便携 Python 方案未定（embeddable vs Miniconda-portable），按需选择；server 模式必须能 `import fastapi`。
