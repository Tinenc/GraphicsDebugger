"""Installed native replay recovery acceptance, run with --ui-python (Python 3.6).

The launcher must set BREAD_REPLAY_RECOVERY_SMOKE_ROOT to a system-temp directory
and put LOCALAPPDATA and RENDERDOC_CONFIG_DIR below it before starting qrenderdoc.
BREAD_REPLAY_RECOVERY_SMOKE_CAPTURE names a read-only existing capture;
BREAD_REPLAY_RECOVERY_SMOKE_EVENT defaults to 62768. No project/diagram restore,
AI request, Shader compilation/replacement, or capture/sidecar save is performed.

This is NOT a real GPU-timeout test. A healthy native controller is first checked
without injection. Then only the FIRST health getter result is replaced with an
explicit synthetic fatal status. Recovery itself uses the installed production
hooks, actual LoadCapture, C++ capture lifecycle, and the new native controller.
The fixture owns this isolated process and closes it after writing result.json.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

from PySide2 import QtCore, QtWidgets


def contained(child, parent):
    child = os.path.normcase(str(Path(child).resolve()))
    parent = os.path.normcase(str(Path(parent).resolve()))
    return child != parent and os.path.commonpath([child, parent]) == parent


def file_stamp(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns]


def sidecar_receipts(capture):
    root = Path(str(capture) + '.bread')
    if not root.is_dir():
        return {}
    # Stat the entire existing sidecar and hash small metadata files, not large
    # preview images. No production persistence operation is invoked by this test.
    receipts = {}
    for path in root.rglob('*'):
        if path.is_file():
            entry = {'stat': file_stamp(path)}
            if path.suffix.lower() == '.json' and entry['stat'][0] <= 4 * 1024 * 1024:
                entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            receipts[str(path.relative_to(root))] = entry
    return receipts


class ReplayRecoverySmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['BREAD_REPLAY_RECOVERY_SMOKE_ROOT']).resolve()
        if not contained(self.root, tempfile.gettempdir()):
            raise RuntimeError('Recovery smoke requires an isolated system-temp root')
        for key in ('LOCALAPPDATA', 'RENDERDOC_CONFIG_DIR'):
            if not contained(os.environ[key], self.root):
                raise RuntimeError('Recovery smoke must isolate ' + key)
        self.root.mkdir(parents=True, exist_ok=True)
        self.capture = str(Path(os.environ['BREAD_REPLAY_RECOVERY_SMOKE_CAPTURE']).resolve())
        self.event = int(os.environ.get('BREAD_REPLAY_RECOVERY_SMOKE_EVENT', '62768'))
        self.result = {'schema': 'bread-replay-recovery-native-smoke/v1', 'ok': False,
                       'pid': os.getpid(), 'phase': 'startup', 'checks': [],
                       'faultInjection': 'First health receipt only; no actual GPU fault is induced',
                       'capture': self.capture, 'eventId': self.event}
        self.started = time.monotonic()
        self.busy = False
        self.done = False
        self.bridge = None
        self.original_health = None
        self.editor = None
        self.panel = None
        self.phase = 'prepare'
        self.suppressed = []
        self.saved_functions = {}

    def save(self):
        self.result.update(phase=self.phase, elapsedSeconds=time.monotonic() - self.started)
        (self.root / 'result.json').write_text(
            json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(self, name, condition):
        self.result['checks'].append({'name': name, 'ok': bool(condition)})
        self.save()
        if not condition:
            raise AssertionError(name)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(60)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def suppress_restore(self):
        for name in ('_schedule_project_auto_activation', '_schedule_frame_render_flow_restore'):
            self.saved_functions[name] = getattr(self.bridge, name)

            def suppressed(*args, _name=name, **kwargs):
                self.suppressed.append(_name)

            setattr(self.bridge, name, suppressed)
        self.bridge._cancel_project_auto_activation()
        self.bridge._abandon_project_launcher_restore()

    def add_fixture_state(self):
        self.panel = self.bridge._shader_workspace_ui
        self.check('installed native Shader Workspace is available', self.panel is not None)
        self.editor = self.ctx.CreateShaderCodeEditor(self.panel.editor_stack)
        self.check('real Scintilla editor created', self.editor is not None)
        self.editor_widget = self.editor.Widget()
        self.draft_text = '// Unsaved native recovery fixture\nfloat4 MainPS() : SV_Target { return 0.25; }\n'
        self.editor.SetText(self.draft_text)
        self.document_id = 'bread-native-recovery-draft'
        self.document = {
            'workspaceId': self.document_id, 'documentKind': 'live-edit',
            'sourcePath': str(self.root / 'never-written-draft.hlsl'),
            'editor': self.editor, 'editorWidget': self.editor_widget,
            'captureBinding': self.bridge._shader_workspace_capture_binding(),
            'dirty': True, 'available': True, 'saveQueued': False, 'readOnly': False,
            'saveInFlight': False, 'commitUnknown': False, 'pendingSave': None,
            'appliedReplacementId': 'fixture-receipt-only-not-an-applied-shader',
        }
        self.panel._documents[self.document_id] = self.document
        self.model = {'liveEdit': {'lastApply': {'replacement': {
            'replacementId': 'fixture-receipt-only-not-an-applied-shader'}}}}
        self.panel._workspaces[self.document_id] = self.model
        self.saved_diagram = {'document': {'captureSessionId': self.old_session,
            'fixture': 'historical immutable evidence', 'stages': []}}
        self.saved_diagram_json = json.dumps(self.saved_diagram, sort_keys=True)
        self.bridge._frame_render_flow_saved = self.saved_diagram
        # A native unsent attachment exposes loadConversations/resetConversations:
        # either ordinary capture callback would remove this chip. No AI runs.
        self.ctx.AddTextToAIAssistant('Native recovery fixture',
                                    'Keep this unsent attachment through same-capture recovery.',
                                    'Isolated native test', '')
        self.main_interface = self.ctx.GetMainWindow()
        self.main_widget = self.main_interface.Widget()
        self.attachment_chips = self.main_widget.findChildren(QtWidgets.QFrame, 'aiAttachmentChip')
        self.check('native AI unsent attachment exists before recovery', len(self.attachment_chips) == 1)
        self.attachment_chip = self.attachment_chips[0]
        self.result['fixtureState'] = {'draftSaved': False, 'shaderActuallyApplied': False,
                                      'savedDiagramKind': 'in-memory fixture history',
                                      'aiTaskStarted': False}

    def recover(self):
        self.original_health = self.bridge._get_replay_health
        self.injected_calls = 0
        # The real workspace's periodic registry refresh can replace an injected
        # fixture model between timer turns. Re-register it at the transaction
        # boundary; checks below use the authoritative live registry, not an
        # orphaned local model retained only by this test.
        self.panel._workspaces[self.document_id] = self.model

        def first_fatal(params):
            actual = self.original_health(params)
            self.injected_calls += 1
            if self.injected_calls == 1:
                if actual.get('healthy') is not True:
                    raise RuntimeError('Real controller is unhealthy before fixture fault injection')
                return dict(actual, healthy=False, code='FixtureInjectedFatal',
                            diagnostic='Synthetic first health receipt; native controller was healthy')
            return actual

        self.bridge._get_replay_health = first_fatal
        try:
            response = self.bridge.handle_request({'id': 'native-recovery-injected',
                'method': 'recover_replay', 'params': {'expected_capture_session_id': self.old_session}})
            self.result['recoveryResponse'] = response
            self.save()
        finally:
            self.bridge._get_replay_health = self.original_health
        recovered = response.get('result', {})
        self.check('production recovery completed after synthetic health injection', recovered.get('recovered') is True)
        self.check('recovery rebuilt original capture without applying replacements',
                   recovered.get('replayContent') == 'capture-original' and
                   recovered.get('replacementsRestored') is False and
                   recovered.get('liveEditsRestored') is False)
        self.check('new capture session is distinct', bool(self.bridge._capture_session_id) and
                   self.bridge._capture_session_id != self.old_session)
        self.check('visible and selected EIDs restored', int(self.ctx.CurEvent()) == self.event and
                   int(self.ctx.CurSelectedEvent()) == self.event)
        self.check('native transaction property cleared',
                   not self.main_widget.property('breadReplayRecoveryCapturePath'))
        self.check('Python transaction flag cleared', not self.bridge._replay_recovery_path)
        self.check('dirty native editor text preserved', self.editor.GetText() == self.draft_text and
                   self.panel._documents.get(self.document_id) is self.document and self.document['dirty'])
        current_model = self.panel._workspaces.get(self.document_id, {})
        self.result['replacementReceiptAfterRecovery'] = {
            'documentReceipt': self.document.get('appliedReplacementId'),
            'workspaceStillRegistered': self.document_id in self.panel._workspaces,
            'registeredModelReceipt': current_model.get('liveEdit', {}).get('lastApply'),
            'documentAvailable': self.document.get('available'),
        }
        self.check('old document and workspace replacement receipts invalidated',
                   not self.document.get('appliedReplacementId') and
                   not current_model.get('liveEdit', {}).get('lastApply'))
        self.check('retained draft is reported and old replay authority disabled',
                   recovered.get('retainedShaderDraftCount') == 1 and self.document.get('available') is False)
        self.check('saved diagram preserved as old-session history',
                   self.bridge._frame_render_flow_saved is self.saved_diagram and
                   json.dumps(self.saved_diagram, sort_keys=True) == self.saved_diagram_json and
                   self.saved_diagram['document']['captureSessionId'] != self.bridge._capture_session_id)
        self.check('recovery callbacks did not schedule auto project/diagram restore',
                   self.suppressed == self.initial_suppressed)
        chips = self.main_widget.findChildren(QtWidgets.QFrame, 'aiAttachmentChip')
        self.check('native AI composer attachment survives without conversation reset',
                   len(chips) == 1 and chips[0] is self.attachment_chip)
        self.result['nativeHealthAfterRecovery'] = self.original_health({})
        self.check('actual replacement controller is healthy',
                   self.result['nativeHealthAfterRecovery'].get('healthy') is True)
        pipeline = self.bridge._get_pipeline_state({'event_id': self.event})
        self.result['pipelineAfterRecovery'] = pipeline
        self.check('actual recovered pipeline has a bound Shader',
                   not pipeline.get('error') and bool(pipeline.get('shaders')))
        self.check('pipeline inspection preserves visible EID', int(self.ctx.CurEvent()) == self.event)
        self.main_widget.grab().save(str(self.root / 'recovery-complete.png'), 'PNG')

    def tick(self):
        if self.done or self.busy:
            return
        self.busy = True
        try:
            if time.monotonic() - self.started > 420:
                raise RuntimeError('Native recovery smoke exceeded 420 seconds')
            if self.phase == 'prepare':
                self.bridge = sys.modules.get('renderdoc_mcp_bridge')
                if self.bridge is None or self.bridge._ctx is None:
                    return
                # PythonShell's forwarding context inherits the optional UI
                # factory/attachment no-ops. Use the installed native context,
                # exactly as the production Shader Workspace does.
                self.ctx = self.bridge._ctx
                self.check('isolated instance starts without capture', not self.ctx.IsCaptureLoaded())
                self.result['bridgeVersion'] = self.bridge.BRIDGE_INTEGRATION_VERSION
                self.check('deployed recovery bridge version', self.bridge.BRIDGE_INTEGRATION_VERSION == '1.13.60')
                self.capture_stamp = file_stamp(self.capture)
                self.sidecars_before = sidecar_receipts(self.capture)
                self.suppress_restore()
                self.phase = 'loading-original-capture'
                self.save()
                # LoadCapture can pump a nested Qt event loop: busy stays true.
                self.ctx.LoadCapture(self.capture, self.ctx.Config().DefaultReplayOptions,
                                     self.capture, False, True)
                self.check('original capture loaded', self.ctx.IsCaptureLoaded())
                self.ctx.SetEventID([], self.event, self.event, True)
                self.old_session = self.bridge._capture_session_id
                self.initial_suppressed = list(self.suppressed)
                self.phase = 'healthy-noop'
                self.save()
                return
            if self.phase == 'healthy-noop':
                self.result['nativeHealthBeforeInjection'] = self.bridge._get_replay_health({})
                self.check('original native controller is healthy',
                           self.result['nativeHealthBeforeInjection'].get('healthy') is True)
                result = self.bridge._recover_replay({'expected_capture_session_id': self.old_session})
                self.result['healthyNoop'] = result
                self.check('healthy recovery is a true no-op', result.get('status') == 'not-needed' and
                           not result.get('attempted') and self.bridge._capture_session_id == self.old_session)
                self.add_fixture_state()
                self.phase = 'injected-fatal-native-reopen'
                self.save()
                return
            if self.phase == 'injected-fatal-native-reopen':
                self.recover()
                self.check('capture bytes not rewritten', file_stamp(self.capture) == self.capture_stamp)
                self.check('capture sidecar files not rewritten', sidecar_receipts(self.capture) == self.sidecars_before)
                self.result['ok'] = True
                self.finish()
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.finish()
        finally:
            self.busy = False

    def finish(self):
        self.done = True
        self.timer.stop()
        self.phase = 'complete' if self.result.get('ok') else 'error'
        self.save()
        QtCore.QTimer.singleShot(50, self.shutdown)

    def shutdown(self):
        # Only this fixture's native editor is removed. No source is saved and
        # no user's process is enumerated or terminated.
        try:
            if self.original_health is not None:
                self.bridge._get_replay_health = self.original_health
            if self.panel is not None:
                self.panel._documents.pop(getattr(self, 'document_id', ''), None)
                self.panel._workspaces.pop(getattr(self, 'document_id', ''), None)
            if self.editor is not None:
                self.ctx.DestroyShaderCodeEditor(self.editor)
                self.editor = None
            if self.ctx.IsCaptureLoaded():
                self.ctx.CloseCapture()
            self.result['shutdownCaptureClosed'] = not self.ctx.IsCaptureLoaded()
        except Exception:
            self.result['shutdownError'] = traceback.format_exc()
        finally:
            self.save()
            QtWidgets.QApplication.instance().quit()


try:
    runner = ReplayRecoverySmoke(pyrenderdoc)
    sys.modules['__main__']._bread_replay_recovery_smoke = runner
    runner.save()
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
except Exception:
    root = Path(os.environ['BREAD_REPLAY_RECOVERY_SMOKE_ROOT'])
    root.mkdir(parents=True, exist_ok=True)
    (root / 'result.json').write_text(json.dumps({'ok': False, 'phase': 'script-startup',
        'error': traceback.format_exc()}, indent=2), encoding='utf-8')
