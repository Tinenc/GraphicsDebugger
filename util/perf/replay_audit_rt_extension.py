"""Additional opt-in diagnostics loaded into the verified independent manual instance."""
import json
import os
from pathlib import Path
import sys
import time
from PySide2 import QtCore, QtWidgets

audit = sys.modules['__main__']._bread_private_replay_audit
root = Path(os.environ['BREAD_AUDIT_RUN']).parents[2]
output = root / 'output/rt-cache-audit-20261112'
output.mkdir(exist_ok=True)
if not hasattr(audit, '_rt_original_perform'):
    audit._rt_original_perform = type(audit).perform


def shader_panel():
    for module in list(sys.modules.values()):
        panel = getattr(module, '_shader_workspace_ui', None) if module is not None else None
        if panel is not None and getattr(panel, 'visual_debug', None) is not None:
            return panel
    raise RuntimeError('The native Shader Workspace is unavailable')


def rt_perform(self, command):
    op = command['operation']
    if op == 'rt_preserve_state':
        window = self.ctx.GetMainWindow().Widget()
        action = window.findChild(QtWidgets.QAction, 'action_Save_Default_Layout')
        if action is None:
            raise RuntimeError('Native save-layout action is unavailable')
        action.trigger()
        self.finish({'event': int(self.ctx.CurEvent()), 'windowGeometry': list(window.geometry().getRect()),
                     'layoutSaved': True})
    elif op == 'rt_resource_creation':
        def creation(ctrl):
            ids = set(int(i) for i in command['resources'])
            chunks = ctrl.GetStructuredFile().chunks
            def encode(obj, depth=0):
                row = {'name': str(obj.name), 'type': str(obj.type.name)}
                if obj.NumChildren() and depth < 5:
                    row['children'] = [encode(obj.GetChild(i), depth+1)
                                       for i in range(min(64, obj.NumChildren()))]
                else:
                    row['integer'] = int(obj.AsInt())
                return row
            return {'resources': [{'id': int(r.resourceId), 'name': str(r.name),
                       'creation': [encode(chunks[i]) for i in r.initialisationChunks[:8]]}
                       for r in ctrl.GetResources() if int(r.resourceId) in ids]}
        self.dispatch_replay(creation, self.finish)
    elif op == 'rt_clear_buffers':
        def clear(ctrl):
            error = str(ctrl.ClearSharedBufferPatches())
            if error:
                raise RuntimeError(error)
            return {'generation': int(ctrl.GetSharedBufferPatchGeneration())}
        def cleared(result):
            self.ctx.RefreshStatus()
            QtCore.QTimer.singleShot(200, lambda: self.finish(result))
        self.dispatch_replay(clear, cleared)
    elif op == 'rt_native_state':
        def state(ctrl):
            return {'event': int(ctrl.GetCurrentEventID()),
                    'bufferGeneration': int(ctrl.GetSharedBufferPatchGeneration()),
                    'shaderGeneration': int(ctrl.GetShaderReplacementGeneration(0)),
                    'checkpoint': json.loads(str(ctrl.GetReplayCheckpointStatus())),
                    'fatal': str(ctrl.GetFatalErrorStatus())}
        self.dispatch_replay(state, self.finish)
    elif op in ('rt_visual', 'rt_visual_restore', 'rt_visual_compare'):
        panel = shader_panel()
        visual = panel.visual_debug
        if not hasattr(visual, '_audit_original_operation'):
            visual._audit_original_operation = visual.operation
            visual._audit_results = []
            def observe(operation, params):
                start = time.perf_counter()
                result = visual._audit_original_operation(operation, params)
                visual._audit_results.append({'operation': operation, 'wallMs': (time.perf_counter() - start) * 1000,
                                              'generation': params.get('requestGeneration'), 'result': result})
                visual._audit_results[:] = visual._audit_results[-24:]
                return result
            visual.operation = observe
        visual._audit_results[:] = []
        started = time.perf_counter()
        before = self.render_widget.property('eidRequestedSerial') or 0
        if op == 'rt_visual_restore':
            visual.stop()
            expected = 'stop'
        elif op == 'rt_visual_compare':
            visual.compare(bool(command.get('original', True)))
            expected = 'compare'
        else:
            document = panel._current_document()
            if document is None or document.get('dirty'):
                raise RuntimeError('The matching clean editor is required for this diagnostic')
            source = str(document['editor'].GetText())
            needle = command.get('needle', 'lobe.nDotVRaw =')
            start = source.index(needle)
            begin = len(source[:start].encode('utf-8'))
            end = begin + len(needle.split(' =')[0].encode('utf-8'))
            visual.begin(document['workspaceId'], begin, end, snapshot={'member': ''})
            expected = 'start'
        generation = visual.state.generation
        def poll():
            if time.perf_counter() - started > 180:
                self.fail('RT visualization did not finish: ' + visual.label.text())
                return
            rows = [r for r in visual._audit_results if r['operation'] == expected and r['generation'] >= generation]
            errors = [r for r in visual._audit_results if r['result'].get('status') == 'error']
            if errors and not visual.busy:
                self.fail(json.dumps(errors, ensure_ascii=True))
                return
            ready = (not visual.busy and rows and (visual.active or op == 'rt_visual_restore'))
            render = self.render_widget
            published = (int(render.property('eidRequestedSerial') or 0) > before and
                         render.property('eidDisplayedEvent') == int(self.ctx.CurEvent()) and
                         not render.property('eidPresentationPending') and
                         render.property('eidPresentationSerial') == render.property('eidRequestedSerial'))
            if not ready or not published:
                QtCore.QTimer.singleShot(10, poll)
                return
            self.finish({'completeUiMs': (time.perf_counter() - started) * 1000,
                         'event': int(self.ctx.CurEvent()), 'active': visual.active,
                         'label': visual.label.text(), 'operations': list(visual._audit_results),
                         'completionEvidence': 'Shader Workspace operation plus new native RT presentation serial'})
        QtCore.QTimer.singleShot(10, poll)
    else:
        self._rt_original_perform(self, command)


def install():
    type(audit).perform = rt_perform
    audit.timer.start()
    (output / 'extension-installed.json').write_text(json.dumps({'pid': os.getpid(), 'ready': True,
          'epoch': time.time()}), encoding='utf-8')


audit.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(install)
