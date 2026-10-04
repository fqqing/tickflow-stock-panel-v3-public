"""缠论引擎入口: 把 tickflow 数据喂给 vendor 的 chan.py.

所有对上游的 import 都放在函数内部 -- bootstrap() 要先改 sys.path 才可能
import 到 vendor 模块, 写死在模块顶层会 import 失败.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from chanlab.bootstrap import bootstrap
from chanlab.profiles import resolve_profile

LevelSpec = str | Any

_LEVEL_ALIASES: dict[str, str] = {
    "1d": "K_DAY",
    "d": "K_DAY",
    "day": "K_DAY",
    "日": "K_DAY",
    "60m": "K_60M",
    "1h": "K_60M",
    "30m": "K_30M",
    "15m": "K_15M",
    "5m": "K_5M",
    "1m": "K_1M",
    "w": "K_WEEK",
    "week": "K_WEEK",
    "month": "K_MON",
    "mon": "K_MON",
}


def _resolve_levels(lv_list: list[LevelSpec] | tuple[LevelSpec, ...] | None):
    """把级别别名解析成 KL_TYPE 列表, 保持从大到小的顺序."""
    bootstrap()
    from Common.CEnum import KL_TYPE

    if lv_list is None:
        return [KL_TYPE.K_DAY]

    resolved = []
    for item in lv_list:
        if isinstance(item, KL_TYPE):
            resolved.append(item)
            continue
        name = _LEVEL_ALIASES.get(str(item).strip().lower())
        if name is None:
            raise ValueError(f"无法识别的 K 线级别: {item!r}")
        resolved.append(getattr(KL_TYPE, name))
    return resolved


def build_chan(
    symbol: str,
    start: str | date | None = None,
    end: str | date | None = None,
    lv_list: list[LevelSpec] | tuple[LevelSpec, ...] | None = None,
    config: dict | None = None,
    profile: str | None = None,
):
    """构造并完成计算的 CChan 实例.

    ``profile`` 是 :mod:`chanlab.profiles` 里的档位名; 没给 profile 也没给 config 时
    用默认档。⚠️ 显式传了 ``config`` 就不再套默认档 -- 否则调用方想复现「上游出厂
    口径」时会被默认档悄悄改回去, 两个档位跑出一模一样的结果而毫无察觉。
    """
    bootstrap()
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE

    levels = _resolve_levels(lv_list)
    if profile is None and config is not None:
        conf = dict(config)
    else:
        conf = resolve_profile(profile, config)
    return CChan(
        code=symbol,
        begin_time=str(start) if isinstance(start, date) else start,
        end_time=str(end) if isinstance(end, date) else end,
        data_src="custom:TickflowAPI.CTickflow",
        lv_list=levels,
        config=CChanConfig(conf),
        autype=AUTYPE.QFQ,
    )


def level_elements(chan, lv_index: int = 0) -> dict[str, list]:
    """取某个级别的四大元素列表.

    上游这些管理类大多 **不是可迭代对象** 也没有统一名字: CSegListComm 叫 ``lst``、
    CZSList 叫 ``zs_lst``、CBSPointList 叫 ``lst``、CBiList 叫 ``bi_list``.
    统一从这里取, 避免每个调用点各写各的属性名.
    """
    kl = chan[lv_index]
    return {
        "merged": kl.lst,
        "bi": kl.bi_list.bi_list,
        "seg": kl.seg_list.lst,
        "zs": kl.zs_list.zs_lst,
        # CBSPointList 的数据分散在 bsp_store_dict / bsp1_list 里, 没有统一的 lst
        # 成员; getSortedBspList 按 bi.idx 升序返回, 正好是我们要的时间序列口径.
        "bsp": kl.bs_point_lst.getSortedBspList(),
    }


def summarize(chan, lv_index: int = 0) -> dict:
    """对某个级别做结构计数摘要, 用于探针与回归测试."""
    elements = level_elements(chan, lv_index)
    bsp = elements["bsp"]
    return {
        "levels": [lv.name for lv in chan.lv_list],
        "klu": sum(len(klc.lst) for klc in elements["merged"]),
        "merged": len(elements["merged"]),
        "bi": len(elements["bi"]),
        "seg": len(elements["seg"]),
        "zs": len(elements["zs"]),
        "bsp": len(bsp),
        "buy": sum(1 for p in bsp if p.is_buy),
        "sell": sum(1 for p in bsp if not p.is_buy),
    }
