"""Full-subresource float-bit presentation, without RenderDoc/Qt dependencies."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import types

import pytest

PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/texture_value_decode.py"
PACKAGE = types.ModuleType("_bread_preview_test_bridge")
PACKAGE.__path__ = [str(PATH.parent)]
sys.modules[PACKAGE.__name__] = PACKAGE
SPEC = importlib.util.spec_from_file_location(
    PACKAGE.__name__ + ".texture_value_decode", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def decode(values, **kwargs):
    raw = struct.pack("<%df" % len(values), *values)
    gray, display = MODULE.decode_r32_uint_grayscale(raw, len(values), 1, **kwargs)
    assert display["decoding"]["sourceSha256"] == hashlib.sha256(raw).hexdigest()
    assert display["decoding"]["sourceBytes"] == len(raw)
    return gray, display


def test_bit_interpretation_signed_finite_nonfinite_and_exact_range():
    gray, display = decode([-1, -0.0, 0.0, 1, float("nan"), float("inf"), -float("inf")],
                           range_mode="explicit", black_point=-1, white_point=1)
    assert list(gray) == [0, 128, 128, 255, 0, 0, 0]
    assert display["subresourceMin"] is display["subresourceMax"] is None
    stats = display["decoding"]
    assert (stats["finiteCount"], stats["nonFiniteCount"], stats["zeroCount"],
            stats["nanCount"], stats["infinityCount"]) == (4, 3, 2, 1, 2)
    assert stats["finiteMinimum"] == -1 and stats["finiteMaximum"] == 1
    assert stats["fullSubresource"] and stats["sourceBytesModified"] is False


def test_full_sparse_atlas_occupancy_and_outlier_in_tail():
    # Useful values beyond the legacy 7500-byte response; no spatial sampling or truncation.
    values = [0.0] * 20000 + [0.01 + n / 100000.0 for n in range(1000)] + [1.0e30]
    gray, display = decode(values)
    assert 0.009 < display["blackPoint"] < 0.011
    assert 0.019 < display["whitePoint"] < 0.021
    assert max(gray[20000:-1]) > 245
    assert not any(gray[:20000])
    assert display["distribution"]["sampleCount"] == 1001
    assert display["distribution"]["histogramCalls"] == 0
    assert display["decoding"]["nonZeroFiniteCount"] == 1001


@pytest.mark.parametrize("value", [0.0, -0.0, 0.25, -2.0])
def test_constants_are_visible_and_minmax_stays_flat(value):
    gray, display = decode([value] * 5)
    assert display["blackPoint"] < display["whitePoint"]
    assert len(gray) == 5
    flat, minmax = decode([value] * 5, range_mode="subresource-minmax")
    assert flat is None and minmax["blackPoint"] == minmax["whitePoint"] == value


def test_all_nonfinite_is_reported_without_inventing_finite_statistics():
    gray, display = decode([float("nan"), float("inf")])
    assert gray == bytearray(2)
    assert display["subresourceMin"] == display["subresourceMax"] == [None]
    assert display["distribution"]["segments"] == []
    assert display["decoding"]["finiteCount"] == 0


def test_extreme_and_subnormal_floats_remain_finite_in_range():
    raw = struct.pack("<4I", 1, 0x80000001, 0x7f7fffff, 0xff7fffff)
    gray, display = MODULE.decode_r32_uint_grayscale(raw, 4, 1, range_mode="subresource-minmax")
    assert len(gray) == 4 and list(gray[2:]) == [255, 0]
    assert display["decoding"]["finiteCount"] == 4
    assert display["blackPoint"] < -1.0e38 and display["whitePoint"] > 1.0e38


def test_native_range_and_input_bytes_unchanged():
    raw = bytearray(struct.pack("<3f", 0.0, 0.5, 1.0))
    before = bytes(raw)
    gray, display = MODULE.decode_r32_uint_grayscale(raw, 3, 1, range_mode="native")
    assert bytes(raw) == before and list(gray) == [0, 128, 255]
    assert display["blackPoint"] == 0 and display["whitePoint"] == 1


def test_default_decoded_gradient_preserves_zero_one_with_hdr_outlier():
    values = [0.1 + index / 1250.0 for index in range(1000)] + [1.0e30]
    gray, display = decode(values)
    assert (display["blackPoint"], display["whitePoint"]) == (0.0, 1.0)
    assert display["distribution"]["rangeDecision"] == "native-visible"
    assert 24 <= gray[0] <= 27 and 227 <= gray[-2] <= 231


def test_default_decoded_sparse_mask_and_noise_stay_at_native_range():
    for values in ([0.0] * 200 + [1.0] * 10,
                   [0.5 + index * 1.0e-10 for index in range(1000)]):
        unused, display = decode(values)
        assert (display["blackPoint"], display["whitePoint"]) == (0.0, 1.0)


def test_explicit_decoded_range_is_not_overridden_by_visibility_policy():
    unused, display = decode([0.1, 0.5, 0.9], range_mode="explicit",
                            black_point=0.2, white_point=0.8)
    assert (display["blackPoint"], display["whitePoint"]) == (0.2, 0.8)
    assert "distribution" not in display


@pytest.mark.parametrize("raw,width,height", [(b"", 1, 1), (b"\0"*8, 1, 1),
                         (b"\0"*4, True, 1), (b"", 0, 1), (b"", 2**30, 1)])
def test_dimensions_and_exact_buffer_length_are_strict(raw, width, height):
    with pytest.raises(ValueError):
        MODULE.decode_r32_uint_grayscale(raw, width, height)


@pytest.mark.parametrize("black,white", [(0, 0), (1, 0), (float("nan"), 1), (0, float("inf")), (True, 2)])
def test_explicit_range_is_strict(black, white):
    with pytest.raises(ValueError):
        decode([0], range_mode="explicit", black_point=black, white_point=white)
