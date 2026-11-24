import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.material_naming import parse_material_parameters
from src.material_parameter_localization import parameter_localization, material_from_semantic_context
from src.shader_asset_semantics import build_material_semantic_context, assess_material_semantic_context


def test_all_parameters_include_defaults_missing_split_components_and_exact_binding():
    material = parse_material_parameters({"scalars": {"Enabled": 1, "Missing": 0.9327},
        "vectors": {"Scale": [3.25, 6.5], "Tint": [0.2, 0.4, 0.6, 0.8]}})
    report = parameter_localization(material, [
        {"bind": 3, "space": 2, "floats": [1, None, 3.25, 1, 6.5, 0.2, 0.4, 0.6, 0.8]},
        {"bind": 5, "space": 0, "floats": [1]}])
    rows = {row["parameter"]: row for row in report["parameters"]}
    assert report["parameterCount"] == report["accountedParameterCount"] == 4
    assert rows["Enabled"]["status"] == "ambiguous"
    assert rows["Enabled"]["components"][0]["candidateCount"] == 3
    assert rows["Missing"]["status"] == "not-found"
    assert rows["Scale"]["tupleCandidateCount"] == 0
    assert [r["locations"][0]["byteOffset"] for r in rows["Scale"]["components"]] == [8, 16]
    assert rows["Tint"]["tupleLocations"] == [{"bind": 3, "space": 2, "byteOffsets": [20, 24, 28, 32]}]
    assert not report["physicalAliasAuthority"] and not report["applied"]


def test_equal_parameters_are_ambiguous_even_at_a_unique_offset():
    report = parameter_localization(parse_material_parameters({"scalars": {"Roughness": 0.75, "Smoothness": 0.75}}),
                                    [{"bind": 4, "space": 0, "floats": [0.75]}])
    assert all(row["status"] == "ambiguous" for row in report["parameters"])
    assert report["parameters"][0]["competingParameters"] == ["Smoothness"]


def test_display_budget_does_not_hide_competing_parameter_claims():
    material = parse_material_parameters({"scalars": {
        **{"Default" + str(i): 1 for i in range(64)},
        "Roughness": 0.75, "Smoothness": 0.75}})
    report = parameter_localization(material, [{"bind": 5, "floats": [1] * 8 + [0.75]}])
    rows = {row["parameter"]: row for row in report["parameters"]}
    for name in ("Roughness", "Smoothness"):
        assert rows[name]["components"][0]["locations"] == []
        assert rows[name]["status"] == "ambiguous"
        assert rows[name]["reasonCode"] == "competing"


def test_missing_block_and_repeated_defaults_are_bounded_and_honest():
    material = parse_material_parameters({"scalars": {"Enabled": 1}})
    missing = parameter_localization(material, [])
    assert missing["parameters"][0]["status"] == "unavailable"
    report = parameter_localization(material, [{"bind": 0, "space": 0, "floats": [1] * 4096}])
    component = report["parameters"][0]["components"][0]
    assert component["candidateCount"] == 4096 and len(component["locations"]) == 8
    assert component["omittedLocationCount"] == 4088


def test_transformed_value_is_explicit_and_does_not_claim_identity():
    material = parse_material_parameters({"scalars": {"Blend": 0.75}})
    plain = parameter_localization(material, [{"bind": 5, "space": 0, "floats": [0.25]}])
    assert plain["parameters"][0]["status"] == "not-found"
    converted = parameter_localization(material, [{"bind": 5, "space": 0, "floats": [0.25]}],
                                       relations=["one-minus"])
    assert converted["parameters"][0]["components"][0]["locations"][0]["relation"] == "one-minus"
    assert converted["physicalAliasAuthority"] is False


def context(tmp_path):
    material = parse_material_parameters({"name": "Skin", "scalars": {"Roughness": 0.75},
                                          "vectors": {"Tint": [0.2, 0.4, 0.6, 1]}})
    identity = {"rawSha256": "a" * 64, "eventId": 77, "stage": "fragment", "entryPoint": "MainPS",
                "captureId": "capture-a", "captureSessionId": "session-a", "shaderResourceId": "12"}
    binding = {"assetRoot": str(tmp_path / "assets"), "assetRelativePath": "Skin.mat",
               "assetContentSha256": "b" * 64, "assetEvidenceSha256": "c" * 64}
    return build_material_semantic_context(material, identity=identity, project_id="project",
        asset_binding=binding, parameter_localization=parameter_localization(material, [
            {"bind": 5, "space": 0, "floats": [0.75, 0.2, 0.4, 0.6, 1]}]))


def test_rehydrate_dictionary_never_reuses_other_draw_offsets(tmp_path):
    record = context(tmp_path)
    assert material_from_semantic_context(record)["scalars"] == [{"name": "Roughness", "value": 0.75}]
    identity = {**record["binding"], "rawSha256": "a" * 64, "eventId": 88}
    assessed = assess_material_semantic_context(record, identity=identity, project_id="project",
                                               asset_root=str(tmp_path / "assets"))
    assert assessed["parameterLocalization"]["status"] == "refresh-current-draw"
    assert "parameters" not in assessed["parameterLocalization"]
    record["parameterDictionary"][0]["value"] = 0.1
    with pytest.raises(ValueError, match="changed"):
        material_from_semantic_context(record)


def test_retained_context_public_tool_reads_buffers_once_without_asset_reader_or_fingerprints(tmp_path, monkeypatch):
    from src import server
    record = context(tmp_path)
    calls = []
    def ipc(method, params, **unused):
        calls.append(method)
        if method == "get_constant_buffer_values":
            return {"constantBuffers": [{"bind": 5, "space": 0, "floats": [0.75]}]}
        if method == "get_bound_textures":
            return []
        raise AssertionError(method)
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc))
    monkeypatch.setattr(server, "_read_workspace_asset_resolution", lambda unused: {"materialSemanticContext": record})
    monkeypatch.setattr(server, "_shader_asset_authorization_identity", Mock(side_effect=AssertionError("no broad capture collection")))
    monkeypatch.setattr(server, "_local_shader_asset_project", lambda: (
        SimpleNamespace(), tmp_path, {"workspaceId": "project", "assetRoot": str(tmp_path / "assets")}))
    monkeypatch.setattr(server, "_shader_organization_semantic_context", lambda *args: {
        "rawSha256": "a" * 64, "entryPoint": "MainPS"})
    selected = SimpleNamespace(capture_id="capture-a", key=SimpleNamespace(raw_sha256="a" * 64, resource_id="12"))
    monkeypatch.setattr(server, "_active_capture_catalog_selection", lambda *args: (None, None, selected, None))
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {"capture": {"captureSessionId": "session-b"}})
    monkeypatch.setattr(server, "_workspace_declarations", lambda unused: {"available": False})
    writes = []
    monkeypatch.setattr(server, "_write_workspace_asset_resolution", lambda path, value: writes.append(value) or str(tmp_path / "resolution.json"))
    monkeypatch.setattr(server, "_read_material_asset", AsyncMock(side_effect=AssertionError("no reread")))
    result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
        77, workspace_path=str(tmp_path), reuse_material_context=True)))
    assert "error" not in result, result
    assert calls == ["get_constant_buffer_values", "get_bound_textures"]
    assert result["material"]["source"] == "retained-material-dictionary"
    saved = writes[0]["materialSemanticContext"]
    assert saved["parameterLocalization"]["accountedParameterCount"] == 2
    assert saved["parameterLocalization"]["binding"]["captureSessionId"] == "session-b"
    assert not result["cbufferAliasRows"]


def test_reader_inherits_exact_asset_argument_and_reuses_embedded_material_preview(monkeypatch):
    from src import server
    operation = {"name": "uex_material_shader_maps", "inputSchema": {"properties": {"asset": {"type": "string"}}}}
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [operation])
    preview = {"Type": "MaterialInstanceConstant", "Name": "Skin",
               "Properties": {"ScalarParameterValues": [{"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.75}]}}
    run = AsyncMock(return_value=json.dumps({"succeeded": True, "stdout": json.dumps({
        "materials": [{"objectName": "Skin", "materialPreview": preview}]})}))
    monkeypatch.setattr(server, "run_asset_tool_operation", run)
    result = asyncio.run(server._read_material_asset("Game/Skin.uasset", reader_operation="uex_material_shader_maps"))
    assert result["succeeded"] and result["document"] == preview
    assert run.call_args.kwargs["arguments"] == {"asset": "Game/Skin.uasset"}


def test_retained_dictionary_needs_offset_review_but_unavailable_is_valid(tmp_path):
    from src import shader_resource_naming_review as naming
    source = b"cbuffer Skin:register(b5){float roughness;}"
    naming.prepare(tmp_path, {"assetRootConfigured": True, "target": {
        "rawSha256": "a" * 64, "eventId": 77, "stage": "fragment"}})
    path = tmp_path / "ai/asset-resolution.json"
    record = context(tmp_path)
    path.write_text(json.dumps({"materialSemanticContext": record}))
    review = {"assets": {"status": "partial", "evidence": "Texture links identify Skin",
                        "usage": "Roughness and Tint guide the material functions"},
              "textures": [], "reconciliation": "Grouped known color components and retained unknown lanes"}
    with pytest.raises(ValueError, match="not localized"):
        naming.record(tmp_path, source, review)
    review["assets"]["parameter_localization"] = {"status": "reviewed",
        "evidence": "Compared both parameters; Roughness enters the highlight width, Tint remains ambiguous"}
    assert naming.record(tmp_path, source, review)["status"] == "complete"
    record["parameterLocalization"]["binding"]["eventId"] = 78
    path.write_text(json.dumps({"materialSemanticContext": record}))
    assert naming.delivery_state(tmp_path, source)["status"] == "pending"
    review["assets"]["parameter_localization"] = {"status": "unavailable",
        "evidence": "The current capture returns no readable numeric data for this block"}
    assert naming.record(tmp_path, source, review)["status"] == "complete"


def test_large_dictionary_keeps_every_parameter_within_context_bound():
    material = {"objectName": "many", "scalars": [{"name": "S" + str(i), "value": 1} for i in range(512)],
                "vectors": [{"name": "V" + str(i), "value": [1, 1, 1, 1]} for i in range(512)]}
    report = parameter_localization(material, [{"bind": 5, "space": 0, "floats": [1] * 4096}])
    saved = build_material_semantic_context(material, identity={}, project_id="project",
                                           parameter_localization=report)
    assert saved["parameterLocalization"]["accountedParameterCount"] == 1024
    assert len(saved["parameterLocalization"]["parameters"]) == 1024
    assert len(json.dumps(saved, ensure_ascii=False, separators=(",", ":")).encode()) <= 512 * 1024
