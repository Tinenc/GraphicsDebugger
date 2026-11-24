"""The public naming tool preserves the cooked-default adapter's evidence limits."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import server


@pytest.mark.parametrize("defaults", [True, False])
def test_public_material_summary_retains_storage_and_layout_limits(defaults, monkeypatch):
    def call(method, params, **unused):
        if method == "get_bound_textures":
            return []
        assert method == "get_constant_buffer_values"
        return {"constantBuffers": [{"bind": 5, "space": 0,
                                     "floats": [300.0, 220.0, 0.0, 0.0]}]}

    if defaults:
        document = {"Type": "Material", "Name": "M_CharacterPBR", "CachedExpressionData": {
            "RuntimeEntries": {"ParameterInfoSet": [{"Name": "MalformedScalar"}]},
            "ScalarValues": [],
            "RuntimeEntries[1]": {"ParameterInfoSet": [{"Name": "Drop Tilling&Offset"}]},
            "VectorValues": [{"R": 300.0, "G": 220.0, "B": 0.0, "A": 0.0}],
        }}
    else:
        document = {"vectors": {"Drop Tilling&Offset": [300.0, 220.0, 0.0, 0.0]}}
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
        11402, material_json=json.dumps(document))))
    assert "error" not in result, result
    assert not result["ready"] and not result["applied"]
    assert result["matchStatus"] == "ambiguous-parameter-layout"
    assert result["readinessScope"] == "automatic-proposal-payload-only"
    summary = result["material"]
    assert summary["defaultValuesOnly"] is defaults
    assert summary["gpuLayoutKnown"] is False
    assert summary["layoutEvidence"]["compiledLayoutRead"] is False
    assert summary["layoutEvidence"]["samePlatformMaterialRecompiled"] is False
    assert summary["layoutEvidence"]["semanticWidthsVerified"] is False
    assert summary["layoutEvidence"]["withheldCandidates"] == 4
    if defaults:
        assert summary["parameterStorage"] == "unreal-cached-expression-defaults"
        assert any("not CBuffer offsets" in warning["reason"] for warning in summary["warnings"])
        assert any(warning.get("field") == "ScalarValues" for warning in summary["warnings"])
    else:
        assert summary["parameterStorage"] == ""
    assert any("component width" in warning["reason"] for warning in summary["warnings"])
    layout = summary["layoutEvidence"]
    assert layout["uncertainties"][0]["code"] == (
        "parameter-output-binding-unverified"
    )
    assert "unmetAcceptance" not in layout
    assert "requiredChecks" not in layout
    assert "acceptance" not in result["resourceNaming"]
    assert "nextRead" not in layout
    assert "run_asset_tool_operation" in layout["availableCapabilities"]
    if defaults:
        assert "uex_material_shader_maps" in layout["availableCapabilities"]
        assert "decode_material_shader_outputs" in layout["availableCapabilities"]
        assert "preshader" in layout["facts"]["compiledLayoutEvidenceKind"]
        assert layout["facts"]["defaultValueOffsetIsGpuOffset"] is False
    else:
        assert "uex_material_shader_maps" not in layout["availableCapabilities"]
    assert "scalars" not in summary and "vectors" not in summary


def test_packoffset_does_not_turn_float2_and_unrelated_scalars_into_float4_aliases(tmp_path, monkeypatch):
    """Stored Trail Size Z/W can equal other fields without owning their bytes."""
    source = tmp_path / "shader.hlsl"
    original = """cbuffer Material : register(b5) {
float ExistingUserName : packoffset(c0.x);
float trailX : packoffset(c1.x);
float trailY : packoffset(c1.y);
float actualSeed : packoffset(c1.z);
float actualRandomMask : packoffset(c1.w);
};
"""
    source.write_text(original, encoding="utf-8")
    def call(method, params, **unused):
        if method == "get_bound_textures":
            return []
        return {"constantBuffers": [{"bind": 5, "space": 0,
                "floats": [0.8231471, 0, 0, 0, 80, 60, 0, 0]}]}
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(
        status=lambda unused: {"source": {"path": str(source)}}))
    monkeypatch.setattr(server, "_read_workspace_asset_resolution", lambda unused: {})
    document = {"Type": "MaterialInstanceConstant", "Name": "MI_Body", "Properties": {
        "ScalarParameterValues": [{"ParameterInfo": {"Name": "NormalIntensity"}, "ParameterValue": 0.8231471}],
        "VectorParameterValues": [{"ParameterInfo": {"Name": "Trail Size"},
                                    "ParameterValue": {"R": 80, "G": 60, "B": 0, "A": 0}}]}}
    result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
        11384, workspace_path=str(tmp_path), material_json=json.dumps(document), fill_gaps=False)))
    assert "error" not in result, result
    # Caller-supplied material JSON is useful proposal evidence, but it is not
    # the Host's content-hash-bound asset authorization.  The supported scalar
    # therefore remains inspectable without becoming an applicable mutation.
    assert not result["ready"] and not result["applied"]
    assert result["renames"] == {}
    assert result["withheldCBufferAliasRows"][0]["parameters"] == [
        "NormalIntensity"
    ]
    assert {
        item["workspaceSymbol"] for item in result["withheldRenames"]
        if "byteOffset" in item
    } == {
        "Material.ExistingUserName", "Material.trailX", "Material.trailY",
        "Material.actualSeed", "Material.actualRandomMask"}
    assert any(
        item["workspaceSymbol"] == "Material.ExistingUserName"
        for item in result["withheldRenames"]
    )
    candidates = result["constantBufferMembers"]["proposals"]
    assert all(item["memberLocationEvidence"] == "source-packoffset" for item in candidates)
    assert result["cbufferAliasRows"] == []
    layout = result["material"]["layoutEvidence"]
    assert "nextRead" not in layout
    assert "uex_material_shader_maps" in layout["availableCapabilities"]
    assert "decode_material_shader_outputs" in layout["availableCapabilities"]
    assert "preshader" in layout["facts"]["compiledLayoutEvidenceKind"]
    assert layout["uncertainties"][0]["code"] == (
        "parameter-output-binding-unverified"
    )
    assert source.read_text(encoding="utf-8") == original


def test_aligned_array_candidates_stay_out_of_automatic_rename_and_alias_payloads(monkeypatch):
    def call(method, params, **unused):
        if method == "get_bound_textures":
            return []
        return {"constantBuffers": [{"bind": 5, "space": 0,
                                     "floats": [0.8231471, 1.0, 0.6612934, 0.0]}]}
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
        11384, material_json=json.dumps({"scalars": {
            "A": 0.8231471, "MaybeRoughness": 1.0, "B": 0.6612934, "MaybeMetallic": 0.0}}))))
    assert "error" not in result, result
    candidates = result["constantBufferMembers"]["proposals"]
    assert {item["parameter"] for item in candidates if item["valueMatchEligible"]} == {"A", "B"}
    assert all(item["autoApplyEligible"] is False for item in candidates)
    assert result["constantBufferMembers"]["withheldCount"] == 4
    assert result["cbufferAliasRows"] == []
    assert result["withheldCBufferAliasRows"][0]["parameters"] == ["A", "B"]
    assert result["renames"] == {}
