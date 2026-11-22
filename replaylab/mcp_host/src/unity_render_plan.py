"""Check resource-version wiring for an Agent-designed Unity effect graph.

No fixed list of effects, capture EIDs, pass count or pipeline templates. This
checks mechanical dependencies only; the Agent must inspect the corresponding
Shader operations and actual Editor implementation before claiming fidelity.
"""
from __future__ import annotations

from typing import Any, Mapping


def validate_render_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise ValueError("render plan must be an object")
    target = plan.get("target", {})
    if not isinstance(target, Mapping) or not all(isinstance(target.get(k), str) and target[k].strip()
            for k in ("pipeline", "editorVersion", "insertionPoint")):
        raise ValueError("target requires the observed pipeline, Editor version and intended insertionPoint")
    passes, external = plan.get("passes", []), plan.get("externalInputs", [])
    if not isinstance(passes, list) or not passes or len(passes) > 1024 or not isinstance(external, list):
        raise ValueError("provide 1 to 1024 passes and a list of externalInputs")
    errors = []
    versions: dict[str, str | None] = {}
    ids: dict[str, dict] = {}

    def names(value, label):
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(label + " must be a list of non-empty resource-version names")
        if len(set(value)) != len(value):
            errors.append({"code": "duplicate-resource-reference", "location": label})
        return value

    for version in names(external, "externalInputs"):
        versions[version] = None
    for item in passes:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("each pass requires a non-empty id")
        name = item["id"]
        if name in ids:
            errors.append({"code": "duplicate-pass-id", "pass": name})
            continue
        ids[name] = item
        names(item.get("reads", []), name + ".reads")
        outputs = names(item.get("writes", []), name + ".writes")
        if not outputs:
            errors.append({"code": "pass-has-no-recorded-output", "pass": name})
        for version in outputs:
            if version in versions:
                errors.append({"code": "resource-version-has-multiple-writers", "resource": version, "pass": name})
            else:
                versions[version] = name
    dependencies = {name: set() for name in ids}
    for name, item in ids.items():
        for version in item.get("reads", []):
            if version not in versions:
                errors.append({"code": "input-has-no-producer", "pass": name, "resource": version})
            elif versions[version] == name:
                errors.append({"code": "pass-reads-its-new-output", "pass": name, "resource": version})
            elif versions[version] is not None:
                dependencies[name].add(versions[version])
    # Resource versions, not allocation names, distinguish ping-pong/history use.
    # A cycle in these versions is a real missing previous-frame/external input.
    remaining = {key: set(value) for key, value in dependencies.items()}
    order = []
    while remaining:
        ready = [name for name, needed in remaining.items() if not needed]
        if not ready:
            errors.append({"code": "dependency-cycle", "passes": list(remaining)})
            break
        for name in ready:
            order.append(name)
            remaining.pop(name)
        for needed in remaining.values():
            needed.difference_update(ready)
    outputs = names(plan.get("outputs", []), "outputs")
    if not outputs:
        errors.append({"code": "effect-output-not-declared"})
    for output in outputs:
        if output not in versions or versions[output] is None:
            errors.append({"code": "effect-output-not-produced", "resource": output})
    resources = plan.get("resources")
    implementation_assets = plan.get("implementationAssets")
    implementation_requirements = []
    detailed = resources is not None or implementation_assets is not None or plan.get("schemaVersion") == 2
    if detailed:
        if not isinstance(resources, list) or len(resources) > 8192:
            raise ValueError("resources must be a bounded list of resource-version descriptors")
        descriptors = {}
        for resource in resources:
            if not isinstance(resource, Mapping) or not isinstance(resource.get("version"), str):
                raise ValueError("each resource descriptor requires a version")
            version = resource["version"]
            if version in descriptors:
                errors.append({"code": "duplicate-resource-descriptor", "resource": version})
            descriptors[version] = resource
            if version not in versions:
                errors.append({"code": "unused-resource-descriptor", "resource": version})
            if not isinstance(resource.get("format"), str) or not resource["format"].strip():
                errors.append({"code": "resource-format-required", "resource": version})
            for dimension in ("width", "height", "samples"):
                if type(resource.get(dimension)) is not int or resource[dimension] < 1:
                    errors.append({"code": "resource-dimension-required", "resource": version, "field": dimension})
            if resource.get("lifetime") not in ("frame", "history", "external"):
                errors.append({"code": "resource-lifetime-required", "resource": version})
            if resource.get("lifetime") == "history" and not resource.get("historyInitialization"):
                errors.append({"code": "history-initialization-required", "resource": version})
        for version in versions:
            if version not in descriptors:
                errors.append({"code": "resource-descriptor-missing", "resource": version})
        if not isinstance(implementation_assets, list) or any(not isinstance(value, str) for value in implementation_assets):
            raise ValueError("implementationAssets must list the manifested project-relative implementation files")
        for asset in implementation_assets:
            if not asset or "\\" in asset or ":" in asset or asset.startswith("/") or any(part in ("", ".", "..") for part in asset.split("/")):
                raise ValueError("implementation asset paths must be canonical relative paths")
        for name, item in ids.items():
            implementation = item.get("implementation")
            if not isinstance(implementation, Mapping):
                errors.append({"code": "pass-implementation-required", "pass": name})
                continue
            asset = implementation.get("asset")
            if asset not in implementation_assets:
                errors.append({"code": "pass-implementation-not-manifested", "pass": name, "asset": asset})
            kind = implementation.get("kind")
            if kind not in ("renderer-feature", "custom-pass", "command-buffer", "compute", "draw", "custom"):
                errors.append({"code": "pass-implementation-kind-required", "pass": name})
            if not isinstance(implementation.get("entryPoint"), str) or not implementation["entryPoint"].strip():
                errors.append({"code": "pass-entry-point-required", "pass": name})
            if not isinstance(item.get("insertionPoint"), str) or not item["insertionPoint"].strip():
                errors.append({"code": "pass-insertion-point-required", "pass": name})
            attachments = item.get("attachments")
            if not isinstance(attachments, list):
                errors.append({"code": "pass-attachment-policy-required", "pass": name})
                attachments = []
            declared_writes = set()
            for attachment in attachments:
                if not isinstance(attachment, Mapping):
                    raise ValueError("pass attachments must be objects")
                version = attachment.get("version")
                if version not in item.get("writes", []):
                    errors.append({"code": "attachment-not-written", "pass": name, "resource": version})
                declared_writes.add(version)
                if attachment.get("load") not in ("load", "clear", "discard") or attachment.get("store") not in ("store", "discard", "resolve"):
                    errors.append({"code": "attachment-load-store-required", "pass": name, "resource": version})
                if attachment.get("load") == "clear" and "clearValue" not in attachment:
                    errors.append({"code": "attachment-clear-value-required", "pass": name, "resource": version})
            for version in set(item.get("writes", [])) - declared_writes:
                errors.append({"code": "output-attachment-policy-missing", "pass": name, "resource": version})
            implementation_requirements.append({"pass": name, "asset": asset, "kind": kind,
                "entryPoint": implementation.get("entryPoint"), "insertionPoint": item.get("insertionPoint"),
                "reads": item.get("reads", []), "writes": item.get("writes", []),
                "runtimeEvidence": "bread_render_capture.passExecution"})
    return {"schema": "bread-unity-render-plan-review/v1", "status": "valid" if not errors else "needs-repair",
            "target": dict(target), "executionOrder": order, "errors": errors,
            "dependencies": {name: sorted(needed) for name, needed in dependencies.items()},
            "descriptorValidation": "checked" if detailed else "not-provided",
            "requiredUnityEvidence": implementation_requirements,
            "semanticValidation": "not-performed", "implementedInUnity": False,
            "visualEquivalenceVerified": False}
