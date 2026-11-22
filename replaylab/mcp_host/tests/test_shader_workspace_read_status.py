"""File/outline reads reuse one validated status only within that request."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from src import server
from src.shader_workspace.errors import ToolConfigurationError


@pytest.fixture
def workspace_status(tmp_path, monkeypatch):
    source = tmp_path / "shader.hlsl"
    source.write_text("float4 MainPS():SV_Target{return 1;}\n", encoding="utf-8")
    current = {
        "workspacePath": str(tmp_path), "revision": 7, "entryPoint": "MainPS",
        "source": {"path": str(source), "present": True,
                   "currentSha256": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    calls = []

    def status(path):
        calls.append((path, current["revision"]))
        return copy.deepcopy(current)

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(status=status))
    return tmp_path, source, current, calls


@pytest.mark.parametrize("mode", ["source", "outline-default", "outline-explicit", "outline-lines"])
def test_one_full_status_for_each_read_request(workspace_status, mode):
    root, source, current, calls = workspace_status
    if mode == "source":
        result = json.loads(server.read_shader_workspace_file(str(root), source.name))
        assert result["text"] == source.read_bytes().decode("utf-8")
    else:
        options = {} if mode == "outline-default" else {"relative_path": source.name}
        if mode == "outline-lines":
            options["lines"] = "1-1"
        result = json.loads(server.get_shader_workspace_outline(str(root), **options))
        assert result["sourceSha256"] == current["source"]["currentSha256"]
        if mode != "outline-lines":
            assert result["entry"]["name"] == "MainPS"
    assert "error" not in result
    assert result["applied"] is False
    assert calls == [(str(root), 7)]


def test_next_outline_request_observes_new_source_entry_and_revision(workspace_status):
    root, source, current, calls = workspace_status
    first = json.loads(server.get_shader_workspace_outline(str(root)))
    replacement = root / "reorganized.hlsl"
    replacement.write_text("float4 FinalPS():SV_Target{return 0.5;}\n", encoding="utf-8")
    current.update(revision=8, entryPoint="FinalPS")
    current["source"].update(path=str(replacement),
                             currentSha256=hashlib.sha256(replacement.read_bytes()).hexdigest())

    second = json.loads(server.get_shader_workspace_outline(str(root)))
    assert first["relativePath"] == source.name
    assert first["entry"]["name"] == "MainPS"
    assert second["relativePath"] == replacement.name
    assert second["entry"]["name"] == "FinalPS"
    assert second["sourceSha256"] == current["source"]["currentSha256"]
    assert second["sourceSha256"] != first["sourceSha256"]
    assert calls == [(str(root), 7), (str(root), 8)]


def test_next_file_request_reads_updated_bytes_and_revalidates(workspace_status):
    root, source, current, calls = workspace_status
    first = json.loads(server.read_shader_workspace_file(str(root), source.name))
    source.write_text("float4 MainPS():SV_Target{return 0.25;}\n", encoding="utf-8")
    current["revision"] = 8
    current["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()

    second = json.loads(server.read_shader_workspace_file(str(root), source.name))
    assert second["text"] == source.read_bytes().decode("utf-8")
    assert second["sha256"] == current["source"]["currentSha256"]
    assert second["sha256"] != first["sha256"]
    assert calls == [(str(root), 7), (str(root), 8)]


@pytest.mark.parametrize("tool", [server.read_shader_workspace_file, server.get_shader_workspace_outline])
def test_read_keeps_workspace_status_validation_failures(tmp_path, monkeypatch, tool):
    source = tmp_path / "shader.hlsl"
    source.write_text("shader source", encoding="utf-8")
    calls = []

    def failed_status(path):
        calls.append(path)
        raise ToolConfigurationError("compiler executable digest changed")

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(status=failed_status))
    result = json.loads(tool(str(tmp_path), relative_path=source.name))
    assert "compiler executable digest changed" in result["error"]
    assert "text" not in result
    assert calls == [str(tmp_path)]


def test_root_only_helper_still_validates_each_call(workspace_status):
    root, _source, current, calls = workspace_status
    assert server._workspace_root_for_files(str(root)) == root
    current["revision"] = 8
    assert server._workspace_root_for_files(str(root)) == root
    assert calls == [(str(root), 7), (str(root), 8)]
