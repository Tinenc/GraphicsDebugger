"""Real local MCP transport failures must not cancel unrelated connections.

No account, external network service or model is contacted. The small stdio
servers implement the MCP JSON-RPC handshake and a callable echo tool; the
HTTP peer deliberately fails or never responds. All files live in pytest's
system temporary directory.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest

from replaylab_agent.external_mcp import ExternalMCPPool


STDIO_SERVER = r'''
import json, os, pathlib, sys
label, pid_path = sys.argv[1:]
pathlib.Path(pid_path).write_text(str(os.getpid()))
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": label, "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Return local test evidence",
            "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}]}
    elif method == "tools/call":
        if message["params"]["arguments"].get("value") == "exit-process":
            os._exit(0)
        result = {"content": [{"type": "text", "text": json.dumps({
            "server": label, "value": message["params"]["arguments"]["value"]})}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''


def _running(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not status.exists() or status.read_text().split()[2] != "Z"


@pytest.fixture
def local_stdio_servers(tmp_path):
    script = tmp_path / "local_mcp_server.py"
    script.write_text(STDIO_SERVER, encoding="utf-8")
    pids = [tmp_path / "first.pid", tmp_path / "last.pid"]
    servers = [
        {"id": label, "transport": "stdio", "command": sys.executable,
         "args": ["-u", str(script), label, str(pid_file)], "cwd": str(tmp_path),
         "startup_timeout_sec": 5}
        for label, pid_file in zip(("first", "last"), pids)
    ]
    yield servers, pids
    # A failing lifecycle assertion must not leave its test subprocess alive.
    # These PIDs originate only from the exact newly launched fixture scripts.
    for pid_file in pids:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            if _running(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


@asynccontextmanager
async def failing_http_peer(mode: str):
    received = asyncio.Event()
    connections: set[asyncio.Task] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            received.set()
            if mode == "http-error":
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            else:
                # Drain the request body but never respond to initialize.
                while await reader.read(4096):
                    pass
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            connections.discard(task)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp", received
    finally:
        listener.close()
        pending = list(connections)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await listener.wait_closed()


async def _assert_stdio_reaped(pid_files):
    for pid_file in pid_files:
        assert pid_file.exists(), "both healthy MCP peers must actually launch"
    deadline = time.monotonic() + 3
    while any(_running(int(path.read_text())) for path in pid_files) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not any(_running(int(path.read_text())) for path in pid_files), "MCP subprocess survived stack close"


@pytest.mark.parametrize("mode", ["timeout", "http-error"])
def test_optional_http_failure_isolated_from_real_stdio_tools(local_stdio_servers, mode):
    stdio, pid_files = local_stdio_servers

    async def run():
        async with failing_http_peer(mode) as (url, received):
            pool = ExternalMCPPool()
            async with AsyncExitStack() as stack:
                await pool.attach([
                    stdio[0], {"id": "broken", "transport": "http", "url": url,
                               "startup_timeout_sec": 0.7}, stdio[1],
                ], stack)
                assert received.is_set(), "exercise an actual local HTTP initialization request"
                assert asyncio.current_task().cancelling() == 0
                assert "broken" in pool.failed_servers
                assert set(pool.tools) == {"first__echo", "last__echo"}
                await asyncio.sleep(0)  # expose cancellation accidentally leaked by teardown
                for label in ("first", "last"):
                    result = await pool.call(f"{label}__echo", {"value": "still usable"})
                    payloads = [json.loads(item.text) for item in result.content if item.type == "text"]
                    assert {"server": label, "value": "still usable"} in payloads
                    assert not result.isError
            await _assert_stdio_reaped(pid_files)

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_real_user_cancellation_propagates_and_reaps_every_started_peer(local_stdio_servers):
    stdio, pid_files = local_stdio_servers

    async def run():
        async with failing_http_peer("timeout") as (url, received):
            pool = ExternalMCPPool()

            async def attach():
                async with AsyncExitStack() as stack:
                    await pool.attach([
                        *stdio, {"id": "waiting", "transport": "http", "url": url,
                                 "startup_timeout_sec": 20},
                    ], stack)

            task = asyncio.create_task(attach())
            try:
                await asyncio.wait_for(received.wait(), timeout=8)
                for _ in range(400):
                    if all(path.exists() for path in pid_files):
                        break
                    await asyncio.sleep(0.02)
                assert all(path.exists() for path in pid_files), "both real stdio peers must start"
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await _assert_stdio_reaped(pid_files)

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_post_start_stdio_exit_fails_call_without_cancelling_healthy_peer(local_stdio_servers):
    stdio, pid_files = local_stdio_servers

    async def run():
        pool = ExternalMCPPool()
        async with AsyncExitStack() as stack:
            await pool.attach(stdio, stack)
            assert set(pool.tools) == {"first__echo", "last__echo"}
            started = time.monotonic()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(
                    pool.call("first__echo", {"value": "exit-process"}), timeout=3,
                )
            assert time.monotonic() - started < 3
            assert asyncio.current_task().cancelling() == 0
            result = await asyncio.wait_for(
                pool.call("last__echo", {"value": "survived peer exit"}), timeout=3,
            )
            payloads = [json.loads(item.text) for item in result.content if item.type == "text"]
            assert {"server": "last", "value": "survived peer exit"} in payloads
            assert not result.isError
        await _assert_stdio_reaped(pid_files)

    asyncio.run(asyncio.wait_for(run(), timeout=15))
