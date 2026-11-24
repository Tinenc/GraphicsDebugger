"""CPU-only tests: synthetic handles and target-control objects, no native modules or processes."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import capture_api_smoke as smoke


DEMO = r"D:\Study\GitHub\renderdocUltra-eid-perf\util\test\demos_x64.exe"


class ProcessAPI:
    def __init__(self, pid=42, image=DEMO, created=150, waits=None):
        self.observed = (pid, image, created)
        self.waits = iter([False, True] if waits is None else waits)
        self.opened, self.closed, self.terminated = [], [], []
        self.identity_calls = 0
        self.times = iter([100, 200])

    def now(self):
        return next(self.times)

    def open(self, pid):
        self.opened.append(pid)
        return "retained-handle-for-original-process"

    def identity(self, handle):
        self.identity_calls += 1
        return self.observed

    def wait(self, handle, milliseconds):
        return next(self.waits)

    def terminate(self, handle):
        self.terminated.append(handle)

    def exit_code(self, handle):
        return 1

    def close(self, handle):
        self.closed.append(handle)


def fake_renderdoc():
    return SimpleNamespace(EnvironmentModification=lambda: SimpleNamespace(),
                           EnvMod=SimpleNamespace(Set="Set"), EnvSep=SimpleNamespace(NoSep="NoSep"))


class CaptureOwnershipTests(unittest.TestCase):
    def test_path_and_creation_time_verified_then_only_original_handle_terminated(self):
        api = ProcessAPI(image="\\\\?\\" + DEMO.upper())
        process = smoke.OwnedWindowsProcess(api, 42, DEMO, 100, 200)
        # Simulate the numeric PID now naming another image. Cleanup must not reopen that PID.
        api.observed = (42, r"C:\Unrelated\editor.exe", 300)
        result = process.cleanup()
        self.assertEqual(api.opened, [42])
        self.assertEqual(api.identity_calls, 1)
        self.assertEqual(api.terminated, ["retained-handle-for-original-process"])
        self.assertEqual(api.closed, api.terminated)
        self.assertTrue(result["verified"])
        self.assertTrue(result["exitVerified"])

    def test_mismatched_path_pid_or_creation_time_is_closed_without_termination(self):
        for overrides in ({"image": r"C:\Unrelated\demos_x64.exe"}, {"pid": 43}, {"created": 99}, {"created": 201}):
            api = ProcessAPI(**overrides)
            with self.assertRaises(RuntimeError):
                smoke.OwnedWindowsProcess(api, 42, DEMO, 100, 200)
            self.assertEqual(api.closed, ["retained-handle-for-original-process"])
            self.assertEqual(api.terminated, [])

    def test_already_exited_process_is_not_terminated(self):
        api = ProcessAPI(waits=[True, True])
        result = smoke.OwnedWindowsProcess(api, 42, DEMO, 100, 200).cleanup()
        self.assertEqual(result["cleanupAction"], "alreadyExited")
        self.assertEqual(api.terminated, [])
        self.assertEqual(len(api.closed), 1)

    def test_exit_racing_termination_is_accepted_only_with_same_handle_signalled(self):
        api = ProcessAPI(waits=[False, True, True])
        with patch.object(api, "terminate", side_effect=OSError("already exiting")):
            result = smoke.OwnedWindowsProcess(api, 42, DEMO, 100, 200).cleanup()
        self.assertTrue(result["exitVerified"])
        self.assertEqual(api.opened, [42])

    def test_cleanup_failure_still_closes_handle(self):
        api = ProcessAPI(waits=[False, False])
        process = smoke.OwnedWindowsProcess(api, 42, DEMO, 100, 200)
        with self.assertRaisesRegex(RuntimeError, "did not exit"):
            process.cleanup()
        self.assertEqual(len(api.closed), 1)
        with self.assertRaisesRegex(RuntimeError, "already closed"):
            process.cleanup()

    def test_fixture_flag_requires_exact_api_and_demo(self):
        smoke.validate_live_forward_fixture(True, ["D3D12"], "D3D12_RenderTarget_Binds")
        for apis, demo in ((["Vulkan"], "D3D12_RenderTarget_Binds"),
                           (["D3D12", "Vulkan"], "D3D12_RenderTarget_Binds"),
                           (["D3D12"], "D3D12_Simple_Triangle")):
            with self.assertRaises(ValueError):
                smoke.validate_live_forward_fixture(True, apis, demo)

    def test_fixture_environment_is_explicit_in_enabled_and_normal_runs(self):
        rd = fake_renderdoc()
        for enabled, expected in ((True, "1"), (False, "0")):
            change = smoke.fixture_environment(rd, enabled)
            self.assertEqual(change.name, "RENDERDOC_LIVE_FORWARD_FIXTURE")
            self.assertEqual(change.value, expected)
            self.assertEqual(change.mod, rd.EnvMod.Set)

    def test_clear_fixture_requires_exact_api_and_demo(self):
        smoke.validate_clear_fixture(True, ["D3D12"], "D3D12_Checkpoint_Triangle")
        for apis, demo in ((["Vulkan"], "D3D12_Checkpoint_Triangle"),
                           (["D3D12", "D3D11"], "D3D12_Checkpoint_Triangle"),
                           (["D3D12"], "D3D12_Simple_Triangle")):
            with self.assertRaises(ValueError):
                smoke.validate_clear_fixture(True, apis, demo)
            smoke.validate_clear_fixture(False, apis, demo)

    def test_alias_variants_require_exact_demo_and_exclude_old_clear_mode(self):
        for mode in ("clear", "read", "split"):
            smoke.validate_alias_fixture(mode, ["D3D12"], "D3D12_Checkpoint_Triangle")
            for apis, demo in ((["Vulkan"], "D3D12_Checkpoint_Triangle"),
                               (["D3D12", "D3D11"], "D3D12_Checkpoint_Triangle"),
                               (["D3D12"], "D3D12_Simple_Triangle")):
                with self.assertRaises(ValueError):
                    smoke.validate_alias_fixture(mode, apis, demo)
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                smoke.validate_alias_fixture(mode, ["D3D12"], "D3D12_Checkpoint_Triangle", True)
        smoke.validate_alias_fixture("off", ["Vulkan"], "VK_Simple_Triangle")
        with self.assertRaisesRegex(ValueError, "Unknown"):
            smoke.validate_alias_fixture("unexpected", ["D3D12"], "D3D12_Checkpoint_Triangle")

    def test_alias_environment_selects_variant_and_clears_inherited_variant(self):
        rd = fake_renderdoc()
        for mode in ("off", "clear", "read", "split"):
            change = smoke.alias_fixture_environment(rd, mode)
            self.assertEqual(change.name, "RENDERDOC_CHECKPOINT_ALIAS_FIXTURE")
            self.assertEqual(change.value, mode)
            self.assertEqual(change.mod, rd.EnvMod.Set)
            self.assertEqual(change.sep, rd.EnvSep.NoSep)

    @unittest.skipUnless(smoke.os.name == "nt", "Windows capture orchestration branch")
    def test_capture_error_and_control_shutdown_error_still_clean_owned_handle(self):
        from unittest.mock import MagicMock
        rd = fake_renderdoc()
        rd.ResultCode = SimpleNamespace(Succeeded="Succeeded")
        rd.CaptureOptions = lambda: None
        rd.ExecuteAndInject = MagicMock(return_value=SimpleNamespace(result="Succeeded", ident=12))
        control = MagicMock()
        control.GetPID.return_value = 42
        control.QueueCapture.side_effect = RuntimeError("stop before any capture")
        control.Shutdown.side_effect = RuntimeError("control failed")
        rd.CreateTargetControl = lambda *args: control
        for demo, live, alias in (("D3D12_RenderTarget_Binds", True, "off"),
                                  ("D3D12_Checkpoint_Triangle", False, "clear"),
                                  ("D3D12_Checkpoint_Triangle", False, "read"),
                                  ("D3D12_Checkpoint_Triangle", False, "split")):
            with self.subTest(demo=demo, alias=alias):
                api = ProcessAPI()
                with patch.object(smoke, "renderdoc_module", return_value=rd), \
                        patch.object(smoke, "WindowsProcessAPI", return_value=api), \
                        patch.object(smoke.os, "kill") as pid_kill:
                    result = smoke.capture_one("D3D12", Path(DEMO), Path("output"), 1, 5, "", "", "",
                                               d3d12_demo=demo, live_forward_fixture=live,
                                               checkpoint_alias_fixture=alias)
                self.assertFalse(result["ok"])
                self.assertTrue(result["ownedProcess"]["exitVerified"])
                self.assertEqual(result["childEnvironment"]["RENDERDOC_LIVE_FORWARD_FIXTURE"],
                                 "1" if live else "0")
                self.assertEqual(result["checkpointAliasFixture"], alias)
                self.assertEqual(result["childEnvironment"]["RENDERDOC_CHECKPOINT_ALIAS_FIXTURE"], alias)
                sent = {entry.name: entry.value for entry in rd.ExecuteAndInject.call_args.args[3]}
                self.assertEqual(sent["RENDERDOC_CHECKPOINT_ALIAS_FIXTURE"], alias)
                self.assertEqual(api.terminated, ["retained-handle-for-original-process"])
                pid_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
