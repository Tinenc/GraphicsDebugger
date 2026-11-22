from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from unittest import mock

import pytest

from test_shader_console import console


FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/qrenderdoc_shader_workspace_scope_smoke.py"


def _pure_memory_fixture():
    tree = ast.parse(FIXTURE.read_text(encoding="utf-8"), feature_version=(3, 6))
    nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)) or
             isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {
                 "_sha", "_text_evidence", "_MemoryHost", "_MemoryStatProxy"} or
             isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SOURCE"
                                                 for target in node.targets)]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(FIXTURE), "exec"), namespace)
    return namespace


def test_ui_memory_host_never_writes_source_and_returns_normalized_client_metadata(tmp_path):
    fixture = _pure_memory_fixture()
    root = tmp_path / "nonexistent-synthetic-workspace"
    host = fixture["_MemoryHost"](console, str(root))
    with mock.patch("builtins.open", side_effect=AssertionError("memory Host accessed filesystem")):
        assert host.load_console_snapshot()["workspaces"] == []
        binding = host.reopen()
        snapshot = host.load_console_snapshot(capture_binding=binding)
        workspace, = snapshot["workspaces"]
        document = host.load_authorized_source_document(
            None, workspace["authorizationId"], workspace["workspaceId"], binding)
        text = "// typed through a native editor\n" + document["text"]
        saved = host.save(host.live_id, host.authorization_id, host.workspace_id, document["rawSha256"], text)
    assert set(saved) == {"rawSha256", "size", "modifiedNs", "changed", "compileScheduled", "watchId"}
    assert saved["rawSha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert saved["size"] == len(text.encode("utf-8"))
    assert saved["changed"] is True and saved["compileScheduled"] is False
    assert 0 < saved["modifiedNs"] <= 0x7FFFFFFFFFFFFFFF
    assert host.text == text and len(host.calls) == 1
    assert not root.exists()


def test_ui_memory_host_rejects_stale_scope_authority_and_wrong_source_cas(tmp_path):
    fixture = _pure_memory_fixture()
    host = fixture["_MemoryHost"](console, str(tmp_path / "memory"))
    old_binding = host.reopen()
    old_ids = (host.live_id, host.authorization_id, host.workspace_id)
    old_hash = fixture["_sha"](host.text)
    host.close_capture()
    with pytest.raises(RuntimeError, match="stale capture"):
        host.load_console_snapshot(capture_binding=old_binding)
    fresh = host.reopen()
    assert fresh["capturePath"] == old_binding["capturePath"]
    assert fresh["captureSessionId"] != old_binding["captureSessionId"]
    with pytest.raises(RuntimeError, match="stale save"):
        host.save(*old_ids, old_hash, "bad")
    with pytest.raises(RuntimeError, match="CAS mismatch"):
        host.save(host.live_id, host.authorization_id, host.workspace_id, "0" * 64, "bad")
    assert host.text == fixture["SOURCE"] and host.calls == []


def test_ui_memory_stat_proxy_does_not_modify_shared_os_or_access_synthetic_source(tmp_path):
    fixture = _pure_memory_fixture()
    host = fixture["_MemoryHost"](console, str(tmp_path / "nonexistent"))
    proxy = fixture["_MemoryStatProxy"](host)
    original_stat = fixture["os"].stat
    metadata = proxy.stat(host.source_path)
    assert metadata.st_size == len(host.text.encode("utf-8"))
    assert metadata.st_mtime_ns == host.modified_ns
    assert fixture["os"].stat is original_stat
    assert proxy.stat(str(tmp_path)).st_mode == tmp_path.stat().st_mode
    assert not Path(host.source_path).exists()


def test_ui_companion_document_uses_same_scope_and_distinct_source_identity(tmp_path):
    fixture = _pure_memory_fixture()
    host = fixture["_MemoryHost"](console, str(tmp_path / "memory"))
    binding = host.reopen()
    companion = host.add_companion()
    workspaces = host.load_console_snapshot(capture_binding=binding)["workspaces"]
    assert len(workspaces) == 2
    assert {item["workspaceId"] for item in workspaces} == {host.workspace_id, companion.workspace_id}
    assert len({item["resourceId"] for item in workspaces}) == 2
    opened = host.load_authorized_source_document(None, companion.authorization_id, companion.workspace_id, binding)
    assert opened["sourcePath"] == companion.source_path
    assert fixture["_MemoryStatProxy"](host).stat(companion.source_path).st_size == opened["size"]
    host.close_capture()
    assert companion.current_binding() is None
    fresh = host.reopen()
    assert companion.current_binding() == fresh
    with pytest.raises(RuntimeError, match="stale capture"):
        host.load_authorized_source_document(None, companion.authorization_id, companion.workspace_id, binding)
    assert not Path(host.root).exists() and not Path(companion.root).exists()


def test_ui_return_diagnostics_preserve_mixed_native_line_endings_exactly():
    fixture = _pure_memory_fixture()
    text = "\r\n" + fixture["SOURCE"]
    evidence = fixture["_text_evidence"](text)
    assert evidence["text"] == text and evidence["repr"] == repr(text)
    assert bytes.fromhex(evidence["utf8Hex"]) == text.encode("utf-8")
    assert evidence["newlines"] == {"crlf": 1, "lfOnly": fixture["SOURCE"].count("\n"), "crOnly": 0}
    assert evidence["sha256"] != fixture["_text_evidence"]("\n" + fixture["SOURCE"])["sha256"]
