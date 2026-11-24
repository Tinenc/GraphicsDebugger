"""Concrete recovery routes and provider-independent Unity asset authoring.

The planner never certifies a reconstruction. It exposes feasible alternatives
and their executable tools; generated assets remain explicitly derived inputs.
The ordinary compilation, execution, image and semantic checks still decide
whether the user's restoration objective is met.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Sequence
import uuid

PLAN_SCHEMA = "bread-unity-recovery-plan/v1"
ASSET_SCHEMA = "bread-unity-recovery-assets/v1"
_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_EXTENSIONS = {".cs", ".compute", ".shader", ".hlsl", ".cginc", ".glslinc", ".asmdef", ".asmref",
               ".json", ".bytes", ".png", ".exr", ".tga", ".dds"}
_MAX_FILE = 64 * 1024 * 1024
_MAX_TOTAL = 128 * 1024 * 1024


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _gaps(state: Mapping[str, Any]) -> list[dict]:
    result = []
    def visit(value):
        if isinstance(value, Mapping):
            if value.get("code"):
                result.append(dict(value))
            for key in ("detail", "source", "gaps", "errors", "error"):
                if key in value:
                    visit(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(state.get("gaps", []))
    visit(state.get("lastToolFailure", {}))
    return result


def _step(tool: str, arguments: Mapping[str, Any] | None = None, *, author: str = "") -> dict:
    value = {"tool": tool, "arguments": dict(arguments or {})}
    if author:
        value.update(needsAuthoring=True, instruction=author)
    return value


def plan_recovery(state: Mapping[str, Any], capabilities: Mapping[str, Any] | None = None,
                  attempts: Sequence[Mapping[str, Any]] | None = None) -> dict:
    """Rank routes using observed failures, target capabilities and actual attempts.

    Attempts count only if tied to this context and backed by an artifact or an
    observation fingerprint. Repeating a narrative does not exhaust a route.
    The Host owns persistent attempt collection; this function is deterministic.
    """
    if not isinstance(state, Mapping) or not _ID.fullmatch(str(state.get("restorationId", ""))):
        raise ValueError("Recovery requires a persistent restoration identity")
    caps = dict(capabilities or {})
    if isinstance(caps.get("data"), Mapping):
        caps = dict(caps["data"])
    observed = state.get("inspection") or {}
    pipeline = caps.get("renderPipeline") or observed.get("renderPipeline") or "unverified"
    version = caps.get("unityVersion") or observed.get("unityVersion") or "observed-version"
    api = caps.get("graphicsDeviceType") or observed.get("targetGraphicsAPI") or "unverified"
    target_inspection = caps.get("activeBuildTargetInspection") or {}
    if not isinstance(target_inspection, Mapping):
        target_inspection = {}
    target_build = {key: target_inspection.get(key) for key in (
        "target", "group", "supportedInEditor", "usesDefaultGraphicsAPIs", "configuredGraphicsAPIs",
        "graphicsApiConfigurationStatus")}
    gaps = _gaps(state)
    codes = sorted({str(row["code"]) for row in gaps})
    failure_text = json.dumps(gaps, ensure_ascii=False).lower()
    families = []
    for family, needles in (
        ("identity-and-evidence", ("identity", "different-project", "different-capture", "source-mismatch", "hash", "changed")),
        ("connection-and-tooling", ("connect", "mcp", "editor", "tool-unavailable", "package", "provider")),
        ("compute-and-platform", ("dispatch", "compute", "platform", "unsupported", "render-request", "pipeline")),
        ("history-and-inputs", ("history", "counter", "unexported", "unavailable", "layout", "unmapped", "cbuffer-coverage")),
        ("implementation-and-binding", ("compile", "pass", "binding", "parameter", "texture", "configuration", "scene")),
        ("visual-quality", ("render-mismatch", "visual", "effect", "comparison", "quality")),
    ):
        if any(needle in failure_text for needle in needles):
            families.append(family)
    if not families:
        families.append("inspect-current-state")
    context = {"restorationId": state["restorationId"],
        "stateFingerprint": observed.get("stateFingerprint"), "requestSha256": (state.get("binding") or {}).get("requestSha256"),
        "sourceHashes": sorted((str(source.get("stage", "")), str(source.get("sourceSha256", ""))) for source in state.get("sources", [])),
        "gapCodes": codes, "capabilities": {key: caps.get(key) for key in (
            "renderPipeline", "unityVersion", "graphicsDeviceType", "supportsComputeShaders", "supportsGeometryShaders",
            "supportsTessellationShaders", "graphicsShaderLevel", "activeBuildTarget", "targetGraphicsAPIs",
            "graphicsCapabilityScope", "packages")}, "activeBuildTargetInspection": target_build}
    context_hash = _digest(context)
    task, project = state["restorationId"], state.get("projectPath", "")
    check = _step("check_unity_restoration", {"restoration_id": task})
    render = _step("render_unity_restoration", {"restoration_id": task})
    assets = _step("apply_unity_recovery_assets", {"restoration_id": task}, author=(
        "Write the actual derived implementation files and explicit provenance. Include source algorithms, "
        "input initialization, output binding and execution evidence; an empty component or placeholder Shader is not a repair."))
    record = _step("record_unity_reconstruction", {"restoration_id": task}, author=(
        "Record the exact recoverable source gap, inference and implementation files, with the runtime binding, component "
        "or executed pass that proves the replacement is used. Preserve captured facts separately from hypotheses."))
    configure = _step("call_unity_project_tool", {"project_path": project, "tool_name": "bread_apply_capture_configuration",
        "arguments": {"request_path": (state.get("binding") or {}).get("requestPath", ""), "configuration": {}}}, author=(
        "Fill the configuration argument with the derived effect/component/feature, runtime bindings and actual inputs/outputs. "
        "Use authored files' canonical Assets/... assetPath values. This writes a configuration sidecar; do not edit the immutable request."))
    adopt = _step("adopt_unity_restoration_render_plan", {"restoration_id": task}, author=(
        "If the adapter changes the pass graph, bind the actual observed version-2 implementation plan after validating its resources and dependencies."))
    routes = []
    def route(name, family, title, reason, steps, artifacts=(), applicable=True):
        routes.append({"id": name, "family": family, "title": title, "applicable": bool(applicable),
            "reason": reason, "requiredArtifacts": list(artifacts), "steps": steps,
            "verification": ["current Editor compile/inspection", "actual execution or complete parameter readback",
                             "visible output compared under the task's fidelity objective", "current algorithm and inference review"]})
    if "identity-and-evidence" in families:
        route("refresh-exact-evidence", "identity-and-evidence", "Rebind changed evidence", 
            "Re-read and bind the same selected project/capture/source. Wrong identities and changed files are never inferred away.",
            [_step("get_unity_restoration_status", {"restoration_id": task}),
             _step("get_unity_shader_transfer_sources", author="Read the same saved workspaces and current source hashes."),
             _step("bind_unity_restoration", {"restoration_id": task}, author="Use the current verified export revision."), check, render])
    if not caps or pipeline == "unverified" or "connection-and-tooling" in families:
        route("inspect-and-repair-connection", "connection-and-tooling", "Repair or adapt the connected Editor",
            "Read actual target capabilities, package versions and available tools. A missing provider authoring tool can be replaced by first-party asset writes.",
            [_step("start_unity_project_connection", {"project_path": project}),
             _step("call_unity_project_tool", {"project_path": project, "tool_name": "bread_inspect_rendering", "arguments": {}}),
             _step("plan_unity_restoration_recovery", {"restoration_id": task})])
    if "compute-and-platform" in families or "implementation-and-binding" in families:
        route("native-compute-adapter", "compute-and-platform", "Implement a compute dispatch",
            "Keep the captured kernel computation, create the matching ComputeShader/resources/dispatch and bind its visible output.",
            [assets, configure, check, adopt, render, record], [".compute kernel", "dispatch component or render pass", "explicit resource descriptor/binding"],
            caps.get("supportsComputeShaders") is not False)
        route("raster-equivalent-adapter", "compute-and-platform", "Translate to fullscreen or geometry passes",
            "When compute or a pipeline API is unavailable, implement equivalent sampling/arithmetic in supported raster passes; preserve dependencies and assess changed numerical behavior.",
            [assets, configure, check, adopt, render, record], ["target-pipeline Shader", "RendererFeature/CustomPass/command buffer", "intermediate resources"])
        route("cpu-staged-adapter", "compute-and-platform", "Generate required inputs on the CPU",
            "For unsupported GPU operations, use a scoped C# component or authored offline generator to calculate required data, upload it and execute the remaining visible Shader algorithm.",
            [assets, configure, check, adopt, render, record], ["derived C# or generated input data", "GPU upload and binding", "readback evidence"])
    if "history-and-inputs" in families:
        route("reconstruct-input-history", "history-and-inputs", "Reconstruct missing history and input state",
            "Inspect reads/writes, neighboring passes, dimensions and data ranges. Build a documented seed or warm-up sequence, estimate counter/layout state where justified, then verify actual use and visible behavior.",
            [_step("get_resource_export_spec_template", author="Read available resources and physical CBuffer fields from the existing operation."),
             assets, configure, check, adopt, render, record], ["inference rationale and available evidence", "history seed or warm-up producer", "bound persistent resource"])
        route("unity-native-input-adapter", "history-and-inputs", "Use the destination's native scene inputs",
            "After tracing source producers and a concrete portability limit, use justified native inputs with measured local error. Preserve source volume/functions when portable; native SH, TAA or shadows are not automatic substitutes.",
            [assets, configure, check, render, record], ["source-to-Unity interface mapping", "derived Shader and configured scene inputs"])
    if "visual-quality" in families or "implementation-and-binding" in families:
        route("trace-and-port-source-dependencies", "visual-quality", "Port the source producer chain and debug its modules",
            "Trace original resource versions, static volumes, screen/history and compute producers. Capture aligned pass/module outputs; repair common coordinates, sampling, shadows or constants before material tuning. Retain the user's strict fidelity target.",
            [_step("trace_unity_effect_dependencies", author="Supply the actual draw/pass EIDs; inspect producers outside the requested range."),
             _step("call_unity_project_tool", {"project_path": project, "tool_name": "bread_frame_debugger", "arguments": {"action": "inspect"}}),
             _step("compare_unity_effect_layers", author="Use aligned float readbacks and same-input per-pass/module fixtures; inspect ROI/channel errors and shared modules."), assets, configure, render])
        route("diagnose-visible-difference", "visual-quality", "Repair the causal visible difference",
            "Use error regions and effect visibility to separate geometry, projection, texture sampling, lighting and composition differences. Fix one diagnosed cause and compare again.",
            [check, _step("render_unity_restoration", {"restoration_id": task}), assets, configure, check, render],
            ["reference/render and difference diagnostics", "specific causal repair"])
    route("custom-versioned-adapter", "custom-adapter", "Author a target-specific implementation",
        "The built-in route catalogue is not a feature ceiling. Inspect installed package/API source, research primary documentation if needed and implement a project-local adapter with the available file and Editor tools.",
        [assets, configure, check, adopt, render, record], ["version-correct derived implementation", "explicit dependencies and inference provenance"])
    if not state.get("binding") and "export-and-organized-sources-required" in codes:
        # Without an import there is no rendered difference to diagnose. An
        # idle bound CS or an unsupported source validator is not evidence that
        # this raster draw needs a compute/CPU rewrite.
        routes = []
        route("prepare-capture-data", "source-and-export", "Prepare and bind captured data",
            "Finalize the existing export with its mesh, textures, physical constants and native Shader/reflection evidence. "
            "Prefer existing organized/compiled/validated Bread sources. Attempt missing source preparation in Bread first; "
            "on a concrete tool/application limitation, retain the draft and passed checks, record unresolved diagnostics "
            "and continue preparation in Unity without requiring permanent replay apply.",
            [_step("get_resource_export_status", author="Reuse the existing operation ID and inspect its exact finalization gaps."),
             _step("get_unity_shader_transfer_sources", author="Reuse prepared Bread sources. For blocked preparation, select exact retained organization_ids and carry the missing validation/diagnostics into Unity."),
             _step("get_resource_export_spec_template", author="Map captured inputs and author the derived Unity implementation. Do not add idle pipeline stages."),
             _step("finalize_resource_export", author="Finalize the corrected transfer specification; preserve successful captured assets."),
             _step("bind_unity_restoration", {"restoration_id": task}), check, render])
    history = list(attempts if attempts is not None else state.get("recoveryAttempts", []))
    for item in routes:
        previous = [entry for entry in history if isinstance(entry, Mapping) and entry.get("contextFingerprint") == context_hash
            and entry.get("routeId") == item["id"] and (entry.get("artifactHashes") or entry.get("observationFingerprint"))]
        failures = [entry for entry in previous if entry.get("outcome") in {"failed", "no-improvement", "unsupported"}]
        item["attemptCount"] = len(previous)
        item["state"] = "capability-unavailable" if not item["applicable"] else "change-implementation-before-retry" if failures else "available"
    available = [item for item in routes if item["state"] == "available"]
    queries = [{"query": f"Unity {version} {pipeline} {api} {topic}", "preferredDomains": ["docs.unity3d.com", "docs.unity.com", "github.com/Unity-Technologies"]}
               for topic in (("compute shader platform support", "custom render pass API") if "compute-and-platform" in families
                             else ("render texture history initialization", "shader built-in camera lighting inputs"))]
    return {"schema": PLAN_SCHEMA, "restorationId": task, "contextFingerprint": context_hash,
        "target": {"pipeline": pipeline, "editorVersion": version, "graphicsAPI": api,
            "graphicsCapabilityScope": caps.get("graphicsCapabilityScope", "unverified"),
            "activeBuildTarget": caps.get("activeBuildTarget", "unverified"),
            "buildTargetInspection": target_build,
            "targetPlayerVerified": False},
        "gapFamilies": families, "sourceGapCodes": codes, "sourceGapIds": sorted({row["gapId"] for row in gaps if row.get("gapId")}), "routes": routes,
        "suggestedRouteId": available[0]["id"] if available else None, "untriedRouteCount": len(available),
        "researchQueries": queries, "terminal": False,
        "hostSemanticApproval": False, "acceptanceChanged": False,
        "policy": "Author and execute a different practical repair when a tool/platform route is unavailable; inference remains explicit and must pass ordinary execution and visual checks."}


def _regular(path: Path, limit: int = _MAX_FILE) -> bytes:
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Recovery asset path is redirected")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Recovery asset is missing or exceeds its size limit")
    return path.read_bytes()


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise ValueError("Recovery assets require canonical project-relative names")
    if value.casefold() == "bread-recovery-manifest.json":
        raise ValueError("The recovery manifest is reserved for the Host")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts):
        raise ValueError("Recovery asset path escapes its task directory")
    if Path(value).suffix.lower() not in _EXTENSIONS:
        raise ValueError("Unsupported recovery asset extension")
    if any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p, re.I) for p in parts):
        raise ValueError("Recovery asset path contains a reserved device name")
    return value


def write_recovery_assets(project_path: str, restoration_id: str, assets: Sequence[Mapping[str, Any]],
                          purpose: str, expected_revision: str | None = None,
                          provenance: Mapping[str, Any] | None = None) -> dict:
    """Write real task-owned Shader/scripts/data even without provider file tools.

    Writes are scoped to Assets/BreadRecovery/<restorationId>. Existing unrelated
    project files, original capture data and package manifests cannot be targeted.
    Each replacement checks its previous revision and bytes. Scripts compile via
    the normal Editor refresh route; writing files alone is never execution proof.
    """
    from .unity_integration import _project, _file_lock
    from .shader_workspace.io import atomic_write_json
    project = _project(project_path)
    if not _ID.fullmatch(str(restoration_id)):
        raise ValueError("Invalid restoration identity")
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 4000:
        raise ValueError("Recovery assets require a concrete implementation purpose")
    if not isinstance(assets, (list, tuple)) or not 1 <= len(assets) <= 128:
        raise ValueError("Provide 1 to 128 derived assets")
    if expected_revision is not None and not _SHA.fullmatch(str(expected_revision)):
        raise ValueError("expected_revision must be the previous manifest SHA-256 revision")
    provenance = dict(provenance or {"classification": "generated-input", "rationale": purpose})
    aliases = {"inferred-history": "history-reconstruction", "inferred-layout": "inferred-input", "unity-native": "platform-adapter"}
    declared = provenance.get("classification")
    if not isinstance(declared, str):
        raise ValueError("Derived assets require a named provenance classification")
    if declared in aliases:
        provenance.update(classification=aliases[declared], declaredClassification=declared)
    if provenance.get("classification") not in {"ported-code", "platform-adapter", "inferred-input", "history-reconstruction", "generated-input"}:
        raise ValueError("Derived assets must declare generated, inferred, ported or adapter provenance")
    if len(json.dumps(provenance, ensure_ascii=False, allow_nan=False)) > 16000:
        raise ValueError("Recovery provenance is too large")
    root = project / "Assets/BreadRecovery" / restoration_id
    for part in (root, *root.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Recovery asset root is redirected")
    prepared, names, total = [], set(), 0
    for item in assets:
        if not isinstance(item, Mapping):
            raise ValueError("Recovery assets must be objects")
        relative = _relative(item.get("path"))
        if relative.casefold() in names:
            raise ValueError("Duplicate recovery asset path")
        names.add(relative.casefold())
        mechanisms = [key for key in ("content", "contentBase64", "sourcePath") if key in item]
        if len(mechanisms) != 1:
            raise ValueError("Each asset needs content, contentBase64 or sourcePath with sha256")
        if mechanisms[0] == "content":
            if not isinstance(item["content"], str):
                raise ValueError("Asset text must be a string")
            raw = item["content"].encode("utf-8")
        elif mechanisms[0] == "contentBase64":
            if not isinstance(item["contentBase64"], str) or len(item["contentBase64"]) > _MAX_FILE * 2:
                raise ValueError("Binary asset is too large")
            try:
                raw = base64.b64decode(item["contentBase64"], validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError("Invalid binary asset encoding") from error
        else:
            source = Path(str(item["sourcePath"]))
            if not source.is_absolute():
                raise ValueError("Recovery sourcePath must be absolute")
            raw = _regular(source)
            if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                raise ValueError("Recovery source file changed")
        total += len(raw)
        if len(raw) > _MAX_FILE or total > _MAX_TOTAL:
            raise ValueError("Recovery asset size limit exceeded")
        prepared.append((relative, raw, hashlib.sha256(raw).hexdigest(), item.get("expectedSha256")))
    manifest_path = root / "bread-recovery-manifest.json"
    lock = project / "Library/Bread/RecoveryLocks" / (restoration_id + ".lock")
    for candidate in [lock, *(root / row[0] for row in prepared)]:
        for part in (candidate, *candidate.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError("Recovery destination is redirected")
    with _file_lock(lock, threading.Event(), timeout=30):
        previous = json.loads(_regular(manifest_path, 2 * 1024 * 1024)) if manifest_path.exists() else None
        if previous and (previous.get("schema") != ASSET_SCHEMA or previous.get("restorationId") != restoration_id
                         or os.path.normcase(previous.get("projectPath", "")) != os.path.normcase(str(project))):
            raise ValueError("Recovery manifest identity changed")
        if previous and _digest({key: value for key, value in previous.items() if key != "revision"}) != previous.get("revision"):
            raise ValueError("Recovery manifest content changed")
        old_files = {row["relativePath"]: row for row in (previous or {}).get("files", [])}
        old_names = {name.casefold(): name for name in old_files}
        if len(old_names) != len(old_files):
            raise ValueError("Recovery manifest has conflicting cross-platform paths")
        for relative, _, _, _ in prepared:
            if relative.casefold() in old_names and old_names[relative.casefold()] != relative:
                raise ValueError("Recovery asset path differs only by case from an existing asset")
        final_names = set(old_files) | {relative for relative, _, _, _ in prepared}
        if len(final_names) > 4096:
            raise ValueError("Recovery workspace contains too many assets")
        folded_names = {name.casefold() for name in final_names}
        for relative in final_names:
            path = root / relative
            if any(parent.as_posix().casefold() in folded_names for parent in Path(relative).parents if str(parent) != "."):
                raise ValueError("Recovery asset paths conflict as a file and directory")
            if path.exists() and not path.is_file():
                raise ValueError("Recovery asset destination is not a regular file")
            for parent in path.parents:
                if parent.exists() and not parent.is_dir():
                    raise ValueError("Recovery asset parent is not a directory")
        wanted = {relative: sha for relative, _, sha, _ in prepared}
        desired_changed = any(old_files.get(relative, {}).get("sha256") != sha for relative, sha in wanted.items())
        if previous and desired_changed and expected_revision != previous.get("revision"):
            raise ValueError("Recovery assets changed; provide the current expected_revision before replacing them")
        if expected_revision is not None and (previous or {}).get("revision") != expected_revision:
            raise ValueError("Recovery revision is stale")
        overrides = {relative: old_sha for relative, _, _, old_sha in prepared}
        for relative, row in old_files.items():
            path = root / _relative(relative)
            current_sha = hashlib.sha256(_regular(path)).hexdigest()
            if current_sha != row["sha256"] and current_sha != wanted.get(relative) and current_sha != overrides.get(relative):
                raise ValueError("A recovery asset has external edits; read it and provide its exact expectedSha256")
        changes = []
        for relative, raw, sha, expected_sha in prepared:
            destination = root / relative
            if destination.exists():
                old = hashlib.sha256(_regular(destination)).hexdigest()
                if old == sha:
                    continue
                if relative not in old_files and old != expected_sha:
                    raise ValueError("Unmanaged recovery destination already exists")
                if expected_sha is not None and old != expected_sha:
                    raise ValueError("Recovery destination changed after it was read")
            changes.append((destination, raw))
        root.mkdir(parents=True, exist_ok=True)
        for destination, raw in changes:
            destination.parent.mkdir(parents=True, exist_ok=True)
            for part in (destination, *destination.parents):
                if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                    raise ValueError("Recovery destination was redirected")
            temporary = destination.with_name(".bread-write-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(raw)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        files = dict(old_files)
        for relative, raw, sha, _ in prepared:
            files[relative] = {"relativePath": relative, "path": str(root / relative),
                "assetPath": (root / relative).relative_to(project).as_posix(), "sha256": sha, "size": len(raw)}
        for relative, row in files.items():
            if hashlib.sha256(_regular(root / relative)).hexdigest() != row["sha256"]:
                raise ValueError("Recovery asset changed during publication")
        manifest = {"schema": ASSET_SCHEMA, "restorationId": restoration_id, "projectPath": str(project),
                    "purpose": purpose, "provenance": provenance, "provenanceAuthority": "agent-derived",
                    "files": [files[key] for key in sorted(files)]}
        manifest["revision"] = _digest(manifest)
        atomic_write_json(manifest_path, manifest)
        return {**manifest, "manifestPath": str(manifest_path), "changedFiles": len(changes), "idempotent": not changes and previous == manifest,
            "compiled": False, "executed": False, "captureDataModified": False,
            "nextAction": {"tool": "call_unity_project_tool", "arguments": {"project_path": str(project), "tool_name": "bread_refresh_assets", "arguments": {}}}}
