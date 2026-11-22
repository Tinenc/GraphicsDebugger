"""Source import keeps its editor when transport and project catalog hashes differ."""
import copy
import json

import pytest

from tests.test_shader_console import ImportedProjectSourceFixture, console


@pytest.fixture
def imported_source(tmp_path, monkeypatch):
    fixture = ImportedProjectSourceFixture(tmp_path)
    project, editor = fixture.project, fixture.editor
    target_raw, target_catalog = "0" * 64, "1" * 64
    manifest = fixture.manifest
    manifest["capture"].update(catalogSha256="2" * 64, api="D3D12")
    manifest["shader"]["rawSha256"] = target_raw
    manifest["baseline"].update(codeSha256=project.CODE_SHA256, kind="DXIL")
    receipt = manifest["source"]["projectSource"]
    receipt["originalRawSha256"] = target_raw
    receipt.pop("receiptSha256")
    receipt["receiptSha256"] = console._canonical_sha256(receipt)
    editor._write(editor.workspace / "manifest.json", manifest)

    registry = json.loads(editor.registry.read_text(encoding="utf8"))
    authorization = registry["entries"][0]
    authorization["workspaceIdentity"]["shaderRawSha256"] = target_raw
    authorization["workspaceIdentitySha256"] = console._canonical_sha256(
        authorization["workspaceIdentity"])
    editor._write(editor.registry, registry)

    target = copy.deepcopy(project.replay_value["captures"][0])
    target.update(captureId=editor.CAPTURE_ID, catalogSha256=target_catalog)
    target["shaders"][0]["rawSha256"] = target_raw
    target["catalogRevisions"] = [{
        "catalogSha256": target_catalog, "shaders": copy.deepcopy(target["shaders"])}]
    project.replay_value["captures"].append(target)
    project.write_replay_manifest()
    monkeypatch.setattr(console, "default_active_project_selection_path", lambda: str(project.selection))
    return fixture, target


def _open(fixture):
    editor = fixture.editor
    return console.load_authorized_source_document(
        str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID,
        capture_binding=editor.capture_binding)


@pytest.mark.parametrize("repeated_revision", [False, True])
def test_first_source_import_resolves_exact_target_evidence_without_destination_publication(
        imported_source, repeated_revision):
    fixture, target = imported_source
    # Only the other capture's source knowledge exists: no destination source
    # publication can supply its normalized catalog SHA on this first import.
    assert len(fixture.project.replay_value["knowledge"]["workspaces"]) == 1
    if repeated_revision:
        revision = copy.deepcopy(target["catalogRevisions"][0])
        revision["catalogSha256"] = "3" * 64
        revision["shaders"][0]["eventIds"] = [99]
        target["catalogRevisions"].append(revision)
        target["activeCatalogSha256"] = revision["catalogSha256"]
        fixture.project.write_replay_manifest()
    assert _open(fixture)["text"] == fixture.project.source_text
    snapshot = console.load_console_snapshot(
        str(fixture.editor.registry), capture_binding=fixture.editor.capture_binding)
    assert snapshot["errors"] == []
    workspace, = snapshot["workspaces"]
    assert workspace["editorEligible"] is True
    assert workspace["autoApplyReady"] is True
    assert workspace["sourceSession"] == {
        "liveEditId": fixture.editor.LIVE_ID, "watchId": fixture.editor.WATCH_ID}


@pytest.mark.parametrize("field,value", [
    ("rawSha256", "9" * 64), ("resourceId", 999), ("stage", "vertex"),
    ("entryPoint", "other"), ("codeSha256", "9" * 64),
    ("interfaceHash", "0" * 64), ("interfaceComplete", False),
    ("layoutHash", "0" * 64), ("encoding", "spirv"),
])
def test_transport_hash_fallback_preserves_exact_target_and_program_checks(imported_source, field, value):
    fixture, target = imported_source
    target["catalogRevisions"][0]["shaders"][0][field] = value
    fixture.project.write_replay_manifest()
    with pytest.raises(console.ShaderConsoleError):
        _open(fixture)


@pytest.mark.parametrize("field,value", [
    ("codeSha256", "9" * 64), ("interfaceHash", "0" * 64),
    ("interfaceComplete", False), ("layoutHash", "0" * 64), ("encoding", "spirv"),
])
def test_conflicting_registered_revisions_are_not_resolved_by_active_revision(imported_source, field, value):
    fixture, target = imported_source
    revision = copy.deepcopy(target["catalogRevisions"][0])
    revision["catalogSha256"] = "3" * 64
    revision["shaders"][0][field] = value
    target["catalogRevisions"].append(revision)
    # Even the original correct revision being active cannot resolve conflict.
    target["activeCatalogSha256"] = target["catalogRevisions"][0]["catalogSha256"]
    fixture.project.write_replay_manifest()
    with pytest.raises(console.ShaderConsoleError, match="catalog provenance"):
        _open(fixture)


def test_catalog_fallback_does_not_search_other_captures(imported_source):
    fixture, target = imported_source
    target["captureId"] = "9" * 64
    fixture.project.write_replay_manifest()
    with pytest.raises(console.ShaderConsoleError, match="capture provenance"):
        _open(fixture)


def test_duplicate_target_inside_one_revision_remains_ambiguous(imported_source):
    fixture, target = imported_source
    rows = target["catalogRevisions"][0]["shaders"]
    rows.append(copy.deepcopy(rows[0]))
    fixture.project.write_replay_manifest()
    with pytest.raises(console.ShaderConsoleError, match="catalog provenance"):
        _open(fixture)
