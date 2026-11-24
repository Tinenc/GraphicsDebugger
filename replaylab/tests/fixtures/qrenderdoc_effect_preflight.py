"""Owned real Vulkan replay probe; no model request and no canonical source edit."""
import json,os,sys,time,traceback,subprocess
from pathlib import Path
from PySide2 import QtCore,QtWidgets
import renderdoc as rd

class Probe(object):
    def __init__(self):
        self.root=Path(os.environ['RD_ANNOTATION_RUN']);self.spec=json.loads((self.root/'spec.json').read_text(encoding='utf-8'))
        self.phase='open';self.start=time.monotonic();self.result={'ok':False,'pid':os.getpid()}
    def save(self):
        self.result['phase']=self.phase;(self.root/'ui-result.json').write_text(json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')
    def tick(self):
        try:
            if time.monotonic()-self.start>500:raise RuntimeError('GPU probe timeout at '+self.phase)
            if self.phase=='open':
                bridge=sys.modules.get('renderdoc_mcp_bridge')
                if bridge is None:return
                bridge._cancel_project_auto_activation();bridge._schedule_project_auto_activation=lambda:None
                pyrenderdoc.LoadCapture(self.spec['capture'],rd.ReplayOptions(),self.spec['capture'],False,True)
                self.phase='worker';self.save();return
            if self.phase=='worker':
                if not pyrenderdoc.IsCaptureLoaded():return
                pyrenderdoc.SetEventID([],1326,1326,True)
                repo=Path(os.environ['RD_VISUAL_FIXTURE_ROOT']).parents[2]
                worker=self.root/'worker.py'
                worker.write_text('''from pathlib import Path
import json,sys,hashlib,traceback,shutil,os,asyncio
root=Path(sys.argv[1]);repo=Path(sys.argv[2]);sys.path.insert(0,str(repo/'replaylab/mcp_host'));sys.path.insert(0,str(repo/'replaylab/src'))
from src.replaylab_tools import configure_environment
configure_environment(repo/'x64/Development-RTAnnotation/replaylab')
from src import server
try:
 spec=json.loads((root/'spec.json').read_text(encoding='utf-8'));workspace=spec['cases'][0]['workspacePath']
 status=server._shader_workspace_service().status(workspace);path=Path(status['source']['path']);source=path.read_bytes()
 case=spec['cases'][0]
 old=case.get('oldText','return (((highlight*reflectance.specularColor)*primaryHighlightIntensity)*5.0f)*reflectance.wetSpecularWeight;')
 new=case.get('newText','return ((((highlight*reflectance.specularColor)*primaryHighlightIntensity)*5.0f)*reflectance.wetSpecularWeight) * saturate(1.0f - abs(primaryHighlightIntensity) * 1000000.0f);')
 observation=case.get('observationEventId',spec.get('eventId',1654));roi=case.get('roi',[.42,.18,.58,.46])
 if spec['cases'][0].get('frozenMCP'):
  from mcp import ClientSession,StdioServerParameters
  from mcp.client.stdio import stdio_client
  async def probe():
   params=StdioServerParameters(command=str(repo/'x64/Development-RTAnnotation/replaylab/BreadMCP.exe'),env=dict(os.environ))
   async with stdio_client(params)as streams:
    async with ClientSession(*streams)as client:
     await client.initialize()
     listing=await client.list_tools();tool=next(t for t in listing.tools if t.name=='run_shader_effect_experiment')
     reply=await client.call_tool(tool.name,dict(workspace_path=workspace,expected_source_sha256=hashlib.sha256(source).hexdigest(),edits=[dict(old_text=old,new_text=new)],observation_event_id=observation,attachment_index=0,roi=roi,flip_y=False,label='Frozen MCP effect control'))
     response=json.loads(reply.content[0].text)
     response['frozenMCPImageBlocks']=sum(c.type=='image'for c in reply.content)
     response['typedEditSchemaVerified']=set(tool.inputSchema['$defs']['ExactSourceEdit']['properties'])=={'old_text','new_text'}
     return response
  response=asyncio.run(probe())
 else:
  response=json.loads(server.run_shader_effect_experiment(workspace,hashlib.sha256(source).hexdigest(),[dict(old_text=old,new_text=new)],observation,0,roi,False,'Focused effect preflight')[0])
 (root/'experiment-result.json').write_text(json.dumps(response,ensure_ascii=False,indent=2),encoding='utf-8')
except Exception:
 (root/'experiment-result.json').write_text(json.dumps({'ok':False,'error':traceback.format_exc()},ensure_ascii=False,indent=2),encoding='utf-8')
''',encoding='utf-8')
                self.out=(self.root/'worker.log').open('w',encoding='utf-8')
                self.process=subprocess.Popen([str(repo/'replaylab/.venv/Scripts/python.exe'),'-X','utf8',str(worker),str(self.root),str(repo)],
                    cwd=str(repo),stdout=self.out,stderr=subprocess.STDOUT,creationflags=0x08000000)
                self.phase='wait';self.save();return
            if self.phase=='wait':
                if self.process.poll()is None:return
                self.out.close();result=json.loads((self.root/'experiment-result.json').read_text(encoding='utf-8'))
                self.result['experiment']=result;self.result['ok']=bool(result.get('ok'))
                self.result['restoredEventId']=int(pyrenderdoc.CurEvent())
                self.finish()
        except Exception:self.result['error']=traceback.format_exc();self.finish()
    def finish(self):
        self.timer.stop();self.phase='finished';self.save();pyrenderdoc.CloseCapture()
        app=QtWidgets.QApplication.instance();QtCore.QTimer.singleShot(100,app.closeAllWindows);QtCore.QTimer.singleShot(300,app.quit)
    def start_timer(self):
        self.timer=QtCore.QTimer();self.timer.setInterval(200);self.timer.timeout.connect(self.tick);self.timer.start()

runner=Probe();sys.modules['__main__']._bread_effect_probe=runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start_timer)
