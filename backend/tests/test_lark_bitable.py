"""lark_bitable 推送通道单元测试。

全部 monkeypatch 子进程与文件读写, 不真正调 lark-cli / 不联网。
覆盖: 去重 / force / 拆批 / 日期转换 / 失败中止 / 删除命令构造。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.services import lark_bitable as lb


@pytest.fixture()
def cli_ok(monkeypatch, tmp_path):
    """把 lark-cli 子进程替换成可编程的假实现; REPO_ROOT 指到临时目录。"""
    monkeypatch.setattr(lb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lb, "find_lark_cli", lambda: "lark-cli.cmd")
    monkeypatch.setattr(lb, "lark_env", lambda cli_path=None: {})
    calls: list[list[str]] = []

    def install(handler):
        def fake_run(args, timeout):
            calls.append(args)
            return handler(args)
        monkeypatch.setattr(lb, "_run_cli", fake_run)
        return calls

    return install


def _completed(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _write_ndjson(repo_root: Path, rel: str, rows: list[dict]) -> None:
    p = repo_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# 值转换
# ---------------------------------------------------------------------------

def test_to_ms_date():
    assert lb.to_ms_date("2026-09-01") == int(
        __import__("datetime").datetime(2026, 9, 1).timestamp() * 1000
    )
    assert lb.to_ms_date("2026-09-01 15:00:00") == lb.to_ms_date("2026-09-01")
    assert lb.to_ms_date("垃圾") is None
    assert lb.to_ms_date(None) is None


def test_to_num():
    assert lb.to_num("3.14") == 3.14
    assert lb.to_num(7) == 7.0
    assert lb.to_num("-") is None
    assert lb.to_num(None) is None
    assert lb.to_num("nan") is None


def test_default_key_norm():
    assert lb.default_key_norm("信号日期", "2026-09-30 00:00:00") == "2026-09-30"
    assert lb.default_key_norm("代码", " 600519 ") == "600519"
    assert lb.default_key_norm("代码", None) == ""


# ---------------------------------------------------------------------------
# 去重与推送
# ---------------------------------------------------------------------------

def _list_handler_factory(repo_root: Path, existing_rows: list[dict]):
    """造一个 handler: +record-list 写 ndjson, +record-batch-create 回 ok。"""
    created: list[list[dict]] = []

    def handler(args):
        if "+record-list" in args:
            out_rel = args[args.index("--output") + 1]
            _write_ndjson(repo_root, out_rel, existing_rows)
            return _completed(stdout=json.dumps({"ok": True, "has_more": False}))
        if "+record-batch-create" in args:
            json_rel = args[args.index("--json") + 1].lstrip("@")
            batch = json.loads((repo_root / json_rel).read_text(encoding="utf-8"))["create_records"]
            created.append(batch)
            ids = [f"rec{i}" for i in range(len(batch))]
            return _completed(stdout=json.dumps({"ok": True, "data": {"record_id_list": ids}}))
        raise AssertionError(f"unexpected args: {args}")

    return handler, created


def test_push_dedup_skips_existing(cli_ok, tmp_path):
    existing = [{"代码": "600519", "信号日期": "2026-09-30"}]
    handler, created = _list_handler_factory(tmp_path, existing)
    cli_ok(handler)

    records = [
        {"代码": "600519", "信号日期": "2026-09-30"},  # 已存在, 应跳过
        {"代码": "000001", "信号日期": "2026-09-30"},  # 新记录
    ]
    result = lb.push_records("B", "T", records, key_fields=("代码", "信号日期"),
                             date_fields=("信号日期",))
    assert result.ok
    assert result.skipped == 1
    assert result.pushed == 1
    # 只推了 000001, 且日期被转成毫秒 int
    assert len(created) == 1
    assert created[0][0]["代码"] == "000001"
    assert isinstance(created[0][0]["信号日期"], int)


def test_push_all_existing_no_write(cli_ok, tmp_path):
    existing = [{"代码": "600519", "信号日期": "2026-09-30"}]
    handler, created = _list_handler_factory(tmp_path, existing)
    calls = cli_ok(handler)

    result = lb.push_records("B", "T", [{"代码": "600519", "信号日期": "2026-09-30"}],
                             key_fields=("代码", "信号日期"))
    assert result.ok and result.pushed == 0 and result.skipped == 1
    assert created == []
    # 只调用了 record-list, 没有 batch-create
    assert all("+record-batch-create" not in c for c in calls)


def test_push_list_failure_aborts(cli_ok, tmp_path):
    def handler(args):
        return _completed(rc=1, stderr="读取失败")
    cli_ok(handler)

    result = lb.push_records("B", "T", [{"代码": "600519"}], key_fields=("代码",))
    assert not result.ok
    assert "中止" in result.error


def test_push_force_skips_dedup_read(cli_ok, tmp_path):
    calls: list[list[str]] = []

    def handler(args):
        calls.append(args)
        if "+record-batch-create" in args:
            json_rel = args[args.index("--json") + 1].lstrip("@")
            batch = json.loads((tmp_path / json_rel).read_text(encoding="utf-8"))["create_records"]
            return _completed(stdout=json.dumps(
                {"ok": True, "data": {"record_id_list": [f"rec{i}" for i in range(len(batch))]}}))
        raise AssertionError("force 模式不应读取表内记录")
    cli_ok(handler)

    result = lb.push_records("B", "T", [{"代码": "600519"}, {"代码": "000001"}],
                             key_fields=("代码",), force=True)
    assert result.ok and result.pushed == 2
    assert all("+record-list" not in c for c in calls)


def test_push_no_key_fields_pushes_all(cli_ok, tmp_path):
    handler, _ = _list_handler_factory(tmp_path, [])
    cli_ok(handler)
    result = lb.push_records("B", "T", [{"代码": "600519"}, {"代码": "000001"}])
    assert result.ok and result.pushed == 2


def test_push_batch_split(cli_ok, tmp_path):
    handler, created = _list_handler_factory(tmp_path, [])
    cli_ok(handler)
    records = [{"代码": str(i)} for i in range(450)]
    result = lb.push_records("B", "T", records, force=True)
    assert result.ok and result.pushed == 450
    assert [len(b) for b in created] == [200, 200, 50]


def test_push_batch_failure_stops(cli_ok, tmp_path):
    def handler(args):
        if "+record-batch-create" in args:
            return _completed(stdout=json.dumps({"ok": False, "msg": "field not found"}))
        raise AssertionError
    cli_ok(handler)
    result = lb.push_records("B", "T", [{"代码": "1"}, {"代码": "2"}], force=True)
    assert not result.ok and result.pushed == 0
    assert "field not found" in result.error


def test_push_timeout(cli_ok, tmp_path):
    def handler(args):
        raise subprocess.TimeoutExpired(cmd="lark-cli", timeout=180)
    cli_ok(handler)
    result = lb.push_records("B", "T", [{"代码": "1"}], force=True)
    assert not result.ok and "超时" in result.error


def test_date_fields_only_convert_listed(cli_ok, tmp_path):
    handler, created = _list_handler_factory(tmp_path, [])
    cli_ok(handler)
    records = [{"代码": "600519", "信号日期": "2026-09-30", "备注日期": "2026-09-30"}]
    lb.push_records("B", "T", records, force=True, date_fields=("信号日期",))
    row = created[0][0]
    assert isinstance(row["信号日期"], int)
    assert row["备注日期"] == "2026-09-30"  # 未列出的日期字段原样


# ---------------------------------------------------------------------------
# existing_keys / list_records
# ---------------------------------------------------------------------------

def test_existing_keys_normalized(cli_ok, tmp_path):
    rows = [
        {"代码": "600519", "信号日期": "2026-09-30"},
        {"代码": "000001", "信号日期": "2026-09-29"},
    ]
    handler, _ = _list_handler_factory(tmp_path, rows)
    cli_ok(handler)
    keys = lb.existing_keys("B", "T", ("代码", "信号日期"))
    assert keys == {("600519", "2026-09-30"), ("000001", "2026-09-29")}


def test_existing_keys_failure_returns_none(cli_ok):
    def handler(args):
        return _completed(rc=1, stderr="auth expired")
    cli_ok(handler)
    assert lb.existing_keys("B", "T", ("代码",)) is None


def test_list_records_pagination(cli_ok, tmp_path):
    pages = {"0": [{"代码": "a"}], "2000": [{"代码": "b"}]}

    def handler(args):
        offset = args[args.index("--offset") + 1]
        out_rel = args[args.index("--output") + 1]
        _write_ndjson(tmp_path, out_rel, pages[offset])
        has_more = offset == "0"
        return _completed(stdout=json.dumps({"ok": True, "has_more": has_more}))
    cli_ok(handler)

    rows = lb.list_records("B", "T", ["代码"])
    assert [r["代码"] for r in rows] == ["a", "b"]


def test_list_records_missing_output_file(cli_ok):
    def handler(args):
        return _completed(stdout=json.dumps({"ok": True}))  # 没写 output 文件
    cli_ok(handler)
    assert lb.list_records("B", "T", ["代码"]) is None


# ---------------------------------------------------------------------------
# delete_records
# ---------------------------------------------------------------------------

def test_delete_records_command(cli_ok):
    captured: list[list[str]] = []

    def handler(args):
        captured.append(args)
        return _completed(stdout=json.dumps({"ok": True}))
    cli_ok(handler)

    result = lb.delete_records("B", "T", ["rec1", "rec2"])
    assert result.ok and result.pushed == 2
    args = captured[0]
    assert "+record-delete" in args
    assert args.count("--record-id") == 2
    assert "--yes" in args  # 缺 --yes 会报 confirmation_required
    assert "--record-ids" not in args  # 不存在这个参数


def test_delete_records_empty(cli_ok):
    def handler(args):
        raise AssertionError("空列表不应调 CLI")
    cli_ok(handler)
    assert lb.delete_records("B", "T", []).ok


def test_delete_records_failure(cli_ok):
    def handler(args):
        return _completed(rc=1, stderr="record not found")
    cli_ok(handler)
    result = lb.delete_records("B", "T", ["recX"])
    assert not result.ok and "record not found" in result.error
