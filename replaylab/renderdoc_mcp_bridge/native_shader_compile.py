"""Capture-bound native source compilation, with no replacement side effects."""
import hashlib
import os
import re


SCHEMA = "bread-native-shader-compile/v1"


def compile_source(bridge, params):
    session = str(params.get("expected_capture_session_id") or "")
    if not session or session != bridge._capture_session_id:
        raise RuntimeError("capture session changed before native Shader compilation")
    token = bridge._current_capture_token()
    expected_path = os.path.normcase(os.path.abspath(str(params.get("capture_path") or "")))
    if not token or os.path.normcase(os.path.abspath(token[1])) != expected_path:
        raise RuntimeError("capture path changed before native Shader compilation")
    source_path, source, source_sha = bridge._read_shader_candidate(params)
    if len(source) > 64 * 1024 * 1024 or b"\0" in source:
        raise ValueError("native Shader source must be bounded UTF-8 without NUL")
    source.decode("utf-8", errors="strict")
    output_path = os.path.join(os.path.dirname(source_path), "native.bin")
    if (os.path.basename(os.path.dirname(os.path.dirname(source_path))) != "jobs"
            or not os.path.basename(os.path.dirname(source_path)).startswith("native-compile-")
            or os.path.lexists(output_path)):
        raise ValueError("native compiler output must be a new owned job artifact")
    language = str(params.get("source_format") or "")
    api = str(params.get("target_api") or "")
    stage_name = bridge._normalise_stage_name(params.get("stage"))
    stage = dict(bridge._available_shader_stages([stage_name])).get(stage_name)
    entry = str(params.get("entry_point") or "")
    profile = str(params.get("target_profile") or "")
    expected_encoding = str(params.get("output_format") or "")
    if stage is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", entry):
        raise ValueError("native Shader entry/stage is invalid")
    allowed = {"d3d11": ("hlsl", {"dxbc"}), "d3d12": ("hlsl", {"dxbc", "dxil"}),
               "vulkan": ("glsl", {"spirv"}), "opengl": ("glsl", {"glsl"})}
    if api not in allowed or language != allowed[api][0] or expected_encoding not in allowed[api][1]:
        raise ValueError("native Shader source/target environment is inconsistent")
    if api == "opengl" and (len(source) > 4 * 1024 * 1024 or not source.strip()
                            or profile not in {"glsl", "opengl-spirv"}
                            or stage_name not in {"vertex", "hull", "domain", "geometry", "fragment", "compute"}):
        raise ValueError("native OpenGL GLSL source/profile is invalid")
    if language == "hlsl" and not re.fullmatch(r"(?:vs|hs|ds|gs|ps|cs|as|ms)_[4-6]_[0-9]+", profile):
        raise ValueError("native HLSL requires the exact captured target profile")

    def run(ctrl):
        if bridge._shader_api_token(ctrl) != api:
            raise RuntimeError("capture API changed before native Shader compilation")
        result = {"schema": SCHEMA, "captureSessionId": session, "capturePath": expected_path,
                  "sourceSha256": source_sha, "sourceFormat": language, "targetApi": api,
                  "stage": stage_name, "entryPoint": entry, "targetProfile": profile,
                  "outputFormat": expected_encoding, "status": "unsupported", "reason": "",
                  "buildTargetShaderSucceeded": False, "freeTargetResourceSucceeded": True,
                  "replayApplied": False, "buildMessages": ""}
        encoding = getattr(bridge.rd.ShaderEncoding, "HLSL" if language == "hlsl" else "GLSL", None)
        if encoding is None or encoding not in list(ctrl.GetTargetShaderEncodings()):
            result["reason"] = "source-encoding-unavailable"
            return result
        if language == "glsl" and entry != "main":
            result["reason"] = "native-glsl-requires-main-entry"
            return result
        if re.search(br"(?im)^\s*#\s*include\b", source):
            result["reason"] = "native-target-compiler-has-no-include-handler"
            return result
        flags = bridge.rd.ShaderCompileFlags()
        if language == "hlsl":
            flag = bridge.rd.ShaderCompileFlag()
            flag.name, flag.value = "@cmdline", "/T " + profile
            flags.flags = [flag]
        target = None
        compiled = None
        try:
            target, messages = bridge._build_target_shader(ctrl, entry, encoding, source, flags, stage)
            result["buildMessages"] = str(messages or "")[:16384]
            if not bridge._resource_id_int(target):
                result.update(status="failed", reason="native-compiler-diagnostic")
                return result
            result["buildTargetShaderSucceeded"] = True
            selected = bridge._exact_shader_entry(list(ctrl.GetShaderEntryPoints(target) or []), entry, stage_name)
            reflection = ctrl.GetShader(bridge.rd.ResourceId(), target, selected) if selected is not None else None
            raw = bridge._shader_raw_bytes(reflection) if reflection else b""
            reflected_encoding = str(getattr(reflection, "encoding", "")).lower().split(".")[-1]
            reflected_encoding = {"openglspirv": "opengl-spirv"}.get(reflected_encoding, reflected_encoding)
            if not raw or reflected_encoding != expected_encoding:
                # Driver GLSL programs need not expose a portable compiled binary.
                # Keep the source editable and use the compatible registered route.
                result["reason"] = "native-compiled-artifact-unavailable"
                return result
            if expected_encoding == "glsl" and raw != source:
                result["reason"] = "native-source-reflection-identity-mismatch"
                return result
            if len(raw) > 512 * 1024 * 1024:
                raise RuntimeError("native compiler output exceeds its byte limit")
            compiled = bytes(raw)
            result.update(status="succeeded", reason="", byteSize=len(compiled),
                          rawSha256=hashlib.sha256(compiled).hexdigest())
        finally:
            if bridge._resource_id_int(target):
                try:
                    ctrl.FreeTargetResource(target)
                except Exception:
                    bridge._register_orphan_shader_target(target)
                    raise
            if bridge._capture_session_id != session or bridge._current_capture_token() != token:
                raise RuntimeError("capture changed during native Shader compilation")
        if compiled is not None:
            # Exclusive publication only after the native temporary is freed.
            with open(output_path, "xb") as handle:
                handle.write(compiled)
            result["outputPath"] = output_path
        return result

    return bridge._replay(run)
