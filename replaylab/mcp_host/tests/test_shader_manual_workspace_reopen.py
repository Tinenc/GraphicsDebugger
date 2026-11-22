"""A published working copy stays visible when its import receipt names an old head."""
import copy
from unittest import mock

import pytest

from test_shader_console import console


@pytest.fixture
def published_source(tmp_path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "shaders" / "active"
    workspace.mkdir(parents=True)
    binding = console.normalize_capture_binding({
        "instanceId": "instance-current", "captureSessionId": "1" * 32,
        "capturePath": str(tmp_path / "fixture.rdc")})
    origin = {"projectId": "2" * 32, "knowledgeId": "3" * 32,
              "originalRawSha256": "a" * 64, "sourceSha256": "b" * 64,
              "stage": "fragment", "entryPoint": "main"}
    model = {
        "workspaceId": "4" * 32, "workspacePath": str(workspace),
        "sourcePath": str(workspace / "shader.hlsl"), "resourceId": 77,
        "shaderRawSha256": origin["originalRawSha256"], "sourceSha256": origin["sourceSha256"],
        "stage": "fragment", "entryPoint": "main", "captureBinding": binding,
        "capturePath": binding["capturePath"], "projectSourceIdentity": dict(origin),
        "projectSourceOrigin": dict(origin), "editorEligible": True,
        "editorEligibility": "managed-source", "autoApplyReady": False,
        "liveEdit": {"activeRevision": 31, "state": "running", "sessionState": "current"},
        "authorizedAt": "2026-01-01", "updatedAt": "2026-01-01",
    }
    head = {"knowledgeId": "5" * 32, "isCurrentHead": True,
            "rawShaderSha256": origin["originalRawSha256"], "sourceSha256": origin["sourceSha256"],
            "stage": "fragment", "entryPoint": "main",
            "compiledCandidateValidationScope": "manual-edit-unvalidated"}
    library = {"available": True, "projectId": origin["projectId"], "projectRoot": str(project),
               "documents": [{"knowledgeId": origin["knowledgeId"], "isCurrentHead": False}, head]}
    return model, library, binding


def snapshot(tmp_path, published_source, models=None):
    model, library, binding = published_source
    models = models or [model]
    authorizations = [{"workspacePath": row["workspacePath"], "index": index}
                      for index, row in enumerate(models)]
    with mock.patch.object(console, "_load_generic_console_entries", return_value={
            "entries": [], "errors": [], "watchedPaths": []}), \
            mock.patch.object(console, "_managed_project_draft_authorizations",
                              return_value=(authorizations, [])), \
            mock.patch.object(console, "_workspace_model", side_effect=lambda auth, unused:
                              copy.deepcopy(models[auth["index"]])), \
            mock.patch.object(console, "_project_library_for_console", return_value=library):
        return console.load_console_snapshot(str(tmp_path / "absent-registry.json"), binding)


def test_reopen_keeps_same_bytes_after_manual_publication_without_rewriting_import_receipt(
        tmp_path, published_source):
    model, unused_library, unused_binding = published_source
    before = copy.deepcopy(model)
    result = snapshot(tmp_path, published_source)
    assert result["errors"] == []
    assert len(result["workspaces"]) == 1
    shown = result["workspaces"][0]
    assert shown["workspaceId"] == model["workspaceId"]
    assert shown["projectSourceIdentity"] == before["projectSourceIdentity"]
    assert shown["projectSourceOrigin"] == before["projectSourceOrigin"]
    assert shown["autoApplyReady"] is False
    assert model == before


def test_changed_source_published_as_current_head_remains_visible(tmp_path, published_source):
    model, library, unused_binding = published_source
    model["projectSourceIdentity"] = None
    model["sourceSha256"] = library["documents"][-1]["sourceSha256"] = "c" * 64
    result = snapshot(tmp_path, published_source)
    assert [row["workspaceId"] for row in result["workspaces"]] == [model["workspaceId"]]
    assert result["workspaces"][0]["projectSourceIdentity"] is None


@pytest.mark.parametrize("field,value", [
    ("rawShaderSha256", "d" * 64), ("stage", "vertex"),
    ("entryPoint", "other"), ("sourceSha256", "e" * 64),
])
def test_equal_source_or_name_never_promotes_a_different_head(tmp_path, published_source, field, value):
    model, library, unused_binding = published_source
    library["documents"][-1][field] = value
    result = snapshot(tmp_path, published_source)
    assert not result["workspaces"]
    assert result["alternateWorkspaces"][0]["historicalSource"] is True
    assert not result["documentAliases"]


def test_ambiguous_current_heads_never_choose_a_winner(tmp_path, published_source):
    unused_model, library, unused_binding = published_source
    library["documents"].append(dict(library["documents"][-1], knowledgeId="6" * 32))
    assert not snapshot(tmp_path, published_source)["workspaces"]


def test_current_active_workspace_wins_exact_source_deduplication(tmp_path, published_source):
    active, library, unused_binding = published_source
    imported = copy.deepcopy(active)
    imported.update(workspaceId="7" * 32, workspacePath=str(
        tmp_path / "project" / "workspaces" / "shaders" / "new-import"),
        authorizedAt="2026-02-01", updatedAt="2026-02-01", autoApplyReady=True,
        liveEdit={"activeRevision": 0})
    imported["projectSourceIdentity"]["knowledgeId"] = library["documents"][-1]["knowledgeId"]
    result = snapshot(tmp_path, published_source, [imported, active])
    assert [row["workspaceId"] for row in result["workspaces"]] == [active["workspaceId"]]
    assert result["documentAliases"][imported["workspaceId"]] == active["workspaceId"]
    assert result["alternateWorkspaces"][0]["workspaceId"] == imported["workspaceId"]


@pytest.mark.parametrize("mismatch", ["project", "workspace-root", "origin-shader"])
def test_presentation_match_never_crosses_project_or_original_provenance(
        tmp_path, published_source, mismatch):
    model, library, unused_binding = published_source
    if mismatch == "project":
        library["projectId"] = "8" * 32
    elif mismatch == "workspace-root":
        model["workspacePath"] = str(tmp_path / "other-project" / "shader")
    else:
        model["projectSourceIdentity"]["originalRawSha256"] = "f" * 64
    assert console._current_project_source_head(model, library) is None
