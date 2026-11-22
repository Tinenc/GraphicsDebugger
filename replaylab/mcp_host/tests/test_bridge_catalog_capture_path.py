"""Equivalent native capture paths retain existing catalog/workspace identity."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

import pytest

from src.active_projects import ActiveReplayProjectService
from src.shader_workspace.catalog import ShaderKey, capture_identity
from tests.test_bridge_catalog_cpu_finalize import _CatalogRig


@pytest.mark.parametrize("spelling", ["native", "forward-slashes", "dot-segments"])
def test_real_catalog_export_normalizes_path_before_identity_and_workspace_lookup(tmp_path, monkeypatch, spelling):
    rig = _CatalogRig(tmp_path, monkeypatch)
    try:
        native_path = os.path.abspath(os.path.normpath(rig.capture_path))
        first = rig.module._export_shader_catalog(rig.params(), defer_publication=True)
        first_bytes = Path(first["catalogPath"]).read_bytes()
        original_catalog = json.loads(first_bytes)
        assert original_catalog["capturePath"] == native_path

        if spelling == "forward-slashes":
            rig.capture_path = native_path.replace("\\", "/")
        elif spelling == "dot-segments":
            rig.capture_path = os.path.join(os.path.dirname(native_path), "unused", "..", "capture.rdc")
        else:
            rig.capture_path = native_path
        second = rig.module._export_shader_catalog(rig.params(), defer_publication=True)
        second_bytes = Path(second["catalogPath"]).read_bytes()
        catalog = json.loads(second_bytes)
        assert catalog["capturePath"] == native_path
        assert catalog["capture"]["path"] == native_path
        assert first_bytes == second_bytes
        assert first["captureId"] == second["captureId"]

        # The unchanged Host algorithm still computes the historical ID from
        # native path text, rather than silently migrating the identity scheme.
        keys = [ShaderKey.from_value(item) for item in catalog["shaders"]]
        assert capture_identity(original_catalog, keys) == second["captureId"]
        # Managed shader paths intentionally have a Windows length limit. Keep
        # this synthetic project short independently of pytest's output path.
        with tempfile.TemporaryDirectory(prefix="cp") as temporary:
            project_root = Path(temporary)
            project = ActiveReplayProjectService(project_root / "a.json", project_root / "p")
            project.ensure_pending("P")
            paths = [project.shader_workspace_path(
                capture_id=result["captureId"], catalog_sha256=hashlib.sha256(content).hexdigest(),
                route_binding_sha256="a" * 64, stage=keys[0].stage,
                shader_resource_id=keys[0].resource_id, raw_sha256=keys[0].raw_sha256,
                create=True,
            ) for result, content in ((first, first_bytes), (second, second_bytes))]
            assert paths[0] == paths[1]
            marker = paths[0] / "already-compiled.bin"
            marker.write_bytes(b"existing compiled candidate")
            assert (paths[1] / marker.name).read_bytes() == b"existing compiled candidate"
        rig.assert_unpublished()
    finally:
        rig.close()


def test_distinct_capture_paths_keep_distinct_catalog_ids(tmp_path, monkeypatch):
    rig = _CatalogRig(tmp_path, monkeypatch)
    try:
        first = rig.collect()["catalog"]
        rig.capture_path = str(tmp_path / "other.rdc")
        second = rig.collect()["catalog"]
        assert first["capturePath"] != second["capturePath"]
        assert rig.module._catalog_capture_id(first) != rig.module._catalog_capture_id(second)
    finally:
        rig.close()
