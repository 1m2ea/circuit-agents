"""
circuit-agents · compiler.compile
================================
M0→M1 流水线编排：Goal → Binder(选型) → Netlister(降低) → Circuit DSL 网表。

把"自动选型 + 降低"串成一步，供 demo / M2 Router / M3 Optimizer 复用。
"""
from __future__ import annotations

from .binder import Binder
from .goal import Goal
from .netlister import Netlister
from .optimizer import Optimizer
from .router import Router


def compile_goal(goal: Goal, auto_bind: bool = True, route: bool = False,
                 no_adapters: bool = False) -> dict:
    """返回可直接被 runtime.py 加载的 spec dict；附带 binder_report。

    route=True 时走 M2 Router（依赖分层 + 并联布线 + 可选格式适配器），
    否则走 M0 Netlister（线性串联）。no_adapters=True 关闭第二层②格式适配器。
    """
    report = None
    if auto_bind:
        binder = Binder()
        tiers = binder.bind(goal)
        goal.tiers = tiers
        report = binder.report(goal, tiers)
    if route:
        spec = Router(default_tier="small").route(goal, no_adapters=no_adapters)
    else:
        spec = Netlister().compile(goal)
    spec["binder_report"] = report
    return spec


def optimize_goal(goal_dict: dict, runs: int = 200, seed: int = 7) -> dict:
    """M3 总入口：对结构化目标跑 贪心 + 搜索，返回优化后的 spec 与 Pareto 前沿。"""
    return Optimizer(runs=runs, seed=seed).optimize(goal_dict)
