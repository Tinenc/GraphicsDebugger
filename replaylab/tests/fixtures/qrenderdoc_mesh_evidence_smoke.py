"""Read installed native mesh evidence in an isolated --ui-python instance.

Set BREAD_MESH_SMOKE_ROOT (system temp), BREAD_MESH_SMOKE_CAPTURE, and isolated
LOCALAPPDATA/RENDERDOC_CONFIG_DIR under the root. Optional BREAD_MESH_SMOKE_EVENT
selects one exact test draw; otherwise a regular nonzero draw is discovered.
Optional BREAD_UINT_SMOKE_EVENT/KIND/INDEX/STAGE adds a float32-bit texture read
in the same capture. It is disabled unless an explicit positive event is given.
No AI, project restore, diagram publication, capture save or Shader edit occurs.
Native geometry.json can subsequently be rendered by the Host Python helper.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
import uuid

from PySide2 import QtCore


def within(child, parent):
    child, parent = os.path.normcase(str(Path(child).resolve())), os.path.normcase(str(Path(parent).resolve()))
    return child != parent and os.path.commonpath([child, parent]) == parent


def stamp(path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def sidecars(capture):
    root = Path(str(capture) + '.bread')
    return {str(path.relative_to(root)): stamp(path) for path in root.rglob('*') if path.is_file()}


class MeshSmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['BREAD_MESH_SMOKE_ROOT']).resolve()
        if not within(self.root, tempfile.gettempdir()):
            raise RuntimeError('Mesh smoke needs an isolated system-temp directory')
        for key in ('LOCALAPPDATA', 'RENDERDOC_CONFIG_DIR'):
            if not within(os.environ[key], self.root):
                raise RuntimeError('Mesh smoke needs isolated ' + key)
        self.root.mkdir(parents=True, exist_ok=True)
        self.capture = Path(os.environ['BREAD_MESH_SMOKE_CAPTURE']).resolve()
        self.result = {'schema': 'bread-mesh-native-smoke/v1', 'ok': False, 'checks': [], 'pid': os.getpid()}
        self.started, self.busy, self.done = time.monotonic(), False, False

    def save(self):
        self.result['elapsedSeconds'] = time.monotonic() - self.started
        (self.root / 'result.json').write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(self, name, condition):
        self.result['checks'].append({'name': name, 'ok': bool(condition)})
        self.save()
        if not condition:
            raise AssertionError(name)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def uint_smoke(self, visible, selected):
        event = int(os.environ.get('BREAD_UINT_SMOKE_EVENT', '0'))
        if event <= 0:
            return
        request_id = uuid.uuid4().hex
        artifact_dir = self.root / ('eid-%d-%s' % (event, request_id))
        artifact_dir.mkdir()
        params = {
            'event_id': event,
            'attachment_kind': os.environ.get('BREAD_UINT_SMOKE_KIND', 'uav'),
            'attachment_index': int(os.environ.get('BREAD_UINT_SMOKE_INDEX', '0')),
            'stage': os.environ.get('BREAD_UINT_SMOKE_STAGE', 'fragment'),
            'mip': 0, 'slice': 0, 'sample': 0, 'channel': 'r',
            'max_dimension': 1024, 'range_mode': 'visible-distribution',
            'value_decode': 'float32-bits',
            'expected_capture_session_id': self.bridge._capture_session_id,
            'request_id': request_id, 'artifact_dir': str(artifact_dir),
        }
        receipt = self.bridge._capture_event_texture_evidence(params)
        (self.root / 'uint-receipt.json').write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        self.result['uintReceiptPath'] = str(self.root / 'uint-receipt.json')
        artifact = receipt.get('artifact', {})
        attachment = artifact.get('attachment', {})
        decoding = artifact.get('display', {}).get('decoding', {})
        expected_pixels = int(attachment.get('viewWidth', 0)) * int(attachment.get('viewHeight', 0))
        self.check('uint evidence is bound to exact event/session', receipt.get('eventId') == event and
                   receipt.get('captureSessionId') == self.bridge._capture_session_id and
                   receipt.get('exactEventBinding') is True)
        self.check('uint evidence decodes complete unmodified subresource', artifact.get('status') == 'saved' and
                   expected_pixels > 0 and decoding.get('fullSubresource') is True and
                   decoding.get('pixelCount') == expected_pixels and
                   decoding.get('sourceBytes') == expected_pixels * 4 and
                   decoding.get('sourceBytesModified') is False and
                   len(decoding.get('sourceSha256', '')) == 64)
        self.check('uint finite and nonfinite counts cover every pixel',
                   decoding.get('finiteCount', -1) + decoding.get('nonFiniteCount', -1) == expected_pixels)
        output = artifact.get('output', {})
        png = Path(output.get('path', ''))
        self.check('uint output remains in isolated artifact directory', within(png, artifact_dir))
        content = png.read_bytes()
        self.check('uint output is verified PNG', content[:8] == b'\x89PNG\r\n\x1a\n' and
                   hashlib.sha256(content).hexdigest() == output.get('sha256'))
        self.check('uint read restores EID and selection', receipt.get('restoration', {}).get('restored') is True and
                   receipt.get('restoration', {}).get('originalEventId') == visible and
                   int(self.ctx.CurEvent()) == visible and int(self.ctx.CurSelectedEvent()) == selected)

    def tick(self):
        if self.busy or self.done:
            return
        self.busy = True
        try:
            if time.monotonic() - self.started > 300:
                raise RuntimeError('Mesh smoke exceeded 300 seconds')
            bridge = sys.modules.get('renderdoc_mcp_bridge')
            if bridge is None or bridge._ctx is None:
                return
            self.bridge, self.ctx = bridge, bridge._ctx
            self.check('isolated instance has no capture before test', not self.ctx.IsCaptureLoaded())
            bridge._schedule_project_auto_activation = lambda *args, **kwargs: None
            bridge._schedule_frame_render_flow_restore = lambda *args, **kwargs: None
            bridge._cancel_project_auto_activation()
            bridge._abandon_project_launcher_restore()
            before, before_sidecars = stamp(self.capture), sidecars(self.capture)
            self.ctx.LoadCapture(str(self.capture), self.ctx.Config().DefaultReplayOptions,
                                 str(self.capture), False, True)
            self.check('capture loaded read-only without project restore', self.ctx.IsCaptureLoaded())
            event = int(os.environ.get('BREAD_MESH_SMOKE_EVENT', '0'))
            if not event:
                def choose(ctrl):
                    pending = list(ctrl.GetRootActions())
                    while pending:
                        action = pending.pop(0)
                        pending[0:0] = list(action.children)
                        if (int(action.flags) & 2 and not int(action.flags) & 0x080000 and
                                100 <= int(action.numIndices) <= 100000 and int(action.numInstances) == 1):
                            return int(action.eventId)
                    raise RuntimeError('No regular geometry draw was found for this fixture')
                event = bridge._replay(choose)
            visible = int(self.ctx.CurEvent())
            selected = int(self.ctx.CurSelectedEvent())
            self.result.update(eventId=event, visibleEventId=visible, bridgeVersion=bridge.BRIDGE_INTEGRATION_VERSION)
            geometry = bridge._get_post_vs_data({'event_id': event, 'max_vertices': 4096, 'max_indices': 12288})
            (self.root / 'geometry.json').write_text(json.dumps(geometry, ensure_ascii=False, indent=2), encoding='utf-8')
            self.result['geometry'] = {key: value for key, value in geometry.items() if key not in ('positions', 'indices')}
            self.check('native read is bound to exact event/session', geometry.get('eventId') == event and
                       geometry.get('captureSessionId') == bridge._capture_session_id)
            self.check('native positions and indices are decoded', bool(geometry.get('positions')) and bool(geometry.get('indices')))
            self.check('positions identify clip space', geometry.get('coordinateSpace') == 'clip')
            self.check('visible and selected EIDs are preserved', int(self.ctx.CurEvent()) == visible and
                       int(self.ctx.CurSelectedEvent()) == selected)
            self.check('native controller remains healthy', bridge._get_replay_health({}).get('healthy') is True)
            self.uint_smoke(visible, selected)
            self.check('capture and existing sidecars unchanged', stamp(self.capture) == before and sidecars(self.capture) == before_sidecars)
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
        self.save()
        QtCore.QTimer.singleShot(50, self.shutdown)

    def shutdown(self):
        try:
            if self.ctx.IsCaptureLoaded():
                self.ctx.CloseCapture()
        except Exception:
            self.result['shutdownError'] = traceback.format_exc()
        self.save()
        # MainWindow.close drives extension/replay cleanup before the Qt loop
        # exits; QApplication.quit bypasses that normal shutdown sequence.
        self.ctx.GetMainWindow().Widget().close()


try:
    runner = MeshSmoke(pyrenderdoc)
    sys.modules['__main__']._bread_mesh_smoke = runner
    runner.save()
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
except Exception:
    Path(os.environ['BREAD_MESH_SMOKE_ROOT'], 'result.json').write_text(
        json.dumps({'ok': False, 'error': traceback.format_exc()}), encoding='utf-8')
