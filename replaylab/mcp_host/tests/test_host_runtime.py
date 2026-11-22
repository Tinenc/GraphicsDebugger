import asyncio
import ctypes
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import host_runtime
from src.host_runtime import instrument_tool_memory
from src.console_events import instrument_mcp_tool
import pytest


def test_operation_timing_trace_is_readable_bounded_and_best_effort(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    host_runtime.record_operation_timing(
        tool="restructure_shader_workspace_source",
        operation="extract",
        outcome="accepted",
        timing={
            "totalMs": 123.4567,
            "activePhase": "transaction",
            "dimensions": {
                "organizationId": "a" * 32,
                "operationId": "extract-12345678",
                "sourceByteSize": 580582,
                "sourceText": "must never reach the trace",
            },
            "phasesMs": {"proposalReplay": 100.1234, "sourceText": "secret"},
        },
    )
    path = tmp_path / "ReplayLab" / "logs" / "host-memory-trace.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["kind"] == "operation-timing"
    assert row["timing"]["totalMs"] == 123.457
    assert row["timing"]["dimensions"]["operationId"] == "extract-12345678"
    assert row["timing"]["phasesMs"] == {"proposalReplay": 100.123}
    assert "sourceText" not in path.read_text(encoding="utf-8")

    def fail_log(unused):
        raise OSError("simulated unavailable trace")
    monkeypatch.setattr(host_runtime, "_append_trace", fail_log)
    host_runtime.record_operation_timing(
        tool="restructure_shader_workspace_source", operation="extract",
        outcome="error", timing={"totalMs": 1.0}, error_type="RuntimeError",
    )


def test_instrument_tool_memory_preserves_async_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    async def restructure_shader_workspace_source(operation: str = "extract") -> str:
        return '{"ok":true,"operation":"%s"}' % operation

    wrapped = instrument_tool_memory(restructure_shader_workspace_source)
    assert inspect.iscoroutinefunction(wrapped)
    assert asyncio.run(wrapped("extract")) == '{"ok":true,"operation":"extract"}'


def test_instrument_tool_memory_keeps_sync_tools_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def get_host_runtime_status() -> str:
        return "idle"

    wrapped = instrument_tool_memory(get_host_runtime_status)
    assert not inspect.iscoroutinefunction(wrapped)
    assert wrapped() == "idle"


def test_instrument_mcp_tool_preserves_async_tools():
    async def describe_unpacked_asset(relative_path: str) -> str:
        return '{"path":"%s"}' % relative_path

    wrapped = instrument_mcp_tool(describe_unpacked_asset)
    assert inspect.iscoroutinefunction(wrapped)
    assert asyncio.run(wrapped("Mats/A.uasset")) == '{"path":"Mats/A.uasset"}'


def test_async_replay_cleanup_does_not_block_status_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    release_started = threading.Event()

    def slow_release():
        release_started.set()
        time.sleep(1.5)
        return {"applied": True, "replayCacheCleared": True}

    monkeypatch.setattr(host_runtime, "_release_qrenderdoc_transient_caches", slow_release)
    monkeypatch.setattr(host_runtime, "memory_snapshot", lambda: {})
    monkeypatch.setattr(host_runtime, "_append_trace", lambda unused: None)

    async def get_bound_textures() -> str:
        return "done"

    async def scenario() -> None:
        replay_call = asyncio.create_task(
            instrument_tool_memory(get_bound_textures)()
        )
        assert await asyncio.to_thread(release_started.wait, 0.5)
        started = time.monotonic()

        def get_host_runtime_status() -> str:
            return "responsive"

        status = instrument_tool_memory(get_host_runtime_status)()
        await asyncio.sleep(0.05)
        assert status == "responsive"
        assert time.monotonic() - started < 0.8
        assert await replay_call == "done"

    asyncio.run(scenario())




def test_stale_double_close_cannot_release_new_long_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    host_runtime.reset_long_tool_state()
    releases = []
    monkeypatch.setattr(host_runtime, "memory_snapshot", lambda: {})
    monkeypatch.setattr(host_runtime, "_append_trace", lambda unused: None)
    monkeypatch.setattr(
        host_runtime,
        "_release_qrenderdoc_transient_caches",
        lambda: releases.append("released") or {"applied": True},
    )

    first = host_runtime.LongToolSlot(
        "finish_shader_workspace_organization", {"organization_id": "first"}
    )
    assert first.busy is None
    first_receipt = first.close(ok=True)
    assert first_receipt is not None
    assert first_receipt["slotReleased"] is True
    assert releases == ["released"]

    second = host_runtime.LongToolSlot(
        "finish_shader_workspace_organization", {"organization_id": "second"}
    )
    assert second.busy is None
    try:
        # The old owner's finally block can run again after another worker has
        # acquired the global slot. It must return its cached receipt without
        # touching the new owner or issuing another cache release.
        assert first.close(ok=False) == first_receipt
        assert first.update("stale-worker", done=999) is False
        status = host_runtime.current_status()
        assert status["toolStatus"] == "running"
        assert status["currentTool"]["name"] == "finish_shader_workspace_organization"
        assert status["currentTool"]["arguments"]["organization_id"] == "second"
        assert status["currentTool"]["phase"] == "started"
        assert "progress" not in status["currentTool"]
        assert "ownerToken" not in status["currentTool"]
        assert releases == ["released"]
        stale = host_runtime.finish_long_tool(
            ok=False, owner_token=first._owner_token
        )
        assert stale["slotReleased"] is False
        assert stale["reason"] == "long-tool-owner-mismatch"
        assert host_runtime.current_status()["toolStatus"] == "running"
    finally:
        second_receipt = second.close(ok=True)
        host_runtime.reset_long_tool_state()

    assert second_receipt is not None
    assert second_receipt["slotReleased"] is True
    assert releases == ["released", "released"]


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes pointer-cache regression")
def test_memory_sampler_reuses_winapi_and_pointer_types_under_load():
    first = host_runtime._windows_process_memory(os.getpid())
    assert first is not None
    api = host_runtime._windows_process_memory_api()
    assert api is not None
    pointer_cache = getattr(ctypes, "_pointer_type_cache")
    pointer_types_before = set(pointer_cache)

    for _ in range(512):
        sample = host_runtime._windows_process_memory(os.getpid())
        assert sample is not None and sample["pid"] == os.getpid()

    singleton_ids = []
    barrier = threading.Barrier(9)

    def read_api() -> None:
        barrier.wait()
        singleton_ids.append(id(host_runtime._windows_process_memory_api()))

    threads = [threading.Thread(target=read_api) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5.0)
        assert not thread.is_alive()

    assert set(singleton_ids) == {id(api)}
    assert set(pointer_cache) == pointer_types_before
