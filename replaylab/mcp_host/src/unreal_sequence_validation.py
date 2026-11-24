"""Validate immutable continuous-frame exports without launching an Editor."""
from __future__ import annotations

from array import array
import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat

from .unity_restore import _file, _not_redirected
from .unreal_integration import project_descriptor
from .unreal_numeric_validation import NumericOptions, compare_numeric
from .unreal_recipe import Record
from .unreal_validation import _float_pixels


class Frame(Record):
    frame: Annotated[int, Field(ge=0)]
    timeSeconds: FiniteFloat
    path: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Capture(Record):
    method: Annotated[str, Field(min_length=1, max_length=100)]
    runId: Annotated[str, Field(min_length=1, max_length=200)]
    advancesWorldTime: bool
    resetsHistoryPerFrame: bool
    spatialSamples: Annotated[int, Field(ge=1)] = 1
    temporalSamples: Annotated[int, Field(ge=1)] = 1
    warmupFrames: Annotated[int, Field(ge=0)]


class Sequence(Record):
    schema_version: Literal["bread-unreal-hdr-sequence/v1"] = Field(alias="schema")
    project: str
    size: tuple[Annotated[int, Field(gt=0)], Annotated[int, Field(gt=0)]]
    encoding: Annotated[str, Field(min_length=1)]
    channels: Annotated[str, Field(pattern=r"^[RGBA]{1,4}$")] = "RGB"
    context: dict
    capture: Capture
    mode: Literal["steady", "motion"]
    frameRate: Annotated[FiniteFloat, Field(gt=0, le=1000)]
    frames: Annotated[list[Frame], Field(min_length=2, max_length=256)]
    savedAssetHashes: dict[str, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = {}
    renderConfigHashes: dict[str, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None] = {}


def review_sequence(project_path, manifest_path, manifest_sha256, numeric_options=None,
                    absolute_tolerance=0., max_changed_pixel_fraction=0., required_frames=8):
    from math import isfinite
    if (not isfinite(absolute_tolerance) or absolute_tolerance < 0
            or not isfinite(max_changed_pixel_fraction) or not 0 <= max_changed_pixel_fraction <= 1
            or not 2 <= required_frames <= 256):
        raise ValueError("Invalid temporal comparison tolerances or required frame count")
    project = project_descriptor(project_path)
    path = Path(manifest_path).absolute()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Sequence manifest exceeds 1 MiB")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest_sha256:
        raise ValueError("Sequence manifest hash mismatch")
    sequence = Sequence.model_validate_json(data)
    if project_descriptor(sequence.project) != project:
        raise ValueError("Sequence belongs to another Unreal project")
    from .unity_restore import _sha
    for relative, digest in sequence.savedAssetHashes.items():
        if _sha(_file(project.parent, relative)) != digest:
            raise ValueError("Saved Unreal asset changed after this sequence")
    for relative, digest in sequence.renderConfigHashes.items():
        if (not relative or "\\" in relative or ":" in relative or Path(relative).is_absolute()
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise ValueError("Rendering configuration path must stay inside the selected project")
        config = project.parent / relative
        _not_redirected(config)
        current = _sha(_file(project.parent, relative)) if config.exists() else None
        if current != digest:
            raise ValueError("Rendering configuration changed after this sequence")
    if len(set(sequence.channels)) != len(sequence.channels):
        raise ValueError("Sequence channels must be unique")
    if not all(key in sequence.context for key in ("captureId", "eventId", "view", "passMeaning", "displayTransform")):
        raise ValueError("Sequence context needs capture, event, view, pass meaning and display transform")
    if sequence.encoding in {"display-srgb", "png"}:
        raise ValueError("Temporal HDR checks require numeric encoding")
    width, height = sequence.size
    if width * height > 4_194_304:
        raise ValueError("Temporal HDR statistics currently support at most four million pixels per frame")
    options = NumericOptions.model_validate(numeric_options or {}).model_dump()
    issues = []
    capture = sequence.capture
    if not capture.advancesWorldTime or capture.resetsHistoryPerFrame:
        issues.append("Capture does not preserve consecutive runtime frames and temporal history")
    if any(word in capture.method.lower() for word in ("highresshot", "high-res-shot", "scene-capture-loop")):
        issues.append("Repeated screenshots/SceneCapture calls cannot establish runtime continuity")
    if capture.spatialSamples != 1 or capture.temporalSamples != 1:
        issues.append("Export averages multiple samples; it cannot certify single-frame stability")
    if len(sequence.frames) < required_frames:
        issues.append("Fewer frames than the requested acceptance count")
    frames = sequence.frames
    paths = [_file(path.parent, f.path) for f in frames]
    if len(set(paths)) != len(paths):
        raise ValueError("Sequence reuses an image path")
    # Inspect all artifacts before expensive temporal statistics; exit status or
    # a completed callback cannot stand in for missing/empty/wrong-size outputs.
    for frame, file in zip(frames, paths):
        stream = _float_pixels(file, frame.sha256, width, height, sequence.channels)
        try:
            next(stream)
        finally:
            stream.close()
    for a, b in zip(frames, frames[1:]):
        if b.frame != a.frame + 1:
            issues.append("Frame indices contain a gap, duplicate or reversal")
            break
    for a, b in zip(frames, frames[1:]):
        if abs((b.timeSeconds - a.timeSeconds) - 1 / sequence.frameRate) > 1e-5:
            issues.append("Frame timestamps are not consecutive at the declared frame rate")
            break
    channels = sequence.channels
    first = previous = low = high = None
    pairs = []

    def pixels(values):
        n = len(channels)
        return (tuple(values[i:i + n]) for i in range(0, len(values), n))

    def measure(a, b):
        # All actual files were hashed and decoded before this in-memory comparison.
        return compare_numeric("reference", "", "target", {"width": width, "height": height, "sha256": ""},
                               channels, absolute_tolerance, max_changed_pixel_fraction, options,
                               lambda file, *_: pixels(a if file == "reference" else b))

    for frame, file in zip(frames, paths):
        values = array("f")
        for pixel in _float_pixels(file, frame.sha256, width, height, channels):
            values.extend(pixel)
        if len(values) != width * height * len(channels):
            raise ValueError("Frame dimensions do not match manifest")
        if first is None:
            first = values
            low, high = array("f", values), array("f", values)
        else:
            pairs.append({"fromFrame": frame.frame - 1, "toFrame": frame.frame, **measure(previous, values)})
            for i, value in enumerate(values):
                # Preserve any invalid sample in the envelope so it cannot disappear through min/max.
                if not isfinite(value) or not isfinite(low[i]):
                    low[i] = high[i] = float("nan")
                else:
                    low[i], high[i] = min(low[i], value), max(high[i], value)
        previous = values
    envelope = measure(low, high)
    returned = measure(first, previous)
    eligible = not issues and sequence.mode == "steady"
    return {"schema": "bread-unreal-hdr-sequence-review/v1", "project": str(project),
            "manifestPath": str(path), "manifestSha256": manifest_sha256,
            "context": sequence.context, "encoding": sequence.encoding, "capture": capture.model_dump(),
            "frameCount": len(frames), "requiredFrames": required_frames, "mode": sequence.mode,
            "assetStateVerified": bool(sequence.savedAssetHashes),
            "renderConfigStateVerified": bool(sequence.renderConfigHashes),
            "snapshotScope": "Only supplied asset/config paths; dependency completeness is not independently proved",
            "metadataContinuous": not issues, "issues": issues,
            "provenance": "Producer-declared frame IDs/times; file hashes and dimensions verified, runtime origin not independently attested",
            "stabilityEligible": eligible,
            "withinTolerance": eligible and envelope["withinTolerance"] and all(p["withinTolerance"] for p in pairs),
            "steadyRange": envelope if sequence.mode == "steady" else None,
            "motionRange": envelope if sequence.mode == "motion" else None,
            "firstToLast": returned, "adjacentFrames": pairs, "requiresSemanticReview": True}
