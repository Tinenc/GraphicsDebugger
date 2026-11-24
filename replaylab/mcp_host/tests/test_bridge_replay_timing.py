"""Replay timing separates scheduling/GIL wait from work without changing recovery."""
import json
from types import SimpleNamespace

import pytest

from tests.test_bridge_catalog_rebind import _load_bridge


@pytest.mark.parametrize("advertised", [True, False, None])
def test_read_pipeline_reuse_is_capability_and_capture_scoped(advertised):
    bridge = _load_bridge()
    observed = []
    reads = []
    def status():
        reads.append(1)
        if advertised is None:
            raise AttributeError("older replay core")
        return json.dumps({"pipelineStateReuse": advertised})
    controller = SimpleNamespace(GetReplayCheckpointStatus=status)
    bridge._set_frame_event = lambda ctrl, eid, force: observed.append((eid, force))
    bridge._capture_session_id = "first-capture"
    bridge._select_event_for_read(controller, 12)
    bridge._select_event_for_read(controller, 12)
    assert len(reads) == 1
    assert observed == [(12, advertised is not True)] * 2
    bridge._capture_session_id = "next-capture"
    bridge._select_event_for_read(controller, 13)
    assert len(reads) == 2


@pytest.mark.parametrize("fails", [False, True])
def test_replay_timing_includes_restoration_and_preserves_real_outcome(fails):
    bridge = _load_bridge()
    clock = {"wall": 1.0, "cpu": 0.01}
    bridge.time = SimpleNamespace(monotonic=lambda: clock["wall"], thread_time=lambda: clock["cpu"])
    timing = {"uiTransactions": [], "replayTransactions": []}
    events = []
    def set_event(event_id, force):
        events.append((event_id, force))
        if event_id == 13:
            clock.update(wall=3.7, cpu=0.07)
    controller = SimpleNamespace(SetFrameEvent=set_event)
    def invoke(tag, callback):
        assert tag == "bounded-test"
        clock["wall"] = 3.0
        callback(controller)
    replay = SimpleNamespace(BlockInvokeTagged=invoke)
    tracker = {"backgroundReplayTag": "bounded-test", "visibleEventId": 13}
    bridge._request_event_state.current = tracker
    bridge._request_dispatch_state.current = {
        "stageTiming": timing, "captureSnapshot": {"replayManager": replay}}
    def work(ctrl):
        bridge._set_frame_event(ctrl, 42)
        clock.update(wall=3.5, cpu=0.04)
        if fails:
            raise ValueError("private failure detail")
        return "private Source result"
    if fails:
        with pytest.raises(ValueError, match="private failure detail"):
            bridge._replay(work)
    else:
        assert bridge._replay(work) == "private Source result"
    assert events == [(42, True), (13, True)]
    assert tracker["eventDirty"] is False and "replayManager" not in tracker
    assert timing["replayTransactions"] == [{"callback": "work",
        "outcome": "failed" if fails else "completed", "replayQueueAndGILWallMs": 2000.0,
        "callbackWallMs": 700.0, "callbackThreadCpuMs": 60.0}]
    assert "private" not in json.dumps(timing)


def test_failed_replay_dispatch_is_timed_without_invoking_callback():
    bridge = _load_bridge()
    clock = {"wall": 1.0}
    bridge.time = SimpleNamespace(monotonic=lambda: clock["wall"])
    timing = {"replayTransactions": []}
    def rejected(callback):
        clock["wall"] = 2.0
        raise RuntimeError("queue rejected")
    bridge._request_dispatch_state.current = {"stageTiming": timing,
        "captureSnapshot": {"replayManager": SimpleNamespace(BlockInvoke=rejected)}}
    with pytest.raises(RuntimeError, match="queue rejected"):
        bridge._replay(lambda ctrl: pytest.fail("rejected work must not execute"))
    assert timing["replayTransactions"] == [{"callback": "replayTransaction", "outcome": "failed",
        "replayQueueAndGILWallMs": 1000.0, "callbackWallMs": 0.0, "callbackThreadCpuMs": None}]


def test_bounded_or_broken_telemetry_cannot_affect_replay_result():
    bridge = _load_bridge()
    replay = SimpleNamespace(BlockInvoke=lambda callback: callback(object()))
    for timing in ({"replayTransactions": []}, {"replayTransactions": None}):
        bridge._request_dispatch_state.current = {"stageTiming": timing,
            "captureSnapshot": {"replayManager": replay}}
        for unused in range(20):
            assert bridge._replay(lambda ctrl: "successful operation") == "successful operation"
        if isinstance(timing["replayTransactions"], list):
            assert len(timing["replayTransactions"]) == 16


@pytest.mark.parametrize("yielded", [False, True])
def test_failed_visible_event_restore_supersedes_retryable_preemption(yielded):
    bridge = _load_bridge()
    def set_event(event_id, force):
        if event_id == 13:
            raise RuntimeError("restore device failure")
    controller = SimpleNamespace(SetFrameEvent=set_event)
    replay = SimpleNamespace(BlockInvokeTagged=lambda tag, callback: callback(controller))
    tracker = {"backgroundReplayTag": "bounded-test", "visibleEventId": 13}
    bridge._request_event_state.current = tracker
    bridge._request_dispatch_state.current = {"captureSnapshot": {"replayManager": replay}}
    def work(ctrl):
        bridge._set_frame_event(ctrl, 42)
        if yielded:
            raise bridge._ReplayLabBackgroundPreempted(
                "Bread background tool yielded after a user replay operation; retry it if still needed")
        raise ValueError("original failure")
    with pytest.raises(RuntimeError, match="could not restore.*restore device failure") as caught:
        bridge._replay(work)
    assert not isinstance(caught.value, bridge._ReplayLabBackgroundPreempted)
    assert "original operation" in str(caught.value)
    assert tracker["eventDirty"] is True and tracker["lastSetSucceeded"] is False
