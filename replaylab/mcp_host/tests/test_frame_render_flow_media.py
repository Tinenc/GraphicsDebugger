import hashlib
from pathlib import Path

import pytest
from PIL import Image

from src.frame_render_flow_media import materialize_animations
from src.frame_render_flow import _animations, _channel_parents


def test_opaque_gif_and_tampered_preview(tmp_path):
    images = []
    for index, color in enumerate(((255, 0, 0, 0), (0, 255, 0, 0))):
        path = tmp_path / (str(index) + ".png")
        Image.new("RGBA", (16, 16), color).save(path)
        images.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "width": 16, "height": 16, "eventId": index + 1, "channel": "rgba"})
    animations = _animations([{"imageIndices": [0, 1], "label": "Lighting accumulation",
                               "description": "Adds the next light.", "frameDurationMs": 300}], images)
    source = {"stages": [{"images": images, "animations": animations}]}
    document = materialize_animations(source, tmp_path / "media")
    saved = document["stages"][0]["animations"][0]
    with Image.open(saved["gifPath"]) as gif:
        assert gif.n_frames == 2
        assert gif.info["duration"] == 300
        assert gif.convert("RGBA").getpixel((0, 0)) == (255, 0, 0, 255)
        gif.seek(1)
        assert gif.convert("RGBA").getpixel((0, 0)) == (0, 255, 0, 255)
    assert "gifPath" not in animations[0]
    assert materialize_animations(source, tmp_path / "media") == document
    Path(images[0]["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        materialize_animations(source, tmp_path / "media")


@pytest.mark.parametrize("change,reason", [
    ({"imageIndices": [0, 0]}, "unique"), ({"imageIndices": [0, 2]}, "integer"),
    ({"frameDurationMs": 5}, "integer"), ({"imageIndices": [0]}, "2 to 16"),
])
def test_invalid_animation_rejected(change, reason):
    row = {"label": "A", "description": "B", "imageIndices": [0, 1], **change}
    with pytest.raises(ValueError, match=reason):
        _animations([row], [{"width": 16, "height": 16}] * 2)


def test_channel_cannot_attach_to_unrelated_texture():
    images = [{"group": "outputs", "eventId": 1, "resourceId": 3},
              {"group": "channels", "eventId": 1, "resourceId": 4, "parentImageIndex": 0}]
    with pytest.raises(ValueError, match="same event"):
        _channel_parents(images)
    images[1]["resourceId"] = 3
    _channel_parents(images)
