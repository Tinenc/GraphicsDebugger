"""The Bridge accepts the Host's content-bound project Shader formats exactly."""
import copy
import hashlib
from pathlib import Path

import pytest

from src import projects
import test_shader_console as fixtures

console = fixtures.console
GLSL = b"#version 450\nlayout(location=0) out vec4 color;\nvoid main(){color=vec4(1);}\n"


def glsl_source(status):
    original = Path(status["source"]["path"])
    path = original.with_suffix(".glsl")
    path.write_bytes(GLSL)
    original.unlink()
    status["source"].update(path=str(path), format="glsl", currentSha256=hashlib.sha256(GLSL).hexdigest())


@pytest.mark.parametrize("compiled", [False, True])
def test_bridge_reads_glsl_created_by_real_project_writer(tmp_path, compiled):
    fixture = fixtures.WriterProjectShaderLibraryFixture(tmp_path)
    catalog, shader, capture_id = fixture.catalog()
    fixture.service.import_capture(catalog)
    status = fixture.workspace_status("glsl-writer", capture_id, shader["shaderResourceId"],
                                      shader["rawSha256"], shader["codeSha256"])
    glsl_source(status)
    if compiled:
        fixture.add_validated_candidate(status, fixtures._writer_dxbc(b"glsl cross-compiled candidate"))
    registered = fixture.service.register_workspace(status, include_compiled_candidate=compiled)["knowledge"]
    snapshot = console.load_project_shader_library_snapshot(str(fixture.selection))
    assert snapshot["available"] and not snapshot["errors"], snapshot["errors"]
    assert len(snapshot["documents"]) == 1
    document = snapshot["documents"][0]
    assert document["sourceFormat"] == "glsl" and document["fileName"].endswith(".glsl")
    assert document["sourceSha256"] == hashlib.sha256(GLSL).hexdigest()
    assert document["knowledgeId"] == registered["knowledgeId"]
    assert document["compiledCandidateAvailable"] is compiled
    loaded = console.load_project_shader_source_document(str(fixture.selection),
        document["projectWorkspaceId"], document["projectId"], document["knowledgeId"], document["sourceSha256"])
    assert loaded["sourceFormat"] == "glsl"
    assert Path(loaded["sourcePath"]).read_bytes() == GLSL


def test_inventory_glsl_identity_matches_host_and_library_reader(tmp_path):
    fixture = fixtures.WriterProjectShaderLibraryFixture(tmp_path)
    fingerprint = fixtures.fingerprint_bytes(fixtures._writer_dxbc(b"inventory baseline"))
    inventory = fixture.inventory(909, fingerprint.raw_sha256)
    status = fixture.workspace_status("inventory-glsl", inventory["capture"]["captureId"], 909,
                                      fingerprint.raw_sha256, fingerprint.code_sha256)
    glsl_source(status)
    fixture.add_validated_candidate(status, fixtures._writer_dxbc(b"inventory candidate"))
    binding = fixture.service.reuse_snapshot_for_shader_inventory(inventory)["binding"]
    registered = fixture.service.register_workspace_from_inventory(
        status, inventory["shaders"][0], inventory, binding)["knowledge"]
    assert console._project_inventory_workspace_artifact_identity(registered) == projects._inventory_workspace_artifact_identity(registered)
    snapshot = console.load_project_shader_library_snapshot(str(fixture.selection))
    assert snapshot["available"] and not snapshot["errors"], snapshot["errors"]
    assert snapshot["documents"][0]["sourceFormat"] == "glsl"
    assert snapshot["documents"][0]["inventoryBacked"] is True


def _record_case(tmp_path):
    fixture = fixtures.ProjectShaderLibraryFixture(tmp_path)
    record = copy.deepcopy(fixture.replay_value["knowledge"]["workspaces"][0])
    captures = copy.deepcopy(fixture.replay_value["captures"])
    activation = {"projectWorkspaceId": fixture.PROJECT_WORKSPACE_ID,
                  "projectId": fixture.REPLAY_PROJECT_ID, "activationRevision": "test"}
    return fixture, record, captures, activation


def _bind_record(fixture, record, source=GLSL):
    record["sourceSha256"] = hashlib.sha256(source).hexdigest()
    metadata = {"knowledgeId", "sourcePath", "sourceOriginPath", "workspacePath",
                "workspaceManifestSha256", "registeredAt"}
    identity = {key: value for key, value in record.items() if key not in metadata}
    if identity.get("sourceFormat", "hlsl") == "hlsl":
        identity.pop("sourceFormat", None)
    record["knowledgeId"] = projects._canonical_sha256(identity)[:32]
    record["sourcePath"] = "knowledge/workspaces/%s.%s" % (record["knowledgeId"], record.get("sourceFormat", "hlsl"))
    (fixture.replay_root / record["sourcePath"]).write_bytes(source)


def test_native_legacy_raw_code_fallback_requires_the_exact_capture_identity(tmp_path):
    fixture, record, captures, activation = _record_case(tmp_path)
    captures[0]["shaders"][0]["codeSha256"] = ""
    record.update(sourceFormat="glsl", codeSha256=record["rawSha256"])
    _bind_record(fixture, record)
    document = console._validate_project_workspace_record(str(fixture.replay_root), captures, record, activation)
    assert document["sourceFormat"] == "glsl" and document["originResourceId"] == 101
    # Changing another field cannot use the missing code fingerprint as a bypass.
    for key, value in (("codeSha256", "f" * 64), ("layoutHash", "f" * 64),
                       ("interfaceHash", "f" * 64), ("rawSha256", "f" * 64),
                       ("originResourceId", 102), ("stage", "vertex"), ("entryPoint", "other")):
        changed = {**record, key: value}
        _bind_record(fixture, changed)
        with pytest.raises(console.ShaderConsoleError, match="physical identity|imported Shader"):
            console._validate_project_workspace_record(str(fixture.replay_root), captures, changed, activation)
    captures[0]["shaders"][0]["codeSha256"] = fixture.CODE_SHA256
    with pytest.raises(console.ShaderConsoleError, match="physical identity"):
        console._validate_project_workspace_record(str(fixture.replay_root), captures, record, activation)


def test_format_is_bound_to_knowledge_identity_extension_and_known_schema(tmp_path):
    fixture, record, captures, activation = _record_case(tmp_path)
    record["sourceFormat"] = "glsl"
    _bind_record(fixture, record)
    for invalid in ("wgsl", "GLSL", None, {}):
        with pytest.raises(console.ShaderConsoleError, match="source format"):
            console._validate_project_workspace_record(str(fixture.replay_root), captures,
                                                       {**record, "sourceFormat": invalid}, activation)
    with pytest.raises(console.ShaderConsoleError, match="knowledgeId"):
        console._validate_project_workspace_record(str(fixture.replay_root), captures,
                                                   {**record, "sourceFormat": "hlsl"}, activation)
    with pytest.raises(console.ShaderConsoleError, match="source path"):
        console._validate_project_workspace_record(str(fixture.replay_root), captures,
            {**record, "sourcePath": record["sourcePath"].replace(".glsl", ".hlsl")}, activation)
    with pytest.raises(console.ShaderConsoleError, match="fields are invalid"):
        console._validate_project_workspace_record(str(fixture.replay_root), captures,
                                                   {**record, "unsupportedField": True}, activation)
    (fixture.replay_root / record["sourcePath"]).write_bytes(GLSL + b"// changed")
    with pytest.raises(console.ShaderConsoleError, match="source hash mismatch"):
        console._validate_project_workspace_record(str(fixture.replay_root), captures, record, activation)
