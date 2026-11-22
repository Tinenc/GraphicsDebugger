"""Exercise the real bridge recovery hooks and lifecycle without native replay."""

import ast
from copy import deepcopy
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from test_bridge_composite_readonly import load_bridge
from test_replay_recovery import recovery


@pytest.fixture
def integrated_recovery(tmp_path, monkeypatch):
    bridge = load_bridge()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setitem(sys.modules, bridge.__name__ + ".replay_recovery", recovery)
    # HAS_UI is deliberately false in load_bridge. Compile the actual viewer
    # class alone against a harmless base so the production callbacks run here.
    syntax = ast.parse(Path(bridge.__file__).read_text(encoding="utf-8"))
    viewer = next(node for node in ast.walk(syntax)
                  if isinstance(node, ast.ClassDef) and node.name == "CaptureLifecycleViewer")
    bridge.qrd = SimpleNamespace(CaptureViewer=object)
    exec(compile(ast.Module(body=[viewer], type_ignores=[]), str(bridge.__file__), "exec"), bridge.__dict__)
    calls = []
    path = tmp_path / "same.rdc"
    path.write_bytes(b"original rdc bytes")
    cancel_event = threading.Event()
    properties = {}
    window = SimpleNamespace(setProperty=lambda name, value: properties.__setitem__(name, value))
    panel_documents = {"draft": {"sourcePath": str(tmp_path / "shader.hlsl"), "dirty": True,
                                  "editor": SimpleNamespace(text="unsaved readable HLSL"),
                                  "appliedReplacementId": "replacement-1"}}
    models = {"draft": {"liveEdit": {"lastApply": {"replacement": {"replacementId": "replacement-1"}}}}}

    def panel_capture_changed(binding):
        calls.append(("workspace-binding", binding))
        # Like the real panel, only availability changes. Its dirty editor
        # contents survive; former replay receipts must already be invalidated.
        assert not panel_documents["draft"].get("appliedReplacementId")
        assert "lastApply" not in models["draft"]["liveEdit"]
        panel_documents["draft"]["available"] = False

    bridge._shader_workspace_ui = SimpleNamespace(_documents=panel_documents, _workspaces=models,
                                                  capture_changed=panel_capture_changed)
    bridge._shader_console_ui = None
    saved = {"document": {"captureSessionId": "old", "stages": [{"images": ["saved-rgb", "saved-a"]}]}}
    bridge._frame_render_flow_saved = saved
    bridge._frame_render_flow_outline_cache = {"captureSessionId": "old"}
    loader = SimpleNamespace(close=lambda: calls.append(("loader-close",)))
    bridge._frame_render_flow_ui = SimpleNamespace(document=saved["document"],
        _channel_preview_loader=loader, view=SimpleNamespace(_channel_cache={"old": ["cached"]}))
    bridge._shared_shader_replacements = {"replacement-1": {"sourcePath": panel_documents["draft"]["sourcePath"]}}
    bridge._event_shader_overrides = {"event-replacement": {"sourcePath": "other.hlsl"}}
    bridge._capture_session_id = "old"
    bridge._active_capture_token = None
    bridge._replay_recovery_path = ""
    bridge._replay_recovery_coordinator = None
    bridge.rd.ResultCode = SimpleNamespace(Succeeded="Succeeded")
    health = {"code": "GPUTimeout"}

    class Context:
        loaded = True
        filename = str(path)
        event = 42
        selected = 41
        load_hook = None
        load_failure = None
        restore_hook = None

        def IsCaptureLoaded(self): return self.loaded
        def IsCaptureLocal(self): return True
        def IsCaptureTemporary(self): return False
        def GetCaptureFilename(self): return self.filename
        def CurEvent(self): return self.event
        def CurSelectedEvent(self): return self.selected
        def GetMainWindow(self): return SimpleNamespace(Widget=lambda: window)
        def Config(self): return SimpleNamespace(DefaultReplayOptions="configured-options")
        def Replay(self):
            ctrl = SimpleNamespace(GetFatalErrorStatus=lambda: SimpleNamespace(code=health["code"]))
            return SimpleNamespace(BlockInvoke=lambda callback: callback(ctrl))

        def LoadCapture(self, *args):
            raise AssertionError("Automatic recovery must not use the modal manual-open API")

        def LoadCaptureForReplayRecovery(self, capture, opts, original):
            calls.append(("load", capture, opts, original, False, True))
            assert properties["breadReplayRecoveryCapturePath"] == str(path)
            assert bridge._replay_recovery_path == bridge._capture_path_key(str(path))
            self.loaded, self.filename = False, ""
            lifecycle.OnCaptureClosed()
            if self.load_hook:
                self.load_hook()
            if self.load_failure:
                return self.load_failure
            self.loaded, self.filename = True, capture
            self.event = self.selected = 100
            health["code"] = "Succeeded"
            lifecycle.OnCaptureLoaded()
            return SimpleNamespace(code="Succeeded")

        def SetEventID(self, excludes, selected, event, force):
            calls.append(("select", selected, event, force))
            self.selected, self.event = selected, event
            lifecycle.OnSelectedEventChanged(selected)
            lifecycle.OnEventChanged(event)
            if self.restore_hook:
                self.restore_hook()

    ctx = Context()
    bridge._ctx = ctx
    lifecycle = bridge.CaptureLifecycleViewer()
    lifecycle._remember_current_events()

    def cancel_for_user(capture_changed=False):
        calls.append(("cancel-ai", capture_changed))
        cancel_event.set()

    bridge._poller = SimpleNamespace(_active_item={"cancelEvent": cancel_event},
                                    cancel_active_for_user_activity=cancel_for_user)
    bridge._current_capture_token = lambda: (ctx.filename, ctx.loaded)
    bridge._reset_symbol_overlay = lambda restore_names: calls.append(("reset-symbol-overlay", restore_names))
    bridge._forget_event_shader_overrides = lambda: bridge._event_shader_overrides.clear()
    bridge._forget_shared_shader_replacements = lambda: bridge._shared_shader_replacements.clear()
    bridge._forget_event_cbuffer_snapshots = lambda: calls.append(("forget-cbuffer",))
    bridge._shared_cbuffer_edits = SimpleNamespace(forget=lambda: calls.append(("forget-live-edits",)))
    bridge._cancel_project_auto_activation = lambda: calls.append(("cancel-auto-restore",))
    bridge._abandon_project_launcher_restore = lambda: calls.append(("abandon-restore",))
    bridge._project_async_executor = SimpleNamespace(cancel_owner=lambda owner: calls.append(("cancel-owner", owner)))
    bridge._publish_instance_manifest = lambda **kwargs: None
    bridge._publish_console_event = lambda *args, **kwargs: None
    bridge._shader_workspace_capture_binding = lambda: ({"capturePath": ctx.filename,
        "captureSessionId": bridge._capture_session_id} if ctx.loaded else None)
    bridge._schedule_project_auto_activation = lambda: calls.append(("auto-apply",))
    bridge._schedule_frame_render_flow_restore = lambda: calls.append(("auto-diagram",))
    bridge._notify_shader_capture_changed = lambda binding: calls.append(("clear-diagram", binding))
    return SimpleNamespace(bridge=bridge, ctx=ctx, calls=calls, properties=properties, health=health,
                           documents=panel_documents, models=models, saved=saved, path=path,
                           cancelled=cancel_event, lifecycle=lifecycle)


def invoke(state):
    response = state.bridge.handle_request({"id": "recover", "method": "recover_replay",
        "params": {"expected_capture_session_id": "old"}})
    assert "result" in response, response
    return response["result"]


def test_bridge_recovery_keeps_dirty_source_saved_diagram_and_ai_while_invalidating_live_state(integrated_recovery):
    state = integrated_recovery
    saved_before = deepcopy(state.saved)
    result = invoke(state)
    assert result["recovered"] and result["captureSessionId"] != "old"
    assert result["retainedShaderDraftCount"] == 1 and result["invalidatedShaderReplacementCount"] == 2
    assert result["beforeSavedApplied"] == [{"workspaceId": "draft", "sourcePath": state.documents["draft"]["sourcePath"],
                                             "dirty": True, "wasApplied": True}]
    assert state.documents["draft"]["editor"].text == "unsaved readable HLSL"
    assert state.documents["draft"]["dirty"]
    assert state.bridge._frame_render_flow_saved is state.saved and state.saved == saved_before
    assert state.bridge._frame_render_flow_ui.document is state.saved["document"]
    assert state.saved["document"]["captureSessionId"] == "old"  # Historical, never silently current.
    assert state.bridge._frame_render_flow_ui.view._channel_cache == {}
    assert state.bridge._frame_render_flow_outline_cache is None
    assert state.ctx.event == 42 and state.ctx.selected == 41
    assert not state.cancelled.is_set()
    assert not any(row[0] in ("cancel-ai", "auto-apply", "auto-diagram", "clear-diagram") for row in state.calls)
    assert state.properties["breadReplayRecoveryCapturePath"] is None and state.bridge._replay_recovery_path == ""
    assert state.path.read_bytes() == b"original rdc bytes"


def test_bridge_healthy_noop_never_invalidates_applied_state_or_changes_editors(integrated_recovery):
    state = integrated_recovery
    state.health["code"] = "Succeeded"
    result = invoke(state)
    assert result["status"] == "not-needed" and not result["attempted"]
    assert state.documents["draft"]["appliedReplacementId"] == "replacement-1"
    assert not state.calls and not state.properties


def test_native_reopen_exception_preserves_dirty_text_saved_images_and_clears_native_guard(integrated_recovery):
    state = integrated_recovery

    def broken():
        raise RuntimeError("native reopen failed")

    state.ctx.load_hook = broken
    result = invoke(state)
    assert result["failedPhase"] == "reopen" and not result["recovered"]
    assert state.documents["draft"]["editor"].text == "unsaved readable HLSL"
    assert state.bridge._frame_render_flow_saved is state.saved
    assert not state.documents["draft"].get("appliedReplacementId")
    assert state.properties["breadReplayRecoveryCapturePath"] is None and state.bridge._replay_recovery_path == ""
    assert not state.cancelled.is_set()


def test_explicit_user_cancel_does_not_reopen_or_clear_existing_state(integrated_recovery):
    state = integrated_recovery
    state.cancelled.set()
    result = invoke(state)
    assert result["errorId"] == "replay-recovery-cancelled" and not result["attempted"]
    assert state.documents["draft"]["appliedReplacementId"] == "replacement-1"
    assert not state.calls and not state.properties


def test_native_reopen_error_returns_diagnostic_and_preserves_work_without_waiting_for_a_dialog(integrated_recovery):
    state = integrated_recovery
    state.ctx.load_failure = SimpleNamespace(code="APIHardwareUnsupported", message="E_INVALIDARG")
    result = invoke(state)
    assert result["failedPhase"] == "reopen" and result["status"] == "failed"
    assert not result["recovered"] and result["attempted"]
    assert result["reopenStatus"]["code"] == "APIHardwareUnsupported"
    assert "E_INVALIDARG" in result["reopenStatus"]["diagnostic"]
    assert not state.ctx.loaded and not any(row[0] == "select" for row in state.calls)
    assert state.documents["draft"]["editor"].text == "unsaved readable HLSL"
    assert state.documents["draft"]["dirty"] and state.bridge._frame_render_flow_saved is state.saved
    assert not state.documents["draft"].get("appliedReplacementId")
    assert state.properties["breadReplayRecoveryCapturePath"] is None
    assert state.bridge._replay_recovery_path == "" and not state.bridge._replay_recovery_coordinator.active
    assert not state.cancelled.is_set()


def test_cancel_after_native_load_stops_before_event_restore_and_cleans_guard(integrated_recovery):
    state = integrated_recovery
    state.ctx.load_hook = state.cancelled.set
    result = invoke(state)
    assert result["errorId"] == "replay-recovery-cancelled" and not result["recovered"]
    assert not any(row[0] == "select" for row in state.calls)
    assert state.properties["breadReplayRecoveryCapturePath"] is None
    assert state.bridge._frame_render_flow_saved is state.saved


def test_successful_gui_request_health_checks_do_not_restore_an_obsolete_tracker_session(integrated_recovery):
    state = integrated_recovery
    result = invoke(state)
    assert result["recovered"]
    assert [row for row in state.calls if row[0] == "select"] == [("select", 41, 42, True)]
    assert state.bridge._request_event_tracker() is None
