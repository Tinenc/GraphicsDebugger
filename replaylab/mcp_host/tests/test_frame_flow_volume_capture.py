import importlib
from pathlib import Path
import tempfile

import pytest

from test_frame_flow_channels import fixture


def volume_fixture(count=4):
    bridge, ctrl, channels, snapshot, parent = fixture()
    module = importlib.import_module(bridge.__name__ + ".frame_flow_volume")
    textures = ctrl.GetTextures()
    textures[0].dimension, textures[0].depth, textures[0].arraysize = 3, 12, 1
    textures[0].format.compCount = count
    ctrl.GetTextures = lambda: textures
    ctrl.color.textureType = "Texture3D"
    ctrl.color.firstSlice, ctrl.color.numSlices = 0, 0
    parent["displayMapping"]["slice"] = 0
    return bridge, ctrl, module, snapshot, parent


def requests(root, selections, mode="native", black=None, white=None):
    rows = []
    for index, (z, channel) in enumerate(selections):
        request_id = "%032x" % (index + 1)
        directory = Path(root) / ("eid-41-" + request_id)
        directory.mkdir()
        rows.append({"slice": z, "channel": channel, "request_id": request_id,
                     "artifact_dir": str(directory), "rangeMode": mode,
                     "blackPoint": black, "whitePoint": white})
    return rows


def test_volume_all_slices_share_one_select_restore_and_correct_w_offsets():
    bridge, ctrl, module, snapshot, parent = volume_fixture()
    with tempfile.TemporaryDirectory() as root:
        rows = requests(root, [(0, "rgb"), (0, "a"), (3, "rgb"), (3, "a"), (5, "r")])
        result = module.capture_volume(bridge, parent, rows, snapshot, lambda: False)
    assert result["complete"] and len(result["receipts"]) == 5
    assert ctrl.events == [(41, True), (7, True)]
    assert [save.slice.sliceIndex for save, path in ctrl.saved] == [0, 0, 3, 3, 5]
    assert result["mipDepth"] == 6 and result["viewSliceCount"] == 6
    assert all(item["receipt"]["restoration"]["restored"] for item in result["receipts"])


def test_volume_display_scale_stays_fixed_across_slices_and_flip_is_propagated():
    bridge, ctrl, module, snapshot, parent = volume_fixture()
    parent["displayMapping"]["flipY"] = True
    with tempfile.TemporaryDirectory() as root:
        rows = requests(root, [(0, "r"), (5, "r")], "explicit", -1.0, 2.0)
        result = module.capture_volume(bridge, parent, rows, snapshot, lambda: False)
    assert result["complete"]
    for item in result["receipts"]:
        display = item["receipt"]["artifact"]["display"]
        assert display["flipY"] and display["blackPoint"] == -1.0 and display["whitePoint"] == 2.0
    assert [(save.comp.blackPoint, save.comp.whitePoint) for save, path in ctrl.saved] == [(-1.0, 2.0)] * 2


def test_single_component_volume_caches_r_and_rejects_absent_channels():
    bridge, ctrl, module, snapshot, parent = volume_fixture(count=1)
    with tempfile.TemporaryDirectory() as root:
        result = module.capture_volume(bridge, parent, requests(root, [(0, "r"), (1, "r")]), snapshot, lambda: False)
    assert result["complete"] and result["componentCount"] == 1
    ctrl.events.clear()
    with tempfile.TemporaryDirectory() as root:
        with pytest.raises(ValueError, match="actual mip"):
            module.capture_volume(bridge, parent, requests(root, [(0, "a")]), snapshot, lambda: False)
    assert not ctrl.events


def test_partial_slice_failure_restores_original_event_and_retains_only_valid_receipts():
    bridge, ctrl, module, snapshot, parent = volume_fixture()
    original = ctrl.SaveTexture
    def failing(save, path):
        if save.slice.sliceIndex == 3:
            raise RuntimeError("texture read failed")
        return original(save, path)
    ctrl.SaveTexture = failing
    with tempfile.TemporaryDirectory() as root:
        result = module.capture_volume(bridge, parent, requests(root, [(0, "r"), (3, "r"), (5, "r")]), snapshot, lambda: False)
    assert not result["complete"]
    assert [item["slice"] for item in result["receipts"]] == [0, 5]
    assert result["errors"][0]["slice"] == 3
    assert ctrl.events == [(41, True), (7, True)]


def test_duplicate_requests_are_rejected_before_any_replay():
    bridge, ctrl, module, snapshot, parent = volume_fixture()
    with tempfile.TemporaryDirectory() as root:
        with pytest.raises(ValueError, match="duplicate"):
            module.capture_volume(bridge, parent, requests(root, [(0, "r"), (0, "r")]), snapshot, lambda: False)
    assert not ctrl.events
