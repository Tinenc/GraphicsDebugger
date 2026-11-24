"""Interrupted owned-file work survives cleanup without retaining launch config."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from replaylab_agent.agent_context import ProviderContext
from replaylab_agent import shader_draft_recovery as recovery


def _turn(tmp_path):
    root = tmp_path / ("native-" + "a" * 32)
    workspace = root / "agent-workspace"
    workspace.mkdir(parents=True)
    return root, workspace


def test_cleanup_recovers_complete_edited_source_and_deletes_only_temporary_config(tmp_path):
    root, workspace = _turn(tmp_path)
    source = "// 尚未导入的修改😀\r\nfloat4 MainPS() : SV_Target { return 0.5; }\n".encode("utf-8")
    (workspace / "proposal.hlsl").write_bytes(source)
    (workspace / "settings.json").write_text('{"token":"private-config"}')
    (workspace / "AGENTS.md").write_text("launch instruction")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "trace.txt").write_text("private trace")
    (workspace / ".codex" / "private.hlsl").write_text("hidden source")
    report = ProviderContext.cleanup_turn_workspace(root)
    assert not root.exists()
    assert report["status"] == "recovered" and report["sourceRetained"] is False
    assert report["workingCopy"] is True and report["replayAuthority"] is False
    copied = report["files"]
    assert len(copied) == 1 and Path(copied[0]["path"]).read_bytes() == source
    assert copied[0]["sha256"] == hashlib.sha256(source).hexdigest()
    saved = json.loads(Path(report["manifestPath"]).read_text(encoding="utf-8"))
    assert saved == report
    latest = json.loads((tmp_path / "recovered-shader-drafts" / "latest.json").read_text())
    assert latest["manifestPath"] == report["manifestPath"]
    assert sorted(path.name for path in Path(report["recoveryDirectory"]).rglob("*") if path.is_file()) == ["manifest.json", "proposal.hlsl"]
    assert "private-config" not in Path(report["manifestPath"]).read_text()


def test_cleanup_without_shader_drafts_remains_ephemeral(tmp_path):
    root, workspace = _turn(tmp_path)
    (workspace / "config.toml").write_text("temporary config")
    assert ProviderContext.cleanup_turn_workspace(root) is None
    assert not root.exists() and not (tmp_path / "recovered-shader-drafts").exists()


@pytest.mark.parametrize("suffix", [".glsl", ".wgsl", ".metal", ".slang", ".shader", ".hlsli"])
def test_supported_shader_drafts_are_recoverable(tmp_path, suffix):
    root, workspace = _turn(tmp_path)
    (workspace / ("proposal" + suffix)).write_bytes(b"// work in progress\n")
    report = ProviderContext.cleanup_turn_workspace(root)
    assert report["status"] == "recovered" and Path(report["files"][0]["path"]).is_file()


@pytest.mark.parametrize("failure", ["file-size", "file-count", "total-size", "archive-size", "binary", "write-error", "scan-limit"])
def test_incomplete_recovery_keeps_original_directory_and_diagnostic(tmp_path, monkeypatch, failure):
    root, workspace = _turn(tmp_path)
    path = workspace / "proposal.hlsl"
    original = b"// unfinished edited source\n"
    path.write_bytes(original)
    if failure == "file-size": monkeypatch.setattr(recovery, "MAX_FILE_BYTES", 2)
    if failure == "file-count": monkeypatch.setattr(recovery, "MAX_RECOVERY_FILES", 0)
    if failure == "total-size": monkeypatch.setattr(recovery, "MAX_RECOVERY_BYTES", 2)
    if failure == "archive-size":
        archive = tmp_path / "recovered-shader-drafts"
        archive.mkdir()
        (archive / "prior.hlsl").write_bytes(b"prior")
        monkeypatch.setattr(recovery, "MAX_ARCHIVE_BYTES", 6)
    if failure == "binary":
        original = b"unreadable\0source"
        path.write_bytes(original)
    if failure == "write-error":
        def failed_write(*args): raise PermissionError("synthetic recovery disk failure")
        monkeypatch.setattr(recovery, "_write_json", failed_write)
    if failure == "scan-limit": monkeypatch.setattr(recovery, "MAX_SCAN_ENTRIES", 0)
    report = ProviderContext.cleanup_turn_workspace(root)
    assert report["status"] == "source-retained" and report["sourceRetained"] is True
    assert path.read_bytes() == original and report["reason"]
    if report["manifestPath"]:
        assert json.loads(Path(report["manifestPath"]).read_text())["sourceRetained"] is True


def test_recovery_does_not_copy_hardlinks_or_escape_through_symlinks(tmp_path):
    root, workspace = _turn(tmp_path)
    outside = tmp_path / "outside.hlsl"
    outside.write_bytes(b"outside content")
    os.link(outside, workspace / "hardlink.hlsl")
    (workspace / "actual.hlsl").write_bytes(b"actual draft")
    try:
        (workspace / "symlink.hlsl").symlink_to(outside)
    except OSError:
        pass
    report = ProviderContext.cleanup_turn_workspace(root)
    assert [Path(item["path"]).name for item in report["files"]] == ["actual.hlsl"]
    assert outside.read_bytes() == b"outside content"


def test_non_owned_directory_is_never_deleted(tmp_path):
    workspace = tmp_path / "user-project"
    workspace.mkdir()
    (workspace / "shader.hlsl").write_bytes(b"user source")
    report = ProviderContext.cleanup_turn_workspace(workspace)
    assert report["sourceRetained"] is True and (workspace / "shader.hlsl").read_bytes() == b"user source"


def test_new_draft_during_recovery_keeps_original_work(tmp_path, monkeypatch):
    root, workspace = _turn(tmp_path)
    (workspace / "first.hlsl").write_bytes(b"first draft")
    read = recovery._read_draft
    def concurrent_edit(path, expected):
        raw = read(path, expected)
        (workspace / "new.hlsl").write_bytes(b"late new draft")
        return raw
    monkeypatch.setattr(recovery, "_read_draft", concurrent_edit)
    report = ProviderContext.cleanup_turn_workspace(root)
    assert report["sourceRetained"] is True and "changed" in report["reason"]
    assert (workspace / "new.hlsl").read_bytes() == b"late new draft"


def test_busy_cleanup_keeps_successful_recovery_locator(tmp_path, monkeypatch):
    root, workspace = _turn(tmp_path)
    (workspace / "proposal.hlsl").write_bytes(b"recoverable draft")
    def busy(_path): raise PermissionError("synthetic provider directory lock")
    monkeypatch.setattr("replaylab_agent.agent_context.shutil.rmtree", busy)
    report = ProviderContext.cleanup_turn_workspace(root)
    assert report["sourceRetained"] is True and root.exists()
    assert Path(report["manifestPath"]).is_file()
    assert Path(report["files"][0]["path"]).read_bytes() == b"recoverable draft"


def test_redirected_archive_never_copies_source_outside_its_root(tmp_path):
    root, workspace = _turn(tmp_path)
    source = workspace / "proposal.hlsl"
    source.write_bytes(b"draft")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (tmp_path / "recovered-shader-drafts").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not grant symlink creation")
    report = ProviderContext.cleanup_turn_workspace(root)
    assert report["sourceRetained"] is True and source.exists()
    assert not list(elsewhere.iterdir())
