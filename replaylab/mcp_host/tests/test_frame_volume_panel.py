from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from test_frame_flow_panel import document, load_module


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/project_frame_volume.py"
spec = importlib.util.spec_from_file_location("frame_volume_panel", SOURCE)
volume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume)


def receipt(tmp_path):
    return {"depth": 64, "mip": 2, "selectedSlice": 31, "viewSliceFirst": 4, "viewSliceCount": 56,
        "slices": [{"slice": z, "channel": channel, "path": str(tmp_path / ("%s-%d.png" % (channel, z))),
                    "sha256": "a" * 64, "width": 160, "height": 120}
                   for channel in ("a", "rgb", "r", "g", "b") for z in (4, 24, 59)]}


def test_sampled_volume_preserves_actual_depth_indices_and_channel_order(tmp_path):
    ast.parse(SOURCE.read_text(encoding="utf-8"), feature_version=(3, 6))
    parent = {"eventId": 10, "resourceId": "7", "componentCount": 4, "displayMapping": {"mip": 2}}
    result = volume.normalize_volume_preview(receipt(tmp_path), parent)
    assert result["depth"] == 64 and result["mip"] == 2
    assert result["viewSliceFirst"] == 4 and result["viewSliceCount"] == 56
    assert [row["channel"] for row in result["slices"]] == [c for c in ("rgb", "r", "g", "b", "a") for _ in range(3)]
    rgb = volume.volume_channel_slices(result, "rgb")
    assert [row["slice"] for row in rgb] == [4, 24, 59]
    assert volume.nearest_slice_index(rgb, 31) == 1
    assert volume.nearest_slice_index(rgb, 63) == 2
    assert volume.nearest_slice_index(rgb, 0) == 0


@pytest.mark.parametrize("change", [
    {"depth": 0}, {"depth": True}, {"mip": 1}, {"viewSliceCount": 0}, {"viewSliceFirst": 63},
    {"selectedSlice": 63}, {"slices": []},
])
def test_volume_rejects_wrong_mip_view_or_unbounded_cache(tmp_path, change):
    payload = receipt(tmp_path)
    payload.update(change)
    with pytest.raises(ValueError):
        volume.normalize_volume_preview(payload, {"displayMapping": {"mip": 2}})


@pytest.mark.parametrize("change", [
    {"slice": True}, {"slice": 3}, {"slice": 60}, {"channel": "rgba"}, {"channel": "depth"},
    {"sha256": ""}, {"path": "relative.png"}, {"path": "https://example.com/slice.png"},
    {"width": 0}, {"eventId": 11}, {"resourceId": 8},
])
def test_volume_rejects_misbound_or_invalid_slice_receipts(tmp_path, change):
    payload = receipt(tmp_path)
    payload["slices"][0].update(change)
    with pytest.raises(ValueError):
        volume.normalize_volume_preview(payload, {"eventId": 10, "resourceId": "7", "displayMapping": {"mip": 2}})


def test_volume_rejects_duplicates_and_unavailable_channels(tmp_path):
    payload = receipt(tmp_path)
    payload["slices"].append(deepcopy(payload["slices"][0]))
    with pytest.raises(ValueError, match="repeats"):
        volume.normalize_volume_preview(payload, {"displayMapping": {"mip": 2}})
    with pytest.raises(ValueError, match="absent"):
        volume.normalize_volume_preview(receipt(tmp_path), {"componentCount": 3, "displayMapping": {"mip": 2}})


def test_frame_panel_preserves_volume_and_flip_receipt_without_remirroring(tmp_path):
    panel = load_module()
    payload = document(tmp_path)
    preview = payload["stages"][0]["images"][0]
    preview.update(resourceId="7", componentCount=4, displayMapping={"mip": 2, "slice": 31, "flipY": True},
                   volumePreview=receipt(tmp_path))
    result = panel.normalize_document(payload)["stages"][0]["images"][0]
    assert len(result["volumePreview"]["slices"]) == 15
    assert result["displayMapping"]["flipY"] is True
    assert panel.preview_navigation(result)["displayMapping"]["flipY"] is True
    assert panel.preview_card_identity(result) != panel.preview_card_identity(dict(result,
        displayMapping=dict(result["displayMapping"], flipY=False)))
