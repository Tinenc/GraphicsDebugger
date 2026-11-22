import copy
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import internal_tool_service as jobs, project_restore as restore


@pytest.mark.parametrize("editor_error", [False, True])
def test_registry_publishes_completion_before_editor_start_and_preserves_commit(tmp_path, monkeypatch, editor_error):
    started, release = threading.Event(), threading.Event()
    publications = []
    progress_publications = []
    def persist(*args, **kwargs):
        publications.append(copy.deepcopy(args[2]))
        progress_publications.append(copy.deepcopy(kwargs.get("progress")))
    monkeypatch.setattr(jobs, "persist_restore_result", persist)
    def runner(project, capture, session, *, progress_callback, cancel_event, deadline_monotonic, post_restore_callback):
        progress_callback({"phase": "complete", "message": "Restored"})
        item = {"workspaceId": "b"*32, "workspacePath": str(tmp_path / "shader"), "status": "pending",
                "captureBinding": {"instanceId": "gui-one", "capturePath": capture, "captureSessionId": session}}
        def start():
            started.set()
            assert release.wait(5)
            if editor_error:
                raise RuntimeError("editor unavailable")
            enabled = dict(item, status="enabled", liveEditId="c"*32, watchId="d"*32)
            return {"attempted": 1, "enabled": 1, "deferred": 0, "failed": 0, "items": [enabled]}
        post_restore_callback(start)
        return {"completed": True, "editing": {"status": "pending", "pending": 1,
            "attempted": 0, "enabled": 0, "deferred": 0, "failed": 0, "items": [item]}}
    clock = [1.0]
    registry = jobs.ProjectRestoreJobRegistry(runner=runner, monotonic=lambda: clock[0], history_ttl_seconds=10)
    try:
        submitted, _ = registry.submit({"projectRoot": str(tmp_path), "capturePath": str(tmp_path / "capture.rdc"),
                                       "captureSessionId": "a" * 32})
        assert started.wait(5)
        snapshot = registry.get(submitted["jobId"])
        assert snapshot["status"] == "completed"
        assert snapshot["result"]["completed"] is True
        assert snapshot["result"]["editing"]["status"] == "pending"
        assert publications[-1]["completed"] is True
        clock[0] += 11
        assert registry.get(submitted["jobId"])["result"]["editing"]["status"] == "pending"
        release.set()
    finally:
        release.set()
        registry.shutdown()
    final = registry.get(submitted["jobId"])
    assert final["status"] == "completed"
    assert final["result"]["completed"] is True
    assert "status" not in final["result"]["editing"]
    assert "status" not in publications[-1]["editing"]
    assert final["result"]["editing"]["failed"] == int(editor_error)
    assert final["result"]["editing"]["enabled"] == int(not editor_error)
    assert progress_publications[-1] and progress_publications[-1] == progress_publications[-2]


def test_restore_uses_artifact_status_without_recovering_public_drafts(monkeypatch, tmp_path):
    revision = [1]
    artifact = SimpleNamespace(status=lambda path: {"revision": revision[0]})
    host = SimpleNamespace(_shader_workspace_artifact_service=lambda: artifact,
                           get_shader_workspace_status=Mock(side_effect=AssertionError("draft recovery")))
    monkeypatch.setattr(restore, "_validate_reuse_workspace_status", lambda status, *args: status)
    assert restore._reuse_workspace_status(host, {}, tmp_path, {}, "", None)["revision"] == 1
    revision[0] = 2
    assert restore._reuse_workspace_status(host, {}, tmp_path, {}, "", None)["revision"] == 2
    host.get_shader_workspace_status.assert_not_called()


def test_project_identity_is_shared_but_same_size_preserved_mtime_changes_are_seen(tmp_path, monkeypatch):
    from src import file_content_cache
    path = tmp_path / "project.json"
    path.write_text('{"projectId":"one"}', encoding="utf-8")
    monkeypatch.setattr(restore._RESTORE_THREAD_STATE, "project_identities", {}, raising=False)
    original = restore.read_json_object
    reader = Mock(wraps=original)
    monkeypatch.setattr(restore, "read_json_object", reader)
    assert restore._read_restore_project_identity(path)["projectId"] == "one"
    assert restore._read_restore_project_identity(path)["projectId"] == "one"
    assert reader.call_count == (1 if file_content_cache.file_stamp(path) is not None else 2)
    stamp = path.stat()
    path.write_text('{"projectId":"two"}', encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert restore._read_restore_project_identity(path)["projectId"] == "two"


def test_unknown_filesystem_identity_does_not_cache_project_state(tmp_path, monkeypatch):
    from src import file_content_cache
    path = tmp_path / "project.json"
    path.write_text('{"projectId":"one"}', encoding="utf-8")
    monkeypatch.setattr(restore._RESTORE_THREAD_STATE, "project_identities", {}, raising=False)
    monkeypatch.setattr(file_content_cache, "file_stamp", lambda path: None)
    assert restore._read_restore_project_identity(path)["projectId"] == "one"
    path.write_text('{"projectId":"two"}', encoding="utf-8")
    assert restore._read_restore_project_identity(path)["projectId"] == "two"
