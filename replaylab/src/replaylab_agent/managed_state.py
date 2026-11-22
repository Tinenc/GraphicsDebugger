"""Recover inaccessible Bread-owned provider state without elevating the app.

Never read/copy vendor credentials. An inaccessible old login stays preserved;
the official provider performs authentication in the new writable directory.
"""
from __future__ import annotations
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _redirected(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def fallback_codex_home(primary: Path, environment) -> Path:
    local = environment.get("LOCALAPPDATA") or environment.get("XDG_DATA_HOME")
    root = Path(local).expanduser() if local else Path.home() / ".local/share"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", primary.name) or primary.name in {".", ".."}:
        raise ValueError("Invalid managed profile identity")
    return absolute(root / "RenderDocBread/provider-state/codex" / primary.name)


def selected_codex_home(primary: Path, environment) -> Path:
    primary = absolute(primary)
    fallback = fallback_codex_home(primary, environment)
    marker = fallback / ".bread-home.json"
    try:
        if not _redirected(fallback) and marker.stat().st_size < 8192:
            value = json.loads(marker.read_text(encoding="utf-8"))
            if value == {"schema": "bread-managed-home/v1", "primary": str(primary)}:
                return fallback
    except (OSError, ValueError):
        pass
    return primary


def _probe(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _redirected(path) or not path.is_dir():
        raise ValueError("Managed state must be an ordinary directory")
    if os.name == "nt":
        # Exercise the same directory-handle canonicalization which Rust's
        # app-server needs. stat()/exists() alone do not prove this access.
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        api.CreateFileW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint]
        handle = api.CreateFileW(str(path), 0x80, 7, None, 3, 0x02000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            if not api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            api.CloseHandle(handle)
    fd, name = tempfile.mkstemp(prefix=".bread-access-", dir=path)
    os.close(fd)
    Path(name).unlink()


def prepare_codex_home(home: Path, environment) -> Path:
    home = absolute(home)
    try:
        _probe(home)
        return home
    except PermissionError:
        if environment.get("REPLAYLAB_MANAGED_CODEX_HOME") != "1":
            raise
    primary_root = absolute(Path(environment.get("LOCALAPPDATA") or environment.get("XDG_DATA_HOME")
                                 or Path.home() / ".local/share") / "ReplayLab/codex")
    # Only recover the fixed, validated Bread namespace. Custom caller homes
    # must never silently redirect or escape their owner's policy.
    if home.parent != primary_root:
        raise PermissionError("The selected provider state directory is inaccessible")
    fallback = fallback_codex_home(home, environment)
    _probe(fallback)
    marker = fallback / ".bread-home.json"
    fd, name = tempfile.mkstemp(prefix=".bread-home-", dir=fallback)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"schema": "bread-managed-home/v1", "primary": str(home)}, stream)
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return fallback
