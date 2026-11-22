"""Saved organization/project receipts keep their declared text language."""
import copy
import json
from unittest import mock

import pytest
import test_shader_console as fixtures

console = fixtures.console


def organized_glsl(tmp_path):
    fixture = fixtures.ShaderConsoleFixture(tmp_path)
    for field in ("source", "baseline_artifact", "proposal_artifact"):
        old = getattr(fixture, field)
        new = old.with_suffix(".glsl")
        old.rename(new)
        setattr(fixture, field, new)
    manifest_path = fixture.workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["source"]["path"] = "source/shader.glsl"
    fixture._write(manifest_path, manifest)
    gate_path = fixture.workspace / "ai/organize-code-gate.json"
    gate = json.loads(gate_path.read_text("utf-8"))
    gate.update(executableEditableSourcePath="source/shader.glsl",
                proposalSourcePath=str(fixture.proposal_artifact))
    fixture._write(gate_path, gate)
    fixture._write(fixture.session_dir / "gate.json", gate)
    session_path = fixture.session_dir / "session.json"
    session = json.loads(session_path.read_text("utf-8"))
    session.update(sourceFormat="glsl", sourcePath="source/shader.glsl",
                   baselineSourcePath=fixture.baseline_artifact.relative_to(fixture.workspace).as_posix(),
                   proposalSourcePath=fixture.proposal_artifact.relative_to(fixture.workspace).as_posix())
    fixture._write(session_path, fixture.organization_manager._seal_session(session))
    fixture.write_watch()
    fixture.write_live()
    return fixture


def test_completed_glsl_source_stays_visible_and_editable(tmp_path):
    fixture = organized_glsl(tmp_path)
    snapshot = console.load_console_snapshot(str(fixture.registry), fixture.capture_binding)
    assert not snapshot["errors"]
    assert len(snapshot["workspaces"]) == 1
    assert snapshot["workspaces"][0]["editorEligible"]
    opened = console.load_authorized_source_document(str(fixture.registry),
        fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID, fixture.capture_binding)
    assert opened["readOnly"] is False
    assert opened["text"] == fixture.source.read_bytes().decode("utf-8")


@pytest.mark.parametrize("change", ["language", "suffix", "bytes", "escape"])
def test_glsl_receipt_does_not_relax_language_paths_or_hashes(tmp_path, change):
    fixture = organized_glsl(tmp_path)
    session_path = fixture.session_dir / "session.json"
    session = json.loads(session_path.read_text("utf-8"))
    if change == "language":
        session["sourceFormat"] = "hlsl"
    elif change == "suffix":
        session["baselineSourcePath"] = session["baselineSourcePath"].replace(".glsl", ".hlsl")
        fixture.baseline_artifact.with_suffix(".hlsl").write_bytes(fixture.baseline_artifact.read_bytes())
    elif change == "escape":
        session["baselineSourcePath"] = "../baseline-source.glsl"
    else:
        fixture.baseline_artifact.write_bytes(b"changed")
    fixture._write(session_path, fixture.organization_manager._seal_session(session))
    with pytest.raises(console.ShaderConsoleError):
        console.load_authorized_source_document(str(fixture.registry),
            fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID, fixture.capture_binding)


def test_imported_glsl_input_is_language_bound(tmp_path):
    fixture = fixtures.ImportedProjectSourceFixture(tmp_path)
    editor = fixture.editor
    registry = json.loads(editor.registry.read_text("utf-8"))
    authorization = console._validate_authorization(registry["entries"][0])
    library = console._project_library_for_console(str(fixture.project.selection))
    old = editor.source
    editor.source = old.with_suffix(".glsl")
    old.rename(editor.source)
    receipt = fixture.manifest["source"]["projectSource"]
    receipt["sourceFormat"] = "glsl"
    receipt["inputPath"] = receipt["inputPath"].replace(".hlsl", ".glsl")
    receipt.pop("receiptSha256")
    receipt["receiptSha256"] = console._canonical_sha256(receipt)
    fixture.input_path.rename(fixture.input_path.with_suffix(".glsl"))
    fixture.manifest["source"]["path"] = "source/shader.glsl"
    editor._write(editor.workspace / "manifest.json", fixture.manifest)
    library = copy.deepcopy(library)
    library["documents"][0]["sourceFormat"] = "glsl"
    with mock.patch.object(console, "_project_library_for_console", return_value=library):
        gate = console._validate_project_source_gate(str(editor.workspace), authorization, str(editor.source))
        assert gate["kind"] == "verified-project-source"
        library["documents"][0]["sourceFormat"] = "hlsl"
        with pytest.raises(console.ShaderConsoleError, match="language"):
            console._validate_project_source_gate(str(editor.workspace), authorization, str(editor.source))
