

---

# 自我改进提案 #001 · 2026-09-06 15:38

> ⚠️ 状态：**待人工确认**。本提案由系统规则式自诊断产出，未经确认不得实施。

## 1. 诊断事实（最近 5 次 run，失败 0 次）

- `summarize` × 1（reason=no_input；证据: 任务『』踩坑: reason(reason)失败[ok=False]；summarize(sum)失败[open=no_）

### 自动沉淀教训（P2 闭环产物）

- 任务『』踩坑: reason(reason)失败[ok=False]；summarize(sum)失败[open=no_input]。同类任务应先排查此环节
- 工具循环达轮数上限时，模型中间独白（Let me read...）会被当成品返回：必须强制收口——执行完挂起调用后去掉 tools 再要一次最终交付。
- LLM 无工具检索时凭参数知识硬编答案：曾幻觉出项目里根本不存在的 Redis 后端（正文 26 次），而真实存储 topology_memory/executions.db/ShareRepo 零提及。retrieve 类节点必须先 qu

## 2. 最优先失败模式

`summarize` / `no_input`（1 次）

## 3. 改进任务书

| 项 | 内容 |
|---|---|
| 改哪个文件 | runtime.py（data_fill 预算 / _auto_fill 路径） |
| 怎么改 | 排查上游检索节点为何没产出：预算是否耗尽、检索技能是否返回空。必要时提高 data_fill_budget 或为该 label 配置兜底源。 |
| 验收标准 | 同类任务不再出现 no_input 开路；diagnose 复测该模式归零。 |
| 风险/回滚 | 放宽预算会增加调用量——控制在预算上限内。 |

*规则式提案声明：以上改法来自系统已收录的提案目录（`compiler/self_improve.py` → `_proposal_for`），事实部分来自真实执行记录，方案部分不含臆造。*


---

# 自我改进提案 #002 · 2026-09-06 15:38

> ⚠️ 状态：**待人工确认**。本提案由系统规则式自诊断产出，未经确认不得实施。

## 1. 诊断事实（最近 2 次 run，失败 2 次）

- `reason` × 2（reason=node_fail；证据: 检索行业数据并深度分析（P3 元循环验证））
- `adc` × 2（reason=node_fail；证据: 检索行业数据并深度分析（P3 元循环验证））
- `reason` × 1（reason=http_error；证据: 任务『检索行业数据并深度分析（P3 元循环验证）』踩坑: reason(r2)失败[open=http_error]；a）

### 自动沉淀教训（P2 闭环产物）

- 任务『检索行业数据并深度分析（P3 元循环验证）』踩坑: reason(r2)失败[open=http_error]；adc(adc)失败[ok=False]；质量门未过(final=0.0,thr=0.5)。同类任务应先排查此环节

## 2. 最优先失败模式

`reason` / `http_error`（1 次）

## 3. 改进任务书

| 项 | 内容 |
|---|---|
| 改哪个文件 | compiler/backend_llm.py / compiler/http_retry.py |
| 怎么改 | 审查退避参数（重试次数/退避曲线）与 timeout 配置；对超时率最高的 tier 考虑提高 VERIFY_TIMEOUT/请求超时，或为该节点配置重试预算。 |
| 验收标准 | 离线：http_retry 自检通过；在线：同类任务 http_error 频次下降（用本模块 diagnose 复测对比）。 |
| 风险/回滚 | 重试预算加大会推高延迟与成本——需同时观察 total_latency_ms。 |

*规则式提案声明：以上改法来自系统已收录的提案目录（`compiler/self_improve.py` → `_proposal_for`），事实部分来自真实执行记录，方案部分不含臆造。*


---

# 自我改进提案 #003 · 2026-09-06 17:00

> ⚠️ 状态：**待人工确认**。本提案由系统规则式自诊断产出，未经确认不得实施。

## 1. 诊断事实（最近 2 次 run，失败 2 次）

- `reason` × 2（reason=node_fail；证据: 检索行业数据并深度分析（P3 元循环验证））
- `adc` × 2（reason=node_fail；证据: 检索行业数据并深度分析（P3 元循环验证））
- `reason` × 1（reason=http_error；证据: 任务『检索行业数据并深度分析（P3 元循环验证）』踩坑: reason(r2)失败[open=http_error]；a）

### 自动沉淀教训（P2 闭环产物）

- 任务『检索行业数据并深度分析（P3 元循环验证）』踩坑: reason(r2)失败[open=http_error]；adc(adc)失败[ok=False]；质量门未过(final=0.0,thr=0.5)。同类任务应先排查此环节

## 2. 最优先失败模式

`reason` / `http_error`（1 次）

## 3. 改进任务书

| 项 | 内容 |
|---|---|
| 改哪个文件 | compiler/backend_llm.py / compiler/http_retry.py |
| 怎么改 | 审查退避参数（重试次数/退避曲线）与 timeout 配置；对超时率最高的 tier 考虑提高 VERIFY_TIMEOUT/请求超时，或为该节点配置重试预算。 |
| 验收标准 | 离线：http_retry 自检通过；在线：同类任务 http_error 频次下降（用本模块 diagnose 复测对比）。 |
| 风险/回滚 | 重试预算加大会推高延迟与成本——需同时观察 total_latency_ms。 |

*规则式提案声明：以上改法来自系统已收录的提案目录（`compiler/self_improve.py` → `_proposal_for`），事实部分来自真实执行记录，方案部分不含臆造。*
