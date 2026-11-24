from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import threading
import time
from unittest import mock

import pytest

from src import internal_host, internal_tool_service as service_module, server
from src import shader_live_edit_client as client
from src.ipc_client import _CALL_BINDING, bind_direct_tool_call


def binding(root):
    return {"instanceId": "qrenderdoc-test", "captureSessionId": "b" * 32,
            "capturePath": str(root / "frame.rdc")}


def request_body(root, operation="start", parameters=None):
    if parameters is None:
        parameters = {"workspace_path": str(root), "debounce_ms": 100}
    return json.dumps({"schema": service_module.SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                       "requestId": "a" * 32, "operation": operation,
                       "parameters": parameters, "binding": binding(root)}).encode()


class Manager:
    def __init__(self):
        self.running = False
        self.calls = []

    def start(self, workspace_path, debounce_ms):
        self.calls.append(("start", _CALL_BINDING.get()))
        self.running = True
        return {"liveEditId": "1" * 32, "workspacePath": workspace_path}

    def list(self):
        return {"sessions": []}

    def poll(self, live_edit_id):
        return {"liveEditId": live_edit_id, "state": "running"}

    def stop(self, live_edit_id):
        self.running = False
        return {"liveEditId": live_edit_id, "state": "stopped"}

    def migrate_authorization(self, **parameters):
        return {"authorizationMigrated": True}

    def save_authorized_source(self, live, authorization, workspace, expected, text):
        assert self.running and live == "1" * 32
        self.calls.append(("save", live, text))
        return {"watchId": "2" * 32, "rawSha256": "3" * 64, "changed": True,
                "compileScheduled": True, "size": len(text), "modifiedNs": 123}


def context(root):
    return {"capture": {"path": binding(root)["capturePath"], "captureSessionId": "b" * 32}}


def test_bound_compiler_contract_is_read_only_and_capture_bound(tmp_path):
    manager=Manager()
    service=service_module.InternalToolService(project=object(),restore_jobs=object(),shader_manager=manager)
    compiler=mock.Mock()
    compiler.compiler_contract.return_value={"schema":"bread-shader-compiler-contract/v1","identitySha256":"c"*64}
    params={"workspace_path":str(tmp_path),"source_workspace_id":"d"*32,"expected_raw_sha256":"e"*64}
    with mock.patch.object(server,"_active_live_context",return_value=context(tmp_path)), \
         mock.patch.object(server,"_shader_workspace_service",return_value=compiler):
        status,result=service.shader_live_edit_operation(request_body(tmp_path,'compiler-contract',params))
    assert status==200 and result['result']['identitySha256']=='c'*64
    compiler.compiler_contract.assert_called_once_with(str(tmp_path),'d'*32,'e'*64)
    assert manager.calls==[]
    compiler.reset_mock()
    changed=context(tmp_path);changed['capture']['captureSessionId']='f'*32
    with mock.patch.object(server,"_active_live_context",return_value=changed), \
         mock.patch.object(server,"_shader_workspace_service",return_value=compiler):
        status,result=service.shader_live_edit_operation(request_body(tmp_path,'compiler-contract',params))
    assert status==409 and not compiler.compiler_contract.called


def test_singleton_start_and_editor_save_share_one_in_memory_session(tmp_path):
    manager = Manager()
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, started = service.shader_live_edit_operation(request_body(tmp_path))
    assert status == 200 and started["result"]["liveEditId"] == "1" * 32
    assert manager.calls[0] == ("start", ("qrenderdoc-test", "b" * 32))
    save = {"liveEditId": "1" * 32, "authorizationId": "4" * 32, "workspaceId": "5" * 32,
            "expectedRawSha256": "6" * 64, "text": "float4 MainPS() { return 0; }"}
    status, saved = service.save_shader_source(json.dumps(save).encode())
    assert status == 200 and saved["compileScheduled"]
    assert manager.calls[-1] == ("save", "1" * 32, save["text"])
    assert _CALL_BINDING.get() is None
    timing = started["result"]["startTiming"]
    assert set(timing["phasesMs"]) == {"captureIdentity", "startAdmission"}
    assert timing["failedPhase"] == ""
    assert all(0 <= elapsed <= timing["totalMs"] for elapsed in timing["phasesMs"].values())
    assert service_module._SHADER_LIVE_START_TIMING.get() is None


def test_source_only_save_accepts_full_file_and_requires_managed_active_project(tmp_path):
    manager = Manager()
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    parameters = {"workspace_path": str(tmp_path), "source_workspace_id": "e" * 32,
                  "expected_raw_sha256": "f" * 64, "text": "// full file\n" * 7000}
    organization = mock.Mock()
    organization.save_editor_source.return_value = {"sourceOnly": True, "compileScheduled": False}
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
         mock.patch.object(server, "_active_project_service") as project, \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=tmp_path) as managed, \
         mock.patch.object(server, "_shader_organization_manager", return_value=organization):
        status, saved = service.shader_live_edit_operation(request_body(tmp_path, "save-source", parameters))
        assert status == 200 and saved["result"]["sourceOnly"]
        managed.assert_called_once_with(project.return_value.status.return_value, str(tmp_path))
        saved_parameters = organization.save_editor_source.call_args.kwargs
        assert {key: value for key, value in saved_parameters.items() if key != "commit_guard"} == parameters
        assert callable(saved_parameters["commit_guard"])
        managed.side_effect = ValueError("outside managed project")
        status, _ = service.shader_live_edit_operation(request_body(tmp_path, "save-source", parameters))
        assert status == 400 and organization.save_editor_source.call_count == 1
    assert not manager.calls and not manager.running


@pytest.mark.parametrize("operation", ["open-source-document", "save-source-document"])
def test_include_document_endpoint_is_managed_and_capture_bound(tmp_path, operation):
    from src.shader_workspace import source_documents
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    parameters = {"workspace_path": str(tmp_path), "source_workspace_id": "e" * 32}
    if operation == "open-source-document":
        parameters["requested_path"] = str(tmp_path / "source/header.hlsli")
        method = "open_source_document"
    else:
        parameters.update(document_id="d" * 64, expected_raw_sha256="f" * 64,
                          expected_source_sha256="a" * 64, text="")
        method = "save_source_document"
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
         mock.patch.object(server, "_active_project_service") as project, \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=tmp_path) as managed, \
         mock.patch.object(server, "_shader_organization_manager") as manager, \
         mock.patch.object(source_documents, method, return_value={"sourceOnly": True}) as perform:
        status, result = service.shader_live_edit_operation(request_body(tmp_path, operation, parameters))
        assert status == 200 and result["result"]["sourceOnly"]
        assert perform.call_args.args == (manager.return_value,)
        assert {key: value for key, value in perform.call_args.kwargs.items() if key != "commit_guard"} == parameters
        managed.assert_called_once_with(project.return_value.status.return_value, str(tmp_path))
        managed.side_effect = ValueError("outside managed project")
        assert service.shader_live_edit_operation(request_body(tmp_path, operation, parameters))[0] == 400
        assert perform.call_count == 1


def test_wrong_capture_session_is_rejected_before_start(tmp_path):
    manager = Manager()
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    changed = context(tmp_path)
    changed["capture"]["captureSessionId"] = "c" * 32
    with mock.patch.object(server, "_active_live_context", return_value=changed):
        status, result = service.shader_live_edit_operation(request_body(tmp_path))
    assert status == 409 and "capture session changed" in result["error"]
    assert not manager.running
    assert result["startTiming"]["failedPhase"] == "captureIdentity"
    assert "startAdmission" not in result["startTiming"]["phasesMs"]
    assert service_module._SHADER_LIVE_START_TIMING.get() is None


@pytest.mark.parametrize("include", [False, True])
@pytest.mark.parametrize("startup_fails", [False, True])
def test_manual_save_starts_compile_even_without_prior_live_edit(tmp_path, include, startup_fails):
    manager = Manager()
    manager.compile_saved_source = mock.Mock(return_value={"compileScheduled": True, "watchId": "2" * 32})
    if startup_fails:
        manager.compile_saved_source.side_effect = RuntimeError("captured compiler route unavailable")
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    parameters = {"workspace_path": str(tmp_path), "source_workspace_id": "e" * 32,
                  "expected_raw_sha256": "f" * 64, "text": "void main() {}"}
    saved = {"sourceOnly": True, "compileScheduled": False, "rawSha256": "c" * 64,
             "sourceWorkspaceId": "e" * 32, "sourcePath": str(tmp_path / "source/shader.glsl"),
             "modifiedNs": 123, "changed": False}
    operation = "save-and-apply-source"
    save_method = "_save_workspace_source_now"
    if include:
        operation += "-document"
        save_method = "_save_workspace_source_document_now"
        parameters.update(document_id="d" * 64, expected_source_sha256="b" * 64, text="")
        saved.update(sourceDocumentId="d" * 64, rootSourceSha256="a" * 64,
                     mutationPaths=[str(tmp_path / "source/header.hlsli")])
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
         mock.patch.object(service_module, save_method, return_value=saved) as save:
        status, response = service.shader_live_edit_operation(request_body(tmp_path, operation, parameters))
    assert status == 200
    value = response["result"]
    assert value["sourceOnly"] is False and value["compileScheduled"] is not startup_fails
    for key in ("rawSha256", "sourceWorkspaceId", "sourcePath", "modifiedNs", "changed"):
        assert value[key] == saved[key]
    if include:
        for key in ("sourceDocumentId", "rootSourceSha256", "mutationPaths"):
            assert value[key] == saved[key]
    save.assert_called_once_with(**parameters)
    manager.compile_saved_source.assert_called_once_with(str(tmp_path), "e" * 32,
        "a" * 64 if include else "c" * 64, capture_binding=binding(tmp_path))
    if startup_fails:
        assert value["autoApplyError"] == "captured compiler route unavailable"
    else:
        assert "autoApplyError" not in value


def test_failed_manual_source_save_never_starts_compile(tmp_path):
    manager = Manager()
    manager.compile_saved_source = mock.Mock()
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    parameters = {"workspace_path": str(tmp_path), "source_workspace_id": "e" * 32,
                  "expected_raw_sha256": "f" * 64, "text": "void main() {}"}
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
         mock.patch.object(service_module, "_save_workspace_source_now", side_effect=ValueError("source changed")):
        status, response = service.shader_live_edit_operation(request_body(tmp_path, "save-and-apply-source", parameters))
    assert status == 400 and "source changed" in response["error"]
    manager.compile_saved_source.assert_not_called()


def test_conversion_endpoint_requires_saved_hash_and_capture_binding(tmp_path):
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    parameters = {"source_workspace_path": str(tmp_path), "expected_source_sha256": "f" * 64}
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
         mock.patch.object(service_module, "_convert_workspace_to_hlsl_now", return_value={"readyForEditing": True}) as convert:
        status, value = service.shader_live_edit_operation(request_body(tmp_path, "convert-hlsl", parameters))
        assert status == 200 and value["result"]["readyForEditing"]
        convert.assert_called_once_with(**parameters)
        parameters.pop("expected_source_sha256")
        assert service.shader_live_edit_operation(request_body(tmp_path, "convert-hlsl", parameters))[0] == 400
        assert convert.call_count == 1


@pytest.mark.parametrize("ready", [True, False])
def test_conversion_only_shows_validated_hlsl_and_bounds_response(tmp_path, ready):
    result = {"workspacePath": str(tmp_path / "hlsl"), "sourcePreserved": True,
              "readyForEditing": ready, "applied": False,
              "abiValidation": {"error": "binding mismatch", "largeReflection": "x" * 100000}}
    with mock.patch.object(server, "_active_project_service") as project, \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=tmp_path), \
         mock.patch.object(server, "convert_shader_workspace_to_hlsl", return_value=json.dumps({"result": result})), \
         mock.patch.object(server, "show_shader_workspace_source", return_value="{}") as show:
        if ready:
            value = service_module._convert_workspace_to_hlsl_now(str(tmp_path), "f" * 64)
            assert value["readyForEditing"] and value["sourcePreserved"]
            assert len(json.dumps(value)) < 1024
            assert "abiValidation" not in value
            show.assert_called_once_with(result["workspacePath"])
            project.return_value.commit_active_project_snapshot.assert_called_once()
        else:
            with pytest.raises(RuntimeError, match="binding mismatch"):
                service_module._convert_workspace_to_hlsl_now(str(tmp_path), "f" * 64)
            show.assert_not_called()


def test_owner_start_failure_timing_retains_the_innermost_phase(tmp_path):
    manager = Manager()
    def failed_start(**unused):
        with service_module._shader_live_start_phase("capturePreflight"):
            raise RuntimeError("controlled IPC failure")
    manager.start = failed_start
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, result = service.shader_live_edit_operation(request_body(tmp_path))
    assert status == 409 and "controlled IPC failure" in result["error"]
    assert result["startTiming"]["failedPhase"] == "capturePreflight"
    assert set(result["startTiming"]["phasesMs"]) == {"captureIdentity", "capturePreflight", "startAdmission"}
    assert service_module._SHADER_LIVE_START_TIMING.get() is None


@pytest.mark.parametrize("operation,parameters", [
    ("list", {}), ("poll", {"live_edit_id": "1" * 32}), ("stop", {"live_edit_id": "1" * 32}),
])
def test_closed_lifecycle_operations_are_dispatched(tmp_path, operation, parameters):
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, result = service.shader_live_edit_operation(request_body(tmp_path, operation, parameters))
    assert status == 200 and isinstance(result["result"], dict)


def test_migration_is_one_explicit_fixed_operation(tmp_path):
    params = {"source_workspace_path": str(tmp_path / "old"), "workspace_path": str(tmp_path / "new"),
              "expected_source_sha256": "d" * 64}
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, result = service.shader_live_edit_operation(request_body(tmp_path, "migrate", params))
    assert status == 200 and result["result"]["authorizationMigrated"]


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(operation="tools/call"),
    lambda v: v["parameters"].update(command="arbitrary"),
    lambda v: v["binding"].update(instanceId="../foreign-instance"),
    lambda v: v["parameters"].update(debounce_ms=True),
])
def test_parser_rejects_generic_calls_extra_fields_and_bad_bindings(tmp_path, mutation):
    value = json.loads(request_body(tmp_path))
    mutation(value)
    with pytest.raises(ValueError):
        service_module.parse_shader_live_edit_operation(json.dumps(value).encode())


def test_worker_reenters_frozen_capture_binding_on_its_own_thread(tmp_path):
    key = os.path.normcase(str(tmp_path.resolve()))
    seen = []
    def apply(*args):
        seen.append(_CALL_BINDING.get())
        return {"applied": True}
    frozen = service_module._freeze_shader_live_session("qrenderdoc-test", "b" * 32, os.getpid(), str(tmp_path / "frame.rdc"))
    with mock.patch.object(service_module, "_shader_capture_session_state", return_value="current"):
        with mock.patch.object(server, "_shader_live_edit_apply_candidate", side_effect=apply):
            # Rebinding the same workspace's next job cannot retarget this one.
            with mock.patch.dict(service_module._SHADER_LIVE_EDIT_BINDINGS, {key: object()}, clear=True):
                worker = threading.Thread(target=frozen.apply_candidate, args=(str(tmp_path), 2, "c" * 64))
                worker.start()
                worker.join(5)
    assert seen == [("qrenderdoc-test", "b" * 32)] and _CALL_BINDING.get() is None


@pytest.mark.parametrize("provide_status", [False, True])
def test_durable_identity_projection_reuses_only_the_supplied_workspace_status(tmp_path, provide_status):
    status = {"workspaceId": "a" * 32,
              "capture": {"captureId": "b" * 64, "capturePath": str(tmp_path / "frame.rdc")},
              "shader": {"shaderKey": "77:fragment:MainPS:" + "c" * 64,
                         "rawSha256": "c" * 64, "resourceId": 77},
              "stage": "fragment", "entryPoint": "MainPS"}
    workspace_service = mock.Mock()
    workspace_service.status.return_value = status
    with mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service):
        identity = server._shader_live_edit_workspace_identity(
            str(tmp_path), **({"status": status} if provide_status else {}))
    assert identity == {"workspaceId": "a" * 32, "captureId": "b" * 64,
                        "capturePath": str(tmp_path / "frame.rdc"),
                        "shaderKey": "77:fragment:MainPS:" + "c" * 64,
                        "shaderRawSha256": "c" * 64, "resourceId": 77,
                        "stage": "fragment", "entryPoint": "MainPS"}
    if provide_status:
        workspace_service.status.assert_not_called()
    else:
        workspace_service.status.assert_called_once_with(str(tmp_path))


def test_start_binding_checks_catalog_in_the_discovered_scope_before_publication(tmp_path):
    from src import ipc_client
    (tmp_path / "qrenderdoc-test").mkdir()
    identity = {"capturePath": str(tmp_path / "frame.rdc"), "captureId": "c" * 64}
    manifest = {"captureLoaded": True, "captureSessionId": "b" * 32, "capturePath": identity["capturePath"],
                "pid": os.getpid(), "instanceId": "qrenderdoc-test"}
    (tmp_path / "qrenderdoc-test" / "instance.json").write_text(json.dumps(manifest), encoding="utf-8")
    status = {"fixture": "one immutable status",
              "source": {"present": True, "lane": "executable-editable"},
              "representation": {"lane": "executable-editable", "compileEligible": True}}
    workspace_service = mock.Mock()
    workspace_service.status.return_value = status

    def validate(workspace, *, status):
        assert workspace == str(tmp_path)
        assert status is workspace_service.status.return_value
        assert _CALL_BINDING.get() == ("qrenderdoc-test", "b" * 32)
        assert service_module._SHADER_LIVE_EDIT_BINDINGS == {}
        return {"capture": {"path": identity["capturePath"],
                            "captureId": identity["captureId"], "captureSessionId": "b" * 32}}

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(ipc_client.IPCClient, "_manifest", return_value=manifest), \
            mock.patch.object(server, "_shader_live_edit_workspace_identity", return_value=identity), \
            mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_workspace_active_capture_context", side_effect=validate) as verified, \
            mock.patch.dict(service_module._SHADER_LIVE_EDIT_BINDINGS, {}, clear=True):
        assert service_module._bound_shader_workspace_identity(str(tmp_path)) == identity
        frozen = service_module._bound_shader_session(str(tmp_path))
        assert frozen.binding == binding(tmp_path)
        verified.assert_called_once_with(str(tmp_path), status=status)
    assert _CALL_BINDING.get() is None
    workspace_service.status.assert_called_once_with(str(tmp_path))


@pytest.mark.parametrize("mutation,expected", [
    ({"heartbeatUnixMs": 1}, "current"),  # A busy UI is not a dead owner.
    ({"captureSessionId": "c" * 32}, "expired"),
    ({"capturePath": "other.rdc"}, "expired"),
    ({"captureLoaded": False}, "expired"),
    ({"stopped": True}, "expired"),
    ({"captureSessionId": "bad"}, "unavailable"),
    ({"instanceId": "different"}, "unavailable"),
])
def test_capture_session_state_uses_process_and_session_not_heartbeat(tmp_path, mutation, expected):
    from src import ipc_client

    scope = binding(tmp_path)
    mailbox = tmp_path / scope["instanceId"]
    mailbox.mkdir()
    manifest = {**scope, "pid": os.getpid(), "captureLoaded": True, "heartbeatUnixMs": 1}
    manifest.update(mutation)
    (mailbox / "instance.json").write_text(json.dumps(manifest), encoding="utf-8")
    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)):
        assert service_module._shader_capture_session_state(scope, os.getpid()) == expected


def test_unreadable_mailbox_is_unknown_until_owner_process_has_exited(tmp_path):
    from src import ipc_client

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)):
        assert service_module._shader_capture_session_state(binding(tmp_path), os.getpid()) == "unavailable"
        with mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=False):
            assert service_module._shader_capture_session_state(binding(tmp_path), 9876) == "expired"


def test_idle_stages_share_mailbox_but_reload_and_write_are_checked_fresh(tmp_path):
    from src import ipc_client
    scope = binding(tmp_path)
    directory = tmp_path / scope["instanceId"]
    directory.mkdir()
    path = directory / "instance.json"
    record = {**scope, "pid": os.getpid(), "captureLoaded": True}
    path.write_text(json.dumps(record), encoding="utf-8")
    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(service_module, "_read_shader_capture_session_state",
                              wraps=service_module._read_shader_capture_session_state) as read, \
            mock.patch.object(service_module.time, "monotonic", return_value=100.0):
        for _ in range(69):
            assert service_module._shader_capture_session_state(scope, os.getpid()) == "current"
        assert read.call_count == 1
        assert service_module._shader_capture_session_state(scope, os.getpid(), fresh=True) == "current"
        assert read.call_count == 2
        record["captureSessionId"] = "f" * 32
        path.write_text(json.dumps(record), encoding="utf-8")
        assert service_module._shader_capture_session_state(scope, os.getpid()) == "expired"
        assert read.call_count == 3
        with mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=False):
            assert service_module._shader_capture_session_state(scope, os.getpid()) == "expired"


def test_expired_frozen_worker_never_enters_apply(tmp_path):
    frozen = service_module._freeze_shader_live_session("qrenderdoc-test", "b" * 32, os.getpid(), str(tmp_path / "frame.rdc"))
    with mock.patch.object(service_module, "_shader_capture_session_state", return_value="expired"):
        with mock.patch.object(server, "_shader_live_edit_apply_candidate") as apply:
            with pytest.raises(RuntimeError, match="no longer current"):
                frozen.apply_candidate(str(tmp_path), 2, "c" * 64)
    apply.assert_not_called()


def test_temporarily_unavailable_frozen_worker_defers_without_entering_apply(tmp_path):
    frozen = service_module._freeze_shader_live_session(
        "qrenderdoc-test", "b" * 32, os.getpid(), str(tmp_path / "frame.rdc")
    )
    with mock.patch.object(
        service_module, "_shader_capture_session_state", return_value="unavailable"
    ):
        with mock.patch.object(server, "_shader_live_edit_apply_candidate") as apply:
            with pytest.raises(
                service_module.ShaderLiveEditSessionUnavailable,
                match="temporarily unavailable",
            ):
                frozen.apply_candidate(str(tmp_path), 2, "c" * 64)
    apply.assert_not_called()


@pytest.mark.parametrize("same_gui_process", [False, True])
def test_existing_owner_tick_resumes_new_capture_without_any_mcp_list(tmp_path, same_gui_process, monkeypatch):
    # Compatibility embedders can explicitly retain historical auto-resume.
    monkeypatch.setattr(server, "_shader_editors_on_demand", False)
    from src import ipc_client
    from src.shader_live_edit import ShaderLiveEditManager
    from tests.test_shader_live_edit import _wait_until
    from tests.test_shader_live_edit_lifecycle import _PersistentWatchManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capture_path = str(tmp_path / "frame.rdc")
    identity = {"workspaceId": "a" * 32, "captureId": "b" * 64, "capturePath": capture_path,
                "shaderKey": "shader", "shaderRawSha256": "c" * 64, "resourceId": 77,
                "stage": "fragment", "entryPoint": "MainPS"}
    mailboxes = tmp_path / "instances"
    mailboxes.mkdir()
    alive = {101}

    def publish(instance, pid, session):
        directory = mailboxes / instance
        directory.mkdir(exist_ok=True)
        record = {"instanceId": instance, "pid": pid, "captureSessionId": session,
                  "capturePath": capture_path, "captureLoaded": True,
                  "heartbeatUnixMs": int(time.time() * 1000)}
        (directory / "instance.json").write_text(json.dumps(record), encoding="utf-8")
        return {key: record[key] for key in ("instanceId", "captureSessionId", "capturePath")}

    old_scope = publish("old-gui", 101, "1" * 32)
    watch = _PersistentWatchManager()
    manager = ShaderLiveEditManager(
        watch, lambda *_: pytest.fail("unbound apply"), registry_path=tmp_path / "registry.json",
        workspace_identity=service_module._bound_shader_workspace_identity,
        bind_session=service_module._bound_shader_session, poll_seconds=0.005, join_seconds=0.2)
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    workspace_service = mock.Mock()
    workspace_service.status.return_value = {
        "source": {"present": True, "lane": "executable-editable"},
        "representation": {"lane": "executable-editable", "compileEligible": True}}

    def validate_catalog(_workspace, *, status):
        assert status is workspace_service.status.return_value
        scope = _CALL_BINDING.get()
        assert scope is not None
        return {"capture": {"captureId": identity["captureId"],
                            "path": capture_path, "captureSessionId": scope[1]}}

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(mailboxes)), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", side_effect=lambda pid: pid in alive), \
            mock.patch.object(server, "_shader_live_edit_workspace_identity", return_value=identity), \
            mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_workspace_active_capture_context", side_effect=validate_catalog) as preflight, \
            mock.patch.object(server, "_shader_live_edit_apply_candidate", side_effect=AssertionError("no replay allowed")):
        try:
            with bind_direct_tool_call(old_scope["instanceId"], old_scope["captureSessionId"]):
                old = manager.start(workspace, 100)
            authorization_id = manager.list()["authorizations"][0]["authorizationId"]
            if same_gui_process:
                new_scope = publish("old-gui", 101, "2" * 32)
                owner_pid = 101
            else:
                alive.remove(101)
                alive.add(202)
                new_scope = publish("new-gui", 202, "2" * 32)
                owner_pid = 202
            # This is the existing Internal owner-monitor callback, not an
            # extra MCP request introduced by the delivery probe.
            refreshed = service.refresh_shader_sessions([owner_pid])
            assert refreshed["attempted"] is True
            resumed = refreshed["results"][0]["result"]["results"][0]
            assert resumed["resumed"] is True and resumed["liveEditId"] != old["liveEditId"]
            _wait_until(lambda: manager.poll(resumed["liveEditId"])["state"] == "running")
            current = manager.poll(resumed["liveEditId"])
            assert current["captureBinding"] == new_scope and current["activeReplacement"] is None
            assert manager.poll(old["liveEditId"])["state"] != "running"
            assert manager.list()["authorizations"][0]["authorizationId"] == authorization_id
            service.refresh_shader_sessions([owner_pid])
            assert len(watch.persistent_start_calls) == 1
            assert preflight.call_count == 2
        finally:
            manager.shutdown()


def test_on_demand_owner_tick_does_not_scan_or_activate_historical_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_shader_editors_on_demand", True)
    manager = Manager()
    manager.reconcile_capture = mock.Mock(side_effect=AssertionError("idle activation"))
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    with mock.patch.object(service_module, "_shader_capture_mailbox", side_effect=AssertionError("idle mailbox scan")):
        assert service.refresh_shader_sessions([101]) == {"attempted": False, "results": [], "reason": "editors-on-demand"}
    with service._automatic_shader_resume(binding(tmp_path)) as allowed:
        assert allowed is False


def test_owner_tick_ignores_unregistered_or_ambiguous_gui_mailboxes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_shader_editors_on_demand", False)
    from src import ipc_client

    manager = Manager()
    manager.reconcile_capture = mock.Mock(return_value={"results": []})
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    for instance in ("one", "two"):
        (tmp_path / instance).mkdir()

    def manifest(directory):
        instance = Path(directory).name
        return {"instanceId": instance, "pid": 101 if instance == "one" else 202,
                "captureLoaded": True, "captureSessionId": "a" * 32,
                "capturePath": str(tmp_path / "frame.rdc")}

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(ipc_client.IPCClient, "_manifest", side_effect=manifest), \
            mock.patch.object(service_module, "_shader_capture_mailbox", side_effect=manifest), \
            mock.patch.object(service_module, "_shader_capture_session_state", return_value="current"):
        service.refresh_shader_sessions([])
        service.refresh_shader_sessions([101, 202])
        manager.reconcile_capture.assert_not_called()
        service.refresh_shader_sessions([202])
        assert manager.reconcile_capture.call_args.args[0]["instanceId"] == "two"


def test_busy_or_unavailable_registered_gui_is_not_erased_from_automatic_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_shader_editors_on_demand", False)
    from src import ipc_client

    manager = Manager()
    manager.reconcile_capture = mock.Mock(return_value={"results": []})
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    records = {}
    for instance, pid in (("busy", 101), ("fresh", 202)):
        (tmp_path / instance).mkdir()
        records[instance] = {"instanceId": instance, "pid": pid, "captureLoaded": True,
                             "captureSessionId": "a" * 32, "capturePath": str(tmp_path / "same.rdc")}
    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(service_module, "_shader_capture_mailbox", side_effect=lambda directory: records.get(directory.name)), \
            mock.patch.object(ipc_client.IPCClient, "_manifest", side_effect=lambda directory: records["fresh"] if Path(directory).name == "fresh" else None), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=True), \
            mock.patch.object(service_module, "_shader_capture_session_state", return_value="current"):
        # No old job is needed to protect the busy GUI: both physical owners
        # remain in uniqueness even when IPC discovery rejects its heartbeat.
        service.refresh_shader_sessions([101, 202])
        manager.reconcile_capture.assert_not_called()
        service.refresh_shader_sessions([202, 303])  # The third owner is unreadable.
        manager.reconcile_capture.assert_not_called()
        service.refresh_shader_sessions([202])
        manager.reconcile_capture.assert_called_once()


def test_internal_start_defers_resume_to_registered_owner_tick():
    manager = Manager()
    manager.resume_persistent = mock.Mock(side_effect=AssertionError("must wait for registered owner tick"))
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    assert service.start()["resumeDeferredToOwnerTick"] is True
    manager.resume_persistent.assert_not_called()


def test_existing_internal_monitor_calls_bounded_capture_refresh():
    import ast

    tree = ast.parse(inspect.getsource(internal_host))
    monitor = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "monitor")
    calls = [node for node in ast.walk(monitor) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "tick"]
    assert any(isinstance(call.func.value, ast.Name)
               and call.func.value.id == "shader_session_refresh"
               and isinstance(call.args[0], ast.Name) and call.args[0].id == "owners"
               for call in calls)
    assert not any(isinstance(node, ast.Await) for node in ast.walk(monitor))


def test_mcp_forwards_without_constructing_task_local_manager(tmp_path):
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        with mock.patch.object(client, "request", return_value={"liveEditId": "1" * 32}) as remote:
            with mock.patch.object(server, "_shader_live_edit_manager", side_effect=AssertionError("must use singleton")):
                with bind_direct_tool_call("qrenderdoc-test", "b" * 32):
                    result = json.loads(server.start_shader_workspace_live_edit(str(tmp_path), 100))
    assert result["operation"] == "start"
    remote.assert_called_once_with("start", {"workspace_path": str(tmp_path), "debounce_ms": 100}, binding(tmp_path))


def test_client_binds_generation_response_and_keeps_health_unauthenticated(tmp_path):
    paths = internal_host._paths(tmp_path)
    paths["state"].write_text(json.dumps({"port": 49743}), encoding="utf-8")
    paths["token"].write_text("x" * 48, encoding="ascii")
    health = {"capabilities": ["shader-live-edit"]}
    def respond(request, timeout):
        if request.full_url.endswith("/health"):
            assert request.get_header("Authorization") is None
            return health
        value = json.loads(request.data)
        assert request.full_url == "http://127.0.0.1:49743/replaylab/shader-live-edit"
        return {"schema": service_module.SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                "requestId": value["requestId"], "binding": value["binding"],
                "hostInstanceId": "f" * 32, "result": {"state": "running"}}
    with mock.patch.object(client, "_read_response", side_effect=respond):
        with mock.patch.object(internal_host, "_validate_generation", return_value="f" * 32) as validate:
            assert client.request("list", {}, binding(tmp_path), root=tmp_path) == {"state": "running"}
    assert validate.call_count == 2


def test_redirects_are_rejected_and_mcp_startup_does_not_resume_watchers():
    with pytest.raises(RuntimeError, match="cannot redirect"):
        client._NoRedirect().redirect_request(None, None, 302, "", {}, "https://foreign.example/")
    assert "resume_persistent" not in inspect.getsource(server.main)


def test_failed_owner_start_preserves_structured_timing_through_http_and_tool_error():
    timing = {"totalMs": 12.0, "phasesMs": {"capturePreflight": 8.0}, "failedPhase": "capturePreflight"}
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = 409
    response.read.return_value = json.dumps({"error": "controlled failure", "startTiming": timing}).encode()
    with mock.patch.object(client._OPENER, "open", return_value=response):
        with pytest.raises(RuntimeError, match="controlled failure") as caught:
            client._read_response(object(), 1)
    error = json.loads(server._err(caught.value))
    assert error["data"]["startTiming"] == timing
    assert "capturePreflight" not in error["error"]


def test_switch_operations_reuse_the_current_candidate_and_trim_the_receipt(tmp_path):
    """The workspace toggle: apply compiles only a changed source; restore rolls back."""

    workspace_service = mock.Mock()
    workspace_service.status.return_value = {
        "source": {"currentSha256": "s" * 64},
        "latestCompiled": {"revision": 4, "rawSha256": "r" * 64, "sourceSha256": "s" * 64}}
    receipt = {"scope": {"captureSessionId": "b" * 32},
               "replacement": {"replacementId": "9" * 32},
               "manifest": {"huge": "x" * 100000}}
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
            mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_shader_live_edit_apply_candidate", return_value=receipt) as apply:
        status, result = service.shader_live_edit_operation(
            request_body(tmp_path, "apply", {"workspace_path": str(tmp_path)}))
    assert status == 200, result
    applied = result["result"]
    assert applied["applied"] and applied["compiled"] is False and applied["revision"] == 4
    assert applied["replacementId"] == "9" * 32
    assert "manifest" not in applied["result"] and applied["result"]["replacement"]["replacementId"] == "9" * 32
    apply.assert_called_once_with(str(tmp_path), 4, "r" * 64)
    workspace_service.compile.assert_not_called()

    # A header-only edit must not reuse a main-SHA-identical old candidate.
    workspace_service.status.return_value["sourceDependenciesCurrent"] = False
    workspace_service.compile_for_live_edit.return_value = {"revision": 5, "rawSha256": "q" * 64, "sourceSha256": "s" * 64}
    with mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_shader_live_edit_apply_candidate", return_value=receipt) as apply:
        header_changed = service_module._apply_workspace_candidate_now(str(tmp_path))
    assert header_changed["compiled"] is True and header_changed["revision"] == 5
    apply.assert_called_once_with(str(tmp_path), 5, "q" * 64)
    workspace_service.status.return_value["sourceDependenciesCurrent"] = True

    # A source edited since the last compile is compiled first.
    workspace_service.status.return_value["source"]["currentSha256"] = "t" * 64
    workspace_service.compile_for_live_edit.return_value = {"revision": 5, "rawSha256": "q" * 64, "sourceSha256": "t" * 64}
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
            mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_shader_live_edit_apply_candidate", return_value=receipt):
        status, result = service.shader_live_edit_operation(
            request_body(tmp_path, "apply", {"workspace_path": str(tmp_path)}))
    assert status == 200 and result["result"]["compiled"] is True and result["result"]["revision"] == 5

    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)), \
            mock.patch.object(server, "rollback_shader_workspace_shared_resource",
                              return_value=json.dumps({"rollback": {"restored": True}})) as rollback:
        status, result = service.shader_live_edit_operation(
            request_body(tmp_path, "restore", {"workspace_path": str(tmp_path), "replacement_id": "9" * 32}))
    assert status == 200 and result["result"]["result"]["rollback"]["restored"]
    rollback.assert_called_once_with(str(tmp_path), "9" * 32)
