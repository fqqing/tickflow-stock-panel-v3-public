"""信号函数注册表 — 统一签名、可发现、可组合的原子信号。

借鉴 czsc 的「信号函数库」理念：每个原子信号是一个可注册、可列出、可复用的函数。
与 czsc 的差异：信号函数输入是 :class:`MarketDataMatrix`（全市场矩阵）而非单标的
CZSC 对象，输出是布尔矩阵（True=信号触发），保持 v3 向量化回测口径。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.backtest.matrix import MarketDataMatrix

# 信号函数签名：输入行情矩阵与扁平平参数字典，返回布尔矩阵。
# params 与策略 META["params"] 共享扁平命名空间（信号函数按需取自己的参数，默认值兜底）。
SignalFn = Callable[..., np.ndarray]

# 信号方向：entry=入场 / exit=离场 / both=双向。
DIRECTION_ENTRY = "entry"
DIRECTION_EXIT = "exit"
DIRECTION_BOTH = "both"


@dataclass(frozen=True)
class SignalDef:
    """一个已注册的信号函数定义。"""

    name: str
    category: str
    direction: str  # DIRECTION_ENTRY / DIRECTION_EXIT / DIRECTION_BOTH
    description: str
    required_fields: frozenset[str]
    warmup: int
    params: tuple[dict, ...]
    enable_param: str | None  # 布尔开关参数名：False 时信号在组合里中性化（等价移除）
    fn: SignalFn


# 名称 -> 定义。模块加载期由 @signal 装饰器填充，运行时只读。
_REGISTRY: dict[str, SignalDef] = {}


def signal(
    *,
    name: str,
    category: str = "通用",
    direction: str = DIRECTION_BOTH,
    description: str = "",
    required_fields: tuple[str, ...] = (),
    warmup: int = 0,
    params: tuple[dict, ...] = (),
    enable_param: str | None = None,
) -> Callable[[SignalFn], SignalFn]:
    """注册一个信号函数。

    用法::

        @signal(name="ma_golden_cross", category="均线", direction="entry",
                description="MA5 上穿 MA20", required_fields=("close",), warmup=20)
        def ma_golden_cross(market, **params):
            ...

    Args:
        name: 全局唯一信号名（组合表达式里引用它）。
        category: 分类（均线/MACD/量价/布林/缠论…），用于前端目录展示。
        direction: 信号方向（entry/exit/both）。
        description: 一句话说明。
        required_fields: 依赖的行情字段（close/open/high/low/volume）。
        warmup: 该信号需要的预热根数（组合时取 max）。
        params: 参数定义 list[dict]，每项含 id/label/type/default 等（与策略 META 同构）。
        enable_param: 布尔开关参数名。该参数为 False 时，信号在组合里中性化
            （all_of 里视为 True、any_of 里视为 False），等价于把信号从组合中移除。
    """
    if not name or not isinstance(name, str):
        raise ValueError("signal name must be a non-empty string")
    if name in _REGISTRY:
        raise ValueError(f"duplicate signal name: {name!r}")

    def decorate(fn: SignalFn) -> SignalFn:
        _REGISTRY[name] = SignalDef(
            name=name,
            category=category,
            direction=direction,
            description=description,
            required_fields=frozenset(required_fields),
            warmup=int(warmup),
            params=tuple(params),
            enable_param=enable_param,
            fn=fn,
        )
        return fn

    return decorate


def get_signal(name: str) -> SignalDef:
    """按名取信号定义，不存在则抛 ValueError。"""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown signal function: {name!r}") from exc


def has_signal(name: str) -> bool:
    return name in _REGISTRY


def list_signals() -> list[SignalDef]:
    """返回全部已注册信号（按 name 排序）。"""
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def signal_catalog() -> list[dict[str, Any]]:
    """序列化为前端可消费的目录（不含 fn）。"""
    out: list[dict[str, Any]] = []
    for s in sorted(_REGISTRY.values(), key=lambda s: (s.category, s.name)):
        out.append({
            "name": s.name,
            "category": s.category,
            "direction": s.direction,
            "description": s.description,
            "required_fields": sorted(s.required_fields),
            "warmup": s.warmup,
            "params": [dict(p) for p in s.params],
        })
    return out
