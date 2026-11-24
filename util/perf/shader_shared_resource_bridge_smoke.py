"""Owned --ui-script GPU test of source Bridge shared replacement and Host ABI.

Set BREAD_SHADER_SHARED_SMOKE_CONFIG to JSON containing repository, hostPython,
output and captures [{name,capture,eventId,source,language,profile,encoding}].
The launcher owns the process/profile. No user process or project is accessed.
Host reflection comparison is real; this is not the full public Host apply API.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid


def run(context):
    import renderdoc as rd
    from PySide2 import QtCore, QtWidgets
    config = json.loads(Path(os.environ["BREAD_SHADER_SHARED_SMOKE_CONFIG"]).read_text(encoding="utf-8-sig"))
    root = Path(config["repository"])
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "bread-shared-shader-bridge-gpu/v1", "ok": False,
              "pid": os.getpid(), "scope": "actual-source-Bridge-and-Host-reflection-comparison",
              "fullPublicHostApply": False, "cases": []}
    app = QtWidgets.QApplication.instance()
    bridge = None
    started = time.monotonic()

    def save():
        report["elapsedSeconds"] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    def check(row, name, condition, detail=None):
        row["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        save()
        if not condition:
            raise AssertionError(name + ": " + str(detail))

    def host_compare(path):
        code = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from src.shader_workspace.abi import parse_shader_abi,compare_shader_abi;"
            "p=sys.argv[2];v=json.load(open(p,encoding='utf-8'));"
            "a=parse_shader_abi(v['baselineInterface']);b=parse_shader_abi(v['candidateInterface']);"
            "v['comparison']=compare_shader_abi(a,b).to_dict();"
            "json.dump(v,open(p,'w',encoding='utf-8'),indent=2)"
        )
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("PYTHON")}
        process = subprocess.Popen([config["hostPython"], "-I", "-c", code,
                                    str(root / "replaylab/mcp_host"), str(path)],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode:
            raise RuntimeError("Host ABI subprocess failed: " + stderr.decode("utf-8", "replace") + stdout.decode("utf-8", "replace"))
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        if context.IsCaptureLoaded():
            raise RuntimeError("fixture requires an empty owned context")
        source = root / "replaylab/renderdoc_mcp_bridge/__init__.py"
        spec = importlib.util.spec_from_file_location("_bread_shared_gpu_bridge", str(source))
        bridge = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge
        spec.loader.exec_module(bridge)
        bridge._ctx = context
        report["bridgeSourceSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        report["hostAbiSourceSha256"] = hashlib.sha256(
            (root / "replaylab/mcp_host/src/shader_workspace/abi.py").read_bytes()).hexdigest()
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
                row["catalog"] = catalog

                def original(ctrl):
                    ctrl.SetFrameEvent(event, True)
                    pipe = ctrl.GetPipelineState()
                    shader = pipe.GetShader(rd.ShaderStage.Pixel)
                    ref = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                    return {"resourceId": bridge._resource_id_int(shader),
                            "rawSha256": hashlib.sha256(bridge._shader_raw_bytes(ref)).hexdigest(),
                            "entryPoint": str(ref.entryPoint),
                            "compiledInterface": bridge._compiled_shader_interface(ref)}

                baseline = bridge._replay(original)
                row["baseline"] = baseline
                workspace = uuid.uuid4().hex

                def pixels():
                    def read(ctrl):
                        ctrl.SetFrameEvent(event, True)
                        result = {}
                        for i, target in enumerate(ctrl.GetPipelineState().GetOutputTargets()):
                            if bridge._resource_id_int(target.resource):
                                sub = rd.Subresource()
                                sub.mip = int(target.firstMip)
                                sub.slice = int(target.firstSlice)
                                result[str(i)] = hashlib.sha256(bytes(ctrl.GetTextureData(target.resource, sub))).hexdigest()
                        return result
                    return bridge._replay(read)

                original_pixels = pixels()
                row["originalPixels"] = original_pixels
                lease = None
                for revision, amount in enumerate(("0.1", "0.2", "0.0", "0.3"), 1):
                    src = capture["source"].replace("$DELTA", amount)
                    negative = revision == 4
                    if negative:
                        if capture["language"] == "hlsl":
                            src = src.replace("float4 main(", "uint4 main(").replace(
                                "return tint + float4(0.3,0,0,0);", "return (uint4)(tint + float4(0.3,0,0,0));")
                        else:
                            src = src.replace("out vec4 color", "out uvec4 color").replace(
                                "color=tint+vec4(0.3,0,0,0);", "color=uvec4(tint+vec4(0.3,0,0,0));")
                    job = folder / "jobs" / ("native-compile-" + uuid.uuid4().hex)
                    job.mkdir(parents=True)
                    src_path = job / ("shader.hlsl" if capture["language"] == "hlsl" else "shader.glsl")
                    src_path.write_text(src, encoding="utf-8")
                    api = "opengl" if capture["name"] in ("OpenGL", "OpenGLES") else capture["name"].lower()
                    built = bridge._compile_shader_source_native({
                        "expected_capture_session_id": bridge._capture_session_id,
                        "capture_path": capture["capture"], "candidate_path": str(src_path),
                        "expected_sha256": hashlib.sha256(src_path.read_bytes()).hexdigest(),
                        "source_format": capture["language"], "target_api": api,
                        "stage": "fragment", "entry_point": baseline["entryPoint"],
                        "target_profile": capture["profile"], "output_format": capture["encoding"]})
                    check(row, "nativeCompile:%d" % revision, built["status"] == "succeeded", built)
                    params = {"candidate_path": built["outputPath"], "expected_sha256": built["rawSha256"],
                              "expected_capture_id": catalog["captureId"], "encoding": capture["encoding"],
                              "stage": "fragment", "entry_point": baseline["entryPoint"]}
                    reflected = bridge._reflect_shader_candidate(params)
                    check(row, "nativeReflect:%d" % revision, reflected["reflectionSucceeded"], reflected)
                    evidence = {"baselineInterface": baseline["compiledInterface"],
                                "candidateInterface": reflected["compiledInterface"]}
                    evidence_path = job / "host-abi.json"
                    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
                    proof = host_compare(evidence_path)
                    check(row, "hostPhysicalABI:%d" % revision,
                          proof["comparison"]["compatible"] is (not negative), proof["comparison"])
                    # Re-run the same true reflection against the historical
                    # zero-enum spelling; this labels an explicit legacy-format control.
                    legacy = dict(evidence)
                    legacy["baselineInterface"] = dict(evidence["baselineInterface"], outputTopology="")
                    legacy_path = job / "host-legacy-topology-control.json"
                    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
                    legacy_proof = host_compare(legacy_path)
                    no_topology = not negative and str(evidence["baselineInterface"]["outputTopology"]).lower().split(".")[-1] in ("unknown", "")
                    check(row, "hostLegacyTopologyControl:%d" % revision,
                          legacy_proof["comparison"]["compatible"] is no_topology,
                          {"expectedCompatible": no_topology, "comparison": legacy_proof["comparison"]})
                    request = dict(params, workspace_id=workspace, revision=revision,
                                   event_id=event, expected_original_resource_id=baseline["resourceId"],
                                   expected_original_sha256=baseline["rawSha256"],
                                   abi_request_id=hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest(),
                                   abi_report_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest())
                    if lease is not None:
                        request.update(abi_validation_mode="inline-physical-with-initial-lease",
                                       abi_lease_request_id=lease["abiRequestId"],
                                       abi_lease_candidate_sha256=lease["candidateRawSha256"],
                                       abi_lease_revision=lease["revision"],
                                       expected_physical_abi_sha256=lease["physicalAbiSha256"],
                                       abi_report_sha256=lease["abiReportSha256"])
                    if negative:
                        before_owner = active["replacementId"]
                        diagnostic = ""
                        try:
                            unexpected = bridge._apply_shader_candidate_shared_resource(request)
                        except RuntimeError as error:
                            diagnostic = str(error)
                        check(row, "realBridgeRejectsChangedOutputType", "ABI" in diagnostic, diagnostic)
                        owners = list(bridge._shared_shader_replacements.values())
                        check(row, "rejectedCandidatePreservesOwnerAndPixels",
                              len(owners) == 1 and owners[0]["replacementId"] == before_owner and
                              pixels() == original_pixels)
                        row["negativeHostReport"] = str(evidence_path)
                        continue
                    active = bridge._apply_shader_candidate_shared_resource(request)
                    if lease is None:
                        lease = active
                    actual = pixels()
                    check(row, "renderChanged:%d" % revision if revision < 3 else "sourceRevertExact",
                          actual != original_pixels if revision < 3 else actual == original_pixels,
                          {"pixels": actual, "originalPixels": original_pixels})
                    again = bridge._apply_shader_candidate_shared_resource(request)
                    check(row, "idempotent:%d" % revision, again.get("idempotent") is True and
                          again["replacementId"] == active["replacementId"])
                    row["operations"].append({"revision": revision, "replacement": active,
                                               "hostReport": str(evidence_path), "pixels": actual})
                rollback = bridge._rollback_shader_candidate_shared_resource({
                    "replacement_id": active["replacementId"], "expected_capture_id": catalog["captureId"],
                    "expected_capture_session_id": bridge._capture_session_id, "workspace_id": workspace})
                active = None
                check(row, "rollbackExactPixels", pixels() == original_pixels, rollback)
                check(row, "noBridgeOwner", not bridge._shared_shader_replacements)
                row["ok"] = True
            except BaseException:
                row["error"] = traceback.format_exc()
            finally:
                try:
                    if active is not None:
                        bridge._rollback_shader_candidate_shared_resource({
                            "replacement_id": active["replacementId"],
                            "expected_capture_id": active["captureId"],
                            "expected_capture_session_id": bridge._capture_session_id,
                            "workspace_id": active["workspaceId"]})
                except BaseException:
                    row["cleanupError"] = traceback.format_exc()
                    row["ok"] = False
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
