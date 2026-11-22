"""Compiled field width is independent of matching material vector values."""
import base64
import copy
import hashlib
import json
import struct

import pytest

from src.material_shader_outputs import decode_outputs, value_sequence_evidence


def test_common_sequence_keeps_physical_lanes_and_reports_relocated_window():
    rows = [{"byteOffset": 16, "components": [{"value": v} for v in [1, 0, 1, 0.5]]}]
    result = value_sequence_evidence(rows, [9] * 9 + [1, 0, 1, 0.5])
    match = result["matches"][0]
    assert (match["declaredByteOffset"], match["captureByteOffset"], match["captureRegister"]) == (16, 36, "c2.y")
    assert match["laneCount"] == 4 and match["byteDelta"] == 20
    assert result["autoApplyEligible"] is result["identityVerified"] is False


def test_common_sequence_repetition_is_ambiguous_and_does_not_promote_names():
    doc = document([(bytes([3, 0, 0]), "Float4", 0)])
    doc["materials"][0]["variants"][0]["numericParameters"][0]["Value"] = dict(zip("RGBA", [1, 0, 1, 0.5]))
    result = decode(doc, capture_values=[9, 1, 0, 1, 0.5, 9, 1, 0, 1, 0.5])
    assert result["outputs"] == [] and result["valueConsistentVariantIndexes"] == []
    evidence = result["variants"][0]["valueSequenceEvidence"]
    assert len(evidence["matches"]) == 2
    assert all(m["seedOccurrenceCount"] == 2 for m in evidence["matches"])


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), -0.0])
def test_sequence_does_not_bridge_unknown_or_different_float32_lane(missing):
    rows = [{"byteOffset": 0, "components": [{"value": v} for v in [1, 0, 1, 0.5]]}]
    assert not value_sequence_evidence(rows, [1, missing, 1, 0.5])["matches"]


def test_sequence_does_not_join_disjoint_declared_fields_or_uniform_fills():
    rows = [{"byteOffset": 0, "components": [{"value": 1}, {"value": 0}]},
            {"byteOffset": 12, "components": [{"value": 1}, {"value": 0.5}]}]
    assert not value_sequence_evidence(rows, [1, 0, 1, 0.5])["matches"]
    rows = [{"byteOffset": 0, "components": [{"value": 1}] * 4}]
    assert not value_sequence_evidence(rows, [1] * 4)["matches"]


def test_sequence_output_and_work_are_bounded():
    rows = [{"byteOffset": 0, "components": [{"value": v} for v in [1, 0, 1, 0.5]]}]
    result = value_sequence_evidence(rows, [1, 0, 1, 0.5] * 5000)
    assert result["truncated"] is True and len(result["matches"]) == 16
    assert result["comparedCaptureLanes"] == 16384


def info(name, association="GlobalParameter", index=-1):
    return {"Name": name, "Association": association, "Index": index}


def document(programs=None):
    # A vector only writes xy. Adjacent zeroes belong to separate scalar outputs.
    programs = programs or [(bytes([3, 0, 0]), "Float2", 0),
                            (bytes([3, 1, 0]), "Float1", 2),
                            (bytes([3, 2, 0]), "Float1", 3)]
    code = b"".join(p[0] for p in programs)
    headers, fields, cursor = [], [], 0
    for program, kind, offset in programs:
        headers.append({"FieldIndex": len(fields), "NumFields": 1,
                        "OpcodeOffset": cursor, "OpcodeSize": len(program)})
        cursor += len(program)
        fields.append({"Type": kind, "ComponentIndex": 0, "BufferOffset": offset})
    variant = {"shaderPlatform": "SP_PCD3D_SM6", "qualityLevel": "Epic", "featureLevel": "SM6",
               "cookedShaderMapHash": "map-a", "resourceHash": "resource-a",
               "uniformLayout": {"preshaderByteSize": 64},
               "resourceBindings": {"virtualTextureStacks": [], "textures": [[]]},
               "preshaderCode": {"base64": base64.b64encode(code).decode(), "byteSize": len(code),
                                 "sha256": hashlib.sha256(code).hexdigest(),
                                 "metadata": {"bPreshader2": False, "bPreFixup": False}},
               "numericParameters": [
                   {"ParameterInfo": info("Dimensions"), "ParameterType": "Vector",
                    "Value": dict(zip("RGBA", [70, 50, 0, 0]))},
                   {"ParameterInfo": info("Seed"), "ParameterType": "Scalar", "Value": 0},
                   {"ParameterInfo": info("Threshold"), "ParameterType": "Scalar", "Value": 0}],
               "preshaderHeaders": headers, "preshaderFields": fields}
    return {"schema": "bread-material-shader-evidence/v1", "reader": {"id": "uex"},
            "readShaderMaps": {"completed": True}, "coverage": {"complete": True},
            "asset": {"path": "Project/Content/M_Base.uasset"},
            "materials": [{"objectName": "M_Base", "variants": [variant]}]}


def decode(doc, **kwargs):
    return decode_outputs(doc, shader_platform="SP_PCD3D_SM6", **kwargs)


def instance(name, parent, value):
    return {"assetPath": "Project/Content/%s.uasset" % name,
            "document": [{"Type": "MaterialInstanceConstant", "Name": name,
                          "Properties": {"Parent": {"ObjectPath": "/Game/%s.%s" % (parent, parent)},
                                         "ScalarParameterValues": [{"ParameterInfo": info("Seed"), "ParameterValue": value}]}}]}


def test_float2_does_not_claim_adjacent_scalar_outputs():
    result = decode(document(), capture_values=[70, 50, 0, 0])
    assert [r["byteSize"] for r in result["outputs"]] == [8, 4, 4]
    assert [r["byteOffset"] for r in result["outputs"]] == [0, 8, 12]
    assert [c["parameterInfo"]["Name"] for r in result["outputs"] for c in r["components"]] == ["Dimensions", "Dimensions", "Seed", "Threshold"]
    assert result["outputCount"] == 3
    assert result["samePlatformMaterialRecompiled"] is result["rawBytecodeIdentityVerified"] is result["applied"] is False


def test_parent_chain_is_ordered_exact_and_leaf_override_wins():
    chain = [instance("MI_Leaf", "MI_Parent", 0.1), instance("MI_Parent", "M_Base", 3)]
    result = decode(document(), chain=chain)
    component = result["outputs"][1]["components"][0]
    assert component["value"] == struct.unpack("<f", struct.pack("<f", 0.1))[0]
    assert component["valueSource"] == "Project/Content/MI_Leaf.uasset"
    with pytest.raises(ValueError, match="parent chain"):
        decode(document(), chain=list(reversed(chain)))
    chain[0]["document"][0]["Properties"]["ScalarParameterValues"] *= 2
    with pytest.raises(ValueError, match="competing"):
        decode(document(), chain=chain)


def test_uex_qualified_parameter_association_matches_compiled_identity():
    chain = [instance("MI_Leaf", "M_Base", 0.25)]
    info_value = chain[0]["document"][0]["Properties"]["ScalarParameterValues"][0][
        "ParameterInfo"
    ]
    info_value["Association"] = (
        "EMaterialParameterAssociation::GlobalParameter"
    )
    component = decode(document(), chain=chain)["outputs"][1]["components"][0]
    assert component["value"] == struct.unpack("<f", struct.pack("<f", 0.25))[0]
    assert component["parameterInfo"]["Association"] == "GlobalParameter"


def test_same_name_different_parameter_association_is_not_overridden():
    chain = [instance("MI_Leaf", "M_Base", 9)]
    chain[0]["document"][0]["Properties"]["ScalarParameterValues"][0]["ParameterInfo"] = info("Seed", "LayerParameter", 0)
    assert decode(document(), chain=chain)["outputs"][1]["components"][0]["value"] == 0


def test_swizzle_append_and_wider_destination_zero_fill_are_explicit():
    # Vector.z + scalar, then zero-fill Float4; no names for the added zeroes.
    program = bytes([3, 0, 0, 36, 1, 2, 255, 255, 255, 3, 1, 0, 37])
    row = decode(document([(program, "Float4", 0)]))["outputs"][0]
    assert [c.get("parameterComponent") for c in row["components"]] == [2, 0, None, None]
    assert row["components"][2] == {"constant": 0.0, "value": 0.0}


def test_unknown_program_keeps_known_outputs_and_never_guesses_names():
    result = decode(document([(b"\xff", "Float4", 0), (bytes([3, 1, 0]), "Float1", 4)]))
    assert result["outputCount"] == 1 and result["outputs"][0]["byteOffset"] == 16
    assert result["variants"][0]["unknownPrograms"][0]["namesInferred"] is False


@pytest.mark.parametrize("mutation", [
    lambda v: v["preshaderCode"].update(sha256="0" * 64),
    lambda v: v["preshaderCode"].update(base64="!!"),
    lambda v: v["preshaderCode"]["metadata"].update(bPreshader2=True),
    lambda v: v["preshaderCode"]["metadata"].update(bPreFixup=True),
    lambda v: v["resourceBindings"].update(virtualTextureStacks=[{}]),
    lambda v: v["resourceBindings"].update(textures=[[], [{}]]),
])
def test_unsupported_encoding_or_corrupt_receipt_fails_closed(mutation):
    doc = document()
    mutation(doc["materials"][0]["variants"][0])
    result = decode(doc)
    assert not result["outputs"] and result["variants"][0]["status"] == "unsupported"


def test_overlapping_outputs_do_not_keep_first_header_names():
    result = decode(document([(bytes([3, 0, 0]), "Float2", 0), (bytes([3, 1, 0]), "Float1", 1)]))
    assert not result["outputs"]


def test_variants_are_compared_without_selecting_first_or_claiming_archive_identity():
    doc = document()
    variants = doc["materials"][0]["variants"]
    variants.extend([copy.deepcopy(variants[0]), copy.deepcopy(variants[0])])
    variants[0]["numericParameters"][0]["Value"]["R"] = 80
    variants[2]["qualityLevel"] = "Medium"
    result = decode(doc, capture_values=[70, 50, 0, 0])
    assert result["valueConsistentVariantIndexes"] == [1, 2]
    assert result["outputCount"] == 3
    variants[2]["numericParameters"][1]["ParameterInfo"]["Name"] = "OtherScalar"
    result = decode(doc, capture_values=[70, 50, 0, 0])
    assert result["outputCount"] == 2 and 8 not in [r["byteOffset"] for r in result["outputs"]]


def test_negative_zero_and_nonfinite_capture_fail_exact_comparison():
    for bad in (-0.0, float("nan"), float("inf"), None):
        result = decode(document(), capture_values=[70, 50, bad, 0])
        assert not result["outputs"]
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [[], {}, {"schema": "unity-material/v1"}])
def test_unknown_reader_schema_never_infers_layout(bad):
    with pytest.raises(ValueError):
        decode(bad)


def test_platform_must_be_explicit_not_inferred_from_array_order():
    with pytest.raises(ValueError, match="shader_platform"):
        decode_outputs(document())


@pytest.mark.parametrize("field", ["readShaderMaps", "coverage"])
def test_incomplete_reads_do_not_produce_mapping(field):
    doc = document()
    doc[field] = {"completed": False, "complete": False}
    with pytest.raises(ValueError, match="incomplete"):
        decode(doc)


def test_truncated_program_and_out_of_range_field_do_not_name_outputs():
    assert not decode(document([(b"\x03\x00", "Float1", 0)]))["outputs"]
    assert not decode(document([(bytes([3, 0, 0]), "Float4", 16)]))["outputs"]


def test_unknown_program_diagnostics_are_bounded():
    result = decode(document([(b"\xff", "Float1", n) for n in range(16)]))
    assert result["variants"][0]["unknownProgramCount"] == 16
    assert len(result["variants"][0]["unknownPrograms"]) == 8


@pytest.mark.parametrize("field_change", [{"Type": "Struct"}, {"ComponentIndex": 1}, {"BufferOffset": 999}])
def test_unknown_physical_field_invalidates_other_mapping_in_the_variant(field_change):
    doc = document()
    doc["materials"][0]["variants"][0]["preshaderFields"][-1].update(field_change)
    result = decode(doc)
    assert result["outputs"] == []
    assert result["variants"][0]["unknownPrograms"][0]["namesInferred"] is False
