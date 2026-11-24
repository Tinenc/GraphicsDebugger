from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.shader_workspace import (
    ShaderWorkspaceWatchManager,
    WorkspaceWatchConflictError,
    WorkspaceWatchError,
)
from src.shader_workspace.leases import WorkspaceSourceTransactionLease
from src.shader_workspace.source_observation import source_observation_transaction
from src.shader_workspace import watcher as watcher_module


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _FakeWorkspaceService:
    def __init__(
        self,
        root: Path,
        block_first: bool = False,
        fail_first: bool = False,
        fail_attempt: int = 0,
    ):
        self.root = root.resolve()
        self.block_first = block_first
        self.fail_attempt = fail_attempt or (1 if fail_first else 0)
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.compile_hashes: list[str] = []
        self.latest: dict | None = None
        self.decompiled_hash = _sha((self.root / "source" / "shader.hlsl").read_bytes())

    def status(self, workspace_path) -> dict:
        root = Path(workspace_path).resolve()
        if root != self.root:
            raise RuntimeError("unexpected workspace")
        source = root / "source" / "shader.hlsl"
        current = _sha(source.read_bytes())
        return {
            "workspacePath": str(root),
            "source": {
                "present": True,
                "path": str(source),
                "currentSha256": current,
                "decompiledSha256": self.decompiled_hash,
                "lane": "executable-editable",
            },
            "representation": {
                "present": True,
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "latestCompiled": dict(self.latest) if self.latest else None,
        }

    def compile(self, workspace_path) -> dict:
        root = Path(workspace_path).resolve()
        source = (root / "source" / "shader.hlsl").read_bytes()
        source_hash = _sha(source)
        attempt = len(self.compile_hashes) + 1
        self.compile_hashes.append(source_hash)
        if attempt == 1:
            self.first_started.set()
            if self.block_first and not self.release_first.wait(3.0):
                raise RuntimeError("test compile release timed out")
        if attempt == self.fail_attempt:
            raise RuntimeError("intentional compiler failure")
        candidate_hash = _sha(b"compiled:" + source)
        self.latest = {
            "sourceSha256": source_hash,
            "revision": attempt,
            "rawSha256": candidate_hash,
        }
        return {
            "revision": attempt,
            "sourceSha256": source_hash,
            "rawSha256": candidate_hash,
        }


class ShaderWorkspaceWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temp_root))
        self.root = Path(self.temporary.name).resolve()
        (self.root / "source").mkdir()
        self.source = self.root / "source" / "shader.hlsl"
        self.initial = b"float4 main() : SV_Target { return 0; }\n"
        self.source.write_bytes(self.initial)
        self.managers: list[ShaderWorkspaceWatchManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()
        self.temporary.cleanup()

    def _manager(self, service: _FakeWorkspaceService) -> ShaderWorkspaceWatchManager:
        manager = ShaderWorkspaceWatchManager(lambda: service)
        self.managers.append(manager)
        return manager

    def _wait_for(self, manager, watch_id, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = manager.poll(watch_id)
            if predicate(result):
                return result
            time.sleep(0.02)
        self.fail("watcher condition was not reached: %r" % manager.poll(watch_id))

    def _manual_job(self, service):
        # Real files and original CAS/compile paths, without a background clock.
        (self.root / "watch").mkdir(exist_ok=True)
        job = watcher_module._WatchJob(
            "a" * 32, self.root, self.source, 100, service, _sha(self.initial),
            None, None, None, None, None, self.root / "watch" / "audit.json",
            mock.Mock(path=self.root / "watch" / "lease", owned=True), lambda job: None)
        job._state = "running"
        return job

    def _open_organization_draft(self):
        from src.shader_workspace.organization import ORGANIZATION_GATE_SCHEMA, ORGANIZATION_SESSION_SCHEMA
        organization = "d" * 32
        session = self.root / "ai" / "organization-sessions" / organization / "session.json"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"schema": ORGANIZATION_SESSION_SCHEMA,
            "organizationId": organization, "state": "drafting"}), encoding="utf-8")
        (self.root / "ai" / "organize-code-gate.json").write_text(json.dumps({
            "schema": ORGANIZATION_GATE_SCHEMA, "organizationId": organization,
            "decision": "editable-draft"}), encoding="utf-8")
        return session

    def test_organization_file_saves_defer_compile_and_candidate_adoption(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        session = self._open_organization_draft()
        changed = self.initial.replace(b"return 0", b"return 7")
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock, \
                mock.patch.object(watcher_module, "_already_compiled_candidate") as candidate:
            job._tick()
            self.source.write_bytes(changed)
            for value in (100.05, 100.2, 101.3, 102.4):
                clock.return_value = value
                job._tick()
            self.assertEqual(job._observed_hash, _sha(changed))
            self.assertEqual(service.compile_hashes, [])
            candidate.assert_not_called()
            self.assertFalse(job._pending)
            # Closing the draft exposes an explicit final candidate; the watcher
            # adopts its revision without recompiling or replaying intermediate text.
            body = json.loads(session.read_text(encoding="utf-8"))
            body["state"] = "completed"
            session.write_text(json.dumps(body), encoding="utf-8")
            candidate.return_value = {"revision": 5, "rawSha256": "c" * 64,
                                      "sourceSha256": _sha(changed)}
            clock.return_value = 102.6
            job._tick()
            self.assertEqual(job._latest_candidate_revision, 5)
            self.assertEqual(service.compile_hashes, [])

    def test_invalid_draft_pointer_defers_work_and_recovers_without_stopping(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        self._open_organization_draft()
        gate = self.root / "ai" / "organize-code-gate.json"
        before = gate.read_bytes()
        gate.write_bytes(b"{broken")
        changed = self.initial.replace(b"return 0", b"return 5")
        self.source.write_bytes(changed)
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock:
            job._tick()
            clock.return_value = 101.2
            job._tick()
            self.assertEqual(job._state, "running")
            self.assertEqual(service.compile_hashes, [])
            gate.write_bytes(before)
            clock.return_value = 101.3
            job._tick()
            self.assertFalse(job._draft_observation_error)
            self.assertEqual(service.compile_hashes, [])
            events = [item["type"] for item in job._events]
            self.assertIn("organizationDraftObservationFailed", events)
            self.assertIn("organizationDraftObservationRecovered", events)

    def test_explicit_editor_save_remains_independent_of_organization_draft(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        self._open_organization_draft()
        changed = self.initial.replace(b"return 0", b"return 8")
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock:
            job._tick()
            saved = job.save_source(_sha(self.initial), changed.decode("utf-8"))
            self.assertTrue(saved["compileScheduled"])
            clock.return_value = 100.11
            job._tick()
            self.assertEqual(service.compile_hashes, [_sha(changed)])
            # A later ordinary filesystem draft is not covered by that save.
            self.source.write_bytes(self.initial)
            clock.return_value = 100.4
            job._tick()
            clock.return_value = 101.5
            job._tick()
            self.assertEqual(service.compile_hashes, [_sha(changed)])

    def test_idle_poll_reuses_full_reads_and_rechecks_at_bounded_interval(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock, \
                mock.patch.object(watcher_module, "_source_hash", wraps=watcher_module._source_hash) as source_read, \
                mock.patch.object(watcher_module, "_already_compiled_candidate", wraps=watcher_module._already_compiled_candidate) as candidate_read, \
                mock.patch.object(watcher_module, "source_observation_transaction", wraps=source_observation_transaction) as observation:
            job._tick()
            self.assertEqual((source_read.call_count, candidate_read.call_count), (1, 1))
            observation.reset_mock()
            for index in range(1, 20):
                clock.return_value = 100.0 + index * 0.05
                job._tick()
            self.assertEqual((source_read.call_count, candidate_read.call_count), (1, 1))
            observation.assert_not_called()
            clock.return_value = 100.01 + watcher_module.FULL_OBSERVATION_INTERVAL_SECONDS
            job._tick()
            self.assertEqual((source_read.call_count, candidate_read.call_count), (2, 2))
        self.assertEqual(service.compile_hashes, [])

    def test_sibling_audit_writes_do_not_rescan_source_or_block_new_save(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock, \
                mock.patch.object(watcher_module, "_source_hash", wraps=watcher_module._source_hash) as read:
            job._tick()
            sequence = job.snapshot()["observationSequence"]
            with mock.patch.object(watcher_module, "source_observation_available") as available:
                for index in range(20):
                    self.assertIsNone(job.snapshot_if_changed(sequence))
                available.assert_not_called()
            for index in range(1, 20):
                (self.root / ("audit-%d.json" % index)).write_text("{}", encoding="utf-8")
                clock.return_value = 100.0 + index * 0.1
                job._tick()
            self.assertEqual(read.call_count, 1)
            # A source atomic rename changes its identity even though its
            # directory identity is unchanged. It must still compile promptly.
            replacement = self.source.with_suffix(".pending")
            changed = self.initial.replace(b"return 0", b"return 2")
            replacement.write_bytes(changed)
            os.replace(replacement, self.source)
            clock.return_value = 102.0
            job._tick()
            self.assertEqual(job.snapshot_if_changed(sequence)["observedSourceSha256"], _sha(changed))
            clock.return_value = 102.11
            job._tick()
        self.assertEqual(service.compile_hashes, [_sha(changed)])

    def test_explicit_saved_retry_compiles_unchanged_draft_instead_of_adopting_candidate(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        self._open_organization_draft()
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock, \
                mock.patch.object(watcher_module, "_already_compiled_candidate") as adopt:
            job._tick()
            for attempt in range(2):
                clock.return_value = 101.0 + attempt
                queued = job.request_saved_source_compile(_sha(self.initial))
                self.assertTrue(queued["compileScheduled"])
                clock.return_value += 0.11
                job._tick()
            self.assertEqual(service.compile_hashes, [_sha(self.initial)] * 2)
            self.assertEqual(job._latest_candidate_revision, 2)
            self.assertEqual(self.source.read_bytes(), self.initial)
            adopt.assert_not_called()

    def test_explicit_saved_retry_recovers_after_compile_failure(self):
        service = _FakeWorkspaceService(self.root, fail_first=True)
        job = self._manual_job(service)
        self._open_organization_draft()
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock:
            job.request_saved_source_compile(_sha(self.initial))
            clock.return_value = 100.11
            job._tick()
            self.assertEqual(job._compile_failures, 1)
            clock.return_value = 100.2
            job.request_saved_source_compile(_sha(self.initial))
            clock.return_value = 100.31
            job._tick()
            self.assertEqual(job._compile_successes, 1)
            self.assertEqual(service.compile_hashes, [_sha(self.initial)] * 2)

    def test_explicit_saved_request_refuses_source_race(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        changed = self.initial.replace(b"return 0", b"return 6")
        self.source.write_bytes(changed)
        with self.assertRaises(WorkspaceWatchConflictError):
            job.request_saved_source_compile(_sha(self.initial))
        self.assertEqual(service.compile_hashes, [])
        self.assertEqual(job._explicit_compile_hash, "")
        self.assertEqual(self.source.read_bytes(), changed)

    def test_candidate_adoption_checks_include_content_not_just_root_hash(self):
        from src.shader_workspace.compile_source_unit import capture_compilation_unit
        source = b'#include "color.hlsli"\nfloat4 main() : SV_Target { return color(); }\n'
        self.source.write_bytes(source)
        header = self.source.parent / "color.hlsli"
        header.write_bytes(b"float4 color() { return 0; }\n")
        unit = capture_compilation_unit(self.source, source)
        manifest = {"source": {"path": "source/shader.hlsl"}, "latestCompiled": {
            "revision": 3, "sourceSha256": _sha(source), "rawSha256": "c" * 64,
            "sourceDependencies": {"digest": unit["digest"]}}}
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(watcher_module._already_compiled_candidate(self.root, _sha(source))["revision"], 3)
        header.write_bytes(b"float4 color() { return 1; }\n")
        self.assertIsNone(watcher_module._already_compiled_candidate(self.root, _sha(source)))
        self.assertEqual(self.source.read_bytes(), source)

    def test_file_change_and_editor_save_keep_original_debounce(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        first = self.initial.replace(b"return 0", b"return 1")
        second = self.initial.replace(b"return 0", b"return 2")
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock:
            job._tick()
            clock.return_value = 100.05
            self.source.write_bytes(first)
            job._tick()
            self.assertEqual(job._observed_hash, _sha(first))
            self.assertEqual(service.compile_hashes, [])
            clock.return_value = 100.16
            job._tick()
            self.assertEqual(service.compile_hashes, [_sha(first)])
            clock.return_value = 100.2
            saved = job.save_source(_sha(first), second.decode("utf-8"))
            self.assertTrue(saved["compileScheduled"])
            clock.return_value = 100.25
            job._tick()
            self.assertEqual(service.compile_hashes, [_sha(first)])
            clock.return_value = 100.31
            job._tick()
            self.assertEqual(service.compile_hashes, [_sha(first), _sha(second)])
            with self.assertRaises(WorkspaceWatchConflictError):
                job.save_source(_sha(first), self.initial.decode("utf-8"))

    def test_periodic_read_detects_content_change_even_when_fingerprint_is_preserved(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        fingerprints = {path: watcher_module._poll_file_fingerprint(path) for path in
                        (self.root, self.source.parent, self.source, self.root / "manifest.json")}
        original_stat = self.source.stat()
        changed = self.initial.replace(b"return 0", b"return 3")
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock, \
                mock.patch.object(watcher_module, "_poll_file_fingerprint", side_effect=fingerprints.get):
            job._tick()
            self.source.write_bytes(changed)
            os.utime(self.source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            clock.return_value = 100.5
            job._tick()
            self.assertEqual(job._observed_hash, _sha(self.initial))
            clock.return_value = 100.01 + watcher_module.FULL_OBSERVATION_INTERVAL_SECONDS
            job._tick()
            self.assertEqual(job._observed_hash, _sha(changed))
            clock.return_value += 0.11
            job._tick()
        self.assertEqual(service.compile_hashes, [_sha(changed)])

    def test_new_manifest_candidate_is_adopted_without_waiting_for_periodic_read(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock:
            job._stable_since = 99.0
            job._tick()
            published = {"sourceSha256": _sha(self.initial), "revision": 7,
                         "rawSha256": _sha(b"new candidate")}
            (self.root / "manifest.json").write_text(json.dumps({"latestCompiled": published}), encoding="utf-8")
            clock.return_value = 100.05
            job._tick()
        self.assertEqual(job._latest_candidate_revision, 7)
        self.assertEqual(job._latest_candidate_raw_sha256, published["rawSha256"])
        self.assertEqual(service.compile_hashes, [])

    def test_invalid_source_observation_recovers_without_periodic_delay(self):
        service = _FakeWorkspaceService(self.root)
        job = self._manual_job(service)
        with mock.patch.object(watcher_module.time, "monotonic", return_value=100.0) as clock:
            job._tick()
            self.source.write_bytes(b"\xff")
            clock.return_value = 100.05
            job._tick()
            self.assertIsNotNone(job._observation_error_key)
            self.source.write_bytes(self.initial)
            clock.return_value = 100.1
            job._tick()
        self.assertIsNone(job._observation_error_key)
        self.assertEqual(service.compile_hashes, [])
        self.assertIn("observationRecovered", [event["type"] for event in job._events])

    def test_debounce_and_latest_saved_revision_wins(self) -> None:
        service = _FakeWorkspaceService(self.root, block_first=True)
        manager = self._manager(service)
        started = manager.start(self.root, 100)
        watch_id = started["watchId"]

        time.sleep(0.18)
        self.assertEqual(service.compile_hashes, [])

        first = b"float4 main() : SV_Target { return 1; }\n"
        newest = b"float4 main() : SV_Target { return 2; }\n"
        self.source.write_bytes(first)
        self.assertTrue(service.first_started.wait(3.0))
        self.source.write_bytes(newest)
        service.release_first.set()

        caught_up = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 2,
        )
        self.assertEqual(service.compile_hashes, [_sha(first), _sha(newest)])
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(newest))
        self.assertEqual(caught_up["observedSourceSha256"], _sha(newest))
        self.assertEqual(caught_up["latestCandidateRevision"], 2)
        self.assertEqual(
            caught_up["latestCandidateRawSha256"],
            _sha(b"compiled:" + newest),
        )
        self.assertFalse(caught_up["candidatePolicy"]["applied"])
        self.assertTrue(caught_up["candidatePolicy"]["requiresReplayAbiValidation"])
        event_types = [event["type"] for event in caught_up["events"]]
        self.assertIn("sourceChangedDuringCompile", event_types)
        self.assertEqual(event_types.count("compileSucceeded"), 2)

        audit = json.loads(Path(caught_up["auditPath"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["watchId"], watch_id)
        self.assertEqual(audit["lastCompiledSourceSha256"], _sha(newest))
        stopped = manager.stop(watch_id)
        self.assertEqual(stopped["state"], "stopped")

    def test_suspended_watcher_ignores_restore_writes_and_adopts_the_result(self) -> None:
        # A project restore writes the project HLSL, compiles and applies it
        # itself, and may roll the source back. None of those writes is an
        # editor save; the watcher must stay quiet and then treat whatever is
        # on disk as its new baseline.
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        restored = b"float4 main() : SV_Target { return 3; }\n"
        with manager.suspend_workspace(self.root, "project-restore") as paused:
            self.assertEqual(1, paused)
            self.source.write_bytes(restored)
            time.sleep(0.4)
            self.assertEqual(service.compile_hashes, [])
        time.sleep(0.4)
        self.assertEqual(service.compile_hashes, [])
        status = manager.poll(watch_id)
        self.assertEqual(status["observedSourceSha256"], _sha(restored))
        event_types = [event["type"] for event in status["events"]]
        self.assertIn("watchSuspended", event_types)
        self.assertIn("watchResumed", event_types)
        # A real editor save after the restore is still compiled.
        edited = b"float4 main() : SV_Target { return 4; }\n"
        self.source.write_bytes(edited)
        caught_up = self._wait_for(
            manager, watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited)])
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(edited))
        manager.stop(watch_id)

    def test_review_temporary_candidate_is_not_observed_and_final_revision_is_adopted(self):
        service = _FakeWorkspaceService(self.root)
        def publish(source, revision):
            service.latest = {"sourceSha256": _sha(source), "revision": revision,
                              "rawSha256": _sha(b"compiled:" + source)}
            (self.root / "manifest.json").write_text(
                json.dumps({"latestCompiled": service.latest}), encoding="utf-8")

        publish(self.initial, 1)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        proposal = b"float4 main() : SV_Target { return 9; }\n"
        with source_observation_transaction(self.root):
            self.source.write_bytes(proposal)
            publish(proposal, 2)
            time.sleep(0.3)
            snapshots = []
            reader = threading.Thread(target=lambda: snapshots.append(manager.poll(watch_id)))
            reader.start()
            reader.join(2.0)
            self.assertFalse(reader.is_alive())
            self.assertTrue(snapshots[0]["sourceObservationPaused"])
            self.assertFalse(snapshots[0]["caughtUp"])
            self.assertEqual(snapshots[0]["latestCandidateRevision"], 1)
            self.assertEqual(service.compile_hashes, [])
            self.source.write_bytes(self.initial)
            publish(self.initial, 3)
        result = self._wait_for(manager, watch_id,
            lambda state: state["caughtUp"] and state["latestCandidateRevision"] == 3)
        self.assertEqual(result["lastCompiledSourceSha256"], _sha(self.initial))
        self.assertEqual(service.compile_hashes, [])
        self.assertEqual([event["revision"] for event in result["events"]
                          if event["type"] == "compileAdopted"], [3])
        # A later ordinary editor save remains observable and compiles normally.
        edited = b"float4 main() : SV_Target { return 4; }\n"
        self.source.write_bytes(edited)
        self._wait_for(manager, watch_id, lambda state: state["caughtUp"]
                       and state["lastCompiledSourceSha256"] == _sha(edited))
        self.assertEqual(service.compile_hashes, [_sha(edited)])

    def test_editor_save_uses_real_cas_and_schedules_the_new_source(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        self._wait_for(
            manager, watch_id, lambda result: result["state"] == "running"
        )

        replacement = "float4 main() : SV_Target { return 7; }\n"
        saved = manager.save_source(watch_id, _sha(self.initial), replacement)

        replacement_bytes = replacement.encode("utf-8")
        replacement_sha256 = _sha(replacement_bytes)
        self.assertEqual(replacement_bytes, self.source.read_bytes())
        self.assertEqual(watch_id, saved["watchId"])
        self.assertEqual(replacement_sha256, saved["rawSha256"])
        self.assertEqual(len(replacement_bytes), saved["size"])
        self.assertGreater(saved["modifiedNs"], 0)
        self.assertTrue(saved["changed"])
        self.assertTrue(saved["compileScheduled"])

        compiled = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileSuccesses"] == 1 and result["caughtUp"],
        )
        self.assertEqual([replacement_sha256], service.compile_hashes)
        self.assertEqual(replacement_sha256, compiled["lastCompiledSourceSha256"])
        save_events = [
            event
            for event in compiled["events"]
            if event["type"] == "editorSourceSaved"
        ]
        self.assertEqual(1, len(save_events))
        self.assertEqual(_sha(self.initial), save_events[0]["expectedSha256"])
        self.assertEqual(replacement_sha256, save_events[0]["sourceSha256"])

        unchanged = manager.save_source(
            watch_id, replacement_sha256, replacement
        )
        self.assertFalse(unchanged["changed"])
        self.assertFalse(unchanged["compileScheduled"])
        manager.stop(watch_id)

    def test_editor_save_rejects_stale_hash_without_overwriting_disk(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        self._wait_for(
            manager, watch_id, lambda result: result["state"] == "running"
        )

        external = b"float4 main() : SV_Target { return 8; }\n"
        self.source.write_bytes(external)
        with self.assertRaisesRegex(
            WorkspaceWatchConflictError, "changed on disk"
        ):
            manager.save_source(
                watch_id,
                _sha(self.initial),
                "float4 main() : SV_Target { return 9; }\n",
            )

        self.assertEqual(external, self.source.read_bytes())
        self.assertFalse(
            any(
                event["type"] == "editorSourceSaved"
                for event in manager.poll(watch_id)["events"]
            )
        )
        manager.stop(watch_id)

    def test_editor_save_is_rejected_after_watcher_stops(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        self._wait_for(
            manager, watch_id, lambda result: result["state"] == "running"
        )
        manager.stop(watch_id)

        with self.assertRaisesRegex(WorkspaceWatchError, "not running"):
            manager.save_source(
                watch_id,
                _sha(self.initial),
                "float4 main() : SV_Target { return 10; }\n",
            )
        self.assertEqual(self.initial, self.source.read_bytes())

    def test_explanatory_lane_is_never_watchable(self) -> None:
        class _ExplanatoryService(_FakeWorkspaceService):
            def status(self, workspace_path) -> dict:
                result = super().status(workspace_path)
                result["source"]["lane"] = "explanatory-ir"
                result["representation"].update(
                    {"lane": "explanatory-ir", "compileEligible": False}
                )
                return result

        service = _ExplanatoryService(self.root)
        manager = self._manager(service)
        with self.assertRaisesRegex(WorkspaceWatchError, "executable-editable"):
            manager.start(self.root, 100)
        self.assertEqual(service.compile_hashes, [])

    def test_stale_toolchain_reports_refresh_before_and_after_source_lease(self) -> None:
        diagnostic = "workspace route catalog differs from the process-owned path"
        for after_lease in (False, True):
            for persistent in (False, True):
                with self.subTest(after_lease=after_lease, persistent=persistent):
                    service = _FakeWorkspaceService(self.root)
                    original_status = service.status
                    calls = []

                    def stale_status(workspace_path):
                        status = original_status(workspace_path)
                        calls.append(status)
                        if not after_lease or len(calls) == 2:
                            status["representation"]["compileEligible"] = False
                            status["toolchainStatus"] = "refresh-required"
                            status["toolchainDiagnostic"] = diagnostic
                        return status

                    service.status = stale_status
                    manager = self._manager(service)
                    start = manager.start_persistent if persistent else manager.start
                    with self.assertRaises(WorkspaceWatchError) as caught:
                        start(self.root, 100)
                    message = str(caught.exception)
                    self.assertIn("requires a toolchain refresh", message)
                    self.assertIn(diagnostic, message)
                    self.assertIn("get_shader_workspace_status", message)
                    self.assertNotIn("requires the executable-editable", message)
                    self.assertEqual(len(calls), 2 if after_lease else 1)
                    self.assertFalse((self.root / ".workspace-watch.lock").exists())
                    self.assertEqual(manager._jobs, {})
                    self.assertEqual(service.compile_hashes, [])
                    self.assertEqual(self.source.read_bytes(), self.initial)

    def test_toolchain_refresh_diagnostic_is_bounded_without_copying_action_payload(self) -> None:
        service = _FakeWorkspaceService(self.root)
        status = service.status(self.root)
        status["representation"]["compileEligible"] = False
        status["toolchainStatus"] = "refresh-required"
        status["toolchainDiagnostic"] = "changed tool: " + "x" * 10000
        status["unrelatedMetadata"] = {"tool": "never-execute-this-action", "payload": "y" * 10000}
        service.status = lambda _path: status
        manager = self._manager(service)
        with self.assertRaises(WorkspaceWatchError) as caught:
            manager.start(self.root, 100)
        message = str(caught.exception)
        self.assertLess(len(message), 1400)
        self.assertIn("changed tool: ", message)
        self.assertIn("[truncated]", message)
        self.assertIn("get_shader_workspace_status", message)
        self.assertNotIn("never-execute-this-action", message)
        self.assertNotIn("yyyy", message)
        self.assertEqual(service.compile_hashes, [])
        self.assertFalse((self.root / ".workspace-watch.lock").exists())

    def test_refresh_flag_never_masks_missing_source_wrong_lane_or_changed_identity(self) -> None:
        for after_lease in (False, True):
            faults = ["missing-source", "source-lane", "representation-lane", "no-stale-status"]
            if after_lease:
                faults.append("workspace-identity")
            for fault in faults:
                with self.subTest(after_lease=after_lease, fault=fault):
                    service = _FakeWorkspaceService(self.root)
                    original_status = service.status
                    calls = []

                    def invalid_status(workspace_path):
                        status = original_status(workspace_path)
                        calls.append(status)
                        if not after_lease or len(calls) == 2:
                            status["representation"]["compileEligible"] = False
                            status["toolchainStatus"] = "refresh-required"
                            status["toolchainDiagnostic"] = "must not mask the source error"
                            if fault == "missing-source":
                                status["source"]["present"] = False
                            elif fault == "source-lane":
                                status["source"]["lane"] = "explanatory-ir"
                            elif fault == "representation-lane":
                                status["representation"]["lane"] = "explanatory-ir"
                            elif fault == "workspace-identity":
                                status["workspacePath"] = str(self.root / "different-workspace")
                            else:
                                status.pop("toolchainStatus")
                        return status

                    service.status = invalid_status
                    manager = self._manager(service)
                    expected = (
                        "workspace identity/lane changed" if after_lease else
                        "tracked decompiled source" if fault == "missing-source" else
                        "requires the executable-editable shader lane")
                    with self.assertRaisesRegex(WorkspaceWatchError, expected) as caught:
                        manager.start(self.root, 100)
                    self.assertNotIn("toolchain refresh", str(caught.exception))
                    self.assertNotIn("must not mask", str(caught.exception))
                    self.assertEqual(manager._jobs, {})
                    self.assertEqual(service.compile_hashes, [])
                    self.assertFalse((self.root / ".workspace-watch.lock").exists())
                    self.assertEqual(self.source.read_bytes(), self.initial)

    def test_compile_failure_is_audited_and_new_hash_can_recover(self) -> None:
        service = _FakeWorkspaceService(self.root, fail_first=True)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        failed_source = b"float4 main() : SV_Target { return 3; }\n"
        self.source.write_bytes(failed_source)
        failed = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileFailures"] == 1,
        )
        self.assertEqual(failed["lastError"]["type"], "RuntimeError")
        time.sleep(0.2)
        self.assertEqual(len(service.compile_hashes), 1)

        recovered_source = b"float4 main() : SV_Target { return 4; }\n"
        self.source.write_bytes(recovered_source)
        recovered = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(recovered["compileAttempts"], 2)
        self.assertEqual(recovered["compileFailures"], 1)
        self.assertEqual(recovered["lastCompiledSourceSha256"], _sha(recovered_source))
        self.assertIsNone(recovered["lastError"])
        event_types = [event["type"] for event in recovered["events"]]
        self.assertIn("compileFailed", event_types)
        self.assertIn("compileSucceeded", event_types)
        manager.stop(watch_id)

    def test_transient_windows_file_failure_retries_the_same_save(self) -> None:
        class _TransientService(_FakeWorkspaceService):
            def compile(inner_self, workspace_path) -> dict:
                if not inner_self.compile_hashes:
                    source = (
                        Path(workspace_path) / "source" / "shader.hlsl"
                    ).read_bytes()
                    inner_self.compile_hashes.append(_sha(source))
                    error = PermissionError(13, "revision publish denied")
                    error.winerror = 5
                    raise error
                return super().compile(workspace_path)

        service = _TransientService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        edited = b"float4 main() : SV_Target { return 31; }\n"
        self.source.write_bytes(edited)

        recovered = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"]
            and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited), _sha(edited)])
        self.assertEqual(recovered["compileAttempts"], 2)
        self.assertEqual(recovered["compileFailures"], 1)
        self.assertFalse(recovered["transientRetry"]["scheduled"])
        event_types = [event["type"] for event in recovered["events"]]
        self.assertIn("compileRetryScheduled", event_types)
        manager.stop(watch_id)

    def test_transient_winerror5_persisting_watcher_audit_keeps_watching(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        real_replace = os.replace
        denial_count = 0

        def deny_watcher_audit_once(temporary, destination):
            nonlocal denial_count
            if Path(destination).parent == self.root / "watch" and denial_count == 0:
                denial_count += 1
                error = PermissionError(13, "Access is denied", str(temporary))
                error.winerror = 5
                error.filename2 = str(destination)
                raise error
            return real_replace(temporary, destination)

        with mock.patch(
            "src.shader_workspace.io.os.replace", side_effect=deny_watcher_audit_once
        ), mock.patch("src.shader_workspace.io.time.sleep", return_value=None):
            started = manager.start(self.root, 100)
            edited = b"float4 main() : SV_Target { return 32; }\n"
            self.source.write_bytes(edited)
            recovered = self._wait_for(
                manager,
                started["watchId"],
                lambda result: result["caughtUp"]
                and result["compileSuccesses"] == 1,
            )

        persisted = json.loads(
            Path(recovered["auditPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(denial_count, 1)
        self.assertEqual(recovered["state"], "running")
        self.assertEqual(persisted["compileSuccesses"], 1)
        self.assertEqual(list((self.root / "watch").glob(".*.tmp-*")), [])
        manager.stop(started["watchId"])

    def test_compile_failure_publishes_dxc_line_diagnostics(self) -> None:
        class _DiagnosticService(_FakeWorkspaceService):
            def compile(inner_self, workspace_path) -> dict:
                root = Path(workspace_path)
                source = (root / "source" / "shader.hlsl").read_bytes()
                inner_self.compile_hashes.append(_sha(source))
                job = root / "jobs" / "compile-r0001-diagnostic"
                job.mkdir(parents=True)
                compiler_input = job / "input.hlsl"
                compiler_input.write_bytes(source)
                (job / "stderr.log").write_text(
                    f"{compiler_input}:12:7: error: undeclared identifier 'bad'\n"
                    f"{compiler_input}(14,3): warning X3206: implicit truncation\n",
                    encoding="utf-8",
                )
                raise RuntimeError(f"external tool exited with code 1; see {job}")

        service = _DiagnosticService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]
        self.source.write_bytes(
            b"float4 main() : SV_Target { return bad; }\n"
        )
        failed = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileFailures"] == 1,
        )
        diagnostics = failed["lastError"]["diagnostics"]
        self.assertEqual(
            [
                {
                    "file": str(self.source),
                    "line": 12,
                    "column": 7,
                    "severity": "error",
                    "message": "undeclared identifier 'bad'",
                },
                {
                    "file": str(self.source),
                    "line": 14,
                    "column": 3,
                    "severity": "warning",
                    "message": "X3206: implicit truncation",
                },
            ],
            diagnostics,
        )
        failed_event = next(
            event
            for event in reversed(failed["events"])
            if event["type"] == "compileFailed"
        )
        self.assertEqual(failed_event["diagnostics"], diagnostics)
        manager.stop(watch_id)

    def test_atomic_replace_save_is_debounced_and_compiled_once(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]

        edited = b"float4 main() : SV_Target { return 9; }\n"
        temporary_source = self.source.with_name("shader.hlsl.ide-save")
        temporary_source.write_bytes(edited)
        temporary_source.replace(self.source)

        caught_up = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited)])
        self.assertEqual(caught_up["observedSourceSha256"], _sha(edited))
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(edited))
        manager.stop(watch_id)

    def test_start_catches_up_source_that_differs_from_existing_candidate(self) -> None:
        service = _FakeWorkspaceService(self.root)
        stale_source = b"float4 main() : SV_Target { return 7; }\n"
        service.latest = {
            "sourceSha256": _sha(stale_source),
            "revision": 4,
            "rawSha256": _sha(b"compiled:" + stale_source),
        }
        current = b"float4 main() : SV_Target { return 8; }\n"
        self.source.write_bytes(current)

        manager = self._manager(service)
        started = manager.start(self.root, 100)
        created = started["events"][0]
        self.assertTrue(created["catchUpCompileScheduled"])
        self.assertEqual(created["staleCandidateSourceSha256"], _sha(stale_source))

        caught_up = self._wait_for(
            manager,
            started["watchId"],
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(current)])
        self.assertEqual(caught_up["observedSourceSha256"], _sha(current))
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(current))
        manager.stop(started["watchId"])

    def test_start_reloads_latest_candidate_after_acquiring_source_lease(self) -> None:
        class _RevisionChangesBeforeLeaseService(_FakeWorkspaceService):
            def __init__(inner_self, root):
                super().__init__(root)
                inner_self.status_calls = 0
                inner_self.latest = {
                    "sourceSha256": _sha(self.initial),
                    "revision": 4,
                    "rawSha256": "4" * 64,
                }

            def status(inner_self, workspace_path):
                result = super().status(workspace_path)
                inner_self.status_calls += 1
                if inner_self.status_calls == 1:
                    # Simulate an organization recovery publishing a newer
                    # candidate after preflight but before watcher lease entry.
                    inner_self.latest = {
                        "sourceSha256": _sha(self.initial),
                        "revision": 5,
                        "rawSha256": "5" * 64,
                    }
                return result

        service = _RevisionChangesBeforeLeaseService(self.root)
        manager = self._manager(service)
        started = manager.start(self.root, 100)
        self.assertGreaterEqual(service.status_calls, 2)
        self.assertEqual(started["latestCandidateRevision"], 5)
        self.assertEqual(started["latestCandidateRawSha256"], "5" * 64)
        self.assertTrue(started["caughtUp"])
        self.assertFalse(started["events"][0]["catchUpCompileScheduled"])
        manager.stop(started["watchId"])

    def test_start_catches_up_edit_saved_before_first_candidate(self) -> None:
        service = _FakeWorkspaceService(self.root)
        edited = b"float4 main() : SV_Target { return 11; }\n"
        self.source.write_bytes(edited)

        manager = self._manager(service)
        started = manager.start(self.root, 100)
        created = started["events"][0]
        self.assertTrue(created["catchUpCompileScheduled"])
        self.assertEqual(created["catchUpReferenceKind"], "decompiled-source")
        self.assertEqual(created["catchUpReferenceSourceSha256"], _sha(self.initial))
        self.assertIsNone(created["staleCandidateSourceSha256"])

        caught_up = self._wait_for(
            manager,
            started["watchId"],
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        self.assertEqual(service.compile_hashes, [_sha(edited)])
        self.assertEqual(caught_up["lastCompiledSourceSha256"], _sha(edited))
        manager.stop(started["watchId"])

    def test_failed_new_save_preserves_previous_candidate_as_last_good(self) -> None:
        service = _FakeWorkspaceService(self.root, fail_attempt=2)
        manager = self._manager(service)
        watch_id = manager.start(self.root, 100)["watchId"]

        good = b"float4 main() : SV_Target { return 5; }\n"
        self.source.write_bytes(good)
        previous = self._wait_for(
            manager,
            watch_id,
            lambda result: result["caughtUp"] and result["compileSuccesses"] == 1,
        )
        previous_raw = _sha(b"compiled:" + good)
        self.assertEqual(previous["latestCandidateRevision"], 1)
        self.assertEqual(previous["latestCandidateRawSha256"], previous_raw)

        invalid = b"float4 main() : SV_Target { syntax error }\n"
        self.source.write_bytes(invalid)
        failed = self._wait_for(
            manager,
            watch_id,
            lambda result: result["compileFailures"] == 1,
        )
        self.assertEqual(failed["latestCandidateRevision"], 1)
        self.assertEqual(failed["latestCandidateRawSha256"], previous_raw)
        self.assertEqual(failed["lastCompiledSourceSha256"], _sha(good))
        self.assertEqual(failed["observedSourceSha256"], _sha(invalid))
        self.assertFalse(failed["caughtUp"])
        self.assertEqual(service.latest["revision"], 1)
        self.assertEqual(service.latest["rawSha256"], previous_raw)
        time.sleep(0.2)
        self.assertEqual(len(service.compile_hashes), 2)
        manager.stop(watch_id)

    def test_explanatory_lane_cannot_start_executable_save_watcher(self) -> None:
        service = _FakeWorkspaceService(self.root)
        original_status = service.status

        def explanatory_status(workspace_path):
            status = original_status(workspace_path)
            status["source"]["lane"] = "explanatory-ir"
            status["representation"]["lane"] = "explanatory-ir"
            status["representation"]["compileEligible"] = False
            return status

        service.status = explanatory_status
        manager = self._manager(service)
        with self.assertRaisesRegex(WorkspaceWatchError, "executable-editable"):
            manager.start(self.root, 100)

    def test_contract_rejects_invalid_debounce_duplicate_and_watch_id(self) -> None:
        service = _FakeWorkspaceService(self.root)
        manager = self._manager(service)
        with self.assertRaisesRegex(WorkspaceWatchError, "integer"):
            manager.start(self.root, True)
        with self.assertRaisesRegex(WorkspaceWatchError, "between"):
            manager.start(self.root, 99)
        watch_id = manager.start(self.root, 100)["watchId"]
        with self.assertRaisesRegex(WorkspaceWatchError, "active watcher"):
            manager.start(self.root, 100)
        with self.assertRaisesRegex(WorkspaceWatchError, "lowercase hex"):
            manager.poll("../../not-a-watch")
        manager.stop(watch_id)

    def test_watchers_share_a_workspace_and_a_stale_exclusive_lock_is_retired(self) -> None:
        """Nobody owns the source: two watchers coexist; an old crash lock never blocks."""

        from src.shader_workspace.leases import participants

        # Residue of the retired exclusive design: a lock file from a crashed
        # older build. It is moved aside, not honoured.
        stale = self.root / ".workspace-watch.lock"
        stale.write_text("crashed-owner-token\n", encoding="ascii")
        first_service = _FakeWorkspaceService(self.root)
        second_service = _FakeWorkspaceService(self.root)
        first = self._manager(first_service)
        second = self._manager(second_service)
        first_id = first.start(self.root, 100)["watchId"]
        self.assertFalse(stale.exists())
        retired = list((self.root / "watch").glob("retired-exclusive-lease-*.lock"))
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].read_text(encoding="ascii"), "crashed-owner-token\n")

        second_id = second.start(self.root, 100)["watchId"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(participants(self.root)), 2)
        stopped = first.stop(first_id)
        self.assertTrue(stopped["events"][-1]["watchLeaseReleased"])
        self.assertEqual(len(participants(self.root)), 1)
        second.stop(second_id)
        self.assertEqual(participants(self.root), [])

    def test_lease_is_a_participant_record_not_an_exclusion(self) -> None:
        from src.shader_workspace.leases import participants

        one = WorkspaceSourceTransactionLease(self.root, owner_instance_id="1" * 32)
        two = WorkspaceSourceTransactionLease(self.root, owner_instance_id="1" * 32,
                                              recover_orphaned=True, recover_legacy=True)
        one.acquire()
        two.acquire()
        self.assertTrue(one.owned and two.owned)
        self.assertFalse((self.root / ".workspace-watch.lock").exists())
        owners = {item["ownerInstanceId"] for item in participants(self.root)}
        self.assertEqual(owners, {"1" * 32})
        self.assertEqual(len(participants(self.root)), 2)
        self.assertTrue(one.release())
        self.assertFalse(one.release())
        self.assertTrue(two.release())
        self.assertEqual(participants(self.root), [])


if __name__ == "__main__":
    unittest.main()
