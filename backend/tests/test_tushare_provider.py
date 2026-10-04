"""tushare 插件的纯逻辑单测 (不依赖网络)。

网络层统一用 monkeypatch 替换 ``provider._call``, 因此这里验证的是
「字段映射 / 量纲换算 / 累积因子稀疏化 / 报告去重 / 批量大小推导」这些真正的风险点。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.data_providers import custom as cs
from app.market_time import cn_today
from app.plugins.tushare import provider as tp

# ---------------------------------------------------------------- 注册与声明


def test_plugin_is_registered_and_declares_expected_datasets():
    plugins = {p["name"]: p for p in cs.list_plugins()}
    assert "tushare" in plugins, "插件未被 loader 发现"
    declared = set(plugins["tushare"]["datasets"])
    assert declared == {"instruments", "daily", "adj_factor", "financial"}


def test_provider_has_dataset_matches_declaration():
    if not cs.provider_has_dataset("tushare", "daily"):
        pytest.skip("tushare 插件当前不可用 (未配置 TUSHARE_TOKEN)")
    for ds in ("instruments", "daily", "adj_factor", "financial"):
        assert cs.provider_has_dataset("tushare", ds)
    assert not cs.provider_has_dataset("tushare", "minute")


def test_availability_reports_missing_token(monkeypatch):
    monkeypatch.setattr(tp, "_token", lambda: None)
    ok, reason = tp.availability()
    assert ok is False
    assert tp._TOKEN_ENV in reason


def test_availability_ok_with_token(monkeypatch):
    monkeypatch.setattr(tp, "_token", lambda: "x")
    assert tp.availability() == (True, "ok")


# ---------------------------------------------------------------- 批量大小


@pytest.mark.parametrize(
    ("span_days", "expected"),
    [
        (1, tp._BATCH_MAX),        # 短窗口 -> 顶到上限
        (30, tp._BATCH_MAX),
        (108, tp._BATCH_MAX),      # 5400 // 108 = 50
        (366, 14),                 # 5400 // 366 = 14
        (1825, 2),                 # 5 年 (extend_history 常见档位)
        (5000, 1),                 # 预算不足 -> 退化为单标的
    ],
)
def test_batch_size_respects_row_budget(span_days, expected):
    assert tp._batch_size(span_days) == expected


def test_batch_size_never_exceeds_row_budget():
    """任一现实窗口下, 一批的估算行数都不能突破 6000 行硬上限。

    估算刻意用**自然日**当行数 (真实交易日约 0.68), 所以这个断言比实际约束更严。
    """
    for span in (1, 7, 30, 120, 366, 1000, 1825, 3650, 5400):
        size = tp._batch_size(span)
        assert 1 <= size <= tp._BATCH_MAX
        assert size * span <= tp._MAX_ROWS_PER_CALL


def test_batch_size_floor_yields_to_row_budget():
    """``_BATCH_MIN`` 只是「预算够时别把批量压太碎」的门槛, 预算不足时必须让步。

    让步是刻意的: 一处超 6000 行会被 Tushare **静默截断**, 比多发几次请求严重得多。
    """
    assert tp._batch_size(1000) == tp._BATCH_MIN   # 5400 // 1000 = 5, 刚好触到门槛
    assert tp._batch_size(2000) == 2               # 预算 2 < 门槛 -> 按预算取 2
    assert tp._batch_size(100_000) == 1            # 退化为单标的, 但绝不为 0


# ---------------------------------------------------------------- 累积因子 -> 稀疏倍率


def test_to_sparse_factors_keeps_only_change_days():
    """累积因子只在除权日跳变, 稀疏化后应恰好只剩那几天。"""
    df = pl.DataFrame({
        "symbol": ["A.SZ"] * 5,
        "trade_date": [date(2026, 1, d) for d in (5, 6, 7, 8, 9)],
        "adj_factor": [1.0, 1.0, 1.1, 1.1, 1.1],
    })
    out = tp.to_sparse_factors(df)
    assert out.height == 1
    row = out.to_dicts()[0]
    assert row["trade_date"] == date(2026, 1, 7)
    assert row["ex_factor"] == pytest.approx(1.1)


def test_to_sparse_factors_multiplies_to_cumulative():
    """核心不变量: 稀疏倍率的累积乘积必须还原出累积因子之比。

    面板 _apply_adj_factor 走的是 cum_prod(ex_factor), Tushare 给的是累积 A,
    两者对齐即 ex_factor_i = A_i / A_{i-1}。
    """
    factors = [1.0, 1.0, 1.1, 1.1, 1.1, 1.265, 1.265]
    df = pl.DataFrame({
        "symbol": ["A.SZ"] * len(factors),
        "trade_date": [date(2026, 1, d) for d in range(1, len(factors) + 1)],
        "adj_factor": factors,
    })
    out = tp.to_sparse_factors(df)
    cum = 1.0
    for v in out["ex_factor"].to_list():
        cum *= v
    assert cum == pytest.approx(factors[-1] / factors[0])


def test_to_sparse_factors_is_per_symbol():
    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ", "B.SZ", "B.SZ"],
        "trade_date": [date(2026, 1, 1), date(2026, 1, 2)] * 2,
        "adj_factor": [1.0, 1.2, 5.0, 5.0],
    })
    out = tp.to_sparse_factors(df)
    # A 有一个事件, B 没有; 绝不能拿 B 的首行去除 A 的末行。
    assert out["symbol"].to_list() == ["A.SZ"]
    assert out["ex_factor"].to_list() == pytest.approx([1.2])


def test_to_sparse_factors_drops_first_bar_and_bad_prev():
    """首根 bar 没有前值; 前值为 0/负 属于脏数据, 都不能产出倍率。"""
    df = pl.DataFrame({
        "symbol": ["A.SZ"] * 3,
        "trade_date": [date(2026, 1, d) for d in (1, 2, 3)],
        "adj_factor": [1.0, 0.0, 2.0],
    })
    assert tp.to_sparse_factors(df).is_empty()


def test_to_sparse_factors_drops_non_positive_ratio():
    """前值正常但自身为 0 -> 比值为 0; 前值为 0 -> 比值为 inf。两者都会毁掉价格。"""
    df = pl.DataFrame({
        "symbol": ["A.SZ"] * 4,
        "trade_date": [date(2026, 1, d) for d in (1, 2, 3, 4)],
        "adj_factor": [1.0, 0.0, 1.5, -1.5],
    })
    assert tp.to_sparse_factors(df).is_empty()


# ---------------------------------------------------------------- 字段重命名容错

def test_rename_to_panel_tolerates_missing_columns():
    """Tushare 的字段集按报表类型/行业浮动 (银行股没有 operate_profit)。

    polars 的 rename 默认 strict, 一个不存在的列就整体抛 ColumnNotFoundError,
    所以必须只重命名真实存在的列, 缺列交给下游 keep 过滤。
    """
    df = pl.DataFrame({"operate_profit": [1.0], "revenue": [2.0]})
    out = tp._rename_to_panel(df, tp._INCOME_MAP)
    assert out.columns == ["operating_profit", "revenue"]   # revenue 同名, 保持
    # 全不匹配时原样返回, 不抛异常
    assert tp._rename_to_panel(df, {"total_assets": "total_assets_x"}).columns == df.columns
    assert tp._rename_to_panel(pl.DataFrame(), tp._INCOME_MAP).is_empty()


def test_to_sparse_factors_empty_and_missing_column():
    assert tp.to_sparse_factors(pl.DataFrame()).is_empty()
    assert tp.to_sparse_factors(pl.DataFrame({"symbol": ["A.SZ"]})).is_empty()


def test_to_sparse_factors_output_is_normalizable():
    """稀疏结果要能被面板的 normalize_adj_factors 接受 (表结构契约)。"""
    from app.data_providers.normalizer import normalize_adj_factors

    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ"],
        "trade_date": [date(2026, 1, 1), date(2026, 1, 2)],
        "adj_factor": [1.0, 1.1],
    })
    out = normalize_adj_factors(tp.to_sparse_factors(df), source="tushare")
    assert out.columns == ["symbol", "trade_date", "ex_factor"]
    assert out.schema["trade_date"] == pl.Date
    assert out.schema["ex_factor"] == pl.Float64


# ---------------------------------------------------------------- 缩放 / 转型助手


def test_num_scales_amount_thousand_yuan_to_yuan():
    """Tushare 的 amount 单位是千元, 面板内部是元。"""
    df = pl.DataFrame({"amount": ["1065690.76822"]})
    out = tp._num(df, "amount", scale=1000.0)
    assert out["amount"].to_list() == pytest.approx([1065690768.22])


def test_num_tolerates_missing_columns_and_nulls():
    df = pl.DataFrame({"a": ["1.5", None]})
    out = tp._num(df, "a", "not_there")
    assert out.columns == ["a"]
    assert out["a"].to_list()[0] == pytest.approx(1.5)
    assert out["a"].to_list()[1] is None


def test_dates_parses_yyyymmdd_and_is_null_safe():
    df = pl.DataFrame({"trade_date": ["20260916", None, "bad"]})
    out = tp._dates(df, "trade_date", "date")
    assert out["date"].to_list()[0] == date(2026, 9, 16)
    assert out["date"].to_list()[1:] == [None, None]


def test_dates_skips_when_column_absent():
    df = pl.DataFrame({"x": [1]})
    assert tp._dates(df, "missing", "date").columns == ["x"]


def test_frame_builds_dataframe_from_fields_items():
    payload = {"code": 0, "data": {"fields": ["ts_code", "close"], "items": [["000001.SZ", 10.5]]}}
    out = tp._frame(payload)
    assert out.height == 1
    assert out["ts_code"].to_list() == ["000001.SZ"]


@pytest.mark.parametrize("payload", [
    {},
    {"code": 0, "data": None},
    {"code": 0, "data": {"fields": [], "items": []}},
    {"code": 0, "data": {"fields": ["a"], "items": []}},
])
def test_frame_returns_empty_on_degenerate_payload(payload):
    assert tp._frame(payload).is_empty()


def test_call_returns_empty_on_api_error(monkeypatch):
    """Tushare 用 code != 0 表达权限/频次错误, 不能让整条同步链路崩掉。"""
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 50101, "msg": "必填参数, ts_code", "data": None}

    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp.httpx, "post", lambda *a, **k: _Resp())
    assert tp._call("income", {"period": "20260630"}, "").is_empty()


def test_call_without_token_short_circuits(monkeypatch):
    monkeypatch.setattr(tp, "_token", lambda: None)
    assert tp._call("daily", {"ts_code": "000001.SZ"}, "").is_empty()


def test_pace_is_globally_shared_across_tasks(monkeypatch):
    """限速器必须模块级共享。

    财务同步 (逐个标的, 约 30 分钟/表) 与盘后日线拉取可能同时进行: 若各自独立
    计步, 合并速率会翻倍并触发上游频次限制 —— 而超频在 ``_call`` 里只表现为
    「返回空表」, 等于**静默丢数据**。
    """
    slept: list[float] = []
    clock = [1000.0]

    def _now() -> float:
        return clock[0]

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(tp.time, "monotonic", _now)
    monkeypatch.setattr(tp.time, "sleep", _sleep)
    monkeypatch.setattr(tp, "_last_request_at", 0.0)

    tp._pace()
    assert slept == [], "距上次请求已久, 首次调用不应睡眠"
    tp._pace()
    assert slept == pytest.approx([tp._INTERVAL_S]), "紧随其后的请求必须补满间隔"
    tp._pace()
    assert len(slept) == 2, "连续请求应每次都补满间隔, 而不是只补第一次"


def test_pace_interval_stays_under_quota():
    """间隔必须 >= 60/200 秒, 否则会踩到 2100 积分档的 200 次/分钟上限。"""
    assert tp._RPM <= 200, "_RPM 不应高于 2100 积分档的 200 次/分钟上限"
    assert tp._INTERVAL_S >= 60.0 / 200.0


# ---------------------------------------------------------------- 资产类型


def test_a_share_only_filters_other_markets():
    syms = ["000001.SZ", "600519.SH", "430047.BJ", "00700.HK", "AAPL.US", "bad", ""]
    assert tp._a_share_only(syms) == ["000001.SZ", "600519.SH", "430047.BJ"]


def test_multi_code_apis_only_contains_daily():
    """fund_daily / index_daily 多代码会静默返回空 —— 必须逐个请求。"""
    assert len(tp._MULTI_CODE_APIS) == 1
    assert "daily" in tp._MULTI_CODE_APIS


def test_get_daily_rejects_unknown_asset_type():
    prov = tp.TushareProvider()
    assert prov.get_daily(["000001.SZ"], None, None, asset_type="crypto").is_empty()


def test_get_adj_factors_returns_empty_for_index_and_etf():
    """指数与 ETF 没有复权概念, 不该白白发请求。"""
    prov = tp.TushareProvider()
    for at in ("index", "etf"):
        assert prov.get_adj_factors(["000001.SH"], None, None, asset_type=at).is_empty()


def test_get_adj_factors_returns_empty_without_symbols():
    prov = tp.TushareProvider()
    assert prov.get_adj_factors([], None, None).is_empty()


def test_get_financials_rejects_unknown_table(caplog):
    prov = tp.TushareProvider()
    assert prov.get_financials("nope", ["000001.SZ"]).is_empty()


def test_get_financials_returns_empty_without_symbols():
    prov = tp.TushareProvider()
    assert prov.get_financials("income", []).is_empty()


def test_get_instruments_rejects_non_stock():
    prov = tp.TushareProvider()
    assert prov.get_instruments("etf") == []


# ---------------------------------------------------------------- 报表去重


def test_dedupe_reports_prefers_report_type_1_and_latest_announce():
    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ", "A.SZ"],
        "period_end": [date(2026, 6, 30)] * 3,
        "announce_date": [date(2026, 8, 1), date(2026, 8, 29), date(2026, 8, 20)],
        "report_type": ["4", "1", "1"],
        "revenue": [1.0, 3.0, 2.0],
    })
    out = tp._dedupe_reports(df, latest_only=True)
    assert out.height == 1
    assert out["revenue"].to_list() == [3.0]


def test_dedupe_reports_latest_only_keeps_newest_period():
    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ", "A.SZ"],
        "period_end": [date(2026, 3, 31), date(2026, 6, 30), date(2025, 12, 31)],
        "announce_date": [date(2026, 4, 1), date(2026, 8, 29), date(2026, 3, 1)],
        "revenue": [1.0, 2.0, 3.0],
    })
    out = tp._dedupe_reports(df, latest_only=True)
    assert out.height == 1
    assert out["period_end"].to_list() == [date(2026, 6, 30)]


def test_dedupe_reports_full_history_is_sorted_desc():
    df = pl.DataFrame({
        "symbol": ["A.SZ", "A.SZ"],
        "period_end": [date(2026, 3, 31), date(2026, 6, 30)],
        "revenue": [1.0, 2.0],
    })
    out = tp._dedupe_reports(df, latest_only=False)
    assert out["period_end"].to_list() == [date(2026, 6, 30), date(2026, 3, 31)]


def test_dedupe_reports_passthrough_when_empty_or_no_period_column():
    assert tp._dedupe_reports(pl.DataFrame(), True).is_empty()
    df = pl.DataFrame({"symbol": ["A.SZ"]})
    assert tp._dedupe_reports(df, True).height == 1


# ---------------------------------------------------------------- 端到端 (假网络)

_DAILY_PAYLOAD = {
    "code": 0,
    "data": {
        "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        "items": [
            ["000001.SZ", "20260916", 10.0, 10.5, 9.9, 10.2, 1234.0, 5678.0],
            ["000001.SZ", "20260915", 9.8, 10.1, 9.7, 10.0, 1000.0, 4000.0],
        ],
    },
}


def test_get_daily_normalizes_units_and_schema(monkeypatch):
    """日线链路: amount 千元->元, trade_date->Date, 输出面板 canonical 列。"""
    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp, "_call", lambda *a, **k: tp._frame(_DAILY_PAYLOAD))

    df = tp.TushareProvider().get_daily(["000001.SZ"], None, None)
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df["date"].to_list() == [date(2026, 9, 15), date(2026, 9, 16)]  # 升序
    assert df["amount"].to_list() == pytest.approx([4_000_000.0, 5_678_000.0])
    assert df["volume"].to_list() == pytest.approx([1000.0, 1234.0])


def test_get_adj_factors_end_to_end_sparsifies(monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "trade_date", "adj_factor"],
            "items": [
                ["000001.SZ", "20260916", 2.2],
                ["000001.SZ", "20260915", 2.2],
                ["000001.SZ", "20260914", 2.0],
                ["000001.SZ", "20260913", 2.0],
            ],
        },
    }
    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp, "_call", lambda *a, **k: tp._frame(payload))

    df = tp.TushareProvider().get_adj_factors(["000001.SZ"], None, None)
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 1
    assert df["trade_date"].to_list() == [date(2026, 9, 15)]
    assert df["ex_factor"].to_list() == pytest.approx([1.1])


def _patch_quiet(monkeypatch, recorder):
    """记录实际发出的 ts_code (限速内置在 _call 里, 这里 _call 被整体替换, 不会 sleep)。"""
    monkeypatch.setattr(tp, "_token", lambda: "x")

    def fake_call(api, params, fields=""):
        recorder.extend(params["ts_code"].split(","))
        return pl.DataFrame()

    monkeypatch.setattr(tp, "_call", fake_call)


def test_get_daily_filters_non_a_share_symbols(monkeypatch):
    """日线标的池可能是多市场兜底 —— 港美股代码发往 Tushare 只会拿到空表,
    白白消耗配额还会把日志刷满 warning。"""
    seen: list[str] = []
    _patch_quiet(monkeypatch, seen)

    out = tp.TushareProvider().get_daily(
        ["000001.SZ", "AAPL.US", "00700.HK", "600519.SH"], None, None
    )
    assert out.is_empty()
    assert seen == ["000001.SZ", "600519.SH"]


def test_get_daily_all_foreign_skips_requests_but_reports_progress(monkeypatch):
    """全是港美股时一个请求都不发, 但仍要回调一次进度 —— 否则前端进度卡在 0。"""
    seen: list[str] = []
    _patch_quiet(monkeypatch, seen)
    ticks: list[tuple[int, int]] = []

    out = tp._fetch_daily(
        "daily", ["AAPL.US", "00700.HK"], None, None,
        lambda cur, tot: ticks.append((cur, tot)),
    )
    assert out.is_empty()
    assert seen == []
    assert ticks == [(1, 1)]


def test_get_adj_factors_filters_non_a_share_symbols(monkeypatch):
    seen: list[str] = []
    _patch_quiet(monkeypatch, seen)

    tp.TushareProvider().get_adj_factors(["600519.SH", "AAPL.US"], None, None)
    assert seen == ["600519.SH"]


def test_get_metrics_scales_ocf_to_or_to_percent(monkeypatch):
    """fina_indicator 里只有 ocf_to_or 是小数, 其余已是百分点。"""
    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "end_date", "ann_date", "eps", "roe", "ocf_to_or"],
            "items": [["000001.SZ", "20260630", "20260829", 1.5, 7.34, -0.0758]],
        },
    }
    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp, "_call", lambda *a, **k: tp._frame(payload))

    df = tp.TushareProvider().get_financials("metrics", ["000001.SZ"])
    assert df.height == 1
    assert df["roe"].to_list() == pytest.approx([7.34])          # 不缩放
    assert df["operating_cash_to_revenue"].to_list() == pytest.approx([-7.58])  # 缩放


def test_get_income_maps_tushare_fields_to_panel_keys(monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "end_date", "ann_date", "report_type", "revenue", "oper_cost"],
            "items": [["000001.SZ", "20260630", "20260829", "1", 1000.0, 600.0]],
        },
    }
    monkeypatch.setattr(tp, "_token", lambda: "x")

    def fake_call(api, params, fields="", *a, **k):
        # 扣非净利润走的是 fina_indicator, 与利润表分属两个接口
        if api == "fina_indicator":
            return tp._frame({
                "code": 0,
                "data": {
                    "fields": ["ts_code", "end_date", "profit_dedt"],
                    "items": [["000001.SZ", "20260630", 250.0]],
                },
            })
        return tp._frame(payload)

    monkeypatch.setattr(tp, "_call", fake_call)

    df = tp.TushareProvider().get_financials("income", ["000001.SZ"])
    assert df["revenue"].to_list() == pytest.approx([1000.0])
    assert df["operating_cost"].to_list() == pytest.approx([600.0])
    assert df["net_income_deducted"].to_list() == pytest.approx([250.0])
    assert df["period_end"].to_list() == [date(2026, 6, 30)]
    # 假 payload 没给 operate_profit (真实场景: 银行/保险股就没有这一项)。
    # 容忍缺列是刻意的 —— 少一列好过整条同步报错, 前端按 null 显示。
    assert "operating_profit" not in df.columns
    assert "operating_cost" in df.columns


def test_get_shares_converts_wan_gu_to_gu(monkeypatch):
    """daily_basic 的股本单位是万股 -> 面板用股。"""
    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "trade_date", "total_share", "float_share"],
            "items": [["000001.SZ", "20260916", 19405.918198, 19405.684991]],
        },
    }
    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp, "_call", lambda *a, **k: tp._frame(payload))
    monkeypatch.setattr(tp, "_recent_open_dates", lambda *a, **k: ["20260916"])

    df = tp.TushareProvider().get_financials("shares", ["000001.SZ"])
    assert df["total_shares"].to_list() == pytest.approx([194_059_181.98])
    assert df["float_shares"].to_list() == pytest.approx([194_056_849.91])
    assert df["period_end"].to_list() == [date(2026, 9, 16)]


# ---------------------------------------------------------------- 与前端字段契约

# 与 frontend/src/components/financials/StockFinancialDetail.tsx 的 FIELD_DEFS 对齐
_FRONTEND_KEYS = {
    "metrics": {
        "eps_basic", "eps_diluted", "bps", "ocfps", "roe", "roe_diluted", "roa",
        "gross_margin", "net_margin", "debt_to_asset_ratio", "revenue_yoy",
        "net_income_yoy", "operating_cash_to_revenue", "inventory_turnover",
    },
    "income": {
        "revenue", "operating_cost", "operating_profit", "selling_expense",
        "admin_expense", "rd_expense", "financial_expense", "non_operating_income",
        "non_operating_expense", "total_profit", "income_tax", "net_income",
        "net_income_attributable", "net_income_deducted", "basic_eps", "diluted_eps",
    },
    "balance_sheet": {
        "total_assets", "total_current_assets", "total_non_current_assets",
        "cash_and_equivalents", "accounts_receivable", "inventory", "fixed_assets",
        "intangible_assets", "goodwill", "total_liabilities",
        "total_current_liabilities", "total_non_current_liabilities",
        "short_term_borrowing", "long_term_borrowing", "accounts_payable",
        "total_equity", "equity_attributable", "retained_earnings", "minority_interest",
    },
    "cash_flow": {
        "net_operating_cash_flow", "net_investing_cash_flow", "net_financing_cash_flow",
        "capex", "net_cash_change",
    },
    "shares": {"total_shares", "float_shares"},
}

# Tushare 侧确实没有对应字段的项 (不是漏配, 见模块 docstring)
_KNOWN_GAPS = {
    # fina_indicator 只有 roe / roe_waa / roe_dt(扣非), 没有稀释 ROE
    ("metrics", "roe_diluted"),
    # 扣非净利润来自 fina_indicator, 由 _fetch_report 单独 join 补上
    ("income", "net_income_deducted"),
    ("shares", "total_shares"),   # 走 daily_basic, 不在字段映射表里
    ("shares", "float_shares"),
}


def test_financial_field_maps_cover_frontend_keys():
    """字段映射必须覆盖前端会渲染的每一个 key, 否则财务页会留空列。"""
    provided = {
        "metrics": {*tp._METRICS_MAP, "operating_cash_to_revenue"},
        "income": set(tp._INCOME_MAP),
        "balance_sheet": set(tp._BALANCE_MAP),
        "cash_flow": set(tp._CASHFLOW_MAP),
        "shares": {"total_shares", "float_shares"},
    }
    for table, keys in _FRONTEND_KEYS.items():
        missing = keys - provided[table] - {k for t, k in _KNOWN_GAPS if t == table}
        assert not missing, f"{table} 缺少前端字段: {sorted(missing)}"
        # 映射表里也不该出现前端根本不渲染的键
        assert provided[table] <= keys, f"{table} 有前端未知字段"


def test_report_type_and_window_constants_are_sane():
    assert tp._REPORT_YEARS >= 1
    start_s, end_s = tp._report_window()
    assert len(start_s) == len(end_s) == 8
    assert start_s < end_s
    assert end_s == cn_today().strftime("%Y%m%d")


# ---------------------------------------------------------------- instruments 多市场合并

def test_merge_markets_not_covered_keeps_other_markets(tmp_path):
    """日K源切到 tushare 时, 港美股标的不能被 A 股维表覆盖掉。"""
    from app.services.instrument_sync import _merge_markets_not_covered

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "00700.HK", "AAPL.US"],
        "name": ["平安银行", "腾讯控股", "苹果"],
        "market": ["cn", "hk", "us"],
    }).write_parquet(inst_dir / "instruments.parquet")

    rows = [{"symbol": "600519.SH", "market": "cn"}]
    merged = _merge_markets_not_covered(rows, tmp_path)
    assert {r["symbol"] for r in merged} == {"600519.SH", "00700.HK", "AAPL.US"}


def test_merge_markets_not_covered_without_existing_file(tmp_path):
    from app.services.instrument_sync import _merge_markets_not_covered

    rows = [{"symbol": "600519.SH", "market": "cn"}]
    assert _merge_markets_not_covered(rows, tmp_path) == rows


def test_merge_markets_not_covered_drops_duplicate_cn_rows(tmp_path):
    from app.services.instrument_sync import _merge_markets_not_covered

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "00700.HK"],
        "market": ["cn", "hk"],
    }).write_parquet(inst_dir / "instruments.parquet")

    rows = [{"symbol": "000001.SZ", "market": "cn"}]
    merged = _merge_markets_not_covered(rows, tmp_path)
    assert [r["symbol"] for r in merged] == ["000001.SZ", "00700.HK"]


def test_get_instruments_shape_matches_flatten_contract(monkeypatch):
    """get_instruments 的输出要能被 instrument_sync._flatten_instruments 直接消费。"""
    from app.services.instrument_sync import _flatten_instruments

    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "symbol", "name", "area", "industry", "market", "list_date"],
            "items": [["600519.SH", "600519", "贵州茅台", "贵州", "白酒", "主板", "20010827"]],
        },
    }
    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp, "_call", lambda *a, **k: tp._frame(payload))

    rows = tp.TushareProvider().get_instruments("stock")
    assert len(rows) == 1
    flat = _flatten_instruments(rows)
    assert flat[0]["symbol"] == "600519.SH"
    assert flat[0]["exchange"] == "SH"
    assert flat[0]["market"] == "cn"
    assert flat[0]["listing_date"] == "2001-08-27"
    assert flat[0]["tick_size"] == 0.01


def test_recent_open_dates_returns_empty_on_failure(monkeypatch):
    monkeypatch.setattr(tp, "_token", lambda: "x")
    monkeypatch.setattr(tp, "_call", lambda *a, **k: pl.DataFrame())
    tp._CAL_CACHE.clear()
    assert tp._recent_open_dates(5) == []


def test_latest_daily_basic_walks_back_until_data(monkeypatch):
    """daily_basic 盘后才入库, 15:00 前查今天会拿到空表, 需要回退到上一个交易日。"""
    monkeypatch.setattr(tp, "_recent_open_dates", lambda *a, **k: ["20260917", "20260916"])

    def fake_call(api, params, fields="", *a, **k):
        if params.get("trade_date") == "20260916":
            return pl.DataFrame({"ts_code": ["000001.SZ"], "total_share": [1.0]})
        return pl.DataFrame()

    monkeypatch.setattr(tp, "_call", fake_call)
    out = tp._latest_available_daily_basic()
    assert out.height == 1
    assert out["ts_code"].to_list() == ["000001.SZ"]
