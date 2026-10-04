"""公式化因子 (GTJA Alpha191 等) 的声明式表达式 IR。

为什么要这层 IR
----------------
项目里新增一个因子要动三处: ``backtest/factor.py::FACTOR_COLUMNS`` 注册表、
``backtest/matrix.py`` 的 NumPy 实现、``strategy/scoring.py`` 的 Polars 实现。
按公式逐个手写会产生数百处重复代码, 两个后端还容易算出口径差异。

这里把因子写成一份**与后端无关的表达式树**, 由两个求值器解释:
  - ``ops_np.evaluate`` —— NumPy 矩阵后端, 输入形状 ``(n_symbols, n_dates)``
  - ``ops_pl.evaluate`` —— Polars 表达式后端, 供策略评分与盘中增量使用

运算符重载让定义贴近研报公式原文::

    alpha002 = -delta((CLOSE - LOW - (HIGH - CLOSE)) / (HIGH - LOW), 1)

节点是 frozen dataclass (可 hash), 表达式树可安全复用并当作缓存 key。

口径约定 (对齐 DolphinDB gtja191Alpha 与项目现有实现)
----------------------------------------------------
- ``cs_rank`` / ``ts_rank`` 返回**百分比排名**, 不是绝对名次。
- ``sma(x, n, m)`` 是递推平滑 ``y = (m*x + (n-m)*y')/n``, 等价 ewm alpha = m/n。
- ``decay_linear(x, n)`` 权重 1..n, **最新值权重最大**, 权重和为 1。
- ``regbeta`` 对窗口内位置 1..n 做一元线性回归, 取**斜率**。
- 所有时序算子在窗口内存在缺失值时返回缺失 (等价 Polars ``min_samples=window``)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ================================================================
# 节点定义
# ================================================================
class Node:
    """表达式节点基类。

    只提供算术运算符重载, 让因子定义读起来像公式。
    比较运算刻意**不**重载 —— dataclass 会生成 ``__eq__`` 用于结构比较,
    再重载 ``__eq__`` 会破坏判等语义, 因此比较改用 ``gt`` / ``lt`` 等函数。
    """

    __slots__ = ()

    def __add__(self, other: Any) -> BinOp:
        return BinOp("+", self, _node(other))

    def __radd__(self, other: Any) -> BinOp:
        return BinOp("+", _node(other), self)

    def __sub__(self, other: Any) -> BinOp:
        return BinOp("-", self, _node(other))

    def __rsub__(self, other: Any) -> BinOp:
        return BinOp("-", _node(other), self)

    def __mul__(self, other: Any) -> BinOp:
        return BinOp("*", self, _node(other))

    def __rmul__(self, other: Any) -> BinOp:
        return BinOp("*", _node(other), self)

    def __truediv__(self, other: Any) -> BinOp:
        return BinOp("/", self, _node(other))

    def __rtruediv__(self, other: Any) -> BinOp:
        return BinOp("/", _node(other), self)

    def __pow__(self, other: Any) -> BinOp:
        return BinOp("^", self, _node(other))

    def __neg__(self) -> UnOp:
        return UnOp("neg", self)

    def __abs__(self) -> UnOp:
        return UnOp("abs", self)


@dataclass(frozen=True)
class Field(Node):
    """输入列。可用列见 ``AVAILABLE_FIELDS``。"""

    name: str


@dataclass(frozen=True)
class Const(Node):
    """标量常量。"""

    value: float


@dataclass(frozen=True)
class BinOp(Node):
    """二元算术: ``+ - * / ^``。"""

    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class UnOp(Node):
    """一元函数: ``neg abs log sign sqrt``。"""

    op: str
    operand: Node


@dataclass(frozen=True)
class Cmp(Node):
    """比较: ``> >= < <= == !=``, 仅用于 ``iif`` 的条件。"""

    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Call(Node):
    """算子调用。

    ``window``   —— 滚动窗口长度 (时序算子用), None 表示无窗口。
    ``extra``    —— 额外标量参数, 如 ``sma`` 的 m、``signedpower`` 的指数。
    """

    name: str
    args: tuple[Node, ...] = ()
    window: int | None = None
    extra: tuple[float, ...] = ()


def _node(value: Any) -> Node:
    """把 Python 标量提升为 Const 节点。"""
    if isinstance(value, Node):
        return value
    if isinstance(value, bool):
        return Const(1.0 if value else 0.0)
    if isinstance(value, (int, float)):
        return Const(float(value))
    raise TypeError(f"不能把 {type(value).__name__} 当成表达式节点: {value!r}")


# ================================================================
# 输入字段
# ================================================================
OPEN = Field("open")
HIGH = Field("high")
LOW = Field("low")
CLOSE = Field("close")
VOLUME = Field("volume")
AMOUNT = Field("amount")
TURNOVER_RATE = Field("turnover_rate")

#: 成交均价。项目内部口径 volume 是「手」, 故股数 = volume * 100。
#: 与 ``strategy/scoring.py::vwap_bias`` 保持一致。
VWAP = AMOUNT / (VOLUME * 100.0)

#: 后端必须能提供的原始列。
AVAILABLE_FIELDS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume", "amount", "turnover_rate"}
)


# ================================================================
# 比较 (用于 iif 条件)
# ================================================================
def gt(left: Any, right: Any) -> Cmp:
    return Cmp(">", _node(left), _node(right))


def ge(left: Any, right: Any) -> Cmp:
    return Cmp(">=", _node(left), _node(right))


def lt(left: Any, right: Any) -> Cmp:
    return Cmp("<", _node(left), _node(right))


def le(left: Any, right: Any) -> Cmp:
    return Cmp("<=", _node(left), _node(right))


def eq(left: Any, right: Any) -> Cmp:
    return Cmp("==", _node(left), _node(right))


def ne(left: Any, right: Any) -> Cmp:
    return Cmp("!=", _node(left), _node(right))


# ================================================================
# 时序算子 (沿时间轴滚动)
# ================================================================
def delay(x: Any, n: int = 1) -> Call:
    """DELAY(A, n) —— n 期前的值。"""
    return Call("delay", (_node(x),), window=int(n))


def delta(x: Any, n: int = 1) -> Call:
    """DELTA(A, n) —— A - DELAY(A, n)。"""
    return Call("delta", (_node(x),), window=int(n))


def ts_sum(x: Any, n: int) -> Call:
    return Call("ts_sum", (_node(x),), window=int(n))


def ts_mean(x: Any, n: int) -> Call:
    """MEAN(A, n) / MA(A, n)。"""
    return Call("ts_mean", (_node(x),), window=int(n))


def ts_std(x: Any, n: int) -> Call:
    """STD(A, n) —— 总体标准差 (ddof=0)。"""
    return Call("ts_std", (_node(x),), window=int(n))


def ts_min(x: Any, n: int) -> Call:
    """TSMIN(A, n) / MIN(A, n)。"""
    return Call("ts_min", (_node(x),), window=int(n))


def ts_max(x: Any, n: int) -> Call:
    """TSMAX(A, n) / MAX(A, n)。"""
    return Call("ts_max", (_node(x),), window=int(n))


def ts_rank(x: Any, n: int) -> Call:
    """TSRANK(A, n) —— 末位值在过去 n 期的百分比排名。"""
    return Call("ts_rank", (_node(x),), window=int(n))


def ts_argmax(x: Any, n: int) -> Call:
    """HIGHDAY(A, n) —— 窗口内最大值距当前的天数 (0 = 今天)。"""
    return Call("ts_argmax", (_node(x),), window=int(n))


def ts_argmin(x: Any, n: int) -> Call:
    """LOWDAY(A, n) —— 窗口内最小值距当前的天数。"""
    return Call("ts_argmin", (_node(x),), window=int(n))


def ts_prod(x: Any, n: int) -> Call:
    return Call("ts_prod", (_node(x),), window=int(n))


def ts_corr(a: Any, b: Any, n: int) -> Call:
    """CORR(A, B, n) —— 滚动 Pearson 相关系数。"""
    return Call("ts_corr", (_node(a), _node(b)), window=int(n))


def decay_linear(x: Any, n: int) -> Call:
    """DECAYLINEAR(A, n) —— 线性衰减加权平均, 最新值权重最大。"""
    return Call("decay_linear", (_node(x),), window=int(n))


def wma(x: Any, n: int) -> Call:
    """WMA(A, n) —— 加权移动平均, 权重 0.9^i (与 decay_linear 不同的权重口径)。"""
    return Call("wma", (_node(x),), window=int(n))


def regbeta(x: Any, n: int) -> Call:
    """REGBETA(A, n) —— A 对序列 1..n 回归的斜率。"""
    return Call("regbeta", (_node(x),), window=int(n))


def regresi(x: Any, n: int) -> Call:
    """REGRESI(A, n) —— 上述回归的残差。"""
    return Call("regresi", (_node(x),), window=int(n))


def sma(x: Any, n: int, m: int) -> Call:
    """SMA(A, n, m) —— 中国式递推平滑, alpha = m/n。"""
    return Call("sma", (_node(x),), window=int(n), extra=(float(m),))


def ewm_mean(x: Any, alpha: float) -> Call:
    """指数加权平均, alpha 直接给定。"""
    return Call("ewm_mean", (_node(x),), extra=(float(alpha),))


# ================================================================
# 横截面算子 (沿标的轴)
# ================================================================
def cs_rank(x: Any) -> Call:
    """RANK(A) —— 当日截面百分比排名。"""
    return Call("cs_rank", (_node(x),))


def cs_scale(x: Any) -> Call:
    """SCALE(A) —— 截面归一化到 |A| 之和为 1。"""
    return Call("cs_scale", (_node(x),))


def indneutralize(x: Any, group: Any) -> Call:
    """行业中性化: 截面内按行业分组去均值。"""
    return Call("indneutralize", (_node(x), _node(group)))


# ================================================================
# 元素级函数
# ================================================================
def abs_(x: Any) -> UnOp:
    return UnOp("abs", _node(x))


def log(x: Any) -> UnOp:
    """LOG(A) —— 自然对数。"""
    return UnOp("log", _node(x))


def sign(x: Any) -> UnOp:
    return UnOp("sign", _node(x))


def sqrt(x: Any) -> UnOp:
    return UnOp("sqrt", _node(x))


def signedpower(x: Any, exponent: float) -> Call:
    """SIGNEDPOWER(A, e) —— sign(A) * |A|^e。"""
    return Call("signedpower", (_node(x),), extra=(float(exponent),))


def iif(cond: Cmp, when_true: Any, when_false: Any) -> Call:
    """三元条件, 对应研报里的 ``? :``。"""
    return Call("iif", (cond, _node(when_true), _node(when_false)))


def max_(a: Any, b: Any) -> Call:
    """元素级较大者。"""
    return Call("max", (_node(a), _node(b)))


def min_(a: Any, b: Any) -> Call:
    """元素级较小者。"""
    return Call("min", (_node(a), _node(b)))


# ================================================================
# 因子定义容器
# ================================================================
@dataclass(frozen=True)
class FactorDef:
    """一个公式化因子的完整声明。"""

    id: str
    label: str
    group: str
    desc: str
    expr: Node
    #: 研报原始公式 (仅作展示与审计)
    formula: str = ""
    #: 需要的额外数据 (如行业分类), 缺失时该因子应被跳过
    requires: tuple[str, ...] = field(default_factory=tuple)

    @property
    def warmup(self) -> int:
        """需要的历史预热天数。

        嵌套算子的窗口要**累加**而不是取最大: ``ts_corr(ts_rank(x, 5), y, 5)``
        需要 5 + 5 = 10 天, 只算 5 会让预热后的头几天仍然是缺失。
        这是所需天数的**上界** (滚动算子实际只需要 ``inner + n - 1`` 天),
        保守一侧是安全的一侧。
        """
        return max(compute_warmup(self.expr), 1)

    @property
    def dependency_fields(self) -> frozenset[str]:
        """该因子依赖的原始字段 (由表达式树自动推导)。"""
        return frozenset(iter_fields(self.expr))


def compute_warmup(node: Node) -> int:
    """递归累加表达式树的窗口长度。"""
    if isinstance(node, Call):
        inner = max((compute_warmup(arg) for arg in node.args), default=0)
        own = node.window or 0
        return own + inner
    if isinstance(node, (BinOp, Cmp)):
        return max(compute_warmup(node.left), compute_warmup(node.right))
    if isinstance(node, UnOp):
        return compute_warmup(node.operand)
    return 0


def iter_calls(node: Node) -> list[Call]:
    """列出表达式树里的全部算子调用 (用于测试统计算子覆盖面)。"""
    found: list[Call] = []

    def walk(n: Node) -> None:
        if isinstance(n, Call):
            found.append(n)
            for arg in n.args:
                walk(arg)
        elif isinstance(n, (BinOp, Cmp)):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, UnOp):
            walk(n.operand)

    walk(node)
    return found


def iter_fields(node: Node) -> set[str]:
    """列出表达式树引用的全部原始字段名。

    用于自动推导因子对数据的依赖 (例如 ``vwap_bias`` 形式的内置虚拟字段需要
    展开成 ``close`` / ``volume`` / ``amount``), 免去手写一份依赖表后与公式漂移。
    """
    found: set[str] = set()

    def walk(n: Node) -> None:
        if isinstance(n, Field):
            found.add(n.name)
        elif isinstance(n, Call):
            for arg in n.args:
                walk(arg)
        elif isinstance(n, (BinOp, Cmp)):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, UnOp):
            walk(n.operand)

    walk(node)
    return found
