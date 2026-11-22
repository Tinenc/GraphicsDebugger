"""Save-time naming is asynchronous, exact-owner-bound and non-destructive."""
import copy
import json
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from src.ipc_client import _CALL_BINDING
from src.shader_workspace import alias_live_restore as live
from src.shader_workspace.alias_display_mirror import current_display_identity


def host_for(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "ai").mkdir(parents=True)
    project = {"scopeConfigured": True, "projectRoot": str(tmp_path), "workspaceId": "project"}
    status = {"latestCompiled": {"revision": 1, "rawSha256": "b" * 64}}
    host = SimpleNamespace(_active_project_service=lambda: SimpleNamespace(status=lambda: project),
        _require_managed_active_workspace=lambda active, path: path,
        _require_same_active_project=mock.Mock(),
        _shader_workspace_service=lambda: SimpleNamespace(status=lambda path: copy.deepcopy(status)))
    binding = {"instanceId": "qrenderdoc-test", "captureSessionId": "a" * 32, "capturePath": "owned.rdc"}
    return host, workspace, project, status, binding


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def test_background_names_do_not_block_save_and_coalesce_newest_revision(tmp_path, monkeypatch):
    host, workspace, project, status, binding = host_for(tmp_path)
    entered, finish = threading.Event(), threading.Event()
    revisions = []
    def run(item):
        revisions.append(item[2])
        entered.set()
        assert finish.wait(3)
    monkeypatch.setattr(live, "_run", run)
    try:
        result = live.schedule(host, workspace, 1, "b" * 64, binding)
        assert result["status"] == "pending" and entered.wait(3)
        live.schedule(host, workspace, 2, "c" * 64, binding)
        live.schedule(host, workspace, 3, "d" * 64, binding)
        assert revisions == [1]
    finally:
        finish.set()
        wait_until(lambda: not live._JOBS)
    assert revisions == [1, 3]


@pytest.mark.parametrize("outcome", ["success", "read-failed", "superseded", "project-changed"])
def test_background_uses_frozen_session_and_persists_optional_diagnostic(tmp_path, monkeypatch, outcome):
    host, workspace, project, status, binding = host_for(tmp_path)
    def restore(*args):
        assert _CALL_BINDING.get() == (binding["instanceId"], binding["captureSessionId"])
        if outcome == "read-failed":
            raise RuntimeError("native owner was replaced")
        return {"status": "restored", "writtenRows": 0, "writtenBlocks": 0}
    restored = mock.Mock(side_effect=restore)
    monkeypatch.setattr(live, "restore_display_alias_mirrors", restored)
    if outcome == "superseded":
        status["latestCompiled"]["revision"] = 2
    if outcome == "project-changed":
        host._require_same_active_project.side_effect = RuntimeError("project switched")
    live._run((host, workspace, 1, "b" * 64, binding, project))
    path = workspace / "ai/cbuffer-display-restore.json"
    if outcome in ("project-changed", "superseded"):
        restored.assert_not_called()
        assert not path.exists()
    else:
        restored.assert_called_once()
        result = json.loads(path.read_text())
        assert result["candidateRawSha256"] == "b" * 64
        assert result["status"] == ("failed" if outcome == "read-failed" else "restored")
        assert ("diagnostic" in result) == (outcome == "read-failed")
    assert _CALL_BINDING.get() is None


def test_fast_display_identity_is_one_read_without_catalog_or_abi_validation(tmp_path):
    status = {"shader": {"resourceId": 42, "rawSha256": "a" * 64},
              "latestCompiled": {"rawSha256": "b" * 64}, "stage": "fragment", "entryPoint": "main"}
    reply = {"replayMutation": False, "physicalAbiValidated": False, "replayRead": True,
             "shaderResourceId": "42"}
    host = SimpleNamespace(ipc=SimpleNamespace(call=mock.Mock(return_value=reply)),
                           _shader_organization_cbuffer_alias_identity=mock.Mock())
    assert current_display_identity(host, tmp_path, status, "session") is reply
    host._shader_organization_cbuffer_alias_identity.assert_not_called()
    host.ipc.call.assert_called_once_with("get_shader_cbuffer_alias_identity", {
        "expected_capture_session_id": "session", "expected_original_resource_id": 42,
        "expected_original_sha256": "a" * 64, "expected_candidate_sha256": "b" * 64,
        "stage": "fragment", "entry_point": "main"}, timeout=30.0)


@pytest.mark.parametrize("change", [{"replayMutation": True}, {"physicalAbiValidated": True},
                                    {"shaderResourceId": "43"}, {"replayRead": False}])
def test_fast_display_identity_rejects_wrong_owner_or_mutating_route(tmp_path, change):
    status = {"shader": {"resourceId": 42}, "latestCompiled": {}}
    host = SimpleNamespace(ipc=SimpleNamespace(call=lambda *a, **k: {
        "replayMutation": False, "physicalAbiValidated": False, "replayRead": True,
        "shaderResourceId": "42", **change}))
    with pytest.raises(RuntimeError, match="read-only ownership"):
        current_display_identity(host, tmp_path, status, "session")


@pytest.mark.parametrize("outcome", ["applied", "rejected", "wrong-revision", "queue-failed"])
def test_real_editor_success_hook_is_best_effort_and_never_queues_preapply(tmp_path, monkeypatch, outcome):
    from src import internal_tool_service, server
    import os
    monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *args, **kwargs: "current")
    candidate = "b" * 64
    apply_result = {"applied": outcome != "rejected", "validation": {"manualEdit": True},
                    "binding": {"revision": 2 if outcome == "wrong-revision" else 1,
                                "candidateRawSha256": candidate}, "replacement": {"replacementId": "owner"}}
    applied = mock.Mock(return_value=copy.deepcopy(apply_result))
    monkeypatch.setattr(server, "_shader_live_edit_apply_candidate", applied)
    scheduled = mock.Mock(return_value={"status": "pending", "reason": "background-display-restore"})
    if outcome == "queue-failed":
        scheduled.side_effect = RuntimeError("alias queue unavailable")
    monkeypatch.setattr(live, "schedule", scheduled)
    frozen = internal_tool_service._freeze_shader_live_session("owned", "a" * 32, os.getpid(), "owned.rdc")
    result = frozen.apply_candidate(str(tmp_path), 1, candidate)
    assert result["applied"] == apply_result["applied"]
    if outcome in ("applied", "queue-failed"):
        scheduled.assert_called_once()
        assert result["displayAliases"]["status"] == ("failed" if outcome == "queue-failed" else "pending")
    else:
        scheduled.assert_not_called()
