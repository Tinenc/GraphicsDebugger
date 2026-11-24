from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.connectors.codex_app_server import (
    CodexAppServerConnector, _JsonlProcess, _TransportClosed,
)
from replaylab_agent.connectors.jsonl_transport import NotificationQueue, read_jsonl_records
from replaylab_agent.errors import ConnectorError


FIXTURE = Path(__file__).parent / "fixtures/fake_codex_app_server.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["final_answer", "commentary", "analysis", "oversized"])
async def test_terminal_snapshot_preserves_only_explicit_bounded_final_answer(tmp_path, phase):
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURE), "snapshot-" + phase], cwd=tmp_path, timeout=10,
    )
    try:
        if phase == "oversized":
            with pytest.raises(ConnectorError, match="external Agent response exceeded"):
                await connector.run("explain")
        else:
            result = await connector.run("explain")
            assert result.text == ("Snapshot answer" if phase == "final_answer" else "")
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_real_stdio_large_tool_arguments_and_media_do_not_end_turn(tmp_path):
    timeline = []
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURE), "large-tool-record"], cwd=tmp_path, timeout=10,
    )
    try:
        result = await connector.run("organize", on_timeline=timeline.append)
    finally:
        await connector.close()
    assert result.text == "evidence answer"
    events = [item for item in timeline if item["activityId"] == "large-tool"]
    assert [item["status"] for item in events] == ["running", "completed"]
    assert all(item["argumentNames"] == ["semantic_fields"] for item in events)
    assert len(json.dumps(timeline)) < 16 * 1024
    assert connector._transport.process is None


@pytest.mark.asyncio
async def test_real_stdio_large_rpc_request_and_response_preserve_exact_payload(tmp_path):
    transport = _JsonlProcess((sys.executable, str(FIXTURE)), cwd=tmp_path, env=None)
    value = {"fields": "shader-value-\u6cd5\u7ebf" * 400_000, "tail": {"complete": True}}
    assert len(json.dumps(value).encode()) > 4 * 1024 * 1024
    try:
        result = await transport.request("fixture/echo", value, timeout=10)
        assert result == value
        # The next record remains correctly framed after the oversized echo.
        assert await transport.request("fixture/echo", {"next": 2}, timeout=5) == {"next": 2}
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_larger_wire_limit_does_not_relax_assistant_text_limit(tmp_path):
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURE), "large-agent-record"], cwd=tmp_path, timeout=10,
    )
    try:
        with pytest.raises(ConnectorError, match="external Agent response exceeded"):
            await connector.run("explain")
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_frames_partial_utf8_crlf_and_final_record_without_newline():
    stream = asyncio.StreamReader(limit=8)
    expected = [b'{"value":"\xe6\xb3\x95\xe7\xba\xbf"}\r\n', b'{"n":2}\n', b'{"tail":true}']
    wire = b"".join(expected)

    async def feed():
        for index in range(0, len(wire), 2):
            stream.feed_data(wire[index:index + 2])
            await asyncio.sleep(0)
        stream.feed_eof()

    task = asyncio.create_task(feed())
    actual = [record async for record in read_jsonl_records(stream, 1024)]
    await task
    assert actual == expected
    assert [json.loads(record) for record in actual] == [{"value": "法线"}, {"n": 2}, {"tail": True}]


@pytest.mark.asyncio
async def test_record_limit_counts_newline_and_multiple_messages_independently():
    stream = asyncio.StreamReader(limit=4)
    stream.feed_data(b'1234567\n1234567\n')
    stream.feed_eof()
    assert [record async for record in read_jsonl_records(stream, 8)] == [b'1234567\n'] * 2
    too_long = asyncio.StreamReader(limit=4)
    too_long.feed_data(b'12345678\n')
    too_long.feed_eof()
    with pytest.raises(ConnectorError, match="transport bound"):
        _ = [record async for record in read_jsonl_records(too_long, 8)]


@pytest.mark.asyncio
async def test_partial_record_read_is_cancellable():
    stream = asyncio.StreamReader(limit=4)
    stream.feed_data(b'{"unfinished":')
    iterator = read_jsonl_records(stream, 1024)
    task = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_notification_backpressure_preserves_values_order_and_byte_accounting():
    queue = NotificationQueue(maxsize=256, byte_watermark=10)
    await queue.put({"first": "complete payload"}, wire_bytes=12)
    waiting = asyncio.create_task(queue.put({"second": "also complete"}, wire_bytes=5))
    await asyncio.sleep(0)
    assert not waiting.done() and queue.full() and queue.queued_wire_bytes == 12
    assert await queue.get() == {"first": "complete payload"}
    await asyncio.wait_for(waiting, 1)
    assert queue.queued_wire_bytes == 5
    assert await queue.get() == {"second": "also complete"}
    assert queue.queued_wire_bytes == 0 and queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("maxsize", [2, 256])
async def test_terminal_notification_has_reserved_slot_without_dropping_activity(tmp_path, maxsize):
    transport = _JsonlProcess(("unused",), cwd=tmp_path, env=None)
    transport.notifications = NotificationQueue(maxsize=maxsize, byte_watermark=10)
    await transport.notifications.put({"activity": "small"}, wire_bytes=1)
    await transport.notifications.put({"activity": "large"}, wire_bytes=20)
    marker = _TransportClosed(ConnectorError("connection failed"))
    transport._put_priority_notification(marker)
    transport._put_priority_notification(_TransportClosed(ConnectorError("duplicate close")))
    assert transport.notifications.qsize() == 3
    assert transport.notifications.get_nowait() == {"activity": "small"}
    assert transport.notifications.get_nowait() == {"activity": "large"}
    assert transport.notifications.get_nowait() is marker
    assert transport.notifications.queued_wire_bytes == 0


@pytest.mark.asyncio
async def test_real_stdio_notifications_before_rpc_ack_spill_and_deliver_once(tmp_path, monkeypatch):
    spools = []
    original = NotificationQueue._put_reserved
    def record_spool(queue, item):
        if queue._spool is not None and queue._spool not in spools:
            spools.append(queue._spool)
        return original(queue, item)
    monkeypatch.setattr(NotificationQueue, "_put_reserved", record_spool)
    timeline = []
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURE), "pre-ack-large-notifications"], cwd=tmp_path, timeout=15,
    )
    try:
        result = await connector.run("organize", on_timeline=timeline.append)
    finally:
        await connector.close()
    assert result.text == "evidence answer"
    actual = [(item["activityId"], item["status"]) for item in timeline
              if item["activityId"].startswith("pre-ack-")]
    assert actual == [(f"pre-ack-{index}", status) for index in range(3)
                      for status in ("running", "completed")]
    assert spools and all(spool.closed for spool in spools)


@pytest.mark.asyncio
async def test_new_rpc_unblocks_existing_backpressure_and_spool_stays_fifo():
    queue = NotificationQueue(maxsize=2, byte_watermark=10)
    first, second, third = {"n": 1}, {"n": 2}, {"n": 3}
    await queue.put(first, wire_bytes=11)
    blocked = asyncio.create_task(queue.put(second, wire_bytes=10,
        wire_record=json.dumps(second).encode()))
    await asyncio.sleep(0)
    assert not blocked.done()
    queue.set_rpc_pending(True)
    await asyncio.wait_for(blocked, 1)
    spool = queue._spool
    assert spool is not None
    assert await queue.get() == first
    # A later in-memory event must not overtake the already spooled event.
    await queue.put(third, wire_bytes=3)
    assert await queue.get() == second
    assert await queue.get() == third
    assert spool.closed and queue._spool is None


@pytest.mark.asyncio
async def test_spool_bound_preserves_prior_notifications_and_close_deletes_file(tmp_path):
    transport = _JsonlProcess(("unused",), cwd=tmp_path, env=None)
    queue = transport.notifications = NotificationQueue(maxsize=1, byte_watermark=5, spool_limit=8)
    await queue.put({"n": 1}, wire_bytes=5)
    await queue.put({"n": 2}, wire_record=b'{"n":2}', rpc_pending=True)
    spool = queue._spool
    with pytest.raises(ConnectorError, match="bounded temporary storage"):
        await queue.put({"n": 3}, wire_record=b'{"n":3}', rpc_pending=True)
    assert queue.qsize() == 2 and queue._spool_size == 7
    await transport.close()
    assert spool.closed and queue._spool is None


@pytest.mark.asyncio
async def test_spool_io_failures_are_explicit_and_cancellation_cleans_up(tmp_path):
    transport = _JsonlProcess(("unused",), cwd=tmp_path, env=None)
    queue = transport.notifications = NotificationQueue(maxsize=1, byte_watermark=5)
    await queue.put({"n": 1}, wire_bytes=5)
    await queue.put({"n": 2}, wire_record=b'{"n":2}', rpc_pending=True)
    spool = queue._spool
    spool.seek(0)
    spool.truncate(1)
    assert queue.get_nowait() == {"n": 1}
    with pytest.raises(ConnectorError, match="temporary backlog read failed"):
        queue.get_nowait()
    async def cancelled_close():
        raise asyncio.CancelledError
    transport._close_process = cancelled_close
    with pytest.raises(asyncio.CancelledError):
        await transport.close()
    assert spool.closed
    with pytest.raises(ConnectorError, match="queue is closed"):
        await queue.put({"n": 3}, wire_record=b'{"n":3}', rpc_pending=True)


@pytest.mark.asyncio
async def test_spool_write_failure_does_not_silently_lose_notification(monkeypatch):
    import replaylab_agent.connectors.jsonl_transport as framing
    queue = NotificationQueue(maxsize=1, byte_watermark=5)
    await queue.put({"n": 1}, wire_bytes=5)
    def unavailable(**kwargs):
        raise OSError("temporary storage unavailable")
    monkeypatch.setattr(framing.tempfile, "TemporaryFile", unavailable)
    with pytest.raises(ConnectorError, match="temporary backlog write failed"):
        await queue.put({"n": 2}, wire_record=b'{"n":2}', rpc_pending=True)
    assert queue.qsize() == 1 and queue.get_nowait() == {"n": 1}
    queue.close()


@pytest.mark.asyncio
async def test_cancel_during_close_still_reaps_real_process_readers_and_spool(tmp_path):
    transport = _JsonlProcess((sys.executable, "-c", "import time; time.sleep(60)"),
        cwd=tmp_path, env=None)
    await transport.start()
    process, reader, stderr = transport.process, transport._reader_task, transport._stderr_task
    entered = asyncio.Event()
    async def delayed_stdin_close():
        entered.set()
        await asyncio.Event().wait()
    process.stdin.wait_closed = delayed_stdin_close
    queue = transport.notifications = NotificationQueue(maxsize=1, byte_watermark=5)
    await queue.put({"n": 1}, wire_bytes=5)
    await queue.put({"n": 2}, wire_record=b'{"n":2}', rpc_pending=True)
    spool = queue._spool
    close_task = asyncio.create_task(transport.close())
    await asyncio.wait_for(entered.wait(), 2)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close_task, 6)
    assert process.returncode is not None and transport.process is None
    assert reader.done() and stderr.done() and spool.closed
    assert transport._close_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [b'{"id":1,"result":', b'not json\n', b'{"x":"\xff"}\n', b'[]\n'])
async def test_malformed_wire_fails_pending_requests_and_keeps_terminal_signal(tmp_path, wire):
    stream = asyncio.StreamReader(limit=8)
    stream.feed_data(wire)
    stream.feed_eof()
    transport = _JsonlProcess(("unused",), cwd=tmp_path, env=None)
    transport.process = SimpleNamespace(stdout=stream, returncode=0)
    pending = asyncio.get_running_loop().create_future()
    transport._pending[1] = pending
    await transport._read_stdout()
    with pytest.raises(ConnectorError):
        await pending
    assert isinstance(transport.notifications.get_nowait(), _TransportClosed)


@pytest.mark.asyncio
async def test_wire_overflow_does_not_skip_record_and_accept_later_reply(tmp_path, monkeypatch):
    import replaylab_agent.connectors.codex_app_server as codex
    monkeypatch.setattr(codex, "_MAX_JSONL_MESSAGE_BYTES", 32)
    stream = asyncio.StreamReader(limit=8)
    stream.feed_data(b'x' * 33 + b'\n{"id":1,"result":{"ok":true}}\n')
    stream.feed_eof()
    transport = _JsonlProcess(("unused",), cwd=tmp_path, env=None)
    transport.process = SimpleNamespace(stdout=stream, returncode=0)
    pending = asyncio.get_running_loop().create_future()
    transport._pending[1] = pending
    await transport._read_stdout()
    with pytest.raises(ConnectorError, match="transport bound"):
        await pending
    assert isinstance(transport.notifications.get_nowait(), _TransportClosed)
