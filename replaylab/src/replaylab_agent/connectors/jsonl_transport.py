"""Bounded framing and backpressure for provider JSONL, independently of answer text."""
from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..errors import ConnectorError


JSONL_READ_CHUNK_BYTES = 64 * 1024


async def read_jsonl_records(stream: asyncio.StreamReader, maximum: int,
                             *, origin: str = "Codex app-server") -> AsyncIterator[bytes]:
    """Frame complete records without making StreamReader buffer a whole line.

    Tool arguments, inline media and metadata are wire records, not assistant
    prose. Keep their bytes intact; never truncate or skip a large valid record.
    The limit includes its newline, as it does on the send path. A final record
    without a newline is returned for the caller's ordinary JSON validation.
    """
    pending = bytearray()
    while chunk := await stream.read(JSONL_READ_CHUNK_BYTES):
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline + 1
            if len(pending) + end - start > maximum:
                raise ConnectorError(
                    f"{origin} emitted a JSONL message over the "
                    f"{maximum // (1024 * 1024)} MiB transport bound"
                )
            pending.extend(chunk[start:end])
            start = end
            if newline >= 0:
                record = bytes(pending)
                pending.clear()
                yield record
    if pending:
        yield bytes(pending)


@dataclass(frozen=True)
class _QueuedNotification:
    value: Any
    wire_bytes: int
    terminal: bool = False


@dataclass(frozen=True)
class _SpooledNotification:
    offset: int
    size: int


class NotificationQueue(asyncio.Queue):
    """Preserve notification order while bounding count and queued wire bytes.

    At most one complete record may cross the byte watermark. This admits a
    valid record larger than the watermark and then pauses the producer until
    consumers make space. Together with the separate per-record cap, storage
    is bounded by watermark + one record rather than count * record cap.
    A pending RPC may spill notifications into a bounded private temporary file:
    its reply must remain readable even before the caller starts consuming events.
    The same deque holds memory and file records, so neither can overtake the other.
    One reserved terminal slot never discards previously received notifications.
    """

    def __init__(self, *, maxsize: int, byte_watermark: int,
                 spool_limit: int = 256 * 1024 * 1024, spool_records: int = 4096):
        super().__init__(maxsize=maxsize)
        self.byte_watermark = byte_watermark
        self.queued_wire_bytes = 0
        self.spool_limit = spool_limit
        self.spool_records = spool_records
        self._spool = None
        self._spool_size = 0
        self._spooled_records = 0
        self._terminal_queued = False
        self._rpc_pending = False
        self._closed = False

    def full(self) -> bool:
        return super().full() or self.queued_wire_bytes >= self.byte_watermark

    async def put(self, item: Any, *, wire_bytes: int = 0,
                  rpc_pending: bool = False, wire_record: bytes | None = None) -> None:
        if self._closed:
            raise ConnectorError("Codex notification queue is closed")
        while self.full() and not (rpc_pending or self._rpc_pending):
            putter = self._get_loop().create_future()
            self._putters.append(putter)
            try:
                await putter
            except BaseException:
                putter.cancel()
                try:
                    self._putters.remove(putter)
                except ValueError:
                    pass
                if not self.full() and not putter.cancelled():
                    self._wakeup_next(self._putters)
                raise
            if self._closed:
                raise ConnectorError("Codex notification queue is closed")
        if self.full():
            if wire_record is None:
                raise ConnectorError("Codex notification backlog has no original wire record")
            if (self._spool_size + len(wire_record) > self.spool_limit
                    or self._spooled_records >= self.spool_records):
                raise ConnectorError("Codex notification backlog exceeded its bounded temporary storage")
            try:
                if self._spool is None:
                    # OS user-temp ACLs on Windows; mode 0600 and unlink-on-close
                    # on POSIX. No task payload is retained after consumption/close.
                    self._spool = tempfile.TemporaryFile(prefix="bread-codex-notifications-", mode="w+b")
                offset = self._spool_size
                self._spool.seek(offset)
                if self._spool.write(wire_record) != len(wire_record):
                    raise OSError("incomplete temporary backlog write")
                self._spool.flush()
            except OSError as exc:
                raise ConnectorError("Codex notification temporary backlog write failed") from exc
            self._spool_size += len(wire_record)
            self._spooled_records += 1
            self._put_reserved(_QueuedNotification(_SpooledNotification(offset, len(wire_record)), 0))
            return
        self.put_nowait(_QueuedNotification(item, wire_bytes))

    def set_rpc_pending(self, value: bool) -> None:
        self._rpc_pending = value
        if value:
            # A request can start after the reader already applied backpressure.
            # Wake that reader so it can spill and reach the reply behind events.
            while self._putters:
                self._wakeup_next(self._putters)

    def put_terminal(self, item: Any) -> None:
        if not self._terminal_queued:
            self._terminal_queued = True
            self._put_reserved(_QueuedNotification(item, 0, terminal=True))

    def _put_reserved(self, item: _QueuedNotification) -> None:
        # asyncio.Queue.put_nowait, with separate explicit spool/terminal bounds.
        self._put(item)
        self._unfinished_tasks += 1
        self._finished.clear()
        self._wakeup_next(self._getters)

    def put_nowait(self, item: Any) -> None:
        if not isinstance(item, _QueuedNotification):
            item = _QueuedNotification(item, 0)
        super().put_nowait(item)

    def _put(self, item: _QueuedNotification) -> None:
        self.queued_wire_bytes += item.wire_bytes
        super()._put(item)

    def _get(self) -> Any:
        item = super()._get()
        self.queued_wire_bytes -= item.wire_bytes
        if item.terminal:
            self._terminal_queued = False
        if isinstance(item.value, _SpooledNotification):
            record = item.value
            try:
                self._spool.seek(record.offset)
                raw = self._spool.read(record.size)
                if len(raw) != record.size:
                    raise OSError("incomplete temporary backlog read")
                value = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError) as exc:
                raise ConnectorError("Codex notification temporary backlog read failed") from exc
            self._spooled_records -= 1
            if self._spooled_records == 0:
                self._close_spool()
            return value
        return item.value

    def close(self) -> None:
        self._closed = True
        while self._putters:
            self._wakeup_next(self._putters)
        self._close_spool()

    def _close_spool(self) -> None:
        if self._spool is not None:
            self._spool.close()
            self._spool = None
        self._spool_size = 0
        self._spooled_records = 0
