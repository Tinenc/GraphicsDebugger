"""Real process ownership, crash recovery and legacy-writer compatibility."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.shader_workspace.errors import ManifestValidationError
from src.shader_workspace.workspace import _WorkspaceFileLock, _workspace_operation_locked


CHILD = """
import sys, time
from pathlib import Path
from src.shader_workspace.workspace import _WorkspaceFileLock
root = Path(sys.argv[1])
with _WorkspaceFileLock(root):
    (root / 'ready').write_text('ready')
    while not (root / 'release').exists():
        time.sleep(.02)
"""


@pytest.fixture
def owner(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists():
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode("utf-8", errors="replace"))
            assert time.monotonic() < deadline, "lock owner did not become ready"
            time.sleep(.02)
        yield process, tmp_path
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_live_owner_cannot_be_stolen_and_marker_is_preserved(owner):
    process, root = owner
    marker = (root / ".workspace-operation.lock").read_bytes()
    with pytest.raises(ManifestValidationError, match="busy"):
        with _WorkspaceFileLock(root, wait_seconds=.1):
            pytest.fail("a second process entered the protected operation")
    assert process.poll() is None
    assert (root / ".workspace-operation.lock").read_bytes() == marker
    assert _workspace_operation_locked(root) is True


def test_crashed_owner_is_recovered_without_manual_lock_removal(owner):
    process, root = owner
    previous = json.loads((root / ".workspace-operation.lock").read_text())
    process.kill()
    process.communicate(timeout=10)
    assert (root / ".workspace-operation.lock").exists()
    assert _workspace_operation_locked(root) is False
    with _WorkspaceFileLock(root, wait_seconds=.1):
        current = json.loads((root / ".workspace-operation.lock").read_text())
        assert current["token"] != previous["token"]
    assert not (root / ".workspace-operation.lock").exists()


def test_normal_contention_waits_for_owner_release(owner):
    process, root = owner
    timer = threading.Timer(.15, lambda: (root / "release").write_text("release"))
    timer.start()
    try:
        with _WorkspaceFileLock(root, wait_seconds=2):
            assert json.loads((root / ".workspace-operation.lock").read_text())["pid"] != process.pid
        process.communicate(timeout=10)
        assert process.returncode == 0
    finally:
        timer.join()


def test_unknown_legacy_token_is_not_assumed_orphaned(tmp_path):
    marker = tmp_path / ".workspace-operation.lock"
    marker.write_bytes(b"legacy-owner-token\n")
    with pytest.raises(ManifestValidationError, match="legacy token is never assumed stale"):
        with _WorkspaceFileLock(tmp_path, wait_seconds=0):
            pass
    assert marker.read_bytes() == b"legacy-owner-token\n"
    assert _workspace_operation_locked(tmp_path) is True


def test_legacy_writer_racing_marker_publication_is_preserved(monkeypatch, tmp_path):
    original_link = __import__("os").link
    marker = tmp_path / ".workspace-operation.lock"

    def race(source, target):
        marker.write_bytes(b"older-active-owner\n")
        original_link(source, target)

    monkeypatch.setattr("src.shader_workspace.workspace.os.link", race)
    with pytest.raises(ManifestValidationError, match="locked by another process"):
        with _WorkspaceFileLock(tmp_path, wait_seconds=0):
            pass
    assert marker.read_bytes() == b"older-active-owner\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_release_never_deletes_replaced_ownership_marker(tmp_path):
    marker = tmp_path / ".workspace-operation.lock"
    with _WorkspaceFileLock(tmp_path):
        value = json.loads(marker.read_text())
        value["token"] = "f" * 32
        marker.write_text(json.dumps(value))
    assert json.loads(marker.read_text())["token"] == "f" * 32


def test_stable_os_lock_file_does_not_mean_an_active_operation(tmp_path):
    assert _workspace_operation_locked(tmp_path) is False
    assert not list(tmp_path.iterdir())
    with _WorkspaceFileLock(tmp_path):
        assert _workspace_operation_locked(tmp_path) is True
    assert (tmp_path / ".workspace-operation.oslock").exists()
    assert _workspace_operation_locked(tmp_path) is False


def test_project_seed_recovers_same_crashed_owner_protocol(owner):
    from src.projects import _workspace_seed_lock
    process, root = owner
    process.kill()
    process.communicate(timeout=10)
    with _workspace_seed_lock(root):
        assert _workspace_operation_locked(root) is True
    assert not (root / ".workspace-operation.lock").exists()


def test_project_seed_preserves_unknown_legacy_owner(tmp_path):
    from src.projects import _workspace_seed_lock, ReplayProjectError
    marker = tmp_path / ".workspace-operation.lock"
    marker.write_bytes(b"old-active-seed-writer\n")
    with pytest.raises(ReplayProjectError, match="legacy token is never assumed stale"):
        with _workspace_seed_lock(tmp_path):
            pass
    assert marker.read_bytes() == b"old-active-seed-writer\n"
