"""扫描结果的定位 / 读取 / 筛选.

产出的 parquet 由 :mod:`scripts.scan_market` 离线生成, 这里只负责读。刻意不把
「触发扫描」也塞进来: 扫描是分钟级的重活, 读是毫秒级的轻活, 混在一起会让
每次筛选都背上不必要的锁与状态。

缓存策略: parquet 一天才变一次, 按 (路径, mtime, size) 缓存 DataFrame。改文件
立刻生效, 不用重启服务。
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

SCAN_DIR = Path(__file__).resolve().parents[2] / "data" / "scan"

# 路径 -> (mtime, size, frame)
_FRAME_CACHE: dict[Path, tuple[float, int, pl.DataFrame]] = {}

# 路径 -> (mtime, size, [symbol, name, ...])
_SYMBOLS_CACHE: dict[Path, tuple[float, int, list[dict]]] = {}

# 允许排序的列. 直接用前端传来的列名拼 sort 会被注入任意表达式, 白名单最省心
SORTABLE = (
    "symbol", "chg_pct", "buy_ago", "buy_gain", "gain_pct", "bsp_ago",
    "bi_pct", "bi_bars", "zs_dist", "zs_ago", "div_ratio", "close",
)

# 买点类型: 上游 BSP_TYPE 的取值。卖点共用同一套类型名, 靠 *_side 区分方向
BSP_TYPES = ("1", "1p", "2", "2s", "3a", "3b")


def scan_files() -> list[dict]:
    """列出所有扫描产物, 按生成时间倒序. 损坏的 meta 跳过而不是报错."""
    out: list[dict] = []
    for path in sorted(SCAN_DIR.glob("scan_*.parquet"), reverse=True):
        meta_path = path.with_suffix(".meta.json")
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        out.append({
            "file": path.name,
            "level": meta.get("level", ""),
            "profile": meta.get("profile", ""),
            "lookback": meta.get("lookback", 0),
            "scanned": meta.get("scanned", 0),
            "generated_at": meta.get("generated_at", ""),
            "mtime": path.stat().st_mtime,
        })
    return out


def resolve_file(
    file: str = "", level: str = "1d", profile: str = "v1", lookback: int = 500,
) -> tuple[Path | None, bool]:
    """定位扫描文件, 返回 ``(路径, 级别是否命中)``.

    匹配优先级: level+profile+lookback 全中 > level+profile 中(取最新) > 最新文件。
    ``matched`` 只看 **level+profile**, 不看 lookback —— 级别对不对是正确性问题
    (拿日线冒充 30m 是灾难), 而 lookback 只是窗口长短, 数据依然有效。

    ⚠️ 必须把「没命中」带出去: 只跑了日线扫描、前端却请求 30m 时, 静默回退会让
    页面上明明写着「30m」实际画的是日线的笔, 这种错最难查。
    """
    if file:
        path = SCAN_DIR / Path(file).name  # 只取文件名, 挡掉 ../ 之类的穿越
        return (path, True) if path.is_file() else (None, False)

    files = scan_files()
    if not files:
        return None, False

    same_level = [c for c in files if c["level"] == level and c["profile"] == profile]
    if not same_level:
        return SCAN_DIR / files[0]["file"], False

    exact = next((c for c in same_level if c["lookback"] == lookback), None)
    return SCAN_DIR / (exact or same_level[0])["file"], True


def load_frame(path: Path) -> pl.DataFrame:
    """读扫描结果, 按 mtime+size 缓存."""
    stat = path.stat()
    hit = _FRAME_CACHE.get(path)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]

    frame = pl.read_parquet(path)
    if len(_FRAME_CACHE) > 8:  # 扫描文件就那么几个, 超了直接清, 不做 LRU
        _FRAME_CACHE.clear()
    _FRAME_CACHE[path] = (stat.st_mtime, stat.st_size, frame)
    return frame


def scan_meta(path: Path, wanted_level: str = "", matched: bool = True) -> dict:
    """读扫描元信息. 没有 meta 文件时用 frame 的行数兜底."""
    meta_path = path.with_suffix(".meta.json")
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    meta.setdefault("file", path.name)
    meta.setdefault("scanned", load_frame(path).height)
    # 前端靠这两项提示「你要的级别没扫过, 现在看的是回退文件」
    meta["requested_level"] = wanted_level
    meta["matched"] = matched

    # 数据截止日: 流水线每天跑一次, 页面上必须能一眼看出**今天跑没跑**。
    # 只看 generated_at 是不够的 —— 流水线可以在没有新数据的日子重跑,
    # 时间戳很新、内容却是上周的。真正能证明新鲜度的是数据本身的最后一根 K 线。
    try:
        newest = load_frame(path)["last_date"].max()
    except Exception:  # 列缺失/类型异常都不该让接口挂掉
        newest = None
    meta["data_last_date"] = str(newest)[:16] if newest else ""
    return meta


def _type_mask(frame: pl.DataFrame, column: str, types: list[str]) -> pl.Expr:
    """列里含任意一个类型. 类型是竖线分隔的串, 按段精确匹配避免 1 命中 1p."""
    expr = pl.lit(False)
    for t in types:
        parts = pl.col(column).fill_null("").str.split("|").list.eval(pl.element() == t)
        expr = expr | parts.list.any()
    return expr


def query(
    frame: pl.DataFrame,
    *,
    types: list[str] | None = None,
    match: str = "last",
    ago_max: int | None = None,
    zs_pos: str = "",
    diverge: str = "",
    seg_dir: str = "",
    bi_dir: str = "",
    keyword: str = "",
    sort: str = "buy_ago",
    desc: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """在扫描结果上做筛选 + 排序 + 分页.

    ``match``:
        ``last``    只认最近那一个买点 (``buy_type``), 与 ``ago_max`` 口径一致
        ``window``  认回溯窗口内出现过的任意买点 (``recent_buy``), 更宽松
    """
    df = apply_filters(
        frame,
        types=types,
        match=match,
        ago_max=ago_max,
        zs_pos=zs_pos,
        diverge=diverge,
        seg_dir=seg_dir,
        bi_dir=bi_dir,
        keyword=keyword,
    )

    total = df.height
    if total == 0:
        return 0, []

    key = sort if sort in SORTABLE else "buy_ago"
    # null 排在最后, 否则「没有买点」的票会霸占升序的第一页
    df = df.sort(key, descending=desc, nulls_last=True)
    df = df.slice(offset, max(0, limit))
    return total, df.to_dicts()


def apply_filters(
    frame: pl.DataFrame,
    *,
    types: list[str] | None = None,
    match: str = "last",
    ago_max: int | None = None,
    zs_pos: str = "",
    diverge: str = "",
    seg_dir: str = "",
    bi_dir: str = "",
    keyword: str = "",
) -> pl.DataFrame:
    """只做筛选不做排序分页, 供单级别查询与多级别共振共用."""
    df = frame

    if keyword:
        kw = keyword.strip().upper()
        df = df.filter(
            pl.col("symbol").str.to_uppercase().str.contains(kw, literal=True)
            | pl.col("name").fill_null("").str.contains(keyword.strip(), literal=True)
        )

    if types:
        column = "recent_buy" if match == "window" else "buy_type"
        df = df.filter(_type_mask(df, column, types))

    if ago_max is not None:
        # 只按最近买点的距离过滤。窗口模式下用 recent_buy 选类型、用 buy_ago 卡距离
        # 会出现「类型命中但距离不符」的错觉, 所以距离一律以最近买点为准
        df = df.filter(pl.col("buy_ago").is_not_null() & (pl.col("buy_ago") <= ago_max))

    if zs_pos:
        df = df.filter(pl.col("zs_pos") == zs_pos)
    if diverge:
        df = df.filter(pl.col("diverge") == diverge)
    if seg_dir:
        df = df.filter(pl.col("seg_dir") == seg_dir)
    if bi_dir:
        df = df.filter(pl.col("bi_dir") == bi_dir)
    return df


def symbols_index(path: Path) -> list[dict]:
    """从扫描产物提取 (symbol, name) 索引, 按 mtime+size 缓存.

    标的搜索需要一份全市场代码表, 而扫描产物里已经有现成的 symbol+name。
    刻意不在启动时全量读日线库去拼: 那要扫 5000 多个小文件。
    """
    stat = path.stat()
    hit = _SYMBOLS_CACHE.get(path)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]

    frame = load_frame(path)
    if "symbol" not in frame.columns:
        rows: list[dict] = []
    else:
        has_name = "name" in frame.columns
        cols = ["symbol", "name"] if has_name else ["symbol"]
        rows = (
            frame.select(cols)
            .unique(subset=["symbol"], keep="first")
            .sort("symbol")
            .to_dicts()
        )
        if not has_name:
            for r in rows:
                r["name"] = None

    if len(_SYMBOLS_CACHE) > 8:
        _SYMBOLS_CACHE.clear()
    _SYMBOLS_CACHE[path] = (stat.st_mtime, stat.st_size, rows)
    return rows


def search_symbols(path: Path, keyword: str, limit: int = 20) -> list[dict]:
    """代码前缀 / 代码包含 / 名称包含, 三档优先级取前 limit 条.

    纯 Python 过滤: 全市场 5000 多条, 一次线性扫描远快于为此建索引。
    """
    rows = symbols_index(path)
    kw = keyword.strip()
    if not kw:
        return rows[:limit]

    upper = kw.upper()
    lower = kw.lower()
    prefix: list[dict] = []
    contains: list[dict] = []
    by_name: list[dict] = []
    for r in rows:
        sym = str(r["symbol"]).upper()
        name = (r.get("name") or "").lower()
        if sym.startswith(upper):
            prefix.append(r)
        elif upper in sym:
            contains.append(r)
        elif lower in name:
            by_name.append(r)
        if len(prefix) >= limit:
            break
    return (prefix + contains + by_name)[:limit]


def resonance(
    primary: pl.DataFrame,
    secondary: pl.DataFrame,
    *,
    primary_filters: dict,
    secondary_filters: dict,
    sort: str = "s_buy_ago",
    desc: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """两个级别的内连接筛选: 「大级别定方向, 小级别找买点」.

    次级别的非 symbol 列统一加 ``s_`` 前缀再 join。之所以不直接用 polars 的
    ``suffix``: 主键级保留原名、次级别加前缀, 前端要按前缀区分两侧字段,
    ``suffix`` 只能在冲突列上加, 会导致两侧字段名不一致(有的带后缀有的不带)。
    """
    p = apply_filters(primary, **primary_filters)
    s = apply_filters(secondary, **secondary_filters)
    s = s.rename({c: f"s_{c}" for c in s.columns if c != "symbol"})

    joined = p.join(s, on="symbol", how="inner")
    total = joined.height
    if total == 0:
        return 0, []

    # 可排序列: 主键级原名 + 次级别带 s_ 前缀
    allowed = set(SORTABLE) | {f"s_{c}" for c in SORTABLE}
    key = sort if sort in allowed else "s_buy_ago"
    if key not in joined.columns:
        key = "s_buy_ago"
    joined = joined.sort(key, descending=desc, nulls_last=True)
    joined = joined.slice(offset, max(0, limit))
    return total, joined.to_dicts()
