"""A serialized replay queue does not silently multiply an IPC call's timeout."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest import mock

import pytest

from src import ipc_client


def test_waiter_times_out_without_dispatch_while_previous_call_is_still_running():
    client = ipc_client.IPCClient(instance_id="original-window")
    entered, release = threading.Event(), threading.Event()

    def running(method, params, timeout, **unused):
        assert method == "first"
        entered.set()
        assert release.wait(2)
        return "first-result"

    with mock.patch.object(client, "_call_locked", side_effect=running) as dispatch, \
            ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.call, "first", {}, 1.5)
        try:
            assert entered.wait(1)
            start = time.monotonic()
            with pytest.raises(ipc_client.IPCBridgeBusyError, match="request was not sent"):
                pool.submit(client.call, "second", {}, 0.05).result(timeout=0.5)
            assert time.monotonic() - start < 0.5
            assert not first.done()
            assert dispatch.call_count == 1
            assert client.instance_id == "original-window"
        finally:
            release.set()
        assert first.result(timeout=1) == "first-result"


def test_queue_time_and_response_wait_share_one_deadline(tmp_path):
    now = [100.0]

    class Queue:
        released = False

        def acquire(self, *, timeout):
            assert timeout == 10.0
            now[0] += 6.0  # Earlier replay work used six seconds of our budget.
            return True

        def release(self):
            self.released = True

    client = ipc_client.IPCClient(instance_id="original-window", timeout=10)
    queue = client._mailbox_lock = Queue()
    requests, responses = tmp_path / "requests", tmp_path / "responses"
    with mock.patch.object(ipc_client.time, "monotonic", side_effect=lambda: now[0]), \
            mock.patch.object(ipc_client.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
            mock.patch.object(client, "_mailbox", return_value=(str(requests), str(responses))), \
            mock.patch.object(client, "bound_instance_state", return_value={"alive": True, "instanceId": "original-window"}):
        with pytest.raises(ipc_client.IPCBridgeBusyError, match="still running"):
            client.call("get_bound_textures", {})
    assert now[0] == pytest.approx(110.0)
    assert queue.released
    assert list(requests.iterdir()) == []
    assert list(responses.iterdir()) == []
    assert client.instance_id == "original-window"


def test_zero_budget_does_not_publish_and_releases_serialization_lock():
    client = ipc_client.IPCClient(instance_id="original-window")
    with mock.patch.object(client, "_call_locked") as dispatch:
        with pytest.raises(ipc_client.IPCBridgeBusyError, match="request was not sent"):
            client.call("ping", {}, timeout=0)
        dispatch.assert_not_called()
    assert client._mailbox_lock.acquire(blocking=False)
    client._mailbox_lock.release()
    assert client.instance_id == "original-window"


def test_expired_queue_call_preserves_direct_instance_and_capture_binding():
    client = ipc_client.IPCClient(instance_id="default-window")
    client._mailbox_lock.acquire()
    try:
        with ipc_client.bind_direct_tool_call("capture-window", "a" * 32):
            with pytest.raises(ipc_client.IPCBridgeBusyError):
                client.call("ping", {}, timeout=0)
            assert ipc_client._CALL_BINDING.get() == ("capture-window", "a" * 32)
        assert ipc_client._CALL_BINDING.get() is None
        assert client.instance_id == "default-window"
    finally:
        client._mailbox_lock.release()
