"""内存数据源: 让 chan.py 引擎直接吃 numpy 数组, 绕开 parquet 重扫.

``TickflowAPI`` 每次 ``get_kl_data`` 都会重扫 parquet (实测 ~1700ms/票),
单票 ``/analysis`` 场景下这是不必要的开销 -- 数据已经由调用方从 repo 缓存
拿到了内存里。本模块把数组先 ``set_frame`` 进模块级注册表, chan.py 的
``CChan`` 实例化本类时按 ``code`` 取回, 把单票耗时压到纯计算 (~400ms)。

坐标约定: ``get_kl_data`` yield 的顺序就是 ``set_idx`` 的序号 (0-based),
与调用方喂进来的数组顺序一致, 因此 klu.idx 天然对齐 v1 的「窗口内索引」。
"""

from __future__ import annotations

from collections.abc import Iterable

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from DataAPI.CommonStockAPI import CCommonStockApi
from KLine.KLine_Unit import CKLine_Unit

# code -> {"dates": [date], "high": [float], "low": [float], "close": [float]}
_REGISTRY: dict[str, dict] = {}


def set_frame(code: str, dates, high, low, close) -> None:
    """把一维数组注册成 code 对应的内存行情, 供 CMemory 读取."""
    _REGISTRY[code] = {"dates": dates, "high": high, "low": low, "close": close}


def clear_frame(code: str | None = None) -> None:
    """释放注册表 (code=None 清空全部), 避免长时间驻留内存."""
    if code is None:
        _REGISTRY.clear()
    else:
        _REGISTRY.pop(code, None)


class CMemory(CCommonStockApi):
    """从内存注册表读取单标的 K 线 (只做日线, 前复权口径由调用方保证)."""

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super().__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self) -> Iterable[CKLine_Unit]:
        frame = _REGISTRY.get(self.code)
        if frame is None:
            return
        dates = frame["dates"]
        high = frame["high"]
        low = frame["low"]
        close = frame["close"]
        for i in range(len(dates)):
            d = dates[i]
            # open 用 close 兜底: 缠论包含处理只用 high/low, 分型/笔/中枢/买卖点
            # 也不依赖 open; MACD 用 close, 所以 open 的取值不影响结构。
            yield CKLine_Unit(
                {
                    DATA_FIELD.FIELD_TIME: CTime(d.year, d.month, d.day, 0, 0),
                    DATA_FIELD.FIELD_OPEN: float(close[i]),
                    DATA_FIELD.FIELD_HIGH: float(high[i]),
                    DATA_FIELD.FIELD_LOW: float(low[i]),
                    DATA_FIELD.FIELD_CLOSE: float(close[i]),
                    DATA_FIELD.FIELD_VOLUME: 0.0,
                    DATA_FIELD.FIELD_TURNOVER: 0.0,
                },
                autofix=True,
            )

    def SetBasciInfo(self) -> None:
        self.name = self.code
        self.is_stock = True

    @classmethod
    def do_init(cls) -> None:
        """内存数据无需连接初始化."""

    @classmethod
    def do_close(cls) -> None:
        """内存数据无需连接释放."""
