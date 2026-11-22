"""Native queued application-UI tools via --ui-python, without a capture.

REPLAYLAB_UI_SMOKE_ROOT, REPLAYLAB_UI_SMOKE_OUTPUT, RENDERDOC_CONFIG_DIR and
LOCALAPPDATA must all point inside an isolated system-temp scratch directory.
Exercises the installed Bridge handlers with real Qt widgets, native windows,
model edit signals and a genuinely modal exec_ dialog. Never modifies a project
or capture; the launcher owns the isolated application's shutdown.
"""
import gc
import importlib.util
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PySide2 import QtCore, QtWidgets


def contained(child, parent):
    child, parent = os.path.normcase(str(child.resolve())), os.path.normcase(str(parent.resolve()))
    return child != parent and os.path.commonpath([child, parent]) == parent


class ApplicationUISmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['REPLAYLAB_UI_SMOKE_ROOT']).resolve()
        self.output = Path(os.environ['REPLAYLAB_UI_SMOKE_OUTPUT']).resolve()
        if not contained(self.root, Path(tempfile.gettempdir())):
            raise RuntimeError('UI smoke must use an isolated system-temp root')
        for value in (self.output, Path(os.environ['RENDERDOC_CONFIG_DIR']), Path(os.environ['LOCALAPPDATA'])):
            if not contained(value, self.root):
                raise RuntimeError('UI smoke output/config/local state must remain below its scratch root')
        self.result = {'schema': 'bread-application-ui-smoke/v1', 'ok': False,
                       'pid': os.getpid(), 'checks': [], 'operations': []}
        self.phase, self.index, self.waiting = 'prepare', 0, None
        self.started, self.last_tick, self.gaps = time.monotonic(), None, []
        self.keep, self.steps, self.signals = [], [], []
        self.done = False
        self.modal_open, self.modal_dismiss = None, None

    def save(self):
        self.result['phase'] = self.phase
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(self, name, condition):
        if not condition:
            raise RuntimeError('Failed: ' + name)
        self.result['checks'].append(name)

    def inspect(self, **arguments):
        return self.bridge._get_application_ui_state(arguments)

    def token(self, name):
        rows = self.inspect(query=name, limit=200)['objects']
        exact = [row for row in rows if row['objectName'] == name]
        if len(exact) != 1:
            raise RuntimeError('Expected one current UI target named %s; got %d' % (name, len(exact)))
        return exact[0]['targetId']

    def command(self, action, target='', **arguments):
        result = self.bridge._control_application_ui({
            'action': action, 'target_id': target, 'arguments': arguments})
        self.check('UI operation returns queued before execution', result['status'] == 'queued')
        self.result['operations'].append(dict(result))
        return result['operationId']

    def status(self, operation_id):
        rows = self.inspect(limit=1)['operations']
        return next((row for row in rows if row['operationId'] == operation_id), None)

    def make(self, cls, name):
        obj = cls(self.panel)
        obj.setObjectName(name)
        self.keep.append(obj)
        return obj

    def prepare(self):
        if self.ctx.IsCaptureLoaded():
            raise RuntimeError('UI smoke must run without a user capture')
        self.bridge = sys.modules.get('renderdoc_mcp_bridge')
        if self.bridge is None or not hasattr(self.bridge, '_get_application_ui_state'):
            return False
        if not getattr(self.bridge, '_shader_workspace_ui', None):
            return False
        if os.environ.get('REPLAYLAB_UI_SMOKE_CONTROLLER'):
            # Optional source-only iteration, confined to this isolated test
            # process. The installed bridge is never overwritten by the fixture.
            source = Path(os.environ['REPLAYLAB_UI_SMOKE_CONTROLLER']).resolve()
            self.bridge._shutdown_application_ui()
            module_name = 'renderdoc_mcp_bridge.project_ui_control'
            spec = importlib.util.spec_from_file_location(module_name, str(source))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[module_name] = module
            self.result['controllerSource'] = str(source)
        main = self.ctx.GetMainWindow().Widget()
        self.panel = QtWidgets.QDialog(main)
        self.panel.setObjectName('breadUISmokePanel')
        self.panel.setWindowTitle('Bread UI control isolated smoke')
        self.keep.extend([main, self.panel])
        form = QtWidgets.QFormLayout(self.panel)
        self.spin = self.make(QtWidgets.QDoubleSpinBox, 'breadUISmokeValue')
        self.spin.setRange(-10.0, 10.0)
        self.spin.setValue(1.0)
        self.spin.valueChanged.connect(lambda value: self.signals.append(('spin', value)))
        form.addRow('Value', self.spin)
        self.line = self.make(QtWidgets.QLineEdit, 'breadUISmokeText')
        self.line.editingFinished.connect(lambda: self.signals.append(('text', self.line.text())))
        form.addRow('Text', self.line)
        self.combo = self.make(QtWidgets.QComboBox, 'breadUISmokeChoice')
        self.combo.addItems(['First', 'Second'])
        form.addRow('Choice', self.combo)
        self.secret = self.make(QtWidgets.QLineEdit, 'breadUISmokeSecret')
        self.secret.setEchoMode(QtWidgets.QLineEdit.Password)
        self.secret.setText('ui-smoke-private-value')
        form.addRow('Private input', self.secret)
        self.readonly = self.make(QtWidgets.QLineEdit, 'breadUISmokeReadonly')
        self.readonly.setText('unchanged')
        self.readonly.setReadOnly(True)
        form.addRow('Readonly', self.readonly)
        self.table = self.make(QtWidgets.QTableWidget, 'breadUISmokeTable')
        self.table.setColumnCount(2)
        self.table.setRowCount(2)
        for row, values in enumerate((('roughness', '0.5'), ('color', '1, 0, 0'))):
            for col, value in enumerate(values):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
        self.table.itemChanged.connect(lambda item: self.signals.append(('cell', item.text())))
        form.addRow('Values', self.table)
        self.button = self.make(QtWidgets.QPushButton, 'breadUISmokeModalButton')
        self.button.setText('Open modal probe')
        self.button.clicked.connect(self.open_modal)
        form.addRow(self.button)
        self.panel.resize(560, 420)
        self.panel.show()
        snapshot = self.inspect(query='breadUISmoke', limit=200)
        self.check('native Qt targets discovered', snapshot['total'] >= 8)
        self.check('password data excluded', 'ui-smoke-private-value' not in json.dumps(snapshot))
        native_models = self.inspect(query='RDTreeView', limit=200)['objects']
        native_models = [row for row in native_models if row.get('className') == 'RDTreeView']
        self.check('native qrenderdoc models expose their public Qt interface',
                   bool(native_models) and all('rowCount' in row and 'modelError' not in row
                                               for row in native_models))
        for row in native_models:
            model_snapshot = self.inspect(target_id=row['targetId'], limit=3)
            self.check('native qrenderdoc model contents can be inspected', 'items' in model_snapshot)
        self.panel_id = self.token('breadUISmokePanel')
        self.steps = [
            ('activate native action after wrapper GC', 'activate', 'action_Texture_Viewer', {}, 'completed',
             lambda: self.ctx.HasTextureViewer()),
            ('set numeric value', 'set_value', 'breadUISmokeValue', {'value': 0.75}, 'completed',
             lambda: self.spin.value() == 0.75 and ('spin', 0.75) in self.signals),
            ('edit text and commit', 'set_value', 'breadUISmokeText', {'value': 'applied'}, 'completed',
             lambda: self.line.text() == 'applied' and ('text', 'applied') in self.signals),
            ('select option', 'select', 'breadUISmokeChoice', {'index': 1}, 'completed',
             lambda: self.combo.currentText() == 'Second'),
            ('select item', 'select', 'breadUISmokeTable', {'row_path': [1], 'column': 0}, 'completed',
             lambda: self.table.currentRow() == 1),
            ('edit model cell', 'set_cell', 'breadUISmokeTable', {'row_path': [0], 'column': 1, 'value': '0.8'}, 'completed',
             lambda: self.table.item(0, 1).text() == '0.8' and ('cell', '0.8') in self.signals),
            ('reject readonly edit', 'set_value', 'breadUISmokeReadonly', {'value': 'bad'}, 'failed',
             lambda: self.readonly.text() == 'unchanged'),
        ]
        for window in ('texture-viewer', 'shader-workspace', 'console', 'bread-ai'):
            self.steps.append(('open ' + window, 'open_window', '', {'window': window}, 'completed', lambda: True))
        return True

    def open_modal(self):
        self.dialog = QtWidgets.QDialog(self.panel)
        self.dialog.setObjectName('breadUISmokeModal')
        self.dialog.setWindowTitle('Modal probe')
        layout = QtWidgets.QVBoxLayout(self.dialog)
        button = QtWidgets.QPushButton('Dismiss probe', self.dialog)
        button.setObjectName('breadUISmokeDismiss')
        button.clicked.connect(self.dialog.accept)
        layout.addWidget(button)
        self.keep.extend([self.dialog, button])
        self.dialog.exec_()

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(30)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.keep.append(self.timer)
        self.timer.start()
        self.save()

    def tick(self):
        if self.done:
            return
        now = time.monotonic()
        if self.last_tick is not None and self.phase != 'prepare':
            self.gaps.append((now - self.last_tick) * 1000.0)
        self.last_tick = now
        try:
            if now - self.started > 60:
                raise RuntimeError('UI smoke timed out in ' + self.phase)
            if self.phase == 'prepare':
                if not self.prepare():
                    return
                self.phase = 'controls'
            if self.waiting is not None:
                operation_id, expected, assertion = self.waiting
                state = self.status(operation_id)
                if not state or state['status'] in ('queued', 'running'):
                    return
                self.check(self.phase + ' reports actual result', state['status'] == expected)
                self.check(self.phase + ' updates normal widget behavior', assertion())
                self.result['operations'].append(dict(state))
                self.waiting = None
                self.index += 1
            if self.index < len(self.steps):
                name, action, target_name, arguments, expected, assertion = self.steps[self.index]
                self.phase = name
                token = self.token(target_name) if target_name else ''
                # Native C++ controls do not necessarily retain their PySide
                # wrapper; an ID must survive between separate tool calls.
                gc.collect()
                operation_id = self.command(action, token, **arguments)
                self.waiting = operation_id, expected, assertion
                self.save()
                return
            if self.modal_open is None:
                self.phase = 'modal dialog'
                self.modal_open = self.command('activate', self.token('breadUISmokeModalButton'))
                self.save()
                return
            if self.modal_dismiss is None:
                snapshot = self.inspect(query='breadUISmokeDismiss', limit=100)
                if not snapshot['activeModalId']:
                    return
                self.check('opening modal command remains running without blocking tools',
                           self.status(self.modal_open)['status'] == 'running')
                self.modal_dismiss = self.command('activate', self.token('breadUISmokeDismiss'))
                self.save()
                return
            if any(self.status(value)['status'] != 'completed' for value in (self.modal_open, self.modal_dismiss)):
                return
            self.check('normal modal dialog dismissed by UI controls', not self.inspect(limit=1)['activeModalId'])
            self.result.update(ok=True, elapsedSeconds=now - self.started, maxUiTickMs=max(self.gaps or [0]),
                               signals=self.signals, widgetCount=self.inspect(limit=1)['total'])
            self.done = True
            self.timer.stop()
            self.panel.close()
            self.phase = 'complete'
            self.save()
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.done = True
            self.timer.stop()
            self.save()


try:
    runner = ApplicationUISmoke(pyrenderdoc)
    runner.save()
    sys.modules['__main__']._bread_application_ui_smoke = runner
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
except Exception:
    Path(os.environ['REPLAYLAB_UI_SMOKE_OUTPUT']).write_text(json.dumps({
        'ok': False, 'phase': 'script-startup', 'error': traceback.format_exc()}, indent=2), encoding='utf-8')
