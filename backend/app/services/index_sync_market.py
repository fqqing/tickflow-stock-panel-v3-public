"""港美股主要指数同步（多市场扩展）。

TickFlow 免费模式无港美股指数，改用腾讯免费接口拉取主要指数日K：
  - 港股: 恒生指数 / 恒生科技 / 恒生国企
  - 美股: 标普500 / 纳斯达克 / 道琼斯 / 罗素2000

存储: kline_index_daily_{hk,us}/date=*/part.parquet（与 A 股指数同构，symbol/date/OHLCV）
维表: instruments_index_{hk,us}/instruments.parquet
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# market → [(symbol, 中文名, 数据源, 源code)]
# 数据源: tencent=fqkline日K; tencent_new=newfqkline日K(美股指数历史); realtime=实时快照兜底
MARKET_INDICES: dict[str, list[tuple[str, str, str, str]]] = {
    "hk": [
        ("HSI", "恒生指数", "tencent", "hkHSI"),
        ("HSTECH", "恒生科技指数", "tencent", "hkHSTECH"),
        ("HSCEI", "恒生国企指数", "tencent", "hkHSCEI"),
        ("HSCCI", "红筹指数", "tencent", "hkHSCCI"),
        ("GEM", "创业板指数", "tencent", "hkGEM"),
        ("HSCI", "恒生综合指数", "tencent", "hkHSCI"),
    ],
    "us": [
        ("SPX", "标普500", "tencent_new", "usINX"),
        ("IXIC", "纳斯达克", "tencent_new", "usIXIC"),
        ("DJI", "道琼斯", "tencent_new", "usDJI"),
        ("NDX", "纳斯达克100", "tencent_new", "usNDX"),
        ("VIX", "VIX恐慌指数", "tencent_new", "usVIX"),
    ],
}


def _fetch_tencent_new_daily(code: str, days: int) -> pl.DataFrame:
    """腾讯 newfqkline 接口（美股指数历史 K 线）。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param={code},day,,,{days},qfq"
    req = urllib.request.Request(url, headers=UA)
    raw = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    data = raw.get("data", {}).get(code, {})
    bars = data.get("qfqday") or data.get("day") or []
    dates, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for b in bars:
        if len(b) < 6:
            continue
        dates.append(b[0])
        opens.append(float(b[1]))
        closes.append(float(b[2]))
        highs.append(float(b[3]))
        lows.append(float(b[4]))
        volumes.append(float(b[5]))
    if not dates:
        return pl.DataFrame()
    return pl.DataFrame({
        "symbol": [code] * len(dates),
        "date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "amount": [0.0] * len(dates),
    })


def _fetch_realtime(code: str) -> pl.DataFrame:
    """腾讯实时行情快照（美股指数等无免费历史的数据源）。

    返回 1 行: symbol/date/open/high/low/close/volume（close=现价, 其余占位）。
    """
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={code}", headers=UA)
    raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", errors="ignore")
    for line in raw.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, payload = line.split("=", 1)
        if var.strip() != f"v_{code}":
            continue
        parts = payload.strip('"').split("~")
        if len(parts) < 33:
            return pl.DataFrame()
        price = float(parts[3])
        chg_pct = float(parts[32]) if parts[32] else 0.0
        return pl.DataFrame({
            "symbol": [code], "date": [datetime.now().date().isoformat()],
            "open": [price], "high": [price], "low": [price],
            "close": [price], "volume": [0.0],
            "change_pct": [chg_pct / 100.0],
        })
    return pl.DataFrame()


def _fetch_tencent_daily(code: str, days: int) -> pl.DataFrame:
    """腾讯前复权日K接口拉取（指数用不复权 day 字段）。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},"
    req = urllib.request.Request(url, headers=UA)
    raw = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    data = raw.get("data", {}).get(code, {})
    bars = data.get("day") or data.get("qfqday") or []
    dates, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for b in bars:
        if len(b) < 6:
            continue
        dates.append(b[0])
        opens.append(float(b[1]))
        closes.append(float(b[2]))
        highs.append(float(b[3]))
        lows.append(float(b[4]))
        volumes.append(float(b[5]))
    if not dates:
        return pl.DataFrame()
    return pl.DataFrame({
        "symbol": [code] * len(dates),
        "date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "amount": [0.0] * len(dates),
    })


def _fetch_eastmoney_daily(code: str, days: int) -> pl.DataFrame:
    """东财历史K线接口（美股指数等腾讯缺失的数据源）。

    返回行: date,open,close,high,low,volume,amount（"2026-08-21,7665.68,7674.37,..."）。
    东财对短连接敏感，失败自动重试 3 次。
    """
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={code}&klt=101&fqt=1&lmt={days}&end=20500101"
           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57")
    raw = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={**UA, "Referer": "https://quote.eastmoney.com/"})
            raw = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            import time
            time.sleep(1.0 * (attempt + 1))
    if raw is None:
        raise RuntimeError(f"东财接口重试 3 次仍失败: {last_err}")
    klines = (raw.get("data") or {}).get("klines") or []
    dates, opens, highs, lows, closes, volumes, amounts = [], [], [], [], [], [], []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        dates.append(parts[0])
        opens.append(float(parts[1]))
        closes.append(float(parts[2]))
        highs.append(float(parts[3]))
        lows.append(float(parts[4]))
        volumes.append(float(parts[5]))
        amounts.append(float(parts[6]))
    if not dates:
        return pl.DataFrame()
    return pl.DataFrame({
        "symbol": [code.split(".")[-1]] * len(dates),
        "date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "amount": amounts,
    })


_FETCHERS = {"tencent": _fetch_tencent_daily, "tencent_new": _fetch_tencent_new_daily, "eastmoney": _fetch_eastmoney_daily, "realtime": _fetch_realtime}


def sync_market_indices(market: str, data_dir: Path, days: int = 400) -> int:
    """同步港美股主要指数日K + 维表。返回写入行数。

    tencent 源写历史日K分区；realtime 源只写最新快照（美股指数免费无历史）。
    """
    if market not in MARKET_INDICES:
        raise ValueError(f"无 {market} 指数配置")
    base = Path(data_dir)
    daily_dir = base / f"kline_index_daily_{market}"
    daily_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    inst_rows: list[dict] = []
    for symbol, name, source, code in MARKET_INDICES[market]:
        try:
            fetcher = _FETCHERS[source]
            df = fetcher(code, days) if source != "realtime" else fetcher(code)
            if df.is_empty():
                logger.warning("指数 %s (%s) 拉取为空", name, code)
                continue
            if source == "realtime":
                # 快照: 每指数一个文件（避免覆盖），kline_index_daily_{market}/latest_{symbol}.parquet
                out = daily_dir / f"latest_{symbol}.parquet"
                df = df.with_columns(pl.lit(symbol).alias("symbol"))
                df.write_parquet(out)
                total += 1
                inst_rows.append({"symbol": symbol, "name": name, "code": code,
                                  "market": market, "type": "index", "snapshot": True})
                logger.info("指数 %s (%s): 实时快照 %.2f", name, code, df["close"][0])
                continue
            df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
            # 统一 symbol = 维表 symbol（HSI/SPX 等），而非数据源 code（hkHSI）
            df = df.with_columns(pl.lit(symbol).alias("symbol"))
            for date_df in df.partition_by("date"):
                dt = date_df["date"][0]
                ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
                # 每指数独立文件（part.parquet 会互相覆盖）
                out = daily_dir / f"date={ds}" / f"{symbol}.parquet"
                out.parent.mkdir(parents=True, exist_ok=True)
                date_df.write_parquet(out)
            total += df.height
            inst_rows.append({"symbol": symbol, "name": name, "code": code,
                              "market": market, "type": "index"})
            logger.info("指数 %s (%s): %d 行", name, code, df.height)
        except Exception as e:  # noqa: BLE001
            logger.warning("指数 %s 同步失败: %s", code, e)

    if inst_rows:
        inst_dir = base / f"instruments_index_{market}"
        inst_dir.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(inst_rows).write_parquet(inst_dir / "instruments.parquet")

    return total


def list_market_indices(market: str) -> list[dict]:
    """市场指数清单（含配置兜底，即使未同步也返回元信息）。"""
    return [
        {"symbol": s, "name": n, "code": c, "market": market, "type": "index"}
        for s, n, source, c in MARKET_INDICES.get(market, [])
    ]
