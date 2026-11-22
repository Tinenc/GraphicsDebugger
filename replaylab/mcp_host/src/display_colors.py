"""Display-only transfer for sampled linear texture previews; source bytes stay intact."""
import hashlib
import io
from pathlib import Path

from PIL import Image


def srgb_preview(source,output_directory):
    source=Path(source);raw=source.read_bytes()
    table=[]
    for value in range(256):
        linear=value/255.
        encoded=12.92*linear if linear<=.0031308 else 1.055*linear**(1/2.4)-.055
        table.append(round(encoded*255))
    with Image.open(io.BytesIO(raw))as image:
        if image.width*image.height>16000000:raise ValueError('Texture preview exceeds the pixel budget')
        image=image.convert('RGB').point(table*3)
        stream=io.BytesIO();image.save(stream,format='PNG');data=stream.getvalue()
    digest=hashlib.sha256(data).hexdigest();target=Path(output_directory)/(digest+'.png')
    from .visual_replies import _write
    _write(target,data)
    return dict(path=str(target),sha256=digest,sourceSha256=hashlib.sha256(raw).hexdigest())
