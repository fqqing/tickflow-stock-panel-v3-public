"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

# A 股三个交易所; 港美股按市场注册表（见 app.markets）
_EXCHANGES = ["SH", "SZ", "BJ"]

# 市场 → TickFlow 交易所（多市场扩展）
_MARKET_EXCHANGES: dict[str, list[str]] = {
    "cn": ["SH", "SZ", "BJ"],
    "hk": ["HK"],
    "us": ["US"],
}


def _market_of_exchange(exchange: str) -> str:
    for market, exchanges in _MARKET_EXCHANGES.items():
        if exchange in exchanges:
            return market
    return "cn"


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        row["market"] = _market_of_exchange(item.get("exchange") or "")
        ext = item.get("ext") or {}
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up")
        row["limit_down"] = ext.get("limit_down")
        rows.append(row)
    return rows


def _fetch_instruments_via_provider() -> list[dict] | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(仍应走 tickflow)时返回 None。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def _merge_markets_not_covered(rows: list[dict], data_dir: Path) -> list[dict]:
    """把已有维表里 **provider 未覆盖市场** 的行原样保留下来。

    provider 提供的标的通常只覆盖部分市场 (tushare 只有 A 股), 而面板全量重写
    instruments.parquet。不做合并的话, 一次标的同步就会把港美股 1.5 万行抹掉。
    """
    covered = {r.get("market") for r in rows if r.get("market")}
    if not covered:
        return rows
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return rows
    try:
        old = pl.read_parquet(path)
        if "market" not in old.columns:
            return rows
        keep = old.filter(~pl.col("market").is_in(sorted(covered)))
    except Exception as e:
        logger.warning("读取已有 instruments 维表失败, 跳过多市场合并: %s", e)
        return rows
    if keep.is_empty():
        return rows
    logger.info(
        "instruments: provider 仅覆盖 %s, 保留已有 %d 行其他市场标的",
        sorted(covered),
        keep.height,
    )
    return [*rows, *keep.to_dicts()]


def sync_instruments(data_dir: Path, markets: list[str] | None = None) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    markets: 指定同步哪些市场（cn/hk/us），缺省同步全部市场。
    返回写入的行数。
    """
    from app.markets import ALL_MARKETS, get_market

    if markets is None:
        markets = list(ALL_MARKETS)

    all_rows = _fetch_instruments_via_provider()
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        tf = get_client()
        all_rows = []
        for market in markets:
            for ex in get_market(market).exchanges:
                try:
                    items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                    if items:
                        all_rows.extend(_flatten_instruments(items))
                        logger.info("instruments %s (%s): %d stocks", ex, market, len(items))
                except Exception as e:
                    logger.warning("get_instruments(%s) failed: %s", ex, e)
    elif all_rows:
        # provider 通常只覆盖自己支持的市场 (例: tushare 只给 A 股 —— 港美股行情不在
        # 其积分体系内), 其余市场沿用已有维表; 否则切换日K源会把港美股标的整体抹掉。
        all_rows = _merge_markets_not_covered(all_rows, data_dir)

    if not all_rows:
        return 0

    df = pl.DataFrame(all_rows)
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    df.write_parquet(inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
