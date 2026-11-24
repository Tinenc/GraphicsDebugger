"""UTF-8 draft copies in the trusted controller's per-run Agent directory.

These files are editable working artifacts, never captured evidence or replay
authority. The model cannot supply a workspace root. Canonical Shader writes
still pass through the organization manager's publication transaction.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

AGENT_WORKSPACE_ROOT_ENV = "REPLAYLAB_AGENT_WORKSPACE_ROOT"
MAX_AGENT_TEXT_BYTES = 4 * 1024 * 1024
MAX_AGENT_JSON_BYTES = 16 * 1024 * 1024
_RESERVED = {".cursor", ".claude", ".gemini", ".codex", ".mcp.json", "mcp.json",
             "settings.json", "config.toml", "agents.md", "claude.md", "gemini.md"}


def _regular_components(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            value = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            raise ValueError("Agent artifact path contains a symlink or reparse point")


def agent_workspace_root() -> Path:
    value = os.environ.get(AGENT_WORKSPACE_ROOT_ENV, "")
    root = Path(value)
    if not value or not root.is_absolute() or not root.is_dir():
        raise ValueError("This run has no controller-provided Agent working directory")
    _regular_components(root)
    return root.resolve()


def _artifact_path(value: str, *, basename_only: bool = False) -> tuple[Path, Path]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Agent file must name a working artifact")
    root = agent_workspace_root()
    supplied = Path(value)
    if ".." in supplied.parts or basename_only and supplied.name != value:
        raise ValueError("Agent file must stay inside the current working directory")
    path = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("Agent file belongs outside the current run") from error
    def reserved(parts):
        return any(part.startswith(".") or part.lower() in _RESERVED
                   or part.endswith((" ", ".")) or ":" in part for part in parts)

    if not relative.parts or reserved(relative.parts):
        raise ValueError("Agent file cannot name a launch or configuration artifact")
    _regular_components(path)
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Agent file is redirected outside the current run")
    if reserved(resolved.relative_to(root).parts):
        raise ValueError("Agent file resolves to a launch or configuration artifact")
    return root, path


def _check_open_file(stream, root: Path, path: Path) -> None:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Agent artifact must be a regular file with one link")
    if os.name == "nt":
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            raise OSError("Cannot determine the Agent artifact handle path")
        final = buffer.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[8:]
        elif final.startswith("\\\\?\\"):
            final = final[4:]
        if Path(final).resolve() != path.resolve():
            raise ValueError("Agent artifact changed while opening")
    _regular_components(path)
    current = path.stat()
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or not path.resolve().is_relative_to(root):
        raise ValueError("Agent artifact changed while opening")


def _read_agent_bytes(value: str, maximum: int) -> tuple[bytes, dict]:
    root, path = _artifact_path(value)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        _check_open_file(stream, root, path)
        if os.fstat(stream.fileno()).st_size > maximum:
            raise ValueError("Agent artifact exceeds its %d MiB limit" % (maximum // (1024 * 1024)))
        raw = stream.read(maximum + 1)
        _check_open_file(stream, root, path)
    if len(raw) > maximum or not raw or b"\0" in raw:
        raise ValueError("Agent artifact must be non-empty NUL-free text up to %d MiB" % (maximum // (1024 * 1024)))
    relative = path.relative_to(root).as_posix()
    return raw, {"path": str(path), "relativePath": relative, "virtualPath": "/" + relative,
                  "sha256": hashlib.sha256(raw).hexdigest(),
                  "sizeBytes": len(raw), "workingCopy": True, "replayAuthority": False}


def read_agent_text_file(value: str) -> tuple[str, dict]:
    raw, metadata = _read_agent_bytes(value, MAX_AGENT_TEXT_BYTES)
    return raw.decode("utf-8", errors="strict"), metadata


def read_agent_json_file(value: str, expected_sha256: str) -> tuple[object, dict]:
    """Read a bounded owned data file once and bind the exact bytes before use.

    Large structured tool arguments use this route instead of serializing the
    same multi-megabyte array into provider messages. This grants no authority
    to its contents; callers still validate their ordinary input schemas.
    """
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Agent JSON file requires its current lowercase 64-character SHA256")
    raw, metadata = _read_agent_bytes(value, MAX_AGENT_JSON_BYTES)
    if metadata["sha256"] != expected_sha256:
        raise ValueError("Agent JSON file SHA256 does not match; re-read the current file and submit its actual hash")
    try:
        value = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (ValueError, RecursionError) as error:
        raise ValueError("Agent data file must contain valid UTF-8 JSON: %s" % str(error)[:200]) from error
    return value, metadata


def copy_text_to_agent_workspace(value: str, raw: bytes) -> dict:
    if not raw or len(raw) > MAX_AGENT_TEXT_BYTES or b"\0" in raw:
        raise ValueError("Agent copy must be non-empty NUL-free text up to 4 MiB")
    raw.decode("utf-8", errors="strict")
    root, path = _artifact_path(value, basename_only=True)
    for attempt in range(8):
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            stream = os.fdopen(os.open(path, flags, 0o600), "wb")
            break
        except FileExistsError:
            path = root / (Path(value).stem + "-" + uuid.uuid4().hex[:12] + Path(value).suffix)
    else:
        raise OSError("Could not allocate an independent Agent draft copy")
    with stream:
        _check_open_file(stream, root, path)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        _check_open_file(stream, root, path)
    relative = path.relative_to(root).as_posix()
    return {"path": str(path), "relativePath": relative, "virtualPath": "/" + relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "sizeBytes": len(raw), "workingCopy": True, "replayAuthority": False}
