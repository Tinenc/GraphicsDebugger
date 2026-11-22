"""Inspect real Game/Scene resize captures. Findings never lock import or repair."""
import math
import mmap
from pathlib import Path
import struct

from PIL import Image
from .file_content_cache import file_stamp
from .unity_layered_validation import _sha
from .unity_temporal_validation import capture_record, _numbers


def _matrix(record):
    value = record.get("nonJitteredProjection", record.get("projection"))
    if isinstance(value, dict):
        return [[float(value["m%d%d" % (r,c)]) for c in range(4)] for r in range(4)]
    if isinstance(value, list) and len(value) == 16:
        # Unity's Matrix4x4 indexer and camera observations use column-major.
        return [[float(value[c*4+r]) for c in range(4)] for r in range(4)]
    raise ValueError("Actual non-jittered projection is missing")


class _Pixels:
    def __init__(self, record):
        self.path = Path(record["path"])
        if not self.path.is_absolute() or not self.path.is_file() or self.path.stat().st_size > 256*1024*1024:
            raise ValueError("Use an existing bounded capture")
        self.stamp = file_stamp(self.path)
        self.stream = self.memory = self.image = None
        self.width, self.height = record["width"], record["height"]
        if any(type(n) is not int or n < 2 for n in (self.width, self.height)):
            raise ValueError("Resize observations need actual image dimensions")
        if self.path.suffix.lower() in {".rgba32f", ".rgb32f", ".r32f", ".rgba16f"}:
            self.channels = {".rgba32f":4,".rgb32f":3,".r32f":1,".rgba16f":4}[self.path.suffix.lower()]
            stride = 2 if self.path.suffix.lower() == ".rgba16f" else 4
            self.unpack = struct.Struct("<" + ("e" if stride == 2 else "f")*self.channels)
            if self.path.stat().st_size != self.width*self.height*self.unpack.size:
                raise ValueError("Capture extent does not match actual pixel bytes")
            self.stream = self.path.open("rb")
            self.memory = mmap.mmap(self.stream.fileno(), 0, access=mmap.ACCESS_READ)
        else:
            self.image = Image.open(self.path)
            if self.image.size != (self.width,self.height):
                self.image.close(); raise ValueError("Image extent contradicts its sidecar")
            self.image = self.image.convert("RGB"); self.channels = 3
        origin = record.get("rowOrigin")
        if origin == "native-gpu-readback" and type(record.get("graphicsUVStartsAtTop")) is bool:
            origin = "top-left" if record["graphicsUVStartsAtTop"] else "bottom-left"
        if origin not in {"top-left","bottom-left"}:
            self.close(); raise ValueError("Record the image row origin")
        self.top = origin == "top-left"
        self.sha = _sha(self.path)

    def pixel(self, x, y):
        if self.memory is not None:
            return self.unpack.unpack_from(self.memory,(y*self.width+x)*self.unpack.size)
        return tuple(n/255 for n in self.image.getpixel((x,y)))

    def close(self):
        if self.memory is not None: self.memory.close()
        if self.stream is not None: self.stream.close()
        if self.image is not None: self.image.close()


def _correspondence(before, after, tolerance):
    """Sample projection-predicted overlap; never resize evidence to its window."""
    readers = []
    try:
        a,b = _Pixels(before),None; readers.append(a)
        b = _Pixels(after); readers.append(b)
        if a.channels != b.channels: raise ValueError("Capture channel layouts differ")
        pa,pb = _matrix(before),_matrix(after)
        perspective = abs(pa[3][2]) > .1 and abs(pb[3][2]) > .1
        if any(abs(p[r][c]) > 1e-6 for p in (pa,pb) for r,c in ((0,1),(1,0),(3,0),(3,1))):
            raise ValueError("Skewed projection needs an explicit correspondence adapter")
        if perspective:
            ka = [pa[i][i]/pa[3][2] for i in (0,1)]
            kb = [pb[i][i]/pb[3][2] for i in (0,1)]
            oa = [pa[i][2]/pa[3][2] for i in (0,1)]
            ob = [pb[i][2]/pb[3][2] for i in (0,1)]
        elif abs(pa[3][3]-1)<1e-6 and abs(pb[3][3]-1)<1e-6:
            ka,kb = [pa[i][i] for i in (0,1)],[pb[i][i] for i in (0,1)]
            oa,ob = [pa[i][3] for i in (0,1)],[pb[i][3] for i in (0,1)]
        else: raise ValueError("Projection type changed during resize")
        if not all(math.isfinite(x) and abs(x)>1e-8 for x in ka+kb):
            raise ValueError("Invalid projection scale")
        differences=[]; signal=[]; invalid=0; sampled=0
        for gy in range(min(64,b.height)):
            y=min(b.height-1,int((gy+.5)*b.height/min(64,b.height)))
            for gx in range(min(64,b.width)):
                x=min(b.width-1,int((gx+.5)*b.width/min(64,b.width)))
                nx=2*(x+.5)/b.width-1; ny=(1-2*(y+.5)/b.height) if b.top else (2*(y+.5)/b.height-1)
                ax=(ka[0]/kb[0]*(nx-ob[0])+oa[0]+1)*a.width/2-.5
                ay_ndc=ka[1]/kb[1]*(ny-ob[1])+oa[1]
                ay=((1-ay_ndc) if a.top else (ay_ndc+1))*a.height/2-.5
                if not (0<=ax<a.width-1 and 0<=ay<a.height-1): continue
                sampled+=1; ix,iy=int(ax),int(ay); fx,fy=ax-ix,ay-iy
                taps=[a.pixel(ix,iy),a.pixel(ix+1,iy),a.pixel(ix,iy+1),a.pixel(ix+1,iy+1)]
                target=b.pixel(x,y)
                for channel in range(min(3,a.channels)):
                    expected=(taps[0][channel]*(1-fx)+taps[1][channel]*fx)*(1-fy)+(taps[2][channel]*(1-fx)+taps[3][channel]*fx)*fy
                    actual=target[channel]
                    if not math.isfinite(expected) or not math.isfinite(actual): invalid+=1; continue
                    differences.append(abs(expected-actual)); signal.append(expected)
        if sampled<16 or not signal: raise ValueError("Too little common image coverage")
        mean=sum(signal)/len(signal); deviation=math.sqrt(sum((x-mean)**2 for x in signal)/len(signal))
        mae=sum(differences)/len(differences)
        # Flat black/constant frames cannot establish geometry correctness.
        state="nonfinite" if invalid else "unproven-flat-image" if deviation<1e-5 else "below-threshold" if mae<=tolerance else "different"
        for image in readers:
            if file_stamp(image.path)!=image.stamp: raise ValueError("Capture changed during comparison")
        return {"status":state,"meanAbsoluteError":mae,"signalDeviation":deviation,"nonFiniteComponents":invalid,
                "absoluteTolerance":tolerance,
                "sampledPixels":sampled,"referenceSha256":a.sha,"renderedSha256":b.sha,
                "reference":str(a.path),"rendered":str(b.path),"method":"held-camera projection correspondence; sampled overlap"}
    finally:
        for image in readers: image.close()


def inspect_resize(experiment, camera_ids, camera_type="SceneView"):
    findings=[]; comparisons=[]
    try:
        captures=experiment["resizeCaptures"]
        rows=[capture_record(captures[name]) for name in ("original","wide","tall","return")]
        cid=rows[0]["cameraId"]
        if camera_type not in {"SceneView", "Game"}:
            raise ValueError("Use Game or SceneView resize observations")
        if cid not in camera_ids or any(row.get("cameraId")!=cid or row.get("cameraType")!=camera_type or row.get("finalCameraOutput") is not True for row in rows):
            raise ValueError("Use one actual %s camera's separate final outputs" % camera_type)
        if camera_type == "Game" and experiment.get("freeAspectRatio") is not True:
            findings.append("game-free-aspect-mode-unobserved")
        sequences=[row["renderSequence"] for row in rows]
        if any(type(n) is not int or n<=0 for n in sequences) or sequences!=sorted(set(sequences)):
            raise ValueError("Actual ordered render sequences are required")
        if len({row["path"] for row in rows})!=4: raise ValueError("Retain four distinct captures")
        if experiment.get("controlledInputs") is not True or not experiment.get("inputStateKey"):
            findings.append("resize-input-controls-unobserved")
        pose=_numbers(rows[0]["worldToCamera"])
        if any(len(_numbers(row["worldToCamera"]))!=len(pose) or any(abs(a-b)>1e-5 for a,b in zip(pose,_numbers(row["worldToCamera"]))) for row in rows):
            findings.append("resize-camera-pose-changed")
        extents=[]
        for row in rows:
            width,height=row["cameraPixelWidth"],row["cameraPixelHeight"]
            if any(type(n) is not int or n<2 for n in (width,height)): raise ValueError("Actual camera pixel extents are missing")
            extents.append((width,height))
            allowed_extents={(width,height)}
            scaled=(row.get("scaledPixelWidth"),row.get("scaledPixelHeight"))
            if all(type(n) is int and n>=2 for n in scaled):
                allowed_extents.add(scaled)
            if (row["width"],row["height"]) not in allowed_extents:
                findings.append("resize-final-output-extent-mismatch")
            matrix=_matrix(row)
            if not all(math.isfinite(x) for line in matrix for x in line) or abs(matrix[0][0])<1e-8:
                raise ValueError("Projection is invalid")
            if abs(abs(matrix[1][1]/matrix[0][0])-width/height)>2/height:
                findings.append("resize-projection-aspect-mismatch")
        if extents[1][0]/extents[1][1]<1.25 or extents[2][0]/extents[2][1]>.8:
            findings.append("resize-wide-and-tall-aspects-unobserved")
        if extents[0]!=extents[3]: findings.append("resize-original-extent-not-restored")
        tolerance=float(experiment.get("absoluteTolerance",.02))
        if not math.isfinite(tolerance) or not 0<=tolerance<=1: raise ValueError("Supply a finite image tolerance")
        for row in rows[1:]:
            comparison=_correspondence(rows[0],row,tolerance); comparisons.append(comparison)
            if comparison["status"]!="below-threshold": findings.append("resize-image-correspondence-"+comparison["status"])
    except (KeyError,ValueError,TypeError,OSError,ArithmeticError) as error:
        findings.append("resize-evidence-incomplete: "+str(error)[:240])
    return {"findings":sorted(set(findings)),"comparisons":comparisons,"diagnosticsOnly":True}
