"""A successful editor save must become the source restored by another RDC."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from unittest import mock

import pytest

from src import internal_tool_service, ipc_client, project_restore, server
from src.shader_live_edit import ShaderLiveEditManager
from src.shader_workspace.compile_source_unit import capture_compilation_unit
from test_shader_live_edit import _FakeWatchManager, _compatible_result, _wait_until


@pytest.mark.parametrize("change", ["source-comment", "header-comment", "missing-proof"])
def test_same_binary_changed_source_unit_still_runs_apply_and_publication(tmp_path, change):
    source = tmp_path / "source/shader.hlsl"
    source.parent.mkdir()
    header = source.parent / "lighting.hlsl"
    header.write_text("float lighting() { return 1; }\n", encoding="utf-8")
    source.write_text('#include "lighting.hlsl"\nfloat4 MainPS():SV_Target { return lighting(); }\n', encoding="utf-8")
    watch = _FakeWatchManager()
    calls = []

    def publish(revision):
        unit = capture_compilation_unit(source, source.read_bytes())
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        value = {"source": {"path": "source/shader.hlsl"}, "latestCompiled": {
            "revision": revision, "rawSha256": "6" * 64, "sourceSha256": source_sha,
            "sourceDependencies": {"digest": unit["digest"]}}}
        (tmp_path / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
        watch.publish(started["watchId"], revision, "6" * 64, source_sha)

    def apply(workspace, revision, raw):
        calls.append(revision)
        result = _compatible_result(tmp_path, revision, raw)
        result["projectPublication"] = {"status": "committed", "knowledgeId": "%032x" % revision}
        return result

    manager = ShaderLiveEditManager(watch, apply, poll_seconds=0.005, join_seconds=0.5)
    try:
        started = manager.start(tmp_path, 100)
        publish(6)
        _wait_until(lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1)
        if change == "source-comment":
            source.write_text(source.read_text() + "// clearer description\n", encoding="utf-8")
        elif change == "header-comment":
            header.write_text(header.read_text() + "// clearer lighting description\n", encoding="utf-8")
        if change == "missing-proof":
            # No valid source-unit identity can authorize the no-callback path.
            job = manager._jobs[started["liveEditId"]]
            with job._lock:
                job._active_apply.pop("sourceIdentity", None)
        publish(7)
        _wait_until(lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 2)
        status = manager.poll(started["liveEditId"])
        assert calls == [6, 7]
        assert status["applyReuses"] == 0
        assert status["lastApply"]["projectPublication"]["knowledgeId"] == "%032x" % 7
    finally:
        manager.shutdown()


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "opengl", "vulkan"])
@pytest.mark.parametrize("outcome", ["committed", "disk-error", "capture-changed", "rejected"])
def test_frozen_editor_callback_publishes_only_successful_current_capture(
        tmp_path, monkeypatch, api, outcome):
    instance, session = "qrenderdoc-save", "b" * 32
    frozen = internal_tool_service._freeze_shader_live_session(instance, session, os.getpid(), str(tmp_path / "frame.rdc"))
    states = iter(["current", "expired" if outcome == "capture-changed" else "current"])
    monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *a, **kw: next(states))
    result = _compatible_result(tmp_path, 7, "6" * 64)
    result["binding"].update(api=api, sourceSha256="7" * 64)
    if outcome == "rejected":
        result["applied"] = False

    def apply(*args):
        assert ipc_client._CALL_BINDING.get() == (instance, session)
        return copy.deepcopy(result)

    def publish(*args, **kwargs):
        assert ipc_client._CALL_BINDING.get() == (instance, session)
        assert args == (str(tmp_path), 7, "6" * 64, result["replacement"]["replacementId"])
        assert kwargs == {"embed_capture_index": False}
        if outcome == "disk-error":
            raise OSError("project disk unavailable")
        return {"projectCommitted": True, "knowledge": {"knowledgeId": "e" * 32}}

    monkeypatch.setattr(server, "_shader_live_edit_apply_candidate", apply)
    sync = mock.Mock(side_effect=publish)
    monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", sync)
    observed = frozen.apply_candidate(str(tmp_path), 7, "6" * 64)
    assert observed["applied"] == result["applied"]
    assert observed["replacement"] == result["replacement"]
    if outcome in {"capture-changed", "rejected"}:
        sync.assert_not_called()
    else:
        sync.assert_called_once()
    if outcome != "rejected":
        assert observed["projectPublication"]["status"] == ("committed" if outcome == "committed" else "pending")
    assert ipc_client._CALL_BINDING.get() is None


def test_strict_same_binary_resume_validates_new_source_without_replacing_native_target(tmp_path, monkeypatch):
    result = _compatible_result(tmp_path, 7, "6" * 64)
    result.update(idempotent=True, operationId="9" * 32)
    result["replacement"]["revision"] = 3
    result["binding"]["sourceSha256"] = "7" * 64
    result["validation"]["captureSessionId"] = "8" * 32
    monkeypatch.setattr(server, "_apply_shader_workspace_shared_resource_with_live_abi_lease", lambda *a, **k: None)
    apply = mock.Mock(return_value=json.dumps(result))
    monkeypatch.setattr(server, "apply_shader_workspace_shared_resource", apply)
    fresh = mock.Mock(return_value=(None, {"source": {"currentSha256": "7" * 64}},
        {"binding": result["binding"]}, {"replayAbiValidated": True, "captureSessionId": "8" * 32}))
    monkeypatch.setattr(server, "_fresh_shader_candidate_validation", fresh)
    readback = mock.Mock(return_value=copy.deepcopy(result))
    monkeypatch.setattr(server, "_observe_current_shared_shader_candidate", readback)
    observed = server._shader_live_edit_apply_candidate_with_abi(str(tmp_path), 7, "6" * 64)
    fresh.assert_called_once_with(str(tmp_path), 7, "6" * 64)
    apply.assert_called_once()
    readback.assert_called_once()
    assert observed["replacement"]["revision"] == 3
    assert observed["binding"]["revision"] == 7
    assert observed["validation"]["currentSourceAbiValidated"] is True


def test_live_apply_does_not_wait_back_on_restore_that_can_be_stopping_its_worker(tmp_path, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()

    def restore():
        with project_restore._RESTORE_GUARD:
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=restore)
    thread.start()
    assert entered.wait(1)
    try:
        monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *a, **kw: "current")
        monkeypatch.setattr(server, "_shader_live_edit_apply_candidate",
                            lambda *a: _compatible_result(tmp_path, 7, "6" * 64))
        sync = mock.Mock(side_effect=AssertionError("must not enter a busy restore"))
        monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", sync)
        frozen = internal_tool_service._freeze_shader_live_session(
            "qrenderdoc-save", "b" * 32, os.getpid(), str(tmp_path / "frame.rdc"))
        result = frozen.apply_candidate(str(tmp_path), 7, "6" * 64)
        assert not release.is_set()
        assert result["applied"] is True and result["projectPublication"]["status"] == "pending"
        sync.assert_not_called()
    finally:
        release.set()
        thread.join(3)


@pytest.mark.parametrize("failure", ["", "changed-source", "changed-capture", "unconfigured"])
def test_project_sync_completes_missing_live_lease_artifact_before_publication(monkeypatch, failure):
    from pathlib import Path
    from test_project_restore import ProjectRestoreTests

    fixture = ProjectRestoreTests("test_final_shader_association_preserves_rdc_bytes_and_old_physical_signed_provenance")
    fixture.setUp()
    try:
        restored, host, replay, unused = fixture._run(host_mutator=fixture._ticketed_restore_host)
        assert restored["completed"]
        status = host.workspace_service._status
        replacement = copy.deepcopy(host.last_apply_response["replacement"])
        host.ipc.shared_replacements = [replacement]
        host.ipc.calls.clear()
        host._require_managed_active_workspace = lambda active, path: Path(path)
        if failure == "unconfigured":
            host.active_service._status["scopeConfigured"] = False

        def validate(workspace, revision, candidate):
            assert (workspace, revision, candidate) == (
                str(fixture.workspace), status["revision"], fixture.candidate_sha)
            status["latestCompiled"].update(replayAbiValidated=True, abiCompatible=True,
                requiresReplayAbiValidation=False, validationScope="replay-reflection-abi-compatible")
            checked = copy.deepcopy(status)
            if failure == "changed-source":
                checked["source"]["currentSha256"] = "a" * 64
            return host.workspace_service, checked, {}, {"replayAbiValidated": True,
                "captureSessionId": "f" * 32 if failure == "changed-capture" else fixture.session_id}

        fresh = mock.Mock(side_effect=validate)
        host._fresh_shader_candidate_validation = fresh
        knowledge = copy.deepcopy(replay.state["workspaces"][fixture.knowledge_id])
        knowledge.update(originCatalogSha256=replay.state["catalogSha256"],
            targetProfile=copy.deepcopy(status["targetProfile"]),
            sourceSha256=status["source"]["currentSha256"],
            compiledCandidate={"rawSha256": fixture.candidate_sha})
        register = mock.Mock(return_value={"knowledge": knowledge})
        monkeypatch.setattr(project_restore, "_host_module", lambda: host)
        monkeypatch.setattr(project_restore, "ReplayProjectService", lambda *args: replay)
        monkeypatch.setattr(project_restore, "_register_applied_workspace_with_readback", register)
        before = fixture.capture_path.read_bytes()
        if failure:
            with pytest.raises(RuntimeError, match="source or capture|not configured"):
                project_restore.sync_active_shader_workspace_after_apply(
                    fixture.workspace, status["revision"], fixture.candidate_sha, replacement["replacementId"])
            register.assert_not_called()
        else:
            result = project_restore.sync_active_shader_workspace_after_apply(
                fixture.workspace, status["revision"], fixture.candidate_sha, replacement["replacementId"])
            assert result["projectCommitted"] is True
            assert result["physicalCandidateRevision"] == replacement["revision"]
            register.assert_called_once()
        assert fresh.call_count == (0 if failure == "unconfigured" else 1)
        assert fixture.capture_path.read_bytes() == before
        assert all(method == "get_shared_shader_replacements" for method, unused in host.ipc.calls)
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("edit", ["comment-only", "changed-result"])
def test_editor_callback_advances_real_project_head_restored_by_another_rdc(monkeypatch, edit):
    from pathlib import Path
    from test_active_projects import ActiveReplayProjectTests, _dxbc

    fixture = ActiveReplayProjectTests("test_applied_user_edit_publishes_current_source_without_old_ai_gate")
    fixture.setUp()
    try:
        fixture._bound()
        catalog, shader, capture_id = fixture._catalog("save-origin", _dxbc(b"original program"), resource_id=181)
        fixture.service.import_capture(catalog)
        status = fixture._workspace_status("save-current", capture_id, shader)
        binary = _dxbc(b"first executable")
        fixture._with_fully_validated_candidate(status, binary)
        previous = fixture.service.register_workspace(status)
        source = Path(status["source"]["path"])
        text = (source.read_bytes() + b"// more useful explanation\n" if edit == "comment-only"
                else b"float4 main():SV_Target { return float4(0, 1, 0, 1); }\n")
        source.write_bytes(text)
        status["source"]["currentSha256"] = hashlib.sha256(text).hexdigest()
        fixture._with_fully_validated_candidate(status,
            binary if edit == "comment-only" else _dxbc(b"green executable"), revision=6)
        workspace = Path(status["workspacePath"])
        expected_raw = status["latestCompiled"]["rawSha256"]
        state = fixture.service.status()
        workspace_service = mock.Mock()
        workspace_service.status.side_effect = lambda *a: copy.deepcopy(status)
        published = []

        def publish(workspace_path, revision, raw, replacement, **kwargs):
            assert (workspace_path, revision, raw) == (str(workspace), 6, expected_raw)
            with fixture.service.pin_active_project(state["workspaceId"], state["projectRoot"]):
                committed = project_restore._register_applied_workspace_with_readback(
                    fixture.service, workspace_service, workspace, copy.deepcopy(status))
            published.append(committed["knowledge"])
            return {"projectCommitted": True, "knowledge": committed["knowledge"]}

        monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", publish)
        monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *a, **kw: "current")
        monkeypatch.setattr(server, "_shader_live_edit_apply_candidate", lambda *a: _compatible_result(workspace, 6, expected_raw))
        monkeypatch.setattr(server, "_workspace_active_capture_context", lambda *a, **k: {})
        monkeypatch.setattr(server, "_ensure_active_workspace_project_capture", lambda *a, **k: None)
        monkeypatch.setattr(server, "_require_same_active_capture", lambda *a, **k: None)
        frozen = internal_tool_service._freeze_shader_live_session(
            "qrenderdoc-save", "b" * 32, os.getpid(), str(source.parent / "frame.rdc"))
        result = frozen.apply_candidate(str(workspace), 6, expected_raw)
        assert result["projectPublication"]["status"] == "committed", result
        assert published[0]["knowledgeId"] != previous["knowledge"]["knowledgeId"]
        other_catalog, unused_shader, unused_capture = fixture._catalog(
            "save-other-rdc", _dxbc(b"original program"), resource_id=9181)
        matches = fixture.service.import_capture(other_catalog)["reusePlan"]["matches"]
        candidate = matches[0]["workspaceCandidates"][0]
        assert candidate["knowledgeId"] == published[0]["knowledgeId"]
        assert candidate["sourceSha256"] == hashlib.sha256(text).hexdigest()
        assert published[0]["compiledCandidate"]["rawSha256"] == expected_raw
        assert published[0]["compiledCandidate"]["sourceRevision"] == 6
    finally:
        fixture.tearDown()
