"""
circuit-agents · executor_trace（薄封装 · 向后兼容）
================================================
观察窗(B) 的可视化追踪渲染，现已统一到 compiler/trace_renderer.py（仓库内唯一源码）。

本文件只做一件事：重导出 render_executor_trace，使既有调用方
（compiler/demo.py 的 `from compiler.executor_trace import render_executor_trace`）
无需任何修改即可继续工作。

不要在本文件里再写渲染逻辑——改 trace_renderer.py（canonical）。
"""
from .trace_renderer import render_executor_trace  # 同包；真源码在 trace_renderer.py

__all__ = ["render_executor_trace"]
