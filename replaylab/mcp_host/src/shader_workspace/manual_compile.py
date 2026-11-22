"""Manual save compilation policy; never produces an ABI compatibility proof."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from .compile_source_unit import capture_compilation_unit, current_dependencies
from .errors import ManifestValidationError
from .io import atomic_write_bytes, read_json_object
from .native_compile import api_token

SCOPE = "manual-edit-unvalidated"
MAX_CACHE_ENTRIES = 16


def mark_candidate(candidate):
    candidate.update(manualFastCompile=True, validationScope=SCOPE,
                     requiresReplayAbiValidation=False, replayAbiValidated=False,
                     abiCompatible=False, semanticEquivalenceVerified=False)
    candidate["abiGate"] = {"status": "skipped-manual-edit",
                            "baselineAbiSha256": candidate["abiGate"].get("baselineAbiSha256")}


def compiler_identity(manifest):
    """All compiler, target and original-program inputs, excluding revision."""
    value = {key: manifest.get(key) for key in
             ("workspaceId", "capture", "shader", "stage", "entryPoint", "targetProfile", "routePlan", "tools")}
    value["baselineRawSha256"] = manifest["baseline"].get("rawSha256")
    value["sourceContract"] = {key: (manifest.get("source") or {}).get(key)
                              for key in ("path", "format", "entryPoint", "lane")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()


def cache_key(service, root, manifest, unit):
    # Stat hints invalidate this in-process optimization on compiler/config
    # changes. The normal compiler launch still checks its pinned file hashes.
    paths = {service.registry.path}
    if service.route_config_path is not None:
        paths.add(service.route_config_path)
    plan = manifest.get("routePlan")
    operations = ([service._bound_route_operation(hop) for hop in plan.get("compile", [])]
                  if isinstance(plan, dict) else [service._bound_operation(manifest, "compiler", "compile")])
    for operation in operations:
        paths.add(operation.executable)
        paths.update(path for path, unused in operation.fixed_file_hashes)
    fingerprints = []
    for path in sorted(paths, key=str):
        try:
            stat = path.stat()
            fingerprints.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except OSError:
            # An unavailable external fallback must not block a usable native
            # compiler; its normal launch will diagnose the missing file.
            fingerprints.append((str(path), None, None, None))
    return (str(root), compiler_identity(manifest), unit["documents"][0]["sha256"], unit["digest"],
            tuple(fingerprints), id(service._native_compile_provider), "bound-route-first-interactive-v1")


def remember(service, root, manifest, unit, result):
    candidate = manifest.get("latestCompiled") or {}
    if (candidate.get("revision") != result.get("revision") or candidate.get("manualFastCompile") is not True
            or candidate.get("rawSha256") != result.get("rawSha256") or candidate.get("manualCompileCache")
            or unit["digest"] != (candidate.get("sourceDependencies") or {}).get("digest", "")):
        return
    with service._manual_compile_cache_lock:
        cache = service._manual_compile_cache
        key = cache_key(service, root, manifest, unit)
        cache[key] = copy.deepcopy(candidate)
        cache.move_to_end(key)
        while len(cache) > MAX_CACHE_ENTRIES:
            cache.popitem(last=False)


def reuse(service, root, manifest, source, unit):
    """Publish another revision of exact cached bytes; retain original provenance."""
    from . import workspace as ws
    key = cache_key(service, root, manifest, unit)
    with service._manual_compile_cache_lock:
        cached = copy.deepcopy(service._manual_compile_cache.get(key))
    if cached is None:
        return None
    source_path = service._artifact(root, cached["sourcePath"])
    blob_path = service._artifact(root, cached["blobPath"])
    compiled = blob_path.read_bytes()
    if (source_path.read_bytes() != source or cached["rawSha256"] != hashlib.sha256(compiled).hexdigest()
            or unit["digest"] != (cached.get("sourceDependencies") or {}).get("digest", "")
            or capture_compilation_unit(ws._source_path(root, manifest), source)["digest"] != unit["digest"]):
        with service._manual_compile_cache_lock:
            service._manual_compile_cache.pop(key, None)
        return None
    # A corrupted artifact/provenance invalidates only this optimization; normal
    # compilation remains available. No cache hit becomes strict ABI evidence.
    validation_manifest = dict(manifest, latestCompiled=cached, revision=cached["revision"])
    try:
        service._validate_latest_candidate(root, validation_manifest, cached["revision"],
                                          service._manifest_baseline_abi(manifest))
    except (ManifestValidationError, OSError, ValueError):
        with service._manual_compile_cache_lock:
            service._manual_compile_cache.pop(key, None)
        return None
    before, before_bytes = ws._manifest_transaction_snapshot(root, manifest)
    revision = int(manifest["revision"]) + 1
    directory = root / "revisions" / ("%04d" % revision)
    staging = directory.parent / (".%04d.staging-%s" % (revision, uuid.uuid4().hex))
    published = False
    try:
        staging.mkdir()
        atomic_write_bytes(staging / "shader.bin", compiled)
        atomic_write_bytes(ws._revision_source_path(staging, manifest), source)
        ws._replace_revision_directory(staging, directory)
        published = True
        candidate = copy.deepcopy(cached)
        candidate.update(revision=revision, blobPath=ws._relative(root, directory / "shader.bin"),
                         sourcePath=ws._relative(root, ws._revision_source_path(directory, manifest)),
                         manualCompileCache={"schema": "bread-manual-compile-cache/v1",
                           "compilerIdentitySha256": key[1], "sourceDependenciesDigest": unit["digest"],
                           "sourceCandidate": copy.deepcopy(cached)})
        manifest.update(revision=revision, latestCompiled=candidate)
        service._append_history(root, manifest, {"operation": "compile-manual-cache", "status": "succeeded",
            "revision": revision, "sourceRevision": cached["revision"], "candidate": candidate})
    except BaseException:
        ws._restore_manifest_transaction(root, manifest, before, before_bytes)
        if published:
            shutil.rmtree(directory)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    with service._manual_compile_cache_lock:
        if key in service._manual_compile_cache:
            service._manual_compile_cache.move_to_end(key)
    return {"workspacePath": str(root), "revision": revision, "sourceSha256": candidate["sourceSha256"],
            "candidateBlobPath": str(directory / "shader.bin"), "rawSha256": candidate["rawSha256"],
            "codeSha256": candidate["codeSha256"], "kind": candidate["kind"],
            "manualFastCompile": True, "compilationCacheHit": True,
            "requiresReplayAbiValidation": False, "replayAbiValidated": False,
            "lastGoodRevision": manifest["lastGood"]["revision"]}


def validate_cache(service, root, manifest, candidate, source, compiled, baseline_abi):
    from . import workspace as ws
    record = candidate.get("manualCompileCache") or {}
    original = record.get("sourceCandidate") or {}
    if (record.get("schema") != "bread-manual-compile-cache/v1"
            or candidate.get("manualFastCompile") is not True
            or original.get("manualFastCompile") is not True or original.get("manualCompileCache")
            or record.get("compilerIdentitySha256") != compiler_identity(manifest)
            or record.get("sourceDependenciesDigest") != (candidate.get("sourceDependencies") or {}).get("digest", "")
            or candidate.get("sourceDependencies") != original.get("sourceDependencies")
            or type(original.get("revision")) is not int or original["revision"] >= candidate["revision"]
            or original.get("rawSha256") != candidate.get("rawSha256")
            or original.get("sourceSha256") != candidate.get("sourceSha256")
            or service._artifact(root, original.get("sourcePath")).read_bytes() != source
            or service._artifact(root, original.get("blobPath")).read_bytes() != compiled):
        raise ManifestValidationError("manual compiler cache source/target binding changed")
    original_manifest = dict(manifest, latestCompiled=original, revision=original["revision"])
    service._validate_latest_candidate(root, original_manifest, original["revision"], baseline_abi)


def apply_request(service, root):
    """Read immutable candidate/source bindings without reflecting or comparing ABI."""
    from . import workspace as ws
    manifest = read_json_object(root / "manifest.json", ws.MAX_MANIFEST_BYTES, "workspace manifest")
    if manifest.get("schema") != ws.WORKSPACE_SCHEMA or manifest.get("schemaVersion") != ws.WORKSPACE_VERSION:
        raise ManifestValidationError("unsupported workspace manifest schema/version")
    candidate = manifest.get("latestCompiled")
    if not isinstance(candidate, dict):
        raise ManifestValidationError("compile a candidate before manual apply")
    revision = candidate.get("revision")
    if type(revision) is not int or revision < 1 or manifest.get("revision") != revision:
        raise ManifestValidationError("manual candidate revision changed")
    source_path = ws._source_path(root, manifest)
    source = ws._source_bytes(source_path)
    source_sha = hashlib.sha256(source).hexdigest()
    archived_source = service._artifact(root, candidate.get("sourcePath"))
    dependencies = current_dependencies(source_path, source, candidate)
    if (candidate.get("sourceSha256") != source_sha or archived_source.read_bytes() != source
            or dependencies.get("current") is not True):
        raise ManifestValidationError("Shader workspace no longer contains the expected saved candidate")
    blob = service._artifact(root, candidate.get("blobPath"))
    compiled = blob.read_bytes()
    raw_sha = hashlib.sha256(compiled).hexdigest()
    if (not 0 < len(compiled) <= ws.MAX_COMPILED_BYTES or candidate.get("byteSize") != len(compiled)
            or candidate.get("rawSha256") != raw_sha):
        raise ManifestValidationError("manual candidate artifact changed")
    baseline = manifest.get("baseline") or {}
    original = service._artifact(root, baseline.get("path")).read_bytes()
    key = ws.ShaderKey.from_value(manifest.get("shader"))
    if (hashlib.sha256(original).hexdigest() != baseline.get("rawSha256")
            or key.raw_sha256 != baseline.get("rawSha256")
            or manifest.get("stage") != key.stage or manifest.get("entryPoint") != key.entry_point):
        raise ManifestValidationError("manual candidate original-program binding changed")
    capture = manifest.get("capture") or {}
    api = api_token(capture.get("api"))
    encoding = str(candidate.get("kind") or "").lower()
    allowed = {"d3d11": {"dxbc"}, "d3d12": {"dxbc", "dxil"}, "vulkan": {"spirv"},
               "opengl": {"opengl-spirv", "glsl"}}
    profile = str((manifest.get("targetProfile") or {}).get("profile") or "")
    if api not in allowed or encoding not in allowed[api] or not profile:
        raise ManifestValidationError("manual candidate target format is unsupported")
    binding = {"workspaceId": manifest["workspaceId"], "captureId": capture["captureId"],
               "revision": revision, "candidateRawSha256": raw_sha, "candidateByteSize": len(compiled),
               "stage": manifest["stage"], "entryPoint": manifest["entryPoint"], "api": api,
               "encoding": encoding, "targetProfile": profile,
               "sourceSha256": source_sha, "sourceDependenciesDigest": dependencies["digest"],
               "baselineRawSha256": baseline["rawSha256"], "compilerIdentitySha256": compiler_identity(manifest)}
    return {"schema": "bread-manual-shader-apply-request/v1", "schemaVersion": 1,
            "binding": binding, "params": {"candidate_path": str(blob), "expected_sha256": raw_sha,
            "stage": binding["stage"], "entry_point": binding["entryPoint"], "encoding": encoding,
            "target_profile": profile}, "candidateBlobBase64": base64.b64encode(compiled).decode("ascii"),
            "nativeCompile": copy.deepcopy(candidate.get("nativeCompile")),
            "manualFastCompile": candidate.get("manualFastCompile") is True,
            "validationScope": SCOPE, "replayAbiValidated": False,
            "capturePath": capture["capturePath"], "sourcePath": str(source_path),
            "sourceDependenciesDigest": dependencies["digest"]}
