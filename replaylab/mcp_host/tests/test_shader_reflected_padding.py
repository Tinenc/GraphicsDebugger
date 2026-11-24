"""CBuffer extents exclude trailing padding which a following member can reuse."""
from types import SimpleNamespace

import pytest

from tests.test_bridge_catalog_rebind import _load_bridge


def scalar_type(**kwargs):
    values = dict(name="float", baseType="VarType.Float", rows=1, columns=1,
                  elements=1, arrayByteStride=0, matrixByteStride=0,
                  flags=0, members=[], RowMajor=lambda: False)
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("rows,columns,row_major,expected", [
    (2, 2, True, 24), (2, 2, False, 24), (2, 3, True, 28), (2, 3, False, 40),
])
def test_matrix_tail_padding_can_hold_the_following_scalar(rows, columns, row_major, expected):
    bridge = _load_bridge()
    matrix = scalar_type(rows=rows, columns=columns, matrixByteStride=16,
                         RowMajor=lambda: row_major)
    assert bridge._shader_type_byte_size(matrix) == expected
    block = SimpleNamespace(byteSize=expected + 4, variables=[
        SimpleNamespace(name="matrix", byteOffset=0, type=matrix),
        SimpleNamespace(name="nextValue", byteOffset=expected, type=scalar_type()),
    ])
    fields, completeness = bridge._constant_block_layout(block)
    assert completeness == "complete" and len(fields) == 2


def test_arrays_use_stride_between_elements_and_only_final_element_extent():
    bridge = _load_bridge()
    vector_array = scalar_type(columns=2, elements=4, arrayByteStride=16)
    assert bridge._shader_type_byte_size(vector_array) == 56
    matrix_array = scalar_type(rows=2, columns=2, matrixByteStride=16,
                               elements=3, arrayByteStride=32)
    assert bridge._shader_type_byte_size(matrix_array) == 88
    structure = scalar_type(name="Instance", baseType="VarType.Struct", rows=0, columns=0,
        elements=2, arrayByteStride=32,
        members=[SimpleNamespace(name="value", byteOffset=16, type=scalar_type())])
    assert bridge._shader_type_byte_size(structure) == 52
    fields = bridge._shader_constant_fields([SimpleNamespace(name="instances", byteOffset=0, type=structure)])
    assert bridge._constant_field_annotations_are_complete(fields, 52)


def test_reused_padding_does_not_allow_real_overlaps():
    bridge = _load_bridge()
    matrix = scalar_type(rows=2, columns=2, matrixByteStride=16)
    block = SimpleNamespace(byteSize=32, variables=[
        SimpleNamespace(name="matrix", byteOffset=0, type=matrix),
        SimpleNamespace(name="overlap", byteOffset=20, type=scalar_type()),
    ])
    assert bridge._constant_block_layout(block)[1] == "ambiguous"


def test_native_empty_struct_does_not_hide_real_fields_at_the_same_offset():
    bridge = _load_bridge()
    empty = scalar_type(name="Empty", baseType="VarType.Struct", rows=0, columns=0)
    assert bridge._shader_type_byte_size(empty) == 0
    nested = scalar_type(name="Nested", baseType="VarType.Struct", rows=0, columns=0,
                         arrayByteStride=32, members=[
        SimpleNamespace(name="a", byteOffset=0, type=scalar_type(columns=3)),
        SimpleNamespace(name="b", byteOffset=16, type=empty),
        SimpleNamespace(name="c", byteOffset=16, type=scalar_type(columns=2)),
    ])
    block = SimpleNamespace(byteSize=24, variables=[
        SimpleNamespace(name="empty", byteOffset=0, type=empty),
        SimpleNamespace(name="nested", byteOffset=0, type=nested),
    ])
    fields, completeness = bridge._constant_block_layout(block)
    assert completeness == "complete"
    assert [field["name"] for field in fields] == ["nested"]
    assert [field["name"] for field in fields[0]["members"]] == ["a", "c"]
    assert fields[0]["byteSize"] == 24


@pytest.mark.parametrize("change", [
    {"baseType": "VarType.Unknown"}, {"rows": -1}, {"elements": 0},
    {"elements": 2, "arrayByteStride": 16}, {"arrayByteStride": 16},
    {"pointerTypeID": 10},
])
def test_unknown_or_unresolved_struct_storage_is_not_omitted_as_empty(change):
    bridge = _load_bridge()
    empty = scalar_type(name="Unresolved", baseType="VarType.Struct", rows=0, columns=0)
    for key, value in change.items():
        setattr(empty, key, value)
    assert not bridge._shader_type_is_empty_struct(empty)
    assert len(bridge._shader_constant_fields([SimpleNamespace(name="unknown", byteOffset=0, type=empty)])) == 1
