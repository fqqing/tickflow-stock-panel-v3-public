"""离线重建 enriched 分区 —— 后端进程不可用时的修复入口。

用法:
    .venv/Scripts/python.exe scripts/rebuild_enriched.py            # 增量(只补缺失日期)
    .venv/Scripts/python.exe scripts/rebuild_enriched.py full       # 全量重写

为什么需要它: 数据修复管道(repair_daily)会先 prune 掉 start_date 之后的
enriched 分区再重算; 若重算过程中后端进程被终止, 这些分区就处于"已删未建"的
中间态。此时不必依赖后端活着, 直接调 pipeline 补回即可。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))

from app.indicators.pipeline import run_pipeline  # noqa: E402


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    full = mode == "full"
    print(f"rebuild enriched: mode={'full' if full else 'incremental'}")
    rows = run_pipeline(new_dates_only=not full)
    print(f"written rows: {rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
