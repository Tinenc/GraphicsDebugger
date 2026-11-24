"""Prove cached native-list execution on a deterministic owned D3D12 capture."""
import argparse
from replay_audit_driver import AuditRun, ROOT

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', required=True)
parser.add_argument('--capture', type=str)
parser.add_argument('--timestamps', action='store_true')
parser.add_argument('--runtime', default='runtime-optimized')
parser.add_argument('--checkpoints', action='store_true')
parser.add_argument('--canonical-shader-replay', action='store_true')
args=parser.parse_args()
audit=AuditRun(args.label, args.runtime)
audit.capture_override=ROOT/'output/gpu-reuse-fixture/reuse_capture.rdc'
if args.capture:
    from pathlib import Path
    audit.capture_override=Path(args.capture).resolve()
audit.generic_capture=True
audit.environment_overrides={'BREAD_AUDIT_SELECT_LAST_DRAW':'1', 'BREAD_AUDIT_NATIVE_FIXTURE':'1',
    'RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY':'1',
    'RENDERDOC_D3D12_ENABLE_PREFIX_COMMAND_REUSE':'1'}
if args.timestamps:
    audit.environment_overrides['RENDERDOC_D3D12_ENABLE_PREPARED_TIMESTAMPS']='1'
try:
    audit.launch()
    audit.command('checkpoint-policy','configure',checkpoints=args.checkpoints,
                  budget=512 * 1024 * 1024, maxCheckpoints=3)
    draws=audit.receipt['ready']['generic']['draws']
    audit.command('warmup','navigate',events=draws[1:],repeats=2)
    audit.command('cached-navigation','navigate',events=draws[1:],repeats=3)
    cached = audit.command('cached-pixels','fingerprint',event=draws[-1],force=False,storePixels=True)
    audit.command('reference-config','audit_configuration',environment={
        'RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY':0,
        'RENDERDOC_D3D12_ENABLE_PREFIX_COMMAND_REUSE':0,
        'RENDERDOC_D3D12_DISABLE_PRECISE_DESCRIPTOR_WAITS':1,
        'RENDERDOC_D3D12_DISABLE_DESCRIPTOR_DECODE_CACHE':1})
    reference = audit.command('baseline-pixels','fingerprint',event=draws[-1],force=True,storePixels=True)
    assert cached['textures'] == reference['textures'], 'Native commands changed owned pixels'
    audit.command('candidate-config','audit_configuration',environment={
        'RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY':1,
        'RENDERDOC_D3D12_ENABLE_PREFIX_COMMAND_REUSE':1,
        'RENDERDOC_D3D12_DISABLE_PRECISE_DESCRIPTOR_WAITS':0,
        'RENDERDOC_D3D12_DISABLE_DESCRIPTOR_DECODE_CACHE':0})
    if args.checkpoints:
        state=audit.command('checkpoint-hit-witness','headless',events=[draws[-1]],repeats=2,force=False)
        audit.receipt['checkpointHitsBeforeMutation']=state['checkpoint']['hits']
        if not state['checkpoint']['hits']:
            raise RuntimeError('Fixture did not exercise an actual checkpoint hit')
    audit.command('edit-debug-invalidation','native_reuse_mutations',historyChecks=True,
                  expectUpstreamCheckpoint=args.checkpoints,
                  canonicalShaderReplay=args.canonical_shader_replay)
    audit.command('recache-after-mutations','navigate',events=draws[1:],repeats=3)
    audit.command('recached-pixels','fingerprint',event=draws[-1],force=False)
    audit.command('inline-cbuffer-zero-delay','inline_cbuffer',event=draws[-1],settleMs=200)
    audit.command('native-hlsl-editor','editor_save_routes',timeout=120)
    if args.timestamps:
        audit.command('native-query-results','query_cache_witness')
        audit.command('counter-routes','gpu_counter_routes')
    audit.receipt['complete']=True
finally:
    if audit.process and audit.process.poll() is None:
        try:
            audit.command('close','close',timeout=8)
            audit.process.wait(timeout=15)
        except Exception:
            audit.process.terminate()
            audit.process.wait(timeout=15)
    audit.receipt['exitCode']=audit.process.returncode if audit.process else None
    audit.save()
assert audit.receipt['exitCode'] == 0, 'Owned native-reuse test did not shut down cleanly'
