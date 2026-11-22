"""Real model delivery: isolated flow, causal slider, exact code and CBuffer links."""
import os
from pathlib import Path
fixture=Path(os.environ['RD_VISUAL_FIXTURE_ROOT'])/'qrenderdoc_rt_annotation_live.py'
source=fixture.read_text(encoding='utf-8')
exec(compile(source[:source.rindex('runner = LiveAnnotation()')],str(fixture),'exec'),globals())
import hashlib,re


class EffectLive(LiveAnnotation):
    def __init__(self):
        super(EffectLive,self).__init__()
        self.visual_root=Path(os.environ['LOCALAPPDATA'])/'ReplayLab/ai-visuals'
        self.pointer=Path(self.spec['capture']+'.bread/frame-render-flow/current.json')
        self.pointer_before=hashlib.sha256(self.pointer.read_bytes()).hexdigest()
    def locate(self,identity):
        # Typed discovery precedes parentWidget to avoid generic PySide wrappers.
        browsers=self.window.findChildren(QtWidgets.QTextBrowser);self.keep.extend(browsers)
        for browser in browsers:
            block=browser.document().begin()
            while block.isValid():
                it=block.begin()
                while not it.atEnd():
                    frag=it.fragment();fmt=frag.charFormat()
                    if fmt.isImageFormat() and identity in fmt.toImageFormat().name():
                        cursor=QtGui.QTextCursor(browser.document());cursor.setPosition(frag.position())
                        browser.setTextCursor(cursor);browser.ensureCursorVisible();self.app.processEvents()
                    it+=1
                block=block.next()
        replies=self.window.findChildren(QtWidgets.QWidget,'aiVisualReply');self.keep.extend(replies)
        return next((w for w in replies if w.property('visualIdentity')==identity),None)
    def tick(self):
        if self.phase not in ('links','check-source','check-cbuffer'):
            return super(EffectLive,self).tick()
        try:
            if self.phase=='links':
                if time.monotonic()<self.ready_at:return
                task=self.result['cases'][-1]['taskId']
                state=json.loads((Path(self.spec['eventsRoot'])/(task+'.report-state.json')).read_text(encoding='utf-8'))
                self.cards=[]
                for ref in state.get('replyVisuals',[]):
                    manifest=json.loads((self.visual_root/(ref['id']+'.json')).read_text(encoding='utf-8'))
                    self.cards.extend((ref['id'],i,c)for i,c in enumerate(manifest['cards']))
                kinds={c['type']for _,_,c in self.cards}
                if not {'code','compare','diagram'}<=kinds:raise RuntimeError('Missing code/comparison/local flow in real answer: '+str(kinds))
                checks=self.result['checks']={}
                checks['globalFramePointerUnchanged']=hashlib.sha256(self.pointer.read_bytes()).hexdigest()==self.pointer_before
                comparisons=[v for v in self.cards if v[2]['type']=='compare'and v[2].get('experiment',{}).get('verified')]
                if not comparisons:raise RuntimeError('No verified controlled comparison delivered')
                identity,index,card=next((v for v in comparisons if v[2]['experiment']['outcome']=='changed'),comparisons[0]);reply=self.locate(identity)
                if reply is None:raise RuntimeError('Native comparison card not in chat')
                selector=reply.findChild(QtWidgets.QComboBox);self.keep.append(selector);selector.setCurrentIndex(index);self.app.processEvents()
                page=reply.findChild(QtWidgets.QStackedWidget).currentWidget();self.keep.append(page)
                canvas=page.findChild(QtWidgets.QWidget,'aiVisualImageCanvas');divider=page.findChild(QtWidgets.QSlider,'aiVisualDivider');self.keep.extend([canvas,divider])
                divider.setValue(20);before=canvas.grab().toImage();divider.setValue(80)
                checks['controlledSliderMoves']=canvas.property('visualWipeRatio')==.8
                checks['experimentPixelsChange']=before!=canvas.grab().toImage()
                checks['comparisonFocusesMarkedRegion']=bool(canvas.property('visualRegionFocused'))
                reply.grab().save(str(self.root/'effect-comparison.png'))
                self.result['experiments']=[c['experiment']for _,_,c in comparisons]
                identity,index,flow=[v for v in self.cards if v[2]['type']=='diagram'][-1];reply=self.locate(identity)
                reply.findChild(QtWidgets.QComboBox).setCurrentIndex(index);self.app.processEvents()
                checks['flowHasRealRTs']=sum(bool(n.get('image'))for n in flow['nodes'])>=2
                checks['flowHasCalculationOrChange']=bool(flow.get('edgeLabels'))or any(n.get('calculation')for n in flow['nodes'])
                flow_page=reply.findChild(QtWidgets.QStackedWidget).currentWidget();self.keep.append(flow_page)
                flow_canvas=flow_page.findChild(QtWidgets.QWidget,'aiVisualImageCanvas');self.keep.append(flow_canvas)
                point=QtCore.QPoint(flow_canvas.width()//6,flow_canvas.height()//4)
                self.app.sendEvent(flow_canvas,QtGui.QHelpEvent(QtCore.QEvent.ToolTip,point,flow_canvas.mapToGlobal(point)))
                checks['flowTooltipHasCompleteDescription']=flow['nodes'][0]['description']in flow_canvas.toolTip()
                self.result['flowTooltipDiagnostics']={'text':flow_canvas.toolTip(),
                    'displayed':QtWidgets.QToolTip.text(),'point':[point.x(),point.y()],
                    'size':[flow_canvas.width(),flow_canvas.height()],'visible':flow_canvas.isVisible()}
                QtWidgets.QToolTip.hideText()
                reply.grab().save(str(self.root/'effect-flow.png'))
                # Expansion must be a separate dialog, with the same local card.
                def inspect_expansion():
                    dialogs=[w for w in self.app.topLevelWidgets()if isinstance(w,QtWidgets.QDialog)and w.isVisible()]
                    local=next((d for d in dialogs if d.findChild(QtWidgets.QWidget,'aiVisualReply')),None)
                    checks['flowExpandsInSeparateWindow']=local is not None
                    if local:
                        local.grab().save(str(self.root/'effect-flow-expanded.png'));local.accept()
                QtCore.QTimer.singleShot(300,inspect_expansion)
                expand=next(b for b in reply.findChildren(QtWidgets.QPushButton)if b.text()==QtCore.QCoreApplication.translate('AIVisualReply','Expand'))
                self.phase='expand-check'
                expand.click()
                self.result['flowCard']=flow
                identity,index,card=next(v for v in self.cards if v[2]['type']=='code');reply=self.locate(identity)
                reply.findChild(QtWidgets.QComboBox).setCurrentIndex(index);self.app.processEvents()
                source=reply.findChild(QtWidgets.QTextBrowser,'aiVisualCode');self.keep.append(source)
                if source is None:raise RuntimeError('Native saved-source code block missing')
                reply.grab().save(str(self.root/'effect-code.png'))
                self.code=card['code'];source.anchorClicked.emit(QtCore.QUrl(self.code['url']))
                self.deadline=time.monotonic()+35;self.phase='check-source';self.save();return
            if self.phase=='check-source':
                bridge=sys.modules['renderdoc_mcp_bridge'];panel=bridge._shader_workspace_ui
                document=panel._current_document()if panel else None
                good=(document and os.path.normcase(os.path.abspath(document.get('sourcePath','')))==os.path.normcase(os.path.abspath(self.code['path'])) and
                      int(document['editor'].GetCursorLine())==int(self.code['startLine']))
                if not good:
                    if time.monotonic()>self.deadline:raise RuntimeError('Code click did not locate exact saved source line')
                    return
                self.result['checks']['sourceLineNavigationVerified']=True
                self.result['sourceNavigation']={'path':self.code['path'],'line':self.code['startLine']}
                urls=[]
                for _,_,card in self.cards:
                    urls.extend(link['url']for link in card.get('links',[])if link['url'].startswith('rdoc://cbuffer/'))
                browsers=self.window.findChildren(QtWidgets.QTextBrowser);self.keep.extend(browsers)
                for browser in browsers:
                    block=browser.document().begin()
                    while block.isValid():
                        it=block.begin()
                        while not it.atEnd():
                            href=it.fragment().charFormat().anchorHref()
                            if href.startswith('rdoc://cbuffer/'):urls.append(href)
                            it+=1
                        block=block.next()
                if not urls:raise RuntimeError('No CBuffer parameter hyperlink delivered')
                self.cbuffer_queue=list(dict.fromkeys(urls));self.cbuffer_url=self.cbuffer_queue.pop(0)
                self.target=json.loads((self.visual_root/'targets'/(self.cbuffer_url.rsplit('/',1)[-1]+'.json')).read_text(encoding='utf-8'))['target']
                # Emit through the real transcript link handler, not direct buffer API.
                transcript=next(b for b in browsers if 'rdoc://cbuffer/'in b.toHtml() or 'bread-visual-slot:'in b.toHtml())
                transcript.anchorClicked.emit(QtCore.QUrl(self.cbuffer_url))
                self.deadline=time.monotonic()+30;self.phase='check-cbuffer';self.save();return
            if self.phase=='check-cbuffer':
                widgets=self.window.findChildren(QtWidgets.QWidget);self.keep.extend(widgets)
                found=next((w for w in widgets if w.property('cbufferNavigationStatus')=='selected'and
                            w.property('cbufferNavigationPath')==self.target['path']),None)
                if found is None:
                    if time.monotonic()>self.deadline:
                        self.result['cbufferDiagnostics']={'eventId':int(pyrenderdoc.CurEvent()),'target':self.target,
                            'viewers':[{'class':w.metaObject().className(),'name':w.objectName(),
                                        'status':w.property('cbufferNavigationStatus'),'path':w.property('cbufferNavigationPath'),
                                        'displayedEvent':w.property('cbufferDisplayedEvent')}
                                       for w in widgets if 'BufferViewer'in w.metaObject().className()],
                            'messages':[b.toPlainText()[-1800:]for b in self.window.findChildren(QtWidgets.QTextBrowser)
                                        if 'rdoc://cbuffer/'in b.toHtml()or 'bread-visual-slot:'in b.toHtml()]}
                        try:
                            pipe=pyrenderdoc.CurPipelineState();ref=pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                            binding=pipe.GetConstantBlock(rd.ShaderStage.Pixel,self.target['blockIndex'],self.target['arrayIndex'])
                            self.result['cbufferDiagnostics'].update(shaderRawSha256=hashlib.sha256(bytes(ref.rawBytes)).hexdigest(),
                                bufferResourceId=str(binding.descriptor.resource))
                        except Exception:self.result['cbufferDiagnostics']['pipelineError']=traceback.format_exc()
                        self.window.grab().save(str(self.root/'effect-cbuffer-failure.png'))
                        raise RuntimeError('CBuffer link did not select the reflected parameter')
                    return
                checks=self.result['checks'];checks['cbufferLeafNavigationVerified']=True
                checks['cbufferEventVerified']=int(pyrenderdoc.CurEvent())==self.target['eventId']
                checks['cbufferViewerVisible']=bool(found.isVisible())
                value=found.findChild(QtWidgets.QDoubleSpinBox,'cbufferLiveValue0')
                self.keep.append(value)
                checks['cbufferEditorReady']=bool(value is not None and value.isVisible()and value.isEnabled())
                self.result['cbufferEditorValue']=value.value()if value is not None else None
                self.result['cbufferTarget']=self.target
                self.result.setdefault('cbufferTargets',[]).append(self.target)
                found.grab().save(str(self.root/'effect-cbuffer.png'))
                if self.cbuffer_queue:
                    self.cbuffer_url=self.cbuffer_queue.pop(0)
                    self.target=json.loads((self.visual_root/'targets'/(self.cbuffer_url.rsplit('/',1)[-1]+'.json')).read_text(encoding='utf-8'))['target']
                    browsers=self.window.findChildren(QtWidgets.QTextBrowser);self.keep.extend(browsers)
                    transcript=next(b for b in browsers if 'bread-visual-slot:'in b.toHtml())
                    transcript.anchorClicked.emit(QtCore.QUrl(self.cbuffer_url));self.deadline=time.monotonic()+30;return
                self.window.grab().save(str(self.root/'effect-answer-chat.png'))
                if not all(checks.values()):raise RuntimeError('Failed native checks: '+str([k for k,v in checks.items()if not v]))
                self.finish()
        except Exception:self.finish(traceback.format_exc())


runner=EffectLive();sys.modules['__main__']._bread_effect_live=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
