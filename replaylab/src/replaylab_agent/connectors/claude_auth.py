"""Bounded, non-interactive Claude account status in one managed profile.

Only the official CLI reads credentials. Raw stdout/stderr, account metadata and
tokens never leave this module or appear in its diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..errors import AuthenticationError, ConfigurationError
from ..process_env import external_agent_environment, resolve_known_vendor_executable
from ..profiles import Profile

MAX_AUTH_STATUS_BYTES = 64 * 1024
MAX_AUTH_STATUS_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    logged_in: bool
    state: str
    detail: str
    message_id: str
    message_args: tuple[str, ...] = ()


def _status(state: str) -> ClaudeAuthStatus:
    if state == "authenticated":
        return ClaudeAuthStatus(
            True, state,
            "Claude is signed in for this connection; model request access has not been tested.",
            "subscription.claude-account-signed-in",
        )
    if state == "unauthenticated":
        return ClaudeAuthStatus(
            False, state,
            "This Claude connection is not signed in. Use Save and Connect to sign in to its isolated profile.",
            "subscription.claude-account-unauthenticated",
        )
    return ClaudeAuthStatus(
        False, state, "Claude sign-in status could not be verified: " + state,
        "subscription.claude-auth-status-failed", (state,),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_status(stdout: bytes, exit_code: int) -> ClaudeAuthStatus:
    try:
        value = json.loads(stdout.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return _status("invalid-response")
    if not isinstance(value, dict) or type(value.get("loggedIn")) is not bool:
        return _status("invalid-response")
    # The CLI uses exit 1 for a valid logged-out status. No other failure exit
    # may be promoted to success, even if the output happens to claim loggedIn.
    if value["loggedIn"] is True:
        return _status("authenticated" if exit_code == 0 else "process-failed")
    return _status("unauthenticated" if exit_code in (0, 1) else "process-failed")


class _OutputLimit(Exception):
    pass


async def check_claude_auth_status(
    profile: Profile,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    cwd: Path | str | None = None,
) -> ClaudeAuthStatus:
    """Read ``claude auth status --json`` without login or model generation.

    Failures return a closed set of diagnostic codes, never vendor output.
    Cancellation propagates after the owned process tree is stopped.
    """
    if profile.preset != "claude-code-acp":
        raise ConfigurationError("Claude account status requires a Claude ACP profile")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigurationError("Claude account status timeout must be positive and finite")
    resolved = resolve_known_vendor_executable(profile.preset, "claude", environ=environ)
    if not resolved:
        return _status("cli-unavailable")
    try:
        environment = external_agent_environment(profile, environ=environ)
    except (AuthenticationError, ConfigurationError):
        return _status("process-failed")

    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task] = []
    output_bytes = 0

    async def read_output(stream: asyncio.StreamReader, *, retain: bool) -> bytes:
        nonlocal output_bytes
        output = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return bytes(output)
            output_bytes += len(chunk)
            if output_bytes > MAX_AUTH_STATUS_BYTES:
                raise _OutputLimit()
            if retain:
                output.extend(chunk)

    async def run() -> ClaudeAuthStatus:
        nonlocal process
        kwargs = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": environment,
            "limit": MAX_AUTH_STATUS_BYTES,
        }
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        # Match the task's project-only settings when checking its fresh
        # workspace. A standalone doctor has no task workspace, so it reads no
        # settings sources. Neither path may execute an ambient apiKeyHelper.
        settings_sources = "project" if cwd is not None else ""
        process = await asyncio.create_subprocess_exec(
            resolved, "--setting-sources", settings_sources,
            "auth", "status", "--json", **kwargs)
        tasks.extend([
            asyncio.create_task(read_output(process.stdout, retain=True)),
            asyncio.create_task(read_output(process.stderr, retain=False)),
            asyncio.create_task(process.wait()),
        ])
        stdout, unused_stderr, exit_code = await asyncio.gather(*tasks)
        return _parse_status(stdout, exit_code)

    try:
        return await asyncio.wait_for(run(), timeout=min(float(timeout), MAX_AUTH_STATUS_SECONDS))
    except asyncio.TimeoutError:
        return _status("timed-out")
    except _OutputLimit:
        return _status("output-limit")
    except OSError:
        return _status("process-failed")
    finally:
        if process is not None and process.returncode is None:
            # Reuse the existing subscription tree cleanup, including Windows
            # .cmd launchers. Import lazily to keep the shared helper reusable.
            from .subscription import _terminate_async_process_tree

            await _terminate_async_process_tree(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["ClaudeAuthStatus", "check_claude_auth_status"]
