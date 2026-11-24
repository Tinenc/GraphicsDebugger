import copy
import json
import struct
from pathlib import Path

import pytest

from src.unity_temporal_validation import review_stability

IDENTITY=[1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]


def samples(tmp_path, values=None, camera=1, pose=0):
    rows=[]
    for index,value in enumerate(values or [1.0]*8):
        path=tmp_path/f"{camera}-{pose}-{index}.rgba32f"
        path.write_bytes(struct.pack("<4f",value,0,0,1))
        matrix=list(IDENTITY);matrix[12]=pose
        row=dict(path=str(path),width=1,height=1,channels=4,cameraId=camera,
            cameraType="Game" if camera==1 else "SceneView",finalCameraOutput=True,
            renderSequence=index+1+pose*8,unityFrame=1,worldToCamera=matrix,nonJitteredProjection=IDENTITY,
            controlledInputs=True,inputStateKey="fixed-light-animation-time",poseId=str(pose),sourceFormat="RGBA32F")
        Path(str(path)+".json").write_text(json.dumps(row))
        rows.append(row)
    return rows


def codes(result):return {row["code"] for row in result["findings"]}


def test_actual_editor_renders_in_same_unity_frame_can_be_stable(tmp_path):
    result=review_stability(samples(tmp_path))
    assert result["findings"]==[]
    assert result["groups"][0]["status"]=="below-threshold"
    assert result["diagnosticsOnly"] and "complete" not in result


def test_alternating_frames_identifies_a_pattern_without_asserting_its_cause(tmp_path):
    result=review_stability(samples(tmp_path,[0,1]*4))
    assert {"temporal-variation-observed","alternating-frame-pattern"}<=codes(result)
    assert result["groups"][0]["adjacent"][0]["maximumAbsoluteError"]==1


def test_local_flicker_reports_worst_tiles_instead_of_only_global_mean(tmp_path):
    rows=samples(tmp_path)
    for index,row in enumerate(rows):
        row.update(width=64,height=1)
        data=[0.0]*(64*4);data[(40*4)]=float(index%2)
        Path(row["path"]).write_bytes(struct.pack("<256f",*data))
        Path(row["path"]+".json").write_text(json.dumps(row))
    result=review_stability(rows)
    assert "temporal-variation-observed" in codes(result)
    assert result["groups"][0]["adjacent"][0]["worstTiles"][0]["x"]==32


def test_nan_output_cannot_be_accepted_as_stable(tmp_path):
    result=review_stability(samples(tmp_path,[float("nan")]*8))
    assert "non-finite-output" in codes(result)


def test_camera_motion_is_not_labelled_as_stationary_flicker(tmp_path):
    rows=samples(tmp_path,[0,1]*4)
    rows[-1]["worldToCamera"]=list(IDENTITY);rows[-1]["worldToCamera"][12]=3
    result=review_stability(rows)
    assert "camera-pose-or-projection-changed" in codes(result)
    assert "temporal-variation-observed" not in codes(result)


def test_unknown_input_controls_and_invalid_matrices_do_not_prove_stability(tmp_path):
    rows=samples(tmp_path)
    for row in rows:row.update(inputStateKey={},nonJitteredProjection=[0]*16)
    result=review_stability(rows)
    assert {"stationary-input-controls-unproven","camera-matrices-unproven"}<=codes(result)


def test_duplicate_render_or_file_is_not_a_second_frame(tmp_path):
    rows=samples(tmp_path);rows[-1]=copy.deepcopy(rows[-2])
    result=review_stability(rows)
    assert {"sample-render-order-unproven","reused-frame-file"}<=codes(result)


def test_sidecar_cannot_be_relabelled_to_another_camera(tmp_path):
    rows=samples(tmp_path)
    rows[0]={"sidecarPath":rows[0]["path"]+".json","cameraId":-2}
    with pytest.raises(ValueError,match="contradicts sidecar"):
        review_stability(rows)


def test_resize_must_have_a_new_steady_group(tmp_path):
    rows=samples(tmp_path)
    last=rows[-1];last.update(width=2)
    Path(last["path"]).write_bytes(struct.pack("<8f",1,0,0,1,1,0,0,1))
    Path(last["path"]+".json").write_text(json.dumps(last))
    result=review_stability(rows)
    assert "extent-or-format-changed-in-steady-group" in codes(result)
    assert result["groups"][0]["adjacent"]==[]


def test_uninitialized_or_reallocated_history_is_observable(tmp_path):
    rows=samples(tmp_path)
    for index,row in enumerate(rows):
        row["resources"]=[dict(kind="history",role="TAA",generation=index+1,
            historyStateObserved=True,historyValid=index>0,historyResetReason="projection")]
    result=review_stability(rows)
    assert {"history-not-ready-after-warmup","history-reallocated-during-steady-hold"}<=codes(result)


def test_decoded_work_budget_is_bounded_before_pixel_comparison(tmp_path):
    rows=samples(tmp_path)
    for row in rows:row.update(width=100000,height=100000)
    with pytest.raises(ValueError,match="Decoded sequence exceeds"):
        review_stability(rows)


def test_warmup_samples_do_not_count_toward_steady_acceptance(tmp_path):
    rows=samples(tmp_path);rows[0]["phase"]="warmup"
    assert "too-few-steady-samples" in codes(review_stability(rows))
