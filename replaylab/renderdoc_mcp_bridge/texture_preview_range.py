"""Bounded, whole-subresource exposure for explanatory texture previews.

Python 3.6 / stdlib only.  GetHistogram scans the complete selected subresource
on the replay GPU; no individual pixel is used as an exposure representative.
The capture and source texture are never modified.
"""

import math


_MIN_NORMAL = 1.1754943508222875e-38
_LIMIT = 1.0e30
_PERCENTILES = (0.005, 0.995)
_MAX_REFINEMENTS = 16
_DARK_LEVEL = 8.0 / 255.0
_MIN_VISIBLE_CONTRAST = 2.0 / 255.0


def select_visibility_range(black, white, lower_quantile, upper_quantile,
                            outside_fraction, receipt):
    """Keep 0..1 unless the measured contents are substantially unreadable.

    These are presentation thresholds, not claims about texture semantics.
    Format names and a single HDR outlier never request automatic exposure.
    """
    receipt["rangePolicy"] = "native-unless-unreadable-v1"
    receipt["nativeOutsideFraction"] = outside_fraction
    receipt["occupiedQuantileRange"] = [lower_quantile, upper_quantile]
    span = upper_quantile - lower_quantile
    meaningful_span = span > max(1.0e-6, abs(lower_quantile) * 1.0e-4,
                                abs(upper_quantile) * 1.0e-4)
    clipped_low = max(0.0, min(1.0, lower_quantile))
    clipped_high = max(0.0, min(1.0, upper_quantile))
    if upper_quantile <= _DARK_LEVEL:
        reason = "adapted-dark"
    elif outside_fraction >= 0.9 and meaningful_span:
        reason = "adapted-clipped"
    elif meaningful_span and clipped_high - clipped_low <= _MIN_VISIBLE_CONTRAST:
        reason = "adapted-low-contrast"
    else:
        receipt["rangeDecision"] = "native-visible"
        receipt["tailClipping"] = False
        return 0.0, 1.0
    # Moving only the white point cannot reveal a narrow positive band near
    # white. Its zero/clear background still maps to black with this lower bound.
    if reason == "adapted-low-contrast" and lower_quantile > 0.0:
        black = lower_quantile
    receipt["rangeDecision"] = reason
    return black, white


def visible_distribution_range(histogram, minimum, maximum, channels, depth=False,
                               normalized=False):
    """Return black/white points and a reproducible distribution receipt.

    ``histogram(low, high, channels)`` follows IReplayController.GetHistogram.
    Alpha is absent from ``channels`` for RGB overviews.  Exactly zero samples
    are excluded from the distribution so an unused atlas cannot overwhelm its
    occupied tiles. Negative data stays signed. Tail clipping is per-component,
    not an invented assertion about the semantic meaning of a channel.
    """
    selected = [index for index, enabled in enumerate(channels) if enabled]
    if not selected or len(channels) != 4:
        raise ValueError("Preview exposure needs explicit component selection")
    finite_min = [float(minimum[i]) for i in selected
                  if minimum[i] is not None and math.isfinite(float(minimum[i]))]
    finite_max = [float(maximum[i]) for i in selected
                  if maximum[i] is not None and math.isfinite(float(maximum[i]))]
    low = max(-_LIMIT, min(finite_min)) if len(finite_min) == len(selected) else -_LIMIT
    high = min(_LIMIT, max(finite_max)) if len(finite_max) == len(selected) else _LIMIT
    receipt = {
        "method": "whole-subresource-histogram-percentile-v1",
        "percentiles": list(_PERCENTILES),
        "channels": list(channels),
        "excludedZeroComponents": True,
        "excludedNonFinite": True,
        "depthClearExclusion": None,
        "histogramCalls": 0,
        "sampleCount": 0,
        "segments": [],
        "tailClipping": True,
        "normalizedSource": bool(normalized),
    }
    # Measure visibility independently of storage type. An HDR/depth texture
    # can be readable at 0..1; a normalized mask can instead contain dark codes.

    cache = {}

    def read(lower, upper):
        key = (lower, upper)
        if key not in cache:
            counts = list(histogram(lower, upper, channels))
            if not 2 <= len(counts) <= 4096 or any(
                    isinstance(count, bool) or int(count) != count or count < 0
                    for count in counts):
                raise RuntimeError("Replay returned an invalid exposure histogram")
            cache[key] = [int(count) for count in counts]
            receipt["histogramCalls"] += 1
        return cache[key]

    def quantile(lower, upper, counts, rank, upper_edge):
        # Refine the bucket containing the ORIGINAL rank. Recomputing a 99.5%
        # quantile after every crop would clip another tail on every iteration.
        for unused in range(_MAX_REFINEMENTS):
            total = sum(counts)
            if not total:
                break
            rank = min(max(1.0, rank), float(total))
            before = 0
            chosen = len(counts) - 1
            for index, count in enumerate(counts):
                if before + count >= rank:
                    chosen = index
                    break
                before += count
            width = (upper - lower) / len(counts)
            bucket_low = lower + width * chosen
            bucket_high = min(upper, bucket_low + width)
            estimate = bucket_high if upper_edge else bucket_low
            # Coarse bins can invent contrast in an actually constant channel.
            # Resolve enough precision to distinguish it from a hidden band.
            if (width <= max(abs(estimate) * 0.0001, 1.0e-12) or
                    bucket_low >= bucket_high):
                return estimate
            narrower = read(bucket_low, bucket_high)
            if not sum(narrower):
                return estimate
            lower, upper, counts = bucket_low, bucket_high, narrower
            rank -= before
        return upper if upper_edge else lower

    segments = []
    if depth:
        if (finite_min and min(finite_min) < 0.0 or
                finite_max and max(finite_max) > 1.0):
            raise ValueError("Raw depth exposure requires values within [0, 1]")
        # RenderDoc expands its maximum by 1e-6 when building histogram bins.
        # Leave that margin below 1 to omit the common forward-Z clear value.
        # If it removes all samples, fall back to the full nonzero distribution.
        depth_upper = min(high, 1.0 - 2.0e-6)
        if depth_upper > _MIN_NORMAL:
            counts = read(_MIN_NORMAL, depth_upper)
            if sum(counts):
                segments.append((_MIN_NORMAL, depth_upper, counts))
                receipt["depthClearExclusion"] = [0.0, depth_upper, 1.0]
        if not segments and high > _MIN_NORMAL:
            segments.append((_MIN_NORMAL, min(1.0, high),
                             read(_MIN_NORMAL, min(1.0, high))))
    else:
        if low < -_MIN_NORMAL:
            segments.append((low, -_MIN_NORMAL, read(low, -_MIN_NORMAL)))
        if high > _MIN_NORMAL:
            segments.append((_MIN_NORMAL, high, read(_MIN_NORMAL, high)))
    segments = [segment for segment in segments if sum(segment[2])]
    total = sum(sum(segment[2]) for segment in segments)
    receipt["sampleCount"] = total
    receipt["segments"] = [{"minimum": lower, "maximum": upper,
                            "sampleCount": sum(counts)}
                           for lower, upper, counts in segments]
    if not total:
        receipt["tailClipping"] = False
        receipt["rangePolicy"] = "native-unless-unreadable-v1"
        receipt["rangeDecision"] = "native-empty"
        return 0.0, 1.0, receipt

    def percentile(fraction, upper_edge):
        rank = max(1.0, fraction * total)
        for lower, upper, counts in segments:
            count = sum(counts)
            if rank <= count:
                return quantile(lower, upper, counts, rank, upper_edge)
            rank -= count
        return segments[-1][1]

    lower_quantile = percentile(_PERCENTILES[0], False)
    upper_quantile = percentile(_PERCENTILES[1], True)
    if len(finite_min) == len(selected):
        lower_quantile = max(lower_quantile, min(finite_min))
    if len(finite_max) == len(selected):
        upper_quantile = min(upper_quantile, max(finite_max))
    black = lower_quantile if depth or low < 0.0 else 0.0
    white = upper_quantile
    if not depth:
        # Keep black background black and preserve the sign of diagnostic data.
        black, white = min(black, 0.0), max(white, 0.0)
    else:
        black, white = max(0.0, black), min(1.0, white)
    if white - black <= max(1.0e-12, abs(white) * 1.0e-6):
        if depth:
            black, white = max(0.0, black - 1.0e-5), min(1.0, white + 1.0e-5)
        elif white > 0.0:
            black = 0.0
        elif black < 0.0:
            white = 0.0
        else:
            black, white = 0.0, 1.0
    native_count = min(total, sum(read(_MIN_NORMAL, 1.0)))
    black, white = select_visibility_range(
        black, white, lower_quantile, upper_quantile,
        1.0 - native_count / float(total), receipt)
    return black, white, receipt
