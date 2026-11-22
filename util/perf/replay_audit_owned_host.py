"""Call a typed source Host tool against only the verified retained audit instance."""
import argparse
import asyncio
import inspect
import json
import os
from pathlib import Path
import sys
from replay_audit_driver import ROOT, read_json, write_json
sys.path[:0] = [str(ROOT / 'output/tooling'), str(ROOT / 'replaylab/mcp_host')]
import psutil

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('tool')
p.add_argument('--params', default='{}')
p.add_argument('--params-file')
p.add_argument('--label', required=True)
args = p.parse_args()
params = read_json(Path(args.params_file).resolve()) if args.params_file else json.loads(args.params)
owner = read_json(ROOT / 'output/interactive-memory-audit/handoff-verified.json')
process = psutil.Process(owner['pid'])
assert Path(process.exe()).resolve() == Path(owner['executable']).resolve()
assert abs(process.create_time() - owner['creationEpoch']) < .01
environment = process.environ()
for key, value in environment.items():
    if key in ('LOCALAPPDATA', 'APPDATA', 'RENDERDOC_CONFIG_DIR') or key.startswith('REPLAYLAB_'):
        os.environ[key] = value
from src.replaylab_tools import configure_environment
configure_environment(Path(owner['executable']).parent / 'replaylab')
from src import server
value = getattr(server, args.tool)(**params)
if inspect.isawaitable(value):
    value = asyncio.run(value)
value = json.loads(value) if isinstance(value, str) else value
path = ROOT / 'output/memory-latency-audit-20261112' / (args.label + '.json')
path.parent.mkdir(parents=True, exist_ok=True)
write_json(path, value)
print(path)
if isinstance(value, dict) and value.get('error'):
    raise RuntimeError(str(value['error'])[:1000])
