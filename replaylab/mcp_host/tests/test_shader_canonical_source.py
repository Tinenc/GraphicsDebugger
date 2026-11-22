"""Existing source owners survive compiler/catalog/language changes."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.shader_workspace.canonical_source import capture_path_identity, existing_sources, select_source
from src.shader_workspace.errors import CatalogValidationError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class SourceReader:
    def __init__(self):
        self.reads = []

    def source_edit_status(self, workspace):
        self.reads.append(workspace)
        m = json.loads((workspace / "manifest.json").read_text())
        assert sha((workspace / "baseline.bin").read_bytes()) == m["shader"]["rawSha256"]
        source = m["source"]
        return {**m, "source": {"present": True, "format": source["format"],
            "currentSha256": sha((workspace / source["path"]).read_bytes()),
            "decompiledSha256": source["sha256"]}}


def owner(root, name, *, capture="frame.rdc", api="Vulkan", text=b"current source", language="hlsl"):
    workspace = root / name
    workspace.mkdir(parents=True)
    original = b"captured original bytes"
    (workspace / "baseline.bin").write_bytes(original)
    (workspace / ("shader." + language)).write_bytes(text)
    shader = {"resourceId": 9, "stage": "fragment", "entryPoint": "main", "rawSha256": sha(original)}
    m = {"workspaceId": sha(name.encode())[:32], "stage": "fragment", "entryPoint": "main",
        "capture": {"capturePath": str(root.parent / capture), "api": api,
            "captureId": "1" * 64, "catalogSha256": "2" * 64}, "shader": shader,
        "source": {"path": "shader." + language, "format": language, "sha256": sha(text)}}
    (workspace / "manifest.json").write_text(json.dumps(m))
    return workspace, m


def scan(root, model, registered=()):
    reader = SourceReader()
    rows, diagnostics = existing_sources(root, capture_path=model["capture"]["capturePath"],
        api=model["capture"]["api"], shader=model["shader"], reader=reader, registered=registered)
    assert diagnostics == []
    return rows, reader


@pytest.mark.parametrize("api", ["D3D11", "D3D12", "Vulkan", "OpenGL", "OpenGLES"])
def test_latest_registered_owner_beats_old_language_and_route_paths(tmp_path, api):
    root = tmp_path / "旧项目名称" / "shaders"
    old, old_manifest = owner(root, "v2/old-route", api=api, text=b"original GLSL", language="glsl")
    current, model = owner(root, "legacy/catalog/stage/current-hlsl", api=api)
    registrations = [{"workspacePath": str(old), "registeredSourceSha256": sha(b"original GLSL"),
        "activeCandidate": True, "registeredAt": "2024-01-01", "knowledgeId": "old"},
        {"workspacePath": str(current), "registeredSourceSha256": sha(b"current source"),
         "activeCandidate": True, "registeredAt": "2026-10-29", "knowledgeId": "current"}]
    rows, reader = scan(root, model, registrations)
    chosen = select_source(rows)
    assert chosen["workspacePath"] == str(current)
    assert chosen["selectionReason"] == "current-project-source"
    assert len(reader.reads) == 2
    assert old.is_dir() and current.is_dir()  # No migration/deletion.


def test_catalog_and_source_changes_are_read_without_renaming_owner(tmp_path):
    root = tmp_path / "shaders"
    workspace, model = owner(root, "v2/old-compiler-hash")
    before = (workspace / "manifest.json").read_bytes()
    first, unused = scan(root, model)
    (workspace / "shader.hlsl").write_bytes(b"user's newer source")
    second, unused = scan(root, model)
    assert select_source(second)["workspacePath"] == str(workspace)
    assert second[0]["sourceSha256"] != first[0]["sourceSha256"]
    assert second[0]["localChanges"] is True
    assert (workspace / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("changed", ["capture", "api", "original", "stage", "entry", "resource"])
def test_another_rdc_or_physical_shader_never_reuses_its_manifest(tmp_path, changed):
    root = tmp_path / "shaders"
    workspace, model = owner(root, "v2/owner")
    target = copy.deepcopy(model)
    if changed == "capture": target["capture"]["capturePath"] = str(tmp_path / "other.rdc")
    if changed == "api": target["capture"]["api"] = "D3D12"
    if changed == "original": target["shader"]["rawSha256"] = "f" * 64
    if changed == "stage": target["shader"]["stage"] = "vertex"
    if changed == "entry": target["shader"]["entryPoint"] = "other"
    if changed == "resource": target["shader"]["resourceId"] = 10
    rows, reader = scan(root, target)
    assert rows == [] and reader.reads == []
    assert workspace.is_dir()


def test_independent_dirty_sources_choose_latest_and_preserve_both(tmp_path):
    root = tmp_path / "shaders"
    first, model = owner(root, "v2/first")
    second, other = owner(root, "v2/second")
    (first / "shader.hlsl").write_bytes(b"first edit")
    (second / "shader.hlsl").write_bytes(b"second edit")
    os.utime(first / "shader.hlsl", ns=(100, 100))
    os.utime(second / "shader.hlsl", ns=(200, 200))
    rows, unused = scan(root, model)
    assert select_source(rows)["workspacePath"] == str(second)
    chosen = select_source(rows, ui={"selectedWorkspaceId": model["workspaceId"],
        "documents": [{"workspaceId": model["workspaceId"], "dirty": True}]})
    assert chosen["workspacePath"] == str(second)
    assert (first / "shader.hlsl").read_bytes() == b"first edit"
    assert (second / "shader.hlsl").read_bytes() == b"second edit"


def test_unsaved_older_ui_editor_is_preserved_without_overriding_newest_source(tmp_path):
    root = tmp_path / "shaders"
    first, model = owner(root, "v2/first")
    second, other = owner(root, "v2/second", text=b"other saved source")
    os.utime(first / "shader.hlsl", ns=(1_800_000_000_000_000_000,) * 2)
    os.utime(second / "shader.hlsl", ns=(1_700_000_000_000_000_000,) * 2)
    rows, unused = scan(root, model, [{"workspacePath": str(first),
        "registeredSourceSha256": sha(b"current source"), "activeCandidate": True, "registeredAt": "2026"}])
    chosen = select_source(rows, ui={"documents": [{"workspaceId": other["workspaceId"], "dirty": True}]})
    assert chosen["workspacePath"] == str(first)
    assert (second / "shader.hlsl").read_bytes() == b"other saved source"


def test_clean_selected_legacy_tab_does_not_override_current_project_head(tmp_path):
    root = tmp_path / "shaders"
    old, model = owner(root, "old-glsl", language="glsl", text=b"old source")
    current, unused = owner(root, "current-hlsl")
    rows, unused = scan(root, model, [{"workspacePath": str(current),
        "registeredSourceSha256": sha(b"current source"), "activeCandidate": True,
        "registeredAt": "2026"}])
    selected = select_source(rows, ui={"selectedWorkspaceId": model["workspaceId"],
        "documents": [{"workspaceId": model["workspaceId"], "dirty": False}]})
    assert selected["workspacePath"] == str(current)


@pytest.mark.parametrize("first,second,equal", [
    (r"D:\旧项目\T2.rdc", "d:/旧项目/t2.rdc", True),
    (r"\\Server\Share\T2.rdc", "//server/share/t2.rdc", True),
    ("/captures/Frame.rdc", "/captures/frame.rdc", False),
    ("/captures/folder/../Frame.rdc", "/captures/Frame.rdc", True),
    ("adb://device/captures/Frame.rdc", "adb://device/captures/frame.rdc", False),
    ("adb://device/captures/a.rdc", "ssh://device/captures/a.rdc", False),
])
def test_capture_identity_follows_path_platform(first, second, equal):
    assert (capture_path_identity(first) == capture_path_identity(second)) is equal


def test_known_damaged_source_is_not_replaced_with_a_new_baseline(tmp_path):
    root = tmp_path / "shaders"
    workspace, model = owner(root, "current")
    (workspace / "shader.hlsl").unlink()
    with pytest.raises(RuntimeError, match="requires recovery"):
        scan(root, model)
    assert (workspace / "manifest.json").is_file()


def test_many_retained_versions_do_not_block_selection_or_truncate_to_old_limit(tmp_path):
    root = tmp_path / "shaders"
    for index in range(33):
        unused, model = owner(root, str(index))
    latest = root / "0" / "shader.hlsl"
    os.utime(latest, ns=(2_000_000_000_000_000_000,) * 2)
    rows, unused = scan(root, model)
    assert len(rows) == 33
    assert select_source(rows)["workspacePath"] == str(latest.parent)


def test_source_read_must_still_match_the_discovered_original(tmp_path):
    root = tmp_path / "shaders"
    unused, model = owner(root, "current")
    class ChangedReader(SourceReader):
        def source_edit_status(self, workspace):
            status = super().source_edit_status(workspace)
            status["shader"]["rawSha256"] = "f" * 64
            return status
    with pytest.raises(RuntimeError, match="identity changed"):
        existing_sources(root, capture_path=model["capture"]["capturePath"],
            api=model["capture"]["api"], shader=model["shader"], reader=ChangedReader())


@pytest.mark.parametrize("api", ["D3D11", "D3D12", "Vulkan", "OpenGL", "OpenGLES"])
def test_begin_keeps_original_source_when_new_eid_was_not_in_old_catalog(tmp_path, monkeypatch, api):
    from src import server
    from src.shader_workspace import organization
    catalog = tmp_path / "old-catalog.json"
    catalog.write_text(json.dumps({"schema": "renderdoc-ultra-capture-symbols/v1",
                                  "schemaVersion": 1, "uses": []}))
    before = catalog.read_bytes()
    shader = {"resourceId": 9, "stage": "fragment", "entryPoint": "main", "rawSha256": "a" * 64}
    status = {"workspaceId": "b" * 32, "capture": {"catalogPath": str(catalog),
        "capturePath": "D:/旧项目/t2.rdc", "captureId": "c" * 64, "api": api},
        "shader": shader, "stage": "fragment", "entryPoint": "main", "targetProfile": {},
        "baseline": {"rawSha256": "a" * 64}}
    context = {"capture": {"path": "d:\\旧项目\\T2.rdc", "api": api,
                           "captureSessionId": "d" * 32, "captureId": "e" * 64}}
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: object())
    monkeypatch.setattr(organization, "_draft_status", lambda *_: copy.deepcopy(status))
    monkeypatch.setattr(server, "_active_live_context", lambda **_: copy.deepcopy(context))
    calls = []
    def bound(current, event, stage):
        calls.append((current, event, stage))
        return copy.deepcopy(shader)
    monkeypatch.setattr(server, "_active_bound_shader", bound)
    monkeypatch.setattr(server, "_require_same_active_capture", lambda *_, **__: None)
    result = server._shader_organization_semantic_context(str(tmp_path), 5372)
    assert result["anchorEventId"] == 5372
    assert result["workspaceId"] == status["workspaceId"]
    assert result["liveBindingEvidence"]["identityVerified"] is True
    assert len(calls) == 1 and calls[0][1:] == (5372, "fragment")
    assert catalog.read_bytes() == before
    context["capture"]["path"] = "D:/旧项目/t3.rdc"
    with pytest.raises(server.ShaderOrganizationError, match="workspace capture"):
        server._shader_organization_semantic_context(str(tmp_path), 5372)
    assert len(calls) == 1
    context["capture"]["path"] = "D:/旧项目/t2.rdc"
    monkeypatch.setattr(server, "_active_bound_shader", lambda *_: {**shader, "resourceId": 10})
    with pytest.raises(server.ShaderOrganizationError, match="not an exact use"):
        server._shader_organization_semantic_context(str(tmp_path), 5372)
    catalog.write_text('{"schema": "damaged", "schemaVersion": 1, "uses": []}')
    with pytest.raises(CatalogValidationError, match="schema/version"):
        server._shader_organization_semantic_context(str(tmp_path), 5372)
