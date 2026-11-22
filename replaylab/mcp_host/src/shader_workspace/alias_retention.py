"""Find current CBuffer naming references without retaining every old publication.

This is an optional cleanup observation, not a condition for publishing names.
Missing project or replay state disables pruning while leaving publication usable.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .io import read_json_object


_SHA = re.compile(r"^[0-9a-f]{64}$")
_SESSION = re.compile(r"^[0-9a-f]{32}$")
_MAX_WORKSPACES = 256
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("Invalid %s SHA256" % label)
    return value


def _stage(value: Any) -> str:
    token = str(value or "").lower()
    return "fragment" if token == "pixel" else token


def _workspace_candidate(path: str | Path, original: str, stage: str) -> str | None:
    # Read one current manifest, never revision directories or audit receipts.
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Current Shader workspace directory is unavailable")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Current Shader workspace manifest is redirected")
    manifest = read_json_object(manifest_path, _MAX_MANIFEST_BYTES, "workspace manifest")
    shader = manifest.get("shader") or {}
    if (manifest.get("schema") != "renderdoc-ultra-shader-workspace/v1"
            or not isinstance(shader, Mapping)
            or shader.get("rawSha256") != original or _stage(manifest.get("stage")) != stage):
        raise ValueError("Current Shader workspace identity does not match")
    candidate = manifest.get("latestCompiled")
    if candidate is None:
        return None
    if not isinstance(candidate, Mapping):
        raise ValueError("Current Shader candidate metadata is invalid")
    return _sha(candidate.get("rawSha256"), "current workspace candidate")


def _project_references(service: Any, original: str, stage: str) -> tuple[set[str], set[str]]:
    # These existing project readers validate/migrate one in-memory snapshot.
    # Only active heads and the project's selected executable fallbacks count.
    manifest = service._load(persist_migrations=False)
    heads = service._active_heads_state(manifest)["workspaces"]
    fallback_ids = service._workspace_restore_fallbacks(manifest)
    selected_ids = {head["knowledgeId"] for head in heads.values()} | set(fallback_ids)
    records = manifest["knowledge"]["workspaces"]
    protected: set[str] = set()
    workspaces: set[str] = set()
    for record in records:
        if (record.get("knowledgeId") not in selected_ids
                or record.get("rawSha256") != original or _stage(record.get("stage")) != stage):
            continue
        candidate = record.get("compiledCandidate")
        if candidate is not None:
            if not isinstance(candidate, Mapping):
                raise ValueError("Current project Shader candidate is invalid")
            protected.add(_sha(candidate.get("rawSha256"), "project candidate"))
        # A current head can have a newer saved candidate which has not been
        # registered yet. Protect that one current manifest as well.
        if record.get("workspacePath"):
            path = Path(str(record["workspacePath"])).expanduser()
            # Project restore uses immutable knowledge artifacts after a local
            # working copy is moved/deleted. A missing historical path is not
            # an active editor and must not disable cleanup forever.
            if path.exists() or path.is_symlink():
                workspaces.add(str(path))
    if len(workspaces) > _MAX_WORKSPACES:
        raise ValueError("Current Shader workspace reference count exceeds the limit")
    return protected, workspaces


def collect_alias_retention(
    workspace_root: str | Path,
    *,
    captured_shader_sha256: str,
    stage: str,
    current_shader_sha256s: Iterable[str],
    capture_id: str,
    capture_session_id: str,
    replacement_state_loader: Callable[[], Mapping[str, Any]],
    project_service: Any = None,
) -> dict[str, Any]:
    """Return Host-observed protection, or a nonfatal cleanup deferral.

    The loader must query ``get_shared_shader_replacements`` for the expected
    capture/session with ``include_all_capture_catalogs=True``. A current native
    owner is authoritative; old restore results, lastApply, events, persistent
    live-edit authorizations and transaction receipts are not live owners.

    The caller must collect this snapshot immediately before its publication
    transaction. It supplies all incoming scopes, including display mirrors.
    This does not authorize pruning unrelated captured Shader lineages.
    """
    protected: set[str] = set()
    result: dict[str, Any] = {
        "schema": "bread-cbuffer-alias-retention/v1",
        "complete": False,
        "protectedShaderRawSha256": [],
    }
    try:
        original = _sha(captured_shader_sha256, "captured Shader")
        canonical_stage = _stage(stage)
        if not canonical_stage:
            raise ValueError("Shader stage is unavailable")
        _sha(capture_id, "capture")
        if not isinstance(capture_session_id, str) or not _SESSION.fullmatch(capture_session_id):
            raise ValueError("Capture session is unavailable")
        protected.add(original)
        for digest in current_shader_sha256s:
            if digest:
                protected.add(_sha(digest, "incoming Shader"))
        paths = {str(Path(workspace_root).expanduser())}
        if project_service is not None:
            project_candidates, project_paths = _project_references(project_service, original, canonical_stage)
            protected.update(project_candidates)
            paths.update(project_paths)
        for path in paths:
            candidate = _workspace_candidate(path, original, canonical_stage)
            if candidate:
                protected.add(candidate)

        state = replacement_state_loader()
        rows = state.get("replacements") if isinstance(state, Mapping) else None
        if (not isinstance(state, Mapping)
                or state.get("schema") != "renderdoc-ultra-shared-shader-replacement-list/v1"
                or state.get("schemaVersion") != 1
                or state.get("captureId") != capture_id
                or state.get("captureSessionId") != capture_session_id
                or state.get("originalShadersPreserved") is not True
                or state.get("replayMutation") is not False
                or not isinstance(rows, list) or len(rows) > 256
                or type(state.get("count")) is not int or state["count"] != len(rows)):
            raise ValueError("Current Shader replacement registry is unavailable or inconsistent")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("Current Shader replacement entry is invalid")
            if row.get("originalRawSha256") != original or _stage(row.get("stage")) != canonical_stage:
                continue
            if (row.get("captureSessionId", capture_session_id) != capture_session_id
                    or row.get("resourceScoped") is not True or row.get("eventScoped") is not False
                    or row.get("originalShaderPreserved") is not True):
                raise ValueError("Current Shader replacement ownership is inconsistent")
            protected.add(_sha(row.get("candidateRawSha256"), "live replacement"))
        result["complete"] = True
    except Exception as error:
        # Cleanup is best effort. A stale registry must not turn an otherwise
        # valid alias publication into an Agent blocker.
        result["diagnostic"] = (" ".join(str(error).split()) or type(error).__name__)[:400]
    result["protectedShaderRawSha256"] = sorted(protected)
    return result
