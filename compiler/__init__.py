"""circuit-agents · compiler 包

布局布线编译器（Layout & Routing Compiler）骨架。
M0: goal (结构化目标) + netlister (目标→网表)
M1: binder   (网表→型号档绑定，复用 runtime._TIERS)
M2: router   (标准单元拓扑 + 约束插入)
M3: optimizer(以 runtime 为 Evaluator 搜索)
"""
