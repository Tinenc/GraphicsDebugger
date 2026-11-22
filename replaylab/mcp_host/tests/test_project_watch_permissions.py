import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

BRIDGE = Path(__file__).resolve().parents[2] / 'renderdoc_mcp_bridge'


def load(name):
    spec = importlib.util.spec_from_file_location('bread_test_' + name, BRIDGE / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_project_stat_watch_detects_atomic_replacement_and_deleted_metadata(tmp_path):
    watch = load('project_watch')
    root = tmp_path / 'project'; (root / '.replaylab').mkdir(parents=True)
    path = root / '.replaylab/workspace.json'; path.write_text('first')
    paths = watch.metadata_paths([{'projectRoot': str(root)}], {'RENDERDOC_ULTRA_PROJECT_REGISTRY': str(tmp_path/'registry.json')})
    baseline = watch.snapshot(paths)
    assert baseline == watch.snapshot(paths)
    pending = path.with_suffix('.pending'); pending.write_text('other')
    timestamp = path.stat().st_mtime_ns
    os.utime(pending, ns=(timestamp, timestamp)); os.replace(pending, path)
    assert watch.snapshot(paths) != baseline
    changed = watch.snapshot(paths); path.unlink()
    assert changed != watch.snapshot(paths)


@pytest.mark.skipif(os.name != 'nt', reason='Windows project permissions')
def test_project_permission_repair_is_bounded_and_preserves_contents(tmp_path):
    permission = load('project_permissions')
    root = tmp_path / 'project'; (root / '.replaylab').mkdir(parents=True)
    manifest = root / '.replaylab/workspace.json'
    manifest.write_text(json.dumps({'schema':'replaylab-project-workspace/v1', 'workspaceId':'a'*32}))
    unrelated = root / 'original.rdc'; unrelated.write_bytes(b'original capture stays untouched')
    before = {p.name:p.read_bytes() for p in (manifest, unrelated)}
    sid = permission.invoking_user_sid()
    executable = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    for inspect in (True, False):
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-EncodedCommand',
            permission.encoded_repair(str(root), 'a'*32, sid, inspect_only=inspect)],
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
        assert before == {p.name:p.read_bytes() for p in (manifest, unrelated)}
    assert subprocess.run([executable, '-NoProfile', '-NonInteractive', '-EncodedCommand',
        permission.encoded_repair(str(root), 'b'*32, sid, inspect_only=True)],
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 12
    with pytest.raises(ValueError): permission.encoded_repair('C:\\', 'a'*32, sid)


def test_loading_permission_failure_repairs_once_and_reloads_without_touching_other_errors(monkeypatch):
    launcher = load('project_launcher')
    denied = {'workspaceId':'a'*32, 'projectRoot':str(Path.cwd()/'registered-project'),
              'unavailable':True, 'unavailableReasonId':'project-access-denied'}
    invalid = dict(denied, workspaceId='b'*32, unavailableReasonId='project-validation-failed')
    from types import SimpleNamespace
    from unittest.mock import Mock
    client = SimpleNamespace(list_projects=Mock(side_effect=[[denied,invalid],[dict(denied,unavailable=False),invalid]]))
    repair = Mock(return_value={'status':'repaired'})
    monkeypatch.setattr(launcher, 'os', SimpleNamespace(name='nt',path=os.path))
    monkeypatch.setattr(launcher._project_permissions, 'repair', repair)
    seen=set();result=launcher._load_projects_with_access_repair(client,seen)
    assert not result[0]['unavailable'] and result[1]['unavailable']
    repair.assert_called_once_with(denied['projectRoot'],denied['workspaceId'],automatic=True)
    client.list_projects=Mock(return_value=[denied])
    launcher._load_projects_with_access_repair(client,seen)
    assert repair.call_count == 1


@pytest.mark.skipif(os.name != 'nt', reason='Windows project permissions')
def test_automatic_owned_project_repair_uses_current_token_without_uac(tmp_path, monkeypatch):
    permission=load('project_permissions')
    root=tmp_path/'project';(root/'.replaylab').mkdir(parents=True)
    path=root/'.replaylab/workspace.json'
    path.write_text(json.dumps({'schema':'replaylab-project-workspace/v1','workspaceId':'a'*32}))
    original=path.read_bytes()
    path.chmod(0o444)
    try:
        result=permission.repair(str(root),'a'*32,automatic=True)
        assert result == {'status':'repaired','elevated':False}
        assert path.read_bytes()==original
        with path.open('ab'): pass
    finally: path.chmod(0o600)
