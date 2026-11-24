from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src import internal_tool_service, server
from src.shader_workspace.status_snapshot import (
    WorkspaceStatusSnapshotChanged, bind_status_snapshot, bound_status_snapshot,
    capture_status_snapshot,
)
from src.shader_workspace.watcher import ShaderWorkspaceWatchManager
from tests.test_shader_workspace_watcher import _FakeWorkspaceService
from tests.test_shader_live_edit_capture_preflight import _CapturePreflightRig


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "source").mkdir(parents=True)
    (root / "source" / "shader.hlsl").write_bytes(b"float4 main() : SV_Target { return 0; }\n")
    (root / "baseline.bin").write_bytes(b"baseline")
    (root / "candidate.bin").write_bytes(b"candidate")
    (root / "report.json").write_text('{"valid": true}', encoding="utf-8")
    job = root / "jobs" / "evidence"
    job.mkdir(parents=True)
    (job / "job.json").write_text('{"operation": "compile"}', encoding="utf-8")
    (job / "output.txt").write_bytes(b"immutable evidence")
    config = tmp_path / "tools.json"
    config.write_text("{}", encoding="utf-8")
    routes = tmp_path / "routes.json"
    routes.write_text("{}", encoding="utf-8")
    executable = tmp_path / "compiler.exe"
    executable.write_bytes(b"pinned tool")
    adapter = tmp_path / "adapter.py"
    adapter.write_bytes(b"pinned adapter")
    raw = tmp_path / "shader.bin"
    raw.write_bytes(b"captured shader")
    key = {"resourceId": 1, "stage": "fragment", "entryPoint": "main",
           "rawSha256": hashlib.sha256(raw.read_bytes()).hexdigest()}
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"shaders": [{**key, "rawPath": raw.name}]}), encoding="utf-8")
    manifest = {"source": {"path": "source/shader.hlsl"},
                "baseline": {"path": "baseline.bin"},
                "latestCompiled": {"blobPath": "candidate.bin", "reportPath": "report.json"},
                "history": [{"jobPath": "jobs/evidence", "outputPath": "jobs/evidence/output.txt"}],
                "capture": {"catalogPath": str(catalog), "capturePath": str(tmp_path / "frame.rdc")},
                "shader": key}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    service = _FakeWorkspaceService(root)
    service.registry = SimpleNamespace(path=config, operations=lambda: (
        SimpleNamespace(executable=executable, fixed_file_hashes=((adapter, "unused"),)),))
    service.route_config_path = routes
    service.status = mock.Mock(wraps=service.status)
    return root, service


@pytest.mark.parametrize("target", [
    "workspace/manifest.json", "workspace/source/shader.hlsl", "workspace/baseline.bin",
    "workspace/candidate.bin", "workspace/report.json", "workspace/jobs/evidence/job.json",
    "workspace/jobs/evidence/output.txt", "tools.json", "routes.json", "compiler.exe",
    "adapter.py", "catalog.json", "shader.bin",
])
def test_snapshot_detects_equal_size_content_change_with_restored_mtime(workspace, target):
    root, service = workspace
    snapshot = capture_status_snapshot(service, root)
    path = root.parent / target
    before = path.stat()
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(WorkspaceStatusSnapshotChanged, match="contents changed"):
        snapshot.checked_status(service)
    assert service.status.call_count == 1


def test_snapshot_read_is_private_and_bound_to_process_configuration(workspace):
    root, service = workspace
    snapshot = capture_status_snapshot(service, root)
    first = snapshot.validated_status()
    first["representation"]["compileEligible"] = False
    assert snapshot.checked_status(service)["representation"]["compileEligible"] is True
    assert bound_status_snapshot(root) is None
    with bind_status_snapshot(snapshot):
        assert bound_status_snapshot(root) is snapshot
        assert bound_status_snapshot(root.parent) is None
    assert bound_status_snapshot(root) is None
    service.route_config_path = root.parent / "different-routes.json"
    with pytest.raises(WorkspaceStatusSnapshotChanged, match="configuration"):
        snapshot.checked_status(service)


def test_status_change_during_mint_is_not_admitted(workspace):
    root, service = workspace
    ordinary = service.status
    def changing_status(path):
        result = ordinary(path)
        (root / "candidate.bin").write_bytes(b"changed candidate")
        return result
    service.status = changing_status
    with pytest.raises(WorkspaceStatusSnapshotChanged, match="during validation"):
        capture_status_snapshot(service, root)


def test_snapshot_does_not_hash_rdc_or_unrelated_job_outputs(workspace):
    root, service = workspace
    snapshot = capture_status_snapshot(service, root)
    (root.parent / "frame.rdc").write_bytes(b"capture file is not workspace status evidence")
    (root / "jobs" / "unrelated").mkdir()
    (root / "jobs" / "unrelated" / "output.txt").write_bytes(b"unrelated")
    (root / "watch").mkdir()
    (root / "watch" / "audit.json").write_text("{}", encoding="utf-8")
    snapshot.checked_status(service)
    assert service.status.call_count == 1


def test_real_watch_start_reuses_validation_and_does_not_compile_unchanged_source(workspace):
    root, service = workspace
    service.latest = {"sourceSha256": service.decompiled_hash, "revision": 1,
                      "rawSha256": hashlib.sha256((root / "candidate.bin").read_bytes()).hexdigest()}
    snapshot = capture_status_snapshot(service, root)
    manager = ShaderWorkspaceWatchManager(lambda: service)
    try:
        with bind_status_snapshot(snapshot):
            started = manager.start(root, 100)
        assert service.status.call_count == 1
        assert started["caughtUp"] is True
        assert started["compileAttempts"] == 0
        assert service.compile_hashes == []
    finally:
        manager.shutdown()


def test_final_source_lease_check_rejects_changed_candidate_before_watching(workspace, monkeypatch):
    from src.shader_workspace import watcher
    root, service = workspace
    snapshot = capture_status_snapshot(service, root)
    ordinary = watcher.WorkspaceSourceTransactionLease.acquire
    def mutate_after_acquire(lease):
        result = ordinary(lease)
        (root / "candidate.bin").write_bytes(b"changed candidate")
        return result
    monkeypatch.setattr(watcher.WorkspaceSourceTransactionLease, "acquire", mutate_after_acquire)
    manager = ShaderWorkspaceWatchManager(lambda: service)
    try:
        with bind_status_snapshot(snapshot), pytest.raises(WorkspaceStatusSnapshotChanged):
            manager.start(root, 100)
        assert manager._jobs == {}
        assert service.compile_hashes == []
    finally:
        manager.shutdown()


@pytest.mark.parametrize("operation", ["start", "resume"])
def test_stale_workspace_is_rejected_before_replay_catalog_or_watch(tmp_path, monkeypatch, operation):
    rig = _CapturePreflightRig(tmp_path, monkeypatch)
    try:
        if operation == "resume":
            rig.authorize_history()
        rig.status["representation"]["compileEligible"] = False
        rig.status["representation"]["refreshRequired"] = True
        if operation == "start":
            with pytest.raises(Exception, match="executable-editable"):
                rig.start()
        else:
            rig.refresh()
        rig.preflight.assert_not_called()
        rig.assert_no_authority()
    finally:
        rig.close()


def test_host_start_context_is_cleared_when_authorization_fails(workspace, monkeypatch):
    root, service = workspace
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    with pytest.raises(RuntimeError, match="failure"):
        with internal_tool_service._shader_live_edit_start_context(str(root)):
            assert bound_status_snapshot(root) is not None
            raise RuntimeError("failure")
    assert bound_status_snapshot(root) is None


def test_same_rdc_new_session_cannot_reuse_old_gpu_replacement(tmp_path, monkeypatch):
    from src.shader_live_edit import ShaderLiveEditError
    rig = _CapturePreflightRig(tmp_path, monkeypatch)
    try:
        started = rig.start()
        job = rig.manager._jobs[started["liveEditId"]]
        original_binding = dict(job.session.binding)
        with job._lock:
            # This is the minimum old receipt that would enter the exact-byte
            # reuse path if the mailbox were allowed to mutate the job binding.
            job._active_apply = {
                "replacement": {"candidateRawSha256": "6" * 64, "revision": 6},
                "validation": {"replayAbiValidated": True},
            }
            rig.publish(session="2" * 32)
            with pytest.raises(ShaderLiveEditError, match="session is no longer current"):
                job._apply(7, "6" * 64)
            assert job._active_apply is None
            assert job._apply_reuses == 0
            assert job.session.binding == original_binding
        rig.apply.assert_not_called()
    finally:
        rig.close()
