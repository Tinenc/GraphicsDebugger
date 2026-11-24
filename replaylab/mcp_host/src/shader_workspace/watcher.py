"""Bounded, auditable save watching for one tracked shader workspace source.

The watcher is deliberately a host-side convenience layer.  It obtains the
fixed, manifest-validated source path from ``ShaderWorkspaceService.status``
and invokes ``ShaderWorkspaceService.compile``.  It never talks to RenderDoc
and never promotes or applies a compiled candidate.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import WorkspaceWatchConflictError, WorkspaceWatchError
from .diagnostics import classify_shader_failure, parse_compiler_diagnostics
from .io import atomic_write_json, is_within
from .leases import WorkspaceSourceLeaseError, WorkspaceSourceTransactionLease
from .source_observation import source_observation_available, source_observation_transaction
from .organization import (
    MAX_SOURCE_BYTES as MAX_ORGANIZED_SOURCE_BYTES,
    ShaderOrganizationError,
    _source_compare_and_swap,
    active_draft_id,
)
from .workspace import MAX_SOURCE_BYTES, ShaderWorkspaceService
from .status_snapshot import bound_status_snapshot
from ..shader_scheduler import ScheduledTask, is_current_task

WATCH_SCHEMA = "renderdoc-ultra-shader-watch/v1"
WATCH_VERSION = 1
MIN_DEBOUNCE_MS = 100
MAX_DEBOUNCE_MS = 5000
DEFAULT_DEBOUNCE_MS = 100
MAX_ACTIVE_WATCHERS = 128
_COMPILER_SLOTS = threading.BoundedSemaphore(2)
MAX_RETAINED_WATCHERS = 32
MAX_AUDIT_EVENTS = 128
STOP_JOIN_SECONDS = 2.0
# Metadata notices ordinary saves on the next debounce tick. Full reads are a
# fallback for writers preserving all timestamps, not a per-second revalidation
# of every restored stage. Explicit save/compile/apply still reads actual bytes.
FULL_OBSERVATION_INTERVAL_SECONDS = 30.0
MAX_TRANSIENT_COMPILE_RETRIES = 2
TRANSIENT_COMPILE_RETRY_DELAYS = (0.10, 0.25)
MAX_COMPILER_DIAGNOSTICS = 128
MAX_COMPILER_DIAGNOSTIC_BYTES = 1024 * 1024
_WATCH_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JOB_HINT = re.compile(r"(?:^|[;\s])see\s+(.+?)\s*$", flags=re.IGNORECASE)


def validate_watch_start_status(status_result: dict) -> None:
    """Reject non-executable/stale workspaces before replay preflight."""

    source_record = status_result.get("source")
    if not isinstance(source_record, dict) or source_record.get("present") is not True:
        raise WorkspaceWatchError(
            "workspace must contain a tracked decompiled source before watching"
        )
    representation = status_result.get("representation")
    if (
        source_record.get("lane") != "executable-editable"
        or not isinstance(representation, dict)
        or representation.get("lane") != "executable-editable"
        or representation.get("compileEligible") is not True
    ):
        raise WorkspaceWatchError(
            _watch_start_rejection(
                status_result, "save watching requires the executable-editable shader lane")
        )


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error if isinstance(next_error, BaseException) else None


def _is_transient_compile_failure(error: BaseException) -> bool:
    """Retry only bounded local Windows sharing/access failures."""

    for item in _exception_chain(error):
        if isinstance(item, OSError):
            if getattr(item, "winerror", None) in {5, 32, 33}:
                return True
            if os.name == "nt" and getattr(item, "winerror", None) is None and getattr(
                item, "errno", None
            ) in {errno.EACCES, errno.EBUSY}:
                return True
        message = str(item)
        if any(token in message for token in ("[WinError 5]", "[WinError 32]", "[WinError 33]")):
            return True
    return False


def _diagnostic_texts(root: Path, error: BaseException) -> list[str]:
    texts = [str(item) for item in _exception_chain(error) if str(item)]
    jobs_root = (root / "jobs").resolve()
    for value in tuple(texts):
        match = _JOB_HINT.search(value)
        if match is None:
            continue
        hinted = Path(match.group(1).strip().strip('"'))
        try:
            job = hinted.resolve(strict=True)
            if (
                not is_within(jobs_root, job)
                or job == jobs_root
                or hinted.is_symlink()
            ):
                continue
            for name in ("stderr.log", "stdout.log"):
                stream = job / name
                if (stream.is_symlink() or not stream.is_file()
                        or stream.stat().st_size > MAX_COMPILER_DIAGNOSTIC_BYTES):
                    continue
                texts.append(stream.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _compiler_diagnostics(
    root: Path, source_path: Path, error: BaseException
) -> list[dict[str, Any]]:
    """Parse target compiler diagnostics into console/link-friendly records."""

    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    texts = _diagnostic_texts(root, error)
    for text_value in texts:
        for record in parse_compiler_diagnostics(text_value, source_path.name):
            file_value = record["file"]
            try:
                # Includes and generated later-hop code do not share the main
                # document's line numbers. Only its same-language compiler copy
                # can be mapped without a source map.
                if Path(file_value.replace("\\", "/")).name.lower() in {
                        "input" + source_path.suffix.lower(), source_path.name.lower()}:
                    file_value = str(source_path)
            except OSError:
                pass
            record["file"] = file_value
            key = tuple(record.values())
            if key in seen:
                continue
            seen.add(key)
            diagnostics.append(record)
            if len(diagnostics) >= MAX_COMPILER_DIAGNOSTICS:
                return diagnostics
    if diagnostics:
        return diagnostics
    # Link/target failures may have no source line. Keep the compiler's reason
    # visible without fabricating a line-one error in the edited document.
    reasons = []
    for text_value in texts:
        for line in text_value.splitlines():
            match = re.match(r"^\s*(?:fatal\s+error|error)\s*:\s*(.+)$", line, re.I)
            if match and match.group(1) not in reasons:
                reasons.append(match.group(1))
    return [
        {
            "file": str(source_path),
            "line": None,
            "column": None,
            "severity": "error",
            "message": _bounded_text("\n".join(reasons) if reasons else error, 4096),
        }
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_text(value: Any, maximum: int = 2048) -> str:
    text = str(value)
    if len(text) <= maximum:
        return text
    return text[:maximum] + "...[truncated]"


def _watch_start_rejection(status: dict, fallback: str) -> str:
    """Keep invalid-source errors distinct from a valid source's stale tools."""
    source = status.get("source")
    representation = status.get("representation")
    if (
        status.get("toolchainStatus") != "refresh-required"
        or not isinstance(source, dict)
        or source.get("present") is not True
        or source.get("lane") != "executable-editable"
        or not isinstance(representation, dict)
        or representation.get("lane") != "executable-editable"
        or representation.get("compileEligible") is not False
    ):
        return fallback
    diagnostic = _bounded_text(status.get("toolchainDiagnostic") or "toolchain binding is stale", 1024)
    # This is a backend failure diagnostic, not a normal UI label.
    return (
        "save watching requires a toolchain refresh: %s; "
        "get_shader_workspace_status reports the source and route-plan identities"
    ) % diagnostic


def _validate_debounce(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkspaceWatchError("debounce_ms must be an integer")
    if value < MIN_DEBOUNCE_MS or value > MAX_DEBOUNCE_MS:
        raise WorkspaceWatchError(
            "debounce_ms must be between %d and %d"
            % (MIN_DEBOUNCE_MS, MAX_DEBOUNCE_MS)
        )
    return value


def _fixed_source(root: Path, expected_source: Path) -> Path:
    """Return the only path a watcher may observe, rejecting redirection."""

    source_dir = root / "source"
    source = Path(expected_source)
    if not source.is_absolute() or source.parent != source_dir:
        raise WorkspaceWatchError(
            "workspace status source must be one direct child of the source directory"
        )
    if source_dir.is_symlink() or source.is_symlink():
        raise WorkspaceWatchError("workspace source path must not be a symlink")
    try:
        source_dir_resolved = source_dir.resolve(strict=True)
        source_resolved = source.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceWatchError(
            "workspace tracked source is not available: %s" % exc
        ) from exc
    if source_dir_resolved != source_dir or source_resolved != source:
        raise WorkspaceWatchError("workspace source path is redirected")
    if not is_within(root, source_resolved) or not source_resolved.is_file():
        raise WorkspaceWatchError(
            "workspace tracked source is not a regular in-workspace file"
        )
    return source_resolved


def _source_bytes(root: Path, expected_source: Path) -> bytes:
    """Read one stable UTF-8 source snapshot without accepting another path."""

    source = _fixed_source(root, expected_source)
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceWatchError("workspace shader source is not a regular file")
    if before.st_size <= 0:
        raise WorkspaceWatchError("workspace shader source is empty")
    if before.st_size > MAX_SOURCE_BYTES:
        raise WorkspaceWatchError(
            "workspace shader source exceeds %d bytes" % MAX_SOURCE_BYTES
        )
    with source.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise WorkspaceWatchError(
            "workspace shader source exceeds %d bytes" % MAX_SOURCE_BYTES
        )
    after = source.stat(follow_symlinks=False)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(raw) != after.st_size:
        raise WorkspaceWatchError("workspace shader source changed while being read")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkspaceWatchError(
            "workspace shader source is not valid UTF-8"
        ) from exc
    if "\x00" in text:
        raise WorkspaceWatchError("workspace shader source contains NUL characters")
    return raw


def _source_hash(root: Path, expected_source: Path) -> str:
    return hashlib.sha256(_source_bytes(root, expected_source)).hexdigest()


def _poll_file_fingerprint(path: Path) -> tuple | None:
    """Cheap observation hint only; never authorizes saving or compilation."""
    try:
        info = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            # Sibling audit/lock writes do not change this source. Still notice
            # directory replacement/redirection; the source stamp notices saves.
            return (info.st_dev, info.st_ino, info.st_mode,
                    getattr(info, "st_file_attributes", 0))
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns,
                getattr(info, "st_file_attributes", 0))
    except OSError:
        return None


def _already_compiled_candidate(root: Path, source_hash: str) -> dict[str, Any] | None:
    """The manifest's latest candidate when it was built from exactly this source."""

    try:
        path = Path(root) / "manifest.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            return None
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    latest = manifest.get("latestCompiled") if isinstance(manifest, dict) else None
    if not isinstance(latest, dict) or latest.get("sourceSha256") != source_hash:
        return None
    # Equal root text does not identify the compilation unit after editing an
    # include. Never adopt a candidate built against different header bytes.
    try:
        source_record = manifest.get("source") or {}
        if not isinstance(source_record, dict) or (
                "sourceDependencies" in latest and not isinstance(latest["sourceDependencies"], dict)):
            return None
        source_path = root / str(source_record.get("path") or "source/shader.hlsl")
        from .compile_source_unit import current_dependencies
        if not current_dependencies(source_path, _source_bytes(root, source_path), latest)["current"]:
            return None
    except (OSError, ValueError, TypeError, WorkspaceWatchError):
        return None
    revision = latest.get("revision")
    raw = latest.get("rawSha256")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return None
    if not isinstance(raw, str) or _SHA256.fullmatch(raw) is None:
        return None
    return {"sourceSha256": source_hash, "revision": revision, "rawSha256": raw, "adopted": True,
            "manualFastCompile": latest.get("manualFastCompile") is True,
            "compilationCacheHit": latest.get("manualCompileCache") is not None}


class _WatchJob:
    def __init__(
        self,
        watch_id: str,
        root: Path,
        source_path: Path,
        debounce_ms: int,
        service: ShaderWorkspaceService,
        initial_hash: str,
        catch_up_reference_hash: str | None,
        catch_up_reference_kind: str | None,
        latest_compiled_hash: str | None,
        latest_candidate_revision: int | None,
        latest_candidate_raw_sha256: str | None,
        audit_path: Path,
        lease: WorkspaceSourceTransactionLease,
        on_finished: Callable[["_WatchJob"], None],
    ):
        self.watch_id = watch_id
        self.root = root
        self.source_path = source_path
        self.debounce_ms = debounce_ms
        self.service = service
        self.audit_path = audit_path
        self._lease = lease
        self._on_finished = on_finished
        self._lock = threading.RLock()
        # Source observation/compile and Host-owned editor saves must never
        # overlap. The transaction CAS writes in place under an exclusive
        # handle; this separate lock keeps the compiler from observing bytes
        # while that bounded write is in progress without blocking snapshots.
        self._source_io_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = ScheduledTask(self._run, name="shader-watch-%s" % watch_id[:8], lane="watch")
        self._state = "starting"
        self._created_at = _utc_now()
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._initial_hash = initial_hash
        self._observed_hash = initial_hash
        # A pristine workspace establishes the watch baseline and is not
        # compiled merely because watching started.  If a previous candidate
        # exists for different source bytes, however, treating the current
        # file as already attempted would strand a pre-watch editor save
        # forever: no further filesystem change would arrive to wake it.  Seed
        # last-attempted from that stale candidate so the normal debounce loop
        # deterministically catches the current file up.
        self._catch_up_reference_hash = catch_up_reference_hash
        self._catch_up_reference_kind = catch_up_reference_kind
        self._last_attempted_hash = (
            catch_up_reference_hash
            if catch_up_reference_hash and catch_up_reference_hash != initial_hash
            else initial_hash
        )
        self._last_compiled_hash = latest_compiled_hash
        self._latest_candidate_revision = latest_candidate_revision
        self._latest_candidate_raw_sha256 = latest_candidate_raw_sha256
        self._stable_since = time.monotonic()
        self._compile_in_progress = False
        self._pending = False
        # A project restore owns this workspace's source for the length of its
        # transaction; the watcher must not treat the restore's writes (or its
        # rollback) as editor saves and race it with its own compile/apply.
        self._suspended_by = ""
        self._compile_attempts = 0
        self._compile_successes = 0
        self._compile_failures = 0
        self._transient_retry_hash: str | None = None
        self._transient_retry_count = 0
        self._transient_retry_due = 0.0
        self._last_error: dict[str, Any] | None = None
        self._observation_error_key: tuple[str, str] | None = None
        self._source_poll_fingerprint: tuple | None = None
        self._source_full_observation_at = float("-inf")
        self._candidate_poll_key: tuple | None = None
        self._candidate_full_observation_at = float("-inf")
        self._candidate_poll_result: dict | None = None
        self._draft_poll_key: tuple | None = None
        self._draft_full_observation_at = float("-inf")
        self._active_draft_id = ""
        self._draft_observation_error = ""
        self._deferred_draft_hash = ""
        self._explicit_editor_hash = ""
        self._explicit_compile_hash = ""
        self._next_sequence = 1
        self._snapshot_generation = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_AUDIT_EVENTS)
        self._dropped_events = 0

    def start(self) -> None:
        with self._lock:
            catch_up_scheduled = self._last_attempted_hash != self._initial_hash
            self._record_locked(
                "watchCreated",
                sourceSha256=self._initial_hash,
                staleCandidateSourceSha256=(
                    self._last_attempted_hash
                    if catch_up_scheduled
                    and self._catch_up_reference_kind == "latest-compiled"
                    else None
                ),
                catchUpReferenceSourceSha256=(
                    self._last_attempted_hash if catch_up_scheduled else None
                ),
                catchUpReferenceKind=(
                    self._catch_up_reference_kind if catch_up_scheduled else None
                ),
                catchUpCompileScheduled=catch_up_scheduled,
                message=(
                    "current source differs from the last tracked source; a debounced "
                    "catch-up compile is scheduled"
                    if catch_up_scheduled
                    else "initial source hash recorded; no initial compile scheduled"
                ),
            )
        self._thread.start()

    def stop(self) -> dict:
        with self._lock:
            if self._state not in {"stopped", "failed"}:
                self._state = "stopping"
                self._stop.set()
                self._record_locked(
                    "stopRequested",
                    message=(
                        "the active compile, if any, is allowed to finish; no new "
                        "compile will start"
                    ),
                )
        if not is_current_task(self._thread):
            self._thread.join(STOP_JOIN_SECONDS)
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def snapshot_if_changed(self, sequence: int | None) -> dict | None:
        with self._lock:
            if sequence == self._snapshot_generation:
                return None
            return self._snapshot_locked()

    def is_terminal(self) -> bool:
        with self._lock:
            return self._state in {"stopped", "failed"}

    def save_source(self, expected_raw_sha256: str, text: str) -> dict:
        """Publish one editor save while this process owns the live watch lease.

        The job lock makes the running-state decision and the source CAS one
        operation with respect to watcher stop/failure. The OS-handle CAS also
        denies ordinary external writes/renames across its final comparison and
        in-place write, so an expected-hash mismatch is never overwritten.
        """

        expected = str(expected_raw_sha256 or "").lower()
        if _SHA256.fullmatch(expected) is None:
            raise WorkspaceWatchError("editor expected source hash is invalid")
        if not isinstance(text, str):
            raise WorkspaceWatchError("editor source must be text")
        if not text:
            raise WorkspaceWatchError("editor source must not be empty")
        if "\x00" in text:
            raise WorkspaceWatchError("editor source contains NUL characters")
        try:
            replacement = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise WorkspaceWatchError("editor source is not valid UTF-8") from exc
        if len(replacement) > min(MAX_SOURCE_BYTES, MAX_ORGANIZED_SOURCE_BYTES):
            raise WorkspaceWatchError("editor source exceeds its fixed size limit")

        replacement_hash = hashlib.sha256(replacement).hexdigest()
        with self._source_io_lock:
            with self._lock:
                if (self._state != "running" or self._stop.is_set() or
                        not self._lease.owned):
                    raise WorkspaceWatchError("workspace watcher is not running")
                before = _source_bytes(self.root, self.source_path)
                before_hash = hashlib.sha256(before).hexdigest()
                if before_hash != expected:
                    raise WorkspaceWatchConflictError(
                        "workspace shader source changed on disk; reload before saving"
                    )
                if before == replacement:
                    info = self.source_path.stat(follow_symlinks=False)
                    return {
                        "watchId": self.watch_id,
                        "workspacePath": str(self.root),
                        "sourcePath": str(self.source_path),
                        "rawSha256": replacement_hash,
                        "size": len(replacement),
                        "modifiedNs": info.st_mtime_ns,
                        "changed": False,
                        "compileScheduled": False,
                    }
                try:
                    swapped = _source_compare_and_swap(
                        self.source_path, before, replacement, "workspace HLSL"
                    )
                except ShaderOrganizationError as exc:
                    raise WorkspaceWatchError(str(exc)) from exc
                if not swapped:
                    raise WorkspaceWatchConflictError(
                        "workspace shader source changed on disk; reload before saving"
                    )

                previous = self._observed_hash
                self._observed_hash = replacement_hash
                self._stable_since = time.monotonic()
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._pending = replacement_hash != self._last_attempted_hash
                # The editor save endpoint is a distinct explicit action. A
                # filesystem/proposal save by an organizer never sets this.
                self._explicit_editor_hash = replacement_hash
                self._last_error = None
                self._record_locked(
                    "editorSourceSaved",
                    previousSha256=previous,
                    sourceSha256=replacement_hash,
                    expectedSha256=expected,
                    compileScheduled=self._pending,
                )
                info = self.source_path.stat(follow_symlinks=False)
                return {
                    "watchId": self.watch_id,
                    "workspacePath": str(self.root),
                    "sourcePath": str(self.source_path),
                    "rawSha256": replacement_hash,
                    "size": len(replacement),
                    "modifiedNs": info.st_mtime_ns,
                    "changed": True,
                    "compileScheduled": self._pending,
                }

    def request_saved_source_compile(self, expected_raw_sha256: str) -> dict:
        """Queue one explicit IDE save/retry; ordinary AI draft saves stay deferred."""
        expected = str(expected_raw_sha256 or "").lower()
        if _SHA256.fullmatch(expected) is None:
            raise WorkspaceWatchError("editor expected source hash is invalid")
        with self._source_io_lock, self._lock:
            if self._state not in {"starting", "running"} or self._stop.is_set() or not self._lease.owned:
                raise WorkspaceWatchError("workspace watcher is not running")
            if _source_hash(self.root, self.source_path) != expected:
                raise WorkspaceWatchConflictError("Shader source changed before explicit compile scheduling")
            # This explicit request also retries unchanged saved bytes and
            # includes. It creates a new candidate revision, so an earlier
            # apply rejection cannot suppress the user's retry as already seen.
            if not (self._pending and self._explicit_compile_hash == expected):
                self._last_attempted_hash = ""
                self._observed_hash = expected
                self._stable_since = time.monotonic()
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._explicit_editor_hash = expected
                self._explicit_compile_hash = expected
                self._pending = True
                self._record_locked("editorCompileRequested", sourceSha256=expected,
                                    compileScheduled=True)
            return {"watchId": self.watch_id, "workspacePath": str(self.root),
                    "sourceSha256": expected, "compileScheduled": True}

    def _run(self):
        try:
            with self._lock:
                self._state = "running"
                self._started_at = _utc_now()
                self._record_locked("watchStarted")
            # Source fingerprints already coalesce saves. Many restored stages
            # must not multiply 20 idle filesystem polls/sec on the GUI host.
            interval = min(0.5, max(0.2, self.debounce_ms / 1000.0))
            while not self._stop.is_set():
                yield interval
                if self._stop.is_set():
                    break
                self._tick()
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._last_error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded_text(exc),
                }
                self._record_locked(
                    "watchFailed",
                    errorType=type(exc).__name__,
                    message=_bounded_text(exc),
                    persist_failure_is_fatal=False,
                )
        finally:
            lease_released = self._lease.release()
            with self._lock:
                if self._state != "failed":
                    self._state = "stopped"
                self._compile_in_progress = False
                self._pending = False
                self._stopped_at = _utc_now()
                self._record_locked(
                    "watchStopped",
                    watchLeaseReleased=lease_released,
                    message=(
                        "cross-process watch lease released"
                        if lease_released
                        else "watch lease token could not be verified; inspect it manually"
                    ),
                    persist_failure_is_fatal=False,
                )
            self._on_finished(self)

    def _tick(self) -> None:
        with self._lock:
            if self._suspended_by:
                return
        # Do not acquire/resolve the cross-process observation lock for a
        # proven idle poll. Any changed hint or bounded full-check deadline
        # still enters the original transaction before reading or publishing.
        now = time.monotonic()
        with self._source_io_lock:
            self._refresh_organization_draft(now)
            if (not self._pending and self._observation_error_key is None
                    and self._source_poll_fingerprint is not None
                    and now - self._source_full_observation_at < FULL_OBSERVATION_INTERVAL_SECONDS
                    and now - self._candidate_full_observation_at < FULL_OBSERVATION_INTERVAL_SECONDS
                    and self._source_poll_fingerprint == tuple(
                        _poll_file_fingerprint(path) for path in
                        (self.root, self.source_path.parent, self.source_path))
                    and self._candidate_poll_key == (
                        _poll_file_fingerprint(self.root / "manifest.json"), self._observed_hash)):
                return
        with source_observation_transaction(self.root, blocking=False) as available:
            if not available:
                return
            with self._source_io_lock:
                self._tick_source_locked()

    def suspend(self, owner: str) -> None:
        """Stop reacting to source changes until ``resume`` (restore-owned)."""
        with self._lock:
            if self._stop.is_set():
                return
            self._suspended_by = str(owner or "restore")
            self._record_locked("watchSuspended", owner=self._suspended_by)

    def resume(self) -> None:
        """Adopt whatever the owner left on disk as the new baseline, no compile."""
        with self._source_io_lock:
            try:
                current_hash = _source_hash(self.root, self.source_path)
            except Exception:
                current_hash = None
            with self._lock:
                owner = self._suspended_by
                self._suspended_by = ""
                if current_hash is not None:
                    self._observed_hash = current_hash
                    self._last_attempted_hash = current_hash
                    self._stable_since = time.monotonic()
                    self._transient_retry_hash = None
                    self._transient_retry_count = 0
                    self._transient_retry_due = 0.0
                    self._pending = False
                self._record_locked(
                    "watchResumed", owner=owner,
                    sourceSha256=current_hash or "",
                )

    def _refresh_organization_draft(self, now: float) -> str:
        gate = self.root / "ai" / "organize-code-gate.json"
        session = (self.root / "ai" / "organization-sessions" /
                   self._active_draft_id / "session.json") if self._active_draft_id else None
        key = (_poll_file_fingerprint(gate),
               _poll_file_fingerprint(session) if session is not None else None)
        if (key != self._draft_poll_key or
                now - self._draft_full_observation_at >= FULL_OBSERVATION_INTERVAL_SECONDS):
            previous = self._active_draft_id
            try:
                self._active_draft_id = active_draft_id(self.root)
            except (OSError, ValueError, RuntimeError) as error:
                message = _bounded_text(error)
                if message != self._draft_observation_error:
                    with self._lock:
                        self._record_locked("organizationDraftObservationFailed", message=message,
                                            compileScheduled=False)
                self._draft_observation_error = message
            else:
                if self._draft_observation_error:
                    with self._lock:
                        self._record_locked("organizationDraftObservationRecovered")
                self._draft_observation_error = ""
            session = (self.root / "ai" / "organization-sessions" /
                       self._active_draft_id / "session.json") if self._active_draft_id else None
            self._draft_poll_key = (_poll_file_fingerprint(gate),
                                   _poll_file_fingerprint(session) if session is not None else None)
            self._draft_full_observation_at = now
            if previous != self._active_draft_id:
                self._candidate_poll_key = None
        return "unavailable" if self._draft_observation_error else self._active_draft_id

    def _tick_source_locked(self) -> None:
        now = time.monotonic()
        manifest_fingerprint = _poll_file_fingerprint(self.root / "manifest.json")
        try:
            source_fingerprint = tuple(_poll_file_fingerprint(path) for path in
                                       (self.root, self.source_path.parent, self.source_path))
            candidate_key = (manifest_fingerprint, self._observed_hash)
            # Metadata only skips an unchanged idle observation. A pending save,
            # new candidate or previous error still takes the original strict
            # read. Periodic full reads catch preserved timestamps/redirection.
            if (source_fingerprint != self._source_poll_fingerprint
                    or self._pending or self._observation_error_key is not None
                    or candidate_key != self._candidate_poll_key
                    or now - self._source_full_observation_at >= FULL_OBSERVATION_INTERVAL_SECONDS):
                current_hash = _source_hash(self.root, self.source_path)
                self._source_poll_fingerprint = source_fingerprint
                self._source_full_observation_at = now
            else:
                current_hash = self._observed_hash
        except Exception as exc:
            with self._lock:
                error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded_text(exc),
                }
                error_key = (error["type"], error["message"])
                if self._observation_error_key != error_key:
                    self._observation_error_key = error_key
                    self._last_error = error
                    self._record_locked(
                        "observationFailed",
                        errorType=error["type"],
                        message=error["message"],
                    )
            return

        now = time.monotonic()
        target_hash: str | None = None
        with self._lock:
            if self._stop.is_set():
                return
            draft_id = self._refresh_organization_draft(now)
            if draft_id and self._explicit_editor_hash != current_hash:
                # Ordinary organization saves are editor working state. Never
                # compile them or publish even an already compiled candidate
                # to live edit before the explicit reviewed final operation.
                self._explicit_editor_hash = ""
                self._observed_hash = current_hash
                self._last_attempted_hash = current_hash
                self._pending = False
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._candidate_poll_key = (manifest_fingerprint, current_hash)
                self._candidate_full_observation_at = now
                if self._deferred_draft_hash != current_hash:
                    self._stable_since = now
                    self._deferred_draft_hash = current_hash
                    self._record_locked("organizationDraftSaved", organizationId=draft_id,
                                        sourceSha256=current_hash, compileScheduled=False)
                return
            self._deferred_draft_hash = ""
            if self._observation_error_key is not None:
                if self._last_error is not None and (
                    self._last_error.get("type"),
                    self._last_error.get("message"),
                ) == self._observation_error_key:
                    self._last_error = None
                self._observation_error_key = None
                self._record_locked("observationRecovered")
            if current_hash != self._observed_hash:
                previous = self._observed_hash
                self._observed_hash = current_hash
                self._stable_since = now
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._pending = current_hash != self._last_attempted_hash
                self._last_error = None
                self._record_locked(
                    "sourceChanged",
                    previousSha256=previous,
                    sourceSha256=current_hash,
                )
                return
            retrying_same_save = self._transient_retry_hash == current_hash
            self._pending = (
                current_hash != self._last_attempted_hash or retrying_same_save
            )
            if not self._pending:
                # A reviewed transaction can restore the same canonical bytes
                # under a newer permanent revision. Adopt that final candidate
                # after the observation transaction, without recompiling it.
                candidate_key = (manifest_fingerprint, current_hash)
                if (candidate_key != self._candidate_poll_key
                        or now - self._candidate_full_observation_at >= FULL_OBSERVATION_INTERVAL_SECONDS):
                    self._candidate_poll_result = _already_compiled_candidate(self.root, current_hash)
                    self._candidate_poll_key = candidate_key
                    self._candidate_full_observation_at = now
                published = self._candidate_poll_result
                if published is None or (
                    published["revision"] == self._latest_candidate_revision
                    and published["rawSha256"] == self._latest_candidate_raw_sha256
                ):
                    return
                self._pending = True
            if retrying_same_save:
                if now < self._transient_retry_due:
                    return
            elif (now - self._stable_since) * 1000.0 < self.debounce_ms:
                return
            target_hash = current_hash
            force_compile = self._explicit_compile_hash == target_hash
            if force_compile:
                self._explicit_compile_hash = ""
            self._last_attempted_hash = target_hash
            self._compile_in_progress = True
            self._compile_attempts += 1
            self._record_locked(
                "compileStarted",
                sourceSha256=target_hash,
                attempt=self._compile_attempts,
                transientRetry=(
                    self._transient_retry_count if retrying_same_save else 0
                ),
            )

        try:
            # Another writer (the AI organizer, a project restore, the switch
            # button) may already have compiled exactly these bytes; adopt that
            # candidate instead of building it again.
            adopted = None if force_compile else _already_compiled_candidate(self.root, target_hash)
            compiler = getattr(self.service, "compile_for_live_edit", self.service.compile)
            compile_started = time.monotonic()
            if adopted is not None:
                result = adopted
            else:
                with _COMPILER_SLOTS:
                    result = compiler(self.root)
            compile_duration_ms = round((time.monotonic() - compile_started) * 1000.0, 3)
            compiled_hash = result.get("sourceSha256")
            revision = result.get("revision")
            candidate_raw_sha256 = result.get("rawSha256")
            if (
                not isinstance(compiled_hash, str)
                or _SHA256.fullmatch(compiled_hash) is None
            ):
                raise WorkspaceWatchError(
                    "compiled candidate does not contain a valid source hash"
                )
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise WorkspaceWatchError(
                    "compiled candidate does not contain a valid revision"
                )
            if (
                not isinstance(candidate_raw_sha256, str)
                or _SHA256.fullmatch(candidate_raw_sha256) is None
            ):
                raise WorkspaceWatchError(
                    "compiled candidate does not contain a valid raw SHA-256"
                )
            with self._lock:
                self._compile_successes += 1
                self._last_compiled_hash = compiled_hash
                # The service snapshots the source under its own cross-process
                # operation lock. If an editor saved between our observation
                # and that snapshot, this is the hash that was actually tried.
                self._last_attempted_hash = compiled_hash
                self._latest_candidate_revision = revision
                self._latest_candidate_raw_sha256 = candidate_raw_sha256
                self._transient_retry_hash = None
                self._transient_retry_count = 0
                self._transient_retry_due = 0.0
                self._last_error = None
                self._record_locked(
                    "compileAdopted" if adopted is not None else "compileSucceeded",
                    requestedSourceSha256=target_hash,
                    compiledSourceSha256=compiled_hash,
                    revision=self._latest_candidate_revision,
                    candidateRawSha256=candidate_raw_sha256,
                    durationMs=compile_duration_ms,
                    manualFastCompile=result.get("manualFastCompile") is True,
                    compilationCacheHit=result.get("compilationCacheHit") is True,
                    message=(
                        "candidate compiled by another writer from the same source was adopted"
                        if adopted is not None else
                        "manual candidate compiled without organization ABI validation"
                        if result.get("manualFastCompile") is True else
                        "candidate remains unapplied and requires replay ABI validation"
                    ),
                )
        except Exception as exc:
            diagnostic = classify_shader_failure(exc, phase="watch-compile")
            diagnostics = _compiler_diagnostics(self.root, self.source_path, exc)
            transient = _is_transient_compile_failure(exc)
            with self._lock:
                self._compile_failures += 1
                self._last_error = {
                    "at": _utc_now(),
                    "type": type(exc).__name__,
                    "message": _bounded_text(exc),
                    "diagnostic": diagnostic,
                    "diagnostics": diagnostics,
                }
                self._record_locked(
                    "compileFailed",
                    requestedSourceSha256=target_hash,
                    errorType=type(exc).__name__,
                    message=_bounded_text(exc),
                    diagnostic=diagnostic,
                    diagnostics=diagnostics,
                    transient=transient,
                )
                if (
                    transient
                    and not self._stop.is_set()
                    and self._transient_retry_count
                    < MAX_TRANSIENT_COMPILE_RETRIES
                ):
                    delay = TRANSIENT_COMPILE_RETRY_DELAYS[
                        self._transient_retry_count
                    ]
                    self._transient_retry_count += 1
                    self._transient_retry_hash = target_hash
                    self._transient_retry_due = time.monotonic() + delay
                    self._record_locked(
                        "compileRetryScheduled",
                        sourceSha256=target_hash,
                        retry=self._transient_retry_count,
                        delayMs=int(delay * 1000),
                        reason="transient-windows-file-sharing",
                    )
                else:
                    self._transient_retry_hash = None
                    self._transient_retry_due = 0.0
        finally:
            try:
                after_hash = _source_hash(self.root, self.source_path)
            except Exception:
                after_hash = None
            with self._lock:
                self._compile_in_progress = False
                if after_hash is not None and after_hash != self._observed_hash:
                    previous = self._observed_hash
                    self._observed_hash = after_hash
                    self._stable_since = time.monotonic()
                    self._transient_retry_hash = None
                    self._transient_retry_count = 0
                    self._transient_retry_due = 0.0
                    self._record_locked(
                        "sourceChangedDuringCompile",
                        previousSha256=previous,
                        sourceSha256=after_hash,
                    )
                self._pending = (
                    not self._stop.is_set()
                    and (
                        self._observed_hash != self._last_attempted_hash
                        or self._transient_retry_hash == self._observed_hash
                    )
                )
                self._persist_locked()

    def _record_locked(
        self,
        event_type: str,
        persist_failure_is_fatal: bool = True,
        **details: Any,
    ) -> None:
        if len(self._events) == self._events.maxlen:
            self._dropped_events += 1
        event = {
            "sequence": self._next_sequence,
            "at": _utc_now(),
            "type": event_type,
        }
        self._next_sequence += 1
        event.update(details)
        self._events.append(event)
        try:
            self._persist_locked()
        except Exception as exc:
            if persist_failure_is_fatal:
                raise WorkspaceWatchError(
                    "could not persist watcher audit state: %s" % exc
                ) from exc

    def _persist_locked(self) -> None:
        # Compile completion can update caughtUp without appending an event.
        # Publish every persisted state transition, including that final edge.
        self._snapshot_generation += 1
        atomic_write_json(self.audit_path, self._snapshot_locked())

    def _snapshot_locked(self) -> dict:
        observation_available = source_observation_available(self.root)
        caught_up = bool(
            self._last_compiled_hash
            and self._last_compiled_hash == self._observed_hash
            # A retry can fail without changing the root source hash (for
            # example after an include edit). The last successful candidate
            # remains available for rollback, but is not the latest saved code.
            and self._last_error is None
            and not self._compile_in_progress
            and not self._pending
            and not self._suspended_by
            and observation_available
        )
        return {
            "schema": WATCH_SCHEMA,
            "observationSequence": self._snapshot_generation,
            "schemaVersion": WATCH_VERSION,
            "watchId": self.watch_id,
            "state": self._state,
            "workspacePath": str(self.root),
            "sourcePath": str(self.source_path),
            "auditPath": str(self.audit_path),
            "watchLease": {
                "path": str(self._lease.path),
                "ownedByThisProcess": self._lease.owned,
            },
            "debounceMs": self.debounce_ms,
            "createdAt": self._created_at,
            "startedAt": self._started_at,
            "stoppedAt": self._stopped_at,
            "initialSourceSha256": self._initial_hash,
            "observedSourceSha256": self._observed_hash,
            "lastAttemptedSourceSha256": self._last_attempted_hash,
            "lastCompiledSourceSha256": self._last_compiled_hash,
            "latestCandidateRevision": self._latest_candidate_revision,
            "latestCandidateRawSha256": self._latest_candidate_raw_sha256,
            "compileInProgress": self._compile_in_progress,
            "pending": self._pending,
            "caughtUp": caught_up,
            "sourceObservationPaused": not observation_available,
            "compileAttempts": self._compile_attempts,
            "compileSuccesses": self._compile_successes,
            "compileFailures": self._compile_failures,
            "transientRetry": {
                "scheduled": self._transient_retry_hash is not None,
                "sourceSha256": self._transient_retry_hash,
                "retry": self._transient_retry_count,
                "maximumRetries": MAX_TRANSIENT_COMPILE_RETRIES,
            },
            "lastError": self._last_error,
            "droppedAuditEvents": self._dropped_events,
            "events": list(self._events),
            "candidatePolicy": {
                "requiresReplayAbiValidation": True,
                "replayAbiValidated": False,
                "applied": False,
            },
        }


class ShaderWorkspaceWatchManager:
    """Own a bounded number of process-local workspace watcher threads."""

    def __init__(
        self,
        service_factory: Callable[[], ShaderWorkspaceService],
        *,
        owner_instance_id: str | None = None,
    ):
        self._service_factory = service_factory
        self._owner_instance_id = str(owner_instance_id or "").strip().lower()
        if self._owner_instance_id and _WATCH_ID.fullmatch(self._owner_instance_id) is None:
            raise WorkspaceWatchError("watch owner instance ID is invalid")
        self._lock = threading.RLock()
        self._jobs: dict[str, _WatchJob] = {}
        self._active_by_workspace: dict[str, str] = {}

    def start(
        self, workspace_path: str | Path, debounce_ms: int = DEFAULT_DEBOUNCE_MS
    ) -> dict:
        return self._start(workspace_path, debounce_ms, recover_orphaned_lease=False)

    def start_persistent(
        self,
        workspace_path: str | Path,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> dict:
        """Resume a durable session, recovering only a prior host-generation lease."""

        return self._start(
            workspace_path,
            debounce_ms,
            recover_orphaned_lease=bool(self._owner_instance_id),
        )

    def _start(
        self,
        workspace_path: str | Path,
        debounce_ms: int,
        *,
        recover_orphaned_lease: bool,
    ) -> dict:
        debounce = _validate_debounce(debounce_ms)
        service = self._service_factory()
        validated = bound_status_snapshot(workspace_path)
        status_result = (validated.validated_status() if validated is not None
                         else service.status(workspace_path))
        root = Path(status_result["workspacePath"]).resolve()
        validate_watch_start_status(status_result)
        source_record = status_result.get("source")
        status_source = Path(source_record.get("path", ""))
        source = _fixed_source(root, status_source)
        if status_source.resolve() != source:
            raise WorkspaceWatchError("workspace status returned an unexpected source path")
        initial_hash = _source_hash(root, source)
        latest = status_result.get("latestCompiled")
        latest_hash = latest.get("sourceSha256") if isinstance(latest, dict) else None
        if not isinstance(latest_hash, str) or _SHA256.fullmatch(latest_hash) is None:
            latest_hash = None
        latest_revision = latest.get("revision") if isinstance(latest, dict) else None
        if (
            isinstance(latest_revision, bool)
            or not isinstance(latest_revision, int)
            or latest_revision < 1
        ):
            latest_revision = None
        latest_raw_sha256 = (
            latest.get("rawSha256") if isinstance(latest, dict) else None
        )
        if (
            not isinstance(latest_raw_sha256, str)
            or _SHA256.fullmatch(latest_raw_sha256) is None
        ):
            latest_raw_sha256 = None
        decompiled_hash = source_record.get("decompiledSha256")
        if not isinstance(decompiled_hash, str) or _SHA256.fullmatch(decompiled_hash) is None:
            decompiled_hash = None
        if latest_hash is not None:
            catch_up_reference_hash = latest_hash
            catch_up_reference_kind = "latest-compiled"
        else:
            # The immutable source record is the only evidence that can distinguish a
            # pristine workspace from an edit saved before watching began. Without it,
            # treating the current bytes as a change would also compile every untouched
            # decompile merely because a watcher was opened.
            catch_up_reference_hash = decompiled_hash
            catch_up_reference_kind = (
                "decompiled-source" if decompiled_hash is not None else None
            )
        workspace_key = os.path.normcase(str(root))

        with self._lock:
            self._prune_locked()
            existing_id = self._active_by_workspace.get(workspace_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and not existing.is_terminal():
                    raise WorkspaceWatchError(
                        "workspace already has an active watcher: %s" % existing_id
                    )
                self._active_by_workspace.pop(workspace_key, None)
            active_count = sum(not job.is_terminal() for job in self._jobs.values())
            if active_count >= MAX_ACTIVE_WATCHERS:
                raise WorkspaceWatchError(
                    "active watcher limit reached (%d)" % MAX_ACTIVE_WATCHERS
                )
            watch_dir = root / "watch"
            if watch_dir.is_symlink():
                raise WorkspaceWatchError("workspace watch directory must not be a symlink")
            watch_dir.mkdir(parents=False, exist_ok=True)
            if watch_dir.resolve() != watch_dir or not watch_dir.is_dir():
                raise WorkspaceWatchError("workspace watch directory is redirected")
            watch_id = uuid.uuid4().hex
            audit_path = watch_dir / (watch_id + ".json")
            lease = WorkspaceSourceTransactionLease(
                root,
                owner_instance_id=self._owner_instance_id or None,
                recover_orphaned=recover_orphaned_lease,
            )
            try:
                lease.acquire()
            except WorkspaceSourceLeaseError as exc:
                raise WorkspaceWatchError(
                    "workspace already has a cross-process watch lease; the lease "
                    "is never assumed stale: %s" % lease.path
                ) from exc
            try:
                # The pre-lease status is used only to locate and reserve the
                # workspace. Organization may publish/recover a newer revision
                # before acquisition, so re-resolve every source/candidate
                # field under the shared lease before constructing the job.
                # A Host-owned start snapshot has already passed full status.
                # Re-hash all of its inputs after lease acquisition instead of
                # parsing the same binary/ABI/route evidence a second time.
                # Any intervening publication rejects this admission attempt.
                locked_status = (validated.checked_status(service) if validated is not None
                                 else service.status(workspace_path))
                locked_root = Path(locked_status["workspacePath"]).resolve()
                locked_source_record = locked_status.get("source")
                locked_representation = locked_status.get("representation")
                if (
                    locked_root != root
                    or not isinstance(locked_source_record, dict)
                    or locked_source_record.get("present") is not True
                    or locked_source_record.get("lane") != "executable-editable"
                    or not isinstance(locked_representation, dict)
                    or locked_representation.get("lane") != "executable-editable"
                    or locked_representation.get("compileEligible") is not True
                ):
                    raise WorkspaceWatchError(
                        _watch_start_rejection(
                            locked_status,
                            "workspace identity/lane changed while acquiring its source lease")
                        if locked_root == root else
                        "workspace identity/lane changed while acquiring its source lease"
                    )
                locked_status_source = Path(
                    locked_source_record.get("path", "")
                )
                locked_source = _fixed_source(locked_root, locked_status_source)
                if (
                    locked_source != source
                    or locked_status_source.resolve() != locked_source
                ):
                    raise WorkspaceWatchError(
                        "workspace source changed while acquiring its source lease"
                    )
                initial_hash = _source_hash(locked_root, locked_source)
                if locked_source_record.get("currentSha256") != initial_hash:
                    raise WorkspaceWatchError(
                        "workspace source hash changed while watcher start was resolving"
                    )
                latest = locked_status.get("latestCompiled")
                latest_hash = (
                    latest.get("sourceSha256")
                    if isinstance(latest, dict)
                    else None
                )
                if (
                    not isinstance(latest_hash, str)
                    or _SHA256.fullmatch(latest_hash) is None
                ):
                    latest_hash = None
                latest_revision = (
                    latest.get("revision") if isinstance(latest, dict) else None
                )
                if (
                    isinstance(latest_revision, bool)
                    or not isinstance(latest_revision, int)
                    or latest_revision < 1
                ):
                    latest_revision = None
                latest_raw_sha256 = (
                    latest.get("rawSha256") if isinstance(latest, dict) else None
                )
                if (
                    not isinstance(latest_raw_sha256, str)
                    or _SHA256.fullmatch(latest_raw_sha256) is None
                ):
                    latest_raw_sha256 = None
                decompiled_hash = locked_source_record.get("decompiledSha256")
                if (
                    not isinstance(decompiled_hash, str)
                    or _SHA256.fullmatch(decompiled_hash) is None
                ):
                    decompiled_hash = None
                if latest_hash is not None:
                    catch_up_reference_hash = latest_hash
                    catch_up_reference_kind = "latest-compiled"
                else:
                    catch_up_reference_hash = decompiled_hash
                    catch_up_reference_kind = (
                        "decompiled-source"
                        if decompiled_hash is not None
                        else None
                    )
                job = _WatchJob(
                    watch_id,
                    root,
                    source,
                    debounce,
                    service,
                    initial_hash,
                    catch_up_reference_hash,
                    catch_up_reference_kind,
                    latest_hash,
                    latest_revision,
                    latest_raw_sha256,
                    audit_path,
                    lease,
                    self._finished,
                )
                self._jobs[watch_id] = job
                self._active_by_workspace[workspace_key] = watch_id
                job.start()
            except Exception:
                self._jobs.pop(watch_id, None)
                self._active_by_workspace.pop(workspace_key, None)
                lease.release()
                raise
        return job.snapshot()

    def poll(self, watch_id: str) -> dict:
        return self._job(watch_id).snapshot()

    def poll_if_changed(self, watch_id: str, sequence: int | None) -> dict | None:
        """Idle coordinators need no disk/OS lock read for an unchanged job.

        A new candidate gets a full snapshot. Apply still acquires the source
        observation transaction and checks actual compiled source independently.
        """
        return self._job(watch_id).snapshot_if_changed(sequence)

    def save_source(self, watch_id: str, expected_raw_sha256: str,
                    text: str) -> dict:
        return self._job(watch_id).save_source(expected_raw_sha256, text)

    def request_saved_source_compile(self, watch_id: str, expected_raw_sha256: str) -> dict:
        return self._job(watch_id).request_saved_source_compile(expected_raw_sha256)

    def compile_check(self, workspace_path: str | Path, text: str) -> dict:
        """Compile an unsaved editor buffer for live diagnostics only.

        Unlike :meth:`save_source` this needs no active watch job: it builds
        a fresh service (the same factory the watcher uses, so the compiler
        binding, includes and target profile are identical) and runs the
        workspace's bound compiler over the provided text in a throwaway
        job. Nothing is written to the workspace, published, or applied;
        only diagnostics are returned.
        """

        service = self._service_factory()
        return service.compile_check(workspace_path, text)

    def stop(self, watch_id: str) -> dict:
        return self._job(watch_id).stop()

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if not job.is_terminal():
                job._stop.set()
        deadline = time.monotonic() + STOP_JOIN_SECONDS
        for job in jobs:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            job._thread.join(remaining)

    def owns_source_lease(self, workspace_path: str | Path) -> bool:
        """True when a live watcher of this process holds the workspace's source lease."""

        key = os.path.normcase(str(Path(workspace_path)))
        with self._lock:
            return any(
                os.path.normcase(str(job.root)) == key and not job.is_terminal()
                and job._lease.owned
                for job in self._jobs.values())

    def suspend_workspace(self, workspace_path: str | Path, owner: str = "project-restore"):
        """Context manager: pause live watchers of one workspace for a transaction.

        Returns the number of watchers paused via the ``with`` target. Every
        paused watcher resumes with the on-disk source as its baseline, so the
        transaction's writes and any rollback are never compiled as user saves.
        """

        manager = self

        class _Suspension:
            def __init__(self) -> None:
                self.jobs: list[_WatchJob] = []

            def __enter__(self):
                key = os.path.normcase(str(Path(workspace_path)))
                with manager._lock:
                    candidates = [
                        job for job in manager._jobs.values()
                        if os.path.normcase(str(job.root)) == key
                        and not job.is_terminal()
                    ]
                for job in candidates:
                    job.suspend(owner)
                    self.jobs.append(job)
                return len(self.jobs)

            def __exit__(self, *_exc):
                for job in self.jobs:
                    try:
                        job.resume()
                    except Exception:
                        pass
                return False

        return _Suspension()

    def _job(self, watch_id: str) -> _WatchJob:
        if not isinstance(watch_id, str) or _WATCH_ID.fullmatch(watch_id) is None:
            raise WorkspaceWatchError("watch_id must be a 32-character lowercase hex ID")
        with self._lock:
            job = self._jobs.get(watch_id)
        if job is None:
            raise WorkspaceWatchError("unknown or expired watch_id")
        return job

    def _finished(self, job: _WatchJob) -> None:
        key = os.path.normcase(str(job.root))
        with self._lock:
            if self._active_by_workspace.get(key) == job.watch_id:
                self._active_by_workspace.pop(key, None)

    def _prune_locked(self) -> None:
        terminal = [
            job for job in self._jobs.values() if job.is_terminal()
        ]
        excess = len(terminal) - MAX_RETAINED_WATCHERS
        if excess <= 0:
            return
        terminal.sort(key=lambda job: job.watch_id)
        for job in terminal[:excess]:
            self._jobs.pop(job.watch_id, None)
