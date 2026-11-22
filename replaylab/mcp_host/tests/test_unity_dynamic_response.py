import copy
import json
from pathlib import Path
import struct

from src.unity_layered_validation import compare_layers
from src.unity_view_validation import _dynamic_findings

IDENTITY=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]


def capture(tmp_path,name,sequence,pose,value):
    path=tmp_path/(name+".rgba32f");path.write_bytes(struct.pack("<4f",value,0,0,1))
    matrix=list(IDENTITY);matrix[12]=pose
    data=dict(path=str(path),width=1,height=1,channels=4,cameraId=-2,cameraType="SceneView",renderSequence=sequence,
              worldToCamera=matrix,nonJitteredProjection=IDENTITY,cameraPosition=[pose,0,0],finalCameraOutput=True)
    Path(str(path)+".json").write_text(json.dumps(data))
    return dict(sidecarPath=str(path)+".json")


def experiments(tmp_path):
    rows=[]
    scenarios=["scene-camera-orbit","scene-camera-translation","scene-light-direction","scene-light-color-intensity","motion-settle-return"]
    for index,scenario in enumerate(scenarios):
        start=index*3+1
        before=capture(tmp_path,scenario+"-before",start,0,1)
        after=capture(tmp_path,scenario+"-after",start+2,1 if scenario.startswith("scene-camera-") else 0,
                      1 if scenario=="motion-settle-return" else 2)
        a=json.loads(Path(before["sidecarPath"]).read_text());b=json.loads(Path(after["sidecarPath"]).read_text())
        comparison=compare_layers([dict(name=scenario,category="full-frame",reference={"path":a["path"]},rendered={"path":b["path"]})])["layers"][0]
        row=dict(scenario=scenario,performed=True,beforeCapture=before,afterCapture=after,outputComparison=comparison,
                 expectedOutputChange=scenario!="motion-settle-return",expectationReason="Known geometry/direct-light response; stationary return matches",
                 evidencePaths=[before["sidecarPath"],after["sidecarPath"]],observations="Observed dynamic response and stable return")
        if scenario.startswith("scene-light-"):
            row.update(lightingBefore=dict(direction=[1,0,0],color=[1,1,1],intensity=1),
                       lightingAfter=dict(direction=[0,1,0],color=[1,0,0],intensity=2))
        if scenario=="motion-settle-return":
            row["middleCapture"]=capture(tmp_path,scenario+"-middle",start+1,2,3)
        middle=row.get("middleCapture") or capture(tmp_path,scenario+"-middle",start+1,.5 if scenario.startswith("scene-camera-") else 0,1.5)
        row["motionSamples"]=[before,middle,after];row["motionComparisons"]=[]
        for left,right in zip(row["motionSamples"],row["motionSamples"][1:]):
            left_data=json.loads(Path(left["sidecarPath"]).read_text());right_data=json.loads(Path(right["sidecarPath"]).read_text())
            row["motionComparisons"].append(compare_layers([dict(name="motion",reference={"path":left_data["path"]},rendered={"path":right_data["path"]})])["layers"][0])
        rows.append(row)
    return rows


def test_orbit_translation_light_controls_and_settle_return_need_actual_scene_outputs(tmp_path):
    for row in experiments(tmp_path):
        assert _dynamic_findings(row,row["scenario"],{-2})==[]


def test_static_camera_is_not_evidence_of_camera_motion(tmp_path):
    row=experiments(tmp_path)[0]
    after=Path(row["afterCapture"]["sidecarPath"]);data=json.loads(after.read_text())
    data["worldToCamera"]=list(IDENTITY);after.write_text(json.dumps(data))
    assert "camera-motion-unobserved" in _dynamic_findings(row,row["scenario"],{-2})


def test_expected_static_baked_contribution_does_not_need_false_dynamic_relighting(tmp_path):
    row=experiments(tmp_path)[2]
    row.update(expectedOutputChange=False,expectationReason="This isolated baked world-volume contribution is static")
    before=json.loads(Path(row["beforeCapture"]["sidecarPath"]).read_text())
    after=json.loads(Path(row["afterCapture"]["sidecarPath"]).read_text())
    Path(after["path"]).write_bytes(Path(before["path"]).read_bytes())
    row["outputComparison"]=compare_layers([dict(name="baked",reference={"path":before["path"]},rendered={"path":after["path"]})])["layers"][0]
    for pair in row["motionComparisons"]:
        source,target=pair["evidencePaths"]
        pair.update(compare_layers([dict(name="motion",reference={"path":source},rendered={"path":target})])["layers"][0])
    assert _dynamic_findings(row,row["scenario"],{-2})==[]


def test_relabelled_camera_changed_bytes_and_nonfinite_motion_are_not_valid(tmp_path):
    row=experiments(tmp_path)[0]
    original=copy.deepcopy(row)
    row["afterCapture"]["cameraId"]=1
    assert "dynamic-observation-incomplete" in _dynamic_findings(row,row["scenario"],{-2})
    row=original
    row["outputComparison"]["metrics"]["nonFiniteComponents"]=1
    assert "dynamic-render-output-invalid-or-unobserved" in _dynamic_findings(row,row["scenario"],{-2})
    after=json.loads(Path(row["afterCapture"]["sidecarPath"]).read_text())
    Path(after["path"]).write_bytes(b"changed")
    assert "dynamic-output-comparison-provenance-missing" in _dynamic_findings(row,row["scenario"],{-2})


def test_two_endpoints_do_not_establish_stable_motion_and_middle_nan_is_reported(tmp_path):
    row=experiments(tmp_path)[0];original=copy.deepcopy(row)
    row.pop("motionSamples")
    assert "dynamic-motion-sequence-missing" in _dynamic_findings(row,row["scenario"],{-2})
    original["motionComparisons"][0]["metrics"]["nonFiniteComponents"]=1
    assert "invalid-output-during-motion" in _dynamic_findings(original,original["scenario"],{-2})
