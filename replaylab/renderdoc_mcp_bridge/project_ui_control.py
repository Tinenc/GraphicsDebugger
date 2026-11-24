"""Typed, process-local UI control for Bread AI (Python 3.6 / PySide2).

The bridge calls this module on the application thread. It exposes the normal
widgets and actions, never arbitrary QObject method names or Python evaluation.
Mutations are queued so a normal modal dialog cannot keep an MCP request asleep.
"""

from __future__ import print_function

import copy
import math
import uuid
import weakref
from collections import OrderedDict

try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:  # Offline tests inject a small typed Qt surface.
    QtCore = QtGui = QtWidgets = None


MAX_OBJECTS = 6000
MAX_TARGETS = MAX_OBJECTS * 2
MAX_PAGE = 200
MAX_TEXT = 2048
MAX_PENDING = 32
MAX_OPERATION_HISTORY = 64

# Protocol identifiers, not UI labels. Show* uses normal dock placement and
# visibility; closing a document still goes through its regular close handler.
WINDOWS = {
    "event-browser": "EventBrowser",
    "api-inspector": "APIInspector",
    "annotation-viewer": "AnnotationViewer",
    "texture-viewer": "TextureViewer",
    "mesh-viewer": "MeshPreview",
    "pipeline-state": "PipelineViewer",
    "capture-dialog": "CaptureDialog",
    "debug-messages": "DebugMessageView",
    "diagnostic-log": "DiagnosticLogView",
    "comments": "CommentView",
    "performance-counters": "PerformanceCounterViewer",
    "statistics": "StatisticsViewer",
    "timeline": "TimelineBar",
    "python-console": "PythonShell",
    "resource-inspector": "ResourceInspector",
}


def _text(value, maximum=MAX_TEXT):
    return str(value or "")[:maximum]


def _integer(value, name, minimum=0, maximum=0x7FFFFFFF):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("ui-invalid-argument: %s must be an integer in [%d, %d]" %
                         (name, minimum, maximum))
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("ui-invalid-value: a finite numeric value is required")
    return value


def _model_call(model, method, *args):
    # Native qrenderdoc subclasses can expose their virtual overrides as private
    # in Shiboken. Calling the public Qt base still performs virtual dispatch.
    return getattr(QtCore.QAbstractItemModel, method)(model, *args)


def _model_text(model, index):
    # Read the native plain-text role first. Rich resource labels use private
    # QVariant types which cannot cross the PySide boundary via DisplayRole.
    accessible_role = getattr(QtCore.Qt, "AccessibleTextRole", 11)
    value = _model_call(model, "data", index, accessible_role)
    if value is not None:
        return _text(value)
    return _text(_model_call(model, "data", index, QtCore.Qt.DisplayRole))


class ApplicationUIController(object):
    def __init__(self, ctx, window_callbacks=None):
        self.ctx = ctx
        self.window_callbacks = dict(window_callbacks or {})
        self._ids = weakref.WeakKeyDictionary()
        # C++ owns native controls; PySide may discard the temporary Python
        # wrapper immediately after inspect(). Retain bounded wrappers until
        # QObject destruction so returned IDs survive the next tool call.
        self._targets = OrderedDict()
        self._operations = []
        self._alive = True

    def shutdown(self):
        self._alive = False
        self._targets.clear()
        self._ids.clear()

    def _app(self):
        if not self._alive or QtWidgets is None:
            raise ValueError("ui-unavailable: application UI controller is unavailable")
        app = QtWidgets.QApplication.instance()
        if app is None or QtCore.QThread.currentThread() != app.thread():
            raise ValueError("ui-wrong-thread: application UI control requires the UI thread")
        return app

    def _id(self, obj):
        token = self._ids.get(obj)
        if token is None:
            token = "ui-" + uuid.uuid4().hex
            self._ids[obj] = token
            owner = weakref.ref(self)

            def destroyed(*_args):
                controller = owner()
                if controller is not None:
                    controller._forget(token)

            obj.destroyed.connect(destroyed)
        self._targets[token] = obj
        self._targets.move_to_end(token)
        while len(self._targets) > MAX_TARGETS:
            self._targets.popitem(last=False)
        return token

    def _forget(self, token):
        obj = self._targets.pop(token, None)
        if obj is not None:
            self._ids.pop(obj, None)

    def _resolve(self, target_id):
        obj = self._targets.get(str(target_id or ""))
        if obj is None:
            raise ValueError("ui-target-stale: inspect the current UI and use a returned target ID")
        try:
            obj.objectName()  # Deleted C++ objects may still have a Python wrapper.
        except RuntimeError:
            self._forget(str(target_id))
            raise ValueError("ui-target-stale: the selected control has been destroyed")
        self._targets.move_to_end(str(target_id))
        return obj

    def _is(self, obj, name):
        cls = getattr(QtWidgets, name, None)
        return cls is not None and isinstance(obj, cls)

    def _editable(self, obj):
        if self._is(obj, "QLineEdit") or self._is(obj, "QTextEdit") or self._is(obj, "QPlainTextEdit"):
            return not obj.isReadOnly()
        return any(self._is(obj, cls) for cls in (
            "QSpinBox", "QDoubleSpinBox", "QAbstractSlider", "QComboBox", "QCheckBox"))

    def _describe(self, obj, parent_id=""):
        result = {"targetId": self._id(obj), "parentId": parent_id,
                  "className": _text(obj.metaObject().className(), 128),
                  "objectName": _text(obj.objectName(), 256)}
        if self._is(obj, "QAction"):
            result.update(kind="action", label=_text(obj.text()), enabled=obj.isEnabled(),
                          visible=obj.isVisible(), checkable=obj.isCheckable())
            if obj.isCheckable():
                result["value"] = obj.isChecked()
            result["operations"] = ["activate"]
            return result
        result.update(kind="widget", visible=obj.isVisible(), enabled=obj.isEnabled(),
                      label=_text(obj.accessibleName() or obj.windowTitle()),
                      tooltip=_text(obj.toolTip()), editable=self._editable(obj))
        operations = ["focus"]
        if obj.objectName() == "aiAnnotationCanvas":
            operations.append("draw")
            result["imageRect"] = obj.property("annotationImageRect")
            result["strokeCount"] = obj.property("annotationStrokeCount")
        if obj.isWindow():
            operations.append("close")
        if self._is(obj, "QAbstractButton"):
            result["label"] = _text(obj.text())
            result["checkable"] = obj.isCheckable()
            if obj.isCheckable():
                result["value"] = obj.isChecked()
            operations.append("activate")
        if self._is(obj, "QLineEdit"):
            # Never expose password, token or hidden-input contents in UI snapshots.
            result["sensitive"] = obj.echoMode() != QtWidgets.QLineEdit.Normal
            if not result["sensitive"]:
                result["value"] = _text(obj.text())
            result["placeholder"] = _text(obj.placeholderText())
        elif self._is(obj, "QLabel"):
            result["label"] = _text(obj.text())
        elif self._is(obj, "QSpinBox") or self._is(obj, "QDoubleSpinBox") or self._is(obj, "QAbstractSlider"):
            result.update(value=obj.value(), minimum=obj.minimum(), maximum=obj.maximum())
        elif self._is(obj, "QComboBox"):
            result.update(index=obj.currentIndex(), value=_text(obj.currentText()), count=obj.count(),
                          options=[{"index": i, "text": _text(obj.itemText(i))}
                                   for i in range(min(obj.count(), MAX_PAGE))])
            operations.append("select")
        elif self._is(obj, "QTabWidget") or self._is(obj, "QTabBar"):
            result.update(index=obj.currentIndex(), count=obj.count(),
                          options=[{"index": i, "text": _text(obj.tabText(i))}
                                   for i in range(min(obj.count(), MAX_PAGE))])
            operations.append("select")
        elif self._is(obj, "QAbstractItemView"):
            model = obj.model()
            if model is not None:
                root_index = QtCore.QModelIndex()
                try:
                    result.update(rowCount=_model_call(model, "rowCount", root_index),
                                  columnCount=_model_call(model, "columnCount", root_index))
                except (RuntimeError, TypeError) as error:
                    result["modelError"] = _text(error)
                current = obj.currentIndex()
                if current.isValid():
                    result["selection"] = self._index_path(current)
            operations.extend(["select", "set_cell"])
        elif self._is(obj, "QTextEdit") or self._is(obj, "QPlainTextEdit"):
            result["selectionText"] = _text(obj.textCursor().selectedText())
            operations.append("select_text")
        if result["editable"]:
            operations.append("set_value")
        result["operations"] = operations
        return result

    def _index_path(self, index):
        rows = []
        column = index.column()
        while index.isValid():
            rows.append(index.row())
            index = index.parent()
        return {"rowPath": list(reversed(rows)), "column": column}

    def _model_index(self, view, args):
        rows = args.get("row_path", [])
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError("ui-invalid-selection: row_path must contain 1 to 32 row indexes")
        model = view.model()
        if model is None:
            raise ValueError("ui-model-unavailable: control has no item model")
        index = QtCore.QModelIndex()
        column = _integer(args.get("column", 0), "column")
        for position, row in enumerate(rows):
            row = _integer(row, "row_path")
            index = _model_call(model, "index", row, column if position == len(rows) - 1 else 0, index)
            if not index.isValid():
                raise ValueError("ui-selection-missing: requested row or column no longer exists")
        return index

    def inspect(self, params=None):
        params = params or {}
        app = self._app()
        offset = _integer(params.get("offset", 0), "offset")
        limit = _integer(params.get("limit", 100), "limit", 1, MAX_PAGE)
        query = _text(params.get("query", ""), 256).casefold()
        target_id = params.get("target_id", "")
        root = self._resolve(target_id) if target_id else None
        roots = [root] if root is not None else list(app.topLevelWidgets())
        pending = [(item, "") for item in roots]
        seen, rows = set(), []
        pos = 0
        while pos < len(pending) and len(seen) < MAX_OBJECTS:
            obj, parent_id = pending[pos]
            pos += 1
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if not (self._is(obj, "QWidget") or self._is(obj, "QAction")):
                continue
            try:
                row = self._describe(obj, parent_id)
                if not query or query in " ".join(str(row.get(key, "")) for key in
                        ("label", "objectName", "className", "tooltip")).casefold():
                    rows.append(row)
                remaining = MAX_OBJECTS - len(pending)
                if remaining > 0:
                    children = list(obj.children())
                    if self._is(obj, "QWidget"):
                        children.extend(obj.actions())
                    pending.extend((child, row["targetId"]) for child in children[:remaining])
            except (RuntimeError, TypeError):
                continue
        modal = app.activeModalWidget()
        result = {"schema": "bread-application-ui/v1", "objects": rows[offset:offset + limit],
                  "offset": offset, "total": len(rows), "hasMore": offset + limit < len(rows),
                  "scanTruncated": pos < len(pending) or len(pending) >= MAX_OBJECTS,
                  "activeModalId": self._id(modal) if modal is not None else "",
                  "windows": sorted(set(WINDOWS) | set(self.window_callbacks)),
                  "operations": [dict(value) for value in self._operations[-16:]]}
        if root is not None and self._is(root, "QAbstractItemView") and root.model() is not None:
            parent = (self._model_index(root, params) if params.get("row_path") else QtCore.QModelIndex())
            model = root.model()
            count = _model_call(model, "rowCount", parent)
            result["items"] = [{"row": i, "values": [_model_text(model,
                                 _model_call(model, "index", i, c, parent))
                                 for c in range(min(_model_call(model, "columnCount", parent), 16))],
                                "hasChildren": _model_call(model, "hasChildren",
                                    _model_call(model, "index", i, 0, parent))}
                               for i in range(offset, min(count, offset + limit))]
            result["itemCount"] = count
            result["itemsHaveMore"] = offset + limit < count
        return result

    def perform(self, params):
        self._app()
        action = str(params.get("action", ""))
        if action not in ("open_window", "focus", "close", "activate", "set_value", "select", "set_cell", "select_text", "draw"):
            raise ValueError("ui-unsupported-action: use a typed application UI operation")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ValueError("ui-invalid-argument: arguments must be an object")
        args = copy.deepcopy(args)
        target_id = str(params.get("target_id", "") or "")
        if action != "open_window":
            self._resolve(target_id)
        elif args.get("window") not in WINDOWS and args.get("window") not in self.window_callbacks:
            raise ValueError("ui-unknown-window: inspect the supported window IDs")
        if sum(row["status"] in ("queued", "running") for row in self._operations) >= MAX_PENDING:
            raise ValueError("ui-busy: inspect pending UI operations before submitting more")
        operation = {"operationId": "ui-operation-" + uuid.uuid4().hex,
                     "action": action, "targetId": target_id, "status": "queued"}
        self._operations.append(operation)
        while len(self._operations) > MAX_OPERATION_HISTORY:
            completed = next((i for i, row in enumerate(self._operations)
                              if row["status"] not in ("queued", "running")), None)
            if completed is None:
                break
            del self._operations[completed]

        def execute():
            try:
                self._app()
                operation["status"] = "running"
                result = self._execute(action, target_id, args)
                operation.update(status="completed", result=result)
            except Exception as error:
                operation.update(status="failed", error=_text(error))

        QtCore.QTimer.singleShot(0, execute)
        return dict(operation)

    def _execute(self, action, target_id, args):
        app = self._app()
        if action == "open_window":
            if app.activeModalWidget() is not None:
                raise ValueError("ui-modal-active: finish or dismiss the active application dialog first")
            window = args["window"]
            if window in self.window_callbacks:
                self.window_callbacks[window]()
            else:
                getattr(self.ctx, "Show" + WINDOWS[window])()
            return {"window": window}
        obj = self._resolve(target_id)
        modal = app.activeModalWidget()
        if modal is not None:
            cursor = obj
            while cursor is not None and cursor != modal:
                cursor = cursor.parent()
            if cursor != modal:
                raise ValueError("ui-modal-active: finish or dismiss the active application dialog first")
        if not obj.isEnabled() and action != "close":
            raise ValueError("ui-control-disabled: this control is disabled in the current application state")
        if action == "activate":
            if self._is(obj, "QAction"):
                obj.trigger()
            elif self._is(obj, "QAbstractButton"):
                obj.click()
            else:
                raise ValueError("ui-not-activatable: choose an action or button from the UI snapshot")
        elif action == "focus":
            if not self._is(obj, "QWidget"):
                raise ValueError("ui-not-widget: this target cannot receive keyboard focus")
            obj.setFocus(QtCore.Qt.OtherFocusReason)
        elif action == "close":
            if not self._is(obj, "QWidget") or not (obj.isWindow() or obj.windowTitle()):
                raise ValueError("ui-not-window: close requires a window or dock panel")
            # The normal handler can refuse, prompt for unsaved edits, or safely
            # stop a live job; never bypass it with deleteLater/removeToolWindow.
            return {"accepted": bool(obj.close())}
        elif action == "select":
            if self._is(obj, "QComboBox") or self._is(obj, "QTabWidget") or self._is(obj, "QTabBar"):
                index = _integer(args.get("index"), "index", 0, max(0, obj.count() - 1))
                if not obj.count():
                    raise ValueError("ui-empty-control: no selectable entries")
                obj.setCurrentIndex(index)
            elif self._is(obj, "QAbstractItemView"):
                index = self._model_index(obj, args)
                obj.setCurrentIndex(index)
                obj.scrollTo(index)
                if args.get("activate") is True:
                    obj.activated.emit(index)
            else:
                raise ValueError("ui-not-selectable: target does not expose selection")
        elif action == "set_cell":
            if not self._is(obj, "QAbstractItemView"):
                raise ValueError("ui-not-item-view: cell edits require an item view")
            index = self._model_index(obj, args)
            if not (_model_call(obj.model(), "flags", index) & QtCore.Qt.ItemIsEditable):
                raise ValueError("ui-cell-read-only: this cell is not editable through the normal UI")
            value = args.get("value")
            if not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
                raise ValueError("ui-invalid-value: a scalar cell value is required")
            if not _model_call(obj.model(), "setData", index, value, QtCore.Qt.EditRole):
                raise ValueError("ui-cell-edit-rejected: the application's item model rejected this value")
        elif action == "select_text":
            if not (self._is(obj, "QTextEdit") or self._is(obj, "QPlainTextEdit")):
                raise ValueError("ui-not-text-editor: use the Shader navigation tool for native Shader editors")
            start = _integer(args.get("start"), "start")
            length = _integer(args.get("length", 0), "length")
            if start + length > obj.document().characterCount() - 1:
                raise ValueError("ui-selection-out-of-range: selection exceeds the current text")
            cursor = obj.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(start + length, QtGui.QTextCursor.KeepAnchor)
            obj.setTextCursor(cursor)
            obj.ensureCursorVisible()
        elif action == "set_value":
            self._set_value(obj, args.get("value"))
        elif action == "draw":
            if obj.objectName() != "aiAnnotationCanvas":
                raise ValueError("ui-not-annotation-canvas: draw requires the inspected RT annotation canvas")
            points = args.get("points")
            if not isinstance(points, list) or not 2 <= len(points) <= 256:
                raise ValueError("ui-invalid-stroke: provide 2 to 256 normalized image points")
            for point in points:
                if (not isinstance(point, list) or len(point) != 2 or
                        any(not 0 <= _number(value) <= 1 for value in point)):
                    raise ValueError("ui-invalid-stroke: coordinates must be finite and inside [0,1]")
            obj.repaint()
            rect = obj.property("annotationImageRect")
            if not isinstance(rect, list) or len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
                raise ValueError("ui-annotation-not-ready: inspect the visible canvas after its image is ready")
            for index, point in enumerate(points):
                position = QtCore.QPointF(rect[0] + point[0] * rect[2], rect[1] + point[1] * rect[3])
                kind = QtCore.QEvent.MouseButtonPress if index == 0 else QtCore.QEvent.MouseMove
                button = QtCore.Qt.LeftButton if index == 0 else QtCore.Qt.NoButton
                app.sendEvent(obj, QtGui.QMouseEvent(kind, position, button, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
            app.sendEvent(obj, QtGui.QMouseEvent(QtCore.QEvent.MouseButtonRelease, position,
                          QtCore.Qt.LeftButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier))
            obj.repaint()
            return {"targetId": target_id, "strokeCount": obj.property("annotationStrokeCount")}
        return {"targetId": target_id}

    def _set_value(self, obj, value):
        if not self._editable(obj):
            raise ValueError("ui-read-only: this target is not an editable value control")
        if self._is(obj, "QCheckBox"):
            if not isinstance(value, bool):
                raise ValueError("ui-invalid-value: checkbox requires a boolean")
            obj.setChecked(value)
        elif self._is(obj, "QSpinBox") or self._is(obj, "QDoubleSpinBox") or self._is(obj, "QAbstractSlider"):
            numeric = _number(value)
            if not self._is(obj, "QDoubleSpinBox"):
                numeric = _integer(value, "value", obj.minimum(), obj.maximum())
            elif not obj.minimum() <= numeric <= obj.maximum():
                raise ValueError("ui-value-out-of-range: value exceeds this control's range")
            obj.setValue(numeric)
        elif self._is(obj, "QComboBox"):
            if not isinstance(value, str):
                raise ValueError("ui-invalid-value: combo value requires text; use select for an index")
            index = obj.findText(value, QtCore.Qt.MatchExactly)
            if index >= 0:
                obj.setCurrentIndex(index)
            elif obj.isEditable():
                obj.setEditText(value)
            else:
                raise ValueError("ui-value-missing: text does not match an available option")
        else:
            if not isinstance(value, str) or len(value) > 1024 * 1024:
                raise ValueError("ui-invalid-value: text must be a string of at most 1 MiB")
            if self._is(obj, "QLineEdit"):
                previous = obj.text()
                obj.setText(value)
                if not obj.hasAcceptableInput():
                    obj.setText(previous)
                    raise ValueError("ui-invalid-input: value does not satisfy the control's validator")
                obj.textEdited.emit(value)
                obj.editingFinished.emit()
            else:
                obj.setPlainText(value)
