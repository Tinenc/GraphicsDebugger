"""Exercise the panel's real async export callbacks without a desktop session."""
from __future__ import annotations

import ast
import types
from unittest.mock import Mock

import pytest

from test_frame_flow_panel import MODULE, document, load_module


class Widget:
    def __init__(self):
        self.value, self.visible, self.enabled, self.tooltip = "", False, True, ""

    def clear(self): self.value = ""
    def setText(self, value): self.value = value
    def text(self): return self.value
    def setToolTip(self, value): self.tooltip = value
    def show(self): self.visible = True
    def hide(self): self.visible = False
    def setEnabled(self, value): self.enabled = value


def make_panel(tmp_path):
    module = load_module()
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    cls = next(node for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef) and node.name == "FrameRenderFlowPanel")
    names = {"_is_current", "set_document", "capture_changed", "_clear_export_message",
             "_reset_export_context", "_choose_export", "export_image", "_export_progress", "_export_finished"}
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    jobs = []

    def job(view, path, progress, finished, parent):
        instance = types.SimpleNamespace(progress=progress, finished=finished, cancel=Mock(), path=path)
        jobs.append(instance)
        return instance

    namespace = dict(module.__dict__, _DiagramExport=job,
                     QtWidgets=types.SimpleNamespace(QFileDialog=types.SimpleNamespace(getSaveFileName=Mock())),
                     QtCore=types.SimpleNamespace(QCoreApplication=types.SimpleNamespace(
                         translate=lambda context, source: source)))
    formatter = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == "_format")
    exec(compile(ast.Module(body=[formatter] + methods, type_ignores=[]), str(MODULE), "exec"), namespace)
    panel_type = type("ExportPanel", (), {node.name: namespace[node.name] for node in methods})
    panel = panel_type()
    panel._test_namespace = namespace
    panel.document = module.normalize_document(document(tmp_path))
    panel.binding = dict(panel.document)
    panel.capture_binding_provider = lambda: panel.binding
    panel._alive, panel._export_generation, panel._export_job_generation = True, 0, None
    panel._export_job, panel.export_state = None, {}
    panel._export_message_timer = Mock()
    panel.view, panel.top = Mock(), Mock()
    panel._populate_outline, panel._search_changed, panel.get_state = Mock(), Mock(), Mock(return_value={})
    for name in ("export_status", "save_image", "cancel_export", "status", "title", "summary",
                 "search_status", "search"):
        setattr(panel, name, Widget())
    return panel, jobs


def test_saved_export_message_expires_but_receipt_remains_available(tmp_path):
    panel, jobs = make_panel(tmp_path)
    path = str(tmp_path / "diagram.png")
    panel.export_image(path)
    jobs[-1].finished({"ok": True, "paths": [path]})
    assert panel.export_status.visible and panel.export_status.value == "Saved image: " + path
    panel._export_message_timer.start.assert_called_once_with(6000)
    panel._clear_export_message()  # The parent-owned single-shot timer callback.
    assert not panel.export_status.visible and panel.export_status.value == ""
    assert panel.export_state == {"ok": True, "paths": [path], "running": False}


@pytest.mark.parametrize("reload_same_path", [False, True])
def test_capture_switch_clears_saved_notice_receipt_and_old_quality_tooltip(tmp_path, reload_same_path):
    panel, jobs = make_panel(tmp_path)
    panel.export_image(str(tmp_path / "diagram.png"))
    jobs[-1].finished({"ok": True, "paths": ["previous.png"]})
    panel.status.tooltip = "old analysis gaps"
    panel.binding["captureSessionId"] = "session-b"
    if not reload_same_path:
        panel.binding["capturePath"] = str(tmp_path / "different.rdc")
    panel.capture_changed()
    assert panel.document is None and not panel.save_image.enabled
    assert panel.export_state == {} and panel.export_status.value == ""
    assert not panel.export_status.visible and panel.status.tooltip == ""
    panel._export_message_timer.stop.assert_called()
    panel.view.set_document.assert_called_once_with(None)


def test_document_revision_clears_old_status_even_with_same_flow_id(tmp_path):
    panel, jobs = make_panel(tmp_path)
    panel.export_image(str(tmp_path / "diagram.png"))
    jobs[-1].finished({"ok": True, "paths": ["old.png"]})
    revised = document(tmp_path)
    revised["summary"] = "A newly analysed revision"
    panel.set_document(revised)
    assert panel.document["summary"] == revised["summary"]
    assert panel.export_state == {} and panel.export_status.value == ""
    assert not panel.export_status.visible and panel.save_image.enabled


def test_delayed_export_cannot_write_status_into_next_capture(tmp_path):
    panel, jobs = make_panel(tmp_path)
    panel.export_image(str(tmp_path / "first.png"))
    first = jobs[-1]
    panel.binding["captureSessionId"] = "session-b"
    panel.capture_changed()
    first.cancel.assert_called_once_with()
    next_document = document(tmp_path)
    next_document["captureSessionId"] = "session-b"
    panel.set_document(next_document)
    assert not panel.save_image.enabled  # The previous encoder is still releasing its file.
    first.progress(90, 100)
    assert panel.export_status.value == "" and not panel.export_status.visible
    first.finished({"ok": True, "paths": ["first.png"]})
    assert panel.save_image.enabled and panel._export_job is None
    assert panel.export_state == {} and not panel.export_status.visible
    panel.export_image(str(tmp_path / "second.png"))
    second = jobs[-1]
    first.finished({"ok": True, "paths": ["first.png"]})
    assert panel._export_job is second and panel.export_state["running"]
    second.finished({"ok": True, "paths": ["second.png"]})
    assert panel.export_state["paths"] == ["second.png"]


def test_binding_change_before_capture_callback_still_rejects_async_export_result(tmp_path):
    panel, jobs = make_panel(tmp_path)
    panel.export_image(str(tmp_path / "first.png"))
    panel.binding["captureSessionId"] = "session-b"
    jobs[-1].finished({"ok": True, "paths": ["first.png"]})
    assert not panel.save_image.enabled
    assert panel.export_state == {}
    assert panel.export_status.value == "" and not panel.export_status.visible


def test_shutdown_ignores_queued_callbacks_without_touching_deleted_widgets(tmp_path):
    panel, jobs = make_panel(tmp_path)
    panel.export_image(str(tmp_path / "first.png"))
    panel._alive = False
    del panel.export_status, panel.cancel_export, panel.save_image, panel._export_message_timer
    jobs[-1].progress(100, 100)
    jobs[-1].finished({"ok": True, "paths": ["first.png"]})
    panel._clear_export_message()


def test_wrong_capture_document_is_rejected_without_clearing_current_export(tmp_path):
    panel, jobs = make_panel(tmp_path)
    panel.export_image(str(tmp_path / "diagram.png"))
    jobs[-1].finished({"ok": True, "paths": ["diagram.png"]})
    other = document(tmp_path)
    other["captureSessionId"] = "wrong-session"
    with pytest.raises(ValueError, match="different capture session"):
        panel.set_document(other)
    assert panel.export_state["ok"] and panel.export_status.visible


def test_file_dialog_result_cannot_export_a_capture_opened_while_choosing_path(tmp_path):
    panel, jobs = make_panel(tmp_path)

    def switch_capture(*args):
        panel.binding["captureSessionId"] = "session-b"
        panel.capture_changed()
        next_document = document(tmp_path)
        next_document["captureSessionId"] = "session-b"
        panel.set_document(next_document)
        return str(tmp_path / "old-capture-path.png"), "PNG image (*.png)"

    panel._test_namespace["QtWidgets"].QFileDialog.getSaveFileName.side_effect = switch_capture
    panel._choose_export()
    assert jobs == [] and panel.export_state == {} and not panel.export_status.visible


def test_export_initialization_failure_restores_controls_and_records_diagnostic(tmp_path):
    panel, jobs = make_panel(tmp_path)
    panel._test_namespace["_DiagramExport"] = Mock(side_effect=RuntimeError("Could not allocate scene"))
    with pytest.raises(RuntimeError, match="Could not allocate scene"):
        panel.export_image(str(tmp_path / "diagram.png"))
    assert panel._export_job is None and panel.save_image.enabled and not panel.cancel_export.visible
    assert panel.export_state == {"ok": False, "paths": [], "running": False,
                                  "error": "Could not allocate scene"}
    assert "Could not allocate scene" in panel.export_status.value
