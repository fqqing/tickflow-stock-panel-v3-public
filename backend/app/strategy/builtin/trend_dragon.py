"""趋势擒龙 — 连续 9 根重心上移后的回调/突破共振 (通达信主图公式)

源脚本 (qushiqinlong/选股_趋势擒龙.py, compute_trend_dragon):

    A1 = C > REF(C, 4)                       # 重心上移
    A2 = BARSLASTCOUNT(A1) == 9              # 连续 9 根重心上移
    A3 = BARSLAST(A2)                        # 距上次 A2 的 bar 数
    XGY = 四组回调突破条件取 OR (依赖 A3 与 MA10)
    信号 = XGY AND C >= REF(C, 1)

四组 OR (A3 是"距上次 9 连阳"的年龄, 越新越靠前):

    1) A3 in [1,3] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3)<REF(O,A3)        回踩那根收阴
    2) A3 in [2,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-1)<REF(O,A3-1)
    3) A3 in [3,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-2)<REF(O,A3-2)
    4) A3 == 5     AND C>MA10 AND H>=HHV(H,5)                                直接创新高

矩阵原生实现 —— 全部算子走 app.backtest.matrix 的 valid_* 族, 与面板
「有效 bar (自动跳停牌日) + 前复权 OHLC」口径一致: REF / BARSLAST /
BARSLASTCOUNT / HHV / MA 全部按有效 bar 计数, 停牌日不计入窗口。

源脚本里另有两条基于事件研究的可选过滤, 这里都移植了:

- ``--max-bias20`` (信号日收盘价相对 MA20 的乖离率上限) -> ``bias20_cap_pct``
- ``--max-momentum`` (资金动能上限) -> ``use_momentum_filter`` + ``momentum_cap``

**两者默认开启且默认值对齐用户日常用法**
(``python 选股_趋势擒龙.py --max-momentum 1 --max-bias20 10 --feishu``, 见源脚本 docstring
第 30 行): ``use_momentum_filter=True`` / ``momentum_cap=1.0`` / ``bias20_cap_pct=10.0``。
要复现源脚本「不加限制」的原始信号, 把开关关掉 / 把上限填 0 即可。

资金动能口径 (源脚本 compute_capital_momentum):

    A1 = C / INDEXC * 1e6            # INDEXC = 该股所属市场的基准指数收盘价
    A2 = MA(A1, 52)                  # 52 个有效 bar
    动能 = (A1 / A2 - 1) * 10         # 过滤: 动能 <= 上限 (事件研究推荐 0)

``INDEXC`` 走矩阵计算特征 ``index_close`` (:mod:`app.backtest.benchmark`),
按标的后缀取基准: SH -> 上证指数, SZ -> 深证成指, BJ -> 北证50。
与源脚本的差异 (均为口径显式化, 非取舍):

1. 源脚本按 ``market`` 取上证指数 / 深证成指, 北交所无对应分支; 这里按后缀映射,
   北交所取北证50, 取不到时退上证指数。
2. 源脚本在「个股日期 ∩ 指数日期」的交集上算 52 日均值; 这里按有效 bar 语义
   (停牌日既不计入也不打断), 指数停牌与个股停牌口径一致。
3. **指数当天缺数据时前向沿用最近一根**: 源脚本的 inner join 会把「最后一行」退回
   前一天。盘中当日指数行可能尚未落盘, 严格丢空会让整个横截面被过滤光。
   (个股 K 线副图那一侧走的是「用实时指数快照补今天」, 矩阵里没有实时快照通道;
   当日指数行一旦落盘就会被自动用上。)
4. **基准数据整列缺失时该票不参与过滤** (此时源脚本会因 ``momentum is None`` 把全部
   候选剔除, 结果直接空掉)。只要该票的基准列可用, 就按源脚本严格执行 —— 动能算不出
   来 (历史不足 52 个交集交易日) 一样剔除。

过滤按**逐 bar** 施加 (每个交易日用当天的动能), 与矩阵里其它过滤一致; 源脚本只算
最新一根再套用到扫描结果上, 两者在「今日选股」这个用法下等价。
"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_barslast as barslast,
)
from app.backtest.matrix import (
    valid_barslastcount as barslastcount,
)
from app.backtest.matrix import (
    valid_rolling_max as rolling_max,
)
from app.backtest.matrix import (
    valid_rolling_mean as rolling_mean,
)
from app.backtest.matrix import (
    valid_shift as shift,
)
from app.backtest.matrix import (
    valid_shift_at as shift_at,
)

_REF_LOOKBACK = 4  # A1: C > REF(C, 4)
_CONTINUOUS_BARS = 9  # A2: 连续 9 根
_MAX_AGE = 5  # A3 只在前 5 根内有意义 (四组条件的取值上限)
_MA_WINDOW = 10  # MA(C, 10)
_NEW_HIGH_WINDOW = 5  # HHV(H, 5)
_DEFAULT_SCAN_DAYS = 5  # 源脚本 --days 默认 5: 近 5 个交易日出现过信号即入选
_MIN_HISTORY = 20  # 源脚本 n < 20 直接返回全 False

# 资金动能: MA(A1, 52), 窗口按有效 bar 计
_MOMENTUM_WINDOW = 52

# 事件研究过滤的默认值 —— 对齐源脚本 docstring 第 30 行的日常调用:
#   python 选股_趋势擒龙.py --max-momentum 1 --max-bias20 10 --feishu
# 源脚本自己不带默认值(None = 不过滤), 这里按用户实际用法把限制默认打开。
_DEFAULT_MOMENTUM_ENABLED = True
_DEFAULT_MOMENTUM_CAP = 1.0
_DEFAULT_BIAS20_CAP_PCT = 10.0

# 依赖深度: A2 需要 4+9 根 -> A3 再等 5 根 -> MA10 / HHV5 各自 10 / 5 根; 60 根足够收敛
_WARMUP_BARS = 60

# 开启资金动能过滤时: 动能本身要先攒满 52 个有效 bar, 其均值再要 52 个 -> 104 根起步
_MOMENTUM_WARMUP_BARS = 120

META = {
    "id": "trend_dragon",
    "name": "趋势擒龙",
    "description": "连续9根收盘重心上移后, 距上次9连阳1~5根内出现回调突破或直接创5日新高, 且站稳MA10、收盘不低于昨收(近N个交易日共振)",
    "tags": ["趋势", "突破", "回调", "量价"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 10e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "scan_days",
            "label": "近N个交易日内出现信号",
            "type": "int",
            "default": _DEFAULT_SCAN_DAYS,
            "min": 1,
            "max": 20,
            "step": 1,
        },
        {
            "id": "require_above_ma10",
            "label": "要求站稳MA10",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_strong_close",
            "label": "要求收盘不低于昨收",
            "type": "bool",
            "default": True,
        },
        {
            "id": "bias20_cap_pct",
            "label": "MA20乖离率上限%(0=不过滤)",
            "type": "float",
            "default": _DEFAULT_BIAS20_CAP_PCT,
            "min": 0.0,
            "max": 50.0,
            "step": 0.1,
        },
        {
            "id": "use_momentum_filter",
            "label": "启用资金动能上限",
            "type": "bool",
            "default": _DEFAULT_MOMENTUM_ENABLED,
        },
        {
            "id": "momentum_cap",
            "label": "资金动能上限(工具默认1)",
            "type": "float",
            "default": _DEFAULT_MOMENTUM_CAP,
            "min": -50.0,
            "max": 50.0,
            "step": 0.1,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_trend_dragon"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20


class TrendDragonMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        if _momentum_enabled(params):
            return max(_WARMUP_BARS, _MOMENTUM_WARMUP_BARS)
        return _WARMUP_BARS

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        close = market.close
        open_ = market.open
        high = market.high
        valid = np.isfinite(close) & np.isfinite(open_) & np.isfinite(high)

        # A1 / A2 / A3 —— 重心上移 -> 连续 9 根 -> 距上次的年龄
        a1 = close > shift(close, _REF_LOOKBACK, valid)
        a2 = barslastcount(a1, valid) == np.float32(_CONTINUOUS_BARS)
        a3 = barslast(a2, valid)

        # REF(X, A3) 三条: 回踩那根的 高/收/开; A3 逐 bar 变化, 用变长取数
        ref_high = shift_at(high, a3, valid)
        ref_close = shift_at(close, a3, valid)
        ref_open = shift_at(open_, a3, valid)
        # REF(X, A3-1) / REF(X, A3-2): 回踩点再往前数一根/两根的 收/开
        ref_close_prev = shift_at(close, a3 - np.float32(1.0), valid)
        ref_open_prev = shift_at(open_, a3 - np.float32(1.0), valid)
        ref_close_prev2 = shift_at(close, a3 - np.float32(2.0), valid)
        ref_open_prev2 = shift_at(open_, a3 - np.float32(2.0), valid)
        # A3 超过 _MAX_AGE 后不再有任何分支命中, 与源脚本一致
        in_range = (a3 >= np.float32(1.0)) & (a3 <= np.float32(_MAX_AGE))

        breakout = (
            # 1) A3 in [1,3]: 突破回踩那根的高点, 且回踩那根收阴
            (in_range & (a3 <= np.float32(3.0)) & (high > ref_high) & (ref_close < ref_open))
            # 2) A3 in [2,4]: 回踩点前一根收阴
            | (
                (a3 >= np.float32(2.0))
                & (a3 <= np.float32(4.0))
                & (high > ref_high)
                & (ref_close_prev < ref_open_prev)
            )
            # 3) A3 in [3,4]: 回踩点前两根收阴
            | (
                (a3 >= np.float32(3.0))
                & (a3 <= np.float32(4.0))
                & (high > ref_high)
                & (ref_close_prev2 < ref_open_prev2)
            )
            # 4) A3 == 5: 直接站上 5 日最高
            | ((a3 == np.float32(_MAX_AGE)) & (high >= rolling_max(high, valid, _NEW_HIGH_WINDOW)))
        )
        # 源脚本里四组分支各自都要求 C > MA10, 提取成公共项; 保险起见先算, 由开关决定是否施加
        above_ma10 = close > matrix_feature(market, "ma10")

        dragon = breakout & valid
        if params.get("require_above_ma10", True):
            dragon &= above_ma10
        if params.get("require_strong_close", True):
            dragon &= valid & (close >= shift(close, 1, valid))

        # 「近 N 个交易日出现过信号」= 信号在有效 bar 上的滚动窗口内出现过
        scan_days = _resolve_scan_days(params)
        entry = rolling_max(dragon.astype(np.float32), valid, scan_days) >= np.float32(0.5)

        bias_cap = _resolve_float(params.get("bias20_cap_pct"), _DEFAULT_BIAS20_CAP_PCT)
        if bias_cap > 0:
            bias_pct = matrix_feature(market, "ma20_bias") * np.float32(100.0)
            entry &= np.isfinite(bias_pct) & (bias_pct <= np.float32(bias_cap))

        if _momentum_enabled(params):
            momentum = _capital_momentum(market, valid)
            cap = _resolve_float(params.get("momentum_cap"), _DEFAULT_MOMENTUM_CAP)
            # 基准数据整列缺失 (该交易所的指数读不到) 时不参与过滤, 避免把结果清空;
            # 只要该票的基准可用, 就与源脚本一致地严格执行 (动能算不出来 -> 剔除)。
            has_benchmark = np.isfinite(matrix_feature(market, "index_close")).any(axis=0)
            keep = ~has_benchmark[None, :] | (np.isfinite(momentum) & (momentum <= np.float32(cap)))
            entry &= keep
        entry &= valid

        ma20 = matrix_feature(market, "ma20")
        previous_close = shift(close, 1, valid)
        exit_ = valid & (close < ma20) & (previous_close >= shift(ma20, 1, valid))

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_trend_dragon",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


def _capital_momentum(market: MarketDataMatrix, valid: np.ndarray) -> np.ndarray:
    """资金动能 = (A1 / MA(A1, 52) - 1) * 10, A1 = C / INDEXC * 1e6。

    - ``INDEXC`` 取矩阵计算特征 ``index_close`` (按标的后缀对齐的基准指数收盘价)。
    - 52 日均值按有效 bar 计: 个股停牌行与「指数缺值」行都不计入。
    - 基准指数不可用 (整列 NaN) 或历史不足 52 个有效 bar 时输出 NaN,
      由调用方决定 NaN 的语义 (本策略: 基准整列缺失 -> 放行; 否则剔除)。
    """
    shape = market.shape
    momentum = np.full(shape, np.nan, dtype=np.float32)
    index_close = matrix_feature(market, "index_close")
    usable = np.isfinite(index_close) & (index_close > 0)
    if not usable.any():
        return momentum

    a1 = np.full(shape, np.nan, dtype=np.float32)
    np.divide(market.close, index_close, out=a1, where=usable)
    a1 *= np.float32(1e6)

    a1_valid = valid & usable & np.isfinite(a1)
    average = rolling_mean(a1, a1_valid, _MOMENTUM_WINDOW)

    ratio = np.full(shape, np.nan, dtype=np.float32)
    np.divide(a1, average, out=ratio, where=np.isfinite(average) & (average != 0))
    np.subtract(ratio, np.float32(1.0), out=ratio)
    np.multiply(ratio, np.float32(10.0), out=momentum, where=np.isfinite(ratio))
    return momentum


def _resolve_scan_days(params: dict) -> int:
    """扫描窗口: 非法值回落到源脚本默认值, 并夹到 [1, 20]。"""
    raw = params.get("scan_days", _DEFAULT_SCAN_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SCAN_DAYS
    return min(max(days, 1), 20)


def _momentum_enabled(params: dict) -> bool:
    """资金动能过滤开关: 缺省按 :data:`_DEFAULT_MOMENTUM_ENABLED` (默认开)。

    面板传下来的 params 只会带用户显式改过的键, 所以缺省值必须与 META 一致 ——
    否则「UI 显示开着、实际没生效」。
    """
    raw = params.get("use_momentum_filter", _DEFAULT_MOMENTUM_ENABLED)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _resolve_float(value: object, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if np.isfinite(number) else fallback


MATRIX_STRATEGY = TrendDragonMatrixStrategy()
