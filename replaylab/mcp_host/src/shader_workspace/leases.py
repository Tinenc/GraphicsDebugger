"""Shared, non-exclusive participation markers for Shader source transactions.

There is no owner of a workspace source. The editor, the AI organizer, a
project restore and a toolchain refresh may all write ``source/shader.hlsl``;
what keeps them honest is not a lock but the compare-and-swap every writer
already does against the source hash it last read (a stale write is refused
with "source changed on disk; reload before saving", never by an exclusion),
plus the manifest transaction lock that serialises compiles.

``WorkspaceSourceTransactionLease`` keeps its name and shape for the callers
that grew around it, but ``acquire`` never fails: it records this participant
under ``watch/participants/`` so a reviewer can see who is touching the
workspace, and ``release`` removes that record. The historical exclusive
``.workspace-watch.lock`` is archived when found so an old crash can no longer
freeze a workspace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid
from datetime import datetime, timezone

SOURCE_TRANSACTION_JOURNAL_FILE = ".workspace-source-transaction.json"
SOURCE_TRANSACTION_BACKUP_FILE = ".workspace-source-transaction.backup"
LEASE_SCHEMA = "replaylab-workspace-source-participant/v3"
LEASE_VERSION = 3
MAX_LEASE_BYTES = 4096
PARTICIPANTS_DIR = "participants"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


class WorkspaceSourceLeaseError(RuntimeError):
    """Kept for callers that still name it; the shared marker never raises it."""


class WorkspaceSourceTransactionLease:
    """A presence record, not an exclusion.

    ``owned`` is True between ``acquire`` and ``release`` for the caller's own
    bookkeeping (the watcher gates its editor-save path on it). Two leases on
    the same workspace coexist; concurrent writers are reconciled by the
    hash-checked source swap, not here.
    """

    FILE_NAME = ".workspace-watch.lock"

    def __init__(
        self,
        root: Path,
        *,
        owner_instance_id: str | None = None,
        recover_orphaned: bool = False,
        recover_legacy: bool = False,
    ):
        self.root = root
        self.path = root / self.FILE_NAME
        self.token = uuid.uuid4().hex
        instance = str(owner_instance_id or "").strip().lower()
        if instance and _TOKEN.fullmatch(instance) is None:
            raise WorkspaceSourceLeaseError("source lease owner instance ID is invalid")
        self.owner_instance_id = instance
        # Accepted and ignored: nothing is exclusive, so nothing is orphaned.
        self.recover_orphaned = bool(recover_orphaned)
        self.recover_legacy = bool(recover_legacy)
        self._owned = False
        self._payload = self._lease_payload()

    @property
    def owned(self) -> bool:
        return self._owned

    @property
    def participant_path(self) -> Path:
        return self.root / "watch" / PARTICIPANTS_DIR / (self.token + ".json")

    def acquire(self) -> None:
        """Record this participant. Never blocks, never refuses."""

        self._archive_legacy_exclusive_lock()
        try:
            self.participant_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(self.participant_path, flags, 0o600)
            try:
                os.write(descriptor, self._payload)
            finally:
                os.close(descriptor)
        except OSError:
            # A participant record is informational; a full disk or a read-only
            # mirror must not stop anyone from editing their own Shader.
            pass
        self._owned = True

    def _lease_payload(self) -> bytes:
        value = {
            "schema": LEASE_SCHEMA,
            "schemaVersion": LEASE_VERSION,
            "token": self.token,
            "ownerInstanceId": self.owner_instance_id,
            "ownerPid": os.getpid(),
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _archive_legacy_exclusive_lock(self) -> bool:
        """Move an old exclusive ``.workspace-watch.lock`` out of the way.

        Older builds refused every writer while this file existed, including
        after a crash. It carries no information a participant record does not,
        so it is kept for the record under ``watch/`` and never honoured.
        """

        try:
            if self.path.is_symlink() or not self.path.is_file():
                return False
            if self.path.stat(follow_symlinks=False).st_size > MAX_LEASE_BYTES:
                return False
            watch_dir = self.root / "watch"
            if watch_dir.exists() and (watch_dir.is_symlink() or not watch_dir.is_dir()):
                return False
            watch_dir.mkdir(parents=False, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive = watch_dir / ("retired-exclusive-lease-%s-%s.lock" % (stamp, self.token[:8]))
            if archive.exists() or archive.is_symlink():
                return False
            self.path.replace(archive)
            return True
        except OSError:
            return False

    def release(self) -> bool:
        """Drop this participant's record."""

        if not self._owned:
            return False
        removed = False
        try:
            path = self.participant_path
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed = True
        except OSError:
            pass
        finally:
            self._owned = False
        return removed

    def __enter__(self) -> "WorkspaceSourceTransactionLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.release()
        return False


def participants(root: Path) -> list[dict]:
    """Who currently holds a participant record on this workspace (best effort)."""

    directory = Path(root) / "watch" / PARTICIPANTS_DIR
    found: list[dict] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return found
    for entry in entries[:64]:
        try:
            if entry.is_symlink() or not entry.is_file() or entry.stat().st_size > MAX_LEASE_BYTES:
                continue
            value = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("schema") == LEASE_SCHEMA:
            found.append({key: value.get(key) for key in ("token", "ownerInstanceId", "ownerPid", "createdAt")})
    return found


__all__ = [
    "PARTICIPANTS_DIR",
    "SOURCE_TRANSACTION_BACKUP_FILE",
    "SOURCE_TRANSACTION_JOURNAL_FILE",
    "WorkspaceSourceLeaseError",
    "WorkspaceSourceTransactionLease",
    "participants",
]
