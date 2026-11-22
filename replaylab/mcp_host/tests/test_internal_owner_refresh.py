from __future__ import annotations

import ast
import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn

from src import internal_host


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


async def _wait_async(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


class _BlockingService:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.calls = []
        self.worker_ids = []
        self.fail = False
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    def refresh_shader_sessions(self, owners):
        with self.lock:
            self.calls.append(list(owners))
            self.worker_ids.append(threading.get_ident())
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        self.entered.set()
        try:
            if not self.release.wait(10.0):
                raise AssertionError("test did not release owner reconciliation")
            if self.fail:
                raise RuntimeError("unavailable mailbox: " + "x" * 4096)
            return {"attempted": True, "results": []}
        finally:
            with self.lock:
                self.active -= 1
            self.completed.set()


def _actual_monitor(tmp_path, refresh, publish_state):
    """Run production's nested monitor without opening a server or real config."""
    tree = ast.parse(Path(internal_host.__file__).read_text(encoding="utf-8"))
    serve = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_serve")
    monitor = copy.deepcopy(next(node for node in serve.body
                                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "monitor"))
    factory = ast.parse("def make_monitor():\n    empty_since = None\n").body[0]
    factory.body.extend([monitor, ast.Return(value=ast.Name(id="monitor", ctx=ast.Load()))])
    module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    scope = dict(vars(internal_host))
    server_box = {}
    scope.update({
        "root": tmp_path,
        "paths": {"shutdown": tmp_path / "shutdown"},
        "live_owners": lambda _root: [101],
        "publish_state": publish_state,
        "shader_session_refresh": refresh,
        "server_box": server_box,
    })
    exec(compile(module, str(internal_host.__file__), "exec"), scope)
    return scope["make_monitor"](), server_box


def test_actual_uvicorn_owner_ticks_keep_heartbeats_fresh_during_five_second_refresh(tmp_path):
    service = _BlockingService()
    refresh = internal_host._OwnerShaderSessionRefresh(service)
    heartbeats = []
    monitor, server_box = _actual_monitor(
        tmp_path, refresh, lambda: heartbeats.append(time.monotonic()),
    )
    config = uvicorn.Config(
        app=None, log_config=None, date_header=False,
        callback_notify=monitor, timeout_notify=1,
    )
    # Normally Config.load populates these before on_tick. No socket, app,
    # transport or lifespan is needed to exercise Uvicorn's real notify loop.
    config.encoded_headers = []
    server = uvicorn.Server(config)
    server_box["server"] = server

    async def exercise_ticks():
        assert await asyncio.wait_for(server.on_tick(0), timeout=0.5) is False
        await _wait_async(service.entered.is_set)
        started = time.monotonic()
        counter = 0
        # Exceed the actual Host state liveness budget with the one refresh
        # still blocked, while exercising on_tick's own one-second throttling.
        while time.monotonic() - started < 5.35:
            await asyncio.sleep(0.1)
            counter += 1
            assert await asyncio.wait_for(server.on_tick(counter), timeout=0.5) is False
        assert service.release.is_set() is False
        assert service.active == 1
        assert service.calls == [[101]]
        assert len(heartbeats) >= 5
        assert time.monotonic() - heartbeats[0] > internal_host.MAX_HEARTBEAT_AGE_MS / 1000
        assert time.monotonic() - heartbeats[-1] < 2.0
        assert max(second - first for first, second in zip(heartbeats, heartbeats[1:])) < 2.0

    try:
        asyncio.run(exercise_ticks())
    finally:
        service.release.set()
        refresh.close()
    assert service.maximum_active == 1


def test_busy_owner_refresh_does_not_queue_repeated_ticks_and_can_run_next_batch():
    service = _BlockingService()
    refresh = internal_host._OwnerShaderSessionRefresh(service)
    owners = [101]
    try:
        assert refresh.tick([]) is False
        assert refresh.tick(owners) is True
        owners.append(202)
        assert service.entered.wait(2.0)
        for _ in range(30):
            assert refresh.tick([303]) is False
        assert service.calls == [[101]]
        assert service.maximum_active == 1
        service.release.set()
        _wait_until(lambda: refresh.tick([404]))
        _wait_until(lambda: len(service.calls) == 2)
        refresh.close()
        assert service.calls == [[101], [404]]
        assert service.maximum_active == 1
        assert refresh.tick([505]) is False
    finally:
        service.release.set()
        refresh.close()


def test_owner_refresh_is_independent_of_the_saturated_http_default_executor():
    service = _BlockingService()
    refresh = internal_host._OwnerShaderSessionRefresh(service)
    default_entered = threading.Event()
    default_release = threading.Event()
    default_worker_ids = []

    def block_http_worker():
        default_worker_ids.append(threading.get_ident())
        default_entered.set()
        assert default_release.wait(3.0)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocked = loop.run_in_executor(None, block_http_worker)
        try:
            await _wait_async(default_entered.is_set)
            assert refresh.tick([101]) is True
            await _wait_async(service.entered.is_set)
            assert not default_release.is_set()
            assert service.calls == [[101]]
            assert service.worker_ids[0] != default_worker_ids[0]
        finally:
            default_release.set()
            service.release.set()
            await blocked

    try:
        asyncio.run(exercise())
    finally:
        default_release.set()
        service.release.set()
        refresh.close()


def test_close_drains_running_refresh_before_return_and_permanently_rejects_ticks():
    service = _BlockingService()
    refresh = internal_host._OwnerShaderSessionRefresh(service)
    close_returned = threading.Event()
    errors = []

    def close():
        try:
            refresh.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_returned.set()

    closer = threading.Thread(target=close)
    try:
        assert refresh.tick([101]) is True
        assert service.entered.wait(2.0)
        closer.start()
        _wait_until(lambda: refresh._closed)
        assert not close_returned.wait(0.1)
        assert refresh.tick([202]) is False
        assert service.active == 1
        service.release.set()
        closer.join(2.0)
        assert not closer.is_alive()
        assert not errors
        assert close_returned.is_set()
        assert service.completed.is_set()
        assert service.active == 0
        assert refresh.tick([303]) is False
        assert service.calls == [[101]]
    finally:
        service.release.set()
        if closer.ident is not None:
            closer.join(2.0)
        refresh.close()


def test_refresh_failure_is_bounded_and_does_not_prevent_later_ticks_or_close(caplog):
    service = _BlockingService()
    service.fail = True
    service.release.set()
    refresh = internal_host._OwnerShaderSessionRefresh(service)
    try:
        assert refresh.tick([101]) is True
        assert service.completed.wait(2.0)
        _wait_until(lambda: refresh.tick([202]))
        _wait_until(lambda: len(service.calls) == 2)
        refresh.close()
        assert len(service.calls) == 2
        messages = [record.getMessage() for record in caplog.records
                    if record.name == internal_host.__name__]
        assert len(messages) == 2
        assert all("RuntimeError" in message and "unavailable mailbox" in message for message in messages)
        assert all(len(message) < 1200 for message in messages)
        assert refresh.tick([303]) is False
    finally:
        refresh.close()


def test_host_finally_drains_owner_refresh_before_service_shutdown_and_state_removal():
    tree = ast.parse(Path(internal_host.__file__).read_text(encoding="utf-8"))
    serve = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_serve")
    cleanup = next(node for node in serve.body if isinstance(node, ast.Try) and node.finalbody)
    calls = [ast.unparse(node) for statement in cleanup.finalbody
             for node in ast.walk(statement) if isinstance(node, ast.Call)]
    drain = calls.index("shader_session_refresh.close()")
    shutdown = calls.index("tool_service.shutdown()")
    remove_state = calls.index("paths['state'].unlink()")
    assert drain < shutdown < remove_state
