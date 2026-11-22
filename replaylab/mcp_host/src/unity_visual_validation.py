"""Full-resolution display-image comparison for a Unity calibration iteration.

This deliberately does not infer matching camera/pass, color transforms or GPU
equivalence. Images are compared without resizing, flipping, exposure or gamma
correction. Those decisions remain explicit in the source/target render setup.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import math
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageChops, ImageFilter, ImageStat


MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 32 * 1024 * 1024

# These are display-fidelity limits, not a proof of matching GPU algorithms.
# The caller records the chosen policy before repair iterations; comparisons
# never change these limits, normalize exposures, resize or recolor evidence.
FIDELITY_DEFAULTS = {
    "maxMeanAbsoluteError": 0.08,
    "severeDifferenceThreshold": 0.25,
    "maxSeverePixelFraction": 0.05,
    "maxLocalMeanAbsoluteError": 0.22,
    "edgeThreshold": 0.08,
    "edgeMatchRadiusPixels": 1,
    "maxMissingEdgeFraction": 0.30,
    "maxAddedEdgeFraction": 0.30,
    "maxLocalMissingEdgeFraction": 0.75,
    "minReferenceContrast": 0.02,
    "minRetainedContrastRatio": 0.15,
    "minReferenceSignal": 0.03,
    "minRetainedSignalRatio": 0.15,
}


def normalize_fidelity_policy(value: Mapping | None = None) -> dict:
    """Resolve bounded, explicit visual goals without inspecting either image."""
    if value is not None and (not isinstance(value, Mapping) or set(value) - set(FIDELITY_DEFAULTS)):
        raise ValueError("Unknown visual fidelity policy fields")
    result = {**FIDELITY_DEFAULTS, **dict(value or {})}
    for key, setting in result.items():
        if key == "edgeMatchRadiusPixels":
            if type(setting) is not int or not 0 <= setting <= 2:
                raise ValueError("edgeMatchRadiusPixels must be an integer between zero and two")
        elif type(setting) not in (int, float) or not math.isfinite(setting) or not 0 <= setting <= 1:
            raise ValueError("Visual fidelity limits must be finite numbers between zero and one")
    if result["edgeThreshold"] <= 0 or result["severeDifferenceThreshold"] <= 0:
        raise ValueError("Visual severity and edge thresholds must be positive")
    return result


def _maximum_component(image):
    bands = image.split()
    result = bands[0].copy()
    try:
        for band in bands[1:]:
            combined = ImageChops.lighter(result, band)
            result.close()
            result = combined
        return result
    finally:
        for band in bands:
            band.close()


def _edge_strength(image):
    """Maximum neighboring component delta, preserving color-only boundaries."""
    result = Image.new("L", image.size, 0)
    for band in image.split():
        try:
            for axis in (0, 1):
                shifted = ImageChops.offset(band, 1 - axis, axis)
                delta = ImageChops.difference(band, shifted)
                shifted.close()
                # offset wraps; the opposite border is not an image neighbor.
                delta.paste(0, (0, 0, 1, image.height) if axis == 0 else (0, 0, image.width, 1))
                combined = ImageChops.lighter(result, delta)
                result.close()
                delta.close()
                result = combined
        finally:
            band.close()
    return result


def _mask(image, threshold):
    return image.point([255 if i > threshold * 255.0 else 0 for i in range(256)])


def _contrast(image):
    statistics = ImageStat.Stat(image)
    # An opaque alpha plane must not disguise loss of the displayed RGB effect.
    values = statistics.var[:3] if image.mode == "RGBA" else statistics.var
    return math.sqrt(sum(values) / len(values)) / 255.0


def _mean(image):
    values = ImageStat.Stat(image).mean
    if image.mode == "RGBA":
        values = values[:3]
    return sum(values) / (255.0 * len(values))


def _visual_fidelity(left, right, difference, policy, mean_error, regions):
    """Full-raster and salient-structure checks; no image transformation for scoring."""
    count = left.width * left.height
    maximum = _maximum_component(difference)
    severe = _mask(maximum, policy["severeDifferenceThreshold"])
    maximum.close()
    severe_fraction = severe.histogram()[255] / count
    severe_bounds = severe.getbbox()
    severe.close()
    left_strength, right_strength = _edge_strength(left), _edge_strength(right)
    left_edges, right_edges = (_mask(value, policy["edgeThreshold"]) for value in (left_strength, right_strength))
    # A source boundary may become weaker under Unity-native lighting. Match
    # strong edges against half-threshold candidates, without changing pixels.
    left_candidates, right_candidates = (_mask(value, policy["edgeThreshold"] * 0.5)
                                         for value in (left_strength, right_strength))
    left_strength.close()
    right_strength.close()
    radius = policy["edgeMatchRadiusPixels"]
    if radius:
        for name, candidates in (("left", left_candidates), ("right", right_candidates)):
            expanded = candidates.filter(ImageFilter.MaxFilter(radius * 2 + 1))
            candidates.close()
            if name == "left":
                left_candidates = expanded
            else:
                right_candidates = expanded
    missing = ImageChops.subtract(left_edges, right_candidates)
    added = ImageChops.subtract(right_edges, left_candidates)
    left_candidates.close()
    right_candidates.close()
    left_count, right_count = left_edges.histogram()[255], right_edges.histogram()[255]
    missing_count, added_count = missing.histogram()[255], added.histogram()[255]
    missing_fraction = missing_count / left_count if left_count else 0.0
    added_fraction = added_count / right_count if right_count else 0.0
    reference_contrast, rendered_contrast = _contrast(left), _contrast(right)
    reference_signal, rendered_signal = _mean(left), _mean(right)
    contrast_ratio = rendered_contrast / reference_contrast if reference_contrast else None
    signal_ratio = rendered_signal / reference_signal if reference_signal else None
    width, height = max(1, (left.width + 7) // 8), max(1, (left.height + 7) // 8)
    local_missing = []
    visibility_collapses = []
    for top in range(0, left.height, height):
        for start in range(0, left.width, width):
            box = (start, top, min(start + width, left.width), min(top + height, left.height))
            with left_edges.crop(box) as edges, missing.crop(box) as lost:
                salient_count = edges.histogram()[255]
                # Avoid declaring failure from one isolated pixel/noisy edge.
                if salient_count >= 8:
                    fraction = lost.histogram()[255] / salient_count
                    if fraction > policy["maxLocalMissingEdgeFraction"]:
                        local_missing.append({"bounds": list(box), "missingEdgeFraction": fraction,
                                              "referenceEdgePixels": salient_count})
            with left.crop(box) as source, right.crop(box) as target:
                source_contrast, target_contrast = _contrast(source), _contrast(target)
                source_signal, target_signal = _mean(source), _mean(target)
                if (source_contrast >= policy["minReferenceContrast"] and source_contrast > 0
                        and target_contrast / source_contrast < policy["minRetainedContrastRatio"]):
                    visibility_collapses.append({"bounds": list(box), "kind": "contrast-collapse",
                        "referenceContrast": source_contrast, "renderedContrast": target_contrast})
                elif (source_signal >= policy["minReferenceSignal"] and source_signal > 0
                        and target_signal / source_signal < policy["minRetainedSignalRatio"]):
                    visibility_collapses.append({"bounds": list(box), "kind": "signal-collapse",
                        "referenceSignal": source_signal, "renderedSignal": target_signal})
    missing_bounds, added_bounds = missing.getbbox(), added.getbbox()
    for image in (left_edges, right_edges, missing, added):
        image.close()
    failures = []
    hints = []

    def fail(code, observed, limit, hint, bounds=None):
        failures.append({"code": code, "observed": observed, "limit": limit,
                         "bounds": list(bounds) if bounds else None})
        if hint not in hints:
            hints.append(hint)

    if mean_error > policy["maxMeanAbsoluteError"]:
        fail("display-mean-error", mean_error, policy["maxMeanAbsoluteError"],
             "Inspect shared color mapping, exposure, lighting/indirect substitution and material values; preserve the reference mapping.")
    if severe_fraction > policy["maxSeverePixelFraction"]:
        fail("severe-pixel-difference", severe_fraction, policy["maxSeverePixelFraction"],
             "Inspect the severe-difference region for missing passes, blending, background/depth dependencies or incorrect coordinate conversion.", severe_bounds)
    local_error = regions[0]["meanAbsoluteError"] if regions else 0.0
    if local_error > policy["maxLocalMeanAbsoluteError"]:
        fail("local-display-error", local_error, policy["maxLocalMeanAbsoluteError"],
             "Inspect the highest-error region and its contributing material/pass before changing global lighting.", regions[0]["bounds"])
    if left_count >= 8 and missing_fraction > policy["maxMissingEdgeFraction"]:
        fail("missing-structure", missing_fraction, policy["maxMissingEdgeFraction"],
             "Check mesh/instance presence, camera projection, visibility, displacement and effect execution; source boundaries are missing.", missing_bounds)
    if left_count >= 8 and right_count >= 8 and added_fraction > policy["maxAddedEdgeFraction"]:
        fail("unexpected-structure", added_fraction, policy["maxAddedEdgeFraction"],
             "Check UV orientation, geometry, pass order, reconstruction artifacts and unexpected shadow boundaries.", added_bounds)
    if local_missing:
        fail("local-structure-loss", max(row["missingEdgeFraction"] for row in local_missing), policy["maxLocalMissingEdgeFraction"],
             "A local source effect/object lost most of its visible boundary; inspect those regions even when full-frame average error is small.", local_missing[0]["bounds"])
    if visibility_collapses:
        fail("effect-visibility-loss", len(visibility_collapses), 0,
             "A source region became blank or lost most of its contrast/signal; verify the core effect executes and its reconstructed inputs are meaningful.", visibility_collapses[0]["bounds"])
    return {"qualityPassed": not failures, "qualityFailures": failures, "repairHints": hints,
        "fidelityMetrics": {"severePixelFraction": severe_fraction,
            "severeDifferenceBounds": list(severe_bounds) if severe_bounds else None,
            "maxLocalMeanAbsoluteError": local_error,
            "referenceEdgePixels": left_count, "renderedEdgePixels": right_count,
            "missingEdgeFraction": missing_fraction, "addedEdgeFraction": added_fraction,
            "referenceContrast": reference_contrast, "renderedContrast": rendered_contrast,
            "retainedContrastRatio": contrast_ratio, "referenceSignal": reference_signal,
            "renderedSignal": rendered_signal, "retainedSignalRatio": signal_ratio,
            "visibilityComponents": "RGB" if left.mode == "RGBA" else left.mode,
            "localStructureLoss": local_missing[:8], "visibilityLossRegions": visibility_collapses[:8],
            "effectVisibility": "loss-detected" if visibility_collapses or local_missing else
                "structure-retained" if left_count >= 8 else "requires-semantic-review",
            "edgeMatching": "strong reference edges against half-threshold candidates within the recorded pixel radius"},
        "coreAlgorithmVerified": False,
        "semanticReviewRequired": True,
        "goal": "functional-visual-fidelity"}


def _image(path: str, expected: str, channels: str):
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise ValueError("Render evidence must be an existing absolute image path")
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Supply the image SHA-256 from its capture/read receipt")
    with candidate.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Render evidence exceeds the image byte limit")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Render image changed after its evidence receipt")
    with Image.open(BytesIO(raw)) as image:
        # Converting an HDR/16-bit signal to RGB8 would hide the differences this
        # operation is meant to reveal. Use a common explicit display export.
        if image.format != "PNG" or len(raw) < 25 or raw[24] != 8 or image.mode not in {"RGB", "RGBA", "L", "LA"}:
            raise ValueError("Comparison requires RGB8/gray8 PNG display exports; preserve HDR data separately")
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("Render evidence exceeds the image pixel limit")
        if channels == "RGBA" and "A" not in image.getbands():
            raise ValueError("RGBA comparison requires an actual alpha channel in both images")
        converted = image.convert(channels)
        info = {"path": str(candidate), "sha256": expected, "width": image.width,
                "height": image.height, "mode": image.mode}
    return converted, info


def compare_render_images(reference_path: str, rendered_path: str, reference_sha256: str,
                          rendered_sha256: str, *, channels: str = "RGB",
                          absolute_tolerance: float = 1.0 / 255.0,
                          max_changed_pixel_fraction: float = 0.0,
                          reference_context: Mapping | None = None,
                          rendered_context: Mapping | None = None,
                          mode: str = "strict-pixel",
                          fidelity: Mapping | None = None) -> dict:
    """Compare fixed display exports against a recorded pixel or visual goal.

    visual-fidelity permits moderate display-color differences while retaining
    full-resolution severe/local error and visible-structure checks. The result
    still requires source/algorithm and reconstruction review by the Agent.
    """
    if mode not in {"strict-pixel", "visual-fidelity"}:
        raise ValueError("Comparison mode must be strict-pixel or visual-fidelity")
    fidelity_policy = normalize_fidelity_policy(fidelity) if mode == "visual-fidelity" else None
    if channels not in {"RGB", "RGBA"}:
        raise ValueError("channels must be RGB or RGBA")
    for value in (absolute_tolerance, max_changed_pixel_fraction):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Comparison tolerances must be finite numbers between zero and one")
    context_keys = ("captureId", "eventId", "view", "displayTransform", "temporalState")
    if reference_context is not None or rendered_context is not None:
        if not isinstance(reference_context, Mapping) or not isinstance(rendered_context, Mapping):
            raise ValueError("Both reference and rendered context are required when binding render evidence")
        for key in context_keys:
            if key not in reference_context or key not in rendered_context or reference_context[key] != rendered_context[key]:
                raise ValueError("Render comparison context differs or is missing: " + key)
    left, reference = _image(reference_path, reference_sha256, channels)
    try:
        right, rendered = _image(rendered_path, rendered_sha256, channels)
        try:
            if left.size != right.size:
                raise ValueError("Render image dimensions differ; match the viewport instead of rescaling evidence")
            difference = ImageChops.difference(left, right)
            try:
                histogram = difference.histogram()
                count = left.width * left.height
                bands = len(channels)
                metrics = {}
                for index, channel in enumerate(channels):
                    hist = histogram[index * 256:(index + 1) * 256]
                    metrics[channel] = {
                        "meanAbsoluteError": sum(i * n for i, n in enumerate(hist)) / (255.0 * count),
                        "rootMeanSquaredError": math.sqrt(sum(i * i * n for i, n in enumerate(hist)) / count) / 255.0,
                        "maxAbsoluteError": max(i for i, n in enumerate(hist) if n) / 255.0,
                    }
                # Include every pixel; transparent RGB can still hold meaningful
                # capture data. Never premultiply it or hide it behind a background.
                changed = Image.new("L", left.size, 0)
                try:
                    cutoff = absolute_tolerance * 255.0
                    for band in difference.split():
                        mask = band.point([255 if i > cutoff else 0 for i in range(256)])
                        merged = ImageChops.lighter(changed, mask)
                        changed.close()
                        mask.close()
                        band.close()
                        changed = merged
                    changed_pixels = changed.histogram()[255]
                    bounds = changed.getbbox()
                finally:
                    changed.close()
                fraction = changed_pixels / count
                regions = []
                tile_width, tile_height = max(1, (left.width + 7) // 8), max(1, (left.height + 7) // 8)
                for top in range(0, left.height, tile_height):
                    for start in range(0, left.width, tile_width):
                        box = (start, top, min(start + tile_width, left.width), min(top + tile_height, left.height))
                        with difference.crop(box) as tile:
                            hist = tile.histogram()
                            error = sum((i % 256) * n for i, n in enumerate(hist)) / (255.0 * tile.width * tile.height * bands)
                        if error:
                            regions.append({"bounds": list(box), "meanAbsoluteError": error})
                regions.sort(key=lambda item: item["meanAbsoluteError"], reverse=True)
                mean_error = sum(m["meanAbsoluteError"] for m in metrics.values()) / bands
                pixel_passed = fraction <= max_changed_pixel_fraction
                quality = (_visual_fidelity(left, right, difference, fidelity_policy, mean_error, regions)
                           if fidelity_policy is not None else {})
                return {"schema": "bread-unity-image-comparison/v1", "status": "compared",
                    "mode": mode, "fidelityPolicy": fidelity_policy,
                    "reference": reference, "rendered": rendered,
                    "comparisonSpace": "encoded-display-components", "channels": channels,
                    "pixelCount": count, "sampled": False, "perChannel": metrics,
                    "meanAbsoluteError": mean_error,
                    "changedPixels": changed_pixels, "changedPixelFraction": fraction,
                    "changedBounds": list(bounds) if bounds else None,
                    "largestErrorRegions": regions[:8],
                    "evidenceContext": dict(reference_context) if reference_context is not None else None,
                    "contextAgreementVerified": reference_context is not None,
                    "tolerances": {"absolute": absolute_tolerance,
                        "maxChangedPixelFraction": max_changed_pixel_fraction},
                    "pixelWithinTolerance": pixel_passed,
                    "withinTolerance": quality.get("qualityPassed", pixel_passed),
                    **quality,
                    "visualEquivalenceVerified": False,
                    "scope": "These image files only; matching pass, camera, color mapping, temporal state and dependencies require separate review."}
            finally:
                difference.close()
        finally:
            right.close()
    finally:
        left.close()
