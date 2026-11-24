"""CPU-only boundary tests; no GUI, capture or process is started."""
import os
from pathlib import Path
import tempfile
import unittest

from run_eid_isolated_gui import contained_path, private_environment


class IsolatedGuiTests(unittest.TestCase):
    def test_inherited_services_projects_and_python_are_not_reused(self):
        inherited = {"PATH": "system", "SystemRoot": "installed-windows",
                     "REPLAYLAB_RUNTIME_DIR": "foreign-service",
                     "REPLAYLAB_INTERNAL_PORT": "49743",
                     "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE": "foreign-project",
                     "RENDERDOC_D3D12_CHECKPOINT_FAIL_AFTER_INITIAL": "1",
                     "PYTHONPATH": "foreign-python", "QT_PLUGIN_PATH": "foreign-qt",
                     "TMPDIR": "foreign-temporary"}
        original = dict(inherited)
        root = Path(os.path.abspath("isolated-tree"))
        run = root / "output" / "gui-isolation" / "case"
        env = private_environment(inherited, root, run, run / "script-config.json")
        for name in ("REPLAYLAB_RUNTIME_DIR", "REPLAYLAB_INTERNAL_PORT",
                     "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE", "PYTHONPATH",
                     "RENDERDOC_D3D12_CHECKPOINT_FAIL_AFTER_INITIAL"):
            self.assertNotIn(name, env)
        self.assertEqual(inherited, original)
        self.assertEqual(env["SystemRoot"], "installed-windows")
        self.assertEqual(env["REPLAYLAB_EXTERNAL_MCP_AUTOSTART"], "0")
        self.assertEqual(Path(env["QT_PLUGIN_PATH"]), root / "x64" / "Development" / "qtplugins")
        for name in ("RENDERDOC_CONFIG_DIR", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR"):
            self.assertIn(run, Path(env[name]).parents)

    def test_case_insensitive_windows_inheritance(self):
        env = private_environment({"replaylab_mcp_port": "123", "pythonhome": "bad"},
                                  Path("root"), Path("root/run"), Path("root/config.json"))
        self.assertNotIn("replaylab_mcp_port", env)
        self.assertNotIn("pythonhome", env)

    def test_path_escape_and_root_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "worktree"
            root.mkdir()
            for target in (root, root / ".." / "outside", Path(str(root) + "-other") / "file"):
                with self.assertRaises(ValueError):
                    contained_path(root, target)
            self.assertEqual(contained_path(root, root / "output" / "run"), root / "output" / "run")

    def test_existing_symlink_ancestor_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "worktree"
            root.mkdir()
            target = Path(directory) / "foreign"
            target.mkdir()
            link = root / "output"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("This account cannot create symlinks")
            with self.assertRaises(ValueError):
                contained_path(root, link / "new-file")


if __name__ == "__main__":
    unittest.main()
