"""Bounded content hashing cache, invalidated by filesystem change identity."""
from collections import OrderedDict
import ctypes
import os
from pathlib import Path
import threading
import hashlib
import json
import time
from functools import lru_cache

_cache = OrderedDict()
_lock = threading.Lock()
_condition = threading.Condition(_lock)
_hashing = set()
_stats = {"memoryHits": 0, "diskHits": 0, "hashReads": 0, "bytesHashed": 0}


@lru_cache(maxsize=1)
def _windows_api():
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ("created", "accessed", "written", "changed")] + [("attributes", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel, Basic


def file_stamp(path):
    stat = Path(path).stat()
    change = getattr(stat, "st_ctime_ns", 0)
    if os.name == "nt":
        change = None  # Windows st_ctime is creation time, not a change identity.
        kernel, Basic = _windows_api()
        # Refuse cached observations while another handle owns write access;
        # Windows can delay write timestamps until that writer closes.
        handle = kernel.CreateFileW(str(Path(path).resolve()), 0x80, 5, None, 3, 0, None)
        if handle not in (None, ctypes.c_void_p(-1).value):
            try:
                value = Basic()
                if kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(value), ctypes.sizeof(value)):
                    change = value.changed
            finally:
                kernel.CloseHandle(handle)
    if change is None or change <= 0:
        return None  # Unsupported/zero change times never authorize reuse.
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, change


def get(path, stamp):
    if stamp is None:
        return None
    key = os.path.normcase(str(Path(path).resolve()))
    with _lock:
        value = _cache.get(key)
        if value and value[0] == stamp:
            _cache.move_to_end(key)
            _stats["memoryHits"] += 1
            return value[1]
    return None


def put(path, stamp, digest):
    if stamp is None:
        return
    key = os.path.normcase(str(Path(path).resolve()))
    with _lock:
        _cache[key] = (stamp, digest)
        _cache.move_to_end(key)
        while len(_cache) > 2048:
            _cache.popitem(last=False)


def statistics():
    with _lock:
        return dict(_stats)


def _disk_path(path):
    root = Path(os.environ.get("BREAD_FILE_DIGEST_CACHE_ROOT") or
                Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home()/".cache")/"ReplayLab/cache/file-digests")
    key = hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode("utf-8")).hexdigest()
    return root / (key + ".json")


def _persistent(path, stamp, digest=None):
    """Disposable file observations, never stored GPU handles or replay authority."""
    if stamp is None:
        return None
    try:
        target = _disk_path(path)
        if digest is None:
            if target.stat().st_size > 4096 or target.is_symlink():
                return None
            row = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(row,dict):
                return None
            seal = row.pop("seal", None)
            if seal != hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest():
                return None
            value = row.get("sha256")
            if (row.get("schema") == "bread-file-digest/v1" and row.get("stamp") == list(stamp)
                    and row.get("path") == os.path.normcase(str(Path(path).resolve()))
                    and isinstance(value,str) and len(value)==64 and all(c in "0123456789abcdef" for c in value)):
                with _lock:
                    _stats["diskHits"] += 1
                return value
        else:
            row = {"schema":"bread-file-digest/v1", "path":os.path.normcase(str(Path(path).resolve())),
                   "stamp":list(stamp), "sha256":digest}
            row["seal"] = hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
            from .shader_workspace.io import atomic_write_json
            atomic_write_json(target,row)
    except (OSError,ValueError,TypeError):
        pass  # A missing, old or corrupt cache always falls back to real bytes.
    return None


def digest_file(path, *, persistent=False, checkpoint=None, maximum_bytes=None):
    """Hash stable bytes once per filesystem change; share concurrent readers.

    Filesystems without a reliable change identity always read bytes. Check the
    native current capture/Shader independently when applying a replacement.
    """
    path = Path(path)
    key = os.path.normcase(str(path.resolve()))
    while True:
        with _condition:
            if key not in _hashing:
                _hashing.add(key)
                break
            _condition.wait(0.1)
        if checkpoint is not None:
            checkpoint("waiting for shared file digest")
    try:
        before = path.stat()
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise ValueError("file exceeds digest size limit")
        stamp = file_stamp(path)
        value = get(path, stamp)
        if value is None and persistent:
            value = _persistent(path,stamp)
        if value is not None and file_stamp(path) == stamp:
            put(path,stamp,value)
            return value
        digest = hashlib.sha256(); size = 0; next_check = time.monotonic()+1
        with path.open("rb") as source:
            while True:
                block = source.read(8*1024*1024)
                if not block:
                    break
                size += len(block)
                if maximum_bytes is not None and size > maximum_bytes:
                    raise ValueError("file exceeds digest size limit")
                digest.update(block)
                if checkpoint is not None and time.monotonic() >= next_check:
                    checkpoint("hashing stable file contents"); next_check = time.monotonic()+1
        after = path.stat(); final_stamp = file_stamp(path)
        if (size != before.st_size or
                (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) !=
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or final_stamp != stamp):
            raise ValueError("file contents changed while hashing")
        value = digest.hexdigest()
        with _lock:
            _stats["hashReads"] += 1; _stats["bytesHashed"] += size
        put(path,final_stamp,value)
        if persistent:
            _persistent(path,final_stamp,value)
        return value
    finally:
        with _condition:
            _hashing.discard(key); _condition.notify_all()
