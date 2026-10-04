"""指数数据源 -- 腾讯公开接口 (qt 快照 + fqkline 日线).

为什么不用镜像
==============
v1 镜像的 ``index_daily`` 只有 253 个交易日 (2025-09 起), 而且 v1 不跑就永久
冻结。腾讯 fqkline 对指数支持 2000 根日线 -- 实测 sh000001 拿到 2018-07-06
起, 是镜像的 8 倍深度。所以:

- **核心指数 (54 个, 见 INDEX_GROUPS) 走自建库**, 8 年历史 + 每天续更
- **长尾指数仍走镜像**, 只有代码没有名字, 作为兜底不缺席

为什么不复用 v1 的 provider
===========================
``quote.py`` 用 importlib 加载 v1 的 tencent provider, 但那个 provider 的标的
池是**个股/ETF**, 指数不在里面; 而且它是"加载 v1 文件"的模式, 与"v2 完全
独立"的目标冲突。指数这块从零写, 依赖只有标准库 urllib, 约 250 行。

代码口径
========
v2 规范 ``000001.SH`` / ``399001.SZ`` / ``899050.BJ``; 腾讯规范 ``sh000001``。
互转用 :func:`_to_tc` / :func:`_from_tc`。注意北证指数代码是 899050 而不是
920xxx, 不能套用股票那套后缀规则。

实测 (2026-10-02)
=================
- qt 快照: 高低频指数全支持, 返回中文名, GBK 编码, 一次可批 20 个
- fqkline: ``count=2000`` 拿到 2000 根 (首 2018-07-06, 末 2026-09-30)
- bj899050 快照有数据, 但 fqkline 只返回 1 根 -- 北证日线是不可用的,
  页面要能容忍这种"有点位没历史"的情况
- ⚠️ **必须带 User-Agent + Referer**: 裸请求会被静默拒绝 (curl 无 UA 返空,
  且不报错 -- 排查时极易误判成"接口挂了")
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

_QT_URL = "http://qt.gtimg.cn/q="
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
#: 自建指数库. 布局与日线库保持一致: kline_index/symbol=000001.SH/data.parquet
INDEX_STORE = DATA_DIR / "kline_index"

#: 快照缓存秒数. 指数变化比个股慢, 盘中 60s 足够
CACHE_TTL = float(os.environ.get("TICKFLOW_QUOTE_TTL", "60"))

#: 默认拉取的日线根数. 2000 根约 8 年, 是腾讯给的上限
DEFAULT_COUNT = int(os.environ.get("TICKFLOW_INDEX_COUNT", "2000"))

_SUFFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
_PREFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}

_CORE_ORDER = ("wide", "style", "dividend", "sector", "bond")

#: 核心指数清单. label 是 v2 自己的显示名 (腾讯给的名偶尔简写不一致, 以这里为准;
#: 空缺时用腾讯返回名兜底)。已实测全部在腾讯有数据。
INDEX_GROUPS: list[dict[str, Any]] = [
    {
        "key": "wide",
        "label": "核心宽基",
        "items": [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
            ("000688.SH", "科创50"),
            ("899050.BJ", "北证50"),
            ("000300.SH", "沪深300"),
            ("000905.SH", "中证500"),
            ("000852.SH", "中证1000"),
            ("000016.SH", "上证50"),
            ("000010.SH", "上证180"),
            ("000009.SH", "上证380"),
            ("000903.SH", "中证A100"),
            ("000906.SH", "中证800"),
            ("399106.SZ", "深证综指"),
            ("399101.SZ", "中小综指"),
            ("399102.SZ", "创业板综"),
            ("399303.SZ", "国证2000"),
        ],
    },
    {
        "key": "style",
        "label": "规模风格",
        "items": [
            ("000043.SH", "超大盘"),
            ("000044.SH", "上证中盘"),
            ("000046.SH", "上证中小"),
            ("000047.SH", "上证全指"),
            ("000020.SH", "中型综指"),
            ("000049.SH", "上证民企"),
            ("000048.SH", "责任指数"),
            ("000021.SH", "180治理"),
            ("399100.SZ", "新指数"),
            ("399008.SZ", "中小300"),
            ("399550.SZ", "央视50"),
        ],
    },
    {
        "key": "dividend",
        "label": "红利防御",
        "items": [
            ("000922.SH", "中证红利"),
            ("399324.SZ", "深证红利"),
            ("000015.SH", "红利指数"),
        ],
    },
    {
        "key": "sector",
        "label": "行业主题",
        "items": [
            ("399997.SZ", "中证白酒"),
            ("399986.SZ", "中证银行"),
            ("399975.SZ", "证券公司"),
            ("399989.SZ", "中证医疗"),
            ("399812.SZ", "养老产业"),
            ("000913.SH", "300医药"),
            ("399808.SZ", "中证新能"),
            ("399995.SZ", "基建工程"),
            ("000998.SH", "中证TMT"),
            ("399996.SZ", "智能家居"),
            ("000949.SH", "中证农业"),
            ("399971.SZ", "中证传媒"),
            ("399967.SZ", "中证军工"),
            ("000819.SH", "有色金属"),
            ("399965.SZ", "800地产"),
            ("000928.SH", "中证能源"),
            ("399998.SZ", "中证煤炭"),
            ("000933.SH", "中证医药"),
            ("000827.SH", "中证环保"),
            ("000032.SH", "上证能源"),
        ],
    },
    {
        "key": "bond",
        "label": "债券",
        "items": [
            ("000832.SH", "中证转债"),
            ("000012.SH", "国债指数"),
            ("399481.SZ", "企债指数"),
        ],
    },
]

#: 所有核心指数的 v2 规范代码, 保持 INDEX_GROUPS 的顺序
CORE_SYMBOLS: list[str] = [s for g in INDEX_GROUPS for s, _ in g["items"]]

#: 首屏默认展示的大盘指数 (跟随用户的持仓/关注习惯)
HEADLINE = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "899050.BJ"]

_Q_CACHE: dict[str, Any] = {"at": 0.0, "rows": {}, "ok": False, "detail": ""}


def _to_tc(symbol: str) -> str:
    """``000001.SH`` -> ``sh000001``."""
    s = symbol.strip().upper()
    if len(s) < 8 or s[-3] != ".":
        return s.lower()
    return _SUFFIX.get(s[-2:], "") + s[:6]


def _from_tc(tc: str) -> str:
    """``sh000001`` -> ``000001.SH``."""
    t = tc.strip().lower()
    return t[2:] + "." + _PREFIX.get(t[:2], "")


def _http(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=_HDR)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _num(raw: str) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def core_groups() -> list[dict[str, Any]]:
    """分组清单 (每项带 symbol/name/group/group_label), 供前端渲染导航."""
    return [
        {
            "key": g["key"],
            "label": g["label"],
            "items": [
                {"symbol": s, "name": n, "group": g["key"], "group_label": g["label"]}
                for s, n in g["items"]
            ],
        }
        for g in INDEX_GROUPS
    ]


def core_name(symbol: str) -> str:
    """清单里登记的显示名. 不在清单返回空串而不是 None, 方便上层 ``or`` 兜底."""
    for s, n in [(s, n) for g in INDEX_GROUPS for s, n in g["items"]]:
        if s == symbol.strip().upper():
            return n
    return ""


def quotes(symbols: list[str] | None = None, ttl: float | None = None) -> dict:
    """指数实时快照.

    params 为空时拉全部核心指数 (54 个, 分 3 批)。任何失败都返回 ``ok=False``
    加 ``detail``, 让前端显示"实时不可用"而不是崩页 -- 与 quote.py 的约定一致。
    """
    limit = CACHE_TTL if ttl is None else ttl
    if (time.perf_counter() - _Q_CACHE["at"]) < limit and _Q_CACHE["rows"]:
        pass
    else:
        rows, detail = _fetch_quotes()
        if rows:
            _Q_CACHE.update(
                {
                    "at": time.perf_counter(),
                    "rows": rows,
                    "ok": True,
                    "detail": "",
                }
            )
        else:
            _Q_CACHE.update(
                {"at": time.perf_counter(), "ok": False, "detail": detail}
            )

    pool: dict[str, dict] = _Q_CACHE.get("rows") or {}
    picked = {s: pool[s] for s in symbols if s in pool} if symbols else dict(pool)
    return {
        "ok": bool(_Q_CACHE["ok"]),
        "detail": _Q_CACHE["detail"],
        "source": "tencent/qt",
        "quotes": picked,
    }


def _fetch_quotes() -> tuple[dict[str, dict], str]:
    targets = CORE_SYMBOLS
    out: dict[str, dict] = {}
    err = ""
    for i in range(0, len(targets), 20):
        chunk = targets[i : i + 20]
        codes = ",".join(_to_tc(s) for s in chunk)
        try:
            raw = _http(_QT_URL + codes).decode("gbk", errors="replace")
        except Exception as exc:
            err = f"qt 快照失败: {type(exc).__name__}: {exc}"
            continue
        for seg in raw.split(";"):
            seg = seg.strip()
            if not seg.startswith("v_"):
                continue
            head, _, body = seg.partition('="')
            body = body.rstrip('"')
            f = body.split("~")
            if not body or len(f) < 33:
                continue
            sym = _from_tc(head[2:])
            last = _num(f[3])
            if last is None:
                continue
            prev = _num(f[4])
            out[sym] = {
                "symbol": sym,
                "name": f[1] or core_name(sym),
                "last_price": last,
                "prev_close": prev,
                "change_pct": _num(f[32]),
                "open": _num(f[5]),
                "high": _num(f[33]) if len(f) > 33 else None,
                "low": _num(f[34]) if len(f) > 34 else None,
                "volume": _num(f[6]),
                "amount": _num(f[37]) if len(f) > 37 else None,
                "updated": f[30] if len(f) > 30 else "",
            }
        time.sleep(0.1)
    return out, (err if not out else "")


def fetch_daily(symbol: str, count: int = DEFAULT_COUNT) -> list[dict[str, Any]]:
    """拉一只指数的日线. 返回 [{symbol,date,open,high,low,close,volume}].

    ⚠️ 北证 (bj899050) 只返回 1 根 -- 腾讯不给北证指数的日线, 这是接口限制不是
    我们的 bug, 上层要按"有点位没历史"处理。
    """
    tc = _to_tc(symbol)
    url = f"{_KLINE_URL}{tc},day,,,{count},qfq"
    try:
        raw = _http(url).decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]

    node = (payload.get("data") or {}).get(tc) or {}
    rows = node.get("day") or node.get("qfqday") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        # [date, open, close, high, low, volume, ...]
        if len(r) < 6:
            continue
        out.append(
            {
                "symbol": symbol.strip().upper(),
                "date": str(r[0]),
                "open": _num(str(r[1])),
                "close": _num(str(r[2])),
                "high": _num(str(r[3])),
                "low": _num(str(r[4])),
                "volume": _num(str(r[5])),
            }
        )
    return out


def store_path(symbol: str) -> Path:
    return INDEX_STORE / f"symbol={symbol.strip().upper()}" / "data.parquet"


def stored_symbols() -> list[str]:
    """自建库里已有哪些指数."""
    if not INDEX_STORE.is_dir():
        return []
    out = []
    for d in INDEX_STORE.iterdir():
        if d.is_dir() and d.name.startswith("symbol=") and (d / "data.parquet").is_file():
            out.append(d.name[7:])
    return sorted(out)


def stored_date(symbol: str) -> str | None:
    """自建库里最后一根的日期. 用于判新鲜度, 只读一列省 IO."""
    import polars as pl

    p = store_path(symbol)
    if not p.is_file():
        return None
    try:
        df = pl.read_parquet(p, columns=["date"])
        if df.is_empty():
            return None
        return str(df["date"].max())
    except Exception:
        return None


def write_store(symbol: str, rows: list[dict[str, Any]]) -> int:
    """把日线写进自建库 (整表覆盖). 返回行数."""
    import polars as pl

    p = store_path(symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(
        rows,
        schema={
            "symbol": pl.Utf8,
            "date": pl.Utf8,
            "open": pl.Float64,
            "close": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "volume": pl.Float64,
        },
    ).unique(subset=["date"], keep="last", maintain_order=True)
    df = df.sort("date")
    # 先写临时文件再替换: 半途崩溃不会留下残缺的企业
    tmp = p.with_suffix(".tmp.parquet")
    df.write_parquet(tmp)
    os.replace(tmp, p)
    return df.height


def read_store(symbol: str, days: int = 0) -> list[dict[str, Any]]:
    """读自建库. days<=0 返回全部."""
    import polars as pl

    p = store_path(symbol)
    if not p.is_file():
        return []
    df = pl.read_parquet(p)
    if days > 0:
        df = df.sort("date").tail(days)
    return list(df.iter_rows(named=True))


def daily(symbol: str, days: int = 250) -> dict:
    """指数日线: 优先自建库, 回退 v1 镜像.

    返回带 ``source`` 与 ``as_of``, 前端必须显式显示数据截止日 -- 镜像那部分
    是会过期的, 不能让用户以为在看今天的行情。
    """
    own = read_store(symbol, days)
    if own:
        return {
            "rows": own,
            "as_of": own[-1]["date"],
            "source": "own",
            "n": len(own),
        }

    try:
        from chanlab import v1mirror

        rows = v1mirror.index_daily(symbol, days)
    except Exception:
        rows = []
    return {
        "rows": rows,
        "as_of": rows[-1]["date"] if rows else None,
        "source": "v1mirror" if rows else "none",
        "n": len(rows),
    }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
