"""Real-model mesh demonstrations and colored chat code, on an owned desktop."""
import os,sys
from pathlib import Path
fixture=Path(os.environ['RD_VISUAL_FIXTURE_ROOT'])/'qrenderdoc_effect_explanation_live.py'
source=fixture.read_text(encoding='utf-8')
exec(compile(source[:source.rindex('runner=EffectLive()')],str(fixture),'exec'),globals())


class TeachingLive(EffectLive):
    def tick(self):
        if self.phase!='links':return LiveAnnotation.tick(self)
        try:
            if time.monotonic()<self.ready_at:return
            state=json.loads((Path(self.spec['eventsRoot'])/(self.result['cases'][-1]['taskId']+'.report-state.json')).read_text(encoding='utf-8'))
            cards=[]
            for ref in state.get('replyVisuals',[]):
                manifest=json.loads((self.root/'local/ReplayLab/ai-visuals'/(ref['id']+'.json')).read_text(encoding='utf-8'))
                cards.extend((ref['id'],i,c)for i,c in enumerate(manifest['cards']))
            checks=self.result['checks']={}
            effects={c.get('meshEffect',{}).get('type')for _,_,c in cards if c['type']=='mesh'}
            if not {'inverted-hull','sine-displacement'}<=effects:raise RuntimeError('Model did not deliver both bounded mesh examples: '+str(effects))
            for effect in ['inverted-hull','sine-displacement']:
                identity,index,card=next(v for v in cards if v[2]['type']=='mesh'and v[2]['meshEffect']['type']==effect)
                reply=self.locate(identity)
                if reply is None:raise RuntimeError('Published mesh is not visible in native chat')
                reply.findChild(QtWidgets.QComboBox).setCurrentIndex(index);self.app.processEvents()
                page=reply.findChild(QtWidgets.QStackedWidget).currentWidget();self.keep.extend([reply,page])
                mesh=page.findChild(QtWidgets.QWidget,'aiVisualMeshCanvas');self.keep.append(mesh)
                on=page.findChild(QtWidgets.QCheckBox,'aiVisualEffectEnabled');self.keep.append(on);on.setChecked(True)
                amount=page.findChild(QtWidgets.QDoubleSpinBox,'aiVisualEffectAmount');self.keep.append(amount)
                amount.setValue(0);self.app.processEvents();before=mesh.grab().toImage();amount.setValue(.15);self.app.processEvents()
                checks[effect+'AmountChangesRenderedPixels']=before!=mesh.grab().toImage()
                comparison=page.findChild(QtWidgets.QCheckBox,'aiVisualMeshCompare');self.keep.append(comparison);comparison.setChecked(True);self.app.processEvents()
                checks[effect+'SynchronizedComparison']=bool(mesh.property('visualMeshCompare'))
                before=mesh.grab().toImage()
                for kind,point in [(QtCore.QEvent.MouseButtonPress,QtCore.QPointF(70,70)),(QtCore.QEvent.MouseMove,QtCore.QPointF(135,95)),(QtCore.QEvent.MouseButtonRelease,QtCore.QPointF(135,95))]:
                    self.app.sendEvent(mesh,QtGui.QMouseEvent(kind,point,QtCore.Qt.LeftButton,QtCore.Qt.LeftButton,QtCore.Qt.NoModifier))
                checks[effect+'OrbitChangesRender']=before!=mesh.grab().toImage()
                if effect=='sine-displacement':
                    phase=page.findChild(QtWidgets.QDoubleSpinBox,'aiVisualEffectPhase');self.keep.append(phase)
                    before=mesh.grab().toImage();phase.setValue(phase.value()+1.3);self.app.processEvents()
                    checks['phaseChangesRenderedGeometry']=before!=mesh.grab().toImage()
                reply.grab().save(str(self.root/(effect+'-demo.png')))
            summary=self.result['cases'][-1]['terminal']['summary']
            checks['answerContainsLanguageTaggedShaderCode']='```hlsl'in summary or '```glsl'in summary
            colors=set()
            palette={'#cc7832','#b8d7a3','#ffc66d','#a9b7c6','#6a8759','#6897bb','#808080','#9876aa',
                     '#0033b3','#871094','#00627a','#202020','#067d17','#1750eb','#6a737d','#9e5b00'}
            browsers=self.window.findChildren(QtWidgets.QTextBrowser);self.keep.extend(browsers)
            for browser in browsers:
                block=browser.document().begin()
                while block.isValid():
                    it=block.begin()
                    while not it.atEnd():
                        frag=it.fragment();color=frag.charFormat().foreground().color().name()
                        if frag.text().strip()and color in palette:colors.add(color)
                        it+=1
                    block=block.next()
            checks['chatRendersDistinctSyntaxRoles']=len(colors)>=4
            checks['globalFramePointerUnchanged']=hashlib.sha256(self.pointer.read_bytes()).hexdigest()==self.pointer_before
            self.result['codeColors']=sorted(colors);self.result['modelCards']=[c for _,_,c in cards]
            self.window.grab().save(str(self.root/'teaching-answer.png'))
            if not all(checks.values()):raise RuntimeError('Failed live teaching checks: '+str([k for k,v in checks.items()if not v]))
            self.finish()
        except Exception:self.finish(traceback.format_exc())


runner=TeachingLive();sys.modules['__main__']._bread_visual_teaching=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
