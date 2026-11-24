"""Reopen real-model teaching cards and verify their final native implementation."""
import os,sys
from pathlib import Path
fixture=Path(os.environ['RD_VISUAL_FIXTURE_ROOT'])/'qrenderdoc_visual_teaching_live.py'
source=fixture.read_text(encoding='utf-8')
exec(compile(source[:source.rindex('runner=TeachingLive()')],str(fixture),'exec'),globals())


class TeachingHistory(TeachingLive):
    def __init__(self):
        super(TeachingHistory,self).__init__()
        previous=json.loads((self.root/'ui-result.json').read_text(encoding='utf-8'))
        self.result['cases']=previous['cases'];self.phase='history-open'
        self.result['schema']='bread-teaching-history-validation/v1'

    def save(self):
        self.result['phase']=self.phase
        (self.root/'teaching-history-result.json').write_text(json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')

    def tick(self):
        try:
            if time.monotonic()-self.started>200:raise RuntimeError('Teaching history timeout: '+self.phase)
            if self.phase=='history-open':
                bridge=sys.modules.get('renderdoc_mcp_bridge')
                if bridge is None:return
                bridge._cancel_project_auto_activation();bridge._schedule_project_auto_activation=lambda:None
                pyrenderdoc.LoadCapture(self.spec['capture'],rd.ReplayOptions(),self.spec['capture'],False,True)
                self.phase='history-assistant';self.save();return
            if self.phase=='history-assistant':
                if not pyrenderdoc.IsCaptureLoaded():return
                self.window=pyrenderdoc.GetMainWindow().Widget();self.keep.append(self.window)
                self.window.findChild(QtWidgets.QAction,'action_AI_Assistant').trigger()
                self.ready_at=time.monotonic()+2;self.phase='links';self.save();return
            return super(TeachingHistory,self).tick()
        except Exception:self.finish(traceback.format_exc())


runner=TeachingHistory();sys.modules['__main__']._bread_teaching_history=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
