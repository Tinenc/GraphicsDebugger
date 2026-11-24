"""Auditable shader representations that never degrade to an empty editor.

The executable workspace source and an explanatory rendering of captured
bytecode are intentionally different trust lanes.  This module contains no AI
logic: it accepts only captured bytes and, optionally, a RenderDoc bridge
disassembly response whose physical shader identity can be checked.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import normalize_stage
from .errors import ManifestValidationError, ToolExecutionError
from .io import atomic_write_bytes, atomic_write_json, is_within, read_json_object
from .toolchains import detect_shader_format


REPRESENTATION_SCHEMA = "renderdoc-ultra-shader-representation/v1"
REPRESENTATION_VERSION = 1
CURRENT_REPRESENTATION_SCHEMA = "renderdoc-ultra-current-shader-representation/v1"
REPRESENTATION_LANES = frozenset(
    ("executable-editable", "explanatory-ir", "binary-only")
)
MAX_REPRESENTATION_BYTES = 64 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTICS = 16
MAX_DIAGNOSTIC_CHARS = 4096
MAX_BINARY_PREVIEW_BYTES = 1024 * 1024
_SAFE_COMPONENT_RE = re.compile(r"[^a-z0-9_.-]+")


def bounded_diagnostic(code: str, message: Any) -> dict[str, str]:
    """Return a bounded, display-only diagnostic record."""

    normalized_code = str(code or "representation-diagnostic").strip()
    normalized_message = str(message or "no additional diagnostic was provided")
    return {
        "code": normalized_code[:128],
        "message": normalized_message[:MAX_DIAGNOSTIC_CHARS],
    }


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _binary_preview(
    data: bytes,
    *,
    detected_format: str,
    raw_sha256: str,
    stage: str,
    entry_point: str,
    target_profile: str,
) -> bytes:
    """Render a bounded exact byte view while retaining the complete binary.

    This is deliberately called a byte view rather than an instruction
    disassembly.  It is the final non-empty fallback when no trustworthy
    disassembler exists or a configured tool fails.
    """

    shown = min(len(data), MAX_BINARY_PREVIEW_BYTES)
    lines = [
        "RenderDoc Ultra captured shader binary view",
        "representation: binary-only (not source and not instruction disassembly)",
        "format: %s" % (detected_format or "unknown"),
        "stage: %s" % stage,
        "entryPoint: %s" % entry_point,
        "targetProfile: %s" % target_profile,
        "byteSize: %d" % len(data),
        "rawSha256: %s" % raw_sha256,
        "previewBytes: %d" % shown,
        "",
    ]
    for offset in range(0, shown, 16):
        chunk = data[offset : offset + 16]
        hexadecimal = " ".join("%02x" % value for value in chunk)
        printable = "".join(chr(value) if 32 <= value < 127 else "." for value in chunk)
        lines.append("%08x  %-47s  |%s|" % (offset, hexadecimal, printable))
    if shown != len(data):
        lines.extend(
            (
                "",
                "preview truncated after %d bytes; baseline/shader.bin is the complete "
                "hash-verified executable" % shown,
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _renderdoc_disassembly(
    response: Mapping[str, Any],
    *,
    expected_resource_id: int,
    expected_stage: str,
    expected_entry_point: str,
    expected_raw_sha256: str,
) -> tuple[bytes, list[str]]:
    """Validate physical identity and bundle every non-error disassembly target."""

    if response.get("error"):
        raise ToolExecutionError("RenderDoc reverse_shader failed: %s" % response["error"])
    try:
        shader_id = int(response.get("shaderId"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolExecutionError("RenderDoc disassembly has no physical shader ResourceId") from exc
    if shader_id != expected_resource_id:
        raise ToolExecutionError("RenderDoc disassembly shader ResourceId contradicts the workspace")
    if normalize_stage(response.get("stage")) != normalize_stage(expected_stage):
        raise ToolExecutionError("RenderDoc disassembly stage contradicts the workspace")
    if str(response.get("entryPoint", "") or "") != expected_entry_point:
        raise ToolExecutionError("RenderDoc disassembly entry point contradicts the workspace")
    raw_sha256 = str(response.get("rawSha256", "") or "").lower()
    if raw_sha256 != expected_raw_sha256:
        raise ToolExecutionError("RenderDoc disassembly raw hash contradicts the workspace")

    values = response.get("disassembly")
    if not isinstance(values, Mapping) or not values:
        raise ToolExecutionError("RenderDoc returned no disassembly targets")
    targets: list[str] = []
    sections: list[str] = [
        "RenderDoc Ultra native disassembly bundle",
        "representation: explanatory-ir (never compile or apply this file)",
        "shaderResourceId: %d" % expected_resource_id,
        "stage: %s" % normalize_stage(expected_stage),
        "entryPoint: %s" % expected_entry_point,
        "rawSha256: %s" % expected_raw_sha256,
        "",
    ]
    for target in sorted(values, key=lambda value: str(value).casefold()):
        name = str(target)
        text = values[target]
        if not isinstance(text, str) or not text.strip():
            continue
        if text.lstrip().lower().startswith("error:"):
            continue
        encoded = text.encode("utf-8", errors="strict")
        if len(encoded) > MAX_REPRESENTATION_BYTES:
            continue
        targets.append(name)
        sections.extend(("===== RenderDoc target: %s =====" % name, text.rstrip(), ""))
    if not targets:
        raise ToolExecutionError("RenderDoc returned no non-error disassembly text")
    payload = ("\n".join(sections) + "\n").encode("utf-8")
    if len(payload) > MAX_REPRESENTATION_BYTES:
        raise ToolExecutionError("RenderDoc disassembly bundle exceeds the representation limit")
    return payload, targets


def publish_fallback_representation(
    workspace_root: Path,
    *,
    baseline_path: Path,
    baseline_raw_sha256: str,
    shader: Mapping[str, Any],
    stage: str,
    entry_point: str,
    target_profile: str,
    bridge_response: Mapping[str, Any] | None = None,
    bridge_event_id: int | None = None,
    diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Publish explanatory IR or a non-empty exact-binary fallback descriptor."""

    root = workspace_root.resolve()
    baseline = baseline_path.resolve()
    if not is_within(root, baseline) or not baseline.is_file():
        raise ManifestValidationError("representation baseline escapes the workspace")
    data = baseline.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != baseline_raw_sha256:
        raise ManifestValidationError("representation baseline hash mismatch")

    clean_diagnostics = [
        bounded_diagnostic(item.get("code", "representation-diagnostic"), item.get("message", ""))
        for item in diagnostics[:MAX_DIAGNOSTICS]
        if isinstance(item, Mapping)
    ]
    lane = "binary-only"
    kind = "captured-binary-byte-view"
    producer: dict[str, Any] = {
        "kind": "fixed-program",
        "component": "renderdoc-mcp.shader_workspace.representations",
    }
    targets: list[str] = []
    payload: bytes
    if bridge_response is not None:
        try:
            payload, targets = _renderdoc_disassembly(
                bridge_response,
                expected_resource_id=int(shader.get("resourceId")),
                expected_stage=stage,
                expected_entry_point=entry_point,
                expected_raw_sha256=baseline_raw_sha256,
            )
            lane = "explanatory-ir"
            kind = "renderdoc-native-disassembly"
            producer = {
                "kind": "fixed-program",
                "component": "qrenderdoc.ReplayController.DisassembleShader",
                "rpc": "reverse_shader",
                "eventId": bridge_event_id,
                "targets": targets,
            }
        except Exception as exc:
            clean_diagnostics.append(bounded_diagnostic("renderdoc-disassembly-rejected", exc))
            payload = b""
    else:
        payload = b""

    detection = detect_shader_format(data)
    if not payload:
        payload = _binary_preview(
            data,
            detected_format=detection.format or "unknown",
            raw_sha256=baseline_raw_sha256,
            stage=stage,
            entry_point=entry_point,
            target_profile=target_profile,
        )
    if not payload or len(payload) > MAX_REPRESENTATION_BYTES:
        raise ManifestValidationError("fallback representation size is invalid")

    directory = root / "explanation"
    if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
        raise ManifestValidationError("workspace explanation directory is redirected")
    directory.mkdir(exist_ok=True)
    artifact = directory / ("native-ir.txt" if lane == "explanatory-ir" else "captured-binary.txt")
    if artifact.is_symlink():
        raise ManifestValidationError("workspace representation artifact is redirected")
    atomic_write_bytes(artifact, payload)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    record = {
        "schema": REPRESENTATION_SCHEMA,
        "schemaVersion": REPRESENTATION_VERSION,
        "lane": lane,
        "kind": kind,
        "path": artifact.relative_to(root).as_posix(),
        "sha256": artifact_hash,
        "byteSize": len(payload),
        "encoding": "utf-8",
        "baseline": {
            "path": baseline.relative_to(root).as_posix(),
            "rawSha256": baseline_raw_sha256,
            "byteSize": len(data),
            "detectedFormat": detection.format,
        },
        "shader": {
            "resourceId": int(shader.get("resourceId")),
            "stage": normalize_stage(stage),
            "entryPoint": entry_point,
        },
        "provenance": producer,
        "diagnostics": clean_diagnostics[:MAX_DIAGNOSTICS],
        "compileEligible": False,
        "applyEligible": False,
        "abiCompatible": False,
        "semanticEquivalenceVerified": False,
        "aiPolicy": {
            "mayExplainOrOrganize": True,
            "organizedOutputLane": "explanatory-ir",
            "mayCompileOrApply": False,
        },
    }
    atomic_write_json(directory / "representation.json", record)
    return _absolute_record(root, record)


def publish_tool_explanatory_representation(
    workspace_root: Path,
    *,
    baseline_path: Path,
    baseline_raw_sha256: str,
    shader: Mapping[str, Any],
    stage: str,
    entry_point: str,
    payload: bytes,
    route_ids: Sequence[str],
    job_paths: Sequence[str],
    diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Publish one fixed-tool decompile result that has no reverse compiler.

    The text remains explanatory even though a trusted, hash-pinned tool
    produced it: without a reverse route and untouched baseline compile probe,
    ReplayLab has no evidence that the text is executable-editable.
    """

    root = workspace_root.resolve()
    baseline = baseline_path.resolve()
    if not is_within(root, baseline) or not baseline.is_file():
        raise ManifestValidationError("representation baseline escapes the workspace")
    baseline_bytes = baseline.read_bytes()
    if hashlib.sha256(baseline_bytes).hexdigest() != baseline_raw_sha256:
        raise ManifestValidationError("representation baseline hash mismatch")
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_REPRESENTATION_BYTES:
        raise ManifestValidationError("tool representation size is invalid")
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("tool representation is not UTF-8") from exc
    if (
        not route_ids
        or len(route_ids) != len(job_paths)
        or len(route_ids) > 4
        or any(not isinstance(value, str) or not value for value in route_ids)
        or any(not isinstance(value, str) or not value for value in job_paths)
    ):
        raise ManifestValidationError("tool representation provenance is invalid")

    directory = root / "explanation"
    if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
        raise ManifestValidationError("workspace explanation directory is redirected")
    directory.mkdir(exist_ok=True)
    artifact = directory / "tool-ir.txt"
    if artifact.is_symlink():
        raise ManifestValidationError("workspace representation artifact is redirected")
    atomic_write_bytes(artifact, payload)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    detection = detect_shader_format(baseline_bytes)
    record = {
        "schema": REPRESENTATION_SCHEMA,
        "schemaVersion": REPRESENTATION_VERSION,
        "lane": "explanatory-ir",
        "kind": "trusted-tool-one-way-decompile",
        "path": artifact.relative_to(root).as_posix(),
        "sha256": artifact_hash,
        "byteSize": len(payload),
        "encoding": "utf-8",
        "baseline": {
            "path": baseline.relative_to(root).as_posix(),
            "rawSha256": baseline_raw_sha256,
            "byteSize": len(baseline_bytes),
            "detectedFormat": detection.format,
        },
        "shader": {
            "resourceId": int(shader.get("resourceId")),
            "stage": normalize_stage(stage),
            "entryPoint": entry_point,
        },
        "provenance": {
            "kind": "fixed-toolchain",
            "component": "renderdoc-mcp.shader_workspace",
            "routeIds": list(route_ids),
            "producedByJobs": list(job_paths),
            "reverseCompilerRoutePresent": False,
            "baselineCompileVerified": False,
        },
        "diagnostics": [
            bounded_diagnostic(
                item.get("code", "representation-diagnostic"),
                item.get("message", ""),
            )
            for item in diagnostics[:MAX_DIAGNOSTICS]
            if isinstance(item, Mapping)
        ],
        "compileEligible": False,
        "applyEligible": False,
        "abiCompatible": False,
        "semanticEquivalenceVerified": False,
        "aiPolicy": {
            "mayExplainOrOrganize": True,
            "organizedOutputLane": "explanatory-ir",
            "mayCompileOrApply": False,
        },
    }
    atomic_write_json(directory / "representation.json", record)
    return _absolute_record(root, record)


def _absolute_record(root: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["present"] = True
    result["path"] = str((root / str(record["path"])).resolve())
    result["descriptorPath"] = str((root / "explanation" / "representation.json").resolve())
    return result


def load_fallback_representation(
    workspace_root: Path,
    *,
    expected_baseline_path: Path,
    expected_baseline_raw_sha256: str,
    expected_shader: Mapping[str, Any],
    expected_stage: str,
    expected_entry_point: str,
    expected_route_plan: Mapping[str, Any] | None = None,
    expected_history: Sequence[Mapping[str, Any]] = (),
    expected_revision: int | None = None,
) -> dict[str, Any] | None:
    """Load and hash-validate a persisted explanatory/binary representation."""

    root = workspace_root.resolve()
    descriptor = root / "explanation" / "representation.json"
    if not descriptor.exists():
        return None
    if descriptor.is_symlink():
        raise ManifestValidationError("workspace representation descriptor is redirected")
    try:
        record = read_json_object(descriptor, MAX_DESCRIPTOR_BYTES, "shader representation")
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc
    required = {
        "schema", "schemaVersion", "lane", "kind", "path", "sha256", "byteSize",
        "encoding", "baseline", "shader", "provenance", "diagnostics",
        "compileEligible", "applyEligible", "abiCompatible",
        "semanticEquivalenceVerified", "aiPolicy",
    }
    if set(record) != required:
        raise ManifestValidationError("shader representation descriptor fields are invalid")
    lane = record.get("lane")
    if (
        record.get("schema") != REPRESENTATION_SCHEMA
        or record.get("schemaVersion") != REPRESENTATION_VERSION
        or lane not in {"explanatory-ir", "binary-only"}
        or record.get("compileEligible") is not False
        or record.get("applyEligible") is not False
        or record.get("abiCompatible") is not False
        or record.get("semanticEquivalenceVerified") is not False
        or record.get("encoding") != "utf-8"
    ):
        raise ManifestValidationError("shader representation trust-lane metadata is invalid")
    ai_policy = record.get("aiPolicy")
    if ai_policy != {
        "mayExplainOrOrganize": True,
        "organizedOutputLane": "explanatory-ir",
        "mayCompileOrApply": False,
    }:
        raise ManifestValidationError("shader representation AI policy is invalid")
    diagnostics = record.get("diagnostics")
    if (
        not isinstance(diagnostics, list)
        or len(diagnostics) > MAX_DIAGNOSTICS
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"code", "message"}
            or not isinstance(item.get("code"), str)
            or not item.get("code")
            or len(item.get("code")) > 128
            or not isinstance(item.get("message"), str)
            or len(item.get("message")) > MAX_DIAGNOSTIC_CHARS
            for item in diagnostics
        )
    ):
        raise ManifestValidationError("shader representation diagnostics are invalid")
    artifact = (root / str(record.get("path", ""))).resolve()
    if (
        not is_within(root / "explanation", artifact)
        or not artifact.is_file()
        or artifact.is_symlink()
    ):
        raise ManifestValidationError("shader representation artifact is missing or redirected")
    data = artifact.read_bytes()
    if (
        not data
        or len(data) > MAX_REPRESENTATION_BYTES
        or record.get("byteSize") != len(data)
        or record.get("sha256") != hashlib.sha256(data).hexdigest()
    ):
        raise ManifestValidationError("shader representation artifact/hash mismatch")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("shader representation is not UTF-8") from exc
    baseline = record.get("baseline")
    shader = record.get("shader")
    expected_baseline = expected_baseline_path.resolve()
    if (
        not isinstance(baseline, Mapping)
        or set(baseline) != {"path", "rawSha256", "byteSize", "detectedFormat"}
        or (root / str(baseline.get("path", ""))).resolve() != expected_baseline
        or baseline.get("rawSha256") != expected_baseline_raw_sha256
        or not isinstance(shader, Mapping)
        or set(shader) != {"resourceId", "stage", "entryPoint"}
        or shader.get("resourceId") != int(expected_shader.get("resourceId"))
        or shader.get("stage") != normalize_stage(expected_stage)
        or shader.get("entryPoint") != expected_entry_point
    ):
        raise ManifestValidationError("shader representation physical identity mismatch")
    if (
        not expected_baseline.is_file()
        or expected_baseline.is_symlink()
        or baseline.get("byteSize") != expected_baseline.stat().st_size
        or hashlib.sha256(expected_baseline.read_bytes()).hexdigest()
        != expected_baseline_raw_sha256
        or baseline.get("detectedFormat")
        != detect_shader_format(expected_baseline.read_bytes()).format
    ):
        raise ManifestValidationError("shader representation baseline evidence is invalid")
    if not _valid_sha256(record.get("sha256")):
        raise ManifestValidationError("shader representation digest is invalid")
    kind = record.get("kind")
    provenance = record.get("provenance")
    relative_artifact = record.get("path")
    if kind == "captured-binary-byte-view":
        if (
            lane != "binary-only"
            or relative_artifact != "explanation/captured-binary.txt"
            or provenance
            != {
                "kind": "fixed-program",
                "component": "renderdoc-mcp.shader_workspace.representations",
            }
        ):
            raise ManifestValidationError("binary representation provenance is invalid")
    elif kind == "renderdoc-native-disassembly":
        if (
            lane != "explanatory-ir"
            or relative_artifact != "explanation/native-ir.txt"
            or not isinstance(provenance, Mapping)
            or set(provenance)
            != {"kind", "component", "rpc", "eventId", "targets"}
            or provenance.get("kind") != "fixed-program"
            or provenance.get("component")
            != "qrenderdoc.ReplayController.DisassembleShader"
            or provenance.get("rpc") != "reverse_shader"
            or (
                provenance.get("eventId") is not None
                and (
                    isinstance(provenance.get("eventId"), bool)
                    or not isinstance(provenance.get("eventId"), int)
                    or provenance.get("eventId") <= 0
                )
            )
            or not isinstance(provenance.get("targets"), list)
            or not provenance.get("targets")
            or len(provenance.get("targets")) > 4096
            or any(
                not isinstance(value, str) or not value
                for value in provenance.get("targets")
            )
        ):
            raise ManifestValidationError("native disassembly provenance is invalid")
    elif kind == "trusted-tool-one-way-decompile":
        _validate_one_way_tool_provenance(
            root,
            record,
            provenance,
            expected_route_plan,
            expected_history,
            expected_revision,
        )
    else:
        raise ManifestValidationError("shader representation kind is invalid")
    return _absolute_record(root, record)


def _validate_one_way_tool_provenance(
    root: Path,
    record: Mapping[str, Any],
    provenance: Any,
    route_plan: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]],
    expected_revision: int | None,
) -> None:
    """Cross-bind one-way explanatory text to its fixed route jobs."""

    if (
        record.get("lane") != "explanatory-ir"
        or record.get("path") != "explanation/tool-ir.txt"
        or not isinstance(provenance, Mapping)
        or set(provenance)
        != {
            "kind",
            "component",
            "routeIds",
            "producedByJobs",
            "reverseCompilerRoutePresent",
            "baselineCompileVerified",
        }
        or provenance.get("kind") != "fixed-toolchain"
        or provenance.get("component") != "renderdoc-mcp.shader_workspace"
        or provenance.get("reverseCompilerRoutePresent") is not False
        or provenance.get("baselineCompileVerified") is not False
        or not isinstance(route_plan, Mapping)
        or route_plan.get("compile") != []
    ):
        raise ManifestValidationError("one-way tool representation provenance is invalid")
    decompile = route_plan.get("decompile")
    if not isinstance(decompile, list) or not decompile or len(decompile) > 4:
        raise ManifestValidationError("one-way tool route plan is invalid")
    expected_route_ids: list[str] = []
    for binding in decompile:
        capability = binding.get("capability") if isinstance(binding, Mapping) else None
        route_id = capability.get("routeId") if isinstance(capability, Mapping) else None
        if not isinstance(route_id, str) or not route_id:
            raise ManifestValidationError("one-way tool route binding is invalid")
        expected_route_ids.append(route_id)
    route_ids = provenance.get("routeIds")
    job_paths = provenance.get("producedByJobs")
    if (
        route_ids != expected_route_ids
        or not isinstance(job_paths, list)
        or len(job_paths) != len(expected_route_ids)
        or any(not isinstance(value, str) or not value for value in job_paths)
    ):
        raise ManifestValidationError("one-way tool route/job provenance mismatch")

    matching_history: Mapping[str, Any] | None = None
    for entry in reversed(list(history)):
        if (
            isinstance(entry, Mapping)
            and entry.get("operation") == "decompile-route-chain"
            and entry.get("status") == "succeeded-explanatory"
            and entry.get("routePlanSha256") == route_plan.get("planSha256")
            and entry.get("revision") == expected_revision
            and entry.get("representationSha256") == record.get("sha256")
            and entry.get("publishedRepresentationPath") == record.get("path")
            and entry.get("outputSha256") == record.get("sha256")
        ):
            matching_history = entry
            break
    if matching_history is None:
        raise ManifestValidationError("one-way tool representation has no matching history")
    hops = matching_history.get("hops")
    if not isinstance(hops, list) or len(hops) != len(expected_route_ids):
        raise ManifestValidationError("one-way tool history hops are invalid")
    previous_hash = str(record.get("baseline", {}).get("rawSha256", ""))
    for index, (hop, route_id, job_path, expected_binding) in enumerate(
        zip(hops, expected_route_ids, job_paths, decompile, strict=True), start=1
    ):
        capability = (
            hop.get("routeBinding", {}).get("capability")
            if isinstance(hop, Mapping)
            else None
        )
        expected_operation = (
            expected_binding.get("operation")
            if isinstance(expected_binding, Mapping)
            else None
        )
        expected_capability = (
            expected_binding.get("capability")
            if isinstance(expected_binding, Mapping)
            else None
        )
        if (
            not isinstance(hop, Mapping)
            or hop.get("hop") != index
            or hop.get("status") != "succeeded"
            or hop.get("jobPath") != job_path
            or hop.get("routeBinding") != expected_binding
            or not isinstance(capability, Mapping)
            or capability.get("routeId") != route_id
            or not isinstance(expected_operation, Mapping)
            or not isinstance(expected_capability, Mapping)
            or hop.get("operation") != expected_operation.get("operation")
            or hop.get("toolId") != expected_operation.get("toolId")
            or hop.get("toolConfigDigest")
            != expected_operation.get("configDigest")
            or hop.get("executableSha256")
            != expected_operation.get("executableSha256")
            or hop.get("fixedFileHashes")
            != expected_operation.get("fixedFileHashes")
            or hop.get("inputFormat") != expected_capability.get("inputFormat")
            or hop.get("outputFormat") != expected_capability.get("outputFormat")
        ):
            raise ManifestValidationError("one-way tool history binding mismatch")
        job = (root / job_path).resolve()
        jobs_root = (root / "jobs").resolve()
        if (
            not is_within(jobs_root, job)
            or job == jobs_root
            or not job.is_dir()
            or job.is_symlink()
        ):
            raise ManifestValidationError("one-way tool job is missing or redirected")
        input_path = (root / str(hop.get("inputPath", ""))).resolve()
        output = (root / str(hop.get("outputPath", ""))).resolve()
        if (
            not is_within(job, input_path)
            or not input_path.is_file()
            or input_path.is_symlink()
            or not is_within(job, output)
            or not output.is_file()
            or output.is_symlink()
        ):
            raise ManifestValidationError("one-way tool artifact escapes its job")
        input_bytes = input_path.read_bytes()
        output_bytes = output.read_bytes()
        input_hash = hashlib.sha256(input_bytes).hexdigest()
        output_hash = hashlib.sha256(output_bytes).hexdigest()
        if (
            hop.get("inputSha256") != input_hash
            or input_hash != previous_hash
            or hop.get("outputSha256")
            != output_hash
            or hop.get("inputDetection")
            != detect_shader_format(
                input_bytes, str(expected_capability.get("inputFormat"))
            ).as_dict()
            or hop.get("outputDetection")
            != detect_shader_format(
                output_bytes, str(expected_capability.get("outputFormat"))
            ).as_dict()
        ):
            raise ManifestValidationError("one-way tool output/hash binding mismatch")
        previous_hash = output_hash
    if hops[-1].get("outputSha256") != record.get("sha256"):
        raise ManifestValidationError("one-way tool final output hash mismatch")


def publish_current_event_representation(
    output_root: Path,
    *,
    event_id: int,
    requested_stage: str,
    bridge_response: Mapping[str, Any],
    capture_status: Mapping[str, Any] | None = None,
    raw_bytes: bytes | None = None,
    raw_bytes_diagnostic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize bridge-owned Shader text without a DX-only workspace.

    This path is intentionally representation-only, so it also covers Vulkan,
    OpenGL, and other APIs for which the executable workspace/catalog does not
    yet have a safe compile/apply implementation.
    """

    if isinstance(event_id, bool) or int(event_id) <= 0:
        raise ToolExecutionError("event_id must be a positive integer")
    stage = normalize_stage(requested_stage)
    if not stage:
        raise ToolExecutionError("stage is required")
    if bridge_response.get("error"):
        # Failed Shader queries often omit stage and identity fields. Preserve
        # the actual replay/reflection failure before checking success metadata.
        message = bounded_diagnostic("shader-query-failed", bridge_response["error"])["message"]
        raise ToolExecutionError("RenderDoc Shader query failed: %s" % message)
    response_stage = normalize_stage(bridge_response.get("stage"))
    if response_stage != stage:
        raise ToolExecutionError("RenderDoc representation stage contradicts the request")
    try:
        resource_id = int(bridge_response.get("shaderId"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolExecutionError("RenderDoc representation has no shader ResourceId") from exc
    if resource_id <= 0:
        raise ToolExecutionError("RenderDoc representation shader ResourceId is invalid")
    raw_sha256 = str(bridge_response.get("rawSha256", "") or "").lower()
    if raw_sha256 and not _valid_sha256(raw_sha256):
        raise ToolExecutionError("RenderDoc representation rawSha256 is malformed")

    diagnostics: list[dict[str, str]] = []
    if isinstance(raw_bytes_diagnostic, Mapping):
        diagnostics.append(
            bounded_diagnostic(
                raw_bytes_diagnostic.get("code", "raw-bytes-unavailable"),
                raw_bytes_diagnostic.get("message", ""),
            )
        )
    if raw_bytes is not None:
        if not isinstance(raw_bytes, bytes) or not raw_bytes:
            raise ToolExecutionError("exported Shader raw bytes are invalid")
        exported_hash = hashlib.sha256(raw_bytes).hexdigest()
        if not raw_sha256 or exported_hash != raw_sha256:
            raise ToolExecutionError("exported Shader bytes contradict reverse_shader hash")
    sections: list[str] = [
        "Bread current-event Shader representation",
        "representation: explanatory-ir (never compile or apply this file)",
        "eventId: %d" % int(event_id),
        "shaderResourceId: %d" % resource_id,
        "stage: %s" % stage,
        "entryPoint: %s" % str(bridge_response.get("entryPoint", "") or ""),
        "rawSha256: %s" % (raw_sha256 or "unavailable-from-bridge"),
        "",
    ]
    included_sources: list[str] = []
    text_sections: list[dict[str, Any]] = []

    def append_text_section(kind: str, name: str, heading: str, contents: str) -> None:
        prefix = "\n".join(sections) + "\n" + heading + "\n"
        text = contents.rstrip() + "\n"
        text_sections.append({
            "kind": kind, "name": name, "offset": len(prefix.encode("utf-8")),
            "startLine": prefix.count("\n") + 1,
            "byteSize": len(text.encode("utf-8")), "lineCount": text.count("\n"),
        })
        sections.extend((heading, contents.rstrip(), ""))

    source_files = bridge_response.get("sourceFiles")
    if isinstance(source_files, list):
        for index, item in enumerate(source_files[:256]):
            if not isinstance(item, Mapping):
                continue
            filename = str(item.get("filename", "embedded-%d" % index))[:1024]
            contents = item.get("contents")
            if not isinstance(contents, str) or not contents.strip() or "\x00" in contents:
                continue
            try:
                encoded = contents.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                continue
            if len(encoded) > MAX_REPRESENTATION_BYTES:
                diagnostics.append(
                    bounded_diagnostic(
                        "embedded-source-oversized",
                        "RenderDoc embedded source %s exceeded the representation limit"
                        % filename,
                    )
                )
                continue
            included_sources.append(filename)
            append_text_section("embedded-source", filename,
                                "===== Embedded source: %s =====" % filename, contents)

    included_targets: list[str] = []
    disassembly = bridge_response.get("disassembly")
    if isinstance(disassembly, Mapping):
        for target in sorted(disassembly, key=lambda value: str(value).casefold()):
            name = str(target)[:1024]
            value = disassembly[target]
            if not isinstance(value, str) or not value.strip():
                continue
            if value.lstrip().lower().startswith("error:"):
                diagnostics.append(bounded_diagnostic("renderdoc-target-error", "%s: %s" % (name, value)))
                continue
            if "\x00" in value:
                diagnostics.append(bounded_diagnostic("renderdoc-target-nul", name))
                continue
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                diagnostics.append(bounded_diagnostic("renderdoc-target-nonutf8", name))
                continue
            if len(encoded) > MAX_REPRESENTATION_BYTES:
                diagnostics.append(bounded_diagnostic("renderdoc-target-oversized", name))
                continue
            included_targets.append(name)
            append_text_section("renderdoc-disassembly", name,
                                "===== RenderDoc target: %s =====" % name, value)

    lane = "explanatory-ir"
    kind = "renderdoc-source-disassembly-bundle"
    payload = ("\n".join(sections) + "\n").encode("utf-8")
    if not included_sources and not included_targets:
        lane = "binary-only"
        kind = "renderdoc-binary-metadata-only"
        diagnostics.append(
            bounded_diagnostic(
                "no-textual-representation",
                "RenderDoc exposed no embedded source or non-error disassembly. "
                + (
                    "The complete raw Shader bytes were exported and hash-verified."
                    if raw_bytes is not None
                    else "Only the raw hash is retained; binary export was not requested or was unavailable."
                ),
            )
        )
        payload = (
            "\n".join(
                sections
                + [
                    "No textual Shader representation was exposed by RenderDoc.",
                    "This is binary-only metadata, not source or decoded instructions.",
                ]
            )
            + "\n"
        ).encode("utf-8")
    if not payload or len(payload) > MAX_REPRESENTATION_BYTES:
        raise ToolExecutionError("current-event representation exceeds the bounded output limit")

    root = output_root.expanduser().resolve()
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise ToolExecutionError("current-event representation root is redirected")
    root.mkdir(parents=True, exist_ok=True)
    safe_stage = _SAFE_COMPONENT_RE.sub("-", stage.lower()).strip("-") or "shader"
    directory = root / ("e%d-%s-%s" % (int(event_id), safe_stage, uuid.uuid4().hex))
    directory.mkdir(exist_ok=False)
    artifact = directory / ("shader.ir.txt" if lane == "explanatory-ir" else "shader.binary.txt")
    atomic_write_bytes(artifact, payload)
    raw_binary_path = None
    if raw_bytes is not None:
        binary_artifact = directory / "shader.bin"
        atomic_write_bytes(binary_artifact, raw_bytes)
        raw_binary_path = str(binary_artifact)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    # Keep the first response directly useful and small. Subsequent reads use
    # byte offsets in this same immutable artifact, with no additional replay.
    preview = payload[:4096].decode("utf-8", errors="ignore")
    preview_bytes = len(preview.encode("utf-8"))
    capture_digest = None
    if isinstance(capture_status, Mapping):
        encoded_capture = json.dumps(
            capture_status, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        capture_digest = hashlib.sha256(encoded_capture).hexdigest()
    record = {
        "schema": CURRENT_REPRESENTATION_SCHEMA,
        "schemaVersion": REPRESENTATION_VERSION,
        "present": True,
        "lane": lane,
        "kind": kind,
        "path": str(artifact),
        "sha256": artifact_hash,
        "byteSize": len(payload),
        "encoding": "utf-8",
        "eventId": int(event_id),
        "shaderResourceId": resource_id,
        "stage": stage,
        "entryPoint": str(bridge_response.get("entryPoint", "") or ""),
        "rawSha256": raw_sha256 or None,
        "rawBytesAvailable": raw_bytes is not None,
        "rawByteSize": len(raw_bytes) if raw_bytes is not None else None,
        "rawBinaryPath": raw_binary_path,
        "embeddedSourceFiles": included_sources,
        "disassemblyTargets": included_targets,
        "availableDisassemblyTargets": [str(value)[:1024] for value in
                                        list(bridge_response.get("disassemblyTargets") or [])[:32]],
        "textSelection": dict(bridge_response.get("textSelection") or {}),
        "sections": text_sections,
        "textPreview": {"offset": 0, "content": preview, "returnedBytes": preview_bytes,
                        "truncated": preview_bytes < len(payload), "nextOffset": preview_bytes},
        "continuation": {"tool": "read_evidence_file", "path": str(artifact),
                         "offset": preview_bytes, "max_bytes": 16384,
                         "replayRequired": False},
        "provenance": {
            "kind": "fixed-program",
            "component": "qrenderdoc.ReplayController.DisassembleShader",
            "rpc": "reverse_shader",
            "captureStatusSha256": capture_digest,
        },
        "diagnostics": diagnostics[:MAX_DIAGNOSTICS],
        "compileEligible": False,
        "applyEligible": False,
        "abiCompatible": False,
        "semanticEquivalenceVerified": False,
        "aiPolicy": {
            "mayExplainOrOrganize": True,
            "organizedOutputLane": "explanatory-ir",
            "mayCompileOrApply": False,
        },
    }
    descriptor = directory / "representation.json"
    atomic_write_json(descriptor, record)
    record["descriptorPath"] = str(descriptor)
    return record
