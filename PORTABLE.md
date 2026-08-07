# circuit-agents 便携 AI 工作站（U 盘版）

把 circuit-agents 整套装进一个 U 盘，插上任意一台 **≥16GB 内存**的电脑即可本地驱动电路引擎，
**零 API 成本**、**拔掉不留痕迹**。

> **当前已建成状态（2026-08-07）**：U 盘（E: 234GB）按 **transformers 桥接路线** 实测建成——
> 1.5B 模型（2.89GB）+ 装好 torch 的便携 Python + `launch.py`（桥接版启动器）。
> 已在真机用 U 盘自己的 `python.exe` 端到端跑通真实推理。
> 原 `portable_launch.py`（Ollama 版启动器）**保留在 `circuit-agents/` 内**，等你拿到 Ollama 引擎后即可改走 Ollama 路线。
> 本文件既是"纸面方案"，也是这份 U 盘的实际说明。

---

## 1. 目录结构（U 盘根下建 `AI/`）

```
U盘根目录/
└── AI/
    ├── launch.py            # 启动器（桥接版：起 local_llm_bridge + circuit-agents server）
    ├── ollama/              # （预留）Ollama 程序 + 模型存储，引擎就绪后启用
    │   └── models/          # 模型文件（OLLAMA_MODELS 指向这里）
    ├── models/
    │   └── Qwen2.5-1.5B/    # 本地 transformers 模型（snapshots/master 含 tokenizer.json + model.safetensors）
    ├── circuit-agents/      # 本项目（本仓库内容；portable_launch.py=Ollama版启动器已保留在内）
    └── python-portable/     # 便携 Python（embeddable 3.13 + torch/transformers/fastapi/uvicorn，已装好）
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

## 3. 组装步骤（桥接版 · 已实测）

1. U 盘根建 `AI/`，并在其下建 `ollama/models/`、`models/`、`circuit-agents/`、`python-portable/`。
2. 把本仓库全部内容（排除 `.git`/`__pycache__`/临时文件）拷到 `AI/circuit-agents/`。
3. 把本地 transformers 模型（如 `Qwen2.5-1.5B-Instruct`）拷到 `AI/models/Qwen2.5-1.5B/`
   （需含 `snapshots/master/tokenizer.json` + `model.safetensors`）。
4. 以 embeddable Python 为基底建 `AI/python-portable/`：
   - 启用 `python313._pth` 里的 `import site` 并加 `Lib/site-packages`；
   - `pip install --target Lib/site-packages torch transformers tokenizers fastapi uvicorn`
     （走国内源如 `https://pypi.tuna.tsinghua.edu.cn/simple`，约 1GB）。
5. 把本仓库的 `launch.py`（桥接版启动器）放在 `AI/launch.py`。
6. （可选，Ollama 路线）把 `circuit-agents/portable_launch.py` 拷为 `AI/launch_ollama.py`，
   等你取到 `ollama.exe` 放入 `AI/ollama/` 后即可改用 Ollama 引擎。

---

## 4. 启动（桥接版）

插上 U 盘，进 `AI/` 目录：

```bash
# 干跑自检：只校验路径/环境，不启动进程（推荐先跑一次）
python-portable\python.exe launch.py --check

# 正式启动：起 local_llm_bridge（加载 U盘模型）+ 起 circuit-agents server
python-portable\python.exe launch.py
```

- 启动器会：设模型路径 → 起 `local_llm_bridge.py`（127.0.0.1:8000，加载 U盘 1.5B）→
  起 `circuit-agents/server.py`（默认 127.0.0.1:8765）。
- 真实模型推理走桥 + `examples/local_model_demo.py` / `run.py --backend local`；
  控制台 8765 的 `/run` 端点默认是确定性模拟器（SimBackend），用于可视化与开发。
- **关闭**：`Ctrl+C`，启动器会终止桥与 server，本机不留残留。
- 常用参数：`--model-path`（模型目录）、`--bridge-port`（桥端口，默认 8000）、
  `--server-port`（server 端口，默认 8765）、`--python`（指定 Python）。

> 没有便携 Python 且目标电脑已装 torch+依赖时，`launch.py` 会用 `sys.executable` 直接跑。

---

## 4b. 实测记录（2026-08-07，U 盘 E:）

- 用 U盘 `python-portable\python.exe` 起桥加载 U盘 `models/Qwen2.5-1.5B`：约 24s 就绪。
- `examples/local_model_demo.py` 端到端跑通真实推理：
  `[r1](REAL-LLM) ok=True q=0.700 :: '今天的天气很好，但项目进展缓慢…'`
  （backend stats：calls=1 successes=1，CPU 推理约 44s）。
- `server.py --selftest`：S1–S30 全量通过（**注：冷盘首次跑 S4 可能因 USB 慢读超 15s 超时，
  系统文件缓存热后稳定通过**，属 dev 自检时序问题，不影响真实推理使用）。

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

## 6. 本机 transformers 桥接（无 Ollama 也能用本地模型）✅ 已真机验证

如果你的本地模型是 **纯 transformers / modelscope 部署**（不是 Ollama，无 OpenAI 服务），
用本仓库的 `local_llm_bridge.py` 在模型和 circuit-agents 之间架一层 **OpenAI 兼容 HTTP 桥**，
即可让 `OllamaBackend(openai 模式)` 驱动它。**已在本机用 Qwen2.5-1.5B（CPU）实测端到端跑通。**

> 适用场景：本机已用 `torch`+`transformers`+`modelscope` 装好一个模型（如 `Qwen/Qwen2.5-1.5B-Instruct`），
> 但不想/不能装 Ollama。U 盘方案仍是主路径；这是「没有 Ollama 也能测真模型」的备选。

### 6.1 原理
```
circuit-agents (OllamaBackend, api_mode="openai")
      │  POST /v1/chat/completions   (忽略 model 字段)
      ▼
local_llm_bridge.py  (stdlib http.server，跑在装了 torch 的 venv)
      │  加载本地模型，用 chat 模板生成
      ▼
你的 transformers 模型（CPU / GPU 均可，零 API 费用、零联网）
```
桥**忽略请求里的 `model` 字段**，永远用启动时加载的模型生成——所以 circuit-agents 把
`small/large/tool/code` 都映射成同一个占位名也能跑。

### 6.2 两步跑通
```bash
# ① 在装有 torch/transformers/modelscope 的 venv 里起桥（默认 127.0.0.1:8000）
python local_llm_bridge.py --offline
#   --model-path 默认指向 ~/llm/models/models/Qwen--Qwen2.5-1.5B-Instruct
#   （modelscope 缓存布局，桥会自动下钻 snapshots/<hash>/ 找到 tokenizer.json）
#   --offline 禁止任何联网；--host/--port 可改监听地址

# ② 在 circuit-agents 自己的 venv 里（另一个终端）：
python examples/local_model_demo.py        # 最小拓扑 src→resistor(真模型)→adc，打印模型产出
# 或对任意 spec：
python run.py examples/xxx.json --backend local
```

### 6.3 环境注意（本机踩过的坑）
- 需要 **fast 分词**（`tokenizers` 库，已随 transformers 装好）+ 模型目录里的 `tokenizer.json`。
  **不需要** `sentencepiece` / `tiktoken`——本机装过 cp313 版的这俩轮子反而让 slow 分词路径
  segfault，卸载后走 fast 分词即正常。
- 模型目录若是 modelscope 缓存（顶层只有 `snapshots/`），`local_llm_bridge.py` 已自动下钻到
  `snapshots/<hash>/`；直接传快照目录也行。
- `OllamaBackend` 的 `timeout` 默认 120s；CPU 上 1.5B 模型单轮约 14–24s，重任务可调大
  （`run.py --backend local` 已设 180s，`examples/local_model_demo.py` 可 `--timeout`）。

---

## 6b. Ollama 真实后端（本机已跑通 ✅ 2026-08-07）

`OllamaBackend` 已在本机跑通**真实本地推理**，不再只是纸面方案。

### 6b.1 已落地的通路
```
circuit-agents (OllamaBackend, native API)
      │  POST http://127.0.0.1:11434/api/chat
      ▼
ollama serve (便携解压版 ollama.exe，无需安装)
      │  加载 GGUF
      ▼
qwen2.5:7b (Q4_K_M, 4.68GB)
```

- **ollama.exe 来源**：`ollama-windows-amd64.zip`（1.46GB）解压出 `ollama.exe`（35.5MB）+ `lib/`，
  **免安装、可放 U 盘**。本机受限网络下靠 **断点续传** 才下满（直连会反复重置）。
- **模型注册**：不重复占盘，直接用 Modelfile 指向已有 GGUF：
  ```
  FROM E:\AI\models\Qwen2.5-7B-GGUF\qwen2.5-7b-instruct-q4_k_m.gguf
  ```
  ```bash
  set OLLAMA_MODELS=C:\path\to\models   # 建议指到 C 盘，避免 U 盘慢读
  ollama.exe serve                       # 监听 127.0.0.1:11434
  ollama.exe create qwen2.5:7b -f Modelfile
  ```
- **零代码改动**：`DEFAULT_OLLAMA_MODELS` 里 `small`/`tool` 已映射 `qwen2.5:7b`，
  注册成这个名字就直接对上。
- **探活**：`curl http://127.0.0.1:11434/api/tags`，或 `OllamaBackend.health_check() -> (ok, detail)`。
- **成本**：本地推理 `total_cost = 0`（S28 自检已断言）。Ollama 不可达时自动降级 `SimBackend`，不会崩。

### 6b.2 质量语义的坑（重要）
`OllamaBackend.run()` 默认用 **tier_cap 先验**当质量（`small=0.70`），
这会**把本地 7B 的真实输出质量压死在 0.70**，导致任何优化都过不了 0.8 质量门。

真实本地模型场景请改用**内容打分**：

```python
from mentor import default_content_quality, rerun_student, make_ollama_student
be = make_ollama_student()               # 探活失败返回 None
rr = rerun_student(spec, be)             # 拿各 resistor 的真实输出
q  = default_content_quality(spec, rr["outputs"])   # 从真实文本估质量
```

`default_content_quality` **只统计 resistor（模型推理）节点**——`power`/`source`/`adc`
的输出是电路语义值（如 `"0.92"`、节点标签），计入会稀释真实内容质量。

---

## 6c. 导师-学生训练电路（Phase 3，已真机端到端 ✅）

用**强云端导师**优化**弱本地学生**的**外部电路结构**（提示词/拓扑/模型选型）——
区别于知识蒸馏：**不动权重、零数据、零算力**。

```
失败案例(execution_store) → 导师(deepseek-reasoner)分析 → 结构化优化方案 JSON
   → apply_optimization(深拷贝，原 spec 不改) → 学生(本地 qwen2.5:7b)重跑
   → 质量门(≥阈值 且 优于原质量) → 通过则固化为可复用模板
```

### 6c.1 配置
| 项 | 环境变量 | 默认 |
|---|---|---|
| 导师模型 | `MENTOR_MODEL` | `deepseek-reasoner` |
| 导师 base | `MENTOR_BASE` | `https://api.deepseek.com` |
| 导师 key | `DEEPSEEK_API_KEY` | — |
| 学生地址 | `OLLAMA_HOST` | `http://127.0.0.1:11434` |
| 学生模型 | `OLLAMA_STUDENT_MODEL` | `qwen2.5:7b` |

### 6c.2 跑法
```bash
# 实景（真导师 + 真本地学生）
python _live_mentor_deepseek.py

# HTTP 端点
POST /mentor/train    {"quality_threshold":0.8,"use_local_student":true}
GET  /mentor/registry  # 已固化的训练成果模板
```

### 6c.3 真机实测（2026-08-07）
任务：*从季度经营简报抽取关键指标并生成结论摘要*（原质量 0.28，失败节点 `ext`/`sum`）

- 导师（DeepSeek-R1，41s）诊断：`ext/sum 均用 small 档，对财报术语和数值语境理解不足`
- 方案：`ext`/`sum` 升 `large` + 注入专业角色提示词 + **`insert_after ext` 插入「指标完整性校验」节点**
- 学生（本地 7B）重跑：节点 5→6，真实输出如
  `[ext] 抽取关键指标：营收、净利润、同比增长率、环比增长率。`
  `[指标完整性校验] 检查关键指标是否完整：营收 有 / 净利润 有 …`
- 结果：**质量 0.28 → 1.0，质量门通过，固化 1 条模板**

### 6c.4 π 心跳自动触发
π 的十进制数字 `digit == 9`（`MENTOR_TRIGGER_DIGIT`，可用环境变量改）时，
心跳自动拉起一次训练闭环；无 store / 无失败案例 / mentor 不可用时**静默降级**，
绝不拖崩宿主。通过质量门的方案会回灌 `TopologyMemory`，供后续 explore/simplify 复用。

```
π: 3 1 4 1 5 9 2 6 5 3 5 8 ...
       ↑       ↑
    explore  mentor(训练)
```

---

## 7. 注意事项

- 目标电脑内存 **≥16GB**（14b 模型推理约占 10G+，7b 约 5G；同机只跑一个模型更稳）。
- 启动器默认监听 `127.0.0.1`，仅本机访问；如需局域网共享，改 `--host 0.0.0.0`（注意安全风险）。
- Ollama 模型文件很大，**第一次**建议在在家有网时预拉好，现场用 `--no-pull` 启动。
- 拔盘前务必 `Ctrl+C` 关闭启动器，否则 Ollama 进程可能仍占用 U 盘文件。
- 便携 Python 已定为 **embeddable 3.13 + 装 torch/transformers/fastapi/uvicorn**（桥接版必需，
  模型推理与 server 都在 U盘 Python 内跑，目标电脑无需预装任何东西）。server 模式必须能 `import fastapi`。
