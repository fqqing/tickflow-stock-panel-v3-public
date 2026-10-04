"""B2 命令行版: 选股结果推送飞书多维表格。

面板上线后**常用路径是前端**: 策略页「推飞书」按钮选策略 + 选日期即时推
(走 /api/lark/push)。本脚本保留给命令行 / 计划任务 / 调试。

记录映射与推送实现统一在 ``app.services.lark_screener`` —— 本文件只是薄壳:
  - 取数走 HTTP (脚本是独立进程, 拿不到后端内存里的 repo / strategy_engine);
  - 拿到 rows 之后全部复用 service, 口径与面板严格一致。

用法(盘后跑, 后端需在运行)::

    python backend/scripts/push_screener_to_lark.py                 # 全部已配置的表
    python backend/scripts/push_screener_to_lark.py --dry-run       # 只看记录不推送
    python backend/scripts/push_screener_to_lark.py --only trend_dragon,bottom_structure
    python backend/scripts/push_screener_to_lark.py --as-of 2026-09-30 --force
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

# 直接 python 跑脚本时 backend 不在 sys.path, 补上以 import app.services
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import lark_bitable as lb
from app.services.lark_screener import (  # noqa: F401  (re-export, 单测按名字取)
    _LIMIT_UP_BY_BOARD,
    STRATEGY_TABLES,
    _abnormal_records,
    build_records,
    push_records_for,
)

__all__ = ["STRATEGY_TABLES", "_abnormal_records", "build_records", "main", "run"]

logger = logging.getLogger("push_screener_to_lark")

_KEY_FIELDS = ("代码", "信号日期")
_DATE_FIELDS = ("信号日期",)

# 本机调用必须绕过代理: 沙箱/终端常设 HTTP_PROXY, 后端重启间隙代理会返 502
# 而不是连接拒绝, 且本机流量没必要出站绕一圈
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, payload: dict | None = None, timeout: int = 300) -> dict:
    """POST/GET 本地后端, 返回解析后的 JSON。失败抛 RuntimeError。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OSError as e:
        raise RuntimeError(f"后端请求失败 {url}: {e}") from e


def _fetch_strategy_rows(backend: str, strategy_id: str, as_of: str | None) -> tuple[str, list[dict]]:
    """调 /api/screener/run_preset, 返回 (as_of, rows)。"""
    payload: dict = {"strategy_id": strategy_id, "asset_type": "stock", "market": "cn"}
    if as_of:
        payload["as_of"] = as_of
    resp = _http_json(f"{backend}/api/screener/run_preset", payload)
    return str(resp.get("as_of") or ""), resp.get("rows") or []


def _fetch_abnormal_rows(backend: str, min_closeness: float = 0.7) -> tuple[str, list[dict]]:
    """调 /api/abnormal/overview, 返回 (as_of, rows)。

    行结构: symbol / name / board / close / windows{3d,10d,30d:{value,threshold,closeness}}
    / max_closeness / status。value 是「N日累计涨跌幅偏离值」(小数)。

    min_closeness 是「接近度」下限 (|偏离|/阈值): 0.5 观察 / 0.7 边缘 / 1.0 已触发。
    默认 0.7 —— 0.5 会把「才刚过半程」的一起拉进来, 与该表历史每天几条的量级不符。
    """
    resp = _http_json(
        f"{backend}/api/abnormal/overview?min_closeness={min_closeness}&limit=500", timeout=120
    )
    as_of = str(resp.get("cache_date") or "")[:10]
    return as_of, resp.get("rows") or []


def _fetch_capital_momentum(backend: str, symbol: str) -> float | None:
    """逐只取资金动能 cm_value (最后一根 bar)。失败返回 None, 不阻断主流程。"""
    try:
        resp = _http_json(
            f"{backend}/api/kline/daily?symbol={symbol}&days=10"
            "&indicators=capital_momentum&fields=date,close,cm_value",
            timeout=60,
        )
    except RuntimeError as e:
        logger.warning("  %s 资金动能获取失败: %s", symbol, e)
        return None
    rows = resp.get("rows") or []
    for r in reversed(rows):
        v = r.get("cm_value")
        if v is not None:
            return lb.to_num(v)
    return None


def run(args: argparse.Namespace) -> int:
    backend = args.backend.rstrip("/")
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else list(STRATEGY_TABLES)
    unknown = [s for s in only if s not in STRATEGY_TABLES]
    if unknown:
        logger.error("未知策略: %s (可选: %s)", unknown, list(STRATEGY_TABLES))
        return 2

    total_pushed = 0
    exit_code = 0
    for sid in only:
        cfg = STRATEGY_TABLES[sid]
        label = cfg["label"]
        if not cfg.get("base_token") or not cfg.get("table_id"):
            logger.warning(
                "[%s] 飞书表未配置(base_token/table_id 为空), 跳过 —— "
                "请在 app/services/lark_screener.py 的 STRATEGY_TABLES 补上该表", label,
            )
            continue
        try:
            if sid == "abnormal":
                as_of, rows = _fetch_abnormal_rows(
                    backend, getattr(args, "abnormal_min_closeness", 0.7)
                )
            else:
                as_of, rows = _fetch_strategy_rows(backend, sid, args.as_of)
        except RuntimeError as e:
            logger.error("[%s] %s", label, e)
            exit_code = 1
            continue
        logger.info("[%s] as_of=%s 选中 %d 只", label, as_of, len(rows))
        if not rows:
            continue

        momentum_map: dict[str, float | None] | None = None
        if sid == "trend_dragon" and not args.no_enrich:
            momentum_map = {}
            for r in rows:
                symbol = str(r.get("symbol") or "")
                momentum_map[symbol] = _fetch_capital_momentum(backend, symbol)

        records = build_records(sid, rows, as_of, momentum_map)
        if args.dry_run:
            logger.info("[%s] DRY RUN 记录样例: %s", label, json.dumps(records[:3], ensure_ascii=False))
            continue

        result = push_records_for(sid, records, force=args.force)
        for d in result.details:
            logger.info("[%s] %s", label, d)
        if not result.ok:
            logger.error("[%s] 推送失败: %s", label, result.error)
            exit_code = 1
            continue
        logger.info("[%s] 推送 %d 条 (去重跳过 %d)", label, result.pushed, result.skipped)
        total_pushed += result.pushed

    logger.info("=== 完成: 共推送 %d 条 ===", total_pushed)
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description="选股结果推送飞书多维表格 (命令行版)")
    p.add_argument("--backend", default="http://127.0.0.1:3018", help="后端地址 (默认本机 3018)")
    p.add_argument("--only", default=None, help="只跑指定策略, 逗号分隔 (默认全部)")
    p.add_argument("--as-of", default=None, help="指定交易日 YYYY-MM-DD (默认最新)")
    p.add_argument("--dry-run", action="store_true", help="只打印记录不推送")
    p.add_argument("--force", action="store_true", help="跳过去重, 强制全量推送")
    p.add_argument("--no-enrich", action="store_true", help="不逐只补资金动能 (更快)")
    p.add_argument(
        "--abnormal-min-closeness", type=float, default=0.7,
        help="异动接近度下限 0.5/0.7/1.0 (观察/边缘/已触发), 默认 0.7",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
