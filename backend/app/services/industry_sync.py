"""行业分类摄取服务。

链路: stock-sdk `board.industry.list()` → 逐板块 `constituents()` → 反推
「个股 → 行业板块」长表 → ``data/industry_members/industry_members.parquet``。

**为什么需要**: Alpha191 的行业中性化需要每个标的的行业归属, 而现有
``instruments`` 维表只含标的元数据(name/股本/涨跌停), 没有行业维度;
``data/kline_daily`` 也没有可用的行业列。故单独立一张维表。

⚠️ **口径**: 这是**东财行业板块(BK 编码)**, 不是申万分类 —— 两者层级与命名都
不同。若后续要做申万口径中性化, 需在本表之上另加一层映射, 不要在本表原地改名。

⚠️ **分层**: 东财行业板块分一级/二级/三级, 同一只票会命中多个板块。本表如实存
下全部 ``(symbol, board_code)`` 关系、不做层级裁剪 —— 摄取环节丢掉层级是不可逆的,
取哪一层属于消费方的语义决策(见 ``load_industry_members``)。

跟随日K数据源: 与标的维表同理(见 ``instrument_sync``), 二者天然耦合、无独立偏好项。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

# 维表落盘位置与列契约(消费方按这些名字取值)。
INDUSTRY_DIR = "industry_members"
INDUSTRY_FILE = "industry_members.parquet"

#: 默认新鲜度阈值(天)。东财板块调整以周/月计, 而全量抓取要 ~500 次请求
#: (实测上游抖动下整轮可达数分钟), 按天重抓是纯浪费 —— 故默认 7 天内不重抓。
INDUSTRY_REFRESH_DAYS = 7

#: 板块完整率下限。低于此值**不覆盖**旧表: 部分板块抓失败产出的表看不出缺口,
#: 却带着当天的 as_of, 会被下游当成新鲜完整数据 —— 宁可留旧数据。
INDUSTRY_MIN_COVERAGE = 0.9

#: 长表列契约。``as_of`` 为同步日期, 便于判断行业归属的新鲜度。
INDUSTRY_COLUMNS = (
    "symbol",
    "code",
    "name",
    "board_code",
    "board_name",
    "board_change_pct",
    "price",
    "change_pct",
    "turnover_rate",
    "pe",
    "pb",
)


def industry_members_path(data_dir: Path) -> Path:
    return Path(data_dir) / INDUSTRY_DIR / INDUSTRY_FILE


def _fetch_via_provider() -> dict | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供行业抓取, 用它拉。

    返回 ``{"rows", "meta", "errors"}``; 未命中(当前无可用数据源)时返回 None。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "fetch_industry_members"):
        return None
    try:
        result = provider.fetch_industry_members() or {}
    except Exception as e:
        logger.warning("provider %s fetch_industry_members 失败: %s", provider_name, e)
        return None
    logger.info(
        "industry_members via %s: %d rows (meta=%s)",
        provider_name, len(result.get("rows") or []), result.get("meta") or {},
    )
    return result


def _atomic_write_parquet(df: pl.DataFrame, out: Path) -> None:
    """先写 .tmp 再原子替换, 避免进程中断留下半截 parquet 让后续 scan 整条链路报错。"""
    tmp = out.with_name(out.name + ".tmp")
    df.write_parquet(tmp)
    tmp.replace(out)


def industry_members_age_days(data_dir: Path) -> int | None:
    """既有行业表的 ``as_of`` 距今天数; 文件缺失或没有 as_of 列时返回 None。"""
    df = load_industry_members(data_dir)
    if df.is_empty() or "as_of" not in df.columns:
        return None
    latest = df["as_of"].max()
    if latest is None:
        return None
    if isinstance(latest, datetime):  # 写入时若被推断成 Datetime, 归一成 date
        latest = latest.date()
    if not isinstance(latest, date):
        return None
    return (cn_today() - latest).days


def sync_industry_members(data_dir: Path, *, force: bool = False) -> int:
    """全量同步「个股 -> 行业板块」长表 → data/industry_members/industry_members.parquet。

    :param force: 为 True 时忽略新鲜度检查强制重抓; 默认在
        ``INDUSTRY_REFRESH_DAYS`` 天内直接跳过(不产生任何网络请求)。

    返回写入行数; 跳过 / 无可用数据源 / 无数据 / 完整率不达标时返回 0 且
    **不动**既有文件(宁可留旧数据, 也不用残缺或空表覆盖)。
    """
    if not force:
        age = industry_members_age_days(data_dir)
        if age is not None and age < INDUSTRY_REFRESH_DAYS:
            logger.info(
                "industry_members 仍新鲜(as_of %d 天前 < %d 天), 跳过重抓",
                age, INDUSTRY_REFRESH_DAYS,
            )
            return 0

    fetched = _fetch_via_provider()
    if not fetched:
        logger.info("industry_members 无可用数据源, 跳过(保留既有文件)")
        return 0

    rows = fetched.get("rows") or []
    meta = fetched.get("meta") or {}
    errors = fetched.get("errors") or {}
    if not rows:
        logger.info("industry_members 抓取结果为空, 跳过(保留既有文件)")
        return 0

    # 完整率闸门: 缺板块的快照看不出缺口却带当天 as_of, 会被下游当成新鲜完整数据。
    requested = meta.get("boards_requested")
    ok = meta.get("boards_ok")
    if isinstance(requested, int) and isinstance(ok, int) and requested > 0:
        ratio = ok / requested
        if ratio < INDUSTRY_MIN_COVERAGE:
            logger.warning(
                "industry_members 板块完整率 %.1f%% (%d/%d) 低于阈值 %.0f%%, 不覆盖旧表; 失败样例: %s",
                ratio * 100, ok, requested, INDUSTRY_MIN_COVERAGE * 100, list(errors)[:5],
            )
            return 0
    else:
        # 老版本桥接不回报 meta → 无法校验。仍然落盘(否则功能不可用), 但明确留痕。
        logger.warning(
            "industry_members 无板块完整率元数据(boards_requested=%r, boards_ok=%r), 跳过校验直接落盘",
            requested, ok,
        )

    df = pl.DataFrame(rows)
    missing = [c for c in ("symbol", "board_code") if c not in df.columns]
    if missing:
        logger.warning("industry_members 上游行缺关键列 %s, 跳过落盘", missing)
        return 0

    # 上游偶有重复行; 同一 (symbol, board_code) 只留最后一次。
    df = df.unique(subset=["symbol", "board_code"], keep="last")
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))
    ordered = [c for c in INDUSTRY_COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in ordered and c != "as_of"]
    df = df.select([*ordered, *extra, "as_of"]).sort(["board_code", "symbol"])

    out = industry_members_path(data_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(df, out)

    logger.info(
        "industry_members synced: %d rows / %d boards / %d symbols → %s",
        df.height,
        df["board_code"].n_unique(),
        df["symbol"].n_unique(),
        out,
    )
    return df.height


def load_industry_members(data_dir: Path) -> pl.DataFrame:
    """读取行业归属长表; 文件缺失时返回空 DataFrame(不抛异常)。

    返回的是**长表**: 一只票可能有多行(东财板块分层)。需要唯一行业时由调用方决定
    聚合口径, 例如按 ``board_code`` 稳定排序取首行, 或先选定一层板块再 join。
    """
    path = industry_members_path(data_dir)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("industry_members 读取失败(%s), 视为空表: %s", path, e)
        return pl.DataFrame()
