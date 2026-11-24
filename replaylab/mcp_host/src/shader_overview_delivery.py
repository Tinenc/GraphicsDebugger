"""Source-bound calculation-directory display receipts, without replay work.

The Agent reviews meaning. This module checks that the UI actually projected
that reviewed metadata instead of a provisional function-call fallback.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def saved_source_hashes(workspace: str, source_sha256: str) -> set[str]:
    from .shader_organization_review import canonical_source_path, _read_bytes
    _, raw = _read_bytes(Path(workspace).resolve(), canonical_source_path(workspace), 4 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError("Saved source changed before calculation-directory display verification")
    return {source_sha256, hashlib.sha256(raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")).hexdigest()}


def _same_shader(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left.get("logicalShaderId") and right.get("logicalShaderId"):
        return left["logicalShaderId"] == right["logicalShaderId"]
    # Legacy UI payloads have no logical key. Use the original identity, never
    # a candidate/source hash or a versioned project knowledge ID.
    if not left.get("shaderRawSha256") or not left.get("stage"):
        return False
    if any(left.get(key) != right.get(key) for key in ("shaderRawSha256", "stage", "entryPoint")):
        return False
    if left.get("captureApi") and right.get("captureApi"):
        normalize = lambda value: str(value).rsplit(".", 1)[-1].lower()
        return normalize(left["captureApi"]) == normalize(right["captureApi"])
    return bool(left.get("captureId") and left.get("captureId") == right.get("captureId"))


def display_receipt(workspace: str, source_sha256: str, ui: Mapping[str, Any],
                    workspace_id: str = "") -> dict[str, Any]:
    from .shader_organization_review import canonical_source_path, _read_bytes
    from .shader_workspace.semantic_overview import reviewed_overview_state
    result = {"required": True, "complete": False, "state": "pending",
              "workspacePath": str(Path(workspace).resolve()), "sourceSha256": source_sha256,
              "recompileRequired": False,
              "nextAction": "show-saved-source-and-read-ui-without-recompiling"}
    try:
        root = Path(result["workspacePath"])
        _, raw = _read_bytes(root, canonical_source_path(workspace), 4 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise ValueError("Saved source changed before calculation-directory display verification")
        _, metadata = _read_bytes(root, "ai/semantic-overview.json", 256 * 1024)
        overview = json.loads(metadata)
        reviewed = reviewed_overview_state(raw.decode("utf-8"), overview)
        if not reviewed["complete"]:
            result["nextAction"] = "update-semantic-overview-without-recompiling"
            raise ValueError(reviewed["reason"])
        result["overviewSha256"] = reviewed["overviewSha256"]
        rows = [row for row in ui.get("workspaces", []) if isinstance(row, Mapping)
                and Path(str(row.get("workspacePath") or "")).resolve() == root
                and (not workspace_id or row.get("workspaceId") == workspace_id)]
        if len(rows) != 1:
            raise ValueError("Reviewed Shader workspace is absent or ambiguous in the UI")
        identity = rows[0].get("workspaceId")
        dirty = {doc.get("workspaceId") for doc in ui.get("documents", [])
                 if isinstance(doc, Mapping) and doc.get("dirty") is True}
        canonical = [row for row in ui.get("workspaces", []) if isinstance(row, Mapping)
                     and row.get("visibleInWorkspaceTree") is True
                     and row.get("retainedWorkingCopy") is not True
                     and row.get("workspaceId") not in dirty
                     and (row.get("workspaceId") == identity or _same_shader(rows[0], row))]
        if len(canonical) != 1 or canonical[0].get("workspaceId") != identity:
            result["duplicateCanonicalWorkspaceIds"] = [row.get("workspaceId") for row in canonical]
            raise ValueError("The original Shader has duplicate visible canonical sources; preserve dirty copies and show its latest canonical source")
        docs = [doc for doc in ui.get("documents", []) if isinstance(doc, Mapping)
                and doc.get("workspaceId") == identity]
        if len(docs) != 1 or ui.get("selectedWorkspaceId") != identity:
            raise ValueError("Reviewed Shader document is not the selected editor")
        doc = docs[0]
        result.update(workspaceId=identity, pending=doc.get("stageOutlinePending") is True)
        result["captureSessionId"] = (ui.get("captureBinding") or {}).get("captureSessionId")
        state = doc.get("stageOutlineState") or {}
        expected_ids = [stage["id"] for stage in overview["stages"]]
        source_hashes = {source_sha256, hashlib.sha256(raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")).hexdigest()}
        if (doc.get("available") is not True or doc.get("dirty") is not False or doc.get("readOnly") is not False
                or rows[0].get("editorEligible") is not True or rows[0].get("openInTab") is not True
                or doc.get("loadedSourceSha256") != source_sha256
                or doc.get("stageOutlineCurrent") is not True
                or state.get("complete") is not True or state.get("state") != "reviewed"
                or state.get("source") != "semantic-overview" or state.get("unresolvedCount") != 0
                or doc.get("stageOutlineSourceSha256") not in source_hashes
                or doc.get("stageOutlineOverviewSha256") != result["overviewSha256"]
                or doc.get("stageOutlineStageIds") != expected_ids):
            raise ValueError("Calculation directory is pending, provisional, stale or differs from the reviewed metadata")
        result.update(complete=True, state="reviewed", stageCount=len(expected_ids), pending=False,
                      canonicalWorkspaceCount=len(canonical))
        result.pop("nextAction", None)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        result["reason"] = str(error)[:800]
    return result


def observe_display(targets: dict, name: str, payload: Any, arguments: Mapping[str, Any]) -> None:
    """Retain failed display obligations; a later read-only UI receipt can repair them."""
    if not isinstance(payload, Mapping):
        return
    if name == "finish_shader_workspace_organization":
        if arguments.get("show_in_workspace") is not True or arguments.get("publication_mode", "final") != "final":
            return
        workspace = str(arguments.get("workspace_path") or payload.get("workspacePath") or "")
        organization = str(arguments.get("organization_id") or payload.get("organizationId") or "")
        for target in targets.values():
            if (Path(str(target.get("workspacePath") or "")).resolve() == Path(workspace).resolve()
                    and target.get("organizationId") == organization):
                target["semanticOverviewDisplayRequired"] = True
                preview = payload.get("preview") or {}
                if isinstance(preview, Mapping) and preview.get("captureSessionId"):
                    target["semanticOverviewDisplayCaptureSessionId"] = preview["captureSessionId"]
                target["semanticOverviewDisplay"] = dict(payload.get("semanticOverviewDelivery") or {
                    "complete": False, "state": "pending", "recompileRequired": False,
                    "reason": "Final calculation-directory display has not been verified"})
        return
    if name != "get_shader_workspace_ui_state" or payload.get("error"):
        return
    ui = payload.get("ui")
    if not isinstance(ui, Mapping):
        return
    for target in targets.values():
        if target.get("semanticOverviewDisplayRequired") is True:
            from .shader_organization_review import source_snapshot
            try:
                snapshot = source_snapshot(target["workspacePath"], target["organizationId"])
                target["semanticOverviewDisplay"] = display_receipt(
                    target["workspacePath"], snapshot["sourceSha256"], ui)
                expected_session = target.get("semanticOverviewDisplayCaptureSessionId")
                if expected_session and target["semanticOverviewDisplay"].get("captureSessionId") != expected_session:
                    target["semanticOverviewDisplay"].update(complete=False, state="pending",
                        reason="Capture session changed since the requested directory display")
            except (OSError, ValueError, KeyError, TypeError) as error:
                target["semanticOverviewDisplay"] = {"complete": False, "reason": str(error)[:800]}
