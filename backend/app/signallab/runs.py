"""Signal Lab 复盘任务的异步状态机。

为什么不复用 ``app/services/pipeline_jobs``: 那个 store 是**全局单飞**的, 一次只认一个活跃
任务, 全市场数据同步(可跑 20+ 分钟)期间会把复盘请求吞掉 —— 返回 reused 却不是同一个任务,
前端只能干等一个永不产出台账的 job。复盘是纯本地 CPU 计算, 与拉行情不冲突, 因此独立一本账。

与 pipeline_jobs 保持同样的形状(status/stage/progress/log/result), 前端轮询逻辑可以复用。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_MAX_LOG_LINES = 200


class SignalLabRunStore:
    """进程内的复盘任务账本。

    只保留内存: 任务状态丢了无所谓(结果已落盘成 parquet), 落盘反而会在重启后
    留下一批永远不推进的 running 记录。
    """

    def __init__(self, max_runs: int = 30) -> None:
        self._max_runs = max_runs
        self._active: dict[str, dict[str, Any]] = {}
        self._finished: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # ===== 生命周期 =====

    def create(self, payload: dict[str, Any] | None = None) -> tuple[str, bool]:
        """单飞创建任务, 返回 ``(run_id, is_new)``。

        ``is_new=False`` 表示已有复盘在跑, 调用方**不得**再调度新的后台任务。
        """
        with self._lock:
            for run in self._active.values():
                if run["status"] in ("pending", "running"):
                    return str(run["id"]), False
            run_id = uuid.uuid4().hex[:10]
            now = time.time()
            self._active[run_id] = {
                "id": run_id,
                "status": "pending",
                "stage": "init",
                "progress": 0,
                "log": [],
                "request": dict(payload or {}),
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "duration_s": None,
                "result": None,
                "error": None,
            }
            return run_id, True

    def start(self, run_id: str) -> None:
        with self._lock:
            run = self._active.get(run_id)
            if not run:
                return
            run["status"] = "running"
            run["started_at"] = time.time()

    def progress(self, run_id: str, stage: str, pct: int, msg: str = "") -> None:
        with self._lock:
            run = self._active.get(run_id)
            if not run:
                return
            run["stage"] = stage
            run["progress"] = max(0, min(100, int(pct)))
            if msg:
                run["log"].append({"ts": time.time(), "stage": stage, "msg": msg})
                del run["log"][:-_MAX_LOG_LINES]

    def succeed(self, run_id: str, result: dict[str, Any]) -> None:
        self._finish(run_id, "succeeded", result=result)

    def fail(self, run_id: str, error: str) -> None:
        self._finish(run_id, "failed", error=error)

    def _finish(self, run_id: str, status: str, *, result: Any = None,
                error: str | None = None) -> None:
        with self._lock:
            run = self._active.pop(run_id, None)
            if not run:
                return
            run["status"] = status
            run["finished_at"] = time.time()
            run["progress"] = 100 if status == "succeeded" else run["progress"]
            run["result"] = result
            run["error"] = error
            run["duration_s"] = round((run["finished_at"] - (run["started_at"] or run["created_at"])), 2)
            self._finished.insert(0, run)
            del self._finished[self._max_runs:]

    # ===== 查询 =====

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._active.get(run_id)
            if run is not None:
                return dict(run)
            for item in self._finished:
                if item["id"] == run_id:
                    return dict(item)
        return None

    def active_id(self) -> str | None:
        with self._lock:
            for run in self._active.values():
                if run["status"] in ("pending", "running"):
                    return str(run["id"])
        return None

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            active = sorted(
                self._active.values(), key=lambda r: r["created_at"], reverse=True
            )
            items = [dict(r) for r in active] + [dict(r) for r in self._finished]
        return items[:limit]


run_store = SignalLabRunStore()
