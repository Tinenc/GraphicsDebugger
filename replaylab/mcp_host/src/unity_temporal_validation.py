"""Bounded, file-only temporal observations. Never accepts a restoration task."""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from PIL import Image

from .file_content_cache import file_stamp
from .unity_layered_validation import _values, _sha


def capture_record(annotation):
    if not isinstance(annotation, dict):
        raise ValueError("A sample must be a capture object")
    record = dict(annotation)
    sidecar = record.get("sidecarPath")
    if sidecar:
        path = Path(sidecar)
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 1024*1024:
            raise ValueError("Use an existing bounded absolute JSON sidecar")
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(saved, dict):
            raise ValueError("A capture sidecar must be an object")
        for key in ("cameraId", "cameraType", "renderSequence", "path", "width", "height", "finalCameraOutput"):
            if key in record and key in saved and record[key] != saved[key]:
                raise ValueError("Capture annotation contradicts sidecar: " + key)
        record.update(saved)
    return record


def _numbers(value):
    if isinstance(value, dict):
        return tuple(n for key in sorted(value) for n in _numbers(value[key]))
    if isinstance(value, (list, tuple)):
        return tuple(n for child in value for n in _numbers(child))
    if type(value) in (float, int) and math.isfinite(value):
        return (float(value),)
    raise ValueError("Camera matrices must contain finite numbers")


def _pose(row):
    # Non-jittered projection allows expected TAA sample offsets while retaining
    # actual camera motion, FOV and aspect differences.
    matrices = _numbers(row["worldToCamera"]), _numbers(row["nonJitteredProjection"])
    if any(len(value) != 16 or not any(value) for value in matrices):
        raise ValueError("Supply actual nonzero 4x4 camera matrices")
    return matrices


def _decoded_components(row):
    path=Path(row["path"])
    metadata={}
    for sidecar in (Path(str(path)+".json"),path.with_suffix(".json")):
        if sidecar.is_file() and sidecar.stat().st_size < 1024*1024:
            metadata=json.loads(sidecar.read_text(encoding="utf-8-sig"));break
    merged={**metadata,**row} if isinstance(metadata,dict) else row
    if path.suffix.lower() in {".rgba32f",".rgb32f",".r32f",".rgba16f",".bin",".raw",".f32"} or merged.get("encoding") in {"float32le","float16le","rgba32f","rgba16f","float32","float16"}:
        channels=merged.get("channels",{".rgba32f":4,".rgb32f":3,".r32f":1,".rgba16f":4}.get(path.suffix.lower()))
        shape=(merged.get("width"),merged.get("height"),channels)
        if any(type(value) is not int or value<=0 for value in shape) or channels>4:
            raise ValueError("Raw samples require actual positive dimensions/channels")
        return math.prod(shape)
    with Image.open(path) as image:
        return image.width*image.height*3


def _same_pose(a, b):
    return all(len(x) == len(y) and len(x) == 16 and all(abs(i-j) <= 1e-5 for i,j in zip(x,y))
               for x,y in zip(a,b))


def _difference(left, right, absolute, relative):
    paths = [Path(left["path"]), Path(right["path"])]
    before = [file_stamp(path) for path in paths]
    shape, a, a_sha = _values(left)
    other, b, b_sha = _values(right)
    if shape != other:
        return {"comparable": False, "reason": "different-pixel-layout", "shapes": [shape, other]}
    total = nonfinite = changed = changed_pixels = 0
    absolute_sum = maximum = 0.0
    pixel_changed = False
    tiles = defaultdict(lambda: [0.0, 0])
    for index, (x,y) in enumerate(zip(a,b)):
        total += 1
        if not math.isfinite(x) or not math.isfinite(y):
            nonfinite += 1; pixel_changed = True
        else:
            error = abs(x-y); absolute_sum += error; maximum = max(maximum, error)
            over = error > absolute + relative*abs(x)
            changed += over; pixel_changed |= over
            pixel = index//shape[2]; py,px = divmod(pixel,shape[0])
            tile = tiles[(px//32,py//32)]; tile[0] += error; tile[1] += 1
        if index % shape[2] == shape[2]-1:
            changed_pixels += pixel_changed; pixel_changed = False
    if total != math.prod(shape):
        raise ValueError("Readback changed or decoder returned incomplete pixels")
    for path, stamp, digest in zip(paths,before,(a_sha,b_sha)):
        if (stamp is not None and file_stamp(path) != stamp) or (stamp is None and _sha(path) != digest):
            raise ValueError("Capture bytes changed during temporal comparison")
    return {"comparable": True, "meanAbsoluteError": absolute_sum/max(1,total-nonfinite),
            "maximumAbsoluteError": maximum, "nonFiniteComponents": nonfinite,
            "changedComponents": changed, "changedPixelFraction": changed_pixels/(shape[0]*shape[1]),
            "worstTiles": [{"x":key[0]*32,"y":key[1]*32,"meanAbsoluteError":value[0]/value[1]}
                for key,value in sorted(tiles.items(),key=lambda row:row[1][0]/max(1,row[1][1]),reverse=True)[:4]],
            "referenceSha256":a_sha,"renderedSha256":b_sha}


def review_stability(samples, absolute_tolerance=0.001, relative_tolerance=0.001,
                     changed_pixel_fraction=0.001, minimum_samples=8):
    if not isinstance(samples,list) or not 1 <= len(samples) <= 64:
        raise ValueError("Supply 1..64 actual final-view sample sidecars")
    if type(minimum_samples) is not int or not 3 <= minimum_samples <= 16:
        raise ValueError("minimum_samples must be 3..16")
    for value in (absolute_tolerance, relative_tolerance, changed_pixel_fraction):
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError("Use finite nonnegative comparison tolerances")
    if changed_pixel_fraction > 1:
        raise ValueError("changed_pixel_fraction must be at most one")
    rows = [capture_record(row) for row in samples]
    files = {Path(row.get("path") or "") for row in rows}
    if any(not path.is_absolute() or not path.is_file() for path in files):
        raise ValueError("Every sample must reference actual absolute image bytes")
    if sum(path.stat().st_size for path in files) > 512*1024*1024:
        raise ValueError("Sequence exceeds 512 MiB; capture fewer/smaller views or an explicit ROI")
    unique_rows={str(row["path"]):row for row in rows}
    if sum(_decoded_components(row) for row in unique_rows.values())>128*1024*1024:
        raise ValueError("Decoded sequence exceeds 128 Mi components; review each held view separately and combine the reports")
    groups, findings, results = defaultdict(list), [], []
    for row in rows:
        if type(row.get("cameraId")) is not int or row.get("finalCameraOutput") is not True:
            findings.append({"code":"sample-final-view-provenance-missing","path":row["path"]}); continue
        if row.get("phase") == "warmup":
            continue
        groups[(row["cameraId"],str(row.get("scenario","stationary")),str(row.get("poseId","fixed")))].append(row)
    for (camera,scenario,pose_id), frames in groups.items():
        local = []
        def note(code, **detail):
            value=dict(code=code,cameraId=camera,scenario=scenario,poseId=pose_id,**detail)
            local.append(value); findings.append(value)
        sequences=[row.get("renderSequence") for row in frames]
        if (any(type(seq) is not int or seq <= 0 for seq in sequences) or
                len(set(sequences)) != len(sequences) or sequences != sorted(sequences)):
            note("sample-render-order-unproven")
        if len({str(row["path"]) for row in frames}) != len(frames): note("reused-frame-file")
        if len(frames) < minimum_samples: note("too-few-steady-samples",count=len(frames),required=minimum_samples)
        controlled=all(row.get("controlledInputs") is True and isinstance(row.get("inputStateKey"),str)
                       and 0<len(row["inputStateKey"])<=512 for row in frames)
        if not controlled or len({str(row.get("inputStateKey")) for row in frames}) != 1:
            note("stationary-input-controls-unproven")
        stable_pose=False; pose_signature=None
        try:
            poses=[_pose(row) for row in frames]
            stable_pose=all(_same_pose(poses[0],pose) for pose in poses)
            if not stable_pose: note("camera-pose-or-projection-changed")
            pose_signature=hashlib.sha256(json.dumps(poses[0]).encode()).hexdigest()
        except (KeyError, ValueError, TypeError): note("camera-matrices-unproven")
        extents={(row.get("width"),row.get("height"),row.get("sourceFormat")) for row in frames}
        if len(extents) != 1: note("extent-or-format-changed-in-steady-group")
        generations=defaultdict(set)
        for row in frames:
            for resource in row.get("resources",[]):
                generation=resource.get("generation")
                if resource.get("kind")=="history" and type(generation) is int and generation>0:
                    generations[(str(resource.get("role")),resource.get("viewIndex",0))].add(generation)
                if resource.get("kind")=="history" and resource.get("historyStateObserved") is True and resource.get("historyValid") is not True:
                    note("history-not-ready-after-warmup",role=resource.get("role"),renderSequence=row.get("renderSequence"),reason=resource.get("historyResetReason"));break
        if len(extents)==1 and stable_pose:
            for (role,view),values in generations.items():
                if len(values)>1: note("history-reallocated-during-steady-hold",role=role,viewIndex=view,generations=sorted(values))
        comparable = stable_pose and len(extents)==1
        adjacent=[];second=[]
        if comparable:
            adjacent=[_difference(a,b,absolute_tolerance,relative_tolerance) for a,b in zip(frames,frames[1:])]
            second=[_difference(a,b,absolute_tolerance,relative_tolerance) for a,b in zip(frames,frames[2:])]
            if any(not row["comparable"] for row in adjacent): note("different-decoded-layout")
            if any(row.get("nonFiniteComponents",0) for row in adjacent): note("non-finite-output")
            if any(row.get("changedPixelFraction",0)>changed_pixel_fraction for row in adjacent):
                note("temporal-variation-observed",controlledInputs=controlled)
            if len(adjacent)>=2 and all(row.get("changedPixelFraction",0)>changed_pixel_fraction for row in adjacent) and second and all(
                    row.get("comparable") and row.get("changedPixelFraction",1)<=changed_pixel_fraction and row.get("nonFiniteComponents",0)==0 for row in second):
                note("alternating-frame-pattern",diagnostic="A/B/A pattern; inspect history ping-pong, duplicate execution and cross-view globals")
        results.append({"cameraId":camera,"cameraType":frames[0].get("cameraType"),"scenario":scenario,"poseId":pose_id,
            "sampleCount":len(frames),"controlledInputs":controlled,"poseSignature":pose_signature,
            "lastCapture":frames[-1]["path"],"evidencePaths":[row["path"] for row in frames],
            "status":"below-threshold" if not local and adjacent else "variation-or-missing-evidence",
            "adjacent":adjacent,"twoRendersApart":second})
    return {"schema":"bread-unity-temporal-review/v1","diagnosticsOnly":True,"acceptanceBy":"agent",
        "groups":results,"findings":findings,"tolerance":{"absolute":absolute_tolerance,"relative":relative_tolerance,
        "changedPixelFraction":changed_pixel_fraction},"automaticImageAdjustment":False,
        "scope":"Pixel variation under supplied input-control annotations; no automatic cause attribution, fidelity acceptance or import lock"}
