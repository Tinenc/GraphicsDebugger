from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from src import asset_index_client, asset_index_jobs, console_events, internal_host, server


class AssetIndexOwnerDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "symbols.sqlite3"
        self.assets = self.root / "assets"
        self.state = self.root / "internal-host.json"
        self.generation = "a" * 32
        env = mock.patch.dict(os.environ, {
            "REPLAYLAB_HOST_INSTANCE_ID": self.generation,
            "REPLAYLAB_INTERNAL_RUNTIME_DIR": str(self.root),
        })
        env.start()
        self.addCleanup(env.stop)
        self.write_state()
        self.scope = (object(), {"scope": {"buildId": "build"}}, self.assets, self.database)

    def write_state(self, **changes):
        state = {"schema": "replaylab-internal-host/v1", "pid": os.getpid(),
                 "hostInstanceId": self.generation}
        state.update(changes)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def test_current_generation_status_uses_local_jobs_without_http(self):
        with mock.patch.object(server, "_active_asset_catalog_context", return_value=self.scope) as context, \
                mock.patch.object(asset_index_jobs, "index_job_status", return_value={"status": "running"}) as local, \
                mock.patch.object(asset_index_client, "request") as remote:
            self.assertEqual({"status": "running"}, server._asset_index_status(self.database))
        context.assert_called_once_with(require_catalog=False, create_dictionary=False)
        local.assert_called_once_with(self.database)
        remote.assert_not_called()

    def test_current_generation_ensure_validates_scope_and_keeps_persistent_ownership(self):
        with mock.patch.object(server, "_active_asset_catalog_context", return_value=self.scope) as context, \
                mock.patch.object(asset_index_jobs, "ensure_index_job", return_value={"owner": "ReplayLabInternal"}) as local, \
                mock.patch.object(asset_index_client, "request") as remote:
            self.assertEqual({"owner": "ReplayLabInternal"}, server._asset_index_job(
                self.database, self.assets, "build", depth="names"))
        context.assert_called_once_with(require_catalog=False, create_dictionary=True)
        local.assert_called_once_with(self.database, self.assets, "build", depth="names", owner="ReplayLabInternal")
        remote.assert_not_called()

    def test_local_dispatch_rejects_changed_database_root_or_build_before_starting(self):
        with mock.patch.object(server, "_active_asset_catalog_context", return_value=self.scope), \
                mock.patch.object(asset_index_jobs, "ensure_index_job") as ensure, \
                mock.patch.object(asset_index_jobs, "index_job_status") as status, \
                mock.patch.object(asset_index_client, "request") as remote:
            for database, assets, build in (
                (self.root / "other.sqlite3", self.assets, "build"),
                (self.database, self.root / "other-assets", "build"),
                (self.database, self.assets, "other-build"),
            ):
                with self.subTest(database=database, assets=assets, build=build):
                    with self.assertRaisesRegex(RuntimeError, "scope changed"):
                        server._asset_index_job(database, assets, build)
            with self.assertRaisesRegex(RuntimeError, "scope changed"):
                server._asset_index_status(self.root / "other.sqlite3")
        ensure.assert_not_called()
        status.assert_not_called()
        remote.assert_not_called()

    def test_foreign_invalid_or_mismatched_process_identity_retains_http_route(self):
        mutations = ({"pid": os.getpid() + 1}, {"pid": str(os.getpid())}, {"pid": True},
                     {"hostInstanceId": "b" * 32}, {"schema": "wrong"})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.write_state(**mutation)
                with mock.patch.object(asset_index_client, "request", return_value={"job": {"status": "remote"}}) as remote, \
                        mock.patch.object(server, "_active_asset_catalog_context") as context:
                    self.assertEqual("remote", server._asset_index_status(self.database)["status"])
                    self.assertEqual("remote", server._asset_index_job(self.database, self.assets, "build")["status"])
                self.assertEqual([mock.call("status"), mock.call("ensure", depth="headers")], remote.call_args_list)
                context.assert_not_called()
        self.state.write_text("broken json", encoding="utf-8")
        with mock.patch.object(asset_index_client, "request", return_value={"job": None}) as remote:
            self.assertIsNone(server._asset_index_status(self.database))
        remote.assert_called_once_with("status")

    def test_same_process_request_no_longer_waits_for_a_second_thread_to_take_its_lock(self):
        selection = threading.RLock()
        blocked = []

        def context(**unused):
            with selection:
                return self.scope

        def remote_request(unused_operation):
            def endpoint():
                acquired = selection.acquire(timeout=0.025)
                blocked.append(not acquired)
                if acquired:
                    selection.release()
            worker = threading.Thread(target=endpoint)
            worker.start()
            worker.join(1)
            if blocked[-1]:
                raise TimeoutError("the HTTP worker is waiting for the caller's selection lock")
            return {"job": None}

        with mock.patch.object(server, "_active_asset_catalog_context", side_effect=context), \
                mock.patch.object(asset_index_jobs, "index_job_status", return_value=None), \
                mock.patch.object(asset_index_client, "request", side_effect=remote_request) as remote:
            with selection:
                # The previous HTTP route cannot reacquire the caller's RLock.
                with mock.patch.object(asset_index_client, "is_current_process_owner", return_value=False):
                    self.assertIsNone(server._asset_index_status(self.database))
                self.assertEqual([True], blocked)
                remote.reset_mock()
                # The current route stays on the owner thread and reenters it.
                self.assertIsNone(server._asset_index_status(self.database))
                remote.assert_not_called()


class ShaderPrepareTimingJournalTests(unittest.TestCase):
    def test_prepare_timing_context_is_accepted_by_the_real_journal(self):
        tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
        prepare = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "prepare_shader_workspace")
        calls = [node for node in ast.walk(prepare) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "emit_console_event"]
        call = next(node for node in calls if any(keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "shaderWorkspacePrepareTimings" for keyword in node.keywords))
        context = ast.literal_eval(next(keyword.value for keyword in call.keywords if keyword.arg == "context"))
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(console_events, "_default_journal_directory", return_value=Path(temporary)):
            self.assertTrue(console_events.emit_console_event(
                "shader", "info", "prepare_shader_workspace timing fixture", phase="prepare",
                code="shaderWorkspacePrepareTimings", context=context))
            events = list(Path(temporary).glob("events.*.jsonl"))
            self.assertEqual(1, len(events))
            record = json.loads(events[0].read_text(encoding="utf-8").strip())
            self.assertEqual("shaderWorkspacePrepareTimings", record["code"])


if __name__ == "__main__":
    unittest.main()
