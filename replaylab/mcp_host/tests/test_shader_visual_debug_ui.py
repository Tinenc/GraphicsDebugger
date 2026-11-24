import ast
import hashlib
import importlib.util
import queue
import os
import re
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ui = load("project_shader_visual_debug")
client = load("shader_editor_client")


def document(source):
    return {"editor": mock.Mock(GetText=lambda: source), "workspaceId": "workspace",
            "sourcePath": "/capture/main.frag", "entryPoint": "main"}


def test_debug_request_preserves_utf8_position_and_unsaved_source():
    source = "// 法线\n\tvec4 color = vec4(1); return color;"
    start = source.encode("utf-8").rindex(b"color")
    result = ui.source_request(document(source), {"resourceId": "ResourceId::42",
        "shaderRawSha256": "original", "stage": "fragment", "targetProfile": {"profile": "ps_6_6"}},
        {"captureId": "a"}, start, start + 5, 102)
    assert result["byteStart"] == start
    assert result["source"] == source
    assert result["sourceSha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert result["shaderId"] == "ResourceId::42"
    assert result["expectedOriginalSha256"] == "original"
    assert result["language"] == "glsl"
    assert result["eventId"] == 102
    assert result["scope"] == "shader-resource" and result["persistentAcrossEvents"] is True
    assert result["targetProfile"] == {"profile": "ps_6_6"}


def test_debug_request_rejects_partial_multibyte_selection():
    source = "// 法线\nfloat x;"
    with pytest.raises(UnicodeDecodeError):
        ui.source_request(document(source), {}, {}, 4, 6, 1)


def test_saved_source_preview_uses_physical_owner_for_compiler_contract():
    preview_id, owner_id = 'a' * 64, 'b' * 32
    current = document('float value = 1;')
    current.update(workspaceId=preview_id, sourceWorkspaceId=owner_id)
    result = ui.source_request(current, {'workspaceId': preview_id,
        'sourceWorkspaceId': owner_id, 'workspacePath': '/managed/shader'}, {}, 6, 11, 61990)
    assert result['workspaceId'] == preview_id  # UI completion still belongs to this tab.
    assert result['sourceWorkspaceId'] == owner_id  # Compilation rebinds the exact physical owner.


@pytest.mark.parametrize("workspace, events, selected, expected", [
    ({}, [11, 23], 23, 23),
    ({}, [11, 23], 4, 11),
    ({"eventId": 11}, [], 4, 11),
    ({}, [], 4, 4),
    ({"eventId": True}, [None, True, -1, 23], 4, 23),
])
def test_persistent_debug_anchor_uses_known_shader_usage(workspace, events, selected, expected):
    assert ui.debug_anchor_event(workspace, events, selected) == expected


@pytest.mark.parametrize("active, previous_workspace, previous_capture, previous_shader, expected", [
    (False, "w", "capture-a", "42", 11),
    (True, "w", "capture-a", "42", 23),
    (True, "other", "capture-a", "42", 11),
    (True, "w", "capture-b", "42", 11),
    (True, "w", "capture-a", "99", 11),
])
def test_begin_on_nondraw_keeps_ui_event_and_binds_only_matching_session(
        monkeypatch, active, previous_workspace, previous_capture, previous_shader, expected):
    controller, previous, doc, _ = selection_controller(monkeypatch, "float probe = 0.2; return probe;")
    binding = {"captureSessionId": "capture-a"}
    doc.update(available=True, captureBinding=binding)
    workspace = {"resourceId": "42"}
    controller.panel._document_model = lambda _: workspace
    controller.panel._workspace_event_ids = {"w": [11, 23]}
    controller.panel.ctx.CurEvent = lambda: 4
    controller.panel.ctx.SetEventID = mock.Mock()
    controller.operation = mock.Mock()
    controller._invalidate = mock.Mock()
    controller.session_id = "existing" if active else ""
    previous.update(workspaceId=previous_workspace, shaderId=previous_shader,
        captureBinding={"captureSessionId": previous_capture}, eventId=23, scope="shader-resource")
    controller.begin("w", 6, 11)
    request = controller.submit.call_args.args[1]
    assert controller.submit.call_args.args[0] == "inspect"
    assert request["eventId"] == expected
    assert request.get("sessionId", "") == ("existing" if expected == 23 else "")
    assert controller.panel.ctx.CurEvent() == 4
    controller.panel.ctx.SetEventID.assert_not_called()


def test_new_request_and_source_edit_invalidate_delayed_compilation():
    state = ui.VisualDebugRequestState()
    old = state.next({"sourceSha256": hashlib.sha256(b"float x;").hexdigest()})
    assert state.accepts(old, document("float x;"))
    current = state.next(old)
    assert not state.accepts(old, document("float x;"))
    assert state.accepts(current, document("float x;"))
    assert not state.accepts(current, document("float y;"))
    assert not state.accepts(current, None)


def test_ui_remains_embedded_python_36_compatible():
    ast.parse((ROOT / "project_shader_visual_debug.py").read_text(encoding="utf-8"),
              feature_version=(3, 6))
    ast.parse((ROOT / "project_shader_debug_snapshots.py").read_text(encoding="utf-8"),
              feature_version=(3, 6))


@pytest.mark.parametrize("reason, expected", [
    ("uninitialized-declaration", "after it has been assigned"),
    ("unresolved-value", "declaration or type"),
    ("unsupported-function-parameter", "A declaration in this Shader"),
    ("ambiguous-call-type", "more than one return type"),
    ("unsupported-call-type", "return type of this function call"),
    ("out-argument-position", "out or inout argument"),
    ("macro-position-unavailable", "macro selection"),
    ("value-not-numeric", "numeric member or component"),
    ("select-executable-value", "inside a function body"),
    ("partial-expression-selection", "not a complete subexpression of the original expression"),
    ("future-parser-reason", "future-parser-reason"),
])
def test_expression_error_keeps_specific_reason_and_diagnostic(monkeypatch, reason, expected):
    monkeypatch.setattr(ui, "QtCore", types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=lambda context, text: text)))
    controller = ui.ShaderVisualDebugController.__new__(ui.ShaderVisualDebugController)
    controller.label = mock.Mock()
    controller.show_error({"reason": "unsupported-expression", "instrumentationReason": reason,
                           "message": reason + ": SampleProbeLayer"})
    assert expected in controller.label.setText.call_args.args[0]
    assert "cannot be safely instrumented" not in controller.label.setText.call_args.args[0]
    assert "SampleProbeLayer" in controller.label.setToolTip.call_args.args[0]


def selection_controller(monkeypatch, source="float4 main() { Surface surface; return surface.color; }"):
    monkeypatch.setattr(ui, "QtCore", types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=lambda context, text: text)))
    picker = mock.Mock()
    monkeypatch.setattr(ui, "QtWidgets", types.SimpleNamespace(QInputDialog=types.SimpleNamespace(getItem=picker)))
    doc = document(source)
    doc["workspaceId"] = "w"
    binding = {"captureSessionId": "capture-a"}
    controller = ui.ShaderVisualDebugController.__new__(ui.ShaderVisualDebugController)
    controller.panel = types.SimpleNamespace(top=None, _documents={"w": doc},
        _current_capture_binding=lambda: binding, ctx=types.SimpleNamespace(CurEvent=lambda: 13))
    controller.state = ui.VisualDebugRequestState()
    start = source.encode().index(b"return")
    request = controller.state.next({"workspaceId": "w", "captureBinding": binding,
        "eventId": 13, "source": source, "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
        "byteStart": start, "byteEnd": start + 6})
    controller.request, controller.session_id = request, ""
    controller.active = controller.closed = controller._stopping = False
    controller.busy = True
    controller._after_stop = []
    controller.results = queue.Queue()
    controller.label = mock.Mock()
    controller.update_controls = mock.Mock()
    controller.submit, controller.stop = mock.Mock(), mock.Mock()
    controller.show_output, controller.clear_marks = mock.Mock(), mock.Mock()
    controller.target = mock.Mock(currentData=lambda: -1, findData=lambda value: 0)
    controller._options = lambda parameters: dict(parameters)
    return controller, request, doc, picker


@pytest.mark.parametrize("choices", [[{"path": ".color", "type": "float3"}],
    [{"path": ".color", "type": "float3"}, {"path": ".roughness", "type": "float"}]])
def test_aggregate_picker_includes_single_member_and_selected_expression(monkeypatch, choices):
    controller, request, _, picker = selection_controller(monkeypatch)
    request["member"] = ".memberFromPreviousSource"
    picker.return_value = ("surface.color (float3)", True)
    controller.results.put(("inspect", request, {"status": "ready", "choices": choices,
        "selection": {"expression": "surface"}, "targets": []}))
    controller.drain()
    picker.assert_called_once()
    assert "surface" in picker.call_args.args[2]
    assert picker.call_args.args[3][0] == "surface.color (float3)"
    assert controller.submit.call_args.args[0] == "start"
    assert controller.submit.call_args.args[1]["member"] == ".color"


def test_scalar_inspect_drops_member_from_previous_selection_without_dialog(monkeypatch):
    controller, request, _, picker = selection_controller(monkeypatch)
    request["member"] = ".oldMember"
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": "", "type": "float"}]}))
    controller.drain()
    picker.assert_not_called()
    assert controller.submit.call_args.args[1]["member"] == ""


def test_snapshot_inspection_revalidates_saved_member_without_reopening_picker(monkeypatch):
    controller, request, _, picker = selection_controller(monkeypatch)
    request["snapshotMember"] = ".color"
    request["snapshotOptions"] = {"mode": "custom", "channel": 1}
    controller._restore_options = mock.Mock()
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": ".color", "type": "float3"}],
        "selection": {"expression": "surface"}}))
    controller.drain()
    picker.assert_not_called()
    controller._restore_options.assert_called_once_with({"mode": "custom", "channel": 1})
    assert controller.submit.call_args.args[1]["member"] == ".color"


def test_snapshot_missing_member_never_silently_selects_another_member(monkeypatch):
    controller, request, _, picker = selection_controller(monkeypatch)
    request["snapshotMember"] = ".removed"
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": ".color", "type": "float3"}]}))
    controller.drain()
    picker.assert_not_called()
    controller.submit.assert_not_called()
    assert "no longer exists" in controller.label.setText.call_args.args[0]


def test_snapshot_missing_alpha_never_silently_changes_to_rgb(monkeypatch):
    controller, request, _, picker = selection_controller(monkeypatch)
    request["snapshotMember"] = ".color"
    request["snapshotOptions"] = {"mode": "custom", "channel": 3}
    controller._restore_options = mock.Mock()
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": ".color", "type": "float3"}]}))
    controller.drain()
    controller._restore_options.assert_not_called()
    controller.submit.assert_not_called()
    assert "does not have the selected channel" in controller.label.setText.call_args.args[0]


@pytest.mark.parametrize("type_name,expected", [("float", 1), ("float3", 3), ("vec2", 2),
    ("uvec4", 4), ("uint64_t2", 2), ("min16float", 1)])
def test_channel_controls_follow_numeric_leaf_shape(type_name, expected):
    assert ui.component_count(type_name) == expected


def test_event_follow_retains_intent_through_pause_and_resumes(monkeypatch):
    controller, request, doc, _ = selection_controller(monkeypatch)
    current_event = [14]
    controller.panel.ctx.CurEvent = lambda: current_event[0]
    controller.session_id, controller.active = "old-preview", True
    controller._follow_ready = controller._follow_enabled = True
    controller._invalidate = mock.Mock()
    controller.restore_output_view = mock.Mock()
    controller.selection = {"byteStart": request["byteStart"], "byteEnd": request["byteEnd"]}
    controller.drain()
    operation, followed = controller.submit.call_args.args
    assert operation == "follow" and followed["eventId"] == 14
    assert followed["sessionId"] == "old-preview" and not followed["inspectOnly"]
    controller.results.put(("follow", followed, {"status": "paused", "reason": "shader-changed", "sessionId": ""}))
    controller.drain()
    assert controller.request is followed and controller.paused and not controller.active
    controller.restore_output_view.assert_called_once()
    assert controller.selection is not None
    current_event[0] = 15
    controller.drain()
    resumed = controller.submit.call_args.args[1]
    controller.results.put(("follow", resumed, {"status": "active", "sessionId": "new-preview", "targetIndex": 1,
        "selection": {"byteStart": request["byteStart"], "byteEnd": request["byteEnd"], "expression": "surface"}}))
    controller.drain()
    assert controller.active and not controller.paused and controller.session_id == "new-preview"
    controller.stop.assert_not_called()


def persistent_receipt(request, **values):
    return dict(status="active", sessionId="persistent-session", targetIndex=0,
        scope="shader-resource", resourceScoped=True, persistentAcrossEvents=True,
        eventScoped=False, anchorEventId=request["eventId"], shaderId="65557",
        selection={"byteStart": request["byteStart"], "byteEnd": request["byteEnd"], "expression": "surface"},
        **values)


def test_persistent_debug_navigation_never_recompiles_reapplies_or_moves_marks(monkeypatch):
    controller, request, doc, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True, shaderId="65557")
    controller._invalidate = mock.Mock()
    other = document("float4 other() { return 1; }")
    controller.panel._documents["unrelated"] = other
    controller.panel._current_document = lambda: other
    event = [29]
    controller.panel.ctx.CurEvent = lambda: event[0]
    generation = controller.state.generation
    controller.results.put(("start", request, persistent_receipt(request)))
    controller.drain()
    assert controller.active and controller.session_id == "persistent-session"
    doc["editor"].SetVisualDebugRange.assert_called_once_with(request["byteStart"], request["byteEnd"], True)
    other["editor"].SetVisualDebugRange.assert_not_called()
    assert "65557 stays overridden across EIDs" in controller.label.setText.call_args.args[0]
    for eid in (1, 30, 999, 13):
        event[0] = eid
        controller.drain()
        controller.follow_event(eid)
    assert request["eventId"] == 13 and controller.state.generation == generation
    assert controller.active and not controller.paused
    controller.submit.assert_not_called()
    controller._invalidate.assert_not_called()
    controller.stop.assert_not_called()
    controller.show_output.assert_called_once_with(0, None)


def test_navigation_during_persistent_inspection_keeps_the_original_anchor(monkeypatch):
    controller, request, _, picker = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True)
    controller._follow_ready = False
    controller._invalidate = mock.Mock()
    controller.panel.ctx.CurEvent = lambda: 99
    controller.drain()
    controller.submit.assert_not_called()
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": "", "type": "float"}]}))
    controller.drain()
    operation, submitted = controller.submit.call_args.args
    assert operation == "start" and submitted["eventId"] == 13
    assert submitted["requestGeneration"] == request["requestGeneration"]
    controller._invalidate.assert_not_called()


@pytest.mark.parametrize("missing", ["scope", "resourceScoped", "persistentAcrossEvents", "eventScoped"])
def test_persistent_label_requires_actual_resource_scope_receipt(monkeypatch, missing):
    controller, request, doc, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True)
    receipt = persistent_receipt(request)
    del receipt[missing]
    controller.results.put(("start", request, receipt))
    controller.drain()
    assert not controller.active and controller.session_id == "persistent-session"
    controller.stop.assert_called_once()
    controller.show_output.assert_not_called()
    doc["editor"].SetVisualDebugRange.assert_not_called()
    assert "Restart Bread" in controller.label.setText.call_args.args[0]


def test_persistent_capture_change_still_stops_the_old_owner(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True)
    controller.session_id, controller.active = "persistent-session", True
    controller.panel._current_capture_binding = lambda: {"captureSessionId": "different-capture"}
    controller.drain()
    controller.stop.assert_called_once()
    controller.submit.assert_not_called()


def test_persistent_reply_cannot_mark_a_different_original_shader(monkeypatch):
    controller, request, doc, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", shaderId="ResourceId::72473")
    controller.results.put(("start", request, persistent_receipt(request)))
    controller.drain()
    controller.stop.assert_called_once()
    assert not controller.active
    doc["editor"].SetVisualDebugRange.assert_not_called()
    request["shaderId"] = "ResourceId::65557"
    assert ui.persistent_receipt_matches(request, persistent_receipt(request))


def test_persistent_request_never_claims_to_pause_on_an_unrelated_draw(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource")
    controller.results.put(("inspect", request, {"status": "paused", "reason": "shader-changed"}))
    controller.drain()
    controller.stop.assert_called_once()
    assert "Restart Bread" in controller.label.setText.call_args.args[0]


def test_persistent_compare_retains_shader_anchor_after_event_navigation(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True)
    controller.session_id, controller.active = "persistent-session", True
    controller._invalidate = mock.Mock()
    controller.panel.ctx.CurEvent = lambda: 999
    controller.compare(True)
    operation, submitted = controller.submit.call_args.args
    assert operation == "compare" and submitted["eventId"] == 13
    assert submitted["sessionId"] == "persistent-session" and submitted["original"]


def test_persistent_option_change_reuses_original_session_even_on_an_unrelated_event(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True)
    controller.session_id, controller.active, controller.paused = "persistent-session", True, False
    controller._setting_options = False
    controller._invalidate = mock.Mock()
    controller.panel.ctx.CurEvent = lambda: 99
    controller.mode = mock.Mock(currentData=lambda: "custom")
    controller.range_min, controller.range_max = mock.Mock(), mock.Mock()
    controller.range_row, controller.range_label = mock.Mock(), mock.Mock()
    controller.options_changed()
    operation, submitted = controller.submit.call_args.args
    assert operation == "start" and submitted["eventId"] == 13
    assert submitted["sessionId"] == "persistent-session"


def test_persistent_options_force_actual_render_target(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    controller.mode = mock.Mock(currentData=lambda: "custom")
    controller.target = mock.Mock(currentData=lambda: -1)
    controller.float_preview = mock.Mock(isChecked=lambda: True)
    controller.range_min, controller.range_max = mock.Mock(value=lambda: 0), mock.Mock(value=lambda: 1)
    controller.iteration = mock.Mock(value=lambda: 1)
    controller.channel = mock.Mock(currentData=lambda: -1)
    assert ui.ShaderVisualDebugController._options(controller, request)["independentFloatOutput"] is False


def test_old_independent_snapshot_uses_actual_rt_with_visible_notice_without_rewriting_it(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    request.update(scope="shader-resource", persistentAcrossEvents=True)
    controller.mode, controller.channel = mock.Mock(findData=lambda v: 0), mock.Mock(findData=lambda v: 0)
    controller.range_min, controller.range_max = mock.Mock(), mock.Mock()
    controller.float_preview, controller.iteration = mock.Mock(), mock.Mock()
    controller.options_changed = mock.Mock()
    options = {"independentFloatOutput": True, "mode": "custom"}
    assert controller._restore_options(options)
    assert options == {"independentFloatOutput": True, "mode": "custom"}
    controller.float_preview.setChecked.assert_called_once_with(False)
    controller.results.put(("start", request, persistent_receipt(request)))
    controller.drain()
    assert "saved snapshot is unchanged" in controller.label.setText.call_args.args[0]
    assert "now writes to the actual RT" in controller.label.setText.call_args.args[0]


def test_event_change_during_inspect_requests_choices_before_apply(monkeypatch):
    controller, request, _, picker = selection_controller(monkeypatch)
    controller._follow_ready = False
    controller._invalidate = mock.Mock()
    controller.panel.ctx.CurEvent = lambda: 14
    controller.drain()
    followed = controller.submit.call_args.args[1]
    assert followed["inspectOnly"]
    picker.return_value = ("surface.color (float3)", True)
    controller.results.put(("follow", followed, {"status": "ready", "choices": [{"path": ".color", "type": "float3"}],
        "selection": {"expression": "surface"}}))
    controller.drain()
    assert controller.submit.call_args.args[0] == "start"
    assert controller.submit.call_args.args[1]["member"] == ".color"
    assert "inspectOnly" not in controller.submit.call_args.args[1]


def test_failed_follow_keeps_intent_but_clears_unowned_session(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    controller.session_id, controller.active = "old-preview", True
    controller.results.put(("follow", request, {"status": "error", "reason": "compile-failed", "message": "error detail", "restored": True}))
    controller.drain()
    assert controller.request is request and controller.paused
    assert not controller.session_id and not controller.active
    assert "error detail" in controller.label.setText.call_args.args[0]


def test_unconfirmed_follow_error_retains_stop_ownership(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    controller.session_id = "previous-preview"
    controller.results.put(("follow", request, {"status": "error", "reason": "operation-failed", "message": "Reply lost"}))
    controller.drain()
    assert controller.session_id == "previous-preview"
    assert controller.request is request and controller.paused


def test_paused_option_changes_update_intent_and_retry_current_event(monkeypatch):
    controller, request, _, _ = selection_controller(monkeypatch)
    controller.paused, controller.active = True, False
    controller._setting_options = False
    controller._follow_ready = True
    controller._invalidate = mock.Mock()
    controller.mode = mock.Mock(currentData=lambda: "custom")
    controller.range_min = mock.Mock(value=lambda: 0)
    controller.range_max = mock.Mock(value=lambda: 1)
    controller.range_row, controller.range_label = mock.Mock(), mock.Mock()
    controller._options = lambda value: dict(value, mode="custom", targetIndex=-1, channel=1)
    controller.options_changed()
    operation, updated = controller.submit.call_args.args
    assert operation == "follow" and updated["eventId"] == 13
    assert updated["channel"] == 1 and updated["targetIndex"] == -1
    assert updated["requestGeneration"] > request["requestGeneration"]
    assert not updated["inspectOnly"]


def test_explicit_restore_disables_follow_even_if_restore_reply_fails(monkeypatch):
    controller, _, _, _ = selection_controller(monkeypatch)
    controller.operation = mock.Mock()
    controller._invalidate = mock.Mock()
    controller._follow_ready = controller._follow_enabled = True
    controller.session_id = "preview"
    ui.ShaderVisualDebugController.stop(controller)
    operation, request = controller.submit.call_args.args
    assert operation == "stop" and not controller._follow_enabled
    controller.results.put(("stop", request, {"status": "error", "reason": "operation-failed", "message": "Reply lost"}))
    controller.drain()
    controller.panel.ctx.CurEvent = lambda: 14
    controller.drain()
    assert controller.submit.call_count == 1
    assert controller.session_id == "preview"


@pytest.mark.parametrize("code,expected", [("snapshot-identity-mismatch", "Shader identity"),
    ("snapshot-anchor-unavailable", "inside a function"), ("snapshot-invalid-options", "display settings"),
    ("snapshot-size-limit", "limit"), ("snapshot-store-busy", "Another update"),
    ("snapshot-store-invalid", "left unchanged"), ("snapshot-store-read-failed", "Could not read"),
    ("snapshot-store-write-failed", "Could not save"), ("snapshot-invalid-order", "Reopen the list"),
    ("future-error", "See the diagnostic")])
def test_snapshot_errors_localize_known_ids_and_keep_diagnostics_separate(monkeypatch, code, expected):
    module = load("project_shader_debug_snapshots")
    monkeypatch.setattr(module, "QtCore", types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=lambda context, text: text)))
    assert expected in module.snapshot_error_message(types.SimpleNamespace(code=code))


def test_snapshot_does_not_silently_fall_back_from_missing_render_target(monkeypatch):
    controller, _, _, _ = selection_controller(monkeypatch)
    controller.target.findData = lambda value: -1
    assert not controller._restore_options({"targetIndex": 2})
    assert "render target is unavailable" in controller.label.setText.call_args.args[0]


def test_real_qt_snapshot_list_renames_reorders_and_reopens_latest_source(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PySide2.QtWidgets")
    application = qt.QApplication.instance() or qt.QApplication([])
    snapshot_ui = load("project_shader_debug_snapshots")
    backend = load("shader_visual_snapshots")
    host = qt.QWidget()
    source = ["float4 main(float4 p:SV_Position):SV_Target { float x=p.x; return x; }"]
    doc = document(source[0])
    doc.update(available=True, workspaceId="w", sourcePath="/capture/main.hlsl")
    # This list/model test covers the older editor's strict source fallback.
    # An unrestricted Mock invents callable native range methods that do not
    # exist. Real native tracking is exercised by shader_anchor_tracking_smoke.
    doc["editor"] = mock.Mock(spec=["GetText", "GoToLine"])
    doc["editor"].GetText = lambda: source[0]
    workspace = {"workspacePath": str(tmp_path / "workspace"), "shaderRawSha256": "sha-original", "stage": "pixel"}
    identity = snapshot_ui.snapshot_identity(doc, workspace)
    state = ui.VisualDebugRequestState()
    start = source[0].encode().rindex(b"x;")
    request = state.next({"workspaceId": "w", "sourceSha256": hashlib.sha256(source[0].encode()).hexdigest(), "member": ""})
    controller = types.SimpleNamespace(request=request, selection={"byteStart": start, "byteEnd": start + 1, "line": 1, "expression": "x"},
        state=state, label=mock.Mock(), begin=mock.Mock(), _options=lambda value: dict(value, mode="custom", channel=-1))
    panel = types.SimpleNamespace(top=host, visual_debug=controller, _current_document=lambda: doc,
        _document_model=lambda workspace_id: workspace)
    view = snapshot_ui.ShaderDebugSnapshots(panel, host)
    view.timer.stop()
    view._module, view._identity = backend, identity
    view._store = backend.SnapshotStore(str(tmp_path / "snapshots"), identity)
    panel._toggle_debug_snapshots = lambda visible: view.setVisible(visible)
    try:
        view.add_current()
        assert view.list.count() == 1
        first_id = view._store.list()[0]["id"]
        view.list.setCurrentRow(0)
        monkeypatch.setattr(qt.QInputDialog, "getText", lambda *args: ("法线与颜色", True))
        view.rename_current()
        assert view._store.list()[0]["name"] == "法线与颜色"
        assert "法线与颜色" in view.list.item(0).text()
        view.add_current()
        second_id = view._store.list()[1]["id"]
        # This executes Qt's native row-moving model operation (the one used
        # by InternalMove), including its real rowsMoved notification. No
        # direct persistence callback or takeItem shortcut is involved.
        root = snapshot_ui.QtCore.QModelIndex()
        assert view.list.model().moveRows(root, 0, 1, root, 2)
        assert [item["id"] for item in view._store.list()] == [first_id, second_id]
        assert view.reorder_timer.isActive()
        application.processEvents()
        assert [item["id"] for item in view._store.list()] == [second_id, first_id]
        source[0] = "// 最新源码\n\n" + source[0]
        view.refresh(True)
        assert "Line 3" in view.list.item(0).text()
        view.activate(view.list.item(0))
        args, kwargs = controller.begin.call_args
        assert args[1] == source[0].encode().rindex(b"x;")
        assert kwargs["snapshot"]["options"]["mode"] == "custom"
        doc["editor"].GoToLine.assert_called_once_with(3, 1)
        controller.begin.reset_mock()
        source[0] = source[0].replace("return x;", "return 0;")
        view.activate(view.list.item(0))
        controller.begin.assert_not_called()
        assert "Unresolved" in view.list.item(0).text()
        view.list.setCurrentRow(0)
        view.delete_current()
        assert len(view._store.list()) == 1
    finally:
        view.timer.stop()
        host.close()
        host.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("accepted", [False, True])
def test_old_member_dialog_cannot_cancel_or_apply_over_a_newer_request(monkeypatch, accepted):
    controller, request, _, picker = selection_controller(monkeypatch)
    def choose(*arguments):
        controller.request = controller.state.next(request)
        return "surface.color (float3)", accepted
    picker.side_effect = choose
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": ".color", "type": "float3"}],
        "selection": {"expression": "surface"}}))
    controller.drain()
    controller.submit.assert_not_called()
    controller.stop.assert_not_called()
    assert controller.request["requestGeneration"] > request["requestGeneration"]


def test_source_change_during_member_dialog_prevents_apply(monkeypatch):
    controller, request, doc, picker = selection_controller(monkeypatch)
    def choose(*arguments):
        doc["editor"].GetText = lambda: "float4 main() { return 0; }"
        return "surface.color (float3)", True
    picker.side_effect = choose
    controller.results.put(("inspect", request, {"status": "ready", "choices": [{"path": ".color", "type": "float3"}],
        "selection": {"expression": "surface"}}))
    controller.drain()
    controller.submit.assert_not_called()
    controller.stop.assert_called_once()


@pytest.mark.parametrize("expression, member", [("mix(a, b, 0.5)", ""), ("surface", ".color")])
def test_active_preview_marks_backend_expression_and_names_chosen_member(monkeypatch, expression, member):
    source = "// 法线\nfloat4 main() { return " + expression + "; }"
    controller, request, doc, _ = selection_controller(monkeypatch, source)
    request["member"] = member
    start = source.encode().index(expression.encode())
    end = start + len(expression.encode())
    controller.results.put(("start", request, {"status": "active", "sessionId": "session", "targetIndex": 0,
        "selection": {"byteStart": start, "byteEnd": end, "expression": expression, "positionKind": "return-value"}}))
    controller.drain()
    doc["editor"].SetVisualDebugRange.assert_called_once_with(start, end, True)
    assert expression + member in controller.label.setText.call_args.args[0]


def test_real_qt_debug_toolbar_wraps_commands_and_keeps_options_separate():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PySide2.QtWidgets")
    application = qt.QApplication.instance() or qt.QApplication([])
    host = qt.QWidget()
    layout = qt.QVBoxLayout(host)
    editor = mock.Mock()
    panel = types.SimpleNamespace(top=host, _current_document=lambda: {"editor": editor})
    controller = ui.ShaderVisualDebugController(panel, mock.Mock(), layout)
    controller.timer.stop()
    try:
        host.resize(290, 400)
        host.show()
        application.processEvents()
        buttons = (controller.start_button, controller.stop_button, controller.compare_button,
                   controller.snapshot_button, controller.options_button)
        bounds = controller.row.rect()
        for button in buttons:
            assert button.isVisible()
            assert bounds.contains(button.geometry())
        for index, button in enumerate(buttons):
            assert all(not button.geometry().intersects(other.geometry()) for other in buttons[index + 1:])
        assert controller.stop_button.y() > controller.start_button.y()
        assert controller.label.isVisible() and not controller.options_panel.isVisible()
        assert "Ctrl+E" in controller.start_button.text()
        assert "Ctrl+Shift+E" in controller.stop_button.text()
        assert not hasattr(controller, "source_button")
        assert controller.mode.currentData() == "custom"
        assert controller.snapshot_shortcut.key().toString() == "Ctrl+Shift+B"
        controller._set_value_type("float3")
        assert not controller.channel.model().item(4).isEnabled()
        assert controller.channel.model().item(3).isEnabled()
        controller.start_button.click()
        editor.RequestVisualDebug.assert_called_once()
        controller.options_button.click()
        application.processEvents()
        assert controller.options_panel.isVisible() and controller.float_preview.isVisible()
        assert not controller.float_preview.isEnabled() and not controller.float_preview.isChecked()
        assert "do not feed later rendering" in controller.float_preview.toolTip()
        controller.mode.setCurrentIndex(controller.mode.findData("range"))
        assert controller.range_row.isVisible() and controller.range_label.isVisible()
        controller.mode.setCurrentIndex(controller.mode.findData("raw"))
        assert not controller.range_row.isVisible() and not controller.range_label.isVisible()
        controller.active, controller.session_id = True, "preview"
        controller.update_controls()
        assert controller.stop_button.isEnabled() and controller.compare_button.isEnabled()
        assert controller.label.font().bold()
    finally:
        controller.closed = True
        host.close()
        host.deleteLater()
        application.processEvents()


def test_portable_transport_uses_fixed_loopback_and_does_not_follow_redirects():
    response = mock.Mock(status=302)
    response.read.return_value = b"moved"
    connection = mock.Mock()
    connection.getresponse.return_value = response
    with mock.patch("http.client.HTTPConnection", return_value=connection) as factory:
        result = client._portable_loopback_request("POST", "/replaylab/shader-live-edit", 12345,
            "Authorization: Bearer secret\r\nContent-Type: application/json\r\n", b"{}", 0.5)
    factory.assert_called_once_with("127.0.0.1", 12345, timeout=0.5)
    assert result == (302, b"moved")
    connection.request.assert_called_once()
    assert connection.request.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
    connection.close.assert_called_once()


def test_portable_transport_rejects_oversized_response_and_closes_connection():
    response = mock.Mock(status=200)
    response.read.return_value = b"x" * (client.MAX_RESPONSE_BYTES + 1)
    connection = mock.Mock()
    connection.getresponse.return_value = response
    with mock.patch("http.client.HTTPConnection", return_value=connection):
        with pytest.raises(client.ReplayLabShaderSaveError, match="response size"):
            client._portable_loopback_request("GET", "/health", 12345, "", b"", 0.5)
    connection.close.assert_called_once()


@pytest.mark.parametrize("path", ["https://remote.example/", "//remote.example", "/health\r\nInjected: yes"])
def test_portable_transport_rejects_nonlocal_request_targets(path):
    with pytest.raises(client.ReplayLabShaderSaveError):
        client._portable_loopback_request("GET", path, 12345, "", b"", 0.5)


def test_non_windows_request_routes_to_portable_transport():
    with mock.patch.object(client.os, "name", "posix"), mock.patch.object(
            client, "_portable_loopback_request", return_value=(200, b"{}")) as transport:
        assert client._winhttp_request_blocking("GET", "/health", 12345, "", b"", 1) == (200, b"{}")
    transport.assert_called_once()


def test_failed_restore_retains_recovery_state_and_success_clears_marks(monkeypatch):
    monkeypatch.setattr(ui, "QtCore", types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=lambda context, text: text)))
    editor = mock.Mock()
    controller = ui.ShaderVisualDebugController.__new__(ui.ShaderVisualDebugController)
    controller.panel = types.SimpleNamespace(_documents={"w": {"editor": editor}})
    controller.operation = mock.Mock(return_value={"status": "invalidated"})
    controller.state = ui.VisualDebugRequestState()
    controller.request = {"workspaceId": "w"}
    controller.session_id = "active-session"
    controller.active = True
    controller.busy = False
    controller.closed = False
    controller._stopping = False
    controller._after_stop = []
    controller.results = queue.Queue()
    controller.label = mock.Mock()
    controller.update_controls = mock.Mock()
    controller.submit = mock.Mock()
    controller.stop()
    assert controller.active
    assert controller.session_id == "active-session"
    editor.SetVisualDebugRange.assert_not_called()
    request = controller.submit.call_args.args[1]
    controller.results.put(("stop", request, {"status": "error", "reason": "restore-failed", "message": "GPU error"}))
    controller.drain()
    assert controller.active and controller.session_id == "active-session"
    assert not controller._stopping
    assert "still needs to be stopped" in controller.label.setText.call_args.args[0]
    controller.stop()
    request = controller.submit.call_args.args[1]
    controller.results.put(("stop", request, {"status": "stopped"}))
    controller.drain()
    assert not controller.active and not controller.session_id and controller.request is None
    editor.SetVisualDebugRange.assert_called_once_with(0, 0, False)


def test_committed_preview_error_marks_actual_expression_and_can_still_stop(monkeypatch):
    monkeypatch.setattr(ui, "QtCore", types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=lambda context, text: text)))
    source = "float value;"
    doc = document(source)
    doc["workspaceId"] = "w"
    binding = {"captureSessionId": "capture-a"}
    controller = ui.ShaderVisualDebugController.__new__(ui.ShaderVisualDebugController)
    controller.panel = types.SimpleNamespace(_documents={"w": doc},
        _current_capture_binding=lambda: binding, ctx=types.SimpleNamespace(CurEvent=lambda: 13))
    controller.state = ui.VisualDebugRequestState()
    request = controller.state.next({"workspaceId": "w", "captureBinding": binding,
        "eventId": 13, "source": source, "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
        "byteStart": 6, "byteEnd": 11})
    controller.request, controller.session_id = request, ""
    controller.active = False
    controller.closed = controller.busy = controller._stopping = False
    controller._after_stop = []
    controller.results = queue.Queue()
    controller.label = mock.Mock()
    controller.update_controls = mock.Mock()
    controller.submit = mock.Mock()
    controller.operation = mock.Mock()
    controller.results.put(("start", request, {"status": "error", "reason": "restore-failed",
        "message": "rollback failed", "activePreview": True, "recoverySession": {
            "sessionId": "committed", "selection": {"byteStart": 6, "byteEnd": 11}}}))
    controller.drain()
    assert controller.active and controller.session_id == "committed"
    doc["editor"].SetVisualDebugRange.assert_called_with(6, 11, True)
    assert "still needs to be stopped" in controller.label.setText.call_args.args[0]
    controller.stop()
    stopping = controller.submit.call_args.args[1]
    assert stopping["sessionId"] == "committed"
    controller.results.put(("stop", stopping, {"status": "stopped"}))
    controller.drain()
    assert not controller.active and not controller.session_id
    doc["editor"].SetVisualDebugRange.assert_called_with(0, 0, False)


def test_forced_destroy_requests_restore_without_accessing_deleted_widgets():
    controller = ui.ShaderVisualDebugController.__new__(ui.ShaderVisualDebugController)
    controller.closed = False
    controller.request = {"workspaceId": "w"}
    controller.session_id = "session"
    controller.busy = False
    controller._stopping = False
    controller.state = ui.VisualDebugRequestState()
    controller.operation = mock.Mock()
    controller.submit = mock.Mock()
    controller.timer = mock.Mock()
    controller.timer.stop.side_effect = RuntimeError("wrapped C++ object has been deleted")
    # No label, panel or editor exists: shutdown must not access any of them.
    controller.shutdown()
    assert controller.closed
    assert controller.submit.call_args.args[0] == "stop"
    assert controller.submit.call_args.args[1]["sessionId"] == "session"


def workspace_method(name, extra=None):
    source = (ROOT / "project_shader_workspace.py").read_text(encoding="utf-8")
    method = next(node for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"hashlib": hashlib, "os": os, "tempfile": tempfile,
                 "TOKEN_RE": re.compile(r"^[0-9a-f]{32,64}$")}
    namespace.update(extra or {})
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<workspace-method>", "exec"), namespace)
    return namespace[name]


def test_capture_source_model_is_editable_but_has_no_host_apply_authority(tmp_path):
    binding = {"captureSessionId": "a" * 32, "capturePath": str(tmp_path / "frame.rdc")}
    panel = types.SimpleNamespace(_capture_binding=binding, _current_capture_binding=lambda: binding,
        _capture_sources={}, _workspaces={}, _source_documents={}, _render_tree=mock.Mock(),
        _open_workspace=mock.Mock(return_value=True))
    identifier = "b" * 32
    source = {"workspaceId": identifier, "captureBinding": binding, "text": "void main(){}",
        "sourcePath": str(tmp_path / "generated0_shader"), "rootSourcePath": str(tmp_path / "generated0_shader"),
        "includeRoot": str(tmp_path), "entryPoint": "main", "language": "glsl", "stage": "fragment", "resourceId": "3"}
    assert workspace_method("open_capture_source")(panel, source)
    workspace = panel._workspaces[identifier]
    assert workspace["documentKind"] == "capture-source"
    assert workspace["sourceEditable"] and not workspace["autoApplyReady"]
    assert workspace["language"] == "glsl" and workspace["includeRoot"] == str(tmp_path)
    assert "authorizationId" not in workspace
    assert "organizationId" not in workspace
    panel._open_workspace.assert_called_once_with(identifier, quiet=False, select=True)


def test_capture_source_save_exports_local_copy_without_replay_or_host(tmp_path):
    destination = tmp_path / "saved.frag"
    qt = types.SimpleNamespace(QCoreApplication=types.SimpleNamespace(translate=lambda context, text: text))
    widgets = types.SimpleNamespace(QFileDialog=types.SimpleNamespace(
        getSaveFileName=lambda *args: (str(destination), "")))
    panel = types.SimpleNamespace(top=None, status_label=mock.Mock(), _sync_document_dirty=mock.Mock())
    source = document("// 法线\nvoid main(){}")
    source.update(fileName="shader.frag")
    assert workspace_method("_export_capture_source", {"QtCore": qt, "QtWidgets": widgets})(panel, source)
    assert destination.read_bytes() == source["editor"].GetText().encode("utf-8")
    assert source["exportPath"] == str(destination)
    source["editor"].SetSavePoint.assert_called_once()
    assert not list(tmp_path.glob(".bread-source-*"))


def output_controller(viewer):
    controller = ui.ShaderVisualDebugController.__new__(ui.ShaderVisualDebugController)
    controller.panel = types.SimpleNamespace(ctx=types.SimpleNamespace(GetTextureViewer=lambda: viewer))
    controller._output_viewer = None
    controller._output_view_token = 0
    controller._output_view_started = False
    return controller


def test_output_focus_keeps_one_native_snapshot_across_rt_switches_and_restores_it():
    viewer = mock.Mock(BeginTemporaryOutputFollow=mock.Mock(return_value=41),
                       UpdateTemporaryOutputFollow=mock.Mock(return_value=True))
    controller = output_controller(viewer)
    controller.show_output(2)
    controller.show_output(0)
    viewer.BeginTemporaryOutputFollow.assert_called_once_with(2)
    viewer.UpdateTemporaryOutputFollow.assert_called_once_with(41, 0)
    controller.restore_output_view()
    viewer.EndTemporaryOutputFollow.assert_called_once_with(41)
    controller.show_output(1)
    assert viewer.BeginTemporaryOutputFollow.call_count == 2


def test_manual_texture_navigation_releases_focus_until_debug_session_stops():
    viewer = mock.Mock(BeginTemporaryOutputFollow=mock.Mock(return_value=41),
                       UpdateTemporaryOutputFollow=mock.Mock(return_value=False))
    controller = output_controller(viewer)
    controller.show_output(2)
    controller.show_output(0)  # native token was invalidated by manual navigation
    controller.show_output(1)
    viewer.BeginTemporaryOutputFollow.assert_called_once()
    viewer.UpdateTemporaryOutputFollow.assert_called_once()
    viewer.EndTemporaryOutputFollow.assert_called_once_with(41)
    controller.restore_output_view()
    viewer.EndTemporaryOutputFollow.assert_called_once_with(41)


def test_destroyed_texture_viewer_does_not_block_debug_cleanup():
    viewer = mock.Mock(BeginTemporaryOutputFollow=mock.Mock(return_value=41))
    viewer.EndTemporaryOutputFollow.side_effect = RuntimeError("wrapped C++ object has been deleted")
    controller = output_controller(viewer)
    controller.show_output(2)
    controller.restore_output_view()
    assert controller._output_view_token == 0 and not controller._output_view_started


def test_float_preview_and_compare_share_the_original_view_snapshot():
    viewer = mock.Mock(BeginTemporaryOutputFollow=mock.Mock(return_value=41),
                       UpdateTemporaryOutputFollow=mock.Mock(return_value=True),
                       ShowTemporaryTexturePreview=mock.Mock(return_value=True))
    controller = output_controller(viewer)
    descriptor = object()
    controller.show_output(2, descriptor)
    controller.show_output(2)  # comparison uses the captured output
    controller.show_output(2, descriptor)
    viewer.BeginTemporaryOutputFollow.assert_called_once_with(2)
    assert viewer.ShowTemporaryTexturePreview.call_args_list == [mock.call(41, descriptor)] * 2
    controller.restore_output_view()
    viewer.EndTemporaryOutputFollow.assert_called_once_with(41)


def test_failed_float_preview_display_releases_only_its_owned_snapshot():
    viewer = mock.Mock(BeginTemporaryOutputFollow=mock.Mock(return_value=41),
                       ShowTemporaryTexturePreview=mock.Mock(return_value=False))
    controller = output_controller(viewer)
    controller.show_output(2, object())
    controller.show_output(0)
    viewer.EndTemporaryOutputFollow.assert_called_once_with(41)
    viewer.BeginTemporaryOutputFollow.assert_called_once()
    assert controller._output_view_token == 0
