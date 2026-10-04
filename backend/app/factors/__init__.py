"""声明式公式化因子框架。

结构::

    ir.py       表达式节点 + 因子定义容器 (与后端无关)
    ops_np.py   NumPy 矩阵后端求值器 —— 回测路径
    ops_pl.py   Polars 表达式后端求值器 —— 策略评分 / 盘中路径
    gtja191.py  GTJA Alpha191 因子声明

用法::

    from app.factors import gtja191, ops_np, ops_pl

    definition = gtja191.SKELETON_BY_ID["gtja001"]
    matrix = ops_np.evaluate(definition.expr, {"close": ..., "volume": ...})

    plan = ops_pl.evaluate(definition.expr)   # → Plan
    frame = plan.apply(frame, alias="gtja001")

核心约定见 ``ir`` 模块顶部: 一份公式只写一遍, 两个后端各自求值且数值对齐。
Polars 后端返回 :class:`~app.factors.ops_pl.Plan` 而非单个 ``pl.Expr``, 原因是
嵌套 ``.over()`` 不可靠 —— 详见 ``ops_pl`` 模块顶部。
"""

from __future__ import annotations

from .ir import (
    AVAILABLE_FIELDS,
    FactorDef,
    Node,
    iter_calls,
)

__all__ = [
    "AVAILABLE_FIELDS",
    "FactorDef",
    "Node",
    "iter_calls",
]
