"""A bounded clip-space wireframe image, not a rendered visibility mask."""
from __future__ import annotations

import io
import math
from typing import Any, Mapping

from PIL import Image, ImageDraw


def _primitives(indices: list[int | None], topology: str):
    strip: list[int] = []
    width = {"PointList": 1, "LineList": 2, "TriangleList": 3}.get(topology)
    for index in indices:
        if index is None:
            strip.clear()
            continue
        strip.append(index)
        if width and len(strip) == width:
            yield tuple(strip)
            strip.clear()
        elif topology == "LineStrip" and len(strip) >= 2:
            yield tuple(strip[-2:])
        elif topology == "TriangleStrip" and len(strip) >= 3:
            yield tuple(strip[-3:])
        elif topology == "TriangleFan" and len(strip) >= 3:
            yield (strip[0], strip[-2], strip[-1])


def _clip(a: tuple[float, float], b: tuple[float, float]):
    """Liang-Barsky clip before raster coordinates reach Pillow's C integers."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    low, high = 0.0, 1.0
    for p, q in ((-dx, a[0] + 1), (dx, 1 - a[0]), (-dy, a[1] + 1), (dy, 1 - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        ratio = q / p
        if p < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return None
    return ((a[0] + low * dx, a[1] + low * dy), (a[0] + high * dx, a[1] + high * dy))


def projected_positions(mesh: Mapping[str, Any]) -> list[tuple[float, float] | None]:
    if mesh.get("coordinateSpace") != "clip":
        raise ValueError("Native post-transform data does not identify clip-space positions")
    result = []
    for position in mesh.get("positions", [])[:16384]:
        if (not isinstance(position, list) or len(position) != 4 or
                not all(isinstance(value, (int, float)) and math.isfinite(value) for value in position)
                or position[3] <= 1e-12):
            result.append(None)
            continue
        x, y = position[0] / position[3], position[1] / position[3]
        # Extreme quotients carry no useful pixel precision and can overflow clipping.
        result.append((x, -y if mesh.get("flipY") else y)
                      if math.isfinite(x) and math.isfinite(y) and abs(x) < 1e12 and abs(y) < 1e12 else None)
    return result


def footprint(mesh: Mapping[str, Any]) -> dict[str, Any] | None:
    """Only a complete single-instance position read can safely bound an RT crop."""
    if (not mesh.get("complete") or not mesh.get("allInstances") or
            not mesh.get("finalPositionStage") or mesh.get("viewCount", 1) != 1):
        return None
    positions = projected_positions(mesh)
    referenced = [positions[index] for index in mesh.get("indices", [])
                  if isinstance(index, int) and 0 <= index < len(positions)]
    if not referenced or any(value is None for value in referenced):
        return None
    xs, ys = zip(*referenced)
    return {"ndcBounds": {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)},
            "sampledVertices": len(referenced), "rasterCoverageVerified": False}


def wireframe(mesh: Mapping[str, Any], max_dimension: int = 512) -> tuple[bytes, dict[str, Any]]:
    if isinstance(max_dimension, bool) or not 64 <= max_dimension <= 2048:
        raise ValueError("max_dimension must be an integer in 64..2048")
    topology = str(mesh.get("topology", "")).rsplit(".", 1)[-1]
    if topology not in {"PointList", "LineList", "LineStrip", "TriangleList", "TriangleStrip", "TriangleFan"}:
        raise ValueError("This wireframe renderer does not support topology " + topology)
    positions = projected_positions(mesh)
    indices = mesh.get("indices", [])[:49152]
    if not positions or not indices:
        raise ValueError("No decoded post-transform geometry is available")
    viewport = mesh.get("viewport") or {}
    width, height = max_dimension, max_dimension
    vx, vy = viewport.get("width"), viewport.get("height")
    if (isinstance(vx, (int, float)) and isinstance(vy, (int, float)) and
            math.isfinite(vx) and math.isfinite(vy) and vx > 0 and vy > 0):
        aspect = vx / vy
        if 0.05 <= aspect <= 20:
            width, height = ((max_dimension, max(32, round(max_dimension / aspect)))
                             if aspect >= 1 else (max(32, round(max_dimension * aspect)), max_dimension))
    canvas = Image.new("RGB", (width, height), (32, 35, 39))
    draw = ImageDraw.Draw(canvas)
    pad, span_x, span_y = 8, width - 17, height - 17
    draw.rectangle((pad, pad, pad + span_x, pad + span_y), outline=(96, 104, 112))

    def pixel(p):
        return (pad + (p[0] + 1) * 0.5 * span_x, pad + (1 - p[1]) * 0.5 * span_y)

    primitives, lines, skipped = 0, 0, 0
    for primitive in _primitives(indices, topology):
        if any(not 0 <= index < len(positions) or positions[index] is None for index in primitive):
            skipped += 1
            continue
        points = [positions[index] for index in primitive]
        primitives += 1
        if len(points) == 1:
            if all(-1 <= value <= 1 for value in points[0]):
                draw.point(pixel(points[0]), fill=(132, 216, 240))
                lines += 1
            continue
        edges = zip(points, points[1:] + points[:1]) if len(points) == 3 else [(points[0], points[1])]
        for a, b in edges:
            clipped = _clip(a, b)
            if clipped:
                draw.line([pixel(clipped[0]), pixel(clipped[1])], fill=(132, 216, 240), width=1)
                lines += 1
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue(), {
        "projection": "clip-xy-divided-by-w", "width": width, "height": height,
        "primitiveCount": primitives, "drawnSegments": lines, "skippedPrimitives": skipped,
        "depthTested": False, "culled": False, "shaderShaded": False,
        "description": "Post-transform wireframe of the selected instance/view; not pixel visibility or a scene render.",
    }
