import asyncio
from contextlib import contextmanager
import hashlib
import json
import threading
from pathlib import Path
from unittest import mock

import pytest

from src import unity_integration as unity
from src.mcp_tool_client import McpToolError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / "UnityProject"
    for name in ("Assets", "Packages", "ProjectSettings", "Library/Bread"):
        (project / name).mkdir(parents=True)
    (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.55f1\n")
    (project / "Packages/manifest.json").write_text('{"dependencies": {}}')
    (project / "Library/Bread/unity-connection.json").write_text('{"enabled":true,"nonce":"same"}')
    service = unity.UnityIntegrationService(tmp_path / "cache")
    executable = tmp_path / "server.exe"
    executable.write_text("fixture")
    monkeypatch.setattr(service, "_runtime_executable", lambda: executable)
    monkeypatch.setattr(unity, "AUTO_RECOVERY_GRACE_SECONDS", 0)
    instance = {"id": "Exact@1234abcd", "hash": "1234abcd"}
    service._instances[unity._key(project)] = instance
    identity = {"success": True, "data": {"projectPath": str(project), "nonce": "same", "unityVersion": "6000.0.55f1"}}
    client = mock.Mock(call=mock.AsyncMock(), close=mock.AsyncMock())
    monkeypatch.setattr("src.mcp_tool_client.shared_client", lambda: client)

    def prepared(*args, **kwargs):
        service._instances[unity._key(project)] = instance
        return {"jobId": "connect-job", "status": "completed", "result": {"connected": True}}

    prepare = mock.Mock(side_effect=prepared)
    monkeypatch.setattr(service, "prepare_project", prepare)
    return project, service, client, identity, prepare


def test_missing_connection_prepares_and_continues_the_original_operation(setup, monkeypatch):
    project, service, client, identity, prepare = setup
    service._instances.clear()
    monkeypatch.setattr(service, "_probe", lambda p: None)
    client.call.side_effect = [{"success": True}, identity, {"success": True, "created": "material"}]
    result = asyncio.run(service.call_tool(str(project), "manage_material", {"action": "create"}))
    assert result["created"] == "material"
    assert prepare.call_args.kwargs == {"reconnect": True, "automatic_recovery": True}
    assert [c.args[1] for c in client.call.call_args_list] == ["set_active_instance", "execute_custom_tool", "manage_material"]


def test_preflight_transport_failure_does_not_lose_or_duplicate_the_write(setup):
    project, service, client, identity, prepare = setup
    client.call.side_effect = [McpToolError("closed before dispatch", transport_failure=True, request_sent=False),
        {"success": True}, identity, {"success": True}]
    assert asyncio.run(service.call_tool(str(project), "manage_material", {"action": "create"}))["success"]
    assert sum(c.args[1] == "manage_material" for c in client.call.call_args_list) == 1
    assert prepare.call_count == 1


@pytest.mark.parametrize("action,replayed", [("inspect", True), ("create", False)])
def test_lost_reply_only_replays_observations(setup, action, replayed):
    project, service, client, identity, prepare = setup
    client.call.side_effect = [{"success": True}, identity,
        McpToolError("reply lost", transport_failure=True, request_sent=True),
        {"success": True}, identity, {"success": True}]
    result = asyncio.run(service.call_tool(str(project), "manage_material", {"action": action}))
    assert prepare.call_count == 1
    count = sum(c.args[1] == "manage_material" for c in client.call.call_args_list)
    assert count == (2 if replayed else 1)
    if replayed:
        assert result["success"]
    else:
        assert result["operationOutcome"] == "unknown" and result["safeToRetry"] is False
        assert result["connectionRestored"] and result["nextAction"]["code"] == "inspect-operation-outcome"


def test_editor_application_error_retains_diagnostic_without_installing_again(setup):
    project, service, client, identity, prepare = setup
    client.call.side_effect = [{"success": True}, identity, McpToolError("material parameter does not exist")]
    result = asyncio.run(service.call_tool(str(project), "manage_material", {"action": "set"}))
    assert "parameter does not exist" in result["detail"]
    assert not result["connectionRecoveryNeeded"]
    assert not service._retryable_connection_result(result)
    assert unity._key(project) in service._instances
    prepare.assert_not_called()


def test_external_connection_recovers_without_overwriting_its_adapter(setup):
    project, service, client, identity, prepare = setup
    adapter = {"descriptor": {"id": "custom-project-mcp"}, "identityTool": "project_identity",
        "identityArguments": {}, "customIdentity": False, "tools": {"manage_material"}, "capabilities": set()}
    service._external_connections[unity._key(project)] = adapter
    client.call.side_effect = [McpToolError("closed", transport_failure=True, request_sent=False),
        identity, identity, {"success": True}, identity]
    assert asyncio.run(service.call_tool(str(project), "manage_material", {"action": "create"}))["success"]
    assert service._external_connections[unity._key(project)] is adapter
    assert sum(c.args[1] == "manage_material" for c in client.call.call_args_list) == 1
    prepare.assert_not_called()


def test_external_parameter_error_keeps_the_healthy_connection(setup):
    project, service, client, identity, prepare = setup
    adapter = {"descriptor": {"id": "custom-project-mcp"}, "identityTool": "project_identity",
        "identityArguments": {}, "customIdentity": False, "tools": {"manage_material"}, "capabilities": set()}
    service._external_connections[unity._key(project)] = adapter
    client.call.side_effect = [identity, McpToolError("invalid material parameter")]
    result = asyncio.run(service.call_tool(str(project), "manage_material", {"action": "set"}))
    assert result["status"] == "failed" and not result["connectionRecoveryNeeded"]
    assert service._external_connections[unity._key(project)] is adapter
    client.close.assert_not_called()
    prepare.assert_not_called()


def test_long_install_returns_one_observable_background_job(setup, monkeypatch):
    project, service, client, identity, prepare = setup
    service._instances.clear()
    monkeypatch.setattr(service, "_probe", lambda p: None)
    prepare.side_effect = None
    prepare.return_value = {"jobId": "install", "status": "queued"}
    result = asyncio.run(service.call_tool(str(project), "bread_inspect_rendering"))
    assert result["status"] == "pending" and result["jobId"] == "install"
    assert result["operationOutcome"] == "not-started"
    assert result["nextAction"]["tool"] == "get_unity_integration_job"
    client.call.assert_not_called()


def test_calls_during_preparation_return_progress_without_waiting_on_the_project_lock(setup):
    project, service, client, identity, prepare = setup
    job = unity._Job("preparing", str(project), status="running", stage="await-editor-connection")
    service._jobs[job.id] = job
    service._active[unity._key(project)] = job.id
    with mock.patch.object(service, "_call_connected_tool", new=mock.AsyncMock()) as call:
        result = asyncio.run(service.call_tool(str(project), "manage_material", {"action": "create"}))
    assert result["status"] == "pending" and result["jobId"] == job.id
    assert result["recovery"]["stage"] == "await-editor-connection"
    call.assert_not_called()
    prepare.assert_not_called()


def test_initial_import_reply_loss_continues_by_reading_the_existing_receipt(setup):
    project, service, client, identity, prepare = setup
    request = project / "Library/Bread/request.json"
    expected = {"requestSha256": "a" * 64, "packageManifestSha256": "b" * 64}
    request.write_text(json.dumps(expected))
    digest = hashlib.sha256(request.read_bytes()).hexdigest()
    checks = {key: True for key in ("meshImport", "shaderCompile", "materialImport", "textureImport", "sceneIntegrity", "pipelineCompatible")}
    service.call_tool = mock.AsyncMock(side_effect=[
        {"status": "failed", "code": "unity-operation-outcome-unknown", "operationOutcome": "unknown", "safeToRetry": False},
        {"success": True, "data": {**expected, "requestFileSha256": digest, "status": "imported", "checks": checks}}])
    job = unity._Job("import", str(project))
    asyncio.run(service._import_worker(job, request, digest, None))
    assert job.status == "completed"
    assert [c.args[2]["action"] for c in service.call_tool.call_args_list] == ["import", "status"]
    assert all(c.args[2]["request_path"] == str(request) for c in service.call_tool.call_args_list)


def test_failed_automatic_prepare_is_reused_until_an_explicit_repair_retry(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "Assets").mkdir()
    (project / "Packages").mkdir()
    (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.55f1\n")
    (project / "Packages/manifest.json").write_text('{"dependencies": {}}')
    service = unity.UnityIntegrationService(tmp_path / "cache")
    previous = unity._Job("previous", str(project), status="failed", automatic_recovery=True)
    service._jobs[previous.id] = previous
    service._active[unity._key(project)] = previous.id
    with mock.patch.object(unity.threading, "Thread") as thread:
        assert service.prepare_project(str(project), automatic_recovery=True)["jobId"] == "previous"
        thread.assert_not_called()
        assert service.prepare_project(str(project), reconnect=True)["jobId"] != "previous"
        thread.assert_called_once()


def test_changed_port_invalidates_cached_identity(setup, tmp_path, monkeypatch):
    project, service, client, identity, prepare = setup
    status = tmp_path / "status"
    status.mkdir()
    monkeypatch.setenv("UNITY_MCP_STATUS_DIR", str(status))
    record = {"reloading": False, "unity_port": 22222, "project_path": str(project)}
    (status / "unity-mcp-status-1234abcd.json").write_text(json.dumps(record))
    service._instances[unity._key(project)].update(nonce="same", port=11111)
    assert not service._known_connection(project)
    record["unity_port"] = 11111
    (status / "unity-mcp-status-1234abcd.json").write_text(json.dumps(record))
    assert service._known_connection(project)


def test_import_poll_reads_the_existing_receipt_after_an_uncertain_write(setup):
    project, service, client, identity, prepare = setup
    service.call_tool = mock.AsyncMock(side_effect=[
        {"status": "pending", "operationOutcome": "unknown", "safeToRetry": False},
        {"success": True, "data": {"status": "imported"}}])
    with mock.patch.object(unity.asyncio, "sleep", new=mock.AsyncMock()):
        result = asyncio.run(service._poll_tool(unity._Job("import", str(project)),
            "bread_import_capture", {"action": "import", "request_path": "same-request.json"}, 2))
    assert result["data"]["status"] == "imported"
    assert [c.args[2]["action"] for c in service.call_tool.call_args_list] == ["import", "status"]
    assert all(c.args[2]["request_path"] == "same-request.json" for c in service.call_tool.call_args_list)


def test_reconnect_waits_for_editor_acknowledgement_before_dispatch(setup, monkeypatch, tmp_path):
    project, service, client, identity, prepare = setup
    config = project / "Library/Bread/unity-connection.json"
    config.write_text('{"enabled":true,"nonce":"same","reconnectRequest":"next"}')
    status = tmp_path / "status"
    status.mkdir()
    monkeypatch.setenv("UNITY_MCP_STATUS_DIR", str(status))
    (status / "unity-mcp-status-1234abcd.json").write_text(json.dumps({
        "reloading": False, "project_path": str(project), "unity_port": 12345}))
    service._instances[unity._key(project)].update(nonce="same", port=12345, reconnectRequest="previous")
    assert not service._known_connection(project)
    monkeypatch.setattr(service, "_bridge_identity", lambda _: identity)
    assert service._probe(project) is None
    client.call.side_effect = [{"success": True}, identity]
    result = asyncio.run(service._call_connected_tool(str(project), "manage_material", {"action": "create"}))
    assert result["code"] == "unity-connection-not-ready" and result["operationOutcome"] == "not-started"
    assert all(c.args[1] != "manage_material" for c in client.call.call_args_list)
    identity["data"]["reconnectRequest"] = "next"
    assert service._probe(project)["reconnectRequest"] == "next"
    client.call.side_effect = [{"success": True}, identity, {"success": True}]
    assert asyncio.run(service._call_connected_tool(str(project), "manage_material", {"action": "create"}))["success"]
    assert service._known_connection(project)


@pytest.mark.parametrize("shared", [False, True])
def test_cancel_during_recovery_respects_installer_ownership(setup, monkeypatch, shared):
    project, service, client, identity, prepare = setup
    monkeypatch.setattr(unity, "AUTO_RECOVERY_GRACE_SECONDS", 5)
    service._call_connected_tool = mock.AsyncMock(return_value={
        "connectionRecoveryNeeded": True, "operationOutcome": "not-started"})
    prepare.side_effect = None
    prepare.return_value = {"jobId": "owned", "status": "queued"}
    monkeypatch.setattr(service, "job_status", lambda _: prepare.return_value)
    cancel = mock.Mock()
    monkeypatch.setattr(service, "cancel_job", cancel)

    async def run():
        task = asyncio.create_task(service.call_tool(str(project), "bread_inspect_rendering"))
        while not prepare.called:
            await asyncio.sleep(0)
        if shared:
            service._jobs["owned"] = unity._Job("owned", str(project), reuse_count=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    if shared:
        cancel.assert_not_called()
    else:
        cancel.assert_called_once_with("owned")


def test_a_second_transport_failure_returns_diagnostics_without_an_install_loop(setup):
    project, service, client, identity, prepare = setup
    client.call.side_effect = McpToolError("server process cannot start", transport_failure=True, request_sent=False)
    result = asyncio.run(service.call_tool(str(project), "manage_material", {"action": "create"}))
    assert result["code"] == "unity-connection-recovery-failed"
    assert not unity.UnityIntegrationService._retryable_connection_result(result)
    assert prepare.call_count == 1 and client.call.await_count == 2


def test_rpc_parameter_error_preserves_the_existing_session():
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    from src.mcp_tool_client import McpToolClient
    client = McpToolClient()
    session = mock.Mock(call_tool=mock.AsyncMock(side_effect=McpError(ErrorData(code=-32602, message="wrong parameter"))))
    client.session = mock.AsyncMock(return_value=session)
    client.close = mock.AsyncMock()
    with pytest.raises(McpToolError) as error:
        asyncio.run(client.call({"id": "only-this-project"}, "manage_material", {}))
    assert not error.value.transport_failure and error.value.request_sent is True
    client.close.assert_not_awaited()


def test_stalled_preflight_recovers_before_the_full_operation_timeout(setup, monkeypatch):
    project, service, client, identity, prepare = setup
    monkeypatch.setattr(unity, "UNITY_PREFLIGHT_TIMEOUT_SECONDS", 0.02)
    counter = 0

    async def call(descriptor, name, arguments):
        nonlocal counter
        counter += 1
        if counter == 1:
            await asyncio.Event().wait()
        if name == "execute_custom_tool":
            return identity
        return {"success": True}

    client.call.side_effect = call
    assert asyncio.run(service.call_tool(str(project), "manage_material", {"action": "create"}))["success"]
    assert prepare.call_count == 1 and counter == 4


def test_cancelling_a_waiting_project_call_releases_its_lock_worker(setup, monkeypatch):
    project, service, client, identity, prepare = setup
    original = unity._file_lock
    waiting, finished = threading.Event(), threading.Event()
    path = service._lock_path(project)

    @contextmanager
    def observed(path, cancel, timeout):
        waiting.set()
        try:
            with original(path, cancel, timeout):
                yield
        finally:
            finished.set()

    async def run():
        with original(path, threading.Event(), timeout=1):
            monkeypatch.setattr(unity, "_file_lock", observed)
            task = asyncio.create_task(service.call_tool(str(project), "bread_inspect_rendering"))
            assert await asyncio.to_thread(waiting.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert finished.is_set()
        with original(path, threading.Event(), timeout=1):
            pass

    asyncio.run(run())
    prepare.assert_not_called()
    client.call.assert_not_called()
