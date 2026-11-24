"""Shader mutations keep pollable receipts across client timeouts."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading
import time
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest

from src import host_runtime, server
from src.shader_workspace import mutation_jobs
from src.shader_workspace.mutation_jobs import (
    get_shader_mutation_job,
    start_shader_mutation_job,
    wait_for_shader_mutation_job,
)
from src.shader_workspace.organization import (
    ShaderOrganizationError,
    ShaderOrganizationManager,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _clear_mutation_jobs() -> None:
    with mutation_jobs._LOCK:
        mutation_jobs._JOBS.clear()
        mutation_jobs._ORDER.clear()
        mutation_jobs._ACTIVE_BY_TARGET.clear()


def test_same_operation_id_joins_and_keeps_terminal_receipt(monkeypatch):
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    release = threading.Event()
    calls: list[str] = []
    operation_id = uuid.uuid4().hex

    def runner():
        calls.append("run")
        assert release.wait(2.0)
        return {"ok": True, "marker": "committed"}

    first = start_shader_mutation_job(
        tool="rename_shader_workspace_symbols",
        workspace_path="D:/ws-idempotent",
        organization_id="a" * 32,
        request_sha256=_digest("same-request"),
        operation_id=operation_id,
        runner=runner,
    )
    joined = start_shader_mutation_job(
        tool="rename_shader_workspace_symbols",
        workspace_path="D:/ws-idempotent",
        organization_id="a" * 32,
        request_sha256=_digest("same-request"),
        operation_id=operation_id,
        runner=lambda: {"ok": False, "marker": "must-not-run"},
    )
    assert joined["operationId"] == first["operationId"]
    assert joined["idempotentReplay"] is True
    assert len(calls) == 1

    release.set()
    finished = wait_for_shader_mutation_job(operation_id, 2.0)
    assert finished is not None and finished["status"] == "done"
    assert finished["result"]["marker"] == "committed"
    assert get_shader_mutation_job(operation_id)["result"] == finished["result"]


def test_operation_id_cannot_be_rebound_to_different_request(monkeypatch):
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    operation_id = uuid.uuid4().hex
    start_shader_mutation_job(
        tool="rename_shader_workspace_symbols",
        workspace_path="D:/ws-binding",
        organization_id="b" * 32,
        request_sha256=_digest("request-one"),
        operation_id=operation_id,
        runner=lambda: {"ok": True},
    )
    with pytest.raises(ValueError, match="different mutation request"):
        start_shader_mutation_job(
            tool="rename_shader_workspace_symbols",
            workspace_path="D:/ws-binding",
            organization_id="b" * 32,
            request_sha256=_digest("request-two"),
            operation_id=operation_id,
            runner=lambda: {"ok": True},
        )


def test_completed_worker_reports_rejected_draft_separately_from_transport(monkeypatch):
    checkpoints = []
    monkeypatch.setattr(
        host_runtime,
        "record_memory_checkpoint",
        lambda *args, **kwargs: checkpoints.append((args[0], kwargs)),
    )
    operation_id = uuid.uuid4().hex
    start_shader_mutation_job(
        tool="restructure_shader_workspace_source",
        workspace_path="D:/ws-rejected-draft",
        organization_id="f" * 32,
        request_sha256=_digest("rejected-draft"),
        operation_id=operation_id,
        runner=lambda: {
            "accepted": False,
            "applied": False,
            "reason": "range crosses a branch-local declaration",
        },
    )
    finished = wait_for_shader_mutation_job(operation_id, 2.0)
    assert finished is not None and finished["status"] == "done"
    assert finished["resultAccepted"] is False
    assert finished["rejectionReason"] == "range crosses a branch-local declaration"
    assert "allowedActions" not in finished
    end = next(kwargs["extra"] for phase, kwargs in checkpoints
               if phase == "shader-mutation-end")
    assert end["ok"] is True  # The worker completed, but its edit was rejected.
    assert end["resultAccepted"] is False
    assert end["rejectionReason"] == finished["rejectionReason"]


def test_second_mutation_reports_busy_operation(monkeypatch):
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    release = threading.Event()
    first_id = uuid.uuid4().hex
    second_id = uuid.uuid4().hex

    first = start_shader_mutation_job(
        tool="restructure_shader_workspace_source",
        workspace_path="D:/ws-busy",
        organization_id="c" * 32,
        request_sha256=_digest("first"),
        operation_id=first_id,
        runner=lambda: (release.wait(2.0) and {"ok": True}),
    )
    deadline = time.time() + 1.0
    while get_shader_mutation_job(first_id)["status"] == "queued" and time.time() < deadline:
        time.sleep(0.01)
    second = start_shader_mutation_job(
        tool="rename_shader_workspace_symbols",
        workspace_path="D:/ws-busy",
        organization_id="c" * 32,
        request_sha256=_digest("second"),
        operation_id=second_id,
        runner=lambda: {"ok": False},
    )
    assert first["operationId"] == first_id
    assert second["status"] == "busy"
    assert second["blockedByOperationId"] == first_id
    assert "allowedActions" not in second
    release.set()
    assert wait_for_shader_mutation_job(first_id, 2.0)["status"] == "done"


@pytest.mark.skipif(os.name != "nt", reason="extended paths are Windows-only")
def test_equivalent_workspace_spelling_shares_active_mutation_target(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    normal = str(tmp_path.resolve())
    extended = "\\\\?\\" + normal
    release = threading.Event()
    first_id = uuid.uuid4().hex
    second_id = uuid.uuid4().hex
    try:
        first = start_shader_mutation_job(
            tool="rename_shader_workspace_symbols",
            workspace_path=normal,
            organization_id="d" * 32,
            request_sha256=_digest("normal-active-target"),
            operation_id=first_id,
            runner=lambda: (release.wait(2.0) and {"ok": True}),
        )
        deadline = time.time() + 1.0
        while (
            get_shader_mutation_job(first_id)["status"] == "queued"
            and time.time() < deadline
        ):
            time.sleep(0.01)
        second = start_shader_mutation_job(
            tool="restructure_shader_workspace_source",
            workspace_path=extended,
            organization_id="d" * 32,
            request_sha256=_digest("extended-busy-target"),
            operation_id=second_id,
            runner=lambda: {"ok": False},
        )
        assert first["operationId"] == first_id
        assert second["status"] == "busy"
        assert second["blockedByOperationId"] == first_id
    finally:
        release.set()
        wait_for_shader_mutation_job(first_id, 2.0)


@pytest.mark.skipif(os.name != "nt", reason="extended paths are Windows-only")
def test_server_rename_replays_legacy_normal_path_hash_after_restart(
    tmp_path, monkeypatch
):
    _clear_mutation_jobs()
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    normal = str(tmp_path.resolve())
    extended = "\\\\?\\" + normal
    operation_id = "legacy-rename-path-operation"
    organization_id = "e" * 32
    renames = {"oldName": "BaseColor"}
    legacy_request = {
        "tool": "rename_shader_workspace_symbols",
        "workspacePath": normal,
        "organizationId": organization_id,
        "renames": renames,
        "comments": {},
        "assetNameAuthorization": None,
    }
    legacy_sha = server._canonical_json_sha256(legacy_request, "legacy rename")

    class DurableManager:
        def rename(self, *args, **kwargs):
            assert legacy_sha in kwargs["compatible_request_sha256"]
            assert kwargs["request_sha256"] != legacy_sha
            return {
                "accepted": True,
                "applied": False,
                "idempotentReplay": True,
                "operationId": operation_id,
                "durableMutation": {"requestSha256": legacy_sha},
            }

    monkeypatch.setattr(server, "_shader_organization_manager", DurableManager)
    result = json.loads(server.rename_shader_workspace_symbols(
        extended,
        organization_id,
        renames,
        operation_id=operation_id,
    ))
    assert "error" not in result, result
    assert result["idempotentReplay"] is True
    _clear_mutation_jobs()


@pytest.mark.skipif(os.name != "nt", reason="extended paths are Windows-only")
def test_server_restructure_replays_legacy_normal_path_hash_after_restart(
    tmp_path, monkeypatch
):
    _clear_mutation_jobs()
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    normal = str(tmp_path.resolve())
    extended = "\\\\?\\" + normal
    operation_id = "legacy-restructure-path-operation"
    organization_id = "f" * 32
    legacy_request = {
        "tool": "restructure_shader_workspace_source",
        "workspacePath": normal,
        "organizationId": organization_id,
        "operation": "note",
        "startLine": 0,
        "endLine": 0,
        "name": "MainPS",
        "newName": "",
        "comment": "Explains the material output.",
        "expectedProposalSha256": "",
        "edits": None,
    }
    legacy_sha = server._canonical_json_sha256(
        legacy_request, "legacy restructure"
    )

    class DurableManager:
        def restructure(self, *args, **kwargs):
            assert legacy_sha in kwargs["compatible_request_sha256"]
            assert kwargs["request_sha256"] != legacy_sha
            return {
                "accepted": True,
                "applied": False,
                "idempotentReplay": True,
                "operationId": operation_id,
                "durableMutation": {"requestSha256": legacy_sha},
            }

    monkeypatch.setattr(server, "_shader_organization_manager", DurableManager)
    result = json.loads(asyncio.run(server.restructure_shader_workspace_source(
        extended,
        organization_id,
        "note",
        name="MainPS",
        comment="Explains the material output.",
        operation_id=operation_id,
    )))
    assert "error" not in result, result
    assert result["idempotentReplay"] is True
    _clear_mutation_jobs()


def test_organization_durable_receipt_accepts_only_declared_legacy_hash():
    manager = object.__new__(ShaderOrganizationManager)
    operation_id = "legacy-durable-receipt"
    legacy_sha = _digest("legacy-path-bound-request")
    canonical_sha = _digest("canonical-path-bound-request")
    session = {
        "mutationReceipts": {
            operation_id: {
                "tool": "rename_shader_workspace_symbols",
                "requestSha256": legacy_sha,
                "committed": True,
                "committedAt": "2026-09-20T00:00:00Z",
                "resultSummary": {"accepted": True, "applied": False},
            }
        },
        "lastMutationResult": {
            "operationId": operation_id,
            "requestSha256": legacy_sha,
            "result": {"accepted": True, "applied": False},
        },
    }
    replay = manager._durable_mutation_replay(
        session,
        tool="rename_shader_workspace_symbols",
        operation_id=operation_id,
        request_sha256=canonical_sha,
        compatible_request_sha256=(legacy_sha,),
    )
    assert replay is not None and replay["idempotentReplay"] is True
    assert replay["durableMutation"]["requestSha256"] == legacy_sha
    with pytest.raises(ShaderOrganizationError, match="different durable mutation"):
        manager._durable_mutation_replay(
            session,
            tool="rename_shader_workspace_symbols",
            operation_id=operation_id,
            request_sha256=canonical_sha,
        )


def test_durable_failed_job_reports_same_operation_retry_capability(monkeypatch):
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    operation_id = uuid.uuid4().hex
    failed = start_shader_mutation_job(
        tool="publish_shader_resource_names",
        workspace_path="D:/ws-durable-recovery",
        organization_id="resource-names:" + "d" * 32,
        request_sha256=_digest("durable-request"),
        operation_id=operation_id,
        runner=lambda: (_ for _ in ()).throw(RuntimeError("uncertain bridge response")),
        retry_failed=True,
    )
    if failed["status"] != "failed":
        failed = wait_for_shader_mutation_job(operation_id, 2.0)
    assert failed is not None and failed["status"] == "failed"
    assert failed["sameOperationRetryAllowed"] is True
    assert failed["operationId"] == operation_id
    assert "allowedActions" not in failed

    recovered = start_shader_mutation_job(
        tool="publish_shader_resource_names",
        workspace_path="D:/ws-durable-recovery",
        organization_id="resource-names:" + "d" * 32,
        request_sha256=_digest("durable-request"),
        operation_id=operation_id,
        runner=lambda: {"ok": True, "marker": "recovered"},
        retry_failed=True,
    )
    if recovered["status"] != "done":
        recovered = wait_for_shader_mutation_job(operation_id, 2.0)
    assert recovered is not None and recovered["status"] == "done"
    assert recovered["result"]["marker"] == "recovered"


def test_alias_publication_requires_explicit_retry_id_before_write(tmp_path, monkeypatch):
    for function in (
        server.publish_shader_resource_names,
        server.publish_cbuffer_aliases,
    ):
        assert (
            inspect.signature(function).parameters["operation_id"].default
            is inspect.Parameter.empty
        )

    manager = mock.Mock()
    resource_rows = [{
        "selector": "resource:test",
        "resourceId": 77,
        "bindNumber": 4,
        "registerSpace": 0,
        "alias": "T_Test",
        "assetPath": "/Game/T_Test",
        "hostEvidenceKind": "same-image",
    }]
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(
        server,
        "_shader_workspace_service",
        lambda: SimpleNamespace(status=lambda unused: {"workspacePath": str(tmp_path)}),
    )
    monkeypatch.setattr(
        server,
        "ipc",
        SimpleNamespace(call=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bridge mutation must not run")
        )),
    )

    published = json.loads(asyncio.run(server.publish_shader_resource_names(
        str(tmp_path),
        json.dumps(resource_rows),
        operation_id="",
        event_id=11384,
    )))
    published_cbuffer = json.loads(asyncio.run(server.publish_cbuffer_aliases(
        str(tmp_path),
        json.dumps([{"bind": 4, "byteOffset": 0, "alias": "Surface"}]),
        operation_id="",
        asset_alias_authorization={"authorizationId": "test"},
    )))

    assert "operation_id is required" in published["error"]
    assert "operation_id is required" in published_cbuffer["error"]
    manager.rename.assert_not_called()
    manager.restructure.assert_not_called()


@pytest.mark.parametrize("tool", ["rename", "restructure"])
def test_source_mutation_owns_generated_id_without_exclusive_progress_slot(
    tmp_path, monkeypatch, tool
):
    _clear_mutation_jobs()
    monkeypatch.setattr(host_runtime, "record_memory_checkpoint", lambda *a, **k: None)
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs["operation_id"])
        return {"accepted": True, "applied": False}
    manager = SimpleNamespace(rename=execute, restructure=execute)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr(server, "workspace_bound_request_hashes", lambda *args: (_digest(tool), ()))
    # A real unrelated telemetry owner remains active. Draft CAS/transactions
    # still serialize writers; a progress row is not a source permission.
    owner = host_runtime.LongToolSlot("compile_shader_workspace", {})
    assert owner.busy is None
    try:
        if tool == "rename":
            returned = json.loads(server.rename_shader_workspace_symbols(
                str(tmp_path), "a" * 32, {"old": "new"}
            ))
        else:
            returned = json.loads(asyncio.run(server.restructure_shader_workspace_source(
                str(tmp_path), "a" * 32, "note", name="Evaluate", comment="Known computation"
            )))
        assert "error" not in returned, returned
        result = returned
        assert result["accepted"] is True
        assert result["toolStatus"] == "ok"
        assert result["hostRuntime"]["elapsedSeconds"] is None
        assert result["mutation"]["status"] == "done"
        generated_id = result["operationId"]
        assert len(generated_id) == 32 and calls == [generated_id]
        assert get_shader_mutation_job(generated_id)["status"] == "done"
        assert host_runtime.current_status()["currentTool"]["name"] == "compile_shader_workspace"
    finally:
        owner.close(ok=True)
        _clear_mutation_jobs()
