"""Native file callbacks restricted to controller-created working artifacts."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import stat
import tempfile

from .errors import PolicyError

AGENT_WORKSPACE_ROOT_ENV = "REPLAYLAB_AGENT_WORKSPACE_ROOT"
MAX_WORKING_TEXT_BYTES = 4 * 1024 * 1024
WORKING_FILE_OBSERVATION_SCHEMA = "replaylab-agent-working-file-observation/v1"
_RESERVED = {"mcp.json", "settings.json", "config.toml", "agents.md", "claude.md", "gemini.md"}


class OwnedWorkspace:
    def __init__(self, root: Path, *, writable: bool = False) -> None:
        supplied = Path(root)
        if not supplied.is_absolute() or not supplied.is_dir():
            raise PolicyError("native working directory must be controller-created and absolute")
        self._no_links(supplied)
        if supplied.resolve() != supplied:
            raise PolicyError("native working directory must be canonical")
        self.root, self.writable = supplied, bool(writable)

    @staticmethod
    def _no_links(path: Path) -> None:
        for item in (path, *path.parents):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PolicyError("native artifact path contains a symlink or reparse point")

    def path(self, value: str, *, write: bool = False) -> Path:
        if not isinstance(value, str) or not value or "\0" in value:
            raise PolicyError("native file must name an ordinary working artifact")
        if write and not self.writable:
            raise PolicyError("this run grants read-only native working artifacts")
        supplied = Path(value)
        if ".." in supplied.parts:
            raise PolicyError("native artifact must stay inside the current run")
        target = supplied if supplied.is_absolute() else self.root / supplied
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise PolicyError("native artifact belongs outside the current run") from error
        if not relative.parts or any(part.startswith(".") or part.rstrip(" .") != part or part.casefold() in _RESERVED or ":" in part
                                     for part in relative.parts):
            raise PolicyError("native artifact cannot name a launch or configuration file")
        self._no_links(target)
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root):
            raise PolicyError("native artifact is redirected outside the current run")
        if any(part.startswith(".") or part.rstrip(" .") != part or part.casefold() in _RESERVED or ":" in part
               for part in resolved.relative_to(self.root).parts):
            raise PolicyError("native artifact resolves to a launch or configuration file")
        if target.exists():
            info = target.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PolicyError("native artifact must be a regular file with one link")
        return target

    def read(self, value: str, line: int | None = None, limit: int | None = None,
             *, observation: dict | None = None) -> str:
        target = self.path(value)
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_size > MAX_WORKING_TEXT_BYTES:
                raise PolicyError("native artifact exceeds 4 MiB")
            raw = stream.read(MAX_WORKING_TEXT_BYTES + 1)
            self.path(value)
            current = target.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PolicyError("native artifact changed while reading")
        if len(raw) > MAX_WORKING_TEXT_BYTES or b"\0" in raw:
            raise PolicyError("native artifact must be NUL-free text up to 4 MiB")
        text = raw.decode("utf-8", errors="strict")
        byte_offset = 0
        if line is None and limit is None:
            result = text
        else:
            first = 1 if line is None else line
            if isinstance(first, bool) or not isinstance(first, int) or first < 1:
                raise PolicyError("line must be a positive one-based integer")
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
                raise PolicyError("limit must be a positive line count")
            lines = text.splitlines(keepends=True)
            byte_offset = len("".join(lines[:first - 1]).encode("utf-8"))
            result = "".join(lines[first - 1:] if limit is None else lines[first - 1:first - 1 + limit])
        returned_bytes = len(result.encode("utf-8"))
        self._record_observation(observation, target, raw, "read", coverage={
            "byteOffset": byte_offset, "returnedBytes": returned_bytes,
            "complete": byte_offset == 0 and returned_bytes == len(raw),
        })
        return result

    def write(self, value: str, content: str, *, observation: dict | None = None) -> None:
        target = self.path(value, write=True)
        if not isinstance(content, str) or "\0" in content:
            raise PolicyError("native artifact content must be NUL-free UTF-8 text")
        raw = content.encode("utf-8", errors="strict")
        if len(raw) > MAX_WORKING_TEXT_BYTES:
            raise PolicyError("native artifact exceeds 4 MiB")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path(value, write=True)
        descriptor, temporary = tempfile.mkstemp(prefix="bread-draft-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self.path(value, write=True)
            os.replace(temporary, target)
            self._record_observation(observation, target, raw, "write")
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _observation(target: Path, raw: bytes, action: str) -> dict:
        """Describe the successful working-file operation, never its content."""
        return {"schema": WORKING_FILE_OBSERVATION_SCHEMA,
                "scope": "owned-workspace", "action": action,
                "basename": target.name, "byteSize": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "workingCopy": True, "replayAuthority": False}

    @classmethod
    def _record_observation(cls, observation: dict | None, target: Path, raw: bytes, action: str,
                            *, coverage: dict | None = None) -> None:
        if observation is not None:
            try:
                value = cls._observation(target, raw, action)
                value["coverage"] = coverage if coverage is not None else {
                    "byteOffset": 0, "returnedBytes": len(raw), "complete": True,
                }
                observation.update(value)
            except Exception:
                # Optional metadata must never turn completed file I/O into
                # failure. The callback publishes only a complete observation.
                pass


__all__ = ["OwnedWorkspace", "AGENT_WORKSPACE_ROOT_ENV"]
