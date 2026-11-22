"""Short shared reads and file-specific metadata transactions.

Readers never take a project lock. Windows readers permit atomic replacement;
kernel-owned writer locks are released even if a process exits unexpectedly.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import time


def open_shared_read(path):
    if os.name != "nt":
        return open(path, "rb")
    import msvcrt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    api.CreateFileW.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = api.CreateFileW(str(Path(path).absolute()), 0x80000000, 7, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


@contextmanager
def file_transaction(target, timeout=2.0):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stable, tiny sidecar; no stale PID lock to delete/recover on next launch.
    with open(str(target) + ".update.lock", "a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0"); stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Metadata file is being updated; retry this file operation") from error
                time.sleep(.01)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
