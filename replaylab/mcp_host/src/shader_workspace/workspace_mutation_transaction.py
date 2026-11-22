"""Crash-recoverable transactions for organization draft mutations.

Organization rename/restructure operations update several review artifacts and,
for CBuffer identifiers, files in the shared alias store.  Atomic replacement
of each file is not sufficient: a process can stop between replacements.  This
module keeps the intent, exact before/after hashes, backups and staged bytes in
the Host-owned configuration tree.  It therefore remains trustworthy across a
Host restart while the AI-editable workspace only contains transaction targets.

Every workspace has one Host-owned mutation slot.  The slot serializes all
organizations which share ``ai/aliases.json`` and ``ai/organized.*``; the
ledger itself records the organization which owns a pending mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, ExitStack
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from .io import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_workspace_identity,
    read_json_object,
)


LEDGER_SCHEMA = "replaylab-shader-workspace-mutation/v1"
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_TARGET_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TARGETS = 1024
_LOCK_RETRY_SECONDS = 5.0
_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[str, tuple[Any, int]] = {}
_ATOMIC_TEMP_SUFFIX = re.compile(r"^[0-9a-f]{32}$")
_MAX_CLEANUP_ENTRIES = MAX_TARGETS * 4 + 32


class WorkspaceMutationRecoveryRequired(RuntimeError):
    """An external edit conflicts with a pending Host transaction."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_redirected(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        # An uninspectable path cannot be a safe transaction boundary.
        return True


def _path_identity(path: Path) -> str:
    return canonical_workspace_identity(path)


def _canonical_path(path: Path) -> Path:
    """Return one Path spelling for an already-authorized physical location."""

    return Path(_path_identity(Path(path)))


def _store_root(store_root: Path, workspace_root: Path) -> Path:
    store = Path(store_root).absolute()
    workspace = _canonical_path(workspace_root)
    resolved = _canonical_path(store)
    if _inside(workspace, resolved):
        raise ValueError("workspace mutation store must be outside the workspace")
    if any(_is_redirected(item) for item in (store, *store.parents)):
        raise ValueError("workspace mutation store must not use redirected paths")
    return resolved


def transaction_directory(
    store_root: Path, workspace_root: Path, organization_id: str
) -> Path:
    root = _canonical_path(workspace_root)
    # ``organization_id`` remains in the API because callers use it to validate
    # ledger ownership.  It deliberately does not select the active slot.
    unused_organization_id = organization_id
    suffix = hashlib.sha256(_path_identity(root).encode("utf-8")).hexdigest()
    return _store_root(store_root, root) / ("workspace-" + suffix)


@contextmanager
def _local_transaction_lock(workspace_root: Path, deadline: float):
    key = _path_identity(workspace_root)
    with _MUTATION_LOCKS_GUARD:
        entry = _MUTATION_LOCKS.get(key)
        if entry is None:
            lock = threading.Lock()
            references = 0
        else:
            lock, references = entry
        _MUTATION_LOCKS[key] = (lock, references + 1)
    acquired = False
    try:
        acquired = lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise RuntimeError(
                "another thread still owns this workspace mutation"
            )
        yield
    finally:
        if acquired:
            lock.release()
        with _MUTATION_LOCKS_GUARD:
            current = _MUTATION_LOCKS.get(key)
            if current is not None and current[0] is lock:
                if current[1] <= 1:
                    _MUTATION_LOCKS.pop(key, None)
                else:
                    _MUTATION_LOCKS[key] = (lock, current[1] - 1)


@contextmanager
def _transaction_lock(
    store_root: Path, workspace_root: Path, organization_id: str
):
    """One process/thread owns every mutation for a physical workspace."""

    store = _store_root(store_root, workspace_root)
    directory = transaction_directory(store, workspace_root, organization_id)
    lock_path = store / (directory.name + ".lock")
    deadline = time.monotonic() + _LOCK_RETRY_SECONDS
    with _local_transaction_lock(workspace_root, deadline):
        store.mkdir(parents=True, exist_ok=True)
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
                            "another process still owns this workspace mutation"
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


def _ledger_path(
    store_root: Path, workspace_root: Path, organization_id: str
) -> Path:
    return transaction_directory(store_root, workspace_root, organization_id) / "ledger.json"


def _read_ledger(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = read_json_object(path, MAX_LEDGER_BYTES, "workspace mutation ledger")
    if value.get("schema") != LEDGER_SCHEMA or value.get("schemaVersion") != 1:
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation ledger schema is invalid; manual recovery is required"
        )
    return value


def _write_ledger(path: Path, value: Mapping[str, Any]) -> None:
    payload = dict(value)
    payload["schema"] = LEDGER_SCHEMA
    payload["schemaVersion"] = 1
    atomic_write_json(path, payload)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validated_roots(
    workspace_root: Path, external_roots: Sequence[Path]
) -> list[Path]:
    roots: list[Path] = []
    for candidate in (workspace_root, *external_roots):
        lexical = Path(candidate).absolute()
        if any(_is_redirected(item) for item in (lexical, *lexical.parents)):
            raise ValueError("workspace mutation root must not use redirected paths")
        value = _canonical_path(lexical)
        identity = _path_identity(value)
        if all(_path_identity(item) != identity for item in roots):
            roots.append(value)
    return roots


def _ordered_external_roots(
    workspace_root: Path, external_roots: Sequence[Path]
) -> list[Path]:
    workspace = _canonical_path(workspace_root)
    return sorted(
        (
            root
            for root in _validated_roots(workspace, list(external_roots))
            if _path_identity(root) != _path_identity(workspace)
        ),
        key=_path_identity,
    )


def _validate_target(path: Path, roots: Sequence[Path]) -> tuple[Path, Path]:
    lexical = Path(path).absolute()
    if any(_is_redirected(item) for item in (lexical, *lexical.parents)):
        raise ValueError("workspace mutation target is redirected: %s" % lexical)
    resolved = _canonical_path(lexical)
    owner = next((root for root in roots if _inside(root, resolved)), None)
    if owner is None:
        raise ValueError("workspace mutation target is outside its allowed roots: %s" % lexical)
    # Reject a symlink/junction escape even when the final resolved spelling is
    # coincidentally below an allowed root.  Missing parents are created later.
    cursor = lexical.parent
    while _inside(owner, _canonical_path(cursor)) and _canonical_path(cursor) != owner:
        if _is_redirected(cursor):
            raise ValueError("workspace mutation target parent is redirected: %s" % cursor)
        cursor = cursor.parent
    if _canonical_path(cursor) != owner:
        raise ValueError("workspace mutation target parent escapes its allowed root: %s" % lexical)
    if lexical.exists() and not lexical.is_file():
        raise ValueError("workspace mutation target is not a regular file: %s" % lexical)
    return resolved, owner


def _read_target(path: Path, owner: Path | None = None) -> bytes | None:
    if owner is not None:
        path, checked_owner = _validate_target(path, [owner])
        if _path_identity(checked_owner) != _path_identity(owner):
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target changed physical owner: %s" % path
            )
    if not path.exists():
        if owner is not None:
            _validate_target(path, [owner])
        return None
    if _is_redirected(path) or not path.is_file():
        raise ValueError("workspace mutation target is not a regular file: %s" % path)
    before = path.stat()
    if before.st_size > MAX_TARGET_BYTES:
        raise ValueError("workspace mutation target exceeds its size limit: %s" % path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_TARGET_BYTES + 1)
    if len(raw) > MAX_TARGET_BYTES:
        raise ValueError("workspace mutation target exceeds its size limit: %s" % path)
    after = path.stat()
    if owner is not None:
        _validate_target(path, [owner])
    if (
        len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("workspace mutation target changed while it was read: %s" % path)
    return raw


def _state(path: Path, owner: Path | None = None) -> tuple[bool, str]:
    raw = _read_target(path, owner)
    return raw is not None, _sha256(raw) if raw is not None else ""


def _row_state(row: Mapping[str, Any], prefix: str) -> tuple[bool, str]:
    return bool(row.get(prefix + "Existed")), str(row.get(prefix + "Sha256") or "")


def _payload_path(directory: Path, row: Mapping[str, Any], key: str) -> Path:
    name = str(row.get(key) or "")
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation ledger contains an invalid payload path"
        )
    return directory / name


def _read_payload(
    directory: Path,
    row: Mapping[str, Any],
    *,
    kind: str,
    required: bool,
) -> bytes | None:
    path = _payload_path(directory, row, kind + "File")
    if not path.exists():
        if required:
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation %s payload is missing" % kind
            )
        return None
    raw = _read_target(path)
    expected = str(row.get(kind + "Sha256") or "")
    if raw is None or _sha256(raw) != expected:
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation %s payload changed" % kind
        )
    return raw


def _validate_ledger_binding(
    ledger: Mapping[str, Any], workspace_root: Path, organization_id: str
) -> tuple[Path, list[Path], list[Mapping[str, Any]]]:
    root = _canonical_path(workspace_root)
    if (
        _path_identity(Path(str(ledger.get("workspaceRoot") or "")))
        != _path_identity(root)
        or str(ledger.get("organizationId") or "") != str(organization_id)
    ):
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation ledger belongs to a different organization"
        )
    external = ledger.get("externalRoots")
    if not isinstance(external, list) or not all(isinstance(item, str) for item in external):
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation ledger external roots are invalid"
        )
    roots = _validated_roots(root, [Path(item) for item in external])
    rows = ledger.get("targets")
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) > MAX_TARGETS
        or not all(isinstance(item, Mapping) for item in rows)
    ):
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation ledger targets are invalid"
        )
    seen: set[str] = set()
    for row in rows:
        target, owner = _validate_target(Path(str(row.get("path") or "")), roots)
        spelling = os.path.normcase(str(target))
        if spelling in seen:
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation ledger contains duplicate targets"
            )
        seen.add(spelling)
        if _path_identity(target) != _path_identity(
            Path(str(row.get("path") or ""))
        ):
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target path is not canonical"
            )
        owner_text = str(row.get("ownerRoot") or "")
        if owner_text and _path_identity(Path(owner_text)) != _path_identity(owner):
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target owner is invalid"
            )
        for prefix in ("before", "after"):
            existed, digest = _row_state(row, prefix)
            if existed != bool(digest) or (digest and (len(digest) != 64 or any(
                    char not in "0123456789abcdef" for char in digest))):
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation target digest is invalid"
                )
    return root, roots, rows


def _pending_external_roots_locked(
    *, store_root: Path, workspace_root: Path, organization_id: str
) -> list[Path]:
    """Return exact external roots named by a pending Host-owned ledger."""

    ledger = _read_ledger(_ledger_path(store_root, workspace_root, organization_id))
    if ledger is None:
        return []
    unused_root, roots, unused_rows = _validate_ledger_binding(
        ledger, workspace_root, organization_id
    )
    workspace = _canonical_path(workspace_root)
    return _ordered_external_roots(
        workspace, [root for root in roots if _path_identity(root) != _path_identity(workspace)]
    )


def _remove_target(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _atomic_temp_matches(final_path: Path, candidate: Path) -> bool:
    prefix = ".%s.tmp-" % final_path.name
    name = candidate.name
    return (
        candidate.parent == final_path.parent
        and name.startswith(prefix)
        and _ATOMIC_TEMP_SUFFIX.fullmatch(name[len(prefix):]) is not None
    )


def _atomic_temp_final_name(candidate: Path) -> str | None:
    """Decode an atomic temp name without scanning once per possible target."""

    name = candidate.name
    marker = ".tmp-"
    if not name.startswith(".") or len(name) <= 1 + len(marker) + 32:
        return None
    suffix = name[-32:]
    head = name[:-32]
    if _ATOMIC_TEMP_SUFFIX.fullmatch(suffix) is None or not head.endswith(marker):
        return None
    final_name = head[1:-len(marker)]
    return final_name or None


def _bounded_directory_entries(directory: Path) -> list[Path]:
    """Read at most the cleanup limit plus one sentinel entry."""

    entries = list(islice(directory.iterdir(), _MAX_CLEANUP_ENTRIES + 1))
    if len(entries) > _MAX_CLEANUP_ENTRIES:
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation directory contains too many cleanup entries"
        )
    return entries


def _cleanup_preledger_directory(directory: Path) -> None:
    """Recover the mkdir -> first-ledger atomic-write crash window."""

    if _is_redirected(directory) or not directory.is_dir():
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation directory has no recoverable ledger"
        )
    ledger_path = directory / "ledger.json"
    try:
        entries = _bounded_directory_entries(directory)
    except WorkspaceMutationRecoveryRequired as exc:
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation directory has no recoverable ledger"
        ) from exc
    for entry in entries:
        if (
            not _atomic_temp_matches(ledger_path, entry)
            or _is_redirected(entry)
            or not entry.is_file()
        ):
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation directory has no recoverable ledger"
            )
    for entry in entries:
        entry.unlink()
    directory.rmdir()
    if directory.exists():
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation pre-ledger cleanup did not remove its directory"
        )


def _cleanup(directory: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if _is_redirected(directory) or not directory.is_dir():
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation directory is redirected during cleanup"
        )
    internal_paths: list[Path] = []
    for row in rows:
        for key in ("beforeFile", "afterFile"):
            path = _payload_path(directory, row, key)
            internal_paths.append(path)
            try:
                if _is_redirected(path) or (path.exists() and not path.is_file()):
                    raise WorkspaceMutationRecoveryRequired(
                        "workspace mutation payload is redirected during cleanup"
                    )
                path.unlink()
            except FileNotFoundError:
                pass
    ledger_path = directory / "ledger.json"
    allowed_temp_finals = {path.name for path in internal_paths}
    allowed_temp_finals.add(ledger_path.name)
    entries = _bounded_directory_entries(directory)
    for entry in entries:
        if entry == ledger_path:
            continue
        final_name = _atomic_temp_final_name(entry)
        if final_name not in allowed_temp_finals:
            continue
        if _is_redirected(entry) or not entry.is_file():
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation temporary is not a regular file: %s" % entry
            )
        entry.unlink()

    # Only the authenticated ledger may remain. Unknown contents are preserved
    # as evidence and keep a fail-closed ledger instead of becoming an orphan.
    remaining = _bounded_directory_entries(directory)
    if any(item != ledger_path for item in remaining):
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation directory contains an unknown cleanup entry"
        )
    if ledger_path.exists():
        if _is_redirected(ledger_path) or not ledger_path.is_file():
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation ledger is redirected during cleanup"
            )
        ledger_path.unlink()
    directory.rmdir()
    if directory.exists():
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation cleanup did not remove its directory"
        )


def _set_recovery_required(
    ledger_path: Path, ledger: dict[str, Any], message: str
) -> WorkspaceMutationRecoveryRequired:
    ledger["phase"] = "recovery-required"
    ledger["recoveryError"] = str(message)[:1200]
    _write_ledger(ledger_path, ledger)
    return WorkspaceMutationRecoveryRequired(
        "%s; workspace mutation requires explicit recovery" % message
    )


def _recognized_states(
    rows: Sequence[Mapping[str, Any]],
    roots: Sequence[Path],
    desired: str | None = None,
) -> list[tuple[Path, tuple[bool, str], tuple[bool, str], tuple[bool, str]]]:
    states = []
    for row in rows:
        path, owner = _validate_target(Path(str(row["path"])), roots)
        owner_text = str(row.get("ownerRoot") or "")
        if owner_text and _path_identity(Path(owner_text)) != _path_identity(owner):
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target changed physical owner: %s" % path
            )
        current = _state(path, owner)
        before = _row_state(row, "before")
        after = _row_state(row, "after")
        if current not in {before, after}:
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target changed outside the operation: %s" % path
            )
        if desired is not None and desired not in {"before", "after"}:
            raise ValueError("invalid recovery direction")
        states.append((path, current, before, after))
    return states


def _restore_direction(
    directory: Path,
    rows: Sequence[Mapping[str, Any]],
    roots: Sequence[Path],
    direction: str,
) -> None:
    states = _recognized_states(rows, roots, direction)
    for row, state in zip(rows, states):
        path, current, before, after = state
        path, owner = _validate_target(path, roots)
        wanted = before if direction == "before" else after
        if current == wanted:
            if _state(path, owner) != wanted:
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation target changed during recovery: %s" % path
                )
            continue
        if not wanted[0]:
            _remove_target(path)
            if _state(path, owner) != wanted:
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation target could not be removed: %s" % path
                )
            continue
        kind = "before" if direction == "before" else "after"
        payload = _read_payload(directory, row, kind=kind, required=True)
        assert payload is not None
        _validate_target(path, [owner])
        atomic_write_bytes(path, payload)
        if _state(path, owner) != wanted:
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target replacement was redirected: %s" % path
            )


def _recover_transaction_locked(
    *,
    store_root: Path,
    workspace_root: Path,
    organization_id: str,
    checkpoint_after_apply: Callable[[], None],
    checkpoint_after_rollback: Callable[[], None] | None = None,
) -> bool:
    """Recover one pending mutation.  Return whether a ledger was present."""

    directory = transaction_directory(store_root, workspace_root, organization_id)
    ledger_path = directory / "ledger.json"
    ledger = _read_ledger(ledger_path)
    if ledger is None:
        # The only instruction before the first ledger write is mkdir.  A
        # process stopped in that tiny window can leave only an empty directory.
        if directory.exists():
            _cleanup_preledger_directory(directory)
        return False
    unused_root, roots, rows = _validate_ledger_binding(
        ledger, workspace_root, organization_id
    )
    phase = str(ledger.get("phase") or "")
    if phase == "recovery-required":
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation requires explicit recovery: %s"
            % str(ledger.get("recoveryError") or "unresolved external change")
        )
    try:
        if phase == "preparing":
            # No live target is written before phase=applying.  Refuse cleanup
            # if an external writer changed a captured target in the meantime.
            states = _recognized_states(rows, roots)
            if any(current != before for unused, current, before, unused_after in states):
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation changed while its intent was prepared"
                )
            _cleanup(directory, rows)
            return True
        if phase in {"prepared", "applying", "checkpointing", "committed"}:
            _restore_direction(directory, rows, roots, "after")
            ledger["phase"] = "checkpointing"
            _write_ledger(ledger_path, ledger)
            checkpoint_after_apply()
            ledger["phase"] = "committed"
            _write_ledger(ledger_path, ledger)
            _cleanup(directory, rows)
            return True
        if phase == "rolling-back":
            _restore_direction(directory, rows, roots, "before")
            if checkpoint_after_rollback is not None:
                checkpoint_after_rollback()
            _cleanup(directory, rows)
            return True
        raise WorkspaceMutationRecoveryRequired(
            "workspace mutation ledger phase is invalid"
        )
    except WorkspaceMutationRecoveryRequired as exc:
        raise _set_recovery_required(ledger_path, ledger, str(exc)) from exc


def _apply_transaction_locked(
    *,
    store_root: Path,
    workspace_root: Path,
    organization_id: str,
    tool: str,
    operation_id: str,
    request_sha256: str,
    targets: Mapping[Path, bytes],
    checkpoint_after_apply: Callable[[], None],
    checkpoint_after_rollback: Callable[[], None] | None = None,
    external_roots: Sequence[Path] = (),
    expected_before: Mapping[Path, bytes | None] | None = None,
) -> None:
    """Commit exact target bytes or restore their exact prior states."""

    root = _canonical_path(workspace_root)
    roots = _validated_roots(root, list(external_roots))
    if not targets or len(targets) > MAX_TARGETS:
        raise ValueError("workspace mutation must contain 1-%d targets" % MAX_TARGETS)
    directory = transaction_directory(store_root, root, organization_id)
    ledger_path = directory / "ledger.json"
    if ledger_path.exists():
        raise WorkspaceMutationRecoveryRequired(
            "a previous workspace mutation must be recovered before another begins"
        )

    if directory.exists():
        _cleanup_preledger_directory(directory)

    prepared: list[tuple[Path, bytes | None, bytes, Path]] = []
    expected_by_path = {
        _path_identity(Path(path)): value
        for path, value in dict(expected_before or {}).items()
    }
    seen: set[str] = set()
    total = 0
    for path, after in targets.items():
        target, owner = _validate_target(Path(path), roots)
        spelling = _path_identity(target)
        if spelling in seen:
            raise ValueError("workspace mutation contains duplicate targets")
        seen.add(spelling)
        after_bytes = bytes(after)
        before = _read_target(target, owner)
        expected_key = _path_identity(target)
        if expected_key in expected_by_path:
            expected = expected_by_path[expected_key]
            if expected is not None:
                expected = bytes(expected)
            if before != expected:
                raise RuntimeError(
                    "workspace mutation target changed before its transaction: %s"
                    % target
                )
        total += len(after_bytes) + (len(before) if before is not None else 0)
        if len(after_bytes) > MAX_TARGET_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("workspace mutation payload exceeds its bounded size")
        prepared.append((target, before, after_bytes, owner))

    directory.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    for index, (path, before, after, owner) in enumerate(prepared):
        rows.append({
            "path": str(path),
            "ownerRoot": str(owner),
            "beforeExisted": before is not None,
            "beforeSha256": _sha256(before) if before is not None else "",
            "beforeFile": "before-%04d.bin" % index,
            "afterExisted": True,
            "afterSha256": _sha256(after),
            "afterFile": "after-%04d.bin" % index,
        })
    ledger: dict[str, Any] = {
        "schema": LEDGER_SCHEMA,
        "schemaVersion": 1,
        "workspaceRoot": str(root),
        "organizationId": str(organization_id),
        "tool": str(tool),
        "operationId": str(operation_id or ""),
        "requestSha256": str(request_sha256 or ""),
        "externalRoots": [str(item) for item in roots if item != root],
        "phase": "preparing",
        "targets": rows,
    }
    _write_ledger(ledger_path, ledger)
    try:
        for row, (unused_path, before, after, unused_owner) in zip(rows, prepared):
            if before is not None:
                atomic_write_bytes(_payload_path(directory, row, "beforeFile"), before)
            atomic_write_bytes(_payload_path(directory, row, "afterFile"), after)
        # Verify every durable payload before granting any live write.
        for row in rows:
            _read_payload(
                directory, row, kind="before", required=bool(row["beforeExisted"])
            )
            _read_payload(directory, row, kind="after", required=True)
        ledger["phase"] = "prepared"
        _write_ledger(ledger_path, ledger)
        states = _recognized_states(rows, roots)
        if any(current != before for unused, current, before, unused_after in states):
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation target changed after its snapshot"
            )
        ledger["phase"] = "applying"
        _write_ledger(ledger_path, ledger)
        for row, (path, unused_before, after, owner) in zip(rows, prepared):
            path, current_owner = _validate_target(path, roots)
            if _path_identity(current_owner) != _path_identity(owner):
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation target changed physical owner: %s" % path
                )
            current = _state(path, owner)
            before_state = _row_state(row, "before")
            if current != before_state:
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation target changed before replacement: %s" % path
                )
            _validate_target(path, [owner])
            atomic_write_bytes(path, after)
            if _state(path, owner) != _row_state(row, "after"):
                raise WorkspaceMutationRecoveryRequired(
                    "workspace mutation target replacement was redirected: %s" % path
                )
        ledger["phase"] = "checkpointing"
        _write_ledger(ledger_path, ledger)
        checkpoint_after_apply()
        ledger["phase"] = "committed"
        _write_ledger(ledger_path, ledger)
    except BaseException as exc:
        # A recognized ordinary failure is immediately rolled back.  Unknown
        # current bytes are an external edit: preserve them and fail closed.
        try:
            ledger["phase"] = "rolling-back"
            _write_ledger(ledger_path, ledger)
            _restore_direction(directory, rows, roots, "before")
            if checkpoint_after_rollback is not None:
                checkpoint_after_rollback()
            _cleanup(directory, rows)
        except BaseException as recovery_error:
            try:
                raise _set_recovery_required(
                    ledger_path, ledger, str(recovery_error)
                ) from recovery_error
            except WorkspaceMutationRecoveryRequired:
                if isinstance(exc, WorkspaceMutationRecoveryRequired):
                    raise
                raise
        raise

    # The checkpoint and committed ledger make the after-images authoritative.
    # Cleanup failure must never roll an already-committed operation back.
    try:
        _cleanup(directory, rows)
    except WorkspaceMutationRecoveryRequired as cleanup_error:
        try:
            raise _set_recovery_required(
                ledger_path, ledger, "committed cleanup failed: %s" % cleanup_error
            ) from cleanup_error
        except WorkspaceMutationRecoveryRequired:
            raise
    except Exception as cleanup_error:
        # Keep the committed phase retryable for transient ACL/sharing failures.
        # The after-images and checkpoint are already authoritative.
        ledger["cleanupError"] = str(cleanup_error)[:1200]
        _write_ledger(ledger_path, ledger)
        raise WorkspaceMutationRecoveryRequired(
            "committed cleanup was deferred and will be retried"
        ) from cleanup_error


def pending_external_roots(
    *, store_root: Path, workspace_root: Path, organization_id: str
) -> list[Path]:
    with _transaction_lock(store_root, workspace_root, organization_id):
        return _pending_external_roots_locked(
            store_root=store_root,
            workspace_root=workspace_root,
            organization_id=organization_id,
        )


def pending_transaction_owner(
    *, store_root: Path, workspace_root: Path
) -> str | None:
    """Return the validated organization owning this workspace's active slot."""

    with _transaction_lock(store_root, workspace_root, ""):
        directory = transaction_directory(store_root, workspace_root, "")
        ledger = _read_ledger(directory / "ledger.json")
        if ledger is None:
            if directory.exists():
                _cleanup_preledger_directory(directory)
            return None
        owner = str(ledger.get("organizationId") or "")
        if not owner or len(owner) > 256:
            raise WorkspaceMutationRecoveryRequired(
                "workspace mutation ledger organization is invalid"
            )
        _validate_ledger_binding(ledger, workspace_root, owner)
        return owner


def recover_transaction(
    *,
    store_root: Path,
    workspace_root: Path,
    organization_id: str,
    checkpoint_after_apply: Callable[[], None],
    checkpoint_after_rollback: Callable[[], None] | None = None,
) -> bool:
    with _transaction_lock(store_root, workspace_root, organization_id):
        external_roots = _pending_external_roots_locked(
            store_root=store_root,
            workspace_root=workspace_root,
            organization_id=organization_id,
        )
        from . import aliases

        with ExitStack() as locks:
            for external_root in external_roots:
                locks.enter_context(aliases.alias_store_transaction(external_root))
            return _recover_transaction_locked(
                store_root=store_root,
                workspace_root=workspace_root,
                organization_id=organization_id,
                checkpoint_after_apply=checkpoint_after_apply,
                checkpoint_after_rollback=checkpoint_after_rollback,
            )


def apply_transaction(
    *,
    store_root: Path,
    workspace_root: Path,
    organization_id: str,
    tool: str,
    operation_id: str,
    request_sha256: str,
    targets: Mapping[Path, bytes],
    checkpoint_after_apply: Callable[[], None],
    checkpoint_after_rollback: Callable[[], None] | None = None,
    external_roots: Sequence[Path] = (),
    expected_before: Mapping[Path, bytes | None] | None = None,
) -> None:
    with _transaction_lock(store_root, workspace_root, organization_id):
        from . import aliases

        with ExitStack() as locks:
            for external_root in _ordered_external_roots(
                _canonical_path(workspace_root), list(external_roots)
            ):
                locks.enter_context(aliases.alias_store_transaction(external_root))
            _apply_transaction_locked(
                store_root=store_root,
                workspace_root=workspace_root,
                organization_id=organization_id,
                tool=tool,
                operation_id=operation_id,
                request_sha256=request_sha256,
                targets=targets,
                checkpoint_after_apply=checkpoint_after_apply,
                checkpoint_after_rollback=checkpoint_after_rollback,
                external_roots=external_roots,
                expected_before=expected_before,
            )


__all__ = [
    "WorkspaceMutationRecoveryRequired",
    "apply_transaction",
    "pending_external_roots",
    "pending_transaction_owner",
    "recover_transaction",
    "transaction_directory",
]
