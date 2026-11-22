"""Compare one observed Unreal output without modifying evidence or asset files."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from .unity_restore import _file, _sha
from .unity_visual_validation import compare_render_images
from .unreal_integration import project_descriptor, restoration_receipt
from .unreal_recipe import Name, NonNegative, Record, Recipe
from .unreal_numeric_validation import NumericOptions, compare_numeric


def _bytes(path, sha):
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Render evidence exceeds 128 MiB")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("Render evidence hash mismatch")
    return data


def _cstring(data, offset):
    end = data.index(0, offset, offset + 256)
    return data[offset:end].decode("ascii"), end + 1


def _exr_channels(data, width, height, channels):
    """Read uncompressed, single-part scanline EXR without changing component precision."""
    if len(data) < 8 or struct.unpack_from("<II", data) != (20000630, 2):
        raise ValueError("Expected an uncompressed single-part scanline EXR")
    offset = 8
    headers = {}
    while data[offset]:
        name, offset = _cstring(data, offset)
        kind, offset = _cstring(data, offset)
        size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        headers[name] = (kind, data[offset:offset + size])
        offset += size
        if offset >= len(data) or offset > 65536:
            raise ValueError("Invalid EXR header")
    offset += 1
    if headers.get("compression") != ("compression", b"\0"):
        raise ValueError("Use uncompressed EXR or little-endian RGBA32F")
    xmin, ymin, xmax, ymax = struct.unpack("<iiii", headers["dataWindow"][1])
    if (xmax - xmin + 1, ymax - ymin + 1) != (width, height):
        raise ValueError("EXR data window differs from the comparison dimensions")
    header = headers["channels"][1]
    order = []
    start = 0
    while header[start]:
        channel, start = _cstring(header, start)
        code, x, y = struct.unpack_from("<i4xii", header, start)
        start += 16
        if code not in {1, 2} or x != 1 or y != 1:
            raise ValueError("Unsupported EXR channel format")
        order.append((channel, "e" if code == 1 else "f"))
    if not set(channels) <= {c for c, _ in order}:
        raise ValueError("Requested channels are absent from EXR")
    if len({c for c, _ in order}) != len(order):
        raise ValueError("Duplicate EXR channels")
    offsets = struct.unpack_from(f"<{height}Q", data, offset)
    row_positions = {}
    for position in offsets:
        if position < offset + height * 8 or position + 8 > len(data):
            raise ValueError("EXR scanline offset is outside pixel storage")
        row, size = struct.unpack_from("<iI", data, position)
        expected = width * sum(struct.calcsize(code) for _, code in order)
        if (row in row_positions or not ymin <= row <= ymax or size != expected
                or position + 8 + size > len(data)):
            raise ValueError("Invalid EXR scanline")
        row_positions[row] = position + 8
    for y in range(ymin, ymax + 1):
        values = {}
        position = row_positions[y]
        for channel, code in order:
            if channel in channels:
                values[channel] = struct.unpack_from(f"<{width}{code}", data, position)
            position += width * struct.calcsize(code)
        yield from zip(*(values[c] for c in channels))


def _float_pixels(path, sha, width, height, channels):
    data = _bytes(path, sha)
    if path.suffix.lower() == ".exr":
        yield from _exr_channels(data, width, height, channels)
        return
    if path.suffix.lower() != ".rgba32f" or len(data) != width * height * 16:
        raise ValueError("Expected EXR or tightly packed little-endian RGBA32F at the same resolution")
    indices = ["RGBA".index(channel) for channel in channels]
    for pixel in struct.iter_unpack("<4f", data):
        yield tuple(pixel[i] for i in indices)


def _float_compare(reference, sha, target, image, channels, tolerance, max_fraction, numeric_options=None):
    return compare_numeric(reference, sha, target, image, channels, tolerance, max_fraction,
                           numeric_options, _float_pixels)


def _current_receipt(project_path, job_id):
    project = project_descriptor(project_path)
    receipt, directory = restoration_receipt(project, job_id)
    hashes = receipt.get("savedAssetHashes") or {}
    if not hashes or any(_sha(_file(project.parent, rel.replace("\\", "/"))) != digest
                         for rel, digest in hashes.items()):
        raise ValueError("Saved Unreal assets changed after this render")
    for rel, digest in receipt.get("renderConfigHashes", {}).items():
        current = _sha(_file(project.parent, rel)) if (project.parent / rel).exists() else None
        if current != digest:
            raise ValueError("Project rendering configuration changed after this render")
    return receipt, directory


def compare(project_path, job_id, pass_id, reference_path, reference_sha256, component, route,
            reference_context, rendered_context, absolute_tolerance, max_changed_pixel_fraction,
            channels, substitution_reason, fidelity=None, numeric_options=None):
    receipt, directory = _current_receipt(project_path, job_id)
    return _compare(receipt, directory, pass_id, reference_path, reference_sha256, component, route,
                    reference_context, rendered_context, absolute_tolerance, max_changed_pixel_fraction,
                    channels, substitution_reason, fidelity, numeric_options)


def _compare(receipt, directory, pass_id, reference_path, reference_sha256, component, route,
             reference_context, rendered_context, absolute_tolerance, max_changed_pixel_fraction,
             channels, substitution_reason, fidelity=None, numeric_options=None):
    image = next((x for x in receipt["images"] if x["name"] == pass_id), None)
    if image is None:
        raise ValueError("Requested pass was not observed")
    keys = ("captureId", "eventId", "view", "displayTransform", "temporalState", "passMeaning", "encoding")
    if any(k not in reference_context or k not in rendered_context
           or reference_context[k] != rendered_context[k] for k in keys):
        raise ValueError("Align capture identity, pass, camera, encoding, display transform and temporal state")
    source_capture = receipt.get("sourceCapture", {})
    capture_id = source_capture.get("captureId")
    if capture_id and reference_context["captureId"] != capture_id:
        raise ValueError("The reference belongs to a different capture than the managed export")
    if rendered_context["encoding"] != image["encoding"]:
        raise ValueError("Comparison encoding does not match the observed UE pass")
    variant = image.get("variant", "baseline")
    if any(context.get("variant", "baseline") != variant for context in (reference_context, rendered_context)):
        raise ValueError("Reference and target contexts must identify the observed diagnostic variant")
    if not component.strip() or not math.isfinite(absolute_tolerance) or absolute_tolerance < 0:
        raise ValueError("A component name and finite absolute tolerance are required")
    if not math.isfinite(max_changed_pixel_fraction) or not 0 <= max_changed_pixel_fraction <= 1:
        raise ValueError("Invalid changed-pixel fraction")
    if route == "replacement" and not substitution_reason.strip():
        raise ValueError("Describe the source effect and its UE substitute")
    path = Path(image["path"])
    if path.parent != directory:
        raise ValueError("The observed RT is outside the restoration evidence folder")
    target, reference = _file(directory, path.name), Path(reference_path)
    preflight = image.get("materialPreflight")
    if preflight:
        report_path = Path(preflight["path"])
        if report_path.parent != directory:
            raise ValueError("Material preflight report is outside the observation directory")
        report = json.loads(_bytes(_file(directory, report_path.name), preflight["sha256"]))
        if preflight.get("passed") is not True or report.get("passed") is not True:
            raise ValueError("Material compilation preflight did not pass")
    if target.suffix == ".png":
        if numeric_options is not None:
            raise ValueError("Regional numeric options require HDR evidence, not a display PNG")
        result = compare_render_images(str(reference), str(target), reference_sha256, image["sha256"],
                                       channels=channels, absolute_tolerance=absolute_tolerance,
                                       max_changed_pixel_fraction=max_changed_pixel_fraction,
                                       reference_context=reference_context, rendered_context=rendered_context,
                                       mode="strict-pixel" if route == "exact" else "visual-fidelity",
                                       fidelity=fidelity)
        result.pop("schema", None)
    else:
        if fidelity is not None:
            raise ValueError("Use component tolerances for numeric RTs and visual fidelity settings for display PNGs")
        if not channels or len(set(channels)) != len(channels) or set(channels) - set("RGBA"):
            raise ValueError("Select unique RGBA channels")
        result = _float_compare(reference, reference_sha256, target, image, channels,
                                absolute_tolerance, max_changed_pixel_fraction, numeric_options)
        result["comparisonSpace"] = "recorded-numeric-components"
    return {"schema": "bread-unreal-comparison/v1", "operationId": receipt["operationId"],
            "jobId": receipt["jobId"], "pass": image, "component": component, "route": route,
            "referencePath": str(reference), "referenceSha256": reference_sha256,
            "substitution": substitution_reason if route == "replacement" else None,
            "materialPreflightStatus": "verified-report" if preflight else "legacy-unverified",
            "requiresSemanticReview": True, **result}


class LayerReference(Record):
    layer: Name
    observation: Name
    referencePath: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    referenceSha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    referenceContext: dict[str, Any]
    renderedContext: dict[str, Any]
    absoluteTolerance: NonNegative = 1.0 / 255.0
    maxChangedPixelFraction: Annotated[float, Field(ge=0, le=1)] = 0.0
    channels: Annotated[str, StringConstraints(min_length=1, max_length=4)] = "RGB"
    fidelity: dict[str, Any] | None = None
    numericOptions: NumericOptions | None = None


def review_layers(project_path, job_id, references):
    receipt, directory = _current_receipt(project_path, job_id)
    recipe_data = json.loads(_file(directory, "recipe.json").read_bytes())
    encoded = json.dumps(recipe_data, sort_keys=True, ensure_ascii=False).encode()
    if hashlib.sha256(encoded).hexdigest() != receipt["recipeSha256"]:
        raise ValueError("The layer plan differs from the executed recipe")
    recipe = Recipe.model_validate(recipe_data)
    if not recipe.layers:
        raise ValueError("The executed recipe has no layer plan")
    required = {(layer.id, name) for layer in recipe.layers for name in layer.passes}
    if len(references) > len(required):
        raise ValueError("Expected at most one reference per declared layer observation")
    declared = {}
    for value in references:
        ref = LayerReference.model_validate(value)
        key = (ref.layer, ref.observation)
        if key not in required or key in declared:
            raise ValueError("Reference must identify a unique declared layer observation")
        declared[key] = ref
    layers = []
    for layer in recipe.layers:
        observations = []
        for name in layer.passes:
            ref = declared.get((layer.id, name))
            if ref is None:
                observations.append({"observation": name, "status": "reference-missing"})
                continue
            try:
                event = layer.sourceEvents.get(name) or receipt.get("sourceCapture", {}).get("eventId")
                if event and ref.referenceContext.get("eventId") != event:
                    raise ValueError("Reference EID differs from the layer's declared producer")
                compared = _compare(receipt, directory, name, ref.referencePath, ref.referenceSha256,
                                    layer.id, layer.route, ref.referenceContext, ref.renderedContext,
                                    ref.absoluteTolerance, ref.maxChangedPixelFraction, ref.channels,
                                    layer.implementation, ref.fidelity,
                                    ref.numericOptions.model_dump() if ref.numericOptions else None)
                status = "within-tolerance" if compared["withinTolerance"] else "mismatch"
                observations.append({"observation": name, "status": status, "comparison": compared})
            except (ValueError, OSError, IndexError, KeyError, struct.error) as error:
                observations.append({"observation": name, "status": "comparison-error", "diagnostic": str(error)})
        statuses = {item["status"] for item in observations}
        if statuses == {"within-tolerance"}:
            status = "within-tolerance"
        elif "mismatch" in statuses:
            status = "mismatch"
        else:
            status = "incomplete"
        layers.append({**layer.model_dump(), "status": status, "observations": observations})
    pending = [layer["id"] for layer in layers if layer["status"] != "within-tolerance"]
    return {"schema": "bread-unreal-layer-review/v1", "jobId": job_id, "operationId": receipt["operationId"],
            "allWithinTolerance": not pending, "pendingLayers": pending, "requiresSemanticReview": True,
            "layers": layers}
