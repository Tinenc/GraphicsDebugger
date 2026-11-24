"""Old zero-valued topology spelling must not strand editable workspaces."""
import ast
import copy
import hashlib
import json
import importlib.util
from pathlib import Path
from unittest import mock

import pytest

import tests.test_shader_abi as support
from src.shader_workspace import ManifestValidationError
from src.shader_workspace.abi import (
    compare_shader_abi, parse_shader_abi, same_physical_shader_abi, physical_shader_abi_hashes,
)


@pytest.fixture(scope="module")
def bridge_compare():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"
    nodes = []
    for node in ast.parse(path.read_text(encoding="utf8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_interface_incompatibilities":
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in (
                    "_INTERFACE_STRUCTURAL_KEYS", "_INTERFACE_USAGE_KEYS")
                for target in node.targets):
            nodes.append(node)
    spec = importlib.util.spec_from_file_location('topology_dxbc_layout', path.parent/'dxbc_cbuffer_layout.py')
    layout = importlib.util.module_from_spec(spec);spec.loader.exec_module(layout)
    namespace = {'_dxbc_cbuffer_layout': layout}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_interface_incompatibilities"]


@pytest.mark.parametrize("stage", ["vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh"])
@pytest.mark.parametrize("legacy_first", [True, False])
def test_topology_alias_keeps_original_hashes_and_all_physical_checks(stage, legacy_first, bridge_compare):
    current = support.complete_interface(stage)
    legacy = copy.deepcopy(current)
    legacy["outputTopology"] = ""
    values = [legacy, current] if legacy_first else [current, legacy]
    baseline, candidate = map(parse_shader_abi, values)
    baseline_hash, candidate_hash = baseline.sha256, candidate.sha256
    assert baseline_hash != candidate_hash
    result = compare_shader_abi(baseline, candidate)
    assert result.compatible
    assert result.baseline_sha256 == baseline_hash
    assert result.candidate_sha256 == candidate_hash
    assert same_physical_shader_abi(baseline, candidate)
    assert physical_shader_abi_hashes(baseline) == {baseline_hash, candidate_hash}
    assert physical_shader_abi_hashes(candidate) == {baseline_hash, candidate_hash}
    assert not bridge_compare(baseline.physical_dict(), candidate.physical_dict())

    for key, value in [("outputTopology", "Topology.TriangleList"), ("entryPoint", "other")]:
        changed = copy.deepcopy(values[1])
        changed[key] = value
        changed = parse_shader_abi(changed)
        assert not compare_shader_abi(baseline, changed).compatible
        assert not same_physical_shader_abi(baseline, changed)
        assert changed.sha256 not in physical_shader_abi_hashes(baseline)
        assert bridge_compare(baseline.physical_dict(), changed.physical_dict())
    changed = copy.deepcopy(values[1])
    changed["readOnlyResources"][0]["bind"] += 1
    changed = parse_shader_abi(changed)
    assert not compare_shader_abi(baseline, changed).compatible
    assert not same_physical_shader_abi(baseline, changed)
    assert bridge_compare(baseline.physical_dict(), changed.physical_dict())
    assert baseline.sha256 == baseline_hash and candidate.sha256 == candidate_hash


def test_missing_topology_does_not_become_proven_unknown(bridge_compare):
    baseline = parse_shader_abi(support.complete_interface()).physical_dict()
    candidate = copy.deepcopy(baseline)
    candidate.pop("outputTopology")
    assert bridge_compare(baseline, candidate) == ["outputTopology"]


@pytest.fixture
def workspace_fixture():
    fixture = support.ShaderWorkspaceAbiGateTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_legacy_rejection_remains_auditable_and_fresh_validation_recovers(workspace_fixture):
    original_interface = support.complete_interface()
    original_interface["outputTopology"] = ""
    with mock.patch.object(support, "complete_interface", return_value=original_interface):
        service, workspace, _, compiled, interface = workspace_fixture._compiled_workspace("legacy-topology")
    candidate = copy.deepcopy(interface)
    candidate["outputTopology"] = "Topology.Unknown"
    response = support.reflection_response(
        candidate, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size)
    # Produce a truthful, hash-bound report under the former exact rule.
    with mock.patch("src.shader_workspace.abi._comparison_interface", side_effect=lambda abi: abi.physical_dict()):
        rejected = service.validate_candidate_reflection(workspace, response)
    rejected_path = Path(rejected["abiReportPath"])
    rejected_bytes = rejected_path.read_bytes()
    manifest_path = Path(workspace) / "manifest.json"
    baseline = json.loads(manifest_path.read_text(encoding="utf8"))["baseline"]
    assert not service.status(workspace)["abiCompatible"]
    assert service.status(workspace)["lastAbiAttempt"]["status"] != "unavailable"
    approved = service.validate_candidate_reflection(workspace, response)
    assert approved["replayAbiValidated"]
    assert service.status(workspace)["abiCompatible"]
    assert json.loads(manifest_path.read_text(encoding="utf8"))["baseline"] == baseline
    assert rejected_path.read_bytes() == rejected_bytes

    # The sentinel compatibility must never excuse a forged stored hash.
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    manifest["baseline"]["abiSha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(ManifestValidationError, match="baseline ABI hash mismatch"):
        service.status(workspace)


def test_reindexed_catalog_accepts_only_topology_spelling_change(workspace_fixture):
    service, workspace, _, _, _ = workspace_fixture._compiled_workspace("catalog-topology")
    path = workspace_fixture.fixture.catalog
    catalog = json.loads(path.read_text(encoding="utf8"))
    catalog["shaders"][0]["outputTopology"] = ""
    path.write_text(json.dumps(catalog), encoding="utf8")
    manifest_path = Path(workspace) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    # A catalog rebind still has to seal the actual new catalog bytes.
    manifest["capture"]["catalogSha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    service.status(workspace)
    catalog["shaders"][0]["outputTopology"] = "Topology.TriangleList"
    path.write_text(json.dumps(catalog), encoding="utf8")
    manifest["capture"]["catalogSha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(ManifestValidationError, match="ABI no longer matches"):
        service.status(workspace)
