"""Compile isolated numeric-localization trials without publishing source."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .compile_source_unit import capture_compilation_unit, current_dependencies, execution_context, validate_snapshot, write_snapshot
from .diagnostics import parse_compiler_diagnostics
from .errors import ManifestValidationError, ToolExecutionError
from .io import atomic_write_bytes, atomic_write_json


def numeric_recovery_contract(workspace_path: str | Path, canonical_source_sha256: str,
                              observation: dict[str, Any],
                              proposal_source_sha256: str = "") -> dict[str, Any]:
    """Describe the available trial operation after a failed final comparison."""
    unstable = ((observation.get("baselineStability") or {}).get("withinTolerance") is False
                or observation.get("diagnosticCode") == "baseline-replay-unstable")
    return {
        "schema": "bread-shader-numeric-recovery/v1",
        "tool": "diagnose_shader_organization_candidate",
        "arguments": {"workspace_path": str(workspace_path),
                      "expected_source_sha256": canonical_source_sha256},
        "requiredSourceArgument": "source_text",
        "sourceTextContract": "Full compilable dependency-coherent trial text; canonical source and proposal stay unchanged.",
        "proposalSourceSha256": proposal_source_sha256,
        "sourceHashRole": "expected_source_sha256 binds saved canonical source, not the trial or proposal",
        "completeSourceReviewRequired": False, "diagnosticOnly": True,
        "publicationAllowed": False, "fixedTolerances": True,
        "retryUnchangedSource": False,
        "nextAction": ("stabilize-replay-and-localize-difference" if unstable
                       else "compare-changed-calculation-groups-and-repair"),
    }


def compile_trial(service: Any, workspace_path: str | Path, source_text: str,
                  expected_source_sha256: str, label: str = "") -> dict[str, Any]:
    # Import locally: workspace owns the lock and compiler helpers and exposes
    # this operation through a small lazy method, without a module cycle.
    from . import workspace as ws

    if (not isinstance(source_text, str) or not source_text or "\0" in source_text
            or len(source_text.encode("utf-8")) > ws.MAX_SOURCE_BYTES):
        raise ValueError("diagnostic source must be nonempty bounded UTF-8 text without NUL")
    if (not isinstance(expected_source_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256)):
        raise ValueError("expected_source_sha256 must be the current canonical source SHA-256")
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError("diagnostic label must contain at most 200 characters")
    source = source_text.encode("utf-8")
    root = service._workspace_root(workspace_path)
    with ws._lock_for(root), ws._WorkspaceFileLock(root):
        manifest = service._load_manifest(root)
        canonical_path = ws._source_path(root, manifest)
        canonical_before = ws._source_bytes(canonical_path)
        if hashlib.sha256(canonical_before).hexdigest() != expected_source_sha256:
            raise ManifestValidationError("canonical source changed before diagnostic compilation")
        source_record = manifest.get("source") or {}
        if (source_record.get("lane") != "executable-editable"
                or source_record.get("compileEligible") is not True):
            raise ToolExecutionError("workspace source lane is not compile-eligible")
        if manifest["stage"] not in ("vertex", "fragment"):
            raise ValueError("numeric diagnostic comparison currently supports vertex and fragment shaders")

        revision = int(manifest["revision"]) + 1
        job = service._new_job_path(root, "diagnostic", revision)
        job.mkdir(parents=False, exist_ok=False)
        input_path = job / ("trial" + canonical_path.suffix)
        output_path = job / "compiled.bin"
        baseline_path = service._artifact(root, manifest["baseline"]["path"])
        source_unit = capture_compilation_unit(canonical_path, source)
        dependencies = write_snapshot(root, input_path, source_unit)
        receipt: dict[str, Any] = {
            "schema": "bread-shader-diagnostic-candidate/v1", "ok": False,
            "diagnosticOnly": True, "applied": False, "published": False,
            "comparisonCompleted": False, "phase": "compile",
            "sourcePreserved": True, "workspacePath": str(root),
            "workspaceId": manifest["workspaceId"], "label": label,
            "expectedSourceSha256": expected_source_sha256,
            "sourceSha256": hashlib.sha256(source).hexdigest(),
            "sourcePath": str(input_path), "jobPath": str(job),
            "receiptPath": str(job / "diagnostic.json"),
        }
        history: dict[str, Any] = {"operation": "numeric-diagnostic", "hops": []}
        try:
            if manifest.get("routePlan") is not None:
                service._require_native_spirv_validator_lane(manifest, "compile")
                if service._validator_bindings(manifest):
                    receipt["baselineValidatorJobs"] = service._run_bound_validators(
                        root, manifest, revision, baseline_path.read_bytes(), baseline_path,
                        baseline_path, history, "baselineValidatorJobs", "diagnostic-baseline-spirv-val")
                compiled, provenance, unused_entry = service._run_route_chain(
                    root, manifest, "compile", revision, source, baseline_path, history,
                    entry_point=str(source_record.get("entryPoint") or manifest["entryPoint"]),
                    source_unit=source_unit)
                atomic_write_bytes(output_path, compiled)
                receipt["compileHops"] = provenance
                if service._validator_bindings(manifest):
                    receipt["validatorJobs"] = service._run_bound_validators(
                        root, manifest, revision, compiled, output_path, baseline_path,
                        history, "validatorJobs", "diagnostic-candidate-spirv-val")
            else:
                operation = service._bound_operation(manifest, "compiler", "compile")
                values = service._template_values(root, manifest, revision, job,
                    input_path, output_path, baseline_path)
                outcome = ws.run_operation(operation, values, job,
                    **execution_context(root, source_unit, dependencies))
                receipt.update(stdoutPath=str(outcome.stdout_path), stderrPath=str(outcome.stderr_path))
                service._require_process_success(outcome)
                service._require_job_output(job, output_path, "diagnostic compiler")
                if not 0 < output_path.stat().st_size <= ws.MAX_COMPILED_BYTES:
                    raise ToolExecutionError("diagnostic compiler output exceeds its size limit")
                compiled = output_path.read_bytes()
            container = service._validated_compiled_container(compiled, manifest, str(output_path))
            validate_snapshot(root, canonical_path, source, dependencies)
            if not current_dependencies(canonical_path, source, {"sourceDependencies": dependencies or {}})["current"]:
                raise ManifestValidationError("include dependencies changed during diagnostic compilation")
            if ws._source_bytes(canonical_path) != canonical_before:
                receipt["sourcePreserved"] = False
                raise ManifestValidationError("canonical source changed during diagnostic compilation")
            raw_sha = hashlib.sha256(compiled).hexdigest()
            # Construct only an in-memory request binding. Never publish this
            # synthetic candidate/revision to the workspace manifest.
            bound = deepcopy(manifest)
            bound["revision"] = revision
            bound["latestCompiled"] = {
                "blobPath": ws._relative(root, output_path), "rawSha256": raw_sha,
                "byteSize": len(compiled), "kind": container.kind,
                "abiGate": {"status": "pending-replay-reflection"},
            }
            baseline_abi = service._manifest_baseline_abi(manifest)
            if baseline_abi is None:
                raise ManifestValidationError("baseline physical interface is unavailable for diagnostic comparison")
            request = service._candidate_reflection_request_from_manifest(root, bound, baseline_abi)
            receipt.update(
                ok=True, compiled=True, phase="compiled-trial", candidateBlobPath=str(output_path),
                rawSha256=raw_sha, kind=container.kind, sourceDependencies=dependencies,
                comparisonParams=request["params"],
                baseline={"path": str(baseline_path), "rawSha256": manifest["baseline"]["rawSha256"]},
                capture=dict(manifest["capture"]), shader=dict(manifest["shader"]),
                entryPoint=manifest["entryPoint"], stage=manifest["stage"],
                requiresReplayAbiValidation=True)
        except Exception as error:
            receipt["sourcePreserved"] = ws._source_bytes(canonical_path) == canonical_before
            logs = []
            paths = [job] + [root / str(hop["jobPath"]) for hop in history.get("hops", [])
                            if isinstance(hop, dict) and hop.get("jobPath")]
            # Route failures record the last job before raising.
            match = ws._LIVE_CHECK_JOB_HINT.search(str(error))
            if match:
                hinted = Path(match.group(1).strip())
                if ws.is_within(root / "jobs", hinted.resolve()):
                    paths.append(hinted)
            for directory in dict.fromkeys(paths):
                for name in ("stderr.log", "stdout.log"):
                    path = directory / name
                    if path.is_file():
                        with path.open("rb") as handle:
                            logs.append(handle.read(16384).decode("utf-8", errors="replace"))
            receipt.update(ok=False, compiled=False, error=str(error)[:2000],
                           diagnosticText="\n".join(logs)[:24000])
            receipt["diagnostics"] = parse_compiler_diagnostics(receipt["diagnosticText"], input_path.name) if logs else []
        atomic_write_json(job / "diagnostic.json", receipt)
        return receipt
