"""多市场路由与门控的纯函数测试。

覆盖 A股/港股/美股扩展中三处"分叉点":
1. 存储目录路由 - enriched_dirname / daily_dirname
2. 交易成本默认值 - _market_default_fees (港股双边印花税)
3. 策略市场兼容门控 - _market_compatible_strategy (港美股过滤涨停依赖策略)

这些函数是多市场行为的收敛点, 一旦回归会静默串数据或算错成本,
但既有测试全部只覆盖 A 股路径, 故单独补齐。
"""

from dataclasses import replace
from datetime import date

import pytest

from app.api.backtest import _market_default_fees
from app.api.screener import _LIMIT_DEPENDENT_SIGNALS, _market_compatible_strategy
from app.markets import stamp_tax_double_sided, stamp_tax_for
from app.tickflow.repository import daily_dirname, enriched_dirname

# ============================================================
# 1. 存储目录路由
# ============================================================


@pytest.mark.parametrize(
    ("asset_type", "market", "expected"),
    [
        # A 股保持原目录名 - 既有数据与 DuckDB 视图依赖于此, 不可改。
        ("stock", "cn", "kline_daily_enriched"),
        # 港美股各自独立目录, 避免与 A 股串数据。
        ("stock", "hk", "kline_daily_enriched_hk"),
        ("stock", "us", "kline_daily_enriched_us"),
    ],
)
def test_enriched_dirname_routes_by_market(asset_type, market, expected):
    assert enriched_dirname(asset_type, market) == expected


@pytest.mark.parametrize(
    ("asset_type", "market", "expected"),
    [
        ("stock", "cn", "kline_daily"),
        ("stock", "hk", "kline_daily_hk"),
        ("stock", "us", "kline_daily_us"),
    ],
)
def test_daily_dirname_routes_by_market(asset_type, market, expected):
    assert daily_dirname(asset_type, market) == expected


def test_dirname_market_defaults_to_cn():
    """省略 market 必须等价于 cn - 保证既有 A 股调用点行为不变。"""
    assert enriched_dirname("stock") == enriched_dirname("stock", "cn")
    assert daily_dirname("stock") == daily_dirname("stock", "cn")
    assert enriched_dirname("stock") == "kline_daily_enriched"
    assert daily_dirname("stock") == "kline_daily"


@pytest.mark.parametrize("market", ["cn", "hk", "us"])
def test_etf_dirname_ignores_market(market):
    """asset_type='etf' 时 market 不参与路由 - ETF 只有 A 股一套目录。

    这是当前实现的实际行为(etf 分支在 market 判断之前 return)。
    若未来要支持港美股 ETF, 此测试会失败并提醒同步修改。
    """
    assert enriched_dirname("etf", market) == "kline_etf_enriched"
    assert daily_dirname("etf", market) == "kline_etf_daily"


def test_enriched_and_daily_dirnames_never_collide():
    """任意 asset_type/market 组合下, 原始日K与 enriched 目录必须不同名。"""
    seen = set()
    for asset_type in ("stock", "etf"):
        for market in ("cn", "hk", "us"):
            d, e = daily_dirname(asset_type, market), enriched_dirname(asset_type, market)
            assert d != e
            seen.add(d)
            seen.add(e)
    # stock 三市场各 2 个目录 + etf 共用 2 个 = 8 个唯一目录名
    assert len(seen) == 8


# ============================================================
# 2. 按市场的默认交易成本
# ============================================================


@pytest.mark.parametrize(
    ("market", "expected_tax", "expected_double"),
    [
        # A 股: 卖出单边 0.05%
        ("cn", 0.0005, False),
        # 港股: 买卖双边各 0.1% - double_sided 为 True 是港股成本正确的关键
        ("hk", 0.001, True),
        # 美股: 无印花税
        ("us", 0.0, False),
    ],
)
def test_market_default_fees(market, expected_tax, expected_double):
    tax, double_sided = _market_default_fees(market)
    assert tax == pytest.approx(expected_tax)
    assert double_sided is expected_double


def test_unknown_market_falls_back_to_cn_fees():
    """未知市场回落到 A 股默认值, 而不是抛错或返回 0 成本。

    回落到"有成本"比回落到"零成本"安全: 后者会让回测结果偏乐观。
    """
    assert _market_default_fees("unknown") == _market_default_fees("cn")


def test_only_hk_is_double_sided():
    """双边印花税只应出现在港股 - 误开会让 A 股/美股回测多扣一次税。"""
    double_sided = {m for m in ("cn", "hk", "us") if _market_default_fees(m)[1]}
    assert double_sided == {"hk"}


def test_zero_tax_implies_not_double_sided():
    """税率为 0 时 double_sided 必须为 False, 否则语义自相矛盾。"""
    for market in ("cn", "hk", "us"):
        tax, double_sided = _market_default_fees(market)
        if tax == 0.0:
            assert double_sided is False


@pytest.mark.parametrize("market", ["cn", "hk", "us"])
def test_default_fees_delegate_to_market_registry(market):
    """费率必须真正取自 app.markets 注册表, 而不是在 API 层重复硬编码。

    这条断言的意义在于: 若有人把数字重新写死回 _market_default_fees,
    改注册表就不会传导到回测, 两处口径会静默分叉 —— 本测试会失败。
    """
    assert _market_default_fees(market) == (
        stamp_tax_for(market),
        stamp_tax_double_sided(market),
    )


def test_registry_change_propagates_to_default_fees(monkeypatch):
    """改注册表里的税率, 回测默认费率必须跟着变(证明是同一份事实来源)。"""
    from app import markets as markets_mod

    bumped = replace(markets_mod.get_market("hk"), stamp_tax=0.007)
    monkeypatch.setitem(markets_mod._META, "hk", bumped)

    tax, double_sided = _market_default_fees("hk")
    assert tax == pytest.approx(0.007), "注册表改了税率, 回测默认值却没跟上"
    assert double_sided is True


# ============================================================
# 3. 策略市场兼容门控
# ============================================================


def test_cn_accepts_every_strategy():
    """A 股不做任何过滤 - 即使策略依赖涨停信号也必须通过。"""
    meta = {"entry_signals": ["signal_limit_up"], "exit_signals": ["signal_limit_down"]}
    assert _market_compatible_strategy(meta, "cn") is True


@pytest.mark.parametrize("market", ["hk", "us"])
@pytest.mark.parametrize("field", ["entry_signals", "exit_signals"])
def test_limit_dependent_strategy_rejected_on_hk_us(market, field):
    """港美股无涨跌停机制, 依赖涨停信号的策略必须被判为不兼容。"""
    meta = {field: ["signal_limit_up"]}
    assert _market_compatible_strategy(meta, market) is False


@pytest.mark.parametrize("signal", sorted(_LIMIT_DEPENDENT_SIGNALS))
def test_every_limit_signal_triggers_rejection(signal):
    """逐个校验涨停信号集合里的每一项都真的会拦下策略。

    参数化自 _LIMIT_DEPENDENT_SIGNALS 本身: 未来往集合里加信号,
    本测试自动覆盖, 不会漏。
    """
    assert _market_compatible_strategy({"entry_signals": [signal]}, "hk") is False


@pytest.mark.parametrize("market", ["hk", "us"])
def test_limit_dependent_alert_field_rejected(market):
    """alerts 里的 field 也参与判定, 不只看 entry/exit。"""
    meta = {"alerts": [{"field": "signal_limit_up"}]}
    assert _market_compatible_strategy(meta, market) is False


@pytest.mark.parametrize("market", ["hk", "us"])
def test_non_limit_strategy_accepted_on_hk_us(market):
    """不依赖涨跌停的普通技术策略在港美股必须可用。"""
    meta = {
        "entry_signals": ["signal_ma_cross", "signal_macd_gold"],
        "exit_signals": ["signal_rsi_high"],
        "alerts": [{"field": "close"}],
    }
    assert _market_compatible_strategy(meta, market) is True


@pytest.mark.parametrize("market", ["cn", "hk", "us"])
def test_empty_and_missing_meta_fields_are_safe(market):
    """meta 字段缺失/为 None/为空列表都不应抛错, 且视为不依赖涨停。"""
    assert _market_compatible_strategy({}, market) is True
    assert _market_compatible_strategy(
        {"entry_signals": None, "exit_signals": None, "alerts": None}, market
    ) is True
    assert _market_compatible_strategy(
        {"entry_signals": [], "exit_signals": [], "alerts": []}, market
    ) is True


@pytest.mark.parametrize("market", ["hk", "us"])
def test_malformed_alerts_entries_do_not_crash(market):
    """alerts 里混入非 dict 元素时应被忽略而非抛 AttributeError。"""
    meta = {"alerts": [None, "not-a-dict", 42, {"field": "signal_ma_cross"}]}
    assert _market_compatible_strategy(meta, market) is True


@pytest.mark.parametrize("market", ["hk", "us"])
def test_mixed_signals_still_rejected(market):
    """只要沾一个涨停信号就不兼容, 其余信号再多也不能放行。"""
    meta = {
        "entry_signals": ["signal_ma_cross", "signal_macd_gold", "signal_limit_up"],
        "exit_signals": ["signal_rsi_high"],
    }
    assert _market_compatible_strategy(meta, market) is False


# ============================================================
# 4. 策略结果缓存按市场隔离
# ============================================================
def test_strategy_cache_files_isolated_per_market(tmp_path):
    """run_all 写缓存必须落到市场独立文件, 否则港股结果会覆盖 A 股结果。"""
    from app.services import strategy_cache

    for market in ("cn", "hk", "us"):
        strategy_cache.write_cache(
            tmp_path, "2026-07-20",
            {f"strategy_{market}": {"total": 1, "as_of": "2026-07-20", "rows": [{"symbol": "X"}]}},
            market=market,
        )

    # cn 沿用原文件名(向后兼容), hk/us 独立文件
    assert (tmp_path / "user_data" / "strategy_cache.json").exists()
    assert (tmp_path / "user_data" / "strategy_cache_hk.json").exists()
    assert (tmp_path / "user_data" / "strategy_cache_us.json").exists()

    # 各市场读到自己的结果, 互不串扰
    assert "strategy_cn" in strategy_cache.read_cache(tmp_path, "cn")["results"]
    assert "strategy_hk" in strategy_cache.read_cache(tmp_path, "hk")["results"]
    assert "strategy_us" in strategy_cache.read_cache(tmp_path, "us")["results"]
    assert "strategy_hk" not in strategy_cache.read_cache(tmp_path, "cn")["results"]


def test_strategy_cache_clear_all_markets(tmp_path):
    """策略代码 reload 后清缓存必须清全部市场, 否则港美股继续展示旧公式结果。"""
    from app.services import strategy_cache

    for market in ("cn", "hk", "us"):
        strategy_cache.write_cache(
            tmp_path, "2026-07-20",
            {f"strategy_{market}": {"total": 1, "as_of": "2026-07-20", "rows": []}},
            market=market,
        )
    strategy_cache.clear_cache(tmp_path)  # market=None → 全部
    assert not (tmp_path / "user_data" / "strategy_cache.json").exists()
    assert not (tmp_path / "user_data" / "strategy_cache_hk.json").exists()
    assert not (tmp_path / "user_data" / "strategy_cache_us.json").exists()


# ============================================================
# 5. ScreenerService.latest_date 单一定义与三条路由分支
# ============================================================
def _latest_date_repo(*, cn=None, market=None, asset=None, duckdb=None):
    """记录调用去向的 repo 桩。"""
    import types as _t
    calls: list[str] = []

    class _R:
        store = _t.SimpleNamespace(data_dir=None)

        def latest_enriched_date_market(self, m):
            calls.append(f"market:{m}")
            return market

        def enriched_latest_date(self):
            calls.append("cn")
            return cn

        def get_enriched_latest_asset(self, asset_type):
            calls.append(f"asset:{asset_type}")
            return None, asset

        def execute_one(self, _sql):
            calls.append("duckdb")
            return (duckdb,) if duckdb else None

    return _R(), calls


def test_latest_date_has_exactly_one_definition():
    """回归: 多市场提交曾引入第二个 latest_date, 弱版本被后定义静默遮蔽。

    重复定义时 Python 不报错, 但先定义的那个永久失效 —— 改错副本会得到
    "代码已改却毫无效果"。此处锁定只允许一个定义。
    """
    import inspect

    from app.services.screener import ScreenerService

    source = inspect.getsource(ScreenerService)
    assert source.count("def latest_date") == 1

    # 生效版本必须同时具备三条分支, 否则等于退回被删掉的弱副本
    bound = inspect.getsource(ScreenerService.latest_date)
    assert "latest_enriched_date_market" in bound  # 港美股
    assert "get_enriched_latest_asset" in bound    # ETF
    assert "kline_enriched" in bound               # DuckDB 兜底


@pytest.mark.parametrize("market", ["hk", "us"])
def test_latest_date_routes_hk_us_to_market_dir(market):
    from app.services.screener import ScreenerService

    repo, calls = _latest_date_repo(market=date(2026, 7, 20))
    svc = ScreenerService(repo, asset_type="stock", market=market)
    assert svc.latest_date() == date(2026, 7, 20)
    assert calls == [f"market:{market}"]


def test_latest_date_routes_etf_to_asset_lookup():
    """A 股 ETF 必须走 asset 专用取数, 不能退化成全股票口径。"""
    from app.services.screener import ScreenerService

    repo, calls = _latest_date_repo(asset=date(2026, 7, 18))
    svc = ScreenerService(repo, asset_type="etf", market="cn")
    assert svc.latest_date() == date(2026, 7, 18)
    assert calls == ["asset:etf"]


def test_latest_date_falls_back_to_duckdb_when_parquet_empty():
    """A 股股票: parquet 无日期时回退 DuckDB, 而非直接返回 None。"""
    from app.services.screener import ScreenerService

    repo, calls = _latest_date_repo(cn=None, duckdb="2026-07-15")
    svc = ScreenerService(repo, asset_type="stock", market="cn")
    assert svc.latest_date() == date(2026, 7, 15)
    assert calls == ["cn", "duckdb"]
