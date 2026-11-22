"""Fresh inventories may seed old workspaces without rewriting their provenance."""
import copy
import json
from pathlib import Path

import pytest

from src import projects, project_restore
from src.shader_workspace.abi import parse_shader_abi
from src.shader_workspace.project_source import source_receipt, validate_project_source
from tests import test_projects as support
from tests.test_shader_abi import complete_interface


@pytest.fixture
def sample():
    fixture = support.ReplayProjectTests()
    fixture.setUp()
    try:
        catalog, shader, capture = fixture._catalog("old", support._dxbc(), resource_id=91)
        fixture.service.import_capture(catalog)
        status = fixture._workspace_status("old-workspace", capture, shader,
                                           "float4 main():SV_Target{return 1;}\n")
        old_interface = complete_interface()
        old_interface["outputTopology"] = ""
        old = parse_shader_abi(old_interface)
        fresh = parse_shader_abi(dict(old_interface, outputTopology="Unknown"))
        status["baseline"].update(compiledInterface=old_interface, abiSha256=old.sha256)
        status["capture"]["api"] = "D3D11"
        status = fixture._with_fully_validated_candidate(status, support._dxbc(b"edited"))
        inventory = fixture._shader_inventory(resource_id=91, raw_sha256=shader["rawSha256"],
            abi_hash=fresh.sha256, layout_hash="1" * 64, interface_hash="2" * 64)
        yield fixture, status, inventory, shader
    finally:
        fixture.tearDown()


def test_inventory_registration_reopens_without_rewriting_historical_binary_origin(sample):
    fixture, status, inventory, shader = sample
    before = copy.deepcopy(status)
    saved = {p: p.read_bytes() for p in Path(status["workspacePath"]).rglob("*") if p.is_file()}
    result = fixture.service.register_workspace_from_inventory(status, inventory["shaders"][0], inventory,
        fixture.service.reuse_snapshot_for_shader_inventory(inventory)["binding"])
    record = result["knowledge"]
    assert result["compiledCandidateCache"] == {
        "retained": False, "reasonId": "historical-topology-abi-recompile-required"}
    assert "compiledCandidate" not in record
    assert record["baselineAbiSha256"] == inventory["shaders"][0]["abiHash"]
    assert record["inventoryOrigin"]["abiHash"] == record["baselineAbiSha256"]
    assert projects.ReplayProjectService(fixture.project).get_workspace_knowledge(record["knowledgeId"]) == record
    repeated = fixture.service.register_workspace_from_inventory(status, inventory["shaders"][0], inventory)
    assert repeated["idempotent"] and repeated["compiledCandidateCache"] == result["compiledCandidateCache"]
    assert status == before
    assert all(p.read_bytes() == data for p, data in saved.items())


def test_fresh_inventory_source_seed_and_prepared_restore_keep_old_baseline(sample):
    fixture, status, inventory, shader = sample
    result = fixture.service.register_workspace_from_inventory(status, inventory["shaders"][0], inventory,
        fixture.service.reuse_snapshot_for_shader_inventory(inventory)["binding"])
    record = result["knowledge"]
    destination = fixture._workspace_status("reopened", status["capture"]["captureId"], shader,
                                             "float4 main():SV_Target{return 0;}\n")
    destination["baseline"] = copy.deepcopy(status["baseline"])
    destination["capture"]["api"] = "D3D11"
    before_manifest = Path(destination["manifestPath"]).read_bytes()
    before_baseline = copy.deepcopy(destination["baseline"])
    seeded = fixture.service.seed_workspace_from_inventory(record["knowledgeId"], destination,
        inventory["shaders"][0], inventory, fixture.service.reuse_snapshot_for_shader_inventory(inventory)["binding"])
    assert seeded["requiresCompile"] and seeded["requiresLiveAbiRevalidation"]
    assert not seeded["verifiedBinaryCandidateMaySkipCompile"]
    assert Path(destination["source"]["path"]).read_bytes() == Path(status["source"]["path"]).read_bytes()
    assert destination["baseline"] == before_baseline
    assert Path(destination["manifestPath"]).read_bytes() == before_manifest
    destination["source"]["currentSha256"] = record["sourceSha256"]
    project_restore._validate_prepared_project_source(destination, record,
        project_restore._normal_shader_identity(inventory["shaders"][0]), {})


def test_imported_source_receipt_keeps_exact_historical_baseline_on_inventory_restore(sample):
    fixture, status, inventory, shader = sample
    result = fixture.service.register_workspace_from_inventory(status, inventory["shaders"][0], inventory,
        fixture.service.reuse_snapshot_for_shader_inventory(inventory)["binding"])
    record = result["knowledge"]
    source_bytes = Path(status["source"]["path"]).read_bytes()
    destination = fixture._workspace_status("imported-source", status["capture"]["captureId"], shader,
                                            source_bytes.decode("utf-8"))
    Path(destination["source"]["path"]).write_bytes(source_bytes)
    destination["source"].update(currentSha256=record["sourceSha256"],
                                 decompiledSha256=record["sourceSha256"])
    destination["baseline"] = copy.deepcopy(status["baseline"])
    destination["capture"]["api"] = "D3D11"
    empty = copy.deepcopy(destination)
    empty["source"]["present"] = False
    bundle = fixture.service.prepare_workspace_source(record["knowledgeId"], empty)
    assert bundle["baselineAbiSha256"] == destination["baseline"]["abiSha256"]
    assert bundle["baselineAbiSha256"] != inventory["shaders"][0]["abiHash"]

    root = Path(destination["workspacePath"])
    input_path = root / "source-inputs" / ("project-%s.hlsl" % record["sourceSha256"])
    input_path.parent.mkdir()
    input_path.write_bytes(source_bytes)
    receipt = source_receipt(bundle, input_path.relative_to(root).as_posix())
    source_record = {"path": "source/shader.hlsl", "sha256": record["sourceSha256"],
                     "entryPoint": bundle["sourceEntryPoint"],
                     "origin": "verified-project-source", "projectSource": receipt}
    manifest = {key: copy.deepcopy(destination[key]) for key in
                ("workspaceId", "stage", "entryPoint", "targetProfile", "baseline")}
    manifest.update(source=source_record, routePlan={"editableFormat": "hlsl"})
    Path(destination["manifestPath"]).write_text(json.dumps(manifest), encoding="utf8")
    assert validate_project_source(root, manifest, source_record) == receipt
    destination["source"].update(origin="verified-project-source", projectSource=receipt)
    identity = project_restore._normal_shader_identity(inventory["shaders"][0])
    binding = {"projectId": bundle["projectId"], "projectPath": bundle["projectPath"]}
    before = copy.deepcopy(destination)
    project_restore._validate_prepared_project_source(destination, record, identity, binding)
    assert destination == before
    # A live-equivalent hash cannot rewrite the receipt's sealed workspace origin.
    destination["source"]["projectSource"]["baselineAbiSha256"] = identity["abiHash"]
    with pytest.raises(RuntimeError, match="baselineAbiSha256"):
        project_restore._validate_prepared_project_source(destination, record, identity, binding)


@pytest.mark.parametrize("change", ["missing", "tampered", "wrong-hash", "real-topology"])
def test_inventory_alias_rejects_unverified_or_changed_physical_evidence(sample, change):
    fixture, status, inventory, shader = sample
    if change == "missing":
        status["baseline"].pop("compiledInterface")
    elif change == "tampered":
        status["baseline"]["compiledInterface"]["readOnlyResources"][0]["bind"] += 1
    elif change == "wrong-hash":
        status["baseline"]["abiSha256"] = "9" * 64
    else:
        status["baseline"]["compiledInterface"]["outputTopology"] = "TriangleList"
        status["baseline"]["abiSha256"] = parse_shader_abi(status["baseline"]["compiledInterface"]).sha256
    before = Path(status["source"]["path"]).read_bytes()
    with pytest.raises(projects.ReplayProjectError):
        fixture.service.register_workspace_from_inventory(status, inventory["shaders"][0], inventory)
    with pytest.raises(projects.ReplayProjectError):
        fixture.service.seed_workspace_from_inventory("1" * 32, status, inventory["shaders"][0], inventory)
    assert Path(status["source"]["path"]).read_bytes() == before
