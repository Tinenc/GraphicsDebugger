"""Native editor navigation against an immutable, offline-read source snapshot.

Run with run_connection_ui_smoke.py --ui-script. No capture, compiler, model,
source save or user-profile writes are needed in the disposable GUI.
"""
import importlib.util
import json
import os
from pathlib import Path
import traceback
import time
import ast
import sys
import threading
from PySide2 import QtCore
import qrenderdoc as qrd

REPO = Path(os.environ['BREAD_DIAG_SOURCE_ROOT'])
ROOT = REPO / 'output/bread-ui-107'


class Executor:
    def __init__(self): self.requests = []
    def submit(self, owner, work, done): self.requests.append((owner, work, done))
    def cancel_owner(self, owner): pass


def start():
    panel = None
    report = {'ok': False}
    try:
        spec = importlib.util.spec_from_file_location('renderdoc_mcp_bridge.navigation_probe',
            str(REPO / 'replaylab/renderdoc_mcp_bridge/project_shader_workspace.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        saved = json.loads((ROOT / 'native-snapshot.json').read_text(encoding='utf-8'))
        binding = saved['binding']; result = saved['result']
        console_spec = importlib.util.spec_from_file_location('renderdoc_mcp_bridge.console_probe',
            str(REPO / 'replaylab/renderdoc_mcp_bridge/shader_console.py'))
        console = importlib.util.module_from_spec(console_spec); console_spec.loader.exec_module(console)
        module._shader_console_protocol = console
        client_spec=importlib.util.spec_from_file_location('bread_hidden_project_probe',
            str(REPO/'replaylab/renderdoc_mcp_bridge/project_restore_client.py'))
        client_module=importlib.util.module_from_spec(client_spec);client_spec.loader.exec_module(client_module)
        helper=client_module.ReplayLabInternalProjectClient(os.environ['BREAD_DIAG_PROJECT_HELPER'])
        # Exercise the transport with our disposable OS probe, independently
        # of the packaged-executable resolver tested by the release audit.
        helper.internal_executable=os.environ['BREAD_DIAG_PROJECT_HELPER']
        status,body=helper._project_transport(1234,'fixture','POST',client_module.PROJECT_OPERATION_PATH,
            b'{}',5,threading.Event(),helper._restore_generation)
        assert status==200 and json.loads(body.decode('utf-8'))['consoleWindow']==0
        report['projectHelperConsoleWindow']=0
        status_ns = dict(sys.modules['renderdoc_mcp_bridge'].__dict__)
        status_ns['_PROJECT_STATUS_UI'] = dict(widget=None,label=None,progress=None,timer=None)
        status_tree = ast.parse((REPO/'replaylab/renderdoc_mcp_bridge/__init__.py').read_text(encoding='utf-8'))
        status_names = {'_project_status_parts','_elide_status_text','_show_project_background_status'}
        status_nodes = [node for node in status_tree.body
                        if isinstance(node,ast.FunctionDef) and node.name in status_names]
        exec(compile(ast.Module(body=status_nodes), 'native-status-probe', 'exec'),status_ns)
        status_ns['_show_project_background_status']('Restoring Shader 2 / 3',current=2,total=3)
        pyrenderdoc.GetMainWindow().Widget().resize(960,720)
        private = Path(os.environ['BREAD_DIAG_UI_REPORT']).parent / 'instance-data'
        private.mkdir()
        selection = private / 'active-project.json'
        selection.write_bytes(Path(saved['selectionPath']).read_bytes())
        journals = private / 'console-events'
        journals.mkdir()
        journal = journals / ('events.qrenderdoc.%d.%s.0000.jsonl' % (os.getpid(), 'a' * 32))
        console.default_console_journal_directory = lambda: str(journals)
        os.environ['RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE'] = str(selection)
        os.environ['RENDERDOC_ULTRA_PROJECT_REGISTRY'] = saved['projectRegistryPath']
        observations = []
        build = module._build_workspace_snapshot
        def observe(request):
            value = build(request)
            observations.append({'unchanged':value.get('unchanged', False),
                'sources':list(value.get('sources', {})), 'errors':value.get('sourceErrors', {}),
                'stale':value.get('staleSources', [])})
            return value
        module._build_workspace_snapshot = observe
        panel = module.ReplayLabShaderWorkspacePanel(pyrenderdoc, qrd,
            registry_path=saved['registryPath'], project_selection_path=str(selection),
            capture_binding_provider=lambda: binding,
            save_client=object())
        panel._request_live_usage = lambda *args: None
        panel._request_source_flow = lambda *args: None
        keys = list(result['sources'])
        assert len(keys) >= 2
        state = {'index':0, 'clicked':False, 'started':time.monotonic(), 'idle':None}
        report['sourceOpenMilliseconds']=[]
        timer = QtCore.QTimer(panel.top)
        def finish(error=''):
            timer.stop()
            report.update(ok=not bool(error), error=error, observations=observations,
                openedDocuments=len(panel._documents), pending=list(panel._pending_open))
            try: panel.shutdown()
            except Exception: report['shutdownError']=traceback.format_exc();report['ok']=False
            Path(os.environ['BREAD_DIAG_UI_REPORT']).write_text(json.dumps(report),encoding='utf-8')
            pyrenderdoc.GetMainWindow().Widget().close()
        def tick():
            try:
                # Heartbeats are unrelated to the selection/source files.
                (private/'instance.json').write_text(str(time.monotonic()),encoding='utf-8')
                journal.write_text('', encoding='utf-8')
                if time.monotonic()-state['started']>35:
                    raise AssertionError('Native asynchronous open timed out')
                if state['index']<len(keys):
                    key=keys[state['index']]
                    if not state['clicked'] and key in panel._tree_items:
                        state['clickTime']=time.monotonic()
                        if state['index'] == 1:
                            panel.refresh(True)
                            panel.file_tree.activated.emit(panel.file_tree.indexFromItem(panel._tree_items[key]))
                        else:
                            panel.file_tree.itemClicked.emit(panel._tree_items[key],0)
                        state['clicked']=True
                    if state['clicked'] and panel._current_workspace_id()==key:
                        assert panel._documents[key]['editor'].GetText()
                        model=panel._workspaces[key]
                        reply=panel.navigate_shader_target(dict(capturePath=model['capturePath'],
                            captureSessionId=binding['captureSessionId'],resourceId=model['resourceId'],
                            stage=model['stage'],entryPoint=model['entryPoint']))
                        assert reply.get('accepted'),reply
                        panel.file_tree.itemActivated.emit(panel._tree_items[key],0)
                        assert panel._current_workspace_id()==key
                        report['sourceOpenMilliseconds'].append(round((time.monotonic()-state['clickTime'])*1000,2))
                        state['index']+=1;state['clicked']=False
                    return
                if not state.get('reopened'):
                    key=keys[0]
                    if not state.get('reopening'):
                        assert panel._close_document(key,prompt=False)
                        panel._source_documents.pop(key,None)
                        panel.file_tree.itemClicked.emit(panel._tree_items[key],0)
                        state['reopening']=True
                    if panel._current_workspace_id()==key:
                        assert panel._documents[key]['editor'].GetText()
                        state['reopened']=True
                    return
                if state['idle'] is None and not panel._snapshot_running:
                    state['idle']=(time.monotonic(),sum(not x['unchanged'] for x in observations))
                if state['idle'] and time.monotonic()-state['idle'][0]>3:
                    full=sum(not x['unchanged'] for x in observations)-state['idle'][1]
                    assert full<=1,('Heartbeat caused full refresh loop',full)
                    label=status_ns['_PROJECT_STATUS_UI']['label']
                    progress=status_ns['_PROJECT_STATUS_UI']['progress']
                    assert label.width()>=160 and label.text() and label.isVisible(),label.width()
                    assert progress.isVisible() and progress.value()==2
                    report['progressTextWidth']=label.width()
                    report['windowWidth']=pyrenderdoc.GetMainWindow().Widget().width()
                    report['checks']=['asynchronous-native-tree-click','pipeline-target-selects-native-editor',
                        'independent-shader-switch','ipc-heartbeats-do-not-rescan-project',
                        'console-journal-appends-do-not-rescan-shaders',
                        'click-during-background-refresh-keeps-target',
                        'close-and-reopen-saved-shader','native-item-activation',
                        'embedded-python-36-project-helper-has-no-console',
                        'progress-label-visible-beside-bar-at-narrow-window']
                    finish()
            except Exception: finish(traceback.format_exc())
        timer.timeout.connect(tick);timer.start(50)
        panel._navigation_probe_timer=timer
        report['asynchronous'] = True
        return
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        if not report.get('asynchronous'):
            if panel is not None:
                try: panel.shutdown()
                except Exception: report['shutdownError'] = traceback.format_exc(); report['ok'] = False
            Path(os.environ['BREAD_DIAG_UI_REPORT']).write_text(json.dumps(report), encoding='utf-8')
            pyrenderdoc.GetMainWindow().Widget().close()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start)
