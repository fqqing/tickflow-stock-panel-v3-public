"""Unit tests for app.markets: market dispatch, symbol normalization, limits, fees, sessions."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta

import pytest

from app.markets import (
    ALL_MARKETS,
    CN_TZ,
    MARKET_CN,
    MARKET_HK,
    MARKET_US,
    exchanges_for,
    get_market,
    has_limit,
    is_trading_now,
    lot_size_for,
    market_limit_pct,
    market_of,
    normalize_symbol,
    stamp_tax_double_sided,
    stamp_tax_for,
    trading_session_label,
)

# ── market_of: suffix dispatch ────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", MARKET_CN),
        ("000001.SZ", MARKET_CN),
        ("920344.BJ", MARKET_CN),
        ("00700.HK", MARKET_HK),
        ("AAPL.US", MARKET_US),
    ],
)
def test_market_of_suffix_dispatch(symbol, expected):
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.sh", MARKET_CN),
        ("00700.hk", MARKET_HK),
        ("aapl.us", MARKET_US),
    ],
)
def test_market_of_lowercase_input(symbol, expected):
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000", MARKET_CN),
        ("12345", MARKET_CN),
        ("AAPL", MARKET_US),
        ("ABCD", MARKET_US),
    ],
)
def test_market_of_no_suffix_fallback(symbol, expected):
    # All-digit codes are A shares, alphabetic codes are US by default.
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.XX", MARKET_CN),
        ("ABCD.XX", MARKET_US),
        ("600000.SHX", MARKET_CN),
    ],
)
def test_market_of_unknown_suffix(symbol, expected):
    # Unknown suffix: fallback keyed on the code's first character.
    assert market_of(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        (" AAPL.US ", MARKET_US),
        (" 600000.SH ", MARKET_CN),
        ("\t00700.HK\n", MARKET_HK),
        ("  600000  ", MARKET_CN),
    ],
)
def test_market_of_strips_whitespace(symbol, expected):
    # Padded input must resolve to the same market as the trimmed form:
    # market_of strips before matching, so the suffix still matches and a
    # numeric A-share code is no longer mis-routed to US.
    assert market_of(symbol) == expected
    assert market_of(symbol) == market_of(symbol.strip())


def test_market_of_agrees_with_normalize_symbol_on_padded_input():
    # Regression guard: market_of and normalize_symbol must not disagree on
    # whitespace handling (normalize_symbol has always stripped first).
    for raw in (" 600000.SH ", "\t00700.HK\n", " AAPL.US "):
        assert market_of(raw) == market_of(normalize_symbol(raw))


def test_market_of_empty_and_none():
    assert market_of("") == MARKET_US
    with pytest.raises(AttributeError):
        market_of(None)


# ── normalize_symbol ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("hk00700", "00700.HK"),
        ("hk700", "00700.HK"),  # zero-padded to 5 digits
        ("AAPL", "AAPL.US"),
        ("aapl", "AAPL.US"),
        ("600000", "600000.SH"),
        ("000001", "000001.SZ"),
        ("900001", "900001.SH"),
        ("300001", "300001.SZ"),
    ],
)
def test_normalize_symbol_common_forms(symbol, expected):
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SH600000", "600000.SH"),
        ("sh600000", "600000.SH"),
        ("sz000001", "000001.SZ"),
        ("bj920344", "920344.BJ"),
    ],
)
def test_normalize_symbol_a_share_exchange_prefix(symbol, expected):
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", "600000.SH"),
        ("00700.HK", "00700.HK"),
        ("AAPL.US", "AAPL.US"),
        (" 600000.SH ", "600000.SH"),
    ],
)
def test_normalize_symbol_idempotent(symbol, expected):
    # Already-normalized symbols round-trip; surrounding whitespace is stripped.
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("920344", "920344.BJ"),  # BJ 920 段先判, 不再被 6/9 规则错配成 .SH
        ("430001", "430001.BJ"),  # BJ 4xx 段
        ("HK", "00000.HK"),
        ("US", ".US"),
        ("SH", "SH.US"),  # bare "SH" is too short for the prefix branch
        ("US00700", "00700.US"),
        ("600000.XX", "600000.XX.US"),  # unknown suffix gets .US appended
    ],
)
def test_normalize_symbol_quirky_forms(symbol, expected):
    # Documenting actual behavior of edge inputs, not endorsing it.
    assert normalize_symbol(symbol) == expected


# ── market_limit_pct ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", 10.0),
        ("000001.SZ", 10.0),
        ("300001.SZ", 20.0),  # ChiNext
        ("688001.SH", 20.0),  # STAR
        ("689001.SH", 20.0),  # STAR (CDR prefix)
        ("830001.BJ", 30.0),  # Beijing 8xx
        ("430001.BJ", 30.0),  # Beijing 4xx
        ("920344.BJ", 30.0),  # Beijing 920
    ],
)
def test_market_limit_pct_a_share_boards(symbol, expected):
    assert market_limit_pct(symbol) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        # 主板 ST: 仅在 price_limits 的时间切换日之前降到 5%
        ("600001.SH", "ST大王", 5.0),
        ("600001.SH", "st小写", 5.0),  # ST 判定大小写不敏感
        # 创业板/科创板 ST 保持板块 20% —— 修复前这里被错算成 5%
        ("300001.SZ", "ST创业", 20.0),
        ("300001.SZ", "st公司", 20.0),
        ("688001.SH", "ST科创", 20.0),
        # 北交所 ST 保持 30%
        ("920344.BJ", "ST北证", 30.0),
    ],
)
def test_market_limit_pct_st_only_reduces_main_board(symbol, name, expected):
    """ST 只压主板, 不能覆盖创业板/科创板/北交所的板块基准。

    修复前 market_limit_pct 自带平行实现, ST 判定在板块判定之前无条件返回
    5.0; 现已委托 app.price_limits(单一事实源), 口径为「base == 主板 10%
    且为风险警示且在切换日之前」才降 5%。
    """
    from app.price_limits import MAIN_BOARD_ST_LIMIT_CHANGE_DATE

    before_cut = MAIN_BOARD_ST_LIMIT_CHANGE_DATE - timedelta(days=1)
    assert market_limit_pct(symbol, name, trade_date=before_cut) == pytest.approx(expected)


def test_market_limit_pct_main_board_st_after_rule_change():
    """切换日当天及之后, 主板 ST 回到 10% —— 时间维度必须透传给 price_limits。"""
    from app.price_limits import MAIN_BOARD_ST_LIMIT_CHANGE_DATE

    assert market_limit_pct(
        "600001.SH", "ST大王", trade_date=MAIN_BOARD_ST_LIMIT_CHANGE_DATE
    ) == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("300001.SZ", "普通股", 20.0),
        ("600001.SH", None, 10.0),
    ],
)
def test_market_limit_pct_without_st_name(symbol, name, expected):
    assert market_limit_pct(symbol, name) == pytest.approx(expected)


@pytest.mark.parametrize("symbol", ["AAPL.US", "00700.HK", "aapl.us"])
def test_market_limit_pct_hk_us_has_no_limit(symbol):
    assert market_limit_pct(symbol) is None


# ── fees and trading units ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("market", "tax", "double_sided"),
    [
        (MARKET_CN, 0.0005, False),  # sell side only
        (MARKET_HK, 0.001, True),  # both sides
        (MARKET_US, 0.0, False),  # none
    ],
)
def test_stamp_tax_rates(market, tax, double_sided):
    assert stamp_tax_for(market) == pytest.approx(tax)
    assert stamp_tax_double_sided(market) is double_sided


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, 100),
        (MARKET_HK, 100),
        (MARKET_US, 1),
    ],
)
def test_lot_size_for(market, expected):
    assert lot_size_for(market) == expected


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, True),
        (MARKET_HK, False),
        (MARKET_US, False),
    ],
)
def test_has_limit(market, expected):
    assert has_limit(market) is expected


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, ["SH", "SZ", "BJ"]),
        (MARKET_HK, ["HK"]),
        (MARKET_US, ["US"]),
    ],
)
def test_exchanges_for(market, expected):
    assert exchanges_for(market) == expected


def test_exchanges_for_returns_a_copy():
    result = exchanges_for(MARKET_CN)
    result.append("XX")
    assert get_market(MARKET_CN).exchanges == ("SH", "SZ", "BJ")


# ── trading sessions (deterministic via injected dt) ─────────────────


def _bj(hour: int, minute: int = 0) -> datetime:
    # A fixed weekday in Beijing time; only the wall-clock component matters.
    return datetime(2026, 7, 6, hour, minute, tzinfo=CN_TZ)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 30, True),  # morning open is inclusive
        (9, 29, False),
        (11, 29, True),
        (11, 30, False),  # lunch break starts exactly at 11:30, end is exclusive
        (12, 0, False),
        (12, 59, False),
        (13, 0, True),
        (14, 59, True),
        (15, 0, False),  # close at 15:00, end is exclusive
        (8, 0, False),
    ],
)
def test_is_trading_now_cn_sessions(hour, minute, expected):
    assert is_trading_now(MARKET_CN, _bj(hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 30, True),
        (9, 29, False),
        (11, 59, True),
        (12, 0, False),
        (13, 0, True),
        (15, 59, True),
        (16, 0, False),
    ],
)
def test_is_trading_now_hk_sessions(hour, minute, expected):
    assert is_trading_now(MARKET_HK, _bj(hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (21, 30, True),  # US open (DST window) is inclusive
        (21, 29, False),
        (23, 0, True),
        (3, 59, True),
        (4, 0, True),  # 04:00 belongs to the 22:30-05:00 winter window
        (4, 59, True),
        (5, 0, False),  # all US windows closed at 05:00
        (12, 0, False),
        (20, 0, False),
    ],
)
def test_is_trading_now_us_cross_midnight_sessions(hour, minute, expected):
    # The union of the DST (21:30-04:00) and non-DST (22:30-05:00) windows.
    assert is_trading_now(MARKET_US, _bj(hour, minute)) is expected


def test_is_trading_now_accepts_naive_datetime():
    # Only dt.time() is used, so a naive wall-clock datetime also works.
    assert is_trading_now(MARKET_CN, datetime(2026, 7, 6, 10, 0)) is True
    assert is_trading_now(MARKET_CN, datetime(2026, 7, 6, 16, 0)) is False


def test_is_trading_now_without_dt_returns_bool():
    # Smoke test for the default "now" path: it always yields a bool.
    for market in ALL_MARKETS:
        assert isinstance(is_trading_now(market), bool)


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (MARKET_CN, "09:30-11:30 / 13:00-15:00"),
        (MARKET_HK, "09:30-12:00 / 13:00-16:00"),
        (MARKET_US, "21:30-04:00 / 22:30-05:00"),
    ],
)
def test_trading_session_label(market, expected):
    assert trading_session_label(market) == expected


# ── registry and error handling ──────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        exchanges_for,
        has_limit,
        lot_size_for,
        stamp_tax_for,
        stamp_tax_double_sided,
        trading_session_label,
        is_trading_now,
    ],
)
def test_unknown_market_raises_value_error(fn):
    with pytest.raises(ValueError) as excinfo:
        fn("xx")
    assert "xx" in str(excinfo.value)


def test_registry_self_consistency():
    assert ALL_MARKETS == [MARKET_CN, MARKET_HK, MARKET_US]
    for market in ALL_MARKETS:
        meta = get_market(market)
        assert meta.market == market
        assert meta.lot_size > 0
        assert meta.price_round > 0
        assert meta.t_plus in (0, 1)
        assert meta.sessions
        assert all(len(session) == 2 for session in meta.sessions)
        # A market has price limits exactly when it carries a default pct.
        assert has_limit(market) is (meta.default_limit_pct is not None)
        # No tax means no double-sided flag; double-sided implies a tax.
        if meta.stamp_tax == 0:
            assert meta.stamp_tax_double_sided is False
        if meta.stamp_tax_double_sided:
            assert meta.stamp_tax > 0

    cn = get_market(MARKET_CN)
    assert cn.has_limit is True
    assert cn.default_limit_pct == 10.0
    assert cn.t_plus == 1
    assert cn.exchanges == ("SH", "SZ", "BJ")
    assert cn.lot_size == 100

    hk = get_market(MARKET_HK)
    assert hk.t_plus == 0
    assert hk.stamp_tax_double_sided is True
    assert hk.lot_size == 100

    us = get_market(MARKET_US)
    assert us.has_limit is False
    assert us.default_limit_pct is None
    assert us.stamp_tax == 0.0
    assert us.lot_size == 1


def test_market_meta_is_frozen():
    meta = get_market(MARKET_CN)
    with pytest.raises(FrozenInstanceError):
        meta.lot_size = 200


# ============================================================
# market_limit_pct 委托 price_limits（单一事实源）
# ============================================================
class TestMarketLimitPctDelegation:
    """market_limit_pct 曾自带一份平行实现, 与 app.price_limits 有两处口径矛盾。

    现已改为委托 price_limits。这些测试锁定"委托"这一事实本身: 任何一处口径
    分叉(ST 判定顺序、板块号段精度、时间切换)都会被捕获。
    """

    # 主板 ST 降 5% 的时间切换日之前
    BEFORE = date(2026, 7, 1)
    AFTER = date(2026, 8, 1)

    @pytest.mark.parametrize(
        ("symbol", "name", "expected"),
        [
            # 回归重点: ST 判定必须在板块判定「之后」, 且仅对主板生效。
            # 旧实现无条件先返回 5%, 创业板/科创板 ST 被错算成 5%。
            ("600000.SH", "ST浦发", 5.0),
            ("300750.SZ", "ST宁德", 20.0),
            ("688111.SH", "ST金山", 20.0),
            ("920344.BJ", "ST北证", 30.0),
            # 非 ST 基准
            ("600000.SH", "浦发银行", 10.0),
            ("300750.SZ", "宁德时代", 20.0),
            ("688111.SH", "金山办公", 20.0),
            ("920344.BJ", "北证股份", 30.0),
            ("000001.SZ", "平安银行", 10.0),
        ],
    )
    def test_st_only_reduces_main_board(self, symbol, name, expected):
        assert market_limit_pct(symbol, name, trade_date=self.BEFORE) == expected

    def test_main_board_st_no_longer_reduced_after_rule_change(self):
        """price_limits 有 MAIN_BOARD_ST_LIMIT_CHANGE_DATE 时间切换, 必须透传。"""
        assert market_limit_pct("600000.SH", "ST浦发", trade_date=self.AFTER) == 10.0

    @pytest.mark.parametrize(
        ("symbol", "name"),
        [("600000.SH", "ST浦发"), ("300750.SZ", "ST宁德"), ("920344.BJ", "北证股份")],
    )
    @pytest.mark.parametrize("trade_date", [date(2026, 7, 1), date(2026, 8, 1)])
    def test_agrees_with_price_limits_exactly(self, symbol, name, trade_date):
        """百分比口径换算后必须与单一事实源逐个一致(本函数返回 10.0, 源返回 0.10)。"""
        from app.price_limits import is_risk_warning_name, price_limit_pct

        expected = price_limit_pct(
            symbol, trade_date, is_risk_warning=is_risk_warning_name(name)
        ) * 100
        assert market_limit_pct(symbol, name, trade_date=trade_date) == expected

    @pytest.mark.parametrize("symbol", ["00700.HK", "AAPL.US", "0700.HK"])
    def test_no_limit_markets_return_none(self, symbol):
        """港美股无涨跌停, 恒 None —— 不能因委托而误落入 A 股分支。"""
        assert market_limit_pct(symbol, "任意名称") is None

    def test_trade_date_defaults_to_today(self):
        """省略 trade_date 时按"当前规则"解释, 不应抛异常。"""
        assert market_limit_pct("600000.SH", "浦发银行") == 10.0


# ============================================================
# normalize_symbol 北交所号段
# ============================================================
class TestNormalizeSymbolBeijing:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            # 回归重点: 920xxx 与沪市 B 股 900xxx 同以 "9" 开头,
            # 旧实现先走 6/9 规则, 把北交所代码错配成 .SH
            ("920344", "920344.BJ"),
            ("920819", "920819.BJ"),
            # 新三板转板号段同属北交所
            ("830799", "830799.BJ"),
            ("871981", "871981.BJ"),
            ("430418", "430418.BJ"),
            # 沪市: 60xxxx / 688xxx / 900xxx(B 股) 不受影响
            ("600000", "600000.SH"),
            ("688111", "688111.SH"),
            ("900901", "900901.SH"),
            # 深市
            ("000001", "000001.SZ"),
            ("300750", "300750.SZ"),
            ("200011", "200011.SZ"),
        ],
    )
    def test_pure_numeric_exchange_inference(self, code, expected):
        assert normalize_symbol(code) == expected

    @pytest.mark.parametrize("code", ["920344", "830799", "600000", "000001"])
    def test_normalize_then_market_of_is_cn(self, code):
        """归一化结果必须能被 market_of 正确识别为 A 股。"""
        assert market_of(normalize_symbol(code)) == "cn"

    @pytest.mark.parametrize("code", ["920344", "830799", "430418"])
    def test_beijing_limit_pct_via_normalized_symbol(self, code):
        """归一化 → 涨跌幅 全链路: 北交所应得 30%, 而非主板 10%。

        这是两处修复的交汇点: 旧实现下 920344 会被归一化成 .SH, 再喂给
        price_limits 就得到主板 10%, 与北交所 30% 相差三倍。
        """
        assert market_limit_pct(normalize_symbol(code), None) == 30.0
