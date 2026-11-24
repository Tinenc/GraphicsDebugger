"""Bounded, read-only scalar presentation of complete little-endian R32_UINT data.

The caller owns exact EID/subresource selection and PNG publication. This module
does not infer a texture's meaning: float bit reinterpretation must be explicit.
No per-pixel Python list is retained, including for a large sparse atlas.
"""

from array import array
import hashlib
import math
import struct
import sys

from .texture_preview_range import select_visibility_range


def _float_from_ordered(key):
    bits = (key ^ 0x80000000) if key & 0x80000000 else (~key & 0xFFFFFFFF)
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def decode_r32_uint_grayscale(raw, width, height, value_decode="float32-bits",
                              range_mode="visible-distribution", black_point=None,
                              white_point=None):
    """Return ``(gray8, display_patch)``; gray8 is None for a flat min/max range.

    Statistics cover every texel, before any presentation crop/resize. Quantile
    bounds use a fixed 65536-bin monotonic IEEE-754 histogram, not spatial pixel
    sampling; bin boundary error is disclosed in the receipt. Nonfinite values
    render black. Signed zero is valid background and excluded only from the
    distribution used to choose the visible range. The source stays untouched.
    """
    if (type(width) is not int or type(height) is not int or
            width <= 0 or height <= 0 or width * height > 32 * 1024 * 1024):
        raise ValueError("R32 decoding requires positive dimensions within 32 Mpix")
    if value_decode != "float32-bits":
        raise ValueError("the scalar decoder requires explicit float32-bits")
    if range_mode not in ("native", "explicit", "subresource-minmax", "visible-distribution"):
        raise ValueError("unsupported decoded texture range mode")
    if range_mode == "explicit":
        if (isinstance(black_point, bool) or isinstance(white_point, bool) or
                not isinstance(black_point, (int, float)) or
                not isinstance(white_point, (int, float)) or
                not math.isfinite(black_point) or not math.isfinite(white_point) or
                black_point >= white_point):
            raise ValueError("decoded explicit range must be finite and increasing")
        black_point, white_point = float(black_point), float(white_point)
    elif black_point is not None or white_point is not None:
        raise ValueError("decoded black/white points require explicit range")
    try:
        data = memoryview(raw)
        if not data.c_contiguous:
            raise ValueError("R32 bytes must be contiguous")
        data = data.cast("B")
    except (TypeError, AttributeError):
        raise ValueError("R32 decoding requires a contiguous byte buffer")
    count = width * height
    if len(data) != count * 4:
        raise ValueError("R32 data must contain exactly width * height * 4 bytes")

    # RenderDoc's supported capture platforms store these components little-endian.
    # memoryview uses native byte order, so explicitly handle the other host order.
    if sys.byteorder == "little":
        words = data.cast("I")
        floats = data.cast("f")
    else:
        words = array("I")
        words.frombytes(data.tobytes())
        words.byteswap()
        floats = memoryview(words).cast("B").cast("f")
    histogram = array("Q", [0]) * 65536
    minimum_key, maximum_key = 0xFFFFFFFF, 0
    finite_count, zero_count, nan_count, infinity_count, native_count = 0, 0, 0, 0, 0
    for bits in words:
        magnitude = bits & 0x7FFFFFFF
        if magnitude >= 0x7F800000:
            if magnitude == 0x7F800000:
                infinity_count += 1
            else:
                nan_count += 1
            continue
        finite_count += 1
        ordered = (~bits & 0xFFFFFFFF) if bits & 0x80000000 else (bits ^ 0x80000000)
        if ordered < minimum_key:
            minimum_key = ordered
        if ordered > maximum_key:
            maximum_key = ordered
        if magnitude == 0:
            zero_count += 1
        else:
            histogram[ordered >> 16] += 1
            if not bits & 0x80000000 and magnitude <= 0x3F800000:
                native_count += 1
    finite_min = _float_from_ordered(minimum_key) if finite_count else None
    finite_max = _float_from_ordered(maximum_key) if finite_count else None
    nonzero_count = finite_count - zero_count
    decoding = {
        "schema": "bread-texture-value-decode/v1", "sourceFormat": "R32_UINT",
        "interpretation": "ieee754-float32-little-endian", "pixelCount": count,
        "finiteCount": finite_count, "nonFiniteCount": nan_count + infinity_count,
        "nanCount": nan_count, "infinityCount": infinity_count, "zeroCount": zero_count,
        "nonZeroFiniteCount": nonzero_count, "sourceBytes": len(data),
        "sourceSha256": hashlib.sha256(data).hexdigest(), "fullSubresource": True,
        "sourceBytesModified": False, "rangeMethod": range_mode,
        "finiteMinimum": finite_min, "finiteMaximum": finite_max,
        "nonFiniteDisplay": "black", "histogramBins": 65536,
        "histogramKey": "ordered-ieee754-high16", "percentileLow": 0.005,
        "percentileHigh": 0.995,
    }
    display = {"valueDecode": value_decode, "decoding": decoding,
               "subresourceMin": None, "subresourceMax": None}
    if range_mode == "native":
        black_point, white_point = 0.0, 1.0
    elif range_mode in ("subresource-minmax", "visible-distribution"):
        display["subresourceMin"] = [finite_min]
        display["subresourceMax"] = [finite_max]
        black_point, white_point = finite_min, finite_max
        if range_mode == "visible-distribution":
            segments = []
            if nonzero_count:
                low_rank = int((nonzero_count - 1) * 0.005)
                high_rank = int((nonzero_count - 1) * 0.995)
                accumulated, lower_bin, upper_bin = 0, None, None
                for index, population in enumerate(histogram):
                    accumulated += population
                    if lower_bin is None and accumulated > low_rank:
                        lower_bin = index
                    if accumulated > high_rank:
                        upper_bin = index
                        break
                # Quantile bounds bracket whole histogram bins and are clamped
                # to exact finite extrema; even the extreme finite bins stay finite.
                lower_key = max(minimum_key, lower_bin << 16)
                upper_key = min(maximum_key, (upper_bin << 16) | 0xFFFF)
                black_point = _float_from_ordered(lower_key)
                white_point = _float_from_ordered(upper_key)
            if black_point is None or white_point is None:
                black_point, white_point = 0.0, 1.0
            lower_quantile, upper_quantile = black_point, white_point
            if black_point >= white_point:
                if black_point > 0:
                    black_point = 0.0
                elif white_point < 0:
                    white_point = 0.0
                else:
                    black_point, white_point = 0.0, 1.0
            if nonzero_count:
                segments.append({"minimum": black_point, "maximum": white_point,
                                 "sampleCount": nonzero_count})
            display["distribution"] = {
                "method": "whole-subresource-float32-bits-histogram-v1",
                "channels": [True, False, False, False],
                "percentiles": [0.005, 0.995], "excludedZeroComponents": True,
                "excludedNonFinite": True, "histogramCalls": 0,
                "sampleCount": nonzero_count, "segments": segments,
            }
            distribution = display["distribution"]
            if nonzero_count:
                black_point, white_point = select_visibility_range(
                    black_point, white_point, lower_quantile, upper_quantile,
                    1.0 - native_count / float(nonzero_count), distribution)
            else:
                black_point, white_point = 0.0, 1.0
                distribution.update(rangePolicy="native-unless-unreadable-v1",
                                    rangeDecision="native-empty")
    display["blackPoint"], display["whitePoint"] = black_point, white_point
    if (black_point is None or white_point is None or black_point >= white_point):
        return None, display
    scale = 255.0 / (white_point - black_point)
    gray = bytearray(count)
    for index, value in enumerate(floats):
        if not math.isfinite(value) or value <= black_point:
            continue
        if value >= white_point:
            gray[index] = 255
        else:
            gray[index] = int((value - black_point) * scale + 0.5)
    return gray, display
