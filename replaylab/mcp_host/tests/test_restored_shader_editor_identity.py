"""Current capture checks without redundant replay/context work in restore."""
from __future__ import annotations

import copy
import contextvars
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from src import ipc_client, server, internal_tool_service
from src.ipc_client import bind_direct_tool_call
from tests.test_bridge_catalog_rebind import _load_bridge


def _context(path, session="b" * 32):
    return {"schema": "renderdoc-ultra-current-context/v1", "schemaVersion": 1,
            "identityOnly": True,
            "capture": {"loaded": True, "captureId": "a" * 64,
                        "captureSessionId": session, "path": str(path),
                        "api": "GraphicsAPI.D3D12"}}


@pytest.fixture
def owner(tmp_path, monkeypatch):
    root = tmp_path / "instances"
    directory = root / "restored-test"
    directory.mkdir(parents=True)
    path = tmp_path / "frame.rdc"
    context = _context(path)
    mailbox = {"instanceId": directory.name, "pid": os.getpid(),
               "captureLoaded": True, "captureSessionId": "b" * 32,
               "capturePath": str(path), "heartbeatUnixMs": int(time.time() * 1000)}

    def publish():
        (directory / "instance.json").write_text(json.dumps(mailbox), encoding="utf-8")

    publish()
    monkeypatch.setattr(ipc_client, "INSTANCE_ROOT", str(root))
    client = ipc_client.IPCClient(instance_id=directory.name)
    client.call = mock.Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(context))
    monkeypatch.setattr(server, "ipc", client)
    return SimpleNamespace(path=path, context=context, mailbox=mailbox,
                           publish=publish, client=client, instance=directory.name)


@pytest.mark.parametrize("batch_name", ["_restored_shader_editor_batch", "_project_shader_restore_batch"])
def test_restore_batch_reuses_observed_context_but_each_read_checks_mailbox(owner, batch_name):
    with bind_direct_tool_call(owner.instance, "b" * 32):
        with getattr(server, batch_name)(owner.path, "b" * 32):
            first = server._active_live_context()
            first["capture"]["api"] = "changed caller copy"
            assert server._active_live_context()["capture"]["api"] == "GraphicsAPI.D3D12"
            server._active_live_context()
        server._active_live_context()
    # One admission RPC, zero identity RPCs in the batch, normal RPC after exit.
    assert owner.client.call.call_count == 2
    assert server._RESTORED_SHADER_EDITOR_CONTEXT.get() is None


@pytest.mark.parametrize("mutation", ["session", "path", "closed", "pid", "removed"])
@pytest.mark.parametrize("batch_name", ["_restored_shader_editor_batch", "_project_shader_restore_batch"])
def test_restore_batch_rejects_real_owner_changes_before_cached_identity(owner, mutation, batch_name):
    with bind_direct_tool_call(owner.instance, "b" * 32):
        with getattr(server, batch_name)(owner.path, "b" * 32):
            if mutation == "session":
                owner.mailbox["captureSessionId"] = "c" * 32
            elif mutation == "path":
                owner.mailbox["capturePath"] = str(owner.path.with_name("other.rdc"))
            elif mutation == "closed":
                owner.mailbox["captureLoaded"] = False
            elif mutation == "pid":
                owner.mailbox["pid"] += 1
            else:
                root = owner.path.parent / "instances" / owner.instance
                (root / "instance.json").unlink()
            if mutation != "removed":
                owner.publish()
            with pytest.raises(RuntimeError, match="capture changed or is unavailable"):
                server._active_live_context()
    assert owner.client.call.call_count == 1


def test_event_state_and_other_threads_never_use_restore_identity_cache(owner):
    def rpc(method, params, **kwargs):
        result = copy.deepcopy(owner.context)
        if not params:
            result.update(identityOnly=False, events={"currentEventId": 17})
        return result
    owner.client.call.side_effect = rpc
    observed = []
    with bind_direct_tool_call(owner.instance, "b" * 32):
        with server._restored_shader_editor_batch(owner.path, "b" * 32):
            assert server._active_live_context(require_event_state=True)["events"]["currentEventId"] == 17
            thread = threading.Thread(target=lambda: observed.append(server._active_live_context()))
            thread.start()
            thread.join(2)
            assert not thread.is_alive()
    assert len(observed) == 1 and owner.client.call.call_count == 3


def test_copied_context_cannot_keep_batch_identity_after_its_owner_returns(owner):
    with bind_direct_tool_call(owner.instance, "b" * 32):
        with server._restored_shader_editor_batch(owner.path, "b" * 32):
            retained = contextvars.copy_context()
            assert retained.run(server._active_live_context)["capture"]["loaded"]
        with pytest.raises(RuntimeError, match="capture changed or is unavailable"):
            retained.run(server._active_live_context)
    assert owner.client.call.call_count == 1


def test_restore_batch_remote_operations_reuse_binding_without_local_context_rpc(owner):
    from src import shader_live_edit_client
    with bind_direct_tool_call(owner.instance, "b" * 32):
        with server._restored_shader_editor_batch(owner.path, "b" * 32):
            with mock.patch.object(shader_live_edit_client, "request", return_value={}) as request:
                for operation in ("list", "start", "poll"):
                    server._shader_live_edit_remote(operation, {})
    assert owner.client.call.call_count == 1
    assert request.call_count == 3
    assert all(call.args[2] == {"instanceId": owner.instance, "captureSessionId": "b" * 32,
                              "capturePath": str(owner.path)} for call in request.call_args_list)


def _bridge_identity():
    bridge = _load_bridge()
    bridge._capture_session_id = "b" * 32
    bridge._ctx = SimpleNamespace(IsCaptureLoaded=lambda: True,
        GetCaptureFilename=lambda: "D:/capture.rdc",
        APIProps=lambda: SimpleNamespace(pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12"))
    bridge._known_catalog_capture_ids = {"a" * 64: "D:/capture.rdc"}
    bridge._known_catalog_identity_pairs = {("a" * 64, "c" * 64)}
    bridge._known_catalog_stable_pairs = {("d" * 64, "e" * 64)}
    bridge._invoke_on_ui_sync = mock.Mock(side_effect=lambda fn: fn())
    bridge._replay = mock.Mock(side_effect=AssertionError("identity must not enter replay"))
    return bridge


def test_catalog_identity_uses_real_ui_capture_and_admitted_sets_without_replay():
    bridge = _bridge_identity()
    result = bridge._get_capture_status({"identity_only": True})
    assert result["identityOnly"] is True
    assert result["captureSessionId"] == "b" * 32 and result["api"] == "GraphicsAPI.D3D12"
    assert result["capturePath"] == "D:/capture.rdc"
    assert result["knownCatalogIdentityPairs"] == [{"captureId": "a" * 64, "identityEvidenceSha256": "c" * 64}]
    assert result["knownCatalogStablePairs"] == [{"stableCaptureId": "d" * 64, "stableIdentityEvidenceSha256": "e" * 64}]
    assert "frameNumber" not in result and "draws" not in result
    bridge._invoke_on_ui_sync.assert_called_once()
    bridge._replay.assert_not_called()


def test_catalog_identity_unloaded_and_corrupt_admitted_state_are_real_failures():
    bridge = _bridge_identity()
    bridge._ctx.IsCaptureLoaded = lambda: False
    assert bridge._get_capture_status({"identity_only": True}) == {"loaded": False, "identityOnly": True}
    bridge._ctx.IsCaptureLoaded = lambda: True
    bridge._known_catalog_identity_pairs = {("invalid", "c" * 64)}
    with pytest.raises(RuntimeError, match="state is invalid"):
        bridge._get_capture_status({"identity_only": True})
    bridge._replay.assert_not_called()


@pytest.mark.parametrize("params", [{}, {"identity_only": False}, {"identity_only": "true"}])
def test_full_capture_status_retains_real_replay_frame_statistics(params):
    bridge = _bridge_identity()
    controller = SimpleNamespace(GetAPIProperties=bridge._ctx.APIProps,
        GetFrameInfo=lambda: SimpleNamespace(frameNumber=9,
            stats=SimpleNamespace(recorded=SimpleNamespace(draws=11, dispatches=2, calls=40))))
    bridge._replay.side_effect = lambda fn: fn(controller)
    result = bridge._get_capture_status(params)
    assert result["frameNumber"] == 9 and result["draws"] == 11
    bridge._replay.assert_called_once()
    bridge._invoke_on_ui_sync.assert_not_called()
