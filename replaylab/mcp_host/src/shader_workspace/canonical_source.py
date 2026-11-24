"""Choose existing managed source by captured program, never compiler policy.

Selection is read-only and confers no compile/replay authority. Capture-local
manifests stay with their RDC; shared project knowledge handles another RDC.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
import os
import ntpath
from pathlib import Path
import re

from .catalog import normalize_stage
from .io import canonical_workspace_identity, is_within, read_json_object


def api_family(value):
    token = str(value or "").lower().rsplit(".", 1)[-1].replace(" ", "")
    return {"direct3d11": "d3d11", "direct3d12": "d3d12", "gl": "opengl",
            "openglcore": "opengl", "opengles": "opengl"}.get(token, token)


def capture_path_identity(value):
    """Compare persisted paths by their platform, without rewriting the path."""
    path = str(value or "").strip()
    if not path:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path):
        return path  # Remote capture schemes and case-sensitive paths are opaque.
    if re.match(r"^[a-zA-Z]:[/\\]", path) or path.startswith(("\\\\", "//")):
        return ntpath.normcase(ntpath.normpath(path))
    if path.startswith("/"):
        # A stored Linux path remains case-sensitive, also on a Windows Host.
        import posixpath
        return posixpath.normpath(path)
    return canonical_workspace_identity(path)


@lru_cache(maxsize=128)
def _manifest(path, size, mtime, inode):
    del size, mtime, inode
    return read_json_object(Path(path), 16 * 1024 * 1024, "managed Shader source manifest")


def _manifests(root):
    if not root.is_dir() or root.is_symlink():
        return
    count = 0
    for directory, children, files in os.walk(root, followlinks=False):
        count += 1
        if count > 4096:
            raise RuntimeError("managed Shader source inventory exceeds its bounded size")
        path = Path(directory)
        children[:] = [name for name in children if not (path / name).is_symlink()]
        if "manifest.json" in files:
            children[:] = []  # Never walk source, jobs, AI history or binaries.
            manifest = path / "manifest.json"
            if not manifest.is_symlink():
                yield manifest
        elif len(path.relative_to(root).parts) >= 8:
            children[:] = []


def _same_original(document, capture_path, api, shader):
    capture, original = document.get("capture") or {}, document.get("shader") or {}
    return (capture_path_identity(capture.get("capturePath", "")) == capture_path
            and api_family(capture.get("api")) == api_family(api)
            and original.get("rawSha256") == shader.get("rawSha256")
            and normalize_stage(document.get("stage")) == normalize_stage(shader.get("stage"))
            and document.get("entryPoint", "") == shader.get("entryPoint", "")
            and original.get("resourceId") == shader.get("resourceId"))


def existing_sources(root, *, capture_path, api, shader, reader, registered=()):
    """Find exact same-RDC owners across old catalog and route-version paths."""
    root = Path(root).resolve()
    expected_path = capture_path_identity(capture_path)
    registrations = {}
    for item in registered:
        if isinstance(item, Mapping) and item.get("workspacePath"):
            key = canonical_workspace_identity(item["workspacePath"])
            if key not in registrations or str(item.get("registeredAt") or "") > str(registrations[key].get("registeredAt") or ""):
                registrations[key] = item
    rows, diagnostics = [], []
    for path in _manifests(root):
        matching_original = False
        try:
            info = path.stat()
            manifest = _manifest(str(path), info.st_size, info.st_mtime_ns, info.st_ino)
            if not _same_original(manifest, expected_path, api, shader):
                continue
            matching_original = True
            workspace = path.parent.resolve()
            if not is_within(root, workspace):
                continue
            # Verify current source and original bytes, without ABI, replay,
            # decompilation or loading a compiler registry.
            source_reader = reader() if callable(reader) else reader
            status = source_reader.source_edit_status(workspace)
            if not _same_original(status, expected_path, api, shader):
                raise RuntimeError("original Shader identity changed while reading its source")
            source = status.get("source") or {}
            saved = registrations.get(canonical_workspace_identity(workspace), {})
            if source.get("present") is not True or source.get("format") not in {"hlsl", "glsl"}:
                if saved:
                    raise RuntimeError("registered Shader source is missing or not editable")
                continue
            published_sha = saved.get("registeredSourceSha256")
            dirty = (source.get("currentSha256") != published_sha if published_sha
                     else source.get("currentSha256") != source.get("decompiledSha256"))
            source_path = source.get("path") or (manifest.get("source") or {}).get("path")
            modified_ns = 0
            if source_path:
                actual_source = Path(source_path)
                if not actual_source.is_absolute():
                    actual_source = workspace / actual_source
                if is_within(workspace, actual_source.resolve()) and actual_source.is_file():
                    modified_ns = actual_source.stat().st_mtime_ns
            rows.append({"workspacePath": str(workspace), "workspaceId": status["workspaceId"],
                "sourceSha256": source["currentSha256"], "sourceFormat": source["format"],
                "catalogSha256": status["capture"].get("catalogSha256"),
                "captureId": status["capture"].get("captureId"), "localChanges": dirty,
                "activeHead": saved.get("activeCandidate") is True,
                "registeredAt": str(saved.get("registeredAt") or ""),
                "sourceModifiedNs": modified_ns,
                "knowledgeId": saved.get("knowledgeId")})
        except (OSError, ValueError, RuntimeError) as error:
            if matching_original or canonical_workspace_identity(path.parent) in registrations:
                # A damaged existing owner must not be replaced with a newly
                # reconstructed source at a different catalog/compiler path.
                raise RuntimeError("existing Shader source requires recovery at %s: %s" %
                                   (path.parent, str(error)[:300])) from error
            diagnostics.append({"workspacePath": str(path.parent), "reason": str(error)[:300]})
    return rows, diagnostics


def _registered_time_ns(value):
    """Read legacy date-only and timezone-aware registrations without new fields."""
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}", text):
            text += "-01-01"
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = date.astimezone(timezone.utc) - epoch
        return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    except (ValueError, OverflowError):
        return 0


def source_selection_time_ns(row):
    modified = row.get("sourceModifiedNs")
    modified = modified if type(modified) is int and modified > 0 else 0
    return max(modified, _registered_time_ns(row.get("registeredAt")))


def select_source(rows, *, ui=None):
    """Always choose the newest matching saved source, then HLSL, preserving all.

    Source discovery already checks the original program. Compilation, ABI and
    replay are separate. A selected/dirty old tab never overrides newer disk
    content, and this read never changes or closes its unsaved editor buffer.
    """
    if not rows:
        return None
    chosen = min(rows, key=lambda row: (
        -source_selection_time_ns(row),
        0 if str(row.get("sourceFormat") or "").lower() == "hlsl" else 1,
        0 if row.get("activeHead") else 1,
        canonical_workspace_identity(row["workspacePath"]),
        str(row.get("workspaceId") or ""), str(row.get("sourceSha256") or "")))
    result = {**chosen, "selectionPolicy": "latest-source-hlsl-tiebreak/v1",
              "selectionTimestampNs": source_selection_time_ns(chosen),
              "selectionReason": "current-project-source" if chosen.get("activeHead") else "latest-existing-source"}
    retained = [{key: row.get(key) for key in ("workspaceId", "workspacePath", "sourceSha256")}
                for row in rows if row is not chosen and
                (row.get("sourceSha256"), row.get("sourceFormat")) !=
                (chosen.get("sourceSha256"), chosen.get("sourceFormat"))]
    if retained:
        result["retainedSourceConflicts"] = retained
    return result
