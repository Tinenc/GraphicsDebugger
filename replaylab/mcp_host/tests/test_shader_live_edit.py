from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.shader_live_edit import (
    SHARED_APPLY_SCHEMA,
    SHARED_ROLLBACK_TOOL,
    ShaderLiveEditError,
    ShaderLiveEditManager,
)
from src.shader_workspace import ShaderWorkspaceWatchManager
from src.shader_workspace.source_observation import source_observation_transaction


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


class _FakeWatchManager:
    """A deliberately permissive watcher used to test live-edit ownership itself."""

    def __init__(
        self,
        initial_revision: int = 5,
        initial_raw_sha256: str = "5" * 64,
        initial_compile_successes: int = 0,
    ):
        self.initial_revision = initial_revision
        self.initial_raw_sha256 = initial_raw_sha256
        self.initial_compile_successes = initial_compile_successes
        self.start_calls: list[tuple[str, int]] = []
        self.stop_calls: list[str] = []
        self.save_calls: list[tuple[str, str, str]] = []
        self.poll_calls = 0
        self.block_first_start = False
        self.first_start_entered = threading.Event()
        self.release_first_start = threading.Event()
        self._lock = threading.RLock()
        self._snapshots: dict[str, dict] = {}

    def start(self, workspace_path, debounce_ms):
        root = Path(workspace_path).resolve(strict=True)
        with self._lock:
            self.start_calls.append((str(root), debounce_ms))
            start_number = len(self.start_calls)
        if self.block_first_start and start_number == 1:
            self.first_start_entered.set()
            if not self.release_first_start.wait(2.0):
                raise RuntimeError("test did not release the first watcher start")
        with self._lock:
            watch_id = "%032x" % start_number
            initial_source_sha256 = hashlib.sha256(
                ("source:%d:%s" % (
                    self.initial_revision,
                    self.initial_raw_sha256,
                )).encode("ascii")
            ).hexdigest()
            snapshot = {
                "watchId": watch_id,
                "workspacePath": str(root),
                "state": "running",
                "latestCandidateRevision": self.initial_revision,
                "latestCandidateRawSha256": self.initial_raw_sha256,
                "compileInProgress": False,
                "pending": False,
                "caughtUp": True,
                "observedSourceSha256": initial_source_sha256,
                "lastAttemptedSourceSha256": initial_source_sha256,
                "lastCompiledSourceSha256": initial_source_sha256,
                "compileAttempts": self.initial_compile_successes,
                "compileSuccesses": self.initial_compile_successes,
                "compileFailures": 0,
                "lastError": None,
                "events": [],
            }
            self._snapshots[watch_id] = snapshot
            return copy.deepcopy(snapshot)

    def poll(self, watch_id):
        with self._lock:
            self.poll_calls += 1
            return copy.deepcopy(self._snapshots[watch_id])

    def stop(self, watch_id):
        with self._lock:
            self.stop_calls.append(watch_id)
            snapshot = self._snapshots[watch_id]
            snapshot["state"] = "stopped"
            snapshot["compileInProgress"] = False
            return copy.deepcopy(snapshot)

    def save_source(self, watch_id, expected_raw_sha256, text):
        with self._lock:
            snapshot = self._snapshots[watch_id]
            if snapshot["state"] != "running":
                raise RuntimeError("test watcher is not running")
            self.save_calls.append((watch_id, expected_raw_sha256, text))
            raw = text.encode("utf-8")
            return {
                "watchId": watch_id,
                "rawSha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "modifiedNs": 123456789,
                "changed": True,
                "compileScheduled": True,
            }

    def publish(
        self,
        watch_id: str,
        revision: int,
        raw_sha256: str,
        source_sha256: str | None = None,
    ) -> None:
        with self._lock:
            snapshot = self._snapshots[watch_id]
            compiled_source_sha256 = source_sha256 or hashlib.sha256(
                ("source:%d:%s" % (revision, raw_sha256)).encode("ascii")
            ).hexdigest()
            snapshot["compileAttempts"] += 1
            snapshot["compileSuccesses"] += 1
            snapshot.update(
                {
                    "state": "running",
                    "latestCandidateRevision": revision,
                    "latestCandidateRawSha256": raw_sha256,
                    "compileInProgress": False,
                    "pending": False,
                    "caughtUp": True,
                    "observedSourceSha256": compiled_source_sha256,
                    "lastAttemptedSourceSha256": compiled_source_sha256,
                    "lastCompiledSourceSha256": compiled_source_sha256,
                    "lastError": None,
                }
            )
            snapshot["events"].append(
                {
                    "type": "compileSucceeded",
                    "revision": revision,
                    "candidateRawSha256": raw_sha256,
                }
            )

    def observe_pending(self, watch_id: str, source_sha256: str) -> None:
        with self._lock:
            snapshot = self._snapshots[watch_id]
            snapshot.update(
                {
                    "compileInProgress": False,
                    "pending": True,
                    "caughtUp": False,
                    "observedSourceSha256": source_sha256,
                }
            )

    def fail_compile(self, watch_id: str, message: str) -> None:
        with self._lock:
            snapshot = self._snapshots[watch_id]
            snapshot["compileAttempts"] += 1
            snapshot["compileFailures"] += 1
            snapshot["pending"] = False
            snapshot["caughtUp"] = False
            snapshot["lastError"] = {
                "at": "2026-08-24T00:00:00Z",
                "type": "RuntimeError",
                "message": message,
                "diagnostic": {
                    "schema": "renderdoc-ultra-shader-recovery-diagnostic/v1",
                    "schemaVersion": 1,
                    "phase": "watch-compile",
                    "category": "tool-rejected-input",
                    "exceptionType": "RuntimeError",
                    "message": message,
                    "retryable": False,
                    "allowedActions": [
                        "preserve-evidence",
                        "repair-provisional-source-if-compile-route-exists",
                    ],
                },
                "diagnostics": [
                    {
                        "file": "shader.hlsl",
                        "line": 9,
                        "column": 1,
                        "severity": "error",
                        "message": message,
                    }
                ],
            }
            snapshot["events"].append(
                {
                    "type": "compileFailed",
                    "at": "2026-08-24T00:00:00Z",
                    "errorType": "RuntimeError",
                    "message": message,
                    "diagnostic": copy.deepcopy(
                        snapshot["lastError"]["diagnostic"]
                    ),
                    "diagnostics": copy.deepcopy(
                        snapshot["lastError"]["diagnostics"]
                    ),
                }
            )


class _SourceWorkspaceService:
    """Small real-watcher fixture for the save-to-apply integration boundary."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.source = self.root / "source" / "shader.hlsl"
        self.decompiled_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.latest: dict | None = None
        self.compile_hashes: list[str] = []

    def status(self, workspace_path) -> dict:
        root = Path(workspace_path).resolve()
        if root != self.root:
            raise RuntimeError("unexpected workspace")
        current = hashlib.sha256(self.source.read_bytes()).hexdigest()
        return {
            "workspacePath": str(root),
            "source": {
                "present": True,
                "path": str(self.source),
                "currentSha256": current,
                "decompiledSha256": self.decompiled_sha256,
                "lane": "executable-editable",
            },
            "representation": {
                "present": True,
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "latestCompiled": copy.deepcopy(self.latest),
        }

    def compile(self, workspace_path) -> dict:
        if Path(workspace_path).resolve() != self.root:
            raise RuntimeError("unexpected workspace")
        source = self.source.read_bytes()
        source_sha256 = hashlib.sha256(source).hexdigest()
        self.compile_hashes.append(source_sha256)
        revision = len(self.compile_hashes)
        raw_sha256 = hashlib.sha256(b"compiled:" + source).hexdigest()
        self.latest = {
            "revision": revision,
            "sourceSha256": source_sha256,
            "rawSha256": raw_sha256,
        }
        return copy.deepcopy(self.latest)


def _compatible_result(workspace: Path, revision: int, raw_sha256: str) -> dict:
    replacement_id = "%032x" % (0x1000 + revision)
    return {
        "schema": SHARED_APPLY_SCHEMA,
        "operation": "apply-shared-resource",
        "binding": {
            "revision": revision,
            "candidateRawSha256": raw_sha256,
        },
        "validation": {"replayAbiValidated": True},
        "scope": "captured-resource",
        "applied": True,
        "replacement": {
            "replacementId": replacement_id,
            "revision": revision,
            "candidateRawSha256": raw_sha256,
            "originalResourceId": 77,
            "resourceScoped": True,
            "eventScoped": False,
            "allOriginalResourceUsesAffected": True,
            "originalShaderPreserved": True,
        },
        "rollback": {
            "tool": SHARED_ROLLBACK_TOOL,
            "workspacePath": str(workspace),
            "replacementId": replacement_id,
        },
    }


def _rejected_result(revision: int, raw_sha256: str) -> dict:
    return {
        "schema": SHARED_APPLY_SCHEMA,
        "operation": "apply-shared-resource",
        "binding": {
            "revision": revision,
            "candidateRawSha256": raw_sha256,
        },
        "validation": {"replayAbiValidated": False},
        "applied": False,
        "reason": "candidate ABI is not exactly compatible",
    }


class ShaderLiveEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve() / "workspace"
        self.workspace.mkdir()
        self.watch = _FakeWatchManager()
        self.manager: ShaderLiveEditManager | None = None

    def tearDown(self):
        if self.manager is not None:
            self.manager.shutdown()
        self.temporary.cleanup()

    def _manager(self, apply_candidate):
        self.manager = ShaderLiveEditManager(
            self.watch,
            apply_candidate,
            poll_seconds=0.005,
            join_seconds=0.5,
        )
        return self.manager

    def _identity(self, *, workspace_id: str = "a" * 32) -> dict:
        return {
            "workspaceId": workspace_id,
            "captureId": "b" * 64,
            "capturePath": str(self.workspace / "capture.rdc"),
            "shaderKey": "77:fragment:MainPS:" + "c" * 64,
            "shaderRawSha256": "c" * 64,
            "resourceId": 77,
            "stage": "fragment",
            "entryPoint": "MainPS",
        }

    def test_only_revisions_published_after_start_are_applied(self):
        calls = []

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        _wait_until(lambda: self.watch.poll_calls >= 3)
        self.assertEqual(calls, [])

        candidate_hash = "6" * 64
        self.watch.publish(started["watchId"], 6, candidate_hash)
        _wait_until(lambda: len(calls) == 1)
        self.assertEqual(calls, [(str(self.workspace), 6, candidate_hash)])
        manager.stop(started["liveEditId"])

    def test_queued_live_apply_waits_until_source_review_transaction_finishes(self):
        calls = []
        def apply_candidate(workspace, revision, raw_sha256):
            calls.append(revision)
            return _compatible_result(self.workspace, revision, raw_sha256)
        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        with source_observation_transaction(self.workspace):
            self.watch.publish(started["watchId"], 6, "6" * 64)
            _wait_until(lambda: self.watch.poll_calls >= 5)
            time.sleep(0.05)
            self.assertEqual(calls, [])
        _wait_until(lambda: calls == [6])
        status = manager.poll(started["liveEditId"])
        self.assertEqual(status["applyFailures"], 0)
        manager.stop(started["liveEditId"])

    def test_catch_up_compile_that_finishes_during_start_is_not_skipped(self):
        self.watch = _FakeWatchManager(
            initial_revision=6,
            initial_raw_sha256="6" * 64,
            initial_compile_successes=1,
        )
        calls = []

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        _wait_until(lambda: len(calls) == 1)
        self.assertEqual(calls, [(str(self.workspace), 6, "6" * 64)])
        self.assertTrue(
            started["events"][0]["catchUpCandidateAlreadyCompiled"]
        )
        manager.stop(started["liveEditId"])

    def test_pending_new_save_never_applies_previous_candidate(self):
        calls = []

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        pending_source_sha256 = hashlib.sha256(b"pending source N+1").hexdigest()
        self.watch.observe_pending(started["watchId"], pending_source_sha256)
        polls_before = self.watch.poll_calls
        _wait_until(lambda: self.watch.poll_calls >= polls_before + 5)
        self.assertEqual(calls, [])

        self.watch.publish(
            started["watchId"],
            6,
            "6" * 64,
            source_sha256=pending_source_sha256,
        )
        _wait_until(lambda: len(calls) == 1)
        self.assertEqual(calls, [(str(self.workspace), 6, "6" * 64)])
        manager.stop(started["liveEditId"])

    def test_real_hlsl_save_debounces_compiles_validates_and_applies(self):
        source_dir = self.workspace / "source"
        source_dir.mkdir()
        source = source_dir / "shader.hlsl"
        source.write_text(
            "float4 MainPS() : SV_Target { return 0; }\n", encoding="utf-8"
        )
        service = _SourceWorkspaceService(self.workspace)
        watch_manager = ShaderWorkspaceWatchManager(lambda: service)
        calls = []

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        self.manager = ShaderLiveEditManager(
            watch_manager,
            apply_candidate,
            poll_seconds=0.005,
            join_seconds=0.5,
        )
        started = self.manager.start(self.workspace, 100)
        source.write_text(
            "float4 MainPS() : SV_Target { return 1; }\n", encoding="utf-8"
        )
        _wait_until(lambda: len(calls) == 1, timeout=5.0)

        edited_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        candidate_sha256 = hashlib.sha256(
            b"compiled:" + source.read_bytes()
        ).hexdigest()
        self.assertEqual(service.compile_hashes, [edited_sha256])
        self.assertEqual(
            calls,
            [(str(self.workspace), 1, candidate_sha256)],
        )
        # The callback records entry before the asynchronous coordinator has
        # persisted its successful result. Observe completion before counters.
        _wait_until(lambda: self.manager.poll(started["liveEditId"])["applySuccesses"] == 1,
                    timeout=5.0)
        status = self.manager.poll(started["liveEditId"])
        self.assertEqual(status["watch"]["compileSuccesses"], 1)
        self.assertEqual(status["applySuccesses"], 1)
        self.assertFalse(status["policy"]["freshReplayAbiValidationRequired"])
        self.assertTrue(status["policy"]["unprovenCandidateRequiresAbiCheck"])
        self.assertTrue(status["policy"]["compileWhenSourceChanged"])
        self.manager.stop(started["liveEditId"])

    def test_manual_saved_glsl_preview_starts_and_retries_rejected_candidate(self):
        from src.shader_live_edit import ShaderLiveEditSession
        from src.shader_workspace.organization import ORGANIZATION_GATE_SCHEMA, ORGANIZATION_SESSION_SCHEMA
        source_dir = self.workspace / "source"
        source_dir.mkdir()
        original = source_dir / "shader.hlsl"
        original.write_bytes(b"void main() { outColor = vec4(0); }\n")
        service = _SourceWorkspaceService(self.workspace)
        service.source = source_dir / "shader.glsl"
        original.rename(service.source)
        edited = b"void main() { outColor = vec4(1, 0, 1, 1); }\n"
        service.source.write_bytes(edited)
        expected = hashlib.sha256(edited).hexdigest()
        session = self.workspace / "ai/organization-sessions" / ("d" * 32) / "session.json"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"schema": ORGANIZATION_SESSION_SCHEMA,
            "organizationId": "d" * 32, "state": "drafting"}), encoding="utf-8")
        (self.workspace / "ai/organize-code-gate.json").write_text(json.dumps({
            "schema": ORGANIZATION_GATE_SCHEMA, "organizationId": "d" * 32,
            "decision": "editable-draft"}), encoding="utf-8")
        watch = ShaderWorkspaceWatchManager(lambda: service)
        calls = []
        def apply(workspace, revision, raw):
            calls.append(revision)
            return (_rejected_result(revision, raw) if len(calls) == 1 else
                    _compatible_result(self.workspace, revision, raw))
        self.manager = ShaderLiveEditManager(watch, apply, poll_seconds=0.005, join_seconds=0.5,
            registry_path=Path(self.temporary.name) / "live-edit.json",
            workspace_identity=lambda _path: self._identity(),
            bind_session=lambda _path: ShaderLiveEditSession(capture_binding, lambda: "current", apply))
        capture_binding = {"instanceId": "gui", "captureSessionId": "b" * 32,
                           "capturePath": self._identity()["capturePath"]}
        first = self.manager.compile_saved_source(str(self.workspace), "a" * 32, expected,
                                                  capture_binding=capture_binding)
        self.assertTrue(first["compileScheduled"])
        _wait_until(lambda: self.manager.poll(first["liveEditId"])["applyRejections"] == 1, timeout=5.0)
        second = self.manager.compile_saved_source(str(self.workspace), "a" * 32, expected,
                                                   capture_binding=capture_binding)
        self.assertEqual(first["liveEditId"], second["liveEditId"])
        _wait_until(lambda: self.manager.poll(first["liveEditId"])["applySuccesses"] == 1, timeout=5.0)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(service.compile_hashes, [expected, expected])
        self.assertEqual(service.source.read_bytes(), edited)
        # The request cannot become an authorization for a different workspace.
        with self.assertRaisesRegex(ShaderLiveEditError, "authorization changed"):
            self.manager.compile_saved_source(str(self.workspace), "f" * 32, expected,
                                             capture_binding=capture_binding)
        for changed in ({"instanceId": "other-gui"}, {"captureSessionId": "c" * 32}):
            with self.subTest(binding=changed), self.assertRaisesRegex(ShaderLiveEditError, "another GUI"):
                self.manager.compile_saved_source(str(self.workspace), "a" * 32, expected,
                    capture_binding={**capture_binding, **changed})
        self.assertEqual(service.compile_hashes, [expected, expected])

    def test_host_save_requires_exact_running_authorization_and_workspace(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        manager = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        self.manager = manager
        started = manager.start(self.workspace, 100)
        live_edit_id = started["liveEditId"]
        watch_id = started["watchId"]
        _wait_until(lambda: manager.poll(live_edit_id)["state"] == "running")
        authorization = json.loads(registry.read_text(encoding="utf-8"))[
            "entries"
        ][0]
        authorization_id = authorization["authorizationId"]
        workspace_id = authorization["workspaceIdentity"]["workspaceId"]
        expected_sha256 = "d" * 64
        text = "float4 MainPS() : SV_Target { return 11; }\n"

        saved = manager.save_authorized_source(
            live_edit_id,
            authorization_id,
            workspace_id,
            expected_sha256,
            text,
        )

        self.assertEqual(
            [(watch_id, expected_sha256, text)], self.watch.save_calls
        )
        self.assertEqual(live_edit_id, saved["liveEditId"])
        self.assertEqual(authorization_id, saved["authorizationId"])
        self.assertEqual(workspace_id, saved["workspaceId"])
        self.assertEqual(
            hashlib.sha256(text.encode("utf-8")).hexdigest(), saved["rawSha256"]
        )

        with self.assertRaisesRegex(
            ShaderLiveEditError, "authorization does not own"
        ):
            manager.save_authorized_source(
                live_edit_id,
                "f" * 32,
                workspace_id,
                expected_sha256,
                text,
            )
        with self.assertRaisesRegex(
            ShaderLiveEditError, "unavailable or ambiguous"
        ):
            manager.save_authorized_source(
                live_edit_id,
                authorization_id,
                "f" * 32,
                expected_sha256,
                text,
            )
        self.assertEqual(1, len(self.watch.save_calls))

        manager.stop(live_edit_id)
        with self.assertRaisesRegex(ShaderLiveEditError, "not running"):
            manager.save_authorized_source(
                live_edit_id,
                authorization_id,
                workspace_id,
                expected_sha256,
                text,
            )
        self.assertEqual(1, len(self.watch.save_calls))

    def test_transient_winerror5_persisting_live_audit_keeps_session_running(self):
        calls = []
        audit_path = None

        def apply_candidate(workspace, revision, raw_sha256):
            # The mutation callback is never entered until applyStarted itself
            # has crossed the durable audit barrier.
            persisted = json.loads(Path(audit_path).read_text(encoding="utf-8"))
            self.assertEqual(persisted["events"][-1]["type"], "applyStarted")
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        audit_path = started["auditPath"]
        real_replace = os.replace
        denial_count = 0

        def deny_apply_started_once(temporary, destination):
            nonlocal denial_count
            if Path(destination) == Path(audit_path) and denial_count == 0:
                candidate = json.loads(Path(temporary).read_text(encoding="utf-8"))
                if candidate["events"][-1]["type"] == "applyStarted":
                    denial_count += 1
                    error = PermissionError(13, "Access is denied", str(temporary))
                    error.winerror = 5
                    error.filename2 = str(destination)
                    raise error
            return real_replace(temporary, destination)

        with mock.patch(
            "src.shader_workspace.io.os.replace", side_effect=deny_apply_started_once
        ), mock.patch("src.shader_workspace.io.time.sleep", return_value=None):
            self.watch.publish(started["watchId"], 6, "6" * 64)
            _wait_until(
                lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
            )

        status = manager.poll(started["liveEditId"])
        persisted = json.loads(
            Path(status["auditPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(denial_count, 1)
        self.assertEqual(status["state"], "running")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(status["applyInProgress"])
        self.assertIsNone(persisted["applyInProgress"])
        self.assertEqual(persisted["applySuccesses"], 1)
        event_types = [event["type"] for event in persisted["events"]]
        self.assertLess(event_types.index("applyQueued"), event_types.index("applyStarted"))
        self.assertLess(event_types.index("applyStarted"), event_types.index("applySucceeded"))
        self.assertEqual(list((self.workspace / "live-edit").glob(".*.tmp-*")), [])
        manager.stop(started["liveEditId"])

    def test_persistent_winerror5_before_apply_fails_closed_without_mutation(self):
        calls = []

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        real_replace = os.replace

        def deny_live_audit(temporary, destination):
            if Path(destination) == Path(started["auditPath"]):
                error = PermissionError(13, "Access is denied", str(temporary))
                error.winerror = 5
                error.filename2 = str(destination)
                raise error
            return real_replace(temporary, destination)

        with mock.patch(
            "src.shader_workspace.io.os.replace", side_effect=deny_live_audit
        ), mock.patch("src.shader_workspace.io.time.sleep", return_value=None):
            self.watch.publish(started["watchId"], 6, "6" * 64)
            _wait_until(
                lambda: manager.poll(started["liveEditId"])["state"] == "failed"
            )

        status = manager.poll(started["liveEditId"])
        self.assertEqual(calls, [])
        self.assertEqual(status["applySuccesses"], 0)
        self.assertIsNone(status["activeReplacement"])
        self.assertIsNone(status["applyInProgress"])
        self.assertEqual(list((self.workspace / "live-edit").glob(".*.tmp-*")), [])

    def test_transient_winerror5_persisting_authorization_registry_is_retried(self):
        # CI's TEMP can use an 8.3 user-directory alias; the registry resolves
        # it before writing, so fault injection must match that same identity.
        registry = (Path(self.temporary.name) / "config" / "live-edit.json").resolve()
        identity = self._identity()
        real_replace = os.replace
        attempts = 0

        def deny_registry_once(temporary, destination):
            nonlocal attempts
            if Path(destination) == registry:
                attempts += 1
                if attempts == 1:
                    error = PermissionError(13, "Access is denied", str(temporary))
                    error.winerror = 5
                    error.filename2 = str(destination)
                    raise error
            return real_replace(temporary, destination)

        self.manager = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(identity),
        )
        with mock.patch(
            "src.shader_workspace.io.os.replace", side_effect=deny_registry_once
        ), mock.patch("src.shader_workspace.io.time.sleep", return_value=None):
            started = self.manager.start(self.workspace, 100)

        self.assertEqual(attempts, 3)
        persisted = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(persisted["entries"][0]["lastLiveEditId"], started["liveEditId"])
        self.assertEqual(list(registry.parent.glob(".*.tmp-*")), [])
        self.manager.stop(started["liveEditId"])

    def test_compatible_candidate_applies_with_exact_rollback_and_audit_metadata(self):
        manager = self._manager(
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            )
        )
        started = manager.start(self.workspace, 100)
        candidate_hash = "6" * 64
        self.watch.publish(started["watchId"], 6, candidate_hash)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
        )

        status = manager.poll(started["liveEditId"])
        replacement_id = "%032x" % 0x1006
        self.assertEqual(status["latestHandledRevision"], 6)
        self.assertEqual(status["applyAttempts"], 1)
        self.assertEqual(status["rollback"]["tool"], SHARED_ROLLBACK_TOOL)
        self.assertEqual(status["rollback"]["replacementId"], replacement_id)
        self.assertTrue(status["activeReplacement"]["originalShaderPreserved"])
        self.assertEqual(status["activeReplacement"]["originalResourceId"], 77)

        audit = json.loads(Path(status["auditPath"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["rollback"], status["rollback"])
        succeeded = [event for event in audit["events"] if event["type"] == "applySucceeded"]
        self.assertEqual(succeeded[-1]["revision"], 6)
        self.assertEqual(succeeded[-1]["candidateRawSha256"], candidate_hash)
        self.assertTrue(succeeded[-1]["originalShaderPreserved"])

        # Public snapshots are copies; a caller cannot rewrite future audit/rollback state.
        status["rollback"]["replacementId"] = "f" * 32
        status["events"][0]["type"] = "tampered"
        fresh = manager.poll(started["liveEditId"])
        self.assertEqual(fresh["rollback"]["replacementId"], replacement_id)
        self.assertNotEqual(fresh["events"][0]["type"], "tampered")
        manager.stop(started["liveEditId"])

    def test_byte_identical_candidate_reuses_active_replacement_without_callback(self):
        calls = []

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        candidate_hash = "6" * 64
        source = self.workspace / "source/shader.hlsl"
        source.parent.mkdir()
        source.write_bytes(b"float4 MainPS() : SV_Target { return 1; }\n")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = {"source": {"path": "source/shader.hlsl"}, "latestCompiled": {
            "revision": 6, "rawSha256": candidate_hash, "sourceSha256": source_hash}}
        (self.workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.watch.publish(started["watchId"], 6, candidate_hash, source_hash)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
        )

        manifest["latestCompiled"]["revision"] = 7
        (self.workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.watch.publish(started["watchId"], 7, candidate_hash, source_hash)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applyReuses"] == 1
        )
        status = manager.poll(started["liveEditId"])
        self.assertEqual(calls, [(str(self.workspace), 6, candidate_hash)])
        self.assertEqual(status["latestHandledRevision"], 7)
        self.assertEqual(status["applyAttempts"], 1)
        self.assertEqual(status["applySuccesses"], 1)
        self.assertEqual(status["activeReplacement"]["revision"], 6)
        self.assertEqual(
            status["lastApply"]["operation"], "reuse-active-shared-resource"
        )
        self.assertTrue(status["lastApply"]["reused"])
        self.assertFalse(status["lastApply"]["mutationPerformed"])
        reused = [
            event for event in status["events"] if event["type"] == "applyReused"
        ]
        self.assertEqual(reused[-1]["revision"], 7)
        self.assertFalse(reused[-1]["replayAbiValidationPerformed"])
        manager.stop(started["liveEditId"])

    def test_slow_apply_does_not_block_monitor_and_pending_queue_is_latest_wins(self):
        calls = []
        first_apply_entered = threading.Event()
        release_first_apply = threading.Event()

        def apply_candidate(workspace, revision, raw_sha256):
            calls.append((workspace, revision, raw_sha256))
            if revision == 6:
                first_apply_entered.set()
                if not release_first_apply.wait(2.0):
                    raise RuntimeError("test did not release first apply")
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        self.watch.publish(started["watchId"], 6, "6" * 64)
        self.assertTrue(first_apply_entered.wait(1.0))
        polls_while_apply_started = self.watch.poll_calls

        self.watch.publish(started["watchId"], 7, "7" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["latestScheduledRevision"]
            == 7
        )
        self.watch.publish(started["watchId"], 8, "8" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["latestScheduledRevision"]
            == 8
        )
        status = manager.poll(started["liveEditId"])
        self.assertGreater(self.watch.poll_calls, polls_while_apply_started)
        self.assertEqual(status["pendingApply"]["revision"], 8)
        self.assertEqual(status["applyInProgress"]["revision"], 6)
        self.assertEqual(status["supersededApplies"], 1)

        release_first_apply.set()
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 2
        )
        self.assertEqual([call[1] for call in calls], [6, 8])
        manager.stop(started["liveEditId"])

    def test_abi_rejection_is_handled_once_without_poll_retry(self):
        calls = []

        def apply_candidate(_workspace, revision, raw_sha256):
            calls.append((revision, raw_sha256))
            return _rejected_result(revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        self.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applyRejections"] == 1
        )
        polls_after_rejection = self.watch.poll_calls
        _wait_until(lambda: self.watch.poll_calls >= polls_after_rejection + 5)
        self.assertEqual(calls, [(6, "6" * 64)])
        status = manager.poll(started["liveEditId"])
        self.assertEqual(status["latestHandledRevision"], 6)
        self.assertEqual(status["applyAttempts"], 1)
        self.assertEqual(status["lastApply"]["applied"], False)
        manager.stop(started["liveEditId"])

    def test_rejected_save_preserves_last_good_active_replacement(self):
        def apply_candidate(_workspace, revision, raw_sha256):
            if revision == 6:
                return _compatible_result(self.workspace, revision, raw_sha256)
            return _rejected_result(revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        self.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
        )
        previous = manager.poll(started["liveEditId"])
        previous_replacement = copy.deepcopy(previous["activeReplacement"])
        previous_rollback = copy.deepcopy(previous["rollback"])

        self.watch.publish(started["watchId"], 7, "7" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applyRejections"] == 1
        )
        rejected = manager.poll(started["liveEditId"])
        self.assertEqual(rejected["activeReplacement"], previous_replacement)
        self.assertEqual(rejected["lastGoodReplacement"], previous_replacement)
        self.assertEqual(rejected["rollback"], previous_rollback)
        self.assertTrue(
            rejected["policy"]["failedSavePreservesLastGoodReplacement"]
        )
        manager.stop(started["liveEditId"])

    def test_successful_save_supersedes_last_good_rollback_identity(self):
        manager = self._manager(
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            )
        )
        started = manager.start(self.workspace, 100)
        self.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
        )
        first = manager.poll(started["liveEditId"])
        first_id = first["rollback"]["replacementId"]

        self.watch.publish(started["watchId"], 7, "7" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 2
        )
        second = manager.poll(started["liveEditId"])
        second_id = second["rollback"]["replacementId"]
        self.assertNotEqual(second_id, first_id)
        self.assertEqual(second["latestHandledRevision"], 7)
        self.assertEqual(second["activeReplacement"]["revision"], 7)
        self.assertEqual(second["lastGoodReplacement"], second["activeReplacement"])
        self.assertEqual(second["rollback"]["replacementId"], second_id)
        manager.stop(started["liveEditId"])

    def test_apply_exception_does_not_block_the_next_revision(self):
        calls = []

        def apply_candidate(_workspace, revision, raw_sha256):
            calls.append(revision)
            if revision == 6:
                raise RuntimeError("transient replay failure")
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        self.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applyFailures"] == 1
        )
        self.watch.publish(started["watchId"], 7, "7" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
        )
        status = manager.poll(started["liveEditId"])
        self.assertEqual(calls, [6, 7])
        self.assertEqual(status["latestHandledRevision"], 7)
        self.assertEqual(status["applyAttempts"], 2)
        self.assertEqual(status["applyFailures"], 1)
        self.assertEqual(status["applySuccesses"], 1)
        manager.stop(started["liveEditId"])

    def test_apply_failure_and_compile_failure_keep_last_good_and_report_phase(self):
        def apply_candidate(_workspace, revision, raw_sha256):
            if revision == 7:
                raise RuntimeError("transient live replay failure")
            return _compatible_result(self.workspace, revision, raw_sha256)

        manager = self._manager(apply_candidate)
        started = manager.start(self.workspace, 100)
        self.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 1
        )
        previous = manager.poll(started["liveEditId"])
        previous_replacement = copy.deepcopy(previous["activeReplacement"])

        self.watch.publish(started["watchId"], 7, "7" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applyFailures"] == 1
        )
        apply_failed = manager.poll(started["liveEditId"])
        self.assertEqual(apply_failed["activeReplacement"], previous_replacement)
        self.assertEqual(apply_failed["lastGoodReplacement"], previous_replacement)

        self.watch.fail_compile(started["watchId"], "syntax error at line 9")
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["watch"][
                "compileFailures"
            ]
            == 1
        )
        compile_failed = manager.poll(started["liveEditId"])
        self.assertEqual(
            compile_failed["lastCompileError"]["message"],
            "syntax error at line 9",
        )
        self.assertEqual(
            compile_failed["lastCompileError"]["diagnostic"]["category"],
            "tool-rejected-input",
        )
        self.assertNotIn("allowedActions", compile_failed["lastCompileError"]["diagnostic"])
        self.assertEqual(
            compile_failed["lastCompileError"]["diagnostics"][0]["line"], 9
        )
        self.assertEqual(compile_failed["activeReplacement"], previous_replacement)
        self.assertIn(
            "compileFailed", [event["type"] for event in compile_failed["events"]]
        )

        self.watch.publish(started["watchId"], 8, "8" * 64)
        _wait_until(
            lambda: manager.poll(started["liveEditId"])["applySuccesses"] == 2
        )
        recovered = manager.poll(started["liveEditId"])
        self.assertIsNone(recovered["lastCompileError"])
        self.assertIn(
            "compileRecovered", [event["type"] for event in recovered["events"]]
        )
        manager.stop(started["liveEditId"])

    def test_stop_is_terminal_and_prevents_later_apply(self):
        calls = []
        manager = self._manager(
            lambda _workspace, revision, raw: (
                calls.append(revision)
                or _compatible_result(self.workspace, revision, raw)
            )
        )
        started = manager.start(self.workspace, 100)
        stopped = manager.stop(started["liveEditId"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertIn(started["watchId"], self.watch.stop_calls)
        self.assertIn("liveEditStopRequested", [event["type"] for event in stopped["events"]])
        self.assertIn("liveEditStopped", [event["type"] for event in stopped["events"]])

        self.watch.publish(started["watchId"], 6, "6" * 64)
        time.sleep(0.03)
        self.assertEqual(calls, [])

    def test_one_live_edit_owner_per_workspace(self):
        manager = self._manager(
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            )
        )
        first = manager.start(self.workspace, 100)
        # A second start joins the one session instead of failing: still one
        # owner, one watcher, and the caller learns which session it joined.
        joined = manager.start(self.workspace, 100)
        self.assertEqual(joined["liveEditId"], first["liveEditId"])
        self.assertTrue(joined["joinedExistingSession"])
        self.assertEqual(len(self.watch.start_calls), 1)

        manager.stop(first["liveEditId"])
        second = manager.start(self.workspace, 100)
        self.assertNotEqual(first["liveEditId"], second["liveEditId"])
        self.assertEqual(len(self.watch.start_calls), 2)
        manager.stop(second["liveEditId"])

    def test_persistent_authorisation_resumes_after_manager_restart_and_stop_revokes(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        identity = self._identity()
        first_watch = self.watch
        first = ShaderLiveEditManager(
            first_watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(identity),
        )
        started = first.start(self.workspace, 100)
        self.assertTrue(registry.is_file())
        persisted = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["entries"]), 1)
        self.assertEqual(
            persisted["entries"][0]["lastLiveEditId"], started["liveEditId"]
        )
        first.shutdown()

        second_watch = _FakeWatchManager()
        second = ShaderLiveEditManager(
            second_watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(identity),
        )
        self.manager = second
        resumed = second.resume_persistent()
        self.assertTrue(resumed["results"][0]["resumed"])
        self.assertEqual(len(second_watch.start_calls), 1)
        live_edit_id = resumed["results"][0]["liveEditId"]
        stopped = second.stop(live_edit_id)
        self.assertEqual(stopped["state"], "stopped")
        persisted = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(persisted["entries"], [])

    def test_persistent_revoke_exception_still_stops_workers_and_remains_retryable(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        manager = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        self.manager = manager
        started = manager.start(self.workspace, 100)
        live_edit_id = started["liveEditId"]
        authorization_id = manager._authorization_by_live_edit[live_edit_id]
        self.assertIsNotNone(manager._registry)

        with mock.patch.object(
            manager._registry,
            "revoke",
            side_effect=PermissionError(5, "Access is denied"),
        ):
            with self.assertRaisesRegex(
                ShaderLiveEditError,
                "monitoring was stopped.*revocation was not confirmed",
            ):
                manager.stop(live_edit_id)

        status = manager.poll(live_edit_id)
        job = manager._jobs[live_edit_id]
        self.assertEqual(status["state"], "stopped")
        self.assertFalse(job._thread.is_alive())
        self.assertFalse(job._apply_thread.is_alive())
        self.assertIn(started["watchId"], self.watch.stop_calls)
        self.assertEqual(
            manager._authorization_by_live_edit[live_edit_id], authorization_id
        )
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["entries"][0][
                "authorizationId"
            ],
            authorization_id,
        )
        failure = [
            event
            for event in status["events"]
            if event["type"] == "persistentAuthorizationRevokeFailed"
        ][-1]
        self.assertFalse(failure["durableAuthorizationRevoked"])
        self.assertTrue(failure["monitoringTerminal"])

        # The exact ID remains available for a later explicit retry.  A
        # successful retry clears durable authorization without restarting the
        # already-terminal watcher.
        retried = manager.stop(live_edit_id)
        self.assertEqual(retried["state"], "stopped")
        self.assertNotIn(live_edit_id, manager._authorization_by_live_edit)
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["entries"], []
        )
        self.assertEqual(self.watch.stop_calls.count(started["watchId"]), 1)

    def test_unconfirmed_false_revoke_result_cannot_claim_authorization_removed(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        manager = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        self.manager = manager
        started = manager.start(self.workspace, 100)
        live_edit_id = started["liveEditId"]
        self.assertIsNotNone(manager._registry)

        with mock.patch.object(manager._registry, "revoke", return_value=False):
            with self.assertRaisesRegex(
                ShaderLiveEditError,
                "monitoring was stopped.*revocation was not confirmed",
            ):
                manager.stop(live_edit_id)

        status = manager.poll(live_edit_id)
        self.assertEqual(status["state"], "stopped")
        self.assertFalse(manager._jobs[live_edit_id]._thread.is_alive())
        self.assertFalse(manager._jobs[live_edit_id]._apply_thread.is_alive())
        self.assertEqual(
            len(json.loads(registry.read_text(encoding="utf-8"))["entries"]), 1
        )

        # Restore the real revoke method and finish the durable part of stop.
        manager.stop(live_edit_id)
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["entries"], []
        )

    def test_failed_resume_orphan_can_be_revoked_by_exact_last_live_edit_id(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        identity = self._identity()
        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(identity),
        )
        started = first.start(self.workspace, 100)
        authorization_id = json.loads(registry.read_text(encoding="utf-8"))[
            "entries"
        ][0]["authorizationId"]
        first.shutdown()

        second = ShaderLiveEditManager(
            _FakeWatchManager(),
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: (_ for _ in ()).throw(
                RuntimeError("workspace route catalog bytes changed after creation")
            ),
        )
        self.manager = second
        resumed = second.resume_persistent()
        self.assertFalse(resumed["results"][0]["resumed"])
        self.assertEqual(second._jobs, {})

        stopped = second.stop(started["liveEditId"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertFalse(stopped["monitoringWasActive"])
        self.assertTrue(stopped["orphanedPersistentAuthorization"])
        self.assertTrue(stopped["persistentAuthorizationRevoked"])
        self.assertEqual(stopped["revokedAuthorizationId"], authorization_id)
        self.assertEqual(stopped["workspacePath"], str(self.workspace))
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["entries"], []
        )

    def test_orphan_revoke_write_failure_is_explicit_and_retains_authorization(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        started = first.start(self.workspace, 100)
        first.shutdown()
        before = registry.read_bytes()

        second = ShaderLiveEditManager(
            _FakeWatchManager(),
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        self.manager = second
        with mock.patch(
            "src.shader_live_edit.atomic_write_json",
            side_effect=PermissionError(5, "Access is denied"),
        ):
            with self.assertRaisesRegex(
                ShaderLiveEditError,
                "no authorization was reported revoked",
            ):
                second.stop(started["liveEditId"])

        self.assertEqual(registry.read_bytes(), before)
        retained = json.loads(registry.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["lastLiveEditId"], started["liveEditId"])
        self.assertTrue(second.stop(started["liveEditId"])["persistentAuthorizationRevoked"])

    def test_orphan_stop_never_falls_back_from_unknown_or_ambiguous_session_id(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        started = first.start(self.workspace, 100)
        first.shutdown()

        second = ShaderLiveEditManager(
            _FakeWatchManager(),
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(self._identity()),
        )
        self.manager = second
        before = registry.read_bytes()
        with self.assertRaisesRegex(ShaderLiveEditError, "no exact persistent"):
            second.stop("f" * 32)
        self.assertEqual(registry.read_bytes(), before)

        other_workspace = Path(self.temporary.name).resolve() / "other-workspace"
        other_workspace.mkdir()
        self.assertIsNotNone(second._registry)
        second._registry.authorize(
            other_workspace,
            self._identity(workspace_id="d" * 32),
            100,
            last_live_edit_id=started["liveEditId"],
        )
        ambiguous = registry.read_bytes()
        with self.assertRaisesRegex(ShaderLiveEditError, "ambiguous"):
            second.stop(started["liveEditId"])
        self.assertEqual(registry.read_bytes(), ambiguous)

    def test_persistent_authorisation_never_moves_to_changed_workspace_identity(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        original_identity = self._identity()
        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(original_identity),
        )
        first.start(self.workspace, 100)
        first.shutdown()

        replacement_watch = _FakeWatchManager()
        replacement_identity = self._identity(workspace_id="d" * 32)
        second = ShaderLiveEditManager(
            replacement_watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(replacement_identity),
        )
        self.manager = second
        resumed = second.resume_persistent()
        self.assertFalse(resumed["results"][0]["resumed"])
        self.assertIn("identity", resumed["results"][0]["message"])
        self.assertEqual(replacement_watch.start_calls, [])

    def test_route_catalog_staleness_blocks_resume_without_revoking_consent(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        identity = self._identity()
        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(identity),
        )
        first.start(self.workspace, 100)
        first.shutdown()
        before = registry.read_bytes()

        replacement_watch = _FakeWatchManager()
        second = ShaderLiveEditManager(
            replacement_watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: (_ for _ in ()).throw(
                RuntimeError("workspace route catalog bytes changed after creation")
            ),
        )
        self.manager = second
        resumed = second.resume_persistent()
        self.assertFalse(resumed["results"][0]["resumed"])
        self.assertIn("route catalog bytes changed", resumed["results"][0]["message"])
        self.assertEqual(replacement_watch.start_calls, [])
        self.assertEqual(registry.read_bytes(), before)

    def test_explicit_migration_moves_one_authorization_and_resumes_target(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        target = Path(self.temporary.name).resolve() / "migrated-workspace"
        target.mkdir()
        source_identity = self._identity(workspace_id="a" * 32)
        target_identity = self._identity(workspace_id="d" * 32)
        source_hash = "e" * 64

        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(source_identity),
        )
        started = first.start(self.workspace, 100)
        original_authorization = json.loads(
            registry.read_text(encoding="utf-8")
        )["entries"][0]
        first.shutdown()

        def identity(path):
            if Path(path).resolve() == target:
                return copy.deepcopy(target_identity)
            return copy.deepcopy(source_identity)

        def migration_evidence(source_path, target_path, expected_hash):
            value = {
                "schema": (
                    "renderdoc-ultra-shader-live-edit-authorization-"
                    "migration-evidence/v1"
                ),
                "schemaVersion": 1,
                "sourceWorkspacePath": str(Path(source_path).resolve()),
                "workspacePath": str(Path(target_path).resolve()),
                "sourceSha256": expected_hash,
                "sourceWorkspaceIdentity": copy.deepcopy(source_identity),
                "workspaceIdentity": copy.deepcopy(target_identity),
                "migrationRecordSha256": "f" * 64,
                "sourceCandidatesImported": False,
                "semanticEquivalenceVerified": False,
            }
            value["evidenceSha256"] = hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return value

        second_watch = _FakeWatchManager()
        second = ShaderLiveEditManager(
            second_watch,
            lambda _workspace, revision, raw: _compatible_result(
                target, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=identity,
            workspace_migration_evidence=migration_evidence,
        )
        self.manager = second
        migrated = second.migrate_authorization(
            self.workspace, target, source_hash
        )
        self.assertTrue(migrated["authorizationMigrated"])
        self.assertFalse(migrated["watchStarted"])
        self.assertEqual(
            migrated["authorizationId"],
            original_authorization["authorizationId"],
        )
        persisted = json.loads(registry.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(len(persisted), 1)
        self.assertEqual(Path(persisted[0]["workspacePath"]), target)
        self.assertEqual(persisted[0]["workspaceIdentity"], target_identity)
        self.assertIsNone(persisted[0]["lastLiveEditId"])

        resumed = second.resume_persistent()
        self.assertTrue(resumed["results"][0]["resumed"])
        self.assertEqual(
            Path(second_watch.start_calls[0][0]).resolve(), target
        )
        second.stop(resumed["results"][0]["liveEditId"])

    def test_authorization_migration_rejects_source_hash_or_physical_change(self):
        registry = Path(self.temporary.name) / "config" / "live-edit.json"
        target = Path(self.temporary.name).resolve() / "migrated-workspace-rejected"
        target.mkdir()
        source_identity = self._identity(workspace_id="a" * 32)
        changed_identity = self._identity(workspace_id="d" * 32)
        changed_identity["resourceId"] = 88
        first = ShaderLiveEditManager(
            self.watch,
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda _path: copy.deepcopy(source_identity),
        )
        first.start(self.workspace, 100)
        first.shutdown()
        before = registry.read_bytes()

        def evidence(_source, _target, expected_hash):
            value = {
                "schema": (
                    "renderdoc-ultra-shader-live-edit-authorization-"
                    "migration-evidence/v1"
                ),
                "schemaVersion": 1,
                "sourceWorkspacePath": str(self.workspace),
                "workspacePath": str(target),
                "sourceSha256": expected_hash,
                "sourceWorkspaceIdentity": copy.deepcopy(source_identity),
                "workspaceIdentity": copy.deepcopy(changed_identity),
                "migrationRecordSha256": "f" * 64,
                "sourceCandidatesImported": False,
                "semanticEquivalenceVerified": False,
            }
            value["evidenceSha256"] = hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return value

        second = ShaderLiveEditManager(
            _FakeWatchManager(),
            lambda _workspace, revision, raw: _compatible_result(
                target, revision, raw
            ),
            poll_seconds=0.005,
            join_seconds=0.5,
            registry_path=registry,
            workspace_identity=lambda path: (
                copy.deepcopy(changed_identity)
                if Path(path).resolve() == target
                else copy.deepcopy(source_identity)
            ),
            workspace_migration_evidence=evidence,
        )
        self.manager = second
        with self.assertRaisesRegex(ShaderLiveEditError, "physical identity"):
            second.migrate_authorization(self.workspace, target, "e" * 64)
        self.assertEqual(registry.read_bytes(), before)

    def test_concurrent_start_reserves_one_workspace_owner_before_watcher_creation(self):
        self.watch.block_first_start = True
        manager = self._manager(
            lambda _workspace, revision, raw: _compatible_result(
                self.workspace, revision, raw
            )
        )
        result = []
        errors = []

        def start_first():
            try:
                result.append(manager.start(self.workspace, 100))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=start_first)
        thread.start()
        self.assertTrue(self.watch.first_start_entered.wait(1.0))
        with self.assertRaisesRegex(ShaderLiveEditError, "start in progress"):
            manager.start(self.workspace, 100)
        self.assertEqual(len(self.watch.start_calls), 1)

        self.watch.release_first_start.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        manager.stop(result[0]["liveEditId"])


if __name__ == "__main__":
    unittest.main()
