"""Reopen real-model effect cards and verify native links without a model request."""
import os,sys
from pathlib import Path
fixture=Path(os.environ['RD_VISUAL_FIXTURE_ROOT'])/'qrenderdoc_effect_explanation_live.py'
source=fixture.read_text(encoding='utf-8')
exec(compile(source[:source.rindex('runner=EffectLive()')],str(fixture),'exec'),globals())


class EffectHistory(EffectLive):
    def __init__(self):
        super(EffectHistory,self).__init__()
        previous=json.loads((self.root/'ui-result.json').read_text(encoding='utf-8'))
        self.result['cases']=previous['cases'];self.phase='history-open'
        self.result['schema']='bread-effect-history-validation/v1'
    def save(self):
        self.result['phase']=self.phase
        (self.root/'effect-history-result.json').write_text(json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')
    def tick(self):
        try:
            if time.monotonic()-self.started>200:raise RuntimeError('History verification timeout: '+self.phase)
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
            return super(EffectHistory,self).tick()
        except Exception:self.finish(traceback.format_exc())


runner=EffectHistory();sys.modules['__main__']._bread_effect_history=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
