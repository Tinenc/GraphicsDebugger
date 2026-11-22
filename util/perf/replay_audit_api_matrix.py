"""Read-only pre-existing demo captures, each in its own isolated regression process."""
from pathlib import Path
import argparse
import traceback
from replay_audit_driver import AuditRun, ROOT, write_json


CAPTURES = {
    'd3d11': 'D:/Study/GitHub/renderdocUltra/output/eid-live-refresh-20260905/api-captures/20260905-135519/D3D11_Simple_Triangle_frame5.rdc',
    'd3d12': 'D:/Study/GitHub/renderdocUltra/output/eid-checkpoints-20260905/d3d12-fixture/captures/20260905-185202/D3D12_Checkpoint_Triangle_frame5.rdc',
    'opengl': 'D:/Study/GitHub/renderdocUltra/output/eid-live-refresh-20260905/api-captures/20260905-135519/GL_Simple_Triangle_frame5.rdc',
    'vulkan': 'D:/Study/GitHub/renderdocUltra/output/eid-live-refresh-20260905/api-captures/20260905-140731/VK_Simple_Triangle_frame5.rdc',
}


def run_api(api, path, suffix, runtime_name='runtime-optimized', inspect_api=False):
    audit = AuditRun('optimized-api-' + api + '-' + suffix, runtime_name, timing=True)
    audit.capture_override = Path(path)
    audit.generic_capture = True
    try:
        audit.launch()
        event = audit.receipt['ready']['generic']['event']
        audit.command('no-checkpoints', 'configure', checkpoints=False)
        status = audit.command('viewer-status', 'viewer_status')
        if not status['textureVisible'] or not status['pipelineVisible']:
            raise RuntimeError('The regression layout must show both viewers')
        a = audit.command('fingerprint-a', 'fingerprint', event=event)
        b = audit.command('fingerprint-b', 'fingerprint', event=event)
        if a['textures'] != b['textures'] or not a['textures']:
            raise RuntimeError('Repeated real replay changed the demo output')
        witness = audit.command('native-thumbnail-witness', 'thumbnail_witness', event=event)
        if not witness['allMatch']:
            raise RuntimeError('GPU preview differs from canonical reference')
        if inspect_api:
            details = audit.command('api-inspector', 'api_inspector_probe', show=True)
            if details['displayedEvent'] != event or not details['rootRows']:
                raise RuntimeError('Deferred API inspector did not display the current event')
        audit.receipt['complete'] = True
    except Exception:
        audit.receipt['error'] = traceback.format_exc()
        raise
    finally:
        if audit.process and audit.process.poll() is None:
            try:
                audit.command('audit-close', 'close', timeout=8)
                audit.process.wait(timeout=15)
            except Exception:
                audit.process.terminate()
                audit.process.wait(timeout=15)
        audit.receipt['exitCode'] = audit.process.returncode if audit.process else None
        audit.save()
        if audit.receipt['exitCode'] != 0:
            raise RuntimeError('Regression process did not exit cleanly: ' + str(audit.receipt['exitCode']))
    return audit.receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suffix', default='002')
    parser.add_argument('--runtime', default='runtime-optimized')
    args = parser.parse_args()
    results = {}
    for api, path in CAPTURES.items():
        print('API', api, flush=True)
        results[api] = run_api(api, path, args.suffix, args.runtime)
    write_json(ROOT / 'output/optimized-api-matrix.json', results)
