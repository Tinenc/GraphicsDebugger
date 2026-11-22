"""Crash-recoverable multi-layout publication of authorized CBuffer aliases.

Alias files are individually atomically replaced, but one material can name
several constant-buffer layouts.  This module stages every resulting file,
records exact before/after digests, and only then replaces the live files under
the shared alias-store lock.  A failed apply rolls every recognized target back.
After a Host restart, the same caller operation id either replays its committed
receipt or recovers an interrupted apply before trying the identical request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any

from . import aliases, alias_field_storage, alias_field_gc, alias_history
from .io import (
    atomic_write_bytes,
    canonical_workspace_identity,
    read_json_object,
)


LEDGER_SCHEMA = "replaylab-cbuffer-alias-publication-ledger/v1"
MAX_LEDGER_BYTES = 4 * 1024 * 1024
# Older Hosts checked compact JSON but staged indented JSON. Such failed stage
# files are not live publications; permit bounded cleanup of that known format
# so the same authenticated operation can retry after the storage fix.
MAX_LEGACY_STAGE_BYTES = aliases.MAX_FILE_BYTES * 4
_LEGACY_OPERATION_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,127}$")
_STAGE_DIRECTORY = re.compile(r"^stage-[0-9a-f]{32}$")
_ATOMIC_TEMP_SUFFIX = re.compile(r"^[0-9a-f]{32}$")
_BACKUP_FILE = re.compile(r"^backup-([0-9]{3})-([0-9a-f]{64})\.json$")
_MAX_CLEANUP_ENTRIES = aliases.MAX_ENTRIES + 8
_KNOWN_PHASES = frozenset({
    "preparing",
    "prepared",
    "applying",
    "failed-rolled-back",
    "interrupted-rolled-back",
    "committed",
    "recovery-required",
})
_LOCK_RETRY_SECONDS = 5.0
_OPERATION_LOCKS_GUARD = threading.Lock()
_OPERATION_LOCKS: dict[str, tuple[Any, int]] = {}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path_identity(path: Path | str) -> str:
    return canonical_workspace_identity(path)


def _caller_request_hashes(
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str],
) -> frozenset[str]:
    primary = str(caller_request_sha256 or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", primary) is None:
        raise ValueError("caller_request_sha256 must be a SHA-256 digest")
    compatible = tuple(compatible_caller_request_sha256 or ())
    if len(compatible) > 8:
        raise ValueError("compatible caller request has too many digests")
    accepted = {primary}
    for value in compatible:
        candidate = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", candidate) is None:
            raise ValueError("compatible caller request digest is not a SHA-256")
        accepted.add(candidate)
    return frozenset(accepted)


def _same_physical_path(left: Path | str, right: Path | str) -> bool:
    if _path_identity(left) == _path_identity(right):
        return True
    try:
        return os.path.samefile(str(left), str(right))
    except (OSError, ValueError):
        return False


def publication_paths(workspace_root: Path, operation_id: str) -> tuple[Path, Path]:
    root = Path(workspace_root) / "ai" / "cbuffer-alias-publications"
    operation = str(operation_id)
    # Pre-hash releases wrote the caller ID directly. Continue an existing
    # ledger only when its old, portable path is already present; new ledgers
    # always use a fixed hexadecimal component. This keeps old committed and
    # interrupted operations recoverable while making documented IDs containing
    # ':' (and Windows reserved/trailing-dot spellings) safe and collision-free.
    legacy = root / operation
    legacy_ledger = legacy / "ledger.json"
    try:
        use_legacy = bool(
            _LEGACY_OPERATION_DIRECTORY.fullmatch(operation)
            and not legacy.is_symlink()
            and not legacy_ledger.is_symlink()
            and legacy_ledger.is_file()
        )
    except OSError:
        use_legacy = False
    suffix = hashlib.sha256(operation.encode("utf-8")).hexdigest()[:32]
    directory = legacy if use_legacy else root / ("operation-" + suffix)
    return directory, directory / "ledger.json"


def read_ledger(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = read_json_object(path, MAX_LEDGER_BYTES, "CBuffer alias publication ledger")
    if value.get("schema") != LEDGER_SCHEMA or value.get("schemaVersion") != 1:
        raise ValueError("CBuffer alias publication ledger schema is invalid")
    return value


def write_ledger(path: Path, value: Mapping[str, Any]) -> None:
    payload = dict(value)
    payload["schema"] = LEDGER_SCHEMA
    payload["schemaVersion"] = 1
    serialized = alias_field_storage.json_bytes(payload)
    if len(serialized) > MAX_LEDGER_BYTES:
        raise ValueError("CBuffer alias publication ledger exceeds its bounded size")
    atomic_write_bytes(path, serialized)


@contextmanager
def _local_operation_lock(transaction_dir: Path, deadline: float):
    """Bound one operation in-process without serializing unrelated operations."""

    key = _path_identity(transaction_dir)
    with _OPERATION_LOCKS_GUARD:
        entry = _OPERATION_LOCKS.get(key)
        if entry is None:
            lock = threading.Lock()
            references = 0
        else:
            lock, references = entry
        _OPERATION_LOCKS[key] = (lock, references + 1)
    acquired = False
    try:
        acquired = lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise RuntimeError(
                "another thread still owns this CBuffer alias operation"
            )
        yield
    finally:
        if acquired:
            lock.release()
        with _OPERATION_LOCKS_GUARD:
            current = _OPERATION_LOCKS.get(key)
            if current is not None and current[0] is lock:
                if current[1] <= 1:
                    _OPERATION_LOCKS.pop(key, None)
                else:
                    _OPERATION_LOCKS[key] = (lock, current[1] - 1)


@contextmanager
def _publication_transaction_lock(transaction_dir: Path):
    """Serialize one durable operation across live publish and fast replay."""

    deadline = time.monotonic() + _LOCK_RETRY_SECONDS
    with _local_operation_lock(transaction_dir, deadline):
        transaction_dir.mkdir(parents=True, exist_ok=True)
        if _is_redirected(transaction_dir) or not transaction_dir.is_dir():
            raise RuntimeError("CBuffer alias transaction directory is redirected")
        lock_path = transaction_dir / ".operation.lock"
        if lock_path.exists() and (
            _is_redirected(lock_path) or not lock_path.is_file()
        ):
            raise RuntimeError("CBuffer alias operation lock is redirected")
        stream = lock_path.open("a+b")
        locked = False
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "another process still owns this CBuffer alias operation"
                        )
                    time.sleep(0.02)
            yield
        finally:
            if locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()


def _read_file(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if _is_redirected(path) or not path.is_file():
        raise ValueError("CBuffer alias target is not a regular file: %s" % path)
    before = path.stat()
    if before.st_size > aliases.MAX_LEGACY_FILE_BYTES:
        raise ValueError("CBuffer alias target exceeds its size limit: %s" % path)
    with path.open("rb") as stream:
        raw = stream.read(aliases.MAX_LEGACY_FILE_BYTES + 1)
    if len(raw) > aliases.MAX_LEGACY_FILE_BYTES:
        raise ValueError("CBuffer alias target exceeds its size limit: %s" % path)
    if len(raw) > aliases.MAX_FILE_BYTES:
        aliases._validate_legacy_payload_bytes(raw)
    after = path.stat()
    if (
        len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("CBuffer alias target changed while it was read: %s" % path)
    return raw


def _target_state(path: Path) -> tuple[bool, str]:
    raw = _read_file(path)
    return (raw is not None, _sha256(raw) if raw is not None else "")


def _validated_targets(
    publications: Sequence[Mapping[str, Any]], alias_root: Path
) -> list[dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for publication in publications:
        members = publication.get("members")
        if not isinstance(members, list):
            raise ValueError("CBuffer publication members must be a list")
        key = aliases.layout_sha256(str(publication.get("block") or ""), members)
        if not key:
            raise ValueError("CBuffer publication layout is incomplete")
        targets.setdefault(
            key,
            {
                "layoutSha256": key,
                "path": str(aliases.layout_alias_path(key, alias_root)),
            },
        )
    return [targets[key] for key in sorted(targets)]


def _ledger_matches(
    ledger: Mapping[str, Any],
    *,
    operation_id: str,
    request_sha256: str,
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str],
    workspace_root: Path,
    alias_root: Path,
    targets: Sequence[Mapping[str, Any]],
) -> bool:
    accepted_caller_hashes = _caller_request_hashes(
        caller_request_sha256, compatible_caller_request_sha256
    )
    expected_layouts = [str(item.get("layoutSha256") or "") for item in targets]
    actual_targets = ledger.get("targets")
    actual_layouts = ledger.get("requestedLayoutSha256") or (
        [str(item.get("layoutSha256") or "") for item in actual_targets]
        if isinstance(actual_targets, list)
        and all(isinstance(item, Mapping) for item in actual_targets)
        else []
    )
    return (
        ledger.get("operationId") == operation_id
        and ledger.get("requestSha256") == request_sha256
        and (
            not ledger.get("callerRequestSha256")
            or ledger.get("callerRequestSha256") in accepted_caller_hashes
        )
        and (
            not ledger.get("workspaceRoot")
            or _same_physical_path(
                str(ledger.get("workspaceRoot") or ""), workspace_root
            )
        )
        and _same_physical_path(str(ledger.get("aliasRoot") or ""), alias_root)
        and actual_layouts == expected_layouts
    )


def replay_committed_receipt(
    *,
    workspace_root: Path,
    operation_id: str,
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str] = (),
) -> dict[str, Any] | None:
    root = Path(workspace_root).resolve()
    transaction_dir, ledger_path = publication_paths(root, operation_id)
    if not ledger_path.exists():
        return None
    with _publication_transaction_lock(transaction_dir):
        return _replay_committed_receipt_locked(
            workspace_root=root,
            operation_id=operation_id,
            caller_request_sha256=caller_request_sha256,
            compatible_caller_request_sha256=compatible_caller_request_sha256,
        )


def _replay_committed_receipt_locked(
    *,
    workspace_root: Path,
    operation_id: str,
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Replay a committed exact receipt before live authorization is rebuilt.

    Only ledgers written with the canonical caller request binding are eligible.
    Older ledgers remain recoverable through the fully authorized transaction
    path, but cannot bypass its fresh validation.
    """

    root = Path(workspace_root).resolve()
    transaction_dir, ledger_path = publication_paths(root, operation_id)
    ledger = read_ledger(ledger_path)
    if ledger is None:
        return None
    if ledger.get("operationId") != operation_id:
        raise RuntimeError(
            "CBuffer alias operation id is bound to a different caller request"
        )
    if not ledger.get("callerRequestSha256") or not ledger.get("workspaceRoot"):
        # A pre-upgrade receipt has no caller-level binding. It can still be
        # recovered by the ordinary live-authorized path, never by this bypass.
        return None
    accepted_caller_hashes = _caller_request_hashes(
        caller_request_sha256, compatible_caller_request_sha256
    )
    if (
        ledger.get("callerRequestSha256") not in accepted_caller_hashes
        or not _same_physical_path(
            str(ledger.get("workspaceRoot") or ""), root
        )
    ):
        raise RuntimeError(
            "CBuffer alias operation id is bound to a different caller request"
        )
    phase = str(ledger.get("phase") or "")
    if phase not in _KNOWN_PHASES:
        raise RuntimeError(
            "CBuffer alias operation ledger has an unrecognized phase; "
            "manual recovery is required"
        )
    if phase != "committed":
        return None
    result = ledger.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("committed CBuffer alias receipt is missing")
    if ledger.get("fieldPageProtection") is True:
        with aliases.alias_store_transactions([Path(str(ledger["aliasRoot"]))]):
            _cleanup_committed_or_raise(ledger_path, transaction_dir, ledger)
    else:
        _cleanup_committed_or_raise(ledger_path, transaction_dir, ledger)
    replay = dict(result)
    replay["idempotentReplay"] = True
    return replay


def _restore_interrupted_apply(
    ledger: dict[str, Any], *, transaction_dir: Path, alias_root: Path
) -> None:
    target_rows = ledger.get("targets")
    if not isinstance(target_rows, list):
        raise RuntimeError("CBuffer alias recovery ledger has no targets")
    recovery: list[tuple[Path, bytes | None]] = []
    for index, row in enumerate(target_rows):
        if not isinstance(row, Mapping):
            raise RuntimeError("CBuffer alias recovery target is invalid")
        key = str(row.get("layoutSha256") or "")
        path = aliases.layout_alias_path(key, alias_root)
        existed = row.get("originalExisted") is True
        original_sha = str(row.get("originalSha256") or "")
        staged_sha = str(row.get("stagedSha256") or "")
        current_exists, current_sha = _target_state(path)
        recognized = (
            (not current_exists and not existed)
            or (current_exists and current_sha in {original_sha, staged_sha})
        )
        if not recognized:
            raise RuntimeError(
                "CBuffer alias target changed outside the interrupted operation; "
                "automatic recovery refuses to overwrite it: %s" % path
            )
        original: bytes | None = None
        if existed:
            backup = transaction_dir / ("backup-%03d-%s.json" % (index, key))
            original = _read_file(backup)
            if original is None or _sha256(original) != original_sha:
                raise RuntimeError("CBuffer alias recovery backup is missing or changed")
        recovery.append((path, original))

    for path, original in recovery:
        if original is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_bytes(path, original)


def _is_redirected(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        return True


def _bounded_entries(directory: Path) -> list[Path]:
    entries = list(islice(directory.iterdir(), _MAX_CLEANUP_ENTRIES + 1))
    if len(entries) > _MAX_CLEANUP_ENTRIES:
        raise RuntimeError("CBuffer alias staging contains too many cleanup entries")
    return entries


def _atomic_temp_final_name(path: Path) -> str | None:
    name = path.name
    marker = ".tmp-"
    if not name.startswith(".") or len(name) <= 1 + len(marker) + 32:
        return None
    suffix = name[-32:]
    head = name[:-32]
    if _ATOMIC_TEMP_SUFFIX.fullmatch(suffix) is None or not head.endswith(marker):
        return None
    final_name = head[1:-len(marker)]
    return final_name or None


def _validate_cleanup_file(path: Path, label: str, max_bytes: int) -> None:
    if _is_redirected(path) or not path.is_file():
        raise RuntimeError("%s is not a regular file" % label)
    try:
        if path.stat().st_size > max_bytes:
            raise RuntimeError("%s exceeds its size limit" % label)
    except OSError as exc:
        raise RuntimeError("%s cannot be inspected" % label) from exc


def _directory_identity(path: Path, label: str) -> tuple[int, int]:
    if _is_redirected(path) or not path.is_dir():
        raise RuntimeError("%s is redirected or invalid" % label)
    try:
        info = os.stat(str(path), follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("%s cannot be inspected" % label) from exc
    return int(info.st_dev), int(info.st_ino)


def _assert_directory_identity(
    path: Path, expected: tuple[int, int], label: str
) -> None:
    if _directory_identity(path, label) != expected:
        raise RuntimeError("%s changed during cleanup" % label)


def _unlink_cleanup_file(
    path: Path,
    *,
    ancestors: Sequence[tuple[Path, tuple[int, int], str]],
    label: str,
    max_bytes: int,
) -> None:
    # Two checks around the file check catch a directory replacement performed
    # after enumeration but before deletion. The postcondition prevents a
    # redirected replacement from being silently accepted for the next item.
    for ancestor, identity, ancestor_label in ancestors:
        _assert_directory_identity(ancestor, identity, ancestor_label)
    _validate_cleanup_file(path, label, max_bytes)
    for ancestor, identity, ancestor_label in ancestors:
        _assert_directory_identity(ancestor, identity, ancestor_label)
    path.unlink()
    for ancestor, identity, ancestor_label in ancestors:
        _assert_directory_identity(ancestor, identity, ancestor_label)


def _legacy_stage_cleanup_plan(
    stage_root: Path,
    *,
    transaction_dir: Path,
    target_keys: Sequence[str],
) -> tuple[list[Path], Path, tuple[int, int]]:
    """Authenticate a pre-intent stage created by an older Host release."""

    stage_root = Path(stage_root).absolute()
    transaction_dir = Path(transaction_dir).absolute()
    if (
        _STAGE_DIRECTORY.fullmatch(stage_root.name) is None
        or not _same_physical_path(stage_root.parent, transaction_dir)
    ):
        raise RuntimeError("CBuffer alias legacy stage is redirected or invalid")
    _directory_identity(transaction_dir, "CBuffer alias transaction directory")
    stage_identity = _directory_identity(stage_root, "CBuffer alias legacy stage")
    allowed_files = {str(key) + ".json" for key in target_keys}
    cleanup: list[Path] = []
    for path in _bounded_entries(stage_root):
        final_name = path.name
        if final_name == ".alias-publication.lock":
            _validate_cleanup_file(
                path, "CBuffer alias legacy stage lock", aliases.MAX_FILE_BYTES
            )
            cleanup.append(path)
            continue
        if final_name not in allowed_files:
            final_name = _atomic_temp_final_name(path) or ""
        if final_name not in allowed_files:
            raise RuntimeError(
                "CBuffer alias legacy stage contains an unknown cleanup entry"
            )
        _validate_cleanup_file(
            path, "CBuffer alias legacy stage file", MAX_LEGACY_STAGE_BYTES
        )
        cleanup.append(path)
    return cleanup, stage_root, stage_identity


def _cleanup_preledger_staging(
    transaction_dir: Path, target_keys: Sequence[str]
) -> None:
    """Recover both current ledger temps and strictly proved legacy staging."""

    entries = _bounded_entries(transaction_dir)
    transaction_identity = _directory_identity(
        transaction_dir, "CBuffer alias transaction directory"
    )
    target_keys = tuple(str(key) for key in target_keys)
    key_by_index = {index: key for index, key in enumerate(target_keys)}
    files: list[Path] = []
    stages: list[tuple[list[Path], Path, tuple[int, int]]] = []
    for path in entries:
        if path.name == ".operation.lock":
            continue
        if _atomic_temp_final_name(path) == "ledger.json":
            _validate_cleanup_file(
                path, "CBuffer alias pre-ledger temporary", MAX_LEDGER_BYTES
            )
            files.append(path)
            continue
        if _STAGE_DIRECTORY.fullmatch(path.name) is not None:
            stages.append(
                _legacy_stage_cleanup_plan(
                    path,
                    transaction_dir=transaction_dir,
                    target_keys=target_keys,
                )
            )
            continue
        final_name = _atomic_temp_final_name(path) or path.name
        backup = _BACKUP_FILE.fullmatch(final_name)
        if backup is not None:
            index = int(backup.group(1))
            key = backup.group(2)
            if key_by_index.get(index) != key:
                raise RuntimeError(
                    "CBuffer alias legacy backup does not match this request"
                )
            _validate_cleanup_file(
                path, "CBuffer alias legacy backup", aliases.MAX_LEGACY_FILE_BYTES
            )
            files.append(path)
            continue
        raise RuntimeError(
            "CBuffer alias transaction has no ledger and contains unknown data"
        )
    # Validate the complete bounded tree before removing any evidence. All of
    # these files predate the first durable intent, so no live alias was written.
    for path in files:
        _unlink_cleanup_file(
            path,
            ancestors=[(
                transaction_dir,
                transaction_identity,
                "CBuffer alias transaction directory",
            )],
            label="CBuffer alias legacy transaction file",
            max_bytes=max(MAX_LEDGER_BYTES, aliases.MAX_LEGACY_FILE_BYTES),
        )
    for stage_files, stage_root, stage_identity in stages:
        for path in stage_files:
            _unlink_cleanup_file(
                path,
                ancestors=[
                    (
                        transaction_dir,
                        transaction_identity,
                        "CBuffer alias transaction directory",
                    ),
                    (stage_root, stage_identity, "CBuffer alias legacy stage"),
                ],
                label="CBuffer alias legacy stage file",
                max_bytes=MAX_LEGACY_STAGE_BYTES,
            )
        _assert_directory_identity(
            transaction_dir,
            transaction_identity,
            "CBuffer alias transaction directory",
        )
        _assert_directory_identity(
            stage_root, stage_identity, "CBuffer alias legacy stage"
        )
        stage_root.rmdir()
        _assert_directory_identity(
            transaction_dir,
            transaction_identity,
            "CBuffer alias transaction directory",
        )
        if stage_root.exists():
            raise RuntimeError("CBuffer alias legacy stage cleanup was incomplete")


def _cleanup_committed_staging(
    transaction_dir: Path,
    ledger: Mapping[str, Any],
) -> None:
    """Remove only ledger-authenticated staging files; preserve unknown evidence."""

    transaction_dir = Path(transaction_dir).absolute()
    transaction_identity = _directory_identity(
        transaction_dir, "CBuffer alias transaction directory"
    )
    stage_text = str(ledger.get("stageRoot") or "")
    stage_root = Path(stage_text).absolute()
    if (
        not stage_text
        or _STAGE_DIRECTORY.fullmatch(stage_root.name) is None
        or not _same_physical_path(stage_root.parent, transaction_dir)
    ):
        raise RuntimeError("CBuffer alias committed stage path is invalid")
    targets = ledger.get("targets")
    if not isinstance(targets, list) or not all(
        isinstance(item, Mapping) for item in targets
    ):
        raise RuntimeError("CBuffer alias committed cleanup targets are invalid")

    backup_paths: list[Path] = []
    target_keys: list[str] = []
    for index, target in enumerate(targets):
        key = str(target.get("layoutSha256") or "")
        # layout_alias_path validates the exact lowercase SHA-256 spelling.
        aliases.layout_alias_path(key, stage_root)
        target_keys.append(key)
        backup_paths.append(
            transaction_dir / ("backup-%03d-%s.json" % (index, key))
        )

    transaction_entries = _bounded_entries(transaction_dir)
    stage_plans: list[tuple[list[Path], Path, tuple[int, int]]] = []
    for path in transaction_entries:
        if _STAGE_DIRECTORY.fullmatch(path.name) is None:
            continue
        stage_plans.append(
            _legacy_stage_cleanup_plan(
                path,
                transaction_dir=transaction_dir,
                target_keys=target_keys,
            )
        )
    if stage_root.exists() and not any(
        candidate.name == stage_root.name for unused, candidate, unused_id in stage_plans
    ):
        raise RuntimeError("CBuffer alias committed stage is not a valid directory")

    allowed_transaction = {
        "ledger.json",
        ".operation.lock",
        stage_root.name,
        *(path.name for path in backup_paths),
        *(path.name for unused, path, unused_id in stage_plans),
    }
    allowed_transaction_temps = {
        "ledger.json",
        *(path.name for path in backup_paths),
    }
    transaction_temporaries: list[Path] = []
    unknown_transaction: list[Path] = []
    for path in transaction_entries:
        if path.name in allowed_transaction:
            continue
        final_name = _atomic_temp_final_name(path)
        if final_name in allowed_transaction_temps:
            _validate_cleanup_file(
                path,
                "CBuffer alias transaction temporary",
                MAX_LEDGER_BYTES,
            )
            transaction_temporaries.append(path)
            continue
        unknown_transaction.append(path)
    if unknown_transaction:
        raise RuntimeError(
            "CBuffer alias transaction contains an unknown cleanup entry"
        )

    for path in (*transaction_temporaries, *backup_paths):
        if not path.exists():
            continue
        _unlink_cleanup_file(
            path,
            ancestors=[(
                transaction_dir,
                transaction_identity,
                "CBuffer alias transaction directory",
            )],
            label="CBuffer alias transaction cleanup file",
            max_bytes=max(MAX_LEDGER_BYTES, aliases.MAX_LEGACY_FILE_BYTES),
        )
    for stage_files, candidate, stage_identity in stage_plans:
        for path in stage_files:
            _unlink_cleanup_file(
                path,
                ancestors=[
                    (
                        transaction_dir,
                        transaction_identity,
                        "CBuffer alias transaction directory",
                    ),
                    (candidate, stage_identity, "CBuffer alias stage"),
                ],
                label="CBuffer alias stage cleanup file",
                max_bytes=MAX_LEGACY_STAGE_BYTES,
            )
        _assert_directory_identity(
            transaction_dir,
            transaction_identity,
            "CBuffer alias transaction directory",
        )
        _assert_directory_identity(candidate, stage_identity, "CBuffer alias stage")
        candidate.rmdir()
        _assert_directory_identity(
            transaction_dir,
            transaction_identity,
            "CBuffer alias transaction directory",
        )
        if candidate.exists():
            raise RuntimeError("CBuffer alias stage cleanup was incomplete")


def _cleanup_committed_or_raise(
    ledger_path: Path,
    transaction_dir: Path,
    ledger: dict[str, Any],
) -> None:
    try:
        _cleanup_committed_staging(transaction_dir, ledger)
        _release_field_pin(transaction_dir, ledger)
    except Exception as exc:
        ledger["cleanupError"] = str(exc)[:800]
        write_ledger(ledger_path, ledger)
        raise RuntimeError(
            "committed CBuffer alias cleanup is incomplete and will be retried"
        ) from exc
    if ledger.pop("cleanupError", None) is not None:
        write_ledger(ledger_path, ledger)


def _release_field_pin(transaction_dir: Path, ledger: Mapping[str, Any]) -> None:
    # Caller holds the alias-root lock. Old ledgers have no pins; their pages
    # are covered by the one-time migration snapshot instead.
    if ledger.get("fieldPageProtection") is True:
        alias_field_gc.release_transaction_pin(
            Path(str(ledger["aliasRoot"])), transaction_dir=transaction_dir,
            operation_id=str(ledger["operationId"]), request_sha256=str(ledger["requestSha256"]))


def _field_maintenance(root: Path) -> dict[str, Any]:
    try:
        return {"complete": True, **alias_field_gc.collect_unreferenced_pages(root)}
    except Exception as exc:
        # A cache cleanup issue cannot invalidate the names already committed.
        return {"complete": False, "deferred": True, "diagnostic": str(exc)[:400]}


def publish_transaction(
    *,
    workspace_root: Path,
    operation_id: str,
    request_sha256: str,
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str] = (),
    publications: Sequence[Mapping[str, Any]],
    build_result: Callable[[list[dict[str, Any]]], Mapping[str, Any]],
    before_apply: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Apply all authorized alias publications once and retain the exact receipt."""

    root = Path(workspace_root).resolve()
    transaction_dir, unused_ledger_path = publication_paths(root, operation_id)
    with _publication_transaction_lock(transaction_dir):
        return _publish_transaction_locked(
            workspace_root=root,
            operation_id=operation_id,
            request_sha256=request_sha256,
            caller_request_sha256=caller_request_sha256,
            compatible_caller_request_sha256=compatible_caller_request_sha256,
            publications=publications,
            build_result=build_result,
            before_apply=before_apply,
        )


def _publish_transaction_locked(
    *,
    workspace_root: Path,
    operation_id: str,
    request_sha256: str,
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str] = (),
    publications: Sequence[Mapping[str, Any]],
    build_result: Callable[[list[dict[str, Any]]], Mapping[str, Any]],
    before_apply: Callable[[], None] | None = None,
) -> dict[str, Any]:
    workspace_root = Path(workspace_root).resolve()
    selected_root, project_root, user_root = aliases._store_selection()
    alias_root = selected_root.resolve()
    store_kind = "project" if project_root is not None else "user"
    transaction_dir, ledger_path = publication_paths(workspace_root, operation_id)
    targets = _validated_targets(publications, alias_root)
    lock_roots = [alias_root, user_root] if project_root is not None else [alias_root]
    with aliases.alias_store_transactions(lock_roots):
        # Preserve legacy per-user aliases before the transaction snapshots the
        # selected project store. Adoption is a copy, not the requested effect.
        for target in targets:
            aliases._adopt_user_file_at(
                str(target["layoutSha256"]), alias_root
            )

        ledger = read_ledger(ledger_path)
        if ledger is None:
            _cleanup_preledger_staging(
                transaction_dir,
                [str(target["layoutSha256"]) for target in targets],
            )
        else:
            if not _ledger_matches(
                ledger,
                operation_id=operation_id,
                request_sha256=request_sha256,
                caller_request_sha256=caller_request_sha256,
                compatible_caller_request_sha256=compatible_caller_request_sha256,
                workspace_root=workspace_root,
                alias_root=alias_root,
                targets=targets,
            ):
                raise RuntimeError(
                    "CBuffer alias operation id is bound to different inputs or store"
                )
            phase = str(ledger.get("phase") or "")
            if phase not in _KNOWN_PHASES:
                raise RuntimeError(
                    "CBuffer alias operation ledger has an unrecognized phase; "
                    "manual recovery is required"
                )
            if phase == "committed":
                result = ledger.get("result")
                if not isinstance(result, Mapping):
                    raise RuntimeError("committed CBuffer alias receipt is missing")
                if not ledger.get("callerRequestSha256"):
                    ledger["callerRequestSha256"] = caller_request_sha256
                    ledger["workspaceRoot"] = str(workspace_root)
                    write_ledger(ledger_path, ledger)
                _cleanup_committed_or_raise(ledger_path, transaction_dir, ledger)
                replay = dict(result)
                replay["idempotentReplay"] = True
                return replay
            if phase == "recovery-required":
                raise RuntimeError(
                    "CBuffer alias operation requires manual recovery before retry"
                )
            if phase == "applying":
                try:
                    _restore_interrupted_apply(
                        ledger, transaction_dir=transaction_dir, alias_root=alias_root
                    )
                except Exception as exc:
                    ledger["phase"] = "recovery-required"
                    ledger["recoveryError"] = str(exc)[:800]
                    write_ledger(ledger_path, ledger)
                    raise
                ledger["phase"] = "interrupted-rolled-back"
                write_ledger(ledger_path, ledger)
            if ledger.get("phase") in {
                "preparing",
                "prepared",
                "failed-rolled-back",
                "interrupted-rolled-back",
            }:
                _cleanup_committed_staging(transaction_dir, ledger)
                _release_field_pin(transaction_dir, ledger)

        requested_layouts = [str(item["layoutSha256"]) for item in targets]
        history_diagnostics: list[dict[str, Any]] = []
        try:
            history_plans = alias_history.discover_history_publications(
                alias_root, list(publications), diagnostics=history_diagnostics)
        except Exception as exc:
            # An unrelated damaged history file must not block current naming.
            history_plans = []
            history_diagnostics.append({"reason": str(exc)[:400]})
        targets = _validated_targets([*publications, *history_plans], alias_root)
        # Initialize before creating any new pages. Existing pre-upgrade pages
        # may belong to an interrupted legacy transaction with no root pin.
        alias_field_gc.ensure_epoch(alias_root)
        transaction_dir.mkdir(parents=True, exist_ok=True)
        stage_root = transaction_dir / ("stage-" + uuid.uuid4().hex)
        prepared_targets: list[dict[str, Any]] = []
        for target in targets:
            key = str(target["layoutSha256"])
            live_path = aliases.layout_alias_path(key, alias_root)
            original = _read_file(live_path)
            original_sha = _sha256(original) if original is not None else ""
            prepared_targets.append({
                "layoutSha256": key,
                "path": str(live_path),
                "originalExisted": original is not None,
                "originalSha256": original_sha,
                "stagedSha256": "",
            })

        # Persist the exact stage/key intent before creating any staging path or
        # backup. A kill from this point onward can clean only ledger-bound files.
        ledger = {
            "schema": LEDGER_SCHEMA,
            "schemaVersion": 1,
            "operationId": operation_id,
            "requestSha256": request_sha256,
            "callerRequestSha256": caller_request_sha256,
            "workspaceRoot": str(workspace_root),
            "aliasRoot": str(alias_root),
            "stageRoot": str(stage_root),
            "targets": prepared_targets,
            "requestedLayoutSha256": requested_layouts,
            "fieldPageProtection": True,
            "phase": "preparing",
            "result": None,
        }
        write_ledger(ledger_path, ledger)
        stage_root.mkdir(parents=True, exist_ok=False)
        for index, target in enumerate(prepared_targets):
            key = str(target["layoutSha256"])
            live_path = Path(str(target["path"]))
            original = _read_file(live_path)
            current = (
                original is not None,
                _sha256(original) if original is not None else "",
            )
            expected = (
                bool(target["originalExisted"]),
                str(target["originalSha256"]),
            )
            if current != expected:
                raise RuntimeError(
                    "CBuffer alias target changed while staging its transaction: %s"
                    % live_path
                )
            if original is not None:
                backup = transaction_dir / ("backup-%03d-%s.json" % (index, key))
                atomic_write_bytes(backup, original)
                atomic_write_bytes(
                    aliases.layout_alias_path(key, stage_root), original
                )

        staged_results: list[dict[str, Any]] = []
        for publication in publications:
            result = aliases._publish_aliases_unlocked(
                str(publication.get("block") or ""),
                list(publication.get("members") or []),
                list(publication.get("entries") or []),
                "ai",
                block_alias=(
                    str(publication.get("blockAlias") or "")
                    if publication.get("blockAlias") is not None
                    else None
                ),
                shader_scope=(
                    publication.get("shaderScope")
                    if isinstance(publication.get("shaderScope"), Mapping)
                    else None
                ),
                semantic_fields=publication.get("semanticFields"),
                semantic_source_sha256=str(publication.get("semanticSourceSha256") or ""),
                semantic_captured_shader_sha256=str(publication.get("semanticCapturedShaderSha256") or ""),
                semantic_retention=publication.get("semanticRetention"),
                semantic_usage_review=publication.get("semanticUsageReview"),
                root=stage_root,
                # Immutable content-addressed pages are prepared in the final
                # store. Only the atomically committed layout manifests expose
                # them; rollback never removes another view's shared pages.
                semantic_fields_root=alias_root,
                store_kind=store_kind,
            )
            key = str(result.get("layoutSha256") or "")
            result["path"] = str(aliases.layout_alias_path(key, alias_root))
            result["store"] = store_kind
            staged_results.append(result)

        history_results = [alias_history.prune_history_file(
            aliases.layout_alias_path(str(plan["layoutSha256"]), stage_root), plan)
            for plan in history_plans]

        for target in prepared_targets:
            key = str(target["layoutSha256"])
            staged = _read_file(aliases.layout_alias_path(key, stage_root))
            if staged is None:
                # A fully rejected publication legitimately has no new file.
                target["stagedSha256"] = target["originalSha256"]
            else:
                target["stagedSha256"] = _sha256(staged)

        result = dict(build_result(staged_results) or {})
        cleanup_rows = [row.get("semanticHistoryCleanup") or {} for row in [*staged_results, *history_results]]
        result["semanticHistoryCleanup"] = {
            key: sum(int(row.get(key) or 0) for row in cleanup_rows)
            for key in ("removedViews", "removedAliases", "removedBlockAliases")}
        result["semanticHistoryCleanup"]["historicalLayoutsUpdated"] = len(history_results)
        if history_diagnostics:
            result["semanticHistoryCleanup"].update(
                deferred=True, deferredLayoutCount=len(history_diagnostics),
                diagnostics=history_diagnostics[:8])
        ledger["phase"] = "prepared"
        ledger["result"] = result
        write_ledger(ledger_path, ledger)

        has_file_effect = any(
            bool(target["originalExisted"])
            != bool(target["stagedSha256"])
            or str(target["originalSha256"]) != str(target["stagedSha256"])
            for target in prepared_targets
        )
        if before_apply is not None and has_file_effect:
            before_apply()
        # Refuse to replace an external user edit that landed after the exact
        # snapshot, even though that writer does not participate in our lock.
        for target in prepared_targets:
            current = _target_state(Path(str(target["path"])))
            expected = (
                bool(target["originalExisted"]), str(target["originalSha256"])
            )
            if current != expected:
                raise RuntimeError(
                    "CBuffer alias target changed after transaction snapshot: %s"
                    % target["path"]
                )

        pin_paths = [path for index, target in enumerate(prepared_targets)
                     for path in (transaction_dir / ("backup-%03d-%s.json" % (index, target["layoutSha256"])),
                                  aliases.layout_alias_path(str(target["layoutSha256"]), stage_root))
                     if path.exists()]
        alias_field_gc.pin_transaction(
            alias_root, transaction_dir=transaction_dir, operation_id=operation_id,
            request_sha256=request_sha256, manifest_paths=pin_paths)
        ledger["phase"] = "applying"
        write_ledger(ledger_path, ledger)
        try:
            for target in prepared_targets:
                key = str(target["layoutSha256"])
                staged_path = aliases.layout_alias_path(key, stage_root)
                staged = _read_file(staged_path)
                if staged is None:
                    continue
                live_path = Path(str(target["path"]))
                current = _target_state(live_path)
                expected = (
                    bool(target["originalExisted"]), str(target["originalSha256"])
                )
                if current != expected:
                    raise RuntimeError(
                        "CBuffer alias target changed before its atomic replace: %s"
                        % live_path
                    )
                atomic_write_bytes(live_path, staged)
            ledger["phase"] = "committed"
            ledger["result"] = result
            write_ledger(ledger_path, ledger)
        except Exception as exc:
            try:
                _restore_interrupted_apply(
                    ledger, transaction_dir=transaction_dir, alias_root=alias_root
                )
                ledger["phase"] = "failed-rolled-back"
                ledger["error"] = str(exc)[:800]
                write_ledger(ledger_path, ledger)
            except Exception as recovery_error:
                ledger["phase"] = "recovery-required"
                ledger["error"] = str(exc)[:800]
                ledger["recoveryError"] = str(recovery_error)[:800]
                write_ledger(ledger_path, ledger)
                raise RuntimeError(
                    "CBuffer alias publication failed and rollback was incomplete: %s"
                    % recovery_error
                ) from recovery_error
            try:
                _cleanup_committed_staging(transaction_dir, ledger)
                _release_field_pin(transaction_dir, ledger)
            except Exception as cleanup_error:
                # Original manifests are already restored and that fact is
                # durable. A busy backup/pin is retriable maintenance, not an
                # incomplete rollback requiring unavailable manual recovery.
                ledger["cleanupError"] = str(cleanup_error)[:800]
                try:
                    write_ledger(ledger_path, ledger)
                except (OSError, ValueError):
                    pass
            raise
        _cleanup_committed_or_raise(ledger_path, transaction_dir, ledger)
        result["semanticFieldCacheCleanup"] = _field_maintenance(alias_root)
        ledger["result"] = result
        try:
            write_ledger(ledger_path, ledger)
        except (OSError, ValueError):
            # The already durable receipt still proves the committed naming;
            # the optional maintenance counters do not authorize an extra write.
            pass
        return result
