# The one-line strings below intentionally mirror DXC's fixed LLVM printer.
# ruff: noqa: E501

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.test_shader_abi as abi_support
import tests.test_shader_workspace as workspace_support
import tests.test_shader_workspace_routes as route_support
from src.shader_workspace import (
    AbiValidationError,
    CatalogValidationError,
    DxilAccessSignatureError,
    ManifestValidationError,
    ShaderKey,
    ShaderWorkspaceService,
    build_candidate_reflection_request,
    compare_shader_abi,
    extract_dxil_cbuffer_access_signature,
    extract_dxil_unannotated_cbuffer_bindings,
    parse_shader_abi,
    validate_candidate_reflection_response,
)
from src.shader_workspace.catalog import capture_identity, load_selected_shader
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry

REAL_SHAPE_DXIL = b'''target datalayout = "e-m:e-p:32:32-i32:32-f32:32-n8:16:32:64"
target triple = "dxil-ms-dx"
; Buffer Definitions:
; cbuffer
; {
;   [8 x i8] (type annotation not present)
; }
; cbuffer
; {
;   [7572 x i8] (type annotation not present)
; }
; cbuffer
; {
;   [528 x i8] (type annotation not present)
; }
; cbuffer
; {
;   [860 x i8] (type annotation not present)
; }
; Resource Bindings:
%dx.types.Handle = type { i8* }
%dx.types.CBufRet.i32 = type { i32, i32, i32, i32 }
%dx.types.CBufRet.f32 = type { float, float, float, float }
define void @main() {
  %18 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 0, i32 0, i1 false)
  %17 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 1, i32 1, i1 false)
  %16 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 2, i32 2, i1 false)
  %15 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 3, i32 3, i1 false)
  %20 = call %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32(i32 59, %dx.types.Handle %18, i32 0)
  %21 = extractvalue %dx.types.CBufRet.i32 %20, 0
  %22 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %17, i32 44)
  %23 = extractvalue %dx.types.CBufRet.f32 %22, 3
  %24 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %17, i32 205)
  %25 = extractvalue %dx.types.CBufRet.f32 %24, 0
  %26 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %16, i32 19)
  %27 = extractvalue %dx.types.CBufRet.f32 %26, 2
  %28 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %15, i32 14)
  %29 = extractvalue %dx.types.CBufRet.f32 %28, 3
  ret void
}
declare %dx.types.Handle @dx.op.createHandle(i32, i8, i32, i32, i1)
declare %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32(i32, %dx.types.Handle, i32)
declare %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32, %dx.types.Handle, i32)
!dx.resources = !{!4}
!4 = !{null, null, !5, null}
!5 = !{!6, !7, !8, !9}
!6 = !{i32 0, %B0* undef, !"", i32 0, i32 0, i32 1, i32 8, null}
!7 = !{i32 1, %B1* undef, !"", i32 0, i32 1, i32 1, i32 7572, null}
!8 = !{i32 2, %B2* undef, !"", i32 0, i32 2, i32 1, i32 528, null}
!9 = !{i32 3, %B3* undef, !"", i32 0, i32 3, i32 1, i32 860, null}
'''


MODERN_DXIL = b'''target triple = "dxil-ms-dx"
%dx.types.Handle = type { i8* }
%dx.types.ResBind = type { i32, i32, i32, i8 }
%dx.types.ResourceProperties = type { i32, i32 }
define void @main() {
  %100 = call %dx.types.Handle @dx.op.createHandleFromBinding(i32 217, %dx.types.ResBind { i32 7, i32 7, i32 0, i8 0 }, i32 7, i1 false)
  %101 = call %dx.types.Handle @dx.op.annotateHandle(i32 216, %dx.types.Handle %100, %dx.types.ResourceProperties { i32 2, i32 0 })
  %1 = call %dx.types.Handle @dx.op.createHandleFromBinding(i32 217, %dx.types.ResBind { i32 0, i32 0, i32 0, i8 2 }, i32 0, i1 false)
  %2 = call %dx.types.Handle @dx.op.annotateHandle(i32 216, %dx.types.Handle %1, %dx.types.ResourceProperties { i32 13, i32 8 })
  %3 = call i32 @dx.op.cbufferLoad.i32(i32 58, %dx.types.Handle %2, i32 4, i32 4)
  ret void
}
declare %dx.types.Handle @dx.op.createHandleFromBinding(i32, %dx.types.ResBind, i32, i1)
declare %dx.types.Handle @dx.op.annotateHandle(i32, %dx.types.Handle, %dx.types.ResourceProperties)
declare i32 @dx.op.cbufferLoad.i32(i32, %dx.types.Handle, i32, i32)
!dx.resources = !{!4}
!4 = !{null, null, !5, null}
!5 = !{!6}
!6 = !{i32 0, %B0* undef, !"", i32 0, i32 0, i32 1, i32 8, null}
'''


def ambiguous_interface() -> dict:
    value = abi_support.complete_interface()
    block = value["constantBlocks"][0]
    block.update(
        {
            "space": 0,
            "bind": 0,
            "bindArraySize": 1,
            "byteSize": 8,
            "fields": [],
            "layoutCompleteness": "ambiguous",
        }
    )
    return value


class DxilAccessSignatureTests(unittest.TestCase):
    def test_bounded_umin_add_indices_match_bridge_and_reject_overflow(self) -> None:
        source = REAL_SHAPE_DXIL.replace(
            b"  %22 = call", b"  %bounded = call i32 @dx.op.binary.i32(i32 40, i32 %21, i32 2)\n"
            b"  %index = add i32 %bounded, 44\n  %22 = call"
        ).replace(b"%dx.types.Handle %17, i32 44)", b"%dx.types.Handle %17, i32 %index)")
        result = extract_dxil_cbuffer_access_signature(source)
        self.assertEqual([716, 732, 748, 3280], [x.byte_offset for x in result.block(0, 1).accesses])
        bridge_path = Path(__file__).parents[2] / "renderdoc_mcp_bridge" / "dxil_access_signature.py"
        spec = importlib.util.spec_from_file_location("bridge_bounded_access", bridge_path)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        self.assertEqual(result.to_dict(), bridge.extract_dxil_cbuffer_access_signature(source).to_dict())
        variants = [
            source.replace(b"i32 %21, i32 2)", b"i32 %21, i32 4096)"),
            source.replace(b"%bounded, 44", b"%bounded, 4294967295"),
            source.replace(b"add i32 %bounded, 44", b"add nsw i32 %bounded, 2147483647"),
            source.replace(b"add i32 %bounded", b"sub i32 %bounded"),
            source.replace(b"i32 40, i32 %21", b"i32 39, i32 %21"),
        ]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(DxilAccessSignatureError):
                    extract_dxil_cbuffer_access_signature(invalid)
                with self.assertRaises(bridge.DxilAccessSignatureError):
                    bridge.extract_dxil_cbuffer_access_signature(invalid)

    def test_legacy_signature_tracks_only_extracted_lanes(self) -> None:
        signature = extract_dxil_cbuffer_access_signature(REAL_SHAPE_DXIL)
        b0 = signature.block(0, 0)
        self.assertIsNotNone(b0)
        assert b0 is not None
        self.assertEqual((0, 4), (b0.envelope_start, b0.envelope_end))
        self.assertEqual(
            [(0, 4, "i32")],
            [
                (item.byte_offset, item.byte_width, item.return_type)
                for item in b0.accesses
            ],
        )
        self.assertNotEqual(16, b0.envelope_end)

        b1 = signature.block(0, 1)
        b2 = signature.block(0, 2)
        b3 = signature.block(0, 3)
        assert b1 is not None and b2 is not None and b3 is not None
        self.assertEqual([716, 3280], [item.byte_offset for item in b1.accesses])
        self.assertEqual((312, 316), (b2.envelope_start, b2.envelope_end))
        self.assertEqual((236, 240), (b3.envelope_start, b3.envelope_end))
        self.assertEqual(
            signature.signature_sha256,
            extract_dxil_cbuffer_access_signature(REAL_SHAPE_DXIL).signature_sha256,
        )

    def test_bridge_layout_parser_uses_dxc_annotations_not_names(self) -> None:
        # The Python-3.6 Bridge parser owns this fixed-printer evidence.  The
        # result is entirely register/space/size based; reflected names are not
        # an input.
        parser_path = (
            Path(__file__).resolve().parents[2]
            / "renderdoc_mcp_bridge"
            / "dxil_access_signature.py"
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "test_dxil_layout_parser", parser_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            (
                (0, 0, 8),
                (0, 1, 7572),
                (0, 2, 528),
                (0, 3, 860),
            ),
            module.extract_dxil_unannotated_cbuffer_bindings(REAL_SHAPE_DXIL),
        )
        self.assertEqual(
            extract_dxil_unannotated_cbuffer_bindings(REAL_SHAPE_DXIL),
            module.extract_dxil_unannotated_cbuffer_bindings(REAL_SHAPE_DXIL),
        )

        wrong_size = REAL_SHAPE_DXIL.replace(
            b"[528 x i8] (type annotation not present)",
            b"[544 x i8] (type annotation not present)",
        )
        with self.assertRaisesRegex(
            module.DxilAccessSignatureError, "size contradicts"
        ):
            module.extract_dxil_unannotated_cbuffer_bindings(wrong_size)

    def test_modern_binding_annotation_and_scalar_load_are_supported(self) -> None:
        signature = extract_dxil_cbuffer_access_signature(MODERN_DXIL)
        block = signature.block(0, 0)
        assert block is not None
        self.assertEqual((4, 8), (block.envelope_start, block.envelope_end))
        self.assertEqual("i32", block.accesses[0].return_type)

        wrong_class = MODERN_DXIL.replace(
            b"%dx.types.Handle %2, i32 4, i32 4",
            b"%dx.types.Handle %101, i32 4, i32 4",
        )
        with self.assertRaisesRegex(
            DxilAccessSignatureError, "statically bound CBuffer handle"
        ):
            extract_dxil_cbuffer_access_signature(wrong_class)

    def test_hash_dynamic_offset_indirect_user_and_oob_fail_closed(self) -> None:
        with self.assertRaisesRegex(DxilAccessSignatureError, "hash mismatch"):
            extract_dxil_cbuffer_access_signature(
                REAL_SHAPE_DXIL, expected_sha256="0" * 64
            )
        dynamic = REAL_SHAPE_DXIL.replace(
            b"%dx.types.Handle %18, i32 0)",
            b"%dx.types.Handle %18, i32 %dynamic)",
            1,
        )
        with self.assertRaisesRegex(DxilAccessSignatureError, "static DXC form"):
            extract_dxil_cbuffer_access_signature(dynamic)
        indirect = REAL_SHAPE_DXIL.replace(
            b"%21 = extractvalue %dx.types.CBufRet.i32 %20, 0",
            b"call void @consume(%dx.types.CBufRet.i32 %20)",
        )
        with self.assertRaisesRegex(DxilAccessSignatureError, "non-extractvalue"):
            extract_dxil_cbuffer_access_signature(indirect)
        out_of_bounds = REAL_SHAPE_DXIL.replace(
            b"%21 = extractvalue %dx.types.CBufRet.i32 %20, 0",
            b"%21 = extractvalue %dx.types.CBufRet.i32 %20, 2",
        )
        with self.assertRaisesRegex(DxilAccessSignatureError, "envelope"):
            extract_dxil_cbuffer_access_signature(out_of_bounds)

    def test_catalog_lowers_hash_bound_unannotated_uint4_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "blobs").mkdir()
            blob = route_support.make_dxil()
            raw_hash = hashlib.sha256(blob).hexdigest()
            key = ShaderKey(528, "fragment", "main", raw_hash)
            (root / "blobs" / (raw_hash + ".bin")).write_bytes(blob)

            shader = abi_support.complete_interface()
            shader["encoding"] = "DXIL"
            block = shader["constantBlocks"][0]
            block.update({
                "name": "AnyDisplayName",
                "space": 0,
                "bind": 2,
                "bindArraySize": 1,
                "byteSize": 528,
                "fields": [{
                    "name": "AnyMemberName",
                    "path": "AnyMemberName",
                    "byteOffset": 0,
                    "relativeOffset": 0,
                    "absoluteByteOffset": 0,
                    "byteSize": 528,
                    "type": "",
                    "baseType": "VarType.UInt",
                    "rows": 1,
                    "columns": 4,
                    "elements": 33,
                    "arrayByteStride": 16,
                    "matrixByteStride": 0,
                    "rowMajor": False,
                    "flags": 0,
                    "pointerTypeID": 0xFFFFFFFF,
                    "typeInfo": {
                        "name": "",
                        "baseType": "VarType.UInt",
                        "rows": 1,
                        "columns": 4,
                        "elements": 33,
                        "arrayByteStride": 16,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                    },
                }],
                "layoutCompleteness": "complete",
                "layoutEvidence": {
                    "kind": "dxc-dxil-buffer-definition",
                    "typeAnnotation": "absent",
                    "sourceSha256": hashlib.sha256(
                        REAL_SHAPE_DXIL
                    ).hexdigest(),
                    "accessSignatureSha256": (
                        extract_dxil_cbuffer_access_signature(
                            REAL_SHAPE_DXIL
                        ).signature_sha256
                    ),
                },
            })
            shader.update({
                "shaderKey": key.catalog_key,
                "resourceId": key.resource_id,
                "shaderResourceId": key.resource_id,
                "stage": key.stage,
                "entry": key.entry_point,
                "entryPoint": key.entry_point,
                "rawPath": "blobs/%s.bin" % raw_hash,
                "rawSize": len(blob),
                "rawSha256": raw_hash,
            })
            catalog = {
                "schema": "renderdoc-ultra-capture-symbols/v1",
                "schemaVersion": 1,
                "capturePath": "D:/captures/unannotated-b2.rdc",
                "api": "D3D12",
                "shaders": [shader],
                "uses": [],
            }
            catalog["captureId"] = capture_identity(catalog, [key])
            path = root / "shader_catalog.v1.json"
            atomic_write_json(path, catalog)

            selected = load_selected_shader(path, key)
            self.assertEqual(
                "captured-reflection-dxil-access-signature-required",
                selected.abi_status,
            )
            assert selected.abi is not None
            self.assertEqual(
                "ambiguous",
                selected.abi.constant_blocks[0].layout_completeness,
            )
            # The reflected UInt4[33] shape is retained for diagnostics, but
            # it no longer authorizes replacement compatibility by itself.
            self.assertEqual("uint", selected.abi.constant_blocks[0].fields[0].base_type)
            self.assertFalse(compare_shader_abi(
                selected.abi, selected.abi
            ).compatible)

            candidate_value = copy.deepcopy(shader)
            candidate_block = candidate_value["constantBlocks"][0]
            candidate_block.pop("layoutEvidence")
            candidate_block["layoutCompleteness"] = "complete"
            candidate_block["fields"][0]["baseType"] = "VarType.Float"
            candidate_block["fields"][0]["typeInfo"][
                "baseType"
            ] = "VarType.Float"
            candidate = parse_shader_abi(candidate_value)
            access = extract_dxil_cbuffer_access_signature(REAL_SHAPE_DXIL)
            self.assertTrue(compare_shader_abi(
                selected.abi,
                candidate,
                baseline_dxil_access=access,
                candidate_dxil_access=access,
            ).compatible)

            wrong_b2_source = REAL_SHAPE_DXIL.replace(
                b"%26 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32",
                b"%26 = call %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32",
            ).replace(
                b"%27 = extractvalue %dx.types.CBufRet.f32 %26, 2",
                b"%27 = extractvalue %dx.types.CBufRet.i32 %26, 2",
            )
            wrong_b2_access = extract_dxil_cbuffer_access_signature(
                wrong_b2_source
            )
            self.assertFalse(compare_shader_abi(
                selected.abi,
                candidate,
                baseline_dxil_access=access,
                candidate_dxil_access=wrong_b2_access,
            ).compatible)

            broken = copy.deepcopy(catalog)
            broken["shaders"][0]["constantBlocks"][0]["layoutEvidence"][
                "sourceSha256"
            ] = "not-a-hash"
            atomic_write_json(path, broken)
            with self.assertRaisesRegex(
                CatalogValidationError, "layoutEvidence hashes are invalid"
            ):
                load_selected_shader(path, key)


class AmbiguousCBufferAbiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interface = ambiguous_interface()
        self.baseline = parse_shader_abi(self.interface)
        self.candidate = parse_shader_abi(copy.deepcopy(self.interface))
        self.access = extract_dxil_cbuffer_access_signature(REAL_SHAPE_DXIL)

    def _bridge_unavailable_interface(self) -> dict:
        """Mirror the exact incomplete-interface shape emitted by the Bridge."""

        value = copy.deepcopy(self.interface)
        value["interfaceComplete"] = False
        value["encoding"] = "ShaderEncoding.DXIL"
        for block in value["constantBlocks"]:
            if block.get("layoutCompleteness") == "ambiguous":
                block["layoutEvidence"] = {
                    "kind": "dxc-dxil-buffer-definition",
                    "typeAnnotation": "unavailable",
                }
        return value

    def _reflection_request(
        self,
        candidate_path: Path,
        *,
        api: str = "D3D12",
        candidate_kind: str = "DXIL",
        target_profile: str = "ps_6_0",
    ) -> tuple[dict, str]:
        raw_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        return (
            build_candidate_reflection_request(
                workspace_id="workspace-dxil",
                capture_id="d" * 64,
                api=api,
                candidate_path=candidate_path,
                candidate_sha256=raw_hash,
                candidate_byte_size=candidate_path.stat().st_size,
                candidate_kind=candidate_kind,
                revision=1,
                stage="fragment",
                entry_point="main",
                baseline_abi_sha256=self.baseline.sha256,
                target_profile=target_profile,
            ),
            raw_hash,
        )

    def _assert_reflection_fails_closed(
        self,
        request: dict,
        response: dict,
        *,
        baseline_access,
        candidate_access,
    ) -> None:
        try:
            _candidate, comparison, _evidence = (
                validate_candidate_reflection_response(
                    request,
                    response,
                    self.baseline,
                    baseline_dxil_access=baseline_access,
                    candidate_dxil_access=candidate_access,
                )
            )
        except AbiValidationError:
            return
        self.assertFalse(comparison.compatible)

    def test_ambiguous_layout_requires_two_exact_signatures(self) -> None:
        self.assertFalse(compare_shader_abi(self.baseline, self.candidate).compatible)
        self.assertFalse(
            compare_shader_abi(
                self.baseline,
                self.candidate,
                baseline_dxil_access=self.access,
            ).compatible
        )
        comparison = compare_shader_abi(
            self.baseline,
            self.candidate,
            baseline_dxil_access=self.access,
            candidate_dxil_access=self.access,
        )
        self.assertTrue(comparison.compatible)

    def test_candidate_may_drop_loads_but_not_add_or_retype_them(self) -> None:
        # Block 1 (two loads in the fixture) is ambiguous as well, like every
        # block of a stripped shipped shader.
        interface = ambiguous_interface()
        second = copy.deepcopy(interface["constantBlocks"][0])
        second.update({"bind": 1, "byteSize": 7572})
        interface["constantBlocks"].append(second)
        baseline = parse_shader_abi(interface)
        candidate = parse_shader_abi(copy.deepcopy(interface))
        # Dead-code elimination after an edit: block 1 keeps one of its two
        # loads. Every remaining load is one the capture performed.
        dropped_source = REAL_SHAPE_DXIL.replace(
            b"  %24 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32"
            b"(i32 59, %dx.types.Handle %17, i32 205)\n"
            b"  %25 = extractvalue %dx.types.CBufRet.f32 %24, 0\n",
            b"",
        )
        self.assertNotEqual(dropped_source, REAL_SHAPE_DXIL)
        dropped = extract_dxil_cbuffer_access_signature(dropped_source)
        comparison = compare_shader_abi(
            baseline,
            candidate,
            baseline_dxil_access=self.access,
            candidate_dxil_access=dropped,
        )
        self.assertTrue(comparison.compatible, comparison.differences)
        # The reverse direction (the capture is the smaller set) adds a load the
        # capture never proved and stays rejected.
        comparison = compare_shader_abi(
            baseline,
            candidate,
            baseline_dxil_access=dropped,
            candidate_dxil_access=self.access,
        )
        self.assertFalse(comparison.compatible)
        self.assertEqual(
            "access-signature-mismatch", comparison.differences[0]["reason"]
        )
        # Dropping every load of a block is not a subset proof either.
        emptied_source = dropped_source.replace(
            b"  %22 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32"
            b"(i32 59, %dx.types.Handle %17, i32 44)\n"
            b"  %23 = extractvalue %dx.types.CBufRet.f32 %22, 3\n",
            b"",
        )
        self.assertNotEqual(emptied_source, dropped_source)
        emptied = extract_dxil_cbuffer_access_signature(emptied_source)
        comparison = compare_shader_abi(
            baseline,
            candidate,
            baseline_dxil_access=self.access,
            candidate_dxil_access=emptied,
        )
        self.assertFalse(comparison.compatible)

    def test_return_type_and_reflection_envelope_must_match_exactly(self) -> None:
        changed_type_source = REAL_SHAPE_DXIL.replace(
            b"%20 = call %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32",
            b"%20 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32",
        ).replace(
            b"%21 = extractvalue %dx.types.CBufRet.i32 %20, 0",
            b"%21 = extractvalue %dx.types.CBufRet.f32 %20, 0",
        )
        changed_type = extract_dxil_cbuffer_access_signature(changed_type_source)
        comparison = compare_shader_abi(
            self.baseline,
            self.candidate,
            baseline_dxil_access=self.access,
            candidate_dxil_access=changed_type,
        )
        self.assertFalse(comparison.compatible)
        self.assertEqual(
            "access-signature-mismatch", comparison.differences[0]["reason"]
        )

        wrong_envelope = ambiguous_interface()
        wrong_envelope["constantBlocks"][0]["byteSize"] = 16
        comparison = compare_shader_abi(
            self.baseline,
            parse_shader_abi(wrong_envelope),
            baseline_dxil_access=self.access,
            candidate_dxil_access=self.access,
        )
        self.assertFalse(comparison.compatible)
        self.assertEqual(
            "access-signature-envelope-mismatch",
            comparison.differences[0]["reason"],
        )

    def test_complete_baseline_cannot_be_weakened_by_ambiguous_candidate(self) -> None:
        candidate_value = ambiguous_interface()
        baseline_value = copy.deepcopy(candidate_value)
        baseline_value["constantBlocks"][0].update(
            {
                "layoutCompleteness": "complete",
                "fields": [
                    {
                        "byteOffset": 0,
                        "byteSize": 4,
                        "baseType": "VarType.Float",
                        "rows": 1,
                        "columns": 1,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0xFFFFFFFF,
                        "members": [],
                    }
                ],
            }
        )
        comparison = compare_shader_abi(
            parse_shader_abi(baseline_value),
            parse_shader_abi(candidate_value),
            baseline_dxil_access=self.access,
            candidate_dxil_access=self.access,
        )
        self.assertFalse(comparison.compatible)
        self.assertEqual(
            "candidate-ambiguous-baseline-complete",
            comparison.differences[0]["reason"],
        )

    def test_reflection_contract_persists_physical_evidence_without_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.dxil"
            candidate_path.write_bytes(b"fixed candidate")
            raw_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            request = build_candidate_reflection_request(
                workspace_id="workspace-dxil",
                capture_id="d" * 64,
                api="D3D12",
                candidate_path=candidate_path,
                candidate_sha256=raw_hash,
                candidate_byte_size=candidate_path.stat().st_size,
                candidate_kind="DXIL",
                revision=1,
                stage="fragment",
                entry_point="main",
                baseline_abi_sha256=self.baseline.sha256,
                target_profile="ps_6_0",
            )
            response = abi_support.reflection_response(
                copy.deepcopy(self.interface),
                raw_hash,
                candidate_path.stat().st_size,
            )
            response["encoding"] = "dxil"
            _candidate, comparison, evidence = (
                validate_candidate_reflection_response(
                    request,
                    response,
                    self.baseline,
                    baseline_dxil_access=self.access,
                    candidate_dxil_access=self.access,
                )
            )
            self.assertTrue(comparison.compatible)
            proof = evidence["dxilCBufferAccess"]
            self.assertTrue(proof["doesNotReconstructFieldsOrNames"])
            self.assertEqual(
                [],
                evidence["compiledInterface"]["constantBlocks"][0]["fields"],
            )

    def test_bridge_incomplete_interface_accepts_two_exact_dxil_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.dxil"
            candidate_path.write_bytes(b"fixed candidate")
            request, raw_hash = self._reflection_request(candidate_path)
            response = abi_support.reflection_response(
                self._bridge_unavailable_interface(),
                raw_hash,
                candidate_path.stat().st_size,
            )
            response["encoding"] = "dxil"

            candidate, comparison, evidence = (
                validate_candidate_reflection_response(
                    request,
                    response,
                    self.baseline,
                    baseline_dxil_access=self.access,
                    candidate_dxil_access=self.access,
                )
            )

            self.assertTrue(comparison.compatible)
            self.assertEqual(
                "ambiguous", candidate.constant_blocks[0].layout_completeness
            )
            self.assertEqual(
                self.access.signature_sha256,
                evidence["dxilCBufferAccess"]["baseline"]["signatureSha256"],
            )
            self.assertEqual(
                self.access.signature_sha256,
                evidence["dxilCBufferAccess"]["candidate"]["signatureSha256"],
            )

    def test_bridge_incomplete_interface_missing_candidate_evidence_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.dxil"
            candidate_path.write_bytes(b"fixed candidate")
            request, raw_hash = self._reflection_request(candidate_path)

            missing_layout = self._bridge_unavailable_interface()
            missing_layout["constantBlocks"][0].pop("layoutEvidence")
            cases = (
                (
                    "baseline-access",
                    self._bridge_unavailable_interface(),
                    None,
                    self.access,
                ),
                (
                    "candidate-access",
                    self._bridge_unavailable_interface(),
                    self.access,
                    None,
                ),
                ("layout-evidence", missing_layout, self.access, self.access),
            )
            for label, interface, baseline_access, candidate_access in cases:
                with self.subTest(label=label):
                    response = abi_support.reflection_response(
                        interface,
                        raw_hash,
                        candidate_path.stat().st_size,
                    )
                    response["encoding"] = "dxil"
                    self._assert_reflection_fails_closed(
                        request,
                        response,
                        baseline_access=baseline_access,
                        candidate_access=candidate_access,
                    )

    def test_bridge_incomplete_interface_non_d3d12_dxil_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.bin"
            candidate_path.write_bytes(b"fixed candidate")
            scopes = (
                ("d3d11-dxbc", "D3D11", "DXBC", "ps_5_0"),
                ("d3d12-dxbc", "D3D12", "DXBC", "ps_5_0"),
            )
            for label, api, candidate_kind, target_profile in scopes:
                with self.subTest(label=label):
                    request, raw_hash = self._reflection_request(
                        candidate_path,
                        api=api,
                        candidate_kind=candidate_kind,
                        target_profile=target_profile,
                    )
                    interface = self._bridge_unavailable_interface()
                    interface["encoding"] = "ShaderEncoding.DXBC"
                    response = abi_support.reflection_response(
                        interface,
                        raw_hash,
                        candidate_path.stat().st_size,
                    )
                    response["encoding"] = candidate_kind.lower()
                    self._assert_reflection_fails_closed(
                        request,
                        response,
                        baseline_access=self.access,
                        candidate_access=self.access,
                    )


ROUTE_TOOL = r'''from pathlib import Path
import shutil
import struct
import sys

mode = sys.argv[1]
if mode == "disassemble":
    shutil.copyfile(Path(sys.argv[2]), Path(sys.argv[4]))
elif mode == "assemble":
    shutil.copyfile(Path(sys.argv[4]), Path(sys.argv[3]))
elif mode in {"dxil-to-spirv", "hlsl-to-spirv"}:
    name = b"main\0"
    name += b"\0" * (-len(name) % 4)
    name_words = struct.unpack("<%dI" % (len(name) // 4), name)
    op_entry = [((3 + len(name_words)) << 16) | 15, 4, 1, *name_words]
    Path(sys.argv[3]).write_bytes(
        struct.pack(
            "<%dI" % (5 + len(op_entry)),
            0x07230203, 0x00010000, 0, 2, 0, *op_entry,
        )
    )
elif mode == "spirv-to-hlsl":
    Path(sys.argv[3]).write_text(
        "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
        encoding="utf-8",
    )
elif mode == "spirv-to-dxil":
    shutil.copyfile(Path(sys.argv[4]), Path(sys.argv[3]))
else:
    raise SystemExit(9)
'''


class DxilAccessWorkspaceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=os.environ.get("RENDERDOC_ULTRA_TEST_TEMP", None)
        )
        # Private evidence helpers receive the canonical root established by
        # public workspace entry points, including Windows short TEMP paths.
        self.root = Path(self.temporary.name).resolve()
        self.script = self.root / "route_tool.py"
        self.script.write_text(ROUTE_TOOL, encoding="utf-8")
        self.fixed_ir = self.root / "fixed.ll"
        self.fixed_ir.write_bytes(REAL_SHAPE_DXIL)
        executable = Path(sys.executable).resolve()
        identity = {
            "executable": str(executable),
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "fixedFileHashes": {
                str(self.script.resolve()): hashlib.sha256(
                    self.script.read_bytes()
                ).hexdigest(),
                str(self.fixed_ir.resolve()): hashlib.sha256(
                    self.fixed_ir.read_bytes()
                ).hexdigest(),
            },
            "decompile": {
                "args": [
                    str(self.script.resolve()),
                    "disassemble",
                    str(self.fixed_ir.resolve()),
                    "{input}",
                    "{output}",
                ],
                "timeoutSeconds": 10,
            },
            "compile": {
                "args": [
                    str(self.script.resolve()),
                    "assemble",
                    "{input}",
                    "{output}",
                    "{baseline}",
                ],
                "timeoutSeconds": 10,
            },
        }

        def routed_tool(operation: str, mode: str) -> dict:
            return {
                "executable": identity["executable"],
                "executableSha256": identity["executableSha256"],
                "fixedFileHashes": identity["fixedFileHashes"],
                operation: {
                    "args": [
                        str(self.script.resolve()),
                        mode,
                        "{input}",
                        "{output}",
                        "{baseline}",
                        "{target_profile}",
                    ],
                    "timeoutSeconds": 10,
                },
            }

        self.tools_path = self.root / "shader-tools.v1.json"
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {
                    "fixed-dxil-asm": identity,
                    "fixed-dxil-spirv": routed_tool(
                        "transform", "dxil-to-spirv"
                    ),
                    "fixed-spirv-hlsl": routed_tool(
                        "decompile", "spirv-to-hlsl"
                    ),
                    "fixed-hlsl-spirv": routed_tool(
                        "compile", "hlsl-to-spirv"
                    ),
                    "fixed-spirv-dxil": routed_tool(
                        "transform", "spirv-to-dxil"
                    ),
                },
            },
        )
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="fixed-dxil-disassembly",
                tool_id="fixed-dxil-asm",
                operation="decompile",
                input_format="dxil",
                output_format="dxil-asm",
                stages=["fragment"],
                fidelity="ir-disassembly",
                priority=1,
                implementation="fixed test DXIL disassembly",
                source_url="https://github.com/microsoft/DirectXShaderCompiler",
            ),
            route_record(
                registry,
                route_id="fixed-dxil-assembly",
                tool_id="fixed-dxil-asm",
                operation="compile",
                input_format="dxil-asm",
                output_format="dxil",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixed test DXIL assembly",
                source_url="https://github.com/microsoft/DirectXShaderCompiler",
            ),
            route_record(
                registry,
                route_id="fixed-dxil-spirv",
                tool_id="fixed-dxil-spirv",
                operation="transform",
                input_format="dxil",
                output_format="spirv",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=10,
                implementation="fixed test DXIL transform",
                source_url="https://example.invalid/dxil-spirv",
            ),
            route_record(
                registry,
                route_id="fixed-spirv-hlsl",
                tool_id="fixed-spirv-hlsl",
                operation="decompile",
                input_format="spirv",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=10,
                implementation="fixed test HLSL reconstruction",
                source_url="https://example.invalid/spirv-hlsl",
                caveats=["Fixture HLSL is reconstructed."],
            ),
            route_record(
                registry,
                route_id="fixed-hlsl-spirv",
                tool_id="fixed-hlsl-spirv",
                operation="compile",
                input_format="hlsl",
                output_format="spirv",
                stages=["fragment"],
                # The fixture uses SPIR-V only as a D3D12-bound intermediate
                # before the fixed SPIR-V -> DXIL transform.
                target_apis=["d3d12"],
                fidelity="native-compile",
                priority=10,
                implementation="fixed test HLSL compiler",
                source_url="https://example.invalid/hlsl-spirv",
            ),
            route_record(
                registry,
                route_id="fixed-spirv-dxil",
                tool_id="fixed-spirv-dxil",
                operation="transform",
                input_format="spirv",
                output_format="dxil",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=10,
                implementation="fixed test DXIL packer",
                source_url="https://example.invalid/spirv-dxil",
            ),
        ]
        self.routes = routes
        self.routes_path = self.root / "shader-toolchains.v1.json"
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, [])
        )
        self.blob = route_support.make_dxil()
        raw_hash = hashlib.sha256(self.blob).hexdigest()
        self.key = ShaderKey(451, "fragment", "main", raw_hash)
        catalog_dir = self.root / "catalog"
        (catalog_dir / "blobs").mkdir(parents=True)
        (catalog_dir / "blobs" / (raw_hash + ".bin")).write_bytes(self.blob)
        shader = ambiguous_interface()
        # Mirrors stripped-DXIL bridge catalogs: all non-CBuffer ABI records are
        # present, but unreliable member reflection lowers the global claim.
        shader["interfaceComplete"] = False
        shader["encoding"] = "ShaderEncoding.DXIL"
        shader.update(
            {
                "shaderKey": self.key.catalog_key,
                "resourceId": self.key.resource_id,
                "shaderResourceId": self.key.resource_id,
                "stage": self.key.stage,
                "entry": self.key.entry_point,
                "entryPoint": self.key.entry_point,
                "rawPath": "blobs/%s.bin" % raw_hash,
                "rawSize": len(self.blob),
                "rawSha256": raw_hash,
            }
        )
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/ambiguous-dxil.rdc",
            "api": "D3D12",
            "shaders": [shader],
            "uses": [
                {
                    "eventId": 12,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": raw_hash,
                }
            ],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        self.catalog_path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(self.catalog_path, catalog)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hash_bound_route_evidence_repairs_gate_and_is_revalidated(self) -> None:
        service = ShaderWorkspaceService(self.tools_path, self.routes_path)
        workspace = self.root / "workspace"
        created = service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxil-asm",
        )
        self.assertEqual(
            "captured-reflection-dxil-access-signature-required",
            created["baselineAbiStatus"],
        )
        selected = load_selected_shader(self.catalog_path, self.key)
        assert selected.abi is not None
        without_access = compare_shader_abi(selected.abi, selected.abi)
        self.assertFalse(without_access.compatible)
        self.assertEqual(
            "access-signature-unavailable",
            without_access.differences[0]["reason"],
        )
        service.decompile(workspace)
        compiled = service.compile(workspace)
        service.candidate_reflection_request(workspace)
        response = abi_support.reflection_response(
            ambiguous_interface(),
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(validated["compatible"])
        report = json.loads(
            Path(validated["abiReportPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "ambiguous-layout-physical-access-only",
            report["evidence"]["dxilCBufferAccess"]["scope"],
        )
        self.assertTrue(service.status(workspace)["abiCompatible"])

        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        baseline_job = workspace / manifest["source"]["producedByJobs"][0]
        baseline_output = baseline_job / "output.ll"
        baseline_output.write_bytes(baseline_output.read_bytes() + b"; tampered\n")
        with self.assertRaisesRegex(
            ManifestValidationError, "does not prove exact compatibility"
        ):
            service.status(workspace)

    def test_projection_rejects_missing_non_cbuffer_or_envelope_facts(self) -> None:
        original = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        mutations = (
            lambda shader: shader.pop("outputSignature"),
            lambda shader: shader["readOnlyResources"][0].pop("bind"),
            lambda shader: shader["constantBlocks"][0].pop("byteSize"),
        )
        service = ShaderWorkspaceService(self.tools_path, self.routes_path)
        try:
            for index, mutate in enumerate(mutations):
                catalog = copy.deepcopy(original)
                mutate(catalog["shaders"][0])
                atomic_write_json(self.catalog_path, catalog)
                workspace = self.root / ("rejected-projection-%d" % index)
                created = service.create(
                    self.catalog_path,
                    self.key.catalog_key,
                    workspace,
                    editable_format="dxil-asm",
                )
                self.assertEqual("unavailable", created["baselineAbiStatus"])
                service.decompile(workspace)
                service.compile(workspace)
                with self.assertRaisesRegex(AbiValidationError, "fail-closed"):
                    service.candidate_reflection_request(workspace)
        finally:
            atomic_write_json(self.catalog_path, original)

    def test_non_dxil_incomplete_interface_is_not_projected(self) -> None:
        blob = workspace_support.make_dxbc()
        raw_hash = hashlib.sha256(blob).hexdigest()
        key = ShaderKey(452, "fragment", "main", raw_hash)
        catalog_dir = self.root / "non-dxil-catalog"
        (catalog_dir / "blobs").mkdir(parents=True)
        (catalog_dir / "blobs" / (raw_hash + ".bin")).write_bytes(blob)
        shader = ambiguous_interface()
        shader.update(
            {
                "interfaceComplete": False,
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
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/ambiguous-dxbc.rdc",
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        catalog["captureId"] = capture_identity(catalog, [key])
        path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(path, catalog)
        selected = load_selected_shader(path, key)
        self.assertEqual("dxbc", selected.encoding)
        self.assertIsNone(selected.abi)
        self.assertEqual("unavailable", selected.abi_status)

    def _compiled_evidence_manifest(
        self, name: str
    ) -> tuple[ShaderWorkspaceService, Path, dict]:
        service = ShaderWorkspaceService(self.tools_path, self.routes_path)
        workspace = self.root / name
        service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxil-asm",
        )
        service.decompile(workspace)
        service.compile(workspace)
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        return service, workspace, manifest

    def _compiled_hlsl_evidence_manifest(
        self, name: str
    ) -> tuple[ShaderWorkspaceService, Path, dict, dict]:
        service = ShaderWorkspaceService(self.tools_path, self.routes_path)
        workspace = self.root / name
        service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="hlsl",
        )
        decompiled = service.decompile(workspace)
        source_path = Path(decompiled["sourcePath"])
        source_path.write_text(
            source_path.read_text(encoding="utf-8")
            + "// human/AI-organized editable layer\n",
            encoding="utf-8",
        )
        compiled = service.compile(workspace)
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        return service, workspace, manifest, compiled

    def test_compile_only_import_gets_independent_access_evidence(self) -> None:
        self._assert_compile_only_import_access_evidence(native=False)

    def test_native_compile_only_import_gets_independent_access_evidence(self) -> None:
        self._assert_compile_only_import_access_evidence(native=True)

    def _assert_compile_only_import_access_evidence(self, *, native: bool) -> None:
        # Imported HLSL has no decompile hop. It still needs actual original
        # and candidate IR rather than trusting its source declarations.
        routes = [route for route in self.routes if route["routeId"] not in
                  ("fixed-dxil-assembly", "fixed-dxil-spirv", "fixed-spirv-hlsl")]
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, [])
        )
        service = ShaderWorkspaceService(self.tools_path, self.routes_path)
        workspace = self.root / "compile-only-access"
        created = service.create(
            self.catalog_path, self.key.catalog_key, workspace, editable_format="hlsl"
        )
        self.assertEqual([], created["routePlan"]["decompile"])
        represented = service.representation(workspace)
        source = b"float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n"
        (workspace / "explanation" / "promoted.hlsl").write_bytes(source)
        service.promote_explanatory_source(
            workspace,
            expected_representation_sha256=represented["sha256"],
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
            acknowledge_semantic_equivalence_unverified=True,
        )
        if native:
            from tests.test_native_shader_compile import response
            service._native_compile_provider = lambda request: response(request, self.blob)
        compiled = service.compile(workspace)
        if native:
            self.assertEqual("renderdoc-native", compiled["compilerBackend"])
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        for role in ("baseline", "candidate"):
            self.assertIsNotNone(service._dedicated_dxil_asm_evidence(workspace, manifest, role))
        response = abi_support.reflection_response(
            ambiguous_interface(), compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        self.assertTrue(service.validate_candidate_reflection(workspace, response)["compatible"])

    def test_repeated_access_validation_reuses_only_hash_bound_parse(self) -> None:
        service, workspace, manifest, _compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-access-parse-cache")
        )
        with mock.patch(
            "src.shader_workspace.workspace.extract_dxil_cbuffer_access_signature",
            wraps=extract_dxil_cbuffer_access_signature,
        ) as parser:
            first = service._dxil_access_evidence(workspace, manifest)
            second = service._dxil_access_evidence(workspace, manifest)

        self.assertEqual(first, second)
        self.assertTrue(all(value is not None for value in first))
        # This fixture deliberately emits identical fixed DXIL text for the
        # baseline and candidate. The service still rebinds and hashes both
        # job artifacts on every call, while the immutable digest is parsed
        # only once.
        self.assertEqual(1, parser.call_count)

    def test_hlsl_round_trip_uses_independent_bound_disassemblies(self) -> None:
        service, workspace, manifest, compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-independent-evidence")
        )
        self.assertEqual(
            [
                "fixed-dxil-spirv",
                "fixed-spirv-hlsl",
            ],
            [
                hop["capability"]["routeId"]
                for hop in manifest["routePlan"]["decompile"]
            ],
        )
        self.assertEqual(
            ["fixed-hlsl-spirv", "fixed-spirv-dxil"],
            [
                hop["capability"]["routeId"]
                for hop in manifest["routePlan"]["compile"]
            ],
        )
        # The HLSL route contains no native IR adjacency. The separately run
        # fixed disassembler is therefore the only physical access proof.
        self.assertIsNone(service._baseline_dxil_asm_evidence(workspace, manifest))
        self.assertIsNone(service._candidate_dxil_asm_evidence(workspace, manifest))
        baseline_bound = service._dedicated_dxil_asm_evidence(
            workspace, manifest, "baseline"
        )
        candidate_bound = service._dedicated_dxil_asm_evidence(
            workspace, manifest, "candidate"
        )
        assert baseline_bound is not None and candidate_bound is not None
        self.assertEqual(REAL_SHAPE_DXIL, baseline_bound[0])
        self.assertEqual(REAL_SHAPE_DXIL, candidate_bound[0])

        evidence = manifest["dxilDisassemblyEvidence"]
        self.assertEqual(
            "renderdoc-ultra-shader-workspace-dxil-disassembly-evidence/v1",
            evidence["schema"],
        )
        self.assertEqual(1, evidence["schemaVersion"])
        baseline_record = evidence["baseline"]
        candidate_record = evidence["candidate"]
        self.assertEqual(
            "renderdoc-ultra-shader-workspace-dxil-disassembly-record/v1",
            candidate_record["schema"],
        )
        self.assertEqual(1, candidate_record["schemaVersion"])
        self.assertEqual(
            "fixed-dxil-disassembly",
            baseline_record["routeBinding"]["capability"]["routeId"],
        )
        self.assertNotIn(
            baseline_record["jobPath"], manifest["source"]["producedByJobs"]
        )
        self.assertNotIn(
            candidate_record["jobPath"],
            [hop["jobPath"] for hop in manifest["latestCompiled"]["compileHops"]],
        )
        self.assertEqual(manifest["workspaceId"], baseline_record["workspaceId"])
        self.assertEqual(manifest["capture"]["captureId"], baseline_record["captureId"])
        self.assertEqual(0, baseline_record["revision"])
        self.assertEqual(1, candidate_record["revision"])
        self.assertEqual(
            manifest["baseline"]["rawSha256"], baseline_record["inputSha256"]
        )
        self.assertEqual(compiled["rawSha256"], candidate_record["inputSha256"])
        self.assertEqual(
            manifest["routePlan"]["routeCatalogSha256"],
            candidate_record["routeCatalogSha256"],
        )
        self.assertEqual(
            manifest["routePlan"]["toolRegistrySha256"],
            candidate_record["toolRegistrySha256"],
        )
        self.assertTrue(candidate_record["command"])
        self.assertIn(
            str(self.fixed_ir.resolve()),
            candidate_record["fixedFileHashes"],
        )

        response = abi_support.reflection_response(
            ambiguous_interface(),
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(validated["compatible"])

    def test_baseline_native_evidence_survives_replaced_editable_source(self) -> None:
        service, workspace, manifest, compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-restored-source-evidence")
        )
        response = abi_support.reflection_response(
            ambiguous_interface(), compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        service.validate_candidate_reflection(workspace, response)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        original = service._dedicated_dxil_asm_evidence(workspace, manifest, "baseline")
        self.assertIsNotNone(original)
        changed = copy.deepcopy(manifest)
        # Project-source publication can replace both the HLSL origin and its hash, while
        # baseline/shader.bin, the compiled revision and all native evidence stay identical.
        changed["source"] = {
            **changed["source"], "origin": "verified-project-source", "sha256": "1" * 64,
        }
        with mock.patch.object(service, "_attempt_dedicated_dxil_disassembly") as execute:
            self.assertEqual(
                original, service._dedicated_dxil_asm_evidence(workspace, changed, "baseline")
            )
            service._ensure_dedicated_baseline_dxil_disassembly(workspace, changed)
            execute.assert_not_called()
        candidate = changed["latestCompiled"]
        service._validate_abi_report(
            workspace, changed, candidate, candidate["abiGate"],
            service._manifest_baseline_abi(changed),
        )
        # Removing the precise historical owner still rejects the same native artifact.
        changed["history"] = [
            entry for entry in changed["history"]
            if entry.get("baselineDxilDisassemblyEvidence") !=
            changed["dxilDisassemblyEvidence"]["baseline"]
        ]
        self.assertIsNone(service._dedicated_dxil_asm_evidence(workspace, changed, "baseline"))

    @staticmethod
    def _revision_publish_denied(staging: Path, destination: Path) -> PermissionError:
        class WindowsPublishDenied(PermissionError):
            # POSIX OSError does not render winerror, even when that attribute
            # is supplied. Keep the simulated historical Windows record exact
            # so recovery remains tested on Linux and macOS too.
            def __str__(self) -> str:
                return "[WinError 5] Access is denied: %r -> %r" % (self.filename, self.filename2)
        error = WindowsPublishDenied(13, "Access is denied", str(staging))
        error.winerror = 5
        error.filename2 = str(destination)
        return error

    def test_revision_publish_retries_transient_windows_access_denied(self) -> None:
        service, workspace, manifest, _compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-publish-retry")
        )
        original_replace = Path.replace
        attempts = 0

        def fail_once(staging: Path, destination: Path):
            nonlocal attempts
            if staging.name.startswith(".0002.staging-"):
                attempts += 1
                if attempts == 1:
                    raise self._revision_publish_denied(staging, destination)
            return original_replace(staging, destination)

        with mock.patch.object(Path, "replace", fail_once), mock.patch(
            "src.shader_workspace.workspace.time.sleep", return_value=None
        ):
            if os.name != "nt":
                # POSIX permission errors are not Windows sharing violations;
                # retain the original failure and the preceding valid revision.
                with self.assertRaises(PermissionError):
                    service.compile(workspace)
                self.assertEqual(1, attempts)
                self.assertEqual(1, json.loads(
                    (workspace / "manifest.json").read_text(encoding="utf-8"))["revision"])
                return
            compiled = service.compile(workspace)

        self.assertEqual(2, attempts)
        self.assertEqual(2, compiled["revision"])
        refreshed = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, refreshed["revision"])
        self.assertEqual(
            2, refreshed["dxilDisassemblyEvidence"]["candidate"]["revision"]
        )
        self.assertNotEqual(
            manifest["dxilDisassemblyEvidence"]["candidate"]["jobPath"],
            refreshed["dxilDisassemblyEvidence"]["candidate"]["jobPath"],
        )

    def test_failed_revision_publish_rolls_back_active_dxil_evidence(self) -> None:
        service, workspace, before, _compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-publish-rollback")
        )
        original_record = copy.deepcopy(
            before["dxilDisassemblyEvidence"]["candidate"]
        )
        original_replace = Path.replace

        def always_fail(staging: Path, destination: Path):
            if staging.name.startswith(".0002.staging-"):
                raise self._revision_publish_denied(staging, destination)
            return original_replace(staging, destination)

        with mock.patch.object(Path, "replace", always_fail), mock.patch(
            "src.shader_workspace.workspace.time.sleep", return_value=None
        ):
            with self.assertRaises(PermissionError):
                service.compile(workspace)

        failed = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, failed["revision"])
        self.assertEqual(1, failed["latestCompiled"]["revision"])
        self.assertEqual(
            original_record,
            failed["dxilDisassemblyEvidence"]["candidate"],
        )
        failed_history = failed["history"][-1]
        self.assertEqual("failed", failed_history["status"])
        self.assertEqual(2, failed_history["revision"])
        self.assertEqual(
            2, failed_history["candidateDxilDisassemblyEvidence"]["revision"]
        )
        self.assertFalse((workspace / "revisions" / "0002").exists())

        recovered = service.compile(workspace)
        self.assertEqual(2, recovered["revision"])
        final_manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            2, final_manifest["dxilDisassemblyEvidence"]["candidate"]["revision"]
        )

    def test_status_strictly_repairs_legacy_failed_publish_evidence(self) -> None:
        service, workspace, _manifest, compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-legacy-publish-repair")
        )
        response = abi_support.reflection_response(
            ambiguous_interface(),
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        self.assertTrue(
            service.validate_candidate_reflection(workspace, response)["compatible"]
        )
        original_replace = Path.replace

        def always_fail(staging: Path, destination: Path):
            if staging.name.startswith(".0002.staging-"):
                raise self._revision_publish_denied(staging, destination)
            return original_replace(staging, destination)

        with mock.patch.object(Path, "replace", always_fail), mock.patch(
            "src.shader_workspace.workspace.time.sleep", return_value=None
        ):
            with self.assertRaises(PermissionError):
                service.compile(workspace)

        legacy = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        failed_record = legacy["history"][-1][
            "candidateDxilDisassemblyEvidence"
        ]
        self.assertEqual(2, failed_record["revision"])
        legacy["dxilDisassemblyEvidence"]["candidate"] = copy.deepcopy(
            failed_record
        )
        atomic_write_json(workspace / "manifest.json", legacy)

        status = service.status(workspace)
        self.assertTrue(status["abiCompatible"])
        repaired = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            1, repaired["dxilDisassemblyEvidence"]["candidate"]["revision"]
        )
        recovery = repaired["history"][-1]
        self.assertEqual(
            "interrupted-compile-evidence-recovery", recovery["operation"]
        )
        self.assertEqual("succeeded", recovery["status"])
        self.assertEqual(2, recovery["discardedFailedRevision"])

    def test_status_never_repairs_unexplained_candidate_evidence(self) -> None:
        service, workspace, _manifest, compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-unexplained-evidence")
        )
        response = abi_support.reflection_response(
            ambiguous_interface(),
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        service.validate_candidate_reflection(workspace, response)
        changed = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        unexplained = copy.deepcopy(
            changed["dxilDisassemblyEvidence"]["candidate"]
        )
        unexplained["revision"] = 2
        unexplained["inputArtifactPath"] = "revisions/0002/shader.bin"
        changed["dxilDisassemblyEvidence"]["candidate"] = unexplained
        atomic_write_json(workspace / "manifest.json", changed)

        with self.assertRaisesRegex(
            ManifestValidationError, "does not prove exact compatibility"
        ):
            service.status(workspace)
        retained = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(unexplained, retained["dxilDisassemblyEvidence"]["candidate"])
        self.assertFalse(
            any(
                entry.get("operation")
                == "interrupted-compile-evidence-recovery"
                for entry in retained["history"]
            )
        )

    def test_migrated_hlsl_regenerates_baseline_access_with_current_tools(
        self,
    ) -> None:
        service = ShaderWorkspaceService(self.tools_path, self.routes_path)
        source_workspace = self.root / "hlsl-before-toolchain-migration"
        created = service.create(
            self.catalog_path,
            self.key.catalog_key,
            source_workspace,
            editable_format="hlsl",
        )
        decompiled = service.decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_path.write_text(
            source_path.read_text(encoding="utf-8") + "// preserved edit\n",
            encoding="utf-8",
        )
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_manifest = json.loads(
            (source_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        old_baseline_record = source_manifest["dxilDisassemblyEvidence"][
            "baseline"
        ]
        self.assertIsNotNone(
            service._dedicated_dxil_asm_evidence(
                source_workspace, source_manifest, "baseline"
            )
        )

        upgraded_routes = json.loads(
            self.routes_path.read_text(encoding="utf-8")
        )
        for route in upgraded_routes["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_routes)

        # A service is intentionally immutable for one exact owner-controlled
        # registry/catalog snapshot. The server factory constructs a fresh
        # service after either small config hash changes.
        migration_service = ShaderWorkspaceService(
            self.tools_path, self.routes_path
        )
        target_workspace = self.root / "hlsl-after-toolchain-migration"
        migrated = migration_service.migrate_toolchain(
            source_workspace,
            target_workspace,
            expected_route_plan_sha256=created["routePlan"]["planSha256"],
            expected_source_sha256=source_hash,
        )
        self.assertFalse(migrated["latestCandidateImported"])
        before_compile = json.loads(
            (target_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        # Path-bound r7 evidence is intentionally not copied into r8.
        self.assertNotIn("dxilDisassemblyEvidence", before_compile)

        compiled = migration_service.compile(target_workspace)
        manifest = json.loads(
            (target_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        evidence = manifest["dxilDisassemblyEvidence"]
        baseline_record = evidence["baseline"]
        candidate_record = evidence["candidate"]
        self.assertIsNotNone(baseline_record)
        self.assertIsNotNone(candidate_record)
        self.assertNotEqual(old_baseline_record, baseline_record)
        self.assertEqual(str(target_workspace.resolve()), baseline_record["workspacePath"])
        self.assertEqual(manifest["workspaceId"], baseline_record["workspaceId"])
        self.assertEqual(
            manifest["routePlan"]["routeCatalogSha256"],
            baseline_record["routeCatalogSha256"],
        )
        self.assertEqual(
            manifest["routePlan"]["toolRegistrySha256"],
            baseline_record["toolRegistrySha256"],
        )
        self.assertTrue(
            any(
                entry.get("operation")
                == "baseline-dxil-disassembly-refresh"
                and entry.get("status") == "succeeded"
                for entry in manifest["history"]
            )
        )
        self.assertIsNotNone(
            migration_service._dedicated_dxil_asm_evidence(
                target_workspace, manifest, "baseline"
            )
        )

        # Mirror an already-created pre-fix r8: its fresh candidate evidence is
        # present, but the migrated baseline slot/history was never generated.
        first_refresh_job = baseline_record["jobPath"]
        manifest["dxilDisassemblyEvidence"]["baseline"] = None
        manifest["history"] = [
            entry
            for entry in manifest["history"]
            if entry.get("operation") != "baseline-dxil-disassembly-refresh"
        ]
        atomic_write_json(target_workspace / "manifest.json", manifest)
        self.assertIsNone(
            json.loads(
                (target_workspace / "manifest.json").read_text(encoding="utf-8")
            )["dxilDisassemblyEvidence"]["baseline"]
        )

        incomplete_candidate = copy.deepcopy(ambiguous_interface())
        incomplete_candidate["interfaceComplete"] = False
        incomplete_candidate["encoding"] = "ShaderEncoding.DXIL"
        for block in incomplete_candidate["constantBlocks"]:
            if block.get("layoutCompleteness") == "ambiguous":
                block["layoutEvidence"] = {
                    "kind": "dxc-dxil-buffer-definition",
                    "typeAnnotation": "unavailable",
                }
        response = abi_support.reflection_response(
            incomplete_candidate,
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = migration_service.validate_candidate_reflection(
            target_workspace, response
        )
        self.assertTrue(validated["compatible"])
        report = json.loads(
            Path(validated["abiReportPath"]).read_text(encoding="utf-8")
        )
        access_evidence = report["evidence"]["dxilCBufferAccess"]
        self.assertEqual(
            "ambiguous-layout-physical-access-only",
            access_evidence["scope"],
        )
        self.assertEqual(
            access_evidence["baseline"]["signatureSha256"],
            access_evidence["candidate"]["signatureSha256"],
        )
        final_status = migration_service.status(target_workspace)
        self.assertTrue(final_status["abiCompatible"])
        final_manifest = json.loads(
            (target_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertIsNotNone(final_manifest["dxilDisassemblyEvidence"]["baseline"])
        self.assertNotEqual(
            first_refresh_job,
            final_manifest["dxilDisassemblyEvidence"]["baseline"]["jobPath"],
        )

    def test_dedicated_evidence_rejects_every_mutable_binding_class(self) -> None:
        service, workspace, manifest, _unused_compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-evidence-bindings")
        )
        mutations = {
            "workspace": lambda record: record.__setitem__(
                "workspaceId", "different-workspace"
            ),
            "revision": lambda record: record.__setitem__("revision", 99),
            "catalog": lambda record: record.__setitem__(
                "routeCatalogSha256", "0" * 64
            ),
            "tool-dependency": lambda record: record["routeBinding"][
                "operation"
            ].__setitem__("fixedFileHashes", {}),
            "input": lambda record: record.__setitem__("inputSha256", "1" * 64),
            "output": lambda record: record.__setitem__("outputSha256", "2" * 64),
            "command": lambda record: record.__setitem__("command", ["not-dxc"]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(manifest)
                mutate(changed["dxilDisassemblyEvidence"]["candidate"])
                compile_history = next(
                    entry
                    for entry in changed["history"]
                    if entry.get("operation") == "compile-route-chain"
                    and entry.get("status") == "succeeded"
                )
                mutate(compile_history["candidateDxilDisassemblyEvidence"])
                self.assertIsNone(
                    service._dedicated_dxil_asm_evidence(
                        workspace, changed, "candidate"
                    )
                )

    def test_tampered_dedicated_output_keeps_ambiguous_gate_closed(self) -> None:
        service, workspace, manifest, compiled = (
            self._compiled_hlsl_evidence_manifest("hlsl-tampered-evidence")
        )
        candidate_record = manifest["dxilDisassemblyEvidence"]["candidate"]
        output_path = workspace / candidate_record["outputPath"]
        output_path.write_bytes(output_path.read_bytes() + b"; same access, tampered\n")
        response = abi_support.reflection_response(
            ambiguous_interface(),
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(workspace, response)
        self.assertFalse(validated["compatible"])
        self.assertEqual(
            "access-signature-unavailable",
            validated["differences"][0]["reason"],
        )

    def test_missing_independent_disassembler_keeps_hlsl_gate_closed(self) -> None:
        routes_without_disassembly = [
            route
            for route in self.routes
            if route["routeId"] != "fixed-dxil-disassembly"
        ]
        route_path = self.root / "shader-toolchains-no-dxc.v1.json"
        atomic_write_json(
            route_path,
            toolchain_document(self.tools_path, routes_without_disassembly, []),
        )
        service = ShaderWorkspaceService(self.tools_path, route_path)
        workspace = self.root / "hlsl-no-disassembly"
        service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="hlsl",
        )
        service.decompile(workspace)
        compiled = service.compile(workspace)
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        decompile_history = next(
            entry
            for entry in manifest["history"]
            if entry.get("operation") == "decompile-route-chain"
        )
        self.assertEqual(
            "failed",
            decompile_history["baselineDxilDisassemblyEvidence"]["status"],
        )
        self.assertNotIn("dxilDisassemblyEvidence", manifest)
        response = abi_support.reflection_response(
            ambiguous_interface(),
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(workspace, response)
        self.assertFalse(validated["compatible"])
        self.assertEqual(
            "access-signature-unavailable",
            validated["differences"][0]["reason"],
        )

    def test_duplicate_direct_baseline_hops_are_not_arbitrarily_selected(self) -> None:
        service, workspace, manifest = self._compiled_evidence_manifest(
            "duplicate-baseline-evidence"
        )
        self.assertIsNotNone(
            service._baseline_dxil_asm_evidence(workspace, manifest)
        )
        plan = manifest["routePlan"]
        decompile_binding = plan["decompile"][0]
        compile_binding = plan["compile"][0]
        decompile_entry = next(
            item
            for item in manifest["history"]
            if item.get("operation") == "decompile-route-chain"
        )
        decompile_hop = decompile_entry["hops"][0]
        compile_hop = manifest["latestCompiled"]["compileHops"][0]

        expected = [decompile_binding, compile_binding, decompile_binding]
        actual = []
        for index, (binding, hop) in enumerate(
            zip(expected, [decompile_hop, compile_hop, decompile_hop], strict=True)
        ):
            rebound = copy.deepcopy(hop)
            rebound["hop"] = index + 1
            rebound["routeBinding"] = binding
            actual.append(rebound)
        plan["decompile"] = expected
        manifest["source"]["producedByJobs"] = [
            item["jobPath"] for item in actual
        ]
        decompile_entry["hops"] = actual

        # baseline -> ll -> same baseline -> ll is a fully hash-adjacent chain,
        # but it contains two equally eligible direct baseline conversions.
        self.assertIsNone(
            service._baseline_dxil_asm_evidence(workspace, manifest)
        )

    def test_duplicate_direct_candidate_producers_are_not_selected(self) -> None:
        service, workspace, manifest = self._compiled_evidence_manifest(
            "duplicate-candidate-evidence"
        )
        self.assertIsNotNone(
            service._candidate_dxil_asm_evidence(workspace, manifest)
        )
        plan = manifest["routePlan"]
        compile_binding = plan["compile"][0]
        decompile_binding = plan["decompile"][0]
        compile_hop = manifest["latestCompiled"]["compileHops"][0]
        decompile_entry = next(
            item
            for item in manifest["history"]
            if item.get("operation") == "decompile-route-chain"
        )
        decompile_hop = decompile_entry["hops"][0]

        expected = [compile_binding, decompile_binding, compile_binding]
        actual = []
        for index, (binding, hop) in enumerate(
            zip(expected, [compile_hop, decompile_hop, compile_hop], strict=True)
        ):
            rebound = copy.deepcopy(hop)
            rebound["hop"] = index + 1
            rebound["routeBinding"] = binding
            actual.append(rebound)
        plan["compile"] = expected
        manifest["latestCompiled"]["compileHops"] = actual

        # ll -> candidate -> ll -> candidate has two direct producers of the
        # exact latest raw bytes. Neither old input .ll is authoritative.
        self.assertIsNone(
            service._candidate_dxil_asm_evidence(workspace, manifest)
        )

    def test_access_required_workspace_fails_closed_when_adjacent_proof_is_missing(
        self,
    ) -> None:
        service, workspace, manifest = self._compiled_evidence_manifest(
            "missing-adjacent-access-proof"
        )
        candidate = manifest["latestCompiled"]
        response = abi_support.reflection_response(
            ambiguous_interface(),
            candidate["rawSha256"],
            candidate["byteSize"],
        )
        response["encoding"] = "dxil"
        with mock.patch.object(
            service, "_baseline_dxil_asm_evidence", return_value=None
        ):
            result = service.validate_candidate_reflection(workspace, response)
        self.assertFalse(result["compatible"])
        self.assertEqual(
            "access-signature-unavailable",
            result["differences"][0]["reason"],
        )


if __name__ == "__main__":
    unittest.main()
