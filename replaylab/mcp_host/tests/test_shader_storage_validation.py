from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace as NS

import pytest
from tests.test_shader_numeric_validation import FakeBridge, native_blob, validation

PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/shader_storage_validation.py"
spec = importlib.util.spec_from_file_location("bread_shader_storage_validation_test", PATH)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


def instruction(opcode, *args):
    return [((len(args) + 1) << 16) | opcode] + list(args)


def module(readonly=True, direct=False, extra=None):
    words = [0x07230203, 0x10300, 0, 100, 0]
    words += instruction(71, 25, 34, 0) + instruction(71, 25, 33, 40)
    if readonly:
        words += instruction(71, 25, 24) if direct else instruction(72, 24, 0, 24)
    words += instruction(29, 23, 1) + instruction(30, 24, 23)
    words += instruction(32, 26, 2, 24) + instruction(59, 26, 25, 2)
    words += extra or []
    return struct.pack("<%dI" % len(words), *words)


@pytest.mark.parametrize("direct", [False, True])
def test_native_nonwritable_decoration_proves_storage_binding(direct):
    assert storage.spirv_readonly_bindings(module(direct=direct)) == {(0, 40)}
    assert storage.spirv_readonly_bindings(module(False)) == set()


@pytest.mark.parametrize("raw", [b"readonly buffer is only source text", module()[:-1],
    module(extra=instruction(73, 98)), module(extra=instruction(72, 24, 0, 24, 7))])
def test_unrecognized_or_contradictory_binary_never_proves_readonly(raw):
    assert storage.spirv_readonly_bindings(raw) == set()


def test_one_undecorated_member_or_alias_prevents_readonly_proof():
    raw = module().replace(struct.pack("<3I", (3 << 16) | 30, 24, 23),
                           struct.pack("<4I", (4 << 16) | 30, 24, 23, 1))
    assert storage.spirv_readonly_bindings(raw) == set()
    alias = (instruction(71, 50, 34, 0) + instruction(71, 50, 33, 40)
             + instruction(30, 51, 1) + instruction(32, 52, 2, 51)
             + instruction(59, 52, 50, 2))
    assert storage.spirv_readonly_bindings(module(extra=alias)) == set()


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(validation, "_storage_module", lambda: storage)
    bridge = FakeBridge()
    bridge.rw = NS(fixedBindSetOrSpace=0, fixedBindNumber=40, bindArraySize=1)
    bridge.original_blob = native_blob("original")
    def live(*args, **kwargs):
        bridge.ctrl.event_id = 50
        return {"reflection": NS(raw=bridge.original_blob, readWriteResources=[bridge.rw]),
                "pipelineResource": 3}
    bridge._validate_live_shader_event = live
    bridge.ctrl.GetShader = lambda unused, target, entry: NS(raw=bridge.ctrl.targets[target], readWriteResources=[bridge.rw])
    pipeline = bridge.ctrl.GetPipelineState()
    pipeline.GetReadWriteResources = lambda stage, only_used: [NS(access=NS(index=0, arrayElement=0), descriptor=NS(resource=100))]
    bridge.ctrl.GetPipelineState = lambda: pipeline
    bridge.ctrl.GetBuffers = lambda: [NS(resourceId=100, length=4)]
    bridge.buffer_reads = []
    def read(resource, offset, size):
        bridge.buffer_reads.append(bridge.ctrl.mapping)
        return b"abcd"
    bridge.ctrl.GetBufferData = read
    return bridge


def use_spirv(bridge, candidate_readonly=True):
    bridge.original_blob = module()
    blobs = {"baseline": module(), "candidate": module(candidate_readonly)}
    bridge._read_shader_candidate = lambda params: (None, blobs[params["candidate_path"]], hashlib.sha256(blobs[params["candidate_path"]]).hexdigest())
    bridge.rd.ShaderEncoding.SPIRV = "SPIRV"
    bridge.params["encoding"] = "spirv"
    bridge._shader_api_token = lambda ctrl: "vulkan"


@pytest.mark.parametrize("directive", [
    "#extension GL_NV_shader_buffer_load : require",
    "#extension GL_NV_gpu_shader5 : enable",
    "#extension GL_EXT_buffer_reference2 : enable",
    "#extension GL_ARB_bindless_texture : enable",
    "#extension GL_NV_bindless_texture : require",
    "#extension all : warn",
    "#extension GL_NV_shader_buffer_\\\nload : require",
    "# /* comment */ extension GL_NV_shader_buffer_load : enable",
    '#include "address_helpers.glsl"',
])
def test_glsl_unbound_storage_cannot_hide_in_empty_binding_table(directive):
    bridge = FakeBridge()
    raw = ("#version 450\n" + directive + "\nvoid main(){}\n").encode()
    proof = storage.readonly_storage_proof(bridge, NS(raw=raw, encoding="GLSL", readWriteResources=[]))
    assert not proof["complete"] and proof["uncovered"]


def test_glsl_comments_and_disabled_extensions_do_not_invent_writes():
    raw = b"#version 450\n// #extension GL_NV_shader_buffer_load : enable\n#extension GL_ARB_bindless_texture : disable\nvoid main(){}"
    proof = storage.readonly_storage_proof(FakeBridge(), NS(raw=raw, encoding="ShaderEncoding.GLSL", readWriteResources=[]))
    assert proof["complete"]
    # The same text without native GLSL encoding cannot be accepted as bytecode.
    assert storage.readonly_storage_proof(FakeBridge(), NS(raw=raw, readWriteResources=[]))["uncovered"]


@pytest.mark.parametrize("changed", [False, True])
def test_glsl_storage_uses_full_bytes_and_a_b_a_even_with_readonly_qualifier(bridge, changed):
    bridge.rd.ShaderEncoding.GLSL = "GLSL"
    bridge.params.update(encoding="glsl", entry_point="main")
    bridge._shader_api_token = lambda ctrl: "opengl"
    bridge.original_blob = b"#version 450\nlayout(std430,binding=40) readonly buffer B{uint x;};void main(){}"
    blobs = {"baseline": bridge.original_blob, "candidate": bridge.original_blob.replace(b"readonly ", b"")}
    bridge._read_shader_candidate = lambda p: (None, blobs[p["candidate_path"]], hashlib.sha256(blobs[p["candidate_path"]]).hexdigest())
    get_live = bridge._validate_live_shader_event
    def live(*args, **kwargs):
        result = get_live(*args, **kwargs)
        result["reflection"].encoding = "GLSL"
        return result
    bridge._validate_live_shader_event = live
    bridge.ctrl.GetShader = lambda unused, target, entry: NS(raw=bridge.ctrl.targets[target], encoding="GLSL", readWriteResources=[bridge.rw])
    if changed:
        bridge.ctrl.GetBufferData = lambda *args: b"abce" if bridge.ctrl.mapping == 202 else b"abcd"
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is (not changed)
    assert result["baselineStability"]["checked"] and result["replayRestored"]
    assert result["storageValidation"]["mode"] == "full-resource-bytes"
    assert result["metrics"]["outputs"][-1]["comparisonKind"] == "byte-exact-storage"
    if not changed: assert bridge.buffer_reads == [201, 202, 201]
    assert bridge.ctrl.mapping is None and not bridge.ctrl.targets


def test_readonly_vulkan_ssbo_uses_normal_output_comparison_without_buffer_readback(bridge):
    use_spirv(bridge)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["replayRestored"]
    assert result["storageValidation"]["mode"] == "read-only-or-absent"
    assert all(p["complete"] for p in result["storageValidation"]["readOnlyProofs"])
    assert bridge.buffer_reads == []
    assert bridge.ctrl.mapping is None and not bridge.ctrl.targets


def test_real_or_unknown_writable_storage_requires_full_a_b_a(bridge):
    bridge.ctrl.ComparePixelShaderPair = lambda *args: pytest.fail("RT-only pair cannot validate storage")
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["baselineStability"]["checked"]
    assert result["storageValidation"]["outputCount"] == 1
    assert bridge.buffer_reads == [201, 202, 201]
    assert result["metrics"]["outputs"][-1]["comparisonKind"] == "byte-exact-storage"


def test_candidate_changing_readonly_to_write_gets_storage_comparison_and_rejects_difference(bridge):
    use_spirv(bridge, candidate_readonly=False)
    bridge.ctrl.GetBufferData = lambda *args: b"abce" if bridge.ctrl.mapping == 202 else b"abcd"
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["replayRestored"]
    assert result["storageValidation"]["mode"] == "full-resource-bytes"
    assert result["metrics"]["outputs"][-1]["mismatchSamples"] == [{"byteOffset": 3, "baselineValue": 100, "candidateValue": 101}]
    assert bridge.ctrl.mapping is None and not bridge.ctrl.targets


def test_nondeterministic_storage_cannot_pass_matching_a_b(bridge):
    reads = []
    def read(*args):
        reads.append(1)
        return b"abce" if len(reads) == 3 else b"abcd"
    bridge.ctrl.GetBufferData = read
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"]
    assert result["diagnosticCode"] == "baseline-replay-unstable"


def test_missing_repeat_budget_rejects_storage_instead_of_claiming_success(bridge, monkeypatch):
    monkeypatch.setattr(validation, "_baseline_repeat_permission", lambda *args: "diagnostic-budget-exhausted")
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"]
    assert result["diagnosticCode"] == "storage-baseline-repeat-unavailable"


def test_storage_limits_fail_before_readback_and_restore_targets(bridge):
    bridge.ctrl.GetBuffers = lambda: [NS(resourceId=100, length=validation.OUTPUT_LIMIT + 1)]
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "budget" in result["reason"]
    assert bridge.buffer_reads == []
    assert bridge.ctrl.mapping is None and not bridge.ctrl.targets


def test_storage_readback_failure_still_restores_state(bridge):
    bridge.ctrl.GetBufferData = lambda *args: (_ for _ in ()).throw(RuntimeError("readback failed"))
    with pytest.raises(RuntimeError, match="readback failed"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and not bridge.ctrl.targets
    assert bridge.ctrl.event_id == 17


def test_image_subresources_and_secondary_counter_are_all_observed(bridge):
    pipeline = bridge.ctrl.GetPipelineState()
    pipeline.GetReadWriteResources = lambda *args: [NS(access=NS(index=0, arrayElement=0), descriptor=NS(resource=41, secondary=100))]
    bridge.ctrl.texture.mips = 2
    bridge.ctrl.texture.arraysize = 2
    bridge.ctrl.texture.byteSize = 16
    read = []
    bridge.ctrl.GetTextureData = lambda resource, sub: read.append(sub) or b"abcd"
    result = storage.storage_outputs(bridge, bridge.ctrl, "fragment", 100, 100, [bridge.rw])
    assert [item[0]["kind"] for item in result].count("storage-image") == 4
    assert [item[0]["kind"] for item in result].count("storage-buffer") == 1
    assert read == [(0, 0, 0), (0, 1, 0), (1, 0, 0), (1, 1, 0)]


@pytest.mark.parametrize("operation", [instruction(17, 5347), instruction(14, 5348, 1), instruction(32, 91, 5349, 1), instruction(17, 5390)])
def test_unbound_addressing_cannot_use_empty_or_readonly_reflection(operation, bridge):
    bridge.original_blob = module(extra=operation)
    for resources in ([], [bridge.rw]):
        proof = storage.readonly_storage_proof(bridge, NS(raw=bridge.original_blob, readWriteResources=resources))
        assert not proof["complete"] and "resolved memory" in proof["uncovered"]
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["replayRestored"]


def test_partial_descriptor_array_feedback_cannot_pass_storage_observation(bridge):
    bridge.rw.bindArraySize = 4
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "binding-range coverage" in result["reason"]
    assert bridge.buffer_reads == []


def test_missing_static_binding_or_unbound_heap_access_cannot_pass(bridge):
    for access in (NS(index=1, arrayElement=0), NS(index=0xffffffff, arrayElement=0), NS(index=0, arrayElement=1)):
        bridge.ctrl.GetPipelineState().GetReadWriteResources = lambda *args: [NS(access=access, descriptor=NS(resource=100))]
        result = validation.compare_candidates(bridge, bridge.params)
        assert not result["accepted"] and "binding table" in result["reason"]
    bridge.ctrl.GetPipelineState().GetReadWriteResources = lambda *args: []
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "incomplete" in result["reason"]


@pytest.mark.parametrize("raw", [b"#version 460\n#extension GL_NV_shader_buffer_load: enable", b"", b"DXBC", module()[:-1]])
def test_unknown_binary_or_source_format_is_not_proof_of_absent_storage(bridge, raw):
    proof = storage.readonly_storage_proof(bridge, NS(raw=raw, readWriteResources=[]))
    assert not proof["complete"] and "bytecode" in proof["uncovered"]


def test_missing_hidden_counter_fails_and_observed_counter_difference_is_rejected(bridge):
    pipeline = bridge.ctrl.GetPipelineState()
    desc = NS(resource=100, flags=2, secondary=0)
    pipeline.GetReadWriteResources = lambda *args: [NS(access=NS(index=0, arrayElement=0), descriptor=desc)]
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "counter resource" in result["reason"]
    desc.secondary = 101
    bridge.ctrl.GetBuffers = lambda: [NS(resourceId=100, length=4), NS(resourceId=101, length=4)]
    bridge.ctrl.GetBufferData = lambda resource, *args: b"abce" if resource == 101 and bridge.ctrl.targets.get(bridge.ctrl.mapping) == native_blob("candidate") else b"abcd"
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["replayRestored"]
    assert result["metrics"]["outputs"][-1]["failedComponents"] == 1
