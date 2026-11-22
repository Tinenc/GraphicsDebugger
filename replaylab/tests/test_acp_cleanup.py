from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_windows_job_keeps_64_bit_handles_and_terminates_descendants() -> None:
    import replaylab_agent.connectors.acp as acp_connector

    job_handle = 0x1234567887654321
    process_handle = 0x2345678998765432
    closed: list[int] = []
    terminated: list[tuple[int, int]] = []
    assigned: list[tuple[int, int]] = []

    api = acp_connector._WindowsJobApi(
        create_job=lambda security, name: (
            job_handle if security is None and name is None else 0
        ),
        set_information=lambda handle, info_class, pointer, size: (
            handle == job_handle
            and info_class
            == acp_connector._WindowsJob._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
            and pointer is not None
            and size > 0
        ),
        open_process=lambda access, inherit, pid: (
            process_handle
            if access
            == (
                acp_connector._WindowsJob._PROCESS_SET_QUOTA
                | acp_connector._WindowsJob._PROCESS_TERMINATE
            )
            and inherit is False
            and pid == 4242
            else 0
        ),
        assign_process=lambda job, process: (
            assigned.append((job, process)) or True
        ),
        terminate_job=lambda handle, code: (
            terminated.append((handle, code)) or True
        ),
        close_handle=lambda handle: closed.append(handle) or True,
    )

    job = acp_connector._WindowsJob._assign_with_api(4242, api)

    assert job is not None
    assert job.handle == job_handle
    assert assigned == [(job_handle, process_handle)]
    assert closed == [process_handle]

    job.close()
    job.close()

    assert terminated == [(job_handle, 0)]
    assert closed == [process_handle, job_handle]


def test_windows_job_closes_handle_when_termination_fails() -> None:
    import replaylab_agent.connectors.acp as acp_connector

    closed: list[int] = []

    def fail_termination(handle: int, code: int) -> bool:
        del handle, code
        raise OSError("termination failed")

    api = acp_connector._WindowsJobApi(
        create_job=lambda security, name: 0,
        set_information=lambda *args: False,
        open_process=lambda *args: 0,
        assign_process=lambda *args: False,
        terminate_job=fail_termination,
        close_handle=lambda handle: closed.append(handle) or True,
    )
    job = acp_connector._WindowsJob()
    job.handle = 0x1234567887654321
    job.assigned = True
    job._api = api

    job.close()

    assert closed == [0x1234567887654321]


@pytest.mark.asyncio
async def test_managed_agent_process_closes_windows_pipe_transport_before_return(
    monkeypatch,
) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    events: list[str] = []
    monkeypatch.setattr(acp_connector, "_ACP_FINAL_PIPE_DRAIN_TIMEOUT", 0.01)

    class Stdin:
        def close(self) -> None:
            events.append("stdin.close")

        async def wait_closed(self) -> None:
            events.append("stdin.wait_closed")

    class Transport:
        def close(self) -> None:
            events.append("transport.close")

    class Process:
        pid = 4242
        returncode = 0
        stdin = Stdin()
        _transport = Transport()

        async def wait(self) -> int:
            events.append("process.wait")
            return self.returncode

        async def communicate(self):
            events.append("process.communicate.start")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("process.communicate.cancelled")

    process = Process()

    class Manager:
        async def __aenter__(self):
            return object(), process

        async def __aexit__(self, *args):
            del args
            events.append("manager.exit")
            return False

    async with acp_connector._managed_agent_process(Manager(), "test ACP"):
        events.append("body")

    assert events == [
        "body",
        "manager.exit",
        "stdin.close",
        "stdin.wait_closed",
        "process.wait",
        "process.communicate.start",
        "process.communicate.cancelled",
        "transport.close",
    ]


@pytest.mark.asyncio
async def test_managed_agent_process_escalates_the_owned_tree_before_failing(
    monkeypatch,
) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    release_cleanup = asyncio.Event()
    forced_processes: list[object] = []
    process = SimpleNamespace(pid=4242, returncode=None)

    class SlowManager:
        async def __aenter__(self):
            return object(), process

        async def __aexit__(self, *args):
            del args
            await release_cleanup.wait()
            process.returncode = -9
            return False

    async def force_tree(candidate) -> bool:
        forced_processes.append(candidate)
        release_cleanup.set()
        return True

    monkeypatch.setattr(acp_connector, "_ACP_CLEANUP_TIMEOUT", 0.02)
    monkeypatch.setattr(acp_connector, "_ACP_FORCE_CLEANUP_TIMEOUT", 0.5)
    monkeypatch.setattr(acp_connector, "_force_stop_agent_process_tree", force_tree)

    async with acp_connector._managed_agent_process(SlowManager(), "test ACP"):
        pass

    assert forced_processes == [process]
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_managed_agent_process_without_owned_pid_remains_fail_closed(
    monkeypatch,
) -> None:
    import replaylab_agent.connectors.acp as acp_connector
    from replaylab_agent.errors import LifecycleCleanupError

    cleanup_cancelled = asyncio.Event()

    class UnownedHangingManager:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *args):
            del args
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

    monkeypatch.setattr(acp_connector, "_ACP_CLEANUP_TIMEOUT", 0.02)
    with pytest.raises(LifecycleCleanupError, match="cleanup was not confirmed"):
        async with acp_connector._managed_agent_process(
            UnownedHangingManager(), "test ACP"
        ):
            pass
    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_cancellation_during_tree_escalation_waits_for_confirmed_cleanup(
    monkeypatch,
) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    force_started = asyncio.Event()
    release_force = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    process = SimpleNamespace(pid=4242, returncode=None)

    class SlowManager:
        async def __aenter__(self):
            return object(), process

        async def __aexit__(self, *args):
            del args
            await release_cleanup.wait()
            process.returncode = -9
            cleanup_finished.set()
            return False

    async def force_tree(candidate) -> bool:
        assert candidate is process
        force_started.set()
        await release_force.wait()
        release_cleanup.set()
        return True

    monkeypatch.setattr(acp_connector, "_ACP_CLEANUP_TIMEOUT", 0.02)
    monkeypatch.setattr(acp_connector, "_ACP_TREE_KILL_TIMEOUT", 0.5)
    monkeypatch.setattr(acp_connector, "_ACP_FORCE_CLEANUP_TIMEOUT", 0.5)
    monkeypatch.setattr(acp_connector, "_force_stop_agent_process_tree", force_tree)

    async def use_manager() -> None:
        async with acp_connector._managed_agent_process(SlowManager(), "test ACP"):
            pass

    operation = asyncio.create_task(use_manager())
    await asyncio.wait_for(force_started.wait(), timeout=1.0)
    operation.cancel()
    release_force.set()

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cleanup_finished.is_set()
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_connection_test_gives_replaylab_tree_escalation_the_first_deadline(
    monkeypatch,
    tmp_path,
) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    captured: dict[str, object] = {}

    class Connection:
        async def initialize(self, **kwargs):
            assert kwargs == {"protocol_version": 1}
            return {"agentInfo": {"name": "fake", "version": "1"}}

        async def new_session(self, **kwargs):
            assert kwargs == {"cwd": str(tmp_path), "mcp_servers": []}
            return SimpleNamespace(session_id="session-1", config_options=[])

    class ImmediateManager:
        async def __aenter__(self):
            return Connection(), SimpleNamespace(pid=4242, returncode=0)

        async def __aexit__(self, *args):
            del args
            return False

    def spawn_agent_process(client, *command, **kwargs):
        captured["client"] = client
        captured["command"] = command
        captured["kwargs"] = kwargs
        return ImmediateManager()

    sdk = SimpleNamespace(protocol_version=1, spawn_agent_process=spawn_agent_process)
    monkeypatch.setattr(acp_connector, "_load_acp_sdk", lambda: sdk)
    monkeypatch.setattr(acp_connector, "_make_client", lambda sdk, state: object())

    connector = acp_connector.ACPConnector(["fake-acp"], cwd=tmp_path, timeout=5)
    result = await connector.test_connection()

    assert result["sessionId"] == "session-1"
    assert captured["command"] == ("fake-acp",)
    transport = captured["kwargs"]["transport_kwargs"]
    assert transport["shutdown_timeout"] == acp_connector._ACP_TRANSPORT_SHUTDOWN_TIMEOUT
    assert (
        transport["shutdown_timeout"]
        > acp_connector._ACP_CLEANUP_TIMEOUT
        + acp_connector._ACP_TREE_KILL_TIMEOUT
        + acp_connector._ACP_FORCE_CLEANUP_TIMEOUT
    )
