"""Distribution-level exposure regressions, independent of a replay device."""
import importlib.util
import math
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "texture_preview_range.py"
SPEC = importlib.util.spec_from_file_location("bread_texture_preview_range", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def exposure(pixels, mask=(True, True, True, False), depth=False, normalized=False):
    calls = []

    def histogram(low, high, channels):
        calls.append((low, high, list(channels)))
        bins = [0] * 256
        # Match the native inclusive upper edge and its tiny epsilon.
        effective_high = high + high * 1.0e-6
        for pixel in pixels:
            for index, enabled in enumerate(channels):
                value = pixel[index]
                if enabled and math.isfinite(value) and low <= value <= effective_high:
                    bucket = int((value - low) * len(bins) / (effective_high - low))
                    if 0 <= bucket < len(bins):
                        bins[bucket] += 1
        return bins

    minimum = [min(pixel[i] for pixel in pixels) for i in range(4)]
    maximum = [max(pixel[i] for pixel in pixels) for i in range(4)]
    result = MODULE.visible_distribution_range(histogram, minimum, maximum,
                                              mask, depth, normalized)
    return result, calls


def test_sparse_hdr_atlas_uses_occupied_distribution_not_brightest_pixel():
    pixels = [(0.0, 0.0, 0.0, 1.0e20)] * 20000
    pixels += [(0.01 + index / 100000.0,) * 3 + (1.0e20,) for index in range(1000)]
    pixels += [(65504.0, 65504.0, 65504.0, 1.0e20)]
    (black, white, receipt), calls = exposure(pixels)
    assert black == 0.0
    assert 0.019 < white < 0.021
    assert receipt["sampleCount"] == 3003
    assert receipt["histogramCalls"] <= 10
    assert all(call[2] == [True, True, True, False] for call in calls)
    assert receipt["excludedZeroComponents"] is True


def test_nan_infinite_samples_do_not_change_visible_range():
    pixels = [(float("nan"), float("inf"), -float("inf"), 0.0)] * 20
    pixels += [(0.1 + index / 1000.0,) * 3 + (0.0,) for index in range(1000)]
    (black, white, receipt), unused = exposure(pixels)
    assert black == 0.0
    assert white == 1.0  # A modest HDR tail does not hide the ordinary gradient.
    assert receipt["rangeDecision"] == "native-visible"
    assert receipt["sampleCount"] == 3000
    assert receipt["histogramCalls"] <= 34


@pytest.mark.parametrize("clear", [0.0, 1.0])
def test_depth_ignores_clear_background_and_reveals_surface_distribution(clear):
    pixels = [(clear, 0, 0, 0)] * 10000
    pixels += [(0.01 + index / 100000.0, 0, 0, 0) for index in range(1000)]
    (black, white, receipt), unused = exposure(pixels, (True, False, False, False), depth=True)
    assert 0.0099 < black < 0.0102
    assert 0.0198 < white < 0.0201
    assert receipt["sampleCount"] == 1000


def test_positive_constant_and_empty_textures_are_still_previewable():
    (black, white, unused), calls = exposure([(0.0, 0.0, 0.0, 0.0)] * 10)
    assert (black, white) == (0.0, 1.0)
    (black, white, unused), calls = exposure([(20.0, 20.0, 20.0, 0.0)] * 10)
    assert (black, white) == (0.0, 1.0)  # A flat white field has no hidden detail.


def test_substantially_clipped_signed_values_reveal_both_sides():
    pixels = [(index / 10.0 - 50.0,) * 3 + (1.0,) for index in range(1000)]
    (black, white, unused), calls = exposure(pixels)
    assert -50.0 <= black <= -49.0 and 49.0 <= white <= 50.0


def test_narrow_unorm_mask_uses_occupied_codes_instead_of_fixed_zero_one_range():
    pixels = [(0.0, 0.0, 0.0, 0.0)] * 100
    pixels += [(code / 255.0, 0.0, 0.0, 0.0) for code in (1, 2, 3, 4)] * 1000
    (black, white, receipt), calls = exposure(
        pixels, (True, False, False, False), normalized=True)
    assert black == 0.0 and 0.0156 <= white <= 0.0158
    assert receipt["method"] == "whole-subresource-histogram-percentile-v1"
    assert receipt["normalizedSource"] is True
    assert receipt["sampleCount"] == 4000
    assert calls


def test_quantiles_do_not_repeatedly_discard_tail_when_bucket_is_refined():
    pixels = [(index / 1000.0,) * 3 + (0.0,) for index in range(1, 1001)]
    pixels += [(1.0e15,) * 3 + (0.0,)]
    (unused, white, receipt), calls = exposure(pixels)
    assert 0.99 <= white <= 1.01
    assert receipt["histogramCalls"] < 14


@pytest.mark.parametrize("normalized,depth", [(False, False), (True, False), (False, True)])
def test_readable_gradient_keeps_default_regardless_of_format(normalized, depth):
    pixels = [(0.1 + index / 1250.0, 0, 0, 1) for index in range(1000)]
    (black, white, receipt), unused = exposure(
        pixels, (True, False, False, False), depth=depth, normalized=normalized)
    assert (black, white) == (0.0, 1.0)
    assert receipt["rangeDecision"] == "native-visible"


@pytest.mark.parametrize("foreground", [0.5, 1.0])
def test_sparse_mask_and_constant_midtones_do_not_trigger_normalization(foreground):
    pixels = [(0, 0, 0, 1)] * 10000 + [(foreground, 0, 0, 1)] * 100
    (black, white, receipt), unused = exposure(pixels, (True, False, False, False))
    assert (black, white) == (0.0, 1.0)
    assert receipt["rangeDecision"] == "native-visible"


def test_flat_midtone_numerical_noise_is_not_amplified():
    pixels = [(0.5 + index * 1.0e-10, 0, 0, 1) for index in range(1000)]
    (black, white, receipt), unused = exposure(pixels, (True, False, False, False))
    assert (black, white) == (0.0, 1.0)


def test_readable_hdr_content_does_not_follow_isolated_bright_outlier():
    pixels = [(0.1 + index / 1250.0,) * 3 + (0,) for index in range(1000)]
    pixels.append((65504, 65504, 65504, 0))
    (black, white, receipt), unused = exposure(pixels)
    assert (black, white) == (0.0, 1.0)


def test_nearly_white_depth_band_gets_measured_visibility_range():
    pixels = [(1, 0, 0, 0)] * 10000
    pixels += [(0.9991 + index / 10000000.0, 0, 0, 0) for index in range(1000)]
    (black, white, receipt), unused = exposure(pixels, (True, False, False, False), depth=True)
    assert 0.9990 < black < white < 0.9993
    assert receipt["rangeDecision"] == "adapted-low-contrast"


def test_histogram_contract_rejects_bad_gpu_counts():
    with pytest.raises(RuntimeError, match="invalid exposure histogram"):
        MODULE.visible_distribution_range(lambda *unused: [0, -1], [0]*4, [1]*4,
                                          [True, False, False, False])
