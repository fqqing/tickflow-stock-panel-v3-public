"""/api/kline 的条件请求 (ETag/304) 与响应压缩 (gzip)。

背景: 切周期/切票/分钟轮询会反复打同一个 URL, 而响应体动辄 0.4~1.3MB
(1000 天日K实测 1.32MB, 5m/120 交易日实测约 1MB)。ETag 让重复请求退化成
304 + 空 body, gzip 让首次请求降到百 KB 量级。两者都只动后端, 前端是
fetch + react-query, 浏览器自动带 If-None-Match 并自动解 gzip, 业务代码零改动。

这里用最小 FastAPI app 挂真实 router 走 HTTP 层, 而不是直调端点函数:
304 与 Content-Encoding 都是 HTTP 语义, 直调测不到。
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import StreamingResponse

from app.api import kline as kline_api

_URL = "/api/kline/daily?symbol=600000.SH&days=30"


def _daily_df(n: int = 60, drift: float = 0.0) -> pl.DataFrame:
    # 末根对齐今天: get_daily 默认区间是 [today - days, today], 固定日期会整体落空
    end = date.today()
    return pl.DataFrame(
        {
            "symbol": ["600000.SH"] * n,
            "date": [end - timedelta(days=n - 1 - i) for i in range(n)],
            "open": [10.0 + i * 0.01 for i in range(n)],
            "high": [11.0 + i * 0.01 for i in range(n)],
            "low": [9.0 + i * 0.01 for i in range(n)],
            "close": [10.0 + (i % 7) * 0.1 + drift for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        }
    )


def _repo(rows: pl.DataFrame | None = None) -> MagicMock:
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "stock"
    repo.get_instruments.return_value = pl.DataFrame(
        {"symbol": [], "name": [], "total_shares": [], "float_shares": []}
    )
    repo.get_daily_asset.return_value = rows if rows is not None else pl.DataFrame()
    return repo


def _client(rows: pl.DataFrame | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = _repo(rows if rows is not None else _daily_df())
    return TestClient(app)


# ===== ETag / 304 =====


def test_daily_response_carries_etag_and_no_cache():
    r = _client().get(_URL)
    assert r.status_code == 200
    assert r.headers["etag"].startswith('W/"')
    assert r.headers["cache-control"] == "private, no-cache"


def test_unchanged_data_is_304_with_empty_body():
    c = _client()
    first = c.get(_URL)
    etag = first.headers["etag"]

    second = c.get(_URL, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


def test_etag_changes_when_last_row_moves():
    """末根价格变了必须回 200, 否则盘中实时蜡烛会被 304 冻住。"""
    c = _client()
    etag = c.get(_URL).headers["etag"]

    c.app.state.repo.get_daily_asset.return_value = _daily_df(drift=3.0)
    again = c.get(_URL, headers={"If-None-Match": etag})
    assert again.status_code == 200
    assert again.headers["etag"] != etag


def test_etag_catches_mid_history_rewrite():
    """只改中间一行: 行数/末日/末收都不变, 靠行摘要才能抓到。

    这正是「末日 + 行数」式指纹会漏掉的场景 (历史数据被重算/修正)。
    """
    c = _client()
    etag = c.get(_URL).headers["etag"]

    df = _daily_df()
    rewritten = df.with_columns(
        pl.when(pl.arange(0, df.height) == 5)
        .then(pl.col("close") + 1.0)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    c.app.state.repo.get_daily_asset.return_value = rewritten
    again = c.get(_URL, headers={"If-None-Match": etag})
    assert again.status_code == 200
    assert again.headers["etag"] != etag


def test_minute_k_also_supports_conditional_get():
    """分钟K是轮询场景 (KLinePro 对分钟周期开了 refetchInterval), 更要能 304。"""
    c = _client()
    r = c.get("/api/kline/minute-k?symbol=600000.SH&period=5m&days=5")
    assert r.status_code == 200
    assert r.headers.get("etag")
    second = c.get(
        "/api/kline/minute-k?symbol=600000.SH&period=5m&days=5",
        headers={"If-None-Match": r.headers["etag"]},
    )
    assert second.status_code == 304


# ===== gzip =====


def _asgi_get(app, path: str, accept_encoding: str = "gzip"):
    """直接按 ASGI 协议调用, 拿未经处理的原始字节。

    httpx 会自动解码 content-encoding, 而我们要断言的正是压缩后的字节与
    分块行为, 所以这里自己实现 receive/send, 不走 httpx。
    返回 (start 消息, 完整 body, 非空 chunk 列表)。
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"accept-encoding", accept_encoding.encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    messages: list[dict] = []
    seen = {"n": 0}

    async def receive():
        seen["n"] += 1
        if seen["n"] == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    async def main():
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(main())
    start = next(m for m in messages if m["type"] == "http.response.start")
    chunks = [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]
    return start, b"".join(chunks), [c for c in chunks if c]


def _big_json_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)

    @app.get("/big")
    def big():
        return {"rows": [{"date": "2026-01-01", "close": i * 1.5} for i in range(400)]}

    @app.get("/small")
    def small():
        return {"ok": True}

    return app


def test_large_json_is_gzipped():
    start, body, _ = _asgi_get(_big_json_app(), "/big")
    headers = dict(start["headers"])
    assert headers[b"content-encoding"] == b"gzip"
    assert body[:2] == b"\x1f\x8b"  # gzip magic
    decompressed = gzip.decompress(body)
    assert len(body) < len(decompressed)  # 确实变小了
    assert len(json.loads(decompressed)["rows"]) == 400


def test_small_json_is_not_gzipped():
    start, body, _ = _asgi_get(_big_json_app(), "/small")
    headers = dict(start["headers"])
    assert b"content-encoding" not in headers
    assert body == b'{"ok":true}'


def test_event_stream_is_not_buffered_by_gzip():
    """SSE 必须逐块发出, 不能攒到流结束。

    回测/优化的进度流依赖这一点: 一旦被压缩缓冲, 进度会全部压到最后一次性
    到达, 前端看起来就是「一直 0% 然后瞬间 100%」。starlette 1.0 的
    GZipMiddleware 默认排除 text/event-stream, 这里把这个前提锁住。
    """
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)

    @app.get("/sse")
    async def sse():
        async def gen():
            for _ in range(3):
                yield b"event: progress\ndata: " + b"x" * 1500 + b"\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    start, body, chunks = _asgi_get(app, "/sse")
    headers = dict(start["headers"])
    assert b"content-encoding" not in headers
    assert len(chunks) >= 2, "SSE 被攒成一块了, 进度会卡到结束才出现"
    assert body.startswith(b"event: progress")
