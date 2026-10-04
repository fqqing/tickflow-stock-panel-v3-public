"""声明式因子框架的测试: 算子正确性 + 双后端数值一致性。

分三层:

1. **算子正确性** —— 用可以手算的序列验证 ``ops_np`` 的语义 (权重方向、百分比
   排名、回归斜率、递推平滑、缺失传播)。这一层防的是「两个后端一起算错」。
2. **双后端一致性 (干净面板)** —— 同一份表达式树分别交给 ``ops_np`` 与
   ``ops_pl`` 求值, 断言逐元素数值一致。这一层防的是「两个后端各算各的」。
3. **双后端一致性 (带缺失面板)** —— 面板里挖掉一部分 ``(标的, 日期)`` 单元再比
   一次。缺失值是双后端最容易漂移的地方 (Polars 的 ``.over()`` 嵌套陷阱、
   ``ewm_mean`` 的 ``ignore_nulls`` 口径、相关系数的方差下限都是在这里暴露的)。

一致性测试的数据布局刻意做成互补的两半: 面板是 ``(n_symbols, n_dates)``
矩阵, DataFrame 是等价的 ``(symbol, date)`` 长表。
"""

from __future__ import annotations

import datetime

import numpy as np
import polars as pl
import pytest

from app.factors import gtja191, ir, ops_np, ops_pl


# ================================================================
# 测试数据
# ================================================================
def _matrix_panel(
    n_symbols: int = 40,
    n_dates: int = 90,
    seed: int = 42,
) -> dict[str, np.ndarray]:
    """构造一段没有缺失值的合成行情。"""
    rng = np.random.default_rng(seed)
    close = 10.0 + np.cumsum(rng.normal(0.0, 0.2, (n_symbols, n_dates)), axis=1)
    close = np.abs(close) + 1.0
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.01, close.shape)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.01, close.shape)))
    open_ = close * (1.0 + rng.normal(0.0, 0.005, close.shape))
    volume = np.abs(rng.normal(1.0e6, 1.0e5, close.shape))
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": volume * close * 100.0,
    }


def _to_frame(
    panel: dict[str, np.ndarray],
    n_symbols: int,
    n_dates: int,
) -> pl.DataFrame:
    """把 ``(n_symbols, n_dates)`` 矩阵摊平成 ``(symbol, date)`` 长表。

    ``ravel`` 行优先, 因此 symbol 变化最慢, 与 ``_matrix_panel`` 的轴序一致。
    """
    symbols = [f"{index:06d}.SZ" for index in range(n_symbols)]
    dates = [f"2026-01-{index + 1:02d}" for index in range(n_dates)] if n_dates <= 28 else [
        f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}" for index in range(n_dates)
    ]
    data: dict[str, object] = {
        "symbol": np.repeat(symbols, n_dates),
        "date": np.tile(dates, n_symbols),
    }
    for name, values in panel.items():
        data[name] = values.ravel()
    return pl.DataFrame(data)


def _punch_holes(
    panel: dict[str, np.ndarray],
    fraction: float = 0.06,
    seed: int = 7,
) -> dict[str, np.ndarray]:
    """在同一个 ``(symbol, date)`` 掩码上把各列一起置为缺失。

    模拟停牌 / 数据缺口: 同一天同一标的的所有列一起缺, 而不是各列独立随机缺,
    否则会造出「有收盘价却没有成交量」这类现实里不存在的组合。
    """
    rng = np.random.default_rng(seed)
    shape = next(iter(panel.values())).shape
    mask = rng.random(shape) < fraction
    holed: dict[str, np.ndarray] = {}
    for name, values in panel.items():
        copy = np.array(values, dtype=np.float64, copy=True)
        copy[mask] = np.nan
        holed[name] = copy
    return holed


# ================================================================
# 第一层: 算子正确性
# ================================================================
def _single_row(values: list[float], n_dates: int | None = None) -> np.ndarray:
    """把一维序列包成 ``(1, T)`` 矩阵。"""
    array = np.asarray(values, dtype=np.float64).reshape(1, -1)
    if n_dates is not None and array.shape[1] < n_dates:
        pad = np.full((1, n_dates - array.shape[1]), np.nan)
        array = np.concatenate([pad, array], axis=1)
    return array


def test_ts_mean_and_sum_match_numpy():
    values = _single_row([1.0, 2.0, 3.0, 4.0, 5.0])
    mean = ops_np.evaluate(ir.ts_mean(ir.CLOSE, 3), {"close": values})
    total = ops_np.evaluate(ir.ts_sum(ir.CLOSE, 3), {"close": values})
    assert np.isnan(mean[0, :2]).all()
    np.testing.assert_allclose(mean[0, 2:], [2.0, 3.0, 4.0])
    np.testing.assert_allclose(total[0, 2:], [6.0, 9.0, 12.0])


def test_ts_std_is_population_std():
    values = _single_row([1.0, 2.0, 3.0, 4.0])
    std = ops_np.evaluate(ir.ts_std(ir.CLOSE, 4), {"close": values})
    np.testing.assert_allclose(std[0, 3], np.std([1.0, 2.0, 3.0, 4.0]))


def test_decay_linear_weights_newest_highest():
    # 权重 1:2:3 (旧→新), 归一化后 (1*1 + 2*2 + 3*3) / 6 = 14/6
    values = _single_row([1.0, 2.0, 3.0])
    out = ops_np.evaluate(ir.decay_linear(ir.CLOSE, 3), {"close": values})
    np.testing.assert_allclose(out[0, 2], 14.0 / 6.0)


def test_ts_rank_returns_percentile_of_last_value():
    values = _single_row([3.0, 1.0, 2.0])
    out = ops_np.evaluate(ir.ts_rank(ir.CLOSE, 3), {"close": values})
    # 末位 2.0, 窗口内 <= 2.0 的有 {1.0, 2.0} → 2/3
    np.testing.assert_allclose(out[0, 2], 2.0 / 3.0)


def test_regbeta_recovers_exact_slope():
    # 完美线性 [2,4,6,8] 对 1..4 回归, 斜率应为 2。
    values = _single_row([2.0, 4.0, 6.0, 8.0])
    out = ops_np.evaluate(ir.regbeta(ir.CLOSE, 4), {"close": values})
    np.testing.assert_allclose(out[0, 3], 2.0, atol=1e-12)


def test_regresi_is_residual_of_last_point():
    # 完美线性时残差应为 0。
    values = _single_row([2.0, 4.0, 6.0, 8.0])
    out = ops_np.evaluate(ir.regresi(ir.CLOSE, 4), {"close": values})
    np.testing.assert_allclose(out[0, 3], 0.0, atol=1e-12)


def test_sma_is_recursive_with_alpha_m_over_n():
    # SMA(X, 2, 1) → alpha = 0.5: y0=1, y1=1.5, y2=2.25
    values = _single_row([1.0, 2.0, 3.0])
    out = ops_np.evaluate(ir.sma(ir.CLOSE, 2, 1), {"close": values})
    np.testing.assert_allclose(out[0], [1.0, 1.5, 2.25])


def test_delay_and_delta_shift_by_position():
    values = _single_row([1.0, 2.0, 4.0, 8.0])
    delayed = ops_np.evaluate(ir.delay(ir.CLOSE, 1), {"close": values})
    changed = ops_np.evaluate(ir.delta(ir.CLOSE, 1), {"close": values})
    assert np.isnan(delayed[0, 0])
    np.testing.assert_allclose(delayed[0, 1:], [1.0, 2.0, 4.0])
    np.testing.assert_allclose(changed[0, 1:], [1.0, 2.0, 4.0])


def test_ts_argmax_counts_days_since_extreme():
    values = _single_row([1.0, 5.0, 2.0, 3.0])
    out = ops_np.evaluate(ir.ts_argmax(ir.CLOSE, 4), {"close": values})
    # 最大值 5.0 在窗口内第 1 位 (0 基), 距末位 2 天
    np.testing.assert_allclose(out[0, 3], 2.0)


def test_cs_rank_is_percentile_per_date():
    # 两列 (两天): 第 1 天排名 1,2,3; 第 2 天排名 3,1,2
    panel = np.array(
        [
            [10.0, 30.0],
            [20.0, 10.0],
            [30.0, 20.0],
        ]
    )
    out = ops_np.evaluate(ir.cs_rank(ir.CLOSE), {"close": panel})
    np.testing.assert_allclose(out[:, 0], [1.0 / 3.0, 2.0 / 3.0, 1.0])
    np.testing.assert_allclose(out[:, 1], [1.0, 1.0 / 3.0, 2.0 / 3.0])


def test_missing_value_propagates_through_window():
    values = np.array([[1.0, np.nan, 3.0, 4.0]])
    out = ops_np.evaluate(ir.ts_mean(ir.CLOSE, 2), {"close": values})
    # 窗口 [1, NaN] 与 [NaN, 3] 都必须给缺失。
    assert np.isnan(out[0, 1])
    assert np.isnan(out[0, 2])
    np.testing.assert_allclose(out[0, 3], 3.5)


def test_ewm_skips_missing_without_polluting_state():
    """递推平滑遇到缺失要给缺失, 但**不能**把缺失当成观测污染后续。"""
    values = _single_row([np.nan, 2.0, np.nan, 4.0, 5.0, np.nan, 7.0])
    out = ops_np.evaluate(ir.ewm_mean(ir.CLOSE, 0.5), {"close": values})
    np.testing.assert_allclose(
        out[0], [np.nan, 2.0, np.nan, 3.0, 4.0, np.nan, 5.5]
    )


def test_iif_propagates_missing_condition():
    """条件缺失 → 结果缺失 (对齐 Polars 的 null 比较语义)。"""
    values = np.array([[np.nan, 2.0, 3.0]])
    out = ops_np.evaluate(
        ir.iif(ir.gt(ir.CLOSE, 0.0), 1.0, 0.0), {"close": values}
    )
    assert np.isnan(out[0, 0]), "缺失条件被 np.where 当成了 False 分支"
    np.testing.assert_allclose(out[0, 1:], [1.0, 1.0])


def test_cs_rank_only_ranks_present_samples():
    panel = np.array([[1.0], [np.nan], [3.0], [5.0]])
    out = ops_np.evaluate(ir.cs_rank(ir.CLOSE), {"close": panel})
    # 3 个有效样本 → 名次依次 1/2/3, 百分比即 1/3, 2/3, 1
    np.testing.assert_allclose(out[0, 0], 1.0 / 3.0)
    assert np.isnan(out[1, 0])
    np.testing.assert_allclose(out[2, 0], 2.0 / 3.0)
    np.testing.assert_allclose(out[3, 0], 1.0)


def test_corr_is_undefined_when_one_side_is_constant():
    """窗口内某一路是常量 → 相关系数无定义 → 缺失 (而非 0)。"""
    values = np.array([[2.0, 2.0, 2.0, 2.0, 2.0]])
    varying = np.array([[1.0, 3.0, 2.0, 5.0, 4.0]])
    out = ops_np.evaluate(
        ir.ts_corr(ir.CLOSE, ir.OPEN, 5), {"close": values, "open": varying}
    )
    assert np.isnan(out[0, 4]), "常量序列的相关系数应为缺失"
    # 单边行情的真实写照: ts_rank 在连续窗口上取到同一个值 → 常量段
    trending = np.array([[5.0, 4.0, 3.0, 2.0, 1.0, 0.5, 0.4, 0.3, 0.2, 0.1]])
    rank = ops_np.evaluate(ir.ts_rank(ir.CLOSE, 5), {"close": trending})
    np.testing.assert_allclose(rank[0, 8], 0.2)  # 末位是窗口最小 → 1/5
    np.testing.assert_allclose(rank[0, 6], 0.2)


# ================================================================
# 第二层: 双后端一致性 (干净面板)
# ================================================================
_N_SYMBOLS = 40
_N_DATES = 90


@pytest.fixture(scope="module")
def panel() -> dict[str, np.ndarray]:
    return _matrix_panel(_N_SYMBOLS, _N_DATES)


@pytest.fixture(scope="module")
def frame(panel: dict[str, np.ndarray]) -> pl.DataFrame:
    return _to_frame(panel, _N_SYMBOLS, _N_DATES)


def _evaluate_both(
    expr: ir.Node,
    panel: dict[str, np.ndarray],
    frame: pl.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    """同一表达式分别过两个后端, 返回 ``(矩阵结果, 长表结果)``。"""
    matrix_result = ops_np.evaluate(expr, panel)
    long_frame = ops_pl.evaluate(expr).apply(frame, alias="value")
    long_result = long_frame["value"].to_numpy().reshape(_N_SYMBOLS, _N_DATES)
    return matrix_result, long_result


def _assert_backends_agree(
    matrix_result: np.ndarray,
    long_result: np.ndarray,
    label: str,
) -> None:
    np.testing.assert_allclose(
        matrix_result,
        long_result,
        rtol=1e-9,
        atol=1e-9,
        equal_nan=True,
        err_msg=f"{label} 双后端结果不一致",
    )


@pytest.mark.parametrize("definition", gtja191.SKELETON_FACTORS, ids=lambda d: d.id)
def test_skeleton_factors_agree_across_backends(
    definition: ir.FactorDef,
    panel: dict[str, np.ndarray],
    frame: pl.DataFrame,
) -> None:
    """同一表达式在 NumPy 与 Polars 后端必须逐元素一致。"""
    matrix_result, long_result = _evaluate_both(definition.expr, panel, frame)
    assert matrix_result.shape == (_N_SYMBOLS, _N_DATES)
    _assert_backends_agree(matrix_result, long_result, definition.id)


@pytest.mark.parametrize("definition", gtja191.SKELETON_FACTORS, ids=lambda d: d.id)
def test_skeleton_factors_produce_signal(
    definition: ir.FactorDef,
    panel: dict[str, np.ndarray],
) -> None:
    """因子必须有实际产出, 预热期之后不能大面积缺失。

    ``warmup`` 按嵌套窗口**累加**, 是所需天数的上界, 所以真正的要求是「预热期
    之后基本填满」。这里不要求 100% 有值: ``ts_corr`` 的某一端在窗口内是常量时
    相关系数在数学上无定义, 而 ``ts_rank`` 在单边行情里会连成常量段
    (gtja005 就有约 5% 的单元属于这种情况, 见
    ``test_corr_is_undefined_when_one_side_is_constant``)。窗口配错会导致
    预热期后大段空白, 用 90% 足以拦截。
    """
    result = ops_np.evaluate(definition.expr, panel)
    assert np.isfinite(result).any(), f"{definition.id} 全为缺失"
    warmup = definition.warmup
    tail = result[:, warmup:]
    present = np.isfinite(tail).mean()
    assert present > 0.9, (
        f"{definition.id} 预热 {warmup} 天后仍只有 {present:.1%} 有值 "
        f"({int((~np.isfinite(tail)).sum())} 个缺失单元)"
    )


# ================================================================
# 第三层: 双后端一致性 (带缺失面板)
# ================================================================
#: 覆盖各类缺失敏感路径: 横截面排名、时序变换后再排名、滚动相关、递推平滑、
#: 位置加权、回归、平移。
_RAGGED_EXPRESSIONS: tuple[tuple[str, ir.Node], ...] = (
    ("cs_rank(close)", ir.cs_rank(ir.CLOSE)),
    ("cs_scale(close)", ir.cs_scale(ir.CLOSE)),
    ("cs_rank(delta(log(volume),1))", ir.cs_rank(ir.delta(ir.log(ir.VOLUME), 1))),
    ("cs_rank(ts_max(close,10))", ir.cs_rank(ir.ts_max(ir.CLOSE, 10))),
    ("cs_scale(delta(close,1))", ir.cs_scale(ir.delta(ir.CLOSE, 1))),
    ("ts_rank(close,5)", ir.ts_rank(ir.CLOSE, 5)),
    (
        "ts_corr(ts_rank(volume,5), ts_rank(high,5), 5)",
        ir.ts_corr(ir.ts_rank(ir.VOLUME, 5), ir.ts_rank(ir.HIGH, 5), 5),
    ),
    # 跨轴嵌套: 先按日排名, 再按标的滚动 (需要物化中间列)
    (
        "ts_corr(cs_rank(close), cs_rank(open), 6)",
        ir.ts_corr(ir.cs_rank(ir.CLOSE), ir.cs_rank(ir.OPEN), 6),
    ),
    # 时序 → 横截面 → 时序, 两层嵌套
    (
        "ts_max(cs_rank(ts_max(close,10)),3)",
        ir.ts_max(ir.cs_rank(ir.ts_max(ir.CLOSE, 10)), 3),
    ),
    (
        "decay_linear(cs_rank(ts_max(close,10)),2)",
        ir.decay_linear(ir.cs_rank(ir.ts_max(ir.CLOSE, 10)), 2),
    ),
    ("sma(close,20,1)", ir.sma(ir.CLOSE, 20, 1)),
    ("ewm_mean(close,0.3)", ir.ewm_mean(ir.CLOSE, 0.3)),
    ("decay_linear(close,5)", ir.decay_linear(ir.CLOSE, 5)),
    ("wma(close,5)", ir.wma(ir.CLOSE, 5)),
    ("regbeta(close,6)", ir.regbeta(ir.CLOSE, 6)),
    ("ts_argmax(close,6)", ir.ts_argmax(ir.CLOSE, 6)),
    ("ts_prod(close,3)", ir.ts_prod(ir.CLOSE, 3)),
    ("delay(close,3)", ir.delay(ir.CLOSE, 3)),
    (
        "iif(close>delay(close,1), std(close,20), 0)",
        ir.iif(ir.gt(ir.CLOSE, ir.delay(ir.CLOSE, 1)), ir.ts_std(ir.CLOSE, 20), 0),
    ),
)


@pytest.fixture(scope="module")
def ragged_panel(panel: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return _punch_holes(panel)


@pytest.fixture(scope="module")
def ragged_frame(ragged_panel: dict[str, np.ndarray]) -> pl.DataFrame:
    return _to_frame(ragged_panel, _N_SYMBOLS, _N_DATES)


@pytest.mark.parametrize(
    "expr", [case[1] for case in _RAGGED_EXPRESSIONS], ids=[c[0] for c in _RAGGED_EXPRESSIONS]
)
def test_backends_agree_with_missing_cells(
    expr: ir.Node,
    ragged_panel: dict[str, np.ndarray],
    ragged_frame: pl.DataFrame,
) -> None:
    """面板挖洞后双后端仍须逐元素一致 —— 缺失语义是漂移高发区。"""
    matrix_result, long_result = _evaluate_both(expr, ragged_panel, ragged_frame)
    assert np.isfinite(matrix_result).any(), "挖洞后完全没有产出, 用例失去意义"
    _assert_backends_agree(matrix_result, long_result, "ragged")


def test_ragged_panel_actually_has_missing_cells(
    ragged_panel: dict[str, np.ndarray],
) -> None:
    """自检: 挖洞必须真的挖掉了 (否则第三层测试白跑)。"""
    holes = np.isnan(ragged_panel["close"])
    assert 0.02 < holes.mean() < 0.15
    # 同一 (标的, 日期) 的所有列应一起缺失。
    assert np.array_equal(holes, np.isnan(ragged_panel["volume"]))


def test_nan_input_is_normalized_to_null():
    """从 numpy 来的 ``NaN`` 必须先归一成 ``null``。

    Polars 里 ``NaN`` 是有效**值**, ``null`` 才是缺失。若不归一, ``count`` /
    ``rank`` / ``min_samples`` 会把 NaN 当样本, 双后端立刻漂移 (实测
    ``cs_rank`` 分母从 38 变 40), 而 ``ewm_mean`` 的 ``ignore_nulls`` 也拦不住
    NaN, 一个 NaN 会毁掉整条递推链。
    """
    nan_frame = pl.DataFrame(
        {
            "symbol": ["A"] * 3 + ["B"] * 3 + ["C"] * 3,
            "date": [0, 1, 2] * 3,
            "close": [1.0, float("nan"), 3.0, 2.0, 2.0, 2.0, 3.0, 3.0, 3.0],
        }
    )
    null_frame = nan_frame.with_columns(pl.col("close").fill_nan(None))
    # 前提: 构造出来确实是 NaN 而不是 null。
    assert nan_frame["close"].is_nan().sum() == 1
    assert nan_frame["close"].is_null().sum() == 0

    fixed = ops_pl.normalize_missing(nan_frame)
    assert fixed["close"].is_nan().sum() == 0
    assert fixed["close"].is_null().sum() == 1

    plan = ops_pl.evaluate(ir.cs_rank(ir.CLOSE))
    from_nan = plan.apply(nan_frame, alias="value")["value"].to_list()
    from_null = plan.apply(null_frame, alias="value")["value"].to_list()
    # NaN 输入与 null 输入必须走同一条路。
    assert from_nan == from_null
    assert from_nan[0] == pytest.approx(1.0 / 3.0)  # d0: A 在 3 个样本里排第 1
    assert from_nan[1] is None  # d1: A 缺失
    # 分母是有效样本数 (2) 而不是行数 (3) —— 归一化失效时这里会变成 1/3。
    assert from_nan[4] == pytest.approx(1.0 / 2.0)
    assert from_nan[6] == pytest.approx(1.0)  # d0: C 最大

    ewm = ops_pl.evaluate(ir.ewm_mean(ir.CLOSE, 0.5)).apply(
        pl.DataFrame(
            {
                "symbol": ["A"] * 4,
                "date": [0, 1, 2, 3],
                "close": [1.0, float("nan"), 3.0, 4.0],
            }
        ),
        alias="value",
    )["value"].to_list()
    assert ewm[0] == pytest.approx(1.0)
    assert ewm[1] is None
    assert ewm[2] == pytest.approx(2.0)  # 0.5*3 + 0.5*1, 缺失不参与递推
    assert ewm[3] == pytest.approx(3.0)  # 0.5*4 + 0.5*2


# ================================================================
# 第四层: 注册表接入 (因子目录 + 评分依赖 + 物化)
# ================================================================
def test_skeleton_factors_are_registered_in_factor_columns():
    """骨架因子必须出现在回测因子目录里 —— 那是 API 的唯一白名单。"""
    from app.backtest.factor import FACTOR_COLUMNS

    registered = {item["id"] for item in FACTOR_COLUMNS}
    missing = [d.id for d in gtja191.SKELETON_FACTORS if d.id not in registered]
    assert not missing, f"未注册到 FACTOR_COLUMNS: {missing}"


def test_scoring_registry_derives_gtja_dependencies():
    """评分侧的依赖与预热必须能从表达式树自动推导。"""
    from app.strategy.scoring import (
        VIRTUAL_SCORING_DEPENDENCIES,
        scoring_dependencies,
        scoring_warmup_bars,
    )

    for definition in gtja191.SKELETON_FACTORS:
        deps = VIRTUAL_SCORING_DEPENDENCIES[definition.id]
        assert deps == definition.dependency_fields, definition.id
        assert deps <= ir.AVAILABLE_FIELDS, definition.id

    # gtja013 = sqrt(HIGH*LOW) - VWAP, 而 VWAP = amount / (volume * 100)
    assert scoring_dependencies({"gtja013": 1.0}) == {
        "high",
        "low",
        "amount",
        "volume",
    }
    # 预热取所有启用因子的最大值, 是数据加载窗口的依据。
    assert scoring_warmup_bars({"gtja014": 1.0}) == 5
    assert scoring_warmup_bars({"gtja023": 1.0}) == 40


def test_materialize_scoring_columns_computes_all_skeleton_factors(
    frame: pl.DataFrame,
) -> None:
    """物化入口必须把所有骨架因子落成列, 且不留下中间列。"""
    from app.strategy.scoring import materialize_scoring_columns

    names = [definition.id for definition in gtja191.SKELETON_FACTORS]
    out = materialize_scoring_columns(frame, names)

    for name in names:
        assert name in out.columns, f"{name} 未物化"
        assert out[name].is_not_null().any(), f"{name} 全为缺失"
    assert not [column for column in out.columns if column.startswith("__factor_stage_")]
    # 原有列不能被改动或丢失。
    assert frame.columns == [c for c in out.columns if c in frame.columns]


@pytest.mark.parametrize("definition", gtja191.SKELETON_FACTORS, ids=lambda d: d.id)
def test_materialized_column_matches_numpy_backend(
    definition: ir.FactorDef,
    panel: dict[str, np.ndarray],
    frame: pl.DataFrame,
) -> None:
    """端到端闭环: 评分路径物化出的列 == 回测路径 NumPy 矩阵结果。"""
    from app.strategy.scoring import materialize_scoring_columns

    out = materialize_scoring_columns(frame, [definition.id])
    long_result = out[definition.id].to_numpy().reshape(_N_SYMBOLS, _N_DATES)
    _assert_backends_agree(
        ops_np.evaluate(definition.expr, panel), long_result, definition.id
    )


def test_materialize_is_idempotent_and_keeps_requested_only(frame: pl.DataFrame) -> None:
    """重复物化不重算, 也不应凭空多出没被请求的因子列。"""
    from app.strategy.scoring import materialize_scoring_columns

    once = materialize_scoring_columns(frame, ["gtja014"])
    twice = materialize_scoring_columns(once, ["gtja014"])
    assert once.columns == twice.columns
    assert "gtja023" not in twice.columns


# ================================================================
# 第五层: 回测 API 全流程 (替身引擎, 不依赖本地数据)
# ================================================================
class _StubEngine:
    """最小 ``BacktestEngine`` 替身。

    只需 ``load_panel``; ``repo`` / ``data_generation`` / ``assert_data_generation``
    等可选钩子缺失时 ``FactorBacktestService`` 会自行跳过。
    """

    def __init__(self, panel: pl.DataFrame) -> None:
        self._panel = panel

    def load_panel(
        self,
        symbols: list[str] | None,
        start: datetime.date,
        end: datetime.date,
        *,
        columns: list[str] | None = None,
        asset_type: str = "stock",
        expected_generation: str | None = None,
    ) -> pl.DataFrame:
        panel = self._panel
        if symbols is not None:
            panel = panel.filter(pl.col("symbol").is_in(list(symbols)))
        if columns is not None:
            panel = panel.select([c for c in columns if c in panel.columns])
        return panel.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _business_days(start: datetime.date, count: int) -> list[datetime.date]:
    days: list[datetime.date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += datetime.timedelta(days=1)
    return days


def _api_panel(
    n_symbols: int = 60,
    n_dates: int = 200,
    seed: int = 11,
) -> pl.DataFrame:
    """构造一份满足因子回测入参要求的日线面板。"""
    rng = np.random.default_rng(seed)
    days = _business_days(datetime.date(2025, 1, 1), n_dates)
    close = 10.0 + np.cumsum(rng.normal(0.0, 0.2, (n_symbols, n_dates)), axis=1)
    close = np.abs(close) + 2.0
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.012, close.shape)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.012, close.shape)))
    open_ = close * (1.0 + rng.normal(0.0, 0.006, close.shape))
    volume = np.abs(rng.normal(1.0e6, 1.5e5, close.shape))
    return pl.DataFrame(
        {
            "symbol": np.repeat([f"{i:06d}.SZ" for i in range(n_symbols)], n_dates),
            # symbol 变化最慢, 与矩阵行的排布一致。
            "date": pl.Series(list(days) * n_symbols, dtype=pl.Date),
            "open": open_.ravel(),
            "high": high.ravel(),
            "low": low.ravel(),
            "close": close.ravel(),
            "volume": volume.ravel(),
            "amount": (volume * close * 100.0).ravel(),
            "turnover_rate": np.abs(rng.normal(2.0, 0.5, close.shape)).ravel(),
        }
    )


def test_factor_batch_runs_skeleton_factors_end_to_end() -> None:
    """走一遍真正的 ``FactorBacktestService.run_batch``: 注册表 → 加载 → 物化 → IC。"""
    from app.backtest.factor import (
        FACTOR_COLUMNS,
        FactorBacktestService,
        FactorBatchConfig,
    )

    panel = _api_panel()
    days = sorted(panel["date"].unique().to_list())
    names = [definition.id for definition in gtja191.SKELETON_FACTORS]
    assert set(names) <= {item["id"] for item in FACTOR_COLUMNS}

    service = FactorBacktestService(_StubEngine(panel))
    result = service.run_batch(
        FactorBatchConfig(
            factor_names=names,
            symbols=None,
            start=days[100],
            end=days[-1],
            rebalance="daily",
            n_groups=5,
        )
    )

    assert result.error is None, result.error
    assert {item.factor_name for item in result.results} == set(names)
    for item in result.results:
        assert item.error is None, f"{item.factor_name}: {item.error}"
        assert item.ic_mean is not None, f"{item.factor_name} 未产出 IC"
        assert np.isfinite(item.ic_mean), item.factor_name
        assert item.coverage is not None and item.coverage > 0.5, item.factor_name
        assert item.n_dates > 50, f"{item.factor_name} 有效日期过少"


def test_factor_run_single_computes_group_and_long_short() -> None:
    """单因子入口要能产出分层净值与多空统计。"""
    from app.backtest.factor import FactorBacktestService, FactorConfig

    panel = _api_panel(n_symbols=40, n_dates=160)
    days = sorted(panel["date"].unique().to_list())
    service = FactorBacktestService(_StubEngine(panel))
    result = service.run(
        FactorConfig(
            factor_name="gtja124",
            symbols=None,
            start=days[80],
            end=days[-1],
            rebalance="daily",
            n_groups=5,
        )
    )

    assert result.error is None, result.error
    assert result.ic_mean is not None and np.isfinite(result.ic_mean)
    assert len(result.group_stats) == 5
    assert result.long_short_stats.get("total_return") is not None
    assert result.coverage is not None and result.coverage > 0.5


# ================================================================
# 框架自检
# ================================================================
def test_warmup_accumulates_nested_windows():
    """预热天数按嵌套窗口**累加**, 是所需天数的上界 (取最大会偏小)。"""
    # CLOSE - DELAY(CLOSE, 5) → 5
    assert gtja191.SKELETON_BY_ID["gtja014"].warmup == 5
    # SMA(iif(..., STD(CLOSE,20), ...), 20, 1) → 20 (STD 满窗) + 20 (SMA 递推收敛)
    assert gtja191.SKELETON_BY_ID["gtja023"].warmup == 40
    # DECAYLINEAR(RANK(TSMAX(CLOSE,30)), 2) → 30 + 2
    assert gtja191.SKELETON_BY_ID["gtja124"].warmup == 32


def test_nested_window_inputs_are_materialized():
    """嵌套 ``.over()`` 必须被拆成「中间列 + 单层表达式」。

    Polars 对嵌套 ``.over()`` 处理不可靠: 聚合型会**静默丢弃**外层分组 (实测全
    null / count 全 0), 滚动型和 ``when/then`` / 乘法组合时也会整列退化成 null
    (gtja001 的 ``CORR(RANK(DELTA(...)), RANK(...), 6)``)。所以凡是自身会产生
    ``.over()`` 的算子, 只要入参已含 ``.over()`` 就必须先物化。
    """
    # 入参无窗口 → 不需要物化。
    assert ops_pl.evaluate(ir.cs_rank(ir.CLOSE)).stages == ()
    assert ops_pl.evaluate(ir.cs_scale(ir.CLOSE / ir.VOLUME)).stages == ()
    assert ops_pl.evaluate(ir.delay(ir.CLOSE, 3)).stages == ()
    assert ops_pl.evaluate(ir.ts_mean(ir.CLOSE, 5)).stages == ()

    # 入参含窗口算子 → 必须物化。
    assert len(ops_pl.evaluate(ir.cs_rank(ir.delta(ir.log(ir.VOLUME), 1))).stages) == 1
    assert len(ops_pl.evaluate(ir.cs_scale(ir.ts_max(ir.CLOSE, 10))).stages) == 1
    assert len(ops_pl.evaluate(ir.ts_sum(ir.ts_mean(ir.CLOSE, 5), 10)).stages) == 1

    # 两个入参都含窗口 → 各自物化一次。
    assert (
        len(ops_pl.evaluate(ir.ts_corr(ir.cs_rank(ir.CLOSE), ir.cs_rank(ir.OPEN), 6)).stages)
        == 2
    )
    # 时序套横截面套时序 → 两层都要物化。
    assert (
        len(ops_pl.evaluate(ir.ts_max(ir.cs_rank(ir.ts_max(ir.CLOSE, 30)), 3)).stages)
        == 2
    )

    # 骨架因子里最复杂的一个: 两个 cs_rank 入参 + 内侧 delta 的物化。
    gtja001 = gtja191.SKELETON_BY_ID["gtja001"]
    assert len(ops_pl.evaluate(gtja001.expr).stages) == 3



def test_skeleton_covers_key_operators():
    """骨架样例必须覆盖难点算子, 否则等于没验证。"""
    used: set[str] = set()
    for definition in gtja191.SKELETON_FACTORS:
        used.update(call.name for call in ir.iter_calls(definition.expr))
    required = {"ts_rank", "decay_linear", "regbeta", "ts_corr", "sma", "iif", "cs_rank"}
    missing = required - used
    assert not missing, f"骨架样例缺少算子覆盖: {sorted(missing)}"


def test_factor_ids_are_unique_and_prefixed():
    ids = [definition.id for definition in gtja191.SKELETON_FACTORS]
    assert len(ids) == len(set(ids))
    assert all(name.startswith("gtja") for name in ids)


def test_unknown_field_raises_clear_error():
    values = _matrix_panel(2, 30)
    definition = ir.FactorDef(
        id="broken",
        label="broken",
        group="test",
        desc="引用了不存在的字段",
        expr=ir.Field("vwap_raw") + 1.0,
    )
    with pytest.raises(KeyError, match="未知字段"):
        ops_np.evaluate(definition.expr, values)


def test_requires_are_declared_in_available_fields():
    assert gtja191.REQUIRED_FIELDS <= ir.AVAILABLE_FIELDS
