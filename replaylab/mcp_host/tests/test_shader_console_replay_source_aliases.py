"""Restore-only source owners must not become extra authored Shader documents."""
import copy
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

import test_shader_console as fixtures

console = fixtures.console


def _pair(tmp_path, monkeypatch, snapshot=True):
    capture_path = str((tmp_path / "capture.rdc").resolve())
    binding = console.normalize_capture_binding({"instanceId": "1234-fixture",
        "captureSessionId": "8" * 32, "capturePath": capture_path})
    monkeypatch.setattr(fixtures.ShaderConsoleFixture, "capture_binding",
                        property(lambda unused: copy.deepcopy(binding)))
    fixture = fixtures.ImportedProjectSourceFixture(tmp_path, managed_name="d" * 64)
    editor, project = fixture.editor, fixture.project
    fixture.manifest.update(stage="fragment", entryPoint="MainPS")
    fixture.manifest["capture"]["capturePath"] = capture_path
    editor._write(editor.workspace / "manifest.json", fixture.manifest)
    registry = json.loads(editor.registry.read_bytes())
    for entry in registry["entries"]:
        entry["workspaceIdentity"]["capturePath"] = capture_path
        entry["workspaceIdentitySha256"] = console._canonical_sha256(entry["workspaceIdentity"])
    editor._write(editor.registry, registry)
    for capture in project.replay_value["captures"]:
        capture["capturePath"] = capture_path
    project.write_replay_manifest()
    draft = editor.workspace.with_name("a" * 64)
    shutil.copytree(str(editor.workspace), str(draft))
    manifest = copy.deepcopy(fixture.manifest)
    manifest.update(workspaceId="5" * 32, updatedAt="2026-08-25T04:00:00Z")
    editor._write(draft / "manifest.json", manifest)
    source = draft / "source" / "shader.hlsl"
    source.write_bytes(project.source_text.replace("return 1;", "return 0.1;").encode("utf-8"))
    # Restore recreated the old source much later. Dates must not choose it.
    os.utime(str(editor.source), (2000000000, 2000000000))
    os.utime(str(source), (1000000000, 1000000000))
    monkeypatch.setattr(console, "default_active_project_selection_path", lambda: str(project.selection))
    record = {
        "knowledgeId": project.knowledge_id, "sourceSha256": project.source_sha256,
        "managedWorkspace": {"workspaceId": editor.WORKSPACE_ID,
                             "path": editor.workspace.relative_to(project.project_root).as_posix()},
        "preservedLocalSource": {"workspaceId": manifest["workspaceId"],
                                 "path": draft.relative_to(project.project_root).as_posix()},
        "identity": {"resourceId": 101, "rawSha256": project.RAW_SHA256,
                     "stage": "fragment", "entryPoint": "MainPS"},
    }
    payload = {"schema": "replaylab-verified-rdc-restore/v1", "schemaVersion": 2,
               "projectId": project.REPLAY_PROJECT_ID, "projectWorkspaceId": project.PROJECT_WORKSPACE_ID,
               "capturePath": editor.capture_binding["capturePath"], "shaders": [record]}
    snapshot_path = project.replay_root / "derived" / "validated-rdc-restores" / (
        hashlib.sha256(console._normal_path(payload["capturePath"]).encode("utf-8")).hexdigest() + ".json")
    if snapshot:
        snapshot_path.parent.mkdir(parents=True)
        editor._write(snapshot_path, project._seal(payload, "sealSha256"))
    return fixture, draft, payload, snapshot_path


def _models(fixture, draft):
    editor = fixture.editor
    models = console.load_console_snapshot(str(editor.registry), capture_binding=editor.capture_binding)
    return models["workspaces"] + models["alternateWorkspaces"]


def test_saved_replay_owner_aliases_to_preserved_latest_source(tmp_path, monkeypatch):
    fixture, draft, payload, snapshot_path = _pair(tmp_path, monkeypatch)
    editor = fixture.editor
    before = {str(path): path.read_bytes() for path in (editor.source, draft / "source/shader.hlsl", snapshot_path)}
    snapshot = console.load_console_snapshot(str(editor.registry), capture_binding=editor.capture_binding)
    assert not snapshot["errors"]
    assert [item["workspaceId"] for item in snapshot["workspaces"]] == ["5" * 32]
    canonical = snapshot["workspaces"][0]
    assert canonical["projectSourceIdentity"] is None
    assert canonical["projectSourceOrigin"] is not None  # locally edited imported draft remains current
    assert canonical["sourceSha256"] == hashlib.sha256(before[str(draft / "source/shader.hlsl")]).hexdigest()
    assert snapshot["documentAliases"] == {editor.WORKSPACE_ID: "5" * 32}
    replay = snapshot["alternateWorkspaces"][0]
    assert replay["internalReplaySource"] is True
    assert replay["canonicalSourceWorkspaceId"] == "5" * 32
    assert not canonical.get("relatedWorkspaceBindings")
    assert {key: Path(key).read_bytes() for key in before} == before


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "opengl", "opengles", "vulkan"])
def test_alias_identity_is_api_independent(tmp_path, monkeypatch, api):
    fixture, draft, payload, unused = _pair(tmp_path, monkeypatch)
    models = _models(fixture, draft)
    for model in models:
        model["captureApi"] = api
    library = console.load_project_shader_library_snapshot(str(fixture.project.selection))
    aliases, unused = console._replay_source_document_aliases(models, fixture.editor.capture_binding, library)
    assert aliases == {fixture.editor.WORKSPACE_ID: "5" * 32}


@pytest.mark.parametrize("field,value", [
    ("resourceId", 102), ("shaderRawSha256", "0" * 64), ("stage", "vertex"),
    ("entryPoint", "OtherPS"), ("capturePath", "/unrelated/capture.rdc"),
    ("captureApi", "opengl"), ("targetProfile", {"profile": "ps_5_0"}),
])
def test_identical_filename_or_program_never_merges_other_original(tmp_path, monkeypatch, field, value):
    fixture, draft, payload, unused = _pair(tmp_path, monkeypatch)
    models = _models(fixture, draft)
    next(item for item in models if item["workspaceId"] == "5" * 32)[field] = value
    library = console.load_project_shader_library_snapshot(str(fixture.project.selection))
    aliases, unused = console._replay_source_document_aliases(models, fixture.editor.capture_binding, library)
    assert not aliases


@pytest.mark.parametrize("change", ["changed-source", "newer-head", "bad-seal", "wrong-project", "wrong-path", "malformed-identity", "duplicate-id"])
def test_stale_or_ambiguous_restore_metadata_cannot_hide_source(tmp_path, monkeypatch, change):
    fixture, draft, payload, snapshot_path = _pair(tmp_path, monkeypatch)
    models = _models(fixture, draft)
    library = console.load_project_shader_library_snapshot(str(fixture.project.selection))
    replay = next(item for item in models if item["workspaceId"] == fixture.editor.WORKSPACE_ID)
    if change == "changed-source":
        replay["sourceSha256"] = "0" * 64
        replay["projectSourceIdentity"] = None
    elif change == "newer-head":
        for item in library["documents"]:
            item["isCurrentHead"] = False
    elif change == "duplicate-id":
        models.append(copy.deepcopy(models[0]))
    else:
        if change == "wrong-project":
            payload["projectId"] = "0" * 32
        elif change == "wrong-path":
            payload["shaders"][0]["preservedLocalSource"]["path"] = "../outside"
        elif change == "malformed-identity":
            payload["shaders"][0]["identity"] = []
        sealed = fixture.project._seal(payload, "sealSha256")
        if change == "bad-seal":
            sealed["sealSha256"] = "0" * 64
        fixture.editor._write(snapshot_path, sealed)
    aliases, unused = console._replay_source_document_aliases(models, fixture.editor.capture_binding, library)
    assert not aliases


@pytest.mark.parametrize("legacy", [False, True])
def test_generated_replay_owner_is_recognized_without_optional_snapshot(tmp_path, monkeypatch, legacy):
    fixture, draft, payload, unused = _pair(tmp_path, monkeypatch, snapshot=False)
    models = _models(fixture, draft)
    replay = next(item for item in models if item["workspaceId"] == fixture.editor.WORKSPACE_ID)
    name = (draft.name + "-replay-" + fixture.project.knowledge_id if legacy else
            console._canonical_sha256({"domain": "replaylab-isolated-replay-workspace/v1",
                "originalWorkspace": draft.name, "knowledgeId": fixture.project.knowledge_id}))
    replay["workspacePath"] = str(draft.with_name(name))
    library = console.load_project_shader_library_snapshot(str(fixture.project.selection))
    aliases, unused = console._replay_source_document_aliases(models, fixture.editor.capture_binding, library)
    assert aliases == {fixture.editor.WORKSPACE_ID: "5" * 32}


def test_same_original_without_restore_provenance_remains_independent(tmp_path, monkeypatch):
    fixture, draft, payload, unused = _pair(tmp_path, monkeypatch, snapshot=False)
    models = _models(fixture, draft)
    library = console.load_project_shader_library_snapshot(str(fixture.project.selection))
    aliases, unused = console._replay_source_document_aliases(models, fixture.editor.capture_binding, library)
    assert not aliases
