"""Persistent live-edit authorizations are reclaimed automatically at the cap."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest import mock

import pytest

from src.shader_live_edit import (
    ShaderLiveEditError,
    _PersistentLiveEditRegistry,
)


def _identity(capture: Path, *, raw: str = "a" * 64, resource: int = 755) -> dict:
    return {
        "workspaceId": uuid.uuid4().hex,
        "captureId": "c" * 64,
        "capturePath": str(capture),
        "shaderKey": "fragment:%d" % resource,
        "shaderRawSha256": raw,
        "resourceId": resource,
        "stage": "fragment",
        "entryPoint": "MainPS",
    }


class ReclaimTests(unittest.TestCase):
    def setUp(self) -> None:
        # Exercise durable-cap reclamation with a small fixture. The actual
        # watcher-thread limit is independent of retained authorization count.
        self.capacity = 8
        patcher = mock.patch("src.shader_live_edit.MAX_PERSISTENT_LIVE_EDIT_AUTHORIZATIONS", self.capacity)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = _PersistentLiveEditRegistry(self.root / "registry.json")
        self.capture = self.root / "6.rdc"
        self.capture.write_bytes(b"rdc")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _workspace(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        return path

    def _fill(self, **kwargs) -> list[Path]:
        workspaces = []
        for index in range(self.capacity):
            workspace = self._workspace("ws-%d" % index)
            self.registry.authorize(workspace, _identity(self.capture, raw=("%02x" % index) * 32, resource=100 + index), 100)
            workspaces.append(workspace)
            time.sleep(0.002)
        return workspaces

    def test_limit_without_reclaim_still_raises(self) -> None:
        self._fill()
        with self.assertRaisesRegex(ShaderLiveEditError, "limit reached"):
            self.registry.authorize(self._workspace("ws-new"), _identity(self.capture), 100)

    def test_dead_workspace_is_released_first(self) -> None:
        workspaces = self._fill()
        import shutil
        shutil.rmtree(workspaces[3])
        released = self.registry.reclaim_for(_identity(self.capture))
        self.assertEqual([item["reclaimReason"] for item in released], ["workspace-missing"])
        self.assertEqual(released[0]["workspacePath"], str(workspaces[3]))
        self.registry.authorize(self._workspace("ws-new"), _identity(self.capture), 100)
        self.assertEqual(len(self.registry.entries()), self.capacity)

    def test_same_physical_shader_is_superseded(self) -> None:
        workspaces = self._fill()
        # ws-2 holds the same physical shader as the newcomer.
        same = _identity(self.capture, raw="02" * 32, resource=102)
        released = self.registry.reclaim_for(same)
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["workspacePath"], str(workspaces[2]))
        self.assertEqual(released[0]["reclaimReason"], "superseded-same-shader")

    def test_protected_workspaces_are_never_released(self) -> None:
        workspaces = self._fill()
        same = _identity(self.capture, raw="02" * 32, resource=102)
        import os
        protected = {os.path.normcase(os.path.normpath(str(workspaces[2])))}
        released = self.registry.reclaim_for(same, protected_keys=protected)
        # falls through to "other capture" rule, which finds nothing here
        self.assertEqual(released, [])

    def test_other_capture_least_recent_is_released_last(self) -> None:
        self._fill()
        other_capture = self.root / "7.rdc"
        other_capture.write_bytes(b"rdc")
        released = self.registry.reclaim_for(_identity(other_capture, raw="f" * 64, resource=999))
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["reclaimReason"], "least-recent-other-capture")
        self.assertEqual(released[0]["workspacePath"], str(self.root / "ws-0"))

    def test_below_limit_is_a_no_op(self) -> None:
        self.registry.authorize(self._workspace("ws-0"), _identity(self.capture), 100)
        self.assertEqual(self.registry.reclaim_for(_identity(self.capture)), [])


def test_durable_registry_byte_budget_rejects_new_record_without_corrupting_previous_consent(tmp_path, monkeypatch):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"capture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = _PersistentLiveEditRegistry(tmp_path / "registry.json")
    original_entry = registry.authorize(workspace, _identity(capture), 100)
    original_bytes = registry.path.read_bytes()
    monkeypatch.setattr("src.shader_live_edit.MAX_LIVE_EDIT_REGISTRY_BYTES", len(original_bytes) + 50)
    another = tmp_path / "another-workspace"
    another.mkdir()
    with pytest.raises(ShaderLiveEditError, match="byte budget"):
        registry.authorize(another, _identity(capture, resource=99), 100)
    assert registry.path.read_bytes() == original_bytes
    assert registry.entries() == [original_entry]


if __name__ == "__main__":
    unittest.main()
