"""Publish one finished source/apply pair without repeating its physical work."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import copy


def preserve_historical_artifacts(project, status):
    """Recover exact archived evidence without constructing executable tools."""
    if status.get("artifactReadOnly") is not True:
        return status
    source, baseline, latest = (status.get(name) or {} for name in ("source", "baseline", "latestCompiled"))
    if not latest or latest.get("sourceSha256") != source.get("currentSha256"):
        return status
    replay = project.replay_service()
    head = replay.find_current_workspace_source_head(
        source_sha256=source["currentSha256"], raw_sha256=baseline["rawSha256"],
        stage=status["stage"], entry_point=status.get("entryPoint", ""),
        workspace_id=status["workspaceId"], capture_id=status["capture"]["captureId"],
        target_profile=status["targetProfile"], baseline_abi_sha256=baseline.get("abiSha256", ""))
    if not isinstance(head, Mapping) or not isinstance(head.get("knowledge"), Mapping):
        return status
    expected = {"sourceSha256": source.get("currentSha256"), "rawSha256": baseline.get("rawSha256"),
        "stage": status.get("stage"), "entryPoint": status.get("entryPoint", ""),
        "workspaceId": status.get("workspaceId"), "captureId": status["capture"].get("captureId"),
        "originResourceId": status["shader"].get("resourceId"), "targetProfile": status.get("targetProfile"),
        "baselineAbiSha256": baseline.get("abiSha256", "")}

    def matches(old):
        candidate = old.get("compiledCandidate") or {}
        return (all(old.get(name) == value for name, value in expected.items())
            and Path(str(old.get("workspacePath") or "")).resolve() == Path(status["workspacePath"]).resolve()
            and old.get("sourceFormat", "hlsl") == source.get("format")
            and candidate.get("sourceSha256") == source["currentSha256"]
            and candidate.get("rawSha256") == latest.get("rawSha256")
            and candidate.get("sourceRevision") == latest.get("revision")
            and candidate.get("targetProfile") == status.get("targetProfile"))

    from .projects import _compiler_binding_identity, _assembly_disassembly_identity, _file_bytes, MAX_SOURCE_BYTES
    import hashlib
    # A prior metadata-only publication may already have made a weaker head.
    # Read original immutable history; never guess argv from a route snapshot.
    manifest = replay._load(persist_migrations=False)
    records = [item for item in manifest["knowledge"]["workspaces"] if matches(item)]
    result = dict(status)
    artifacts = list(status.get("assemblyDisassemblyArtifacts") or [])
    by_role = {_assembly_disassembly_identity(item)["role"]: item for item in artifacts}
    recovered = []
    for record in records:
        old = replay._workspace_knowledge_from_manifest(manifest, record["knowledgeId"])
        changed = False
        if old.get("compilerBinding") is not None:
            clean = _compiler_binding_identity(old["compilerBinding"])
            route_sha = (status.get("routePlan") or {}).get("planSha256")
            if clean["targetProfile"] != status["targetProfile"]:
                raise RuntimeError("archived compiler evidence changed its target profile")
            if route_sha and clean.get("routePlanSha256") != route_sha:
                continue
            current = result.get("compilerBinding")
            if current is not None and _compiler_binding_identity(current) != clean:
                raise RuntimeError("compiler evidence conflicts for the exact saved source/candidate")
            if current is None:
                result["compilerBinding"] = copy.deepcopy(clean)
                changed = True
        for archived in old.get("assemblyDisassemblyArtifacts") or []:
            clean = _assembly_disassembly_identity(archived)
            path = replay._artifact(archived.get("artifactPath"), "archived Shader disassembly")
            raw = _file_bytes(path, MAX_SOURCE_BYTES, "archived Shader disassembly")
            if len(raw) != clean["byteSize"] or hashlib.sha256(raw).hexdigest() != clean["sha256"]:
                raise RuntimeError("archived Shader disassembly content/hash mismatch")
            current = by_role.get(clean["role"])
            if current is not None:
                if _assembly_disassembly_identity(current) != clean:
                    raise RuntimeError("disassembly evidence conflicts for the exact saved source/candidate")
            else:
                restored = {**clean, "path": str(path)}
                artifacts.append(restored)
                by_role[clean["role"]] = restored
                changed = True
        if changed:
            recovered.append(old["knowledgeId"])
    result["assemblyDisassemblyArtifacts"] = artifacts
    if recovered:
        result["historicalPublicationEvidence"] = {
            "knowledgeIds": recovered, "executionAuthority": False,
            "expectedHead": {"headKey": head["headKey"], "generation": head["generation"],
                             "knowledgeId": head["knowledge"]["knowledgeId"]}}
    return result


def recover_finished_binding(workspace, status):
    """Recover optional current documentation, never physical apply authority.

    A failed final publication can leave the completed review and documents on
    disk. Only that exact reviewed source may carry them into the retry; manual
    edits continue to publish source without stale organization documents.
    """
    from .shader_organization_review import _read_bytes, publication_review
    from .shader_workspace.explanations import source_binding_metadata
    import json

    root = Path(workspace).resolve()
    gate_path = root / "ai" / "organize-code-gate.json"
    if not gate_path.exists():
        return None
    try:
        _, raw = _read_bytes(root, str(gate_path), 2 * 1024 * 1024)
        gate = json.loads(raw)
        if not isinstance(gate, Mapping):
            return None
        final = gate.get("finalBinding") or {}
        source = status.get("source") or {}
        candidate = status.get("latestCompiled") or {}
        if not all(isinstance(item, Mapping) for item in (final, source, candidate)):
            return None
        if (gate.get("finishedCurrentDraft") is not True
                or gate.get("publicationContract") != "validated-source"
                or final.get("complete") is not True
                or final.get("sourceSha256") != source.get("currentSha256")
                or final.get("candidateRawSha256") != candidate.get("rawSha256")
                or final.get("revision") != status.get("revision")
                or candidate.get("sourceSha256") != source.get("currentSha256")):
            return None
        review = publication_review(str(root), str(gate.get("organizationId") or ""))
        if review.get("ready") is not True or review.get("sourceSha256") != final["sourceSha256"]:
            return None
        # Legacy unbound documents remain readable elsewhere, but cannot be
        # implicitly attached to a new retry's project head. Flow graph JSON
        # carries its own strict source/revision identity in the project writer.
        for name in ("analysis.md", "flowchart.mmd"):
            artifact = root / "ai" / name
            if artifact.exists() or artifact.is_symlink():
                _, document = _read_bytes(root, str(artifact), 1024 * 1024)
                if source_binding_metadata(root, artifact, document, status).get("currentSourceMatches") is not True:
                    return None
        return {"sourceSha256": final["sourceSha256"],
                "expectedCandidateRawSha256": final["candidateRawSha256"],
                "expectedRevision": final["revision"], "finalBinding": final,
                "recoveredCurrentReview": True}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def require_finish_binding(host, status, finished):
    source, candidate = host._current_workspace_publication_binding(status)
    final = finished.get("finalBinding") or {}
    if (source != (finished.get("sourceSha256") or final.get("sourceSha256"))
            or candidate != finished.get("expectedCandidateRawSha256")
            or status.get("revision") != finished.get("expectedRevision")):
        raise RuntimeError("validated Shader binding changed before project publication")


def register_finished_workspace(host, project, service, workspace, status, finished, context):
    """Keep the reviewed analysis/graphs in the single project transaction."""
    require_finish_binding(host, status, finished)

    def before_publish(reviewed_status):
        require_finish_binding(host, reviewed_status, finished)
        host._ensure_active_workspace_project_capture(project, reviewed_status, context)

    return host._register_reviewed_workspace_with_readback(
        project, service, str(workspace), before_publish=before_publish)


def publish_finished_replacement(host, workspace_path, finished, applied):
    """None preserves the existing source-only/unconfigured-project route."""
    replacement = applied.get("replacement") or {}
    arguments = {"workspace_path": workspace_path,
                 "expected_revision": finished.get("expectedRevision"),
                 "expected_candidate_raw_sha256": finished.get("expectedCandidateRawSha256"),
                 "replacement_id": replacement.get("replacementId"), "embed_capture_index": False}
    recovery = {"tool": "register_active_project_shader_replacement", "arguments": arguments,
                "recompileRequired": False, "reapplyRequired": False}
    try:
        active = host._active_project_service().status()
        if active.get("scopeConfigured") is not True:
            return None
        host._strict_lower_hex(arguments["replacement_id"], 32, "finished replacement_id")
        from .project_restore import sync_active_shader_workspace_after_apply
        receipt = sync_active_shader_workspace_after_apply(**arguments, expected_finish=finished)
        knowledge = receipt.get("knowledge")
        if receipt.get("projectCommitted") is not True or not isinstance(knowledge, Mapping):
            raise RuntimeError("finished Shader registration omitted its committed knowledge")
        registration = {"schema": "replaylab-active-workspace-registration/v1", "schemaVersion": 1,
            "ok": True, "workspacePath": workspace_path, "projectPath": active.get("projectRoot"),
            "captureId": receipt.get("captureId"), "captureSessionId": receipt.get("captureSessionId"),
            "result": receipt["workspaceRegistration"], "applied": False, "sourceOnlyDraft": False}
        complete = receipt.get("derivedAssociationWritten") is True
        return {"registration": registration,
                "replacementRegistration": {**receipt, "ok": complete,
                    "automatic": True, "recompileRequired": False, "reapplyRequired": False,
                    **({"recovery": recovery} if not complete else {})}}
    except Exception as error:
        diagnostic = str(error)[:800]
        return {"registration": {"ok": False, "error": diagnostic},
                "replacementRegistration": {"ok": False, "automatic": True,
                    "error": diagnostic, "recovery": recovery,
                    "recompileRequired": False, "reapplyRequired": False}}
