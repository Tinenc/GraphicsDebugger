from pathlib import Path
import json

from PIL import Image
from src.display_colors import srgb_preview
from src import server


def test_linear_texture_preview_encodes_rgb_and_preserves_original(tmp_path):
    original=tmp_path/'raw.png';Image.new('RGB',(2,2),(128,64,255)).save(original);raw=original.read_bytes()
    result=srgb_preview(original,tmp_path/'previews')
    with Image.open(result['path'])as image:assert image.getpixel((0,0))==(188,137,255)
    assert original.read_bytes()==raw


def test_srgb_texture_storage_is_not_encoded_twice(tmp_path,monkeypatch):
    original=tmp_path/'raw.png';Image.new('RGB',(2,2),(128,64,255)).save(original)
    monkeypatch.setattr(server,'save_texture',lambda **kw:json.dumps(dict(saved=str(original),sourceColorSpace='srgb')))
    result=json.loads(server.capture_texture_image(77,inline=False,display_transfer='linear-to-srgb')[0])
    assert result['displayTransfer']=='identity'and result['path']==str(original)
