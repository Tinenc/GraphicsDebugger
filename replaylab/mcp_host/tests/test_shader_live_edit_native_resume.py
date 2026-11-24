"""Offline Native-resume boundary tests with real Temp Source and UI readers.

The Native gateway is a controlled read-only inventory. These tests do not
compile, apply, contact qrenderdoc or use the developer's project state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from src import internal_tool_service, ipc_client, server
from src.ipc_client import bind_direct_tool_call
from src.shader_live_edit import ShaderLiveEditManager, ShaderLiveEditSessionUnavailable
from src.shader_workspace import ShaderWorkspaceWatchManager
from src.shader_workspace.status_snapshot import (
    WorkspaceStatusSnapshotChanged, bind_status_snapshot, capture_status_snapshot,
)
from test_shader_console import ShaderConsoleFixture, build_shader_flow_graph, console
from test_shader_live_edit import _compatible_result, _wait_until


class _PortableResumeConsoleFixture(ShaderConsoleFixture):
    @property
    def capture_binding(self):
        return console.normalize_capture_binding({
            "instanceId": "1234-fixture", "captureSessionId": "8" * 32,
            "capturePath": str((self.root / "capture.rdc").resolve()),
        })

    def __init__(self, root):
        super().__init__(root)
        capture_path = self.capture_binding["capturePath"]
        manifest_path = self.workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf8"))
        manifest["capture"]["capturePath"] = capture_path
        self._write(manifest_path, manifest)
        registry = json.loads(self.registry.read_text(encoding="utf8"))
        for entry in registry["entries"]:
            entry["workspaceIdentity"]["capturePath"] = capture_path
            entry["workspaceIdentitySha256"] = console._canonical_sha256(entry["workspaceIdentity"])
        self._write(self.registry, registry)


class ResumeFixture:
    def __init__(self, tmp_path):
        self.ui = _PortableResumeConsoleFixture(tmp_path)
        self.ui.write_editable_draft()
        self.root = self.ui.workspace.resolve()
        self.source = self.ui.source.resolve()
        self.candidate = self.root / "candidate.bin"
        self.candidate.write_bytes(b"controlled existing candidate payload")
        self.raw_sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.binding = dict(self.ui.capture_binding)
        self.manifest = self.root / "manifest.json"
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value.update(revision=7, stage="fragment", entryPoint="MainPS")
        value["latestCompiled"] = {
            "revision": 7, "sourceSha256": self.source_sha,
            "rawSha256": self.raw_sha, "candidateBlobPath": str(self.candidate),
            "replayAbiValidated": True, "abiCompatible": True,
            "requiresReplayAbiValidation": False,
            "abiGate": {"status": "compatible", "requestId": "4" * 64,
                        "reportSha256": "5" * 64},
        }
        self.ui._write(self.manifest, value)
        self.value = value
        self.inventory = {
            "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
            "schemaVersion": 1, "captureId": self.ui.CAPTURE_ID,
            "captureSessionId": self.binding["captureSessionId"],
            "originalShadersPreserved": True, "replayMutation": False,
            "mutationEpoch": 3, "count": 1,
            "replacements": [{
                "replacementId": "a" * 32, "workspaceId": self.ui.WORKSPACE_ID,
                # An older Native revision may contain the same current bytes.
                "revision": 3, "candidateRawSha256": self.raw_sha,
                "originalResourceId": 101, "targetResourceId": 909,
                "originalRawSha256": self.ui.RAW_SHA256,
                "physicalAbiSha256": "d" * 64,
                "stage": "fragment", "entryPoint": "MainPS",
                "resourceScoped": True, "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "originalShaderPreserved": True,
                "postApplyValidationFailed": False,
            }],
        }
        self.mailbox_root = tmp_path / "instances"
        directory = self.mailbox_root / self.binding["instanceId"]
        directory.mkdir(parents=True)
        self.mailbox = directory / "instance.json"
        self.mailbox_value = {
            **self.binding, "pid": 1234, "captureLoaded": True,
        }
        self.write_mailbox()
        self.native = mock.Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(self.inventory))
        self.service = mock.Mock()
        self.service.status.side_effect = self.status
        self.service.compile.side_effect = AssertionError("resume must not compile")
        config = tmp_path / "tools.json"
        config.write_text("{}", encoding="utf-8")
        self.service.registry = SimpleNamespace(path=config, operations=lambda: ())
        self.service.route_config_path = None
        self.service._tool_config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
        self.service._route_config_sha256 = ""

    def write_mailbox(self):
        self.mailbox.write_text(json.dumps(self.mailbox_value), encoding="utf-8")

    def status(self, path):
        assert str(self.root) == str(path)
        value = copy.deepcopy(self.value)
        value["workspacePath"] = str(self.root)
        value["source"] = {
            "present": True, "path": str(self.source),
            "currentSha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "decompiledSha256": self.source_sha, "lane": "executable-editable",
        }
        value["representation"] = {
            "lane": "executable-editable", "compileEligible": True,
        }
        return value

    def recover(self):
        with bind_direct_tool_call(self.binding["instanceId"], self.binding["captureSessionId"]):
            return server._shader_live_edit_recover_active_candidate(str(self.root), self.binding)

    def patches(self):
        return mock.patch.multiple(server, _shader_workspace_service=lambda: self.service)


def test_native_resume_reads_current_files_and_keeps_physical_revision(tmp_path):
    f = ResumeFixture(tmp_path)
    before = {p: p.read_bytes() for p in (f.source, f.candidate, f.manifest)}
    inventory = copy.deepcopy(f.inventory)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        proof = f.recover()
    assert proof["operation"] == "resume-existing-shared-resource"
    assert proof["mutationPerformed"] is False and proof["nativeMutationEpoch"] == 3
    assert proof["binding"]["revision"] == 7
    assert proof["replacement"]["revision"] == 3
    assert proof["binding"]["sourceSha256"] == f.source_sha
    assert proof["workspaceValidationReused"] is False
    f.native.assert_called_once_with("get_shared_shader_replacements", {
        "expected_capture_id": f.ui.CAPTURE_ID,
        "expected_capture_session_id": f.binding["captureSessionId"],
    }, timeout=30.0)
    assert f.inventory == inventory
    assert all(p.read_bytes() == raw for p, raw in before.items())
    f.service.compile.assert_not_called()


def test_native_resume_cannot_adopt_failed_installed_candidate(tmp_path):
    f = ResumeFixture(tmp_path)
    f.inventory["replacements"][0]["postApplyValidationFailed"] = True
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        assert f.recover() is None
    f.service.compile.assert_not_called()


def test_owner_start_native_resume_reuses_one_full_status_validation(tmp_path):
    f = ResumeFixture(tmp_path)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        with internal_tool_service._shader_live_edit_start_context(str(f.root)):
            proof = f.recover()
        assert f.service.status.call_count == 1
        assert proof["replacement"]["revision"] == 3
        assert proof["binding"]["revision"] == 7
        assert proof["workspaceValidationReused"] is True
        # The private receipt expires with owner-start; ordinary later resume
        # must validate again, even for the same path and source bytes.
        f.recover()
        assert f.service.status.call_count == 2
    assert f.native.call_count == 2
    f.service.compile.assert_not_called()


@pytest.mark.parametrize("target", ["source", "candidate", "manifest"])
def test_owner_start_snapshot_rejects_edits_with_preserved_mtime(tmp_path, target):
    f = ResumeFixture(tmp_path)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        snapshot = capture_status_snapshot(f.service, f.root)
        path = getattr(f, target)
        info = path.stat()
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with bind_status_snapshot(snapshot), pytest.raises(WorkspaceStatusSnapshotChanged):
            f.recover()
    assert f.service.status.call_count == 1
    f.native.assert_not_called()


@pytest.mark.parametrize("during_query", [False, True])
def test_owner_start_native_resume_rechecks_operation_lock(tmp_path, during_query):
    f = ResumeFixture(tmp_path)
    marker = f.root / ".workspace-operation.lock"
    def inventory(*args, **kwargs):
        marker.write_bytes(b"unknown active operation")
        return copy.deepcopy(f.inventory)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        snapshot = capture_status_snapshot(f.service, f.root)
        if during_query:
            f.native.side_effect = inventory
        else:
            marker.write_bytes(b"unknown active operation")
        with bind_status_snapshot(snapshot), pytest.raises(RuntimeError, match="changed during Native Shader resume"):
            f.recover()
    assert f.service.status.call_count == 1
    assert f.native.call_count == int(during_query)


@pytest.mark.parametrize("field,value", [
    ("candidateRawSha256", "0" * 64), ("originalRawSha256", "0" * 64),
    ("originalResourceId", 102), ("workspaceId", "0" * 32),
    ("resourceScoped", False), ("eventScoped", True),
])
def test_unmatched_native_mapping_cannot_seed_readiness(tmp_path, field, value):
    f = ResumeFixture(tmp_path)
    f.inventory["replacements"][0][field] = value
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        assert f.recover() is None


@pytest.mark.parametrize("target", ["source", "candidate", "manifest"])
def test_file_edit_during_native_query_is_preserved_and_rejected(tmp_path, target):
    f = ResumeFixture(tmp_path)
    path = getattr(f, target)
    changed = path.read_bytes() + b"\nexternal edit"
    def edited_inventory(*args, **kwargs):
        path.write_bytes(changed)
        return copy.deepcopy(f.inventory)
    with f.patches(), mock.patch.object(server.ipc, "call", side_effect=edited_inventory):
        with pytest.raises(RuntimeError, match="changed during Native Shader resume"):
            f.recover()
    assert path.read_bytes() == changed


def test_unvalidated_source_and_wrong_direct_owner_never_query_native(tmp_path):
    f = ResumeFixture(tmp_path)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        with bind_direct_tool_call("foreign-instance", f.binding["captureSessionId"]):
            with pytest.raises(RuntimeError, match="frozen instance/session"):
                server._shader_live_edit_recover_active_candidate(str(f.root), f.binding)
        f.value["latestCompiled"]["abiCompatible"] = False
        assert f.recover() is None
        f.value["latestCompiled"]["abiCompatible"] = True
        f.source.write_bytes(f.source.read_bytes() + b"\nunsaved externally")
        assert f.recover() is None
    f.native.assert_not_called()


def test_changed_include_skips_optional_resume_but_keeps_apply_binding_strict(tmp_path):
    f = ResumeFixture(tmp_path)
    f.value["sourceDependenciesCurrent"] = False
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        assert f.recover() is None
    f.native.assert_not_called()
    with pytest.raises(RuntimeError, match="include files changed"):
        server._require_shader_candidate_binding(f.status(str(f.root)), None)


def test_manual_save_with_old_valid_candidate_starts_fresh_compile_and_apply(tmp_path):
    """Current IDE text differs from an ABI-valid historical candidate at startup."""
    from src.shader_workspace.organization import ORGANIZATION_GATE_SCHEMA, ORGANIZATION_SESSION_SCHEMA
    f = ResumeFixture(tmp_path)
    edited = f.source.read_bytes() + b"\n// saved user change before live session starts\n"
    f.source.write_bytes(edited)
    edited_sha = hashlib.sha256(edited).hexdigest()
    organization_id = "e" * 32
    session_path = f.root / "ai/organization-sessions" / organization_id / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({"schema": ORGANIZATION_SESSION_SCHEMA,
        "organizationId": organization_id, "state": "drafting"}), encoding="utf-8")
    (f.root / "ai/organize-code-gate.json").write_text(json.dumps({
        "schema": ORGANIZATION_GATE_SCHEMA, "organizationId": organization_id,
        "decision": "editable-draft"}), encoding="utf-8")
    identity = json.loads(f.ui.registry.read_text(encoding="utf-8"))["entries"][0]["workspaceIdentity"]
    old_inventory = copy.deepcopy(f.inventory)
    def compile_current(path):
        assert str(path) == str(f.root) and f.source.read_bytes() == edited
        f.candidate.write_bytes(b"compiled:" + edited)
        f.value["revision"] = 8
        f.value["latestCompiled"].update(revision=8, sourceSha256=edited_sha,
            rawSha256=hashlib.sha256(f.candidate.read_bytes()).hexdigest(),
            replayAbiValidated=False, abiCompatible=False, requiresReplayAbiValidation=True)
        f.ui._write(f.manifest, f.value)
        return copy.deepcopy(f.value["latestCompiled"])
    f.service.compile_for_live_edit.side_effect = compile_current
    apply = mock.Mock(side_effect=lambda path, revision, raw: _compatible_result(f.root, revision, raw))
    watch = ShaderWorkspaceWatchManager(lambda: f.service)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native), \
            mock.patch.object(ipc_client, "INSTANCE_ROOT", str(f.mailbox_root)), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=True):
        session = internal_tool_service._freeze_shader_live_session(
            f.binding["instanceId"], f.binding["captureSessionId"], 1234, f.binding["capturePath"])
        session = type(session)(session.binding, session.state, apply, session.recover_active_candidate)
        manager = ShaderLiveEditManager(watch, apply, poll_seconds=0.005, join_seconds=0.5,
            bind_session=lambda unused: session, registry_path=f.ui.registry,
            workspace_identity=lambda unused: identity,
            start_context=internal_tool_service._shader_live_edit_start_context)
        try:
            queued = manager.compile_saved_source(str(f.root), f.ui.WORKSPACE_ID, edited_sha,
                                                  capture_binding=f.binding)
            assert queued["compileScheduled"] is True
            _wait_until(lambda: manager.poll(queued["liveEditId"])["applySuccesses"] == 1, timeout=5.0)
            assert manager.poll(queued["liveEditId"])["latestHandledRevision"] == 8
            f.service.compile_for_live_edit.assert_called_once()
            f.service.compile.assert_not_called()
            apply.assert_called_once_with(str(f.root), 8, f.value["latestCompiled"]["rawSha256"])
            assert f.source.read_bytes() == edited
            assert f.inventory == old_inventory
            f.native.assert_not_called()
        finally:
            manager.shutdown()
            watch.shutdown()


@pytest.mark.parametrize("fault", ["session", "zero-epoch", "duplicate"])
def test_invalid_or_ambiguous_native_proof_is_rejected(tmp_path, fault):
    f = ResumeFixture(tmp_path)
    if fault == "session":
        f.inventory["captureSessionId"] = "f" * 32
    elif fault == "zero-epoch":
        f.inventory["mutationEpoch"] = 0
    else:
        f.inventory["replacements"].append(copy.deepcopy(f.inventory["replacements"][0]))
        f.inventory["count"] = 2
    with f.patches(), mock.patch.object(server.ipc, "call", f.native):
        with pytest.raises(RuntimeError, match="Native Shader resume"):
            f.recover()


@pytest.mark.parametrize("change", ["session", "closed", "path", "during-query"])
def test_frozen_singleton_closure_rechecks_actual_mailbox(tmp_path, change):
    f = ResumeFixture(tmp_path)
    with f.patches(), mock.patch.object(server.ipc, "call", f.native), \
            mock.patch.object(ipc_client, "INSTANCE_ROOT", str(f.mailbox_root)), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=True):
        session = internal_tool_service._freeze_shader_live_session(
            f.binding["instanceId"], f.binding["captureSessionId"], 1234,
            f.binding["capturePath"])
        if change == "during-query":
            def expire(*args, **kwargs):
                f.mailbox_value["captureSessionId"] = "f" * 32
                f.write_mailbox()
                return copy.deepcopy(f.inventory)
            f.native.side_effect = expire
        elif change == "session":
            f.mailbox_value["captureSessionId"] = "f" * 32
        elif change == "closed":
            f.mailbox_value["captureLoaded"] = False
        else:
            f.mailbox_value["capturePath"] = str(tmp_path / "other.rdc")
        f.write_mailbox()
        with pytest.raises(ShaderLiveEditSessionUnavailable):
            session.recover_active_candidate(str(f.root))
        assert ipc_client._CALL_BINDING.get() is None
    assert f.native.call_count == (1 if change == "during-query" else 0)


def test_real_watcher_singleton_and_ui_reader_resume_without_apply(tmp_path):
    f = ResumeFixture(tmp_path)
    watch = ShaderWorkspaceWatchManager(lambda: f.service)
    identity = json.loads(f.ui.registry.read_text(encoding="utf-8"))["entries"][0]["workspaceIdentity"]
    graph = build_shader_flow_graph(
        f.source.read_bytes().decode("utf-8"), workspace_id=f.ui.WORKSPACE_ID,
        source_path="source/shader.hlsl", source_sha256=f.source_sha,
        source_revision=7, stage="fragment", entry_point="MainPS",
        generated_at="2026-10-12T00:00:00Z", origin="editable-draft")
    f.ui._write(f.root / "ai" / "flow-graph.json", graph)
    apply = mock.Mock(side_effect=AssertionError("resume must not apply"))
    with f.patches(), mock.patch.object(server.ipc, "call", f.native), \
            mock.patch.object(ipc_client, "INSTANCE_ROOT", str(f.mailbox_root)), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=True):
        session = internal_tool_service._freeze_shader_live_session(
            f.binding["instanceId"], f.binding["captureSessionId"], 1234,
            f.binding["capturePath"])
        # Use the production frozen recovery closure; a separate apply callback
        # makes any accidental GPU mutation fail this offline test.
        session = type(session)(session.binding, session.state, apply, session.recover_active_candidate)
        manager = ShaderLiveEditManager(
            watch, apply, poll_seconds=0.005, join_seconds=0.5,
            bind_session=lambda unused: session, registry_path=f.ui.registry,
            workspace_identity=lambda unused: identity,
            start_context=internal_tool_service._shader_live_edit_start_context,
            phase_context=internal_tool_service._shader_live_start_phase)
        timing = {"phasesMs": {}, "failedPhase": ""}
        timing_token = internal_tool_service._SHADER_LIVE_START_TIMING.set(timing)
        try:
            started = manager.start(str(f.root), 100)
            assert f.service.status.call_count == 1
            assert {"statusSnapshot", "watcherStart", "nativeRecovery", "resumeValidation",
                    "nativeInventory", "editorPublication"} <= set(timing["phasesMs"])
            assert timing["failedPhase"] == ""
            _wait_until(lambda: manager.poll(started["liveEditId"])["state"] == "running")
            resumed = manager.poll(started["liveEditId"])
            assert resumed["activeReplacement"]["revision"] == 3
            assert resumed["latestHandledRevision"] == 7
            assert resumed["lastApply"]["mutationPerformed"] is False
            assert resumed["lastApply"]["nativeMutationEpoch"] == 3
            snapshot = console.load_console_snapshot(str(f.ui.registry), capture_binding=f.binding)
            workspace = snapshot["workspaces"][0]
            assert workspace["autoApplyReady"] is True, snapshot["errors"]
            opened = console.load_authorized_source_document(
                str(f.ui.registry), f.ui.AUTHORIZATION_ID, f.ui.WORKSPACE_ID,
                capture_binding=f.binding)
            assert opened["liveEditId"] == started["liveEditId"]
            assert opened["rawSha256"] == f.source_sha
            gate = console._validate_editable_source_gate(str(f.root), identity, str(f.source))
            assert gate["finalRevision"] == 7
            read_graph, unused_path = console._validate_shader_flow_graph(
                str(f.root), identity, str(f.source), gate)
            assert read_graph["sourceRevision"] == 7
            assert read_graph["sourceSha256"] == f.source_sha
            assert resumed["applyAttempts"] == resumed["applySuccesses"] == 0
            assert watch.poll(started["watchId"])["compileAttempts"] == 0
            apply.assert_not_called()
            f.service.compile.assert_not_called()
            assert f.inventory["mutationEpoch"] == 3
            # The actual reader refuses a candidate-payload mismatch, even when
            # Source, numeric revisions and the old gate still match.
            manifest = copy.deepcopy(f.value)
            manifest["latestCompiled"]["rawSha256"] = "0" * 64
            f.ui._write(f.manifest, manifest)
            stale = console.load_console_snapshot(str(f.ui.registry), capture_binding=f.binding)
            assert stale["workspaces"][0]["autoApplyReady"] is False
        finally:
            internal_tool_service._SHADER_LIVE_START_TIMING.reset(timing_token)
            manager.shutdown()
            watch.shutdown()
