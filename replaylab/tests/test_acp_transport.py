from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from replaylab_agent.connectors.acp import ACPConnector, _load_acp_sdk
from replaylab_agent.connectors.acp_transport import (
    BoundedACPTransport, _BoundedUpdateClient, spawn_bounded_agent_process,
)
from replaylab_agent.errors import ConnectorError


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows console ownership")
async def test_background_acp_process_has_no_console_and_is_reaped():
    from replaylab_agent.connectors.acp_transport import _hidden_stdio_transport
    script = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow(), flush=True); input()"
    async with _hidden_stdio_transport(sys.executable, "-c", script, shutdown_timeout=1) as (reader, writer, process):
        assert (await asyncio.wait_for(reader.readline(), 5)).strip() == b"0"
        assert process.returncode is None
    assert process.returncode is not None


class Writer:
    def __init__(self):
        self.payloads = []
        self.closed = False
    def write(self, value):
        self.payloads.append(value)
    async def drain(self):
        pass
    def close(self):
        self.closed = True
    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_acp_real_sdk_preserves_large_tool_records_and_finishes(tmp_path):
    fixture = Path(__file__).parent / "fixtures/fake_acp_agent.py"
    connector = ACPConnector([sys.executable, str(fixture), "no-mcp", "large-tool-record"],
        cwd=tmp_path, timeout=20)
    assert _load_acp_sdk().spawn_agent_process is spawn_bounded_agent_process
    result = await connector.test_model(prompt="Reply with exactly: OK")
    assert result["ok"] and result["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_acp_send_and_receive_preserve_complete_unicode_and_media_records():
    reader, writer = asyncio.StreamReader(limit=8), Writer()
    transport = BoundedACPTransport(reader, writer)
    message = {"method": "session/update", "params": {"text": "法线" * 4_000_000,
        "tail": {"mimeType": "image/png", "data": "abc123"}}}
    await transport.send(message)
    assert len(writer.payloads[0]) > 16 * 1024 * 1024
    async def feed():
        wire = writer.payloads[0]
        for offset in range(0, len(wire), 65536):
            reader.feed_data(wire[offset:offset + 65536])
            await asyncio.sleep(0)
        reader.feed_eof()
    feeder = asyncio.create_task(feed())
    assert await transport.receive() == message
    assert await transport.receive() is None
    await feeder
    await transport.close()
    assert writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [b"x" * 33 + b"\n", b"not-json\n", b"[]\n", b'{"x":"\xff"}\n'])
async def test_acp_invalid_or_oversized_message_cannot_be_skipped(wire):
    reader = asyncio.StreamReader(limit=4)
    reader.feed_data(wire + b'{"id":1,"result":{}}\n')
    reader.feed_eof()
    transport = BoundedACPTransport(reader, Writer(), maximum=32)
    with pytest.raises(ConnectorError):
        await transport.receive()
    assert transport.failure is not None
    with pytest.raises(ConnectorError):
        await transport.receive()


@pytest.mark.asyncio
async def test_acp_outgoing_overflow_is_bounded_before_writing():
    writer = Writer()
    transport = BoundedACPTransport(asyncio.StreamReader(), writer, maximum=32)
    with pytest.raises(ConnectorError, match="owned file reference"):
        await transport.send({"bulk": "x" * 33})
    assert not writer.payloads


@pytest.mark.asyncio
async def test_acp_partial_read_is_cancellable():
    reader = asyncio.StreamReader(limit=4)
    reader.feed_data(b'{"incomplete":')
    transport = BoundedACPTransport(reader, Writer())
    pending = asyncio.create_task(transport.receive())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await transport.close()


@pytest.mark.asyncio
async def test_acp_new_send_waits_before_encoding_bulk_data(monkeypatch):
    import replaylab_agent.connectors.acp_transport as module
    writer = Writer()
    entered, release = asyncio.Event(), asyncio.Event()
    async def drain():
        entered.set()
        await release.wait()
    writer.drain = drain
    calls, original = [], module.json.dumps
    def encode(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module.json, "dumps", encode)
    transport = BoundedACPTransport(asyncio.StreamReader(), writer)
    first = asyncio.create_task(transport.send({"first": 1}))
    await entered.wait()
    second = asyncio.create_task(transport.send({"second": 2}))
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()
    await asyncio.gather(first, second)
    assert [json.loads(value) for value in writer.payloads] == [{"first": 1}, {"second": 2}]


@pytest.mark.asyncio
async def test_slow_update_callbacks_keep_aggregate_payload_memory_bounded(monkeypatch):
    import replaylab_agent.connectors.acp_transport as module
    monkeypatch.setattr(module, "ACP_MAX_PENDING_UPDATES", 2)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"method":"session/update","params":{}}\n' * 3)
    transport = BoundedACPTransport(reader, Writer())
    await transport.receive()
    await transport.receive()
    with pytest.raises(ConnectorError, match="update backlog"):
        await transport.receive()
    assert transport._pending_updates == 2


@pytest.mark.asyncio
async def test_update_budget_is_released_after_callback_and_on_cancellation():
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"method":"session/update","params":{}}\n')
    transport = BoundedACPTransport(reader, Writer())
    await transport.receive()
    started = asyncio.Event()
    class Client:
        async def session_update(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
    client = _BoundedUpdateClient(Client(), transport)
    task = asyncio.create_task(client.session_update("session", {}))
    await started.wait()
    assert transport._pending_updates == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport._pending_updates == transport._pending_update_bytes == 0


@pytest.mark.asyncio
async def test_real_invalid_wire_preserves_error_instead_of_cleanup_or_generic_disconnect(tmp_path):
    command = "import sys; sys.stdin.readline(); print('not-json', flush=True)"
    connector = ACPConnector([sys.executable, "-c", command], cwd=tmp_path, timeout=5)
    with pytest.raises(ConnectorError, match="ACP agent emitted invalid JSONL"):
        await connector.test_connection()
