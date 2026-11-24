"""Explicit captured-GLSL identity and native compiler capability, not SPIR-V."""
from .errors import ManifestValidationError, ToolchainRoutingError

NATIVE_SOURCE = "bread-opengl-native-source/v1"


def api(value):
    return str(value or "").lower().replace("graphicsapi.", "") in {"opengl", "gl"}


def plan(stage):
    from .workspace import _canonical_sha256, ROUTE_PLAN_SCHEMA, ROUTE_PLAN_VERSION
    value = {"schema": ROUTE_PLAN_SCHEMA, "schemaVersion": ROUTE_PLAN_VERSION,
             "inputFormat": "glsl", "editableFormat": "glsl", "stage": stage,
             "decompile": [], "compile": [], "validators": [],
             "nativeSource": {"schema": NATIVE_SOURCE, "api": "opengl",
                              "compilerContract": "bread-native-shader-compile/v1"}}
    value["planSha256"] = _canonical_sha256(value)
    return value


def is_native(manifest):
    return isinstance(manifest.get("routePlan"), dict) and "nativeSource" in manifest["routePlan"]


def validate_plan(manifest):
    if (not api(manifest.get("capture", {}).get("api"))
            or manifest.get("baseline", {}).get("kind") != "GLSL"
            or manifest.get("entryPoint") != "main"
            or manifest.get("stage") not in {"vertex", "hull", "domain", "geometry", "fragment", "compute"}
            or manifest.get("tools") != {}
            or manifest.get("routePlan") != plan(manifest["stage"])):
        raise ManifestValidationError("captured GLSL native source route identity is invalid")


def profile(stage, capture_api):
    if not api(capture_api) or stage not in {"vertex", "hull", "domain", "geometry", "fragment", "compute"}:
        raise ToolchainRoutingError("captured GLSL source requires an OpenGL pipeline stage")
    return {"profile": "glsl", "stage": stage, "major": None, "minor": None,
            "programKind": None, "programKindName": stage, "containerFormat": "glsl",
            "apiEnvironment": "opengl", "profileSource": "capture-api-plus-exact-glsl-source"}


def candidate_allowed(manifest, kind):
    return (kind == "GLSL" and api(manifest.get("capture", {}).get("api"))
            and manifest.get("baseline", {}).get("kind") in {"GLSL", "OPENGL-SPIRV"})


def compiler_binding(manifest):
    from .workspace import _canonical_sha256
    value = {"schema": "bread-native-shader-compiler-binding/v1", "schemaVersion": 1,
             "backend": "renderdoc-native", "api": "opengl", "sourceFormat": "glsl",
             "outputFormat": "glsl", "compilerContract": "bread-native-shader-compile/v1",
             "targetProfile": manifest["targetProfile"],
             "routePlanSha256": manifest["routePlan"]["planSha256"],
             "executionAuthority": False, "evidenceOnly": True}
    value["bindingSha256"] = _canonical_sha256(value)
    return value


def reference(service, root, manifest):
    from .workspace import _file_sha256, _source_path
    baseline = service._artifact(root, manifest["baseline"]["path"])
    source = _source_path(root, manifest)
    digest = manifest["baseline"]["rawSha256"]
    return {"workspacePath": str(root), "referenceOnly": True, "sourcePath": str(baseline),
            "relativePath": manifest["baseline"]["path"], "sourceSha256": digest,
            "baselinePath": str(baseline), "baselineRawSha256": digest, "sourceFormat": "glsl",
            "entryPoint": "main", "reusedReference": True, "decompilerExecuted": False,
            "matchesCurrentSource": source.is_file() and _file_sha256(source) == digest,
            "currentSourcePreserved": True, "revision": manifest["revision"],
            "compileEligible": False, "applyEligible": False, "semanticEquivalenceVerified": False}


def check_source(service, root, manifest, source):
    """Compile an unsaved editor buffer without changing a revision or receipt."""
    import hashlib
    import shutil
    from .native_compile import _request, _check_response
    from .errors import ToolExecutionError
    if service._native_compile_provider is None:
        return {"ok": None, "reason": "native replay compiler is unavailable"}
    job = service._new_job_path(root, "native-compile", int(manifest["revision"]) + 1)
    job.mkdir()
    try:
        path = job / "input.glsl"
        path.write_bytes(source)
        request = _request(manifest, path, source, "main")
        result = service._native_compile_provider(request)
        if result.get("status") == "unsupported" and set(result) == {"status", "reason"}:
            return {"ok": None, "reason": result["reason"]}
        _check_response(request, result)
        if result.get("status") == "succeeded":
            output = job / "native.bin"
            if (result.get("buildTargetShaderSucceeded") is not True
                    or result.get("rawSha256") != hashlib.sha256(source).hexdigest()
                    or result.get("byteSize") != len(source)
                    or result.get("outputPath") != str(output)
                    or not output.is_file() or output.is_symlink() or output.read_bytes() != source):
                raise ToolExecutionError("native GLSL live-check source identity changed")
            return {"ok": True, "diagnostics": []}
        if result.get("status") == "failed":
            return {"ok": False, "diagnostics": [{"file": "<workspace>", "line": 0, "column": 0,
                    "severity": "error", "message": result.get("buildMessages") or "Native GLSL compilation failed"}]}
        return {"ok": None, "reason": result.get("reason") or "native compiler is unavailable"}
    finally:
        shutil.rmtree(job)
