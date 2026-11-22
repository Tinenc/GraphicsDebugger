"""A user EID selection pauses restore IPC without abandoning other shaders."""

from unittest import mock

import pytest

from src import ipc_client


def yielded(message=None):
    return ipc_client.IPCBridgeResponseError(-32010, message or
        "Bread background tool yielded after a user replay operation; retry it if still needed")


def test_restore_resumes_same_operation_and_capture_with_original_deadline():
    client = ipc_client.IPCClient()
    checkpoint = mock.Mock()
    requests = []

    def send(method, params, timeout, *, deadline):
        requests.append((method, dict(params), timeout, deadline))
        if len(requests) < 3:
            raise yielded()
        return {"workspacePath": "target", "restored": True}

    with ipc_client.bind_direct_tool_call("owner", "a" * 32), \
            ipc_client.resume_after_foreground_yield(checkpoint), \
            mock.patch.object(client, "_call_locked", side_effect=send), \
            mock.patch.object(ipc_client.time, "sleep") as sleep:
        assert client.call("resolve_shader", {"event_id": 61990})["restored"] is True
    assert len(requests) == 3
    assert requests[0] == requests[1] == requests[2]
    assert requests[0][1]["__replaylab_expected_capture_session_id"] == "a" * 32
    assert checkpoint.call_count == 4
    assert [c.args[0] for c in sleep.call_args_list] == [0.05, 0.1]
    assert ipc_client._FOREGROUND_YIELD_CHECKPOINT.get() is None
    assert ipc_client._CALL_BINDING.get() is None


@pytest.mark.parametrize("failure", [
    yielded("Bread background tool was cancelled"),
    yielded("The frozen capture session changed before the Bread tool started"),
    ipc_client.IPCBridgeResponseError(-32000, next(iter(ipc_client._FOREGROUND_YIELD_MESSAGES))),
    ipc_client.IPCBridgeTimeoutError("missing reply may have committed"),
])
def test_non_yield_failures_are_never_retried(failure):
    client = ipc_client.IPCClient()
    with ipc_client.bind_direct_tool_call("owner", "a" * 32), \
            ipc_client.resume_after_foreground_yield(mock.Mock()), \
            mock.patch.object(client, "_call_locked", side_effect=failure) as send:
        with pytest.raises(type(failure)):
            client.call("apply_shader", {})
    assert send.call_count == 1


@pytest.mark.parametrize("pinned,enabled", [(False, True), (True, False)])
def test_unpinned_or_non_restore_calls_do_not_silently_retry(pinned, enabled):
    import contextlib
    client = ipc_client.IPCClient()
    with (ipc_client.bind_direct_tool_call("owner", "a" * 32) if pinned else contextlib.nullcontext()), \
            (ipc_client.resume_after_foreground_yield(mock.Mock()) if enabled else contextlib.nullcontext()), \
            mock.patch.object(client, "_call_locked", side_effect=yielded()) as send:
        with pytest.raises(ipc_client.IPCBridgeResponseError):
            client.call("read_shader", {})
    assert send.call_count == 1


def test_cancel_or_project_change_stops_before_resume():
    client = ipc_client.IPCClient()
    checkpoint = mock.Mock(side_effect=RuntimeError("project changed"))
    with ipc_client.bind_direct_tool_call("owner", "a" * 32), \
            ipc_client.resume_after_foreground_yield(checkpoint), \
            mock.patch.object(client, "_call_locked", side_effect=yielded()) as send:
        with pytest.raises(RuntimeError, match="project changed"):
            client.call("read_shader", {})
    assert send.call_count == 1


def test_repeated_yields_do_not_extend_operation_deadline():
    client = ipc_client.IPCClient()
    # call admission, initial send, catch, sleep completion.
    with ipc_client.bind_direct_tool_call("owner", "a" * 32), \
            ipc_client.resume_after_foreground_yield(mock.Mock()), \
            mock.patch.object(client, "_call_locked", side_effect=yielded()) as send, \
            mock.patch.object(ipc_client.time, "monotonic", side_effect=[0, 0, 0, .09, .1]), \
            mock.patch.object(ipc_client.time, "sleep"):
        with pytest.raises(ipc_client.IPCBridgeResponseError):
            client.call("read_shader", {}, timeout=.1)
    assert send.call_count == 1
