"""Saved Source display is independent of organization/proposal lifecycles."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import types
from unittest import mock

import pytest

from src import server
from src.ipc_client import bind_direct_tool_call


BRIDGE_ROOT = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"
spec = importlib.util.spec_from_file_location("saved_source_console", BRIDGE_ROOT / "shader_console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


@pytest.fixture
def saved_source(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "source").mkdir(parents=True)
    (workspace / "ai").mkdir()
    source = workspace / "source" / "shader.hlsl"
    source.write_bytes(b"float4 MainPS() : SV_Target { return 1; }\n")
    manifest = {
        "schema": console.WORKSPACE_SCHEMA, "schemaVersion": 1,
        "workspaceId": "1" * 32,
        "capture": {"captureId": "2" * 64, "capturePath": str(tmp_path / "source-capture.rdc")},
        "shader": {"resourceId": 1000000000000000723, "rawSha256": "3" * 64,
                   "stage": "fragment", "entryPoint": "MainPS"},
        "stage": "fragment", "entryPoint": "MainPS",
        "source": {"path": "source/shader.hlsl", "encoding": "utf-8"},
        "latestCompiled": {"sourceSha256": "0" * 64},
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # The source need not originate in the capture whose UI displays it.
    binding = {"instanceId": "saved-source-test", "captureSessionId": "4" * 32,
               "capturePath": str(tmp_path / "current-capture.rdc")}
    return workspace, source, manifest, binding


@pytest.mark.parametrize("gate_state", ("absent", "broken", "drafting", "finish-failed-draft-preserved", "accepted-executable-editable"))
def test_saved_source_ignores_current_organization_and_stale_proposal_receipts(saved_source, gate_state):
    workspace, source, manifest, binding = saved_source
    gate_path = workspace / "ai" / "organize-code-gate.json"
    if gate_state == "broken":
        gate_path.write_text("{broken", encoding="utf-8")
    elif gate_state != "absent":
        gate_path.write_text(json.dumps({"decision": gate_state, "organizationId": "5" * 32,
                                        "finalBinding": {"sourceSha256": "0" * 64}}), encoding="utf-8")
    (workspace / "ai" / "organization-preview.json").write_text(
        json.dumps({"organizationId": "6" * 32, "sourceSha256": "7" * 64}), encoding="utf-8")
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    opened = console.load_saved_shader_source_preview(str(workspace), digest, binding)
    assert opened["text"] == source.read_bytes().decode("utf-8")
    assert opened["sourceKind"] == "saved-source"
    assert opened["organizationId"] == ""
    assert opened["captureId"] == manifest["capture"]["captureId"]
    assert opened["capturePath"] != binding["capturePath"]
    assert opened["captureBinding"] == console.normalize_capture_binding(binding)
    assert opened["readOnly"] is True
    assert opened["completedChecks"] == ["source-read"]
    assert opened["replayApplied"] is False
    assert not {"authorizationId", "liveEditId", "watchId"} & opened.keys()
    assert {path: path.read_bytes() for path in before} == before
    again = console.load_saved_shader_source_preview(str(workspace), digest, binding)
    assert again["workspaceId"] == opened["workspaceId"]


@pytest.mark.parametrize("changed", ("source-bytes", "source-path", "bad-utf8", "nul"))
def test_saved_source_does_not_turn_real_content_errors_into_display_success(saved_source, changed):
    workspace, source, manifest, binding = saved_source
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if changed == "source-path":
        manifest["source"]["path"] = "../shader.hlsl"
        (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        source.write_bytes({"source-bytes": b"// different Source\n", "bad-utf8": b"\xff", "nul": b"x\0y"}[changed])
        if changed != "source-bytes":
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(console.ShaderConsoleError):
        console.load_saved_shader_source_preview(str(workspace), digest, binding)


@pytest.mark.parametrize("changed", ("source-bytes", "source-path", "execution-metadata"))
def test_saved_source_rechecks_physical_identity_but_not_unrelated_metadata(saved_source, changed):
    workspace, source, manifest, binding = saved_source
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    read_bytes = console._read_file_bytes
    mutated = False

    def race(path, maximum, field):
        nonlocal mutated
        raw = read_bytes(path, maximum, field)
        if field == "saved canonical source" and not mutated:
            mutated = True
            if changed == "source-bytes":
                source.write_bytes(b"// concurrent edit\n")
            elif changed == "source-path":
                manifest["source"]["path"] = "source/other.hlsl"
                source.with_name("other.hlsl").write_bytes(raw)
            else:
                manifest["latestCompiled"] = {"revision": 123, "sourceSha256": digest}
                manifest["organizationId"] = "9" * 32
            (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return raw

    with mock.patch.object(console, "_read_file_bytes", side_effect=race):
        if changed == "execution-metadata":
            opened = console.load_saved_shader_source_preview(str(workspace), digest, binding)
            assert opened["rawSha256"] == digest
        else:
            with pytest.raises(console.ShaderConsoleError, match="changed"):
                console.load_saved_shader_source_preview(str(workspace), digest, binding)


def test_host_shows_actual_source_without_organization_or_capture_catalog_preflight(saved_source):
    workspace, source, _manifest, binding = saved_source
    context = {"capture": {"loaded": True, "captureSessionId": binding["captureSessionId"], "captureId": ""}}
    shown = {"shown": True, "sourceKind": "saved-source", "readOnly": True, "replayApplied": False}
    with mock.patch.object(server, "_active_project_service"), \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=workspace), \
         mock.patch.object(server, "_alias_publication_workspace_status", side_effect=AssertionError("display needs no baseline validation")), \
         mock.patch.object(server, "_workspace_active_capture_context", side_effect=AssertionError("no capture-catalog preflight")), \
         mock.patch.object(server, "_shader_workspace_service", side_effect=AssertionError("no compiler/tool registry")), \
         mock.patch.object(server.ipc, "call", side_effect=[context, shown]) as ipc:
        result = json.loads(server.show_shader_workspace_source(str(workspace)))
    assert result == shown
    assert ipc.call_args_list[0] == mock.call("get_current_renderdoc_context", {"identity_only": True}, timeout=30.0)
    assert ipc.call_args_list[1] == mock.call("show_shader_organization_preview", {
        "workspace_path": str(workspace), "canonical_source": True,
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_capture_session_id": binding["captureSessionId"],
    }, timeout=30.0)


@pytest.mark.parametrize("scope", ("direct", "process-owner", "direct-precedes-owner"))
def test_host_scoped_saved_source_display_uses_bridge_current_capture_checks(saved_source, scope):
    workspace, source, _manifest, binding = saved_source
    shown = {"shown": True, "sourceKind": "saved-source", "readOnly": False, "replayApplied": False}
    scoped_session = binding["captureSessionId"]
    owner_session = scoped_session if scope == "process-owner" else "9" * 32
    with mock.patch.object(server, "_active_project_service"), \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=workspace), \
         mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", owner_session), \
         mock.patch.object(server.ipc, "call", return_value=shown) as ipc:
        if scope == "process-owner":
            result = json.loads(server.show_shader_workspace_source(str(workspace)))
        else:
            with bind_direct_tool_call("saved-source-test", scoped_session):
                result = json.loads(server.show_shader_workspace_source(str(workspace)))
    assert result == shown
    ipc.assert_called_once_with("show_shader_organization_preview", {
        "workspace_path": str(workspace), "canonical_source": True,
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_capture_session_id": scoped_session,
    }, timeout=30.0)


@pytest.mark.parametrize("scope", ("direct", "process-owner"))
def test_stale_saved_source_scope_fails_without_retargeting_or_retry(saved_source, scope):
    workspace, source, _manifest, binding = saved_source
    before = source.read_bytes()
    stale_session = "7" * 32
    with mock.patch.object(server, "_active_project_service"), \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=workspace), \
         mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", stale_session if scope == "process-owner" else ""), \
         mock.patch.object(server.ipc, "call", side_effect=RuntimeError("Capture session changed before organization preview")) as ipc:
        if scope == "process-owner":
            result = json.loads(server.show_shader_workspace_source(str(workspace)))
        else:
            with bind_direct_tool_call("saved-source-test", stale_session):
                result = json.loads(server.show_shader_workspace_source(str(workspace)))
    assert result["error"] == "Capture session changed before organization preview"
    assert ipc.call_count == 1
    assert ipc.call_args.args[0] == "show_shader_organization_preview"
    assert ipc.call_args.args[1]["expected_capture_session_id"] == stale_session
    assert source.read_bytes() == before


@pytest.mark.parametrize("reuse_live_canonical", (False, True))
@pytest.mark.parametrize("activate_window", (None, False, True))
def test_bridge_saved_source_presentation_does_not_mint_live_authority_or_use_proposal_receipt(reuse_live_canonical, activate_window):
    bridge_path = BRIDGE_ROOT / "__init__.py"
    tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_show_shader_organization_preview")
    session, source_sha = "4" * 32, "5" * 64
    binding = {"instanceId": "saved-source-test", "captureSessionId": session,
               "capturePath": "D:/current-capture.rdc"}
    opened = {"captureBinding": binding, "organizationId": "", "rawSha256": source_sha,
              "captureId": "6" * 64, "workspaceId": "7" * 64,
              "sourceWorkspaceId": "8" * 32, "canonicalSourceUpdated": True}
    loader = mock.Mock(return_value=opened)
    panel = mock.Mock()
    panel._capture_binding = binding
    panel.show_organization_preview.return_value = {
        "shown": True,
        "workspaceId": opened["sourceWorkspaceId"] if reuse_live_canonical else opened["workspaceId"],
        "documentKind": "live-edit" if reuse_live_canonical else "organization-preview",
        "readOnly": not reuse_live_canonical,
        "canonicalWorkspaceReused": reuse_live_canonical}
    namespace = {
        "_require_capture_identity": mock.Mock(side_effect=AssertionError("no source/current capture ID alignment")),
        "_shader_override_hex": lambda value, *_args: value,
        "_capture_session_id": session, "_shader_workspace_capture_binding": lambda: binding,
        "_request_admitted_capture_identity": lambda: None,
        "_shader_console_protocol": types.SimpleNamespace(load_saved_shader_source_preview=loader),
        "_invoke_on_ui_sync": lambda callback: callback(),
        "_shader_workspace_ui": panel, "_shader_workspace_callbacks": [],
    }
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(bridge_path), "exec"), namespace)
    params = {"canonical_source": True, "workspace_path": "D:/workspace",
              "expected_source_sha256": source_sha, "expected_capture_session_id": session}
    if activate_window is not None:
        params["activate_window"] = activate_window
    with mock.patch("builtins.open", side_effect=AssertionError("display cannot write Source")):
        result = namespace[handler.name](params)
    loader.assert_called_once_with("D:/workspace", source_sha, capture_binding=binding)
    panel.show_organization_preview.assert_called_once_with(opened)
    if activate_window is False:
        panel.show.assert_not_called()
    else:
        panel.show.assert_called_once_with()
    assert result["sourceKind"] == "saved-source"
    assert result["sourceCaptureId"] == opened["captureId"]
    assert result["organizationId"] == ""
    assert result["readOnly"] is not reuse_live_canonical
    assert result["canonicalWorkspaceReused"] is reuse_live_canonical
    assert result["documentKind"] == ("live-edit" if reuse_live_canonical else "organization-preview")
    assert result["liveEditStarted"] is False and result["replayApplied"] is False


def _redirected_saved_source_display(dirty=False):
    """Exercise the real Bridge endpoint with an older replay-file request."""
    bridge_path = BRIDGE_ROOT / "__init__.py"
    tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_show_shader_organization_preview")
    session, requested_id, current_id = "4" * 32, "7" * 32, "8" * 32
    old_sha = "5" * 64
    text = "float a = 0.1; // latest editable source\n" + ("// unsaved\n" if dirty else "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    binding = {"instanceId": "saved-source-test", "captureSessionId": session,
               "capturePath": "D:/current-capture.rdc"}
    old_project = {"knowledgeId": "1" * 32, "sourceSha256": old_sha}
    opened = {
        "captureBinding": binding, "capturePath": binding["capturePath"],
        "organizationId": "", "rawSha256": old_sha, "captureId": "6" * 64,
        "workspaceId": "9" * 64, "sourceWorkspaceId": requested_id,
        "canonicalSourceUpdated": True, "resourceId": 65557, "stage": "fragment",
        "entryPoint": "main", "shaderRawSha256": "a" * 64,
        "projectSourceIdentity": old_project, "projectSourceBinding": old_project,
    }
    workspace = dict(opened, workspaceId=current_id, sourceWorkspaceId=current_id,
                     workspacePath="D:/project/current", projectSourceIdentity=None,
                     projectSourceBinding=None)
    editor = types.SimpleNamespace(IsReadOnly=lambda: False)
    document = {"workspaceId": current_id, "sourceWorkspaceId": current_id,
                "sourcePath": "D:/project/current/source/shader.hlsl", "available": True,
                "captureBinding": binding, "editor": editor, "dirty": dirty}
    shown = {"shown": True, "workspaceId": current_id, "documentKind": "live-edit",
             "readOnly": False, "canonicalWorkspaceReused": True,
             "redirectedFromWorkspaceId": requested_id,
             "displayedSourceSha256": digest, "displayedSourceWorkspaceId": current_id,
             "displayedSourcePath": document["sourcePath"]}
    panel = types.SimpleNamespace(
        _capture_binding=binding, _documents={current_id: document},
        _workspaces={current_id: workspace}, _workspace_event_ids={current_id: [5366]},
        _canonical_workspace_id=lambda key: current_id if key == requested_id else key,
        _document_source_text=lambda doc: text,
        show_organization_preview=lambda value: shown, show=mock.Mock())
    namespace = {
        "hashlib": hashlib, "_shader_override_hex": lambda value, *_args: value,
        "_capture_session_id": session, "_shader_workspace_capture_binding": lambda: binding,
        "_request_admitted_capture_identity": lambda: None,
        "_shader_console_protocol": types.SimpleNamespace(
            load_saved_shader_source_preview=lambda *args, **kwargs: opened,
            _same_path=lambda left, right: left == right),
        "_invoke_on_ui_sync": lambda callback: callback(),
        "_shader_workspace_ui": panel, "_shader_workspace_callbacks": [],
    }
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(bridge_path), "exec"), namespace)
    params = {"canonical_source": True, "workspace_path": "D:/project/replay-cache",
              "expected_source_sha256": old_sha, "expected_capture_session_id": session}
    return namespace[handler.name], params, opened, workspace, document, shown, panel, digest


@pytest.mark.parametrize("dirty", (False, True))
def test_saved_replay_redirect_reports_actual_current_text_without_old_project_binding(dirty):
    call, params, opened, workspace, document, shown, panel, digest = _redirected_saved_source_display(dirty)
    result = call(params)
    assert result["redirectedToCurrentSource"] is True
    assert result["workspaceId"] == result["sourceWorkspaceId"] == document["workspaceId"]
    assert result["sourceSha256"] == digest != opened["rawSha256"]
    assert result["sourceWorkspacePath"] == workspace["workspacePath"]
    assert result["eventIds"] == [5366]
    assert result["projectSourceIdentity"] is None and result["projectSourceBinding"] is None
    assert result["displayedSource"]["dirty"] is dirty
    assert result["requestedSource"]["workspaceId"] == opened["sourceWorkspaceId"]
    assert result["requestedSource"]["sourceSha256"] == opened["rawSha256"]
    assert result["requestedSource"]["projectSourceBinding"] == opened["projectSourceBinding"]
    assert result["replayApplied"] is False and result["liveEditStarted"] is False
    assert workspace["projectSourceBinding"] is None


@pytest.mark.parametrize("changed", (
    "alias", "source-id", "source-sha", "source-path", "resource", "original",
    "stage", "entry", "capture-path", "capture-session", "unavailable", "readonly", "proposal",
))
def test_saved_replay_redirect_rechecks_current_document_identity(changed):
    call, params, opened, workspace, document, shown, panel, digest = _redirected_saved_source_display()
    if changed == "alias":
        panel._canonical_workspace_id = lambda key: key
    elif changed == "source-id":
        shown["displayedSourceWorkspaceId"] = "f" * 32
    elif changed == "source-sha":
        shown["displayedSourceSha256"] = opened["rawSha256"]
    elif changed == "source-path":
        shown["displayedSourcePath"] = "D:/project/replay-cache/source/shader.hlsl"
    elif changed == "resource":
        workspace["resourceId"] += 1
    elif changed == "original":
        workspace["shaderRawSha256"] = "b" * 64
    elif changed == "stage":
        workspace["stage"] = "vertex"
    elif changed == "entry":
        workspace["entryPoint"] = "other"
    elif changed == "capture-path":
        workspace["capturePath"] = "D:/other.rdc"
    elif changed == "capture-session":
        document["captureBinding"] = dict(document["captureBinding"], captureSessionId="f" * 32)
    elif changed == "unavailable":
        document["available"] = False
    elif changed == "readonly":
        shown["readOnly"] = True
    elif changed == "proposal":
        opened["canonicalSourceUpdated"] = False
    with pytest.raises(RuntimeError, match="current-source"):
        call(params)
    panel.show.assert_not_called()


def test_host_does_not_reattach_old_project_binding_after_current_source_redirect(saved_source):
    workspace, source, manifest, binding = saved_source
    old_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    old_binding = {"sourceSha256": old_sha, "captureSessionId": binding["captureSessionId"]}
    redirected = {"shown": True, "redirectedToCurrentSource": True,
                  "sourceSha256": "e" * 64, "sourceWorkspacePath": "D:/project/current",
                  "projectSourceBinding": None,
                  "requestedSource": {"sourceSha256": old_sha, "projectSourceBinding": old_binding}}
    service = mock.Mock()
    service.status.return_value = {}
    with mock.patch.object(server, "_active_project_service", return_value=service), \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=workspace), \
         mock.patch.object(server.ipc, "call", return_value=redirected):
        with bind_direct_tool_call("saved-source-test", binding["captureSessionId"]):
            result = json.loads(server._show_saved_shader_workspace_source(
                str(workspace), project_source_binding=old_binding))
    assert result == redirected


@pytest.mark.parametrize("changed", ("stale-session", "closed-before-read", "closed-after-read", "path-after-read", "session-after-read"))
def test_bridge_saved_source_real_loader_rejects_capture_lifecycle_changes(saved_source, changed):
    workspace, source, _manifest, binding = saved_source
    bridge_path = BRIDGE_ROOT / "__init__.py"
    tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ("_show_shader_organization_preview", "_shader_workspace_capture_binding")]
    state = {"loaded": changed != "closed-before-read", "path": binding["capturePath"]}
    panel = mock.Mock()
    panel._capture_binding = binding
    namespace = {
        "_shader_override_hex": lambda value, *_args: value,
        "_capture_session_id": binding["captureSessionId"],
        "_request_admitted_capture_identity": lambda: None,
        "_active_capture_token": ("loaded", binding["capturePath"]),
        "INSTANCE_ID": binding["instanceId"],
        "_ctx": types.SimpleNamespace(IsCaptureLoaded=lambda: state["loaded"],
                                      GetCaptureFilename=lambda: state["path"]),
        "_shader_console_protocol": console,
        "_shader_workspace_ui": panel, "_shader_workspace_callbacks": [],
    }
    invocations = 0

    def invoke(callback):
        nonlocal invocations
        invocations += 1
        # This is the same boundary as Qt running the actual final show
        # callback after the production loader read/checked the real files.
        if invocations == 2:
            if changed == "closed-after-read":
                state["loaded"] = False
            elif changed == "path-after-read":
                state["path"] = str(source.parent / "another.rdc")
            elif changed == "session-after-read":
                namespace["_capture_session_id"] = "8" * 32
        return callback()

    namespace["_invoke_on_ui_sync"] = invoke
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(bridge_path), "exec"), namespace)
    params = {"canonical_source": True, "workspace_path": str(workspace),
              "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "expected_capture_session_id": "9" * 32 if changed == "stale-session" else binding["captureSessionId"]}
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    with pytest.raises(RuntimeError, match="Capture session changed|current capture binding"):
        namespace["_show_shader_organization_preview"](params)
    panel.show_organization_preview.assert_not_called()
    assert {path: path.read_bytes() for path in before} == before


def test_saved_source_editable_ui_does_not_claim_validation_or_application():
    workspace_path = BRIDGE_ROOT / "project_shader_workspace.py"
    tree = ast.parse(workspace_path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == "_status_text")
    namespace = {"QtCore": types.SimpleNamespace(QCoreApplication=types.SimpleNamespace(
        translate=lambda _context, source: source))}
    latest_event = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_latest_event")
    exec(compile(ast.Module(body=[latest_event, method], type_ignores=[]), str(workspace_path), "exec"), namespace)
    text = namespace[method.name](None, {"sourceKind": "saved-source", "documentKind": "organization-preview"})
    assert text == "Source is editable; compile and apply checks are still pending."
    assert "segment" not in text and "read-only" not in text


def test_closed_shader_workspace_can_be_recreated_in_background_then_explicitly_raised():
    bridge_path = BRIDGE_ROOT / "__init__.py"
    tree = ast.parse(bridge_path.read_text(encoding="utf-8"))
    register = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_register_shader_workspace_ui")
    panel = mock.Mock()
    constructor = mock.Mock(return_value=panel)
    namespace = {
        "_shader_workspace_ui": None, "_shader_workspace_callbacks": [],
        "_shader_workspace_protocol": types.SimpleNamespace(HAS_QT=True,
            ReplayLabShaderWorkspacePanel=constructor),
        "_shader_console_protocol": types.SimpleNamespace(default_registry_path=lambda: "D:/registry.json"),
        "_shader_workspace_capture_binding": mock.Mock(), "_ensure_project_async_executor": mock.Mock(),
        "_shader_visual_debug_operation": mock.Mock(),
        "qrd": types.SimpleNamespace(WindowMenu=types.SimpleNamespace(Tools=1, Window=2)),
        "QtCore": types.SimpleNamespace(QCoreApplication=types.SimpleNamespace(translate=lambda _ctx, source: source)),
    }
    exec(compile(ast.Module(body=[register], type_ignores=[]), str(bridge_path), "exec"), namespace)
    namespace[register.name](mock.Mock())
    panel.show.assert_called_once_with()
    constructor.call_args.kwargs["closed_callback"]()
    assert namespace["_shader_workspace_ui"] is None
    panel.show.reset_mock()
    callback = namespace["_shader_workspace_callbacks"][0]
    callback(activate_window=False)
    assert namespace["_shader_workspace_ui"] is panel
    assert constructor.call_count == 2
    panel.show.assert_not_called()
    callback()
    assert constructor.call_count == 2
    panel.show.assert_called_once_with()
