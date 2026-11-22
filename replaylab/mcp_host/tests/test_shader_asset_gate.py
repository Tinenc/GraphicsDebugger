"""Asset matching waits for a complete index, and its result is kept in the project."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import server


ASSET_BINDING = {
    "assetRoot": "C:/assets",
    "catalogSha256": "c" * 64,
    "assetVersionSha256": "d" * 64,
    "assetContentSha256": "e" * 64,
    "assetEvidenceSha256": "f" * 64,
    "evidenceAssets": [{
        "assetId": "asset-material",
        "relativePath": "Materials/M_Test.uasset",
        "contentSha256": "e" * 64,
    }],
}
NO_MATCH_BINDING = {
    **{key: value for key, value in ASSET_BINDING.items()
       if key not in {"assetContentSha256", "evidenceAssets"}},
    "evidenceAssets": [],
}


def asset_authority(*, positive: bool = False, final_no_match: bool = False):
    return {
        "schema": "replaylab-shader-asset-authorization/v1",
        "schemaVersion": 1,
        "current": positive or final_no_match,
        "resolvedAssets": ([{
            "assetRelativePath": "Materials/M_Test.uasset",
            "assetBinding": ASSET_BINDING,
            "readerProof": {"contentBearingTools": ["retrieve_shader_asset_candidates"]},
        }] if positive else []),
        "noSupportedMatch": ({
            "finalNoSupportedMatch": True,
            "resolutionId": "negative-receipt",
        } if final_no_match else None),
        "workspaceMirrorAuthority": False,
    }


class AssetIndexGateTests(unittest.TestCase):
    def test_inspect_shader_surfaces_captured_original_workspace_identity(self) -> None:
        effective = {
            "stage": "fragment",
            "resourceId": 902,
            "entryPoint": "MainPS",
            "rawSha256": "e" * 64,
            "capturedOriginalIdentity": {
                "stage": "fragment",
                "resourceId": 902,
                "entryPoint": "MainPS",
                "rawSha256": "3" * 64,
                "captureSessionId": "a" * 32,
            },
        }
        with mock.patch.object(
            server,
            "_call",
            return_value=json.dumps({
                "schema": "renderdoc-internal-inspect-shader/v2",
                "eventId": 11384,
                "stage": "fragment",
                "shader": effective,
            }),
        ):
            result = json.loads(server.inspect_shader(11384, "fragment"))

        self.assertEqual(result["workspaceIdentity"]["rawSha256"], "3" * 64)
        self.assertEqual(result["shader"]["rawSha256"], "e" * 64)
        self.assertEqual(
            result["identityUsage"]["workspaceAndContinuation"],
            "workspaceIdentity",
        )


    def test_configured_asset_failure_is_not_misreported_as_no_asset_root(self) -> None:
        with mock.patch.object(
            server,
            "_active_asset_catalog_context",
            side_effect=RuntimeError("asset catalog database is locked"),
        ):
            state = server._asset_index_state()
        self.assertEqual(state["state"], "failed")
        self.assertTrue(state["assetRootConfigured"])
        self.assertNotIn("requiredAction", state)

    def test_running_scan_reports_indexing_and_names_the_wait_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "assets.sqlite3"
            database.write_bytes(b"")
            status = {"scope": {"buildId": "build-1"}}
            with mock.patch.object(server, "_active_asset_catalog_context",
                                   return_value=(None, status, root, database)), \
                    mock.patch.object(server, "AssetCatalogService") as catalog, \
                    mock.patch.object(server, "_asset_index_status",
                                      return_value={"status": "running", "owner": "ReplayLabInternal",
                                                    "progress": {"files": 120}, "elapsedSeconds": 31.0}):
                catalog.return_value.status.return_value = {"complete": False, "assetCount": 120}
                state = server._asset_index_state()
        self.assertEqual(state["state"], "indexing")
        self.assertTrue(state["running"])
        self.assertNotIn("requiredAction", state)
        self.assertEqual(state["partialAssetCount"], 120)


    def test_missing_remembered_asset_reports_complete_catalog_no_match(self) -> None:
        """A stale resolution must not crash before returning search coverage."""

        catalog = mock.Mock()
        catalog.query.return_value = {"assets": []}
        catalog.status.return_value = {"complete": True}
        store = mock.Mock()
        store.lookup.return_value = {
            "resolved": True,
            "resolution": {
                "assetRelativePath": "Materials/M_Removed.uasset",
            },
        }
        identity = {
            "rawSha256": "a" * 64,
            "stage": "fragment",
            "entryPoint": "MainPS",
        }
        features = {"structuralKey": "shape"}
        with mock.patch.object(
            server,
            "_ensure_shader_asset_catalog",
            return_value=(mock.Mock(), {}, Path("C:/assets"), Path("C:/assets.db"), {"partial": False}),
        ), mock.patch.object(
            server, "_shader_asset_capture_features", return_value=(identity, features)
        ), mock.patch.object(
            server, "AssetCatalogService", return_value=catalog
        ), mock.patch.object(
            server, "_shader_asset_game_key", return_value="game"
        ), mock.patch.object(
            server.shader_asset_retrieval,
            "ShaderAssetResolutionStore",
            return_value=store,
        ), mock.patch.object(
            server, "_asset_index_state", return_value={"state": "ready"}
        ):
            result = json.loads(
                server.retrieve_shader_asset_candidates(11384, "fragment")
            )

        self.assertEqual(result["state"], "remembered-resolution")
        self.assertFalse(result["resolvedAssetStillPresent"])
        self.assertEqual(result["searchCoverage"]["outcome"], "no-match")







    def test_host_authority_requires_store_receipt_and_current_dependencies(self) -> None:
        identity = {
            "captureId": "b" * 64,
            "captureSessionId": "a" * 32,
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        row = {
            "resolutionId": 7,
            "assetRelativePath": "Materials/M_Test.uasset",
            "assetContentSha256": ASSET_BINDING["assetContentSha256"],
            "assetClass": "MaterialInstanceConstant",
            "catalogSha256": ASSET_BINDING["catalogSha256"],
            "negative": False,
            "confidence": 0.98,
            "rationale": "exact material parameters and texture references",
            "evidence": {
                "shaderIdentity": identity,
                "assetBinding": ASSET_BINDING,
                "readerProof": {
                    "contentBearingTools": ["retrieve_shader_asset_candidates"]
                },
            },
        }
        context = (None, {"scope": {"gameId": "g", "buildId": "b"}},
                   Path("C:/assets"), Path("C:/managed/catalog.sqlite3"))
        with mock.patch.object(
            server, "_shader_asset_capture_features",
            return_value=(identity, {"structuralKey": "shape"}),
        ), mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context,
        ), mock.patch.object(
            server.shader_asset_retrieval, "ShaderAssetResolutionStore"
        ) as resolution_store:
            resolution_store.return_value.lookup.return_value = {
                "resolutions": [], "rejected": []
            }
            missing = server._shader_asset_authorization_identity(
                "C:/workspace", 11402, "PS", "c" * 64
            )
            self.assertFalse(missing["current"])

            resolution_store.return_value.lookup.return_value = {
                "resolutions": [row], "rejected": []
            }
            # A same-user/Agent write can make a self-consistent SQLite row,
            # but it cannot mint the Host process provenance receipt.
            with mock.patch.object(
                server, "_asset_evidence_binding_is_current", return_value=(True, "")
            ):
                forged = server._shader_asset_authorization_identity(
                    "C:/workspace", 11402, "PS", "c" * 64
                )
            self.assertFalse(forged["current"])
            self.assertIn("Host provenance", forged["staleReasons"][0])

            row["evidence"] = server._seal_resolution_evidence(
                row, row["evidence"]
            )
            with mock.patch.object(
                server, "_asset_evidence_binding_is_current",
                return_value=(False, "an asset evidence dependency changed"),
            ):
                stale = server._shader_asset_authorization_identity(
                    "C:/workspace", 11402, "PS", "c" * 64
                )
            self.assertFalse(stale["current"])
            self.assertEqual(
                stale["staleReasons"], ["an asset evidence dependency changed"]
            )

            with mock.patch.object(
                server, "_asset_evidence_binding_is_current", return_value=(True, "")
            ):
                current = server._shader_asset_authorization_identity(
                    "C:/workspace", 11402, "PS", "c" * 64
                )
            self.assertTrue(current["current"])
            self.assertEqual(current["resolvedAssets"][0]["resolutionId"], 7)
            self.assertFalse(current["workspaceMirrorAuthority"])

    def test_asset_authorization_survives_reopen_session_and_resource_id(self) -> None:
        identity = {
            "captureId": "b" * 64,
            "captureSessionId": "a" * 32,
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        stored = {
            "captureId": "b" * 64,
            "captureSessionId": "ffffffffffffffffffffffffffffffff",
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000999",
            "rawSha256": "c" * 64,
        }
        row = {
            "resolutionId": 9,
            "assetRelativePath": "Materials/M_Test.uasset",
            "assetContentSha256": ASSET_BINDING["assetContentSha256"],
            "assetClass": "MaterialInstanceConstant",
            "catalogSha256": ASSET_BINDING["catalogSha256"],
            "negative": False,
            "confidence": 0.98,
            "rationale": "exact material parameters and texture references",
            "evidence": {
                "shaderIdentity": stored,
                "assetBinding": ASSET_BINDING,
                "readerProof": {
                    "contentBearingTools": ["retrieve_shader_asset_candidates"]
                },
            },
        }
        row["evidence"] = server._seal_resolution_evidence(row, row["evidence"])
        context = (None, {"scope": {"gameId": "g", "buildId": "b"}},
                   Path("C:/assets"), Path("C:/managed/catalog.sqlite3"))
        with mock.patch.object(
            server, "_shader_asset_capture_features",
            return_value=(identity, {"structuralKey": "shape"}),
        ), mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context,
        ), mock.patch.object(
            server.shader_asset_retrieval, "ShaderAssetResolutionStore"
        ) as resolution_store, mock.patch.object(
            server, "_asset_evidence_binding_is_current", return_value=(True, "")
        ):
            resolution_store.return_value.lookup.return_value = {
                "resolutions": [row], "rejected": []
            }
            current = server._shader_asset_authorization_identity(
                "C:/workspace", 11402, "PS", "c" * 64
            )
        self.assertTrue(current["current"])
        self.assertEqual(current["resolvedAssets"][0]["resolutionId"], 9)

    def test_direct_sqlite_resolution_forgery_has_no_host_provenance(self) -> None:
        from src.shader_asset_retrieval import ShaderAssetResolutionStore

        identity = {
            "captureId": "b" * 64,
            "captureSessionId": "a" * 32,
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            managed = Path(directory)
            database = managed / "catalog.sqlite3"
            forged = ShaderAssetResolutionStore(
                managed / server.SHADER_ASSET_RESOLUTIONS_FILENAME
            )
            forged.record(
                game_key="test-game",
                shader_raw_sha256="c" * 64,
                structural="shape",
                stage="PS",
                entry_point="MainPS",
                asset_relative_path="Materials/M_Test.uasset",
                asset_content_sha256=ASSET_BINDING["assetContentSha256"],
                asset_class="MaterialInstanceConstant",
                catalog_sha256=ASSET_BINDING["catalogSha256"],
                negative=False,
                confidence=0.99,
                decided_by="ai",
                rationale="forged but self-consistent",
                evidence={
                    "shaderIdentity": identity,
                    "assetBinding": ASSET_BINDING,
                    "readerProof": {"contentBearingTools": ["reader"]},
                },
            )
            context = (
                None,
                {"scope": {"gameId": "g", "buildId": "b"}},
                Path("C:/assets"),
                database,
            )
            with mock.patch.object(
                server, "_shader_asset_capture_features",
                return_value=(identity, {"structuralKey": "shape"}),
            ), mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context,
            ), mock.patch.object(
                server, "_shader_asset_game_key", return_value="test-game",
            ), mock.patch.object(
                server, "_asset_evidence_binding_is_current", return_value=(True, ""),
            ):
                authority = server._shader_asset_authorization_identity(
                    "C:/workspace", 11402, "PS", "c" * 64
                )
        self.assertFalse(authority["current"])
        self.assertIn("Host provenance", authority["staleReasons"][0])

    def test_negative_submit_records_agent_search_memory_without_host_authority(self) -> None:
        identity = {
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        context = (
            None,
            {"scope": {"gameId": "g", "buildId": "b"}},
            Path("C:/assets"),
            Path("C:/managed/catalog.sqlite3"),
        )
        report_store = mock.Mock()
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context,
        ), mock.patch.object(
            server, "_shader_asset_capture_features",
            return_value=(identity, {"structuralKey": "shape"}),
        ), mock.patch.object(
            server, "_shader_asset_game_key", return_value="test-game",
        ), mock.patch.object(
            server, "current_task_report_store", return_value=report_store,
        ), mock.patch.object(server, "AssetCatalogService") as catalog, \
                mock.patch.object(
                    server.shader_asset_retrieval, "ShaderAssetResolutionStore"
                ) as resolution_store, mock.patch.object(
                    server, "_asset_no_match_evidence_binding",
                    side_effect=AssertionError("must not build a root manifest"),
                ), mock.patch.object(
                    server, "_seal_resolution_evidence",
                    side_effect=AssertionError("must not mint a Host HMAC"),
                ):
            catalog.return_value.status.return_value = {
                "complete": True, "assetCount": 321, "catalogSha256": "d" * 64,
            }
            resolution_store.return_value.record.return_value = {
                "resolutionId": 7, "negative": True,
            }
            result = json.loads(server.submit_shader_asset_resolution(
                11402, "PS", "", 0.8, "all supported readers returned no match",
                links_json=json.dumps({"searchCoverage": {"readers": ["uex", "json"]}}),
                final_no_supported_match=True,
            ))

        self.assertNotIn("error", result)
        self.assertEqual(result["semanticAuthority"], "agent")
        self.assertFalse(result["hostWorkflowAuthorizationRequired"])
        self.assertFalse(result["publishingAuthority"])
        self.assertNotIn("assetBinding", result)
        self.assertEqual(result["resolution"]["searchCoverage"]["catalogAssetCount"], 321)
        evidence = resolution_store.return_value.record.call_args.kwargs["evidence"]
        self.assertEqual(evidence["decisionKind"], "agent-no-supported-match")
        self.assertNotIn("assetBinding", evidence)
        self.assertNotIn("hostResolutionHmacSha256", evidence)
        report_store.require_shader_asset_no_match_finalization.assert_not_called()

    def test_positive_submit_accepts_catalog_match_without_prior_describe_proof(self) -> None:
        identity = {
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        binding = {
            **ASSET_BINDING,
            "assetRootManifestSha256": "1" * 64,
            "assetRootFileCount": 1,
            "assetRootByteSize": 128,
            "assetContentSha256": "e" * 64,
        }
        asset = {
            "assetId": "asset-material",
            "relativePath": "Materials/M_Test.uasset",
            "contentSha256": "e" * 64,
            "metadata": {"className": "MaterialInstanceConstant"},
        }
        report_store = mock.Mock()
        report_store.require_shader_asset_positive_resolution.side_effect = RuntimeError(
            "positive asset resolution requires this exact asset in a successful "
            "Host content-reader receipt for the current task"
        )
        context = (
            None,
            {"scope": {"gameId": "g", "buildId": "b"}},
            Path("C:/assets"),
            Path("C:/managed/catalog.sqlite3"),
        )
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context,
        ), mock.patch.object(
            server, "_shader_asset_capture_features",
            return_value=(identity, {"structuralKey": "shape", "boundTextureNames": []}),
        ), mock.patch.object(
            server, "_shader_asset_game_key", return_value="test-game",
        ), mock.patch.object(
            server, "_asset_resolution_evidence_binding", return_value=binding,
        ), mock.patch.object(
            server, "current_task_report_store", return_value=report_store,
        ), mock.patch.object(server, "AssetCatalogService") as catalog, \
                mock.patch.object(
                    server.shader_asset_retrieval, "ShaderAssetResolutionStore"
                ) as resolution_store:
            catalog.return_value.query.return_value = {"assets": [asset]}
            resolution_store.return_value.record.return_value = {
                "resolutionId": 1,
                "negative": False,
            }
            accepted = json.loads(server.submit_shader_asset_resolution(
                11402,
                "PS",
                asset["assetId"],
                0.95,
                "exact material content",
            ))
            self.assertNotIn("error", accepted)
            self.assertNotEqual(accepted.get("authorizationBlocked"), True)
            recorded_evidence = resolution_store.return_value.record.call_args.kwargs[
                "evidence"
            ]
            self.assertRegex(
                recorded_evidence["hostResolutionHmacSha256"], r"^[0-9a-f]{64}$"
            )
            reader_proof = recorded_evidence.get("readerProof") or {}
            self.assertEqual(reader_proof.get("source"), "catalog-match")
            self.assertTrue(
                server._asset_content_proof_is_host_sealed(
                    reader_proof.get("assetContentProof") or {}
                )
            )

    def test_positive_submit_still_blocks_without_catalog_content_hash(self) -> None:
        identity = {
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        binding = {
            **ASSET_BINDING,
            "assetRootManifestSha256": "1" * 64,
            "assetRootFileCount": 1,
            "assetRootByteSize": 128,
            "assetContentSha256": "",
        }
        asset = {
            "assetId": "asset-material",
            "relativePath": "Materials/M_Test.uasset",
            "metadata": {"className": "MaterialInstanceConstant"},
        }
        report_store = mock.Mock()
        report_store.require_shader_asset_positive_resolution.side_effect = RuntimeError(
            "missing content proof"
        )
        context = (
            None,
            {"scope": {"gameId": "g", "buildId": "b"}},
            Path("C:/assets"),
            Path("C:/managed/catalog.sqlite3"),
        )
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context,
        ), mock.patch.object(
            server, "_shader_asset_capture_features",
            return_value=(identity, {"structuralKey": "shape", "boundTextureNames": []}),
        ), mock.patch.object(
            server, "_shader_asset_game_key", return_value="test-game",
        ), mock.patch.object(
            server, "_asset_resolution_evidence_binding", return_value=binding,
        ), mock.patch.object(
            server, "current_task_report_store", return_value=report_store,
        ), mock.patch.object(server, "AssetCatalogService") as catalog, \
                mock.patch.object(
                    server.shader_asset_retrieval, "ShaderAssetResolutionStore"
                ) as resolution_store:
            catalog.return_value.query.return_value = {"assets": [asset]}
            rejected = json.loads(server.submit_shader_asset_resolution(
                11402,
                "PS",
                asset["assetId"],
                0.95,
                "exact material content",
            ))
            self.assertTrue(rejected.get("authorizationBlocked") is True)
            status = rejected.get("authorizationStatus") or {}
            self.assertEqual(status.get("code"), "submit-missing-catalog-content-hash")
            resolution_store.return_value.record.assert_not_called()

    def test_positive_submit_uses_exact_catalog_content_without_task_ledger(self) -> None:
        identity = {
            "eventId": 11402,
            "stage": "PS",
            "entryPoint": "MainPS",
            "shaderResourceId": "1000000000000000723",
            "rawSha256": "c" * 64,
        }
        binding = {
            **ASSET_BINDING,
            "assetRootManifestSha256": "1" * 64,
            "assetRootFileCount": 1,
            "assetRootByteSize": 128,
        }
        asset = {
            "assetId": "asset-material",
            "relativePath": "Materials/M_Test.uasset",
            "metadata": {"className": "MaterialInstanceConstant"},
        }
        context = (
            None,
            {"scope": {"gameId": "g", "buildId": "b"}},
            Path("C:/assets"),
            Path("C:/managed/catalog.sqlite3"),
        )
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context,
        ), mock.patch.object(
            server, "_shader_asset_capture_features",
            return_value=(identity, {"structuralKey": "shape", "boundTextureNames": []}),
        ), mock.patch.object(
            server, "_shader_asset_game_key", return_value="test-game",
        ), mock.patch.object(
            server, "_asset_resolution_evidence_binding", return_value=binding,
        ), mock.patch.object(server, "AssetCatalogService") as catalog, \
                mock.patch.object(
                    server.shader_asset_retrieval, "ShaderAssetResolutionStore"
                ) as resolution_store:
            catalog.return_value.query.return_value = {"assets": [asset]}
            resolution_store.return_value.record.return_value = {
                "resolutionId": 1,
                "negative": False,
            }
            accepted = json.loads(server.submit_shader_asset_resolution(
                11402,
                "PS",
                asset["assetId"],
                0.95,
                "exact material content",
            ))
            self.assertNotIn("error", accepted)
            recorded_evidence = resolution_store.return_value.record.call_args.kwargs[
                "evidence"
            ]
            self.assertRegex(
                recorded_evidence["hostResolutionHmacSha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual(
                (recorded_evidence.get("readerProof") or {}).get("source"),
                "catalog-match",
            )
            self.assertTrue(server._asset_content_proof_is_host_sealed(
                recorded_evidence["readerProof"]["assetContentProof"]
            ))

    def test_final_no_match_requires_current_task_host_reader_ledger(self) -> None:
        binding = {
            **NO_MATCH_BINDING,
            "assetRootManifestSha256": "1" * 64,
            "assetRootFileCount": 10,
            "assetRootByteSize": 2048,
        }
        report_store = mock.Mock(enabled=True, task_id="task-current")
        server._ASSET_CONTENT_RECEIPT_TASKS.clear()
        server._ASSET_NO_MATCH_READER_TASKS.clear()
        with mock.patch.object(
            server, "current_task_report_store", return_value=report_store
        ):
            with self.assertRaisesRegex(RuntimeError, "Host task's"):
                server._require_host_no_match_grounding(binding)
            # A successful read of an arbitrary asset is content evidence for a
            # positive decision about that asset, not coverage of every possible
            # material for this Shader.
            server._seal_asset_content_proof({
                "assetId": "unrelated-texture",
                "relativePath": "Textures/T_Unrelated.png",
                "tool": "read_unpacked_asset",
                "receiptSha256": "7" * 64,
                "assetBinding": binding,
            })
            with self.assertRaisesRegex(RuntimeError, "unrelated asset read"):
                server._require_host_no_match_grounding(binding)
            server._seal_asset_reader_attempt({
                "supported": True,
                "started": True,
                "outcome": "no-match",
                "readerId": "catalog-reader",
                "coverage": {"completeCatalog": True, "narrowed": False},
            }, binding)
            with self.assertRaisesRegex(RuntimeError, "two distinct"):
                server._require_host_no_match_grounding(binding)
            server._seal_asset_reader_attempt({
                "supported": True,
                "started": True,
                "outcome": "no-match",
                "readerId": "direct-root-reader",
                "coverage": {"completeCatalog": True, "narrowed": False},
            }, binding)
            server._require_host_no_match_grounding(binding)

            # A different task cannot replay either Host outcome.
            report_store.task_id = "task-next"
            with self.assertRaisesRegex(RuntimeError, "Host task's"):
                server._require_host_no_match_grounding(binding)

    def test_asset_root_change_invalidates_positive_and_no_match_bindings(self) -> None:
        from src import asset_catalog
        from src.asset_catalog import AssetCatalogService

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "unpacked"
            root.mkdir()
            material = root / "material.json"
            material.write_text(
                '{"name":"M_Test","scalars":{"Roughness":0.5}}',
                encoding="utf-8",
            )
            database = base / "managed" / "assets.sqlite3"
            database.parent.mkdir()
            catalog = AssetCatalogService(database)
            catalog.scan(root, build_id="build", depth="full")
            asset = catalog.query(
                relative_paths=["material.json"], limit=1,
                include_relationships=True,
            )["assets"][0]
            with mock.patch.object(
                asset_catalog,
                "_content_manifest_file_sha256",
                wraps=asset_catalog._content_manifest_file_sha256,
            ) as content_hash:
                positive = server._asset_resolution_evidence_binding(
                    catalog, root, asset
                )
                first_hash_count = content_hash.call_count
                self.assertGreater(first_hash_count, 0)
                negative = server._asset_no_match_evidence_binding(catalog, root)
                # The second binding consumes the catalog-owner generation;
                # unchanged contents are not read again.
                self.assertEqual(content_hash.call_count, first_hash_count)
            context = (
                None,
                {"scope": {"buildId": "build"}},
                root.resolve(),
                database.resolve(),
            )
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ):
                with mock.patch.object(
                    asset_catalog.os, "walk", wraps=os.walk
                ) as walk, mock.patch.object(
                    asset_catalog, "_content_manifest_file_sha256",
                    wraps=asset_catalog._content_manifest_file_sha256,
                ) as content_hash:
                    self.assertEqual(
                        server._asset_evidence_binding_is_current(positive), (True, "")
                    )
                    self.assertEqual(
                        server._asset_evidence_binding_is_current(negative), (True, "")
                    )
                    server._asset_catalog_version_binding(catalog, root)
                    self.assertEqual(walk.call_count, 0)
                    self.assertEqual(content_hash.call_count, 0)

                # A completed owner refresh advances its scan receipt but the
                # stable content Merkle is unchanged. Reopening an unchanged
                # project therefore keeps both positive and negative proofs.
                prior_generation = positive["assetRootManifestGeneration"]
                unchanged = catalog.refresh_content_manifest(root)
                self.assertGreater(
                    unchanged["assetRootManifestGeneration"], prior_generation
                )
                self.assertEqual(
                    server._asset_catalog_version_binding(catalog, root)[
                        "assetVersionSha256"
                    ],
                    positive["assetVersionSha256"],
                )
                self.assertEqual(
                    server._asset_evidence_binding_is_current(positive), (True, "")
                )
                self.assertEqual(
                    server._asset_evidence_binding_is_current(negative), (True, "")
                )

                # File-system time noise advances the owner receipt but not the
                # stable path+size+content Merkle used by no-match authority.
                unchanged_stat = material.stat()
                os.utime(
                    material,
                    ns=(
                        unchanged_stat.st_atime_ns,
                        unchanged_stat.st_mtime_ns + 10_000_000,
                    ),
                )
                timestamp_only = catalog.refresh_content_manifest(root)
                self.assertEqual(
                    timestamp_only["assetRootManifestSha256"],
                    negative["assetRootManifestSha256"],
                )
                self.assertEqual(
                    server._asset_evidence_binding_is_current(negative), (True, "")
                )

                # Equal-size overwrite plus restored mtime must still move the
                # owner's Merkle generation (NTFS ChangeTime on Windows).
                old_stat = material.stat()
                time.sleep(0.02)
                material.write_text(
                    '{"name":"M_Test","scalars":{"Roughness":0.7}}',
                    encoding="utf-8",
                )
                self.assertEqual(material.stat().st_size, old_stat.st_size)
                os.utime(
                    material,
                    ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns),
                )
                refreshed = catalog.refresh_content_manifest(root)
                self.assertNotEqual(
                    refreshed["assetRootManifestSha256"],
                    positive["assetRootManifestSha256"],
                )
                # Content overwrite of a matched material invalidates both the
                # positive content proof and the whole-root no-match conclusion.
                self.assertFalse(
                    server._asset_evidence_binding_is_current(positive)[0]
                )
                self.assertFalse(
                    server._asset_evidence_binding_is_current(negative)[0]
                )

                (root / "new-material.json").write_text(
                    '{"name":"M_New"}', encoding="utf-8"
                )
                catalog.refresh_content_manifest(root)
                # Adding an unrelated file does not change a positive proof's
                # selected content hash, but it invalidates a whole-root no-match.
                self.assertFalse(
                    server._asset_evidence_binding_is_current(positive)[0]
                )
                self.assertFalse(
                    server._asset_evidence_binding_is_current(negative)[0]
                )

    def test_no_match_binding_is_invalid_after_asset_deletion(self) -> None:
        from src.asset_catalog import AssetCatalogService

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "unpacked"
            root.mkdir()
            material = root / "material.json"
            material.write_text('{"name":"M_Test"}', encoding="utf-8")
            database = base / "managed" / "assets.sqlite3"
            database.parent.mkdir()
            catalog = AssetCatalogService(database)
            catalog.scan(root, build_id="build", depth="full")
            catalog.refresh_content_manifest(root)
            negative = server._asset_no_match_evidence_binding(catalog, root)
            context = (
                None,
                {"scope": {"buildId": "build"}},
                root.resolve(),
                database.resolve(),
            )

            material.unlink()
            catalog.refresh_content_manifest(root)
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ):
                current, reason = server._asset_evidence_binding_is_current(
                    negative
                )

            self.assertFalse(current)
            self.assertIn("content revision changed", reason)

    def test_activation_epoch_ignores_an_old_done_job_and_starts_refresh(self) -> None:
        context = (
            None,
            {"scope": {"buildId": "build"}},
            Path("C:/assets"),
            Path("C:/managed/assets.sqlite3"),
        )
        old_job = {
            "status": "done",
            "result": {"contentManifest": {"assetRootManifestGeneration": 1}},
        }
        server._ASSET_ACTIVATION_REFRESHED.clear()
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context
        ), mock.patch.object(server, "AssetCatalogService") as catalog, \
                mock.patch.object(server, "_asset_index_status", return_value=old_job), \
                mock.patch.object(
                    server, "_asset_index_job", return_value={"status": "running"}
                ) as start:
            catalog.return_value.status.return_value = {
                "catalogSha256": "a" * 64
            }
            result = server._ensure_asset_activation_content_refresh(
                force_activation=True
            )
            self.assertEqual(result["status"], "running")
            start.assert_called_once_with(
                Path("C:/managed/assets.sqlite3"),
                Path("C:/assets"),
                "build",
                depth="headers",
            )
            # Queries in the same activation consume the completed generation
            # without starting another O(N) walk.
            server._ensure_asset_activation_content_refresh()
            start.assert_called_once()

    def test_activation_refresh_prepares_a_missing_catalog_instead_of_silently_stopping(self) -> None:
        context = (
            None,
            {"scope": {"buildId": "build"}},
            Path("C:/assets"),
            Path("C:/managed/assets.sqlite3"),
        )
        server._ASSET_ACTIVATION_REFRESHED.clear()
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context
        ) as active, mock.patch.object(
            Path, "is_file", return_value=False
        ), mock.patch.object(
            server, "_asset_index_status", return_value=None
        ), mock.patch.object(
            server, "_asset_index_job", return_value={"status": "running"}
        ) as start:
            result = server._ensure_asset_activation_content_refresh(
                force_activation=True,
                depth="names",
            )

        self.assertEqual(result["status"], "running")
        active.assert_called_once_with(
            require_catalog=False,
            create_dictionary=True,
        )
        start.assert_called_once_with(
            Path("C:/managed/assets.sqlite3"),
            Path("C:/assets"),
            "build",
            depth="names",
        )

    def test_activation_refresh_surfaces_catalog_prepare_failure(self) -> None:
        context = (
            None,
            {"scope": {"buildId": "build"}},
            Path("C:/assets"),
            Path("C:/managed/assets.sqlite3"),
        )
        server._ASSET_ACTIVATION_REFRESHED.clear()
        with mock.patch.object(
            server, "_active_asset_catalog_context", return_value=context
        ), mock.patch.object(
            Path, "is_file", return_value=False
        ), mock.patch.object(
            server, "_asset_index_status", return_value=None
        ), mock.patch.object(
            server,
            "_asset_index_job",
            side_effect=RuntimeError("catalog is locked"),
        ):
            with self.assertRaisesRegex(RuntimeError, "catalog is locked"):
                server._ensure_asset_activation_content_refresh(
                    force_activation=True,
                    depth="names",
                )

    def test_activate_project_schedules_a_new_content_generation(self) -> None:
        service = mock.Mock()
        service.activate.return_value = {"workspaceId": "project-a", "assetRootConfigured": True}
        with mock.patch.object(server, "_active_project_service", return_value=service), \
                mock.patch.object(
                    server, "_ensure_asset_activation_content_refresh",
                    return_value={"status": "running", "depth": "names"},
                ) as refresh:
            result = json.loads(server.activate_active_replay_project("C:/projects/a"))
        self.assertNotIn("error", result)
        refresh.assert_called_once_with(force_activation=True, depth="names")

    def test_configure_asset_root_schedules_only_a_names_generation(self) -> None:
        service = mock.Mock()
        service.configure_asset_root.return_value = {"workspaceId": "project-a"}
        with mock.patch.object(server, "_active_project_service", return_value=service), \
                mock.patch.object(
                    server, "_ensure_asset_activation_content_refresh",
                    return_value={"status": "running", "depth": "names"},
                ) as refresh:
            result = json.loads(
                server.configure_active_project_asset_root("C:/assets")
            )
        self.assertNotIn("error", result)
        refresh.assert_called_once_with(force_activation=True, depth="names")

    def test_activation_refresh_invalidates_a_new_root_file_without_manual_gate_scan(self) -> None:
        from src.asset_catalog import AssetCatalogService

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "unpacked"
            root.mkdir()
            (root / "material.json").write_text('{"name":"M_A"}', encoding="utf-8")
            database = base / "managed" / "assets.sqlite3"
            database.parent.mkdir()
            catalog = AssetCatalogService(database)
            catalog.scan(root, build_id="build", depth="full")
            catalog.refresh_content_manifest(root)
            negative = server._asset_no_match_evidence_binding(catalog, root)
            (root / "new-material.json").write_text(
                '{"name":"M_B"}', encoding="utf-8"
            )
            context = (
                None,
                {"scope": {"buildId": "build"}},
                root.resolve(),
                database.resolve(),
            )

            def owner_refresh(*unused, **unused_kwargs):
                catalog.scan(root, build_id="build", depth="headers")
                return {
                    "status": "done",
                    "result": {
                        "contentManifest": catalog.refresh_content_manifest(root)
                    },
                }

            server._ASSET_ACTIVATION_REFRESHED.clear()
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ), mock.patch.object(
                server, "_asset_index_status",
                return_value={"status": "done", "result": {"contentManifest": {}}},
            ), mock.patch.object(
                server, "_asset_index_job", side_effect=owner_refresh
            ):
                server._ensure_asset_activation_content_refresh(
                    force_activation=True
                )
                self.assertFalse(
                    server._asset_evidence_binding_is_current(negative)[0]
                )

    def test_wait_tool_is_sliced_and_never_blocks_the_host(self) -> None:
        # The tool is a coroutine on purpose: a synchronous sleep here holds the whole
        # MCP server and every other call (even ping) times out behind it.
        self.assertTrue(asyncio.iscoroutinefunction(server.wait_for_unpacked_asset_index))
        for invalid in (0, -5, 100000):
            payload = json.loads(asyncio.run(server.wait_for_unpacked_asset_index(invalid)))
            self.assertIn("timeout_seconds must be between", payload["error"])
        with mock.patch.object(server, "_asset_index_state",
                               return_value={"state": "ready", "assetCount": 7}):
            ready = json.loads(asyncio.run(server.wait_for_unpacked_asset_index(5)))
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["schema"], "replaylab-unpacked-asset-index-wait/v1")


    def test_catalog_publish_survives_a_reader_holding_the_old_file(self) -> None:
        """WinError 5 on the final rename must not throw away a 190k-file scan."""

        from src import asset_catalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "catalog.tmp"
            target = root / "catalog.sqlite3"
            temporary.write_bytes(b"new")
            target.write_bytes(b"old")
            attempts = {"count": 0}
            real_replace = os.replace

            def flaky(source, destination):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise PermissionError(5, "Access is denied")
                real_replace(source, destination)

            with mock.patch.object(asset_catalog.os, "replace", side_effect=flaky), \
                    mock.patch.object(asset_catalog.time, "sleep"):
                asset_catalog._replace_with_retry(temporary, target)
            self.assertEqual(attempts["count"], 3)
            self.assertEqual(target.read_bytes(), b"new")

            # A reader that never lets go still surfaces the error, bounded.
            refused = {"count": 0}

            def always_denied(source, destination):
                refused["count"] += 1
                raise PermissionError(5, "Access is denied")

            temporary.write_bytes(b"newer")
            with mock.patch.object(asset_catalog.os, "replace", side_effect=always_denied), \
                    mock.patch.object(asset_catalog.time, "sleep"):
                with self.assertRaises(PermissionError):
                    asset_catalog._replace_with_retry(temporary, target, attempts=3)
            self.assertEqual(refused["count"], 3)


if __name__ == "__main__":
    unittest.main()


class TargetedHydrationTests(unittest.TestCase):
    def test_names_index_plus_draw_words_parse_only_the_matching_files(self) -> None:
        """No whole-tree parse: the words from the draw pick the files; those get read."""

        from src.asset_catalog import AssetCatalogService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "unpacked"
            managed = Path(directory) / "managed"
            (root / "Content" / "Hat").mkdir(parents=True)
            (root / "Content" / "Rock").mkdir(parents=True)
            managed.mkdir()
            (root / "Content" / "Hat" / "M_Hat_Straw.mtl").write_text(
                "newmtl Hat\nmap_Kd T_Hat_D.png\nKd 0.12345 0.5 0.25\n", encoding="utf-8")
            (root / "Content" / "Hat" / "T_Hat_D.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
            (root / "Content" / "Rock" / "M_Rock.mtl").write_text(
                "newmtl Rock\nKd 0.9 0.9 0.9\n", encoding="utf-8")
            for index in range(40):
                (root / "Content" / "Rock" / ("Filler_%02d.ubulk" % index)).write_bytes(bytes(256))
            catalog = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            names = catalog.scan(root, build_id="b", depth="names")
            self.assertEqual(names["deferredCount"], names["assetCount"])

            features = {"boundTextureNames": ["T_Hat_D"], "extraTokens": ["hat", "straw"],
                        "resourceNames": []}
            receipt = server._targeted_asset_hydration(catalog, root, features)
            hydrated = set(receipt["hydrated"])
            self.assertIn("Content/Hat/M_Hat_Straw.mtl", hydrated)
            self.assertIn("Content/Hat/T_Hat_D.png", hydrated)
            self.assertNotIn("Content/Rock/M_Rock.mtl", hydrated)
            self.assertFalse(any(path.endswith(".ubulk") for path in hydrated))
            # Only the targeted rows left the deferred state.
            status = AssetCatalogService(managed / "asset-catalog-v1.sqlite3").status()
            parsed = AssetCatalogService(managed / "asset-catalog-v1.sqlite3").query(
                text="Hat", limit=20)["assets"]
            self.assertTrue(all(item["hashStatus"] == "sha256" for item in parsed))
            self.assertEqual(status["assetCount"], names["assetCount"])
            rock = AssetCatalogService(managed / "asset-catalog-v1.sqlite3").query(
                text="M_Rock", limit=5)["assets"][0]
            self.assertEqual(rock["hashStatus"], "deferred")
            # A second request for the same words parses nothing new.
            again = server._targeted_asset_hydration(
                AssetCatalogService(managed / "asset-catalog-v1.sqlite3"), root, features)
            self.assertEqual(again["hydrated"], [])


class AssetsFirstTests(unittest.TestCase):
    """Naming happens after the assets are read, and the Host says so up front.

    In a real run the AI organized a 4,000-line shader and named ~500 locals
    without ever opening the project's assets -- it never saw the procedure
    (the Skill payload had been spilled to a file its client could not read),
    so nothing in the loop told it. A procedure nobody reads is not a control;
    the tool result it cannot avoid reading is.
    """



class AssetIndexReceiptTests(unittest.TestCase):
    def test_background_index_receipt_reports_state_without_a_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = (
                None,
                {"workspaceId": "project-1", "projectRoot": str(root),
                 "scope": {"buildId": "build-1"}},
                root / "assets",
                root / "catalog.sqlite3",
            )
            job = {"status": "running", "jobId": "job-1"}
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ), mock.patch.object(server, "_asset_index_job", return_value=job):
                receipt = json.loads(server.index_active_unpacked_assets())
        self.assertTrue(receipt["started"])
        self.assertEqual(receipt["job"], job)
        self.assertNotIn("next", receipt)
        self.assertNotIn("requiredAction", receipt)


class AssetContentProofAttemptTests(unittest.TestCase):
    """Describe can read disk while the names-only catalog has no hashable row.

    That used to return empty assetContentProofs with no actionable status, so
    Agents retried describe/submit forever. On-demand hydrate must create/hash
    the exact path (and fail closed with authorizationBlocked when it cannot).
    """

    def test_missing_catalog_row_hydrates_from_disk_and_seals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            root.mkdir()
            relative = "Materials/M_Missing.uasset"
            disk = root / "Materials"
            disk.mkdir()
            (disk / "M_Missing.uasset").write_bytes(b"ue-package-bytes")
            hashed = {
                "assetId": "asset-missing",
                "relativePath": relative,
                "contentSha256": "a" * 64,
                "hashStatus": "sha256",
                "relationships": [],
            }
            catalog = mock.Mock()
            catalog.query.side_effect = [
                {"assets": []},
                {"assets": [hashed]},
            ]
            catalog.hydrate.return_value = {
                "schema": "renderdoc-ultra-unpacked-asset-hydrate/v1",
                "hydrated": [relative],
            }
            context = (
                None,
                {"scope": {"gameId": "g", "buildId": "b"}},
                root,
                Path(temporary) / "catalog.sqlite3",
            )
            binding = {
                **ASSET_BINDING,
                "assetRoot": str(root),
                "assetContentSha256": "a" * 64,
                "evidenceAssets": [{
                    "assetId": "asset-missing",
                    "relativePath": relative,
                    "contentSha256": "a" * 64,
                }],
            }
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ), mock.patch.object(
                server, "AssetCatalogService", return_value=catalog
            ), mock.patch.object(
                server, "_asset_resolution_evidence_binding", return_value=binding
            ):
                proof, status = server._asset_content_proof_attempt(
                    relative,
                    "describe_unpacked_asset",
                    receipt={"relativePath": relative},
                )
            self.assertIsNotNone(proof)
            self.assertTrue(status.get("authorized"))
            self.assertEqual(status.get("code"), "content-proof-sealed")
            self.assertNotIn("requiredAction", status)
            self.assertNotIn("next", status)
            catalog.hydrate.assert_called_once()
            hydrated_paths = list(catalog.hydrate.call_args.args[1])
            self.assertEqual(hydrated_paths, [relative])
            self.assertEqual(proof["relativePath"], relative)
            self.assertIn("hostReaderProofHmacSha256", proof)

    def test_deferred_catalog_row_hydrates_before_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            root.mkdir()
            relative = "Materials/M_Deferred.uasset"
            (root / "Materials").mkdir()
            (root / relative).write_bytes(b"deferred-bytes")
            deferred = {
                "assetId": "asset-deferred",
                "relativePath": relative,
                "contentSha256": "",
                "hashStatus": "deferred",
                "relationships": [],
            }
            hashed = {
                **deferred,
                "contentSha256": "b" * 64,
                "hashStatus": "sha256",
            }
            catalog = mock.Mock()
            catalog.query.side_effect = [
                {"assets": [deferred]},
                {"assets": [hashed]},
            ]
            catalog.hydrate.return_value = {"hydrated": [relative]}
            context = (
                None,
                {"scope": {"gameId": "g", "buildId": "b"}},
                root,
                Path(temporary) / "catalog.sqlite3",
            )
            binding = {
                **ASSET_BINDING,
                "assetRoot": str(root),
                "assetContentSha256": "b" * 64,
                "evidenceAssets": [{
                    "assetId": "asset-deferred",
                    "relativePath": relative,
                    "contentSha256": "b" * 64,
                }],
            }
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ), mock.patch.object(
                server, "AssetCatalogService", return_value=catalog
            ), mock.patch.object(
                server, "_asset_resolution_evidence_binding", return_value=binding
            ):
                proof, status = server._asset_content_proof_attempt(
                    relative, "describe_unpacked_asset"
                )
            self.assertIsNotNone(proof)
            self.assertEqual(status.get("code"), "content-proof-sealed")
            catalog.hydrate.assert_called()

    def test_missing_disk_and_catalog_returns_blocked_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            root.mkdir()
            catalog = mock.Mock()
            catalog.query.return_value = {"assets": []}
            context = (
                None,
                {"scope": {"gameId": "g", "buildId": "b"}},
                root,
                Path(temporary) / "catalog.sqlite3",
            )
            with mock.patch.object(
                server, "_active_asset_catalog_context", return_value=context
            ), mock.patch.object(
                server, "AssetCatalogService", return_value=catalog
            ):
                proof, status = server._asset_content_proof_attempt(
                    "Materials/M_Absent.uasset", "describe_unpacked_asset"
                )
            self.assertIsNone(proof)
            self.assertFalse(status.get("authorized"))
            self.assertEqual(status.get("code"), "catalog-path-missing")
            self.assertNotIn("requiredAction", status)
            self.assertNotIn("next", status)
            self.assertEqual(status.get("detail", {}).get("matchCount"), 0)
            catalog.hydrate.assert_not_called()
