"""Native dock lifetime regression through --ui-python (bundled Python 3.6).

Run a separate process with RENDERDOC_CONFIG_DIR and LOCALAPPDATA both below one
absolute system-temp directory, plus REPLAYLAB_DOCK_SMOKE_OUTPUT. Do not open a
user capture. Seed config/UI.config with {"rdocConfigData": 1,
"Analytics_TotalOptOut": true, "CheckUpdate_AllowChecks": false} so a first-run
analytics prompt does not intercept --ui-python. The fixture refuses non-isolated
settings and writes only Layout6 inside that scratch root. It never compiles,
saves, or replaces a Shader, and closes its own no-capture window when finished.

Exercises the real AddDockWindow API with missing/detached/self references,
reloads a saved layout that predates the workspace/console/AI panels, preserves
an unsaved native editor and live timers, and repeatedly redocks/resizes tools.
The output is updated before every operation so a crash identifies its trigger.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PySide2 import QtCore, QtWidgets
import qrenderdoc as qrd


def _contained(child, parent):
    child, parent = os.path.normcase(str(child.resolve())), os.path.normcase(str(parent.resolve()))
    return os.path.commonpath([child, parent]) == parent and child != parent


class DockLayoutSmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.output = Path(os.environ['REPLAYLAB_DOCK_SMOKE_OUTPUT']).resolve()
        self.config = Path(os.environ['RENDERDOC_CONFIG_DIR']).resolve()
        self.local = Path(os.environ['LOCALAPPDATA']).resolve()
        # Leave TEMP unchanged in the launcher; isolate only application settings below it.
        self.scratch = Path(os.environ['REPLAYLAB_DOCK_SMOKE_ROOT']).resolve()
        if not self.scratch.is_absolute() or not _contained(self.config, self.scratch):
            raise RuntimeError('RENDERDOC_CONFIG_DIR must be below the isolated smoke root')
        if not _contained(self.local, self.scratch) or not _contained(self.output, self.scratch):
            raise RuntimeError('LOCALAPPDATA and output must be below the isolated smoke root')
        if not _contained(self.scratch, Path(tempfile.gettempdir())):
            raise RuntimeError('Dock smoke root must be below the system temp directory')
        self.keep = []
        self.result = {'schema': 'bread-dock-layout-smoke/v1', 'ok': False,
                       'pid': os.getpid(), 'checks': [], 'operations': []}
        self.phase = 'starting'
        self.started = time.monotonic()
        self.last_tick = None
        self.gaps = []
        self.workspace_ticks = 0
        self.console_ticks = 0
        self.steps = []
        self.index = 0
        self.editor = None
        self.original_layout = None
        self.done = False

    def save(self):
        self.result['phase'] = self.phase
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(35)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def action(self, name):
        action = self.window.findChild(QtWidgets.QAction, name)
        if action is None or not action.isEnabled():
            raise RuntimeError('Missing or disabled native action: ' + name)
        self.keep.append(action)
        action.trigger()

    def assert_docked(self, widget):
        parent = widget.parentWidget()
        names = []
        while parent is not None:
            names.append(str(parent.metaObject().className()))
            parent = parent.parentWidget()
        if 'ToolWindowManagerArea' not in names or 'ToolWindowManager' not in names:
            raise RuntimeError('Widget was left detached: ' + str(widget.objectName()))

    def make_widget(self, name):
        widget = QtWidgets.QWidget(self.window)
        widget.setObjectName(name)
        widget.setWindowTitle(name)
        self.keep.append(widget)
        return widget

    def check(self, name, condition):
        if not condition:
            raise RuntimeError('Failed: ' + name)
        self.result['checks'].append(name)

    def prepare(self):
        if self.ctx.IsCaptureLoaded():
            raise RuntimeError('This isolated dock smoke must run without a user capture')
        self.bridge = sys.modules.get('renderdoc_mcp_bridge')
        if self.bridge is None:
            return False
        self.workspace = getattr(self.bridge, '_shader_workspace_ui', None)
        self.console = getattr(self.bridge, '_shader_console_ui', None)
        if self.workspace is None or self.console is None:
            return False
        # PythonShell's script proxy does not forward the optional native-editor factory.
        # The first-party panel retains the real UI-thread context used in production.
        self.ctx = self.workspace.ctx
        self.window = self.ctx.GetMainWindow().Widget()
        self.texture = self.ctx.GetTextureViewer().Widget()
        self.ai = self.window.findChild(QtWidgets.QWidget, 'aiAssistant')
        self.keep.extend([self.window, self.texture, self.workspace.top, self.console.top, self.ai])
        self.check('default layout creates the AI panel', self.ai is not None)
        self.assert_docked(self.workspace.top)
        self.assert_docked(self.console.top)
        self.assert_docked(self.ai)

        window_menu = self.window.findChild(QtWidgets.QMenu, 'menu_Window')
        titles = [str(action.text()).replace('&', '') for action in window_menu.actions()]
        self.check('retired panel menu entries are absent', not any(
            title in ('Material Parameters', 'Material parameters', '材质参数',
                      'Semantic Render Channels', 'Semantic render channels', '语义渲染通道')
            for title in titles))
        self.check('native pipeline and resource windows remain available',
                   self.window.findChild(QtWidgets.QAction, 'action_Pipeline_State') is not None and
                   self.window.findChild(QtWidgets.QAction, 'action_Resource_Inspector') is not None)

        self.editor = self.ctx.CreateShaderCodeEditor(self.workspace.top)
        self.editor_widget = self.editor.Widget()
        self.keep.append(self.editor_widget)
        self.source = '// Unsaved in-memory dock lifetime sentinel\nfloat4 Probe() { return 1.0; }\n'
        self.editor.SetLanguage('hlsl')
        self.editor.SetText(self.source)
        self.editor.SetModified(True)
        self.editor_widget.hide()
        self.workspace_timer = QtCore.QTimer(self.workspace.top)
        self.workspace_timer.setInterval(10)
        self.workspace_timer.timeout.connect(self.workspace_tick)
        self.workspace_timer.start()
        self.console_timer = QtCore.QTimer(self.console.top)
        self.console_timer.setInterval(10)
        self.console_timer.timeout.connect(self.console_tick)
        self.console_timer.start()
        self.keep.extend([self.workspace_timer, self.console_timer])
        self.action('action_Save_Layout_6')
        self.layout = self.config / 'Layout6.config'
        if not self.layout.is_file():
            raise RuntimeError('Save Layout did not use the isolated config directory')
        self.original_layout = self.layout.read_bytes()
        value = json.loads(self.original_layout.decode('utf-8-sig'))
        removed = []
        def strip_panels(node):
            if isinstance(node, dict):
                if isinstance(node.get('objects'), list):
                    kept = []
                    for obj in node['objects']:
                        if obj.get('name') in ('replaylabShaderWorkspace', 'breadConsole', 'aiAssistant'):
                            removed.append(obj['name'])
                        else:
                            kept.append(obj)
                    node['objects'] = kept
                    node['currentIndex'] = 0
                for child in node.values():
                    strip_panels(child)
            elif isinstance(node, list):
                for child in node:
                    strip_panels(child)
        strip_panels(value)
        self.check('legacy layout omits all first-party panels', set(removed) == {
            'replaylabShaderWorkspace', 'breadConsole', 'aiAssistant'})
        self.layout.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

        self.unbound = self.make_widget('breadDockSmokeUnboundReference')
        self.probe = self.make_widget('breadDockSmokeProbe')
        self.other = self.make_widget('breadDockSmokeOther')
        self.steps = [
            ('unmanaged reference', self.unmanaged_reference),
            ('null reference', self.null_reference),
            ('same managed widget and reference', self.self_reference),
            ('detach reference', self.detach_reference),
            ('detached reference fallback', self.detached_reference),
            ('load older saved layout', lambda: self.action('action_Load_Layout_6')),
            ('preserved source and panel timers', self.check_preservation),
        ]
        for i in range(12):
            self.steps.append(('redock and resize %d' % i, lambda i=i: self.redock(i)))
        self.steps.extend([
            ('preserved source after repeated docking', self.check_preservation),
            ('restore isolated original layout', self.restore),
            ('finish', self.finish),
        ])
        return True

    def workspace_tick(self):
        self.workspace_ticks += 1

    def console_tick(self):
        self.console_ticks += 1

    def unmanaged_reference(self):
        self.ctx.AddDockWindow(self.probe, qrd.DockReference.RightOf, self.unbound, 0.25)
        self.assert_docked(self.probe)

    def null_reference(self):
        self.ctx.AddDockWindow(self.other, qrd.DockReference.AddTo, None, 0.25)
        self.assert_docked(self.other)

    def self_reference(self):
        self.ctx.AddDockWindow(self.probe, qrd.DockReference.RightOf, self.probe, 0.25)
        self.assert_docked(self.probe)

    def detach_reference(self):
        self.ctx.AddDockWindow(self.probe, qrd.DockReference.NoArea, None)
        self.check('NoArea detaches the managed reference', self.probe.parentWidget() is None)

    def detached_reference(self):
        self.ctx.AddDockWindow(self.other, qrd.DockReference.AddTo, self.probe)
        self.assert_docked(self.other)

    def check_preservation(self):
        self.check('workspace controller survives', self.bridge._shader_workspace_ui is self.workspace)
        self.check('console controller survives', self.bridge._shader_console_ui is self.console)
        self.check('AI controller survives older layout',
                   self.window.findChild(QtWidgets.QWidget, 'aiAssistant') is self.ai)
        self.check('unsaved native source and modified state survive',
                   str(self.editor.GetText()) == self.source and self.editor.IsModified())
        self.check('panel-owned timer callbacks continue',
                   self.workspace_ticks > 2 and self.console_ticks > 2)
        self.check('original panel poll timers remain active',
                   self.workspace.poll_timer.isActive() and self.console.timer.isActive())
        self.assert_docked(self.workspace.top)
        self.assert_docked(self.console.top)
        self.assert_docked(self.ai)

    def redock(self, index):
        target = self.workspace.top if index % 2 else self.console.top
        self.ctx.AddDockWindow(target, qrd.DockReference.AddTo, self.texture, 0.2)
        self.ctx.RaiseDockWindow(target)
        self.assert_docked(target)
        for splitter in self.window.findChildren(QtWidgets.QSplitter):
            if 'ToolWindowManagerSplitter' not in str(splitter.metaObject().className()):
                continue
            sizes = splitter.sizes()
            if len(sizes) > 1 and min(sizes) > 2:
                delta = 1 if index % 2 else -1
                sizes[0] += delta
                sizes[-1] -= delta
                splitter.setSizes(sizes)

    def restore(self):
        self.layout.write_bytes(self.original_layout)
        self.workspace_timer.stop()
        self.console_timer.stop()
        self.ctx.DestroyShaderCodeEditor(self.editor)
        self.editor = None
        self.action('action_Load_Layout_6')
        self.unbound.deleteLater()

    def finish(self):
        self.result.update(ok=True, workspaceTimerTicks=self.workspace_ticks,
                           consoleTimerTicks=self.console_ticks,
                           maxUiTickMs=max(self.gaps or [0]),
                           elapsedSeconds=time.monotonic() - self.started,
                           sourceSha256=hashlib.sha256(self.source.encode('utf-8')).hexdigest())
        self.done = True
        self.timer.stop()
        self.save()
        QtCore.QTimer.singleShot(50, self.window.close)

    def tick(self):
        if self.done:
            return
        now = time.monotonic()
        if self.last_tick is not None and self.phase != 'starting':
            self.gaps.append((now - self.last_tick) * 1000.0)
        self.last_tick = now
        try:
            if now - self.started > 90:
                raise RuntimeError('Timed out in ' + self.phase)
            if self.phase == 'starting':
                if not self.prepare():
                    return
            name, operation = self.steps[self.index]
            self.phase = name
            self.save()
            started = time.monotonic()
            operation()
            self.result['operations'].append({'name': name,
                                              'elapsedMs': (time.monotonic() - started) * 1000.0})
            self.index += 1
            self.save()
        except Exception:
            self.result.update(error=traceback.format_exc(), needsIsolatedProcessShutdown=True)
            self.done = True
            self.timer.stop()
            self.save()
            if hasattr(self, 'window'):
                QtCore.QTimer.singleShot(50, self.window.close)


runner = DockLayoutSmoke(pyrenderdoc)
sys.modules['__main__']._bread_dock_layout_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
