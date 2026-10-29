"""每日精选历史重跑 + 飞书更新（新逻辑）。

把飞书「每日精选」表里的旧逻辑数据（10-19~10-29，每天 5 只、无买入说明、日线近似结算）
替换为新逻辑数据（top_n=6 + min_family_count=3 + 利空过滤 + 分钟级结算 + 买入说明）。

流程:
  1. 逐日 run(as_of, push_lark=False) 重跑 -> 落盘 picks.parquet
  2. settle_pending() 分钟级结算 -> 落盘
  3. --apply 时: 删飞书旧记录(日期范围) -> push_rows_to_lark 推全部新记录

用法（v1 venv 的 python + PYTHONPATH 指向 v3 backend）:
    cd D:/project/GP/tickflow-stock-panel-v3
    D:/project/GP/tickflow-stock-panel/backend/.venv/Scripts/python.exe \
        scripts/backfill_daily_picks.py --start 2026-10-19 --end 2026-10-28
    # 实际删除+推送飞书
    ... --apply
    # 关闭利空过滤(0b)时
    ... --no-bearish
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.services.daily_picks import (  # noqa: E402
    LARK_BASE_TOKEN,
    LARK_TABLE_ID,
    DailyPicksService,
)
from app.services.screener import ScreenerService  # noqa: E402
from app.strategy import config as strategy_config  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backfill_daily_picks")
for noisy in ("app.tickflow.repository", "app.services.screener", "app.strategy.engine",
              "app.services.announcement_filter"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_bootstrap(data_dir: Path):
    """按 main.py 的最小口径构建 repo + engine + svc。"""
    store = DataStore(data_dir)
    repo = KlineRepository(store)
    # 生产启动同款：先建 instruments 缓存再算 enriched 全量指标（~40s）
    repo.refresh_cache(background=False)
    strategy_dirs = [
        ROOT / "backend" / "app" / "strategy" / "builtin",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
        data_dir / "strategies" / "composite",
    ]
    engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    svc = ScreenerService(repo, asset_type="stock", market="cn")
    return store, repo, engine, svc


def _lark_existing(records: list[dict], start: str, end: str) -> tuple[list[str], list[dict]]:
    """把飞书表记录按「选股日期在 [start, end]」拆成 (要删的 record_id 列表, 保留的记录)。"""
    del_ids: list[str] = []
    keep: list[dict] = []
    for r in records:
        d = str(r.get("选股日期") or "")[:10]
        rid = r.get("record_id")
        if start <= d <= end and rid:
            del_ids.append(rid)
        else:
            keep.append(r)
    return del_ids, keep


def main() -> int:
    p = argparse.ArgumentParser(description="每日精选历史重跑 + 飞书更新")
    p.add_argument("--start", required=True, help="起始交易日 YYYY-MM-DD")
    p.add_argument("--end", required=True, help="结束交易日 YYYY-MM-DD")
    p.add_argument("--data-dir", default=str(ROOT / "data"), help="数据目录")
    p.add_argument("--top-n", type=int, default=6)
    p.add_argument("--min-family-count", type=int, default=3)
    p.add_argument("--no-bearish", action="store_true", help="关闭利空过滤(0b)")
    p.add_argument("--apply", action="store_true", help="实际删除+推送飞书（默认只本地重跑）")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    store, repo, engine, _svc = _build_bootstrap(data_dir)
    svc = DailyPicksService(repo, engine)

    # 阻止 settle_pending 内部自动回写飞书，飞书操作由本脚本显式控制
    svc.sync_settlement_to_lark = lambda: {"ok": True, "synced": 0, "error": None}

    # 交易日序列（升序），过滤 [start, end]
    dates = [d for d in svc._trading_dates() if args.start <= d <= args.end]
    if not dates:
        logger.error("范围内无交易日: %s ~ %s", args.start, args.end)
        return 1
    logger.info("待重跑 %d 个交易日: %s ~ %s", len(dates), dates[0], dates[-1])

    bearish = not args.no_bearish
    total_rows = 0
    for d in dates:
        rows = svc.run(
            as_of=date.fromisoformat(d),
            top_n=args.top_n,
            min_family_count=args.min_family_count,
            push_lark=False,
            bearish_filter=bearish,
        )
        total_rows += len(rows)
        logger.info("  %s -> %d 只", d, len(rows))

    # 结算（0a 分钟级 + P2 买入说明）
    settled = svc.settle_pending()
    logger.info("结算 %d 条", settled)

    # 本地结果摘要
    df = svc._load()
    df = df.filter(pl.col("pick_date").is_in(dates))
    logger.info("本地落盘: %s (%d 行)", svc.picks_path, df.height)
    if not df.is_empty():
        n_hit = int(df.filter(pl.col("hit") == True).height)  # noqa: E712
        n_all = int(df.filter(pl.col("pnl_pct").is_not_null()).height)
        n_note = int(df.filter(pl.col("entry_note").is_not_null()).height)
        logger.info("  已结算 %d 条(命中 %d), 有买入说明 %d 条", n_all, n_hit, n_note)

    if not args.apply:
        logger.info("dry-run 完成，未动飞书。确认无误后加 --apply 执行删除+推送。")
        return 0

    # ── 飞书：删旧推新 ──
    from app.services import lark_bitable as lb

    records = lb.list_records(LARK_BASE_TOKEN, LARK_TABLE_ID, ["代码", "选股日期"])
    if records is None:
        logger.error("读飞书表失败，中止飞书更新（本地已落盘）")
        return 2
    del_ids, _keep = _lark_existing(records, args.start, args.end)
    logger.info("飞书表现有 %d 条，日期范围内待删 %d 条", len(records), len(del_ids))

    if del_ids:
        res = lb.delete_records(LARK_BASE_TOKEN, LARK_TABLE_ID, del_ids)
        if not res.ok:
            logger.error("删除飞书旧记录失败: %s", res.error)
            return 2
        logger.info("已删除旧记录 %d 条", res.pushed)

    # 推全部新记录（含结算字段 + 买入说明）。
    # 飞书删除后有一致性延迟，push 去重可能读到未生效的旧 key 误 skip，故循环重试到补齐（push 幂等）。
    import time as _time

    rows = df.to_dicts() if not df.is_empty() else []
    push = {"ok": False, "pushed": 0, "skipped": len(rows), "error": None}
    for attempt in range(1, 5):
        push = svc.push_rows_to_lark(rows)
        logger.info("推送尝试 %d: pushed=%d skipped=%d err=%s", attempt,
                    push.get("pushed"), push.get("skipped"), push.get("error"))
        if not push.get("ok") or (push.get("skipped") or 0) == 0:
            break
        _time.sleep(4)
    return 0 if push.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
