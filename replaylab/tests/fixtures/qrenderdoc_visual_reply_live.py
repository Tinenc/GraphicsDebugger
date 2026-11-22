"""Real model -> Host snapshot -> native chat visual acceptance in an owned process.

The shared launcher provides an isolated configuration/desktop and audited API relay.
"""
from pathlib import Path
import os

fixture=Path(os.environ['RD_VISUAL_FIXTURE_ROOT'])/'qrenderdoc_rt_annotation_live.py'
source=fixture.read_text(encoding='utf-8')
exec(compile(source[:source.rindex('runner = LiveAnnotation()')],str(fixture),'exec'),globals())


class VisualReplyLive(LiveAnnotation):
    def tick(self):
        if self.phase not in ('links','visual-check','visual-persist','visual-no-capture'):
            return super(VisualReplyLive,self).tick()
        try:
            if self.phase=='links':
                if time.monotonic()<self.ready_at:return
                replies=self.window.findChildren(QtWidgets.QWidget,'aiVisualReply')
                self.keep.extend(replies)
                if not replies:
                    if time.monotonic()>self.ready_at+20:raise RuntimeError('Completed real-model answer has no native visual widget')
                    return
                self.reply=next((w for w in replies if w.isVisible()),replies[-1]);self.keep.append(self.reply)
                self.selector=self.reply.findChild(QtWidgets.QComboBox)
                self.keep.append(self.selector)
                state=Path(self.spec['eventsRoot'])/(self.result['cases'][-1]['taskId']+'.report-state.json')
                refs=json.loads(state.read_text(encoding='utf-8')).get('replyVisuals',[])
                if not refs:raise RuntimeError('No Host-published visual receipt')
                self.identity=refs[-1]['id']
                manifest=Path(os.environ['LOCALAPPDATA'])/'ReplayLab/ai-visuals'/(self.identity+'.json')
                self.manifest=json.loads(manifest.read_text(encoding='utf-8'))
                self.types=[c['type']for c in self.manifest['cards']]
                if set(self.types)!={'image','compare','mesh','diagram'}:raise RuntimeError('Model did not publish all requested visual types: '+str(self.types))
                self.result['visualManifest']=str(manifest)
                self.result['modelPublishedTypes']=self.types
                self.phase='visual-check';self.save();return
            if self.phase=='visual-check':
                checks={}
                def activate(kind):
                    index=self.types.index(kind);self.selector.setCurrentIndex(index)
                    self.app.processEvents()
                    stack=self.reply.findChild(QtWidgets.QStackedWidget)
                    self.keep.append(stack)
                    page=stack.currentWidget();self.keep.append(page);return page
                page=activate('image')
                self.reply.grab().save(str(self.root/'visual-marked-rt.png'))
                checks['capturedImageAndMarks']=bool(self.manifest['cards'][self.types.index('image')]['annotations'])
                checks['visibleInChat']=self.reply.isVisible() and self.reply.height()>200
                page=activate('compare');canvas=page.findChild(QtWidgets.QWidget,'aiVisualImageCanvas');self.keep.append(canvas)
                buttons=page.findChildren(QtWidgets.QToolButton);self.keep.extend(buttons)
                def click(source):next(b for b in buttons if b.text()==self.translated('AIVisualReply',source)).click();self.app.processEvents()
                click('Before');before=canvas.grab().toImage();click('After');after=canvas.grab().toImage()
                checks['differentCapturedStages']=before!=after
                click('Wipe');divider=page.findChild(QtWidgets.QSlider,'aiVisualDivider');self.keep.append(divider)
                divider.setValue(20);left=canvas.grab().toImage();divider.setValue(80);right=canvas.grab().toImage()
                checks['nativeWipeChangesPixels']=left!=right and canvas.property('visualWipeRatio')==.8
                click('Side by side');checks['sideBySideWorks']=canvas.property('visualCompareMode')==3
                self.reply.grab().save(str(self.root/'visual-comparison.png'))
                page=activate('mesh');mesh=page.findChild(QtWidgets.QWidget,'aiVisualMeshCanvas');self.keep.append(mesh)
                checks['capturedTrianglesRendered']=int(mesh.property('visualMeshTriangleCount'))>0
                before=mesh.grab().toImage()
                events=[(QtCore.QEvent.MouseButtonPress,QtCore.QPointF(80,80)),(QtCore.QEvent.MouseMove,QtCore.QPointF(150,110)),
                        (QtCore.QEvent.MouseButtonRelease,QtCore.QPointF(150,110))]
                for kind,point in events:self.app.sendEvent(mesh,QtGui.QMouseEvent(kind,point,QtCore.Qt.LeftButton,QtCore.Qt.LeftButton,QtCore.Qt.NoModifier))
                checks['orbitChangesRender']=mesh.property('visualMeshYaw')!=0 and mesh.grab().toImage()!=before
                env=page.findChild(QtWidgets.QSlider,'aiVisualEnvironmentStrength');self.keep.append(env)
                env.setValue(0);before=mesh.grab().toImage();env.setValue(150)
                checks['materialControlChangesRender']=mesh.grab().toImage()!=before
                self.reply.grab().save(str(self.root/'visual-captured-mesh.png'))
                # Verify capture-bound native navigation using a model-created card link.
                links=page.findChildren(QtWidgets.QPushButton);self.keep.extend(links)
                eid=int(self.manifest['cards'][self.types.index('mesh')]['eventId'])
                self.jump=next(b for b in links if b.text()=='EID '+str(eid));self.keep.append(self.jump)
                self.jump.click();checks['eventLinkReallyNavigates']=int(pyrenderdoc.CurEvent())==eid
                page=activate('diagram');self.reply.grab().save(str(self.root/'visual-diagram.png'))
                self.result['checks']=checks
                if not all(checks.values()):raise RuntimeError('Native visual failure: '+str([k for k,v in checks.items()if not v]))
                self.window.grab().save(str(self.root/'visual-answer-chat.png'))
                self.ready_at=time.monotonic()+2;self.phase='visual-persist';self.save();return
            if self.phase=='visual-persist':
                if time.monotonic()<self.ready_at:return
                stores=list((self.root/'config/ReplayLab/conversations').glob('*.json'))
                if len(stores)!=1:raise RuntimeError('Missing conversation store')
                wrapper=json.loads(stores[0].read_text(encoding='utf-8'))
                payload=json.loads(base64.b64decode(wrapper['payload']))
                messages=[m for t in payload['conversations']for m in t['messages']if m.get('role')=='assistant']
                refs=[v['id']for m in messages for v in m.get('visuals',[])]
                if self.identity not in refs:raise RuntimeError('Native visual references were not persisted')
                self.result['checks']['conversationPersistsVisualReference']=True
                self.result['conversationStore']=str(stores[0])
                self.selector.setCurrentIndex(self.types.index('mesh'));self.app.processEvents()
                pyrenderdoc.CloseCapture();self.jump.click()
                self.result['checks']['noCaptureNavigationBlocked']=not pyrenderdoc.IsCaptureLoaded()
                self.ready_at=time.monotonic()+.5;self.phase='visual-no-capture';self.save();return
            if self.phase=='visual-no-capture':
                if time.monotonic()<self.ready_at:return
                # Bread switches to the no-capture history scope on closure. Assets
                # remain in the original capture's conversation, checked after restart.
                self.result['captureClosureSwitchesHistoryScope']=not bool(self.window.findChildren(QtWidgets.QWidget,'aiVisualReply'))
                self.finish()
        except Exception:self.finish(traceback.format_exc())


runner=VisualReplyLive()
sys.modules['__main__']._bread_visual_reply_live=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
