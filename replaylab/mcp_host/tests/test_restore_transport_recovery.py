"""Identity recovery uses real project snapshots and one fixed IPC owner."""

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from src import ipc_client, project_restore
from src.active_projects import ActiveReplayProjectService


@pytest.fixture
def restore_context(tmp_path, monkeypatch):
    service = ActiveReplayProjectService(tmp_path / "state.json", tmp_path / "projects")
    first = service.create_project(name="First", game_id="game", build_id="build", api_family="d3d11")
    second = service.create_project(name="Second", game_id="other", build_id="build", api_family="d3d11")
    service.activate(first["projectRoot"])
    active = service.status()
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"test capture")
    session = "a" * 32
    instance_root = tmp_path / "instances"
    owner = instance_root / "owner"
    owner.mkdir(parents=True)
    (owner / "instance.json").write_text(json.dumps({
        "instanceId": "owner", "pid": os.getpid(), "heartbeatUnixMs": int(time.time() * 1000),
        "captureLoaded": True, "stopped": False,
    }), encoding="utf-8")
    monkeypatch.setattr(ipc_client, "INSTANCE_ROOT", str(instance_root))
    client = ipc_client.IPCClient(instance_id="owner")
    identity = {"capture": {"loaded": True, "captureSessionId": session,
                            "path": str(capture), "api": "D3D11"}}
    host = SimpleNamespace(ipc=client, _active_project_service=lambda: service,
                           _active_live_context=mock.Mock(return_value=identity))
    monkeypatch.setattr(project_restore._RESTORE_THREAD_STATE, "control_checkpoint", None, raising=False)
    monkeypatch.setattr(project_restore._RESTORE_THREAD_STATE, "context_recovery", [], raising=False)
    return SimpleNamespace(service=service, active=active, project=Path(first["projectRoot"]),
                           other=second, capture=capture, session=session, client=client,
                           host=host, identity=identity)


def require(state):
    return project_restore._require_restore_context(
        state.host, state.active, state.project, state.capture, state.session)


@pytest.mark.parametrize("failure", [ipc_client.IPCBridgeBusyError("busy"),
                                   ipc_client.IPCBridgeTimeoutError("Timeout (60s)"),
                                   OSError("identity response unreadable")])
def test_transient_identity_read_retries_once_and_revalidates_real_snapshot(restore_context, failure):
    state = restore_context
    state.host._active_live_context.side_effect = [failure, state.identity]
    with state.service.snapshot_active_project(state.active["workspaceId"], state.project), \
            state.client.bind_capture_session(state.session):
        assert require(state)["workspaceId"] == state.active["workspaceId"]
    assert state.host._active_live_context.call_count == 2
    assert project_restore._RESTORE_THREAD_STATE.context_recovery == [{
        "instanceId": "owner", "errorType": type(failure).__name__,
        "error": str(failure), "recovered": True,
    }]
    assert ipc_client._CALL_BINDING.get() is None


def test_repeated_transport_failure_is_unavailable_without_claiming_a_change(restore_context):
    state = restore_context
    state.host._active_live_context.side_effect = ipc_client.IPCBridgeTimeoutError("Timeout (60s)")
    with state.client.bind_capture_session(state.session), \
            pytest.raises(project_restore.RestoreContextUnavailable, match="IPCBridgeTimeoutError.*Timeout") as failure:
        require(state)
    assert state.host._active_live_context.call_count == 2
    assert isinstance(failure.value.__cause__, ipc_client.IPCBridgeTimeoutError)
    assert not isinstance(failure.value, project_restore.RestoreContextChanged)


def test_unbound_transport_failure_has_no_safe_owner_for_retry(restore_context):
    state = restore_context
    state.host._active_live_context.side_effect = ipc_client.IPCBridgeTimeoutError("missing reply")
    with pytest.raises(project_restore.RestoreContextUnavailable):
        require(state)
    assert state.host._active_live_context.call_count == 1


def test_dead_bound_owner_does_not_retry_or_rebind(restore_context):
    state = restore_context

    def unavailable(**unused):
        (Path(ipc_client.INSTANCE_ROOT) / "owner" / "instance.json").unlink()
        raise ipc_client.IPCBridgeTimeoutError("owner no longer answers")

    state.host._active_live_context.side_effect = unavailable
    with state.client.bind_capture_session(state.session), pytest.raises(project_restore.RestoreContextUnavailable):
        require(state)
    assert state.host._active_live_context.call_count == 1
    assert state.client.instance_id == "owner"


@pytest.mark.parametrize("field,value", [("captureSessionId", "b" * 32),
                                         ("path", "other.rdc"), ("api", "Vulkan")])
def test_read_after_timeout_still_rejects_real_capture_changes(restore_context, field, value):
    state = restore_context
    changed = {"capture": {**state.identity["capture"], field: value}}
    state.host._active_live_context.side_effect = [ipc_client.IPCBridgeTimeoutError("busy"), changed]
    with state.client.bind_capture_session(state.session), \
            pytest.raises(project_restore.RestoreContextChanged, match="session, path or API changed"):
        require(state)
    assert state.host._active_live_context.call_count == 2


def test_project_switch_during_timeout_prevents_second_identity_request(restore_context):
    state = restore_context

    def changed(**unused):
        state.service.activate(state.other["projectRoot"])
        raise ipc_client.IPCBridgeTimeoutError("busy")

    state.host._active_live_context.side_effect = changed
    with pytest.raises(project_restore.RestoreContextChanged, match="selection or configuration changed"):
        with state.service.snapshot_active_project(state.active["workspaceId"], state.project), \
                state.client.bind_capture_session(state.session):
            require(state)
    assert state.host._active_live_context.call_count == 1


@pytest.mark.parametrize("stop", [project_restore.RestoreCancelled("cancelled"),
                                 project_restore.RestoreDeadlineExceeded("deadline")])
def test_cancel_or_deadline_checkpoint_prevents_retry(restore_context, monkeypatch, stop):
    state = restore_context
    state.host._active_live_context.side_effect = ipc_client.IPCBridgeTimeoutError("busy")

    def checkpoint(phase):
        if phase == "before restore identity retry":
            raise stop

    monkeypatch.setattr(project_restore._RESTORE_THREAD_STATE, "control_checkpoint", checkpoint)
    with state.client.bind_capture_session(state.session), pytest.raises(type(stop)):
        require(state)
    assert state.host._active_live_context.call_count == 1


@pytest.mark.parametrize("message", [
    "The frozen capture session changed before the Bread tool started",
    "The RenderDoc capture changed before the background tool started",
])
def test_readable_frozen_session_rejection_remains_context_changed(restore_context, message):
    state = restore_context
    state.host._active_live_context.side_effect = ipc_client.IPCBridgeResponseError(-32000, message)
    with state.client.bind_capture_session(state.session), pytest.raises(project_restore.RestoreContextChanged):
        require(state)
    assert state.host._active_live_context.call_count == 1


def test_unreadable_identity_is_unavailable_and_not_retried(restore_context):
    state = restore_context
    state.host._active_live_context.side_effect = RuntimeError("unsupported current-context schema")
    with state.client.bind_capture_session(state.session), pytest.raises(project_restore.RestoreContextUnavailable):
        require(state)
    assert state.host._active_live_context.call_count == 1


def test_observed_capture_unload_is_context_changed_without_retry(restore_context):
    state = restore_context
    state.host._active_live_context.side_effect = RuntimeError("No RenderDoc capture is loaded")
    with state.client.bind_capture_session(state.session), pytest.raises(project_restore.RestoreContextChanged):
        require(state)
    assert state.host._active_live_context.call_count == 1


def test_missing_frozen_owner_fails_restore_admission_as_unavailable_without_reading_other_window(restore_context):
    state = restore_context
    owner_manifest = Path(ipc_client.INSTANCE_ROOT) / "owner" / "instance.json"
    other_manifest = json.loads(owner_manifest.read_text(encoding="utf-8"))
    other_manifest.update(instanceId="other-window", captureSessionId="b" * 32)
    other = Path(ipc_client.INSTANCE_ROOT) / "other-window"
    other.mkdir()
    (other / "instance.json").write_text(json.dumps(other_manifest), encoding="utf-8")
    owner_manifest.write_text("{unreadable", encoding="utf-8")
    state.host.ipc = ipc_client.IPCClient()
    with mock.patch.object(project_restore, "_host_module", return_value=state.host):
        result = project_restore.restore_active_capture_project(state.project, state.capture, state.session)
    assert result["completed"] is False
    assert result["errorType"] == "IPCBridgeUnavailableError"
    assert "owner is unavailable" in result["error"]
    assert "RestoreContextChanged" not in result["error"]
    state.host._active_live_context.assert_not_called()
    assert state.host.ipc.instance_id == ""
    assert ipc_client._CALL_BINDING.get() is None


@pytest.mark.parametrize("failed", [True, False])
def test_worker_nested_outcome_remains_readable_in_http_result_and_persisted_artifact(restore_context, failed):
    from src import internal_tool_service
    state = restore_context
    result = {"completed": not failed}
    if failed:
        result.update(error="RestoreContextUnavailable: IPCBridgeTimeoutError: Timeout (60s)",
                      errorType="RestoreContextUnavailable")
    registry = internal_tool_service.ProjectRestoreJobRegistry(runner=lambda *args, **kwargs: result)
    try:
        submitted, unused = registry.submit({"projectRoot": str(state.project),
                                            "capturePath": str(state.capture),
                                            "captureSessionId": state.session})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            code, snapshot = internal_tool_service.project_restore_get_response(submitted["jobId"], 0, registry)
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        assert code == 200
        # A successfully delivered nested failure intentionally has no outer
        # worker error. Both consumers must still see the real restore outcome.
        assert snapshot["status"] == "completed"
        assert "error" not in snapshot
        assert snapshot["result"] == result
        path = internal_tool_service._restore_result_record_path(str(state.project), str(state.capture))
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["schema"] == "replaylab-project-restore-result/v1"
        assert persisted["status"] == ("failed" if failed else "finished")
        assert persisted["error"] == result.get("error")
        if failed:
            assert persisted["errorType"] == result["errorType"]
    finally:
        registry.shutdown()
