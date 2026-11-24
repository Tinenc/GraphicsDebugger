"""Retain ordinary Shader drafts before deleting an owned Agent turn directory.

Recovery copies are editable source artifacts, never Host/replay evidence. Launch
configuration, hidden directories, links and unrelated files are not archived.
Any incomplete recovery keeps the original turn directory instead of losing work.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

SCHEMA = "replaylab-shader-draft-recovery/v1"
SHADER_SUFFIXES = frozenset({".hlsl", ".hlsli", ".glsl", ".glsli", ".wgsl", ".metal",
    ".slang", ".shader", ".vert", ".frag", ".geom", ".tesc", ".tese", ".comp", ".fx", ".fxh"})
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RECOVERY_FILES = 64
MAX_RECOVERY_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SCAN_ENTRIES = 8192
OUTPUT_RETENTION_SCHEMA = "replaylab-owned-work-output-retention/v1"
_WORKSPACE_NAMES = frozenset({"agent-workspace", "cursor-workspace"})
_LAUNCH_NAMES = frozenset({"mcp.json", "settings.json", "config.toml", "agents.md", "claude.md", "gemini.md"})


def _ordinary(info: os.stat_result) -> bool:
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_path(path: Path) -> None:
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if not _ordinary(info):
            raise ValueError("Shader recovery path contains a link or reparse point")


def validated_turn_root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or re.fullmatch(r"native-[0-9a-f]{32}", root.name) is None:
        raise ValueError("Shader recovery requires a controller-owned turn directory")
    _safe_path(root)
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Shader recovery requires a canonical turn directory")
    return root


def _scan(root: Path, *, drafts_only: bool) -> list[tuple[Path, os.stat_result]]:
    pending = [(root, 0)]
    files = []
    inspected = 0
    while pending:
        directory, depth = pending.pop()
        _safe_path(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                inspected += 1
                if inspected > MAX_SCAN_ENTRIES:
                    raise ValueError("Shader recovery inspection limit reached")
                # Windows DirEntry's cached stat omits file identity/link
                # counts. lstat obtains the facts used by the copy boundary.
                info = Path(entry.path).lstat()
                if entry.name.startswith(".") or getattr(info, "st_file_attributes", 0) & 0x2 or not _ordinary(info):
                    continue
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= 16:
                        raise ValueError("Shader recovery directory depth limit reached")
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if not drafts_only or path.suffix.casefold() in SHADER_SUFFIXES:
                        files.append((path, info))
    return files


def retain_owned_work_outputs(turn_root: Path) -> dict | None:
    """Keep ordinary non-Shader outputs at the paths the Agent delivered.

    Launch files are never copied or included in the output receipt. A scan
    failure keeps the original directory because deleting unknown work would
    be irreversible. Shader-only turns retain their existing archive behavior.
    """
    root = validated_turn_root(turn_root)
    result = {"schema": OUTPUT_RETENTION_SCHEMA, "status": "source-retained",
              "sourceRetained": True, "sourceDirectory": str(root),
              "workspaceDirectories": [], "manifestPath": "", "recoveryDirectory": "",
              "files": [], "outputCount": 0, "truncated": False,
              "workingCopy": True, "replayAuthority": False, "reason": ""}
    try:
        outputs = []
        for path, info in _scan(root, drafts_only=False):
            relative = path.relative_to(root)
            parts = relative.parts
            # These two sibling files are created by native_mcp, outside cwd.
            if len(parts) == 1 and re.fullmatch(r"(?:mcp-servers|live-edit-registry)-[0-9a-f]{32}\.json", parts[0]):
                continue
            # This is the same reserved launch surface as OwnedWorkspace, at
            # the managed cwd root. Nested user output names remain outputs.
            if len(parts) == 2 and parts[0] in _WORKSPACE_NAMES and parts[1].casefold() in _LAUNCH_NAMES:
                continue
            outputs.append((path, info))
        if not any(path.suffix.casefold() not in SHADER_SUFFIXES for path, _ in outputs):
            return None
        result["outputCount"] = len(outputs)
        result["truncated"] = len(outputs) > MAX_RECOVERY_FILES
        result["files"] = [{"relativePath": path.relative_to(root).as_posix(),
                            "path": str(path), "sizeBytes": info.st_size}
                           for path, info in outputs[:MAX_RECOVERY_FILES]]
        result["workspaceDirectories"] = [str(root / name) for name in sorted(_WORKSPACE_NAMES)
            if any(path.is_relative_to(root / name) for path, _ in outputs)]
        result["reason"] = "Agent task outputs are retained at their original paths"
    except Exception as error:
        from .agent_diagnostics import public_error
        result.update(truncated=True, reason="Output inspection incomplete; original turn retained: " + public_error(error))
    return result


def _read_draft(path: Path, expected: os.stat_result) -> bytes:
    _safe_path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns):
            raise ValueError("Shader draft changed before recovery")
        raw = stream.read(MAX_FILE_BYTES + 1)
        _safe_path(path)
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise ValueError("Shader draft changed during recovery")
    if len(raw) > MAX_FILE_BYTES or b"\0" in raw:
        raise ValueError("Shader recovery accepts UTF-8 source files up to 4 MiB")
    raw.decode("utf-8", errors="strict")
    return raw


def _write_json(path: Path, value: dict) -> None:
    _safe_path(path)
    temporary = path.with_name(".recovery-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_shader_drafts(turn_root: Path) -> dict | None:
    """Return a recovery manifest, or None when no ordinary drafts exist.

    The caller may delete the source directory only when sourceRetained=false.
    Failure diagnostics contain no source contents or launch configuration.
    """
    root = validated_turn_root(turn_root)
    archive = root.parent / "recovered-shader-drafts"
    destination = archive / root.name
    manifest_path = destination / "manifest.json"
    result = {"schema": SCHEMA, "status": "source-retained", "sourceDirectory": str(root),
              "recoveryDirectory": str(destination), "manifestPath": "", "sourceRetained": True,
              "workingCopy": True, "replayAuthority": False, "files": [], "reason": ""}
    try:
        files = _scan(root, drafts_only=True)
        if not files:
            return None
        if len(files) > MAX_RECOVERY_FILES or sum(info.st_size for _, info in files) > MAX_RECOVERY_BYTES:
            raise ValueError("Shader recovery count or total size limit reached; original turn preserved")
        if any(info.st_size > MAX_FILE_BYTES for _, info in files):
            raise ValueError("Shader draft exceeds 4 MiB; original turn preserved")
        _safe_path(archive)
        if archive.exists():
            used = sum(info.st_size for _, info in _scan(archive, drafts_only=False))
            if used + sum(info.st_size for _, info in files) > MAX_ARCHIVE_BYTES:
                raise ValueError("Shader recovery archive is full; original turn preserved")
        destination.mkdir(parents=True, exist_ok=False)
        _safe_path(destination)
        for path, info in files:
            relative = path.relative_to(root)
            target = destination / relative
            _safe_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = _read_draft(path, info)
            with target.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            result["files"].append({"relativePath": relative.as_posix(), "path": str(target),
                                    "sizeBytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        def identities(entries):
            return {(str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    for path, info in entries}
        if identities(_scan(root, drafts_only=True)) != identities(files):
            raise ValueError("Shader drafts changed while recovering; original turn preserved")
        result.update(status="recovered", sourceRetained=False, manifestPath=str(manifest_path))
        _write_json(manifest_path, result)
        _write_json(archive / "latest.json", {"schema": SCHEMA, "manifestPath": str(manifest_path),
                                            "sourceDirectory": str(root), "sourceRetained": False})
    except Exception as error:
        from .agent_diagnostics import public_error
        result.update(status="source-retained", sourceRetained=True, reason=public_error(error))
        # Even when the archive is full, retain a small locator for the original
        # directory. A disk/permission failure may prevent this; the caller also
        # logs the original directory and returns these diagnostics.
        try:
            _safe_path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            result["manifestPath"] = str(manifest_path)
            _write_json(manifest_path, result)
            _write_json(archive / "latest.json", {"schema": SCHEMA, "manifestPath": str(manifest_path),
                                                "sourceDirectory": str(root), "sourceRetained": True})
        except Exception:
            result["manifestPath"] = ""
    return result
