from __future__ import annotations

import ctypes
import os
import sys
import threading

import pytest

from replaylab_agent import bridge


def test_pid_probe_keeps_high_handle_and_always_closes() -> None:
    high_handle = 0x1234567887654321
    calls: list[tuple[str, int]] = []

    def open_process(access, inherit, pid):
        assert access == bridge._PROCESS_QUERY_LIMITED_INFORMATION
        assert inherit is False
        assert pid == 4242
        return high_handle

    def get_exit_code(handle, value):
        calls.append(("query", handle))
        ctypes.cast(value, bridge._WINDOWS_DWORD_POINTER).contents.value = (
            bridge._STILL_ACTIVE
        )
        return 1

    def close_handle(handle):
        calls.append(("close", handle))
        return 1

    api = bridge._WindowsProcessApi(
        open_process=open_process,
        get_exit_code=get_exit_code,
        close_handle=close_handle,
    )

    assert bridge._pid_is_live_windows(4242, api) is True
    assert calls == [("query", high_handle), ("close", high_handle)]

    calls.clear()

    def failed_query(handle, unused_value):
        calls.append(("query", handle))
        return 0

    failed_api = bridge._WindowsProcessApi(
        open_process=open_process,
        get_exit_code=failed_query,
        close_handle=close_handle,
    )
    assert bridge._pid_is_live_windows(4242, failed_api) is False
    assert calls == [("query", high_handle), ("close", high_handle)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes singleton regression")
def test_pid_probe_reuses_typed_api_and_pointer_cache_under_load() -> None:
    api = bridge._windows_process_api()
    assert api is not None
    pointer_cache = getattr(ctypes, "_pointer_type_cache")
    pointer_types_before = set(pointer_cache)

    for _ in range(512):
        assert bridge._pid_is_live(os.getpid()) is True

    singleton_ids: list[int] = []
    barrier = threading.Barrier(9)

    def read_api() -> None:
        barrier.wait()
        singleton_ids.append(id(bridge._windows_process_api()))

    threads = [threading.Thread(target=read_api) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5.0)
        assert not thread.is_alive()

    assert set(singleton_ids) == {id(api)}
    assert set(pointer_cache) == pointer_types_before
