"""Run a bounded file-protocol diagnostic on the verified audit instance."""
import argparse
from pathlib import Path
import sys
import time
import uuid
import json
from replay_audit_driver import ROOT, read_json, write_json
sys.path.insert(0, str(ROOT / 'output/tooling'))
import psutil

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('operation')
p.add_argument('--params', default='{}')
p.add_argument('--label', required=True)
p.add_argument('--timeout', type=float, default=180)
args = p.parse_args()
owner = read_json(ROOT / 'output/interactive-memory-audit/handoff-verified.json')
process = psutil.Process(owner['pid'])
assert Path(process.exe()).resolve() == Path(owner['executable']).resolve()
assert abs(process.create_time() - owner['creationEpoch']) < .01
directory = Path(process.environ()['BREAD_AUDIT_RUN'])
out = ROOT / 'output/rt-cache-audit-20261112'
out.mkdir(exist_ok=True)
token = args.label + '-' + uuid.uuid4().hex[:10]
log = Path(owner['executable']).parent / 'eid_timing_driver.log'
offset = log.stat().st_size if log.exists() else 0
before = process.memory_info()
start = time.perf_counter()
write_json(directory / 'command.json', dict(json.loads(args.params), id=token, operation=args.operation))
path = directory / ('result-' + token + '.json')
while not path.exists():
    if time.perf_counter() - start > args.timeout:
        raise TimeoutError(token)
    time.sleep(.025)
value = read_json(path)
write_json(out / (args.label + '.json'), value)
after = process.memory_info()
write_json(out / (args.label + '-envelope.json'), {'pid': process.pid,
           'observedWallMs': (time.perf_counter() - start) * 1000,
           'beforePrivateBytes': before.private, 'afterPrivateBytes': after.private,
           'workingSetBytes': after.rss, 'handles': process.num_handles(), 'threads': process.num_threads()})
if log.exists():
    with log.open('rb') as f:
        f.seek(offset)
        (out / (args.label + '-driver.log')).write_bytes(f.read())
print(args.label, 'ok=' + str(value.get('ok')))
if not value.get('ok'):
    raise RuntimeError(value.get('error'))
