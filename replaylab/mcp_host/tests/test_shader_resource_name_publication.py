"""Current-RDC texture names preserve project aliases and user ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src import server
from src.shader_workspace.resource_name_publication import (
    LEDGER_SCHEMA,
    merge_alias_manifest,
    publication_paths,
    texture_physical_target,
    texture_target,
    write_ledger,
    write_manifest,
)


CAPTURE_ID = "a" * 64
CAPTURE_SESSION_ID = "b" * 32
RAW_SHA = "c" * 64


def _shader_alias() -> dict:
    return {
        "verified": True,
        "metadata": {"programVerified": True},
        "provenance": "program",
        "captureId": CAPTURE_ID,
        "shaderResourceId": 11,
        "stage": "fragment",
        "entryPoint": "MainPS",
        "rawSha256": RAW_SHA,
        "bindingKind": "shader",
        "alias": "ExistingProjectShader",
    }


def _texture_alias(name: str) -> dict:
    return {
        "verified": True,
        "metadata": {
            "programVerified": True,
            "resourceBindingVerified": True,
            "eventId": 11384,
            "semanticEvidenceOwner": "ai",
            "semanticEvidenceKind": "pixel-same-image",
            "assetPath": "/Game/T_Body_Base",
        },
        "provenance": "ai-content-match",
        "captureId": CAPTURE_ID,
        "shaderResourceId": 11,
        "resourceId": 77,
        "stage": "fragment",
        "entryPoint": "MainPS",
        "rawSha256": RAW_SHA,
        "bindingKind": "textureResource",
        "bindNumber": 4,
        "registerSpace": 1,
        "alias": name,
    }


def test_merge_replaces_same_physical_texture_across_eid_and_bind():
    old_texture = _texture_alias("OldManagedName")
    old_texture["metadata"]["eventId"] = 11200
    old_texture["bindNumber"] = 9
    old_texture["registerSpace"] = 3
    new_texture = _texture_alias("T_Body_BaseMap")
    merged, replaced = merge_alias_manifest(
        CAPTURE_ID, [_shader_alias(), old_texture], [new_texture]
    )
    assert merged["aliases"] == [_shader_alias(), new_texture]
    assert texture_target(old_texture) != texture_target(new_texture)
    assert replaced == {texture_physical_target(new_texture)}


class _FastSlot:
    def __init__(self, unused_name, unused_arguments=None):
        self.busy = None

    def update(self, unused_phase, **unused_progress):
        return True

    def close(self, *, ok):
        return {"ok": ok, "elapsedSeconds": 0.0, "endMemory": {}}


def _authorization() -> dict:
    return {
        "workspaceId": "f" * 32,
        "bindingId": "1" * 32,
        "captureId": CAPTURE_ID,
        "captureSessionId": CAPTURE_SESSION_ID,
        "eventId": 11384,
        "stage": "fragment",
        "entryPoint": "MainPS",
        "shaderResourceId": 11,
        "shaderRawSha256": RAW_SHA,
        "resolutionId": 7,
        "assetId": "asset-a",
        "assetContentSha256": "d" * 64,
        "readerProofSha256": "e" * 64,
    }


def _row() -> dict:
    return {
        "selector": "resource:_32",
        "resourceId": 77,
        "bindNumber": 4,
        "registerSpace": 1,
        "alias": "T_Body_BaseMap",
        "assetPath": "/Game/T_Body_Base",
        "hostEvidenceKind": "pixel-same-image",
    }


@pytest.fixture(autouse=True)
def _bind_current_texture_target(monkeypatch):
    """Keep transaction tests focused on commit/recovery, not live replay IPC."""

    monkeypatch.setattr(
        server,
        "_alias_publication_workspace_status",
        lambda value: {
            "workspacePath": str(Path(value).resolve()),
            "workspaceId": "f" * 32,
        },
    )
    monkeypatch.setattr(
        server,
        "_current_resource_alias_publication_binding",
        lambda unused_root, rows, **unused: (list(rows), _authorization()),
    )


def test_publication_merges_old_manifest_before_rollback(tmp_path, monkeypatch):
    old_manifest = {
        "schema": "renderdoc-ultra-aliases/v1",
        "schemaVersion": 1,
        "captureId": CAPTURE_ID,
        "aliases": [_shader_alias(), _texture_alias("OldManagedName")],
    }
    old_path = tmp_path / "old-aliases.json"
    old_raw = json.dumps(old_manifest, separators=(",", ":")).encode()
    old_path.write_bytes(old_raw)
    old_sha = hashlib.sha256(old_raw).hexdigest()
    state = {"transaction": "2" * 32, "manifest": old_path, "sha": old_sha}
    calls: list[str] = []

    def ipc_call(method, params, **unused):
        calls.append(method)
        if method == "get_symbol_alias_transaction":
            present = bool(state["transaction"])
            return {
                "schema": "renderdoc-ultra-alias-transaction-status/v1",
                "schemaVersion": 1,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "transactionId": state["transaction"],
                "state": "committed" if present else "rolled-back",
                "committed": present,
                "mutationPresent": present,
                "recoveryRequired": present,
                "rollbackAvailable": present,
                "manifestSha256": state["sha"] if present else "",
                "manifestPath": str(state["manifest"]) if present else "",
                "replayMutation": False,
            }
        if method == "rollback_symbol_aliases":
            assert params["expected_transaction_id"] == "2" * 32
            state["transaction"] = ""
            return {
                "schema": "renderdoc-ultra-alias-rollback/v1",
                "schemaVersion": 1,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "transactionId": "2" * 32,
                "status": "restored",
                "restored": True,
                "verified": True,
                "recoveryRequired": False,
                "failures": [],
            }
        if method == "load_symbol_aliases":
            merged = json.loads(Path(params["path"]).read_text(encoding="utf-8"))
            assert _shader_alias() in merged["aliases"]
            assert _texture_alias("OldManagedName") not in merged["aliases"]
            assert _texture_alias("T_Body_BaseMap") in merged["aliases"]
            count = len(merged["aliases"])
            state.update(
                transaction=params["operation_id"],
                manifest=Path(params["path"]),
                sha=params["expected_sha256"],
            )
            return {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "path": params["path"],
                "confirmedOnly": True,
                "strictAtomic": True,
                "committed": True,
                "recoveryRequired": False,
                "rollbackAvailable": True,
                "transactionId": params["operation_id"],
                "operationId": params["operation_id"],
                "counts": {
                    "manifestAliases": count,
                    "deterministicAliases": count,
                    "liveBoundAliases": count,
                    "activeValidatedAliases": count,
                    "renamedShaders": 1,
                    "renamedTextures": 1,
                    "renamedResources": 2,
                    "keptUser": 0,
                    "skipped": 0,
                    "conflicts": 0,
                },
                "written": [
                    {"resourceId": 11, "displayName": "ExistingProjectShader",
                     "bindingKind": "shader"},
                    {"resourceId": 77, "displayName": "T_Body_BaseMap",
                     "bindingKind": "textureresource"},
                ],
                "replacedManaged": [],
                "keptUser": [],
                "skipped": [],
                "conflicts": [],
            }
        raise AssertionError(method)

    manager = SimpleNamespace(
        verify_resource_alias_authorization=lambda unused_ws, rows, unused_auth: (
            list(rows), _authorization()
        )
    )
    service = SimpleNamespace(status=lambda unused: {"workspacePath": str(tmp_path)})
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "LongToolSlot", _FastSlot)
    monkeypatch.setattr(server, "update_long_tool", lambda *a, **k: None)

    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path), json.dumps([_row()]),
        operation_id="resource-publication-test", event_id=11384, wait_seconds=5,
    )))
    assert result["complete"] is True
    assert result["counts"]["replacedManaged"] == 1
    assert result["rows"][0]["outcome"] == "replacedManaged"
    assert calls.index("rollback_symbol_aliases") < calls.index("load_symbol_aliases")
    ledger = json.loads(Path(result["operationLedgerPath"]).read_text(encoding="utf-8"))
    assert ledger["phase"] == "committed"
    assert ledger["callerRequestSha256"]
    assert ledger["workspaceRoot"] == str(tmp_path.resolve())

    unavailable_manager = mock.Mock()
    unavailable_manager.verify_resource_alias_authorization.side_effect = RuntimeError(
        "live resource authorization is no longer available"
    )
    monkeypatch.setattr(
        server, "_shader_organization_manager", lambda: unavailable_manager
    )
    monkeypatch.setattr(
        server,
        "ipc",
        SimpleNamespace(call=mock.Mock(side_effect=AssertionError("no replay IPC"))),
    )
    replay = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path), json.dumps([_row()]),
        operation_id="resource-publication-test", event_id=11384, wait_seconds=0,
    )))
    unavailable_manager.verify_resource_alias_authorization.assert_not_called()
    assert replay["idempotentReplay"] is True
    assert replay["operationId"] == "resource-publication-test"
    assert replay["requestSha256"] == ledger["requestSha256"]


@pytest.mark.skipif(os.name != "nt", reason="extended paths are Windows-only")
@pytest.mark.parametrize("first_extended", [False, True])
def test_resource_server_canonical_fast_replay_is_bidirectional(
    tmp_path, monkeypatch, first_extended
):
    normal = str(tmp_path.resolve())
    extended = "\\\\?\\" + normal
    first = extended if first_extended else normal
    second = normal if first_extended else extended
    operation_id = (
        "resource-canonical-extended-first"
        if first_extended
        else "resource-canonical-normal-first"
    )
    rows = [_row()]
    caller_request = {
        "tool": "publish_shader_resource_names",
        "operationId": operation_id,
        "workspacePath": first,
        "eventId": 11384,
        "stage": server.normalize_stage("fragment"),
        "rows": rows,
    }
    caller_sha, unused_compatible = server.workspace_bound_request_hashes(
        caller_request, Path(first).resolve()
    )
    unused_manifest, ledger_path = publication_paths(tmp_path, operation_id)
    write_ledger(ledger_path, {
        "schema": LEDGER_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "requestSha256": "9" * 64,
        "callerRequestSha256": caller_sha,
        "workspaceRoot": first,
        "phase": "committed",
        "result": {
            "schema": "replaylab-resource-name-publication/v1",
            "schemaVersion": 1,
            "operationId": operation_id,
            "requestSha256": "9" * 64,
            "complete": True,
            "applied": True,
        },
    })

    service = SimpleNamespace(status=lambda value: {"workspacePath": value})
    unavailable = mock.Mock()
    unavailable.verify_resource_alias_authorization.side_effect = AssertionError(
        "canonical fast replay rebuilt live authorization"
    )
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: unavailable)
    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        second,
        json.dumps(rows),
        operation_id=operation_id,
        event_id=11384,
        wait_seconds=0,
    )))
    assert "error" not in result, result
    assert result["idempotentReplay"] is True
    unavailable.verify_resource_alias_authorization.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="extended paths are Windows-only")
def test_resource_server_accepts_legacy_normal_hash_from_extended_path(
    tmp_path, monkeypatch
):
    normal = str(tmp_path.resolve())
    extended = "\\\\?\\" + normal
    operation_id = "resource-legacy-normal-caller-hash"
    rows = [_row()]
    legacy_request = {
        "tool": "publish_shader_resource_names",
        "operationId": operation_id,
        "workspacePath": normal,
        "eventId": 11384,
        "stage": server.normalize_stage("fragment"),
        "rows": rows,
    }
    legacy_sha = server._canonical_json_sha256(
        legacy_request, "legacy resource caller request"
    )
    unused_manifest, ledger_path = publication_paths(tmp_path, operation_id)
    write_ledger(ledger_path, {
        "schema": LEDGER_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "requestSha256": "8" * 64,
        "callerRequestSha256": legacy_sha,
        "workspaceRoot": normal,
        "phase": "committed",
        "result": {
            "operationId": operation_id,
            "requestSha256": "8" * 64,
            "complete": True,
        },
    })
    service = SimpleNamespace(status=lambda value: {"workspacePath": value})
    unavailable = mock.Mock()
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: unavailable)
    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        extended,
        json.dumps(rows),
        operation_id=operation_id,
        event_id=11384,
        wait_seconds=0,
    )))
    assert "error" not in result, result
    assert result["idempotentReplay"] is True
    unavailable.verify_resource_alias_authorization.assert_not_called()


def test_resource_unknown_ledger_phase_fails_closed_and_preserves_evidence(
    tmp_path, monkeypatch
):
    operation_id = "resource-unknown-ledger-phase"
    rows = [_row()]
    caller_request = {
        "tool": "publish_shader_resource_names",
        "operationId": operation_id,
        "workspacePath": str(tmp_path.resolve()),
        "eventId": 11384,
        "stage": server.normalize_stage("fragment"),
        "rows": rows,
    }
    caller_sha, unused_compatible = server.workspace_bound_request_hashes(
        caller_request, tmp_path
    )
    unused_manifest, ledger_path = publication_paths(tmp_path, operation_id)
    write_ledger(ledger_path, {
        "schema": LEDGER_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "requestSha256": "7" * 64,
        "callerRequestSha256": caller_sha,
        "workspaceRoot": str(tmp_path.resolve()),
        "phase": "future-version-phase",
        "result": None,
    })
    original = ledger_path.read_bytes()
    service = SimpleNamespace(status=lambda value: {"workspacePath": value})
    unavailable = mock.Mock()
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: unavailable)
    monkeypatch.setattr(
        server,
        "ipc",
        SimpleNamespace(call=mock.Mock(side_effect=AssertionError("no bridge call"))),
    )
    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path),
        json.dumps(rows),
        operation_id=operation_id,
        event_id=11384,
        wait_seconds=0,
    )))
    assert "unrecognized phase" in result["error"]
    assert ledger_path.read_bytes() == original
    unavailable.verify_resource_alias_authorization.assert_not_called()


def test_resource_recovery_required_rejects_uncertain_live_state(
    tmp_path, monkeypatch
):
    operation_id = "resource-ambiguous-recovery"
    rows = [_row()]
    authorization = _authorization()
    aliases = server._resource_alias_manifest_rows(rows, authorization)
    merged, replaced = merge_alias_manifest(CAPTURE_ID, [], aliases)
    manifest_path, ledger_path = publication_paths(tmp_path, operation_id)
    manifest_sha = write_manifest(manifest_path, merged)
    bridge_operation_id = hashlib.sha256(
        ("resource-name-publication\0" + operation_id).encode("utf-8")
    ).hexdigest()[:32]
    request_sha = server._canonical_json_sha256({
        "tool": "publish_shader_resource_names",
        "workspaceId": authorization["workspaceId"],
        "bindingId": authorization["bindingId"],
        "rows": rows,
    }, "resource name publication")
    caller_request = {
        "tool": "publish_shader_resource_names",
        "operationId": operation_id,
        "workspacePath": str(tmp_path.resolve()),
        "eventId": 11384,
        "stage": server.normalize_stage("fragment"),
        "rows": rows,
    }
    caller_sha, unused_compatible = server.workspace_bound_request_hashes(
        caller_request, tmp_path
    )
    write_ledger(ledger_path, {
        "schema": LEDGER_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "bridgeOperationId": bridge_operation_id,
        "requestSha256": request_sha,
        "callerRequestSha256": caller_sha,
        "workspaceRoot": str(tmp_path.resolve()),
        "bindingId": authorization["bindingId"],
        "captureId": CAPTURE_ID,
        "captureSessionId": CAPTURE_SESSION_ID,
        "manifestPath": str(manifest_path),
        "manifestSha256": manifest_sha,
        "oldTransactionId": "",
        "oldManifestPath": "",
        "oldManifestSha256": "",
        "replacedTargets": [list(item) for item in sorted(replaced)],
        "phase": "recovery-required",
        "result": None,
        "recoveryError": "previous recovery was interrupted",
    })
    original = ledger_path.read_bytes()
    calls: list[str] = []

    def ipc_call(method, unused_params, **unused):
        calls.append(method)
        assert method == "get_symbol_alias_transaction"
        return {
            "schema": "renderdoc-ultra-alias-transaction-status/v1",
            "schemaVersion": 1,
            "captureId": CAPTURE_ID,
            "captureSessionId": CAPTURE_SESSION_ID,
            "transactionId": "6" * 32,
            "state": "committed",
            "committed": True,
            "mutationPresent": True,
            "recoveryRequired": True,
            "rollbackAvailable": True,
            "manifestSha256": "5" * 64,
            "manifestPath": str(tmp_path / "unrelated-aliases.json"),
            "replayMutation": False,
        }

    manager = SimpleNamespace(
        verify_resource_alias_authorization=lambda unused_ws, value, unused_auth: (
            list(value), authorization
        )
    )
    service = SimpleNamespace(status=lambda value: {"workspacePath": value})
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "LongToolSlot", _FastSlot)
    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path),
        json.dumps(rows),
        operation_id=operation_id,
        event_id=11384,
        wait_seconds=5,
    )))
    assert result["ok"] is False
    assert "recovery is still ambiguous" in result["error"]
    assert ledger_path.read_bytes() == original
    assert calls == ["get_symbol_alias_transaction"]


def test_no_mutation_user_name_receipt_is_complete(tmp_path, monkeypatch):
    def ipc_call(method, params, **unused):
        if method == "get_symbol_alias_transaction":
            return {
                "schema": "renderdoc-ultra-alias-transaction-status/v1",
                "schemaVersion": 1,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "transactionId": "",
                "state": "absent",
                "committed": False,
                "mutationPresent": False,
                "recoveryRequired": False,
                "rollbackAvailable": False,
                "manifestSha256": "",
                "manifestPath": "",
                "replayMutation": False,
            }
        if method == "load_symbol_aliases":
            return {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "path": params["path"],
                "confirmedOnly": True,
                "strictAtomic": True,
                "committed": True,
                "mutationPresent": False,
                "recoveryRequired": False,
                "rollbackAvailable": False,
                "transactionId": "",
                "operationId": params["operation_id"],
                "counts": {
                    "manifestAliases": 1, "deterministicAliases": 1,
                    "liveBoundAliases": 1, "activeValidatedAliases": 1,
                    "renamedShaders": 0, "renamedTextures": 0,
                    "renamedResources": 0, "keptUser": 1,
                    "skipped": 0, "conflicts": 0,
                },
                "written": [], "replacedManaged": [],
                "keptUser": [{
                    "resourceId": 77,
                    "displayName": "MyUserTexture",
                    "requestedDisplayName": "T_Body_BaseMap",
                    "bindingKind": "textureresource",
                }],
                "skipped": [], "conflicts": [],
            }
        raise AssertionError(method)

    manager = SimpleNamespace(
        verify_resource_alias_authorization=lambda unused_ws, rows, unused_auth: (
            list(rows), _authorization()
        )
    )
    service = SimpleNamespace(status=lambda unused: {"workspacePath": str(tmp_path)})
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "LongToolSlot", _FastSlot)
    monkeypatch.setattr(server, "update_long_tool", lambda *a, **k: None)

    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path), json.dumps([_row()]),
        operation_id="resource-user-kept-test", event_id=11384, wait_seconds=5,
    )))
    assert result["complete"] is True
    assert result["applied"] is False
    assert result["counts"]["keptUser"] == 1
    assert result["rows"][0]["currentDisplayName"] == "MyUserTexture"


def test_unverified_old_manifest_is_never_rolled_back(tmp_path, monkeypatch):
    old_path = tmp_path / "unverified-old-aliases.json"
    old_path.write_text(json.dumps({
        "schema": "renderdoc-ultra-aliases/v1",
        "schemaVersion": 1,
        "captureId": CAPTURE_ID,
        "aliases": [_shader_alias()],
    }), encoding="utf-8")
    calls: list[str] = []

    def ipc_call(method, unused_params, **unused):
        calls.append(method)
        if method != "get_symbol_alias_transaction":
            raise AssertionError("no bridge mutation is allowed after failed preflight")
        return {
            "schema": "renderdoc-ultra-alias-transaction-status/v1",
            "schemaVersion": 1,
            "captureId": CAPTURE_ID,
            "captureSessionId": CAPTURE_SESSION_ID,
            "transactionId": "2" * 32,
            "state": "committed",
            "committed": True,
            "mutationPresent": True,
            "recoveryRequired": True,
            "rollbackAvailable": True,
            # Deliberately does not match old_path's bytes.
            "manifestSha256": "0" * 64,
            "manifestPath": str(old_path),
            "replayMutation": False,
        }

    manager = SimpleNamespace(
        verify_resource_alias_authorization=lambda unused_ws, rows, unused_auth: (
            list(rows), _authorization()
        )
    )
    service = SimpleNamespace(status=lambda unused: {"workspacePath": str(tmp_path)})
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "LongToolSlot", _FastSlot)
    monkeypatch.setattr(server, "update_long_tool", lambda *a, **k: None)

    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path), json.dumps([_row()]),
        operation_id="resource-invalid-old-manifest", event_id=11384, wait_seconds=5,
    )))
    assert result["ok"] is False
    assert "do not match bridge status" in result["error"]
    assert calls
    assert set(calls) == {"get_symbol_alias_transaction"}


@pytest.mark.parametrize("initial_phase", ["old-rolled-back", "recovery-required"])
def test_old_rolled_back_ledger_resumes_merged_commit(
    tmp_path, monkeypatch, initial_phase
):
    operation_id = "resource-resume-" + initial_phase
    authorization = _authorization()
    rows = [_row()]
    aliases = server._resource_alias_manifest_rows(rows, authorization)
    merged, replaced = merge_alias_manifest(
        CAPTURE_ID,
        [_shader_alias(), _texture_alias("OldManagedName")],
        aliases,
    )
    manifest_path, ledger_path = publication_paths(tmp_path, operation_id)
    manifest_sha = write_manifest(manifest_path, merged)
    old_manifest_path = tmp_path / "old-aliases.json"
    old_manifest_raw = json.dumps({
        "schema": "renderdoc-ultra-aliases/v1",
        "schemaVersion": 1,
        "captureId": CAPTURE_ID,
        "aliases": [_shader_alias(), _texture_alias("OldManagedName")],
    }, separators=(",", ":")).encode("utf-8")
    old_manifest_path.write_bytes(old_manifest_raw)
    old_manifest_sha = hashlib.sha256(old_manifest_raw).hexdigest()
    bridge_operation_id = hashlib.sha256(
        ("resource-name-publication\0" + operation_id).encode("utf-8")
    ).hexdigest()[:32]
    request_sha = server._canonical_json_sha256({
        "tool": "publish_shader_resource_names",
        "workspaceId": authorization["workspaceId"],
        "bindingId": authorization["bindingId"],
        "rows": rows,
    }, "resource name publication")
    write_ledger(ledger_path, {
        "schema": LEDGER_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "bridgeOperationId": bridge_operation_id,
        "requestSha256": request_sha,
        "bindingId": authorization["bindingId"],
        "captureId": CAPTURE_ID,
        "captureSessionId": CAPTURE_SESSION_ID,
        "manifestPath": str(manifest_path),
        "manifestSha256": manifest_sha,
        "oldTransactionId": "2" * 32,
        "oldManifestPath": str(old_manifest_path),
        "oldManifestSha256": old_manifest_sha,
        "replacedTargets": [list(item) for item in sorted(replaced)],
        "phase": initial_phase,
        "result": None,
    })
    calls: list[str] = []

    def ipc_call(method, params, **unused):
        calls.append(method)
        if method == "get_symbol_alias_transaction":
            return {
                "schema": "renderdoc-ultra-alias-transaction-status/v1",
                "schemaVersion": 1,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "transactionId": "",
                "state": "absent",
                "committed": False,
                "mutationPresent": False,
                "recoveryRequired": False,
                "rollbackAvailable": False,
                "manifestSha256": "",
                "manifestPath": "",
                "replayMutation": False,
            }
        if method == "load_symbol_aliases":
            assert params["operation_id"] == bridge_operation_id
            return {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "path": params["path"],
                "confirmedOnly": True,
                "strictAtomic": True,
                "committed": True,
                "recoveryRequired": False,
                "rollbackAvailable": True,
                "transactionId": bridge_operation_id,
                "operationId": bridge_operation_id,
                "counts": {
                    "manifestAliases": 2, "deterministicAliases": 2,
                    "liveBoundAliases": 2, "activeValidatedAliases": 2,
                    "renamedShaders": 1, "renamedTextures": 1,
                    "renamedResources": 2, "keptUser": 0,
                    "skipped": 0, "conflicts": 0,
                },
                "written": [
                    {"resourceId": 11, "displayName": "ExistingProjectShader"},
                    {"resourceId": 77, "displayName": "T_Body_BaseMap"},
                ],
                "replacedManaged": [], "keptUser": [],
                "skipped": [], "conflicts": [],
            }
        raise AssertionError(method)

    manager = SimpleNamespace(
        verify_resource_alias_authorization=lambda unused_ws, value, unused_auth: (
            list(value), authorization
        )
    )
    service = SimpleNamespace(status=lambda unused: {"workspacePath": str(tmp_path)})
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "LongToolSlot", _FastSlot)
    monkeypatch.setattr(server, "update_long_tool", lambda *a, **k: None)

    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path), json.dumps(rows),
        operation_id=operation_id, event_id=11384, wait_seconds=5,
    )))
    assert result["complete"] is True
    assert result["counts"]["replacedManaged"] == 1
    assert "rollback_symbol_aliases" not in calls
    assert calls == ["get_symbol_alias_transaction", "load_symbol_aliases"]
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["phase"] == "committed"


def test_failed_receipt_rolls_back_new_transaction_without_previous_aliases(
    tmp_path, monkeypatch
):
    state = {"transaction": "", "manifest": "", "sha": ""}
    calls: list[str] = []

    def ipc_call(method, params, **unused):
        calls.append(method)
        if method == "get_symbol_alias_transaction":
            present = bool(state["transaction"])
            return {
                "schema": "renderdoc-ultra-alias-transaction-status/v1",
                "schemaVersion": 1,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "transactionId": state["transaction"],
                "state": "committed" if present else "absent",
                "committed": present,
                "mutationPresent": present,
                "recoveryRequired": present,
                "rollbackAvailable": present,
                "manifestSha256": state["sha"] if present else "",
                "manifestPath": state["manifest"] if present else "",
                "replayMutation": False,
            }
        if method == "load_symbol_aliases":
            # Model a committed bridge transaction whose reply is repeatedly
            # lost/invalid, including the idempotent receipt lookup.
            state.update(
                transaction=params["operation_id"],
                manifest=params["path"],
                sha=params["expected_sha256"],
            )
            raise RuntimeError("synthetic committed response loss")
        if method == "rollback_symbol_aliases":
            transaction = state["transaction"]
            assert transaction == params["expected_transaction_id"]
            state.update(transaction="", manifest="", sha="")
            return {
                "schema": "renderdoc-ultra-alias-rollback/v1",
                "schemaVersion": 1,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "transactionId": transaction,
                "status": "restored",
                "restored": True,
                "verified": True,
                "recoveryRequired": False,
                "failures": [],
            }
        raise AssertionError(method)

    manager = SimpleNamespace(
        verify_resource_alias_authorization=lambda unused_ws, rows, unused_auth: (
            list(rows), _authorization()
        )
    )
    service = SimpleNamespace(status=lambda unused: {"workspacePath": str(tmp_path)})
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "LongToolSlot", _FastSlot)
    monkeypatch.setattr(server, "update_long_tool", lambda *a, **k: None)

    result = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path), json.dumps([_row()]),
        operation_id="resource-new-transaction-response-loss", event_id=11384,
        wait_seconds=5,
    )))
    assert result["ok"] is False
    assert state["transaction"] == ""
    assert calls.count("rollback_symbol_aliases") == 1
    ledger_path = next(
        (tmp_path / "ai" / "resource-name-publications").glob("*.operation.json")
    )
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["phase"] == (
        "failed-new-rolled-back"
    )
