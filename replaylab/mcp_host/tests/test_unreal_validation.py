from __future__ import annotations

from array import array
import hashlib
import json
import os
from pathlib import Path
import runpy
import struct
import sys
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from src.unreal_numeric_validation import _percentiles
from src.unreal_validation import _float_compare, _compare
from src.unreal_sequence_validation import review_sequence


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hdr(path, values):
    path.write_bytes(b"".join(struct.pack("<4f", v, v, v, 1) for v in values))
    return path


def compare_pair(tmp_path, values, options=None, width=None, tolerance=0., fraction=0.):
    width = width or len(values)
    reference = hdr(tmp_path / "reference.rgba32f", [0.] * len(values))
    target = hdr(tmp_path / "target.rgba32f", values)
    return _float_compare(reference, sha(reference), target,
                          {"sha256": sha(target), "width": width, "height": len(values) // width},
                          "RGB", tolerance, fraction, options)


def test_hdr_percentiles_preserve_hdr_tail_and_coordinates(tmp_path):
    result = compare_pair(tmp_path, list(range(101)), tolerance=100)
    assert result["p95AbsoluteError"] == 95
    assert result["p99AbsoluteError"] == 99
    assert result["maxAbsoluteError"] == 100  # no display clamp
    assert result["worstPixels"][0]["x"] == 100
    assert result["perChannel"]["R"]["meanAbsoluteError"] == 50
    assert result["withinTolerance"]
    assert _percentiles(array("d", [0.] * 200))["p99AbsoluteError"] == 0
    assert _percentiles(array("d", [0., 10.]))["p95AbsoluteError"] == 9.5


def test_face_mask_prevents_background_diluting_a_local_failure(tmp_path):
    mask = tmp_path / "face.png"
    image = Image.new("L", (10, 10))
    image.putpixel((3, 4), 255)
    image.save(mask)
    options = {"regions": [{"id": "Face", "bounds": [2, 3, 5, 6],
                            "mask": {"path": str(mask), "sha256": sha(mask)}}]}
    result = compare_pair(tmp_path, [10. if i == 43 else 0. for i in range(100)], options,
                          width=10, fraction=.02)
    assert result["wholeFrameWithinTolerance"]
    assert not result["withinTolerance"]
    face = result["regions"][0]
    assert face["pixelCount"] == 1 and face["changedPixelFraction"] == 1
    assert face["changedBounds"] == [3, 4, 4, 5]
    assert face["p99AbsoluteError"] == 10
    assert face["worstPixels"][0]["y"] == 4


def test_numeric_tail_limits_are_used_in_acceptance(tmp_path):
    result = compare_pair(tmp_path, [0.] * 99 + [10.],
                          {"limits": {"maxAbsoluteError": 1}}, fraction=1.)
    assert not result["withinTolerance"] and result["failedLimits"] == ["maxAbsoluteError"]
    result = compare_pair(tmp_path, list(range(101)),
                          {"limits": {"p95AbsoluteError": 94, "p99AbsoluteError": 98}}, fraction=1.)
    assert result["failedLimits"] == ["p95AbsoluteError", "p99AbsoluteError"]
    result = compare_pair(tmp_path, [5.], {"limits": {"p99AbsoluteError": 1},
                                         "regions": [{"id": "Face", "limits": {"maxAbsoluteError": 10}}]}, fraction=1.)
    assert result["regions"][0]["failedLimits"] == ["p99AbsoluteError"]


@pytest.mark.parametrize("options,match", [
    ({"regions": [{"id": "Face", "bounds": [0, 0, 0, 1]}]}, "nonempty"),
    ({"regions": [{"id": "Face", "bounds": [-1, 0, 2, 1]}]}, "inside"),
    ({"regions": [{"id": "Face"}, {"id": "Face"}]}, "unique"),
    ({"limits": {"p99AbsoluteError": float("inf")}}, "finite"),
])
def test_bad_regional_options_are_rejected(tmp_path, options, match):
    with pytest.raises(ValueError, match=match):
        compare_pair(tmp_path, [0., 1.], options)


def test_mask_identity_dimensions_origin_and_empty_coverage_are_checked(tmp_path):
    mask = tmp_path / "mask.png"
    Image.new("L", (2, 1)).save(mask)
    spec = {"path": str(mask), "sha256": sha(mask)}
    for change, match in [({}, "zero pixels"), ({"sha256": "0" * 64}, "hash"),
                          ({"origin": "bottom-left"}, "origin")]:
        with pytest.raises(ValueError, match=match):
            compare_pair(tmp_path, [0., 1.], {"regions": [{"id": "Face", "mask": {**spec, **change}}]})
    Image.new("L", (1, 2), 255).save(mask)
    with pytest.raises(ValueError, match="resolution"):
        compare_pair(tmp_path, [0., 1.], {"regions": [{"id": "Face", "mask": {**spec, "sha256": sha(mask)}}]})


def test_nonfinite_pixels_fail_and_report_is_json_safe(tmp_path):
    result = compare_pair(tmp_path, [0., float("nan"), float("inf")], fraction=1.)
    assert result["nonFinitePixels"] == 2 and not result["withinTolerance"]
    assert result["nonFiniteLocations"] == [{"x": 1, "y": 0}, {"x": 2, "y": 0}]
    json.dumps(result, allow_nan=False)
    with pytest.raises(ValueError, match="EXR"):
        p = tmp_path / "not-hdr.exr"
        Image.new("RGB", (2, 1)).save(p, format="PNG")
        from src.unreal_validation import _float_pixels
        list(_float_pixels(p, sha(p), 2, 1, "RGB"))


def test_comparison_api_checks_preflight_report_hash_and_regional_options(tmp_path):
    reference = hdr(tmp_path / "reference.rgba32f", [0., 0.])
    target = hdr(tmp_path / "target.rgba32f", [0., 5.])
    report = tmp_path / "preflight.json"
    report.write_text('{"passed":true}')
    image = {"name": "HDR", "path": str(target), "sha256": sha(target), "width": 2, "height": 1,
             "encoding": "final-linear-hdr", "materialPreflight": {"passed": True, "path": str(report), "sha256": sha(report)}}
    receipt = {"images": [image], "operationId": "test", "jobId": "test"}
    context = {"captureId": "test", "eventId": 1, "view": "front", "displayTransform": "none",
               "temporalState": "steady", "passMeaning": "HDR", "encoding": "final-linear-hdr"}
    result = _compare(receipt, tmp_path, "HDR", str(reference), sha(reference), "face", "exact",
                      context, context, 0., 0., "RGB", "", numeric_options={"regions": [{"id": "Face", "bounds": [0, 0, 1, 1]}]})
    assert result["withinTolerance"] and not result["wholeFrameWithinTolerance"]
    assert result["materialPreflightStatus"] == "verified-report"
    report.write_text('{"passed":false}')
    with pytest.raises(ValueError, match="hash"):
        _compare(receipt, tmp_path, "HDR", str(reference), sha(reference), "face", "exact",
                 context, context, 0., 0., "RGB", "")


def test_uncompressed_mixed_half_float_exr_retains_numeric_components(tmp_path):
    from src.unreal_validation import _float_pixels
    def attr(name, type_, value):
        return name.encode() + b"\0" + type_.encode() + b"\0" + struct.pack("<I", len(value)) + value
    channels = b"".join(name.encode() + b"\0" + struct.pack("<iB3xii", code, 0, 1, 1)
                        for name, code in [("B", 1), ("G", 2), ("R", 2)]) + b"\0"
    header = (struct.pack("<II", 20000630, 2) + attr("channels", "chlist", channels)
              + attr("compression", "compression", b"\0")
              + attr("dataWindow", "box2i", struct.pack("<4i", 0, 0, 1, 0)) + b"\0")
    pixel_bytes = struct.pack("<2e4f", 2.5, -3.5, .125, .25, 100., -10.)
    path = tmp_path / "mixed.exr"
    path.write_bytes(header + struct.pack("<Q", len(header) + 8) + struct.pack("<iI", 0, len(pixel_bytes)) + pixel_bytes)
    assert list(_float_pixels(path, sha(path), 2, 1, "RGB")) == [(100., .125, 2.5), (-10., .25, -3.5)]
    with pytest.raises(ValueError, match="dimensions"):
        list(_float_pixels(path, sha(path), 1, 2, "RGB"))
    data = bytearray(path.read_bytes())
    struct.pack_into("<I", data, len(header) + 8 + 4, 4)
    path.write_bytes(data)
    with pytest.raises(ValueError, match="scanline"):
        list(_float_pixels(path, sha(path), 2, 1, "RGB"))


def test_material_preflight_service_preserves_failed_result_status(tmp_path, monkeypatch):
    from src.unreal_integration import UnrealService
    from src import unreal_live_connection
    service = UnrealService()
    project = tmp_path / "Test.uproject"
    project.write_text('{}')
    monkeypatch.setattr(unreal_live_connection, "discover", lambda *a: None)
    monkeypatch.setattr(service, "start", lambda path, editor, operation: operation(project, {"editor": "test", "jobId": "fixture"}))
    monkeypatch.setattr(service, "_run_editor_python", lambda *a: {"status": "executed", "result": {"passed": False}})
    result = service.preflight(str(project), "test", ["/Game/Fixture/M_Test"])
    assert result["status"] == "material-preflight-blocked"
    with pytest.raises(ValueError, match="asset paths"):
        service.preflight(str(project), "test", ["/Game/../OtherProject/M_Test"])


def test_tools_have_the_correct_effect_class_and_discoverable_schemas():
    from replaylab_agent.policy import READ_ONLY_TOOLS, tool_call_effects, tool_effects_are_host_known
    from src import server
    for name in ["get_unreal_validation_schema", "review_unreal_hdr_sequence"]:
        assert name in READ_ONLY_TOOLS and tool_call_effects(name, {}) == frozenset()
    assert tool_effects_are_host_known("preflight_unreal_materials")
    assert tool_call_effects("preflight_unreal_materials", {}) == frozenset({"project"})
    result = json.loads(server.get_unreal_validation_schema())
    # The Host's compact success envelope contains the actual JSON schemas.
    assert "p99AbsoluteError" in json.dumps(result)
    assert "timeSeconds" in json.dumps(result)


def sequence_fixture(tmp_path, values):
    project = tmp_path / "Test.uproject"
    project.write_text('{"FileVersion":3}')
    frames = []
    for i, value in enumerate(values):
        path = hdr(tmp_path / f"frame{i}.rgba32f", [value, 0.])
        frames.append({"frame": i, "timeSeconds": i / 30, "path": path.name, "sha256": sha(path)})
    manifest = {"schema": "bread-unreal-hdr-sequence/v1", "project": str(project), "size": [2, 1],
                "encoding": "final-linear-hdr", "mode": "steady", "frameRate": 30,
                "context": {"captureId": "fixture", "eventId": 1, "view": "front",
                            "passMeaning": "indirect", "displayTransform": "none"},
                "capture": {"method": "movie-render-queue", "runId": "fixture-run",
                            "advancesWorldTime": True, "resetsHistoryPerFrame": False, "warmupFrames": 32},
                "frames": frames}
    return project, manifest


def review_fixture(tmp_path, project, manifest, **options):
    path = tmp_path / "sequence.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return review_sequence(str(project), str(path), sha(path), **options)


def test_sequence_detects_middle_frame_flicker_even_when_return_is_exact(tmp_path):
    project, manifest = sequence_fixture(tmp_path, [0, 0, 0, 4, 0, 0, 0, 0])
    result = review_fixture(tmp_path, project, manifest)
    assert result["metadataContinuous"] and not result["withinTolerance"]
    assert result["firstToLast"]["maxAbsoluteError"] == 0
    assert result["steadyRange"]["maxAbsoluteError"] == 4
    assert result["adjacentFrames"][2]["toFrame"] == 3
    assert result["adjacentFrames"][2]["p99AbsoluteError"] > 0
    assert "not independently attested" in result["provenance"]


def test_sequence_stable_and_motion_are_not_confused(tmp_path):
    project, manifest = sequence_fixture(tmp_path, [1] * 8)
    assert review_fixture(tmp_path, project, manifest)["withinTolerance"]
    manifest["mode"] = "motion"
    result = review_fixture(tmp_path, project, manifest)
    assert not result["stabilityEligible"] and result["steadyRange"] is None
    assert not result["withinTolerance"] and result["motionRange"] is not None


@pytest.mark.parametrize("change", ["gap", "time", "history", "average", "screenshot", "short"])
def test_sequence_rejects_invalid_continuity_claims(tmp_path, change):
    project, manifest = sequence_fixture(tmp_path, [0] * 8)
    if change == "gap": manifest["frames"][4]["frame"] += 1
    if change == "time": manifest["frames"][4]["timeSeconds"] += 1
    if change == "history": manifest["capture"]["resetsHistoryPerFrame"] = True
    if change == "average": manifest["capture"]["temporalSamples"] = 8
    if change == "screenshot": manifest["capture"]["method"] = "HighResShot"
    if change == "short": manifest["frames"] = manifest["frames"][:3]
    result = review_fixture(tmp_path, project, manifest)
    assert result["issues"] and not result["withinTolerance"]


def test_sequence_checks_actual_files_not_only_a_completed_callback(tmp_path):
    project, manifest = sequence_fixture(tmp_path, [0] * 8)
    path = tmp_path / manifest["frames"][4]["path"]
    path.write_bytes(b"broken")
    with pytest.raises(ValueError, match="hash"):
        review_fixture(tmp_path, project, manifest)
    manifest["frames"][4]["sha256"] = sha(path)
    with pytest.raises(ValueError, match="resolution"):
        review_fixture(tmp_path, project, manifest)


def test_sequence_checks_recorded_asset_freshness(tmp_path):
    project, manifest = sequence_fixture(tmp_path, [0] * 8)
    asset = tmp_path / 'Content/Fixture.uasset'
    asset.parent.mkdir()
    asset.write_bytes(b'original fixture')
    manifest['savedAssetHashes'] = {'Content/Fixture.uasset': sha(asset)}
    assert review_fixture(tmp_path, project, manifest)['assetStateVerified']
    asset.write_bytes(b'edited fixture')
    with pytest.raises(ValueError, match='asset changed'):
        review_fixture(tmp_path, project, manifest)


def editor_namespace(monkeypatch, stats):
    class Material:
        def get_path_name(self): return "/Game/Fixture/M_Test.M_Test"
    unreal = SimpleNamespace(Material=Material, EditorAssetLibrary=None,
                             MaterialEditingLibrary=SimpleNamespace(recompile_material=lambda m: None,
                                                                   get_statistics=stats))
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "src/unreal_editor_script.py"))
    return namespace, Material()


def test_compile_failure_blocks_capture_before_rt_allocation(tmp_path, monkeypatch):
    namespace, material = editor_namespace(monkeypatch, lambda m: SimpleNamespace(
        num_pixel_shader_instructions=0, num_vertex_shader_instructions=0))
    log = tmp_path / "editor.log"
    log.write_text("")
    preflight = namespace["preflight_materials"]([material], log_path=log)
    assert not preflight["passed"] and "No compiled" in preflight["diagnostics"][0]
    capture = namespace["capture"]
    capture.__globals__["preflight_materials"] = lambda *args: preflight
    capture.__globals__["spawn"] = lambda *args: pytest.fail("RT capture started despite compilation failure")
    with pytest.raises(RuntimeError, match="blocked capture"):
        capture(None, {}, {}, [{"id": "Probe"}], {"test": material}, {}, tmp_path, 16, 16, False, ())
    assert not json.loads((tmp_path / "material-preflight-Probe.json").read_text())["passed"]


def test_fallback_log_blocks_even_positive_or_stale_resource_statistics(tmp_path, monkeypatch):
    log = tmp_path / "editor.log"
    log.write_text("old unrelated failure\n")
    def stats(material):
        with log.open("a") as stream:
            stream.write("LogMaterial: Failed to compile Material /Game/Fixture/M_Test; Default Material will be used\n")
        return SimpleNamespace(num_pixel_shader_instructions=20, num_vertex_shader_instructions=10)
    namespace, material = editor_namespace(monkeypatch, stats)
    result = namespace["preflight_materials"]([material], log_path=log)
    assert not result["passed"] and any("Default Material" in d for d in result["diagnostics"])


def test_compile_preflight_distinguishes_success_unavailable_and_old_diagnostics(tmp_path, monkeypatch):
    namespace, material = editor_namespace(monkeypatch, lambda m: SimpleNamespace(
        num_pixel_shader_instructions=20, num_vertex_shader_instructions=10))
    log = tmp_path / "editor.log"
    log.write_text("Failed to compile Material /Game/Old; Default Material will be used\n")
    assert namespace["preflight_materials"]([material], log_path=log)["passed"]
    assert not namespace["preflight_materials"]([material])["passed"]
    namespace["EDIT"].get_statistics = None
    assert namespace["preflight_materials"]([material], log_path=log)["status"] == "unavailable"


def test_variant_preflight_reads_resources_without_recompiling_unchanged_parents(tmp_path, monkeypatch):
    namespace, material = editor_namespace(monkeypatch, lambda m: SimpleNamespace(
        num_pixel_shader_instructions=20, num_vertex_shader_instructions=10))
    calls = []
    namespace['EDIT'].recompile_material = calls.append
    log = tmp_path / 'editor.log'
    log.write_text('')
    assert namespace['preflight_materials']([material, material], log_path=log)['passed']
    assert calls == [material]
    calls.clear()
    assert namespace['preflight_materials']([material], log_path=log, recompile=False)['passed']
    assert not calls


@pytest.mark.skipif(not os.environ.get("BREAD_UNREAL_TEST_EDITOR"), reason="Needs a stock Unreal Editor and GPU")
def test_stock_editor_compile_preflight_accepts_valid_and_blocks_invalid_material(tmp_path, monkeypatch):
    from src.unreal_integration import UnrealService, _editor_script
    editor = Path(os.environ["BREAD_UNREAL_TEST_EDITOR"])
    build = json.loads((editor.parents[2] / "Build/Build.version").read_text(encoding="utf-8-sig"))
    project = tmp_path / "CompilePreflight.uproject"
    project.write_text(json.dumps({"FileVersion": 3,
                                  "EngineAssociation": f'{build["MajorVersion"]}.{build["MinorVersion"]}'}))
    code = f'''import runpy
from pathlib import Path
helper = runpy.run_path({str(_editor_script())!r})
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
logs = sorted(Path({str(tmp_path)!r}).glob('Saved/Bread/EditorMCP/*/editor.log'))
assert len(logs) == 1
tools = unreal.AssetToolsHelpers.get_asset_tools()
good = tools.create_asset('M_Good', '/Game/PreflightFixture', unreal.Material, unreal.MaterialFactoryNew())
valid = helper['preflight_materials']([good], world, logs[0], False)
bad = tools.create_asset('M_Bad', '/Game/PreflightFixture', unreal.Material, unreal.MaterialFactoryNew())
node = unreal.MaterialEditingLibrary.create_material_expression(bad, unreal.MaterialExpressionCustom, 0, 0)
node.set_editor_property('code', 'return BreadDeliberateUndefinedSymbol;')
node.set_editor_property('output_type', unreal.CustomMaterialOutputType.CMOT_FLOAT3)
assert unreal.MaterialEditingLibrary.connect_material_property(node, '', unreal.MaterialProperty.MP_BASE_COLOR)
invalid = helper['preflight_materials']([bad], world, logs[0], False)
result = {{'valid': valid, 'invalid': invalid}}
'''
    monkeypatch.setenv("BREAD_UNREAL_EDITOR_PYTHON", "1")
    service = UnrealService()
    queued = service.call(str(project), "call_tool", {"toolset_name": "BreadEditorTools", "tool_name": "run_python",
                                                      "arguments": {"code": code}}, str(editor))
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = service.status(queued["jobId"])
        if result["status"] not in {"queued", "running"}: break
        time.sleep(.5)
    assert result["status"] == "executed", result
    assert result["result"]["valid"]["passed"], result
    assert not result["result"]["invalid"]["passed"], result
    assert not list((tmp_path / 'Content').rglob('*.uasset'))  # no effect assets saved
