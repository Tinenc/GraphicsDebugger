"""Bounded model-visible previews of already validated event texture evidence.

The full PNG and manifest remain the authority. This module changes only the
transport preview: RGB conversion precedes resizing so zero-alpha scene pixels
are not lost, and no new replay/export is performed.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image as PillowImage


MODEL_IMAGE_MAX_DIMENSION = 1024
MODEL_IMAGE_MAX_BYTES = 1_200_000
SOURCE_MAX_PIXELS = 32 * 1024 * 1024
SOURCE_MAX_BYTES = 128 * 1024 * 1024


def event_texture_model_image(payload: dict[str, Any], *, inline: bool) -> tuple[dict[str, Any], Any]:
    """Return a receipt plus real MCP image content, or a precise omission fact.

    Only accepts the Host's verified saved-event receipt, not an arbitrary path.
    Inclusion means bytes are in this result; provider support is decided by the
    downstream connection. Never mark a picture as reviewed on the model's behalf.
    """

    from mcp.server.fastmcp import Image

    receipt = dict(payload)
    delivery: dict[str, Any] = {
        "included": False,
        "contentType": "image",
        "mimeType": "image/png",
        "transport": "mcp-image-content",
    }
    receipt["modelImage"] = delivery
    if not inline:
        delivery["reason"] = "metadata-only-requested"
        return receipt, None
    artifact = payload.get("artifact") or {}
    output = artifact.get("output") or {}
    if payload.get("error") or artifact.get("status") != "saved":
        delivery["reason"] = "no-saved-image"
        return receipt, None
    try:
        target = Path(output["path"])
        expected_size = int(output["byteSize"])
        if expected_size < 1 or expected_size > SOURCE_MAX_BYTES:
            raise ValueError("saved image exceeds the source byte budget")
        with target.open("rb") as stream:
            raw = stream.read(expected_size + 1)
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != output["sha256"]:
            raise ValueError("saved image no longer matches its verified receipt")
        with PillowImage.open(BytesIO(raw)) as source:
            if source.format != "PNG" or source.width * source.height > SOURCE_MAX_PIXELS:
                raise ValueError("saved PNG exceeds the source pixel budget or has the wrong format")
            original_size = source.size
            # PNG export can preserve alpha=0 on scene RGB. Premultiplied resize
            # would erase it and falsely show an isolated UI layer on black.
            preview = source.convert("RGB")
        preview.thumbnail((MODEL_IMAGE_MAX_DIMENSION, MODEL_IMAGE_MAX_DIMENSION),
                          PillowImage.Resampling.LANCZOS)
        while True:
            encoded = BytesIO()
            preview.save(encoded, format="PNG")
            image_bytes = encoded.getvalue()
            if len(image_bytes) <= MODEL_IMAGE_MAX_BYTES:
                break
            if max(preview.size) <= 128:
                raise ValueError("preview cannot fit the model image byte budget")
            preview = preview.resize((max(1, preview.width // 2), max(1, preview.height // 2)),
                                     PillowImage.Resampling.LANCZOS)
        delivery.update({
            "included": True,
            "sourceSha256": output["sha256"],
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "byteSize": len(image_bytes),
            "width": preview.width, "height": preview.height,
            "sourceWidth": original_size[0], "sourceHeight": original_size[1],
            "alphaMode": "opaque-rgb",
            "resized": preview.size != original_size,
        })
        return receipt, Image(data=image_bytes, format="png")
    except (OSError, KeyError, TypeError, ValueError, PillowImage.DecompressionBombError) as error:
        delivery["reason"] = "preview-unavailable"
        delivery["detail"] = str(error)
        return receipt, None
