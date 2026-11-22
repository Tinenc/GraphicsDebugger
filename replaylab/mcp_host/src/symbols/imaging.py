"""Tiny pure-Python image toolkit for texture identity fingerprints.

Decodes the block formats games ship most (BC1/DXT1, BC2/DXT3, BC3/DXT5,
BC4, BC5) and plain 8-bit formats, reduces any image to a 16x16 grayscale
thumbnail plus mean colour, and compares thumbnails with a difference hash
evaluated over the eight dihedral transforms so mirrored/rotated/resized
copies still match.  No third-party imaging library is required, which keeps
the frozen Host self-contained; unsupported formats report why.
"""

from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Sequence
from typing import Any

THUMB = 16
HASH_STRONG = 6
HASH_PLAUSIBLE = 12


class ImageDecodeError(ValueError):
    """The bytes do not describe a decodable image in the claimed format."""


# ---------------------------------------------------------------- block formats

def _rgb565(value: int) -> tuple[int, int, int]:
    r = (value >> 11) & 0x1F
    g = (value >> 5) & 0x3F
    b = value & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _bc1_block(data: bytes, offset: int, out: bytearray, x0: int, y0: int, width: int, height: int,
               alpha_from: bytes | None = None) -> None:
    c0, c1, bits = struct.unpack_from("<HHI", data, offset)
    r0, g0, b0 = _rgb565(c0)
    r1, g1, b1 = _rgb565(c1)
    if c0 > c1 or alpha_from is not None:
        palette = [
            (r0, g0, b0, 255), (r1, g1, b1, 255),
            ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255),
            ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255),
        ]
    else:
        palette = [
            (r0, g0, b0, 255), (r1, g1, b1, 255),
            ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255), (0, 0, 0, 0),
        ]
    for row in range(4):
        y = y0 + row
        if y >= height:
            break
        for col in range(4):
            x = x0 + col
            if x >= width:
                continue
            r, g, b, a = palette[(bits >> (2 * (row * 4 + col))) & 3]
            if alpha_from is not None:
                a = alpha_from[row * 4 + col]
            position = (y * width + x) * 4
            out[position:position + 4] = bytes((r, g, b, a))


def _bc4_channel(data: bytes, offset: int) -> list[int]:
    a0, a1 = data[offset], data[offset + 1]
    bits = int.from_bytes(data[offset + 2:offset + 8], "little")
    if a0 > a1:
        palette = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    else:
        palette = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
    return [palette[(bits >> (3 * i)) & 7] for i in range(16)]


def decode_block_compressed(data: bytes, width: int, height: int, fmt: str) -> bytearray:
    """Decode BC1/BC2/BC3/BC4/BC5 to RGBA8 (BC4 → gray, BC5 → RG)."""

    fmt = fmt.upper()
    if fmt == "BC7":
        from .bc7 import decode_bc7

        try:
            return decode_bc7(data, width, height)
        except ValueError as exc:
            raise ImageDecodeError(str(exc)) from exc
    block_bytes = {"BC1": 8, "DXT1": 8, "BC2": 16, "DXT3": 16, "BC3": 16, "DXT5": 16, "BC4": 8, "BC5": 16}.get(fmt)
    if block_bytes is None:
        raise ImageDecodeError("unsupported block format %s" % fmt)
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    needed = blocks_x * blocks_y * block_bytes
    if len(data) < needed:
        raise ImageDecodeError("%s payload is %d bytes, %dx%d needs %d" % (fmt, len(data), width, height, needed))
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(blocks_y):
        for bx in range(blocks_x):
            x0, y0 = bx * 4, by * 4
            if fmt in ("BC1", "DXT1"):
                _bc1_block(data, offset, out, x0, y0, width, height)
            elif fmt in ("BC2", "DXT3"):
                alpha_bits = int.from_bytes(data[offset:offset + 8], "little")
                alpha = bytes(((alpha_bits >> (4 * i)) & 0xF) * 17 for i in range(16))
                _bc1_block(data, offset + 8, out, x0, y0, width, height, alpha_from=alpha)
            elif fmt in ("BC3", "DXT5"):
                alpha = bytes(_bc4_channel(data, offset))
                _bc1_block(data, offset + 8, out, x0, y0, width, height, alpha_from=alpha)
            elif fmt == "BC4":
                red = _bc4_channel(data, offset)
                for i in range(16):
                    x, y = x0 + i % 4, y0 + i // 4
                    if x < width and y < height:
                        position = (y * width + x) * 4
                        out[position:position + 4] = bytes((red[i], red[i], red[i], 255))
            else:  # BC5
                red = _bc4_channel(data, offset)
                green = _bc4_channel(data, offset + 8)
                for i in range(16):
                    x, y = x0 + i % 4, y0 + i // 4
                    if x < width and y < height:
                        position = (y * width + x) * 4
                        out[position:position + 4] = bytes((red[i], green[i], 0, 255))
            offset += block_bytes
    return out


_UNCOMPRESSED = {
    "B8G8R8A8": (4, (2, 1, 0, 3)), "R8G8B8A8": (4, (0, 1, 2, 3)), "A8R8G8B8": (4, (1, 2, 3, 0)),
    "R8G8B8": (3, (0, 1, 2, None)), "B8G8R8": (3, (2, 1, 0, None)),
    "G8": (1, (0, 0, 0, None)), "R8": (1, (0, 0, 0, None)), "A8": (1, (None, None, None, 0)),
    "R8G8": (2, (0, 1, None, None)), "G16": (2, (1, 1, 1, None)),
}


def decode_uncompressed(data: bytes, width: int, height: int, fmt: str) -> bytearray:
    spec = _UNCOMPRESSED.get(fmt.upper())
    if spec is None:
        raise ImageDecodeError("unsupported uncompressed format %s" % fmt)
    stride, order = spec
    needed = width * height * stride
    if len(data) < needed:
        raise ImageDecodeError("%s payload is %d bytes, %dx%d needs %d" % (fmt, len(data), width, height, needed))
    out = bytearray(width * height * 4)
    for index in range(width * height):
        source = index * stride
        pixel = []
        for channel in order:
            if channel is None:
                pixel.append(255 if len(pixel) == 3 else 0)
            else:
                pixel.append(data[source + channel])
        out[index * 4:index * 4 + 4] = bytes(pixel)
    return out


# ---------------------------------------------------------------- PNG / DDS

def decode_png(data: bytes) -> tuple[bytearray, int, int]:
    """Minimal PNG decoder: 8-bit gray/RGB/RGBA/palette/gray+alpha, no interlace."""

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageDecodeError("not a PNG")
    position = 8
    width = height = 0
    bit_depth = color_type = interlace = 0
    palette = b""
    idat = bytearray()
    while position + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        chunk = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk[:13])
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if width <= 0 or height <= 0 or width * height > 4096 * 4096:
        raise ImageDecodeError("PNG dimensions are invalid")
    if bit_depth != 8 or interlace:
        raise ImageDecodeError("PNG bit depth %d / interlace %d not supported" % (bit_depth, interlace))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ImageDecodeError("PNG colour type %d not supported" % color_type)
    stride = width * channels
    expected_bytes = (stride + 1) * height
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(idat), expected_bytes + 1)
    if len(raw) > expected_bytes or decoder.unconsumed_tail:
        raise ImageDecodeError("PNG image data exceeds its declared dimensions")
    if len(raw) < expected_bytes or not decoder.eof:
        raise ImageDecodeError("PNG image data is truncated")
    previous = bytearray(stride)
    pixels = bytearray()
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        if filter_type > 4:
            raise ImageDecodeError("PNG filter type is invalid")
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += stride + 1
        for i in range(stride) if filter_type else ():
            a = line[i - channels] if i >= channels else 0
            b = previous[i]
            c = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + predictor) & 0xFF
        pixels += line
        previous = line
    out = bytearray(width * height * 4)
    for index in range(width * height):
        source = index * channels
        if color_type == 0:
            g = pixels[source]
            out[index * 4:index * 4 + 4] = bytes((g, g, g, 255))
        elif color_type == 2:
            out[index * 4:index * 4 + 4] = bytes(pixels[source:source + 3]) + b"\xff"
        elif color_type == 3:
            entry = pixels[source] * 3
            out[index * 4:index * 4 + 4] = bytes(palette[entry:entry + 3]) + b"\xff" if entry + 3 <= len(palette) else b"\x00\x00\x00\xff"
        elif color_type == 4:
            g, a = pixels[source], pixels[source + 1]
            out[index * 4:index * 4 + 4] = bytes((g, g, g, a))
        else:
            out[index * 4:index * 4 + 4] = bytes(pixels[source:source + 4])
    return out, width, height


def decode_dds(data: bytes) -> tuple[bytearray, int, int, str]:
    """Decode the top mip of a DDS file (BC1-5 or 32-bit RGBA)."""

    if data[:4] != b"DDS " or len(data) < 128:
        raise ImageDecodeError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88]
    payload = 128
    fmt = ""
    if fourcc == b"DX10":
        dxgi = struct.unpack_from("<I", data, 128)[0]
        payload = 148
        fmt = {71: "BC1", 74: "BC2", 77: "BC3", 80: "BC4", 83: "BC5", 98: "BC7", 28: "R8G8B8A8", 87: "B8G8R8A8"}.get(dxgi, "")
        if not fmt:
            raise ImageDecodeError("DDS DXGI format %d not supported" % dxgi)
    elif fourcc in (b"DXT1", b"DXT3", b"DXT5", b"ATI1", b"BC4U", b"ATI2", b"BC5U"):
        fmt = {b"DXT1": "BC1", b"DXT3": "BC2", b"DXT5": "BC3", b"ATI1": "BC4", b"BC4U": "BC4",
               b"ATI2": "BC5", b"BC5U": "BC5"}[fourcc]
    else:
        rgb_bits = struct.unpack_from("<I", data, 88)[0]
        masks = struct.unpack_from("<IIII", data, 92)
        if rgb_bits == 32 and masks[0] == 0x00FF0000:
            fmt = "B8G8R8A8"
        elif rgb_bits == 32 and masks[0] == 0x000000FF:
            fmt = "R8G8B8A8"
        elif rgb_bits == 8:
            fmt = "G8"
        else:
            raise ImageDecodeError("DDS pixel format not supported")
    if width * height > 4096 * 4096:
        raise ImageDecodeError("DDS dimensions are invalid")
    if fmt in ("BC1", "BC2", "BC3", "BC4", "BC5", "BC7"):
        return decode_block_compressed(data[payload:], width, height, fmt), width, height, fmt
    return decode_uncompressed(data[payload:], width, height, fmt), width, height, fmt


# ---------------------------------------------------------------- thumbnails and hashes

def gray_thumbnail(rgba: Sequence[int], width: int, height: int, size: int = THUMB) -> tuple[bytes, tuple[int, int, int, int]]:
    """Box-filter to ``size``x``size`` luminance; also returns the mean RGBA."""

    if width <= 0 or height <= 0 or len(rgba) < width * height * 4:
        raise ImageDecodeError("image buffer is smaller than its dimensions")
    sums = [0, 0, 0, 0]
    total = width * height
    luma = bytearray(total)
    for index in range(total):
        base = index * 4
        r, g, b, a = rgba[base], rgba[base + 1], rgba[base + 2], rgba[base + 3]
        sums[0] += r; sums[1] += g; sums[2] += b; sums[3] += a
        luma[index] = (299 * r + 587 * g + 114 * b) // 1000
    thumb = bytearray(size * size)
    # Each thumbnail cell averages its source box; boxes are at least one
    # pixel so images smaller than the thumbnail are upsampled, not holed.
    for cy in range(size):
        y0 = cy * height // size
        y1 = max(y0 + 1, (cy + 1) * height // size)
        for cx in range(size):
            x0 = cx * width // size
            x1 = max(x0 + 1, (cx + 1) * width // size)
            acc = count = 0
            for y in range(y0, min(y1, height)):
                row = y * width
                for x in range(x0, min(x1, width)):
                    acc += luma[row + x]
                    count += 1
            thumb[cy * size + cx] = acc // count if count else 0
    mean = tuple(value // total for value in sums)
    return bytes(thumb), mean  # type: ignore[return-value]


def _transforms(gray: bytes, size: int = THUMB) -> list[bytes]:
    """The eight dihedral variants (identity, rotations, mirrors)."""

    grid = [list(gray[y * size:(y + 1) * size]) for y in range(size)]

    def rotate(g):
        return [[g[size - 1 - x][y] for x in range(size)] for y in range(size)]

    def mirror(g):
        return [list(reversed(row)) for row in g]

    variants = []
    current = grid
    for _ in range(4):
        variants.append(current)
        variants.append(mirror(current))
        current = rotate(current)
    return [bytes(value for row in variant for value in row) for variant in variants]


def dhash(gray: bytes, size: int = THUMB) -> int:
    """64-bit difference hash from a square grayscale thumbnail (9x8 grid)."""

    grid = []
    for row in range(8):
        y0, y1 = row * size // 8, (row + 1) * size // 8
        line = []
        for col in range(9):
            x0, x1 = col * size // 9, (col + 1) * size // 9
            if x1 <= x0:
                x1 = x0 + 1
            total = count = 0
            for y in range(y0, y1):
                for x in range(x0, min(x1, size)):
                    total += gray[y * size + x]
                    count += 1
            line.append(total / count if count else 0)
        grid.append(line)
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (1 if grid[row][col] < grid[row][col + 1] else 0)
    return value


def dhash_variants(gray: bytes, size: int = THUMB) -> list[int]:
    return [dhash(variant, size) for variant in _transforms(gray, size)]


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


_TRANSFORM_NAMES = ("identity", "mirror-x", "rotate-90", "rotate-90+mirror", "rotate-180",
                    "mirror-y", "rotate-270", "rotate-270+mirror")


def compare_hashes(query_hash: int, candidate_variants: Sequence[int]) -> dict[str, Any]:
    """Best dihedral match of one query hash against a candidate's 8 variants."""

    best = min(range(len(candidate_variants)), key=lambda index: hamming(query_hash, candidate_variants[index]))
    distance = hamming(query_hash, candidate_variants[best])
    return {
        "distance": distance,
        "transform": _TRANSFORM_NAMES[best] if best < len(_TRANSFORM_NAMES) else str(best),
        "verdict": "same-image" if distance <= HASH_STRONG else ("plausible" if distance <= HASH_PLAUSIBLE else "different"),
    }


def fingerprint(rgba: Sequence[int], width: int, height: int) -> dict[str, Any]:
    """Thumbnail + mean colour + dihedral dHash set for one decoded image."""

    thumb, mean = gray_thumbnail(rgba, width, height)
    variants = dhash_variants(thumb)
    flat = (thumb.count(thumb[0]) == len(thumb)) if thumb else True
    return {
        "thumbnail": base64.b64encode(thumb).decode("ascii"),
        "thumbnailSize": THUMB,
        "meanColor": list(mean),
        "dhash": variants[0],
        "dhashVariants": variants,
        "flat": flat,  # a uniform image matches everything; never treat as identity
        "width": width,
        "height": height,
    }


def fingerprint_from_thumbnail(thumbnail_b64: str) -> dict[str, Any]:
    thumb = base64.b64decode(thumbnail_b64)
    if len(thumb) != THUMB * THUMB:
        raise ImageDecodeError("thumbnail must be %d bytes" % (THUMB * THUMB))
    variants = dhash_variants(thumb)
    return {"thumbnail": thumbnail_b64, "thumbnailSize": THUMB, "dhash": variants[0], "dhashVariants": variants,
            "flat": thumb.count(thumb[0]) == len(thumb)}
