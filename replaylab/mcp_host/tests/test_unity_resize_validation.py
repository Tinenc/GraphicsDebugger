from array import array
import json
import math
from pathlib import Path
import pytest

from src.unity_resize_validation import inspect_resize

IDENTITY=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]


def resize_experiment(tmp_path, stretch=False, flat=False, camera_id=-2, camera_type="SceneView", scale=1):
    captures={}
    for sequence,(name,width,height) in enumerate((("original",64,48),("wide",96,48),("tall",48,96),("return",64,48)),1):
        aspect=width/height
        projection=list(IDENTITY);projection[0]=2/aspect;projection[5]=2;projection[10]=-1;projection[11]=-1;projection[14]=-.2;projection[15]=0
        data=array("f");pixel_width,pixel_height=int(width*scale),int(height*scale)
        for y in range(pixel_height):
            for x in range(pixel_width):
                vx=(2*(x+.5)/pixel_width-1)*(4/3 if stretch else aspect)/2
                vy=(1-2*(y+.5)/pixel_height)/2
                data.extend([.3,.3,.3,1] if flat else [.5+.15*math.sin(3*vx),.5+.15*math.cos(4*vy),.5+.1*math.sin(5*(vx+vy)),1])
        path=tmp_path/("resize-"+str(camera_id)+"-"+name+".rgba32f");path.write_bytes(data.tobytes())
        record=dict(path=str(path),width=pixel_width,height=pixel_height,cameraPixelWidth=width,cameraPixelHeight=height,
            scaledPixelWidth=pixel_width,scaledPixelHeight=pixel_height,
            cameraId=camera_id,cameraType=camera_type,finalCameraOutput=True,renderSequence=sequence,
            worldToCamera=IDENTITY,nonJitteredProjection=projection,rowOrigin="top-left")
        sidecar=Path(str(path)+".json");sidecar.write_text(json.dumps(record))
        captures[name]={"sidecarPath":str(sidecar)}
    return dict(scenario="game-resize-wide-tall-return" if camera_type=="Game" else "resize-wide-tall-return",
        freeAspectRatio=camera_type=="Game",performed=True,controlledInputs=True,inputStateKey="held-camera",
        resizeCaptures=captures,evidencePaths=[r["sidecarPath"] for r in captures.values()],observations="Actual wide, tall and restored view outputs")


def test_actual_camera_resizes_with_consistent_geometry_are_compared_without_stretching_images(tmp_path):
    result=inspect_resize(resize_experiment(tmp_path),{-2})
    assert result["findings"]==[]
    assert len(result["comparisons"])==3
    assert all(r["status"]=="below-threshold" for r in result["comparisons"])
    assert result["comparisons"][2]["meanAbsoluteError"]<1e-7


def test_correct_projection_with_stretched_final_presentation_is_detected(tmp_path):
    result=inspect_resize(resize_experiment(tmp_path,stretch=True),{-2})
    assert "resize-image-correspondence-different" in result["findings"]
    assert "resize-projection-aspect-mismatch" not in result["findings"]


def test_frozen_capture_projection_does_not_match_a_tall_scene_window(tmp_path):
    row=resize_experiment(tmp_path)
    path=Path(row["resizeCaptures"]["tall"]["sidecarPath"])
    record=json.loads(path.read_text());record["nonJitteredProjection"][0]=1.5;path.write_text(json.dumps(record))
    assert "resize-projection-aspect-mismatch" in inspect_resize(row,{-2})["findings"]


def test_text_only_or_same_aspect_resize_cannot_establish_wide_tall_rendering(tmp_path):
    assert inspect_resize(dict(performed=True,evidencePaths=["anything"],observations="works"),{-2})["findings"]
    row=resize_experiment(tmp_path)
    row["resizeCaptures"]["tall"]=row["resizeCaptures"]["wide"]
    assert inspect_resize(row,{-2})["findings"]


def test_flat_frames_cannot_prove_no_stretch(tmp_path):
    assert "resize-image-correspondence-unproven-flat-image" in inspect_resize(resize_experiment(tmp_path,flat=True),{-2})["findings"]


def test_resize_does_not_accept_another_camera_or_forged_image_dimensions(tmp_path):
    row=resize_experiment(tmp_path)
    path=Path(row["resizeCaptures"]["wide"]["sidecarPath"])
    record=json.loads(path.read_text());record["cameraId"]=1;path.write_text(json.dumps(record))
    assert inspect_resize(row,{-2})["findings"]
    record["cameraId"]=-2;record["width"]=100;path.write_text(json.dumps(record))
    result=inspect_resize(row,{-2})
    assert "resize-final-output-extent-mismatch" in result["findings"]


@pytest.mark.parametrize("scale", [1,.5])
def test_game_free_aspect_uses_its_own_actual_final_outputs_and_render_scale(tmp_path,scale):
    row=resize_experiment(tmp_path,camera_id=1,camera_type="Game",scale=scale)
    result=inspect_resize(row,{1},camera_type="Game")
    assert result["findings"]==[]
    assert len(result["comparisons"])==3
    assert inspect_resize(row,{-2})["findings"]  # Game is not Scene proof.


def test_stretched_game_final_output_is_detected_even_with_correct_projection(tmp_path):
    row=resize_experiment(tmp_path,stretch=True,camera_id=1,camera_type="Game")
    assert "resize-image-correspondence-different" in inspect_resize(row,{1},camera_type="Game")["findings"]


def test_game_resize_requires_observed_free_aspect_mode(tmp_path):
    row=resize_experiment(tmp_path,camera_id=1,camera_type="Game")
    row.pop("freeAspectRatio")
    assert "game-free-aspect-mode-unobserved" in inspect_resize(row,{1},camera_type="Game")["findings"]
