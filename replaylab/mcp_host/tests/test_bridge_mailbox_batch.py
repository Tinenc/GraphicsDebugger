"""Real JsonPoller filesystem claims, independent admission and bounded FIFO."""

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.test_bridge_dispatch_identity import dispatch


@pytest.fixture
def mailbox(dispatch, monkeypatch):
    bridge = dispatch.bridge
    for key, name in (("REQUEST_DIR", "requests"), ("RESPONSE_DIR", "responses"),
                      ("PROCESSING_DIR", "processing")):
        directory = dispatch.root / name
        directory.mkdir()
        monkeypatch.setattr(bridge, key, str(directory))
    for key, name in (("READY_FILE", "legacy.ready"), ("REQ_FILE", "legacy.request.json"),
                      ("RESP_FILE", "legacy.response.json")):
        monkeypatch.setattr(bridge, key, str(dispatch.root / name))
    dispatch.poller._accepting = True
    bridge._publish_instance_manifest = mock.Mock()
    return dispatch


def request(mailbox, token, modified_ns, *, method="get_current_renderdoc_context", params=None):
    request_id = token * 32
    path = Path(mailbox.bridge.REQUEST_DIR) / (request_id + ".json")
    path.write_text(json.dumps({"id": request_id, "method": method,
                               "params": {"identity_only": True} if params is None else params}), encoding="utf-8")
    os.utime(path, ns=(modified_ns, modified_ns))
    return path


def pop_all(mailbox):
    return [mailbox.poller._request_queue.get_nowait()
            for unused in range(mailbox.poller._request_queue.qsize())]


def test_one_tick_claims_small_bounded_oldest_first_burst_without_parsing_or_running_handlers(mailbox):
    # Random UUID lexical order is opposite publication age. An incoming smaller
    # UUID may not jump in front of an already pending older request.
    paths = [request(mailbox, token, (index + 1) * 1000000000)
             for index, token in enumerate("fedcba9876")]
    mailbox.bridge._read_ipc_request_json = mock.Mock(side_effect=AssertionError("Qt must not parse request bodies"))
    timing = {"pollGapMs": 250.0, "manifestPublishWallMs": 0.0}
    assert mailbox.poller._process_queued_request(timing) is True
    items = pop_all(mailbox)
    assert len(items) == mailbox.bridge.IPC_MAX_CLAIMS_PER_TICK == 4
    assert [item["requestId"] for item in items] == [path.stem for path in paths[:4]]
    assert all(item["request"] is None and Path(item["claimPath"]).is_file() for item in items)
    assert all(Path(item["responsePath"]).exists() is False for item in items)
    assert all(path.is_file() for path in paths[4:])
    assert timing["pendingRequestsAtPoll"] == 10
    assert all(item["stageTiming"]["pendingRequestsAtPoll"] == 10 for item in items)
    mailbox.bridge._read_ipc_request_json.assert_not_called()
    # Getter observations are independent per admission; no shared snapshot cache.
    assert mailbox.getters.count("api") == 4
    # Lifecycle/token guards also read the path; they remain in force.
    assert mailbox.getters.count("path") >= 4
    assert len({id(item["captureSnapshot"]) for item in items}) == 4
    assert len({id(item["captureSnapshot"]["captureIdentity"]) for item in items}) == 4


def test_new_uuid_cannot_starve_older_pending_second_batch(mailbox):
    paths = [request(mailbox, token, (index + 1) * 1000000000)
             for index, token in enumerate("fedcba")]
    assert mailbox.poller._process_queued_request()
    pop_all(mailbox)
    newcomer = request(mailbox, "0", 20000000000)
    assert mailbox.poller._process_queued_request()
    items = pop_all(mailbox)
    assert [item["requestId"] for item in items] == [paths[4].stem, paths[5].stem, newcomer.stem]


def test_atomic_claim_race_does_not_consume_or_execute_another_pollers_request(mailbox):
    paths = [request(mailbox, token, (index + 1) * 1000000000) for index, token in enumerate("fedcba")]
    replace = mailbox.bridge.os.replace
    attempts = []

    def raced(source, destination):
        attempts.append(Path(source).name)
        if len(attempts) == 1:
            raise FileNotFoundError("another claimant won")
        return replace(source, destination)

    with mock.patch.object(mailbox.bridge.os, "replace", side_effect=raced):
        assert mailbox.poller._process_queued_request()
    assert len(attempts) == 4
    assert [item["requestId"] for item in pop_all(mailbox)] == [path.stem for path in paths[1:4]]
    assert paths[0].exists() and all(path.exists() for path in paths[4:])


def test_capture_reload_between_batch_claims_keeps_separate_facts_and_fails_old_request(mailbox):
    first, second = request(mailbox, "f", 1000000000), request(mailbox, "a", 2000000000)
    original_submit = mailbox.poller._submit_claimed_request
    calls = []

    def reloaded(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        calls.append(args[0])
        if len(calls) == 1:
            mailbox.bridge._capture_session_id = "b" * 32
            mailbox.state["api"] = "GraphicsAPI.Vulkan"
            mailbox.poller.cancel_active_for_user_activity(capture_changed=True)
        return result

    mailbox.poller._submit_claimed_request = reloaded
    assert mailbox.poller._process_queued_request()
    items = pop_all(mailbox)
    assert [item["requestId"] for item in items] == [first.stem, second.stem]
    assert [item["captureSnapshot"]["captureSessionId"] for item in items] == ["a" * 32, "b" * 32]
    assert [item["captureSnapshot"]["captureIdentity"]["api"] for item in items] == ["GraphicsAPI.D3D11", "GraphicsAPI.Vulkan"]
    assert "error" in mailbox.poller._execute_item(items[0])
    assert mailbox.poller._execute_item(items[1])["result"]["capture"]["captureSessionId"] == "b" * 32


def test_user_activity_between_claims_still_cancels_stale_sensitive_work_in_actual_worker(mailbox):
    executed = []
    mailbox.bridge.METHODS["batch_selection_test"] = lambda params: executed.append(params["index"]) or {}
    first = request(mailbox, "f", 1000000000, method="batch_selection_test", params={"index": 1})
    second = request(mailbox, "a", 2000000000, method="batch_selection_test", params={"index": 2})
    original_submit = mailbox.poller._submit_claimed_request
    claimed = []

    def changed(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        claimed.append(args[0])
        if len(claimed) == 1:
            mailbox.poller.cancel_active_for_user_activity()
        return result

    mailbox.poller._submit_claimed_request = changed
    assert mailbox.poller._process_queued_request()
    mailbox.poller._request_queue.put(mailbox.poller._WORKER_STOP)
    worker = threading.Thread(target=mailbox.poller._worker_main)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    first_response = json.loads((Path(mailbox.bridge.RESPONSE_DIR) / first.name).read_text())
    second_response = json.loads((Path(mailbox.bridge.RESPONSE_DIR) / second.name).read_text())
    assert first_response["error"]["code"] == -32010
    assert second_response["result"] == {}
    assert executed == [2]


def test_stop_between_claims_leaves_remaining_files_and_cancels_owned_item(mailbox):
    first, second = request(mailbox, "f", 1000000000), request(mailbox, "a", 2000000000)
    original_submit = mailbox.poller._submit_claimed_request

    def stopped(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        mailbox.poller._stop_event.set()
        return result

    mailbox.poller._submit_claimed_request = stopped
    assert mailbox.poller._process_queued_request()
    assert not first.exists() and second.exists()
    mailbox.poller.stop()
    response = json.loads((Path(mailbox.bridge.RESPONSE_DIR) / first.name).read_text())
    assert response["error"]["code"] == -32010
    assert not list(Path(mailbox.bridge.PROCESSING_DIR).iterdir())
    assert mailbox.poller._process_queued_request() is False
    assert second.exists()


def test_batch_preserves_existing_bounded_queue_backpressure_without_running_overflow(mailbox):
    paths = [request(mailbox, token, (index + 1) * 1000000000) for index, token in enumerate("fedcba9876")]
    assert mailbox.poller._process_queued_request()
    assert mailbox.poller._process_queued_request()
    assert mailbox.poller._request_queue.qsize() == mailbox.bridge.MAX_IPC_BACKGROUND_REQUESTS == 8
    assert mailbox.poller._process_queued_request()
    assert mailbox.poller._request_queue.qsize() == 8
    for path in paths[8:]:
        response = json.loads((Path(mailbox.bridge.RESPONSE_DIR) / path.name).read_text())
        assert response["error"]["code"] == -32010
        assert "queue is full" in response["error"]["message"]
    assert all(Path(item["claimPath"]).exists() for item in pop_all(mailbox))


def test_single_request_handler_and_legacy_mailbox_keep_their_wire_results(mailbox):
    modern = request(mailbox, "f", 1000000000)
    mailbox.poller.timerEvent(None)
    item = pop_all(mailbox)[0]
    response = mailbox.poller._execute_item(item)
    assert response["id"] == modern.stem and response["result"]["identityOnly"] is True
    legacy_id = "1" * 32
    Path(mailbox.bridge.REQ_FILE).write_text(json.dumps({"id": legacy_id,
        "method": "get_current_renderdoc_context", "params": {"identity_only": True}}), encoding="utf-8")
    Path(mailbox.bridge.READY_FILE).write_text("ready", encoding="utf-8")
    mailbox.poller.timerEvent(None)
    item = pop_all(mailbox)[0]
    assert item["expectedId"] is None and item["responsePath"] == mailbox.bridge.RESP_FILE
    response = mailbox.poller._execute_item(item)
    assert response["id"] == legacy_id and response["result"]["identityOnly"] is True


def test_tick_gap_pending_count_and_manifest_write_delay_reach_only_safe_envelope_fields(mailbox, monkeypatch):
    # This synthetic clock distinguishes delayed timer entry from synchronous
    # manifest persistence before admission; neither number claims Qt-only time.
    clock = {"wall": 1.0, "unix": 1000.0}
    monkeypatch.setattr(mailbox.bridge, "time", SimpleNamespace(
        monotonic=lambda: clock["wall"], time=lambda: clock["unix"], thread_time=lambda: 0.0))

    def persist():
        clock["wall"] += 0.2

    mailbox.bridge._publish_instance_manifest = persist
    request(mailbox, "f", 1000000000, params={"identity_only": True, "providerReasoning": "private", "credential": "secret"})
    mailbox.poller.timerEvent(None)
    item = pop_all(mailbox)[0]
    mailbox.poller._finish_item(item, mailbox.poller._execute_item(item))
    response = json.loads(Path(item["responsePath"]).read_text())
    diagnostic = response["bridgeTiming"]
    assert diagnostic["pollGapMs"] is None
    assert diagnostic["pendingRequestsAtPoll"] == 1
    assert diagnostic["manifestPublishWallMs"] == pytest.approx(200.0)
    assert "private" not in json.dumps(diagnostic) and "secret" not in json.dumps(diagnostic)
    clock.update(wall=6.0, unix=1000.5)
    request(mailbox, "a", 2000000000)
    mailbox.poller.timerEvent(None)
    item = pop_all(mailbox)[0]
    diagnostic = mailbox.bridge._bridge_request_timing(item)
    assert diagnostic["pollGapMs"] == 5000.0
    assert diagnostic["manifestPublishWallMs"] == 0.0
    assert diagnostic["pendingRequestsAtPoll"] == 1
