"""Isolated native frame-flow UI acceptance via --ui-python.

Set BREAD_FRAME_FLOW_SMOKE_ROOT, BREAD_FRAME_FLOW_SMOKE_SOURCE, and isolate
RENDERDOC_CONFIG_DIR / LOCALAPPDATA below the system-temp root. No capture is
opened, edited, or replayed; navigation uses an observed fake capture boundary.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PySide2 import QtCore, QtGui, QtWidgets
import qrenderdoc as qrd


class FrameFlowSmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['BREAD_FRAME_FLOW_SMOKE_ROOT']).resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        if os.path.commonpath([str(self.root), str(temporary)]) != str(temporary):
            raise RuntimeError('Frame flow smoke requires system-temp isolation')
        for name in ('RENDERDOC_CONFIG_DIR', 'LOCALAPPDATA'):
            path = Path(os.environ[name]).resolve()
            if os.path.commonpath([str(self.root), str(path)]) != str(self.root) or path == self.root:
                raise RuntimeError(name + ' must be below the isolated smoke root')
        self.output = self.root / 'result.json'
        self.result = {'ok': False, 'pid': os.getpid(), 'checks': []}
        self.started, self.last_tick = time.monotonic(), None
        self.gaps, self.export_gaps, self.phase, self.navigations = [], [], 'starting', []
        self.targets = []
        self.binding = {'captureSessionId': 'smoke-a', 'capturePath': str(self.root / 'fake.rdc')}

    def save(self):
        self.result['phase'] = self.phase
        self.output.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(self, name, value):
        if not value:
            raise RuntimeError('Failed: ' + name)
        self.result['checks'].append(name)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def make_document(self):
        doc = {'schema': 'bread-frame-render-flow/v1', 'flowId': 'native-smoke',
               'captureSessionId': self.binding['captureSessionId'],
               'capturePath': self.binding['capturePath'], 'captureId': 'a' * 64,
               'title': '一帧的渲染流程 · Frame rendering flow',
               'summary': '阶段卡片包含完整缩略图、范围和映射说明。箭头表示执行顺序。',
               'coverage': {'totalActionCount': 128, 'coveredActionCount': 128,
                            'omittedActionCount': 0, 'complete': True, 'sourceComplete': True},
               'stages': []}
        for index in range(32):
            begin = index * 4 + 1
            images = []
            for offset in range(8 if index == 0 else 2):
                path = str(self.root / ('preview-%d-%d.png' % (index, offset)))
                image = QtGui.QImage(960 if index == 0 or offset == 0 else 320, 360,
                                     QtGui.QImage.Format_ARGB32 if index == 0 and offset == 0 else QtGui.QImage.Format_RGB32)
                image.fill(QtGui.QColor.fromHsv((index * 21 + offset * 75) % 360, 150, 155,
                                              0 if index == 0 and offset == 0 else 255))
                painter = QtGui.QPainter(image)
                painter.setPen(QtGui.QPen(QtGui.QColor('white'), 8))
                painter.drawRect(image.rect().adjusted(4, 4, -5, -5))
                painter.drawLine(0, 0, image.width(), image.height())
                painter.drawLine(image.width(), 0, 0, image.height())
                painter.end()
                image.save(path)
                images.append({'title': '输出 %d / Output %d' % (offset + 1, offset + 1),
                               'description': '完整图像，边界均可见；数据通道不施加颜色 gamma。',
                               'eventId': begin + (offset % 4), 'path': path, 'channel': 'RGBA',
                               'group': 'channels' if offset == 6 else 'inputs' if offset == 7 else 'outputs',
                               'channelMeaning': 'R: 粗糙度，决定高光扩散的范围。数值越大反射越柔和；G: 金属度；B: 遮蔽；A: 材质标记' if offset == 6 else '',
                               'format': 'R16G16B16A16_FLOAT', 'resourceId': str(index * 10 + offset + 50),
                               'componentCount': 4,
                               'attachmentStage': 'fragment',
                               'displayMapping': {'rangeMode': 'explicit', 'blackPoint': 0,
                                   'whitePoint': 0.75, 'mip': 0, 'slice': 0, 'sample': 0,
                                   'attachmentKind': 'rtv', 'attachmentIndex': offset}})
            if index == 0:
                images[0]['channelGroups'] = [{'channel': channel} for channel in 'rgba']
                images[2]['channelGroups'] = [{'channel': 'rgb', 'description': '基础颜色'},
                                             {'channel': 'a', 'description': '透明覆盖率'}]
                # Deliberately unordered input catches a presenter that simply
                # repeats publication order or stacks channels inside a MRT card.
                images[6].update(parentImageIndex=0, resourceId=images[0]['resourceId'], eventId=begin, channel='b')
                for channel in ('a', 'r', 'g'):
                    images.append(dict(images[6], channel=channel))
                for channel in ('a', 'rgb'):
                    images.append(dict(images[2], group='channels', channel=channel, parentImageIndex=2,
                        channelMeaning='RGB: 基础颜色；A: 透明覆盖率'))
                for channel_image in [value for value in images if value['group'] == 'channels']:
                    channel = channel_image['channel']
                    shade = {'r': 60, 'g': 110, 'b': 160, 'a': 210, 'rgb': 240}[channel]
                    path = str(self.root / ('channel-' + channel + '.png'))
                    bitmap = QtGui.QImage(320, 180, QtGui.QImage.Format_RGB32)
                    bitmap.fill(QtGui.QColor(shade, shade, shade))
                    bitmap.save(path)
                    channel_image['path'] = path
                # Optional gallery detail does not override intentional media.
                # Keep exact duplicates out, but retain another event/exposure.
                for image_index in (0, 4, 5, 7):
                    images[image_index]['presentation'] = 'detail'
                images.append(dict(images[2]))
                images.append(dict(images[2], eventId=begin + 3, presentation='detail'))
                images.append(dict(images[2], presentation='detail',
                    displayMapping=dict(images[2]['displayMapping'], whitePoint=2.0)))
                for image_index in (0, 1):
                    images[image_index].update(
                        description='纹理用途说明 %d；逐帧说明必须随图像切换。' % image_index,
                        channelMeaning='RGB: 场景颜色；A: 覆盖率 %d' % image_index,
                        evidence='verified texture evidence %d' % image_index)
            doc['stages'].append({'id': 'stage-%d' % index,
                                  'title': ('主深度与模板分类 / Depth' if index == 0 else
                                            '不透明物体 GBuffer' if index == 1 else '阶段 %d / Stage %d' % (index + 1, index + 1)),
                                  'summary': '读取前面阶段的资源并输出当前阶段的结果；根据捕获证据说明作用。',
                                  'confidence': 'captured' if index % 2 == 0 else 'inferred',
                                  'startEventId': begin, 'endEventId': begin + 3,
                                  'eventIds': list(range(begin, begin + 4)),
                                  'representativeEventId': begin + 3, 'navigationEventId': begin + 2,
                                  'navigationStatus': 'last-draw', 'navigationHasWork': True,
                                  'category': 'depth' if index == 0 else 'geometry' if index == 1 else 'lighting',
                                  'comparisons': [{'leftImageIndex': 0, 'rightImageIndex': 1,
                                     'label': '光照合成前后 / Lighting comparison',
                                     'relationship': '左侧为输入，右侧为加入光照后的输出；拖动竖杆比较。',
                                     'differentResource': True, 'visualComparable': True}] if index == 0 else [],
                                  'animations': [{'label': '逐步累积灯光 / Light accumulation',
                                      'description': '依次查看中间结果；可以暂停观察当前步骤。',
                                      'imageIndices': [0, 1], 'frameDurationMs': 400}] if index == 0 else [],
                                  'images': images})
            if index == 0:
                doc['stages'][-1]['algorithm'] = {
                    'inputs': [{'eventId': 1, 'resourceId': 50, 'role': '上一阶段的场景颜色',
                                'evidence': '绑定 t0 并采样场景颜色'}],
                    'steps': [{'eventId': 1, 'title': '提取明亮区域',
                               'calculation': '读取场景颜色，只保留超过亮度阈值的部分。',
                               'evidence': 'max(sceneColor - threshold, 0)'},
                              {'eventId': 3, 'title': '横向模糊亮部',
                               'calculation': '沿水平方向采样邻近像素，再按权重求和。',
                               'evidence': 'Sample offsets -2, -1, 0, 1, 2'}],
                    'outputs': [{'eventId': 3, 'resourceId': 52, 'role': '柔和的高亮颜色'}],
                    'integration': [{'sourceEventId': 3, 'eventId': 7, 'resourceId': 52,
                                     'targetResourceId': 60, 'mode': 'downstream',
                                     'explanation': '后续叠加到场景颜色，形成明亮物体周围的光晕。',
                                     'evidence': 'sceneColor + blurredHighlights'}]}
        return doc

    def prepare(self):
        if self.ctx.IsCaptureLoaded():
            raise RuntimeError('Fixture must not open a user capture')
        bridge = sys.modules.get('renderdoc_mcp_bridge')
        if bridge is None or not getattr(bridge, '_shader_workspace_ui', None):
            return False
        self.window = self.ctx.GetMainWindow().Widget()
        # Hidden no-RDC launch can leave a Popup as Qt's only visible window.
        # Closing that popup must not terminate this acceptance runner's loop.
        QtWidgets.QApplication.instance().setQuitOnLastWindowClosed(False)
        spec = importlib.util.spec_from_file_location('bread_frame_flow_smoke_source',
                    os.environ['BREAD_FRAME_FLOW_SMOKE_SOURCE'])
        module = importlib.util.module_from_spec(spec)
        # The source panel now shares the canonical channel-plan implementation.
        module.__package__ = 'renderdoc_mcp_bridge'
        spec.loader.exec_module(module)
        owner = self

        class Context(object):
            def AddDockWindow(self, *args):
                return owner.ctx.AddDockWindow(*args)

            def RaiseDockWindow(self, *args):
                return owner.ctx.RaiseDockWindow(*args)

            def GetAction(self, eid):
                class Action(object):
                    eventId = eid
                return Action() if 0 < eid <= 128 else None

            def SetEventID(self, excluded, selected, current):
                owner.navigations.append((selected, current))

        def navigate(target):
            self.targets.append(target)
            self.navigations.append((target['eventId'], target['eventId']))
            return True

        self.module = module
        self.panel = module.FrameRenderFlowPanel(Context(), qrd, lambda: self.binding,
                                                navigation_callback=navigate)
        self.original_palette = QtGui.QPalette(self.panel.top.palette())
        self.original_palette.setColor(QtGui.QPalette.Window, QtGui.QColor('#333333'))
        self.original_palette.setColor(QtGui.QPalette.Base, QtGui.QColor('#282828'))
        self.original_palette.setColor(QtGui.QPalette.Text, QtGui.QColor('#dddddd'))
        self.original_palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor('#dddddd'))
        self.original_palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor('#3e8cb7'))
        self.original_palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor('#ffffff'))
        self.panel.top.setPalette(self.original_palette)
        document = self.make_document()
        started = time.monotonic()
        state = self.panel.set_document(document)
        self.result['setDocumentMs'] = (time.monotonic() - started) * 1000
        self.panel.show()
        opaque = module._decode_preview(document['stages'][0]['images'][0]['path'])
        sample = opaque.pixelColor(60, 20)
        self.check('zero-alpha texture preserves visible RGB without transparency',
                   sample.alpha() == 255 and sample.red() > 100)
        # The old small/flat sample missed alpha premultiplication inside the
        # decoder's scaled-read path. Compare matching opaque and masked RGB
        # gradients larger than the thumbnail in both dimensions.
        baseline = QtGui.QImage(1280, 720, QtGui.QImage.Format_RGB32)
        masked = QtGui.QImage(1280, 720, QtGui.QImage.Format_ARGB32)
        zero = QtGui.QImage(1280, 720, QtGui.QImage.Format_ARGB32)
        for y in range(720):
            for x in range(1280):
                rgb = QtGui.QColor(x * 255 // 1279, y * 255 // 719, (x + y) * 255 // 1998)
                baseline.setPixelColor(x, y, rgb)
                rgb.setAlpha((0, 32, 128, 255)[(x // 80 + y // 90) % 4])
                masked.setPixelColor(x, y, rgb)
                rgb.setAlpha(0)
                zero.setPixelColor(x, y, rgb)
        decoded = []
        for name, value in [('opaque', baseline), ('mixed-alpha', masked), ('zero-alpha', zero)]:
            path = str(self.root / ('gradient-' + name + '.png'))
            value.save(path)
            decoded.append(module._decode_preview(path))
        self.check('large gradient thumbnails respect size budget',
                   all(image.width() == 640 and image.height() == 360 for image in decoded))
        self.check('mixed-alpha scaling preserves every RGB pixel', decoded[0] == decoded[1])
        self.check('zero-alpha scaling preserves every RGB pixel', decoded[0] == decoded[2])
        self.window.resize(1500, 1000)
        self.check('all 32 stages retained', state['stageCount'] == 32)
        self.check('all 78 preview records retained', state['previewCount'] == 78)
        self.check('diagram setup does not replay', not self.navigations)
        self.phase = 'wait previews'
        return True

    def pointer_event(self, kind, point, button, buttons):
        viewport = self.panel.view.viewport()
        event = QtGui.QMouseEvent(kind, QtCore.QPointF(point),
                    QtCore.QPointF(viewport.mapToGlobal(point)), button, buttons, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(viewport, event)

    def item_point(self, item, local):
        scene = item.mapToScene(local)
        self.panel.view.centerOn(scene)
        return self.panel.view.mapFromScene(scene)

    def click(self, point):
        self.pointer_event(QtCore.QEvent.MouseButtonPress, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
        self.pointer_event(QtCore.QEvent.MouseButtonRelease, point, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)

    def click_view(self, view, point):
        viewport = view.viewport()
        for kind, buttons in ((QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton),
                              (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)):
            event = QtGui.QMouseEvent(kind, QtCore.QPointF(point),
                QtCore.QPointF(viewport.mapToGlobal(point)), QtCore.Qt.LeftButton, buttons, QtCore.Qt.NoModifier)
            QtWidgets.QApplication.sendEvent(viewport, event)

    def check_directory(self):
        if self.panel.get_state()['loadedPreviewCount'] == 0:
            return
        outline, view = self.panel.outline, self.panel.view
        self.check('directory contains all modules in execution order', outline.count() == 32 and
                   [str(outline.item(index).data(QtCore.Qt.UserRole)) for index in range(32)] ==
                   ['stage-%d' % index for index in range(32)])
        self.check('directory is visible and uses a readable native font', self.panel.directory.isVisible() and
                   self.panel.directory.width() >= 140 and outline.font().pointSizeF() >= 10)
        self.check('initial directory selection follows the first module', outline.currentRow() == 0)
        outline.scrollToItem(outline.item(8), QtWidgets.QAbstractItemView.EnsureVisible)
        self.click_view(outline, outline.visualItemRect(outline.item(8)).center())
        visible = view.mapToScene(view.viewport().rect()).boundingRect()
        self.check('directory click scrolls to the module title without replay',
                   view._stages['stage-8'].title.sceneBoundingRect().intersects(visible) and
                   outline.currentRow() == 8 and not self.navigations)
        # Change only the canvas scroll position; the queued signal must update
        # the directory on the next UI turn, including scrolling its own list.
        target_top = view._stages['stage-19'].sceneBoundingRect().top()
        view.verticalScrollBar().setValue(view.verticalScrollBar().value() +
            int((target_top - visible.top()) * view.transform().m11()))
        self.phase = 'check directory scroll'

    def check_directory_scroll(self):
        self.check('canvas scrolling synchronizes directory highlight without replay',
                   self.panel.outline.currentRow() == 19 and not self.navigations)
        item = self.panel.outline.item(19)
        self.check('current directory item remains visible while reading a long diagram',
                   self.panel.outline.viewport().rect().intersects(self.panel.outline.visualItemRect(item)))
        self.panel.top.grab().save(str(self.root / 'directory-reading-dark.png'))
        self.panel.view.verticalScrollBar().setValue(self.panel.view.verticalScrollBar().minimum())
        self.phase = 'check initial previews'

    def finish_checks(self):
        state = self.panel.get_state()
        if state['loadedPreviewCount'] == 0:
            return False
        self.check('thumbnail reads are lazy and bounded', 0 < state['loadedPreviewCount'] < 64
                   and state['pendingPreviewCount'] <= 4)
        view = self.panel.view
        visible = view.mapToScene(view.viewport().rect()).boundingRect()
        self.check('new diagram starts with stage one title visible at the top',
                   view._stages['stage-0'].title.sceneBoundingRect().intersects(visible) and
                   view.verticalScrollBar().value() == view.verticalScrollBar().minimum())
        view.focus_stage('stage-8')
        previous_center = view.mapToScene(view.viewport().rect().center())
        self.panel.set_document(self.panel.document)
        current_center = view.mapToScene(view.viewport().rect().center())
        self.check('same diagram refresh preserves the reading position',
                   abs(previous_center.y() - current_center.y()) < 3 and
                   current_center.y() > view._stages['stage-0'].height)
        item = self.panel.view._stages['stage-0']
        tooltip = item.previews[-1].toolTip()
        self.check('verified display mapping is shown', '0.75' in tooltip and 'MIP' in tooltip)
        self.panel.view.fit_width()
        self.panel.view.zoom(1.1)
        self.panel.search.setText('EID 33')
        self.check('search finds real stage without replay', self.panel._matches == ['stage-8'] and not self.navigations)
        self.panel.search.clear()
        self.panel.view.focus_stage('stage-0')
        self.panel.view.fit_width()
        item = self.panel.view._stages['stage-0']
        grid = [preview for preview in item.previews if isinstance(preview, self.module._PreviewItem)]
        self.check('main gallery keeps only distinct primary results in one row', len(grid) == 2 and
                   grid[0].pos().y() == grid[1].pos().y() and
                   [card.preview['resourceId'] for card in grid] == ['52', '53'])
        self.check('comparison and animation are side by side without duplicate standalone images',
                   item.comparisons[0].y() == item.animations[0].y() and
                   item.comparisons[0].x() < item.animations[0].x() and
                   not any(card.preview['resourceId'] in ('50', '51') for card in grid))
        self.check('explicit comparison and animation retain a detail-tagged source',
                   item.comparisons[0].previews[0].preview['presentation'] == 'detail' and
                   item.animations[0].previews[0].preview['presentation'] == 'detail')
        comparison = item.comparisons[0]
        self.check('comparison retains both texture explanations and channel meanings without standalone cards',
                   all('纹理用途说明 %d' % index in caption.toPlainText() and
                       '覆盖率 %d' % index in caption.toPlainText() and
                       'verified texture evidence %d' % index in comparison.image_links[index].toolTip()
                       for index, caption in enumerate(comparison.image_captions)))
        self.check('channels and inputs start folded', not self.panel.view._expanded_groups and
                   set(item.group_toggles) == {'channels:0', 'channels:2', 'details'})
        self.check('main stage shows concise summary without calculation overview or details',
                   'algorithm' not in item.group_toggles and
                   not hasattr(item, 'algorithm_step_links') and
                   not hasattr(item, 'algorithm_summary_rows') and
                   item.summary.toPlainText() == self.module.concise_caption({'description': item.stage['summary']}))
        self.check_typography(item, grid[0], dark=True)
        arrow = next(node for node in self.panel.view._scene.items()
                     if isinstance(node, QtWidgets.QGraphicsLineItem))
        self.check('arrows are bright and thicker than 3px', arrow.pen().widthF() >= 3 and
                   arrow.pen().color().lightnessF() - self.original_palette.color(QtGui.QPalette.Window).lightnessF() > 0.4)
        self.check('limited category colors distinguish stage groups', item._accent !=
                   self.panel.view._stages['stage-2']._accent)
        point = self.item_point(item, QtCore.QPointF(140, 22))
        self.pointer_event(QtCore.QEvent.MouseButtonPress, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
        self.pointer_event(QtCore.QEvent.MouseMove, point + QtCore.QPoint(100, 0), QtCore.Qt.NoButton, QtCore.Qt.LeftButton)
        self.pointer_event(QtCore.QEvent.MouseButtonRelease, point + QtCore.QPoint(100, 0), QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
        self.check('dragging a stage does not navigate', not self.navigations)
        point = self.item_point(item, QtCore.QPointF(140, 22))
        self.click(point)
        self.check('stage navigation uses last effective draw, not range end', self.navigations == [(3, 3)])
        self.click(self.item_point(item.title, QtCore.QPointF(item.title.boundingRect().width() - 5, 8)))
        self.check('blank area beside title text is not a link', self.navigations == [(3, 3)])
        self.click(self.item_point(item, QtCore.QPointF(1000, item.height - 4)))
        self.check('stage empty space never navigates', self.navigations == [(3, 3)])
        preview = grid[1]
        preview_point = self.item_point(preview, preview.image_rect.center())
        self.click(preview_point)
        self.check('texture image itself never navigates', self.navigations == [(3, 3)])
        self.click(self.item_point(preview.title, QtCore.QPointF(30, 8)))
        self.check('underlined texture title uses its own EID', self.navigations[-1] == (4, 4))
        target = self.targets[-1]
        self.check('preview selects exact texture and numeric mapping', target['resourceId'] == '53' and
                   target['channel'] == 'RGBA' and target['attachmentStage'] == 'fragment' and
                   target['mip'] == target['slice'] == target['sample'] == 0 and
                   target['displayMapping']['blackPoint'] == 0.0 and target['displayMapping']['whitePoint'] == 0.75)
        comparison = item.comparisons[0]
        point = self.item_point(comparison, comparison.image_rect.center())
        before = list(self.navigations)
        self.pointer_event(QtCore.QEvent.MouseButtonPress, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
        self.pointer_event(QtCore.QEvent.MouseMove, point + QtCore.QPoint(120, 0), QtCore.Qt.NoButton, QtCore.Qt.LeftButton)
        self.pointer_event(QtCore.QEvent.MouseButtonRelease, point + QtCore.QPoint(120, 0), QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
        self.check('real divider drag compares different resources without replay', comparison.ratio > 0.6 and self.navigations == before)
        self.click(self.item_point(comparison, comparison.image_rect.topLeft() + QtCore.QPointF(40, 40)))
        self.check('comparison pixels never navigate', self.navigations == before)
        animation = item.animations[0]
        for receiver in animation.previews:
            receiver.pixmap = QtGui.QPixmap.fromImage(self.module._decode_preview(receiver.preview['path']))
        previous_frame = animation.frame
        animation_height = animation.height
        self.check('visible animation advances existing PNG evidence',
                   animation.advance(time.monotonic() + 1, True) and animation.frame != previous_frame)
        self.check('animation switches its texture explanation and evidence without moving the layout',
                   '纹理用途说明 %d' % animation.frame in animation.caption.toPlainText() and
                   '覆盖率 %d' % animation.frame in animation.caption.toPlainText() and
                   'verified texture evidence %d' % animation.frame in animation.image_link.toolTip() and
                   animation.height == animation_height)
        previous_frame = animation.frame
        self.check('offscreen animation is suspended',
                   not animation.advance(time.monotonic() + 2, False) and animation.frame == previous_frame)
        self.click(self.item_point(animation.control, QtCore.QPointF(25, 8)))
        self.check('animation pause control does not navigate', not animation.playing and self.navigations == before)
        self.check('paused animation does not advance', not animation.advance(time.monotonic() + 3, True))
        self.stage_height_before_popup = item.height
        self.stage_previews_before_popup = len(item.previews)
        toggle = item.group_toggles['channels:0']
        self.click(self.item_point(toggle, toggle.boundingRect().center()))
        self.phase = 'check expanded'
        return True

    def check_expanded(self):
        item = self.panel.view._stages['stage-0']
        popup = self.panel.view._channel_popup
        if popup is None or any(card.pixmap is None for card in popup.previews):
            return
        self.check('channel details open in a separate popup without changing the main diagram',
                   popup.isVisible() and len(item.previews) == self.stage_previews_before_popup and
                   item.height == self.stage_height_before_popup and not self.panel.view._expanded_groups)
        row = popup.previews
        self.check('popup displays all four verified channels in RGBA order on one row',
                   [card.preview['channel'] for card in row] == ['r', 'g', 'b', 'a'] and
                   len(set(card.y() for card in row)) == 1 and
                   all(row[index].x() + row[index].width < row[index + 1].x() for index in range(3)))
        self.check('channel titles identify supplied roles without external-link arrows',
                   [card.title.toPlainText() for card in row] ==
                   ['R: 粗糙度', 'G: 金属度', 'B: 遮蔽', 'A: 材质标记'])
        self.check('channel explanation remains below the image in smaller type',
                   '决定高光扩散的范围' in row[0].caption.toPlainText() and
                   '数值越大反射越柔和' in row[0].caption.toPlainText() and
                   row[0].caption.y() > row[0].image_rect.bottom() and
                   row[0].caption.font().pointSizeF() < row[0].title.font().pointSizeF())
        self.check('popup uses real per-channel pixels including captured alpha',
                   all(card.pixmap.toImage().pixelColor(10, 10) == QtGui.QColor(shade, shade, shade)
                       for card, shade in zip(row, (60, 110, 160, 210))))
        point = popup.view.mapFromScene(row[1].title.mapToScene(QtCore.QPointF(28, 8)))
        self.click_view(popup.view, point)
        self.check('channel title keeps exact channel and parent texture navigation',
                   self.targets[-1]['channel'] == 'g' and self.targets[-1]['resourceId'] == '50' and
                   self.targets[-1]['eventId'] == 1)
        self.check('divider position and paused animation survive popup opening',
                   item.comparisons[0].ratio > 0.6 and not item.animations[0].playing)
        popup.grab().save(str(self.root / 'channels-rgba-popup.png'))
        popup.close_button.click()
        self.check('top-right close stops popup image loading without closing the diagram',
                   not popup.isVisible() and not popup._alive and self.panel.view._alive)
        # Let Qt finish releasing the popup's native mouse grab before the
        # next simulated user click. Two clicks in one timer callback can be
        # replayed as an outside click against the newly opened popup.
        self.phase = 'open RGB channels'

    def check_rgb_channels(self):
        item = self.panel.view._stages['stage-0']
        popup = self.panel.view._channel_popup
        self.result['popupDebug'] = {'present': popup is not None,
            'alive': popup._alive if popup is not None else False,
            'visible': popup.isVisible() if popup is not None else False,
            'previews': len(popup.previews) if popup is not None else 0,
            'loaded': sum(card.pixmap is not None for card in popup.previews) if popup is not None else 0,
            'failed': sum(card.failed for card in popup.previews) if popup is not None else 0,
            'pending': len(popup._pending) if popup is not None else 0,
            'togglePending': self.panel.view._pending_group_toggles}
        if popup is None or not popup._alive or any(card.pixmap is None for card in popup.previews):
            return
        row = popup.previews
        self.check('logical RGB and alpha remain two horizontal popup groups',
                   [card.preview['channel'] for card in row] == ['rgb', 'a'] and
                   row[0].y() == row[1].y() and
                   [card.title.toPlainText() for card in row] == ['RGB: 基础颜色', 'A: 透明覆盖率'])
        popup.resize(480, popup.height())
        view = popup.view
        self.check('narrow popup keeps channel text full-size with horizontal scrolling',
                   view.viewport().width() <= 480 and view.transform().m11() == 1.0 and
                   view.horizontalScrollBar().maximum() > view.horizontalScrollBar().minimum())
        view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
        self.check('narrow popup can scroll to the alpha channel',
                   view.mapToScene(view.viewport().rect()).boundingRect().right() >= row[1].sceneBoundingRect().right() - 2)
        popup.grab().save(str(self.root / 'channels-narrow-popup.png'))
        self.click(QtCore.QPoint(-20, -20))
        self.check('click outside closes the channel popup and releases its loader',
                   not popup.isVisible() and not popup._alive)
        self.panel.view._open_channels('stage-0', 'channels:0')
        popup = self.panel.view._channel_popup
        QtWidgets.QApplication.sendEvent(popup, QtGui.QKeyEvent(QtCore.QEvent.KeyPress,
            QtCore.Qt.Key_Escape, QtCore.Qt.NoModifier))
        self.check('Escape closes the channel popup', not popup.isVisible() and not popup._alive)
        self.panel.view._open_channels('stage-0', 'channels:0')
        popup = self.panel.view._channel_popup
        self.panel.set_document(self.panel.document)
        self.check('document refresh closes the stale popup before rebuilding cards',
                   self.panel.view._channel_popup is None and not popup.isVisible())
        self.channel_requests = []
        self.panel.view.channel_request_callback = lambda preview, complete: self.channel_requests.append((preview, complete))
        self.panel.view._rebuild()
        item = self.panel.view._stages['stage-0']
        self.check('texture without saved channels still has an on-demand details entry',
                   'channels:3' in item.group_toggles)
        self.click(self.item_point(item.group_toggles['channels:3'], item.group_toggles['channels:3'].boundingRect().center()))
        self.phase = 'check channel request'

    def channel_response(self, parent, channels):
        return {'images': [dict(parent, channel=channel, group='channels',
            path=str(self.root / ('channel-' + channel + '.png')),
            description='按需读取实际通道', channelMeaning='实际通道数据 ' + channel.upper())
            for channel in channels]}

    def check_channel_request(self):
        popup = self.panel.view._channel_popup
        if popup is None or not self.channel_requests:
            return
        self.check('on-demand popup opens immediately without synthesizing alpha',
                   popup.isVisible() and popup.status.isVisible() and not popup.previews and len(self.channel_requests) == 1)
        parent, complete = self.channel_requests[-1]
        self.check('channel callback receives exact event resource and subresource identity',
                   parent['eventId'] == 4 and parent['resourceId'] == '53' and
                   parent['displayMapping']['attachmentIndex'] == '3')
        complete(self.channel_response(parent, ['a', 'b', 'g', 'r']))
        self.phase = 'check requested channels'

    def check_requested_channels(self):
        popup = self.panel.view._channel_popup
        if any(card.pixmap is None for card in popup.previews):
            return
        self.check('asynchronous channel receipt replaces loading with all verified RGBA views',
                   [card.preview['channel'] for card in popup.previews] == ['r', 'g', 'b', 'a'] and
                   not popup.status.isVisible())
        popup.grab().save(str(self.root / 'channels-requested-popup.png'))
        popup.close()
        self.panel.view._open_channels('stage-0', 'channels:3')
        self.check('reopening the same exact texture uses cached channel receipts', len(self.channel_requests) == 1)
        self.panel.view._open_channels('stage-0', 'channels:5')
        parent, complete = self.channel_requests[-1]
        complete(self.channel_response(parent, ['r']))
        self.check('a one-channel receipt is shown alone without invented G B or alpha',
                   len(self.panel.view._channel_popup.previews) == 1 and
                   self.panel.view._channel_popup.previews[0].preview['channel'] == 'r')
        self.panel.view._open_channels('stage-0', 'channels:4')
        parent, complete = self.channel_requests[-1]
        response = self.channel_response(parent, ['a'])
        response['images'][0]['resourceId'] = 'wrong-resource'
        complete(response)
        self.check('wrong-resource channel receipt is rejected without replacing the diagram',
                   not self.panel.view._channel_popup.previews and self.panel.view._channel_popup.status.isVisible())
        self.panel.view._open_channels('stage-0', 'channels:4')
        parent, stale_complete = self.channel_requests[-1]
        self.panel.view.channel_request_callback = None
        self.panel.set_document(self.panel.document)
        stale_complete(self.channel_response(parent, ['a']))
        self.check('old channel completion cannot reopen a popup after document replacement',
                   self.panel.view._channel_popup is None and not self.panel.view._channel_cache)
        self.saved_offline_navigations = list(self.navigations)
        self.panel.document['stages'][0]['images'][0]['channelCacheComplete'] = False
        def unavailable(_parent, _complete):
            raise RuntimeError('Fixture replay is unavailable')
        self.panel.view.channel_request_callback = unavailable
        # set_document closes the old Qt.Popup with deleteLater. Let its native
        # grab/outside-click events drain before showing another popup; otherwise
        # a queued click from the previous synthetic interaction can dismiss it.
        self.phase = 'open saved offline channels'

    def open_saved_offline_channels(self):
        self.panel.view._open_channels('stage-0', 'channels:0')
        self.offline_popup_started = time.monotonic()
        self.phase = 'check saved offline channels'

    def check_saved_offline_channels(self):
        popup = self.panel.view._channel_popup
        self.result['offlinePopupDebug'] = {
            'present': popup is not None,
            'alive': popup._alive if popup is not None else False,
            'visible': popup.isVisible() if popup is not None else False,
            'status': popup.status.text() if popup is not None else '',
            'pending': len(popup._pending) if popup is not None else 0,
            'channels': [{'channel': card.preview['channel'], 'path': card.preview['path'],
                          'exists': Path(card.preview['path']).is_file(),
                          'loaded': card.pixmap is not None, 'failed': card.failed}
                         for card in popup.previews] if popup is not None else [],
        }
        if popup is not None and (not popup._alive or not popup.isVisible()):
            raise RuntimeError('Saved channel popup closed before offline image loading; see offlinePopupDebug')
        if time.monotonic() - self.offline_popup_started > 15:
            raise RuntimeError('Saved channel image loading exceeded 15 seconds; see offlinePopupDebug')
        if popup is None or any(card.pixmap is None for card in popup.previews):
            return
        self.check('failed live refresh preserves all saved RGBA images in the open popup',
                   popup.isVisible() and [card.preview['channel'] for card in popup.previews] == list('rgba') and
                   popup._saved_previews and popup.status.isVisible() and not self.panel.view._channel_cache)
        self.check('viewing saved channel files never changes replay selection', self.navigations == self.saved_offline_navigations)
        popup.grab().save(str(self.root / 'channels-saved-offline-popup.png'))
        def unavailable_revision():
            raise RuntimeError('Fixture capture is restoring')
        self.panel.view.channel_revision_provider = unavailable_revision
        self.panel.view._open_channels('stage-0', 'channels:0')
        popup = self.panel.view._channel_popup
        self.check('unavailable revision provider cannot prevent saved popup opening',
                   popup.isVisible() and len(popup.previews) == 4 and popup._saved_previews)
        self.panel.view.channel_revision_provider = None
        self.panel.view.channel_request_callback = None
        self.panel.view._close_channel_popup()
        self.phase = 'open detail after offline'

    def open_detail_after_offline(self):
        item = self.panel.view._stages['stage-0']
        self.click(self.item_point(item.group_toggles['details'], item.group_toggles['details'].boundingRect().center()))
        self.phase = 'check detail gallery'

    def check_detail_gallery(self):
        item = self.panel.view._stages['stage-0']
        details = [card for card in item.previews if isinstance(card, self.module._PreviewItem)
                   and card.preview.get('presentation') == 'detail' and card.preview.get('group') != 'channels']
        self.check('one folded gallery reveals auxiliary outputs and inputs without losing records',
                   len(details) == 5 and any(card.preview['group'] == 'inputs' for card in details))
        related = [card for card in item.previews if isinstance(card, self.module._PreviewItem)
                   and card.preview['resourceId'] == '52' and card.preview['group'] == 'outputs']
        self.check('same resource at a different EID or exposure remains a separate card',
                   len(related) == 3 and {card.preview['eventId'] for card in related} == {3, 4} and
                   {card.preview['displayMapping']['whitePoint'] for card in related} == {'0.75', '2.0'})
        self.check('exact duplicate record is not rendered twice', len(item.previews) == 11)
        exposure = next(card for card in related if card.preview['displayMapping']['whitePoint'] == '2.0')
        self.click(self.item_point(exposure.title, QtCore.QPointF(28, 8)))
        self.check('detail title navigates with its own display range', self.targets[-1]['eventId'] == 3 and
                   self.targets[-1]['displayMapping']['whitePoint'] == 2.0)
        self.click(self.item_point(item.group_toggles['details'], item.group_toggles['details'].boundingRect().center()))
        self.phase = 'check collapsed'

    def check_typography(self, item, preview, dark):
        def contrast(color, background):
            def luminance(value):
                channels = [value.redF(), value.greenF(), value.blueF()]
                linear = [part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4
                          for part in channels]
                return sum(weight * part for weight, part in zip((0.2126, 0.7152, 0.0722), linear))
            first, second = sorted((luminance(color), luminance(background)))
            return (second + 0.05) / (first + 0.05)
        for label, title, body, metadata in [('stage', item.title, item.summary, item.metadata),
                                            ('texture', preview.title, preview.caption, preview.metadata)]:
            colors = [text.defaultTextColor() for text in (title, body, metadata)]
            tones = [color.lightnessF() for color in colors]
            fonts = [text.font().pointSizeF() for text in (title, body, metadata)]
            self.check(('%s %s text has title/body/metadata hierarchy' % ('dark' if dark else 'light', label)),
                       fonts[0] > fonts[1] > fonts[2] and
                       (tones[0] > tones[1] > tones[2] if dark else tones[0] < tones[1] < tones[2]) and
                       all(contrast(color, item.palette.color(QtGui.QPalette.Base)) >= 4.5 for color in colors))
            self.check(('%s %s title uses neutral text and explicit link affordance' % ('dark' if dark else 'light', label)),
                       title.font().underline() and '↗' not in title.toPlainText() and
                       max(colors[0].red(), colors[0].green(), colors[0].blue()) -
                       min(colors[0].red(), colors[0].green(), colors[0].blue()) <= 5 and
                       (tones[0] > 0.9 if dark else tones[0] < 0.1))

    def check_collapsed(self):
        self.check('closing popups and auxiliary gallery leaves the compact main diagram', not self.panel.view._expanded_groups and
                   len(self.panel.view._stages['stage-0'].previews) == 6)
        light = QtGui.QPalette(self.original_palette)
        light.setColor(QtGui.QPalette.Window, QtGui.QColor('#f0f1f3'))
        light.setColor(QtGui.QPalette.Base, QtGui.QColor('#ffffff'))
        light.setColor(QtGui.QPalette.Text, QtGui.QColor('#242932'))
        light.setColor(QtGui.QPalette.WindowText, QtGui.QColor('#242932'))
        light.setColor(QtGui.QPalette.Highlight, QtGui.QColor('#346ea0'))
        self.panel.top.setPalette(light)
        self.check('palette change updates canvas', self.panel.view.backgroundBrush().color() == light.color(QtGui.QPalette.Window))
        self.check('directory inherits the light theme without fixed dark colors',
                   self.panel.outline.palette().color(QtGui.QPalette.Text) == light.color(QtGui.QPalette.Text) and
                   self.panel.outline.palette().color(QtGui.QPalette.Window) == light.color(QtGui.QPalette.Window))
        item = self.panel.view._stages['stage-0']
        self.check_typography(item, next(preview for preview in item.previews
            if isinstance(preview, self.module._PreviewItem)), dark=False)
        self.panel.view.zoom(0.48)
        item = self.panel.view._stages['stage-0']
        self.panel.view.centerOn(item)
        self.phase = 'capture light'
        return True

    def save_full_stage(self, name):
        item = self.panel.view._stages['stage-0']
        if any(preview.pixmap is None for preview in item.previews):
            return False
        self.panel.top.grab().save(str(self.root / (name + '-panel.png')))
        source = item.sceneBoundingRect().adjusted(-10, -10, 10, 110)
        image = QtGui.QImage(int(source.width()), int(source.height()), QtGui.QImage.Format_RGB32)
        image.fill(self.panel.view.palette().color(QtGui.QPalette.Window))
        painter = QtGui.QPainter(image)
        painter.setRenderHints(self.panel.view.renderHints())
        self.panel.view.scene().render(painter, QtCore.QRectF(image.rect()), source)
        painter.end()
        image.save(str(self.root / (name + '.png')))
        return True

    def tick(self):
        now = time.monotonic()
        if self.last_tick is not None:
            self.gaps.append((now - self.last_tick) * 1000)
            if self.phase in ('export diagram', 'export split diagram') and getattr(self, 'previous_tick_phase', None) == self.phase:
                self.export_gaps.append((now - self.last_tick) * 1000)
        self.last_tick = now
        self.previous_tick_phase = self.phase
        try:
            if now - self.started > 120:
                raise RuntimeError('Timed out in ' + self.phase)
            if self.phase == 'starting':
                self.prepare()
            elif self.phase == 'wait previews':
                self.check_directory()
            elif self.phase == 'check directory scroll':
                self.check_directory_scroll()
            elif self.phase == 'check initial previews':
                self.finish_checks()
            elif self.phase == 'check expanded':
                self.check_expanded()
            elif self.phase == 'check RGB channels':
                self.check_rgb_channels()
            elif self.phase == 'open RGB channels':
                item = self.panel.view._stages['stage-0']
                button = item.group_toggles['channels:2']
                self.click(self.item_point(button, button.boundingRect().center()))
                self.phase = 'check RGB channels'
            elif self.phase == 'check channel request':
                self.check_channel_request()
            elif self.phase == 'check requested channels':
                self.check_requested_channels()
            elif self.phase == 'open saved offline channels':
                self.open_saved_offline_channels()
            elif self.phase == 'check saved offline channels':
                self.check_saved_offline_channels()
            elif self.phase == 'open detail after offline':
                self.open_detail_after_offline()
            elif self.phase == 'check detail gallery':
                self.check_detail_gallery()
            elif self.phase == 'check collapsed':
                self.check_collapsed()
            elif self.phase == 'capture light':
                if not self.save_full_stage('light'):
                    return
                self.panel.top.setPalette(self.original_palette)
                self.phase = 'capture dark'
            elif self.phase == 'capture dark':
                if not self.save_full_stage('dark'):
                    return
                document = self.panel.document
                # The positive sequence needs distinct intermediate evidence.
                # Repeating the same {before, after} set in both media is now
                # correctly treated as a redundant chain by the presenter.
                document['stages'][0]['animations'][0]['imageIndices'] = [1, 2]
                document['stages'][0]['comparisons'][0]['rightImageIndex'] = 2
                document['stages'][0]['processSteps'] = [
                    {'label': '输入颜色', 'imageIndex': 0},
                    {'label': '逐级滤波', 'animationIndex': 0},
                    {'label': '最终合成', 'comparisonIndex': 0}]
                document['stages'][1]['images'][1]['eventId'] = 8
                document['stages'][1]['processGraph'] = {
                    'nodes': [{'id': 'input', 'label': '输入图像', 'imageIndex': 0},
                              {'id': 'blur', 'label': '空间滤波', 'eventId': 6},
                              {'id': 'fft', 'label': '频域滤波', 'eventId': 7},
                              {'id': 'combine', 'label': '合成图像', 'imageIndex': 1}],
                    'edges': [{'from': 'input', 'to': 'blur', 'label': '颜色'},
                              {'from': 'input', 'to': 'fft', 'label': '颜色'},
                              {'from': 'blur', 'to': 'combine'},
                              {'from': 'fft', 'to': 'combine'}]}
                document['stages'][2]['processGraph'] = {
                    'nodes': [{'id': 'prepare', 'label': '准备参数', 'eventId': 9},
                              {'id': 'calculate', 'label': '计算结果', 'eventId': 12}],
                    'edges': [{'from': 'prepare', 'to': 'calculate'}]}
                document['stages'][2]['visualReview'] = {'graphPresentation': 'omit'}
                single_draw = document['stages'][3]
                for preview in single_draw['images']:
                    preview['eventId'] = 14
                    preview['semantic'] = 'interpreted-resource-content'
                single_draw['processGraph'] = {
                    'nodes': [{'id': 'first', 'label': '单次绘制的颜色输出', 'imageIndex': 0},
                              {'id': 'second', 'label': '单次绘制的另一输出', 'imageIndex': 1}],
                    'edges': [{'from': 'first', 'to': 'second'}]}
                single_draw['processSteps'] = [{'label': '旧第一步', 'imageIndex': 0},
                                                {'label': '旧第二步', 'imageIndex': 1}]
                self.panel.view._expanded_groups.add(('stage-0', 'details'))
                self.panel.set_document(document)
                stage = self.panel.view._stages['stage-0']
                self.check('distinct process media form a left-to-right sequence', len(getattr(stage, 'process_items', [])) == 3 and
                    stage.process_items[0].x() < stage.process_items[1].x() < stage.process_items[2].x())
                self.check('process referenced output is not duplicated', sum(
                    isinstance(item, self.module._PreviewItem) and item.preview['resourceId'] == '50' and
                    item.preview.get('group') == 'outputs'
                    for item in stage.previews) == 1)
                self.check('process image keeps channel popup access without inline channel cards',
                    'channels:0' in stage.group_toggles and
                    not any(card.preview['group'] == 'channels' for card in stage.previews))
                graph_stage = self.panel.view._stages['stage-1']
                omitted_graph_stage = self.panel.view._stages['stage-2']
                self.check('omitting a redundant text graph retains its module and previews',
                    not getattr(omitted_graph_stage, 'graph_nodes', {}) and len(omitted_graph_stage.previews) == 2 and
                    self.panel.document['stages'][2]['processGraph']['nodes'][0]['id'] == 'prepare')
                single_draw_item = self.panel.view._stages['stage-3']
                self.check('single draw MRT displays independent result cards without graph or legacy arrows',
                    not getattr(single_draw_item, 'graph_nodes', {}) and not getattr(single_draw_item, 'process_items', []) and
                    len(single_draw_item.previews) == 2 and single_draw_item.previews[0].y() == single_draw_item.previews[1].y())
                self.check('internal semantic tags stay out of visible captions and tooltips',
                    all('interpreted-resource-content' not in card.caption.toPlainText() and
                        'interpreted-resource-content' not in card.toolTip() for card in single_draw_item.previews))
                graph_nodes = graph_stage.graph_nodes
                self.check('data-flow graph keeps branch and composite endpoints', len(graph_stage.graph_edges) == 4 and
                    graph_nodes['input'].x() < graph_nodes['blur'].x() < graph_nodes['combine'].x() and
                    graph_nodes['blur'].x() == graph_nodes['fft'].x() and
                    graph_nodes['blur'].y() < graph_nodes['fft'].y())
                self.check('data-flow media is not repeated in the main gallery', len(graph_stage.previews) == 2)
                self.panel.view.focus_stage('stage-0')
                self.panel.view.fit_width()
                self.panel.top.grab().save(str(self.root / 'process-panel.png'))
                self.export_navigations = list(self.navigations)
                self.export_scroll = self.panel.view.verticalScrollBar().value()
                self.panel.export_image(str(self.root / 'full-diagram.png'))
                self.phase = 'export diagram'
            elif self.phase == 'export diagram':
                job = self.panel._export_job
                if job is not None:
                    if job.phase != 'layout' and not hasattr(self, 'export_sample'):
                        last = job.previews[-1]
                        point = last.sceneBoundingRect().center()
                        if hasattr(last, 'image_rect'):
                            point = last.mapToScene(last.image_rect.center())
                        self.export_sample = (int(point.x() + 20 - 65), int(point.y()))
                        comparison = next(item for item in job.scene.items() if isinstance(item, self.module._ComparisonItem))
                        point = comparison.mapToScene(comparison.image_rect.center())
                        self.compare_sample = (int(point.x() + 20 + 45), int(point.y() - 12))
                        stage = next(item for item in job.scene.items() if isinstance(item, self.module._StageItem)
                                     and item.stage['id'] == 'stage-0')
                        self.check('PNG export omits removed calculation UI and independent channel popups',
                                   'algorithm' not in stage.group_toggles and
                                   not hasattr(stage, 'algorithm_step_links') and
                                   not any(card.preview['group'] == 'channels' for card in stage.previews))
                        graph_stage = next(item for item in job.scene.items() if isinstance(item, self.module._StageItem)
                                           and item.stage['id'] == 'stage-1')
                        self.check('PNG export preserves all branch and composition lines', len(graph_stage.graph_edges) == 4)
                        detail_cards = [card for card in stage.previews if isinstance(card, self.module._PreviewItem)
                            and card.preview.get('presentation') == 'detail' and card.preview['group'] != 'channels']
                        self.check('PNG export includes expanded auxiliary gallery and explicit detail-tagged process source',
                                   len(detail_cards) == 6)
                    return
                state = self.panel.export_state
                self.result['export'] = state
                self.check('full diagram export succeeds', state['ok'] and state['stageCount'] == 32)
                self.check('normal-sized full diagram uses one PNG', len(state['paths']) == 1)
                image = QtGui.QImage(state['paths'][0])
                self.check('export includes full height beyond viewport', not image.isNull() and
                           image.height() == state['height'] and image.height() > 10000)
                sample = image.pixelColor(*self.export_sample)
                self.check('offscreen last-stage preview is decoded for export', sample.alpha() == 255 and
                           max(sample.red(), sample.green(), sample.blue()) > 100)
                sample = image.pixelColor(*self.compare_sample)
                self.check('comparison image survives export strip clipping',
                           max(sample.red(), sample.green(), sample.blue()) > 100 and
                           max(sample.red(), sample.green(), sample.blue()) - min(sample.red(), sample.green(), sample.blue()) > 30)
                self.check('export preserves navigation and reading position', self.navigations == self.export_navigations and
                           abs(self.panel.view.verticalScrollBar().value() - self.export_scroll) <= 2)
                self.check('saved-image notice is transient', self.panel.export_status.isVisible() and
                           self.panel._export_message_timer.isActive())
                self.notice_receipt = dict(self.panel.export_state)
                self.phase = 'wait export notice'
            elif self.phase == 'wait export notice':
                if self.panel.export_status.isVisible():
                    return
                self.check('saved-image notice expires without losing export receipt',
                           not self.panel.export_status.text() and self.panel.export_state == self.notice_receipt)
                self.module.MAX_EXPORT_PART_PIXELS = 1120 * 4000
                self.panel.export_image(str(self.root / 'split-diagram.png'))
                self.phase = 'export split diagram'
            elif self.phase == 'export split diagram':
                if self.panel._export_job is not None:
                    return
                state = self.panel.export_state
                self.result['splitExport'] = state
                self.check('long diagram splits with all paths reported', state['ok'] and len(state['paths']) > 1 and
                           all(Path(path).is_file() for path in state['paths']))
                self.check('split images cover every row without truncation',
                           sum(QtGui.QImage(path).height() for path in state['paths']) == state['height'])
                switched_document = dict(self.panel.document)
                self.panel.export_image(str(self.root / 'obsolete-diagram.png'))
                self.binding = dict(self.binding, captureSessionId='smoke-b',
                                    capturePath=str(self.root / 'second.rdc'))
                before = list(self.navigations)
                self.check('old capture cannot navigate identical EIDs', self.panel._navigate(2) is False)
                self.check('stale diagram is cleared', self.panel.get_state()['stageCount'] == 0)
                self.check('capture switch clears export receipt and saved-image notice',
                           not self.panel.export_state and not self.panel.export_status.isVisible() and
                           not self.panel.export_status.text() and not self.panel._export_message_timer.isActive())
                self.check('capture change does not replay', self.navigations == before)
                switched_document.update(self.binding)
                self.panel.set_document(switched_document)
                self.check('pending old export is cancelled before enabling another export',
                           self.panel._export_job.cancel_requested and not self.panel.save_image.isEnabled())
                self.phase = 'capture export cancellation'
            elif self.phase == 'capture export cancellation':
                if self.panel._export_job is not None:
                    return
                self.check('cancelled old export cannot overwrite the new capture UI',
                           not self.panel.export_state and not self.panel.export_status.isVisible() and
                           not self.panel.export_status.text() and self.panel.save_image.isEnabled() and
                           self.panel.document['captureSessionId'] == 'smoke-b')
                self.panel.shutdown()
                self.check('image workers stopped at panel shutdown', not self.panel.view._alive)
                self.result.update(ok=True, elapsedSeconds=now-self.started, maxTimerGapMs=max(self.gaps or [0]),
                                   maxExportTimerGapMs=max(self.export_gaps or [0]))
                self.phase = 'complete'
                self.timer.stop()
                QtCore.QTimer.singleShot(100, self.window.close)
                QtCore.QTimer.singleShot(150, QtWidgets.QApplication.instance().quit)
            self.save()
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.timer.stop()
            self.save()
            if hasattr(self, 'window'):
                QtCore.QTimer.singleShot(100, self.window.close)
                QtCore.QTimer.singleShot(150, QtWidgets.QApplication.instance().quit)


runner = FrameFlowSmoke(pyrenderdoc)
sys.modules['__main__']._bread_frame_flow_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
