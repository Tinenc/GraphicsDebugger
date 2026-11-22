"""Owned repeated native compile/apply/debug/rollback and descriptor inspector checks."""
import argparse
import sys
import os
from pathlib import Path
from replay_audit_driver import AuditRun, ROOT

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label',required=True)
parser.add_argument('--runtime',required=True)
parser.add_argument('--capture',required=True)
parser.add_argument('--batches',type=int,default=6)
parser.add_argument('--cycles',type=int,default=10)
parser.add_argument('--heap-trace',action='store_true')
parser.add_argument('--no-timing',action='store_true')
parser.add_argument('--skip-descriptor-scan',action='store_true',
                    help='Use a non-million-heap fixture; descriptor-page coverage is excluded')
parser.add_argument('--unique-shader-variants', action='store_true',
                    help='Compile up to 4096 distinct Shader variants instead of cycling 17')
args=parser.parse_args()
audit=AuditRun(args.label,args.runtime,timing=not args.no_timing)
audit.generic_capture=True
audit.capture_override=Path(args.capture).resolve()
audit.environment_overrides={'BREAD_AUDIT_SELECT_LAST_DRAW':'1','BREAD_AUDIT_NATIVE_FIXTURE':'1'}
heap_session = heap_script = None
try:
    audit.launch()
    audit.command('no-checkpoints','configure',checkpoints=False)
    audit.command('layout','comparison_layout',navigationControls=True)
    draws=audit.receipt['ready']['generic']['draws']
    event=draws[-1]
    reference=audit.command('reference','fingerprint',event=event)
    if not args.skip_descriptor_scan:
        audit.command('sparse-view-scan','descriptor_view_scan')
    for i in range(3):audit.command('warmup-'+str(i),'native_reuse_mutations',variant=i,invalidCompile=True)
    if args.heap_trace:
        import frida
        audit.command('python-heap-start','python_heap_start')
        heap_session=frida.attach(audit.process.pid)
        heap_script=heap_session.create_script((ROOT/'util/perf/replay_audit_heap.js').read_text())
        heap_script.load();heap_script.exports_sync.begin()
    audit.receipt['memoryPhases']=['warmup-2']
    sys.path.insert(0,str(ROOT/'replaylab/mcp_host'))
    import src.ipc_client as ipc
    ipc.INSTANCE_ROOT=str(audit.directory/'local/ReplayLab/ipc-v2/instances')
    instance=next(p.name for p in Path(ipc.INSTANCE_ROOT).iterdir() if p.name.startswith(str(audit.process.pid)+'-'))
    client=ipc.IPCClient(instance_id=instance)
    context=client.call('get_current_renderdoc_context',{})
    client.capture_session_id=context['capture']['captureSessionId']
    for batch in range(args.batches):
        for cycle in range(args.cycles):
            name='mutation-%d-%d'%(batch,cycle)
            result=audit.command(name,'native_reuse_mutations',variant=batch*args.cycles+cycle,
                invalidCompile=True,borrowedBuild=True,uniqueShaderVariants=args.unique_shader_variants)
            assert result['before']['textures']==reference['textures']
            # Exercise the actual file-IPC tool dispatch and native reflection read path.
            state=client.call('get_pipeline_state',{'event_id':event},timeout=20)
            assert isinstance(state,dict) and not state.get('error'),str(state)[:300]
            shader_info=client.call('get_shader_info',{'event_id':event,'stage':'fragment'},timeout=20)
            assert isinstance(shader_info,dict) and shader_info.get('shaderId'),str(shader_info)[:300]
        if args.skip_descriptor_scan:
            audit.command('scan-'+str(batch),'collect_garbage')
        else:
            audit.command('scan-'+str(batch),'descriptor_view_scan')
        audit.receipt['memoryPhases'].append('scan-'+str(batch))
        audit.command('navigation-'+str(batch),'navigate',events=draws[-3:],repeats=2,
                      **({'mode':'set_event'} if args.no_timing else {}))
    final=audit.command('final-pixels','fingerprint',event=event)
    assert final['textures']==reference['textures']
    audit.command('post-gc','collect_garbage')
    audit.command('final-status','status')
    if heap_script:
        from replay_audit_driver import write_json
        # Collect Python wrapper cycles and stop tracemalloc before classifying native survivors.
        # Otherwise retained SWIG array wrappers and the tracer's tables look like native leaks.
        audit.command('python-retained','python_heap_finish')
        write_json(audit.directory/'native-retained.json',heap_script.exports_sync.snapshot(True))
        heap_script.exports_sync.close();heap_session.detach();heap_session=None
    audit.receipt['complete']=True
finally:
    if heap_session:heap_session.detach()
    if audit.process and audit.process.poll() is None:
        try:audit.command('close','close',timeout=10);audit.process.wait(timeout=20)
        except Exception:audit.process.terminate();audit.process.wait(timeout=15)
    audit.receipt['exitCode']=audit.process.returncode if audit.process else None
    audit.save()
assert audit.receipt['exitCode']==0
