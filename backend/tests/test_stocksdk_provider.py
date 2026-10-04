"""StockSDKProvider 归一化与桥接契约测试。

不依赖真实 node / 网络: mock bridge.run_job 返回样例 payload, 只验证 Python 侧的
归一化、除权因子合成对齐、符号回显、空结果处理与注册接线。
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess

import polars as pl

from app.plugins.stocksdk import bridge
from app.plugins.stocksdk import provider as sp
from app.plugins.stocksdk.provider import StockSDKProvider


def _patch_run_job(monkeypatch, mapping):
    """mapping: op -> payload dict(将作为 run_job 返回值)。"""

    def fake(job, timeout=None):
        return mapping[job["op"]]

    monkeypatch.setattr(sp.bridge, "run_job", fake)


def test_get_daily_normalizes_and_echoes_symbol(monkeypatch):
    _patch_run_job(monkeypatch, {
        "daily": {"ok": True, "op": "daily", "rows": {
            "600519.SH": [
                {"date": "2026-01-05", "open": 1385.0, "high": 1431.9, "low": 1385.0,
                 "close": 1426.0, "volume": 70949, "amount": 1.0e10, "code": "600519"},
                {"date": "2026-01-06", "open": 1432.5, "high": 1437.0, "low": 1416.5,
                 "close": 1428.0, "volume": 39586, "amount": 5.6e9, "code": "600519"},
            ],
        }},
    })
    df = StockSDKProvider().get_daily(["600519.SH"], dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 15))
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.height == 2
    assert df["symbol"].unique().to_list() == ["600519.SH"]
    assert df.schema["date"] == pl.Date
    assert df.schema["close"] == pl.Float64


def test_get_adj_factors_from_bridge_ratio(monkeypatch):
    # 桥接内部已算好 ex_factor = close_hfq/close_none, 这里验证 Python 侧归一化。
    _patch_run_job(monkeypatch, {
        "adj": {"ok": True, "op": "adj", "rows": {
            "600519.SH": [
                {"symbol": "600519.SH", "trade_date": "2020-01-02", "ex_factor": 5.29},
                {"symbol": "600519.SH", "trade_date": "2020-01-03", "ex_factor": 5.30},
            ],
        }},
    })
    df = StockSDKProvider().get_adj_factors(["600519.SH"], None, None)
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 2
    assert df.schema["trade_date"] == pl.Date
    assert abs(df["ex_factor"][0] - 5.29) < 1e-9


def test_get_minute_datetime_is_beijing_wall_clock(monkeypatch):
    # timestamp 1779327300000 = 2026-05-21 01:35 UTC = 09:35 Asia/Shanghai
    _patch_run_job(monkeypatch, {
        "minute": {"ok": True, "op": "minute", "rows": {
            "600519.SH": [
                {"date": "2026-05-21 09:35", "open": 1284.9, "high": 1289.1, "low": 1283.9,
                 "close": 1286.7, "volume": 2740, "amount": 3.6e8, "timestamp": 1779327300000},
            ],
        }},
    })
    df = StockSDKProvider().get_minute(["600519.SH"], None, None)
    assert set(df.columns) == {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
    assert df.height == 1
    ts = df["datetime"][0]
    assert (ts.hour, ts.minute) == (9, 35)
    assert df["symbol"][0] == "600519.SH"


def test_get_realtime_normalizes_units(monkeypatch):
    """快照量纲归一到内部 schema: 涨幅小数化、成交额万元→元、科创板成交量股→手。"""
    rows = [
        {"symbol": "600519.SH", "name": "贵州茅台", "last_price": 1200.0, "prev_close": 1194.0,
         "open": 1186.0, "high": 1203.0, "low": 1180.0, "volume": 16325,
         "amount": 195900.0, "change_pct": 0.5},
        {"symbol": "688029.SH", "name": "南微医学", "last_price": 81.81, "prev_close": 79.06,
         "open": 78.7, "high": 82.35, "low": 78.7, "volume": 1742054,
         "amount": 14077.0, "change_pct": 3.48},
    ]
    _patch_run_job(monkeypatch, {"realtime": {"ok": True, "op": "realtime", "rows": rows}})
    out = StockSDKProvider().get_realtime()
    required = {"symbol", "last_price", "prev_close", "open", "high", "low", "volume"}
    assert required <= set(out[0].keys())

    main, star = out[0], out[1]
    # 涨幅: 百分数 → 小数
    assert abs(main["change_pct"] - 0.005) < 1e-12
    assert abs(star["change_pct"] - 0.0348) < 1e-12
    # 成交额: 万元 → 元
    assert abs(main["amount"] - 1.959e9) < 1e-3
    assert abs(star["amount"] - 1.4077e8) < 1e-3
    # 成交量: 主板已是「手」不动; 科创板「股」→「手」
    assert main["volume"] == 16325
    assert abs(star["volume"] - 17420.54) < 1e-9
    # 原始入参不被就地修改
    assert rows[1]["volume"] == 1742054


def test_normalize_realtime_rows_tolerates_missing_and_bad_fields():
    rows = [
        {"symbol": "300001.SZ", "volume": None, "amount": None, "change_pct": None},
        {"symbol": "688001.SH", "volume": "abc", "amount": "x", "change_pct": "y"},
        {"symbol": "600000.SH", "change_pct": -1.53},
    ]
    out = sp._normalize_realtime_rows(rows)
    assert out[0]["volume"] is None and out[0]["amount"] is None and out[0]["change_pct"] is None
    assert out[1]["volume"] is None and out[1]["amount"] is None and out[1]["change_pct"] is None
    assert abs(out[2]["change_pct"] + 0.0153) < 1e-12
    assert "volume" not in out[2]


def test_get_index_realtime_uses_index_op_and_normalizes_units(monkeypatch):
    """指数快照走 indexQuotes op, 量纲与股票快照同一套归一口径。"""
    rows = [
        {"symbol": "000001.SH", "name": "上证指数", "last_price": 3853.07, "prev_close": 3864.28,
         "open": 3861.75, "high": 3863.15, "low": 3842.72, "volume": 188399620,
         "amount": 33128265.0, "change_pct": -0.29},
        {"symbol": "399006.SZ", "name": "创业板指", "last_price": 3247.69, "prev_close": 3247.92,
         "open": 3246.89, "high": 3258.16, "low": 3227.18, "volume": 55184239,
         "amount": 16402651.0, "change_pct": 1.41},
    ]
    seen: list[dict] = []

    def fake(job, timeout=None):
        seen.append(job)
        return {"ok": True, "op": "indexQuotes", "rows": rows}

    monkeypatch.setattr(sp.bridge, "run_job", fake)
    p = StockSDKProvider()
    assert p.supports_index_realtime is True
    out = p.get_index_realtime(["000001.SH", "399006.SZ"])

    assert seen[0]["op"] == "indexQuotes"
    assert seen[0]["symbols"] == ["000001.SH", "399006.SZ"]
    assert [r["symbol"] for r in out] == ["000001.SH", "399006.SZ"]
    # 涨幅: 百分数 → 小数
    assert abs(out[0]["change_pct"] + 0.0029) < 1e-12
    assert abs(out[1]["change_pct"] - 0.0141) < 1e-12
    # 成交额: 万元 → 元
    assert abs(out[0]["amount"] - 3.3128265e11) < 1e-3
    # 成交量: 指数不套用科创板「股→手」规则 (000688.SH 这类指数代码以 000 开头)
    assert out[0]["volume"] == 188399620
    star_like = sp._normalize_realtime_rows(
        [{"symbol": "000688.SH", "volume": 1000, "amount": 1.0, "change_pct": 0.0}]
    )[0]
    assert star_like["volume"] == 1000


def test_get_index_realtime_empty_symbols_skips_bridge(monkeypatch):
    def boom(job, timeout=None):
        raise AssertionError("空符号列表不应触发桥接调用")

    monkeypatch.setattr(sp.bridge, "run_job", boom)
    assert StockSDKProvider().get_index_realtime([]) == []
    assert StockSDKProvider().get_index_realtime(None) == []


def test_get_index_realtime_degrades_on_bridge_error(monkeypatch):
    def boom(job, timeout=None):
        raise sp.bridge.StockSDKBridgeError("node missing")

    monkeypatch.setattr(sp.bridge, "run_job", boom)
    assert StockSDKProvider().get_index_realtime(["000001.SH"]) == []


def test_bridge_index_quotes_prefixes_exchange(tmp_path):
    """指数代码必须带 sh/sz 前缀: 裸 000001 会被上游当成平安银行。"""
    if shutil.which("node") is None:
        raise AssertionError("node is required for stock-sdk bridge path regression test")

    bridge_path = tmp_path / "bridge.mjs"
    shutil.copyfile(bridge._BRIDGE_MJS, bridge_path)

    pkg_dir = tmp_path / "node_modules" / "stock-sdk"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": "stock-sdk", "type": "module", "main": "index.js"}),
        encoding="utf-8",
    )
    # 假 SDK: quotes.cn 原样回显收到的代码, 便于断言桥接是否加了前缀。
    (pkg_dir / "index.js").write_text(
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get quotes() {\n"
        "    return { cn: async (codes) => codes.map((c) => ({\n"
        "      code: c.slice(2), marketId: c.startsWith('sh') ? '1' : '51',\n"
        "      name: 'echo:' + c, price: 1, prevClose: 1,\n"
        "    })) }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({"op": "indexQuotes", "symbols": ["000001.SH", "399001.SZ"]}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert [r["symbol"] for r in result["rows"]] == ["000001.SH", "399001.SZ"]
    assert [r["name"] for r in result["rows"]] == ["echo:sh000001", "echo:sz399001"]


def test_bridge_minute_prefixes_exchange_code(tmp_path):
    """分钟K必须带 sh/sz/bj 前缀 —— 与指数同理, 但成因不同。

    回归用例: 曾经 opMinute 直接把 app 符号(600519.SH)或裸代码(600519)喂给
    `kline.cnMinute`, 而上游**不会**像 `kline.cn` 那样自动补交易所前缀, 于是
    一律返回空数组 —— 现象是「日K正常、分时图只有坐标轴没有线」, 且全程无报错
    (provider 把空结果当正常返回, /api/kline/minute 给出 source="none")。
    """
    if shutil.which("node") is None:
        raise AssertionError("node is required for stock-sdk bridge path regression test")

    bridge_path = tmp_path / "bridge.mjs"
    shutil.copyfile(bridge._BRIDGE_MJS, bridge_path)

    pkg_dir = tmp_path / "node_modules" / "stock-sdk"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": "stock-sdk", "type": "module", "main": "index.js"}),
        encoding="utf-8",
    )
    # 假 SDK: cnMinute 把收到的代码塞进 echo 字段回显, 便于断言桥接加了前缀。
    (pkg_dir / "index.js").write_text(
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get kline() {\n"
        "    return { cnMinute: async (code) => [{\n"
        "      time: '2026-09-16 09:30', timestamp: 1789522200000,\n"
        "      open: 1, close: 1, high: 1, low: 1, volume: 1, amount: 1,\n"
        "      echo: code,\n"
        "    }] }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({
            "op": "minute",
            "symbols": ["600519.SH", "000001", "sz300750"],
            "period": "1",
            "start": "20260916",
            "end": "20260916",
        }),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    rows = result["rows"]
    # key 必须保持原始 app 符号, 否则 Python 侧落盘的 symbol 列会被上游代码污染。
    assert list(rows) == ["600519.SH", "000001", "sz300750"]
    # 送给上游的代码必须带交易所前缀 (裸代码按号段猜, 已带前缀的原样透传)。
    assert rows["600519.SH"][0]["echo"] == "sh600519"
    assert rows["000001"][0]["echo"] == "sz000001"
    assert rows["sz300750"][0]["echo"] == "sz300750"


def test_get_instruments_flatten_compatible(monkeypatch):
    rows = [{"symbol": "600519.SH", "name": "贵州茅台", "code": "600519", "exchange": "SH",
             "region": "CN", "type": "stock", "total_shares": 1, "float_shares": 1,
             "limit_up": 1.0, "limit_down": 1.0}]
    _patch_run_job(monkeypatch, {"instruments": {"ok": True, "op": "instruments", "rows": rows}})
    out = StockSDKProvider().get_instruments("stock")
    assert out[0]["symbol"] == "600519.SH"
    assert out[0]["exchange"] == "SH"
    # 非 stock 资产暂不覆盖
    assert StockSDKProvider().get_instruments("etf") == []


def test_empty_symbols_returns_empty():
    p = StockSDKProvider()
    assert p.get_daily([], None, None).is_empty()
    assert p.get_adj_factors([], None, None).is_empty()
    assert p.get_minute([], None, None).is_empty()


def test_bridge_error_degrades_to_empty(monkeypatch):
    def boom(job, timeout=None):
        raise sp.bridge.StockSDKBridgeError("node missing")

    monkeypatch.setattr(sp.bridge, "run_job", boom)
    assert StockSDKProvider().get_daily(["600519.SH"], None, None).is_empty()
    assert StockSDKProvider().get_realtime() == []
    assert StockSDKProvider().get_instruments("stock") == []


def test_bridge_uses_utf8_error_tolerant_subprocess(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"ok": True, "op": "ping"})
        stderr = ""

    monkeypatch.setattr(bridge, "_node_bin", lambda: "node")

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert bridge.run_job({"op": "ping"})["ok"] is True
    kwargs = calls[0][1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_bridge_mjs_resolves_local_stock_sdk_on_windows_path(tmp_path):
    if shutil.which("node") is None:
        raise AssertionError("node is required for stock-sdk bridge path regression test")

    bridge_path = tmp_path / "bridge.mjs"
    shutil.copyfile(bridge._BRIDGE_MJS, bridge_path)

    pkg_dir = tmp_path / "node_modules" / "stock-sdk"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": "stock-sdk", "type": "module", "main": "index.js"}),
        encoding="utf-8",
    )
    (pkg_dir / "index.js").write_text(
        "export class StockSDK { static version = 'fake-local' }\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({"op": "ping"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result == {"ok": True, "op": "ping", "version": "fake-local"}


def test_plugin_discovered_in_loader():
    """插件被发现并记录状态 (即使依赖没装, 不可用)。"""
    from app.data_providers import custom as cs

    plugins = {p["name"]: p for p in cs.list_plugins()}
    assert "stocksdk" in plugins
    assert plugins["stocksdk"]["runtime"] == "node"
    assert "daily" in plugins["stocksdk"]["datasets"]
    assert "realtime" in plugins["stocksdk"]["datasets"]
    assert "financial" not in plugins["stocksdk"]["datasets"]
    assert cs.is_builtin("stocksdk")
    # 内置源不出现在用户自定义源列表
    assert "stocksdk" not in [s["name"] for s in cs.list_sources()]


def test_plugin_registered_when_available(monkeypatch):
    """依赖可用时, 插件注册进 _PROVIDERS 并可路由。"""
    from app.data_providers import custom as cs
    from app.data_providers.custom import loader

    # mock availability 返回 (True, "ok")
    monkeypatch.setattr(loader, "_call_check", lambda ref: (True, "ok"))
    monkeypatch.setattr(loader, "_load_entry", _load_stocksdk_entry)
    loader._load_builtin_plugins()

    assert "stocksdk" in cs.names()
    assert cs.is_custom_provider("stocksdk")
    assert cs.provider_has_dataset("stocksdk", "daily")
    assert cs.provider_has_dataset("stocksdk", "realtime")
    assert not cs.provider_has_dataset("stocksdk", "financial")


def _load_stocksdk_entry(entry_ref: str):
    """测试用: 无条件加载 stocksdk provider 类 (跳过 check)。"""
    if "StockSDKProvider" in entry_ref:
        from app.plugins.stocksdk.provider import StockSDKProvider
        return StockSDKProvider
    if "availability" in entry_ref:
        from app.plugins.stocksdk.bridge import availability
        return availability
    raise ValueError(f"unknown entry: {entry_ref}")


def test_builtin_not_editable():
    from app.data_providers import custom as cs

    assert cs.get_config_dict("stocksdk") is None
    for fn in (lambda: cs.save_config("stocksdk", {}), lambda: cs.delete_config("stocksdk")):
        try:
            fn()
            raise AssertionError("expected ValueError for builtin")
        except ValueError:
            pass


def _write_fake_sdk(tmp_path, index_js: str):
    """在 tmp_path 下铺一个最小 node_modules/stock-sdk, 并拷一份 bridge.mjs。"""
    if shutil.which("node") is None:
        raise AssertionError("node is required for stock-sdk bridge path regression test")
    bridge_path = tmp_path / "bridge.mjs"
    shutil.copyfile(bridge._BRIDGE_MJS, bridge_path)
    pkg_dir = tmp_path / "node_modules" / "stock-sdk"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": "stock-sdk", "type": "module", "main": "index.js"}),
        encoding="utf-8",
    )
    (pkg_dir / "index.js").write_text(index_js, encoding="utf-8")
    return bridge_path


def test_bridge_minute_thrown_error_keeps_key_and_reports(tmp_path):
    """回归: 上游抛错时 opMinute 曾经完全静默。

    `out[sym]` 的赋值在 await 之后, 一旦 `cnMinute` 抛异常(实测上游会抛
    `SdkError: fetch failed`), mapPool 会把异常吞进 results[i], 于是 out 里
    永远没有这个 key —— 输出 `rows:{}`, Python 侧拿到空 df 报 `source="none"`,
    与"这只票确实没有分钟数据"**完全无法区分**, 分时线消失因此长期查不出原因。

    现在要求: key 必须存在(空数组), 且 errors 里如实记下该 symbol。
    """
    bridge_path = _write_fake_sdk(
        tmp_path,
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get kline() {\n"
        "    return { cnMinute: async () => { throw new Error('fetch failed') } }\n"
        "  }\n"
        "}\n",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({
            "op": "minute",
            "symbols": ["600519.SH"],
            "period": "1",
            "start": "20260916",
            "end": "20260916",
        }),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    # 关键: key 必须存在, 不能因为抛错就消失。
    assert list(result["rows"]) == ["600519.SH"]
    assert result["rows"]["600519.SH"] == []
    # 并且失败原因要能被上游看到, 而不是静默成"没数据"。
    assert "fetch failed" in result["errors"]["600519.SH"]


def test_bridge_fetch_retry_retries_thrown_errors(tmp_path):
    """回归: fetchWithRetry 曾经只重试"空数组", 不重试抛出的异常。

    上游抖动有两种面孔 —— 空返回, 或 `fetch failed`(连接层失败)。只重试前者时,
    一次连接抖动就直接判定为无数据。这里造假 SDK: 前两次抛错、第三次返回数据,
    要求最终仍拿到数据(证明异常路径也走了退避重试)。
    """
    bridge_path = _write_fake_sdk(
        tmp_path,
        "let n = 0\n"
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get kline() {\n"
        "    return {\n"
        "      cnMinute: async () => {\n"
        "        n += 1\n"
        "        if (n < 3) throw new Error('fetch failed #' + n)\n"
        "        return [{ time: '2026-09-16 09:30', timestamp: 1789522200000,\n"
        "                  open: 1, close: 1, high: 1, low: 1, volume: 1, amount: 1 }]\n"
        "      },\n"
        "    }\n"
        "  }\n"
        "}\n",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({
            "op": "minute",
            "symbols": ["600519.SH"],
            "period": "1",
            "start": "20260916",
            "end": "20260916",
        }),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert len(result["rows"]["600519.SH"]) == 1
    # 重试成功 → 不应残留错误记录。
    assert "errors" not in result


def test_bridge_industries_flattens_board_members(tmp_path):
    """industries op: 反推「个股 -> 行业板块」长表, 成分股补交易所前缀。"""
    bridge_path = _write_fake_sdk(
        tmp_path,
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get board() {\n"
        "    return { industry: {\n"
        "      list: async () => [\n"
        "        { rank: 1, name: '半导体', code: 'BK1325', changePercent: 6.2 },\n"
        "        { rank: 2, name: '运动服装', code: 'BK1355', changePercent: 7.0 },\n"
        "      ],\n"
        "      constituents: async (code) => code === 'BK1325'\n"
        "        ? [{ rank: 1, code: '688432', name: '有研硅', price: 54.06, changePercent: 20 },\n"
        "           { rank: 2, code: '600519', name: '贵州茅台', price: 1260, changePercent: 1 }]\n"
        "        : [{ rank: 1, code: '300005', name: '探路者', price: 11.2, changePercent: 10 }],\n"
        "    } }\n"
        "  }\n"
        "}\n",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({"op": "industries", "concurrency": 2}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    rows = result["rows"]
    assert len(rows) == 3
    # 板块完整率必须回报: 调用方全靠它判断这份快照敢不敢覆盖旧表。
    assert result["meta"] == {"boards_total": 2, "boards_requested": 2, "boards_ok": 2}
    assert "errors" not in result

    by_symbol = {r["symbol"]: r for r in rows}
    # 成分股只有 6 位数字, 必须按号段补交易所前缀。
    assert set(by_symbol) == {"688432.SH", "600519.SH", "300005.SZ"}
    assert by_symbol["688432.SH"]["board_code"] == "BK1325"
    assert by_symbol["688432.SH"]["board_name"] == "半导体"
    assert by_symbol["688432.SH"]["code"] == "688432"
    # 板块自身涨幅随成分股一并带出, 便于消费方判断板块强弱。
    assert by_symbol["688432.SH"]["board_change_pct"] == 6.2
    assert by_symbol["300005.SZ"]["board_code"] == "BK1355"


def test_bridge_industries_partial_board_failure_reports_errors(tmp_path):
    """单个板块抓失败不致命: 其余板块照常返回, 失败的板块代码记入 errors。"""
    bridge_path = _write_fake_sdk(
        tmp_path,
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get board() {\n"
        "    return { industry: {\n"
        "      list: async () => [\n"
        "        { rank: 1, name: '半导体', code: 'BK1325', changePercent: 6.2 },\n"
        "        { rank: 2, name: '坏板块', code: 'BK9999', changePercent: 0 },\n"
        "      ],\n"
        "      constituents: async (code) => code === 'BK9999'\n"
        "        ? Promise.reject(new Error('fetch failed'))\n"
        "        : [{ rank: 1, code: '688432', name: '有研硅', price: 54.06, changePercent: 20 }],\n"
        "    } }\n"
        "  }\n"
        "}\n",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({"op": "industries", "concurrency": 2}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert [r["symbol"] for r in result["rows"]] == ["688432.SH"]
    assert "BK9999" in result["errors"]
    # 失败板块不计入 ok, 完整率因此可被下游识别为 1/2。
    assert result["meta"]["boards_ok"] == 1
    assert result["meta"]["boards_requested"] == 2


def test_industries_op_selected_by_boards_filter(tmp_path):
    """boards 过滤: 只抓指定板块, 减少无谓请求。"""
    bridge_path = _write_fake_sdk(
        tmp_path,
        "export class StockSDK {\n"
        "  static version = 'fake-local'\n"
        "  get board() {\n"
        "    return { industry: {\n"
        "      list: async () => [\n"
        "        { rank: 1, name: '半导体', code: 'BK1325', changePercent: 6.2 },\n"
        "        { rank: 2, name: '运动服装', code: 'BK1355', changePercent: 7.0 },\n"
        "      ],\n"
        "      constituents: async () =>\n"
        "        [{ rank: 1, code: '688432', name: '有研硅', price: 1, changePercent: 1 }],\n"
        "    } }\n"
        "  }\n"
        "}\n",
    )

    proc = subprocess.run(
        ["node", str(bridge_path)],
        input=json.dumps({"op": "industries", "boards": ["BK1355"], "concurrency": 2}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert [r["board_code"] for r in result["rows"]] == ["BK1355"]


def test_get_industry_members_passthrough(monkeypatch):
    rows = [{"board_code": "BK1325", "board_name": "半导体", "symbol": "688432.SH",
             "code": "688432", "name": "有研硅", "board_change_pct": 6.2,
             "price": 54.06, "change_pct": 20.0, "turnover_rate": 1.0, "pe": 1.0, "pb": 1.0}]
    _patch_run_job(monkeypatch, {"industries": {
        "ok": True, "op": "industries", "rows": rows,
        "meta": {"boards_total": 496, "boards_requested": 496, "boards_ok": 496},
    }})
    out = StockSDKProvider().fetch_industry_members()
    assert out["rows"] == rows
    # meta 必须原样透出: 调用方靠它判断板块完整率, 决定敢不敢覆盖旧表。
    assert out["meta"]["boards_ok"] == 496
    assert out["errors"] == {}


def test_get_industry_members_partial_failure_still_returns_rows(monkeypatch):
    """部分板块失败: 有效行照常返回 + meta/errors 一并透出, 由调用方决定是否采用。"""
    rows = [{"board_code": "BK1325", "symbol": "688432.SH", "code": "688432"}]
    _patch_run_job(monkeypatch, {"industries": {
        "ok": True, "op": "industries", "rows": rows,
        "meta": {"boards_total": 496, "boards_requested": 496, "boards_ok": 100},
        "errors": {"BK9999": "fetch failed"},
    }})
    out = StockSDKProvider().fetch_industry_members()
    assert out["rows"] == rows
    assert out["meta"]["boards_ok"] == 100
    assert "BK9999" in out["errors"]


def test_get_industry_members_degrades_on_bridge_error(monkeypatch):
    def boom(job, timeout=None):
        raise bridge.StockSDKBridgeError("node missing")

    monkeypatch.setattr(sp.bridge, "run_job", boom)
    out = StockSDKProvider().fetch_industry_members()
    assert out["rows"] == []
    # 桥接整体失败要能分辨: 区别于"上游返回空"。
    assert "node missing" in out["errors"]["__bridge__"]
