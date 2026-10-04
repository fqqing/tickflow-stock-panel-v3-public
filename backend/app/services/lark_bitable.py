"""飞书多维表格 (Bitable) 推送通道 — lark-cli 用户身份封装。

职责
----
把「字段名 -> 值」的记录列表追加写入飞书多维表格, 支持按业务键去重(安全重复运行)。
表结构(有哪些字段)由调用方负责, 本模块不关心具体业务表。

为什么封装 lark-cli 而不是直接调 OpenAPI:
  用户身份(--as user)免自建应用/免 tenant_token 续期, 与本机已登录的 lark-cli 会话
  共享凭据。qushiqinlong 三策略推送已用同一通道稳定运行, 此处把踩过的坑全部固化:
  1. lark-cli.cmd 内部以裸名 node 启动, PATH 被裁剪(IDE/Git Bash 启动)时静默失败
     -> find_lark_cli() + lark_env() 兜底定位并补 PATH。
  2. stderr 是 GBK 中文, 必须 errors="replace" 解码, 否则 UnicodeDecodeError 被
     静默吞掉, 表现为「去重读取失败」。
  3. --json @file / --output 只接受**相对路径**, 子进程统一 cwd=仓库根。
  4. +record-list 分页: has_more 在 stdout 顶层或 data 内(两种格式都兼容), offset 步进 2000。
  5. lark-cli 的 manifest 文件是把 .ndjson 后缀替换为 .manifest.json(不是追加), 清理时要一起删。

典型用法::

    from app.services import lark_bitable

    records = [
        {"代码": "600519", "名称": "贵州茅台", "信号日期": "2026-09-30", "收盘价": 1258.62},
    ]
    result = lark_bitable.push_records(
        base_token="EEYSbsdLpa9QkZsmGyVc7vCCnbb",
        table_id="tbl8TfrGiJYfzDd7",
        records=records,
        key_fields=("代码", "信号日期"),   # 传 None 或 force=True 则不去重
        date_fields=("信号日期",),          # 日期列写盘时自动转 Base 毫秒时间戳
    )
    # result.ok / result.pushed / result.skipped
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 仓库根 (backend/app/services/lark_bitable.py -> services/app/backend/根)
REPO_ROOT = Path(__file__).resolve().parents[3]
# lark-cli 只接受相对路径, 临时文件统一放这里(子进程 cwd=REPO_ROOT)
TMP_DIR_REL = Path("data") / "user_data" / "_lark_tmp"

# 单批写入上限 (lark-cli / Base API 批次限制)
_BATCH_SIZE = 200
# +record-list 单页上限与 offset 步进
_PAGE_SIZE = 2000
# 子进程超时(秒): 读单页 / 写单批
_READ_TIMEOUT = 120
_WRITE_TIMEOUT = 180


@dataclass
class PushResult:
    """推送结果。ok=False 时 error 带原因; pushed/skipped 语义见 push_records。"""

    ok: bool
    pushed: int = 0
    skipped: int = 0
    error: str | None = None
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# lark-cli 定位与环境
# ---------------------------------------------------------------------------

def find_lark_cli() -> str:
    """定位 lark-cli 可执行文件。

    不要只写裸名: 从 IDE 任务 / Git Bash 启动时 PATH 常被裁剪, 裸名找不到。
    which + 常见安装目录兜底, 保证任何启动方式下都能定位。
    """
    names = (
        ["lark-cli.cmd", "lark-cli.exe", "lark-cli"]
        if os.name == "nt"
        else ["lark-cli"]
    )
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    candidates = []
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
    """给 lark-cli 子进程补全 PATH。

    lark-cli.cmd 内部以裸名 node 启动 run.js, node 不在 PATH 时 rc=1 且报错是
    GBK 中文 -> 解码崩 -> 表现为「读取失败」。主动把 node 常见目录补进 PATH。
    """
    cli = cli_path or find_lark_cli()
    env = dict(os.environ)
    path = env.get("PATH", "")
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "nodejs"),
        os.path.dirname(cli) if cli else "",
    ]
    versions_root = os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions"
    )
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


# ---------------------------------------------------------------------------
# 字段值转换
# ---------------------------------------------------------------------------

def to_ms_date(s: Any) -> int | None:
    """"2026-09-01" -> 毫秒时间戳 (Base 时区当日 00:00); 解析失败返回 None。"""
    try:
        return int(datetime.strptime(str(s)[:10], "%Y-%m-%d").timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def to_num(x: Any) -> float | None:
    """转 float, 无效值("-"/None/NaN)转 None (Base 数字列不接受非法值)。"""
    try:
        v = float(x)
        return None if v != v else v  # NaN 检查
    except (TypeError, ValueError):
        return None


def default_key_norm(field_name: str, value: Any) -> str:
    """去重键默认归一化: 日期类字段截到 YYYY-MM-DD, 其余 str 去空白。"""
    if value is None:
        return ""
    text = str(value).strip()
    if "日期" in field_name or "date" in field_name.lower():
        return text[:10]
    return text


# ---------------------------------------------------------------------------
# 临时文件
# ---------------------------------------------------------------------------

def _tmp_rel_path(prefix: str, suffix: str = ".ndjson", clean: bool = True) -> Path:
    """生成仓库根下的相对临时路径。

    clean=True 时先清扫目录内旧残留(Windows 句柄延迟删不掉的只留在子目录)。
    一次要生成多个文件(如拆批)时, 只有第一个文件应 clean=True,
    否则后建的文件会把先建的清掉。
    """
    abs_dir = REPO_ROOT / TMP_DIR_REL
    abs_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for f in abs_dir.iterdir():
            with suppress(OSError):
                f.unlink()
    name = f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 100000}{suffix}"
    return TMP_DIR_REL / name


def _cleanup_tmp(*rel_paths: Path) -> None:
    """尽力删除临时文件(含 lark-cli 的 .manifest.json), 失败不抛错。"""
    for rel in rel_paths:
        stem = rel.with_suffix("") if rel.suffix == ".ndjson" else rel
        for f in (rel, stem.with_suffix(".manifest.json")):
            with suppress(OSError):
                (REPO_ROOT / f).unlink()


def _run_cli(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """统一子进程调用: cwd=仓库根(相对路径要求), utf-8 + errors=replace(GBK 报错解码兜底)。"""
    return subprocess.run(
        [find_lark_cli(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=lark_env(),
        cwd=REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# 读: 列表 / 去重键
# ---------------------------------------------------------------------------

def list_records(
    base_token: str,
    table_id: str,
    field_ids: list[str],
) -> list[dict[str, Any]] | None:
    """读出表内全部记录的指定字段 (ndjson 分页)。失败返回 None。"""
    records: list[dict[str, Any]] = []
    tmp = _tmp_rel_path("list")
    offset = 0
    try:
        while True:
            args = [
                "base", "+record-list",
                "--base-token", base_token, "--table-id", table_id,
                *[a for fid in field_ids for a in ("--field-id", fid)],
                "--offset", str(offset),
                "--format", "ndjson", "--output", str(tmp), "--as", "user",
            ]
            try:
                proc = _run_cli(args, _READ_TIMEOUT)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                logger.warning("lark-cli 读取失败: %s: %s", type(e).__name__, e)
                return None
            abs_tmp = REPO_ROOT / tmp
            if proc.returncode != 0 or not abs_tmp.exists():
                logger.warning(
                    "lark-cli 读取失败 (rc=%s): %s",
                    proc.returncode, (proc.stderr or proc.stdout or "").strip()[:300],
                )
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
            # has_more 在 stdout 顶层; 兼容 data 内嵌两种格式
            has_more = False
            try:
                out = json.loads(proc.stdout)
                has_more = bool(out.get("ok") and (out.get("has_more") or out.get("data", {}).get("has_more")))
            except (ValueError, AttributeError):
                pass
            if not has_more:
                break
            offset += _PAGE_SIZE
        return records
    finally:
        _cleanup_tmp(tmp)


def existing_keys(
    base_token: str,
    table_id: str,
    key_fields: tuple[str, ...] | list[str],
    key_norm: Any = None,
) -> set[tuple[str, ...]] | None:
    """查询表内全部已有业务键组合, 用于去重; 失败返回 None。

    key_norm: 可选 (field_name, value) -> str, 默认 default_key_norm。
    """
    norm = key_norm or default_key_norm
    rows = list_records(base_token, table_id, list(key_fields))
    if rows is None:
        return None
    keys: set[tuple[str, ...]] = set()
    for r in rows:
        keys.add(tuple(norm(f, r.get(f)) for f in key_fields))
    return keys


# ---------------------------------------------------------------------------
# 写: 批量创建 / 删除
# ---------------------------------------------------------------------------

def push_records(
    base_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    key_fields: tuple[str, ...] | list[str] | None = None,
    force: bool = False,
    key_norm: Any = None,
    date_fields: tuple[str, ...] | list[str] = (),
) -> PushResult:
    """把记录追加写入多维表格。

    - records 的值用**原始业务值** (日期列给 "YYYY-MM-DD" 字符串, 数字列给数值);
      date_fields 列出的字段会在写盘时统一转 Base 要求的毫秒时间戳。
      这样去重归一化 (str[:10]) 与表内读回的格式一致, 不会发生「毫秒 int vs 日期串」误判。
    - key_fields 给出且非 force 时, 先读表内已有键做去重(安全重复运行);
      读取失败时**中止推送**(ok=False), 避免重复导入。
    """
    details: list[str] = []
    skipped = 0
    todo = records
    if key_fields and not force:
        norm = key_norm or default_key_norm
        existing = existing_keys(base_token, table_id, key_fields, key_norm=norm)
        if existing is None:
            return PushResult(
                ok=False,
                error="无法读取表内已有记录, 为避免重复导入已中止推送 (可 force=True 强制)",
            )
        todo = [
            r for r in records
            if tuple(norm(f, r.get(f)) for f in key_fields) not in existing
        ]
        skipped = len(records) - len(todo)
        if skipped:
            details.append(f"去重: 跳过 {skipped} 条表内已存在的记录")
        if not todo:
            return PushResult(ok=True, pushed=0, skipped=skipped, details=details)

    if not todo:
        return PushResult(ok=True, pushed=0, skipped=skipped, details=details)

    # 日期列转毫秒时间戳 (Base 日期字段只接受 ms); 其余原样
    payload = [
        {k: (to_ms_date(v) if k in date_fields else v) for k, v in r.items()}
        for r in todo
    ]

    # 拆批 + 生成临时 JSON (--json @file 要求相对路径; 只第一批清扫目录, 否则后批把先批的文件清掉)
    batch_files: list[Path] = []
    for i in range(0, len(payload), _BATCH_SIZE):
        rel = _tmp_rel_path("push", suffix=f"_{i // _BATCH_SIZE}.json", clean=(i == 0))
        with (REPO_ROOT / rel).open("w", encoding="utf-8") as f:
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
                return PushResult(ok=False, pushed=pushed, skipped=skipped,
                                  error="找不到 lark-cli, 请先安装: npm install -g @larksuite/cli",
                                  details=details)
            except subprocess.TimeoutExpired:
                return PushResult(ok=False, pushed=pushed, skipped=skipped,
                                  error=f"批次 {rel} 写入超时", details=details)

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


def delete_records(base_token: str, table_id: str, record_ids: list[str]) -> PushResult:
    """按 record_id 删除记录 (清理脏数据用)。必须逐个 --record-id 重复传, 不存在 --record-ids。"""
    if not record_ids:
        return PushResult(ok=True)
    args = [
        "base", "+record-delete",
        "--base-token", base_token, "--table-id", table_id,
        *[a for rid in record_ids for a in ("--record-id", rid)],
        "--as", "user", "--yes",
    ]
    try:
        proc = _run_cli(args, _WRITE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return PushResult(ok=False, error=f"{type(e).__name__}: {e}")
    if proc.returncode != 0:
        return PushResult(ok=False, error=(proc.stderr or proc.stdout or "").strip()[:300])
    return PushResult(ok=True, pushed=len(record_ids))
