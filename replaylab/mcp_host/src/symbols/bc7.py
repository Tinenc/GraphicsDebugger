"""Pure-Python BC7 block decoder (all eight modes) for thumbnail fingerprints.

Correctness over speed: a 32x32 mip is 64 blocks, well under a millisecond
of decode budget per texture.  Tables follow the D3D11 BC7 specification.
"""

from __future__ import annotations

_PARTITION2 = [
    [0,0,1,1,0,0,1,1,0,0,1,1,0,0,1,1],[0,0,0,1,0,0,0,1,0,0,0,1,0,0,0,1],[0,1,1,1,0,1,1,1,0,1,1,1,0,1,1,1],[0,0,0,1,0,0,1,1,0,1,1,1,1,1,1,1],
    [0,0,0,0,0,0,0,1,0,0,0,1,0,0,1,1],[0,0,1,1,0,1,1,1,0,1,1,1,1,1,1,1],[0,0,0,1,0,0,1,1,0,1,1,1,1,1,1,1],[0,0,0,0,0,0,0,1,0,0,1,1,0,1,1,1],
    [0,0,0,0,0,0,0,0,0,0,0,1,0,0,1,1],[0,0,1,1,0,1,1,1,1,1,1,1,1,1,1,1],[0,0,0,0,0,0,0,1,0,1,1,1,1,1,1,1],[0,0,0,0,0,0,0,0,0,0,0,1,0,1,1,1],
    [0,0,0,1,0,1,1,1,1,1,1,1,1,1,1,1],[0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1],[0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1],[0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1],
    [0,0,0,0,1,0,0,0,1,1,1,0,1,1,1,1],[0,1,1,1,0,0,0,1,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,1,0,0,0,1,1,1,0],[0,1,1,1,0,0,1,1,0,0,0,1,0,0,0,0],
    [0,0,1,1,0,0,0,1,0,0,0,0,0,0,0,0],[0,0,0,0,1,0,0,0,1,1,0,0,1,1,1,0],[0,0,0,0,0,0,0,0,1,0,0,0,1,1,0,0],[0,1,1,1,0,0,1,1,0,0,1,1,0,0,0,1],
    [0,0,1,1,0,0,0,1,0,0,0,1,0,0,0,0],[0,0,0,0,1,0,0,0,1,0,0,0,1,1,0,0],[0,1,1,0,0,1,1,0,0,1,1,0,0,1,1,0],[0,0,1,1,0,1,1,0,0,1,1,0,1,1,0,0],
    [0,0,0,1,0,1,1,1,1,1,1,0,1,0,0,0],[0,0,0,0,1,1,1,1,1,1,1,1,0,0,0,0],[0,1,1,1,0,0,0,1,1,0,0,0,1,1,1,0],[0,0,1,1,1,0,0,1,1,0,0,1,1,1,0,0],
    [0,1,0,1,0,1,0,1,0,1,0,1,0,1,0,1],[0,0,0,0,1,1,1,1,0,0,0,0,1,1,1,1],[0,1,0,1,1,0,1,0,0,1,0,1,1,0,1,0],[0,0,1,1,0,0,1,1,1,1,0,0,1,1,0,0],
    [0,0,1,1,1,1,0,0,0,0,1,1,1,1,0,0],[0,1,0,1,0,1,0,1,1,0,1,0,1,0,1,0],[0,1,1,0,1,0,0,1,0,1,1,0,1,0,0,1],[0,1,0,1,1,0,1,0,1,0,1,0,0,1,0,1],
    [0,1,1,1,0,0,1,1,1,1,0,0,1,1,1,0],[0,0,0,1,0,0,1,1,1,1,0,0,1,0,0,0],[0,0,1,1,0,0,1,0,0,1,0,0,1,1,0,0],[0,0,1,1,1,0,1,1,1,1,0,1,1,1,0,0],
    [0,1,1,0,1,0,0,1,1,0,0,1,0,1,1,0],[0,0,1,1,1,1,0,0,1,1,0,0,0,0,1,1],[0,1,1,0,0,1,1,0,1,0,0,1,1,0,0,1],[0,0,0,0,0,1,1,0,0,1,1,0,0,0,0,0],
    [0,1,0,0,1,1,1,0,0,1,0,0,0,0,0,0],[0,0,1,0,0,1,1,1,0,0,1,0,0,0,0,0],[0,0,0,0,0,0,1,0,0,1,1,1,0,0,1,0],[0,0,0,0,0,1,0,0,1,1,1,0,0,1,0,0],
    [0,1,1,0,1,1,0,0,1,0,0,1,0,0,1,1],[0,0,1,1,0,1,1,0,1,1,0,0,1,0,0,1],[0,1,1,0,0,0,1,1,1,0,0,1,1,1,0,0],[0,0,1,1,1,0,0,1,1,1,0,0,0,1,1,0],
    [0,1,1,0,1,1,0,0,1,1,0,0,1,0,0,1],[0,1,1,0,0,0,1,1,0,0,1,1,1,0,0,1],[0,1,1,1,1,1,1,0,1,0,0,0,0,0,0,1],[0,0,0,1,1,0,0,0,1,1,1,0,0,1,1,1],
    [0,0,0,0,1,1,1,1,0,0,1,1,0,0,1,1],[0,0,1,1,0,0,1,1,1,1,1,1,0,0,0,0],[0,0,1,0,0,0,1,0,1,1,1,0,1,1,1,0],[0,1,0,0,0,1,0,0,0,1,1,1,0,1,1,1],
]
_PARTITION3 = [
    [0,0,1,1,0,0,1,1,0,2,2,1,2,2,2,2],[0,0,0,1,0,0,1,1,2,2,1,1,2,2,2,1],[0,0,0,0,2,0,0,1,2,2,1,1,2,2,1,1],[0,2,2,2,0,0,2,2,0,0,1,1,0,1,1,1],
    [0,0,0,0,0,0,0,0,1,1,2,2,1,1,2,2],[0,0,1,1,0,0,1,1,0,0,2,2,0,0,2,2],[0,0,2,2,0,0,2,2,1,1,1,1,1,1,1,1],[0,0,1,1,0,0,1,1,2,2,1,1,2,2,1,1],
    [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2],[0,0,0,0,1,1,1,1,1,1,1,1,2,2,2,2],[0,0,0,0,1,1,1,1,2,2,2,2,2,2,2,2],[0,0,1,2,0,0,1,2,0,0,1,2,0,0,1,2],
    [0,1,1,2,0,1,1,2,0,1,1,2,0,1,1,2],[0,1,2,2,0,1,2,2,0,1,2,2,0,1,2,2],[0,0,1,1,0,1,1,2,1,1,2,2,1,2,2,2],[0,0,1,1,2,0,0,1,2,2,0,0,2,2,2,0],
    [0,0,0,1,0,0,1,1,0,1,1,2,1,1,2,2],[0,1,1,1,0,0,1,1,2,0,0,1,2,2,0,0],[0,0,0,0,1,1,2,2,1,1,2,2,1,1,2,2],[0,0,2,2,0,0,2,2,0,0,2,2,1,1,1,1],
    [0,1,1,1,0,1,1,1,0,2,2,2,0,2,2,2],[0,0,0,1,0,0,0,1,2,2,2,1,2,2,2,1],[0,0,0,0,0,0,1,1,0,1,2,2,0,1,2,2],[0,0,0,0,1,1,0,0,2,2,1,0,2,2,1,0],
    [0,1,2,2,0,1,2,2,0,0,1,1,0,0,0,0],[0,0,1,2,0,0,1,2,1,1,2,2,2,2,2,2],[0,1,1,0,1,2,2,1,1,2,2,1,0,1,1,0],[0,0,0,0,0,1,1,0,1,2,2,1,1,2,2,1],
    [0,0,2,2,1,1,0,2,1,1,0,2,0,0,2,2],[0,1,1,0,0,1,1,0,2,0,0,2,2,2,2,2],[0,0,1,1,0,1,2,2,0,1,2,2,0,0,1,1],[0,0,0,0,2,0,0,0,2,2,1,1,2,2,2,1],
    [0,0,0,0,0,0,0,2,1,1,2,2,1,2,2,2],[0,2,2,2,0,0,2,2,0,0,1,2,0,0,1,1],[0,0,1,1,0,0,1,2,0,0,2,2,0,2,2,2],[0,1,2,0,0,1,2,0,0,1,2,0,0,1,2,0],
    [0,0,0,0,1,1,1,1,2,2,2,2,0,0,0,0],[0,1,2,0,1,2,0,1,2,0,1,2,0,1,2,0],[0,1,2,0,2,0,1,2,1,2,0,1,0,1,2,0],[0,0,1,1,2,2,0,0,1,1,2,2,0,0,1,1],
    [0,0,1,1,1,1,2,2,2,2,0,0,0,0,1,1],[0,1,0,1,0,1,0,1,2,2,2,2,2,2,2,2],[0,0,0,0,0,0,0,0,2,1,2,1,2,1,2,1],[0,0,2,2,1,1,2,2,0,0,2,2,1,1,2,2],
    [0,0,2,2,0,0,1,1,0,0,2,2,0,0,1,1],[0,2,2,0,1,2,2,1,0,2,2,0,1,2,2,1],[0,1,0,1,2,2,2,2,2,2,2,2,0,1,0,1],[0,0,0,0,2,1,2,1,2,1,2,1,2,1,2,1],
    [0,1,0,1,0,1,0,1,0,1,0,1,2,2,2,2],[0,2,2,2,0,1,1,1,0,2,2,2,0,1,1,1],[0,0,0,2,1,1,1,2,0,0,0,2,1,1,1,2],[0,0,0,0,2,1,1,2,2,1,1,2,2,1,1,2],
    [0,2,2,2,0,1,1,1,0,1,1,1,0,2,2,2],[0,0,0,2,1,1,1,2,1,1,1,2,0,0,0,2],[0,1,1,0,0,1,1,0,0,1,1,0,2,2,2,2],[0,0,0,0,0,0,0,0,2,1,1,2,2,1,1,2],
    [0,1,1,0,0,1,1,0,2,2,2,2,2,2,2,2],[0,0,2,2,0,0,1,1,0,0,1,1,0,0,2,2],[0,0,2,2,1,1,2,2,1,1,2,2,0,0,2,2],[0,0,0,0,0,0,0,0,0,0,0,0,2,1,1,2],
    [0,0,0,2,0,0,0,1,0,0,0,2,0,0,0,1],[0,2,2,2,1,2,2,2,0,2,2,2,1,2,2,2],[0,1,0,1,2,2,2,2,2,2,2,2,2,2,2,2],[0,1,1,1,2,0,1,1,2,2,0,1,2,2,2,0],
]
_ANCHOR2 = [15,15,15,15,15,15,15,15,15,15,15,15,15,15,15,15,15,2,8,2,2,8,8,15,2,8,2,2,8,8,2,2,15,15,6,8,2,8,15,15,2,8,2,2,2,15,15,6,6,2,6,8,15,15,2,2,15,15,15,15,15,2,2,15]
_ANCHOR3_A = [3,3,15,15,8,3,15,15,8,8,6,6,6,5,3,3,3,3,8,15,3,3,6,10,5,8,8,6,8,5,15,15,8,15,3,5,6,10,8,15,15,3,15,5,15,15,15,15,3,15,5,5,5,8,5,10,5,10,8,13,15,12,3,3]
_ANCHOR3_B = [15,8,8,3,15,15,3,8,15,15,15,15,15,15,15,8,15,8,15,3,15,8,15,8,3,15,6,10,15,15,10,8,15,3,15,10,10,8,9,10,6,15,8,15,3,6,6,8,15,3,15,15,15,15,15,15,15,15,15,15,3,15,15,8]
_WEIGHTS = {2: [0, 21, 43, 64], 3: [0, 9, 18, 27, 37, 46, 55, 64],
            4: [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64]}
# mode: (subsets, partition bits, rotation bits, index selection bit, colour bits, alpha bits,
#        endpoint p-bits, shared p-bits, index bits, secondary index bits)
_MODES = {
    0: (3, 4, 0, 0, 4, 0, 1, 0, 3, 0), 1: (2, 6, 0, 0, 6, 0, 0, 1, 3, 0), 2: (3, 6, 0, 0, 5, 0, 0, 0, 2, 0),
    3: (2, 6, 0, 0, 7, 0, 1, 0, 2, 0), 4: (1, 0, 2, 1, 5, 6, 0, 0, 2, 3), 5: (1, 0, 2, 0, 7, 8, 0, 0, 2, 2),
    6: (1, 0, 0, 0, 7, 7, 1, 0, 4, 0), 7: (2, 6, 0, 0, 5, 5, 1, 0, 2, 0),
}


class _Bits:
    __slots__ = ("value", "position")

    def __init__(self, block: bytes):
        self.value = int.from_bytes(block, "little")
        self.position = 0

    def read(self, count: int) -> int:
        if count <= 0:
            return 0
        result = (self.value >> self.position) & ((1 << count) - 1)
        self.position += count
        return result


def _expand(value: int, bits: int) -> int:
    if bits >= 8:
        return value
    value <<= 8 - bits
    return value | (value >> bits)


def _interpolate(a: int, b: int, weight: int) -> int:
    return (a * (64 - weight) + b * weight + 32) >> 6


def decode_bc7_block(block: bytes) -> list[tuple[int, int, int, int]]:
    """Decode one 16-byte BC7 block into 16 RGBA tuples (row-major)."""

    bits = _Bits(block)
    mode = 0
    while mode < 8 and not bits.read(1):
        mode += 1
    if mode >= 8:
        return [(0, 0, 0, 0)] * 16
    subsets, part_bits, rot_bits, sel_bits, cbits, abits, ep_pbits, shared_pbits, ibits, ibits2 = _MODES[mode]
    partition = bits.read(part_bits)
    rotation = bits.read(rot_bits)
    index_selection = bits.read(sel_bits)
    count = subsets * 2
    endpoints = [[0, 0, 0, 0] for _ in range(count)]
    for channel in range(3):
        for index in range(count):
            endpoints[index][channel] = bits.read(cbits)
    if abits:
        for index in range(count):
            endpoints[index][3] = bits.read(abits)
    if ep_pbits:
        for index in range(count):
            p = bits.read(1)
            for channel in range(4):
                if channel < 3 or abits:
                    endpoints[index][channel] = (endpoints[index][channel] << 1) | p
    if shared_pbits:
        for subset in range(subsets):
            p = bits.read(1)
            for index in (subset * 2, subset * 2 + 1):
                for channel in range(3):
                    endpoints[index][channel] = (endpoints[index][channel] << 1) | p
    total_cbits = cbits + (1 if ep_pbits or shared_pbits else 0)
    total_abits = (abits + (1 if ep_pbits else 0)) if abits else 0
    for endpoint in endpoints:
        for channel in range(3):
            endpoint[channel] = _expand(endpoint[channel], total_cbits)
        endpoint[3] = _expand(endpoint[3], total_abits) if abits else 255
    if subsets == 1:
        subset_of = [0] * 16
        anchors = [0]
    elif subsets == 2:
        subset_of = _PARTITION2[partition]
        anchors = [0, _ANCHOR2[partition]]
    else:
        subset_of = _PARTITION3[partition]
        anchors = [0, _ANCHOR3_A[partition], _ANCHOR3_B[partition]]
    primary = []
    for pixel in range(16):
        width = ibits - 1 if pixel in anchors else ibits
        primary.append(bits.read(width))
    secondary = []
    if ibits2:
        for pixel in range(16):
            width = ibits2 - 1 if pixel == 0 else ibits2
            secondary.append(bits.read(width))
    result = []
    for pixel in range(16):
        subset = subset_of[pixel]
        low, high = endpoints[subset * 2], endpoints[subset * 2 + 1]
        color_index, alpha_index = primary[pixel], (secondary[pixel] if ibits2 else primary[pixel])
        color_bits, alpha_bits = ibits, (ibits2 if ibits2 else ibits)
        if ibits2 and index_selection:
            color_index, alpha_index = alpha_index, color_index
            color_bits, alpha_bits = alpha_bits, color_bits
        weight_c = _WEIGHTS[color_bits][color_index]
        weight_a = _WEIGHTS[alpha_bits][alpha_index]
        r = _interpolate(low[0], high[0], weight_c)
        g = _interpolate(low[1], high[1], weight_c)
        b = _interpolate(low[2], high[2], weight_c)
        a = _interpolate(low[3], high[3], weight_a) if abits else 255
        if rotation == 1:
            r, a = a, r
        elif rotation == 2:
            g, a = a, g
        elif rotation == 3:
            b, a = a, b
        result.append((r, g, b, a))
    return result


def decode_bc7(data: bytes, width: int, height: int) -> bytearray:
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    if len(data) < blocks_x * blocks_y * 16:
        raise ValueError("BC7 payload is truncated")
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(blocks_y):
        for bx in range(blocks_x):
            texels = decode_bc7_block(data[offset:offset + 16])
            offset += 16
            for i, (r, g, b, a) in enumerate(texels):
                x, y = bx * 4 + i % 4, by * 4 + i // 4
                if x < width and y < height:
                    position = (y * width + x) * 4
                    out[position:position + 4] = bytes((r, g, b, a))
    return out
