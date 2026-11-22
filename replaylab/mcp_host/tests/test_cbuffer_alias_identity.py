from __future__ import annotations

import importlib.util
import ast
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


def _module():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "cbuffer_alias_identity.py"
    spec = importlib.util.spec_from_file_location("tested_cbuffer_alias_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_original_top_members_survive_ambiguous_physical_abi_projection():
    block = SimpleNamespace(name="cbuffer0", fixedBindNumber=3,
                            fixedBindSetOrSpace=2, byteSize=44,
                            variables=[SimpleNamespace(name="unknown", byteOffset=0)])
    # One opaque placeholder wider than any register: not a row anyone can alias
    # by offset (its byteSize does not fit a float4), but it is still one panel
    # row named "unknown" at offset 0, so it maps as itself.
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]),
        lambda variables: [{"path": "unknown", "absoluteByteOffset": 0, "byteSize": 44}],
        lambda fields, size: False,
    )[0]
    assert result["block"] == "cbuffer0"
    assert result["members"] == [{"name": "unknown", "offset": 0}]
    assert (result["bind"], result["space"]) == (3, 2)
    assert result["memberMappingKind"] == "placeholder"
    assert result["leaves"] == [{"path": "unknown", "offset": 0, "byteSize": 44}]


def test_stripped_dxil_placeholder_array_maps_every_panel_row():
    """The common case: no debug info, the block is one ``unknown[N]`` float4 array.

    The panel shows rows ``unknown[0]``.. at 16-byte strides and the user can
    alias each; the AI's renames must land on the same rows. Before this the
    array was skipped whole and every rename came back ``unmapped``.
    """

    block = SimpleNamespace(name="cbuffer1", fixedBindNumber=1,
                            fixedBindSetOrSpace=0, byteSize=64,
                            variables=[SimpleNamespace(name="unknown", byteOffset=0)])
    fields = [{"path": "unknown", "absoluteByteOffset": 0, "byteSize": 64, "elements": 4,
               "arrayByteStride": 16, "rows": 1, "columns": 4, "baseType": "float"}]
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]), lambda variables: fields,
        lambda fields, size: False,
    )[0]
    assert result["memberMappingAvailable"] is True
    assert result["memberMappingKind"] == "placeholder"
    assert result["leaves"] == [
        {"path": "unknown[%d]" % index, "offset": 16 * index, "byteSize": 16,
         "baseType": "float", "componentByteSize": 4}
        for index in range(4)]


def test_overlapping_placeholders_map_nothing():
    block = SimpleNamespace(name="cbuffer3", fixedBindNumber=3, fixedBindSetOrSpace=0,
                            byteSize=144, variables=[SimpleNamespace(name="unknown", byteOffset=0)] * 3)
    fields = [{"path": "unknown", "absoluteByteOffset": 0, "byteSize": 16, "elements": 1,
               "rows": 1, "columns": 4, "baseType": "float"} for _ in range(3)]
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]), lambda variables: fields,
        lambda fields, size: False,
    )[0]
    assert result["leaves"] == []
    assert result["memberMappingAvailable"] is False
    assert result["memberMappingKind"] == "none"


def test_only_proven_scalar_or_vector_leaves_are_exposed_for_offset_mapping():
    block = SimpleNamespace(name="Constants", fixedBindNumber=0,
                            fixedBindSetOrSpace=0, byteSize=64,
                            variables=[SimpleNamespace(name="nested", byteOffset=0)])
    fields = [{"path": "nested", "members": [
        {"path": "nested.tint", "absoluteByteOffset": 4, "byteSize": 16, "elements": 1,
         "rows": 1, "columns": 4, "baseType": "float"},
        {"path": "nested.array", "absoluteByteOffset": 32, "byteSize": 32, "elements": 2,
         "arrayByteStride": 16, "rows": 1, "columns": 4, "baseType": "float"},
        {"path": "nested.structArray", "elements": 2, "members": [
            {"path": "nested.structArray.scalar", "absoluteByteOffset": 48,
             "byteSize": 4, "elements": 1}]},
    ]}]
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]), lambda variables: fields,
        lambda fields, size: True,
    )[0]
    assert result["members"] == [{"name": "nested", "offset": 0}]
    assert result["memberMappingKind"] == "annotated"
    # Scalars/vectors map; the struct array has no stride and remains unmapped.
    assert result["leaves"] == [
        {"path": "nested.tint", "offset": 4, "byteSize": 16, "baseType": "float", "componentByteSize": 4},
        {"path": "nested.array[0]", "offset": 32, "byteSize": 16, "baseType": "float", "componentByteSize": 4},
        {"path": "nested.array[1]", "offset": 48, "byteSize": 16, "baseType": "float", "componentByteSize": 4},
    ]


def test_nested_struct_arrays_expand_actual_stride_and_indexed_paths():
    fields = [{"path": "instances", "absoluteByteOffset": 16, "elements": 2,
               "arrayByteStride": 64, "byteSize": 128, "members": [
        {"path": "instances.color", "absoluteByteOffset": 16, "byteSize": 12,
         "rows": 1, "columns": 3, "baseType": "VarType.Float"},
        {"path": "instances.layers", "absoluteByteOffset": 32, "elements": 2,
         "arrayByteStride": 16, "byteSize": 32, "members": [
            {"path": "instances.layers.weight", "absoluteByteOffset": 36, "byteSize": 4,
             "rows": 1, "columns": 1, "baseType": "VarType.UInt"}]}]}]
    leaves = _module()._leaves_for(fields, 144)
    assert [(leaf["path"], leaf["offset"]) for leaf in leaves] == [
        ("instances[0].color", 16), ("instances[0].layers[0].weight", 36),
        ("instances[0].layers[1].weight", 52), ("instances[1].color", 80),
        ("instances[1].layers[0].weight", 100), ("instances[1].layers[1].weight", 116)]
    assert leaves[1]["baseType"] == "uint"
    assert _module()._leaves_for(fields, 60) == []



@pytest.mark.parametrize("shape", [{"rows": 2, "columns": 1},
                                    {"rows": 1, "columns": 1, "bitFieldSize": 4},
                                    {"rows": 1, "columns": 1, "bitFieldOffset": 2}])
def test_semantic_view_rejects_incomplete_matrix_layout_and_bitfields_without_removing_alias_leaf(shape):
    block = SimpleNamespace(name="Constants", fixedBindNumber=0, fixedBindSetOrSpace=0,
                            byteSize=16, variables=[SimpleNamespace(name="value", byteOffset=0)])
    fields = [{"path": "value", "absoluteByteOffset": 0, "byteSize": 8,
               "baseType": "float", **shape}]
    result = _module().reflection_alias_blocks(
        SimpleNamespace(constantBlocks=[block]), lambda variables: fields, lambda fields, size: True)
    leaf = result[0]["leaves"][0]
    assert leaf["path"] == "value"
    assert leaf["semanticMappingAvailable"] is False


@pytest.mark.parametrize("row_major,expected,size", [
    (True, [0, 4, 16, 20, 32, 36], 40),
    (False, [0, 16, 4, 20, 8, 24], 28),
])
def test_matrix_array_preserves_alias_identity_and_exposes_only_actual_component_offsets(row_major, expected, size):
    fields = [{"path": "objects.basis", "absoluteByteOffset": 16, "elements": 2,
               "arrayByteStride": 64, "byteSize": 128, "rows": 3, "columns": 2,
               "matrixByteStride": 16, "rowMajor": row_major, "baseType": "float"}]
    leaves = _module()._leaves_for(fields, 144)
    assert [(leaf["path"], leaf["offset"], leaf["byteSize"]) for leaf in leaves] == [
        ("objects.basis[0]", 16, size), ("objects.basis[1]", 80, size)]
    assert all(leaf["componentOffsets"] == expected for leaf in leaves)
    assert all(leaf.get("semanticMappingAvailable", True) for leaf in leaves)


def test_matrix_last_major_padding_is_not_required_to_fit_array_or_block_extent():
    fields = [{"path": "basis", "absoluteByteOffset": 0, "byteSize": 44,
               "rows": 3, "columns": 3, "matrixByteStride": 16,
               "rowMajor": True, "baseType": "float"}]
    leaves = _module()._leaves_for(fields, 44)
    assert leaves[0]["byteSize"] == 44
    assert leaves[0]["componentOffsets"] == [0, 4, 8, 16, 20, 24, 32, 36, 40]


@pytest.mark.parametrize("extra", [{"matrixByteStride": 8}, {"matrixByteStride": 14},
                                    {"rowMajor": None}, {"baseType": "half"}])
def test_invalid_or_non_32bit_matrix_never_claims_semantic_lanes(extra):
    fields = [{"path": "basis", "absoluteByteOffset": 0, "byteSize": 64,
               "rows": 3, "columns": 3, "matrixByteStride": 16,
               "rowMajor": True, "baseType": "float", **extra}]
    leaves = _module()._leaves_for(fields, 64)
    assert leaves[0]["semanticMappingAvailable"] is False


def _wrapper(monkeypatch, *, owned=False):
    import hashlib
    package = "tested_cbuffer_alias_wrapper"
    module = types.ModuleType(package)
    module.__path__ = []
    monkeypatch.setitem(sys.modules, package, module)
    root = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"
    for name in ("cbuffer_alias_identity", "cbuffer_display_identity", "cbuffer_display_reflection"):
        spec = importlib.util.spec_from_file_location(package + "." + name, root / (name + ".py"))
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, loaded)
        spec.loader.exec_module(loaded)
    helper = sys.modules[package + ".cbuffer_display_reflection"]
    original = SimpleNamespace(entryPoint="MainPS", rawBytes=b"captured original", constantBlocks=[SimpleNamespace(
        name="cbuffer0", fixedBindNumber=0, fixedBindSetOrSpace=0, byteSize=44,
        variables=[SimpleNamespace(name="unknown", byteOffset=0)])])
    original_sha = hashlib.sha256(original.rawBytes).hexdigest()
    record = {"captureId": "a" * 64, "captureSessionId": "b" * 32,
              "captureToken": "capture-token", "expectedOriginalResourceId": 17,
              "expectedOriginalSha256": original_sha, "stageName": "fragment", "entryPoint": "MainPS",
              "originalResource": 17, "targetResource": 27, "targetResourceId": 27,
              "capturedOriginalReflection": original,
              "candidateRawSha256": hashlib.sha256(b"verified replacement").hexdigest()}
    pipeline = SimpleNamespace(reflection=original, selected=17, entry="MainPS")
    pipeline.GetShaderReflection = lambda stage: pipeline.reflection
    pipeline.GetShader = lambda stage: pipeline.selected
    pipeline.GetShaderEntryPoint = lambda stage: pipeline.entry
    controller = SimpleNamespace(GetPipelineState=lambda: pipeline, target=27 if owned else 0)
    controller.GetTargetShaderReplacement = lambda resource: controller.target
    restored, checks, reads = [], [], []
    def get_shader(pipeline, resource, entry):
        reads.append(resource)
        return original if resource == 17 else controller.candidate
    controller.GetShader = get_shader
    controller.GetShaderEntryPoints = lambda resource: ["MainPS"]
    controller.candidate = SimpleNamespace(entryPoint="MainPS", rawBytes=b"verified replacement", constantBlocks=[])
    def forbidden(*args, **kwargs):
        raise AssertionError("Display names never compile, replay, apply, compare ABI, or select EIDs")
    module.__dict__.update(
        _pipeline=pipeline, _controller=controller, _original=original, _reads=reads, _record=record,
        _shader_raw_bytes=lambda reflection: reflection.rawBytes,
        _request_capture_loaded=lambda: True, _capture_session_id="b" * 32,
        _current_capture_token=lambda: "capture-token", _shared_shader_mutation_epoch=7,
        _normalise_stage_name=lambda value: value,
        _available_shader_stages=lambda values: [("fragment", "ps")],
        _shared_shader_replacements={17: record} if owned else {}, _event_shader_overrides={},
        _symbol_capture_snapshot={}, _shader_visual_debug_manager=None,
        _invoke_on_ui_sync=lambda callback: callback(), _context_resource_ids=lambda: {17: 17},
        _require_shared_shader_ui_owner=lambda resource, owner: checks.append((resource, owner)),
        _shader_constant_fields=lambda variables: [{"path": "unknown", "byteSize": 44}],
        _constant_field_annotations_are_complete=lambda fields, size: False,
        _set_frame_event=forbidden, _validate_live_shader_event=forbidden,
        _require_capture_identity=forbidden, _physical_shader_interface_sha256=forbidden,
        _replay=lambda callback: callback(controller), _ctx=SimpleNamespace(CurPipelineState=lambda: pipeline),
        _resource_id_int=lambda value: int(value or 0), _require_healthy_replay=lambda ctrl: None,
        _shader_api_token=lambda ctrl: "d3d12",
        _known_stage_name=lambda value: getattr(value, "stage", ""),
        _exact_shader_entry=lambda entries, entry, stage: entry if entry in entries else None,
        rd=SimpleNamespace(ResourceId=lambda: 0))
    module._get_shader_cbuffer_alias_identity = lambda params: helper.read_alias_identity(module, params)
    module._get_shader_cbuffer_display_identity = lambda params: helper.read_display_identity(module, params)
    params = {"expected_capture_id": "a" * 64, "expected_capture_session_id": "b" * 32,
              "event_id": 12, "stage": "fragment", "entry_point": "MainPS",
              "expected_original_resource_id": 17, "expected_original_sha256": original_sha}
    return module.__dict__, params, restored, checks


@pytest.mark.parametrize("owned", [False, True])
def test_wrapper_reads_exact_original_identity_without_replay_or_catalog(monkeypatch, owned):
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=owned)
    result = namespace["_get_shader_cbuffer_alias_identity"](params)
    assert result["captureId"] == ("a" * 64 if owned else "")
    assert result["captureSessionId"] == "b" * 32
    assert (result["eventId"], result["stage"], result["entryPoint"], result["shaderResourceId"]) == (12, "fragment", "MainPS", 17)
    assert result["rawSha256"] == params["expected_original_sha256"]
    assert result["activeDisplayShaderRawSha256"] == result["rawSha256"]
    assert bool(result["activeReplacementShaderRawSha256"]) is owned
    assert result["blocks"][0]["members"] == [{"name": "unknown", "offset": 0}]
    assert result["blocks"][0]["memberMappingAvailable"] is False
    assert result["blocks"][0]["leaves"] == []
    assert result["replayMutation"] is result["physicalAbiValidated"] is False
    assert result["activeDisplayEphemeral"] is False
    assert namespace["_reads"] == [] and restored == []
    assert len(checks) == 2


def test_wrapper_rejects_stale_shared_owner_before_using_its_reflection(monkeypatch):
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=True)
    namespace["_shared_shader_replacements"][17]["captureSessionId"] = "d" * 32
    with pytest.raises(RuntimeError, match="different Shader or capture session"):
        namespace["_get_shader_cbuffer_alias_identity"](params)
    assert restored == [] and checks == []


def test_wrapper_rejects_capture_change_during_lookup_without_selecting_event(monkeypatch):
    namespace, params, restored, unused_checks = _wrapper(monkeypatch)
    original = namespace["_shader_raw_bytes"]
    def changed(reflection):
        namespace["_capture_session_id"] = "d" * 32
        return original(reflection)
    namespace["_shader_raw_bytes"] = changed
    with pytest.raises(RuntimeError, match="capture changed before reflection read"):
        namespace["_get_shader_cbuffer_alias_identity"](params)
    assert restored == []


def test_wrapper_returns_exact_active_display_layout_without_abi_or_replay(monkeypatch):
    import hashlib
    namespace, params, restored, unused_checks = _wrapper(monkeypatch, owned=True)
    namespace["_pipeline"].reflection = SimpleNamespace(rawBytes=b"verified replacement", constantBlocks=[SimpleNamespace(
        name="CaptureConstantsB0", fixedBindNumber=0, fixedBindSetOrSpace=0, byteSize=16,
        variables=[SimpleNamespace(name="cb0_c0_x", byteOffset=0)])])
    result = namespace["_get_shader_cbuffer_alias_identity"](params)
    assert result["blocks"][0]["block"] == "cbuffer0"
    assert result["activeDisplayBlocks"][0]["block"] == "CaptureConstantsB0"
    assert result["activeDisplayShaderRawSha256"] == hashlib.sha256(b"verified replacement").hexdigest()
    assert result["activeReplacementShaderRawSha256"] == result["activeDisplayShaderRawSha256"]
    assert namespace["_reads"] == [] and restored == []


@pytest.mark.parametrize("display_candidate", [False, True])
def test_ui_pipeline_layout_is_independent_from_get_shader_creation_reflection(monkeypatch, display_candidate):
    import hashlib
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=True)
    original = namespace["_pipeline"].reflection
    candidate = namespace["_controller"].candidate
    namespace["_pipeline"].reflection = candidate if display_candidate else original
    result = namespace["_get_shader_cbuffer_alias_identity"](params)
    assert result["activeReplacementShaderRawSha256"] == hashlib.sha256(candidate.rawBytes).hexdigest()
    assert result["activeDisplayShaderRawSha256"] == hashlib.sha256(namespace["_pipeline"].reflection.rawBytes).hexdigest()
    assert ("activeDisplayBlocks" in result) is display_candidate
    assert namespace["_reads"] == [] and restored == [] and len(checks) == 2


@pytest.mark.parametrize("owned,raw", [(False, b"verified replacement"), (True, b"foreign program"), (True, b"")])
def test_ui_layout_must_belong_to_original_or_verified_owner(monkeypatch, owned, raw):
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=owned)
    namespace["_pipeline"].reflection = SimpleNamespace(rawBytes=raw)
    with pytest.raises(RuntimeError, match="panel Shader reflection"):
        namespace["_get_shader_cbuffer_alias_identity"](params)
    assert restored == [] and len(checks) == 1


def test_owner_change_during_lookup_never_returns_a_verified_candidate(monkeypatch):
    namespace, params, restored, checks = _wrapper(monkeypatch, owned=True)
    original_check = namespace["_require_shared_shader_ui_owner"]
    def change_owner(resource, owner):
        original_check(resource, owner)
        if len(checks) == 2:
            raise RuntimeError("replacement is no longer owned")
    namespace["_require_shared_shader_ui_owner"] = change_owner
    with pytest.raises(RuntimeError, match="no longer owned"):
        namespace["_get_shader_cbuffer_alias_identity"](params)
    assert restored == []
