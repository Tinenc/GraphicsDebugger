from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / 'replaylab/renderdoc_mcp_bridge'


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in self.callbacks:
            callback()


class Timer:
    def __init__(self, parent):
        self.timeout = Signal()
        self.active = False

    def setSingleShot(self, value):
        self.single = value

    def start(self, interval):
        self.active = True
        self.interval = interval

    def stop(self):
        self.active = False

    def fire(self):
        if self.active:
            self.active = False
            self.timeout.emit()


class Object:
    def __init__(self, parent):
        self.parent = parent

    def deleteLater(self):
        self.deleted = True


class Widget:
    def __init__(self, placeholder=True):
        self.placeholder = placeholder

    def property(self, name):
        return self.placeholder if name == 'breadFrameFlowPlaceholder' else None


class Window:
    def __init__(self, widgets=()):
        self.widgets = list(widgets)
        self.filters = []

    def findChildren(self, kind, name):
        assert kind is Widget and name == 'breadFrameRenderFlow'
        return self.widgets

    def installEventFilter(self, value):
        self.filters.append(value)

    def removeEventFilter(self, value):
        self.filters.remove(value)


def load_layout():
    qt = SimpleNamespace(QtCore=SimpleNamespace(QObject=Object, QTimer=Timer, QByteArray=bytes,
                         QEvent=SimpleNamespace(DynamicPropertyChange=170)),
                         QtWidgets=SimpleNamespace(QWidget=Widget))
    spec = importlib.util.spec_from_file_location('frame_flow_layout_probe',
                                                  BRIDGE / 'project_frame_flow_layout.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {'PySide2': qt}):
        spec.loader.exec_module(module)
    return module


def test_default_layout_adopts_native_placeholder_without_opening_a_new_dock():
    module = load_layout()
    placeholder = Widget()
    window, populated = Window([placeholder]), []

    def ensure():
        populated.append(module.find_frame_flow_placeholder(window))
        placeholder.placeholder = False

    controller = module.FrameFlowLayoutController(window, ensure)
    assert populated == [placeholder]
    controller.synchronize()
    assert populated == [placeholder]
    assert module.find_frame_flow_placeholder(window) is None


@pytest.mark.parametrize('widgets', [[], [Widget(False)]])
def test_custom_layout_without_placeholder_does_not_open_or_raise_frame_flow(widgets):
    module = load_layout()
    calls = []
    controller = module.FrameFlowLayoutController(Window(widgets), lambda: calls.append(True))
    controller.synchronize()
    assert calls == []


def test_saved_layout_adoption_waits_until_native_restore_finishes_and_cancels_on_close():
    module = load_layout()
    window, calls = Window(), []
    controller = module.FrameFlowLayoutController(window, lambda: calls.append(True))
    event = SimpleNamespace(type=lambda: 170,
                            propertyName=lambda: b'breadFrameFlowLayoutGeneration')
    window.widgets = [Widget()]
    assert controller.eventFilter(window, event) is False
    assert calls == [] and controller.timer.active
    controller.timer.fire()
    assert calls == [True]
    controller.eventFilter(window, event)
    controller.close()
    controller.timer.fire()
    controller.synchronize()
    assert calls == [True] and window.filters == []


def test_unrelated_window_changes_do_not_create_frame_flow():
    module = load_layout()
    window = Window()
    controller = module.FrameFlowLayoutController(window, lambda: pytest.fail('Unexpected adoption'))
    event = SimpleNamespace(type=lambda: 170, propertyName=lambda: b'somethingElse')
    assert controller.eventFilter(window, event) is False
    event = SimpleNamespace(type=lambda: 170, propertyName=lambda: b'breadFrameFlowLayoutGeneration')
    assert controller.eventFilter(Window(), event) is False
    assert not controller.timer.active


def panel_show():
    tree = ast.parse((BRIDGE / 'project_frame_flow.py').read_text('utf8'))
    panel = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.ClassDef) and node.name == 'FrameRenderFlowPanel'
                 and any(isinstance(item, ast.FunctionDef) and item.name == 'show' for item in node.body))
    function = next(node for node in panel.body if isinstance(node, ast.FunctionDef) and node.name == 'show')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(BRIDGE / 'project_frame_flow.py'), 'exec'), namespace)
    return namespace['show']


@pytest.mark.parametrize('parent, expected_docks', [(object(), 0), (None, 1)])
def test_show_reuses_adopted_dock_and_reopens_layout_hidden_dock(parent, expected_docks):
    docks, raises = [], []
    top = SimpleNamespace(parentWidget=lambda: parent)
    panel = SimpleNamespace(_alive=True, _docked=True, top=top,
        ctx=SimpleNamespace(AddDockWindow=lambda *args: docks.append(args),
                            RaiseDockWindow=lambda *args: raises.append(args)),
        qrd=SimpleNamespace(DockReference=SimpleNamespace(MainToolArea='main')))
    panel._refresh_dock_ancestors = lambda: setattr(panel, '_dock_ancestors', [parent] if parent else [])
    panel_show()(panel)
    assert len(docks) == expected_docks and raises == [(top,)]


def test_native_layout_creates_placeholder_only_when_default_or_requested_by_saved_layout():
    source = (ROOT / 'qrenderdoc/Windows/MainWindow.cpp').read_text('utf8')
    initial = source[source.index('void MainWindow::LoadInitialLayout()'):
                     source.index('bool MainWindow::ErrorReportsAllowed()')]
    assert 'if(!LoadLayout(0))\n    ResetFactoryLayout();' in initial
    assert 'frameFlow ? frameFlow : CreateFrameRenderFlowPlaceholder()' in initial
    assert 'manager->moveToolWindows(previouslyDocked, ToolWindowManager::NoArea);' in initial
    assert 'forceCloseToolWindow' not in initial
    callback = source[source.index('setToolWindowCreateCallback('):source.index('ui->action_Start_Replay_Loop')]
    assert 'objectName == lit("breadFrameRenderFlow")' in callback
    assert 'return CreateFrameRenderFlowPlaceholder();' in callback
    restore = source[source.index('bool MainWindow::LoadLayout(int layout)'):
                     source.index('void MainWindow::showLaunchError')]
    assert 'if(toolWindow->objectName() == lit("breadFrameRenderFlow"))\n            continue;' in restore
    assert restore.index('breadFrameFlowLayoutGeneration') > restore.index('Manually closing orphaned window')


def test_layout_controller_supports_embedded_python_36_and_import_without_qt():
    path = BRIDGE / 'project_frame_flow_layout.py'
    ast.parse(path.read_text('utf8'), feature_version=(3, 6))
    spec = importlib.util.spec_from_file_location('frame_flow_layout_no_qt', path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {'PySide2': None}):
        spec.loader.exec_module(module)
    assert module.find_frame_flow_placeholder(Window([Widget()])) is None


def test_only_native_dock_owner_chain_is_retained():
    module = load_layout()
    root = SimpleNamespace(parentWidget=lambda: None)
    area = SimpleNamespace(parentWidget=lambda: root)
    stack = SimpleNamespace(parentWidget=lambda: area)
    child = SimpleNamespace(parentWidget=lambda: stack)
    assert module.retain_dock_ancestors(child) == [stack, area, root]
    assert module.retain_dock_ancestors(root) == []


def test_restore_refreshes_existing_panel_owner_chain_without_reopening_it():
    module = load_layout()
    refreshes = []
    panel = SimpleNamespace(_refresh_dock_ancestors=lambda: refreshes.append(True))
    controller = module.FrameFlowLayoutController(Window([Widget(False)]),
        lambda: pytest.fail('Existing tab must not be reopened'), existing_panel=lambda: panel)
    controller.synchronize()
    assert refreshes == [True, True]
