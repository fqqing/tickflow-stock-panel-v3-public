"""数据分区体检：逐分区统计文件数、行数、schema，输出 Markdown 报告。

用法:
    .venv/Scripts/python.exe scripts/healthcheck_data.py [--out report.md]
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

VOL_REF_COL = "volume"

# 报告里逐分区表格只列最近多少个分区(全量 994 行没人看, 头部用汇总代替)
TAIL_N = 20


def _dates(ds: Path) -> list[str]:
    if not ds.exists():
        return []
    return sorted(p.name.split("=", 1)[1] for p in ds.iterdir() if p.is_dir() and p.name.startswith("date="))


def _files(d: Path) -> list[Path]:
    return [p for p in d.iterdir() if p.suffix == ".parquet"]


def _schema_of(path: Path) -> pl.Schema:
    return pl.scan_parquet(path).collect_schema()


def scan_dataset(name: str, ds: Path, sample_cols: list[str]) -> list[dict]:
    rows: list[dict] = []
    for dt in _dates(ds):
        d = ds / f"date={dt}"
        fs = _files(d)
        size = sum(f.stat().st_size for f in fs)
        rec: dict = {
            "date": dt,
            "files": len(fs),
            "mb": round(size / 1024 / 1024, 2),
        }
        if not fs:
            rows.append(rec)
            continue
        try:
            sch = _schema_of(fs[0])
            rec["cols"] = len(sch)
            dtcol = next((c for c in ("datetime", "trade_date", "date") if c in sch), None)
            rec["dtcol"] = dtcol
            if dtcol:
                rec["dttype"] = str(sch[dtcol])
            lf = pl.scan_parquet(d)
            cols = [c for c in sample_cols if c in lf.collect_schema()]
            agg = [pl.len().alias("n")]
            if "symbol" in lf.collect_schema():
                agg.append(pl.col("symbol").n_unique().alias("symbols"))
            for c in cols:
                agg.append(pl.col(c).median().alias(f"med_{c}"))
            st = lf.select(agg).collect().to_dicts()[0]
            rec["rows"] = st.get("n")
            rec["symbols"] = st.get("symbols")
            for c in cols:
                rec[f"med_{c}"] = round(float(st[f"med_{c}"]), 4) if st.get(f"med_{c}") is not None else None
        except Exception as exc:  # noqa: BLE001
            rec["err"] = f"{type(exc).__name__}: {exc}"[:120]
        rows.append(rec)
    return rows


def _cn_counts(ds: Path) -> list[tuple[str, int]]:
    """每个分区的 A 股行数 (.SH/.SZ/.BJ)。

    只统计 A 股: 港美股交易日历不同, 混在一起会把 A 股假期误报成缺口。
    """
    out: list[tuple[str, int]] = []
    for dt in _dates(ds):
        try:
            n = (
                pl.scan_parquet(ds / f"date={dt}")
                .filter(pl.col("symbol").str.contains(r"\.(SH|SZ|BJ)$"))
                .select(pl.len())
                .collect()
                .item()
            )
        except Exception:  # noqa: BLE001
            n = 0
        out.append((dt, int(n or 0)))
    return out


def _sparse(pairs: list[tuple[str, int]], window: int = 3, threshold: float = 0.5) -> list[tuple[str, int, int]]:
    """找出行数显著低于邻近分区的日期 (历史内部空洞)。

    基准取前后各 window 个分区行数的中位数 —— 不能用全集中位数, 标的池
    随时间增长(2022 年约 4700 只, 现在约 5500 只), 用全集中位数会误报早期分区。
    """
    out: list[tuple[str, int, int]] = []
    for i, (dt, n) in enumerate(pairs):
        neighbours = [v for _, v in pairs[max(0, i - window):i]] + [v for _, v in pairs[i + 1:i + 1 + window]]
        if len(neighbours) < 2:
            continue
        base = statistics.median(neighbours)
        if base > 0 and n < base * threshold:
            out.append((dt, n, int(base)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    datasets = {
        "kline_daily": ["volume", "close", "amount"],
        "kline_daily_enriched": ["volume", "close", "raw_close"],
        "kline_minute": ["volume", "close"],
    }

    lines: list[str] = ["# 数据分区体检", ""]
    for name, cols in datasets.items():
        ds = DATA / name
        rows = scan_dataset(name, ds, cols)
        if not rows:
            lines.append(f"## {name}\n\n(无分区)\n")
            continue
        lines.append(f"## {name}  ({len(rows)} 个分区)\n")
        total_rows = sum(r.get("rows") or 0 for r in rows)
        total_mb = sum(r.get("mb") or 0 for r in rows)
        errs = [(r["date"], r["err"]) for r in rows if r.get("err")]
        lines.append(
            f"- 日期范围 **{rows[0]['date']} ~ {rows[-1]['date']}**"
            f"，共 {len(rows)} 个分区、{total_rows:,} 行、{total_mb:,.0f} MB\n"
        )
        if errs:
            lines.append(f"- ⚠️ 读取失败 {len(errs)} 个分区: "
                         + ", ".join(d for d, _ in errs[:10]) + "\n")
        # 表格只列最近 TAIL_N 个分区(全量 994 行没人看), 头部信息已在上面汇总
        tail = rows[-TAIL_N:]
        keys = ["date", "files", "symbols", "rows", "mb", "cols", "dtcol", "dttype", "med_volume", "med_close", "med_amount", "med_raw_close", "err"]
        keys = [k for k in keys if any(k in r for r in tail)]
        lines.append(f"最近 {len(tail)} 个分区:\n")
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("|" + "|".join("---" for _ in keys) + "|")
        for r in tail:
            lines.append("| " + " | ".join("" if r.get(k) is None else str(r.get(k)) for k in keys) + " |")
        lines.append("")

    # 相邻交易日 volume 比值：检测半成品分区
    lines.append("## 相邻交易日 volume 中位数比值（<0.5 疑似半成品）\n")
    ds = DATA / "kline_daily"
    dates = _dates(ds)
    stats: dict[str, float] = {}
    for dt in dates:
        try:
            v = pl.scan_parquet(ds / f"date={dt}").select(pl.col("volume").median()).collect().item()
            stats[dt] = float(v) if v is not None else 0.0
        except Exception:  # noqa: BLE001
            stats[dt] = 0.0
    lines.append("| 日期 | volume 中位数 | 与前一日比值 |")
    lines.append("|---|---|---|")
    prev_v: float | None = None
    for dt in dates:
        v = stats[dt]
        ratio = (v / prev_v) if (prev_v and prev_v > 0) else None
        flag = " ⚠️" if (ratio is not None and ratio < 0.5) else ""
        lines.append(f"| {dt} | {v:,.0f} | {'' if ratio is None else f'{ratio:.3f}{flag}'} |")
        prev_v = v
    lines.append("")

    # 稀疏分区：A 股行数远低于邻近交易日 —— 历史内部空洞。
    # scan_recent_integrity 扫不到这类(只看最近 7 天且只报尾部缺口)。
    # 只统计 A 股后缀: 港美股交易日历与 A 股不同, 混在一起会把 A 股假期
    # 误报成缺口 (2026-06-19 端午: 分区只剩 8 只美股, A 股休市属正常)。
    lines.append("## 稀疏分区（A 股行数 < 邻近交易日中位数 50%，疑似历史缺口）\n")
    cn_pairs = _cn_counts(DATA / "kline_daily")
    cn_trade_days = [(dt, n) for dt, n in cn_pairs if n > 0]
    bad = _sparse(cn_trade_days)
    if bad:
        lines.append("| 日期 | A 股行数 | 邻近交易日中位数 |")
        lines.append("|---|---|---|")
        for dt, n, base in bad:
            lines.append(f"| {dt} | {n:,} | {base:,} |")
    else:
        lines.append("(未发现)\n")

    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
