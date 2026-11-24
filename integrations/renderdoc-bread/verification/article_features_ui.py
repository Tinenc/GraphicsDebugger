"""Run the author's annotation checks, then the real DF mesh/UV viewer.

Requires an owned GUI with isolated configuration, plus the two-draw fixture
created by run_article_features.py. No external model or user capture is used.
"""
from pathlib import Path
import base64
import os
import sys
import traceback

repo = Path(os.environ['BREAD_ARTICLE_REPO'])
source = (repo / 'replaylab/tests/fixtures/qrenderdoc_rt_annotation_smoke.py').read_text(encoding='utf-8')
exec(compile(source.split('\nrunner = AnnotationSmoke()')[0], str(repo / 'replaylab/tests/fixtures/qrenderdoc_rt_annotation_smoke.py'), 'exec'))


class ArticleFeatures(AnnotationSmoke):
    def finish(self, error=None):
        if error or self.phase.startswith('mesh-'):
            AnnotationSmoke.finish(self, error)
        else:
            self.result['annotationPassed'] = True
            self.result['renderTarget'] = next(str(t.resourceId).split('::')[-1]
                for t in pyrenderdoc.GetTextures()
                if pyrenderdoc.GetResourceName(t.resourceId) == 'BreadVerificationColor')
            self.phase = 'mesh-open'

    def click_at(self, widget, x, y):
        p = QtCore.QPointF(widget.width() * x, widget.height() * y)
        for kind, buttons in ((QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton),
                              (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)):
            self.app.sendEvent(widget, QtGui.QMouseEvent(kind, p, QtCore.Qt.LeftButton,
                                                       buttons, QtCore.Qt.NoModifier))

    def tick(self):
        if not self.phase.startswith('mesh-'):
            result = AnnotationSmoke.tick(self)
            if hasattr(self, 'window') and not getattr(self, 'sized', False):
                self.window.resize(1600, 1000)
                self.sized = True
            return result
        try:
            if time.monotonic() - self.started > 120:
                raise RuntimeError('Mesh/UV acceptance timed out at ' + self.phase)
            if self.phase == 'mesh-open':
                pyrenderdoc.SetEventID([], self.anchor, self.anchor, True)
                pyrenderdoc.ShowMeshPreview()
                self.mesh = pyrenderdoc.GetMeshPreview()
                self.mesh.ShowMeshData(rd.MeshDataStage.VSIn)
                self.view = self.mesh.Widget()
                pyrenderdoc.RaiseDockWindow(self.view)
                self.keep.extend([self.mesh, self.view])
                toggle = self.view.findChild(QtWidgets.QToolButton, 'meshUVToggle')
                self.keep.append(toggle)
                self.check('UV viewport toggle is available', toggle is not None)
                toggle.setChecked(True)
                self.phase = 'mesh-ready'
                return
            if self.phase == 'mesh-ready':
                self.atlas = self.view.findChild(QtWidgets.QWidget, 'meshUVCanvas')
                self.native = self.view.findChild(QtWidgets.QWidget, 'render')
                if not self.atlas or not self.atlas.property('uvTriangleCount'):
                    return
                self.keep.extend([self.atlas, self.native])
                self.check('live UV geometry contains captured triangle', self.atlas.property('uvTriangleCount') == 1)
                channels = self.view.findChild(QtWidgets.QComboBox, 'meshUVChannels')
                textures = self.view.findChild(QtWidgets.QComboBox, 'meshUVTextures')
                self.keep.extend([channels, textures])
                self.check('UV channel selector populated', channels.count() > 0)
                self.result['uvChannels'] = [channels.itemText(i) for i in range(channels.count())]
                uv_index = next(i for i in range(channels.count()) if 'TEXCOORD' in channels.itemText(i).upper())
                channels.setCurrentIndex(uv_index)
                self.result['selectedUVChannel'] = channels.itemText(uv_index)
                self.check('bound texture selector populated', textures.count() >= 2)
                self.result['textureOptions'] = [textures.itemText(i) for i in range(textures.count())]
                textures.setCurrentIndex(1)
                self.phase = 'mesh-texture'
                return
            if self.phase == 'mesh-texture':
                if not self.atlas.property('uvTextureReady'):
                    return
                self.check('captured texture loaded in UV viewport', True)
                for channel in 'RGBA':
                    control = self.view.findChild(QtWidgets.QCheckBox, 'meshUVChannel' + channel)
                    self.keep.append(control)
                    self.check('texture channel control ' + channel, control is not None)
                    control.setChecked(channel == 'R')
                menu = self.view.findChild(QtWidgets.QToolButton, 'meshUVTextureChannels')
                self.keep.append(menu)
                self.check('single channel reflected by native control', menu.text() == 'R')
                for channel in 'RGB':
                    self.view.findChild(QtWidgets.QCheckBox, 'meshUVChannel' + channel).setChecked(True)
                self.phase = 'mesh-select'
                return
            if self.phase == 'mesh-select':
                # A selected point is rendered as a six-vertex marker in the native overlay.
                for mode, count in (('Face', 3), ('Point', 6), ('Edge', 2)):
                    control = self.view.findChild(QtWidgets.QToolButton, 'meshUV' + mode)
                    self.keep.append(control)
                    self.check(mode + ' selection tool available', control is not None and control.isVisible())
                    control.click()
                    self.app.sendEvent(self.atlas, QtGui.QKeyEvent(QtCore.QEvent.KeyPress,
                        QtCore.Qt.Key_A, QtCore.Qt.AltModifier))
                    hit = False
                    for y in range(1, 20):
                        for x in range(1, 20):
                            self.click_at(self.atlas, x / 20.0, y / 20.0)
                            if self.atlas.property('uvSelectionCount'):
                                hit = True
                                break
                        if hit:
                            break
                    self.check(mode + ' selects captured UV component', hit)
                    self.check(mode + ' selects exactly one component', self.atlas.property('uvSelectionCount') == 1)
                    self.check(mode + ' UV selection highlights native 3D vertices',
                        int(self.native.property('uvHighlightedVertices')) == count)
                control = self.view.findChild(QtWidgets.QComboBox, 'controlType')
                fov = self.view.findChild(QtWidgets.QSlider, 'cameraFov')
                self.keep.extend([control, fov])
                self.check('DCC controls and FOV are present', control is not None and control.count() >= 2 and fov is not None)
                self.result['cameraControls'] = [control.itemText(i) for i in range(control.count())]
                presets = self.view.findChildren(QtWidgets.QComboBox)
                self.keep.extend(presets)
                preset = next(c for c in presets if c.findText('3ds Max') >= 0 and c.findText('Maya') >= 0)
                self.result['cameraPresets'] = [preset.itemText(i) for i in range(preset.count())]
                previous = preset.currentIndex()
                preset.setCurrentIndex(preset.findText('Maya'))
                preset.activated[int].emit(preset.currentIndex())
                self.check('Maya navigation preset can be selected', preset.currentText() == 'Maya')
                preset.setCurrentIndex(previous)
                preset.activated[int].emit(previous)
                original = fov.value()
                changed = original + 1 if original < fov.maximum() else original - 1
                fov.setValue(changed)
                self.check('FOV can be adjusted', fov.value() == changed)
                fov.setValue(original)
                self.view.grab().save(str(self.output.with_name('mesh-uv.png')))
                self.window.grab().save(str(self.output.with_name('mesh-window.png')))
                pattern = next(str(t.resourceId).split('::')[-1] for t in pyrenderdoc.GetTextures()
                               if t.width == 2 and t.height == 2)
                self.view.setProperty('meshEvidenceRequest', json.dumps({
                    'requestId':'df-article-mesh', 'eventId':self.anchor, 'stage':'vs-in',
                    'channel':'TEXCOORD', 'pair':0, 'textureResourceId':pattern,
                    'selectionMode':'face', 'selectedVertices':[0,1,2],
                    'framing':'context', 'viewAngles':[0,0,0]}))
                self.phase = 'mesh-capture'
                return
            if self.phase == 'mesh-capture':
                # QWidget.grab cannot read the native GPU child window. Use the
                # production mesh evidence API, which reads its replay output.
                evidence = dict(self.view.property('meshEvidence') or {})
                if not evidence.get('ready'):
                    return
                for field, name in (('meshPng', 'mesh-gpu.png'), ('uvPng', 'uv-evidence.png')):
                    data = base64.b64decode(evidence[field])
                    preview = QtGui.QImage.fromData(data)
                    self.check(name + ' is a real nonempty snapshot', not preview.isNull())
                    colors = set(preview.pixel(x, y) for y in range(0, preview.height(), 3)
                                 for x in range(0, preview.width(), 3))
                    self.check(name + ' contains visible geometry', len(colors) > 3)
                    self.output.with_name(name).write_bytes(data)
                self.result['meshEvidence'] = {k:v for k,v in evidence.items() if not k.endswith('Png')}
                self.result['meshPassed'] = True
                self.finish()
        except Exception:
            self.finish(traceback.format_exc())


runner = ArticleFeatures()
sys.modules['__main__']._bread_article_features = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
