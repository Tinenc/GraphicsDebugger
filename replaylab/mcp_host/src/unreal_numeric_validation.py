"""Regional and temporal HDR statistics. No engine execution or image resampling."""
from __future__ import annotations

from array import array
import hashlib
import heapq
import math
from pathlib import Path
from typing import Annotated

from PIL import Image
from pydantic import Field, model_validator

from .unreal_recipe import Name, NonNegative, Record


class ErrorLimits(Record):
    maxAbsoluteError: NonNegative | None = None
    p95AbsoluteError: NonNegative | None = None
    p99AbsoluteError: NonNegative | None = None


class CoverageMask(Record):
    path: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    channel: Annotated[str, Field(pattern=r"^[RGBA]$")] = "R"
    threshold: Annotated[int, Field(ge=1, le=255)] = 1
    origin: str = "top-left"


class Region(Record):
    id: Name
    bounds: tuple[int, int, int, int] | None = None
    mask: CoverageMask | None = None
    limits: ErrorLimits | None = None


class NumericOptions(Record):
    regions: Annotated[list[Region], Field(max_length=16)] = []
    limits: ErrorLimits = ErrorLimits()
    tileSize: Annotated[int, Field(ge=8, le=512)] = 64
    worstCount: Annotated[int, Field(ge=1, le=32)] = 8

    @model_validator(mode="after")
    def unique_regions(self):
        if len({r.id for r in self.regions}) != len(self.regions):
            raise ValueError("Region IDs must be unique")
        return self


def _percentiles(values):
    """Exact type-7 quantiles using a packed copy instead of millions of Python floats."""
    if not values:
        return {"p95AbsoluteError": None, "p99AbsoluteError": None}
    work = array("d", values)

    def select(k):
        lo, hi = 0, len(work) - 1
        while lo < hi:
            pivot = sorted((work[lo], work[(lo + hi) // 2], work[hi]))[1]
            lt, i, gt = lo, lo, hi
            while i <= gt:
                if work[i] < pivot:
                    work[lt], work[i] = work[i], work[lt]
                    lt += 1
                    i += 1
                elif work[i] > pivot:
                    work[i], work[gt] = work[gt], work[i]
                    gt -= 1
                else:
                    i += 1
            if k < lt:
                hi = lt - 1
            elif k > gt:
                lo = gt + 1
            else:
                return work[k]
        return work[lo]

    result = {}
    for name, q in (("p95AbsoluteError", .95), ("p99AbsoluteError", .99)):
        position = (len(work) - 1) * q
        a, b = math.floor(position), math.ceil(position)
        low, high = select(a), select(b)
        result[name] = low + (high - low) * (position - a)
    return result


def _selectors(options, width, height):
    selectors = []
    budget = width * height
    for region in options.regions:
        x0, y0, x1, y1 = region.bounds or (0, 0, width, height)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("Region bounds must be a nonempty half-open rectangle inside the image")
        budget += (x1 - x0) * (y1 - y0)
        if budget > 32_000_000:
            raise ValueError("Regional comparison exceeds the 32-million-pixel statistics budget")
        mask = None
        if region.mask:
            spec = region.mask
            path = Path(spec.path)
            if spec.origin != "top-left":
                raise ValueError("Masks must explicitly use the image's top-left origin; no implicit flip")
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Coverage mask exceeds 32 MiB")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != spec.sha256:
                raise ValueError("Coverage mask hash mismatch")
            import io
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.size != (width, height):
                    raise ValueError("Coverage must be a PNG at the exact HDR resolution")
                if image.mode not in {"L", "RGB", "RGBA"}:
                    raise ValueError("Use an 8-bit L, RGB or RGBA coverage mask")
                if image.mode == "L":
                    if spec.channel != "R":
                        raise ValueError("A grayscale mask uses channel R")
                    mask = image.tobytes()
                else:
                    if spec.channel not in image.getbands():
                        raise ValueError("Coverage channel is absent from mask")
                    mask = image.getchannel(spec.channel).tobytes()
        selectors.append((region, (x0, y0, x1, y1), mask))
    return selectors


class _Stats:
    def __init__(self, channels, options, tolerance):
        self.channels, self.options, self.tolerance = channels, options, tolerance
        self.errors = array("d")
        self.sums, self.peaks = [0.] * len(channels), [0.] * len(channels)
        self.count = self.invalid = self.changed = 0
        self.bounds = None
        self.worst, self.tiles = [], {}
        self.invalid_pixels = []

    def add(self, x, y, a, b):
        self.count += 1
        if not all(math.isfinite(v) for v in (*a, *b)):
            self.invalid += 1
            if len(self.invalid_pixels) < self.options.worstCount:
                self.invalid_pixels.append({"x": x, "y": y})
            return
        delta = tuple(abs(u - v) for u, v in zip(a, b))
        error = max(delta)
        self.errors.append(error)
        for i, d in enumerate(delta):
            self.sums[i] += d
            self.peaks[i] = max(self.peaks[i], d)
        if error > self.tolerance:
            self.changed += 1
            old = self.bounds or [x, y, x + 1, y + 1]
            self.bounds = [min(old[0], x), min(old[1], y), max(old[2], x + 1), max(old[3], y + 1)]
        # NaN/Inf already rejected; heaps and quantiles never contain non-finite data.
        if len(self.worst) < self.options.worstCount:
            heapq.heappush(self.worst, (error, x, y, a, b))
        elif error > self.worst[0][0]:
            heapq.heapreplace(self.worst, (error, x, y, a, b))
        key = (x // self.options.tileSize, y // self.options.tileSize)
        tile = self.tiles.get(key)
        if tile is None:
            tile = self.tiles[key] = [0, 0., 0., 0]
        tile[0] += 1
        tile[1] += error
        tile[2] = max(tile[2], error)
        tile[3] += error > self.tolerance

    def report(self, limits, max_fraction):
        peak = max(self.peaks)
        quantiles = ({"p95AbsoluteError": 0., "p99AbsoluteError": 0.}
                     if self.errors and peak == 0 else _percentiles(self.errors))
        checks = {"maxAbsoluteError": peak, **quantiles}
        failed = [name for name, limit in limits.model_dump().items()
                  if limit is not None and (checks[name] is None or checks[name] > limit)]
        valid = self.count - self.invalid
        fraction = self.changed / self.count if self.count else None
        tiles = sorted(self.tiles.items(), key=lambda item: (item[1][2], item[1][1] / item[1][0]), reverse=True)
        return {
            "pixelCount": self.count, "validPixelCount": valid, "nonFinitePixels": self.invalid,
            "nonFiniteLocations": self.invalid_pixels,
            "changedPixelFraction": fraction, "changedBounds": self.bounds,
            "meanAbsoluteError": sum(self.errors) / valid if valid else None,
            "maxAbsoluteError": peak if valid else None, **quantiles,
            "withinTolerance": bool(valid and not self.invalid and fraction <= max_fraction and not failed),
            "failedLimits": failed, "limits": limits.model_dump(exclude_none=True),
            "perChannel": {c: {"maxAbsoluteError": p if valid else None,
                               "meanAbsoluteError": s / valid if valid else None}
                           for c, p, s in zip(self.channels, self.peaks, self.sums)},
            "worstPixels": [{"x": x, "y": y, "maxAbsoluteError": e,
                             "reference": a, "rendered": b} for e, x, y, a, b in sorted(self.worst, reverse=True)],
            "worstTiles": [{"bounds": [x * self.options.tileSize, y * self.options.tileSize,
                                        (x + 1) * self.options.tileSize, (y + 1) * self.options.tileSize],
                            "pixelCount": n, "meanAbsoluteError": s / n, "maxAbsoluteError": p,
                            "changedPixelFraction": changed / n}
                           for (x, y), (n, s, p, changed) in tiles[:self.options.worstCount]],
        }


def compare_numeric(reference, sha, target, image, channels, tolerance, max_fraction, options, reader):
    options = NumericOptions.model_validate(options or {})
    width, height = image["width"], image["height"]
    if not (isinstance(width, int) and isinstance(height, int) and 0 < width * height <= 8_388_608):
        raise ValueError("Invalid numeric image dimensions or image exceeds eight million pixels")
    selectors = _selectors(options, width, height)
    whole = _Stats(channels, options, tolerance)
    stats = [_Stats(channels, options, tolerance) for _ in selectors]
    lhs = reader(reference, sha, width, height, channels)
    rhs = reader(target, image["sha256"], width, height, channels)
    seen = 0
    for index, (a, b) in enumerate(zip(lhs, rhs, strict=True)):
        y, x = divmod(index, width)
        if y >= height:
            raise ValueError("HDR has more pixels than declared")
        a, b = tuple(a), tuple(b)
        whole.add(x, y, a, b)
        for stat, (region, (x0, y0, x1, y1), mask) in zip(stats, selectors):
            if x0 <= x < x1 and y0 <= y < y1 and (mask is None or mask[index] >= region.mask.threshold):
                stat.add(x, y, a, b)
        seen += 1
    if seen != width * height:
        raise ValueError("HDR pixel count differs from dimensions")
    result = whole.report(options.limits, max_fraction)
    regions = [{"id": region.id, "bounds": bounds,
                "mask": region.mask.model_dump() if region.mask else None,
                **stat.report(ErrorLimits.model_validate({**options.limits.model_dump(exclude_none=True),
                                                        **(region.limits.model_dump(exclude_none=True) if region.limits else {})}),
                              max_fraction)}
               for stat, (region, bounds, _) in zip(stats, selectors)]
    for report in (result, *regions):
        for tile in report["worstTiles"]:
            tile["bounds"][2] = min(width, tile["bounds"][2])
            tile["bounds"][3] = min(height, tile["bounds"][3])
    if any(r["pixelCount"] == 0 for r in regions):
        raise ValueError("Coverage selects zero pixels; empty regions cannot pass")
    return {**result, "wholeFrameWithinTolerance": result["withinTolerance"],
            "withinTolerance": all(r["withinTolerance"] for r in regions) if regions else result["withinTolerance"],
            "acceptanceScope": "regions" if regions else "whole-frame",
            "errorDefinition": "absolute per-channel; pixel quantiles use maximum absolute channel error",
            "quantileMethod": "linear interpolation, type 7; invalid pixels fail separately",
            "regions": regions}
