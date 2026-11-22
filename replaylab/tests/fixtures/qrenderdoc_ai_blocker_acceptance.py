"""Real native replay acceptance; use an isolated system-temp profile and demo captures.

BREAD_BLOCKER_ACCEPTANCE_ROOT contains capture-results.json from capture_api_smoke.
BREAD_BLOCKER_ACCEPTANCE_VERSION is the release version without its display prefix.
No shader replacement, user capture, project restore or online AI request is performed.
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
import renderdoc as rd


class Acceptance(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['BREAD_BLOCKER_ACCEPTANCE_ROOT']).resolve()
        self.root.relative_to(Path(tempfile.gettempdir()).resolve())
        for key in ('LOCALAPPDATA', 'APPDATA', 'RENDERDOC_CONFIG_DIR'):
            Path(os.environ[key]).resolve().relative_to(self.root)
        self.result = {'schema': 'bread-ai-blocker-native-acceptance/v1',
                       'ok': False, 'pid': os.getpid(), 'checks': [], 'captures': []}
        self.started = time.monotonic()
        self.expected_version = os.environ['BREAD_BLOCKER_ACCEPTANCE_VERSION']
        self.cycles = int(os.environ.get('BREAD_BLOCKER_ACCEPTANCE_CYCLES', '1'))
        if not 1 <= self.cycles <= 10:
            raise ValueError('Acceptance cycles must be 1..10')
        self.done = False

    def save(self):
        (self.root / 'native-result.json').write_text(
            json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(self, name, value):
        self.result['checks'].append({'name': name, 'ok': bool(value)})
        self.save()
        if not value:
            raise AssertionError(name)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        if self.done:
            return
        try:
            if time.monotonic() - self.started > 120:
                raise RuntimeError('Native acceptance timed out waiting for bridge')
            bridge = sys.modules.get('renderdoc_mcp_bridge')
            if bridge is None or bridge._ctx is None:
                return
            self.done = True
            self.timer.stop()
            self.ctx = bridge._ctx
            self.result['bridgePath'] = str(Path(bridge.__file__).resolve())
            self.result['bridgeSha256'] = hashlib.sha256(Path(bridge.__file__).read_bytes()).hexdigest()
            self.result['applicationVersion'] = QtWidgets.QApplication.applicationVersion()
            self.result['windowTitle'] = self.ctx.GetMainWindow().Widget().windowTitle()
            self.check('actual application version matches release',
                       self.result['applicationVersion'] == 'v' + self.expected_version)
            self.check('actual window title includes release version',
                       'RenderDocBread v' + self.expected_version in self.result['windowTitle'])
            bridge._schedule_project_auto_activation = lambda *args, **kwargs: None
            bridge._schedule_frame_render_flow_restore = lambda *args, **kwargs: None
            bridge._cancel_project_auto_activation()
            bridge._abandon_project_launcher_restore()
            captures = json.loads((self.root / 'capture-results.json').read_text('utf-8'))
            for item in captures * self.cycles:
                if not item.get('ok'):
                    continue
                capture = Path(item['capture']).resolve()
                capture.relative_to(self.root)
                before = hashlib.sha256(capture.read_bytes()).hexdigest()
                self.ctx.LoadCapture(str(capture), self.ctx.Config().DefaultReplayOptions,
                                     str(capture), False, True)
                self.check(item['api'] + ' demo loads in actual GUI', self.ctx.IsCaptureLoaded())
                QtWidgets.QApplication.processEvents()
                events = self.ctx.GetMainWindow().Widget().findChild(QtWidgets.QTreeView, 'events')
                self.check(item['api'] + ' actual event tree is populated',
                           events is not None and events.model().rowCount() > 0 and
                           events.model().index(0, 0).data() is not None)
                snapshot = bridge._get_frame_render_flow_outline({
                    'expected_capture_session_id': bridge._capture_session_id, 'limit': 5000})
                rows = snapshot['actions']
                draws = [a for a in rows if a['actionKind'] == 'draw' and a['hasWork'] is True]
                self.check(item['api'] + ' frame outline contains active draws', bool(draws))

                def observe(ctrl):
                    pending = list(ctrl.GetRootActions())
                    active, eligible = [], []
                    while pending:
                        action = pending.pop(0)
                        pending[0:0] = list(action.children)
                        if (int(action.flags) & int(rd.ActionFlags.Drawcall) and action.numIndices > 0
                                and not int(action.flags) & int(rd.ActionFlags.Indirect)):
                            record = {'eventId': int(action.eventId), 'flags': int(action.flags),
                                'rawInstances': int(action.numInstances),
                                'workload': bridge._action_workload(action)}
                            if record['workload']['instanceCount'] > 0:
                                eligible.append(record)
                            if not int(action.flags) & int(rd.ActionFlags.Instanced):
                                active.append(record)
                    return {'ordinary': active, 'eligible': eligible}

                observed = bridge._replay(observe)
                ordinary = observed['ordinary']
                self.check(item['api'] + ' ordinary draws have one effective instance',
                           bool(observed['eligible']) and all(a['workload']['instanceCount'] == 1 for a in ordinary))
                by_event = {a['eventId']: a for a in rows}
                self.check(item['api'] + ' no ordinary nonzero draw is discarded',
                           all(by_event[a['eventId']]['hasWork'] is True for a in ordinary))
                candidates = ordinary or observed['eligible']
                chosen = next((a for a in candidates if a['flags'] & int(rd.ActionFlags.Indexed)), candidates[0])
                if item['api'] == 'D3D11':
                    self.check('real D3D11 DrawIndexed is exercised', bool(chosen['flags'] & int(rd.ActionFlags.Indexed)))
                    self.check('real D3D11 DrawIndexed reports raw zero instances', chosen['rawInstances'] == 0)
                visible, selected = int(self.ctx.CurEvent()), int(self.ctx.CurSelectedEvent())
                geometry = bridge._get_post_vs_data({'event_id': chosen['eventId']})
                self.check(item['api'] + ' actual post-VS positions are available', bool(geometry.get('positions')))
                self.check(item['api'] + ' mesh read preserves visible selection',
                           int(self.ctx.CurEvent()) == visible and int(self.ctx.CurSelectedEvent()) == selected)
                shader = bridge._get_shader_info({'event_id': chosen['eventId'], 'stage': 'vertex'})
                self.check(item['api'] + ' actual compiled vertex interface is read', bool(shader.get('compiledInterface')))
                entry = {'api': item['api'], 'event': chosen, 'ordinaryDrawCount': len(ordinary),
                         'outline': snapshot, 'vertex': shader, 'geometry': geometry,
                         'captureSha256': before}
                self.result['captures'].append(entry)
                self.check(item['api'] + ' replay remains healthy', bridge._get_replay_health({}).get('healthy') is True)
                self.ctx.CloseCapture()
                QtWidgets.QApplication.processEvents()
                self.check(item['api'] + ' closed event tree has no stale rows', events.model().rowCount() == 0)
                self.check(item['api'] + ' source capture bytes are preserved', hashlib.sha256(capture.read_bytes()).hexdigest() == before)
            self.check('all requested captures completed', len(self.result['captures']) == len(captures) * self.cycles)
            self.result['ok'] = True
        except Exception:
            self.done = True
            self.result['error'] = traceback.format_exc()
        finally:
            self.save()
            if self.done:
                QtCore.QTimer.singleShot(100, self.shutdown)

    def shutdown(self):
        if self.ctx.IsCaptureLoaded():
            self.ctx.CloseCapture()
        self.ctx.GetMainWindow().Widget().close()


runner = Acceptance(pyrenderdoc)
sys.modules['__main__']._bread_blocker_acceptance = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
