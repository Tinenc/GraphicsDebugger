"""Read captured Shader reference data or optional validated workspace inputs.

These are observations of local workspace artifacts, never model attestations.
The capture-compatible source and its semantic names remain separate from the
Unity adaptation. No compilation, naming publication or replay happens here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "bread-unity-organized-sources/v1"
_STAGES = {"vs": "vertex", "hs": "hull", "ds": "domain", "gs": "geometry",
           "ps": "fragment", "fs": "fragment", "cs": "compute", "as": "task", "ms": "mesh",
           "tess_ctrl": "hull", "tess_eval": "domain", "pixel": "fragment",
           "raygeneration": "raygen", "raygen": "raygen", "intersection": "intersection",
           "anyhit": "anyhit", "closesthit": "closesthit", "miss": "miss", "callable": "callable"}
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def stage_name(value: Any) -> str:
    value = str(value or "").lower()
    value = _STAGES.get(value, value)
    if value not in set(_STAGES.values()):
        raise ValueError("Unknown captured shader stage")
    return value


def _read(root: Path, value: str, limit: int = 16 * 1024 * 1024) -> tuple[Path, bytes]:
    from .shader_organization_review import _read_bytes
    return _read_bytes(root, value, limit)


def _object(root: Path, relative: str) -> tuple[dict, str]:
    _, raw = _read(root, relative)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Workspace evidence is not a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(label + " is not a SHA-256 identity")
    return value


def _dependency(path: str) -> tuple[Path, bytes]:
    # Only call for an entry in the compiler-validated include closure. External
    # explicit includes are supported by the existing Workspace compiler too.
    from .shader_workspace.source_documents import _read as read_document
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("Compiled include path must be absolute")
    return candidate, read_document(candidate)


def _path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _draft_status(root: Path, status: Mapping[str, Any], organization_id: str, expected_sha256: str | None = None) -> dict:
    """Read an explicitly selected proposal without promoting/applying it."""
    relative = "ai/organization-sessions/" + organization_id + "/session.json"
    session, _ = _object(root, relative)
    if (session.get("organizationId") != organization_id
            or session.get("workspaceId") != status.get("workspaceId")
            or session.get("originalRawSha256") != (status.get("baseline") or {}).get("rawSha256")
            or stage_name(session.get("stage")) != stage_name(status.get("stage"))
            or str(session.get("entryPoint") or "") != str(status.get("entryPoint") or "")):
        raise ValueError("Selected organization draft belongs to a different Shader workspace/program")
    path, raw = _read(root, str(session.get("proposalSourcePath") or ""), 4 * 1024 * 1024)
    if path.parent != root / "ai" / "organization-sessions" / organization_id:
        raise ValueError("Selected proposal escapes its organization session")
    sha = hashlib.sha256(raw).hexdigest()
    recorded_sha = _digest(session.get("proposalSourceSha256"), "Organization proposal")
    if sha != (expected_sha256 or recorded_sha):
        raise ValueError("Selected organization proposal changed since its saved receipt; read its current bytes and pass draft_sha256 to select an edited reference explicitly")
    return {**status, "source": {**(status.get("source") or {}), "present": True,
        "path": str(path), "currentSha256": sha, "format": session.get("sourceFormat", "hlsl"),
        "proposalReceiptCurrent": sha == recorded_sha}}


def _compile_evidence(status: Mapping[str, Any], sha: str, dependency_digest: str) -> dict:
    compiled = status.get("latestCompiled") or {}
    current = bool(compiled.get("sourceSha256") == sha and _SHA.fullmatch(str(compiled.get("rawSha256") or ""))
        and status.get("sourceDependenciesCurrent") is True
        and str((status.get("source") or {}).get("dependenciesDigest") or "") == dependency_digest)
    return {"sourceSha256": compiled.get("sourceSha256"), "candidateRawSha256": compiled.get("rawSha256"),
        "current": current, "compiled": current, "abiCompatible": current and status.get("abiCompatible") is True}


def collect_organized_sources(workspace_paths: Sequence[str],
                              status_loader: Callable[[str], Mapping[str, Any]],
                              identity_loader: Callable[..., Mapping[str, Any]] | None = None,
                              *, require_replay_validation: bool = True,
                              organization_ids: Mapping[str, str] | None = None,
                              draft_sha256: Mapping[str, str] | None = None) -> dict:
    """Resolve current source, validation and channel-merged display evidence.

    Missing preparation is returned as actionable gaps so an Agent can organize
    the same workspace and retry. A stale or unrelated source is never relabelled
    as ready. The caller may bind the returned stages to a captured draw bundle.
    """
    if (not isinstance(workspace_paths, (list, tuple)) or len(workspace_paths) > 64
            or any(not isinstance(p, str) or not p for p in workspace_paths)):
        raise ValueError("workspace_paths must contain at most 64 workspace paths")
    if organization_ids is not None and not isinstance(organization_ids, Mapping):
        raise ValueError("organization_ids must map selected workspace paths to organization IDs")
    selections = {}
    selected_roots = {_path_key(path) for path in workspace_paths}
    for requested, organization_id in (organization_ids or {}).items():
        if (not isinstance(requested, str) or not isinstance(organization_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", organization_id)):
            raise ValueError("Organization draft selection is invalid")
        key = _path_key(requested)
        if key not in selected_roots or key in selections:
            raise ValueError("Organization draft must select exactly one of the requested workspaces")
        selections[key] = organization_id
    if selections and require_replay_validation:
        raise ValueError("Organization proposals are reference-only; strict validation uses the published source")
    if draft_sha256 is not None and not isinstance(draft_sha256, Mapping):
        raise ValueError("draft_sha256 must map selected draft workspace paths to observed SHA-256 values")
    draft_hashes = {}
    for requested, sha in (draft_sha256 or {}).items():
        if not isinstance(requested, str) or _path_key(requested) not in selections:
            raise ValueError("Draft SHA-256 must refer to an explicitly selected organization")
        key = _path_key(requested)
        if key in draft_hashes:
            raise ValueError("Duplicate draft SHA-256 selection")
        draft_hashes[key] = _digest(sha, "Selected draft")
    sources, gaps, seen = [], [], set()
    for requested in workspace_paths:
        root = Path(requested).expanduser().resolve(strict=True)
        if str(root) in seen:
            continue
        seen.add(str(root))
        status = status_loader(str(root))
        canonical_status = status
        organization_id = selections.get(_path_key(root))
        if organization_id:
            status = _draft_status(root, status, organization_id, draft_hashes.get(_path_key(root)))
        source = status.get("source") or {}
        stage = stage_name(status.get("stage"))
        row: dict[str, Any] = {"workspacePath": str(root), "workspaceId": status.get("workspaceId"),
            "stage": stage, "api": (status.get("capture") or {}).get("api"),
            "entryPoint": status.get("entryPoint"), "ready": False, "gaps": []}

        def gap(code: str, tool: str, detail: str = "") -> None:
            item = {"code": code, "workspacePath": str(root), "stage": stage, "tool": tool}
            if detail:
                item["detail"] = detail[:1000]
            row["gaps"].append(item)
            gaps.append(item)

        row["rawSha256"] = _digest((status.get("baseline") or {}).get("rawSha256"), "Original shader")
        if not source.get("present") or not source.get("path"):
            gap("organized-source-missing", "begin_shader_workspace_organization" if require_replay_validation
                else "get_current_shader_representation")
            sources.append(row)
            continue
        path, raw = _read(root, str(source["path"]), 4 * 1024 * 1024)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source.get("currentSha256"):
            raise ValueError("Shader source changed during Unity source preparation")
        row.update(sourcePath=str(path), sourceSha256=digest, language=source.get("format"))
        row["evidenceScope"] = {"validationScope": status.get("validationScope", "saved-workspace"),
            "currentReplayValidated": status.get("currentReplayValidated") is True,
            "executionAuthority": False}
        row["sourceSelection"] = {"kind": "organization-draft" if organization_id else "saved-source"}
        if organization_id:
            row["sourceSelection"]["organizationId"] = organization_id
            row["sourceSelection"]["proposalReceiptCurrent"] = source["proposalReceiptCurrent"]
        if not require_replay_validation:
            # A Unity port consumes this program as reference data. It never
            # installs these bytes into the captured pipeline. Resolve includes
            # directly, without requiring a compiler, ABI or replay observation.
            from .shader_workspace.source_documents import capture_source_unit
            unit = (capture_source_unit(path, allow_incomplete=True)
                    if str(source.get("format", "")).lower() in {"hlsl", "glsl"}
                    or path.suffix.lower() in {".hlsl", ".hlsli", ".glsl", ".fx", ".vert", ".frag", ".comp", ".geom", ".tesc", ".tese"}
                    else {"documents": [], "digest": ""})
            row["dependencyFiles"] = [{"path": document["path"], "sha256": document["sha256"]}
                for document in unit["documents"] if Path(document["path"]) != path]
            row["validation"] = {"mode": "capture-reference", "replayValidated": False,
                "sourceDependenciesDigest": unit["digest"],
                "compileEvidence": _compile_evidence(canonical_status, digest, unit["digest"])}
            row["validation"]["passedChecks"] = [name for name, passed in (
                ("compilation", row["validation"]["compileEvidence"]["compiled"]),
                ("abi", row["validation"]["compileEvidence"]["abiCompatible"])) if passed]
            row["warnings"] = [{"code": "reference-include-unresolved", "detail": str(error)}
                for error in unit.get("errors", [])]
            row["namingDelivery"] = {"required": False, "status": "reference-only"}
            try:
                semantic, semantic_hash = _object(root, "ai/cbuffer-name-publication.json")
                if semantic.get("semanticSourceSha256") == digest:
                    row.update(semanticPublication=semantic, semanticPublicationSha256=semantic_hash)
            except (OSError, ValueError):
                pass
            if identity_loader is not None:
                try:
                    identity = identity_loader(str(root), status=status)
                    if (identity.get("rawSha256") != row["rawSha256"]
                            or stage_name(identity.get("stage")) != stage):
                        raise ValueError("Display reflection belongs to another captured Shader")
                    row.update(cbufferIdentity=dict(identity), blocks=identity.get("blocks", []),
                        activeDisplayBlocks=identity.get("activeDisplayBlocks", []),
                        activeDisplayShaderRawSha256=identity.get("activeDisplayShaderRawSha256") or row["rawSha256"])
                except (OSError, ValueError, RuntimeError) as error:
                    row["displayIdentityDiagnostic"] = str(error)[:1000]
            row["ready"] = True
            display_gaps = []
            preparation_gaps = [{"code": "organization-proposal-not-published", "tool": "finish_shader_workspace_organization"}]
            if not organization_id:
                try:
                    # Prefer existing complete proof, without running compilers,
                    # replay, organization or another identity-loader IPC here.
                    strict = collect_organized_sources([str(root)], lambda _: status)
                    validated = strict["sources"][0]
                    display_gaps = [gap for gap in strict["gaps"] if gap["code"] == "organized-resource-naming-pending"]
                    preparation_gaps = [gap for gap in strict["gaps"] if gap["code"] != "organized-resource-naming-pending"]
                    if not preparation_gaps:
                        for key in ("cbufferIdentity", "blocks", "activeDisplayBlocks", "activeDisplayShaderRawSha256",
                                    "displayIdentityDiagnostic"):
                            if key in row:
                                validated[key] = row[key]
                        row = validated
                        row.update(ready=True, gaps=[])
                        row["warnings"] = ([{"code": "bread-display-delivery-incomplete", "displayGaps": display_gaps}]
                            if display_gaps else [])
                    elif validated.get("validation", {}).get("organizationId"):
                        row["validation"]["organizationId"] = validated["validation"]["organizationId"]
                except (OSError, ValueError, RuntimeError) as error:
                    preparation_gaps = [{"code": "bread-source-validation-unavailable", "detail": str(error)[:1000]}]
            if not preparation_gaps:
                row["sourcePreparation"] = {"preferredLocation": "bread", "status": "prepared",
                    "fallbackAllowed": True, "missingChecks": [], "displayGaps": display_gaps}
            else:
                row["sourcePreparation"] = {"preferredLocation": "bread", "status": "incomplete",
                    "fallbackAllowed": True, "fallbackLocation": "unity", "missingChecks": preparation_gaps,
                    "nextAction": "prepare-in-bread-or-transfer-with-explicit-tool-diagnostics"}
                row["warnings"].append({"code": "bread-source-preparation-incomplete", "missingChecks": preparation_gaps})
            if verify_source_files([row]):
                raise ValueError("Shader reference data changed during source preparation")
            sources.append(row)
            continue
        compiled = status.get("latestCompiled") or {}
        if (compiled.get("sourceSha256") != digest or status.get("abiCompatible") is not True
                or status.get("sourceDependenciesCurrent") is not True
                or status.get("abiValidationStatus") in {"incompatible", "source-changed", "not-compiled"}):
            gap("organized-source-validation-stale", "finish_shader_workspace_organization")
        row["dependencyFiles"] = []
        dependencies = compiled.get("sourceDependencies") or {}
        # The compiler's include closure is independently read, including local
        # dependencies outside the source directory but inside this workspace.
        for document in dependencies.get("files", dependencies.get("documents", [])):
            if not isinstance(document, Mapping):
                raise ValueError("Malformed compiled include evidence")
            candidate = document.get("sourcePath") or document.get("path") or document.get("resolvedPath")
            expected = document.get("sourceSha256") or document.get("sha256")
            if candidate and expected:
                included, content = _dependency(str(candidate))
                if hashlib.sha256(content).hexdigest() != expected:
                    gap("organized-include-changed", "finish_shader_workspace_organization", str(candidate))
                row["dependencyFiles"].append({"path": str(included), "sha256": expected})
        try:
            gate, gate_hash = _object(root, "ai/organize-code-gate.json")
            if (gate.get("finalSourceSha256") != digest or gate.get("wholeSourceValidated") is not True
                    or gate.get("organizedCompiledAndAbiValidated") is not True
                    or gate.get("finalCandidateRawSha256") != compiled.get("rawSha256")
                    or gate.get("publicationContract") != "validated-source"):
                gap("organized-source-publication-stale", "finish_shader_workspace_organization")
            from .shader_organization_review import publication_review
            reviewed = publication_review(str(root), str(gate.get("organizationId") or ""))
            if not reviewed.get("ready") or reviewed.get("sourceSha256") != digest:
                gap("organized-source-review-pending", "record_shader_organization_review")
            row["validation"] = {"organizationId": gate.get("organizationId"), "gateSha256": gate_hash,
                "candidateRawSha256": compiled.get("rawSha256"), "equivalenceLevel": gate.get("equivalenceLevel"),
                "sourceDependenciesDigest": source.get("dependenciesDigest"), "review": reviewed}
        except (OSError, ValueError, KeyError, TypeError) as error:
            gap("organized-source-publication-missing", "finish_shader_workspace_organization", str(error))
        from .shader_semantic_delivery import delivery_state
        naming = delivery_state(str(root), digest, {"required": False}, source_path=str(path))
        row["namingDelivery"] = naming
        if naming.get("required") and naming.get("status") != "complete":
            gap("organized-resource-naming-pending", "publish_cbuffer_aliases", str(naming.get("reason", "")))
        try:
            semantic, semantic_hash = _object(root, "ai/cbuffer-name-publication.json")
            if semantic.get("semanticSourceSha256") == digest:
                row.update(semanticPublication=semantic, semanticPublicationSha256=semantic_hash,
                    activeDisplayShaderRawSha256=semantic.get("activeDisplayShaderRawSha256"))
        except (OSError, ValueError):
            pass  # A shader without constants does not need a CBuffer publication.
        if identity_loader is not None:
            try:
                identity = identity_loader(str(root), status=status)
                if (identity.get("rawSha256") != row["rawSha256"]
                        or stage_name(identity.get("stage")) != stage):
                    raise ValueError("Display reflection belongs to another captured Shader")
                row["cbufferIdentity"] = dict(identity)
                row["blocks"] = identity.get("blocks", [])
                row["activeDisplayBlocks"] = identity.get("activeDisplayBlocks", [])
                row["activeDisplayShaderRawSha256"] = identity.get("activeDisplayShaderRawSha256") or row["rawSha256"]
            except (OSError, ValueError, RuntimeError) as error:
                # Typed captures remain portable offline. The CBuffer decoder
                # reports a gap if it actually needs missing replacement layout.
                row["displayIdentityDiagnostic"] = str(error)[:1000]
        row["ready"] = not row["gaps"]
        validation = row.get("validation") or {}
        code_ready = not any(gap["code"] != "organized-resource-naming-pending" for gap in row["gaps"])
        validation.update(mode="validated-source", replayValidated=bool(code_ready and
            validation.get("equivalenceLevel") in {"exact-code", "exact-captured-code", "rename-only", "observational-pass"}))
        validation["compileEvidence"] = _compile_evidence(status, digest, str(source.get("dependenciesDigest") or ""))
        validation["passedChecks"] = [name for name, passed in (
            ("compilation", validation["compileEvidence"]["compiled"]),
            ("abi", validation["compileEvidence"]["abiCompatible"]),
            ("organization-review", (validation.get("review") or {}).get("ready") is True),
            ("equivalence", validation["replayValidated"])) if passed]
        row["validation"] = validation
        sources.append(row)
    return {"schema": SCHEMA, "sources": sources, "gaps": gaps,
            "ready": bool(sources) and not gaps, "captureCompatibleSourcesModified": False,
            "sourcePreparationPolicy": "bread-first-unity-fallback"}


def bind_sources_to_bundle(sources: Sequence[Mapping[str, Any]], bundle: Mapping[str, Any],
                           *, require_all: bool = True) -> list[dict]:
    """Require exact stage/program matches; session ResourceIds are not identity."""
    if require_all and any(s.get("status") != "captured" for s in bundle.get("shaders", [])):
        raise ValueError("A bound shader stage has unavailable capture evidence")
    captured = {(stage_name(s.get("stage")), s.get("rawSha256"), str(s.get("entryPoint") or ""))
                for s in bundle.get("shaders", []) if s.get("status") == "captured"}
    selected, keys = [], set()
    for source in sources:
        key = (stage_name(source.get("stage")), source.get("rawSha256"), str(source.get("entryPoint") or ""))
        if key not in captured:
            raise ValueError("Organized Shader does not match a stage of this captured EID")
        def api_name(value):
            # RenderDoc enums and persisted source manifests use both spellings.
            name = str(value or "").rsplit(".", 1)[-1].lower()
            return {"opengles": "opengl", "gles": "opengl", "gl": "opengl", "vk": "vulkan",
                    "direct3d11": "d3d11", "directx11": "d3d11",
                    "direct3d12": "d3d12", "directx12": "d3d12"}.get(name, name)
        if bundle.get("api") and api_name(source.get("api")) != api_name(bundle["api"]):
            raise ValueError("Organized Shader uses a different capture graphics API")
        if key in keys:
            raise ValueError("Multiple organized sources selected for the same captured stage")
        if source.get("ready") is not True:
            raise ValueError("Organized Shader has unresolved validation or naming gaps")
        keys.add(key)
        selected.append(dict(source))
    if require_all and (not captured or captured != keys):
        raise ValueError("Functional restoration requires organized sources for every captured shader stage")
    return selected


def compact_source_evidence(source: Mapping[str, Any], path: str, sha256: str) -> dict:
    """Portable source identity and checks, with full proof in the package manifest."""
    keys = ("workspacePath", "workspaceId", "stage", "api", "rawSha256", "entryPoint", "ready",
            "sourcePath", "sourceSha256", "language", "dependencyFiles", "sourceSelection",
            "semanticPublicationSha256", "evidenceScope", "sourcePreparation")
    result = {key: source[key] for key in keys if key in source}
    validation = source.get("validation") or {}
    result["validation"] = {key: validation[key] for key in (
        "mode", "replayValidated", "organizationId", "gateSha256", "equivalenceLevel", "passedChecks") if key in validation}
    result["evidence"] = {"path": path, "sha256": sha256}
    return result


def verify_source_files(sources: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Cheap final acceptance freshness check, with no toolchain/replay side effect."""
    gaps = []
    for source in sources:
        root = Path(str(source.get("workspacePath", ""))).resolve()
        rows = [{"path": source.get("sourcePath"), "sha256": source.get("sourceSha256")},
                *[{**row, "compiledInclude": True} for row in source.get("dependencyFiles", [])]]
        validation = source.get("validation") or {}
        if validation.get("mode") != "capture-reference":
            rows.append({"path": "ai/organize-code-gate.json", "sha256": validation.get("gateSha256")})
        if source.get("semanticPublicationSha256"):
            rows.append({"path": "ai/cbuffer-name-publication.json", "sha256": source["semanticPublicationSha256"]})
        for row in rows:
            try:
                _, raw = (_dependency(str(row.get("path") or "")) if row.get("compiledInclude") else
                          _read(root, str(row.get("path") or "")))
                if hashlib.sha256(raw).hexdigest() != _digest(row.get("sha256"), "Source evidence"):
                    raise ValueError("Content changed")
            except (OSError, ValueError) as error:
                gaps.append({"code": "organized-source-evidence-changed", "workspacePath": str(root),
                             "path": row.get("path"), "detail": str(error)[:500]})
    return gaps
