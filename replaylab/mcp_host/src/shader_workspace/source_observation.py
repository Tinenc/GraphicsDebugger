"""Keep temporary organization source swaps out of save/watch replay publication.

This lock serializes observation, not editing authority: it never restricts an
editor save or decides what the AI may change. A review holds it while canonical
source temporarily contains a proposal. Watch/apply readers try it without
waiting and resume after the reviewed source has been restored or published.
The OS releases it on process exit, so an old lock file cannot block recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time

from .io import canonical_workspace_identity


_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_DEPTH = threading.local()


@contextmanager
def source_observation_transaction(root: str | Path, *, blocking: bool = True):
    root = Path(root).resolve(strict=True)
    key = canonical_workspace_identity(root)
    with _GUARD:
        local = _LOCKS.setdefault(key, threading.RLock())
    acquired = local.acquire(blocking=blocking)
    if not acquired:
        yield False
        return
    depths = getattr(_DEPTH, "workspaces", None)
    if depths is None:
        depths = _DEPTH.workspaces = {}
    if depths.get(key, 0):
        depths[key] += 1
        try:
            yield True
        finally:
            depths[key] -= 1
            local.release()
        return
    stream = None
    locked = False
    try:
        directory = Path(root) / "watch"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".source-observation.lock"
        if directory.is_symlink() or path.is_symlink():
            raise RuntimeError("source observation lock must not use a redirected path")
        stream = path.open("a+b")
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + 300.0
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, BlockingIOError):
                if not blocking:
                    yield False
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("source observation transaction is still in progress")
                time.sleep(0.02)
        depths[key] = 1
        yield True
    finally:
        depths.pop(key, None)
        if stream is not None:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        local.release()


def source_observation_available(root: str | Path) -> bool:
    with source_observation_transaction(root, blocking=False) as available:
        return available
