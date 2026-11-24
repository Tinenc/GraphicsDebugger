"""Immutable, bounded visual answers. Presentation never changes capture/replay.

Only content-addressed local PNG/JSON assets reach the native chat renderer.
Captured sources are acquired by the Host; file references and material demos
remain explicitly illustrative. Native clients independently verify every hash.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
_HEX = re.compile(r"^[0-9a-f]{64}$")


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ImageSource(Spec):
    eventId: int = Field(default=0, ge=0, le=0xffffffff)
    path: str = Field(default="", max_length=32768)
    attachmentKind: Literal["rtv", "dsv", "uav", "srv", "present"] = "rtv"
    attachmentIndex: int = Field(default=0, ge=0, le=4095)
    channel: Literal["rgba", "r", "g", "b", "a", "depth"] = "rgba"
    mip: int = Field(default=0, ge=0, le=31)
    slice: int = Field(default=0, ge=0, le=0xffffffff)
    sample: int = Field(default=0, ge=0, le=0xffffffff)
    stage: Literal['fragment','vertex','geometry','compute','hull','domain','task','mesh'] = 'fragment'
    flipY: bool | None = None
    rangeMode: Literal["native", "explicit", "visible-distribution"] = "native"
    blackPoint: float | None = None
    whitePoint: float | None = None

    @model_validator(mode="after")
    def source(self):
        if bool(self.eventId) == bool(self.path):
            raise ValueError("Choose exactly one captured eventId or evidence-file path")
        if any(x is not None and not math.isfinite(x) for x in (self.blackPoint,self.whitePoint)):
            raise ValueError("Display points must be finite")
        return self


class EffectDrawExclusion(Spec):
    eventId: int = Field(ge=1, le=0xffffffff)
    evidence: str = Field(min_length=20, max_length=600)
    experimentReceipt: str = Field(min_length=1, max_length=32768)


def neighbouring_composite_draws(actions: list[dict], event_id: int) -> list[int]:
    """Find adjacent draws worth checking, without asserting that they contribute."""
    index = next((i for i, row in enumerate(actions) if row['eventId'] == event_id), None)
    if index is None or not actions[index].get('outputs') or not actions[index].get('numIndices'):
        return []
    fields = ('markerPath', 'outputs', 'flags', 'numIndices', 'numInstances')
    signature = tuple(actions[index].get(key) for key in fields)
    neighbours = []
    for step in (-1, 1):
        for offset in range(1, 5):
            pos = index + step * offset
            if not 0 <= pos < len(actions):
                break
            row = actions[pos]
            if tuple(row.get(key) for key in fields) != signature:
                break
            neighbours.append(row['eventId'])
    return sorted(neighbours)


def _combined_baseline_matches(cards: list[dict], root: Path) -> bool:
    if len(cards) < 2:
        return True
    first = cards[0]
    with Image.open(root/'blobs'/first['image']['file']) as image:
        width, height = image.size
        roi = first['experiment']['roi']
        box = (int(roi[0]*width), int(roi[1]*height), math.ceil(roi[2]*width), math.ceil(roi[3]*height))
        baseline = image.crop(box).convert('RGB')
    for card in cards[1:]:
        if card['image']['sha256'] == first['image']['sha256']:
            continue
        with Image.open(root/'blobs'/card['image']['file']) as image:
            if image.size != (width, height):
                return False
            delta = ImageChops.difference(baseline, image.crop(box).convert('RGB'))
        # Validated replay baselines may differ by one quantization step.
        if any(maximum > 1 for _, maximum in delta.getextrema()):
            return False
    return True


class Mark(Spec):
    type: Literal["ellipse", "rect", "arrow", "label", "pen"]
    points: list[list[float]] = Field(min_length=1, max_length=128)
    color: str = Field(default="#ff4050", pattern=r"^#[a-fA-F0-9]{6}$")
    text: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def coordinates(self):
        if any(len(p)!=2 or any(not math.isfinite(v) or not 0<=v<=1 for v in p) for p in self.points):
            raise ValueError("Annotation points must be normalized to the saved image, top-left origin")
        if self.type in {"ellipse","rect","arrow"} and len(self.points)!=2:
            raise ValueError("Circle, rectangle and arrow require two points; the arrow tip is last")
        return self


class Link(Spec):
    label: str = Field(min_length=1,max_length=160)
    url: str = Field(max_length=8192)

    @model_validator(mode="after")
    def navigation(self):
        u=urlsplit(self.url)
        if (u.scheme!="rdoc" or u.netloc not in {"event","eid","resource","texture","buffer","shader","mesh","cbuffer","uv"}
                or any(ord(c)<32 for c in self.url+self.label)):
            raise ValueError("Visual links must be RenderDoc event/resource/Shader navigation")
        return self


class Material(Spec):
    baseColor: list[float] = Field(default_factory=lambda:[.65,.68,.75], min_length=3,max_length=3)
    roughness: float = Field(default=.4,ge=.02,le=1)
    metalness: float = Field(default=0,ge=0,le=1)
    environmentColor: list[float] = Field(default_factory=lambda:[.2,.2,.2],min_length=3,max_length=3)
    environmentStrength: float = Field(default=.25,ge=0,le=2)
    texture: ImageSource | None = None

    @model_validator(mode="after")
    def finite(self):
        if any(not math.isfinite(v) or not 0<=v<=1 for v in self.baseColor+self.environmentColor):
            raise ValueError("Material baseColor must be three finite values in 0..1")
        return self


class CodeRange(Spec):
    startLine: int = Field(ge=1,le=10000000)
    endLine: int = Field(ge=1,le=10000000)

    @model_validator(mode='after')
    def order(self):
        if self.endLine < self.startLine:
            raise ValueError('Code range end must follow its start')
        return self


class CodeMapping(Spec):
    displayLine: int = Field(ge=1,le=120)
    sourceLine: int = Field(ge=1,le=10000000)
    symbol: str = Field(default='',pattern=r'^[A-Za-z_][A-Za-z0-9_]*$|^$')
    sourceSymbol: str = Field(default='',pattern=r'^[A-Za-z_][A-Za-z0-9_]*$|^$')


class CodeNote(Spec):
    sourceLine: int = Field(ge=1, le=10000000)
    text: str = Field(min_length=1, max_length=240, pattern=r'^[^\r\n]*\S[^\r\n]*$')


class CodeBlock(Spec):
    title: str = Field(default='',max_length=160)
    workspacePath: str = Field(min_length=1,max_length=32768)
    sourceFile: str = Field(default='',max_length=2048)
    startLine: int = Field(default=1,ge=1,le=10000000)
    endLine: int = Field(default=1,ge=1,le=10000000)
    expectedSourceSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    exampleCode: str = Field(default='',max_length=16000)
    sourceRanges: list[CodeRange] = Field(default_factory=list,max_length=12)
    codeMappings: list[CodeMapping] = Field(default_factory=list,max_length=240)
    notes: list[CodeNote] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def source(self):
        if self.startLine>self.endLine or (not self.sourceRanges and self.endLine>=self.startLine+120):
            raise ValueError('Code blocks need an ordered source range of at most 120 lines')
        if self.exampleCode and self.sourceRanges:
            raise ValueError('Choose sourceRanges or exampleCode, not both')
        return self


class Node(Spec):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    text: str = Field(min_length=1,max_length=240)
    eventId: int = Field(default=0,ge=0,le=0xffffffff)
    image: ImageSource | None = None
    secondImage: ImageSource | None = None
    imageLabel: str = Field(default='',max_length=80)
    secondImageLabel: str = Field(default='',max_length=80)
    annotations: list[Mark] = Field(default_factory=list,max_length=16)
    secondAnnotations: list[Mark] = Field(default_factory=list,max_length=16)
    calculation: str = Field(default='',max_length=1200)
    description: str = Field(default='',max_length=400)
    details: str = Field(default='',max_length=4000)
    links: list[Link] = Field(default_factory=list,max_length=8)
    codeBlocks: list[CodeBlock] = Field(default_factory=list,max_length=4)


class MeshEffect(Spec):
    type: Literal['none','inverted-hull','sine-displacement'] = 'none'
    amount: float = Field(default=.04,ge=0,le=.25)
    frequency: float = Field(default=6.,ge=.1,le=20)
    phase: float = Field(default=0.,ge=-100,le=100)
    color: str = Field(default='#101018',pattern=r'^#[a-fA-F0-9]{6}$')
    enabled: bool = True


class Card(Spec):
    type: Literal["image","compare","mesh-uv","mesh","diagram","code"]
    title: str = Field(min_length=1,max_length=160)
    caption: str = Field(default="",max_length=1600)
    image: ImageSource | None = None
    secondImage: ImageSource | None = None
    detailImage: ImageSource | None = None
    secondDetailImage: ImageSource | None = None
    beforeLabel: str = Field(default="",max_length=120)
    afterLabel: str = Field(default="",max_length=120)
    annotations: list[Mark] = Field(default_factory=list,max_length=32)
    secondAnnotations: list[Mark] = Field(default_factory=list,max_length=32)
    eventId: int = Field(default=0,ge=0,le=0xffffffff)
    instance: int = Field(default=0,ge=0,le=0xffffffff)
    stage: Literal["vs-out","gs-out"] = "vs-out"
    meshFile: str = Field(default="",max_length=32768)
    primitive: Literal["sphere","cube","plane"] | None = None
    material: Material = Field(default_factory=Material)
    meshEffect: MeshEffect = Field(default_factory=MeshEffect)
    links: list[Link] = Field(default_factory=list,max_length=6)
    nodes: list[Node] = Field(default_factory=list,max_length=12)
    edges: list[list[str]] = Field(default_factory=list,max_length=24)
    edgeLabels: list[str] = Field(default_factory=list,max_length=24)
    edgeDetails: list[str] = Field(default_factory=list,max_length=24)
    workspacePath: str = Field(default='',max_length=32768)
    sourceFile: str = Field(default='',max_length=2048)
    startLine: int = Field(default=1,ge=1,le=10000000)
    endLine: int = Field(default=1,ge=1,le=10000000)
    expectedSourceSha256: str = Field(default='',max_length=64)
    exampleCode: str = Field(default='',max_length=16000)
    sourceRanges: list[CodeRange] = Field(default_factory=list,max_length=12)
    codeMappings: list[CodeMapping] = Field(default_factory=list,max_length=240)
    experimentReceipt: str = Field(default='',max_length=32768)
    comparisonScope: Literal['auto', 'appearance', 'stage'] = Field(default='auto',
        description='Appearance requires a verified on/off experiment at the same captured Present as the other appearance cards. Stage explicitly compares intermediate results.')

    @model_validator(mode="after")
    def shape(self):
        if self.type in {"image","compare","mesh-uv"} and self.image is None:
            raise ValueError("Image cards require an image source")
        if self.type in {"compare","mesh-uv"} and self.secondImage is None:
            raise ValueError("Comparison cards require both sources")
        if self.type=="mesh" and sum((bool(self.eventId),bool(self.meshFile),bool(self.primitive)))!=1:
            raise ValueError("Mesh needs one eventId, exported meshFile, or illustrative primitive")
        ids={n.id for n in self.nodes}
        if len(ids)!=len(self.nodes) or any(len(e)!=2 or any(x not in ids for x in e) for e in self.edges):
            raise ValueError("Diagram edges must connect unique declared node IDs")
        if self.type=="diagram" and not self.nodes:
            raise ValueError("Diagram requires nodes")
        if self.edgeLabels and (len(self.edgeLabels)!=len(self.edges) or any(len(v)>240 for v in self.edgeLabels)):
            raise ValueError('Edge labels must match edges and contain at most 240 characters')
        if self.edgeDetails and (len(self.edgeDetails)!=len(self.edges) or any(len(v)>2000 for v in self.edgeDetails)):
            raise ValueError('Edge details must match edges and contain at most 2000 characters')
        if self.type=='code' and (not self.workspacePath or not _HEX.fullmatch(self.expectedSourceSha256)
                                  or not self.startLine<=self.endLine
                                  or (not self.sourceRanges and self.endLine>=self.startLine+120)):
            raise ValueError('Code cards need an exact saved-source hash and a range of at most 120 lines')
        if sum(len(m.points) for m in self.annotations+self.secondAnnotations)>1024:
            raise ValueError("Visual annotation point budget exceeded")
        return self


def root_path() -> Path:
    local=os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    return (Path(local) if local else Path.home()/".local/share")/"ReplayLab/ai-visuals"


def _write(path: Path, data: bytes):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Visual storage must not be redirected")
    if path.is_file() and path.read_bytes()==data:return
    fd,name=tempfile.mkstemp(prefix=".visual-",dir=str(path.parent))
    try:
        with os.fdopen(fd,"wb") as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _json(obj):
    return json.dumps(obj,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(",",":")).encode("utf-8")


def save_navigation_target(target: dict, capture: dict) -> dict:
    """Mint a durable navigation identity from Host-observed reflection only."""
    identity=dict(capture)
    if identity.get('local') and identity.get('path'):
        st=Path(identity['path']).stat();identity.update(size=st.st_size,mtimeMs=st.st_mtime_ns//1_000_000)
    data=_json({'schema':'bread-ai-navigation/v1','capture':identity,'target':target})
    if len(data)>MAX_ASSET_BYTES:raise ValueError('Navigation data exceeds the 8 MiB inspection payload limit')
    digest=hashlib.sha256(data).hexdigest()
    _write(root_path()/'targets'/(digest+'.json'),data)
    kind = 'uv' if target.get('kind') == 'uv' else 'cbuffer'
    return {'id':digest,'url':f'rdoc://{kind}/{digest}','target':target}


def _asset(root: Path, data: bytes, suffix: str):
    if len(data)>MAX_ASSET_BYTES:raise ValueError("Visual asset exceeds 8 MiB")
    digest=hashlib.sha256(data).hexdigest()
    _write(root/"blobs"/(digest+suffix),data)
    return {"sha256":digest,"file":digest+suffix,"bytes":len(data)}


def _triangles(indices,topology):
    strip=[]
    for index in indices:
        if index is None:strip=[];continue
        strip.append(index)
        if topology=="TriangleList" and len(strip)==3:yield strip;strip=[]
        elif topology=="TriangleStrip" and len(strip)>=3:
            a,b,c=strip[-3:];yield [b,a,c] if len(strip)%2==0 else [a,b,c]
        elif topology=="TriangleFan" and len(strip)>=3:yield [strip[0],strip[-2],strip[-1]]


def canonical_mesh(mesh: dict) -> dict:
    """Use real output positions, with an explicitly approximate view reconstruction."""
    positions=mesh.get("positions",[])
    if not 1<=len(positions)<=16384:raise ValueError("No bounded captured geometry")
    clip=mesh.get("coordinateSpace")=="clip"
    viewport=mesh.get("viewport") or {}
    aspect=float(viewport.get("width") or 1)/max(1,float(viewport.get("height") or 1))
    vertices=[]
    for p in positions:
        if p is None:vertices.append([0.,0.,0.]);continue
        if len(p) not in (3,4) or any(not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>1e12 for v in p):
            raise ValueError("Invalid captured positions")
        # Clip W carries perspective depth. Projection scale/FOV is not known;
        # this is a geometry illustration, never a pixel-coverage/shading proof.
        vertices.append([float(p[0])*aspect,float(p[1])*(-1 if mesh.get("flipY") else 1),
                         float(p[3] if clip and len(p)==4 else p[2])])
    indices=mesh.get("indices",[])
    if len(indices)>49152:raise ValueError("Geometry index budget exceeded")
    if any(i is not None and (isinstance(i,bool) or not isinstance(i,int) or not 0<=i<len(vertices)) for i in indices):
        raise ValueError("Invalid geometry indices")
    triangles=list(_triangles(indices,mesh.get("topology")))
    if not triangles:raise ValueError("This interactive preview needs triangle geometry")
    return {"vertices":vertices,"triangles":triangles,"space":"approximate-view" if clip else "shader-output",
            "complete":bool(mesh.get("complete")),"sourceStage":mesh.get("stage"),
            "instance":mesh.get("instance",0),"limitations":mesh.get("limitations",[])}


def obj_mesh(data: bytes) -> dict:
    positions=[];texcoords=[];vertices=[];uvs=[];triangles=[];mapping={}
    for line in data.decode("utf-8",errors="strict").splitlines():
        words=line.split()
        if not words or words[0].startswith("#"):continue
        if words[0]=="v":
            p=[float(x)for x in words[1:4]]
            if len(p)!=3 or any(not math.isfinite(x) or abs(x)>1e12 for x in p):raise ValueError("Invalid OBJ position")
            positions.append(p)
        elif words[0]=="vt":texcoords.append([float(x)for x in words[1:3]])
        elif words[0]=="f":
            face=[]
            for token in words[1:]:
                fields=token.split("/");pi=int(fields[0]);pi=pi-1 if pi>0 else len(positions)+pi
                ti=int(fields[1]) if len(fields)>1 and fields[1] else 0
                ti=ti-1 if ti>0 else len(texcoords)+ti if ti<0 else -1
                if not 0<=pi<len(positions):raise ValueError("Invalid OBJ face index")
                key=(pi,ti)
                if key not in mapping:
                    mapping[key]=len(vertices);vertices.append(positions[pi])
                    uv=texcoords[ti] if 0<=ti<len(texcoords) else [0.,0.]
                    if len(uv)!=2 or any(not math.isfinite(x) or abs(x)>1e6 for x in uv):raise ValueError("Invalid OBJ UV")
                    uvs.append(uv)
                face.append(mapping[key])
            triangles.extend([[face[0],face[i],face[i+1]]for i in range(1,len(face)-1)])
        if len(positions)>65536 or len(texcoords)>65536 or len(vertices)>16384 or len(triangles)>16384:
            raise ValueError("Exported OBJ exceeds interactive mesh limits")
    if not vertices or not triangles:raise ValueError("No OBJ triangles")
    return {"vertices":vertices,"uvs":uvs,"triangles":triangles,"space":"object","complete":True}


def publish(title: str, cards: list[dict], context: dict, *, capture_image: Callable,
            capture_mesh: Callable, authorize_file: Callable, root: Path | None = None,
            read_code: Callable | None = None, effect_neighbours: Callable | None = None,
            effect_inputs: Callable | None = None,
            effect_exclusions: list[EffectDrawExclusion] | None = None) -> dict:
    if not isinstance(title,str) or not 1<=len(title)<=160:raise ValueError("Visual answer needs a short title")
    if not 1<=len(cards)<=6:raise ValueError("A visual answer needs 1..6 focused cards")
    specs=[Card.model_validate(c)for c in cards]
    root=root or root_path()
    capture=dict(context.get("capture") or {})
    if capture.get("loaded") and capture.get("local") and capture.get("path"):
        st=Path(capture['path']).stat();capture.update(size=st.st_size,mtimeMs=st.st_mtime_ns//1_000_000)
    def image(source: ImageSource, *, diagram=False):
        metadata={}
        if source.eventId:
            metadata = (capture_image(source, preserve_alpha=True)
                        if diagram and source.channel == "rgba" else capture_image(source))
            if not metadata.get("path"):raise ValueError("The captured image was not saved")
            path=Path(metadata['path']);provenance="captured"
        else:path=authorize_file(source.path);provenance="reference"
        if path.stat().st_size>MAX_ASSET_BYTES:raise ValueError("Source image exceeds 8 MiB")
        data=path.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in {"PNG","JPEG","WEBP"} or im.mode not in {'RGB','RGBA','P','L','LA'} or not 1<=im.width<=8192 or not 1<=im.height<=8192 or im.width*im.height>16_000_000:
                raise ValueError("Unsupported or oversized visual image")
            im.load()
            im = im.convert("RGBA")
            flip = source.flipY
            if flip is None and metadata.get('resourceId'):
                selected = context.get('texture') or {}
                flip = (str(selected.get('resourceId')) == str(metadata['resourceId'])
                        and selected.get('flipY') is True)
            if flip:
                im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            output = io.BytesIO()
            im.save(output,format="PNG")
            result=_asset(root,output.getvalue(),".png")
            result.update(width=im.width,height=im.height,provenance=provenance,
                          source=dict(source.model_dump(exclude={'path'}), flipY=bool(flip)),
                          presentationFlipApplied=bool(flip),
                          sourceSha256=hashlib.sha256(data).hexdigest())
            if metadata.get('resourceId'):
                result['resourceId'] = str(metadata['resourceId'])
            return result
    built=[]
    for spec in specs:
        card=spec.model_dump(exclude={'image','secondImage','detailImage','secondDetailImage','meshFile','material','workspacePath','sourceFile',
                                      'expectedSourceSha256','experimentReceipt','exampleCode'})
        if spec.image:card['image']=image(spec.image)
        if spec.secondImage:card['secondImage']=image(spec.secondImage)
        if spec.detailImage:card['detailImage']=image(spec.detailImage)
        if spec.secondDetailImage:card['secondDetailImage']=image(spec.secondDetailImage)
        if spec.type=='diagram':
            card['nodes']=[]
            for node in spec.nodes:
                saved=node.model_dump(exclude={'image','secondImage','codeBlocks'})
                if node.image:
                    saved['image']=image(node.image, diagram=True)
                    if not saved['eventId'] and node.image.eventId:saved['eventId']=node.image.eventId
                if node.secondImage:
                    saved['secondImage'] = image(node.secondImage, diagram=True)
                saved['codeBlocks']=[]
                for block in node.codeBlocks:
                    if read_code is None:raise ValueError('Saved Shader source reader unavailable')
                    code=read_code(block)
                    from .code_excerpts import validate_fragment
                    validate_fragment(code['text'])
                    if block.notes:
                        visible_lines = set(code.get('lineNumbers', ()))
                        noted_lines = {note.sourceLine for note in block.notes}
                        if not noted_lines <= visible_lines or len(noted_lines) != len(block.notes):
                            raise ValueError('Code notes need distinct saved-source lines visible in this block')
                        code = dict(code, notes=[note.model_dump() for note in block.notes])
                    saved['codeBlocks'].append(dict(code,title=block.title))
                card['nodes'].append(saved)
        if spec.type=='code':
            if read_code is None:raise ValueError('Saved Shader source reader unavailable')
            card['code']=read_code(spec)
            from .code_excerpts import validate_fragment
            validate_fragment(card['code']['text'])
        if spec.experimentReceipt:
            from .effect_experiments import verified_comparison
            card['experiment']=verified_comparison(authorize_file(spec.experimentReceipt),capture,
                                                    card.get('image',{}),card.get('secondImage',{}))
        if spec.type=='compare':
            if card['image']['sha256']==card['secondImage']['sha256']:
                raise ValueError('Comparison inputs have identical displayed pixels. Publish one image for no visible difference, or collect the actual changed output.')
            if (card['image']['width'],card['image']['height'])==(card['secondImage']['width'],card['secondImage']['height']):
                with Image.open(root/'blobs'/card['image']['file']) as a, Image.open(root/'blobs'/card['secondImage']['file']) as b:
                    delta=ImageChops.difference(a.convert('RGB'),b.convert('RGB'))
                    bounds=delta.getbbox()
                    def changed_pixels(im):
                        bands=im.split()
                        return im.width*im.height-ImageChops.lighter(ImageChops.lighter(bands[0],bands[1]),bands[2]).histogram()[0]
                    stats={'differentPixels':changed_pixels(delta),
                           'bounds':list(bounds)if bounds else None,'meanAbsoluteDifference':ImageStat.Stat(delta).mean}
                    if not stats['differentPixels']:
                        raise ValueError('Comparison inputs have identical displayed pixels. Publish one image for no visible difference, or collect the actual changed output.')
                    roi=card.get('experiment',{}).get('roi')
                    if roi:
                        box=(int(roi[0]*a.width),int(roi[1]*a.height),max(1,int(math.ceil(roi[2]*a.width))),max(1,int(math.ceil(roi[3]*a.height))))
                        regional=delta.crop(box)
                        stats['regionDifferentPixels']=changed_pixels(regional)
                        if card['experiment']['outcome']=='changed'and not stats['regionDifferentPixels']:
                            raise ValueError('The displayed experiment pair has no change inside its stated region. Recheck output, coordinates and display mapping before attributing the effect.')
                    card['comparisonStats']=stats
        if spec.type=='mesh':
            material=spec.material.model_dump(exclude={'texture'})
            if spec.material.texture:material['texture']=image(spec.material.texture)
            card['material']=material
            if spec.eventId:
                mesh=capture_mesh(spec)
                if mesh.get('eventId')!=spec.eventId or mesh.get('captureSessionId')!=capture.get('captureSessionId'):
                    raise ValueError("Mesh evidence does not match this capture/EID")
                geometry=canonical_mesh(mesh)
            elif spec.meshFile:
                path=authorize_file(spec.meshFile)
                if path.stat().st_size>MAX_ASSET_BYTES:raise ValueError("Mesh file exceeds 8 MiB")
                geometry=obj_mesh(path.read_bytes()) if path.suffix.lower()=='.obj' else canonical_mesh(json.loads(path.read_text(encoding='utf-8')))
            else:geometry=None
            if geometry:card['geometry']=_asset(root,_json(geometry),'.json');card['geometryInfo']={k:v for k,v in geometry.items()if k not in {'vertices','triangles','uvs'}}
        built.append(card)
    built.sort(key=lambda card: card['type'] != 'compare')
    appearance_cards = [card for card in built
                        if card['type'] == 'compare' and card['comparisonScope'] != 'stage']
    presentation_bases = [(card, (card.get('experiment') or {}).get('presentationBasis'),
                           (card.get('experiment') or {}).get('capturedPresent', False))
                          for card in appearance_cards]
    verified_basis = next((basis for _, basis, present in presentation_bases if present), None)
    final_basis = verified_basis or next((basis for _, basis, _ in presentation_bases if basis), None)
    for card, basis, present in presentation_bases:
        if ((final_basis is not None and basis != final_basis)
                or (card['comparisonScope'] == 'appearance' and not present)):
            target = f" EID {verified_basis['eventId']}" if verified_basis else ''
            raise ValueError(f"Comparison '{card['title']}' must use a verified pair at the same final "
                             f"presentation output{target}, with the same display mapping. Re-run "
                             "the layer's on/off experiment with observation_scope='appearance'. "
                             "Use comparisonScope='stage' only to explain intermediate results.")
    diagrams = [card for card in built if card['type'] == 'diagram']
    nodes = [node for diagram in diagrams for node in diagram['nodes']]
    effect_cards = [(card, card['experiment']) for card in appearance_cards if 'experiment' in card]
    effect_sources = {exp['sourceEventId'] for _, exp in effect_cards if exp.get('sourceEventId')}
    effect_outputs = {exp['observationEventId'] for _, exp in effect_cards}
    linked_draws = set()
    for card, _ in effect_cards:
        for link in card['links']:
            url = urlsplit(link['url'])
            if url.netloc in {'event', 'eid'} and re.fullmatch(r'/[1-9][0-9]{0,9}', url.path):
                linked_draws.add(int(url.path[1:]))
    layer_events = effect_sources | (linked_draws - effect_outputs)
    displayed_events = {node['eventId'] for node in nodes if node.get('image') or node.get('secondImage')}
    code_blocks = [card['code'] for card in built if 'code' in card]
    code_blocks.extend(block for node in nodes for block in node['codeBlocks'])
    covered = {Path(block['workspacePath']).resolve() for block in code_blocks if block.get('workspacePath')}
    missing = {}
    for card in built:
        experiment = card.get('experiment') or {}
        if experiment.get('verified') and experiment.get('workspacePath') and (diagrams or card['comparisonScope'] != 'stage'):
            path = Path(experiment['workspacePath']).resolve()
            if path not in covered:
                missing[path] = dict(workspacePath=str(path), eventId=experiment['sourceEventId'],
                                     stage=experiment['stage'])
    marked_effect = (dict(relatedEvents=sorted(layer_events),
                          missingLayerViews=sorted(layer_events - effect_sources - displayed_events),
                          needsDiagram=len(layer_events) > 1 and not any(d['edges'] for d in diagrams),
                          needsFinalReplay=[card['title'] for card, exp in effect_cards if not exp.get('capturedPresent')])
                     if effect_cards else None)
    if marked_effect is not None and effect_neighbours is not None:
        from .effect_experiments import verify_layer_exclusion
        candidates = {eid for anchor in effect_sources for eid in effect_neighbours(anchor)}
        reference = next((exp for _, exp in effect_cards if exp['capturedPresent']), None)
        exclusions = {}
        for row in effect_exclusions or []:
            if reference is None or row.eventId not in candidates - effect_sources:
                raise ValueError('An exclusion must identify an unconfirmed neighbouring draw and a final comparison.')
            digest = verify_layer_exclusion(authorize_file(row.experimentReceipt), capture, row.eventId, reference)
            exclusions[row.eventId] = dict(evidence=row.evidence, receiptSha256=digest)
        known_layers = (layer_events | candidates) - exclusions.keys()
        # An adjacent raw RT pair can show pipeline order. It cannot prove an isolated final effect.
        final_layers = {exp['sourceEventId'] for _, exp in effect_cards if exp.get('capturedPresent')}
        if len(known_layers) > 1:
            connected_events = set()
            input_images = set()
            outputs = {eid for eid in effect_outputs if eid}
            baseline_hashes = {card['image']['sourceSha256'] for card, exp in effect_cards
                               if exp.get('capturedPresent')}
            final_cards = [card for card, exp in effect_cards if exp.get('capturedPresent')]
            for diagram in diagrams:
                def images(node):
                    return [node[key] for key in ('image', 'secondImage') if key in node]
                # Walk back from the final composite, rather than counting unrelated edges.
                connected_ids = {node['id'] for node in diagram['nodes'] if any(
                    image['source']['eventId'] in outputs or image['sourceSha256'] in baseline_hashes
                    for image in images(node))}
                previous_count = -1
                while previous_count != len(connected_ids):
                    previous_count = len(connected_ids)
                    connected_ids.update(start for start, end in diagram['edges'] if end in connected_ids)
                for node in diagram['nodes']:
                    if node['id'] not in connected_ids or not images(node):
                        continue
                    connected_events.add(node['eventId'])
                    input_images.update((image['source']['eventId'], image.get('resourceId'))
                                        for image in images(node) if image['source']['attachmentKind'] == 'srv')
            missing_final = sorted(known_layers - final_layers)
            missing_nodes = sorted(known_layers - connected_events)
            missing_inputs = []
            if effect_inputs is not None:
                for eid in sorted(final_layers):
                    missing_inputs.extend(dict(eventId=eid, resourceId=rid) for rid in effect_inputs(eid)
                                          if (eid, rid) not in input_images)
            marked_effect.update(relatedEvents=sorted(known_layers),
                                 candidateExclusions=exclusions, missingFinalLayerComparisons=missing_final,
                                 missingLayerViews=missing_nodes, missingInputTextures=missing_inputs,
                                 combinedBaselineMismatch=not _combined_baseline_matches(final_cards, root),
                                 needsDiagram=not any(d['edges'] for d in diagrams))
    manifest={'schema':'bread-ai-visual/v1','title':title,'capture':capture,'cards':built,
              'replayModified':False,'materialIsIllustrative':True}
    data=_json(manifest)
    if len(data)>MAX_MANIFEST_BYTES:raise ValueError("Visual manifest is too large")
    digest=hashlib.sha256(data).hexdigest();_write(root/(digest+'.json'),data)
    return {'schema':'bread-ai-visual-receipt/v1','id':digest,'path':str(root/(digest+'.json')),
            'cardCount':len(built),'applied':False,'presentation':{'shaderCodeGaps':list(missing.values()),
                **({'markedEffect': marked_effect} if marked_effect else {}), 'cards':[
                dict(type=c['type'],title=c['title'],nodes=[dict(id=n['id'],title=n['text'],
                    imageCount=int(bool(n.get('image')))+int(bool(n.get('secondImage'))),
                    codeBlockCount=len(n.get('codeBlocks',[])),hasDetails=bool(n.get('details')))
                    for n in c.get('nodes',[])])for c in built]}}
