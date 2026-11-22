"""Portable capture identity, independent of replay sessions and catalog subsets."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path


def path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _stamp(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@lru_cache(maxsize=32)
def _content(path: str, stamp: tuple) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    if _stamp(Path(path)) != stamp:
        raise ValueError("Capture file changed while reading its source identity")
    return digest.hexdigest()


def capture_source(capture: dict) -> dict:
    """Hash a local RDC once per file change; remote captures keep live identity."""
    path = str(capture.get("path") or "")
    result = {"path": path, "api": capture.get("api", "")}
    if path and Path(path).is_file():
        try:
            key = path_key(path)
            stamp = _stamp(Path(key))
            result.update(contentSha256=_content(key, stamp), byteSize=stamp[2])
        except (OSError, ValueError) as error:
            # A replay can still supply the bound bytes when the original RDC
            # is remote, moved, inaccessible or being rewritten. Identity is
            # optional provenance, never a prerequisite for reading that data.
            result["identityDiagnostic"] = str(error)[:500]
    return result


def same_source(before: dict, after: dict, *, legacy_path: bool = False) -> bool:
    old, new = before.get("contentSha256"), after.get("contentSha256")
    if old:
        return bool(new and old == new and before.get("byteSize") == after.get("byteSize"))
    return bool(legacy_path and before.get("path") and after.get("path")
                and path_key(before["path"]) == path_key(after["path"]))
