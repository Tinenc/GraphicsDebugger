"""Compare full EID presentation and restored pixels in isolated backend processes."""
import argparse
from pathlib import Path
from replay_audit_driver import AuditRun

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', required=True)
parser.add_argument('--runtime', required=True)
parser.add_argument('--capture', type=Path, required=True)
parser.add_argument('--initial-command-cache', action='store_true',
                    help='Require native hits from a matching experimental initial-command runtime')
parser.add_argument('--force-program-relink', action='store_true')
parser.add_argument('--mutations', action='store_true')
args = parser.parse_args()
audit = AuditRun(args.label, args.runtime, timing=True)
audit.generic_capture = True
audit.capture_override = args.capture.resolve(strict=True)
audit.environment_overrides = {'BREAD_AUDIT_SELECT_LAST_DRAW': '1', 'BREAD_AUDIT_NATIVE_FIXTURE': '1',
    'RENDERDOC_D3D11_ENABLE_INITIAL_COMMAND_CACHE': '1' if args.initial_command_cache else '0',
    'RENDERDOC_GL_FORCE_INITIAL_PROGRAM_RELINK': '1' if args.force_program_relink else '0'}
try:
    audit.launch()
    draws = audit.receipt['ready']['generic']['draws']
    events = list(dict.fromkeys([draws[0], draws[len(draws)//2], draws[-1]]))
    assert len(events) > 1, 'Switch timing requires multiple actual draw EIDs'
    route = events + list(reversed(events[1:-1]))
    audit.command('no-checkpoints', 'configure', checkpoints=False)
    first = audit.command('reference-a', 'fingerprint', event=events[-1], force=True)
    second = audit.command('reference-b', 'fingerprint', event=events[-1], force=True)
    assert first['textures'] == second['textures'], 'Full reference is not deterministic'
    audit.command('warmup', 'navigate', events=events, repeats=2)
    audit.command('navigation', 'navigate', events=route, repeats=8)
    forced = audit.command('forced-native', 'headless', events=events, repeats=10, force=True)
    if args.initial_command_cache:
        assert any('initialCommandCacheHits=' in line for sample in forced['samples']
                   for line in sample['driverLines']), 'Experimental runtime produced no native command hits'
    actual = audit.command('cached-result', 'fingerprint', event=events[-1], force=False)
    assert actual['textures'] == second['textures'], 'Cached output differs from stable full reference'
    audit.command('native-thumbnails', 'thumbnail_expose')
    if args.mutations:
        for name, values in [('first', 1), ('drag120', 120)]:
            result = audit.command('cbuffer-' + name, 'inline_cbuffer', event=events[-1],
                                   settleMs=200, debounceMs=0, dragValues=values)
            assert result['restoredBytes'], 'CBuffer undo did not restore original bytes'
        restored = audit.command('after-undo', 'fingerprint', event=events[-1], force=True)
        assert restored['textures'] == second['textures'], 'CBuffer undo did not restore original pixels'
        audit.command('navigation-after-edit', 'navigate', events=route, repeats=4)
    audit.receipt.update(complete=True, stableReference=True, cachedPixelsMatch=True,
                         finalPixels=second['textures'])
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
assert audit.receipt.get('complete') and audit.receipt['exitCode'] == 0
