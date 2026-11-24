"""Delivered ordinary files survive turn cleanup at their original paths."""
import os
from pathlib import Path

import pytest

from replaylab_agent.agent_context import ProviderContext
from replaylab_agent import shader_draft_recovery as recovery


def _turn(tmp_path, workspace_name="agent-workspace"):
    root = tmp_path / ("native-" + "b" * 32)
    workspace = root / workspace_name
    workspace.mkdir(parents=True)
    return root, workspace


@pytest.mark.parametrize("workspace_name", ["agent-workspace", "cursor-workspace"])
def test_completed_report_outputs_keep_original_paths_without_copying_config(tmp_path, workspace_name):
    root, workspace = _turn(tmp_path, workspace_name)
    outputs = {"report.md": b"# Delivered report\n", "data.csv": b"name,value\na,1\n",
               "plot.png": b"\x89PNG\x00binary output", "notes": b"extensionless user output"}
    for name, raw in outputs.items():
        (workspace / name).write_bytes(raw)
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text('{"token":"private bootstrap"}')
    (root / ("mcp-servers-" + "c" * 32 + ".json")).write_text("private credential")
    (root / ("live-edit-registry-" + "d" * 32 + ".json")).write_text("temporary state")
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["schema"] == recovery.OUTPUT_RETENTION_SCHEMA
    assert receipt["sourceRetained"] is True and receipt["sourceDirectory"] == str(root)
    assert receipt["workspaceDirectories"] == [str(workspace)]
    assert receipt["outputCount"] == len(outputs) and receipt["truncated"] is False
    assert {Path(item["path"]).name for item in receipt["files"]} == set(outputs)
    for name, raw in outputs.items():
        assert (workspace / name).read_bytes() == raw
    assert not (tmp_path / "recovered-shader-drafts").exists()
    assert "private" not in str(receipt) and receipt["manifestPath"] == ""


def test_bootstrap_only_turn_still_cleans_up(tmp_path):
    root, workspace = _turn(tmp_path, "cursor-workspace")
    (workspace / ".cursor").mkdir()
    (workspace / ".cursor" / "cli.json").write_text("provider policy")
    (workspace / ".mcp.json").write_text("launch config")
    for name in ("config.toml", "settings.json", "AGENTS.md"):
        (workspace / name).write_text("launch only")
    (root / ("mcp-servers-" + "c" * 32 + ".json")).write_text("private credential")
    (root / ("live-edit-registry-" + "d" * 32 + ".json")).write_text("temporary state")
    assert ProviderContext.cleanup_turn_workspace(root) is None
    assert not root.exists()


def test_mixed_shader_and_general_output_retains_both_original_paths(tmp_path):
    root, workspace = _turn(tmp_path)
    (workspace / "shader.hlsl").write_bytes(b"edited shader")
    (workspace / "report.md").write_bytes(b"the report references shader.hlsl")
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["schema"] == recovery.OUTPUT_RETENTION_SCHEMA
    assert {Path(row["path"]).name for row in receipt["files"]} == {"shader.hlsl", "report.md"}
    assert (workspace / "shader.hlsl").read_bytes() == b"edited shader"


def test_nested_user_config_is_output_not_managed_bootstrap(tmp_path):
    root, workspace = _turn(tmp_path)
    (workspace / "example-project").mkdir()
    output = workspace / "example-project" / "settings.json"
    output.write_bytes(b'{"user-setting":true}')
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["files"][0]["path"] == str(output) and output.is_file()


def test_general_output_is_not_limited_to_shader_copy_size_or_read_as_text(tmp_path, monkeypatch):
    root, workspace = _turn(tmp_path)
    output = workspace / "image.png"
    output.write_bytes(b"binary\0content")
    monkeypatch.setattr(recovery, "MAX_FILE_BYTES", 1)
    def no_copy(*args):
        raise AssertionError("General retention must not read or copy file contents")
    monkeypatch.setattr(recovery, "_read_draft", no_copy)
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["files"][0]["sizeBytes"] == 14 and output.exists()


def test_retained_listing_is_bounded_without_deleting_unlisted_outputs(tmp_path, monkeypatch):
    root, workspace = _turn(tmp_path)
    for index in range(3):
        (workspace / f"report-{index}.txt").write_text("delivered")
    monkeypatch.setattr(recovery, "MAX_RECOVERY_FILES", 1)
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["outputCount"] == 3 and len(receipt["files"]) == 1 and receipt["truncated"] is True
    assert len(list(workspace.iterdir())) == 3


def test_scan_limit_preserves_uninspected_work(tmp_path, monkeypatch):
    root, workspace = _turn(tmp_path)
    (workspace / "report.md").write_text("delivered")
    monkeypatch.setattr(recovery, "MAX_SCAN_ENTRIES", 0)
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["sourceRetained"] is True and receipt["truncated"] is True
    assert "inspection incomplete" in receipt["reason"].lower()
    assert (workspace / "report.md").is_file()


def test_links_never_become_output_receipts_or_scan_external_directories(tmp_path):
    root, workspace = _turn(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "secret.txt"
    external.write_bytes(b"outside original")
    os.link(external, workspace / "hardlink.txt")
    try:
        (workspace / "external").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    (workspace / "actual.md").write_text("real output")
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert [Path(row["path"]).name for row in receipt["files"]] == ["actual.md"]
    assert external.read_bytes() == b"outside original"


def test_general_output_created_during_shader_recovery_is_not_deleted(tmp_path, monkeypatch):
    root, workspace = _turn(tmp_path)
    (workspace / "proposal.hlsl").write_bytes(b"shader source")
    read_draft = recovery._read_draft
    def late_report(path, expected):
        raw = read_draft(path, expected)
        (workspace / "report.md").write_text("late delivered report")
        return raw
    monkeypatch.setattr(recovery, "_read_draft", late_report)
    receipt = ProviderContext.cleanup_turn_workspace(root)
    assert receipt["schema"] == recovery.OUTPUT_RETENTION_SCHEMA
    assert (workspace / "report.md").read_text() == "late delivered report"
    assert receipt["shaderRecovery"]["status"] == "recovered"
