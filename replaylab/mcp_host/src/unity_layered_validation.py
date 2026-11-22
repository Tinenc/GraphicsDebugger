"""Optional per-layer numerical observations; no workflow or acceptance gate."""
from array import array
import hashlib
import math
import json
import struct
from pathlib import Path
import sys

from PIL import Image
from .file_content_cache import file_stamp


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _values(row):
    path = Path(row["path"])
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("Comparison needs an existing bounded absolute file")
    row = dict(row)
    for sidecar in (Path(str(path) + ".json"), path.with_suffix(".json")):
        if sidecar.is_file() and sidecar.stat().st_size < 1024 * 1024:
            metadata = json.loads(sidecar.read_text(encoding="utf-8-sig"))
            if isinstance(metadata, dict):
                row = {**metadata, **row}
            break
    suffix = path.suffix.lower()
    if type(row.get("flipY", False)) is not bool:
        raise ValueError("flipY must be an explicitly chosen Boolean")
    row["encoding"] = {"rgba32f":"float32le","rgba16f":"float16le","float32":"float32le","float16":"float16le"}.get(row.get("encoding"),row.get("encoding"))
    raw_formats = {".rgba32f": ("float32le", 4), ".rgb32f": ("float32le", 3),
                   ".r32f": ("float32le", 1), ".rgba16f": ("float16le", 4)}
    if suffix in raw_formats:
        encoding, channels = raw_formats[suffix]
        if not row.get("encoding"):
            row["encoding"] = encoding
        row.setdefault("channels", channels)
    if row.get("encoding") in {"float32le", "float16le"}:
        shape = tuple(row.get(key) for key in ("width", "height", "channels"))
        stride = 4 if row["encoding"] == "float32le" else 2
        if any(type(n) is not int or n <= 0 for n in shape) or shape[2] > 4 or math.prod(shape) * stride != path.stat().st_size:
            raise ValueError("Raw float data needs width/height/channels matching its bytes; provide dimensions or a JSON sidecar")
        def values():
            with path.open("rb") as stream:
                if row.get("flipY"):
                    row_bytes = shape[0]*shape[2]*stride
                    for y in reversed(range(shape[1])):
                        stream.seek(y*row_bytes); raw = stream.read(row_bytes)
                        if stride == 2:
                            yield from (v[0] for v in struct.iter_unpack("<e", raw))
                        else:
                            block = array("f"); block.frombytes(raw)
                            if sys.byteorder != "little": block.byteswap()
                            yield from block
                    return
                for raw in iter(lambda: stream.read(1024 * 1024), b""):
                    if stride == 2:
                        yield from (v[0] for v in struct.iter_unpack("<e", raw))
                        continue
                    block = array("f"); block.frombytes(raw)
                    if sys.byteorder != "little":
                        block.byteswap()
                    yield from block
        return shape, values(), _sha(path)
    if suffix == ".exr":
        raise ValueError("Use the paired linear .rgba32f + JSON readback for HDR comparison; do not quantize EXR to a PNG")
    if suffix in {".bin", ".raw", ".f32", ".rgba32f", ".rgba16f"}:
        raise ValueError("Raw texture data needs explicit decoded float encoding and dimensions; packed/compressed bytes need their source-format decoder")
    with Image.open(path) as image:
        if image.width * image.height > 32 * 1024 * 1024:
            raise ValueError("Image exceeds comparison dimensions")
        image = image.convert("RGB")
        if row.get("flipY"):
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return (image.width, image.height, 3), (value / 255.0 for pixel in image.getdata() for value in pixel), _sha(path)


def compare_layers(layers, objective="strict-fidelity"):
    if not isinstance(layers, list) or not 1 <= len(layers) <= 128:
        raise ValueError("Provide 1 to 128 layer/pass comparisons")
    results = []
    for layer in layers:
        source_rows=[layer["reference"],layer["rendered"]]
        contribution=any(key in layer for key in ("referenceBefore","renderedBefore"))
        if contribution:
            if not layer.get("referenceBefore") or not layer.get("renderedBefore"):
                raise ValueError("Effect contributions require both referenceBefore and renderedBefore")
            source_rows.extend([layer["referenceBefore"],layer["renderedBefore"]])
        stamps=[file_stamp(Path(row["path"])) for row in source_rows]
        category = layer.get("category", "ported-core")
        if category not in {"ported-core", "native-engine", "post-process", "full-frame"}:
            raise ValueError("Unknown comparison category")
        shape, left, reference_sha = _values(layer["reference"])
        target_shape, right, target_sha = _values(layer["rendered"])
        if shape != target_shape:
            raise ValueError("Use the same explicit RT dimensions/channels; no automatic resize, crop or recoloring")
        before_hashes=[]
        if contribution:
            left_shape,left_before,left_sha=_values(layer["referenceBefore"])
            right_shape,right_before,right_sha=_values(layer["renderedBefore"])
            if left_shape!=shape or right_shape!=shape:
                raise ValueError("Before/after contributions need identical explicit pixel layouts")
            # Bind the original iterators before creating lazy differences.
            left=((after-before) for after,before in zip(left,left_before))
            right=((after-before) for after,before in zip(right,right_before))
            before_hashes=[left_sha,right_sha]
        count = invalid = violations = 0
        decoded=compared_pixels=changed_pixels=0
        changed_pixel=-1;bounds=None
        threshold = layer.get("tolerance") or {}
        absolute_tolerance, relative_tolerance = threshold.get("absolute", 1e-5), threshold.get("relative", 1e-4)
        for tolerance in (absolute_tolerance, relative_tolerance):
            if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError("Tolerance must be finite and non-negative")
        roi = layer.get("roi")
        if roi is not None and (not isinstance(roi, list) or len(roi) != 4 or
            any(type(n) is not int for n in roi) or min(roi) < 0 or
            roi[2] <= 0 or roi[3] <= 0 or roi[0] + roi[2] > shape[0] or roi[1] + roi[3] > shape[1]):
            raise ValueError("ROI must be [x,y,width,height] within the identical RTs")
        channel_bias = [0.0] * shape[2]
        channel_abs = [0.0] * shape[2]
        channel_counts = [0] * shape[2]
        tiles = {}
        absolute = square = maximum = sx = sy = sxx = syy = sxy = 0.0
        for index, (a, b) in enumerate(zip(left, right)):
            decoded+=1
            pixel, channel = divmod(index, shape[2]); y, x = divmod(pixel, shape[0])
            if roi is not None and not (roi[0] <= x < roi[0]+roi[2] and roi[1] <= y < roi[1]+roi[3]):
                continue
            compared_pixels+=channel==0
            over=not math.isfinite(a) or not math.isfinite(b) or abs(a-b)>absolute_tolerance+relative_tolerance*abs(a)
            if over:
                if pixel!=changed_pixel:changed_pixels+=1;changed_pixel=pixel
                bounds=[x,y,x,y] if bounds is None else [min(bounds[0],x),min(bounds[1],y),max(bounds[2],x),max(bounds[3],y)]
            if not math.isfinite(a) or not math.isfinite(b):
                invalid += 1
                continue
            error = abs(a - b)
            violations += error > absolute_tolerance + relative_tolerance * abs(a)
            channel_bias[channel] += b - a; channel_abs[channel] += error; channel_counts[channel] += 1
            tile = tiles.setdefault((x // 32, y // 32), [0, 0]); tile[0] += error; tile[1] += 1
            absolute += error; square += error * error; maximum = max(maximum, error)
            sx += a; sy += b; sxx += a * a; syy += b * b; sxy += a * b; count += 1
        if decoded!=math.prod(shape):
            raise ValueError("Readback changed or returned incomplete pixels during comparison")
        digests=[reference_sha,target_sha,*before_hashes]
        for row,stamp,digest in zip(source_rows,stamps,digests):
            path=Path(row["path"])
            if (stamp is not None and stamp!=file_stamp(path)) or (stamp is None and _sha(path)!=digest):
                raise ValueError("Readback changed during comparison; reuse the completed capture instead of a live file")
        metrics = {"comparedComponents": count, "nonFiniteComponents": invalid,
                   "componentsOverTolerance": violations, "roi": roi,
                   "comparedPixels":compared_pixels,"pixelsOverTolerance":changed_pixels,
                   "changedPixelFraction":changed_pixels/max(1,compared_pixels),
                   "differenceBounds":None if bounds is None else [bounds[0],bounds[1],bounds[2]-bounds[0]+1,bounds[3]-bounds[1]+1],
                   "tolerance": {"absolute": absolute_tolerance, "relative": relative_tolerance},
                   "channelMeanSignedError": [v/max(1,n) for v,n in zip(channel_bias,channel_counts)],
                   "channelMeanAbsoluteError": [v/max(1,n) for v,n in zip(channel_abs,channel_counts)],
                   "worstTiles": [{"x": key[0]*32, "y": key[1]*32, "meanAbsoluteError": value[0]/value[1]}
                       for key,value in sorted(tiles.items(),key=lambda row:row[1][0]/row[1][1],reverse=True)[:8]]}
        if count:
            mx, my = sx / count, sy / count
            vx, vy, covariance = max(0, sxx / count - mx * mx), max(0, syy / count - my * my), sxy / count - mx * my
            data_range = layer.get("dataRange", 1.0)
            if type(data_range) not in (int, float) or not math.isfinite(data_range) or data_range <= 0:
                raise ValueError("dataRange must be a fixed positive comparison range")
            c1, c2 = (0.01 * data_range) ** 2, (0.03 * data_range) ** 2
            mse = square / count
            metrics.update(meanAbsoluteError=absolute / count, maximumAbsoluteError=maximum,
                rootMeanSquareError=math.sqrt(mse), psnr=None if mse == 0 else 10 * math.log10(data_range * data_range / mse),
                globalSsim=((2 * mx * my + c1) * (2 * covariance + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2)))
            if category == "native-engine" and mx > 0 and my > 0:
                metrics["suggestedExposureDeltaEV"] = math.log2(mx / my)
        fixture = layer.get("inputFixture") or {}
        isolated = None
        if fixture.get("referencePath") and fixture.get("renderedPath"):
            isolated = _sha(fixture["referencePath"]) == _sha(fixture["renderedPath"])
        results.append({"name": layer.get("name", ""), "category": category, "shape": list(shape),
            "referenceSha256": reference_sha, "renderedSha256": target_sha, "metrics": metrics,
            "referenceOrigin":layer.get("referenceOrigin","unspecified"),
            "module":layer.get("module") or layer.get("sharedShader") or "",
            "comparisonMode":"after-minus-before" if contribution else "output",
            "beforeSha256":before_hashes,"evidencePaths":[row["path"] for row in source_rows],
            "identicalInputFixture": isolated,
            "guidance": "Compare the same isolated inputs with a small numerical error budget" if category == "ported-core" else
                "Use a shared fixed RT/input fixture for this pass; distinguish its error from upstream accumulated error" if category == "post-process" else
                "Inspect source shadow/lighting algorithms and shared inputs; native replacement does not waive the fidelity target" if category == "native-engine" else
                "Assess final camera composition after reviewing isolated layers"})
    groups = {}
    for source, result in zip(layers, results):
        group = str(source.get("module") or source.get("sharedShader") or "")
        if group:
            groups.setdefault(group, []).append({"name": result["name"], "metrics": result["metrics"]})
    first = next((r["name"] for r in results if r["metrics"]["componentsOverTolerance"] or r["metrics"]["nonFiniteComponents"]), None)
    return {"schema": "bread-unity-layer-comparison/v1", "layers": results, "diagnosticsOnly": True,
        "objective": objective, "firstDivergentLayer": first, "sharedModuleGroups": groups,
        "firstDivergentLayerScope":"Supplied list order only; inspect actual dependencies for a causal boundary",
        "acceptanceBy": "agent", "automaticImageAdjustment": False,
        "metricScope": "MAE/RMSE/PSNR and fast global SSIM; no local/perceptual or algorithm-equivalence proof"}
