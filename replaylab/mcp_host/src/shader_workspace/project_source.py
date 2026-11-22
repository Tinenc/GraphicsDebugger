"""Managed source provenance for direct reuse of verified project Shader source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .errors import ManifestValidationError


PROJECT_SOURCE_SCHEMA = "replaylab-verified-project-shader-source/v1"


def can_import_project_source(status: Mapping[str, Any]) -> bool:
    """An empty source or untouched captured GLSL can receive the project edit.

    Native OpenGL workspaces already contain their captured text at creation.
    That baseline is not a user edit. Never replace edited or compiled source.
    The workspace transaction checks the file bytes again before writing.
    """
    source = status.get("source") or {}
    baseline = status.get("baseline") or {}
    return source.get("present") is False or bool(
        source.get("present") is True
        and source.get("format") == "glsl"
        and source.get("origin") == "captured-native-source"
        and baseline.get("kind", "").upper() == "GLSL"
        and source.get("currentSha256") == baseline.get("rawSha256")
        and status.get("revision") == 0
        and status.get("latestCompiled") is None
    )


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_receipt(bundle: Mapping[str, Any], input_path: str) -> dict[str, Any]:
    receipt = {key: value for key, value in bundle.items() if key != "sourceText"}
    receipt["inputPath"] = input_path
    receipt["receiptSha256"] = _digest(receipt)
    return receipt


def validate_project_source(root: Path, manifest: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the immutable imported input; later user edits remain free."""
    receipt = source.get("projectSource")
    if not isinstance(receipt, Mapping):
        raise ManifestValidationError("project source provenance is missing")
    value = dict(receipt)
    digest = value.pop("receiptSha256", None)
    language = value.get("sourceFormat", "hlsl")
    expected_language = (manifest.get("routePlan") or {}).get("editableFormat", "hlsl")
    if (value.get("schema") != PROJECT_SOURCE_SCHEMA or digest != _digest(value)
            or language not in {"hlsl", "glsl"} or language != expected_language
            or value.get("sourceSha256") != source.get("sha256")
            or value.get("sourceEntryPoint") != source.get("entryPoint")
            or value.get("originalRawSha256") != manifest["baseline"]["rawSha256"]
            or value.get("baselineAbiSha256") != manifest["baseline"].get("abiSha256")
            or value.get("targetProfile") != manifest.get("targetProfile")
            or value.get("stage") != manifest.get("stage")
            or value.get("entryPoint") != manifest.get("entryPoint")
            or value.get("applied") is not False
            or value.get("requiresCompile") is not True
            or value.get("requiresLiveAbiRevalidation") is not True
            or value.get("semanticEquivalenceVerified") is not False):
        raise ManifestValidationError("project source identity/provenance changed")
    for key, length in (("projectId", 32), ("knowledgeId", 32), ("sourceSha256", 64), ("originWorkspaceId", 32),
                        ("originalRawSha256", 64), ("baselineAbiSha256", 64)):
        item = value.get(key)
        if not isinstance(item, str) or len(item) != length or any(c not in "0123456789abcdef" for c in item):
            raise ManifestValidationError("project source %s is invalid" % key)
    relative = "source-inputs/project-%s.%s" % (value["sourceSha256"], language)
    path = Path(root) / relative
    if (value.get("inputPath") != relative or path.parent.is_symlink() or path.is_symlink()
            or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024
            or hashlib.sha256(path.read_bytes()).hexdigest() != value["sourceSha256"]):
        raise ManifestValidationError("project source immutable input changed")
    return dict(receipt)


def archive_superseded_project_source_gate(
    workspace_path: str | Path, *, restored_record: Mapping[str, Any] | None = None,
    applied_item: Mapping[str, Any] | None = None,
) -> str | None:
    """Detach only an old draft pointer after an exact managed source import.

    Sessions, proposals and their signed reviews remain untouched. No review
    or execution permission is transferred to the imported source: normal
    compile and live ABI checks still apply. Running this at physical admission
    also recovers an earlier import which stopped before its first compile.
    The internal restore caller may instead supply its committed project record
    and successful apply item. That only retires a saved draft whose actual
    proposal is the same source already restored, never a different proposal.
    """
    from .io import atomic_write_bytes, atomic_write_json, is_within
    from .source_observation import source_observation_transaction
    from .workspace import _WorkspaceFileLock

    root = Path(workspace_path).resolve()
    gate_path = root / "ai" / "organize-code-gate.json"
    if not gate_path.is_file():
        return None

    def read(path: Path, limit: int = 4 * 1024 * 1024) -> bytes:
        if (path.is_symlink() or not is_within(root, path.resolve())
                or path.stat().st_size > limit):
            raise ValueError("project source history path is invalid")
        return path.read_bytes()

    # Organization begin/source mutations take this observation lock before
    # exposing a new pointer. The workspace lock also serializes compilation.
    with source_observation_transaction(root), _WorkspaceFileLock(root):
        try:
            manifest_path = root / "manifest.json"
            manifest_bytes, gate_bytes = read(manifest_path), read(gate_path)
            manifest, gate = json.loads(manifest_bytes), json.loads(gate_bytes)
            source = manifest.get("source") or {}
            source_path = root / str(source.get("path", ""))
            source_bytes = read(source_path, 64 * 1024 * 1024)
            current = hashlib.sha256(source_bytes).hexdigest()
            extra_snapshots: list[tuple[Path, bytes]] = []
            applied_source = restored_record is not None or applied_item is not None
            if applied_source:
                if not isinstance(restored_record, Mapping) or not isinstance(applied_item, Mapping):
                    return None
                latest = manifest.get("latestCompiled") or {}
                identity = restored_record.get("identity") or {}
                knowledge_id = str(restored_record.get("knowledgeId") or "")
                if (applied_item.get("status") != "applied"
                        or re.fullmatch(r"[0-9a-f]{32}", str(applied_item.get("replacementId") or "")) is None
                        or applied_item.get("workspaceId") != manifest.get("workspaceId")
                        or Path(str(applied_item.get("workspacePath") or "")).resolve() != root
                        or (restored_record.get("managedWorkspace") or {}).get("workspaceId") != manifest.get("workspaceId")
                        or re.fullmatch(r"[0-9a-f]{32}", knowledge_id) is None
                        or identity.get("rawSha256") != manifest["baseline"]["rawSha256"]
                        or identity.get("stage") != manifest.get("stage")
                        or identity.get("entryPoint", "") != manifest.get("entryPoint", "")
                        or restored_record.get("sourceSha256") != current
                        or latest.get("sourceSha256") != current
                        or latest.get("revision") != restored_record.get("candidateRevision")
                        or latest.get("rawSha256") != restored_record.get("candidateRawSha256")):
                    return None
                candidate_path = root / str(latest.get("blobPath") or "")
                candidate_bytes = read(candidate_path, 64 * 1024 * 1024)
                if hashlib.sha256(candidate_bytes).hexdigest() != latest["rawSha256"]:
                    return None
                extra_snapshots.append((candidate_path, candidate_bytes))
                receipt = {"knowledgeId": knowledge_id, "receiptSha256": ""}
            else:
                if source.get("origin") != "verified-project-source":
                    return None
                receipt = validate_project_source(root, manifest, source)
                if current != receipt["sourceSha256"]:
                    return None  # Unpublished canonical edits retain their draft.
            organization_id = gate.get("organizationId", "")
            if (gate.get("schema") != "renderdoc-ai-shader-organization-gate/v1"
                    or re.fullmatch(r"[0-9a-f]{32}", organization_id) is None
                    or gate.get("workspaceId") != manifest.get("workspaceId")):
                return None
            session_path = root / "ai" / "organization-sessions" / organization_id / "session.json"
            session_bytes = read(session_path)
            session = json.loads(session_bytes)
            if (session.get("schema") != "renderdoc-ai-shader-organization-session/v1"
                    or session.get("organizationId") != organization_id
                    or session.get("workspaceId") != manifest.get("workspaceId")
                    or session.get("originalRawSha256") != manifest["baseline"]["rawSha256"]):
                return None
            if applied_source:
                if (gate.get("decision") != "editable-draft"
                        or gate.get("proposalSourceSha256") != current
                        or (gate.get("finalBinding") or {}).get("sourceSha256") != current
                        or session.get("syncedDraftSourceSha256") != current):
                    return None
                proposal_path = root / str(session.get("proposalSourcePath") or "")
                if not is_within(session_path.parent, proposal_path.resolve()):
                    return None
                proposal_bytes = read(proposal_path, 64 * 1024 * 1024)
                if proposal_bytes != source_bytes:
                    return None  # Keep a newer/different unfinished proposal.
                extra_snapshots.append((proposal_path, proposal_bytes))
            else:
                owned = {row.get(key) for row in (gate, session) for key in (
                    "baselineSourceSha256", "proposalSourceSha256", "syncedDraftSourceSha256")}
                owned.add((gate.get("finalBinding") or {}).get("sourceSha256"))
                if current in owned or not any(re.fullmatch(r"[0-9a-f]{64}", str(value or "")) for value in owned):
                    return None  # A new draft based on this imported source owns it.
        except (OSError, ValueError, KeyError, TypeError, ManifestValidationError):
            return None

        history = root / "ai" / "organization-source-history"
        if history.is_symlink() or not is_within(root, history.resolve()):
            raise ManifestValidationError("organization source history path is redirected")
        history.mkdir(exist_ok=True)
        gate_sha = hashlib.sha256(gate_bytes).hexdigest()
        archive = history / (organization_id + "-" + gate_sha + ".json")
        if archive.exists() and read(archive) != gate_bytes:
            raise ManifestValidationError("archived organization pointer changed")
        atomic_write_bytes(archive, gate_bytes)
        atomic_write_json(history / (organization_id + "-" + gate_sha + "-replacement.json"), {
            "schema": "replaylab-project-source-draft-history/v1", "organizationId": organization_id,
            "gateSha256": gate_sha, "sourceSha256": current,
            "projectSourceReceiptSha256": receipt["receiptSha256"],
            "knowledgeId": receipt["knowledgeId"], "reviewTransferred": False,
            "currentSourceAlreadyApplied": applied_source,
            "candidateRawSha256": latest["rawSha256"] if applied_source else None,
            "requiresCompile": True, "requiresLiveAbiRevalidation": True})
        # Keep the pointer if a writer changed any participating source/session
        # after inspection; the archive alone never grants physical admission.
        if (read(manifest_path) != manifest_bytes or read(gate_path) != gate_bytes
                or read(session_path) != session_bytes or read(source_path, 64 * 1024 * 1024) != source_bytes
                or any(read(path, 64 * 1024 * 1024) != content for path, content in extra_snapshots)):
            return None
        gate_path.unlink()
        return str(archive)
