"""Unified event stream consumed by the native qrenderdoc Assistant panel."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

TASK_EVENT_SCHEMA = "replaylab-ai-task-event/v1"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TAIL_READ_BYTES = 64 * 1024
_MAX_LAST_EVENT_BYTES = 16 * 1024 * 1024
_ARTIFACT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_PRUNE_ENTRIES = 4096
_DELTA_FLUSH_SECONDS = 0.1
_DELTA_CHARS = 8 * 1024
_DELTA_KINDS = frozenset({"message-delta", "agent-message-delta", "reasoning-delta"})
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|secret|token)", re.IGNORECASE
)
TaskEventKind = Literal[
    "queued",
    "planning",
    "running",
    "message-delta",
    "agent-message-delta",
    "reasoning-delta",
    "tool-started",
    "tool-completed",
    "approval-required",
    "completed",
    "failed",
    "cancelled",
]


def default_task_event_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Never the shared temp directory: on Linux/macOS that put the task event
    # stream, the log and the tool settings under a world-readable /tmp that any
    # local user could pre-plant and that vanished on reboot. XDG data home is the
    # per-user equivalent of LOCALAPPDATA, which the rest of the product uses.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "ai-tasks"


def _prune_stale_task_events(root: Path, *, now: float | None = None) -> None:
    """Best-effort bounded retention for task transcripts.

    Only direct children whose names already satisfy the task-id contract are
    considered.  This keeps cleanup from following attacker-created directory
    trees or touching unrelated files in the ReplayLab data directory.
    """

    if not root.is_dir():
        return
    cutoff = (time.time() if now is None else now) - _ARTIFACT_RETENTION_SECONDS
    try:
        entries = list(root.iterdir())[:_MAX_PRUNE_ENTRIES]
    except OSError:
        return
    for entry in entries:
        if entry.suffix != ".jsonl" or not _TASK_ID_RE.fullmatch(entry.stem):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                for suffix in (".report.json", ".report-state.json"):
                    sidecar = root / f"{entry.stem}{suffix}"
                    if sidecar.is_file() and not sidecar.is_symlink():
                        sidecar.unlink()
        except OSError:
            # Retention must never make an AI request fail. A later task will
            # retry deletion after transient sharing/permission errors clear.
            continue


@dataclass(slots=True)
class TaskEvent:
    task_id: str
    kind: TaskEventKind
    sequence: int
    timestamp_ms: int
    backend: str = ""
    profile: str = ""
    summary: str = ""
    text: str = ""
    tool: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    delivery_id: str = ""
    schema: str = TASK_EVENT_SCHEMA

    def to_wire(self) -> dict[str, Any]:
        value = asdict(self)
        delivery_id = value.pop("delivery_id")
        wire = {
            "schema": value.pop("schema"),
            "taskId": value.pop("task_id"),
            "kind": value.pop("kind"),
            "sequence": value.pop("sequence"),
            "timestampUnixMs": value.pop("timestamp_ms"),
            **value,
        }
        if delivery_id:
            wire["deliveryId"] = delivery_id
        return wire


@contextmanager
def _exclusive_path_lock(path: Path) -> Iterator[None]:
    """Serialize append/projector decisions across processes on one task file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - Windows is the packaged ReplayLab target
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class TaskEventWriter:
    """Append-only JSONL with one file per task and defensive key redaction."""

    def __init__(self, root: Path | str | None = None, task_id: str = "") -> None:
        self.root = Path(root) if root else default_task_event_root()
        self.task_id = task_id or uuid.uuid4().hex
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id contains unsafe path characters")
        _prune_stale_task_events(self.root)
        self._sequence = self._last_sequence()
        self._lock = threading.RLock()
        self._pending_delta: tuple[TaskEventKind, dict[str, Any]] | None = None
        self._delta_timer: threading.Timer | None = None

    def emit_delta(self, kind: TaskEventKind, **values: Any) -> None:
        """Coalesce a contiguous provider stream, with at most 100ms latency.

        The public/disk protocol stays v1. Context, item identity, reset and
        lifecycle boundaries are never merged. A synchronous event (including
        terminal delivery) first persists all preceding text. Sequence numbers
        are allocated under the existing cross-process lock at persistence.
        """
        if kind not in _DELTA_KINDS:
            raise ValueError("emit_delta requires a public text delta")
        # Own the metadata: provider callbacks may reuse/mutate their objects.
        values = _redact(values)
        with self._lock:
            self._check_not_deleted()
            data = values.get("data") or {}
            immediate = (data.get("reset") is True or data.get("stream") == "final"
                         or data.get("lifecycle") in {"completed", "failed", "cancelled", "canceled"})
            context = {key: value for key, value in values.items() if key != "text"}
            if self._pending_delta is not None:
                old_kind, old_values = self._pending_delta
                old_context = {key: value for key, value in old_values.items() if key != "text"}
                if not immediate and kind == old_kind and context == old_context:
                    if len(old_values.get("text", "")) + len(values.get("text", "")) <= _DELTA_CHARS:
                        old_values["text"] = old_values.get("text", "") + values.get("text", "")
                        return
                self._flush_delta_locked()
            if immediate or len(values.get("text", "")) >= _DELTA_CHARS:
                self.emit(kind, **values)
                return
            self._pending_delta = (kind, values)
            timer = threading.Timer(_DELTA_FLUSH_SECONDS, self._flush_delta_timer)
            timer.daemon = True
            self._delta_timer = timer
            timer.start()

    def flush(self) -> None:
        with self._lock:
            self._flush_delta_locked()

    def _flush_delta_timer(self) -> None:
        with self._lock:
            # A canceled timer may already be waiting for this lock. It must
            # never flush a newer stream in place of its own pending batch.
            if self._delta_timer is not threading.current_thread():
                return
            try:
                self._flush_delta_locked()
            except Exception:
                # Keep the batch for the next synchronous call to retry/raise.
                # Background I/O failure must never discard an acknowledged
                # final delivery or resurrect a deleted conversation.
                logging.getLogger(__name__).exception("Task stream flush failed")

    def _flush_delta_locked(self) -> None:
        if self._delta_timer is not None:
            self._delta_timer.cancel()
            self._delta_timer = None
        pending = self._pending_delta
        if pending is None:
            return
        self._pending_delta = None
        try:
            self.emit(pending[0], **pending[1])
        except Exception:
            self._pending_delta = pending
            raise

    @property
    def path(self) -> Path:
        return self.root / f"{self.task_id}.jsonl"

    def _check_not_deleted(self) -> None:
        if (self.root / ".deleted-tasks" / (self.task_id + ".json")).exists():
            raise RuntimeError("The conversation owning this task was deleted")

    def emit(
        self,
        kind: TaskEventKind,
        *,
        delivery_id: str = "",
        **values: Any,
    ) -> TaskEvent:
        delivery_id = str(delivery_id or "")
        if delivery_id and not _DELIVERY_ID_RE.fullmatch(delivery_id):
            raise ValueError("delivery_id contains unsafe characters")
        with self._lock:
            self._flush_delta_locked()
            self._check_not_deleted()
            with _exclusive_path_lock(self.path.with_suffix(".jsonl.lock")):
                self._check_not_deleted()
                if delivery_id:
                    existing = self._event_for_delivery_id(delivery_id)
                    if existing is not None:
                        self._sequence = max(self._sequence, existing.sequence)
                        return existing
                self._sequence = max(self._sequence, self._last_sequence()) + 1
                if "data" in values:
                    values["data"] = _redact(values["data"])
                event = TaskEvent(
                    task_id=self.task_id,
                    kind=kind,
                    sequence=self._sequence,
                    timestamp_ms=int(time.time() * 1000),
                    delivery_id=delivery_id,
                    **values,
                )
                self.root.mkdir(parents=True, exist_ok=True)
                encoded = json.dumps(
                    event.to_wire(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8", errors="backslashreplace")
                with self.path.open("a+b") as stream:
                    stream.seek(0, os.SEEK_END)
                    end = stream.tell()
                    separator = b""
                    if end:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            # A killed writer may leave an incomplete JSON object at EOF. Keep
                            # that fragment on its own corrupt line so this event remains a
                            # parseable JSONL record and future writers can resume its sequence.
                            # This also completes a lone CR into a CRLF boundary.
                            separator = b"\n"
                    stream.write(separator + encoded + b"\n")
                    stream.flush()
                    # Outbox acknowledgement happens only after emit returns. Make
                    # that return a durable sink boundary, not merely a Python-buffer
                    # flush that can be lost while SQLite has already persisted ACK.
                    if delivery_id:
                        os.fsync(stream.fileno())
                return event

    def _event_for_delivery_id(self, delivery_id: str) -> TaskEvent | None:
        if not self.path.is_file():
            return None
        needle = json.dumps(delivery_id, separators=(",", ":")).encode("ascii")
        matched: dict[str, Any] | None = None
        try:
            with self.path.open("rb") as stream:
                for raw_line in stream:
                    if needle not in raw_line:
                        continue
                    try:
                        value = json.loads(raw_line)
                    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                        continue
                    if (
                        isinstance(value, dict)
                        and value.get("schema") == TASK_EVENT_SCHEMA
                        and value.get("taskId") == self.task_id
                        and value.get("deliveryId") == delivery_id
                    ):
                        matched = value
                        break
        except OSError:
            return None
        if matched is None:
            return None
        sequence = matched.get("sequence")
        timestamp = matched.get("timestampUnixMs")
        kind = matched.get("kind")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or not isinstance(kind, str)
        ):
            return None
        data = matched.get("data", {})
        return TaskEvent(
            task_id=self.task_id,
            kind=kind,  # type: ignore[arg-type]
            sequence=sequence,
            timestamp_ms=timestamp,
            backend=str(matched.get("backend", "")),
            profile=str(matched.get("profile", "")),
            summary=str(matched.get("summary", "")),
            text=str(matched.get("text", "")),
            tool=str(matched.get("tool", "")),
            data=data if isinstance(data, dict) else {},
            delivery_id=delivery_id,
        )

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                position = stream.tell()
                suffix = b""
                scanned = 0
                while position > 0:
                    count = min(position, _TAIL_READ_BYTES)
                    position -= count
                    stream.seek(position)
                    suffix = stream.read(count) + suffix
                    scanned += count
                    lines = suffix.split(b"\n")
                    suffix = lines.pop(0)
                    for line in reversed(lines):
                        sequence = self._sequence_from_line(line.rstrip(b"\r"))
                        if sequence is not None:
                            return sequence
                    if scanned >= _MAX_LAST_EVENT_BYTES and position > 0:
                        raise ValueError("last task event exceeds the safety bound")
                sequence = self._sequence_from_line(suffix.rstrip(b"\r"))
                return sequence if sequence is not None else 0
        except OSError:
            return 0

    def _sequence_from_line(self, line: bytes) -> int | None:
        if not line:
            return None
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("schema") != TASK_EVENT_SCHEMA or value.get("taskId") != self.task_id:
            return None
        sequence = value.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return None
        return sequence


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY_RE.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value
