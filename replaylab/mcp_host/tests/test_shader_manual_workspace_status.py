"""Manual apply completion is a UI fact, separate from strict ABI approval."""
import ast
import copy
import types
from unittest import mock

import pytest

from test_shader_workspace_panel import (
    MODULE_PATH, _capture_binding, _load_without_qt, _method, _parse, _scoped_panel,
)


@pytest.fixture
def status_text():
    module = _load_without_qt()
    namespace = {
        "QtCore": types.SimpleNamespace(QCoreApplication=types.SimpleNamespace(
            translate=lambda context, text: text)),
        "_latest_event": module._latest_event,
    }
    tree = ast.Module(body=[copy.deepcopy(_method(_parse(MODULE_PATH), "_status_text"))],
                      type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(MODULE_PATH), "exec"), namespace)
    return lambda workspace: namespace["_status_text"](None, workspace)


@pytest.fixture
def applied_workspace():
    return {
        "autoApplyReady": False,
        "sourceSha256": "a" * 64,
        "watch": {
            "state": "running", "watchId": "watch-current",
            "observedSourceSha256": "a" * 64,
            "lastCompiledSourceSha256": "a" * 64,
            "latestCandidateRevision": 31,
            "events": [{"type": "compileSucceeded", "sequence": 4, "revision": 31}],
        },
        "liveEdit": {
            "state": "running", "sessionState": "current", "watchId": "watch-current",
            "activeRevision": 31, "latestHandledRevision": 31,
            "events": [{"type": "applySucceeded", "sequence": 8, "revision": 31}],
        },
    }


def test_manual_applied_source_displays_success_without_changing_abi_readiness(
        status_text, applied_workspace):
    before = copy.deepcopy(applied_workspace)
    assert status_text(applied_workspace) == "Compiled and applied"
    assert applied_workspace == before
    assert applied_workspace["autoApplyReady"] is False


def test_reused_manual_candidate_reports_current_source_revision(status_text, applied_workspace):
    applied_workspace["liveEdit"]["activeRevision"] = 24
    applied_workspace["liveEdit"]["events"][-1]["type"] = "applyReused"
    assert status_text(applied_workspace) == "Compiled and applied"


def test_restored_current_revision_does_not_require_retained_success_history(
        status_text, applied_workspace):
    applied_workspace["liveEdit"]["events"] = []
    assert status_text(applied_workspace) == "Compiled and applied"


@pytest.mark.parametrize("target,key,value", [
    ("workspace", "sourceSha256", "b" * 64),
    ("workspace", "sourceSha256", ""),
    ("watch", "observedSourceSha256", "b" * 64),
    ("watch", "lastCompiledSourceSha256", "b" * 64),
    ("watch", "latestCandidateRevision", 32),
    ("watch", "watchId", "watch-older"),
    ("watch", "state", "stopped"),
    ("liveEdit", "activeRevision", 0),
    ("liveEdit", "sessionState", "stale"),
    ("liveEdit", "state", "stopped"),
])
def test_older_or_expired_manual_application_does_not_label_current_source_applied(
        status_text, applied_workspace, target, key, value):
    (applied_workspace if target == "workspace" else applied_workspace[target])[key] = value
    assert "checks are still pending" in status_text(applied_workspace)


@pytest.mark.parametrize("target,kind,expected", [
    ("watch", "compileFailed", "Compilation failed; the last good replacement remains active"),
    ("liveEdit", "applyFailed", "Replacement failed; the last good replacement remains active"),
    ("liveEdit", "applyRejected", "Replacement failed; the last good replacement remains active"),
    ("liveEdit", "liveEditFailed", "Replacement failed; the last good replacement remains active"),
])
def test_later_failures_keep_last_good_message(status_text, applied_workspace, target, kind, expected):
    applied_workspace[target]["events"].append({"type": kind, "sequence": 12, "revision": 32})
    assert status_text(applied_workspace) == expected


@pytest.mark.parametrize("target,key,expected", [
    ("watch", "compileInProgress", "Compiling…"),
    ("watch", "pending", "Waiting for source to stabilize…"),
    ("liveEdit", "pendingApply", "Applying saved Shader…"),
    ("liveEdit", "applyInProgress", "Applying saved Shader…"),
])
def test_new_work_keeps_progress_visible(status_text, applied_workspace, target, key, expected):
    applied_workspace[target][key] = True
    assert status_text(applied_workspace) == expected


def retained_after_manual_publication(tmp_path, applied_workspace):
    """Reproduce the captured panel/console disagreement after revision 33.

    The console had the exact current apply, while the panel retained an old
    source-only model with empty audits even after its buffer became clean.
    """
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(
        tmp_path, _capture_binding(tmp_path))
    source_path = str(tmp_path / "project" / "source" / "shader.hlsl")
    current = copy.deepcopy(applied_workspace)
    current.update(workspaceId="current", sourcePath=source_path,
                   captureBinding=panel._capture_binding, shaderRawSha256="b" * 64,
                   stage="fragment", entryPoint="main", resourceId=65557,
                   projectSourceIdentity={"projectId": "c" * 32})
    old = dict(current, retainedWorkingCopy=True, autoApplyReady=False, liveEdit={}, watch={})
    editor = mock.Mock()
    editor.GetText.return_value = "saved shader text"
    document = {"workspaceId": "current", "workspace": old, "editor": editor,
                "captureBinding": panel._capture_binding, "sourcePath": source_path,
                "rawSha256": "a" * 64, "baselineEditorText": "saved shader text",
                "dirty": False, "conflict": False, "saveInFlight": False,
                "documentToken": 7, "textRevision": 33}
    panel._documents = {"current": document}
    panel._workspaces = {"current": current}
    return panel, current, document


@pytest.mark.parametrize("dirty", [False, True])
def test_returned_canonical_model_releases_sticky_retained_state_without_touching_editor(
        tmp_path, status_text, applied_workspace, dirty):
    panel, current, document = retained_after_manual_publication(tmp_path, applied_workspace)
    document["dirty"] = dirty
    document["editor"].GetText.return_value = "unsaved shader text" if dirty else "saved shader text"
    baseline = {key: value for key, value in document.items() if key != "workspace"}
    assert "checks are still pending" in status_text(document["workspace"])
    panel._rebind_project_documents(panel._capture_binding)
    assert panel._document_model("current") is current
    assert document["workspace"] is current
    assert status_text(current) == "Compiled and applied"
    assert {key: value for key, value in document.items() if key != "workspace"} == baseline
    document["editor"].SetText.assert_not_called()
    document["editor"].SetSavePoint.assert_not_called()


@pytest.mark.parametrize("mismatch", ["capture", "source", "original", "stage", "entry", "resource"])
def test_retained_tab_never_borrows_another_source_or_capture_audit(
        tmp_path, applied_workspace, mismatch):
    panel, current, document = retained_after_manual_publication(tmp_path, applied_workspace)
    if mismatch == "capture":
        current["captureBinding"] = dict(panel._capture_binding, captureSessionId="f" * 32)
    elif mismatch == "source":
        current["sourcePath"] = str(tmp_path / "other" / "shader.hlsl")
    else:
        field = {"original": "shaderRawSha256", "stage": "stage", "entry": "entryPoint",
                 "resource": "resourceId"}[mismatch]
        current[field] = 999 if mismatch == "resource" else "different"
    panel._rebind_project_documents(panel._capture_binding)
    retained = panel._document_model("current")
    assert retained["retainedWorkingCopy"] is True
    assert retained["liveEdit"] == retained["watch"] == {}
    assert retained["autoApplyReady"] is False
    document["editor"].SetText.assert_not_called()
