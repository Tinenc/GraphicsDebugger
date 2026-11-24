"""Persistent active ReplayLab project orchestration.

This module deliberately sits above :mod:`src.projects`.  It owns the
machine-local active-project selection and the portable project workspace
layout, while ``ReplayProjectService`` remains the authority for physically
matched cross-capture knowledge.

An automatically-created workspace starts in ``pending-scope`` state.  It may
stage capture catalogs and remember an unpacked asset root, but it cannot
import, seed, or otherwise reuse physical knowledge until a user has supplied
an exact game/build scope.  AI-produced analysis and Mermaid flowcharts are
stored as explanatory, content-addressed artifacts and never become physical
evidence or apply authorization. Structured Shader flow graphs are likewise
stored only as navigation metadata bound to reviewed source hashes.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

from .projects import ReplayProjectError, ReplayProjectService
from .shader_workspace.io import (
    atomic_write_bytes,
    atomic_write_json,
    is_within,
    read_json_object,
)
from .shader_workspace.flow_graph import (
    MAX_FLOW_GRAPH_BYTES,
    ShaderFlowGraphError,
    validate_shader_flow_graph,
)
from .symbols.matcher import _normalise_catalog


ACTIVE_SELECTION_SCHEMA = "replaylab-active-project-selection/v1"
ACTIVE_SELECTION_VERSION = 1
PROJECT_WORKSPACE_SCHEMA = "replaylab-project-workspace/v1"
PROJECT_WORKSPACE_VERSION = 1
SHARED_DICTIONARY_SCHEMA = "replaylab-shared-dictionary/v1"
SHARED_DICTIONARY_VERSION = 1
FUTURE_FRAME_PLAN_SCHEMA = "replaylab-future-frame-load-plan/v1"
FUTURE_FRAME_PLAN_VERSION = 1
CAPTURE_LIBRARY_SCHEMA = "replaylab-project-capture-library/v1"
CAPTURE_LIBRARY_VERSION = 2
CAPTURE_CONTENT_IDENTITY_SCHEMA = "replaylab-rdc-content-identity/v1"
CAPTURE_CONTENT_IDENTITY_ALGORITHM = "sha256-full"
CAPTURE_PROJECT_MATCH_SCHEMA = "replaylab-capture-project-match/v1"
PROJECT_REGISTRY_SCHEMA = "replaylab-project-registry/v1"
PROJECT_REGISTRY_VERSION = 1
EXCLUSIVE_PROJECT_LOCK_SCHEMA = "replaylab-exclusive-project-lock/v2"
EXCLUSIVE_PROJECT_LOCK_VERSION = 2

MAX_CONFIG_BYTES = 1024 * 1024
MAX_EXCLUSIVE_PROJECT_LOCK_BYTES = 4096
SELECTION_FILE_LOCK_WAIT_SECONDS = 5.0
EXCLUSIVE_LOCK_RELEASE_WAIT_SECONDS = 5.0
MAX_CATALOG_BYTES = 256 * 1024 * 1024
MAX_STAGED_SHADER_BYTES = 512 * 1024 * 1024
MAX_STAGED_CATALOG_REVISIONS = 4096
MAX_STAGED_CATALOG_STORAGE_BYTES = 64 * 1024 * 1024 * 1024
MAX_EXPLANATORY_BYTES = 1024 * 1024
MAX_EXPLANATORY_RECORDS = 4096
MAX_CAPTURE_LIBRARY_RECORDS = 4096
MAX_CAPTURE_IDENTITY_BYTES = 64 * 1024 * 1024 * 1024
MAX_CAPTURE_IDENTITY_SECONDS = 75.0
CAPTURE_IDENTITY_CHUNK_BYTES = 4 * 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
WINDOWS_PROCESS_IDENTITY_RE = re.compile(r"^windows-filetime:[0-9a-f]{16}$")
LINUX_PROCESS_IDENTITY_RE = re.compile(r"^linux-startticks:[1-9][0-9]*$")
LOCAL_PROCESS_IDENTITY_RE = re.compile(r"^process-local:[0-9a-f]{32}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)

_LAYOUT = {
    "metadataDirectory": ".replaylab",
    "workspaceManifest": ".replaylab/workspace.json",
    "replayProject": ".replaylab/replay-project",
    "captureCatalogs": "captures/catalogs",
    "shaderWorkspaces": "workspaces/shaders",
    "sharedDictionaries": "shared/dictionaries",
    "sharedExplanatory": "shared/dictionaries/explanatory",
    "sharedIndex": "shared/dictionaries/index.json",
}

_CAPTURE_LIBRARY_RELATIVE = ".replaylab/capture-library.json"

_DIRECTORY_LAYOUT_KEYS = (
    "metadataDirectory",
    "captureCatalogs",
    "shaderWorkspaces",
    "sharedDictionaries",
    "sharedExplanatory",
)

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_SELECTION_LOCK_DEPTH = threading.local()
_UNSET = object()
_PROCESS_LOCAL_LOCK_IDENTITY = uuid.uuid4().hex


class ActiveProjectError(ReplayProjectError):
    """Fail-closed active project configuration error."""


@dataclass(frozen=True)
class ActiveProjectSnapshot:
    """One fixed selection/configuration; project artifacts remain live reads."""

    state_path: str
    project_root: Path
    workspace_id: str
    selection_sha256: str
    config_sha256: str
    manifest_json: bytes


_ACTIVE_PROJECT_SNAPSHOTS: ContextVar[tuple[ActiveProjectSnapshot, ...]] = ContextVar(
    "replaylab_active_project_snapshots", default=()
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _document_hash(value: Mapping[str, Any], hash_field: str) -> str:
    material = dict(value)
    material.pop(hash_field, None)
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _seal(value: dict[str, Any], hash_field: str) -> dict[str, Any]:
    result = dict(value)
    result[hash_field] = _document_hash(result, hash_field)
    return result


def _verify_seal(value: Mapping[str, Any], hash_field: str, label: str) -> None:
    declared = str(value.get(hash_field, "") or "").lower()
    if not SHA256_RE.fullmatch(declared):
        raise ActiveProjectError("%s hash is invalid" % label)
    if declared != _document_hash(value, hash_field):
        raise ActiveProjectError("%s hash mismatch" % label)


def _text(
    value: Any,
    field: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ActiveProjectError("%s must be a string" % field)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > maximum or "\x00" in result:
        raise ActiveProjectError("%s has invalid content" % field)
    return result


def _sha256(value: Any, field: str) -> str:
    result = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(result):
        raise ActiveProjectError("%s must be a lowercase SHA-256" % field)
    return result


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ActiveProjectError("%s must be a positive integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActiveProjectError("%s must be a positive integer" % field) from exc
    if result <= 0:
        raise ActiveProjectError("%s must be a positive integer" % field)
    return result


def _api_family(value: Any, *, allow_empty: bool = True) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    aliases = {
        "direct3d11": "d3d11",
        "directx11": "d3d11",
        "direct3d12": "d3d12",
        "directx12": "d3d12",
    }
    result = aliases.get(text, text)
    if not result and allow_empty:
        return ""
    if not result or len(result) > 64:
        raise ActiveProjectError("api_family is invalid")
    return result


def _filesystem_root(path: Path) -> bool:
    return path == Path(path.anchor)


def _stat_modified_time_ns(value: os.stat_result) -> int:
    return int(
        getattr(value, "st_mtime_ns", int(float(value.st_mtime) * 1000000000))
    )


def _capture_file_observation(path: Path) -> dict[str, Any]:
    """Return bounded cache metadata for one already validated capture path."""

    try:
        value = path.stat()
    except OSError as exc:
        raise ActiveProjectError("capture metadata could not be read") from exc
    size = int(value.st_size)
    if size < 0 or size > MAX_CAPTURE_IDENTITY_BYTES:
        raise ActiveProjectError(
            "capture exceeds the %d-byte content identity limit"
            % MAX_CAPTURE_IDENTITY_BYTES
        )
    return {
        "sizeBytes": size,
        "modifiedTimeNs": _stat_modified_time_ns(value),
        "fileIdentity": "%d:%d"
        % (int(getattr(value, "st_dev", 0)), int(getattr(value, "st_ino", 0))),
    }


def _capture_content_identity(path: Path) -> dict[str, Any]:
    """Hash a complete RDC with fixed memory, size and wall-time bounds.

    The full digest is the stable identity.  Size/mtime/file-id are only a
    performance cache key: a metadata change causes a fresh full hash, while an
    unchanged observation can reuse the digest previously established when the
    reference was explicitly added.
    """

    before = _capture_file_observation(path)
    digest = hashlib.sha256()
    started = time.monotonic()
    total = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            opened_observation = {
                "sizeBytes": int(opened.st_size),
                "modifiedTimeNs": _stat_modified_time_ns(opened),
                "fileIdentity": "%d:%d"
                % (
                    int(getattr(opened, "st_dev", 0)),
                    int(getattr(opened, "st_ino", 0)),
                ),
            }
            if opened_observation != before:
                raise ActiveProjectError(
                    "capture changed before content identity hashing began"
                )
            while True:
                if time.monotonic() - started > MAX_CAPTURE_IDENTITY_SECONDS:
                    raise ActiveProjectError(
                        "capture content identity exceeded the %.0f-second safety limit"
                        % MAX_CAPTURE_IDENTITY_SECONDS
                    )
                chunk = stream.read(CAPTURE_IDENTITY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CAPTURE_IDENTITY_BYTES:
                    raise ActiveProjectError(
                        "capture exceeds the content identity byte limit"
                    )
                digest.update(chunk)
            closed = os.fstat(stream.fileno())
            closed_observation = {
                "sizeBytes": int(closed.st_size),
                "modifiedTimeNs": _stat_modified_time_ns(closed),
                "fileIdentity": "%d:%d"
                % (
                    int(getattr(closed, "st_dev", 0)),
                    int(getattr(closed, "st_ino", 0)),
                ),
            }
    except ActiveProjectError:
        raise
    except OSError as exc:
        raise ActiveProjectError("capture content identity could not be read") from exc
    after = _capture_file_observation(path)
    if total != before["sizeBytes"] or closed_observation != before or after != before:
        raise ActiveProjectError("capture changed while its content identity was hashed")
    return {
        "schema": CAPTURE_CONTENT_IDENTITY_SCHEMA,
        "algorithm": CAPTURE_CONTENT_IDENTITY_ALGORITHM,
        "sha256": digest.hexdigest(),
        **before,
    }


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _process_lock_owner_state(pid: int) -> tuple[str, str]:
    """Return ``(state, creation_identity)`` for one process.

    ``state`` is ``alive``, ``dead`` or ``unknown``.  A missing or unreadable
    identity never authorises recovery.  Windows FILETIME and Linux start
    ticks distinguish a live process from a later process that reused its PID.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unknown", ""

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            error_invalid_parameter = 87
            synchronize = 0x00100000
            wait_object_0 = 0
            wait_timeout = 258
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(
                process_query_limited_information | synchronize, False, pid
            )
            if not handle:
                if ctypes.get_last_error() == error_invalid_parameter:
                    return "dead", ""
                return "unknown", ""
            try:
                wait_result = int(kernel32.WaitForSingleObject(handle, 0))
                if wait_result == wait_object_0:
                    return "dead", ""
                if wait_result != wait_timeout:
                    return "unknown", ""
                creation = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return "alive", ""
                creation_value = (
                    int(creation.dwHighDateTime) << 32
                ) | int(creation.dwLowDateTime)
                return "alive", "windows-filetime:%016x" % creation_value
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, ImportError, OSError, ValueError):
            # Fall through to the conservative process-local/kill probe.
            pass

    if sys.platform.startswith("linux"):
        process_stat = Path("/proc") / str(pid) / "stat"
        try:
            raw = process_stat.read_text(encoding="ascii", errors="strict")
        except FileNotFoundError:
            return "dead", ""
        except (OSError, UnicodeError):
            return "unknown", ""
        # comm is parenthesised and may contain spaces or ')', so split after
        # the final ')' rather than tokenising the complete record.
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split() if closing >= 0 else []
        if len(fields) <= 19 or not fields[19].isdigit():
            return "alive", ""
        return "alive", "linux-startticks:%s" % fields[19]

    if pid == os.getpid():
        return "alive", "process-local:%s" % _PROCESS_LOCAL_LOCK_IDENTITY
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", ""
    except (PermissionError, OSError):
        return "unknown", ""
    return "alive", ""


def _exclusive_project_lock_payload(token: str) -> bytes:
    state, identity = _process_lock_owner_state(os.getpid())
    if state != "alive" or not identity:
        # This opaque identity is useful for exact release in the current
        # process.  Another process cannot use it as PID-reuse evidence and
        # therefore remains fail-closed until the PID is known to be dead.
        identity = "process-local:%s" % _PROCESS_LOCAL_LOCK_IDENTITY
    value = {
        "schema": EXCLUSIVE_PROJECT_LOCK_SCHEMA,
        "schemaVersion": EXCLUSIVE_PROJECT_LOCK_VERSION,
        "token": token,
        "ownerPid": os.getpid(),
        "processStartIdentity": identity,
        "acquiredAt": _utc_now(),
    }
    return _canonical_bytes(_seal(value, "lockSha256")) + b"\n"


def _lock_snapshot(path: Path) -> tuple[bytes, os.stat_result] | None:
    """Read one stable, bounded, regular lock file without following links."""

    descriptor = None
    try:
        if path.is_symlink():
            return None
        descriptor = os.open(
            str(path),
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_EXCLUSIVE_PROJECT_LOCK_BYTES
        ):
            return None
        chunks = []
        remaining = MAX_EXCLUSIVE_PROJECT_LOCK_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        entry_after = path.stat(follow_symlinks=False)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        _stat_modified_time_ns(before),
    )
    handle_after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        _stat_modified_time_ns(after),
    )
    entry_after_identity = (
        entry_after.st_dev,
        entry_after.st_ino,
        entry_after.st_size,
        _stat_modified_time_ns(entry_after),
    )
    if (
        before_identity != handle_after_identity
        or before_identity != entry_after_identity
        or not stat.S_ISREG(entry_after.st_mode)
        or len(raw) != before.st_size
    ):
        return None
    return raw, after


def _validated_process_identity_scheme(value: str) -> str:
    if WINDOWS_PROCESS_IDENTITY_RE.fullmatch(value) is not None:
        return "windows-filetime"
    if LINUX_PROCESS_IDENTITY_RE.fullmatch(value) is not None:
        return "linux-startticks"
    if LOCAL_PROCESS_IDENTITY_RE.fullmatch(value) is not None:
        return "process-local"
    return ""


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _exclusive_project_lock_owner(raw: bytes) -> tuple[int, str] | None:
    """Read a complete v2 owner identity without granting lock recovery."""

    if re.fullmatch(rb"[0-9a-f]{32}(?:\r?\n)?", raw) is not None:
        # v1 closed its descriptor before entering the critical section and
        # persisted no PID/host generation.  Age therefore cannot prove that
        # its owner is gone: some pinned project operations have no uniform
        # wall-clock limit.  Preserve the legacy token for explicit recovery.
        return None

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "schemaVersion",
        "token",
        "ownerPid",
        "processStartIdentity",
        "acquiredAt",
        "lockSha256",
    }:
        return None
    token = value.get("token")
    owner_pid = value.get("ownerPid")
    stored_identity = value.get("processStartIdentity")
    acquired_at = value.get("acquiredAt")
    lock_sha256 = value.get("lockSha256")
    stored_scheme = (
        _validated_process_identity_scheme(stored_identity)
        if isinstance(stored_identity, str)
        else ""
    )
    if (
        value.get("schema") != EXCLUSIVE_PROJECT_LOCK_SCHEMA
        or value.get("schemaVersion") != EXCLUSIVE_PROJECT_LOCK_VERSION
        or not isinstance(token, str)
        or HEX32_RE.fullmatch(token) is None
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or owner_pid > 0xFFFFFFFF
        or not stored_scheme
        or not isinstance(acquired_at, str)
        or UTC_TIMESTAMP_RE.fullmatch(acquired_at) is None
        or not isinstance(lock_sha256, str)
        or SHA256_RE.fullmatch(lock_sha256) is None
        or lock_sha256 != _document_hash(value, "lockSha256")
    ):
        return None
    try:
        datetime.fromisoformat(acquired_at[:-1] + "+00:00")
    except ValueError:
        return None
    return owner_pid, stored_identity


def _recoverable_exclusive_project_lock(
    raw: bytes, unused_metadata: os.stat_result
) -> bool:
    """Return true only for a dead owner or reused PID proven by a v2 lock."""
    owner = _exclusive_project_lock_owner(raw)
    if owner is None:
        return False
    owner_pid, stored_identity = owner
    stored_scheme = _validated_process_identity_scheme(stored_identity)
    state, current_identity = _process_lock_owner_state(owner_pid)
    if state == "dead":
        return True
    if (
        state == "alive"
        and current_identity
        and current_identity != stored_identity
        and _validated_process_identity_scheme(current_identity) == stored_scheme
        and stored_scheme in {"windows-filetime", "linux-startticks"}
    ):
        return True
    return False


def _archive_recoverable_exclusive_project_lock(
    path: Path, *, is_recoverable: Callable[[bytes, os.stat_result], bool] | None = None,
) -> Path | None:
    """Atomically move one verified stale lock aside before reacquisition."""

    snapshot = _lock_snapshot(path)
    if snapshot is None:
        return None
    raw, metadata = snapshot
    validator = is_recoverable or _recoverable_exclusive_project_lock
    if not validator(raw, metadata):
        return None
    expected_identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        _stat_modified_time_ns(metadata),
    )
    archive = path.with_name("%s.recovered-%s" % (path.name, uuid.uuid4().hex))
    try:
        final = path.stat(follow_symlinks=False)
        if (
            final.st_dev,
            final.st_ino,
            final.st_size,
            _stat_modified_time_ns(final),
        ) != expected_identity:
            return None
        path.replace(archive)
        moved = archive.stat(follow_symlinks=False)
        moved_identity = (
            moved.st_dev,
            moved.st_ino,
            moved.st_size,
            _stat_modified_time_ns(moved),
        )
        if moved_identity != expected_identity or archive.read_bytes() != raw:
            if not path.exists() and not path.is_symlink():
                archive.replace(path)
            return None
        return archive
    except OSError:
        return None


def _publish_exclusive_project_lock(path: Path, payload: bytes) -> None:
    """Publish a complete lock payload with one no-replace filesystem step.

    Writing the final O_EXCL path in place leaves an empty/partial lock if the
    process dies between create and fsync.  A same-directory Windows rename or
    POSIX hard link makes the already-fsynced inode visible at the authoritative
    name atomically and fails when another owner already published that name.
    """

    temporary = path.with_name(".%s.pending-%s" % (path.name, uuid.uuid4().hex))
    descriptor = None
    try:
        descriptor = os.open(
            str(temporary),
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("exclusive project lock write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if os.name == "nt":
            # Windows rename is no-replace: Python raises FileExistsError when
            # the destination exists. It also works on volumes without links.
            os.rename(str(temporary), str(path))
        else:
            # POSIX rename replaces an existing destination, so use link as
            # the atomic no-replace publication primitive and drop the staging
            # name in the finally block.
            os.link(str(temporary), str(path))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass


def _release_owned_exclusive_lock(path: Path, payload: bytes, description: str) -> None:
    """Release our exact lock, allowing short Windows reader/share conflicts.

    A concurrent reader may temporarily prevent unlink or a stable snapshot.
    Silently ignoring either case leaves a live-owner lock after the critical
    section has ended. Check ownership again on every retry, so a replacement
    token is never removed by the previous owner.
    """
    deadline = time.monotonic() + EXCLUSIVE_LOCK_RELEASE_WAIT_SECONDS
    delay = 0.01
    last_error: OSError | None = None
    while True:
        snapshot = _lock_snapshot(path)
        if snapshot is not None:
            if snapshot[0] != payload:
                return
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError as error:
                last_error = error
        else:
            try:
                path.lstat()
            except FileNotFoundError:
                return
            except OSError as error:
                last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ActiveProjectError(
                "%s could not release its owned lock after the operation" % description
            ) from last_error
        time.sleep(min(delay, remaining))
        delay = min(0.1, delay * 2)


@contextmanager
def _exclusive_lock(path: Path, description: str, wait_seconds: float = 0.0) -> Iterator[None]:
    token = uuid.uuid4().hex
    payload = _exclusive_project_lock_payload(token)
    recovered = None
    deadline = time.monotonic() + max(0.0, wait_seconds)
    delay = 0.01
    while True:
        try:
            _publish_exclusive_project_lock(path, payload)
            break
        except FileExistsError as exc:
            if recovered is None:
                recovered = _archive_recoverable_exclusive_project_lock(path)
                if recovered is not None:
                    continue
            snapshot = _lock_snapshot(path)
            owner = _exclusive_project_lock_owner(snapshot[0]) if snapshot is not None else None
            remaining = deadline - time.monotonic()
            # A live competing Host can hold the short selection transaction.
            # Wait for its normal release; never steal it or trust lock age.
            # Unknown/legacy/corrupt owners retain the existing fail-closed path.
            owner_alive = owner is not None and _process_lock_owner_state(owner[0])[0] == "alive"
            disappeared = snapshot is None and not path.exists() and not path.is_symlink()
            if remaining > 0 and (owner_alive or disappeared):
                time.sleep(min(delay, remaining))
                delay = min(0.1, delay * 2)
                continue
            raise ActiveProjectError(
                "%s is locked; its owner could not be proven inactive"
                % description
            ) from exc
        except Exception:
            snapshot = _lock_snapshot(path)
            if snapshot is not None and snapshot[0] == payload:
                _release_owned_exclusive_lock(path, payload, description)
            raise
    try:
        yield
    finally:
        _release_owned_exclusive_lock(path, payload, description)
        if recovered is not None:
            try:
                recovered.unlink()
            except OSError:
                pass


def _regular_bytes(path: Path, maximum: int, description: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ActiveProjectError("%s must be a regular non-symlink file" % description)
    size = path.stat().st_size
    if size <= 0 or size > maximum:
        raise ActiveProjectError(
            "%s must contain 1..%d bytes" % (description, maximum)
        )
    value = path.read_bytes()
    if len(value) != size:
        raise ActiveProjectError("%s changed while it was read" % description)
    return value


def _staged_catalog_usage(catalogs_root: Path) -> tuple[int, int]:
    """Count published revision files without following redirected entries."""

    revision_count = 0
    total_bytes = 0
    for family in catalogs_root.iterdir():
        if family.name.startswith(".current-"):
            continue
        if family.is_symlink():
            raise ActiveProjectError("capture catalog family is redirected")
        if not family.is_dir():
            raise ActiveProjectError(
                "capture catalog root contains an unexpected file"
            )
        legacy = family / "catalog.json"
        if legacy.exists() or legacy.is_symlink():
            if legacy.is_symlink() or not legacy.is_file():
                raise ActiveProjectError("legacy capture catalog is redirected")
            revision_count += 1
        revisions = family / "revisions"
        if revisions.exists() or revisions.is_symlink():
            if revisions.is_symlink() or not revisions.is_dir():
                raise ActiveProjectError("capture catalog revisions are redirected")
            for revision in revisions.iterdir():
                if revision.is_symlink() or not revision.is_dir():
                    raise ActiveProjectError(
                        "capture catalog revision contains a redirected entry"
                    )
                catalog = revision / "catalog.json"
                if catalog.is_symlink() or not catalog.is_file():
                    raise ActiveProjectError(
                        "capture catalog revision is incomplete or redirected"
                    )
                revision_count += 1
        pending = [family]
        while pending:
            directory = pending.pop()
            for entry in directory.iterdir():
                if entry.is_symlink():
                    raise ActiveProjectError(
                        "capture catalog storage contains a redirected entry"
                    )
                if entry.is_dir():
                    pending.append(entry)
                elif entry.is_file():
                    total_bytes += entry.stat().st_size
                    if total_bytes > MAX_STAGED_CATALOG_STORAGE_BYTES:
                        return revision_count, total_bytes
                else:
                    raise ActiveProjectError(
                        "capture catalog storage contains a non-regular entry"
                    )
    return revision_count, total_bytes


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    if not slug:
        slug = "untitled"
    return slug[:96]


def _pinned_active_operation(function):
    """Run one active-project method under a stable selection/config snapshot."""

    @wraps(function)
    def guarded(self, *args, **kwargs):
        supplied = kwargs.pop("expected_project", None)
        if supplied is not None and not isinstance(supplied, Mapping):
            raise ActiveProjectError("expected_project must be a project status object")
        expected = dict(supplied) if supplied is not None else self.status()
        workspace_id = str(expected.get("workspaceId", "") or "")
        project_root = str(expected.get("projectRoot", "") or "")
        with self.pin_active_project(workspace_id, project_root):
            # The preparation already read/validated the project status. Only
            # selection/configuration identity belongs in this commit check;
            # do not rescan all replay artifacts while holding the selection.
            current_root, manifest = self._live_active()
            current = {
                "workspaceId": manifest["workspaceId"],
                "projectRoot": str(current_root),
                "state": manifest["state"],
                "scope": manifest["scope"],
                "assetRoot": manifest.get("assetRoot") or None,
            }
            if any(
                (
                    current.get("workspaceId") != expected.get("workspaceId"),
                    os.path.normcase(str(current.get("projectRoot", "") or ""))
                    != os.path.normcase(str(expected.get("projectRoot", "") or "")),
                    current.get("state") != expected.get("state"),
                    current.get("scope") != expected.get("scope"),
                    current.get("assetRoot") != expected.get("assetRoot"),
                )
            ):
                raise ActiveProjectError("active project changed before the operation")
            return function(self, *args, **kwargs)

    return guarded


def _selection_serialized(function):
    """Serialize operations that create or replace the active selection."""

    @wraps(function)
    def guarded(self, *args, **kwargs):
        with self._selection_guard():
            return function(self, *args, **kwargs)

    return guarded


class ActiveReplayProjectService:
    """Manage one persistent active ReplayLab project selection.

    ``projects_home`` remains the default location for automatically managed
    projects.  A sealed machine-local registry beside ``state_path`` also owns
    explicit references to user-selected portable project roots.  Removing a
    registry entry never removes the portable project directory.
    """

    def __init__(self, state_path: str | Path, projects_home: str | Path, registry_path=None):
        raw_state = Path(state_path).expanduser()
        if raw_state.exists() and raw_state.is_symlink():
            raise ActiveProjectError("active project state must not be a symlink")
        if raw_state.exists() and not raw_state.is_file():
            raise ActiveProjectError("active project state path must be a file")
        self.state_path = raw_state.resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.parent.is_symlink() or not self.state_path.parent.is_dir():
            raise ActiveProjectError("active project state parent is invalid")

        raw_home = Path(projects_home).expanduser()
        if raw_home.exists() and raw_home.is_symlink():
            raise ActiveProjectError("projects_home must not be a symlink")
        try:
            raw_home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ActiveProjectError("projects_home could not be created") from exc
        self.projects_home = raw_home.resolve()
        if self.projects_home.is_symlink() or not self.projects_home.is_dir():
            raise ActiveProjectError("projects_home must be a regular directory")
        if _filesystem_root(self.projects_home):
            raise ActiveProjectError("projects_home must not be a filesystem root")

        self.registry_path = Path(registry_path).absolute() if registry_path else self.state_path.parent / "project-registry.json"
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        if self.registry_path.exists() and self.registry_path.is_symlink():
            raise ActiveProjectError("project registry must not be a symlink")
        if self.registry_path.exists() and not self.registry_path.is_file():
            raise ActiveProjectError("project registry path must be a file")

    @property
    def _selection_lock_path(self) -> Path:
        return self.state_path.parent / (".%s.lock" % self.state_path.name)

    @contextmanager
    def _selection_guard(self, wait_seconds: float | None = None) -> Iterator[None]:
        """Hold the process/file selection lock, reusing it on this thread.

        ``wait_seconds`` bounds thread and cross-process selection contention.
        Short file transactions can overlap between the task and internal
        Hosts; waiting for their release does not grant stale-lock recovery.
        """

        key = os.path.normcase(str(self.state_path))
        thread_lock = _thread_lock(self.state_path)
        started = time.monotonic()
        if wait_seconds is None:
            thread_lock.acquire()
        elif not thread_lock.acquire(timeout=max(0.0, float(wait_seconds))):
            raise ActiveProjectError(
                "the project service is busy with another operation")
        try:
            remaining = None if wait_seconds is None else max(
                0.0, float(wait_seconds) - (time.monotonic() - started))
            yield from self._selection_guard_locked(key, remaining)
        finally:
            thread_lock.release()

    def _selection_guard_locked(self, key: str, wait_seconds: float | None = None) -> Iterator[None]:
        depths = getattr(_SELECTION_LOCK_DEPTH, "values", None)
        if depths is None:
            depths = {}
            _SELECTION_LOCK_DEPTH.values = depths
        depth = int(depths.get(key, 0))
        depths[key] = depth + 1
        try:
            if depth:
                yield
            else:
                with _exclusive_lock(
                    self._selection_lock_path, "active project selection",
                    SELECTION_FILE_LOCK_WAIT_SECONDS if wait_seconds is None else max(0.0, wait_seconds),
                ):
                    yield
        finally:
            if depth:
                depths[key] = depth
            else:
                depths.pop(key, None)

    def _project_root(self, value: str | Path) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ActiveProjectError("project root must be absolute")
        if raw.exists() and raw.is_symlink():
            raise ActiveProjectError("project root must not be a symlink")
        root = raw.resolve()
        if _filesystem_root(root) or root == self.projects_home:
            raise ActiveProjectError("project root must not be a project-home or filesystem root")
        return root

    def _registry_root(self, value: str | Path) -> Path:
        """Normalise a registry path without requiring its target to be usable.

        Registry removal must remain possible when a portable project was
        moved, disconnected, damaged, or replaced by a symlink. Operations
        that read/write project content still go through ``_project_root``.
        """

        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ActiveProjectError("registered project root must be absolute")
        root = Path(os.path.abspath(str(raw)))
        if _filesystem_root(root) or self._registry_key(root) == self._registry_key(
            self.projects_home
        ):
            raise ActiveProjectError(
                "registered project root must not be a project-home or filesystem root"
            )
        return root

    @staticmethod
    def _registry_key(root: Path) -> str:
        return os.path.normcase(os.path.abspath(str(root)))

    def _initial_project_registry(self) -> dict[str, Any]:
        records = []
        for candidate in sorted(
            self.projects_home.iterdir(), key=lambda item: item.name.lower()
        ):
            if candidate.name.startswith(".") or candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                manifest = self._read_workspace(candidate.resolve())
            except Exception:
                continue
            records.append(
                {
                    "workspaceId": str(manifest["workspaceId"]),
                    "projectRoot": str(candidate.resolve()),
                    "name": str(manifest["name"]),
                    "registeredAt": str(manifest["createdAt"]),
                }
            )
        return {
            "schema": PROJECT_REGISTRY_SCHEMA,
            "schemaVersion": PROJECT_REGISTRY_VERSION,
            "projects": records,
            "updatedAt": _utc_now(),
        }

    def _write_project_registry(self, value: Mapping[str, Any]) -> dict[str, Any]:
        material = dict(value)
        material["updatedAt"] = _utc_now()
        material = _seal(material, "registrySha256")
        atomic_write_json(self.registry_path, material)
        return material

    def _read_project_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            value = self._initial_project_registry()
            return self._write_project_registry(value)
        try:
            value = read_json_object(
                self.registry_path, MAX_CONFIG_BYTES, "project registry"
            )
        except ValueError as exc:
            raise ActiveProjectError(str(exc)) from exc
        if set(value) != {
            "schema",
            "schemaVersion",
            "projects",
            "updatedAt",
            "registrySha256",
        }:
            raise ActiveProjectError("project registry fields are invalid")
        if (
            value.get("schema") != PROJECT_REGISTRY_SCHEMA
            or value.get("schemaVersion") != PROJECT_REGISTRY_VERSION
        ):
            raise ActiveProjectError("unsupported project registry schema/version")
        _verify_seal(value, "registrySha256", "project registry")
        projects = value.get("projects")
        if not isinstance(projects, list) or len(projects) > MAX_CAPTURE_LIBRARY_RECORDS:
            raise ActiveProjectError("project registry is oversized")
        roots: set[str] = set()
        workspaces: set[str] = set()
        normalised = []
        for record in projects:
            if not isinstance(record, dict) or set(record) != {
                "workspaceId",
                "projectRoot",
                "name",
                "registeredAt",
            }:
                raise ActiveProjectError("project registry entry is invalid")
            workspace_id = str(record.get("workspaceId", "") or "")
            if not HEX32_RE.fullmatch(workspace_id):
                raise ActiveProjectError("registered workspaceId is invalid")
            root_text = _text(record.get("projectRoot"), "registered projectRoot", 4096)
            root = self._registry_root(root_text)
            key = self._registry_key(root)
            if key in roots or workspace_id in workspaces:
                raise ActiveProjectError("project registry contains a duplicate project")
            roots.add(key)
            workspaces.add(workspace_id)
            normalised.append(
                {
                    "workspaceId": workspace_id,
                    "projectRoot": str(root),
                    "name": _text(record.get("name"), "registered project name", 256),
                    "registeredAt": _text(
                        record.get("registeredAt"), "project registration time", 128
                    ),
                }
            )
        result = dict(value)
        result["projects"] = normalised
        return result

    def _registered_project_root(self, value: str | Path) -> Path:
        root = self._project_root(value)
        key = self._registry_key(root)
        if not any(
            self._registry_key(Path(record["projectRoot"])) == key
            for record in self._read_project_registry()["projects"]
        ):
            raise ActiveProjectError("project is not registered on this machine")
        return root

    def _register_project_root(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> None:
        from .shared_file_io import file_transaction
        with file_transaction(self.registry_path):
            registry = self._read_project_registry()
            key = self._registry_key(root)
            workspace_id = str(manifest["workspaceId"])
            for record in registry["projects"]:
                if self._registry_key(Path(record["projectRoot"])) == key:
                    if record["workspaceId"] == workspace_id:
                        return
                    raise ActiveProjectError("project path is registered with another workspace")
                if record["workspaceId"] == workspace_id:
                    raise ActiveProjectError("workspace is already registered at another path")
            registry["projects"].append(
                {
                    "workspaceId": workspace_id,
                    "projectRoot": str(root),
                    "name": str(manifest["name"]),
                    "registeredAt": _utc_now(),
                }
            )
            self._write_project_registry(registry)

    @staticmethod
    def _config_path(root: Path) -> Path:
        return root / _LAYOUT["workspaceManifest"]

    @staticmethod
    def _project_lock_path(root: Path) -> Path:
        return root / _LAYOUT["metadataDirectory"] / ".active-project.lock"

    def _managed_path(self, root: Path, relative: str, label: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(
            part in ("", ".", "..") for part in pure.parts
        ):
            raise ActiveProjectError("%s is not a safe project-relative path" % label)
        candidate = root.joinpath(*pure.parts)
        cursor = root
        for component in pure.parts:
            cursor = cursor / component
            if cursor.exists() and cursor.is_symlink():
                raise ActiveProjectError("%s traverses a symlink" % label)
        resolved = candidate.resolve()
        if not is_within(root, resolved):
            raise ActiveProjectError("%s escapes the project" % label)
        return resolved

    def _paths(self, root: Path) -> dict[str, str]:
        return {
            key: str(self._managed_path(root, relative, "layout.%s" % key))
            for key, relative in _LAYOUT.items()
        }

    def _initial_shared_index(self, workspace_id: str) -> dict[str, Any]:
        return _seal(
            {
                "schema": SHARED_DICTIONARY_SCHEMA,
                "schemaVersion": SHARED_DICTIONARY_VERSION,
                "workspaceId": workspace_id,
                "explanatory": [],
                "updatedAt": _utc_now(),
            },
            "indexSha256",
        )

    def _read_workspace(
        self, root: Path, *, validate_shared_artifacts: bool = True
    ) -> dict[str, Any]:
        if root.is_symlink() or not root.is_dir():
            raise ActiveProjectError("project root must be a regular directory")
        try:
            value = read_json_object(
                self._config_path(root), MAX_CONFIG_BYTES, "project workspace manifest"
            )
        except ValueError as exc:
            raise ActiveProjectError(str(exc)) from exc
        expected_keys = {
            "schema",
            "schemaVersion",
            "workspaceId",
            "name",
            "state",
            "scope",
            "assetRoot",
            "layout",
            "createdAt",
            "updatedAt",
            "configSha256",
        }
        if set(value) != expected_keys:
            raise ActiveProjectError("project workspace manifest fields are invalid")
        if (
            value.get("schema") != PROJECT_WORKSPACE_SCHEMA
            or value.get("schemaVersion") != PROJECT_WORKSPACE_VERSION
        ):
            raise ActiveProjectError("unsupported project workspace schema/version")
        _verify_seal(value, "configSha256", "project workspace manifest")
        if not HEX32_RE.fullmatch(str(value.get("workspaceId", ""))):
            raise ActiveProjectError("workspaceId is invalid")
        _text(value.get("name"), "name", 256, allow_empty=True)
        state = value.get("state")
        if state not in ("pending-scope", "bound"):
            raise ActiveProjectError("project workspace state is invalid")
        scope = value.get("scope")
        if not isinstance(scope, dict) or set(scope) != {
            "gameId",
            "buildId",
            "platform",
            "apiFamily",
        }:
            raise ActiveProjectError("project workspace scope is invalid")
        game = _text(scope.get("gameId"), "scope.gameId", 256, allow_empty=True)
        build = _text(scope.get("buildId"), "scope.buildId", 256, allow_empty=True)
        _text(scope.get("platform"), "scope.platform", 128, allow_empty=True)
        if _api_family(scope.get("apiFamily")) != scope.get("apiFamily"):
            raise ActiveProjectError("scope.apiFamily is not canonical")
        if state == "pending-scope" and any(scope.values()):
            raise ActiveProjectError("pending project must not carry a partial scope")
        if state == "bound" and (not game or not build):
            raise ActiveProjectError("bound project requires gameId and buildId")
        if value.get("layout") != _LAYOUT:
            raise ActiveProjectError("project workspace layout is invalid")
        asset_root = value.get("assetRoot")
        _text(asset_root, "assetRoot", 4096, allow_empty=True)
        if asset_root and not Path(asset_root).is_absolute():
            raise ActiveProjectError("assetRoot must be an absolute path")
        if asset_root:
            resolved_asset = Path(asset_root).resolve()
            if (
                _filesystem_root(resolved_asset)
                or resolved_asset == root
                or is_within(resolved_asset, root)
            ):
                raise ActiveProjectError("assetRoot has an unsafe project boundary")
        for key in _DIRECTORY_LAYOUT_KEYS:
            directory = self._managed_path(root, _LAYOUT[key], "layout.%s" % key)
            if directory.is_symlink() or not directory.is_dir():
                raise ActiveProjectError("project directory is missing or redirected: %s" % key)
        if validate_shared_artifacts:
            self._read_shared_index(root, str(value["workspaceId"]))
        return value

    def _write_workspace(self, root: Path, value: dict[str, Any]) -> None:
        material = dict(value)
        material["updatedAt"] = _utc_now()
        material = _seal(material, "configSha256")
        atomic_write_json(self._config_path(root), material)

    def _read_selection(self) -> dict[str, Any]:
        try:
            value = read_json_object(
                self.state_path, MAX_CONFIG_BYTES, "active project selection"
            )
        except ValueError as exc:
            raise ActiveProjectError(str(exc)) from exc
        if set(value) != {
            "schema",
            "schemaVersion",
            "workspaceId",
            "projectRoot",
            "selectedAt",
            "selectionSha256",
        }:
            raise ActiveProjectError("active project selection fields are invalid")
        if (
            value.get("schema") != ACTIVE_SELECTION_SCHEMA
            or value.get("schemaVersion") != ACTIVE_SELECTION_VERSION
        ):
            raise ActiveProjectError("unsupported active selection schema/version")
        _verify_seal(value, "selectionSha256", "active project selection")
        if not HEX32_RE.fullmatch(str(value.get("workspaceId", ""))):
            raise ActiveProjectError("active workspaceId is invalid")
        root_text = _text(value.get("projectRoot"), "projectRoot", 4096)
        if not Path(root_text).is_absolute():
            raise ActiveProjectError("active projectRoot must be absolute")
        return value

    def _write_selection(self, root: Path, workspace_id: str) -> None:
        value = _seal(
            {
                "schema": ACTIVE_SELECTION_SCHEMA,
                "schemaVersion": ACTIVE_SELECTION_VERSION,
                "workspaceId": workspace_id,
                "projectRoot": str(root),
                "selectedAt": _utc_now(),
            },
            "selectionSha256",
        )
        with self._selection_guard():
            try:
                current = self._read_selection()
            except ActiveProjectError:
                current = None
            if (current is not None and current["workspaceId"] == workspace_id
                    and os.path.normcase(current["projectRoot"]) == os.path.normcase(str(root))):
                # Activation of the current project is idempotent. Refreshing
                # selectedAt would invalidate a long-operation selection seal
                # without changing any project identity or configuration.
                return
            atomic_write_json(self.state_path, value)

    @staticmethod
    def _capture_ref_id(path: Path) -> str:
        canonical = os.path.normcase(str(path.resolve())).replace("\\", "/")
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _initial_capture_library(self, workspace_id: str) -> dict[str, Any]:
        now = _utc_now()
        return _seal(
            {
                "schema": CAPTURE_LIBRARY_SCHEMA,
                "schemaVersion": CAPTURE_LIBRARY_VERSION,
                "workspaceId": workspace_id,
                "captures": [],
                "createdAt": now,
                "updatedAt": now,
            },
            "librarySha256",
        )

    def _capture_library_path(self, root: Path) -> Path:
        return self._managed_path(
            root, _CAPTURE_LIBRARY_RELATIVE, "capture library"
        )

    def _read_capture_library(
        self, root: Path, workspace_id: str
    ) -> dict[str, Any]:
        path = self._capture_library_path(root)
        if not path.exists():
            # Projects created before the launcher feature remain readable. The
            # empty library is materialised only by an explicit add operation.
            return self._initial_capture_library(workspace_id)
        try:
            value = read_json_object(
                path, MAX_CONFIG_BYTES, "project capture library"
            )
        except ValueError as exc:
            raise ActiveProjectError(str(exc)) from exc
        expected = {
            "schema",
            "schemaVersion",
            "workspaceId",
            "captures",
            "createdAt",
            "updatedAt",
            "librarySha256",
        }
        if set(value) != expected:
            raise ActiveProjectError("project capture library fields are invalid")
        library_version = value.get("schemaVersion")
        if (
            value.get("schema") != CAPTURE_LIBRARY_SCHEMA
            or library_version not in (1, CAPTURE_LIBRARY_VERSION)
            or value.get("workspaceId") != workspace_id
        ):
            raise ActiveProjectError("project capture library identity is invalid")
        _verify_seal(value, "librarySha256", "project capture library")
        captures = value.get("captures")
        if (
            not isinstance(captures, list)
            or len(captures) > MAX_CAPTURE_LIBRARY_RECORDS
        ):
            raise ActiveProjectError("project capture library is oversized")
        seen: set[str] = set()
        normalised_records = []
        for record in captures:
            if not isinstance(record, dict) or set(record) not in ({
                "captureRefId",
                "path",
                "displayName",
                "addedAt",
            }, {
                "captureRefId",
                "path",
                "displayName",
                "addedAt",
                "contentIdentity",
            }):
                raise ActiveProjectError("project capture reference is invalid")
            capture_ref_id = _sha256(
                record.get("captureRefId"), "captureRefId"
            )
            if capture_ref_id in seen:
                raise ActiveProjectError("project capture reference is duplicated")
            seen.add(capture_ref_id)
            path_text = _text(record.get("path"), "capture.path", 4096)
            capture_path = Path(path_text)
            if not capture_path.is_absolute() or capture_path.suffix.lower() != ".rdc":
                raise ActiveProjectError("capture path must be an absolute .rdc path")
            if self._capture_ref_id(capture_path) != capture_ref_id:
                raise ActiveProjectError("capture reference identity does not match its path")
            _text(record.get("displayName"), "capture.displayName", 512)
            _text(record.get("addedAt"), "capture.addedAt", 128)
            identity = record.get("contentIdentity")
            if identity is not None:
                if not isinstance(identity, dict) or set(identity) != {
                    "schema",
                    "algorithm",
                    "sha256",
                    "sizeBytes",
                    "modifiedTimeNs",
                    "fileIdentity",
                }:
                    raise ActiveProjectError("capture content identity is invalid")
                if (
                    identity.get("schema") != CAPTURE_CONTENT_IDENTITY_SCHEMA
                    or identity.get("algorithm")
                    != CAPTURE_CONTENT_IDENTITY_ALGORITHM
                ):
                    raise ActiveProjectError(
                        "capture content identity schema/algorithm is invalid"
                    )
                _sha256(identity.get("sha256"), "capture.contentIdentity.sha256")
                for field in ("sizeBytes", "modifiedTimeNs"):
                    number = identity.get(field)
                    if isinstance(number, bool):
                        raise ActiveProjectError(
                            "capture content identity metadata is invalid"
                        )
                    try:
                        number = int(number)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ActiveProjectError(
                            "capture content identity metadata is invalid"
                        ) from exc
                    if number < 0:
                        raise ActiveProjectError(
                            "capture content identity metadata is invalid"
                        )
                _text(
                    identity.get("fileIdentity"),
                    "capture.contentIdentity.fileIdentity",
                    128,
                )
                identity = dict(identity)
            clean_record = dict(record)
            # A v1 record is readable but deliberately has no authority for
            # path-triggered project activation. Re-adding the same RDC upgrades
            # it with a full content identity and publishes v2 on the next write.
            clean_record["contentIdentity"] = identity
            normalised_records.append(clean_record)
        _text(value.get("createdAt"), "captureLibrary.createdAt", 128)
        _text(value.get("updatedAt"), "captureLibrary.updatedAt", 128)
        normalised = dict(value)
        normalised["schemaVersion"] = CAPTURE_LIBRARY_VERSION
        normalised["captures"] = normalised_records
        return normalised

    def _write_capture_library(self, root: Path, value: dict[str, Any]) -> None:
        material = dict(value)
        material["schemaVersion"] = CAPTURE_LIBRARY_VERSION
        material["updatedAt"] = _utc_now()
        material = _seal(material, "librarySha256")
        atomic_write_json(self._capture_library_path(root), material)

    @staticmethod
    def _validate_capture_file(value: str | Path) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ActiveProjectError("capture path must be absolute")
        if raw.suffix.lower() != ".rdc":
            raise ActiveProjectError("capture path must name an .rdc file")
        if raw.is_symlink() or not raw.is_file():
            raise ActiveProjectError("capture must be an existing regular non-symlink file")
        return raw.resolve()

    def _prepare_capture_record(
        self, capture_path: Path, added_at: str | None = None
    ) -> dict[str, Any]:
        return {
            "captureRefId": self._capture_ref_id(capture_path),
            "path": str(capture_path),
            "displayName": capture_path.name,
            "addedAt": added_at or _utc_now(),
            # Adding a reference must stay metadata-only. Full RDC hashing used
            # to block this operation for large captures and never established
            # a game/build boundary. Legacy identities remain readable, but new
            # references are associated by one unambiguous registered path.
            "contentIdentity": None,
        }

    def _capture_library_status(
        self, root: Path, workspace_id: str
    ) -> dict[str, Any]:
        library = self._read_capture_library(root, workspace_id)
        records = []
        for record in library["captures"]:
            capture_path = Path(record["path"])
            available = bool(
                capture_path.is_file() and not capture_path.is_symlink()
            )
            size = None
            modified_ns = None
            if available:
                try:
                    stat_result = capture_path.stat()
                    size = int(stat_result.st_size)
                    modified_ns = int(stat_result.st_mtime_ns)
                except OSError:
                    available = False
            records.append(
                {
                    **record,
                    "available": available,
                    "sizeBytes": size,
                    "modifiedTimeNs": modified_ns,
                    "thumbnail": {
                        "state": "embedded-lazy" if available else "placeholder",
                        "source": "rdc-container-metadata",
                        "replayRequired": False,
                        "reason": (
                            "qrenderdoc loads the embedded RDC thumbnail lazily"
                            if available
                            else "RDC file is unavailable"
                        ),
                    },
                }
            )
        return {
            "schema": CAPTURE_LIBRARY_SCHEMA,
            "schemaVersion": CAPTURE_LIBRARY_VERSION,
            "workspaceId": workspace_id,
            "count": len(records),
            "captures": records,
            "policy": {
                "referencesOnly": True,
                "captureFilesCopied": False,
                "automaticAssociationRequiresContentIdentity": False,
                "automaticAssociationMode": "unique-normalised-path",
                "legacyPathOnlyAssociationAllowed": True,
                "contentIdentityAlgorithm": CAPTURE_CONTENT_IDENTITY_ALGORITHM,
                "contentIdentityMaximumBytes": MAX_CAPTURE_IDENTITY_BYTES,
                "contentIdentityMaximumSeconds": MAX_CAPTURE_IDENTITY_SECONDS,
                "thumbnailExtractionSynchronous": False,
                "thumbnailReplayRequired": False,
                "thumbnailLoadingPolicy": "ui-thread-lazy-bounded-cache",
            },
        }

    def _read_shared_index(self, root: Path, workspace_id: str) -> dict[str, Any]:
        path = self._managed_path(root, _LAYOUT["sharedIndex"], "shared index")
        try:
            value = read_json_object(path, MAX_CONFIG_BYTES, "shared dictionary index")
        except ValueError as exc:
            raise ActiveProjectError(str(exc)) from exc
        if set(value) != {
            "schema",
            "schemaVersion",
            "workspaceId",
            "explanatory",
            "updatedAt",
            "indexSha256",
        }:
            raise ActiveProjectError("shared dictionary index fields are invalid")
        if (
            value.get("schema") != SHARED_DICTIONARY_SCHEMA
            or value.get("schemaVersion") != SHARED_DICTIONARY_VERSION
            or value.get("workspaceId") != workspace_id
        ):
            raise ActiveProjectError("shared dictionary identity is invalid")
        _verify_seal(value, "indexSha256", "shared dictionary index")
        records = value.get("explanatory")
        if not isinstance(records, list) or len(records) > MAX_EXPLANATORY_RECORDS:
            raise ActiveProjectError("shared explanatory index is invalid or oversized")
        identities: set[tuple[str, str, str]] = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "knowledgeId",
                "kind",
                "artifactPath",
                "artifactSha256",
                "registeredAt",
                "policy",
            }:
                raise ActiveProjectError("shared explanatory record is invalid")
            knowledge_id = str(record.get("knowledgeId", ""))
            if not HEX32_RE.fullmatch(knowledge_id):
                raise ActiveProjectError("shared explanatory knowledgeId is invalid")
            kind = _text(record.get("kind"), "explanatory.kind", 64)
            if kind not in {"mermaid-flowchart", "shader-flow-graph"}:
                raise ActiveProjectError("unsupported shared explanatory kind")
            digest = _sha256(record.get("artifactSha256"), "artifactSha256")
            identity = (knowledge_id, kind, digest)
            if identity in identities:
                raise ActiveProjectError("duplicate shared explanatory record")
            identities.add(identity)
            if record.get("policy") != {
                "explanatoryOnly": True,
                "physicalEvidence": False,
                "applyAuthorization": False,
                "aiProvisionalAutoApply": False,
            }:
                raise ActiveProjectError("shared explanatory policy is invalid")
            artifact_relative = _text(
                record.get("artifactPath"), "explanatory.artifactPath", 4096
            )
            _text(record.get("registeredAt"), "explanatory.registeredAt", 128)
            artifact = self._managed_path(
                root, artifact_relative, "shared explanatory artifact"
            )
            if hashlib.sha256(
                _regular_bytes(
                    artifact,
                    MAX_EXPLANATORY_BYTES,
                    "shared explanatory artifact",
                )
            ).hexdigest() != digest:
                raise ActiveProjectError("shared explanatory artifact hash mismatch")
        return value

    def _write_shared_index(self, root: Path, value: dict[str, Any]) -> None:
        material = dict(value)
        material["updatedAt"] = _utc_now()
        material = _seal(material, "indexSha256")
        path = self._managed_path(root, _LAYOUT["sharedIndex"], "shared index")
        atomic_write_json(path, material)

    def has_active_project(self) -> bool:
        return self.state_path.is_file() and not self.state_path.is_symlink()

    @_selection_serialized
    def create_pending(
        self, project_root: str | Path, name: str = "Untitled Bread Project"
    ) -> dict[str, Any]:
        root = self._project_root(project_root)
        if root.exists():
            raise ActiveProjectError("project path already exists: %s" % root)
        root.parent.mkdir(parents=True, exist_ok=True)
        if not is_within(self.projects_home, root.parent.resolve()):
            raise ActiveProjectError("project parent escapes projects_home")
        workspace_id = uuid.uuid4().hex
        staging = Path(
            tempfile.mkdtemp(prefix=".%s.staging-" % root.name, dir=str(root.parent))
        )
        now = _utc_now()
        manifest = _seal(
            {
                "schema": PROJECT_WORKSPACE_SCHEMA,
                "schemaVersion": PROJECT_WORKSPACE_VERSION,
                "workspaceId": workspace_id,
                "name": _text(name, "name", 256, allow_empty=True),
                "state": "pending-scope",
                "scope": {
                    "gameId": "",
                    "buildId": "",
                    "platform": "",
                    "apiFamily": "",
                },
                "assetRoot": "",
                "layout": dict(_LAYOUT),
                "createdAt": now,
                "updatedAt": now,
            },
            "configSha256",
        )
        try:
            for key in _DIRECTORY_LAYOUT_KEYS:
                staging.joinpath(*PurePosixPath(_LAYOUT[key]).parts).mkdir(
                    parents=True, exist_ok=True
                )
            atomic_write_json(
                staging.joinpath(*PurePosixPath(_LAYOUT["sharedIndex"]).parts),
                self._initial_shared_index(workspace_id),
            )
            atomic_write_json(
                staging.joinpath(*PurePosixPath(_LAYOUT["workspaceManifest"]).parts),
                manifest,
            )
            atomic_write_json(
                staging.joinpath(*PurePosixPath(_CAPTURE_LIBRARY_RELATIVE).parts),
                self._initial_capture_library(workspace_id),
            )
            staging.replace(root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        manifest = self._read_workspace(root)
        self._register_project_root(root, manifest)
        self._write_selection(root, workspace_id)
        return self.status()

    @_selection_serialized
    def ensure_pending(
        self, name: str = "Untitled Bread Project"
    ) -> dict[str, Any]:
        if self.has_active_project():
            return self.status()
        slug = _safe_slug(name)
        root = self.projects_home / slug
        if root.exists():
            try:
                return self.activate(root)
            except ActiveProjectError:
                root = self.projects_home / ("%s-%s" % (slug, uuid.uuid4().hex[:8]))
        return self.create_pending(root, name)

    def create_project(
        self,
        *,
        name: str,
        game_id: str,
        build_id: str,
        platform: str = "",
        api_family: str = "",
        asset_root: str | Path | None = None,
        capture_files: Sequence[str | Path] = (),
        project_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Create, bind, register, activate, and optionally populate one project.

        All caller-controlled paths are validated before publishing the project
        directory. When no explicit game/build pair is supplied, the portable
        workspace itself becomes the private sharing boundary. Capture-specific
        catalog/layout/ABI checks still gate every later physical reuse.
        """

        project_name = _text(name, "name", 256)
        game = _text(game_id, "game_id", 256, allow_empty=True)
        build = _text(build_id, "build_id", 256, allow_empty=True)
        if bool(game) != bool(build):
            raise ActiveProjectError(
                "game_id and build_id must either both be supplied or both be empty"
            )
        platform_value = _text(platform, "platform", 128, allow_empty=True)
        api = _api_family(api_family)
        captures = [self._validate_capture_file(value) for value in capture_files]
        if len(captures) > MAX_CAPTURE_LIBRARY_RECORDS:
            raise ActiveProjectError("too many capture files were supplied")
        prepared_records = []
        prepared_ids = set()
        for capture in captures:
            capture_ref_id = self._capture_ref_id(capture)
            if capture_ref_id in prepared_ids:
                continue
            prepared_ids.add(capture_ref_id)
            prepared_records.append(self._prepare_capture_record(capture))

        asset_value: Path | None = None
        if asset_root is not None and str(asset_root).strip():
            raw_asset = Path(asset_root).expanduser()
            if not raw_asset.is_absolute() or raw_asset.is_symlink():
                raise ActiveProjectError("asset root must be an absolute non-symlink directory")
            asset_value = raw_asset.resolve()
            if not asset_value.is_dir() or _filesystem_root(asset_value):
                raise ActiveProjectError("asset root must be a non-root directory")

        slug = _safe_slug(project_name)
        with self._selection_guard():
            # Materialise the registry before choosing a new path so legacy
            # projects under projects_home are migrated exactly once.
            self._read_project_registry()
            if project_root is None:
                root = self.projects_home / slug
                if root.exists():
                    root = self.projects_home / ("%s-%s" % (slug, uuid.uuid4().hex[:8]))
            else:
                root = Path(project_root).expanduser()
            root = self._project_root(root)
            if root.exists():
                raise ActiveProjectError("project path already exists: %s" % root)
            root.parent.mkdir(parents=True, exist_ok=True)
            if root.parent.is_symlink() or not root.parent.is_dir():
                raise ActiveProjectError("project parent must be a regular directory")
            if asset_value is not None and (
                asset_value == root or is_within(asset_value, root)
            ):
                raise ActiveProjectError("asset root must not contain the project root")

            workspace_id = uuid.uuid4().hex
            if not game:
                game = "replaylab-project"
                build = workspace_id
                platform_value = ""
                api = ""
            staging = Path(
                tempfile.mkdtemp(
                    prefix=".%s.staging-" % root.name, dir=str(root.parent)
                )
            )
            now = _utc_now()
            manifest = _seal(
                {
                    "schema": PROJECT_WORKSPACE_SCHEMA,
                    "schemaVersion": PROJECT_WORKSPACE_VERSION,
                    "workspaceId": workspace_id,
                    "name": project_name,
                    "state": "bound",
                    "scope": {
                        "gameId": game,
                        "buildId": build,
                        "platform": platform_value,
                        "apiFamily": api,
                    },
                    "assetRoot": str(asset_value) if asset_value is not None else "",
                    "layout": dict(_LAYOUT),
                    "createdAt": now,
                    "updatedAt": now,
                },
                "configSha256",
            )
            library = self._initial_capture_library(workspace_id)
            library["captures"] = prepared_records
            library = _seal(
                {
                    **library,
                    "schemaVersion": CAPTURE_LIBRARY_VERSION,
                    "updatedAt": _utc_now(),
                },
                "librarySha256",
            )
            previous_selection_exists = bool(
                self.state_path.exists() and not self.state_path.is_symlink()
            )
            previous_selection = None
            if previous_selection_exists:
                try:
                    if self.state_path.stat().st_size > MAX_CONFIG_BYTES:
                        raise ActiveProjectError(
                            "active project selection is too large to preserve"
                        )
                    previous_selection = self.state_path.read_bytes()
                except OSError as exc:
                    raise ActiveProjectError(
                        "active project selection could not be preserved"
                    ) from exc
            published = False
            try:
                for key in _DIRECTORY_LAYOUT_KEYS:
                    staging.joinpath(*PurePosixPath(_LAYOUT[key]).parts).mkdir(
                        parents=True, exist_ok=True
                    )
                atomic_write_json(
                    staging.joinpath(*PurePosixPath(_LAYOUT["sharedIndex"]).parts),
                    self._initial_shared_index(workspace_id),
                )
                atomic_write_json(
                    staging.joinpath(*PurePosixPath(_LAYOUT["workspaceManifest"]).parts),
                    manifest,
                )
                atomic_write_json(
                    staging.joinpath(*PurePosixPath(_CAPTURE_LIBRARY_RELATIVE).parts),
                    library,
                )
                ReplayProjectService.create(
                    staging.joinpath(
                        *PurePosixPath(_LAYOUT["replayProject"]).parts
                    ),
                    name=project_name,
                    game_id=game,
                    build_id=build,
                    platform=platform_value,
                    api_family=api,
                )
                # Validate every sealed document before making the directory
                # visible to project-list. That command shares this selection
                # lock, so no client can observe the rename mid-transaction.
                self._read_workspace(staging)
                self._read_capture_library(staging, workspace_id)
                staging.replace(root)
                published = True
                self._write_selection(root, workspace_id)
                result = self._project_status(root)
                self._register_project_root(root, manifest)
                return result
            except Exception:
                rollback_error = None
                quarantined = None
                if published and root.exists() and not root.is_symlink():
                    quarantined = root.parent / (
                        ".%s.failed-%s" % (root.name, workspace_id)
                    )
                    try:
                        root.replace(quarantined)
                    except OSError as exc:
                        rollback_error = exc
                if previous_selection_exists and previous_selection is not None:
                    atomic_write_bytes(self.state_path, previous_selection)
                elif not previous_selection_exists:
                    try:
                        self.state_path.unlink()
                    except FileNotFoundError:
                        pass
                if quarantined is not None and quarantined.exists():
                    shutil.rmtree(quarantined, ignore_errors=True)
                if rollback_error is not None:
                    raise ActiveProjectError(
                        "project creation failed and its published root could not be quarantined"
                    ) from rollback_error
                raise
            finally:
                if staging.exists() and not staging.is_symlink():
                    shutil.rmtree(staging, ignore_errors=True)

    def add_capture_files(
        self, project_root: str | Path, capture_files: Sequence[str | Path]
    ) -> dict[str, Any]:
        root = self._registered_project_root(project_root)
        captures = [self._validate_capture_file(value) for value in capture_files]
        if not captures:
            raise ActiveProjectError("at least one capture file is required")
        if len(captures) > MAX_CAPTURE_LIBRARY_RECORDS:
            raise ActiveProjectError("too many capture files were supplied")
        prepared = {}
        for capture_path in captures:
            capture_ref_id = self._capture_ref_id(capture_path)
            if capture_ref_id not in prepared:
                prepared[capture_ref_id] = self._prepare_capture_record(capture_path)
        with self._selection_guard():
            with _thread_lock(root), _exclusive_lock(
                self._project_lock_path(root), "project capture library"
            ):
                manifest = self._read_workspace(root)
                library = self._read_capture_library(root, str(manifest["workspaceId"]))
                by_id = {
                    record["captureRefId"]: record
                    for record in library["captures"]
                }
                for capture_ref_id, record in prepared.items():
                    existing = by_id.get(capture_ref_id)
                    if existing is not None:
                        # Re-adding is the explicit migration/rebind action for
                        # legacy records or a path whose RDC was replaced.
                        existing.update(record)
                        continue
                    if len(by_id) >= MAX_CAPTURE_LIBRARY_RECORDS:
                        raise ActiveProjectError("project capture library limit reached")
                    library["captures"].append(record)
                    by_id[capture_ref_id] = record
                self._write_capture_library(root, library)
        return self.project_status(root)

    def remove_capture_references(
        self, project_root: str | Path, capture_ref_ids: Sequence[str]
    ) -> dict[str, Any]:
        """Remove only capture-library records; referenced RDC files stay untouched."""

        root = self._registered_project_root(project_root)
        requested = {
            _sha256(value, "captureRefId") for value in list(capture_ref_ids or [])
        }
        if not requested:
            raise ActiveProjectError("at least one capture reference is required")
        with self._selection_guard():
            with _thread_lock(root), _exclusive_lock(
                self._project_lock_path(root), "project capture library"
            ):
                manifest = self._read_workspace(root)
                library = self._read_capture_library(root, str(manifest["workspaceId"]))
                existing = {
                    str(record["captureRefId"]) for record in library["captures"]
                }
                missing = requested - existing
                if missing:
                    raise ActiveProjectError("capture reference is not registered in this project")
                library["captures"] = [
                    record
                    for record in library["captures"]
                    if record["captureRefId"] not in requested
                ]
                self._write_capture_library(root, library)
        return self.project_status(root)

    def _project_status_from_manifest(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Build a fully validated project status from an already-read manifest."""

        status = self._status_from_active(root, manifest)
        status["captureLibrary"] = self._capture_library_status(
            root, str(manifest["workspaceId"])
        )
        return status

    def _project_status(self, root: Path) -> dict[str, Any]:
        manifest = self._read_workspace(root)
        return self._project_status_from_manifest(root, manifest)

    def project_status(self, project_root: str | Path) -> dict[str, Any]:
        return self._project_status(self._registered_project_root(project_root))

    def register_existing_project(self, project_root: str | Path) -> dict[str, Any]:
        """Validate and register a portable project without copying or activating it."""

        root = self._project_root(project_root)
        if root.is_symlink() or not root.is_dir():
            raise ActiveProjectError("existing project must be a regular directory")
        with self._selection_guard():
            manifest = self._read_workspace(root)
            self._read_capture_library(root, str(manifest["workspaceId"]))
            if manifest["state"] == "bound":
                self._replay_service_for(root, manifest)
            self._register_project_root(root, manifest)
            status = self._project_status(root)
            status["active"] = self._selection_matches(root, str(manifest["workspaceId"]))
            return status

    def _selection_matches(self, root: Path, workspace_id: str) -> bool:
        if not self.has_active_project():
            return False
        try:
            selection = self._read_selection()
        except ActiveProjectError:
            return False
        return (
            str(selection["workspaceId"]) == workspace_id
            and self._registry_key(Path(selection["projectRoot"]))
            == self._registry_key(root)
        )

    def unregister_project(self, project_root: str | Path) -> dict[str, Any]:
        """Forget one machine-local registry entry without touching project files."""

        root = self._registry_root(project_root)
        key = self._registry_key(root)
        from .shared_file_io import file_transaction
        with self._selection_guard(), file_transaction(self.registry_path):
            registry = self._read_project_registry()
            record = next(
                (
                    item
                    for item in registry["projects"]
                    if self._registry_key(Path(item["projectRoot"])) == key
                ),
                None,
            )
            if record is None:
                raise ActiveProjectError("project is not registered on this machine")
            registry["projects"] = [
                item
                for item in registry["projects"]
                if self._registry_key(Path(item["projectRoot"])) != key
            ]
            self._write_project_registry(registry)
            if self._selection_matches(root, str(record["workspaceId"])):
                try:
                    self.state_path.unlink()
                except FileNotFoundError:
                    pass
        return {
            "schema": "replaylab-project-unregister/v1",
            "schemaVersion": 1,
            "workspaceId": str(record["workspaceId"]),
            "projectRoot": str(root),
            "name": str(record["name"]),
            "removed": True,
            "projectFilesDeleted": False,
        }

    def configure_project_settings(
        self,
        project_root: str | Path,
        *,
        game_id: str = "",
        build_id: str = "",
        platform: str = "",
        api_family: str = "",
        asset_root: str | Path | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Configure an explicit project without changing active selection.

        Scope and asset-root validation completes before any write. If a
        pending scope is bound, the workspace manifest publishes that scope
        and the asset-root update together in one atomic replace. The replay
        project directory may be recovered after a crash by the same existing
        scope-consistency rules used by :meth:`configure_scope`.
        """

        root = self._registered_project_root(project_root)
        game = _text(game_id, "game_id", 256, allow_empty=True)
        build = _text(build_id, "build_id", 256, allow_empty=True)
        if bool(game) != bool(build):
            raise ActiveProjectError(
                "game_id and build_id must either both be supplied or both be empty"
            )
        desired_scope = None
        if game:
            desired_scope = {
                "gameId": game,
                "buildId": build,
                "platform": _text(
                    platform, "platform", 128, allow_empty=True
                ),
                "apiFamily": _api_family(api_family),
            }

        asset_supplied = asset_root is not _UNSET
        asset_text = ""
        if asset_supplied and asset_root is not None and str(asset_root).strip():
            raw_asset = Path(asset_root).expanduser()
            if not raw_asset.is_absolute() or raw_asset.is_symlink():
                raise ActiveProjectError(
                    "asset root must be an absolute non-symlink directory"
                )
            resolved_asset = raw_asset.resolve()
            if not resolved_asset.is_dir() or _filesystem_root(resolved_asset):
                raise ActiveProjectError("asset root must be a non-root directory")
            if resolved_asset == root or is_within(resolved_asset, root):
                raise ActiveProjectError("asset root must not contain the project root")
            asset_text = str(resolved_asset)

        # The target may be the active project. Serialise against
        # pin_active_project for the complete read/create/publish transaction so
        # no MCP operation can observe a different scope or asset-root midway.
        with self._selection_guard():
            with _thread_lock(root), _exclusive_lock(
                self._project_lock_path(root), "project settings"
            ):
                manifest = self._read_workspace(root)
                if manifest["state"] == "bound":
                    if desired_scope is not None and manifest["scope"] != desired_scope:
                        raise ActiveProjectError(
                            "project scope is immutable; create another project for a game/build"
                        )
                elif desired_scope is not None:
                    replay_root = self._managed_path(
                        root, _LAYOUT["replayProject"], "replay project root"
                    )
                    if replay_root.exists():
                        replay_status = ReplayProjectService(replay_root).status()
                        if replay_status["scope"] != desired_scope:
                            raise ActiveProjectError(
                                "existing replay project conflicts with requested scope"
                            )
                    else:
                        ReplayProjectService.create(
                            replay_root,
                            name=manifest["name"],
                            game_id=desired_scope["gameId"],
                            build_id=desired_scope["buildId"],
                            platform=desired_scope["platform"],
                            api_family=desired_scope["apiFamily"],
                        )
                    manifest["state"] = "bound"
                    manifest["scope"] = desired_scope
                if asset_supplied:
                    manifest["assetRoot"] = asset_text
                self._write_workspace(root, manifest)
        return self.project_status(root)

    LIST_LOCK_WAIT_SECONDS = 2.0

    def list_projects(self) -> dict[str, Any]:
        """List validated roots from the machine-local project registry."""
        try:
            with self._selection_guard(wait_seconds=self.LIST_LOCK_WAIT_SECONDS):
                return self._list_projects_locked()
        except ActiveProjectError as exc:
            if "busy" not in str(exc):
                raise
        # The selection thread lock is held by a long operation (an activation
        # validating a project, a registration). The registry and the selection
        # file are written atomically, so an unguarded read is a consistent
        # snapshot -- stale by at most that operation -- and the window opens.
        value = self._list_projects_locked()
        value["busy"] = True
        return value

    def _list_projects_locked(self) -> dict[str, Any]:
        active_workspace_id = ""
        if self.has_active_project():
            try:
                active_workspace_id = str(self._read_selection()["workspaceId"])
            except ActiveProjectError:
                # A corrupt selection must not make otherwise valid projects
                # disappear from the recovery/selection window.
                active_workspace_id = ""
        projects = []
        rejected = []
        registry = self._read_project_registry()
        for record in sorted(
            registry["projects"], key=lambda item: item["name"].lower()
        ):
            candidate = Path(record["projectRoot"])
            try:
                manifest=self._read_workspace(candidate,validate_shared_artifacts=False)
                status=self._status_from_active(candidate,manifest,validate_replay=False)
                status["captureLibrary"]=self._capture_library_status(candidate,str(manifest["workspaceId"]))
                status["validationScope"]="workspace-and-capture-library-metadata"
                if status["workspaceId"] != record["workspaceId"]:
                    raise ActiveProjectError(
                        "registered workspace identity does not match the project"
                    )
            except Exception as exc:
                rejected.append(
                    {
                        "workspaceId": record["workspaceId"],
                        "projectRoot": str(candidate),
                        "name": record["name"],
                        "reasonId": ("project-files-busy" if getattr(exc, "winerror", None) in (32, 33)
                                     else "project-access-denied" if isinstance(exc, PermissionError)
                                     else "project-validation-failed"),
                        "reasonDetail": str(exc)[:1024],
                        # Keep the legacy field for older bridge builds.
                        "reason": str(exc)[:1024],
                    }
                )
                continue
            status["active"] = status["workspaceId"] == active_workspace_id
            projects.append(status)
        return {
            "schema": "replaylab-project-list/v1",
            "schemaVersion": 1,
            "projectsHome": str(self.projects_home),
            "count": len(projects),
            "projects": projects,
            "rejected": rejected,
        }

    def _capture_project_candidates(
        self, capture_path: Path
    ) -> list[dict[str, Any]]:
        path_key = os.path.normcase(str(capture_path))
        candidates = []
        registry = self._read_project_registry()
        for registry_record in registry["projects"]:
            project_root = Path(registry_record["projectRoot"])
            try:
                manifest = self._read_workspace(project_root)
                if manifest["workspaceId"] != registry_record["workspaceId"]:
                    continue
                if manifest["state"] == "bound":
                    self._replay_service_for(project_root, manifest)
                library = self._read_capture_library(
                    project_root, str(manifest["workspaceId"])
                )
            except Exception:
                continue
            for record in library["captures"]:
                if os.path.normcase(str(record["path"])) != path_key:
                    continue
                candidates.append(
                    {
                        "workspaceId": str(manifest["workspaceId"]),
                        "projectRoot": str(project_root),
                        "name": str(manifest["name"]),
                        "state": str(manifest["state"]),
                        "scope": dict(manifest["scope"]),
                    }
                )
        return candidates

    def match_and_activate_capture(
        self, capture_path: str | Path, activation_guard=None
    ) -> dict[str, Any]:
        """Activate only one unambiguous registered capture-path association."""

        capture = self._validate_capture_file(capture_path)
        with self._selection_guard():
            matches_by_workspace = {}
            for candidate in self._capture_project_candidates(capture):
                matches_by_workspace[candidate["workspaceId"]] = {
                    key: candidate[key]
                    for key in (
                        "workspaceId",
                        "projectRoot",
                        "name",
                        "state",
                        "scope",
                    )
                }
            matches = list(matches_by_workspace.values())
            matches.sort(key=lambda value: value["projectRoot"].lower())
            project = matches[0] if len(matches) == 1 else None
            activation_allowed = bool(
                project is not None and
                (activation_guard is None or activation_guard())
            )
            if activation_allowed:
                self._write_selection(
                    Path(project["projectRoot"]), project["workspaceId"]
                )
        return {
            "schema": CAPTURE_PROJECT_MATCH_SCHEMA,
            "schemaVersion": 1,
            "capturePath": str(capture),
            "contentIdentity": None,
            "contentIdentityCacheHit": False,
            "matchCount": len(matches),
            "matches": matches,
            "activated": activation_allowed,
            "project": project,
            "associationMode": "unique-normalised-path",
            "reason": (
                "unique registered capture path matched and activated"
                if activation_allowed
                else (
                    "unique registered capture path matched but the live capture lease expired"
                    if project is not None
                    else (
                    "multiple projects contain the same registered capture path"
                    if matches
                    else "no project contains this capture path"
                )
                )
            ),
        }

    @_selection_serialized
    def activate(self, project_root: str | Path) -> dict[str, Any]:
        root = self._registered_project_root(project_root)
        manifest = self._read_workspace(root)
        # Validate the replay manifest/artifacts and capture library before
        # publishing the selection.  Reuse that same validated snapshot as the
        # command result instead of re-reading the whole project via status().
        status = self._project_status_from_manifest(root, manifest)
        self._write_selection(root, str(manifest["workspaceId"]))
        return status

    def _bound_project_snapshot(self) -> ActiveProjectSnapshot | None:
        key = os.path.normcase(str(self.state_path))
        return next((item for item in reversed(_ACTIVE_PROJECT_SNAPSHOTS.get())
                     if item.state_path == key), None)

    def _live_active(
        self, *, validate_shared_artifacts: bool = True
    ) -> tuple[Path, dict[str, Any]]:
        selection = self._read_selection()
        root = self._registered_project_root(selection["projectRoot"])
        workspace = self._read_workspace(
            root, validate_shared_artifacts=validate_shared_artifacts
        )
        if workspace["workspaceId"] != selection["workspaceId"]:
            raise ActiveProjectError("active selection does not match the project workspace")
        return root, workspace

    def _active(self) -> tuple[Path, dict[str, Any]]:
        snapshot = self._bound_project_snapshot()
        if snapshot is not None:
            return snapshot.project_root, json.loads(snapshot.manifest_json)
        return self._live_active()

    @contextmanager
    def snapshot_active_project(
        self, expected_workspace_id: str, expected_project_root: str | Path
    ) -> Iterator[ActiveProjectSnapshot]:
        """Bind long preparation/reads to one project without holding its selection lock.

        Check the snapshot before replay side effects, and use
        ``commit_active_project_snapshot`` for short durable publications. A
        selection or configuration change invalidates the operation; it never
        redirects its reads into the newly selected project. Context propagation
        through asyncio.to_thread also preserves this binding.
        """

        workspace_id = str(expected_workspace_id or "").strip().lower()
        if not HEX32_RE.fullmatch(workspace_id):
            raise ActiveProjectError("expected active workspaceId is invalid")
        expected_root = self._project_root(expected_project_root)
        with self._selection_guard():
            # Selection/configuration checkpoints are independent of explanatory
            # diagrams. Their readers and publishers validate their actual bytes.
            root, manifest = self._live_active(validate_shared_artifacts=False)
            if root != expected_root or manifest["workspaceId"] != workspace_id:
                raise ActiveProjectError("active project changed before the operation")
            selection = self._read_selection()
            snapshot = ActiveProjectSnapshot(
                os.path.normcase(str(self.state_path)), root, workspace_id,
                selection["selectionSha256"], manifest["configSha256"],
                _canonical_bytes(manifest),
            )
        token = _ACTIVE_PROJECT_SNAPSHOTS.set(_ACTIVE_PROJECT_SNAPSHOTS.get() + (snapshot,))
        try:
            yield snapshot
            self.assert_active_project_snapshot(snapshot)
        finally:
            _ACTIVE_PROJECT_SNAPSHOTS.reset(token)

    def _assert_active_project_snapshot_locked(self, snapshot: ActiveProjectSnapshot) -> None:
        if (not isinstance(snapshot, ActiveProjectSnapshot)
                or snapshot.state_path != os.path.normcase(str(self.state_path))):
            raise ActiveProjectError("project snapshot belongs to another selection service")
        selection = self._read_selection()
        root, manifest = self._live_active(validate_shared_artifacts=False)
        if (selection["selectionSha256"] != snapshot.selection_sha256
                or root != snapshot.project_root
                or manifest["workspaceId"] != snapshot.workspace_id
                or manifest["configSha256"] != snapshot.config_sha256):
            raise ActiveProjectError("active project selection or configuration changed during the operation")

    def assert_active_project_snapshot(self, snapshot: ActiveProjectSnapshot) -> None:
        """Check the real current selection/configuration at a bounded checkpoint."""

        with self._selection_guard():
            self._assert_active_project_snapshot_locked(snapshot)

    @contextmanager
    def commit_active_project_snapshot(self, snapshot: ActiveProjectSnapshot) -> Iterator[None]:
        """Validate a prepared operation immediately before its short atomic commit."""

        with self._selection_guard():
            self._assert_active_project_snapshot_locked(snapshot)
            yield

    @contextmanager
    def pin_active_project(
        self, expected_workspace_id: str, expected_project_root: str | Path
    ) -> Iterator[None]:
        """Prevent the active selection from crossing projects during one operation."""

        workspace_id = str(expected_workspace_id or "").strip().lower()
        if not HEX32_RE.fullmatch(workspace_id):
            raise ActiveProjectError("expected active workspaceId is invalid")
        raw_root = Path(expected_project_root).expanduser()
        if not raw_root.is_absolute():
            raise ActiveProjectError("expected active projectRoot must be absolute")
        expected_root = self._project_root(raw_root)
        with self._selection_guard():
            snapshot = self._bound_project_snapshot()
            if snapshot is not None:
                self._assert_active_project_snapshot_locked(snapshot)
            current_root, current_manifest = self._live_active()
            if (
                current_root != expected_root
                or current_manifest["workspaceId"] != workspace_id
            ):
                raise ActiveProjectError("active project changed before the operation")
            yield
            current_root, current_manifest = self._live_active()
            if (
                current_root != expected_root
                or current_manifest["workspaceId"] != workspace_id
            ):
                raise ActiveProjectError("active project changed during the operation")

    def status(self) -> dict[str, Any]:
        if self._bound_project_snapshot() is not None:
            root, manifest = self._active()
            return self._status_from_active(root, manifest)
        with self._selection_guard():
            root, manifest = self._active()
        # Replay artifact validation can be expensive. Its paths/configuration
        # are now fixed, so it must not hold up selection or unrelated readers.
        return self._status_from_active(root, manifest)

    def _status_from_active(
        self, root: Path, manifest: Mapping[str, Any], *, validate_replay: bool = True
    ) -> dict[str, Any]:
        paths = self._paths(root)
        asset_text = str(manifest.get("assetRoot", "") or "")
        asset_available = bool(
            asset_text
            and Path(asset_text).is_dir()
            and not Path(asset_text).is_symlink()
        )
        replay_status = None
        if manifest["state"] == "bound" and validate_replay:
            unused_service, replay_status = self._replay_service_and_status_for(
                root, manifest
            )
        return {
            "schema": PROJECT_WORKSPACE_SCHEMA,
            "schemaVersion": PROJECT_WORKSPACE_VERSION,
            "workspaceId": manifest["workspaceId"],
            "name": manifest["name"],
            "state": manifest["state"],
            "projectRoot": str(root),
            "scope": dict(manifest["scope"]),
            "scopeConfigured": manifest["state"] == "bound",
            "assetRoot": asset_text or None,
            "assetRootConfigured": bool(asset_text),
            "assetRootAvailable": asset_available,
            "paths": paths,
            "replayProject": replay_status,
            "policy": {
                "automaticPendingWorkspaceCreation": True,
                "physicalKnowledgeReuseAllowed": validate_replay and manifest["state"] == "bound",
                "crossGameAutomaticPhysicalReuse": False,
                "aiProvisionalAutoApply": False,
                "sourceSeedIsApplyAuthorization": False,
                "liveAbiRevalidationRequired": True,
                "liveMaterialLayoutRevalidationRequired": True,
            },
        }

    def configure_scope(
        self,
        *,
        game_id: str,
        build_id: str,
        platform: str = "",
        api_family: str = "",
    ) -> dict[str, Any]:
        game = _text(game_id, "game_id", 256)
        build = _text(build_id, "build_id", 256)
        platform_value = _text(
            platform, "platform", 128, allow_empty=True
        )
        api = _api_family(api_family)
        desired_scope = {
            "gameId": game,
            "buildId": build,
            "platform": platform_value,
            "apiFamily": api,
        }
        before = self.status()
        root = Path(str(before["projectRoot"])).resolve()
        with self.pin_active_project(before["workspaceId"], root):
            with _thread_lock(root), _exclusive_lock(
                self._project_lock_path(root), "active project configuration"
            ):
                manifest = self._read_workspace(root)
                if manifest["state"] == "bound":
                    if manifest["scope"] != desired_scope:
                        raise ActiveProjectError(
                            "project scope is immutable; create another project for a game/build"
                        )
                    return self.status()
                replay_root = self._managed_path(
                    root, _LAYOUT["replayProject"], "replay project root"
                )
                if replay_root.exists():
                    replay_status = ReplayProjectService(replay_root).status()
                    if replay_status["scope"] != desired_scope:
                        raise ActiveProjectError(
                            "existing replay project conflicts with requested scope"
                        )
                else:
                    ReplayProjectService.create(
                        replay_root,
                        name=manifest["name"],
                        game_id=game,
                        build_id=build,
                        platform=platform_value,
                        api_family=api,
                    )
                manifest["state"] = "bound"
                manifest["scope"] = desired_scope
                self._write_workspace(root, manifest)
            return self.status()

    def configure_asset_root(self, asset_root: str | Path | None) -> dict[str, Any]:
        before = self.status()
        root = Path(str(before["projectRoot"])).resolve()
        asset_text = ""
        if asset_root is not None and str(asset_root).strip():
            raw = Path(asset_root).expanduser()
            if not raw.is_absolute():
                raise ActiveProjectError("asset root must be an absolute path")
            if raw.exists() and raw.is_symlink():
                raise ActiveProjectError("asset root must not be a symlink")
            resolved = raw.resolve()
            if not resolved.is_dir() or _filesystem_root(resolved):
                raise ActiveProjectError("asset root must be a non-root directory")
            # An asset root may be a dedicated directory inside the project,
            # but it must not be a broad parent that also contains the project.
            if resolved == root or is_within(resolved, root):
                raise ActiveProjectError("asset root must not contain the project root")
            asset_text = str(resolved)
        with self.pin_active_project(before["workspaceId"], root):
            with _thread_lock(root), _exclusive_lock(
                self._project_lock_path(root), "active project configuration"
            ):
                manifest = self._read_workspace(root)
                manifest["assetRoot"] = asset_text
                self._write_workspace(root, manifest)
            return self.status()

    def resolve_asset_path(
        self, relative_path: str | Path, *, must_exist: bool = True
    ) -> Path:
        unused_root, manifest = self._active()
        asset_text = str(manifest.get("assetRoot", "") or "")
        if not asset_text:
            raise ActiveProjectError("asset root is not configured")
        asset_root = Path(asset_text)
        if asset_root.is_symlink() or not asset_root.is_dir():
            raise ActiveProjectError("configured asset root is unavailable")
        pure = PurePosixPath(str(relative_path).replace("\\", "/"))
        if pure.is_absolute() or not pure.parts or any(
            part in ("", ".", "..") for part in pure.parts
        ):
            raise ActiveProjectError("asset path must be a safe relative path")
        cursor = asset_root
        for component in pure.parts:
            cursor = cursor / component
            if cursor.exists() and cursor.is_symlink():
                raise ActiveProjectError("asset path traverses a symlink")
        result = cursor.resolve()
        if not is_within(asset_root, result):
            raise ActiveProjectError("asset path escapes the configured asset root")
        if must_exist and not result.exists():
            raise ActiveProjectError("asset path does not exist")
        return result

    def _replay_service_and_status_for(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> tuple[ReplayProjectService, dict[str, Any]]:
        if manifest["state"] != "bound":
            raise ActiveProjectError(
                "project game/build scope is not configured; physical knowledge reuse is blocked"
            )
        replay_root = self._managed_path(
            root, _LAYOUT["replayProject"], "replay project root"
        )
        service = ReplayProjectService(replay_root)
        replay_status = service.status()
        configured_scope = manifest["scope"]
        replay_scope = replay_status["scope"]
        if (
            replay_scope["gameId"] != configured_scope["gameId"]
            or replay_scope["buildId"] != configured_scope["buildId"]
        ):
            raise ActiveProjectError("active and replay project game/build scopes differ")
        # Platform/API may be intentionally left empty and pinned by the first
        # imported capture.  Accept that one-way refinement here so a crash
        # between ReplayProjectService.import_capture() and our scope snapshot
        # update remains recoverable.  A configured non-empty value must still
        # match exactly.
        for field in ("platform", "apiFamily"):
            configured = configured_scope[field]
            if configured and configured != replay_scope[field]:
                raise ActiveProjectError(
                    "active and replay project %s scopes differ" % field
                )
        return service, replay_status

    def _replay_service_for(
        self, root: Path, manifest: Mapping[str, Any]
    ) -> ReplayProjectService:
        service, unused_status = self._replay_service_and_status_for(root, manifest)
        return service

    def replay_service(self) -> ReplayProjectService:
        if self._bound_project_snapshot() is not None:
            root, manifest = self._active()
            return self._replay_service_for(root, manifest)
        with self._selection_guard():
            root, manifest = self._active()
        return self._replay_service_for(root, manifest)

    def capture_catalog_path(
        self, capture_id: str, catalog_sha256: str | None = None
    ) -> Path:
        root, unused_manifest = self._active()
        digest = _sha256(capture_id, "capture_id")
        base = self._managed_path(
            root, _LAYOUT["captureCatalogs"], "capture catalog directory"
        )
        if catalog_sha256 is None:
            # v1 projects stored one catalog directly below captureId. Keep
            # that location readable so existing projects remain portable.
            result = (base / digest / "catalog.json").resolve()
        else:
            revision = _sha256(catalog_sha256, "catalog_sha256")
            result = (
                base / digest / "revisions" / revision / "catalog.json"
            ).resolve()
        if not is_within(base, result):
            raise ActiveProjectError("capture catalog path escapes the project")
        return result

    def shader_workspace_path(
        self,
        *,
        capture_id: str,
        catalog_sha256: str | None = None,
        route_binding_sha256: str | None = None,
        stage: str,
        shader_resource_id: int,
        raw_sha256: str,
        create: bool = False,
    ) -> Path:
        root, unused_manifest = self._active()
        capture = _sha256(capture_id, "capture_id")
        raw = _sha256(raw_sha256, "raw_sha256")
        stage_key = re.sub(r"[^a-z0-9]+", "-", str(stage or "").lower()).strip("-")
        if not SAFE_COMPONENT_RE.fullmatch(stage_key):
            raise ActiveProjectError("stage is invalid")
        resource = _positive(shader_resource_id, "shader_resource_id")
        base = self._managed_path(
            root, _LAYOUT["shaderWorkspaces"], "shader workspace directory"
        )
        revision = (
            _sha256(catalog_sha256, "catalog_sha256")
            if catalog_sha256 is not None
            else ""
        )
        route_binding = (
            _sha256(route_binding_sha256, "route_binding_sha256")
            if route_binding_sha256 is not None
            else ""
        )
        # Keep every identity field in one collision-resistant binding while
        # avoiding a Windows path that concatenates four full SHA-256 values.
        # The previous readable layout reached 455 characters for a normal job
        # directory; CreateProcess and DXC then failed even though Python could
        # create the files.  Existing workspaces remain readable when callers
        # hold their explicit path, while all newly allocated managed paths use
        # this compact v2 layout.
        workspace_binding = hashlib.sha256(
            _canonical_bytes(
                {
                    "schema": "replaylab-shader-workspace-binding/v2",
                    "captureId": capture,
                    "catalogSha256": revision,
                    "routeBindingSha256": route_binding,
                    "stage": stage_key,
                    "shaderResourceId": resource,
                    "rawSha256": raw,
                }
            )
        ).hexdigest()
        result = (base / "v2" / workspace_binding).resolve()
        if not is_within(base, result):
            raise ActiveProjectError("shader workspace path escapes the project")
        if os.name == "nt":
            # Reserve space for the longest fixed route-job name and output.
            # Fail before creating a workspace when even the compact project
            # root cannot satisfy tools that still use the Win32 MAX_PATH ABI.
            path_probe = (
                result
                / "jobs"
                / ("decompile-h99-r9999-" + ("f" * 32))
                / "output.dxil.ll"
            )
            if len(str(path_probe)) >= 248:
                raise ActiveProjectError(
                    "active project root is too long for Windows Shader tool jobs"
                )
        if create:
            result.mkdir(parents=True, exist_ok=True)
            if result.is_symlink() or not result.is_dir():
                raise ActiveProjectError("shader workspace path is redirected")
        return result

    def shared_dictionary_directory(self, kind: str) -> Path:
        root, unused_manifest = self._active()
        component = str(kind or "").strip().lower()
        if component not in {
            "shaders",
            "aliases",
            "materials",
            "events",
            "assets",
            "explanatory",
        }:
            raise ActiveProjectError("unknown shared dictionary kind")
        base = self._managed_path(
            root, _LAYOUT["sharedDictionaries"], "shared dictionary directory"
        )
        result = (base / component).resolve()
        if not is_within(base, result):
            raise ActiveProjectError("shared dictionary path escapes the project")
        result.mkdir(parents=True, exist_ok=True)
        if result.is_symlink() or not result.is_dir():
            raise ActiveProjectError("shared dictionary path is redirected")
        return result

    @_pinned_active_operation
    def stage_capture_catalog(
        self,
        catalog_path: str | Path,
        *,
        expected_capture_id: str | None = None,
        expected_capture_path: str | None = None,
        expected_api: str | None = None,
    ) -> dict[str, Any]:
        source = Path(catalog_path).expanduser()
        if source.exists() and source.is_symlink():
            raise ActiveProjectError("capture catalog must not be a symlink")
        source = source.resolve()
        _regular_bytes(source, MAX_CATALOG_BYTES, "capture catalog")
        try:
            normalised_path, catalog = _normalise_catalog(source)
        except Exception as exc:
            raise ActiveProjectError(
                "capture catalog validation failed: %s" % exc
            ) from exc
        if normalised_path != source:
            raise ActiveProjectError("capture catalog path changed during validation")
        # These expectations come from the server-owned live context/export,
        # never from the MCP request. Reject a crossed capture before any
        # catalog or Shader bytes are published into the project.
        capture_id = _sha256(catalog.get("captureId"), "captureId")
        capture_path = _text(
            catalog.get("capturePath", ""),
            "capturePath",
            32768,
            allow_empty=expected_capture_path is None,
        )
        capture_api = _api_family(
            catalog.get("api", ""), allow_empty=expected_api is None
        )
        if expected_capture_id is not None:
            expected_id = _sha256(expected_capture_id, "expected_capture_id")
            if capture_id != expected_id:
                raise ActiveProjectError(
                    "capture catalog captureId differs from the expected exported identity"
                )
        if expected_capture_path is not None:
            expected_path = _text(
                expected_capture_path, "expected_capture_path", 32768
            )
            if os.path.normcase(
                str(Path(capture_path).expanduser().resolve())
            ) != os.path.normcase(
                str(Path(expected_path).expanduser().resolve())
            ):
                raise ActiveProjectError(
                    "capture catalog path differs from the active RenderDoc capture"
                )
        if expected_api is not None:
            expected_api_family = _api_family(expected_api, allow_empty=False)
            if capture_api != expected_api_family:
                raise ActiveProjectError(
                    "capture catalog API differs from the active RenderDoc capture"
                )
        total_blob_bytes = 0
        blobs: dict[str, bytes] = {}
        for shader in catalog["shaders"]:
            raw_path = str(shader.get("rawPath", "") or "")
            if not raw_path:
                continue
            raw = _regular_bytes(Path(raw_path), MAX_CATALOG_BYTES, "shader blob")
            total_blob_bytes += len(raw)
            if total_blob_bytes > MAX_STAGED_SHADER_BYTES:
                raise ActiveProjectError("staged shader blobs exceed the project limit")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != shader.get("rawSha256"):
                raise ActiveProjectError("shader blob changed after catalog validation")
            blobs.setdefault(digest, raw)
            shader["rawPath"] = "blobs/%s.bin" % digest
        payload = (
            json.dumps(
                catalog,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if not payload or len(payload) > MAX_CATALOG_BYTES:
            raise ActiveProjectError("normalised capture catalog exceeds the project limit")
        catalog_sha256 = hashlib.sha256(payload).hexdigest()
        legacy_destination = self.capture_catalog_path(capture_id)
        revision_destination = self.capture_catalog_path(
            capture_id, catalog_sha256
        )
        destination = revision_destination
        root, unused_manifest = self._active()

        def validate_existing_catalog(path: Path) -> None:
            existing = _regular_bytes(
                path, MAX_CATALOG_BYTES, "staged capture catalog"
            )
            if existing != payload:
                raise ActiveProjectError(
                    "catalog revision is already staged with different evidence"
                )
            for digest, blob in blobs.items():
                existing_blob = _regular_bytes(
                    path.parent / "blobs" / (digest + ".bin"),
                    MAX_CATALOG_BYTES,
                    "staged shader blob",
                )
                if (
                    hashlib.sha256(existing_blob).hexdigest() != digest
                    or existing_blob != blob
                ):
                    raise ActiveProjectError("staged shader blob hash mismatch")

        with _thread_lock(root), _exclusive_lock(
            self._project_lock_path(root), "active project configuration"
        ):
            # An existing v1 catalog is one immutable revision. Reuse it only
            # when its complete normalized bytes match; a different payload
            # with the same captureId is a legitimate catalog revision because
            # captureId intentionally covers transport + Shader identities,
            # not uses/reflection/diagnostic evidence.
            if legacy_destination.exists() or legacy_destination.is_symlink():
                legacy = _regular_bytes(
                    legacy_destination,
                    MAX_CATALOG_BYTES,
                    "legacy staged capture catalog",
                )
                if legacy == payload:
                    destination = legacy_destination
                    validate_existing_catalog(destination)
                    idempotent = True
                elif revision_destination.parent.exists():
                    destination = revision_destination
                    validate_existing_catalog(destination)
                    idempotent = True
                else:
                    idempotent = False
            elif revision_destination.parent.exists():
                validate_existing_catalog(revision_destination)
                idempotent = True
            else:
                idempotent = False

            if not idempotent:
                catalogs_root = legacy_destination.parent.parent
                revision_count, storage_bytes = _staged_catalog_usage(
                    catalogs_root
                )
                if revision_count >= MAX_STAGED_CATALOG_REVISIONS:
                    raise ActiveProjectError(
                        "active project capture catalog revision limit reached"
                    )
                incoming_bytes = len(payload) + sum(
                    len(blob) for blob in blobs.values()
                )
                if (
                    storage_bytes + incoming_bytes
                    > MAX_STAGED_CATALOG_STORAGE_BYTES
                ):
                    raise ActiveProjectError(
                        "active project capture catalog storage limit reached"
                    )
                capture_dir = revision_destination.parent
                revisions_root = capture_dir.parent
                revisions_root.mkdir(parents=True, exist_ok=True)
                if revisions_root.is_symlink() or not revisions_root.is_dir():
                    raise ActiveProjectError(
                        "capture catalog revisions directory is redirected"
                    )
                staging = Path(
                    tempfile.mkdtemp(
                        prefix=".%s.staging-" % catalog_sha256,
                        dir=str(revisions_root),
                    )
                )
                try:
                    (staging / "blobs").mkdir(parents=True)
                    for digest, blob in blobs.items():
                        atomic_write_bytes(staging / "blobs" / (digest + ".bin"), blob)
                    atomic_write_bytes(staging / "catalog.json", payload)
                    staging.replace(capture_dir)
                except Exception:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
                destination = revision_destination
        unused_root, current_manifest = self._active()
        return {
            "captureId": capture_id,
            "capturePath": capture_path,
            "apiFamily": capture_api,
            "catalogPath": str(destination),
            "catalogSha256": catalog_sha256,
            "catalogRevisionSha256": catalog_sha256,
            "shaderBlobCount": len(blobs),
            "idempotent": idempotent,
            "scopeConfigured": current_manifest["state"] == "bound",
            "physicalKnowledgeImportAllowed": current_manifest["state"] == "bound",
        }

    def _sync_scope_from_replay(self, root: Path) -> None:
        replay_root = self._managed_path(
            root, _LAYOUT["replayProject"], "replay project root"
        )
        replay_scope = ReplayProjectService(replay_root).status()["scope"]
        with _thread_lock(root), _exclusive_lock(
            self._project_lock_path(root), "active project configuration"
        ):
            manifest = self._read_workspace(root)
            if (
                manifest["scope"]["gameId"] != replay_scope["gameId"]
                or manifest["scope"]["buildId"] != replay_scope["buildId"]
            ):
                raise ActiveProjectError("replay project changed game/build scope")
            changed = False
            for field in ("platform", "apiFamily"):
                configured = manifest["scope"][field]
                detected = replay_scope[field]
                if configured and configured != detected:
                    raise ActiveProjectError("replay project changed %s scope" % field)
                if not configured and detected:
                    manifest["scope"][field] = detected
                    changed = True
            if changed:
                self._write_workspace(root, manifest)

    @_pinned_active_operation
    def import_capture(
        self,
        catalog_path: str | Path,
        *,
        expected_capture_id: str | None = None,
        expected_capture_path: str | None = None,
        expected_api: str | None = None,
    ) -> dict[str, Any]:
        # Resolve the service first so a pending project cannot be made to look
        # authoritative merely by staging a valid catalog.
        service = self.replay_service()
        staged = self.stage_capture_catalog(
            catalog_path,
            expected_capture_id=expected_capture_id,
            expected_capture_path=expected_capture_path,
            expected_api=expected_api,
        )
        plan = service.import_capture(staged["catalogPath"])
        root, unused_manifest = self._active()
        self._sync_scope_from_replay(root)
        return {
            "stagedCatalog": staged,
            "reusePlan": self._decorate_reuse_plan(plan),
            "physicalKnowledgeImported": True,
            "autoApplied": False,
        }

    @staticmethod
    def _read_flowchart(status: Mapping[str, Any]) -> tuple[bytes, str] | None:
        if status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ActiveProjectError(
                "workspace status must come from strict workspace validation"
            )
        workspace_text = str(status.get("workspacePath", "") or "")
        if not workspace_text:
            return None
        raw_workspace = Path(workspace_text).expanduser()
        if raw_workspace.exists() and raw_workspace.is_symlink():
            raise ActiveProjectError("workspace path must not be a symlink")
        workspace = raw_workspace.resolve()
        if not workspace.is_dir():
            return None
        source = workspace / "ai" / "flowchart.mmd"
        if not source.exists() and not source.is_symlink():
            return None
        if source.is_symlink() or not is_within(workspace, source.resolve()):
            raise ActiveProjectError("workspace flowchart escapes its workspace")
        raw = _regular_bytes(source, MAX_EXPLANATORY_BYTES, "workspace flowchart")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ActiveProjectError("workspace flowchart must be UTF-8 text") from exc
        if "\x00" in text:
            raise ActiveProjectError("workspace flowchart contains NUL characters")
        from .shader_workspace.explanations import source_binding_metadata
        provenance = source_binding_metadata(workspace, source, raw, status)
        if provenance["currentSourceMatches"] is False:
            raise ActiveProjectError(provenance["reason"])
        return raw, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _read_shader_flow_graph(
        status: Mapping[str, Any],
    ) -> tuple[bytes, str] | None:
        if status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ActiveProjectError(
                "workspace status must come from strict workspace validation"
            )
        workspace_text = str(status.get("workspacePath", "") or "")
        if not workspace_text:
            return None
        raw_workspace = Path(workspace_text).expanduser()
        if raw_workspace.exists() and raw_workspace.is_symlink():
            raise ActiveProjectError("workspace path must not be a symlink")
        workspace = raw_workspace.resolve()
        if not workspace.is_dir():
            return None
        artifact = workspace / "ai" / "flow-graph.json"
        if not artifact.exists() and not artifact.is_symlink():
            return None
        source = status.get("source")
        if not isinstance(source, Mapping) or source.get("present") is not True:
            raise ActiveProjectError(
                "workspace Shader flow graph has no current source binding"
            )
        declared_source = Path(str(source.get("path", "") or ""))
        source_path = declared_source.resolve()
        if (
            artifact.is_symlink()
            or not is_within(workspace, artifact.resolve())
            or declared_source.is_symlink()
            or not source_path.is_file()
            or not is_within(workspace, source_path)
        ):
            raise ActiveProjectError(
                "workspace Shader flow graph escapes its workspace"
            )
        try:
            # Validate and publish one exact byte string.  A second read would
            # permit an atomic replacement between validation and copying.
            raw = _regular_bytes(
                artifact, MAX_FLOW_GRAPH_BYTES,
                "workspace Shader flow graph"
            )
            value = json.loads(raw.decode("utf-8", errors="strict"))
            if not isinstance(value, Mapping):
                raise ShaderFlowGraphError(
                    "workspace Shader flow graph must contain one JSON object"
                )
            validate_shader_flow_graph(
                value,
                expected_workspace_id=str(status.get("workspaceId", "") or ""),
                expected_source_sha256=str(
                    source.get("currentSha256", "") or ""
                ),
                expected_source_revision=status.get("revision"),
                expected_source_path=str(
                    source_path.relative_to(workspace)
                ).replace("\\", "/"),
            )
            digest = hashlib.sha256(raw).hexdigest()

            projected = status.get("flowGraph")
            if (
                not isinstance(projected, Mapping)
                or projected.get("available") is not True
                or projected.get("status") != "current"
                or projected.get("stale") is not False
                or projected.get("graphId") != value.get("graphId")
                or projected.get("sourceSha256") != value.get("sourceSha256")
                or projected.get("sourceRevision") != value.get("sourceRevision")
                or projected.get("artifactSha256") != digest
                or Path(str(projected.get("artifactPath", "") or "")).resolve()
                != artifact.resolve()
            ):
                raise ShaderFlowGraphError(
                    "workspace status does not bind the current Shader flow graph"
                )

        except (UnicodeError, ValueError, TypeError,
                ShaderFlowGraphError) as exc:
            raise ActiveProjectError(
                "workspace Shader flow graph is invalid: %s" % exc
            ) from exc
        return raw, digest

    def _register_explanatory_artifact(
        self,
        root: Path,
        workspace_id: str,
        knowledge_id: str,
        prepared: tuple[bytes, str] | None,
        *,
        kind: str,
        suffix: str,
        label: str,
    ) -> list[dict[str, Any]]:
        if prepared is None:
            return []
        raw, digest = prepared
        relative = "%s/%s.%s" % (
            _LAYOUT["sharedExplanatory"],
            digest,
            suffix,
        )
        destination = self._managed_path(root, relative, label)
        policy = {
            "explanatoryOnly": True,
            "physicalEvidence": False,
            "applyAuthorization": False,
            "aiProvisionalAutoApply": False,
        }
        with _thread_lock(root), _exclusive_lock(
            self._project_lock_path(root), "active project configuration"
        ):
            index = self._read_shared_index(root, workspace_id)
            existing = [
                record
                for record in index["explanatory"]
                if record["knowledgeId"] == knowledge_id
                and record["artifactSha256"] == digest
                and record["kind"] == kind
            ]
            if existing:
                return existing
            if len(index["explanatory"]) >= MAX_EXPLANATORY_RECORDS:
                raise ActiveProjectError("shared explanatory artifact limit reached")
            if destination.exists():
                if hashlib.sha256(
                    _regular_bytes(
                        destination, MAX_EXPLANATORY_BYTES, label
                    )
                ).hexdigest() != digest:
                    raise ActiveProjectError(
                        "content-addressed %s changed" % label
                    )
            else:
                atomic_write_bytes(destination, raw)
            record = {
                "knowledgeId": knowledge_id,
                "kind": kind,
                "artifactPath": relative,
                "artifactSha256": digest,
                "registeredAt": _utc_now(),
                "policy": policy,
            }
            index["explanatory"].append(record)
            self._write_shared_index(root, index)
            return [record]

    def _register_flowchart(
        self,
        root: Path,
        workspace_id: str,
        knowledge_id: str,
        prepared: tuple[bytes, str] | None,
    ) -> list[dict[str, Any]]:
        return self._register_explanatory_artifact(
            root,
            workspace_id,
            knowledge_id,
            prepared,
            kind="mermaid-flowchart",
            suffix="flowchart.mmd",
            label="flowchart artifact",
        )

    def _register_shader_flow_graph(
        self,
        root: Path,
        workspace_id: str,
        knowledge_id: str,
        prepared: tuple[bytes, str] | None,
    ) -> list[dict[str, Any]]:
        return self._register_explanatory_artifact(
            root,
            workspace_id,
            knowledge_id,
            prepared,
            kind="shader-flow-graph",
            suffix="flow-graph.json",
            label="Shader flow graph artifact",
        )

    @_pinned_active_operation
    def register_workspace(
        self,
        status: Mapping[str, Any],
        *,
        include_explanatory: bool = True,
        require_current_candidate: bool = False,
        include_compiled_candidate: bool = True,
        expected_head: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(status, Mapping):
            raise ActiveProjectError("workspace status must be an object")
        # A later user edit deliberately changes the program. Its old analysis
        # and graph remain available on the original immutable project record,
        # but cannot describe or block publication of the newly applied source.
        explanatory_diagnostics: list[dict[str, str]] = []

        def prepare_optional(reader: Any, artifact: str) -> tuple[bytes, str] | None:
            if not include_explanatory:
                return None
            try:
                return reader(status)
            except (ActiveProjectError, OSError) as exc:
                # An old or malformed diagram cannot invalidate source or ABI
                # evidence. Keep it on disk and report why it was not copied;
                # never publish it against the new source revision.
                explanatory_diagnostics.append({
                    "artifact": artifact,
                    "status": "skipped",
                    "reason": str(exc)[:800],
                })
                return None

        prepared_flowchart = prepare_optional(self._read_flowchart, "ai/flowchart.mmd")
        prepared_flow_graph = prepare_optional(self._read_shader_flow_graph, "ai/flow-graph.json")
        service = self.replay_service()
        try:
            options: dict[str, Any] = {}
            options["include_compiled_candidate"] = include_compiled_candidate
            if expected_head is not None:
                options["expected_head"] = expected_head
            if not include_explanatory:
                options["include_analysis"] = False
            if require_current_candidate or not include_explanatory:
                options["require_current_candidate"] = True
            result = service.register_workspace(status, **options)
        except ReplayProjectError as exc:
            if isinstance(exc, ActiveProjectError):
                raise
            raise ActiveProjectError(str(exc)) from exc
        knowledge = result.get("knowledge")
        if not isinstance(knowledge, Mapping):
            raise ActiveProjectError("replay project returned invalid workspace knowledge")
        knowledge_id = str(knowledge.get("knowledgeId", ""))
        if not HEX32_RE.fullmatch(knowledge_id):
            raise ActiveProjectError("replay project returned invalid knowledgeId")
        root, manifest = self._active()
        flowcharts = self._register_flowchart(
            root,
            str(manifest["workspaceId"]),
            knowledge_id,
            prepared_flowchart,
        )
        flow_graphs = self._register_shader_flow_graph(
            root,
            str(manifest["workspaceId"]),
            knowledge_id,
            prepared_flow_graph,
        )
        value = dict(result)
        value["flowchartArtifacts"] = flowcharts
        value["flowGraphArtifacts"] = flow_graphs
        value["explanatoryDiagnostics"] = list(value.get("explanatoryDiagnostics") or []) + explanatory_diagnostics
        value["policy"] = {
            "flowchartExplanatoryOnly": True,
            "flowGraphNavigationOnly": True,
            "physicalEvidence": False,
            "applyAuthorization": False,
        }
        return value

    @_pinned_active_operation
    def get_workspace_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        """Round-trip one published workspace through the strict project reader.

        Registration receipts alone are not publication evidence: an adapter
        could acknowledge a write without the manifest or content-addressed
        artifacts being readable.  The underlying project reader revalidates
        the canonical knowledge identity and every referenced source/binary
        artifact before this method returns.
        """

        try:
            return self.replay_service().get_workspace_knowledge(knowledge_id)
        except ReplayProjectError as exc:
            if isinstance(exc, ActiveProjectError):
                raise
            raise ActiveProjectError(str(exc)) from exc

    @_pinned_active_operation
    def register_alias_table(self, alias_table_path: str | Path) -> dict[str, Any]:
        # ReplayProjectService accepts only verifiedOnly exports and rejects
        # AIProvisional rows, so no weaker active-project path exists.
        try:
            return self.replay_service().register_alias_table(alias_table_path)
        except ReplayProjectError as exc:
            if isinstance(exc, ActiveProjectError):
                raise
            raise ActiveProjectError(str(exc)) from exc

    @_pinned_active_operation
    def save_material_knowledge(
        self, session: Mapping[str, Any], parameter_ids: Sequence[str]
    ) -> dict[str, Any]:
        try:
            return self.replay_service().save_material_knowledge(
                session, parameter_ids
            )
        except ReplayProjectError as exc:
            if isinstance(exc, ActiveProjectError):
                raise
            raise ActiveProjectError(str(exc)) from exc

    @_pinned_active_operation
    def seed_workspace(
        self, knowledge_id: str, destination_status: Mapping[str, Any],
        *, supersede_local_edit: bool = False,
    ) -> dict[str, Any]:
        try:
            return self.replay_service().seed_workspace(
                knowledge_id, destination_status,
                supersede_local_edit=supersede_local_edit,
            )
        except ReplayProjectError as exc:
            if isinstance(exc, ActiveProjectError):
                raise
            raise ActiveProjectError(str(exc)) from exc

    def _explanatory_by_knowledge(
        self, kind: str
    ) -> dict[str, list[dict[str, Any]]]:
        root, manifest = self._active()
        index = self._read_shared_index(root, str(manifest["workspaceId"]))
        result: dict[str, list[dict[str, Any]]] = {}
        for record in index["explanatory"]:
            if record["kind"] != kind:
                continue
            artifact = self._managed_path(
                root, record["artifactPath"], "shared explanatory artifact"
            )
            result.setdefault(record["knowledgeId"], []).append(
                {
                    "kind": record["kind"],
                    "path": str(artifact),
                    "sha256": record["artifactSha256"],
                    **record["policy"],
                }
            )
        return result

    def _flowcharts_by_knowledge(self) -> dict[str, list[dict[str, Any]]]:
        return self._explanatory_by_knowledge("mermaid-flowchart")

    def _flow_graphs_by_knowledge(self) -> dict[str, list[dict[str, Any]]]:
        return self._explanatory_by_knowledge("shader-flow-graph")

    def _decorate_reuse_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        flowcharts = self._flowcharts_by_knowledge()
        flow_graphs = self._flow_graphs_by_knowledge()
        matches = []
        preparation_steps = []
        for original in list(plan.get("matches", []) or []):
            match = dict(original)
            workspace_candidates = []
            for source in list(match.get("workspaceCandidates", []) or []):
                candidate = dict(source)
                binary_eligible = bool(
                    candidate.get("binaryCandidateEligible") is True
                    and isinstance(candidate.get("binaryCandidate"), Mapping)
                )
                candidate["flowchartCandidates"] = flowcharts.get(
                    str(candidate.get("knowledgeId", "")), []
                )
                candidate["flowGraphCandidates"] = flow_graphs.get(
                    str(candidate.get("knowledgeId", "")), []
                )
                candidate["preparationOnly"] = True
                candidate["compileRequired"] = not binary_eligible
                candidate["freshCompileMayBeSkipped"] = binary_eligible
                candidate["compileFallbackRequiredOnBinaryRejection"] = (
                    binary_eligible
                )
                candidate["freshCaptureRebindRequired"] = True
                candidate["liveAbiRevalidationRequired"] = True
                candidate["autoApplyAuthorized"] = False
                workspace_candidates.append(candidate)
                if candidate.get("sourceSeedEligible") is True:
                    preparation_steps.append(
                        {
                            "kind": "seed-reviewed-hlsl",
                            "knowledgeId": candidate.get("knowledgeId"),
                            "shader": match.get("shader"),
                            "matchMode": candidate.get("matchMode"),
                            "automaticPreparationEligible": True,
                            "physicalMutation": False,
                            "requiresFreshCompileAndLiveAbiValidation": (
                                not binary_eligible
                            ),
                            "requiresFreshCompile": not binary_eligible,
                            "requiresFreshLiveAbiValidation": True,
                        }
                    )
                if binary_eligible:
                    binary = candidate["binaryCandidate"]
                    preparation_steps.append(
                        {
                            "kind": "load-verified-compiled-shader-candidate",
                            "knowledgeId": candidate.get("knowledgeId"),
                            "shader": match.get("shader"),
                            "matchMode": candidate.get("matchMode"),
                            "artifactPath": binary.get("artifactPath"),
                            "sha256": binary.get("sha256"),
                            "sourceSha256": binary.get("sourceSha256"),
                            "compilerBindingSha256": binary.get(
                                "compilerBindingSha256"
                            ),
                            "automaticPreparationEligible": True,
                            "physicalMutation": False,
                            "freshCompileMayBeSkipped": True,
                            "requiresFreshCaptureRebind": True,
                            "requiresFreshLiveAbiValidation": True,
                            "oldReplacementIdReused": False,
                            "oldCaptureAbiAuthorityReused": False,
                        }
                    )
            match["workspaceCandidates"] = workspace_candidates
            for alias in list(match.get("aliasCandidates", []) or []):
                preparation_steps.append(
                    {
                        "kind": "load-verified-alias-candidate",
                        "knowledgeId": alias.get("knowledgeId"),
                        "shader": match.get("shader"),
                        "matchMode": alias.get("matchMode"),
                        "automaticPreparationEligible": True,
                        "physicalMutation": False,
                        "requiresFreshBindingAndLayoutValidation": True,
                    }
                )
            for material in list(match.get("materialCandidates", []) or []):
                preparation_steps.append(
                    {
                        "kind": "list-material-value-candidate",
                        "knowledgeId": material.get("knowledgeId"),
                        "shader": match.get("shader"),
                        "matchMode": material.get("matchMode"),
                        "automaticPreparationEligible": True,
                        "physicalMutation": False,
                        "requiresNewLiveMaterialSession": True,
                    }
                )
            matches.append(match)
        decorated = dict(plan)
        decorated["matches"] = matches
        decorated["preparationSteps"] = preparation_steps
        decorated["activeProjectPolicy"] = {
            "scopeConfigured": True,
            "programMatchesMayPrepareCandidates": True,
            "preparedCandidateIsApplyAuthorization": False,
            "aiProvisionalMayPreparePhysicalCandidate": False,
            "aiProvisionalAutoApply": False,
            "physicalMutationAutoApply": False,
            "verifiedBinaryCandidateMaySkipCompile": True,
            "binaryCandidateRequiresFreshCaptureRebind": True,
            "binaryCandidateRequiresFreshLiveAbiRevalidation": True,
            "oldCompiledCandidateAbiAuthorityReused": False,
            "oldReplacementIdReused": False,
            "liveAbiRevalidationRequired": True,
            "liveMaterialLayoutRevalidationRequired": True,
        }
        return decorated

    @_pinned_active_operation
    def future_frame_plan(
        self, capture_id: str = "", catalog_sha256: str = ""
    ) -> dict[str, Any]:
        root, manifest = self._active()
        if manifest["state"] != "bound":
            return {
                "schema": FUTURE_FRAME_PLAN_SCHEMA,
                "schemaVersion": FUTURE_FRAME_PLAN_VERSION,
                "workspaceId": manifest["workspaceId"],
                "projectRoot": str(root),
                "scopeConfigured": False,
                "blocked": True,
                "blockedReason": (
                    "gameId/buildId must be explicitly configured before physical "
                    "cross-frame knowledge can be matched or prepared"
                ),
                "reusePlan": None,
                "preparationSteps": [],
                "policy": {
                    "crossGameAutomaticPhysicalReuse": False,
                    "aiProvisionalAutoApply": False,
                    "autoApplyAuthorized": False,
                },
            }
        digest = _sha256(capture_id, "capture_id")
        decorated = self._decorate_reuse_plan(
            self.replay_service().reuse_plan(digest, catalog_sha256)
        )
        return {
            "schema": FUTURE_FRAME_PLAN_SCHEMA,
            "schemaVersion": FUTURE_FRAME_PLAN_VERSION,
            "workspaceId": manifest["workspaceId"],
            "projectRoot": str(root),
            "scopeConfigured": True,
            "blocked": False,
            "blockedReason": "",
            "reusePlan": decorated,
            "preparationSteps": decorated["preparationSteps"],
            "policy": dict(decorated["activeProjectPolicy"]),
        }


__all__ = [
    "ACTIVE_SELECTION_SCHEMA",
    "ACTIVE_SELECTION_VERSION",
    "PROJECT_WORKSPACE_SCHEMA",
    "PROJECT_WORKSPACE_VERSION",
    "FUTURE_FRAME_PLAN_SCHEMA",
    "FUTURE_FRAME_PLAN_VERSION",
    "CAPTURE_LIBRARY_SCHEMA",
    "CAPTURE_LIBRARY_VERSION",
    "CAPTURE_CONTENT_IDENTITY_SCHEMA",
    "CAPTURE_CONTENT_IDENTITY_ALGORITHM",
    "CAPTURE_PROJECT_MATCH_SCHEMA",
    "PROJECT_REGISTRY_SCHEMA",
    "PROJECT_REGISTRY_VERSION",
    "ActiveProjectError",
    "ActiveReplayProjectService",
]
