# 布局布线器（Layout & Routing Compiler）· 架构蓝图

> **目标**：把"一个目标"自动编译成"一段满足约束、且接近最优的 Circuit DSL 拓扑"。
> **核心类比**：IC 设计里的 逻辑综合 → 器件选型 → 布局布线 → 时序/功耗仿真 → 迭代。
> 我们的"仿真器"就是已经写好的 `runtime.py`——它给出延迟/成本/良率的可比指标，
> 正好充当编译器闭环里的 **cost model / 时序仿真器**。

---

## 0. 为什么是"布局布线器"，而不是"更聪明的 Agent"

单体智能有天花板且不可预测。IC 业的答案从来不是追更高的晶体管频率，而是
**用结构与布线提升系统级指标**。Agent 系统同理：把"目标 → 拓扑"的活变成可重复、
可优化的**编译步骤**，才能从"手焊电路"走向"大规模集成电路"。这正是当初那句
"从模数混合走向大规模集成电路"的落地路径。

---

## 1. 总体流水线（六段）

```
目标 Goal
   │
   ▼
[1 解析器 GoalParser]   NL ──► 结构化目标            （可选 / LLM，M4）
   │  结构化目标 = { 所需能力, 约束, 模态输入 }
   ▼
[2 综合 Netlister]      结构化目标 ──► 必需功能 DAG（netlist）
   │  例：{ 检索, 推理, 计算, 校验 }
   ▼
[3 选型 Binder]         每个功能节点 ──► 元件类型 + 型号档   （复用 _TIERS）
   │  按 capability vs cost/yield 绑 small / large / tool
   ▼
[4 布局布线 Router]     绑定图 ──► 放置 + 连线 的完整拓扑      （核心 / 新颖）
   │  插入 opamp(调度) / capacitor(汇合) / diode+adc(校验) / watchdog(界)
   │  套用"标准单元"拓扑模板 + 满足约束
   ▼
[5 评估 Evaluator]      ≡ runtime.py 仿真 → 指标(延迟 / 成本 / 良率)
   │
   ▼
[6 优化 Optimizer]      用 Evaluator 反馈搜索(绑定 × 布线)     （M3）
   │  目标：min cost，s.t. latency≤L ∧ quality≥Q ∧ ¬watchdog_tripped
   ▼
[输出]  优化后的 Circuit DSL JSON + 设计理由(rationale)
```

闭环要点：**Router 生成候选 → Evaluator 打分 → Optimizer 不满意就改绑定/布线 → 再仿**。
和 EDA 的 place-and-route + 仿真器完全同构。

---

## 2. 输入：目标长什么样

两种模式，**先结构化、后 NL**：

- **结构化目标（M0 先建，规则即可跑）**
  ```json
  {
    "capabilities": ["retrieve", "reason", "calculate", "verify"],
    "constraints":  {"max_latency_ms": 2000, "max_cost": 0.05, "min_quality": 0.85},
    "modalities":   ["pdf", "table"],
    "reliability":  "high"
  }
  ```
- **NL 目标（M4，LLM 驱动）**：自然语言 → 上述结构化目标。最难的一环是
  **规划出"所需能力"**（planning），需要评测(eval)兜底。

---

## 3. 标准单元库（拓扑模板 / cells）

Router 不直接暴力枚举，而是从一组**已验证的拓扑模板**里选 + 拼——等价于 IC 里的
standard-cell：把"工程经验"固化成可复用结构。

| 意图 | 模板 | 说明 |
|---|---|---|
| 单一能力 | 串联 + clarifier | 顺序流过，调度器先澄清规格（抗规格方差） |
| 互不相关子任务 | 并联分流 + 电容汇合 | 延迟=最慢支路，质量=最优支路 |
| 质量敏感 | 反馈环(adc + watchdog) | 重试提升良率，代价成本↑ |
| 异质输入 | 桥式整流优先 | 多模态先对齐成单路 |
| 高可靠 | 冗余并联 + 投票(diode/adc) | 一支开路不影响其余 |

---

## 4. 布局 vs 布线（在我们语境下的区分）

- **布局 Placement** = 决定*结构*：并联还是串联、在哪插反馈、几条冗余支路。
- **布线 Routing** = 决定*连接*：实际导线、调度器如何分发、上下文如何在 Agent 间传递
  （导线载的是**结构化上下文**，不是原始 token 流）。

---

## 5. 优化器（Optimizer）

闭包：`生成候选 → runtime 仿真打分 → 不满意就改 → 再仿`。

- **第一版 · 贪心**：先选满足 `min_quality` 的最小型号档，再按约束决定并联/反馈/冗余。
- **第二版 · 搜索**：枚举 `patterns × tiers`（或轻量贝叶斯 / RL），在 Evaluator 上找
  **Pareto 前沿**（延迟/成本/良率的权衡曲线）。
- **目标函数**：`min cost`，约束 `latency ≤ L_max ∧ quality ≥ Q_min ∧ ¬watchdog_tripped`。

---

## 6. 库反哺（闭环）

真实跑出来的拓扑会回吐实测 `yield / accuracy / cost` → 更新 `_TIERS` 库 →
下次选型更准。这正是"硅后数据喂下一代节点"的 Agent 版。**前提**是把
`2`（真实后端）接上，才能测得真值，否则库里仍是估计。

---

## 7. 构建顺序（里程碑）

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| **M0** | 结构化目标 schema + Netlister（规则） | — |
| **M1** | Binder（复用现有 `_TIERS`） | M0 |
| **M2** | Router + 5 个标准单元 + 约束插入 | M0, M1 |
| **M3** | Optimizer（先贪心，后搜索），以 runtime 为 Evaluator | M2 |
| **M4**（可选） | NL → 结构化目标的 LLM 解析器 | M0 |
| **M5**（可选） | 真实 LLM 后端（即"1/2"中的 2），让 Evaluator 用真指标 | M3 |

> 注：用户此前建议的 **1（recovery 系数）** 与 **2（真实后端）** 是沿途补强——
> 1 让电阻语义更真（强 Agent 可部分挽救弱输入），2 让 Evaluator 脱离模拟。
> 两者都不阻塞 M0–M3 的骨架搭建，但 2 会显著抬高优化器的可信度。

---

## 8. 开放问题 / 风险（诚实边界）

- 组件画像(库)是**估计值**，垃圾进 → 垃圾拓扑；真实值要靠 M5 测。
- NL → netlist（规划"所需能力"）是最难的 AI 环节；结构化目标是务实第一目标。
- 拓扑空间**组合爆炸**，必须靠标准单元库剪枝，不能暴力枚举。
- "所需能力"如何从目标推出，本身是 planning 问题，需要 eval 兜底而非拍脑袋。

---

## 9. 走查示例

**目标**："总结一篇 PDF 并核对里面的数字"

1. **Netlist**：`{ retrieve(PDF), reason, calculate, verify }`
2. **Bind**：retrieve→tool 档（读 PDF）、reason→large、calculate→tool、verify→large
3. **Router**：模态含 pdf → 先桥式整流；reason 与 calculate 互不相关 → 并联；
   verify 质量敏感 → 在 calculate 外包反馈环（adc + watchdog, max_iter=3）
4. **Emit**：一段 Circuit DSL，含
   `power → opamp → bridge → {并联: reason, calculate(反馈)} → capacitor → diode → adc`
5. **Evaluator**：仿真得延迟/成本/良率；Optimizer 若超预算则下调型号档或去掉冗余。

---

*本蓝图是设计文档（无代码）。下一步按 M0→M3 顺序落地骨架；M4/M5 与 1/2 为补强。*
