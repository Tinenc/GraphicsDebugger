"""Verify inherited annotation framing in an owned application, without an AI request."""
import json
import os
from pathlib import Path
import sys
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtGui, QtWidgets


class Probe:
    def __init__(self):
        self.root = Path(os.environ['RD_ANNOTATION_RUN'])
        self.spec = json.loads((self.root / 'spec.json').read_text(encoding='utf-8'))
        self.capture = Path(self.spec['capture'])
        self.before = self.capture.stat()
        self.app = QtWidgets.QApplication.instance()
        self.phase = 'open'
        self.ready = 0
        self.deadline = time.monotonic() + 120
        self.keep = []
        self.case = 0
        self.cases = [(False, .2), (False, .8), (False, 1.75), (True, 1)]
        self.result = dict(ok=False, cases=[])

    def check(self, value, name):
        if not value:
            raise AssertionError(name)

    def button(self, source):
        text = QtCore.QCoreApplication.translate('AITextureAnnotation', source)
        return next(b for b in self.panel.findChildren(QtWidgets.QAbstractButton) if b.text() == text)

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise TimeoutError(self.phase)
            if time.monotonic() < self.ready:
                return
            if self.phase == 'open':
                bridge = sys.modules.get('renderdoc_mcp_bridge')
                if bridge is None:
                    return
                self.check(not pyrenderdoc.IsCaptureLoaded(), 'owned empty session')
                bridge._cancel_project_auto_activation()
                bridge._schedule_project_auto_activation = lambda: None
                pyrenderdoc.LoadCapture(str(self.capture), rd.ReplayOptions(), str(self.capture), False, True)
                self.phase = 'loaded'
            elif self.phase == 'loaded':
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                eid = int(self.spec['eventId'])
                pyrenderdoc.SetEventID([], eid, eid, True)
                pyrenderdoc.ShowTextureViewer()
                self.viewer = pyrenderdoc.GetTextureViewer()
                self.texture = next(t for t in pyrenderdoc.GetTextures()
                                    if str(t.resourceId).split('::')[-1] == self.spec['resourceId'])
                self.viewer.ViewTexture(self.texture.resourceId, rd.CompType.Typeless, True)
                self.window = pyrenderdoc.GetMainWindow().Widget()
                self.widget = self.viewer.Widget()
                self.render = self.widget.findChild(QtWidgets.QWidget, 'render')
                self.hscroll = self.widget.findChild(QtWidgets.QScrollBar, 'renderHScroll')
                self.vscroll = self.widget.findChild(QtWidgets.QScrollBar, 'renderVScroll')
                self.keep.extend([self.viewer, self.window, self.widget, self.render, self.hscroll, self.vscroll])
                self.window.findChild(QtWidgets.QAction, 'action_AI_Assistant').trigger()
                self.phase = 'binding'
                self.ready = time.monotonic() + 1
            elif self.phase == 'binding':
                self.brush = self.window.findChild(QtWidgets.QPushButton, 'aiAnnotateRenderTarget')
                self.keep.append(self.brush)
                if self.brush is None or not self.brush.isEnabled():
                    return
                self.phase = 'prepare'
            elif self.phase == 'prepare':
                autofit, scale = self.cases[self.case]
                self.viewer.SetZoomLevel(autofit, scale)
                self.phase = 'pan'
                self.ready = time.monotonic() + .4
            elif self.phase == 'pan':
                for scrollbar in (self.hscroll, self.vscroll):
                    if scrollbar.isEnabled():
                        scrollbar.setValue(scrollbar.maximum() // 3)
                self.phase = 'snapshot'
                self.ready = time.monotonic() + .4
            elif self.phase == 'snapshot':
                autofit, requested = self.cases[self.case]
                scale = self.viewer.GetZoomLevel() if autofit else requested
                self.saved = dict(zoom=self.viewer.GetZoomLevel(), autofit=self.viewer.IsZoomAutoFit(),
                                  scroll=[s.value() for s in (self.hscroll, self.vscroll)])
                dpr = self.render.devicePixelRatioF()
                origin = self.render.mapToGlobal(QtCore.QPoint())
                self.expected = [origin.x() - (self.hscroll.value() if self.hscroll.isEnabled() else 0) / dpr,
                                 origin.y() - (self.vscroll.value() if self.vscroll.isEnabled() else 0) / dpr,
                                 self.texture.width * scale / dpr,
                                 self.texture.height * scale / dpr]
                self.render.grab().save(str(self.root / ('case-%d-before.png' % self.case)))
                self.brush.click()
                self.phase = 'canvas'
                self.ready = time.monotonic() + .3
            elif self.phase == 'canvas':
                self.panel = self.window.findChild(QtWidgets.QWidget, 'aiTextureAnnotation')
                if self.panel is None:
                    return
                self.canvas = self.panel.findChild(QtWidgets.QWidget, 'aiAnnotationCanvas')
                self.keep.extend([self.panel, self.canvas])
                self.canvas.grab()
                source = self.canvas.property('annotationSourceSize')
                self.check(source == QtCore.QSize(self.texture.width, self.texture.height),
                           'annotation preserves native RT pixels: ' + str(source))
                bounds = list(self.canvas.property('annotationImageRect'))
                origin = self.canvas.mapToGlobal(QtCore.QPoint())
                actual = [bounds[0] + origin.x(), bounds[1] + origin.y(), bounds[2], bounds[3]]
                error = [abs(a-b) for a, b in zip(actual, self.expected)]
                self.result['cases'].append(dict(before=self.saved, expected=self.expected,
                                                actual=actual, errors=error))
                self.check(max(error[2:]) < .05, 'annotation preserves RT display size: ' + str(error))
                self.check(max(error[:2]) < 1.5, 'annotation preserves RT display position: ' + str(error))
                self.panel.grab().save(str(self.root / ('case-%d-annotation.png' % self.case)))
                self.button('Cancel').click()
                self.phase = 'returned'
                self.ready = time.monotonic() + .4
            elif self.phase == 'returned':
                after = dict(zoom=self.viewer.GetZoomLevel(), autofit=self.viewer.IsZoomAutoFit(),
                             scroll=[s.value() for s in (self.hscroll, self.vscroll)])
                self.check(after == self.saved, 'cancel preserves live zoom and pan')
                self.result['cases'][-1]['afterCancel'] = after
                self.render.grab().save(str(self.root / ('case-%d-after.png' % self.case)))
                self.case += 1
                if self.case < len(self.cases):
                    self.phase = 'prepare'
                else:
                    self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error=None):
        self.timer.stop()
        after = self.capture.stat()
        self.result.update(ok=not error, error=error, phase=self.phase,
                           captureUnchanged=(after.st_size, after.st_mtime_ns) ==
                                            (self.before.st_size, self.before.st_mtime_ns))
        (self.root / 'annotation-view-result.json').write_text(
            json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')
        QtCore.QMetaObject.invokeMethod(pyrenderdoc.GetMainWindow().Widget(), 'close', QtCore.Qt.QueuedConnection)

    def run(self):
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)


probe = Probe()
sys.modules['__main__']._bread_annotation_view_probe = probe
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.run)
