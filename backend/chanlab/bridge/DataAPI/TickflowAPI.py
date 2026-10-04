"""tickflow 数据源: 让 chan.py 直接读本地 K 线, 支持日线与分钟线.

本模块由 bootstrap 通过扩展 ``DataAPI.__path__`` 注入, 加载时的模块名是
``DataAPI.TickflowAPI``. 因此这里 **不能使用相对导入** -- 相对路径会被解析到
DataAPI 包内部去, 必须写绝对导入 ``chanlab.loader``.

按周期分源::

    日线   -> v1 的 enriched parquet (前复权, 与 v1 对拍口径一致)
    分钟线 -> free-stockdb 本地库 (2025-01-02 起, 1m/5m/15m/30m/60m)

``CChan`` 会对每个级别各实例化一次本类, 所以构造里不能做重活.
"""

from __future__ import annotations

from collections.abc import Iterable

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_Unit import CKLine_Unit

from chanlab.loader import load_symbol_daily, load_symbol_minute

KL_TO_MINUTE_FREQ = {
    KL_TYPE.K_1M: "1m",
    KL_TYPE.K_5M: "5m",
    KL_TYPE.K_15M: "15m",
    KL_TYPE.K_30M: "30m",
    KL_TYPE.K_60M: "60m",
}


class CTickflow(CCommonStockApi):
    """从本地数据读取单标的 K 线, 日线与分钟线走不同的源."""

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super().__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self) -> Iterable[CKLine_Unit]:
        """按时间升序 yield 单根 K 线."""
        if self.k_type == KL_TYPE.K_DAY:
            yield from self._daily()
        else:
            yield from self._minute()

    def _daily(self) -> Iterable[CKLine_Unit]:
        frame = load_symbol_daily(self.code, self.begin_date, self.end_date)
        for row in frame.iter_rows(named=True):
            day = row["date"]
            yield CKLine_Unit(
                {
                    DATA_FIELD.FIELD_TIME: CTime(day.year, day.month, day.day, 0, 0),
                    DATA_FIELD.FIELD_OPEN: float(row["open"]),
                    DATA_FIELD.FIELD_HIGH: float(row["high"]),
                    DATA_FIELD.FIELD_LOW: float(row["low"]),
                    DATA_FIELD.FIELD_CLOSE: float(row["close"]),
                    DATA_FIELD.FIELD_VOLUME: float(row.get("volume") or 0.0),
                    DATA_FIELD.FIELD_TURNOVER: float(row.get("amount") or 0.0),
                },
                autofix=True,  # 复权后的 OHLC 可能有浮点毛刺, 自修正而不是抛异常
            )

    def _minute(self) -> Iterable[CKLine_Unit]:
        freq = KL_TO_MINUTE_FREQ.get(self.k_type)
        if freq is None:
            raise ValueError(f"暂不支持的周期: {self.k_type}")
        frame = load_symbol_minute(
            self.code,
            freq=freq,
            start=self.begin_date,
            end=self.end_date,
        )
        for row in frame.iter_rows(named=True):
            ts = row["datetime"]
            yield CKLine_Unit(
                {
                    DATA_FIELD.FIELD_TIME: CTime(
                        ts.year, ts.month, ts.day, ts.hour, ts.minute
                    ),
                    DATA_FIELD.FIELD_OPEN: float(row["open"]),
                    DATA_FIELD.FIELD_HIGH: float(row["high"]),
                    DATA_FIELD.FIELD_LOW: float(row["low"]),
                    DATA_FIELD.FIELD_CLOSE: float(row["close"]),
                    DATA_FIELD.FIELD_VOLUME: float(row["volume"]),
                    DATA_FIELD.FIELD_TURNOVER: float(row["amount"]),
                },
                autofix=True,
            )

    def SetBasciInfo(self) -> None:
        self.name = self.code
        self.is_stock = True

    @classmethod
    def do_init(cls) -> None:
        """本地数据无需连接初始化."""

    @classmethod
    def do_close(cls) -> None:
        """本地数据无需连接释放."""
