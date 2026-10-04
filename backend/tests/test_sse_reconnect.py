"""回测/优化/walk-forward 的 SSE 断点续传 (Last-Event-ID)。

浏览器 EventSource 断线重连时会自动带上最后收到的 ``Last-Event-ID``; 服务端
必须给每条事件编号并从该号之后续推, 否则重连会把上百条历史进度整段重放
(切页回来 / 刷新重连时尤其明显)。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.backtest import _sse_frame, _sse_start_cursor


def _request(headers: dict[str, str] | None = None):
    return SimpleNamespace(headers=headers or {})


def test_start_cursor_without_header_starts_from_zero():
    """首次连接没有 Last-Event-ID -> 从 0 开始, 与加编号之前行为一致。"""
    assert _sse_start_cursor(_request()) == 0
    assert _sse_start_cursor(_request({"last-event-id": ""})) == 0


def test_start_cursor_resumes_after_last_received_id():
    """id 是 1-based, 收到 id=N 说明前 N 条已送达 -> 下一条下标就是 N。"""
    assert _sse_start_cursor(_request({"last-event-id": "5"})) == 5
    assert _sse_start_cursor(_request({"last-event-id": "0"})) == 0


def test_start_cursor_ignores_garbage():
    """脏值不能让流崩溃, 退化成全量重放。"""
    assert _sse_start_cursor(_request({"last-event-id": "abc"})) == 0
    assert _sse_start_cursor(_request({"last-event-id": "-3"})) == 0


def test_frame_carries_id_before_event_line():
    frame = _sse_frame("progress", {"day": 3}, 7)
    assert frame == 'id: 7\nevent: progress\ndata: {"day": 3}\n\n'


def test_frame_without_id_is_plain_event():
    """终态前的前置错误帧不参与续传, 不带 id。"""
    frame = _sse_frame("error", {"message": "boom"})
    assert frame == 'event: error\ndata: {"message": "boom"}\n\n'
    assert "id:" not in frame


def test_frame_keeps_non_ascii_and_non_serializable():
    """中文不转义, 不可序列化对象退化成 str (回测结果里混着 numpy 标量)。"""
    frame = _sse_frame("done", {"label": "完成"}, 1)
    assert 'data: {"label": "完成"}' in frame

    class _Weird:
        def __str__(self) -> str:
            return "weird"

    assert 'data: {"x": "weird"}' in _sse_frame("done", {"x": _Weird()}, 2)
