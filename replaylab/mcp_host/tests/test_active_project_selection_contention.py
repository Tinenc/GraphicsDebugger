"""Internal and task Hosts must wait for short selection commits, never steal them."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from src import active_projects as projects


def test_foreign_host_selection_lock_is_waited_for_until_normal_release(tmp_path):
    lock = tmp_path / "selection.lock"
    code = """import pathlib,sys
from src import active_projects as p
with p._exclusive_lock(pathlib.Path(sys.argv[1]), 'child selection'):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(projects.__file__).resolve().parents[1])
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment)
    acquired, finished, errors = threading.Event(), threading.Event(), []
    def acquire():
        try:
            with projects._exclusive_lock(lock, "parent selection", wait_seconds=3.0):
                acquired.set()
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()
    worker = None
    try:
        assert child.stdout.readline().strip() == "ready"
        original = lock.read_bytes()
        worker = threading.Thread(target=acquire)
        worker.start()
        assert not acquired.wait(0.08)
        assert lock.read_bytes() == original
        child.stdin.write("release\n")
        child.stdin.flush()
        child.wait(timeout=3)
        assert finished.wait(3)
        assert not errors and acquired.is_set()
        assert not lock.exists()
        assert not list(tmp_path.glob("*.recovered-*"))
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)
        if worker is not None:
            worker.join(timeout=3)


def test_live_owner_timeout_keeps_original_lock(tmp_path):
    lock = tmp_path / "selection.lock"
    with projects._exclusive_lock(lock, "owner"):
        original = lock.read_bytes()
        with pytest.raises(projects.ActiveProjectError, match="owner could not be proven inactive"):
            with projects._exclusive_lock(lock, "contender", wait_seconds=0.05):
                pytest.fail("a live lock was stolen")
        assert lock.read_bytes() == original


def test_snapshot_rechecks_actual_selection_after_contention(tmp_path):
    service = projects.ActiveReplayProjectService(tmp_path / "active.json", tmp_path / "projects")
    initial = service.ensure_pending("Before")
    with pytest.raises(projects.ActiveProjectError, match="selection or configuration changed"):
        with service.snapshot_active_project(initial["workspaceId"], initial["projectRoot"]):
            # An authorized user change is still detected; waiting is never a
            # reason to silently keep executing against another project.
            selection = json.loads(service.state_path.read_bytes())
            selection["selectedAt"] = "2030-01-01T00:00:00Z"
            selection["selectionSha256"] = projects._document_hash(selection, "selectionSha256")
            service.state_path.write_text(json.dumps(selection), encoding="utf-8")


def test_unverifiable_owner_is_not_hidden_by_waiting(tmp_path):
    lock = tmp_path / "selection.lock"
    lock.write_bytes(b"a" * 32)
    with pytest.raises(projects.ActiveProjectError, match="owner could not be proven inactive"):
        with projects._exclusive_lock(lock, "legacy", wait_seconds=0.1):
            pytest.fail("an unverifiable lock was stolen")
    assert lock.read_bytes() == b"a" * 32
