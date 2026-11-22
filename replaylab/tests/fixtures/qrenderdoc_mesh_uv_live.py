"""Verify UV and historical parameter navigation on an owned replay session."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide2 import QtCore, QtGui, QtWidgets
import renderdoc as rd


class SurfaceEvents(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.changes = []

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.Show):
            self.changes.append((watched.objectName(), int(event.type())))
        return False


class Probe:
    def __init__(self):
        self.root = Path(os.environ['RD_ANNOTATION_RUN'])
        self.repo = Path(os.environ['RD_VISUAL_FIXTURE_ROOT']).parents[2]
        self.app = QtWidgets.QApplication.instance()
        self.targets = sorted((self.root/'local/ReplayLab/ai-visuals/targets').glob('*.json'))
        self.targets = [p for p in self.targets if not json.loads(p.read_text(encoding='utf-8'))['target'].get('kind')]
        self.capture = json.loads(self.targets[0].read_text(encoding='utf-8'))['capture']['path']
        self.deadline = time.monotonic() + 180
        self.keep = []
        self.phase = 'capture'
        self.result = {'ok': False, 'cbuffer': []}

    def finish(self, error=None):
        self.timer.stop()
        self.result.update(ok=not error, error=error, phase=self.phase)
        (self.root/'mesh-uv-result.json').write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')
        # Run native teardown after returning from the Python timer callback.
        window = pyrenderdoc.GetMainWindow().Widget()
        QtCore.QMetaObject.invokeMethod(window, 'close', QtCore.Qt.QueuedConnection)

    def link(self, url):
        browsers = self.window.findChildren(QtWidgets.QTextBrowser)
        self.keep.extend(browsers)
        chat = next((b for b in browsers if b.objectName() == 'aiConversationBrowser'), None)
        if chat is None:
            # The normal transcript handler is attached to its QTextBrowser.
            for b in browsers:
                b.anchorClicked.emit(QtCore.QUrl(url))
        else:
            chat.anchorClicked.emit(QtCore.QUrl(url))

    def start_worker(self, script, name):
        file = self.root/(name+'.py')
        prefix = """import json,sys,os
from pathlib import Path
root=Path(os.environ['RD_ANNOTATION_RUN'])
repo=Path(os.environ['RD_VISUAL_FIXTURE_ROOT']).parents[2]
sys.path.insert(0,str(repo/'replaylab/mcp_host'))
from src.ipc_client import IPCClient
c=IPCClient(timeout=110)
"""
        file.write_text(prefix + script, encoding='utf-8')
        self.worker_log = (self.root/(name+'.log')).open('w', encoding='utf-8')
        self.worker = subprocess.Popen([str(self.repo/'replaylab/.venv/Scripts/python.exe'), '-X', 'utf8', str(file)],
            stdout=self.worker_log, stderr=subprocess.STDOUT, creationflags=0x08000000)

    def click(self, widget, position):
        self.app.sendEvent(widget, QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonPress, position,
            QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
        self.app.sendEvent(widget, QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonRelease, position,
            QtCore.Qt.LeftButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier))

    def key(self, widget, key, modifiers=QtCore.Qt.NoModifier):
        self.app.sendEvent(widget, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, modifiers))

    def drag(self, widget, start, end, during):
        for event_type, pos, button, buttons in (
                (QtCore.QEvent.MouseButtonPress, start, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton),
                (QtCore.QEvent.MouseMove, end, QtCore.Qt.NoButton, QtCore.Qt.LeftButton),
                (QtCore.QEvent.MouseButtonRelease, end, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)):
            screen = QtCore.QPointF(widget.mapToGlobal(pos.toPoint()))
            self.app.sendEvent(widget, QtGui.QMouseEvent(
                event_type, pos, screen, button, buttons, QtCore.Qt.NoModifier))
            if event_type == QtCore.QEvent.MouseMove:
                during()

    def timings(self, repaint):
        repaint()
        samples = []
        for _ in range(25):
            start = time.perf_counter()
            repaint()
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        return dict(medianMs=round(samples[12], 3), p95Ms=round(samples[23], 3))

    def verify_native_selection(self, view, atlas, native, mode, wireframe):
        viewport = native.findChild(QtWidgets.QWidget)
        assert viewport and viewport.isVisible()
        self.keep.append(viewport)
        width, height = viewport.width(), viewport.height()
        assert width > 60 and height > 60

        def hit(widget):
            width, height = widget.width(), widget.height()
            # Try the middle first, then a grid across the actual viewport.
            coordinates = sorted(
                ((x / 20, y / 20) for x in range(1, 20) for y in range(1, 20)),
                key=lambda pt: abs(pt[0] - .5) + abs(pt[1] - .5))
            for x, y in coordinates:
                position = QtCore.QPointF(width * x, height * y)
                self.click(widget, position)
                if atlas.property('uvSelectionCount'):
                    return position
            raise RuntimeError('No triangle hit in ' + widget.objectName())

        checks = self.result['checks']
        native_mode = mode.currentIndex()
        toolbar = view.findChild(QtWidgets.QWidget, 'meshComponentToolbar')
        assert toolbar and toolbar.isVisible()
        assert toolbar.mapToGlobal(QtCore.QPoint(0, 0)).y() < viewport.mapToGlobal(QtCore.QPoint(0, 0)).y()
        checks['toolbarAboveMesh'] = True
        for name, vertices in (('Face', 3), ('Point', 6), ('Edge', 2)):
            button = view.findChild(QtWidgets.QToolButton, 'meshUV' + name)
            self.keep.append(button)
            assert not button.icon().isNull() and button.toolTip() and button.isVisible()
            assert button.parentWidget() == toolbar
            button.click()
            self.key(atlas, QtCore.Qt.Key_A, QtCore.Qt.AltModifier)
            if name == 'Point':
                assert native.property('uvPointMarkers') > 0
                before_point = atlas.grab().toImage()
            atlas_hit = hit(atlas)
            if name == 'Point':
                after_point = atlas.grab().toImage()
                pixels = []
                for y in range(after_point.height()):
                    for x in range(after_point.width()):
                        p = after_point.pixelColor(x, y)
                        if (p.red() > 220 and 130 < p.green() < 190 and p.blue() < 80 and
                                after_point.pixel(x, y) != before_point.pixel(x, y)):
                            pixels.append((x, y))
                assert pixels
                w = max(x for x, _ in pixels) - min(x for x, _ in pixels) + 1
                h = max(y for _, y in pixels) - min(y for _, y in pixels) + 1
                assert 0.7 < w / h < 1.4, (w, h)
                checks['roundUvPoint'] = dict(width=w, height=h,
                                               unselected3DMarkers=native.property('uvPointMarkers'))
            assert native.property('uvHighlightedVertices') == vertices
            assert mode.currentIndex() == native_mode and native.isVisible()
            self.key(atlas, QtCore.Qt.Key_A, QtCore.Qt.AltModifier)
            native_hit = hit(viewport)
            assert atlas.property('uvSelectionCount') == 1
            assert native.property('uvHighlightedVertices') == vertices
            assert mode.currentIndex() == native_mode and native.isVisible()
            checks[name] = dict(uvToMesh=True, meshToUV=True, nativeViewportPreserved=True,
                                uvPixel=[atlas_hit.x(), atlas_hit.y()],
                                meshPixel=[native_hit.x(), native_hit.y()])

        view.findChild(QtWidgets.QToolButton, 'meshUVFace').click()
        hit(viewport)
        before_vertices = native.property('uvHighlightedVertices')
        assert before_vertices == 3
        view.findChild(QtWidgets.QToolButton, 'meshUVPoint').click()
        assert atlas.property('uvSelectionCount') == 3
        view.findChild(QtWidgets.QToolButton, 'meshUVEdge').click()
        assert atlas.property('uvSelectionCount') == 3
        view.findChild(QtWidgets.QToolButton, 'meshUVFace').click()
        assert atlas.property('uvSelectionCount') == 1
        primary = view.findChild(QtWidgets.QTableView, 'inTable')
        output = view.findChild(QtWidgets.QTableView, 'out1Table')
        self.keep.extend([primary, output])
        selected_rows = lambda table: sorted(i.row() for i in table.selectionModel().selectedRows())
        assert len(selected_rows(primary)) >= 3
        assert selected_rows(primary) == selected_rows(output)
        row = selected_rows(primary)[0]
        primary.selectRow(row)
        assert view.findChild(QtWidgets.QToolButton, 'meshUVPoint').isChecked()
        assert atlas.property('uvSelectionCount') == 1
        assert selected_rows(primary) == selected_rows(output)
        primary.clearSelection()
        assert atlas.property('uvSelectionCount') == 0 and not selected_rows(output)
        checks['componentConversionAndTables'] = True

        zooms = []
        for dx, dy, px, py in ((0, 120, 0, 0), (120, 0, 0, 0), (0, 0, 0, 40)):
            pos = QtCore.QPointF(atlas.rect().center())
            event = QtGui.QWheelEvent(pos, QtCore.QPointF(atlas.mapToGlobal(pos.toPoint())),
                                     QtCore.QPoint(px, py), QtCore.QPoint(dx, dy),
                                     dy or dx, QtCore.Qt.Vertical if dy else QtCore.Qt.Horizontal,
                                     QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
            self.app.sendEvent(atlas, event)
            assert event.isAccepted()
            zooms.append(atlas.property('uvZoom'))
        assert 1.0 < zooms[0] < zooms[1] < zooms[2]
        checks['zoom'] = zooms

        wireframe.setChecked(False)
        assert native.isVisible() and not wireframe.isChecked()
        wireframe.setChecked(True)
        assert native.isVisible() and wireframe.isChecked()
        checks['wireframe'] = True

        splitter = view.findChild(QtWidgets.QSplitter, 'meshUVSplitter')
        assert splitter and not splitter.opaqueResize()
        handle = splitter.handle(1)
        self.keep.extend([splitter, handle])
        sizes = splitter.sizes()
        start = QtCore.QPointF(handle.rect().center())

        def verify_drag():
            assert splitter.sizes() == sizes

        self.drag(handle, start, start - QtCore.QPointF(30, 0), verify_drag)
        assert splitter.sizes() != sizes
        splitter.setSizes(sizes)
        checks['splitterResizesOnRelease'] = True
        self.result['atlasPaint'] = self.timings(atlas.repaint)
        self.result['atlasPaint']['size'] = [atlas.width(), atlas.height()]

        self.key(viewport, QtCore.Qt.Key_3)
        self.key(viewport, QtCore.Qt.Key_B)
        self.drag(viewport, QtCore.QPointF(1, 1),
                  QtCore.QPointF(width - 1, height - 1), lambda: None)
        assert atlas.property('uvSelectionCount') > 1 and native.isVisible()
        checks['meshBoxSelection'] = True
        self.key(viewport, QtCore.Qt.Key_A, QtCore.Qt.AltModifier)
        self.key(viewport, QtCore.Qt.Key_B)

        cursor_probe = os.getenv('RD_MESH_CURSOR_TEST_DLL')
        if cursor_probe:
            import ctypes
            cursor_library = ctypes.WinDLL(cursor_probe)
            set_cursor = cursor_library.setMeshProbeCursor
            set_cursor.argtypes = (ctypes.c_int, ctypes.c_int)
            set_cursor.restype = ctypes.c_int

            def focus_under_pointer(widget):
                cursor = widget.mapToGlobal(widget.rect().center())
                assert set_cursor(cursor.x(), cursor.y())
                assert QtGui.QCursor.pos() == cursor
                key = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_F, QtCore.Qt.NoModifier)
                key.ignore()
                self.app.sendEvent(self.window, key)
                assert key.isAccepted()

            hit(atlas)
            previous_zoom = atlas.property('uvZoom')
            focus_under_pointer(atlas)
            uv_zoom = atlas.property('uvZoom')
            assert uv_zoom != previous_zoom
            focus_under_pointer(viewport)
            assert atlas.property('uvZoom') == uv_zoom
            checks['followsPointerWithFocusElsewhere'] = True

        fov = view.findChild(QtWidgets.QSlider, 'cameraFov')
        fov_value = view.findChild(QtWidgets.QLabel, 'cameraFovValue')
        self.keep.extend([fov, fov_value, toolbar])
        previous_fov = fov.value()
        assert (fov.minimum(), fov.maximum()) == (1, 130)
        for value in (1, 60, 130):
            fov.setValue(value)
            assert str(value) in fov_value.text() and mode.currentIndex() == native_mode
        fov.setValue(previous_fov)
        checks['fovSlider'] = True

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise RuntimeError('Timed out at ' + self.phase)
            if self.phase == 'capture':
                bridge = sys.modules.get('renderdoc_mcp_bridge')
                if not bridge:
                    return
                bridge._cancel_project_auto_activation()
                bridge._schedule_project_auto_activation = lambda: None
                pyrenderdoc.LoadCapture(self.capture, rd.ReplayOptions(), self.capture, False, True)
                self.phase = 'chat'
            elif self.phase == 'chat':
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                self.window = pyrenderdoc.GetMainWindow().Widget()
                self.keep.append(self.window)
                view = pyrenderdoc.GetMeshPreview().Widget()
                toggle = view.findChild(QtWidgets.QToolButton, 'meshUVToggle')
                self.keep.extend([view, toggle])
                assert toggle.isChecked()
                self.result['uvDefaultOpen'] = True
                self.window.findChild(QtWidgets.QAction, 'action_AI_Assistant').trigger()
                self.phase = 'cbuffer-link'
            elif self.phase == 'cbuffer-link':
                if not self.targets:
                    if (self.root/'uv-link.json').is_file():
                        self.open_uv_link()
                        return
                    self.start_worker("""
from src.visual_replies import save_navigation_target
ctx=c.call('get_current_renderdoc_context',{})
observed=c.call('get_mesh_uv_navigation_target',dict(event_id=1291,stage='vs-in',
    channel='_input1',pair=0,texture_resource_id='4047'))
link=save_navigation_target(observed['target'],ctx['capture'])
(root/'uv-link.json').write_text(json.dumps(link),encoding='utf-8')
""", 'uv-link-worker')
                    self.phase = 'uv-link'
                    return
                self.target = self.targets.pop(0)
                self.expected = json.loads(self.target.read_text(encoding='utf-8'))['target']
                pyrenderdoc.SetEventID([], 1326, 1326, True)
                self.link('rdoc://cbuffer/' + self.target.stem)
                self.phase = 'cbuffer-check'
            elif self.phase == 'cbuffer-check':
                widgets = self.window.findChildren(QtWidgets.QWidget)
                self.keep.extend(widgets)
                view = next((w for w in widgets if w.property('cbufferNavigationStatus') == 'selected' and
                             w.property('cbufferNavigationPath') == self.expected['path']), None)
                if view is None:
                    return
                assert int(pyrenderdoc.CurEvent()) == self.expected['eventId']
                self.result['cbuffer'].append(self.expected)
                self.phase = 'cbuffer-link'
            elif self.phase == 'uv-link':
                if self.worker.poll() is None:
                    return
                self.worker_log.close()
                if self.worker.returncode:
                    raise RuntimeError((self.root/'uv-link-worker.log').read_text(encoding='utf-8'))
                self.open_uv_link()
            elif self.phase == 'uv-view':
                view = pyrenderdoc.GetMeshPreview().Widget()
                atlas = view.findChild(QtWidgets.QWidget, 'meshUVCanvas')
                mesh = view.findChild(QtWidgets.QWidget, 'meshUVTextureView')
                self.keep.extend([view, atlas, mesh])
                if (atlas is None or not atlas.property('uvTextureReady') or
                        not atlas.property('uvTriangleCount') or mesh is None or
                        not mesh.property('uvTextureRendered')):
                    if time.monotonic() - self.uv_started > 25:
                        self.result['uvPending'] = dict(
                            visible=view.isVisible(), atlasVisible=atlas.isVisible() if atlas else False,
                            triangleCount=atlas.property('uvTriangleCount') if atlas else None,
                            textureReady=atlas.property('uvTextureReady') if atlas else None,
                            gpuRendered=mesh.property('uvTextureRendered') if mesh else None)
                        raise RuntimeError('UV data did not reach both previews')
                    return
                channel = view.findChild(QtWidgets.QComboBox, 'meshUVChannels')
                texture = view.findChild(QtWidgets.QComboBox, 'meshUVTextures')
                mode = view.findChild(QtWidgets.QComboBox, 'visualisation')
                native = view.findChild(QtWidgets.QWidget, 'render')
                wireframe = view.findChild(QtWidgets.QToolButton, 'wireframeRender')
                self.keep.extend([channel, texture, mode, native, wireframe])
                if int(pyrenderdoc.CurEvent()) != 1291 or not atlas.isVisible() or not mesh.isVisible():
                    if time.monotonic() - self.uv_started < 8:
                        return
                    self.result['uvVisibility'] = dict(
                        event=int(pyrenderdoc.CurEvent()), view=view.isVisible(),
                        atlas=atlas.isVisible(), mesh=mesh.isVisible(), visualisation=mode.currentText())
                    raise RuntimeError('UV navigation did not expose the linked mesh/texture mode')
                assert not native.isVisible()
                self.result.update(triangleCount=atlas.property('uvTriangleCount'), channel=channel.currentText(),
                                   texture=texture.currentText(), textureChoices=texture.count())
                assert channel.currentIndex() >= 0 and texture.currentIndex() > 0
                wireframe.setChecked(False)
                self.native_state = (view, atlas, native, mode, wireframe)
                self.textured_view = mesh
                self.texture_picker = texture
                self.phase = 'texture-wire-off'
            elif self.phase == 'texture-wire-off':
                if self.textured_view.property('uvWireframeRendered'):
                    return
                self.native_state[-1].setChecked(True)
                self.phase = 'texture-wire-on'
            elif self.phase == 'texture-wire-on':
                if not self.textured_view.property('uvWireframeRendered'):
                    return
                view, atlas, native, mode, wireframe = self.native_state
                assert self.textured_view.isVisible() and not native.isVisible()
                self.result['checks'] = dict(gpuTexture=True, texturedWireframe=True)
                self.surface_events = SurfaceEvents()
                native.installEventFilter(self.surface_events)
                self.textured_view.installEventFilter(self.surface_events)
                self.texture_key = self.textured_view.property('uvTextureKey')
                self.assert_texture_surface(native)
                index = self.texture_picker.currentIndex()
                other = index % (self.texture_picker.count() - 1) + 1
                for target in (other, index, other):
                    self.texture_picker.setCurrentIndex(target)
                    self.assert_texture_surface(native)
                self.phase = 'texture-switch'
            elif self.phase == 'texture-switch':
                view, atlas, native, mode, wireframe = self.native_state
                self.assert_texture_surface(native)
                if self.textured_view.property('uvTextureKey') == self.texture_key:
                    return
                self.result['checks']['textureSwitchWithoutSurfaceChanges'] = True
                native.removeEventFilter(self.surface_events)
                self.textured_view.removeEventFilter(self.surface_events)
                wireframe.setChecked(True)
                mode.setCurrentIndex(int(rd.Visualisation.Solid))
                self.phase = 'native-view'
            elif self.phase == 'native-view':
                view, atlas, native, mode, wireframe = self.native_state
                assert native.isVisible()
                self.verify_native_selection(view, atlas, native, mode, wireframe)
                view.grab().save(str(self.root/'mesh-uv.png'))
                self.start_worker("""
data=c.call('inspect_vertex_effect_scope',dict(event_id=1310,observation_event_id=1654))
(root/'vertex-scope.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
""", 'vertex-scope-worker')
                self.phase = 'vertex-scope'
            elif self.phase == 'vertex-scope':
                if self.worker.poll() is None:
                    return
                self.worker_log.close()
                if self.worker.returncode:
                    raise RuntimeError((self.root/'vertex-scope-worker.log').read_text(encoding='utf-8'))
                self.result['vertexScope'] = json.loads((self.root/'vertex-scope.json').read_text(encoding='utf-8'))
                table = self.native_state[0].findChild(QtWidgets.QTableView, 'inTable')
                self.evidence_indices = sorted({int(table.model().index(row, 1).data()) for row in range(3)})
                self.result['evidenceIndices'] = self.evidence_indices
                pyrenderdoc.SetEventID([], 1326, 1326, True)
                self.start_worker('expected='+repr(self.evidence_indices)+"\n"+"""
from src import server
server.ipc=c
before=c.call('get_current_renderdoc_context',{})['events']
omitted=json.loads(server.capture_mesh_uv_evidence(event_id=1291,inline=False)[0])
assert omitted['textureSelectionRequired']and 66250 in omitted['textureResourceIds'],omitted
assert c.call('get_current_renderdoc_context',{})['events']==before
blocks=server.capture_mesh_uv_evidence(event_id=1291,stage='vs-in',channel='_input1',
    texture_resource_id=4047,selection_mode='point',selected_vertices=expected,inline=False)
result=json.loads(blocks[0])
assert result.get('ready'),result
assert result['channel']=='_input1' and result['navigation']['target']['channel']=='_input1',result
assert set(result['selectedVertices'])==set(expected),result
assert len(result['images'])==2 and result['images'][0]['sha256']!=result['images'][1]['sha256']
assert set(result['navigation']['target']['selectedVertices'])==set(result['selectedVertices'])
from PIL import Image
for image in result['images']:
    with Image.open(image['path']) as im:assert im.width>60 and im.height>60
cases=[result.copy()]
for mode in ('face','edge'):
    answer=json.loads(server.capture_mesh_uv_evidence(event_id=1291,stage='vs-in',channel='_input1',
        texture_resource_id=4047,selection_mode=mode,selected_vertices=expected,inline=False)[0])
    assert answer.get('ready') and answer['selectedCount']>0,answer
    assert answer['navigation']['target']['selectedElements']==sorted(answer['selectedElements'],key=int)
    cases.append(answer)
result['selectionCases']=cases
feature=json.loads(server.capture_mesh_uv_evidence(event_id=1291,stage='vs-in',channel='_input1',
    texture_resource_id=66250,selection_mode='face',selected_elements=['18'],
    selection_expansion='connected',inline=False)[0])
assert feature.get('ready')and feature['selectedCount']>1,feature
assert feature['meshTextureRendered']and feature['uvTextureRendered']and feature['textureName'],feature
assert feature['visualCard']['caption']==feature['textureName']
assert feature['selectionComplete']and len(feature['detailImages'])==2,feature
assert feature['visualCard']['type']=='mesh-uv'
assert feature['visualCard']['annotations']and feature['visualCard']['secondAnnotations']
assert len(feature['navigation']['target']['selectedElements'])==feature['selectedCount']
result['feature']=feature
region=json.loads(server.capture_mesh_uv_evidence(event_id=1291,stage='vs-in',channel='_input1',
    texture_resource_id=66250,selection_mode='face',uv_bounds=[.5,0.,1.,.1],
    include_details=False,inline=False)[0])
assert region.get('ready')and region['selectedCount']>=feature['selectedCount'],region
assert set(feature['selectedVertices'])<=set(region['selectedVertices']),region
result['uvRegionCount']=region['selectedCount']
untextured=json.loads(server.capture_mesh_uv_evidence(event_id=1291,stage='vs-in',channel='_input1',
    texture_resource_id=0,selection_mode='face',selected_elements=[str(i)for i in range(34)],
    view_angles=[-90.,-90.,-90.],inline=False)[0])
assert untextured.get('ready')and untextured['selectedCount']==34,untextured
with Image.open(untextured['images'][0]['path'])as image:assert min(image.size)>=300,image.size
untextured['fullFeature']=True
result['selectionCases'].append(untextured)
result['untexturedFeature']=untextured
large=json.loads(server.capture_mesh_uv_evidence(event_id=1291,stage='vs-in',channel='_input1',
    texture_resource_id=66250,selection_mode='edge',uv_bounds=[-10.,-10.,10.,10.],
    framing='preserve',include_details=False,inline=False)[0])
assert large.get('ready')and large['selectedCount']>4096,large
assert large['selectionComplete']and not large['selectionListComplete'],large
assert len(large['selectedElements'])<=64 and 'selectionKeyData'not in large['navigation']['target'],large
from src.visual_replies import root_path
target_file=root_path()/'targets'/(large['navigation']['id']+'.json')
saved=json.loads(target_file.read_text(encoding='utf-8'))['target']
assert saved['selectionKeyData'].startswith('v1:')and saved['selectedCount']==large['selectedCount']
large['expectedPackedSelection']=saved['selectionKeyData']
result['selectionCases'].append(large)
result['largeSelectionCount']=large['selectedCount']
(root/'native-evidence.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
""", 'native-evidence-worker')
                self.phase = 'native-evidence'
            elif self.phase == 'native-evidence':
                if self.worker.poll() is None:
                    return
                self.worker_log.close()
                if self.worker.returncode:
                    raise RuntimeError((self.root/'native-evidence-worker.log').read_text(encoding='utf-8'))
                self.result['nativeEvidence'] = json.loads((self.root/'native-evidence.json').read_text(encoding='utf-8'))
                self.evidence_cases = list(self.result['nativeEvidence']['selectionCases'])
                # Reopen the returned link after clearing; it must restore real component IDs.
                view, atlas, native, mode, wireframe = self.native_state
                self.key(atlas, QtCore.Qt.Key_A, QtCore.Qt.AltModifier)
                assert atlas.property('uvSelectionCount') == 0
                pyrenderdoc.SetEventID([], 1326, 1326, True)
                self.evidence_case = self.evidence_cases.pop(0)
                self.link(self.evidence_case['navigation']['url'])
                self.phase = 'evidence-navigation'
            elif self.phase == 'evidence-navigation':
                view = pyrenderdoc.GetMeshPreview().Widget()
                value = view.property('meshEvidence')
                if not value or not value.get('ready'):
                    return
                assert value['selectionMode'] == self.evidence_case['selectionMode']
                if 'expectedPackedSelection' in self.evidence_case:
                    assert value['selectedCount']==self.evidence_case['selectedCount']
                    assert value['selectionKeyData']==self.evidence_case['expectedPackedSelection']
                    self.result['checks']['completeLargeSelectionNavigation']=True
                else:
                    assert set(value['selectedVertices']) == set(self.evidence_case['selectedVertices'] if self.evidence_case.get('fullFeature') else self.evidence_indices)
                    assert set(value['selectedElements']) == set(self.evidence_case['selectedElements'])
                if self.evidence_cases:
                    self.evidence_case = self.evidence_cases.pop(0)
                    pyrenderdoc.SetEventID([], 1326, 1326, True)
                    self.link(self.evidence_case['navigation']['url'])
                    return
                self.result['checks']['nativeMeshUVImagesAndSelectionLink'] = True
                self.result['checks']['fullConnectedFeatureAndPairedContext']=self.result['nativeEvidence']['feature']['selectedCount']>1
                self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def assert_texture_surface(self, native):
        assert self.textured_view.isVisible() and not native.isVisible()
        assert self.textured_view.property('uvTextureRendered')
        assert not self.surface_events.changes, self.surface_events.changes

    def open_uv_link(self):
        link = json.loads((self.root/'uv-link.json').read_text(encoding='utf-8'))
        self.link(link['url'])
        self.result['uvTarget'] = link['target']
        self.phase = 'uv-view'
        self.uv_started = time.monotonic()

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)


runner = Probe()
sys.modules['__main__']._bread_uv_probe = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
