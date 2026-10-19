"""每日精选(纸面跟踪)服务。

把项目已有的「策略引擎 + 多因子评分 + 选股」串成一条每日荐股链路:

  盘后跑 27 个内置策略 → 聚合多策略共振命中的股票 → 按共振数/评分排序
  → 过滤 ST/退市/一字涨停/跌停/停牌 → 取 Top N 落盘快照。

「命中率」采用**纸面跟踪(paper trading)**而非重历史回放:
  - 选股当日收盘价记为 entry_close(买入参考价);
  - 持有 hold_days 个交易日后, 取到期日收盘价 exit_close;
  - pnl_pct = (exit - entry) / entry, pnl_pct > 0 即「命中」;
  - 累计统计: 命中率 / 平均收益 / 中位收益 / 强命中(≥2%) / 止损(≤-3%)。

数据落盘: ``<data_dir>/daily_picks/picks.parquet`` 单文件(每天仅 3~5 行, 无需分区)。
"""
from __future__ import annotations

import json
import logging
import math
from bisect import bisect_right
from datetime import date, datetime

import polars as pl

from app.tickflow.repository import enriched_dirname

logger = logging.getLogger(__name__)

#: 命中分层阈值(小数, 与 change_pct 约定一致: 0.02 = 2%)
_STRONG_HIT_PCT = 0.02   # 强命中: 持有期收益 ≥ +2%
_STOP_LOSS_PCT = -0.03   # 止损: 持有期收益 ≤ -3%

#: 次新股阈值: 上市不满 N 个自然日即剔除(默认 60 天 ≈ 覆盖新股无涨跌幅限制
#: + 连续一字板阶段, 且技术指标/缠论结构缺足够历史数据)。
_NEW_LISTING_DAYS = 60

#: picks.parquet 列顺序(固定, 保证空表时 schema 稳定)
_PICK_COLUMNS = [
    "pick_date", "settle_date", "symbol", "name", "rank",
    "signal_count", "score", "strategy_hits",
    "entry_close", "exit_close", "pnl_pct", "hit",
    "hold_days", "created_at",
]

#: 飞书多维表格「每日精选」(lark-cli base +base-create 建立, 字段见 _to_lark_records)
LARK_BASE_TOKEN = "WsO0bkz53aNLBqsgYNPcYXY7n1c"
LARK_TABLE_ID = "tblQyGsbRzBWoZCp"
#: 飞书表日期字段(写盘转毫秒时间戳 / 结算回写转 "YYYY-MM-DD HH:MM")
_LARK_DATE_FIELDS = ("选股日期", "结算日期")


def _is_bad_name(name: str | None) -> bool:
    """ST / *ST / 退市整理股(名字含 ST 或 退)。"""
    if not name:
        return False
    up = str(name).upper()
    return "ST" in up or "退" in up


def _to_date(v) -> date | None:
    """把 str / date / datetime 统一转成 date; 无法解析返回 None。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None
class DailyPicksService:
    """每日精选选股 + 纸面命中率结算。"""

    def __init__(self, repo, engine) -> None:
        self.repo = repo
        self.engine = engine
        self.data_dir = repo.store.data_dir
        self.picks_path = self.data_dir / "daily_picks" / "picks.parquet"
        self._enriched_dir = self.data_dir / enriched_dirname("stock", "cn")

    # ── 存储读写 ──────────────────────────────────────────────
    def _load(self) -> pl.DataFrame:
        if not self.picks_path.exists():
            return pl.DataFrame(schema={
                "pick_date": pl.Utf8, "settle_date": pl.Utf8, "symbol": pl.Utf8, "name": pl.Utf8,
                "rank": pl.Int64, "signal_count": pl.Int64, "score": pl.Float64, "strategy_hits": pl.Utf8,
                "entry_close": pl.Float64, "exit_close": pl.Float64, "pnl_pct": pl.Float64, "hit": pl.Boolean,
                "hold_days": pl.Int64, "created_at": pl.Utf8,
            })
        try:
            df = pl.read_parquet(self.picks_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("daily_picks 读取失败: %s", e)
            return pl.DataFrame()
        if df.is_empty():
            return df
        # 规范化类型(parquet 读回后数值列可能被推断为其它类型)
        casts = []
        for col in ("rank", "signal_count", "hold_days"):
            if col in df.columns:
                casts.append(pl.col(col).cast(pl.Int64))
        for col in ("score", "entry_close", "exit_close", "pnl_pct"):
            if col in df.columns:
                casts.append(pl.col(col).cast(pl.Float64))
        if casts:
            df = df.with_columns(casts)
        if "hit" in df.columns:
            df = df.with_columns(pl.col("hit").cast(pl.Boolean))
        return df

    def _save(self, df: pl.DataFrame) -> None:
        if df.is_empty():
            return
        self.picks_path.parent.mkdir(parents=True, exist_ok=True)
        # 固定列序输出, 保证 schema 稳定
        cols = [c for c in _PICK_COLUMNS if c in df.columns]
        df.select(cols).write_parquet(self.picks_path)

    def _trading_dates(self) -> list[str]:
        """enriched 分区目录里的交易日序列(升序)。"""
        try:
            return sorted(
                entry.name[len("date="):]
                for entry in self._enriched_dir.iterdir()
                if entry.is_dir() and entry.name.startswith("date=")
            )
        except OSError:
            return []

    # ── 选股 ──────────────────────────────────────────────────
    def run(
        self,
        as_of: date | None = None,
        top_n: int = 5,
        hold_days: int = 1,
        push_lark: bool = True,
        min_listing_days: int = _NEW_LISTING_DAYS,
    ) -> list[dict]:
        """跑全量策略 → 聚合共振 → 过滤 → 取 Top N → 落盘。返回本次精选明细。

        push_lark=True 时, 落盘后顺带推送到飞书多维表格(失败不阻断选股主流程)。
        min_listing_days: 次新股过滤阈值(自然日), 上市不满该天数的股票剔除; <=0 关闭。
        """
        from app.services.screener import ScreenerService
        from app.strategy import config as strategy_config

        svc = ScreenerService(self.repo, asset_type="stock", market="cn")
        if as_of is None:
            as_of = svc.latest_date()
        if not as_of:
            logger.warning("daily_picks: 无可用数据日期, 跳过")
            return []

        # 1) 加载当日 enriched(含 signal / name / close 等, 用于过滤)
        df = svc._load_enriched_for_date(as_of)
        if df.is_empty():
            logger.warning("daily_picks: %s enriched 为空, 跳过", as_of)
            return []

        # 2) 候选池过滤: 停牌 / ST退市 / 跌停 / 一字涨停(买不进) / 次新股
        mask = pl.col("close").is_not_null() & (pl.col("close") > 0)
        if "name" in df.columns:
            bad = pl.col("name").fill_null("").str.contains("(?i)ST|退")
            mask = mask & ~bad
        if "signal_limit_down" in df.columns:
            mask = mask & ~pl.col("signal_limit_down").fill_null(False)
        one_word_ready = all(c in df.columns for c in ("signal_limit_up", "open", "high", "low", "close"))
        if one_word_ready:
            one_word = (
                pl.col("signal_limit_up").fill_null(False)
                & (pl.col("open") == pl.col("high"))
                & (pl.col("high") == pl.col("low"))
                & (pl.col("low") == pl.col("close"))
            )
            mask = mask & ~one_word
        if min_listing_days and min_listing_days > 0:
            new_syms = self._new_listing_symbols(as_of, min_listing_days)
            if new_syms:
                mask = mask & ~pl.col("symbol").is_in(new_syms)
        candidates = df.filter(mask)
        if candidates.is_empty():
            logger.info("daily_picks: %s 过滤后候选池为空", as_of)
            return []
        name_map = {}
        if "name" in candidates.columns:
            for r in candidates.iter_rows(named=True):
                name_map[str(r["symbol"])] = r.get("name")

        # 3) 跑全量策略
        all_ids = [
            meta["id"]
            for meta in self.engine.list_strategies()
            if not meta.get("research_only")
            and "stock" in meta.get("asset_types", ["stock"])
            and "1d" in meta.get("timeframes", ["1d"])
        ]
        if not all_ids:
            return []
        all_overrides = strategy_config.list_overrides(self.data_dir)
        params_map = {sid: dict((all_overrides.get(sid) or {}).get("params") or {}) for sid in all_ids}
        overrides_map = {sid: all_overrides.get(sid, {}) for sid in all_ids}
        context = svc.build_strategy_context(
            self.engine, as_of, all_ids,
            timeframe="1d", params_map=params_map, overrides_map=overrides_map,
            current=df,
        )
        engine_results = self.engine.run_all(
            context, params_map=params_map, overrides_map=overrides_map, strategy_ids=all_ids,
        )

        # 4) 聚合多策略共振命中
        agg: dict[str, dict] = {}
        for sid, result in engine_results.items():
            for row in result.rows:
                sym = str(row.get("symbol") or "")
                if not sym or sym not in name_map:
                    continue
                entry = agg.setdefault(sym, {"hits": [], "best_score": None})
                entry["hits"].append(sid)
                s = row.get("score")
                if s is not None:
                    try:
                        s = float(s)
                    except (TypeError, ValueError):
                        s = None
                    if s is not None and (entry["best_score"] is None or s > entry["best_score"]):
                        entry["best_score"] = s
        if not agg:
            logger.info("daily_picks: %s 无策略命中", as_of)
            return []

        # 5) 排序: 共振数降序 → 最高分降序
        ranked = sorted(
            agg.items(),
            key=lambda kv: (-len(kv[1]["hits"]), -(kv[1]["best_score"] or 0.0)),
        )
        picks = ranked[: max(1, min(top_n, len(ranked)))]

        # 6) 定 settle_date(pick_date 之后第 hold_days 个交易日)
        settle_date = self._settle_date_after(as_of.isoformat(), hold_days)

        # 7) 落盘(先删该日旧记录, 支持重跑)
        existing = self._load()
        if not existing.is_empty() and "pick_date" in existing.columns:
            existing = existing.filter(pl.col("pick_date") != as_of.isoformat())
        created = datetime.now().isoformat(timespec="seconds")
        rows: list[dict] = []
        for i, (sym, info) in enumerate(picks, start=1):
            rows.append({
                "pick_date": as_of.isoformat(),
                "settle_date": settle_date,
                "symbol": sym,
                "name": name_map.get(sym),
                "rank": i,
                "signal_count": len(info["hits"]),
                "score": info["best_score"],
                "strategy_hits": json.dumps(info["hits"], ensure_ascii=False),
                "entry_close": None,  # 由下方从 enriched 回填
                "exit_close": None,
                "pnl_pct": None,
                "hit": None,
                "hold_days": hold_days,
                "created_at": created,
            })
        # 回填 entry_close
        close_map = {str(r["symbol"]): r.get("close") for r in candidates.iter_rows(named=True)}
        for r in rows:
            r["entry_close"] = close_map.get(r["symbol"])

        new_df = pl.DataFrame(rows, schema={
            "pick_date": pl.Utf8, "settle_date": pl.Utf8, "symbol": pl.Utf8, "name": pl.Utf8,
            "rank": pl.Int64, "signal_count": pl.Int64, "score": pl.Float64, "strategy_hits": pl.Utf8,
            "entry_close": pl.Float64, "exit_close": pl.Float64, "pnl_pct": pl.Float64, "hit": pl.Boolean,
            "hold_days": pl.Int64, "created_at": pl.Utf8,
        })
        if new_df.is_empty():
            return []
        merged = pl.concat([existing, new_df], how="vertical") if not existing.is_empty() else new_df
        self._save(merged)

        logger.info("daily_picks: %s 选出 %d 只 (top_n=%d, hold=%d)", as_of, len(rows), top_n, hold_days)
        if push_lark and rows:
            try:
                push_res = self.push_rows_to_lark(rows)
                logger.info("daily_picks: 飞书推送 pushed=%d skipped=%d err=%s",
                            push_res.get("pushed"), push_res.get("skipped"), push_res.get("error"))
            except Exception as e:  # noqa: BLE001
                logger.warning("daily_picks: 飞书推送失败(不阻断选股主流程): %s", e)
        return rows

    def _new_listing_symbols(self, as_of: date, min_listing_days: int) -> set[str]:
        """返回「上市不满 min_listing_days 自然日」的 A 股 symbol 集合。

        上市日期取自 instruments.parquet 的 listing_date; 缺失的(仅退市股)放行,
        由 ST/退 过滤兜底。
        """
        try:
            inst = self.repo.get_instruments()
        except Exception as e:  # noqa: BLE001
            logger.warning("daily_picks 读取 instruments 失败, 跳过次新过滤: %s", e)
            return set()
        if inst.is_empty() or "listing_date" not in inst.columns or "symbol" not in inst.columns:
            return set()
        out: set[str] = set()
        for r in inst.iter_rows(named=True):
            sym = r.get("symbol")
            ld = r.get("listing_date")
            if not sym or ld is None:
                continue
            d = _to_date(ld)
            if d is None:
                continue
            if (as_of - d).days < min_listing_days:
                out.add(str(sym))
        return out

    def _settle_date_after(self, pick_date: str, hold_days: int) -> str | None:
        dates = self._trading_dates()
        if not dates:
            return None
        pos = bisect_right(dates, pick_date)
        idx = pos + hold_days - 1
        if idx < len(dates):
            return dates[idx]
        return None

    # ── 飞书推送 ──────────────────────────────────────────────
    def _strategy_labels(self, strategy_hits: str | None) -> list[str]:
        """strategy_hits(json 列表) -> 中文策略名列表。"""
        try:
            ids = json.loads(strategy_hits or "[]")
        except ValueError:
            return []
        labels: list[str] = []
        for sid in ids:
            try:
                labels.append(str(self.engine.get(sid).meta.get("name") or sid))
            except Exception:
                labels.append(str(sid))
        return labels

    def _to_lark_records(self, rows: list[dict]) -> list[dict]:
        """picks 行 -> 飞书表记录(字段名对齐; pnl_pct×100; hit 转 是/否)。"""
        out: list[dict] = []
        for r in rows:
            pnl = r.get("pnl_pct")
            hit = r.get("hit")
            out.append({
                "代码": str(r.get("symbol") or "").split(".")[0],
                "名称": str(r.get("name") or ""),
                "选股日期": r.get("pick_date"),
                "排名": r.get("rank"),
                "共振策略数": r.get("signal_count"),
                "评分": r.get("score"),
                "买入参考价": r.get("entry_close"),
                "触发策略": "、".join(self._strategy_labels(r.get("strategy_hits"))) or None,
                "结算日期": r.get("settle_date"),
                "结算价": r.get("exit_close"),
                "持有收益%": round(float(pnl) * 100, 2) if pnl is not None else None,
                "命中": ("是" if hit is True else "否" if hit is False else None),
            })
        return out

    def push_rows_to_lark(self, rows: list[dict]) -> dict:
        """把精选行推送到飞书表(按 代码+选股日期 去重)。返回结果摘要。"""
        from app.services import lark_bitable as lb

        if not LARK_BASE_TOKEN or not LARK_TABLE_ID:
            return {"ok": False, "pushed": 0, "skipped": 0, "error": "飞书表未配置"}
        if not rows:
            return {"ok": True, "pushed": 0, "skipped": 0, "error": None}
        result = lb.push_records(
            LARK_BASE_TOKEN, LARK_TABLE_ID, self._to_lark_records(rows),
            key_fields=("代码", "选股日期"),
            date_fields=_LARK_DATE_FIELDS,
        )
        return {
            "ok": result.ok, "pushed": result.pushed, "skipped": result.skipped,
            "error": result.error, "details": result.details,
        }

    def sync_settlement_to_lark(self) -> dict:
        """把已结算 pick 的 结算价/持有收益%/命中/结算日期 回写到飞书表。"""
        from app.services import lark_bitable as lb

        if not LARK_BASE_TOKEN or not LARK_TABLE_ID:
            return {"ok": False, "synced": 0, "error": "飞书表未配置"}
        df = self._load()
        if df.is_empty() or "pnl_pct" not in df.columns:
            return {"ok": True, "synced": 0, "error": None}
        settled = df.filter(pl.col("pnl_pct").is_not_null())
        if settled.is_empty():
            return {"ok": True, "synced": 0, "error": None}

        rows = lb.list_records(LARK_BASE_TOKEN, LARK_TABLE_ID, ["代码", "选股日期"])
        if rows is None:
            return {"ok": False, "synced": 0, "error": "读取飞书表失败"}
        rid_by_key: dict[tuple[str, str], str] = {}
        for rec in rows:
            rid = rec.get("record_id")
            code = str(rec.get("代码") or "").strip()
            d = str(rec.get("选股日期") or "")[:10]
            if rid and code:
                rid_by_key[(code, d)] = rid

        synced = 0
        errors: list[str] = []
        for r in settled.iter_rows(named=True):
            code = str(r.get("symbol") or "").split(".")[0]
            d = str(r.get("pick_date") or "")[:10]
            rid = rid_by_key.get((code, d))
            if not rid:
                continue
            pnl = r.get("pnl_pct")
            hit = r.get("hit")
            fields = {
                "结算价": r.get("exit_close"),
                "持有收益%": round(float(pnl) * 100, 2) if pnl is not None else None,
                "命中": ("是" if hit is True else "否" if hit is False else None),
                "结算日期": str(r.get("settle_date") or "")[:10],
            }
            res = lb.upsert_record(
                LARK_BASE_TOKEN, LARK_TABLE_ID, rid, fields, date_fields=("结算日期",),
            )
            if res.ok:
                synced += 1
            else:
                errors.append(res.error or "未知错误")
        return {"ok": True, "synced": synced, "error": "; ".join(errors) if errors else None}

    # ── 结算 ──────────────────────────────────────────────────
    def settle_pending(self, as_of: date | None = None) -> int:
        """对到期且未结算的 pick 结算收益。返回本次结算条数。"""
        df = self._load()
        if df.is_empty() or "exit_close" not in df.columns:
            return 0
        pending = df.filter(pl.col("exit_close").is_null() & pl.col("settle_date").is_not_null())
        if pending.is_empty():
            return 0

        today = (as_of or date.today()).isoformat()
        dates = self._trading_dates()
        settled = 0
        updates: dict[str, dict] = {}
        for r in pending.iter_rows(named=True):
            settle_date = str(r["settle_date"])
            if settle_date > today:  # 尚未到期
                continue
            exit_close = self._close_on(settle_date, str(r["symbol"]))
            if exit_close is None:
                continue
            entry = r.get("entry_close")
            pnl_pct = None
            hit = None
            if entry:
                pnl_pct = round((exit_close - float(entry)) / float(entry), 6)
                hit = pnl_pct > 0
            updates[str(r["symbol"]) + "|" + str(r["pick_date"])] = {
                "exit_close": exit_close, "pnl_pct": pnl_pct, "hit": hit,
            }
        if not updates:
            return 0

        for key, patch in updates.items():
            sym, pick_date = key.split("|", 1)
            mask = (pl.col("symbol") == sym) & (pl.col("pick_date") == pick_date)
            df = df.with_columns([
                pl.when(mask).then(pl.lit(patch["exit_close"])).otherwise(pl.col("exit_close")).alias("exit_close"),
                pl.when(mask).then(pl.lit(patch["pnl_pct"])).otherwise(pl.col("pnl_pct")).alias("pnl_pct"),
                pl.when(mask).then(pl.lit(patch["hit"])).otherwise(pl.col("hit")).alias("hit"),
            ])
            settled += 1
        self._save(df)
        logger.info("daily_picks: 结算 %d 条", settled)
        if settled:
            try:
                sync_res = self.sync_settlement_to_lark()
                logger.info("daily_picks: 结算回写飞书 synced=%d err=%s",
                            sync_res.get("synced"), sync_res.get("error"))
            except Exception as e:  # noqa: BLE001
                logger.warning("daily_picks: 结算回写飞书失败(不阻断主流程): %s", e)
        return settled

    def _close_on(self, d: str, symbol: str) -> float | None:
        """读某交易日的收盘价(窄读 parquet 分区)。"""
        part = self._enriched_dir / f"date={d}" / "part.parquet"
        if not part.exists():
            return None
        try:
            lf = pl.scan_parquet(part)
            cols = lf.collect_schema().names()
            if "symbol" not in cols or "close" not in cols:
                return None
            row = lf.filter(pl.col("symbol") == symbol).select("close").collect()
            if row.is_empty():
                return None
            v = row[0, "close"]
            return float(v) if v is not None else None
        except Exception as e:  # noqa: BLE001
            logger.warning("daily_picks _close_on(%s,%s) 失败: %s", d, symbol, e)
            return None

    # ── 查询 ──────────────────────────────────────────────────
    def today(self, as_of: date | None = None) -> dict:
        """最新一期精选(默认最新 pick_date)。"""
        df = self._load()
        if df.is_empty() or "pick_date" not in df.columns:
            return {"pick_date": None, "picks": []}
        latest = df["pick_date"].max()
        if latest is None:
            return {"pick_date": None, "picks": []}
        rows = df.filter(pl.col("pick_date") == latest).sort("rank").to_dicts()
        return {"pick_date": latest, "picks": self._sanitize(rows)}

    def history(self, limit: int = 200) -> list[dict]:
        """历史精选(含命中标记), 按 pick_date 降序。"""
        df = self._load()
        if df.is_empty():
            return []
        df = df.sort(["pick_date", "rank"], descending=[True, False]).head(limit)
        return self._sanitize(df.to_dicts())

    def stats(self) -> dict:
        """命中率统计(仅统计已结算条目)。"""
        df = self._load()
        if df.is_empty():
            return self._empty_stats()
        settled = df.filter(pl.col("pnl_pct").is_not_null())
        if settled.is_empty():
            return self._empty_stats()
        pnl = settled["pnl_pct"].drop_nulls().to_list()
        total = len(settled)
        wins = sum(1 for p in pnl if p > 0)
        strong = sum(1 for p in pnl if p >= _STRONG_HIT_PCT)
        stops = sum(1 for p in pnl if p <= _STOP_LOSS_PCT)
        pnl_sorted = sorted(pnl)
        n = len(pnl_sorted)
        median = pnl_sorted[n // 2] if n % 2 else (pnl_sorted[n // 2 - 1] + pnl_sorted[n // 2]) / 2
        avg = sum(pnl_sorted) / n
        return {
            "total": total,
            "wins": wins,
            "strong_hits": strong,
            "stop_losses": stops,
            "hit_rate": round(wins / total * 100, 2),
            "avg_pnl_pct": round(avg, 4),
            "median_pnl_pct": round(median, 4),
            "best_pnl_pct": round(pnl_sorted[-1], 4),
            "worst_pnl_pct": round(pnl_sorted[0], 4),
            "pending": int(df.filter(pl.col("pnl_pct").is_null()).height),
        }

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "total": 0, "wins": 0, "strong_hits": 0, "stop_losses": 0,
            "hit_rate": None, "avg_pnl_pct": None, "median_pnl_pct": None,
            "best_pnl_pct": None, "worst_pnl_pct": None, "pending": 0,
        }

    @staticmethod
    def _sanitize(rows: list[dict]) -> list[dict]:
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, float) and not math.isfinite(v):
                    r[k] = None
                elif isinstance(v, (date, datetime)):
                    r[k] = v.isoformat()
        return rows
