"""Exercise material naming through the public tools and real bridge field names."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import struct
import types
import unittest
import uuid
import threading
import time
from unittest import mock

from src import server
from src.shader_asset_retrieval import ShaderAssetResolutionStore
from src.shader_workspace import aliases
from src.shader_workspace import cbuffer_alias_publication as alias_publication
from src.shader_workspace.organization import ShaderOrganizationManager


class MaterialNamingMCPTests(unittest.TestCase):
    def test_nonfinite_lanes_preserve_offsets_in_naming_and_variance(self):
        def call(method, params, **unused):
            if method == "get_bound_textures":
                return []
            return {"constantBuffers": [{"bind": 4, "space": 2,
                    "floats": [None, 0.8231471, None, float(params["event_id"])],
                    "uints": [0x7fc00000, 0, 0x7f800000, 0]}]}

        with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)):
            result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                10, material_json=json.dumps({"scalars": {"SurfaceValue": 0.8231471}}))))
            variance = json.loads(asyncio.run(server.profile_constant_buffer_variance("11,12")))
        self.assertNotIn("error", result, result)
        proposals = result["constantBufferMembers"]["proposals"]
        self.assertEqual([(item["bind"], item["space"], item["lane"]) for item in proposals], [(4, 2, 1)])
        self.assertEqual(result["laneNarrowing"]["blocks"]["b4:s2"]["lanes"], [1, 3])
        block = variance["blocks"]["b4:s2"]
        self.assertEqual(block["unavailableLanes"], [0, 2])
        self.assertEqual(block["constantLanes"], [1])
        self.assertEqual(block["varyingLanes"], [3])
        json.dumps(result, allow_nan=False)
        json.dumps(variance, allow_nan=False)

    def test_variance_uses_the_bound_host_client_and_keeps_blocks_separate(self):
        def call(method, params, **unused):
            self.assertEqual(method, "get_constant_buffer_values")
            self.assertEqual(params["stage"], "fragment")
            return {"constantBuffers": [
                {"bind": 0, "space": 0, "floats": [float(params["event_id"])]},
                {"bind": 4, "space": 2, "floats": [0.8231471]},
            ]}

        with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)):
            result = json.loads(asyncio.run(server.profile_constant_buffer_variance("11,12", "pixel")))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["blocks"]["b0:s0"]["varyingLanes"], [0])
        self.assertEqual(result["blocks"]["b4:s2"]["constantLanes"], [0])

    def test_proposals_use_each_blocks_variance_and_bits(self):
        def call(method, params, **unused):
            if method == "get_bound_textures":
                return []
            self.assertEqual(method, "get_constant_buffer_values")
            return {"constantBuffers": [
                {"bind": 0, "space": 0,
                 "floats": [0.2938471 if params["event_id"] == 12 else 0.8231471, 1e-40]},
                {"bind": 4, "space": 2, "floats": [0.0, 0.8231471]},
            ]}

        with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)):
            result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                10, material_json=json.dumps({"scalars": {"SurfaceValue": 0.8231471}}),
                same_material_event_ids="11,12", fill_gaps=False)))
        self.assertNotIn("error", result, result)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["cbufferAliasRows"], [])
        self.assertEqual(result["withheldCBufferAliasRows"], [{
            "bind": 4,
            "space": 2,
            "row": 0,
            "byteOffset": 0,
            "alias": "SurfaceValue",
            "lanes": [1],
            "parameters": ["SurfaceValue"],
            "provisional": False,
            "readByThisShader": False,
        }])
        self.assertEqual(result["laneNarrowing"]["blocks"]["b0:s0"]["lanes"], [])
        self.assertEqual(result["laneNarrowing"]["blocks"]["b4:s2"]["lanes"], [0, 1])
        proposals = result["constantBufferMembers"]["proposals"]
        self.assertEqual([(item["bind"], item["space"], item["lane"]) for item in proposals], [(4, 2, 1)])
        self.assertEqual(proposals[0]["parameter"], "SurfaceValue")

    def test_workspace_mirror_pixel_links_cannot_authorize_texture_renames(self):
        bound = [
            {"slot": 3, "bind": 7, "space": 1, "resourceId": 11},
            {"slot": 8, "bind": 7, "space": 0, "resourceId": 22},
        ]

        def call(method, params, **unused):
            if method == "get_bound_textures":
                return bound
            self.assertEqual(method, "get_constant_buffer_values")
            return {"constantBuffers": []}

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shader.hlsl"
            source.write_text(
                "Texture2D sourceA : register(t7, space1);\n"
                "Texture2D sourceB : register(t7);\n", encoding="utf-8")
            workspace = types.SimpleNamespace(status=lambda unused: {"source": {"path": str(source)}})
            with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)), \
                    mock.patch.object(server, "_shader_workspace_service", return_value=workspace), \
                    mock.patch.object(server, "_read_workspace_asset_resolution", return_value={
                        "identityLinks": [{"resourceId": 22, "asset": "/Game/TB", "why": "same pixels"}]}):
                result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                    10, workspace_path=temporary, material_json=json.dumps({
                        "textures": {"SurfaceA": "/Game/TA", "SurfaceB": "/Game/TB"}}))))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["renames"], {})
        self.assertEqual(result["textureSlots"]["proposals"], [])

    def test_explicit_agent_texture_content_match_produces_publishable_rows(self):
        bound = [{"slot": 0, "bind": 4, "space": 1, "resourceId": 77}]

        def call(method, params, **unused):
            return bound if method == "get_bound_textures" else {"constantBuffers": []}

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shader.hlsl"
            source.write_text("Texture2D _32 : register(t4, space1);\n", encoding="utf-8")
            workspace = types.SimpleNamespace(status=lambda unused: {"source": {"path": str(source)}})
            with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)), \
                    mock.patch.object(server, "_shader_workspace_service", return_value=workspace):
                result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                    10, workspace_path=temporary,
                    material_json=json.dumps({"textures": {"BaseMap": "/Game/T_Base"}}),
                    anchors_json=json.dumps([{
                        "fixes": "texture-slot", "slot": 0, "parameter": "BaseMap",
                        "contentMatch": True,
                        "asset": "/Game/T_Base",
                        "evidenceKind": "pixel-same-image",
                        "resourceId": 77, "bindPoint": 4, "space": 1,
                        "why": "decoded pixels match the unpacked texture",
                    }]))))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["renames"], {"resource:_32": "BaseMap"})
        self.assertFalse(result["hostWorkflowAuthorizationRequired"])
        self.assertNotIn("assetRenameAuthorization", result)
        self.assertEqual(result["textureSlots"]["autoApplyCount"], 1)
        self.assertEqual(result["withheldRenames"], [])
        self.assertEqual(result["resourceAliasRows"], [{
            "selector": "resource:_32",
            "resourceId": 77,
            "bindNumber": 4,
            "registerSpace": 1,
            "alias": "BaseMap",
            "assetPath": "/Game/T_Base",
            "hostEvidenceKind": "pixel-same-image",
        }])

    def test_exact_reader_proof_does_not_replace_agent_content_match(self):
        bound = [{"slot": 0, "bind": 4, "space": 1, "resourceId": 77}]

        def call(method, params, **unused):
            return bound if method == "get_bound_textures" else {"constantBuffers": []}

        binding = {
            "assetContentSha256": "1" * 64,
            "assetEvidenceSha256": "2" * 64,
            "evidenceAssets": [{
                "assetId": "asset-a", "relativePath": "Game/Materials/M_A.uasset",
                "contentSha256": "1" * 64,
            }],
        }
        server._ASSET_CONTENT_RECEIPT_TASKS.clear()
        proof = server._seal_asset_content_proof({
            "assetId": "asset-a", "relativePath": "Game/Materials/M_A.uasset",
            "assetBinding": binding, "receiptSha256": "3" * 64,
        })
        resolution = {
            "resolutionId": 9,
            "assetRelativePath": "Game/Materials/M_A.uasset",
            "assetContentSha256": "1" * 64,
            "assetBinding": binding,
            "readerProof": {
                "textureIdentityLinks": [{
                    "resourceId": 77, "bind": 4, "space": 1,
                    "asset": "/Game/T_Base", "evidenceKind": "pixel-same-image",
                }],
            },
        }
        receipt = {
            "succeeded": True, "source": "registered-reader",
            "document": {"textures": {"BaseMap": "/Game/T_Base"}},
            "assetContentProofs": [proof],
        }
        manager = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shader.hlsl"
            source.write_text("Texture2D _32 : register(t4, space1);\n", encoding="utf-8")
            workspace = types.SimpleNamespace(status=lambda unused: {"source": {"path": str(source)}})
            with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)), \
                    mock.patch.object(server, "_shader_workspace_service", return_value=workspace), \
                    mock.patch.object(server, "_shader_asset_authorization_identity",
                                      return_value={"current": True, "resolvedAssets": [resolution]}), \
                    mock.patch.object(server, "_read_material_asset", new=mock.AsyncMock(return_value=receipt)), \
                    mock.patch.object(server, "_shader_organization_manager", return_value=manager):
                result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                    10, workspace_path=temporary,
                    asset_path="Game/Materials/M_A.uasset")))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["renames"], {})
        self.assertEqual(result["resourceAliasRows"], [])
        self.assertEqual(result["textureSlots"]["autoApplyCount"], 0)
        self.assertEqual(result["withheldRenames"][0]["workspaceSymbol"], "resource:_32")
        self.assertFalse(result["hostWorkflowAuthorizationRequired"])
        self.assertEqual(result["semanticNameAuthority"], "agent")
        manager.issue_asset_rename_authorization.assert_not_called()

    def test_reader_of_different_material_does_not_authorize_a_texture_name(self):
        bound = [{"slot": 0, "bind": 4, "space": 0, "resourceId": 77}]

        def call(method, params, **unused):
            return bound if method == "get_bound_textures" else {"constantBuffers": []}

        binding_a = {
            "assetContentSha256": "1" * 64,
            "assetEvidenceSha256": "2" * 64,
            "evidenceAssets": [{
                "assetId": "asset-a", "relativePath": "Game/M_A.uasset",
                "contentSha256": "1" * 64,
            }],
        }
        resolution = {
            "resolutionId": 1, "assetRelativePath": "Game/M_A.uasset",
            "assetContentSha256": "1" * 64, "assetBinding": binding_a,
            "readerProof": {"textureIdentityLinks": [{
                "resourceId": 77, "asset": "/Game/T_Base",
                "evidenceKind": "resolved-reference-chain",
            }]},
        }
        proof_b = {
            "assetId": "asset-b", "relativePath": "Game/M_B.uasset",
            "assetBinding": {
                "assetContentSha256": "4" * 64,
                "assetEvidenceSha256": "5" * 64,
            },
            "receiptSha256": "6" * 64,
        }
        receipt = {
            "succeeded": True, "source": "registered-reader",
            "document": {"textures": {"BaseMap": "/Game/T_Base"}},
            "assetContentProofs": [proof_b],
        }
        manager = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shader.hlsl"
            source.write_text("Texture2D _32 : register(t4);\n", encoding="utf-8")
            workspace = types.SimpleNamespace(status=lambda unused: {"source": {"path": str(source)}})
            with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)), \
                    mock.patch.object(server, "_shader_workspace_service", return_value=workspace), \
                    mock.patch.object(server, "_shader_asset_authorization_identity",
                                      return_value={"current": True, "resolvedAssets": [resolution]}), \
                    mock.patch.object(server, "_read_material_asset", new=mock.AsyncMock(return_value=receipt)), \
                    mock.patch.object(server, "_shader_organization_manager", return_value=manager):
                result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                    10, workspace_path=temporary, asset_path="Game/M_A.uasset")))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["renames"], {})
        self.assertEqual(result["resourceAliasRows"], [])
        self.assertEqual(result["textureSlots"]["autoApplyCount"], 0)
        self.assertFalse(result["hostWorkflowAuthorizationRequired"])
        self.assertNotIn("assetRenameAuthorization", result)
        manager.issue_asset_rename_authorization.assert_not_called()

    def test_capture_errors_are_reported_instead_of_ready_empty_names(self):
        for failed_method in ("get_constant_buffer_values", "get_bound_textures"):
            with self.subTest(failed_method=failed_method):
                def call(method, params, **unused):
                    if method == failed_method:
                        return {"error": "captured Shader reflection is unavailable"}
                    return {"constantBuffers": []}

                with mock.patch.object(server, "ipc", types.SimpleNamespace(call=call)):
                    result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
                        10, material_json=json.dumps({"scalars": {"SurfaceValue": 0.8231471}}))))
                self.assertIn("reflection is unavailable", result["error"])
                self.assertNotIn("ready", result)


class CBufferAliasPublicationMCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        store = mock.patch.object(aliases, "project_store_root", return_value=Path(self.temporary.name))
        store.start()
        self.addCleanup(store.stop)
        workspace = mock.patch.object(
            server, "_workspace_root_for_files", return_value=self.workspace
        )
        workspace.start()
        self.addCleanup(workspace.stop)
        lightweight_workspace = mock.patch.object(
            server,
            "_alias_publication_workspace_status",
            side_effect=lambda value: {
                "workspacePath": str(
                    self.workspace
                    if value == "test-workspace"
                    else Path(value).resolve()
                ),
                "workspaceId": "workspace-a",
            },
        )
        lightweight_workspace.start()
        self.addCleanup(lightweight_workspace.stop)
        self.block = {
            "block": "Material", "bind": 4, "space": 0,
            "members": [{"name": "parameters", "offset": 0}],
            "leaves": [{"path": "parameters[0]", "offset": 0, "byteSize": 16},
                       {"path": "parameters[1]", "offset": 16, "byteSize": 16}],
        }

        self.identity = {
            "captureId": "capture-a",
            "captureSessionId": "session-a",
            "eventId": 11834,
            "stage": "fragment",
            "entryPoint": "MainPS",
            "shaderResourceId": 77,
            "rawSha256": "a" * 64,
            "blocks": [self.block],
        }
        identity = mock.patch.object(
            server,
            "_shader_organization_cbuffer_alias_identity",
            side_effect=lambda unused, **kwargs: self.identity,
        )
        identity.start()
        self.addCleanup(identity.stop)
        service = mock.Mock(
            side_effect=AssertionError(
                "CBuffer alias publication loaded ShaderWorkspaceService"
            )
        )
        workspace_service = mock.patch.object(
            server, "_shader_workspace_service", service
        )
        workspace_service.start()
        self.addCleanup(workspace_service.stop)
        toolchain = mock.Mock(
            side_effect=AssertionError(
                "CBuffer alias publication loaded the Shader tool registry"
            )
        )
        toolchain_catalog = mock.patch.object(
            server, "_shader_toolchain_catalog", toolchain
        )
        toolchain_catalog.start()
        self.addCleanup(toolchain_catalog.stop)

    def call_publish(
        self,
        rows,
        *,
        block_alias="",
        blocks=None,
        operation_id=None,
        compiled_layout_receipt=None,
        legacy_authorization=None,
        naming_evidence=None,
        semantic_fields=None,
        semantic_source_sha256="",
        binding_reviews=None,
    ):
        if blocks is not None:
            self.identity["blocks"] = blocks
        arguments = {
            "block_alias": block_alias,
            "operation_id": operation_id or uuid.uuid4().hex,
        }
        if compiled_layout_receipt is not None:
            arguments["compiled_layout_receipt"] = compiled_layout_receipt
        if legacy_authorization is not None:
            arguments["asset_alias_authorization"] = legacy_authorization
        if naming_evidence is not None:
            arguments["naming_evidence"] = naming_evidence
        if semantic_fields is not None:
            arguments["semantic_fields_json"] = json.dumps(semantic_fields)
        if binding_reviews is not None:
            arguments["binding_reviews_json"] = json.dumps(binding_reviews)
        if semantic_source_sha256:
            arguments["semantic_source_sha256"] = semantic_source_sha256
        return json.loads(asyncio.run(server.publish_cbuffer_aliases(
            "test-workspace", json.dumps(rows), **arguments
        )))

    def publish(self, rows, **kwargs):
        if kwargs.get("compiled_layout_receipt") is None:
            kwargs["compiled_layout_receipt"] = self.compiled_receipt(rows)
        result = self.call_publish(rows, **kwargs)
        self.assertNotIn("error", result, result)
        return result

    def test_semantic_only_publication_binds_current_physical_lanes_without_compile(self):
        fields = [{"bind": 4, "space": 0, "name": "baseColor", "type": "float3",
                   "components": [{"offset": 0}, {"offset": 4}, {"offset": 8}],
                   "status": "inferred", "provenance": "shader", "evidence": "shader.hlsl:123"},
                  {"bind": 4, "space": 0, "name": "roughness", "type": "float",
                   "components": [{"offset": 12}], "status": "confirmed",
                   "provenance": "asset", "evidence": "Material parameter vector alpha"}]
        result = self.call_publish([], semantic_fields=fields)
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["counts"]["writtenSemanticFields"], 2)
        self.assertEqual(result["counts"]["writtenRows"], 0)
        view = result["published"][0]["semanticView"]
        self.assertEqual(view["shaderScope"]["shaderRawSha256"], "a" * 64)
        self.assertEqual(view["fields"][1]["name"], "roughness")

    def test_unpublished_binding_is_reported_even_when_requested_fields_all_succeed(self):
        self.identity["blocks"].append({"block": "View", "bind": 1, "space": 0,
            "members": [{"name": "viewScale", "offset": 0}],
            "leaves": [{"path": "viewScale", "offset": 0, "byteSize": 4}]})
        result = self.call_publish([], semantic_fields=[{
            "bind": 4, "space": 0, "name": "baseColor", "type": "float4",
            "components": [{"offset": n} for n in (0, 4, 8, 12)],
            "status": "inferred", "provenance": "shader", "evidence": "shader.hlsl:123"}],
            binding_reviews=[{"bind": 4, "unresolvedOffsets": [16, 20, 24, 28],
                              "evidence": "Unconsumed trailing declaration, keep physical labels"}])
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["bindingCoverage"]["pendingBindings"],
                         [{"bind": 1, "space": 0, "missingLaneCount": 1}])

    def test_reviewed_unknown_lanes_need_no_invented_alias_or_file_write(self):
        result = self.call_publish([], binding_reviews=[{
            "bind": 4, "unresolvedOffsets": list(range(0, 32, 4)),
            "evidence": "Only indirect indexing is known; candidate material layout has no matching parameter map"}])
        self.assertNotIn("error", result, result)
        self.assertTrue(result["bindingCoverage"]["complete"])
        self.assertEqual(result["bindingCoverage"]["unresolvedLaneCount"], 8)
        self.assertFalse(result["bindingCoverage"]["namingComplete"])
        self.assertEqual(result["published"], [])
        self.assertEqual(result["counts"]["writtenSemanticFields"], 0)

    def test_unused_reflected_storage_is_accounted_without_claiming_it_was_named(self):
        result = self.call_publish([], binding_reviews=[{
            "bind": 4, "unresolvedOffsets": [], "unusedOffsets": list(range(0, 32, 4)),
            "evidence": "Current source declares this block but never reads it.",
            "unusedEvidence": "All helpers, branches and array accesses reviewed; no use of this binding."}])
        self.assertNotIn("error", result, result)
        self.assertTrue(result["bindingCoverage"]["complete"])
        self.assertEqual(result["bindingCoverage"]["namedLaneCount"], 0)
        self.assertEqual(result["bindingCoverage"]["reviewedUnusedLaneCount"], 8)
        self.assertEqual(result["bindingCoverage"]["unresolvedLaneCount"], 0)
        self.assertEqual(result["published"], [])

    def test_semantic_publication_rejects_stale_source_before_writing(self):
        result = self.call_publish([], semantic_source_sha256="c" * 64, semantic_fields=[{
            "bind": 4, "name": "roughness", "type": "float", "components": [{"offset": 0}],
            "status": "inferred", "provenance": "shader", "evidence": "shader.hlsl:123"}])
        self.assertIn("does not match", str(result))

    def test_semantic_source_provenance_is_verified_once_and_durable_replay_survives_later_edit(self):
        source = self.workspace / "source.hlsl"
        source.write_text("float roughness;", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        status = {"workspacePath": str(self.workspace), "workspaceId": "workspace-a",
                  "source": {"path": "source.hlsl"}}
        arguments = dict(semantic_source_sha256=digest, operation_id="semantic-source-replay-001", semantic_fields=[{
            "bind": 4, "name": "roughness", "type": "float", "components": [{"offset": 0}],
            "status": "inferred", "provenance": "shader", "evidence": "source.hlsl:1"}])
        with mock.patch.object(server, "_alias_publication_workspace_status", return_value=status):
            result = self.call_publish([], **arguments)
            self.assertNotIn("error", result, result)
            self.assertEqual(result["published"][0]["semanticView"]["sourceSha256"], digest)
            source.write_text("float updatedRoughness;", encoding="utf-8")
            replay = self.call_publish([], **arguments)
            self.assertNotIn("error", replay, replay)
            self.assertEqual(replay["published"], result["published"])

    def test_semantic_view_mirrors_replacement_layout_with_compiled_shader_scope(self):
        self.identity["activeDisplayShaderRawSha256"] = "b" * 64
        self.identity["activeDisplayBlocks"] = [{
            "block": "CaptureConstantsB4", "bind": 4, "space": 0, "byteSize": 32,
            "members": [{"name": "cb4_c0_x", "offset": 0}, {"name": "cb4_c0_y", "offset": 4}],
            "leaves": [{"path": "cb4_c0_x", "offset": 0, "byteSize": 4, "baseType": "float"},
                       {"path": "cb4_c0_y", "offset": 4, "byteSize": 4, "baseType": "float"}]}]
        result = self.call_publish([], semantic_fields=[{
            "bind": 4, "name": "detailScale", "type": "float2",
            "components": [{"offset": 0}, {"offset": 4}],
            "status": "inferred", "provenance": "shader", "evidence": "source:123"}, {
            "bind": 4, "name": "unusedValue", "type": "float", "components": [{"offset": 16}],
            "status": "inferred", "provenance": "asset", "evidence": "material.json:12"}])
        self.assertNotIn("error", result, result)
        self.assertEqual(result["counts"]["writtenSemanticFields"], 2)
        self.assertTrue(result["partial"])
        self.assertEqual(result["semanticMirrorSkipped"][0]["name"], "unusedValue")
        mirror = result["semanticMirrors"][0]
        self.assertEqual(mirror["semanticFieldsWritten"], 1)
        self.assertEqual(mirror["semanticView"]["shaderScope"]["shaderRawSha256"], "b" * 64)
        self.assertNotEqual(mirror["layoutSha256"], result["published"][0]["layoutSha256"])
        self.assertTrue(Path(mirror["path"]).is_file())

    def test_user_mirror_view_kept_satisfies_delivery_without_overwriting_it(self):
        self.identity["activeDisplayShaderRawSha256"] = "b" * 64
        self.identity["activeDisplayBlocks"] = [self.block]
        value = {"name": "userRoughness", "type": "float", "components": [{"offset": 0}],
                 "status": "confirmed", "provenance": "asset", "evidence": "user assigned"}
        aliases.publish_aliases(self.block["block"], self.block["members"], [], "user",
                                shader_scope={"shaderRawSha256": "b" * 64, "stage": "fragment",
                                              "bind": 4, "space": 0}, semantic_fields=[value])
        result = self.call_publish([], semantic_fields=[dict(value, name="aiRoughness", bind=4)])
        self.assertNotIn("error", result, result)
        self.assertFalse(result["partial"])
        self.assertEqual(result["counts"]["writtenSemanticFields"], 1)
        self.assertEqual(result["counts"]["requestedDisplaySemanticFields"], 1)
        self.assertEqual(result["counts"]["writtenDisplaySemanticFields"], 0)
        self.assertEqual(result["counts"]["keptUserDisplaySemanticFields"], 1)
        self.assertEqual(result["semanticMirrors"][0]["semanticView"]["fields"][0]["name"], "userRoughness")

    def test_all_user_semantic_fields_are_preserved_without_a_repeat_publication_loop(self):
        value = {"name": "userRoughness", "type": "float", "components": [{"offset": 0}],
                 "status": "confirmed", "provenance": "asset", "evidence": "user assigned"}
        aliases.publish_aliases(self.block["block"], self.block["members"], [], "user",
                                shader_scope={"shaderRawSha256": "a" * 64, "stage": "fragment",
                                              "bind": 4, "space": 0}, semantic_fields=[value])
        result = self.call_publish([], semantic_fields=[dict(value, name="aiRoughness", bind=4)])
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertFalse(result["partial"])
        self.assertFalse(result["facts"]["newAliasesWritten"])
        self.assertEqual(result["counts"]["keptUserSemanticFields"], 1)
        self.assertTrue(result["acceptance"]["requestedSemanticFieldsAccountedFor"])

    def test_stripped_duplicate_unknown_rows_publish_against_verified_active_typed_reflection(self):
        self.identity["activeDisplayShaderRawSha256"] = "b" * 64
        self.identity["activeDisplayBlocks"] = [{
            "block": "CaptureConstantsB4", "bind": 4, "space": 0, "byteSize": 32,
            "members": [{"name": "cb4_c0_x", "offset": 0}],
            "leaves": [{"path": "cb4_c0_x", "offset": 0, "byteSize": 4, "baseType": "float"}]}]
        self.block.update(members=[{"name": "unknown", "offset": 0}] * 13, leaves=[],
                          byteSize=32, memberMappingKind="placeholder")
        result = self.call_publish([], semantic_fields=[{
            "bind": 4, "name": "roughness", "type": "float", "components": [{"offset": 0}],
            "status": "inferred", "provenance": "shader", "evidence": "source:123"}])
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["counts"]["writtenSemanticFields"], 1)
        self.assertEqual(result["counts"]["writtenDisplaySemanticFields"], 1)
        self.assertTrue(result["acceptance"]["currentDisplaySemanticFieldsAccountedFor"])
        self.assertEqual(result["published"][0]["semanticBindingBasis"], "verified-active-reflection")
        self.assertEqual(result["published"][0]["semanticView"]["capturedShaderRawSha256"], "a" * 64)
        self.assertEqual(result["semanticOriginalUnavailable"][0]["bind"], 4)
        receipt = json.loads(Path(result["receiptPersistence"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["semanticViewStorePaths"][0]["shaderRawSha256"], "b" * 64)

    def test_owner_candidate_and_display_layout_have_independent_durable_identities(self):
        from src.shader_semantic_delivery import publication_state
        source = self.workspace / "source.glsl"
        source.write_text("layout(std140) uniform Material { vec4 tint; };", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        status = {"workspacePath": str(self.workspace), "workspaceId": "workspace-a",
                  "source": {"path": "source.glsl"}}
        self.identity.update(activeDisplayShaderRawSha256="a" * 64,
                             activeReplacementShaderRawSha256="b" * 64)
        fields = [{"bind": 4, "name": "tint", "type": "float4",
                   "components": [{"offset": offset} for offset in (0, 4, 8, 12)],
                   "status": "inferred", "provenance": "shader", "evidence": "source.glsl:1"}]
        reviews = [{"bind": 4, "unresolvedOffsets": [16, 20, 24, 28],
                    "evidence": "Unconsumed trailing declaration; keep physical names"}]
        with mock.patch.object(server, "_alias_publication_workspace_status", return_value=status):
            arguments = dict(semantic_source_sha256=digest, semantic_fields=fields, binding_reviews=reviews)
            first = self.call_publish([], operation_id="owner-publication-first", **arguments)
            self.assertNotIn("error", first, first)
            saved = Path(first["receiptPersistence"]["path"])
            first_receipt = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(first_receipt["activeDisplayShaderRawSha256"], "a" * 64)
            self.assertEqual(first_receipt["activeReplacementShaderRawSha256"], "b" * 64)
            self.assertEqual(publication_state(first_receipt, digest, "b" * 64)["status"], "complete")
            self.assertEqual(publication_state(first_receipt, digest, "c" * 64)["status"], "complete")
            self.identity["activeReplacementShaderRawSha256"] = "c" * 64
            second = self.call_publish([], operation_id="owner-publication-second", **arguments)
            self.assertNotIn("error", second, second)
            self.assertNotEqual(first["requestSha256"], second["requestSha256"])
            current = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(publication_state(current, digest, "c" * 64)["status"], "complete")
            current_bytes = saved.read_bytes()
            # A timeout retry is historical and must neither rewrite nor bless
            # the current candidate using the previous operation's identity.
            retry = self.call_publish([], operation_id="owner-publication-first", **arguments)
            self.assertEqual(retry["activeReplacementShaderRawSha256"], "b" * 64)
            self.assertEqual(saved.read_bytes(), current_bytes)
            self.identity["activeReplacementShaderRawSha256"] = ""
            unapplied = self.call_publish([], **arguments)
            self.assertNotIn("error", unapplied, unapplied)
            current = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(current["activeReplacementShaderRawSha256"], "")
            self.assertEqual(publication_state(current, digest, "c" * 64)["status"], "complete")
            self.assertFalse(publication_state(current, digest, "c" * 64)["shaderExecutionModified"])

    def test_partial_binding_coverage_is_not_reused_after_owner_candidate_changes(self):
        source = self.workspace / "source.glsl"
        source.write_text("layout(std140) uniform Material { vec4 tint; };", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        status = {"workspacePath": str(self.workspace), "workspaceId": "workspace-a",
                  "source": {"path": "source.glsl"}}
        self.identity.update(activeDisplayShaderRawSha256="a" * 64,
                             activeReplacementShaderRawSha256="b" * 64)
        self.identity["blocks"].append({"block": "View", "bind": 1, "space": 0,
            "members": [{"name": "viewScale", "offset": 0}],
            "leaves": [{"path": "viewScale", "offset": 0, "byteSize": 4}]})
        reviews = [{"bind": 4, "unresolvedOffsets": list(range(0, 32, 4)),
                    "evidence": "Dynamic indexing; no reliable role recovered"},
                   {"bind": 1, "unresolvedOffsets": [0],
                    "evidence": "View consumer cannot yet distinguish scale from bias"}]
        with mock.patch.object(server, "_alias_publication_workspace_status", return_value=status):
            first = self.call_publish([], semantic_source_sha256=digest, binding_reviews=reviews)
            self.assertNotIn("error", first, first)
            self.assertTrue(first["bindingCoverage"]["complete"])
            unchanged = self.call_publish([], semantic_source_sha256=digest, binding_reviews=reviews[:1])
            self.assertTrue(unchanged["bindingCoverage"]["complete"])
            self.identity["activeReplacementShaderRawSha256"] = "c" * 64
            changed = self.call_publish([], semantic_source_sha256=digest, binding_reviews=reviews[:1])
            self.assertNotIn("error", changed, changed)
            self.assertFalse(changed["bindingCoverage"]["complete"])
            self.assertEqual(changed["bindingCoverage"]["pendingBindings"],
                             [{"bind": 1, "space": 0, "missingLaneCount": 1}])

    def compiled_receipt(
        self,
        rows,
        *,
        output_ranges=None,
        raw_sha256=None,
        layout_sha256=None,
    ):
        members = [
            {"name": item.get("name"), "offset": item.get("offset")}
            for item in self.block["members"]
        ]
        return server._compiled_layout_receipt(
            shader_platform="SP_PCD3D_SM6",
            metadata_sha256="1" * 64,
            evidence_files=[{
                "path": str(self.workspace / "material-shader-metadata.json"),
                "sha256": "2" * 64,
                "byteSize": 128,
            }],
            outputs=output_ranges or [{
                "byteOffset": int(row["byteOffset"]),
                "byteSize": 16,
                "outputType": "Float4",
            } for row in rows],
            alias_rows=rows,
            event_id=self.identity["eventId"],
            stage=self.identity["stage"],
            bind=rows[0]["bind"],
            space=rows[0].get("space", 0),
            capture_evidence={"workspacePhysicalBindingVerified": True},
            shader_raw_sha256=raw_sha256 or self.identity["rawSha256"],
            layout_sha256=layout_sha256 or aliases.layout_sha256(
                self.block["block"], members
            ),
            capture_api="D3D12",
            capture_platform_compatible=True,
        )

    def test_same_platform_compiled_evidence_publishes_semantic_rows(self):
        rows = [
            {"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}
        ]
        result = self.publish(rows)
        self.assertTrue(result["applied"])
        self.assertEqual(result["counts"]["writtenRows"], 1)
        self.assertFalse(result["facts"]["hostWorkflowAuthorizationRequired"])
        self.assertTrue(result["compiledLayoutEvidence"]["provided"])
        self.assertTrue(result["compiledLayoutEvidence"]["validated"])
        self.assertEqual(result["compiledLayoutEvidence"]["authority"], "evidence-only")

    def test_semantic_rows_without_compiled_evidence_use_current_reflection(self):
        result = self.call_publish([
            {"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}
        ])
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertFalse(result["compiledLayoutEvidence"]["provided"])
        self.assertTrue(result["compiledLayoutEvidence"]["semanticNamesAgentChosen"])
        self.assertEqual(result["semanticNameVerification"], "agent-provenance-only")
        self.assertEqual(result["physicalBinding"]["shaderRawSha256"], self.identity["rawSha256"])

    def test_agent_naming_evidence_is_recorded_but_not_authority(self):
        evidence = {
            "schema": "bread-cbuffer-naming-evidence/v1",
            "summary": "The unpacked material labels the captured offset as base color.",
            "references": [{
                "kind": "unpacked-asset",
                "locator": "/Game/Materials/M_Test:BaseColor",
                "sha256": "d" * 64,
            }],
        }
        result = self.call_publish(
            [{"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}],
            naming_evidence=evidence,
        )
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertEqual(result["namingEvidence"]["authority"], "agent-provenance-only")
        self.assertEqual(result["namingEvidence"]["references"][0]["sha256"], "d" * 64)
        self.assertTrue(result["receiptPersistence"]["persisted"])

    def test_agent_naming_evidence_does_not_override_bad_physical_offset(self):
        result = self.call_publish(
            [{"bind": 4, "space": 0, "byteOffset": 512, "alias": "BaseColor"}],
            naming_evidence={
                "schema": "bread-cbuffer-naming-evidence/v1",
                "summary": "An asset suggests the name.",
                "references": [{"kind": "analysis", "locator": "material parameter"}],
            },
        )
        self.assertIn("no current reflected leaf", result["error"])

    def test_naming_evidence_is_bound_to_idempotent_request(self):
        rows = [{"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}]
        evidence = {
            "schema": "bread-cbuffer-naming-evidence/v1",
            "summary": "Matched unpacked material parameter.",
            "references": [{"kind": "unpacked-asset", "locator": "M_Test:BaseColor"}],
        }
        operation_id = "named-cbuffer-evidence-operation"
        first = self.call_publish(rows, operation_id=operation_id, naming_evidence=evidence)
        self.assertNotIn("error", first, first)
        repeated = self.call_publish(rows, operation_id=operation_id, naming_evidence=evidence)
        self.assertTrue(repeated["idempotentReplay"])
        changed = self.call_publish(
            rows, operation_id=operation_id,
            naming_evidence={**evidence, "summary": "A different claim."},
        )
        self.assertIn("operation", changed["error"].lower())

    def test_neutral_reflection_names_need_no_compiled_evidence(self):
        result = self.call_publish(
            [{
                "bind": 4,
                "space": 0,
                "byteOffset": 0,
                "alias": "cbuffer_b4_offset_0",
            }],
            block_alias="cbuffer_b4",
        )
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertTrue(result["compiledLayoutEvidence"]["neutralOnly"])
        self.assertFalse(result["compiledLayoutEvidence"]["provided"])

    def test_nonzero_space_neutral_name_has_one_deterministic_spelling(self):
        self.block["space"] = 2
        result = self.call_publish(
            [{
                "bind": 4,
                "space": 2,
                "byteOffset": 0,
                "alias": "cbuffer_s2_b4_offset_0",
            }],
            block_alias="cbuffer_s2_b4",
        )
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])

    def test_semantic_rows_require_explicit_true_platform_compatibility(self):
        rows = [
            {"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}
        ]
        for compatible in (False, None):
            with self.subTest(capturePlatformCompatible=compatible):
                receipt = self.compiled_receipt(rows)
                receipt["capturePlatformCompatible"] = compatible
                unsigned = {
                    key: value
                    for key, value in receipt.items()
                    if key != "receiptSha256"
                }
                receipt["receiptSha256"] = server._canonical_json_sha256(
                    unsigned, "compiled CBuffer layout receipt"
                )
                result = self.call_publish(
                    rows,
                    compiled_layout_receipt=receipt,
                )
                self.assertIn("proved compatible", result["error"])

    def test_forged_legacy_authorization_is_ignored_not_verified(self):
        manager = mock.Mock()
        manager.normalize_cbuffer_alias_rows.side_effect = (
            ShaderOrganizationManager.normalize_cbuffer_alias_rows
        )
        manager.verify_cbuffer_alias_authorization.side_effect = AssertionError(
            "legacy authorization must not be consulted"
        )
        with mock.patch.object(
            server, "_shader_organization_manager", return_value=manager
        ):
            result = self.publish(
                [{"bind": 4, "byteOffset": 0, "alias": "BaseColor"}],
                compiled_layout_receipt=self.compiled_receipt([
                    {"bind": 4, "byteOffset": 0, "alias": "BaseColor"}
                ]),
                legacy_authorization={
                    "authorizationId": "forged",
                    "authorizationHmacSha256": "0" * 64,
                    "evidenceAssets": [],
                },
            )
        manager.verify_cbuffer_alias_authorization.assert_not_called()
        self.assertTrue(result["applied"])
        self.assertEqual(result["counts"]["writtenRows"], 1)

    def test_compiled_layout_receipt_hash_change_is_rejected_before_write(self):
        rows = [{"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}]
        receipt = self.compiled_receipt(rows)
        receipt["metadataSha256"] = "3" * 64
        result = self.call_publish(rows, compiled_layout_receipt=receipt)
        self.assertIn("malformed or changed", result["error"])
        self.assertEqual(aliases.read_aliases(
            aliases.layout_sha256(self.block["block"], self.block["members"])
        ), [])

    def test_compiled_layout_receipt_must_cover_requested_byte_offset(self):
        rows = [{"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}]
        receipt = self.compiled_receipt(rows, output_ranges=[{
            "byteOffset": 16, "byteSize": 16, "outputType": "Float4"
        }])
        result = self.call_publish(rows, compiled_layout_receipt=receipt)
        self.assertIn("outside the decoded compiled output ranges", result["error"])

    def test_compiled_layout_receipt_must_match_current_reflected_layout(self):
        rows = [{"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}]
        receipt = self.compiled_receipt(rows, layout_sha256="b" * 64)
        result = self.call_publish(rows, compiled_layout_receipt=receipt)
        self.assertIn("differs from current reflected layout", result["error"])

    def test_physical_identity_change_rejects_new_write_but_not_committed_replay(self):
        rows = [{"bind": 4, "space": 0, "byteOffset": 0, "alias": "BaseColor"}]
        receipt = self.compiled_receipt(rows)
        operation_id = "physical-identity-bound-operation"
        first = self.publish(
            rows,
            operation_id=operation_id,
            compiled_layout_receipt=receipt,
        )
        self.assertTrue(first["compiledLayoutEvidence"]["validated"])

        self.identity["rawSha256"] = "b" * 64
        with mock.patch.object(
            server,
            "_shader_organization_cbuffer_alias_identity",
            side_effect=AssertionError("committed replay must precede live identity"),
        ):
            replay = self.call_publish(
                rows,
                operation_id=operation_id,
                compiled_layout_receipt=receipt,
            )
        self.assertTrue(replay["idempotentReplay"])

        changed = self.call_publish(
            rows,
            operation_id="changed-physical-identity-operation",
            compiled_layout_receipt=receipt,
        )
        self.assertIn("different original Shader bytes", changed["error"])

    def test_committed_same_request_replays_before_live_physical_identity(self):
        rows = [{
            "bind": 4,
            "space": 0,
            "byteOffset": 0,
            "alias": "cbuffer_b4_offset_0",
        }]
        operation_id = "durable-cbuffer-operation"

        first = self.call_publish(rows, operation_id=operation_id)
        self.assertNotIn("error", first, first)
        self.assertTrue(first["applied"])

        with mock.patch.object(
            server,
            "_shader_organization_cbuffer_alias_identity",
            side_effect=AssertionError("durable replay must not rebuild live identity"),
        ):
            replay = self.call_publish(rows, operation_id=operation_id)
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["operationId"], operation_id)
        self.assertEqual(replay["requestSha256"], first["requestSha256"])

    @unittest.skipUnless(os.name == "nt", "extended paths are a Windows contract")
    def test_server_fast_replay_accepts_legacy_normal_hash_from_extended_path(self):
        rows = [{
            "bind": 4,
            "space": 0,
            "byteOffset": 0,
            "alias": "cbuffer_b4_offset_0",
        }]
        authorization = {"authorizationId": "durable-path-auth"}
        operation_id = "durable-cbuffer-path-spelling"
        normal = str(self.workspace.resolve())
        extended = "\\\\?\\" + normal

        with mock.patch.object(
            server,
            "_workspace_root_for_files",
            side_effect=lambda value: Path(value).resolve(),
        ):
            first = json.loads(asyncio.run(server.publish_cbuffer_aliases(
                normal,
                json.dumps(rows),
                operation_id=operation_id,
                asset_alias_authorization=authorization,
            )))
        self.assertNotIn("error", first, first)

        transaction_dir, ledger_path = alias_publication.publication_paths(
            self.workspace, operation_id
        )
        ledger = alias_publication.read_ledger(ledger_path)
        legacy_caller = {
            "tool": "publish_cbuffer_aliases",
            "operationId": operation_id,
            "workspacePath": normal,
            "rows": rows,
            "blockAlias": "",
            "assetAliasAuthorization": authorization,
        }
        ledger["callerRequestSha256"] = server._canonical_json_sha256(
            legacy_caller, "legacy CBuffer alias caller request"
        )
        alias_publication.write_ledger(ledger_path, ledger)

        with mock.patch.object(
            server,
            "_workspace_root_for_files",
            side_effect=lambda value: Path(value).resolve(),
        ), mock.patch.object(
            server,
            "_shader_organization_cbuffer_alias_identity",
            side_effect=AssertionError("durable fast replay must not rebuild live identity"),
        ):
            replay = json.loads(asyncio.run(server.publish_cbuffer_aliases(
                extended,
                json.dumps(rows),
                operation_id=operation_id,
                asset_alias_authorization=authorization,
            )))

        self.assertNotIn("error", replay, replay)
        self.assertTrue(replay["idempotentReplay"])
        self.assertTrue(transaction_dir.is_dir())

    @unittest.skipUnless(os.name == "nt", "extended paths are a Windows contract")
    def test_server_canonical_fast_replay_is_bidirectional(self):
        rows = [{
            "bind": 4,
            "space": 0,
            "byteOffset": 0,
            "alias": "cbuffer_b4_offset_0",
        }]
        authorization = {"authorizationId": "canonical-path-auth"}
        normal = str(self.workspace.resolve())
        extended = "\\\\?\\" + normal

        for first, second, label in (
            (normal, extended, "normal-to-extended"),
            (extended, normal, "extended-to-normal"),
        ):
            with self.subTest(direction=label):
                operation_id = "canonical-cbuffer-" + label
                with mock.patch.object(
                    server,
                    "_workspace_root_for_files",
                    side_effect=lambda value: Path(value).resolve(),
                ):
                    first_result = json.loads(asyncio.run(
                        server.publish_cbuffer_aliases(
                            first,
                            json.dumps(rows),
                            operation_id=operation_id,
                            asset_alias_authorization=authorization,
                        )
                    ))
                self.assertNotIn("error", first_result, first_result)

                with mock.patch.object(
                    server,
                    "_workspace_root_for_files",
                    side_effect=lambda value: Path(value).resolve(),
                ), mock.patch.object(
                    server,
                    "_shader_organization_cbuffer_alias_identity",
                    side_effect=AssertionError(
                        "canonical fast replay rebuilt live identity"
                    ),
                ):
                    replay = json.loads(asyncio.run(
                        server.publish_cbuffer_aliases(
                            second,
                            json.dumps(rows),
                            operation_id=operation_id,
                            asset_alias_authorization=authorization,
                        )
                    ))
                self.assertNotIn("error", replay, replay)
                self.assertTrue(replay["idempotentReplay"])

    def test_server_fast_replay_retries_post_commit_staging_cleanup(self):
        rows = [{
            "bind": 4,
            "space": 0,
            "byteOffset": 0,
            "alias": "cbuffer_b4_offset_0",
        }]
        authorization = {"authorizationId": "durable-cleanup-auth"}
        operation_id = "durable-cbuffer-cleanup"
        original_cleanup = alias_publication._cleanup_committed_staging
        attempts = 0

        def fail_once(transaction_dir, ledger):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("injected post-commit sharing violation")
            return original_cleanup(transaction_dir, ledger)

        with mock.patch.object(
            alias_publication, "_cleanup_committed_staging", side_effect=fail_once
        ):
            first = json.loads(asyncio.run(server.publish_cbuffer_aliases(
                "test-workspace",
                json.dumps(rows),
                operation_id=operation_id,
                asset_alias_authorization=authorization,
            )))
        self.assertIn("cleanup is incomplete", first["error"])
        transaction_dir, ledger_path = alias_publication.publication_paths(
            self.workspace, operation_id
        )
        ledger = alias_publication.read_ledger(ledger_path)
        self.assertEqual(ledger["phase"], "committed")
        stage_root = Path(ledger["stageRoot"])
        self.assertTrue(stage_root.is_dir())

        with mock.patch.object(
            server,
            "_shader_organization_cbuffer_alias_identity",
            side_effect=AssertionError("durable replay must not rebuild live identity"),
        ), mock.patch.object(
            alias_publication, "_cleanup_committed_staging", side_effect=fail_once
        ):
            replay = json.loads(asyncio.run(server.publish_cbuffer_aliases(
                "test-workspace",
                json.dumps(rows),
                operation_id=operation_id,
                asset_alias_authorization=authorization,
            )))
        self.assertNotIn("error", replay, replay)
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(attempts, 2)
        self.assertFalse(stage_root.exists())
        self.assertFalse(list(transaction_dir.glob("backup-*.json")))

    def test_fast_replay_lock_wait_does_not_block_async_event_loop(self):
        rows = [{
            "bind": 4,
            "space": 0,
            "byteOffset": 0,
            "alias": "cbuffer_b4_offset_0",
        }]
        authorization = {"authorizationId": "nonblocking-auth"}
        entered = threading.Event()
        release = threading.Event()

        def blocking_replay(**arguments):
            entered.set()
            if not release.wait(1.0):
                raise RuntimeError("test replay wait was not released")
            return {
                "schema": "replaylab-cbuffer-alias-publication/v1",
                "schemaVersion": 1,
                "operationId": arguments["operation_id"],
                "idempotentReplay": True,
                "applied": True,
            }

        async def run_request():
            started = time.monotonic()
            task = asyncio.create_task(server.publish_cbuffer_aliases(
                "test-workspace",
                json.dumps(rows),
                operation_id="nonblocking-fast-replay",
                asset_alias_authorization=authorization,
            ))
            observed = await asyncio.to_thread(entered.wait, 1.0)
            responsive_after = time.monotonic() - started
            release.set()
            return observed, responsive_after, json.loads(await task)

        safety_release = threading.Timer(0.4, release.set)
        safety_release.start()
        try:
            with mock.patch.object(
                alias_publication,
                "replay_committed_receipt",
                side_effect=blocking_replay,
            ):
                observed, responsive_after, result = asyncio.run(run_request())
        finally:
            release.set()
            safety_release.cancel()

        self.assertTrue(observed)
        self.assertLess(responsive_after, 0.2)
        self.assertTrue(result["idempotentReplay"])

    def test_private_source_rename_alias_publisher_is_fail_closed(self):
        source = (
            "cbuffer Material : register(b1) {\n"
            "  float4 member0 : packoffset(c0);\n"
            "};\n"
            "float4 MainPS() : SV_Target { return member0; }\n"
        )
        with self.assertRaisesRegex(
            Exception, "explicit publish_cbuffer_aliases tool"
        ):
            ShaderOrganizationManager._publish_cbuffer_aliases(
                source, {"member0": "materialValue0"}
            )

    def test_missing_current_physical_block_is_rejected_before_write(self):
        result = self.call_publish([
            {"bind": 8, "byteOffset": 0, "alias": "Surface"}
        ])
        self.assertIn("no unique current reflected block", result["error"])

    def test_invalid_alias_is_rejected_before_write(self):
        result = self.call_publish([
            {"bind": 4, "byteOffset": 0, "alias": "X" * 65}
        ])
        self.assertIn("valid alias", result["error"])
        self.assertEqual(list(Path(self.temporary.name).glob("*.json")), [])

    def test_invalid_row_makes_the_direct_publication_batch_atomic(self):
        result = self.call_publish([
            {"bind": 4, "byteOffset": 0, "alias": "Surface"},
            {"bind": 4, "byteOffset": 16, "alias": "X" * 65},
            {"bind": 9, "byteOffset": 0, "alias": "Missing"},
        ])
        self.assertIn("valid alias", result["error"])
        layout = aliases.layout_sha256(self.block["block"], self.block["members"])
        self.assertEqual(aliases.read_aliases(layout), [])

    def test_all_rows_and_block_alias_written_report_full_apply(self):
        result = self.publish([
            {"bind": 4, "byteOffset": 0, "alias": "Surface"},
            {"bind": 4, "byteOffset": 16, "alias": "Lighting"},
        ], block_alias="Material Parameters")
        self.assertTrue(result["applied"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["counts"]["writtenRows"], 2)
        self.assertEqual(result["counts"]["writtenBlockAliases"], 1)
        written = json.loads(Path(result["published"][0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual([item["alias"] for item in written["aliases"]], ["Surface", "Lighting"])
        self.assertEqual(
            [item["alias"] for item in written["blockAliases"]],
            ["Material Parameters"],
        )
        self.assertEqual(
            written["blockAliases"][0]["shaderScope"]["shaderRawSha256"],
            self.identity["rawSha256"],
        )
        self.assertEqual(
            written["blockAliases"][0]["shaderScope"]["shaderResourceId"],
            str(self.identity["shaderResourceId"]),
        )

    def test_user_names_kept_are_not_reported_as_new_writes(self):
        aliases.publish_aliases("Material", self.block["members"], [
            {"path": "parameters[0]", "offset": 0, "alias": "User Surface"}], "user")
        result = self.publish([{"bind": 4, "byteOffset": 0, "alias": "AI Surface"}])
        self.assertFalse(result["applied"])
        self.assertEqual(result["counts"]["keptUserRows"], 1)
        self.assertEqual(result["counts"]["writtenRows"], 0)
        block_only = self.publish([{"bind": 4, "byteOffset": 0, "alias": "AI Surface"}],
                                  block_alias="Material Parameters")
        self.assertTrue(block_only["applied"])
        # Respecting the user's existing row name fulfills that request's
        # precedence rule; it is not a missing effect that should be retried.
        self.assertFalse(block_only["partial"])
        self.assertEqual(block_only["counts"]["writtenRows"], 0)
        self.assertEqual(block_only["counts"]["writtenBlockAliases"], 1)

    def test_uncompiled_semantic_name_keeps_existing_user_alias(self):
        aliases.publish_aliases("Material", self.block["members"], [
            {"path": "parameters[0]", "offset": 0, "alias": "User Surface"}], "user")
        result = self.call_publish([
            {"bind": 4, "space": 0, "byteOffset": 0, "alias": "AI Surface"}
        ])
        self.assertNotIn("error", result, result)
        self.assertFalse(result["applied"])
        self.assertEqual(result["counts"]["keptUserRows"], 1)

    def test_block_alias_cannot_ambiguously_target_several_physical_layouts(self):
        other = {**self.block, "block": "Other", "space": 2}
        result = self.call_publish([
            {"bind": 4, "space": 0, "byteOffset": 0, "alias": "Surface"},
            {"bind": 4, "space": 2, "byteOffset": 0, "alias": "Other Surface"},
        ], block_alias="First Material", blocks=[self.block, other])
        self.assertIn("several CBuffer layouts", result["error"])

    def test_placeholder_names_publish_with_exact_shader_physical_scope(self):
        self.block["members"] = [{"name": "unknown", "offset": 0}] * 13
        self.block["leaves"] = [{"path": "unknown[1]", "offset": 16, "byteSize": 16}]
        self.block["memberMappingAvailable"] = True
        self.block["memberMappingKind"] = "placeholder"
        result = self.publish([{"bind": 4, "byteOffset": 16, "alias": "Material Tint"}],
                              block_alias="Material Parameters")
        self.assertTrue(result["applied"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["counts"]["writtenRows"], 1)
        self.assertEqual(result["counts"]["writtenBlockAliases"], 1)
        self.assertEqual(result["counts"]["rejectedAliases"], 0)
        self.assertEqual(result["counts"]["unprocessedRows"], 0)
        scope = result["published"][0]["shaderScope"]
        self.assertEqual(scope["shaderRawSha256"], self.identity["rawSha256"])
        self.assertEqual(
            scope["shaderResourceId"], str(self.identity["shaderResourceId"])
        )
        self.assertEqual(scope["stage"], self.identity["stage"])
        self.assertEqual((scope["bind"], scope["space"]), (4, 0))
        self.assertTrue(result["facts"]["newAliasesWritten"])
        self.assertTrue(result["facts"]["placeholderAliasesShaderScoped"])
        self.assertTrue(result["acceptance"]["noUnprocessedRows"])


class BoundTextureBridgeShapeTests(unittest.TestCase):
    def test_constant_buffer_values_are_strict_json_and_keep_original_bits(self):
        bridge_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"
        tree = ast.parse(bridge_path.read_text(encoding="utf-8-sig"))
        functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)
                     and item.name in {"_get_constant_buffer_values", "_strict_float_vector"}]
        bits = [0x7fc01234, 0x7f800000, 0xff800000, 1, 0x3f52b9c5]
        data = struct.pack("<5I", *bits)
        resource = types.SimpleNamespace(resourceId=77, name="bound")
        reflection = types.SimpleNamespace(constantBlocks=[types.SimpleNamespace(
            name="Material", fixedBindNumber=4, fixedBindSetOrSpace=2, byteSize=len(data))])
        pipeline = types.SimpleNamespace(GetShader=lambda unused: 5)
        read_sizes = []
        controller = types.SimpleNamespace(
            GetPipelineState=lambda: pipeline, GetShaderEntryPoints=lambda unused: ["main"],
            GetShader=lambda *unused: reflection, GetResources=lambda: [resource],
            GetBufferData=lambda rid, offset, size: read_sizes.append(size) or data[:size])
        namespace = {
            "math": math, "MAX_CONSTANT_BUFFER_VALUE_BYTES": 64 * 1024,
            "_normalise_stage_name": lambda stage: stage,
            "_available_shader_stages": lambda unused: [("fragment", "PS")],
            "_replay": lambda callback: callback(controller),
            "_set_frame_event": lambda *unused: None,
            "_get_pipeline_obj": lambda *unused: 1,
            "_pipeline_shader_entry_selection": lambda *unused: {"entry": "main"},
            "_get_constant_block_binding": lambda *unused: {
                "resourceId": 77, "byteOffset": 0, "byteSize": 2 * 1024 * 1024},
            "_resource_id_lookup": lambda *unused: 77,
            "_resource_id_int": int, "_safe_int": lambda value, default: int(value),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(bridge_path), "exec"), namespace)
        result = namespace["_get_constant_buffer_values"]({"event_id": 10, "stage": "fragment"})
        block = json.loads(json.dumps(result, allow_nan=False))["constantBuffers"][0]
        self.assertEqual(read_sizes, [len(data)])
        self.assertEqual(block["status"], "available")
        self.assertEqual(block["uints"], bits)
        self.assertEqual(block["floats"][:3], [None] * 3)
        self.assertGreater(block["floats"][3], 0.0)
        self.assertEqual(block["floats"][4], struct.unpack("<f", data[-4:])[0])
        self.assertEqual(block["floatNonFinite"], [
            {"index": 0, "value": "nan"}, {"index": 1, "value": "+inf"},
            {"index": 2, "value": "-inf"}])
        # The bound view can also be shorter than the reflected declaration.
        namespace["_get_constant_block_binding"] = lambda *unused: {
            "resourceId": 77, "byteOffset": 0, "byteSize": 4}
        short = namespace["_get_constant_buffer_values"]({"event_id": 10, "stage": "fragment"})
        self.assertEqual(read_sizes[-1], 4)
        self.assertEqual(short["constantBuffers"][0]["uints"], bits[:1])

    def test_bound_texture_reports_the_reflected_register_space(self):
        bridge_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"
        tree = ast.parse(bridge_path.read_text(encoding="utf-8-sig"))
        functions = [
            next(item for item in tree.body
                 if isinstance(item, ast.FunctionDef) and item.name == name)
            for name in ("_exact_readonly_descriptor_resources", "_get_bound_textures")
        ]
        reflection = types.SimpleNamespace(readOnlyResources=[types.SimpleNamespace(
            isTexture=True, name="sampled", fixedBindNumber=7, fixedBindSetOrSpace=2)])
        pipeline = types.SimpleNamespace(GetShader=lambda unused: 5, GetReadOnlyResources=lambda unused: [])
        controller = types.SimpleNamespace(
            GetPipelineState=lambda: pipeline, GetShaderEntryPoints=lambda unused: ["main"],
            GetShader=lambda *unused: reflection, GetResources=lambda: [], GetTextures=lambda: [])
        namespace = {
            "_normalise_stage_name": lambda stage: stage,
            "_available_shader_stages": lambda unused: [("fragment", "PS")],
            "_replay": lambda callback: callback(controller),
            "_set_frame_event": lambda *unused: None,
            "_get_pipeline_obj": lambda *unused: 1,
            "_pipeline_shader_entry_selection": lambda *unused: {"entry": "main"},
            "_shader_reflection_identity_failure": lambda *unused: None,
            "_resolve_vulkan_descriptors": lambda *unused: {},
            "_resolve_bound_resource": lambda *unused: 77,
            "_safe_int": lambda value, default: int(value),
            "_infer_role": lambda *unused: "unknown",
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(bridge_path), "exec"), namespace)
        result = namespace["_get_bound_textures"]({"event_id": 10, "stage": "fragment"})
        self.assertEqual((result[0]["bind"], result[0]["space"], result[0]["resourceId"]), (7, 2, 77))
