"""ChanStructure -- v2 前后端之间唯一的缠论结构契约.

为什么要有 pydantic 模型而不是直接拼 dict
--------------------------------------
1. 契约要能被**校验**. 上游 chan.py 的字段名很不一致 (CSegList 叫 ``lst``, CZSList
   叫 ``zs_lst``, bi 的成员变量几乎全是名字重整过的私有属性), 手写 dict 时写错一个
   属性只会在运行时炸在某只冷门票上。
2. 前后端要共享同一份 JSON Schema. 这里定义完可以直接 ``model_json_schema()`` 导出。
3. 后续接 FastAPI 时 response_model 直接复用, 不写第二遍。

坐标约定 (关键, 见 docs/chan-structure.md)
-------------------------------------
- ``i``  K 线索引: 请求窗口内的 0-based 下标, 与 K 线数组的顺序一一对应
- ``t``  epoch 毫秒 (UTC). 日线取当日 00:00 UTC, 分钟线取该分钟的 UTC 时刻。
  lightweight-charts 直接吃这个值做横轴
- ``p``  价格

``i`` 与 ``t`` 双写是为了两种渲染需求: 前端做逐点定位用 ``i`` (O(1) 数组下标),
画 overlay 时对齐全市场多标的用 ``t`` (跨数据源可比)。只给一个就必然有一边要反查。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0"


class Point(BaseModel):
    """结构上的一个端点: 某一根 K 线上的某个价位."""

    model_config = ConfigDict(extra="forbid")

    i: int = Field(ge=0, description="K 线索引, 窗口内 0-based")
    t: int = Field(description="epoch 毫秒 (UTC)")
    p: float = Field(description="端点价格")


class EngineInfo(BaseModel):
    """产出这批结构的引擎信息.

    前端要按 ``profile`` 做缓存 key -- 同一标的换档位会得到不一样的笔, 不带上这一项
    会让两个档位的结构互相污染缓存。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="chan.py", description="引擎标识")
    profile: str = Field(description="chanlab.profiles 里的档位名")
    lv_config: dict[str, Any] = Field(default_factory=dict,
                                      description="实际生效的 CChanConfig 关键项")


class Stroke(BaseModel):
    """笔."""

    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=0, description="笔序号, 时间升序")
    dir: str = Field(description="up / down")
    sure: bool = Field(description="是否已确认; False 是最后一笔虚笔, 前端应淡化 or 不画")
    begin: Point
    end: Point
    high: float = Field(description="笔区间最高价")
    low: float = Field(description="笔区间最低价")
    seg_idx: int | None = Field(default=None, description="所属线段序号; 未归入线段时为 null")
    klc_cnt: int = Field(ge=1, description="覆盖的合并 K 线数")
    klu_cnt: int = Field(ge=1, description="覆盖的原始 K 线数")


class Segment(BaseModel):
    """线段. v1 完全没有这一层, 是本次迁移新增的结构能力."""

    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=0)
    dir: str = Field(description="up / down")
    sure: bool
    begin: Point
    end: Point
    high: float
    low: float
    stroke_begin: int = Field(description="起始笔序号")
    stroke_end: int = Field(description="结束笔序号")
    stroke_count: int = Field(
        ge=1,
        description="包含的笔数。完全形成的线段必然 >= 3 笔; 只有 1~2 笔的是计算过程中的"
                    "过渡态, 此时 sure 一定为 False, 前端应当跳过而不渲染。"
                    "上游允许这种半成品存在, 所以这里不能卡死成 >= 3。",
    )


class Center(BaseModel):
    """中枢."""

    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=0)
    zg: float = Field(description="中枢上沿 = 构成中枢各笔高点的最小值")
    zd: float = Field(description="中枢下沿 = 构成中枢各笔低点的最大值")
    peak_high: float = Field(description="中枢涉及笔的最高价")
    peak_low: float = Field(description="中枢涉及笔的最低价")
    begin: Point
    end: Point
    stroke_count: int = Field(ge=1)
    sure: bool = Field(default=True)


class BuySellPoint(BaseModel):
    """买卖点.

    ``types`` 可能同时有多个值 (同一个点在两个判定口径下都成立), 上游有 6 类:
    1 / 1p(盘整背驰) / 2 / 2s(类二买) / 3a / 3b。v1 只有 1/2/3。
    """

    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=0)
    types: list[str] = Field(description="BSP_TYPE 取值: 1 / 1p / 2 / 2s / 3a / 3b")
    is_buy: bool
    at: Point
    stroke_idx: int | None = Field(default=None, description="落在哪一笔的终点")
    features: dict[str, float] = Field(default_factory=dict, description="可选特征值")


class LevelChan(BaseModel):
    """单个级别的完整结构."""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(description="级别标识, 如 1d / 30m")
    kl_type: str = Field(description="上游 KL_TYPE 名, 如 K_DAY")
    klu_count: int = Field(ge=0, description="喂进去的原始 K 线根数")
    klc_count: int = Field(ge=0, description="包含处理后的合并 K 线数")
    strokes: list[Stroke] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    centers: list[Center] = Field(default_factory=list)
    buy_sell_points: list[BuySellPoint] = Field(default_factory=list)
    parent_map: list[int] | None = Field(
        default=None,
        description="多级别时才有: 本级别第 j 根 K 线对应父级别第 parent_map[j] 根 K 线。"
                    "区间套渲染靠它, 单级别请求时为 null。",
    )


class ChanStructure(BaseModel):
    """顶层契约对象.

    ⚠️ 字段名不要叫 ``schema`` -- pydantic 的 BaseModel 有同名方法, 会被 shadow。
    """

    model_config = ConfigDict(extra="forbid")

    contract: str = Field(default=f"chan-structure/{CONTRACT_VERSION}",
                          description="契约名与版本")
    symbol: str
    window_begin: int | None = Field(default=None, description="窗口起始 epoch ms (UTC)")
    window_end: int | None = Field(default=None, description="窗口结束 epoch ms (UTC)")
    engine: EngineInfo
    primary_level: str = Field(description="主级别, levels[0]")
    levels: list[LevelChan] = Field(min_length=1)

    @property
    def level_names(self) -> list[str]:
        return [lv.level for lv in self.levels]
