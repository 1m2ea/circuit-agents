# Circuit DSL — 用电路拓扑描述 Agent 工作流

把"电路设计"的思想搬来做 Agent 编排：用**确定性的拓扑结构**去驾驭
**概率性的模型输出**。本规范定义一套元件库与一个 JSON 拓扑描述语言
（DSL），并给出执行语义与指标定义。配套的运行时见 `runtime.py`。

---

## 1. 核心隐喻

| 电路元件 | Agent 工作流角色 | 关键参数 |
|---|---|---|
| 电源 Power | 初始任务 / 目标 | `task` |
| 运放 OpAmp | 调度器（高 Zin 读任务、低 Zout 驱动支路） | `spec_clarify` |
| 电阻 Resistor | 原子 Agent（小/大/工具模型）· **变换器**：`output=min(输入质量,自身能力)` | `model`, `cost`, `latency`, `accuracy`, `yield` |
| 电容 Capacitor | 上下文汇合 / 缓冲 | `cost`, `latency` |
| 二极管 Diode | 单向格式校验（防错误回流） | `cost`, `latency` |
| ADC | 评估打分器（把模糊输出量化成电平） | `threshold` |
| 看门狗 Watchdog | 反馈环迭代上限（防无限重试） | 由 `feedback.max_iter` 控制 |
| 桥式整流 Bridge | 多模态输入对齐成标准格式 | `cost`, `latency` |
| 逻辑门 LogicGate | 条件路由（按 ADC 电平选支路） | — |
| 源 Source | 外部原始信号（文本/图像/表格） | `quality` |
| 导线 Wire | 确定性结构化数据传递 | — |

### 对原映射的校准（来自讨论）
- **电阻不是确定性的**：Agent 支路有 `yield`（良率），可能"开路"
  （幻觉/拒答）却照样 `cost`。故需 **保险丝/良率监视** 概念——
  运行时把 `ok=False` 的支路当作开路处理，并计入成本。
- **逻辑门需要 ADC 先量化**：Agent 输出是连续置信分布，不是干净电平。
  `adc` 节点把模糊质量量化成 `high/low`，逻辑门据此路由。
- **并联只抗"执行"方差，不抗"规格"方差**：若任务规格本身含糊，所有
  支路一起挂。故 `opamp` 的 `spec_clarify` 在扇出前先澄清规格。
- **反馈环是离散的、每轮都烧钱**：不像运放 ns 级收敛，故必须配
  `watchdog`（迭代上限），否则"settle time = 成本"会失控。
- **电阻是变换器，不是生成器**：Agent 这一步的输出质量受上游输入约束
  （`output = min(input, capability)`）。因此"最弱一环"能否传导，取决于下游
  是不是纯透传——桥式整流的短板只有在变换器语义下才会真实生效。

---

## 2. DSL（JSON 拓扑描述）

```jsonc
{
  "name": "parallel-fanout",          // 拓扑名（用于报告）
  "task": "并行：检索+深析+计算",       // 电源提供的任务信号
  "components": {                     // 元件表，key = 节点 id
    "src":   {"type": "power",  "label": "任务"},
    "sched": {"type": "opamp",  "label": "调度器", "spec_clarify": true},
    "a":     {"type": "resistor","label": "快速检索", "model": "small"},
    "b":     {"type": "resistor","label": "深度分析", "model": "large"},
    "c":     {"type": "resistor","label": "计算",     "model": "tool"},
    "merge": {"type": "capacitor","label": "汇合"},
    "d":     {"type": "diode",  "label": "校验"}
  },
  "wires": [                         // 有向边 [from, to]
    ["src","sched"],
    ["sched","a"], ["sched","b"], ["sched","c"],
    ["a","merge"], ["b","merge"], ["c","merge"],
    ["merge","d"]
  ],
  "feedback": {                      // 可选：反馈环（不参与前向 DAG）
    "from": "wg", "to": "sched", "max_iter": 3
  }
}
```

### 字段语义
- `components.<id>.type`：见上表元件类型。
- `resistor.model`：`small` / `large` / `tool` 三档，对应默认
  `cost/latency/accuracy/yield`（见 `runtime.SimBackend._TIERS`）。
  也可逐字段覆盖。
- `wires`：声明**前向**数据流。运行时按拓扑分层，同层节点**并行**
  执行，层延迟 = 该层最大 `latency_ms`；跨层延迟累加。
- `feedback`：声明反馈环 `from → to`，迭代上限 `max_iter`。该边
  **不**计入前向 DAG（避免成环），由引擎单独控制重试。

---

## 3. 执行语义

1. **分层（Kahn）**：忽略 `feedback` 边，对前向 DAG 做拓扑分层。
2. **逐层执行**：每层内节点并行；层延迟取 `max`，成本累加。
3. **反馈环**：若存在 `feedback`，引擎重复"从 `to` 出发的前向传播"
   至多 `max_iter` 次；当 `adc.ok`（电平 high）或迭代耗尽时停止。
   每次迭代都重新计入成本与延迟（重试即烧钱）。
4. **终止指标**：
   - `success`：有 `adc` 时取 `adc.ok`；无 `adc` 时恒为 true。
   - `final_quality`：有 `adc` 取 `adc` 质量分；否则取所有终端节点
     质量的最大值。
   - `watchdog_tripped`：迭代耗尽仍未达标。

---

## 4. 指标定义

| 指标 | 含义 | 电路类比 |
|---|---|---|
| `total_latency_ms` | 端到端延迟 | 信号传播总时长 |
| `total_cost` | 累计花费（$）。含开路支路成本 | 总功耗 |
| `final_quality` | 最终输出质量 [0,1] | 输出电压 |
| `success_rate` | 多次运行中达标比例 | 良率 |
| `iterations` | 实际反馈迭代次数 | 收敛步数 |

> 注：默认后端 `SimBackend` 为**随机模拟**（给定 seed 可复现），
> 用于验证拓扑*相对*行为。接入真实模型只需实现 `Backend` 接口并传给
> `Circuit(backend=...)`。

---

## 5. 示例拓扑（见 `examples/`）
- `series.json`：纯串联流水线（演示良率乘衰）。
- `parallel.json`：并联分流（演示延迟=最慢支路）。
- `feedback.json`：带自校正反馈环（演示重试提升良率）。
- `bridge_rectifier.json`：多模态整流桥（演示短板效应）。
