from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import runpy
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src import unreal_live_connection as live


def prepared_bridge(tmp_path, monkeypatch):
    project = tmp_path / "Demo.uproject"
    project.write_text("{}")
    nonce = uuid4().hex
    directory = tmp_path / "Saved/Bread/LiveEditor" / nonce
    directory.mkdir(parents=True)
    (directory / "requests").mkdir()
    script = directory / "bridge.py"
    script.write_bytes(Path(live.__file__).with_name("unreal_live_editor.py").read_bytes())
    editor = tmp_path / "Engine/Binaries/Win64/UnrealEditor.exe"
    startup = {"schema": live.PROTOCOL, "nonce": nonce, "project": str(project),
               "editor": str(editor), "script": str(script)}
    (directory / "startup.json").write_text(json.dumps(startup))
    monkeypatch.setenv("BREAD_UNREAL_LIVE_REQUEST", str(directory / "startup.json"))
    callbacks = []
    ue = SimpleNamespace(
        Paths=SimpleNamespace(get_project_file_path=lambda: str(project),
                              convert_relative_path_to_full=lambda value: value),
        register_slate_post_tick_callback=lambda callback: callbacks.append(callback) or len(callbacks),
        counter=0)
    monkeypatch.setitem(sys.modules, "unreal", ue)
    runpy.run_path(str(script), run_name="__main__")
    (directory.parent / "current.json").write_text(json.dumps({"schema": live.PROTOCOL, "nonce": nonce}))
    monkeypatch.setattr(live, "_process", lambda pid: {
        "ProcessId": os.getpid(), "ExecutablePath": str(editor),
        "CommandLine": f'"{editor}" "{project}" -ExecCmds=py "{script}"'})
    return project, editor, directory, ue, callbacks[0]


def execute_one(project, session, directory, callback, code):
    job_id = uuid4().hex
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(live.execute, project, session, job_id, code)
        path = directory / "requests" / (job_id + ".request.json")
        deadline = time.monotonic() + 3
        while not path.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        callback(.016)
        result = pending.result(timeout=3)
    callback(.016)
    assert (directory / "requests" / (job_id + ".executing.json")).is_file()
    return result


def test_live_connection_reconnects_and_runs_once_on_editor_thread(tmp_path, monkeypatch):
    project, editor, directory, ue, callback = prepared_bridge(tmp_path, monkeypatch)
    session = live.discover(project, editor.with_name("UnrealEditor-Cmd.exe"))
    assert session["pid"] == os.getpid()
    result = execute_one(project, session, directory, callback,
                         "unreal.counter += 1\nresult={'count': unreal.counter}")
    assert result["status"] == "executed" and result["result"] == {"count": 1}
    assert ue.counter == 1
    assert live.discover(project, editor)["nonce"] == session["nonce"]


def test_live_connection_preserves_failure_and_stays_usable(tmp_path, monkeypatch):
    project, editor, directory, ue, callback = prepared_bridge(tmp_path, monkeypatch)
    session = live.discover(project, editor)
    result = execute_one(project, session, directory, callback,
                         "unreal.counter += 1\nraise ValueError('intentional failure')")
    assert result["status"] == "failed" and "intentional failure" in result["diagnostic"]
    result = execute_one(project, session, directory, callback, "result=unreal.counter")
    assert result["result"] == 1


def test_live_connection_rejects_foreign_process_and_changed_script(tmp_path, monkeypatch):
    project, editor, directory, ue, callback = prepared_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_process", lambda pid: {
        "ExecutablePath": str(editor), "CommandLine": str(tmp_path / "Other.uproject")})
    assert live.discover(project, editor) is None
    (directory / "bridge.py").write_text("unexpected source")
    with pytest.raises(ValueError, match="does not identify"):
        live.discover(project, editor)


def test_live_bridge_does_not_execute_another_sessions_request(tmp_path, monkeypatch):
    project, editor, directory, ue, callback = prepared_bridge(tmp_path, monkeypatch)
    job_id = uuid4().hex
    request = {"jobId": job_id, "nonce": uuid4().hex, "code": "unreal.counter += 1"}
    (directory / "requests" / (job_id + ".request.json")).write_text(json.dumps(request))
    callback(.016)
    assert ue.counter == 0
    assert not (directory / "requests" / (job_id + ".response.json")).exists()


def test_live_bridge_rejects_non_json_result_without_losing_connection(tmp_path, monkeypatch):
    project, editor, directory, ue, callback = prepared_bridge(tmp_path, monkeypatch)
    session = live.discover(project, editor)
    result = execute_one(project, session, directory, callback, "result=float('nan')")
    assert result["status"] == "failed"
    assert execute_one(project, session, directory, callback, "result=True")["result"] is True
def test_unreal_service_uses_live_provider_without_batch_or_foreground(tmp_path, monkeypatch):
    from src.unreal_integration import UnrealService
    project, editor, directory, ue, callback = prepared_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("BREAD_UNREAL_EDITOR_PYTHON", "1")
    service = UnrealService()
    info = {"editor": str(editor.with_name("UnrealEditor-Cmd.exe")),
            "nativeMcpAvailable": False}
    connected = service._connect(project, info)
    assert connected["transport"] == "interactive-editor"
    monkeypatch.setattr(live, "execute", lambda *args: {"status": "executed", "result": "live"})
    assert service._run_editor_python(project, {**info, "jobId": uuid4().hex}, "result='live'")["result"] == "live"
def test_launch_uses_explorer_when_parent_job_disallows_breakaway(tmp_path, monkeypatch):
    from src import unreal_integration
    project=tmp_path/"Demo.uproject"
    project.write_text("{}")
    editor=tmp_path/"Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
    monkeypatch.setattr(unreal_integration,"_project_editor_running",lambda unused:False)
    calls=[]
    def refused(*args,**kwargs):
        error=PermissionError("Parent job denies breakaway")
        error.winerror=5
        raise error
    monkeypatch.setattr(live.subprocess,"Popen",refused)
    def launch(arguments,directory):
        calls.append(arguments)
        script=Path(next(a.split("=",1)[1] for a in arguments if a.startswith("-BreadLiveScript=")))
        startup=json.loads((script.parent/"startup.json").read_bytes())
        ready={**startup,"pid":777,"scriptSha256":live._sha(script)}
        (script.parent/"ready.json").write_text(json.dumps(ready))
        monkeypatch.setattr(live,"_process",lambda pid: {
            "ExecutablePath":arguments[0],"CommandLine":" ".join(arguments)})
    monkeypatch.setattr(live,"_shell_launch",launch)
    session=live.launch(project,{"editor":str(editor)})
    assert session["pid"]==777 and len(calls)==1
    assert project.read_bytes()==b"{}"
    registration=json.loads((Path(session["directory"])/"launch.json").read_bytes())
    assert registration=={"method":"explorer-shell","pid":777}


def test_shell_launch_transports_paths_as_data(tmp_path, monkeypatch):
    recorded=[]
    monkeypatch.setattr(live.subprocess,"run",lambda *args,**kwargs:recorded.append((args,kwargs)))
    executable=str(tmp_path/"引擎 folder/UnrealEditor.exe")
    project=str(tmp_path/"a $() project.uproject")
    live._shell_launch([executable,project],tmp_path)
    arguments,kwargs=recorded[0]
    assert executable not in arguments[0][-1] and project not in arguments[0][-1]
    payload=json.loads(kwargs["input"])
    assert payload["executable"]==executable
    assert payload["arguments"]==live.subprocess.list2cmdline([project])
    assert payload["directory"]==str(tmp_path)
