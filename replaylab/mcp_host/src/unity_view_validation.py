"""Diagnostic review of view evidence; never a workflow lock or task verdict."""
import json
from pathlib import Path


def _dynamic_findings(row,scenario,scene_ids):
    from .unity_temporal_validation import capture_record,_pose,_same_pose,_numbers
    from .unity_layered_validation import _sha
    from .unity_difference_diagnosis import _status
    findings=[]
    try:
        before=capture_record(row["beforeCapture"]);after=capture_record(row["afterCapture"])
        if (before.get("cameraId") not in scene_ids or before.get("cameraId")!=after.get("cameraId") or
            before.get("finalCameraOutput") is not True or after.get("finalCameraOutput") is not True or
            before.get("cameraType")!="SceneView" or after.get("cameraType")!="SceneView" or
            type(before.get("renderSequence")) is not int or type(after.get("renderSequence")) is not int or
            not 0<before["renderSequence"]<after["renderSequence"]):
            return ["actual-scene-render-sequences-unproven"]
        poses=(_pose(before),_pose(after))
        same=_same_pose(*poses)
        world_same=all(abs(a-b)<=1e-5 for a,b in zip(poses[0][0],poses[1][0]))
        if scenario in {"scene-camera-orbit","scene-camera-translation"} and world_same:
            findings.append("camera-motion-unobserved")
        if scenario=="scene-camera-translation":
            positions=[_numbers(value["cameraPosition"]) for value in (before,after)]
            if len(positions[0])!=3 or len(positions[1])!=3 or all(abs(a-b)<1e-5 for a,b in zip(*positions)):
                findings.append("camera-translation-unobserved")
        if scenario.startswith("scene-light-"):
            if not same:findings.append("light-test-camera-was-not-held")
            keys=("direction",) if scenario=="scene-light-direction" else ("color","intensity")
            left=row["lightingBefore"];right=row["lightingAfter"]
            if not any(_numbers(left[key])!=_numbers(right[key]) for key in keys):
                findings.append("light-control-change-unobserved")
        if scenario=="motion-settle-return":
            middle=capture_record(row["middleCapture"])
            middle_path=Path(middle.get("path", ""))
            if (not same or middle.get("cameraId")!=before["cameraId"] or
                middle.get("finalCameraOutput") is not True or
                not before["renderSequence"]<middle.get("renderSequence",0)<after["renderSequence"] or
                _same_pose(poses[0],_pose(middle)) or not middle_path.is_absolute() or not middle_path.is_file()):
                findings.append("camera-motion-and-return-unproven")
        comparison=row.get("outputComparison") or {}
        state=_status(comparison)
        for capture,digest in ((before,comparison.get("referenceSha256")),(after,comparison.get("renderedSha256"))):
            path=Path(capture.get("path", ""))
            if not path.is_absolute() or not path.is_file() or path.stat().st_size>256*1024*1024 or _sha(path)!=digest:
                findings.append("dynamic-output-comparison-provenance-missing");break
        if state=="unknown" or comparison.get("metrics",{}).get("nonFiniteComponents")!=0:
            findings.append("dynamic-render-output-invalid-or-unobserved")
        expected=row.get("expectedOutputChange")
        if type(expected) is not bool or not row.get("expectationReason"):
            findings.append("dynamic-response-expectation-unrecorded")
        elif (state=="different")!=expected:
            findings.append("dynamic-render-response-outside-expectation")
        samples=row.get("motionSamples",[]);pairs=row.get("motionComparisons",[])
        if not isinstance(samples,list) or not 3<=len(samples)<=16 or not isinstance(pairs,list) or len(pairs)!=len(samples)-1:
            findings.append("dynamic-motion-sequence-missing")
        else:
            moving=[capture_record(sample) for sample in samples]
            seq=[sample.get("renderSequence") for sample in moving]
            if (any(type(value) is not int or value<=0 for value in seq) or seq!=sorted(set(seq)) or
                any(sample.get("cameraId")!=before["cameraId"] or sample.get("finalCameraOutput") is not True for sample in moving) or
                moving[0].get("path")!=before.get("path") or moving[-1].get("path")!=after.get("path") or
                len({sample.get("path") for sample in moving})!=len(moving)):
                findings.append("dynamic-motion-sequence-provenance-missing")
            for index,pair in enumerate(pairs):
                if _status(pair)=="unknown" or pair.get("metrics",{}).get("nonFiniteComponents")!=0:
                    findings.append("invalid-output-during-motion");break
                for sample,digest in ((moving[index],pair.get("referenceSha256")),(moving[index+1],pair.get("renderedSha256"))):
                    path=Path(sample.get("path",""))
                    if not path.is_absolute() or not path.is_file() or path.stat().st_size>256*1024*1024 or _sha(path)!=digest:
                        findings.append("motion-comparison-provenance-missing");break
    except (ValueError,KeyError,TypeError,OSError,AttributeError):
        findings.append("dynamic-observation-incomplete")
    return findings


def review_views(observations, captures, comparisons, experiments, required_effects=(), stability=None):
    if not isinstance(observations, dict):
        raise ValueError("Supply the actual camera observation object")
    cameras = {row["cameraId"]: row for row in observations.get("cameras", []) if isinstance(row, dict) and isinstance(row.get("cameraId"), int)}
    findings = []
    def finding(code, **detail):
        findings.append(dict(code=code, **detail))
    for kind in ("Game", "SceneView"):
        if not any(row.get("cameraType") == kind for row in cameras.values()):
            finding("view-not-observed", cameraType=kind)
    chains = {}
    for cid, camera in cameras.items():
        chains[cid] = {row.get("type", row.get("name")) for row in camera.get("features", []) if row.get("active") is True}
        if camera.get("cameraType") == "SceneView":
            if camera.get("projectionMatchesViewport") is False:
                finding("camera-projection-viewport-aspect-difference",cameraId=cid,
                        projectionAspect=camera.get("projectionAspect"),viewportAspect=camera.get("viewportAspect"))
            for name in ("sceneLighting", "imageEffects"):
                if camera.get(name) is not True:
                    finding("scene-switch-disabled-or-unknown", cameraId=cid, setting=name)
            if "drawMode" in camera and camera["drawMode"] != "Textured":
                finding("scene-diagnostic-draw-mode",cameraId=cid,drawMode=camera["drawMode"])
        executed = {row.get("name") for row in observations.get("executedFeatures", [])
                    if row.get("cameraId") == cid and row.get("enabled") is True
                    and row.get("renderSequence") == camera.get("renderSequence") and row.get("renderSequence", 0) > 0}
        for effect in required_effects:
            if effect not in executed:
                finding("effect-execution-not-observed", cameraId=cid, effect=effect)
        if not any(row.get("cameraId") == cid and row.get("kind") in {"screen","history"}
                   for row in observations.get("resources", [])):
            finding("view-resource-ownership-not-observed", cameraId=cid)
    games = [cid for cid in cameras if cameras[cid].get("cameraType") == "Game"]
    scenes = [cid for cid in cameras if cameras[cid].get("cameraType") == "SceneView"]
    for game in games:
        for scene in scenes:
            if chains[game] != chains[scene]:
                finding("feature-definition-difference", gameCameraId=game, sceneCameraId=scene,
                        onlyGame=sorted(chains[game]-chains[scene]), onlyScene=sorted(chains[scene]-chains[game]))
    allocations = {}
    for row in observations.get("resources", []):
        if not isinstance(row, dict):
            continue
        if row.get("viewIndependent") is True:
            if row.get("kind") == "history":
                finding("history-declared-view-independent",cameraId=row.get("cameraId"),role=row.get("role"))
            else:
                continue
        key = row.get("textureId")
        if key:
            allocations.setdefault(key, []).append(row)
        camera = cameras.get(row.get("cameraId"), {})
        if row.get("renderSequence") != camera.get("renderSequence"):
            finding("resource-from-another-render", cameraId=row.get("cameraId"), role=row.get("role"))
        if row.get("kind") in {"screen","history"}:
            width,height=row.get("viewportWidth",row.get("width")),row.get("viewportHeight",row.get("height"))
            cw,ch=camera.get("width"),camera.get("height")
            if all(type(value) is int and value>0 for value in (width,height,cw,ch)):
                # Allow integer rounding of half/quarter-resolution viewports;
                # padded allocations must report their actual active viewport.
                if abs(width/height-cw/ch)>2/max(1,height):
                    finding("screen-resource-aspect-difference",cameraId=row.get("cameraId"),role=row.get("role"),
                        viewport=[width,height],cameraExtent=[cw,ch],diagnostic="Inspect per-view descriptor, viewport, texel size and UV scale/bias")
            else:
                finding("screen-resource-extent-unobserved",cameraId=row.get("cameraId"),role=row.get("role"))
    for resource, rows in allocations.items():
        owners = sorted({(row.get("cameraId"),row.get("viewIndex",0)) for row in rows if isinstance(row.get("cameraId"), int)})
        if len(owners) > 1:
            finding("view-dependent-resource-shared", textureId=resource, cameraIds=sorted({row[0] for row in owners}),
                    cameraViews=[list(row) for row in owners],roles=[row.get("role") for row in rows])
    final = {}
    for capture in captures:
        if not isinstance(capture, dict):
            raise ValueError("Capture records must be objects")
        record = dict(capture)
        sidecar = capture.get("sidecarPath")
        if sidecar:
            path = Path(sidecar)
            if not path.is_absolute() or path.stat().st_size > 1024*1024:
                raise ValueError("Use a bounded absolute view-capture sidecar")
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            if any(key in record and key in saved and record[key] != saved[key]
                   for key in ("cameraId","cameraType","renderSequence","path","finalCameraOutput")):
                finding("capture-annotation-contradicts-sidecar",sidecarPath=str(path))
                continue
            record = {**record, **saved}
        cid = record.get("cameraId")
        image = Path(record.get("path") or "")
        if cid not in cameras or record.get("finalCameraOutput") is not True or not image.is_absolute() or not image.is_file():
            finding("final-camera-capture-unproven", cameraId=cid, path=str(image))
            continue
        if record.get("cameraType") != cameras[cid].get("cameraType") or record.get("renderSequence", 0) <= 0:
            finding("capture-camera-provenance-missing", cameraId=cid)
            continue
        final[cid] = str(image)
    for cid in cameras:
        if cid not in final:
            finding("final-view-output-missing", cameraId=cid)
        matched = any(row.get("cameraId") == cid and row.get("category") == "full-frame"
            and row.get("referenceOrigin") == "rdc-original" and row.get("alignmentRecorded") is True
            and (row.get("rendered") or {}).get("path") == final.get(cid) for row in comparisons)
        if not matched:
            finding("aligned-original-final-comparison-missing", cameraId=cid)
    scenarios = ("scene-motion-only", "game-motion-only", "different-extents", "history-reset", "unfocused-scene",
                 "resize-wide-tall-return", "multi-angle-holds")
    for scenario in scenarios:
        records = [row for row in experiments if row.get("scenario") == scenario and row.get("performed") is True]
        if not any(row.get("evidencePaths") and row.get("observations") for row in records):
            finding("view-isolation-experiment-missing", scenario=scenario)
    from .unity_resize_validation import inspect_resize
    resize_reviews=[]
    for kind, ids, scenario, code in (
            ("SceneView", scenes, "resize-wide-tall-return", "scene-resize-rendering-unproven"),
            ("Game", games, "game-resize-wide-tall-return", "game-free-aspect-rendering-unproven")):
        for cid in ids:
            reviews=[inspect_resize(row,{cid},camera_type=kind) for row in experiments
                     if row.get("scenario")==scenario and row.get("performed") is True]
            resize_reviews.extend(dict(row,cameraId=cid,cameraType=kind) for row in reviews)
            if not reviews or not any(not row["findings"] for row in reviews):
                finding(code,cameraId=cid,diagnostics=reviews)
    # Moving outputs should change predictably, not pass a stationary pixel test.
    # Inspect real endpoint sidecars and numerical reports; never move the project
    # cameras/lights here or mistake input-control annotations for causal proof.
    scene_ids={cid for cid,row in cameras.items() if row.get("cameraType")=="SceneView"}
    for scenario in ("scene-camera-orbit","scene-camera-translation","scene-light-direction",
                     "scene-light-color-intensity","motion-settle-return"):
        records=[row for row in experiments if row.get("scenario")==scenario and row.get("performed") is True]
        details=[_dynamic_findings(row,scenario,scene_ids) for row in records]
        if not records or all(details):
            finding("dynamic-scene-response-unproven",scenario=scenario,diagnostics=details)
    valid_stability=isinstance(stability,dict) and stability.get("schema")=="bread-unity-temporal-review/v1"
    for cid in cameras:
        groups=[row for row in stability.get("groups",[]) if row.get("cameraId")==cid] if valid_stability else []
        if not any(row.get("status")=="below-threshold" and row.get("lastCapture")==final.get(cid) for row in groups):
            finding("final-view-temporal-stability-unproven",cameraId=cid)
        if len({row.get("poseSignature") for row in groups if row.get("status")=="below-threshold" and row.get("poseSignature")})<3:
            finding("multi-angle-stability-unproven",cameraId=cid)
    if valid_stability:
        for row in stability.get("findings",[]):
            finding("temporal-observation",observation=row)
    return {"schema":"bread-unity-view-review/v1", "diagnosticsOnly":True, "acceptanceBy":"agent",
            "findings":findings, "resizeReviews":resize_reviews, "observedFinalViews":final, "localChecksDoNotEstablishCompletion":True,
            "scope":"Supplied observations and evidence annotations, not Shader equivalence or automatic whole-task acceptance",
            "nextAction":"Repair diagnosed renderer/resource/history differences, gather both final outputs and original-reference comparisons. Stabilize animation/jitter/history when testing an unchanged view. Continue independent work when evidence is missing; do not turn this report into an import lock."}
