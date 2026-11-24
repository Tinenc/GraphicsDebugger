"""Background cancellation must stop reads without leaking across workers."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
from unittest import mock

import pytest


MODULE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_console.py"
spec = importlib.util.spec_from_file_location("shader_console_cancellation", MODULE)
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


def test_cancelled_snapshot_stops_before_any_journal_or_project_read():
    stop = threading.Event()
    stop.set()
    with mock.patch.object(console, "_load_generic_console_entries") as read:
        with pytest.raises(console.ShaderConsoleReadCancelled):
            with console.cancellable_console_reads(stop):
                console.load_console_snapshot()
        read.assert_not_called()
    assert not hasattr(console._console_read_scope, "events")


def test_cancellation_after_file_read_is_not_reported_as_bad_json():
    stop = threading.Event()

    def read(*unused):
        stop.set()
        return b'{"valid": true}'

    with mock.patch.object(console, "_read_file_bytes", side_effect=read):
        with console.cancellable_console_reads(stop):
            with pytest.raises(console.ShaderConsoleReadCancelled) as caught:
                console._read_json("unused", 1024, "fixture")
            assert not isinstance(caught.value, console.ShaderConsoleError)
            stop.clear()


def test_nested_scope_restores_outer_cancellation_and_cleans_on_error():
    outer, inner = threading.Event(), threading.Event()
    with console.cancellable_console_reads(outer):
        with pytest.raises(ValueError, match="fixture"):
            with console.cancellable_console_reads(inner):
                raise ValueError("fixture")
        assert console._console_read_scope.events == (outer,)
        outer.set()
        with pytest.raises(console.ShaderConsoleReadCancelled):
            with console.cancellable_console_reads(inner):
                pytest.fail("Nested scope must inherit the outer stop")
        outer.clear()
    assert not hasattr(console._console_read_scope, "events")


def test_reused_worker_and_other_thread_do_not_inherit_cancelled_scope(tmp_path):
    source = tmp_path / "source.json"
    source.write_text('{"valid": true}', encoding="utf-8")
    stop = threading.Event()
    stop.set()

    def cancelled():
        with pytest.raises(console.ShaderConsoleReadCancelled):
            with console.cancellable_console_reads(stop):
                console._read_json(str(source), 1024, "fixture")
        return threading.get_ident()

    def normal():
        return threading.get_ident(), console._read_json(str(source), 1024, "fixture")

    with ThreadPoolExecutor(max_workers=1) as pool:
        previous = pool.submit(cancelled).result(timeout=5)
        current, value = pool.submit(normal).result(timeout=5)
    assert previous == current
    assert value == {"valid": True}
    assert normal()[1] == {"valid": True}


def test_active_worker_cancellation_does_not_cancel_concurrent_reader(tmp_path):
    source = tmp_path / "source.json"
    source.write_text('{"valid": true}', encoding="utf-8")
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()

    def worker():
        with pytest.raises(console.ShaderConsoleReadCancelled):
            with console.cancellable_console_reads(stop):
                entered.set()
                assert release.wait(5)
                console._read_json(str(source), 1024, "fixture")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker)
        try:
            assert entered.wait(5)
            stop.set()
            assert console._read_json(str(source), 1024, "fixture") == {"valid": True}
        finally:
            release.set()
        pending.result(timeout=5)
