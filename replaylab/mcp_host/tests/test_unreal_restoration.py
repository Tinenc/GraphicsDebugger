from __future__ import annotations

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

from src.unreal_integration import UnrealService, _native_arguments, _sha, inspect_project
from src.unreal_recipe import Recipe
from src.unreal_validation import _float_pixels, compare, review_layers


def input_pin(name, source, type_):
    return {"name": name, "source": source, "type": type_, "comment": name}


def sample_recipe():
    return {
        "surface": "Card", "size": [96, 96],
        "camera": {"location": [-280, 0, 0], "fov": 55},
        "exposureMode": "manual", "globalIllumination": "none",
        "postProcess": {"bloom_intensity": 0, "motion_blur_amount": 0, "auto_exposure_bias": -1},
        "surfaces": [{
            "id": "Card", "template": "unlit",
            "parameters": {"Tint": {"type": "vector3", "comment": "Linear palette", "value": [1, .1, .01]},
                           "Brightness": {"type": "scalar", "comment": "Exposure-independent scale", "value": .8}},
            "functions": [{"id": "Palette", "operator": "multiply", "outputType": "vector3",
                           "comment": "Scale the palette before output.",
                           "inputs": [input_pin("Color", "Tint", "vector3"),
                                      input_pin("Scale", "Brightness", "scalar")]}],
            "outputs": {"MP_EMISSIVE_COLOR": "Palette"}, "instance": {"Tint": [.05, .3, 1]},
        }, {
            "id": "CopyScene", "template": "render-target",
            "parameters": {"Input": {"type": "texture2d", "comment": "Scene capture input", "texture": "Occlusion"}},
            "functions": [{"id": "Copy", "code": "return Texture2DSample(InputRT, InputRTSampler, UV).rgb * 0.5;",
                           "outputType": "vector3", "comment": "Sample the prior pass at half intensity.",
                           "inputs": [input_pin("InputRT", "Input", "texture2d"),
                                      input_pin("UV", "UV0", "vector2")]}],
            "outputs": {"MP_EMISSIVE_COLOR": "Copy"},
        }, {
            "id": "Presentation", "template": "post-process",
            "parameters": {},
            "functions": [{"id": "Present", "code": "return Color;", "outputType": "vector3",
                           "comment": "Keep the current scene color at this blend point.",
                           "inputs": [input_pin("Color", "PostProcessInput0", "vector3")]}],
            "outputs": {"MP_EMISSIVE_COLOR": "Present"},
        }],
        "sceneObjects": [{"id": "Occluder", "shape": "Cube", "location": [0, 190, 0]}],
        "liveCaptures": [{"id": "Occlusion", "mode": "scene-depth", "size": [64, 64],
                          "location": [-200, 190, 0], "orthoWidth": 200, "showOnly": ["Occluder"]}],
        "passes": [{"id": "Final", "mode": "final"}, {"id": "Linear", "mode": "scene-color"},
                   {"id": "Normal", "mode": "world-normal"}, {"id": "Depth", "mode": "scene-depth"},
                   {"id": "OcclusionReadback", "mode": "live", "source": "Occlusion"},
                   {"id": "Half", "mode": "material", "precision": "float16", "material": "CopyScene", "reads": {"Input": "Linear"}}],
    }


def layered_recipe():
    recipe = sample_recipe()
    recipe["surfaces"].append({"id": "LitBody", "template": "default-lit",
                               "nativeSpecular": "matte-diffuse", "functions": [],
                               "parameters": {"Albedo": {"type": "vector3", "value": [.25, .25, .25],
                                                          "comment": "Matte calibration albedo"}},
                               "outputs": {"MP_BASE_COLOR": "Albedo"}})
    recipe["lights"] = [{"id": "Sun", "kind": "directional", "intensity": 3,
                         "color": [1, .25, .05], "specularScale": 0},
                        {"id": "Fill", "kind": "point", "intensity": 60, "units": "CANDELAS",
                         "location": [-155, 0, 0], "attenuationRadius": 500,
                         "color": [.05, .2, 1], "specularScale": 0}]
    recipe["variants"] = [
        {"id": "IsolatedMath", "parameters": {"Tint": [.5, .2, .1]},
         "passes": [{"id": "Formula", "mode": "scene-color"}]},
        {"id": "BothLights", "surface": "LitBody",
         "passes": [{"id": "BothLinear", "mode": "scene-color"}, {"id": "BothFinal", "mode": "final"}]},
        {"id": "WithoutFill", "surface": "LitBody", "lightScales": {"Fill": 0},
         "passes": [{"id": "KeyLinear", "mode": "scene-color"}, {"id": "KeyFinal", "mode": "final"}]},
        {"id": "SpecularOn", "surface": "LitBody", "lightSpecularScales": {"Sun": 1},
         "passes": [{"id": "SpecularLinear", "mode": "scene-color"}]},
        {"id": "Grading", "postProcess": {"color_saturation": [1, 1, 1, 0]},
         "passes": [{"id": "GradeFinal", "mode": "final"}, {"id": "GradeLinear", "mode": "scene-color"}]},
    ]
    recipe["layers"] = [
        {"id": "Palette", "kind": "diffuse", "route": "exact", "surface": "Card",
         "functions": ["Palette"], "captureBasis": "Fixture palette with an analytic multiplier",
         "implementation": "Native multiply in a material function", "adjust": "Inspect tint and brightness",
         "passes": ["Formula"]},
        {"id": "Lighting", "kind": "multi-light", "route": "replacement", "surface": "LitBody",
         "captureBasis": "Two native lights in the fixture",
         "implementation": "UE direction and point light with matte diffuse",
         "adjust": "Compare the key-only view before changing the fill",
         "passes": ["BothLinear", "KeyLinear", "BothFinal", "KeyFinal"]},
        {"id": "Overall", "kind": "appearance", "route": "replacement",
         "captureBasis": "Fixture baseline", "implementation": "Calibrated display transform",
         "adjust": "Review exposure and preceding layers before post-process", "passes": ["Final"]},
    ]
    return recipe


def test_unreal_recipe_rejects_unbound_cycles_and_bad_parameter_override():
    source = sample_recipe()
    assert Recipe.model_validate(source).passes[-1].reads == {"Input": "Linear"}
    source["passes"][-1]["reads"]["Input"] = "Half"
    with pytest.raises(ValueError, match="earlier producer"):
        Recipe.model_validate(source)
    source = sample_recipe()
    source["surfaces"][0]["instance"]["Tint"] = 1.0
    with pytest.raises(ValueError, match="Invalid instance override"):
        Recipe.model_validate(source)
    source = sample_recipe()
    source["sceneObjects"][0]["material"] = "Card"
    with pytest.raises(ValueError, match="feedback"):
        Recipe.model_validate(source)


@pytest.mark.parametrize("ray_tracing", [0, 1])
def test_final_hdr_captures_the_engine_final_pipeline_without_changing_raw_scene_color(tmp_path, monkeypatch, ray_tracing):
    source = sample_recipe()
    source["passes"].insert(2, {"id": "LitHDR", "mode": "final-hdr"})
    assert Recipe.model_validate(source).passes[2].mode == "final-hdr"
    properties = {"post_process_settings": object()}
    captured = []
    component = SimpleNamespace(
        use_ray_tracing_if_enabled=False,
        get_editor_property=properties.__getitem__,
        set_editor_property=properties.__setitem__,
        capture_scene=lambda: captured.append(properties["capture_source"]),
    )
    actor = SimpleNamespace(get_component_by_class=lambda _: component)
    destroyed = []
    unreal = SimpleNamespace(
        MaterialEditingLibrary=None, EditorAssetLibrary=None,
        SceneCapture2D=object, SceneCaptureComponent2D=object, EditorActorSubsystem=object,
        SceneCaptureSource=SimpleNamespace(
            SCS_FINAL_COLOR_LDR="final-ldr", SCS_FINAL_COLOR_HDR="final-hdr",
            SCS_SCENE_COLOR_HDR_NO_ALPHA="raw-scene-color"),
        TextureRenderTargetFormat=SimpleNamespace(RTF_RGBA8="rgba8", RTF_RGBA32F="rgba32f"),
        RenderingLibrary=SimpleNamespace(create_render_target2d=lambda *args: object()),
        SystemLibrary=SimpleNamespace(get_console_variable_int_value=lambda _: ray_tracing),
        get_editor_subsystem=lambda _: SimpleNamespace(destroy_actor=destroyed.append),
    )
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "src/unreal_editor_script.py"))
    capture = namespace["capture"]
    capture.__globals__.update(
        preflight_materials=lambda *args: {"passed": True},
        spawn=lambda *args: actor,
        exposure=lambda settings, *args: settings,
        lighting_override=lambda settings, *args: settings,
        save_rt=lambda world, rt, mode, directory, name, width, height, encoding: {"mode": mode, "encoding": encoding},
    )
    passes = [
        {"id": "Raw", "mode": "scene-color"},
        {"id": "Lit", "mode": "final-hdr"},
        {"id": "Final", "mode": "final"},
    ]
    images = capture(object(), {"fov": 55}, {}, passes, {}, {}, tmp_path, 96, 96, True, ("lumen", "none"))
    assert captured == ["raw-scene-color", "final-hdr", "final-ldr"]
    assert [image["captureSource"] for image in images] == [
        "SCS_SCENE_COLOR_HDR_NO_ALPHA", "SCS_FINAL_COLOR_HDR", "SCS_FINAL_COLOR_LDR",
    ]
    assert [image["encoding"] for image in images] == ["scene-linear-hdr", "final-linear-hdr", "display-srgb"]
    assert [image["renderTargetFormat"] for image in images] == ["RTF_RGBA32F", "RTF_RGBA32F", "RTF_RGBA8"]
    assert [image["captureRayTracingIfEnabled"] for image in images] == [bool(ray_tracing)] * 3
    assert destroyed == [actor]


def test_unreal_layer_plan_requires_final_coverage_and_independent_variant_rts():
    data = layered_recipe()
    assert Recipe.model_validate(data).surfaces[-1].nativeSpecular == "matte-diffuse"
    data["layers"].pop()
    with pytest.raises(ValueError, match="baseline appearance"):
        Recipe.model_validate(data)
    data = layered_recipe()
    data["variants"][0]["passes"].append({"id": "Copy", "mode": "material", "material": "CopyScene",
                                           "reads": {"Input": "Linear"}})
    with pytest.raises(ValueError, match="earlier producer"):
        Recipe.model_validate(data)
    data = layered_recipe()
    data["surfaces"][-1]["outputs"]["MP_SPECULAR"] = "Albedo"
    with pytest.raises(ValueError, match="explicit Metallic"):
        Recipe.model_validate(data)


def test_light_variants_can_observe_a_level_with_a_renamed_mesh(tmp_path, monkeypatch):
    controls = {"intensity": 2.0, "specular_scale": 1.0}
    component = SimpleNamespace(
        get_editor_property=controls.__getitem__,
        set_intensity=lambda value: controls.update(intensity=value),
        set_specular_scale=lambda value: controls.update(specular_scale=value),
    )
    mesh = SimpleNamespace(get_actor_label=lambda: "Renamed character")
    sun = SimpleNamespace(get_actor_label=lambda: "Restoration light Sun")
    editor = SimpleNamespace(get_all_level_actors=lambda: [mesh, sun])
    monkeypatch.setitem(sys.modules, "unreal", SimpleNamespace(
        MaterialEditingLibrary=None, EditorAssetLibrary=None,
        EditorActorSubsystem=None, get_editor_subsystem=lambda _: editor,
    ))
    script = Path(__file__).resolve().parents[1] / "src/unreal_editor_script.py"
    observe = runpy.run_path(str(script))["capture_observations"]
    monkeypatch.setitem(observe.__globals__, "observation_lighting", lambda actors: ("lumen", "lumen"))
    monkeypatch.setitem(observe.__globals__, "light_component", lambda actor: component)
    snapshots = []
    def capture(world, camera, post, effects, materials, live, directory, *args):
        snapshots.append(dict(controls))
        return [dict(name=effects[0]["id"])]
    monkeypatch.setitem(observe.__globals__, "capture", capture)
    recipe = {
        "size": [32, 32], "passes": [{"id": "Base"}], "lights": [{"id": "Sun"}],
        "variants": [
            {"id": "NoDirect", "passes": [{"id": "A"}], "lightScales": {"Sun": 0.0},
             "parameters": {}, "postProcess": {}},
            {"id": "NoSpecular", "passes": [{"id": "B"}], "lightScales": {},
             "lightSpecularScales": {"Sun": 0.0}, "parameters": {}, "postProcess": {}},
        ],
    }
    images = observe(None, {}, recipe, {}, {}, tmp_path, False)
    assert [image["variant"] for image in images] == ["baseline", "NoDirect", "NoSpecular"]
    assert snapshots == [{"intensity": 2.0, "specular_scale": 1.0},
                         {"intensity": 0.0, "specular_scale": 1.0},
                         {"intensity": 2.0, "specular_scale": 0.0}]
    assert controls == snapshots[0]


def test_unreal_native_requests_limit_edit_dispatch():
    args = {"toolset_name": "toolset_registry.toolsets.core.blueprint.BlueprintTools",
            "tool_name": "add_node", "arguments": {"graph": "ConstructionScript"}}
    assert _native_arguments("call_tool", args) == args
    with pytest.raises(ValueError, match="scoped"):
        _native_arguments("call_tool", {**args, "tool_name": "execute_python"})
    with pytest.raises(ValueError, match="toolset"):
        _native_arguments("call_tool", {**args, "toolset_name": "PythonTools"})


def _captured_mesh(package):
    package.mkdir()
    corners = [(-.5, -.5, -.5), (.5, -.5, -.5), (.5, .5, -.5), (-.5, .5, -.5),
               (-.5, -.5, .5), (.5, -.5, .5), (.5, .5, .5), (-.5, .5, .5)]
    triangles = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4,
                 1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7]
    vertices = b"".join(struct.pack("<3f", *xyz) for xyz in corners)
    indices = struct.pack(f"<{len(triangles)}H", *triangles)
    gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}], "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
            "buffers": [{"uri": "asset.bin", "byteLength": len(vertices) + len(indices)}],
            "bufferViews": [{"buffer": 0, "byteLength": len(vertices), "target": 34962},
                            {"buffer": 0, "byteOffset": len(vertices), "byteLength": len(indices), "target": 34963}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(corners), "type": "VEC3",
                           "min": [-.5] * 3, "max": [.5] * 3},
                          {"bufferView": 1, "componentType": 5123, "count": len(triangles), "type": "SCALAR"}]}
    (package / "asset.bin").write_bytes(vertices + indices)
    (package / "asset.gltf").write_text(json.dumps(gltf), encoding="utf-8")
    (package / "package-manifest.json").write_text(json.dumps({"files": [
        {"path": p.name, "size": p.stat().st_size, "sha256": _sha(p)} for p in package.iterdir()]}), encoding="utf-8")


def _wait(service, queued):
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        result = service.status(queued["jobId"])
        if result["status"] not in {"running", "queued"}:
            return result
        time.sleep(.5)
    raise AssertionError("Editor job did not finish")


@pytest.mark.skipif(not os.environ.get("BREAD_UNREAL_TEST_EDITOR"), reason="Needs an installed Unreal Editor and GPU")
def test_stock_editor_writes_editable_map_and_rt_evidence(tmp_path):
    editor = Path(os.environ["BREAD_UNREAL_TEST_EDITOR"])
    engine = json.loads((editor.parents[2] / "Build/Build.version").read_text(encoding="utf-8-sig"))
    association = f'{engine["MajorVersion"]}.{engine["MinorVersion"]}'
    project = tmp_path / "BreadTest.uproject"
    descriptor = json.dumps({"FileVersion": 3, "EngineAssociation": association}).encode()
    project.write_bytes(descriptor)
    op = "a" * 32
    second = "b" * 32
    for operation in (op, second):
        _captured_mesh(tmp_path / operation)

    class PackageManager:
        def status(self, operation):
            assert operation in (op, second)
            return {"status": "finalized", "packagePath": str(tmp_path / operation),
                    "request": {"captureId": "fixture", "eventId": 1 if operation == op else 2}}

        def validate(self, operation):
            assert operation in (op, second)
            return {"valid": True}

    service = UnrealService()
    probed = _wait(service, service.probe(str(project), str(editor)))
    assert probed["status"] == "probed", probed
    assert probed["renderCapabilities"]["templates"]["default-lit"]
    assert "point" in probed["renderCapabilities"]["lightTypes"]
    recipe = layered_recipe()
    recipe["additionalMeshes"] = [{"id": "SecondDraw", "operationId": second, "material": "LitBody",
                                    "location": [0, -95, 0], "scale": [0.5, 0.5, 0.5]}]
    queued = service.import_package(str(project), str(editor), PackageManager(), op, recipe)
    assert queued["status"] == "queued", queued
    result = _wait(service, queued)
    assert result["status"] == "rendered", json.dumps(result, indent=2)
    assert project.read_bytes() == descriptor
    assert any("/Materials/Instances/MI_Card." in asset for asset in result["assets"])
    assert any("/Materials/Functions/MF_Card_Palette." in asset for asset in result["assets"])
    assert any("/Textures/RenderTargets/RT_Occlusion." in asset for asset in result["assets"])
    assert sum("/Meshes/" in asset for asset in result["assets"]) == 2
    assert result["sourcePackages"][second]["eventId"] == 2
    assert any(rel.endswith("Restoration.umap") for rel in result["savedAssetHashes"])
    images = {image["name"]: image for image in result["images"]}
    with Image.open(images["Final"]["path"]) as final:
        r, g, b = final.convert("RGB").getpixel((48, 48))
        assert b > g > r and b > 40
    linear = images["Linear"]
    half = images["Half"]
    index = 48 * 96 + 48
    pixel = list(_float_pixels(Path(linear["path"]), linear["sha256"], 96, 96, "RGB"))[index]
    copied = list(_float_pixels(Path(half["path"]), half["sha256"], 96, 96, "RGB"))[index]
    assert pixel[2] > .01
    assert copied == pytest.approx([x * .5 for x in pixel], abs=.005)
    depth = images["OcclusionReadback"]
    depth_pixels = list(_float_pixels(Path(depth["path"]), depth["sha256"], 64, 64, "R"))
    assert depth_pixels[32 * 64 + 32] == pytest.approx((150,), abs=.02)
    assert depth_pixels[0][0] > 100000
    reopened = _wait(service, service.observe(str(project), str(editor), queued["jobId"]))
    assert reopened["status"] == "rendered", json.dumps(reopened, indent=2)
    assert reopened["savedAssetHashes"] == result["savedAssetHashes"]
    assert reopened["sourcePackages"] == result["sourcePackages"]
    new_depth = next(image for image in reopened["images"] if image["name"] == "OcclusionReadback")
    assert new_depth["sha256"] == depth["sha256"]
    context = {"captureId": "fixture", "eventId": 1, "view": "camera", "displayTransform": "none",
               "temporalState": "first", "passMeaning": "scene-linear", "encoding": linear["encoding"]}
    report = compare(project, queued["jobId"], "Linear", linear["path"], linear["sha256"],
                     "Shader calculation", "exact", context, context, 0., 0., "RGB", "")
    assert report["withinTolerance"] and report["requiresSemanticReview"]
    wrong = {**context, "captureId": "another capture"}
    with pytest.raises(ValueError, match="different capture"):
        compare(project, queued["jobId"], "Linear", linear["path"], linear["sha256"],
                "Shader calculation", "exact", wrong, wrong, 0., 0., "RGB", "")
    formula = images["Formula"]
    formula_pixel = list(_float_pixels(Path(formula["path"]), formula["sha256"], 96, 96, "RGB"))[index]
    assert formula_pixel == pytest.approx((.4, .16, .08), abs=.005)
    both, key = (images[name] for name in ("BothLinear", "KeyLinear"))
    both_pixels = list(_float_pixels(Path(both["path"]), both["sha256"], 96, 96, "RGB"))
    key_pixels = list(_float_pixels(Path(key["path"]), key["sha256"], 96, 96, "RGB"))
    assert max(x[2] - y[2] for x, y in zip(both_pixels, key_pixels)) > .01
    assert images["GradeFinal"]["sha256"] != images["Final"]["sha256"]
    grade_linear = images["GradeLinear"]
    assert grade_linear["sha256"] == images["Linear"]["sha256"]
    layer_ref = {**context, "variant": "IsolatedMath", "encoding": formula["encoding"]}
    layer_report = review_layers(project, queued["jobId"], [{
        "layer": "Palette", "observation": "Formula", "referencePath": formula["path"],
        "referenceSha256": formula["sha256"], "referenceContext": layer_ref, "renderedContext": layer_ref,
    }])
    assert layer_report["layers"][0]["status"] == "within-tolerance"
    assert layer_report["pendingLayers"] == ["Lighting", "Overall"]
    assert not layer_report["allWithinTolerance"]
    assert images["SpecularLinear"]["variant"] == "SpecularOn"
    light_ref = {**context, "variant": "BothLights", "encoding": both["encoding"]}
    replacement = compare(project, queued["jobId"], "BothLinear", both["path"], both["sha256"],
                          "Lighting", "replacement", light_ref, light_ref, .001, 0., "RGB", "Native lights")
    assert replacement["withinTolerance"] and replacement["comparisonSpace"] == "recorded-numeric-components"
    stale = tmp_path / next(iter(result["savedAssetHashes"]))
    with stale.open("ab") as stream:
        stream.write(b"stale")
    with pytest.raises(ValueError, match="changed"):
        compare(project, queued["jobId"], "Linear", linear["path"], linear["sha256"],
                "Shader calculation", "exact", context, context, 0., 0., "RGB", "")



@pytest.mark.skipif(not os.environ.get("BREAD_UNREAL_TEST_GI") or not os.environ.get("BREAD_UNREAL_TEST_EDITOR"),
                    reason="Needs an installed UE Editor with a DX12 SM6 GPU")
def test_scene_capture_reads_native_lumen_bounce(tmp_path):
    editor = Path(os.environ["BREAD_UNREAL_TEST_EDITOR"])
    engine = json.loads((editor.parents[2] / "Build/Build.version").read_text(encoding="utf-8-sig"))
    association = f'{engine["MajorVersion"]}.{engine["MinorVersion"]}'
    project = tmp_path / "BreadLumenTest.uproject"
    project.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": association}), encoding="utf-8")
    config = tmp_path / "Config"
    config.mkdir()
    (config / "DefaultEngine.ini").write_text(
        "[/Script/WindowsTargetPlatform.WindowsTargetSettings]\nDefaultGraphicsRHI=DefaultGraphicsRHI_DX12\n"
        "+D3D12TargetedShaderFormats=PCD3D_SM6\n\n[/Script/Engine.RendererSettings]\n"
        "r.DynamicGlobalIlluminationMethod=1\nr.GenerateMeshDistanceFields=True\nr.AllowStaticLighting=False\n"
        "r.Lumen.HardwareRayTracing=0\nr.DefaultFeature.AutoExposure=False\n", encoding="ascii")
    package = tmp_path / "source"
    _captured_mesh(package)

    class PackageManager:
        def status(self, operation):
            return {"status": "finalized", "packagePath": str(package)}

        def validate(self, operation):
            return {"valid": True}

    def material(name, template, color, pin):
        return {"id": name, "template": template, "functions": [],
                "parameters": {"Color": {"type": "vector3", "value": color, "comment": name}},
                "outputs": {pin: "Color"}}

    width, height = 160, 128
    recipe = {"surface": "Receiver", "size": [width, height], "camera": {"location": [-285, 0, 0], "fov": 65},
              "surfaces": [material("Receiver", "default-lit", [.65, .65, .65], "MP_BASE_COLOR"),
                           material("Emitter", "unlit", [12, .01, .01], "MP_EMISSIVE_COLOR")],
              "exposureMode": "manual", "reflections": "none",
              "postProcess": {"auto_exposure_bias": 0, "bloom_intensity": 0, "motion_blur_amount": 0},
              "sceneObjects": [{"id": "Emitter", "shape": "Cube", "location": [-95, 78, 0],
                                "scale": [.72, .16, 1.4], "material": "Emitter"}],
              "passes": [{"id": "Linear", "mode": "scene-color", "warmup": 12},
                         {"id": "Final", "mode": "final", "warmup": 6}]}
    service = UnrealService()
    pixels = {}
    for method in ("none", "lumen"):
        queued = service.import_package(str(project), str(editor), PackageManager(), "a" * 32,
                                        {**recipe, "globalIllumination": method})
        result = _wait(service, queued)
        assert result["status"] == "rendered", json.dumps(result, indent=2)
        image = next(item for item in result["images"] if item["name"] == "Linear")
        assert image["captureLighting"]["globalIllumination"] == method
        pixels[method] = list(_float_pixels(Path(image["path"]), image["sha256"], width, height, "RGB"))
    receiver = [y * width + x for y in range(48, 81) for x in range(65, 90)]
    delta = [pixels["lumen"][i][0] - pixels["none"][i][0] for i in receiver]
    mean = sum(delta) / len(delta)
    print(f"Native Lumen receiver mean red radiance delta: {mean:.6f}")
    assert mean > .005, "No native red indirect-light contribution was measured on the receiver"


@pytest.mark.skipif(not os.environ.get("BREAD_UNREAL_TEST_EDITOR"), reason="Needs an installed stock Unreal Editor")
def test_stock_editor_mcp_authored_assets_are_saved(tmp_path, monkeypatch):
    editor = Path(os.environ["BREAD_UNREAL_TEST_EDITOR"])
    build = json.loads((editor.parents[2] / "Build/Build.version").read_text(encoding="utf-8-sig"))
    association = f'{build["MajorVersion"]}.{build["MinorVersion"]}'
    project = tmp_path / "BreadEditorMcpTest.uproject"
    descriptor = json.dumps({"FileVersion": 3, "EngineAssociation": association}).encode()
    project.write_bytes(descriptor)
    service = UnrealService()
    info = inspect_project(str(project), str(editor))
    if info["nativeMcpAvailable"]:
        pytest.skip("The fixture verifies the stock Python fallback")
    args = {"toolset_name": "BreadEditorTools", "tool_name": "run_python",
            "arguments": {"code": "result = True"}}
    monkeypatch.delenv("BREAD_UNREAL_EDITOR_PYTHON", raising=False)
    with pytest.raises(ValueError, match="not been granted"):
        service.call(str(project), "call_tool", args, str(editor))
    monkeypatch.setenv("BREAD_UNREAL_EDITOR_PYTHON", "1")
    connected = _wait(service, service.prepare(str(project), str(editor)))
    assert connected["status"] == "connected" and connected["provider"] == "bread-editor-python", connected
    assert "run_python" in connected["toolsets"][0]["tools"]
    args["arguments"]["code"] = """import unreal
asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
folder = "/Game/BreadProbe"
material = asset_tools.create_asset("M_Check", folder, unreal.Material, unreal.MaterialFactoryNew())
mf = asset_tools.create_asset("MF_Check", folder, unreal.MaterialFunction, unreal.MaterialFunctionFactoryNew())
mi = asset_tools.create_asset("MI_Check", folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
unreal.MaterialEditingLibrary.set_material_instance_parent(mi, material)
factory = unreal.BlueprintFactory()
factory.set_editor_property("parent_class", unreal.Actor)
blueprint = asset_tools.create_asset("BP_Check", folder, unreal.Blueprint, factory)
constant = unreal.MaterialEditingLibrary.create_material_expression(material, unreal.MaterialExpressionConstant, -250, 0)
constant.set_editor_property("r", 0.5)
constant.set_editor_property("desc", "Bread Editor MCP test")
assert unreal.MaterialEditingLibrary.connect_material_property(constant, "", unreal.MaterialProperty.MP_ROUGHNESS)
unreal.MaterialEditingLibrary.recompile_material(material)
assets = [material, mf, mi, blueprint]
assert all(unreal.EditorAssetLibrary.save_loaded_asset(asset) for asset in assets)
result = [asset.get_name() for asset in assets]
"""
    executed = _wait(service, service.call(str(project), "call_tool", args, str(editor)))
    assert executed["status"] == "executed", executed
    assert executed["result"] == ["M_Check", "MF_Check", "MI_Check", "BP_Check"]
    assert project.read_bytes() == descriptor
    assert sorted(asset.stem for asset in (tmp_path / "Content/BreadProbe").glob("*.uasset")) == [
        "BP_Check", "MF_Check", "MI_Check", "M_Check"
    ]
