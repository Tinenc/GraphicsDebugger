"""Restore the existing user's capture selection after a graceful development update.

Also prepares and cancels one Vulkan annotation without submitting any model request.
Leaves the application running and never edits capture/project/Shader content.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from PySide2 import QtCore, QtWidgets
import renderdoc as rd


class Restore(object):
    def __init__(self):
        self.report = Path(os.environ['RD_ANNOTATION_RESTORE_REPORT'])
        self.base = self.report.parent
        self.before = json.loads((self.base/'before-deployment-context.json').read_text(encoding='utf-8'))
        self.event = int(self.before['events']['currentEventId'])
        self.resource = self.before.get('texture', {}).get('resourceId')
        self.start = time.monotonic()
        self.loaded_at = None
        self.phase = 'loading'
        self.keep = []
        self.result = {'ok':False,'pid':os.getpid(),'eventId':self.event,'resourceId':self.resource}

    def save(self):
        self.result['phase'] = self.phase
        self.report.write_text(json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')

    def tick(self):
        try:
            if time.monotonic()-self.start > 300:
                raise RuntimeError('Restoration timed out at '+self.phase)
            if self.phase == 'loading':
                if not pyrenderdoc.IsCaptureLoaded():return
                if self.loaded_at is None:self.loaded_at=time.monotonic()
                if time.monotonic()-self.loaded_at < 3:return
                bridge=sys.modules.get('renderdoc_mcp_bridge')
                if bridge is None:return
                if getattr(bridge,'_project_auto_restore_client',None) is not None:return
                pyrenderdoc.SetEventID([],self.event,self.event,True)
                pyrenderdoc.ShowTextureViewer()
                self.viewer=pyrenderdoc.GetTextureViewer()
                self.window=pyrenderdoc.GetMainWindow().Widget()
                self.keep.extend([self.viewer,self.window])
                texture=next((t.resourceId for t in pyrenderdoc.GetTextures()
                              if str(t.resourceId) in (str(self.resource),'ResourceId::'+str(self.resource))),None)
                if texture is not None:self.viewer.ViewTexture(texture,rd.CompType.Typeless,True)
                self.window.findChild(QtWidgets.QAction,'action_AI_Assistant').trigger()
                self.ready_at=time.monotonic()+1
                self.phase='brush';self.save();return
            if self.phase == 'brush':
                if time.monotonic()<self.ready_at:return
                self.brush=self.window.findChild(QtWidgets.QPushButton,'aiAnnotateRenderTarget')
                if self.brush is None or not self.brush.isEnabled():return
                self.keep.append(self.brush)
                self.brush.click();self.phase='canvas';self.save();return
            if self.phase == 'canvas':
                panel=self.window.findChild(QtWidgets.QWidget,'aiTextureAnnotation')
                if panel is None:return
                self.keep.append(panel)
                self.result['vulkanCanvasVisible']=bool(panel.isVisible())
                canvas=panel.findChild(QtWidgets.QWidget,'aiAnnotationCanvas')
                self.keep.append(canvas)
                self.result['canvasSize']=[canvas.width(),canvas.height()]
                panel.grab().save(str(self.base/'restored-vulkan-canvas.png'))
                text=QtCore.QCoreApplication.translate('AITextureAnnotation','Cancel')
                buttons=panel.findChildren(QtWidgets.QAbstractButton)
                self.keep.extend(buttons)
                next(b for b in buttons if b.text()==text).click()
                self.phase='verify';self.save();return
            if self.phase == 'verify':
                self.result.update(currentEventId=int(pyrenderdoc.CurEvent()),
                    selectedEventId=int(pyrenderdoc.CurSelectedEvent()),
                    currentResource=str(self.viewer.GetCurrentResource()),capture=str(pyrenderdoc.GetCaptureFilename()))
                before=json.loads((self.base/'get_shader_workspace_ui_state.json').read_text(encoding='utf-8'))
                checks=[]
                for document in before.get('documents',[]):
                    path=Path(document.get('sourcePath',''))
                    expected=document.get('sourceSha256')
                    if expected and path.is_file():
                        observed=hashlib.sha256(path.read_bytes()).hexdigest()
                        checks.append({'workspaceId':document.get('workspaceId'),'unchanged':observed==expected})
                self.result['savedSourceChecks']=checks
                self.result['ok']=(self.result['vulkanCanvasVisible'] and self.result['currentEventId']==self.event
                                   and all(x['unchanged'] for x in checks))
                self.timer.stop();self.phase='restored';self.save()
        except Exception:
            self.timer.stop();self.result['error']=traceback.format_exc();self.phase='failed';self.save()

    def run(self):
        self.timer=QtCore.QTimer();self.timer.setInterval(200);self.timer.timeout.connect(self.tick)
        self.timer.start();self.save()


runner=Restore()
sys.modules['__main__']._bread_deployment_restore=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.run)
