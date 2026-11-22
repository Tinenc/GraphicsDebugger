from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from src import internal_tool_service as service_module, server
from src.shader_live_edit import capture_bindings_match
from tests.test_shader_live_edit import _wait_until
from tests.test_shader_live_edit_capture_preflight import _CapturePreflightRig


def _binding(path):
    return {"instanceId": "qrenderdoc-test", "captureSessionId": "b" * 32,
            "capturePath": str(path)}


@pytest.mark.parametrize("left,right", [
    ("D:/Study/抓帧/白银之城/6.rdc", "d:/study/抓帧/白银之城/6.rdc"),
    ("/tmp/Capture.rdc", "/tmp/capture.rdc"),
])
def test_capture_path_case_uses_actual_host_os_rules(left, right):
    assert capture_bindings_match(_binding(left), _binding(right)) is (os.name == "nt")


def test_capture_paths_resolve_before_comparison(tmp_path):
    assert capture_bindings_match(_binding(tmp_path / "6.rdc"),
                                  _binding(tmp_path / "unused" / ".." / "6.rdc"))


@pytest.mark.parametrize("changed", [
    {"instanceId": "QRENDERDOC-TEST"},
    {"captureSessionId": "c" * 32},
    {"capturePath": "D:/Study/抓帧/白银之城/7.rdc"},
    {"capturePath": ""},
    {"capturePath": None},
])
def test_path_normalization_keeps_instance_session_and_actual_path_exact(changed):
    original = _binding("D:/Study/抓帧/白银之城/6.rdc")
    assert not capture_bindings_match(original, {**original, **changed})


def _request(rig, operation, binding):
    parameters = {"list": {}, "poll": {"live_edit_id": rig.live_id},
                  "start": {"workspace_path": str(rig.workspace), "debounce_ms": 100}}[operation]
    return json.dumps({"schema": service_module.SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                       "requestId": "f" * 32, "operation": operation,
                       "parameters": parameters, "binding": binding}).encode()


def _capture_read_calls(rig):
    return {name: tuple(getattr(rig, name).mock_calls)
            for name in ("workspace_status", "preflight", "recover_native")}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    value = _CapturePreflightRig(tmp_path, monkeypatch)
    started = value.start()
    value.live_id = started["liveEditId"]
    _wait_until(lambda: value.manager.poll(value.live_id)["state"] == "running")
    # Initial admission reads the workspace for catalog validation, then the
    # actual job checks whether a validated Native replacement can be recovered.
    # The operations under test must not repeat either phase or read the source
    # again. Capture their baseline rather than equating different call counts.
    value.preflight.assert_called_once()
    value.recover_native.assert_called_once()
    value.startup_calls = _capture_read_calls(value)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows path case identifies the same actual capture")
def test_windows_mcp_list_poll_and_join_keep_same_real_current_session(rig):
    original_binding = copy.deepcopy(rig.scope)
    original = rig.manager.poll(rig.live_id)
    rig.scope["capturePath"] = rig.scope["capturePath"].swapcase()
    assert rig.scope["capturePath"] != original_binding["capturePath"]
    for operation in ("list", "poll", "start"):
        code, receipt = rig.service.shader_live_edit_operation(_request(rig, operation, rig.scope))
        assert code == 200, receipt
        if operation == "list":
            sessions = receipt["result"]["sessions"]
            assert len(sessions) == 1
            actual = sessions[0]
        else:
            actual = receipt["result"]
        assert actual["liveEditId"] == rig.live_id
        assert actual["watchId"] == original["watchId"]
        assert actual["captureBinding"] == original_binding
        assert actual["state"] == "running" and actual["sessionState"] == "current"
        if operation == "start":
            assert actual["joinedExistingSession"] is True
    assert len(rig.watch.start_calls) == 1 and rig.watch.persistent_start_calls == []
    assert _capture_read_calls(rig) == rig.startup_calls
    assert rig.manager.poll(rig.live_id)["watch"]["compileAttempts"] == 0
    rig.apply.assert_not_called()


@pytest.mark.parametrize("field", ["instanceId", "captureSessionId", "capturePath"])
@pytest.mark.parametrize("operation", ["list", "poll", "start"])
def test_mcp_foreign_binding_is_hidden_or_rejected_without_join_or_replay(rig, monkeypatch, field, operation):
    original = rig.manager.poll(rig.live_id)
    different = copy.deepcopy(rig.scope)
    different[field] = {"instanceId": "other-gui", "captureSessionId": "2" * 32,
                        "capturePath": str(Path(rig.scope["capturePath"]).with_name("other.rdc"))}[field]
    # Only the GUI/context boundary changes. The real existing job, frozen
    # owner callback, registry, watcher and lifecycle guards remain intact.
    monkeypatch.setattr(server, "_active_live_context", lambda **_kwargs: {
        "capture": {"path": different["capturePath"], "captureSessionId": different["captureSessionId"]}})
    code, receipt = rig.service.shader_live_edit_operation(_request(rig, operation, different))
    if operation == "list":
        assert code == 200 and receipt["result"]["sessions"] == []
    else:
        assert code == 409 and "another GUI capture session" in receipt["error"]
    actual = rig.manager.poll(rig.live_id)
    assert actual["captureBinding"] == original["captureBinding"]
    assert actual["watchId"] == original["watchId"]
    assert len(rig.watch.start_calls) == 1 and rig.watch.persistent_start_calls == []
    assert _capture_read_calls(rig) == rig.startup_calls
    assert actual["watch"]["compileAttempts"] == 0
    rig.apply.assert_not_called()
