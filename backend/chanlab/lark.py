"""飞书多维表格推送 (N11) —— 复用本机 lark-cli 的用户身份通道。

为什么不直接调 OpenAPI: 走 lark-cli 的 ``--as user`` 与本机已登录会话共享凭据,
免自建应用 / 免 tenant_token 续期。v1 用它稳定推了几个月的 qushiqinlong 三表,
这里把踩过的坑原样搬过来:

1. ``lark-cli.cmd`` 内部以裸名 ``node`` 启动, PATH 被裁剪时静默失败
   (rc=1 且报错是 GBK 中文) -> :func:`find_lark_cli` + :func:`lark_env` 兜底补 PATH。
2. stderr 是 GBK, 必须 ``errors="replace"`` 解码, 否则 UnicodeDecodeError 被吞掉,
   表现为「去重读取失败」。
3. ``--json @file`` / ``--output`` 只接受**相对路径**, 子进程统一 ``cwd=ROOT``。
4. ``+record-list`` 分页: ``has_more`` 在 stdout 顶层或 data 内 (两种都兼容), offset 步进 2000。

表从哪来: v1 是「策略 id -> 固定表」的硬编码映射, 而 v2 的策略池是用户自己建的,
写死映射没有意义。所以表的 ``base_token`` / ``table_id`` 存在**策略条目**里,
前端在策略上填一次, 之后一点就推。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TMP_DIR_REL = Path("data") / "pool" / "_lark_tmp"

_BATCH_SIZE = 200
_PAGE_SIZE = 2000
_READ_TIMEOUT = 120
_WRITE_TIMEOUT = 180

#: 去重键. 同一天同一只票重复推只留一条, 这也是「手抖点两次」的安全网
KEY_FIELDS = ("代码", "信号日期")
DATE_FIELDS = ("信号日期",)

_BSP_LABEL = {
    "1": "一类买点", "1p": "一类买点(盘整)", "2": "二类买点",
    "2s": "类二类买点", "3a": "三类买点A", "3b": "三类买点B",
}
_ZS_LABEL = {"above": "中枢上方", "inside": "中枢内", "below": "中枢下方"}
_DIVERGE_LABEL = {"bottom": "底背驰", "top": "顶背驰"}


@dataclass
class PushResult:
    """推送结果。ok=False 时 error 带原因; pushed / skipped 见 :func:`push`。"""

    ok: bool
    pushed: int = 0
    skipped: int = 0
    error: str | None = None
    details: list[str] = field(default_factory=list)


def find_lark_cli() -> str:
    """定位 lark-cli。不要只写裸名: 从 IDE / Git Bash 启动时 PATH 常被裁剪。"""
    names = ["lark-cli.cmd", "lark-cli.exe", "lark-cli"] if os.name == "nt" else ["lark-cli"]
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    candidates: list[str] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "npm", "lark-cli.cmd"))
    candidates.append(
        os.path.join(
            os.path.expanduser("~"),
            ".workbuddy", "binaries", "node", "cli-connector-packages", "lark-cli.cmd",
        )
    )
    for c in candidates:
        if os.path.exists(c):
            return c
    return names[0]


def lark_env(cli_path: str | None = None) -> dict[str, str]:
    """给子进程补 PATH, 保证 lark-cli 内部的裸名 node 能找到。"""
    cli = cli_path or find_lark_cli()
    env = dict(os.environ)
    path = env.get("PATH", "")
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "nodejs"),
        os.path.dirname(cli) if cli else "",
    ]
    versions_root = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(versions_root):
        for sub in sorted(os.listdir(versions_root)):
            if sub.startswith("."):
                continue
            d = os.path.join(versions_root, sub)
            if os.path.isdir(d):
                candidates.append(d)
    add = [d for d in candidates if d and os.path.isdir(d) and d.lower() not in path.lower()]
    if add:
        env["PATH"] = os.pathsep.join(add) + os.pathsep + path
    return env


def status() -> dict[str, Any]:
    """lark-cli 是否可用。不可用时前端直接禁用推送按钮并说明原因。"""
    cli = find_lark_cli()
    available = bool(shutil.which(cli) or (os.path.isabs(cli) and os.path.exists(cli)))
    return {"cli": cli, "available": available}


# --------------------------------------------------------------------------
# 值转换
# --------------------------------------------------------------------------


def to_ms_date(value: Any) -> int | None:
    """"2026-09-30" -> 毫秒时间戳 (Base 日期字段只接受 ms)。解析失败返回 None。"""
    try:
        return int(datetime.strptime(str(value)[:10], "%Y-%m-%d").timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def to_num(value: Any) -> float | None:
    """转 float, 无效值("-"/None/NaN)转 None —— Base 数字列不接受非法值。"""
    try:
        v = float(value)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def _pct(value: Any, digits: int = 2) -> float | None:
    """小数涨跌幅 -> 百分数数值。扫描行里的 chg_pct / zs_dist 都是小数。"""
    v = to_num(value)
    return round(v * 100, digits) if v is not None else None


def key_norm(field_name: str, value: Any) -> str:
    """去重键归一化: 日期列截到 YYYY-MM-DD, 其余去空白。"""
    if value is None:
        return ""
    text = str(value).strip()
    if "日期" in field_name or "date" in field_name.lower():
        return text[:10]
    return text


# --------------------------------------------------------------------------
# 扫描行 -> 表记录
# --------------------------------------------------------------------------


#: v1 同源策略 -> 目标表字段。每张表的列是历史沿用下来的, 不能统一(表还是那几张表,
#: 用户在用的视图/筛选都挂在列名上), 所以这里原样照 v1 lark_screener.build_records 写。
_STRATEGY_FIELDS: dict[str, tuple[str, ...]] = {
    "bottom_structure": ("信号状态", "DIF", "DEA", "钝化类型", "MA5", "MA20"),
    "upward_trend_breakout": (),
    "trend_dragon": ("MA5", "MA13", "乖离MA20%", "资金动能"),
    "startup_surge": ("量比5日", "换手率%", "20日动量%", "MA5", "MA20", "评分"),
}


def build_records(
    rows: list[dict],
    as_of: str,
    mode: str = "single",
    strategy_id: str = "",
) -> list[dict]:
    """把结果行映射成表记录。

    两种表形态:

    - 缠论类 (``single``/``resonance``): 字段由本模块定义 —— 策略池是动态的,
      不可能给每个策略建一套列, 用户按这里给的字段名建表即可。
    - **v1 同源策略** (``strategy``): 目标表就是 v1 那几张历史表, 列名必须沿用,
      否则用户表里既有的记录会出现一半有值一半空。字段名见 :data:`_STRATEGY_FIELDS`。

    ``mode=resonance`` 时行里带 ``s_`` 前缀的次级别字段, 多推一列「次级别买点」。
    """
    if mode == "strategy":
        return _strategy_records(rows, as_of, strategy_id)
    out: list[dict] = []
    for r in rows:
        symbol = str(r.get("symbol") or "")
        code, _, market = symbol.partition(".")
        buy = str(r.get("buy_type") or "")
        rec: dict[str, Any] = {
            "代码": code,
            "名称": str(r.get("name") or ""),
            "市场": market,
            "信号日期": as_of,
            "收盘价": to_num(r.get("close")),
            # ⚠️ 扫描行的 chg_pct / zs_dist / bi_pct 已经是**百分数** (scanner._pct
            # 出来的), 这里只取数不改量纲 -- 早期用 _pct() 又乘了一次 100,
            # 表里的涨跌幅全是 312.0 这种, 2026-10-05 修。
            "涨跌幅%": _r2(r.get("chg_pct")),
            "买点": _BSP_LABEL.get(buy, buy),
            "距今": to_num(r.get("buy_ago")),
            "中枢位置": _ZS_LABEL.get(str(r.get("zs_pos") or ""), ""),
            "距中枢%": _r2(r.get("zs_dist")),
            "背驰": _DIVERGE_LABEL.get(str(r.get("diverge") or ""), ""),
            "末笔%": _r2(r.get("bi_pct")),
        }
        if mode == "resonance":
            s_buy = str(r.get("s_buy_type") or "")
            rec["次级别买点"] = _BSP_LABEL.get(s_buy, s_buy)
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# 子进程
# --------------------------------------------------------------------------


def _strategy_records(rows: list[dict], as_of: str, strategy_id: str) -> list[dict]:
    """v1 表口径的记录。数值单位沿用 v1: 涨跌幅/动量是百分数, 价格原值保留 2 位。"""
    wanted = _STRATEGY_FIELDS.get(strategy_id)
    if wanted is None:
        raise KeyError(f"该策略没有登记目标表字段: {strategy_id}")
    out: list[dict] = []
    for r in rows:
        symbol = str(r.get("symbol") or "")
        code, _, market = symbol.partition(".")
        rec: dict[str, Any] = {
            "代码": code,
            "名称": str(r.get("name") or ""),
            "市场": market,
            "信号日期": as_of,
            "收盘价": _r2(r.get("close")),
            "涨跌幅%": _pct(r.get("change_pct")),
        }
        for column in wanted:
            rec[column] = _mapped(column, r)
        out.append(rec)
    return out


def _mapped(column: str, row: dict) -> Any:
    """按 v1 的口径取一个派生列。取不到一律 None —— 宁可空着也不填 0,
    「没算出来」和「算出来是 0」在表上是两件事。"""
    if column in ("信号状态", "钝化类型"):
        return ""  # v1: 策略矩阵内部值, 输出行不携带
    if column == "MA13":
        return None  # v1: enriched 没有 ma13 列
    source = {
        "DIF": "macd_dif",
        "DEA": "macd_dea",
        "MA5": "ma5",
        "MA20": "ma20",
        "乖离MA20%": "bias_ma20",
        "资金动能": "capital_momentum",
        "量比5日": "vol_ratio_5d",
        "换手率%": "turnover_rate",
        "20日动量%": "momentum_20d",
    }.get(column)
    value = to_num(row.get(source or ""))
    if column in ("DIF", "DEA", "乖离MA20%", "资金动能"):
        return value
    if column == "20日动量%":
        return round(value * 100, 2) if value is not None else None
    if column == "评分":
        return None if row.get("score") is None else round(float(row["score"]), 2)
    return _r2(value)


def _r2(value: Any) -> float | None:
    """数值 -> 保留 2 位。表内数值列精度就是 2 位, 存原值会带一串浮点尾巴。"""
    number = to_num(value)
    return None if number is None else round(float(number), 2)


def _tmp_rel_path(prefix: str, suffix: str = ".ndjson", clean: bool = True) -> Path:
    """仓库根下的相对临时路径 (lark-cli 只接受相对路径)。

    ``clean=True`` 时先清扫目录内旧残留。一次要生成多个文件时只有第一个应该
    clean, 否则后建的文件会把先建的清掉。
    """
    abs_dir = ROOT / TMP_DIR_REL
    abs_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for f in abs_dir.iterdir():
            with contextlib.suppress(OSError):
                f.unlink()
    name = f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 100000}{suffix}"
    return TMP_DIR_REL / name


def _cleanup_tmp(*rel_paths: Path) -> None:
    for rel in rel_paths:
        stem = rel.with_suffix("") if rel.suffix == ".ndjson" else rel
        for f in (rel, stem.with_suffix(".manifest.json")):
            with contextlib.suppress(OSError):
                (ROOT / f).unlink()


def _run_cli(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [find_lark_cli(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=lark_env(),
        cwd=ROOT,
    )


def existing_keys(
    base_token: str, table_id: str, key_fields: tuple[str, ...] = KEY_FIELDS,
) -> set[tuple[str, ...]] | None:
    """读表内已有业务键, 用于去重。失败返回 None (调用方应**中止推送**)。

    ``key_fields``: 业务键列名。告警表是 (代码, 信号日期, 告警类型) 三元键
    (同一天同一只票可以既出买点又突破中枢), 其余表沿用默认二元键。
    """
    records: list[dict[str, Any]] = []
    tmp = _tmp_rel_path("list")
    offset = 0
    try:
        while True:
            args = [
                "base", "+record-list",
                "--base-token", base_token, "--table-id", table_id,
                *[a for fid in key_fields for a in ("--field-id", fid)],
                "--offset", str(offset),
                "--format", "ndjson", "--output", str(tmp), "--as", "user",
            ]
            try:
                proc = _run_cli(args, _READ_TIMEOUT)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return None
            abs_tmp = ROOT / tmp
            if proc.returncode != 0 or not abs_tmp.exists():
                return None
            with abs_tmp.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
            has_more = False
            try:
                out = json.loads(proc.stdout)
                has_more = bool(
                    out.get("ok") and (out.get("has_more") or out.get("data", {}).get("has_more"))
                )
            except (ValueError, AttributeError):
                pass
            if not has_more:
                break
            offset += _PAGE_SIZE
    finally:
        _cleanup_tmp(tmp)
    return {tuple(key_norm(f, r.get(f)) for f in key_fields) for r in records}


def push(
    base_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    *,
    force: bool = False,
    key_fields: tuple[str, ...] = KEY_FIELDS,
) -> PushResult:
    """追加写入多维表格, 按 ``key_fields`` (默认 (代码, 信号日期)) 去重。

    ``force=True`` 跳过去重。读取表内已有记录失败时**中止推送** (ok=False):
    拿不到现有键还硬写, 就是把重复数据灌进去。
    """
    details: list[str] = []
    todo = records
    skipped = 0
    if not force:
        existing = existing_keys(base_token, table_id, key_fields)
        if existing is None:
            return PushResult(
                ok=False,
                error="无法读取表内已有记录, 为避免重复导入已中止推送 (可勾选强制推送)",
            )
        todo = [
            r for r in records
            if tuple(key_norm(f, r.get(f)) for f in key_fields) not in existing
        ]
        skipped = len(records) - len(todo)
        if skipped:
            details.append(f"去重: 跳过 {skipped} 条表内已存在的记录")
        if not todo:
            return PushResult(ok=True, pushed=0, skipped=skipped, details=details)

    payload = [
        {k: (to_ms_date(v) if k in DATE_FIELDS else v) for k, v in r.items()} for r in todo
    ]

    batch_files: list[Path] = []
    for i in range(0, len(payload), _BATCH_SIZE):
        rel = _tmp_rel_path("push", suffix=f"_{i // _BATCH_SIZE}.json", clean=(i == 0))
        with (ROOT / rel).open("w", encoding="utf-8") as f:
            json.dump({"create_records": payload[i : i + _BATCH_SIZE]}, f, ensure_ascii=False)
        batch_files.append(rel)

    pushed = 0
    try:
        for rel in batch_files:
            args = [
                "base", "+record-batch-create",
                "--base-token", base_token, "--table-id", table_id,
                "--json", f"@{rel}", "--as", "user",
            ]
            try:
                proc = _run_cli(args, _WRITE_TIMEOUT)
            except FileNotFoundError:
                return PushResult(
                    ok=False, pushed=pushed, skipped=skipped,
                    error="找不到 lark-cli, 请先安装: npm install -g @larksuite/cli",
                    details=details,
                )
            except subprocess.TimeoutExpired:
                return PushResult(
                    ok=False, pushed=pushed, skipped=skipped,
                    error=f"批次 {rel.name} 写入超时", details=details,
                )

            n_batch = 0
            try:
                out = json.loads(proc.stdout)
                if out.get("ok"):
                    n_batch = len(out.get("data", {}).get("record_id_list", []))
            except (ValueError, AttributeError):
                if '"ok": true' in (proc.stdout or ""):
                    n_batch = _BATCH_SIZE
            if n_batch > 0:
                pushed += n_batch
                details.append(f"批次 {rel.name}: {n_batch} 条写入成功")
            else:
                return PushResult(
                    ok=False, pushed=pushed, skipped=skipped,
                    error=f"批次 {rel.name} 写入失败: {(proc.stdout or proc.stderr or '')[:300]}",
                    details=details,
                )
    finally:
        _cleanup_tmp(*batch_files)

    return PushResult(ok=True, pushed=pushed, skipped=skipped, details=details)
