"""Shared lifetime leases protect a login directory used by another app.

The OS releases handles on crashes; a leftover empty sidecar never blocks use.
Readers may run concurrently. Only destructive cleanup requires exclusivity.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import re


@contextmanager
def profile_lease(config_path, profile_id, *, cleanup=False):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", profile_id or "") or profile_id in {".", ".."}:
        raise ValueError("Invalid provider lease identity")
    path = Path(config_path).parent / "provider-use" / (profile_id + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        api.CreateFileW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        # OPEN_ALWAYS requires write access only when creating the empty file.
        if not path.exists():
            try:
                with path.open("xb"):
                    pass
            except FileExistsError:
                pass
        handle = api.CreateFileW(str(path.absolute()), 0x80000000, 0 if cleanup else 1,
                                 None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            api.CloseHandle(handle)
    else:
        import fcntl
        with path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if cleanup else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
