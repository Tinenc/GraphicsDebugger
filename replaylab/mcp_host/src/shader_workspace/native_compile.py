"""Prefer replay-native source compilation without weakening candidate gates."""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from .compile_source_unit import write_snapshot, validate_snapshot, capture_compilation_unit
from .errors import ManifestValidationError, ToolExecutionError
from .io import atomic_write_bytes, atomic_write_json, read_json_object

SCHEMA = "bread-native-shader-compile/v1"

# These are the stages accepted by the replay drivers' BuildTargetShader path.
# A source compiler can support additional stages (for example DXIL libraries)
# without the replay-native compiler accepting that same source/entry contract.
_PIPELINE_STAGES = {"vertex", "hull", "domain", "geometry", "fragment", "compute"}
_NATIVE_STAGES = {
    "d3d11": _PIPELINE_STAGES,
    "d3d12": _PIPELINE_STAGES | {"task", "mesh"},
    "vulkan": _PIPELINE_STAGES | {"task", "mesh"},
    "opengl": _PIPELINE_STAGES,
}


def api_token(value):
    token = str(value or "").lower().replace("graphicsapi.", "")
    return {"direct3d11": "d3d11", "direct3d12": "d3d12", "vk": "vulkan", "gl": "opengl"}.get(token, token)


def invoke(ipc, request):
    """Bind a native compiler call to the current session, never a cached API."""
    from ..ipc_client import IPCBridgeUnavailableError
    try:
        live = ipc.call("get_capture_status", {"identity_only": True}, timeout=30)
    except IPCBridgeUnavailableError:
        return {"status": "unsupported", "reason": "native-replay-unavailable"}
    if not live.get("loaded"):
        return {"status": "unsupported", "reason": "native-replay-not-loaded"}
    session = str(live.get("captureSessionId") or "")
    if not session:
        raise ToolExecutionError("native compile capture session is unavailable")
    if (api_token(live.get("api")) != request["target_api"]
            or os.path.normcase(os.path.abspath(str(live.get("capturePath") or ""))) !=
               os.path.normcase(os.path.abspath(request["capture_path"]))):
        return {"status": "unsupported", "reason": "native-replay-target-unavailable"}
    response = ipc.call("compile_shader_source_native", {
        **request, "expected_capture_session_id": session}, timeout=300)
    if not isinstance(response, Mapping) or response.get("captureSessionId") != session:
        raise ToolExecutionError("native compiler response belongs to another capture session")
    return response


def _request(manifest, input_path, source, entry):
    plan = manifest.get("routePlan") or {}
    language = plan.get("editableFormat") or "hlsl"
    return {"candidate_path": str(input_path), "expected_sha256": hashlib.sha256(source).hexdigest(),
            "capture_path": manifest["capture"]["capturePath"],
            "target_api": api_token(manifest["capture"]["api"]), "source_format": language,
            "output_format": ("glsl" if api_token(manifest["capture"]["api"]) == "opengl"
                              else str(manifest["baseline"]["kind"]).lower()),
            "target_profile": manifest["targetProfile"]["profile"],
            "stage": manifest["stage"], "entry_point": entry}


def _check_response(request, response):
    expected = {"schema": SCHEMA, "sourceSha256": request["expected_sha256"],
                "sourceFormat": request["source_format"], "targetApi": request["target_api"],
                "stage": request["stage"], "entryPoint": request["entry_point"],
                "targetProfile": request["target_profile"], "outputFormat": request["output_format"],
                "freeTargetResourceSucceeded": True, "replayApplied": False}
    if (not isinstance(response, Mapping)
            or any(response.get(k) != v for k, v in expected.items())
            or not isinstance(response.get("captureSessionId"), str)
            or len(response["captureSessionId"]) != 32
            or os.path.normcase(os.path.abspath(response.get("capturePath", ""))) !=
               os.path.normcase(os.path.abspath(request["capture_path"]))):
        raise ToolExecutionError("native compiler returned mismatched source/capture/profile evidence")


def try_compile(service, root, manifest, source, unit, revision):
    """Return a verified native artifact, or a recorded bounded fallback reason."""
    from . import workspace as ws
    provider = service._native_compile_provider
    language = (manifest.get("routePlan") or {}).get("editableFormat") or "hlsl"
    api = api_token(manifest["capture"]["api"])
    if provider is None or language not in {"hlsl", "glsl"}:
        return None
    if {"d3d11": "hlsl", "d3d12": "hlsl", "vulkan": "glsl", "opengl": "glsl"}.get(api) != language:
        return None
    job = service._new_job_path(root, "native-compile", revision)
    job.mkdir(parents=False, exist_ok=False)
    input_path = job / ("input." + language)
    dependencies = write_snapshot(root, input_path, unit)
    def require_unchanged_source():
        current_path = ws._source_path(root, manifest)
        if ws._source_bytes(current_path) != source:
            raise ManifestValidationError("source changed during native compilation")
        if capture_compilation_unit(current_path, source)["digest"] != unit["digest"]:
            raise ManifestValidationError("include dependencies changed during native compilation")
        validate_snapshot(root, current_path, source, dependencies)
    from .catalog import normalize_stage
    if (normalize_stage(manifest["stage"]) not in _NATIVE_STAGES.get(api, set())
            or str(manifest["targetProfile"]["profile"]).startswith("lib_")):
        # Keep the ordinary registered route in charge of its own stage/profile
        # checks. Skipping this unsupported optimization does not authorize apply.
        response = {"status": "unsupported", "reason": "native-source-stage-unavailable"}
    # BuildTargetShader has no include callback. The external route consumes
    # the already frozen dependency snapshot, rather than flattening directives.
    elif dependencies is not None:
        response = {"status": "unsupported", "reason": "native-target-compiler-has-no-include-handler"}
    else:
        entry = str((manifest.get("source") or {}).get("entryPoint") or manifest["entryPoint"])
        request = _request(manifest, input_path, input_path.read_bytes(), entry)
        response = provider(request)
        require_unchanged_source()
        if not isinstance(response, Mapping):
            raise ToolExecutionError("native compiler returned a non-object")
        if response.get("reason") in {"native-replay-unavailable", "native-replay-not-loaded",
                                      "native-replay-target-unavailable"}:
            if (set(response) != {"status", "reason"} or response.get("status") != "unsupported"
                    or (job / "native.bin").exists()):
                raise ToolExecutionError("native compiler unavailable receipt is inconsistent")
        else:
            _check_response(request, response)
        if response.get("status") == "succeeded":
            if response.get("buildTargetShaderSucceeded") is not True:
                raise ToolExecutionError("native compiler success has no successful target build")
            output = job / "native.bin"
            if output.is_symlink() or not output.is_file() or Path(response.get("outputPath", "")) != output:
                raise ToolExecutionError("native compiler artifact is missing or redirected")
            if not 0 < output.stat().st_size <= ws.MAX_COMPILED_BYTES:
                raise ToolExecutionError("native compiler artifact exceeds its byte limit")
            compiled = output.read_bytes()
            if response.get("rawSha256") != hashlib.sha256(compiled).hexdigest() or response.get("byteSize") != len(compiled):
                raise ToolExecutionError("native compiler artifact digest differs from its response")
            if request["output_format"] == "glsl" and compiled != source:
                raise ToolExecutionError("native GLSL artifact differs from the exact compiled source")
            try:
                container = service._validated_compiled_container(compiled, manifest, "native compiler output",
                                                                  artifact_format=request["output_format"])
            except ToolExecutionError as error:
                response = {**response, "status": "unsupported", "reason": "native-output-contract-mismatch",
                            "diagnostic": str(error)}
            else:
                receipt = {"schema": SCHEMA, "workspaceId": manifest["workspaceId"],
                           "captureId": manifest["capture"]["captureId"],
                           "baselineRawSha256": manifest["baseline"]["rawSha256"],
                           "revision": revision, "request": request, "response": response}
                report = job / "native-compile.json"
                atomic_write_json(report, receipt)
                record = {"reportPath": ws._relative(root, report),
                          "reportSha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                          "jobPath": ws._relative(root, job), "inputPath": ws._relative(root, input_path),
                          "outputPath": ws._relative(root, output)}
                return compiled, container, record
    require_unchanged_source()
    if response.get("status") not in {"unsupported", "failed"}:
        raise ToolExecutionError("native compiler returned an invalid outcome")
    atomic_write_json(job / "native-attempt.json", response)
    from .native_glsl import is_native
    service._append_history(root, manifest, {"operation": "native-compile-attempt", "revision": revision,
        "status": response["status"], "reason": response.get("reason", ""),
        "diagnostic": response.get("buildMessages") or response.get("diagnostic", ""),
        "jobPath": ws._relative(root, job), "fallback": (
            "repair-source-or-reconnect-native-compiler" if is_native(manifest)
            else "registered-capture-compatible-compiler")})
    return None


def compile_current(service, root, manifest, source, unit, *, manual_fast=False):
    """Publish a native candidate under the same transaction and validation rules."""
    from . import workspace as ws
    revision = int(manifest["revision"]) + 1
    result = try_compile(service, root, manifest, source, unit, revision)
    if result is None:
        return None
    compiled, container, record = result
    if not manual_fast:
        service._ensure_dedicated_baseline_dxil_disassembly(root, manifest)
    before, before_bytes = ws._manifest_transaction_snapshot(root, manifest)
    directory = root / "revisions" / ("%04d" % revision)
    if directory.exists():
        raise ManifestValidationError("native compile revision already exists")
    history = {"operation": "compile-native-source", "revision": revision, "nativeCompile": record,
               "routePlanSha256": (manifest.get("routePlan") or {}).get("planSha256"),
               "outputSha256": hashlib.sha256(compiled).hexdigest()}
    published = False
    try:
        if not manual_fast and service._needs_dedicated_dxil_disassembly_evidence(manifest):
            service._attempt_dedicated_dxil_disassembly(root, manifest, role="candidate", revision=revision,
                input_bytes=compiled, input_artifact_path=directory / "shader.bin", history=history)
        if (ws._source_bytes(ws._source_path(root, manifest)) != source
                or capture_compilation_unit(ws._source_path(root, manifest), source)["digest"] != unit["digest"]):
            raise ManifestValidationError("source changed before native candidate publication")
        staging = directory.parent / (".%04d.staging-%s" % (revision, uuid.uuid4().hex))
        staging.mkdir()
        try:
            atomic_write_bytes(staging / "shader.bin", compiled)
            atomic_write_bytes(ws._revision_source_path(staging, manifest), source)
            ws._replace_revision_directory(staging, directory)
            published = True
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        candidate = {"revision": revision, "blobPath": ws._relative(root, directory / "shader.bin"),
            "sourcePath": ws._relative(root, ws._revision_source_path(directory, manifest)),
            "sourceSha256": hashlib.sha256(source).hexdigest(), "rawSha256": hashlib.sha256(compiled).hexdigest(),
            "codeSha256": container.code_sha256, "byteSize": len(compiled), "kind": container.kind,
            "nativeCompile": record, "validationScope": "container-structure-only",
            "requiresReplayAbiValidation": True, "replayAbiValidated": False, "abiCompatible": False,
            "semanticEquivalenceVerified": False, "abiGate": {"status": ws._initial_abi_gate_status(manifest),
                "baselineAbiSha256": manifest["baseline"].get("abiSha256")}}
        if manual_fast:
            from .manual_compile import mark_candidate
            mark_candidate(candidate)
        manifest["revision"] = revision
        manifest["latestCompiled"] = candidate
        history.update(status="succeeded", candidate=candidate)
        service._append_history(root, manifest, history)
    except Exception:
        ws._restore_manifest_transaction(root, manifest, before, before_bytes)
        if published:
            shutil.rmtree(directory)
        raise
    return {"workspacePath": str(root), "revision": revision, "sourceSha256": candidate["sourceSha256"],
            "candidateBlobPath": str(directory / "shader.bin"), "rawSha256": candidate["rawSha256"],
            "codeSha256": container.code_sha256, "kind": container.kind, "jobPath": str(root / record["jobPath"]),
            "compilerBackend": "renderdoc-native", "nativeCompile": record,
            "lastGoodRevision": manifest["lastGood"]["revision"],
            "requiresReplayAbiValidation": not manual_fast, "replayAbiValidated": False,
            "manualFastCompile": manual_fast}


def validate_provenance(service, root, manifest, candidate, source, compiled):
    from . import workspace as ws
    record = candidate.get("nativeCompile")
    if not isinstance(record, dict) or set(record) != {"reportPath", "reportSha256", "jobPath", "inputPath", "outputPath"}:
        raise ManifestValidationError("native compiler provenance is invalid")
    job = service._job_directory(root, record["jobPath"])
    paths = [service._regular_route_artifact(root, job, record[key], "native compiler artifact")
             for key in ("reportPath", "inputPath", "outputPath")]
    report_path, input_path, output_path = paths
    if hashlib.sha256(report_path.read_bytes()).hexdigest() != record["reportSha256"]:
        raise ManifestValidationError("native compiler receipt digest changed")
    receipt = read_json_object(report_path, 128 * 1024, "native compiler receipt")
    entry = str((manifest.get("source") or {}).get("entryPoint") or manifest["entryPoint"])
    request = _request(manifest, input_path, source, entry)
    if (receipt.get("schema") != SCHEMA or receipt.get("request") != request
            or receipt.get("workspaceId") != manifest["workspaceId"]
            or receipt.get("captureId") != manifest["capture"]["captureId"]
            or receipt.get("baselineRawSha256") != manifest["baseline"]["rawSha256"]
            or receipt.get("revision") != candidate["revision"]
            or input_path.read_bytes() != source or output_path.read_bytes() != compiled):
        raise ManifestValidationError("native compiler receipt source/target binding changed")
    response = receipt.get("response")
    try:
        _check_response(request, response)
    except ToolExecutionError as error:
        raise ManifestValidationError(str(error)) from error
    if (response.get("status") != "succeeded" or response.get("buildTargetShaderSucceeded") is not True
            or response.get("rawSha256") != hashlib.sha256(compiled).hexdigest()
            or response.get("byteSize") != len(compiled)):
        raise ManifestValidationError("native compiler receipt does not prove this artifact")
    if request["output_format"] == "glsl" and compiled != source:
        raise ManifestValidationError("native GLSL artifact differs from its source")
    validate_snapshot(root, ws._source_path(root, manifest), source, None)
