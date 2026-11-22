"""Agent-authored resource naming coverage; local facts, never inferred names.

The single sidecar retains the current preparation and a source-bound review.
No compiler, replay read, asset scan or semantic matching runs here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

PATH = "ai/resource-naming-review.json"
SCHEMA = "bread-shader-resource-naming-review/v1"


def _quality_review_guidance():
    """Prompt an Agent judgment; never score or rewrite semantic names here."""
    return {"authority": "agent-review-required", "countsAreCoverageOnly": True,
            "recordIn": "reconciliation", "checks": [
                "Use concise role-first texture/CBuffer labels; retain only qualifiers that distinguish visible values.",
                "Inspect repeated prefixes and descriptions; a shared prefix or dot-path rewrite is not semantic naming.",
                "Keep physical paths, array indices, offsets and user-authored names; shorten display text only with unambiguous context.",
                "Resolve short-name collisions with the smallest meaningful qualifier, never blind truncation or arbitrary counters.",
                "Review distinct array roles before expansion; put usage and provenance in descriptions/evidence, not repeated label suffixes.",
                "Revisit every unnamed consumer family after organization; distinguish unresolved roles from proven unused lanes across all branches and dynamic indices.",
                "Review coverage is not naming completion: report named, reviewed-unused and unresolved lanes separately; a global unknown statement cannot settle available consumer evidence.",
                "Check published labels in their actual display scope; report remaining uncertainty, not success from field counts.",
            ]}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _read(root, relative=PATH):
    from .shader_organization_review import _read_bytes
    _, raw = _read_bytes(Path(root), relative, 1024 * 1024)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Resource naming record must be an object")
    return value


def _write_target(workspace):
    root = Path(workspace)
    target = root / PATH
    for path in (root, target.parent, target):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Resource naming record path is redirected")
    target.resolve().relative_to(root.resolve())
    return target


def prepare(workspace, preparation):
    """Persist the inexpensive begin-time target/configuration, retaining review."""
    from .shader_workspace.io import atomic_write_json
    root = Path(workspace)
    selected = {key: preparation.get(key) for key in ("target", "assetRootConfigured", "assetRootIdentity")}
    try:
        record = _read(root)
    except (OSError, ValueError):
        record = {}
    if record.get("preparation") != selected or record.get("schema") != SCHEMA:
        record.update(schema=SCHEMA, preparation=selected)
        atomic_write_json(_write_target(root), record)


def inventory(source, context=None):
    from .shader_workspace.quality import declared_binding_inventory
    from .shader_workspace.outline import _strip_comments
    text = _strip_comments((context["declarationSource"] if context else source).decode("utf-8-sig", errors="replace"))
    declarations = declared_binding_inventory(text)
    textures = [row for row in declarations if row["kind"] == "texture"]
    # GLSL combined samplers/image uniforms also need an explicit disposition.
    for match in re.finditer(r"(?:layout\s*\(([^)]*)\)\s*)?uniform\s+((?:[iu]?sampler|[iu]?image|texture)\w+)\s+(\w+)\s*(?:\[[^]]*\])?\s*;", text):
        binding = re.search(r"\bbinding\s*=\s*(\d+)", match.group(1) or "")
        descriptor_set = re.search(r"\bset\s*=\s*(\d+)", match.group(1) or "")
        name = match.group(3)
        textures.append({"kind": "texture", "name": name, "renameSelector": "resource:" + name,
                         "type": match.group(2), "bind": int(binding.group(1)) if binding else None,
                         "space": int(descriptor_set.group(1)) if descriptor_set else 0,
                         "generatedPhysicalName": bool(re.fullmatch(r"_?\d+|(?:texture|tex|sampler)_?\d+", name, re.I))})
    return {"cbuffers": [row for row in declarations if row["kind"] == "cbuffer"], "textures": textures,
            "hasConstantBlocks": bool(re.search(r"\b(?:cbuffer|tbuffer)\s+\w+\s*(?::[^{};]+)?\{|\bConstantBuffer\s*<|\buniform\s+\w+\s*\{", text))}


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(label + " requires concise, source-specific evidence")
    return value.strip()


def _asset_evidence(root, assets, preparation):
    paths = assets.get("evidence_paths", [])
    if (not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(path, str) or not path for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError("assets.evidence_paths must list at most eight distinct workspace evidence files")
    if assets["status"] in {"matched", "reused"} and not paths:
        raise ValueError("Matched/reused assets require readable evidence_paths, normally ai/asset-resolution.json; a summary alone is not a match receipt")
    from .shader_organization_review import _read_bytes
    evidence = []
    target = preparation.get("target") or {}
    previous = {item.get("path"): item.get("sha256") for item in assets.get("evidenceFiles", []) if isinstance(item, Mapping)}
    for path in paths:
        resolved, raw = _read_bytes(Path(root), path, 4 * 1024 * 1024)
        relative = resolved.relative_to(Path(root).resolve()).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        if relative in previous and previous[relative] != digest:
            raise ValueError("Asset evidence changed; assess/reuse the new evidence before completing names")
        if relative == "ai/asset-resolution.json":
            value = json.loads(raw)
            if not isinstance(value, Mapping) or value.get("shaderRawSha256") != target.get("rawSha256"):
                raise ValueError("Asset resolution belongs to a different captured Shader")
            for key in ("eventId", "stage"):
                if value.get(key) and value[key] != target.get(key):
                    raise ValueError("Asset resolution belongs to a different Shader use")
            bound_root = (value.get("assetBinding") or {}).get("assetRoot")
            if bound_root and preparation.get("assetRootIdentity"):
                identity = hashlib.sha256(str(Path(bound_root).resolve()).replace("\\", "/").casefold().encode()).hexdigest()
                if identity != preparation["assetRootIdentity"]:
                    raise ValueError("Asset resolution belongs to a different unpacked asset root")
            if assets["status"] in {"matched", "reused"} and value.get("noSupportedMatch") is True:
                raise ValueError("A no-match asset investigation cannot substantiate matched/reused names")
        evidence.append({"path": relative, "sha256": digest})
    return evidence


def _published(root, row, declaration, preparation, cache):
    from .shader_workspace.resource_name_publication import publication_paths
    operation = _text(row.get("operation_id"), "published texture operation_id")
    if operation not in cache:
        from .shader_organization_review import _read_bytes
        manifest_path, ledger_path = publication_paths(Path(root), operation)
        ledger = _read(root, str(ledger_path))
        result = ledger.get("result") or {}
        if (ledger.get("operationId") != operation or ledger.get("phase") != "committed"
                or result.get("complete") is not True
                or Path(str(ledger.get("workspaceRoot") or "")).resolve() != Path(root).resolve()):
            raise ValueError("Texture naming needs a committed publication receipt; finish publish_shader_resource_names first")
        _, raw = _read_bytes(Path(root), str(manifest_path), 32 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != ledger.get("manifestSha256"):
            raise ValueError("Texture publication manifest changed after commit")
        manifest = json.loads(raw)
        cache[operation] = result, manifest
    result, manifest = cache[operation]
    target = preparation.get("target") or {}
    # A saved one-event catalog and the active full catalog have different
    # capture IDs. The committed managed-workspace transaction already proved
    # that relationship; compare its exact Shader/EID/stage/resource tuple.
    for field in ("eventId", "stage"):
        if target.get(field) and result.get(field) != target[field]:
            raise ValueError("Texture naming receipt belongs to another current target")
    matching = [item for item in result.get("rows", []) if isinstance(item, Mapping)
                and item.get("bindNumber", item.get("bind")) == declaration.get("bind")
                and item.get("registerSpace", item.get("space", 0)) == declaration.get("space", 0)
                and item.get("outcome") in {"written", "replacedManaged", "keptUser"}]
    if len(matching) != 1:
        raise ValueError("Texture naming receipt does not identify this Shader binding")
    aliases = [item for item in manifest.get("aliases", []) if isinstance(item, Mapping)
               and item.get("rawSha256") == target.get("rawSha256")
               and item.get("resourceId") == matching[0].get("resourceId")
               and item.get("bindNumber") == declaration.get("bind")
               and item.get("registerSpace", 0) == declaration.get("space", 0)
               and item.get("stage") == target.get("stage")
               and (item.get("metadata") or {}).get("eventId") == target.get("eventId")]
    if len(aliases) != 1:
        raise ValueError("Texture publication has no exact captured Shader/resource/binding evidence")


def _parameter_localization_review(root, assets, preparation):
    """An associated parameter dictionary must be investigated, not just read.

    Unavailable or ambiguous layout evidence is an explicit outcome, never a
    requirement to guess names or block independent source work.
    """
    if preparation.get("assetRootConfigured") is False:
        return None
    try:
        resolution = _read(root, "ai/asset-resolution.json")
    except (OSError, ValueError):
        return None
    context = resolution.get("materialSemanticContext") or {}
    parameters = [item for item in context.get("parameterDictionary", [])
                  if isinstance(item, Mapping) and item.get("kind") in {"scalar", "vector", "integer"}]
    if not parameters:
        return None
    binding, target = context.get("binding") or {}, preparation.get("target") or {}
    if (binding.get("shaderRawSha256") != target.get("rawSha256") or
            binding.get("stage") != target.get("stage")):
        return None  # A different Shader's historical dictionary is not this task's obligation.
    if binding.get("assetRoot") and preparation.get("assetRootIdentity"):
        root_identity = hashlib.sha256(str(Path(binding["assetRoot"]).resolve()).replace("\\", "/").casefold().encode()).hexdigest()
        if root_identity != preparation["assetRootIdentity"]:
            return None
    decision = assets.get("parameter_localization")
    if not isinstance(decision, Mapping) or decision.get("status") not in {"reviewed", "unavailable"}:
        raise ValueError("Material parameters were retained but not localized: use propose_shader_names_from_asset with reuse_material_context=True, inspect each parameterLocalization outcome, then record assets.parameter_localization (reviewed or unavailable with a concrete reason)")
    result = {"status": decision["status"], "evidence": _text(decision.get("evidence"), "parameter_localization.evidence"),
              "parameterCount": len(parameters)}
    if decision["status"] == "unavailable":
        return result
    localization = context.get("parameterLocalization") or {}
    observed = localization.get("binding") or {}
    if (localization.get("schema") != "bread-material-parameter-localization/v1" or
            localization.get("accountedParameterCount") != len(parameters) or
            any(observed.get(key) != binding.get(key) for key in
                ("shaderRawSha256", "eventId", "stage", "parameterDocumentSha256")) or
            observed.get("eventId") != target.get("eventId")):
        raise ValueError("Per-parameter localization is absent or belongs to another EID/dictionary; compare the retained dictionary to current CBuffer values once")
    result.update(statusCounts=localization.get("statusCounts") or {},
                  observationSha256=_digest(localization))
    previous = decision.get("observationSha256")
    if previous and previous != result["observationSha256"]:
        raise ValueError("Parameter locations changed; reconcile the new observations before claiming names complete")
    return result


def make_review(workspace, source, value, preparation, context=None):
    """Validate coverage/receipts only; semantic conclusions belong to the Agent."""
    if not isinstance(value, Mapping):
        raise ValueError("resource_naming_review must be an object")
    from .shader_workspace.naming_delivery import source_context
    context = context or source_context(workspace, source)
    if not context["complete"]:
        raise ValueError("Resource naming include snapshot is unavailable: " + context.get("diagnostic", ""))
    assets = value.get("assets")
    if not isinstance(assets, Mapping) or assets.get("status") not in {
            "not-configured", "matched", "reused", "partial", "no-match", "reader-unavailable"}:
        raise ValueError("assets must record configuration, investigation/reuse outcome and usage")
    configured = preparation.get("assetRootConfigured")
    if assets["status"] == "not-configured" and configured is not False:
        raise ValueError("Configured or unknown asset roots cannot be reported not-configured; inspect the project or record the concrete unavailable reader")
    if configured is False and assets["status"] in {"matched", "reused", "partial"}:
        raise ValueError("No asset root is configured for the claimed asset match")
    asset_files = _asset_evidence(workspace, assets, preparation)
    localization = _parameter_localization_review(workspace, assets, preparation)
    assets = {"status": assets["status"], "evidence": _text(assets.get("evidence"), "assets.evidence"),
              "usage": _text(assets.get("usage"), "assets.usage"),
              "evidence_paths": [item["path"] for item in asset_files], "evidenceFiles": asset_files}
    if localization is not None:
        assets["parameter_localization"] = localization
    declarations = {item["renameSelector"]: item for item in inventory(source, context)["textures"]}
    textures, rows = value.get("textures"), []
    if not isinstance(textures, list) or len(textures) != len(declarations):
        raise ValueError("textures must review every declared texture binding once; use unresolved with a concrete reason for unknown names")
    seen, publications = set(), {}
    for item in textures:
        if not isinstance(item, Mapping) or item.get("selector") not in declarations or item["selector"] in seen:
            raise ValueError("Texture review needs unique current-source resource selectors")
        selector, status = item["selector"], item.get("status")
        if status not in {"published", "retained", "unresolved"}:
            raise ValueError("Texture status must be published, retained or unresolved")
        declaration = declarations[selector]
        if status == "retained" and declaration.get("generatedPhysicalName"):
            raise ValueError("An unchanged physical placeholder is unresolved, not a completed semantic name")
        if status == "published":
            _published(workspace, item, declaration, preparation, publications)
        row = {"selector": selector, "status": status, "evidence": _text(item.get("evidence"), "texture.evidence")}
        if status == "published":
            row["operation_id"] = item["operation_id"]
        rows.append(row)
        seen.add(selector)
    return {"sourceSha256": hashlib.sha256(source).hexdigest(), "preparationSha256": _digest(preparation),
            "sourceDependenciesDigest": context["sourceDependenciesDigest"],
            "assets": assets, "textures": rows, "reconciliation": _text(value.get("reconciliation"), "reconciliation"),
            "author": "agent", "hostSemanticApproval": False}


def record(workspace, source, value):
    from .shader_workspace.io import atomic_write_json
    root = Path(workspace)
    try:
        container = _read(root)
    except (OSError, ValueError):
        container = {"schema": SCHEMA, "preparation": {"assetRootConfigured": None}}
    container["review"] = make_review(root, source, value, container.get("preparation") or {})
    atomic_write_json(_write_target(root), container)
    return delivery_state(root, source)


def delivery_state(workspace, source, context=None):
    from .shader_workspace.naming_delivery import source_context
    context = context or source_context(workspace, source)
    source_sha = hashlib.sha256(source).hexdigest()
    declarations = inventory(source, context)
    required = bool(declarations["hasConstantBlocks"] or declarations["textures"] or not context["complete"])
    try:
        container = _read(workspace)
    except (OSError, ValueError):
        container = {}
    preparation = container.get("preparation") or {}
    required = required or preparation.get("assetRootConfigured") is True
    result = {"required": required, "status": "pending" if required else "not-applicable", "sourceSha256": source_sha,
              "receiptRelativePath": PATH, "tool": "record_shader_resource_naming_review", "recompileRequired": not context["complete"],
              "preparation": preparation,
              "namingQualityReview": _quality_review_guidance(),
              "sourceDependenciesDigest": context["sourceDependenciesDigest"],
              "textureCoverage": {"bindingCount": len(declarations["textures"]), "reviewedBindingCount": 0,
                                  "missingCount": len(declarations["textures"]), "unresolvedCount": 0}}
    if not required:
        return result
    if not context["complete"]:
        return {**result, "reason": "naming-source-dependencies-unavailable", "diagnostic": context.get("diagnostic", ""),
                "nextAction": "repair-or-save-include-closure-and-compile-current-source"}
    review = container.get("review")
    if not isinstance(review, Mapping):
        return {**result, "reason": "resource-naming-review-missing"}
    if review.get("sourceSha256") != source_sha:
        return {**result, "reason": "resource-naming-source-stale"}
    if review.get("sourceDependenciesDigest", "") != context["sourceDependenciesDigest"]:
        return {**result, "reason": "resource-naming-source-dependencies-stale"}
    if review.get("preparationSha256") != _digest(preparation):
        return {**result, "reason": "resource-naming-target-or-assets-changed"}
    try:
        checked = make_review(workspace, source, review, preparation, context)
    except (OSError, ValueError, TypeError) as error:
        return {**result, "reason": "resource-naming-review-incomplete", "diagnostic": str(error)[:400]}
    return {**result, "status": "complete", "reason": "resource-naming-reviewed", "receiptHash": _digest(checked),
            "assetUsage": checked["assets"], "textureCoverage": {
                "bindingCount": len(checked["textures"]), "reviewedBindingCount": len(checked["textures"]),
                "missingCount": 0, "unresolvedCount": sum(row["status"] == "unresolved" for row in checked["textures"])}}


def aggregate(cbuffer, workspace, source, context=None):
    resource = delivery_state(workspace, source, context)
    pending = ((cbuffer.get("required") and cbuffer.get("status") != "complete")
               or (resource["required"] and resource["status"] != "complete"))
    required = bool(cbuffer.get("required") or resource["required"])
    actions = []
    if cbuffer.get("required") and cbuffer.get("status") != "complete":
        actions.append({"tool": "publish_cbuffer_aliases", "purpose": "Review every active binding and choose concise, distinguishable semantic labels; preserve array/physical identity and user names, publish supported fields and account for unresolved lanes; do not recompile"})
    if resource["required"] and resource["status"] != "complete":
        actions.extend([{"tool": "get_shader_workspace_outline", "arguments": {"section": "resource-naming"},
                         "purpose": "Use local binding/configuration inventory; reuse existing asset evidence when current"},
                        {"tool": "record_shader_resource_naming_review", "purpose": "Record asset use and all texture dispositions in one batch; reconciliation explains role clarity, necessary qualifiers, collisions and uncertainty, not only publication counts"}])
    recompile = bool(cbuffer.get("recompileRequired") or resource.get("recompileRequired"))
    if recompile:
        actions = [{"tool": "get_shader_workspace_status", "purpose": "Locate the changed or missing include; save or repair that source before compiling"},
                   {"tool": "compile_shader_workspace", "purpose": "Refresh the bounded include snapshot, then review and publish names for that exact source unit"}]
    return {**cbuffer, "required": required, "status": "pending" if pending else "complete" if required else "not-applicable",
            "recompileRequired": recompile,
            "cbufferStatus": cbuffer.get("status"), "cbufferRequired": bool(cbuffer.get("required")),
            "resourceNamingDelivery": resource, "nextActions": actions,
            **({"reason": resource["reason"]} if resource["required"] and resource["status"] != "complete" and cbuffer.get("status") != "pending" else {})}
