"""Read-only multi-capture EID/presentation and cache-vs-reference regression."""
import argparse
import hashlib
from pathlib import Path
import traceback
from replay_audit_driver import AuditRun, ROOT, write_json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', required=True)
parser.add_argument('--runtime', required=True)
parser.add_argument('--capture', type=Path, action='append', required=True)
args = parser.parse_args()
results = []
for index, capture in enumerate(args.capture):
    capture = capture.resolve(strict=True)
    audit = AuditRun(args.label + '-' + str(index), args.runtime, timing=False)
    audit.generic_capture = True
    audit.capture_override = capture
    audit.environment_overrides = {'BREAD_AUDIT_SELECT_LAST_DRAW': '1'}
    try:
        audit.launch()
        draws = audit.receipt['ready']['generic']['draws']
        events = list(dict.fromkeys([draws[max(0, len(draws)//2)], *draws[-3:]]))
        audit.command('warmup', 'navigate', events=events, repeats=2)
        audit.command('navigation', 'navigate', events=list(reversed(events)) + events, repeats=3)
        reference_a = audit.command('reference-a', 'fingerprint', event=events[-1], force=True)
        reference_b = audit.command('reference-b', 'fingerprint', event=events[-1], force=True)
        audit.command('recache', 'navigate', events=events, repeats=2)
        actual = audit.command('cached-result', 'fingerprint', event=events[-1], force=False)
        stable = reference_a['textures'] == reference_b['textures']
        matches = actual['textures'] == reference_b['textures']
        if stable and not matches:
            raise RuntimeError('Stable full reference differs from cached replay')
        audit.command('native-preview', 'thumbnail_expose')
        audit.receipt.update(complete=True, referenceStable=stable, cachedPixelsMatch=matches,
                             referenceStabilityRequiredForExactClaim=True)
    except Exception:
        audit.receipt['error'] = traceback.format_exc()
        raise
    finally:
        if audit.process and audit.process.poll() is None:
            try:
                audit.command('close', 'close', timeout=10)
                audit.process.wait(timeout=20)
            except Exception:
                audit.process.terminate()
                audit.process.wait(timeout=20)
        audit.receipt['exitCode'] = audit.process.returncode if audit.process else None
        audit.save()
        results.append(audit.receipt)
        write_json(ROOT / 'output' / (args.label + '.json'), results)
    assert audit.receipt['exitCode'] == 0
print('Read-only capture regressions completed:', len(results))
