"""Bounded desktop shell execution using the DeepAgents backend contract.

This is a local OS shell, not a filesystem or network sandbox. ``root_dir`` is
the working directory; ``virtual_mode`` only changes SDK file-tool routing.
The caller must check the user's native-tool policy before providing this
backend to an agent. Windows commands use cmd.exe, POSIX commands use /bin/sh.
Owned background descendants are stopped when the command completes.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse
from .filesystem_backend import Utf8GrepMixin


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume only after the suspended shell belongs to its kill-on-close job.

Popen closes the primary thread handle, so NtResumeProcess resumes the owned
process through Popen's retained process handle instead of racing its startup.
"""
    resume = ctypes.WinDLL("ntdll").NtResumeProcess
    resume.argtypes = [ctypes.c_void_p]
    resume.restype = ctypes.c_long
    status = resume(int(process._handle))  # type: ignore[attr-defined]
    if status < 0:
        raise OSError(f"Cannot resume contained shell process (NTSTATUS {status:#x})")


class DesktopShellBackend(Utf8GrepMixin,LocalShellBackend):
    """SDK local file tools plus an output-bounded, cancellable native shell."""

    def __init__(
        self,
        root_dir: str | Path | None = None,
        *,
        virtual_mode: bool = False,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        inherit_env: bool = True,
    ) -> None:
        self._validate_timeout(timeout)
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        super().__init__(
            root_dir=root_dir, virtual_mode=virtual_mode, timeout=timeout,
            max_output_bytes=max_output_bytes, env=env, inherit_env=inherit_env,
        )
        if not self.cwd.is_dir():
            raise ValueError("root_dir must be an existing directory")

    @staticmethod
    def _validate_timeout(timeout: int | float) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        return float(timeout)

    def _response(self, output: bytes, code: int, truncated: bool) -> ExecuteResponse:
        # Invalid native bytes can expand during UTF-8 replacement; cap the
        # final response as well as the raw capture buffer.
        encoded = output.decode("utf-8", "replace").encode("utf-8")
        clipped = len(encoded) > self._max_output_bytes
        return ExecuteResponse(
            output=encoded[:self._max_output_bytes].decode("utf-8", "ignore"),
            exit_code=code,
            truncated=truncated or clipped,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._execute(command, timeout=timeout, cancel=threading.Event())

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        cancel = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            self._execute, command, timeout=timeout, cancel=cancel,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel.set()
            # Cancelling a to_thread future does not stop its subprocess. Wait
            # for bounded tree cleanup before acknowledging cancellation.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            if not worker.cancelled():
                worker.exception()  # consume any concurrent launch failure
            raise

    def _execute(
        self, command: str, *, timeout: int | None, cancel: threading.Event,
    ) -> ExecuteResponse:
        duration = self._validate_timeout(self._default_timeout if timeout is None else timeout)
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            return self._response(b"Error: Command must be a non-empty string without NUL bytes.", 1, False)
        if cancel.is_set():
            return self._response(b"Command cancelled.", 130, False)

        process: subprocess.Popen[bytes] | None = None
        job: Any = None
        reader: threading.Thread | None = None
        captured = bytearray()
        truncated = False
        timed_out = False
        read_error: str | None = None
        launched_at = time.monotonic()
        try:
            options: dict[str, Any] = {
                "cwd": str(self.cwd), "env": self._env, "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "bufsize": 0,
            }
            if os.name == "nt":
                # Ignore a task-controlled COMSPEC. Do not start an uncontained
                # shell when Windows refuses to assign the process to a job.
                system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
                shell = subprocess.list2cmdline([str(system_root / "System32" / "cmd.exe")])
                # cmd's /s /c quote handling differs from the CRT argv parser:
                # list2cmdline on `command` would turn its quotes into literal
                # backslashes and break executable paths containing spaces.
                argv: Any = f'{shell} /d /s /c "{command}"'
                options["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
            else:
                argv = ["/bin/sh", "-c", command]
                options["start_new_session"] = True
            process = subprocess.Popen(argv, **options)
            if os.name == "nt":
                from .connectors.acp import _WindowsJob
                job = _WindowsJob.assign(process.pid)
                if job is None:
                    raise OSError("Cannot contain desktop shell process tree; command was not started")
                _resume_windows_process(process)

            def read_output() -> None:
                nonlocal truncated, read_error
                assert process is not None and process.stdout is not None
                try:
                    while chunk := process.stdout.read(65_536):
                        remaining = self._max_output_bytes - len(captured)
                        captured.extend(chunk[:remaining])
                        truncated |= len(chunk) > remaining
                except (OSError, ValueError) as exc:
                    read_error = str(exc)

            reader = threading.Thread(target=read_output, name="bread-desktop-shell-output", daemon=True)
            reader.start()
            while process.poll() is None:
                if cancel.is_set():
                    break
                if time.monotonic() - launched_at >= duration:
                    timed_out = True
                    break
                cancel.wait(0.02)
        except (OSError, ValueError) as exc:
            return self._response(f"Error executing command: {exc}".encode("utf-8", "replace"), 1, False)
        finally:
            if process is not None:
                if job is not None:
                    job.close()
                elif os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if reader is not None:
                    reader.join(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

        if cancel.is_set():
            return self._response(b"Command cancelled.\n" + captured, 130, truncated)
        if timed_out:
            return self._response(f"Command timed out after {duration:g} seconds.\n".encode() + captured, 124, truncated)
        if read_error:
            return self._response(f"Error reading command output: {read_error}\n".encode() + captured, 1, truncated)
        assert process is not None
        return self._response(bytes(captured) or b"<no output>", process.returncode, truncated)


__all__ = ["DesktopShellBackend"]
