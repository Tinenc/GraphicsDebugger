"""Unity header byte order and serialized-type fields follow format versions."""
import struct

import pytest

from src.symbols.engines import unity_serialized


def serialized(version, *, endian=0, classes=(115, 21, 28)):
    order = ">" if endian else "<"
    metadata = b"2021.3.16f1\0" + struct.pack(order + "i?i", 19, False, len(classes))
    for class_id in classes:
        metadata += struct.pack(order + "i", class_id)
        if version >= 16:
            metadata += b"\0"
        if version >= 17:
            metadata += struct.pack(order + "h", -1)
        if (version < 16 and class_id < 0) or (version >= 16 and class_id == 114):
            metadata += b"s" * 16
        metadata += b"h" * 16
    metadata += struct.pack(order + "i", 0)  # Empty object table; types are not objects.
    data_offset = 48 if version >= 22 else 20
    file_size = data_offset + len(metadata)
    header = struct.pack(">4I", len(metadata), file_size, version, data_offset) + bytes([endian, 0, 0, 0])
    if version >= 22:
        header += struct.pack(">IQQQ", len(metadata), file_size, data_offset, 0)
    return header + metadata, file_size, data_offset, len(metadata)


@pytest.mark.parametrize("version", [13, 16, 17, 22])
@pytest.mark.parametrize("endian", [0, 1])
def test_type_headers_do_not_shift_classes_and_large_sizes_stay_big_endian(version, endian):
    data, size, offset, metadata = serialized(version, endian=endian)
    parsed = unity_serialized.parse_serialized(data)
    assert parsed["declaredFileSize"] == size
    assert parsed["metadataSize"] == metadata
    assert parsed["dataOffset"] == offset
    assert parsed["objectClasses"] == ["MonoScript", "Material", "Texture2D"]
    assert parsed["classificationScope"] == "serialized-types"
    assert parsed["classificationComplete"] is True


def test_mono_behaviour_script_hash_does_not_shift_next_material_type():
    data, *_ = serialized(22, classes=(114, 21))
    assert unity_serialized.parse_serialized(data)["objectClasses"] == ["MonoBehaviour", "Material"]


def test_truncated_type_header_is_not_claimed_complete():
    data, *_ = serialized(22, classes=(21, 28))
    parsed = unity_serialized.parse_serialized(data[:-12])
    assert parsed["objectClasses"] == ["Material"]
    assert parsed["classificationComplete"] is False
