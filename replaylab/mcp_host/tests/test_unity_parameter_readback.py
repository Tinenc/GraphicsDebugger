"""Captured parameter acceptance requires actual typed, complete readback."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct

import pytest

from src.cbuffer_transfer import resolve_cbuffer_transfer
from src.unity_parameter_readback import verify_parameter_readback
from test_cbuffer_transfer import block, field, mapping, source


def binding(value, *, name="_Parameter", scalar="float", kind="property", target_type="float", rows=1, columns=1, elements=1):
    source_type = scalar + (f"{rows}x{columns}" if rows > 1 and columns > 1 else str(rows * columns) if rows * columns > 1 else "")
    if elements > 1:
        source_type += f"[{elements}]"
    return {"name": name, "value": value, "type": target_type, "kind": kind,
            "scalarType": scalar, "rows": rows, "columns": columns, "elements": elements,
            "valueOrder": "row-major-logical",
            "sourceIdentity": {"fieldId": "a" * 64, "physicalId": "b" * 64, "type": source_type}}


def material(item, value, property_type="Float"):
    return {"properties": [item]}, {"material": {"properties": {item["name"]: value},
                                               "propertyTypes": {item["name"]: property_type}}}


def runtime(item, value, observed_type=None):
    return {"runtimeBindings": [item]}, {"runtimeBindings": {"readback": [{
        "name": item["name"], "type": observed_type or item["type"], "scalarType": item.get("scalarType"),
        "sourceIdentity": json.dumps(item["sourceIdentity"]), "value": value,
        "scope": "gpu-buffer-data" if item["type"] == "buffer" else "renderer-property-block"}]}}


def assert_gap(request, observed, suffix):
    gaps = verify_parameter_readback(request, observed)
    assert any(row["code"] == "unity-parameter-" + suffix for row in gaps), gaps
    assert all(row["blocking"] for row in gaps)


def test_material_float_compares_float32_bits_without_numeric_tolerance():
    request, observed = material(binding(1.000000001), 1.0)
    assert verify_parameter_readback(request, observed) == []
    observed["material"]["properties"]["_Parameter"] = struct.unpack("<f", struct.pack("<I", 0x3F800001))[0]
    assert_gap(request, observed, "value-mismatch")


@pytest.mark.parametrize("wrong", [0.0, True, float("inf"), float("nan"), [0.0]])
def test_negative_zero_shape_and_nonfinite_are_not_accepted(wrong):
    request, observed = material(binding(-0.0), wrong)
    assert_gap(request, observed, "value-mismatch")
    observed["material"]["properties"]["_Parameter"] = -0.0
    assert verify_parameter_readback(request, observed) == []


@pytest.mark.parametrize("metadata", [{}, {"_Parameter": "Int"}, {"_Parameter": "Vector"}])
def test_material_requires_real_shader_property_type(metadata):
    request, observed = material(binding(1.0), 1.0)
    observed["material"]["propertyTypes"] = metadata
    assert_gap(request, observed, "type-mismatch")


@pytest.mark.parametrize("scalar,expected,actual", [
    ("uint", 0xFFFFFFFF, -1), ("uint", 0xFFFFFFFF, 0xFFFFFFFF),
    ("uint", 0x80000000, -0x80000000), ("int", -1, 0xFFFFFFFF), ("bool", True, 1)])
def test_integer_readback_preserves_exact_32_bit_storage(scalar, expected, actual):
    request, observed = runtime(binding(expected, scalar=scalar, target_type="int"), actual)
    assert verify_parameter_readback(request, observed) == []
    observed["runtimeBindings"]["readback"][0]["value"] = float(actual)
    assert_gap(request, observed, "value-mismatch")
    observed["runtimeBindings"]["readback"][0]["value"] = (actual + 1) & 0xFFFFFFFF
    assert_gap(request, observed, "value-mismatch")


def test_high_uint_material_is_not_rounded_through_float_storage():
    request, observed = material(binding(16777217, scalar="uint", target_type="int"), 16777217, "Int")
    assert verify_parameter_readback(request, observed) == []
    observed["material"]["properties"]["_Parameter"] = 16777216
    assert_gap(request, observed, "value-mismatch")
    observed["material"]["propertyTypes"]["_Parameter"] = "Float"
    assert_gap(request, observed, "type-mismatch")


def test_vectors_only_allow_unity_vector4_zero_padding():
    item = binding([1, 2, 3], target_type="vector", columns=3)
    request, observed = material(item, [1, 2, 3, 0], "Vector")
    assert verify_parameter_readback(request, observed) == []
    observed["material"]["properties"]["_Parameter"] = [1, 2, 3]
    assert_gap(request, observed, "value-mismatch")
    observed["material"]["properties"]["_Parameter"] = [1, 2, 3, 1]
    assert_gap(request, observed, "value-mismatch")


def test_vector_array_uses_exact_element_count_components_and_zero_padding():
    item = binding([1, 2, 3, 4, 5, 6], target_type="vector", kind="array", columns=3, elements=2)
    request, observed = runtime(item, [[1, 2, 3, 0], [4, 5, 6, 0]], "vector-array")
    assert verify_parameter_readback(request, observed) == []
    observed["runtimeBindings"]["readback"][0]["value"] = [1, 2, 3, 0, 4, 5, 6, 0]
    assert_gap(request, observed, "value-mismatch")
    observed["runtimeBindings"]["readback"][0]["value"] = [[1, 2, 3, 0], [4, 5, 6, 0], [0, 0, 0, 0]]
    assert_gap(request, observed, "value-mismatch")


def test_matrix_row_major_and_matrix_array_dimensions_are_exact():
    values = list(range(16))
    item = binding(values, target_type="matrix", kind="matrix", rows=4, columns=4)
    request, observed = runtime(item, values)
    assert verify_parameter_readback(request, observed) == []
    observed["runtimeBindings"]["readback"][0]["value"] = [values[column * 4 + row] for row in range(4) for column in range(4)]
    assert_gap(request, observed, "value-mismatch")
    item = binding(list(range(32)), target_type="matrix", kind="array", rows=4, columns=4, elements=2)
    request, observed = runtime(item, [list(range(16)), list(range(16, 32))], "matrix-array")
    assert verify_parameter_readback(request, observed) == []
    observed["runtimeBindings"]["readback"][0]["value"] = list(range(32))
    assert_gap(request, observed, "value-mismatch")


@pytest.mark.parametrize("item", [
    binding(list(range(9)), target_type="matrix", kind="matrix", rows=3, columns=3),
    binding([1, 2], scalar="int", target_type="vector", columns=2),
    binding([1, 2], scalar="int", target_type="int", kind="array", elements=2),
    binding(1.0, scalar="double"), binding(1, scalar="uint", target_type="float")])
def test_unsupported_storage_is_not_excused_by_matching_numeric_values(item):
    request, observed = runtime(item, item["value"])
    assert_gap(request, observed, "request-invalid")


@pytest.mark.parametrize("change", ["sourceIdentity", "scalarType", "type", "scope", "missing", "duplicate"])
def test_runtime_requires_matching_source_and_typed_observation(change):
    request, observed = runtime(binding(2.0), 2.0)
    rows = observed["runtimeBindings"]["readback"]
    if change == "missing":
        rows.clear()
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows[0][change] = "other"
    assert verify_parameter_readback(request, observed)


def captured_buffer(payload):
    return {"name": "_Data", "type": "buffer", "stride": 4,
            "bufferBase64": base64.b64encode(payload).decode(),
            "sourceIdentity": {"resourceId": "42", "byteSize": len(payload), "capturePhase": "before-action",
                               "sha256": hashlib.sha256(payload).hexdigest()}}


def buffer_value(payload):
    return {"byteLength": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "hashCoversAllBytes": True, "previewWords": list(struct.unpack("<16I", payload[:64]))}


def test_buffer_compares_all_bytes_not_preview_and_requires_full_hash_claim():
    payload = bytes(range(128))
    item = captured_buffer(payload)
    request, observed = runtime(item, buffer_value(payload))
    assert verify_parameter_readback(request, observed) == []
    changed = payload[:-1] + bytes([payload[-1] ^ 1])
    observed["runtimeBindings"]["readback"][0]["value"] = buffer_value(changed)
    assert observed["runtimeBindings"]["readback"][0]["value"]["previewWords"] == buffer_value(payload)["previewWords"]
    assert_gap(request, observed, "value-mismatch")
    observed["runtimeBindings"]["readback"][0]["value"] = {**buffer_value(payload), "hashCoversAllBytes": False}
    assert_gap(request, observed, "value-mismatch")
    item["bufferBase64"] = base64.b64encode(changed).decode()
    assert_gap(request, observed, "request-invalid")


def test_real_cbuffer_transfer_request_has_supported_authoritative_shape(tmp_path):
    fields = [field("Tint"), field("Weights", 16, type="float2", columns=2, elements=2, arrayByteStride=16, byteSize=32)]
    raw = struct.pack("<12f", .1, .2, 3, 4, 5, 6, 0, 0, 7, 8, 0, 0)
    root, bundle = source(tmp_path, [block(fields, byteSize=48)], raw=raw)
    request = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping("Tint"), mapping("Weights", target="_Weights")]}, root)
    assert request["coverage"]["complete"]
    tint, weights = request["properties"][0], request["runtimeBindings"][0]
    _, observed = runtime(weights, [[5, 6, 0, 0], [7, 8, 0, 0]], "vector-array")
    observed["material"] = {"properties": {"_Color": [.1, .2, 3, 4]}, "propertyTypes": {"_Color": "Vector"}}
    assert verify_parameter_readback(request, observed) == []
    observed["material"]["properties"]["_Color"][0] = .11
    assert_gap(request, observed, "value-mismatch")


def test_real_raw_cbuffer_preserves_64bit_bytes(tmp_path):
    payload = struct.pack("<d", 1.0000000000000002)
    root, bundle = source(tmp_path, [block([field("Precise", type="double", baseType="VarType.Double", rows=1, columns=1, byteSize=8)])], raw=payload)
    request = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping("Precise", binding="raw-buffer")]}, root)
    item = request["runtimeBindings"][0]
    _, observed = runtime(item, {"byteLength": 8, "sha256": hashlib.sha256(payload).hexdigest(), "hashCoversAllBytes": True})
    assert verify_parameter_readback(request, observed) == []
    item["value"] = 1.0
    assert_gap(request, observed, "request-invalid")


def test_system_and_extra_non_capture_bindings_are_outside_this_validator():
    item = binding([1] * 16, kind="system", target_type="matrix", rows=4, columns=4)
    request = {"properties": [{"name": "_Custom", "value": 999, "type": "float"}],
               "runtimeBindings": [item, {"name": "_Additional", "type": "vector", "value": [1, 2]}]}
    assert verify_parameter_readback(request, {}) == []


def test_authoritative_material_cannot_be_shadowed_by_custom_renderer_override():
    item = binding(2.0)
    request, observed = material(item, 2.0)
    observed["runtimeBindings"] = {"readback": [{"name": item["name"], "value": 999}]}
    assert_gap(request, observed, "shadowed")


def test_malformed_or_missing_material_readback_is_blocking():
    request, observed = material(binding(1.0), 1.0)
    observed["material"] = []
    assert_gap(request, observed, "missing")


def test_effect_typed_upload_is_distinct_from_material_and_buffer_readback():
    request, observed = runtime(binding([1, 2, 3], target_type="vector", columns=3), [1, 2, 3, 0])
    request["executionKind"] = observed["executionKind"] = "effect"
    observed["effect"] = {"kind": "executable-effect", "uploadsVerified": True, "error": None}
    row = observed["runtimeBindings"]["readback"][0]
    row.update(scope="compute-upload", uploadVerified=True)
    assert verify_parameter_readback(request, observed) == []
    row["value"][0] = 9
    assert_gap(request, observed, "value-mismatch")
    row["value"][0] = 1
    row["uploadVerified"] = False
    assert_gap(request, observed, "upload-unverified")
    row["uploadVerified"] = True
    observed["effect"]["error"] = "kernel unavailable"
    assert_gap(request, observed, "type-mismatch")
    observed["effect"]["error"] = None
    request["executionKind"] = "draw"
    assert_gap(request, observed, "type-mismatch")


def test_compute_effect_buffer_still_requires_actual_complete_byte_readback():
    payload = bytes(range(128))
    request, observed = runtime(captured_buffer(payload), buffer_value(payload))
    request["executionKind"] = observed["executionKind"] = "effect"
    observed["effect"] = {"kind": "executable-effect", "uploadsVerified": True}
    assert verify_parameter_readback(request, observed) == []
    row = observed["runtimeBindings"]["readback"][0]
    row.update(scope="compute-upload", uploadVerified=True)
    assert_gap(request, observed, "type-mismatch")


@pytest.mark.parametrize("kind", ["append", "counter"])
def test_buffer_counter_initialization_needs_actual_readback_and_explicit_provenance(kind):
    payload = bytes(range(128))
    item = {**captured_buffer(payload), "bufferType": kind, "initialCounter": 3,
            "counterProvenance": {"kind": "inferred", "reason": "three valid entries, original hidden counter unavailable"}}
    request, observed = runtime(item, {**buffer_value(payload), "counter": 3})
    assert verify_parameter_readback(request, observed) == []
    observed["runtimeBindings"]["readback"][0]["value"]["counter"] = 0
    assert_gap(request, observed, "value-mismatch")
    item["initialCounter"] = 100
    assert_gap(request, observed, "request-invalid")
    item["initialCounter"] = 3
    item.pop("counterProvenance")
    assert_gap(request, observed, "request-invalid")
