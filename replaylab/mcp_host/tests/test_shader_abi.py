from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import tests.test_shader_workspace as workspace_test_support

from src.shader_workspace import (
    AbiValidationError,
    CatalogValidationError,
    ManifestValidationError,
    build_candidate_reflection_request,
    compare_shader_abi,
    list_exact_shader_family_uses,
    parse_shader_abi,
    validate_candidate_reflection_response,
)
from src.shader_workspace.catalog import ShaderKey, capture_identity


def resource_variable_type() -> dict:
    return {
        "name": "float4",
        "baseType": "VarType.Float",
        "rows": 1,
        "columns": 4,
        "elements": 1,
        "arrayByteStride": 0,
        "matrixByteStride": 0,
        "rowMajor": True,
        "flags": 0,
        "pointerTypeID": 0xFFFFFFFF,
        "byteSize": 16,
        "members": [],
    }


def complete_interface(stage: str = "fragment") -> dict:
    value = {
        "interfaceComplete": True,
        "stage": stage,
        "entryPoint": "main",
        "encoding": "ShaderEncoding.DXBC",
        "outputTopology": "Topology.Unknown",
        "dynamicLinkageInterfaceCount": 0,
        "inputSignature": [
            {
                "varName": "uv",
                "semanticName": "TEXCOORD",
                "semanticIdxName": "TEXCOORD0",
                "semanticIndex": 0,
                "regIndex": 0,
                "systemValue": "ShaderBuiltin.Undefined",
                "compType": "CompType.Float",
                "compCount": 2,
                "stream": 0,
                "regChannelMask": 3,
                "channelUsedMask": 3,
                "perPrimitiveRate": False,
                "needSemanticIndex": False,
            }
        ],
        "outputSignature": [
            {
                "varName": "color",
                "semanticName": "SV_Target",
                "semanticIdxName": "SV_Target0",
                "semanticIndex": 0,
                "regIndex": 0,
                "systemValue": "ShaderBuiltin.ColorOutput",
                "compType": "CompType.Float",
                "compCount": 4,
                "stream": 0,
                "regChannelMask": 15,
                "channelUsedMask": 15,
                "perPrimitiveRate": False,
                "needSemanticIndex": False,
            }
        ],
        "readOnlyResources": [
            {
                "name": "Albedo",
                "space": 0,
                "bind": 3,
                "arraySize": 1,
                "resourceType": "DescriptorType.ReadOnlyImage",
                "descriptorType": "DescriptorType.ReadOnlyImage",
                "textureType": "Texture2D",
                "variableType": resource_variable_type(),
                "isTexture": True,
                "hasSampler": False,
                "isInputAttachment": False,
                "isReadOnly": True,
            }
        ],
        "readWriteResources": [],
        "samplers": [
            {
                "name": "LinearSampler",
                "space": 0,
                "bind": 2,
                "arraySize": 1,
            }
        ],
        "constantBlocks": [
            {
                "name": "PerMaterial",
                "space": 0,
                "bind": 1,
                "bindArraySize": 1,
                "byteSize": 80,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "name": "Transform",
                        "path": "Transform",
                        "byteOffset": 0,
                        "relativeOffset": 0,
                        "absoluteByteOffset": 0,
                        "byteSize": 64,
                        "type": "VarType.Float4x4",
                        "baseType": "VarType.Float",
                        "rows": 4,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 16,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0xFFFFFFFF,
                    },
                    {
                        "name": "Tint",
                        "path": "Tint",
                        "byteOffset": 64,
                        "relativeOffset": 64,
                        "absoluteByteOffset": 64,
                        "byteSize": 16,
                        "type": "VarType.Float4",
                        "baseType": "VarType.Float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0xFFFFFFFF,
                    },
                ],
            }
        ],
        "threadGroupSize": [],
    }
    if stage == "compute":
        value["inputSignature"] = []
        value["outputSignature"] = []
        value["threadGroupSize"] = [8, 8, 1]
    return value


def reflection_response(interface: dict, raw_hash: str, byte_size: int) -> dict:
    return {
        "schema": "renderdoc-ultra-shader-reflection/v1",
        "schemaVersion": 1,
        "rawSha256": raw_hash,
        "reflectedRawSha256": raw_hash,
        "byteSize": byte_size,
        "stage": interface["stage"],
        "entryPoint": interface["entryPoint"],
        "reflectedEntryPoint": interface["entryPoint"],
        "encoding": "dxbc",
        "buildTargetShaderSucceeded": True,
        "reflectionSucceeded": True,
        "freeTargetResourceSucceeded": True,
        "buildMessages": "",
        "compiledInterface": interface,
    }


class ShaderFamilyCatalogTests(unittest.TestCase):
    def test_family_uses_preserve_captured_resource_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blob_dir = root / "blobs"
            blob_dir.mkdir()
            blob = workspace_test_support.make_dxbc(b"shared shader code")
            raw_hash = hashlib.sha256(blob).hexdigest()
            (blob_dir / (raw_hash + ".bin")).write_bytes(blob)
            first = ShaderKey(77, "fragment", "main", raw_hash)
            duplicate_code = ShaderKey(78, "fragment", "main", raw_hash)

            def shader_record(key: ShaderKey) -> dict:
                record = copy.deepcopy(complete_interface())
                record.update(
                    {
                        "shaderKey": key.catalog_key,
                        "resourceId": key.resource_id,
                        "shaderResourceId": key.resource_id,
                        "stage": key.stage,
                        "entry": key.entry_point,
                        "entryPoint": key.entry_point,
                        "rawPath": "blobs/%s.bin" % raw_hash,
                        "rawSize": len(blob),
                        "rawSha256": raw_hash,
                    }
                )
                return record

            def use(event_id: int, key: ShaderKey) -> dict:
                value = key.as_dict()
                value["eventId"] = event_id
                return value

            catalog = {
                "schema": "renderdoc-ultra-capture-symbols/v1",
                "schemaVersion": 1,
                "capturePath": "D:/captures/family.rdc",
                "api": "D3D11",
                "shaders": [
                    shader_record(first),
                    shader_record(duplicate_code),
                ],
                "uses": [
                    use(11, first),
                    use(12, first),
                    use(13, duplicate_code),
                ],
            }
            catalog["captureId"] = capture_identity(
                catalog, [first, duplicate_code]
            )
            path = root / "shader_catalog.v1.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")

            self.assertEqual(
                list_exact_shader_family_uses(path, first.catalog_key),
                [11, 12],
            )
            self.assertEqual(
                list_exact_shader_family_uses(path, duplicate_code.catalog_key),
                [13],
            )
            catalog["shaders"][0]["interfaceComplete"] = False
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(CatalogValidationError, "no complete compiled ABI"):
                list_exact_shader_family_uses(path, first.catalog_key)
            self.assertEqual(list_exact_shader_family_uses(
                path, first.catalog_key, require_complete_abi=False), [11, 12])
            catalog["shaders"][0]["interfaceComplete"] = True
            catalog["uses"].append(use(11, duplicate_code))
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(
                CatalogValidationError,
                "does not preserve the selected Shader family",
            ):
                list_exact_shader_family_uses(path, first.catalog_key)
            with self.assertRaisesRegex(CatalogValidationError, "does not preserve"):
                list_exact_shader_family_uses(path, first.catalog_key, require_complete_abi=False)


class ShaderAbiModelTests(unittest.TestCase):
    def test_complete_interface_is_canonical_and_ignores_display_names(self) -> None:
        baseline_value = complete_interface()
        candidate_value = copy.deepcopy(baseline_value)
        candidate_value["inputSignature"][0]["varName"] = "renamed_input"
        candidate_value["readOnlyResources"][0]["name"] = "renamed_texture"
        candidate_value["constantBlocks"][0]["name"] = "cb1"
        candidate_value["constantBlocks"][0]["fields"][0]["name"] = "_child0"
        candidate_value["constantBlocks"][0]["fields"][0]["path"] = "_child0"

        baseline = parse_shader_abi(baseline_value)
        candidate = parse_shader_abi(candidate_value)
        comparison = compare_shader_abi(baseline, candidate)

        self.assertTrue(comparison.compatible)
        self.assertEqual(baseline.sha256, candidate.sha256)
        self.assertEqual(baseline.to_dict()["schemaVersion"], 1)

    def test_order_does_not_change_register_mapped_abi(self) -> None:
        baseline_value = complete_interface()
        extra = copy.deepcopy(baseline_value["readOnlyResources"][0])
        extra["bind"] = 7
        baseline_value["readOnlyResources"].append(extra)
        candidate_value = copy.deepcopy(baseline_value)
        candidate_value["readOnlyResources"].reverse()

        comparison = compare_shader_abi(
            parse_shader_abi(baseline_value), parse_shader_abi(candidate_value)
        )
        self.assertTrue(comparison.compatible)

    def test_signature_used_mask_is_analysis_not_physical_abi(self) -> None:
        baseline = parse_shader_abi(complete_interface())

        for signature_name in ("inputSignature", "outputSignature"):
            with self.subTest(signature=signature_name):
                candidate_value = complete_interface()
                candidate_value[signature_name][0]["channelUsedMask"] = 0
                candidate = parse_shader_abi(candidate_value)
                comparison = compare_shader_abi(baseline, candidate)

                self.assertTrue(comparison.compatible, comparison.differences)
                self.assertEqual(baseline.sha256, candidate.sha256)

    def test_opaque_cbuffer_compares_only_verifiable_physical_range(self) -> None:
        baseline_value = complete_interface()
        baseline_value["constantBlocks"][0]["fields"] = []
        baseline = parse_shader_abi(baseline_value)

        candidate = parse_shader_abi(complete_interface())
        comparison = compare_shader_abi(baseline, candidate)
        self.assertTrue(comparison.compatible)
        self.assertNotEqual(comparison.baseline_sha256, comparison.candidate_sha256)

        changes = {
            "space": lambda block: block.update({"space": 2}),
            "binding": lambda block: block.update({"bind": 7}),
            "count": lambda block: block.update({"bindArraySize": 2}),
            "byte-range": lambda block: block.update({"byteSize": 96}),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                changed = complete_interface()
                change(changed["constantBlocks"][0])
                result = compare_shader_abi(
                    baseline, parse_shader_abi(changed)
                )
                self.assertFalse(result.compatible)

    def test_legacy_stripped_dxil_unknown_placeholders_become_opaque(self) -> None:
        value = complete_interface()
        value["constantBlocks"][0]["byteSize"] = 8
        value["constantBlocks"][0]["fields"] = [
            {
                "name": "unknown",
                "path": "unknown",
                "byteOffset": 0,
                "relativeOffset": 0,
                "absoluteByteOffset": 0,
                "byteSize": 16,
                "baseType": "VarType.UInt",
                "rows": 1,
                "columns": 4,
                "elements": 0,
                "arrayByteStride": 16,
                "matrixByteStride": 16,
                "rowMajor": True,
                "flags": 1,
                "pointerTypeID": 0xFFFFFFFF,
                "typeInfo": {
                    "name": "",
                    "baseType": "VarType.UInt",
                    "rows": 1,
                    "columns": 4,
                    "elements": 0,
                    "arrayByteStride": 16,
                    "matrixByteStride": 16,
                    "rowMajor": True,
                    "flags": 1,
                },
            },
            {
                "name": "unknown",
                "path": "unknown",
                "byteOffset": 0,
                "relativeOffset": 0,
                "absoluteByteOffset": 0,
                "byteSize": 1,
                "baseType": "VarType.UByte",
                "rows": 1,
                "columns": 1,
                "elements": 0,
                "arrayByteStride": 16,
                "matrixByteStride": 16,
                "rowMajor": True,
                "flags": 1,
                "pointerTypeID": 0xFFFFFFFF,
                "typeInfo": {
                    "name": "",
                    "baseType": "VarType.UByte",
                    "rows": 1,
                    "columns": 1,
                    "elements": 0,
                    "arrayByteStride": 16,
                    "matrixByteStride": 16,
                    "rowMajor": True,
                    "flags": 1,
                },
            },
        ]
        parsed = parse_shader_abi(value)
        self.assertEqual(8, parsed.constant_blocks[0].byte_size)
        self.assertEqual((), parsed.constant_blocks[0].fields)
        self.assertEqual(
            "ambiguous", parsed.constant_blocks[0].layout_completeness
        )
        self.assertFalse(compare_shader_abi(parsed, parsed).compatible)

        declared_opaque = copy.deepcopy(value)
        declared_opaque["constantBlocks"][0]["layoutCompleteness"] = "opaque"
        with self.assertRaises(AbiValidationError):
            parse_shader_abi(declared_opaque)

        typed_invalid = complete_interface()
        typed_invalid["constantBlocks"][0]["fields"][0]["byteSize"] = 81
        with self.assertRaises(AbiValidationError):
            parse_shader_abi(typed_invalid)

    def test_complete_unknown_cbuffer_fields_remain_strict(self) -> None:
        baseline_value = complete_interface()
        block = baseline_value["constantBlocks"][0]
        block["byteSize"] = 80
        first = copy.deepcopy(block["fields"][0])
        second = copy.deepcopy(block["fields"][0])
        for field, offset, size in ((first, 0, 64), (second, 64, 16)):
            field.update(
                {
                    "name": "unknown",
                    "path": "unknown",
                    "byteOffset": offset,
                    "relativeOffset": offset,
                    "absoluteByteOffset": offset,
                    "byteSize": size,
                    "rows": 1,
                    "columns": 4,
                    "elements": 1,
                    "arrayByteStride": size,
                    "matrixByteStride": 16,
                    "typeInfo": {
                        "name": "",
                        "baseType": "VarType.Float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": size,
                        "matrixByteStride": 16,
                        "rowMajor": False,
                        "flags": 0,
                    },
                }
            )
        block["fields"] = [first, second]
        baseline = parse_shader_abi(baseline_value)
        self.assertEqual(2, len(baseline.constant_blocks[0].fields))
        self.assertEqual(
            "complete", baseline.constant_blocks[0].layout_completeness
        )

        candidate_value = copy.deepcopy(baseline_value)
        candidate_value["constantBlocks"][0]["fields"][0].update(
            {"byteSize": 32, "arrayByteStride": 32}
        )
        candidate_value["constantBlocks"][0]["fields"][0]["typeInfo"].update(
            {"arrayByteStride": 32}
        )
        candidate_value["constantBlocks"][0]["fields"][1].update(
            {
                "byteOffset": 32,
                "relativeOffset": 32,
                "absoluteByteOffset": 32,
                "byteSize": 48,
                "arrayByteStride": 48,
            }
        )
        candidate_value["constantBlocks"][0]["fields"][1]["typeInfo"].update(
            {"arrayByteStride": 48}
        )
        comparison = compare_shader_abi(
            baseline, parse_shader_abi(candidate_value)
        )
        self.assertFalse(comparison.compatible)

    def test_partial_unknown_cbuffer_preserves_every_valid_range(self) -> None:
        baseline_value = complete_interface()
        block = baseline_value["constantBlocks"][0]
        block["byteSize"] = 80

        def unknown(offset: int, size: int, elements: int = 1) -> dict:
            return {
                "name": "unknown",
                "path": "unknown",
                "byteOffset": offset,
                "relativeOffset": offset,
                "absoluteByteOffset": offset,
                "byteSize": size,
                "type": "",
                "baseType": "VarType.UInt",
                "rows": 1,
                "columns": 1,
                "elements": elements,
                "arrayByteStride": size,
                "matrixByteStride": 0,
                "rowMajor": False,
                "flags": 0,
                "pointerTypeID": 0xFFFFFFFF,
                "typeInfo": {
                    "name": "",
                    "baseType": "VarType.UInt",
                    "rows": 1,
                    "columns": 1,
                    "elements": elements,
                    "arrayByteStride": size,
                    "matrixByteStride": 0,
                    "rowMajor": False,
                    "flags": 0,
                },
            }

        block["fields"] = [unknown(0, 32), unknown(32, 32), unknown(0, 96, 0)]
        baseline = parse_shader_abi(baseline_value)
        self.assertEqual(2, len(baseline.constant_blocks[0].fields))
        self.assertEqual(
            "ambiguous", baseline.constant_blocks[0].layout_completeness
        )

        declared_partial = copy.deepcopy(baseline_value)
        declared_partial["constantBlocks"][0]["layoutCompleteness"] = "partial"
        with self.assertRaises(AbiValidationError):
            parse_shader_abi(declared_partial)

        candidate_value = copy.deepcopy(baseline_value)
        candidate_value["constantBlocks"][0]["fields"] = [
            unknown(0, 16),
            unknown(16, 64),
            unknown(0, 96, 0),
        ]
        comparison = compare_shader_abi(
            baseline, parse_shader_abi(candidate_value)
        )
        self.assertFalse(comparison.compatible)

    def test_typed_cbuffer_cannot_be_replaced_by_opaque_layout(self) -> None:
        baseline = parse_shader_abi(complete_interface())
        candidate_value = complete_interface()
        candidate_value["constantBlocks"][0]["fields"] = []
        comparison = compare_shader_abi(
            baseline, parse_shader_abi(candidate_value)
        )
        self.assertFalse(comparison.compatible)
        self.assertTrue(
            any("constantBlocks" in item["path"] for item in comparison.differences)
        )

    def test_typed_cbuffer_keeps_recursive_layout_comparison(self) -> None:
        baseline_value = complete_interface()
        parent = baseline_value["constantBlocks"][0]["fields"][0]
        parent.update(
            {
                "baseType": "VarType.Struct",
                "rows": 1,
                "columns": 1,
                "matrixByteStride": 0,
                "members": [
                    {
                        "name": "Child",
                        "path": "Transform.Child",
                        "byteOffset": 0,
                        "relativeOffset": 0,
                        "absoluteByteOffset": 0,
                        "byteSize": 16,
                        "baseType": "VarType.Float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0xFFFFFFFF,
                    }
                ],
            }
        )
        candidate_value = copy.deepcopy(baseline_value)
        candidate_value["constantBlocks"][0]["fields"][0]["members"][0][
            "rowMajor"
        ] = True

        comparison = compare_shader_abi(
            parse_shader_abi(baseline_value), parse_shader_abi(candidate_value)
        )
        self.assertFalse(comparison.compatible)
        self.assertTrue(
            any("members" in item["path"] for item in comparison.differences)
        )

    def test_annotated_cbuffer_ranges_must_not_overlap(self) -> None:
        value = complete_interface()
        value["constantBlocks"][0]["fields"][1].update(
            {
                "byteOffset": 0,
                "relativeOffset": 0,
                "absoluteByteOffset": 0,
            }
        )
        with self.assertRaisesRegex(AbiValidationError, "overlapping"):
            parse_shader_abi(value)

        outside = complete_interface()
        outside["constantBlocks"][0]["fields"][1].update(
            {
                "byteOffset": 72,
                "relativeOffset": 72,
                "absoluteByteOffset": 72,
            }
        )
        with self.assertRaisesRegex(AbiValidationError, "outside"):
            parse_shader_abi(outside)

    def test_bridge_type_info_shape_is_cross_checked(self) -> None:
        value = complete_interface()
        field = value["constantBlocks"][0]["fields"][0]
        flags = field.pop("flags")
        field["typeInfo"] = {
            "name": "",
            "baseType": field["baseType"],
            "rows": field["rows"],
            "columns": field["columns"],
            "elements": field["elements"],
            "arrayByteStride": field["arrayByteStride"],
            "matrixByteStride": field["matrixByteStride"],
            "rowMajor": field["rowMajor"],
            "flags": flags,
            "flagsText": "NoFlags",
        }
        parsed = parse_shader_abi(value)
        self.assertEqual(parsed.constant_blocks[0].fields[0].flags, 0)

        value["constantBlocks"][0]["fields"][0]["typeInfo"]["rows"] = 3
        with self.assertRaisesRegex(AbiValidationError, "contradicts"):
            parse_shader_abi(value)

    def test_every_physical_category_changes_compatibility(self) -> None:
        baseline = parse_shader_abi(complete_interface())
        changes = {
            "stage": lambda value: value.__setitem__("stage", "vertex"),
            "entry": lambda value: value.__setitem__("entryPoint", "other"),
            "input-mask": lambda value: value["inputSignature"][0].update(
                {"compCount": 1, "regChannelMask": 1, "channelUsedMask": 1}
            ),
            "output-type": lambda value: value["outputSignature"][0].update(
                {"compType": "CompType.UInt"}
            ),
            "srv-register": lambda value: value["readOnlyResources"][0].update(
                {"bind": 4}
            ),
            "uav": lambda value: value["readWriteResources"].append(
                {
                    "space": 0,
                    "bind": 0,
                    "arraySize": 1,
                    "resourceType": "DescriptorType.ReadWriteImage",
                    "descriptorType": "DescriptorType.ReadWriteImage",
                    "textureType": "Texture2D",
                    "variableType": resource_variable_type(),
                    "isTexture": True,
                    "hasSampler": False,
                    "isInputAttachment": False,
                    "isReadOnly": False,
                }
            ),
            "sampler-space": lambda value: value["samplers"][0].update(
                {"space": 1}
            ),
            "cb-register": lambda value: value["constantBlocks"][0].update(
                {"bind": 5}
            ),
            "cb-field-offset": lambda value: (
                value["constantBlocks"][0]["fields"][0].update(
                    {
                        "byteOffset": 16,
                        "relativeOffset": 16,
                        "absoluteByteOffset": 16,
                    }
                ),
                value["constantBlocks"][0]["fields"][1].update(
                    {
                        "byteOffset": 0,
                        "relativeOffset": 0,
                        "absoluteByteOffset": 0,
                    }
                ),
            ),
            "cb-matrix-stride": lambda value: value["constantBlocks"][0][
                "fields"
            ][0].update({"matrixByteStride": 32}),
            "cb-row-major": lambda value: value["constantBlocks"][0]["fields"][
                0
            ].update({"rowMajor": True}),
            "cb-type-flags": lambda value: value["constantBlocks"][0]["fields"][
                1
            ].update({"flags": 0x400}),
            "cb-pointer-type": lambda value: value["constantBlocks"][0]["fields"][
                1
            ].update({"pointerTypeID": 4}),
            "topology": lambda value: value.__setitem__(
                "outputTopology", "Topology.TriangleList"
            ),
            "resource-variable-type": lambda value: value[
                "readOnlyResources"
            ][0]["variableType"].update({"columns": 2, "byteSize": 8}),
            "signature-rate": lambda value: value["inputSignature"][0].update(
                {"perPrimitiveRate": True}
            ),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                candidate_value = complete_interface()
                change(candidate_value)
                candidate = parse_shader_abi(candidate_value)
                comparison = compare_shader_abi(baseline, candidate)
                self.assertFalse(comparison.compatible)
                self.assertTrue(comparison.differences)

    def test_compute_thread_group_is_exact(self) -> None:
        baseline = parse_shader_abi(complete_interface("compute"))
        candidate_value = complete_interface("compute")
        candidate_value["threadGroupSize"] = [16, 4, 1]
        candidate = parse_shader_abi(candidate_value)
        comparison = compare_shader_abi(baseline, candidate)
        self.assertFalse(comparison.compatible)
        self.assertTrue(
            any("threadGroupSize" in item["path"] for item in comparison.differences)
        )

    def test_incomplete_or_ambiguous_reflection_is_rejected(self) -> None:
        variants = []
        missing_topology = complete_interface()
        missing_topology.pop("outputTopology")
        variants.append(missing_topology)
        missing_dynamic_linkage = complete_interface()
        missing_dynamic_linkage.pop("dynamicLinkageInterfaceCount")
        variants.append(missing_dynamic_linkage)
        string_register = complete_interface()
        string_register["readOnlyResources"][0]["bind"] = "3"
        variants.append(string_register)
        conflicting_alias = complete_interface()
        conflicting_alias["constantBlocks"][0]["arraySize"] = 2
        variants.append(conflicting_alias)
        invalid_absolute = complete_interface()
        invalid_absolute["constantBlocks"][0]["fields"][1][
            "absoluteByteOffset"
        ] = 63
        variants.append(invalid_absolute)
        incomplete = complete_interface()
        incomplete["interfaceComplete"] = False
        variants.append(incomplete)

        for index, value in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaises(AbiValidationError):
                    parse_shader_abi(value)

    def test_duplicate_physical_binding_is_rejected(self) -> None:
        value = complete_interface()
        value["readOnlyResources"].append(
            copy.deepcopy(value["readOnlyResources"][0])
        )
        with self.assertRaisesRegex(AbiValidationError, "duplicate"):
            parse_shader_abi(value)


class CandidateReflectionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blob = b"exact candidate bytes"
        self.path = self.root / "candidate.dxbc"
        self.path.write_bytes(self.blob)
        self.raw_hash = hashlib.sha256(self.blob).hexdigest()
        self.baseline = parse_shader_abi(complete_interface())
        self.request = build_candidate_reflection_request(
            workspace_id="workspace-1",
            capture_id="c" * 64,
            api="GraphicsAPI.D3D11",
            candidate_path=self.path,
            candidate_sha256=self.raw_hash,
            candidate_byte_size=len(self.blob),
            candidate_kind="DXBC",
            revision=1,
            stage="pixel",
            entry_point="main",
            baseline_abi_sha256=self.baseline.sha256,
            target_profile="ps_5_0",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_request_is_bound_and_response_can_prove_exact_abi(self) -> None:
        self.assertEqual(self.request["method"], "reflect_shader_candidate")
        self.assertEqual(self.request["params"]["encoding"], "dxbc")
        self.assertEqual(
            set(self.request["params"]),
            {
                "candidate_path",
                "expected_sha256",
                "stage",
                "entry_point",
                "encoding",
                "target_profile",
            },
        )
        response = reflection_response(
            complete_interface(), self.raw_hash, len(self.blob)
        )
        candidate, comparison, evidence = validate_candidate_reflection_response(
            self.request, response, self.baseline
        )
        self.assertTrue(comparison.compatible)
        self.assertEqual(candidate.sha256, self.baseline.sha256)
        self.assertTrue(evidence["freeTargetResourceSucceeded"])

    def test_lifecycle_hash_and_interface_evidence_are_fail_closed(self) -> None:
        base = reflection_response(complete_interface(), self.raw_hash, len(self.blob))
        variants = []
        no_free = copy.deepcopy(base)
        no_free["freeTargetResourceSucceeded"] = False
        variants.append(no_free)
        wrong_reflected_hash = copy.deepcopy(base)
        wrong_reflected_hash["reflectedRawSha256"] = "0" * 64
        variants.append(wrong_reflected_hash)
        no_interface = copy.deepcopy(base)
        no_interface["compiledInterface"] = None
        variants.append(no_interface)
        wrong_entry = copy.deepcopy(base)
        wrong_entry["reflectedEntryPoint"] = "other"
        variants.append(wrong_entry)
        dynamic_linkage = copy.deepcopy(base)
        dynamic_linkage["compiledInterface"]["dynamicLinkageInterfaceCount"] = 1
        variants.append(dynamic_linkage)

        for index, value in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaises(AbiValidationError):
                    validate_candidate_reflection_response(
                        self.request, value, self.baseline
                    )

        tampered_request = copy.deepcopy(self.request)
        tampered_request["params"]["stage"] = "vertex"
        with self.assertRaisesRegex(AbiValidationError, "request binding changed"):
            validate_candidate_reflection_response(
                tampered_request, base, self.baseline
            )

    def test_well_formed_abi_mismatch_is_reported_not_accepted(self) -> None:
        changed = complete_interface()
        changed["readOnlyResources"][0]["bind"] = 9
        response = reflection_response(changed, self.raw_hash, len(self.blob))
        unused_candidate, comparison, unused_evidence = (
            validate_candidate_reflection_response(
                self.request, response, self.baseline
            )
        )
        self.assertFalse(comparison.compatible)
        self.assertTrue(comparison.differences)

    def test_request_rejects_changed_file_and_wrong_api_encoding_pair(self) -> None:
        self.path.write_bytes(b"changed")
        with self.assertRaisesRegex(AbiValidationError, "byte size changed"):
            build_candidate_reflection_request(
                workspace_id="workspace-1",
                capture_id="c" * 64,
                api="D3D11",
                candidate_path=self.path,
                candidate_sha256=self.raw_hash,
                candidate_byte_size=len(self.blob),
                candidate_kind="DXBC",
                revision=1,
                stage="fragment",
                entry_point="main",
                baseline_abi_sha256=self.baseline.sha256,
                target_profile="ps_5_0",
            )
        with self.assertRaisesRegex(AbiValidationError, "Vulkan.*native SPIR-V"):
            build_candidate_reflection_request(
                workspace_id="workspace-1",
                capture_id="c" * 64,
                api="Vulkan",
                candidate_path=self.path,
                candidate_sha256=hashlib.sha256(b"changed").hexdigest(),
                candidate_byte_size=len(b"changed"),
                candidate_kind="DXBC",
                revision=1,
                stage="fragment",
                entry_point="main",
                baseline_abi_sha256=self.baseline.sha256,
                target_profile="ps_5_0",
            )

    def test_vulkan_and_opengl_spirv_requests_preserve_environment(self) -> None:
        for api, kind, profile in (
            ("GraphicsAPI.Vulkan", "SPIRV", "spirv"),
            ("GraphicsAPI.OpenGL", "OPENGL-SPIRV", "opengl-spirv"),
        ):
            with self.subTest(api=api):
                request = build_candidate_reflection_request(
                    workspace_id="workspace-spirv",
                    capture_id="e" * 64,
                    api=api,
                    candidate_path=self.path,
                    candidate_sha256=self.raw_hash,
                    candidate_byte_size=len(self.blob),
                    candidate_kind=kind,
                    revision=3,
                    stage="compute",
                    entry_point="main",
                    baseline_abi_sha256=self.baseline.sha256,
                    target_profile=profile,
                )
                self.assertEqual(request["binding"]["api"], api.split(".")[-1].lower())
                self.assertEqual(request["binding"]["encoding"], kind.lower())
                self.assertEqual(request["binding"]["targetProfile"], profile)

        with self.assertRaisesRegex(AbiValidationError, "capture-bound SPIR-V"):
            build_candidate_reflection_request(
                workspace_id="workspace-spirv",
                capture_id="e" * 64,
                api="Vulkan",
                candidate_path=self.path,
                candidate_sha256=self.raw_hash,
                candidate_byte_size=len(self.blob),
                candidate_kind="SPIRV",
                revision=3,
                stage="compute",
                entry_point="main",
                baseline_abi_sha256=self.baseline.sha256,
                target_profile="opengl-spirv",
            )

    def test_d3d12_dxil_request_is_explicitly_scoped(self) -> None:
        request = build_candidate_reflection_request(
            workspace_id="workspace-2",
            capture_id="d" * 64,
            api="GraphicsAPI.D3D12",
            candidate_path=self.path,
            candidate_sha256=self.raw_hash,
            candidate_byte_size=len(self.blob),
            candidate_kind="DXIL",
            revision=2,
            stage="fragment",
            entry_point="main",
            baseline_abi_sha256=self.baseline.sha256,
            target_profile="ps_6_7",
        )
        self.assertEqual(request["binding"]["api"], "d3d12")
        self.assertEqual(request["binding"]["encoding"], "dxil")
        self.assertEqual(request["binding"]["targetProfile"], "ps_6_7")


class ShaderWorkspaceAbiGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = workspace_test_support.ShaderWorkspaceTests(
            methodName="test_create_decompile_compile_records_auditable_candidate"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _add_complete_catalog_interface(self) -> dict:
        catalog = json.loads(self.fixture.catalog.read_text(encoding="utf-8"))
        interface = complete_interface()
        catalog["shaders"][0].update(interface)
        self.fixture.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        return interface

    def _compiled_workspace(self, name: str):
        interface = self._add_complete_catalog_interface()
        service, workspace, created = self.fixture._create(name)
        service.decompile(workspace)
        compiled = service.compile(workspace)
        return service, workspace, created, compiled, interface

    def test_workspace_promotes_only_after_exact_replay_reflection(self) -> None:
        service, workspace, created, compiled, interface = self._compiled_workspace(
            "workspace-abi-compatible"
        )
        self.assertEqual(created["baselineAbiStatus"], "captured-reflection-complete")
        request = service.candidate_reflection_request(workspace)
        self.assertEqual(request["params"]["candidate_path"], compiled["candidateBlobPath"])
        response = reflection_response(
            interface, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size
        )

        validated = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(validated["replayAbiValidated"])
        self.assertFalse(validated["requiresReplayAbiValidation"])
        self.assertTrue(Path(validated["abiReportPath"]).is_file())

        status = service.status(workspace)
        self.assertTrue(status["replayAbiValidated"])
        self.assertFalse(status["requiresReplayAbiValidation"])
        self.assertEqual(
            status["latestCompiled"]["validationScope"],
            "replay-reflection-abi-compatible",
        )
        self.assertEqual(status["lastGood"]["revision"], 0)
        self.assertFalse(status["applied"])

        # Apply is expected to reflect freshly every time. Revalidating the
        # same revision is idempotent and never rewrites its first audit report.
        second = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(second["alreadyPersisted"])
        self.assertEqual(second["abiReportSha256"], validated["abiReportSha256"])

    def test_compatible_candidate_does_not_validate_edited_source(self) -> None:
        service, workspace, _, compiled, interface = self._compiled_workspace("abi-success-edit")
        service.validate_candidate_reflection(
            workspace, reflection_response(
                interface, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size
            )
        )
        status = service.status(workspace)
        self.assertEqual(status["abiValidationStatus"], "compatible")
        source = Path(status["source"]["path"])
        source.write_text(source.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")

        edited = service.status(workspace)
        self.assertEqual(edited["abiValidationStatus"], "source-changed")
        self.assertFalse(edited["representation"]["abiCompatible"])
        self.assertIsNone(edited["lastAbiAttempt"])
        # Existing candidate-level fields retain their documented meaning.
        self.assertTrue(edited["abiCompatible"])
        self.assertTrue(edited["replayAbiValidated"])
        self.assertEqual(edited["latestCompiled"]["abiGate"]["status"], "compatible")

        service.compile(workspace)
        recompiled = service.status(workspace)
        self.assertEqual(recompiled["abiValidationStatus"], "pending-replay-reflection")
        self.assertFalse(recompiled["abiCompatible"])
        self.assertFalse(recompiled["representation"]["abiCompatible"])
        self.assertTrue(recompiled["requiresReplayAbiValidation"])

    def test_workspace_mismatch_is_persisted_without_authorizing_candidate(self) -> None:
        service, workspace, unused_created, compiled, interface = (
            self._compiled_workspace("workspace-abi-mismatch")
        )
        changed = copy.deepcopy(interface)
        changed["constantBlocks"][0]["fields"][0]["rowMajor"] = True
        response = reflection_response(
            changed, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size
        )
        result = service.validate_candidate_reflection(workspace, response)
        self.assertFalse(result["compatible"])
        self.assertTrue(result["requiresReplayAbiValidation"])
        self.assertFalse(result["replayAbiValidated"])
        self.assertTrue(Path(result["abiReportPath"]).is_file())

        status = service.status(workspace)
        self.assertTrue(status["requiresReplayAbiValidation"])
        self.assertFalse(status["replayAbiValidated"])
        self.assertEqual(
            status["latestCompiled"]["abiGate"]["status"],
            "pending-replay-reflection",
        )
        self.assertFalse(status["abiCompatible"])
        self.assertEqual(status["abiValidationStatus"], "incompatible")
        attempt = status["lastAbiAttempt"]
        self.assertTrue(attempt["appliesToCurrentSource"])
        self.assertEqual(attempt["comparison"]["differences"], result["differences"])
        self.assertEqual(attempt["sourceSha256"], compiled["sourceSha256"])
        self.assertEqual(attempt["candidateRawSha256"], compiled["rawSha256"])
        self.assertEqual(status["lastGood"]["revision"], 0)
        self.assertFalse(status["applied"])

        before = Path(result["abiReportPath"]).read_bytes()
        repeated = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(repeated["alreadyPersisted"])
        self.assertEqual(before, Path(repeated["abiReportPath"]).read_bytes())
        varied_response = copy.deepcopy(response)
        varied_response["buildMessages"] = "A fresh replay reflection attempt"
        repeated = service.validate_candidate_reflection(workspace, varied_response)
        self.assertTrue(repeated["alreadyPersisted"])
        self.assertEqual(repeated["abiReportPath"], result["abiReportPath"])
        self.assertEqual(service.status(workspace)["historyCount"], status["historyCount"])

        # Saving changes invalidates the diagnostic for the editor, while its
        # immutable evidence still describes the previous compiled candidate.
        source = Path(status["source"]["path"])
        source.write_text(source.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")
        edited_status = service.status(workspace)
        self.assertEqual(edited_status["abiValidationStatus"], "source-changed")
        self.assertFalse(edited_status["lastAbiAttempt"]["appliesToCurrentSource"])
        recompiled = service.compile(workspace)
        # The fixture compiler emits equal bytes: revision/source binding,
        # rather than bytecode equality, determines diagnostic freshness.
        self.assertEqual(recompiled["rawSha256"], compiled["rawSha256"])
        new_status = service.status(workspace)
        self.assertIsNone(new_status["lastAbiAttempt"])
        self.assertEqual(new_status["abiValidationStatus"], "pending-replay-reflection")
        self.assertFalse(new_status["abiCompatible"])
        self.assertEqual(before, Path(result["abiReportPath"]).read_bytes())

    def test_successful_revalidation_clears_rejected_attempt(self) -> None:
        service, workspace, _, compiled, interface = self._compiled_workspace("abi-retry")
        changed = copy.deepcopy(interface)
        changed["constantBlocks"][0]["fields"][0]["rowMajor"] = True
        size = Path(compiled["candidateBlobPath"]).stat().st_size
        failed = service.validate_candidate_reflection(
            workspace, reflection_response(changed, compiled["rawSha256"], size)
        )
        service.validate_candidate_reflection(
            workspace, reflection_response(interface, compiled["rawSha256"], size)
        )
        status = service.status(workspace)
        self.assertIsNone(status["lastAbiAttempt"])
        self.assertEqual(status["abiValidationStatus"], "compatible")
        self.assertTrue(status["abiCompatible"])
        self.assertTrue(Path(failed["abiReportPath"]).is_file())

        success_report = Path(status["latestCompiled"]["abiGate"]["reportPath"])
        success_report = Path(workspace) / success_report
        success_bytes = success_report.read_bytes()
        service.validate_candidate_reflection(
            workspace, reflection_response(changed, compiled["rawSha256"], size)
        )
        rejected_status = service.status(workspace)
        self.assertEqual(rejected_status["abiValidationStatus"], "incompatible")
        self.assertFalse(rejected_status["abiCompatible"])
        self.assertFalse(rejected_status["replayAbiValidated"])
        self.assertTrue(rejected_status["requiresReplayAbiValidation"])
        self.assertEqual(success_bytes, success_report.read_bytes())
        service.validate_candidate_reflection(
            workspace, reflection_response(interface, compiled["rawSha256"], size)
        )
        self.assertTrue(service.status(workspace)["abiCompatible"])
        self.assertEqual(success_bytes, success_report.read_bytes())

    def test_rejected_abi_report_comparison_is_revalidated(self) -> None:
        service, workspace, _, compiled, interface = self._compiled_workspace("abi-reject-tamper")
        changed = copy.deepcopy(interface)
        changed["constantBlocks"][0]["fields"][0]["rowMajor"] = True
        result = service.validate_candidate_reflection(
            workspace, reflection_response(
                changed, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size
            )
        )
        report_path = Path(result["abiReportPath"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["comparison"]["compatible"] = True
        report_path.write_text(json.dumps(report), encoding="utf-8")
        manifest_path = Path(workspace) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["lastAbiAttempt"]["reportSha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        # The tampered record is diagnostic evidence only: status still answers
        # (source, candidate and gate do not depend on it), but the record is
        # reported as unreadable rather than as the verdict it claims.
        status = service.status(workspace)
        self.assertEqual(status["lastAbiAttempt"]["status"], "unavailable")
        self.assertIn("recorded comparison", status["lastAbiAttempt"]["error"])
        self.assertFalse(status["latestCompiled"]["abiCompatible"])
        with self.assertRaisesRegex(ManifestValidationError, "recorded comparison"):
            service._current_rejected_abi_attempt(
                service._workspace_root(workspace),
                json.loads(manifest_path.read_text(encoding="utf-8")))

    def test_workspace_report_tampering_is_rejected(self) -> None:
        service, workspace, unused_created, compiled, interface = (
            self._compiled_workspace("workspace-abi-report-tamper")
        )
        response = reflection_response(
            interface, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size
        )
        result = service.validate_candidate_reflection(workspace, response)
        report_path = Path(result["abiReportPath"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["comparison"]["compatible"] = False
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ManifestValidationError, "report hash mismatch"):
            service.status(workspace)

    def test_missing_baseline_interface_blocks_gate(self) -> None:
        service, workspace, created = self.fixture._create("workspace-no-baseline-abi")
        self.assertEqual(created["baselineAbiStatus"], "unavailable")
        service.decompile(workspace)
        service.compile(workspace)
        with self.assertRaisesRegex(AbiValidationError, "fail-closed"):
            service.candidate_reflection_request(workspace)

    def test_dynamic_linkage_baseline_is_explicitly_blocked(self) -> None:
        interface = self._add_complete_catalog_interface()
        catalog = json.loads(self.fixture.catalog.read_text(encoding="utf-8"))
        interface["dynamicLinkageInterfaceCount"] = 1
        catalog["shaders"][0].update(interface)
        self.fixture.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        service, workspace, unused_created = self.fixture._create(
            "workspace-dynamic-linkage"
        )
        service.decompile(workspace)
        service.compile(workspace)
        status = service.status(workspace)
        self.assertEqual(
            status["latestCompiled"]["abiGate"]["status"],
            "blocked-d3d11-dynamic-linkage",
        )
        with self.assertRaisesRegex(AbiValidationError, "not eligible"):
            service.candidate_reflection_request(workspace)

    def test_declared_but_incomplete_catalog_interface_is_rejected(self) -> None:
        catalog = json.loads(self.fixture.catalog.read_text(encoding="utf-8"))
        catalog["shaders"][0].update(complete_interface())
        catalog["shaders"][0].pop("outputTopology")
        self.fixture.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        with self.assertRaisesRegex(CatalogValidationError, "compiled interface"):
            self.fixture._create("workspace-invalid-baseline-abi")


if __name__ == "__main__":
    unittest.main()
