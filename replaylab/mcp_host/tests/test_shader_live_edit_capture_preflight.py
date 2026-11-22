from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src import internal_tool_service as service_module, ipc_client, server
from src.ipc_client import _CALL_BINDING, bind_direct_tool_call
from src.shader_live_edit import ShaderLiveEditManager
from src.shader_workspace.catalog import ShaderKey, capture_identity
from tests.test_shader_live_edit import _wait_until
from tests.test_shader_live_edit_lifecycle import _PersistentWatchManager


class _CapturePreflightRig:
    """Real consent, mailbox, binding and job code; replay is the only boundary."""

    def __init__(self, tmp_path, monkeypatch):
        # This rig exercises the retained legacy resume/preflight contract.
        # Default on-demand scheduling has its own no-scan/no-resume tests.
        monkeypatch.setattr(server, "_shader_editors_on_demand", False)
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.registry_path = tmp_path / "registry.json"
        self.mailboxes = tmp_path / "instances"
        self.mailboxes.mkdir()
        self.scope = {
            "instanceId": "preflight-gui",
            "captureSessionId": "1" * 32,
            "capturePath": str(tmp_path / "frame.rdc"),
        }
        self.status = {
            "workspaceId": "a" * 32,
            "workspacePath": str(self.workspace),
            "stage": "fragment",
            "entryPoint": "MainPS",
            "capture": {
                "capturePath": self.scope["capturePath"],
                "api": "GraphicsAPI.D3D12",
                "catalogPath": str(tmp_path / "catalog.json"),
                "catalogSha256": "d" * 64,
            },
            "shader": {"resourceId": 853, "rawSha256": "c" * 64},
            # An edited source is legitimate input to a new watcher. The
            # catalog preflight must bind the captured raw Shader instead.
            "source": {"present": True, "lane": "executable-editable",
                       "rawSha256": "e" * 64, "path": str(self.workspace / "shader.hlsl")},
            "representation": {"lane": "executable-editable", "compileEligible": True},
        }
        self.set_resource(853)
        self.publish()
        self.watch = _PersistentWatchManager()
        self.workspace_status = mock.Mock(side_effect=lambda _path: copy.deepcopy(self.status))
        self.preflight = mock.Mock(side_effect=self.verified_context)
        self.recover_native = mock.Mock(wraps=server._shader_live_edit_recover_active_candidate)
        self.apply = mock.Mock(side_effect=AssertionError("offline preflight must not apply"))
        monkeypatch.setattr(ipc_client, "INSTANCE_ROOT", str(self.mailboxes))
        monkeypatch.setattr(service_module, "_SHADER_LIVE_EDIT_BINDINGS", {})
        monkeypatch.setattr(server, "_shader_workspace_service",
                            lambda: SimpleNamespace(status=self.workspace_status))
        monkeypatch.setattr(server, "_workspace_active_capture_context", self.preflight)
        monkeypatch.setattr(server, "_shader_live_edit_apply_candidate", self.apply)
        monkeypatch.setattr(server, "_shader_live_edit_recover_active_candidate", self.recover_native)
        monkeypatch.setattr(server, "_active_live_context", lambda **_kwargs: self.context())
        self.manager = ShaderLiveEditManager(
            self.watch, self.apply, registry_path=self.registry_path,
            workspace_identity=service_module._bound_shader_workspace_identity,
            bind_session=service_module._bound_shader_session,
            poll_seconds=0.005, join_seconds=0.2,
        )
        self.service = service_module.InternalToolService(
            project=object(), restore_jobs=object(), shader_manager=self.manager)

    def catalog_id(self, resource_id):
        return capture_identity(self.status["capture"], [
            ShaderKey(resource_id, "fragment", "MainPS", self.status["shader"]["rawSha256"]),
        ])

    def set_resource(self, resource_id):
        self.status["shader"]["resourceId"] = resource_id
        self.status["shader"]["shaderKey"] = "%d:fragment:MainPS:%s" % (
            resource_id, self.status["shader"]["rawSha256"])
        self.status["capture"]["captureId"] = self.catalog_id(resource_id)

    def publish(self, *, session=None):
        if session is not None:
            self.scope["captureSessionId"] = session
        directory = self.mailboxes / self.scope["instanceId"]
        directory.mkdir(exist_ok=True)
        value = {**self.scope, "pid": os.getpid(), "captureLoaded": True,
                 "heartbeatUnixMs": int(time.time() * 1000)}
        (directory / "instance.json").write_text(json.dumps(value), encoding="utf-8")

    def context(self):
        return {"capture": {
            "captureId": self.status["capture"]["captureId"],
            "captureSessionId": self.scope["captureSessionId"],
            "path": self.scope["capturePath"],
            "api": self.status["capture"]["api"],
        }}

    def verified_context(self, workspace_path, *, status):
        assert Path(workspace_path) == self.workspace
        assert status == self.status
        assert _CALL_BINDING.get() == (
            self.scope["instanceId"], self.scope["captureSessionId"])
        return self.context()

    def authorize_history(self):
        # Seed through the real registry API, never hand-write a permissive
        # registry schema or bypass the production identity projection.
        identity = server._shader_live_edit_workspace_identity(
            str(self.workspace), status=copy.deepcopy(self.status))
        return self.manager._registry.authorize(self.workspace, identity, 100)

    def start(self):
        with bind_direct_tool_call(self.scope["instanceId"], self.scope["captureSessionId"]):
            return self.manager.start(self.workspace, 100)

    def refresh(self):
        self.publish()
        return self.service.refresh_shader_sessions([os.getpid()])

    def poll_public(self, live_id):
        value = {"schema": service_module.SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                 "requestId": "f" * 32, "operation": "poll",
                 "parameters": {"live_edit_id": live_id}, "binding": self.scope}
        return self.service.shader_live_edit_operation(json.dumps(value).encode())

    def assert_no_authority(self):
        assert self.watch.start_calls == []
        assert self.watch.persistent_start_calls == []
        assert self.manager.list()["sessions"] == []
        assert service_module._SHADER_LIVE_EDIT_BINDINGS == {}
        self.apply.assert_not_called()

    def close(self):
        self.manager.shutdown()
        for job in self.manager._jobs.values():
            job._thread.join(2.0)
            job._apply_thread.join(2.0)
            assert not job._thread.is_alive()
            assert not job._apply_thread.is_alive()
        self.apply.assert_not_called()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    value = _CapturePreflightRig(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("operation", ["start", "resume"])
@pytest.mark.parametrize("failure", ["catalog-rejected", "changed-resource-id"])
def test_catalog_mismatch_rejects_before_watch_and_preserves_existing_consent(rig, operation, failure):
    authorization = rig.authorize_history()
    registry_before = rig.registry_path.read_bytes()
    if failure == "catalog-rejected":
        rig.preflight.side_effect = RuntimeError("Live Shader catalog did not match expected_capture_id")
    else:
        # Same raw bytes and entry point, but the replay allocated another
        # resource. Use the real catalog hash algorithm to model that change.
        changed = rig.context()
        changed["capture"]["captureId"] = rig.catalog_id(860)
        assert changed["capture"]["captureId"] != rig.status["capture"]["captureId"]
        rig.preflight.side_effect = None
        rig.preflight.return_value = changed
    if operation == "start":
        with pytest.raises(RuntimeError, match="catalog"):
            rig.start()
    else:
        refreshed = rig.refresh()
        result = refreshed["results"][0]["result"]["results"][0]
        assert result["resumed"] is False and "catalog" in result["message"]
        for _ in range(3):
            rig.refresh()
    rig.assert_no_authority()
    assert rig.registry_path.read_bytes() == registry_before
    assert rig.manager.list()["authorizations"] == [authorization]
    assert rig.workspace_status.call_count == rig.preflight.call_count == 1
    assert _CALL_BINDING.get() is None


@pytest.mark.parametrize("operation", ["start", "resume"])
def test_current_physical_source_passes_once_without_preflight_on_owner_ticks_or_poll(rig, operation):
    # This is the new workspace bound to the currently live physical RID,
    # rather than an old authorization silently moved to that RID.
    rig.set_resource(860)
    authorization = rig.authorize_history() if operation == "resume" else None
    if operation == "resume":
        result = rig.refresh()["results"][0]["result"]["results"][0]
        assert result["resumed"] is True
        live_id = result["liveEditId"]
    else:
        live_id = rig.start()["liveEditId"]
    _wait_until(lambda: rig.manager.poll(live_id)["state"] == "running")
    # Admission validates the live catalog once. Starting the actual job also
    # reads current workspace status once to decide whether an existing Native
    # mapping can be recovered; this uncompiled fixture returns without IPC.
    status_calls_after_start = rig.workspace_status.call_count
    assert status_calls_after_start == 2
    assert rig.preflight.call_count == rig.recover_native.call_count == 1
    for _ in range(3):
        rig.refresh()
        code, value = rig.poll_public(live_id)
        assert code == 200 and value["result"]["state"] == "running"
        assert rig.workspace_status.call_count == status_calls_after_start
        assert rig.preflight.call_count == rig.recover_native.call_count == 1
    assert len(rig.watch.start_calls) == 1
    assert len(rig.watch.persistent_start_calls) == (operation == "resume")
    state = rig.manager.poll(live_id)
    assert state["captureBinding"] == rig.scope
    assert state["activeReplacement"] is None
    entries = rig.manager.list()["authorizations"]
    assert len(entries) == 1 and entries[0]["workspaceIdentity"]["resourceId"] == 860
    if authorization:
        assert entries[0]["authorizationId"] == authorization["authorizationId"]
        assert entries[0]["authorizedAt"] == authorization["authorizedAt"]


@pytest.mark.parametrize("field,value", [
    ("captureSessionId", "2" * 32),
    ("path", "C:/offline-only/other.rdc"),
    ("captureId", "f" * 64),
])
def test_returned_context_must_match_frozen_scope_before_new_consent_is_written(rig, field, value):
    changed = rig.context()
    changed["capture"][field] = value
    rig.preflight.side_effect = None
    rig.preflight.return_value = changed
    with pytest.raises(RuntimeError, match="changed during catalog"):
        rig.start()
    rig.assert_no_authority()
    assert rig.manager.list()["authorizations"] == []
    assert not rig.registry_path.exists()


def test_capture_change_during_slow_preflight_cannot_publish_old_binding_or_watch(rig):
    authorization = rig.authorize_history()
    registry_before = rig.registry_path.read_bytes()
    entered = threading.Event()
    release = threading.Event()
    errors = []
    old_context = rig.context()

    def slow_preflight(workspace_path, *, status):
        rig.verified_context(workspace_path, status=status)
        entered.set()
        if not release.wait(3.0):
            raise AssertionError("test did not release catalog preflight")
        return old_context

    def start():
        try:
            rig.start()
        except Exception as exc:
            errors.append(exc)

    rig.preflight.side_effect = slow_preflight
    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(2.0)
        rig.assert_no_authority()
        rig.publish(session="2" * 32)
    finally:
        release.set()
        worker.join(3.0)
    assert not worker.is_alive()
    assert len(errors) == 1 and "changed during catalog" in str(errors[0])
    rig.assert_no_authority()
    assert rig.registry_path.read_bytes() == registry_before
    assert rig.manager.list()["authorizations"] == [authorization]
    assert rig.workspace_status.call_count == rig.preflight.call_count == 1
