"""The event-evidence result contains pixels, without changing saved evidence."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from src import server
from src.evidence_image import event_texture_model_image, MODEL_IMAGE_MAX_BYTES


def _receipt(tmp_path: Path, *, size=(2048, 1024), noise=False) -> dict:
    image = (Image.effect_noise(size, 100).convert("RGB") if noise
             else Image.new("RGBA", size, (45, 175, 220, 0)))
    output = tmp_path / "evidence.png"
    image.save(output)
    raw = output.read_bytes()
    return {"schema": "replaylab-event-texture-evidence-host/v1", "eventId": 41,
            "status": "saved", "artifact": {"status": "saved", "output": {
                "path": str(output), "byteSize": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "mimeType": "image/png"}}}


def test_public_capture_returns_actual_image_by_default_without_another_replay(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path)
    calls = []
    def saved(**arguments):
        calls.append(arguments)
        return json.dumps(receipt)
    monkeypatch.setattr(server, "_capture_event_texture_evidence_receipt", saved)
    before = Path(receipt["artifact"]["output"]["path"]).read_bytes()
    blocks = server.capture_event_texture_evidence(41, "rtv")
    result = json.loads(blocks[0])
    assert len(calls) == 1
    content = blocks[1].to_image_content()
    actual = base64.b64decode(content.data)
    assert content.type == "image" and content.mimeType == "image/png"
    assert result["modelImage"]["included"]
    assert result["modelImage"]["sha256"] == hashlib.sha256(actual).hexdigest()
    assert result["modelImage"]["sourceSha256"] == receipt["artifact"]["output"]["sha256"]
    image = Image.open(BytesIO(actual))
    assert image.size == (1024, 512) and image.mode == "RGB"
    # The scene color has alpha zero in the source, but still reaches the model.
    assert image.getpixel((512, 256)) == (45, 175, 220)
    assert Path(receipt["artifact"]["output"]["path"]).read_bytes() == before
    assert "modelImage" not in receipt


def test_full_resolution_noisy_png_is_bounded_for_model_transport(tmp_path):
    receipt = _receipt(tmp_path, size=(2048, 2048), noise=True)
    result, image = event_texture_model_image(receipt, inline=True)
    raw = base64.b64decode(image.to_image_content().data)
    assert len(raw) <= MODEL_IMAGE_MAX_BYTES
    assert max(result["modelImage"]["width"], result["modelImage"]["height"]) <= 1024
    assert result["modelImage"]["resized"]


@pytest.mark.parametrize("state", ["skip", "error", "changed", "metadata"])
def test_unsent_image_is_explicit_and_never_claimed_as_seen(tmp_path, state):
    receipt = _receipt(tmp_path, size=(4, 4))
    if state == "skip":
        receipt["artifact"]["status"] = "skipped"
    elif state == "error":
        receipt = {"error": "no capture"}
    elif state == "changed":
        Path(receipt["artifact"]["output"]["path"]).write_bytes(b"changed")
    result, image = event_texture_model_image(receipt, inline=state != "metadata")
    assert image is None and result["modelImage"]["included"] is False
    assert result["modelImage"]["reason"]
    assert "reviewed" not in result["modelImage"]


def test_metadata_only_does_not_open_image(tmp_path, monkeypatch):
    receipt = _receipt(tmp_path, size=(4, 4))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("metadata must not read pixels"))
    result, image = event_texture_model_image(receipt, inline=False)
    assert image is None and result["modelImage"]["reason"] == "metadata-only-requested"
