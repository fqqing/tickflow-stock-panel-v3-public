"""读取 v2 自己的 v1 数据镜像 (data/v1mirror/).

这些文件由 ``scripts/migrate_from_v1.py`` 从 v1 **一次性搬来**, 之后由 v2 自己
维护, 不再依赖 v1 进程. 换句话说: 本模块读的是 v2 的资产, 不是 v1 的目录。

为什么不是「直读 v1 data/」
--------------------------
直读看着更省事, 但有个致命缺陷: 一旦 v1 不再启动, 那些文件就永久冻结在迁移那
一天, 指数 / 情绪 / 概念会越用越旧, 而且旧得悄无声息. 镜像 + v2 自维护才是
「单进程独立运行」的正解。

缓存
----
按 (mtime, size) 自动失效, 默认开启. 与 loader.py 的 TICKFLOW_CACHE_FRAMES
用途不同: 那个服务于批量调参 (整轮一次), 这个服务于常驻服务端 (文件变了就换),
而且 watchlist 写回后 mtime 会变, 缓存自动失效, 不需要手动清。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
MIRROR_DIR = ROOT / "data" / "v1mirror"
MANIFEST = MIRROR_DIR / "_manifest.json"

# key -> (mtime, size, frame)
_FRAME_CACHE: dict[str, tuple[float, int, pl.DataFrame]] = {}
_JSON_CACHE: dict[str, tuple[float, int, Any]] = {}

# 概念/行业表里那两列中文列名是同花顺源的原样字段, 没有别名可用。
CONCEPT_COL = "所属概念"
INDUSTRY_COL = "所属同花顺行业"

FIN_TABLES = ("fin_metrics", "fin_income", "fin_balance", "fin_cashflow", "fin_shares")


def mirror_dir() -> Path:
    return MIRROR_DIR


def is_ready() -> bool:
    """镜像目录是否存在且已产出 manifest."""
    return MANIFEST.exists()


def manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:  # 坏了就当空, 上层按缺数据处理
        return {}


def status() -> dict[str, Any]:
    """给 /api/mirror 用: 有哪些数据, 各多少行, 缺什么."""
    man = manifest()
    items = {
        k: {
            "rows": v.get("rows"),
            "at": v.get("at"),
            "kind": v.get("kind"),
        }
        for k, v in man.items()
    }
    keys = set(man)
    return {
        "ready": bool(keys),
        "dir": str(MIRROR_DIR),
        "items": items,
        "missing": [k for k in sorted(keys) if not (MIRROR_DIR / f"{k}.parquet").exists()],
    }


def _path(key: str) -> Path:
    p_json = MIRROR_DIR / f"{key}.json"
    return p_json if p_json.exists() else MIRROR_DIR / f"{key}.parquet"


def load(key: str) -> pl.DataFrame:
    """读一张镜像表 (带缓存). 文件不存在返回空 frame, 不抛异常。"""
    path = _path(key)
    if not path.exists():
        return pl.DataFrame()
    sig = (path.stat().st_mtime, path.stat().st_size)
    hit = _FRAME_CACHE.get(key)
    if hit is not None and (hit[0], hit[1]) == sig:
        return hit[2]
    frame = pl.read_parquet(path)
    _FRAME_CACHE[key] = (sig[0], sig[1], frame)
    return frame


def load_json(key: str) -> Any:
    path = MIRROR_DIR / f"{key}.json"
    if not path.exists():
        return None
    sig = (path.stat().st_mtime, path.stat().st_size)
    hit = _JSON_CACHE.get(key)
    if hit is not None and (hit[0], hit[1]) == sig:
        return hit[2]
    data = json.loads(path.read_text(encoding="utf-8"))
    _JSON_CACHE[key] = (sig[0], sig[1], data)
    return data


# ---------------------------------------------------------------- 自选股


def watchlist() -> list[dict[str, Any]]:
    """自选列表。分组名从 watchlist_groups 里补上。"""
    df = load("watchlist")
    if df.is_empty():
        return []
    groups = load_json("watchlist_groups") or {}
    name_of: dict[str, str] = {}
    if isinstance(groups, dict):
        # v1 的形态既可能是 {id: {name:..}} 也可能是 {id: "名字"}
        for gid, val in groups.items():
            if isinstance(val, dict):
                name_of[gid] = str(val.get("name", gid))
            else:
                name_of[gid] = str(val)
    elif isinstance(groups, list):
        for g in groups:
            gid = str(g.get("id", ""))
            if gid:
                name_of[gid] = str(g.get("name", gid))

    out: list[dict[str, Any]] = []
    for row in df.sort("added_at", descending=True).iter_rows(named=True):
        gids = row.get("group_ids") or []
        out.append(
            {
                "symbol": row.get("symbol"),
                "note": row.get("note") or "",
                "added_at": row.get("added_at"),
                "groups": [name_of.get(g, g) for g in gids],
            }
        )
    return out


def watchlist_add(symbol: str, note: str = "") -> list[dict[str, Any]]:
    """加自选。已存在则只更新备注。写回 v2 自己的镜像文件。"""
    symbol = symbol.strip().upper()
    df = load("watchlist")
    if df.is_empty():
        df = pl.DataFrame(
            {"symbol": [], "added_at": [], "note": [], "group_ids": []},
            schema={"symbol": pl.String, "added_at": pl.String, "note": pl.String,
                    "group_ids": pl.List(pl.String)},
        )
    if df.height and symbol in set(df["symbol"]):
        df = df.with_columns(
            pl.when(pl.col("symbol") == symbol).then(pl.lit(note)).otherwise(pl.col("note")).alias("note")
        )
    else:
        df = pl.concat(
            [
                df,
                pl.DataFrame(
                    {
                        "symbol": [symbol],
                        "added_at": [datetime.now().isoformat(timespec="seconds")],
                        "note": [note],
                        "group_ids": [[]],
                    },
                    schema=df.schema,
                ),
            ]
        )
    _write_watchlist(df)
    return watchlist()


def watchlist_remove(symbol: str) -> list[dict[str, Any]]:
    symbol = symbol.strip().upper()
    df = load("watchlist")
    if not df.is_empty():
        df = df.filter(pl.col("symbol") != symbol)
        _write_watchlist(df)
    return watchlist()


def _write_watchlist(df: pl.DataFrame) -> None:
    path = MIRROR_DIR / "watchlist.parquet"
    df.write_parquet(path)
    _FRAME_CACHE.pop("watchlist", None)


# ---------------------------------------------------------------- 标的元数据


def instruments(symbols: list[str] | None = None) -> pl.DataFrame:
    df = load("instruments")
    if df.is_empty() or not symbols:
        return df
    return df.filter(pl.col("symbol").is_in(symbols))


def instrument_map(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """按 symbol 取元数据 (name / limit_up / float_shares 等)."""
    df = instruments(symbols)
    if df.is_empty():
        return {}
    cols = [c for c in ("symbol", "name", "exchange", "limit_up", "limit_down",
                        "float_shares", "total_shares") if c in df.columns]
    return {r["symbol"]: r for r in df.select(cols).iter_rows(named=True)}


# ---------------------------------------------------------------- 概念 / 行业


def _sector_frame(kind: str) -> tuple[pl.DataFrame, str]:
    if kind == "industry":
        return load("industry"), INDUSTRY_COL
    return load("concept"), CONCEPT_COL


def sector_list(kind: str = "concept", *, limit: int = 200) -> list[dict[str, Any]]:
    """列出全部概念/行业及其成分股数量。概念是分号分隔的多值字段。"""
    df, col = _sector_frame(kind)
    if df.is_empty() or col not in df.columns:
        return []
    exploded = (
        df.select(pl.col("symbol"), pl.col(col).fill_null(""))
        .with_columns(pl.col(col).str.split(";").alias("tags"))
        .explode("tags")
        .with_columns(pl.col("tags").str.strip_chars())
        .filter(pl.col("tags") != "")
    )
    agg = (
        exploded.group_by("tags")
        .len()
        .rename({"tags": "name", "len": "count"})
        .sort("count", descending=True)
        .head(limit)
    )
    return list(agg.iter_rows(named=True))


def sector_members(kind: str, name: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """某个概念/行业下的成分股。行业名用前缀匹配 (医药生物 命中 医药生物-xx)。"""
    df, col = _sector_frame(kind)
    if df.is_empty() or col not in df.columns:
        return []
    hit = df.filter(pl.col(col).fill_null("").str.contains(name, literal=True))
    cols = [c for c in ("symbol", "股票简称", col) if c in hit.columns]
    return list(hit.select(cols).head(limit).iter_rows(named=True))


def symbol_sectors(symbol: str) -> dict[str, list[str]]:
    """反查某只票属于哪些概念 / 行业。"""
    symbol = symbol.strip().upper()
    out: dict[str, list[str]] = {"concept": [], "industry": []}
    for kind in ("concept", "industry"):
        df, col = _sector_frame(kind)
        if df.is_empty() or col not in df.columns:
            continue
        row = df.filter(pl.col("symbol") == symbol)
        if row.is_empty():
            continue
        raw = row.get_column(col)[0] or ""
        sep = ";" if kind == "concept" else "-"
        out[kind] = [s.strip() for s in str(raw).split(sep) if s.strip()]
    return out


# ---------------------------------------------------------------- 情绪 / 指数 / 财务


def regime_recent(days: int = 250) -> list[dict[str, Any]]:
    df = load("regime")
    if df.is_empty():
        return []
    return list(df.sort("date").tail(days).iter_rows(named=True))


def index_daily(symbol: str, days: int = 250) -> list[dict[str, Any]]:
    df = load("index_daily")
    if df.is_empty():
        return []
    return list(
        df.filter(pl.col("symbol") == symbol.strip().upper())
        .sort("date")
        .tail(days)
        .iter_rows(named=True)
    )


def index_symbols() -> list[str]:
    df = load("index_daily")
    if df.is_empty():
        return []
    return sorted(df["symbol"].unique().to_list())


def financials(symbol: str, table: str = "fin_metrics", *, limit: int = 40) -> list[dict[str, Any]]:
    if table not in FIN_TABLES:
        return []
    df = load(table)
    if df.is_empty():
        return []
    return list(
        df.filter(pl.col("symbol") == symbol.strip().upper())
        .sort("period_end", descending=True)
        .head(limit)
        .iter_rows(named=True)
    )
