"""Restore diagnostics survive Windows readers and delayed progress callbacks."""

import ctypes
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import internal_tool_service as internal
from src.shader_workspace import io as workspace_io


def request(tmp_path):
    capture = tmp_path / "frame.rdc"
    if not capture.exists():
        capture.write_bytes(b"test capture")
    return {"projectRoot": str(tmp_path), "capturePath": str(capture),
            "captureSessionId": "a" * 32}


def record(tmp_path):
    args = request(tmp_path)
    path = internal._restore_result_record_path(args["projectRoot"], args["capturePath"])
    return json.loads(path.read_text(encoding="utf-8"))


def wait_completed(registry, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = registry.get(job_id)
        if value["status"] in {"completed", "failed"}:
            return value
        time.sleep(0.005)
    pytest.fail("restore worker did not finish")


def progress(item):
    return {"phase": "complete", "message": "restore phase", "item": item,
            "current": 0, "total": 0}


def failed_item_result():
    return {"completed": True, "shaders": {"attempted": 1, "applied": 0, "failed": 1,
            "items": [{"error": "published project Shader artifact differs from live validation"}]}}


@pytest.mark.skipif(os.name != "nt", reason="native Windows sharing semantics")
def test_terminal_result_retries_real_reader_sharing_violation(tmp_path, monkeypatch):
    args = request(tmp_path)
    internal.persist_restore_result(args["projectRoot"], args["capturePath"], None, None,
                                    running=True, progress=[progress("complete")])
    path = internal._restore_result_record_path(args["projectRoot"], args["capturePath"])
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    denied, released = threading.Event(), threading.Event()
    replace = workspace_io.os.replace
    attempts = []

    def replacing(source, destination):
        attempts.append(source)
        try:
            return replace(source, destination)
        except PermissionError:
            denied.set()
            assert released.wait(2)
            raise

    monkeypatch.setattr(workspace_io.os, "replace", replacing)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(internal.persist_restore_result, args["projectRoot"],
                                     args["capturePath"], failed_item_result(), None)
            try:
                assert denied.wait(2)
            finally:
                kernel.CloseHandle(handle)
                handle = None
                released.set()
            future.result(timeout=2)
        saved = record(tmp_path)
        assert len(attempts) == 2
        assert saved["status"] == "finished"
        assert saved["completed"] is True
        assert saved["shaders"] == failed_item_result()["shaders"]
        assert not list(path.parent.glob("*.tmp*"))
    finally:
        released.set()
        if handle is not None:
            kernel.CloseHandle(handle)


def test_out_of_order_progress_keeps_highest_sequence(tmp_path, monkeypatch):
    delayed, release = threading.Event(), threading.Event()
    observed = []
    def runner(*args, progress_callback, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as executor:
            old = executor.submit(progress_callback, progress("older"))
            assert delayed.wait(2)
            progress_callback(progress("newer"))
            release.set()
            old.result(timeout=2)
        observed.append(record(tmp_path))
        return failed_item_result()

    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    publish = registry._persist_result

    def delayed_publish(job_id, result, error, **kwargs):
        if kwargs.get("running") and kwargs["progress"][-1]["sequence"] == 1:
            delayed.set()
            assert release.wait(2)
        return publish(job_id, result, error, **kwargs)

    monkeypatch.setattr(registry, "_persist_result", delayed_publish)
    try:
        job, unused = registry.submit(request(tmp_path))
        assert wait_completed(registry, job["jobId"])["status"] == "completed"
        assert observed[0]["latestSequence"] == 2
        assert observed[0]["progress"][-1]["item"] == "newer"
        assert record(tmp_path)["shaders"]["failed"] == 1
    finally:
        release.set()
        registry.shutdown()


@pytest.mark.parametrize("next_job", [False, True])
@pytest.mark.parametrize("worker_failure", [False, True])
def test_delayed_progress_cannot_reopen_terminal_or_overwrite_next_owner(
        tmp_path, monkeypatch, next_job, worker_failure):
    delayed, release = threading.Event(), threading.Event()
    callbacks = ThreadPoolExecutor(max_workers=1)
    pending = []
    calls = []

    def runner(*args, progress_callback, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            pending.append(callbacks.submit(progress_callback, progress("late")))
            assert delayed.wait(2)
            if worker_failure:
                raise RuntimeError("restore transport unavailable")
        return failed_item_result()

    registry = internal.ProjectRestoreJobRegistry(runner=runner)
    publish = registry._persist_result

    def delayed_publish(job_id, result, error, **kwargs):
        if kwargs.get("running"):
            delayed.set()
            assert release.wait(2)
        return publish(job_id, result, error, **kwargs)

    monkeypatch.setattr(registry, "_persist_result", delayed_publish)
    try:
        job, unused = registry.submit(request(tmp_path))
        first = wait_completed(registry, job["jobId"])
        assert first["status"] == ("failed" if worker_failure else "completed")
        if next_job:
            job, unused = registry.submit(request(tmp_path))
            assert wait_completed(registry, job["jobId"])["status"] == "completed"
        before = record(tmp_path)
        release.set()
        pending[0].result(timeout=2)
        assert record(tmp_path) == before
        assert before["jobId"] == job["jobId"]
        if worker_failure and not next_job:
            assert before["status"] == "failed"
            assert "restore transport unavailable" in before["error"]
        else:
            assert before["status"] == "finished"
            assert before["shaders"]["failed"] == 1
    finally:
        release.set()
        callbacks.shutdown()
        registry.shutdown()
