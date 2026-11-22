"""Bounded ACP stdio transport through the SDK's message-transport interface.

StreamReader.limit is only an I/O watermark: the ACP SDK reassembles overruns
without a record limit. Keep a separate full-message budget for tool metadata and
media, independently of the much smaller final assistant-text budget.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import inspect
import os
import subprocess
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from ..errors import ConnectorError
from .jsonl_transport import JSONL_READ_CHUNK_BYTES, read_jsonl_records


ACP_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
ACP_MAX_PENDING_UPDATE_BYTES = 128 * 1024 * 1024
ACP_MAX_PENDING_UPDATES = 128


class BoundedACPTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: Any,
                 *, maximum: int = ACP_MAX_MESSAGE_BYTES):
        self.writer = writer
        self.maximum = maximum
        self.failure: ConnectorError | None = None
        self._records = read_jsonl_records(reader, maximum, origin="ACP agent")
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._update_receipts: deque[int] = deque()
        self._pending_update_bytes = 0
        self._pending_updates = 0

    async def send(self, message: dict[str, Any]) -> None:
        # Encode after acquiring the lock: simultaneous callbacks must not build
        # an unbounded queue of complete multi-megabyte encoded send buffers.
        async with self._write_lock:
            if self._closed:
                raise ConnectorError("ACP transport is closed")
            try:
                payload = (json.dumps(message, ensure_ascii=False,
                    separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
                if len(payload) > self.maximum:
                    raise ConnectorError("ACP message exceeds the bounded transport capacity; "
                        "use an owned file reference or paged tool input for bulk data")
                self.writer.write(payload)
                await self.writer.drain()
            except ConnectorError as exc:
                self.failure = exc
                raise
            except (OSError, ValueError, TypeError) as exc:
                self.failure = ConnectorError("ACP message could not be encoded or written")
                raise self.failure from exc

    async def receive(self) -> dict[str, Any] | None:
        # The SDK dispatches notifications as tasks. Yield between records so
        # normal timeline callbacks finish before the next large update arrives.
        await asyncio.sleep(0)
        if self.failure is not None:
            raise self.failure
        try:
            async for line in self._records:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConnectorError("ACP agent emitted invalid JSONL") from exc
                if not isinstance(message, dict):
                    raise ConnectorError("ACP agent emitted a non-object JSONL message")
                if message.get("method") == "session/update":
                    if (self._pending_updates >= ACP_MAX_PENDING_UPDATES
                            or self._pending_update_bytes + len(line) > ACP_MAX_PENDING_UPDATE_BYTES):
                        raise ConnectorError("ACP session-update backlog exceeded its bounded capacity")
                    self._update_receipts.append(len(line))
                    self._pending_update_bytes += len(line)
                    self._pending_updates += 1
                return message
            return None
        except ConnectorError as exc:
            self.failure = exc
            raise
        except OSError as exc:
            self.failure = ConnectorError("ACP stdio connection failed while reading")
            raise self.failure from exc

    async def close(self) -> None:
        self._closed = True
        self.writer.close()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), timeout=0.25)


class _BoundedUpdateClient:
    """Account for updates until an asynchronous host callback has consumed them."""
    def __init__(self, client: Any, transport: BoundedACPTransport):
        self._client = client
        self._transport = transport

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        transport = self._transport
        receipt = transport._update_receipts.popleft() if transport._update_receipts else 0
        try:
            await self._client.session_update(session_id, update, **kwargs)
        finally:
            if receipt:
                transport._pending_update_bytes -= receipt
                transport._pending_updates -= 1


class _CheckedConnection:
    """Keep the SDK's typed API while retaining precise transport failures."""
    def __init__(self, connection: Any, transport: BoundedACPTransport):
        self._connection = connection
        self._transport = transport

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._connection, name)
        if not callable(value):
            return value
        async def await_checked(awaitable: Any) -> Any:
            try:
                return await awaitable
            except Exception as exc:
                if self._transport.failure is not None and self._transport.failure is not exc:
                    raise self._transport.failure from exc
                raise
        @wraps(value)
        def checked(*args: Any, **kwargs: Any) -> Any:
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                # SDK pending requests otherwise receive only "Connection closed".
                if self._transport.failure is not None and self._transport.failure is not exc:
                    raise self._transport.failure from exc
                raise
            # SDK parameter decorators are synchronous functions returning an
            # awaitable, so iscoroutinefunction alone misses initialize/prompt.
            return await_checked(result) if inspect.isawaitable(result) else result
        return checked


@asynccontextmanager
async def _hidden_stdio_transport(command: str, *args: str, env=None, cwd=None,
                                  stderr=asyncio.subprocess.PIPE, limit=JSONL_READ_CHUNK_BYTES,
                                  shutdown_timeout=2.0):
    """Windows transport for SDK releases without a process-flags parameter."""
    from acp.transports import default_environment
    environment = dict(default_environment())
    environment.update(env or {})
    process = await asyncio.create_subprocess_exec(command, *args, env=environment, cwd=cwd,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
        limit=limit, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        yield process.stdout, process.stdin, process
    finally:
        if process.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError, TimeoutError):
                process.stdin.close()
                await asyncio.wait_for(process.stdin.wait_closed(), timeout=shutdown_timeout)
        try:
            await asyncio.wait_for(process.wait(), timeout=shutdown_timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=shutdown_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


@asynccontextmanager
async def spawn_bounded_agent_process(
    to_client: Any, command: str, *args: str,
    env: Mapping[str, str] | None = None, cwd: str | Path | None = None,
    transport_kwargs: Mapping[str, Any] | None = None, **connection_kwargs: Any,
) -> AsyncIterator[tuple[Any, asyncio.subprocess.Process]]:
    # Use the SDK's documented connection/stdio seams, not process-global
    # monkeypatches or modifications to the installed third-party package.
    from acp.client.connection import ClientSideConnection
    from acp.transports import spawn_stdio_transport

    options = dict(transport_kwargs or {})
    options["limit"] = JSONL_READ_CHUNK_BYTES
    # The SDK currently omits CREATE_NO_WINDOW and exposes no creationflags
    # argument. Retain its connection/message transport and own only spawning.
    spawn = _hidden_stdio_transport if os.name == "nt" else spawn_stdio_transport
    async with spawn(command, *args, env=env, cwd=cwd, **options) as (reader, writer, process):
        transport = BoundedACPTransport(reader, writer)
        def client_factory(agent: Any) -> _BoundedUpdateClient:
            client = to_client(agent) if callable(to_client) else to_client
            return _BoundedUpdateClient(client, transport)
        connection = ClientSideConnection(client_factory, transport, **connection_kwargs)
        try:
            yield _CheckedConnection(connection, transport), process
        finally:
            await connection.close()
