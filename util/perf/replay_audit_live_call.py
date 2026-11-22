"""Call the typed first-party bridge of the independently owned manual instance."""
import argparse
import json
from pathlib import Path
import sys
from replay_audit_driver import ROOT, read_json, write_json

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('tool')
p.add_argument('--params', default='{}')
p.add_argument('--params-file')
p.add_argument('--label', required=True)
p.add_argument('--timeout', type=int, default=45)
p.add_argument('--quiet', action='store_true')
args = p.parse_args()
sys.path.insert(0, str(ROOT / 'replaylab/mcp_host'))
sys.path.insert(0, str(ROOT / 'output/tooling'))
import psutil
import src.ipc_client as ipc
owned = read_json(ROOT / 'output/interactive-memory-audit/handoff-verified.json')
process = psutil.Process(owned['pid'])
assert Path(process.exe()).resolve() == Path(owned['executable']).resolve()
assert abs(process.create_time() - owned['creationEpoch']) < .01
run = ROOT / 'output/runs/interactive-manual-delivery-001'
ipc.INSTANCE_ROOT = str(run / 'local/ReplayLab/ipc-v2/instances')
instance = next(x.name for x in Path(ipc.INSTANCE_ROOT).iterdir()
                if x.name.startswith(str(process.pid) + '-'))
client = ipc.IPCClient(instance_id=instance)
context = client.call('get_current_renderdoc_context', {}, timeout=30)
client.capture_session_id = context['capture']['captureSessionId']
out = ROOT / 'output/followup-interactive-20261112'
out.mkdir(exist_ok=True)
value = context if args.tool == 'get_current_renderdoc_context' else client.call(
    args.tool, read_json(Path(args.params_file)) if args.params_file else json.loads(args.params),
    timeout=args.timeout)
write_json(out / (args.label + '.json'), value)
if args.quiet:
    print(str(out / (args.label + '.json')))
else:
    print(json.dumps(value, ensure_ascii=True))
