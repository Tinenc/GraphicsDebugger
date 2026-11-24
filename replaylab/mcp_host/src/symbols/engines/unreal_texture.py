"""Thumbnail fingerprints for cooked Unreal textures without an engine loader.

Locates ``FTexturePlatformData`` in a Texture2D's ``.uexp`` by its pixel
format string, walks the mip table (UE 5.3+ data-resource style, or the
older inline ``FByteBulkData`` headers) and decodes the smallest inline mip
that is still ≥ 8 px, which is all a 16x16 identity thumbnail needs.  Mips
stored in ``.ubulk`` are not read.  Unsupported formats (BC7, ASTC, float)
are reported by name so the caller can say why no fingerprint exists.
"""

from __future__ import annotations

import re
import struct
from typing import Any

from ..imaging import ImageDecodeError, decode_block_compressed, decode_uncompressed, fingerprint

_PIXEL_FORMAT = re.compile(rb"PF_[A-Za-z0-9_]{1,40}\x00")
_BLOCK_FORMATS = {"PF_DXT1": "BC1", "PF_DXT3": "BC2", "PF_DXT5": "BC3", "PF_BC4": "BC4", "PF_BC5": "BC5", "PF_BC7": "BC7"}
_PLAIN_FORMATS = {"PF_B8G8R8A8": "B8G8R8A8", "PF_R8G8B8A8": "R8G8B8A8", "PF_G8": "G8", "PF_R8": "R8",
                  "PF_A8": "A8", "PF_R8G8": "R8G8", "PF_G16": "G16"}
MIN_THUMB_SOURCE = 8
MAX_THUMB_SOURCE = 128


def mip_bytes(fmt: str, width: int, height: int) -> int:
    if fmt in _BLOCK_FORMATS:
        block = 8 if _BLOCK_FORMATS[fmt] in ("BC1", "BC4") else 16
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block
    if fmt in _PLAIN_FORMATS:
        stride = {"B8G8R8A8": 4, "R8G8B8A8": 4, "G8": 1, "R8": 1, "A8": 1, "R8G8": 2, "G16": 2}[_PLAIN_FORMATS[fmt]]
        return width * height * stride
    known = {"PF_BC7": 16, "PF_BC6H": 16, "PF_FloatRGBA": 8, "PF_R16F": 2, "PF_G16R16": 4, "PF_A32B32G32R32F": 16}
    if fmt in known:
        if fmt in ("PF_BC7", "PF_BC6H"):
            return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
        return width * height * known[fmt]
    if fmt.startswith("PF_ASTC"):
        try:
            bw, bh = fmt[len("PF_ASTC_"):].split("x")[:2]
            bw, bh = int(bw), int(bh[:2])
        except (ValueError, IndexError):
            return -1
        return ((width + bw - 1) // bw) * ((height + bh - 1) // bh) * 16
    return -1


def parse_platform_data(data: bytes) -> dict[str, Any]:
    """Locate the texture platform data and enumerate its mips."""

    match = _PIXEL_FORMAT.search(data)
    if match is None:
        raise ImageDecodeError("no pixel format string in export data")
    fmt = match.group(0)[:-1].decode("ascii")
    string_length_offset = match.start() - 4
    if string_length_offset < 12:
        raise ImageDecodeError("pixel format string has no platform-data header")
    declared_length = struct.unpack_from("<i", data, string_length_offset)[0]
    if declared_length != len(match.group(0)):
        raise ImageDecodeError("pixel format string is not FString framed")
    size_x, size_y, packed = struct.unpack_from("<iiI", data, string_length_offset - 12)
    if not (1 <= size_x <= 32768 and 1 <= size_y <= 32768):
        raise ImageDecodeError("platform data dimensions are invalid")
    cursor = match.end()
    if packed & 0x80000000:  # BitMask_HasOptData
        cursor += 8
    first_mip = struct.unpack_from("<i", data, cursor)[0]
    cursor += 4
    num_mips = struct.unpack_from("<i", data, cursor)[0]
    cursor += 4
    if not (0 <= first_mip <= 20) or not (1 <= num_mips <= 20):
        raise ImageDecodeError("mip table counts are invalid (first=%d, count=%d)" % (first_mip, num_mips))
    mips: list[dict[str, Any]] = []
    expected_x, expected_y = max(1, size_x >> first_mip), max(1, size_y >> first_mip)
    style = None
    for index in range(num_mips):
        if cursor + 4 > len(data):
            break
        head = struct.unpack_from("<I", data, cursor)[0]
        payload = mip_bytes(fmt, expected_x, expected_y)
        entry: dict[str, Any] = {"index": first_mip + index, "sizeX": expected_x, "sizeY": expected_y,
                                 "inline": False, "offset": None, "bytes": payload}
        if style in (None, "resource") and cursor + 16 <= len(data) and \
                struct.unpack_from("<iii", data, cursor + 4) == (expected_x, expected_y, 1):
            # UE 5.3+: int32 data-resource index, no inline payload (bulk file)
            style = "resource"
            cursor += 16
        elif style in (None, "resource") and payload > 0 and cursor + 4 + payload + 12 <= len(data) and \
                struct.unpack_from("<iii", data, cursor + 4 + payload) == (expected_x, expected_y, 1):
            style = "resource"
            entry.update({"inline": True, "offset": cursor + 4})
            cursor += 4 + payload + 12
        else:
            # Legacy FByteBulkData header: flags, count, size, offset(int64) [+ payload]
            flags = head
            wide = bool(flags & 0x2000)  # BULKDATA_Size64Bit
            if wide:
                if cursor + 4 + 8 + 8 + 8 > len(data):
                    break
                count, size_on_disk, offset = struct.unpack_from("<qqq", data, cursor + 4)
                header = 4 + 24
            else:
                if cursor + 4 + 4 + 4 + 8 > len(data):
                    break
                count, size_on_disk, offset = struct.unpack_from("<iiq", data, cursor + 4)
                header = 4 + 16
            inline = not (flags & 0x100) and not (flags & 0x0100000) and size_on_disk > 0 and (
                flags & 0x40 or offset in (-1, 0) or cursor + header + size_on_disk + 12 <= len(data))
            style = "legacy"
            if inline and cursor + header + size_on_disk + 12 <= len(data) and \
                    struct.unpack_from("<iii", data, cursor + header + size_on_disk) == (expected_x, expected_y, 1):
                entry.update({"inline": True, "offset": cursor + header, "bytes": size_on_disk})
                cursor += header + size_on_disk + 12
            elif cursor + header + 12 <= len(data) and \
                    struct.unpack_from("<iii", data, cursor + header) == (expected_x, expected_y, 1):
                cursor += header + 12
            else:
                break
        mips.append(entry)
        expected_x, expected_y = max(1, expected_x >> 1), max(1, expected_y >> 1)
    if not mips:
        raise ImageDecodeError("no mip entries could be aligned for %s %dx%d" % (fmt, size_x, size_y))
    return {"sizeX": size_x, "sizeY": size_y, "pixelFormat": fmt, "firstMipToSerialize": first_mip,
            "numMips": num_mips, "mips": mips, "style": style}


def decode_mip(data: bytes, fmt: str, mip: dict[str, Any]) -> bytearray:
    payload = data[int(mip["offset"]):int(mip["offset"]) + int(mip["bytes"])]
    if fmt in _BLOCK_FORMATS:
        return decode_block_compressed(payload, int(mip["sizeX"]), int(mip["sizeY"]), _BLOCK_FORMATS[fmt])
    if fmt in _PLAIN_FORMATS:
        return decode_uncompressed(payload, int(mip["sizeX"]), int(mip["sizeY"]), _PLAIN_FORMATS[fmt])
    raise ImageDecodeError("pixel format %s is not decodable here (BC7/ASTC/float need a provider)" % fmt)


def texture_fingerprint(export_data: bytes) -> dict[str, Any]:
    """Fingerprint the smallest decodable inline mip of a cooked Texture2D."""

    try:
        platform = parse_platform_data(export_data)
    except (ImageDecodeError, struct.error) as exc:
        return {"available": False, "reason": str(exc)}
    fmt = platform["pixelFormat"]
    candidates = [mip for mip in platform["mips"] if mip["inline"]
                  and MIN_THUMB_SOURCE <= max(int(mip["sizeX"]), int(mip["sizeY"])) <= MAX_THUMB_SOURCE]
    candidates.sort(key=lambda mip: max(int(mip["sizeX"]), int(mip["sizeY"])))
    if not candidates:
        inline = [mip for mip in platform["mips"] if mip["inline"]]
        return {"available": False, "pixelFormat": fmt, "sizeX": platform["sizeX"], "sizeY": platform["sizeY"],
                "reason": ("no inline mip between %d and %d px (inline mips: %s); larger mips live in .ubulk"
                           % (MIN_THUMB_SOURCE, MAX_THUMB_SOURCE, ", ".join("%dx%d" % (m["sizeX"], m["sizeY"]) for m in inline) or "none"))}
    # prefer ~32 px: enough detail for a 16x16 thumbnail, cheap to decode
    chosen = min(candidates, key=lambda mip: abs(max(int(mip["sizeX"]), int(mip["sizeY"])) - 32))
    try:
        rgba = decode_mip(export_data, fmt, chosen)
    except (ImageDecodeError, struct.error, IndexError) as exc:
        return {"available": False, "pixelFormat": fmt, "sizeX": platform["sizeX"], "sizeY": platform["sizeY"],
                "reason": str(exc)}
    result = fingerprint(rgba, int(chosen["sizeX"]), int(chosen["sizeY"]))
    result.update({"available": True, "pixelFormat": fmt, "sizeX": platform["sizeX"], "sizeY": platform["sizeY"],
                   "mipUsed": {"index": chosen["index"], "sizeX": chosen["sizeX"], "sizeY": chosen["sizeY"]},
                   "mipStyle": platform["style"]})
    return result
