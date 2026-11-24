"""Reopen both real-model answers and exercise their native visual navigation."""
import os
from pathlib import Path

fixture=Path(os.environ['RD_VISUAL_FIXTURE_ROOT'])/'qrenderdoc_effect_history.py'
source=fixture.read_text(encoding='utf-8')
exec(compile(source[:source.rindex('runner=EffectHistory()')],str(fixture),'exec'),globals())


class AnnotationEvidenceHistory(EffectHistory):
    def __init__(self):
        super(AnnotationEvidenceHistory,self).__init__()
        self.all_cases=list(self.result['cases'])
        self.face=next(c for c in self.all_cases if c['id']=='face-shadow')
        self.mouth=next(c for c in self.all_cases if c['id']=='mouth-line')
        self.result['cases']=[self.face]
        self.face_selected=False
        self.mouth_finished=False
        self.result['schema']='bread-annotation-evidence-history/v1'

    def save(self):
        self.result['phase']=self.phase
        (self.root/'annotation-evidence-history.json').write_text(
            json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')

    def cards_for(self,case):
        task=case['taskId']
        state=json.loads((Path(self.spec['eventsRoot'])/(task+'.report-state.json')).read_text(encoding='utf-8'))
        cards=[]
        for ref in state.get('replyVisuals',[]):
            manifest=json.loads((self.visual_root/(ref['id']+'.json')).read_text(encoding='utf-8'))
            cards.extend((ref['id'],i,c)for i,c in enumerate(manifest['cards']))
        return cards

    def select_reply(self,identity):
        if self.locate(identity) is not None:return
        bar=self.window.findChild(QtWidgets.QFrame,'aiConversationBar')
        # AIConversationListView deliberately has no Q_OBJECT; its native
        # metaobject is QWidget. Select the direct tab-strip widget by role.
        widgets=bar.findChildren(QtWidgets.QWidget);self.keep.extend(widgets)
        children=[w for w in widgets if w.parentWidget()==bar]
        tabs=next(w for w in children if not isinstance(w,QtWidgets.QAbstractButton))
        self.keep.extend([bar,tabs])
        self.app.sendEvent(tabs,QtGui.QKeyEvent(QtCore.QEvent.KeyPress,QtCore.Qt.Key_Home,QtCore.Qt.NoModifier))
        for _ in range(6):
            self.app.sendEvent(tabs,QtGui.QKeyEvent(QtCore.QEvent.KeyPress,QtCore.Qt.Key_Return,QtCore.Qt.NoModifier))
            self.app.processEvents()
            if self.locate(identity) is not None:return
            self.app.sendEvent(tabs,QtGui.QKeyEvent(QtCore.QEvent.KeyPress,QtCore.Qt.Key_Right,QtCore.Qt.NoModifier))
        raise RuntimeError('Saved conversation card could not be selected: '+identity)

    def finish(self,error=None):
        if not error and not self.mouth_finished:
            self.phase='mouth-cards';self.save();return
        self.result['cases']=self.all_cases
        super(AnnotationEvidenceHistory,self).finish(error)

    def tick(self):
        try:
            if self.phase=='links' and not self.face_selected:
                if time.monotonic()<self.ready_at:return
                cards=self.cards_for(self.face)
                if not cards:raise RuntimeError('Face answer has no visual manifests')
                self.select_reply(cards[0][0]);self.face_selected=True
            if self.phase=='mouth-cards':
                self.mouth_cards=self.cards_for(self.mouth)
                if not self.mouth_cards:raise RuntimeError('Mouth answer has no visual manifests')
                self.select_reply(self.mouth_cards[0][0])
                checks=self.result['checks']
                image_cards=[v for v in self.mouth_cards if v[2]['type']=='image']
                checks['mouthHasNativeMeshAndUVImages']=len(image_cards)>=2
                checks['mouthHasImageAnnotations']=sum(len(c.get('annotations',[]))for _,_,c in image_cards)>=2
                for identity,index,card in image_cards:
                    reply=self.locate(identity)
                    if reply is None:raise RuntimeError('Saved mouth image is missing from chat')
                    reply.findChild(QtWidgets.QComboBox).setCurrentIndex(index);self.app.processEvents()
                    reply.grab().save(str(self.root/('mouth-history-image-'+str(index)+'.png')))
                compares=[v for v in self.mouth_cards if v[2]['type']=='compare'and v[2].get('experiment',{}).get('verified')]
                if not compares:raise RuntimeError('Mouth answer lacks a verified comparison')
                identity,index,card=compares[0];reply=self.locate(identity)
                reply.findChild(QtWidgets.QComboBox).setCurrentIndex(index);self.app.processEvents()
                page=reply.findChild(QtWidgets.QStackedWidget).currentWidget();self.keep.append(page)
                canvas=page.findChild(QtWidgets.QWidget,'aiVisualImageCanvas')
                divider=page.findChild(QtWidgets.QSlider,'aiVisualDivider');self.keep.extend([canvas,divider])
                divider.setValue(20);before=canvas.grab().toImage();divider.setValue(80)
                checks['mouthComparisonPixelsChange']=before!=canvas.grab().toImage()
                checks['mouthExperimentHasDifferentImages']=card['image']['sha256']!=card['secondImage']['sha256']
                reply.grab().save(str(self.root/'mouth-history-comparison.png'))
                links=[link['url']for _,_,c in image_cards for link in c.get('links',[])if link['url'].startswith('rdoc://uv/')]
                if not links:raise RuntimeError('Native mouth images lack mesh/UV navigation')
                self.mesh_url=links[0]
                self.mesh_target=json.loads((self.visual_root/'targets'/(self.mesh_url.rsplit('/',1)[-1]+'.json')).read_text(encoding='utf-8'))['target']
                checks['mouthLinkHasExactComponents']=bool(self.mesh_target.get('selectedElements'))
                pyrenderdoc.SetEventID([],1326,1326,True)
                # Prime only the evidence reader, at a different EID. No component
                # IDs, channel or texture are applied by the test. The actual
                # transcript click must restore every target field and selection.
                view=pyrenderdoc.GetMeshPreview().Widget();self.keep.append(view)
                view.setProperty('meshEvidenceRequest',json.dumps({
                    'eventId':self.mesh_target['eventId'],'stage':self.mesh_target['stage'],
                    'requestId':'history-read-selection'}))
                browsers=self.window.findChildren(QtWidgets.QTextBrowser);self.keep.extend(browsers)
                transcript=next(b for b in browsers if 'bread-visual-slot:'in b.toHtml())
                transcript.anchorClicked.emit(QtCore.QUrl(self.mesh_url))
                self.deadline=time.monotonic()+35;self.phase='mouth-navigation';self.save();return
            if self.phase=='mouth-navigation':
                view=pyrenderdoc.GetMeshPreview().Widget();self.keep.append(view)
                value=view.property('meshEvidence')
                if not value or not value.get('ready'):
                    if time.monotonic()>self.deadline:raise RuntimeError('Mouth mesh navigation did not finish')
                    return
                target=self.mesh_target;checks=self.result['checks']
                checks['mouthLinkRestoresEID']=int(pyrenderdoc.CurEvent())==target['eventId']
                checks['mouthLinkRestoresChannel']=value['channel']==target['channel']and value['pair']==target['pair']
                checks['mouthLinkRestoresTexture']=str(value['textureResourceId'])==str(target['textureResourceId'])
                checks['mouthLinkRestoresExactSelection']=value['selectionMode']==target['selectionMode']and set(value['selectedElements'])==set(target['selectedElements'])
                self.result['mouthNavigation']={'target':target,'actual':{k:v for k,v in value.items()if k not in ('images','meshPng','uvPng')}}
                view.grab().save(str(self.root/'mouth-history-mesh-navigation.png'))
                self.mouth_finished=True
                if not all(checks.values()):raise RuntimeError('Failed native checks: '+str([k for k,v in checks.items()if not v]))
                self.finish();return
            super(AnnotationEvidenceHistory,self).tick()
        except Exception:self.finish(traceback.format_exc())


runner=AnnotationEvidenceHistory();sys.modules['__main__']._bread_annotation_history=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
