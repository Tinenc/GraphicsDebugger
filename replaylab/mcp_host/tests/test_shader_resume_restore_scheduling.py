from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from src import internal_tool_service as internal, server
from src.shader_live_edit import ShaderLiveEditDeferred
from tests.test_shader_live_edit import _wait_until
from tests.test_shader_live_edit_internal import Manager, binding, context, request_body
from tests.test_shader_live_edit_capture_preflight import _CapturePreflightRig


def request(root, scope=None):
    scope = scope or binding(root)
    return {"projectRoot": str(root), "capturePath": scope["capturePath"],
            "captureSessionId": scope["captureSessionId"]}


def completed(registry, job_id):
    _wait_until(lambda: registry.get(job_id)["status"] in {"completed", "failed"})
    return registry.get(job_id)


def owned_result(scope, *, enabled=1):
    return {"completed": True, "editing": {"enabled": enabled, "items": [
        {"status": "enabled", "captureBinding": scope}]}}


def test_auto_resume_already_in_flight_drains_before_restore_without_holding_registry_lock(tmp_path):
    entered = threading.Event()
    registry = internal.ProjectRestoreJobRegistry(runner=lambda *args, **kwargs: (entered.set() or {}))
    scope = binding(tmp_path)
    try:
        with registry.reserve_shader_resume(scope) as admitted:
            assert admitted
            job, unused = registry.submit(request(tmp_path))
            assert registry.get(job["jobId"])["status"] == "queued"
            assert not entered.wait(0.03)
            with pytest.raises(ShaderLiveEditDeferred):
                internal._automatic_shader_resume_checkpoint()
            with registry.reserve_shader_resume(scope) as second:
                assert second is False
            # Other capture/session work can still be admitted while this one
            # waits; the reservation is state, not a held global mutex.
            other = {**scope, "captureSessionId": "d" * 32}
            with registry.reserve_shader_resume(other) as different:
                assert different
        assert entered.wait(1)
        assert completed(registry, job["jobId"])["status"] == "completed"
        with registry.reserve_shader_resume(scope) as admitted_again:
            assert admitted_again  # No project editors were enabled.
    finally:
        registry.shutdown()


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_waiting_restore_keeps_status_cancel_and_deadline_responsive(tmp_path, stop):
    calls = []
    registry = internal.ProjectRestoreJobRegistry(
        runner=lambda *args, **kwargs: calls.append(args) or {},
        deadline_seconds=0.06 if stop == "deadline" else 10)
    try:
        with registry.reserve_shader_resume(binding(tmp_path)):
            job, unused = registry.submit(request(tmp_path))
            if stop == "cancel":
                assert registry.cancel(job["jobId"])["status"] == "failed"
            result = completed(registry, job["jobId"])
            assert result["status"] == "failed"
            assert calls == []
        assert registry.shader_resume_allowed(binding(tmp_path))
    finally:
        registry.shutdown()


def test_restore_tail_list_and_explicit_start_poll_do_not_wait_for_own_restore(tmp_path, monkeypatch):
    manager = Manager()
    manager.reconcile_capture = mock.Mock(return_value={"results": []})
    service = None
    def runner(*args, **kwargs):
        for op, parameters in [("list", {}), ("start", {"workspace_path": str(tmp_path), "debounce_ms": 100}),
                               ("poll", {"live_edit_id": "1" * 32})]:
            code, result = service.shader_live_edit_operation(request_body(tmp_path, op, parameters))
            assert code == 200, result
        return owned_result(binding(tmp_path))
    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    service = internal.InternalToolService(project=object(), restore_jobs=registry, shader_manager=manager)
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: context(tmp_path))
    try:
        job, unused = registry.submit(request(tmp_path))
        result = completed(registry, job["jobId"])
        assert result["status"] == "completed", result
        manager.reconcile_capture.assert_not_called()
        assert manager.running is True
        code, result = service.shader_live_edit_operation(request_body(tmp_path, "list", {}))
        assert code == 200
        manager.reconcile_capture.assert_not_called()
    finally:
        registry.shutdown()


def test_restore_can_apply_before_resuming_retained_live_edit_authorization(tmp_path, monkeypatch):
    manager = Manager()
    authorization = {"workspacePath": str(tmp_path), "authorizationId": "a" * 32}
    manager.list = mock.Mock(return_value={"sessions": [], "authorizations": [authorization]})
    manager.reconcile_capture = mock.Mock(return_value={"results": []})
    manager.stop = mock.Mock(side_effect=AssertionError("restore must retain user authorization"))
    service = None

    def owner_list(operation, parameters):
        assert operation == "list" and parameters == {}
        code, response = service.shader_live_edit_operation(request_body(tmp_path, "list", {}))
        assert code == 200, response
        return response["result"]

    def runner(*args, **kwargs):
        # Exercise the real shared-apply admission against the owner service
        # while its scheduler has automatic editor resume deferred to restore.
        server._require_workspace_live_edit_quiescent(str(tmp_path), action="applying")
        assert manager.list.return_value["authorizations"] == [authorization]
        code, started = service.shader_live_edit_operation(request_body(tmp_path))
        assert code == 200, started
        return owned_result(binding(tmp_path))

    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    service = internal.InternalToolService(project=object(), restore_jobs=registry, shader_manager=manager)
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: context(tmp_path))
    monkeypatch.setattr(server, "_shader_live_edit_remote", owner_list)
    try:
        job, unused = registry.submit(request(tmp_path))
        result = completed(registry, job["jobId"])
        assert result["status"] == "completed", result
        manager.reconcile_capture.assert_not_called()
        manager.stop.assert_not_called()
        assert manager.running is True
    finally:
        registry.shutdown()


@pytest.mark.parametrize("state", ["starting", "running", "stopping", "stopped", "failed"])
def test_shared_apply_still_checks_actual_live_edit_writer_state(tmp_path, monkeypatch, state):
    status = {"sessions": [{"workspacePath": str(tmp_path), "state": state}],
              "authorizations": [{"workspacePath": str(tmp_path)}]}
    monkeypatch.setattr(server, "_shader_live_edit_remote", lambda *args: status)
    if state in {"stopped", "failed"}:
        server._require_workspace_live_edit_quiescent(str(tmp_path), action="applying")
    else:
        with pytest.raises(RuntimeError, match="stop the exact Shader live-edit session"):
            server._require_workspace_live_edit_quiescent(str(tmp_path), action="applying")


@pytest.mark.parametrize("result", [
    {"completed": False}, {"completed": True},
    {"completed": True, "editing": {"enabled": 0}},
    {"completed": True, "editing": {"enabled": True}},
    {"completed": True, "editing": {"enabled": 1, "items": [{"status": "failed"}]}},
])
def test_incomplete_or_failed_editor_restore_does_not_take_automatic_resume_ownership(tmp_path, result):
    registry = internal.ProjectRestoreJobRegistry(runner=lambda *args, **kwargs: result)
    try:
        job, unused = registry.submit(request(tmp_path))
        completed(registry, job["jobId"])
        with registry.reserve_shader_resume(binding(tmp_path)) as admitted:
            assert admitted
    finally:
        registry.shutdown()


def test_success_ownership_is_exact_and_replacement_restore_failure_releases_it(tmp_path):
    scope = binding(tmp_path)
    answers = [owned_result(scope), RuntimeError("new project restore failed")]
    def runner(*args, **kwargs):
        value = answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    try:
        job, unused = registry.submit(request(tmp_path))
        assert completed(registry, job["jobId"])["status"] == "completed"
        assert not registry.shader_resume_allowed(scope)
        for change in [{"instanceId": "another-owner"}, {"captureSessionId": "c" * 32},
                       {"capturePath": str(tmp_path / "other.rdc")}]:
            assert registry.shader_resume_allowed({**scope, **change})
        new_project = request(tmp_path)
        new_project["projectRoot"] = str(tmp_path / "new-project")
        next_job, unused = registry.submit(new_project)
        assert completed(registry, next_job["jobId"])["status"] == "failed"
        assert registry.shader_resume_allowed(scope)
    finally:
        registry.shutdown()


def test_source_only_restore_suppresses_automatic_resume_without_creating_a_live_editor(tmp_path, monkeypatch):
    scope = binding(tmp_path)
    manager = Manager()
    manager.reconcile_capture = mock.Mock(side_effect=AssertionError("draft must not auto-resume"))
    manager.start = mock.Mock(side_effect=AssertionError("draft must not start a watcher"))
    outcome = {"completed": True, "editing": {"enabled": 0, "deferred": 1,
        "items": [{"status": "deferred", "captureBinding": scope,
                   "readOnly": True, "backgroundAutoApplyAuthorized": False}]}}
    registry = internal.ProjectRestoreJobRegistry(runner=lambda *args, **kwargs: outcome)
    service = internal.InternalToolService(project=object(), restore_jobs=registry, shader_manager=manager)
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: context(tmp_path))
    try:
        job, unused = registry.submit(request(tmp_path))
        assert completed(registry, job["jobId"])["status"] == "completed"
        assert not registry.shader_resume_allowed(scope)
        assert registry.shader_resume_allowed({**scope, "captureSessionId": "e" * 32})
        code, result = service.shader_live_edit_operation(request_body(tmp_path, "list", {}))
        assert code == 200, result
        manager.reconcile_capture.assert_not_called()
        manager.start.assert_not_called()
    finally:
        registry.shutdown()


def test_automatic_owner_tick_defers_running_restore_and_retries_after_failure(tmp_path, monkeypatch):
    rig = _CapturePreflightRig(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def runner(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        raise RuntimeError("intentional restore failure")
    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    rig.service.restore_jobs = registry
    try:
        rig.authorize_history()
        job, unused = registry.submit(request(tmp_path, rig.scope))
        assert entered.wait(1)
        refresh = rig.refresh()
        assert refresh["results"][0]["deferred"] is True
        rig.preflight.assert_not_called()
        rig.assert_no_authority()
        release.set()
        assert completed(registry, job["jobId"])["status"] == "failed"
        refresh = rig.refresh()
        assert refresh["results"][0]["result"]["results"][0]["resumed"] is True
        rig.preflight.assert_called_once()
    finally:
        release.set()
        registry.shutdown()
        rig.close()


def test_restore_submitted_during_local_status_prevents_catalog_export_and_keeps_retry(tmp_path, monkeypatch):
    rig = _CapturePreflightRig(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def runner(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        raise RuntimeError("intentional restore failure")
    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    rig.service.restore_jobs = registry
    submitted = []
    original_status = rig.workspace_status.side_effect
    def preempted_status(path):
        if not submitted:
            job, unused = registry.submit(request(tmp_path, rig.scope))
            submitted.append(job)
        return original_status(path)
    rig.workspace_status.side_effect = preempted_status
    try:
        rig.authorize_history()
        refresh = rig.refresh()
        rig.preflight.assert_not_called()
        rig.assert_no_authority()
        assert entered.wait(1)
        assert rig.manager._capture_resume_attempts == {}
        release.set()
        assert completed(registry, submitted[0]["jobId"])["status"] == "failed"
        refresh = rig.refresh()
        assert refresh["results"][0]["result"]["results"][0]["resumed"] is True
    finally:
        release.set()
        registry.shutdown()
        rig.close()


def test_completed_project_editor_scope_hints_are_bounded(tmp_path):
    registry = internal.ProjectRestoreJobRegistry(runner=lambda *args, **kwargs: {})
    try:
        with registry._lock:
            for number in range(140):
                scope = {**binding(tmp_path), "captureSessionId": "%032x" % number}
                job = SimpleNamespace(capture_path=scope["capturePath"],
                                      capture_session_id=scope["captureSessionId"])
                registry._remember_project_editors_locked(job, owned_result(scope))
        assert len(registry._project_editor_scopes) == 128
        assert registry.shader_resume_allowed({**binding(tmp_path), "captureSessionId": "%032x" % 0})
        assert not registry.shader_resume_allowed({**binding(tmp_path), "captureSessionId": "%032x" % 139})
    finally:
        registry.shutdown()
