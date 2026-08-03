# 验证结果 — Circuit DSL 能否预测系统行为？

对 4 个示例拓扑各跑 400 次（seed=42）取均值。结论：**这套隐喻确实能  
预测延迟与质量的大致走向**，并验证了"生成器 vs 变换器"这一关键语义校准点。

## 指标对比（400 runs 均值）

| 拓扑          | 延迟(ms)   | 成本($) | 最终质量      | 达标率   | 关键验证                       |
| ----------- | -------- | ----- | --------- | ----- | -------------------------- |
| series 串联   | **1990** | 0.025 | **0.540** | 1.000 | 延迟=各段之和；质量被最弱环(r1=0.70)限速  |
| parallel 并联 | **1620** | 0.030 | **0.985** | 1.000 | 延迟=最慢支路(1500)；质量=最优支路      |
| feedback 反馈 | 2071     | 0.040 | **0.919** | 0.998 | 重试把质量从~0.54拉到0.92，代价是成本↑   |
| bridge 整流桥  | 1800     | 0.033 | **0.716** | 1.000 | 短板(图像0.80)现已传导：core 再生也救不回 |

## 预测逐条核对

### ✅ 并联延迟 = 最慢支路（最核心的一条）

parallel 的层延迟精确等于 `opamp(50) + max(a,b,c)=1500 + merge(30) + diode(40) = 1620`，  
与实测 **1620** 完全一致。串联 series 是 `50+200+1500+200+40 = 1990`，实测 1990。  
并行比串联快 370ms，正是省掉了"依次流过大型电阻"的串行等待。

### ✅ 并联质量 = 最优支路（best-of-breed）

parallel 终质量是 merge 取 max → 工具模型(0.99) 兜底，得 0.981；  
series 终质量被任意一段开路拖累（`0.95×0.90×0.95≈0.81` 全活概率，叠加噪声→0.657）。  
并联在质量上碾压串联，符合直觉。

### ✅ 反馈环提升质量，但烧钱

feedback 靠 watchdog（max_iter=3）重试 rescue 失败支路，终质量 0.918（vs 同结构无重试约 0.66），  
但成本和延迟都高于纯串联——正是"'settle time = 成本'、必须配看门狗"那句校准的量化佐证。  
达标率 0.998 意味着仍有 ~0.2% 在 3 次重试后仍失败（看门狗触发）。

### ✅ 整流桥的"短板效应"现已传导（修正后复验）

把 `resistor` 改为**变换器**语义（`output = min(input, capability)`）后，bridge 终质量从 0.823 → **0.716**，  
主要由图像源 0.80 的短板经 `core` 传导下来（`0.80 × core良率0.90 ≈ 0.72`）。说明：  
**电阻是生成器还是变换器，直接决定"短板"能否传导**——这正是当初校准点的量化佐证，而非空话。

## 结论

- 拓扑层面的预测（并行省延迟、并联提质量、反馈提质量换成本）全部成立。
- 元件层面的"生成 vs 变换"语义已修正为变换器（`min(input, capability)`），桥式短板现已真实生效；后续可加 `recovery` 系数让强 Agent 能部分挽救弱输入，而非纯透传。
- DSL + runtime 已具备作为"布局布线器"前身的雏形：既能描述拓扑，又能跑出可比指标。

---

## 勘误（2026-08-01 · 开路语义修复）

上表数值为 **修复前** 测得。M0 端到端测试暴露 `runtime.py` 电阻分支的一个边界 bug：  
上游电阻 yield 失败（开路）时，下游仍执行 `min(0,cap)+uniform(-0.03,0.03)`，噪声把 0 顶成  
`≈0.02` 的正信号向后传播，导致"开路"被伪装成"微弱可用信号"。

**修复**：在电阻分支先判 `if inp <= 0.0: return 开路`（不再叠加噪声）；仅在 `inp>0` 且  
`rng < yld` 时才产出 `ok=True`。修复后：

- 开路严格归零，`chain_ok_rate == 四电阻全活率`（≈0.778），不再有幽灵正信号；
- 全活时质量上限回到应有的 ~0.92（修复前被个别 0.02 噪声污染读数）。

**对上表的影响**：定性结论（并行省延迟、并联提质量、反馈提质量换成本、桥式短板传导）  
全部不变；仅"串联/桥式在出现 yield 失败轮次"的最终质量均值会略微下移（失败轮次现在严格计 0，  
而非 ~0.02），属边际修正，不改变任何判断。后续若重测，建议统一在修复后版本上跑。

---

## 编译器落地结果（M0–M2 · 2026-08-01）

端到端 demo（`compiler/demo.py`，每例 300 runs，seed=7）证明"目标 → 拓扑 → 仿真"  
三段流水线可用，并锁定 M2 的核心论点：**布线本身决定系统级延迟，runtime 一行未改**。

### 走查目标（COMPILER.md §9）

"总结一篇 PDF 并核对里面的数字" — `capabilities=[retrieve,reason,calculate,verify]`，  
`constraints={max_latency_ms:2000, max_cost:0.05, min_quality:0.85}`，`modalities=[pdf,table]`，`reliability=high`。

### 四阶段对比

| 阶段                 | 选型         | 布线           | 延迟(ms)   | 成本($)     | 质量(均值)    | 全活率       | 约束                |
| ------------------ | ---------- | ------------ | -------- | --------- | --------- | --------- | ----------------- |
| A 基线 small         | 默认         | 串联           | 1150     | 0.019     | 0.539     | 79%       | 质量✗               |
| B 指定 large/tool    | 手填         | 串联           | 4950     | 0.065     | 0.712     | 78%       | 成本✗ 延迟✗           |
| C Binder 自动        | tool×4     | 串联           | **3550** | 0.035     | 0.879     | 93%       | 延迟✗               |
| D Router 全并联       | tool×4     | **并联**       | **1180** | 0.036     | **0.967** | 93%       | **全过 ✓**          |
| E 全并联+反馈环          | tool×4     | 并联+retry×3   | 1270     | 0.039     | 0.968     | **100%**  | **全过 ✓**          |
| F 全并联+冗余K=2        | tool×8     | 并联+any汇合     | 1210     | 0.060     | 0.973     | **99.7%** | 成本✗               |
| **G M3 Optimizer** | tool×4     | **并联(自动搜得)** | **1180** | **0.036** | 0.967     | 92.5%     | **全过 ✓·min-cost** |
| H0 无recovery(η=0)  | small→tool | 串联           | 1280     | 0.019     | 0.648     | 92.3%     | 质量✗               |
| H1 recovery(η=0.5) | small→tool | 串联           | 1280     | 0.019     | **0.781** | 92.3%     | **质量✓**           |

### M2 核心结论

- **延迟从 3550 → 1180ms**：C 串联 4 个 tool 电阻（各 800ms，延迟求和）；D 用 Router 把四步  
  放进同一层并联（`dependencies=[]` 声明互不依赖），延迟取同层 max=800，加前缀(100)+adc(200)  
  ≈1180。**runtime.py 的 layer 化 `propagate()` 早已支持同层并行，只是此前网表是纯链，能力休眠。**
- **质量从 0.879 → 0.967**：并联 max 汇合让"单支路开路不再清零产出"——D 的 `out_rate≈100%`  
  而 `all_fired_rate≈93%`（串联时二者相等）。这是标准单元#2"质量=最优支路"的真实收益，  
  不是模拟漏洞（电容汇合取 max 是既定语义，已确认）。
- **三项约束首次全过**：延迟≤2000、成本≤0.05、质量≥0.85 在 D 同时成立。M0–M1 始终卡在"质量达标延迟炸"。
- **诚实边界**：D 演示的是"结构上限"——它假设四步互不依赖。真实依赖（如 verify 依赖  
  calculate）应写成 `dependencies` 边，Router 会自动分层（跨层串联、同层并联）。反馈环(adc+watchdog)  
  与冗余(标准单元#3/#5) 是 runtime 已原生支持的标准单元，留待 M2 后续子步；最终由 M3 Optimizer  
  在 runtime 仿真上搜 Pareto 前沿。

### Goal schema 新增

`dependencies` 字段：`None`=线性串联(向后兼容)，`[]`=全并联，`[[pre,post],...]`=DAG  
（`from_dict` 含自环/环检测校验）。Router 复用 `Netlister._prefix` 保证"前缀块"单一真相源。

### 反馈环标准单元（#3）落地 + 一处关键修正

- 新增 `feedback` 字段：`{"max_iter": N}`（N≥1），`None`=无环。`Router.route()` 据此写  
  `spec["feedback"]`，**runtime 一行未改**（原生单环：`execute()` 重试整链直到 `feedback["from"]` 节点 `.ok`）。
- **关键修正（先踩坑后修正）**：初版把门控点设在**终端 adc**，结果 E 与 D 数值完全相同——  
  并联下终端 adc 只读 `max` 支路质量（≈0.95），单支路开路不会拉低它，质量门永不触发，环变空转。  
  **正确门控点 = 末级电容汇合（pmerge/lmerge）**：其 `.ok = 所有末级支路均 fired`，才能真实捕获  
  单点 yield 失败。`execute()` 检查的是节点的 `.ok` 而非质量，故门控必须取"ok 语义=全活"的汇合点。  
  修正后 E 的 `all_fired_rate` 92.67%→**100%**（样本内），代价 avg 延迟/成本仅 +8%（1180→1270ms、0.036→0.039），  
  三项约束仍全过。
- 诚实边界：runtime 仅支持**单条**反馈环，故门控=末级汇合（整链重试），无法给每个能力各包独立环  
  （那需改 runtime，违背"内核稳定"）。§9「calculate 外包反馈环」在单环约束下等价于"给整链加重试保险"，  
  行为与图示一致（不画'假局部'装饰）。反馈环是可靠性保险，冗余(标准单元#5) 仍待补。

### 冗余标准单元（#5）落地——并补一个最小 runtime 开关

- 新增 `redundancy` 字段：`{能力名: K}`（K≥1 总副本数，K≥2 即冗余），`None`=无冗余。  
  `Router.route()` 对 K≥2 的能力发出 K 个并联电阻副本，并由一个 `capacitor(mode="any")` 收口  
  （任一副本存活即 ok，一支开路不影响其余）；其余能力仍直连本层汇合（`all` 语义）。
- **runtime 改动（必要且最小，已获确认）**：`capacitor` 分支读取 `mode`——`mode=="any"` 时  
  `ok = any(s.ok)`，否则维持默认 `ok = all`。**默认行为零变化**，现有所有拓扑（串联/并联/反馈）  
  不受影响。这是给标准单元加开关，不是重写内核传播逻辑。
- **F 演示（全能力 K=2）**：`all_fired_rate` 92%→**99.7%**（单点 yield 失败被副本吸收），  
  延迟**零增加**（同层并联取 max），但成本 0.036→**0.060 突破 0.05 约束**。  
  这把"冗余不免费"钉死了：要可靠又不破预算，必须只冗余最关键的 1~2 个能力（或降 K）。
- **M2 收口**：至此五个标准单元全部就位——串联(clarifier) / 并联(capacitor) / 反馈环(adc+watchdog) /  
  桥式整流(异质模态) / 冗余(any汇合)。编译器骨架（M0 解析 → M1 选型 → M2 布局布线）完成，  
  下一步 M3 Optimizer 以 runtime 为 Evaluator，在 `{pattern × tiers × 冗余配置}` 上搜 Pareto 前沿。

### runtime 内核改动累计（诚实清单）

| 改动                             | 类型     | 影响面                                  |
| ------------------------------ | ------ | ------------------------------------ |
| 开路语义修复（2026-08-01）             | bug 修正 | 电阻无输入严格开路，杜绝噪声幽灵信号                   |
| 电容 `mode="any"` 开关（2026-08-02） | 最小扩展   | 默认 `all` 不变；仅冗余汇合用到 `any`            |
| 电阻 `recovery` 系数（2026-08-02）   | 最小扩展   | 仅当 `cap>inp`（弱但存活输入）按 η 抬升输出；开路仍严格开路 |

> 两项改动均**向后兼容**（默认行为不变），未触及 `propagate()` 分层延迟逻辑——"布线决定系统级延迟"  
> 的论点依旧成立。

---

## M3 Optimizer 落地（2026-08-02）

`compiler/optimizer.py` 实现 COMPILER.md §5 的优化闭包：**生成候选(config) → runtime.py 仿真打分  
→ 贪心/搜索改 config → 再仿**。以 `compiler/compile.py:optimize_goal()` 为总入口，demo 新增 **G 用例**。

### 设计（与 §5 同构）

- **搜索空间（4 旋钮，标准单元库剪枝，不暴力枚举）**：① `pattern`（串联/并联/DAG）  
  ② `tiers`（每能力 small/large/tool，Binder 给最小达标档，可升降）③ `redundancy`（`{cap:K}`）  
  ④ `feedback`（on/off + max_iter）。
- **Evaluator**：复用 `runtime.py`，每个候选跑 **N=200** 次仿真取均值（与 D/E/F 共用同一套种子派生，  
  指标可比）。`evaluate()` 直接吃 Router 产出的 spec dict 构造 `Circuit`，**不再落临时文件**。
- **目标函数**：`min cost`，约束 `latency≤L ∧ quality≥Q ∧ ¬watchdog_tripped`（即三项约束全过）。
- **两阶段**：① **贪心**——Binder 最小档 + 并联起手，hill-climb 修约束（升级档/加反馈/加冗余/降档省成本）；  
  ② **搜索**——枚举 `tiers(3) × pattern(2) × feedback(2) × redundancy(3)` ≈ 36 候选，收集可行解，  
  求 **Pareto 前沿**，挑 **min-cost 可行解**。

### 验证（§9 目标，G = Optimizer 自动解）

- 贪心起手即 `tiers={全 tool}, 并联, 无冗余/反馈` → 成本 0.036 / 延迟 1180 / 质量 0.967 / 全活 92.5% /  
  **可行 ✓**（与 D 同构）。
- 搜索给出 **8 个可行解**、Pareto 前沿 **8 点**；最终解 = 搜索得到的 **min-cost 可行解 = 0.036 / 1180ms**  
  （即 D 本身）。**诚实结论**：对 §9 这个宽松约束目标，"并联 + Binder(tool)"已是最小可行解，  
  优化器没有盲目堆冗余/反馈——这恰是「min cost s.t. 约束」的本意，也是它比手调更可信的地方。

### 关键产出

- `compiler/optimizer.py`：`Optimizer`（evaluate / greedy / search / optimize），含 `_better`（可行性优先）  
  与 `_pareto`（min cost / min latency / max quality 支配比较）两个纯函数。
- `compiler/compile.py`：新增 `optimize_goal(goal_dict)`，M0→M3 流水线闭环。
- `examples/generated_pdf_verify_optimized.json`：优化器产出的最终拓扑（可直接被 draw.py / runtime 消费）。
- `diagrams/pareto_front.svg`：8 个可行解的 成本-vs-延迟 散点，颜色标质量、蓝环标最终解。
- `diagrams/pdf_summarize_verify_optimized.svg`：优化解拓扑图（与 D 同构：四能力同层并联 + 末级 adc）。

### 诚实边界 / 下一步

- 优化器价值在**更紧约束**下才显出来（如把 `max_cost` 压到 0.03，或要求 `all_fired≥99%` 时它会自动  
  加反馈/选择性冗余）。§9 目标偏宽松，故 G≈D。这是演示选择，不是算法局限。
- 依赖 M3 的「真实 LLM 后端（补强#2 / M5）」才能把 `_TIERS` 估计值替换为实测值，否则 Evaluator 仍是模拟。

### 接入 circuit-planner skill + 修约束 bug（2026-08-02 15:25）

- `scripts/plan.py` 新增 `--optimize`（+ `--runs=N` / `--key-file=PATH`）开关，调用 `optimize_goal()`，  
  在 [3] 段输出 候选数/可行数/Pareto 前沿 + 推荐解（成本/延迟/质量/产出率/全交付率）。默认仍走快速确定性  
  `compile_goal`（不加 flag 行为不变），`--optimize` 为显式开启（避免每次规划都跑 ~7200 次仿真）。
- **修 bug**：`optimizer.evaluate` / `_violation` 原假设约束三键（max_latency_ms/max_cost/min_quality）  
  全存在；NL 解析出的目标常只带部分约束（如仅 `max_latency_ms`+`min_quality` 无 `max_cost`）会  
  `KeyError` 崩。改为"约束键缺失=无该限制"。复跑验证：`--optimize "总结PDF…质量90%以上,延迟不超3000ms"`  
  → 候选 36 / 可行 18 / Pareto 10，推荐解 all-tool 并联 成本 0.034 / 延迟 1110ms / 质量 0.998 / 可行 ✓。
- 用户此前提的 **补强#1（recovery 系数）** 已完成（见下方专节）。它让强 Agent 能部分挽救弱但存活的输入，  
  电阻语义更真，也拓宽了优化器的可行域——现已落地，不阻塞当前骨架。
- **未动 identity 文件**（仍待你主动提）；本次按老规矩「草图先行 + 确认」推进 M3，未改 runtime 内核。

---

## 补强#1 · recovery 系数落地（2026-08-02）

让**强 Agent 能部分挽救"弱但存活"的输入**，使电阻（变换器）语义更真实——这是早期  
RESULTS.md 结论里就预留的增强点（`output = min(input, capability)` 之外，允许强 agent 把  
弱输入往自身上限方向拉一把）。

### 设计（严守内核边界）

- **新增 `Goal.recovery` 字段**：η∈[0,1]，缺省 0 = 旧行为（无恢复），`from_dict` 校验区间。
- **语义**：电阻输出在 `rng<yld` 命中后——
  - 若 `cap > inp`（上游弱于本 agent 上限）：`q = inp + η·(cap − inp)`（仍不超 cap）；
  - 否则（含 `cap ≤ inp`）：退回 `min(inp, cap)`（旧行为）。
- **内核铁律不动**：上游 `ok=False`（开路，`inp≤0`）时仍在该分支**之前**直接返回开路——  
  recovery **绝不 revive 死输入**，延续 2026-08-01 的"开路必须保持开路"修正。
- **改动面全向后兼容**：`Netlister`/`Router` 把 `recovery` 写进每个电阻组件（缺省 0）；  
  runtime 电阻分支 `comp.get("recovery", 0.0)`，旧网表无此键 ⇒ 0 ⇒ 旧行为。

### 验证（H 用例：串联 small(弱,cap0.70) → tool(强,cap0.99)，约束 min_quality=0.75）

| 配置            | 天花板质量     | 均值质量      | all_fired | 质量约束     |
| ------------- | --------- | --------- | --------- | -------- |
| H0 η=0（透传弱输入） | 0.702     | 0.648     | 92.3%     | ✗ 不达标    |
| **H1 η=0.5**  | **0.846** | **0.781** | 92.3%     | **✓ 达标** |

- η=0.5 把"弱输入天花板"从 0.70 抬到 0.846，均值 0.648→0.781，**质量约束由 ✗ 变 ✓**——  
  证明"强 agent 挽救弱输入"成立。
- **关键诚实点**：H0/H1 的 `all_fired_rate` 完全相同（92.3%）——上游 small 一旦 yield 失败（开路），  
  下游 tool 无论 η 多少都**严格开路**。recovery 只对"弱但存活"的输入生效，死输入一律不救，  
  内核未被破坏。
- **一个实测小陷阱（已修）**：初版 `from_dict` 算了 `rec` 却漏传进 `Goal(...)` 构造器，  
  导致 `recovery` 恒为默认值 0（H1 与 H0 数值完全相同）。补上 `recovery=rec` 后恢复预期。

---

## 补强#2 · 真实 LLM 后端（= M5「接真后端」部分，2026-08-02）

把 Evaluator 从"纯模拟"推进到"真模型可插拔"。**抽象接缝本就在 runtime 就位**  
（`Backend` 基类 + `Circuit(spec, backend)` + `propagate()` 内 `self.backend.run(comp, inputs)`），  
因此这次只新增一个 `RealLLMBackend(Backend)` 子类，**传播逻辑 / 分层延迟逻辑零改动**。

### 设计（守内核 + 诚实边界）

- **新增 `compiler/backend_llm.py`：`RealLLMBackend(SimBackend)`**。
  - 继承 `SimBackend`：所有**非 resistor 组件**（power/opamp/capacitor/adc/bridge…）  
    直接复用父类确定性实现——电容器/调度器/ADC 不该、也不该用 LLM 去"跑"。
  - 仅 **resistor（原子 agent）** 走真 LLM：`拼 prompt(上游 Signal.value) →
    OpenAI-compatible `chat/completions`→ 响应 →`Signal(ok/quality/cost/lat)\`。
  - 依赖仅 stdlib `urllib`（**不装任何包**，契合"低操作成本"）；`base_url` 可配，  
    默认 OpenAI，也覆盖自托管 / 内网代理 endpoint。
- **默认不启用**：`compile` / `demo` / `optimizer` 全部仍用 `SimBackend` 做对照与回归基准；  
  只有你显式传 `RealLLMBackend` 时才走真模型。
- **开路语义延续内核**：上游全死（`inp≤0`）→ 直接返回开路，**不浪费真调用**（离线计数已验证 `calls==0`）。
- **LLM 输出质量无法被自动精确度量** —— `quality` 用 per-tier 能力上限（cap）作先验，  
  这是**已知近似**，不是模拟漏洞（真要精确需接 judge / 人工评测，留作后续）。

### 离线验证（无需 API key，`python -m compiler.backend_llm` 全过 ✓）

1. **parity**：同种子下 `RealLLMBackend` 处理全部结构件与 `SimBackend` 输出逐字段一致。
2. **dry_run**：组装正确请求但不发送——`model` 按 tier 映射（large→gpt-4o）、  
   prompt 含上游上下文、无 key 时不带 `Authorization` 头。
3. **注入假响应**：用注入式 `_http_post` 返回 canned OpenAI 响应，验证 `value` 映射、  
   `quality=cap`、`cost` 按 usage 估算。
4. **错误路径**：`_http_post` 抛 `URLError` → `ok=False, open=http_error`（与 yield_fail 同语义）。
5. **开路语义**：上游全死 → 直接开路且**未发起任何真调用**（计数校验 `calls==0`）。

### 整链集成验证（D 拓扑 + dry_run）

`Router.route(D)` → `Circuit(spec, RealLLMBackend(dry_run=True)).propagate()`：  
四步 resistor 全部正确组装 prompt（上游上下文已修复为可读文本，不再嵌套 `Signal(...)` repr），  
结构件耗时 310ms，真调用耗时由 API 实测（dry-run 记为 0）——**证明真后端无缝接入完整流水线**。

### 如何启用（你给 key 后即生效）

```python
import runtime
from compiler.backend_llm import RealLLMBackend

# 方式一：环境变量（推荐，不把明文写进代码）
#   set OPENAI_API_KEY=sk-...   （或 AGENT_API_KEY）
#   set AGENT_API_BASE=https://your-proxy/v1   # 自托管/内网代理时
spec = runtime.load("examples/generated_pdf_verify.json")
backend = RealLLMBackend()                 # 自动读 env；dry_run=True 可先干跑看 prompt
res = runtime.Circuit(spec, backend).execute()
```

### 真·在线验证（2026-08-02 跑通 ✓）

用 DeepSeek（OpenAI-compatible）实测，证明真后端在完整流水线上真正发起并消费了真 LLM 调用。

- **运行**：`DEEPSEEK_API_KEY="$(cat key_tmp.txt)" python circuit-agents/compiler/_verify_real.py`  
  （沙箱不继承本机全局环境变量，故用"文件注入"：key 只在进程内存，明文不进命令/对话）。
- **目标**：`retrieve→reason→verify`（依赖 DAG + 1 次反馈环）→ `compile_goal(route=True)` →  
  `RealLLMBackend(base_url=https://api.deepseek.com/v1, model_map:{small/tool→deepseek-chat, large→deepseek-reasoner})`  
  → `Circuit.propagate()`。
- **实测结果**：
  | 项                      | 值                                                               |
  | ---------------------- | --------------------------------------------------------------- |
  | success                | **True**                                                        |
  | 三个 resistor（cap_0/1/2） | 全部 ok=True，返回**真实 LLM 文本**（reason 产出推理段、verify 产出 Python 连通性代码） |
  | total_latency          | **9176 ms**（三次真实串行调用 933+2811+5091 + 结构件开销）                     |
  | total_cost             | 0.0173（按 DeepSeek 真实 token 用量估算）                                |
  | 编译组件数                  | 9（power/opamp/电容汇合×3/adc/反馈环）                                   |
- **关键诚实点**：`final_quality=0.7` 仍是 **small 档 cap 先验（0.70）**，**不是对真实 LLM  
  输出质量的测量**。本次三个 resistor 实际都返回了连贯、相关文本（推理 + 代码），足以证明真模型  
  确实在跑；要拿"真实质量分"仍需接 judge 模型或人工评测（与离线声明一致）。
- 沙箱**本次未屏蔽出网**，HTTPS 到 `api.deepseek.com` 成功；若以后报 `open=http_error` 网络类错误，  
  是沙箱隔离所致、非代码问题（需关沙箱重跑，须经你同意）。
- 本次 Binder 因 `min_quality=0.6` 把全链绑到 small→`deepseek-chat`；`deepseek-reasoner`(large)  
  映射已就绪但未在本 smoke 触发（要触发需把某能力显式绑 large 或收紧质量约束）。
- **2026-08-02 补跑 `--large-cap reason`**：把 reason 显式绑 large → `deepseek-reasoner` **成功触发**  
  （`cap_1` 显示 `model=deepseek-reasoner`、ok=True、耗时 4414ms 明显长于 chat 档、中间汇合 `lmerge_1`  
  质量上限抬到 0.92）。但 smoke 任务本身**无真实输入内容**，`retrieve` 步无物可检 → 上下文空 →  
  reasoner 如实返回 "no upstream context"、verify 同理（空）。这证明 reasoner 接线正确、且比 chat  
  更"严谨"；`final_quality=0.7` 是末级 verify(small cap) 的读数，非 reasoner。要看实质推理，  
  需给任务喂真实内容（见下）。
- **2026-08-02 再补 `--task "用三句话解释光合作用"`**：给 power 节点注入真实任务后整链有上下文，  
  cap_0(retrieve,chat) 输出光合作用的真实中文说明、cap_1(reason,reasoner) 产出**实质推理**  
  （"light reactions depend on light absorbed by chlorophyll…"）、cap_2(verify,chat) 确认陈述正确。  
  至此 chat + reasoner 双模型在真实任务上完整跑通，真后端接线彻底验证（final_quality=0.7 仍属  
  末级 verify 的 small cap 读数，与 reasoner 无关）。

> 本机原本未配 key；2026-08-02 已用 **DeepSeek 真·在线跑通**，离线自检 + dry_run + 在线实测  
> 三重覆盖"接线正确性"。

### 诚实边界总结

- 补强#2 解决的是"Evaluator 脱离纯模拟"——但**优化器的搜索逻辑不变**，只是把  
  `SimBackend` 换成 `RealLLMBackend` 即可在真实成本/延迟/质量上重新打分（候选仍由标准单元库剪枝）。
- 真后端的 `quality` 是 cap 先验近似；要拿真实质量需额外 judge 模型或人工反馈，属 M5 后续。
- 未动 runtime 内核（`Backend`/`Circuit`/`propagate` 原封不动）；仅新增子类 + 离线自检。

---

## M4 · NL→结构化目标（混合解析器，2026-08-01）

把"自然语言目标"接到流水线最前端：**NL → Goal**。COMPILER.md 把这一步列为最难的一环  
（"规划所需能力需要 eval 兜底"），故采用与用户确认的 **混合路线：规则兜底 + 可选 LLM 增强**。

### 设计（守住内核 + 诚实边界）

- **受控能力词表（cell library）**：`CAPABILITY_VOCAB` 把 9 个 canonical 能力  
  （retrieve/reason/calculate/verify/translate/extract/classify **+ organize/summarize**，后两者 2026-08-02 增补，覆盖「整理成表格/摘要」类结构化输出动作）映射到中英文触发词。  
  **无论规则还是 LLM，吐出的 capabilities 都限制在这张表内**，不乱造新能力——这是"eval 兜底"的第一道闸。
- **规则解析（兜底，离线可跑）** `_parse_rule`：关键词→能力（去重保序）；正则→约束  
  （`max_latency_ms` / `max_cost` / `min_quality` / `max_chars`(字数上限，独立字段)）；模态词表→`modalities`；  
  高可靠/务必/必须…→`high`，随便/宽松…→`low`。**规划启发：文档/媒体类目标（含 pdf/image…  
  ）自动补 `retrieve`**（要先读源才能处理）。兜底 `capabilities=["reason"]` 保证非空。
- **LLM 增强（opt-in）** `_parse_llm`：有 key 时走 DeepSeek（OpenAI-compatible）  
  `chat/completions` + `response_format=json_object`，按 schema 出结构化 JSON 做"能力规划"；  
  做**受控词表约束 + `Goal.from_dict` 校验**。**失败/无 key/输出非法 → 自动回退规则**（`parse()` 的 try/except）。
- **默认不触网**：整条 M0→M4 流水线离线即可演示（与 RealLLMBackend 同哲学）；只有你显式给  
  `DEEPSEEK_API_KEY` 才走 LLM 规划。
- **下游零改动**：`GoalParser.parse()` 产出标准 `Goal`，后续 `compile_goal()` / `Circuit` 完全复用。

### 新增文件

- `compiler/nl_parser.py`：`GoalParser`（`parse` / `_parse_rule` / `_parse_llm` / `_extract_json`  
  / `selftest`）+ 两张词表。仅依赖 stdlib，无外部包。
- `compiler/demo.py`：抽取 `_simulate()` / `_print_metrics()` 复用仿真循环；新增  
  `run_nl_case(nl, api_key=None, …)`（走 `compile_goal(auto_bind=True, route=True)`）+ `main()` 末尾 3 个 NL 例子。

### 验证

- **离线自检**（`python -m compiler.nl_parser`，4 项全过 ✓）：
  1. 规则解析正确性（文档类自动补 retrieve + 约束/模态/可靠性识别）；
  2. LLM 注入假响应 → 结构化 JSON 正确映射；
  3. LLM 非法响应 → 自动回退规则；
  4. 无 key → 直接规则。
- **端到端 demo**（`python -m compiler.demo`，A–H 结果不变，新增 M4 三段）实测解析：
  | NL 输入                             | 解析出 capabilities                 | constraints         | modalities  | reliability | Binder  | 延迟约束          |
  | --------------------------------- | -------------------------------- | ------------------- | ----------- | ----------- | ------- | ------------- |
  | 总结一篇PDF并核对里面的数字，要求高可靠，延迟不超过3000ms | retrieve,reason,calculate,verify | max_latency_ms:3000 | pdf         | high        | 全 small | ✓ 1200ms≤3000 |
  | 把这段英文翻译成中文                        | translate                        | —                   | —           | normal      | small   | —             |
  | 从图片里提取表格并分类，随便处理                  | retrieve,extract,classify        | —                   | image,table | low         | 全 small | —             |
  （Binder 默认取满足 `min_quality=0.8` 的档；示例未显式写 min_quality 故落到 small——  
  这是选型问题，归 M1/M3，不归 M4。M4 的职责是"把 NL 正确翻成 Goal"，已达成。）

### 诚实边界

- 规则解析是**保守近似**：遇词表里没有的新说法/新能力会漏（这正是文档说 NL→netlist 最难的原因）。  
  LLM 增强补"规则覆盖不到的新表述"，但 LLM 仍是"规划建议"，最终拓扑是否真满足目标要靠  
  M3 Optimizer + runtime 仿真/Evaluator 兜底。
- M4 只负责 **NL→Goal 这一跳**；下游 `compile_goal` / `Circuit` 不变。
- 顺序进度：补强#1 ✓ → 补强#2 ✓ → **M4 ✓**（流水线现可从纯 NL 起手）。

> 未动 runtime 内核、未动 `Backend`/`Circuit`/`propagate`；仅新增 `nl_parser.py` + 复用既有编译器。

### 能力词表补全（2026-08-02）

- 新增 `organize`（整理/编排/排版/做成表格/列表…）与 `summarize`（摘要/综述/概括…）两个 canonical 能力，  
  修复实测中「整理成表格」被漏抓、未进拓扑的短板。**不动 `reason` 词表**（总结/归纳已在其中），与现存 7 能力零冲突；  
  `python -m compiler.nl_parser` 离线自检 4 项仍全过。LLM 模式因 `_build_messages` 动态取词表而自动受益。
- 配套：`circuit-planner` skill 的 `plan.py`（`SUGGESTED_TOOL`）+ `SKILL.md`（能力→工具映射表）同步补 `organize`/`summarize`。
- 验证：`查今日抖音热点并整理成表格` → `capabilities:[retrieve, organize]` + `modalities:[table]`（执行计划第 2 步即 `[organize]`）；  
  `把调查结果整理成表格并写一段摘要` → `[retrieve, organize, summarize]` 三步骤拓扑序正确。

## circuit-planner 接入 draw.py 电路图渲染（2026-08-02 续）

- **用户指令「继续，用复杂的电路图架构做到这个技能里面」**：把 `circuit-agents/draw.py`（拓扑→示意 SVG 渲染器）接进 `circuit-planner` skill 的 `plan.py`，让规划产物多一张**可读的电路拓扑图**。
- **draw.py 能力盘点**（已读确认，`draw(spec, out_path)` 消费 spec dict）：  
  颜色按类型编码（POWER/SOURCE/OP-AMP/RESISTOR/CAPACITOR/ADC/BRIDGE/…），节点画对应元件字形  
  （电池/圆圈源/运放三角/电阻锯齿/电容双极板/ADC 方框/BRIDGE 方框）；前馈 wire 用贝塞尔+箭头；  
  **反馈环**画红色虚线 retry 环（`retry ×N` 标注，走 `spec.feedback`）；分层布局（`Circuit.layers()`  
  按依赖分层，同层横排、跨层纵向堆叠）。已覆盖复杂架构：并联扇入、桥式整流（多模态汇合）、反馈自纠错环。
- **plan.py 改动**：main() 已加 `--draw` 开关（opts 字典 + arg 解析 + `[8]` 分支）。  
  分支内 `import draw`（COMPILER_DIR 顶部已插入 sys.path ⇒ `circuit-agents/draw.py` 可解析，  
  draw.py 自身的 `from runtime import ...` 同目录也解析）；由 `goal.name`（LLM 回 name，无则由 NL 清洗兜底）  
  生成 `diagrams/<name>.svg`（写于 `scripts/diagrams/`）；SKILL.md 已注明 agent 用 `present_files` 展示。
- **端到端实测通过 ✓**：
  - 命令：`<PY> plan.py --draw "把这篇长文读一遍，挑出三个重点，然后用大白话说给外行听"`（key 在位→走 LLM 模式）。  
    产物 `scripts/diagrams/simplify_article.svg`（9093 字节）：LLM 解析 name=`simplify_article` →  
    拓扑 POWER + SOURCE(输入[text]) + OP-AMP(调度器) + 三层 CAPACITOR(汇合) + 三 RESISTOR(retrieve/summarize/translate, model=small) + ADC(质量评估 thr=0.8)。SVG 合法、元素齐全。
  - 另跑 `draw.py`（自带 4 例：`series`/`parallel`/`feedback`/`bridge_rectifier`）渲到 `circuit-agents/diagrams/`，  
    其中 `bridge-rectifier-multimodal.svg` 展示完整复杂架构：4 路 SOURCE(文本/图像/表格) 并行扇入 → BRIDGE 整流  
    → RESISTOR(统一理解, large) → ADC(评估)，颜色按类型、箭头 wire、POWER 供电，证明渲染引擎对复杂拓扑完整可用。
- **路径坑已规避**：`draw.py` 在 `circuit-agents/` 根、plan.py 在 `scripts/`，靠 `COMPILER_DIR` 提前入 sys.path  
  解决 `import draw`；无 Git Bash `/c/...` 错拼问题（本次用 `cd scripts` + 相对 `plan.py` 跑 managed python）。
- **诚实边界**：`--draw` 是"规划可视化"，渲染的是 `compile_goal`/`optimize_goal` 产出的 spec 拓扑，  
  图本身不保证运行时质量；默认**关闭**（不加 flag 不变），加 `--draw` 才渲。若用户要"每次都出图"可改默认。
- 更新本小节 + 追加记忆；SKILL.md 的 `--draw` 文档已于上一轮补好（无需再改）。

## circuit-planner 闭环执行模式（--execute，2026-08-02 收口）

- **用户指令「继续优化技能」→ AskUserQuestion 选「闭环执行模式（推荐）」→ 用户确认「对」**：让 skill 从"只规划"升级为"规划 + 出运行 book + agent 当运行时真实执行"。
- **架构硬约束（已向用户讲清并确认）**：`plan.py` 是跑在 Bash 沙箱的 Python 脚本，**调不动 WorkBuddy 原生工具**（WebFetch/Read/Write/Bash 在我这边）。故"执行模式"真实形态 = `plan.py` 产 **runbook（人读 [9] + 机读 JSON）→ 我（agent）按拓扑序用对应工具真实执行、串上下文、并行同发、反馈环整链重试 → 产出交付物**。延续"circuit-agents=规划内核，WorkBuddy=运行时"既定架构。
- **plan.py 改动**：
  - 加 `--execute` 开关（opts + arg 解析 + 帮助文案）；main() 末尾 `[9]` 分支调 `_runbook(spec, goal.name)` 打印执行 runbook 并落 `runbooks/<name>_runbook.json`（写于 `scripts/runbooks/`）。
  - 新增 `_layers(spec)`（按最长路径分层，同层=可并行）与 `_runbook(spec, goal_name)`：遍历拓扑序电阻步骤，逐步给 序号/能力/建议工具(tier)/输入上下文/产出/层号/并行步骤；**`input_context` 穿过电容/opamp 缓冲，解析到真正产出数据的上游 resistor/source**（标注"步骤N [cap] 产出"或"原始任务输入(源)"），避免只显示"汇合上下文(某电容)"这种不可执行描述。
  - 全局字段：反馈环（`feedback.max_iter` → 整链重试策略）、质量门（`adc` 节点列表）。
- **SKILL.md 改动**：开关列表补 `--execute`；"执行"段重写为「闭环运行模式」协议（照 runbook 串上下文、并行同发、质量门自检、反馈环重试、present_files 交付物、诚实边界）。
- **实测通过 ✓**：
  - `plan.py --execute "查今日抖音热点并整理成表格"` → runbook：步骤1[retrieve]吃原始输入、步骤2[organize]吃步骤1产出、质量门=['adc']、无反馈环；JSON 落盘 `scripts/runbooks/hot_topics_table_runbook.json`。
  - **live-demo 闭环**：我作为运行时照 runbook 真实执行——步骤1 WebSearch 多源检索今日抖音热点（5 条：美丽明天首播/2026版西游草根爆款/七彩祥云治愈系/安嘉和口碑反转/8月2日运势），步骤2 组织成表格 `Write` 落盘 `douyin_hotspots_2026-08-02.md`（含序号/话题/简介/热度/来源/日期/链接+观察）。规划→出图→执行→交付 全链路打通。
- **诚实边界**：runbook 是"规划建议+接线图"，执行质量取决于所用工具/模型；脚本不保证运行时成功。`--execute` 默认关闭，加 flag 才出 runbook；`--draw`/`--execute` 可组合（同时出图+runbook）。
- 待用户决策：① 是否把 `--draw`/`--execute` 设默认；② 是否扩展 runbook 支持"带实际工具调用指令"的更强执行器（需另起 agent 循环，非脚本内）；③ identity 文件仍待用户主动提；④ 桌面 `key_tmp.txt` 用完删（我无法代删）。

## circuit-planner 自驱动执行循环（exec_loop.py，2026-08-02 收口）

- **用户指令「继续优化技能」→ AskUserQuestion 选「闭环执行模式（推荐）」→ 确认「1」（自驱动执行循环方向）**。把"照 runbook 手动执行"升级成"机械循环驱动"：agent 不必每步重新理解 runbook，驱动器算出下一步指令，agent 照做后回写，循环推进。
- **新增 `scripts/exec_loop.py`**（纯 stdlib，~150 行）：吃 `plan.py --execute` 产出的 `runbooks/<name>_runbook.json`，维护状态（`runbooks/<name>_state.json`：已完成步骤+各步产出摘要 / gate / iter / status / feedback_max_iter）。
  - 每次调用吐出下一步 **action directive**（JSON）：`execute_step`（含 capability/tool/tier/input_context[已串好上游产物]/parallel_with/produces/hint）、`quality_check`（质量门自检）、`retry_chain`（整链重试）、`done`/`failed`。
  - `--record="STEP:TEXT"` 回写某步产出；`--record="gate:pass|fail"` 回写质量门结论；`--reset` 清空状态。
  - `next_directive()` 状态机：按拓扑序推进 → 全步完成进质量门 → gate:pass→done；gate:fail 且含 `feedback(max_iter=N)` → iter++、清空产物、整链重试，超过 N → failed（无反馈环则不自动重试）。
  - **边界**：脚本只做调度+状态管理，**不调真实工具**；真实工具调用仍在 agent 侧（既定架构，不碰 WorkBuddy 内核）。
- **SKILL.md 改动**：工作流程加「步骤3 自驱动执行循环（exec_loop.py）」，给出 `--reset/--record` 用法与质量门/反馈环语义。
- **实测通过 ✓**：
  - 基本流转（`hot_topics_table_runbook.json`）：reset→步骤1(吃原始输入)→记录→步骤2(input_context 自动串"步骤1[retrieve]产出")→记录→quality_check(无反馈环不重试)→gate:pass→done。证明上下文串接 + 分步驱动正确。
  - 反馈环重试（演示 runbook `_demo_retry_runbook.json`，feedback max_iter=2）：两步跑完→quality_check→`gate:fail`→`retry_chain`(iter 2,刷新上下文从步骤1重跑)→重跑两步→`gate:pass`→done。证明整链重试策略正确。
  - 复杂任务规划：`plan.py --execute --draw "总结一篇PDF并核对里面的数字，要求高可靠"` → 解析 [retrieve,reason,calculate,verify]+pdf+high，4 电阻链 + adc 质量门，runbook + SVG 落盘。
  - **重要发现**：编译器目前 `feedback` 是**显式字段**（NL 解析只把"高可靠"映射到 `reliability=high`，**不自动接反馈环**），故该高可靠任务 runbook 的 `feedback=null`。要真实看到反馈环拓扑/重试，需目标显式带 feedback（或未来增强解析器把高可靠→自动接反馈环，属编译器规划层改动，不在本次执行器范围内）。
- **circuit-planner 现四块增强全部就位**：能力词表(organize/summarize) + LLM 增强 + --optimize(M3) + --draw(出图) + --execute(出 runbook) + 自驱动执行循环(exec_loop)。规划→出图→出 runbook→机械驱动执行→交付 全闭环。
- 待决策：① --draw/--execute/exec_loop 是否默认开启；③ identity 仍待提；④ key_tmp.txt 用完删。  
  （② 已落地：见下「高可靠语义补全」专节。）

## 高可靠语义补全（M4 增强 · 2026-08-02 续，收口）

把上文「自驱动执行循环」段里挂着的待决策 **② 是否增强编译器让"高可靠"自动接反馈环** 落地。  
`exec_loop` 段曾记录一个"重要发现"：编译器把 `feedback` 当作**显式字段**，NL 解析只把"高可靠"  
映射到 `reliability=high`，**不会自动接反馈环**，导致真实高可靠任务（如「总结一篇PDF并核对里面的  
数字，要求高可靠」）runbook 的 `feedback=null`——图与重试策略都看不到环。本次在 **M4 规划层**  
补上这一语义缺口。

### 改动（`compiler/nl_parser.py`）

`parse()` 在解析完成后追加一段语义补全（位于 try/except 之外，规则与 LLM 两条路径都受益）：

\`\`\`python

# 「高可靠」语义补全：要求高可靠 ⇒ 自动接一条反馈环（整链重试）作可靠性保险，

# 除非目标已显式声明 feedback（尊重显式意图，不覆盖）。

if g.reliability == "high" and not g.feedback:  
g.feedback = {"max_iter": 3}  
\`\`\`

- **语义**：说"高可靠 / 务必 / 必须 / 严格 / 关键" ⇒ 自动接一条 **整链重试**反馈环（max_iter=3）作可靠性保险。
- **尊重显式意图**：若目标已显式声明 `feedback`（LLM / 手工 Goal），不覆盖、不重复接——只补"隐式高可靠但没明说环"的缺口。
- **下游零改动**：`Router.route()` 本就 `if goal.feedback:` 写 `spec["feedback"]`（门控点=末级汇合 `pmerge`，runtime 原生单环）；`plan.py` 的 `--draw`/`--execute` 也本就读取 `spec.feedback` / `feedback`。M4 这唯一一处改动，让"高可靠"自然流到拓扑与 SVG，无需动编译器布线器或渲染器。
- `selftest()` case 1 增断言 `assert g.feedback and g.feedback.get("max_iter") == 3`，离线自检 **5 项全过**。

### 端到端验证通过 ✓

`plan.py --execute --draw "总结一篇PDF并核对里面的数字，要求高可靠"`：

- 解析：`capabilities=[retrieve,reason,calculate,verify]` + `modalities=[pdf]` + `reliability=high`；NL 未显式带 feedback ⇒ 自动补 `feedback={"max_iter":3}`。
- `spec["feedback"] = {"from":"pmerge","to":"cmerge","max_iter":3}`（末级汇合门控整链重试）。
- runbook：`反馈环=有(max_iter=3)`、质量门=`['adc']`、重试策略文案正确（from=pmerge→to=cmerge，整链重试最多 3 次）。
- SVG：`scripts/diagrams/pdf_check.svg`（10926 字节）含红色虚线 retry 环——`stroke="#c0392b" stroke-dasharray="6 4" marker-end="arrowF"` + 红字 `retry ×3` 标注。高可靠任务终于在图与 runbook 上都能看到整链重试闭环。

### 诚实边界

- 这是**规划层语义补全**，不是新增 runtime 能力——反馈环（标准单元#3）此前已落地并由 `Router` 接线；本次只是让"说'高可靠'"与"真的有重试环"对齐，消除规划层与拓扑之间的语义断层。
- `max_iter=3` 是合理默认（与 §9 E 用例一致）；若日后要按任务风险动态调环数，可在 `parse()` 内据约束/模态细化，属后续增强、非必须。
- 仍属 **M4 范围**（NL→Goal 这一跳），未动 runtime 内核、未动 `Backend/Circuit/propagate`、未动 `draw.py`、未动 `Router`。

## circuit-planner 收口批次：默认开关 + 更强执行器 + 速查（2026-08-02 末尾）

用户「继续」→「1234」= 一次性收口四件待办：① exec_loop 真实重试演示；② `--draw`/`--execute` 改默认开；③ 更强执行器（诚实版）；④ 收尾（速查卡 + 待决策整理）。

### ① exec_loop 真实重试演示（证明"自动接环"真的能触发重试）

用刚生成、现已带 `feedback(max_iter=3)` 的 `pdf_check_runbook.json` 跑通整链：  
`--reset` → 记录4步 → `--record="gate:fail"` → 下一步指令变 **`retry_chain`（iter 2/3，上下文清空重跑）** → 第2轮记录4步 → `gate:pass` → `done`。  
**关键对照**：修复前该 runbook 的 `feedback=null`，同样 `gate:fail` 会直接落 `failed`；现在因为 M4 把"高可靠"自动接环，重试闭环**真触发**——规划→出 runbook→执行器 全链路打通的最后一公里证据。

### ② --draw / --execute 改默认开（2026-08-02 末尾）

- `plan.py`：`opts` 默认 `draw=True, execute=True`；新增 `--no-draw` / `--no-execute` 可单独关闭；帮助文案同步更新（注明"默认开启 + --no-* 关"）。
- 实测：不加任何 flag 跑 `plan.py "翻译这段英文"` → 自动落 `diagrams/translate_text.svg` + `runbooks/translate_text_runbook.json`（\[8\]\[9] 段均输出）。
- 影响：每次规划默认顺带出图 + runbook，省去手动加 flag；纯规划不想落盘时用 `--no-*`。
- SKILL.md 同步：开关注释、[8]/执行段、速查卡全部改为"默认开 + --no-* 关"。

### ③ 更强执行器（诚实版，2026-08-02 末尾）

- **架构铁律（重申）**：`exec_loop.py` 跑在 Bash 沙箱，**调不动 WorkBuddy 原生工具**（Read/WebFetch/Write/Bash 是 agent 运行时才有）；真·工具调用只能由 agent 侧完成。故"更强执行器"不可能是"脚本自主跑工具"——这是既定架构，不碰 WorkBuddy 内核。
- **现实形态**：给 agent 的机械循环降摩擦——`exec_loop` 每次除 JSON 指令外，额外打印一条**可直接回贴的下一步命令**：  
  `execute_step` → `python exec_loop.py <rb> --record="<step>:<产出>"`；`quality_check` → `gate:pass`/`gate:fail` 二选一。agent 照贴即推进，不必手工拼 `--record`；`retry_chain`/`done`/`failed` 也各印一句收尾提示。
- 实测：reset→首步指令带 prefilled `--record="1:..."`；记录 step1（单步任务）后即印 `gate:pass`/`gate:fail` 两条命令。
- SKILL.md 在 exec_loop 段补「更强执行器（零摩擦自驱动）」说明，并把"脚本只调度、不调真实工具"诚实边界写清。

### ④ 收尾：速查卡 + 待决策整理

- SKILL.md 末尾新增「速查卡（一口背下）」：规划 / 执行 / 自驱动 / 重试 / 诚实边界 五条随身记。
- **待决策仅剩**（①②已落地、高可靠自动接环已落地）：
  - ③ **identity 文件**（SOUL/IDENTITY/USER/BOOTSTRAP）仍待用户主动提——未动。
  - ④ **桌面 `key_tmp.txt` 删除**：属个人目录文件删除，按个人文件安全规则需用户再明确确认一次（或用户手动删）；我不代删。

## 真并行执行（A+B+C · 2026-08-02 续）

用户路线第一层「真正并行执行」= A(exec_loop 真并行吐出) + B(规划侧出并联分层) + C(同能力多实例)。本次落地并端到端验证。

### A — exec_loop 真并行吐出（脚本侧）

`next_directive()` 改为：收集所有未完成步骤，按 `layer` 分组，**总是先处理最早一层**；若该层 ≥2 步则一次性吐 `action:"parallel"`，把整层每步的 directive（capability/tool/input_context）成批列出并提示 agent 并发执行；单步层回退 `execute_step`。底部便利命令对 `parallel` 自动拼好 `python exec_loop.py <rb> --record="1:.." --record="2:.."` 一次性回写整层。

- 正确性护栏：只吐"最早未完成层"的 frontier，下游层（如依赖并行检索结果的 reason）不会提前并发，避免拿到空输入。

### B — 规划侧并联分层 DAG（编译器侧，M4+nl_parser）

`_parse_rule` 新增并行意图识别：目标含「并行/同时/分别/各自/并发/同步/各/一并/都」→ 进入"并联优先分层 DAG"：

- source 能力(retrieve/extract) 并联在首层；sink 能力(reason/summarize/…) 依赖全部 source（sink 之间仍并联）。
- 纯 source 或纯 sink 且无分层 → 退化全并联(dependencies=[])。
- 多重集(Step C)：**按并列对象数量**拆 source 为多个并联实例（retrieve→retrieve#2… 上限6）。先识别显式数量词（搜4个/搜四个），否则取检索动词后、到下一个 sink 动词/连接词之间的并列块，按分隔符切分出并列项数（取最多一段）。修正前按"触发词出现次数+顿号 bump"算，导致"同时搜四个榜单 A、B、C、D"只拆成 2 路而非 4 路。
- 关键 bug 修复：扩展名 `retrieve#2` 带 `#\d+` 后缀，归类 source/sink 必须按**基础名**判定，否则被误判为 sink 反被 retrieve 喂 → 多出错误边、分层错乱。已用 `re.sub(r"#\d+$","",c)` 修正。

### C — 同能力多实例（由 B 多重集覆盖）

"分别检索 A 和 B" → capabilities=[retrieve, retrieve#2]，全并联；runbook 两 retrieve 步骤 `parallel_with` 互指。plan.py 的 `SUGGESTED_TOOL` 查表增加 `_tool_for()` 基础名归一，让 `retrieve#2` 也能正确映射到工具提示（顺带让冗余单元 `#r` 标签受益）。

### 端到端验证（已跑通）

目标："并行检索2024年新能源汽车销量和政策，然后总结"

- `Goal`: caps=[retrieve, retrieve#2, reason], deps=[['retrieve','reason'],['retrieve#2','reason']] → 两 retrieve 并联 → reason 串后。
- runbook：步骤1/2 互 `parallel_with`，步骤3 输入上下文正确汇入两者产出。
- exec_loop：`--reset`→`parallel`(步骤1+2 成批) → record 1+2 → `execute_step`(步骤3, input_context 含两检索产出) → record 3 → `quality_check` → `gate:pass` → `done`。全链路闭环。
- `nl_parser` 离线 selftest 仍全绿（并行正则不误命中现有用例）。

### 诚实边界（重申）

- exec_loop 仍只做调度+状态管理，不调 WorkBuddy 原生工具；真并行执行靠 agent 运行时一次发多个工具调用兑现（如多条 WebFetch）。脚本只"算清楚该并发哪些步"并成批吐指令。
- 规则解析是保守近似：并行意图靠关键词触发；多重集靠检索词计数/并列连词启发，复杂指代（"A和B"具体是什么）靠 agent 读原始 NL 推断，未做深解析。

## 第二层增强：模板复用 + 锁相环实时纠偏（2026-08-02 续）

用户路线第二层：③ 模板复用（规划侧）+ ④ 锁相环实时纠偏（执行侧）。校验判据按用户拍板取 **A 关键词/存在性（无需 key）**。本次落地并端到端验证。

### ③ 模板复用（compiler/templates.py + plan.py）

- 新增 `compiler/templates.py`：模板注册表 `TEMPLATES`，每条含 `name / match_caps / match_mods / min_modalities / goal`（goal 为待编译的 Goal 字典）。
- **匹配=双重包含**：用户 caps 须 ⊇ 模板 `match_caps`（触发能力齐全），且模板 goal.caps 须 ⊇ 用户 caps（替换不丢步）；模态⊆允许集且达 `min_modalities` 下限；且须"增值"（模板多出能力 / 自带 feedback / dependencies）。取 goal.caps 最多者（最具体）。
- `plan.py` 在 `parse()` 后自动查表：`match_template(goal)` 命中则用 `build_goal_from_template` 套用（保留用户 description/name/reliability，feedback 用户显式优先），[2.5] 标"命中模板 X，跳过从头编译"；`--no-template` 可强制重编译。
- 种子 7 个（2026-08-02 续新增 4 个）：原 3 个 `research_report`(检索→抽取→推理→综述, 并联+反馈)、`verify_report`(检索→计算→核对→整理, 串行+反馈)、`multimodal_summary`(多模态检索→抽取→综述, 需≥2模态) + 新增 `data_analysis`(检索→抽取→计算→综述)、`document_review`(检索→抽取→核对→整理)、`code_review`(检索→抽取→推理→核对→整理)、`comparison`(检索→抽取→综述，与 research_report 仅以 reason 区分)。均补朴素解析易漏的 `extract` 步 + reliability 保险反馈环；各模板 goal.caps 设计为互不超集以防误命中。
- 价值：稳定可复用、和已有并联/高可靠/反馈天然叠加、不改 runtime；机制可扩展（往 TEMPLATES 加条目即可）。

### ④ 锁相环实时纠偏（plan.py.\_runbook + exec_loop.py）

- runbook 新增 `milestones`：**默认每个节点后**插轻量里程碑（并行前沿整层 + 单步节点级，降门槛让所有任务默认受保护），含 `after_steps / scope(layer|node) / check(轻量校验说明) / trigger_policy=consecutive_fail>=2 / on_fail=retry_upstream`。[9] 展示里程碑数（简单线性链也 ≥1，不再为 0）。
- `exec_loop` 新增两个 action + 状态字段 `milestones / corrections`：
  - `milestone_check`：前沿完成且未检 → 阻塞并吐校验指令（判据 A：产出非空 + 覆盖目标关键实体/数据项；agent 侧做，无需模型）。
  - `correct`：`ms:fail` 且未超上限(MAX_CORR=2) → 清空上游+下游步骤重跑（重试上游检索），并**清掉该里程碑 fail 标记**以便重跑后重新校验；超上限则放行继续（防卡死）。
  - `--record="ms:pass|fail"` 应用到当前待校验里程碑；底部便利命令补 `milestone_check`/`correct` 分支。
- 关键 bug 修复：`correct` 必须 `pop` 掉里程碑的 fail 状态，否则重跑后不会重新触发 `milestone_check` 而反复 `correct` 卡死——已修。

### 端到端验证（已跑通）

- ③：`检索资料并分析综述` → 命中 research_report，朴素 [retrieve,reason,summarize] 升级为 [retrieve,extract,reason,summarize] + 并联 DAG + feedback(max_iter=3)；`--no-template` 可绕过。
- ④：research_report runbook 走 `reset→parallel(1,2)→milestone_check→ms:fail→correct(重跑1,2)→milestone_check→ms:pass→execute_step(3)→execute_step(4)→quality_check→gate:pass→done`。中途漂移被抓住并纠偏，而非一路跑完才发现偏。
- 回归：翻译(单步/无模板/无里程碑) 仍 `execute_step→quality_check→done`；纯并行目标带里程碑流程正常。
- `compiler/templates.py` 与 `compiler/nl_parser.py` 自检全绿。

### 诚实边界（重申）

- 模板是"已知良好拓扑"复用，匹配靠能力签名(双重包含)粗粒度；复杂意图仍靠 agent 读 NL 补足，未做深语义解析。
- 锁相环的"轻量校验"默认 A(关键词/存在性) 是规则启发、非语义判断；要更准可后续切 B(LLM 判偏离) 或 C(复用 adc 阈值)，判据已抽象在 milestone 的 `check` 字段与 exec_loop 的 ms: 处理，切换低成本。

---

## 第二层增强：总线式架构重构 + 自适应模型选型（2026-08-02 续）

用户确认「⑤+⑥ 一起做」（第二层剩余两项）。两项均为**规划/表示层**增强，不动 runtime 内核。

### ⑤ 总线式架构重构（shared_context_bus）

**动机**：步骤数过大时，逐层显式串接的 runbook 冗长、不易读；把"逐层串接"压缩为"所有阶段挂载同一共享总线"的抽象，便于人/agent 把握全局。  
**落点**：

- `plan.py._runbook()`：**按拓扑形态触发**（任一层含 ≥3 个无依赖并行节点即启用，如 C 任务 4 个独立检索挂总线），步骤数 > `BUS_THRESHOLD`(默认 10) 作为兜底；启用时 runbook 增加 `phases`(按 step 所在 layer 分组、连续 0-based 阶段号、含 `bus_writes`/`bus_reads_from`) 与 `bus`(类型/`trigger`来源/阶段数/约定说明)。
- `draw.py`：任一层含 ≥3 个并行电阻 或 电阻总数 > 10 时，左侧画一条紫色背板总线贯穿各层，**并行层每个电阻都挂总线短桩**（更真实反映多元件挂总线），并标注「共享上下文总线」。  
  **语义**：纯表示抽象——同层步并行写总线最新快照、跨层上下文顺次累积，下游读快照；等价逐层串接的压缩。`同层max/跨层sum` 的 runtime 延迟语义**不变**(propagate 一行未改)。  
  **验证**：C 任务(4 并行检索) → `bus.trigger=topology(并行层≥3节点)`、SVG 含总线标签+紫色背板+4 个检索短桩；12 步串行拓扑 → `bus.trigger=scale`；5 步/无并行层拓扑回归 → `bus=None`。

### ⑥ 自适应模型选型（auto_tiers）

**动机**：原 `compile_goal` 默认 `auto_bind=True` 走 Binder 基线(最便宜达标档)，对"高可靠/高质诉求"目标不敏感——推理/核验/抽取/综述这类质量敏感步本该用大模型。  
**设计**：新增 `auto_tiers(goal)`，`plan.py` 非 `--optimize` 路径套用后改调 `compile_goal(goal, auto_bind=False, route=True)`（Binder 仍被当作"精度下限基线"复用，代码不变）：

1. **精度硬下限**：复用 Binder 的"达标最低成本"基线，保证 `min_quality` 不被破坏；
2. **质量敏感升档**：`reliability=high` 或 `min_quality>=0.85` → `reason/verify/extract/summarize` 升 `large`；
3. **成本/延迟受限保 small**：约束含 `max_cost`/`max_latency_ms` 时不升档（软约束，硬精度仍优先）；
4. 其余步维持精度下限对应的档(默认 small)。  
   **注意**：`max_chars`(字数/篇幅上限) 是**独立约束字段**，不属于成本/延迟受限，因此**不会**压低型号档——高可靠 + 字数约束仍正常升 `large`（修正：早期曾误把"不超过200字"当作成本约束而压档，现两者已分离）。  
   **回溯兼容**：常规可靠 + 默认 `min_quality` → 全 `small`(同旧 Binder 基线)；`--no-auto-tiers` 可整体回退到 Binder 基线。  
   **验证**：

- 高可靠 `总结一篇PDF并核对里面的数字` → `reason/verify=large`、`retrieve/calculate=small`；
- 高可靠 + `max_cost` 受限 → 质量敏感步保 `small`；
- 高可靠 + `不超过200字`(max_chars) → 质量敏感步仍升 `large`（字数约束不触发成本保护）；
- `min_quality=0.9` → 精度下限强制 `tool`(不被 small 覆盖)；
- 多重集 `retrieve#2` 归一正确；模板 research_report + 高可靠 → `extract/reason/summarize=large`、`retrieve=small`，③⑥ 组合生效。

### ⑦ 规划层预估耗时（第四步，runbook 可感知反馈）

**动机**：runbook 已告诉 agent 做什么/怎么并行/怎么重试，但没告诉"大概要多久"——丢了"拓扑能提前算账"的优势。  
**落点**：`plan.py._runbook()` 按"能力基础名 × 型号档"给每步一个 heuristic 延迟（`_LATENCY_MODEL`：large 比 small 重，tool 按 large 估），写入每步 `estimated_latency_ms`；并联层(同层)取 max、串联层(跨层)累加（同 runtime 分层语义），汇总为 `estimated_total_ms` + `estimated_breakdown`（含 `longest_parallel_layer_ms` / `rest_serial_ms`）。[9] 开头多一行 `预估总耗时: X.Xs（最长并行层取max=…s，其余串联累加=…s）[heuristic]`。  
**验证**：C 任务 → 预估 3.4s（最长并行层 1.6s【4 检索 max】+ 串联 1.8s【reason/classify/analyze】）；B 任务 → 1.3s。数值为启发式估算，非实测。  
**诚实边界**：延迟表是拍的 heuristic，目的是"量级可感知"，不是精确计时；接 `--backend=real` 实测后可用真值回填（对应后续"实际 vs 预估"复盘标注，P3）。

### 端到端验证（已跑通）

- ⑤：12 步拓扑 runbook 含 `phases`/`bus`，SVG 正确渲染背板总线；小规模回归 `bus=None`。
- ⑥：`auto_tiers` 四组用例 + 模板组合用例全绿；`[3b]` 在 plan 输出报告所选档与规则。
- 组合：`检索资料并分析综述，要求高可靠` 同时触发 ③(模板) + ④(锁相环里程碑) + ⑥(自适应升档)，runbook/SVG/拓扑 JSON 一致。

### 诚实边界（重申）

- 总线是"规划层压缩表示"，不改变执行语义；按拓扑形态(并行层≥3节点)或 >10 步启用，小规模且无并行层拓扑与旧 runbook 完全一致。
- 自适应选型仍是规则启发(可靠性/约束/步类型三因子)，未引入模型判优；与 M3 Optimizer 不冲突——`--optimize` 路径绕开 `auto_tiers`，由 Pareto 搜索定档。`min_quality` 精度硬下限始终优先于"降成本保 small"。

---

## 第三层增强：自动布局布线器 + 运行时拓扑热更新（2026-08-02 续，收口）

用户确认「⑦+⑧ 一起做」，并明确两项范围：**⑦ 只填默认串行**(不动已测并行路径)、**⑧ 档位升级自愈**(small→large→tool，最小改动)。

### ⑦ 自动布局布线器（语义 DAG 推断，默认快路径、零仿真）

**动机**：之前默认 `dependencies=None` → 全串行；并行只能靠 nl_parser 的"source/sink 粗分"或显式声明。没有"自动按能力语义连出合理 DAG"的快路径。M3 Optimizer 虽搜拓扑，但只在 `--optimize` 时跑且只切全局串/并(`patterns=[None,[]]`)，不推断"哪些能力依赖哪些"。  
**设计**：新增 `compiler/router_auto.py:infer_dependencies(goal)`——一张"能力语义依赖表"(生产者→消费者，如 `retrieve→{extract,reason,…,summarize}`、`reason→{verify,summarize,…}`，末端 `organize/summarize` 无出边) + **优先级守卫**(仅允许低优先级→严格更高优先级，保证无环) → 生成 `[[producer,consumer],…]` 边。  
**落点**：`plan.py` 在解析+模板后、编译前，仅当 `goal.dependencies is None`(默认串行) 时套用推断 DAG；已测的并行意图路径(nl_parser source/sink 粗分)与模板显式 DAG 不动。`--no-auto-route` 回退旧串行。多重集能力(`retrieve#2`)按基础名匹配，两实例都正确喂下游(并行源)。  
**与 M3 区别**：M3=`--optimize` 才跑的昂贵 Pareto 搜索(全局串/并切换)；⑦=默认快路径的确定性语义推断，即时零仿真。  
**验证**：`router_auto.selftest`(4 用例全过：基础推断 / 多重集并行源 / 单能力→None / 全能力组合经 from_dict 无环校验)；`检索资料然后推理并总结`(无并行词)→ `[2.6]` 报告 1 条边 `retrieve→reason`，DAG 分层 `[retrieve]|[reason]`（旧版会是全串行 `[retrieve]→[reason]` 等价，但更通用地支持多源并行）。

### ⑧ 运行时拓扑热更新（自愈 runtime，档位升级）

**动机**：执行中某电阻 yield 失败(开路)且仍在反馈预算内时，旧版只用原拓扑空转重试；应在运行时"热改"该节点再续跑，而非重启整链。  
**设计**：`runtime.Circuit.execute(self_heal=None)` 新增自愈——`self_heal=None` 读 `spec["self_heal"]`(默认 False，向后兼容)；显式 bool 可覆盖。`execute` 每轮 propagate 后若未达标且仍有反馈预算，调 `_escalate_failed()` 对当前 `ok=False` 的电阻**就地**升档(small→large→tool，幂等、已到 tool 不再动)，下一轮用升级后的拓扑续跑。`Goal` 加 `self_heal` 字段(默认 False，from_dict/to_dict 校验布尔) + `Router.route()` 写 `spec["self_heal"]` + `plan.py --self-heal` 开启。  
**范围（用户选「档位升级」）**：仅升档，不改拓扑结构(不加节点/不重连边)；属"热升级型号"而非"重路由"，runtime 内核改动最小。  
**验证**：构造确定性 yield 失败(给 SimBackend 打补丁使 small 必 `ok=False`、large/tool 必 `ok=True`) + 反馈环(max_iter=3)：

- 不开 self_heal → 3 轮全 fail，`success=False`，无 `self_healed`；
- 开 self_heal → 第1轮 small 失败→升 `cap_0:large`→第2轮通过，`success=True`，`self_healed={'cap_0':'large'}`；
- 向后兼容：默认 `execute()` 无 `self_healed` 键、不报错（旧行为零变化）。
- `plan.py --self-heal` → `goal.self_heal=true` + `spec["self_heal"]=true` + `[2.7]` 报告。

### 诚实边界（重申）

- ⑦ 是启发式语义推断（角色优先级表），不是深语义解析；复杂/隐含的数据流仍靠 agent 读 NL 或显式依赖/模板补足。推断仅填"默认串行"空档，不覆盖并行意图与模板。
- ⑧ 只响应 yield 失败(`ok=False`)的热升级，不响应"低质量但未开路"——低质量由 adc 质量门 + 反馈环整体重试兜住，与自愈互补；自愈的升级预算受反馈环 `max_iter` 约束（超出预算仍失败）。

## 补强#3 · 在线实测常态化（--backend real）+ ② 节点格式校验适配器（2026-08-02 续，收口）

本批在"3 层 8 方向路线图"收口后，按用户最新显式请求补三项增强：补强#3（真实 LLM 后端在线实测常态化）、② 节点格式校验适配器、更多模板种子（共 3→7）。三项均**纯规划/表示层**，不动 runtime 内核传播逻辑。

### ② 节点格式校验适配器（formats.py + router.py + runtime.py + draw.py）

**动机**：早期路线图把"② 节点格式校验"标为"未单独做、并入 M5/运行时"（见下方路线图状态）。本次把它真正落地为**规划层语义表 + 自动插适配节点**，而非依赖真后端。

- 隐喻核心：把每个能力的 I/O 看成"信号格式"——`raw`（原始/非结构化，类比 analog）vs `struct`（结构化/离散，类比 digital）。每个 canonical 能力在 `formats.CAP_FORMAT` 声明 `(consumes, produces)` 格式；如 `extract:(raw,struct)`、`calculate:(struct,struct)`、`retrieve:(raw,raw)`。
- **断点自动插适配器**：`router.route(goal, no_adapters=False)` 在布线时，对每条边检查相邻节点格式——`raw→struct` 自动插 **ADC 适配**（raw 进、struct 出），`struct→raw` 插 **DAC 适配**，`struct↔struct` 同类不插（transcode 仅在异构时）。合成节点名 `fmt@{kind}:{from}>{to}`，拆边为 `[pre,adapter]+[adapter,post]`。`spec` 新增 `adapters` 字段登记每个适配器（from_fmt/to_fmt/kind）。
- **runtime 零成本透传**：`SimBackend.run` 对 `format_adapter` 类型近零成本确定性透传（`cost≈0, latency≈5ms`，`ok` 继承上游聚合），**不引入任何 LLM 调用**——适配器只是"格式语义标注"，不是新执行单元。向后兼容：`--no-adapters` 可整体关掉（spec 不增 `adapters`、不插节点，拓扑与旧版一致）。
- **draw.py**：`format_adapter` 渲染为橙色 `ADAPT` 文字框，param 显示 `from_fmt→to_fmt`，便于在 SVG 上直观看到"哪里发生了 raw↔struct 转换"。

**验证**：`formats.selftest`（5 用例全过：raw→struct 插 ADC / struct→raw 插 DAC / 同类不插 / 多重集 / 全链路）；`plan.py` 集成实测 `verify_report`（retrieve→calculate 存在 raw→struct 断点）**自动插入 1 个 ADC**；`--no-adapters` 关闭后该节点消失、拓扑回归旧版；`draw.py` 渲出 `diagrams/retrieve_calc_verify.svg` 含 `ADAPT` 节点（标注 raw→struct + "ADC 适配"）。SimBackend 跑含适配器拓扑无网络回归（`success=True`）。

### 补强#3 · 真实 LLM 后端在线实测常态化（--backend real CLI 开关）

把"真·在线跑通"（此前是一次性 `_verify_real.py` 演示）沉淀为 **plan.py 可复用的 CLI 开关**，让"规划 → 真跑"成为常态化选项，而非偶发脚本。

- **新增 `--backend real`**：`plan.py` 在编译/优化后，走 `[10]` 真实后端实测段——构造 `RealLLMBackend` 并 `Circuit.propagate()`。
- **离线安全（默认）**：无 API key 时 `_run_real` 用 `RealLLMBackend(dry_run=True)`（组装请求但不发送，离线可跑、零网络风险）；有 key 且未显式 `--base-url` 且 `DEEPSEEK_API_KEY` 在位时，自动填 `https://api.deepseek.com/v1`（覆盖常用自托管/代理 endpoint）。
- **自动模型识别**：`backend_llm.py` 新增 `_auto_model_map(base_url)`——base_url 含 `deepseek` 自动套 `{small/tool→deepseek-chat, large→deepseek-reasoner}`，否则 OpenAI 默认映射。新增 `parity` 用例覆盖 `format_adapter` 结构与父类逐字段一致。
- **诚实边界（重要）**：Bash 沙箱**默认屏蔽出网**（即便有 key 环境变量也可能无法真发 HTTPS），故"真跑"在沙箱内通常落到 `dry_run` 离线演示；要真正发起调用需**关掉沙箱**并在本机/有出网环境跑（须经用户同意）。`[10]` 输出已含失败提示（网络 / key / base_url 三要素排查）。`final_quality` 仍是 per-tier 能力上限（cap）先验近似，非对真 LLM 输出质量的测量（要精确需接 judge / 人工评测）。

**验证**：`backend_llm.selftest`（模块运行 `python -m compiler.backend_llm`，5 用例全过：parity / dry_run / 注入假响应 / 错误路径 / 开路语义）；`plan.py --backend real` 在沙箱内走 dry_run 路径、`[10]` 正确输出 success/iterations/cost/latency/quality + 各组件 + self_healed + 失败提示，无网络回归。

### 更多模板种子（3→7）

`compiler/templates.py` 新增 4 个已知良好拓扑（与既有 3 个不互相遮蔽——靠"双重包含 + 最具体 + 增值 + 模态下限"匹配，且各模板 goal.caps 设计成互不超集以防误命中）：

- `data_analysis`：retrieve→extract→calculate→summarize（数据型任务）。
- `document_review`：retrieve→extract→verify→organize（文档审校）。
- `code_review`：retrieve→extract→reason→verify→organize（代码审查）。
- `comparison`：retrieve→extract→summarize（多方案对比；与 `research_report` 仅以 `reason` 区分、与 `multimodal_summary` 仅以模态数区分，逻辑隔离）。

**验证**：`templates.selftest` 扩 g7–g11（含 `comparison` 不与 `research_report` 冲突断言）；`plan.py` 实测 `数据分析报告` 类目标命中 `data_analysis`；所有 touched 文件 `py_compile` 通过。模板数由 3 扩到 **7**。

### 诚实边界（本批重申）

- ② 是**规划层语义增强**：适配器节点只标注"格式断点"，runtime 透传、不产生新执行成本；它不是真后端的数据转换逻辑（那在 M5 真 LLM 里由模型自身完成）。
- 补强#3 解决的是"真后端可常态化触发"——但 `quality` 仍是 cap 先验；要真实质量分仍需 judge / 人工。
- 三项全部向后兼容：默认行为（无适配器、sim 后端、3+ 模板）零变化；开关/字段缺失时即回退旧行为。

---

### 路线图完成状态

- 第一层：① 真并行指令吐出 ② 节点格式校验适配器（formats+router 自动插 ADC/DAC 合成节点，已落地） ③ 模板复用 ✓（种子 3→7）
- 第二层：④ 锁相环纠偏 ⑤ 总线重构 ⑥ 自适应选型 ✓
- 第三层：⑦ 自动布局布线器 ⑧ 运行时拓扑热更新 ✓
- **原 3 层 8 方向路线图已收口；并额外收口了早期标为"并入 M5/运行时"的 ②（格式校验适配器），以及补强#3（真实 LLM 后端在线实测常态化，--backend real 开关）与模板种子扩到 7 个。**

---

## Round 2：看门狗健康自检(Watchdog) + SVG 复盘标注（2026-08-01 续）

用户在五方向优化清单里把 ③ 看门狗计数器 与 ⑤ SVG 复盘标注 留到第二轮（两者都依赖 runtime 执行遥测）。本次落地并端到端验证。

### ③ 看门狗健康自检（runtime.Watchdog + Circuit.execute 集成）

**动机**：元件库缺"健康自检"——一个节点反复产出"将过不过"的平庸质量（弱但不死、未开路），旧 runtime 既不会标记、也不会优先替换，只能靠 adc 门 + 反馈环整体重试兜住。应在 runtime 层给每个节点记质量采样，连续平庸即标"劣化"，并在自愈开启时优先提档。  
**设计**：新增 `runtime.Watchdog` 类——按 `电路签名::节点` 索引，质量采样滚动存 `circuit-agents/.watchdog_state.json`（**跨轮/跨任务持久化**）。判定：某节点质量连续 `DEGRADED_CONSEC=3` 次落在平庸带 `DEGRADED_BAND=[0.55,0.85]`（将过不过/弱但不死）→ `degraded=True`（**动态、非粘滞**：一旦节点被升级并产出高质量，下一采样清零 `band_consec` 自动解除）。`Circuit.execute(self_heal, watchdog)` 集成：

- 开局（仅 `self_heal` 开且 `watchdog` 在位）：对**历史已 degraded 的电阻**优先就地提档（small→large→tool），即"优先替换/升级"，让历史劣化节点在当前任务直接受益；
- 每轮迭代把各节点 quality 记入看门狗（跨轮累积）；
- 返回 `res["watchdog"]`（各节点 `{samples,band_consec,degraded}`）+ `res["watchdog_pre_escalated"]`（本轮因看门狗预升级的节点）。  
  **向后兼容**：`execute()` 不传 `watchdog` → 不写状态文件、不加该字段、预升级不发生；旧行为零变化。看门狗只标记/升级"可升级的电阻"，电容/汇合/adc 等结构节点被标记但不自动升级（仅作健康报告）。

### ⑤ SVG 复盘标注（draw.py + plan.py 执行 pass）

**动机**：拓扑图只画"规划长相"，没有"实际跑得怎样"的复盘。⑤ 依据真实/仿真执行遥测叠加标注，让一次执行的结果在图上可读。  
**落点**：

- `draw.py.draw(spec, out_path, exec_result=None)` 新增 `exec_result` 入参；有数据时叠加——**慢节点(延迟≥`SLOW_THRESHOLD=1000ms`)红框** / **重试或曾失步(ok=False 或在 `self_healed` 中)橙虚框** / **自愈升级(在 `self_healed` 中)绿✓标** / **看门狗 degraded 紫⚠标**；右下角附**颜色图例**。无 `exec_result` 则与旧版完全一致（纯规划/离线图不含任何标注）。
- `plan.py`：默认 `--execute` 补一个 **SimBackend(random.Random(0)) 确定性仿真执行 pass**（零 API 成本），产出 `exec_result` 传给 `draw`；加载/保存 `Watchdog` 状态（跨轮累积）；`[9]` runbook 多打"仿真实测/自愈升级/看门狗劣化节点"三行 + 标注说明；`--backend=real` 真实结果在 `[10]` 后**用真实实测结果重绘带标注的 SVG**（覆盖仿真版）。新增 `--no-watchdog` 开关可跳过状态持久化。  
  **验证**：
- 单元：Watchdog 连续 3 次落带→degraded、2 次→非、出带重置→非；execute 返回 `watchdog`/`self_healed`/`watchdog_pre_escalated`；draw 四种颜色+图例全部出现、无 `exec_result` 时零标注。
- 集成（规则路径 `--key-file=`，不动桌面 key 文件）：同一高可靠任务连跑 3 次 → 看门狗跨轮累积，第 3 次 `看门狗劣化节点(跨轮)` 列出 10 个 degraded 节点（`pdf_check.svg` 呈现 13 个紫⚠）；再开 `--self-heal` 跑一次 → 历史 degraded 电阻被预升级（`self_healed={'cap_0':large,…, 'cap_4':tool}`），final_quality 由 0.715 跃升至 0.94，`music_rank_summary.svg` 呈现 5 个绿✓ + 5 个橙虚框 + 红框慢节点 + 图例；degraded 因升级后产出高质量而自动解除（post-run 快照 `无`）。  
  **诚实边界**：仿真遥测是 SimBackend 的随机电阻模型（cap/yield 先验），非真模型表现；想看真值用 `--backend=real`（沙箱内通常 dry_run，详见补强#3 边界）。看门狗"劣化"是启发式历史健康记录，标记电容/汇合/adc 等结构节点仅作提示、不自动升级。

### 路线图完成状态（更新）

- 原 3 层 8 方向 + ② + 补强#3 + 模板 7 个 ✓
- **Round 2 收口**：③ 看门狗健康自检（runtime 跨轮劣化标记 + 自愈优先升级）✓；⑤ SVG 复盘标注（红/橙/绿/紫四色 + 图例，仿真/真实双数据源）✓

---

## Phase C：LLM 实例封装（每个电阻 = 独立 LLM 实例）（2026-08-02 续）

用户在"模拟→真实多智能体"演进方向上拍板：先把**一个元件**改造成「带独立角色系统提示词 + 独立 API 调用的 LLM 实例」，再逐步铺开。本轮按用户选择（q-0 → 先写 `reason/summarize` 生成式；q-1 → **只写封装 + 提示词模板，不接 API、不在此环境跑**）落地，并把接缝做进已有的 `RealLLMBackend` 上。

### 落点：`compiler/llm_agents.py`（新增）

**核心封装** `class LLMAgentBackend(RealLLMBackend)`：

- **内核零改动**：完全复用父类 `RealLLMBackend` 的传输 / 模型映射(`model_map`/`_auto_model_map`) / `dry_run` / 注入式 `http_post` / 开路语义。只重写 `_build_messages`——把父类那句「通用占位 system」换成**按节点能力选出的角色系统提示词**。
- **能力读取**：编译产物里电阻节点是 `{"type":"resistor","label":<能力名>,"model":<tier>}`（`netlister.py:69`），故按 `comp["label"]`(=能力名) 选词；也兼容显式 `comp["capability"]` 字段（`_capability_of()`）。
- `render_messages(comp, inputs)`：离线渲染出 `messages`（不发送），用于审阅提示词装配。
- `selftest()`：**纯离线**（dry_run / 注入假响应 / render_messages），无需 key、不发网络，只验证「提示词装配 + 接线」正确。

**九能力角色提示词** `CAPABILITY_PROMPTS`：统一结构 = Role / Responsibility / Input / Output / Constraints（中文书写，与中文任务链路一致）。本次重点写好两个**生成式**角色：

- `reason`（推理）：基于上游上下文做严谨推理，红线=禁止编造、推断显式标「（推断）」、依据不足明说。
- `summarize`（摘要）：把上游中间结果压成最终交付物，红线=不新增事实/数字、金字塔结构、冲突结论并列不裁决。
- 其余 7 个（`retrieve/extract/calculate/translate/classify/verify/organize`）一并补齐，结构相同、各角色定制：
  - **`retrieve` 作为「工具型节点」对照**：其提示词明确它是"用检索工具的节点"（只取回带出处的原始资料，不生成/不推理），与 `reason/summarize` 自由生成式形成对比。

### 验证（`python compiler/llm_agents.py`，7 项全过，零真实 API 调用）

- 九能力提示词齐全；
- `reason`/`summarize` 渲染：system == 注册能力提示词 + user 含上游 ctx；
- `retrieve`：工具型措辞（检索工具 / 未检索到）正确；
- 未知能力回退通用占位，不崩；
- `dry_run` 接线：经 `LLMAgentBackend` 装配能力 system（无网络）；
- 开路语义：上游全死 → 直接开路（与内核一致）。

### 诚实边界（本批）

- 本轮**只交付封装 + 提示词模板**：不在此环境配置 key、不发起真实调用；真·跑通需接 `--backend real`（见补强#3 边界，沙箱内通常落 dry_run）。
- 封装本身已接入 `Circuit.propagate` 链路（`run()` 继承父类，自动用能力 system），未来把 `LLMAgentBackend` 传给 `Circuit(backend=...)` 即可让每个电阻走真 LLM 实例——这是"每个电阻=独立 LLM 实例"的**客户端接缝**，调度器(opamp)仍是规则/LLM 规划，与用户"主 agent 也是 LLM 实例"的更完整形态尚有距离（那步需改 `plan.py` 让调度器也输出 DAG JSON）。
- 提示词质量靠人工审阅（本次先交 `reason`/`summarize` 草稿供用户审），非自动评测。

---

## Phase C 三个改进已落地（2026-08-02 末 · 用户评审后）

用户在验收 Phase C 封装后提出三点评审意见，本次全部实现（用户选"三条全做"）：

- **① Token 成本（tier 感知精简提示词）**：`CAPABILITY_PROMPTS` 每个能力新增 `system_short`（精简版，~40-70 字，仅保留角色 + 最关键红线）；`LLMAgentBackend.system_prompt_for()` 按 `comp["model"]` 选词——`small` 档用 `system_short`、`large/tool` 用完整版。简单节点（如单次 retrieve / small 档）不再为长 system 买单。
- **② calculate 可靠性边界（运行时结构性数值校验）**：LLM 算术概率性不可靠，提示词约束必要但不充分。新增 `_safe_eval`（白名单 AST 递归求值，只放行数字 / `+-*/%**` / 括号，绝不 eval 任意串）+ `_numeric_consistency_check`（正则抓输出里的显式等式 `a op b = c`，独立重算比对）。`LLMAgentBackend.run()` 对 calculate 节点、`ok` 且非 dry_run 时挂 `meta["numeric_check"]={checked, mismatches, ok}`。这是**结构性兜底**，不依赖模型自觉；对硬精度要求（财务等）仍建议上游带权威数字 + 此校验双重保险。calculate 提示词也同步要求"每步写成显式等式便于核验"。
- **③ reason 的「（推断）」标注（跨节点验证契约）**：`verify` 完整版提示词新增第 6 条——若上游含 reason 产出，专门检查推断性陈述是否都标「（推断）」，未标注记为「推断标注缺失」纳入汇总。把"模型自觉标注"从软约束升级为 verify 节点的硬检查项。

**验证**：`python compiler/llm_agents.py` 扩到 **12 项全过**——新增 system_short 齐全 / tier 感知选词(small≠large) / verify 含推断检查 / calculate 数值校验抓出故意写错等式（`1200+350=1600`≠1550）且不误杀正确算术。未发起任何真实 API 调用。  
**诚实边界更新**：提示词仍是"软契约"的起点；① 省 token、② 提供算术兜底、③ 把推断标注变成 verify 可查项——但 LLM 对"推断 vs 事实"的自觉仍非 100%，③ 是"尽量查出来"而非"保证零遗漏"，最终可靠性仍靠人工 / 更强 judge。

---

## Phase C 真·接线跑通 + 修上下文断流 bug（2026-08-02 深夜）

用户要求"LLMAgentBackend 真正接进 Circuit(backend=...) 跑一次真模型"。封装 + 三个改进已就绪，本次落"真跑"并修一个**阻断性 bug**。

### bug：任务描述到不了电阻（上下文断流）

首跑真模型发现 `reason` / `summarize` 都回"未收到上游上下文"——上游只收到 `goal.name` 兜底串 `"unnamed-goal"`，任务描述根本没流下去。

**根因**（链路追踪）：

- `netlister._prefix` 造电源节点 `src = {"type":"power","label": goal.name or "任务"}` —— 只带 `label`，**没带 `task`**。
- `runtime.SimBackend.run` 的 power 分支返回 `comp.get("task", comp.get("label",""))`：因 `src` 没 `task`，回退到 `label` = `"unnamed-goal"`。
- `Circuit.propagate()` 只读 `components` 与 `wires`，**从不读 `spec["task"]` 顶层字段**——于是 `netlister.compile()` 里那句 `spec["task"] = goal.description` 成了**死元数据**，电源节点把任务名当信号源发下去，描述永远到不了下游电阻。

### 修复（最小、架构正确）

`netlister._prefix` 让电源节点自身携带 `task`：

```python
comps["src"] = {"type": "power", "label": goal.name or "任务",
                "task": goal.description or "自动编译目标"}
```

- 向后兼容：`SimBackend` power 分支本就读 `task` 回退 `label`；`_prefix` 被 `Router` 复用，故 Router 拓扑也一并受益。
- 未动 `propagate()` / runtime 内核——延续"布线决定系统级延迟、内核稳定"原则。

### demo 配套（`compiler/_demo_llm_agents_run.py`）

- goal 加 `name="live-reason-summarize"`（否则 label 仍是 "unnamed-goal"）+ `constraints={"min_quality": 0.6}`。
- 注释说明：`min_quality=0.6` 是为让 **ADC 门控反映"各节点都已成功产出"**——因 LLM 后端的质量数仍是 **small 档 cap 先验（0.70）**、非真实测得的输出质量，阈值压到先验之下以免卡在已知近似上。生产环境应换成真实质量估计（长度/格式校验 或 下游 judge 节点），属 M5 后续。

### 真·在线验证通过 ✓（DeepSeek，沙箱出网开放、key 在位）

- 运行：`python compiler/_demo_llm_agents_run.py`（默认真·live，key 从 `~/Desktop/key_tmp.txt` 文件读入，明文不进对话/命令）。
- 编译：7 组件 / 6 边 → `Circuit(spec, LLMAgentBackend).propagate()`。
- 实测：
  | 节点                | 档/model               | 结果                                            |
  | ----------------- | --------------------- | --------------------------------------------- |
  | cap_0 [reason]    | small / deepseek-chat | 真调 1741ms，产出光合作用解释，**正确标注「（推断）」**（改进③在真模型上生效） |
  | cap_1 [summarize] | small / deepseek-chat | 真调 1076ms，正确压缩 reason 产出                      |
  | adc               | thr=0.6               | **ok=True** q=0.70                            |
  | total             | —                     | 3127ms / $0.0146                              |
- **关键证明**：每个电阻 = 独立 LLM 实例、带各自能力 system 提示词，且**任务描述经 电源→调度器→电容→reason 正确流入上游上下文**——断流 bug 已修复。

### 回归

- `python compiler/llm_agents.py`（12 项全过，无真实 API）+ `python compiler/backend_llm.py`（5 项全过，无真实 API）—— netlister 改动零回归。
- 注：`backend_llm.py` 直接 `python compiler/backend_llm.py` 跑会因 `from runtime import ...` 缺路径报 `ModuleNotFoundError`（预存在，因它没自举 sys.path；从 `circuit-agents/` 根 `PYTHONPATH=.` 跑即过，demo/llm_agents 已自举故无碍）。

### 诚实边界（重申）

- 本次修复的是"任务描述如何流到电阻"的**接线正确性**，不是质量评估——`final_quality=0.70` 仍是 small 档 cap 先验，非对真 LLM 输出质量的测量（要精确需 judge / 人工）。
- 真后端接线、提示词装配、推断标注、上下文串接，四件事在真模型上均已实证；调度器(opamp)仍是规则/LLM 规划，与"主 agent 也是 LLM 实例"的更完整形态仍有距离（那步需改 `plan.py` 让调度器也输出 DAG JSON）。

---

## 每个 agent 可调用技能（技能包 + 真·工具调用）（2026-08-02 续，收口）

用户拍板「让每个 agent 都可以调用技能」。在已落地的 `LLMAgentBackend`（每个电阻 = 独立 LLM 实例 + 能力 system 提示词）之上，把"技能"从"提示词里声明一下"升级为 **真·工具调用（OpenAI `tools` / `tool_calls` 循环）**：模型能主动发起调用、运行时执行、结果回灌、模型再据此续答。

### 设计（两轮回合 AskUserQuestion 锁定）

- **技能 = 外部工具函数（tool-calling）**，不是仅提示词声明、也不是 WorkBuddy 原生技能——是"给模型挂一组可调用的函数 + JSON 入参 schema，模型按需调用、我们执行"。
- **实现形态**：`CAPABILITY_PROMPTS` 每能力新增 `skills` 字段（声明该能力实例可挂哪些技能名）；`LLMAgentBackend` 把技能声明注入 system 提示词 + 用 `build_tools_schema()` 构造 OpenAI `tools`；运行时 `execute_skill()` 执行后把结果以 `role:"tool"` 消息回灌。
- **执行方式 = 真·工具调用**：模型在响应里发 `tool_calls` → 逐个 `execute_skill(name, arguments_json)` → 追加 `role:"tool"` 消息 → 模型继续，直到不再发调用或触达迭代上限。
- **首个技能包 = `reason` + `run_code`**（Python 解释器）：自包含、无需外部 API，验证"每个 agent 能调技能"闭环的最简形态。
- **架构约束（用户确认）**：kernel（`Circuit.propagate` / 分层 / 开路语义）不动；**只改封装层 `LLMAgentBackend`**。`agent_skills.py` 是全新的独立技能注册/执行模块。

### 落点

- **`compiler/agent_skills.py`（新增）**：技能注册表 + 执行层。
  - `SKILLS`：`name → {description, parameters(JSON schema), handler}`。
  - `run_code` handler：`subprocess` 跑 `sys.executable -c <code>`，临时目录 + 10s 超时，捕获 stdout/stderr/returncode 回字符串。
  - `build_tools_schema(skill_names)` → OpenAI `tools` 列表；`execute_skill(name, arguments_json)` → 字符串（吞掉所有异常，未注册技能优雅报错）；`skill_declaration_text(skill_names)` → 注入 system 的技能说明文本。
  - **诚实边界文档**：`run_code` 执行模型生成的 Python，**当前无沙箱隔离**（仅临时目录 + 10s 超时）；生产应加 seccomp / 禁网 / 资源限额。
- **`compiler/llm_agents.py`（改写）**：
  - `CAPABILITY_PROMPTS["reason"]` 加 `"skills": ["run_code"]`。
  - `system_prompt_for()` **保持纯净**（只返回能力基础提示词，保证已有 selftest 的精确相等断言不过）；技能声明改在 `_build_messages()` 内追加（不污染纯函数）。
  - 新增 `_tools_for(comp)`（→ `build_tools_schema` 或 None）、`_chat_one(messages, model, tools)`（复用父类 `_post` 的单次对话封装）。
  - **重写 `run()` 为工具调用循环**（仅电阻覆盖父类）：`tool_choice="auto"`、`_MAX_TOOL_ITERS=4` 上限；每轮若响应含 `tool_calls` 则执行并回灌、否则收口为最终 `Signal`；信号 `meta` 记录 `tool_calls` / `tool_log`（含每个技能名 + 结果前 400 字）；calculate 节点仍挂 `numeric_check`。
- **`compiler/_demo_llm_agents_run.py`（配套）**：默认 `--goal` 改数值任务（"一笔 10000 元本金，年利率 3.5%，存 5 年：分别算出单利和复利的最终金额，并解释两者的差异"）触发 `run_code`；打印块展示 `技能调用` + 每步 `工具结果`。

### 验证

- **离线 selftest（`python compiler/llm_agents.py`）扩到 15 项全过**：含"reason 声明 run_code + `_tools_for` 正确产出 tools schema"、"`execute_skill(run_code)` 真跑 Python 返回 42"、未注册技能优雅报错、以及**真·工具调用循环集成用例**（注入式假响应序列：先回 `run_code` tool_call → 执行 `6*7=42` → 再回终答，验证循环融合）。`system_prompt_for` 的纯函数断言由精确 `==` 改为 `startswith` + 显式 run_code 声明检查，避免回归。
- **dry-run**：确认 `reason` 组装的 system 已含"你可调用以下技能…- run_code：执行一段 Python 代码…"，且 user 消息携带新数值任务。
- **真·live（DeepSeek，沙箱出网开放、key 在位）实测通过 ✓**：
  | 节点                | 档/model               | 技能调用           | 结果                                                                                                             |
  | ----------------- | --------------------- | -------------- | -------------------------------------------------------------------------------------------------------------- |
  | cap_0 [reason]    | small / deepseek-chat | **`run_code`** | 真调 5870ms；Python 算出 **单利=11750.00 / 复利=11876.86 / 差异=126.86**（stdout 干净返回），并**融合进终答**（markdown 表格含两种计息公式与最终金额） |
  | cap_1 [summarize] | small / deepseek-chat | —              | 真调 1853ms，正确压缩 reason 产出（含差异 126.86 元 / 相对差异约 1.08%）                                                           |
  | adc               | thr=0.6               | —              | ok=True q=0.70                                                                                                 |
  | total             | —                     | —              | **8034ms / $0.0159**                                                                                           |
  **关键证明**：`reason` 这个 LLM 实例在真模型上**自主判断需要算数 → 发起 `run_code` 工具调用 → 运行时执行 Python → 结果回灌 → 模型把执行结果写进最终交付物**——"每个 agent 可调用技能"在真实模型上闭环实证。

### 诚实边界（本批）

- **`run_code` 无沙箱隔离**（仅临时目录 + 10s 超时）：执行的是模型生成的任意 Python，存在执行任意代码风险；生产环境必须加隔离（禁网 / seccomp / 资源限额 / 白名单），否则不应暴露在不可信输入下。当前定位是"验证技能包机制的自包含 demo"，非生产执行器。
- 技能仍是"函数 + schema"形式，不是 WorkBuddy 原生技能；每个能力实例可挂不同技能包（如 retrieve→WebSearch、verify→CrossCheck），属后续扩展。
- `final_quality=0.70` 仍是 small 档 cap 先验，非对真 LLM 输出质量的测量（要精确需 judge / 人工）。
- 内核（`propagate` / 分层 / 开路语义）零改动；仅封装层 `LLMAgentBackend` + 新增 `agent_skills.py`。

---

## 扩技能包（第一层：retrieve 技能包）（2026-08-02 深夜）

用户在"每个 agent 可调用技能"闭环后拍板"扩技能包"，按三层路线图（retrieve 立刻 → reason/verify 本周 → 其余按需）落地**第一层 retrieve 技能包**。先澄清一个误解：DeepSeek key 是"模型推理 key"（只给 DeepSeek 推理用），**不能当搜索 key**——`web_search` 仍需独立的搜索后端，故本层走"无 key 真实源(DuckDuckGo) + 预留 `SEARCH_API_KEY`/Tavily 插槽"。

### 落点

- **`compiler/agent_skills.py` 新增 3 个 handler + SKILLS 注册**：
  - `_web_search_ddg(query, max_results=5)`：DuckDuckGo HTML 正则抓取（timeout=6）；`_web_search_tavily(query, api_key, max_results=5)`：Tavily 结构化搜索。
  - `_web_search(query, max_results=5)`：分发——若 `SEARCH_PROVIDER=tavily` 且 `SEARCH_API_KEY` 在位走 Tavily，否则 DDG（无 key 真实源）。
  - `_read_page(url)`：抓取 URL 全文、strip 标签、截断 4000 字（支持 `file://` 本地文件）。
  - `_query_db(query)`：本地 `docs/` + `circuit-agents` 下 `.md/.txt/.py/.json` 做 grep，最多 15 命中（零外部依赖，环境健壮）。
  - `SKILLS` 现 = `run_code, web_search, read_page, query_db`；`execute_skill` 失败统一「技能 [名称] 调用失败：原因」（未注册→"未注册"）。
- **`compiler/llm_agents.py`**：`CAPABILITY_PROMPTS["retrieve"]` 加 `"skills":["web_search","read_page","query_db"]` + 终止指引（"收集 2-3 条后停搜并输出带来源清单"）。
- **`compiler/_demo_llm_agents_run.py`**：加 `--demo retrieve`（retrieve→summarize 链路 + 本地导向默认任务，避免模型把通用名误搜到 GitHub）。

### 修两个真·live 暴露的 bug（封装层，内核零改动）

① `_MAX_TOOL_ITERS` 4→8，触顶强制再调一次（`tool_choice="none"`）收口并给 tier 先验 quality（不再因 quality=0 被下游判开路）；② 覆盖 `_render_value` 深度上限 3→12——`aggregate` 把上游值包成 Signal 列表，"电阻→汇合→格式适配→汇合→下游"3 层嵌套在 depth>=3 被截断成 "[N items]" 导致 summarize 拿空上下文。

### 验证

- 离线 selftest 17/17（含 retrieve 技能声明 / `_tools_for` 产 3 个 tool schema / query_db 命中 / web_search-read_page 容错）。
- 真·live（DeepSeek）`cap_0[retrieve]` 发起 6 次工具调用（query_db×5 命中 COMPILER.md/RESULTS.md + read_page 读 runtime.py）产出带来源整理结果 8.6s；`cap_1[summarize]` 正确接收并产出 Signal 字段表摘要；adc ok；整链 9670ms/$0.0214。

### 诚实边界

- 本沙箱出网仅放行 DeepSeek API，DuckDuckGo 不可达 → web_search 实测全超时、模型降级到本地 query_db（环境限制非代码缺陷，生产开 egress 或填 Tavily key 即真实联网）。
- 仍属第一层，reason/verify 第二层、其余第三层后续扩。

---

## 扩技能包（第二层：reason+calculator / verify+cross_check+diff_text）（2026-08-02 深夜）

用户"继续"（路线图中"第二层（本周）reason+verify"），在已落地的 retrieve 技能包之上，给 **reason** 与 **verify** 两个能力各挂一个技能包：reason 新增 `calculator`（算术），verify 新增 `cross_check`（本地取证）与 `diff_text`（比对）。延续"内核零改动、只扩封装层"原则。

### 落点

- **`compiler/agent_skills.py` 新增 3 个 handler + SKILLS 注册**：
  - `_safe_eval(expr)`：白名单 AST 递归求值（`_ALLOWED_AST` = Expression/BinOp/UnaryOp/Constant/Add/Sub/Mult/Div/Pow/Mod/USub/UAdd/FloorDiv），`eval` 时 `{"__builtins__":{}}`，**绝不 eval 任意串**——给 `calculator` 兜底。
  - `_calculator(expression)`：调 `_safe_eval`，返回数值结果文本（非法表达式→明确报错而非崩溃）。
  - `_cross_check(claim)`：用 `re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}")` 抽取 claim 关键词（标识符 + 2+ 汉字），剔除停用词，对每个关键词 `_query_db`（本地 docs/源码 grep）取证；当某关键词形如 `expr=val` 时顺带数值重算比对。返回「命中 X/Y 条 + 每条是否佐证」的取证报告，末位给「一致/不一致」初步结论。
  - `_diff_text(original, conclusion)`：比对两段文本的数字与关键词集合，返回「未发现明显不一致 / 发现 N 处差异」短结论。
  - `SKILLS` 现 = `run_code, web_search, read_page, query_db, calculator, cross_check, diff_text`；`execute_skill` 失败统一「技能 [名称] 调用失败：原因」措辞（未注册→"未注册"）。
- **`compiler/llm_agents.py`**：
  - `CAPABILITY_PROMPTS["reason"]` 技能扩为 `["run_code", "calculator"]`（之前只 run_code）。
  - `CAPABILITY_PROMPTS["verify"]` 加 `"skills": ["cross_check", "diff_text"]` + 提示词要求"先 cross_check 取证、再 diff_text 比对、最后给明确核验结论"。
  - `_chat_one(self, messages, model, tools=None, tool_choice="auto")` 新增 `tools`/`tool_choice` 参数（复用父类 `_post` 单次对话封装）。
  - `_MAX_TOOL_ITERS` 4→**8**（与第一层修复合并，覆盖 retrieve 长调用链）。
  - `run()` 触顶回退重写：强制再调一次 `tool_choice="none"` 收口，quality = tier 先验（若最终有内容）否则 0.0（避免 quality=0 被下游判开路）；`except` 内 `final={}` 已修。
  - 覆盖 `_render_value` 深度 12（父 `RealLLMBackend` 在 depth>=3 截断成 "[N items]"，导致多层嵌套 Signal 列表上下文丢失）。
- **`compiler/_demo_llm_agents_run.py`**：加 `--demo verify`（默认任务 = 核验 claim「circuit-agents 在 runtime.py 中用 Signal 类在节点间传递消息，且 \_TIERS 定义了 small/large/tool 三档型号」，`capabilities=["verify"]`）；dry-run 块展示首个电阻 messages。

### 验证

- **离线 selftest（`python compiler/llm_agents.py`）**：新增 8c（retrieve 三技能 + `_tools_for` 产 3 个 tool schema）/ 8d（query_db 命中 + web_search/read_page 容错）/ 8e（reason+calculator / verify+cross_check+diff_text 声明+执行：`calculator` 算 `(10000*(1+0.035*5))`→含"11750"、diff_text 与 cross_check 非空）—— 全部离线、无 key、无网络。尾行「✓ 第二层技能包: reason+calculator / verify+cross_check+diff_text 声明+执行通过」+「全部离线自检通过 ✓」。
- **dry-run**：确认 reason 的 tools 含 calculator、verify 的 tools 含 cross_check/diff_text、user 消息带核验 claim。
- **真·live（DeepSeek，沙箱出网开放、key 在位）实测通过 ✓**：
  | 节点             | 档/model               | 技能调用                                          | 结果                                                                                                                                                                                                           |
  | -------------- | --------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
  | cap_0 [verify] | small / deepseek-chat | **`cross_check`→`diff_text`→`cross_check`×3** | 真调 6036ms；`cross_check` 取证「关键词 8/13 命中」（`runtime`/`Signal`/`_TIERS` 等本地文档佐证），`diff_text` 报「未发现明显数字/关键词层面不一致」；终答「## 核验结果：**一致**」（含「runtime.py 中用 Signal 类在节点间传递消息」5/5 命中、`_TIERS 定义 small/large/tool` 6/6 命中） |
  | adc            | thr=0.6               | —                                             | ok=True q=0.70                                                                                                                                                                                               |
  | total          | —                     | —                                             | **6316ms / $0.0151**                                                                                                                                                                                         |
  **关键证明**：`verify` 这个 LLM 实例在真模型上**自主决定"要先取证" → 发起 `cross_check`（本地 grep 文档核对比对）→ 再 `diff_text` 比对 → 最后融合成明确核验结论**——"每个 agent 可调用技能包"在 verify 维度也闭环实证。

### 诚实边界（本批）

- `calculator` 的 `_safe_eval` 是**结构性算术兜底**（白名单 AST），不依赖模型自觉；对硬精度要求（财务等）仍建议上游带权威数字 + 此校验双重保险（与 Phase C 改进②同哲学）。
- `cross_check` 是**本地文档取证**（grep `docs/`+`circuit-agents` 下 .md/.txt/.py/.json），非语义理解——它验证"claim 里的关键词是否出现在项目资料里"，不判断 claim 逻辑真伪；生产若要对外部事实核验需接 web_search（第一层已挂）或 judge。
- `diff_text` 仅做**数字/关键词集合比对**，浅层一致性检查；深层语义冲突仍靠模型。
- 真·live 时 `web_search` 因沙箱只放行 DeepSeek API 仍不可达，但 verify 技能包完全不依赖联网（cross_check/diff_text 皆本地），故本层不受出网限制影响——比第一层 retrieve 更"环境健壮"。
- `final_quality=0.70` 仍是 small 档 cap 先验，非对真 LLM 输出质量的测量（要精确需 judge / 人工）。
- 内核（`propagate`/分层/开路语义）零改动；仅封装层 `LLMAgentBackend` + `agent_skills.py`。

---

## 扩技能包（第三层：extract/translate/classify/calculate/organize/summarize 领域工具）（2026-08-02 末）

用户"第三层"= 把技能包路线图的最后一档（其余 6 个原子能力各挂领域工具）一次性落地。延续  
"纯 stdlib 优先、可选库接缝、内核零改动"原则。现已**集齐九能力技能包**——每个电阻节点都有可  
调用的领域工具。

### 落点

- **`compiler/agent_skills.py` 新增 9 个 handler + 全部注册进 `SKILLS`**（现共 16 技能）：
  - `extract_fields(text, patterns_json?)`：按自定义正则抽字段，或留空抽邮箱/网址/电话/日期等内置实体（纯 stdlib）。
  - `extract_pdf(path)` / `extract_ocr(image_path)`：**可选库接缝**——优先 pdfplumber/PyPDF2、pytesseract+Pillow；均无则返回明确"需 pip install"提示、不崩。
  - `apply_glossary(text, glossary_json?)`：按术语表统一译名/用词，保术语一致（纯 stdlib）。
  - `classify_taxonomy(text, taxonomy_json?)`：按分类体系关键词打分、返回降序类别+依据（纯 stdlib）。
  - `unit_convert(value, from, to)`：长度/质量/时间/数据/体积 + 温度（含偏移）换算表（纯 stdlib）。
  - `spreadsheet_calc(csv_text, column?, op?)`：CSV 聚合 sum/avg/min/max/count（纯 csv 模块）。
  - `apply_template(content, template_name?)`：bullet/numbered/sections/qa 模板塑形（纯 stdlib）。
  - `apply_style_guide(text, guide?, max_length?)`：concise/bullets/no_jargon 文体约束后处理（纯 stdlib）。
- **`compiler/llm_agents.py` 给 6 个能力加 `skills` 列表**：
  - `extract`→`[extract_fields, extract_pdf, extract_ocr]`、`translate`→`[apply_glossary]`、  
    `classify`→`[classify_taxonomy]`、`calculate`→`[unit_convert, spreadsheet_calc]`、  
    `organize`→`[apply_template]`、`summarize`→`[apply_style_guide]`。
  - 同步更新模块安全边界 docstring：第三层 7 个纯 stdlib 技能环境健壮、2 个（PDF/OCR）可选库接缝。
- **`compiler/_demo_llm_agents_run.py`** 加 `--demo layer3`（extract→classify→organize→summarize 链路 + 示例反馈文本，引导各节点调专属技能）。
- 旧 selftest 断言 8b（"summarize 无技能包"）已失效（现九能力全挂技能），改为验证"未知能力→None"契约。

### 验证

- **离线 selftest（`python compiler/llm_agents.py`）22/22 全过**：8f（九能力均声明+装配对应 skills schema）/ 8g（逐个 `execute_skill` 真跑 7 个纯 stdlib 技能并断言正确结果 + `extract_pdf`/`extract_ocr` 无库优雅降级不崩）。
- **真·live（DeepSeek，沙箱出网开放、key 在位）实测通过 ✓** —— `--demo layer3`，4 个 L3 能力在真模型上**各自发起工具调用**：
  | 节点                | 技能调用                         | 真实产出                                                           |
  | ----------------- | ---------------------------- | -------------------------------------------------------------- |
  | cap_0 [extract]   | `extract_fields`             | 抽中 电话 138-0013-8000 / 邮箱 <good@restaurant.com> / 日期 2026-03-15 |
  | cap_1 [classify]  | `classify_taxonomy`          | 餐饮服务(命中6) / 正向为主混合情感(正向3+负向1) / 联系方式(命中3)，附依据                  |
  | cap_2 [organize]  | `apply_template`(sections)   | 按主题分段结构化输出                                                     |
  | cap_3 [summarize] | `apply_style_guide`(concise) | 简洁文体摘要稿                                                        |
  total **21572ms / $0.0233**；`adc ok=True`。translate(`apply_glossary`)/calculate(`unit_convert`+`spreadsheet_calc`) 的 handler 真跑与接线由离线 selftest 8f/8g 证明（尚未单独跑其真·live，模式已备）。

### 诚实边界（本批）

- `extract_pdf`/`extract_ocr` 依赖第三方库，本沙箱未预装 → 调用返回明确"需安装"提示、不崩；生产 `pip install` 后即真可用（与 web_search 的"无 key 真实源 + Tavily 插槽"同哲学）。`extract_fields` 永远可用。
- 第三层技能多为**结构性/格式助手**（与 L2 的 calculator/cross_check 同哲学）：把"软约束"变"可执行后处理"（术语一致、分类有据、单位换算、表格聚合、模板塑形、文体约束）。它们**不替代** LLM 的判断，而是让判断有可追溯的结构支撑。
- `classify` 的 `classify_taxonomy` 需传入 taxonomy（模型在 live 中自行构造了分类体系并交由技能打分）；无体系时技能会提示传入，不擅自编类别。
- `final_quality=0.70` 仍是 small 档 cap 先验，非真实测得质量。
- 内核（`propagate`/分层/开路语义）零改动；仅封装层 `LLMAgentBackend` + 新模块 `agent_skills.py`。

### 技能包路线图收口

第一层 retrieve（web_search/read_page/query_db）· 第二层 reason(+calculator)/verify(+cross_check/diff_text)· 第三层 extract/translate/classify/calculate/organize/summarize 领域工具 —— **三层八方向 + 九能力技能包全部落地**。每个电阻节点现在是"带独立角色提示词 + 可调用领域工具"的真·agent 实例。

---

## 自动并联（让接好的 LLM 当判官）（2026-08-02 深夜）

用户观察到：走 circuit 编排时，可并联的任务**默认被串起来**，只有依赖特别明显、或点名要求时才并联。  
诊断后定位根因，并按用户定调——**"接了 API key，就让模型自己判断能不能并联"**——把并联判定权交给已接好的 LLM，并让执行侧真并发。

### 根因（4 个断点）

1. **规划提示词默认串行**（主因）：`compiler/nl_parser.py` 的 LLM 系统提示原写 `dependencies … 省略=线性串联`。模型把 `dependencies` 当可选字段**多数情况省略** → 回退规则串行。
2. **实例拆分卡关键词**：提示只说"『同时/分别/各自』才拆 #N 实例"，所以"分析A、分析B"这类无并列词的被坍缩成单个能力，根本生不出并联层。
3. **plan.py 只在省略时套规则**：`circuit-planner/scripts/plan.py:511` `if goal.dependencies is None: infer_dependencies(...)`。规则 `infer_dependencies`（`router_auto.py:50`）只在**不同能力角色**间连边，独立组合返回 `None` → 标串联。
4. **执行侧假并联**：`runtime.py:280` 的 `propagate` 层内是纯 `for` 串行，延迟却用 `max` 假装并行——拓扑画成并联、实际仍逐个跑。

### 修复（让接好的 LLM 判，并执行侧真并发）

- **`nl_parser.py` 翻转提示词默认语义**：`dependencies` 改为"**默认并联**——省略或 `[]` 都视同全并联；仅当子任务间存在明确先后/数据依赖（『先X再Y』『基于X的结果』）才列依赖边"。  
  泛化实例拆分：任何"多个并列/独立对象或子任务"（不必带并列词）都拆成 `#N` 并行实例，**严禁坍缩**。模块 docstring 同步说明 LLM 现负责"依赖/并联判断，默认并联"。
- **`plan.py` 默认并联兜底**：`infer_dependencies` 返回 `None`（无语义边）时，改设 `goal.dependencies = []`（并联）而非留 `None`→串行；`[2.6]` 打印文案改为"无依赖则并联优先"。`--no-auto-route` 仍可回退旧默认串行。
- **`runtime.py` 真并发**：`propagate` 同层多节点（len>1）改用 `ThreadPoolExecutor` 并发执行（pred 皆在前层已算完，本层只写各自独立 key，无竞态）；单节点层仍串行。这是本次唯一改动运行时的地方，内核 `propagate` 语义/开路/分层不变。
- **透传已具备**：`plan.py._load_key` 默认读桌面 `key_tmp.txt`——所以"接好的 key"plan.py 会自动吃到，LLM 路天然开启，无需手动传参。

### 验证

- **离线 selftest**：
  - `nl_parser` 新增 ⑦（独立多子任务 LLM 回 `[]` ⇒ 并联）⑧（带『先X再Y』⇒ 保留依赖边，不强行并联）——全部离线通过；尾行"M4 nl_parser 离线自检全部通过 ✓"。
  - `router_auto` / `llm_agents` selftest **无回归**（22/22 + 4/4）。
- **runtime 并发测试**（sleep 后端，3 节点串行基线 1.20s / 并发预期 0.90s）：实测 **wall=0.902s**，明显小于串行求和，证真并发。
- **真·live（DeepSeek，key 在位）**：用 `GoalParser()` 解析"分析A公司的年度财报，分析B公司的年度财报，最后把两家的结论汇总成一份对比报告"（**无『并行』词**）→
  - `caps = ['reason', 'reason#2', 'summarize']`（模型自动拆出两个并行 reason）
  - `deps = [['reason','summarize'], ['reason#2','summarize']]`（两 reason 互不依赖→**并联**，共同喂 summarize）
  - 1.42s 完成。**对比旧行为**（坍缩单 reason + 默认串行），接好的模型已自主判出"分析A、分析B 独立可并"。

### 诚实边界（本批）

- **信任模型判断的代价**：若 LLM 把"其实有隐含先后/共享中间结论"的任务误判成独立，会错并联。保守守卫是"只有显式先后词才加边，其余默认并"——把误判风险压到最低，但仍存在。用户明确选择信任接好的模型当判官，此取舍已确认。
- 真并发的安全前提是 DAG 分层保证同层节点无相互依赖；本改动未改变分层算法。
- 本次是技能包之外**首改运行时**（`propagate` 加并发），其余内核语义零改动。

---

## 数据依赖分析：让 LLM 声明 input/output，引擎确定性算拓扑（2026-08-03）

上一节「让接好的 LLM 当判官」把并联判定权交给模型，但**依赖判断仍是概率性的**——模型可能误判"有依赖却判成并联"（危险侧）。用户的进一步洞察：**调度器不需要"判断能不能并联"，只需让 LLM 诚实声明每个子任务"吃什么(input)和产什么(output)"，依赖图由规则引擎按数据流向确定性算出**（编译器式数据依赖分析 / netlist 生成）。这把"依赖判断"从概率推理变成确定性规则匹配。

### 设计（相比上一节的改进）

- **LLM 只做擅长的事**：理解自然语言、拆子任务、声明 `inputs`/`outputs`。**不再让模型判先后**。
- **依赖图是代码算的，不是模型猜的**：子任务 X 的 `output` 被子任务 Y 的 `input` 引用 → 边 `[X, Y]`（Y 依赖 X，串联在 X 后）；互不引用 → **并联自然涌现**。
- **误并从根上消除**：上一节的"默认并联"误差偏误并（危险侧），靠反馈环兜底；本方案里并联是数据流向的必然结果，无依赖即并、有依赖才串，不存在"误判成并"的空间。
- **混合对齐兜底（按用户选定）**：产物名做归一化匹配（`_norm_token` 去空白/标点/大小写）；`input` 名无对应 `output`（命名漂移）→ **仅告警、不建边**，退为并联而非静默误并。
- **保留 LLM `dependencies` 可选覆盖**：仅用于"非数据、纯顺序"的软依赖（如叙事顺序），与引擎边合并，确定性、零 LLM 重判。

### 改动落点

- **`compiler/goal.py`**：`Goal` 新增 `subtasks` 字段（默认 `None`），`GOAL_JSON_SCHEMA` 补描述，`from_dict`/`to_dict` 透传（仅结构化透传，不强制校验内部字段）。
- **`compiler/router_auto.py`**：新增 `dependencies_from_subtasks(subtasks, override_deps)`——扫 `outputs` 建"归一化产物名→生产方"映射，对每条 `input` 命中则建边、未命中则告警；合并 LLM 软依赖覆盖；返回 subtask-id 边。新增 `_norm_token` 归一化。
- **`compiler/nl_parser.py`**：
  - `_build_messages` 提示词改写为 **subtasks schema**（每个 subtask = `{id, capability, inputs[], outputs[]}`），明确"不要自己判先后，引擎按 input/output 算"；few-shot 改为例 1（PDF 总结+核算+核对链式）、例 2（A/B 财报独立→汇总）。
  - `_parse_llm` 增加分支：含 `subtasks` → 走 `_goal_from_subtasks`（为每个 subtask 分配"能力名#实例序号"节点名，复用 `#N` 约定兼容 `compile_goal` 的 `_base` 剥离；引擎算边→翻译为节点名→`Goal.from_dict` 校验/环检测）。
  - 模块 docstring 同步：LLM 现只负责"声明 IO"，依赖判定移交规则引擎。
- **`circuit-planner/scripts/plan.py`**：`goal.subtasks` 存在且已算出 `dependencies` 时标记 `dataflow_routed`，打印 `[2.6] 数据依赖分析（netlist 式）…`；**复用既有 dependencies 消费路径，零破坏**。

### 验证

- **离线 selftest**：
  - `router_auto` 新增 ⑤（A/B 并联→共喂 C 确定性算边）⑥（未满足 input 仅告警不误并）⑦（LLM 软依赖覆盖合并）——全过（用例 1–7）。
  - `nl_parser` 新增 ⑨（LLM 声明 inputs/outputs → 引擎算出 `[retrieve→reason, retrieve#2→reason]`）⑩（未满足 input 退并联）——全过；尾行"M4 nl_parser 离线自检全部通过 ✓"。
  - `llm_agents` **无回归**。
- **真·live（DeepSeek，key 在位）**：用 `GoalParser()` 解析"查中国2025年GDP总量，查中国2025年人均GDP，然后做对比分析"→
  - `caps = ['retrieve', 'retrieve#2', 'reason']`
  - `subtasks`：A(retrieve, out=gdp_total) / B(retrieve, out=gdp_per_capita) / C(reason, in=[gdp_total, gdp_per_capita])
  - `deps = [['retrieve','reason'], ['retrieve#2','reason']]`（引擎从 input/output **确定性算出**，模型全程未判先后）
  - 1.59s。**对比上一节**：上一节模型要自己输出 `dependencies=[]`；本节模型只声明"我产 gdp_total / 我吃 gdp_total"，拓扑由代码保证正确。

### 诚实边界（本批）

- **命名漂移仍是唯一残余风险**：若 LLM 把某 subtask 的 `input` 名写成与上游 `output` 不一致（如上游 `gdp_total`、下游写 `china_gdp_total`），匹配失败 → 该边不建 → 退为并联。本方案通过**告警**把它暴露出来（不再静默误并），但无法自动纠正——根治靠提示词强调"复用上游 output 原名"。
- `subtasks` 仅作结构化透传/可追溯，下游拓扑完全由 `capabilities`+`dependencies`（节点名形式）驱动，故 `compile_goal`/`runtime` 零改动。
- 与上一节 `runtime` 真并发叠加：拓扑画成并联时，执行侧 `ThreadPoolExecutor` 真一起跑——"确定性拓扑 + 真并发"齐活。
- 本节 `component_io` 仅作结构化透传，当时 `runtime` 未改；**真正的运行期改动在下一节「线性关系自测」——`propagate` 内嵌了每个电阻的线性关系自测（属内核改动）**，详见下节。

---

## 线性关系自测：每个电阻都具备判断线性关系的能力（2026-08-03）

上一节把"串/并联判据"从模型概率判断变成了**确定性数据依赖分析**（LLM 声明 input/output → 引擎算拓扑）。但用户的核心洞察再往前推一步：**判据不该只在中央调度器规划阶段算一次，而要让"每一个电阻 agent 在运行时也具备判断线性关系的能力"**——即把"汇合完整性检查"下沉到每个节点。这样即便规划侧拓扑算错、或上游在运行期意外死亡/命名漂移，单个电阻也能在进入后端前自检"我声明的依赖到底有没有被上游真正交付"，缺则短路、不硬凑、不幻觉。

### 设计

- **判据 = 线性关系（数据依赖），这是串并联的唯一标准**：子任务 X 的产出是 Y 的输入 → 有线性关系 → 串联（Y 等 X）；否则并联。上一节由中央引擎在规划阶段判定一次；本节让**每个电阻在运行时再独立核一遍**，双保险。
- **自测机制（用户选定 A 运行时自测）**：每个电阻跑前，核对它声明的 `required_inputs`（线性关系）是否真的被其直接前驱信号的 `produced_outputs` 覆盖——
  - 全覆盖 → 线性关系成立，正常调后端；
  - 缺任一 → 短路返回 `Signal(ok=False, meta={"gate":"fail_linear","missing":[...]})`，**不调后端**，天然喂进已有的 `feedback`/`adc` 重试环。
- **无 `required_inputs` 的节点跳过**（默认零回归）：只有声明了数据依赖契约的电阻才触发自测。
- **产出信号累积透传 `produced_outputs`**：每个电阻产出的信号都打上 `produced_outputs = 自身声明产出 ∪ 所有上游产出`（经中间汇合/适配节点继续向下游转发）。于是下游只需看"直接前驱信号的 `produced_outputs`"即可核对线性关系，**不必追溯整条上游链**——这是修复"汇合节点不转发导致误 gate"的关键。

### 改动落点

- **`compiler/goal.py`**：`GOAL_JSON_SCHEMA` 新增 `component_io`（节点名→{required_inputs,produced_outputs}）；`Goal` 新增 `component_io: Optional[dict]=None` 字段（中文注释说明"runtime 在每个电阻跑前核对 required_inputs 是否被上游 produced_outputs 覆盖，缺则 gate:fail"）；`from_dict` 校验为 dict，`to_dict` 透传。
- **`compiler/nl_parser.py`**：`_goal_from_subtasks` 在生成节点的同时顺手产出 `component_io[node]={required_inputs:输入列表, produced_outputs:输出列表}` 写入 `goal_dict`。
- **`compiler/router.py`**：`route()` 取 `goal.component_io`，给单份电阻与冗余副本电阻组件贴上 `required_inputs`/`produced_outputs`（无则跳过）；`_rationale` 在存在 `component_io` 时追加"线性关系自测(每个电阻跑前核对 required_inputs⊆上游produced_outputs)"。
- **`circuit-agents/runtime.py`**（**本批实改运行时**）：`propagate()` 抽出内部 `_run_one(cid)`——先做线性关系自测（见设计），后调 `backend.run`，并给产出信号打 `produced_outputs` 累积透传标。单节点层与并发层（上一节 `ThreadPoolExecutor`）均调 `_run_one`，并发层仅读前层 out、写本节点独立 key，无竞态。新增 `selftest()`（S1–S5，详见验证）。
- **`compiler/llm_agents.py`**：`_build_messages` 若组件有 `required_inputs`/`produced_outputs`，在 user message 注入「【你的线性关系契约】」段，让电阻 agent 也意识到自己的数据依赖契约（上游未给齐须显式说明"依赖输入缺失"而非硬凑）；确定性闸门仍在 runtime，提示词只作软约束。

### 核心代码（`runtime.py` `_run_one`）

```python
req = comp.get("required_inputs")
if req:
    available = set()
    for p in self.pred[cid]:
        s = out.get(p)
        if s is not None and s.ok:
            available.update(s.meta.get("produced_outputs") or [])
    missing = [r for r in req if r not in available]
    if missing:
        return Signal(value=None, quality=0.0, ok=False, cost=0.0, latency_ms=0.0,
                      meta={"gate": "fail_linear", "missing": missing,
                            "required": list(req), "node": cid})
sig = self.backend.run(comp, ins)
# produced_outputs = 自身声明 ∪ 上游累积（经汇合透传）
upstream_outputs = set()
for s in ins:
    if s is not None and s.ok:
        upstream_outputs.update(s.meta.get("produced_outputs") or [])
own = comp.get("produced_outputs") or []
combined = list(dict.fromkeys(list(own) + list(upstream_outputs)))
if combined:
    sig.meta["produced_outputs"] = combined
return sig
```

### 验证

- **离线 selftest（`runtime.py` 内 S1–S5 全过）**：
  - S1 满足：A 产出 x，B 需 x → B 正常、`produced_outputs` 标正确。
  - S2 上游死：A `ok=False` → B 的 required 未满足 → `gate:fail_linear`（短路不调后端）。
  - S3 命名漂移：A 产 x，B 声明需 y（名不一致）→ 即便 A 正常，B 仍 `gate:fail_linear`（抓出依赖误判）。
  - S4 汇合透传：A→电容M→B，一致 IO 通过、无 gate（验证 `produced_outputs` 经汇合累积）。
  - S5 汇合漂移：A→M→B，B 需 y 经汇合仍被抓出 `gate:fail_linear`。
  - 旁证：`router_auto` 7 用例、`nl_parser` 全过、`llm_agents` 无回归。
- **真·live（DeepSeek，key 在位）**：GDP 例 `caps=['retrieve','retrieve#2','reason']`，`component_io` 正确（retrieve 产 gdp_total / retrieve#2 产 gdp_per_capita / reason 需两者）；
  - happy-path 因沙箱不可达 DuckDuckGo，`retrieve` 两节点 `ok=False` → 依赖它们的 `reason` 节点**正确触发 `gate:fail_linear`**（而非从空上下文幻觉）——正是用户想堵的缺口；
  - 一致 IO 在真实 Router 拓扑 `cap_2: ok=True gate=None`；漂移 IO `cap_2: gate=fail_linear missing=['gdp_total_typo']`（仅一个缺失，正确）。

### 诚实边界（本批）

- **汇合透传修复（本批关键 bug）**：初版 `produced_outputs` 只打在电阻自身产出上，中间电容汇合节点不转发，导致真实 Router 拓扑（cap_0→lmerge_0→cap_3→lmerge_1→cap_2）下 `cap_2` 的直接前驱是汇合节点（其 `produced_outputs=None`）→ 下游看到 available 为空 → **每个依赖节点都误判 `gate:fail_linear`**。修复为产出信号 `produced_outputs = 自身声明 ∪ 所有上游产出（累积透传）`，使汇合节点继续向下游转发产物名；新增 S4/S5 固化此行为。
- 自测是**运行期最后一道闸**：规划侧拓扑（上一节）算错 + 运行期上游死亡/漂移，都会被本闸抓住。但自测只能判"声明了 required 的依赖"——未声明依赖契约的节点仍不触发（零回归优先）。
- 命名漂移本批通过 `gate:fail_linear` **暴露**（而非静默误并/幻觉），但无法自动纠正——根治靠提示词强调"复用上游 output 原名" + 上一节 `_norm_token` 归一化告警双保险。
- 与上一节真并发叠加：并联层同层节点并发跑，每个仍各自做线性关系自测，互不影响。

---

## 技能默认调用 apikey（默认真模型，无 key 退回 mock）（2026-08-03）

用户要求：「将技能设置为默认调用 apikey，除非没有 apikey」。即把"默认走 mock、显式 `--backend real` 才用真模型"的默认行为翻转——**agent 跑技能（LLM 实例）与 NL 规划默认都走真实模型，检测不到 key 才退回 mock/离线**。

### 设计

- **统一 key 解析 `resolve_api_key()`**（compiler/backend_llm.py）：优先级 = 显式参数 > 环境变量 `DEEPSEEK_API_KEY`/`OPENAI_API_KEY`/`AGENT_API_KEY` > 本地文件 `~/Desktop/key_tmp.txt`（沿用 \_demo_llm_agents_run.py / \_verify_real.py 约定，明文 key 绝不 print/进日志）。空串 `""` 视作"显式强制离线"，不会回退到文件。
- **默认后端工厂 `get_default_backend()`**：解析到 key → 返回 `LLMAgentBackend`（真模型）；否则返回 `SimBackend`（mock）。即"有 key 才烧真模型，无 key 不触网"。
- **范围 = 执行 + 规划都改**（用户选定）：执行（`RealLLMBackend`/`run.py`/`demo.py` 默认用工厂）+ 规划（`GoalParser` 默认尝试真·LLM 规划，无 key 回退规则兜底）。

### 改动落点

- **compiler/backend_llm.py**：新增 `resolve_api_key()` + `KEY_FILE_PATH` 常量 + `get_default_backend()`（延迟 import `LLMAgentBackend` 避免循环依赖）；`RealLLMBackend.__init__` 的 `api_key` 解析改为 `resolve_api_key(api_key)`；模块 docstring 更新默认行为。
- **compiler/nl_parser.py**：`GoalParser.__init__` 的 `api_key` 解析改为 `resolve_api_key(api_key)`（顶部加 `from .backend_llm import resolve_api_key`）；模块 docstring 更新。
- **run.py**：新增 `--backend {auto,real,mock,sim}`（默认 auto）；`_build_backend()` 据模式选后端（auto=默认工厂 / real=强制真模型(无 key 报错退出) / mock,sim=强制 SimBackend 离线对照）。
- **compiler/demo.py**：`run_nl_case` 规划+执行默认走 `resolve_api_key()`+`get_default_backend()`（无 key 回退离线）；`api_key=""` 仍可强制离线；文档文案更新。

### 验证

- 离线 selftest 全过：`backend_llm`（6 用例，含新增"有 key 带 Bearer 头 / 无 key 不带"双校验）、`nl_parser`（10 用例）无回归。
- 默认行为实测（不触网）：env 空时 `resolve_api_key()` 读到 `~/Desktop/key_tmp.txt` → 非空；`get_default_backend()` 返回 `LLMAgentBackend`（真模型）。内存覆盖常量+清空 env 模拟无 key → `resolve_api_key()` 返回 `""`、`get_default_backend()` 返回 `SimBackend`（mock）。
- `run.py --help` 显示 `--backend`；`demo.py` import OK；`run_nl_case(..., api_key="", runs=2)` 离线跑通（规则解析 + SimBackend 仿真，无网络）。

### 诚实边界

- **真模型会烧 key / 出网**：默认 auto 在有 key 时走真实 LLM 调用。`demo.py` 原 `_simulate` 跑 `runs=300` 次取均值——真模型下等于 300 次真实 API 调用，成本显著。快速真模型验证建议控制次数，或用 `run.py --backend real --runs 1`，或移除/改名 `key_tmp.txt` 退回离线。
- **key 来源优先级固定**：环境变量优先于文件；若想只用文件 key，确保未设 `DEEPSEEK_API_KEY` 等。强制离线用 `--backend mock`（run.py）或 `api_key=""`（demo）。
- `resolve_api_key("")` 现在"强制无 key"（不回退文件）——与旧 `api_key=None` 语义不同；旧 `None` 现在表示"按默认解析（env→文件）"。

---

## 命名漂移符号映射表（确定性「转接头」）（2026-08-03）

上一节把命名漂移通过 `gate:fail_linear` **暴露**（而非静默误并），但只能"发现"、不能"纠正"。本批按用户加固方案升级为**确定性编译期符号映射表**：在 netlist 生成阶段，用纯规则找出"名字不同但语义等价"的变量对 `{下游输入: 上游产物}`，注入拓扑，下游按映射取数。即把"告警提示"升级为"转接头"——**不靠更聪明的 AI，靠确定性编译步骤**。

### 设计（与用户对齐）

- **唯一残余风险定位**：模型犯错/节点失败/格式不匹配/调度误判都有对应闸（质量门、反馈环、格式适配器、默认并联+规则推断）；唯独"命名漂移"是语义层微偏差——上游 `gdp_china_2024` / 下游 `china_gdp_2024`，连线在、信号在、标签没对上 → "接触不良"，依赖被误判未满足 → 误并联 + 运行期 `gate:fail_linear`。
- **加固 = 符号映射表（转接头）**：规划/编译阶段增加确定性变量映射检查；`B.inputs[Y]` 与上游 `A.outputs[X]` 等价但名字不同 → 不直接断开，生成 `{Y: X}` 映射注入拓扑，下游按映射取数。
- **确定性等价判定（纯规则，零 LLM 成本）**，经 AskUserQuestion 确认选"纯确定性规则"：
  - ① token 集合相等（含同义词归一）：`gdp_china_2024` 与 `china_gdp_2024` 拆成 {gdp, china, 2024} 集合相等 → 判定等价（抓词序/同义漂移）；
  - ② Levenshtein 比 ≥0.85 且非包含子串关系 → 抓 `gdp_t0tal` ← `gdp_total` 这类拼写/编码漂移（非包含子串防 `gdp_total` vs `gdp_total_2` 误并）；
  - 可选同义词表 `SYNONYMS`（如 国内生产总值→gdp）做跨写法/跨语言兜底，可空。
- **判不出 / 歧义 → 维持现状（零回归，经确认选"维持现状"）**：规则判不出（如 `gdp_total_typo` vs `gdp_total`）或同时漂移匹配多个上游（歧义多解）→ 不映射、不建边、退并联、运行期诚实 `gate:fail_linear` 暴露。绝不静默误并。
- **零回归原则**：映射表只对"声明了依赖契约（subtasks → component_io）的节点"生成；无契约节点不触发、其运行期自测本就跳过。

### 改动落点

- **compiler/router_auto.py**：
  - 新增 `_tokens()`（变量名拆成成分 token 集合，含同义词归一）、`_lev_ratio()`（difflib SequenceMatcher.ratio，纯 stdlib）、`SYNONYMS` 表、`_equiv_drift()`（①② 双规则）。
  - 新增 `_analyze_dependencies(subs, override_deps)` → 返回 `(edges, input_maps)`：`input_maps` 为 `{consumer_subtask_id: {下游输入名: 上游实际产物名}}`，**仅含经映射判定等价、但名称不一致的对**（精确同名不进表，已是同一变量）。
  - `dependencies_from_subtasks()` 保留兼容签名，仅返回 `edges[0]`（历史调用/selftest 零改动）。
- **compiler/nl_parser.py**：`_goal_from_subtasks` 改调 `_analyze_dependencies`，把 `input_maps` 按节点名写入 `component_io[node]["input_map"]`（docstring 同步）。
- **compiler/router.py**：`route()` 在 `io` 存在时把 `input_map` 透传到电阻 comp（单份 + 冗余分支）；`_rationale` 在有映射节点时注明"命名漂移符号映射表(N 个节点已注入 input_map 转接头)"。
- **runtime.py**：`_run_one` 线性关系自测把 `required_inputs` 经 `input_map` 翻译为上游实际产物名再核对 `available`（仍报下游原名便于人读）；映射只来自编译期确定性等价判定，翻译安全、零回归。
- **compiler/llm_agents.py**：`_build_messages` 在线性关系契约中注入"你的输入 Y 由上游 X 满足（符号映射）"，让电阻 agent 知道"我的 Y 由上游的 X 满足"，避免它自己重新猜连线造成二次漂移。

### 验证

- **离线 selftest 全过**：
  - `router_auto` 新增 8/9/10/11 四用例：词序漂移 `china_gdp_2024←gdp_china_2024` 建边+映射；Levenshtein `gdp_t0tal←gdp_total` 建边+映射；`gdp_total_typo` 判不出→维持现状(不退并联)；歧义多解→放弃自动映射(防静默误并)。历史 1–7 无回归。
  - `nl_parser` 新增用例 11：子任务分解 + 命名漂移映射（下游 `china_gdp_2024`←上游 `gdp_china_2024`）建边 + `input_map` 注入 `component_io`。
  - `runtime` 新增 S6/S7：S6 映射消解漂移（`input_map{y:x}` → B 通过，转接头生效）；S7 映射在但上游真没产出 x → 仍诚实 `gate:fail_linear`（防静默误判掩盖缺数据）。
  - `llm_agents` 新增用例 10：含 `input_map` 的电阻提示词显式声明「china_gdp_2024 ← 上游 gdp_china_2024」。
  - `backend_llm` 6 用例无回归。
- **端到端冒烟（无 key/无网络）**：注入式假 LLM 产 `subtasks`（A 产 `gdp_china_2024`、B 产 `gdp_per_capita`、C 需 `china_gdp_2024`+`gdp_per_capita`）→ `GoalParser` 生成 `component_io[reason].input_map={china_gdp_2024:gdp_china_2024}` → `Router.route` 透传到 `reason` 电阻 comp → `Circuit.execute`（SimBackend）成功（`final_quality=0.694`，reason 节点 `ok=True`，无 `gate:fail`）。全链路映射闭合。

### 诚实边界（本批）

- 符号映射是**编译期确定性转接头**，不是 AI 猜测：只在"token 集合相等 / Levenshtein≥0.85 且非包含"这一窄判定内生效；超出即维持现状（告警 + 退并联 + 运行期 gate），绝不静默误并——这是有意为之的保守边界。
- 歧义（一个下游输入同时漂移匹配多个上游产物）**刻意不自动映射**，避免把 A 的产出错接到 B 的需求；此时退回人工/显式 `dependencies` 声明。
- 映射只做"翻译取数"：运行期仍核对"映射后的上游实际产物名"是否真被产出；若上游真没产出该名（S7），照样 `gate:fail_linear`——映射不掩盖真实缺数据。
- 真正并发安全前提不变：DAG 分层正确（同层并联节点互不依赖）；映射是本层内 per-节点翻译，不引入跨层竞争。

---

## 命名漂移符号映射表（确定性「转接头」）（2026-08-03）

上一节把命名漂移通过 `gate:fail_linear` **暴露**（而非静默误并），但只能"发现"、不能"纠正"。本批按用户加固方案升级为**确定性编译期符号映射表**：在 netlist 生成阶段，用纯规则找出"名字不同但语义等价"的变量对 `{下游输入: 上游产物}`，注入拓扑，下游按映射取数。即把"告警提示"升级为"转接头"——**不靠更聪明的 AI，靠确定性编译步骤**。

### 设计（与用户对齐）

- **唯一残余风险定位**：模型犯错/节点失败/格式不匹配/调度误判都有对应闸（质量门、反馈环、格式适配器、默认并联+规则推断）；唯独"命名漂移"是语义层微偏差——上游 `gdp_china_2024` / 下游 `china_gdp_2024`，连线在、信号在、标签没对上 → "接触不良"，依赖被误判未满足 → 误并联 + 运行期 `gate:fail_linear`。
- **加固 = 符号映射表（转接头）**：规划/编译阶段增加确定性变量映射检查；`B.inputs[Y]` 与上游 `A.outputs[X]` 等价但名字不同 → 不直接断开，生成 `{Y: X}` 映射注入拓扑，下游按映射取数。
- **确定性等价判定（纯规则，零 LLM 成本）**，经 AskUserQuestion 确认选"纯确定性规则"：
  - ① token 集合相等（含同义词归一）：`gdp_china_2024` 与 `china_gdp_2024` 拆成 {gdp, china, 2024} 集合相等 → 判定等价（抓词序/同义漂移）；
  - ② Levenshtein 比 ≥0.85 且非包含子串关系 → 抓 `gdp_t0tal` ← `gdp_total` 这类拼写/编码漂移（非包含子串防 `gdp_total` vs `gdp_total_2` 误并）；
  - 可选同义词表 `SYNONYMS`（如 国内生产总值→gdp）做跨写法/跨语言兜底，可空。
- **判不出 / 歧义 → 维持现状（零回归，经确认选"维持现状"）**：规则判不出（如 `gdp_total_typo` vs `gdp_total`）或同时漂移匹配多个上游（歧义多解）→ 不映射、不建边、退并联、运行期诚实 `gate:fail_linear` 暴露。绝不静默误并。
- **零回归原则**：映射表只对"声明了依赖契约（subtasks → component_io）的节点"生成；无契约节点不触发、其运行期自测本就跳过。

### 改动落点

- **compiler/router_auto.py**：
  - 新增 `_tokens()`（变量名拆成成分 token 集合，含同义词归一）、`_lev_ratio()`（difflib SequenceMatcher.ratio，纯 stdlib）、`SYNONYMS` 表、`_equiv_drift()`（①② 双规则）。
  - 新增 `_analyze_dependencies(subs, override_deps)` → 返回 `(edges, input_maps)`：`input_maps` 为 `{consumer_subtask_id: {下游输入名: 上游实际产物名}}`，**仅含经映射判定等价、但名称不一致的对**（精确同名不进表，已是同一变量）。
  - `dependencies_from_subtasks()` 保留兼容签名，仅返回 `edges[0]`（历史调用/selftest 零改动）。
- **compiler/nl_parser.py**：`_goal_from_subtasks` 改调 `_analyze_dependencies`，把 `input_maps` 按节点名写入 `component_io[node]["input_map"]`（docstring 同步）。
- **compiler/router.py**：`route()` 在 `io` 存在时把 `input_map` 透传到电阻 comp（单份 + 冗余分支）；`_rationale` 在有映射节点时注明"命名漂移符号映射表(N 个节点已注入 input_map 转接头)"。
- **runtime.py**：`_run_one` 线性关系自测把 `required_inputs` 经 `input_map` 翻译为上游实际产物名再核对 `available`（仍报下游原名便于人读）；映射只来自编译期确定性等价判定，翻译安全、零回归。
- **compiler/llm_agents.py**：`_build_messages` 在线性关系契约中注入"你的输入 Y 由上游 X 满足（符号映射）"，让电阻 agent 知道"我的 Y 由上游的 X 满足"，避免它自己重新猜连线造成二次漂移。

### 验证

- **离线 selftest 全过**：
  - `router_auto` 新增 8/9/10/11 四用例：词序漂移 `china_gdp_2024←gdp_china_2024` 建边+映射；Levenshtein `gdp_t0tal←gdp_total` 建边+映射；`gdp_total_typo` 判不出→维持现状(不退并联)；歧义多解→放弃自动映射(防静默误并)。历史 1–7 无回归。
  - `nl_parser` 新增用例 11：子任务分解 + 命名漂移映射（下游 `china_gdp_2024`←上游 `gdp_china_2024`）建边 + `input_map` 注入 `component_io`。
  - `runtime` 新增 S6/S7：S6 映射消解漂移（`input_map{y:x}` → B 通过，转接头生效）；S7 映射在但上游真没产出 x → 仍诚实 `gate:fail_linear`（防静默误判掩盖缺数据）。
  - `llm_agents` 新增用例 10：含 `input_map` 的电阻提示词显式声明「china_gdp_2024 ← 上游 gdp_china_2024」。
  - `backend_llm` 6 用例无回归。
- **端到端冒烟（无 key/无网络）**：注入式假 LLM 产 `subtasks`（A 产 `gdp_china_2024`、B 产 `gdp_per_capita`、C 需 `china_gdp_2024`+`gdp_per_capita`）→ `GoalParser` 生成 `component_io[reason].input_map={china_gdp_2024:gdp_china_2024}` → `Router.route` 透传到 `reason` 电阻 comp → `Circuit.execute`（SimBackend）成功（`final_quality=0.694`，reason 节点 `ok=True`，无 `gate:fail`）。全链路映射闭合。

### 诚实边界（本批）

- 符号映射是**编译期确定性转接头**，不是 AI 猜测：只在"token 集合相等 / Levenshtein≥0.85 且非包含"这一窄判定内生效；超出即维持现状（告警 + 退并联 + 运行期 gate），绝不静默误并——这是有意为之的保守边界。
- 歧义（一个下游输入同时漂移匹配多个上游产物）**刻意不自动映射**，避免把 A 的产出错接到 B 的需求；此时退回人工/显式 `dependencies` 声明。
- 映射只做"翻译取数"：运行期仍核对"映射后的上游实际产物名"是否真被产出；若上游真没产出该名（S7），照样 `gate:fail_linear`——映射不掩盖真实缺数据。
- 真正并发安全前提不变：DAG 分层正确（同层并联节点互不依赖）；映射是本层内 per-节点翻译，不引入跨层竞争。

---

## 真·在线链路验证（2026-08-02）

按用户确认的「需要」，跑通真实链路（DeepSeek 真模型）验证命名漂移符号映射表是否真生效：

- **真实规划 code path（1 次 DeepSeek 调用）**：`GoalParser(api_key).parse(nl)` 把「分析步骤输入变量命名为 china_gdp_2024」写进 NL 后，DeepSeek **自觉对齐了上下游命名**（`retrieve` 产出 `china_gdp_2024`、下游 `reason` 输入也是 `china_gdp_2024`）→ **未触发漂移、不建映射**。这正是零回归设计：无漂移 ⇒ 不建边/不建映射，映射 code path 真实跑过但正确空转。
- **确定性漂移 + 真后端执行（3 个电阻 = 3 次 DeepSeek 调用，总成本≈$0.0158）**：构造下游输入 `china_gdp_2024`、上游产出 `gdp_china_2024`/`gdp_per_capita` → router 真实捕获并打日志「符号映射(命名漂移修复): C 的 china_gdp_2024 ← A 的 gdp_china_2024」；编译后 `cap_2` 带 `input_map={china_gdp_2024:gdp_china_2024}`；`Circuit.propagate()`（LLMAgentBackend / DeepSeek）执行结果：**`cap_0`/`cap_1`/`cap_2` 全部 ok=True，`cap_2` 无 `gate:fail_linear`，链路闭合**。
  - 关键判据：若无 `input_map`，`cap_2.required_inputs=['china_gdp_2024',...]` 不在上游 `produced_outputs(['gdp_china_2024',...])` 中 → 必被 `gate:fail_linear` 闸断；实测 ok=True 证明映射在**真实 LLM 后端**下真正消解了漂移。
- **提示词核验**：`cap_2` 电阻 user 提示词显式声明「你声明的必要输入…其中经符号映射由上游实际产物满足：china_gdp_2024 ← 上游的 gdp_china_2024」——转接头已告知 agent，避免二次漂移。
- **验证脚本**：`compiler/verify_drift_smoke.py`（参数化 CI 冒烟：默认离线 subtasks+期望 input_map 走 SimBackend 不烧 key、确定性可重复、exit 0/1；`--online --nl` 走真实 LLM 规划，`--execute` 再跑真后端证明链路闭合；key 仅从 `~/Desktop/key_tmp.txt` 读，明文不落盘/不 print）。

## **结论**：命名漂移符号映射表在「真实 LLM 规划 + 真实 LLM 执行」全链路下生效且零回归——有漂移时确定性修好、无漂移时绝不误建映射。

## 真·在线链路验证（2026-08-02）

按用户确认的「需要」，跑通真实链路（DeepSeek 真模型）验证命名漂移符号映射表是否真生效：

- **真实规划 code path（1 次 DeepSeek 调用）**：`GoalParser(api_key).parse(nl)` 把「分析步骤输入变量命名为 china_gdp_2024」写进 NL 后，DeepSeek **自觉对齐了上下游命名**（`retrieve` 产出 `china_gdp_2024`、下游 `reason` 输入也是 `china_gdp_2024`）→ **未触发漂移、不建映射**。这正是零回归设计：无漂移 ⇒ 不建边/不建映射，映射 code path 真实跑过但正确空转。
- **确定性漂移 + 真后端执行（3 个电阻 = 3 次 DeepSeek 调用，总成本≈$0.0158）**：构造下游输入 `china_gdp_2024`、上游产出 `gdp_china_2024`/`gdp_per_capita` → router 真实捕获并打日志「符号映射(命名漂移修复): C 的 china_gdp_2024 ← A 的 gdp_china_2024」；编译后 `cap_2` 带 `input_map={china_gdp_2024:gdp_china_2024}`；`Circuit.propagate()`（LLMAgentBackend / DeepSeek）执行结果：**`cap_0`/`cap_1`/`cap_2` 全部 ok=True，`cap_2` 无 `gate:fail_linear`，链路闭合**。
  - 关键判据：若无 `input_map`，`cap_2.required_inputs=['china_gdp_2024',...]` 不在上游 `produced_outputs(['gdp_china_2024',...])` 中 → 必被 `gate:fail_linear` 闸断；实测 ok=True 证明映射在**真实 LLM 后端**下真正消解了漂移。
- **提示词核验**：`cap_2` 电阻 user 提示词显式声明「你声明的必要输入…其中经符号映射由上游实际产物满足：china_gdp_2024 ← 上游的 gdp_china_2024」——转接头已告知 agent，避免二次漂移。
- **验证脚本**：`compiler/verify_drift_smoke.py`（参数化 CI 冒烟：默认离线 subtasks+期望 input_map 走 SimBackend 不烧 key、确定性可重复、exit 0/1；`--online --nl` 走真实 LLM 规划，`--execute` 再跑真后端证明链路闭合；key 仅从 `~/Desktop/key_tmp.txt` 读，明文不落盘/不 print）。

## **结论**：命名漂移符号映射表在「真实 LLM 规划 + 真实 LLM 执行」全链路下生效且零回归——有漂移时确定性修好、无漂移时绝不误建映射。

## 命名漂移 · 参数化 CI 冒烟脚本（2026-08-03）

把真实验证脚本参数化为可接 CI 的冒烟工具 `compiler/verify_drift_smoke.py`（用户确认「两者都要：默认离线，--online 走 NL」）：

- **默认离线模式（CI 主用，不烧 key / 不联网）**：`--subtasks '<JSON>'`（或 `--subtasks-file` / `--demo` 内置样例）+ 可选 `--expect '<节点→{下游:上游}>'` 或 `--expect-contains '<{下游:上游}>'`。  
  流程：`_goal_from_subtasks`（确定性）→ `compile_goal` → `Circuit.propagate()`（SimBackend）。三级断言：① 生成的 input_maps 与期望一致；② 带映射电阻 ok=True、无 `gate:fail_linear`（映射在 runtime 真正消解漂移）；③ 因果性——去掉所有 input_map 重跑，原漂移节点必 `gate:fail_linear`（证明映射承重，非摆设）。
- **在线模式 `--online --nl '<NL>'`**：调真实 `GoalParser`（DeepSeek）规划，报告产出的 component_io / input_map；可加 `--expect` 断言（真模型非确定性，best-effort）；加 `--execute` 再跑一次真后端（多几次真实调用）证明链路闭合。需 `~/Desktop/key_tmp.txt` 或环境变量。
- **退出码**：0=通过，1=断言失败，2=用法/缺 key。可直接接 CI（GitHub Actions step 例：`python compiler/verify_drift_smoke.py --demo --expect '{"reason#3":{"china_gdp_2024":"gdp_china_2024"}}'`）。
- **实测**（离线五组）：`--demo` 通过；`--demo --expect` 正确/错误分别 exit 0/1（断言真实生效）；无漂移 `--expect '{}'` 通过（零回归）；`--expect-contains` 通过。在线 `--online --nl` 规划 1 次调用 exit 0（真实模型自觉对齐命名、未触发漂移，符合零回归）。
- 旧的 `_verify_drift_real.py` 已由本脚本完全覆盖并删除。

---

## 命名漂移 · 参数化 CI 冒烟脚本（2026-08-03）

把真实验证脚本参数化为可接 CI 的冒烟工具 `compiler/verify_drift_smoke.py`（用户确认「两者都要：默认离线，--online 走 NL」）：

- **默认离线模式（CI 主用，不烧 key / 不联网）**：`--subtasks '<JSON>'`（或 `--subtasks-file` / `--demo` 内置样例）+ 可选 `--expect '<节点→{下游:上游}>'` 或 `--expect-contains '<{下游:上游}>'`。  
  流程：`_goal_from_subtasks`（确定性）→ `compile_goal` → `Circuit.propagate()`（SimBackend）。三级断言：① 生成的 input_maps 与期望一致；② 带映射电阻 ok=True、无 `gate:fail_linear`（映射在 runtime 真正消解漂移）；③ 因果性——去掉所有 input_map 重跑，原漂移节点必 `gate:fail_linear`（证明映射承重，非摆设）。
- **在线模式 `--online --nl '<NL>'`**：调真实 `GoalParser`（DeepSeek）规划，报告产出的 component_io / input_map；可加 `--expect` 断言（真模型非确定性，best-effort）；加 `--execute` 再跑一次真后端（多几次真实调用）证明链路闭合。需 `~/Desktop/key_tmp.txt` 或环境变量。
- **退出码**：0=通过，1=断言失败，2=用法/缺 key。可直接接 CI（GitHub Actions step 例：`python compiler/verify_drift_smoke.py --demo --expect '{"reason#3":{"china_gdp_2024":"gdp_china_2024"}}'`）。
- **实测**（离线五组）：`--demo` 通过；`--demo --expect` 正确/错误分别 exit 0/1（断言真实生效）；无漂移 `--expect '{}'` 通过（零回归）；`--expect-contains` 通过。在线 `--online --nl` 规划 1 次调用 exit 0（真实模型自觉对齐命名、未触发漂移，符合零回归）。
- 旧的 `_verify_drift_real.py` 已由本脚本完全覆盖并删除。
---

## 主项目最小可行功能推进（2026-08-03 续）

用户指令「继续推进主项目所有最小可行功能」→ 对齐后确认**全部推进** A+B+C+D（按依赖顺序，每块动手前再对齐）：
**A 汇合节点完整性检查 / B 规划前确认环 / C 异构校验 / D 3.5 多任务进化增强**。
以下分块记录。

### A. 汇合节点完整性检查（runtime.py，2026-08-03 续，已完成）

- **动机**：原内核里汇合（capacitor）节点只要上游信号 `ok` 就放行，不校验"承运值是否空壳"。
  于是「上游产出 x 但值为空串」会带着 x 这个名字一路透传到下游、下游线性关系闸因名字存在而放过 →
  空壳数据进入推理。这是"假装有数据"的静默bug。
- **改动落点（全在 `runtime.py`，内核零回归）**：
  - 新增模块函数 `_value_empty(v)`（None/空串/纯空白/全空聚合 → True）+ `_completeness_missing(ins, fields)`。
  - `Circuit.__init__` 新增开关 `self.join_completeness = spec.get("join_completeness", True)`（默认开，spec 可关）。
  - `Circuit._run_one` 在电容节点放出信号前，计算「下游要求且本节点转发」的字段
    （`downstream_req` = 后继电阻的 `required_inputs` ∩ 本节点 `produced_outputs`），若任一字段的上游承运值为空壳 →
    置 `sig.ok=False` 且 `sig.meta["gate"]="incomplete"`、`incomplete_fields=[...]`。
- **连锁效果（与既有机制协同）**：电容 `gate=incomplete` → 下游电阻因前驱不 `ok` → 触发 `gate:fail_linear`
  → `CircuitExecutor` 自动补数闭环救活。**零回归**：仅对"下游要求且本节点转发"的字段生效，真实非空数据不受影响。
- **验证**：
  - `runtime.py` 离线自检新增 **S8**：A 产出空壳 x → 电容 M `gate=incomplete`(ok=False) → B `gate:fail_linear`；
    对照 `join_completeness=False` 时 B 旧行为（空壳放行）证明检查确为新增。
  - `circuit_executor_selftest` 新增**端到端协同**：A 空壳 x → M `incomplete` → B `fail_linear` → 执行器按 B 的 filler 自动补 x → B ok。
  - 全量 `python runtime.py` 通过（S1–S8 + CircuitExecutor 补数/动态技能/观察窗 + 3.5 进化，无 key/无网）。
- **诚实边界**：完整性检查默认只挂在 `capacitor`（汇合节点）；`format_adapter`/`diode` 等中间节点未挂（如需可后续扩）。
  `mode="any"` 冗余汇合场景下，只要任一副本承运值非空即不算 incomplete（与"任一副本存活"语义一致）。
---

## 主项目最小可行功能推进（2026-08-03 续）

用户指令「继续推进主项目所有最小可行功能」→ 对齐后确认**全部推进** A+B+C+D（按依赖顺序，每块动手前再对齐）：
**A 汇合节点完整性检查 / B 规划前确认环 / C 异构校验 / D 3.5 多任务进化增强**。
以下分块记录。

### A. 汇合节点完整性检查（runtime.py，2026-08-03 续，已完成）

- **动机**：原内核里汇合（capacitor）节点只要上游信号 `ok` 就放行，不校验"承运值是否空壳"。
  于是「上游产出 x 但值为空串」会带着 x 这个名字一路透传到下游、下游线性关系闸因名字存在而放过 →
  空壳数据进入推理。这是"假装有数据"的静默bug。
- **改动落点（全在 `runtime.py`，内核零回归）**：
  - 新增模块函数 `_value_empty(v)`（None/空串/纯空白/全空聚合 → True）+ `_completeness_missing(ins, fields)`。
  - `Circuit.__init__` 新增开关 `self.join_completeness = spec.get("join_completeness", True)`（默认开，spec 可关）。
  - `Circuit._run_one` 在电容节点放出信号前，计算「下游要求且本节点转发」的字段
    （`downstream_req` = 后继电阻的 `required_inputs` ∩ 本节点 `produced_outputs`），若任一字段的上游承运值为空壳 →
    置 `sig.ok=False` 且 `sig.meta["gate"]="incomplete"`、`incomplete_fields=[...]`。
- **连锁效果（与既有机制协同）**：电容 `gate=incomplete` → 下游电阻因前驱不 `ok` → 触发 `gate:fail_linear`
  → `CircuitExecutor` 自动补数闭环救活。**零回归**：仅对"下游要求且本节点转发"的字段生效，真实非空数据不受影响。
- **验证**：
  - `runtime.py` 离线自检新增 **S8**：A 产出空壳 x → 电容 M `gate=incomplete`(ok=False) → B `gate:fail_linear`；
    对照 `join_completeness=False` 时 B 旧行为（空壳放行）证明检查确为新增。
  - `circuit_executor_selftest` 新增**端到端协同**：A 空壳 x → M `incomplete` → B `fail_linear` → 执行器按 B 的 filler 自动补 x → B ok。
  - 全量 `python runtime.py` 通过（S1–S8 + CircuitExecutor 补数/动态技能/观察窗 + 3.5 进化，无 key/无网）。
- **诚实边界**：完整性检查默认只挂在 `capacitor`（汇合节点）；`format_adapter`/`diode` 等中间节点未挂（如需可后续扩）。
  `mode="any"` 冗余汇合场景下，只要任一副本承运值非空即不算 incomplete（与"任一副本存活"语义一致）。

### B. 规划前确认环（circuit-planner/scripts/plan.py，2026-08-03 续，已完成）

- **动机（用户待办）**：原架构 NL 直接编译，调度器不回述理解；若解析错方向，整条链编译错。需要"规划前确认环"——先回述理解、用户确认再编译。
- **落点（`~/.workbuddy/skills/circuit-planner/scripts/plan.py`，规划入口 CLI）**：
  - 新增纯函数 `_build_recap(nl, goal, mode)`：把规划器理解回述为可读摘要——能力步骤 / 依赖布线(并联·串联·DAG) / 约束(reliability/min_quality/max_cost/…) / 反馈环 / 子任务 IO。
  - 新增 `--confirm` 开关：在**编译前**、且仅当**交互终端(TTY)**时打印摘要并 `input()` 等用户确认；非交互/CI/常驻自动模式（`--confirm` 未给，或非 TTY）**不阻断**，保持原自动规划行为。
  - 新增 `--self-test`：离线断言 `_build_recap` 含关键字段（能力/依赖/DAG/约束），无需 TTY/网络。
- **设计取舍**：确认环默认关闭，避免与"circuit-planner 常驻即自动"的偏好冲突；它是一项**安全闸门**（用户跑 plan.py 想先核对理解时显式 `--confirm` 开启）。取消编译返回 0（正常取消非错误）。
- **验证**：`python plan.py --self-test` 离线通过；真实 NL 目标跑通（非 TTY 下确认环自动跳过，不阻断流程）。
- **诚实边界**：`--confirm` 依赖真实 TTY 的 `input()`，CI/管道/常驻自动场景自动失效（不卡流程）；确认发生在「解析后、模板匹配/编译前」，回述的是 NL 解析出的初始理解（模板改写前的 goal）。

### C. 异构校验（runtime.py + compiler/backend_llm.py + circuit-planner/scripts/plan.py，2026-08-03 续，已完成）

- **动机（用户待办）**：原内核的质量门 / verify 节点与主链路同走一个 LLM 后端。同源 LLM 的系统性错误可能被自己的质量门放过（"自己审自己"）。需要"异构校验"——verify 节点走**独立后端（不同模型/供应商）**。
- **落点**：
  - `runtime.py`：
    - `Circuit.__init__(spec, backend, verify_backend=None)` 新增 `verify_backend`；`Circuit._backend_for(comp)` 路由——组件 `label` 以 `verify` 前缀（如 `verify#quality`、`verify`）命中时走 `verify_backend`，否则走主 `backend`。
    - `_run_one` 改用 `_backend_for(comp).run(...)`；当 `verify_backend` 未配置但组件是 verify 时，退回主 backend 并 `sig.meta["warnings"].append("hetero_verify_unconfigured")`（诚实告警，不静默）。
    - `_rerun_with_filled` 同样走 `_backend_for`（补数重跑也保持异构）。
    - `CircuitExecutor.__init__` 新增 `verify_backend` 参数，覆盖 `circuit.verify_backend`（子电路递归继承 `self.verify_backend`，保持异构一致）。
  - `compiler/backend_llm.py`：新增 `resolve_verify_backend()` —— 读 `VERIFY_API_KEY` / `VERIFY_DEEPSEEK_API_KEY` 与 `VERIFY_API_BASE` / `VERIFY_OPENAI_BASE_URL`，配置齐全则返回一个独立 `LLMAgentBackend(...)`；否则返回 `None`（触发退回主 backend + 告警）。
  - `circuit-planner/scripts/plan.py`：`_run_real` 接线 `Circuit(spec, backend, verify_backend=resolve_verify_backend())`，端到端启用异构校验。
  - 新增 `hetero_verify_selftest()`（`_TagBackend` 标记 main/verify），纳入 `runtime.py __main__`。
- **设计取舍**：verify 节点识别靠 `label` 前缀 `verify`（规划器/模板把校验类电阻标 `verify#xxx` 即可启用异构）；不强制改名主链路组件。**零回归**：未配置 `VERIFY_*` 时整条链退回旧行为（verify 走主 backend），仅多一条 `hetero_verify_unconfigured` 告警，普通执行不受影响。
- **验证**：`hetero_verify_selftest` 离线通过——verify 节点走独立 backend、其余走主 backend、未配置时正确退回并告警；全量 `python runtime.py` 含该用例通过（无 key/无网）。
- **诚实边界**：异构校验只解决"同源 LLM 自验证"风险，不保证 verify 后端一定更正确；必须用户另行配置 `VERIFY_*` 环境变量（独立 key/模型）才真正异构。未配置时它是"显性降级"而非"假装异构"。

### D. 3.5 多任务进化增强（runtime.py，2026-08-03 续，已完成）

- **动机（用户待办）**：原 `maybe_evolve` 只认「JSON 列表串且长度 > 阈值」，触发面窄、阈值写死、且业务侧无法主动要求「下一步就深挖」。需要把 3.5 进化从「仅 JSON 列表>阈值」扩为可配/更通用触发。
- **落点（全在 `runtime.py`，C 异构校验代码之上，内核零回归）**：
  - `_as_list` → `_countable(val)`：归一为 `(items, count)`，接受 **list/tuple/set/dict(按键数)/JSON 列表串**；普通字符串/数字/None 返回 `(None, 0)`（不触发，零误触发）。
  - `maybe_evolve` 重写为两级触发：
    - **② 显式提示队列** `state._evolve_requests`（形如 `[{"key":"frameworks","top_k":3}]`）：组件/技能可绕过阈值强制进化（top_k 可单条覆盖），置 `sub["_evolve_explicit"]=True`。
    - **① 泛化自动发现**：对任意可计数集合，若 `count > 生效阈值` 则取前 `evolve_top_k` 条生成。
  - **③ 阈值/取数可配**：生效 `evolve_threshold`/`evolve_top_k` 先读 `circuit.spec` 覆盖构造参数；并写入 `self._evolve_thr`/`self._evolve_top_k` 供观察窗 `evolve_detect` 事件显示真实生效值；事件新增 `explicit` 字段。
  - `run()` 的 `evolve_detect` 事件改用生效阈值并显示 `explicit`；子电路递归契约不变（`evolve_enabled=False` 防无限递归）。
  - 新增 `evolve_enhanced_selftest()`（D1 显式绕过阈值 / D2 非列表零误触发 / D3 dict 触发 / D4 spec 覆盖阈值 / D5 tuple 触发），纳入 `__main__`。
- **设计取舍**：子电路形状（拼『分析 top-k』电阻）保持不变，只扩「何时触发」与「阈值来源」；显式提示是**业务侧拉闸**，默认队列为空 → 退旧自动行为。**零回归**：无非列表值误触发；未达阈值且无显式提示 → 返回 None，普通执行不受影响。
- **验证**：`evolve_enhanced_selftest` 离线通过（5 项全绿）；既有 `circuit_executor_evolve_selftest`（research 8 框架>5 端到端递归）仍通过；全量 `python runtime.py` 通过（无 key/无网）。
- **诚实边界**：进化仍是「检索到一堆 → 自动拼第二步」的启发式；显式提示只强制「触发」，不保证第二步子电路本身更对。`_evolve_requests` 目前由自检/下游组件注入，规划器尚未自动产出（可作为后续）。

### ① 规划器自动产出显式进化提示（compiler/compile.py + runtime.py + circuit-planner/scripts/plan.py，2026-08-03 续，已完成）

- **动机（用户待办 + D 增强收口）**：D 让 `maybe_evolve` 支持显式提示队列 `state._evolve_requests`，但此前只能由自检/下游组件手动注入。要把它「接进规划器」——让 NL 规划在编译期自动判定「该进化」并写进 spec，闭环引擎（CircuitExecutor）消费。
- **落点**：
  - `compiler/compile.py`：新增 `_infer_evolve_requests(spec)` 启发式——扫描组件/连线，当某「检索/研究」类子任务（`label` ∈ retrieve/search/... 或其 produced 字段含 list/options/frameworks 等集合信号）存在下游「分析/推理」类子任务（`reason/analyze/compare/...`）消费同一集合字段时，对该字段发 `{"key": <字段名>, "top_k": 3}`；`compile_goal` 在返回 spec 前赋值 `spec["evolve_requests"]`（与 `binder_report` 同位置）。
  - `runtime.py` `CircuitExecutor.__init__`：把 `circuit.spec.get("evolve_requests")` 种进 `state["_evolve_requests"]`，供 `maybe_evolve` 的显式提示队列消费（零回归：无则空列表，退旧自动行为）。
  - `circuit-planner/scripts/plan.py` `_run_real`：由 `circuit.execute()`（仅 propagate，不跑进化/补数/异构）切换为 `CircuitExecutor(circuit).run()`——使规划器的真实执行走闭环引擎，**同时激活 A 自动补数 + C 异构校验 + D 进化**，并消费 spec.evolve_requests。为兼容原 `res` 打印，给 `CircuitExecutor.run()` 返回补 `iterations=1`/`self_healed={}`。
- **设计取舍**：启发式只「建议」，是否真进化仍取决于该字段的检索值是否真为可计数集合（`maybe_evolve` 显式分支对非空壳列表才触发）；误提示无害（数据非列表则跳过）。把 spec 作为唯一载体，规划器→执行器零额外传参。**注意**：此改动让 plan.py 的真实执行从 propagate-only 升级为闭环引擎，是 ① 的必然代价（否则显式提示在规划器路径永不触发）。
- **验证**：`python -m compiler.compile` 新增 `_planner_evolve_selftest` 离线通过（retrieve→reason + 集合字段 frameworks → 推断 evolve_requests=[{key:frameworks,top_k:3}]；CircuitExecutor 正确种进 state）；`python runtime.py` 全量仍绿（含 C/D 回归）。`plan.py --self-test` 不受影响。
- **诚实边界**：启发式依赖 capability/字段命名约定（CAPABILITY_VOCAB + 集合 token 词表）；若模型把「研究」输出命名成非集合词，可能漏提示。真异构仍需 ③ 配 VERIFY_*；本块只负责「规划器发出进化意图」。
