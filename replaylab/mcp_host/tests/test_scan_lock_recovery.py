"""Only proved inactive scan owners may be replaced; scan data is never touched."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from src import active_projects as locks
from src.symbols import scanner


LEGACY = (b'{"createdUnixNs": 1788836849410962500, "pid": 22796, '
          b'"token": "22796:1788836849410962500:b709641566adbe4270bebae7cff4fa13"}\n')


def _probe_owner(state, identity="", pid=22796):
    original = locks._process_lock_owner_state
    return mock.patch.object(locks, "_process_lock_owner_state", side_effect=lambda queried:
                             (state, identity) if queried == pid else original(queried))


def _structured(identity="windows-filetime:0000000000001234", pid=22796):
    value = json.loads(locks._exclusive_project_lock_payload("a" * 32))
    value.update(ownerPid=pid, processStartIdentity=identity)
    value["lockSha256"] = locks._document_hash(value, "lockSha256")
    return json.dumps(value).encode()


def test_real_legacy_dead_pid_lock_is_archived_and_new_owner_released(tmp_path):
    path = tmp_path / "same.scan.lock"
    path.write_bytes(LEGACY)
    with _probe_owner("dead"):
        with scanner._exclusive_scan_lock(path):
            acquired = json.loads(path.read_bytes())
            assert acquired["ownerPid"] == os.getpid()
            assert acquired["processStartIdentity"]
            assert acquired["schemaVersion"] == locks.EXCLUSIVE_PROJECT_LOCK_VERSION
        assert not path.exists()
    archives = list(tmp_path.glob("*.recovered-*"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == LEGACY


@pytest.mark.parametrize("state,identity", [("alive", "windows-filetime:0000000000005678"),
                                           ("unknown", ""), ("alive", "")])
def test_legacy_live_or_unverifiable_pid_is_not_recovered_even_when_old(tmp_path, state, identity):
    path = tmp_path / "same.scan.lock"
    path.write_bytes(LEGACY)
    with _probe_owner(state, identity), pytest.raises(RuntimeError, match="already locked"):
        with scanner._exclusive_scan_lock(path):
            pytest.fail("unverified owner was replaced")
    assert path.read_bytes() == LEGACY
    assert list(tmp_path.glob("*.recovered-*")) == []


def test_v2_pid_reuse_requires_a_different_os_process_start_identity(tmp_path):
    path = tmp_path / "same.scan.lock"
    original = _structured()
    path.write_bytes(original)
    with _probe_owner("alive", "windows-filetime:0000000000005678"):
        with scanner._exclusive_scan_lock(path):
            assert json.loads(path.read_bytes())["ownerPid"] == os.getpid()
    assert not path.exists()
    assert next(tmp_path.glob("*.recovered-*")).read_bytes() == original


@pytest.mark.parametrize("state,identity", [("alive", "windows-filetime:0000000000001234"),
                                           ("unknown", ""), ("alive", "linux-startticks:1234")])
def test_v2_active_or_unverifiable_owner_stays_locked(tmp_path, state, identity):
    path = tmp_path / "same.scan.lock"
    original = _structured()
    path.write_bytes(original)
    with _probe_owner(state, identity), pytest.raises(RuntimeError, match="already locked"):
        with scanner._exclusive_scan_lock(path):
            pytest.fail("live owner was replaced")
    assert path.read_bytes() == original


@pytest.mark.parametrize("raw", [b"", b"{broken", LEGACY.replace(b'"pid": 22796', b'"pid": true'),
                                LEGACY.replace(b'22796:', b'22797:'),
                                LEGACY.replace(b'{', b'{"pid":1,', 1),
                                b'{"pid":22796,"createdUnixNs":1,"token":"x"}'])
def test_malformed_legacy_lock_never_authorizes_recovery(tmp_path, raw):
    path = tmp_path / "same.scan.lock"
    path.write_bytes(raw)
    with _probe_owner("dead"), pytest.raises(RuntimeError, match="already locked"):
        with scanner._exclusive_scan_lock(path):
            pytest.fail("malformed owner was replaced")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("error", [RuntimeError("scan failed"), asyncio.CancelledError(), KeyboardInterrupt()])
def test_failed_or_cancelled_scan_releases_its_exact_lock(tmp_path, error):
    path = tmp_path / "same.scan.lock"
    with pytest.raises(type(error)):
        with scanner._exclusive_scan_lock(path):
            raise error
    assert not path.exists()


def test_release_preserves_a_lock_replaced_by_another_owner(tmp_path):
    path = tmp_path / "same.scan.lock"
    replacement = _structured()
    with scanner._exclusive_scan_lock(path):
        path.write_bytes(replacement)
    assert path.read_bytes() == replacement


def test_failed_atomic_publication_leaves_no_authoritative_or_pending_lock(tmp_path, monkeypatch):
    path = tmp_path / "same.scan.lock"
    monkeypatch.setattr(locks.os, "fsync", mock.Mock(side_effect=OSError("flush failed")))
    with pytest.raises(OSError, match="flush failed"):
        with scanner._exclusive_scan_lock(path):
            pytest.fail("unwritten lock acquired")
    assert list(tmp_path.iterdir()) == []


def test_hard_process_exit_is_recovered_using_real_os_creation_identity(tmp_path):
    path = tmp_path / "crash.scan.lock"
    code = """
import os, pathlib, sys
from src.symbols.scanner import _exclusive_scan_lock
with _exclusive_scan_lock(pathlib.Path(sys.argv[1])):
    os._exit(0)
"""
    module_root = Path(scanner.__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(module_root)}
    child = subprocess.run([sys.executable, "-c", code, str(path)],
                           cwd=module_root, env=environment, capture_output=True, timeout=10)
    assert child.returncode == 0, child.stderr.decode(errors="replace")
    raw = path.read_bytes()
    owner = json.loads(raw)
    assert locks._process_lock_owner_state(owner["ownerPid"])[0] == "dead"
    with scanner._exclusive_scan_lock(path):
        assert json.loads(path.read_bytes())["ownerPid"] == os.getpid()
    assert not path.exists()
    assert next(tmp_path.glob("*.recovered-*")).read_bytes() == raw
