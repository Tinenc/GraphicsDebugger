"""Persist the public resolution shape, then consume it through public naming."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src import server
from src.shader_workspace.organization import ShaderOrganizationManager
from src.shader_workspace.publication_receipts import CompletedShaderReviews


ASSET = "Game/Content/MI_Surface.uasset"
MATERIAL = {"textures": {"SurfaceTexture": "/Game/T_Surface"}}
LINKS = {"textures": {"t7, space1": "/Game/T_Surface"}}


@pytest.fixture
def naming_runtime():
    def call(method, params, **unused):
        if method == "get_bound_textures":
            return [{"slot": 99, "bind": 7, "space": 1, "resourceId": 11}]
        assert method == "get_constant_buffer_values"
        return {"constantBuffers": []}

    with mock.patch.object(server, "ipc") as ipc, \
            mock.patch.object(server, "_read_material_asset", new_callable=mock.AsyncMock, return_value={
                "succeeded": True, "source": "uex_preview_asset", "document": MATERIAL}) as preview, \
            mock.patch.object(server, "_workspace_declarations", return_value={"available": False}):
        ipc.call.side_effect = call
        yield ipc, preview


def propose(workspace, **kwargs):
    return json.loads(asyncio.run(server.propose_shader_names_from_asset(
        42, workspace_path=str(workspace), **kwargs)))


def test_submit_writer_to_naming_reader_preserves_path_and_texture_binding(tmp_path, naming_runtime):
    # Exercise the evidence chain: the Host persists the exact material read and
    # texture link as reusable context, but that old task receipt is not workflow
    # authority. The Agent explicitly confirms the current content match and the
    # proposal re-binds it to the current resource/bind/space tuple.
    server._write_workspace_asset_resolution(tmp_path, {"resolvedAssets": [ASSET]})
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    database_path = tmp_path / "catalog.sqlite3"
    source = tmp_path / "source" / "shader.hlsl"
    source.parent.mkdir()
    source.write_text("Texture2D _32 : register(t7, space1);\n", encoding="utf-8")
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
    asset = {"assetId": "asset-id", "relativePath": ASSET, "contentSha256": "b" * 64,
             "metadata": {"className": "MaterialInstanceConstant"}}
    binding = {
        "assetRoot": str(asset_root.resolve()),
        "catalogSha256": "c" * 64,
        "assetRootManifestSha256": "d" * 64,
        "assetRootFileCount": 1,
        "assetRootByteSize": 128,
        "assetVersionSha256": "e" * 64,
        "assetContentSha256": "b" * 64,
        "assetEvidenceSha256": "f" * 64,
        "evidenceAssets": [{
            "assetId": "asset-id", "relativePath": ASSET,
            "contentSha256": "b" * 64,
        }],
    }

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
                    "present": True, "path": str(source), "format": "hlsl",
                    "currentSha256": source_sha, "lane": "executable-editable",
                    "compileEligible": True,
                },
                "representation": {
                    "lane": "executable-editable", "compileEligible": True,
                },
            }

    report_store = mock.Mock(enabled=True, task_id="roundtrip-task")
    server._ASSET_CONTENT_RECEIPT_TASKS.clear()
    with mock.patch.object(server, "_active_asset_catalog_context", return_value=(
            None, {"scope": {"gameId": "g", "buildId": "b"}},
            asset_root, database_path)), \
            mock.patch.object(server, "_shader_asset_capture_features", return_value=(
                identity, features)), \
            mock.patch.object(server, "_shader_asset_game_key", return_value="test-game"), \
            mock.patch.object(server, "_asset_resolution_evidence_binding", return_value=binding), \
            mock.patch.object(server, "_asset_evidence_binding_is_current", return_value=(True, "")), \
            mock.patch.object(server, "current_task_report_store", return_value=report_store), \
            mock.patch.object(server, "AssetCatalogService") as catalog, \
            mock.patch.object(server.shader_asset_retrieval, "_material_document", return_value={}):
        catalog.return_value.query.return_value = {"assets": [asset], "catalogSha256": "c" * 64}
        proof = server._seal_asset_content_proof({
            "assetId": "asset-id", "relativePath": ASSET,
            "tool": "registered-reader", "receiptSha256": "3" * 64,
            "assetBinding": binding,
        })
        report_store.require_shader_asset_positive_resolution.return_value = {
            "candidateAssetId": "asset-id",
            "assetContentProof": proof,
            "assetBinding": binding,
            "textureIdentityLinks": [{
                "resourceId": 11, "bind": 7, "space": 1,
                "asset": "/Game/T_Surface",
                "evidenceKind": "resolved-reference-chain",
            }],
        }
        receipt = json.loads(server.submit_shader_asset_resolution(
            42, "fragment", "asset-id", 0.9, "Matched captured texture evidence",
            workspace_path=str(tmp_path), links_json=json.dumps(LINKS)))
        assert "error" not in receipt, receipt
        persisted = server._read_workspace_asset_resolution(tmp_path)
        assert persisted["shaderRawSha256"] == "a" * 64
        assert persisted["stage"] == "fragment"
        assert persisted["eventId"] == 42
        assert len(persisted["resolvedAssets"]) == 1
        assert persisted["resolvedAssets"][0]["assetRelativePath"] == ASSET
        assert persisted["resolvedAssets"][0]["links"] == LINKS

        # A second Host read proves the exact material document consumed by the
        # naming pass.  Reusing a candidate list or caller material_json would
        # not satisfy this proof.
        naming_runtime[1].return_value = {
            "succeeded": True, "source": "registered-reader",
            "document": MATERIAL, "assetContentProofs": [proof],
        }
        manager = ShaderOrganizationManager(
            lambda: WorkspaceService(), lambda *unused: {},
            completed_reviews=CompletedShaderReviews(tmp_path / "reviews.sqlite3"),
        )
        declarations = {
            "available": True,
            "cbufferLanes": {},
            "textureSlots": {(1, 7): "resource:_32"},
            "declaredLaneCount": 0,
            "declaredSlotCount": 1,
        }
        with mock.patch.object(server, "_workspace_declarations", return_value=declarations), \
                mock.patch.object(server, "_shader_organization_manager", return_value=manager):
            without_agent_match = propose(tmp_path)
            result = propose(tmp_path, anchors_json=json.dumps([{
                "fixes": "texture-slot", "slot": 0,
                "parameter": "SurfaceTexture", "contentMatch": True,
                "asset": "/Game/T_Surface",
                "evidenceKind": "resolved-reference-chain",
                "resourceId": 11, "bindPoint": 7, "space": 1,
                "why": "the Agent selected the material's exact texture reference",
            }]))
        assert without_agent_match.get("ready") is False, without_agent_match
        assert without_agent_match["resourceAliasRows"] == []
        assert result.get("ready") is True, result
        assert naming_runtime[1].await_count == 2
        naming_runtime[1].assert_awaited_with(
            ASSET, reader_operation="", reader_arguments=None)
        assert result["material"]["source"] == "registered-reader"
        assert result["textureSlots"]["matchedCount"] == 1
        texture = result["textureSlots"]["proposals"][0]
        assert (texture["parameter"], texture["bindPoint"], texture["space"]) == (
            "SurfaceTexture", 7, 1)
        assert result["renames"] == {"resource:_32": "SurfaceTexture"}
        assert result["resourceAliasRows"] == [{
            "selector": "resource:_32",
            "resourceId": 11,
            "bindNumber": 7,
            "registerSpace": 1,
            "alias": "SurfaceTexture",
            "assetPath": "/Game/T_Surface",
            "hostEvidenceKind": "resolved-reference-chain",
        }]
        assert "resourceAliasAuthorization" not in result
        assert result["semanticNameAuthority"] == "agent"
        assert result["hostWorkflowAuthorizationRequired"] is False
        assert "assetRenameAuthorization" not in result
        assert result["applied"] is False


@pytest.mark.parametrize("entry", [ASSET, {"assetRelativePath": ASSET},
                                     {"assetPath": ASSET}, {"relativePath": ASSET}])
def test_single_record_path_compatibility(tmp_path, naming_runtime, entry):
    server._write_workspace_asset_resolution(tmp_path, {"resolvedAssets": [entry]})
    result = propose(tmp_path)
    assert "error" not in result, result
    assert result["ready"] is False
    assert result["matchStatus"] == "no-automatic-proposal"
    assert result["readinessScope"] == "automatic-proposal-payload-only"
    assert result["material"]["source"] == "uex_preview_asset"
    naming_runtime[1].assert_awaited_once_with(ASSET, reader_operation="", reader_arguments=None)


def test_multiple_assets_require_selection_before_preview_or_capture_reads(tmp_path, naming_runtime):
    server._write_workspace_asset_resolution(tmp_path, {"resolvedAssets": [
        {"assetRelativePath": ASSET, "links": LINKS}, {"assetPath": "Game/M_Parent.uasset"}]})
    result = propose(tmp_path)
    assert result["ready"] is False
    assert result["reason"] == "multiple-resolved-assets"
    assert result["assetCandidates"] == [
        {"assetPath": ASSET}, {"assetPath": "Game/M_Parent.uasset"}]
    called = [item.args[0] for item in naming_runtime[0].call.call_args_list]
    assert called in ([], ["get_current_renderdoc_context"])
    assert "get_bound_textures" not in called
    assert "get_constant_buffer_values" not in called
    naming_runtime[1].assert_not_called()


def test_explicit_workspace_hint_selects_a_read_but_cannot_authorize_names(tmp_path, naming_runtime):
    server._write_workspace_asset_resolution(tmp_path, {"resolvedAssets": [
        {"assetRelativePath": "Game/M_Parent.uasset",
         "links": {"textures": {"t7, space1": "/Game/WrongTexture"}}},
        {"assetRelativePath": ASSET, "links": LINKS}]})
    result = propose(tmp_path, asset_path=ASSET)
    assert result.get("ready") is False, result
    assert result["matchStatus"] == "no-automatic-proposal"
    assert result["readinessScope"] == "automatic-proposal-payload-only"
    naming_runtime[1].assert_awaited_once_with(ASSET, reader_operation="", reader_arguments=None)
    assert result["textureSlots"]["matchedCount"] == 0
    assert result["hostWorkflowAuthorizationRequired"] is False
    assert "assetRenameAuthorization" not in result


def test_explicit_material_json_takes_priority_without_picking_a_record(tmp_path, naming_runtime):
    server._write_workspace_asset_resolution(tmp_path, {"resolvedAssets": [
        {"assetRelativePath": ASSET, "links": LINKS}, {"assetPath": "Game/M_Parent.uasset"}]})
    result = propose(tmp_path, asset_path="Game/Explicit.uasset", material_json=json.dumps(MATERIAL))
    assert "error" not in result, result
    assert result["ready"] is False
    assert result["matchStatus"] == "no-automatic-proposal"
    assert result["readinessScope"] == "automatic-proposal-payload-only"
    assert result["material"]["source"] == "material_json"
    assert result["textureSlots"]["matchedCount"] == 0
    naming_runtime[1].assert_not_called()


def test_malformed_record_is_never_stringified_into_an_asset_path(tmp_path, naming_runtime):
    server._write_workspace_asset_resolution(tmp_path, {"resolvedAssets": [{"confidence": 0.9}]})
    result = propose(tmp_path)
    assert "no material to read" in result["error"]
    called = [item.args[0] for item in naming_runtime[0].call.call_args_list]
    assert called in ([], ["get_current_renderdoc_context"])
    assert "get_bound_textures" not in called
    assert "get_constant_buffer_values" not in called
    naming_runtime[1].assert_not_called()
