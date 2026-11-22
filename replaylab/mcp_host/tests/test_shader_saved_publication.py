"""Project restore consumes successful saved source even after its RDC closes."""
import copy
import hashlib
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from src import server, shader_saved_publication as publication
import test_active_projects as project_fixtures
from test_active_projects import _dxbc
from test_shader_live_edit import _compatible_result


@pytest.fixture
def rig():
    f = project_fixtures.ActiveReplayProjectTests("test_applied_user_edit_publishes_current_source_without_old_ai_gate")
    f.setUp()
    f._bound()
    catalog, shader, capture_id = f._catalog("pending-origin", _dxbc(b"original program"), resource_id=181)
    f.service.import_capture(catalog)
    status = f._workspace_status("pending-source", capture_id, shader)
    original_workspace = Path(status["workspacePath"])
    workspace = Path(f.service.status()["paths"]["shaderWorkspaces"]) / "pending-source"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(original_workspace), str(workspace))
    status["workspacePath"] = str(workspace)
    status["manifestPath"] = str(workspace / "manifest.json")
    status["source"]["path"] = str(workspace / "source" / "shader.hlsl")
    status["representation"] = {"lane": "executable-editable", "compileEligible": True}
    status["capture"]["capturePath"] = str(f.root / "origin.rdc")
    f._with_fully_validated_candidate(status, _dxbc(b"old candidate"))
    initial = f.service.register_workspace(status)
    binding = {"instanceId": "qrenderdoc-test", "captureSessionId": "a" * 32,
               "capturePath": status["capture"]["capturePath"]}
    host = SimpleNamespace(
        _active_project_service=lambda: f.service,
        _require_managed_active_workspace=lambda active, path: Path(path),
        _shader_workspace_service=lambda: SimpleNamespace(status=lambda path: copy.deepcopy(status)),
        _require_shader_candidate_binding=server._require_shader_candidate_binding,
        _workspace_active_capture_context=lambda *a, **k: {"capture": dict(binding)},
        _ensure_active_workspace_project_capture=lambda *a: None,
        _fresh_shader_candidate_validation=mock.Mock(side_effect=AssertionError("already validated")),
    )
    active = f.service.status()
    replay = f.service.replay_service()

    def prepare(revision):
        source = Path(status["source"]["path"])
        source.write_text("float4 main():SV_Target { return %d; }\n" % revision, encoding="utf-8")
        status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        f._with_fully_validated_candidate(status, _dxbc(("candidate-%d" % revision).encode()), revision=revision)
        intent = publication.prepare(host, status["workspacePath"], revision, status["latestCompiled"]["rawSha256"], binding)
        result = _compatible_result(Path(status["workspacePath"]), revision, status["latestCompiled"]["rawSha256"])
        result["binding"]["sourceSha256"] = status["source"]["currentSha256"]
        result["replacement"].update(originalResourceId=181,
            originalRawSha256=status["baseline"]["rawSha256"], captureSessionId=binding["captureSessionId"])
        return intent, result

    yield SimpleNamespace(f=f, status=status, host=host, replay=replay, initial=initial,
                          active=active, prepare=prepare, binding=binding)
    with publication._GUARD:
        for event in publication._INFLIGHT.values():
            event.set()
        publication._INFLIGHT.clear()
    f.tearDown()


def head(rig):
    return rig.replay.current_workspace_source_head(rig.initial["knowledge"]["knowledgeId"])["knowledge"]


def test_successful_save_publishes_frozen_source_after_capture_and_game_switch(rig):
    intent, result = rig.prepare(6)
    saved_sha = result["binding"]["sourceSha256"]
    # A newer, uncompiled editor buffer must not replace the already applied
    # revision in its immutable publication snapshot.
    source = Path(rig.status["source"]["path"])
    source.write_text("uncompiled next edit", encoding="utf-8")
    other = rig.f.service.create_project(name="Another game", game_id="other", build_id="new", api_family="vulkan")
    with mock.patch.object(server.ipc, "call", side_effect=AssertionError("publication cannot access replay")):
        receipt = publication.complete(intent, result)
    assert receipt["status"] == "committed"
    assert rig.f.service.status()["workspaceId"] == other["workspaceId"]
    assert rig.f.service.replay_service().status()["knowledgeCounts"]["workspaces"] == 0
    assert head(rig)["sourceSha256"] == saved_sha
    assert source.read_text() == "uncompiled next edit"
    catalog, unused_shader, unused_capture = rig.f._catalog("pending-next-rdc", _dxbc(b"original program"), resource_id=9181)
    rig.replay.import_capture(catalog)
    plan = rig.replay.reuse_plan(unused_capture)
    assert plan["matches"][0]["workspaceCandidates"][0]["sourceSha256"] == saved_sha
    assert not intent.exists()


def test_restore_drains_durable_ready_intent_after_retired_job_and_newer_edit(rig, monkeypatch):
    intent, result = rig.prepare(6)
    saved_sha = result["binding"]["sourceSha256"]
    with mock.patch.object(publication, "_publish", side_effect=OSError("temporary project write lock")):
        with pytest.raises(OSError):
            publication.complete(intent, result)
    assert publication._read(intent)["state"] == "ready"
    assert str(intent) not in publication._INFLIGHT
    Path(rig.status["source"]["path"]).write_text("later unvalidated text", encoding="utf-8")
    with mock.patch.object(server.ipc, "call", side_effect=AssertionError("retry cannot access old capture")):
        drained = publication.drain(Path(rig.active["projectRoot"]), rig.replay.root)
    assert drained["committed"] == 1
    assert head(rig)["sourceSha256"] == saved_sha
    assert not intent.exists()


def test_restore_waits_for_preceding_inflight_save_then_sees_its_new_source(rig):
    intent, result = rig.prepare(6)
    waiting = threading.Event()
    done = []
    worker = threading.Thread(target=lambda: done.append(publication.drain(
        Path(rig.active["projectRoot"]), rig.replay.root, lambda message: waiting.set())))
    worker.start()
    try:
        assert waiting.wait(1)
        assert not done
        publication.complete(intent, result)
        worker.join(3)
        assert not worker.is_alive() and len(done) == 1
        assert head(rig)["sourceSha256"] == result["binding"]["sourceSha256"]
    finally:
        publication.cancel(intent) if intent.exists() else None
        worker.join(3)


def test_older_delayed_success_cannot_overwrite_newest_success(rig):
    older, first = rig.prepare(6)
    with mock.patch.object(publication, "_publish", side_effect=OSError("delayed")):
        with pytest.raises(OSError):
            publication.complete(older, first)
    newer, second = rig.prepare(7)
    publication.complete(newer, second)
    drained = publication.drain(Path(rig.active["projectRoot"]), rig.replay.root)
    assert drained["superseded"] == 1
    assert head(rig)["sourceSha256"] == second["binding"]["sourceSha256"]


@pytest.mark.parametrize("invalid", ["failed-apply", "wrong-source", "wrong-capture", "wrong-original"])
def test_failed_or_foreign_apply_cannot_publish_source(rig, invalid):
    intent, result = rig.prepare(6)
    if invalid == "failed-apply":
        result["applied"] = False
    elif invalid == "wrong-source":
        result["binding"]["sourceSha256"] = "0" * 64
    elif invalid == "wrong-capture":
        result["replacement"]["captureSessionId"] = "0" * 32
    else:
        result["replacement"]["originalRawSha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="successful native apply"):
        publication.complete(intent, result)
    publication.drain(Path(rig.active["projectRoot"]), rig.replay.root)
    assert head(rig) == rig.initial["knowledge"]


def test_initial_head_uses_absence_cas_instead_of_overwriting_a_new_head(rig):
    key = rig.initial["head"]["headKey"]
    with pytest.raises(Exception, match="compare-and-swap conflict"):
        rig.replay.register_workspace(rig.status,
            expected_head={"headKey": key, "generation": 0, "knowledgeId": ""})


def test_prepared_draft_without_native_success_is_not_restored(rig, monkeypatch):
    intent, unused_result = rig.prepare(6)
    with publication._GUARD:
        publication._INFLIGHT.pop(str(intent))
    from src import project_restore
    monkeypatch.setattr(project_restore, "_load_workspace_shader_pair_validation", lambda *a: None)
    publication.drain(Path(rig.active["projectRoot"]), rig.replay.root)
    assert head(rig) == rig.initial["knowledge"]
    assert not intent.exists()


def test_snapshot_preparation_failure_cleans_owned_files_and_keeps_project_head(rig):
    rig.status["assemblyDisassemblyArtifacts"] = [{
        "path": str(Path(rig.status["workspacePath"]) / "missing-assembly.txt")}]
    with pytest.raises(FileNotFoundError):
        rig.prepare(6)
    directory = Path(rig.status["workspacePath"]) / "watch" / "project-publications"
    assert not list(directory.iterdir())
    assert head(rig) == rig.initial["knowledge"]


@pytest.mark.parametrize("outcome", ["capture-switched", "write-delayed", "rejected"])
def test_live_callback_retains_project_commit_after_origin_capture_closes(rig, monkeypatch, outcome):
    from src import internal_tool_service, shader_live_edit, project_restore
    from test_shader_manual_publication import prepare_manual
    initial_intent, applied = prepare_manual(rig, 6)
    publication.cancel(initial_intent)
    for name, value in vars(rig.host).items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(shader_live_edit, "current_live_apply_owner", lambda *a: object())
    state = ["current"]
    monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *a, **kw: state[0])
    sync = mock.Mock(side_effect=AssertionError("old capture is closed"))
    monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", sync)

    def apply(*args):
        state[0] = "expired"
        Path(rig.status["source"]["path"]).write_text("later uncompiled source", encoding="utf-8")
        if outcome == "rejected":
            applied["applied"] = False
        return copy.deepcopy(applied)

    monkeypatch.setattr(server, "_shader_live_edit_apply_candidate", apply)
    frozen = internal_tool_service._freeze_shader_live_session(
        rig.binding["instanceId"], rig.binding["captureSessionId"], os.getpid(), rig.binding["capturePath"])
    if outcome == "write-delayed":
        with mock.patch.object(publication, "_publish", side_effect=OSError("temporary disk lock")):
            result = frozen.apply_candidate(rig.status["workspacePath"], 6, applied["binding"]["candidateRawSha256"])
        assert result["projectPublication"]["status"] == "pending"
        publication.drain(Path(rig.active["projectRoot"]), rig.replay.root)
    else:
        result = frozen.apply_candidate(rig.status["workspacePath"], 6, applied["binding"]["candidateRawSha256"])
    sync.assert_not_called()
    assert result["applied"] is (outcome != "rejected")
    if outcome == "rejected":
        assert head(rig) == rig.initial["knowledge"]
    else:
        assert head(rig)["sourceSha256"] == applied["binding"]["sourceSha256"]
        if outcome == "capture-switched":
            assert result["projectPublication"]["status"] == "committed"


def test_manual_prepare_failure_remains_visible_after_successful_native_apply(rig, monkeypatch):
    from src import internal_tool_service, shader_live_edit, project_restore
    from test_shader_manual_publication import prepare_manual
    intent, applied = prepare_manual(rig, 6)
    publication.cancel(intent)
    monkeypatch.setattr(shader_live_edit, "current_live_apply_owner", lambda *args: object())
    monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *args, **kwargs: "current")
    monkeypatch.setattr(publication, "prepare", mock.Mock(side_effect=OSError("project disk unavailable")))
    monkeypatch.setattr(server, "_shader_live_edit_apply_candidate", lambda *args: copy.deepcopy(applied))
    sync = mock.Mock(side_effect=AssertionError("manual saves cannot use strict publication"))
    monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", sync)
    frozen = internal_tool_service._freeze_shader_live_session(
        rig.binding["instanceId"], rig.binding["captureSessionId"], os.getpid(), rig.binding["capturePath"])
    result = frozen.apply_candidate(rig.status["workspacePath"], 6, applied["binding"]["candidateRawSha256"])
    assert result["applied"] is True
    assert result["projectPublication"] == {"status": "pending", "message": "project disk unavailable"}
    assert head(rig) == rig.initial["knowledge"]
    sync.assert_not_called()
