import json
from pathlib import Path

from tests.test_shader_console import ProjectShaderLibraryFixture, console


class SecondProject(ProjectShaderLibraryFixture):
    PROJECT_WORKSPACE_ID = 'b'*32
    REPLAY_PROJECT_ID = 'c'*32
    SOURCE_WORKSPACE_ID = 'd'*32


def test_two_private_selections_read_one_shared_registry_without_crossing_projects(tmp_path, monkeypatch):
    first = ProjectShaderLibraryFixture(tmp_path/'first')
    second = SecondProject(tmp_path/'second')
    shared = tmp_path/'shared/project-registry.json'; shared.parent.mkdir()
    registry = dict(first.registry_value)
    registry.pop('registrySha256')
    registry['projects'] += second.registry_value['projects']
    registry = first._seal(registry, 'registrySha256')
    first._write(shared, registry)
    monkeypatch.setenv('RENDERDOC_ULTRA_PROJECT_REGISTRY', str(shared))
    selections = []
    for index, fixture in enumerate((first, second)):
        private = tmp_path/('instance%d'%index)/'active-project.json'; private.parent.mkdir()
        private.write_bytes(fixture.selection.read_bytes()); selections.append(private)
        result = console.load_project_shader_library_snapshot(str(private))
        assert result['available'] and not result['errors'], result['errors']
        assert result['projectWorkspaceId'] == fixture.PROJECT_WORKSPACE_ID
        assert result['projectRoot'] == str(fixture.project_root)
        assert len(result['documents']) == 1
        assert any(console._same_path(str(shared), p) for p in result['watchedPaths'])
        assert not (private.parent/'project-registry.json').exists()
    assert selections[0].read_bytes() != selections[1].read_bytes()


def test_missing_explicit_registry_is_diagnosed_and_watched_without_wrong_fallback(tmp_path, monkeypatch):
    fixture = ProjectShaderLibraryFixture(tmp_path)
    missing = tmp_path/'shared/project-registry.json'
    monkeypatch.setenv('RENDERDOC_ULTRA_PROJECT_REGISTRY', str(missing))
    monkeypatch.setenv('RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE', str(fixture.selection))
    binding = {'instanceId':'own-instance','captureSessionId':'1'*32,'capturePath':str(tmp_path/'capture.rdc')}
    result = console.load_console_snapshot(str(tmp_path/'empty-live.json'), capture_binding=binding)
    assert result['sourceDiscoveryErrors']
    assert any(console._same_path(str(missing), p) for p in result['watchedPaths'])
    assert not result['workspaces']
    missing.parent.mkdir(); missing.write_bytes(fixture.registry.read_bytes())
    recovered = console.load_console_snapshot(str(tmp_path/'empty-live.json'), capture_binding=binding)
    assert not recovered['sourceDiscoveryErrors']
