"""Offline behavior tests for typed UI control, including modal-safe submission."""
import importlib.util
import gc
from pathlib import Path
import types
import weakref

import pytest


MODULE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_ui_control.py"


class Signal:
    def __init__(self):
        self.count = 0
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *_args):
        self.count += 1
        for callback in self.callbacks:
            callback(*_args)


class Widget:
    def __init__(self, name="", parent=None):
        self.name, self.owner = name, parent
        self.child_list, self.action_list = [], []
        self.enabled, self.visible, self.deleted = True, True, False
        self.destroyed = Signal()
        self.focused, self.closed = False, False
        if parent:
            parent.child_list.append(self)

    def metaObject(self):
        return types.SimpleNamespace(className=lambda: type(self).__name__)

    def objectName(self):
        if self.deleted:
            raise RuntimeError("native object deleted")
        return self.name

    def property(self, name):
        return getattr(self, 'properties', {}).get(name)

    def repaint(self):
        pass

    def parent(self):
        return self.owner

    def children(self):
        return self.child_list

    def actions(self):
        return self.action_list

    def isVisible(self):
        return self.visible

    def isEnabled(self):
        return self.enabled

    def accessibleName(self):
        return self.name

    def windowTitle(self):
        return self.name if self.isWindow() else ""

    def toolTip(self):
        return ""

    def isWindow(self):
        return self.owner is None

    def setFocus(self, _reason):
        self.focused = True

    def close(self):
        self.closed = True
        return True


class Action(Widget):
    def text(self):
        return self.name

    def isCheckable(self):
        return False

    def trigger(self):
        self.activated = True


class Button(Widget):
    def text(self):
        return self.name

    def isCheckable(self):
        return False

    def click(self):
        self.clicked = True


class LineEdit(Widget):
    Normal = 0

    def __init__(self, name="", parent=None):
        super().__init__(name, parent)
        self.value, self.mode, self.read_only = "", 0, False
        self.editingFinished = Signal()
        self.textEdited = Signal()

    def echoMode(self):
        return self.mode

    def text(self):
        return self.value

    def setText(self, value):
        self.value = value

    def isReadOnly(self):
        return self.read_only

    def placeholderText(self):
        return ""

    def hasAcceptableInput(self):
        return self.value != "invalid"


class SpinBox(Widget):
    def __init__(self, name="", parent=None):
        super().__init__(name, parent)
        self.current = 2

    def value(self):
        return self.current

    def minimum(self):
        return -10

    def maximum(self):
        return 10

    def setValue(self, value):
        self.current = value


class DoubleSpinBox(SpinBox):
    pass


class ComboBox(Widget):
    def __init__(self, name="", parent=None):
        super().__init__(name, parent)
        self.options, self.current = ["A", "B"], 0

    def count(self):
        return len(self.options)

    def currentIndex(self):
        return self.current

    def currentText(self):
        return self.options[self.current]

    def itemText(self, index):
        return self.options[index]

    def findText(self, value, _match):
        return self.options.index(value) if value in self.options else -1

    def isEditable(self):
        return False

    def setCurrentIndex(self, index):
        self.current = index


class Index:
    def __init__(self, row=-1, col=-1, parent=None):
        self.r, self.c, self.owner = row, col, parent

    def isValid(self):
        return self.r >= 0 and self.c >= 0

    def row(self):
        return self.r

    def column(self):
        return self.c

    def parent(self):
        return self.owner or Index()


class Model:
    def __init__(self):
        self.rows = [["roughness", 0.5], ["baseColor", "1, 0, 0"]]

    def rowCount(self, parent):
        return len(self.rows) if parent is None or not parent.isValid() else 0

    def columnCount(self, _parent):
        return 2

    def index(self, row, col, parent=None):
        return Index(row, col, parent) if 0 <= row < self.rowCount(parent) and 0 <= col < 2 else Index()

    def data(self, index, _role):
        return self.rows[index.row()][index.column()]

    def hasChildren(self, _index):
        return False

    def flags(self, index):
        return 16 if index.column() == 1 else 0

    def setData(self, index, value, _role):
        self.rows[index.row()][index.column()] = value
        return True


class ItemView(Widget):
    def __init__(self, name="", parent=None):
        super().__init__(name, parent)
        self.item_model, self.current, self.activated = Model(), Index(), Signal()

    def model(self):
        return self.item_model

    def currentIndex(self):
        return self.current

    def setCurrentIndex(self, index):
        self.current = index

    def scrollTo(self, _index):
        pass


@pytest.fixture
def ui():
    spec = importlib.util.spec_from_file_location("bread_ui_control_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = types.SimpleNamespace(roots=[], modal=None, thread=lambda: 1)
    app.topLevelWidgets = lambda: app.roots
    app.activeModalWidget = lambda: app.modal
    scheduled = []
    module.QtWidgets = types.SimpleNamespace(
        QWidget=Widget, QAction=Action, QAbstractButton=Button,
        QLineEdit=LineEdit, QAbstractSpinBox=SpinBox, QSpinBox=SpinBox,
        QDoubleSpinBox=DoubleSpinBox, QComboBox=ComboBox, QAbstractItemView=ItemView,
        QApplication=types.SimpleNamespace(instance=lambda: app))
    module.QtCore = types.SimpleNamespace(
        Qt=types.SimpleNamespace(OtherFocusReason=1, MatchExactly=0, DisplayRole=0,
                                 AccessibleTextRole=11, EditRole=2, ItemIsEditable=16),
        QThread=types.SimpleNamespace(currentThread=lambda: 1), QModelIndex=Index,
        QAbstractItemModel=Model,
        QTimer=types.SimpleNamespace(singleShot=lambda _delay, callback: scheduled.append(callback)))
    ctx = types.SimpleNamespace(ShowTextureViewer=lambda: app.roots.append(Widget("texture")))
    control = module.ApplicationUIController(ctx)
    return types.SimpleNamespace(module=module, app=app, queue=scheduled, control=control)


def target(ui, widget):
    ui.app.roots.append(widget)
    return ui.control.inspect()["objects"][-1]["targetId"]


def run(ui, action, widget, **arguments):
    token = target(ui, widget)
    result = ui.control.perform({"action": action, "target_id": token, "arguments": arguments})
    assert result["status"] == "queued"
    ui.queue.pop(0)()
    return ui.control.inspect()["operations"][-1]


def test_snapshot_has_stable_process_local_targets_and_redacts_password(ui):
    root = Widget("main")
    secret = LineEdit("key", root)
    secret.value, secret.mode = "actual secret", 2
    action = Action("open texture", root)
    root.action_list.append(action)  # Owned and attached references must deduplicate.
    ui.app.roots.append(root)
    first = ui.control.inspect()
    second = ui.control.inspect()
    assert len(first["objects"]) == 3
    assert [row["targetId"] for row in first["objects"]] == [row["targetId"] for row in second["objects"]]
    row = next(row for row in first["objects"] if row["objectName"] == "key")
    assert row["sensitive"] and "value" not in row
    assert "actual secret" not in str(first)
    assert ui.control.inspect({"query": "texture"})["total"] == 1


def test_activation_is_queued_so_modal_dialog_cannot_hold_tool_submission(ui):
    button = Button("color")
    token = target(ui, button)
    button.click = lambda: setattr(ui.app, "modal", Widget("color dialog"))
    result = ui.control.perform({"action": "activate", "target_id": token})
    assert ui.app.modal is None and result["status"] == "queued"
    ui.queue.pop(0)()
    snapshot = ui.control.inspect()
    assert snapshot["activeModalId"]
    assert snapshot["operations"][-1]["status"] == "completed"
    # Snapshot contents are copies; a later completion cannot mutate old receipts.
    assert result["status"] == "queued"


def test_target_lifetime_is_checked_again_when_queued_work_runs(ui):
    button = Button("apply")
    token = target(ui, button)
    ui.control.perform({"action": "activate", "target_id": token})
    button.deleted = True
    ui.queue.pop(0)()
    assert not hasattr(button, "clicked")
    row = ui.control._operations[-1]
    assert row["status"] == "failed" and "ui-target-stale" in row["error"]


def test_native_wrapper_survives_tool_boundary_and_releases_on_destroy(ui):
    button = Button("native-owned button")
    reference = weakref.ref(button)
    token = ui.control._id(button)
    del button
    gc.collect()
    assert reference() is not None
    assert ui.control._resolve(token) is reference()
    reference().destroyed.emit()
    gc.collect()
    assert reference() is None
    with pytest.raises(ValueError, match="ui-target-stale"):
        ui.control._resolve(token)


def test_retained_native_wrappers_are_bounded_and_release_on_shutdown(ui):
    ui.module.MAX_TARGETS = 4
    first = ui.control._id(Button("first"))
    for index in range(8):
        ui.control._id(Button("entry%d" % index))
    assert len(ui.control._targets) == 4
    with pytest.raises(ValueError, match="ui-target-stale"):
        ui.control._resolve(first)
    ui.control.shutdown()
    assert not ui.control._targets and not ui.control._ids


@pytest.mark.parametrize("value,expected", [(3.5, "completed"), (11.0, "failed"), (float("nan"), "failed")])
def test_numeric_controls_use_normal_range_and_value_change_path(ui, value, expected):
    spin = DoubleSpinBox("roughness")
    row = run(ui, "set_value", spin, value=value)
    assert row["status"] == expected
    assert spin.value() == (value if expected == "completed" else 2)


def test_invalid_text_is_rejected_without_leaving_an_invalid_edit(ui):
    line = LineEdit("input")
    line.value = "old"
    result = run(ui, "set_value", line, value="invalid")
    assert result["status"] == "failed" and line.value == "old"
    assert not line.editingFinished.count
    assert run(ui, "set_value", line, value="new")["status"] == "completed"
    assert line.value == "new" and line.editingFinished.count == 1


def test_readonly_and_disabled_controls_cannot_be_written(ui):
    line = LineEdit("source")
    line.read_only = True
    assert run(ui, "set_value", line, value="new")["status"] == "failed"
    line.read_only, line.enabled = False, False
    assert run(ui, "set_value", line, value="new")["status"] == "failed"
    assert line.value == ""


def test_select_and_edit_item_rows_use_model_callbacks_and_editability(ui):
    view = ItemView("cbuffer")
    token = target(ui, view)
    snapshot = ui.control.inspect({"target_id": token})
    assert snapshot["items"][0]["values"] == ["roughness", "0.5"]
    assert run(ui, "select", view, row_path=[1], column=0, activate=True)["status"] == "completed"
    assert view.current.row() == 1 and view.activated.count == 1
    assert run(ui, "set_cell", view, row_path=[0], column=1, value=0.8)["status"] == "completed"
    assert view.item_model.rows[0][1] == 0.8
    assert run(ui, "set_cell", view, row_path=[0], column=0, value="rename")["status"] == "failed"
    assert view.item_model.rows[0][0] == "roughness"
    assert run(ui, "select", view, row_path=[99])["status"] == "failed"


def test_native_private_model_overrides_use_public_qt_virtual_dispatch(ui):
    class NativePrivateModel(Model):
        def rowCount(self, _parent):
            raise TypeError("rowCount(const QModelIndex &) is a private method")

        def columnCount(self, _parent):
            raise TypeError("columnCount(const QModelIndex &) is a private method")

    view = ItemView("native tree")
    view.item_model = NativePrivateModel()
    # This emulates Qt's public C++ virtual dispatch, not Python subclass calls.
    ui.module.QtCore.QAbstractItemModel = types.SimpleNamespace(
        rowCount=Model.rowCount, columnCount=Model.columnCount,
        index=lambda model, row, col, parent: Index(row, col, parent)
            if 0 <= row < Model.rowCount(model, parent) and 0 <= col < 2 else Index(),
        data=Model.data, hasChildren=Model.hasChildren, flags=Model.flags, setData=Model.setData)
    token = target(ui, view)
    snapshot = ui.control.inspect({"target_id": token})
    assert snapshot["items"][0]["values"] == ["roughness", "0.5"]
    assert run(ui, "set_cell", view, row_path=[0], column=1, value=0.9)["status"] == "completed"
    assert view.item_model.rows[0][1] == 0.9


def test_resource_tree_uses_native_accessible_text_before_unconvertible_rich_display_role(ui):
    view = ItemView("API inspector resources")
    view.item_model.rows = [["Resource 18993447", "StructuredBuffer"],
                            ["Pipeline 155", "D3D12"], ["Texture 333", "Normal"]]
    requests = []

    def native_data(model, index, role):
        requests.append((index.row(), index.column(), role))
        if role == ui.module.QtCore.Qt.AccessibleTextRole:
            # QVariant<QString> is converted to str by PySide before reaching Python.
            return str(model.rows[index.row()][index.column()])
        raise TypeError("Can't find converter for RichResourceTextPtr")

    class PublicModel(Model):
        data = native_data

    ui.module.QtCore.QAbstractItemModel = PublicModel
    token = target(ui, view)
    snapshot = ui.control.inspect({"target_id": token})
    assert snapshot["itemCount"] == 3
    assert [item["values"] for item in snapshot["items"]] == view.item_model.rows
    assert requests == [(row, column, 11) for row in range(3) for column in range(2)]


def test_accessible_role_undefined_falls_back_per_cell_without_losing_later_resource_rows(ui):
    view = ItemView("mixed native model")
    view.item_model.rows = [["Resource 18993447", "RGBA16_FLOAT"],
                            ["roughness", "0.5"], ["Texture 333", "Normal"]]
    requests = []

    def native_data(model, index, role):
        requests.append((index.row(), index.column(), role))
        if role == ui.module.QtCore.Qt.AccessibleTextRole:
            return model.rows[index.row()][index.column()] if index.row() != 1 else None
        if index.row() != 1:
            raise TypeError("Can't find converter for RichResourceTextPtr")
        return model.rows[index.row()][index.column()]

    class PublicModel(Model):
        data = native_data

    ui.module.QtCore.QAbstractItemModel = PublicModel
    token = target(ui, view)
    snapshot = ui.control.inspect({"target_id": token})
    assert [item["values"] for item in snapshot["items"]] == view.item_model.rows
    assert requests == [(0, 0, 11), (0, 1, 11), (1, 0, 11), (1, 0, 0),
                        (1, 1, 11), (1, 1, 0), (2, 0, 11), (2, 1, 11)]


def test_empty_native_accessible_text_is_an_intentional_label_not_a_rich_display_fallback(ui):
    def native_data(_model, _index, role):
        assert role == ui.module.QtCore.Qt.AccessibleTextRole
        return ""

    class PublicModel(Model):
        data = native_data

    ui.module.QtCore.QAbstractItemModel = PublicModel
    assert ui.module._model_text(Model(), Index(0, 0)) == ""


def test_modal_dialog_blocks_background_controls_but_accepts_its_own(ui):
    button = Button("outside")
    ui.app.modal = Widget("confirm")
    assert run(ui, "activate", button)["status"] == "failed"
    inside = Button("cancel", ui.app.modal)
    assert run(ui, "activate", inside)["status"] == "completed"
    assert inside.clicked and not hasattr(button, "clicked")


def test_annotation_strokes_reject_other_controls_and_validate_before_dispatch(ui):
    assert run(ui, "draw", Widget("editor"), points=[[0, 0], [1, 1]])["status"] == "failed"
    canvas = Widget("aiAnnotationCanvas")
    canvas.properties = {'annotationImageRect': [10, 20, 100, 80], 'annotationStrokeCount': 0}
    assert 'draw' in ui.control.inspect({'target_id': target(ui, canvas)})['objects'][0]['operations']
    for points in ([], [[0, 0]], [[0, 0], [float('nan'), 1]], [[0, 0], [1.1, 1]], [[0, 0], [1, True]]):
        result = run(ui, "draw", canvas, points=points)
        assert result['status'] == 'failed' and 'ui-invalid' in result['error']
    assert canvas.properties['annotationStrokeCount'] == 0


def test_annotation_draw_maps_image_coordinates_through_actual_canvas_rect(ui):
    canvas = Widget('aiAnnotationCanvas')
    canvas.properties = {'annotationImageRect': [20, 50, 200, 100], 'annotationStrokeCount': 0}
    ui.module.QtCore.Qt.LeftButton, ui.module.QtCore.Qt.NoButton, ui.module.QtCore.Qt.NoModifier = 1, 0, 0
    ui.module.QtCore.QEvent = types.SimpleNamespace(MouseButtonPress=2, MouseMove=5, MouseButtonRelease=3)
    ui.module.QtCore.QPointF = lambda x, y: (x, y)
    ui.module.QtGui = types.SimpleNamespace(QMouseEvent=lambda *args: args)
    events = []
    def dispatch(obj, event):
        assert obj is canvas
        events.append(event)
        if event[0] == 3:canvas.properties['annotationStrokeCount'] += 1
    ui.app.sendEvent = dispatch
    result = run(ui, 'draw', canvas, points=[[0, 0], [.5, .25], [1, 1]])
    assert result['status'] == 'completed' and result['result']['strokeCount'] == 1
    assert [event[1] for event in events] == [(20, 50), (120, 75), (220, 150), (220, 150)]
    assert [event[0] for event in events] == [2, 5, 5, 3]


def test_builtin_and_custom_windows_use_application_show_callbacks(ui):
    calls = []
    ui.control.window_callbacks["shader-workspace"] = lambda: calls.append("workspace")
    for name in ("texture-viewer", "shader-workspace"):
        ui.control.perform({"action": "open_window", "arguments": {"window": name}})
        ui.queue.pop(0)()
    assert ui.app.roots[0].name == "texture" and calls == ["workspace"]
    with pytest.raises(ValueError, match="ui-unknown-window"):
        ui.control.perform({"action": "open_window", "arguments": {"window": "eval"}})
    with pytest.raises(ValueError, match="ui-unsupported-action"):
        ui.control.perform({"action": "invokeMethod", "arguments": {"method": "eval"}})


def test_pending_limits_shutdown_and_thread_checks(ui):
    for _ in range(ui.module.MAX_PENDING):
        ui.control.perform({"action": "open_window", "arguments": {"window": "texture-viewer"}})
    with pytest.raises(ValueError, match="ui-busy"):
        ui.control.perform({"action": "open_window", "arguments": {"window": "texture-viewer"}})
    ui.control.shutdown()
    for callback in ui.queue:
        callback()
    assert not ui.app.roots
    assert all(row["status"] == "failed" for row in ui.control._operations)
    ui.control._alive = True
    ui.module.QtCore.QThread.currentThread = lambda: 2
    with pytest.raises(ValueError, match="ui-wrong-thread"):
        ui.control.inspect()


def test_snapshot_scan_and_page_are_bounded(ui):
    ui.module.MAX_OBJECTS = 24
    root = Widget("main")
    for index in range(40):
        Widget("entry%d" % index, root)
    ui.app.roots.append(root)
    result = ui.control.inspect({"offset": 5, "limit": 3})
    assert result["total"] == 24 and result["scanTruncated"]
    assert len(result["objects"]) == 3 and result["hasMore"]
