"""Private-desktop Qt regression; only writes its disposable fake project."""
import importlib.util
import json
import os
from pathlib import Path
import time
import traceback
from PySide2 import QtCore

source = Path(r'D:\Study\GitHub\renderdocUltra\replaylab\renderdoc_mcp_bridge\project_launcher.py')
spec = importlib.util.spec_from_file_location('bread_refresh_native', str(source))
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
root = Path(os.environ['BREAD_DIAG_UI_REPORT']).parent / 'fake-project'
(root/'.replaylab').mkdir(parents=True)
manifest = root/'.replaylab/workspace.json'
project = {'workspaceId':'a'*32,'name':'before','state':'bound','projectRoot':str(root),
    'scope':{},'assetRoot':None,'active':True,'captures':[
        {'path':str(root/('%d.rdc'%i)),'captureRefId':str(i+1)*64,'displayName':'%d.rdc'%i,
         'available':True,'sizeBytes':1} for i in range(2)]}
manifest.write_text(json.dumps(project), encoding='utf-8')
class Client:
    def list_projects(self): return [json.loads(manifest.read_text(encoding='utf-8'))]
started = time.monotonic()
state = {'phase':'initial'}

def start():
    dialog = module.ReplayLabProjectLauncher(Client(), lambda *a: None)
    state['dialog'] = dialog
    dialog.setWindowModality(QtCore.Qt.ApplicationModal)
    dialog.metadata_watch_timer.setInterval(100)
    dialog.show()
    timer = QtCore.QTimer(dialog)
    def finish(ok, error=''):
        timer.stop(); dialog.accept()
        result = {'ok':ok, 'error':error, 'checks':['live-modal-project-metadata-refresh',
            'capture-selection-preserved-by-path','watch-stopped-after-accept'] if ok else []}
        result['watchActiveAfterClose'] = dialog.metadata_watch_timer.isActive()
        result['ok'] = result['ok'] and not result['watchActiveAfterClose']
        Path(os.environ['BREAD_DIAG_UI_REPORT']).write_text(json.dumps(result), encoding='utf-8')
        pyrenderdoc.GetMainWindow().Widget().close()
        QtCore.QTimer.singleShot(500, lambda: os._exit(0))
    def tick():
        try:
            if time.monotonic()-started > 15: raise RuntimeError('Project metadata refresh timed out')
            if state['phase']=='initial' and not dialog._busy and dialog._watch_baseline is not None and dialog.capture_list.count()==2:
                dialog.capture_list.clearSelection(); dialog.capture_list.item(1).setSelected(True)
                project['name']='after'; manifest.write_text(json.dumps(project), encoding='utf-8')
                state['phase']='changed'
            elif state['phase']=='changed' and not dialog._busy and dialog.projects[0]['name']=='after':
                selected = dialog.capture_list.selectedItems()
                if len(selected)!=1 or selected[0].data(QtCore.Qt.UserRole)!=project['captures'][1]['path']:
                    raise RuntimeError('Refresh changed the selected capture')
                finish(True)
        except Exception:
            finish(False, traceback.format_exc())
    timer.timeout.connect(tick); timer.start(50)
    state['timer'] = timer
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start)
