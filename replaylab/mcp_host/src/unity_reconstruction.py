"""Evidence-bound inferred inputs and alternate implementations.

Missing source data is a reconstruction problem, not a universal stopping rule.
These records never relabel guessed data as captured data, and cannot excuse a
failed compiler, changed artifact, wrong project, or a failed visual check.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping


_SOURCE_GAPS = frozenset({
    "capture_resource_unexported", "captured_buffer_binding_unmapped", "vertex_channel_not_imported",
    "cbuffer_mapping_value_unavailable", "cbuffer_mapping_unresolved", "cbuffer_used_lanes_unmapped",
    "cbuffer_raw_unavailable", "cbuffer_layout_unavailable", "cbuffer_type_unsupported",
    "cbuffer_values_unavailable", "cbuffer_reflection_unavailable", "cbuffer_raw_bytes_unavailable",
    "cbuffer_active_layout_unavailable", "cbuffer_array_stride_unavailable", "cbuffer_layout_offset_missing",
    "cbuffer_live_scalar_encoding_unavailable", "cbuffer_live_value_shape_unavailable",
    "cbuffer_matrix_stride_unavailable", "cbuffer_scalar_type_unsupported", "cbuffer_shape_unsupported",
    "cbuffer_value_outside_captured_window", "cbuffer_value_unavailable",
    "texture_requires_conversion", "texture_sampling_color_space_unknown", "normal_map_decode_unverified",
    "source_mips_not_preserved", "source_mip_count_unknown", "sampler_mapping_unsupported",
    "sampler_mapping_required", "texture_uv_transform_unresolved", "vertex_role_unverified",
    "mesh_uv_bake_policy_unverified", "texture_uv_transform_unsupported", "unmapped_vertex_channel",
    "history-input-unavailable", "hidden-uav-counter", "unknown-buffer-layout",
})
_CLASSES = frozenset({"inferred-history", "inferred-layout", "generated-input", "unity-native", "platform-adapter"})


def gap_id(gap: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps({key: gap.get(key) for key in ("code", "detail")},
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def reconstructable(gap: Mapping[str, Any]) -> bool:
    if gap.get("code") == "cbuffer-coverage-incomplete":
        return True
    if gap.get("code") not in {"export-configuration-gap", "unity-configuration-gap"}:
        return False
    detail = gap.get("detail") or {}
    return isinstance(detail, Mapping) and detail.get("code") in _SOURCE_GAPS


def _text(value: Any, label: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(label + " requires bounded, nonempty text")
    return value


def prepare_records(state: Mapping[str, Any], records: list[dict], hasher: Callable) -> list[dict]:
    """Bind an inference to exact open source gaps and real implementation files."""
    if state.get("acceptance", {}).get("mode", "strict-pixel") != "visual-fidelity":
        raise ValueError("Inferred reconstruction requires an explicit visual-fidelity objective")
    if not isinstance(records, list) or not records or len(records) > 256:
        raise ValueError("Provide a bounded list of reconstruction records")
    gaps = {gap_id(g): g for g in [*state.get("gaps", []),
        *[g for row in state.get("resolvedReconstructions", []) for g in row.get("sourceGaps", [])]]}
    project = Path(state["projectPath"]).resolve()
    request_hash = (state.get("binding") or {}).get("requestSha256")
    if not request_hash:
        raise ValueError("Bind an import before recording a reconstruction")
    result, ids = [], set()
    for record in records:
        if not isinstance(record, Mapping) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(record.get("id", ""))):
            raise ValueError("Reconstruction needs a stable id")
        if record["id"] in ids or record.get("classification") not in _CLASSES:
            raise ValueError("Duplicate reconstruction or unknown classification")
        ids.add(record["id"])
        selected = record.get("sourceGapIds")
        if (not isinstance(selected, list) or not selected or len(selected) > 256
                or any(not isinstance(key, str) or key not in gaps or not reconstructable(gaps[key]) for key in selected)):
            raise ValueError("Only exact current missing-source/configuration gaps can be reconstructed")
        evidence = record.get("evidence")
        if not isinstance(evidence, list) or not evidence or len(evidence) > 64:
            raise ValueError("Describe capture observations, deductions or research used for the inference")
        evidence = [_text(item, "Inference evidence") for item in evidence]
        files = record.get("implementationFiles")
        if not isinstance(files, list) or not files or len(files) > 128:
            raise ValueError("Reconstruction needs actual implementation/input files")
        file_records = []
        for item in files:
            path = Path(_text(item, "Implementation path"))
            if not path.is_absolute() or not path.resolve().is_relative_to(project / "Assets"):
                raise ValueError("Reconstruction implementation must be inside the selected project's Assets")
            file_records.append({"path": str(path.resolve()), "sha256": hasher(path)})
        verification = dict(record.get("verification") or {})
        kind = verification.get("kind")
        if kind in {"pass", "runtime-binding", "component"}:
            _text(verification.get("passId"), "Actual execution passId", 128)
            if kind == "runtime-binding":
                _text(verification.get("name"), "Runtime target name", 256)
            if kind == "component":
                _text(verification.get("type"), "Component type", 512)
        elif kind == "unity-interface":
            token = _text(verification.get("interface"), "Unity interface", 256)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", token):
                raise ValueError("Unity interface must name the actual API identifier")
            source = Path(_text(verification.get("sourcePath"), "Derived source path")).resolve()
            if str(source) not in {row["path"] for row in file_records} or source.suffix.lower() not in {".shader", ".hlsl", ".cginc", ".compute", ".cs"}:
                raise ValueError("Unity interface requires a tracked derived source")
            if source.stat().st_size > 16 * 1024 * 1024 or token not in source.read_text(encoding="utf-8-sig"):
                raise ValueError("The recorded Unity interface is absent from the actual derived source")
            verification["sourcePath"] = str(source)
        else:
            raise ValueError("Verification needs a pass, runtime binding, executed component or Unity interface")
        result.append({"id": record["id"], "classification": record["classification"],
            "reason": _text(record.get("reason"), "Reconstruction reason"), "evidence": evidence,
            "sourceGapIds": selected, "sourceGaps": [gaps[key] for key in selected],
            "implementationFiles": file_records, "verification": verification,
            "requestSha256": request_hash, "origin": "inferred-or-derived", "capturedDataClaimed": False})
    return result


def assess(state: Mapping[str, Any], gaps: list[dict], hasher: Callable) -> tuple[list[dict], list[dict]]:
    """Resolve source gaps only after fresh actual execution, image and source review."""
    inspection = state.get("inspection") or {}
    comparison = state.get("comparison") or {}
    review = state.get("portingReview") or {}
    current = (comparison.get("withinTolerance") is True
        and comparison.get("stateFingerprint") == inspection.get("stateFingerprint")
        and comparison.get("revision") == state["revision"]
        and review.get("stateFingerprint") == inspection.get("stateFingerprint")
        and review.get("revision") == state["revision"])
    observed_files = {str(Path(row["path"]).resolve()): row.get("sha256") for row in inspection.get("files", [])}
    records, covered = [], set()
    for record in state.get("reconstructions", []):
        problems = []
        if record.get("requestSha256") != (state.get("binding") or {}).get("requestSha256"):
            problems.append("import-request-changed")
        if not current:
            problems.append("current-visual-result-and-source-review-required")
        for row in record["implementationFiles"]:
            try:
                if hasher(row["path"]) != row["sha256"] or observed_files.get(row["path"]) != row["sha256"]:
                    problems.append("implementation-not-current-in-unity")
            except (OSError, ValueError):
                problems.append("implementation-file-changed")
        if not any(row.get("reconstructionId") == record["id"] and row.get("disposition") in {"approximation", "unity-native"}
                for row in review.get("decisions", [])):
            problems.append("specific-reconstruction-review-required")
        verification = record["verification"]
        if verification["kind"] != "unity-interface":
            matching = [row for row in inspection.get("passExecution", []) if row.get("passId") == verification["passId"]
                and row.get("renderId") == inspection.get("renderExecutionId") and inspection.get("renderExecutionId")
                and row.get("cameraName") == (inspection.get("renderCamera") or {}).get("name")]
            if not matching:
                problems.append("replacement-pass-not-executed")
            else:
                assets = set()
                for row in matching:
                    relative = str(row.get("implementationAsset") or "")
                    root = Path(state["projectPath"])
                    assets.add(str((root / relative).resolve()))
                    assets.add(str((root / str(state["binding"].get("destination") or "") / relative).resolve()))
                if not assets.intersection(row["path"] for row in record["implementationFiles"]):
                    problems.append("replacement-execution-not-bound-to-implementation")
        if verification["kind"] == "runtime-binding":
            bindings = (inspection.get("runtimeBindings") or {}).get("readback", [])
            if not any(row.get("name") == verification["name"] and row.get("value") is not None for row in bindings):
                problems.append("replacement-input-not-observed")
        if verification["kind"] == "component" and not any(row.get("type") == verification["type"] for row in inspection.get("components", [])):
            problems.append("replacement-component-not-observed")
        resolved = not problems and state.get("acceptance", {}).get("mode") == "visual-fidelity"
        records.append({**record, "status": "validated-reconstruction" if resolved else "needs-verification", "problems": sorted(set(problems))})
        if resolved:
            covered.update(record["sourceGapIds"])
    remaining = [gap for gap in gaps if not (reconstructable(gap) and gap_id(gap) in covered)]
    # Never leave a previously accepted reconstruction silently stale merely
    # because an upstream gap no longer appears in an incomplete observation.
    for record in records:
        if record["problems"]:
            remaining.append({"code": "reconstruction-validation-required", "tool": "check_unity_restoration",
                "detail": {"id": record["id"], "problems": record["problems"]}})
    return remaining, records
