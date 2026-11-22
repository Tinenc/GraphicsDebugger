"""A restored same-program Shader must remain a canonical editable document."""
import copy
import json

import pytest

import test_shader_console as fixtures

console = fixtures.console


def _restored_fixture(tmp_path, monkeypatch, api="D3D11", encoding="DXBC"):
    fixture = fixtures.ImportedProjectSourceFixture(tmp_path)
    project, editor = fixture.project, fixture.editor
    raw = "f" * 64
    manifest = fixture.manifest
    manifest["shader"]["rawSha256"] = raw
    manifest["capture"].update(api="GraphicsAPI." + api, catalogSha256="d" * 64)
    manifest["baseline"].update(codeSha256=project.CODE_SHA256, kind=encoding)
    receipt = manifest["source"]["projectSource"]
    receipt["originalRawSha256"] = raw
    receipt.pop("receiptSha256")
    receipt["receiptSha256"] = console._canonical_sha256(receipt)
    editor._write(editor.workspace / "manifest.json", manifest)
    registry = json.loads(editor.registry.read_bytes())
    entry = registry["entries"][0]
    entry["workspaceIdentity"]["shaderRawSha256"] = raw
    entry["workspaceIdentitySha256"] = console._canonical_sha256(entry["workspaceIdentity"])
    editor._write(editor.registry, registry)
    for path in list((editor.workspace / "watch").glob("*.json")) + list((editor.workspace / "live-edit").glob("*.json")):
        path.write_text(path.read_text(encoding="utf-8").replace(project.RAW_SHA256, raw), encoding="utf-8")
    origin = project.replay_value["captures"][0]
    origin["api"] = api
    origin["shaders"][0]["encoding"] = "ShaderEncoding." + encoding
    target = copy.deepcopy(origin)
    target.update(captureId=editor.CAPTURE_ID, catalogSha256="d" * 64)
    target["shaders"][0]["rawSha256"] = raw
    project.replay_value["captures"].append(target)
    project.write_replay_manifest()
    monkeypatch.setattr(console, "default_active_project_selection_path", lambda: str(project.selection))
    return fixture, target


@pytest.mark.parametrize("api,encoding", [("D3D11", "DXBC"), ("D3D12", "DXIL"),
                                         ("Vulkan", "SPIR-V"), ("OpenGL", "GLSL")])
def test_equivalent_capture_restore_keeps_live_document_visible(tmp_path, monkeypatch, api, encoding):
    fixture, target = _restored_fixture(tmp_path, monkeypatch, api, encoding)
    editor = fixture.editor
    snapshot = console.load_console_snapshot(str(editor.registry), capture_binding=editor.capture_binding)
    assert not snapshot["errors"]
    model = snapshot["workspaces"][0]
    assert model["editorEligible"], model
    assert model["autoApplyReady"]
    document = console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID,
        editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
    assert document["text"] == fixture.project.source_text
    assert model["captureBinding"] == editor.capture_binding


def test_complete_interface_without_resource_layout_opens_restored_source(tmp_path, monkeypatch):
    fixture, target = _restored_fixture(tmp_path, monkeypatch)
    from test_shader_console_project_formats import _bind_record
    project = fixture.project
    record = project.replay_value["knowledge"]["workspaces"][0]
    previous = record["knowledgeId"]
    record["layoutHash"] = ""
    project.replay_value["captures"][0]["shaders"][0]["layoutHash"] = ""
    target["shaders"][0]["layoutHash"] = ""
    _bind_record(project, record, project.source.read_bytes())
    project.replay_value["activeKnowledge"]["workspaces"] = {record["knowledgeId"]: 1}
    assert previous != record["knowledgeId"]
    project.write_replay_manifest()
    receipt = fixture.manifest["source"]["projectSource"]
    receipt["knowledgeId"] = record["knowledgeId"]
    receipt.pop("receiptSha256")
    receipt["receiptSha256"] = console._canonical_sha256(receipt)
    fixture.editor._write(fixture.editor.workspace / "manifest.json", fixture.manifest)
    model = console.load_console_snapshot(str(fixture.editor.registry),
        capture_binding=fixture.editor.capture_binding)["workspaces"][0]
    assert model["editorEligible"] and model["autoApplyReady"], model


@pytest.mark.parametrize("change", ["code", "interface", "layout", "incomplete", "api", "encoding", "capture", "baseline"])
def test_similar_source_cannot_authorize_unrelated_capture_document(tmp_path, monkeypatch, change):
    fixture, target = _restored_fixture(tmp_path, monkeypatch)
    if change in ("code", "interface", "layout"):
        target["shaders"][0][{"code": "codeSha256", "interface": "interfaceHash", "layout": "layoutHash"}[change]] = "0" * 64
    elif change == "incomplete":
        target["shaders"][0]["interfaceComplete"] = False
    elif change == "api":
        target["api"] = "Vulkan"
    elif change == "encoding":
        target["shaders"][0]["encoding"] = "spirv"
    elif change == "capture":
        target["captureId"] = "0" * 64
    else:
        fixture.manifest["baseline"]["codeSha256"] = "0" * 64
        fixture.editor._write(fixture.editor.workspace / "manifest.json", fixture.manifest)
    fixture.project.write_replay_manifest()
    editor = fixture.editor
    with pytest.raises(console.ShaderConsoleError):
        console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID,
            editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
    model = console.load_console_snapshot(str(editor.registry), capture_binding=editor.capture_binding)["workspaces"][0]
    assert not model["editorEligible"]
