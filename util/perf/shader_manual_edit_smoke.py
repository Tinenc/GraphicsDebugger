"""Owned native five-API manual-save smoke test; no user project is accessed.

Run with --ui-script in a fresh isolated qrenderdoc profile. Set
BREAD_SHADER_MANUAL_SMOKE_CONFIG to the shared-Shader capture fixture config
shape (repository, output, captures with name/capture/eventId/source/language/
profile/encoding). Only disposable fixture files under the configured output
directory are written. Compatible with embedded Python 3.6.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid


def run(context):
    import renderdoc as rd
    from PySide2 import QtCore, QtWidgets
    config = json.loads(Path(os.environ["BREAD_SHADER_MANUAL_SMOKE_CONFIG"]).read_text(encoding="utf-8-sig"))
    root = Path(config["repository"])
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "bread-manual-shader-gpu/v1", "ok": False,
              "pid": os.getpid(), "fullPublicHostApply": False,
              "fixtureCompileArtifactReused": False, "cases": []}
    app = QtWidgets.QApplication.instance()
    started = time.monotonic()

    def save():
        report["elapsedSeconds"] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(report, indent=2), encoding="utf8")

    def check(row, name, condition, detail=None):
        row["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        save()
        if not condition:
            raise AssertionError(name + ": " + str(detail))

    try:
        if context.IsCaptureLoaded():
            raise RuntimeError("manual smoke requires an empty owned context")
        source = root / "replaylab/renderdoc_mcp_bridge/__init__.py"
        spec = importlib.util.spec_from_file_location("_bread_manual_gpu_bridge", str(source))
        bridge = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge
        spec.loader.exec_module(bridge)
        bridge._ctx = context
        report["bridgeSourceSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()

        def unexpected_validation(*args, **kwargs):
            raise AssertionError("manual save invoked ABI/replay validation")
        validators = {name: getattr(bridge, name) for name in (
            "_validate_live_shader_event", "_physical_shader_interface_sha256",
            "_physical_shader_interfaces_compatible", "_dxil_ambiguous_interface_compatible")}

        for capture in config["captures"]:
            row = {"name": capture["name"], "checks": [], "operations": [], "ok": False}
            report["cases"].append(row)
            folder = output.parent / capture["name"]
            folder.mkdir(exist_ok=True)
            active = None
            try:
                context.LoadCapture(capture["capture"], rd.ReplayOptions(), capture["capture"], False, True)
                check(row, "captureLoaded", context.IsCaptureLoaded())
                event = int(capture["eventId"])
                context.SetEventID([], event, event, True)
                bridge._synchronise_capture_lifecycle()
                catalog = bridge._export_shader_catalog({"output_dir": str(folder / "catalog"),
                    "event_ids": [event], "stages": ["fragment"]})

                def original(ctrl):
                    pipe = ctrl.GetPipelineState()
                    ref = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                    return {"resourceId": bridge._resource_id_int(pipe.GetShader(rd.ShaderStage.Pixel)),
                            "rawSha256": hashlib.sha256(bridge._shader_raw_bytes(ref)).hexdigest(),
                            "entryPoint": str(ref.entryPoint)}
                baseline = bridge._replay(original)
                for name in validators:
                    setattr(bridge, name, unexpected_validation)
                workspace = uuid.uuid4().hex

                def pixels():
                    def read(ctrl):
                        result = {}
                        # Read the output left by the real apply. Do not force
                        # a replay here that could hide a missing UI refresh.
                        for i, target in enumerate(ctrl.GetPipelineState().GetOutputTargets()):
                            if bridge._resource_id_int(target.resource):
                                sub = rd.Subresource()
                                sub.mip = int(target.firstMip)
                                sub.slice = int(target.firstSlice)
                                result[str(i)] = hashlib.sha256(bytes(ctrl.GetTextureData(target.resource, sub))).hexdigest()
                        return result
                    return bridge._replay(read)

                original_pixels = pixels()
                check(row, "originalOutputAvailable", bool(original_pixels))
                first_pixels = None
                first_compile = None
                first_source_sha = None
                for revision, delta in enumerate(("0.1", "0.2", "0.1", "0.0"), 1):
                    text = capture["source"].replace("$DELTA", delta)
                    job = folder / "jobs" / ("native-compile-" + uuid.uuid4().hex)
                    job.mkdir(parents=True)
                    path = job / ("shader.hlsl" if capture["language"] == "hlsl" else "shader.glsl")
                    path.write_text(text, encoding="utf8")
                    api = "opengl" if capture["name"] in ("OpenGL", "OpenGLES") else capture["name"].lower()
                    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    compile_started = time.monotonic()
                    reused_compile = revision == 3
                    if reused_compile:
                        # Model the Host's actual content-addressed compile
                        # cache with the first real compiler artifact. Native
                        # DXBC compilation can encode the random job path, so
                        # recompiling equal source is not an exact-byte undo.
                        check(row, "fixtureReusesExactCompilerArtifact",
                              first_compile is not None and source_sha == first_source_sha
                              and hashlib.sha256(Path(first_compile["outputPath"]).read_bytes()).hexdigest()
                                  == first_compile["rawSha256"])
                        built = dict(first_compile)
                        report["fixtureCompileArtifactReused"] = True
                    else:
                        built = bridge._compile_shader_source_native({
                            "expected_capture_session_id": bridge._capture_session_id,
                            "capture_path": capture["capture"], "candidate_path": str(path),
                            "expected_sha256": source_sha, "source_format": capture["language"],
                            "target_api": api, "stage": "fragment", "entry_point": baseline["entryPoint"],
                            "target_profile": capture["profile"], "output_format": capture["encoding"]})
                        if revision == 1:
                            first_compile = dict(built)
                            first_source_sha = source_sha
                    compile_ms = (time.monotonic() - compile_started) * 1000
                    check(row, "compile:%d" % revision, built["status"] == "succeeded", built)
                    request = {"candidate_path": built["outputPath"], "expected_sha256": built["rawSha256"],
                        "expected_capture_id": catalog["captureId"],
                        "expected_capture_session_id": bridge._capture_session_id,
                        "event_id": event, "stage": "fragment", "entry_point": baseline["entryPoint"],
                        "encoding": capture["encoding"], "workspace_id": workspace, "revision": revision,
                        "expected_original_resource_id": baseline["resourceId"],
                        "expected_original_sha256": baseline["rawSha256"],
                        "expected_source_sha256": source_sha, "expected_profile": capture["profile"],
                        "abi_validation_mode": "manual-fast"}
                    apply_started = time.monotonic()
                    active = bridge._apply_shader_candidate_shared_resource(request)
                    apply_ms = (time.monotonic() - apply_started) * 1000
                    check(row, "manualUnvalidated:%d" % revision,
                        active.get("manualEdit") is True and active.get("replayAbiValidated") is False
                        and active.get("postApplyAnchorVerified") is False
                        and active.get("freshPhysicalAbiValidationPerformed") is False
                        and "restoreValidationReceipt" not in active, active)
                    actual = pixels()
                    check(row, "output:%d" % revision,
                          actual != original_pixels if revision < 4 else actual == original_pixels,
                          {"actual": actual, "original": original_pixels})
                    if revision == 1:
                        first_pixels = actual
                    if revision == 3:
                        check(row, "undoUsesExactNativeCache", active.get("targetCacheHit") is True
                              and active.get("targetBuiltOnce") is False and actual == first_pixels)
                    repeated = bridge._apply_shader_candidate_shared_resource(request)
                    check(row, "sameBytesAreIdempotent:%d" % revision, repeated.get("idempotent") is True
                          and repeated["replacementId"] == active["replacementId"])
                    row["operations"].append({"revision": revision, "compileMs": compile_ms,
                        "fixtureCompileArtifactReused": reused_compile,
                        "applyMs": apply_ms, "replacement": active, "pixels": actual})

                # Exercise Bridge's metadata update without changing the exact
                # native candidate. This is not a claim about a Host/compiler
                # cache hit; fullPublicHostApply stays false in this fixture.
                metadata_source = text + "\n// unchanged executable, source metadata revision\n"
                metadata_path = folder / ("metadata.hlsl" if capture["language"] == "hlsl" else "metadata.glsl")
                metadata_path.write_text(metadata_source, encoding="utf8")
                metadata_request = dict(request, revision=5,
                    expected_source_sha256=hashlib.sha256(metadata_path.read_bytes()).hexdigest())
                previous_target = active["targetResourceId"]
                active = bridge._apply_shader_candidate_shared_resource(metadata_request)
                check(row, "sameNativeBytesNewSourceMetadata", active.get("metadataOnly") is True
                      and active["targetResourceId"] == previous_target
                      and active.get("targetBuiltOnce") is False
                      and active.get("sourceSha256") == metadata_request["expected_source_sha256"])

                invalid_job = folder / "jobs" / ("native-compile-" + uuid.uuid4().hex)
                invalid_job.mkdir(parents=True)
                invalid_path = invalid_job / path.name
                invalid_path.write_text("this is deliberately invalid shader source @@@", encoding="utf8")
                invalid = bridge._compile_shader_source_native({
                    "expected_capture_session_id": bridge._capture_session_id,
                    "capture_path": capture["capture"], "candidate_path": str(invalid_path),
                    "expected_sha256": hashlib.sha256(invalid_path.read_bytes()).hexdigest(),
                    "source_format": capture["language"], "target_api": api,
                    "stage": "fragment", "entry_point": baseline["entryPoint"],
                    "target_profile": capture["profile"], "output_format": capture["encoding"]})
                rejected = invalid.get("status") != "succeeded"
                if not rejected:
                    # GLSL's compiled artifact can be source; the required
                    # driver compiler runs at BuildTargetShader during apply.
                    invalid_request = dict(request, revision=6,
                        candidate_path=invalid["outputPath"], expected_sha256=invalid["rawSha256"])
                    try:
                        bridge._apply_shader_candidate_shared_resource(invalid_request)
                    except RuntimeError as exc:
                        rejected = True
                        row["expectedCompileError"] = str(exc)
                check(row, "compileErrorKeepsLastGood", rejected and pixels() == original_pixels
                      and bridge._shared_shader_replacements[baseline["resourceId"]]["replacementId"] == active["replacementId"])
                rollback = bridge._rollback_shader_candidate_shared_resource({
                    "replacement_id": active["replacementId"], "expected_capture_id": catalog["captureId"],
                    "expected_capture_session_id": bridge._capture_session_id, "workspace_id": workspace})
                active = None
                check(row, "rollbackExactOutput", pixels() == original_pixels, rollback)
                bridge._release_shared_shader_replacements()
                check(row, "allOwnedTargetsReleased", not bridge._shared_shader_replacements
                      and not bridge._shared_shader_manual_target_cache)
                row["ok"] = True
            except BaseException:
                row["error"] = traceback.format_exc()
            finally:
                for name, callback in validators.items():
                    setattr(bridge, name, callback)
                bridge._release_shared_shader_replacements()
                context.CloseCapture()
                bridge._synchronise_capture_lifecycle()
                app.processEvents()
                save()
        report["ok"] = bool(report["cases"]) and all(row["ok"] for row in report["cases"])
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if context.IsCaptureLoaded():
            context.CloseCapture()
        save()
        context.GetMainWindow().Widget().close()
        QtCore.QCoreApplication.exit(0 if report["ok"] else 1)


if "pyrenderdoc" in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: run(pyrenderdoc))
