"""Transient Shader Workspace visual debugging, independent of saved source.

The injected operation executes on a worker and owns all replay mutations.
UI requests carry capture, source and generation identities; old completions
never mark a newer editor revision as active. Compatible with embedded Python 3.6.
"""
from __future__ import absolute_import

import hashlib
import os
import queue
import re
import threading
import uuid

try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:
    QtCore = QtGui = QtWidgets = None


def component_count(type_name):
    """Numeric leaf types arrive already resolved by the instrumenter."""
    value = str(type_name or "")
    vector = re.fullmatch(r"(?:[biud]?vec)([1-4])", value)
    if vector:
        return int(vector.group(1))
    vector = re.fullmatch(r"(?:bool|int|uint|float|half|double|min16float|min10float|min16int|min12int|min16uint|(?:float|u?int)(?:16|32|64)_t)([1-4])?", value)
    return int(vector.group(1) or 1) if vector else 4


def source_request(document, workspace, binding, byte_start, byte_end, event_id):
    source = str(document["editor"].GetText())
    raw = source.encode("utf-8")
    start, end = int(byte_start), int(byte_end)
    if not 0 <= start < end <= len(raw):
        raise ValueError("Invalid UTF-8 expression range")
    # Reject byte offsets inside a multibyte codepoint, instead of silently
    # debugging an adjacent identifier after a translated comment.
    raw[:start].decode("utf-8")
    raw[start:end].decode("utf-8")
    path = str(document.get("sourcePath") or workspace.get("sourcePath") or "")
    extension = os.path.splitext(path)[1].lower()
    return {
        "captureBinding": dict(binding or {}),
        "workspaceId": str(document.get("workspaceId") or ""),
        "workspacePath": str(workspace.get("workspacePath") or ""),
        "sourceWorkspaceId": str(workspace.get("sourceWorkspaceId") or document.get("sourceWorkspaceId") or workspace.get("workspaceId") or document.get("workspaceId") or ""),
        "sourcePath": path, "rootSourcePath": str(workspace.get("rootSourcePath") or path),
        "includeRoot": str(workspace.get("includeRoot") or ""),
        "source": source, "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "byteStart": start, "byteEnd": end,
        "entryPoint": str(document.get("entryPoint") or workspace.get("entryPoint") or "main"),
        "eventId": int(event_id), "shaderId": str(workspace.get("resourceId") or ""),
        "scope": "shader-resource", "persistentAcrossEvents": True,
        "expectedOriginalSha256": str(workspace.get("shaderRawSha256") or ""),
        "targetProfile": workspace.get("targetProfile") or "",
        "stage": str(workspace.get("stage") or ""),
        "language": str(workspace.get("language") or ("glsl" if extension in (".glsl", ".frag", ".vert", ".comp", ".geom", ".tesc", ".tese") else "hlsl")),
    }


def persistent_receipt_matches(request, result):
    """Display the persistent scope only for the requested original Shader."""
    expected = str(request.get("shaderId") or "").split("::")[-1]
    actual = str(result.get("shaderId") or "").split("::")[-1]
    return (result.get("scope") == "shader-resource" and result.get("resourceScoped") is True
            and result.get("persistentAcrossEvents") is True and result.get("eventScoped") is False
            and result.get("independentFloatOutput") is not True
            and (not expected or actual == expected))


def debug_anchor_event(workspace, event_ids, selected_event, previous=None):
    """Choose a known use of this document without changing UI selection."""
    if previous and previous.get("scope") == "shader-resource":
        anchor = previous.get("eventId")
        if isinstance(anchor, int) and not isinstance(anchor, bool) and anchor > 0:
            return anchor
    events = [value for value in event_ids
              if isinstance(value, int) and not isinstance(value, bool) and value > 0]
    if selected_event in events:
        return selected_event
    anchor = workspace.get("eventId")
    if isinstance(anchor, int) and not isinstance(anchor, bool) and anchor > 0:
        return anchor
    # Older workspaces can lack the usage catalog. The replay adapter resolves
    # their original Shader's usage before inspecting an unrelated current EID.
    return events[0] if events else selected_event


class VisualDebugRequestState(object):
    """Revision identities also used by tests without a Qt runtime."""
    def __init__(self):
        self.owner_id = uuid.uuid4().hex
        self.generation = 0

    def next(self, parameters):
        self.generation += 1
        result = dict(parameters)
        result.update(ownerId=self.owner_id, requestGeneration=self.generation)
        return result

    def accepts(self, parameters, document):
        if parameters.get("requestGeneration") != self.generation or document is None:
            return False
        return hashlib.sha256(str(document["editor"].GetText()).encode("utf-8")).hexdigest() == parameters.get("sourceSha256")


if QtWidgets is not None:
    class _DebugButtonLayout(QtWidgets.QLayout):
        """Keep commands readable by wrapping whole buttons in narrow docks."""
        def __init__(self):
            super(_DebugButtonLayout, self).__init__()
            self._items = []
            self.setContentsMargins(0, 0, 0, 0)
            self.setSpacing(4)

        def addItem(self, item):
            self._items.append(item)

        def count(self):
            return len(self._items)

        def itemAt(self, index):
            return self._items[index] if 0 <= index < len(self._items) else None

        def takeAt(self, index):
            return self._items.pop(index) if 0 <= index < len(self._items) else None

        def expandingDirections(self):
            return QtCore.Qt.Orientations(0)

        def hasHeightForWidth(self):
            return True

        def heightForWidth(self, width):
            return self._arrange(QtCore.QRect(0, 0, width, 0), False)

        def minimumSize(self):
            size = QtCore.QSize()
            for item in self._items:
                size = size.expandedTo(item.minimumSize())
            return size

        def sizeHint(self):
            width = sum(item.sizeHint().width() for item in self._items)
            width += max(0, len(self._items) - 1) * self.spacing()
            return QtCore.QSize(width, self.minimumSize().height())

        def setGeometry(self, rect):
            super(_DebugButtonLayout, self).setGeometry(rect)
            self._arrange(rect, True)

        def _arrange(self, rect, apply):
            x, y, row_height = rect.x(), rect.y(), 0
            for item in self._items:
                size = item.sizeHint()
                if x > rect.x() and x + size.width() > rect.x() + rect.width():
                    x, y, row_height = rect.x(), y + row_height + self.spacing(), 0
                if apply:
                    item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), size))
                x += size.width() + self.spacing()
                row_height = max(row_height, size.height())
            return y + row_height - rect.y()


class ShaderVisualDebugController(object):
    def __init__(self, panel, operation, layout):
        self.panel = panel
        self.operation = operation
        self.state = VisualDebugRequestState()
        self.results = queue.Queue()
        self.session_id = ""
        self.request = None
        self.active = False
        self.busy = False
        self.closed = False
        self._setting_options = False
        self._after_stop = []
        self._stopping = False
        self._output_viewer = None
        self._output_view_token = 0
        self._output_view_started = False
        self.selection = None
        self.paused = False
        self._follow_ready = False
        self._follow_enabled = False
        self._snapshot_preview_notice = False
        self.row = QtWidgets.QWidget(panel.top)
        self.row.setObjectName("breadShaderVisualDebugControls")
        rows = QtWidgets.QVBoxLayout(self.row)
        rows.setContentsMargins(0, 0, 0, 0)
        controls = _DebugButtonLayout()
        rows.addLayout(controls)
        self.start_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Output to RT (Ctrl+E)"), self.row)
        self.start_button.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Output the value at the caret in every draw using this Shader (Ctrl+E). Debugging stays active when the selected EID changes."))
        self.start_button.clicked.connect(self.request_current)
        controls.addWidget(self.start_button)
        self.stop_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Restore (Ctrl+Shift+E)"), self.row)
        self.stop_button.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Restore the rendering from before debugging (Ctrl+Shift+E)"))
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.stop_button)
        self.compare_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Hold to compare"), self.row)
        self.compare_button.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Hold to show the rendering from before debugging; release to return"))
        self.compare_button.pressed.connect(lambda: self.compare(True))
        self.compare_button.released.connect(lambda: self.compare(False))
        controls.addWidget(self.compare_button)
        self.snapshot_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Save debug snapshot"), self.row)
        self.snapshot_button.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Save this debug location and display settings (Ctrl+Shift+B)"))
        self.snapshot_button.clicked.connect(self.save_snapshot)
        controls.addWidget(self.snapshot_button)
        self.snapshot_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Shift+B"), panel.top)  # i18n-ignore: portable keyboard shortcut, not user-visible prose.
        self.snapshot_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.snapshot_shortcut.activated.connect(self.save_snapshot)
        self.options_button = QtWidgets.QToolButton(self.row)
        self.options_button.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug options"))
        self.options_button.setCheckable(True)
        self.options_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.options_button.setArrowType(QtCore.Qt.RightArrow)
        self.options_button.toggled.connect(self._show_options)
        controls.addWidget(self.options_button)
        self.label = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Click a value in the code, then press Ctrl+E. Ctrl+Shift+E restores the rendering."), self.row)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        rows.addWidget(self.label)
        self.options_panel = QtWidgets.QWidget(self.row)
        self.options_panel.setVisible(False)
        settings = QtWidgets.QFormLayout(self.options_panel)
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        settings.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        rows.addWidget(self.options_panel)
        self.mode = QtWidgets.QComboBox(self.options_panel)
        self.mode.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Value display"))
        self.mode.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Custom (automatic channels)"), "custom")  # i18n-ignore: item data is a stable backend display-mode ID.
        self.mode.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Raw RGBA"), "raw")  # i18n-ignore: item data is a stable backend display-mode ID.
        self.mode.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Grayscale"), "grayscale")  # i18n-ignore: item data is a stable backend display-mode ID.
        self.mode.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Normal: -1 to 1"), "normal")  # i18n-ignore: item data is a stable backend display-mode ID.
        self.mode.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Custom range"), "range")  # i18n-ignore: item data is a stable backend display-mode ID.
        self.mode.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Execution coverage"), "coverage")  # i18n-ignore: item data is a stable backend display-mode ID.
        self.mode.currentIndexChanged.connect(self.options_changed)
        settings.addRow(QtCore.QCoreApplication.translate("ReplayLabProjects", "Value display"), self.mode)
        self.range_row = QtWidgets.QWidget(self.options_panel)
        range_layout = QtWidgets.QHBoxLayout(self.range_row)
        range_layout.setContentsMargins(0, 0, 0, 0)
        self.range_min = QtWidgets.QDoubleSpinBox(self.range_row)
        self.range_max = QtWidgets.QDoubleSpinBox(self.range_row)
        self.range_min.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Display range minimum"))
        self.range_max.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Display range maximum"))
        for control in (self.range_min, self.range_max):
            control.setRange(-1.0e12, 1.0e12)
            control.setDecimals(5)
            control.setMinimumWidth(0)
            control.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            control.setVisible(False)
            control.editingFinished.connect(self.options_changed)
            range_layout.addWidget(control)
        self.range_max.setValue(1.0)
        settings.addRow(QtCore.QCoreApplication.translate("ReplayLabProjects", "Display range"), self.range_row)
        self.range_label = settings.labelForField(self.range_row)
        self.range_label.setVisible(False)
        self.range_row.setVisible(False)
        self.target = QtWidgets.QComboBox(self.options_panel)
        self.target.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug render target"))
        self.target.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Automatic RT"), -1)
        self.target.currentIndexChanged.connect(self.options_changed)
        settings.addRow(QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug render target"), self.target)
        self.float_preview = QtWidgets.QCheckBox(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Independent RGBA32F preview"), self.options_panel)
        self.float_preview.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Persistent debugging writes to the captured render target, so later draws can use the result. Independent previews do not feed later rendering. Captured blending and write masks still apply."))
        self.float_preview.setChecked(False)
        self.float_preview.setEnabled(False)
        self.float_preview.toggled.connect(self.options_changed)
        settings.addRow(self.float_preview)
        self.channel = QtWidgets.QComboBox(self.options_panel)
        self.channel.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Channel mapped to the render target"))
        for channel_index, channel_name in enumerate(("RGBA", "R", "G", "B", "A")):
            self.channel.addItem(channel_name, channel_index - 1)  # i18n-ignore: standard channel identifiers, not translated prose.
        self.channel.currentIndexChanged.connect(self.options_changed)
        settings.addRow(QtCore.QCoreApplication.translate("ReplayLabProjects", "Channel"), self.channel)
        self.iteration = QtWidgets.QSpinBox(self.options_panel)
        self.iteration.setRange(1, 65536)
        self.iteration.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Execution occurrence: 1 is the first time this expression is reached"))
        settings.addRow(QtCore.QCoreApplication.translate("ReplayLabProjects", "Occurrence:"), self.iteration)
        self.iteration.editingFinished.connect(self.options_changed)
        layout.addWidget(self.row)
        self.observation_note = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Observation preview: later calculations may still run."), self.options_panel)
        self.observation_note.setWordWrap(True)
        self.observation_note.setToolTip(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "The selected value is observed at this expression. Later calculations and resource writes may still execute to preserve derivatives. Later discard or clip is ignored after observation; fragments that never reach the expression are marked."))
        settings.addRow(self.observation_note)
        self.timer = QtCore.QTimer(panel.top)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.drain)
        self.timer.start()
        self.update_controls()

    def _show_options(self, visible):
        self.options_panel.setVisible(visible)
        self.options_button.setArrowType(QtCore.Qt.DownArrow if visible else QtCore.Qt.RightArrow)

    def update_controls(self):
        self.snapshot_button.setEnabled(bool(self.request and self.selection) and not self.closed and not self._stopping)
        self.start_button.setEnabled(callable(self.operation) and not self.closed and not self._stopping)
        self.stop_button.setEnabled(bool(self.request or self.session_id or self.busy))
        self.compare_button.setEnabled(self.active and bool(self.session_id) and not self._stopping)
        status_font = self.label.font()
        status_font.setBold(bool(self.session_id) or self.paused)
        self.label.setFont(status_font)

    def bind_editor(self, document):
        editor = document.get("editor")
        if editor is not None and hasattr(editor, "SetVisualDebugCallback"):
            workspace_id = document["workspaceId"]
            editor.SetVisualDebugCallback(lambda start, end: self.begin(workspace_id, start, end))
            editor.SetStopVisualDebugCallback(self.stop)

    def request_current(self):
        document = self.panel._current_document()
        if document and hasattr(document["editor"], "RequestVisualDebug"):
            document["editor"].RequestVisualDebug()
        else:
            self.label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Open a Shader in the workspace before starting visual debugging."))

    def save_snapshot(self, *_unused):
        view = getattr(self.panel, "debug_snapshots", None)
        if view is not None:
            view.add_current()

    def open_source_file(self):
        if self.closed or not callable(self.operation):
            return
        filename, _filter = QtWidgets.QFileDialog.getOpenFileName(self.panel.top,
            QtCore.QCoreApplication.translate("ReplayLabProjects", "Open source file for this draw"), "",
            QtCore.QCoreApplication.translate("ReplayLabProjects", "Shader source (*.hlsl *.glsl *.frag *.hlsli);;All files (*)"))
        if filename:
            self.capture_current(source_file=filename)

    def capture_current(self, _checked=False, source_file=None):
        if self.closed or not callable(self.operation):
            return
        if self.request or self.session_id:
            self.stop_then(lambda: self.capture_current(source_file=source_file))
            return
        binding = self.panel._current_capture_binding()
        if not binding:
            self.label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Open a capture and select a draw before opening its Shader source."))
            return
        request = self.state.next({"captureBinding": binding, "eventId": int(self.panel.ctx.CurEvent())})
        if source_file:
            request["sourceFile"] = str(source_file)
        self._invalidate(request)
        self.busy = True
        self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Reading Shader source…"))
        self.submit("capture-source", request)
        self.update_controls()

    def _invalidate(self, request):
        # This operation changes a cheap generation token only and must never
        # wait for replay/compilation. It makes a stop effective before a worker
        # has finished compiling an older candidate.
        if callable(self.operation):
            self.operation("invalidate", {"ownerId": self.state.owner_id,
                "requestGeneration": request["requestGeneration"]})

    def submit(self, operation, request):
        callback, output = self.operation, self.results
        def run():
            try:
                result = callback(operation, dict(request))
                if not isinstance(result, dict):
                    result = {"status": "error", "reason": "invalid-response", "message": "Invalid debug response"}
            except Exception as exc:
                result = {"status": "error", "reason": "operation-failed", "message": str(exc)}
            output.put((operation, request, result))
        worker = threading.Thread(target=run, name="BreadShaderVisualDebug")
        worker.daemon = True
        worker.start()

    def begin(self, workspace_id, start, end, snapshot=None):
        if self.closed or self._stopping or not callable(self.operation):
            return
        document = self.panel._documents.get(workspace_id)
        binding = self.panel._current_capture_binding()
        if (not document or not document.get("available") or not binding or
                document.get("captureBinding") != binding):
            self.label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Open the matching capture before debugging this Shader."))
            return
        if document.get("saveInFlight") or document.get("commitUnknown") or document.get("switchInFlight"):
            self.label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Wait for the current Shader save or switch to finish."))
            return
        workspace = self.panel._document_model(workspace_id) or document.get("workspace") or {}
        try:
            previous = getattr(self, "request", None) or {}
            if (not self.session_id or previous.get("workspaceId") != workspace_id or
                    previous.get("captureBinding") != binding or
                    previous.get("shaderId") != str(workspace.get("resourceId") or "")):
                previous = None
            event_id = debug_anchor_event(workspace,
                getattr(self.panel, "_workspace_event_ids", {}).get(workspace_id, ()),
                int(self.panel.ctx.CurEvent()), previous)
            request = source_request(document, workspace, binding, start, end, event_id)
            if previous:
                request["sessionId"] = self.session_id
            request = self.state.next(request)
            self._invalidate(request)
        except Exception as exc:
            self.show_error({"message": str(exc)})
            return
        self.request = request
        self.selection = None
        self.paused = False
        self._follow_ready = False
        self._follow_enabled = True
        self._snapshot_preview_notice = False
        if snapshot is not None:
            request["snapshotMember"] = str(snapshot.get("member") or "")
            request["snapshotOptions"] = dict(snapshot.get("options") or {})
        self.busy = True
        if getattr(self.panel, "check_debounce_timer", None) is not None:
            self.panel.check_debounce_timer.stop()
        self.label.setText(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Resolving the expression and available render targets…"))
        self.submit("inspect", request)
        self.update_controls()

    def _options(self, request):
        value = dict(request)
        value.update(mode=str(self.mode.currentData()), targetIndex=int(self.target.currentData()),
                     independentFloatOutput=False,
                     rangeMin=float(self.range_min.value()), rangeMax=float(self.range_max.value()),
                     iteration=int(self.iteration.value()) - 1, channel=int(self.channel.currentData()))
        return value

    def _restore_options(self, options):
        saved_target = int(options.get("targetIndex", -1))
        if saved_target >= 0 and self.target.findData(saved_target) < 0:
            self.label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "The snapshot's render target is unavailable on this draw. Choose another target and debug the value again."))
            return False
        self._setting_options = True
        try:
            for control, key in ((self.mode, "mode"), (self.target, "targetIndex"), (self.channel, "channel")):
                index = control.findData(options.get(key, "custom" if key == "mode" else -1))
                control.setCurrentIndex(max(0, index))
            self.range_min.setValue(float(options.get("rangeMin", 0.0)))
            self.range_max.setValue(float(options.get("rangeMax", 1.0)))
            self._snapshot_preview_notice = bool(options.get("independentFloatOutput", False))
            self.float_preview.setChecked(False)
            self.iteration.setValue(int(options.get("iteration", 0)) + 1)
            self.options_changed()
        finally:
            self._setting_options = False
        return True

    def _set_value_type(self, type_name):
        channel = getattr(self, "channel", None)
        if channel is None:
            return
        width = component_count(type_name)
        self._setting_options = True
        try:
            for index in range(1, 5):
                item = channel.model().item(index)
                if item is not None:
                    item.setEnabled(index <= width)
            if int(channel.currentData()) >= width:
                channel.setCurrentIndex(0)
        finally:
            self._setting_options = False

    def follow_event(self, event_id):
        if not self.request or self._stopping:
            return
        if self.request.get("scope") == "shader-resource":
            # The event is an inspection anchor, not the lifetime/scope of
            # the replacement. Navigation must not cancel or rebuild it.
            return
        document = self.panel._documents.get(self.request.get("workspaceId"))
        if (not self.state.accepts(self.request, document) or
                self.request.get("captureBinding") != self.panel._current_capture_binding()):
            self.stop()
            return
        request = dict(self.request, eventId=int(event_id), sessionId=self.session_id)
        request["inspectOnly"] = not getattr(self, "_follow_ready", False)
        request = self.state.next(request)
        self._invalidate(request)
        self.request = request
        self.busy = True
        self.active = False
        self.label.setText(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Following the debug value at EID %1…").replace("%1", str(event_id)))
        self.submit("follow", request)
        self.update_controls()

    def options_changed(self, *_unused):
        if not hasattr(self, "range_min"):
            return
        is_range = self.mode.currentData() == "range"
        self.range_min.setVisible(is_range)
        self.range_max.setVisible(is_range)
        self.range_row.setVisible(is_range)
        self.range_label.setVisible(is_range)
        if self._setting_options or self._stopping or not self.request or (not self.active and not self.paused):
            return
        if is_range and self.range_max.value() <= self.range_min.value():
            self.label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "The display range maximum must be greater than its minimum."))
            return
        self.request = self.state.next(self._options(self.request))
        self._invalidate(self.request)
        self.busy = True
        if self.session_id:
            self.request["sessionId"] = self.session_id
        if self.paused:
            self.request.update(sessionId=self.session_id, inspectOnly=not self._follow_ready)
        self.submit("follow" if self.paused else "start", self.request)
        self.update_controls()

    def stop(self, *_unused):
        if self._stopping or not callable(self.operation):
            return
        request = self.state.next(self.request or {})
        request["sessionId"] = self.session_id
        self._invalidate(request)
        self._stopping = True
        self._follow_ready = False
        self._follow_enabled = False
        self.busy = True
        self.label.setText(QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Restoring the rendering from before debugging…"))
        self.submit("stop", request)
        self.update_controls()

    def stop_then(self, callback):
        self._after_stop.append(callback)
        self.stop()

    def compare(self, original):
        if self.closed or self._stopping or not self.session_id or not self.active or not self.request:
            return
        self.request = self.state.next(self.request)
        self._invalidate(self.request)
        request = dict(self.request, sessionId=self.session_id, original=bool(original))
        self.submit("compare", request)

    def clear_marks(self):
        for document in self.panel._documents.values():
            editor = document.get("editor")
            if editor is not None and hasattr(editor, "SetVisualDebugRange"):
                editor.SetVisualDebugRange(0, 0, False)

    def show_output(self, index, preview=None):
        # Native tokens retain the actual following/tab/type state and expire
        # on manual navigation. Once released, option changes do not take the
        # user's texture viewer back until a new debug session is started.
        if self._output_view_started:
            if self._output_viewer is not None and self._output_view_token:
                try:
                    updated = self._output_viewer.UpdateTemporaryOutputFollow(self._output_view_token, int(index))
                    if updated and preview is not None:
                        updated = self._output_viewer.ShowTemporaryTexturePreview(self._output_view_token, preview)
                    if not updated:
                        # A missing thumbnail can also reject the update while
                        # the native token is still owned. End releases that
                        # snapshot; an already-abandoned token is a safe no-op.
                        self._output_viewer.EndTemporaryOutputFollow(self._output_view_token)
                        self._output_view_token = 0
                except RuntimeError:
                    self._output_view_token = 0
            return
        self._output_view_started = True
        try:
            viewer = self.panel.ctx.GetTextureViewer()
            if hasattr(viewer, "BeginTemporaryOutputFollow"):
                self._output_viewer = viewer
                self._output_view_token = int(viewer.BeginTemporaryOutputFollow(int(index)))
                if self._output_view_token and preview is not None:
                    if not viewer.ShowTemporaryTexturePreview(self._output_view_token, preview):
                        viewer.EndTemporaryOutputFollow(self._output_view_token)
                        self._output_view_token = 0
        except RuntimeError:
            self._output_view_token = 0

    def restore_output_view(self):
        viewer = getattr(self, "_output_viewer", None)
        token = getattr(self, "_output_view_token", 0)
        self._output_viewer = None
        self._output_view_token = 0
        self._output_view_started = False
        if viewer is not None and token:
            try:
                viewer.EndTemporaryOutputFollow(token)
            except RuntimeError:
                pass

    def source_changed(self, workspace_id):
        if self.request and self.request.get("workspaceId") == workspace_id:
            self.clear_marks()
            self.stop()

    def shutdown(self):
        if self.closed:
            return
        # A forced parent destruction may already have deleted child widgets.
        # Cleanup must depend only on the operation and immutable identities,
        # never on QLabel/Scintilla lifetime or the result-polling timer.
        if (self.request or self.session_id or self.busy) and not self._stopping:
            request = self.state.next(self.request or {})
            request["sessionId"] = self.session_id
            self._invalidate(request)
            self.submit("stop", request)
        self.restore_output_view()
        self.closed = True
        try:
            self.timer.stop()
        except RuntimeError:
            pass

    def show_error(self, result):
        reason = str(result.get("reason") or "")
        if reason in ("unsupported-stage", "fragment-stage-required", "stage-unsupported"):
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "RT output debugging requires a pixel or fragment Shader.")
        elif reason in ("unsupported-expression", "unsafe-expression", "unsupported-source"):
            message = self._expression_error(result)
        elif reason in ("no-render-target", "no-output", "no-color-target"):
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "This draw has no compatible color render target.")
        elif reason == "not-draw":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Select a draw call to preview this Shader value.")
        elif reason in ("stale-source", "stale-capture", "stale-request", "capture-changed", "event-changed", "shader-changed", "superseded"):
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "The source or capture changed. Request visual debugging again.")
        elif reason in ("session-changed", "replacement-changed"):
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "The active Shader replacement changed. Stop or restart visual debugging to reconcile its state.")
        elif reason == "interface-mismatch":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "The debug Shader has an incompatible interface and was not applied.")
        elif reason == "compile-failed":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug Shader compilation failed: %1").replace("%1", str(result.get("message") or ""))
        elif reason == "compiler-unavailable":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "The compiler required for this Shader source and graphics API is unavailable: %1").replace("%1", str(result.get("message") or ""))
        elif reason == "source-unavailable":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "This capture has no editable Shader source. Open a source file for the selected draw.")
        elif reason == "runtime-outdated":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Restart Bread to enable the current visual debugging runtime.")
        elif reason == "independent-preview-unsupported":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Persistent debugging must use the actual render target. Independent previews do not feed later rendering.")
        elif reason == "channel-out-of-range" or result.get("instrumentationReason") == "channel-out-of-range":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "This value does not have the selected channel. Choose an available channel or automatic channels.")
        elif reason.startswith("include-"):
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Could not resolve the Shader compilation unit: %1").replace("%1", str(result.get("message") or ""))
        else:
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Visual debugging failed: %1").replace("%1", str(result.get("message") or reason))
        self.label.setText(message)
        diagnostic = str(result.get("message") or result.get("instrumentationReason") or reason)
        self.label.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Diagnostic: %1").replace("%1", diagnostic))

    def _expression_error(self, result):
        code = str(result.get("instrumentationReason") or "")
        if code == "channel-out-of-range":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This value does not have the selected channel. Choose an available channel or automatic channels.")
        if code in ("select-value", "select-executable-value", "invalid-source-position", "invalid-utf8-position"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Select a variable, function call or return expression inside a function body.")
        if code in ("select-single-value-access", "unsupported-value-expression"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Select one complete value expression. A selection spanning multiple statements cannot be displayed.")
        if code == "partial-expression-selection":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "The selection is not a complete subexpression of the original expression. Select the whole expression or one of its values.")
        if code == "uninitialized-declaration":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This variable has no value at its declaration. Select a use after it has been assigned.")
        if code == "unresolved-value":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "The selected value's declaration or type could not be resolved at this position.")
        if code in ("unresolved-member", "unsupported-member-type", "invalid-swizzle"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "The selected member or component could not be resolved. Select the containing value to choose a member.")
        if code == "ambiguous-call-type":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This function call matches more than one return type. Assign its result to an explicitly typed local variable and select that variable.")
        if code == "unsupported-call-type":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "The return type of this function call is not supported. Select an argument or assign the result to an explicitly typed local variable.")
        if code in ("value-not-numeric", "member-selection-required"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This selection has no directly displayable numeric value. Select a numeric member or component.")
        if code in ("array-access-needs-element-selection", "array-element-required", "select-array-element", "type-expansion-limit"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Select a specific array element or matrix component to display.")
        if code == "out-argument-position":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This value is an out or inout argument. Select its use after the function call.")
        if code in ("compound-declaration-position", "loop-declaration-position"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This declaration is part of a loop header or multiple declarations. Select a later use of the variable.")
        if code in ("macro-position-unavailable", "preprocessed-selection-ambiguous"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "This macro selection cannot be mapped to one expression. Select a value in the surrounding code.")
        if code in ("unsupported-function-parameter", "unsupported-struct-declaration", "unsupported-declaration"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "A declaration in this Shader could not be analysed. The diagnostic identifies the declaration that needs attention.")
        if code in ("unbalanced-source", "incomplete-statement", "incomplete-assignment"):
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "The Shader contains an incomplete statement or unmatched delimiter. Complete the source before previewing a value.")
        if code == "entry-point-ambiguous":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "The Shader entry point could not be identified uniquely. Check its name and the selected source file.")
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "This expression is not supported for visual debugging (%1): %2").replace(
            "%1", code or str(result.get("reason") or "")).replace("%2", str(result.get("message") or ""))

    def drain(self):
        if (not self.closed and self.request and not self._stopping
                and self.request.get("scope") == "shader-resource"
                and (self.panel._documents.get(self.request.get("workspaceId")) is None
                     or self.request.get("captureBinding") != self.panel._current_capture_binding())):
            self.stop()
        if (not self.closed and self.request and not self._stopping
                and self.request.get("scope") != "shader-resource"
                and getattr(self, "_follow_enabled", True)):
            try:
                if int(self.panel.ctx.CurEvent()) != int(self.request.get("eventId", 0)):
                    self.follow_event(int(self.panel.ctx.CurEvent()))
            except Exception:
                pass
        while not self.closed:
            try:
                operation, request, result = self.results.get_nowait()
            except queue.Empty:
                return
            if request.get("requestGeneration") != self.state.generation:
                continue
            self.busy = False
            self.label.setToolTip("")
            if result.get("status") == "error":
                recovery = result.get("recoverySession") or {}
                if result.get("activePreview") and recovery.get("sessionId"):
                    # The native override committed even though its texture
                    # receipt and rollback failed. This is a real active
                    # session: retain Stop and mark the committed expression.
                    self.session_id = str(recovery["sessionId"])
                    self.request = request
                    self.active = True
                    self._stopping = False
                    document = self.panel._documents.get(request.get("workspaceId"))
                    if (self.state.accepts(request, document) and
                            request.get("captureBinding") == self.panel._current_capture_binding()):
                        self.clear_marks()
                        selection = recovery.get("selection") or {}
                        document["editor"].SetVisualDebugRange(
                            int(selection.get("byteStart", request["byteStart"])),
                            int(selection.get("byteEnd", request["byteEnd"])), True)
                    self.label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Restoration failed; the debug session still needs to be stopped: %1").replace(
                            "%1", str(result.get("message") or result.get("reason") or "")))
                    self.update_controls()
                    continue
                if operation == "stop":
                    self._stopping = False
                    self._after_stop = []
                    self.label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Restoration failed; the debug session still needs to be stopped: %1").replace(
                            "%1", str(result.get("message") or result.get("reason") or "")))
                    self.update_controls()
                    continue
                if operation == "follow":
                    if result.get("restored") is True:
                        self.session_id = ""
                    self.active = False
                    self.paused = True
                self.show_error(result)
                self.update_controls()
                continue
            if operation == "stop":
                self._stopping = False
                self.session_id = ""
                self.request = None
                self.selection = None
                self.active = False
                self.paused = False
                self.clear_marks()
                self.restore_output_view()
                if getattr(self.panel, "check_debounce_timer", None) is not None:
                    self.panel.check_debounce_timer.start()
                self.label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Visual debugging stopped; previous rendering restored."))
                self.update_controls()
                callbacks, self._after_stop = self._after_stop, []
                for callback in callbacks:
                    callback()
                continue
            if operation == "capture-source":
                if (request.get("captureBinding") != self.panel._current_capture_binding() or
                        int(request["eventId"]) != int(self.panel.ctx.CurEvent())):
                    continue
                if self.panel.open_capture_source(result):
                    self.label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Shader source opened. Edits remain a local preview; Ctrl+S exports a copy."))
                else:
                    self.show_error({"reason": "source-unavailable", "message": result.get("message") or ""})
                self.update_controls()
                continue
            document = self.panel._documents.get(request.get("workspaceId"))
            if (not self.state.accepts(request, document) or
                    request.get("captureBinding") != self.panel._current_capture_binding()):
                self.stop()
                continue
            if result.get("status") == "paused":
                if request.get("scope") == "shader-resource":
                    self.active = False
                    self.stop()
                    self.show_error({"reason": "runtime-outdated"})
                    self.update_controls()
                    continue
                self.session_id = ""
                self.active = False
                self.paused = True
                self.restore_output_view()
                self.label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Visual debugging paused at EID %1: this draw does not use the selected Shader or a compatible color target. It will resume on a matching draw.").replace("%1", str(request.get("eventId", ""))))
                self.label.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Diagnostic: %1").replace("%1", str(result.get("message") or result.get("reason") or "")))
                self.update_controls()
                continue
            if operation == "inspect" or (operation == "follow" and result.get("status") == "ready"):
                choices = list(result.get("choices") or [])
                # Every new source/selection is inspected independently. Never
                # carry a member chosen for an earlier structure into this plan.
                request.pop("member", None)
                saved_member = request.pop("snapshotMember", None)
                if saved_member is not None:
                    if not any(str(item.get("path") or "") == saved_member for item in choices):
                        self.label.setText(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "The snapshot's selected member no longer exists. Select the value again to choose a current member."))
                        self.update_controls()
                        continue
                    request["member"] = saved_member
                elif len(choices) > 1 or any(item.get("path") for item in choices):
                    expression = str((result.get("selection") or {}).get("expression") or
                        request["source"].encode("utf-8")[request["byteStart"]:request["byteEnd"]].decode("utf-8"))  # i18n-ignore: UTF-8 codec and the selected user-authored Shader expression.
                    labels = ["%s%s (%s)" % (expression, item.get("path", ""), item.get("type", "")) for item in choices]
                    selected, accepted = QtWidgets.QInputDialog.getItem(self.panel.top,
                        QtCore.QCoreApplication.translate("ReplayLabProjects", "Choose a value to output"),
                        QtCore.QCoreApplication.translate("ReplayLabProjects", "Select a member or component of %1:").replace("%1", expression),
                        labels, 0, False)
                    # The dialog runs a nested event loop. A newer request must
                    # survive an old dialog closing, including Cancel.
                    if request.get("requestGeneration") != self.state.generation:
                        continue
                    document = self.panel._documents.get(request.get("workspaceId"))
                    if (not accepted or selected not in labels or
                            not self.state.accepts(request, document) or
                            request.get("captureBinding") != self.panel._current_capture_binding()):
                        self.stop()
                        continue
                    request["member"] = str(choices[labels.index(selected)].get("path") or "")
                elif choices:
                    request["member"] = str(choices[0].get("path") or "")
                self._setting_options = True
                previous_target = self.target.currentData()
                self.target.clear()
                self.target.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "Automatic RT"), -1)
                for target in result.get("targets") or []:
                    self.target.addItem(QtCore.QCoreApplication.translate("ReplayLabProjects", "RT %1 (%2 channels)").replace(
                        "%1", str(target.get("index", 0))).replace("%2", str(target.get("components", 4))), int(target.get("index", 0)))
                index = self.target.findData(previous_target)
                self.target.setCurrentIndex(max(0, index))
                self._setting_options = False
                selected_type = next((item.get("type") for item in choices if str(item.get("path") or "") == request.get("member", "")), "")
                if "snapshotOptions" in request:
                    snapshot_options = request.pop("snapshotOptions")
                    if int(snapshot_options.get("channel", -1)) >= component_count(selected_type):
                        self._set_value_type(selected_type)
                        self.label.setText(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "This value does not have the selected channel. Choose an available channel or automatic channels."))
                        self.update_controls()
                        continue
                    if not self._restore_options(snapshot_options):
                        self.update_controls()
                        continue
                self._set_value_type(selected_type)
                self.selection = dict(result.get("selection") or {})
                self._follow_ready = True
                request.pop("inspectOnly", None)
                request = self._options(request)
                self.request = request
                self.busy = True
                self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Compiling and applying visual debug output…"))
                self.submit("start", request)
            elif operation in ("start", "follow"):
                self.session_id = str(result.get("sessionId") or "")
                self.active = result.get("status") == "active" and bool(self.session_id)
                self.paused = False
                if (self.active and request.get("scope") == "shader-resource"
                        and not persistent_receipt_matches(request, result)):
                    # An older event-only backend cannot satisfy a persistent
                    # request. Retain its token for cleanup without showing a
                    # misleading persistent mark or scope label.
                    self.active = False
                    self.stop()
                    self.show_error({"reason": "runtime-outdated"})
                    self.update_controls()
                    continue
                if self.active:
                    self.show_output(result.get("targetIndex", 0), result.get("previewTexture"))
                    self.clear_marks()
                    selection = result.get("selection") or {}
                    self.selection = dict(selection)
                    start = int(selection.get("byteStart", request["byteStart"]))
                    end = int(selection.get("byteEnd", request["byteEnd"]))
                    document["editor"].SetVisualDebugRange(start, end, True)
                    expression = str(selection.get("expression") or request["source"].encode("utf-8")[start:end].decode("utf-8"))  # i18n-ignore: encoding identifier and user-authored Shader expression, not UI prose.
                    expression += str(request.get("member") or "")
                    if result.get("scope") == "shader-resource":
                        message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Visual debug active: %1 → RT %2. Shader %3 stays overridden across EIDs; later draws use its output.").replace(
                            "%1", expression).replace("%2", str(result.get("targetIndex", ""))).replace(
                            "%3", str(result.get("shaderId") or request.get("shaderId") or ""))
                        if getattr(self, "_snapshot_preview_notice", False):
                            message += "\n" + QtCore.QCoreApplication.translate("ReplayLabProjects", "This snapshot used an independent preview. Persistent debugging now writes to the actual RT; the saved snapshot is unchanged.")
                        self.label.setText(message)
                    elif result.get("independentFloatOutput"):
                        self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Visual debug active: %1 → independent RGBA32F preview (RT %2)").replace(
                            "%1", expression).replace("%2", str(result.get("targetIndex", ""))))
                    else:
                        self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Visual debug active: %1 → RT %2").replace(
                            "%1", expression).replace("%2", str(result.get("targetIndex", ""))))
                else:
                    self.show_error(result)
            elif operation == "compare":
                if (request.get("scope") == "shader-resource"
                        and not persistent_receipt_matches(request, result)):
                    self.stop()
                    self.show_error({"reason": "runtime-outdated"})
                    self.update_controls()
                    continue
                self.show_output(result.get("targetIndex", 0),
                                 None if result.get("status") == "comparing" else result.get("previewTexture"))
                if result.get("status") == "comparing":
                    self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Comparing the rendering from before debugging…"))
                elif result.get("scope") == "shader-resource":
                    self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Visual debug active for Shader %1 across EIDs. Later draws use its output.").replace(
                        "%1", str(result.get("shaderId") or request.get("shaderId") or "")))
                else:
                    self.label.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Visual debug active"))
            self.update_controls()
