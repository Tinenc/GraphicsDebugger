from src.frame_flow_precache import precache


def test_native_fatal_stops_remaining_batches_and_keeps_saved_images():
    data = {"captureSessionId": "session", "stages": [{"id": "stage", "images": [
        {"eventId": n + 1, "resourceId": n + 20, "attachmentKind": "rtv", "group": "outputs"}
        for n in range(18)]}]}
    calls = []
    def failed(items):
        calls.append(items)
        raise RuntimeError("bread-replay-unavailable: GPU synchronization failed")
    result = precache(data, lambda *args: {}, failed, lambda *args: None)
    assert len(calls) == 1 and result["attempted"] == 8 and result["deferred"] == 10
    assert not result["complete"] and len(data["stages"][0]["images"]) == 18


def document():
    return {"captureSessionId": "session", "stages": [{"id": "stage", "images": [
        {"eventId": 7, "resourceId": 42, "attachmentKind": "rtv", "group": "outputs", "title": "Color"}]}]}


def test_publication_caches_rgb_and_alpha_once_and_replaces_in_place_on_retry():
    data = document()
    calls = []
    def capture(items):
        calls.append(items)
        return {"captureSessionId": "session", "replayRevision": [1, 2], "items": [{
            "key": "stage:0", "componentCount": 4, "channelGroups": [{"channel": "rgb"}, {"channel": "a"}],
            "receipts": [{"channel": "rgb"}, {"channel": "a"}], "errors": []}]}
    def accept(parent, item, request, reply):
        return {"channel": "rgba" if item["channel"] == "rgb" else item["channel"], "manifestPath": item["channel"]}
    allocate = lambda preview, channel: {"channel": channel}
    assert precache(data, allocate, capture, accept)["complete"]
    assert data["stages"][0]["images"][0]["channelCacheComplete"]
    assert [image["channel"] for image in data["stages"][0]["images"][1:]] == ["rgba", "a"]
    assert precache(data, allocate, capture, accept)["complete"]
    assert len(data["stages"][0]["images"]) == 3


def test_partial_failure_keeps_verified_channel_and_reports_missing_for_repair():
    data = document()
    def capture(items):
        return {"captureSessionId": "session", "items": [{"key": "stage:0", "componentCount": 4,
                "channelGroups": [{"channel": "rgb"}, {"channel": "a"}],
                "receipts": [{"channel": "rgb"}], "errors": [{"channel": "a", "error": "read failed"}]}]}
    result = precache(data, lambda parent, channel: {}, capture,
                      lambda *args: {"channel": "rgba", "manifestPath": "verified"})
    assert not result["complete"] and result["errors"][0]["missingChannels"] == ["a"]
    assert len(data["stages"][0]["images"]) == 2
    assert not data["stages"][0]["images"][0]["channelCacheComplete"]


def test_single_component_and_explicit_no_split_do_not_capture():
    for changes in ({"componentCount": 1}, {"channelGroups": []}, {"attachmentKind": "dsv"}):
        data = document()
        data["stages"][0]["images"][0].update(changes)
        def unexpected(*args):
            raise AssertionError("No channel replay should be requested")
        assert precache(data, unexpected, unexpected, unexpected)["attempted"] == 0


def test_wrong_session_preserves_existing_diagram_and_never_accepts_images():
    data = document()
    def unexpected(*args):
        raise AssertionError("Foreign receipt cannot be admitted")
    result = precache(data, lambda *args: {}, lambda *args: {"captureSessionId": "foreign"}, unexpected)
    assert not result["complete"] and len(data["stages"][0]["images"]) == 1


def test_transient_channel_failure_retries_only_missing_group_before_publication():
    data = document()
    calls = []
    def capture(items):
        calls.append(items)
        return {"captureSessionId": "session", "replayRevision": [1, 2], "items": [{
            "key": "stage:0", "componentCount": 4,
            "channelGroups": [{"channel": "rgb"}, {"channel": "a"}],
            "receipts": [{"channel": "rgb" if len(calls) == 1 else "a"}],
            "errors": [{"channel": "a", "error": "transient read"}] if len(calls) == 1 else []}]}
    result = precache(data, lambda parent, channel: {"channel": channel}, capture,
                      lambda parent, item, request, reply: {"channel": item["channel"], "manifestPath": "verified"})
    assert result["complete"]
    assert len(calls) == 2 and calls[1][0]["requests"] == [{"channel": "a"}]
    assert len(data["stages"][0]["images"]) == 3
