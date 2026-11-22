"""A replay cache is not another editable version of its retained source."""
import copy
import hashlib
import types
from unittest import mock

import pytest

import test_shader_workspace_panel as panel_tests


def duplicate_panel(tmp_path, dirty=False):
    panel, provider, panel_type, current, source, preview = panel_tests._canonical_display_fixture(tmp_path)
    current["shaderRawSha256"] = "8" * 64
    old_id = "7" * 32
    old = dict(current, workspaceId=old_id, sourcePath=str(tmp_path / "replay/source/shader.hlsl"),
               internalReplaySource=True, canonicalSourceWorkspaceId=current["workspaceId"],
               historicalSource=True)
    old_source = dict(source, workspaceId=old_id, sourcePath=old["sourcePath"],
                      rawSha256="6" * 64, text="float4 MainPS() : SV_Target { return 0; }")
    old_doc = copy.deepcopy(panel._documents[current["workspaceId"]])
    old_doc.update(workspaceId=old_id, sourceWorkspaceId=old_id, workspace=old,
                   rawSha256=old_source["rawSha256"], baselineEditorText=old_source["text"])
    old_doc["editor"].GetText.return_value = "my unsaved edit" if dirty else old_source["text"]
    panel._workspaces[old_id] = old
    panel._source_documents[old_id] = old_source
    panel._documents[old_id] = old_doc
    panel.tab_bar.ids.append(old_id)
    panel._pinned = [old_id]
    panel._restore_open = [old_id]
    panel._restore_active = old_id
    aliases = {old_id: current["workspaceId"]}
    result = {"snapshot": {"workspaces": [current], "alternateWorkspaces": [old],
                           "documentAliases": aliases}, "eventIds": {}, "sources": {},
              "watchedPaths": [], "existingPaths": [], "signature": ()}
    stale = dict(preview, sourceWorkspaceId=old_id, sourcePath=old["sourcePath"],
                 shaderRawSha256=current["shaderRawSha256"], rawSha256=old_source["rawSha256"],
                 organizationId="", text=old_source["text"])
    return panel, provider, panel_type, current, old, source, stale, result


def test_clean_internal_replay_tab_retires_to_latest_editable_source(tmp_path):
    panel, _, panel_type, current, old, source, _, result = duplicate_panel(tmp_path)
    panel._open_workspace = mock.Mock(wraps=types.MethodType(panel_type._open_workspace, panel))
    panel._pending_open[old["workspaceId"]] = {"quiet": True, "select": False}
    original = panel._documents[current["workspaceId"]]
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert list(panel._workspaces) == [current["workspaceId"]]
    assert old["workspaceId"] not in panel._documents
    assert panel._pinned == [current["workspaceId"]]
    assert panel._restore_active == current["workspaceId"]
    assert panel._document_aliases == result["snapshot"]["documentAliases"]
    assert panel._documents[current["workspaceId"]] is original
    assert original["editor"].GetText() == source["text"]
    assert all(key != old["workspaceId"] for key in panel.tab_bar.ids)
    assert panel.tab_bar.ids == [current["workspaceId"]]


def test_unsignalled_dirty_internal_tab_is_retained_without_discarding_edits(tmp_path):
    panel, _, _, current, old, _, _, result = duplicate_panel(tmp_path, dirty=True)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    retained = panel._workspaces[old["workspaceId"]]
    assert retained["retainedWorkingCopy"] is True and retained["autoApplyReady"] is False
    assert old["workspaceId"] not in panel._document_aliases
    assert panel._documents[old["workspaceId"]]["editor"].GetText() == "my unsaved edit"
    assert current["workspaceId"] in panel._workspaces


def test_late_internal_show_callback_cannot_replace_latest_source_or_identity(tmp_path):
    panel, _, _, current, old, source, stale, result = duplicate_panel(tmp_path)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    panel._open_workspace.reset_mock()
    before = dict(current)
    panel._refresh_saved_organization_source = mock.Mock(side_effect=AssertionError("stale bytes must not refresh latest"))
    state = panel.show_organization_preview(stale)
    assert state["workspaceId"] == current["workspaceId"]
    assert state["redirectedFromWorkspaceId"] == old["workspaceId"]
    assert state["displayedSourceWorkspaceId"] == current["workspaceId"]
    assert state["displayedSourcePath"] == current["sourcePath"]
    assert state["displayedSourceSha256"] == hashlib.sha256(source["text"].encode()).hexdigest()
    assert list(panel._workspaces) == [current["workspaceId"]]
    assert current == before
    assert panel._source_documents[current["workspaceId"]] == source
    assert panel._documents[current["workspaceId"]]["editor"].GetText() == source["text"]
    assert not panel._organization_previews
    panel._open_workspace.assert_called_once_with(current["workspaceId"], quiet=True, select=True)


def test_late_show_reports_unsaved_canonical_editor_hash_without_replacing_it(tmp_path):
    panel, _, _, current, old, source, stale, result = duplicate_panel(tmp_path)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    editor = panel._documents[current["workspaceId"]]["editor"]
    editor.GetText.return_value = "// 最新未保存修改\nfloat4 MainPS() { return 2; }"
    editor.SetText.reset_mock()
    state = panel.show_organization_preview(stale)
    assert state["displayedSourceSha256"] == hashlib.sha256(editor.GetText().encode()).hexdigest()
    assert state["displayedSourceSha256"] != stale["rawSha256"]
    editor.SetText.assert_not_called()
    assert panel._source_documents[current["workspaceId"]] == source


def test_snapshot_retires_saved_preview_of_internal_source_from_previous_callback(tmp_path):
    panel, _, _, current, old, source, stale, result = duplicate_panel(tmp_path)
    preview_id = stale["workspaceId"]
    preview_model = dict(stale, editorEligible=True, readOnly=False)
    panel._organization_previews[preview_id] = {"workspace": preview_model, "opened": stale}
    panel._workspaces[preview_id] = preview_model
    panel._source_documents[preview_id] = stale
    preview_doc = copy.deepcopy(panel._documents[old["workspaceId"]])
    preview_doc.update(workspaceId=preview_id, workspace=preview_model,
                       documentKind="organization-preview")
    panel._documents[preview_id] = preview_doc
    panel.tab_bar.ids.append(preview_id)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert list(panel._workspaces) == [current["workspaceId"]]
    assert not panel._organization_previews
    assert preview_id not in panel._documents
    assert old["workspaceId"] not in panel._documents
    assert panel._documents[current["workspaceId"]]["editor"].GetText() == source["text"]


@pytest.mark.parametrize("change", ["capture", "session", "raw", "resource", "profile", "api", "stage", "entry", "include", "draft", "organization", "dirty"])
def test_alias_preview_requires_current_capture_and_exact_original_program(tmp_path, change):
    panel, provider, _, current, old, _, stale, result = duplicate_panel(tmp_path)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    if change == "capture":
        stale["capturePath"] = str(tmp_path / "another.rdc")
    elif change == "session":
        stale["captureBinding"] = dict(stale["captureBinding"], captureSessionId="9" * 32)
    elif change == "raw":
        stale["shaderRawSha256"] = "9" * 64
    elif change == "resource":
        stale["resourceId"] += 1
    elif change in ("profile", "api"):
        field = "targetProfile" if change == "profile" else "api"
        current[field], stale[field] = "current", "different"
    elif change == "stage":
        stale["stage"] = "vertex"
    elif change == "entry":
        stale["entryPoint"] = "OtherPS"
    elif change == "include":
        stale["documentKind"] = "source-include"
    elif change == "draft":
        stale["canonicalSourceUpdated"] = False
    elif change == "organization":
        stale["organizationId"] = "1" * 32
    else:
        panel._documents[stale["workspaceId"]] = {"dirty": True}
    assert panel._aliased_source_preview_workspace(stale) == ""


def test_late_open_resolves_alias_before_existing_stale_tab_lookup(tmp_path):
    panel, _, panel_type, current, old, _, _, result = duplicate_panel(tmp_path)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    # A queued callback can still carry the retired ID. It selects the existing
    # latest editor without creating another tab or another source read.
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    before = list(panel.tab_bar.ids)
    assert panel._open_workspace(old["workspaceId"], quiet=True)
    assert panel.tab_bar.ids == before
    assert panel.tab_bar.ids[panel.tab_bar.selected] == current["workspaceId"]


def test_late_navigation_does_not_apply_old_source_offsets_or_include_context(tmp_path):
    panel, _, panel_type, current, old, _, _, result = duplicate_panel(tmp_path)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    editor = panel._documents[current["workspaceId"]]["editor"]
    editor.GoToLine.reset_mock()
    panel._definition_requested = mock.Mock()
    assert panel._open_workspace(old["workspaceId"], navigation=(123, 4), definition=("old.hlsli", 10, 2))
    editor.GoToLine.assert_not_called()
    panel._definition_requested.assert_not_called()


def test_aliases_are_cleared_with_capture_binding(tmp_path):
    panel, provider, _, current, old, _, _, result = duplicate_panel(tmp_path)
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    provider["binding"] = dict(panel._capture_binding, captureSessionId="9" * 32)
    assert panel._canonical_workspace_id(old["workspaceId"]) == old["workspaceId"]
    panel.capture_changed(provider["binding"])
    assert panel._document_aliases == {}


@pytest.mark.parametrize("aliases", [{"old": "old"}, {"old": "middle", "middle": "old"}, {"old": ""}])
def test_cyclic_or_malformed_display_aliases_do_not_redirect(aliases):
    module = panel_tests._load_without_qt()
    assert module._document_alias_target("old", aliases) == "old"
