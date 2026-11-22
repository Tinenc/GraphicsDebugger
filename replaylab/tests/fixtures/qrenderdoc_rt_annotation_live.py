"""Actual brush -> attachment -> model request acceptance in an owned test process."""
import json
import base64
import os
from pathlib import Path
import sys
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtGui, QtWidgets


class LiveAnnotation(object):
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.root = Path(os.environ['RD_ANNOTATION_RUN'])
        self.spec = json.loads((self.root / 'spec.json').read_text(encoding='utf-8'))
        self.phase = 'open'
        self.index = 0
        self.started = time.monotonic()
        self.keep = []
        self.result = {'schema': 'bread-rt-annotation-live/v1', 'ok': False, 'cases': [], 'pid': os.getpid()}
        self.known_tasks = set()

    def save(self):
        self.result['phase'] = self.phase
        (self.root / 'ui-result.json').write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def translated(self, context, source):
        return QtCore.QCoreApplication.translate(context, source)

    def button(self, source):
        buttons = self.panel.findChildren(QtWidgets.QAbstractButton)
        self.keep.extend(buttons)
        return next(b for b in buttons if b.text() == self.translated('AITextureAnnotation', source))

    def drag(self, points):
        left, top, width, height = self.canvas.property('annotationImageRect')
        events=[(QtCore.QEvent.MouseButtonPress,points[0])]
        events.extend((QtCore.QEvent.MouseMove,point) for point in points[1:])
        events.append((QtCore.QEvent.MouseButtonRelease,points[-1]))
        for kind, uv in events:
            point = QtCore.QPointF(left+uv[0]*width, top+uv[1]*height)
            event = QtGui.QMouseEvent(kind, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
            self.app.sendEvent(self.canvas, event)

    def task_events(self):
        root = self.root / 'local/ReplayLab/ai/task-events'
        # The concrete path is obtained from the same exported runtime helper in the launcher.
        root = Path(self.spec['eventsRoot'])
        return list(root.glob('*.jsonl')) if root.is_dir() else []

    def tick(self):
        try:
            if time.monotonic() - self.started > self.spec.get('timeout', 900):
                raise RuntimeError('Live validation timed out at ' + self.phase)
            if self.phase == 'open':
                bridge = sys.modules.get('renderdoc_mcp_bridge')
                if bridge is None: return
                if pyrenderdoc.IsCaptureLoaded(): raise RuntimeError('Expected owned empty test process')
                bridge._cancel_project_auto_activation()
                bridge._schedule_project_auto_activation = lambda: None
                pyrenderdoc.LoadCapture(self.spec['capture'], rd.ReplayOptions(), self.spec['capture'], False, True)
                self.phase = 'assistant'; self.save(); return
            if self.phase == 'assistant':
                if not pyrenderdoc.IsCaptureLoaded(): return
                self.window = pyrenderdoc.GetMainWindow().Widget()
                self.keep.append(self.window)
                self.window.findChild(QtWidgets.QAction, 'action_AI_Assistant').trigger()
                pyrenderdoc.ShowTextureViewer()
                self.viewer = pyrenderdoc.GetTextureViewer()
                self.keep.append(self.viewer)
                self.phase = 'prepare'; self.save(); return
            if self.phase == 'prepare':
                combos = self.window.findChildren(QtWidgets.QComboBox)
                self.keep.extend(combos)
                profile = next((c for c in combos if any(str(c.itemData(i)) == self.spec['profile']
                                                        for i in range(c.count()))), None)
                if profile is None: return
                buttons = self.window.findChildren(QtWidgets.QPushButton)
                self.keep.extend(buttons)
                new = next(b for b in buttons if b.accessibleName() == self.translated('AIAssistant', 'New conversation'))
                new.click()
                profile.setCurrentIndex(next(i for i in range(profile.count()) if str(profile.itemData(i)) == self.spec['profile']))
                case=self.spec['cases'][self.index]
                self.draft_edit_done = False
                self.annotation_eid=int(case.get('eventId',self.spec.get('eventId',37)))
                rid=str(case.get('resourceId',self.spec.get('resourceId',54)))
                pyrenderdoc.SetEventID([], self.annotation_eid, self.annotation_eid, True)
                if case.get('noAnnotation'):
                    self.phase = 'send'; self.save(); return
                texture=next(t for t in pyrenderdoc.GetTextures() if str(t.resourceId) in (rid,'ResourceId::'+rid))
                self.texture_size=[int(texture.width),int(texture.height)]
                self.viewer.ViewTexture(texture.resourceId, rd.CompType.Typeless, True)
                self.ready_at = time.monotonic()+1
                self.phase = 'brush'; self.save(); return
            if self.phase == 'brush':
                if time.monotonic() < self.ready_at: return
                self.brush = self.window.findChild(QtWidgets.QPushButton, 'aiAnnotateRenderTarget')
                self.keep.append(self.brush)
                self.brush.click(); self.phase = 'canvas'; self.save(); return
            if self.phase == 'canvas':
                self.panel = self.window.findChild(QtWidgets.QWidget, 'aiTextureAnnotation')
                if self.panel is None: return
                self.canvas = self.panel.findChild(QtWidgets.QWidget, 'aiAnnotationCanvas')
                self.keep.extend([self.panel, self.canvas])
                case = self.spec['cases'][self.index]
                if case.get('verifyAnnotationResolution'):
                    native = self.canvas.property('annotationSourceSize')
                    if (native.width(), native.height()) != tuple(self.texture_size):
                        raise RuntimeError('Annotation editor did not use native RT pixels')
                for mark in case['marks']:
                    if mark.get('color'):
                        def choose_color(color=mark['color']):
                            dialog = self.app.activeModalWidget()
                            if not isinstance(dialog, QtWidgets.QColorDialog):
                                raise RuntimeError('Native mark color dialog did not open')
                            dialog.setCurrentColor(QtGui.QColor(color))
                            dialog.accept()
                        QtCore.QTimer.singleShot(50, choose_color)
                        self.button('Color').click()
                    if mark['tool'] == 'eraser' and mark.get('width'):
                        self.panel.findChild(QtWidgets.QSpinBox, 'aiAnnotationEraserWidth').setValue(mark['width'])
                    self.button({'ellipse':'Circle','arrow':'Arrow','pen':'Pen','eraser':'Eraser'}[mark['tool']]).click()
                    self.drag(mark['points'])
                if not self.button('Add to conversation').isEnabled(): raise RuntimeError('No mark was accepted')
                self.panel.grab().save(str(self.root / (case['id']+'-canvas.png')))
                switch=int(case.get('switchEventId',self.spec.get('switchEventId',22)))
                pyrenderdoc.SetEventID([], switch, switch, True)
                self.phase = 'attach'; self.save(); return
            if self.phase == 'attach':
                self.button('Add to conversation').click()
                self.phase = 'send'; self.save(); return
            if self.phase == 'send':
                chips = self.window.findChildren(QtWidgets.QFrame, 'aiAttachmentChip')
                self.keep.extend(chips)
                case = self.spec['cases'][self.index]
                expected_chips = 0 if case.get('noAnnotation') else 1
                if len(chips) != expected_chips: raise RuntimeError('Unexpected visible attachment count')
                if case.get('verifyDraftEdit') and not getattr(self, 'draft_edit_done', False):
                    previews = [b for b in chips[0].findChildren(QtWidgets.QToolButton)
                                if b.accessibleName().startswith(self.translated('AIAssistant', 'Preview attachment: %1').split('%1')[0])]
                    if len(previews) != 1: raise RuntimeError('Missing single annotation edit target')
                    self.keep.extend(previews)
                    previews[0].click()
                    self.panel = self.window.findChild(QtWidgets.QWidget, 'aiTextureAnnotation')
                    self.canvas = self.panel.findChild(QtWidgets.QWidget, 'aiAnnotationCanvas')
                    self.keep.extend([self.panel,self.canvas])
                    self.panel.grab().save(str(self.root/(case['id']+'-reopened.png')))
                    if not self.button('Update conversation marks').isEnabled():
                        raise RuntimeError('Reopened draft did not restore its marks')
                    self.button('Update conversation marks').click()
                    self.draft_edit_done = True
                    self.result['draftEditKeepsOneChip'] = True
                    return
                editors = [w for w in self.app.allWidgets() if '@skill:' in w.toolTip()]
                self.keep.extend(editors)
                if not editors or not editors[0].isEnabled(): return
                editor = editors[0]
                case = self.spec['cases'][self.index]
                if case.get('largeImageForPersistence'):
                    # A real inline raster > 2 MiB exercises the byte budget. Use a
                    # synthetic Qt drop, leaving the user's OS clipboard untouched.
                    data = os.urandom(1000*800*4)
                    image = QtGui.QImage(data,1000,800,4000,QtGui.QImage.Format_RGB32).copy()
                    mime = QtCore.QMimeData(); mime.setImageData(image)
                    target = editor.parentWidget()
                    enter = QtGui.QDragEnterEvent(QtCore.QPoint(10,10),QtCore.Qt.CopyAction,
                                                 mime,QtCore.Qt.LeftButton,QtCore.Qt.NoModifier)
                    self.app.sendEvent(target,enter)
                    drop = QtGui.QDropEvent(QtCore.QPointF(10,10),QtCore.Qt.CopyAction,
                                           mime,QtCore.Qt.LeftButton,QtCore.Qt.NoModifier)
                    self.app.sendEvent(target,drop)
                    self.keep.extend([mime,target])
                    if not drop.isAccepted():raise RuntimeError('Large raster drop was not accepted')
                    self.result['largeImageDropAccepted']=True
                self.known_tasks = set(str(p) for p in self.task_events())
                event = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_unknown, QtCore.Qt.NoModifier, case['prompt'])
                self.app.sendEvent(editor, event)
                if editor.property('plainText') != case['prompt']: raise RuntimeError('Prompt entry failed')
                self.app.sendEvent(editor, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return, QtCore.Qt.NoModifier))
                if editor.property('plainText'): raise RuntimeError('Send was not accepted')
                self.result['cases'].append({'id':case['id'], 'prompt':case['prompt'], 'annotationEID':self.annotation_eid,
                    'currentEIDAtSend':int(pyrenderdoc.CurEvent()), 'sent':True})
                self.phase = 'result'; self.save(); return
            if self.phase == 'result':
                new = [p for p in self.task_events() if str(p) not in self.known_tasks]
                if not new: return
                path = sorted(new, key=lambda p:p.stat().st_mtime)[0]
                events = []
                for line in path.read_text(encoding='utf-8').splitlines():
                    try: events.append(json.loads(line))
                    except ValueError: pass
                terminal = [e for e in events if e.get('kind') in ('completed','failed','cancelled','approval-required')]
                if not terminal: return
                case = self.result['cases'][-1]
                case.update(taskId=path.stem, events=str(path), terminal=terminal[-1])
                self.window.grab().save(str(self.root / (case['id']+'-answer.png')))
                if (terminal[-1]['kind'] != 'completed' or
                    terminal[-1].get('data', {}).get('terminalStatus') not in ('complete', 'completed')):
                    raise RuntimeError('Model task did not complete: '+terminal[-1]['kind'])
                self.ready_at = time.monotonic()+1
                self.phase = 'links'; self.save(); return
            if self.phase == 'links':
                if time.monotonic() < self.ready_at: return
                case = self.result['cases'][-1]
                browsers = self.window.findChildren(QtWidgets.QTextBrowser)
                self.keep.extend(browsers)
                links = []
                for browser in browsers:
                    block = browser.document().begin()
                    while block.isValid():
                        fragment = block.begin()
                        while not fragment.atEnd():
                            fmt = fragment.fragment().charFormat()
                            href = fmt.anchorHref()
                            url = QtCore.QUrl(href)
                            if url.scheme() == 'rdoc' and url.host() in ('event', 'eid'):
                                eid = int(url.path().strip('/'))
                                if eid not in [item['eventId'] for item in links]:
                                    browser.anchorClicked.emit(url)
                                    links.append({'url':href,'eventId':eid,
                                                  'observedEventId':int(pyrenderdoc.CurEvent())})
                            fragment += 1
                        block = block.next()
                case['eventLinks'] = links
                case['eventLinksVerified'] = bool(links) and all(x['eventId']==x['observedEventId'] for x in links)
                if (self.spec['cases'][self.index].get('verifyPersistence') or
                        self.spec['cases'][self.index].get('verifyPairPersistence') or
                        self.spec['cases'][self.index].get('verifyAnnotationResolution')):
                    stores=list((self.root/'config/ReplayLab/conversations').glob('*.json'))
                    if len(stores)!=1:raise RuntimeError('Expected one owned conversation store')
                    wrapper=json.loads(stores[0].read_text(encoding='utf-8'))
                    payload=base64.b64decode(wrapper['payload'])
                    saved=json.loads(payload)
                    matches=[m for t in saved['conversations'] for m in t['messages']
                             if m.get('text')==case['prompt']]
                    expected_count=3 if self.spec['cases'][self.index].get('verifyPersistence') else 2
                    if len(matches)!=1 or len(matches[0].get('attachments',[]))!=expected_count:
                        raise RuntimeError('Large inline image and annotation pair were omitted')
                    attachments=matches[0]['attachments']
                    if (self.spec['cases'][self.index].get('verifyPersistence') or
                            self.spec['cases'][self.index].get('verifyPairPersistence')) and (
                            sum(a['originalBytes'] for a in attachments)<2*1024*1024):
                        raise RuntimeError('Persistence regression did not exercise large data')
                    metadata=[{k:a.get(k) for k in ('id','mediaType','originalBytes','context')}
                              for a in attachments]
                    (self.root/'persisted-attachment-metadata.json').write_text(
                        json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
                    for attachment in attachments:
                        annotation=attachment.get('context',{}).get('annotation',{})
                        role=annotation.get('imageRole')
                        if role in ('original','annotated'):
                            data=base64.b64decode(attachment['content'])
                            (self.root/(role+'-sent.png')).write_bytes(data)
                            if self.spec['cases'][self.index].get('verifyAnnotationResolution'):
                                image=QtGui.QImage.fromData(data)
                                native=(annotation['textureWidth'],annotation['textureHeight'])
                                encoded=(annotation['imageWidth'],annotation['imageHeight'])
                                if (native != tuple(self.texture_size) or
                                        encoded != (image.width(),image.height()) or
                                        max(encoded) != min(1600,max(native)) or min(encoded)<1):
                                    raise RuntimeError('RT attachment dimensions violate the transport contract')
                    case['largeAttachmentsPersisted']={'count':expected_count,'payloadBytes':len(payload)}
                if self.spec['cases'][self.index].get('verifyHistoryEdit'):
                    references = []
                    for browser in browsers:
                        block = browser.document().begin()
                        while block.isValid():
                            fragment = block.begin()
                            while not fragment.atEnd():
                                href = fragment.fragment().charFormat().anchorHref()
                                if href.startswith('replaylab-annotation://edit/'):
                                    references.append((browser,href,fragment.fragment().position()))
                                fragment += 1
                            block = block.next()
                    unique = {href for _,href,_ in references}
                    if len(unique) != 1:
                        raise RuntimeError('Transcript must show only one editable marked image')
                    self.history_browser,self.history_href,position = references[0]
                    image_id = QtCore.QUrl(self.history_href).path().strip('/')
                    self.history_image_url = QtCore.QUrl('replaylab-attachment://'+image_id)
                    self.history_before = self.history_browser.document().resource(
                        QtGui.QTextDocument.ImageResource,self.history_image_url)
                    if self.spec['cases'][self.index].get('verifyHistoryContextMenu'):
                        cursor=QtGui.QTextCursor(self.history_browser.document())
                        cursor.setPosition(position)
                        self.history_browser.setTextCursor(cursor)
                        self.history_browser.ensureCursorVisible()
                        point=None
                        viewport=self.history_browser.viewport()
                        for y in range(0,viewport.height(),5):
                            for x in range(0,viewport.width(),5):
                                p=QtCore.QPoint(x,y)
                                if self.history_browser.anchorAt(p)==self.history_href:
                                    point=p;break
                            if point is not None:break
                        if point is None:raise RuntimeError('No hit target for the native marked image')
                        def choose_edit():
                            menu=self.app.activePopupWidget()
                            if not isinstance(menu,QtWidgets.QMenu):return
                            text=self.translated('AIConversationView','Edit marks in a new draft')
                            action=next((a for a in menu.actions() if a.text()==text),None)
                            menu.close()
                            if action is not None:
                                self.result['historyContextMenuActivated']=True
                                action.trigger()
                        QtCore.QTimer.singleShot(50,choose_edit)
                        self.history_browser.customContextMenuRequested.emit(point)
                        if not self.result.get('historyContextMenuActivated'):
                            raise RuntimeError('Native history context menu did not expose editing')
                    else:
                        self.history_browser.anchorClicked.emit(QtCore.QUrl(self.history_href))
                    self.phase='history-edit'; self.save(); return
                self.index += 1
                if self.index == len(self.spec['cases']): self.finish(); return
                self.phase = 'prepare'; self.save()
            if self.phase == 'history-edit':
                self.panel = self.window.findChild(QtWidgets.QWidget, 'aiTextureAnnotation')
                if self.panel is None:return
                self.canvas = self.panel.findChild(QtWidgets.QWidget, 'aiAnnotationCanvas')
                self.keep.extend([self.panel,self.canvas])
                if not self.button('Update conversation marks').isEnabled():
                    raise RuntimeError('History edit did not restore the original strokes')
                self.button('Arrow').click()
                self.drag([[.68,.3],[.6,.5]])
                self.button('Update conversation marks').click()
                self.phase='history-draft'; self.save(); return
            if self.phase == 'history-draft':
                chips=self.window.findChildren(QtWidgets.QFrame,'aiAttachmentChip')
                self.keep.extend(chips)
                if len(chips)!=1: raise RuntimeError('History edit did not create one draft chip')
                after=self.history_browser.document().resource(QtGui.QTextDocument.ImageResource,
                                                               self.history_image_url)
                if after != self.history_before:
                    raise RuntimeError('History edit changed the already sent image')
                self.result['cases'][-1]['historyEditCreatesNewDraftAndKeepsSentImage']=True
                self.window.grab().save(str(self.root/'history-edit-draft.png'))
                if self.spec['cases'][self.index].get('verifyReloadEdit'):
                    pyrenderdoc.CloseCapture()
                    pyrenderdoc.LoadCapture(self.spec['capture'],rd.ReplayOptions(),self.spec['capture'],False,True)
                    self.ready_at=time.monotonic()+2
                    self.phase='reload';self.save();return
                self.index+=1
                if self.index==len(self.spec['cases']): self.finish(); return
                self.phase='prepare';self.save()
            if self.phase=='reload':
                if not pyrenderdoc.IsCaptureLoaded() or time.monotonic()<self.ready_at:return
                self.history_browser.anchorClicked.emit(QtCore.QUrl(self.history_href))
                self.phase='reload-editor';self.save();return
            if self.phase=='reload-editor':
                self.panel=self.window.findChild(QtWidgets.QWidget,'aiTextureAnnotation')
                if self.panel is None:raise RuntimeError('Persisted annotation was not editable after reload')
                self.keep.append(self.panel)
                if not self.button('Update conversation marks').isEnabled():
                    raise RuntimeError('Reloaded annotation lost its strokes')
                self.button('Cancel').click()
                self.result['cases'][-1]['reloadRestoresEditableMarks']=True
                self.finish();return
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error=None):
        self.timer.stop()
        self.result.update(ok=error is None, error=error, elapsedSeconds=time.monotonic()-self.started)
        self.save()
        pyrenderdoc.CloseCapture()
        QtCore.QTimer.singleShot(100, self.app.closeAllWindows)
        QtCore.QTimer.singleShot(300, self.app.quit)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.tick)
        self.timer.start(); self.save()


runner = LiveAnnotation()
sys.modules['__main__']._bread_annotation_live = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
