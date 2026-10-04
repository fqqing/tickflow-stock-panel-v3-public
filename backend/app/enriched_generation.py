from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl

logger = logging.getLogger(__name__)


class EnrichedGenerationUnavailableError(RuntimeError):
    """The enriched dataset has no stable generation available for readers."""


# 孤儿判定: 标记停在 publishing 且属主进程已不在, 说明那次发布永远不会 commit。
# PID 会被操作系统回收复用, 复用后存活探测会误判"属主还在", 因此除了 pid 还要
# 看标记的时间戳 —— 发布期间 ``_touch_publishing_marker`` 会定期续期, 所以
# 阈值可以设得比较短, 又不会误伤真正在跑的长时间发布 (全量重建可达数分钟)。
_ORPHAN_STALE_SECONDS = 300.0
_TOUCH_INTERVAL_SECONDS = 60.0

_WRITER_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_ACTIVE_PUBLICATIONS: weakref.WeakValueDictionary[str, EnrichedPublication] = (
    weakref.WeakValueDictionary()
)
# 当前线程已经持有的锁键 -> 重入深度。``_exclusive_generation_lock`` 用 RLock 做
# 进程内互斥, 于是同一线程嵌套进入是合法的; 但文件锁按句柄生效, 再开一个句柄去锁
# 同一字节必然失败, 会被误报成"另一个发布正在进行"。这里记下重入状态, 嵌套时直接
# 复用外层已持有的文件锁。
_HELD_LOCKS = threading.local()


def _marker_path(data_dir: Path, asset_type: str) -> Path:
    return Path(data_dir) / f".matrix_generation_{asset_type}.json"


def _lock_key(data_dir: Path, asset_type: str) -> tuple[str, str]:
    return (str(Path(data_dir).resolve()), asset_type)


def _writer_lock(data_dir: Path, asset_type: str) -> threading.RLock:
    key = _lock_key(data_dir, asset_type)
    with _WRITER_LOCKS_GUARD:
        return _WRITER_LOCKS.setdefault(key, threading.RLock())


def _held_locks() -> dict[tuple[str, str], int]:
    held = getattr(_HELD_LOCKS, "held", None)
    if held is None:
        held = {}
        _HELD_LOCKS.held = held
    return held


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        )
    return payload


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _unlock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _try_lock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise EnrichedGenerationUnavailableError(
                "another enriched publication is active"
            ) from exc
        return
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise EnrichedGenerationUnavailableError(
            "another enriched publication is active"
        ) from exc


@contextmanager
def _exclusive_generation_lock(data_dir: Path, asset_type: str) -> Iterator[None]:
    key = _lock_key(data_dir, asset_type)
    held = _held_locks()
    depth = held.get(key, 0)
    lock = _writer_lock(data_dir, asset_type)
    with lock:
        if depth:
            # 同一线程重入: 进程内 RLock 已覆盖互斥语义, 文件锁不能再开一个句柄
            # (同进程两个句柄锁同一字节也会失败), 直接复用外层。
            held[key] = depth + 1
            try:
                yield
            finally:
                held[key] = depth
            return
        lock_path = Path(data_dir) / f".matrix_generation_{asset_type}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            _try_lock_file(stream)
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
                _unlock_file(stream)


_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87
_WAIT_TIMEOUT = 258

_WINDOWS_KERNEL32: Any = None


def _windows_kernel32() -> Any:
    """惰性加载 kernel32 并声明函数签名 (指针必须声明为 c_void_p, 否则 64 位被截断)。"""
    global _WINDOWS_KERNEL32
    if _WINDOWS_KERNEL32 is None:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        _WINDOWS_KERNEL32 = kernel32
    return _WINDOWS_KERNEL32


def _windows_process_alive(pid: int) -> bool:
    """Windows 存活探测: 只查询退出状态, 不会误伤目标进程。

    ⚠️ 别用 ``os.kill(pid, 0)``: Windows 上非 0 信号走 TerminateProcess, 且
    CPython 用 ``OpenProcess(PROCESS_ALL_ACCESS)`` 打开 —— 对完整性级别更高或受
    保护的进程会拿到 ERROR_ACCESS_DENIED, "无权打开"与"不存在"混在一起不可靠。
    这里用最小权限组合 + ``WaitForSingleObject``:
      - ERROR_INVALID_PARAMETER (87) => 没有这个 pid
      - 其它打开失败 (含 ACCESS_DENIED) => 保守判活, 宁可让路给别的发布
      - 打开成功 => WAIT_TIMEOUT = 还在跑; WAIT_OBJECT_0 = 已退出
    """
    import ctypes

    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(
        _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid),
    )
    if not handle:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _marker_is_stale(payload: dict[str, Any], *, seconds: float = _ORPHAN_STALE_SECONDS) -> bool:
    """标记的时间戳是否已超过给定阈值 (缺失时间戳一律按过期处理)。"""
    stamp = payload.get("updated_at_ns")
    if not isinstance(stamp, int):
        return True
    return time.time_ns() - stamp > int(seconds * 1_000_000_000)


def _publication_owner_is_active(
    payload: dict[str, Any] | None,
    *,
    same_pid_is_active: bool = True,
) -> bool:
    """publishing 标记的属主是否还在/必须让路。

    - 本进程内还有活着的发布对象 => 是。同进程判定不依赖 pid, 最可靠。
    - 属主就是本进程 (对象已不在) => 由 ``same_pid_is_active`` 决定:
      * **读端传 True (默认)** —— ``abandon`` 在已改动时刻意把标记留在 publishing
        (fail-closed: 半删/半写的目录不能被读端放行, 2026-09-18
        test_data_clear_generation 回归钉死)。
      * **写端接管传 False** —— 下一次写入会整体重写并 commit 出新 generation,
        同进程遗留标记允许被 ``recover=True`` 接管 (恢复出厂语义)。
    - 属主是别的进程, 仍存活 **且标记新鲜** => 是。
    - 属主进程已消失, 或标记已过期 => 否, 这是孤儿。

    "标记新鲜"这一条专门对付 pid 回收复用: 复用后存活探测会误判成"属主还在",
    只靠 pid 判断会让孤儿标记永久卡死 (2026-09-18 实盘踩到, 卡了 2 小时)。
    发布期间 ``_touch_publishing_marker`` 会续期, 所以长时间发布不会被误判。
    """
    if payload is None or payload.get("state", "ready") == "ready":
        return False
    publication_id = payload.get("publication_id")
    if publication_id is not None and _ACTIVE_PUBLICATIONS.get(str(publication_id)) is not None:
        return True
    owner_pid = payload.get("owner_pid")
    if owner_pid == os.getpid():
        return same_pid_is_active
    if not _process_is_alive(owner_pid):
        return False
    return not _marker_is_stale(payload)


def _publication_is_orphan(payload: dict[str, Any] | None) -> bool:
    """publishing 且无人负责 —— 永远不会有人来 commit, 读端可以就地恢复。"""
    if payload is None or payload.get("state", "ready") == "ready":
        return False
    return not _publication_owner_is_active(payload)


def _touch_publishing_marker(path: Path, publication_id: str) -> None:
    """发布期间刷新标记时间戳 (心跳), 让"过期"兜底不会误伤长时间发布。

    只在超过 ``_TOUCH_INTERVAL_SECONDS`` 时才落盘, 避免每个分区都写一次。
    """
    current = _read_marker(path)
    if current is None or current.get("publication_id") != publication_id:
        return
    if not _marker_is_stale(current, seconds=_TOUCH_INTERVAL_SECONDS):
        return
    current["updated_at_ns"] = time.time_ns()
    _write_marker(path, current)


def _recover_orphaned_marker(data_dir: Path, asset_type: str) -> str | None:
    """把孤儿的 publishing 标记恢复成 ready, 返回稳定 generation; 不该恢复则 None。

    读端也必须有这条路: 属主进程崩掉后标记停在 publishing, 若只有写端能 recover,
    则每次 ``get_matrix_data_generation`` 都会永久抛 "being published", 实时行情 /
    选股 / 回测全线降级 (2026-09-18 实盘踩到: 标记卡了 2 小时)。
    恢复沿用标记里原本的 generation —— 那是本次发布前的稳定版本, 与 ``abandon``
    的语义一致; 下一次真正写入会 bump 出新 generation。
    """
    path = _marker_path(data_dir, asset_type)
    try:
        with _exclusive_generation_lock(data_dir, asset_type):
            current = _read_marker(path)
            if current is None:
                return None
            generation = current.get("generation")
            if current.get("state", "ready") == "ready":
                return generation if isinstance(generation, str) and generation else None
            if not _publication_is_orphan(current):
                return None
            if not isinstance(generation, str) or not generation:
                generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
            logger.warning(
                "enriched 发布标记为孤儿, 已恢复为 ready (asset_type=%s, owner_pid=%s, "
                "updated_at_ns=%s, generation=%s)",
                asset_type, current.get("owner_pid"), current.get("updated_at_ns"), generation,
            )
            return generation
    except EnrichedGenerationUnavailableError:
        # 拿不到锁说明确实有别的进程在发布, 交给调用方按"正在发布"处理。
        return None


def _ready_payload(generation: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "generation": generation,
        "updated_at_ns": time.time_ns(),
    }


def get_enriched_generation(
    data_dir: Path,
    asset_type: str = "stock",
    *,
    initialize: bool = True,
) -> str:
    path = _marker_path(data_dir, asset_type)
    payload = _read_marker(path)
    if payload is None:
        if not initialize:
            raise EnrichedGenerationUnavailableError(
                "enriched data generation marker is unavailable"
            )
        with _exclusive_generation_lock(data_dir, asset_type):
            payload = _read_marker(path)
            if payload is None:
                generation = uuid.uuid4().hex
                _write_marker(path, _ready_payload(generation))
                return generation
    state = payload.get("state", "ready")
    generation = payload.get("generation")
    if state == "ready" and isinstance(generation, str) and generation:
        return generation
    # 标记停在 publishing: 属主可能早就崩了。孤儿就地恢复, 否则如实报"正在发布"。
    recovered = _recover_orphaned_marker(data_dir, asset_type)
    if recovered is None:
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )
    return recovered


def enriched_publication_incomplete(
    data_dir: Path,
    asset_type: str = "stock",
) -> bool:
    try:
        payload = _read_marker(_marker_path(data_dir, asset_type))
    except EnrichedGenerationUnavailableError:
        return True
    if payload is None:
        return False
    return (
        payload.get("state", "ready") != "ready"
        or not isinstance(payload.get("generation"), str)
        or not payload["generation"]
    )


def bump_enriched_generation(data_dir: Path, asset_type: str = "stock") -> str:
    path = _marker_path(data_dir, asset_type)
    with _exclusive_generation_lock(data_dir, asset_type):
        current = _read_marker(path)
        if current is not None and current.get("state", "ready") != "ready":
            raise EnrichedGenerationUnavailableError(
                "cannot bump an incomplete enriched publication"
            )
        generation = uuid.uuid4().hex
        _write_marker(path, _ready_payload(generation))
        return generation


class EnrichedPublication:
    """Publish one logical enriched write batch under a stable generation token."""

    def __init__(
        self,
        data_dir: Path,
        asset_type: str = "stock",
        *,
        recover: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.asset_type = asset_type
        self.recover = recover
        self._publishing = False
        self._changed = False
        self._base_generation: str | None = None
        self._publication_id = uuid.uuid4().hex

    def begin(self) -> None:
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            self._claim_or_verify()

    def mark_changed(self) -> None:
        if not self._publishing:
            raise RuntimeError("enriched publication has not started")
        self._changed = True
        # 逐文件调用 (如整目录清空) 时也维持心跳, 否则长时间发布会被当成过期孤儿。
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            self._touch_marker()

    def _touch_marker(self) -> None:
        """给标记续期 (心跳); 必须已在 ``_exclusive_generation_lock`` 内调用。"""
        _touch_publishing_marker(
            _marker_path(self.data_dir, self.asset_type), self._publication_id,
        )

    def abandon(self) -> None:
        if not self._publishing or self._changed:
            return
        path = _marker_path(self.data_dir, self.asset_type)
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            current = _read_marker(path)
            if current is not None and current.get("publication_id") == self._publication_id:
                _write_marker(path, _ready_payload(str(self._base_generation)))
        self._publishing = False

    def write_parquet(self, df: pl.DataFrame, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")
        try:
            df.write_parquet(temporary)
            with temporary.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            with _exclusive_generation_lock(self.data_dir, self.asset_type):
                self._claim_or_verify()
                self._touch_marker()
                os.replace(temporary, out)
                _fsync_directory(out.parent)
                self._changed = True
        finally:
            temporary.unlink(missing_ok=True)

    def commit(self) -> str | None:
        if not self._changed:
            return None
        path = _marker_path(self.data_dir, self.asset_type)
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            current = _read_marker(path)
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
        self._publishing = False
        return generation

    def _claim_or_verify(self) -> None:
        path = _marker_path(self.data_dir, self.asset_type)
        try:
            current = _read_marker(path)
        except EnrichedGenerationUnavailableError:
            if not self.recover:
                raise
            current = None
        if self._publishing:
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            return
        _ACTIVE_PUBLICATIONS[self._publication_id] = self
        if current is not None and current.get("state", "ready") != "ready":
            # 写端接管: 同进程遗留标记不算 "active", 允许 recover=True 重建。
            if _publication_owner_is_active(current, same_pid_is_active=False):
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is active"
                )
            if not self.recover:
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is incomplete"
                )
        generation = None if current is None else current.get("generation")
        if not isinstance(generation, str) or not generation:
            generation = uuid.uuid4().hex
        self._base_generation = generation
        _write_marker(path, {
            "state": "publishing",
            "generation": generation,
            "publication_id": self._publication_id,
            "owner_pid": os.getpid(),
            "updated_at_ns": time.time_ns(),
        })
        self._publishing = True
