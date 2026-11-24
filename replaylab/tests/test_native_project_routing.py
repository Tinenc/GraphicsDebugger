"""Preserve per-window project selection across sanitized vendor processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.native_mcp import build_native_mcp_route
from replaylab_agent.process_env import external_agent_environment
from replaylab_agent.profiles import Profile
from src.active_projects import ActiveReplayProjectService


def test_two_vendor_routes_keep_their_own_project_after_environment_sanitization(tmp_path, monkeypatch):
    global_state = tmp_path / 'global/active-project.json'
    projects = tmp_path / 'projects'
    unrelated = ActiveReplayProjectService(global_state, projects).ensure_pending('Other window')
    host = tmp_path / 'BreadMCP.exe'
    host.write_bytes(b'test-host')
    profile = Profile('codex', 'codex-app-server', options={'allow_unsandboxed_agent': True})
    routes = []
    for instance in ('window-a', 'window-b'):
        state = tmp_path / instance / 'active-project.json'
        expected = ActiveReplayProjectService(state, projects).ensure_pending(instance)
        monkeypatch.setenv('RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE', str(state))
        monkeypatch.setenv('RENDERDOC_ULTRA_PROJECTS_HOME', str(projects))
        monkeypatch.setenv('REPLAYLAB_INSTANCE_ID', 'ambient-instance-must-not-win')
        monkeypatch.setenv('OPENAI_API_KEY', 'not-an-mcp-credential')
        context = ProviderContext(profile, '', instance, {}, None, mcp_executable=host)
        (tmp_path / instance / 'turn').mkdir()
        route = build_native_mcp_route(context, workspace=tmp_path / instance / 'turn',
            event_root=tmp_path / instance / 'events', allowed_effects=('workspace', 'project'))
        assert route.env['REPLAYLAB_INSTANCE_ID'] == instance
        assert route.env['RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE'] == str(state)
        assert 'OPENAI_API_KEY' not in route.env
        routes.append((route, expected))

    # A provider may keep only ordinary OS variables. Its later MCP spawn must
    # still receive the owning Host's routing from the explicit declaration.
    sanitized = external_agent_environment(profile)
    assert 'RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE' not in sanitized
    script = '''import json,os,sys
from replaylab_agent import mcp_proxy
args=mcp_proxy._parser().parse_args(json.loads(sys.argv[1])[3:])
host,host_args,allowed,effects,scope,environment=mcp_proxy._validate_configuration(args)
os.environ.clear();os.environ.update(environment)
from src import server
print(json.dumps({'instance':os.environ['REPLAYLAB_INSTANCE_ID'],
    'state':str(server._ACTIVE_PROJECT_STATE_PATH),
    'project':server._active_project_service().status()['workspaceId']}))
'''
    observed = []
    for route, expected in routes:
        result = subprocess.run([sys.executable, '-c', script, json.dumps(route.args)],
            env={**sanitized, **route.env}, capture_output=True, text=True, encoding='utf-8',
            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout.strip().splitlines()[-1])
        assert data['project'] == expected['workspaceId'] != unrelated['workspaceId']
        assert data['state'] == route.env['RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE']
        assert data['instance'] == route.env['REPLAYLAB_INSTANCE_ID']
        observed.append(data['project'])
    assert len(set(observed)) == 2
