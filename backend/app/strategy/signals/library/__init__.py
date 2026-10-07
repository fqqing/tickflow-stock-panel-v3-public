"""信号函数库 — 原子信号的可复用实现。

每个子模块 import 即触发 @signal 注册。新增信号函数时：
1. 在对应子模块（或新建）用 @signal 装饰器注册
2. 在本文件 import 该子模块
即可被组合表达式引用，无需改动引擎。
"""
from __future__ import annotations

from app.strategy.signals.library import ma, macd, volume, boll  # noqa: F401 (注册副作用)

__all__ = ["ma", "macd", "volume", "boll"]
