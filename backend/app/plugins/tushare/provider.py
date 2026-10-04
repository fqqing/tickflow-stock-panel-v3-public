"""Tushare Pro 数据源插件。

覆盖数据集
==========
``instruments`` / ``daily`` / ``adj_factor`` / ``financial``

分层实现: 走 Tushare 官方的 **HTTP API** (``POST https://api.tushare.pro``) 而不是
``tushare`` pip 包 —— 项目 backend 没有该依赖, HTTP 通道零新增依赖 (只用已有的 httpx),
且与 ``plugins/em_xdxr`` 的形态一致。token 从环境变量 ``TUSHARE_TOKEN`` 读
(项目根 ``.env``, 已被 .gitignore)。

Tushare Pro 的能力边界 (2100 积分档, 2026-09-17 实测)
====================================================
积分接口 (2000 分: 200 次/分钟, 单接口 10 万次/日) 可用:

    stock_basic  daily  fund_daily  index_daily  adj_factor  stk_limit
    daily_basic  income  balancesheet  cashflow  fina_indicator  trade_cal

**不在积分体系内 (需单独付费, 本插件不覆盖)**:

    分钟线 stk_mins (约 1000 元/月)  港美股  Level-2  五档盘口  WebSocket 推送

所以面板的「分钟 K / 实时行情 / 五档」仍需 stock-sdk 或自定义 HTTP 源;
Tushare 在本项目里承担的是**日线与基本面**这条稳定链路。

量纲与口径换算 (逐条实测确认)
==============================
- ``daily`` 的 ``vol`` 单位是 **手** (与面板内部一致), ``amount`` 单位是 **千元**
  (面板内部是元) → x1000。
- ``daily`` 是**不复权**行情, 与面板 ``kline_daily`` 的 raw 口径一致 ——
  实测 000858.SZ 2025-09-15~2026-09-17 共 244 个交易日收盘价 ``max|diff| = 0.0``。
- ``adj_factor`` 是**累积因子** ``A``, 满足 ``close_qfq = close_raw x A_d / A_last``;
  而面板 ``indicators/pipeline.py::_apply_adj_factor`` 要求的是**稀疏逐次倍率**
  ``ex_factor``。由 ``cum_prod(ex_factor) = A`` 反解出::

      ex_factor_i = A_i / A_{i-1}      # 只在 A 真正变化的日子产出

  即「每次除权事件一个倍率」。实测 000858.SZ 两年 657 个自然行里只有 **5 个**变化日,
  与除权除息日一一对应。
- ``daily_basic`` 的 ``total_share`` / ``float_share`` 单位是 **万股** → x1e4。
- ``fina_indicator`` 的比率字段绝大多数已经是**百分点** (``roe`` = 7.34 表示 7.34%),
  **只有 ``ocf_to_or`` 是小数** (-0.0758) → x100。面板财务表统一按百分点存储。
- ``index_daily`` 的 ``amount`` 同样是千元 (上证指数 87.1 万元 → 8711 亿元, 量级正确)。

已知限制
========
1. **``daily`` 是盘后入库** (交易日 15:00~16:00 之间)。当日盘中拿不到今天的 bar ——
   所以「日内分时 / 当日实时」不能靠 Tushare, 面板需另配实时源。
2. **单次响应硬上限 6000 行**, 超出会**静默截断** (实测 50 只 x 1 年恰好 6000 行且被截断)。
   因此批量大小按请求区间**动态反推**, 见 ``_batch_size``。
3. ``adj_factor`` 与 ``plugins/em_xdxr`` 存在约 **2e-5** 的相对差: Tushare 走交易所除权
   参考价口径, em_xdxr 走「除权日前一根收盘价」的乘法口径。em_xdxr 与用户侧选股工具
   (``qushiqinlong``) 逐日一致 (误差 0.000%), 故**除权因子仍建议用 em_xdxr**;
   本插件的 ``adj_factor`` 作为灾备/交叉校验。
4. ``shares`` 表只提供**最新一期**股本快照 (走 ``daily_basic`` 的最近交易日全市场快照),
   与面板 ``latest_only=True`` 的默认调用一致。
5. **多代码批量只有 ``daily`` 支持**。``fund_daily`` / ``index_daily`` 传逗号分隔的多个
   ts_code 不会报错, 而是**静默返回空表** (2026-09-17 实测), 所以它们退化成逐个标的请求。
   财务接口同理: ``income`` / ``balancesheet`` / ``cashflow`` / ``fina_indicator``
   都**只接受单个 ts_code**。因此除 ``daily`` 外的接口都受 200 次/分钟限制, 一次全市场
   财务同步约需 **30 分钟/表**。``period`` 等不带 ``ts_code`` 的全市场查询方式在本 token
   上被直接拒绝 (``code=50101 必填参数, ts_code``)。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import polars as pl

from app.data_providers.custom.provider import _token_from_env
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.market_time import cn_today

logger = logging.getLogger(__name__)

# 本插件声明的数据集 (日K / 除权因子 / 标的维表 / 财务)
_DATASETS = ("instruments", "daily", "adj_factor", "financial")

_HOST = "https://api.tushare.pro"
_TOKEN_ENV = "TUSHARE_TOKEN"
_TIMEOUT_S = 30.0

# Tushare 单次响应硬上限。实测 50 只 x 1 年 = 6000 行, 恰好触顶且无任何提示。
_MAX_ROWS_PER_CALL = 6000
# 留 10% 余量, 避免上游口径微调后静默截断丢数据。
_ROW_BUDGET = _MAX_ROWS_PER_CALL * 9 // 10
# 预算充裕时的批量区间。预算不够时下限让步 (见 _batch_size), 因为 6000 行是硬约束。
_BATCH_MIN = 5
_BATCH_MAX = 50

# 2100 积分档的额度是 200 次/分钟; 留出余量避免踩限流。
_RPM = 180
# 任意两次请求的最小间隔 (秒)。
_INTERVAL_S = 60.0 / _RPM

# 各资产类型对应的日线接口 (面板 asset_type -> Tushare api_name)
_DAILY_API = {
    "stock": "daily",
    "etf": "fund_daily",
    "index": "index_daily",
}

# 只有 daily 支持一次传多个 ts_code (逗号分隔)。fund_daily / index_daily 传多个
# 不会报错, 而是**静默返回空表** (2026-09-17 实测) —— 必须逐个标的请求。
_MULTI_CODE_APIS = frozenset({"daily"})

# 交易日历缓存: 同一天内重复查询没有必要 (面板一次同步会多次调用)。
_CAL_CACHE: dict[str, str] = {}

_SUFFIX_EXCHANGE = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


# ================================================================
# token / 可用性
# ================================================================

def _token() -> str | None:
    """读 TUSHARE_TOKEN: 先环境变量, 再项目根 .env (复用 custom provider 的解析)。"""
    token = _token_from_env(_TOKEN_ENV)
    return token.strip() if token else None


def availability() -> tuple[bool, str]:
    """插件自检 (plugin.yaml 的 check 指向这里)。

    没有 token 时标记为不可用并给出配置提示 —— 不注册进 _PROVIDERS,
    面板设置页会显示 install_hint 而不是让用户在同步时才发现失败。
    """
    token = _token()
    if not token:
        return False, f"未配置 {_TOKEN_ENV}"
    return True, "ok"


# ================================================================
# HTTP 通道
# ================================================================

def _call(api_name: str, params: dict, fields: str = "") -> pl.DataFrame:
    """调用一个 Tushare 接口, 返回 DataFrame (失败返回空表并告警)。

    不抛异常: Tushare 的权限/频次错误通过 ``code != 0`` 返回, 面板的同步流程
    更适合「记一条 warning + 跳过」而不是整条链路崩掉。
    """
    token = _token()
    if not token:
        logger.warning("tushare %s: 未配置 %s", api_name, _TOKEN_ENV)
        return pl.DataFrame()
    # 限速内置于通道本身, 而不是散在各个调用循环里 —— 任何新增的请求路径都自动受限。
    _pace()
    body = {"api_name": api_name, "token": token, "params": params, "fields": fields}
    try:
        resp = httpx.post(_HOST, json=body, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("tushare %s 请求失败: %s", api_name, e)
        return pl.DataFrame()

    code = payload.get("code")
    if code != 0:
        logger.warning("tushare %s 返回错误 code=%s msg=%s", api_name, code, payload.get("msg"))
        return pl.DataFrame()
    return _frame(payload)


def _frame(payload: dict) -> pl.DataFrame:
    """Tushare 的 {fields, items} 结构 -> DataFrame (全部按 Utf8 收, 后续显式转型)。"""
    data = payload.get("data") or {}
    cols = data.get("fields") or []
    items = data.get("items") or []
    if not cols or not items:
        return pl.DataFrame()
    rows = [dict(zip(cols, item, strict=False)) for item in items]
    return pl.DataFrame(rows, infer_schema_length=None)


def _num(df: pl.DataFrame, *cols: str, scale: float = 1.0) -> pl.DataFrame:
    """把若干列转成 Float64 (缺失列跳过), 可选统一缩放。"""
    present = [c for c in cols if c in df.columns]
    if not present:
        return df
    return df.with_columns(
        [(pl.col(c).cast(pl.Float64, strict=False) * scale) for c in present]
    )


def _dates(df: pl.DataFrame, src: str, dst: str) -> pl.DataFrame:
    """'YYYYMMDD' 字符串 -> pl.Date。"""
    if src not in df.columns:
        return df
    return df.with_columns(
        pl.col(src).cast(pl.Utf8, strict=False).str.to_date("%Y%m%d", strict=False).alias(dst)
    )


def _rename_to_panel(df: pl.DataFrame, mapping: dict[str, str]) -> pl.DataFrame:
    """面板字段名 -> Tushare 列名 的映射反着用, **只重命名真实存在的列**。

    ``DataFrame.rename`` 默认 strict: 映射里出现一个不存在的列就整体抛
    ``ColumnNotFoundError``。而 Tushare 返回的字段集是按报表类型/行业浮动的
    (典型: 银行/保险股没有 ``operate_profit``, 部分行业没有 ``inventories``),
    所以这里必须宽容处理, 缺列交给下游 ``keep`` 过滤与 null 语义兜底。
    """
    rev = {v: k for k, v in mapping.items() if v in df.columns}
    return df.rename(rev) if rev else df


# ================================================================
# 批量大小
# ================================================================

def _batch_size(span_days: int) -> int:
    """按 Tushare 单次 6000 行上限, 反推每批标的数。

    ``span_days`` 用**自然日**估算 —— 实际交易日约为其 0.68, 所以方向是保守的。
    仅用于 ``daily`` / ``adj_factor``: 财务接口不支持批量, 只能逐个标的请求。

    ``_BATCH_MIN`` 只是「预算够时别把批量压得太碎」的优化门槛, 一旦预算不足就
    **让位给 6000 行硬上限** (超限会被 Tushare 静默截断, 比多几次往返严重得多):
    窗口长到 ``_ROW_BUDGET // span`` 不足 ``_BATCH_MIN`` 时按预算取值, 最低退化到 1。
    """
    per_symbol = max(int(span_days), 1)
    by_budget = _ROW_BUDGET // per_symbol
    if by_budget >= _BATCH_MIN:
        return min(_BATCH_MAX, by_budget)
    return max(1, by_budget)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


_pace_lock = threading.Lock()
_last_request_at = 0.0


def _pace() -> None:
    """请求间隔限速: 保证任意两次 Tushare 请求之间至少隔 ``_INTERVAL_S`` 秒。

    限速器**必须是模块级共享**的, 不能由各循环自己计步: 财务同步 (逐个标的,
    约 30 分钟/表) 与盘后日线拉取可能同时进行, 各算各的会让合并速率翻倍并
    触发上游频次限制 —— 而 ``_call`` 失败只记 warning 返回空表, 超频等于
    **静默丢数据**。持锁 sleep 让并发任务自然排队。
    """
    global _last_request_at
    with _pace_lock:
        wait = _INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


# ================================================================
# 交易日历
# ================================================================

def _recent_open_dates(days_back: int = 20) -> list[str]:
    """返回最近 ``days_back`` 天内的开市日, **降序** (最新在前)。"""
    today = cn_today()
    start = today - timedelta(days=days_back)
    key = f"{start.isoformat()}_{today.isoformat()}"
    if key in _CAL_CACHE:
        return _CAL_CACHE[key].split(",")
    df = _call(
        "trade_cal",
        {"exchange": "SSE", "start_date": start.strftime("%Y%m%d"),
         "end_date": today.strftime("%Y%m%d"), "is_open": "1"},
        "cal_date,is_open",
    )
    if df.is_empty() or "cal_date" not in df.columns:
        return []
    out = sorted(df["cal_date"].cast(pl.Utf8).to_list(), reverse=True)
    _CAL_CACHE[key] = ",".join(out)
    return out


# ================================================================
# daily
# ================================================================

def _fetch_daily(
    api_name: str,
    symbols: list[str],
    start_time: datetime | None,
    end_time: datetime | None,
    on_chunk_done: Callable[[int, int], None] | None,
) -> pl.DataFrame:
    """拉日线 (不复权), 返回规范化后的面板日K表。"""
    # Tushare 只覆盖 A 股: 港美股代码 (AAPL.US / 00700.HK) 发过去只会拿到空表,
    # 白白消耗配额还把日志刷满 warning。日线标的池可能是多市场兜底
    # (见 daily_pipeline._resolve_universe 的 free 分支), 必须先过滤。
    symbols = _a_share_only(symbols)
    if not symbols:
        if on_chunk_done:
            on_chunk_done(1, 1)
        return pl.DataFrame()

    end = end_time or datetime.now()
    start = start_time or end.replace(year=end.year - 1)
    start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    span = max((end.date() - start.date()).days + 1, 1)

    # fund_daily / index_daily 不支持多代码, 退化成逐个请求。
    size = _batch_size(span) if api_name in _MULTI_CODE_APIS else 1
    chunks = _chunks(symbols, size)
    frames: list[pl.DataFrame] = []
    for i, chunk in enumerate(chunks):
        df = _call(api_name, {"ts_code": ",".join(chunk),
                              "start_date": start_s, "end_date": end_s},
                   "ts_code,trade_date,open,high,low,close,vol,amount")
        if df.is_empty():
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
            continue
        frames.append(df)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    if not frames:
        return pl.DataFrame()

    df = pl.concat(frames, how="diagonal_relaxed")
    df = _num(df, "open", "high", "low", "close")
    # amount: 千元 -> 元 (面板内部口径); vol 已是「手」, 无需换算。
    df = _num(df, "amount", scale=1000.0)
    df = _num(df, "vol")
    df = df.rename({"vol": "volume"} if "vol" in df.columns else {})
    df = _dates(df, "trade_date", "date")
    # 手工转好 pl.Date 后就丢掉 trade_date —— normalize_daily 自己还会把
    # trade_date 改名成 date, 留着会撞成重复列。
    df = df.drop("trade_date")
    # Tushare 返回按 trade_date 降序, 逐票翻正; 去重防多批重叠。
    df = df.drop_nulls("date").unique(subset=["ts_code", "date"], keep="last")
    df = df.sort(["ts_code", "date"])
    # normalize_daily 负责 ts_code->symbol、类型转换、停牌过滤与列裁剪。
    return normalize_daily(df, source="tushare")


# ================================================================
# adj_factor
# ================================================================

def _fetch_adj_factors(
    symbols: list[str],
    start_time: datetime | None,
    end_time: datetime | None,
    on_chunk_done: Callable[[int, int], None] | None,
) -> pl.DataFrame:
    """拉累积复权因子并转成面板要的**稀疏逐次倍率**。

    必须覆盖「本地 K 线最早日 ~ 最晚日」而不是增量窗口: 前复权以最新价为锚,
    任意 bar 的因子依赖其**之后**的全部事件, 按增量窗口取会让历史 bar 因子残缺
    (与 em_xdxr 同一理由)。
    """
    symbols = _a_share_only(symbols)
    if not symbols:
        if on_chunk_done:
            on_chunk_done(1, 1)
        return pl.DataFrame()

    end = end_time or datetime.now()
    start = start_time or end.replace(year=end.year - 1)
    start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    span = max((end.date() - start.date()).days + 1, 1)

    size = _batch_size(span)
    chunks = _chunks(symbols, size)
    frames: list[pl.DataFrame] = []
    for i, chunk in enumerate(chunks):
        df = _call("adj_factor", {"ts_code": ",".join(chunk),
                                  "start_date": start_s, "end_date": end_s},
                   "ts_code,trade_date,adj_factor")
        if not df.is_empty():
            frames.append(df)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))

    if not frames:
        return pl.DataFrame()

    df = pl.concat(frames, how="diagonal_relaxed")
    df = _num(df, "adj_factor")
    df = _dates(df, "trade_date", "trade_date")
    df = df.drop_nulls(["trade_date", "adj_factor"]).unique(
        subset=["ts_code", "trade_date"], keep="last"
    )
    df = df.rename({"ts_code": "symbol"}).sort(["symbol", "trade_date"])

    sparse = to_sparse_factors(df)
    if not sparse.is_empty():
        logger.info(
            "tushare adj_factor: 累积 %d 行 -> 稀疏 %d 个事件, 覆盖 %d 只",
            df.height, sparse.height, sparse["symbol"].n_unique(),
        )
    return normalize_adj_factors(sparse, source="tushare")


def to_sparse_factors(df: pl.DataFrame) -> pl.DataFrame:
    """累积因子 -> 稀疏逐次倍率。

    Tushare 的 ``adj_factor`` 是累积口径 ``A``, 满足 ``close_qfq = close x A_d / A_last``;
    面板 ``_apply_adj_factor`` 走的是 ``adjusted = raw x cum_prod(ex_factor <= d) / total``。
    两者对齐即 ``cum_prod(ex_factor) = A``, 于是::

        ex_factor_i = A_i / A_{i-1}

    只保留 ``A`` 真正变化的日子 —— 绝大多数交易日比值恒为 1, 留下来会让
    ``adj_factor`` 表膨胀到每日一行且毫无信息量。

    同时丢掉**非正/非有限**的倍率: 前值缺失或为 0 时比值会退化成 0 或 inf,
    下游 ``_apply_adj_factor`` 会直接把它乘进价格, 把整段行情打成 0。

    输入需含 symbol / trade_date / adj_factor, 返回 symbol / trade_date / ex_factor。
    """
    if df.is_empty() or "adj_factor" not in df.columns:
        return pl.DataFrame()
    out = (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("adj_factor").shift(1).over("symbol").alias("_prev"))
        .filter(pl.col("_prev").is_not_null() & (pl.col("_prev") > 0))
        .with_columns((pl.col("adj_factor") / pl.col("_prev")).alias("ex_factor"))
        .filter(
            pl.col("ex_factor").is_finite()
            & (pl.col("ex_factor") > 0)
            & ((pl.col("ex_factor") - 1.0).abs() > 1e-9)
        )
        .select(["symbol", "trade_date", "ex_factor"])
        .sort(["symbol", "trade_date"])
    )
    return out


# ================================================================
# instruments
# ================================================================

def _latest_available_daily_basic() -> pl.DataFrame:
    """取最近一个**数据已入库**交易日的全市场 daily_basic 快照。

    ``daily_basic`` 与 ``daily`` 一样是盘后入库, 直接查今天会在 15:00 前拿到空表,
    所以沿交易日历倒着找到第一份有数据的快照。
    """
    for cal_date in _recent_open_dates(20)[:10]:
        fmt = f"{cal_date[:4]}-{cal_date[4:6]}-{cal_date[6:]}"
        df = _call("daily_basic", {"trade_date": cal_date},
                   "ts_code,trade_date,total_share,float_share")
        if not df.is_empty():
            logger.info("tushare daily_basic 快照日期: %s", fmt)
            return df
    return pl.DataFrame()


def _latest_limit_prices() -> pl.DataFrame:
    """取最近一个数据已入库交易日的全市场涨跌停价 (intraday 图要用)。"""
    for cal_date in _recent_open_dates(20)[:10]:
        df = _call("stk_limit", {"trade_date": cal_date},
                   "ts_code,trade_date,up_limit,down_limit")
        if not df.is_empty():
            return df
    return pl.DataFrame()


def _build_instruments() -> list[dict]:
    """组装 A 股标的维表 (flatten 前的 SDK 形态, 供 instrument_sync 复用)。

    字段口径对齐 ``app/services/instrument_sync.py::_flatten_instruments``:
    exchange 用短码 (SH/SZ/BJ)、region 用大区码 (CN)、ext 里放扩展字段。
    """
    basic = _call("stock_basic", {"list_status": "L"},
                  "ts_code,symbol,name,area,industry,market,list_date")
    if basic.is_empty():
        return []

    basic = _dates(basic, "list_date", "listing_date")

    shares = _num(_latest_available_daily_basic(), "total_share", "float_share", scale=1e4)
    if not shares.is_empty():
        shares = shares.select(["ts_code", "total_share", "float_share"])

    limits = _num(_latest_limit_prices(), "up_limit", "down_limit")
    if not limits.is_empty():
        limits = limits.select(["ts_code", "up_limit", "down_limit"])

    df = basic
    if not shares.is_empty():
        df = df.join(shares, on="ts_code", how="left")
    if not limits.is_empty():
        df = df.join(limits, on="ts_code", how="left")

    rows: list[dict] = []
    for r in df.iter_rows(named=True):
        ts_code = r.get("ts_code") or ""
        code, _, suffix = ts_code.partition(".")
        if suffix not in _SUFFIX_EXCHANGE:
            continue
        rows.append({
            "symbol": ts_code,
            "name": r.get("name"),
            "code": code,
            "exchange": _SUFFIX_EXCHANGE[suffix],
            "region": "CN",
            "type": "stock",
            "ext": {
                "listing_date": (
                    r["listing_date"].isoformat() if r.get("listing_date") else None
                ),
                "total_shares": r.get("total_share"),
                "float_shares": r.get("float_share"),
                # A 股最小价格变动单位恒为 0.01 元 (Tushare 不提供该字段)。
                "tick_size": 0.01,
                "limit_up": r.get("up_limit"),
                "limit_down": r.get("down_limit"),
            },
        })
    logger.info("tushare instruments: %d 只 A 股", len(rows))
    return rows


# ================================================================
# financial
# ================================================================

# 面板前端字段 -> Tushare 字段。键是 frontend/src/components/financials/
# StockFinancialDetail.tsx 里 FIELD_DEFS 用的 key, 值是该 key 对应的 Tushare 列。
_INCOME_MAP = {
    "revenue": "revenue",
    "operating_cost": "oper_cost",
    "operating_profit": "operate_profit",
    "selling_expense": "sell_exp",
    "admin_expense": "admin_exp",
    "rd_expense": "rd_exp",
    "financial_expense": "fin_exp",
    "non_operating_income": "non_oper_income",
    "non_operating_expense": "non_oper_exp",
    "total_profit": "total_profit",
    "income_tax": "income_tax",
    "net_income": "n_income",
    "net_income_attributable": "n_income_attr_p",
    "basic_eps": "basic_eps",
    "diluted_eps": "diluted_eps",
}

_BALANCE_MAP = {
    "total_assets": "total_assets",
    "total_current_assets": "total_cur_assets",
    "total_non_current_assets": "total_nca",
    "cash_and_equivalents": "money_cap",
    "accounts_receivable": "accounts_receiv",
    "inventory": "inventories",
    "fixed_assets": "fix_assets",
    "intangible_assets": "intan_assets",
    "goodwill": "goodwill",
    "total_liabilities": "total_liab",
    "total_current_liabilities": "total_cur_liab",
    "total_non_current_liabilities": "total_ncl",
    "short_term_borrowing": "st_borr",
    "long_term_borrowing": "lt_borr",
    "accounts_payable": "acct_payable",
    "total_equity": "total_hldr_eqy_inc_min_int",
    "equity_attributable": "total_hldr_eqy_exc_min_int",
    "retained_earnings": "undistr_porfit",
    "minority_interest": "minority_int",
}

_CASHFLOW_MAP = {
    "net_operating_cash_flow": "n_cashflow_act",
    "net_investing_cash_flow": "n_cashflow_inv_act",
    "net_financing_cash_flow": "n_cash_flows_fnc_act",
    "capex": "c_pay_acq_const_fiolta",
    "net_cash_change": "n_incr_cash_cash_equ",
}

# fina_indicator 里除 ocf_to_or 外都已是百分点; ocf_to_or 是小数, 要 x100。
_METRICS_MAP = {
    "eps_basic": "eps",
    "eps_diluted": "dt_eps",
    "bps": "bps",
    "ocfps": "ocfps",
    "roe": "roe",
    "roa": "roa",
    "gross_margin": "grossprofit_margin",
    "net_margin": "netprofit_margin",
    "debt_to_asset_ratio": "debt_to_assets",
    "revenue_yoy": "or_yoy",
    "net_income_yoy": "netprofit_yoy",
    "inventory_turnover": "inv_turn",
}

_REPORT_APIS = {"income": "income", "balance_sheet": "balancesheet", "cash_flow": "cashflow"}
_REPORT_MAPS = {
    "income": _INCOME_MAP,
    "balance_sheet": _BALANCE_MAP,
    "cash_flow": _CASHFLOW_MAP,
}
# 财务接口的查询窗口: 近 5 年
_REPORT_YEARS = 5


def _a_share_only(symbols: list[str]) -> list[str]:
    """财务接口只覆盖 A 股, 且港美股本就不在积分体系内 —— 先滤掉再发请求。"""
    return [
        s for s in symbols
        if s.endswith((".SH", ".SZ", ".BJ")) and len(s) == 9 and s[:6].isdigit()
    ]


def _collect_per_symbol(
    api: str,
    symbols: list[str],
    fields: str,
    extra: dict | None = None,
    label: str = "",
) -> pl.DataFrame:
    """逐个标的请求并汇总。

    ⚠️ Tushare 的 ``income`` / ``balancesheet`` / ``cashflow`` / ``fina_indicator``
    **只接受单个 ts_code**: 逗号分隔的多代码不会报错, 而是**静默返回空表**
    (实测 2026-09-17)。所以这里没法像 ``daily`` 那样批量, 只能一标的一次请求。

    成本提醒: 2100 积分档是 200 次/分钟, 一次全市场 (约 5600 只) 同步需要
    **约 30 分钟/表**。调用方应知悉, 面板侧建议按需手动触发。
    """
    if not symbols:
        return pl.DataFrame()
    extra = extra or {}
    rows: list[dict] = []
    total = len(symbols)
    for i, sym in enumerate(symbols):
        df = _call(api, {"ts_code": sym, **extra}, fields)
        if not df.is_empty():
            rows.extend(df.to_dicts())
        if total >= 200 and (i + 1) % 200 == 0:
            logger.info("tushare %s: 进度 %d/%d", label or api, i + 1, total)
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def _report_window() -> tuple[str, str]:
    today = cn_today()
    return f"{today.year - _REPORT_YEARS}0101", today.strftime("%Y%m%d")


def _log_report_cost(label: str, n_symbols: int) -> None:
    """把「逐个标的」的时间成本提前说出来, 免得用户以为同步卡死了。"""
    if n_symbols >= 200:
        logger.info(
            "tushare %s: 需逐个标的请求 %d 次, 按 200 次/分钟约 %.0f 分钟",
            label, n_symbols, n_symbols / 200.0,
        )


def _dedupe_reports(df: pl.DataFrame, latest_only: bool) -> pl.DataFrame:
    """同一报告期可能有多条 (合并报表/母公司/调整前后), 只留最终版本。

    ``report_type == '1'`` 是合并报表; 再按 ``announce_date`` 取最新一条
    (上市公司会对早期报表出更正公告)。``latest_only`` 时每只票只留最新一期。
    """
    if df.is_empty() or "period_end" not in df.columns:
        return df
    if "report_type" in df.columns:
        merged = df.filter(pl.col("report_type").cast(pl.Utf8) == "1")
        if not merged.is_empty():
            df = merged
    sort_cols = ["symbol", "period_end"]
    if "announce_date" in df.columns:
        sort_cols.append("announce_date")
    df = df.sort(sort_cols, nulls_last=True).unique(
        subset=["symbol", "period_end"], keep="last"
    )
    if latest_only:
        df = df.sort(["symbol", "period_end"]).group_by("symbol", maintain_order=False).last()
    return df.sort(["symbol", "period_end"], descending=[False, True])


def _fetch_report(table: str, symbols: list[str], latest_only: bool) -> pl.DataFrame:
    """拉三大报表之一 (近 5 年), 映射成面板字段。逐个标的请求, 见 _collect_per_symbol。"""
    api = _REPORT_APIS[table]
    fmap = _REPORT_MAPS[table]
    syms = _a_share_only(symbols)
    if not syms:
        return pl.DataFrame()
    _log_report_cost(table, len(syms))

    start_s, end_s = _report_window()
    fields = ",".join(["ts_code", "end_date", "ann_date", "report_type", *fmap.values()])
    df = _collect_per_symbol(
        api, syms, fields, {"start_date": start_s, "end_date": end_s}, label=table
    )
    if df.is_empty():
        return pl.DataFrame()

    df = _num(df, *fmap.values())
    df = _dates(df, "end_date", "period_end").rename({"ts_code": "symbol"})
    if "ann_date" in df.columns:
        df = df.rename({"ann_date": "announce_date"})
        df = _dates(df, "announce_date", "announce_date")
    # Tushare 字段名 -> 面板字段名
    df = _rename_to_panel(df, fmap)

    # 扣非净利润来自 fina_indicator (利润表本身没有), 单独补一列。
    if table == "income":
        extra = _fetch_deducted_profit(syms, start_s, end_s)
        if not extra.is_empty():
            df = df.join(extra, on=["symbol", "period_end"], how="left")

    keep = ["symbol", "period_end", "announce_date"]
    keep += [c for c in fmap if c in df.columns]
    if "net_income_deducted" in df.columns:
        keep.append("net_income_deducted")
    df = df.select([c for c in keep if c in df.columns])
    return _dedupe_reports(df, latest_only)


def _fetch_deducted_profit(symbols: list[str], start_s: str, end_s: str) -> pl.DataFrame:
    """从 fina_indicator 取扣非净利润 (profit_dedt), 用于补利润表的「扣非净利润」."""
    df = _collect_per_symbol(
        "fina_indicator", symbols, "ts_code,end_date,profit_dedt",
        {"start_date": start_s, "end_date": end_s}, label="profit_dedt",
    )
    if df.is_empty():
        return pl.DataFrame()
    df = _num(df, "profit_dedt")
    df = _rename_to_panel(df, {"net_income_deducted": "profit_dedt"})
    df = _dates(df, "end_date", "period_end").rename({"ts_code": "symbol"})
    if "period_end" not in df.columns:
        return pl.DataFrame()
    return df.drop_nulls("period_end").unique(subset=["symbol", "period_end"], keep="last")


def _fetch_metrics(symbols: list[str], latest_only: bool) -> pl.DataFrame:
    """拉 fina_indicator -> 面板 metrics 表。"""
    syms = _a_share_only(symbols)
    if not syms:
        return pl.DataFrame()
    _log_report_cost("metrics", len(syms))

    start_s, end_s = _report_window()
    fields = ",".join(["ts_code", "end_date", "ann_date", *_METRICS_MAP.values(), "ocf_to_or"])
    df = _collect_per_symbol(
        "fina_indicator", syms, fields, {"start_date": start_s, "end_date": end_s},
        label="metrics",
    )
    if df.is_empty():
        return pl.DataFrame()

    df = _num(df, *dict.fromkeys([*_METRICS_MAP.values(), "ocf_to_or"]))
    # 唯一一个小数口径的比率: 经营现金/营收 (0.0758 -> 7.58 百分点)
    if "ocf_to_or" in df.columns:
        df = df.with_columns((pl.col("ocf_to_or") * 100.0).alias("operating_cash_to_revenue"))
    df = _dates(df, "end_date", "period_end").rename({"ts_code": "symbol"})
    if "ann_date" in df.columns:
        df = df.rename({"ann_date": "announce_date"})
        df = _dates(df, "announce_date", "announce_date")
    df = _rename_to_panel(df, _METRICS_MAP)

    keep = ["symbol", "period_end", "announce_date"]
    keep += [*_METRICS_MAP, "operating_cash_to_revenue"]
    return _dedupe_reports(df.select([c for c in keep if c in df.columns]), latest_only)


def _fetch_shares() -> pl.DataFrame:
    """最新一期股本快照 -> 面板 shares 表 (单位: 股)。"""
    snap = _latest_available_daily_basic()
    if snap.is_empty():
        return pl.DataFrame()
    df = _num(snap, "total_share", "float_share", scale=1e4)
    df = _dates(df, "trade_date", "period_end").rename(
        {"ts_code": "symbol", "total_share": "total_shares", "float_share": "float_shares"}
    )
    return df.select(["symbol", "period_end", "total_shares", "float_shares"]).drop_nulls(
        "period_end"
    )


# ================================================================
# Provider
# ================================================================

@dataclass
class _TushareConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "tushare"
    display_name: str = "Tushare Pro"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: Path | None = None
    builtin: bool = True


class TushareProvider:
    """Tushare Pro 数据源 (日线 / 除权因子 / 标的维表 / 财务)。"""

    name = "tushare"
    builtin = True

    def __init__(self) -> None:
        self.config = _TushareConfig()
        self.display_name = self.config.display_name

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        return None

    # ---- 标的维表 -------------------------------------------------

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """返回 flatten 前的标的行 (instrument_sync 会再过一遍 _flatten_instruments)。

        只覆盖 A 股 —— Tushare 的港股/美股日线不在积分体系内, 面板的港美股标的
        仍应从原数据源同步(见 services/instrument_sync.py 的市场合并逻辑)。
        """
        if asset_type and asset_type != "stock":
            return []
        return _build_instruments()

    # ---- 日线 -----------------------------------------------------

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日线 (不复权)。asset_type 决定走 daily / fund_daily / index_daily。"""
        api = _DAILY_API.get(asset_type or "stock")
        if api is None:
            logger.warning("tushare get_daily: 不支持的 asset_type=%s", asset_type)
            return pl.DataFrame()
        return _fetch_daily(api, symbols, start_time, end_time, on_chunk_done)

    # ---- 复权因子 -------------------------------------------------

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """复权因子 (累积 -> 稀疏逐次倍率)。指数与 ETF 无复权概念。"""
        if asset_type in {"index", "etf"}:
            return pl.DataFrame()
        if on_chunk_done and not symbols:
            on_chunk_done(1, 1)
        return _fetch_adj_factors(symbols, start_time, end_time, on_chunk_done)

    # ---- 财务 -----------------------------------------------------

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """财务表: metrics / income / balance_sheet / cash_flow / shares。"""
        if not symbols:
            return pl.DataFrame()
        if table in _REPORT_APIS:
            return _fetch_report(table, symbols, latest_only)
        if table == "metrics":
            return _fetch_metrics(symbols, latest_only)
        if table == "shares":
            # 只提供最新一期快照 (详见模块 docstring「已知限制」第 4 条)。
            return _fetch_shares()
        logger.warning("tushare get_financials: 未知表 %s", table)
        return pl.DataFrame()
