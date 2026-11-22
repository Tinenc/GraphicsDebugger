"""Selection lock release must survive readers without abandoning a live lock."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src import active_projects as active


def test_release_retries_a_temporarily_unreadable_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'selection.lock'
    snapshot = active._lock_snapshot
    calls = []
    with active._exclusive_lock(path, 'test selection'):
        def read(current):
            calls.append(current)
            return None if len(calls) <= 2 else snapshot(current)
        monkeypatch.setattr(active, '_lock_snapshot', read)
    assert len(calls) == 3
    assert not path.exists()


def test_release_retries_a_sharing_violation(tmp_path, monkeypatch):
    path = tmp_path / 'selection.lock'
    unlink = Path.unlink
    attempts = []
    with active._exclusive_lock(path, 'test selection'):
        def remove(current, *args, **kwargs):
            if current == path:
                attempts.append(current)
                if len(attempts) <= 2:
                    raise PermissionError('simulated Windows sharing violation')
            return unlink(current, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', remove)
    assert len(attempts) == 3
    assert not path.exists()


def test_release_never_removes_a_replacement_token_after_retry(tmp_path, monkeypatch):
    path = tmp_path / 'selection.lock'
    unlink = Path.unlink
    with active._exclusive_lock(path, 'test selection'):
        def remove(current, *args, **kwargs):
            if current == path:
                path.write_bytes(b'replacement owner')
                raise PermissionError('token changed during sharing conflict')
            return unlink(current, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', remove)
    assert path.read_bytes() == b'replacement owner'


@pytest.mark.parametrize('unreadable', [False, True])
def test_release_exhaustion_is_reported_without_deleting_unproven_owner(tmp_path, monkeypatch, unreadable):
    path = tmp_path / 'selection.lock'
    monkeypatch.setattr(active, 'EXCLUSIVE_LOCK_RELEASE_WAIT_SECONDS', 0.02)
    unlink = Path.unlink
    with pytest.raises(active.ActiveProjectError, match='could not release its owned lock'):
        with active._exclusive_lock(path, 'test selection'):
            if unreadable:
                monkeypatch.setattr(active, '_lock_snapshot', lambda unused: None)
            else:
                def remove(current, *args, **kwargs):
                    if current == path:
                        raise PermissionError('persistent read handle')
                    return unlink(current, *args, **kwargs)
                monkeypatch.setattr(Path, 'unlink', remove)
    assert path.is_file()


def test_critical_section_error_still_releases_lock(tmp_path):
    path = tmp_path / 'selection.lock'
    with pytest.raises(ValueError, match='operation failed'):
        with active._exclusive_lock(path, 'test selection'):
            raise ValueError('operation failed')
    assert not path.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows non-delete-sharing reader regression')
def test_windows_reader_process_cannot_leave_live_owner_lock(tmp_path):
    path = tmp_path / 'selection.lock'
    reader = None
    try:
        with active._exclusive_lock(path, 'test selection'):
            reader = subprocess.Popen(
                [sys.executable, '-c',
                 'import os,sys,time; f=os.open(sys.argv[1],os.O_RDONLY); '
                 'print("ready",flush=True); time.sleep(.3); os.close(f)', str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert reader.stdout.readline().strip() == 'ready'
        assert not path.exists()
        assert reader.wait(timeout=5) == 0
        # Reuse succeeds while the original lock-owning process remains alive.
        with active._exclusive_lock(path, 'next selection'):
            assert path.is_file()
        assert not path.exists()
    finally:
        if reader is not None:
            reader.wait(timeout=5)
            reader.stdout.close()
            reader.stderr.close()
