"""Project-local sharing of optional source-bound navigation descriptions."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .io import atomic_write_bytes


MAX_BYTES = 256 * 1024


def _path(root: Path, original: str, source: str) -> Path:
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (original, source)):
        raise ValueError("Invalid Shader overview source identity")
    return root / "derived" / "shader-overviews" / original / (source + ".json")


def _safe(root: Path, path: Path) -> None:
    path.resolve().relative_to(root.resolve())
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Shader overview storage is redirected")
        if current == current.parent:
            raise ValueError("Shader overview storage escapes its root")
        current = current.parent


def _read(root: Path, path: Path, original: str, source: str) -> bytes | None:
    _safe(root, path)
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Shader overview exceeds its storage limit")
    value = json.loads(raw.decode("utf-8"))
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("stages"), list) or len(value["stages"]) > 256):
        raise ValueError("Invalid Shader overview metadata")
    if value.get("sourceSha256") != source or value.get("originalRawSha256", original) != original:
        return None
    return raw


def publish_overview(project: Path, workspace: Path, original: str, source: str) -> bool:
    project, workspace = Path(project).resolve(), Path(workspace).resolve()
    target = _path(project, original, source)
    raw = _read(workspace, workspace / "ai" / "semantic-overview.json", original, source)
    if raw is None:
        return False
    _safe(project, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, raw)
    return True


def restore_overview(project: Path, workspace: Path, original: str, source: str,
                     *, origin_workspace: Path | None = None,
                     origin_workspace_id: str | None = None) -> bool:
    project, workspace = Path(project).resolve(), Path(workspace).resolve()
    raw = _read(project, _path(project, original, source), original, source)
    if raw is None and origin_workspace is not None:
        # Legacy/manual publications may predate the shared sidecar store.
        # Carry their existing navigation with its ORIGINAL source hash/review:
        # the editor reanchors it, and never claims a fresh semantic review.
        origin = Path(origin_workspace)
        if origin.is_symlink():
            return False
        origin = origin.resolve()
        manifest_path = origin / "manifest.json"
        _safe(origin, manifest_path)
        if manifest_path.is_file() and manifest_path.stat().st_size <= 8 * 1024 * 1024:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (isinstance(manifest, dict) and manifest.get("workspaceId") == origin_workspace_id
                    and (manifest.get("baseline") or {}).get("rawSha256") == original):
                path = origin / "ai" / "semantic-overview.json"
                _safe(origin, path)
                if path.is_file() and path.stat().st_size <= MAX_BYTES:
                    overview = json.loads(path.read_text(encoding="utf-8"))
                    historical_source = overview.get("sourceSha256") if isinstance(overview, dict) else None
                    if isinstance(historical_source, str) and re.fullmatch(r"[0-9a-f]{64}", historical_source):
                        raw = _read(origin, path, original, historical_source)
    if raw is None:
        return False
    target = workspace / "ai" / "semantic-overview.json"
    _safe(workspace, target)
    if target.exists():
        try:
            with target.open("rb") as stream:
                previous_raw = stream.read(MAX_BYTES + 1)
            if len(previous_raw) > MAX_BYTES:
                return False
            previous = json.loads(previous_raw.decode("utf-8"))
            previous_source = previous.get("sourceSha256") if isinstance(previous, dict) else None
            # Replace only a known old revision after the project source import.
            # Current or unrecognised local descriptions remain the editor's work.
            if (not isinstance(previous, dict) or previous.get("version") != 1
                    or not isinstance(previous_source, str)
                    or re.fullmatch(r"[0-9a-f]{64}", previous_source) is None
                    or previous_source == source
                    or previous.get("originalRawSha256", original) != original):
                return False
            if target.read_bytes() != previous_raw:
                return False
        except (ValueError, OSError):
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, raw)
    return True
