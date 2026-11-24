"""Functional image acceptance tolerates small native changes, not lost effects."""
from __future__ import annotations

import hashlib

from PIL import Image, ImageChops, ImageDraw
import pytest

from src.unity_visual_validation import compare_render_images, normalize_fidelity_policy


def saved(tmp_path, name, image):
    path = tmp_path / (name + ".png")
    image.save(path)
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def result(tmp_path, left, right, **kwargs):
    a, ah = saved(tmp_path, "reference", left)
    b, bh = saved(tmp_path, "render", right)
    return compare_render_images(a, b, ah, bh, mode="visual-fidelity", **kwargs)


def scene(size=(128, 128)):
    image = Image.new("RGB", size, (24, 24, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 20, 80, 96), fill=(110, 70, 40))
    draw.ellipse((85, 35, 115, 65), fill=(45, 95, 130))
    return image


def test_small_native_light_offset_passes_without_claiming_pixel_or_algorithm_equivalence(tmp_path):
    left = scene()
    right = ImageChops.add(left, Image.new("RGB", left.size, (8, 10, 12)))
    compared = result(tmp_path, left, right)
    assert compared["withinTolerance"] and compared["qualityPassed"]
    assert not compared["pixelWithinTolerance"]
    assert compared["meanAbsoluteError"] > 1 / 255
    assert compared["semanticReviewRequired"] and not compared["coreAlgorithmVerified"]
    assert not compared["visualEquivalenceVerified"] and not compared["sampled"]
    assert compared["goal"] == "functional-visual-fidelity"


def test_lower_exposure_preserves_edges_near_threshold(tmp_path):
    left = Image.new("RGB", (64, 64), (25, 25, 25))
    ImageDraw.Draw(left).rectangle((14, 14, 45, 45), fill=(48, 48, 48))
    right = left.point(lambda value: round(value * 0.8))
    compared = result(tmp_path, left, right)
    assert compared["qualityPassed"]
    assert compared["fidelityMetrics"]["missingEdgeFraction"] == 0


def test_a_one_pixel_raster_edge_difference_is_not_a_missing_object(tmp_path):
    left = scene()
    right = ImageChops.offset(left, 1, 0)
    compared = result(tmp_path, left, right)
    assert compared["qualityPassed"]
    assert compared["fidelityMetrics"]["missingEdgeFraction"] == 0


def test_black_render_cannot_pass_with_missing_visibility(tmp_path):
    left = scene()
    compared = result(tmp_path, left, Image.new("RGB", left.size, "black"))
    assert not compared["qualityPassed"]
    assert {"missing-structure", "effect-visibility-loss"} <= {row["code"] for row in compared["qualityFailures"]}
    assert compared["repairHints"]


def test_small_bright_effect_disappearance_is_not_hidden_by_full_frame_average(tmp_path):
    left = Image.new("RGB", (256, 256), "black")
    ImageDraw.Draw(left).rectangle((98, 101, 110, 113), fill=(220, 120, 60))
    compared = result(tmp_path, left, Image.new("RGB", left.size, "black"))
    assert compared["meanAbsoluteError"] < 0.01
    assert compared["fidelityMetrics"]["severePixelFraction"] < 0.01
    assert not compared["qualityPassed"]
    assert "local-structure-loss" in {row["code"] for row in compared["qualityFailures"]}
    assert compared["fidelityMetrics"]["localStructureLoss"][0]["bounds"] == [96, 96, 128, 128]


def test_uniform_visible_output_disappearing_is_still_a_failure(tmp_path):
    left = Image.new("RGB", (64, 64), (18, 18, 18))
    compared = result(tmp_path, left, Image.new("RGB", left.size, "black"))
    assert compared["meanAbsoluteError"] < 0.08
    assert compared["fidelityMetrics"]["referenceEdgePixels"] == 0
    assert not compared["qualityPassed"]
    assert "effect-visibility-loss" in {row["code"] for row in compared["qualityFailures"]}


def test_opaque_alpha_does_not_disguise_disappearing_rgb_signal(tmp_path):
    left = Image.new("RGBA", (64, 64), (18, 18, 18, 255))
    compared = result(tmp_path, left, Image.new("RGBA", left.size, (0, 0, 0, 255)), channels="RGBA")
    assert compared["meanAbsoluteError"] < 0.08
    assert compared["fidelityMetrics"]["visibilityComponents"] == "RGB"
    assert not compared["qualityPassed"]


def test_color_inversion_requires_repair(tmp_path):
    left = scene()
    compared = result(tmp_path, left, ImageChops.invert(left))
    assert not compared["qualityPassed"]
    assert "display-mean-error" in {row["code"] for row in compared["qualityFailures"]}


def test_uniform_reference_does_not_claim_effect_visibility_from_matching_pixels(tmp_path):
    image = Image.new("RGB", (16, 16), (32, 96, 128))
    compared = result(tmp_path, image, image)
    assert compared["qualityPassed"]
    assert compared["fidelityMetrics"]["effectVisibility"] == "requires-semantic-review"
    assert compared["semanticReviewRequired"]


def test_recorded_policy_is_fixed_and_can_be_stricter(tmp_path):
    left = scene()
    right = ImageChops.add(left, Image.new("RGB", left.size, (8, 8, 8)))
    policy = normalize_fidelity_policy({"maxMeanAbsoluteError": 0.01})
    compared = result(tmp_path, left, right, fidelity=policy)
    assert not compared["qualityPassed"]
    assert compared["fidelityPolicy"] == policy
    assert normalize_fidelity_policy()["maxMeanAbsoluteError"] == 0.08


def test_visual_mode_keeps_every_reference_pixel_and_receipt_hash(tmp_path):
    left = scene()
    before = left.tobytes()
    compared = result(tmp_path, left, left)
    assert compared["pixelCount"] == 128 * 128 and not compared["sampled"]
    assert left.tobytes() == before
    assert compared["reference"]["sha256"] == compared["rendered"]["sha256"]
    assert compared["comparisonSpace"] == "encoded-display-components"


@pytest.mark.parametrize("policy", [{"unknown": 1}, {"maxMeanAbsoluteError": True},
    {"maxSeverePixelFraction": 2}, {"edgeMatchRadiusPixels": 3}, {"edgeMatchRadiusPixels": 1.0},
    {"edgeThreshold": 0}, {"maxLocalMeanAbsoluteError": float("nan")}])
def test_invalid_policy_rejected(policy):
    with pytest.raises(ValueError):
        normalize_fidelity_policy(policy)


def test_unknown_mode_rejected_without_loading_evidence():
    with pytest.raises(ValueError, match="mode"):
        compare_render_images("missing", "missing", "", "", mode="make-it-match")
