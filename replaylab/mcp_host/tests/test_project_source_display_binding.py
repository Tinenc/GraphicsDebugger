"""Project source display routes through the current exact restore match."""
import hashlib
import contextlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src import server
from test_shader_saved_source_display import saved_source


@pytest.mark.parametrize("mismatch", ["", "project", "source", "original", "stage", "entry", "session"])
@pytest.mark.parametrize("activate_window", [True, False])
def test_saved_project_draft_uses_current_capture_binding_without_recompiling(saved_source, mismatch, activate_window):
    workspace, source, manifest, current = saved_source
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    original = manifest["shader"]["rawSha256"]
    identity = {"resourceId": 1000000000000000728, "anchorEventId": 61408,
                "stage": "fragment", "entryPoint": "MainPS", "rawSha256": original}
    project_id = "a" * 32
    arguments = {"project_id": project_id, "knowledge_id": "b" * 32,
                 "identity": identity, "capture_id": "c" * 64,
                 "capture_path": current["capturePath"], "capture_session_id": current["captureSessionId"],
                 "activate_window": activate_window}
    if mismatch == "project":
        arguments["project_id"] = "d" * 32
    elif mismatch == "source":
        source_sha = "d" * 64
    elif mismatch == "original":
        identity["rawSha256"] = "d" * 64
    elif mismatch == "stage":
        identity["stage"] = "vertex"
    elif mismatch == "entry":
        identity["entryPoint"] = "OtherPS"
    before = source.read_bytes()
    located = {"captureId": arguments["capture_id"], "captureSessionId": current["captureSessionId"],
        "eventId": 61408, "stage": "fragment", "entryPoint": "MainPS", "rawSha256": original,
        "shaderResourceId": identity["resourceId"], "workspacePath": str(workspace)}
    with mock.patch.object(server, "_active_project_service") as active, \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=workspace), \
         mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", "e" * 32 if mismatch == "session" else ""), \
         mock.patch.object(server, "_shader_workspace_service", side_effect=AssertionError("display cannot compile")), \
         mock.patch.object(server, "_project_source_editor_workspace", return_value=workspace), \
         mock.patch.object(server, "get_active_shader_workspace_path", return_value=json.dumps(located)), \
         mock.patch.object(server.ipc, "call", return_value={"shown": True, "replayApplied": False}) as ipc:
        active.return_value.status.return_value = {"replayProject": {"projectId": project_id}}
        active.return_value.replay_service.return_value.get_workspace_knowledge.return_value = {
            "rawSha256": original, "stage": "fragment", "entryPoint": "MainPS",
            "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        result = json.loads(server._show_project_shader_source(str(workspace), source_sha, **arguments))
    assert source.read_bytes() == before
    if mismatch:
        assert result.get("error")
        ipc.assert_not_called()
    else:
        assert result["shown"] and result["replayApplied"] is False
        ipc.assert_called_once()
        params = ipc.call_args.args[1]
        assert params.get("activate_window", True) is activate_window
        binding = params["project_source_binding"]
        assert binding["resourceId"] == identity["resourceId"] != manifest["shader"]["resourceId"]
        assert binding["captureId"] == arguments["capture_id"] != manifest["capture"]["captureId"]
        assert binding["anchorEventId"] == 61408
        assert binding["originalRawSha256"] == original
        assert binding["sourceSha256"] == source_sha
        assert params["workspace_path"] == str(workspace)


def test_warm_pending_source_rebinds_resource_and_reuses_identical_current_editable_source(saved_source, tmp_path):
    workspace, source, manifest, current = saved_source
    current_workspace = tmp_path / "current-target"
    (current_workspace / "source").mkdir(parents=True)
    current_source = current_workspace / "source" / "shader.hlsl"
    current_source.write_bytes(source.read_bytes())
    current_manifest = json.loads(json.dumps(manifest))
    current_manifest["shader"]["resourceId"] = 728
    current_manifest["capture"]["captureId"] = "c" * 64
    current_manifest["source"]["sha256"] = "0" * 64  # actual bytes are authoritative
    (current_workspace / "manifest.json").write_text(json.dumps(current_manifest), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    identity = {"resourceId": 3213, "anchorEventId": 61408, "stage": "fragment",
        "entryPoint": "MainPS", "rawSha256": manifest["shader"]["rawSha256"]}
    located = {"captureId": "c" * 64, "captureSessionId": current["captureSessionId"],
        "eventId": 61408, "stage": "fragment", "entryPoint": "MainPS", "rawSha256": identity["rawSha256"],
        "shaderResourceId": 728, "workspacePath": str(current_workspace)}
    with mock.patch.object(server, "_active_project_service") as active, \
         mock.patch.object(server, "_require_managed_active_workspace", side_effect=lambda active, path: Path(path)), \
         mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", ""), \
         mock.patch.object(server, "get_active_shader_workspace_path", return_value=json.dumps(located)) as locate, \
         mock.patch.object(server.ipc, "call", return_value={"shown": True}) as ipc:
        active.return_value.status.return_value = {"replayProject": {"projectId": "a" * 32}}
        active.return_value.replay_service.return_value.get_workspace_knowledge.return_value = {
            "rawSha256": identity["rawSha256"], "stage": "fragment", "entryPoint": "MainPS", "sourceSha256": digest}
        result = json.loads(server._show_project_shader_source(str(workspace), digest,
            project_id="a" * 32, knowledge_id="b" * 32, identity=identity, capture_id="d" * 64,
            capture_path=current["capturePath"], capture_session_id=current["captureSessionId"]))
    assert result["shown"], result
    assert result["sourceWorkspacePath"] == str(current_workspace)
    assert result["projectSourceBinding"]["resourceId"] == 728
    assert ipc.call_args.args[1]["workspace_path"] == str(current_workspace)
    assert "expected_resource_id" not in locate.call_args.kwargs
    assert source.read_bytes() == current_source.read_bytes()


@pytest.mark.parametrize("origin_changed", ["deleted", "edited"])
def test_missing_or_edited_origin_materializes_project_artifact_without_compile_or_overwrite(origin_changed):
    from test_shader_workspace_routes import ShaderWorkspaceRouteChainTests
    from src.projects import ReplayProjectService
    from src.shader_workspace.catalog import load_selected_shader
    fixture = ShaderWorkspaceRouteChainTests()
    fixture.setUp()
    try:
        fixture._install_interface(fixture.catalog_path, fixture._complete_dxil_interface())
        service = fixture.service()
        origin = fixture.root / "original-workspace"
        service.create(fixture.catalog_path, fixture.key.catalog_key, origin, editable_format="hlsl")
        service.decompile(origin)
        status = service.status(origin)
        source = Path(status["source"]["path"])
        original_bytes = source.read_bytes()
        project = fixture.root / "project"
        ReplayProjectService.create(project, "shared-source", game_id="test", build_id="1",
                                    platform="windows-x64", api_family="d3d12")
        replay = ReplayProjectService(project)
        replay.import_capture(fixture.catalog_path)
        registered = replay.register_workspace(status, include_compiled_candidate=False)
        knowledge = registered["knowledge"]
        if origin_changed == "deleted":
            source.unlink()
        else:
            source.write_bytes(b"// user's independent draft, never overwrite\n")
        base = fixture.root / "current-default"
        active = {"workspaceId": "a" * 32, "projectRoot": str(fixture.root),
                  "paths": {"shaderWorkspaces": str(fixture.root)}}
        active_service = mock.Mock()
        active_service.shader_workspace_path.return_value = base
        active_service.pin_active_project.side_effect = lambda *args: contextlib.nullcontext()
        selected = load_selected_shader(fixture.catalog_path, fixture.key.catalog_key)
        located = {"workspacePath": str(origin), "eventId": 61408, "stage": "fragment",
            "entryPoint": status["entryPoint"], "shaderResourceId": status["shader"]["resourceId"],
            "rawSha256": status["baseline"]["rawSha256"], "captureId": status["capture"]["captureId"],
            "catalogSha256": status["capture"]["catalogSha256"], "routeBindingSha256": "f" * 64}
        with mock.patch.object(server, "_active_capture_catalog_selection", return_value=(
                {"captureSessionId": "b" * 32}, fixture.catalog_path, selected, "binding")), \
             mock.patch.object(server, "_shader_workspace_service", return_value=service), \
             mock.patch.object(server, "_require_managed_active_workspace", side_effect=lambda active, path: Path(path)), \
             mock.patch.object(service, "compile", side_effect=AssertionError("display must not compile")), \
             mock.patch.object(service, "decompile", side_effect=AssertionError("saved project text must not be decompiled")):
            actual_status = service.status
            def wrong_empty_target(path):
                value = actual_status(path)
                if value["source"]["present"] is False:
                    value["shader"]["resourceId"] += 1
                return value
            with mock.patch.object(service, "status", side_effect=wrong_empty_target), \
                 mock.patch.object(service, "import_project_source", side_effect=AssertionError("wrong physical target must not be written")):
                with pytest.raises(RuntimeError, match="different current Shader"):
                    server._project_source_editor_workspace(active_service, active, replay, knowledge,
                        located, knowledge["sourceSha256"], "b" * 32)
            first = server._project_source_editor_workspace(active_service, active, replay, knowledge,
                located, knowledge["sourceSha256"], "b" * 32)
            first_status = service.status(first)
            first_source = Path(first_status["source"]["path"])
            assert first_source.read_bytes() == original_bytes
            assert not first_status.get("latestCompiled")
            assert first_status["source"]["projectSource"]["knowledgeId"] == knowledge["knowledgeId"]
            again = server._project_source_editor_workspace(active_service, active, replay, knowledge,
                located, knowledge["sourceSha256"], "b" * 32)
            assert again == first
            first_source.write_bytes(b"// independent unsaved delivery draft on disk\n")
            different = server._project_source_editor_workspace(active_service, active, replay, knowledge,
                located, knowledge["sourceSha256"], "b" * 32)
            assert different != first
            assert first_source.read_bytes() == b"// independent unsaved delivery draft on disk\n"
            assert Path(service.status(different)["source"]["path"]).read_bytes() == original_bytes
            # Removing a generated copy's tracked file does not make its
            # manifest empty or authorize an import over that preserved draft.
            Path(service.status(different)["source"]["path"]).unlink()
            recovered = server._project_source_editor_workspace(active_service, active, replay, knowledge,
                located, knowledge["sourceSha256"], "b" * 32)
            assert recovered not in {first, different}
            assert Path(service.status(recovered)["source"]["path"]).read_bytes() == original_bytes
            assert (different / "manifest.json").is_file()
            assert first_source.read_bytes() == b"// independent unsaved delivery draft on disk\n"
            assert server._project_source_editor_workspace(active_service, active, replay, knowledge,
                located, knowledge["sourceSha256"], "b" * 32) == recovered
        if origin_changed == "deleted":
            assert not source.exists()
        else:
            assert source.read_bytes() == b"// user's independent draft, never overwrite\n"
    finally:
        fixture.tearDown()
