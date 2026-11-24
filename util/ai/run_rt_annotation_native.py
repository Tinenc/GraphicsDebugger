"""Exercise the production composer with an existing native-agent connection.

Uses an owned desktop/profile and the connection's normal installed SDK. No
credentials or user conversation files are copied into the fixture.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'replaylab/src'))
sys.path.insert(0, str(ROOT/'replaylab/mcp_host'))
from replaylab_agent.profiles import ProfileStore, PROFILE_SCHEMA
from src.replaylab_tools import configure_environment, TOOLS_ENV, ROUTES_ENV


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--reuse-native-account', action='store_true',
                        help='Use the normal Bread provider state; keep test UI/history isolated')
    args = parser.parse_args()
    profile = ProfileStore().load().profiles[args.profile]
    if profile.provider.kind != 'agent':
        raise ValueError('Choose an existing native-agent connection')
    profile.resolve_model(args.model)
    root = Path(tempfile.mkdtemp(prefix='bread-rt-annotation-native-'))
    for folder in ('config', 'local', 'roaming/ReplayLab/ai'):
        (root/folder).mkdir(parents=True)
    clone = asdict(profile)
    clone.update(model=args.model, default_model=args.model, models=[args.model])
    (root/'roaming/ReplayLab/ai/profiles.json').write_text(json.dumps({
        'schema': PROFILE_SCHEMA, 'activeProfile': profile.id, 'profiles': [clone]}), encoding='utf-8')
    (root/'config/UI.config').write_text(json.dumps({'rdocConfigData': 1,
        'UILanguage': 'zh_CN', 'Analytics_TotalOptOut': True,
        'CheckUpdate_AllowChecks': False, 'Tips_HasSeenFirst': True}), encoding='utf-8')
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    spec.update(capture=str(args.capture.resolve(strict=True)), profile=profile.id,
                model=args.model, eventsRoot=str(root/'local/ReplayLab/ai-tasks'),
                timeout=args.timeout-30)
    if args.reuse_native_account:
        spec['eventsRoot']=str(Path(os.environ['LOCALAPPDATA'])/'ReplayLab/ai-tasks')
    if spec.get('projectPath'):
        source = Path(os.environ['LOCALAPPDATA'])/'ReplayLab/config'
        state = json.loads((source/'active-project.json').read_text(encoding='utf-8'))
        if Path(state['projectRoot']).resolve() != Path(spec['projectPath']).resolve():
            raise ValueError('Scenario project differs from the selected project')
        target = root/'local/ReplayLab/config'
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/'active-project.json', target)
        registry = json.loads((source/'project-registry.json').read_text(encoding='utf-8'))
        registry['projects'] = [p for p in registry['projects'] if p['workspaceId'] == state['workspaceId']]
        if len(registry['projects']) != 1:
            raise ValueError('Project registration is not unique')
        registry.pop('registrySha256', None)
        registry['registrySha256'] = hashlib.sha256(json.dumps(registry, ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        (target/'project-registry.json').write_text(json.dumps(registry, ensure_ascii=False), encoding='utf-8')
    owned = dict(os.environ)
    if not args.reuse_native_account:
        owned['LOCALAPPDATA'] = str(root/'local')
    owned.pop(TOOLS_ENV, None)
    owned.pop(ROUTES_ENV, None)
    tools = configure_environment(args.app.resolve().parent/'replaylab', owned)
    if tools.get('error'):
        raise RuntimeError('Owned Shader tools are unavailable: '+str(tools['error']))
    (root/'spec.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding='utf-8')
    launch = dict(schema='bread-native-annotation-launch/v1', run=str(root), model=args.model,
                  profile=profile.id, app=str(args.app.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(launch, indent=2), encoding='utf-8')
    print(json.dumps(launch), flush=True)
    command=[sys.executable, '-X', 'utf8', str(ROOT/'util/ai/run_visual_reply_history.py'),
        '--app', str(args.app.resolve()), '--run', str(root), '--ui-script',
        str(ROOT/'replaylab/tests/fixtures/qrenderdoc_rt_annotation_live.py'),
        '--result-name', 'ui-result.json', '--bundled-agent', '--timeout', str(args.timeout)]
    if args.reuse_native_account:
        command.append('--use-user-local-data')
    result = subprocess.run(command)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
