"""每日盘后无人值守推送: 自举后端 -> 触发全市场同步 -> 推送飞书。

⚠️ **面板推送上线后, 常用路径改为前端**: 策略页「推飞书」按钮选策略 + 选日期
即时推 (走 /api/lark/push), 推送结果直接在面板上可见。本脚本保留给「机器整天
没开、需要自举 + 补同步」的无人值守场景 —— 它比面板多做的就这两件事。

针对「项目不一定每天都启动 / 盘后不一定开着」的两个兜底:
  1. **后端自举**: 健康检查不通就自己拉起 uvicorn(独立进程, 与本脚本解耦),
     轮询 /health 等它预热完(enriched 重算可能要几分钟)再继续。
  2. **自动同步**: 拉起后先跑一次 /api/pipeline/run 全市场同步, 保证数据到最新
     交易日 —— 否则机器整天没开时, 推的是上次开机那天的陈旧结果。
  3. **幂等**: 推送按 (代码, 日期) 去重, 重复运行安全, 所以错过几天后补跑也不会
     灌重复数据。配合计划任务的 StartWhenAvailable, 关机错过则在下次开机后自动补。

用法::

    python backend/scripts/daily_lark_push.py                # 全量: 拉起 + 同步 + 推送
    python backend/scripts/daily_lark_push.py --no-sync      # 已有新鲜数据时跳过同步
    python backend/scripts/daily_lark_push.py --dry-run      # 只跑流程不写飞书

退出码: 0 成功 / 1 推送阶段有失败 / 2 后端起不来或同步失败。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BACKEND_DIR / "scripts"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

# 加载根 .env, 让定时任务启动时也能读到 DAILY_LARK_PUSH_ENABLED 开关
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR.parent / ".env")

_ENABLED_FLAG = str(os.environ.get("DAILY_LARK_PUSH_ENABLED", "true")).strip().lower()
if _ENABLED_FLAG in ("0", "false", "no", "disabled"):
    print("[daily_lark_push] 已禁用: DAILY_LARK_PUSH_ENABLED=" + _ENABLED_FLAG)
    sys.exit(0)

import push_screener_to_lark as push  # noqa: E402

logger = logging.getLogger("daily_lark_push")

LOG_DIR = BACKEND_DIR.parent / "data" / "logs"

# 本机调用必须绕过代理(沙箱/终端常设 HTTP_PROXY, 后端重启间隙会返 502 而非连接拒绝)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_POLL_INTERVAL = 10.0


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


def _health(backend: str, timeout: int = 5) -> bool:
    """后端健康检查 (/health 200 即视为就绪)。"""
    try:
        with _OPENER.open(f"{backend}/health", timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def _ensure_backend(backend: str, boot_timeout: int) -> tuple[bool, subprocess.Popen | None]:
    """后端不在就拉起, 然后轮询等就绪。返回 (是否可用, 由本脚本拉起的进程)。

    进程对象只在**本次拉起**时非 None, 便于 --stop-after 收尾时关掉它;
    本来就跑着的后端不会被动。
    """
    if _health(backend):
        logger.info("后端已在运行")
        return True, None

    port = urlparse(backend).port or 3018
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    boot_log = LOG_DIR / "backend-boot.log"
    logger.info("后端未运行, 拉起中 (port=%s, 日志 %s)", port, boot_log)
    kwargs: dict = {"cwd": str(BACKEND_DIR), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    with boot_log.open("a", encoding="utf-8") as f:
        kwargs["stdout"] = f
        kwargs["stderr"] = subprocess.STDOUT
        if os.name == "nt":
            # 脱离本进程: 计划任务结束时不会把它一起带走
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
            **kwargs,
        )

    deadline = time.monotonic() + boot_timeout
    waited = 0.0
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        if _health(backend):
            logger.info("后端已就绪 (等待 %.0fs)", waited)
            return True, proc
    logger.error("后端在 %ss 内未就绪", boot_timeout)
    proc.terminate()
    return False, proc


def _wait_data_ready(backend: str, timeout: int) -> bool:
    """等 enriched 历史指标重算完成。

    /health 200 只说明 uvicorn 活着, enriched 要重算 100+ 万行历史指标, 期间
    /api/abnormal/overview 的 cache_date 为空、选股结果也会失真(实测刚起来时
    查到 0 只)。故用 cache_date 作为「数据可用」信号再继续。
    """
    deadline = time.monotonic() + timeout
    waited = 0.0
    while time.monotonic() < deadline:
        try:
            resp = _http_json(f"{backend}/api/abnormal/overview?limit=1", timeout=60)
        except RuntimeError:
            resp = {}
        cache_date = str(resp.get("cache_date") or "")
        if cache_date:
            logger.info("数据已就绪 (enriched cache_date=%s, 等待 %.0fs)", cache_date, waited)
            return True
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    logger.warning("等待数据就绪超时 (%ss), 继续尝试", timeout)
    return False


def _run_sync(backend: str, timeout: int) -> bool:
    """触发全市场同步并等它跑完。返回是否成功。"""
    try:
        resp = _http_json(f"{backend}/api/pipeline/run", {}, timeout=60)
    except RuntimeError as e:
        logger.error("触发同步失败: %s", e)
        return False
    job_id = resp.get("job_id")
    logger.info("同步任务 %s (reused=%s)", job_id, resp.get("reused"))

    deadline = time.monotonic() + timeout
    last_stage = ""
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            j = _http_json(f"{backend}/api/pipeline/jobs/{job_id}", timeout=60)
        except RuntimeError as e:
            logger.warning("轮询任务状态失败: %s", e)
            continue
        status = str(j.get("status") or "")
        stage = f"{status} {j.get('stage') or ''} {j.get('progress') or ''}%".strip()
        if stage != last_stage:
            logger.info("  同步进度: %s", stage)
            last_stage = stage
        if status in ("succeeded", "failed", "cancelled"):
            logger.info("同步结束: %s", status)
            return status == "succeeded"
    logger.error("同步在 %ss 内未结束", timeout)
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="每日盘后无人值守: 自举后端 + 同步 + 推送飞书")
    p.add_argument("--backend", default="http://127.0.0.1:3018", help="后端地址")
    p.add_argument("--no-sync", action="store_true", help="跳过全市场同步直接推送")
    p.add_argument("--boot-timeout", type=int, default=900, help="等后端就绪秒数 (默认 900)")
    p.add_argument("--sync-timeout", type=int, default=2400, help="等同步完成秒数 (默认 2400)")
    p.add_argument("--ready-timeout", type=int, default=900, help="等 enriched 就绪秒数 (默认 900)")
    p.add_argument("--only", default=None, help="只跑指定数据源, 逗号分隔")
    p.add_argument("--as-of", default=None, help="指定交易日 YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="只跑流程不写飞书")
    p.add_argument("--force", action="store_true", help="跳过去重")
    p.add_argument(
        "--abnormal-min-closeness", type=float, default=0.7,
        help="异动接近度下限 0.5/0.7/1.0, 默认 0.7",
    )
    p.add_argument("--ignore-weekend", action="store_true", help="周末也跑 (默认周六日直接跳过)")
    p.add_argument(
        "--stop-after", action="store_true",
        help="推送结束后关掉**本次拉起**的后端 (后端峰值内存约 7GB, 无人值守场景省资源)",
    )
    args = p.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "daily_lark_push.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger.info("=== 每日推送开始 (weekday=%s) ===", date.today().weekday())

    if not args.ignore_weekend and date.today().weekday() >= 5:
        logger.info("周末, 跳过")
        return 0

    backend = args.backend.rstrip("/")
    ok, proc = _ensure_backend(backend, args.boot_timeout)
    if not ok:
        return 2

    code = 2
    try:
        if args.no_sync:
            _wait_data_ready(backend, args.ready_timeout)
        if args.no_sync or _run_sync(backend, args.sync_timeout):
            push_args = argparse.Namespace(
                backend=backend,
                only=args.only,
                as_of=args.as_of,
                dry_run=args.dry_run,
                force=args.force,
                no_enrich=False,
                abnormal_min_closeness=args.abnormal_min_closeness,
            )
            code = push.run(push_args)
    finally:
        if proc is not None and args.stop_after:
            logger.info("关闭本次拉起的后端 (pid=%s)", proc.pid)
            proc.terminate()

    logger.info("=== 每日推送结束 (exit=%s) ===", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
