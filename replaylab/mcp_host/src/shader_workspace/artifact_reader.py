"""Read existing workspace evidence without constructing executable tool routes.

Compiler availability is an execution concern. A changed or removed compiler
must not invalidate exact source/binary/ABI artifacts that were already saved.
This facade intentionally exposes no compile, import, recovery or apply method.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import hashlib
from pathlib import Path
import threading

from .errors import ManifestValidationError, ToolConfigurationError
from .io import read_json_object
from .workspace import (ShaderWorkspaceService, MAX_MANIFEST_BYTES,
                        DXIL_DISASSEMBLY_RECORD_SCHEMA, DXIL_DISASSEMBLY_RECORD_VERSION,
                        DXIL_DISASSEMBLY_EVIDENCE_SCHEMA, DXIL_DISASSEMBLY_EVIDENCE_VERSION,
                        _DXIL_DISASSEMBLY_RECORD_FIELDS, _relative, detect_shader_format)


class _ArtifactValidator(ShaderWorkspaceService):
    def __init__(self):
        # Deliberately do not initialize ToolRegistry or retain launch providers.
        self.route_config_path = None
        self._dxil_access_cache_lock = threading.RLock()
        self._dxil_access_signature_cache = OrderedDict()

    def _process_catalog(self):
        raise ToolConfigurationError("artifact inspection cannot resolve executable tools")

    def _project_compiler_binding(self, manifest):
        from .native_glsl import is_native, candidate_allowed, compiler_binding
        if is_native(manifest) or candidate_allowed(manifest, (manifest.get("latestCompiled") or {}).get("kind")):
            return compiler_binding(manifest)
        # Old route snapshots preserve hashes/profile, but not the full argv
        # template. Never invent a current compiler binding from that evidence.
        return None

    def _project_assembly_disassembly_artifacts(self, root, manifest):
        if self._needs_dedicated_dxil_disassembly_evidence(manifest):
            return super()._project_assembly_disassembly_artifacts(root, manifest)
        # Optional one-way disassembly descriptions can require an argv template
        # absent from old snapshots; leave those out instead of consulting tools.
        return []

    def _dedicated_dxil_asm_evidence(self, root, manifest, role):
        """Bind archived physical IR to exact bytes/jobs, without a new tool run.

        The normal execution service still validates the current tool identity.
        This is only the already-produced evidence used by an artifact reader.
        """
        collection = manifest.get("dxilDisassemblyEvidence")
        if (role not in {"baseline", "candidate"} or not isinstance(collection, Mapping)
                or set(collection) != {"schema", "schemaVersion", "baseline", "candidate"}
                or collection.get("schema") != DXIL_DISASSEMBLY_EVIDENCE_SCHEMA
                or collection.get("schemaVersion") != DXIL_DISASSEMBLY_EVIDENCE_VERSION):
            return None
        record = collection.get(role)
        if not isinstance(record, Mapping) or set(record) != _DXIL_DISASSEMBLY_RECORD_FIELDS:
            return None
        try:
            owner = manifest.get("baseline") if role == "baseline" else manifest.get("latestCompiled")
            revision = 0 if role == "baseline" else manifest["revision"]
            owner_path = self._artifact(root, owner.get("path") if role == "baseline" else owner.get("blobPath"))
            owner_bytes = owner_path.read_bytes()
            digest = hashlib.sha256(owner_bytes).hexdigest()
            plan = manifest.get("routePlan") or {}
            route = record.get("routeBinding")
            operation = route.get("operation") if isinstance(route, Mapping) else None
            capability = route.get("capability") if isinstance(route, Mapping) else None
            if not isinstance(operation, Mapping) or not isinstance(capability, Mapping):
                return None
            expected = {
                "schema": DXIL_DISASSEMBLY_RECORD_SCHEMA,
                "schemaVersion": DXIL_DISASSEMBLY_RECORD_VERSION,
                "evidenceRole": role, "workspacePath": str(root.resolve()),
                "workspaceId": manifest.get("workspaceId"),
                "captureId": manifest["capture"].get("captureId"), "revision": revision,
                "routePlanSha256": plan.get("planSha256"),
                "operation": operation.get("operation"), "toolId": operation.get("toolId"),
                "toolConfigDigest": operation.get("configDigest"),
                "executableSha256": operation.get("executableSha256"),
                "fixedFileHashes": operation.get("fixedFileHashes"),
                "inputArtifactPath": _relative(root, owner_path), "inputFormat": "dxil",
                "inputSha256": digest, "inputByteSize": len(owner_bytes), "outputFormat": "dxil-asm",
                "returnCode": 0, "timedOut": False, "launchError": None, "status": "succeeded"}
            if (any(record.get(k) != v for k, v in expected.items())
                    or digest != owner.get("rawSha256") or len(owner_bytes) != owner.get("byteSize")
                    or owner.get("revision") != revision
                    or capability.get("toolId") != operation.get("toolId")
                    or capability.get("toolConfigDigest") != operation.get("configDigest")
                    or capability.get("inputFormat") != "dxil" or capability.get("outputFormat") != "dxil-asm"):
                return None
            job = self._job_directory(root, record.get("jobPath"))
            payloads = {}
            for name in ("input", "output", "stdout", "stderr"):
                path = self._regular_route_artifact(root, job, record.get(name + "Path"), "archived DXIL " + name)
                payloads[name] = path.read_bytes()
                if hashlib.sha256(payloads[name]).hexdigest() != record.get(name + "Sha256"):
                    return None
            if (payloads["input"] != owner_bytes or len(payloads["output"]) != record.get("outputByteSize")
                    or record.get("inputDetection") != detect_shader_format(payloads["input"], "dxil").as_dict()
                    or record.get("outputDetection") != detect_shader_format(payloads["output"], "dxil-asm").as_dict()):
                return None
            job_path = job / "job.json"
            if job_path.is_symlink() or not job_path.is_file():
                return None
            job_record = read_json_object(job_path, MAX_MANIFEST_BYTES, "archived DXIL job")
            for name in ("toolId", "operation", "toolConfigDigest", "executableSha256", "fixedFileHashes",
                         "command", "status", "returnCode", "launchError", "stdoutTruncated", "stderrTruncated"):
                if job_record.get(name) != record.get(name):
                    return None
            required_job_fields = {"schema", "schemaVersion", "toolId", "operation", "toolConfigDigest",
                                   "executableSha256", "fixedFileHashes", "command", "cwd", "environmentKeys",
                                   "timeoutSeconds", "status", "returnCode", "launchError", "stdoutTruncated",
                                   "stderrTruncated", "stdoutPath", "stderrPath"}
            if (set(job_record) != required_job_fields
                    or job_record.get("schema") != "renderdoc-ultra-shader-job/v1"
                    or job_record.get("schemaVersion") != 1 or job_record.get("cwd") != str(job)
                    or job_record.get("timeoutSeconds") != operation.get("timeoutSeconds")
                    or not isinstance(job_record.get("environmentKeys"), list)
                    or any(not isinstance(key, str) for key in job_record["environmentKeys"])
                    or job_record.get("stdoutPath") != "stdout.log" or job_record.get("stderrPath") != "stderr.log"
                    or job_record.get("stdoutTruncated") is not record.get("stdoutTruncated")
                    or job_record.get("stderrTruncated") is not record.get("stderrTruncated")
                    or not isinstance(job_record.get("command"), list) or not job_record["command"]):
                return None
            history_key = "baselineDxilDisassemblyEvidence" if role == "baseline" else "candidateDxilDisassemblyEvidence"
            history_operations = ({"decompile-route-chain", "baseline-dxil-disassembly-refresh"}
                                  if role == "baseline" else {"compile-route-chain", "project-candidate-import"})
            matches = [item for item in manifest.get("history", []) if isinstance(item, Mapping)
                       and item.get("operation") in history_operations and item.get("status") == "succeeded"
                       and item.get("revision") == revision and item.get("routePlanSha256") == plan.get("planSha256")
                       and (role == "baseline" or item.get("outputSha256") == digest)
                       and item.get(history_key) == record]
            if len(matches) != 1:
                return None
            text = payloads["output"].decode("utf8")
            if not text or "\x00" in text:
                return None
            return payloads["output"], record["outputSha256"]
        except (KeyError, TypeError, AttributeError, ValueError, OSError, ManifestValidationError):
            return None


class ShaderWorkspaceArtifactReader:
    """File-integrity reader; stored ABI results are historical evidence only."""

    def source_edit_status(self, workspace_path: str | Path) -> dict:
        return _ArtifactValidator().source_edit_status(workspace_path)

    def status(self, workspace_path: str | Path) -> dict:
        from ..shader_restore_admission import artifact_status
        return artifact_status(self, workspace_path, self._validated_artifact_status)

    def _validated_artifact_status(self, workspace_path: str | Path) -> dict:
        result = _ArtifactValidator().status(workspace_path, _artifacts_only=True)
        result.update(artifactReadOnly=True, validationScope="stored-artifact-integrity",
                      currentReplayValidated=False, executionAuthority=False,
                      toolchainExecutionValidated=False)
        result["representation"]["applyEligible"] = False
        return result


class _ReplayValidator(_ArtifactValidator):
    """Validate existing binaries against live replay without running compilers."""

    def _load_manifest(self, root, **kwargs):
        kwargs['allow_stale_route_bindings'] = True
        return super()._load_manifest(root, **kwargs)

    def status(self, workspace_path, **kwargs):
        return super().status(workspace_path, _artifacts_only=True)

    def _ensure_dedicated_baseline_dxil_disassembly(self, root, manifest):
        if not self._needs_dedicated_dxil_disassembly_evidence(manifest):
            return None
        existing = self._dedicated_dxil_asm_evidence(root, manifest, 'baseline')
        if existing is None:
            from .errors import AbiValidationError
            raise AbiValidationError('Saved DXIL access evidence is unavailable; recover that evidence before replacement')
        return dict(manifest['dxilDisassemblyEvidence']['baseline'])


class ShaderWorkspaceReplayService(ShaderWorkspaceArtifactReader):
    """Existing-artifact ABI evidence operations; intentionally no compile API."""
    def __init__(self):
        self._validator = _ReplayValidator()

    def manual_candidate_apply_request(self, path):
        # This reads exact saved binary/source bindings; it does not compile
        # or manufacture ABI evidence. The Bridge owns current replay apply.
        return self._validator.manual_candidate_apply_request(path)

    def candidate_reflection_request(self, path):
        return self._validator.candidate_reflection_request(path)

    def validate_candidate_reflection(self, path, response):
        return self._validator.validate_candidate_reflection(path, response)

    def baseline_reflection_request(self, path):
        return self._validator.baseline_reflection_request(path)

    def recover_baseline_reflection(self, path, request, response):
        return self._validator.recover_baseline_reflection(path, request, response)
