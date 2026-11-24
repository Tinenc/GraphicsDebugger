"""Process-local reuse of a fully validated, unchanged project read.

Records the files actually consumed by the validator. No stored flag grants
replay authority; the current capture and Shader apply boundary are unchanged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import os
from pathlib import Path
import threading

from .file_content_cache import file_stamp

_READS = ContextVar("bread_project_validation_reads", default=None)
_MAX_FILES = 16384


def _stamp(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    # Preserve lexical identity as well as the final file. A parent junction
    # redirect must not inherit a previous successful observation.
    return os.path.normcase(str(path.resolve())), file_stamp(path)


def observe_path(path, resolved):
    record = _READS.get()
    if record is None:
        return
    path = Path(path).absolute()
    stamp = _stamp(path)
    if (stamp is None or stamp[1] is None or stamp[0] != os.path.normcase(str(resolved))
            or (path in record["files"] and record["files"][path] != stamp)
            or len(record["files"]) >= _MAX_FILES):
        record["usable"] = False
    record["files"][path] = stamp


@contextmanager
def reading(path):
    record = _READS.get()
    if record is None:
        yield
        return
    path = Path(path).absolute()
    before = _stamp(path)
    yield
    after = _stamp(path)
    if (before is None or before[1] is None or before != after
            or (path in record["files"] and record["files"][path] != after)
            or len(record["files"]) >= _MAX_FILES):
        record["usable"] = False
    record["files"][path] = after


class ProjectReadCache:
    """One bounded observation per service, discarded on any changed input."""

    def __init__(self):
        self._lock = threading.RLock()
        self._files = {}
        self._value = None
        self._key = None

    def load(self, manifest_path, loader, *, key=None):
        with self._lock:
            if self._value is not None and self._key == key:
                try:
                    unchanged = all(_stamp(path) == stamp for path, stamp in self._files.items())
                except OSError:
                    unchanged = False
                if unchanged:
                    return copy.deepcopy(self._value)
            self._files, self._value = {}, None
            record = {"files": {}, "usable": True}
            token = _READS.set(record)
            try:
                with reading(manifest_path):
                    value = loader()
            finally:
                _READS.reset(token)
            # Inputs may change after their individual reads while the rest of
            # the project is validated. Such a read remains uncached.
            try:
                stable = (record["usable"] and record["files"]
                          and all(_stamp(path) == stamp for path, stamp in record["files"].items()))
            except OSError:
                stable = False
            if stable:
                self._files = record["files"]
                self._value = copy.deepcopy(value)
                self._key = key
            return value
