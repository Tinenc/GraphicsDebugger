"""Mount-aware object references must preserve exact captured texture bindings."""

import asyncio
import hashlib
import json
import os
import struct
from pathlib import Path
from unittest import mock

import pytest

from src import server, uex_profiles
from src.asset_catalog import AssetCatalogService
from src.shader_workspace.organization import ShaderOrganizationManager
from src.shader_workspace.publication_receipts import CompletedShaderReviews
from src.symbols.engines.unreal_package import read_summary
from tests.test_texture_identity import texture_package


@pytest.fixture
def mounted_catalog(tmp_path):
    root = tmp_path / "assets"
    content = root / "export-folder" / "Project" / "Content"
    content.mkdir(parents=True)
    (content.parent / "Project.uproject").write_text("{}")
    path = content / "T_Surface.uasset"
    path.write_bytes(texture_package("T_Crow_Body_D"))
    catalog = AssetCatalogService(tmp_path / "catalog.sqlite3")
    catalog.scan(root, build_id="fixture", depth="headers")
    return root, content.parent.parent, path, catalog


def test_exact_mount_and_single_export_resolve_without_prefix_guess(mounted_catalog):
    root, mount, path, catalog = mounted_catalog
    relative = path.relative_to(root).as_posix()
    refs = ["Project/Content/T_Surface.0", "/Game/T_Surface.T_Surface", relative]
    result = catalog.resolve_unreal_texture_paths(root, refs, reader_mount_roots=[mount])
    assert result["resolved"] == dict.fromkeys(refs, relative)
    assert not catalog.resolve_unreal_texture_paths(root, refs[:1])["resolved"]


@pytest.mark.parametrize("reference", ["Project/Content/T_Surface.1", "Other/Content/T_Surface.0",
                                      "T_Surface.0", "../Project/Content/T_Surface.0",
                                      "Project/Content/T_Surface.ArbitraryExport"])
def test_incomplete_or_wrong_namespace_does_not_resolve(mounted_catalog, reference):
    root, mount, path, catalog = mounted_catalog
    assert not catalog.resolve_unreal_texture_paths(root, [reference], reader_mount_roots=[mount])["resolved"]


def test_reference_resolution_rechecks_same_size_mtime_changes(mounted_catalog):
    root, mount, path, catalog = mounted_catalog
    before = path.stat()
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = catalog.resolve_unreal_texture_paths(root, ["Project/Content/T_Surface.0"], reader_mount_roots=[mount])
    assert not result["resolved"]


def test_numeric_reference_does_not_collapse_multiple_exports_to_a_package(mounted_catalog):
    root, mount, path, catalog = mounted_catalog
    data = bytearray(path.read_bytes())
    summary = read_summary(data)
    # The fixture has one contiguous export at the end. Add a second valid
    # export and update the summary count/total/depends fields.
    old_size = len(data)
    export = data[summary["exportOffset"]:] + bytes(68)  # UE 5.4 export remainder
    data = data[:summary["exportOffset"]] + export * 2
    export_pair = struct.pack("<ii", 1, summary["exportOffset"])
    offset = data.index(export_pair)
    struct.pack_into("<i", data, offset, 2)
    for position in (28, summary["nameOffset"] - 4):
        assert struct.unpack_from("<i", data, position)[0] == old_size
        struct.pack_into("<i", data, position, len(data))
    path.write_bytes(data)
    catalog.hydrate(root, [path.relative_to(root).as_posix()])
    assert catalog.query(relative_paths=[path.relative_to(root).as_posix()])["assets"][0]["metadata"]["exportCount"] == 2
    result = catalog.resolve_unreal_texture_paths(root, ["Project/Content/T_Surface.0"], reader_mount_roots=[mount])
    assert not result["resolved"]


@pytest.mark.parametrize("identity_link", [False, True])
def test_public_naming_resolves_reader_reference_with_recorded_mount(mounted_catalog, tmp_path, identity_link):
    root, mount, path, catalog = mounted_catalog
    relative = path.relative_to(root).as_posix()
    material_path = path.with_name("MI_Surface.json")
    material_document = {"textures": {"SurfaceTexture": "Project/Content/T_Surface.0"}}
    material_path.write_text(json.dumps(material_document), encoding="utf-8")
    catalog.hydrate(root, [material_path.relative_to(root).as_posix()])
    material = catalog.query(
        relative_paths=[material_path.relative_to(root).as_posix()], limit=1,
    )["assets"][0]

    source = tmp_path / "source" / "shader.hlsl"
    source.parent.mkdir()
    source.write_text(
        "Texture2D _32 : register(t7, space1);\n"
        "Texture2D _36 : register(t9, space2);\n",
        encoding="utf-8",
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    identity = {
        "eventId": 42,
        "rawSha256": "a" * 64,
        "stage": "fragment",
        "entryPoint": "MainPS",
        "shaderResourceId": 17,
        "captureId": "2" * 64,
        "captureSessionId": "capture-session",
    }
    features = {"structuralKey": "shape", "boundTextureNames": []}
    binding = {
        "assetRoot": str(root.resolve()),
        "catalogSha256": "c" * 64,
        "assetRootManifestSha256": "d" * 64,
        "assetRootFileCount": 2,
        "assetRootByteSize": 256,
        "assetVersionSha256": "e" * 64,
        "assetContentSha256": material["contentSha256"],
        "assetEvidenceSha256": "f" * 64,
        "evidenceAssets": [{
            "assetId": material["assetId"],
            "relativePath": material["relativePath"],
            "contentSha256": material["contentSha256"],
        }],
    }
    bound = [{"bind": 7, "space": 1, "resourceId": 11, "name": "unrelated-debug-name"},
             {"bind": 9, "space": 2, "resourceId": 11, "name": "another-debug-name"}]

    def call(method, params, **unused):
        return bound if method == "get_bound_textures" else {"constantBuffers": []}

    class WorkspaceService:
        def status(self, workspace_path):
            assert Path(workspace_path).resolve() == tmp_path.resolve()
            return {
                "schema": "renderdoc-ultra-shader-workspace-status/v1",
                "workspacePath": str(tmp_path.resolve()),
                "workspaceId": "1" * 32,
                "capture": {
                    "captureId": identity["captureId"],
                    "captureSessionId": identity["captureSessionId"],
                },
                "shader": {
                    "resourceId": identity["shaderResourceId"],
                    "rawSha256": identity["rawSha256"],
                    "stage": identity["stage"],
                    "entryPoint": identity["entryPoint"],
                },
                "stage": identity["stage"],
                "entryPoint": identity["entryPoint"],
                "source": {
                    "present": True,
                    "path": str(source),
                    "format": "hlsl",
                    "currentSha256": source_sha,
                    "lane": "executable-editable",
                    "compileEligible": True,
                },
                "representation": {
                    "lane": "executable-editable",
                    "compileEligible": True,
                },
            }

    texture_links = [{
        "resourceId": 11,
        "bind": 7,
        "space": 1,
        "asset": relative,
        "evidenceKind": "resolved-reference-chain",
    }]
    if identity_link:
        texture_links.append({
            "resourceId": 11,
            "bind": 9,
            "space": 2,
            "asset": relative,
            "evidenceKind": "resolved-reference-chain",
        })
    report_store = mock.Mock(enabled=True, task_id="texture-reference-task")
    server._ASSET_CONTENT_RECEIPT_TASKS.clear()
    proof = None

    with mock.patch.object(server, "_active_asset_catalog_context", return_value=(
            None, {"scope": {"gameId": "g", "buildId": "b"}},
            root, catalog.database_path)), \
            mock.patch.object(server, "_shader_asset_capture_features", return_value=(
                identity, features)), \
            mock.patch.object(server, "_shader_asset_game_key", return_value="test-game"), \
            mock.patch.object(server, "_asset_resolution_evidence_binding", return_value=binding), \
            mock.patch.object(server, "_asset_evidence_binding_is_current", return_value=(True, "")), \
            mock.patch.object(server, "current_task_report_store", return_value=report_store):
        proof = server._seal_asset_content_proof({
            "assetId": material["assetId"],
            "relativePath": material["relativePath"],
            "tool": "registered-reader",
            "receiptSha256": "3" * 64,
            "assetBinding": binding,
        })
        report_store.require_shader_asset_positive_resolution.return_value = {
            "candidateAssetId": material["assetId"],
            "assetContentProof": proof,
            "assetBinding": binding,
            "textureReferenceLinks": texture_links,
        }
        submitted = json.loads(server.submit_shader_asset_resolution(
            42, "fragment", material["assetId"], 0.9,
            "Exact material read and texture reference chain",
            workspace_path=str(tmp_path),
            links_json=json.dumps({"textures": {"t7, space1": relative}}),
        ))
        assert "error" not in submitted, submitted

    manager = ShaderOrganizationManager(
        lambda: WorkspaceService(), lambda *unused: {},
        completed_reviews=CompletedShaderReviews(tmp_path / "reviews.sqlite3"),
    )
    declarations = {
        "available": True,
        "cbufferLanes": {},
        "textureSlots": {(1, 7): "resource:_32", (2, 9): "resource:_36"},
        "declaredLaneCount": 0,
        "declaredSlotCount": 2,
    }
    read_receipt = {
        "succeeded": True,
        "source": "registered-reader",
        "document": material_document,
        "assetContentProofs": [proof],
        "textureReferenceLinks": texture_links,
    }

    with mock.patch.object(server, "ipc") as ipc, \
            mock.patch.object(server, "_read_material_asset", new_callable=mock.AsyncMock,
                              return_value=read_receipt), \
            mock.patch.object(server, "_workspace_declarations", return_value=declarations), \
            mock.patch.object(server, "_active_asset_catalog_context", return_value=(
                None, {"scope": {"gameId": "g", "buildId": "b"}},
                root, catalog.database_path)), \
            mock.patch.object(server, "_shader_asset_capture_features", return_value=(
                identity, features)), \
            mock.patch.object(server, "_shader_asset_game_key", return_value="test-game"), \
            mock.patch.object(server, "_asset_evidence_binding_is_current", return_value=(True, "")), \
            mock.patch.object(server, "current_task_report_store", return_value=report_store), \
            mock.patch.object(server, "_shader_organization_manager", return_value=manager), \
            mock.patch.object(uex_profiles, "_load", return_value={"profiles": {
                "fixture": {"assetRoot": str(root), "paksDir": str(mount)}}}):
        ipc.call.side_effect = call
        authority = server._shader_asset_authorization_identity(
            str(tmp_path), 42, "fragment",
        )
        assert authority["current"] is True, json.dumps(authority, indent=2)
        current_proof = authority["resolvedAssets"][0]["readerProof"]["assetContentProof"]
        assert current_proof["assetId"] == proof["assetId"]
        assert current_proof["relativePath"] == proof["relativePath"]
        assert current_proof["assetBinding"] == proof["assetBinding"]
        assert current_proof["tool"] == "submit_shader_asset_resolution"
        assert current_proof["receiptSha256"] != proof["receiptSha256"]
        read_receipt["assetContentProofs"] = [current_proof]
        result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
            42, workspace_path=str(tmp_path))))
    assert "error" not in result, result
    assert result["textureSlots"]["matchedCount"] == (2 if identity_link else 1), result
    slots = {(item["bindPoint"], item["space"]) for item in result["textureSlots"]["proposals"]}
    assert slots == ({(7, 1), (9, 2)} if identity_link else {(7, 1)})
    assert result["textureSlots"]["pathResolution"]["resolved"]["Project/Content/T_Surface.0"] == relative
    assert result["semanticNameAuthority"] == "agent", json.dumps(result, indent=2)
    assert result["hostWorkflowAuthorizationRequired"] is False
    assert "assetRenameAuthorization" not in result
    assert result["applied"] is False


def test_exact_zero_distance_image_remains_best_resource_link():
    links = server._identity_links({"signals": {"textureIdentity": [{"resourceId": 17, "matches": [
        {"verdict": "same-image", "distance": 0, "relativePath": "textures/exact.uasset"},
        {"verdict": "same-image", "distance": 4, "relativePath": "textures/weaker.uasset"}]}]}})
    assert links[0]["asset"] == "textures/exact.uasset"
    assert links[0]["resourceId"] == 17
