"""Native HDR/atlas exposure and Present-backbuffer acceptance (Python 3.6).

Run an isolated --ui-python instance with BREAD_EXPOSURE_SMOKE_ROOT, _CAPTURE,
_EVENT, _SOURCE (bridge directory). LOCALAPPDATA and RENDERDOC_CONFIG_DIR must
be inside the system-temp root. The fixture never writes the capture/project.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PySide2 import QtCore, QtGui
import renderdoc as rd


class ExposureSmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['BREAD_EXPOSURE_SMOKE_ROOT']).resolve()
        if os.path.commonpath([str(self.root), tempfile.gettempdir()]) != tempfile.gettempdir():
            raise RuntimeError('Native exposure smoke requires system-temp isolation')
        for key in ('LOCALAPPDATA', 'RENDERDOC_CONFIG_DIR'):
            if os.path.commonpath([str(self.root), str(Path(os.environ[key]).resolve())]) != str(self.root):
                raise RuntimeError('Native exposure smoke must isolate ' + key)
        self.capture = os.environ['BREAD_EXPOSURE_SMOKE_CAPTURE']
        self.event = int(os.environ['BREAD_EXPOSURE_SMOKE_EVENT'])
        self.source = Path(os.environ['BREAD_EXPOSURE_SMOKE_SOURCE'])
        info = os.stat(self.capture)
        self.source_stat = (info.st_size, info.st_mtime_ns)
        self.result = {'ok': False, 'phase': 'startup', 'pid': os.getpid(), 'eventId': self.event}
        self.started, self.busy, self.done, self.last_tick = time.monotonic(), False, False, None
        self.gaps = []

    def save(self):
        (self.root / 'result.json').write_text(json.dumps(self.result, indent=2, ensure_ascii=False), encoding='utf-8')

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def open_capture(self):
        try:
            self.result['phase'] = 'loading'
            self.save()
            self.ctx.LoadCapture(self.capture, rd.ReplayOptions(), self.capture, False, True)
            self.result['phase'] = 'loaded'
            self.save()
        except Exception:
            self.finish(None, traceback.format_exc())

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name, str(self.source / (name + '.py')))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def image_stats(self, path):
        bridge = sys.modules['renderdoc_mcp_bridge']
        reduction = bridge._reduce_saved_image(str(path), None, 320, opaque_rgb=True)
        image = QtGui.QImage(str(path))
        if image.isNull() or image.hasAlphaChannel():
            raise RuntimeError('Texture preview did not preserve opaque RGB')
        total, visible, luminance = image.width() * image.height(), 0, 0.0
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                maximum = max(color.red(), color.green(), color.blue())
                visible += maximum > 8
                luminance += (color.red() + color.green() + color.blue()) / 3.0
        return {'visibleFraction': float(visible) / max(1, total),
                'meanRgb': luminance / max(1, total), 'reduction': reduction}

    def check_alpha_reduction(self):
        bridge = sys.modules['renderdoc_mcp_bridge']
        rows = []
        for maximum in (None, 4):
            path = self.root / ('alpha-preservation-%s.png' % maximum)
            image = QtGui.QImage(16, 16, QtGui.QImage.Format_ARGB32)
            image.fill(QtGui.QColor(32, 96, 160, 0))
            if not image.save(str(path), 'PNG'):
                raise RuntimeError('Alpha regression input could not be saved')
            receipt = bridge._reduce_saved_image(str(path), None, maximum, opaque_rgb=True)
            output = QtGui.QImage(str(path))
            for y in range(output.height()):
                for x in range(output.width()):
                    if output.pixelColor(x, y).getRgb() != (32, 96, 160, 255):
                        raise RuntimeError('Transparent scene RGB was lost during reduction')
            if output.width() != (maximum or 16) or output.hasAlphaChannel():
                raise RuntimeError('Opaque preview dimensions or alpha are wrong')
            rows.append({'maxDimension': maximum, 'receipt': receipt})
        return rows

    def run(self, ctrl, original_event):
        exposure = self.load('texture_preview_range')
        binding = self.load('frame_stage_evidence')
        textures = {str(texture.resourceId): texture for texture in ctrl.GetTextures()}
        rows = []
        try:
            ctrl.SetFrameEvent(self.event, True)
            for index, target in enumerate(ctrl.GetPipelineState().GetOutputTargets()):
                texture = textures.get(str(target.resource))
                if texture is None:
                    continue
                sub = rd.Subresource(target.firstMip, target.firstSlice, 0)
                minimum, maximum = ctrl.GetMinMax(target.resource, sub, rd.CompType.Typeless)
                count = min(3, int(texture.format.compCount))
                low, high = list(minimum.floatValue), list(maximum.floatValue)
                mask = [channel < count for channel in range(4)]
                def histogram(lower, upper, channels):
                    return ctrl.GetHistogram(target.resource, sub, rd.CompType.Typeless, lower, upper, channels)
                started = time.monotonic()
                black, white, receipt = exposure.visible_distribution_range(
                    histogram, low, high, mask, normalized=texture.format.compType in (rd.CompType.UNorm, rd.CompType.SNorm))
                row = {'index': index, 'resourceId': str(target.resource),
                       'format': texture.format.Name(), 'distributionMs': (time.monotonic() - started) * 1000,
                       'oldRange': [min(low[:count]), max(high[:count])],
                       'newRange': [black, white], 'distribution': receipt}
                for label, endpoints in (('before', row['oldRange']), ('after', row['newRange'])):
                    save = rd.TextureSave()
                    save.resourceId, save.typeCast, save.destType = target.resource, rd.CompType.Typeless, rd.FileType.PNG
                    save.mip, save.slice.sliceIndex = target.firstMip, target.firstSlice
                    save.sample.mapToArray, save.sample.sampleIndex = False, 0
                    save.alpha, save.channelExtract = rd.AlphaMapping.Discard, -1
                    save.comp.blackPoint, save.comp.whitePoint = endpoints
                    if endpoints[0] >= endpoints[1]:
                        save.comp.blackPoint, save.comp.whitePoint = 0.0, 1.0
                    path = self.root / ('rt%d-%s.png' % (index, label))
                    result = ctrl.SaveTexture(save, str(path))
                    if result.code != rd.ResultCode.Succeeded:
                        raise RuntimeError('Native texture export failed: ' + str(result))
                    row[label] = self.image_stats(path)
                    row[label]['path'] = str(path)
                rows.append(row)
            actions = []
            def walk(items):
                for item in items:
                    if int(item.flags) & 0x100:
                        actions.append(item)
                    walk(item.children)
            walk(ctrl.GetRootActions())
            if not actions:
                raise RuntimeError('Acceptance capture has no Present event')
            present = max(actions, key=lambda action: action.eventId)
            ctrl.SetFrameEvent(present.eventId, True)
            target = binding.present_output_binding(present, ctrl, lambda resource: str(resource) if resource != rd.ResourceId.Null() else '')
            save = rd.TextureSave()
            save.resourceId, save.destType = target.resource, rd.FileType.PNG
            save.mip, save.slice.sliceIndex = 0, 0
            save.sample.mapToArray, save.sample.sampleIndex = False, 0
            save.alpha, save.channelExtract = rd.AlphaMapping.Discard, -1
            save.comp.blackPoint, save.comp.whitePoint = 0.0, 1.0
            present_path = self.root / 'present-composite.png'
            result = ctrl.SaveTexture(save, str(present_path))
            if result.code != rd.ResultCode.Succeeded:
                raise RuntimeError('Native Present export failed: ' + str(result))
            (self.root / 'present-raw-rgba.png').write_bytes(present_path.read_bytes())
            present_result = self.image_stats(present_path)
            present_result.update(eventId=int(present.eventId), resourceId=str(target.resource), path=str(present_path))
            return {'textures': rows, 'present': present_result}
        finally:
            ctrl.SetFrameEvent(original_event, True)

    def tick(self):
        now = time.monotonic()
        if self.last_tick is not None and self.busy:
            self.gaps.append((now - self.last_tick) * 1000)
        self.last_tick = now
        if self.done:
            return
        if now - self.started > 240 and not self.busy:
            return self.finish(None, 'Native texture exposure smoke timed out')
        if self.result['phase'] == 'startup':
            bridge = sys.modules.get('renderdoc_mcp_bridge')
            if bridge is None or not getattr(bridge, '_shader_workspace_ui', None):
                return
            try:
                self.result['alphaPreservation'] = self.check_alpha_reduction()
            except Exception:
                return self.finish(None, traceback.format_exc())
            self.result['phase'] = 'opening'
            QtCore.QTimer.singleShot(0, self.open_capture)
            return
        if self.busy or self.result['phase'] != 'loaded' or not self.ctx.IsCaptureLoaded():
            return
        self.busy = True
        self.result['phase'] = 'native replay'
        self.save()
        original = int(self.ctx.CurEvent())
        def worker(ctrl):
            try:
                result, error = self.run(ctrl, original), None
            except Exception:
                result, error = None, traceback.format_exc()
            self.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: self.finish(result, error))
        self.ctx.Replay().AsyncInvoke(worker)

    def finish(self, result, error):
        self.done = True
        self.timer.stop()
        if result:
            self.result.update(result)
        info = os.stat(self.capture)
        unchanged = self.source_stat == (info.st_size, info.st_mtime_ns)
        self.result.update(ok=not error and unchanged, error=error, captureUnchanged=unchanged,
                           phase='complete', maxTimerGapMs=max(self.gaps or [0]),
                           elapsedSeconds=time.monotonic()-self.started)
        self.save()
        QtCore.QTimer.singleShot(100, self.ctx.GetMainWindow().Widget().close)


try:
    runner = ExposureSmoke(pyrenderdoc)
    sys.modules['__main__']._bread_exposure_smoke = runner
    runner.save()
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
except Exception:
    (Path(os.environ['BREAD_EXPOSURE_SMOKE_ROOT']) / 'result.json').write_text(
        json.dumps({'ok': False, 'phase': 'bootstrap', 'error': traceback.format_exc()}, indent=2),
        encoding='utf-8')
