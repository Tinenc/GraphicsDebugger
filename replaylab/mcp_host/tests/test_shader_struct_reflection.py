import pytest
from copy import deepcopy
from types import SimpleNamespace

from tests.test_bridge_catalog_rebind import _load_bridge
from tests.test_shader_abi import complete_interface
from src.shader_workspace.abi import parse_shader_abi, compare_shader_abi
from src.shader_workspace.errors import AbiValidationError
from src.symbols.scanner import interface_is_complete


def _struct_field():
    scalar = dict(name="tint", byteOffset=0, absoluteByteOffset=0,
                  relativeOffset=0, byteSize=16, baseType="VarType.Float",
                  rows=1, columns=4, elements=1, arrayByteStride=0,
                  matrixByteStride=0, rowMajor=False, flags=0,
                  pointerTypeID=0xFFFFFFFF)
    return dict(name="instances", byteOffset=0, relativeOffset=0,
                absoluteByteOffset=0, byteSize=65536, baseType="VarType.Struct",
                rows=0, columns=0, elements=256, arrayByteStride=256,
                matrixByteStride=0, rowMajor=False, flags=0,
                pointerTypeID=0xFFFFFFFF, members=[scalar])


def _interface(field):
    result = complete_interface()
    result["encoding"] = "ShaderEncoding.SPIRV"
    result["constantBlocks"][0].update(byteSize=65536, fields=[field])
    return result


def test_native_struct_array_with_zero_scalar_dimensions_remains_complete():
    bridge = _load_bridge()
    field = _struct_field()
    assert bridge._constant_field_annotations_are_complete([field], 65536)
    abi = parse_shader_abi(_interface(field))
    assert interface_is_complete(_interface(field))
    aggregate = abi.constant_blocks[0].fields[0]
    assert (aggregate.rows, aggregate.columns, aggregate.elements) == (0, 0, 256)
    assert aggregate.members[0].byte_size == 16


@pytest.mark.parametrize("mutation", ["empty", "wrong-type", "negative", "stride", "outside-element"])
def test_invalid_zero_dimension_or_struct_layout_stays_rejected(mutation):
    field = _struct_field()
    if mutation == "empty":
        field["members"] = []
    elif mutation == "wrong-type":
        field["baseType"] = "VarType.Float"
    elif mutation == "negative":
        field["rows"] = -1
    elif mutation == "stride":
        field["arrayByteStride"] = 0
    elif mutation == "outside-element":
        field["members"][0].update(byteOffset=252, relativeOffset=252, absoluteByteOffset=252)
    assert not _load_bridge()._constant_field_annotations_are_complete([field], 65536)
    assert not interface_is_complete(_interface(field))
    with pytest.raises(AbiValidationError):
        parse_shader_abi(_interface(field))


def test_structured_resource_type_accepts_native_zero_dimensions():
    result = complete_interface()
    aggregate = _struct_field()
    aggregate.update(byteSize=256, elements=1, arrayByteStride=0)
    result["readOnlyResources"][0]["variableType"] = aggregate
    abi = parse_shader_abi(result)
    assert interface_is_complete(result)
    assert abi.read_only_resources[0].variable_type.base_type == "struct"


def test_last_struct_element_does_not_require_unused_tail_padding():
    field = _struct_field()
    field.update(byteSize=272, elements=2)
    assert _load_bridge()._constant_field_annotations_are_complete([field], 272)
    assert parse_shader_abi(_interface(field)).constant_blocks[0].fields[0].byte_size == 272


def _runtime_storage_interface():
    native_array = SimpleNamespace(name="float4", baseType="VarType.Float", rows=1,
                                  columns=4, elements=0xFFFFFFFF, arrayByteStride=16,
                                  matrixByteStride=0, flags=0, members=[])
    native_struct = SimpleNamespace(name="storage", baseType="VarType.Float", rows=0,
                                   columns=0, elements=1, arrayByteStride=0,
                                   matrixByteStride=0, flags=0,
                                   members=[SimpleNamespace(name="values", byteOffset=0, type=native_array)])
    variable_type = _load_bridge()._shader_constant_type_dict(native_struct)
    result = complete_interface()
    result["encoding"] = "ShaderEncoding.SPIRV"
    binding = deepcopy(result["readOnlyResources"][0])
    binding.update(variableType=variable_type, isTexture=False, isReadOnly=False,
                   resourceType="DescriptorType.ReadWriteBuffer",
                   descriptorType="DescriptorType.ReadWriteBuffer", textureType="TextureType.Buffer")
    result["readWriteResources"] = [binding]
    return result


def test_runtime_storage_array_preserves_unsized_identity_without_fake_capacity():
    result = _runtime_storage_interface()
    shape = result["readWriteResources"][0]["variableType"]
    assert shape["baseType"] == "VarType.Struct"
    assert shape["byteSize"] == shape["members"][0]["byteSize"] == 0
    assert shape["members"][0]["elements"] == 0xFFFFFFFF
    assert interface_is_complete(result)
    abi = parse_shader_abi(result)
    assert compare_shader_abi(abi, parse_shader_abi(abi.to_dict())).compatible
    changed = deepcopy(result)
    changed["readWriteResources"][0]["variableType"]["members"][0]["arrayByteStride"] = 32
    changed["readWriteResources"][0]["variableType"]["members"][0]["typeInfo"]["arrayByteStride"] = 32
    assert not compare_shader_abi(abi, parse_shader_abi(changed)).compatible


@pytest.mark.parametrize("mutation", ["fake-capacity", "zero-stride", "following-member", "cbuffer"])
def test_runtime_array_layout_remains_strict(mutation):
    result = _runtime_storage_interface()
    shape = result["readWriteResources"][0]["variableType"]
    tail = shape["members"][0]
    if mutation == "fake-capacity":
        tail["byteSize"] = shape["byteSize"] = 16
    elif mutation == "zero-stride":
        tail["arrayByteStride"] = tail["typeInfo"]["arrayByteStride"] = 0
    elif mutation == "following-member":
        other = deepcopy(_struct_field()["members"][0])
        other.update(byteOffset=16, relativeOffset=16, absoluteByteOffset=16)
        shape["members"].append(other)
        shape["byteSize"] = 32
    else:
        result["constantBlocks"][0]["fields"] = [tail]
    assert not interface_is_complete(result)
    with pytest.raises(AbiValidationError):
        parse_shader_abi(result)

