"""Restore real-model visual history in a fresh native process, without API calls."""
import base64
import json
import os
from pathlib import Path
import sys
import time
import traceback
import renderdoc as rd
from PySide2 import QtCore,QtGui,QtWidgets


class VisualHistory(object):
    def __init__(self):
        self.root=Path(os.environ['RD_ANNOTATION_RUN'])
        self.spec=json.loads((self.root/'spec.json').read_text(encoding='utf-8'))
        self.start=time.monotonic();self.phase='open';self.keep=[]
        self.result={'ok':False,'checks':{},'pid':os.getpid()}
    def save(self):
        self.result['phase']=self.phase
        (self.root/'visual-history-result.json').write_text(json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')
    def tick(self):
        try:
            app=QtWidgets.QApplication.instance()
            if time.monotonic()-self.start>180:raise RuntimeError('History restore timed out at '+self.phase)
            if self.phase=='open':
                bridge=sys.modules.get('renderdoc_mcp_bridge')
                if bridge is None:return
                bridge._cancel_project_auto_activation();bridge._schedule_project_auto_activation=lambda:None
                pyrenderdoc.LoadCapture(self.spec['capture'],rd.ReplayOptions(),self.spec['capture'],False,True)
                self.phase='assistant';self.save();return
            if self.phase=='assistant':
                if not pyrenderdoc.IsCaptureLoaded():return
                self.window=pyrenderdoc.GetMainWindow().Widget();self.keep.append(self.window)
                self.window.findChild(QtWidgets.QAction,'action_AI_Assistant').trigger()
                self.phase='restore';self.save();return
            if self.phase=='restore':
                replies=self.window.findChildren(QtWidgets.QWidget,'aiVisualReply');self.keep.extend(replies)
                if not replies:return
                self.reply=next((w for w in replies if w.isVisible()),replies[-1]);self.keep.append(self.reply)
                selector=self.reply.findChild(QtWidgets.QComboBox);self.keep.append(selector)
                self.result['checks']['freshProcessRestoresFourCards']=selector is not None and selector.count()==4
                selector.setCurrentIndex(2);app.processEvents()
                self.reply.grab().save(str(self.root/'visual-history-mesh.png'))
                mesh=self.reply.findChild(QtWidgets.QWidget,'aiVisualMeshCanvas');self.keep.append(mesh)
                self.result['checks']['restoredGeometryRenders']=mesh is not None and int(mesh.property('visualMeshTriangleCount'))>0
                # Query the concrete Qt type first. The shipped PySide bridge
                # caches a QWidget wrapper if parentWidget() creates it first.
                browsers=self.window.findChildren(QtWidgets.QTextBrowser)
                self.keep.extend(browsers)
                browser=next((b for b in browsers if b.viewport()==self.reply.parentWidget()),None)
                if browser is None:raise RuntimeError('Visual reply is outside the native chat viewport')
                self.keep.append(browser)
                old=self.reply.geometry();bar=browser.verticalScrollBar();self.keep.append(bar)
                bar.setValue(bar.minimum());app.processEvents()
                bar.setValue(bar.maximum());app.processEvents()
                self.result['checks']['scrollKeepsVisualAttached']=self.reply.isVisible() and self.reply.geometry().intersects(browser.viewport().rect())
                self.window.resize(1700,900);app.processEvents()
                self.ready=time.monotonic()+.5;self.phase='resize';self.save();return
            if self.phase=='resize':
                if time.monotonic()<self.ready:return
                self.reply.grab().save(str(self.root/'visual-history-resized.png'))
                self.result['checks']['resizeKeepsRendererVisible']=self.reply.isVisible() and self.reply.width()>180
                self.window.grab().save(str(self.root/'visual-history-chat.png'))
                self.result['ok']=all(self.result['checks'].values())
                if not self.result['ok']:raise RuntimeError('History checks failed')
                self.finish()
        except Exception:self.finish(traceback.format_exc())
    def finish(self,error=None):
        self.timer.stop()
        if error:self.result.update(ok=False,error=error)
        self.phase='finished';self.save();pyrenderdoc.CloseCapture()
        app=QtWidgets.QApplication.instance()
        QtCore.QTimer.singleShot(200,app.closeAllWindows);QtCore.QTimer.singleShot(400,app.quit)
    def run(self):
        self.timer=QtCore.QTimer();self.timer.setInterval(200);self.timer.timeout.connect(self.tick);self.timer.start()


runner=VisualHistory()
sys.modules['__main__']._bread_visual_history=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.run)
