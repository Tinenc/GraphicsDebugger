import copy
import json
from pathlib import Path
import struct

from src.unity_view_validation import review_views


def observations(shared=False):
    return {"cameras":[{"cameraId":cid,"cameraType":kind,"width":1,"height":1,"renderSequence":4,"sceneLighting":True,"imageEffects":True,
        "features":[{"type":"SourceLighting","active":True},{"type":"Temporal","active":True}]} for cid,kind in [(1,"Game"),(-2,"SceneView")]],
        "resources":[{"cameraId":cid,"textureId":8 if shared else cid*10,"width":1,"height":1,"role":"mask","kind":"screen","renderSequence":4} for cid in [1,-2]],
        "executedFeatures":[{"cameraId":cid,"name":"Temporal","enabled":True,"renderSequence":4} for cid in [1,-2]]}


def codes(result):return {row["code"] for row in result["findings"]}


def test_local_zero_error_does_not_establish_either_final_view():
    result=review_views(observations(),[],[{"name":"CoC","mae":0}],[],["Temporal"])
    assert "final-view-output-missing" in codes(result)
    assert "aligned-original-final-comparison-missing" in codes(result)
    assert result["localChecksDoNotEstablishCompletion"] and result["diagnosticsOnly"]
    assert "complete" not in result


def test_shared_screen_history_and_missing_execution_are_reported_without_a_lock():
    value=observations(shared=True);value["executedFeatures"]=[]
    value["resources"][0].update(kind="history",viewIndependent=True)
    value["cameras"][1]["features"]=[];value["cameras"][1]["imageEffects"]=False
    result=review_views(value,[],[],[],["Temporal"])
    assert {"history-declared-view-independent","view-dependent-resource-shared","effect-execution-not-observed",
            "feature-definition-difference","scene-switch-disabled-or-unknown"}<=codes(result)
    assert result["acceptanceBy"]=="agent" and "blocked" not in result


def test_world_atlas_can_be_shared_but_stale_view_resource_is_not_current():
    value=observations()
    value["resources"] += [{"cameraId":cid,"textureId":99,"kind":"world","viewIndependent":True} for cid in [1,-2]]
    value["resources"][0]["renderSequence"]=3
    result=review_views(value,[],[],[])
    assert "view-dependent-resource-shared" not in codes(result)
    assert "resource-from-another-render" in codes(result)


def final_evidence(tmp_path):
    from test_unity_dynamic_response import experiments as dynamic_experiments
    captures=[];comparisons=[]
    for cid,kind in [(1,"Game"),(-2,"SceneView")]:
        image=tmp_path/(str(cid)+".rgba32f");image.write_bytes(struct.pack("<4f",1,0,0,1))
        sidecar=Path(str(image)+".json");sidecar.write_text(json.dumps({"path":str(image),"cameraId":cid,
            "cameraType":kind,"renderSequence":4,"finalCameraOutput":True}))
        captures.append({"sidecarPath":str(sidecar)})
        comparisons.append({"cameraId":cid,"category":"full-frame","referenceOrigin":"rdc-original","alignmentRecorded":True,"rendered":{"path":str(image)}})
    experiments=[{"scenario":name,"performed":True,"evidencePaths":[str(tmp_path/"observed.json")],"observations":"Separate camera ownership; stable fixed-view temporal baseline"}
        for name in ["scene-motion-only","game-motion-only","different-extents","history-reset","unfocused-scene","resize-wide-tall-return","multi-angle-holds"]]
    experiments.extend(dynamic_experiments(tmp_path))
    from test_unity_resize_validation import resize_experiment
    experiments=[row for row in experiments if row["scenario"]!="resize-wide-tall-return"]
    experiments.append(resize_experiment(tmp_path))
    experiments.append(resize_experiment(tmp_path,camera_id=1,camera_type="Game"))
    return captures,comparisons,experiments


def test_complete_view_observations_still_do_not_certify_whole_task(tmp_path):
    from src.unity_temporal_validation import review_stability
    from test_unity_temporal_validation import samples
    captures,comparisons,experiments=final_evidence(tmp_path)
    rows=[row for cid in [1,-2] for pose in range(3) for row in samples(tmp_path,camera=cid,pose=pose)]
    for capture in captures:
        cid=json.loads(Path(capture["sidecarPath"]).read_text())["cameraId"]
        capture["sidecarPath"]=next(row["path"]+".json" for row in rows if row["cameraId"]==cid and row["renderSequence"]==24)
    for comparison,capture in zip(comparisons,captures):
        comparison["rendered"]["path"]=json.loads(Path(capture["sidecarPath"]).read_text())["path"]
    result=review_views(observations(),captures,comparisons,experiments,["Temporal"],review_stability(rows))
    assert result["findings"]==[] and len(result["observedFinalViews"])==2
    assert result["acceptanceBy"]=="agent" and result["localChecksDoNotEstablishCompletion"]


def test_scene_window_shape_difference_and_resize_checks_are_reported():
    value=observations();value["cameras"][1].update(width=1920,height=600)
    result=review_views(value,[],[],[])
    assert "screen-resource-aspect-difference" in codes(result)
    assert "final-view-temporal-stability-unproven" in codes(result)


def test_scene_resize_does_not_establish_game_free_aspect(tmp_path):
    captures,comparisons,experiments=final_evidence(tmp_path)
    experiments=[row for row in experiments if row["scenario"]!="game-resize-wide-tall-return"]
    result=review_views(observations(),captures,comparisons,experiments)
    assert "game-free-aspect-rendering-unproven" in codes(result)
    assert "scene-resize-rendering-unproven" not in codes(result)


def test_old_unity_regression_reference_is_not_original_capture_proof(tmp_path):
    captures,comparisons,experiments=final_evidence(tmp_path)
    comparisons[0]["referenceOrigin"]="retained-unity-kernel"
    result=review_views(observations(),captures,comparisons,experiments)
    assert any(row["code"]=="aligned-original-final-comparison-missing" and row["cameraId"]==1 for row in result["findings"])


def test_capture_cannot_be_relabelled_as_another_camera(tmp_path):
    captures,comparisons,experiments=final_evidence(tmp_path)
    captures[0]["cameraId"]=-2
    result=review_views(observations(),captures,comparisons,experiments)
    assert "capture-annotation-contradicts-sidecar" in codes(result)


def test_explicit_raw_row_alignment_does_not_require_lossy_conversion(tmp_path):
    from src.unity_layered_validation import compare_layers
    left=tmp_path/"reference.r32f";right=tmp_path/"camera.r32f"
    left.write_bytes(struct.pack("<4f",1,2,3,4));right.write_bytes(struct.pack("<4f",3,4,1,2))
    for path in [left,right]:Path(str(path)+".json").write_text(json.dumps({"width":2,"height":2,"channels":1}))
    layer={"name":"final","reference":{"path":str(left)},"rendered":{"path":str(right),"flipY":True}}
    result=compare_layers([layer])
    assert result["layers"][0]["metrics"]["maximumAbsoluteError"]==0
    assert result["automaticImageAdjustment"] is False
