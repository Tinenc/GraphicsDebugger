import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src.shader_workspace import ShaderKey
from src.shader_workspace.catalog import capture_identity, load_selected_shader
from src.shader_workspace.errors import CatalogValidationError, ToolExecutionError, ManifestValidationError
from src.shader_workspace import workspace as ws
from tests.test_spirv_workspace import spirv_workspace_fixture
from tests.test_native_shader_compile import response


@pytest.fixture
def glsl_fixture(spirv_workspace_fixture):
    f = spirv_workspace_fixture
    raw = b"#version 330\nlayout(location=0) out vec4 color;\nvoid main(){color=vec4(1);}\n"
    key = ShaderKey(501, "fragment", "main", hashlib.sha256(raw).hexdigest())
    path = f["catalogPath"]
    catalog = json.loads(path.read_text())
    catalog.update(api="GraphicsAPI.OpenGL", capturePath="D:/captures/opengl-frame.rdc")
    row = catalog["shaders"][0]
    row.update(shaderKey=key.catalog_key, rawSha256=key.raw_sha256, encoding="ShaderEncoding.GLSL",
               rawSize=len(raw), rawPath="blobs/" + key.raw_sha256 + ".glsl")
    catalog["uses"][0].update(shaderKey=key.catalog_key, rawSha256=key.raw_sha256)
    catalog["captureId"] = capture_identity(catalog, [key])
    (path.parent / row["rawPath"]).write_bytes(raw)
    path.write_text(json.dumps(catalog), encoding="utf-8")
    f.update(key=key, blob=raw, catalog=catalog)
    f["service"]._native_compile_provider = lambda request: response(request, Path(request["candidate_path"]).read_bytes())
    return f


def test_captured_glsl_workspace_native_compile_reflect_and_reload(glsl_fixture):
    f = glsl_fixture
    service, root = f["service"], f["root"] / "native-glsl"
    with mock.patch.object(ws, "run_operation", side_effect=AssertionError("No external compiler/decompiler")):
        service.create(f["catalogPath"], f["key"], root)
        initial = service.status(root)
        assert initial["source"]["origin"] == "captured-native-source"
        assert initial["source"]["format"] == "glsl"
        assert service.reuse_pristine_decompiled_source(root)["reusedPreparedSource"]
        reference = service.decompile_reference(root)
        assert reference["decompilerExecuted"] is False and reference["sourceSha256"] == f["key"].raw_sha256
        before = (root / "manifest.json").read_bytes()
        assert service.compile_check(root, f["blob"].decode())["ok"]
        assert (root / "manifest.json").read_bytes() == before
        source = root / "source/shader.glsl"
        source.write_bytes(f["blob"] + b"// edited\n")
        compiled = service.compile(root)
    assert compiled["kind"] == "GLSL" and compiled["sourceSha256"] == compiled["rawSha256"]
    request = service.candidate_reflection_request(root)
    assert request["binding"]["encoding"] == "glsl"
    assert request["binding"]["targetProfile"] == "glsl"
    reflected = dict(schema="renderdoc-ultra-shader-reflection/v1", schemaVersion=1,
        captureId=request["binding"]["captureId"], captureSessionId="a" * 32,
        rawSha256=compiled["rawSha256"], reflectedRawSha256=compiled["rawSha256"],
        byteSize=Path(compiled["candidateBlobPath"]).stat().st_size, encoding="glsl",
        stage="fragment", entryPoint="main", reflectedEntryPoint="main",
        buildTargetShaderSucceeded=True, reflectionSucceeded=True, freeTargetResourceSucceeded=True,
        compiledInterface=f["catalog"]["shaders"][0])
    assert service.validate_candidate_reflection(root, reflected)["compatible"]
    final = service.status(root)
    assert final["latestCompiled"]["abiCompatible"]
    assert service._project_compiler_binding(json.loads((root / "manifest.json").read_text()))["backend"] == "renderdoc-native"
    (root / compiled["nativeCompile"]["outputPath"]).write_bytes(b"tampered")
    with pytest.raises(ManifestValidationError): service.status(root)


def test_glsl_capture_requires_opengl_not_inferred_from_text(glsl_fixture):
    f = glsl_fixture
    f["catalog"]["api"] = "Vulkan"
    f["catalog"]["captureId"] = capture_identity(f["catalog"], [f["key"]])
    f["catalogPath"].write_text(json.dumps(f["catalog"]), encoding="utf-8")
    with pytest.raises(CatalogValidationError, match="OpenGL"):
        load_selected_shader(f["catalogPath"], f["key"])


@pytest.mark.parametrize("encoding", ["GLSL", "ShaderEncoding.GLSL", "shaderencoding.glsl"])
def test_catalog_accepts_only_explicit_native_glsl_encodings(glsl_fixture, encoding):
    f = glsl_fixture
    f["catalog"]["shaders"][0]["encoding"] = encoding
    f["catalogPath"].write_text(json.dumps(f["catalog"]), encoding="utf8")
    selected = load_selected_shader(f["catalogPath"], f["key"])
    assert selected.encoding == "glsl"
    assert selected.blob == f["blob"]
    assert selected.key.raw_sha256 == hashlib.sha256(selected.blob).hexdigest()


@pytest.mark.parametrize("encoding", ["", "Unknown.GLSL", "ShaderEncoding.HLSL", "ShaderEncoding.DXIL"])
def test_glsl_text_is_not_inferred_for_missing_or_contradicting_encoding(glsl_fixture, encoding):
    f = glsl_fixture
    f["catalog"]["shaders"][0]["encoding"] = encoding
    f["catalogPath"].write_text(json.dumps(f["catalog"]), encoding="utf8")
    with pytest.raises(CatalogValidationError, match="supported executable"):
        load_selected_shader(f["catalogPath"], f["key"])


@pytest.mark.parametrize("stage,entry", [("mesh", "main"), ("fragment", "DifferentEntry")])
def test_native_glsl_catalog_rejects_unsupported_stage_or_entry(glsl_fixture, stage, entry):
    f = glsl_fixture
    key = ShaderKey(f["key"].resource_id, stage, entry, f["key"].raw_sha256)
    f["catalog"]["shaders"][0].update(stage=stage, entryPoint=entry, shaderKey=key.catalog_key)
    f["catalog"]["captureId"] = capture_identity(f["catalog"], [key])
    f["catalogPath"].write_text(json.dumps(f["catalog"]), encoding="utf8")
    with pytest.raises(CatalogValidationError, match="pipeline stage and main entry"):
        load_selected_shader(f["catalogPath"], key)


def test_glsl_compile_failure_preserves_editable_source_and_prior_revision(glsl_fixture):
    f = glsl_fixture
    service, root = f["service"], f["root"] / "native-failed"
    service.create(f["catalogPath"], f["key"], root)
    service._native_compile_provider = lambda request: response(request, status="failed")
    with pytest.raises(ToolExecutionError, match="native compiler"):
        service.compile(root)
    assert (root / "source/shader.glsl").read_bytes() == f["blob"]
    assert service.status(root)["latestCompiled"] is None


def test_native_glsl_receipt_cannot_bind_different_artifact_text(glsl_fixture):
    f = glsl_fixture
    service, root = f["service"], f["root"] / "native-mismatch"
    service.create(f["catalogPath"], f["key"], root)
    before = (root / "manifest.json").read_bytes()
    service._native_compile_provider = lambda request: response(request, f["blob"] + b"// different bytes\n")
    with pytest.raises(ToolExecutionError, match="exact compiled source"):
        service.compile(root)
    assert (root / "manifest.json").read_bytes() == before


def test_glsl_project_candidate_import_revalidates_source_without_reusing_abi(glsl_fixture):
    from tests.test_shader_workspace_routes import ShaderWorkspaceRouteChainTests
    f = glsl_fixture
    service, root = f["service"], f["root"] / "native-import"
    service.create(f["catalogPath"], f["key"], root)
    status = service.status(root)
    artifact = f["root"] / "candidate.glsl"
    artifact.write_bytes(f["blob"])
    plan = ShaderWorkspaceRouteChainTests._project_candidate_plan(artifact,
        raw_sha256=f["key"].raw_sha256, code_sha256=f["key"].raw_sha256,
        byte_size=len(f["blob"]), kind="GLSL", source_sha256=f["key"].raw_sha256,
        source_revision=1, target_profile=status["targetProfile"])
    manifest = json.loads((root / "manifest.json").read_text())
    plan["compilerBindingSha256"] = service._project_compiler_binding(manifest)["bindingSha256"]
    bad = dict(plan, compilerBindingSha256="0" * 64)
    with pytest.raises(ToolExecutionError, match="compiler binding"):
        service.hydrate_project_candidate(root, bad)
    imported = service.hydrate_project_candidate(root, plan)
    assert imported["requiresReplayAbiValidation"] is True
    current = service.status(root)["latestCompiled"]
    assert current["kind"] == "GLSL" and current["abiCompatible"] is False


def test_verified_project_glsl_can_replace_only_pristine_captured_source(glsl_fixture):
    f = glsl_fixture
    service, root = f["service"], f["root"] / "native-source-import"
    service.create(f["catalogPath"], f["key"], root)
    before = service.status(root)
    source = f["blob"].decode() + "// verified project edit\n"
    bundle = {"schema": "replaylab-verified-project-shader-source/v1",
        "projectId": "a" * 32, "projectPath": str(f["root"] / "project"),
        "knowledgeId": "b" * 32, "sourceText": source, "sourceFormat": "glsl",
        "sourceEntryPoint": "main", "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
        "originalRawSha256": before["baseline"]["rawSha256"],
        "baselineAbiSha256": before["baseline"]["abiSha256"], "stage": before["stage"],
        "entryPoint": "main", "targetProfile": before["targetProfile"],
        "originWorkspaceId": "c" * 32, "originWorkspacePath": str(f["root"] / "origin"),
        "analysisSha256": "", "applied": False, "requiresCompile": True,
        "requiresLiveAbiRevalidation": True, "semanticEquivalenceVerified": False}
    with mock.patch("src.projects.ReplayProjectService.prepare_workspace_source", return_value=bundle):
        service.import_project_source(root, bundle)
        assert service.status(root)["source"]["origin"] == "verified-project-source"
        assert service.compile(root)["kind"] == "GLSL"
        with pytest.raises(ToolExecutionError, match="overwrite"):
            service.import_project_source(root, bundle)
    assert (root / "source/shader.glsl").read_text() == source


def test_baseline_glsl_reflection_can_start_before_any_compile(glsl_fixture):
    from src.shader_workspace.baseline_reflection_recovery import reflection_request
    f = glsl_fixture
    service, root = f["service"], f["root"] / "native-reflection"
    service.create(f["catalogPath"], f["key"], root)
    manifest = json.loads((root / "manifest.json").read_text())
    request = reflection_request(service, root, manifest)
    assert request["binding"]["revision"] == 1
    assert request["binding"]["encoding"] == "glsl"
    assert manifest["revision"] == 0
