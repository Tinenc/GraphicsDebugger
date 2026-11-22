"""Observed functional restoration obligations in the Agent task ledger."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

_TOOLS = frozenset({"begin_unity_restoration", "bind_unity_restoration", "get_unity_restoration_status",
    "check_unity_restoration", "render_unity_restoration", "review_unity_restoration",
    "plan_unity_restoration_recovery", "apply_unity_recovery_assets", "record_unity_reconstruction",
    "adopt_unity_restoration_render_plan", "use_unity_visual_fidelity_goal"})


def _summary(value: Mapping[str, Any]) -> dict:
    return {"restorationId": value["restorationId"], "projectPath": value.get("projectPath", ""),
        "workflowPolicy": "agent-directed", "diagnosticsOnly": True,
        "revision": value.get("revision"), "complete": value.get("complete") is True,
        "gapCodes": [str(gap.get("code", "")) for gap in value.get("gaps", [])][:64],
        "nextTools": list(value.get("nextTools", []))[:16],
        "stateFingerprint": (value.get("inspection") or {}).get("stateFingerprint"),
        "recovery": value.get("recovery", {}), "acceptanceMode": (value.get("acceptance") or {}).get("mode", "strict-pixel"),
        "sourceTransfers": [{key: source[key] for key in ("workspacePath", "sourcePath", "sourceSha256", "dependencyFiles",
            "validation", "sourceSelection", "semanticPublicationSha256") if key in source}
            for source in value.get("sources", []) if (value.get("binding")
                and ((source.get("validation") or {}).get("mode") == "capture-reference"
                    or (source.get("sourcePreparation") or {}).get("displayGaps")))]}


def annotate_transferred_shader_reviews(review: Mapping[str, Any], state: Mapping[str, Any]) -> dict:
    """Defer only exact bound reference drafts to the Unity task's checks.

    This does not complete the original Shader organization or grant replay apply.
    Standalone reverse-edit intents retain their own Shader review obligations.
    """
    from .unity_shader_sources import verify_source_files
    transfers = []
    for restoration in state.get("unityRestorations", {}).values():
        for source in restoration.get("sourceTransfers", []):
            selection = source.get("sourceSelection") or {}
            validation = source.get("validation") or {}
            organization_id = selection.get("organizationId") or validation.get("organizationId")
            if organization_id and not verify_source_files([source]):
                transfers.append((source, organization_id, restoration["restorationId"]))
    rows = []
    for target in review.get("targets", []):
        row = dict(target)
        for source, organization_id, restoration_id in transfers:
            if (os.path.normcase(str(Path(str(row.get("workspacePath", ""))).resolve())) ==
                    os.path.normcase(str(Path(source["workspacePath"]).resolve()))
                    and row.get("organizationId") == organization_id
                    and row.get("sourceSha256") == source.get("sourceSha256")
                    and not row.get("sourceConflict")):
                row["unityTransfer"] = {"restorationId": restoration_id, "referenceOnly": True,
                    "sourceSha256": source["sourceSha256"],
                    "originalReplayComplete": (source.get("validation") or {}).get("replayValidated") is True}
                break
        rows.append(row)
    # Unity preparation is advisory even before a formal package is bound.
    # Preserve all source findings, but do not turn optional Bread publication,
    # navigation or UI display receipts into a second mandatory port workflow.
    if state.get("unityRestorations"):
        restoration_id = next(iter(state["unityRestorations"]))
        for row in rows:
            row.setdefault("unityTransfer", {"restorationId": restoration_id, "referenceOnly": True,
                "sourceSha256": row.get("sourceSha256"), "diagnosticsOnly": True,
                "originalReplayComplete": False})
    return {**review, "targets": rows}


def track(state: dict, tool: str, payload: Any, arguments: Mapping[str, Any], failed: bool) -> None:
    if not state:
        return
    rows = state.setdefault("unityRestorations", {})
    if (tool == "import_resource_export_to_unity" and isinstance(payload, Mapping)
            and payload.get("functionalRestorationRequired") is True):
        imports = state.setdefault("unityFunctionalImports", {})
        key = str(payload.get("operationId", "")) + ":" + os.path.normcase(str(payload.get("projectPath", "")))
        imports[key] = {"operationId": payload.get("operationId"), "projectPath": payload.get("projectPath"),
                        "complete": False, "gapCodes": ["functional-import-needs-restoration-task"],
                        "diagnosticsOnly": True, "workflowPolicy": "agent-directed",
                        "nextTools": ["begin_unity_restoration", "bind_unity_restoration"]}
        state["unityFunctionalImports"] = dict(list(imports.items())[-32:])
    if tool in _TOOLS:
        if isinstance(payload, Mapping) and payload.get("schema") == "bread-unity-restoration/v1" and not failed:
            row = _summary(payload)
            rows[row["restorationId"]] = row
            binding = payload.get("binding") or {}
            if binding.get("operationId"):
                key = str(binding["operationId"]) + ":" + os.path.normcase(str(payload.get("projectPath", "")))
                state.setdefault("unityFunctionalImports", {}).pop(key, None)
        elif arguments.get("restoration_id") in rows:
            task_id = arguments["restoration_id"]
            from .unity_restoration import RestorationStore
            try:
                store = RestorationStore()
                # A failed read/bind/review does not change already saved assets
                # or erase a successful observation. The error is a diagnostic.
                value = store.status(task_id, verify_files=False)
                rows[task_id] = _summary(value)
            except (OSError, ValueError):
                rows[task_id]["diagnostic"] = "Latest restoration observation unavailable"
    elif tool == "call_unity_project_tool":
        if arguments.get("tool_name") in {"bread_connection_identity", "bread_inspect_rendering", "bread_inspect_capture", "read_console"}:
            return
        project = os.path.normcase(str(Path(str(arguments.get("project_path", ""))).resolve()))
        mutating = arguments.get("tool_name") in {"bread_apply_capture_configuration", "bread_import_capture", "bread_apply_project_assets"}
        changed = isinstance(payload, Mapping) and bool(payload.get("changedFiles") or payload.get("sceneModified"))
        if not mutating and not changed:
            return
        for task_id, row in rows.items():
            if os.path.normcase(str(Path(row["projectPath"]).resolve())) == project:
                # A general Editor tool can change scene memory before saving.
                # A new inspect/render receipt is required even if disk hashes
                # have not changed yet. No semantic success is inferred here.
                from .unity_restoration import RestorationStore
                try:
                    value = RestorationStore().invalidate(task_id, "project-tool-after-observation")
                    rows[task_id] = _summary(value)
                except (OSError, ValueError):
                    row.update(complete=False, gapCodes=["restoration-state-unavailable"])
    state["unityRestorations"] = dict(list(rows.items())[-32:])


def refresh(state: dict) -> list[dict]:
    """Recheck saved evidence before accepting a complete task assessment."""
    from .unity_restoration import RestorationStore
    rows = state.get("unityRestorations", {})
    store = RestorationStore()
    for task_id, old in list(rows.items()):
        try:
            rows[task_id] = _summary(store.status(task_id, verify_files=False))
        except (OSError, ValueError, KeyError) as error:
            rows[task_id] = {**old, "complete": False, "gapCodes": ["restoration-state-unavailable"],
                             "diagnostic": str(error)[:500], "nextTools": ["get_unity_restoration_status"]}
    return [dict(row) for row in rows.values() if not row.get("complete")] + list(state.get("unityFunctionalImports", {}).values())


def stable_activity(value: Mapping[str, Any]) -> dict:
    """New PNG paths/timestamps and repeated identical iterations are not progress."""
    comparison = value.get("comparison") or {}
    binding = value.get("binding") or {}
    review = value.get("portingReview")
    review = ({"stateFingerprint": review.get("stateFingerprint"),
               "assessmentAreas": sorted((review.get("assessments") or {}).keys()),
               "decisions": [{key: row.get(key) for key in ("module", "source", "target", "disposition", "fieldId", "reconstructionId")}
                             for row in review.get("decisions", [])]} if isinstance(review, Mapping) else review)
    # Diagnostic wording is not an implementation change. Source, request,
    # observation and image identities below carry actual changed evidence.
    gap_codes = set()
    def collect_codes(item):
        if isinstance(item, Mapping):
            if item.get("code"):
                gap_codes.add(str(item["code"]))
            collect_codes(item.get("detail"))
        elif isinstance(item, list):
            for row in item:
                collect_codes(row)
    collect_codes(value.get("gaps", []))
    return {"restorationId": value.get("restorationId"),
        "complete": value.get("complete"), "gapCodes": sorted(gap_codes),
        "sourceHashes": [(s.get("stage"), s.get("sourceSha256")) for s in value.get("sources", [])],
        "requestSha256": binding.get("requestSha256"),
        "stateFingerprint": (value.get("inspection") or {}).get("stateFingerprint"),
        "referenceSha256": (comparison.get("reference") or {}).get("sha256"),
        "renderedSha256": (comparison.get("rendered") or {}).get("sha256"),
        "withinTolerance": comparison.get("withinTolerance"), "review": review,
        "reconstructionEvidence": value.get("reconstructions"), "acceptance": value.get("acceptance")}
