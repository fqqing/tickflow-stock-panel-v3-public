"""回测撮合引擎 —— 把「信号 + 风控规则」在一段日线上变成可评估的交易序列。

移植说明 (为什么是重写而不是复制 v1)
----------------------------------
v1 ``app/backtest/engine.py`` 有 3191 行, 里面混着多策略矩阵、选股候选池打分、
分钟级精确成交、monte carlo、并行 worker —— 那些是 v1 选股平台需要的。
v2 现在只要一条主链: **给一只票的一段 K 线和一对买卖信号, 算准它的效果**。

所以本模块只抽出撮合与绩效口径, 并保证两件事:

1. **成本模型与 v1 同口径**, 否则历史调优结论跨仓库没法比:
   买入腿 = 佣金 + 滑点 (+ 双边印花税); 卖出腿 = 佣金 + 印花税 + 滑点。
2. **净值曲线故意与 v1 不同**: v1 把收益记在「出场日」, 持仓期间曲线是平的,
   一笔亏 30% 持有一个月的交易在那个曲线上**完全看不到过程回撤**。这里改成
   逐日盯市 (见 :func:`_build_curves`)。

退出优先级 (高 -> 低)
------------------
    止损 > 止盈 > 卖点信号 > 最长持仓 > 回测结束

优先级与 v1 一致, 但 **触发价判定不同**: 止损/止盈按盘中 extremum
(``low``/``high``) 触发, 而不是 v1 的「按当日 close 事后判定」—— 后者在跳空股
上会给出实际不可能拿到的成交价, 详见 :func:`_resolve_exit`。

使用约束
-------
- 纯 numpy + polars 输入, 不依赖 FastAPI, 脚本和 notebook 里能直接跑
- ``frame`` 必须已按日期**升序**, 同一交易日一行 (``chanlab.loader`` 已保证)
- **不模拟涨跌停不可成交**: 库里没有涨跌停标记, 硬做需要额外数据源。
  这会让回测在极端行情下偏乐观, 读结论时自己留一格安全边际。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import numpy as np

TRADING_DAYS_PER_YEAR = 252

EXIT_SIGNAL = "signal"
EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_MAX_HOLD = "max_hold"
EXIT_END = "end"


@dataclass
class MatcherConfig:
    """撮合与成本参数。

    默认值贴近 A 股散户实际: 佣金双边万 2.5 且单笔最低 5 元、印花税千 1 仅卖出、
    滑点 5 个基点。``min_commission`` 是小资金回测的关键 —— 不设会把频繁小仓位的
    成本算得过于乐观。
    """

    entry_fill: Literal["close_t", "open_t+1"] = "open_t+1"
    exit_fill: Literal["close_t", "open_t+1"] = "open_t+1"

    commission_pct: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_pct: float = 0.001
    stamp_tax_double_sided: bool = False
    slippage_bps: float = 5.0

    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_hold_days: int | None = None

    initial_capital: float = 1_000_000.0
    #: 单次开仓占用的资金比例, 取值 (0, 1]。单票全仓 = 1.0
    position_pct: float = 1.0
    #: A 股 100 股一手; None 表示允许碎股 (纯百分比验算时用)
    lot_size: int | None = 100
    #: T+1: 当日买入不允许当日卖出。open_t+1 天然满足, 这里给 close_t 兜底
    t_plus_one: bool = True

    def buy_cost_rate(self) -> float:
        """买入费率 (比例部分; 最低佣金在计费时按金额兜)。"""
        stamp = self.stamp_tax_pct if self.stamp_tax_double_sided else 0.0
        return self.commission_pct + stamp + self.slippage_bps / 10_000.0

    def sell_cost_rate(self) -> float:
        """卖出费率 (比例部分)。"""
        return self.commission_pct + self.stamp_tax_pct + self.slippage_bps / 10_000.0


@dataclass
class Trade:
    """一笔完整交易 (开 -> 平)。"""

    symbol: str
    entry_idx: int
    exit_idx: int
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int
    #: 毛收益率 (不含任何成本)
    gross_pnl_pct: float
    #: 净收益率 = 盈亏金额 / 买入金额, 已扣双边成本
    pnl_pct: float
    pnl_amount: float
    buy_fee: float
    sell_fee: float
    duration: int
    exit_reason: str

    @property
    def fees(self) -> float:
        return self.buy_fee + self.sell_fee

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_idx": self.entry_idx,
            "exit_idx": self.exit_idx,
            "entry_date": _date_str(self.entry_date),
            "exit_date": _date_str(self.exit_date),
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "shares": self.shares,
            "gross_pnl_pct": round(self.gross_pnl_pct, 6),
            "pnl_pct": round(self.pnl_pct, 6),
            "pnl_amount": round(self.pnl_amount, 2),
            "buy_fee": round(self.buy_fee, 2),
            "sell_fee": round(self.sell_fee, 2),
            "duration": self.duration,
            "exit_reason": self.exit_reason,
        }


@dataclass
class BacktestResult:
    """回测产物。所有非标量都是 list[dict], 可直接 JSON 序列化。"""

    trades: list[Trade]
    equity_curve: list[dict]
    drawdown_curve: list[dict]
    stats: dict
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stats": self.stats,
            "config": self.config,
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "drawdown_curve": self.drawdown_curve,
        }


# ==================================================================
# 主入口
# ==================================================================


def run_backtest(
    frame: Any,
    entries: Iterable[bool] | np.ndarray | None,
    exits: Iterable[bool] | np.ndarray | None,
    config: MatcherConfig | None = None,
    *,
    symbol: str = "",
) -> BacktestResult:
    """对单标的的一段日线做撮合回测。

    ``entries``/``exits`` 是与 ``frame`` **等长**的布尔序列: True 表示该 bar 上
    产生买点/卖点。信号只是**意图**, 不保证成交 —— 持仓中不再加仓 (no
    pyramiding), 空仓时的卖点直接丢弃, 资金不足一手时该笔作废。

    ``frame`` 需要 ``date``/``open``/``high``/``low``/``close`` 五列。
    """
    cfg = config or MatcherConfig()
    n = frame.height
    if n == 0:
        return BacktestResult([], [], [], _empty_stats(cfg), _config_summary(cfg))

    dates = _to_dates(frame["date"])
    o = _f(frame["open"])
    h = _f(frame["high"])
    lo = _f(frame["low"])
    c = _f(frame["close"])

    ent = _to_bool(entries, n)
    ext = _to_bool(exits, n)

    # open_t+1 是唯一没有未来函数的写法: 收盘才知道的信号只能用次日开盘成交。
    # close_t 留给「盘中就能确认信号」的规则 (例如纯用开盘价判定的)。
    entry_ref = o if cfg.entry_fill == "open_t+1" else c
    exit_ref = o if cfg.exit_fill == "open_t+1" else c
    ent_exec = _shift_true(ent) if cfg.entry_fill == "open_t+1" else ent
    ext_exec = _shift_true(ext) if cfg.exit_fill == "open_t+1" else ext

    trades = _match(dates, o, h, lo, c, ent_exec, ext_exec, entry_ref, exit_ref, cfg, symbol)
    equity, drawdown = _build_curves(dates, c, trades, cfg)
    stats = _calc_stats(trades, dates, equity, cfg)

    return BacktestResult(trades, equity, drawdown, stats, _config_summary(cfg))


# ==================================================================
# 撮合状态机
# ==================================================================


def _match(
    dates: list[date],
    o: np.ndarray,
    h: np.ndarray,
    lo: np.ndarray,
    c: np.ndarray,
    ent: np.ndarray,
    ext: np.ndarray,
    entry_ref: np.ndarray,
    exit_ref: np.ndarray,
    cfg: MatcherConfig,
    symbol: str,
) -> list[Trade]:
    """逐 bar 状态机: 空仓 -> 找买点 -> 持仓 -> 找退出 -> ... 循环。"""
    n = len(o)
    trades: list[Trade] = []

    cash = cfg.initial_capital
    holding = False
    entry_i = -1
    entry_price = 0.0
    shares = 0
    buy_fee = 0.0

    for i in range(n):
        if not holding:
            if ent[i] and np.isfinite(entry_ref[i]) and entry_ref[i] > 0:
                px = float(entry_ref[i])
                lot = _buy_lot(cash, px, cfg)
                if lot is not None:
                    shares, buy_fee = lot
                    cash -= shares * px + buy_fee
                    holding, entry_i, entry_price = True, i, px
            if not holding:
                continue  # 没成交 (无信号或资金不足)

        # 买入成功后 **不跳过本根的退出判定**: T+0 场景下同一根可能既有买点又有
        # 卖点信号; T+1 时 days == 0 会在 _resolve_exit 里被拦住。
        days = i - entry_i
        # close_t 口径允许信号当天成交, 这时要显式拦住当天买当天卖
        skip_signal_exit = cfg.t_plus_one and days == 0
        reason, exit_price = _resolve_exit(
            i, days, entry_price, exit_ref, o, h, lo, ext, cfg, skip_signal_exit
        )
        if reason is None:
            continue

        trade = _close_trade(
            dates, reason, entry_i, i, entry_price, exit_price, shares, buy_fee, cfg, symbol
        )
        trades.append(trade)
        cash += shares * exit_price - trade.sell_fee
        holding, shares, buy_fee = False, 0, 0.0

    # 回测结束仍持仓: 按最后一根收盘平掉, 让绩效反映真实浮盈而不是「假装没这笔」
    if holding:
        i = n - 1
        px = float(c[i]) if np.isfinite(c[i]) else entry_price
        trades.append(
            _close_trade(
                dates, EXIT_END, entry_i, i, entry_price, px, shares, buy_fee, cfg, symbol
            )
        )

    return trades


def _close_trade(
    dates: list[date],
    reason: str,
    entry_i: int,
    exit_i: int,
    entry_price: float,
    exit_price: float,
    shares: int,
    buy_fee: float,
    cfg: MatcherConfig,
    symbol: str,
) -> Trade:
    """按给定价格结算一笔持仓, 生成 :class:`Trade`。"""
    sell_fee = _fee_of(shares, exit_price, cfg.sell_cost_rate(), cfg.min_commission)
    cost = shares * entry_price
    gross = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    amount = shares * (exit_price - entry_price) - buy_fee - sell_fee
    return Trade(
        symbol=symbol,
        entry_idx=entry_i,
        exit_idx=exit_i,
        entry_date=dates[entry_i],
        exit_date=dates[exit_i],
        entry_price=entry_price,
        exit_price=exit_price,
        shares=int(shares),
        gross_pnl_pct=gross,
        pnl_pct=amount / cost if cost > 0 else 0.0,
        pnl_amount=amount,
        buy_fee=buy_fee,
        sell_fee=sell_fee,
        duration=exit_i - entry_i,
        exit_reason=reason,
    )


def _resolve_exit(
    i: int,
    days: int,
    entry_price: float,
    exit_ref: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    lo: np.ndarray,
    ext: np.ndarray,
    cfg: MatcherConfig,
    skip_signal_exit: bool,
) -> tuple[str | None, float]:
    """判定第 i 根是否退出, 返回 ``(原因, 成交价)``; ``None`` 表示继续持有。

    止损/止盈按 **盘中 extremum** 触发 (v1 是按当日 close 事后判定):

    - 止损: ``low <= stop`` 触发, 成交价 ``min(open, stop)`` —— 跳空低开时真实
      成交价就是开盘价, 不可能拿到止损价本身
    - 止盈: ``high >= target`` 触发, 成交价 ``max(open, target)``

    ``days == 0`` 时不做止损/止盈判定: 入场当天的最低价属于入场之前的波动。
    """
    if entry_price <= 0:
        return None, 0.0
    open_i = float(o[i]) if np.isfinite(o[i]) else None

    if cfg.stop_loss_pct is not None and days > 0 and np.isfinite(lo[i]):
        stop = entry_price * (1.0 - abs(cfg.stop_loss_pct))
        if lo[i] <= stop:
            return EXIT_STOP_LOSS, min(open_i, stop) if open_i is not None else stop

    if cfg.take_profit_pct is not None and days > 0 and np.isfinite(h[i]):
        target = entry_price * (1.0 + abs(cfg.take_profit_pct))
        if h[i] >= target:
            return EXIT_TAKE_PROFIT, max(open_i, target) if open_i is not None else target

    if not skip_signal_exit and ext[i] and np.isfinite(exit_ref[i]):
        return EXIT_SIGNAL, float(exit_ref[i])

    if cfg.max_hold_days is not None and days >= cfg.max_hold_days and np.isfinite(exit_ref[i]):
        return EXIT_MAX_HOLD, float(exit_ref[i])

    return None, 0.0


# ==================================================================
# 资金 / 费用
# ==================================================================


def _buy_lot(cash: float, price: float, cfg: MatcherConfig) -> tuple[int, float] | None:
    """按可用资金算手数与买入费用; 资金不足一手返回 ``None``。"""
    budget = cash * min(max(cfg.position_pct, 0.0), 1.0)
    unit = cfg.lot_size if (cfg.lot_size and cfg.lot_size > 0) else 1
    lots = int(budget // (price * unit))
    if lots <= 0:
        return None

    shares = lots * unit
    while shares > 0:
        gross = shares * price
        fee = _fee_of(shares, price, cfg.buy_cost_rate(), cfg.min_commission)
        if gross + fee <= cash:
            return shares, fee
        shares -= unit
    return None


def _fee_of(shares: int, price: float, rate: float, min_fee: float) -> float:
    return max(shares * price * rate, min_fee)


# ==================================================================
# 净值曲线与统计
# ==================================================================


def _build_curves(
    dates: list[date],
    c: np.ndarray,
    trades: list[Trade],
    cfg: MatcherConfig,
) -> tuple[list[dict], list[dict]]:
    """逐 bar 登账构建权益/回撤曲线。

    ⚠️ 与 v1 的关键差异: v1 把整笔收益记在出场日, 持仓期间曲线是平的,
    **过程回撤完全不可见**。这里每天按收盘价给未平仓头寸估值:

        equity[t] = cash[t] + shares * close[t]

    登账顺序按现实: 同一根上「先结算卖出、再扣款买入」—— 反序会让同一天先买
    后卖变成虚假的资金不足。
    """
    n = len(dates)
    if n == 0:
        return [], []

    buys: dict[int, Trade] = {t.entry_idx: t for t in trades}
    sells: dict[int, Trade] = {t.exit_idx: t for t in trades}

    cash = cfg.initial_capital
    shares = 0
    entry_px = 0.0
    equity = np.empty(n, dtype=float)

    for i in range(n):
        if i in sells and shares > 0:
            t = sells[i]
            cash += shares * t.exit_price - t.sell_fee
            shares, entry_px = 0, 0.0
        if i in buys:
            t = buys[i]
            shares, entry_px = t.shares, t.entry_price
            cash -= t.shares * t.entry_price + t.buy_fee

        px = float(c[i]) if np.isfinite(c[i]) else entry_px
        equity[i] = cash + shares * px

    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak

    eq_list = [{"date": _date_str(dates[i]), "value": round(float(equity[i]), 2)} for i in range(n)]
    dd_list = [{"date": _date_str(dates[i]), "value": round(float(dd[i]), 6)} for i in range(n)]
    return eq_list, dd_list


def _calc_stats(
    trades: list[Trade],
    dates: list[date],
    equity: list[dict],
    cfg: MatcherConfig,
) -> dict[str, Any]:
    n_bars = len(dates)
    eq = np.array([e["value"] for e in equity], dtype=float) if equity else np.array([])
    if eq.size == 0:
        return _empty_stats(cfg)

    final = float(eq[-1])
    total_return = final / cfg.initial_capital - 1.0
    years = max(n_bars / TRADING_DAYS_PER_YEAR, 1e-9)
    annual_return = (final / cfg.initial_capital) ** (1.0 / years) - 1.0 if final > 0 else -1.0

    peak = np.maximum.accumulate(eq)
    max_dd = float(np.min((eq - peak) / peak))

    pnls = np.array([t.pnl_pct for t in trades], dtype=float)
    durations = np.array([t.duration for t in trades], dtype=float)
    n_trades = len(trades)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    held_bars = float(durations.sum()) if n_trades else 0.0

    daily_ret = np.diff(eq) / eq[:-1] if eq.size > 1 else np.array([])

    return {
        "initial_capital": round(cfg.initial_capital, 2),
        "final_capital": round(final, 2),
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "max_drawdown": round(max_dd, 6),
        "calmar": round(annual_return / abs(max_dd), 6) if max_dd < 0 else None,
        "sharpe": _sharpe(daily_ret),
        "trades": n_trades,
        "win_rate": round(float(wins.size / n_trades), 6) if n_trades else 0.0,
        "avg_pnl_pct": round(float(pnls.mean()), 6) if n_trades else 0.0,
        "median_pnl_pct": round(float(np.median(pnls)), 6) if n_trades else 0.0,
        "best_pnl_pct": round(float(pnls.max()), 6) if n_trades else 0.0,
        "worst_pnl_pct": round(float(pnls.min()), 6) if n_trades else 0.0,
        "avg_win_pct": round(float(wins.mean()), 6) if wins.size else 0.0,
        "avg_loss_pct": round(float(losses.mean()), 6) if losses.size else 0.0,
        "payoff_ratio": round(abs(float(wins.mean()) / float(losses.mean())), 6)
        if wins.size and losses.size and float(losses.mean()) != 0
        else None,
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss > 0 else None,
        "avg_holding_days": round(float(durations.mean()), 2) if n_trades else 0.0,
        "exposure_pct": round(min(held_bars / n_bars, 1.0), 6) if n_bars else 0.0,
        "total_fees": round(float(sum(t.fees for t in trades)), 2),
        "bars": n_bars,
        "start_date": _date_str(dates[0]) if dates else None,
        "end_date": _date_str(dates[-1]) if dates else None,
        "exit_reasons": _exit_reason_counts(trades),
    }


def _sharpe(daily_ret: np.ndarray, risk_free_annual: float = 0.0) -> float | None:
    """年化夏普。样本 <2 或零波动返回 ``None`` —— 不虚报 0。"""
    r = daily_ret[np.isfinite(daily_ret)]
    if r.size < 2:
        return None
    std = float(np.std(r, ddof=1))
    if std <= 0:
        return None
    excess = float(np.mean(r)) - risk_free_annual / TRADING_DAYS_PER_YEAR
    return round(excess / std * float(np.sqrt(TRADING_DAYS_PER_YEAR)), 6)


def _exit_reason_counts(trades: list[Trade]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
    return out


# ==================================================================
# 网格调参
# ==================================================================


def grid_search(
    frame: Any,
    signal_factory: Callable[[dict[str, Any]], tuple[np.ndarray, np.ndarray]],
    param_grid: dict[str, list[Any]],
    config: MatcherConfig | None = None,
    *,
    metric: str = "total_return",
    top: int | None = None,
    symbol: str = "",
) -> list[dict[str, Any]]:
    """对小网格穷举调参, 返回按 ``metric`` 降序的结果清单。

    ``signal_factory(params)`` 返回 ``(entries, exits)`` 布尔数组 —— 把「怎么算
    信号」留在外面, 引擎只负责撮合, 换任何指标都不用改这里。

    组合数很大时先自己用随机搜索缩范围: 这里是纯 Python 循环, 没做并行。
    """
    keys = list(param_grid)
    rows: list[dict[str, Any]] = []
    for combo in _iter_grid(param_grid):
        params = dict(zip(keys, combo, strict=True))
        try:
            ent, ext = signal_factory(params)
        except ValueError:
            # 参数组合可能让指标的 warmup window 越界, 这类组合直接跳过
            continue
        res = run_backtest(frame, ent, ext, config, symbol=symbol)
        rows.append({"params": params, "stats": res.stats, "trades": len(res.trades)})

    rows.sort(key=lambda x: _metric_value(x["stats"], metric), reverse=True)
    return rows[:top] if top else rows


def _metric_value(stats: dict[str, Any], metric: str) -> float:
    v = stats.get(metric)
    if not isinstance(v, int | float):
        return float("-inf")
    return float(v)


def _iter_grid(param_grid: dict[str, list[Any]]) -> list[tuple]:
    keys = list(param_grid)
    out: list[tuple] = [()]
    for k in keys:
        out = [(*row, v) for row in out for v in param_grid[k]]
    return out


# ==================================================================
# 工具
# ==================================================================


def _config_summary(cfg: MatcherConfig) -> dict[str, Any]:
    return {
        "entry_fill": cfg.entry_fill,
        "exit_fill": cfg.exit_fill,
        "commission_pct": cfg.commission_pct,
        "min_commission": cfg.min_commission,
        "stamp_tax_pct": cfg.stamp_tax_pct,
        "stamp_tax_double_sided": cfg.stamp_tax_double_sided,
        "slippage_bps": cfg.slippage_bps,
        "stop_loss_pct": cfg.stop_loss_pct,
        "take_profit_pct": cfg.take_profit_pct,
        "max_hold_days": cfg.max_hold_days,
        "position_pct": cfg.position_pct,
        "lot_size": cfg.lot_size,
        "t_plus_one": cfg.t_plus_one,
    }


def _empty_stats(cfg: MatcherConfig) -> dict[str, Any]:
    return {
        "initial_capital": round(cfg.initial_capital, 2),
        "final_capital": round(cfg.initial_capital, 2),
        "total_return": 0.0,
        "annual_return": 0.0,
        "max_drawdown": 0.0,
        "calmar": None,
        "sharpe": None,
        "trades": 0,
        "win_rate": 0.0,
        "avg_pnl_pct": 0.0,
        "median_pnl_pct": 0.0,
        "best_pnl_pct": 0.0,
        "worst_pnl_pct": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
        "payoff_ratio": None,
        "profit_factor": None,
        "avg_holding_days": 0.0,
        "exposure_pct": 0.0,
        "total_fees": 0.0,
        "bars": 0,
        "start_date": None,
        "end_date": None,
        "exit_reasons": {},
    }


def _f(s: Any) -> np.ndarray:
    return np.asarray(s.to_numpy() if hasattr(s, "to_numpy") else s, dtype=float)


def _to_bool(x: Iterable[bool] | np.ndarray | None, n: int) -> np.ndarray:
    if x is None:
        return np.zeros(n, dtype=bool)
    arr = x.to_numpy() if hasattr(x, "to_numpy") else np.asarray(list(x))
    if arr.shape[0] != n:
        raise ValueError(f"信号长度 {arr.shape[0]} 与 K 线行数 {n} 不一致")
    return arr.astype(bool)


def _shift_true(flags: np.ndarray) -> np.ndarray:
    """信号右移一根: t 日的信号 t+1 日执行。"""
    out = np.zeros_like(flags)
    out[1:] = flags[:-1]
    return out


def _to_dates(s: Any) -> list[date]:
    vals = s.to_list() if hasattr(s, "to_list") else list(s)
    out: list[date] = []
    for v in vals:
        if isinstance(v, datetime):
            out.append(v.date())
        elif isinstance(v, date):
            out.append(v)
        else:
            out.append(datetime.strptime(str(v)[:10], "%Y-%m-%d").date())
    return out


def _date_str(v: date | datetime | None) -> str | None:
    return None if v is None else str(v)[:10]
