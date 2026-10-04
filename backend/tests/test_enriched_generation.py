from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, PanelCache
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    _exclusive_generation_lock,
    _marker_path,
    _touch_publishing_marker,
    _windows_process_alive,
    get_enriched_generation,
)
from app.tickflow.repository import DataStore, KlineRepository


def _frame(value: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date(2026, 8, 14)],
        "open": [value],
        "high": [value],
        "low": [value],
        "close": [value],
        "volume": [1_000.0],
    })


def test_repository_enriched_noop_does_not_bump_generation(tmp_path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    frame = _frame()

    repo.append_enriched(frame)
    first = repo.get_matrix_data_generation("stock")
    repo.append_enriched(frame)

    assert repo.get_matrix_data_generation("stock") == first


def test_failed_multi_partition_publication_remains_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    publication = EnrichedPublication(tmp_path, recover=True)
    first = tmp_path / "kline_daily_enriched" / "date=2026-08-13" / "part.parquet"
    second = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    publication.write_parquet(_frame(10.0), first)

    original_write = pl.DataFrame.write_parquet

    def fail_second(self, path, *args, **kwargs):
        if "2026-08-14" in str(path):
            raise OSError("injected write failure")
        return original_write(self, path, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", fail_second)
    with pytest.raises(OSError, match="injected"):
        publication.write_parquet(_frame(11.0), second)

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "publishing"
    assert first.is_file()
    assert not second.is_file()
    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")


def test_recovery_replaces_stale_publication_but_not_active_owner(tmp_path) -> None:
    first = EnrichedPublication(tmp_path, recover=True)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    first.write_parquet(_frame(10.0), out)

    with pytest.raises(EnrichedGenerationUnavailableError, match="active"):
        EnrichedPublication(tmp_path, recover=True).write_parquet(_frame(11.0), out)

    del first
    recovered = EnrichedPublication(tmp_path, recover=True)
    recovered.write_parquet(_frame(12.0), out)
    recovered_generation = recovered.commit()

    assert recovered_generation == get_enriched_generation(tmp_path, "stock")
    assert pl.read_parquet(out)["close"].item() == pytest.approx(12.0)


def _live_foreign_pid() -> subprocess.Popen:
    """起一个真活着的子进程, 用它模拟"别人家的 pid" (含被回收复用的那种)。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(1.0)
    assert child.poll() is None, "子进程没起来, 测试前提不成立"
    return child


def _write_marker(data_dir, *, owner_pid: int | str, updated_at_ns: int | str) -> None:
    (data_dir / ".matrix_generation_stock.json").write_text(
        json.dumps({
            "state": "publishing",
            "generation": "stale-generation",
            "publication_id": "stale-publication",
            "owner_pid": owner_pid,
            "updated_at_ns": updated_at_ns,
        }),
        encoding="utf-8",
    )


@pytest.mark.skipif(os.name != "nt", reason="只验证 Windows 存活探测")
def test_windows_process_probe_rejects_unknown_pid() -> None:
    # ⚠️ 这条钉住"不用 os.kill(pid, 0)"的原因: Windows 上 os.kill 走
    # OpenProcess(PROCESS_ALL_ACCESS) + TerminateProcess, 对受保护进程会拿到
    # ACCESS_DENIED, 把"无权打开"当成"不存在/存在"都不可靠, 且理论上会误杀目标。
    # 改成最小权限打开 + WaitForSingleObject 之后, 不存在的 pid 必须判 False。
    assert _windows_process_alive(0xFFFFFFF0) is False
    assert _windows_process_alive(os.getpid()) is True


def test_stale_marker_with_recycled_pid_is_treated_as_orphan(tmp_path) -> None:
    """pid 被回收复用后, 存活探测会误判"属主还在" —— 必须靠时间戳兜底。

    否则孤儿标记会永久卡死 (2026-09-18 实盘: 标记卡了 2 小时, 实时行情全线降级)。
    """
    child = _live_foreign_pid()
    try:
        _write_marker(tmp_path, owner_pid=child.pid, updated_at_ns=0)
        out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
        publication = EnrichedPublication(tmp_path, recover=True)
        publication.write_parquet(_frame(10.0), out)
        publication.commit()

        marker = json.loads(
            (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
        )
        assert marker["state"] == "ready"
        assert marker["generation"] != "stale-generation"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_fresh_marker_with_live_owner_still_blocks_recovery(tmp_path) -> None:
    """对照组: 属主进程真的还活着且标记新鲜 -> 必须让路, 不能被抢。"""
    child = _live_foreign_pid()
    try:
        _write_marker(tmp_path, owner_pid=child.pid, updated_at_ns=time.time_ns())
        out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
        with pytest.raises(EnrichedGenerationUnavailableError, match="active"):
            EnrichedPublication(tmp_path, recover=True).write_parquet(_frame(10.0), out)
        with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
            get_enriched_generation(tmp_path, "stock")
    finally:
        child.kill()
        child.wait(timeout=10)


def test_reader_recovers_orphaned_marker(tmp_path) -> None:
    """根因回归: 读端必须能自愈孤儿标记。

    以前只有写端带 recover, 读端 (get_matrix_data_generation -> 这里) 会永久抛
    "being published", 于是 enriched 缓存刷新 / 实时行情 / 回测全被一个死 pid 卡死。
    """
    _write_marker(tmp_path, owner_pid=999999999, updated_at_ns=0)

    generation = get_enriched_generation(tmp_path, "stock")

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
    # 恢复沿用发布前的稳定 generation, 下一次真正写入才 bump
    assert marker["generation"] == "stale-generation"
    assert generation == "stale-generation"


def test_reader_recovery_skipped_when_publication_owner_is_live(tmp_path) -> None:
    """读端自愈不能越权: 本进程内还有活着的发布对象时必须如实报"正在发布"。"""
    publication = EnrichedPublication(tmp_path, recover=True)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    publication.write_parquet(_frame(10.0), out)

    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")

    publication.commit()
    assert get_enriched_generation(tmp_path, "stock")


def test_heartbeat_refreshes_only_when_stale(tmp_path) -> None:
    """心跳: 过期才续期, 新鲜就跳过 (避免每个分区都落盘)。"""
    path = _marker_path(tmp_path, "stock")
    _write_marker(tmp_path, owner_pid=os.getpid(), updated_at_ns=0)

    _touch_publishing_marker(path, "stale-publication")
    refreshed = json.loads(path.read_text(encoding="utf-8"))["updated_at_ns"]
    assert refreshed > 0

    time.sleep(0.01)
    _touch_publishing_marker(path, "stale-publication")
    assert json.loads(path.read_text(encoding="utf-8"))["updated_at_ns"] == refreshed

    _touch_publishing_marker(path, "another-publication")
    assert json.loads(path.read_text(encoding="utf-8"))["updated_at_ns"] == refreshed


def test_exclusive_generation_lock_allows_same_thread_reentry(tmp_path) -> None:
    """同一线程嵌套进入必须复用外层文件锁。

    _writer_lock 是 RLock (同线程可重入), 但文件锁按句柄生效 —— 嵌套时再开一个
    句柄去锁同一字节必然失败, 被误报成 "another enriched publication is active"
    (2026-09-18 实测确认, 且报错文案与真实竞争完全一样, 极难排查)。
    """
    with ExitStack() as stack:
        for _ in range(3):  # 同时持有 3 层, 模拟调用链嵌套
            stack.enter_context(_exclusive_generation_lock(tmp_path, "stock"))
    # 退出后锁必须真的释放, 别人能拿到
    with _exclusive_generation_lock(tmp_path, "stock"):
        pass


def test_panel_cache_generation_change_forces_recompute() -> None:
    cache = PanelCache()
    calls: list[int] = []
    args = (["000001.SZ"], date(2026, 8, 13), date(2026, 8, 14), None)

    def compute(*_args):
        calls.append(len(calls) + 1)
        return pl.DataFrame({"value": [calls[-1]]})

    first = cache.get_or_compute(*args, compute, "stock", "generation-a")
    second = cache.get_or_compute(*args, compute, "stock", "generation-b")

    assert first["value"].item() == 1
    assert second["value"].item() == 2
    assert cache.stats()["compute_count"] == 2


def test_panel_reader_retries_when_generation_changes_during_scan(tmp_path) -> None:
    generations = iter(["generation-a", "generation-b", "generation-b", "generation-b"])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda _asset_type: next(generations),
    )
    engine = BacktestEngine(repo)
    calls: list[str] = []

    def load(*_args):
        calls.append("scan")
        return _frame(float(len(calls)))

    engine._load_panel_inner = load
    panel = engine.load_panel(
        None,
        date(2026, 8, 14),
        date(2026, 8, 14),
        columns=["symbol", "date", "close"],
    )

    assert calls == ["scan", "scan"]
    assert panel["close"].item() == pytest.approx(2.0)


def test_matrix_reader_retries_when_generation_changes_during_build(
    tmp_path,
    monkeypatch,
) -> None:
    generations = iter(["generation-a", "generation-b", "generation-b", "generation-b"])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda _asset_type: next(generations),
        get_instruments_asset=lambda _asset_type, _market="cn": pl.DataFrame(),
    )
    engine = BacktestEngine(repo)
    calls: list[str | None] = []
    expected = SimpleNamespace(
        execution_backend="matrix_native",
        base_columns={"open", "high", "low", "close", "volume"},
        instrument_columns=set(),
        matrix_columns=set(),
    )
    market = SimpleNamespace()

    def load_matrix(*_args, source_generation=None, **_kwargs):
        calls.append(source_generation)
        return market

    monkeypatch.setattr("app.backtest.engine.load_market_data_matrix_from_parquet", load_matrix)

    result = engine.load_market_data_matrix_for_backtest(
        None,
        date(2026, 8, 14),
        date(2026, 8, 14),
        expected,
    )

    assert result is market
    assert calls == ["generation-a", "generation-b"]


def test_live_flush_write_recovers_stale_marker_from_dead_process(
    tmp_path, monkeypatch
) -> None:
    """实时 enriched 落盘(repository 路径)遇到僵死 publishing 标记应接管自愈,
    而非持续抛错直到下一次盘后管道。"""
    from app.tickflow.repository import DataStore, KlineRepository

    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps({
            "state": "publishing",
            "generation": "stale-generation",
            "publication_id": "stale-publication",
            "owner_pid": 999999999,
            "updated_at_ns": 0,
        }),
        encoding="utf-8",
    )

    repo = KlineRepository(DataStore(tmp_path))
    repo.append_enriched(_frame(10.0))

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
