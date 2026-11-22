"""Saved replay source and unfinished editor source have independent owners."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import project_restore
from test_project_restore_source_admission import RestoreHost, editable, prepare, target


class IsolatedRestoreHost(RestoreHost):
    def __init__(self, target):
        super().__init__(target)
        self.created = []
        self.shown = []
        self.catalog_change = {}
        self.replay_workspace = project_restore._isolated_replay_workspace_path(target.workspace, "1" * 32)
        self.status["source"] = {**editable(target), "currentSha256": "e" * 64,
                                 "modifiedSinceDecompile": True}
        self.draft = target.workspace / "source" / ("shader." + target.language)
        self.draft.parent.mkdir()
        self.draft.write_bytes(b"// unfinished local draft must survive\n")
        self.status["source"]["currentSha256"] = hashlib.sha256(self.draft.read_bytes()).hexdigest()

    def _shader_workspace_service(self):
        return SimpleNamespace(status=self.read_status, create=self.create)

    def _active_capture_catalog_selection(self, event, stage):
        identity = self.target.identity
        key = SimpleNamespace(resource_id=identity["resourceId"], raw_sha256=identity["rawSha256"],
                              stage=stage, entry_point=identity["entryPoint"], catalog_key="exact-original")
        for name, value in self.catalog_change.items():
            setattr(key, name, value)
        return ({"captureSessionId": self.session_id}, self.target.root / "catalog.json",
                SimpleNamespace(capture_id="c" * 64, key=key), None)

    def create(self, catalog, key, workspace, *, editable_format="auto"):
        assert key == "exact-original"
        assert editable_format in ("auto", "hlsl", "glsl")
        assert Path(workspace) == self.replay_workspace
        self.replay_workspace.mkdir()
        self.created.append(workspace)
        status = copy.deepcopy(self.target.status)
        status.update(workspacePath=str(workspace), workspaceId="2" * 32)
        status["source"] = {"present": False}
        status["latestCompiled"] = None
        self.other_status[str(workspace)] = status

    def prepare_shader_workspace(self, event, stage, workspace, **expected):
        assert Path(workspace) == self.replay_workspace
        assert expected["admit_existing"] is True
        self.prepare_calls += 1
        status = self.other_status[str(workspace)]
        status["source"] = editable(self.target)
        source = self.replay_workspace / "source" / ("shader." + self.target.language)
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(self.target.source_bytes)
        return json.dumps({"schema": "renderdoc-ultra-shader-workspace-mcp/v1", "operation": "prepare",
            "result": {"workspacePath": str(workspace),
                "created": {"admittedExisting": True, "sourcePreserved": True},
                "binding": {"workspacePath": str(workspace), "captureId": "c" * 64,
                    "captureSessionId": self.session_id, "eventId": event, "stage": stage,
                    "entryPoint": self.target.identity["entryPoint"],
                    "originalResourceId": self.target.identity["resourceId"],
                    "originalRawSha256": self.target.identity["rawSha256"]}}})

    def _show_saved_shader_workspace_source(self, workspace, **kwargs):
        assert kwargs == {"activate_window": False}
        self.shown.append(workspace)
        return json.dumps({"shown": True})


def knowledge(target):
    return {**target.identity, "knowledgeId": "1" * 32, "sourceSha256": target.source_sha,
            "sourceFormat": target.language,
            "targetProfile": copy.deepcopy(target.status["targetProfile"]),
            "registeredWorkspace": {"path": str(target.workspace), "workspaceId": "b" * 32}}


def isolated(target, host, **kwargs):
    return prepare(target, host, inventory_mode=True, registered_knowledge=knowledge(target),
                   capture_path=target.capture_path, **kwargs)


def test_unvalidated_draft_restores_saved_source_in_an_independent_owner(target):
    host = IsolatedRestoreHost(target)
    before = host.draft.read_bytes()
    draft_status = copy.deepcopy(host.status)
    diagnostic = {}
    workspace, status, _, _ = isolated(target, host, diagnostic=diagnostic)
    assert workspace == host.replay_workspace
    assert status["source"]["currentSha256"] == target.source_sha
    assert host.draft.read_bytes() == before
    assert host.status == draft_status
    assert diagnostic["preservedLocalSource"] == {
        "path": target.workspace.relative_to(Path(target.active["projectRoot"])).as_posix(),
        "workspaceId": "b" * 32}
    assert len(host.created) == host.prepare_calls == 1
    # A second cold restore reuses the independent owner, preserving its cache
    # and source history rather than creating another workspace per reopen.
    candidate = {"sourceSha256": target.source_sha, "revision": 7, "rawSha256": "5" * 64}
    host.other_status[str(workspace)].update(revision=7, latestCompiled=candidate)
    workspace2, status2, capture_id, catalog = isolated(target, host, diagnostic={})
    assert workspace2 == workspace
    assert len(host.created) == 1
    progress = []
    reused = project_restore._compile_reuse_workspace(
        host, target.active, workspace2, status2, target.identity, capture_id, catalog,
        knowledge(target), SimpleNamespace(emit=lambda *args, **kwargs: progress.append(args)), 1, 1)
    assert reused["latestCompiled"] == candidate
    assert progress[0][0] == "shader-candidate-load"


@pytest.mark.parametrize("field,value", [("resource_id", 999), ("raw_sha256", "f" * 64),
                                         ("stage", "vertex"), ("entry_point", "other")])
def test_isolation_cannot_substitute_another_captured_original(target, field, value):
    host = IsolatedRestoreHost(target)
    before = host.draft.read_bytes()
    host.catalog_change[field] = value
    with pytest.raises(RuntimeError, match="catalog changed its original Shader"):
        isolated(target, host)
    assert not host.created and host.prepare_calls == 0
    assert host.draft.read_bytes() == before


def test_include_only_edit_also_preserves_separate_replay_source(target):
    host = IsolatedRestoreHost(target)
    host.status["source"]["currentSha256"] = target.source_sha
    host.status["sourceDependenciesCurrent"] = False
    host.status["latestCompiled"] = {"sourceSha256": target.source_sha}
    diagnostic = {}
    isolated(target, host, diagnostic=diagnostic)
    assert diagnostic["preservedLocalSource"]["workspaceId"] == "b" * 32
    assert host.status["sourceDependenciesCurrent"] is False


def test_cached_replay_hint_reads_current_draft_without_pin_to_previous_text(target):
    host = IsolatedRestoreHost(target)
    diagnostic = {}
    isolated(target, host, diagnostic=diagnostic)
    record = {"identity": target.identity, "managedWorkspace": {"workspaceId": "2" * 32},
              "preservedLocalSource": diagnostic["preservedLocalSource"]}
    first = project_restore._restore_local_source_display(host, target.active,
        Path(target.active["projectRoot"]), target.capture_path, record)
    host.status["source"]["currentSha256"] = "9" * 64
    second = project_restore._restore_local_source_display(host, target.active,
        Path(target.active["projectRoot"]), target.capture_path, record)
    assert first["sourceSha256"] != second["sourceSha256"]
    assert second["sourceSha256"] == "9" * 64 and second["shown"] is True
    assert second["workspacePath"] == str(target.workspace)
    assert host.shown == [str(target.workspace), str(target.workspace)]


def test_display_hint_does_not_admit_another_original_or_owner(target):
    host = IsolatedRestoreHost(target)
    diagnostic = {}
    isolated(target, host, diagnostic=diagnostic)
    record = {"identity": target.identity, "managedWorkspace": {"workspaceId": "2" * 32},
              "preservedLocalSource": diagnostic["preservedLocalSource"]}
    host.status["shader"]["rawSha256"] = "f" * 64
    assert project_restore._restore_local_source_display(host, target.active,
        Path(target.active["projectRoot"]), target.capture_path, record) is None
    assert host.shown == []


def test_existing_applied_replay_owner_is_not_nested_on_retry(target):
    host = IsolatedRestoreHost(target)
    first, _, _, _ = isolated(target, host)
    original_lookup = host.get_active_shader_workspace_path
    def current_owner(*args, **kwargs):
        value = json.loads(original_lookup(*args, **kwargs))
        value["workspacePath"] = str(first)
        return json.dumps(value)
    host.get_active_shader_workspace_path = current_owner
    diagnostic = {}
    second, _, _, _ = isolated(target, host, diagnostic=diagnostic)
    assert second == first
    assert len(host.created) == 1 and host.prepare_calls == 1
    assert diagnostic["preservedLocalSource"]["workspaceId"] == "b" * 32


def test_legacy_record_recovers_local_editor_from_exact_registered_knowledge(target):
    host = IsolatedRestoreHost(target)
    isolated(target, host)
    record = {"identity": target.identity, "managedWorkspace": {
        "workspaceId": "2" * 32,
        "path": host.replay_workspace.relative_to(Path(target.active["projectRoot"])).as_posix()}}
    displayed = project_restore._restore_local_source_display(host, target.active,
        Path(target.active["projectRoot"]), target.capture_path, record, knowledge(target))
    assert displayed["workspacePath"] == str(target.workspace)
    assert displayed["workspaceId"] == "b" * 32 and displayed["shown"] is True


def test_isolated_owner_name_is_bounded_stable_and_distinguishes_owners(tmp_path):
    original = tmp_path / ("a" * 64)
    first = project_restore._isolated_replay_workspace_path(original, "1" * 32)
    assert first.parent == original.parent
    assert len(first.name) == 64 and all(char in "0123456789abcdef" for char in first.name)
    assert first == project_restore._isolated_replay_workspace_path(original, "1" * 32)
    assert first != project_restore._isolated_replay_workspace_path(original, "2" * 32)
    assert first != project_restore._isolated_replay_workspace_path(tmp_path / ("b" * 64), "1" * 32)
    # Moving an otherwise identical project keeps its deterministic owner name.
    moved = project_restore._isolated_replay_workspace_path(tmp_path / "moved" / original.name, "1" * 32)
    assert moved.name == first.name
    compiler_tail = Path("jobs") / ("3" * 32) / "input.hlsl"
    assert len(str(first / compiler_tail)) == len(str(original / compiler_tail))


@pytest.mark.parametrize("legacy_state", ["current", "no-candidate", "changed-original", "changed-include"])
def test_existing_legacy_owner_is_adopted_only_with_current_exact_candidate(target, legacy_state):
    host = IsolatedRestoreHost(target)
    legacy = target.workspace.with_name(target.workspace.name + "-replay-" + "1" * 32)
    legacy.mkdir()
    status = copy.deepcopy(target.status)
    status.update(workspacePath=str(legacy), workspaceId="4" * 32, revision=7,
                  source=editable(target), latestCompiled={
                      "sourceSha256": target.source_sha, "revision": 7, "rawSha256": "5" * 64})
    if legacy_state == "no-candidate":
        status["latestCompiled"] = None
    elif legacy_state == "changed-original":
        status["shader"]["rawSha256"] = "6" * 64
    elif legacy_state == "changed-include":
        status["sourceDependenciesCurrent"] = False
    host.other_status[str(legacy)] = status
    before = copy.deepcopy(status)
    diagnostic = {}
    owner, returned, _, _ = isolated(target, host, diagnostic=diagnostic)
    assert host.other_status[str(legacy)] == before
    if legacy_state == "current":
        assert owner == legacy
        assert returned["latestCompiled"] == before["latestCompiled"]
        assert host.prepare_calls == 0 and host.created == []
    else:
        assert owner == host.replay_workspace
        assert len(host.created) == host.prepare_calls == 1
    assert diagnostic["preservedLocalSource"]["workspaceId"] == "b" * 32
