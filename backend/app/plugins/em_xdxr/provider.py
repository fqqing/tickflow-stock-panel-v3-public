"""东财除权事件 → 通达信口径「乘法前复权」因子 provider。

为什么需要它
============
面板的复权链路是「日K 拉**不复权** → 用 ``data/adj_factor/all.parquet`` 的**逐次倍率**
自行前复权」(``indicators/pipeline.py::_apply_adj_factor``):

    adjusted = raw x cum_prod(ex_factor 至该日) / cum_prod(ex_factor 全部)

这要求 ``ex_factor`` 是**每次除权事件一个稀疏倍率**。而 stock-sdk 的 ``adj`` op 产出的是
「每个交易日一行」的 ``close_hfq / close_none`` —— 那是**累积口径**, 直接喂进去会被
``cum_prod`` 连乘, 价格彻底错。本插件绕开它, 直接从东财 datacenter 的
``RPT_SHAREBONUS_DET`` (分红送配明细) 取**除权事件**, 按通达信/交易师的乘法公式
逐事件算出稀疏倍率。

口径依据
========
用户侧选股工具 ``qushiqinlong/data_source.py`` 已用 28 只交易师基准逐口径验证:

    乘法 28/28  |  腾讯 qfq 的加法口径 27/28  |  不复权 25/28

所以面板也必须用乘法口径, 两边才能逐日对齐。公式 (与工具 ``_apply_qfq`` 同一式)::

    ratio = (P - 每股派息 + 配股数 x 配股价) / (P x (1 + 每股送转 + 配股数))
    ex_factor = 1 / ratio

``P`` = 该除权日**前一根**的收盘价 (不复权, 取本地 ``kline_daily``)。

为什么 ``ex_factor`` 要取倒数
=============================
工具的 ``ratio`` 是「把除权日之前的历史价格乘上去」的倍率 (< 1), 而
``_apply_adj_factor`` 用的是「累积因子 / 累积因子末端」的比值 (> 1 方向)。设工具 bar 因子
为 ``Π(其后各事件 ratio)``, 面板 bar 因子为 ``S_i / S_last``, 令二者相等即得
``ex_factor_k = 1 / ratio_k``。2026-09-17 用五粮液/开滦股份/索菲亚 244 个交易日实测:
本地不复权 + 本插件因子 → 前复权, 与用户工具 ``fetch_kline`` **误差 0.000%**。

已知边界
========
- 东财该报表只有**派息**与**送转**, 没有**配股**字段 (工具走东财的兜底路径同样如此);
  配股事件极罕见, 漏掉时该次复权不完整。走 pytdx 的工具能拿到配股, 这是两边唯一可能的残差。
- 只覆盖 A 股 (``asset_type="stock"``); ETF 返回空表。
- 事件窗口取「本地 K 线最早日 ~ 最晚日」而非 ``start_time``: 前复权以最新价为锚,
  任意 bar 的因子是其**之后**所有事件的倍率乘积, 只取最近一段事件会让历史 bar 因子残缺。
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
import polars as pl

from app.config import settings
from app.parquet import scan_daily_parquet

logger = logging.getLogger(__name__)

# 本插件只提供复权因子; 日K 由日K数据源提供(声明成 daily 会误导设置页)
_DATASETS = ("adj_factor",)

_HOST = "https://datacenter-web.eastmoney.com"
_REPORT = "RPT_SHAREBONUS_DET"
_PAGE_SIZE = 500
# 分页上限(500/页 -> 20 万条事件)。A 股全历史分红送配明细约 6~8 万条, 余量充足。
_MAX_PAGES = 400
_TIMEOUT_S = 20.0
_HEADERS = {
    "Referer": "https://data.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

# 单次复权倍率护栏(与工具 _apply_qfq 一致): 超出区间视为脏数据丢弃
_RATIO_MIN = 0.05
_RATIO_MAX = 1.05

_SUFFIXES = ("SH", "SZ", "BJ")


def _norm_date(raw: object) -> str:
    """东财日期形如 '2026-09-17 00:00:00' -> '2026-09-17'。"""
    return str(raw or "")[:10]


def _norm_symbol(row: dict) -> str | None:
    """优先用 SECUCODE(带交易所后缀), 退化到 SECURITY_CODE 按代码段推断。"""
    secucode = str(row.get("SECUCODE") or "").strip().upper()
    if "." in secucode:
        code, _, suffix = secucode.partition(".")
        if suffix in _SUFFIXES:
            return f"{code}.{suffix}"
    code = str(row.get("SECURITY_CODE") or "").strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code[0] == "6":
        return f"{code}.SH"
    if code[0] in {"0", "3"}:
        return f"{code}.SZ"
    if code[0] in {"4", "8", "9"}:
        return f"{code}.BJ"
    return None


def _fetch_events(start: date, end: date) -> pl.DataFrame:
    """拉全市场除权除息事件 (长表, 一次查询覆盖所有股票, 按页取完)。

    返回 symbol / trade_date / fh(每股派息) / sg(每股送转) 四列。配股字段东财缺失,
    统一置 0 (见模块 docstring 的「已知边界」)。
    """
    flt = f"(EX_DIVIDEND_DATE>='{start.isoformat()}')(EX_DIVIDEND_DATE<='{end.isoformat()}')"
    rows: list[dict] = []
    with httpx.Client(timeout=_TIMEOUT_S, headers=_HEADERS) as client:
        for page in range(1, _MAX_PAGES + 1):
            params = {
                "reportName": _REPORT,
                "columns": "SECUCODE,SECURITY_CODE,EX_DIVIDEND_DATE,"
                "PRETAX_BONUS_RMB,BONUS_IT_RATIO",
                "filter": flt,
                "pageSize": str(_PAGE_SIZE),
                "pageNumber": str(page),
                "sortColumns": "EX_DIVIDEND_DATE",
                "sortTypes": "1",
            }
            try:
                resp = client.get(f"{_HOST}/api/data/v1/get", params=params)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                logger.warning("除权事件拉取失败 (第 %d 页): %s", page, e)
                break
            result = payload.get("result") or {}
            batch = result.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            pages = int(result.get("pages") or 1)
            if page >= pages:
                break

    if not rows:
        return pl.DataFrame()

    out: list[dict] = []
    for r in rows:
        sym = _norm_symbol(r)
        d = _norm_date(r.get("EX_DIVIDEND_DATE"))
        if not sym or len(d) != 10:
            continue
        # 东财字段单位为「每 10 股」, 统一换算成「每股」
        fh = float(r.get("PRETAX_BONUS_RMB") or 0.0) / 10.0
        sg = float(r.get("BONUS_IT_RATIO") or 0.0) / 10.0
        if fh == 0.0 and sg == 0.0:
            continue
        out.append({"symbol": sym, "trade_date": d, "fh": fh, "sg": sg})

    if not out:
        return pl.DataFrame()
    return (
        pl.DataFrame(out)
        .with_columns(pl.col("trade_date").str.to_date(strict=False))
        .drop_nulls("trade_date")
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["symbol", "trade_date"])
    )


def _load_raw_closes(symbols: list[str]) -> pl.DataFrame:
    """读本地不复权日K收盘价。

    注意 ``kline_daily`` 存的**始终是不复权价**: 复权只发生在
    ``kline_daily_enriched`` 的派生列上, 所以这里取到的是推导倍率所需的原始价。
    """
    daily_dir = settings.data_dir / "kline_daily"
    if not daily_dir.exists():
        return pl.DataFrame()
    try:
        return (
            scan_daily_parquet(f"{daily_dir.as_posix()}/**/*.parquet")
            .filter(pl.col("symbol").is_in(symbols))
            .select(["symbol", "date", "close"])
            .collect()
            .drop_nulls("close")
            .sort(["symbol", "date"])
        )
    except Exception as e:
        logger.warning("除权因子: 读取本地日K失败: %s", e)
        return pl.DataFrame()


def _build_factors(raw: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """逐事件算稀疏倍率: 除权日前一根的不复权收盘价代入通达信乘法公式。"""
    by_sym: dict[str, tuple[list[date], list[float]]] = {}
    for sym, dates, closes in (
        raw.group_by("symbol").agg(pl.col("date"), pl.col("close")).iter_rows()
    ):
        by_sym[str(sym)] = (list(dates), [float(c) for c in closes])

    out_rows: list[tuple[str, date, float]] = []
    dropped = 0
    for ex_date, sym, fh, sg in events.select(["trade_date", "symbol", "fh", "sg"]).iter_rows():
        series = by_sym.get(str(sym))
        if not series:
            continue
        dates, closes = series
        idx = bisect.bisect_left(dates, ex_date)
        if idx <= 0:  # 首个 bar 之前的事件: 无「前一根」可用, 也无需复权
            continue
        price = closes[idx - 1]
        if price is None or price <= 0:
            continue
        ratio = (price - float(fh)) / (price * (1.0 + float(sg)))
        if not (_RATIO_MIN < ratio < _RATIO_MAX):
            dropped += 1
            continue
        out_rows.append((str(sym), ex_date, 1.0 / ratio))

    if dropped:
        logger.warning("除权因子: %d 个事件倍率越界(可能为脏数据), 已丢弃", dropped)
    if not out_rows:
        return pl.DataFrame()
    return pl.DataFrame(
        out_rows,
        schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64},
        orient="row",
    ).sort(["symbol", "trade_date"])


@dataclass
class _EmXdxrConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "em_xdxr"
    display_name: str = "东财除权事件(乘法前复权因子)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class EmXdxrAdjProvider:
    """只提供 ``adj_factor`` 数据集的数据源插件。日K 仍由日K数据源提供。"""

    name = "em_xdxr"
    builtin = True

    def __init__(self) -> None:
        self.config = _EmXdxrConfig()
        self.display_name = self.config.display_name

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        return None

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """返回 (symbol, trade_date, ex_factor) 稀疏表, 供 _apply_adj_factor 消费。

        ``start_time`` / ``end_time`` 只用于进度展示, **不裁剪事件窗口** —— 理由见
        模块 docstring「已知边界」: 前复权以最新价为锚, 历史 bar 的因子依赖其之后
        的全部事件, 按增量窗口取事件会让历史 bar 因子残缺。
        """
        if not symbols or asset_type == "etf":
            return pl.DataFrame()
        if on_chunk_done:
            on_chunk_done(1, 1)

        raw = _load_raw_closes(symbols)
        if raw.is_empty():
            logger.warning("除权因子: 本地无日K数据, 无法推导倍率 (先同步日K)")
            return pl.DataFrame()

        ev_start = raw["date"].min()
        ev_end = raw["date"].max()
        events = _fetch_events(ev_start, ev_end)
        if events.is_empty():
            logger.info("除权因子: [%s ~ %s] 区间内无除权事件", ev_start, ev_end)
            return pl.DataFrame()

        factors = _build_factors(raw, events)
        if not factors.is_empty():
            logger.info(
                "除权因子: 事件 %d 条 -> 倍率 %d 条, 覆盖 %d 只 (窗口 %s ~ %s)",
                events.height,
                factors.height,
                factors["symbol"].n_unique(),
                ev_start,
                ev_end,
            )
        return factors
