"""Real, free local commands in system-temporary test directories only."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from replaylab_agent.desktop_shell import DesktopShellBackend


def python_command(source: str) -> str:
    encoded = base64.b64encode(source.encode()).decode()
    argv = [sys.executable, "-c", f"exec(__import__('base64').b64decode('{encoded}'))"]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def process_is_running(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            assert api.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not status.exists() or status.read_text().split()[2] != "Z"


def descendant_command(pid_file: Path, *, parent_wait: bool = True) -> str:
    child = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(20)"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep({20 if parent_wait else 0.3})"
    return python_command(parent)


def assert_owned_child_stopped(pid_file: Path) -> None:
    assert pid_file.exists(), "test child must actually start before cancellation/timeout"
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not process_is_running(pid), f"owned child {pid} outlived command cleanup"


def test_sdk_contract_cwd_environment_and_both_output_streams(tmp_path):
    work = tmp_path / "working directory"
    work.mkdir()
    backend = DesktopShellBackend(work, env={"BREAD_SHELL_TEST": "present"})
    result = backend.execute(python_command(
        "import os,sys; print(os.getcwd(),flush=True); print(os.environ['BREAD_SHELL_TEST'],flush=True); print('stderr-visible',file=sys.stderr)"
    ))
    assert isinstance(backend, LocalShellBackend)
    assert isinstance(result, ExecuteResponse)
    assert result.exit_code == 0, result.output
    assert str(work) in result.output
    assert "present" in result.output and "stderr-visible" in result.output
    assert backend.virtual_mode is False


def test_combined_output_is_bounded_while_process_keeps_draining(tmp_path):
    backend = DesktopShellBackend(tmp_path, max_output_bytes=1024)
    result = backend.execute(python_command(
        "import os; os.write(1,b'x'*200000); os.write(2,b'y'*200000)"
    ))
    assert result.exit_code == 0, result.output
    assert result.truncated
    assert len(result.output.encode()) <= 1024


def test_utf8_replacement_and_error_text_respect_total_byte_cap(tmp_path):
    backend = DesktopShellBackend(tmp_path, max_output_bytes=5)
    result = backend.execute(python_command("import os; os.write(1,b'\\xff'*50)"))
    assert result.truncated
    assert len(result.output.encode()) <= 5
    assert len(backend.execute("").output.encode()) <= 5


def test_nonzero_exit_code_and_stdin_eof(tmp_path):
    result = DesktopShellBackend(tmp_path).execute(python_command(
        "import sys; print(repr(sys.stdin.read())); sys.exit(7)"
    ))
    assert result.exit_code == 7
    assert "''" in result.output


def test_timeout_stops_owned_descendants(tmp_path):
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    result = DesktopShellBackend(tmp_path).execute(descendant_command(pid_file), timeout=1)
    assert result.exit_code == 124, result.output
    assert "timed out" in result.output
    assert time.monotonic() - started < 6
    assert_owned_child_stopped(pid_file)


def test_async_cancellation_waits_for_owned_process_tree_cleanup(tmp_path):
    pid_file = tmp_path / "async-child.pid"

    async def run():
        backend = DesktopShellBackend(tmp_path)
        task = asyncio.create_task(backend.aexecute(descendant_command(pid_file)))
        try:
            for _ in range(250):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.02)
            assert pid_file.exists(), "child startup timed out"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert_owned_child_stopped(pid_file)


def test_normal_completion_reclaims_background_descendants(tmp_path):
    pid_file = tmp_path / "background-child.pid"
    result = DesktopShellBackend(tmp_path).execute(descendant_command(pid_file, parent_wait=False))
    assert result.exit_code == 0, result.output
    assert_owned_child_stopped(pid_file)


def test_virtual_file_routing_is_not_a_shell_sandbox(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    evidence = tmp_path / "outside.txt"
    evidence.write_text("real host access")
    result = DesktopShellBackend(work, virtual_mode=True).execute(python_command(
        f"from pathlib import Path; print(Path({str(evidence)!r}).read_text())"
    ))
    assert result.exit_code == 0
    assert "real host access" in result.output


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_is_rejected_before_launch(tmp_path, value):
    backend = DesktopShellBackend(tmp_path)
    with pytest.raises(ValueError, match="finite and positive"):
        backend.execute("echo should-not-run", timeout=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_output_budget(tmp_path, value):
    with pytest.raises(ValueError, match="positive integer"):
        DesktopShellBackend(tmp_path, max_output_bytes=value)


@pytest.mark.skipif(os.name != "nt", reason="Windows job containment")
def test_windows_command_stays_suspended_if_containment_fails(tmp_path):
    touched = tmp_path / "must-not-exist.txt"
    with mock.patch("replaylab_agent.connectors.acp._WindowsJob.assign", return_value=None), mock.patch(
        "replaylab_agent.desktop_shell.subprocess.Popen", wraps=subprocess.Popen
    ) as launch:
        result = DesktopShellBackend(tmp_path).execute(python_command(
            f"from pathlib import Path; Path({str(touched)!r}).write_text('wrong')"
        ))
    flags = launch.call_args.kwargs["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & 0x00000004
    assert result.exit_code == 1
    assert "not started" in result.output
    assert not touched.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd quoting")
def test_windows_quoted_executable_and_arguments_keep_native_shell_semantics(tmp_path):
    command_file = tmp_path / "command with spaces.cmd"
    command_file.write_text("@echo off\necho first=%1\necho second=%2\n")
    result = DesktopShellBackend(tmp_path).execute(f'"{command_file}" "a & b" "two words"')
    assert result.exit_code == 0, ascii(result.output)
    assert 'first="a & b"' in result.output and 'second="two words"' in result.output
