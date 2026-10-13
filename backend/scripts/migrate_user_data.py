"""一次性数据迁移: user_data 根下的个人数据文件 → user_data/admin/。

用于单用户 → 多用户升级。迁移前自动备份整个 user_data 目录(含 auth.json/
secrets.json), 幂等可重复运行(目标已存在则跳过)。

用法:
    python scripts/migrate_user_data.py [user_data_dir]
默认 user_data_dir = <项目根>/data/user_data
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

# 按用户隔离的个人数据文件(移入 admin/)
PERSONAL_FILES = [
    "watchlist.parquet",
    "watchlist_groups.json",
    "preferences.json",
    "ai_reports.json",
    "ai_stock_reports.json",
    "ai_market_recaps.json",
    "alerts.jsonl",
    "last_backtest_trades.json",
    "research_candidates.json",
]


def main() -> None:
    if len(sys.argv) > 1:
        user_data = Path(sys.argv[1])
    else:
        user_data = Path(__file__).resolve().parent.parent.parent / "data" / "user_data"

    if not user_data.exists():
        print(f"[跳过] user_data 不存在: {user_data}")
        return

    admin = user_data / "admin"

    # 1. 全量备份(含 auth.json / secrets.json / 目录), 失败则中止不迁移
    backup = user_data.parent / f"user_data_backup_{int(time.time())}"
    shutil.copytree(user_data, backup)
    print(f"[备份] {user_data.name} -> {backup.name}")

    # 2. 迁移
    admin.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for f in PERSONAL_FILES:
        src = user_data / f
        dst = admin / f
        if not src.exists():
            continue
        if dst.exists():
            print(f"[跳过] 已迁移: {f}")
            continue
        shutil.move(str(src), str(dst))
        moved.append(f)

    print(f"\n[完成] 迁移 {len(moved)} 个文件 -> admin/:")
    for f in moved:
        print(f"  ✓ {f}")


if __name__ == "__main__":
    main()
