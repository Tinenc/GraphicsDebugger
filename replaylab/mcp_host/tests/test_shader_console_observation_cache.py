"""Display snapshots reuse validation without turning metadata into authority."""
import copy
import os
from unittest import mock

import pytest

from test_shader_console import (
    ImportedProjectSourceFixture, ProjectShaderLibraryFixture, console,
)


def test_project_library_cache_reuses_validation_but_reads_exact_inputs(tmp_path):
    fixture = ProjectShaderLibraryFixture(tmp_path, compiled_candidate=True)
    cache = console._ObservedProjectLibraryCache()
    with mock.patch.object(console, "_load_project_shader_library",
                           wraps=console._load_project_shader_library) as validate:
        original = cache.load(str(fixture.selection))
        dependencies = copy.deepcopy(cache.entry[1])
        with mock.patch.object(console, "_read_file_bytes",
                               wraps=console._read_file_bytes) as reads:
            cached = cache.load(str(fixture.selection))
        assert validate.call_count == 1
        assert cached == original
        assert {call.args[0] for call in reads.call_args_list} == set(dependencies["files"])
        cached["documents"].clear()
        assert len(cache.load(str(fixture.selection))["documents"]) == 1


@pytest.mark.parametrize("artifact", ["source", "compiled"])
def test_observation_cache_detects_changed_bytes_even_with_identical_metadata(tmp_path, artifact):
    fixture = ProjectShaderLibraryFixture(tmp_path, compiled_candidate=True)
    cache = console._ObservedProjectLibraryCache()
    result = cache.load(str(fixture.selection))
    path = (fixture.source if artifact == "source" else
            tmp_path.__class__(result["documents"][0]["compiledCandidateArtifactPath"]))
    # Defeat the metadata fast rejection intentionally; a cache hit must still
    # verify file content, rather than rely on modification times or size.
    stamps = dict(cache.entry[1]["paths"])
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with mock.patch.object(console, "_observation_path_stamp",
                           side_effect=lambda p: stamps.get(os.path.abspath(str(p)))):
        with pytest.raises(console.ShaderConsoleError, match="hash|fingerprint"):
            cache.load(str(fixture.selection))
    assert cache.entry is None


def test_observation_cache_rechecks_removed_input_and_redirected_parent(tmp_path):
    fixture = ProjectShaderLibraryFixture(tmp_path)
    cache = console._ObservedProjectLibraryCache()
    cache.load(str(fixture.selection))
    source = fixture.source.read_bytes()
    fixture.source.unlink()
    with pytest.raises(console.ShaderConsoleError, match="unavailable"):
        cache.load(str(fixture.selection))
    fixture.source.write_bytes(source)
    cache.load(str(fixture.selection))
    original = console._is_reparse
    with mock.patch.object(console, "_is_reparse", side_effect=lambda p: (
            os.path.abspath(str(p)) == str(fixture.source.parent.resolve()) or original(p))), \
            mock.patch.object(console, "_observation_path_stamp", return_value=None):
        with pytest.raises(console.ShaderConsoleError, match="redirected"):
            cache.load(str(fixture.selection))


def test_console_projection_cache_never_skips_strict_document_open(tmp_path):
    fixture = ImportedProjectSourceFixture(tmp_path)
    fixture.install_graph()
    editor = fixture.editor
    with mock.patch.object(console, "_observed_project_library", console._ObservedProjectLibraryCache()), \
            mock.patch.object(console, "default_active_project_selection_path",
                              return_value=str(fixture.project.selection)), \
            mock.patch.object(console, "_load_project_shader_library",
                              wraps=console._load_project_shader_library) as validate:
        first = console.load_console_snapshot(str(editor.registry), editor.capture_binding)
        second = console.load_console_snapshot(str(editor.registry), editor.capture_binding)
        assert first["workspaces"] == second["workspaces"]
        assert validate.call_count == 1
        assert getattr(console._console_read_scope, "library_memo", None) is None
        console.load_authorized_source_document(
            str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID,
            capture_binding=editor.capture_binding)
        assert validate.call_count > 1
        fixture.project.source.write_text("tampered", encoding="utf8")
        refreshed = console.load_console_snapshot(str(editor.registry), editor.capture_binding)
        assert not refreshed["workspaces"][0]["editorEligible"]


def test_optional_missing_graph_is_reloaded_when_created(tmp_path):
    fixture = ImportedProjectSourceFixture(tmp_path)
    cache = console._ObservedProjectLibraryCache()
    initial = cache.load(str(fixture.project.selection))
    assert initial["documents"][0]["flowGraph"] is None
    graph = fixture.install_graph()
    latest = cache.load(str(fixture.project.selection))
    assert latest["documents"][0]["flowGraph"]["graphId"] == graph["graphId"]


def test_display_batch_scans_drafts_once_per_capture_without_retaining_authority(tmp_path):
    binding = {"capturePath": str(tmp_path/'a.rdc'), "captureSessionId": "a"*32}
    other = dict(binding, captureSessionId="b"*32)
    result = ([{"workspaceId":"c"*32}], [str(tmp_path/'source')])
    with mock.patch.object(console, '_managed_project_draft_authorizations_uncached',
                           return_value=result) as scan:
        with console.console_read_batch():
            first = console._managed_project_draft_authorizations(binding)
            first[0][0]['workspaceId'] = 'mutated'
            with console.console_read_batch():
                second = console._managed_project_draft_authorizations(binding)
            assert second[0][0]['workspaceId'] == 'c'*32
            console._managed_project_draft_authorizations(other)
            assert scan.call_count == 2
        console._managed_project_draft_authorizations(binding)
        assert scan.call_count == 3
    assert not hasattr(console._console_read_scope, 'draft_memo')


def test_private_selection_parent_heartbeat_does_not_invalidate_display(tmp_path, monkeypatch):
    fixture = ProjectShaderLibraryFixture(tmp_path/'project')
    private = tmp_path/'ipc-instance'; private.mkdir()
    selection = private/'active-project.json';selection.write_bytes(fixture.selection.read_bytes())
    monkeypatch.setenv('RENDERDOC_ULTRA_PROJECT_REGISTRY',str(fixture.registry))
    value = console.load_project_shader_library_snapshot(str(selection))
    assert not value['errors']
    assert str(selection) in value['watchedPaths']
    assert str(private) not in value['watchedPaths']
    before = console._console_paths_signature(value['watchedPaths'])
    (private/'instance.json').write_text('heartbeat',encoding='utf-8')
    assert console._console_paths_signature(value['watchedPaths']) == before
    original = selection.read_bytes();selection.unlink()
    assert console._console_paths_signature(value['watchedPaths']) != before
    selection.write_bytes(original)
    recovered = console.load_project_shader_library_snapshot(str(selection))
    assert recovered['available'] and not recovered['errors']


def test_known_locator_reads_exact_managed_leaf_without_directory_search(tmp_path, monkeypatch):
    from test_shader_console import ShaderConsoleFixture
    fixture=ShaderConsoleFixture(tmp_path/'managed',workspace_name='a'*64)
    monkeypatch.setattr(console,'_validate_active_project_selection',lambda path:{
        'path':str(tmp_path/'selection.json'),'projectRoot':str(fixture.root)})
    monkeypatch.setattr(console,'_validate_active_project_workspace',lambda selection:{})
    monkeypatch.setattr(console,'_project_relative_path',lambda *args:str(fixture.root))
    monkeypatch.setattr(console.os,'scandir',mock.Mock(side_effect=AssertionError('Known source must not search')))
    with console.console_read_batch(),console.known_source_locator(str(fixture.workspace)):
        records,paths=console._managed_project_draft_authorizations(fixture.capture_binding)
    assert len(records)==1 and records[0]['workspaceId']==fixture.WORKSPACE_ID
    foreign=tmp_path/'foreign'/('b'*64);foreign.mkdir(parents=True)
    with console.console_read_batch(),console.known_source_locator(str(foreign)):
        assert console._managed_project_draft_authorizations(fixture.capture_binding)[0]==[]
