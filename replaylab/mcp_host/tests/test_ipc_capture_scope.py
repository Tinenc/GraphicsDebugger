"""Capture switching cannot retarget an old Agent Host's nested IPC reads."""
from __future__ import annotations

import json

import pytest

from src.ipc_client import IPCClient, bind_direct_tool_call


SESSION_A, SESSION_B = "a" * 32, "b" * 32


def client(monkeypatch):
    monkeypatch.setenv("REPLAYLAB_TOOL_SCOPE", json.dumps({
        "capture_session_id": SESSION_A, "capture_path": "D:/captures/a.rdc"}))
    return IPCClient(instance_id="same-window")


def test_all_nested_calls_pin_session_and_reject_implicit_ui_switch(monkeypatch):
    ipc = client(monkeypatch)
    active, calls = [SESSION_A], []
    def dispatch(method, params, *args, **kwargs):
        calls.append((method, dict(params)))
        if params["__replaylab_expected_capture_session_id"] != active[0]:
            raise RuntimeError("The frozen capture session changed before the Bread tool started")
        return {"captureSessionId": active[0]}
    monkeypatch.setattr(ipc, "_call_locked", dispatch)
    ipc.call("get_current_renderdoc_context", {})
    active[0] = SESSION_B
    # Even the identity read cannot quietly adopt B after the UI switched.
    with pytest.raises(RuntimeError, match="frozen capture session changed"):
        ipc.call("get_current_renderdoc_context", {"identity_only": True})
    with pytest.raises(RuntimeError, match="frozen capture session changed"):
        ipc.call("get_frame_render_flow_outline", {})
    assert ipc.capture_session_id == SESSION_A
    assert all(params["__replaylab_expected_capture_session_id"] == SESSION_A
               for _, params in calls)


def test_model_parameters_cannot_rebind_capture(monkeypatch):
    ipc = client(monkeypatch)
    calls = []
    monkeypatch.setattr(ipc, "_call_locked", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="change its frozen capture"):
        ipc.call("get_pipeline_state", {"__replaylab_expected_capture_session_id": SESSION_B})
    assert not calls


def test_explicit_open_receipt_rebinds_followup_calls_only_to_requested_file(monkeypatch):
    ipc = client(monkeypatch)
    calls = []
    def dispatch(method, params, *args, **kwargs):
        calls.append(dict(params))
        return ({"opened": "D:/captures/b.rdc", "captureSessionId": SESSION_B}
                if method == "open_capture" else {"captureSessionId": SESSION_B})
    monkeypatch.setattr(ipc, "_call_locked", dispatch)
    ipc.call("open_capture", {"capture_path": "D:/captures/b.rdc"})
    ipc.call("get_frame_render_flow_outline", {})
    assert calls[0]["__replaylab_expected_capture_session_id"] == SESSION_A
    assert calls[1]["__replaylab_expected_capture_session_id"] == SESSION_B


@pytest.mark.parametrize("receipt", [
    {"opened": "D:/captures/unrequested.rdc", "captureSessionId": SESSION_B},
    {"error": "failed", "opened": "D:/captures/b.rdc", "captureSessionId": SESSION_B},
    {"captureSessionId": SESSION_B},
])
def test_unrelated_open_or_context_receipt_cannot_rebind(monkeypatch, receipt):
    ipc = client(monkeypatch)
    monkeypatch.setattr(ipc, "_call_locked", lambda *args, **kwargs: receipt)
    ipc.call("open_capture", {"capture_path": "D:/captures/b.rdc"})
    ipc.call("get_current_renderdoc_context", {})
    assert ipc.capture_session_id == SESSION_A


def recovery(**updates):
    return {"schema": "bread-replay-recovery/v1", "status": "recovered",
            "recovered": True, "previousCaptureSessionId": SESSION_A,
            "captureSessionId": SESSION_B, **updates}


@pytest.mark.parametrize("updates", [{}, {"recovered": False}, {"status": "failed"},
                                     {"previousCaptureSessionId": "c" * 32}])
def test_only_verified_recovery_lineage_can_advance_session(monkeypatch, updates):
    ipc = client(monkeypatch)
    calls = []
    def dispatch(method, params, *args, **kwargs):
        calls.append(dict(params))
        return recovery(**updates)
    monkeypatch.setattr(ipc, "_call_locked", dispatch)
    ipc.call("recover_replay", {"expected_capture_session_id": SESSION_A})
    ipc.call("inspect_frame_render_stage", {"event_id": 31})
    expected = SESSION_A if updates else SESSION_B
    assert ipc.capture_session_id == expected
    assert calls[-1]["__replaylab_expected_capture_session_id"] == expected


def test_direct_job_owns_binding_without_rebinding_task_process(monkeypatch):
    ipc = client(monkeypatch)
    calls = []
    monkeypatch.setattr(ipc, "_call_locked", lambda method, params, *args, **kwargs:
                       calls.append(dict(params)) or recovery(previousCaptureSessionId=SESSION_B,
                                                              captureSessionId="c" * 32))
    with bind_direct_tool_call("direct-owner", SESSION_B):
        ipc.call("recover_replay", {})
    ipc.call("get_frame_render_flow_outline", {})
    assert calls[0]["__replaylab_expected_capture_session_id"] == SESSION_B
    assert calls[1]["__replaylab_expected_capture_session_id"] == SESSION_A
    assert ipc.capture_session_id == SESSION_A


@pytest.mark.parametrize("scope", ["not-json", "[]", '{"capture_session_id": 32}',
                                  '{"capture_session_id": "invalid"}'])
def test_malformed_task_scope_cannot_silently_become_unscoped(monkeypatch, scope):
    monkeypatch.setenv("REPLAYLAB_TOOL_SCOPE", scope)
    with pytest.raises(ValueError, match="scope|session"):
        IPCClient(instance_id="same-window")
