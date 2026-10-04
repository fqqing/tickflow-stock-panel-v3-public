"""缠论引擎的配置档位 (profile).

为什么要有这一层
----------------
chan.py 的 ``CChanConfig`` 有几十个旋钮, 每一个都能改变笔的样子。不加管控的话,
终端页 / 扫描器 / 实验脚本各自写一套 config, 同一个标的会画出互不相认的三种结构
-- 这正是 v1 后期「改来改去」的重演。所有档位集中在这里, 调用点只传名字。

档位的来历
----------
``v1`` 档不是拍脑袋定的, 是 ``backend/scripts/tune_bi.py`` 在 10 只 A 股 x 800 根
日线上遍历标定出来的结果。标定前先确认了两个前提:
1. 两侧的**包含处理完全一致** (10 只票 100% 重合), 所以差异与底图无关
2. 三个主控开关里, ``bi_end_is_peak`` 是最大差异源, ``bi_fx_check`` 次之
"""

from __future__ import annotations

# 所有档位共用的静音项. 上游默认会 print 大量 warning, 批量跑时会淹没输出
QUIET = {
    "print_warning": False,
    "print_err_time": False,
}

PROFILES: dict[str, dict] = {
    # chan.py 出厂默认: bi_strict=True / bi_fx_check=strict / bi_end_is_peak=True
    # 三道闸门全开, 结构最保守, 但与我们 v1 的画法相差最远 (笔数只有 v1 的 ~70%)
    "upstream": {},
    # 标定后最接近 v1 自研口径的档位。
    #   bi_end_is_peak=False: 不要求笔端点是两端之间的极值。这是最大的一道差异闸门,
    #     v1 完全没有; 关掉后笔数从 44 回到 56, 与 v1 的 60 基本持平。
    #   bi_fx_check=loss:     分型有效性只比较顶底那两根 K 线本身, 不扩大到前后三根。
    #   bi_strict=True:       保留标准笔跨度 (>=4 根合并 K 线), 与 v1 的 min_gap=4 一致。
    # 实测 (10 只 x 800 根): 贴合 v1 97%, 覆盖 v1 96%, 端点召回 97%。
    "v1": {
        "bi_strict": True,
        "bi_fx_check": "loss",
        "bi_end_is_peak": False,
    },
}

DEFAULT_PROFILE = "v1"


def resolve_profile(name: str | None = None, overrides: dict | None = None) -> dict:
    """把档位名解析成 CChanConfig 字典.

    ``overrides`` 里同名的键会覆盖档位值, 用于单次实验; 值为 None 的键会被删除,
    方便显式退回上游默认。
    """
    name = name or DEFAULT_PROFILE
    if name not in PROFILES:
        raise ValueError(f"未知缠论配置档位: {name!r}, 可选 {sorted(PROFILES)}")

    conf = {**QUIET, **PROFILES[name]}
    for key, value in (overrides or {}).items():
        if value is None:
            conf.pop(key, None)
        else:
            conf[key] = value
    return conf


def available_profiles() -> list[str]:
    return sorted(PROFILES)
