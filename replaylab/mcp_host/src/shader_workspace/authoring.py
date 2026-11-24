"""Convert current editable source through compilers into a separate HLSL document.

The captured binary stays the replacement identity. Conversion always reads the
current saved source, never substitutes an older captured or compiled version.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import uuid

from .compile_source_unit import capture_compilation_unit, validate_snapshot
from .errors import ShaderWorkspaceError, ToolchainRoutingError
from .io import atomic_write_bytes, atomic_write_json, is_within
from .leases import WorkspaceSourceTransactionLease


def retain_function_comments(original: str, converted: str) -> tuple[str, list[str]]:
    """Carry preceding explanations only when one unchanged function name matches.

    This does not rename generated symbols or infer algorithm equivalence. The
    complete original is also retained, including comments inside functions.
    """
    definitions = re.compile(
        r"(?m)^((?:(?:[ \t]*//[^\n]*\n)|(?:[ \t]*/\*[\s\S]*?\*/[ \t]*\n))+)[ \t]*"
        r"(?:[A-Za-z_]\w*[ \t]+)+([A-Za-z_]\w*)[ \t]*\([^;{}]*\)\s*\{"
    )
    comments: dict[str, list[str]] = {}
    for match in definitions.finditer(original):
        comments.setdefault(match.group(2), []).append(match.group(1))
    retained = []
    for name, entries in comments.items():
        if len(entries) != 1:
            continue
        pattern = re.compile(r"(?m)^(?=[ \t]*(?:[A-Za-z_]\w*[ \t]+)+" +
                             re.escape(name) + r"[ \t]*\([^;{}]*\)\s*\{)")
        if len(list(pattern.finditer(converted))) == 1:
            converted = pattern.sub(lambda _: entries[0], converted, count=1)
            retained.append(name)
    return converted, retained


def convert_to_hlsl(service, source_workspace_path, workspace_path, *,
                    expected_source_sha256, host_instance_id=None) -> dict:
    # Imported here to keep workspace's public service free of import cycles.
    from .workspace import (_lock_for, _WorkspaceFileLock, _workspace_creation_identity,
                            _source_path, _source_bytes, _utc_now)

    expected = str(expected_source_sha256 or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ToolchainRoutingError("expected_source_sha256 must be a lowercase SHA-256")
    original = service._workspace_root(source_workspace_path)
    target = (Path(workspace_path).expanduser().resolve() if str(workspace_path or "").strip()
              else original.with_name(original.name + "-hlsl-" + uuid.uuid4().hex[:8]))
    if target == original or is_within(original, target) or is_within(target, original):
        raise ShaderWorkspaceError("HLSL conversion needs a separate sibling workspace")
    if target.exists() or target.is_symlink():
        raise ShaderWorkspaceError("HLSL conversion target already exists: %s" % target)
    with (WorkspaceSourceTransactionLease(original, owner_instance_id=host_instance_id,
                                         recover_orphaned=bool(host_instance_id)),
          _lock_for(original), _WorkspaceFileLock(original)):
        manifest = service._load_manifest(original, allow_stale_route_bindings=True,
                                          validate_latest_candidate=False)
        plan = manifest.get("routePlan") or {}
        if plan.get("editableFormat") != "glsl":
            raise ToolchainRoutingError("language conversion requires an editable GLSL workspace")
        # Native OpenGL GLSL and OpenGL-flavoured SPIR-V cannot be routed through
        # Vulkan semantics. Retain their working source until a native bridge is
        # available rather than silently changing coordinates and buffer layouts.
        if (plan.get("inputFormat") != "spirv"
                or (manifest.get("targetProfile") or {}).get("apiEnvironment") != "vulkan"):
            raise ToolchainRoutingError("this capture has no validated HLSL authoring bridge; the original source is unchanged")
        source_path, source, unused_record = service._source_for_toolchain_migration(original, manifest)
        if hashlib.sha256(source).hexdigest() != expected:
            raise ToolchainRoutingError("source changed before HLSL conversion; save and retry")
        unit = capture_compilation_unit(source_path, source)
        catalog = service._catalog_for_create(None)
        selection = catalog.select_round_trip("spirv", manifest["stage"],
                                              editable_format="hlsl", target_api="vulkan")
        glsl_selection = catalog.select_round_trip("spirv", manifest["stage"],
                                                   editable_format="glsl", target_api="vulkan")
        service.create(manifest["capture"]["catalogPath"], manifest["shader"], target,
                       toolchain_catalog=catalog, toolchain_selection=selection,
                       editable_format="hlsl", _expected_creation_identity=_workspace_creation_identity(manifest))
        receipt = {"schema": "bread-shader-hlsl-conversion/v1", "at": _utc_now(),
                   "sourceWorkspacePath": str(original), "sourceWorkspaceId": manifest["workspaceId"],
                   "previousSourceSha256": expected, "sourcePreserved": True,
                   "semanticEquivalenceVerified": False, "applied": False}
        history = {"operation": "convert-current-source-to-hlsl", "revision": 0, "hops": []}
        try:
            # Establish the usual trusted reconstruction provenance first; the
            # current organized source then replaces it as a normal source edit.
            service.decompile(target)
            target_manifest = service._load_manifest(target)
            baseline = service._artifact(target, target_manifest["baseline"]["path"])
            compile_manifest = copy.deepcopy(target_manifest)
            compile_manifest["routePlan"] = service._route_plan(catalog, glsl_selection, "spirv", manifest["stage"])
            current_binary, source_jobs, current_entry = service._run_route_chain(
                target, compile_manifest, "compile", 0, source, baseline, history,
                entry_point=str(unused_record.get("entryPoint") or manifest["entryPoint"]), source_unit=unit)
            validate_snapshot(target, source_path, source, history.get("sourceDependencies"))
            conversion_history = {"operation": "convert-current-spirv-to-hlsl", "revision": 0, "hops": []}
            converted, conversion_jobs, unused_entry = service._run_route_chain(
                target, target_manifest, "decompile", 0, current_binary, baseline,
                conversion_history, entry_point=current_entry)
            text, comments = retain_function_comments(source.decode("utf-8-sig"), converted.decode("utf-8-sig"))
            converted = text.encode("utf-8")
            inputs = target / "source-inputs"
            inputs.mkdir(exist_ok=True)
            atomic_write_bytes(inputs / "before-hlsl.glsl", source)
            destination = _source_path(target, target_manifest)
            atomic_write_bytes(destination, converted)
            receipt.update({"sourcePath": str(destination), "sourceSha256": hashlib.sha256(converted).hexdigest(),
                            "preservedSourcePath": str(inputs / "before-hlsl.glsl"),
                            "retainedFunctionComments": comments,
                            "sourceCompileJobs": source_jobs, "conversionJobs": conversion_jobs})
            compiled = service.compile(target, prefer_native=False)
            if _source_bytes(source_path) != source:
                raise ToolchainRoutingError("source changed during HLSL conversion; original remains authoritative")
            validate_snapshot(target, source_path, source, history.get("sourceDependencies"))
            receipt["status"] = "compiled-awaiting-replay-validation"
            atomic_write_json(inputs / "hlsl-conversion.json", receipt)
            return {"workspacePath": str(target), "sourcePath": str(destination),
                    "sourceWorkspacePath": str(original), "sourcePreserved": True,
                    "previousSourceSha256": expected, "sourceSha256": receipt["sourceSha256"],
                    "conversionReceiptPath": str(inputs / "hlsl-conversion.json"),
                    "preservedSourcePath": str(inputs / "before-hlsl.glsl"),
                    "compiled": compiled, "status": service.status(target), "applied": False,
                    "semanticEquivalenceVerified": False}
        except Exception as error:
            receipt.update({"status": "failed-original-preserved", "error": str(error), "history": history})
            atomic_write_json(target / "hlsl-conversion-failed.json", receipt)
            raise ShaderWorkspaceError("HLSL conversion failed; original source is unchanged. Details: %s: %s" %
                                       (target / "hlsl-conversion-failed.json", error)) from error
