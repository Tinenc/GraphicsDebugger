from __future__ import annotations

import copy
import contextlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from src import project_restore
from src.rdc_project_index import new_index, upsert_project_binding
from src.rdc_shader_inventory_cache import (
    build_shader_inventory_cache_document,
    load_shader_inventory_cache,
)


class _FastMCP:
    def __init__(self, *unused_args, **unused_kwargs):
        pass

    def tool(self, *unused_args, **unused_kwargs):
        return lambda function: function

    def run(self, **unused_kwargs):
        pass


def _load_server():
    try:
        return importlib.import_module("src.server")
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
    mcp_package = types.ModuleType("mcp")
    mcp_package.__path__ = []
    mcp_server = types.ModuleType("mcp.server")
    mcp_server.__path__ = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    with mock.patch.dict(
        sys.modules,
        {
            "mcp": mcp_package,
            "mcp.server": mcp_server,
            "mcp.server.fastmcp": fastmcp,
        },
    ):
        return importlib.import_module("src.server")


server = _load_server()


class _ActiveService:
    def __init__(self, status: dict):
        self._status = status
        self.aliases_directory = (
            Path(str(status["projectRoot"])).resolve() / "shared" / "aliases"
        )
        self.aliases_directory.mkdir(parents=True, exist_ok=True)
        self.pin_depth = 0
        self.blocked_switches = 0

    def status(self) -> dict:
        return copy.deepcopy(self._status)

    def shared_dictionary_directory(self, kind: str) -> Path:
        if kind != "aliases":
            raise AssertionError("unexpected shared dictionary kind")
        return self.aliases_directory

    @contextlib.contextmanager
    def pin_active_project(self, workspace_id: str, project_root: Path):
        if (
            workspace_id != self._status["workspaceId"]
            or Path(project_root).resolve()
            != Path(self._status["projectRoot"]).resolve()
        ):
            raise RuntimeError("active project changed before the operation")
        self.pin_depth += 1
        try:
            yield
        finally:
            self.pin_depth -= 1
            if (
                workspace_id != self._status["workspaceId"]
                or Path(project_root).resolve()
                != Path(self._status["projectRoot"]).resolve()
            ):
                raise RuntimeError("active project changed during the operation")


class _WorkspaceService:
    def __init__(self, status: dict):
        self._status = status
        self.status_calls = 0
        self.import_calls = 0
        self.import_handler = None
        self.extra_statuses: list[dict] = []
        self.fallback_status = None

    def status(self, workspace_path: str) -> dict:
        self.status_calls += 1
        for status in [self._status, *self.extra_statuses]:
            if Path(workspace_path).resolve() == Path(status["workspacePath"]).resolve():
                return copy.deepcopy(status)
        if self.fallback_status is not None:
            return copy.deepcopy(self.fallback_status(workspace_path))
        raise AssertionError("unexpected workspace path")

    def import_project_candidate(self, workspace_path: str, candidate_plan: dict) -> dict:
        self.import_calls += 1
        if self.import_handler is None:
            raise AssertionError("compiled project candidate was not configured")
        return self.import_handler(workspace_path, candidate_plan)


class _ReplayService:
    def __init__(self, state: dict):
        self.state = state
        self.snapshot_calls = 0
        self.inventory_snapshot_calls = 0
        self.reuse_plan_calls = 0
        self.reuse_plan_response: dict | None = None
        self.materialize_calls = 0
        self.materialize_response: dict | None = None
        self.inventory_snapshot_handler = None
        self.inventory_seed_handler = None
        self.inventory_register_handler = None
        self.inventory_register_calls = 0
        self.last_inventory_materialization = None

    def restorable_shader_raw_hashes(self):
        return sorted({item["rawSha256"] for item in self.state["workspaces"].values()})

    def restore_knowledge_snapshot(self, capture_id: str, catalog_sha256: str) -> dict:
        self.snapshot_calls += 1
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected catalogSha256")
        return copy.deepcopy(self.state)

    def reuse_plan(self, capture_id: str, catalog_sha256: str) -> dict:
        self.reuse_plan_calls += 1
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected catalogSha256")
        if self.reuse_plan_response is not None:
            return copy.deepcopy(self.reuse_plan_response)
        result = {
            "schema": "renderdoc-ultra-project-reuse-plan/v1",
            "schemaVersion": 1,
            "projectId": self.state["projectId"],
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "matchedShaderCount": 0,
            "matches": [],
        }
        return result

    def reuse_snapshot_for_shader_inventory(self, inventory: dict) -> dict:
        if self.inventory_snapshot_handler is None:
            raise AssertionError("Shader inventory snapshot was not configured")
        self.inventory_snapshot_calls += 1
        return self.inventory_snapshot_handler(inventory, None)

    def validate_shader_inventory_snapshot(
        self, inventory: dict, binding: dict
    ) -> dict:
        if self.inventory_snapshot_handler is None:
            raise AssertionError("Shader inventory snapshot was not configured")
        return self.inventory_snapshot_handler(inventory, binding)

    def seed_workspace_from_inventory(
        self,
        knowledge_id: str,
        destination_status: dict,
        target_shader: dict,
        inventory: dict,
        expected_binding: dict,
        **policy,
    ) -> dict:
        if self.inventory_seed_handler is None:
            raise AssertionError("Shader inventory source seed was not configured")
        return self.inventory_seed_handler(
            knowledge_id,
            destination_status,
            target_shader,
            inventory,
            expected_binding,
            **policy,
        )

    def register_workspace_from_inventory(
        self,
        status: dict,
        target_shader: dict,
        inventory: dict,
        expected_binding: dict,
    ) -> dict:
        self.inventory_register_calls += 1
        if self.inventory_register_handler is None:
            raise AssertionError("Shader inventory publication was not configured")
        return self.inventory_register_handler(
            status, target_shader, inventory, expected_binding
        )

    def materialize_alias_candidates(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        unused_shader: dict,
        unused_candidates: list,
    ) -> dict:
        self.materialize_calls += 1
        if project_id != self.state["projectId"]:
            raise AssertionError("unexpected alias projectId")
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected alias captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected alias catalogSha256")
        if self.materialize_response is None:
            raise AssertionError("alias materialization was not configured")
        return copy.deepcopy(self.materialize_response)

    def materialize_alias_batch(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        targets: list,
    ) -> dict:
        self.materialize_calls += 1
        if project_id != self.state["projectId"]:
            raise AssertionError("unexpected alias projectId")
        if capture_id != self.state["captureId"]:
            raise AssertionError("unexpected alias captureId")
        if catalog_sha256 != self.state["catalogSha256"]:
            raise AssertionError("unexpected alias catalogSha256")
        if self.materialize_response is None or len(targets) != 1:
            raise AssertionError("alias batch materialization was not configured")
        materialized = copy.deepcopy(self.materialize_response)
        return {
            "schema": "renderdoc-ultra-project-alias-materialization-batch/v1",
            "schemaVersion": 1,
            "projectId": project_id,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "targetCount": 1,
            "candidateCount": len(targets[0]["candidates"]),
            "aliasCount": materialized["aliasCount"],
            "materializations": [materialized],
            "displayAliasesOnly": True,
            "applied": False,
            "projectChangedDuringBatch": False,
        }

    def materialize_alias_batch_from_inventory(
        self,
        project_id: str,
        inventory: dict,
        expected_binding: dict,
        targets: list,
    ) -> dict:
        self.materialize_calls += 1
        if project_id != self.state["projectId"]:
            raise AssertionError("unexpected alias projectId")
        if self.materialize_response is None or len(targets) != 1:
            raise AssertionError("inventory alias batch was not configured")
        capture_id = expected_binding["captureId"]
        catalog_sha256 = expected_binding["catalogSha256"]
        materialized = copy.deepcopy(self.materialize_response)
        materialized["captureId"] = capture_id
        materialized["catalogSha256"] = catalog_sha256
        materialized["inventoryOnly"] = True
        materialized["aliasTable"]["captureId"] = capture_id
        materialized["aliasTable"]["catalogSha256"] = catalog_sha256
        for alias in materialized["aliasTable"]["aliases"]:
            alias["captureId"] = capture_id
        encoded = (
            json.dumps(
                materialized["aliasTable"],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        materialized["jsonBytes"] = encoded
        materialized["sha256"] = project_restore.hashlib.sha256(encoded).hexdigest()
        result = {
            "schema": "renderdoc-ultra-project-alias-materialization-batch/v1",
            "schemaVersion": 1,
            "projectId": project_id,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "targetCount": 1,
            "candidateCount": len(targets[0]["candidates"]),
            "aliasCount": materialized["aliasCount"],
            "materializations": [materialized],
            "displayAliasesOnly": True,
            "inventoryOnly": True,
            "applied": False,
            "projectChangedDuringBatch": False,
        }
        self.last_inventory_materialization = copy.deepcopy(result)
        return result


class _IPC:
    def __init__(self, capture_path: Path, session_id: str, index: dict | None):
        self.capture_path = capture_path
        self.session_id = session_id
        self.index = index
        self.calls: list[tuple[str, dict]] = []
        self.write_error: Exception | None = None
        self.bind_error: Exception | None = None
        self.resource_index_snapshot: dict | None = None
        self.inventory_assets: list[dict] = []
        self.inventory_unindexed_resource_ids: list[int] = []
        self.inventory_result_mutator = None
        self.expected_result_mutator = None
        self.alias_load_error: Exception | None = None
        self.alias_load_error_after_commit: Exception | None = None
        self.alias_response_mutator = None
        self.alias_transaction: dict | None = None
        self.alias_last_rollback: dict | None = None
        self.alias_rollback_calls = 0
        self.shader_operations: dict[str, dict] = {}
        self.shader_operation_recovery_calls = 0
        self.shared_replacements: list[dict] = []
        self.section_version = 1
        self.begin_reports_running = False
        self.poll_errors: list[Exception] = []

    def _begin_resource_index_snapshot(self) -> dict:
        if self.resource_index_snapshot is None:
            raise AssertionError("resource-index job was not created")
        completed = copy.deepcopy(self.resource_index_snapshot)
        if not self.begin_reports_running:
            return completed
        self.begin_reports_running = False
        running = copy.deepcopy(completed)
        running["status"] = "running"
        running.pop("result", None)
        return running

    def call(self, method: str, params: dict | None = None, timeout=None):
        params = copy.deepcopy(params or {})
        self.calls.append((method, params))
        if method == "read_rdc_project_index":
            return {
                "schema": "replaylab-rdc-project-index-transport/v1",
                "schemaVersion": 1,
                "sectionName": "replaylab/cache/project-reuse-index",
                "sectionVersion": self.section_version,
                "present": self.index is not None,
                "encodedSize": 1,
                "encodedSha256": "a" * 64,
                "document": copy.deepcopy(self.index),
                "capturePath": str(self.capture_path),
                "captureSessionId": self.session_id,
            }
        if method == "begin_rdc_project_shader_resource_index":
            if self.bind_error is not None:
                raise self.bind_error
            api = project_restore._asset_api(params["expected_api"])
            inventory_only = params.get("inventory_all_shaders") is True
            if inventory_only:
                assets = copy.deepcopy(self.inventory_assets)
                targets = params.get("target_raw_sha256s")
                filtered = []
                if targets is not None:
                    filtered = sorted({asset["resourceId"] for asset in assets if asset["rawSha256"] not in targets})
                    assets = [asset for asset in assets if asset["rawSha256"] in targets]
                inventory_material = {
                    "api": api,
                    "shaders": [
                        {
                            "resourceId": asset["resourceId"],
                            "stableKeySha256": asset["stableKeySha256"],
                            "anchorEventId": asset["anchorEventId"],
                            "constantBlocks": asset["constantBlocks"],
                        }
                        for asset in assets
                    ],
                }
                resource_inventory_sha = project_restore._canonical_sha256(
                    inventory_material
                )
                stable_resource_set_sha = project_restore._canonical_sha256(
                    sorted({asset["stableKeySha256"] for asset in assets})
                )
                live_identity_sha = project_restore._canonical_sha256(
                    {
                        "api": api,
                        "shaders": [
                            {
                                "resourceId": asset["resourceId"],
                                "stableKeySha256": asset["stableKeySha256"],
                                "stableKeyComplete": asset["stableKeyComplete"],
                                "anchorEventId": asset["anchorEventId"],
                            }
                            for asset in assets
                        ],
                    }
                )
                result = {
                    "schema": "replaylab-project-shader-resource-index/v2",
                    "schemaVersion": 2,
                    "capturePath": str(self.capture_path),
                    "captureId": params["expected_capture_id"],
                    "captureSessionId": self.session_id,
                    "identityEvidenceSha256": params[
                        "expected_identity_evidence_sha256"
                    ],
                    "requestSha256": "c" * 64,
                    "inventoryOnly": True,
                    "inventoryComplete": True,
                    "leaseEligible": False,
                    "matched": False,
                    "capability": {
                        "supported": True,
                        "api": api,
                        "eventTraversalSupported": False,
                        "fullFrameTraversalSupported": False,
                        "anchorPreflightRequired": True,
                    },
                    "assets": assets,
                    "rawBytecodeBlobs": [],
                    "uniqueShaderResourceCount": (
                        len({asset["resourceId"] for asset in assets}) +
                        len(self.inventory_unindexed_resource_ids)
                    ),
                    "processedShaderResourceCount": (
                        len({asset["resourceId"] for asset in assets}) +
                        len(self.inventory_unindexed_resource_ids)
                    ),
                    "indexedShaderResourceCount": len(
                        {asset["resourceId"] for asset in assets}
                    ),
                    "unindexedShaderResourceCount": len(
                        self.inventory_unindexed_resource_ids
                    ),
                    "unindexedShaderResourceIds": copy.deepcopy(
                        self.inventory_unindexed_resource_ids
                    ),
                    "matchedShaderCount": len(assets),
                    "representativeEidCount": len(assets),
                    "symbolSnapshotPublished": True,
                    "symbolSnapshotShaderCount": len(assets),
                    "symbolSnapshotUseCount": len(assets),
                    "symbolSnapshotCaptureId": stable_resource_set_sha,
                    "resourceInventorySha256": resource_inventory_sha,
                    "stableResourceSetSha256": stable_resource_set_sha,
                    "liveIdentitySha256": live_identity_sha,
                    "eventTraversalCount": 0,
                    "fullFrameTraversal": False,
                    "gpuReplayMutation": False,
                    "physicalApplyAuthority": False,
                    "anchorPerShaderMaximum": 1,
                    "resourceOwnershipScope": "target-capture-session",
                    "crossCaptureResourceIdReuse": False,
                    "originalShaderIdentityPolicy": "captured-original-owner-v1",
                }
                if targets is not None:
                    observed = sorted({asset["rawSha256"] for asset in assets})
                    result.update(inventoryScope="target-raw-sha256", inventoryComplete=False,
                                  rawHashPurpose="captured-executable-identity", semanticEquivalenceVerified=False,
                                  targetRawSha256s=targets, scanComplete=True,
                                  targetInventoryComplete=not self.inventory_unindexed_resource_ids,
                                  rawIdentitySearchComplete=not self.inventory_unindexed_resource_ids,
                                  rawIdentityUnavailableShaderResourceIds=list(self.inventory_unindexed_resource_ids),
                                  filteredOutShaderResourceCount=len(filtered), filteredOutShaderResourceIds=filtered,
                                  targetMatchedShaderResourceIds=sorted({asset["resourceId"] for asset in assets}),
                                  observedTargetRawSha256s=observed, unobservedTargetRawSha256s=sorted(set(targets) - set(observed)),
                                  symbolSnapshotPublished=False, symbolSnapshotShaderCount=0,
                                  symbolSnapshotUseCount=0, symbolSnapshotCaptureId="")
                    result["uniqueShaderResourceCount"] += len(filtered)
                    result["processedShaderResourceCount"] += len(filtered)
                if self.inventory_result_mutator is not None:
                    self.inventory_result_mutator(result)
                self.resource_index_snapshot = {
                    "schema": "replaylab-project-shader-resource-index-job/v1",
                    "schemaVersion": 1,
                    "jobId": "d" * 32,
                    "status": "completed",
                    "captureSessionId": self.session_id,
                    "requestSha256": "c" * 64,
                    "idempotent": False,
                    "committed": False,
                    "result": result,
                }
                return self._begin_resource_index_snapshot()
            assets = []
            for shader in params["expected_shaders"]:
                asset = copy.deepcopy(shader)
                asset.update(
                    {
                        "ownerResourceId": shader["resourceId"],
                        "ownerCaptureSessionId": self.session_id,
                        "resourceOwnershipScope": "target-capture-session",
                        "crossCaptureResourceIdReuse": False,
                        "api": api,
                        "physicalAbiSha256": (
                            "" if api == "vulkan" else "f" * 64
                        ),
                        "physicalAbiValidated": api != "vulkan",
                        "requiresAnchorPreflight": True,
                        "constantBlocksMatched": True,
                        "interfaceMatched": True,
                        "contentMatchMode": (
                            "module-sha256-owner-rid-anchor-preflight"
                            if api == "vulkan"
                            else "stable-key-owner-rid"
                        ),
                    }
                )
                assets.append(asset)
            result = {
                "schema": "replaylab-project-shader-resource-index/v2",
                "schemaVersion": 2,
                "capturePath": str(self.capture_path),
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "identityEvidenceSha256": params[
                    "expected_identity_evidence_sha256"
                ],
                "matched": True,
                "reasonId": "",
                "requestSha256": "c" * 64,
                "inventoryOnly": False,
                "inventoryComplete": False,
                "leaseEligible": True,
                "capability": {
                    "supported": True,
                    "api": api,
                    "eventTraversalSupported": False,
                    "fullFrameTraversalSupported": False,
                    "physicalAbiFromNullPipeline": api != "vulkan",
                    "anchorPreflightRequired": True,
                },
                "assets": assets,
                "matchedShaderCount": len(assets),
                "expectedShaderCount": len(assets),
                "liveIdentitySha256": "e" * 64,
                "eventTraversalCount": 0,
                "fullFrameTraversal": False,
                "gpuReplayMutation": False,
                "physicalApplyAuthority": False,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
            }
            if self.expected_result_mutator is not None:
                self.expected_result_mutator(result)
            self.resource_index_snapshot = {
                "schema": "replaylab-project-shader-resource-index-job/v1",
                "schemaVersion": 1,
                "jobId": "d" * 32,
                "status": "completed",
                "captureSessionId": self.session_id,
                "requestSha256": "c" * 64,
                "idempotent": False,
                "committed": False,
                "result": result,
            }
            return self._begin_resource_index_snapshot()
        if method == "poll_rdc_project_shader_resource_index":
            if self.resource_index_snapshot is None:
                raise AssertionError("resource-index job was not started")
            if self.poll_errors:
                raise self.poll_errors.pop(0)
            return copy.deepcopy(self.resource_index_snapshot)
        if method == "commit_rdc_project_shader_resource_index":
            return {
                "schema": "replaylab-project-shader-resource-index-lease/v1",
                "schemaVersion": 1,
                "jobId": params["job_id"],
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "identityEvidenceSha256": params[
                    "expected_identity_evidence_sha256"
                ],
                "liveIdentitySha256": params["live_identity_sha256"],
                "committed": True,
                "idempotent": False,
                "eventTraversalCount": 0,
                "gpuReplayMutation": False,
                "physicalApplyAuthority": False,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
            }
        if method == "get_shared_shader_replacements":
            return {
                "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "replacements": copy.deepcopy(self.shared_replacements),
                "count": len(self.shared_replacements),
                "mutationEpoch": len(self.shared_replacements),
                "originalShadersPreserved": True,
                "replayMutation": False,
            }
        if method == "write_rdc_project_index":
            if self.write_error is not None:
                raise self.write_error
            self.index = copy.deepcopy(params["document"])
            return {
                "schema": "replaylab-rdc-project-index-transport/v1",
                "schemaVersion": 1,
                "sectionName": "replaylab/cache/project-reuse-index",
                "sectionVersion": 1,
                "present": True,
                "written": True,
                "physicalApplyAuthority": False,
                "document": copy.deepcopy(params["document"]),
                "capturePath": str(self.capture_path),
                "captureSessionId": self.session_id,
            }
        if method == "load_symbol_aliases":
            if self.alias_load_error is not None:
                raise self.alias_load_error
            encoded = Path(params["path"]).read_bytes()
            if project_restore.hashlib.sha256(encoded).hexdigest() != params[
                "expected_sha256"
            ]:
                raise AssertionError("alias digest mismatch")
            table = json.loads(encoded.decode("utf-8"))
            aliases = table["aliases"]
            operation_id = params["operation_id"]
            self.alias_transaction = {
                "transactionId": operation_id,
                "captureId": table["captureId"],
                "captureSessionId": self.session_id,
                "manifestSha256": params["expected_sha256"],
                "state": "committed",
            }
            response = {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "captureId": table["captureId"],
                "captureSessionId": self.session_id,
                "operationId": operation_id,
                "transactionId": operation_id,
                "confirmedOnly": params["confirmed_only"],
                "strictAtomic": params["strict_atomic"],
                "committed": True,
                "recoveryRequired": False,
                "rollbackAvailable": True,
                "recoveryLedgerPublished": True,
                "counts": {
                    "manifestAliases": len(aliases),
                    "activeValidatedAliases": len(aliases),
                    "skipped": 0,
                    "conflicts": 0,
                },
            }
            if self.alias_load_error_after_commit is not None:
                raise self.alias_load_error_after_commit
            if self.alias_response_mutator is not None:
                self.alias_response_mutator(response)
            return response
        if method == "get_symbol_alias_transaction":
            transaction = self.alias_transaction
            if (
                transaction is not None
                and transaction["transactionId"] == params["transaction_id"]
            ):
                return {
                    "schema": "renderdoc-ultra-alias-transaction-status/v1",
                    "schemaVersion": 1,
                    "captureId": transaction["captureId"],
                    "captureSessionId": transaction["captureSessionId"],
                    "transactionId": transaction["transactionId"],
                    "state": transaction["state"],
                    "committed": transaction["state"] == "committed",
                    "mutationPresent": True,
                    "recoveryRequired": True,
                    "rollbackAvailable": True,
                    "manifestSha256": transaction["manifestSha256"],
                    "replayMutation": False,
                }
            return {
                "schema": "renderdoc-ultra-alias-transaction-status/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": params["expected_capture_session_id"],
                "transactionId": "",
                "state": "absent",
                "committed": False,
                "mutationPresent": False,
                "recoveryRequired": False,
                "rollbackAvailable": False,
                "manifestSha256": "",
                "replayMutation": False,
            }
        if method == "rollback_symbol_aliases":
            self.alias_rollback_calls += 1
            transaction_id = params["expected_transaction_id"]
            if (
                self.alias_transaction is not None
                and self.alias_transaction["transactionId"] != transaction_id
            ):
                raise AssertionError("alias rollback transaction mismatch")
            status = (
                "restored" if self.alias_transaction is not None else "already-restored"
            )
            self.alias_transaction = None
            receipt = {
                "schema": "renderdoc-ultra-alias-rollback/v1",
                "schemaVersion": 1,
                "captureId": params["expected_capture_id"],
                "captureSessionId": self.session_id,
                "transactionId": transaction_id,
                "status": status,
                "restored": True,
                "alreadyRestored": status == "already-restored",
                "verified": True,
                "strictReceipt": True,
                "captureTokenVerified": True,
                "originalNamesVerified": True,
                "physicalAtomic": False,
                "recoveryRequired": False,
                "overlayRestored": True,
                "renameLedgerRestored": True,
                "resourceCount": 1,
                "failures": [],
            }
            self.alias_last_rollback = copy.deepcopy(receipt)
            return receipt
        if method == "recover_shader_candidate_shared_resource_operation":
            self.shader_operation_recovery_calls += 1
            operation_id = params["operation_id"]
            outcome = self.shader_operations.get(operation_id)
            if outcome is None:
                status = "absent"
                replacement_id = ""
                known = False
                created = False
                rollback = None
            else:
                status = "recovered"
                replacement_id = outcome["replacementId"]
                known = True
                created = True
                rollback = {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": replacement_id,
                    "captureId": params["expected_capture_id"],
                    "captureSessionId": params["expected_capture_session_id"],
                    "workspaceId": params["workspace_id"],
                    "resourceScoped": True,
                    "eventScoped": False,
                    "cleared": True,
                    "originalShaderPreserved": True,
                    "targetFreed": True,
                }
                self.shader_operations.pop(operation_id, None)
            return {
                "schema": "renderdoc-ultra-shared-shader-operation-recovery/v1",
                "schemaVersion": 1,
                "operationId": operation_id,
                "captureId": params["expected_capture_id"],
                "captureSessionId": params["expected_capture_session_id"],
                "workspaceId": params["workspace_id"],
                "status": status,
                "operationKnown": known,
                "operationCreatedReplacement": created,
                "replacementId": replacement_id,
                "mutationPresent": False,
                "recovered": True,
                "originalShaderPreserved": True,
                "strictReceipt": True,
                "rollback": rollback,
            }
        raise AssertionError("unexpected IPC method: %s" % method)


class _Host:
    def __init__(
        self,
        active: dict,
        workspace_status: dict,
        capture_path: Path,
        session_id: str,
        index: dict,
        identity_evidence: dict,
    ):
        self.active_service = _ActiveService(active)
        self.workspace_service = _WorkspaceService(workspace_status)
        self.workspace_service.fallback_status = self._status_for_path
        self.ipc = _IPC(capture_path, session_id, index)
        self.ipc.instance_id = "test-instance"
        self.capture_path = capture_path
        self.session_id = session_id
        self.identity_evidence = identity_evidence
        self.scope_calls = 0
        self.projection_calls = 0
        self.apply_calls = 0
        self.prepare_calls = 0
        self.workspace_prepare_calls = 0
        self.workspace_rebind_calls = 0
        self.seed_calls = 0
        self.compile_calls = 0
        self.prepared_response: dict | None = None
        self.prepare_progress_events: list[dict] = []
        self.target_workspace: Path | None = None
        self.target_status: dict | None = None
        self.material_session: dict | None = None
        self.material_prepare_calls = 0
        self.material_update_calls = 0
        self.material_update_error: Exception | None = None
        self.material_rollback_calls = 0
        self.compile_error: Exception | None = None
        self.switch_project_on_compile = False
        self.switch_project_on_apply = False
        self.shader_rollback_calls = 0
        self.shader_rollback_error: Exception | None = None
        self.live_context_calls = 0
        self.switch_project_after_material_success = False
        self.switch_on_live_context_call: int | None = None
        self.require_alias_before_apply = False
        self.apply_response_mutator = None
        self.apply_error_after_commit: Exception | None = None
        self.apply_error_workspace_ids: set[str] = set()
        self.last_apply_response: dict | None = None
        self.edit_start_calls = []
        self.edit_start_error = None

    def _active_project_service(self):
        return self.active_service

    def _shader_workspace_service(self):
        return self.workspace_service

    def start_shader_workspace_live_edit(self, workspace_path):
        if self.active_service.pin_depth:
            raise AssertionError("editor start must run after the project pin is released")
        self.edit_start_calls.append(workspace_path)
        if self.edit_start_error:
            raise self.edit_start_error
        status = self._status_for_path(workspace_path)
        return json.dumps({
            "schema": "replaylab-shader-live-edit-mcp/v1", "schemaVersion": 1,
            "operation": "start", "appliedByThisCall": False,
            "backgroundAutoApplyAuthorized": True,
            "result": {"schema": "replaylab-shader-live-edit/v1", "state": "running",
                "startTiming": {"totalMs": 12.0, "phasesMs": {"capturePreflight": 8.0}, "failedPhase": ""},
                "workspacePath": workspace_path, "liveEditId": "a" * 32, "watchId": "b" * 32,
                "captureBinding": {"instanceId": "test-instance", "captureSessionId": self.session_id,
                                   "capturePath": str(self.capture_path)},
                "watch": {"state": "running", "caughtUp": True, "observedSourceSha256": status["source"]["currentSha256"]},
                "latestHandledRevision": status["latestCompiled"]["revision"],
                "latestHandledCandidateRawSha256": status["latestCompiled"]["rawSha256"]}})

    def list_shader_workspace_live_edits(self):
        return json.dumps({"schema": "replaylab-shader-live-edit-mcp/v1", "schemaVersion": 1,
            "operation": "list", "appliedByThisCall": False, "backgroundAutoApplyAuthorized": True,
            "result": {"sessions": []}})

    def _active_live_context(self, allow_missing_capture_id=False):
        self.live_context_calls += 1
        if self.live_context_calls == self.switch_on_live_context_call:
            self.active_service._status["workspaceId"] = "4" * 32
        return {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "restoreValidationPolicySha256": getattr(self, "restore_validation_policy_sha256", None),
            "capture": {
                "loaded": True,
                "path": str(self.capture_path),
                "captureSessionId": self.session_id,
                "captureId": "",
                "api": "D3D11",
            },
        }

    def _require_same_active_project(self, active):
        if active["workspaceId"] != self.active_service._status["workspaceId"]:
            raise AssertionError("active project changed")
        return copy.deepcopy(active)

    def _workspace_catalog_binding_projection(self, status):
        self.projection_calls += 1
        return {
            "workspaceId": status["workspaceId"],
            "captureId": status["capture"]["captureId"],
            "capturePath": status["capture"]["capturePath"],
            "api": status["capture"]["api"],
            "catalogPath": status["capture"]["catalogPath"],
            "catalogSha256": status["capture"]["catalogSha256"],
            "shaderKey": status["shader"]["shaderKey"],
            "stage": status["stage"],
            "entryPoint": status["entryPoint"],
            "resourceId": status["shader"]["resourceId"],
            "rawSha256": status["shader"]["rawSha256"],
        }

    def _workspace_catalog_rebind_scope(self, status):
        self.scope_calls += 1
        return {"identityEvidence": copy.deepcopy(self.identity_evidence)}

    def _canonical_json_sha256(self, value, label):
        return project_restore._canonical_sha256(value)

    def apply_shader_workspace_shared_resource(
        self,
        workspace_path,
        expected_revision,
        expected_candidate_raw_sha256,
        operation_id,
        expected_anchor_event_id=None,
    ):
        if self.require_alias_before_apply and not any(
            method == "load_symbol_aliases" for method, unused in self.ipc.calls
        ):
            raise AssertionError("Shader apply ran before the alias overlay was restored")
        self.apply_calls += 1
        status = self._status_for_path(workspace_path)
        if expected_anchor_event_id != getattr(self, "expected_anchors", {}).get(status["workspaceId"], 9):
            raise AssertionError(
                "restore did not pass its frozen representative EID to apply"
            )
        if status["workspaceId"] in self.apply_error_workspace_ids:
            raise RuntimeError("selected cached Shader apply failed")
        candidate_abi = getattr(self, "candidate_abi_sha256", status["baseline"]["abiSha256"])
        report_path = "revisions/%04d/abi-report.json" % expected_revision
        status["latestCompiled"]["abiGate"] = {
            "status": "compatible", "baselineAbiSha256": status["baseline"]["abiSha256"],
            "candidateAbiSha256": candidate_abi, "requestId": "1" * 64,
            "reportSha256": "3" * 64, "reportPath": report_path,
        }
        response = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "operationId": operation_id,
            "binding": {
                "workspaceId": status["workspaceId"],
                "captureId": status["capture"]["captureId"],
                "revision": expected_revision,
                "candidateRawSha256": expected_candidate_raw_sha256,
                "candidateByteSize": status["latestCompiled"]["byteSize"],
                "baselineAbiSha256": status["baseline"]["abiSha256"],
                "stage": status["stage"],
                "entryPoint": status["entryPoint"],
                "api": "d3d11",
                "encoding": "dxbc",
                "targetProfile": status["targetProfile"]["profile"],
            },
            "validation": {
                "workspacePath": str(Path(workspace_path).resolve()),
                "revision": expected_revision,
                "requestId": "1" * 64,
                "compatible": True,
                "baselineAbiSha256": status["baseline"]["abiSha256"],
                "candidateAbiSha256": candidate_abi,
                "differences": [],
                "differencesTruncated": False,
                "requiresReplayAbiValidation": False,
                "replayAbiValidated": True,
                "abiReportSha256": "3" * 64,
                "abiReportPath": str(Path(workspace_path).resolve() / report_path),
                "applied": False,
            },
            "replacement": {
                "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                "schemaVersion": 1,
                "captureId": status["capture"]["captureId"],
                "replacementId": "9" * 32,
                "operationId": operation_id,
                "operationCreatedReplacement": True,
                "workspaceId": status["workspaceId"],
                "captureSessionId": self.session_id,
                "revision": expected_revision,
                "anchorEventId": expected_anchor_event_id,
                "stage": status["stage"],
                "entryPoint": status["entryPoint"],
                "api": "d3d11",
                "encoding": "dxbc",
                "originalResourceId": status["shader"]["resourceId"],
                "originalRawSha256": status.get("baseline", {}).get(
                    "rawSha256", status["shader"]["rawSha256"]
                ),
                "candidateRawSha256": expected_candidate_raw_sha256,
                "physicalAbiSha256": status["baseline"]["abiSha256"],
                "abiRequestId": "1" * 64,
                "abiReportSha256": "3" * 64,
                "resourceScoped": True,
                "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "configured": True,
                "originalShaderPreserved": True,
                "singleOwner": True,
            },
            "scope": "captured-resource",
            "applied": True,
            "rollback": {
                "tool": "rollback_shader_workspace_shared_resource",
                "workspacePath": str(Path(workspace_path).resolve()),
                "replacementId": "9" * 32,
            },
        }
        self.ipc.shader_operations[operation_id] = {
            "replacementId": "9" * 32,
            "workspaceId": status["workspaceId"],
        }
        if self.apply_error_after_commit is not None:
            raise self.apply_error_after_commit
        if self.apply_response_mutator is not None:
            self.apply_response_mutator(response)
        self.last_apply_response = copy.deepcopy(response)
        if self.switch_project_on_apply:
            self.active_service._status["workspaceId"] = "4" * 32
        return json.dumps(response)

    def _status_for_path(self, workspace_path):
        if (
            self.target_workspace is not None
            and Path(workspace_path).resolve() == self.target_workspace.resolve()
            and self.target_status is not None
        ):
            return self.target_status
        if Path(workspace_path).resolve() == Path(
            self.workspace_service._status["workspacePath"]
        ).resolve():
            return self.workspace_service._status
        for status in self.workspace_service.extra_statuses:
            if Path(workspace_path).resolve() == Path(status["workspacePath"]).resolve():
                return status
        raise AssertionError("unexpected workspace path")

    def _prepare_active_capture_project(self, unused_progress_token=None):
        self.prepare_calls += 1
        if self.prepared_response is None:
            return json.dumps({"error": "fallback deliberately stopped by test"})
        return json.dumps(copy.deepcopy(self.prepared_response))

    def _prepare_active_capture_project_with_progress(
        self, progress_token, callback
    ):
        if len(progress_token) != 32:
            raise AssertionError("unexpected restore progress token")
        for event in self.prepare_progress_events:
            callback(copy.deepcopy(event))
        return self._prepare_active_capture_project(progress_token)

    def prepare_active_capture_project(self):
        return self._prepare_active_capture_project()

    def get_active_shader_workspace_path(self, event_id, stage, **expected):
        if self.target_workspace is None or self.target_status is None:
            raise AssertionError("reuse target was not configured")
        if expected != {
            "expected_resource_id": self.target_status["shader"]["resourceId"],
            "expected_entry_point": self.target_status["entryPoint"],
            "expected_raw_sha256": self.target_status["shader"]["rawSha256"],
            "expected_capture_session_id": self.session_id,
        }:
            raise AssertionError("restore omitted its complete frozen Shader identity")
        return json.dumps(
            {
                "schema": "replaylab-active-shader-workspace-path/v1",
                "schemaVersion": 1,
                "captureId": self.target_status["capture"]["captureId"],
                "captureSessionId": self.session_id,
                "eventId": event_id,
                "stage": stage,
                "entryPoint": self.target_status["entryPoint"],
                "shaderResourceId": self.target_status["shader"]["resourceId"],
                "rawSha256": self.target_status["shader"]["rawSha256"],
                "catalogSha256": self.target_status["capture"]["catalogSha256"],
                "workspacePath": str(self.target_workspace),
                "applied": False,
            }
        )

    def prepare_shader_workspace(
        self,
        event_id,
        stage,
        workspace_path,
        *,
        expected_resource_id=None,
        expected_entry_point="",
        expected_raw_sha256="",
        expected_capture_id="",
        expected_capture_session_id="",
        admit_existing=False,
    ):
        if self.target_workspace is None or Path(workspace_path).resolve() != self.target_workspace:
            raise AssertionError("unexpected reuse workspace")
        self.workspace_prepare_calls += 1
        existed = self.target_workspace.exists()
        if admit_existing:
            self.workspace_rebind_calls += 1
        selected_keys = [
            shader_key
            for selected_event_id, shader_key in self.identity_evidence["useTuples"]
            if selected_event_id == event_id
        ]
        status = self.target_status
        if (
            existed is not admit_existing
            or status is None
            or selected_keys != [status["shader"]["shaderKey"]]
            or stage != status["stage"]
            or expected_resource_id != status["shader"]["resourceId"]
            or expected_entry_point != status["entryPoint"]
            or expected_raw_sha256 != status["shader"]["rawSha256"]
            or expected_capture_id != status["capture"]["captureId"]
            or expected_capture_session_id != self.session_id
        ):
            return json.dumps(
                {"error": "workspace does not match the frozen representative EID"}
            )
        if not existed:
            self.target_workspace.mkdir(parents=True, exist_ok=False)
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "prepare",
                "result": {
                    "workspacePath": str(self.target_workspace),
                    "binding": {
                        "workspacePath": str(self.target_workspace),
                        "workspaceId": status["workspaceId"],
                        "captureId": status["capture"]["captureId"],
                        "captureSessionId": self.session_id,
                        "eventId": event_id,
                        "stage": status["stage"],
                        "entryPoint": status["entryPoint"],
                        "originalResourceId": status["shader"]["resourceId"],
                        "originalRawSha256": status["shader"]["rawSha256"],
                    },
                    "created": {
                        "admittedExisting": existed,
                        "sourcePreserved": existed,
                    },
                },
            }
        )

    def get_shader_workspace_status(self, workspace_path):
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "status",
                "result": copy.deepcopy(self._status_for_path(workspace_path)),
            }
        )

    def seed_active_shader_workspace(self, knowledge_id, workspace_path):
        status = self._status_for_path(workspace_path)
        self.seed_calls += 1
        status["source"].update(
            {
                "currentSha256": "0" * 64,
                "modifiedSinceDecompile": True,
            }
        )
        return json.dumps(
            {
                "schema": "replaylab-active-workspace-seed/v1",
                "schemaVersion": 1,
                "captureId": status["capture"]["captureId"],
                "captureSessionId": self.session_id,
                "workspacePath": str(Path(workspace_path).resolve()),
                "knowledgeId": knowledge_id,
                "applied": False,
            }
        )

    def compile_shader_workspace(self, workspace_path):
        status = self._status_for_path(workspace_path)
        self.compile_calls += 1
        if self.compile_error is not None:
            raise self.compile_error
        status["revision"] = getattr(self, "compile_revision", 1)
        status["latestCompiled"] = {
            "revision": status["revision"],
            "sourceSha256": status["source"]["currentSha256"],
            "rawSha256": "f" * 64,
            "byteSize": 4,
            "kind": "DXBC",
        }
        if self.switch_project_on_compile:
            if self.active_service.pin_depth:
                self.active_service.blocked_switches += 1
            else:
                self.active_service._status["workspaceId"] = "4" * 32
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "compile",
                "result": copy.deepcopy(status),
            }
        )

    def prepare_material_session(self, *unused_args):
        self.material_prepare_calls += 1
        if self.material_session is None:
            raise AssertionError("material reuse was not configured")
        return json.dumps(copy.deepcopy(self.material_session))

    def update_material_parameters(self, session_id, updates):
        if self.material_session is None or session_id != self.material_session["sessionId"]:
            raise AssertionError("unexpected material session")
        self.material_update_calls += 1
        if self.material_update_error is not None:
            raise self.material_update_error
        result = copy.deepcopy(self.material_session)
        result.update(
            {
                "operation": "update-many",
                "updatedParameterIds": [item["parameter"] for item in updates],
                "updatedParameterCount": len(updates),
                "applied": True,
            }
        )
        if self.switch_project_after_material_success:
            # _restore_material performs one immediate context validation after
            # this update. Switch on the following caller-level validation so
            # the successful session enters the global rollback ledger first.
            self.switch_on_live_context_call = self.live_context_calls + 2
        return json.dumps(result)

    def rollback_material_session(self, session_id):
        self.material_rollback_calls += 1
        session = self.material_session or {
            "captureId": "3" * 64,
            "captureSessionId": self.session_id,
        }
        return json.dumps(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "schemaVersion": 1,
                "operation": "rollback",
                "sessionId": session_id,
                "captureId": session["captureId"],
                "captureSessionId": session["captureSessionId"],
                "applied": False,
                "snapshotReleased": True,
            }
        )

    def rollback_shader_workspace_shared_resource(
        self, workspace_path, replacement_id
    ):
        self.shader_rollback_calls += 1
        if self.shader_rollback_error is not None:
            raise self.shader_rollback_error
        status = self._status_for_path(workspace_path)
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
                "operation": "rollback-shared-resource",
                "workspaceId": status["workspaceId"],
                "captureId": status["capture"]["captureId"],
                "replacement": {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": replacement_id,
                    "captureId": status["capture"]["captureId"],
                    "captureSessionId": self.session_id,
                    "workspaceId": status["workspaceId"],
                    "resourceScoped": True,
                    "eventScoped": False,
                    "cleared": True,
                    "originalShaderPreserved": True,
                    "targetFreed": True,
                },
                "applied": False,
            }
        )


class ProjectRestoreTests(unittest.TestCase):
    def test_internal_prepare_expands_exact_spill_before_schema_validation(self):
        output_root = self.root / "tool-output"
        exact = {
            "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
            "schemaVersion": 1,
            "operation": "prepare",
            "result": {
                "workspaceId": "a" * 32,
                "rows": [
                    {"index": index, "source": "x" * 200}
                    for index in range(200)
                ],
            },
        }
        with mock.patch.object(
            server, "_asset_tool_output_root", return_value=output_root
        ), mock.patch.object(server, "MAX_AGENT_RESULT_BYTES", 700):
            preview_text = server._ok_compact(exact)
            preview = json.loads(preview_text)
            self.assertIn("completeContent", preview)
            # The compact preview may keep the wrapper schema. The exact spill
            # must still supply the complete rows used by restore.
            self.assertEqual(preview.get("schema"), exact["schema"])
            restored = project_restore._tool_json(
                preview_text,
                "prepare target Shader workspace",
                schema="renderdoc-ultra-shader-workspace-mcp/v1",
                schema_version=1,
            )

        self.assertEqual(restored, exact)

    def test_internal_prepare_rejects_changed_exact_spill(self):
        output_root = self.root / "tool-output"
        exact = {
            "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
            "schemaVersion": 1,
            "operation": "prepare",
            "result": {"source": "x" * 20000},
        }
        with mock.patch.object(
            server, "_asset_tool_output_root", return_value=output_root
        ), mock.patch.object(server, "MAX_AGENT_RESULT_BYTES", 700):
            preview_text = server._ok_compact(exact)
            pointer = json.loads(preview_text)["completeContent"]
            Path(pointer["path"]).write_bytes(b"{}")
            with self.assertRaisesRegex(RuntimeError, "content changed"):
                project_restore._tool_json(
                    preview_text,
                    "prepare target Shader workspace",
                    schema="renderdoc-ultra-shader-workspace-mcp/v1",
                    schema_version=1,
                )

    def test_product_target_lookup_batches_only_project_original_hashes(self):
        targets = sorted({self.raw_sha, *("%064x" % index for index in range(1, 18))})
        with mock.patch.object(_ReplayService, "restorable_shader_raw_hashes", return_value=targets):
            result, host, unused_replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertTrue(result["completed"], result)
        begins = [params for method, params in host.ipc.calls if method == "begin_rdc_project_shader_resource_index"]
        self.assertEqual(len(begins), 2)
        self.assertEqual([digest for params in begins for digest in params["target_raw_sha256s"]], targets)
        self.assertTrue(all(0 < len(params["target_raw_sha256s"]) <= 16 for params in begins))
        self.assertEqual(result["targetLookup"]["observedTargetRawSha256s"], [self.raw_sha])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertFalse(result["fullFrame"])

    def test_product_target_no_match_is_bounded_and_does_not_prepare_source(self):
        with mock.patch.object(_ReplayService, "restorable_shader_raw_hashes", return_value=["0" * 64]):
            result, host, unused_replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["cache"]["reason"], "project-targets-not-present")
        self.assertEqual(result["targetLookup"]["unobservedTargetRawSha256s"], ["0" * 64])
        self.assertFalse(result["fullFrame"])
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(host.apply_calls, 0)

    def test_committed_restore_starts_current_editor_after_releasing_project_pin(self):
        result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["editing"]["enabled"], 1, result["editing"])
        self.assertEqual(host.edit_start_calls, [str(self.workspace)])
        self.assertEqual(result["editing"]["items"][0]["captureBinding"]["captureSessionId"], self.session_id)
        self.assertEqual(result["editing"]["items"][0]["startTiming"]["phasesMs"]["capturePreflight"], 8.0)

    def test_committed_restore_returns_before_editor_start_and_keeps_bound_owner(self):
        callbacks = []
        result, host, unused_replay, unused_progress = self._run(post_restore_callback=callbacks.append)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["editing"]["status"], "pending")
        self.assertEqual(result["editing"]["pending"], 1)
        self.assertEqual(host.edit_start_calls, [])
        self.assertEqual(len(callbacks), 1)
        editing = callbacks[0]()
        self.assertEqual(editing["enabled"], 1, editing)
        self.assertEqual(host.edit_start_calls, [str(self.workspace)])
        self.assertEqual(editing["items"][0]["captureBinding"]["captureSessionId"], self.session_id)

    def test_no_applied_shaders_do_not_publish_a_fictitious_editor_startup(self):
        callbacks = []
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True,
            compile_error=RuntimeError("compiler unavailable"), post_restore_callback=callbacks.append)
        self.assertEqual(result["shaders"]["applied"], 0, result)
        self.assertEqual(result["editing"], {"attempted": 0, "enabled": 0, "deferred": 0, "failed": 0, "items": []})
        self.assertEqual(callbacks, [])
        self.assertEqual(host.edit_start_calls, [])

    def test_legacy_display_aliases_restore_without_project_alias_records(self):
        from src.shader_workspace import alias_display_mirror
        receipt = {"status": "restored", "writtenRows": 8, "writtenBlocks": 2, "skipped": []}
        with mock.patch.object(alias_display_mirror, "restore_display_alias_mirrors", return_value=receipt) as mirror:
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["editing"]["enabled"], 1)
        self.assertEqual(result["editing"]["items"][0]["displayAliases"], receipt)
        mirror.assert_called_once()
        self.assertEqual(mirror.call_args.args[0], host)
        self.assertEqual(mirror.call_args.args[1], self.workspace)
        self.assertEqual(mirror.call_args.args[3:], (self.session_id, self.project))

    def test_display_alias_backfill_failure_keeps_valid_shader_and_editor(self):
        from src.shader_workspace import alias_display_mirror
        with mock.patch.object(alias_display_mirror, "restore_display_alias_mirrors",
                               side_effect=RuntimeError("alias file unavailable")):
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["editing"]["enabled"], 1)
        self.assertEqual(result["editing"]["items"][0]["displayAliases"],
                         {"status": "failed", "diagnostic": "alias file unavailable"})
        self.assertEqual(host.shader_rollback_calls, 0)

    def _open_source_only_draft(self):
        from src.shader_workspace.organization import ORGANIZATION_GATE_SCHEMA, ORGANIZATION_SESSION_SCHEMA
        organization_id = "d" * 32
        directory = self.workspace / "ai" / "organization-sessions" / organization_id
        directory.mkdir(parents=True, exist_ok=True)
        session = directory / "session.json"
        session.write_text(json.dumps({"schema": ORGANIZATION_SESSION_SCHEMA,
            "organizationId": organization_id, "state": "drafting"}), encoding="utf-8")
        proposal = directory / "proposal.hlsl"
        proposal.write_text("// unfinished durable draft\nfloat4 MainPS() { return 1; }\n", encoding="utf-8")
        gate = self.workspace / "ai" / "organize-code-gate.json"
        gate.write_text(json.dumps({"schema": ORGANIZATION_GATE_SCHEMA,
            "organizationId": organization_id, "decision": "editable-draft"}), encoding="utf-8")
        return organization_id, {path: path.read_bytes() for path in (session, proposal, gate)}

    def test_active_draft_restores_applied_shader_without_starting_automatic_editor(self):
        organization_id, before = self._open_source_only_draft()
        with mock.patch.object(_Host, "list_shader_workspace_live_edits",
                               side_effect=AssertionError("draft must not reconcile automatic editors")), \
                mock.patch.object(_Host, "start_shader_workspace_live_edit",
                                  side_effect=AssertionError("draft must not start watcher")):
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["editing"]["enabled"], 0)
        self.assertEqual(result["editing"]["failed"], 0)
        self.assertEqual(result["editing"]["deferred"], 1)
        item = result["editing"]["items"][0]
        self.assertEqual(item["organizationId"], organization_id)
        self.assertEqual(item["reason"], "organization-draft-open")
        self.assertEqual(item["editingMode"], "source-only")
        self.assertIs(item["readOnly"], True)
        self.assertIs(item["backgroundAutoApplyAuthorized"], False)
        self.assertNotIn("liveEditId", item)
        self.assertEqual(host.edit_start_calls, [])
        self.assertEqual(host.shader_rollback_calls, 0)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_deferred_draft_does_not_hide_restored_source_identity_failure(self):
        result, host, unused_replay, unused_progress = self._run()
        self._open_source_only_draft()
        status = host._shader_workspace_service().status(str(self.workspace))
        with mock.patch.object(host._shader_workspace_service(), "status", return_value={
                **status, "source": {**status["source"], "currentSha256": "e" * 64}}), \
                mock.patch.object(host, "start_shader_workspace_live_edit") as start:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["failed"], 1, editing)
        self.assertEqual(editing["deferred"], 0)
        start.assert_not_called()

    def test_deferred_draft_admission_is_revoked_when_capture_changes_or_restore_cancels(self):
        from src.shader_workspace import organization
        result, host, unused_replay, unused_progress = self._run()
        self._open_source_only_draft()
        reader = organization.active_draft_id
        for cause in ("capture", "cancel"):
            with self.subTest(cause=cause):
                cancel = threading.Event()
                def read_draft(path):
                    value = reader(path)
                    if cause == "cancel":
                        cancel.set()
                    return value
                context_check = mock.Mock(side_effect=(
                    [None, RuntimeError("capture changed after draft admission")]
                    if cause == "capture" else None))
                with mock.patch.object(organization, "active_draft_id", side_effect=read_draft), \
                        mock.patch.object(project_restore, "_require_restore_context", context_check), \
                        mock.patch.object(host, "start_shader_workspace_live_edit") as start:
                    editing = project_restore._enable_restored_shader_editing(
                        host, self.active, self.project, self.capture_path, self.session_id,
                        self.binding["shaders"], result["shaders"]["items"], cancel_event=cancel)
                self.assertEqual(editing["deferred"], 0, editing)
                self.assertEqual(editing["enabled"], 0)
                self.assertEqual(editing["failed"], 1)
                self.assertEqual(editing["items"][0]["status"], "failed")
                self.assertNotIn("organizationId", editing["items"][0])
                start.assert_not_called()

    def test_restore_identity_timeout_recovers_on_bound_owner_through_editor_start(self):
        from src.ipc_client import IPCBridgeTimeoutError, _CALL_BINDING, bind_direct_tool_call
        observed = []

        def configure(host, replay):
            host.ipc.bind_capture_session = lambda session: bind_direct_tool_call("test-instance", session)
            host.ipc.bound_instance_state = lambda: {"instanceId": "test-instance", "alive": True}
            original = host._active_live_context
            reads = 0

            def identity(**kwargs):
                nonlocal reads
                reads += 1
                observed.append(("identity", _CALL_BINDING.get()))
                if reads == 2:
                    raise IPCBridgeTimeoutError("Timeout (60s)")
                return original(**kwargs)

            host._active_live_context = identity
            start = host.start_shader_workspace_live_edit

            def editor(workspace):
                observed.append(("editor", _CALL_BINDING.get()))
                return start(workspace)

            host.start_shader_workspace_live_edit = editor

        result, host, unused_replay, unused_progress = self._run(host_mutator=configure)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["editing"]["enabled"], 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(len(result["contextRecovery"]), 1)
        self.assertTrue(result["contextRecovery"][0]["recovered"])
        self.assertIn(("editor", ("test-instance", self.session_id)), observed)
        self.assertTrue(all(binding == ("test-instance", self.session_id) for unused, binding in observed))
        self.assertIsNone(_CALL_BINDING.get())

    def test_target_workspace_yield_resumes_inside_restore_and_enables_editor(self):
        from src.ipc_client import IPCClient, IPCBridgeResponseError, bind_direct_tool_call
        sends = []

        def configure(host, replay):
            host.ipc.bind_capture_session = lambda session: bind_direct_tool_call("test-instance", session)
            locate = host.get_active_shader_workspace_path
            client = IPCClient()

            def send(method, params, timeout, *, deadline):
                sends.append((method, deadline))
                if len(sends) == 1:
                    raise IPCBridgeResponseError(-32010,
                        "Bread background tool yielded after a user replay operation; retry it if still needed")
                return locate(params["event_id"], params["stage"], **params["expected"])

            client._call_locked = send
            host.get_active_shader_workspace_path = lambda event_id, stage, **expected: client.call(
                "resolve_target", {"event_id": event_id, "stage": stage, "expected": expected})

        with mock.patch("src.ipc_client.time.sleep"):
            result, host, unused_replay, unused_progress = self._run(
                index_present=False, auto_prepare=True, host_mutator=configure)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(result["shaders"]["failed"], 0, result)
        self.assertEqual(result["editing"]["enabled"], 1, result)
        self.assertGreaterEqual(len(sends), 2)
        self.assertEqual(sends[0], sends[1])

    def test_identity_unavailable_after_apply_rolls_back_on_same_owner_without_inventory_fallback(self):
        from src.ipc_client import IPCBridgeTimeoutError, _CALL_BINDING, bind_direct_tool_call
        observed_rollbacks = []
        original_index = copy.deepcopy(self.index)

        def configure(host, replay):
            host.ipc.bind_capture_session = lambda session: bind_direct_tool_call("test-instance", session)
            host.ipc.bound_instance_state = lambda: {"instanceId": "test-instance", "alive": True}
            original = host._active_live_context

            def identity(**kwargs):
                if host.apply_calls:
                    raise IPCBridgeTimeoutError("Timeout (60s)")
                return original(**kwargs)

            host._active_live_context = identity
            rollback = host.rollback_shader_workspace_shared_resource

            def recorded_rollback(*args, **kwargs):
                observed_rollbacks.append(_CALL_BINDING.get())
                return rollback(*args, **kwargs)

            host.rollback_shader_workspace_shared_resource = recorded_rollback

        result, host, unused_replay, progress = self._run(host_mutator=configure)
        self.assertFalse(result["completed"], result)
        self.assertEqual(result["errorType"], "RestoreContextUnavailable")
        self.assertIn("IPCBridgeTimeoutError: Timeout (60s)", result["error"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(observed_rollbacks, [("test-instance", self.session_id)])
        self.assertEqual(host.prepare_calls, 0)
        self.assertNotIn("resource-inventory", [event["phase"] for event in progress])
        self.assertEqual(host.ipc.index, original_index)
        self.assertIsNone(_CALL_BINDING.get())

    def test_restored_editor_polls_starting_on_one_bound_gui_even_if_global_selection_changes(self):
        from src.ipc_client import _CALL_BINDING
        original_start = _Host.start_shader_workspace_live_edit
        polls = []
        def start(host, workspace):
            self.assertEqual(_CALL_BINDING.get(), ("test-instance", self.session_id))
            response = json.loads(original_start(host, workspace))
            host.ready_editor_response = copy.deepcopy(response)
            host.ipc.instance_id = "other-active-gui"
            response["result"]["state"] = "starting"
            response["result"]["watch"]["state"] = "starting"
            return json.dumps(response)
        def poll(host, live_id):
            polls.append((_CALL_BINDING.get(), live_id))
            response = copy.deepcopy(host.ready_editor_response)
            response["operation"] = "poll"
            return json.dumps(response)
        with mock.patch.object(_Host, "start_shader_workspace_live_edit", start), mock.patch.object(
                _Host, "poll_shader_workspace_live_edit", poll, create=True):
            result, unused_host, unused_replay, unused_progress = self._run()
        self.assertEqual(result["editing"]["enabled"], 1, result["editing"])
        self.assertEqual(polls, [(("test-instance", self.session_id), "a" * 32)])
        self.assertIsNone(_CALL_BINDING.get())

    def test_wrong_candidate_gui_is_rejected_inside_binding_before_editor_start(self):
        from src.ipc_client import _CALL_BINDING
        result, host, unused_replay, unused_progress = self._run()
        host.edit_start_calls.clear()
        host.ipc.instance_id = "wrong-gui"
        original_context = host._active_live_context
        def context(**kwargs):
            value = original_context(**kwargs)
            if _CALL_BINDING.get() == ("wrong-gui", self.session_id):
                value["capture"]["captureSessionId"] = "e" * 32
            return value
        host._active_live_context = context
        editing = project_restore._enable_restored_shader_editing(
            host, self.active, self.project, self.capture_path, self.session_id,
            self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["failed"], 1)
        self.assertEqual(host.edit_start_calls, [])

    def test_owner_resumed_editor_before_restore_is_reused_without_new_grant_or_revision_gate(self):
        from src.ipc_client import _CALL_BINDING
        result, host, unused_replay, unused_progress = self._run()
        resumed = json.loads(host.start_shader_workspace_live_edit(str(self.workspace)))
        # The owner resumed before restore recompiled/applied the same source.
        # Its watcher cursor remains older; it is not evidence for that apply.
        resumed["result"]["latestHandledRevision"] = 0
        resumed["result"]["latestHandledCandidateRawSha256"] = "1" * 64
        host.edit_start_calls.clear()
        listing = json.loads(host.list_shader_workspace_live_edits())
        listing["result"]["sessions"] = [resumed["result"]]
        polls = []
        def poll(live_id):
            polls.append((_CALL_BINDING.get(), live_id))
            value = copy.deepcopy(resumed); value["operation"] = "poll"
            return json.dumps(value)
        with mock.patch.object(host, "list_shader_workspace_live_edits", return_value=json.dumps(listing)), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", poll, create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit") as start, \
                mock.patch.object(project_restore, "_retire_restored_editor_job") as retire:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["enabled"], 1, editing)
        self.assertEqual(polls, [(("test-instance", self.session_id), "a" * 32)])
        start.assert_not_called(); retire.assert_not_called()

    def test_existing_editor_that_fails_poll_is_not_retired_or_reauthorized(self):
        result, host, unused_replay, unused_progress = self._run()
        resumed = json.loads(host.start_shader_workspace_live_edit(str(self.workspace)))
        listing = json.loads(host.list_shader_workspace_live_edits())
        listing["result"]["sessions"] = [resumed["result"]]
        with mock.patch.object(host, "list_shader_workspace_live_edits", return_value=json.dumps(listing)), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", side_effect=RuntimeError("owner unavailable"), create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit") as start, \
                mock.patch.object(project_restore, "_retire_restored_editor_job") as retire:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["failed"], 1)
        start.assert_not_called(); retire.assert_not_called()

    def test_owner_resume_between_list_and_start_is_reused_with_one_start_attempt(self):
        result, host, unused_replay, unused_progress = self._run()
        resumed = json.loads(host.start_shader_workspace_live_edit(str(self.workspace)))
        empty = json.loads(host.list_shader_workspace_live_edits())
        listing = copy.deepcopy(empty); listing["result"]["sessions"] = [resumed["result"]]
        resumed["operation"] = "poll"
        with mock.patch.object(host, "list_shader_workspace_live_edits", side_effect=[json.dumps(empty), json.dumps(listing)]), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", return_value=json.dumps(resumed), create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit", side_effect=RuntimeError("already active")) as start:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id,
                self.binding["shaders"], result["shaders"]["items"])
        self.assertEqual(editing["enabled"], 1, editing)
        self.assertEqual(start.call_count, 1)

    def test_exact_editor_retirement_preserves_durable_consent_and_other_scope(self):
        from src import internal_tool_service
        binding = {"instanceId": "test-instance", "captureSessionId": self.session_id,
                   "capturePath": str(self.capture_path)}
        stopped = []
        job = types.SimpleNamespace(workspace=self.workspace, session=types.SimpleNamespace(binding=binding),
                                    stop=lambda:stopped.append("a" * 32))
        key = project_restore._path_key(self.workspace)
        manager = types.SimpleNamespace(_lifecycle_lock=threading.RLock(), _lock=threading.RLock(),
            _jobs={"a" * 32:job}, _active_by_workspace={key:"a" * 32}, _registry=mock.Mock())
        with mock.patch.object(internal_tool_service, "_SHADER_LIVE_EDIT_MANAGER", manager):
            wrong = dict(binding, captureSessionId="e" * 32)
            self.assertIn("not confirmed", project_restore._retire_restored_editor_job("a" * 32, self.workspace, wrong))
            self.assertEqual(stopped, [])
            self.assertIn("retired", project_restore._retire_restored_editor_job("a" * 32, self.workspace, binding))
        self.assertEqual(stopped, ["a" * 32])
        self.assertEqual(manager._active_by_workspace, {})
        manager._registry.revoke.assert_not_called()

    def test_post_start_failed_watch_retires_only_returned_owned_job(self):
        original_start = _Host.start_shader_workspace_live_edit
        def start(host, workspace):
            response = json.loads(original_start(host, workspace))
            response["result"]["watch"]["state"] = "failed"
            return json.dumps(response)
        with mock.patch.object(_Host, "start_shader_workspace_live_edit", start), mock.patch.object(
                project_restore, "_retire_restored_editor_job", return_value="retired") as retire:
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["editing"]["failed"], 1)
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertEqual(retire.call_args.args[0], "a" * 32)

    def _two_restored_editors(self):
        restored, host, unused, unused_progress = self._run()
        other_workspace = self.shader_root / "editor-second"
        other_workspace.mkdir()
        other_status = copy.deepcopy(self.workspace_status)
        other_status.update(workspacePath=str(other_workspace), workspaceId="e" * 32)
        host.workspace_service.extra_statuses.append(other_status)
        records = [copy.deepcopy(self.shader_record), copy.deepcopy(self.shader_record)]
        records[1]["managedWorkspace"] = {
            "workspaceId": "e" * 32, "path": other_workspace.relative_to(self.project).as_posix()}
        items = [copy.deepcopy(restored["shaders"]["items"][0]) for unused in range(2)]
        items[1].update(workspaceId="e" * 32, workspacePath=str(other_workspace))
        listing = json.loads(host.list_shader_workspace_live_edits())
        for index, workspace in enumerate((self.workspace, other_workspace)):
            snapshot = json.loads(host.start_shader_workspace_live_edit(str(workspace)))["result"]
            snapshot.update(sessionState="current", liveEditId=("a" if index == 0 else "c") * 32)
            listing["result"]["sessions"].append(snapshot)
        host.edit_start_calls.clear()
        return host, records, items, listing

    def test_editor_batch_reads_one_list_and_reuses_ready_current_owner_snapshots(self):
        host, records, items, listing = self._two_restored_editors()
        with mock.patch.object(host, "list_shader_workspace_live_edits", return_value=json.dumps(listing)) as read_list, \
                mock.patch.object(host, "get_shader_workspace_status", side_effect=AssertionError("asset/organization status must not run")), \
                mock.patch.object(host, "poll_shader_workspace_live_edit", side_effect=AssertionError("ready owner already returned a snapshot"), create=True), \
                mock.patch.object(host, "start_shader_workspace_live_edit", side_effect=AssertionError("existing owners must not be started")), \
                mock.patch.object(project_restore, "_require_restore_context", wraps=project_restore._require_restore_context) as contexts:
            editing = project_restore._enable_restored_shader_editing(
                host, self.active, self.project, self.capture_path, self.session_id, records, items)
        self.assertEqual(editing["enabled"], 2, editing)
        self.assertEqual(read_list.call_count, 1)
        self.assertEqual(contexts.call_count, 2)
        self.assertEqual([item["liveEditId"] for item in editing["items"]], ["a" * 32, "c" * 32])

    def test_editor_batch_final_context_change_retires_only_new_jobs(self):
        host, records, items, listing = self._two_restored_editors()
        frozen_active = copy.deepcopy(self.active)
        listing["result"]["sessions"] = listing["result"]["sessions"][:1]
        original_start = host.start_shader_workspace_live_edit
        def change_after_start(workspace):
            response = json.loads(original_start(workspace))
            response["result"]["liveEditId"] = "c" * 32
            host.active_service._status["workspaceId"] = "4" * 32
            return json.dumps(response)
        with mock.patch.object(host, "list_shader_workspace_live_edits", return_value=json.dumps(listing)), \
                mock.patch.object(host, "start_shader_workspace_live_edit", side_effect=change_after_start), \
                mock.patch.object(project_restore, "_retire_restored_editor_job", return_value="retired") as retire:
            editing = project_restore._enable_restored_shader_editing(
                host, frozen_active, self.project, self.capture_path, self.session_id, records, items)
        self.assertEqual(editing["enabled"], 0, editing)
        self.assertEqual(editing["failed"], 2, editing)
        self.assertEqual([item["workspaceId"] for item in editing["items"]],
                         [self.managed_workspace_id, "e" * 32])
        self.assertEqual(retire.call_count, 1)
        self.assertEqual(retire.call_args.args[0], "c" * 32)

    def test_product_restore_results_pass_the_real_embedded_client_contract(self):
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("product_restore_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        for target_mode in (False, True):
            with self.subTest(target_mode=target_mode):
                result, unused_host, unused_replay, unused_progress = self._run(
                    index_present=not target_mode, auto_prepare=target_mode)
                accepted = client.ReplayLabRestoreHostClient._result(
                    result, str(self.project), str(self.capture_path), self.session_id)
                self.assertEqual(accepted["editing"]["enabled"], 1, accepted["editing"])
        with mock.patch.object(_ReplayService, "restorable_shader_raw_hashes", return_value=["0" * 64]):
            result, unused_host, unused_replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertTrue(client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)["completed"])

    def test_editor_failure_preserves_committed_replacement_and_reports_separate_failure(self):
        timing = {"totalMs": 12.0, "phasesMs": {"capturePreflight": 8.0}, "failedPhase": "capturePreflight"}
        failed = json.dumps({"error": "editor unavailable", "data": {"startTiming": timing}})
        with mock.patch.object(_Host, "start_shader_workspace_live_edit", return_value=failed):
            result, host, unused_replay, unused_progress = self._run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertEqual(result["editing"]["failed"], 1)
        self.assertIn("editor unavailable", result["editing"]["items"][0]["error"])
        self.assertEqual(result["editing"]["items"][0]["startTiming"], timing)

    def test_editor_start_rejects_changed_source_and_capture_without_reusing_old_grant(self):
        result, host, unused_replay, unused_progress = self._run()
        applied = result["shaders"]["items"]
        host.edit_start_calls.clear()
        for mutation in ("source", "resource", "capture", "cancel"):
            with self.subTest(mutation=mutation):
                host.edit_start_calls.clear()
                original = copy.deepcopy(host.workspace_service._status)
                event = threading.Event()
                if mutation == "source":
                    host.workspace_service._status["source"]["currentSha256"] = "9" * 64
                elif mutation == "resource":
                    host.workspace_service._status["shader"]["resourceId"] += 1
                elif mutation == "capture":
                    host.session_id = "e" * 32
                else:
                    event.set()
                editing = project_restore._enable_restored_shader_editing(
                    host, self.active, self.project, self.capture_path, self.session_id,
                    self.binding["shaders"], applied, cancel_event=event)
                self.assertEqual(editing["failed"], 1)
                self.assertEqual(editing["enabled"], 0)
                self.assertEqual(host.edit_start_calls, [])
                host.workspace_service._status = original
                host.session_id = self.session_id

    def setUp(self) -> None:
        project_restore._MATERIAL_SYNC_PARAMETERS.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.replay_path = self.project / ".replaylab" / "replay-project"
        self.shader_root = self.project / "workspaces" / "shaders"
        self.workspace = self.shader_root / "cached"
        self.workspace.mkdir(parents=True)
        self.replay_path.mkdir(parents=True)
        self.capture_path = self.root / "frame.rdc"
        self.capture_path.write_bytes(b"rdc")
        self.catalog_path = self.project / "captures" / "catalog.json"
        self.catalog_path.parent.mkdir(parents=True)

        self.project_id = "1" * 32
        self.active_workspace_id = "2" * 32
        self.capture_id = "3" * 64
        self.knowledge_sha = "5" * 64
        self.scope_sha = "6" * 64
        self.matcher_sha = "7" * 64
        self.managed_workspace_id = "8" * 32
        self.knowledge_id = "a" * 32
        self.raw_sha = "b" * 64
        self.code_sha = "c" * 64
        self.candidate_sha = "f" * 64
        self.live_abi_sha = "f" * 64
        self.source_sha = "0" * 64
        self.session_id = "9" * 32
        constant_block = {
            "index": 0,
            "name": "PerMaterial",
            "bind": 0,
            "space": 0,
            "bindArraySize": 1,
            "byteSize": 16,
            "bufferBacked": True,
            "inlineDataBytes": False,
            "compileConstants": False,
            "fields": [],
            "layoutCompleteness": "opaque",
        }
        shader_catalog_record = {
            "shaderResourceId": 7,
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "encoding": "ShaderEncoding.DXBC",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceComplete": True,
            "inputSignature": [],
            "outputSignature": [],
            "readOnlyResources": [],
            "readWriteResources": [],
            "samplers": [],
            "constantBlocks": [constant_block],
            "threadGroupSize": [],
            "outputTopology": "",
            "dynamicLinkageInterfaceCount": 0,
        }
        self.interface_sha = project_restore.interface_hash(shader_catalog_record)
        self.layout_sha = project_restore.layout_hash(shader_catalog_record)
        shader_catalog_record["interfaceHash"] = self.interface_sha
        shader_catalog_record["layoutHash"] = self.layout_sha
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "api": "GraphicsAPI.D3D11",
            "capturePath": str(self.capture_path),
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9],
                "stages": ["fragment"],
            },
            "shaders": [shader_catalog_record],
            "uses": [
                {
                    "shaderResourceId": 7,
                    "resourceId": 7,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": self.raw_sha,
                    "shaderKey": "7:fragment:main:%s" % self.raw_sha,
                    "eventId": 9,
                }
            ],
        }
        self.catalog_path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.catalog_sha = project_restore.hashlib.sha256(
            self.catalog_path.read_bytes()
        ).hexdigest()
        self.shader_key = "7:fragment:main:%s" % self.raw_sha
        self.stable_key_sha = project_restore._canonical_sha256(
            {
                "api": "d3d11",
                "encoding": "dxbc",
                "rawSha256": self.raw_sha,
                "stage": "fragment",
                "entryPoint": "main",
                "abiHash": self.live_abi_sha,
                "layoutHash": self.layout_sha,
            }
        )
        self.target_profile = {
            "kind": "DXBC",
            "stage": "fragment",
            "profile": "ps_5_0",
        }
        self.identity = {
            "api": "d3d11",
            "encoding": "dxbc",
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "abiHash": self.live_abi_sha,
            "layoutHash": self.layout_sha,
            "stableKeyComplete": True,
            "stableKeySha256": self.stable_key_sha,
            "anchorEventId": 9,
        }
        self.shader_record = {
            "identity": copy.deepcopy(self.identity),
            "knowledgeId": self.knowledge_id,
            "managedWorkspace": {
                "workspaceId": self.managed_workspace_id,
                "path": self.workspace.relative_to(self.project).as_posix(),
            },
            "candidateRevision": 3,
            "candidateRawSha256": self.candidate_sha,
            "sourceSha256": self.source_sha,
            "artifactRefs": {
                "projectArtifactId": self.knowledge_id,
                "revision": 3,
                "compiledSha256": self.candidate_sha,
                "compileConfigSha256": project_restore._canonical_sha256(
                    self.target_profile
                ),
                "representationsSha256": "",
                "aliasesSha256": "",
            },
        }
        self.binding = {
            "projectId": self.project_id,
            "workspaceId": self.active_workspace_id,
            "scopeSha256": self.scope_sha,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "knowledgeContentSha256": self.knowledge_sha,
            "matcherPolicySha256": self.matcher_sha,
            "shaders": [copy.deepcopy(self.shader_record)],
            "materials": [],
        }
        self.index = None
        self.active = {
            "workspaceId": self.active_workspace_id,
            "scopeConfigured": True,
            "projectRoot": str(self.project),
            "paths": {
                "replayProject": str(self.replay_path),
                "shaderWorkspaces": str(self.shader_root),
            },
            "replayProject": {
                "projectId": self.project_id,
                "projectPath": str(self.replay_path),
            },
        }
        self.workspace_status = {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(self.workspace),
            "workspaceId": self.managed_workspace_id,
            "capture": {
                "captureId": self.capture_id,
                "capturePath": str(self.capture_path),
                "api": "D3D11",
                "catalogPath": str(self.catalog_path),
                "catalogSha256": self.catalog_sha,
            },
            "shader": {
                "shaderKey": self.shader_key,
                "resourceId": 7,
                "rawSha256": self.raw_sha,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "targetProfile": copy.deepcopy(self.target_profile),
            "revision": 3,
            "source": {
                "present": True,
                "currentSha256": self.source_sha,
            },
            "latestCompiled": {
                "revision": 3,
                "sourceSha256": self.source_sha,
                "rawSha256": self.candidate_sha,
                "byteSize": 4,
                "kind": "DXBC",
                "replayAbiValidated": True,
                "abiCompatible": True,
                "abiGate": {
                    "status": "compatible",
                    "baselineAbiSha256": self.live_abi_sha,
                },
            },
            "baseline": {
                "rawSha256": self.raw_sha,
                "kind": "DXBC",
                "abiSha256": self.live_abi_sha,
            },
        }
        self.state = {
            "projectId": self.project_id,
            "projectPath": str(self.replay_path),
            "scopeSha256": self.scope_sha,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "knowledgeContentSha256": self.knowledge_sha,
            "matcherPolicySha256": self.matcher_sha,
            "capture": {"shaders": [copy.deepcopy(self.identity)]},
            "workspaces": {
                self.knowledge_id: {
                    "knowledgeId": self.knowledge_id,
                    "sourceSha256": self.source_sha,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": self.raw_sha,
                    "stableKeyComplete": True,
                    "stableKeySha256": self.stable_key_sha,
                }
            },
            "materials": {},
        }
        self.identity_evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9],
                "stages": ["fragment"],
            },
            "shaderKeys": [self.shader_key],
            "useTuples": [[9, self.shader_key]],
        }
        self.material_layout_sha = project_restore._catalog_block_projection(
            constant_block, 0, "fragment"
        )["layoutSha256"]
        self._refresh_exact_binding([self.shader_record])

    def _refresh_exact_binding(self, shader_records, material_records=None):
        records = [copy.deepcopy(item) for item in shader_records]
        materials = [copy.deepcopy(item) for item in (material_records or [])]
        receipts = [
            project_restore._realized_receipt_from_record(
                item,
                item["identity"]["abiHash"],
                abi_report_sha256="3" * 64,
            )
            for item in records
        ]
        self.binding = project_restore._binding_document(
            self.active,
            self.state,
            records,
            materials,
            receipts,
        )
        self.index = upsert_project_binding(new_index(), self.binding)

    def _live_replacement_record(self, **updates):
        replacement = {
            "replacementId": "e" * 32,
            "operationId": "d" * 32,
            "workspaceId": self.managed_workspace_id,
            "revision": 3,
            "anchorEventId": 9,
            "stage": "fragment",
            "entryPoint": "main",
            "api": "d3d11",
            "encoding": "dxbc",
            "originalResourceId": 7,
            "originalRawSha256": self.raw_sha,
            "candidateRawSha256": self.candidate_sha,
            "physicalAbiSha256": self.live_abi_sha,
            "resourceScoped": True,
            "eventScoped": False,
            "allOriginalResourceUsesAffected": True,
            "originalShaderPreserved": True,
        }
        replacement.update(updates)
        return replacement

    @staticmethod
    def _reseal_index_document(document):
        clean = copy.deepcopy(document)
        clean["sealSha256"] = project_restore._canonical_sha256(
            {key: value for key, value in clean.items() if key != "sealSha256"}
        )
        return clean

    def _shader_inventory_sidecar_document(self) -> dict:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        source_block = catalog["shaders"][0]["constantBlocks"][0]
        block = {
            **project_restore._catalog_block_projection(
                source_block, 0, "fragment"
            ),
            "name": str(source_block.get("name", "") or ""),
            "fields": [],
        }
        material = {
            "api": "d3d11",
            "encoding": "dxbc",
            "rawSha256": self.raw_sha,
            "stage": "fragment",
            "entryPoint": "main",
            "abiHash": self.live_abi_sha,
            "layoutHash": self.layout_sha,
        }
        return build_shader_inventory_cache_document(
            self.project_id,
            self.scope_sha,
            self.capture_path,
            {
                "assets": [
                    {
                        **self.identity,
                        "rawSize": 4,
                        "interfaceHash": self.interface_sha,
                        "interfaceComplete": True,
                        "interfaceEvidence": "null-pipeline-reflection",
                        "physicalAbiSha256": self.live_abi_sha,
                        "physicalAbiValidated": True,
                        "stableKeyMaterial": material,
                        "identityAmbiguous": False,
                        "formatSupported": True,
                        "anchorSource": "resource-usage-index",
                        "constantBlocks": [block],
                    }
                ],
                "coverage": {"inventoryComplete": True},
            },
        )

    def tearDown(self) -> None:
        project_restore._MATERIAL_SYNC_PARAMETERS.clear()
        self.temp.cleanup()

    def _configure_reuse_plan(
        self,
        host,
        *,
        with_material=False,
        with_binary=False,
        binary_import_error=None,
        binary_import_source_change=None,
        preexisting_binary_profile_mismatch=False,
        preexisting_binary_compiler_mismatch=False,
        precreate_target_workspace=False,
        target_source_modified=False,
    ):
        target_workspace = self.shader_root / "restored"
        target_status = copy.deepcopy(self.workspace_status)
        target_status.update(
            {
                "workspacePath": str(target_workspace),
                "workspaceId": "d" * 32,
                "revision": 0,
                "latestCompiled": None,
                "baseline": {
                    "rawSha256": self.raw_sha,
                    "kind": "DXBC",
                    "abiSha256": self.live_abi_sha,
                },
            }
        )
        target_status["source"] = {
            "present": True,
            "currentSha256": "e" * 64,
            "modifiedSinceDecompile": bool(target_source_modified),
            "lane": "executable-editable",
            "format": "hlsl",
        }
        host.target_workspace = target_workspace
        host.target_status = target_status
        if precreate_target_workspace:
            target_workspace.mkdir(parents=True, exist_ok=False)
        match = {
            "shader": copy.deepcopy(self.identity),
            "workspaceCandidates": [
                {
                    "knowledgeId": self.knowledge_id,
                    "matchMode": "raw-exact",
                    "sourceSha256": self.source_sha,
                    "sourceSeedEligible": True,
                    "activeCandidate": True,
                    "stableKeyComplete": True,
                    "stableKeySha256": self.stable_key_sha,
                }
            ],
            "aliasCandidates": [],
            "materialCandidates": [],
        }
        if with_binary:
            binary_path = self.replay_path / "artifacts" / ("f" * 64 + ".dxbc")
            match["workspaceCandidates"][0].update(
                {
                    "binaryCandidateEligible": True,
                    "binaryCandidate": {
                        "artifactPath": str(binary_path),
                        "sha256": "f" * 64,
                        "codeSha256": "c" * 64,
                        "byteSize": 4,
                        "kind": "dxbc",
                        "sourceSha256": self.source_sha,
                        "sourceRevision": 1,
                        "targetProfile": copy.deepcopy(
                            target_status["targetProfile"]
                        ),
                        "compilerBindingSha256": "d" * 64,
                        "candidateOnly": True,
                        "contentAddressed": True,
                        "integrityVerified": True,
                    },
                }
            )

            def project_import_receipt(source_sha256):
                return {
                    "artifactSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "sourceSha256": source_sha256,
                    "sourceRevision": 1,
                    "targetProfile": copy.deepcopy(target_status["targetProfile"]),
                    "compilerBindingSha256": "d" * 64,
                    "originValidationEvidenceOnly": True,
                    "freshCaptureRebindRequired": True,
                    "freshLiveAbiRevalidationRequired": True,
                    "applyAuthorization": False,
                    "oldReplacementIdReused": False,
                    "oldCaptureAbiAuthorityReused": False,
                }

            def import_candidate(workspace_path, unused_candidate):
                if binary_import_source_change is not None:
                    target_status["source"][
                        "currentSha256"
                    ] = binary_import_source_change
                if binary_import_error is not None:
                    raise binary_import_error
                project_import = project_import_receipt(
                    target_status["source"]["currentSha256"]
                )
                target_status["revision"] = 1
                target_status["latestCompiled"] = {
                    "revision": 1,
                    "sourceSha256": target_status["source"]["currentSha256"],
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "projectImport": project_import,
                }
                return {
                    "workspacePath": workspace_path,
                    "revision": 1,
                    "sourceSha256": target_status["source"]["currentSha256"],
                    "candidateBlobPath": str(binary_path),
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "kind": "DXBC",
                    "projectImport": project_import,
                    "compilerSkipped": True,
                    "compiledFromProjectArtifact": True,
                    "requiresReplayAbiValidation": True,
                    "replayAbiValidated": False,
                    "applied": False,
                }

            host.workspace_service.import_handler = import_candidate
            if preexisting_binary_profile_mismatch:
                stale_receipt = project_import_receipt(self.source_sha)
                stale_receipt["targetProfile"]["profile"] = "ps_4_0"
                target_status["revision"] = 2
                target_status["latestCompiled"] = {
                    "revision": 2,
                    "sourceSha256": self.source_sha,
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "projectImport": stale_receipt,
                }
            elif preexisting_binary_compiler_mismatch:
                stale_receipt = project_import_receipt(self.source_sha)
                stale_receipt["compilerBindingSha256"] = "e" * 64
                target_status["revision"] = 2
                target_status["latestCompiled"] = {
                    "revision": 2,
                    "sourceSha256": self.source_sha,
                    "rawSha256": "f" * 64,
                    "codeSha256": "c" * 64,
                    "byteSize": 4,
                    "kind": "DXBC",
                    "projectImport": stale_receipt,
                }
        if with_material:
            material_knowledge_id = "6" * 32
            saved_parameter_id = "7" * 32
            live_parameter_id = "8" * 32
            self.state["materials"][material_knowledge_id] = {
                "knowledgeId": material_knowledge_id,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": self.raw_sha,
                "codeSha256": self.code_sha,
                "interfaceComplete": True,
                "interfaceHash": self.interface_sha,
                "layoutHash": self.layout_sha,
                "constantBlockPath": "PerMaterial",
                "constantBlockLayoutSha256": self.material_layout_sha,
                "parameters": [
                    {
                        "parameterId": saved_parameter_id,
                        "path": "PerMaterial.Roughness",
                        "baseType": "Float",
                        "components": 1,
                        "value": 0.35,
                    }
                ],
            }
            match["materialCandidates"] = [
                {
                    "knowledgeId": material_knowledge_id,
                    "matchMode": "raw-exact",
                    "parameterCount": 1,
                    "constantBlockPath": "PerMaterial",
                    "constantBlockLayoutSha256": self.material_layout_sha,
                    "activeCandidate": True,
                }
            ]
            host.material_session = {
                "schema": "renderdoc-ultra-material-session/v1",
                "schemaVersion": 1,
                "sessionId": "5" * 32,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
                "eventId": 9,
                "stage": "fragment",
                "shaderResourceId": 7,
                "constantBlockIndex": 0,
                "constantBlockPath": "PerMaterial",
                "layoutSha256": self.material_layout_sha,
                "parameters": [
                    {
                        "parameterId": live_parameter_id,
                        "path": "PerMaterial.Roughness",
                        "baseType": "Float",
                        "components": 1,
                    }
                ],
            }
        plan = {
            "schema": "renderdoc-ultra-project-reuse-plan/v1",
            "schemaVersion": 1,
            "projectId": self.project_id,
            "projectPath": str(self.replay_path),
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "matchedShaderCount": 1,
            "matches": [match],
            "policy": {
                "rawExactOrCodeInterfaceLayoutExactOnly": True,
                "oldWorkspaceManifestReused": False,
                "oldCaptureAbiReportReused": False,
                "oldMaterialSessionReused": False,
                "liveAbiRevalidationRequiredBeforeShaderApply": True,
                "liveMaterialLayoutRevalidationRequired": True,
            },
        }
        host.prepared_response = {
            "schema": "replaylab-active-capture-prepare/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "project": copy.deepcopy(self.active),
            "captureExport": {
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
                "fullFrame": True,
            },
            "stagedCatalog": {
                "captureId": self.capture_id,
                "catalogPath": str(self.catalog_path),
                "catalogSha256": self.catalog_sha,
            },
            "reusePlan": plan,
            "applied": False,
        }

    def _configure_alias_reuse(self, host, replay, *, prepared=False):
        alias_candidate = {
            "knowledgeId": "4" * 32,
            "groupIndex": 0,
            "matchMode": "raw-exact",
            "activeCandidate": True,
        }
        if prepared:
            plan = host.prepared_response["reusePlan"]
            plan["matches"][0]["aliasCandidates"] = [alias_candidate]
        else:
            plan = {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha,
                "matchedShaderCount": 1,
                "matches": [
                    {
                        "shader": copy.deepcopy(self.identity),
                        "workspaceCandidates": [],
                        "aliasCandidates": [alias_candidate],
                        "materialCandidates": [],
                    }
                ],
            }
            replay.reuse_plan_response = plan
        alias = {
            "captureId": self.capture_id,
            "shaderResourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "bindingKind": "shader",
            "registerSpace": 0,
            "bindNumber": 0,
            "byteOffset": 0,
            "canonicalPath": "",
            "alias": "MainSurfacePS",
            "originalName": "",
            "provenance": "program",
            "confidence": 1.0,
            "verified": True,
            "metadata": {"programVerified": True},
        }
        alias_table = {
            "schema": "renderdoc-ultra-aliases/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "captureCatalog": str(self.catalog_path),
            "verifiedOnly": True,
            "aliases": [alias],
            "shaders": [
                {
                    "shaderResourceId": 7,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": self.raw_sha,
                    "codeSha256": self.code_sha,
                    "aliases": [alias],
                    "verified": True,
                }
            ],
            "symbolRules": [],
        }
        encoded = (
            json.dumps(alias_table, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        replay.materialize_response = {
            "schema": "renderdoc-ultra-project-alias-materialization/v1",
            "schemaVersion": 1,
            "projectId": self.project_id,
            "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha,
            "shader": copy.deepcopy(self.identity),
            "aliasCount": 1,
            "aliasTable": alias_table,
            "jsonBytes": encoded,
            "sha256": project_restore.hashlib.sha256(encoded).hexdigest(),
            "displayAliasesOnly": True,
            "applied": False,
        }

    def _add_second_cached_shader(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        second_catalog_shader = copy.deepcopy(catalog["shaders"][0])
        second_catalog_shader.update({"shaderResourceId": 8, "resourceId": 8})
        catalog["shaders"].append(second_catalog_shader)
        second_shader_key = "8:fragment:main:%s" % self.raw_sha
        second_use = copy.deepcopy(catalog["uses"][0])
        second_use.update(
            {
                "shaderResourceId": 8,
                "resourceId": 8,
                "shaderKey": second_shader_key,
                "eventId": 10,
            }
        )
        catalog["uses"].append(second_use)
        catalog["selection"]["eventIds"] = [9, 10]
        self.catalog_path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.catalog_sha = project_restore.hashlib.sha256(
            self.catalog_path.read_bytes()
        ).hexdigest()
        self.workspace_status["capture"]["catalogSha256"] = self.catalog_sha
        self.state["catalogSha256"] = self.catalog_sha
        second_identity = copy.deepcopy(self.identity)
        second_identity.update({"resourceId": 8, "anchorEventId": 10})
        second_knowledge_id = "b" * 32
        self.state["capture"]["shaders"].append(copy.deepcopy(second_identity))
        self.state["workspaces"][second_knowledge_id] = {
            "knowledgeId": second_knowledge_id,
            "sourceSha256": self.source_sha,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "stableKeyComplete": True,
            "stableKeySha256": self.stable_key_sha,
        }
        second_workspace = self.shader_root / "cached-second"
        second_workspace.mkdir()
        second_status = copy.deepcopy(self.workspace_status)
        second_status.update(
            {
                "workspacePath": str(second_workspace),
                "workspaceId": "e" * 32,
            }
        )
        second_status["shader"].update(
            {"shaderKey": second_shader_key, "resourceId": 8}
        )
        second_record = copy.deepcopy(self.shader_record)
        second_record.update(
            {
                "identity": second_identity,
                "knowledgeId": second_knowledge_id,
                "managedWorkspace": {
                    "workspaceId": "e" * 32,
                    "path": second_workspace.relative_to(self.project).as_posix(),
                },
                "artifactRefs": {
                    **second_record["artifactRefs"],
                    "projectArtifactId": second_knowledge_id,
                },
            }
        )
        self._refresh_exact_binding(
            [*self.binding["shaders"], second_record],
            self.binding["materials"],
        )
        self.identity_evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": [9, 10],
                "stages": ["fragment"],
            },
            "shaderKeys": [self.shader_key, second_shader_key],
            "useTuples": [[9, self.shader_key], [10, second_shader_key]],
        }
        return second_status

    def _configure_inventory_replay(self, replay, host, *, index_present):
        # Exact RDC bindings now validate project knowledge before any live
        # inventory exists. The first inventory snapshot is therefore always
        # the miss/stale fallback for the current resource set.
        cache_snapshot_pending = False
        last_snapshot: dict | None = None

        def snapshot(inventory, expected_binding):
            nonlocal cache_snapshot_pending, last_snapshot
            if expected_binding is not None:
                if (
                    last_snapshot is None
                    or last_snapshot["inventory"] != inventory
                    or last_snapshot["binding"] != expected_binding
                ):
                    raise AssertionError("Shader inventory binding changed")
                return copy.deepcopy(last_snapshot)

            cache_lookup = cache_snapshot_pending
            cache_snapshot_pending = False
            project_capture = inventory["capture"]
            knowledge = {"workspaces": {}, "aliases": [], "materials": {}}
            for knowledge_id, record in self.state["workspaces"].items():
                targets = [
                    shader
                    for shader in inventory["shaders"]
                    if shader["stage"] == record["stage"]
                    and shader.get("entryPoint", "")
                    == record.get("entryPoint", "")
                    and shader["rawSha256"] == record["rawSha256"]
                    and shader["stableKeySha256"]
                    == record["stableKeySha256"]
                ]
                if not targets:
                    continue
                target = targets[0]
                knowledge["workspaces"][knowledge_id] = {
                    "physicalIdentity": {
                        field: target[field]
                        for field in (
                            "api",
                            "encoding",
                            "rawSha256",
                            "stage",
                            "entryPoint",
                            "abiHash",
                            "layoutHash",
                            "stableKeySha256",
                        )
                    },
                    "interfaceComplete": target["interfaceComplete"],
                    "source": {
                        "kind": "verified-hlsl",
                        "knowledgeId": knowledge_id,
                        "artifactPath": str(
                            self.replay_path / "artifacts" / (knowledge_id + ".hlsl")
                        ),
                        "sha256": record["sourceSha256"],
                        "byteSize": 4,
                    },
                    "registeredWorkspace": copy.deepcopy(record.get("registeredWorkspace")),
                    "publicationMode": record.get("publicationMode"),
                    "sourceOriginPath": record.get("sourceOriginPath"),
                    "registeredCompiledCandidatePresent": record.get("compiledCandidate") is not None,
                    "targetProfile": copy.deepcopy(
                        self.workspace_status["targetProfile"]
                    ),
                    "compiledCandidate": {
                        "sha256": self.candidate_sha,
                    } if record.get("publicationMode") != "source-only-draft" else None,
                    "compilerBinding": None,
                    "assemblyDisassemblyArtifacts": [],
                }

            if cache_lookup:
                capture_id = self.state["captureId"]
                catalog_sha256 = self.state["catalogSha256"]
            else:
                capture_id = project_capture["captureId"]
                catalog_sha256 = project_capture["inventorySha256"]
            knowledge_ids = {
                "workspaces": sorted(knowledge["workspaces"]),
                "aliases": [],
                "materials": [],
            }
            binding = {
                "schema": "renderdoc-ultra-project-shader-inventory-binding/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "scopeSha256": self.scope_sha,
                "captureId": capture_id,
                "catalogSha256": catalog_sha256,
                "inventorySha256": project_capture["inventorySha256"],
                "knowledgeContentSha256": self.knowledge_sha,
                "matcherPolicySha256": self.matcher_sha,
                "activeKnowledgeRevision": 1,
                "knowledgeIds": knowledge_ids,
                "knowledgeIdsSha256": project_restore._canonical_sha256(
                    knowledge_ids
                ),
                "artifactRefs": [],
                "artifactRefsSha256": project_restore._canonical_sha256([]),
            }
            binding["bindingSha256"] = project_restore._canonical_sha256(binding)

            configured_plan = (
                copy.deepcopy(host.prepared_response["reusePlan"])
                if host.prepared_response is not None
                else copy.deepcopy(replay.reuse_plan_response)
            )
            matches = []
            if configured_plan is not None:
                for match in configured_plan["matches"]:
                    target = next(
                        (
                            shader
                            for shader in inventory["shaders"]
                            if shader["resourceId"]
                            == match["shader"]["resourceId"]
                            and shader["stableKeySha256"]
                            == match["shader"]["stableKeySha256"]
                        ),
                        None,
                    )
                    if target is None:
                        continue
                    match["shader"] = copy.deepcopy(target)
                    match["materialCandidates"] = []
                    matches.append(match)
            policy = {
                "inventoryOnly": True,
                "fuzzyExecutableMatchAllowed": False,
                "stableKeyExactRequiredForSourceOrBinary": False,
                "stableKeyExactRequiredForSource": False,
                "rawExactSourcePreparationAllowed": True,
                "stableKeyExactRequiredForBinary": True,
                "targetAnchorRequiredBeforeExecutableApply": True,
                "binaryExecutableAutoApplyAuthorized": False,
                "inventoryMaterialValueReuseAllowed": False,
            }
            plan = {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "captureId": (
                    capture_id if cache_lookup else project_capture["captureId"]
                ),
                "catalogSha256": (
                    catalog_sha256
                    if cache_lookup
                    else project_capture["inventorySha256"]
                ),
                "matchedShaderCount": len(matches),
                "matches": matches,
                "policy": policy,
            }
            snapshot_value = {
                "schema": "renderdoc-ultra-project-shader-inventory-reuse/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "projectPath": str(self.replay_path),
                "inventory": copy.deepcopy(inventory),
                "reusePlan": plan,
                "binding": binding,
                "knowledge": knowledge,
            }
            snapshot_value["snapshotSha256"] = project_restore._canonical_sha256(
                {"binding": binding, "knowledge": knowledge}
            )
            last_snapshot = copy.deepcopy(snapshot_value)
            replay.last_inventory_snapshot = copy.deepcopy(snapshot_value)
            return snapshot_value

        def seed(knowledge_id, destination_status, target_shader, inventory, binding,
                 supersede_local_edit=False, published_source_advance=None):
            record = self.state["workspaces"][knowledge_id]
            if target_shader["stableKeySha256"] != record["stableKeySha256"]:
                raise AssertionError("Shader inventory stable key changed")
            if (destination_status["source"].get("modifiedSinceDecompile")
                    and not supersede_local_edit and published_source_advance is None
                    and destination_status["source"].get("currentSha256") != record["sourceSha256"]):
                raise AssertionError("destination must contain unmodified executable-editable HLSL")
            host.seed_calls += 1
            host.seed_supersede_calls = getattr(host, "seed_supersede_calls", 0) + int(bool(supersede_local_edit))
            host.seed_published_advance = copy.deepcopy(published_source_advance)
            del binding
            host.target_status["source"].update(
                {
                    "currentSha256": record["sourceSha256"],
                    "modifiedSinceDecompile": True,
                }
            )
            return {
                "supersededLocalEdit": ({"backupPath": str(host.target_workspace / "ai" / "local-edits" / "x.hlsl"),
                                         "sourceSha256": "e" * 64, "bytes": 3}
                                        if supersede_local_edit or published_source_advance is not None else None),
                "schema": "renderdoc-ultra-project-workspace-inventory-seed/v1",
                "schemaVersion": 1,
                "projectId": self.project_id,
                "captureId": inventory["capture"]["captureId"],
                "catalogSha256": inventory["capture"]["inventorySha256"],
                "knowledgeId": knowledge_id,
                "targetResourceId": target_shader["resourceId"],
                "targetStableKeySha256": target_shader["stableKeySha256"],
                "workspacePath": str(host.target_workspace),
                "sourceSha256": record["sourceSha256"],
                "applied": False,
            }

        def register(status, target_shader, inventory, expected_binding):
            nonlocal last_snapshot
            if (
                last_snapshot is None
                or last_snapshot["inventory"] != inventory
                or last_snapshot["binding"] != expected_binding
            ):
                raise AssertionError("Shader inventory publication binding changed")
            matches = [
                (knowledge_id, value)
                for knowledge_id, value in last_snapshot["knowledge"][
                    "workspaces"
                ].items()
                if value["physicalIdentity"]["stableKeySha256"]
                == target_shader["stableKeySha256"]
            ]
            if len(matches) != 1:
                raise AssertionError("Shader inventory publication target changed")
            knowledge_id, artifact = matches[0]
            latest = status.get("latestCompiled")
            if not isinstance(latest, dict):
                raise AssertionError("validated Shader candidate is absent")
            artifact["compiledCandidate"] = {
                "rawSha256": latest["rawSha256"],
                "sourceSha256": latest["sourceSha256"],
                "sourceRevision": latest["revision"],
                "targetProfile": copy.deepcopy(status["targetProfile"]),
            }
            last_snapshot["snapshotSha256"] = project_restore._canonical_sha256(
                {
                    "binding": last_snapshot["binding"],
                    "knowledge": last_snapshot["knowledge"],
                }
            )
            return {
                "knowledge": {"knowledgeId": knowledge_id},
                "idempotent": False,
                "applied": False,
                "binding": copy.deepcopy(last_snapshot["binding"]),
                "snapshotSha256": last_snapshot["snapshotSha256"],
            }

        replay.inventory_snapshot_handler = snapshot
        replay.inventory_seed_handler = seed
        replay.inventory_register_handler = register

    def _run(
        self,
        *,
        write_error=None,
        bind_error=None,
        index_present=True,
        auto_prepare=False,
        with_material=False,
        with_binary=False,
        compile_error=None,
        switch_project_on_compile=False,
        switch_project_on_apply=False,
        with_aliases=False,
        alias_load_error=None,
        alias_load_error_after_commit=None,
        alias_response_mutator=None,
        require_alias_before_apply=False,
        apply_response_mutator=None,
        apply_error_after_commit=None,
        binary_import_error=None,
        binary_import_source_change=None,
        preexisting_binary_profile_mismatch=False,
        preexisting_binary_compiler_mismatch=False,
        precreate_target_workspace=False,
        target_source_modified=False,
        extra_workspace_statuses=None,
        apply_error_workspace_ids=None,
        inventory_asset_mutator=None,
        inventory_unindexed_resource_ids=None,
        inventory_result_mutator=None,
        expected_result_mutator=None,
        prepare_progress_events=None,
        material_update_error=None,
        shader_rollback_error=None,
        switch_project_after_material_success=False,
        shared_replacements=None,
        inventory_register_error=None,
        rdc_section_version=1,
        cancel_on_progress_phase=None,
        deadline_monotonic=None,
        return_control_error=False,
        resource_index_begin_running=False,
        resource_index_poll_errors=None,
        source_preparation_only=False,
        source_preparation_mutator=None,
        host_mutator=None,
        post_restore_callback=None,
    ):
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            self.index if index_present else None,
            self.identity_evidence,
        )
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog_shaders = {
            shader["resourceId"]: shader for shader in catalog["shaders"]
        }
        for identity in self.state["capture"]["shaders"]:
            catalog_shader = catalog_shaders[identity["resourceId"]]
            blocks = [
                {
                    **project_restore._catalog_block_projection(
                        block, index, identity["stage"]
                    ),
                    "name": str(block.get("name", "") or ""),
                    "fields": [],
                }
                for index, block in enumerate(catalog_shader["constantBlocks"])
            ]
            asset = {
                **copy.deepcopy(identity),
                "stableKeyMaterial": {
                    field: identity[field]
                    for field in (
                        "api",
                        "encoding",
                        "rawSha256",
                        "stage",
                        "entryPoint",
                        "abiHash",
                        "layoutHash",
                    )
                },
                "ownerResourceId": identity["resourceId"],
                "ownerCaptureSessionId": self.session_id,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
                "interfaceHash": self.interface_sha,
                "interfaceComplete": True,
                "interfaceEvidence": "null-pipeline-reflection",
                "constantBlocks": blocks,
                "rawSize": 4,
                "rawBytecodeIncluded": False,
                "formatSupported": True,
                "identityAmbiguous": False,
                "physicalAbiSha256": self.live_abi_sha,
                "physicalAbiValidated": True,
                "requiresAnchorPreflight": True,
                "anchorSource": "resource-usage-index",
                "exactExecutableMatchEligible": True,
            }
            if inventory_asset_mutator is not None:
                inventory_asset_mutator(asset)
            if source_preparation_only:
                asset.update(interfaceComplete=False, stableKeyComplete=False,
                             exactExecutableMatchEligible=False)
            host.ipc.inventory_assets.append(asset)
        host.ipc.inventory_unindexed_resource_ids = list(
            inventory_unindexed_resource_ids or []
        )
        host.ipc.inventory_result_mutator = inventory_result_mutator
        host.ipc.expected_result_mutator = expected_result_mutator
        host.ipc.shared_replacements = copy.deepcopy(shared_replacements or [])
        host.ipc.section_version = rdc_section_version
        host.ipc.begin_reports_running = resource_index_begin_running
        host.ipc.poll_errors = list(resource_index_poll_errors or [])
        if auto_prepare:
            self._configure_reuse_plan(
                host,
                with_material=with_material,
                with_binary=with_binary,
                binary_import_error=binary_import_error,
                binary_import_source_change=binary_import_source_change,
                preexisting_binary_profile_mismatch=(
                    preexisting_binary_profile_mismatch
                ),
                preexisting_binary_compiler_mismatch=(
                    preexisting_binary_compiler_mismatch
                ),
                precreate_target_workspace=precreate_target_workspace,
                target_source_modified=target_source_modified,
            )
        host.compile_error = compile_error
        host.switch_project_on_compile = switch_project_on_compile
        host.switch_project_on_apply = switch_project_on_apply
        host.ipc.write_error = write_error
        host.ipc.bind_error = bind_error
        replay = _ReplayService(self.state)
        self._configure_inventory_replay(
            replay, host, index_present=index_present
        )
        if source_preparation_only:
            candidate = host.prepared_response["reusePlan"]["matches"][0]["workspaceCandidates"][0]
            candidate.update(sourcePreparationOnly=True, binaryCandidateEligible=False, binaryCandidate=None)
            host.target_status["source"].update({
                "currentSha256": self.source_sha,
                "projectSource": {
                    "schema":"replaylab-verified-project-shader-source/v1",
                    "projectId":self.project_id,"projectPath":str(self.replay_path),
                    "knowledgeId":self.knowledge_id,"sourceSha256":self.source_sha,
                    "originalRawSha256":self.raw_sha,"baselineAbiSha256":self.live_abi_sha,
                    "stage":self.identity["stage"],"entryPoint":self.identity["entryPoint"],
                    "targetProfile":copy.deepcopy(host.target_status["targetProfile"]),
                    "applied":False,"requiresCompile":True,"requiresLiveAbiRevalidation":True,
                },
            })
            if source_preparation_mutator is not None:
                source_preparation_mutator(host, replay)
        if inventory_register_error is not None:
            def reject_inventory_publication(*unused_args, **unused_kwargs):
                raise inventory_register_error

            replay.inventory_register_handler = reject_inventory_publication
        if with_aliases:
            self._configure_alias_reuse(host, replay, prepared=auto_prepare)
        host.ipc.alias_load_error = alias_load_error
        host.ipc.alias_load_error_after_commit = alias_load_error_after_commit
        host.ipc.alias_response_mutator = alias_response_mutator
        host.require_alias_before_apply = require_alias_before_apply
        host.apply_response_mutator = apply_response_mutator
        host.apply_error_after_commit = apply_error_after_commit
        host.workspace_service.extra_statuses = list(extra_workspace_statuses or [])
        host.apply_error_workspace_ids = set(apply_error_workspace_ids or [])
        host.prepare_progress_events = list(prepare_progress_events or [])
        host.material_update_error = material_update_error
        host.shader_rollback_error = shader_rollback_error
        host.switch_project_after_material_success = (
            switch_project_after_material_success
        )
        if host_mutator is not None:
            host_mutator(host, replay)
        progress = []
        cancel_event = (
            threading.Event() if cancel_on_progress_phase is not None else None
        )

        def record_progress(event):
            progress.append(event)
            if (
                cancel_event is not None
                and event.get("phase") == cancel_on_progress_phase
            ):
                cancel_event.set()

        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=replay
        ):
            try:
                result = project_restore.restore_active_capture_project(
                    self.project,
                    self.capture_path,
                    self.session_id,
                    record_progress,
                    cancel_event=cancel_event,
                    deadline_monotonic=deadline_monotonic,
                    post_restore_callback=post_restore_callback,
                )
            except project_restore.RestoreControlStop as exc:
                if not return_control_error:
                    raise
                result = exc
        return result, host, replay, progress

    def test_progress_item_clips_long_unicode_without_interrupting_callback(self) -> None:
        events = []
        warnings = []
        progress = project_restore._Progress("a" * 32, events.append, warnings)

        progress.emit(
            "shader-apply",
            "Applying Shader",
            current=1,
            total=1,
            item="目录/" + ("着" * 2000) + "/shader.hlsl",
        )

        self.assertEqual(len(events), 1)
        self.assertLessEqual(len(events[0]["item"].encode("utf-8")), 1024)
        self.assertTrue(events[0]["item"].startswith("目录/"))
        self.assertEqual(warnings, [])

    def test_empty_project_never_starts_shader_inventory(self) -> None:
        empty_status = {
            "projectId": self.project_id,
            "knowledgeCounts": {
                "workspaces": 0,
                "materials": 0,
                "aliases": 0,
            },
        }
        with mock.patch.object(
            _ReplayService, "status", return_value=empty_status, create=True
        ):
            result, host, unused_replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertEqual(result["restoreMode"], "no-project-knowledge")
        self.assertEqual(result["shaders"]["attempted"], 0)
        self.assertEqual(result["materials"]["attempted"], 0)
        self.assertEqual(
            result["inventoryCache"]["reason"],
            "not-required-for-empty-project",
        )
        methods = [method for method, unused in host.ipc.calls]
        self.assertNotIn("read_rdc_project_index", methods)
        self.assertNotIn("begin_rdc_project_shader_resource_index", methods)
        self.assertNotIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

    def test_exact_cache_hit_indexes_resources_and_never_prepares_full_frame(self) -> None:
        result, host, replay, progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.scope_calls, 1)
        self.assertEqual(host.projection_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.snapshot_calls, 2)
        self.assertEqual(replay.inventory_snapshot_calls, 0)
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["desiredHead"]["headSha256"], self.binding["desiredProjectHead"]["headSha256"])
        self.assertEqual(result["realizedReceipts"]["validatedForRouting"], 1)
        self.assertEqual(result["realizedReceipts"]["persisted"], 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("begin_rdc_project_shader_resource_index"), 1
        )
        self.assertEqual(
            methods.count("commit_rdc_project_shader_resource_index"), 1
        )
        self.assertNotIn("bind_rdc_project_shader_catalog", methods)
        self.assertNotIn("export_shader_catalog", methods)
        begin = next(
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        )
        self.assertFalse(begin["inventory_all_shaders"])
        self.assertFalse(begin["discover_anchor_eids"])
        self.assertEqual(len(begin["expected_shaders"]), 1)
        expected = host.ipc.resource_index_snapshot["result"]["assets"][0]
        self.assertEqual(expected, {
            **begin["expected_shaders"][0],
            "ownerResourceId": 7,
            "ownerCaptureSessionId": self.session_id,
            "resourceOwnershipScope": "target-capture-session",
            "crossCaptureResourceIdReuse": False,
            "api": "d3d11",
            "physicalAbiSha256": "f" * 64,
            "physicalAbiValidated": True,
            "requiresAnchorPreflight": True,
            "constantBlocksMatched": True,
            "interfaceMatched": True,
            "contentMatchMode": "stable-key-owner-rid",
        })
        self.assertEqual(expected["encoding"], "dxbc")
        self.assertEqual(expected["abiHash"], self.live_abi_sha)
        self.assertTrue(expected["stableKeyComplete"])
        self.assertEqual(expected["stableKeySha256"], self.stable_key_sha)
        self.assertEqual(expected["interfaceHash"], self.interface_sha)
        self.assertEqual(expected["layoutHash"], self.layout_sha)
        self.assertTrue(expected["interfaceComplete"])
        self.assertEqual(
            set(expected["constantBlocks"][0]),
            {
                "space",
                "bind",
                "index",
                "path",
                "byteSize",
                "layoutSha256",
                "abiSha256",
            },
        )
        phases = [event["phase"] for event in progress]
        self.assertNotIn("resource-inventory", phases)
        self.assertNotIn("prepare", phases)
        replacement = host.last_apply_response["replacement"]
        self.assertTrue(replacement["resourceScoped"])
        self.assertFalse(replacement["eventScoped"])
        self.assertTrue(replacement["allOriginalResourceUsesAffected"])
        self.assertTrue(replacement["originalShaderPreserved"])
        self.assertTrue(all(len(event["message"].encode("utf-8")) <= 1024 for event in progress))

    def test_foreground_poll_timeout_retries_the_same_shader_index_job(self) -> None:
        transient = RuntimeError(
            "Timeout (5s). Make sure RenderDoc is running with capture loaded. "
            "Open Tools > Bread AI, then run Check Connection."
        )
        result, host, unused_replay, progress = self._run(
            resource_index_begin_running=True,
            resource_index_poll_errors=[transient],
        )

        self.assertTrue(result["completed"])
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("begin_rdc_project_shader_resource_index"), 1
        )
        self.assertEqual(
            methods.count("poll_rdc_project_shader_resource_index"), 2
        )
        begin = next(
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        )
        self.assertFalse(begin["inventory_all_shaders"])
        self.assertNotIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

    def test_same_capture_session_adopts_exact_live_replacement_without_mutation(
        self,
    ) -> None:
        result, host, unused_replay, unused_progress = self._run(
            shared_replacements=[self._live_replacement_record()]
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertTrue(result["shaders"]["items"][0]["idempotentlyAdopted"])
        self.assertEqual(result["realizedReceipts"]["adoptedCurrentSession"], 1)
        self.assertEqual(result["realizedReceipts"]["mutationsCreated"], 0)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(methods.count("get_shared_shader_replacements"), 1)
        receipt = host.ipc.index["bindings"][0]["realizedApplySnapshot"][
            "receipts"
        ][0]
        self.assertNotIn("replacementId", receipt)
        self.assertNotIn("captureSessionId", receipt)
        self.assertNotIn("operationId", receipt)
        self.assertNotIn("targetResourceId", receipt)
        self.assertTrue(receipt["routingOnly"])
        self.assertFalse(receipt["physicalApplyAuthority"])

    def test_v2_rdc_section_transport_remains_readable_after_v3_codec(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            rdc_section_version=2
        )

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(host.apply_calls, 1)

    def test_unrelated_active_revision_audit_does_not_invalidate_desired_head(
        self,
    ) -> None:
        self.binding["desiredProjectHead"]["activeKnowledgeRevision"] = 41
        self.index = upsert_project_binding(new_index(), self.binding)

        result, host, unused_replay, unused_progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["desiredHead"]["activeKnowledgeRevision"], 41)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.apply_calls, 1)

    def test_new_capture_session_never_uses_persisted_receipt_as_apply_authority(
        self,
    ) -> None:
        self.session_id = "4" * 32

        result, host, unused_replay, unused_progress = self._run()

        self.assertTrue(result["completed"])
        self.assertEqual(result["realizedReceipts"]["validatedForRouting"], 1)
        self.assertEqual(result["realizedReceipts"]["adoptedCurrentSession"], 0)
        self.assertEqual(host.apply_calls, 1)

    def test_tampered_or_abi_mismatched_receipt_falls_back_before_apply(self) -> None:
        for label, field in (
            ("candidate-tamper", "candidateRawSha256"),
            ("abi-mismatch", "physicalAbiSha256"),
        ):
            with self.subTest(label=label):
                forged = copy.deepcopy(self.index)
                receipt = forged["bindings"][0]["realizedApplySnapshot"][
                    "receipts"
                ][0]
                receipt[field] = "4" * 64
                receipt["receiptSha256"] = project_restore._canonical_sha256(
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "receiptSha256"
                    }
                )
                snapshot = forged["bindings"][0]["realizedApplySnapshot"]
                snapshot["receiptsSha256"] = project_restore._canonical_sha256(
                    snapshot["receipts"]
                )
                self.index = self._reseal_index_document(forged)

                result, host, unused_replay, progress = self._run(
                    auto_prepare=True
                )

                self.assertTrue(result["completed"])
                self.assertFalse(result["cache"]["hit"])
                self.assertEqual(result["restoreMode"], "project-target-inventory")
                self.assertFalse(result["fullFrame"])
                self.assertEqual(result["shaders"]["applied"], 1)
                self.assertEqual(host.apply_calls, 1)
                begin = next(
                    params
                    for method, params in host.ipc.calls
                    if method == "begin_rdc_project_shader_resource_index"
                )
                self.assertTrue(begin["inventory_all_shaders"])
                self.assertIn(
                    "resource-inventory", [event["phase"] for event in progress]
                )
                self._refresh_exact_binding([self.shader_record])

    def test_index_miss_uses_sealed_inventory_sidecar_and_only_fresh_targets(self) -> None:
        document = self._shader_inventory_sidecar_document()
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ), mock.patch.object(
            project_restore,
            "load_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ), mock.patch.object(
            project_restore,
            "write_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ) as cache_write:
            result, host, replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertTrue(result["inventoryCache"]["hit"])
        self.assertTrue(result["inventoryCache"]["written"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.inventory_snapshot_calls, 2)
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(begins), 1)
        self.assertFalse(begins[0]["inventory_all_shaders"])
        self.assertFalse(begins[0]["discover_anchor_eids"])
        self.assertEqual(len(begins[0]["expected_shaders"]), 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("commit_rdc_project_shader_resource_index"), 1
        )
        self.assertIn("write_rdc_project_index", methods)
        self.assertEqual(cache_write.call_count, 1)
        self.assertNotIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

    def test_first_project_target_lookup_never_publishes_a_full_sidecar(self) -> None:
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ):
            result, host, unused_replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertFalse(result["inventoryCache"]["hit"])
        self.assertFalse(result["inventoryCache"]["written"])
        document = load_shader_inventory_cache(
            self.replay_path,
            self.project_id,
            self.scope_sha,
            self.capture_path,
        )
        self.assertIsNone(document)
        self.assertEqual(result["inventoryScope"], "target-raw-sha256")
        self.assertFalse(result["fullFrame"])
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(begins), 1)
        self.assertTrue(begins[0]["inventory_all_shaders"])
        self.assertEqual(begins[0]["target_raw_sha256s"], [self.raw_sha])
        self.assertIn(
            "resource-inventory", [event["phase"] for event in progress]
        )

        # With no RDC association, a second run stays a narrow target lookup;
        # a subset is never promoted into the full-inventory sidecar cache.
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ):
            second, second_host, unused_replay, second_progress = self._run(
                index_present=False,
                auto_prepare=True,
            )
        self.assertTrue(second["completed"])
        self.assertFalse(second["inventoryCache"]["hit"])
        second_begins = [
            params
            for method, params in second_host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(second_begins), 1)
        self.assertEqual(second_begins[0]["target_raw_sha256s"], [self.raw_sha])
        self.assertEqual(second_begins[0]["expected_shaders"], [])
        self.assertIn(
            "resource-inventory",
            [event["phase"] for event in second_progress],
        )

    def test_sidecar_target_mismatch_falls_back_before_apply(self) -> None:
        document = self._shader_inventory_sidecar_document()
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ), mock.patch.object(
            project_restore,
            "load_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ), mock.patch.object(
            project_restore,
            "_resource_index_job",
            side_effect=project_restore.CatalogPreparationRequired(
                "fresh ABI changed", "shader-resource-index-interface-mismatch"
            ),
        ), mock.patch.object(
            project_restore,
            "write_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ):
            result, host, unused_replay, progress = self._run(
                index_present=False,
                auto_prepare=True,
            )

        self.assertTrue(result["completed"])
        self.assertFalse(result["inventoryCache"]["hit"])
        self.assertIn(
            "live-validation-failed",
            result["inventoryCache"]["reason"],
        )
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.apply_calls, 1)
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertEqual(len(begins), 1)
        self.assertTrue(begins[0]["inventory_all_shaders"])
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertLess(
            phases.index("resource-inventory"), phases.index("shader-apply")
        )

    def test_sidecar_poll_failure_never_expands_to_full_inventory(self) -> None:
        document = self._shader_inventory_sidecar_document()
        with mock.patch.object(
            project_restore,
            "_shader_inventory_cache_binding",
            return_value=(self.project_id, self.scope_sha),
        ), mock.patch.object(
            project_restore,
            "load_shader_inventory_cache",
            return_value=copy.deepcopy(document),
        ), mock.patch.object(
            project_restore,
            "_resource_index_job",
            side_effect=project_restore.CatalogPreparationRequired(
                "bounded bridge poll timed out",
                "shader-resource-index-timeout",
            ),
        ), mock.patch.object(
            project_restore, "_inventory_shader_resources"
        ) as full_inventory:
            result, unused_host, unused_replay, unused_progress = self._run(
                index_present=False, auto_prepare=True
            )

        full_inventory.assert_not_called()
        self.assertFalse(result["completed"])
        self.assertEqual(result["outcome"], "action-required")
        self.assertEqual(
            result["reasonId"], "shader-resource-index-timeout"
        )

    def test_v2_cache_identity_accepts_legacy_project_catalog_only_before_live_job(
        self,
    ) -> None:
        legacy = {
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "interfaceComplete": True,
            "eventIds": [9],
        }

        matched = project_restore._capture_shader(
            {"capture": {"shaders": [legacy]}}, self.identity
        )

        self.assertEqual(matched, legacy)

    def test_cache_hit_ignores_legacy_material_values_and_rewrites_empty_materials(
        self,
    ) -> None:
        material_records = [
            {
                "knowledgeId": "6" * 32,
                "shaderResourceId": 7,
                "stage": "fragment",
                "path": "PerMaterial",
                "layoutSha256": self.material_layout_sha,
                "eventId": 9,
                "blockIndex": 0,
                "parameters": [{"parameterId": "7" * 32}],
            }
        ]
        self._refresh_exact_binding(self.binding["shaders"], material_records)

        result, host, unused_replay, progress = self._run()

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(
            result["materials"],
            {
                "attempted": 0,
                "restored": 0,
                "skipped": 0,
                "failed": 0,
                "sessionIds": [],
                "items": [],
            },
        )
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertEqual(host.material_rollback_calls, 0)
        self.assertNotIn("material-restore", [event["phase"] for event in progress])
        self.assertEqual(host.ipc.index["bindings"][0]["materials"], [])

    def test_cached_duplicate_resource_is_rejected_before_workspace_validation(
        self,
    ) -> None:
        duplicate = copy.deepcopy(self.shader_record)
        duplicate["managedWorkspace"] = {
            "workspaceId": "e" * 32,
            "path": "workspaces/shaders/duplicate",
        }
        binding = copy.deepcopy(self.binding)
        binding["shaders"].append(duplicate)

        with mock.patch.object(
            project_restore, "_validate_workspace_record"
        ) as validate_workspace:
            with self.assertRaisesRegex(
                RuntimeError,
                "one physical Shader resource more than once",
            ):
                project_restore._restore_from_cache(
                    object(),
                    object(),
                    copy.deepcopy(self.active),
                    self.project,
                    self.capture_path,
                    self.session_id,
                    binding,
                    {},
                    {},
                    mock.Mock(),
                    {"attempted": 0},
                    {"attempted": 0},
                    [],
                    [],
                    [],
                )

        validate_workspace.assert_not_called()

    def test_binding_document_rejects_two_executables_for_one_resource(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "one physical Shader resource more than once",
        ):
            project_restore._binding_document(
                self.active,
                {
                    "projectId": self.project_id,
                    "scopeSha256": self.scope_sha,
                    "captureId": self.capture_id,
                    "catalogSha256": self.catalog_sha,
                    "knowledgeContentSha256": self.knowledge_sha,
                    "matcherPolicySha256": self.matcher_sha,
                },
                [self.shader_record, copy.deepcopy(self.shader_record)],
                [],
            )

    def test_project_inventory_is_canonical_for_real_project_service(self) -> None:
        result, host, replay, unused_progress = self._run(
            index_present=False, auto_prepare=True
        )

        self.assertTrue(result["completed"])
        projected = replay.last_inventory_snapshot["inventory"]
        real_project = self.root / "canonical-replay-project"
        project_restore.ReplayProjectService.create(
            real_project,
            name="canonical inventory",
            game_id="game",
            build_id="build",
            api_family="d3d11",
        )
        snapshot = project_restore.ReplayProjectService(
            real_project
        ).reuse_snapshot_for_shader_inventory(projected)

        self.assertEqual(snapshot["inventory"], projected)
        self.assertEqual(snapshot["inventory"]["shaders"][0]["encoding"], "DXBC")
        project_identity = project_restore._normal_shader_identity(snapshot["inventory"]["shaders"][0])
        self.assertTrue(project_identity["stableKeyComplete"])
        self.assertEqual(
            snapshot["inventory"]["shaders"][0]["stableKeySha256"],
            self.stable_key_sha,
        )

        # Regress the real failure mode: Bridge protocol encoding is lowercase
        # ``dxil`` while the persistent project contract canonicalises it to
        # ``DXIL``.  The strict response equality check must still pass without
        # weakening the seven-field stable key.
        dxil_stable_key = project_restore._canonical_sha256(
            {
                "api": "d3d12",
                "encoding": "dxil",
                "rawSha256": self.raw_sha,
                "stage": "fragment",
                "entryPoint": "main",
                "abiHash": self.live_abi_sha,
                "layoutHash": self.layout_sha,
            }
        )
        host.ipc.inventory_assets[0].update(
            {
                "api": "d3d12",
                "encoding": "dxil",
                "stableKeySha256": dxil_stable_key,
            }
        )
        host.ipc.resource_index_snapshot = None
        live_dxil = project_restore._inventory_shader_resources(
            host,
            self.capture_path,
            self.session_id,
            "d3d12",
            project_restore._Progress("3" * 32, None, []),
        )
        dxil_project = self.root / "canonical-dxil-replay-project"
        dxil_status = project_restore.ReplayProjectService.create(
            dxil_project,
            name="canonical DXIL inventory",
            game_id="game",
            build_id="build",
            api_family="d3d12",
        )
        prepared = project_restore._prepare_inventory_response(
            project_restore.ReplayProjectService(dxil_project),
            {"replayProject": {"projectId": dxil_status["projectId"]}},
            live_dxil,
            project_restore._Progress("4" * 32, None, []),
        )

        self.assertEqual(prepared["inventory"]["shaders"][0]["encoding"], "DXIL")
        project_identity = project_restore._normal_shader_identity(prepared["inventory"]["shaders"][0])
        self.assertTrue(project_identity["stableKeyComplete"])
        self.assertEqual(
            prepared["inventory"]["shaders"][0]["stableKeySha256"],
            dxil_stable_key,
        )
        self.assertEqual(prepared["reusePlan"]["matches"], [])

    def test_project_inventory_completeness_does_not_elevate_missing_or_false_evidence(self):
        identity = copy.deepcopy(self.identity)
        for complete in (None, False):
            with self.subTest(projectComplete=complete):
                value = {key: item for key, item in identity.items() if key != "stableKeyComplete"}
                if complete is not None:
                    value["interfaceComplete"] = complete
                normal = project_restore._normal_shader_identity(value)
                self.assertFalse(normal["stableKeyComplete"])
                self.assertFalse(project_restore._knowledge_matches_shader(normal, identity, executable=True))
        for complete in (None, False):
            with self.subTest(liveComplete=complete):
                value = {**identity, "interfaceComplete": True, "stableKeyComplete": complete}
                self.assertFalse(project_restore._normal_shader_identity(value)["stableKeyComplete"])
        for field in ("abiHash", "layoutHash"):
            with self.subTest(missing=field):
                value = {key: item for key, item in identity.items() if key != "stableKeyComplete"}
                value.update(interfaceComplete=True)
                value.pop(field)
                with self.assertRaisesRegex(RuntimeError, "complete Shader stable key omitted"):
                    project_restore._normal_shader_identity(value)

    def test_project_inventory_complete_flag_survives_apply_and_publication(self):
        original = project_restore._prepare_inventory_response
        def project_schema(*args, **kwargs):
            prepared = original(*args, **kwargs)
            for match in prepared["reusePlan"]["matches"]:
                shader = match["shader"]
                shader["interfaceComplete"] = shader.pop("stableKeyComplete")
            return prepared
        with mock.patch.object(project_restore, "_prepare_inventory_response", side_effect=project_schema):
            result, host, unused_replay, unused_progress = self._run(
                index_present=False, auto_prepare=True, with_binary=True)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(result["shaders"]["failed"], 0, result)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertTrue(result["index"]["written"])

    def test_unknown_raw_identity_does_not_claim_project_targets_absent(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_unindexed_resource_ids=[8]
        )

        self.assertFalse(result["completed"])
        self.assertEqual(result["reasonId"], "shader-resource-index-targets-incomplete")
        self.assertEqual(result["shaders"]["applied"], 0)
        inventory = host.ipc.resource_index_snapshot["result"]
        self.assertEqual(inventory["uniqueShaderResourceCount"], 2)
        self.assertEqual(inventory["indexedShaderResourceCount"], 1)
        self.assertEqual(inventory["unindexedShaderResourceCount"], 1)
        self.assertEqual(inventory["unindexedShaderResourceIds"], [8])
        completed = [
            event
            for event in progress
            if event["phase"] == "resource-inventory"
            and event["message"] == "Unique Shader resource inventory completed"
        ][-1]
        self.assertEqual((completed["current"], completed["total"]), (2, 2))
        self.assertEqual(completed["detail"]["indexedShaderResourceCount"], 1)
        self.assertEqual(completed["detail"]["unindexedShaderResourceCount"], 1)

    def test_unindexed_shader_ledger_fails_closed_when_resource_is_also_indexed(
        self,
    ) -> None:
        result, unused_host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_unindexed_resource_ids=[7]
        )

        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-invalid-response",
        )
        self.assertFalse(result["completed"])

    def test_legacy_v2_inventory_without_ledger_remains_compatible_when_complete(
        self,
    ) -> None:
        def omit_ledger(result):
            result.pop("indexedShaderResourceCount")
            result.pop("unindexedShaderResourceCount")
            result.pop("unindexedShaderResourceIds")

        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_result_mutator=omit_ledger
        )

        # The old unfiltered API still accepts complete v2 evidence; the new
        # target API requires its explicit coverage ledger instead.
        self.assertFalse(result["completed"])
        inventory = project_restore._inventory_shader_resources(
            host, self.capture_path, self.session_id, "D3D11",
            project_restore._Progress("a" * 32, None, []))
        self.assertEqual(len(inventory["shaders"]), 1)

    def test_legacy_v2_inventory_without_ledger_rejects_unreported_gap(self) -> None:
        def omit_ledger(result):
            result.pop("indexedShaderResourceCount")
            result.pop("unindexedShaderResourceCount")
            result.pop("unindexedShaderResourceIds")

        result, unused_host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            inventory_unindexed_resource_ids=[8],
            inventory_result_mutator=omit_ledger,
        )

        self.assertFalse(result["completed"])
        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-invalid-response",
        )

    def test_malformed_unindexed_shader_ledgers_have_one_fail_closed_reason(
        self,
    ) -> None:
        def partial(result):
            result.pop("unindexedShaderResourceIds")

        def non_list(result):
            result["unindexedShaderResourceIds"] = "8"

        def invalid_resource_id(result):
            result["uniqueShaderResourceCount"] = 2
            result["processedShaderResourceCount"] = 2
            result["unindexedShaderResourceCount"] = 1
            result["unindexedShaderResourceIds"] = [0]

        def duplicate_resource_id(result):
            result["uniqueShaderResourceCount"] = 3
            result["processedShaderResourceCount"] = 3
            result["unindexedShaderResourceCount"] = 2
            result["unindexedShaderResourceIds"] = [8, 8]

        for label, mutate in {
            "partial": partial,
            "non-list": non_list,
            "invalid-resource-id": invalid_resource_id,
            "duplicate-resource-id": duplicate_resource_id,
        }.items():
            with self.subTest(label=label):
                result, unused_host, unused_replay, unused_progress = self._run(
                    index_present=False,
                    auto_prepare=True,
                    inventory_result_mutator=mutate
                )
                self.assertFalse(result["completed"])
                self.assertEqual(
                    result["reasonId"],
                    "shader-resource-index-invalid-response",
                )

    def test_incomplete_bridge_inventory_preserves_exact_failure_reason(self) -> None:
        def stopped(result):
            result.update(inventoryComplete=False, matched=False,
                          reasonId="shader-resource-index-reflection-field-limit",
                          diagnostics=[{"code": "shader-resource-index-reflection-field-limit",
                                        "resourceId": 7, "stage": "fragment", "entryPoint": "main"}])
            result.pop("symbolSnapshotPublished", None)

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, inventory_result_mutator=stopped)
        self.assertFalse(result["completed"])
        self.assertEqual(result["reasonId"], "shader-resource-index-reflection-field-limit")
        self.assertIn('"resourceId": 7', result["reasonDetail"])
        self.assertEqual(host.apply_calls, 0)

    def test_inventory_failure_cannot_override_mismatched_capture_identity(self) -> None:
        def wrong_capture(result):
            result.update(captureSessionId="f" * 32, inventoryComplete=False, matched=False,
                          reasonId="shader-resource-index-reflection-field-limit")
            result.pop("symbolSnapshotPublished", None)

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, inventory_result_mutator=wrong_capture)
        self.assertFalse(result["completed"])
        self.assertEqual(result["reasonId"], "shader-resource-index-invalid-response")
        self.assertEqual(host.apply_calls, 0)

    def test_physical_shader_resource_count_obeys_inventory_limit(self) -> None:
        with mock.patch.object(project_restore, "_MAX_FAST_CATALOG_SHADERS", 1):
            result, unused_host, unused_replay, unused_progress = self._run(
                index_present=False,
                auto_prepare=True,
                inventory_unindexed_resource_ids=[8]
            )

        self.assertFalse(result["completed"])
        self.assertEqual(
            result["reasonId"],
            "shader-resource-index-invalid-response",
        )

    def test_cache_hit_reloads_project_alias_overlay_before_shader_apply(self) -> None:
        result, host, replay, progress = self._run(
            with_aliases=True,
            require_alias_before_apply=True,
        )

        self.assertTrue(result["completed"])
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(replay.reuse_plan_calls, 1)
        self.assertEqual(replay.inventory_snapshot_calls, 0)
        self.assertEqual(replay.materialize_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("load_symbol_aliases", methods)
        load = next(
            params
            for method, params in host.ipc.calls
            if method == "load_symbol_aliases"
        )
        self.assertTrue(load["confirmed_only"])
        self.assertTrue(load["strict_atomic"])
        self.assertTrue(Path(load["path"]).is_file())
        phases = [event["phase"] for event in progress]
        self.assertIn("alias-restore", phases)
        self.assertLess(phases.index("alias-restore"), phases.index("shader-apply"))

    def test_apply_rejects_forged_abi_and_replacement_identity_without_reindexing(self) -> None:
        mutations = {
            "abi-pending": lambda response: response["validation"].update(
                {
                    "compatible": False,
                    "requiresReplayAbiValidation": True,
                    "replayAbiValidated": False,
                }
            ),
            "foreign-resource": lambda response: response["replacement"].update(
                {"originalResourceId": 404}
            ),
            "foreign-capture": lambda response: response["replacement"].update(
                {"captureId": "4" * 64}
            ),
            "foreign-anchor": lambda response: response["replacement"].update(
                {"anchorEventId": 77}
            ),
            "invalid-id": lambda response: response["replacement"].update(
                {"replacementId": "not-a-replacement"}
            ),
            "foreign-stage": lambda response: response["binding"].update(
                {"stage": "vertex"}
            ),
            "baseline-abi-mismatch": lambda response: response["binding"].update(
                {"baselineAbiSha256": "4" * 64}
            ),
            "candidate-abi-mismatch": lambda response: response[
                "validation"
            ].update({"candidateAbiSha256": "4" * 64}),
            "candidate-bytes-mismatch": lambda response: response["binding"].update(
                {"candidateRawSha256": "4" * 64}),
            "report-path-mismatch": lambda response: response["validation"].update(
                {"abiReportPath": str(self.workspace_status["workspacePath"]) + "/other-report.json"}),
            "reported-digest-mismatch": lambda response: (
                response["validation"].update({"abiReportSha256": "4" * 64}),
                response["replacement"].update({"abiReportSha256": "4" * 64})),
            "request-cross-binding": lambda response: response[
                "replacement"
            ].update({"abiRequestId": "4" * 64}),
            "report-cross-binding": lambda response: response[
                "replacement"
            ].update({"abiReportSha256": "4" * 64}),
            "physical-abi-mismatch": lambda response: response[
                "replacement"
            ].update({"physicalAbiSha256": "4" * 64}),
            "foreign-rollback": lambda response: response["rollback"].update(
                {"replacementId": "4" * 32}
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                original_index = copy.deepcopy(self.index)
                result, host, unused_replay, progress = self._run(
                    apply_response_mutator=mutation
                )

                self.assertTrue(result["completed"])
                self.assertEqual(result["shaders"]["applied"], 0)
                self.assertEqual(result["shaders"]["failed"], 1)
                self.assertIn(
                    "invalid live ABI/replacement evidence",
                    result["shaders"]["items"][0]["error"],
                )
                self.assertFalse(result["index"]["written"])
                self.assertEqual(host.ipc.index, original_index)
                methods = [method for method, unused in host.ipc.calls]
                self.assertNotIn("write_rdc_project_index", methods)
                self.assertEqual(progress[-1]["phase"], "complete")

    def test_partial_cache_apply_preserves_full_binding_for_retry(self) -> None:
        second_status = self._add_second_cached_shader()
        original_index = copy.deepcopy(self.index)

        result, host, replay, progress = self._run(
            extra_workspace_statuses=[second_status],
            apply_error_workspace_ids={second_status["workspaceId"]},
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["attempted"], 2)
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertFalse(result["index"]["written"])
        self.assertEqual(result["realizedReceipts"]["persisted"], 0)
        self.assertIn("preserved for retry", result["index"]["warning"])
        self.assertEqual(host.ipc.index, original_index)
        self.assertEqual(
            host.ipc.index["bindings"][0]["realizedApplySnapshot"],
            original_index["bindings"][0]["realizedApplySnapshot"],
        )
        methods = [method for method, unused in host.ipc.calls]
        self.assertNotIn("write_rdc_project_index", methods)
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_partial_restore_keeps_each_successful_signed_shader_pair(self):
        second_status = self._add_second_cached_shader()

        def signed(host, replay):
            self._ticketed_restore_host(host, replay)
            host.expected_anchors = {second_status["workspaceId"]: 10}

        result, host, unused_replay, unused_progress = self._run(
            extra_workspace_statuses=[second_status],
            apply_error_workspace_ids={second_status["workspaceId"]},
            host_mutator=signed,
        )
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(result["shaders"]["failed"], 1, result)
        self.assertFalse(result["index"]["written"])
        proof = project_restore._load_shader_pair_validation(
            self.project, host.workspace_service._status)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["pair"]["candidateRawSha256"], self.candidate_sha)
        # Both resources deliberately contain the same original/candidate
        # bytes. One successful pair can prove those bytes for either owner;
        # it must not invent a successful receipt for the failed second owner.
        other = project_restore._load_shader_pair_validation(self.project, second_status)
        self.assertEqual(other["replacement"]["originalResourceId"], self.shader_record["identity"]["resourceId"])
        self.assertEqual(len(list((self.replay_path / "derived" / "validated-shader-pairs").rglob("*.json"))), 1)

    def test_cached_shader_pair_cannot_follow_changed_source_candidate_or_profile(self):
        result, host, unused_replay, unused_progress = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        original = copy.deepcopy(host.workspace_service._status)
        for field in ("source", "candidate", "profile", "original"):
            with self.subTest(changed=field):
                changed = copy.deepcopy(original)
                if field == "source":
                    changed["source"]["currentSha256"] = "5" * 64
                elif field == "candidate":
                    changed["latestCompiled"]["rawSha256"] = "5" * 64
                elif field == "profile":
                    changed["targetProfile"] = {"profile": "ps_4_0"}
                else:
                    changed["baseline"]["rawSha256"] = "5" * 64
                self.assertIsNone(project_restore._load_shader_pair_validation(self.project, changed))

    def test_workspace_local_signed_pair_is_saved_without_project_reads_and_is_content_bound(self):
        result, host, unused_replay, unused_progress = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        status = copy.deepcopy(host.workspace_service._status)
        replacement = host.last_apply_response["replacement"]
        lease = {"abiLeaseRequestId": replacement["abiRequestId"],
                 "abiLeaseReportSha256": replacement["abiReportSha256"],
                 "abiLeaseCandidateRawSha256": replacement["candidateRawSha256"],
                 "abiLeaseRevision": replacement["revision"]}
        with mock.patch.object(host, "_active_project_service", side_effect=AssertionError("local proof must not read project status")):
            self.assertTrue(project_restore._persist_workspace_shader_pair_validation(status, replacement, lease))
        cached = project_restore._load_workspace_shader_pair_validation(status)
        self.assertEqual(cached["replacement"]["replacementId"], replacement["replacementId"])
        for field in ("source", "candidate", "profile", "original"):
            with self.subTest(changed=field):
                changed = copy.deepcopy(status)
                if field == "source":
                    changed["source"]["currentSha256"] = "5" * 64
                elif field == "candidate":
                    changed["latestCompiled"]["rawSha256"] = "5" * 64
                elif field == "profile":
                    changed["targetProfile"] = {"profile": "ps_4_0"}
                else:
                    changed["baseline"]["rawSha256"] = "5" * 64
                self.assertIsNone(project_restore._load_workspace_shader_pair_validation(changed))
        local_path = project_restore._workspace_shader_pair_validation_path(status)
        local_path.unlink()
        invalid = copy.deepcopy(replacement)
        invalid["restoreValidationReceipt"]["signature"] = "invalid"
        self.assertFalse(project_restore._persist_workspace_shader_pair_validation(status, invalid, lease))
        self.assertFalse(local_path.exists())
        local_path.write_text("{broken", encoding="utf-8")
        self.assertIsNotNone(project_restore._load_shader_pair_validation(self.project, status))

    def test_restore_lifecycle_uses_real_lightweight_snapshot_and_detects_project_switch(self):
        from src.active_projects import ActiveReplayProjectService
        service = ActiveReplayProjectService(self.root / "selection.json", self.root / "projects")
        # Historical capture import stores the enum spelling with punctuation
        # removed; comparison must use the shared derived API normalizer.
        first = service.create_project(name="First", game_id="game", build_id="build", api_family="GraphicsAPI.D3D11")
        first_root = Path(first["projectRoot"])
        other = service.create_project(name="Other", game_id="other", build_id="build", api_family="d3d11")
        service.activate(first_root)
        active = service.status()
        self.assertEqual(active["replayProject"]["scope"]["apiFamily"], "graphicsapid3d11")
        replay_manifest = Path(active["paths"]["replayProject"]) / "project.json"
        before_manifest = replay_manifest.read_bytes()
        host = _Host(active, self.workspace_status, self.capture_path, self.session_id, self.index, self.identity_evidence)
        host._active_project_service = lambda: service
        host._require_same_active_project = mock.Mock(side_effect=AssertionError("full status race guard must not run"))
        original_context = host._active_live_context
        with mock.patch.object(service, "status", side_effect=AssertionError("full project artifact status must not run")):
            self.assertEqual(project_restore._require_restore_context(
                host, active, first_root, self.capture_path, self.session_id)["workspaceId"], active["workspaceId"])
            for change in ("session", "path", "api", "project"):
                with self.subTest(change=change):
                    def context(**kwargs):
                        value = original_context(**kwargs)
                        if change == "session":
                            value["capture"]["captureSessionId"] = "e" * 32
                        elif change == "path":
                            value["capture"]["path"] = str(self.root / "other.rdc")
                        elif change == "api":
                            value["capture"]["api"] = "GraphicsAPI.Vulkan"
                        else:
                            service.activate(other["projectRoot"])
                        return value
                    host._active_live_context = context
                    with self.assertRaisesRegex(project_restore.RestoreContextChanged, "active project or capture changed"):
                        project_restore._require_restore_context(host, active, first_root, self.capture_path, self.session_id)
                    service.activate(first_root)
                    host._active_live_context = original_context
        host._require_same_active_project.assert_not_called()
        self.assertEqual(replay_manifest.read_bytes(), before_manifest)

    def test_legacy_current_source_proof_preserves_older_physical_pair_without_prior_local_cache(self):
        result, host, unused_replay, unused = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        status = copy.deepcopy(host.workspace_service._status)
        replacement = copy.deepcopy(host.last_apply_response["replacement"])
        lease = {"abiLeaseRequestId": replacement["abiRequestId"],
                 "abiLeaseReportSha256": replacement["abiReportSha256"],
                 "abiLeaseCandidateRawSha256": replacement["candidateRawSha256"],
                 "abiLeaseRevision": replacement["revision"]}
        status["revision"] += 1
        status["latestCompiled"].update(revision=status["revision"], sourceSha256="4" * 64)
        status["source"]["currentSha256"] = "4" * 64
        payload = replacement["restoreValidationReceipt"]["payload"]
        payload["sourceSha256"] = "4" * 64
        payload.pop("targetProfile", None)
        path = project_restore._workspace_shader_pair_validation_path(status)
        if path.exists():
            path.unlink()
        self.assertTrue(project_restore._persist_workspace_shader_pair_validation(
            status, replacement, lease, allow_same_pair_revision_reuse=True))
        saved = project_restore._load_workspace_shader_pair_validation(status)
        self.assertEqual(saved["sourceSha256"], "4" * 64)
        self.assertEqual(saved["replacement"], replacement)
        self.assertLess(saved["replacement"]["revision"], status["revision"])
        before = path.read_bytes()
        for field in ("signed-source", "source-compile", "candidate", "profile", "receipt"):
            with self.subTest(changed=field):
                changed_status, changed_replacement = copy.deepcopy(status), copy.deepcopy(replacement)
                if field == "signed-source":
                    changed_replacement["restoreValidationReceipt"]["payload"]["sourceSha256"] = "5" * 64
                elif field == "source-compile":
                    changed_status["source"]["currentSha256"] = "5" * 64
                elif field == "candidate":
                    changed_status["latestCompiled"]["rawSha256"] = "5" * 64
                elif field == "profile":
                    changed_replacement["restoreValidationReceipt"]["payload"]["targetProfile"] = "ps_4_0"
                else:
                    changed_replacement.pop("restoreValidationReceipt")
                self.assertFalse(project_restore._persist_workspace_shader_pair_validation(
                    changed_status, changed_replacement, lease, allow_same_pair_revision_reuse=True))
                self.assertEqual(path.read_bytes(), before)

    def test_signed_native_profile_token_matches_canonical_compiled_profile(self):
        result, host, unused_replay, unused = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        status = host.workspace_service._status
        replacement = copy.deepcopy(host.last_apply_response["replacement"])
        lease = {"abiLeaseRequestId": replacement["abiRequestId"],
                 "abiLeaseReportSha256": replacement["abiReportSha256"],
                 "abiLeaseCandidateRawSha256": replacement["candidateRawSha256"],
                 "abiLeaseRevision": replacement["revision"]}
        replacement["restoreValidationReceipt"]["payload"]["targetProfile"] = status["targetProfile"]["profile"]
        self.assertTrue(project_restore._persist_workspace_shader_pair_validation(status, replacement, lease))
        self.assertIsNotNone(project_restore._load_workspace_shader_pair_validation(status))
        replacement["restoreValidationReceipt"]["payload"]["targetProfile"] = "wrong_profile"
        self.assertFalse(project_restore._persist_workspace_shader_pair_validation(status, replacement, lease))

    def test_inline_save_local_proof_preserves_new_request_and_initial_lease(self):
        result, host, unused_replay, unused = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        status = copy.deepcopy(host.workspace_service._status)
        replacement = copy.deepcopy(host.last_apply_response["replacement"])
        lease = {"abiLeaseRequestId": replacement["abiRequestId"],
                 "abiLeaseReportSha256": replacement["abiReportSha256"],
                 "abiLeaseCandidateRawSha256": replacement["candidateRawSha256"],
                 "abiLeaseRevision": replacement["revision"]}
        status["revision"] += 1
        status["source"]["currentSha256"] = "6" * 64
        status["latestCompiled"].update(revision=status["revision"], sourceSha256="6" * 64,
                                        rawSha256="5" * 64, abiGate={"status": "pending"})
        replacement.update(revision=status["revision"], candidateRawSha256="5" * 64,
                           abiRequestId="7" * 64, abiValidationMode="inline-physical-with-initial-lease",
                           freshPhysicalAbiValidationPerformed=True, **lease)
        replacement["restoreValidationReceipt"]["payload"].update(
            revision=status["revision"], candidateRawSha256="5" * 64,
            sourceSha256="6" * 64, abiRequestId="7" * 64)
        self.assertNotEqual(replacement["abiRequestId"], lease["abiLeaseRequestId"])
        self.assertTrue(project_restore._persist_workspace_shader_pair_validation(status, replacement, lease))
        saved = project_restore._load_workspace_shader_pair_validation(status)
        self.assertEqual(saved["replacement"]["abiRequestId"], "7" * 64)
        self.assertEqual(saved["replacement"]["restoreValidationReceipt"]["payload"]["abiRequestId"], "7" * 64)
        self.assertEqual(saved["abiLease"], lease)
        # Storage of authenticated-origin evidence does not turn a pending
        # current candidate report into compatible restore authority.
        self.assertIsNone(project_restore._load_shader_pair_validation(self.project, status))
        path = project_restore._workspace_shader_pair_validation_path(status)
        before = path.read_bytes()
        for field in ("abiLeaseRequestId", "abiLeaseCandidateRawSha256", "signedRequest", "freshPhysical"):
            with self.subTest(changed=field):
                malformed = copy.deepcopy(replacement)
                if field == "signedRequest":
                    malformed["restoreValidationReceipt"]["payload"]["abiRequestId"] = "8" * 64
                elif field == "freshPhysical":
                    malformed["freshPhysicalAbiValidationPerformed"] = False
                else:
                    malformed[field] = "8" * 64
                self.assertFalse(project_restore._persist_workspace_shader_pair_validation(status, malformed, lease))
                self.assertEqual(path.read_bytes(), before)

    def test_old_snapshot_routes_unchanged_second_shader_after_first_source_changes(self):
        second = self._add_second_cached_shader()
        second["source"].update(lane="executable-editable", format="hlsl")
        second_knowledge = self.state["workspaces"]["b" * 32]
        second_knowledge.update(api="d3d11", encoding="dxbc", targetProfile=copy.deepcopy(second["targetProfile"]), registeredWorkspace=None)
        self._refresh_exact_binding(self.binding["shaders"], self.binding["materials"])
        def signed(host, replay):
            self._ticketed_restore_host(host, replay)
            host.expected_anchors = {second["workspaceId"]: 10}
            replay.reuse_plan_response = {
                "schema": "renderdoc-ultra-project-reuse-plan/v1", "schemaVersion": 1,
                "projectId": self.project_id, "captureId": self.capture_id,
                "catalogSha256": self.catalog_sha, "matchedShaderCount": 2,
                "matches": [{"shader": copy.deepcopy(identity), "workspaceCandidates": [{
                    "knowledgeId": knowledge_id, "activeCandidate": True, "sourceSeedEligible": True}]}
                    for identity, knowledge_id in zip(self.state["capture"]["shaders"], (self.knowledge_id, "b" * 32))],
            }
        result, host, replay, unused = self._run(extra_workspace_statuses=[second], host_mutator=signed)
        self.assertEqual(result["shaders"]["applied"], 2, result)
        self.assertTrue(result["verifiedRdcCache"]["written"], result)
        host.workspace_service._status["source"]["currentSha256"] = "6" * 64
        subset, statuses = project_restore._load_verified_rdc_restore(
            host, replay, self.active, self.project, self.replay_path,
            self.capture_path, lambda phase: None, host.restore_validation_policy_sha256)
        self.assertTrue(subset["_partialRecovery"])
        self.assertEqual([item["workspaceId"] for item in statuses], [second["workspaceId"]])
        self.assertEqual(len(subset["_individualRecovery"]), 1)
        previous_apply_calls = host.apply_calls
        host.get_active_shader_workspace_path = mock.Mock(side_effect=AssertionError("unchanged second source must keep its known route"))
        identity = self.state["capture"]["shaders"][1]
        diagnostic = {}
        reused = project_restore._prepare_reuse_workspace(
            host, self.active, identity, self.session_id, self.catalog_sha,
            expected_anchor_event_id=10, expected_source_sha256=self.source_sha,
            inventory_mode=True, registered_knowledge=second_knowledge,
            capture_path=self.capture_path, diagnostic=diagnostic)
        self.assertEqual(reused[0], Path(second["workspacePath"]))
        self.assertTrue(diagnostic["reusedPriorSnapshotWorkspace"])
        host.get_active_shader_workspace_path.assert_not_called()
        self.assertEqual(host.apply_calls, previous_apply_calls)
        for change in ("source", "candidate", "profile", "rdc"):
            with self.subTest(change=change):
                original = copy.deepcopy(host.workspace_service.extra_statuses[0])
                if change == "source":
                    host.workspace_service.extra_statuses[0]["source"]["currentSha256"] = "7" * 64
                elif change == "candidate":
                    host.workspace_service.extra_statuses[0]["latestCompiled"]["rawSha256"] = "7" * 64
                elif change == "profile":
                    host.workspace_service.extra_statuses[0]["targetProfile"] = {"profile": "ps_4_0"}
                else:
                    self.capture_path.write_bytes(b"other")
                if change == "source":
                    with self.assertRaises(project_restore.RestoreLocalEdit):
                        project_restore._snapshot_reuse_workspace(
                            host, self.active, identity, second_knowledge, self.capture_path, self.source_sha)
                else:
                    self.assertIsNone(project_restore._snapshot_reuse_workspace(
                        host, self.active, identity, second_knowledge, self.capture_path, self.source_sha))
                host.workspace_service.extra_statuses[0] = original
                self.capture_path.write_bytes(b"rdc")

    def test_registered_unfinished_draft_uses_exact_previous_restore_owner(self):
        self.workspace_status["source"].update(lane="executable-editable", format="hlsl")
        knowledge = self.state["workspaces"][self.knowledge_id]
        knowledge.update(api="d3d11", encoding="dxbc",
                         targetProfile=copy.deepcopy(self.workspace_status["targetProfile"]))
        result, host, replay, unused = self._run(host_mutator=self._signed_single_shader_restore)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertTrue(result["verifiedRdcCache"]["written"], result)
        draft_root = self.workspace.parent / "unfinished-original-owner"
        draft_root.mkdir()
        draft_file = draft_root / "draft.hlsl"
        draft_file.write_bytes(b"unfinished independent local source")
        draft = copy.deepcopy(host.workspace_service._status)
        draft.update(workspacePath=str(draft_root), workspaceId="d" * 32, revision=132)
        draft["source"].update(currentSha256="6" * 64, modifiedSinceDecompile=True)
        draft["latestCompiled"].update(sourceSha256="6" * 64, revision=132,
                                       replayAbiValidated=False, abiCompatible=False,
                                       requiresReplayAbiValidation=True, abiGate={"status": "pending"})
        host.workspace_service.extra_statuses.append(draft)
        knowledge["registeredWorkspace"] = {"path": str(draft_root), "workspaceId": draft["workspaceId"]}
        before = copy.deepcopy(draft)
        host.get_active_shader_workspace_path = mock.Mock(side_effect=AssertionError("known saved owner must be reused"))
        # New policy invalidates fast apply, but not a content-checked route hint.
        host.restore_validation_policy_sha256 = "a" * 64
        prior_applies = host.apply_calls
        restored = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(restored["shaders"]["applied"], 1, restored)
        self.assertEqual(restored["editing"]["enabled"], 0, restored)
        self.assertEqual(restored["editing"]["deferred"], 1, restored)
        self.assertEqual(restored["shaders"]["items"][0]["preservedLocalSource"]["workspacePath"], str(draft_root))
        self.assertEqual(restored["shaders"]["items"][0]["workspacePath"], str(self.workspace))
        self.assertEqual(host.apply_calls, prior_applies + 1)
        self.assertEqual(host.compile_calls, 0)
        host.get_active_shader_workspace_path.assert_not_called()
        self.assertEqual(draft, before)
        self.assertEqual(draft_file.read_bytes(), b"unfinished independent local source")

    def test_registered_unfinished_draft_still_skips_if_previous_owner_changed(self):
        self.workspace_status["source"].update(lane="executable-editable", format="hlsl")
        knowledge = self.state["workspaces"][self.knowledge_id]
        knowledge.update(api="d3d11", encoding="dxbc",
                         targetProfile=copy.deepcopy(self.workspace_status["targetProfile"]))
        result, host, unused_replay, unused = self._run(host_mutator=self._signed_single_shader_restore)
        self.assertTrue(result["verifiedRdcCache"]["written"], result)
        draft_root = self.workspace.parent / "unfinished-original-owner"
        draft_root.mkdir()
        draft = copy.deepcopy(host.workspace_service._status)
        draft.update(workspacePath=str(draft_root), workspaceId="d" * 32)
        draft["source"].update(currentSha256="6" * 64, modifiedSinceDecompile=True)
        draft["latestCompiled"] = None
        host.workspace_service.extra_statuses.append(draft)
        knowledge["registeredWorkspace"] = {"path": str(draft_root), "workspaceId": draft["workspaceId"]}
        host.get_active_shader_workspace_path = mock.Mock(side_effect=AssertionError("never overwrite the protected draft"))
        for change in ("source", "candidate", "capture"):
            with self.subTest(change=change):
                original = copy.deepcopy(host.workspace_service._status)
                if change == "source":
                    host.workspace_service._status["source"].update(currentSha256="7" * 64, modifiedSinceDecompile=True)
                elif change == "candidate":
                    host.workspace_service._status["latestCompiled"]["rawSha256"] = "7" * 64
                else:
                    self.capture_path.write_bytes(b"different capture")
                with self.assertRaises(project_restore.RestoreLocalEdit) as caught:
                    project_restore._prepare_reuse_workspace(
                        host, self.active, self.identity, self.session_id, self.catalog_sha,
                        expected_anchor_event_id=self.identity["anchorEventId"],
                        expected_source_sha256=self.source_sha, inventory_mode=True,
                        registered_knowledge=knowledge, capture_path=self.capture_path)
                self.assertEqual(caught.exception.workspace, draft_root)
                host.workspace_service._status = original
                self.capture_path.write_bytes(b"rdc")
        host.get_active_shader_workspace_path.assert_not_called()

    def test_validated_replay_revision_does_not_overwrite_new_pending_text(self):
        result, host, replay, unused = self._run(host_mutator=self._signed_single_shader_restore)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        knowledge = self.state["workspaces"][self.knowledge_id]
        knowledge.update(targetProfile=copy.deepcopy(self.workspace_status["targetProfile"]))
        pending_sha = "6" * 64
        pending = {"knowledgeId": "b" * 32, "sourceSha256": pending_sha}
        pending_status = copy.deepcopy(host.workspace_service._status)
        pending_status["source"].update(currentSha256=pending_sha, modifiedSinceDecompile=True,
                                        lane="executable-editable", format="hlsl")
        pending_status["latestCompiled"] = None
        source_path = self.workspace / "source" / "shader.hlsl"
        source_path.parent.mkdir(exist_ok=True)
        source_path.write_bytes(b"latest pending source must remain intact")
        replay_workspace = project_restore._isolated_replay_workspace_path(self.workspace, self.knowledge_id)
        hosted = copy.deepcopy(host.workspace_service._status)
        hosted.update(workspacePath=str(replay_workspace))
        located = {"schema": "replaylab-active-shader-workspace-path/v1", "schemaVersion": 1,
            "captureId": self.capture_id, "captureSessionId": self.session_id,
            "eventId": self.identity["anchorEventId"], "stage": self.identity["stage"],
            "entryPoint": self.identity["entryPoint"], "shaderResourceId": self.identity["resourceId"],
            "rawSha256": self.identity["rawSha256"], "applied": False,
            "workspacePath": str(self.workspace), "catalogSha256": self.catalog_sha}
        def prepare(event, stage, path, **kwargs):
            self.assertEqual(Path(path), replay_workspace)
            self.assertTrue(kwargs["admit_existing"])
            self.assertTrue(replay_workspace.is_dir())
            return json.dumps({"schema": "renderdoc-ultra-shader-workspace-mcp/v1", "operation": "prepare",
                "result": {"workspacePath": path, "binding": {"workspacePath": path,
                    "captureId": self.capture_id, "captureSessionId": self.session_id,
                    "eventId": event, "stage": stage, "entryPoint": self.identity["entryPoint"],
                    "originalResourceId": self.identity["resourceId"], "originalRawSha256": self.identity["rawSha256"]},
                    "created": {"admittedExisting": True, "sourcePreserved": True}}})
        selected = mock.Mock(capture_id=self.capture_id)
        selected.key.resource_id = self.identity["resourceId"]
        selected.key.raw_sha256 = self.identity["rawSha256"]
        selected.key.stage = self.identity["stage"]
        selected.key.entry_point = self.identity["entryPoint"]
        selected.key.catalog_key = "exact-catalog-shader-key"
        empty_owner = copy.deepcopy(hosted)
        empty_owner["source"]["present"] = False
        def current_status(path):
            return empty_owner if Path(path) == replay_workspace else pending_status
        def create(catalog, key, path, **kwargs):
            self.assertEqual(key, "exact-catalog-shader-key")
            self.assertEqual(kwargs.get("editable_format"), "hlsl")
            Path(path).mkdir()
        with mock.patch.object(project_restore, "_registered_reuse_workspace", side_effect=project_restore.RestoreLocalEdit(
                "pending source", self.workspace, pending_sha)), \
             mock.patch.object(project_restore, "_snapshot_reuse_workspace", return_value=None), \
             mock.patch.object(project_restore, "_reuse_workspace_status", return_value=hosted), \
             mock.patch.object(host.workspace_service, "status", side_effect=current_status), \
             mock.patch.object(host.workspace_service, "create", side_effect=create, create=True), \
             mock.patch.object(host, "_active_capture_catalog_selection", return_value=(
                 {"captureSessionId": self.session_id}, self.project / "catalog.json", selected, "binding"), create=True), \
             mock.patch.object(host, "get_active_shader_workspace_path", return_value=json.dumps(located)), \
             mock.patch.object(host, "prepare_shader_workspace", side_effect=prepare):
            resolved = project_restore._prepare_reuse_workspace(host, self.active, self.identity,
                self.session_id, self.catalog_sha, expected_anchor_event_id=self.identity["anchorEventId"],
                expected_source_sha256=self.source_sha, inventory_mode=True,
                registered_knowledge=knowledge, capture_path=self.capture_path, pending_source=pending)
        self.assertEqual(resolved[0], replay_workspace)
        self.assertEqual(source_path.read_bytes(), b"latest pending source must remain intact")

    def test_old_glsl_owner_is_rebound_to_current_hlsl_before_project_import(self):
        unused_result, host, unused_replay, unused = self._run()
        knowledge = copy.deepcopy(self.state["workspaces"][self.knowledge_id])
        knowledge.update(api=self.identity["api"], encoding=self.identity["encoding"], sourceFormat="hlsl")
        old = copy.deepcopy(host.workspace_service._status)
        old["source"].update(format="glsl", currentSha256="6" * 64,
                             lane="executable-editable", modifiedSinceDecompile=False)
        original = self.workspace / "source" / "shader.glsl"
        original.parent.mkdir(exist_ok=True)
        original.write_bytes(b"old captured GLSL must remain recoverable")
        created = []
        target = copy.deepcopy(host.workspace_service._status)
        target["source"].update(format="hlsl", lane="executable-editable")
        empty = copy.deepcopy(target)
        empty["source"]["present"] = False
        located = {"schema": "replaylab-active-shader-workspace-path/v1", "schemaVersion": 1,
            "captureId": self.capture_id, "captureSessionId": self.session_id,
            "eventId": self.identity["anchorEventId"], "stage": self.identity["stage"],
            "entryPoint": self.identity["entryPoint"], "shaderResourceId": self.identity["resourceId"],
            "rawSha256": self.identity["rawSha256"], "applied": False,
            "workspacePath": str(self.workspace), "catalogSha256": self.catalog_sha}
        selected = mock.Mock(capture_id=self.capture_id)
        for key in ("resource_id", "raw_sha256", "stage", "entry_point"):
            setattr(selected.key, key, self.identity[{
                "resource_id": "resourceId", "raw_sha256": "rawSha256", "stage": "stage", "entry_point": "entryPoint"}[key]])
        selected.key.catalog_key = "exact-catalog-shader-key"
        def create(catalog, key, path, **kwargs):
            self.assertEqual(kwargs["editable_format"], "hlsl")
            self.assertNotEqual(Path(path), self.workspace)
            self.assertLessEqual(len(Path(path).name), 64)
            Path(path).mkdir()
            created.append(Path(path))
            empty.update(workspacePath=str(path))
            target.update(workspacePath=str(path))
        def prepare(event, stage, path, **kwargs):
            self.assertEqual(created, [Path(path)])
            self.assertTrue(kwargs["admit_existing"])
            self.assertEqual(kwargs["editable_format"], "hlsl")
            return json.dumps({"schema": "renderdoc-ultra-shader-workspace-mcp/v1", "operation": "prepare",
                "result": {"workspacePath": path, "binding": {"workspacePath": path,
                    "captureId": self.capture_id, "captureSessionId": self.session_id,
                    "eventId": event, "stage": stage, "entryPoint": self.identity["entryPoint"],
                    "originalResourceId": self.identity["resourceId"], "originalRawSha256": self.identity["rawSha256"]},
                    "created": {"admittedExisting": True, "sourcePreserved": True}}})
        with mock.patch.object(project_restore, "_registered_reuse_workspace", return_value=None), \
             mock.patch.object(project_restore, "_snapshot_reuse_workspace", return_value=None), \
             mock.patch.object(project_restore, "_reuse_workspace_status", side_effect=lambda *a: target), \
             mock.patch.object(host.workspace_service, "status", side_effect=lambda p: old if Path(p) == self.workspace else empty), \
             mock.patch.object(host.workspace_service, "create", side_effect=create, create=True), \
             mock.patch.object(host, "_active_capture_catalog_selection", return_value=(
                 {"captureSessionId": self.session_id}, self.project / "catalog.json", selected, "binding"), create=True), \
             mock.patch.object(host, "get_active_shader_workspace_path", return_value=json.dumps(located)), \
             mock.patch.object(host, "prepare_shader_workspace", side_effect=prepare):
            result = project_restore._prepare_reuse_workspace(host, self.active, self.identity,
                self.session_id, self.catalog_sha, expected_anchor_event_id=self.identity["anchorEventId"],
                expected_source_sha256=self.source_sha, inventory_mode=True,
                registered_knowledge=knowledge, capture_path=self.capture_path)
        self.assertEqual(result[0], created[0])
        self.assertEqual(original.read_bytes(), b"old captured GLSL must remain recoverable")

    def _signed_single_shader_restore(self, host, replay):
        self._ticketed_restore_host(host, replay)
        replay.reuse_plan_response = {
            "schema": "renderdoc-ultra-project-reuse-plan/v1", "schemaVersion": 1,
            "projectId": self.project_id, "captureId": self.capture_id,
            "catalogSha256": self.catalog_sha, "matchedShaderCount": 1,
            "matches": [{"shader": copy.deepcopy(self.identity), "workspaceCandidates": [{
                "knowledgeId": self.knowledge_id, "activeCandidate": True, "sourceSeedEligible": True}]}],
        }

    def test_signed_pair_reopen_skips_compile_and_ordinary_abi_apply(self):
        cold, previous, unused_replay, unused_progress = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(cold["shaders"]["applied"], 1, cold)
        pair_calls = []

        def install_pair(host, replay):
            def apply(status, proof, session, operation, anchor):
                pair_calls.append(operation)
                wrapped = copy.deepcopy(previous.last_apply_response)
                gate = copy.deepcopy(previous.workspace_service._status["latestCompiled"]["abiGate"])
                host.workspace_service._status["latestCompiled"]["abiGate"] = gate
                wrapped["operationId"] = operation
                wrapped["replacement"].update(operationId=operation, captureSessionId=session,
                                               anchorEventId=anchor, abiValidationMode="verified-shader-pair",
                                               freshPhysicalAbiValidationPerformed=False)
                wrapped["validation"].update(validationScope="verified-shader-pair", reusedValidation=True,
                                              captureSessionId=session, freshPhysicalValidationPerformed=False)
                host.ipc.shader_operations[operation] = {
                    "replacementId": wrapped["replacement"]["replacementId"],
                    "workspaceId": status["workspaceId"],
                }
                return wrapped
            host._apply_project_shader_pair_restore = apply

        warm, host, unused_replay, unused_progress = self._run(host_mutator=install_pair)
        self.assertEqual(warm["shaders"]["applied"], 1, warm)
        self.assertEqual(len(pair_calls), 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertTrue(warm["shaders"]["items"][0]["reusedValidation"])
        self.assertEqual(warm["realizedReceipts"]["reusedValidation"], 1)
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("pair_reopen_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        self.assertTrue(client.ReplayLabRestoreHostClient._result(
            warm, str(self.project), str(self.capture_path), self.session_id)["completed"])

    def test_changed_rdc_pair_receipt_is_a_cache_miss_before_any_apply_or_recovery(self):
        cold, previous, unused_replay, unused_progress = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(cold["shaders"]["applied"], 1, cold)
        self.capture_path.write_bytes(self.capture_path.read_bytes() + b"changed optional section")
        pair_apply = mock.Mock(side_effect=AssertionError("stale receipt reached the Bridge"))

        def install(host, replay):
            self._ticketed_restore_host(host, replay)
            host._apply_project_shader_pair_restore = pair_apply

        warm, host, unused_replay, unused_progress = self._run(host_mutator=install)
        self.assertEqual(warm["shaders"]["applied"], 1, warm)
        pair_apply.assert_not_called()
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertFalse(warm["shaders"]["items"][0].get("reusedValidation", False))

    def test_pair_rdc_routing_hint_is_shared_only_within_its_restore_and_file_facts(self):
        status = {"capture": {"capturePath": str(self.capture_path)}}
        with mock.patch.object(project_restore._RESTORE_THREAD_STATE, "pair_rdc_observations", {}, create=True), \
                mock.patch.object(project_restore, "_verified_rdc_content_sha256", return_value="a" * 64) as digest:
            self.assertEqual(project_restore._pair_restore_rdc_hint(status, self.session_id), "a" * 64)
            self.assertEqual(project_restore._pair_restore_rdc_hint(status, self.session_id), "a" * 64)
            self.assertEqual(digest.call_count, 1)
            project_restore._pair_restore_rdc_hint(status, "b" * 32)
            self.assertEqual(digest.call_count, 2)
            self.capture_path.write_bytes(b"different file contents")
            project_restore._pair_restore_rdc_hint(status, self.session_id)
            self.assertEqual(digest.call_count, 3)

    def test_target_pair_snapshot_receipt_counts_pass_real_gui_client(self):
        def signed_pair(host, replay):
            self._ticketed_restore_host(host, replay)
            sign = host.apply_response_mutator

            def response(wrapped):
                sign(wrapped)
                wrapped["validation"].update(
                    validationScope="verified-shader-pair", reusedValidation=True,
                    captureSessionId=self.session_id, freshPhysicalValidationPerformed=False)
                wrapped["replacement"].update(
                    abiValidationMode="verified-shader-pair",
                    freshPhysicalAbiValidationPerformed=False)
            host.apply_response_mutator = response

        result, unused_host, unused_replay, progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=signed_pair)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["restoreMode"], "project-target-inventory")
        self.assertTrue(result["verifiedRdcCache"]["written"])
        self.assertFalse(result["verifiedRdcCache"]["hit"])
        self.assertEqual(result["realizedReceipts"]["reusedValidation"], 1)
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("target_pair_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        accepted = client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)
        self.assertEqual(accepted["shaders"]["applied"], 1)
        missing_count = copy.deepcopy(result)
        missing_count["realizedReceipts"].pop("reusedValidation")
        with self.assertRaisesRegex(client.ReplayLabRestoreClientError,
                                    "verified RDC cache evidence is inconsistent"):
            client.ReplayLabRestoreHostClient._result(
                missing_count, str(self.project), str(self.capture_path), self.session_id)
        messages = [row["message"] for row in progress]
        self.assertLess(messages.index("Publishing verified RDC restore snapshot"),
                        messages.index("Verified RDC restore snapshot published"))
        self.assertLess(messages.index("Verified RDC restore snapshot published"),
                        messages.index("Enabling restored Shader workspaces"))
        self.assertLess(messages.index("Enabling restored Shader workspace editing"),
                        messages.index("Restored Shader workspace editing batch finished"))

    def test_target_inventory_cache_reuses_eids_and_invalidates_exact_changed_rdc(self):
        import os
        result, host, replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        replay.root = self.replay_path
        replay.status = lambda: {"projectId": self.project_id, "scopeSha256": self.scope_sha}
        host._apply_project_shader_pair_restore = mock.Mock()
        progress = project_restore._Progress("a" * 32, None, [])
        first = project_restore._inventory_project_shader_targets(
            host, replay, self.capture_path, self.session_id, "d3d11", progress)
        with mock.patch.object(project_restore, "_inventory_shader_resources",
                               side_effect=AssertionError("unchanged session must not rescan Shader resources")):
            second = project_restore._inventory_project_shader_targets(
                host, replay, self.capture_path, self.session_id, "d3d11", progress)
        self.assertTrue(second["cachedTargetInventory"])
        self.assertEqual(second["capture"]["captureSessionId"], self.session_id)
        self.assertEqual(second["shaders"], first["shaders"])
        self.assertEqual(second["shaders"][0]["anchorEventId"], first["shaders"][0]["anchorEventId"])
        observed = self.capture_path.stat()
        contents = self.capture_path.read_bytes()
        self.capture_path.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
        os.utime(self.capture_path, ns=(observed.st_atime_ns, observed.st_mtime_ns))
        self.assertIsNone(project_restore._project_target_inventory_cache(
            host, replay, self.capture_path, self.session_id, "d3d11",
            replay.restorable_shader_raw_hashes(), None))

    def test_target_inventory_cache_reopen_refreshes_resource_ids_without_merging_equal_bytes(self):
        unused, host, replay, unused_progress = self._run(index_present=False, auto_prepare=True)
        replay.root = self.replay_path
        replay.status = lambda: {"projectId": self.project_id, "scopeSha256": self.scope_sha}
        host._apply_project_shader_pair_restore = mock.Mock()
        progress = project_restore._Progress("a" * 32, None, [])
        first = project_restore._inventory_project_shader_targets(
            host, replay, self.capture_path, self.session_id, "d3d11", progress)
        fresh = copy.deepcopy(first)
        fresh["capture"].update(captureSessionId="f" * 32, inventorySha256="a" * 64,
                                catalogSha256="a" * 64)
        fresh["shaders"][0]["resourceId"] += 1000
        # Equal shader bytes do not imply one current resource owner. Preserve
        # both exact resources and the EID which identified each one.
        another = copy.deepcopy(fresh["shaders"][0])
        another["resourceId"] += 1
        another["anchorEventId"] += 1
        fresh["shaders"].append(another)
        fresh["targetMatchedShaderResourceIds"] = [row["resourceId"] for row in fresh["shaders"]]
        diagnostic = {"hit": False, "written": False, "reason": "absent-or-stale"}
        before_compile = host.compile_calls
        with mock.patch.object(project_restore, "_inventory_shader_resources", return_value=fresh) as index:
            second = project_restore._inventory_project_shader_targets(
                host, replay, self.capture_path, "f" * 32, "d3d11", progress,
                cache_diagnostic=diagnostic)
        index.assert_called_once()
        self.assertEqual(index.call_args.kwargs["target_raw_sha256s"], replay.restorable_shader_raw_hashes())
        self.assertEqual(diagnostic["reason"], "target-inventory-capture-session-changed")
        self.assertNotIn("cachedTargetInventory", second)
        self.assertEqual(second["shaders"], fresh["shaders"])
        self.assertEqual(len(second["shaders"]), 2)
        self.assertEqual(host.compile_calls, before_compile)
        with mock.patch.object(project_restore, "_inventory_shader_resources",
                               side_effect=AssertionError("current session cache must now be reusable")):
            third = project_restore._inventory_project_shader_targets(
                host, replay, self.capture_path, "f" * 32, "d3d11", progress)
        self.assertTrue(third["cachedTargetInventory"])
        self.assertEqual(third["shaders"], fresh["shaders"])

    def test_alias_load_failure_is_reported_without_blocking_shader_restore(self) -> None:
        result, host, replay, progress = self._run(
            with_aliases=True,
            alias_load_error=RuntimeError("alias overlay rejected"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(replay.materialize_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertTrue(result["index"]["written"])
        self.assertTrue(any("alias overlay rejected" in item for item in result["warnings"]))
        methods = [method for method, unused in host.ipc.calls]
        # The recovered binding is byte-for-byte identical, so reopening must
        # not rewrite the RDC section merely because best-effort alias loading
        # reported a display-only warning.
        self.assertNotIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("alias-restore", phases)
        self.assertNotIn("prepare", phases)
        self.assertEqual(phases[-1], "complete")
        failed_alias = [
            event
            for event in progress
            if event["phase"] == "alias-restore"
            and event["detail"].get("status") == "failed"
        ]
        self.assertEqual(len(failed_alias), 1)

    def test_alias_response_lost_after_commit_is_strictly_recovered(self) -> None:
        result, host, unused_replay, progress = self._run(
            with_aliases=True,
            alias_load_error_after_commit=TimeoutError(
                "alias response was lost after commit"
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertTrue(
            any("fail-safe recovered" in warning for warning in result["warnings"])
        )
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("get_symbol_alias_transaction", methods)
        self.assertIn("rollback_symbol_aliases", methods)
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_alias_corrupt_commit_response_is_strictly_recovered(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            with_aliases=True,
            alias_response_mutator=lambda response: response.update(
                {"transactionId": "4" * 32}
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertTrue(
            any("invalid evidence" in warning for warning in result["warnings"])
        )

    def test_alias_commit_then_cancel_rolls_back_total_restore_ledger(self) -> None:
        stopped, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_aliases=True,
            cancel_on_progress_phase="resource-anchor",
            return_control_error=True,
        )

        self.assertIsInstance(stopped, project_restore.RestoreCancelled)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertEqual(host.active_service.pin_depth, 0)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_alias_commit_then_later_failure_rolls_back_with_shader(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_aliases=True,
            switch_project_on_apply=True,
        )

        self.assertFalse(result["completed"])
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(host.ipc.alias_rollback_calls, 1)
        self.assertIsNone(host.ipc.alias_transaction)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_shader_response_lost_after_mutation_uses_operation_recovery(self) -> None:
        original_index = copy.deepcopy(self.index)
        result, host, unused_replay, progress = self._run(
            apply_error_after_commit=TimeoutError(
                "shared Shader apply response was lost"
            )
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertEqual(host.ipc.shader_operation_recovery_calls, 1)
        self.assertEqual(host.ipc.shader_operations, {})
        self.assertFalse(result["index"]["written"])
        self.assertEqual(host.ipc.index, original_index)
        self.assertIn(
            "fail-safe recovered", result["shaders"]["items"][0]["error"]
        )
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_shader_corrupt_response_rolls_back_by_operation_not_forged_id(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            apply_response_mutator=lambda response: response["replacement"].update(
                {"replacementId": "not-the-real-replacement"}
            )
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertEqual(host.ipc.shader_operation_recovery_calls, 1)
        self.assertEqual(host.ipc.shader_operations, {})
        recovery = next(
            params
            for method, params in host.ipc.calls
            if method == "recover_shader_candidate_shared_resource_operation"
        )
        self.assertEqual(len(recovery["operation_id"]), 32)

    def test_committed_handoff_error_is_failed_without_automatic_rollback_only_when_exact(self):
        for changed in (None, "operation", "session", "candidate", "source-verified",
                        "retained-marker", "created", "version-type", "superseded", "data-extra"):
            with self.subTest(changed=changed):
                def retained_error(response):
                    replacement = copy.deepcopy(response["replacement"])
                    replacement.update(idempotent=False, metadataOnly=False,
                                       supersededReplacementId="8" * 32)
                    data = {"operationId": response["operationId"],
                            "replayReplacementRetained": True, "currentSourceVerified": False,
                            "replacement": replacement, "recordingError": ""}
                    if changed == "operation":
                        data["operationId"] = "0" * 32
                    elif changed == "session":
                        replacement["captureSessionId"] = "0" * 32
                    elif changed == "candidate":
                        replacement["candidateRawSha256"] = "0" * 64
                    elif changed == "source-verified":
                        data["currentSourceVerified"] = True
                    elif changed == "retained-marker":
                        data["replayReplacementRetained"] = 1
                    elif changed == "created":
                        replacement["operationCreatedReplacement"] = False
                    elif changed == "version-type":
                        replacement["schemaVersion"] = True
                    elif changed == "superseded":
                        replacement["supersededReplacementId"] = replacement["replacementId"]
                    elif changed == "data-extra":
                        data["unchecked"] = True
                    response.clear()
                    response.update(error="Shader handoff committed; source read failed",
                                    type="OSError", data=data)

                result, host, unused, unused_progress = self._run(apply_response_mutator=retained_error)
                self.assertEqual(result["shaders"]["applied"], 0)
                self.assertEqual(result["shaders"]["failed"], 1)
                self.assertEqual(result["editing"]["enabled"], 0)
                self.assertFalse(result["index"]["written"])
                self.assertEqual(host.shader_rollback_calls, 0)
                if changed is None:
                    self.assertEqual(host.ipc.shader_operation_recovery_calls, 0)
                    self.assertEqual(len(host.ipc.shader_operations), 1)
                    self.assertIn("current source verification remains incomplete",
                                  result["shaders"]["items"][0]["error"])
                else:
                    self.assertEqual(host.ipc.shader_operation_recovery_calls, 1)
                    self.assertEqual(host.ipc.shader_operations, {})

    def test_inventory_restore_does_not_trust_legacy_catalog_for_material_values(
        self,
    ) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["shaders"][0]["interfaceHash"] = "d" * 64
        self.catalog_path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.catalog_sha = project_restore.hashlib.sha256(
            self.catalog_path.read_bytes()
        ).hexdigest()
        self.binding["catalogSha256"] = self.catalog_sha
        self.index = upsert_project_binding(new_index(), self.binding)
        self.workspace_status["capture"]["catalogSha256"] = self.catalog_sha
        self.state["catalogSha256"] = self.catalog_sha

        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["materials"]["restored"], 0)
        self.assertEqual(result["materials"]["failed"], 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["items"], [])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertTrue(result["index"]["written"])
        self.assertIsNotNone(host.ipc.index)
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertNotIn("prepare", phases)
        self.assertNotIn("material-restore", phases)
        self.assertEqual(phases[-1], "complete")
        self.assertIn("index-write", phases)

    def test_legacy_material_projection_requires_exact_catalog_reflection(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        legacy_identity = {
            "resourceId": 7,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": self.code_sha,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "interfaceComplete": True,
            "eventIds": [9],
        }

        expected = project_restore._expected_resource_shader(
            catalog, legacy_identity, "d3d11", 9
        )

        self.assertEqual(expected["interfaceHash"], self.interface_sha)
        self.assertEqual(expected["layoutHash"], self.layout_sha)
        forged = copy.deepcopy(legacy_identity)
        forged["interfaceHash"] = "d" * 64
        with self.assertRaisesRegex(
            project_restore.CatalogPreparationRequired,
            "legacy Shader interface/layout digest differs",
        ):
            project_restore._expected_resource_shader(
                catalog, forged, "d3d11", 9
            )

    def test_legacy_material_projection_rejects_multi_entrypoint_resource(self) -> None:
        identities = {
            (7, "fragment"): [
                {**copy.deepcopy(self.identity), "entryPoint": "main"},
                {**copy.deepcopy(self.identity), "entryPoint": "shadow"},
            ]
        }

        with self.assertRaisesRegex(
            RuntimeError, "ambiguous across Shader entry points"
        ):
            project_restore._legacy_material_shader_identity(
                identities,
                {"shaderResourceId": 7, "stage": "fragment"},
            )

    def test_v2_material_projection_requires_exact_live_inventory_reflection(
        self,
    ) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        raw_block = catalog["shaders"][0]["constantBlocks"][0]
        live_block = {
            **project_restore._catalog_block_projection(
                raw_block, 0, "fragment"
            ),
            "name": "PerMaterial",
            "fields": [],
        }
        inventory = {
            "shaders": [
                {
                    **copy.deepcopy(self.identity),
                    "interfaceHash": self.interface_sha,
                    "interfaceComplete": True,
                    "constantBlocks": [live_block],
                    "formatSupported": True,
                    "identityAmbiguous": False,
                    "exactExecutableMatchEligible": True,
                }
            ]
        }

        expected = project_restore._expected_resource_shader(
            catalog,
            self.identity,
            "d3d11",
            9,
            inventory=inventory,
        )

        self.assertEqual(expected["constantBlocks"], [live_block])
        tampered = copy.deepcopy(inventory)
        tampered["shaders"][0]["constantBlocks"][0]["abiSha256"] = "d" * 64
        with self.assertRaisesRegex(
            project_restore.CatalogPreparationRequired,
            "ABI/reflection evidence differs",
        ):
            project_restore._expected_resource_shader(
                catalog,
                self.identity,
                "d3d11",
                9,
                inventory=tampered,
            )

    def test_read_only_rdc_does_not_reverse_successful_restore(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            write_error=PermissionError("read only"),
            index_present=False,
            auto_prepare=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertFalse(result["index"]["written"])
        self.assertIn("read only", result["index"]["warning"])
        self.assertEqual(host.prepare_calls, 0)

    def test_absent_index_inventories_reuses_hlsl_and_writes_local_index(self) -> None:
        result, host, replay, progress = self._run(
            index_present=False, auto_prepare=True
        )

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertEqual(
            result["cache"]["reason"], "project-reuse-plan:index-absent"
        )
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertTrue(result["index"]["written"])
        self.assertTrue(result["index"]["present"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.seed_calls, 1)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.snapshot_calls, 0)
        self.assertEqual(replay.inventory_snapshot_calls, 1)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods,
            [
                "read_rdc_project_index",
                "begin_rdc_project_shader_resource_index",
                "write_rdc_project_index",
            ],
        )
        phases = [event["phase"] for event in progress]
        for phase in (
            "resource-inventory",
            "resource-match",
            "shader-prepare",
            "resource-anchor",
            "shader-compile",
            "shader-apply",
            "index-write",
            "complete",
        ):
            self.assertIn(phase, phases)
        self.assertNotIn("index-required", phases)
        self.assertNotIn("failed", phases)
        self.assertNotIn("prepare", phases)
        self.assertLess(phases.index("resource-match"), phases.index("shader-prepare"))
        self.assertLess(phases.index("shader-compile"), phases.index("shader-apply"))
        self.assertLess(phases.index("shader-apply"), phases.index("index-write"))

    def _source_only_knowledge(self, knowledge_id, workspace, workspace_id):
        source = workspace / "source" / "shader.hlsl"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("// saved draft with unresolved output mismatch\n", encoding="utf-8")
        record = self.state["workspaces"][knowledge_id]
        record.update(publicationMode="source-only-draft", workspacePath=str(workspace),
                      workspaceId=workspace_id, sourceOriginPath=str(source),
                      sourceSha256=project_restore.hashlib.sha256(source.read_bytes()).hexdigest(),
                      registeredWorkspace={"workspaceId": workspace_id, "path": str(workspace)})
        return record

    def test_saved_only_source_opens_editor_without_compile_apply_or_watch(self):
        self._source_only_knowledge(self.knowledge_id, self.workspace, self.managed_workspace_id)
        shown = []
        def configure(host, replay):
            host._show_project_shader_source = lambda path, digest, **binding: shown.append((path, digest, binding)) or json.dumps({"shown": True})
        result, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, host_mutator=configure)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"], 0, result)
        self.assertEqual(result["shaders"]["skipped"], 1, result)
        self.assertEqual(result["shaders"]["items"][0]["reason"], "source-only-project-draft")
        self.assertEqual((host.compile_calls, host.apply_calls, host.workspace_prepare_calls), (0, 0, 0))
        self.assertEqual(shown[0][0], str(self.workspace))
        self.assertEqual(shown[0][2]["identity"]["resourceId"], self.state["capture"]["shaders"][0]["resourceId"])
        self.assertEqual(shown[0][2]["knowledge_id"], self.knowledge_id)
        self.assertTrue(shown[0][2]["capture_session_id"])
        self.assertIs(shown[0][2]["activate_window"], False)
        self.assertEqual(host.edit_start_calls, [])

    def test_mixed_accepted_and_pending_source_reuses_fast_cache_and_reopens_draft(self):
        second_status = self._add_second_cached_shader()
        draft = self._source_only_knowledge("b" * 32, Path(second_status["workspacePath"]), second_status["workspaceId"])
        shown = []
        def configure(host, replay):
            self._ticketed_restore_host(host, replay)
            host._show_project_shader_source = lambda path, digest, **binding: shown.append(path) or json.dumps({"shown": True})
            match = copy.deepcopy(host.prepared_response["reusePlan"]["matches"][0])
            match["shader"] = copy.deepcopy(self.state["capture"]["shaders"][1])
            match["workspaceCandidates"][0].update(knowledgeId="b" * 32, sourceSha256=draft["sourceSha256"])
            host.prepared_response["reusePlan"]["matches"].append(match)
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            extra_workspace_statuses=[second_status], host_mutator=configure)
        self.assertTrue(cold["completed"], cold)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        self.assertEqual((cold["shaders"]["applied"], cold["shaders"]["skipped"]), (1, 1))
        counts = (host.compile_calls, host.workspace_prepare_calls, host.apply_calls)
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual((host.compile_calls, host.workspace_prepare_calls, host.apply_calls), counts)
        self.assertEqual(host.verified_apply_calls, 1)
        self.assertEqual((warm["shaders"]["applied"], warm["shaders"]["skipped"]), (1, 1))
        self.assertEqual(shown, [second_status["workspacePath"], second_status["workspacePath"]])
        self.assertEqual(warm["shaders"]["items"][1]["reason"], "source-only-project-draft")
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("mixed_restore_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        for value in (cold, warm):
            accepted = client.ReplayLabRestoreHostClient._result(
                value, str(self.project), str(self.capture_path), value["capture"]["sessionId"])
            self.assertEqual(accepted["shaders"]["skipped"], 1)

    def test_verified_coverage_contains_three_accepted_and_excludes_one_pending(self):
        state = {"workspaces": {}, "capture": {}}
        plan = {"matches": []}
        records = []
        for index in range(4):
            knowledge_id = ("%032x" % (index + 1))
            identity = {"resourceId": index + 1, "rawSha256": "%064x" % (index + 1),
                        "stage": "fragment", "entryPoint": "main"}
            state["workspaces"][knowledge_id] = {"knowledgeId": knowledge_id}
            plan["matches"].append({"shader": identity, "workspaceCandidates": [{
                "knowledgeId": knowledge_id, "sourceSeedEligible": True, "activeCandidate": True}]})
            if index < 3:
                records.append({"knowledgeId": knowledge_id, "identity": identity})
            else:
                state["workspaces"][knowledge_id]["publicationMode"] = "source-only-draft"
        project_restore._verified_restore_coverage(state, plan, records, None)
        with self.assertRaisesRegex(RuntimeError, "omits or repeats"):
            project_restore._verified_restore_coverage(state, plan, records + [{
                "knowledgeId": knowledge_id, "identity": identity}], None)

    def test_pending_head_and_previous_verified_version_share_one_restore_receipt(self):
        pending_id = "b" * 32
        self.state["workspaces"][pending_id] = copy.deepcopy(self.state["workspaces"][self.knowledge_id])
        self.state["workspaces"][pending_id]["knowledgeId"] = pending_id
        draft_path = self.project / "pending-source"
        pending = self._source_only_knowledge(pending_id, draft_path, "c" * 32)
        shown = []
        def configure(host, replay):
            self._ticketed_restore_host(host, replay)
            host._show_project_shader_source = lambda path, digest, **binding: shown.append(binding) or json.dumps({"shown": True})
            candidate = host.prepared_response["reusePlan"]["matches"][0]["workspaceCandidates"][0]
            candidate["pendingSource"] = {"knowledgeId": pending_id, "sourceSha256": pending["sourceSha256"],
                                          "reason": "source-only-project-draft"}
        cold, host, replay, unused = self._run(index_present=False, auto_prepare=True,
            source_preparation_only=True, host_mutator=configure)
        self.assertTrue(cold["completed"], cold)
        self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        for value in (cold, warm):
            self.assertEqual((value["shaders"]["applied"], value["shaders"]["skipped"]), (1, 0))
            item, = value["shaders"]["items"]
            self.assertEqual(item["knowledgeId"], self.knowledge_id)
            self.assertEqual(item["pendingSource"], {"knowledgeId": pending_id,
                "sourceSha256": pending["sourceSha256"], "reason": "source-only-project-draft",
                "workspacePath": str(draft_path), "shown": True})
        self.assertEqual(len(shown), 2)
        self.assertTrue(all(item["activate_window"] is False for item in shown))
        self.assertTrue(all(item["replay_source"] == {"knowledgeId": self.knowledge_id,
            "sourceSha256": self.source_sha, "applied": True} for item in shown))

    def test_warm_shared_knowledge_maps_each_pending_editor_to_its_current_physical_resource(self):
        records, statuses, applied, matches = [], [], [], []
        for index in (1, 2):
            identity = {"resourceId": index * 100, "stage": "fragment", "entryPoint": "main", "rawSha256": "a" * 64}
            workspace_id = str(index) * 32
            replacement = {"abiValidationMode": "verified-rdc-restore", "captureSessionId": self.session_id,
                "workspaceId": workspace_id, "revision": 1, "candidateRawSha256": "b" * 64,
                "originalRawSha256": "a" * 64, "restoreValidationReceipt": {"signature": "sealed"},
                "replacementId": str(index + 2) * 32, "originalResourceId": index * 1000}
            records.append({"identity": identity, "knowledgeId": self.knowledge_id,
                "candidateRevision": 1, "candidateRawSha256": "b" * 64, "sourceSha256": self.source_sha,
                "replacement": {"restoreValidationReceipt": replacement["restoreValidationReceipt"]}})
            statuses.append({"workspacePath": str(self.workspace), "workspaceId": workspace_id})
            applied.append({"applied": True, "operation": "apply-shared-resource", "replacement": replacement,
                "validation": {"validationScope": "verified-rdc-restore", "reusedValidation": True,
                               "captureSessionId": self.session_id}})
            matches.append({"shader": identity, "workspaceCandidates": [{"knowledgeId": self.knowledge_id,
                "activeCandidate": True, "pendingSource": {"knowledgeId": "c" * 32}}]})
        snapshot = {"shaders": records, "bindingState": {"workspaces": {}, "captureId": self.capture_id},
                    "reusePlan": {"matches": matches}}
        result = {"operationId": "d" * 32, "capture": {}, "shaders": {}, "realizedReceipts": {}}
        host = mock.Mock()
        host._apply_verified_project_restore.return_value = {"applied": True, "items": applied}
        host._shader_artifact_replay_scope = contextlib.nullcontext
        shown_targets = []
        def display(host, candidate, state, identity, *rest):
            shown_targets.append(identity["resourceId"])
            return {"resourceIdForTest": identity["resourceId"]}
        with mock.patch.object(project_restore, "_require_restore_context"), \
             mock.patch.object(project_restore, "_verified_restore_binding_state"), \
             mock.patch.object(project_restore, "_fallback_pending_knowledge"), \
             mock.patch.object(project_restore, "_restore_reuse_aliases_best_effort"), \
             mock.patch.object(project_restore, "_show_fallback_pending_source", side_effect=display):
            project_restore._apply_verified_rdc_restore(host, mock.Mock(), self.active, self.project,
                self.capture_path, self.session_id, snapshot, statuses, mock.Mock(), result, [], [], lambda _: None)
        self.assertEqual(shown_targets, [1000, 2000])
        self.assertEqual([item["pendingSource"]["resourceIdForTest"] for item in result["shaders"]["items"]], [1000, 2000])

    def test_verified_abi_cache_cannot_admit_newly_recognized_legacy_failed_source(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        snapshot = json.loads(project_restore._verified_rdc_restore_path(
            self.replay_path, self.capture_path).read_text(encoding="utf-8"))
        snapshot["bindingState"]["workspaces"][self.knowledge_id]["publicationMode"] = "source-only-draft"
        with self.assertRaisesRegex(project_restore._VerifiedRdcRestorePolicyMiss, "source-only"):
            project_restore._verified_restore_workspace_statuses(
                host, self.active, self.project, self.capture_path, snapshot)
        self.assertEqual(host.verified_apply_calls, 0)

    def _ticketed_restore_host(self, host, replay):
        host.verified_apply_calls = 0
        host.restore_validation_policy_sha256 = "9" * 64
        host.workspace_service.status = lambda path: copy.deepcopy(host._status_for_path(path))

        def sign(response):
            replacement = response["replacement"]
            payload = {key: replacement[key] for key in (
                "captureId", "workspaceId", "revision", "candidateRawSha256",
                "originalRawSha256", "physicalAbiSha256", "api", "stage", "entryPoint",
                "encoding", "abiRequestId", "abiReportSha256")}
            payload.update(schema="replaylab-validated-shader-restore/v1",
                           captureSha256=project_restore._verified_rdc_content_sha256(self.capture_path),
                           validationPolicySha256=host.restore_validation_policy_sha256,
                           sourceSha256=self.source_sha)
            replacement["restoreValidationReceipt"] = {"payload": payload, "signature": "a" * 64}

        def fast(snapshot, statuses, session, operation):
            host.verified_apply_calls += 1
            items = []
            for record, status in zip(snapshot["shaders"], statuses):
                replacement = copy.deepcopy(record["replacement"])
                replacement.update(abiValidationMode="verified-rdc-restore",
                                   captureSessionId=session, replacementId="8" * 32,
                                   operationId=operation, operationCreatedReplacement=True)
                validation = copy.deepcopy(record["validation"])
                validation.update(validationScope="verified-rdc-restore", reusedValidation=True,
                                  captureSessionId=session, freshPhysicalValidationPerformed=False)
                items.append({"applied": True, "operation": "apply-shared-resource",
                              "replacement": replacement, "validation": validation})
            return {"applied": True, "items": items}

        host.apply_response_mutator = sign
        host._apply_verified_project_restore = fast

    def _reopen_ticketed_restore(self, host, replay):
        host.session_id = "f" * 32
        host.ipc.session_id = host.session_id
        for asset in host.ipc.inventory_assets:
            asset["ownerCaptureSessionId"] = host.session_id
        host.ipc.calls.clear()
        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=replay
        ):
            return project_restore.restore_active_capture_project(
                self.project, self.capture_path, host.session_id)

    def _physical_ticketed_restore_host(self, host, replay):
        self._ticketed_restore_host(host, replay)
        host.physical_validation_binding = {
            "schema": "replaylab-shader-physical-validation-binding/v1", "schemaVersion": 1,
            "abiRulesVersion": 1,
            "algorithms": [{"role": "native-ABI-checks", "codeSha256": "2" * 64}],
            "nativeBinaries": [{"name": "renderdoc.dll", "sha256": "3" * 64}],
        }
        host.restore_validation_policy_sha256 = project_restore._canonical_sha256(host.physical_validation_binding)
        host.package_provenance_sha256 = "4" * 64
        sign = host.apply_response_mutator

        def physically_signed(response):
            sign(response)
            payload = response["replacement"]["restoreValidationReceipt"]["payload"]
            payload["physicalValidationBinding"] = copy.deepcopy(host.physical_validation_binding)
            payload["packageProvenanceSha256"] = host.package_provenance_sha256

        host.apply_response_mutator = physically_signed

    def test_verified_replay_cache_survives_later_local_draft_edits(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._physical_ticketed_restore_host)
        self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
        draft = self.project / "workspaces" / "shaders" / "unfinished-local-source"
        draft.mkdir()
        status = copy.deepcopy(host.target_status)
        status.update(workspaceId="5" * 32, workspacePath=str(draft))
        status["source"].update(lane="executable-editable", format="hlsl",
                                currentSha256="6" * 64, modifiedSinceDecompile=True)
        status["latestCompiled"] = None
        host.workspace_service.extra_statuses.append(status)
        snapshot_path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["shaders"][0]["preservedLocalSource"] = {
            "path": draft.relative_to(self.project).as_posix(), "workspaceId": status["workspaceId"]}
        snapshot.pop("sealSha256")
        snapshot["sealSha256"] = project_restore._canonical_sha256(snapshot)
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        counts = host.compile_calls, host.workspace_prepare_calls, host.apply_calls
        with mock.patch.object(host, "_show_saved_shader_workspace_source",
                               return_value=json.dumps({"shown": True}), create=True) as show:
            for sha in ("6" * 64, "7" * 64):
                status["source"]["currentSha256"] = sha
                warm = self._reopen_ticketed_restore(host, replay)
                self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
                self.assertEqual(warm["shaders"]["applied"], 1, warm)
                self.assertEqual(warm["shaders"]["skipped"], 0, warm)
                self.assertEqual(warm["shaders"]["items"][0]["preservedLocalSource"], {
                    "workspaceId": "5" * 32, "workspacePath": str(draft), "sourceSha256": sha,
                    "reason": "local-edit", "shown": True})
                self.assertEqual(warm["editing"]["deferred"], 1, warm)
                self.assertEqual(warm["editing"]["enabled"], 0, warm)
                self.assertEqual(warm["editing"]["items"][0]["reason"], "local-edit-preserved")
                self.assertFalse(warm["editing"]["items"][0]["readOnly"])
                self.assertEqual((host.compile_calls, host.workspace_prepare_calls, host.apply_calls), counts)
            self.assertEqual(show.call_args.args, (str(draft),))
        self.assertEqual(host.verified_apply_calls, 2)

        # A removed or retargeted optional editor must never revoke the
        # authenticated executable which was just restored successfully.
        for unavailable in ("deleted-source", "different-original", "deleted-workspace"):
            if unavailable == "deleted-source":
                status["source"]["present"] = False
            elif unavailable == "different-original":
                status["source"]["present"] = True
                status["shader"]["rawSha256"] = "8" * 64
            else:
                draft.rmdir()
            warm = self._reopen_ticketed_restore(host, replay)
            self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
            self.assertEqual(warm["shaders"]["applied"], 1, warm)
            self.assertEqual(warm["shaders"]["failed"], 0, warm)
            self.assertEqual(host.shader_rollback_calls, 0)
            self.assertNotIn("preservedLocalSource", warm["shaders"]["items"][0])

    def test_verified_rdc_batch_uses_existing_artifact_scope_instead_of_compiler_route(self):
        active=[]
        def configure(host,replay):
            self._ticketed_restore_host(host,replay)
            original=host._apply_verified_project_restore
            @contextlib.contextmanager
            def scope():
                active.append(True)
                try:yield
                finally:active.pop()
            def apply(*args):
                self.assertTrue(active,"verified batch must use existing-artifact replay scope")
                return original(*args)
            host._shader_artifact_replay_scope=scope
            host._apply_verified_project_restore=apply
        cold,host,replay,unused=self._run(index_present=False,auto_prepare=True,
            source_preparation_only=True,host_mutator=configure)
        self.assertTrue(cold["completed"],cold)
        warm=self._reopen_ticketed_restore(host,replay)
        self.assertEqual(warm["restoreMode"],"verified-rdc-restore",warm)
        self.assertEqual(active,[])

    def test_verified_rdc_full_cache_reuses_same_physical_domain_after_package_ui_change(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._physical_ticketed_restore_host)
        self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
        counts = host.compile_calls, host.workspace_prepare_calls, host.apply_calls
        host.package_provenance_sha256 = "5" * 64
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertTrue(warm["verifiedRdcCache"]["hit"], warm)
        self.assertEqual((host.compile_calls, host.workspace_prepare_calls, host.apply_calls), counts)
        self.assertEqual(host.verified_apply_calls, 1)
        self.assertEqual(host.ipc.calls, [])

    def test_verified_rdc_physical_algorithm_native_or_rules_change_revalidates_then_reuses(self):
        for changed in ("algorithm", "native", "ABI-rules"):
            with self.subTest(changed=changed):
                # Each case begins with a separately sealed current snapshot.
                cold, host, replay, unused = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    host_mutator=self._physical_ticketed_restore_host)
                self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
                if changed == "algorithm":
                    host.physical_validation_binding["algorithms"][0]["codeSha256"] = "5" * 64
                elif changed == "native":
                    host.physical_validation_binding["nativeBinaries"][0]["sha256"] = "5" * 64
                else:
                    host.physical_validation_binding["abiRulesVersion"] = 2
                host.restore_validation_policy_sha256 = project_restore._canonical_sha256(host.physical_validation_binding)
                refreshed = self._reopen_ticketed_restore(host, replay)
                self.assertTrue(refreshed["completed"], refreshed)
                self.assertFalse(refreshed["verifiedRdcCache"]["hit"], refreshed)
                self.assertEqual(host.verified_apply_calls, 0)
                self.assertEqual(refreshed["cache"]["reason"], "verified-rdc-cache-miss: validation-policy-changed")
                self.assertTrue(refreshed["verifiedRdcCache"]["written"], refreshed)
                self.assertNotIn("recover_shader_candidate_shared_resource_operation", [method for method, _ in host.ipc.calls])
                warm = self._reopen_ticketed_restore(host, replay)
                self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
                self.assertEqual(host.verified_apply_calls, 1)

    def test_current_physical_policy_does_not_accept_legacy_pair_as_new_validation(self):
        result, host, unused_replay, unused_progress = self._run(host_mutator=self._ticketed_restore_host)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        status = host.workspace_service._status
        self.assertIsNotNone(project_restore._load_shader_pair_validation(self.project, status))
        current = {
            "schema": "replaylab-shader-physical-validation-binding/v1", "schemaVersion": 1,
            "abiRulesVersion": 1,
            "algorithms": [{"role": "actual-ABI", "codeSha256": "2" * 64}], "nativeBinaries": [],
        }
        self.assertIsNone(project_restore._load_shader_pair_validation(
            self.project, status, project_restore._canonical_sha256(current)))

    def test_verified_rdc_partial_reflection_second_open_skips_all_live_validation(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold["completed"], cold)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        self.assertFalse(cold["index"]["written"])
        snapshot_path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertTrue(snapshot["shaders"][0]["sourcePreparationOnly"])
        self.assertEqual(snapshot["shaders"][0]["replacement"]["restoreValidationReceipt"],
                         host.last_apply_response["replacement"]["restoreValidationReceipt"])
        counts = host.compile_calls, host.workspace_prepare_calls, host.apply_calls
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(warm["completed"], warm)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual((host.compile_calls, host.workspace_prepare_calls, host.apply_calls), counts)
        self.assertEqual(host.verified_apply_calls, 1)
        self.assertEqual(warm["editing"]["enabled"], 1, warm)
        self.assertTrue(warm["shaders"]["items"][0]["reusedValidation"])
        self.assertEqual(host.ipc.calls, [])
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("verified_restore_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        for value in (cold, warm):
            accepted = client.ReplayLabRestoreHostClient._result(
                value, str(self.project), str(self.capture_path), value["capture"]["sessionId"])
            self.assertEqual(accepted["editing"]["enabled"], 1)

    def test_written_memo_loads_and_passes_real_host_fast_apply_contract(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
        previous = copy.deepcopy(host.last_apply_response)
        host.target_status["representation"] = {"lane": "executable-editable", "compileEligible": True}
        host.workspace_service.candidate_reflection_request = lambda path: {
            "binding": copy.deepcopy(previous["binding"]), "requestId": "1" * 64, "params": {}}
        host._apply_verified_project_restore = server._apply_verified_project_restore
        calls = []
        recovery_calls = []

        def bridge_apply(method, params, **kwargs):
            if method == "recover_shader_candidate_shared_resource_operation":
                recovery_calls.append(copy.deepcopy(params))
                return host.ipc.call(method, params, **kwargs)
            self.assertEqual(method, "apply_shader_candidate_shared_resource")
            calls.append(copy.deepcopy(params))
            return {**copy.deepcopy(previous["replacement"]),
                    "operationId": params["operation_id"],
                    "captureSessionId": host.session_id,
                    "abiValidationMode": "verified-rdc-restore",
                    "freshPhysicalAbiValidationPerformed": False,
                    "restoreValidationReceipt": params["restore_validation_receipt"],
                    "shaderFamilyId": "6" * 64, "targetResourceId": 101,
                    "checkedDerivedPipelineRebuild": False, "capturedRootSignaturesReused": False,
                    "postApplyAnchorVerified": False, "idempotent": False, "metadataOnly": False,
                    "targetBuiltOnce": True, "targetReused": False,
                    "supersededReplacementId": None, "supersededTargetFreed": True}

        before = host.compile_calls, host.workspace_prepare_calls, host.apply_calls
        with mock.patch.object(server, "_shader_workspace_service", return_value=host.workspace_service), \
             mock.patch.object(server, "_verified_restore_catalog_binding", return_value={}), \
             mock.patch.object(server, "_remember_shared_shader_replacement"), \
             mock.patch.object(server, "ipc", types.SimpleNamespace(call=bridge_apply)):
            warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual(warm["shaders"]["applied"], 1, warm)
        self.assertEqual(len(calls), 1)
        self.assertEqual(recovery_calls, [])
        self.assertEqual((host.compile_calls, host.workspace_prepare_calls, host.apply_calls), before)

    def test_verified_rdc_complete_reflection_does_not_invalidate_ticket_with_index_write(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        self.assertFalse(cold["index"]["written"])
        self.assertNotIn("write_rdc_project_index", [method for method, unused in host.ipc.calls])
        for unused in range(2):
            warm = self._reopen_ticketed_restore(host, replay)
            self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
            self.assertEqual(host.ipc.calls, [])
        self.assertEqual(host.verified_apply_calls, 2)

    def test_verified_rdc_old_inventory_cannot_prime_even_when_all_returned_matches_apply(self):
        result, host, unused, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host,
            inventory_result_mutator=lambda value: value.pop("originalShaderIdentityPolicy", None))
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertFalse(result["verifiedRdcCache"]["written"])
        self.assertIn("inventory of captured original", result["verifiedRdcCache"]["reason"])

    def test_verified_rdc_v1_partial_snapshot_is_rebuilt_before_hashing_or_apply(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold["verifiedRdcCache"]["written"])
        path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot.pop("sealSha256")
        snapshot["schemaVersion"] = 1
        snapshot.pop("inventoryEvidence")
        snapshot["sealSha256"] = project_restore._canonical_sha256(snapshot)
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with mock.patch.object(project_restore, "_verified_rdc_content_sha256",
                               side_effect=AssertionError("old coverage cannot reach file hashing")):
            with self.assertRaisesRegex(project_restore._VerifiedRdcRestorePolicyMiss,
                                        "original-shader-inventory-policy-changed"):
                project_restore._load_verified_rdc_restore(host, replay, self.active, self.project,
                    self.replay_path, self.capture_path, lambda unused: None, "9" * 64)
        self.assertEqual(host.verified_apply_calls, 0)
        reopened = self._reopen_ticketed_restore(host, replay)
        self.assertNotEqual(reopened["restoreMode"], "verified-rdc-restore")
        self.assertTrue(reopened["verifiedRdcCache"]["written"], reopened)

    def test_verified_rdc_other_capture_unobserved_targets_are_not_required(self):
        missing = "0" * 64
        def with_other_capture_head(host, replay):
            self._ticketed_restore_host(host, replay)
            replay.restorable_shader_raw_hashes = lambda: [missing, self.raw_sha]
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=with_other_capture_head)
        self.assertEqual(cold["targetLookup"]["unobservedTargetRawSha256s"], [missing])
        self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore")
        self.assertEqual(warm["shaders"]["applied"], 1)

    def test_verified_rdc_coverage_rejects_a_missing_matched_original(self):
        # The live regression has two heads (755/860), but the second restore
        # returned only 755 after 860 was already replaced. Both matches must
        # be retained; a successful subset must not become a complete memo.
        identities = [dict(self.identity, resourceId=1000000000000000755),
                      dict(self.identity, resourceId=1000000000000000860,
                           rawSha256="3c4afb434934308919e884efa26523f832807f2adc839d3a3754ceb559255205")]
        plan = {"matches": [{"shader": identity, "workspaceCandidates": [
            {"knowledgeId": str(index) * 32, "activeCandidate": True, "sourceSeedEligible": True}]}
            for index, identity in enumerate(identities, 1)]}
        records = [{"identity": identity, "knowledgeId": str(index) * 32}
                   for index, identity in enumerate(identities, 1)]
        state = {"capture": {"inventoryOnly": True, "shaders": identities}}
        evidence = {"originalShaderIdentityPolicy": "captured-original-owner-v1",
                    "observedRawSha256s": sorted({item["rawSha256"] for item in identities})}
        project_restore._verified_restore_coverage(state, plan, records, evidence)
        with self.assertRaisesRegex(RuntimeError, "omits or repeats"):
            project_restore._verified_restore_coverage(state, plan, records[:1], evidence)
        with self.assertRaisesRegex(RuntimeError, "omits or repeats"):
            project_restore._verified_restore_coverage(state, plan, [records[0], records[0]], evidence)

    def test_verified_rdc_complete_two_shader_cache_restores_both_on_new_session(self):
        second_status = self._add_second_cached_shader()
        def two_heads(host, replay):
            self._ticketed_restore_host(host, replay)
            host.expected_anchors = {second_status["workspaceId"]: 10}
            matches = [{"shader": copy.deepcopy(item["identity"]), "workspaceCandidates": [{
                "knowledgeId": item["knowledgeId"], "activeCandidate": True, "sourceSeedEligible": True,
                "matchMode": "raw-exact", "sourceSha256": self.source_sha}],
                "aliasCandidates": [], "materialCandidates": []} for item in self.binding["shaders"]]
            replay.reuse_plan_response = {**replay.reuse_plan(self.capture_id, self.catalog_sha),
                                         "matches": matches, "matchedShaderCount": 2}
        cold, host, replay, unused = self._run(extra_workspace_statuses=[second_status], host_mutator=two_heads)
        self.assertEqual(cold["shaders"]["applied"], 2, cold)
        self.assertTrue(cold["verifiedRdcCache"]["written"], cold)
        before = host.compile_calls, host.workspace_prepare_calls, host.apply_calls
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual(warm["shaders"]["applied"], 2, warm)
        self.assertEqual({item["resourceId"] for item in warm["shaders"]["items"]}, {7, 8})
        self.assertEqual((host.compile_calls, host.workspace_prepare_calls, host.apply_calls), before)

    def test_manual_shader_does_not_prevent_signed_siblings_from_priming_cache(self):
        second = self._add_second_cached_shader()
        def configure(host, replay):
            self._ticketed_restore_host(host, replay)
            host.expected_anchors = {second["workspaceId"]: 10}
            matches = [{"shader": copy.deepcopy(item["identity"]), "workspaceCandidates": [{
                "knowledgeId": item["knowledgeId"], "activeCandidate": True, "sourceSeedEligible": True,
                "matchMode": "raw-exact", "sourceSha256": self.source_sha}],
                "aliasCandidates": [], "materialCandidates": []} for item in self.binding["shaders"]]
            replay.reuse_plan_response = {**replay.reuse_plan(self.capture_id, self.catalog_sha),
                                         "matches": matches, "matchedShaderCount": 2}
        cold, host, replay, unused = self._run(extra_workspace_statuses=[second], host_mutator=configure)
        self.assertTrue(cold["completed"], cold)
        path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        records = saved["shaders"]
        records[1]["manualEdit"] = True
        records[1]["replacement"].pop("restoreValidationReceipt")
        workspaces = []
        for record in records:
            workspace = self.project / record["managedWorkspace"]["path"]
            status = host.workspace_service.status(str(workspace))
            workspaces.append((workspace, status, {"replacement": record["replacement"],
                "validation": record["validation"]}, host.session_id))
        partial = project_restore._build_verified_rdc_restore(host, replay, self.active, self.project,
            self.capture_path, saved["bindingState"], records, workspaces, lambda _: None,
            inventory={"originalShaderIdentityPolicy": "captured-original-owner-v1",
                       "shaders": saved["bindingState"]["capture"]["shaders"]})
        self.assertEqual(len(partial["shaders"]), 1)
        self.assertEqual(len(partial["deferredShaders"]), 1)
        self.assertNotIn("replacement", partial["deferredShaders"][0])
        gui = copy.deepcopy(cold)
        gui["restoreMode"] = "full-inventory"
        gui["cache"] = {"hit": False, "reason": "validated-content-snapshot-written"}
        gui["verifiedRdcCache"].update(reason="validated-content-subset-snapshot")
        gui["realizedReceipts"].update(manualEdit=1, persisted=1)
        gui["shaders"]["items"][1]["pairValidationCache"] = {
            "hit": False, "written": False, "reason": "manual-edit-unvalidated"}
        gui["shaders"]["items"][1].update(source="reuse-plan", reusedValidation=False,
            knowledgeId=records[1]["knowledgeId"],
            candidateRevision=records[1]["candidateRevision"], sourceSha256=records[1]["sourceSha256"],
            candidateRawSha256=records[1]["candidateRawSha256"])
        spec = importlib.util.spec_from_file_location("partial_cache_client",
            Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        client.ReplayLabRestoreHostClient._result(gui, str(self.project), str(self.capture_path), self.session_id)
        invalid = copy.deepcopy(gui)
        invalid["realizedReceipts"]["persisted"] = 2
        with self.assertRaises(client.ReplayLabRestoreClientError):
            client.ReplayLabRestoreHostClient._result(invalid, str(self.project), str(self.capture_path), self.session_id)
        project_restore._write_verified_rdc_restore(self.replay_path, self.capture_path, partial)
        before = host.apply_calls
        with mock.patch.object(project_restore, "_inventory_shader_resources",
                               side_effect=RuntimeError("unsigned sibling inventory unavailable")):
            warm = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(warm["completed"], warm)
        self.assertEqual(warm["restoreMode"], "verified-rdc-subset", warm)
        self.assertEqual(warm["shaders"]["applied"], 1)
        self.assertEqual(warm["shaders"]["failed"], 1)
        self.assertEqual(host.verified_apply_calls, 1)
        self.assertEqual(host.apply_calls, before)  # The signed sibling is never applied twice.
        client.ReplayLabRestoreHostClient._result(warm, str(self.project), str(self.capture_path), host.session_id)

    def test_current_signed_pair_repairs_stale_whole_project_policy_without_revalidating(self):
        cold, host, replay, unused = self._run(index_present=False, auto_prepare=True,
            source_preparation_only=True, host_mutator=self._physical_ticketed_restore_host)
        path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        record = snapshot["shaders"][0]
        host.physical_validation_binding["nativeBinaries"][0]["sha256"] = "5" * 64
        host.restore_validation_policy_sha256 = project_restore._canonical_sha256(host.physical_validation_binding)
        applied = {"applied": True, "replacement": copy.deepcopy(record["replacement"]),
                   "validation": copy.deepcopy(record["validation"])}
        payload = applied["replacement"]["restoreValidationReceipt"]["payload"]
        payload.update(physicalValidationBinding=copy.deepcopy(host.physical_validation_binding),
                       validationPolicySha256=host.restore_validation_policy_sha256)
        status = host.workspace_service.status(str(self.project / record["managedWorkspace"]["path"]))
        self.assertTrue(project_restore._persist_shader_pair_validation(self.project, status, applied))
        lease_only = {"replacement": applied["replacement"], "abiLease": {}}
        with mock.patch.object(project_restore, "_load_shader_pair_validation", return_value=lease_only):
            repaired, binding = project_restore._current_verified_pair_record(host, self.active,
                self.project, self.capture_path, snapshot, record, status,
                host.restore_validation_policy_sha256, ())
        self.assertEqual(repaired["validation"]["requestId"], status["latestCompiled"]["abiGate"]["requestId"])
        self.assertEqual(repaired["validation"]["abiReportSha256"], status["latestCompiled"]["abiGate"]["reportSha256"])
        before = host.apply_calls, host.compile_calls
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(warm["completed"], warm)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual((host.apply_calls, host.compile_calls), before)
        self.assertEqual(host.verified_apply_calls, 1)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["shaders"][0]["replacement"]["restoreValidationReceipt"]["payload"][
            "validationPolicySha256"], host.restore_validation_policy_sha256)
        self.assertFalse(any(key.startswith("_") for key in saved))
        with mock.patch.object(project_restore, "_refresh_verified_restore_pairs",
                               side_effect=AssertionError("unchanged memo must not repeat pair recovery")):
            again = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(again["completed"], again)

    def test_newer_pair_repairs_stale_abi_observation_under_same_policy(self):
        cold, host, replay, unused = self._run(index_present=False, auto_prepare=True,
            source_preparation_only=True, host_mutator=self._physical_ticketed_restore_host)
        path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        snapshot = json.loads(path.read_text(encoding="utf-8"));record = snapshot["shaders"][0]
        status = host._status_for_path(str(self.project / record["managedWorkspace"]["path"]))
        status["latestCompiled"]["abiGate"].update(requestId="6"*64, reportSha256="7"*64)
        applied = {"applied": True, "replacement": copy.deepcopy(record["replacement"]),
                   "validation": copy.deepcopy(record["validation"])}
        applied["replacement"].update(abiRequestId="6"*64, abiReportSha256="7"*64)
        applied["replacement"]["restoreValidationReceipt"]["payload"].update(
            abiRequestId="6"*64, abiReportSha256="7"*64)
        self.assertTrue(project_restore._persist_shader_pair_validation(self.project, status, applied))
        before = host.apply_calls, host.compile_calls
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(warm["completed"], warm)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual((host.apply_calls, host.compile_calls), before)
        self.assertEqual(host.verified_apply_calls, 1)

    def test_verified_rdc_policy_change_is_a_cache_miss_before_hash_or_apply(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        for policy in ("e" * 64, None, "invalid", True):
            with self.subTest(policy=policy):
                host.restore_validation_policy_sha256 = policy
                with mock.patch.object(project_restore, "_verified_rdc_content_sha256",
                                       side_effect=AssertionError("policy miss must precede RDC hashing")), \
                     mock.patch.object(project_restore, "_verified_restore_workspace_statuses",
                                       side_effect=AssertionError("policy miss must precede workspace reads")), \
                     mock.patch.object(project_restore, "_inventory_project_shader_targets",
                                       side_effect=RuntimeError("cold fallback reached")) as fallback:
                    result = self._reopen_ticketed_restore(host, replay)
                fallback.assert_called_once()
                self.assertEqual(result["verifiedRdcCache"], {
                    "hit": False, "written": False,
                    "reason": "validation-policy-changed" if policy == "e" * 64 else "validation-policy-unavailable"})
                self.assertEqual(host.verified_apply_calls, 0)
                self.assertNotIn("recover_shader_candidate_shared_resource_operation",
                                 [method for method, params in host.ipc.calls])
                self.assertNotIn("apply_shader_candidate_shared_resource",
                                 [method for method, params in host.ipc.calls])

    def test_verified_rdc_new_policy_completes_cold_restore_then_reuses_new_receipt(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        host.restore_validation_policy_sha256 = "e" * 64
        refreshed = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(refreshed["completed"], refreshed)
        self.assertEqual(host.verified_apply_calls, 0)
        self.assertTrue(refreshed["verifiedRdcCache"]["written"], refreshed)
        self.assertEqual(refreshed["cache"]["reason"],
                         "verified-rdc-cache-miss: validation-policy-changed")
        self.assertEqual(refreshed["shaders"]["failed"], 0)
        self.assertEqual(refreshed["warnings"], [])
        self.assertNotIn("recover_shader_candidate_shared_resource_operation",
                         [method for method, params in host.ipc.calls])
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual(host.verified_apply_calls, 1)

    def test_verified_rdc_priming_preserves_existing_exact_index_cache_hit_reason(self):
        def exact_plan(host, replay):
            self._ticketed_restore_host(host, replay)
            replay.reuse_plan_response = {**replay.reuse_plan(self.capture_id, self.catalog_sha),
                "matchedShaderCount": 1, "matches": [{"shader": copy.deepcopy(self.identity),
                "workspaceCandidates": [{"knowledgeId": self.knowledge_id, "activeCandidate": True,
                    "sourceSeedEligible": True, "matchMode": "raw-exact", "sourceSha256": self.source_sha}],
                "aliasCandidates": [], "materialCandidates": []}]}
        result, unused_host, unused_replay, unused = self._run(host_mutator=exact_plan)
        self.assertTrue(result.get("verifiedRdcCache", {}).get("written"), result)
        self.assertEqual(result["restoreMode"], "exact-rdc-head")
        self.assertEqual(result["cache"], {"hit": True, "reason": "exact-desired-project-head"})

    def test_verified_rdc_same_size_same_timestamp_mutation_falls_back(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        original = self.capture_path.stat()
        data = bytearray(self.capture_path.read_bytes())
        data[0] ^= 1
        self.capture_path.write_bytes(data)
        project_restore.os.utime(self.capture_path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with mock.patch.object(project_restore, "_inventory_project_shader_targets", side_effect=RuntimeError("fallback reached")) as fallback:
            result = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(host.verified_apply_calls, 0)
        fallback.assert_called_once()
        self.assertIn("file contents changed", result["verifiedRdcCache"]["reason"])

    def test_verified_rdc_source_candidate_compiler_shared_and_snapshot_mutations_fall_back(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        saved = copy.deepcopy(host.target_status)
        path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        data = path.read_bytes()
        mutations = {
            "source": lambda: host.target_status["source"].update(currentSha256="a" * 64),
            "candidate": lambda: host.target_status["latestCompiled"].update(rawSha256="a" * 64),
            "compiler": lambda: host.target_status.update(compilerBinding={"changed": True}),
            "report": lambda: host.target_status["latestCompiled"]["abiGate"].update(reportSha256="a" * 64),
            "corrupt-json": lambda: path.write_bytes(b"{"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                host.target_status = copy.deepcopy(saved)
                path.write_bytes(data)
                mutate()
                with mock.patch.object(project_restore, "_inventory_project_shader_targets", side_effect=RuntimeError("fallback reached")) as fallback:
                    self._reopen_ticketed_restore(host, replay)
                fallback.assert_called_once()
                self.assertEqual(host.verified_apply_calls, 0)
        host.target_status = saved
        path.write_bytes(data)
        with mock.patch.object(replay, "reuse_snapshot_for_shader_inventory", side_effect=RuntimeError("shared source bytes changed")), mock.patch.object(
            project_restore, "_inventory_project_shader_targets", side_effect=RuntimeError("fallback reached")
        ) as fallback:
            self._reopen_ticketed_restore(host, replay)
        fallback.assert_called_once()
        self.assertEqual(host.verified_apply_calls, 0)

    def test_verified_rdc_old_unsigned_apply_cannot_prime_cache(self):
        def unsigned_host(host, replay):
            self._ticketed_restore_host(host, replay)
            host.apply_response_mutator = None
        result, host, unused_replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=unsigned_host)
        self.assertTrue(result["completed"], result)
        self.assertFalse(project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path).exists())
        self.assertEqual(host.verified_apply_calls, 0)

    def test_verified_rdc_bad_current_session_response_rolls_back_then_falls_back(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        fast = host._apply_verified_project_restore
        def bad(*args):
            response = fast(*args)
            response["items"][0]["validation"]["captureSessionId"] = "e" * 32
            return response
        host._apply_verified_project_restore = bad
        with mock.patch.object(project_restore, "_inventory_project_shader_targets", side_effect=RuntimeError("fallback reached")) as fallback:
            self._reopen_ticketed_restore(host, replay)
        self.assertEqual(host.shader_rollback_calls, 1)
        fallback.assert_called_once()

    def test_verified_rdc_ticket_fields_and_capture_path_remain_exact(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        path = project_restore._verified_rdc_restore_path(self.replay_path, self.capture_path)
        original = json.loads(path.read_text(encoding="utf-8"))
        for field in ("captureSha256", "sourceSha256", "originalRawSha256", "candidateRawSha256", "abiReportSha256"):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["shaders"][0]["replacement"]["restoreValidationReceipt"]["payload"][field] = "e" * 64
                changed.pop("sealSha256")
                changed["sealSha256"] = project_restore._canonical_sha256(changed)
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "signed validation receipt|signed Shader pair"):
                    project_restore._load_verified_rdc_restore(
                        host, replay, self.active, self.project, self.replay_path, self.capture_path,
                        lambda phase: None, host.restore_validation_policy_sha256)
        other = self.capture_path.with_name("other.rdc")
        other.write_bytes(self.capture_path.read_bytes())
        other_memo = project_restore._verified_rdc_restore_path(self.replay_path, other)
        other_memo.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "snapshot is invalid"):
            project_restore._load_verified_rdc_restore(
                host, replay, self.active, self.project, self.replay_path, other, lambda phase: None)
        self.assertEqual(host.verified_apply_calls, 0)

    def test_verified_rdc_changed_resource_id_is_fast_without_aliases(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        fast = host._apply_verified_project_restore
        def rebind(*args):
            result = fast(*args)
            result["items"][0]["replacement"]["originalResourceId"] = 77
            return result
        host._apply_verified_project_restore = rebind
        result = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["restoreMode"], "verified-rdc-restore", result)
        self.assertEqual(result["shaders"]["items"][0]["resourceId"], 77)
        self.assertEqual(host.shader_rollback_calls, 0)

    def test_verified_rdc_changed_alias_owner_rolls_back_and_rebuilds_inventory(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True, with_aliases=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        fast = host._apply_verified_project_restore
        def rebind(*args):
            result = fast(*args)
            result["items"][0]["replacement"]["originalResourceId"] = 77
            return result
        host._apply_verified_project_restore = rebind
        with mock.patch.object(project_restore, "_inventory_project_shader_targets", side_effect=RuntimeError("fallback reached")) as fallback:
            self._reopen_ticketed_restore(host, replay)
        self.assertEqual(host.shader_rollback_calls, 1)
        fallback.assert_called_once()
        self.assertNotIn("load_symbol_aliases", [method for method, unused in host.ipc.calls])

    def test_verified_rdc_restore_reapplies_aliases_without_preparing_shader(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True, with_aliases=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        prepare_calls = host.workspace_prepare_calls
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(warm["completed"], warm)
        self.assertEqual(warm["restoreMode"], "verified-rdc-restore", warm)
        self.assertEqual(host.workspace_prepare_calls, prepare_calls)
        self.assertIn("load_symbol_aliases", [method for method, unused in host.ipc.calls])

    def test_verified_rdc_host_rollback_failure_stops_instead_of_trying_full_apply(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        host._apply_verified_project_restore = mock.Mock(side_effect=project_restore.RestoreRollbackFailed("needs recovery"))
        with mock.patch.object(project_restore, "_inventory_project_shader_targets") as fallback:
            result = self._reopen_ticketed_restore(host, replay)
        self.assertFalse(result["completed"], result)
        fallback.assert_not_called()

    def test_real_gui_accepts_verified_subset_with_individual_failure_or_manual_result(self):
        cold, host, replay, unused = self._run(index_present=False, auto_prepare=True,
            source_preparation_only=True, host_mutator=self._ticketed_restore_host)
        spec = importlib.util.spec_from_file_location("subset_restore_client_contract",
            Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        for manual in (False, True):
            with self.subTest(manual=manual):
                result = copy.deepcopy(cold)
                ordinary = copy.deepcopy(result["shaders"]["items"][0])
                first = {key: ordinary[key] for key in ("workspaceId", "workspacePath", "resourceId", "stage", "replacementId", "knowledgeId")}
                first.update(status="applied", source="verified-rdc-restore", reusedValidation=True)
                if manual:
                    second = dict(ordinary, workspaceId="d"*32, replacementId="e"*32,
                        knowledgeId="f"*32, resourceId=ordinary["resourceId"]+1,
                        idempotentlyAdopted=False, reusedValidation=False,
                        pairValidationCache={"hit": False, "written": False, "reason": "manual-edit-unvalidated"})
                else:
                    second = {"status": "failed", "source": "reuse-plan", "knowledgeId": "f"*32,
                        "resourceId": ordinary["resourceId"]+1, "stage": ordinary["stage"], "error": "individual source needs repair"}
                result["shaders"].update(attempted=2, applied=1+int(manual), failed=int(not manual), skipped=0, items=[first, second])
                result["restoreMode"] = "verified-rdc-subset"
                result["cache"] = {"hit": True, "reason": "same-rdc-verified-subset"}
                result["verifiedRdcCache"] = {"hit": True, "written": False, "reason": "same-rdc-verified-subset"}
                result["realizedReceipts"].update(produced=1+int(manual), persisted=0, reusedValidation=1,
                    manualEdit=int(manual), mutationsCreated=1+int(manual), adoptedCurrentSession=0, idempotentApplyResponses=0)
                for key in ("inventoryScope", "fullFrame", "targetLookup"):
                    result.pop(key, None)
                result["inventoryCache"] = {"hit": False, "written": False, "reason": "not-read"}
                if manual:
                    result.pop("editing", None)
                elif "editing" in result:
                    result["editing"].update(initializationMs=100, startupReuse={"snapshotReuses": 5})
                    result["editing"]["items"][0]["displayAliasElapsedMs"] = 1.25
                client.ReplayLabRestoreHostClient._result(result, str(self.project), str(self.capture_path), self.session_id)
                wrong = copy.deepcopy(result)
                wrong["restoreMode"] = "verified-rdc-restore"
                with self.assertRaises(client.ReplayLabRestoreClientError):
                    client.ReplayLabRestoreHostClient._result(wrong, str(self.project), str(self.capture_path), self.session_id)

    def test_restore_on_demand_keeps_editable_sources_without_creating_watchers(self):
        def configure(host, replay):
            self._ticketed_restore_host(host, replay)
            host._shader_editors_on_demand = True
        from src.shader_workspace import alias_display_mirror
        names = {"status": "current", "writtenRows": 0, "writtenBlocks": 0}
        with mock.patch.object(alias_display_mirror, "restore_display_alias_mirrors", return_value=names) as mirror:
            result, host, replay, unused = self._run(index_present=False, auto_prepare=True,
                source_preparation_only=True, host_mutator=configure)
        mirror.assert_called_once()
        self.assertIsInstance(mirror.call_args.kwargs["metadata_cache"],
                              alias_display_mirror.DisplayAliasRestoreCache)
        self.assertEqual(result["editing"]["items"][0]["displayAliases"], names)
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["editing"]["ready"], 1, result)
        self.assertEqual(result["editing"]["enabled"], 0)
        self.assertEqual(result["editing"]["failed"], 0)
        self.assertEqual(result["editing"]["items"][0]["editingMode"], "on-demand")
        self.assertFalse(result["editing"]["items"][0]["backgroundAutoApplyAuthorized"])
        spec = importlib.util.spec_from_file_location("on_demand_restore_client",
            Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        client.ReplayLabRestoreHostClient._result(result, str(self.project), str(self.capture_path), self.session_id)
        cached = copy.deepcopy(result)
        cached["editing"]["items"][0]["displayAliases"]["metadataCacheHit"] = True
        client.ReplayLabRestoreHostClient._result(cached, str(self.project), str(self.capture_path), self.session_id)
        cached["editing"]["items"][0]["displayAliases"]["metadataCacheHit"] = "true"
        with self.assertRaises(client.ReplayLabRestoreClientError):
            client.ReplayLabRestoreHostClient._result(cached, str(self.project), str(self.capture_path), self.session_id)
        invalid = copy.deepcopy(result)
        invalid["editing"]["items"][0]["backgroundAutoApplyAuthorized"] = True
        with self.assertRaises(client.ReplayLabRestoreClientError):
            client.ReplayLabRestoreHostClient._result(invalid, str(self.project), str(self.capture_path), self.session_id)

    def test_restore_uses_project_snapshot_and_only_short_memo_commit(self):
        observed = {"snapshots": 0, "commits": 0, "asserts": 0}
        def configure(host, replay):
            self._ticketed_restore_host(host, replay)
            service = host.active_service
            @contextlib.contextmanager
            def snapshot(workspace_id, project):
                observed["snapshots"] += 1
                self.assertEqual(service.pin_depth, 0)
                yield "fixed-project"
            def check(token):
                self.assertEqual(token, "fixed-project")
                self.assertEqual(service.pin_depth, 0)
                observed["asserts"] += 1
            @contextlib.contextmanager
            def commit(token):
                check(token)
                observed["commits"] += 1
                yield
            service.snapshot_active_project = snapshot
            service.assert_active_project_snapshot = check
            service.commit_active_project_snapshot = commit
            service.pin_active_project = mock.Mock(side_effect=AssertionError("long selection pin must not be used"))
        result, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=configure)
        self.assertTrue(result.get("verifiedRdcCache", {}).get("written"), result)
        self.assertEqual(observed["snapshots"], 1)
        self.assertEqual(observed["commits"], 1)
        self.assertGreater(observed["asserts"], 10)
        warm = self._reopen_ticketed_restore(host, replay)
        self.assertTrue(warm["completed"], warm)
        self.assertEqual(observed["snapshots"], 2)
        self.assertEqual(observed["commits"], 1)

    def test_incomplete_reflection_reuses_current_local_candidate_and_revalidates_apply(self):
        def existing_candidate(host, replay):
            # Actual 6.rdc recovered HLSL ABI digest differs from its baseline.
            host.candidate_abi_sha256 = "2efd2ed6bbfe19536f32e879b7a1260602c6291289e7e57dba1fca23b32d625b"
            host.target_status["revision"] = 1
            host.target_status["latestCompiled"] = {
                "revision":1,"sourceSha256":self.source_sha,"rawSha256":"f"*64,
                "kind":"DXBC","byteSize":4}
        result, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=existing_candidate,
            inventory_register_error=AssertionError("source reuse must not register again"))
        self.assertTrue(result["completed"], result)
        self.assertEqual(result["shaders"]["applied"],1, result)
        self.assertEqual(result["editing"]["enabled"],1, result)
        self.assertEqual(result["realizedReceipts"]["produced"],1)
        self.assertEqual(result["realizedReceipts"]["persisted"],0)
        self.assertFalse(result["index"]["written"])
        self.assertIn("fresh-live-abi-no-executable-index",result["cache"]["reason"])
        self.assertEqual(host.target_status["revision"],1)
        self.assertEqual(host.compile_calls,0)
        self.assertEqual(host.apply_calls,1)
        self.assertNotEqual(host.last_apply_response["validation"]["candidateAbiSha256"],
                            host.last_apply_response["validation"]["baselineAbiSha256"])
        self.assertEqual(host.seed_calls,0)
        self.assertEqual(host.workspace_service.import_calls,0)
        self.assertEqual(replay.inventory_register_calls,0)
        self.assertEqual(host.target_status["source"]["projectSource"]["knowledgeId"],self.knowledge_id)
        self.assertNotIn("get_shared_shader_replacements",[method for method,unused in host.ipc.calls])
        self.assertNotIn("write_rdc_project_index",[method for method,unused in host.ipc.calls])
        self.assertFalse(replay.last_inventory_snapshot["knowledge"]["workspaces"][self.knowledge_id]["interfaceComplete"])
        # Exercise the real GUI client contract: an observed successful apply
        # need not publish a fictitiously complete executable RDC receipt.
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("source_restore_client_contract", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        accepted = client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)
        self.assertEqual(accepted["editing"]["enabled"], 1)

    def test_incomplete_reflection_recompiles_a_stale_local_candidate(self):
        for stale_field, stale_value in (("sourceSha256", "e" * 64), ("revision", 0)):
            with self.subTest(stale_field=stale_field):
                def stale_candidate(host, replay):
                    host.target_status["revision"] = 1
                    host.target_status["latestCompiled"] = {
                        "revision": 1, "sourceSha256": self.source_sha,
                        "rawSha256": "f" * 64, "kind": "DXBC", "byteSize": 4,
                        stale_field: stale_value,
                    }
                    host.compile_revision = 2

                result, host, unused_replay, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=stale_candidate)
                self.assertTrue(result["completed"], result)
                self.assertEqual(result["shaders"]["applied"], 1, result)
                self.assertEqual(host.compile_calls, 1)
                self.assertEqual(host.target_status["revision"], 2)
                self.assertEqual(host.apply_calls, 1)
                self.assertFalse(result["index"]["written"])

    def test_actual_capture_distinct_compatible_abi_digests_bind_current_report(self):
        # Values from the accepted 6.rdc public apply receipt, 2026-09-06.
        status = copy.deepcopy(self.workspace_status)
        status["baseline"]["abiSha256"] = "b6020009d362764b604f9abd81b0e1880ecfcca4d997816d632ccd7845a2219e"
        host = _Host(self.active, status, self.capture_path, self.session_id,
                     None, self.identity_evidence)
        host.candidate_abi_sha256 = "2efd2ed6bbfe19536f32e879b7a1260602c6291289e7e57dba1fca23b32d625b"
        workspace = Path(status["workspacePath"])
        wrapped = json.loads(host.apply_shader_workspace_shared_resource(
            str(workspace), status["revision"], self.candidate_sha, "7" * 32,
            expected_anchor_event_id=9))
        checked = project_restore._validate_workspace_apply_response(
            host, workspace, status, wrapped, operation_id="7" * 32,
            expected_revision=status["revision"], expected_candidate_raw_sha256=self.candidate_sha,
            expected_anchor_event_id=9, capture_session_id=self.session_id)
        self.assertIs(checked, wrapped)
        self.assertNotEqual(checked["validation"]["candidateAbiSha256"],
                            checked["validation"]["baselineAbiSha256"])

    def test_source_preparation_checks_original_source_profile_abi_and_project_reference(self):
        for field, replacement in (("knowledgeId","7"*32),("sourceSha256","a"*64),
                                   ("baselineAbiSha256","a"*64),("originalRawSha256","a"*64),
                                   ("projectId","a"*32),("entryPoint","different"),
                                   ("targetProfile",{"profile":"ps_4_0"})):
            with self.subTest(field=field):
                result, host, unused, unused_progress = self._run(
                    index_present=False,auto_prepare=True,source_preparation_only=True,
                    source_preparation_mutator=lambda host,replay:
                        host.target_status["source"]["projectSource"].update({field:replacement}))
                self.assertEqual(result["shaders"]["applied"],0,result)
                self.assertEqual(host.compile_calls,0)
                self.assertEqual(host.apply_calls,0)
                self.assertEqual(host.edit_start_calls,[])

    def _published_source_revision(self, host, replay):
        status = host.target_status
        status["source"].update(origin="verified-project-source", decompiledSha256="a" * 64)
        status["source"]["projectSource"].update(knowledgeId="7" * 32, sourceSha256="a" * 64)
        replay.state["workspaces"][self.knowledge_id]["registeredWorkspace"] = {
            "workspaceId": status["workspaceId"], "path": status["workspacePath"]}

    def test_registered_same_rdc_source_skips_workspace_resolution_and_preparation(self):
        def registered(host, replay):
            status = host.target_status
            replay.state["workspaces"][self.knowledge_id]["registeredWorkspace"] = {
                "workspaceId": status["workspaceId"], "path": status["workspacePath"]}
            host.get_active_shader_workspace_path = mock.Mock(
                side_effect=AssertionError("known source must not re-export its catalog"))

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, precreate_target_workspace=True,
            source_preparation_only=True, source_preparation_mutator=registered)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        host.get_active_shader_workspace_path.assert_not_called()
        self.assertEqual(host.workspace_prepare_calls, 0)

    @staticmethod
    def _without_project_source_receipt(host, replay):
        host.target_status["source"].pop("projectSource")
        host.target_status["source"]["origin"] = "decompiler"

    def test_existing_matching_source_restores_without_import_receipt(self):
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=self._without_project_source_receipt)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(host.seed_calls, 0)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.workspace_service.import_calls, 0)
        self.assertNotIn("projectSource", host.target_status["source"])
        self.assertFalse(result["index"]["written"])

    def test_seeded_retry_renews_missing_receipt_before_compiling_with_old_draft(self):
        def interrupted_seed(host, replay):
            status = host.target_status
            receipt = copy.deepcopy(status["source"]["projectSource"])
            self._without_project_source_receipt(host, replay)
            status["source"].update(origin="captured-native-source", modifiedSinceDecompile=True)
            gate = Path(status["workspacePath"]) / "ai" / "organize-code-gate.json"
            gate.parent.mkdir(parents=True, exist_ok=True)
            gate.write_text("{}")
            original_seed = replay.inventory_seed_handler
            def renew(*args, **kwargs):
                result = original_seed(*args, **kwargs)
                status["source"].update(origin="verified-project-source", projectSource=receipt)
                return result
            replay.inventory_seed_handler = renew
            original_compile = host.compile_shader_workspace
            def compile_after_receipt(*args, **kwargs):
                self.assertEqual(host.seed_calls, 1)
                self.assertEqual(status["source"]["projectSource"], receipt)
                return original_compile(*args, **kwargs)
            host.compile_shader_workspace = compile_after_receipt
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, precreate_target_workspace=True,
            source_preparation_only=True, source_preparation_mutator=interrupted_seed)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(host.seed_calls, 1)
        self.assertEqual(host.seed_supersede_calls, 0)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)

    def test_imported_workspace_renews_stale_receipt_after_seed_or_retry(self):
        for already_seeded in (False, True):
            with self.subTest(retry=already_seeded):
                def stale_import(host, replay):
                    status = host.target_status
                    status["source"].update(
                        origin="verified-project-source", decompiledSha256="a" * 64,
                        currentSha256=self.source_sha if already_seeded else "a" * 64,
                        modifiedSinceDecompile=False)
                    status["source"]["projectSource"].update(knowledgeId="7" * 32, sourceSha256="a" * 64)
                    # The active head was registered in another workspace.
                    replay.state["workspaces"][self.knowledge_id]["registeredWorkspace"] = {
                        "workspaceId": "8" * 32, "path": "other-workspace"}
                    original_seed = replay.inventory_seed_handler

                    def renew(*args, **kwargs):
                        result = original_seed(*args, **kwargs)
                        status["source"].update(decompiledSha256=self.source_sha)
                        status["source"]["projectSource"].update(
                            knowledgeId=self.knowledge_id, sourceSha256=self.source_sha)
                        return result
                    replay.inventory_seed_handler = renew

                result, host, unused_replay, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=stale_import)
                self.assertEqual(result["shaders"]["applied"], 1, result)
                self.assertEqual(host.seed_calls, 1)
                self.assertEqual(host.seed_supersede_calls, 0)
                self.assertEqual(host.compile_calls, 1)
                self.assertEqual(host.apply_calls, 1)
                self.assertEqual(host.target_status["source"]["projectSource"]["knowledgeId"], self.knowledge_id)
                self.assertEqual(host.target_status["source"]["projectSource"]["sourceSha256"], self.source_sha)

    def test_existing_different_source_without_receipt_is_preserved(self):
        def local_source(host, replay):
            self._without_project_source_receipt(host, replay)
            host.target_status["source"].update(
                currentSha256="e" * 64, modifiedSinceDecompile=True)

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=local_source)
        self.assertEqual(result["shaders"]["skipped"], 1, result)
        self.assertEqual(host.seed_calls, 0)
        self.assertEqual(host.target_status["source"]["currentSha256"], "e" * 64)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.workspace_service.import_calls, 0)
        self.assertNotIn("projectSource", host.target_status["source"])
        self.assertFalse(result["index"]["written"])

    def test_newer_validated_local_source_restores_without_seed_compile_or_publication(self):
        self._assert_local_candidate_restore(registered=True)

    def test_old_published_candidate_advances_to_current_head_and_reports_actual_bytes(self):
        receipt = {"previousKnowledgeId": "a" * 32, "generation": 15}

        def old_published(host, replay):
            status = host.target_status
            status["revision"] = 21
            status["source"].update(currentSha256="e" * 64, modifiedSinceDecompile=True)
            status["latestCompiled"] = {
                "revision": 21, "sourceSha256": "e" * 64, "rawSha256": "a" * 64,
                "kind": "DXBC", "byteSize": 4, "replayAbiValidated": True,
                "abiCompatible": True, "requiresReplayAbiValidation": False,
                "abiGate": {"status": "compatible"},
            }
            host.compile_revision = 22
            replay.published_workspace_source_advance = mock.Mock(return_value=receipt)
            original_seed = replay.inventory_seed_handler

            def advance(*args, **kwargs):
                self.assertEqual(kwargs, {"published_source_advance": receipt})
                seeded = original_seed(*args, **kwargs)
                status["source"].update(decompiledSha256=self.source_sha)
                status["source"]["projectSource"].update(
                    knowledgeId=self.knowledge_id, sourceSha256=self.source_sha)
                return seeded
            replay.inventory_seed_handler = advance

        result, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, precreate_target_workspace=True,
            source_preparation_only=True, source_preparation_mutator=old_published)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(result["editing"]["enabled"], 1, result)
        item = result["shaders"]["items"][0]
        self.assertFalse(item["localValidatedCandidate"])
        self.assertEqual(item["knowledgeId"], self.knowledge_id)
        self.assertEqual(item["sourceSha256"], self.source_sha)
        self.assertEqual(item["sourceSha256"], item["projectSourceSha256"])
        self.assertEqual(item["candidateRawSha256"], host.target_status["latestCompiled"]["rawSha256"])
        self.assertNotEqual(item["candidateRawSha256"], "a" * 64)
        self.assertEqual(item["candidateRevision"], 22)
        self.assertEqual(item["supersededLocalEdit"]["sourceSha256"], "e" * 64)
        self.assertEqual(host.seed_calls, 1)
        self.assertEqual(host.seed_supersede_calls, 0)
        self.assertEqual(host.seed_published_advance, receipt)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(replay.inventory_register_calls, 0)
        replay.published_workspace_source_advance.assert_called_once()

    def test_existing_validated_local_source_is_found_before_public_prepare(self):
        self._assert_local_candidate_restore(registered=False)

    def _assert_local_candidate_restore(self, *, registered):
        local_sha = "e" * 64

        def current_local(host, replay):
            status = host.target_status
            status["revision"] = 21
            status["source"].update(currentSha256=local_sha, modifiedSinceDecompile=True)
            status["latestCompiled"] = {
                "revision": 21, "sourceSha256": local_sha, "rawSha256": "f" * 64,
                "kind": "DXBC", "byteSize": 4, "replayAbiValidated": True,
                "abiCompatible": True, "requiresReplayAbiValidation": False,
                "abiGate": {"status": "compatible"},
            }
            if registered:
                replay.state["workspaces"][self.knowledge_id]["registeredWorkspace"] = {
                    "workspaceId": status["workspaceId"], "path": str(host.target_workspace)}
                host.get_active_shader_workspace_path = mock.Mock(
                    side_effect=AssertionError("registered local source must not re-export its catalog"))

        result, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, precreate_target_workspace=True,
            source_preparation_only=True, source_preparation_mutator=current_local,
            inventory_register_error=AssertionError("local source must not change shared knowledge"))
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(result["editing"]["enabled"], 1, result)
        self.assertTrue(result["shaders"]["items"][0]["localValidatedCandidate"])
        self.assertEqual(host.target_status["source"]["currentSha256"], local_sha)
        self.assertEqual(host.target_status["revision"], 21)
        self.assertEqual(host.seed_calls, 0)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(replay.inventory_register_calls, 0)
        self.assertEqual(replay.state["workspaces"][self.knowledge_id]["sourceSha256"], self.source_sha)
        self.assertFalse(result["index"]["written"])
        self.assertEqual(result["realizedReceipts"]["persisted"], 0)
        if registered:
            host.get_active_shader_workspace_path.assert_not_called()
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("local_candidate_restore_client", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        accepted = client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)
        self.assertTrue(accepted["shaders"]["items"][0]["localValidatedCandidate"])

    def test_source_without_receipt_still_requires_current_target_identity(self):
        cases = (
            ("raw", lambda status: status["baseline"].update(rawSha256="e" * 64)),
            ("abi", lambda status: status["baseline"].update(abiSha256="invalid")),
            ("encoding", lambda status: status["baseline"].update(kind="SPIR-V")),
            ("api", lambda status: status["capture"].update(api="Vulkan")),
            ("stage", lambda status: status.update(stage="vertex")),
            ("entry", lambda status: status.update(entryPoint="otherMain")),
            ("profile", lambda status: status.update(targetProfile={"profile": "ps_4_0"})),
        )
        for name, mutate in cases:
            with self.subTest(changed=name):
                def different_target(host, replay):
                    self._without_project_source_receipt(host, replay)
                    mutate(host.target_status)

                result, host, unused_replay, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=different_target)
                self.assertEqual(result["shaders"]["applied"], 0, result)
                self.assertEqual(host.seed_calls, 0)
                self.assertEqual(host.compile_calls, 0)
                self.assertEqual(host.apply_calls, 0)

    def test_source_receipt_cannot_be_bypassed_by_changing_origin(self):
        for origin, receipt in (("verified-project-source", None),
                                ("decompiler", {}),
                                ("decompiler", {"schema": "invalid"})):
            with self.subTest(origin=origin, receipt=receipt):
                def malformed_receipt(host, replay):
                    host.target_status["source"].update(origin=origin, projectSource=receipt)

                result, host, unused_replay, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=malformed_receipt)
                self.assertEqual(result["shaders"]["applied"], 0, result)
                self.assertEqual(host.compile_calls, 0)
                self.assertEqual(host.apply_calls, 0)

    def test_published_workspace_revision_preserves_import_receipt_and_restores(self):
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=self._published_source_revision)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(host.seed_calls, 0)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.target_status["source"]["projectSource"]["knowledgeId"], "7" * 32)
        self.assertEqual(host.target_status["source"]["projectSource"]["sourceSha256"], "a" * 64)

    def test_same_content_knowledge_rebind_restores_without_seed(self):
        """Re-registering identical organized bytes under a new knowledgeId.

        The import receipt still names the older knowledge. Workspace bytes already
        match the active head, so restore must not fail closed on knowledgeId.
        """

        def same_content_rebind(host, replay):
            status = host.target_status
            status["source"].update(
                origin="verified-project-source",
                currentSha256=self.source_sha,
                decompiledSha256=self.source_sha,
            )
            status["source"]["projectSource"].update(
                knowledgeId="c" * 32,
                sourceSha256=self.source_sha,
            )

        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            source_preparation_only=True,
            source_preparation_mutator=same_content_rebind,
        )
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(host.seed_calls, 0)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(
            host.target_status["source"]["projectSource"]["knowledgeId"], "c" * 32
        )

    def test_published_revision_after_toolchain_refresh_preserves_import_receipt(self):
        for corrupted in (False, True):
            with self.subTest(corrupted=corrupted):
                def refreshed(host, replay):
                    self._published_source_revision(host, replay)
                    source = host.target_status["source"]
                    origin = copy.deepcopy(source["projectSource"])
                    source.update(origin="toolchain-refreshed-source", toolchainRefresh={
                        "preservedSourceSha256": "0" * 64 if corrupted else origin["sourceSha256"],
                        "sourceProvenance": {"origin": "verified-project-source",
                            "sha256": origin["sourceSha256"], "projectSource": origin}})
                result, host, unused_replay, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=refreshed)
                self.assertEqual(result["shaders"]["applied"], int(not corrupted), result)
                self.assertEqual(host.seed_calls, 0)
                self.assertEqual(host.target_status["source"]["projectSource"]["knowledgeId"], "7" * 32)

    def test_published_revision_still_requires_same_registered_workspace_and_physical_origin(self):
        cases = (
            ("registration-id", lambda s, k: k["registeredWorkspace"].update(workspaceId="8" * 32)),
            ("registration-path", lambda s, k: k["registeredWorkspace"].update(path="other-workspace")),
            ("missing-registration", lambda s, k: k.pop("registeredWorkspace")),
            ("decompiled-input", lambda s, k: s["source"].update(decompiledSha256="b" * 64)),
            ("origin", lambda s, k: s["source"].update(origin="other")),
            ("original-raw", lambda s, k: s["source"]["projectSource"].update(originalRawSha256="0" * 64)),
            ("baseline-abi", lambda s, k: s["source"]["projectSource"].update(baselineAbiSha256="b" * 64)),
            ("project", lambda s, k: s["source"]["projectSource"].update(projectId="b" * 32)),
            ("profile", lambda s, k: s["source"]["projectSource"].update(targetProfile={"profile": "ps_4_0"})),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                def invalid(host, replay):
                    self._published_source_revision(host, replay)
                    mutate(host.target_status, replay.state["workspaces"][self.knowledge_id])
                result, host, unused, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=invalid)
                self.assertEqual(result["shaders"]["applied"], 0, result)
                self.assertEqual(host.compile_calls, 0)
                self.assertEqual(host.apply_calls, 0)

    def test_unregistered_local_source_is_not_a_published_revision(self):
        def local_edit(host, replay):
            self._published_source_revision(host, replay)
            status = host.target_status
            status["source"]["currentSha256"] = "e" * 64
            with self.assertRaises(project_restore.RestoreLocalEdit):
                project_restore._validate_prepared_project_source(status,
                    replay.state["workspaces"][self.knowledge_id], self.identity, self.state)
            # Do not alter the existing project-seed/backup policy after the
            # shortcut rejected these unregistered bytes.
            status["source"]["currentSha256"] = self.source_sha
        result, host, unused, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=local_edit)
        self.assertEqual(result["shaders"]["applied"], 1, result)

    def _source_snapshot_with_old_identity(self, host, replay, **changes):
        original_snapshot = replay.inventory_snapshot_handler

        def snapshot(inventory, expected_binding):
            result = original_snapshot(inventory, expected_binding)
            knowledge = result["knowledge"]["workspaces"][self.knowledge_id]
            physical = knowledge["physicalIdentity"]
            physical.update(changes)
            physical["stableKeySha256"] = project_restore._canonical_sha256(
                {key: value for key, value in physical.items()
                 if key != "stableKeySha256"})
            knowledge["compiledCandidate"] = None
            return result

        replay.inventory_snapshot_handler = snapshot

    def test_raw_exact_source_restores_with_changed_old_reflection_evidence(self):
        def old_reflection(host, replay):
            self._source_snapshot_with_old_identity(
                host, replay, abiHash="a" * 64, layoutHash="b" * 64)

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=old_reflection)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.workspace_service.import_calls, 0)
        self.assertEqual(
            host.target_status["source"]["projectSource"]["baselineAbiSha256"],
            self.live_abi_sha)
        self.assertFalse(result["index"]["written"])

    def test_raw_exact_source_restores_with_missing_old_reflection_evidence(self):
        def missing_reflection(host, replay):
            self._source_snapshot_with_old_identity(host, replay, abiHash="", layoutHash="")

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=missing_reflection)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.workspace_service.import_calls, 0)
        self.assertFalse(result["index"]["written"])

    def test_source_recovery_still_requires_same_original_program_and_entry(self):
        for key, value in (("rawSha256", "e" * 64),
                           ("stage", "vertex"), ("entryPoint", "OtherMain")):
            with self.subTest(field=key):
                def different_original(host, replay):
                    self._source_snapshot_with_old_identity(host, replay, **{key: value})

                result, host, unused_replay, unused_progress = self._run(
                    index_present=False, auto_prepare=True, source_preparation_only=True,
                    source_preparation_mutator=different_original)
                self.assertEqual(result["shaders"]["applied"], 0, result)
                self.assertEqual(host.compile_calls, 0)
                self.assertEqual(host.apply_calls, 0)

    def test_changed_old_abi_source_still_rejects_failed_current_abi(self):
        def old_reflection(host, replay):
            self._source_snapshot_with_old_identity(
                host, replay, abiHash="a" * 64, layoutHash="b" * 64)

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=old_reflection,
            apply_response_mutator=lambda response: response["validation"].update(
                compatible=False))
        self.assertEqual(result["shaders"]["applied"], 0, result)
        self.assertEqual(result["shaders"]["failed"], 1, result)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.edit_start_calls, [])
        self.assertFalse(result["index"]["written"])

    def test_source_preparation_keeps_compile_and_live_abi_gates(self):
        cases = (
            {"compile_error":RuntimeError("compiler rejected source")},
            {"apply_response_mutator":lambda response:response["validation"].update(compatible=False)},
            {"apply_response_mutator":lambda response:response["replacement"].update(captureSessionId="e"*32)},
        )
        for options in cases:
            with self.subTest(options=options):
                result,host,replay,unused = self._run(
                    index_present=False,auto_prepare=True,source_preparation_only=True,**options)
                self.assertEqual(result["shaders"]["applied"],0,result)
                self.assertEqual(result["shaders"]["failed"],1,result)
                self.assertEqual(host.edit_start_calls,[])
                self.assertEqual(replay.inventory_register_calls,0)
                self.assertFalse(result["index"]["written"])

    def test_source_preparation_revalidates_project_head_before_apply(self):
        def change_head(host,replay):
            original = replay.inventory_snapshot_handler
            checks = []
            def snapshot(inventory,binding):
                if binding is not None:
                    checks.append(True)
                    if len(checks) >= 3:
                        raise RuntimeError("active project knowledge head changed")
                return original(inventory,binding)
            replay.inventory_snapshot_handler = snapshot
        result,host,unused,unused_progress = self._run(
            index_present=False,auto_prepare=True,source_preparation_only=True,
            source_preparation_mutator=change_head)
        self.assertEqual(result["shaders"]["applied"],0,result)
        self.assertEqual(host.compile_calls,1)
        self.assertEqual(host.apply_calls,0)
        self.assertEqual(host.edit_start_calls,[])

    def test_source_preparation_stable_identity_block_is_reported_without_work(self):
        def block_candidate(host, replay):
            match = host.prepared_response["reusePlan"]["matches"][0]
            match["workspaceCandidates"] = []
            match["sourcePreparationBlockedReason"] = "project-shader-stable-identity-mismatch"
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=block_candidate)
        self.assertEqual(result["shaders"]["attempted"], 0)
        self.assertEqual(result["shaders"]["skipped"], 1)
        self.assertEqual(result["shaders"]["items"][0]["reason"],
                         "project-shader-stable-identity-mismatch")
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.edit_start_calls, [])

    def test_source_preparation_does_not_accept_binary_authority(self):
        def allow_binary(host, replay):
            candidate = host.prepared_response["reusePlan"]["matches"][0]["workspaceCandidates"][0]
            candidate["binaryCandidateEligible"] = True
        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            source_preparation_mutator=allow_binary)
        self.assertEqual(result["shaders"]["skipped"], 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.edit_start_calls, [])

    def test_two_executable_entries_for_one_resource_fail_closed_as_a_group(
        self,
    ) -> None:
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            None,
            self.identity_evidence,
        )
        self._configure_reuse_plan(host)
        duplicate = copy.deepcopy(
            host.prepared_response["reusePlan"]["matches"][0]
        )
        host.prepared_response["reusePlan"]["matches"].append(duplicate)
        host.prepared_response["reusePlan"]["matchedShaderCount"] = 2
        replay = _ReplayService(self.state)
        shaders = {
            "attempted": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }
        materials = {
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "sessionIds": [],
            "items": [],
        }

        with mock.patch.object(
            project_restore,
            "_restore_reuse_aliases_best_effort",
            return_value=True,
        ):
            records, material_records, unused_state = (
                project_restore._restore_from_reuse_plan(
                    host,
                    replay,
                    copy.deepcopy(self.active),
                    self.project,
                    self.capture_path,
                    self.session_id,
                    host.prepared_response,
                    {},
                    project_restore._Progress("1" * 32, None, []),
                    shaders,
                    materials,
                    [],
                    [],
                    [],
                )
            )

        self.assertEqual(records, [])
        self.assertEqual(material_records, [])
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(shaders["attempted"], 0)
        self.assertEqual(shaders["applied"], 0)
        self.assertEqual(shaders["skipped"], 2)
        self.assertTrue(
            all(
                "multiple executable entry points" in item["reason"]
                for item in shaders["items"]
            )
        )

    def test_one_executable_entry_for_one_resource_applies_exactly_once(
        self,
    ) -> None:
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            None,
            self.identity_evidence,
        )
        self._configure_reuse_plan(host)
        metadata_only = copy.deepcopy(
            host.prepared_response["reusePlan"]["matches"][0]
        )
        metadata_only["workspaceCandidates"] = []
        host.prepared_response["reusePlan"]["matches"].append(metadata_only)
        host.prepared_response["reusePlan"]["matchedShaderCount"] = 2
        replay = _ReplayService(self.state)
        shaders = {
            "attempted": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }
        materials = {
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "sessionIds": [],
            "items": [],
        }
        applied_workspaces = []

        with mock.patch.object(
            project_restore,
            "_restore_reuse_aliases_best_effort",
            return_value=True,
        ):
            records, material_records, unused_state = (
                project_restore._restore_from_reuse_plan(
                    host,
                    replay,
                    copy.deepcopy(self.active),
                    self.project,
                    self.capture_path,
                    self.session_id,
                    host.prepared_response,
                    {},
                    project_restore._Progress("2" * 32, None, []),
                    shaders,
                    materials,
                    applied_workspaces,
                    [],
                    [],
                )
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(material_records, [])
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(len(applied_workspaces), 1)
        self.assertEqual(shaders["attempted"], 1)
        self.assertEqual(shaders["applied"], 1)
        self.assertEqual(shaders["skipped"], 1)

    def test_existing_reuse_workspace_is_readmitted_for_frozen_inventory_anchor(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            precreate_target_workspace=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.workspace_rebind_calls, 1)
        self.assertEqual(host.apply_calls, 1)

    def test_old_abi_receipt_reopens_exact_source_before_prepare_retry(self) -> None:
        recovered = []

        def configure(host, unused_replay):
            original_prepare = host.prepare_shader_workspace
            calls = 0

            def prepare(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return json.dumps({
                        "error": "candidate ABI report does not prove exact compatibility "
                                 "(DXIL access evidence unavailable after bounded recheck)"
                    })
                return original_prepare(*args, **kwargs)

            class RecoveryService:
                def reopen_candidate_for_project_restore(self, workspace, **expected):
                    recovered.append((Path(workspace), expected))
                    return True

            host.prepare_shader_workspace = prepare
            host._shader_workspace_service = RecoveryService

        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            precreate_target_workspace=True,
            target_source_modified=False,
            host_mutator=configure,
        )
        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.seed_calls, 1)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0][0], host.target_workspace)
        self.assertEqual(recovered[0][1]["expected_source_sha256"], self.source_sha)
        self.assertEqual(recovered[0][1]["expected_resource_id"], self.identity["resourceId"])

    def test_existing_source_guard_can_reopen_exact_stale_abi_before_prepare(self):
        reopened = []

        def configure(host, replay):
            original_status = host.workspace_service.status
            stale = True

            def status(path):
                if stale and Path(path).resolve() == host.target_workspace.resolve():
                    raise RuntimeError("candidate ABI report does not prove exact compatibility (stale)")
                return original_status(path)

            def reopen(path, **expected):
                nonlocal stale
                self.assertEqual(expected["expected_source_sha256"], self.source_sha)
                self.assertEqual(host.target_status["source"]["currentSha256"], self.source_sha)
                reopened.append(Path(path))
                stale = False
                return True

            host.workspace_service.status = status
            host.workspace_service.reopen_candidate_for_project_restore = reopen

        result, host, unused_replay, unused_progress = self._run(
            index_present=False, auto_prepare=True, precreate_target_workspace=True,
            source_preparation_only=True, host_mutator=configure)
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(reopened, [host.target_workspace])
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.seed_calls, 0)

    def test_project_shader_preserves_local_edit_before_public_prepare(self) -> None:
        """Automatic restore must never seed an older artifact over a user's draft."""

        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            precreate_target_workspace=True,
            target_source_modified=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["skipped"], 1)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertEqual(host.seed_calls, 0)
        self.assertEqual(host.workspace_prepare_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        item = result["shaders"]["items"][0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["reason"], "local-edit")
        self.assertEqual(item["workspaceSourceSha256"], "e" * 64)
        self.assertEqual(result["shaders"]["attempted"], 0)
        client_path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_restore_client.py"
        spec = importlib.util.spec_from_file_location("preserved_draft_restore_client", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        accepted = client.ReplayLabRestoreHostClient._result(
            result, str(self.project), str(self.capture_path), self.session_id)
        self.assertEqual(accepted["shaders"]["items"][0]["reason"], "local-edit")
        inconsistent = copy.deepcopy(result)
        inconsistent["shaders"]["attempted"] = 1
        with self.assertRaises(client.ReplayLabRestoreClientError):
            client.ReplayLabRestoreHostClient._result(
                inconsistent, str(self.project), str(self.capture_path), self.session_id)

    def test_existing_reuse_workspace_rejects_historical_anchor_before_apply(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            precreate_target_workspace=True,
            inventory_asset_mutator=lambda asset: asset.update(
                {"anchorEventId": 10}
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertIn(
            "frozen representative EID",
            result["shaders"]["items"][0]["error"],
        )
        self.assertIn(
            "workspacePath=%s" % host.target_workspace,
            result["shaders"]["items"][0]["error"],
        )
        self.assertEqual(host.workspace_prepare_calls, 1)
        self.assertEqual(host.workspace_rebind_calls, 1)
        self.assertEqual(host.apply_calls, 0)
        self.assertFalse(result["index"]["written"])

    def test_inventory_restore_never_invokes_full_catalog_export_progress(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            prepare_progress_events=[
                {
                    "schema": "replaylab-shader-catalog-progress/v1",
                    "phase": "wrong-phase",
                    "current": 1,
                    "total": 2,
                    "eventId": 8,
                    "updatedUnixMs": 100,
                },
                {
                    "schema": "replaylab-shader-catalog-progress/v1",
                    "phase": "export-shader-bindings",
                    "current": 1,
                    "total": 2,
                    "eventId": 9,
                    "updatedUnixMs": 101,
                },
                {
                    "schema": "replaylab-shader-catalog-progress/v1",
                    "phase": "export-shader-bindings",
                    "current": 2,
                    "total": 2,
                    "eventId": 10,
                    "updatedUnixMs": 102,
                },
            ],
        )

        self.assertTrue(result["completed"])
        export_events = [
            event
            for event in progress
            if event["phase"] == "export-shader-bindings"
        ]
        self.assertEqual(export_events, [])
        self.assertEqual(host.prepare_calls, 0)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertIn("resource-match", phases)
        self.assertNotIn("prepare", phases)

    def test_absent_index_does_not_import_event_material_values(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["materials"]["attempted"], 0)
        self.assertEqual(result["materials"]["restored"], 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["failed"], 0)
        self.assertEqual(result["materials"]["sessionIds"], [])
        self.assertEqual(result["materials"]["items"], [])
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertTrue(result["index"]["written"])
        phases = [event["phase"] for event in progress]
        self.assertNotIn("material-restore", phases)
        selected = host.ipc.index["bindings"][0]
        self.assertEqual(selected["materials"], [])

    def test_inventory_restore_never_invokes_event_material_update_route(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
            material_update_error=RuntimeError("material update rejected"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(result["materials"]["restored"], 0)
        self.assertEqual(result["materials"]["failed"], 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["items"], [])
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertTrue(result["index"]["written"])
        self.assertIsNotNone(host.ipc.index)
        self.assertIsNone(result["index"]["warning"])
        methods = [method for method, unused in host.ipc.calls]
        self.assertIn("write_rdc_project_index", methods)
        self.assertNotIn("material-restore", [event["phase"] for event in progress])
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_verified_compiled_candidate_skips_compile_but_still_live_applies(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertTrue(result["index"]["written"])
        phases = [event["phase"] for event in progress]
        self.assertIn("shader-candidate-load", phases)
        self.assertNotIn("shader-compile", phases)
        self.assertLess(
            phases.index("shader-candidate-load"), phases.index("shader-apply")
        )

    def test_same_hlsl_candidate_with_different_compile_profile_is_reimported(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
            preexisting_binary_profile_mismatch=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(
            host.target_status["latestCompiled"]["projectImport"]["targetProfile"],
            host.target_status["targetProfile"],
        )

    def test_same_binary_with_different_compiler_flags_binding_is_reimported(self) -> None:
        result, host, unused_replay, unused_progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
            preexisting_binary_compiler_mismatch=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 1)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(
            host.target_status["latestCompiled"]["projectImport"][
                "compilerBindingSha256"
            ],
            "d" * 64,
        )

    def test_binary_import_fallback_refuses_concurrently_changed_source(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_binary=True,
            binary_import_error=RuntimeError("candidate import rejected"),
            binary_import_source_change="1" * 64,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertIn(
            "source changed before fallback compile",
            result["shaders"]["items"][0]["error"],
        )
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertIsNone(host.ipc.index)
        phases = [event["phase"] for event in progress]
        self.assertIn("shader-candidate-load", phases)
        self.assertNotIn("shader-compile", phases)

    def test_include_unbound_portable_binary_falls_back_to_one_current_compile(self):
        result, host, unused_replay, progress = self._run(
            index_present=False, auto_prepare=True, with_binary=True,
            binary_import_error=RuntimeError(
                "project compiled candidate has no include dependency binding; compile the current source unit"))
        self.assertEqual(result["shaders"]["applied"], 1, result)
        self.assertEqual(result["shaders"]["failed"], 0)
        self.assertEqual(host.workspace_service.import_calls, 1)
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual([item["phase"] for item in progress].count("shader-compile"), 1)

    def test_header_only_edit_invalidates_warm_restore_and_never_applies_stale_binary(self):
        cold, host, replay, unused = self._run(
            index_present=False, auto_prepare=True, source_preparation_only=True,
            host_mutator=self._ticketed_restore_host)
        self.assertTrue(cold.get("verifiedRdcCache", {}).get("written"), cold)
        previous_compile = host.compile_calls
        previous_apply = host.apply_calls
        host.target_status["sourceDependenciesCurrent"] = False
        host.target_status["source"]["dependenciesDigest"] = "d" * 64
        reopened = self._reopen_ticketed_restore(host, replay)
        self.assertEqual(host.verified_apply_calls, 0)
        self.assertEqual(host.apply_calls, previous_apply)
        self.assertEqual(host.compile_calls, previous_compile)
        self.assertEqual(reopened["shaders"]["applied"], 0, reopened)
        self.assertEqual(reopened["shaders"]["skipped"], 1, reopened)
        self.assertEqual(reopened["shaders"]["items"][0]["reason"], "local-edit")
        self.assertIn("include changed", reopened["shaders"]["items"][0]["detail"])

    def test_stale_index_reinventories_and_replaces_only_after_success(self) -> None:
        self.state["knowledgeContentSha256"] = "4" * 64

        result, host, unused_replay, progress = self._run(auto_prepare=True)

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertIn("project-reuse-plan:invalid-or-stale-index", result["cache"]["reason"])
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.apply_calls, 1)
        self.assertTrue(result["index"]["written"])
        selected = host.ipc.index["bindings"][0]
        self.assertEqual(selected["knowledgeContentSha256"], self.knowledge_sha)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertIn("resource-match", phases)
        self.assertNotIn("prepare", phases)
        self.assertIn("index-write", phases)

    def test_inventory_publication_cas_conflict_preserves_old_rdc_receipts(
        self,
    ) -> None:
        original_index = copy.deepcopy(self.index)
        self.state["knowledgeContentSha256"] = "4" * 64

        result, host, unused_replay, progress = self._run(
            auto_prepare=True,
            inventory_register_error=RuntimeError(
                "shader inventory project binding is stale or changed"
            ),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertFalse(result["index"]["written"])
        self.assertEqual(result["realizedReceipts"]["persisted"], 0)
        self.assertEqual(host.ipc.index, original_index)
        self.assertNotIn(
            "write_rdc_project_index",
            [method for method, unused in host.ipc.calls],
        )
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_reuse_compile_failure_never_overwrites_absent_index(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            compile_error=RuntimeError("compiler unavailable"),
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["shaders"]["applied"], 0)
        self.assertEqual(result["shaders"]["failed"], 1)
        self.assertIn("compiler unavailable", result["shaders"]["items"][0]["error"])
        self.assertFalse(result["index"]["written"])
        self.assertIsNone(host.ipc.index)
        methods = [method for method, unused in host.ipc.calls]
        self.assertNotIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("shader-compile", phases)
        self.assertNotIn("index-write", phases)

    def test_project_pin_blocks_activation_during_compile_until_restore_finishes(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            switch_project_on_compile=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(host.compile_calls, 1)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.active_service.blocked_switches, 1)
        self.assertTrue(result["index"]["written"])
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_project_switch_during_reuse_apply_rolls_back_live_replacement(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            switch_project_on_apply=True,
        )

        self.assertFalse(result["completed"])
        self.assertIn("active project changed", result["error"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertIsNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_post_apply_rollback_failure_is_a_fatal_restore_error(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            switch_project_on_apply=True,
            shader_rollback_error=RuntimeError("rollback route unavailable"),
        )

        self.assertFalse(result["completed"])
        self.assertEqual(result["errorType"], "RestoreRollbackFailed")
        self.assertIn("rollback failed", result["error"])
        self.assertIn("rollback route unavailable", result["error"])
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertIsNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_inventory_restore_does_not_create_material_rollback_ledger(self) -> None:
        result, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
            switch_project_after_material_success=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 0)
        self.assertEqual(host.material_prepare_calls, 0)
        self.assertEqual(host.material_update_calls, 0)
        self.assertEqual(host.material_rollback_calls, 0)
        self.assertEqual(result["materials"]["skipped"], 0)
        self.assertEqual(result["materials"]["items"], [])
        self.assertNotIn("5" * 32, project_restore._MATERIAL_SYNC_PARAMETERS)
        self.assertIsNotNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_running_cancel_rolls_back_shader_without_material_ledger(self) -> None:
        stopped, host, unused_replay, progress = self._run(
            index_present=False,
            auto_prepare=True,
            with_material=True,
            cancel_on_progress_phase="index-write",
            return_control_error=True,
        )

        self.assertIsInstance(stopped, project_restore.RestoreCancelled)
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(host.material_update_calls, 0)
        self.assertEqual(host.material_rollback_calls, 0)
        self.assertEqual(host.active_service.pin_depth, 0)
        self.assertNotIn("5" * 32, project_restore._MATERIAL_SYNC_PARAMETERS)
        self.assertIsNone(host.ipc.index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_expired_server_deadline_stops_before_host_work(self) -> None:
        stopped, host, unused_replay, progress = self._run(
            deadline_monotonic=time.monotonic() - 1.0,
            return_control_error=True,
        )

        self.assertIsInstance(stopped, project_restore.RestoreDeadlineExceeded)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.ipc.calls, [])
        self.assertEqual(progress, [])

    def test_project_switch_during_cached_apply_rolls_back_and_never_prepares(self) -> None:
        original_index = copy.deepcopy(self.index)
        result, host, unused_replay, progress = self._run(
            switch_project_on_apply=True
        )

        self.assertFalse(result["completed"])
        self.assertIn("active project or capture changed", result["error"])
        self.assertEqual(host.apply_calls, 1)
        self.assertEqual(host.shader_rollback_calls, 1)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(host.ipc.index, original_index)
        self.assertEqual(progress[-1]["phase"], "failed")

    def test_cached_resource_index_rejects_more_than_256_without_ipc(self) -> None:
        shader_keys = []
        validated = []
        for number in range(257):
            resource_id = number + 1
            event_id = number + 10
            shader_key = "%d:fragment:main:%s" % (resource_id, self.raw_sha)
            shader_keys.append(shader_key)
            identity = {
                "resourceId": resource_id,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": self.raw_sha,
                "eventIds": [event_id],
            }
            status = copy.deepcopy(self.workspace_status)
            status["workspaceId"] = "%032x" % (number + 1)
            status["shader"]["resourceId"] = resource_id
            status["shader"]["shaderKey"] = shader_key
            validated.append(
                (
                    {"identity": identity},
                    self.workspace,
                    status,
                )
            )
        evidence = {
            "selection": {
                "mode": "all_draw_dispatch",
                "eventIds": list(range(10, 267)),
                "stages": ["fragment"],
            },
            "shaderKeys": shader_keys,
            "useTuples": [
                [number + 10, shader_keys[number]] for number in range(257)
            ],
        }
        host = _Host(
            self.active,
            self.workspace_status,
            self.capture_path,
            self.session_id,
            self.index,
            evidence,
        )

        with self.assertRaises(project_restore.CatalogPreparationRequired) as raised:
            project_restore._match_workspace_resources(
                host,
                self.capture_path,
                self.session_id,
                validated,
            )

        self.assertEqual(
            raised.exception.reason_id, "shader-resource-index-shader-limit"
        )
        self.assertEqual(host.scope_calls, 0)
        self.assertEqual(host.projection_calls, 0)
        self.assertEqual(host.ipc.calls, [])

    def test_failed_live_anchor_preserves_index_without_full_prepare(self) -> None:
        original_index = copy.deepcopy(self.index)

        def change_expected_anchor(result):
            result["assets"][0]["anchorEventId"] = 10

        result, host, replay, progress = self._run(
            expected_result_mutator=change_expected_anchor,
            inventory_asset_mutator=lambda asset: asset.update(
                {"anchorEventId": 10}
            )
        )

        self.assertTrue(result["completed"])
        self.assertFalse(result["cache"]["hit"])
        self.assertIn("cache-live-validation-failed", result["cache"]["reason"])
        self.assertEqual(result["shaders"]["attempted"], 0)
        self.assertEqual(host.apply_calls, 0)
        self.assertEqual(host.prepare_calls, 0)
        self.assertEqual(replay.snapshot_calls, 1)
        self.assertEqual(replay.inventory_snapshot_calls, 1)
        self.assertEqual(host.ipc.index, original_index)
        methods = [method for method, unused in host.ipc.calls]
        self.assertEqual(
            methods.count("begin_rdc_project_shader_resource_index"), 2
        )
        self.assertEqual(
            methods.count("commit_rdc_project_shader_resource_index"), 0
        )
        begins = [
            params
            for method, params in host.ipc.calls
            if method == "begin_rdc_project_shader_resource_index"
        ]
        self.assertFalse(begins[0]["inventory_all_shaders"])
        self.assertTrue(begins[1]["inventory_all_shaders"])
        self.assertNotIn("write_rdc_project_index", methods)
        phases = [event["phase"] for event in progress]
        self.assertIn("resource-inventory", phases)
        self.assertIn("resource-match", phases)
        self.assertNotIn("prepare", phases)
        self.assertEqual(phases[-1], "complete")

    def test_live_edit_apply_has_no_implicit_project_sync(self) -> None:
        applied = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "applied": True,
            "replacement": {"replacementId": "1" * 32},
        }
        with mock.patch(
            "src.shader_manual_edit.apply_manual_candidate",
            return_value=copy.deepcopy(applied),
        ):
            result = server._shader_live_edit_apply_candidate(
                "managed-workspace", 4, "a" * 64
            )

        self.assertNotIn("projectSync", result)

    def test_material_update_has_no_implicit_project_sync(self) -> None:
        updated = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "operation": "update",
            "sessionId": "1" * 32,
            "updatedParameterId": "2" * 32,
            "applied": True,
        }

        class Manager:
            def update(self, session_id, parameter, value):
                return copy.deepcopy(updated)

        with mock.patch.object(
            server, "_material_session_manager", return_value=Manager()
        ):
            result = json.loads(
                server.update_material_parameter("1" * 32, "2" * 32, 0.5)
            )

        self.assertNotIn("projectSync", result)

    def test_shader_project_sync_requires_explicit_registration_tool(self) -> None:
        synced = {
            "schema": "replaylab-project-shader-sync/v1",
            "schemaVersion": 1,
            "projectCommitted": True,
        }
        with mock.patch.object(
            project_restore,
            "sync_active_shader_workspace_after_apply",
            return_value=copy.deepcopy(synced),
        ) as sync:
            result = json.loads(
                server.register_active_project_shader_replacement(
                    "managed-workspace", 4, "a" * 64, "1" * 32
                )
            )

        self.assertEqual(result, synced)
        sync.assert_called_once_with(
            "managed-workspace",
            expected_revision=4,
            expected_candidate_raw_sha256="a" * 64,
            replacement_id="1" * 32,
            embed_capture_index=False,
        )

    def test_publication_identity_fills_capture_api_and_live_original_abi(self):
        legacy = copy.deepcopy(self.identity)
        legacy.pop("api")
        legacy.pop("stableKeySha256")
        legacy.pop("abiHash")
        legacy.pop("anchorEventId")
        legacy["eventIds"] = [8]
        legacy["stableKeyComplete"] = False
        legacy["encoding"] = "ShaderEncoding.DXBC"
        status = copy.deepcopy(self.workspace_status)
        status["capture"]["api"] = "graphicsapid3d11"
        identity = project_restore._publication_shader_identity(
            legacy, status, self._live_replacement_record())
        self.assertEqual(identity["api"], "d3d11")
        self.assertEqual(identity["encoding"], "dxbc")
        self.assertEqual(identity["abiHash"], self.live_abi_sha)
        self.assertTrue(identity["stableKeyComplete"])
        self.assertEqual(identity["anchorEventId"], 9)
        project_restore._realized_receipt_from_record(
            {**self.shader_record, "identity": identity}, self.live_abi_sha)
        for field, value in (("api", "vulkan"), ("encoding", "spirv"), ("abiHash", "2" * 64)):
            with self.subTest(field=field):
                changed = copy.deepcopy(legacy)
                changed[field] = value
                with self.assertRaisesRegex(RuntimeError, "live original Shader identity"):
                    project_restore._publication_shader_identity(changed, status)

    def test_live_same_native_pair_accepts_new_source_revision_without_relabeling_physical_receipt(self):
        host = _Host(self.active, self.workspace_status, self.capture_path, self.session_id,
                     self.index, self.identity_evidence)
        physical = self._live_replacement_record(revision=2)
        host.ipc.shared_replacements = [physical]
        matched = project_restore._live_replacement(
            host, self.workspace_status, self.session_id, physical["replacementId"], 3, self.candidate_sha)
        self.assertEqual(matched["revision"], 2)
        for field in ("source", "candidate", "gate", "future", "physical-original", "physical-stage"):
            with self.subTest(field=field):
                status, replacement = copy.deepcopy(self.workspace_status), copy.deepcopy(physical)
                if field == "source":
                    status["source"]["currentSha256"] = "1" * 64
                elif field == "candidate":
                    status["latestCompiled"]["rawSha256"] = "1" * 64
                elif field == "gate":
                    status["latestCompiled"]["abiGate"]["status"] = "pending"
                elif field == "future":
                    replacement["revision"] = 4
                elif field == "physical-original":
                    replacement["originalRawSha256"] = "1" * 64
                else:
                    replacement["stage"] = "vertex"
                host.ipc.shared_replacements = [replacement]
                with self.assertRaisesRegex(RuntimeError, "exact applied workspace candidate"):
                    project_restore._live_replacement(
                        host, status, self.session_id, physical["replacementId"], 3, self.candidate_sha)

    def test_final_shader_association_preserves_rdc_bytes_and_old_physical_signed_provenance(self):
        result, host, replay, unused = self._run(host_mutator=self._ticketed_restore_host)
        self.assertTrue(result["completed"], result)
        old_status = copy.deepcopy(host.workspace_service._status)
        replacement = copy.deepcopy(host.last_apply_response["replacement"])
        lease = {"abiLeaseRequestId": replacement["abiRequestId"],
                 "abiLeaseReportSha256": replacement["abiReportSha256"],
                 "abiLeaseCandidateRawSha256": replacement["candidateRawSha256"],
                 "abiLeaseRevision": replacement["revision"]}
        self.assertTrue(project_restore._persist_workspace_shader_pair_validation(old_status, replacement, lease))
        status = host.workspace_service._status
        status["revision"] += 1
        status["latestCompiled"]["revision"] = status["revision"]
        status["source"]["currentSha256"] = "4" * 64
        status["latestCompiled"]["sourceSha256"] = "4" * 64
        status["latestCompiled"].update(
            replayAbiValidated=True, abiCompatible=True, requiresReplayAbiValidation=False,
            validationScope="replay-reflection-abi-compatible")
        knowledge = copy.deepcopy(replay.state["workspaces"][self.knowledge_id])
        knowledge.update(sourceSha256="4" * 64, targetProfile=copy.deepcopy(status["targetProfile"]),
                         compiledCandidate={"rawSha256": self.candidate_sha},
                         originCatalogSha256=replay.state["catalogSha256"])
        # The recovered catalog's physical JSON hash is deliberately different
        # from its normalized, content-bound project revision.
        status["capture"]["catalogSha256"] = "e" * 64
        replay.state["workspaces"][self.knowledge_id] = knowledge
        catalog_shader = replay.state["capture"]["shaders"][0]
        catalog_shader.pop("api")
        catalog_shader.pop("stableKeySha256")
        catalog_shader.pop("abiHash")
        catalog_shader.pop("anchorEventId")
        catalog_shader["eventIds"] = [8]
        # Actual old catalogs mix encoding with interfaceComplete while the
        # API is capture-level and interfaceHash is not the physical ABI hash.
        catalog_shader.pop("stableKeyComplete")
        catalog_shader["interfaceComplete"] = True
        catalog_shader["interfaceHash"] = "a" * 64
        catalog_shader["encoding"] = "ShaderEncoding.DXBC"
        host.ipc.shared_replacements = [replacement]
        host.ipc.calls.clear()
        host._require_managed_active_workspace = lambda unused_active, path: Path(path)
        before_rdc = self.capture_path.read_bytes()
        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
                project_restore, "ReplayProjectService", return_value=replay), mock.patch.object(
                project_restore, "_register_applied_workspace_with_readback", return_value={"knowledge": knowledge}), mock.patch.object(
                replay, "restore_knowledge_snapshot", wraps=replay.restore_knowledge_snapshot) as snapshot:
            synced = project_restore.sync_active_shader_workspace_after_apply(
                self.workspace, status["revision"], self.candidate_sha, replacement["replacementId"])
        self.assertTrue(snapshot.called)
        self.assertTrue(all(call.args == (status["capture"]["captureId"], knowledge["originCatalogSha256"])
                            for call in snapshot.call_args_list))
        self.assertTrue(synced["projectCommitted"])
        self.assertTrue(synced["derivedAssociationWritten"], synced)
        self.assertTrue(synced["workspacePairValidationWritten"], synced)
        self.assertFalse(synced["rdcIndexWritten"])
        self.assertEqual(self.capture_path.read_bytes(), before_rdc)
        self.assertEqual([method for method, unused in host.ipc.calls],
                         ["get_shared_shader_replacements", "get_shared_shader_replacements"])
        self.assertTrue(all(params.get("include_validation_receipts") is True
                            for method, params in host.ipc.calls))
        association = json.loads(Path(synced["derivedAssociationPath"]).read_text(encoding="utf-8"))
        self.assertEqual(association["currentSourceRevision"], status["revision"])
        self.assertEqual(association["physicalCandidateRevision"], replacement["revision"])
        self.assertEqual(association["binding"]["shaders"][0]["identity"]["anchorEventId"], 9)
        self.assertEqual(project_restore._capture_shader(
            replay.state, association["binding"]["shaders"][0]["identity"]), catalog_shader)
        self.assertFalse(association["physicalApplyAuthority"])
        cached = project_restore._load_workspace_shader_pair_validation(status)
        self.assertEqual(cached["sourceSha256"], "4" * 64)
        self.assertEqual(cached["replacement"], replacement)
        changed = copy.deepcopy(status)
        changed["targetProfile"] = {"profile": "ps_4_0"}
        self.assertFalse(project_restore._persist_workspace_shader_pair_validation(
            changed, replacement, lease, allow_same_pair_revision_reuse=True))
        self.assertIsNone(project_restore._load_workspace_shader_pair_validation(changed))
        # The deployed legacy inventory omitted report/request and proof.
        # It still proves the current target effect, but cannot create a new
        # signed pair cache or report that an optional proof was persisted.
        thin = copy.deepcopy(replacement)
        for key in ("restoreValidationReceipt", "abiRequestId", "abiReportSha256",
                    "abiLeaseRequestId", "abiLeaseReportSha256", "abiLeaseCandidateRawSha256", "abiLeaseRevision"):
            thin.pop(key, None)
        host.ipc.shared_replacements = [thin]
        cached_before = project_restore._workspace_shader_pair_validation_path(status).read_bytes()
        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
                project_restore, "ReplayProjectService", return_value=replay), mock.patch.object(
                project_restore, "_register_applied_workspace_with_readback", return_value={"knowledge": knowledge}):
            no_proof = project_restore.sync_active_shader_workspace_after_apply(
                self.workspace, status["revision"], self.candidate_sha, replacement["replacementId"])
        self.assertTrue(no_proof["projectCommitted"])
        self.assertFalse(no_proof["workspacePairValidationWritten"])
        self.assertEqual(project_restore._workspace_shader_pair_validation_path(status).read_bytes(), cached_before)
        host.ipc.shared_replacements = [replacement]
        with mock.patch.object(project_restore, "_host_module", return_value=host), mock.patch.object(
                project_restore, "ReplayProjectService", return_value=replay), mock.patch.object(
                project_restore, "_register_applied_workspace_with_readback", return_value={"knowledge": knowledge}), mock.patch.object(
                project_restore, "_sync_index_binding", return_value=(True, "")) as embed:
            explicit = project_restore.sync_active_shader_workspace_after_apply(
                self.workspace, status["revision"], self.candidate_sha, replacement["replacementId"], embed_capture_index=True)
        self.assertTrue(explicit["rdcIndexWritten"])
        embed.assert_called_once()

    def test_material_project_sync_requires_explicit_registration_tool(self) -> None:
        synced = {
            "schema": "replaylab-project-material-sync/v1",
            "schemaVersion": 1,
            "projectCommitted": True,
        }
        with mock.patch.object(
            project_restore,
            "sync_active_material_session",
            return_value=copy.deepcopy(synced),
        ) as sync:
            result = json.loads(
                server.register_active_project_material_session(
                    "1" * 32, ["2" * 32]
                )
            )

        self.assertEqual(result, synced)
        sync.assert_called_once_with("1" * 32, ["2" * 32])

    def test_project_material_restore_uses_one_validated_batch_update(self) -> None:
        saved_a = "1" * 32
        saved_b = "2" * 32
        live_a = "3" * 32
        live_b = "4" * 32
        session_id = "5" * 32
        knowledge_id = "6" * 32
        layout_sha = "7" * 64
        knowledge = {
            "knowledgeId": knowledge_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": self.raw_sha,
            "codeSha256": "",
            "interfaceComplete": True,
            "interfaceHash": self.interface_sha,
            "layoutHash": self.layout_sha,
            "constantBlockPath": "PerMaterial",
            "constantBlockLayoutSha256": layout_sha,
            "parameters": [
                {
                    "parameterId": saved_a,
                    "path": "PerMaterial.Roughness",
                    "baseType": "Float",
                    "components": 1,
                    "value": 0.25,
                },
                {
                    "parameterId": saved_b,
                    "path": "PerMaterial.Tint",
                    "baseType": "Float",
                    "components": 3,
                    "value": [0.1, 0.2, 0.3],
                },
            ],
        }
        identity = copy.deepcopy(self.identity)
        binding_state = {
            "projectId": self.project_id,
            "captureId": self.capture_id,
            "materials": {knowledge_id: knowledge},
        }
        material_record = {
            "knowledgeId": knowledge_id,
            "shaderResourceId": 7,
            "stage": "fragment",
            "path": "PerMaterial",
            "layoutSha256": layout_sha,
            "eventId": 9,
            "blockIndex": 0,
            "parameters": [
                {"parameterId": saved_a},
                {"parameterId": saved_b},
            ],
        }
        session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "sessionId": session_id,
            "captureId": self.capture_id,
            "captureSessionId": self.session_id,
            "eventId": 9,
            "stage": "fragment",
            "shaderResourceId": 7,
            "constantBlockIndex": 0,
            "constantBlockPath": "PerMaterial",
            "layoutSha256": layout_sha,
            "parameters": [
                {
                    "parameterId": live_a,
                    "path": "PerMaterial.Roughness",
                    "baseType": "Float",
                    "components": 1,
                },
                {
                    "parameterId": live_b,
                    "path": "PerMaterial.Tint",
                    "baseType": "Float",
                    "components": 3,
                },
            ],
        }

        class Host:
            def __init__(self):
                self.batch_calls = []
                self.rollback_calls = 0

            @staticmethod
            def prepare_material_session(*unused_args):
                return json.dumps(session)

            def update_material_parameters(self, requested_session, updates):
                self.batch_calls.append((requested_session, copy.deepcopy(updates)))
                response = copy.deepcopy(session)
                response.update(
                    {
                        "operation": "update-many",
                        "updatedParameterIds": [live_a, live_b],
                        "updatedParameterCount": 2,
                        "applied": True,
                    }
                )
                return json.dumps(response)

            @staticmethod
            def update_material_parameter(*unused_args):
                raise AssertionError("project restore must not update leaves individually")

            def rollback_material_session(self, unused_session_id):
                self.rollback_calls += 1
                return json.dumps(
                    {
                        "schema": "renderdoc-ultra-material-session/v1",
                        "schemaVersion": 1,
                    }
                )

        host = Host()
        restored, unused_index = project_restore._restore_material(
            host,
            None,
            binding_state,
            self.active_workspace_id,
            identity,
            material_record,
            self.session_id,
        )

        self.assertEqual(restored["parameterCount"], 2)
        self.assertEqual(
            host.batch_calls,
            [
                (
                    session_id,
                    [
                        {"parameter": live_a, "value": 0.25},
                        {"parameter": live_b, "value": [0.1, 0.2, 0.3]},
                    ],
                )
            ],
        )
        self.assertEqual(host.rollback_calls, 0)

    def test_material_sync_accumulates_live_session_leaves_and_rdc_hint(self) -> None:
        parameter_a = "a" * 32
        parameter_b = "b" * 32
        material_session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "captureSessionId": self.session_id,
            "eventId": 9,
            "stage": "fragment",
            "shaderResourceId": 7,
            "constantBlockIndex": 0,
            "constantBlockPath": "cb0",
            "layoutSha256": self.layout_sha,
            "parameters": [
                {
                    "parameterId": parameter_a,
                    "path": "cb0.A",
                    "baseType": "Float",
                    "components": 1,
                    "value": 1.0,
                },
                {
                    "parameterId": parameter_b,
                    "path": "cb0.B",
                    "baseType": "Float",
                    "components": 1,
                    "value": 2.0,
                },
            ],
        }

        class Active:
            def __init__(self, owner):
                self.owner = owner
                self.saved = []
                self.durable_ids = set()

            def status(self):
                return copy.deepcopy(owner.active)

            def pin_active_project(self, unused_workspace_id, unused_project_root):
                return contextlib.nullcontext()

            def save_material_knowledge(self, session, parameter_ids):
                self.saved.append(list(parameter_ids))
                self.durable_ids.update(parameter_ids)
                return {
                    "knowledge": {
                        "knowledgeId": "%032x" % len(self.saved),
                        "parameters": [
                            copy.deepcopy(parameter)
                            for parameter in session["parameters"]
                            if parameter["parameterId"] in self.durable_ids
                        ],
                    }
                }

        owner = self
        active_service = Active(self)

        class Host:
            def get_material_session_status(self, session_id):
                self.last_session_id = session_id
                return json.dumps(copy.deepcopy(material_session))

            def _active_project_service(self):
                return active_service

            def _require_same_active_project(self, active):
                return copy.deepcopy(active)

            def _active_live_context(self, allow_missing_capture_id=False):
                return {
                    "capture": {
                        "path": str(owner.capture_path),
                        "captureSessionId": owner.session_id,
                    }
                }

        class Replay:
            def restore_knowledge_snapshot(self, capture_id, catalog_sha256):
                self.args = (capture_id, catalog_sha256)
                return {"projectId": owner.project_id}

        material_records = []

        def sync_index(
            unused_host,
            unused_active,
            unused_replay,
            unused_binding_state,
            unused_capture_path,
            unused_capture_session_id,
            **records,
        ):
            material_records.append(copy.deepcopy(records["material_record"]))
            return True, ""

        with mock.patch.object(project_restore, "_host_module", return_value=Host()), mock.patch.object(
            project_restore, "ReplayProjectService", return_value=Replay()
        ), mock.patch.object(project_restore, "_sync_index_binding", side_effect=sync_index):
            first = project_restore.sync_active_material_session(
                self.session_id, [parameter_a]
            )
            # Host restart/eviction removes only the optimisation. The durable
            # project head returned by save remains complete and authoritative.
            project_restore._MATERIAL_SYNC_PARAMETERS.clear()
            second = project_restore.sync_active_material_session(
                self.session_id, [parameter_b]
            )

        self.assertEqual(first["parameterIds"], [parameter_a])
        self.assertEqual(second["parameterIds"], [parameter_a, parameter_b])
        self.assertEqual(
            active_service.saved,
            [[parameter_a], [parameter_b]],
        )
        self.assertEqual(
            material_records[-1]["parameters"],
            [{"parameterId": parameter_a}, {"parameterId": parameter_b}],
        )


if __name__ == "__main__":
    unittest.main()


class IncompleteInventoryPreparedSourceTests(unittest.TestCase):
    def fixture(self):
        profile = {"containerFormat": "spirv", "apiEnvironment": "vulkan", "stage": "fragment"}
        status = {
            "source": {"present": True, "lane": "executable-editable", "format": "glsl",
                       "origin": "decompiled", "currentSha256": "a" * 64},
            "baseline": {"rawSha256": "b" * 64, "abiSha256": "c" * 64, "kind": "SPIRV"},
            "shader": {"rawSha256": "b" * 64}, "capture": {"api": "Vulkan"},
            "stage": "fragment", "entryPoint": "main", "targetProfile": profile,
        }
        knowledge = {"sourceFormat": "glsl", "sourceSha256": "a" * 64,
                     "rawSha256": "b" * 64, "baselineAbiSha256": "c" * 64,
                     "stage": "fragment", "entryPoint": "main", "targetProfile": profile}
        identity = {"rawSha256": "b" * 64, "abiHash": "d" * 64, "encoding": "spirv",
                    "api": "vulkan", "stage": "fragment", "entryPoint": "main",
                    "stableKeyComplete": False}
        state = {"capture": {"inventoryOnly": True}}
        return status, knowledge, identity, state

    def test_module_summary_is_not_physical_abi_for_source_preparation(self):
        values = self.fixture()
        project_restore._validate_prepared_project_source(*values, source_preparation_only=True)
        # No current apply/ABI permission is invented by this source-only check.
        self.assertNotIn("replayAbiValidated", values[0])
        with self.assertRaisesRegex(RuntimeError, "baseline.abiSha256"):
            project_restore._validate_prepared_project_source(*values)

    def test_incomplete_inventory_never_waives_target_or_current_baseline_format(self):
        cases = [
            (0, ("baseline", "rawSha256"), "e" * 64),
            (0, ("shader", "rawSha256"), "e" * 64),
            (1, ("rawSha256",), "e" * 64),
            (0, ("baseline", "abiSha256"), "invalid"),
            (0, ("capture", "api"), "OpenGL"),
            (0, ("stage",), "vertex"),
            (1, ("entryPoint",), "other"),
            (0, ("source", "format"), "hlsl"),
            (0, ("baseline", "kind"), "DXBC"),
            (0, ("targetProfile",), {"profile": "other"}),
            (2, ("stableKeyComplete",), True),
            (3, ("capture", "inventoryOnly"), False),
        ]
        for index, path, replacement in cases:
            with self.subTest(index=index, path=path, replacement=replacement):
                values = self.fixture()
                target = values[index]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = replacement
                with self.assertRaises((RuntimeError, ValueError)):
                    project_restore._validate_prepared_project_source(
                        *values, source_preparation_only=True)

    def test_source_preparation_uses_current_baseline_after_reflection_upgrade(self):
        values = self.fixture()
        values[1]["baselineAbiSha256"] = "e" * 64
        before = copy.deepcopy(values)
        project_restore._validate_prepared_project_source(*values, source_preparation_only=True)
        self.assertEqual(values, before)
        with self.assertRaisesRegex(RuntimeError, "baseline.abiSha256"):
            project_restore._validate_prepared_project_source(*values)

    def test_complete_physical_inventory_keeps_exact_abi_requirement(self):
        values = self.fixture()
        values[2].update(stableKeyComplete=True, abiHash="c" * 64)
        project_restore._validate_prepared_project_source(*values, source_preparation_only=True)
        values[2]["abiHash"] = "e" * 64
        with self.assertRaisesRegex(RuntimeError, "baseline.abiSha256"):
            project_restore._validate_prepared_project_source(*values, source_preparation_only=True)
