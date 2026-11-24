"""Host-owned receipts for finalized shader sources, independent of a process.

The model may edit workspace proposal files, but no tool exposes this store.
Only a physically validated finalization inserts a receipt. Its digest identifies
the exact source record; it is not a claim that AI finished a semantic task.
Current source, graph, candidate and replay ABI are still checked by the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _default_path() -> Path:
    configured = os.environ.get("REPLAYLAB_CONFIG_DIR", "").strip()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    root = (Path(configured).expanduser() if configured else
            Path(local) / "ReplayLab" / "config" if local else
            Path.home() / ".config" / "ReplayLab")
    return root / "shader-acceptance" / "completed-reviews.sqlite3"


class CompletedShaderReviews:
    """Remember accepted snapshots outside every AI-editable workspace tree."""

    def __init__(self, path: Path | None = None) -> None:
        # Configuration belongs to the process owner, never an MCP argument.
        self.path = Path(path) if path is not None else _default_path()

    @staticmethod
    def _identity(session: Mapping[str, Any]) -> tuple[str, str, str]:
        raw = json.dumps(dict(session), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return (hashlib.sha256(raw).hexdigest(),
                str(Path(str(session["workspacePath"])).resolve()),
                str(session["organizationId"]))

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        path = self.path.absolute()
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError("shader acceptance store must not use redirected paths")
        if not create and not path.is_file():
            return None
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=ro"),
            uri=True, timeout=5.0,
        )
        if create:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS accepted_reviews ("
                "digest TEXT PRIMARY KEY, workspace TEXT NOT NULL, "
                "organization TEXT NOT NULL, decision TEXT NOT NULL, "
                "accepted_at TEXT NOT NULL)"
            )
        return connection

    def remember(self, session: Mapping[str, Any]) -> None:
        if (session.get("state") != "completed" or
                session.get("decision") != "accepted-executable-editable"):
            raise ValueError("only completed validated shader sources can be retained")
        digest, workspace, organization = self._identity(session)
        connection = self._connect(create=True)
        assert connection is not None
        try:
            with connection:
                connection.execute(
                    "INSERT OR IGNORE INTO accepted_reviews VALUES (?, ?, ?, ?, ?)",
                    (digest, workspace, organization, session["decision"],
                     str(session["completedAt"])),
                )
        finally:
            connection.close()

    def contains(self, session: Mapping[str, Any]) -> bool:
        if (session.get("state") != "completed" or
                session.get("decision") != "accepted-executable-editable"):
            return False
        try:
            digest, workspace, organization = self._identity(session)
            connection = self._connect(create=False)
            if connection is None:
                return False
            try:
                return connection.execute(
                    "SELECT 1 FROM accepted_reviews WHERE digest=? AND workspace=? "
                    "AND organization=? AND decision=?",
                    (digest, workspace, organization, session["decision"]),
                ).fetchone() is not None
            finally:
                connection.close()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return False


class OrganizationCheckpoints(CompletedShaderReviews):
    """Latest Host-authenticated pending session; never publication authority.

    The proposal and explanatory documents are *working state*.
    They deliberately do not participate in this receipt: freezing those files
    made an ordinary draft edit indistinguishable from corruption after a Host
    restart.  The external store authenticates only the exact ``session.json``
    ledger.  Baseline/source/artifact relationships are validated by the
    organization manager when the session is resumed or finalized.

    ``snapshot`` remains in the table for backwards compatibility with stores
    created by the old whole-directory checkpoint implementation.  It is no
    longer an acceptance condition.
    """

    @staticmethod
    def _snapshot(session: Mapping[str, Any]) -> str:
        # Do not inspect mutable workspace artifacts here.  The exact session
        # digest stored beside this marker is the Host-owned ledger receipt.
        # A constant marker also lets current code read legacy rows without
        # requiring (or trusting) their obsolete directory snapshot.
        return "working-state-not-frozen/v2"

    def remember(self, session: Mapping[str, Any]) -> None:
        root = Path(str(session["workspacePath"])).resolve()
        if self.path.resolve().is_relative_to(root):
            raise ValueError("organization checkpoint store must be outside the workspace")
        digest, workspace, organization = self._identity(session)
        pending = session.get("state") in {"drafting", "awaiting-proposal-review"}
        snapshot = self._snapshot(session) if pending else ""
        connection = self._connect(create=True)
        assert connection is not None
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS organization_checkpoints ("
                    "workspace TEXT NOT NULL, organization TEXT NOT NULL, digest TEXT NOT NULL, "
                    "snapshot TEXT NOT NULL, pending INTEGER NOT NULL, PRIMARY KEY(workspace,organization))")
                connection.execute("INSERT OR REPLACE INTO organization_checkpoints VALUES (?,?,?,?,?)",
                                   (workspace, organization, digest, snapshot, int(pending)))
        finally:
            connection.close()

    def contains(self, session: Mapping[str, Any]) -> bool:
        if session.get("state") not in {"drafting", "awaiting-proposal-review"}:
            return False
        try:
            digest, workspace, organization = self._identity(session)
            connection = self._connect(create=False)
            if connection is None:
                return False
            try:
                row = connection.execute("SELECT digest,pending FROM organization_checkpoints "
                    "WHERE workspace=? AND organization=?", (workspace, organization)).fetchone()
                return row == (digest, 1)
            finally:
                connection.close()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return False

    def has_pending(self, workspace: Path, organization: str) -> bool:
        try:
            connection = self._connect(create=False)
            if connection is None:
                return False
            try:
                return connection.execute("SELECT 1 FROM organization_checkpoints "
                    "WHERE workspace=? AND organization=? AND pending=1",
                    (str(workspace.resolve()), organization)).fetchone() is not None
            finally:
                connection.close()
        except (OSError, ValueError, sqlite3.Error):
            return False

    def retire_as_explanation_only(self, session: Mapping[str, Any]) -> None:
        """Retain an authenticated legacy snapshot but remove pending authority."""

        if not self.contains(session):
            raise ValueError("only the exact current organization checkpoint can be retired")
        digest, workspace, organization = self._identity(session)
        connection = self._connect(create=True)
        assert connection is not None
        try:
            with connection:
                updated = connection.execute(
                    "UPDATE organization_checkpoints SET digest=?,snapshot=?,pending=0 "
                    "WHERE workspace=? AND organization=? AND pending=1",
                    (digest, self._snapshot(session), workspace, organization),
                ).rowcount
                if updated != 1:
                    raise ValueError("legacy organization checkpoint changed during retirement")
        finally:
            connection.close()
