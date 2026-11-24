"""Project-owned persistent Python connection for an interactive stock UE Editor."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from .resource_export import _atomic_write_bytes, _atomic_write_json
from .unity_restore import _file, _not_redirected, _sha

PROTOCOL = "bread-live-unreal/v1"


def _script():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "bread_unreal/unreal_live_editor.py"
    return Path(__file__).with_name("unreal_live_editor.py")


def _process(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("Invalid live Editor PID")
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
               f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | "
               "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    raw = subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(raw.decode("utf-8")) if raw.strip() else None


def _matches(session):
    process = _process(session["pid"])
    if not process:
        return False
    executable = Path(process.get("ExecutablePath") or "")
    command = (process.get("CommandLine") or "").replace("\\", "/").casefold()
    return (executable == Path(session["editor"])
            and str(Path(session["project"])).replace("\\", "/").casefold() in command
            and str(Path(session["script"])).replace("\\", "/").casefold() in command)


def discover(project, editor):
    root = project.parent / "Saved/Bread/LiveEditor"
    _not_redirected(root)
    current = root / "current.json"
    if not current.is_file():
        return None
    saved = json.loads(_file(root, "current.json").read_bytes())
    nonce = saved.get("nonce", "")
    if not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise ValueError("Invalid live Editor session identity")
    directory = root / nonce
    ready = json.loads(_file(directory, "ready.json").read_bytes())
    if (ready.get("schema") != PROTOCOL or ready.get("nonce") != nonce
            or Path(ready.get("project", "")) != project
            or Path(ready.get("editor", "")) != editor.with_name("UnrealEditor.exe")
            or Path(ready.get("script", "")) != directory / "bridge.py"
            or ready.get("scriptSha256") != _sha(_file(directory, "bridge.py"))):
        raise ValueError("Live Editor session does not identify the selected project and bridge")
    return {**ready, "directory": str(directory)} if _matches(ready) else None



def _shell_launch(arguments, directory):
    """Ask the user's running Explorer to own the long-lived interactive window."""
    command = r"""
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
$data=ConvertFrom-Json ([Console]::In.ReadToEnd())
$windows=(New-Object -ComObject Shell.Application).Windows()
$application=$null
foreach ($window in $windows) {
    if ([IO.Path]::GetFileName($window.FullName) -ieq 'explorer.exe') {
        $application=$window.Document.Application
        break
    }
}
if ($null -eq $application) {
    $handle=0
    $desktop=$windows.FindWindowSW(0,0,8,[ref]$handle,1)
    if ($null -ne $desktop) { $application=$desktop.Document.Application }
}
if ($null -eq $application) { throw 'The interactive Explorer shell is unavailable' }
$application.ShellExecute($data.executable,$data.arguments,$data.directory,'open',1)
"""
    data = {"executable": arguments[0], "arguments": subprocess.list2cmdline(arguments[1:]),
            "directory": str(directory)}
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        input=json.dumps(data), text=True, encoding="utf-8", capture_output=True,
        check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)


def launch(project, info):
    from .unreal_integration import _enable_plugins, _project_editor_running, _restore_descriptor, PLUGIN_NAMES

    editor = Path(info["editor"])
    session = discover(project, editor)
    if session:
        return session
    if _project_editor_running(project):
        raise ValueError("This project is already open without Bread's live connection. Preserve its edits "
                         "before reopening it through the interactive connection.")
    nonce = uuid4().hex
    root = project.parent / "Saved/Bread/LiveEditor"
    directory = root / nonce
    _not_redirected(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "requests").mkdir()
    script = directory / "bridge.py"
    _atomic_write_bytes(script, _script().read_bytes())
    startup = {"schema": PROTOCOL, "nonce": nonce, "project": str(project),
               "editor": str(editor.with_name("UnrealEditor.exe")), "script": str(script)}
    _atomic_write_json(directory / "startup.json", startup)
    original, configured = _enable_plugins(project, PLUGIN_NAMES)
    try:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
        bootstrap = (f"import os\nos.environ['BREAD_UNREAL_LIVE_REQUEST']={str(directory / 'startup.json')!r}\n"
                     f"__file__={str(script)!r}\n__name__='__main__'\n"
                     "exec(compile(open(__file__,encoding='utf-8').read(),__file__,'exec'))")
        encoded = base64.b64encode(bootstrap.encode("utf-8")).decode("ascii")
        arguments = [startup["editor"], str(project), "-NoSplash", "-NoSourceControl",
                     f"-ExecCmds=py exec(__import__('base64').b64decode('{encoded}'))",
                     f"-BreadLiveScript={script}", f"-abslog={directory / 'editor.log'}"]
        try:
            process = subprocess.Popen(arguments, cwd=project.parent, creationflags=flags,
                                       env={**os.environ, "BREAD_UNREAL_LIVE_REQUEST": str(directory / "startup.json")})
            launch_method = "direct"
        except OSError as error:
            if getattr(error, "winerror", None) != 5:
                raise
            _shell_launch(arguments, project.parent)
            process = None
            launch_method = "explorer-shell"
        deadline = time.monotonic() + 600
        while not (directory / "ready.json").is_file():
            if process is not None and process.poll() is not None:
                raise ValueError(f"Interactive UE Editor exited. See {directory / 'editor.log'}")
            if time.monotonic() >= deadline:
                raise ValueError(f"Interactive UE Editor is still starting. Inspect {directory} before retrying.")
            time.sleep(.5)
        ready = json.loads(_file(directory, "ready.json").read_bytes())
        if (ready.get("nonce") != nonce or (process is not None and ready.get("pid") != process.pid)
                or not _matches(ready)):
            raise ValueError("Interactive UE Editor startup receipt identity mismatch")
        _atomic_write_json(directory / "launch.json", {"method": launch_method, "pid": ready["pid"]})
        _atomic_write_json(root / "current.json", {"schema": PROTOCOL, "nonce": nonce})
    finally:
        _restore_descriptor(project, original, configured)
    session = discover(project, editor)
    if session is None:
        raise ValueError("Interactive UE Editor exited during connection setup")
    return session


def execute(project, session, job_id, code):
    directory = Path(session["directory"])
    request = {"schema": PROTOCOL, "nonce": session["nonce"], "jobId": job_id, "code": code}
    response = directory / "requests" / (job_id + ".response.json")
    _not_redirected(response)
    _atomic_write_json(directory / "requests" / (job_id + ".request.json"), request)
    last_probe = time.monotonic()
    while not response.is_file():
        if time.monotonic() - last_probe > 5:
            if not _matches(session):
                raise ValueError(f"Interactive UE Editor exited. Outcome may be incomplete. Inspect {response.parent / (job_id + '.executing.json')}")
            last_probe = time.monotonic()
        time.sleep(.2)
    if response.stat().st_size > 1024 * 1024:
        raise ValueError("Live Editor response exceeds the byte limit")
    result = json.loads(_file(response.parent, response.name).read_bytes())
    if (result.get("nonce") != session["nonce"] or result.get("jobId") != job_id
            or result.get("pid") != session["pid"] or Path(result.get("project", "")) != project):
        raise ValueError("Live Editor response belongs to another project or request")
    result.pop("nonce")
    return {**result, "provider": "bread-editor-python", "transport": "interactive-editor",
            "receiptPath": str(response), "logPath": str(directory / "editor.log")}
