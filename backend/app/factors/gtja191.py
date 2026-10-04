"""GTJA Alpha191 骨架样例因子 (P1 第一批)。

公式口径以国泰君安 2017 年研报《基于短周期价量特征的多因子选股体系》为准,
并与 DolphinDB ``gtja191Alpha`` 的实现约定对齐:

- ``RANK`` / ``TSRANK`` 返回**百分比排名**, 不是绝对名次;
- ``SMA(A, n, m)`` 平滑系数为 ``m/n``;
- ``SUMAC(A, n)`` 与 ``SUM(A, n)`` 等价。

本批 11 个因子的选取标准是**算子覆盖**而非预测能力: ``ts_corr`` / ``ts_rank`` /
``decay_linear`` / ``regbeta`` / ``sma``(ewm) / ``iif`` 等骨架算子必须各有一个真实
使用案例, 这样后续批量补因子时才不会出现「算子还没实现」的返工。

⚠️ 口径提醒: 公开实现之间对同一因子常有不一致 (尤其 DECAYLINEAR 的权重方向、
TSRANK 的并列名次处理、``^`` 对负底数的行为)。这里采用明确且可审计的定义
(见 ``ir`` 模块顶部的约定)。若要与某个特定第三方实现逐位对齐, 应当改造对应的
**算子实现**, 而不是逐个去改公式, 否则公式与实现会双双漂移。
"""

from __future__ import annotations

from .ir import (
    CLOSE,
    HIGH,
    LOW,
    OPEN,
    VOLUME,
    VWAP,
    FactorDef,
    abs_,
    cs_rank,
    decay_linear,
    delay,
    delta,
    eq,
    gt,
    iif,
    le,
    log,
    max_,
    min_,
    regbeta,
    sign,
    sma,
    sqrt,
    ts_corr,
    ts_max,
    ts_mean,
    ts_rank,
    ts_std,
    ts_sum,
)

#: 研报里的 RET 与 DELAY(CLOSE, 1), 多个因子共用同一节点 (表达式树可安全复用)。
PREV_CLOSE = delay(CLOSE, 1)
RET = CLOSE / PREV_CLOSE - 1.0

#: 分组名, 前端按此归类展示。
GROUP = "GTJA191"


SKELETON_FACTORS: tuple[FactorDef, ...] = (
    FactorDef(
        id="gtja001",
        label="Alpha001",
        group=GROUP,
        desc="量价相关性反转: 成交量变化与开盘涨跌幅的截面排名负相关",
        formula="(-1 * CORR(RANK(DELTA(LOG(VOLUME), 1)), RANK((CLOSE - OPEN) / OPEN), 6))",
        expr=-ts_corr(
            cs_rank(delta(log(VOLUME), 1)),
            cs_rank((CLOSE - OPEN) / OPEN),
            6,
        ),
    ),
    FactorDef(
        id="gtja002",
        label="Alpha002",
        group=GROUP,
        desc="日内位置变化的负向: 收盘在当日区间中的相对位置变动",
        formula="(-1 * DELTA((((CLOSE - LOW) - (HIGH - CLOSE)) / (HIGH - LOW)), 1))",
        expr=-delta((CLOSE - LOW - (HIGH - CLOSE)) / (HIGH - LOW), 1),
    ),
    FactorDef(
        id="gtja003",
        label="Alpha003",
        group=GROUP,
        desc="6 日累计的「跳空后回补幅度」, 以昨日收盘为参照的摆动量",
        formula=(
            "SUM((CLOSE=DELAY(CLOSE,1)?0:CLOSE-(CLOSE>DELAY(CLOSE,1)?"
            "MIN(LOW,DELAY(CLOSE,1)):MAX(HIGH,DELAY(CLOSE,1)))),6)"
        ),
        expr=ts_sum(
            iif(
                eq(CLOSE, PREV_CLOSE),
                0,
                CLOSE
                - iif(
                    gt(CLOSE, PREV_CLOSE),
                    min_(LOW, PREV_CLOSE),
                    max_(HIGH, PREV_CLOSE),
                ),
            ),
            6,
        ),
    ),
    FactorDef(
        id="gtja005",
        label="Alpha005",
        group=GROUP,
        desc="量价秩相关的极值: 成交量与最高价 5 日排名相关性的 3 日最大值反向",
        formula="(-1 * TSMAX(CORR(TSRANK(VOLUME, 5), TSRANK(HIGH, 5), 5), 3))",
        expr=-ts_max(ts_corr(ts_rank(VOLUME, 5), ts_rank(HIGH, 5), 5), 3),
    ),
    FactorDef(
        id="gtja006",
        label="Alpha006",
        group=GROUP,
        desc="开盘与最高价加权价的 4 日变化方向, 截面排名反向",
        formula="(RANK(SIGN(DELTA((OPEN * 0.85 + HIGH * 0.15), 4))) * -1)",
        expr=-cs_rank(sign(delta(OPEN * 0.85 + HIGH * 0.15, 4))),
    ),
    FactorDef(
        id="gtja012",
        label="Alpha012",
        group=GROUP,
        desc="开盘相对 10 日均价的偏离排名, 乘以收盘对均价的绝对偏离排名反向",
        formula="(RANK((OPEN - (SUM(VWAP, 10) / 10)))) * (-1 * (RANK(ABS((CLOSE - VWAP)))))",
        expr=cs_rank(OPEN - ts_sum(VWAP, 10) / 10.0) * (-cs_rank(abs_(CLOSE - VWAP))),
    ),
    FactorDef(
        id="gtja013",
        label="Alpha013",
        group=GROUP,
        desc="最高价与最低价的几何均值相对成交均价的偏离",
        formula="((HIGH * LOW) ^ 0.5) - VWAP",
        expr=sqrt(HIGH * LOW) - VWAP,
    ),
    FactorDef(
        id="gtja014",
        label="Alpha014",
        group=GROUP,
        desc="5 日价格变动 (最小可用样例, 用于框架自检)",
        formula="CLOSE - DELAY(CLOSE, 5)",
        expr=CLOSE - delay(CLOSE, 5),
    ),
    FactorDef(
        id="gtja021",
        label="Alpha021",
        group=GROUP,
        desc="6 日均线的时间趋势斜率 (回归类算子的代表)",
        formula="REGBETA(MEAN(CLOSE, 6), SEQUENCE(6))",
        expr=regbeta(ts_mean(CLOSE, 6), 6),
    ),
    FactorDef(
        id="gtja023",
        label="Alpha023",
        group=GROUP,
        desc="上涨日波动占比: 20 日波动率在涨跌方向上的递推平滑比例",
        formula=(
            "SMA((CLOSE>DELAY(CLOSE,1)?STD(CLOSE,20):0),20,1) / "
            "(SMA((CLOSE>DELAY(CLOSE,1)?STD(CLOSE,20):0),20,1) + "
            "SMA((CLOSE<=DELAY(CLOSE,1)?STD(CLOSE,20):0),20,1)) * 100"
        ),
        expr=sma(
            iif(gt(CLOSE, PREV_CLOSE), ts_std(CLOSE, 20), 0), 20, 1
        )
        / (
            sma(iif(gt(CLOSE, PREV_CLOSE), ts_std(CLOSE, 20), 0), 20, 1)
            + sma(iif(le(CLOSE, PREV_CLOSE), ts_std(CLOSE, 20), 0), 20, 1)
        )
        * 100.0,
    ),
    FactorDef(
        id="gtja124",
        label="Alpha124",
        group=GROUP,
        desc="30 日最高价的截面排名做 2 日线性衰减 (decay_linear 算子的代表)",
        formula="DECAYLINEAR(RANK(TSMAX(CLOSE, 30)), 2)",
        expr=decay_linear(cs_rank(ts_max(CLOSE, 30)), 2),
    ),
)

#: id → 定义, 供注册表与测试使用。
SKELETON_BY_ID: dict[str, FactorDef] = {item.id: item for item in SKELETON_FACTORS}

#: 本批因子用到的原始列 (不含 VWAP 这类派生量)。
REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume", "amount"}
)
