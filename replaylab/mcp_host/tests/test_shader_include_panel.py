import hashlib
import os
import queue
import threading
from unittest import mock

import pytest
from test_shader_workspace_panel import _canonical_display_fixture, _load_without_qt


def _include(workspace, opened, tmp_path):
    return dict(opened, schema="bread-shader-source-document/v1", workspaceId="7" * 64,
                sourceDocumentId="7" * 64, documentKind="source-include", readOnly=False,
                canonicalSourceUpdated=False, sourcePath=str(tmp_path / "source/math.hlsli"),
                rootSourcePath=workspace["sourcePath"], rootSourceSha256="8" * 64,
                sourceRelativePath="source/math.hlsli", fileName="math.hlsli", mutationPaths=[])


@pytest.mark.parametrize("capture_changes", [False, True])
def test_unopened_exact_include_navigation_is_background_and_capture_bound(tmp_path, capture_changes):
    panel, provider, unused, workspace, source, opened = _canonical_display_fixture(tmp_path)
    owner_id = workspace["workspaceId"]
    panel._documents[owner_id]["documentToken"] = 9
    target = _include(workspace, opened, tmp_path)
    panel._save_client = mock.Mock()
    panel._save_client.open_source_document.return_value = target
    protocol = panel.refresh.__func__.__globals__["_shader_console_protocol"]
    with mock.patch.object(protocol, "load_related_shader_source_document", return_value=target):
        panel._definition_requested(target["sourcePath"], 12, 5, owner_id)
        panel._save_client.open_source_document.assert_not_called()
        unused_owner, operation, callback = panel._snapshot_executor.jobs.pop()
        result = operation()
        panel._save_client.open_source_document.assert_called_once_with(
            workspace["workspacePath"], owner_id, target["sourcePath"], panel._capture_binding)
        if capture_changes:
            provider["binding"] = None
        callback(result, "")
    if capture_changes:
        assert target["workspaceId"] not in panel._documents
    else:
        document = panel._documents[target["workspaceId"]]
        assert document["readOnly"] is False
        document["editor"].GoToLine.assert_called_once_with(12, 5)
        assert panel._workspaces[target["workspaceId"]]["autoApplyReady"] is False


def test_exact_foreign_shader_path_cannot_skip_include_authorization(tmp_path):
    panel, unused_provider, unused, workspace, source, opened = _canonical_display_fixture(tmp_path)
    foreign = dict(workspace, workspaceId="f" * 32, sourcePath=str(tmp_path / "other/shader.hlsl"))
    foreign_editor = mock.Mock()
    panel._documents[foreign["workspaceId"]] = dict(workspace=foreign, available=True,
        sourcePath=foreign["sourcePath"], editor=foreign_editor)
    panel._open_related_definition = mock.Mock()
    panel._definition_requested(foreign["sourcePath"], 3, 4, workspace["workspaceId"])
    foreign_editor.GoToLine.assert_not_called()
    panel._open_related_definition.assert_called_once_with(
        workspace["workspaceId"], foreign["sourcePath"], 3, 4)


def test_frozen_preview_keeps_real_identity_and_canonical_include_base(tmp_path):
    panel, unused_provider, unused, workspace, source, opened = _canonical_display_fixture(tmp_path)
    frozen = dict(opened, workspaceId="8" * 64, canonicalSourceUpdated=False,
                  rootSourcePath=workspace["sourcePath"],
                  sourcePath=str(tmp_path / "ai/frozen/shader.hlsl"))
    assert panel.show_organization_preview(frozen)
    # The fixture replaces initial widget construction; refresh exercises the
    # production editor identity/base-directory update on that existing buffer.
    assert panel.show_organization_preview(frozen)
    editor = panel._documents[frozen["workspaceId"]]["editor"]
    editor.SetFileName.assert_called_with(frozen["sourcePath"])
    editor.SetIncludeBaseDirectory.assert_called_with(os.path.dirname(workspace["sourcePath"]))
    assert panel._workspaces[frozen["workspaceId"]]["rootSourcePath"] == workspace["sourcePath"]


def test_include_parent_dirty_buffer_blocks_relocation_without_losing_text(tmp_path):
    panel, unused_provider, unused, workspace, source, opened = _canonical_display_fixture(tmp_path)
    target = _include(workspace, opened, tmp_path)
    target["mutationPaths"] = [workspace["sourcePath"]]
    assert panel.show_organization_preview(target)
    root = panel._documents[workspace["workspaceId"]]
    root["sourcePath"] = workspace["sourcePath"]
    root["dirty"] = True
    root["editor"].GetText.return_value = "// unsaved main"
    document = panel._documents[target["workspaceId"]]
    document["dirty"] = True
    document["editor"].GetText.return_value = "// unsaved header"
    assert panel._save_document(target["workspaceId"]) is False
    assert panel._save_tasks.empty()
    assert root["editor"].GetText() == "// unsaved main"
    assert document["editor"].GetText() == "// unsaved header"


def test_include_save_worker_sends_exact_header_identity_never_main_source_route():
    module = _load_without_qt()
    client = mock.Mock()
    client.request_pending.return_value = False
    client.save_source_document.return_value = {"rawSha256": "f" * 64}
    task = dict(sourceOnly=True, sourceDocumentId="a" * 64, workspacePath="workspace",
                sourceWorkspaceId="b" * 32, baseHash="c" * 64, rootSourceSha256="d" * 64,
                sourceText="float F() { return 2; }", captureBinding={"captureSessionId": "e" * 32})
    tasks, results = queue.Queue(), queue.Queue()
    tasks.put(task); tasks.put(None)
    module._save_worker_loop(client, tasks, results, threading.Event())
    assert results.get_nowait()["kind"] == "saved"
    client.save_source_document.assert_called_once_with(
        "workspace", "b" * 32, "a" * 64, "c" * 64, "d" * 64, task["sourceText"], task["captureBinding"])
    client.save_source.assert_not_called()
    client.save.assert_not_called()


def test_include_save_promotion_keeps_header_refresh_and_new_parent_hash(tmp_path):
    panel, unused_provider, unused, workspace, source, opened = _canonical_display_fixture(tmp_path)
    target = _include(workspace, opened, tmp_path)
    panel.show_organization_preview(target)
    document = panel._documents[target["workspaceId"]]
    text = "float F() { return 3; }"
    digest = hashlib.sha256(text.encode()).hexdigest()
    document["pendingSave"] = dict(sourceOnly=True, sourceDocumentId=target["sourceDocumentId"],
                                   submittedHash=digest, submittedEditorText=text, sourceText=text)
    panel._sync_document_dirty = mock.Mock()
    panel._current_workspace_id = lambda: target["workspaceId"]
    panel._promote_submitted_save(document, dict(target, rawSha256=digest,
        rootSourceSha256="9" * 64, mutationPaths=[], modifiedNs=20, size=len(text)), True, True)
    refreshed = panel._source_documents[target["workspaceId"]]
    assert refreshed["canonicalSourceUpdated"] is False
    assert refreshed["documentKind"] == "source-include"
    assert refreshed["sourcePath"] == target["sourcePath"]
    assert panel._workspaces[target["workspaceId"]]["rootSourceSha256"] == "9" * 64
    assert panel._source_documents[workspace["workspaceId"]] == source


@pytest.mark.parametrize("dirty", [False, True])
def test_include_refresh_never_advances_parent_cas_under_a_dirty_buffer(tmp_path, dirty):
    panel, unused_provider, unused, workspace, source, opened = _canonical_display_fixture(tmp_path)
    target = _include(workspace, opened, tmp_path)
    panel.show_organization_preview(target)
    document = panel._documents[target["workspaceId"]]
    document["dirty"] = dirty
    refreshed = dict(target, rootSourceSha256="9" * 64)
    result = dict(snapshot={"workspaces": [workspace]}, sources={target["workspaceId"]: refreshed},
                  sourceErrors={}, watchedPaths=[], existingPaths=[], signature=(), eventIds={})
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert panel._workspaces[target["workspaceId"]]["rootSourceSha256"] == (
        "8" * 64 if dirty else "9" * 64)
