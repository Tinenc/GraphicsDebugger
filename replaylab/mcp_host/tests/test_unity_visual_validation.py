import hashlib

from PIL import Image
import pytest

from src.unity_visual_validation import compare_render_images


def png(tmp_path, name, pixels, size=(2, 1), mode="RGBA"):
    path = tmp_path / name
    with Image.new(mode, size) as image:
        image.putdata(pixels)
        image.save(path)
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def compare(left, right, **kwargs):
    return compare_render_images(left[0], right[0], left[1], right[1], **kwargs)


def test_all_pixels_compared_and_hidden_alpha_does_not_erase_color(tmp_path):
    left = png(tmp_path, "a.png", [(0, 0, 0, 0), (0, 0, 0, 0)])
    right = png(tmp_path, "b.png", [(0, 0, 0, 0), (255, 0, 0, 0)])
    result = compare(left, right)
    assert result["pixelCount"] == 2 and not result["sampled"]
    assert result["changedPixels"] == 1 and result["changedBounds"] == [1, 0, 2, 1]
    assert result["perChannel"]["R"]["meanAbsoluteError"] == 0.5
    assert result["perChannel"]["G"]["maxAbsoluteError"] == 0
    assert not result["withinTolerance"] and not result["visualEquivalenceVerified"]


def test_rgb_and_rgba_are_explicit(tmp_path):
    left = png(tmp_path, "a.png", [(10, 20, 30, 0)] * 2)
    right = png(tmp_path, "b.png", [(10, 20, 30, 255)] * 2)
    assert compare(left, right)["withinTolerance"]
    assert not compare(left, right, channels="RGBA")["withinTolerance"]
    rgb = png(tmp_path, "c.png", [(10, 20, 30)] * 2, mode="RGB")
    with pytest.raises(ValueError, match="actual alpha"):
        compare(left, rgb, channels="RGBA")


def test_hashes_and_dimensions_are_not_silently_repaired(tmp_path):
    left = png(tmp_path, "a.png", [(10, 20, 30, 0)] * 2)
    right = png(tmp_path, "b.png", [(10, 20, 30, 0)], size=(1, 1))
    with pytest.raises(ValueError, match="dimensions differ"):
        compare(left, right)
    with pytest.raises(ValueError, match="changed after"):
        compare(left, (left[0], "0" * 64))


def test_default_tolerance_is_one_code_value_without_gamma_or_exposure(tmp_path):
    left = png(tmp_path, "a.png", [(32, 128, 254, 255)] * 2)
    right = png(tmp_path, "b.png", [(33, 129, 255, 255)] * 2)
    assert compare(left, right)["withinTolerance"]
    result = compare(left, right, absolute_tolerance=0)
    assert not result["withinTolerance"]
    assert result["meanAbsoluteError"] == pytest.approx(1 / 255)


@pytest.mark.parametrize("tolerance", [True, -1, 1.1, float("nan"), float("inf")])
def test_invalid_tolerance_rejected_before_reading_files(tolerance):
    with pytest.raises(ValueError, match="tolerances"):
        compare_render_images("absent", "absent", "", "", absolute_tolerance=tolerance)


def test_16_bit_gray_does_not_silently_quantize(tmp_path):
    left = png(tmp_path, "a.png", [257, 1024], mode="I;16")
    with pytest.raises(ValueError, match="RGB8/gray8"):
        compare(left, left)


def test_context_binding_rejects_a_different_eid_or_display_mapping(tmp_path):
    sample = png(tmp_path, "a.png", [(10, 20, 30, 255)] * 2)
    context = {"captureId": "capture", "eventId": 10, "view": "color0", "displayTransform": "native", "temporalState": "single-frame"}
    with pytest.raises(ValueError, match="eventId"):
        compare(sample, sample, reference_context=context, rendered_context={**context, "eventId": 11})
    result = compare(sample, sample, reference_context=context, rendered_context=context)
    assert result["contextAgreementVerified"] and result["evidenceContext"] == context


def test_error_regions_locate_the_actual_changed_pixels(tmp_path):
    left = png(tmp_path, "left.png", [(0, 0, 0, 255)] * 2)
    right = png(tmp_path, "right.png", [(0, 0, 0, 255), (255, 0, 0, 255)])
    regions = compare(left, right)["largestErrorRegions"]
    assert len(regions) == 1 and regions[0]["bounds"] == [1, 0, 2, 1]
