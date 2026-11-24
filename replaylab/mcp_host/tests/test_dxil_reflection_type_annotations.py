"""Optional access analysis cannot invalidate complete native DXIL layouts."""
import ast
import hashlib

import pytest

from src.shader_workspace.adapters import dxil_access_signature as host
from test_bridge_dxil_access_gate import load_bridge, load_bridge_parser, reflection


# Older RenderDoc DXC output has LLVM type annotations, but no textual Buffer
# Definitions section. A static two-descriptor CBV array is a normal interface.
ANNOTATED_ARRAY = b'''target triple = "dxil-ms-dx"
%dx.types.Handle = type { i8* }
%Test = type { <4 x float> }
define void @main() {
  %a = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 0, i32 3, i1 false)
  %b = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 0, i32 4, i1 false)
  ret void
}
!dx.resources = !{!0}
!0 = !{null, null, !1, null}
!1 = !{!2}
!2 = !{i32 0, [2 x %Test]* undef, !"test", i32 0, i32 3, i32 2, i32 16, null}
!dx.typeAnnotations = !{!9}
!9 = !{i32 0, %Test undef, !10}
!10 = !{i32 16, !11}
!11 = !{i32 6, !"value", i32 3, i32 0, i32 7, i32 9}
'''


@pytest.mark.parametrize("module", [host, load_bridge_parser()])
def test_static_cbv_array_type_evidence_does_not_claim_access_equivalence(module):
    expected = hashlib.sha256(ANNOTATED_ARRAY).hexdigest()
    assert module.extract_dxil_annotated_cbuffer_bindings(ANNOTATED_ARRAY,
        expected_sha256=expected) == ((0, 3, 16),)
    with pytest.raises(module.DxilAccessSignatureError, match="arrays"):
        module.extract_dxil_cbuffer_access_signature(ANNOTATED_ARRAY)
    with pytest.raises(module.DxilAccessSignatureError, match="hash"):
        module.extract_dxil_annotated_cbuffer_bindings(ANNOTATED_ARRAY, expected_sha256="0" * 64)


@pytest.mark.parametrize("before,after", [
    (b"i32 2, i32 16, null", b"i32 0, i32 16, null"),
    (b"i32 2, i32 16, null", b"i32 4294967295, i32 16, null"),
    (b"[2 x %Test]", b"[3 x %Test]"),
    (b"!10 = !{i32 16", b"!10 = !{i32 32"),
    (b"i32 3, i32 0, i32 7", b"i32 3, i32 17, i32 7"),
    (b"i32 3, i32 0, i32 7", b"i32 7, i32 0, i32 7"),
    (b"%Test undef, !10}", b"%Test undef, !10, %Test undef, !10}"),
    (b"!10 = !{i32 16, !11}", b"!10 = !{i32 16}"),
])
def test_type_annotation_contradictions_fail_closed_in_host_and_bridge(before, after):
    source = ANNOTATED_ARRAY.replace(before, after)
    assert source != ANNOTATED_ARRAY
    for module in (host, load_bridge_parser()):
        with pytest.raises(module.DxilAccessSignatureError):
            module.extract_dxil_annotated_cbuffer_bindings(source)


def test_optional_access_failure_preserves_only_proven_layouts():
    bridge = load_bridge()
    blocks = [{"space": 0, "bind": 3, "byteSize": 16},
              {"space": 0, "bind": 8, "byteSize": 32}]
    access, evidence = bridge._dxil_evidence_from_snapshot(ANNOTATED_ARRAY, blocks)
    assert access is None
    assert set(evidence) == {(0, 8)}
    assert evidence[(0, 8)]["typeAnnotation"] == "unavailable"
    with pytest.raises(RuntimeError, match="contradicts reflection"):
        bridge._dxil_evidence_from_snapshot(ANNOTATED_ARRAY,
            [{"space": 0, "bind": 3, "byteSize": 32}])
    with pytest.raises(RuntimeError, match="incomplete"):
        bridge._dxil_evidence_from_snapshot(ANNOTATED_ARRAY.replace(
            b"!dx.typeAnnotations = !{!9}", b""), blocks)


def test_single_cbv_without_printer_layout_uses_annotation_evidence():
    bridge = load_bridge()
    source = ANNOTATED_ARRAY.replace(b"[2 x %Test]", b"%Test").replace(
        b"i32 2, i32 16, null", b"i32 1, i32 16, null").replace(
        b"i32 0, i32 4, i1 false", b"i32 0, i32 3, i1 false")
    access, evidence = bridge._dxil_evidence_from_snapshot(source,
        [{"space": 0, "bind": 3, "byteSize": 16}])
    assert access is not None and evidence == {}


def test_positive_type_annotations_do_not_upgrade_incomplete_native_reflection():
    bridge = load_bridge()
    native = reflection(b"native")
    native.constantBlocks = native.constantBlocks[:1]
    block = native.constantBlocks[0]
    block.fixedBindNumber, block.byteSize, block.bindArraySize = 3, 16, 2
    block.variables = []
    access, evidence = bridge._dxil_evidence_from_snapshot(ANNOTATED_ARRAY,
        [{"space": 0, "bind": 3, "byteSize": 16}])
    assert access is None and evidence == {}
    result = bridge._compiled_shader_interface(native, evidence)
    assert result["constantBlocks"][0]["layoutCompleteness"] != "complete"


def test_bridge_parser_remains_python36_compatible():
    parser = load_bridge_parser()
    with open(parser.__file__, encoding="utf-8") as source:
        ast.parse(source.read(), feature_version=(3, 6))
