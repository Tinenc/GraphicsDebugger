from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from replaylab_agent.run_lifetime import (
    _WindowsProcess, capture_gui_owner, current_run_lifetime,
    run_lifetime, run_until_cancelled,
)


@pytest.mark.asyncio
async def test_failed_owner_observation_does_not_cancel_or_leak_context():
    class Owner:
        pid = 123
        creation_time = 456
        closed = False

        def exited(self):
            raise PermissionError("transient monitor failure")

        def close(self):
            self.closed = True

    owner = Owner()
    with run_lifetime(owner):
        assert await run_until_cancelled(asyncio.sleep(0.03, result="complete"), cancel_waiter=None, log=logging.getLogger(__name__)) == "complete"
        assert current_run_lifetime().cancellation_reason == ""
    assert owner.closed
    assert current_run_lifetime() is None


def test_ordinary_cli_and_mcp_children_are_not_owner_bound():
    assert capture_gui_owner(SimpleNamespace(prompt_stdin=False, cancel_file=Path("cancel"))) is None
    assert capture_gui_owner(SimpleNamespace(prompt_stdin=True, cancel_file=None)) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI process handle contract")
def test_frozen_owner_skips_only_matching_onefile_launcher(monkeypatch):
    import replaylab_agent.run_lifetime as lifetime

    processes = {}

    class Process:
        def __init__(self, pid):
            self.pid, self.closed = pid, False
            processes[pid] = self

        def image_path(self):
            return os.path.normcase(os.path.abspath(sys.executable))

        def close(self):
            self.closed = True

    monkeypatch.setattr(lifetime, "_WindowsProcess", Process)
    monkeypatch.setattr(lifetime.os, "getppid", lambda: 101)
    monkeypatch.setattr(lifetime, "_windows_parent_pid", lambda pid: 202 if pid == 101 else 0)
    monkeypatch.setattr(lifetime.sys, "frozen", True, raising=False)
    owner = capture_gui_owner(SimpleNamespace(prompt_stdin=True, cancel_file=Path("cancel")))
    assert owner.pid == 202
    assert processes[101].closed
    owner.close()


def _wait_file(path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"child did not publish {path.name}")


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI process handle contract")
@pytest.mark.parametrize("frozen_launcher", [False, True])
def test_real_parent_exit_cancels_run_records_reason_and_closes_provider(tmp_path: Path, frozen_launcher: bool):
    """Real owner -> optional onefile stand-in -> CLI -> real provider subprocess.

    No model/network, user capture or settings are accessed. The parent remains
    alive after normal prompt EOF, then exits abruptly without a cancel file.
    """
    root = Path(__file__).resolve().parents[1]
    worker = tmp_path / "worker.py"
    worker.write_text('''
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from replaylab_agent import cli
from replaylab_agent.agent_progress import ProgressReporter
from replaylab_agent.events import TaskEventWriter
from replaylab_agent.profiles import Profile, ProfileStore
from replaylab_agent.connectors.codex_app_server import _JsonlProcess
folder = Path(sys.argv[1])
sys.frozen = sys.argv[2] == '1'
cli._sync_bundled_unpackers_quietly = lambda: None
class Engine:
    def __init__(self, **kwargs): pass
    async def run(self, request):
        writer = TaskEventWriter(folder / 'events', request.task_id)
        reporter = ProgressReporter(writer, profile_id='local', backend='fixture')
        reporter.queued(prompt_chars=4, preset='fixture', profile_title='fixture', model='offline')
        transport = _JsonlProcess((sys.executable, '-u', '-c', 'import sys; sys.stdin.read()'), cwd=folder, env=os.environ)
        try:
            await transport.start()
            (folder / 'ready.json').write_text(json.dumps({'worker': os.getpid(), 'provider': transport.process.pid}))
            try:
                await asyncio.Future()
            finally:
                await transport.close()
        except asyncio.CancelledError:
            reporter.cancelled()
            raise
cli.DeepAgentsEngine = Engine
store = ProfileStore(folder / 'profiles.json')
store.add(Profile('local', 'ollama', 'offline'))
args = SimpleNamespace(mcp_host=None, context_file=None, live_context=False, instance_id='',
    prompt=None, prompt_stdin=True, profile='local', model='', skill='', intent_kind='',
    task_id='owner-exit', conversation_id='', cancel_file=folder / 'cancel.signal',
    engine_config=None, json=True)
try:
    asyncio.run(cli._run_once(args, store))
except asyncio.CancelledError:
    (folder / 'stopped.json').write_text(json.dumps({'cancelled': True}))
    sys.exit(130)
''', encoding="utf-8")
    launcher = tmp_path / "launcher.py"
    launcher.write_text('''
import subprocess, sys
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], '1'], stdin=sys.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sys.exit(child.wait())
''', encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text('''
import subprocess, sys, time
from pathlib import Path
folder=Path(sys.argv[1])
command=[sys.executable, sys.argv[2], sys.argv[3], str(folder)] if sys.argv[4]=='1' else [sys.executable, sys.argv[3], str(folder), '0']
child=subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open(folder/'child-stderr.log','wb'))
(folder/'child-pid').write_text(str(child.pid))
child.stdin.write(b'work'); child.stdin.close()
while True: time.sleep(.1)
''', encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root / "mcp_host"), str(Path(sys.prefix) / "Lib/site-packages")])
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    env["PYTHONIOENCODING"] = "utf-8"
    # Bypass Windows venv's additional executable redirector in this fixture;
    # the optional launcher above explicitly represents the packaged bootloader.
    python = getattr(sys, "_base_executable", sys.executable)
    owner = subprocess.Popen([python, str(parent), str(tmp_path), str(launcher), str(worker), str(int(frozen_launcher))], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    unrelated = subprocess.Popen([python, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monitored = []
    try:
        _wait_file(tmp_path / "ready.json")
        ids = json.loads((tmp_path / "ready.json").read_text())
        monitored = [_WindowsProcess(pid) for pid in set([*ids.values(), int((tmp_path / "child-pid").read_text())])]
        time.sleep(0.7)
        assert not (tmp_path / "stopped.json").exists(), "normal prompt EOF is not cancellation"
        assert all(not process.exited() for process in monitored)
        owner.terminate()
        owner.wait(timeout=5)
        try:
            _wait_file(tmp_path / "stopped.json", timeout=8)
        except AssertionError as error:
            details = (tmp_path / "events/owner-exit.jsonl").read_text()
            details += (tmp_path / "child-stderr.log").read_text(errors="replace")
            raise AssertionError(details) from error
        deadline = time.monotonic() + 5
        while any(not process.exited() for process in monitored) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert all(process.exited() for process in monitored)
        assert unrelated.poll() is None
        events = [json.loads(line) for line in (tmp_path / "events/owner-exit.jsonl").read_text().splitlines()]
        assert events[0]["data"]["ownerProcessId"] == owner.pid
        assert events[0]["data"]["ownerProcessCreationTime"] > 0
        terminals = [event for event in events if event["kind"] in {"completed", "failed", "cancelled"}]
        assert len(terminals) == 1
        assert terminals[0]["kind"] == "cancelled"
        assert terminals[0]["data"]["reason"] == "owner-process-exited"
        assert terminals[0]["summary"] != "cancelled by user"
    finally:
        if owner.poll() is None:
            owner.terminate()
        owner.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)
        for process in monitored:
            if not process.exited():
                # Exact handles captured from this fixture only; no name/global kill.
                import ctypes
                from ctypes import wintypes
                terminate = process.api.TerminateProcess
                terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
                terminate.restype = wintypes.BOOL
                handle = process.api.OpenProcess(1, False, process.pid)
                if handle:
                    terminate(handle, 1)
                    process.api.CloseHandle(handle)
            process.close()
