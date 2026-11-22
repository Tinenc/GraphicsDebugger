import hashlib
import json
import os
from pathlib import Path
from unittest import mock
import zipfile

import pytest

import build_fs
import restore_dependencies as restore


def test_long_unicode_tree_restores_twice_and_copies_without_changes(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    name = "/".join(["中文-path-" + "a" * 30] * 9) + "/payload.txt"
    content = b"sealed long-path payload"
    archive = inputs / "long.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(name, content)
    record = {"id": "long-tree", "path": archive.name, "size": archive.stat().st_size,
              "sha256": restore.sha(archive), "files": [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}
    target = restore.restore(inputs, tmp_path / "expanded", record)
    physical = build_fs.long_path(target / name)
    assert len(str(physical)) > 400
    assert physical.read_bytes() == content
    timestamp = physical.stat().st_mtime_ns
    assert restore.restore(inputs, tmp_path / "expanded", record) == target
    assert physical.stat().st_mtime_ns == timestamp
    copied = tmp_path / "copy 中文"
    build_fs.copy_directory(target, copied)
    assert build_fs.long_path(copied / name).read_bytes() == content


def test_enumeration_failure_is_not_treated_as_damaged_content(tmp_path, monkeypatch):
    target = tmp_path / "generation"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("preserve")
    def denied(root, *, onerror, followlinks):
        onerror(PermissionError("test enumeration denied"))
        return iter(())
    monkeypatch.setattr(build_fs.os, "walk", denied)
    with pytest.raises(OSError, match="existing data was preserved"):
        restore.verify_tree(target, [])
    assert marker.read_text() == "preserve"


def test_temporary_directory_cleans_deep_unicode_files(tmp_path):
    with build_fs.temporary_directory(prefix="bread-long-cleanup-", dir=tmp_path) as work:
        root = Path(work)
        file = root.joinpath(*(["x" * 50] * 7), "中文.txt")
        file.parent.mkdir(parents=True)
        file.write_text("test")
    assert not root.exists()


def test_cleanup_failure_does_not_replace_completed_package_result(tmp_path, capsys):
    real_remove = build_fs.shutil.rmtree
    with mock.patch.object(build_fs.shutil, "rmtree", side_effect=PermissionError("test locked file")), mock.patch.object(build_fs.time, "sleep"):
        with build_fs.temporary_directory(prefix="bread-locked-cleanup-", dir=tmp_path) as work:
            result = "package-verified"
    assert result == "package-verified"
    assert "Warning:" in capsys.readouterr().err
    real_remove(Path(work))


def test_cleanup_failure_does_not_hide_original_build_failure(tmp_path):
    real_remove = build_fs.shutil.rmtree
    with mock.patch.object(build_fs.shutil, "rmtree", side_effect=PermissionError("test locked file")), mock.patch.object(build_fs.time, "sleep"):
        with pytest.raises(RuntimeError, match="real build failure"):
            with build_fs.temporary_directory(prefix="bread-failed-cleanup-", dir=tmp_path) as work:
                raise RuntimeError("real build failure")
    real_remove(Path(work))


def test_pinned_dotnet_sdk_and_offline_runtime_are_consistent():
    root = Path(__file__).resolve().parents[1]
    sdk = json.loads((root / "dotnet-sdk.global.json").read_text())
    assert sdk["sdk"] == {"version": "10.0.302", "rollForward": "disable", "allowPrerelease": False}
    script = (root / "Build-RenderDocBread.ps1").read_text(encoding="utf-8")
    assert script.index("$pinnedSdk") < script.index("Restore-BuildEnvironment.ps1")
    assert "-p:RuntimeFrameworkVersion=10.0.10" in script
