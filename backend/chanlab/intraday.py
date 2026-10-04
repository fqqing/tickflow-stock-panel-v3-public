"""盘中「分时 + 五档盘口」-- 腾讯公开行情, 零鉴权。

为什么自己解析而不是复用 v1 的 provider
=====================================
v1 的 tencent provider 只解析了常规快照字段(开高低收/量额/换手),
**五档买卖盘它没解析**, 而 `chanlab/quote.py` 又是直接复用它的 —— 要在 v2
做盘口, 只能自己按 qt.gtimg.cn 的原始字段索引取。字段是摸出来的:

    python backend/scripts/probe_depth.py 600519.SH

    [9..18]   买一价,买一量 ... 买五价,买五量   (量单位: 手)
    [19..28]  卖一价,卖一量 ... 卖五价,卖五量
    [30]      行情时间 yyyymmddHHMMSS
    [43]      振幅%    [46][47] 涨停/跌停价   [51] 均价

⚠️ 硬约束: 腾讯对这两个接口**不带 Referer 会返空串而不是报错**,
   失败表现是「0 行」而不是异常, 极易误判成网络问题。头必须带 UA + Referer。

分时: web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600519
     data.<code>.data.data = ["0930 1239.53 161 19956433.00", ...]
     即 HHMM 价格 累计成交量(手) 累计成交额(元), 一天 267 根(含 09:30 与 15:00)

对外量纲(与其它端点一致)
========================
    价格/金额 -> 元   成交量 -> 手   涨跌幅/委比 -> 百分数   时间 -> epoch 毫秒
"""

from __future__ import annotations

import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

#: 只有最近一个交易日的分时: 盘中用不上历史, 分钟 K 归 free-stockdb / 通达信补
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
DEPTH_URL = "https://qt.gtimg.cn/q="

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

CN_TZ = timezone(timedelta(hours=8))

_F_TIME = 30
# ⚠️ 46 是市净率不是涨停价。踩过: 取成 [46]/[47] 时茅台的「涨停」显示成 6.26
#    (正好是它的 PB), 页面上看不出错, 因为它不像个明显不合理的值。
#    验算口径: 涨停 = 昨收 * 1.1 == 1235.58 * 1.1 == 1359.14 == [47]
_F_LIMIT_UP = 47
_F_LIMIT_DOWN = 48
_F_AVG = 51
_F_AMPLITUDE = 43

DEPTH_TTL = float(os.environ.get("TICKFLOW_DEPTH_TTL", "10"))
MINUTE_TTL = float(os.environ.get("TICKFLOW_MINUTE_TTL", "60"))

_depth_cache: dict[str, Any] = {"at": 0.0, "key": "", "row": {}}
_minute_cache: dict[str, Any] = {"at": 0.0, "key": "", "row": {}}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("gbk", errors="replace")


def to_tcode(symbol: str) -> str | None:
    """600519.SH -> sh600519. 腾讯用 sh/sz/bj 前缀 + 6 位数字。"""
    if "." not in symbol:
        return None
    num, mkt = symbol.split(".", 1)
    if len(num) != 6:
        return None
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "")
    return f"{prefix}{num}" if prefix else None


def _num(raw: str) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # 滤 NaN


def _flag_pct(bid_total: float, ask_total: float) -> float | None:
    """委比%: (委买 - 委卖) / (委买 + 委卖) * 100。两边都挂 0 时返回 None。"""
    total = bid_total + ask_total
    if total <= 0:
        return None
    return (bid_total - ask_total) / total * 100.0


def _parse_depth(code: str, body: str) -> dict[str, Any]:
    """解析 qt 快照行。body 形如 `v_sh600519="..."`。"""
    if "=" not in body:
        return {"ok": False, "detail": "快照返回空 (没带 Referer 会被腾讯返空串)"}
    payload = body.split("=", 1)[1].strip().rstrip(";")
    parts = payload.split('"')
    fields = (parts[1] if len(parts) > 1 else "").split("~")
    if len(fields) <= 30:
        return {"ok": False, "detail": f"字段不足: {len(fields)}"}

    def pair(base: int) -> dict[str, Any]:
        return {
            "price": _num(fields[base]),
            "volume": _num(fields[base + 1]),
        }

    bids = [pair(i) for i in range(9, 18, 2)]
    asks = [pair(i) for i in range(19, 28, 2)]
    bid_total = sum((b["volume"] or 0) for b in bids)
    ask_total = sum((a["volume"] or 0) for a in asks)

    raw_time = (fields[_F_TIME] or "").strip()
    fmt = "%Y%m%d%H%M%S"
    try:
        at = datetime.strptime(raw_time, fmt).replace(tzinfo=CN_TZ)
    except ValueError:
        at = None

    return {
        "ok": True,
        "detail": "",
        "symbol": fields[2] and _symbol_of(code, str(fields[2])),
        "name": fields[1],
        "at": at.isoformat() if at else None,
        "last_price": _num(fields[3]),
        "prev_close": _num(fields[4]),
        "open": _num(fields[5]),
        "high": _num(fields[33]),
        "low": _num(fields[34]),
        "avg_price": _num(fields[_F_AVG]) if len(fields) > _F_AVG else None,
        "amplitude": _num(fields[_F_AMPLITUDE]) if len(fields) > _F_AMPLITUDE else None,
        "volume": _num(fields[6]),
        "amount": (_num(fields[37]) or 0) * 10000.0,  # 万元 -> 元
        "limit_up": _num(fields[_F_LIMIT_UP]) if len(fields) > _F_LIMIT_UP else None,
        "limit_down": _num(fields[_F_LIMIT_DOWN]) if len(fields) > _F_LIMIT_DOWN else None,
        "outer": _num(fields[7]),
        "inner": _num(fields[8]),
        "bids": bids,
        "asks": asks,
        "bid_total": bid_total,
        "ask_total": ask_total,
        "bid_ask_ratio": _flag_pct(bid_total, ask_total),
        "source": "tencent/qt",
    }


def _symbol_of(code: str, raw_num: str) -> str:
    """sh600519 -> 600519.SH。腾讯返回的 [2] 是纯数字代码。"""
    num = raw_num or code[2:]
    mkt = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(code[:2].lower(), "")
    return f"{num}.{mkt}" if mkt else num


def depth(symbol: str, ttl: float | None = None) -> dict[str, Any]:
    """五档盘口 + 快照。任何失败都返回 ok=False, 不抛。"""
    limit = DEPTH_TTL if ttl is None else ttl
    fresh = (
        (time.perf_counter() - _depth_cache["at"]) < limit
        and _depth_cache["key"] == symbol
        and _depth_cache["row"]
    )
    if fresh:
        return _depth_cache["row"]

    code = to_tcode(symbol)
    if not code:
        return {"ok": False, "detail": f"无法识别的代码: {symbol}"}
    try:
        row = _parse_depth(code, _get(f"{DEPTH_URL}{code}"))
    except Exception as exc:  # 网络抖动不该让页面白屏
        row = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    if row.get("ok"):
        row["symbol"] = symbol
        _depth_cache.update({"at": time.perf_counter(), "key": symbol, "row": row})
    return row


def minute(symbol: str, ttl: float | None = None) -> dict[str, Any]:
    """当日分时。返回 [{'t': ms, 'price', 'volume', 'amount'}]。

    volume/amount 都是**当日累计值**(腾讯给的就是累计口径), 前端画分时量柱时
    要自己做差分 —— 别直接把累计数当每分钟的量画, 那是一条单调上升的斜线。
    """
    limit = MINUTE_TTL if ttl is None else ttl
    fresh = (
        (time.perf_counter() - _minute_cache["at"]) < limit
        and _minute_cache["key"] == symbol
        and _minute_cache["row"]
    )
    if fresh:
        return _minute_cache["row"]

    code = to_tcode(symbol)
    if not code:
        return {"ok": False, "detail": f"无法识别的代码: {symbol}"}
    try:
        js = _load_minute(code)
    except Exception as exc:
        js = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    if js.get("ok"):
        # _load_minute 里存的是腾讯代码(sh600519), 对外统一用 600519.SH
        js["symbol"] = symbol
        _minute_cache.update({"at": time.perf_counter(), "key": symbol, "row": js})
    return js


def _load_minute(code: str) -> dict[str, Any]:
    import json  # 局部 import: 这个函数只在走网络时调用

    text = _get(f"{MINUTE_URL}?code={code}")
    payload = json.loads(text)
    node = (payload.get("data") or {}).get(code) or {}
    data = node.get("data") or {}
    rows = data.get("data") if isinstance(data, dict) else data
    day = data.get("date") if isinstance(data, dict) else None
    if not rows:
        return {"ok": False, "detail": "分时返回空 (非交易日或代码不受支持)"}

    out = parse_minute_rows(day, rows)
    if not out:
        return {"ok": False, "detail": "分时解析出 0 行"}
    return {
        "ok": True,
        "detail": "",
        "symbol": code,
        "date": day,
        "rows": out,
        "source": "tencent/minute",
    }


def parse_minute_rows(day: str | None, rows: list[Any]) -> list[dict[str, Any]]:
    """`["0930 1239.53 161 19956433.00", ...]` -> 结构化。抽出来是为了能无网络单测。

    腾讯给的是**当日累计**成交量/额, 这里原样保留 —— 差分是前端画量柱时的事,
    在后端差分会让「取最后一根」这类调用拿不到当日总量。
    """
    out: list[dict[str, Any]] = []
    for raw in rows:
        parts = str(raw).split()
        if len(parts) < 3:
            continue
        hhmm, price, vol = parts[0], parts[1], parts[2]
        stamp = _epoch_ms(day, hhmm)
        if stamp is None:
            continue
        out.append(
            {
                "t": stamp,
                "price": _num(price),
                "volume": _num(vol),
                "amount": _num(parts[3]) if len(parts) > 3 else None,
            }
        )
    return out


def _epoch_ms(day: str | None, hhmm: str) -> int | None:
    """20260930 + 0930 -> epoch 毫秒(UTC)。缺任一都返回 None 让调用方跳过。"""
    if not day or len(hhmm) != 4 or len(day) != 8:
        return None
    try:
        dt = datetime(
            int(day[:4]),
            int(day[4:6]),
            int(day[6:8]),
            int(hhmm[:2]),
            int(hhmm[2:]),
            tzinfo=CN_TZ,
        )
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def invalidate() -> None:
    """清缓存。给测试和「手动刷新」用。"""
    _depth_cache["at"] = 0.0
    _minute_cache["at"] = 0.0
