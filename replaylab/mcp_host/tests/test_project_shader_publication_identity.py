"""Legacy capture rows become complete only at verified publication boundaries."""
import copy

import pytest

from src import project_restore as restore


def publication(api="GraphicsAPI.Vulkan", encoding="SPIRV"):
    status = {"workspaceId": "1" * 32, "stage": "fragment", "entryPoint": "main",
              "shader": {"resourceId": 72473}, "capture": {"api": api},
              "baseline": {"kind": encoding, "rawSha256": "2" * 64, "abiSha256": "3" * 64}}
    catalog = {"resourceId": 72473, "rawSha256": "2" * 64, "stage": "fragment",
               "entryPoint": "main", "encoding": "ShaderEncoding." + encoding,
               "interfaceComplete": True, "interfaceHash": "4" * 64,
               "layoutHash": "5" * 64, "eventIds": [5371]}
    replacement = {"anchorEventId": 5371, "physicalAbiSha256": "3" * 64}
    return catalog, status, replacement


@pytest.mark.parametrize("api,encoding,expected", [
    ("GraphicsAPI.D3D11", "DXBC", "d3d11"),
    ("GraphicsAPI.D3D12", "DXIL", "d3d12"),
    ("GraphicsAPI.Vulkan", "SPIRV", "vulkan"),
    ("GraphicsAPI.OpenGL", "GLSL", "opengl"),
    ("GraphicsAPI.OpenGL", "OpenGLSPIRV", "opengl"),
])
def test_legacy_complete_interface_gets_capture_api_and_verified_physical_abi(api, encoding, expected):
    catalog, status, replacement = publication(api, encoding)
    before = copy.deepcopy((catalog, status, replacement))
    assert restore._normal_shader_identity(catalog)["stableKeyComplete"] is False
    identity = restore._publication_shader_identity(catalog, status, replacement)
    assert identity["stableKeyComplete"] is True
    assert identity["api"] == expected
    assert identity["encoding"] == restore._asset_encoding(encoding)
    assert identity["abiHash"] == status["baseline"]["abiSha256"]
    assert identity["abiHash"] != catalog["interfaceHash"]
    assert identity["layoutHash"] == catalog["layoutHash"]
    assert identity["anchorEventId"] == replacement["anchorEventId"]
    assert restore._normal_shader_identity(identity) == identity
    assert (catalog, status, replacement) == before
    receipt = restore._realized_receipt_from_record(
        {"identity": identity, "managedWorkspace": {"workspaceId": status["workspaceId"]},
         "candidateRevision": 2, "candidateRawSha256": "6" * 64}, "3" * 64)
    assert receipt["stableKeySha256"] == identity["stableKeySha256"]
    assert restore._capture_shader({"capture": {"shaders": [catalog]}}, identity) == catalog


@pytest.mark.parametrize("field,value", [
    ("resourceId", 8), ("rawSha256", "7" * 64), ("stage", "vertex"),
    ("entryPoint", "another"), ("api", "d3d12"), ("encoding", "DXIL"),
    ("layoutHash", ""), ("layoutHash", "invalid"),
])
def test_legacy_completion_rejects_wrong_original_or_missing_layout(field, value):
    catalog, status, replacement = publication()
    catalog[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        restore._publication_shader_identity(catalog, status, replacement)


@pytest.mark.parametrize("field,value", [
    ("stableKeyComplete", True), ("stableKeySha256", "8" * 64), ("abiHash", "9" * 64),
])
def test_explicit_v2_identity_is_never_silently_repaired(field, value):
    catalog, status, replacement = publication()
    catalog[field] = value
    with pytest.raises(RuntimeError):
        restore._publication_shader_identity(catalog, status, replacement)


def test_legacy_catalog_never_gains_executable_authority_from_interface_complete():
    catalog, status, replacement = publication()
    restore._publication_shader_identity(catalog, status, replacement)
    legacy = restore._normal_shader_identity(catalog)
    assert legacy["stableKeyComplete"] is False
    assert legacy["api"] == "" and legacy["encoding"] == ""
    assert legacy["anchorEventId"] == 5371


def test_oldest_interface_hash_catalog_can_publish_without_completeness_flag():
    catalog, status, replacement = publication()
    catalog.pop("interfaceComplete")
    catalog.pop("encoding")
    identity = restore._publication_shader_identity(catalog, status, replacement)
    assert identity["abiHash"] == status["baseline"]["abiSha256"]
    assert identity["stableKeyComplete"] is True
    assert restore._capture_shader({"capture": {"shaders": [catalog]}}, identity) == catalog


def test_old_interface_with_new_anchor_remains_only_routing_evidence():
    catalog, status, replacement = publication()
    catalog.pop("eventIds")
    catalog["anchorEventId"] = 5371
    assert restore._normal_shader_identity(catalog)["stableKeyComplete"] is False
    identity = restore._publication_shader_identity(catalog, status, replacement)
    assert restore._capture_shader({"capture": {"shaders": [catalog]}}, identity) == catalog


@pytest.mark.parametrize("encoding", ["", "spv", "ShaderEncoding.SPIRV"])
@pytest.mark.parametrize("api", [None, "", "GraphicsAPI.Vulkan"])
def test_old_interface_with_new_routing_hints_stays_incomplete(encoding, api):
    catalog, status, replacement = publication()
    catalog["encoding"] = encoding
    if api is not None:
        catalog["api"] = api
    catalog["anchorEventId"] = 5371
    assert restore._normal_shader_identity(catalog)["stableKeyComplete"] is False
    identity = restore._publication_shader_identity(catalog, status, replacement)
    assert identity["stableKeyComplete"] is True
    assert restore._capture_shader({"capture": {"shaders": [catalog]}}, identity) == catalog


@pytest.mark.parametrize("api,encoding", [("d3d11", "DXBC"), ("d3d12", "DXIL"),
    ("vulkan", "SPIRV"), ("opengl", "GLSL"), ("opengl", "OpenGLSPIRV")])
def test_legacy_roundtrip_uses_exact_original_with_another_verified_anchor(api, encoding):
    catalog, status, replacement = publication(api, encoding)
    catalog["eventIds"] = [19]
    identity = restore._publication_shader_identity(catalog, status, replacement)
    assert identity["anchorEventId"] == 5371
    assert restore._capture_shader({"capture": {"shaders": [catalog]}}, identity) == catalog
    # A second RDC attaches the same stable program to its own RID and EID.
    # Only its newly bound local identity may match; never reuse an old RID.
    other_catalog = {**catalog, "resourceId": 81, "eventIds": [7]}
    other_status = copy.deepcopy(status)
    other_status["shader"]["resourceId"] = 81
    other_identity = restore._publication_shader_identity(other_catalog, other_status,
        {**replacement, "anchorEventId": 11})
    assert other_identity["stableKeySha256"] == identity["stableKeySha256"]
    state = {"capture": {"shaders": [other_catalog]}}
    assert restore._capture_shader(state, other_identity) == other_catalog
    with pytest.raises(RuntimeError, match="exact project catalog"):
        restore._capture_shader(state, identity)


@pytest.mark.parametrize("field,value", [("api", "d3d12"), ("encoding", "DXIL"),
    ("layoutHash", "6" * 64), ("rawSha256", "7" * 64)])
def test_legacy_restore_rejects_explicit_conflicts_even_with_same_rid(field, value):
    catalog, status, replacement = publication()
    identity = restore._publication_shader_identity(catalog, status, replacement)
    catalog[field] = value
    with pytest.raises(RuntimeError, match="exact project catalog"):
        restore._capture_shader({"capture": {"shaders": [catalog]}}, identity)


def test_legacy_workspace_paths_keep_existing_project_and_unicode_names(tmp_path):
    project = tmp_path / "ReplayLab旧项目 名称"
    shader_root = project / "workspaces" / "shaders"
    workspace = shader_root / "旧版自定义 着色器"
    workspace.mkdir(parents=True)
    source = workspace / "source" / "原始名称.hlsl"
    source.parent.mkdir()
    source.write_bytes(b"original source bytes\r\n")
    before = source.read_bytes()
    relative = workspace.relative_to(project).as_posix()
    resolved = restore._resolve_workspace_path(project, shader_root, relative)
    assert resolved == workspace.resolve()
    assert source.read_bytes() == before and source.name == "原始名称.hlsl"
    assert project.name == "ReplayLab旧项目 名称"
