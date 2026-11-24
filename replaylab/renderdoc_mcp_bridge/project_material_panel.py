"""Localized MiniQt UI for reflected, shared CBuffer parameters (Python 3.6)."""

import importlib.util
import os

try:
    from .material_panel import MaterialPanelError, _stage_abbreviation
except (ImportError, ValueError):
    _spec = importlib.util.spec_from_file_location(
        "_replaylab_material_model", os.path.join(os.path.dirname(__file__), "material_panel.py"))
    _model = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_model)
    MaterialPanelError = _model.MaterialPanelError
    _stage_abbreviation = _model._stage_abbreviation


def register_material_menus(ctx, qrd, QtCore, show_callback, expose_callback):
    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Window,
        [QtCore.QCoreApplication.translate("ReplayLabProjects", "Material Parameters")],
        show_callback)
    ctx.Extensions().RegisterContextMenu(
        qrd.ContextMenu.BufferViewer_Variable,
        [QtCore.QCoreApplication.translate("ReplayLabProjects", "Bread AI"),
         QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Expose as Material Parameter")], expose_callback)


def show_selection_error(ctx, QtCore, diagnostic):
    ctx.Extensions().ErrorDialog(
        QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "%1\n\nThe parameter was not exposed and no CBuffer bytes were changed.")
        .replace("%1", str(diagnostic)),
        QtCore.QCoreApplication.translate("ReplayLabProjects", "Material Parameters"))


def _parameter_error_text(error):
    message_id = getattr(error, "message_id", "")
    if message_id == "material-value-component-count":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Enter %1 comma-separated components").replace("%1", str(error.parameter))
    if message_id == "material-value-float-required":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Float parameters require valid numbers")
    if message_id == "material-value-integer-required":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Integer parameters require decimal integers")
    return str(error)


class MiniQtMaterialPanel(object):
    """A portable dock panel using qrenderdoc's MiniQt helper only."""

    def __init__(self, ctx, qrd_module, qt_core, controller, closed_callback=None):
        global QtCore
        QtCore = qt_core
        self.ctx = ctx
        self.qrd = qrd_module
        self.qt_core = qt_core
        self.controller = controller
        self.closed_callback = closed_callback
        self.mqt = ctx.Extensions().GetMiniQtHelper()
        self.top = self.mqt.CreateToplevelWidget(
            QtCore.QCoreApplication.translate("ReplayLabProjects", "Material Parameters"),
            self._closed)
        self.root = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.top, self.root)
        self.capability = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(
            self.capability,
            QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Shared buffer fields · changes affect all consumers"))
        self.mqt.AddWidget(self.root, self.capability)
        self.content = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.root, self.content)
        self.status_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(self.status_label, QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "No exposed parameters"))
        self.mqt.AddWidget(self.root, self.status_label)
        self.rollback_all_button = self.mqt.CreateButton(self._rollback_all_clicked)
        self.mqt.SetWidgetText(self.rollback_all_button, QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Clear all material field edits"))
        self.mqt.AddWidget(self.root, self.rollback_all_button)
        self._docked = False
        self._building = False
        self._generation = {}
        self._row_status = {}
        self._editor_widgets = {}
        self._dynamic_roots = []
        self._alive = True
        self._callback_epoch = 0
        self._closed_notified = False
        self._pending_timers = []

    def _retire_timer(self, timer):
        try:
            self._pending_timers.remove(timer)
        except ValueError:
            pass
        try:
            timer.stop()
        except Exception:
            pass
        try:
            timer.timeout.disconnect()
        except Exception:
            pass
        try:
            timer.deleteLater()
        except Exception:
            pass

    def _cancel_pending_timers(self):
        # QTimer.singleShot retains an opaque Python callback that cannot be
        # cancelled.  Owning each debounce timer explicitly lets shutdown drop
        # every callback before qrenderdoc tears down PySide/Python 3.6.
        for timer in list(self._pending_timers):
            self._retire_timer(timer)
        self._pending_timers = []

    def _schedule_debounced(self, callback, delay_ms):
        timer = self.qt_core.QTimer()
        timer.setSingleShot(True)

        def _fired(selected_timer=timer, selected_callback=callback):
            self._retire_timer(selected_timer)
            selected_callback()

        timer.timeout.connect(_fired)
        self._pending_timers.append(timer)
        timer.start(int(delay_ms))

    def _closed(self, _ctx, _widget, _text):
        # This callback runs from QWidget destruction. Never enter replay or MiniQt here: doing so
        # re-enters Qt while its object tree is being torn down and can crash qrenderdoc. Explicit
        # rollback remains available in the panel and shutdown() performs it before destruction.
        if self.top is None and self._closed_notified:
            return
        self._cancel_pending_timers()
        if self.controller is not None and hasattr(self.controller, "edit_queue"):
            self.controller.edit_queue.cancel_owner(self)
        self._alive = False
        self._callback_epoch += 1
        self._generation.clear()
        self._row_status.clear()
        self._editor_widgets.clear()
        self._dynamic_roots = []
        self._docked = False
        callback = None
        if self.closed_callback and not self._closed_notified:
            self._closed_notified = True
            callback = self.closed_callback
        # Release borrowed native handles before interpreter finalization.  In
        # particular an undocked panel has no ToolWindowManager owner, so its
        # QWidget would otherwise survive until Qt/Python shutdown ordering is
        # already unsafe.
        self.closed_callback = None
        self.top = None
        self.root = None
        self.capability = None
        self.content = None
        self.status_label = None
        self.rollback_all_button = None
        self.ctx = None
        self.qrd = None
        self.qt_core = None
        self.mqt = None
        self.controller = None
        if callback:
            callback()

    def _require_alive(self):
        if not self._alive or self.top is None:
            raise MaterialPanelError("The material panel has already been closed")

    def show(self):
        self._require_alive()
        if not self._docked:
            ref = None
            dock_ref = self.qrd.DockReference.MainToolArea
            try:
                if self.ctx.HasPipelineViewer():
                    ref = self.ctx.GetPipelineViewer().Widget()
                    dock_ref = self.qrd.DockReference.RightOf
            except Exception:
                ref = None
                dock_ref = self.qrd.DockReference.MainToolArea
            self.ctx.AddDockWindow(self.top, dock_ref, ref, 0.30)
            self._docked = True
        self.ctx.RaiseDockWindow(self.top)

    @staticmethod
    def _format_value(parameter):
        if parameter["baseType"] == "Float":
            return ", ".join("%.9g" % value for value in parameter["value"])
        return ", ".join(str(value) for value in parameter["value"])

    def _set_status(self, text):
        self.mqt.SetWidgetText(self.status_label, str(text))

    def expose(self, selection):
        self._require_alive()
        status = self.controller.expose(selection)
        self.rebuild()
        self.show()
        self._set_status(
            QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Exposed %1 parameter(s) from EID %2; edits update the current preview")
            .replace("%1", str(len(status["parameters"]))).replace("%2", str(status["eventId"])))
        return status

    def _destroy_dynamic(self):
        self.mqt.ClearContainedWidgets(self.content)
        for widget in self._dynamic_roots:
            try:
                self.mqt.DestroyWidget(widget)
            except Exception:
                pass
        self._dynamic_roots = []
        self._row_status = {}
        self._editor_widgets = {}

    def rebuild(self):
        self._require_alive()
        self._building = True
        try:
            self._destroy_dynamic()
            statuses = self.controller.list_status()
            self.mqt.SetWidgetEnabled(self.rollback_all_button, bool(statuses))
            if not statuses:
                empty = self.mqt.CreateLabel()
                self.mqt.SetWidgetText(
                    empty,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Open a Pipeline State CBuffer, right-click a reflected value, then "
                        "choose\nBread AI → Expose as Material Parameter."))
                self.mqt.AddWidget(self.content, empty)
                self._dynamic_roots.append(empty)
                self._set_status(
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Expose a reflected CBuffer field to enable material editing"))
            for session in statuses:
                group = self.mqt.CreateGroupBox(True)
                title = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "%1 · buffer %2 · binding +%3")
                title = title.replace("%1", session["constantBlockDisplayName"] or session["constantBlockPath"])
                title = title.replace("%2", str(session["bufferResourceId"])).replace("%3", str(session["bufferByteOffset"]))
                self.mqt.SetWidgetText(group, title)
                body = self.mqt.CreateVerticalContainer()
                self.mqt.AddWidget(group, body)
                self.mqt.AddWidget(self.content, group)
                self._dynamic_roots.append(group)

                for parameter in session["parameters"]:
                    label = self.mqt.CreateLabel()
                    self.mqt.SetWidgetText(
                        label, "%s [%s%d]\n%s" % (
                            parameter["displayName"], parameter["baseType"],
                            parameter["components"], parameter["physicalIdentity"]))
                    self.mqt.AddWidget(body, label)

                    row = self.mqt.CreateHorizontalContainer()
                    session_id = session["sessionId"]
                    parameter_id = parameter["parameterId"]

                    def _changed(_ctx, widget, _text_value,
                                 sid=session_id, pid=parameter_id):
                        if self._building or not self._alive:
                            return
                        self._apply_widget_text(sid, pid, widget)

                    textbox = self.mqt.CreateTextBox(True, _changed)
                    self._editor_widgets[(session_id, parameter_id)] = textbox
                    self.mqt.SetWidgetText(textbox, self._format_value(parameter))
                    self.mqt.AddWidget(row, textbox)

                    def _apply_clicked(_ctx, _widget, _text_value,
                                       sid=session_id, pid=parameter_id,
                                       selected_widget=textbox):
                        if not self._alive or self.top is None:
                            return
                        self._apply_widget_text(sid, pid, selected_widget)

                    apply_button = self.mqt.CreateButton(_apply_clicked)
                    self.mqt.SetWidgetText(apply_button, QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Apply"))
                    self.mqt.AddWidget(row, apply_button)
                    self.mqt.AddWidget(body, row)

                    row_status = self.mqt.CreateLabel()
                    self.mqt.SetWidgetText(row_status, QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Captured baseline"))
                    self.mqt.AddWidget(body, row_status)
                    self._row_status[(session_id, parameter_id)] = row_status

                def _rollback_clicked(_ctx, _widget, _text_value,
                                      sid=session["sessionId"]):
                    if not self._alive or self.top is None:
                        return
                    self._queue_rollback(sid)

                rollback = self.mqt.CreateButton(_rollback_clicked)
                self.mqt.SetWidgetText(rollback, QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Clear this CBuffer's field edits"))
                self.mqt.AddWidget(body, rollback)
        finally:
            self._building = False

    def _apply_widget_text(self, session_id, parameter_id, widget):
        if not self._alive or self.top is None:
            return
        try:
            text = self.mqt.GetWidgetText(widget)
            self.controller.submit_text(session_id, parameter_id, text,
                lambda status, error: self._edit_completed(session_id, parameter_id, status, error), self)
        except Exception as exc:
            self._edit_completed(session_id, parameter_id, None, exc)

    def _edit_completed(self, session_id, parameter_id, status, error):
        if not self._alive or self.top is None:
            return
        if error is None and status is not None:
            if status.get("previewPublished"):
                message = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Applied revision %1 to shared buffer consumers (generation %2)")
            else:
                message = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Applied revision %1 (generation %2); preview is not confirmed")
            message = message.replace("%1", str(status["revision"])).replace("%2", str(status["generation"]))
            label = self._row_status.get((session_id, parameter_id))
            if label:
                self.mqt.SetWidgetText(label, message)
            self._set_status(message)
        elif error is not None:
            label = self._row_status.get((session_id, parameter_id))
            message = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Not applied: %1").replace("%1", _parameter_error_text(error))
            if label:
                self.mqt.SetWidgetText(label, message)
            self._set_status(message)

    def _queue_rollback(self, session_id):
        def complete(status, error):
            if not self._alive or self.top is None:
                return
            if error is not None:
                self._set_status(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Rollback failed: %1").replace("%1", str(error)))
            elif status is not None:
                self.rebuild()
                self._set_status(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Cleared and released material session %1").replace("%1", session_id))
        self.controller.submit_rollback(session_id, complete, self)

    def _rollback_all_clicked(self, _ctx, _widget, _text):
        if not self._alive or self.top is None:
            return
        try:
            for status in self.controller.list_status():
                self._queue_rollback(status["sessionId"])
        except Exception as exc:
            self._set_status(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Rollback failed: %1").replace("%1", str(exc)))

    def capture_changed(self):
        self.controller.forget_all()
        if self._alive and self._docked:
            self.rebuild()
            self._set_status(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Capture changed; all material-panel identities expired"))

    def shutdown(self, rollback=True):
        if not self._alive and self.top is None:
            if self.controller is not None:
                self.controller.forget_all()
            return
        controller = self.controller
        top = self.top
        mqt = self.mqt
        was_docked = self._docked
        self._cancel_pending_timers()
        if rollback and controller is not None:
            try:
                controller.rollback_all()
            except Exception as exc:
                print("[Bread] Material panel shutdown rollback error: %s" % str(exc))
        if controller is not None:
            controller.forget_all()
        self._alive = False
        self._callback_epoch += 1
        self._generation.clear()
        try:
            if top is not None and mqt is not None:
                if was_docked:
                    mqt.CloseToplevelWidget(top)
                else:
                    # The panel is constructed eagerly but may never be docked.
                    # MiniQt CloseToplevelWidget cannot delete that orphan.
                    mqt.DestroyWidget(top)
        except Exception:
            pass
        # DestroyWidget is deferred for the undocked case. Detach borrowed
        # handles synchronously and let the native callback become a no-op.
        self._closed(None, top, "")
