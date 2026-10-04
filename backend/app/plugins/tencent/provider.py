"""腾讯行情分钟 K provider。

为什么需要它
============
现用的 ``stocksdk``(东方财富) 分钟链路已基本失效, 详见 plugin.yaml。
腾讯 ``mkline`` 是同场景下唯一实测可用的免费源:

    GET https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m1,,320

2026-09-28 实测 ~90~160 只/秒, 沪深 300 只样本 0 失败; 而东财同口径 27.6s 后仍 0 根。

⚠️ 上游行格式的反直觉之处
=========================
腾讯返回的行是::

    ['202609281456', '1243.30', '1243.38', '1243.49', '1242.60', '156.00', {}, '0.12']
       datetime      open       close      high       low        vol(手)  {}   换手率基点

**第 2 位是 close, 不是 high** —— 与常见的 OHLC 顺序不同, 映射错会让最高/最低价
互换, 而因为 high/low 恰好常常包住 open/close, 不显式核对极值关系很难发现。
上面的样例可用 ``high >= max(open, close)`` 且 ``low <= min(open, close)`` 反证。

量纲
====
与项目内部口径(``memory/MEMORY.md``「内部量纲」节)的对照:

| 字段     | 腾讯原始        | 内部口径 | 处理             |
| -------- | --------------- | -------- | ---------------- |
| volume   | 手              | 手       | 直接用           |
| amount   | **不提供**      | 元       | 用 see 下方公式估算 |
| 第 7 位  | 换手率基点      | 非成交额 | **不能用**       |

``amount`` 需自算: ``vol(手) x 100 x price``。实测(600519 2026-09-28, 241 根 m1)
用收盘价估算得 34.87 亿, 本地 enriched 当日 amount = 34.89 亿, 相对差 ~0.06%,
符合 panel 对 amount 的一致性要求。

已知边界
========
- **单次上限约 482 根 bar**: ``count`` 传 <=320 生效, 传更大值会被静默封顶到 320;
  留空反而返回 482 根。故一律留空以拿满。折合 m1 ≈ 2 个交易日 / m5 ≈ 11 个交易日。
- **不支持 beg/end 区间**: 传了返回 0 根(不是报错), 所以本 provider **忽略
  start_time/end_time**, 由下游按 datetime 自行裁剪。
- **不支持批量**: param 拼多只返回空 data, 只能单只请求 + 线程池并发。
- **北交所无分钟数据**: 430/83x/87x/920 各号段实测 mkline 均返回 0 根。
  这些标的自动回落到 stock-sdk(``_bj_fallback``), 失败则静默丢弃 —— 前端分时图
  本就对无数据做了容错。

实时快照 (qt) 与分钟 (mkline) 是两套接口
========================================
本插件同时提供 ``realtime``, 走的是另一个端点::

    GET https://qt.gtimg.cn/q=sh600519,sz000001,sh000001

两者差异(实测 2026-09-29):

| 能力         | mkline (分钟)        | qt (实时快照)                |
| ------------ | -------------------- | ---------------------------- |
| 批量         | 不支持(只能单只)     | **支持, 实测 500 只/请求**   |
| 北交所       | 不支持               | **支持** (bj920xxx)          |
| 指数         | 不支持               | **支持** (sh000001/sz399001) |
| ETF          | 支持                 | 支持                         |
| 成交额       | 不提供(需估算)       | **直接提供** (万元)          |
| vol 单位     | 688/689 为「股」     | **同为** 688/689 为「股」    |

批量上限实测: 50/100/200/500 只均 200 且 ~0.13s; 1000 只直接 HTTP 414
(URI 过长)。故每批取 100 只, 兼顾 URL 长度与风控。
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from app.data_providers.base import AssetType
from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

_DATASETS = ("minute", "realtime")

_HOST = "https://ifzq.gtimg.cn"
_MKLINE = f"{_HOST}/appstock/app/kline/mkline"

#: 实时快照端点。与 mkline 不同 host, 但同属腾讯财经。
_QT = "https://qt.gtimg.cn/q="

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    # 实测不带也能通, 带上更贴近浏览器来源。
    "Referer": "https://gu.qq.com/",
}
_TIMEOUT_S = 15.0

#: 周期映射。腾讯用 m1/m5/m15/m30/m60, 不支持 n 分钟自定义周期。
_FREQ_TO_PERIOD: dict[str, str] = {
    "1m": "m1",
    "5m": "m5",
    "15m": "m15",
    "30m": "m30",
    "60m": "m60",
}
_DEFAULT_PERIOD = "m1"

#: 并发上限。腾讯实测不限流, 但保守起见控制连接数: 过高会增加被风控的概率,
#: 而收益已被单连接 ~0.15s 的延迟摊薄(workers 24 → 40 实测吞吐几乎不再上升)。
_MAX_WORKERS = 24

#: 北交所前缀 —— 腾讯 mkline 不支持, 需回落到 stock-sdk。
_BJ_SUFFIX = ".BJ"

_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

#: 这几个代码段的分钟 vol 单位是「股」而非「手」(见 parse_bars 的量纲说明)。
_VOL_IN_SHARES_PREFIXES = ("688", "689")

# ===== 实时快照 (qt) =====

#: 每批代码数。实测 500 只仍 200, 但 1000 只会 414; 取 100 兼顾 URL 长度与风控。
_QT_BATCH = 100

#: 实时快照并发。全市场约 5500 只 = 56 批, 并发 8 时整轮 ~1s。
_QT_WORKERS = 8

# qt 行是按 "~" 切分的定长字段(完整行 60+ 字段), 只取用到的下标。
_QT_F_NAME = 1
_QT_F_CLOSE = 3
_QT_F_PREV_CLOSE = 4
_QT_F_OPEN = 5
_QT_F_VOLUME = 6
_QT_F_TIME = 30
_QT_F_CHANGE_AMOUNT = 31
_QT_F_CHANGE_PCT = 32
_QT_F_HIGH = 33
_QT_F_LOW = 34
_QT_F_AMOUNT_WAN = 37
_QT_F_TURNOVER = 38
_QT_F_AMPLITUDE = 43

#: 实时快照输出列(与 quote_service 的 realtime record 契约一致)。
_RT_COLUMNS = [
    "symbol",
    "name",
    "last_price",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "change_pct",
    "change_amount",
    "amplitude",
    "turnover_rate",
    "timestamp",
]

#: 本地标的维表缓存: (mtime, symbols)。qt 需要显式代码清单, 而 get_realtime()
#: 是无参的全市场契约, 故从本地维表枚举。盘中每轮都重读 parquet 太浪费。
_SYMBOLS_CACHE: tuple[float, list[str]] | None = None


def _ssl_context() -> ssl.SSLContext:
    """弱化校验的 SSL context。

    腾讯财经站点在部分网络环境下证书链不完整, httpx 严格校验会直接失败;
    分钟数据本身非敏感, 这里只为连通性放宽。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL_CTX = _ssl_context()


def app_to_tencent(sym: str) -> str | None:
    """600519.SH -> sh600519。非沪深交易所返回 None(不可用)。"""
    code, _, suffix = str(sym or "").partition(".")
    suffix = suffix.upper()
    if suffix == "SH":
        return "sh" + code
    if suffix == "SZ":
        return "sz" + code
    return None


def _http_get(url: str) -> dict | None:
    """单次 GET + JSON 解析。失败返回 None(不抛)。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.debug("腾讯分钟请求失败: %s | %s", url, e)
        return None
    if "=" in body[:40]:  # jsonp 形态: xxx={...}
        body = body.split("=", 1)[1]
    try:
        return json.loads(body)
    except Exception:
        logger.debug("腾讯分钟返回非 JSON: %s", body[:120])
        return None


def _fetch_bars(tcode: str, period: str) -> list[list]:
    """拉单只标的单周期的分钟 bar 原始行。count 留空以取最大可用跨度。"""
    payload = _http_get(f"{_MKLINE}?param={tcode},{period},,")
    if not payload:
        return []
    node = (payload.get("data") or {}).get(tcode) or {}
    return node.get(period) or []


def _to_float(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def parse_bars(rows: list[list], symbol: str) -> list[dict]:
    """原始行 -> 内部 schema(dict 列表)。amount 由 vol x 100 x close 估算。

    ⚠️ 量纲陷阱: 科创板(688xxx)的 vol 单位是**股**, 其余板块是**手**。
    实测 2026-09-28 分钟合计 / 日 K volume 的比值:
        600519.SH(主板) = 1.0   300750.SZ(创业板) = 1.0   688788.SH(科创板) = 100.0
    不处理的话科创板约 500 只标的的分钟 volume / amount 会整体放大 100 倍。
    """
    vol_div = 100.0 if symbol[:3] in _VOL_IN_SHARES_PREFIXES else 1.0
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        ts = str(row[0] or "")
        # 腾讯形如 202609281456(12 位)。不足 12 位或非数字直接丢弃。
        if len(ts) != 12 or not ts.isdigit():
            continue
        open_ = _to_float(row[1])
        close_ = _to_float(row[2])
        high_ = _to_float(row[3])
        low_ = _to_float(row[4])
        vol = _to_float(row[5])
        if close_ is None or vol is None:
            continue
        try:
            dt = datetime.strptime(ts, "%Y%m%d%H%M")
        except ValueError:
            continue
        out.append(
            {
                "symbol": symbol,
                "datetime": dt,
                "open": open_,
                "high": high_,
                "low": low_,
                "close": close_,
                "volume": vol / vol_div,
                # 腾讯不给成交额(第 7 位是换手率基点, 不是 amount)。
                # 用 成交量(手) x 100 x 收盘价 估算, 实测与本地日 K amount 相对差 ~0.06%。
                "amount": vol / vol_div * 100.0 * close_,
            }
        )
    return out


def app_to_qt(sym: str) -> str | None:
    """600519.SH -> sh600519。**实时快照专用**, 与 mkline 的 app_to_tencent 分开。

    区别在北交所: qt 快照支持 bj920xxx(实测有数据), 而 mkline 明确不支持,
    故分钟链路仍用 app_to_tencent 把 BJ 挑出去走回落。
    """
    code, _, suffix = str(sym or "").partition(".")
    suffix = suffix.upper()
    if suffix == "SH":
        return "sh" + code
    if suffix == "SZ":
        return "sz" + code
    if suffix == "BJ":
        return "bj" + code
    return None


def qt_to_app(tcode: str) -> str | None:
    """sh600519 -> 600519.SH。app_to_qt 的逆变换。"""
    if len(tcode) <= 2:
        return None
    prefix, code = tcode[:2].lower(), tcode[2:]
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix)
    return f"{code}.{suffix}" if suffix else None


def _http_get_text(url: str) -> str | None:
    """GET 文本(gbk 系编码)。失败返回 None(不抛)。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
            raw = resp.read()
    except Exception as e:
        logger.debug("腾讯实时请求失败: %s | %s", url[:80], e)
        return None
    # 腾讯财经返回 gbk; 股票名含生僻字时 gbk 会抛, 用 gb18030 超集兜底。
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "replace")


def parse_quote_line(tcode: str, fields: list[str]) -> dict | None:
    """一行 qt 快照 -> 内部 realtime record。结构/停牌异常返回 None。

    ⚠️ 量纲换算(与 ``_normalize_realtime_rows`` 的契约一致):

    | 字段           | 腾讯原始        | 内部口径  | 处理             |
    | -------------- | --------------- | --------- | ---------------- |
    | amount         | **万元**        | 元        | *10000           |
    | change_pct     | 百分数 -0.67    | 小数      | /100             |
    | amplitude      | 百分数 1.21     | 小数      | /100             |
    | turnover_rate  | 百分数 0.21     | 小数      | /100             |
    | volume         | 手(688/689 股)  | 手        | /100 (仅 688/689)|

    ``turnover_rate`` 之所以要 /100: quote_service._build_quote_extra 会再 *100
    存成百分数, 入口契约是小数制。
    """
    if len(fields) <= _QT_F_AMPLITUDE:
        return None
    close = _to_float(fields[_QT_F_CLOSE])
    # 停牌 / 无效代码: 腾讯返回 close=0 且各字段为空壳, 直接丢弃,
    # 否则会把 0 价写进日K(quote_service 虽然会用 close 补 OHLC, 但 0 价本身是错的)。
    if close is None or close <= 0:
        return None
    symbol = qt_to_app(tcode)
    if symbol is None:
        return None
    prev_close = _to_float(fields[_QT_F_PREV_CLOSE])
    open_ = _to_float(fields[_QT_F_OPEN])
    high = _to_float(fields[_QT_F_HIGH])
    low = _to_float(fields[_QT_F_LOW])
    # 停牌 / 零成交: 腾讯把 open/high/low 全给 0(现价仍是昨收)。0 值会让下游
    # 的蜡烛图从 0 起画, 也会触发量纲自检的 low<=0 违例。这里统一用 close 补齐
    # (停牌日 OHLC 等价, 与 quote_service._build_daily 的兜底一致)。
    if not open_:
        open_ = close
    if not high:
        high = close
    if not low:
        low = close
    vol = _to_float(fields[_QT_F_VOLUME])
    amount_wan = _to_float(fields[_QT_F_AMOUNT_WAN])
    pct = _to_float(fields[_QT_F_CHANGE_PCT])
    amplitude = _to_float(fields[_QT_F_AMPLITUDE])
    turnover = _to_float(fields[_QT_F_TURNOVER])

    # 与分钟同源同病: 688/689 的 vol 单位是「股」。
    vol_div = 100.0 if symbol[:3] in _VOL_IN_SHARES_PREFIXES else 1.0
    volume = (vol / vol_div) if vol is not None else None
    amount = (amount_wan * 10000.0) if amount_wan is not None else None

    # 时间形如 20260929161458(14 位)。盘后返回的是当日收盘那一刻。
    ts_ms: int | None = None
    raw_time = (fields[_QT_F_TIME] or "").strip()
    if len(raw_time) == 14 and raw_time.isdigit():
        try:
            dt = datetime.strptime(raw_time, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
            ts_ms = int(dt.timestamp() * 1000)
        except ValueError:
            ts_ms = None

    return {
        "symbol": symbol,
        "name": (fields[_QT_F_NAME] or "").strip() or None,
        "last_price": close,
        "prev_close": prev_close,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": amount,
        "change_pct": (pct / 100.0) if pct is not None else None,
        "change_amount": _to_float(fields[_QT_F_CHANGE_AMOUNT]),
        "amplitude": (amplitude / 100.0) if amplitude is not None else None,
        "turnover_rate": (turnover / 100.0) if turnover is not None else None,
        "timestamp": ts_ms,
    }


def _qt_parse_body(body: str) -> list[dict]:
    """qt 响应体 -> records。形如 v_sh600519="1~贵州茅台~..."; 多条以分号分隔。"""
    out: list[dict] = []
    for chunk in body.split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key = chunk.split("=", 1)[0].strip()
        if not key.startswith("v_"):
            continue
        payload = chunk.split('"', 2)
        if len(payload) < 2:
            continue
        tcode = key[2:]
        row = parse_quote_line(tcode, payload[1].split("~"))
        if row is not None:
            out.append(row)
    return out


def _qt_fetch_batch(tcodes: list[str]) -> list[dict]:
    """拉一批(<=_QT_BATCH)代码的实时快照。失败返回空列表(不抛)。"""
    body = _http_get_text(_QT + ",".join(tcodes))
    if not body:
        return []
    return _qt_parse_body(body)


def _local_market_symbols() -> list[str]:
    """从本地标的维表枚举全市场 A 股 + ETF 代码, 供 qt 按批查询。

    qt 必须显式传代码列表(不像东财 batch.cn 那样一次给全市场), 而
    ``get_realtime()`` 的契约是无参全市场 —— 所以从本地维表取清单。
    维表由 tushare/管道维护, 与面板口径一致, 且省掉一次网络枚举。

    缓存按文件 mtime 判定, 盘中轮询不会反复读 parquet。
    """
    global _SYMBOLS_CACHE
    from app.config import settings

    paths = [
        Path(settings.data_dir) / "instruments" / "instruments.parquet",
        Path(settings.data_dir) / "instruments_etf" / "instruments_etf.parquet",
    ]
    exists = [p for p in paths if p.exists()]
    if not exists:
        return []
    stamp = max(p.stat().st_mtime for p in exists)
    if _SYMBOLS_CACHE is not None and _SYMBOLS_CACHE[0] == stamp:
        return _SYMBOLS_CACHE[1]

    symbols: list[str] = []
    seen: set[str] = set()
    for path in exists:
        try:
            df = pl.read_parquet(path, columns=["symbol"])
        except Exception as e:
            logger.warning("腾讯实时: 读取标的维表失败 %s: %s", path.name, e)
            continue
        for sym in df["symbol"].cast(pl.Utf8).to_list():
            sym = (sym or "").strip()
            # 只查 A 股(沪深北)。维表里混了港美股, qt 查不了。
            if not sym.endswith((".SH", ".SZ", ".BJ")) or sym in seen:
                continue
            seen.add(sym)
            symbols.append(sym)
    _SYMBOLS_CACHE = (stamp, symbols)
    return symbols


@dataclass
class _TencentConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "tencent"
    display_name: str = "腾讯行情(分钟K)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class TencentMinuteProvider:
    """腾讯行情数据源: ``minute``(mkline) + ``realtime``(qt 快照)。

    类名沿用 TencentMinuteProvider 是为了不动 plugin.yaml 的 entry 引用。
    """

    name = "tencent"
    builtin = True

    def __init__(self) -> None:
        self.config = _TencentConfig()
        self.display_name = self.config.display_name

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        return None

    # ---- realtime (qt 快照) ----
    # 与 stock-sdk 的 batch.cn 不同, qt **不含**指数(指数要按码单查),
    # 故显式声明该能力, 让 quote_service 走 _fetch_plugin_index_quotes 补拉。
    supports_index_realtime = True

    def _qt_fetch(self, symbols: list[str]) -> list[dict]:
        """按批并发拉 qt 快照。只查沪深北(其余后缀静默丢弃)。"""
        tcodes: list[str] = []
        for sym in symbols:
            code = app_to_qt(sym)
            if code:
                tcodes.append(code)
        if not tcodes:
            return []
        batches = [tcodes[i : i + _QT_BATCH] for i in range(0, len(tcodes), _QT_BATCH)]
        workers = min(_QT_WORKERS, max(1, len(batches)))
        t0 = time.perf_counter()
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(_qt_fetch_batch, batches):
                rows.extend(part)
        logger.info(
            "腾讯实时快照: %d 只请求 / %d 只返回, %d 批, %.2fs",
            len(tcodes),
            len(rows),
            len(batches),
            time.perf_counter() - t0,
        )
        return rows

    def get_realtime(self) -> list[dict]:
        """全市场(A 股股票 + ETF)实时快照。

        返回行与 quote_service 的 realtime record 契约一致(见 _RT_COLUMNS),
        量纲已在 parse_quote_line 归一。
        """
        symbols = _local_market_symbols()
        if not symbols:
            logger.warning("腾讯实时: 本地标的维表为空, 无法枚举全市场(请先跑一次标的同步)")
            return []
        rows = self._qt_fetch(symbols)
        if not rows:
            logger.warning("腾讯实时: 全市场快照返回 0 行(可能被风控或网络不通)")
        return rows

    def get_index_realtime(self, symbols: list[str]) -> list[dict]:
        """按码单拉指数实时快照(返回行与 get_realtime 同 schema)。"""
        syms = [s for s in (symbols or []) if s]
        if not syms:
            return []
        return self._qt_fetch(syms)

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "minute":
            df = self.get_minute(symbols or ["600519.SH"], None, None)
            return {
                "provider": self.name,
                "dataset": "minute",
                "rows": df.height,
                "columns": df.columns,
                "preview": df.head(5).to_dicts() if not df.is_empty() else [],
            }
        if dataset == "realtime":
            # 全市场拉取太慢, 试拉只查给定标的(默认几只标杆 + 科创板验证量纲)。
            probe = symbols or ["600519.SH", "000001.SZ", "688981.SH", "920002.BJ"]
            rows = self._qt_fetch(probe)
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(rows[0].keys()) if rows else [],
                "preview": rows[:5],
            }
        raise ValueError(f"腾讯行情不支持数据集: {dataset}")

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K。

        ``start_time`` / ``end_time`` 由调用方传达过来但**上游不支持区间参数**
        (传了会返回 0 根), 故这里忽略, 一律取最近约 482 根 bar, 由下游自行裁剪。
        """
        if not symbols:
            return pl.DataFrame()

        period = _FREQ_TO_PERIOD.get(str(freq or "").strip().lower(), _DEFAULT_PERIOD)
        routed = [(s, app_to_tencent(s)) for s in symbols]
        cn_pairs = [(s, c) for s, c in routed if c]
        bj_symbols = [s for s, c in routed if not c and s.upper().endswith(_BJ_SUFFIX)]

        total = len(cn_pairs) + (1 if bj_symbols else 0)
        if total == 0:
            # 全部被过滤掉时仍回调一次, 否则前端进度条卡在 0(见 skill §5)。
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        frames: list[pl.DataFrame] = []
        workers = min(_MAX_WORKERS, max(1, len(cn_pairs)))
        if cn_pairs:
            logger.info(
                "腾讯分钟K 拉取开始(%d symbols, period=%s, workers=%d)",
                len(cn_pairs),
                period,
                workers,
            )

            def one(pair: tuple[str, str]) -> pl.DataFrame:
                sym, tcode = pair
                rows = parse_bars(_fetch_bars(tcode, period), sym)
                return pl.DataFrame(rows) if rows else pl.DataFrame()

            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for df in pool.map(one, cn_pairs):
                    if not df.is_empty():
                        frames.append(df)
            got = sum(f.height for f in frames)
            logger.info(
                "腾讯分钟K 拉取完成(%d symbols, %d 行, %.2fs)",
                len(cn_pairs),
                got,
                time.perf_counter() - t0,
            )
            if on_chunk_done:
                on_chunk_done(1 if not bj_symbols else total - 1, total)

        if bj_symbols:
            frames.extend(self._bj_fallback(bj_symbols, freq))
            if on_chunk_done:
                on_chunk_done(total, total)

        if not frames:
            return pl.DataFrame()
        df = pl.concat(frames, how="diagonal_relaxed")
        df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        keep = [c for c in _MINUTE_CANONICAL if c in df.columns]
        return df.select(keep).sort(["symbol", "datetime"])

    @staticmethod
    def _bj_fallback(bj_symbols: list[str], freq: str) -> list[pl.DataFrame]:
        """北交所分钟数据: 腾讯不支持, 回落到 stock-sdk(东财)。

        东财现状失效概率很高, 故失败只记 debug 不告警 —— 否则每次全市场同步
        都会被 347 条 warning 刷屏。拿不到就当无数据, 不影响沪深主链路。
        """
        try:
            from app.plugins.stocksdk.provider import StockSDKProvider

            provider = StockSDKProvider()
            try:
                df = provider.get_minute(bj_symbols, None, None, freq=freq)
            finally:
                provider.close()
        except Exception as e:
            logger.debug("北交所分钟回落失败: %s", e)
            return []
        if df.is_empty():
            logger.info("北交所分钟: %d 只标的无数据(腾讯不支持, 东财回落为空)", len(bj_symbols))
            return []
        logger.info("北交所分钟: 东财回落 %d 只 -> %d 行", len(bj_symbols), df.height)
        return [df]


def availability() -> tuple[bool, str]:
    """探活: 优先探 mkline(分钟), 失败再探 qt(实时快照)。不抛异常。

    两个端点不同 host(ifzq vs qt), 理论上可能单边故障。任一可用就注册插件,
    并在 status 里说明当前实际可用的数据集 —— 否则 qt 正常时也会被误判为不可用,
    用户将失去唯一的免费实时源。
    """
    rows = _fetch_bars("sh600519", "m1")
    if rows:
        last = rows[-1]
        if len(last) >= 6:
            return True, f"ok ({len(rows)} bars, latest={last[0]})"
    probe = _qt_fetch_batch(["sh600519"])
    if probe:
        row = probe[0]
        return True, (
            f"ok (分钟接口无数据, 仅实时快照可用: {row.get('symbol')} "
            f"close={row.get('last_price')})"
        )
    return False, "腾讯 mkline 与 qt 均无数据返回(可能被风控或网络不通)"
