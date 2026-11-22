"""One visible published Shader across old GLSL and newer HLSL heads."""
import copy
from types import SimpleNamespace
from unittest import mock

import pytest

import test_shader_console as fixtures
from test_shader_workspace_display_aliases import duplicate_panel

console = fixtures.console


def _models(tmp_path, api="vulkan"):
    fixture = SimpleNamespace(registry=tmp_path / "empty-registry.json",
        capture_binding=console.normalize_capture_binding(dict(instanceId="1234-fixture",
            captureSessionId="8" * 32, capturePath=str(tmp_path / "capture.rdc"))))
    models, heads = [], {}
    for index, language in enumerate(("hlsl", "glsl")):
        workspace_id, knowledge_id, source_sha = str(index + 1) * 32, str(index + 3) * 32, str(index + 5) * 64
        identity = dict(projectId="9" * 32, knowledgeId=knowledge_id,
                        originalRawSha256="a" * 64, stage="fragment", entryPoint="main", sourceSha256=source_sha)
        models.append(dict(workspaceId=workspace_id, workspacePath=str(tmp_path / language),
            sourcePath=str(tmp_path / language / ("shader." + language)), sourceSha256=source_sha,
            captureApi=api, capturePath=fixture.capture_binding["capturePath"], captureBinding=fixture.capture_binding,
            captureId="b" * 64, resourceId=72473, shaderRawSha256="a" * 64, stage="fragment", entryPoint="main",
            projectSourceIdentity=identity, editorEligible=True, editorEligibility="managed-source",
            autoApplyReady=index == 0, activeOrganization=index == 1))
        heads[workspace_id] = dict(knowledgeId=knowledge_id, isCurrentHead=True, sourceFormat=language,
            rawShaderSha256="a" * 64, stage="fragment", entryPoint="main", sourceSha256=source_sha,
            activationRevision=33 if index == 0 else 7)
    library = dict(available=True, projectId="9" * 32, documents=list(heads.values()))
    return fixture, models, heads, library


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "vulkan", "opengl", "opengles"])
def test_new_live_published_head_supersedes_old_language_without_deleting_source(tmp_path, api):
    fixture, models, heads, library = _models(tmp_path, api)
    authorizations = [dict(workspacePath=model["workspacePath"], index=index) for index, model in enumerate(models)]
    with mock.patch.object(console, "_managed_project_draft_authorizations", return_value=(authorizations, [])), \
         mock.patch.object(console, "_workspace_model", side_effect=lambda auth, unused: copy.deepcopy(models[auth["index"]])), \
         mock.patch.object(console, "_project_library_for_console", return_value=library), \
         mock.patch.object(console, "_current_project_source_head", side_effect=lambda model, lib: heads[model["workspaceId"]]):
        result = console.load_console_snapshot(str(fixture.registry), fixture.capture_binding)
    assert not result["errors"]
    assert [model["workspaceId"] for model in result["workspaces"]] == [models[0]["workspaceId"]]
    assert result["documentAliases"] == {models[1]["workspaceId"]: models[0]["workspaceId"]}
    old = result["alternateWorkspaces"][0]
    assert old["supersededProjectRepresentation"] and old["supersededCaptureSource"]
    assert old["sourceSha256"] == models[1]["sourceSha256"]


@pytest.mark.parametrize("change", ["api", "original", "entry", "stage", "capture", "source", "dirty", "retained", "ambiguous-head"])
def test_different_identity_or_independent_edits_remain_visible(tmp_path, change):
    _, models, heads, library = _models(tmp_path)
    current, old = models
    if change in ("api", "original", "entry", "stage", "capture", "source"):
        field = {"api": "captureApi", "original": "shaderRawSha256", "entry": "entryPoint",
                 "stage": "stage", "capture": "capturePath", "source": "sourceSha256"}[change]
        old[field] = "different"
    elif change == "dirty":
        old["dirty"] = True
    elif change == "retained":
        old["retainedWorkingCopy"] = True
    else:
        heads[old["workspaceId"]]["activationRevision"] = 33
    assert not console._superseded_project_format_aliases(models, library, heads)


@pytest.mark.parametrize("live_state", ["new-live", "both-live", "neither-live", "old-live"])
def test_original_import_receipt_does_not_hide_a_later_exact_published_head(tmp_path, live_state):
    _, models, heads, library = _models(tmp_path)
    current, old = models
    current["projectSourceOrigin"] = dict(current.pop("projectSourceIdentity"), sourceSha256="0" * 64,
                                           knowledgeId="0" * 32)
    current["autoApplyReady"] = live_state in ("new-live", "both-live")
    old["autoApplyReady"] = live_state in ("old-live", "both-live")
    assert console._superseded_project_format_aliases(models, library, heads) == {
        old["workspaceId"]: current["workspaceId"]}
    assert current["projectSourceOrigin"]["sourceSha256"] == "0" * 64


def test_logical_document_identity_excludes_version_language_capture_and_resource(tmp_path):
    _, models, _, _ = _models(tmp_path)
    current, old = models
    old.update(workspaceId="c" * 32, captureId="d" * 64, resourceId=999,
               capturePath=str(tmp_path / "second.rdc"), sourcePath=str(tmp_path / "shader.glsl"))
    assert console.logical_shader_document_id(current) == console.logical_shader_document_id(old)
    for field in ("captureApi", "shaderRawSha256", "stage", "entryPoint"):
        changed = dict(old, **{field: "d" * 64 if field == "shaderRawSha256" else "different"})
        assert console.logical_shader_document_id(changed) != console.logical_shader_document_id(current)


@pytest.mark.parametrize("local_work", ["clean", "dirty", "retained"])
def test_panel_preserves_working_copy_when_hiding_old_published_representation(tmp_path, local_work):
    panel, _, _, current, old, _, _, result = duplicate_panel(tmp_path, dirty=local_work == "dirty")
    old.pop("internalReplaySource")
    old.update(supersededProjectRepresentation=True, supersededCaptureSource=True)
    if local_work == "retained":
        old["retainedWorkingCopy"] = True
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    if local_work == "clean":
        assert list(panel._workspaces) == [current["workspaceId"]]
        assert old["workspaceId"] not in panel._documents
    else:
        assert panel._workspaces[old["workspaceId"]]["retainedWorkingCopy"] is True
        assert old["workspaceId"] not in panel._document_aliases
        assert old["workspaceId"] in panel._documents
