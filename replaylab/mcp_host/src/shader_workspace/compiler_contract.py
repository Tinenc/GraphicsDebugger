"""One bound compiler recipe for manual save, diagnostics and value previews.

This is a projection of existing bindings, not a new persistent schema or a
source migration. Old manifests keep their paths, compiler and immutable jobs.
"""
import hashlib
import json
import os
from pathlib import Path

SCHEMA = "bread-shader-compiler-contract/v1"


def external_operations(service, manifest):
    plan = manifest.get("routePlan")
    if isinstance(plan, dict):
        return [service._bound_route_operation(hop) for hop in plan.get("compile", [])]
    return [service._bound_operation(manifest, "compiler", "compile")]


def prefer_native(service, manifest):
    return not external_operations(service, manifest)


def project(service, root, manifest):
    operations = external_operations(service, manifest)
    source = manifest.get("source") or {}
    plan = manifest.get("routePlan") or {}
    baseline = service._artifact(root, manifest["baseline"]["path"])
    steps = []
    for index, operation in enumerate(operations):
        hop = (plan.get("compile") or [None] * len(operations))[index]
        capability = (hop or {}).get("capability") or {}
        steps.append({"program": str(operation.executable), "arguments": list(operation.arguments),
                      "files": [[str(operation.executable), operation.executable_sha256]] +
                               [[str(path), sha] for path, sha in operation.fixed_file_hashes],
                      "inputFormat": capability.get("inputFormat", "hlsl"),
                      "outputFormat": capability.get("outputFormat", str(manifest["baseline"]["kind"]).lower()),
                      "configDigest": operation.config_digest, "toolId": operation.tool_id,
                      "timeoutSeconds": operation.timeout_seconds})
    values = service._template_values(root, manifest, int(manifest["revision"])+1,
        root, root / "input", root / "output", baseline)
    for key in ("input", "output", "job_dir", "entry_point"):
        values.pop(key)
    value = {"schema": SCHEMA, "backend": "bound-route" if steps else "replay-native",
             "workspaceId": manifest["workspaceId"], "originalRawSha256": manifest["baseline"]["rawSha256"],
             "stage": manifest["stage"], "entryPoint": source.get("entryPoint") or manifest["entryPoint"],
             "language": plan.get("editableFormat") or "hlsl", "steps": steps, "values": values,
             "outputFormat": steps[-1]["outputFormat"] if steps else str(manifest["baseline"]["kind"]).lower(),
             "flags": [], "policy": "bound-route-first-interactive-v1"}
    value["environment"] = {name: os.environ[name] for name in
        ("SystemRoot", "SYSTEMROOT", "WINDIR", "ComSpec", "COMSPEC") if os.environ.get(name)}
    # Files and environment participate in every read, including warm cache
    # lookup. A replaced DLL/config/path must never reuse an old preview target.
    value["fingerprints"] = []
    for path in sorted({path for step in steps for path, _ in step["files"]}):
        stat = Path(path).stat()
        value["fingerprints"].append([path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
    value["identitySha256"] = hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode("utf8")).hexdigest()
    return value
