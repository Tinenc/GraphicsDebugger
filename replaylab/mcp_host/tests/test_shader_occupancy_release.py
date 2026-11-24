"""Idle AI-task Shader occupancy release and organization idle marking."""

from __future__ import annotations

import json
import hashlib
import unittest
from pathlib import Path
from unittest import mock

from src import server


class ShaderOccupancyReleaseTests(unittest.TestCase):
    def _watcher_delivery(self, workspace: Path):
        workspace.mkdir(exist_ok=True)
        source = workspace / "shader.hlsl"
        source.write_bytes(b"float4 MainPS() { return 1; }\n")
        candidate = workspace / "shader.dxil"
        candidate.write_bytes(b"compiled candidate")
        raw_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        status = {
            "workspaceId": "a" * 32, "workspacePath": str(workspace), "revision": 11,
            "capture": {"captureId": "b" * 64, "capturePath": "D:/capture.rdc"},
            "stage": "fragment", "entryPoint": "MainPS",
            "shader": {"resourceId": 1000000000000000723, "rawSha256": "c" * 64},
            "representation": {"lane": "executable-editable", "compileEligible": True},
            "source": {"path": str(source), "currentSha256": source_sha},
            "latestCompiled": {"revision": 11, "rawSha256": raw_sha,
                               "sourceSha256": source_sha, "candidateBlobPath": str(candidate)},
        }
        # The live watcher may reuse an older successful physical receipt for
        # source revision 11 because its new compile produced identical bytes.
        replacement = {"replacementId": "d" * 32, "candidateRawSha256": raw_sha, "revision": 10}
        binding = {"instanceId": "instance-1", "captureSessionId": "e" * 32,
                   "capturePath": "D:/capture.rdc"}
        live = {
            "liveEditId": "f" * 32, "workspacePath": str(workspace),
            "state": "running", "sessionState": "current", "captureBinding": binding,
            "latestHandledRevision": 11, "latestHandledCandidateRawSha256": raw_sha,
            "activeReplacement": replacement,
            "lastApply": {"applied": True, "replacement": replacement,
                          "validation": {"replayAbiValidated": True, "reusedValidation": True}},
        }
        inventory = {"captureId": "b" * 64, "captureSessionId": "e" * 32,
                     "replacements": [{**replacement, "workspaceId": "a" * 32,
                                       "originalResourceId": "1000000000000000723",
                                       "originalRawSha256": "c" * 64,
                                       "stage": "fragment", "entryPoint": "MainPS"}]}
        context = {"capture": {"captureSessionId": "e" * 32, "path": "D:/capture.rdc"}}
        return status, live, inventory, context

    def test_watcher_applied_delivery_is_retained_and_reconciled_at_task_end(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            status, live, inventory, context = self._watcher_delivery(workspace)
            task_id = "watcher-delivery"
            state_path = root / f"{task_id}.report-state.json"
            state_path.write_text(json.dumps({
                "schema": "replaylab-task-report-state/v1", "taskId": task_id,
                "pendingShaderWork": {str(workspace): {"delivery": {
                    "liveEditStarted": True, "liveEditId": "f" * 32, "replayApplied": False}}},
            }), encoding="utf-8")
            service = mock.Mock()
            service.status.return_value = status
            with mock.patch.object(server, "_shader_live_edit_remote", return_value=live) as remote, \
                    mock.patch.object(server, "_shader_workspace_service", return_value=service), \
                    mock.patch.object(server, "_active_live_context", return_value=context), \
                    mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", "e" * 32), \
                    mock.patch.object(server.ipc, "instance_id", "instance-1"), \
                    mock.patch.object(server.ipc, "call", return_value=inventory) as ipc_call:
                result = server._release_task_shader_occupancy(task_id=task_id, event_root=root)
            remote.assert_called_once_with("poll", {"live_edit_id": "f" * 32})
            ipc_call.assert_called_once_with("get_shared_shader_replacements", {
                "expected_capture_id": "b" * 64, "expected_capture_session_id": "e" * 32,
            }, timeout=60.0)
            self.assertEqual(result["stoppedLiveEditIds"], [])
            self.assertEqual(result["retainedLiveEditIds"], ["f" * 32])
            self.assertEqual(result["reconciledDeliveries"][0]["sourceSha256"], status["source"]["currentSha256"])
            self.assertEqual(result["reconciledDeliveries"][0]["revision"], 11)
            delivery = json.loads(state_path.read_text(encoding="utf-8"))["pendingShaderWork"][str(workspace)]["delivery"]
            self.assertTrue(delivery["replayApplied"])
            self.assertTrue(delivery["liveEditStarted"])
            self.assertEqual(delivery["replacementId"], "d" * 32)

    def test_watcher_delivery_does_not_adopt_stale_or_other_source_and_capture(self) -> None:
        import tempfile

        cases = ("session", "instance", "workspace", "resource", "candidate", "source-binding",
                 "current-replacement", "not-applied", "changed-source-file", "changed-candidate-file")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "workspace"
                status, live, inventory, context = self._watcher_delivery(workspace)
                if case == "session":
                    live["captureBinding"]["captureSessionId"] = "1" * 32
                elif case == "instance":
                    live["captureBinding"]["instanceId"] = "other-instance"
                elif case == "workspace":
                    live["workspacePath"] = str(Path(tmp) / "other-workspace")
                elif case == "resource":
                    inventory["replacements"][0]["originalResourceId"] = 1000000000000000755
                elif case == "candidate":
                    live["latestHandledCandidateRawSha256"] = "2" * 64
                elif case == "source-binding":
                    status["latestCompiled"]["sourceSha256"] = "3" * 64
                elif case == "current-replacement":
                    inventory["replacements"] = []
                elif case == "not-applied":
                    live["lastApply"]["applied"] = False
                elif case == "changed-source-file":
                    Path(status["source"]["path"]).write_bytes(b"new uncompiled source")
                elif case == "changed-candidate-file":
                    Path(status["latestCompiled"]["candidateBlobPath"]).write_bytes(b"other candidate bytes")
                service = mock.Mock()
                service.status.return_value = status
                with mock.patch.object(server, "_shader_live_edit_remote", return_value=live), \
                        mock.patch.object(server, "_shader_workspace_service", return_value=service), \
                        mock.patch.object(server, "_active_live_context", return_value=context), \
                        mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", "e" * 32), \
                        mock.patch.object(server.ipc, "instance_id", "instance-1"), \
                        mock.patch.object(server.ipc, "call", return_value=inventory):
                    self.assertIsNone(server._current_live_edit_delivery(str(workspace), "f" * 32))

    def test_delivery_readback_failure_does_not_revoke_a_possible_editor(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "readback-failed.report-state.json").write_text(json.dumps({
                "schema": "replaylab-task-report-state/v1", "taskId": "readback-failed",
                "pendingShaderWork": {str(root): {"delivery": {
                    "liveEditStarted": True, "liveEditId": "f" * 32, "replayApplied": False}}},
            }), encoding="utf-8")
            with mock.patch.object(server, "_shader_live_edit_remote", side_effect=RuntimeError("unavailable")) as remote:
                result = server._release_task_shader_occupancy(task_id="readback-failed", event_root=root)
            remote.assert_called_once_with("poll", {"live_edit_id": "f" * 32})
            self.assertEqual(result["stoppedLiveEditIds"], [])
            self.assertFalse(result["released"])
            self.assertTrue(result["errors"][0].startswith("inspect-delivery:"))

    def test_release_idle_organization_sessions_preserves_active_drafts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            session_dir = workspace / "ai" / "organization-sessions" / ("a" * 32)
            session_dir.mkdir(parents=True)
            session_path = session_dir / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "organizationId": "a" * 32,
                        "state": "awaiting-proposal-review",
                    }
                ),
                encoding="utf-8",
            )
            released = server._release_idle_organization_sessions(
                workspace, reason="ai-task-idle"
            )
            self.assertEqual(released, [])
            updated = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["state"], "awaiting-proposal-review")
            self.assertNotIn("releasedReason", updated)
            self.assertTrue(server._workspace_has_active_organization(workspace))

    def test_completed_delivery_retains_persistent_live_edit_and_state(
        self,
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            event_root = Path(tmp)
            workspace = event_root / "shader-ws"
            workspace.mkdir()
            task_id = "task-occupancy-1"
            state_path = event_root / f"{task_id}.report-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema": "replaylab-task-report-state/v1",
                        "taskId": task_id,
                        "pendingShaderWork": {
                            str(workspace): {
                                "organizationId": "b" * 32,
                                "state": "delivered",
                                "delivery": {
                                    "liveEditStarted": True,
                                    "liveEditId": "c" * 32,
                                    "replayApplied": True,
                                    "replacementId": "d" * 32,
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            session_dir = workspace / "ai" / "organization-sessions" / ("b" * 32)
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(
                json.dumps(
                    {
                        "organizationId": "b" * 32,
                        "state": "soft-publish-ready",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                server, "_shader_live_edit_remote"
            ) as live_edit:
                result = server._release_task_shader_occupancy(
                    task_id=task_id,
                    event_root=event_root,
                    rollback_replacement=False,
                    reason="ai-task-idle",
                )
            live_edit.assert_not_called()
            self.assertEqual(result["stoppedLiveEditIds"], [])
            self.assertEqual(result["retainedLiveEditIds"], ["c" * 32])
            self.assertEqual(result["releasedOrganizations"], [])
            self.assertEqual(result["rolledBackReplacementIds"], [])
            updated = json.loads(state_path.read_text(encoding="utf-8"))
            delivery = updated["pendingShaderWork"][str(workspace)]["delivery"]
            self.assertTrue(delivery.get("liveEditStarted"))
            self.assertEqual(delivery.get("liveEditId"), "c" * 32)
            self.assertTrue(delivery.get("replayApplied"))

    def test_unapplied_live_edit_is_stopped_at_task_end(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            event_root = Path(tmp)
            task_id = "task-occupancy-incomplete"
            workspace = event_root / "shader-ws"
            workspace.mkdir()
            state_path = event_root / f"{task_id}.report-state.json"
            state_path.write_text(
                json.dumps({
                    "schema": "replaylab-task-report-state/v1",
                    "taskId": task_id,
                    "pendingShaderWork": {
                        str(workspace): {
                            "state": "correction-required",
                            "delivery": {
                                "liveEditStarted": True,
                                "liveEditId": "e" * 32,
                                "replayApplied": False,
                            },
                        }
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                server, "_shader_live_edit_remote",
                return_value={"state": "stopped", "liveEditId": "e" * 32},
            ) as live_edit:
                result = server._release_task_shader_occupancy(
                    task_id=task_id,
                    event_root=event_root,
                    rollback_replacement=False,
                )
            self.assertEqual(live_edit.call_args_list, [
                mock.call("poll", {"live_edit_id": "e" * 32}),
                mock.call("stop", {"live_edit_id": "e" * 32}),
            ])
            self.assertEqual(result["retainedLiveEditIds"], [])
            self.assertEqual(result["stoppedLiveEditIds"], ["e" * 32])
            updated = json.loads(state_path.read_text(encoding="utf-8"))
            delivery = updated["pendingShaderWork"][str(workspace)]["delivery"]
            self.assertFalse(delivery["liveEditStarted"])
            self.assertNotIn("liveEditId", delivery)

    def test_terminal_failure_or_cancellation_preserves_applied_replacement(self) -> None:
        import tempfile

        for terminal_kind in ("failed", "cancelled"):
            with self.subTest(terminal_kind=terminal_kind), tempfile.TemporaryDirectory() as tmp:
                event_root = Path(tmp)
                workspace = event_root / "shader-ws"
                workspace.mkdir()
                task_id = "task-applied-" + terminal_kind
                state_path = event_root / f"{task_id}.report-state.json"
                state_path.write_text(json.dumps({
                    "schema": "replaylab-task-report-state/v1",
                    "taskId": task_id,
                    "pendingShaderWork": {
                        str(workspace): {"delivery": {
                            "liveEditStarted": True,
                            "liveEditId": "a" * 32,
                            "replayApplied": True,
                            "replacementId": "b" * 32,
                        }},
                    },
                }), encoding="utf-8")
                (event_root / f"{task_id}.jsonl").write_text(
                    json.dumps({
                        "taskId": task_id,
                        "kind": terminal_kind,
                        "data": {"terminalStatus": terminal_kind},
                    }) + "\n",
                    encoding="utf-8",
                )
                with mock.patch.object(server, "_task_event_root", return_value=event_root), \
                        mock.patch.object(server, "_shader_live_edit_remote") as live_edit, \
                        mock.patch.object(server.ipc, "call") as ipc_call:
                    server._release_terminal_task_shader_occupancy_sweep()
                live_edit.assert_not_called()
                ipc_call.assert_not_called()
                delivery = json.loads(state_path.read_text(encoding="utf-8"))[
                    "pendingShaderWork"
                ][str(workspace)]["delivery"]
                self.assertTrue(delivery["replayApplied"])
                self.assertEqual(delivery["replacementId"], "b" * 32)
                self.assertTrue(delivery["liveEditStarted"])

    def test_failed_delivery_stops_live_edit_before_rollback(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            event_root = Path(tmp)
            task_id = "task-occupancy-failed"
            workspace = event_root / "shader-ws"
            workspace.mkdir()
            (event_root / f"{task_id}.report-state.json").write_text(
                json.dumps({
                    "schema": "replaylab-task-report-state/v1",
                    "taskId": task_id,
                    "pendingShaderWork": {
                        str(workspace): {
                            "delivery": {
                                "liveEditStarted": True,
                                "liveEditId": "a" * 32,
                                "replayApplied": True,
                                "replacementId": "b" * 32,
                            },
                        },
                    },
                }),
                encoding="utf-8",
            )
            operations = []

            def stop_live_edit(operation, parameters):
                operations.append(operation)
                self.assertEqual(parameters["live_edit_id"], "a" * 32)
                return {"state": "stopped"}

            def rollback(operation, parameters, timeout):
                operations.append(operation)
                self.assertEqual(parameters["replacement_id"], "b" * 32)
                self.assertEqual(timeout, 120.0)
                return {"rolledBack": True}

            workspace_service = mock.Mock()
            workspace_service.status.return_value = {
                "capture": {"captureId": "c" * 64},
                "workspaceId": "d" * 32,
            }
            with mock.patch.object(server, "_shader_live_edit_remote",
                                   side_effect=stop_live_edit), \
                    mock.patch.object(server, "_shader_workspace_service",
                                      return_value=workspace_service), \
                    mock.patch.object(server.ipc, "call", side_effect=rollback):
                result = server._release_task_shader_occupancy(
                    task_id=task_id, event_root=event_root,
                    rollback_replacement=True)
            self.assertEqual(operations, [
                "stop", "rollback_shader_candidate_shared_resource"])
            self.assertEqual(result["stoppedLiveEditIds"], ["a" * 32])
            self.assertEqual(result["retainedLiveEditIds"], [])
            self.assertEqual(result["rolledBackReplacementIds"], ["b" * 32])


class ShaderOwnerHandoffTests(unittest.TestCase):
    def test_cross_catalog_idle_handoff_keeps_last_good_and_all_workspace_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            manifest = workspace / "manifest.json"
            manifest.write_text(json.dumps({"workspaceId": "a" * 32}), encoding="utf8")
            source = workspace / "shader.hlsl"
            source.write_bytes(b"saved edited source")
            row = {"workspaceId": "a" * 32, "captureId": "b" * 64,
                   "captureSessionId": "c" * 32, "originalResourceId": 77,
                   "replacementId": "d" * 32, "candidateRawSha256": "e" * 64, "revision": 21}
            session = {"workspacePath": str(workspace), "liveEditId": "f" * 32,
                       "state": "running", "pendingApply": None, "applyInProgress": None}
            client = mock.Mock()
            client.call.return_value = {"replacements": [row]}
            with mock.patch.object(server, "_release_terminal_task_shader_occupancy_sweep"), \
                    mock.patch.object(server, "_shader_live_edit_remote", side_effect=[
                        {"sessions": [session]}, {"state": "stopped"}]) as live:
                result = server._takeover_idle_foreign_shared_shader_owner(
                    ipc=client, capture_id="1" * 64, capture_session_id="c" * 32,
                    original_resource_id=77, claimant_workspace_id="2" * 32)
            client.call.assert_called_once_with("get_shared_shader_replacements", {
                "expected_capture_id": "1" * 64, "expected_capture_session_id": "c" * 32,
                "include_all_capture_catalogs": True}, timeout=60.0)
            self.assertEqual(live.call_args_list, [mock.call("list", {}),
                             mock.call("stop", {"live_edit_id": "f" * 32})])
            self.assertFalse(result["released"])
            self.assertTrue(result["retryApply"])
            self.assertEqual(result["expectedOwner"], {key: row[key] for key in (
                "replacementId", "workspaceId", "captureId", "captureSessionId",
                "candidateRawSha256", "revision")})
            self.assertEqual(source.read_bytes(), b"saved edited source")
            self.assertEqual(json.loads(manifest.read_text(encoding="utf8")), {"workspaceId": "a" * 32})

    def test_busy_or_unreadable_live_owner_never_hands_off(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "manifest.json").write_text(json.dumps({"workspaceId": "a" * 32}))
            row = {"workspaceId": "a" * 32, "captureId": "b" * 64,
                   "originalResourceId": 77, "replacementId": "d" * 32}
            for outcome in ({"sessions": [{"workspacePath": str(workspace),
                    "state": "running", "applyInProgress": {"revision": 2}}]},
                    RuntimeError("live owner unavailable"), {}):
                client = mock.Mock()
                client.call.return_value = {"replacements": [row]}
                with self.subTest(outcome=outcome), \
                        mock.patch.object(server, "_release_terminal_task_shader_occupancy_sweep"), \
                        mock.patch.object(server, "_notify_shader_occupancy"), \
                        mock.patch.object(server, "_shader_live_edit_remote", side_effect=[outcome]) as live:
                    result = server._takeover_idle_foreign_shared_shader_owner(
                        ipc=client, capture_id="1" * 64, capture_session_id="c" * 32,
                        original_resource_id=77, claimant_workspace_id="2" * 32)
                self.assertFalse(result["released"])
                self.assertNotIn("expectedOwner", result)
                self.assertNotIn("retryApply", result)
                self.assertEqual(client.call.call_count, 1)
                live.assert_called_once_with("list", {})

    def test_owner_already_gone_allows_one_mutation_side_recheck(self):
        client = mock.Mock()
        client.call.return_value = {"replacements": []}
        with mock.patch.object(server, "_release_terminal_task_shader_occupancy_sweep"), \
                mock.patch.object(server, "_shader_live_edit_remote") as live:
            result = server._takeover_idle_foreign_shared_shader_owner(
                ipc=client, capture_id="1" * 64, capture_session_id="c" * 32,
                original_resource_id=77, claimant_workspace_id="2" * 32)
        self.assertEqual(result, {"released": False, "retryApply": True, "reason": "no-foreign-owner"})
        live.assert_not_called()

    def test_apply_passes_exact_handoff_and_retries_only_once(self):
        import copy
        from contextlib import ExitStack

        status = {"workspaceId": "a" * 32, "workspacePath": "fixture", "revision": 1,
                  "capture": {"captureId": "b" * 64, "captureSessionId": "c" * 32},
                  "latestCompiled": {"rawSha256": "d" * 64, "abiGate": {
                      "status": "compatible", "requestId": "e" * 64, "reportSha256": "f" * 64}}}
        request = {"requestId": "e" * 64, "params": {}, "binding": {
            "workspaceId": "a" * 32, "captureId": "b" * 64, "revision": 1,
            "candidateRawSha256": "d" * 64}}
        service = mock.Mock()
        service.status.return_value = status
        service.candidate_reflection_request.return_value = request
        proof = {"workspaceId": "1" * 32, "captureId": "2" * 64,
                 "captureSessionId": "c" * 32, "replacementId": "3" * 32,
                 "candidateRawSha256": "4" * 64, "revision": 5}
        for owner in (proof, None):
            for retry_fails in (False, True):
                calls = []

                def apply(method, params, timeout):
                    calls.append((method, copy.deepcopy(params)))
                    if len(calls) == 1 or retry_fails:
                        raise RuntimeError("owned by a different workspace")
                    return {"replacementId": "5" * 32, "operationId": "6" * 32}

                takeover = {"released": False, "retryApply": True}
                if owner is not None:
                    takeover["expectedOwner"] = owner
                patches = {
                    "_observe_current_shared_shader_candidate": mock.Mock(return_value=None),
                    "_fresh_shader_candidate_validation": mock.Mock(return_value=(
                        service, status, request, {"replayAbiValidated": True, "abiReportSha256": "f" * 64})),
                    "_require_shader_candidate_binding": mock.Mock(return_value="7" * 64),
                    "_expected_shared_shader_anchor": mock.Mock(return_value={
                        "eventId": 123, "originalResourceId": 77, "originalRawSha256": "8" * 64}),
                    "_require_workspace_live_edit_quiescent": mock.Mock(),
                    "_takeover_idle_foreign_shared_shader_owner": mock.Mock(return_value=takeover),
                    "_validated_shared_shader_apply_response": mock.Mock(side_effect=lambda value, **unused: value),
                    "_remember_shared_shader_replacement": mock.Mock(),
                    "_remember_route_success": mock.Mock(),
                }
                patches["_remember_route_success"].return_value = None
                with self.subTest(owner=owner is not None, retry_fails=retry_fails), ExitStack() as stack:
                    for name, value in patches.items():
                        stack.enter_context(mock.patch.object(server, name, value))
                    stack.enter_context(mock.patch.object(server.ipc, "call", side_effect=apply))
                    result = json.loads(server.apply_shader_workspace_shared_resource(
                        "fixture", 1, "d" * 64, "6" * 32, expected_anchor_event_id=123))
                self.assertEqual(len(calls), 2)
                self.assertNotIn("expected_owner", calls[0][1])
                self.assertEqual(calls[1][1].get("expected_owner"), owner)
                self.assertEqual(result.get("applied") is True, not retry_fails)
                if owner is not None and not retry_fails:
                    self.assertTrue(result["idleOwnerTakeover"]["handoffCompleted"])

    def test_committed_handoff_keeps_validated_effect_when_post_apply_read_fails(self):
        from contextlib import ExitStack

        status = {"workspaceId": "a" * 32, "workspacePath": "fixture", "revision": 1,
                  "capture": {"captureId": "b" * 64, "captureSessionId": "c" * 32},
                  "latestCompiled": {"rawSha256": "d" * 64, "abiGate": {
                      "status": "compatible", "requestId": "e" * 64, "reportSha256": "f" * 64}}}
        request = {"requestId": "e" * 64, "params": {}, "binding": {
            "workspaceId": "a" * 32, "captureId": "b" * 64, "revision": 1,
            "candidateRawSha256": "d" * 64}}
        proof = {"replacementId": "3" * 32}
        applied = {"replacementId": "5" * 32, "operationId": "6" * 32,
                   "operationCreatedReplacement": True, "idempotent": False, "metadataOnly": False,
                   "captureId": "b" * 64, "workspaceId": "a" * 32, "originalResourceId": 77,
                   "supersededReplacementId": "3" * 32}
        for failure in ("status", "request", "source"):
            with self.subTest(failure=failure), ExitStack() as stack:
                service = mock.Mock()
                service.status.side_effect = [status, OSError("source read failed")] if failure == "status" else [status, status]
                service.candidate_reflection_request.side_effect = [request, OSError("request read failed")] if failure == "request" else [request, request]
                binding = mock.Mock(return_value="7" * 64)
                if failure == "source":
                    binding.side_effect = ["7" * 64, "7" * 64, "8" * 64]
                remember = mock.Mock()
                route = mock.Mock()
                patches = {
                    "_observe_current_shared_shader_candidate": mock.Mock(return_value=None),
                    "_fresh_shader_candidate_validation": mock.Mock(return_value=(service, status, request,
                        {"replayAbiValidated": True, "abiReportSha256": "f" * 64})),
                    "_require_shader_candidate_binding": binding,
                    "_expected_shared_shader_anchor": mock.Mock(return_value={"eventId": 123,
                        "originalResourceId": 77, "originalRawSha256": "8" * 64}),
                    "_require_workspace_live_edit_quiescent": mock.Mock(),
                    "_takeover_idle_foreign_shared_shader_owner": mock.Mock(return_value={
                        "released": False, "retryApply": True, "expectedOwner": proof}),
                    "_validated_shared_shader_apply_response": mock.Mock(return_value=applied),
                    "_remember_shared_shader_replacement": remember,
                    "_remember_route_success": route,
                }
                for name, value in patches.items():
                    stack.enter_context(mock.patch.object(server, name, value))
                ipc = stack.enter_context(mock.patch.object(server.ipc, "call", side_effect=[
                    RuntimeError("owned by a different workspace"), applied]))
                result = json.loads(server.apply_shader_workspace_shared_resource(
                    "fixture", 1, "d" * 64, "6" * 32, expected_anchor_event_id=123))
                self.assertIn("verification failed", result["error"])
                self.assertIs(result["data"]["currentSourceVerified"], False)
                self.assertIs(result["data"]["replayReplacementRetained"], True)
                self.assertEqual(result["data"]["replacement"]["replacementId"], "5" * 32)
                self.assertNotIn("applied", result)
                self.assertEqual(ipc.call_count, 2)
                self.assertTrue(all(call.args[0] == "apply_shader_candidate_shared_resource" for call in ipc.call_args_list))
                remember.assert_called_once()
                self.assertIs(remember.call_args.kwargs["status"], status)
                route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
