"""Portable animations derived only from the publication's verified previews."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from io import BytesIO
import os
from pathlib import Path
import tempfile

from PIL import Image


def materialize_animations(document: dict, output_directory: str | Path) -> dict:
    """Keep source PNGs lossless; GIFs are opaque, bounded presentation copies.

    The view plays the original PNG sequence, avoiding GIF palette loss. The
    companion GIF makes the same explanation portable outside Bread.
    """
    result = deepcopy(document)
    root = Path(output_directory)
    for stage in result.get("stages", []):
        for animation in stage.get("animations", []):
            frames = []
            try:
                for index in animation["imageIndices"]:
                    receipt = stage["images"][index]
                    path = Path(receipt["path"])
                    if path.stat().st_size > 128 * 1024 * 1024:
                        raise ValueError("Animation preview exceeds the image byte budget")
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                        raise ValueError("Animation preview changed after evidence verification")
                    with Image.open(BytesIO(raw)) as image:
                        if image.format != "PNG" or image.width * image.height > 16 * 1024 * 1024:
                            raise ValueError("Animation preview exceeds the decoded pixel budget")
                        if (image.width, image.height) != (receipt["width"], receipt["height"]):
                            raise ValueError("Animation preview dimensions do not match its receipt")
                        # Drop alpha, do not composite it: GPU output alpha is
                        # often a material parameter instead of transparency.
                        frame = image.convert("RGB")
                        frame.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                        frames.append(frame)
                buffer = BytesIO()
                frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:],
                               duration=animation["frameDurationMs"], loop=0, disposal=2)
                encoded = buffer.getvalue()
                digest = hashlib.sha256(encoded).hexdigest()
                root.mkdir(parents=True, exist_ok=True)
                destination = root / (digest + ".gif")
                if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    descriptor, scratch = tempfile.mkstemp(prefix=".animation-", suffix=".tmp", dir=str(root))
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(encoded)
                        os.replace(scratch, destination)
                    finally:
                        if os.path.exists(scratch):
                            os.unlink(scratch)
                animation.update(gifPath=str(destination), gifSha256=digest,
                                 frameEventIds=[stage["images"][index]["eventId"]
                                                for index in animation["imageIndices"]])
            finally:
                for frame in frames:
                    frame.close()
    return result
