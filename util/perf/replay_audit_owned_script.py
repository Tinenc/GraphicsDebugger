"""Execute an owned audit file through Bread's existing native Python console API."""
import argparse
import json
from pathlib import Path
import sys
import time

from replay_audit_driver import ROOT, read_json, write_json

sys.path[:0] = [str(ROOT / 'output/tooling'), str(ROOT / 'replaylab/mcp_host')]
import psutil
import src.ipc_client as ipc

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('script')
p.add_argument('--label', required=True)
args = p.parse_args()
source = Path(args.script).resolve()
assert source.is_file() and ROOT in source.parents and source.suffix == '.py'
owner = read_json(ROOT / 'output/interactive-memory-audit/handoff-verified.json')
process = psutil.Process(owner['pid'])
assert Path(process.exe()).resolve() == Path(owner['executable']).resolve()
assert abs(process.create_time() - owner['creationEpoch']) < .01
ipc.INSTANCE_ROOT = str(Path(process.environ()['LOCALAPPDATA']) / 'ReplayLab/ipc-v2/instances')
client = ipc.IPCClient(instance_id=next(Path(ipc.INSTANCE_ROOT).glob(str(process.pid) + '-*')).name)
context = client.call('get_current_renderdoc_context', {}, timeout=30)
client.capture_session_id = context['capture']['captureSessionId']
ui = client.call('get_application_ui_state', {'query': 'lineInput', 'limit': 20}, timeout=30)
editor = next(x for x in ui['objects'] if x.get('objectName') == 'lineInput')
buttons = client.call('get_application_ui_state', {'query': 'execute', 'limit': 100}, timeout=30)
button = next(x for x in buttons['objects'] if x.get('objectName') == 'execute'
              and x.get('className') == 'QPushButton')
assert editor.get('editable') and 'set_value' in editor['operations']
original = editor.get('value', '')
code = "exec(compile(open(%r, 'rb').read(), %r, 'exec'))" % (str(source), source.name)
out = ROOT / 'output/rt-cache-audit-20261112'
out.mkdir(exist_ok=True)
write_json(out / (args.label + '-before.json'), {'context': context, 'consoleSource': original})
changed = client.call('control_application_ui', {'action': 'set_value', 'target_id': editor['targetId'],
                       'arguments': {'value': code}}, timeout=30)
for _ in range(100):
    state = client.call('get_application_ui_state', {'query': 'lineInput', 'limit': 20}, timeout=30)
    operation = next((x for x in state.get('operations', []) if x.get('operationId') == changed.get('operationId')), {})
    if operation.get('status') in ('completed', 'failed'):
        assert operation['status'] == 'completed', operation
        break
    time.sleep(.02)
else:
    raise RuntimeError('Console source publication did not complete')
started = client.call('control_application_ui', {'action': 'activate', 'target_id': button['targetId'],
                       'arguments': {}}, timeout=30)
# Execution is asynchronous; the script must record its own completion receipt.
write_json(out / (args.label + '-dispatch.json'), {'set': changed, 'run': started,
           'pid': process.pid, 'script': str(source), 'scriptSha256': __import__('hashlib').sha256(source.read_bytes()).hexdigest()})
time.sleep(.2)
print(json.dumps(started, ensure_ascii=True))
