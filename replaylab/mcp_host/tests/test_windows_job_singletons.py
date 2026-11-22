from __future__ import annotations

import ctypes
import os
import threading
from types import SimpleNamespace

import pytest

from src.shader_workspace import tools
from src.symbols import providers


def _fake_provider_api(high_job: int, calls: list[tuple]) -> providers._WindowsJobApi:
    return providers._WindowsJobApi(
        create_job=lambda unused_security, unused_name: high_job,
        set_information=lambda job, info_class, unused_info, unused_size: (
            calls.append(("configure", job, info_class)) or 1
        ),
        assign_process=lambda job, process: (
            calls.append(("assign", job, process)) or 1
        ),
        terminate_job=lambda job, code: (
            calls.append(("terminate", job, code)) or 1
        ),
        close_handle=lambda handle: calls.append(("close", handle)) or 1,
    )


def _fake_tool_api(high_job: int, calls: list[tuple]) -> tools._WindowsJobApi:
    return tools._WindowsJobApi(
        create_job=lambda unused_security, unused_name: high_job,
        set_information=lambda job, info_class, unused_info, unused_size: (
            calls.append(("configure", job, info_class)) or 1
        ),
        assign_process=lambda job, process: (
            calls.append(("assign", job, process)) or 1
        ),
        close_handle=lambda handle: calls.append(("close", handle)) or 1,
    )


def test_provider_and_tool_jobs_keep_64_bit_handles() -> None:
    high_job = 0x1234567887654321
    high_process = 0x2234567887654321

    provider_calls: list[tuple] = []
    provider_api = _fake_provider_api(high_job, provider_calls)
    provider_job = providers._assign_windows_kill_job_with_api(
        SimpleNamespace(_handle=high_process), provider_api
    )
    assert provider_job == high_job
    assert ("assign", high_job, high_process) in provider_calls
    assert not any(call[0] == "close" for call in provider_calls)

    tool_calls: list[tuple] = []
    tool_api = _fake_tool_api(high_job, tool_calls)
    tool_job = tools._attach_windows_kill_job_with_api(
        SimpleNamespace(_handle=high_process), tool_api
    )
    assert tool_job == high_job
    assert ("assign", high_job, high_process) in tool_calls
    assert not any(call[0] == "close" for call in tool_calls)


def test_failed_job_configuration_closes_full_width_handle() -> None:
    high_job = 0x1234567887654321
    calls: list[tuple] = []
    provider_api = providers._WindowsJobApi(
        create_job=lambda unused_security, unused_name: high_job,
        set_information=lambda *unused: 0,
        assign_process=lambda *unused: 1,
        terminate_job=lambda *unused: 1,
        close_handle=lambda handle: calls.append(("provider-close", handle)) or 1,
    )
    tool_api = tools._WindowsJobApi(
        create_job=lambda unused_security, unused_name: high_job,
        set_information=lambda *unused: 0,
        assign_process=lambda *unused: 1,
        close_handle=lambda handle: calls.append(("tool-close", handle)) or 1,
    )

    assert providers._assign_windows_kill_job_with_api(
        SimpleNamespace(_handle=11), provider_api
    ) is None
    assert tools._attach_windows_kill_job_with_api(
        SimpleNamespace(_handle=11), tool_api
    ) is None
    assert calls == [
        ("provider-close", high_job),
        ("tool-close", high_job),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object cleanup regression")
def test_job_cleanup_uses_full_width_handle(monkeypatch, tmp_path) -> None:
    high_job = 0x1234567887654321
    calls: list[tuple] = []
    provider_api = _fake_provider_api(high_job, calls)
    tool_api = _fake_tool_api(high_job, calls)
    monkeypatch.setattr(providers, "_windows_job_api", lambda: provider_api)
    monkeypatch.setattr(tools, "_windows_job_api", lambda: tool_api)

    providers._terminate_process_tree(
        SimpleNamespace(poll=lambda: 0, kill=lambda: None),
        high_job,
        tmp_path,
    )
    tools._close_windows_handle(high_job)

    assert ("terminate", high_job, 1) in calls
    assert calls.count(("close", high_job)) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes singleton regression")
def test_job_api_singletons_do_not_grow_pointer_type_cache() -> None:
    provider_api = providers._windows_job_api()
    tool_api = tools._windows_job_api()
    assert provider_api is not None
    assert tool_api is not None
    pointer_cache = getattr(ctypes, "_pointer_type_cache")
    pointer_types_before = set(pointer_cache)

    for _ in range(512):
        assert providers._windows_job_api() is provider_api
        assert tools._windows_job_api() is tool_api

    provider_ids: list[int] = []
    tool_ids: list[int] = []
    barrier = threading.Barrier(9)

    def read_apis() -> None:
        barrier.wait()
        provider_ids.append(id(providers._windows_job_api()))
        tool_ids.append(id(tools._windows_job_api()))

    threads = [threading.Thread(target=read_apis) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5.0)
        assert not thread.is_alive()

    assert set(provider_ids) == {id(provider_api)}
    assert set(tool_ids) == {id(tool_api)}
    assert set(pointer_cache) == pointer_types_before
