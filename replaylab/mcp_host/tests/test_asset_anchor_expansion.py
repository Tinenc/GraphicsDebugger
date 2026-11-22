"""Content leads use exact adapter relations without requiring all draw textures."""
from pathlib import Path
from unittest import mock
import json

import pytest

from src.asset_catalog import AssetCatalogError, AssetCatalogService
from src import server
from src.shader_asset_retrieval import anchor_expansion, retrieve, _consistency
from src.symbols.imaging import fingerprint
from tests.test_texture_identity import gradient_rgba, png_bytes


def write(root, name, text):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text if isinstance(text, bytes) else text.encode())
    return target


def catalog_at(tmp_path, files):
    root = tmp_path / "assets"
    for path, content in files.items():
        write(root, path, content)
    catalog = AssetCatalogService(tmp_path / "catalog.sqlite3")
    catalog.scan(root, build_id="test", scan_mode="full")
    return catalog, root


def anchor(catalog, path):
    asset = catalog.query(relative_paths=[path], limit=1)["assets"][0]
    return [{"texture": "Texture2D 7", "resourceId": 7, "matches": [{
        "assetId": asset["assetId"], "ownerPath": path, "distance": 0,
        "verdict": "same-image", "transform": "identity"}]}]


def test_one_generic_texture_finds_material_and_mesh_without_crossing_shared_texture(tmp_path):
    image = png_bytes(gradient_rgba(16, 16), 16, 16)
    catalog, root = catalog_at(tmp_path, {
        "Scene/Textures/anchor.png": image,
        "Scene/Textures/shared.png": png_bytes(bytes([200, 200, 200, 255]) * 256, 16, 16),
        "Scene/Materials/surface.mtl": "newmtl Surface\nmap_Kd ../Textures/anchor.png\nmap_Ks ../Textures/shared.png\n",
        "Scene/Other/unrelated.mtl": "newmtl Other\nmap_Kd ../Textures/shared.png\n",
        "Scene/Meshes/object.obj": "mtllib ../Materials/surface.mtl\nv 0 0 0\n",
    })
    first = anchor(catalog, "Scene/Textures/anchor.png")
    seed = first[0]["matches"][0]["assetId"]
    with mock.patch.object(catalog, "scan", side_effect=AssertionError("no scan")), \
            mock.patch.object(Path, "rglob", side_effect=AssertionError("no tree walk")):
        graph = catalog.expand_reference_neighborhood([seed])
    paths = {item["relativePath"] for item in graph["assets"]}
    assert "Scene/Materials/surface.mtl" in paths
    assert "Scene/Meshes/object.obj" in paths
    assert "Scene/Other/unrelated.mtl" not in paths
    assert graph["filesystemScanned"] is False
    expansion = anchor_expansion(catalog, first)
    candidate = next(item for item in expansion["candidates"] if item["relativePath"].endswith("surface.mtl"))
    assert candidate["minimumHops"] == 1
    assert candidate["anchorPaths"][0]["steps"][0]["relation"] == "uses-texture"


def test_unity_guid_sidecars_expand_texture_to_material_and_shader(tmp_path):
    image = png_bytes(gradient_rgba(16, 16), 16, 16)
    texture_guid, shader_guid = "a" * 32, "b" * 32
    catalog, root = catalog_at(tmp_path, {
        "Assets/Textures/a.png": image,
        "Assets/Textures/a.png.meta": "fileFormatVersion: 2\nguid: %s\n" % texture_guid,
        "Assets/Materials/surface.mat": "%%YAML 1.1\nm_Texture: {fileID: 2800000, guid: %s, type: 3}\nm_Shader: {fileID: 4800000, guid: %s, type: 3}\n" % (texture_guid, shader_guid),
        "Assets/Shaders/lit.shader": 'Shader "Example/Lit" { SubShader { Pass {} } }',
        "Assets/Shaders/lit.shader.meta": "fileFormatVersion: 2\nguid: %s\n" % shader_guid,
    })
    captured = fingerprint(gradient_rgba(16, 16), 16, 16)
    matched = {"resourceId": 7, "name": "Texture2D 7", "available": True,
               "thumbnail": captured["thumbnail"], "meanColor": captured["meanColor"]}
    result = retrieve(catalog, {"stage": "fragment", "textureSlotCount": 18,
        "boundTextureFingerprints": [matched,
            {"resourceId": 8, "available": False, "reason": "runtime render target"},
            {"resourceId": 9, "available": False, "reason": "unsupported decoder"}]}, top_k=8)
    candidates = {item["relativePath"]: item for item in result["candidates"]}
    assert "Assets/Materials/surface.mat" in candidates
    assert "Assets/Shaders/lit.shader" in candidates
    material = candidates["Assets/Materials/surface.mat"]
    assert material["anchorEvidence"]["minimumHops"] == 2
    assert candidates["Assets/Shaders/lit.shader"]["anchorEvidence"]["minimumHops"] == 4
    assert material["physicalIdentityVerified"] is False
    assert result["policy"]["allBoundTexturesRequired"] is False
    without_unknown = retrieve(catalog, {"stage": "fragment", "textureSlotCount": 1,
        "boundTextureFingerprints": [matched]}, top_k=8)
    same = next(item for item in without_unknown["candidates"] if item["relativePath"] == material["relativePath"])
    assert same["confidence"] == material["confidence"]
    assert same["adjustedScore"] == material["adjustedScore"]


def test_unknown_and_plausible_textures_never_seed_graph_expansion(tmp_path):
    catalog, root = catalog_at(tmp_path, {"Scene/Textures/a.png": png_bytes(gradient_rgba(16, 16), 16, 16)})
    observations = anchor(catalog, "Scene/Textures/a.png")
    observations[0]["matches"][0]["verdict"] = "plausible"
    with mock.patch.object(catalog, "expand_reference_neighborhood", side_effect=AssertionError("weak evidence")):
        result = anchor_expansion(catalog, observations)
    assert result["status"] == "no-strong-indexed-anchor"
    assert not result["candidates"]


def test_reference_expansion_bounds_fanout_and_reports_truncation(tmp_path):
    files = {"Scene/Textures/a.png": png_bytes(gradient_rgba(16, 16), 16, 16)}
    files.update({"Scene/Materials/%03d.mtl" % index: "newmtl Test\nmap_Kd ../Textures/a.png\n" for index in range(48)})
    catalog, root = catalog_at(tmp_path, files)
    identity = anchor(catalog, "Scene/Textures/a.png")[0]["matches"][0]["assetId"]
    result = catalog.expand_reference_neighborhood([identity], max_assets=8)
    assert len(result["assets"]) == 8
    assert result["truncated"] is True
    with pytest.raises(AssetCatalogError):
        catalog.expand_reference_neighborhood([identity], max_assets=1000)


def test_screen_space_route_reports_code_binding_topology_without_inventing_material(tmp_path):
    catalog, root = catalog_at(tmp_path, {"Game/Readme.txt": "No decoded shaders"})
    result = retrieve(catalog, {"stage": "fragment", "textureSlotCount": 9,
        "constantBlockSizes": [64, 48], "drawGeometry": {"available": True, "vertexCount": 3, "topology": "TriangleList"},
        "codeHints": {"textureSampleCount": 31, "usesDerivatives": False}})
    route = result["signals"]["retrievalRoute"]
    assert route["route"] == "screen-space-code-bindings"
    assert route["primaryEvidence"] == ["shader-code", "resource-bindings", "draw-topology"]
    assert route["materialIdentityRequired"] is False
    assert route["shaderContentIdentityStatus"] == "not-compared"
    assert not result["candidates"]
    assert not _consistency({"textures": ["one.png"]}, {"codeHints": {"textureSampleCount": 31}})


def test_public_retrieval_hydrates_only_anchor_adjacent_unread_material_once(tmp_path):
    root = tmp_path / "assets"
    image = png_bytes(gradient_rgba(16, 16), 16, 16)
    write(root, "Content/Hat/pixels.png", image)
    write(root, "Content/Hat/owner.mtl", "newmtl Owner\nmap_Kd pixels.png\n")
    write(root, "Content/Rock/unrelated.mtl", "newmtl Other\n")
    catalog = AssetCatalogService(tmp_path / "catalog.sqlite3")
    catalog.scan(root, build_id="test", depth="names")
    catalog.hydrate(root, ["Content/Hat/pixels.png"])
    captured = fingerprint(gradient_rgba(16, 16), 16, 16)
    identity = {"rawSha256": "a" * 64, "stage": "fragment", "entryPoint": "MainPS"}
    features = {"structuralKey": "b" * 64, "stage": "fragment", "boundTextureFingerprints": [
        {"resourceId": 7, "available": True, "thumbnail": captured["thumbnail"], "meanColor": captured["meanColor"]}]}
    store = mock.Mock()
    store.lookup.return_value = {"resolved": False, "sameShapeSuggestions": []}
    with mock.patch.object(server, "_ensure_shader_asset_catalog", return_value=(
            mock.Mock(), {}, root, catalog.database_path, {"partial": False})), \
            mock.patch.object(server, "_shader_asset_capture_features", return_value=(identity, features)), \
            mock.patch.object(server, "AssetCatalogService", return_value=catalog), \
            mock.patch.object(server, "_shader_asset_game_key", return_value="test"), \
            mock.patch.object(server.shader_asset_retrieval, "ShaderAssetResolutionStore", return_value=store), \
            mock.patch.object(catalog, "hydrate", wraps=catalog.hydrate) as hydrate, \
            mock.patch.object(catalog, "scan", side_effect=AssertionError("no rescan")):
        first = json.loads(server.retrieve_shader_asset_candidates(7))
        assert first["state"] == "candidates-ready", first
        assert hydrate.call_args.args[1] == ["Content/Hat/owner.mtl"]
        candidate = next(item for item in first["retrieval"]["candidates"] if item["relativePath"] == "Content/Hat/owner.mtl")
        assert candidate["anchorEvidence"]["minimumHops"] == 1
        second = json.loads(server.retrieve_shader_asset_candidates(7))
        assert second["state"] == "candidates-ready"
        assert hydrate.call_count == 1
    assert catalog.query(relative_paths=["Content/Rock/unrelated.mtl"], limit=1)["assets"][0]["parseStatus"] == "deferred"


def test_unreal_texture_owner_expands_through_two_material_instance_generations(tmp_path):
    from tests.test_shader_asset_retrieval import NAMES, build_package, crow_material, crow_instance, uexp_blob
    from tests.test_texture_identity import texture_package, texture_uexp
    names = NAMES + ["MI_Child", "/Game/Art/Materials/MI_CrowFeather"]
    child = build_package(names, [
        ("/Script/CoreUObject", "Package", 0, "/Script/Engine"),
        ("/Script/CoreUObject", "Class", -1, "MaterialInstanceConstant"),
        ("/Script/CoreUObject", "Package", 0, "/Game/Art/Materials/MI_CrowFeather"),
        ("/Script/Engine", "MaterialInstanceConstant", -3, "MI_CrowFeather"),
    ], "MI_Child", -2)
    pixels = gradient_rgba(16, 16)
    catalog, root = catalog_at(tmp_path, {
        "Game/Game.uproject": "{}",
        "Game/Content/Art/Materials/M_CrowFeather.uasset": crow_material(),
        "Game/Content/Art/Materials/M_CrowFeather.uexp": uexp_blob(["FDepthOnlyPS"]),
        "Game/Content/Art/Materials/MI_CrowFeather.uasset": crow_instance(),
        "Game/Content/Art/Materials/MI_Child.uasset": child,
        "Game/Content/Art/Textures/T_Crow_Body_D.uasset": texture_package("T_Crow_Body_D"),
        "Game/Content/Art/Textures/T_Crow_Body_D.uexp": texture_uexp(pixels, 16, 16),
    })
    captured = fingerprint(pixels, 16, 16)
    result = retrieve(catalog, {"stage": "fragment", "boundTextureFingerprints": [
        {"resourceId": 7, "available": True, "thumbnail": captured["thumbnail"], "meanColor": captured["meanColor"]}]}, top_k=8)
    child = next(item for item in result["candidates"] if item["relativePath"].endswith("MI_Child.uasset"))
    path = child["anchorEvidence"]["anchorPaths"][0]
    assert [step["relation"] for step in path["steps"]] == ["uses-texture", "parent-material", "parent-material"]
    assert child["physicalIdentityVerified"] is False
