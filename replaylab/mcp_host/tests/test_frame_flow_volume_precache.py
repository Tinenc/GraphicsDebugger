from copy import deepcopy

import pytest

from src.frame_flow_volume_precache import precache_volumes, slice_plan
from src.frame_flow_precache import precache


def document(count=4, depth=5):
    return {"captureSessionId": "session", "stages": [{"id": "volume", "images": [{
        "eventId": 9, "resourceId": 72, "attachmentKind": "uav", "attachmentIndex": 0,
        "attachmentStage": "compute", "textureDimension": 3, "mipDepth": depth,
        "viewSliceFirst": 0, "viewSliceCount": depth, "componentCount": count,
        "displayMapping": {"mip": 2, "slice": 1, "sample": 0, "blackPoint": 0.0, "whitePoint": 1.0},
    }]}]}


def parent(data):
    return data["stages"][0]["images"][0]


def reply(preview, requests):
    return {"captureSessionId": "session", "replayRevision": [0, 1], "originalEventId": 17,
            "componentCount": preview["componentCount"], "mipDepth": preview["mipDepth"],
            "viewSliceFirst": preview["viewSliceFirst"], "viewSliceCount": preview["viewSliceCount"],
            "receipts": [dict(request) for request in requests], "errors": [], "complete": True}


def accept(preview, item, request, response):
    assert preview["displayMapping"]["slice"] == item["slice"]
    assert request["slice"] == item["slice"]
    return {"path": "verified-%s-%s.png" % (item["slice"], item["channel"]), "sha256": "a" * 64,
            "eventId": preview["eventId"], "resourceId": preview["resourceId"],
            "displayMapping": deepcopy(preview["displayMapping"])}


def test_volume_publication_samples_real_mip_depth_and_keeps_channel_identity():
    data, calls = document(depth=103), []
    parent(data)["displayMapping"]["slice"] = 43
    def capture(preview, requests):
        calls.append(requests)
        return reply(preview, requests)
    result = precache_volumes(data, lambda *args: {}, capture, accept)
    volume = parent(data)["volumePreview"]
    assert result["complete"] and len(calls) == 1 and volume["complete"]
    z = {item["slice"] for item in volume["slices"]}
    assert len(z) <= 32 and {0, 43, 102} <= z
    assert {item["channel"] for item in volume["slices"]} == {"rgb", "r", "g", "b", "a"}
    assert all(item["rangeMode"] == "native" for item in calls[0])
    assert volume["sampled"] and volume["mip"] == 2 and volume["selectedSlice"] == 43


def test_single_component_volume_still_caches_every_z_without_fake_rgb_or_alpha():
    data = document(count=1)
    result = precache_volumes(data, lambda *args: {}, reply, accept)
    assert result["complete"]
    assert [(item["slice"], item["channel"]) for item in parent(data)["volumePreview"]["slices"]] == [(z, "r") for z in range(5)]


def test_single_channel_skip_does_not_skip_volume_publication():
    data = document(count=1)
    def no_channel_capture(*args):
        raise AssertionError("Single channel needs no split preview")
    result = precache(data, lambda *args: {}, no_channel_capture, accept, volume_capture=reply)
    assert result["complete"] and result["volumes"]["prepared"] == 1
    assert len(parent(data)["volumePreview"]["slices"]) == 5


def test_partial_view_and_shared_range_are_respected():
    data = document(depth=30)
    image = parent(data)
    image.update(viewSliceFirst=7, viewSliceCount=5)
    image["displayMapping"].update(slice=9, blackPoint=-2.0, whitePoint=0.7)
    requests = []
    def capture(preview, rows):
        requests.extend(rows)
        return reply(preview, rows)
    assert precache_volumes(data, lambda *args: {}, capture, accept)["complete"]
    assert {item["slice"] for item in requests} == set(range(7, 12))
    assert {(item["rangeMode"], item["blackPoint"], item["whitePoint"]) for item in requests} == {("explicit", -2.0, 0.7)}
    image["displayMapping"]["slice"] = 0
    with pytest.raises(ValueError, match="interval"):
        slice_plan(image)


def test_duplicate_or_foreign_slices_are_rejected_without_admission():
    for change in ("duplicate", "foreign", "revision", "session"):
        data = document()
        parent(data)["channelCacheRevision"] = [0, 1]
        def capture(preview, requests):
            result = reply(preview, requests)
            if change == "duplicate":
                result["receipts"][1] = result["receipts"][0]
            elif change == "foreign":
                result["receipts"][0]["slice"] = 999
            elif change == "revision":
                result["replayRevision"] = [4, 2]
            else:
                result["captureSessionId"] = "another"
            return result
        result = precache_volumes(data, lambda *args: {}, capture, accept)
        assert not result["complete"] and "volumePreview" not in parent(data)


def test_partial_failure_keeps_verified_slice_and_records_missing_without_fake_completion():
    data = document(count=1)
    def capture(preview, requests):
        result = reply(preview, requests[:1])
        result["errors"] = [{"slice": 1, "channel": "r", "error": "temporary read failure"}]
        return result
    result = precache_volumes(data, lambda *args: {}, capture, accept)
    assert not result["complete"] and result["failed"] == 1
    assert len(parent(data)["volumePreview"]["slices"]) == 1
    assert not parent(data)["volumePreview"]["complete"]


def test_repeated_parent_reuses_same_verified_slice_files_in_one_publication():
    data, calls = document(), []
    data["stages"][0]["images"].append(deepcopy(parent(data)))
    def capture(preview, requests):
        calls.append(requests)
        return reply(preview, requests)
    assert precache_volumes(data, lambda *args: {}, capture, accept)["prepared"] == 2
    assert len(calls) == 1
    assert data["stages"][0]["images"][0]["volumePreview"] == data["stages"][0]["images"][1]["volumePreview"]


def test_large_volume_limits_readback_work_but_retains_exact_selected_z():
    data, calls = document(depth=128), []
    parent(data)["textureByteSize"] = 128 * 1024 * 1024
    parent(data)["displayMapping"]["slice"] = 70
    def capture(preview, requests):
        calls.append(requests)
        return reply(preview, requests)
    assert precache_volumes(data, lambda *args: {}, capture, accept)["complete"]
    z = {item["slice"] for item in calls[0]}
    assert z == {0, 70, 127}
    assert len(calls[0]) * parent(data)["textureByteSize"] <= 2 * 1024 * 1024 * 1024


def test_fully_failed_volume_does_not_attach_empty_set_or_preserve_stale_cache():
    data = document(count=1)
    parent(data)["volumePreview"] = {"slices": [{"path": "stale.png"}]}
    def capture(preview, requests):
        result = reply(preview, [])
        result["errors"] = [{"error": "read failed"}]
        return result
    assert not precache_volumes(data, lambda *args: {}, capture, accept)["complete"]
    assert "volumePreview" not in parent(data)
