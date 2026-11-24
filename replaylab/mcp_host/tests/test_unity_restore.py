"""Canonical resource export packages become bounded, repeatable Unity requests."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import types
import uuid

import pytest

from src import resource_export, unity_restore
from tests.test_bridge_resource_export import (
    INDEXED_FLAG, _Format, _MeshController, _pipeline, _rd_module, load_resource_export,
)


SMOKE_WHITE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP4DwQACfsD/fteaysAAAAASUVORK5CYII=")
SMOKE_CORNERS_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP4z8DwHwyBNBAw/AcAR8oI+ItOQ4UAAAAASUVORK5CYII=")
SMOKE_URP_SHADER = '''Shader "Bread/SyntheticURPIntegration"
{
    Properties
    {
        _BaseMap("Base Map", 2D) = "white" {}
        _BaseColor("Base Color", Color) = (1,1,1,1)
    }
    SubShader
    {
        Tags { "RenderType"="Opaque" "RenderPipeline"="UniversalPipeline" }
        Pass
        {
            Name "Forward"
            Tags { "LightMode"="UniversalForward" }
            Cull Off
            HLSLPROGRAM
            #pragma vertex Vert
            #pragma fragment Frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"
            TEXTURE2D(_BaseMap);
            SAMPLER(sampler_BaseMap);
            CBUFFER_START(UnityPerMaterial)
                half4 _BaseColor;
            CBUFFER_END
            struct Attributes { float4 positionOS : POSITION; };
            struct Varyings { float4 positionCS : SV_POSITION; float2 uv : TEXCOORD0; };
            Varyings Vert(Attributes input)
            {
                Varyings output;
                output.positionCS = TransformObjectToHClip(input.positionOS.xyz);
                output.uv = input.positionOS.xy + 0.5;
                return output;
            }
            half4 Frag(Varyings input) : SV_Target
            {
                return SAMPLE_TEXTURE2D(_BaseMap, sampler_BaseMap, input.uv) * _BaseColor;
            }
            ENDHLSL
        }
    }
}
'''
SMOKE_UV_SHADER = (SMOKE_URP_SHADER.replace("Bread/SyntheticURPIntegration", "Bread/SyntheticURPUVIntegration")
    .replace("half4 _BaseColor;", "half4 _BaseColor;\n                float4 _BaseMap_ST;")
    .replace("float4 positionOS : POSITION;", "float4 positionOS : POSITION; float2 uv : TEXCOORD0;")
    .replace("output.uv = input.positionOS.xy + 0.5;", "output.uv = TRANSFORM_TEX(input.uv, _BaseMap);"))


def finalized_package(base: Path, *, strategy="evidence-only", texture=None, source_use=None,
                      shader_source=None, material_color=None, texture_png=None,
                      asset_name="BreadTriangle", texture_sampler=None, texture_mapping=None,
                      uv_policy="preserve", include_uvs=False, source_api="D3D11", material_target="_BaseColor",
                      quad=False, uv_components=2, unity_options=None, dispatch=False):
    """Real ticket -> bridge capture fixture -> manager acceptance -> finalization.

    Only the captured GPU driver is synthetic. All file manifests, canonical
    streams, receipts, source copies and glTF output use production code.
    """
    bridge = load_resource_export()
    manager = resource_export.ResourceExportManager(base)
    operation_id = uuid.uuid4().hex
    session = "6" * 32
    prepared = manager.prepare_ticket(operation_id=operation_id, event_id=23, instance=0,
        view=0, asset_name=asset_name, capture_session_id=session, api=source_api)
    request = bridge.validate_capture_request({"event_id": 23, "instance": 0, "view": 0,
        "request_sha256": prepared["operation"]["requestSha256"], "export_ticket": prepared["ticket"],
        "ticket_path": prepared["ticketPath"], "ticket_sha256": prepared["ticketSha256"]}, "", session)
    positions = [-0.5, -0.5, 0, 0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, 0.5, 0] if quad else [-0.5, -0.5, 0, 0.5, -0.5, 0, 0, 0.5, 0]
    uvs = [[0, 0], [1, 0], [1, 1], [0, 1]] if quad else [[0, 0], [1, 0], [0.5, 1]]
    uvs = [(uv + [0.75, -0.25])[:uv_components] for uv in uvs]
    indices = [0, 1, 2, 0, 2, 3] if quad else [0, 1, 2]
    count = len(positions) // 3
    vertices = [value for i in range(count) for value in (positions[i * 3:i * 3 + 3] + uvs[i])] if include_uvs else positions
    controller = _MeshController(vertices, indices)
    pipeline = _pipeline(controller, True)
    pipeline.GetVBuffers()[0].byteStride = 12 + 4 * uv_components if include_uvs else 12
    pipeline.GetVertexInputs()[0].format = _Format(count=3)
    if include_uvs:
        inputs = pipeline.GetVertexInputs()
        uv = copy.copy(inputs[0])
        uv.name, uv.byteOffset, uv.format = "TEXCOORD0", 12, _Format(count=uv_components)
        pipeline.GetVertexInputs = lambda: inputs + [uv]
    action = types.SimpleNamespace(numIndices=len(indices), numVertices=count, numInstances=1, indexOffset=0,
        vertexOffset=0, instanceOffset=0, baseVertex=0, eventId=23, flags=INDEXED_FLAG,
        customName="BreadTriangle", children=[])
    controller.GetRootActions = lambda: [action]
    controller.GetPipelineState = lambda: pipeline
    controller.GetTextures = lambda: []
    controller.GetAPIProperties = lambda: types.SimpleNamespace(pipelineType=source_api)
    rd = _rd_module()
    if dispatch:
        rd.ActionFlags.Dispatch = 2
        action.flags = 2
        action.dispatchDimension = [8, 8, 1]
    state = bridge.capture_controller(controller, rd, request, [])
    state["bundle"]["syntheticFixture"] = {
        "enabled": True, "source": "test_unity_restore.finalized_package",
        "notAnActualGameCapture": True, "description": "Synthetic GPU driver and Shader inputs for Editor integration testing."}
    evidence = {}
    if shader_source is not None:
        raw = ("// Synthetic fixture, not a captured native Shader.\n" + shader_source).encode("utf-8")
        blocks = [{"name": "UnityPerMaterial", "bind": 0, "byteOffset": 0, "byteSize": 16,
                   "variablesStatus": "captured", "variables": [{"name": "_BaseColor",
                       "type": "float4", "value": material_color}],
                   "layoutFields": [{"name": "_BaseColor", "byteOffset": 0, "byteSize": 16}]}] \
            if material_color is not None else []
        stage = "compute" if dispatch else "fragment"
        evidence = {stage: {"shaderId": 77, "stage": stage, "entryPoint": "CSMain" if dispatch else "Frag",
            "encoding": "synthetic-text-fixture", "constantBuffers": blocks,
            "evidenceContract": {"observed": [], "generated": ["Synthetic integration fixture: not game evidence"]}}}
        raw_evidence = {"shaderRaw": {stage: raw}}
        if material_color is not None:
            raw_evidence["cbufferRaw"] = {stage: [{"constantBlockIndex": 0,
                "bufferResourceId": 99, "byteOffset": 0, "data": struct.pack("<4f", *material_color)}]}
        bridge.attach_raw_evidence(state, evidence, raw_evidence)
    response = bridge.finalize_capture(state, evidence)
    spec = {"targets": ["gltf", "unity-urp"], "assetName": asset_name,
            "uvPolicy": uv_policy, "shader": {"strategy": strategy}, "attributeMappings": [
                {"source": {"attributeIndex": 0}, "target": "POSITION"}]}
    if dispatch:
        spec["attributeMappings"] = []
    if unity_options is not None:
        spec["unity"] = unity_options
    if include_uvs:
        spec["attributeMappings"].append({"source": {"attributeIndex": 1}, "target": "TEXCOORD_0"})
    if shader_source is not None:
        recorded = response["bundle"]["shaders"][0]
        spec["shader"].update(provenance={
            "stage": recorded["stage"], "rawSha256": recorded["rawSha256"],
            "evidenceSha256": recorded["evidenceSha256"], "sourceKind": "synthetic-test-fixture",
            "note": "Authored URP integration fixture, not a recovered game Shader."})
        if not dispatch:
            spec["shader"]["unityShaderSource"] = shader_source
        if material_color is not None:
            spec["constantBuffers"] = [{"source": {"stage": stage, "block": "UnityPerMaterial",
                "variable": "_BaseColor"}, "classification": "material", "target": material_target,
                "type": "color", "value": material_color}]
    if texture is not None:
        # Inject canonical source evidence before Host acceptance, so package
        # metadata and all source/artifact hashes still pass production gates.
        manifest_path = Path(response["manifestPath"])
        bundle = json.loads(manifest_path.read_text())
        relative = "nested/textures/resource-42.png"
        image = manifest_path.parent / relative
        image.parent.mkdir(parents=True)
        image.write_bytes(texture_png if texture_png is not None else b"\x89PNG\r\n\x1a\nfixture")
        record = {"path": relative, "size": image.stat().st_size,
                  "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
        texture = copy.deepcopy(texture)
        texture["resource"] = {"dimension": 2, "arraySize": 1, "depth": 1,
                               "sampleCount": 1, "mips": 1, **texture.get("resource", {})}
        texture.setdefault("resourceId", 42)
        texture.setdefault("artifacts", [{**record, "status": "captured", "encoding": "png",
                                          "scope": "mip 0, slice 0"}])
        texture.setdefault("bindings", [{"stage": "fragment", "descriptor": {
            "firstMip": 0, "firstSlice": 0, "numMips": 1, "numSlices": 1,
            "viewFormat": {"compType": "UNormSRGB", "srgbCorrected": True}}}])
        bundle["textures"] = [texture]
        bundle["files"].append(record)
        manifest_path.write_text(json.dumps(bundle), encoding="utf-8")
        response["manifestSha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        response["files"] = bundle["files"]
        spec["textures"] = [{"sourceResourceId": 42, "targetProperty": "_BaseMap",
                             "colorSpace": "sRGB", "samplerPolicy": "target-default"}]
        if texture_sampler is not None:
            spec["textures"][0].update(sampler=texture_sampler, samplerPolicy="explicit")
        if texture_mapping is not None:
            spec["textures"][0].update(texture_mapping)
        if source_use is not None:
            spec["textures"][0]["sourceUse"] = source_use
    captured = manager.record_capture(operation_id, response)
    result = manager.finalize(operation_id, captured["capture"]["manifestSha256"], spec)
    return Path(result["packagePath"])


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "Project"
    (path / "ProjectSettings").mkdir(parents=True)
    (path / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f1\n")
    (path / "Assets").mkdir()
    (path / "Packages").mkdir()
    (path / "Packages" / "manifest.json").write_text('{"dependencies":{}}')
    return path


@pytest.fixture
def package(tmp_path):
    return finalized_package(tmp_path / "Managed")


def _read_request(result):
    return json.loads(Path(result["requestPath"]).read_text(encoding="utf-8"))


def _update_manifest(package):
    """Produce internally consistent altered packages for structural rejection tests."""
    path = package / "package-manifest.json"
    manifest = json.loads(path.read_text())
    for record in manifest["files"]:
        content = (package / record["path"]).read_bytes()
        record.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
    spec = json.loads((package / "transfer-spec.json").read_text())
    manifest["provenance"]["transferSpecSha256"] = resource_export._json_sha256(spec)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_real_finalized_package_prepares_without_reader_or_unity_and_is_idempotent(package, project):
    result = unity_restore.prepare_unity_import(package, project)
    request = _read_request(result)
    assert request["schema"] == "bread-unity-import/v1"
    assert request["mesh"]["indices"] == [0, 1, 2]
    assert request["mesh"]["attributes"]["POSITION"]["data"] == [
        [-0.5, -0.5, 0], [0.5, -0.5, 0], [0, 0.5, 0]]
    assert request["shader"] is None
    assert {gap["code"] for gap in result["gaps"]} == {"unity_shader_required"}
    assert request["visualEquivalenceVerified"] is False
    assert not list((project / "Assets").iterdir())
    assert json.loads((project / "Packages" / "manifest.json").read_text()) == {"dependencies": {}}
    stamp = Path(result["requestPath"]).stat().st_mtime_ns
    repeated = unity_restore.prepare_unity_import(package, project)
    assert repeated["requestPath"] == result["requestPath"] and repeated["idempotent"]
    assert Path(result["requestPath"]).stat().st_mtime_ns == stamp
    assert str(Path(result["requestPath"]).relative_to(project)).startswith("Library")


def test_explicit_native_strategy_has_no_missing_shader_gap(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr")
    result = unity_restore.prepare_unity_import(package, project)
    assert not result["gaps"]
    assert _read_request(result)["shaderStrategy"] == "target-native-pbr"


@pytest.mark.parametrize("components", [1, 2, 3, 4])
def test_packed_texcoords_reach_unity_with_layout_and_shader_bindings(tmp_path, project, components):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr",
                                include_uvs=True, uv_components=components)
    result = unity_restore.prepare_unity_import(package, project)
    request = _read_request(result)
    assert not result["gaps"]
    attribute = request["mesh"]["attributes"]["TEXCOORD_0"]
    assert attribute["components"] == components
    assert attribute["data"][2] == [0.5, 1, 0.75, -0.25][:components]
    binding = next(x for x in request["mesh"]["shaderBindings"] if x["unityAttribute"] == "TEXCOORD_0")
    assert binding["sourceAttributeIndex"] == 1
    assert binding["source"]["byteOffset"] == 12
    assert binding["source"]["byteStride"] == 12 + components * 4
    assert binding["source"]["sourceFormat"]["compCount"] == components
    assert binding["shaderSemantic"] == "TEXCOORD0"
    assert binding["shaderType"] == "float" + (str(components) if components > 1 else "")


def test_vertex_provenance_cannot_retarget_another_accessor(package, project):
    path = package / "asset.gltf"
    gltf = json.loads(path.read_text())
    gltf["extras"]["replaylab"]["vertexChannels"][0]["accessorIndex"] += 1
    path.write_text(json.dumps(gltf))
    _update_manifest(package)
    with pytest.raises(unity_restore.UnityRestoreError, match="provenance disagrees"):
        unity_restore.prepare_unity_import(package, project)


def test_explicit_unity_packed_channel_override_keeps_all_components(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr",
                                include_uvs=True, uv_components=4)
    gltf = json.loads((package / "asset.gltf").read_text())
    channel = gltf["extras"]["replaylab"]["vertexChannels"][1]["gltfSemantic"]
    spec_path = package / "transfer-spec.json"
    spec = json.loads(spec_path.read_text())
    spec["unity"] = {"vertexChannels": {channel: "TEXCOORD_3"}}
    spec_path.write_text(json.dumps(spec))
    _update_manifest(package)
    result = unity_restore.prepare_unity_import(package, project)
    request = _read_request(result)
    assert "TEXCOORD_0" not in request["mesh"]["attributes"]
    assert request["mesh"]["attributes"]["TEXCOORD_3"]["data"][2] == [0.5, 1, 0.75, -0.25]
    assert result["vertexBindings"][1]["shaderSemantic"] == "TEXCOORD3"
    assert result["vertexBindings"][1]["mappingBasis"] == "explicit-unity-map"


def test_packed_uv_flip_requires_component_aware_adaptation(tmp_path):
    with pytest.raises(resource_export.ResourceExportError, match="per-component UV adaptation"):
        finalized_package(tmp_path / "Managed", include_uvs=True, uv_components=4, uv_policy="flip-v")


def test_preserved_raw_only_channels_do_not_disappear_from_import_status(package, project):
    path = package / "asset.gltf"
    gltf = json.loads(path.read_text())
    gltf["extras"]["replaylab"]["omittedAttributes"] = [{"name": "vertexId", "reason": "integer precision"}]
    path.write_text(json.dumps(gltf))
    _update_manifest(package)
    result = unity_restore.prepare_unity_import(package, project)
    gap = next(x for x in result["gaps"] if x["code"] == "vertex_channel_not_imported")
    assert gap["source"]["name"] == "vertexId" and "preserved source bytes" in gap["recovery"]


@pytest.mark.parametrize("fault", ["schema", "version", "hash", "duplicate", "untracked-buffer", "source-bundle"])
def test_package_identity_and_hash_failures_do_not_write_requests(package, project, fault):
    path = package / "package-manifest.json"
    manifest = json.loads(path.read_text())
    if fault == "schema":
        manifest["schema"] = "something-else/v1"
    elif fault == "version":
        manifest["schemaVersion"] = True
    elif fault == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
    elif fault == "hash":
        (package / "asset.bin").write_bytes(b"changed")
    elif fault == "source-bundle":
        manifest["captureBundleSha256"] = "1" * 64
    else:
        gltf_path = package / "asset.gltf"
        gltf = json.loads(gltf_path.read_text())
        (package / "untracked.bin").write_bytes((package / "asset.bin").read_bytes())
        gltf["buffers"][0]["uri"] = "untracked.bin"
        gltf_path.write_text(json.dumps(gltf))
        _update_manifest(package)
        manifest = json.loads(path.read_text())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(unity_restore.UnityRestoreError):
        unity_restore.prepare_unity_import(package, project)
    assert not (project / "Library" / "Bread" / "Requests").exists()


@pytest.mark.parametrize("fault", ["negative-index", "negative-view", "before-view", "float-offset",
                                  "past-view", "past-buffer", "float-count", "bad-indices"])
def test_accessor_bounds_are_enforced_even_with_updated_hashes(package, project, fault):
    path = package / "asset.gltf"
    gltf = json.loads(path.read_text())
    accessor = gltf["accessors"][0]
    view = gltf["bufferViews"][accessor["bufferView"]]
    if fault == "negative-index":
        gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"] = -1
    elif fault == "negative-view":
        accessor["bufferView"] = -1
    elif fault == "before-view":
        view["byteOffset"] = 12
        accessor["byteOffset"] = -12
    elif fault == "float-offset":
        accessor["byteOffset"] = 0.5
    elif fault == "past-view":
        view["byteLength"] = 4
    elif fault == "past-buffer":
        view["byteLength"] = 100000
    elif fault == "float-count":
        accessor["count"] = 3.0
    else:
        index = gltf["meshes"][0]["primitives"][0]["indices"]
        gltf["accessors"][index]["componentType"] = 5126
    path.write_text(json.dumps(gltf), encoding="utf-8")
    _update_manifest(package)
    with pytest.raises(unity_restore.UnityRestoreError):
        unity_restore.prepare_unity_import(package, project)


def test_nested_texture_artifact_is_bound_to_selected_source(package, tmp_path, project):
    package = finalized_package(tmp_path / "Textures", texture={"resource": {
        "dimension": 2, "arraySize": 1, "depth": 1, "sampleCount": 1, "mips": 1}})
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert Path(texture["sourcePath"]) == package / "source/nested/textures/resource-42.png"
    assert texture["colorSpace"] == "sRGB" and texture["normalMap"] is False
    assert texture["sourceView"]["firstMip"] == 0


@pytest.mark.parametrize("setting", [
    {"filterMode": "Linear"}, {"filterMode": 1}, {"wrapU": "Border"}, {"wrapV": "1"},
    {"anisoLevel": -1}, {"anisoLevel": 17}, {"anisoLevel": 1.0}, {"anisoLevel": True},
    {"lodBias": 0.5}, {"comparison": "LessEqual"}, {"wrapW": "Repeat"},
])
def test_unsupported_sampler_is_preserved_as_nonblocking_gap(tmp_path, project, setting):
    source_sampler = {"filterMode": "Point", "wrapU": "Clamp", "anisoLevel": 4, **setting}
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr",
                                texture={}, texture_sampler=source_sampler)
    result = unity_restore.prepare_unity_import(package, project)
    request = _read_request(result)
    assert len(request["textures"]) == 1
    texture = request["textures"][0]
    assert texture["sourceSampler"] == source_sampler
    assert texture["sampler"] == {k: v for k, v in source_sampler.items() if k not in setting}
    assert result["gaps"] == [{"code": "sampler_mapping_unsupported", "resourceId": "42",
                               "unsupportedFields": sorted(setting)}]


@pytest.mark.parametrize("sampler,expected", [
    ({"filterMode": "point", "wrapU": "repeat", "wrapV": "clamp", "anisoLevel": 0},
     {"filterMode": "Point", "wrapU": "Repeat", "wrapV": "Clamp", "anisoLevel": 0}),
    ({"filterMode": "Bilinear", "wrapU": "Mirror", "wrapV": "MirrorOnce", "anisoLevel": 16},
     {"filterMode": "Bilinear", "wrapU": "Mirror", "wrapV": "MirrorOnce", "anisoLevel": 16}),
    ({"filterMode": "Trilinear"}, {"filterMode": "Trilinear"}),
])
def test_supported_sampler_roundtrips_only_importable_settings(tmp_path, project, sampler, expected):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr",
                                texture={}, texture_sampler=sampler)
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert texture["sampler"] == expected and texture["sourceSampler"] == sampler
    assert not result["gaps"]


def test_non_object_sampler_cannot_reach_editor_as_silent_default(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr",
                                texture={}, texture_sampler=["Point"])
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert texture["sampler"] == {} and texture["sourceSampler"] == ["Point"]
    assert result["gaps"] == [{"code": "sampler_mapping_unsupported", "resourceId": "42",
                               "unsupportedFields": ["sampler"]}]


@pytest.mark.parametrize("source_api", ["D3D11", "D3D12", "Vulkan", "OpenGL"])
def test_api_family_does_not_trigger_texture_or_uv_flip(tmp_path, project, source_api):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={},
                                include_uvs=True, source_api=source_api, texture_png=SMOKE_WHITE_PNG)
    request = _read_request(unity_restore.prepare_unity_import(package, project))
    assert request["platformContract"]["sourceApi"] == source_api
    assert request["platformContract"]["automaticApiFlip"] is False
    assert request["platformContract"]["imagePixelsRewritten"] is False
    assert request["mesh"]["attributes"]["TEXCOORD_0"]["data"] == [[0, 0], [1, 0], [0.5, 1]]
    assert "unityUvTransform" not in request["textures"][0]
    assert Path(request["textures"][0]["sourcePath"]).read_bytes() == SMOKE_WHITE_PNG


@pytest.mark.parametrize("space,expected", [
    ("captured-uv", {"scale": [1, 1], "offset": [0, 0]}),
    ("exported-uv", {"scale": [1, -1], "offset": [0, 1]}),
])
def test_per_texture_transform_is_composed_after_known_mesh_uv_bake(tmp_path, project, space, expected):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={},
        include_uvs=True, uv_policy="flip-v", texture_mapping={
            "uvTransform": {"scale": [1, -1], "offset": [0, 1], "space": space}})
    result = unity_restore.prepare_unity_import(package, project)
    request = _read_request(result)
    assert request["mesh"]["attributes"]["TEXCOORD_0"]["data"] == [[0, 1], [1, 1], [0.5, 0]]
    transform = request["textures"][0]["unityUvTransform"]
    assert transform["scale"] == expected["scale"] and transform["offset"] == expected["offset"]
    assert transform["provenance"] == {"inputSpace": space, "meshUvPolicy": "flip-v",
                                       "operation": "explicit-per-texture"}
    assert not result["gaps"]


@pytest.mark.parametrize("transform", [
    {"scale": [1, -1], "offset": [0, 1], "rotation": 90},
    {"scale": [1, -1], "offset": [0, 1], "space": ["captured-uv"]},
    {"scale": [True, 1], "offset": [0, 0]}, {"scale": [1], "offset": [0, 0]},
])
def test_unrepresentable_uv_mapping_is_a_gap_and_never_implicitly_applied(tmp_path, project, transform):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={},
                                texture_mapping={"uvTransform": transform})
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert texture["uvTransform"] == transform and "unityUvTransform" not in texture
    assert {gap["code"] for gap in result["gaps"]} == {"texture_uv_transform_unsupported"}


@pytest.mark.parametrize("view,requested,codes", [
    ({"compType": "UNormSRGB", "srgbCorrected": True}, "sRGB", set()),
    ({"srgbCorrected": True}, "sRGB", set()),
    ({"compType": "UNorm", "srgbCorrected": False}, "sRGB", {"texture_sampling_color_space_mismatch"}),
    ({"compType": "UNormSRGB", "srgbCorrected": True}, "linear", {"texture_sampling_color_space_mismatch"}),
    ({"compType": "Typeless", "srgbCorrected": False}, "sRGB", {"texture_sampling_color_space_unknown"}),
    ({}, "sRGB", {"texture_sampling_color_space_unknown"}),
])
def test_bound_view_color_space_wins_over_resource_format_and_unknown_is_reported(
        tmp_path, project, view, requested, codes):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={
        "resource": {"format": {"compType": "UNormSRGB", "srgbCorrected": True}},
        "bindings": [{"stage": "fragment", "descriptor": {"viewFormat": view}}]},
        texture_mapping={"colorSpace": requested})
    result = unity_restore.prepare_unity_import(package, project)
    contract = _read_request(result)["textures"][0]["colorSpaceContract"]
    assert contract["requested"] == requested and contract["effective"] == requested
    assert contract["resourceFormatSpace"] == "sRGB"
    assert {gap["code"] for gap in result["gaps"]} == codes


def test_unity_normal_repacking_requires_linear_import_and_matching_shader_decode(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={},
        texture_mapping={"role": "normal", "unityNormalMap": True, "colorSpace": "sRGB"})
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert texture["colorSpace"] == "linear" and texture["normalMap"] is True
    assert texture["colorSpaceContract"]["requested"] == "sRGB"
    assert {gap["code"] for gap in result["gaps"]} == {
        "texture_data_color_space_conflict", "texture_sampling_color_space_mismatch", "normal_map_decode_unverified"}


@pytest.mark.parametrize("explicit_transform", [False, True])
def test_material_st_mapping_is_never_overwritten_by_texture_uv_mapping(tmp_path, project, explicit_transform):
    mapping = {"uvTransform": {"scale": [1, -1], "offset": [0, 1]}} if explicit_transform else {}
    package = finalized_package(tmp_path / "Managed", texture={}, shader_source=SMOKE_URP_SHADER,
        texture_mapping=mapping, material_target="_BaseMap_ST", material_color=[1, -1, 0, 1])
    result = unity_restore.prepare_unity_import(package, project)
    request = _read_request(result)
    assert len(request["properties"]) == 1
    assert {key: request["properties"][0][key] for key in ("name", "type", "value")} == {"name": "_BaseMap_ST", "type": "color", "value": [1, -1, 0, 1]}
    assert request["properties"][0]["sourceIdentity"]["physicalId"]
    assert "unityUvTransform" not in request["textures"][0]
    assert {gap["code"] for gap in result["gaps"]} == ({"texture_uv_parameter_conflict"} if explicit_transform else set())


def test_data_role_does_not_silently_change_explicit_color_space_or_repack_normals(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={},
                                texture_mapping={"role": "normal", "colorSpace": "sRGB"})
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert texture["colorSpace"] == "sRGB" and texture["normalMap"] is False
    assert {gap["code"] for gap in result["gaps"]} == {"texture_data_color_space_conflict"}


def test_sampler_capture_evidence_is_preserved_without_claiming_default_is_exact(tmp_path, project):
    source_sampler = {"addressU": "Border", "compareFunction": "LessEqual", "mipBias": 0.75}
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={
        "bindings": [{"stage": "fragment", "combinedSampler": source_sampler,
                      "descriptor": {"viewFormat": {"compType": "UNormSRGB"}}}]})
    result = unity_restore.prepare_unity_import(package, project)
    texture = _read_request(result)["textures"][0]
    assert texture["samplerPolicy"] == "target-default" and texture["sampler"] == {}
    assert texture["capturedSamplerEvidence"]["combinedSampler"] == source_sampler
    assert texture["capturedSamplerEvidence"]["separateSamplerAssociationInferred"] is False
    assert _read_request(result)["visualEquivalenceVerified"] is False


def test_unknown_source_mips_do_not_claim_a_preserved_chain(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", strategy="target-native-pbr", texture={"resource": {"mips": 0}})
    result = unity_restore.prepare_unity_import(package, project)
    assert _read_request(result)["textures"][0]["mipPolicy"] == "base-level-image"
    assert {gap["code"] for gap in result["gaps"]} == {"source_mip_count_unknown"}


@pytest.mark.parametrize("resource", [{"arraySize": 2}, {"cubemap": True}, {"sampleCount": 4},
                                     {"depth": 4}, {"dimension": 3}, {"arraySize": 0},
                                     {"sampleCount": 0}, {"dimension": 0}])
def test_single_image_cannot_claim_array_cube_volume_or_msaa_restore(tmp_path, project, resource):
    package = finalized_package(tmp_path / "Managed", texture={"resource": resource})
    result = unity_restore.prepare_unity_import(package, project)
    assert not _read_request(result)["textures"]
    assert "texture_requires_conversion" in {gap["code"] for gap in result["gaps"]}


@pytest.mark.parametrize("field", ["firstMip", "firstSlice"])
def test_binding_nested_subresource_is_used_even_when_selector_only_names_stage(tmp_path, project, field):
    texture = {"resource": {"dimension": 2, "arraySize": 1, "mips": 4},
               "bindings": [{"stage": "fragment", "descriptor": {field: 2}}]}
    package = finalized_package(tmp_path / "Managed", texture=texture, source_use={"stage": "fragment"})
    result = unity_restore.prepare_unity_import(package, project)
    assert not _read_request(result)["textures"]
    assert "texture_requires_conversion" in {gap["code"] for gap in result["gaps"]}


def test_multiple_source_mips_are_reported_as_incomplete(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", texture={"resource": {"mips": 4}})
    result = unity_restore.prepare_unity_import(package, project)
    assert len(_read_request(result)["textures"]) == 1
    assert "source_mips_not_preserved" in {gap["code"] for gap in result["gaps"]}


def test_changed_existing_request_is_not_overwritten(package, project):
    result = unity_restore.prepare_unity_import(package, project)
    path = Path(result["requestPath"])
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(unity_restore.UnityRestoreError, match="existing Unity import request changed"):
        unity_restore.prepare_unity_import(package, project)
    assert path.read_text() == "changed"


def test_redirected_library_is_rejected_before_creating_bread_directory(package, project, tmp_path):
    outside = tmp_path / "Outside"
    outside.mkdir()
    try:
        (project / "Library").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(unity_restore.UnityRestoreError, match="redirected"):
        unity_restore.prepare_unity_import(package, project)
    assert not (outside / "Bread").exists()


def test_redirected_source_root_is_rejected(package, project, tmp_path):
    link = tmp_path / "PackageLink"
    try:
        link.symlink_to(package, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(unity_restore.UnityRestoreError, match="redirected"):
        unity_restore.prepare_unity_import(link, project)


@pytest.mark.parametrize("missing", ["Assets", "Packages/manifest.json", "ProjectSettings/ProjectVersion.txt"])
def test_incomplete_unity_project_is_rejected_before_writing_library(package, project, missing):
    path = project / missing
    path.rmdir() if path.is_dir() else path.unlink()
    with pytest.raises(unity_restore.UnityRestoreError):
        unity_restore.prepare_unity_import(package, project)
    assert not (project / "Library").exists()


def test_synthetic_shader_texture_and_material_provenance_survive_the_production_pipeline(tmp_path, project):
    color = [0.125, 0.5, 0.875, 1.0]
    package = finalized_package(tmp_path / "ShaderFixture", strategy="generated-forward",
        texture={"resource": {"width": 1, "height": 1}}, texture_png=SMOKE_WHITE_PNG,
        shader_source=SMOKE_URP_SHADER, material_color=color, asset_name="BreadTexturedSynthetic")
    request = _read_request(unity_restore.prepare_unity_import(package, project))
    assert not request["gaps"]
    assert request["shaderStrategy"] == "generated-forward"
    assert Path(request["shader"]["sourcePath"]).read_text() == SMOKE_URP_SHADER
    assert request["textures"][0]["property"] == "_BaseMap"
    assert request["textures"][0]["colorSpace"] == "sRGB"
    assert Path(request["textures"][0]["sourcePath"]).read_bytes() == SMOKE_WHITE_PNG
    assert len(request["properties"]) == 1
    assert {key: request["properties"][0][key] for key in ("name", "type", "value")} == {"name": "_BaseColor", "type": "color", "value": color}
    assert request["properties"][0]["sourceIdentity"]["physicalId"]
    bundle = json.loads((package / "source/manifest.json").read_text())
    assert bundle["syntheticFixture"]["notAnActualGameCapture"] is True
    assert bundle["shaders"][0]["encoding"] == "synthetic-text-fixture"
    assert "TransformObjectToHClip" in SMOKE_URP_SHADER


def test_multifile_implementation_retains_primary_shader_includes_and_pipeline(tmp_path, project):
    options = {"pipelinePolicy": {"targetPipeline": "built-in", "allowCreateUrp": False},
               "shaderAssetPath": "Shaders/Captured.shader", "assets": [
                   {"path": "Shaders/Captured.shader", "kind": "shader", "content": 'Shader "Bread/Test" { SubShader {} }'},
                   {"path": "Shaders/Material.hlsl", "kind": "include", "content": "float4 EvaluateMaterial() { return 1; }"},
                   {"path": "Runtime/Effect.cs", "kind": "script", "content": "class Effect {}"}],
               "scene": {"camera": {"projection": "perspective"}, "lights": []},
               "runtimeBindings": [{"name": "_Matrix", "type": "matrix", "value": [1] * 16}]}
    package = finalized_package(tmp_path / "Managed", unity_options=options)
    request = _read_request(unity_restore.prepare_unity_import(package, project))
    assert request["pipelinePolicy"] == options["pipelinePolicy"]
    assert len(request["assets"]) == 3
    assert request["shader"]["sourcePath"].endswith("Captured.shader")
    assert all(hashlib.sha256(Path(asset["sourcePath"]).read_bytes()).hexdigest() == asset["sha256"] for asset in request["assets"])
    assert request["runtimeBindings"] == options["runtimeBindings"]
    assert "unity_shader_required" not in {gap["code"] for gap in request["gaps"]}


def test_pipeline_creation_requires_explicit_urp_target(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", unity_options={"pipelinePolicy": {"allowCreateUrp": True}})
    with pytest.raises(unity_restore.UnityRestoreError, match="targetPipeline=urp"):
        unity_restore.prepare_unity_import(package, project)


def test_primary_shader_cannot_reference_an_unmanifested_file(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", unity_options={"shaderAssetPath": "External.shader"})
    with pytest.raises(unity_restore.UnityRestoreError, match="manifested Shader"):
        unity_restore.prepare_unity_import(package, project)


def test_buffer_binding_uses_exact_pre_action_window_and_readback_hash(tmp_path):
    raw = struct.pack("<4I", 0xDEADBEEF, 2, 3, 4)
    path = tmp_path / "source/buffer.bin"
    path.parent.mkdir()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    bundle = {"buffers": [{"resourceId": "123", "status": "captured", "capturePhase": "before-action", "byteOffset": 256, "byteSize": len(raw),
        "raw": {"path": "buffer.bin", "sha256": digest, "size": len(raw)},
        "bindings": [{"stage": "vertex", "descriptor": {"elementByteSize": 8}}]}]}
    entry = {"name": "_Bones", "bufferSource": {"resourceId": "123", "sourceUse": {"stage": "vertex"}}}
    bindings = unity_restore._buffer_bindings([entry], bundle, tmp_path, {"source/buffer.bin": digest})
    assert bindings[0]["stride"] == 8 and base64.b64decode(bindings[0]["bufferBase64"]) == raw
    assert bindings[0]["sourceIdentity"]["byteOffset"] == 256
    assert bindings[0]["sourceIdentity"]["sha256"] == digest
    with pytest.raises(unity_restore.UnityRestoreError, match="before-action"):
        unity_restore._buffer_bindings([{**entry, "bufferSource": {"resourceId": "123", "phase": "after-action"}}], bundle, tmp_path, {"source/buffer.bin": digest})
    path.write_bytes(bytes(len(raw)))
    with pytest.raises(unity_restore.UnityRestoreError, match="changed while preparing"):
        unity_restore._buffer_bindings([entry], bundle, tmp_path, {"source/buffer.bin": digest})
