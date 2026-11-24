"""ACP terminal lifecycle backed by explicitly enabled local execution."""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Mapping


class ACPTerminals:
    def __init__(self, cwd: Path, environment: Mapping[str, str]) -> None:
        self.cwd = cwd
        self.environment = dict(environment)
        self.tasks: dict[str, asyncio.Task] = {}

    async def create(self, command: str, args=(), cwd: str | None = None, env=(), output_byte_limit: int | None = None) -> dict:
        from .desktop_shell import DesktopShellBackend
        if len(self.tasks) >= 8:
            raise ValueError("Release an existing terminal before starting another")
        directory = Path(cwd).resolve() if cwd else self.cwd
        if not directory.is_dir():
            raise ValueError("Terminal cwd does not exist")
        environment = dict(self.environment)
        for item in env or ():
            name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
            value = item.get("value") if isinstance(item, dict) else getattr(item, "value", "")
            if name:
                environment[str(name)] = str(value)
        argv = [str(command), *(str(arg) for arg in (args or ()))]
        if os.name == "nt":
            # ACP supplies executable + argv, not a shell fragment. A literal
            # encoded PowerShell call preserves &, %, quotes and spaces without
            # cmd.exe expanding them before the child receives its arguments.
            script = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv) + "; exit $LASTEXITCODE"
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            line = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
        else:
            line = shlex.join(argv)
        backend = DesktopShellBackend(root_dir=directory, virtual_mode=True, timeout=120,
            max_output_bytes=min(max(int(output_byte_limit or 100_000), 1024), 100_000),
            env=environment, inherit_env=False)
        identity = uuid.uuid4().hex
        self.tasks[identity] = asyncio.create_task(backend.aexecute(line))
        return {"terminalId": identity}

    def _task(self, identity: str) -> asyncio.Task:
        if identity not in self.tasks:
            raise ValueError("Unknown or released terminal")
        return self.tasks[identity]

    async def output(self, identity: str) -> dict:
        task = self._task(identity)
        if not task.done():
            return {"output": "", "truncated": False}
        if task.cancelled():
            return {"output": "", "truncated": False, "exitStatus": {"exitCode": 130}}
        result = task.result()
        return {"output": result.output, "truncated": result.truncated, "exitStatus": {"exitCode": result.exit_code}}

    async def wait(self, identity: str) -> dict:
        try:
            result = await asyncio.shield(self._task(identity))
        except asyncio.CancelledError:
            if not self._task(identity).cancelled():
                raise
            return {"exitCode": 130}
        return {"exitCode": result.exit_code}

    async def kill(self, identity: str) -> dict:
        task = self._task(identity)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return {}

    async def release(self, identity: str) -> dict:
        await self.kill(identity)
        self.tasks.pop(identity, None)
        return {}

    async def close(self) -> None:
        await asyncio.gather(*(self.kill(identity) for identity in list(self.tasks)), return_exceptions=True)
        self.tasks.clear()
