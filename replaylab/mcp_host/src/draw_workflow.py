"""Optional, read-only routing advice for independent draw operations.

This is not an executor, completion gate, or a second artifact store. The Agent
can call existing Shader, asset and export tools directly. A Host may provide
already observed receipts to avoid repeating unchanged work; it must check the
receipt's files and hashes before marking ``outputsPresent`` true. Model-supplied
claims are not verified receipts. Unknown identity never establishes reuse.

Source engines select asset readers. Unity URP is a separate output target and
is not a requirement for Shader organization or naming.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
from typing import Any, Mapping, Sequence

SCHEMA = "replaylab-draw-workflow-plan/v1"
ACTIONS = ("organize_shader", "resolve_names", "export_unity", "validate_unity",
           "update_unity_shader")

_ACTION_STEPS = {
    "organize_shader": ("capture_evidence", "asset_lookup", "shader_source",
                        "shader_organization"),
    "resolve_names": ("capture_evidence", "asset_lookup", "name_resolution"),
    "export_unity": ("capture_evidence", "asset_lookup", "shader_source", "shader_organization",
                     "shader_translation", "resource_export", "unity_connection",
                     "unity_import", "unity_validation"),
    "validate_unity": ("unity_connection", "unity_validation"),
    "update_unity_shader": ("capture_evidence", "shader_source", "shader_translation",
                            "unity_connection", "unity_shader_update", "unity_validation"),
}

# Tool families describe purpose, not mandatory calls or a hidden execution order.
_FAMILIES = {
    "capture_evidence": "capture_inspection",
    "asset_lookup": "unpacked_asset_query",
    "shader_source": "shader_workspace",
    "shader_organization": "shader_workspace_organization",
    "name_resolution": "shader_and_resource_aliases",
    "shader_translation": "unity_shader_transfer",
    "resource_export": "resource_export",
    "unity_connection": "unity_project_connection",
    "unity_import": "unity_resource_import",
    "unity_shader_update": "unity_project_tools",
    "unity_validation": "unity_target_validation",
}


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _path(value: Any) -> str:
    path = _text(value)
    if not path:
        return ""
    # Windows drive/UNC paths are insensitive to slash style and case. POSIX
    # paths remain case sensitive so unrelated projects never share a receipt.
    if ntpath.splitdrive(path)[0] or "\\" in path:
        return ntpath.normcase(ntpath.normpath(path))
    return path.rstrip("/") or "/"


def _known(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _target_pipeline(unity: Mapping[str, Any]) -> str:
    # A supplied existing project is not permission to switch it to URP. Only
    # an unspecified destination uses the default restoration profile.
    return _text(unity.get("pipeline")) or ("unverified" if _text(unity.get("projectPath")) else "urp")


def _recipe(step: str, capture: Mapping[str, Any], assets: Mapping[str, Any],
            unity: Mapping[str, Any], engine: str) -> tuple[dict[str, Any], list[str]]:
    draw = {key: capture.get(key) for key in ("captureFingerprint", "eventId", "stage")}
    shader = {key: capture.get(key) for key in ("shaderSha256", "stage", "sourceSha256")}
    target = {key: unity.get(key) for key in ("projectPath", "editorVersion",
                                             "pipelineVersion", "providerVersion")}
    target["projectPath"] = _path(target["projectPath"])
    target["pipeline"] = _target_pipeline(unity)
    if step == "capture_evidence":
        data = draw
    elif step == "asset_lookup":
        data = {**draw, "sourceEngine": engine,
                **{key: capture.get(key) for key in
                   ("shaderSha256", "bindingsHash", "resourceHash", "constantsHash")},
                "roots": sorted({_path(root) for root in assets.get("roots", ())}),
                "indexRevision": assets.get("indexRevision"),
                "readerId": assets.get("readerId")}
    elif step in {"shader_source", "shader_organization"}:
        data = shader
        if step == "shader_organization":
            data = {**data, **draw, "namingHash": capture.get("namingHash"),
                    "organizationSettingsHash": capture.get("organizationSettingsHash")}
    elif step == "name_resolution":
        data = {**draw, **shader,
                **{key: capture.get(key) for key in
                   ("bindingsHash", "constantsHash", "resourceHash", "namingHash")}}
    elif step == "shader_translation":
        data = {**shader, "pipeline": target["pipeline"], "pipelineVersion": unity.get("pipelineVersion"),
                "translationSettingsHash": unity.get("translationSettingsHash")}
    elif step == "resource_export":
        data = {**draw, **{key: capture.get(key) for key in
                          ("bindingsHash", "resourceHash", "constantsHash")},
                "transferSpecHash": unity.get("transferSpecHash")}
    elif step == "unity_connection":
        data = target
    elif step == "unity_import":
        data = {**target, "settingsHash": unity.get("settingsHash"),
                "exportPackageHash": unity.get("exportPackageHash"),
                "shaderTransferHash": unity.get("shaderTransferHash")}
    elif step == "unity_shader_update":
        data = {**target, "importHash": unity.get("importHash"),
                "shaderTransferHash": unity.get("shaderTransferHash"),
                "settingsHash": unity.get("settingsHash")}
    else:
        data = {**target, "importHash": unity.get("importHash"),
                "referenceHash": unity.get("referenceHash"),
                "validationSettingsHash": unity.get("validationSettingsHash")}
    missing = [key for key, value in data.items() if not _known(value)]
    if data.get("pipeline") == "unverified":
        missing.append("pipeline")
    return data, missing


def _dependencies(step: str, action: str) -> tuple[str, ...]:
    return {
        "asset_lookup": ("capture_evidence",),
        # Shader source identity is independent of EID, textures and asset names.
        "shader_organization": ("shader_source", "capture_evidence"),
        "name_resolution": ("capture_evidence",),
        "shader_translation": ("shader_source",),
        "resource_export": ("capture_evidence",),
        "unity_import": ("resource_export", "shader_translation", "unity_connection"),
        "unity_shader_update": ("shader_translation", "unity_connection"),
        "unity_validation": (("unity_import", "unity_connection") if action == "export_unity"
                             else ("unity_shader_update", "unity_connection")
                             if action == "update_unity_shader" else ("unity_connection",)),
    }.get(step, ())


def build_draw_workflow_plan(
    action: str,
    *,
    capture_identity: Mapping[str, Any] | None = None,
    source_engine: str = "unknown",
    asset_context: Mapping[str, Any] | None = None,
    unity_context: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Advise only the steps relevant to one requested operation.

    ``captureFingerprint`` identifies immutable capture evidence, not just a path
    or session ResourceId. Hashes/revisions describe current observed inputs.
    Missing values keep a step runnable, but prevent receipt reuse. An optional
    asset lookup failure never blocks Shader work or capture-based export.

    ``artifacts`` is Host-owned receipt data: step, inputFingerprint, receiptRef,
    outputFingerprint, status (complete/no_match/unsupported), outputsPresent.
    For reuse the Host must have just verified outputs or observed a live session;
    in particular an old connection receipt is not a current Unity connection.
    This function checks identity consistency, not semantic success or disk files.
    """
    if action not in ACTIONS:
        raise ValueError("unsupported draw action: " + _text(action))
    capture = dict(capture_identity or {})
    assets = dict(asset_context or {})
    unity = dict(unity_context or {})
    engine = _text(source_engine).lower() or "unknown"
    capture["stage"] = _text(capture.get("stage")).upper()
    if capture.get("eventId") is not None:
        event_id = capture["eventId"]
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("eventId must be a positive integer")
    for field in ("roots",):
        value = assets.get(field, ())
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError("asset roots must be a list of paths")
    reader_status = _text(assets.get("readerStatus")).lower() or "unknown"
    if reader_status not in {"available", "missing", "failed", "unsupported", "unknown"}:
        raise ValueError("unsupported readerStatus: " + reader_status)

    steps: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    for step_id in _ACTION_STEPS[action]:
        data, missing = _recipe(step_id, capture, assets, unity, engine)
        dependencies = _dependencies(step_id, action)
        # Receipt output hashes bind downstream work to the actual producer,
        # including a repaired output generated from otherwise identical inputs.
        # Connection liveness must be observed for execution, but restarting an
        # Editor does not invalidate files previously imported into that project.
        upstream = {name: outputs.get(name) for name in dependencies
                    if name != "unity_connection"}
        missing += ["receipt:" + name for name, value in upstream.items() if not value]
        optional = step_id == "asset_lookup"
        fingerprint = None if missing else _hash({"step": step_id, "inputs": data,
                                                 "upstream": upstream})
        step: dict[str, Any] = {
            "id": step_id, "toolFamily": _FAMILIES[step_id], "optional": optional,
            "dependsOn": list(dependencies), "status": "run",
            "inputFingerprint": fingerprint, "requiredEvidence": missing,
            "reason": "missing_or_changed_receipt", "receiptRef": None,
        }
        if action == "export_unity" and step_id == "shader_organization":
            step.update(preferred=True, blocking=False,
                instruction="Reuse prepared Bread source or decompile, organize, compile and validate it before Unity adaptation. "
                    "Permanent replay apply is not required. On concrete application/tool failure, retain the best source, "
                    "passed checks and unresolved diagnostics, then organize/validate/repair the remainder in Unity.",
                fallback={"destination": "unity", "condition": "application-or-tool-limitation", "preservePartialEvidence": True})
        if optional and not assets.get("roots"):
            step.update(status="optional_skip", reason="no_asset_roots")
        elif optional and reader_status in {"missing", "failed", "unsupported"}:
            step.update(status="optional_skip", reason="reader_" + reader_status)
        elif optional and reader_status == "unknown":
            step.update(status="optional", reason="reader_availability_unknown")
        if fingerprint and step["status"] not in {"optional_skip", "optional"}:
            for record in reversed(list(artifacts)):
                if not isinstance(record, Mapping):
                    continue
                if (record.get("step") != step_id
                        or record.get("inputFingerprint") != fingerprint
                        or record.get("outputsPresent") is not True
                        or not _text(record.get("receiptRef"))
                        or not _text(record.get("outputFingerprint"))):
                    continue
                if record.get("status") == "complete":
                    step.update(status="reuse", reason="matching_observed_receipt",
                                receiptRef=record["receiptRef"])
                    outputs[step_id] = _text(record["outputFingerprint"])
                    break
                if optional and record.get("status") in {"no_match", "unsupported"}:
                    step.update(status="optional_skip", reason="cached_" + record["status"],
                                receiptRef=record["receiptRef"])
                    break
        # Optional asset evidence never becomes a required dependency. Its actual
        # effect is represented by namingHash/transferSpecHash after it is used.
        steps.append(step)

    skipped_assets = next((item for item in steps if item["id"] == "asset_lookup"
                           and item["status"] in {"optional_skip", "optional"}), None)
    needs_unity = action in {"export_unity", "validate_unity", "update_unity_shader"}
    return {
        "schema": SCHEMA, "action": action, "execution": "not_started",
        "advisoryOnly": True, "sourceEngine": engine,
        "target": {"engine": "unity", "pipeline": _target_pipeline(unity)} if needs_unity else None,
        "assetAssistance": "capture_only" if skipped_assets else "optional",
        "nameProvenance": "inferred_until_evidenced",
        "steps": steps,
        "limitations": ([skipped_assets["reason"]] if skipped_assets else []),
        "policy": {
            "missingReaderBlocksShader": False,
            "missingReaderBlocksCaptureExport": False,
            "fullPipelineRequired": False,
            "preserveCapturedShaderVariant": True,
            "separateUnityShaderSource": True,
            "planProvesExecution": False,
            "sourcePreparation": "bread-first-unity-fallback",
            "incompleteSourceValidationBlocksUnity": False,
        },
    }
