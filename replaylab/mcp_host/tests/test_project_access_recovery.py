from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from src import internal_host
from src.shader_workspace import io


def test_transient_windows_sharing_error_retries_one_read_without_writes(monkeypatch):
    error = PermissionError("file in use"); error.winerror = 32
    read = Mock(side_effect=[error, {"project": "intact"}])
    sleeps = []
    monkeypatch.setattr(io, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(io, "time", SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(io, "_read_json_object_once", read)
    assert io.read_json_object(Path("workspace.json"), 1024, "project") == {"project": "intact"}
    assert read.call_count == 2 and sleeps == [.01]


def test_real_access_denial_remains_bounded_and_is_not_treated_as_corruption(monkeypatch):
    error = PermissionError("access denied"); error.winerror = 5
    read = Mock(side_effect=error)
    sleeps = []
    monkeypatch.setattr(io, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(io, "time", SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(io, "_read_json_object_once", read)
    with pytest.raises(PermissionError):
        io.read_json_object(Path("workspace.json"), 1024, "project")
    assert read.call_count == 6 and sum(sleeps) == pytest.approx(.31)


@pytest.mark.parametrize("operation,parameters", [("list-projects", {}),
    ("activate-project", {"projectRoot": str(Path.cwd())})])
def test_project_permission_denial_is_not_a_generic_host_failure(operation, parameters):
    import json
    service = SimpleNamespace(list_projects=Mock(side_effect=PermissionError("denied")),
                              activate=Mock(side_effect=PermissionError("denied")))
    registry = internal_host.ProjectOperationRegistry(service)
    request = internal_host.parse_project_operation_request(json.dumps({
        "schema": internal_host.PROJECT_OPERATION_SCHEMA, "requestId": "c"*32,
        "operation": operation, "parameters": parameters}).encode())
    status, value = registry.execute(request)
    assert status == 403
    assert value["error"]["code"] == "project-access-denied"


def test_existing_project_can_be_registered_and_opened_without_project_write_access(tmp_path, monkeypatch):
    from src import projects
    from src.active_projects import ActiveReplayProjectService
    original = ActiveReplayProjectService(tmp_path / "first/active.json", tmp_path / "projects")
    created = original.create_project(name="Portable", game_id="game", build_id="build")
    root = Path(created["projectRoot"])
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    deny_writes = Mock(side_effect=PermissionError("project folder is read only"))
    monkeypatch.setattr(projects, "_project_lock", deny_writes)
    relocated = ActiveReplayProjectService(tmp_path / "second/active.json", tmp_path / "elsewhere")
    assert relocated.register_existing_project(root)["workspaceId"] == created["workspaceId"]
    assert relocated.activate(root)["workspaceId"] == created["workspaceId"]
    assert len(relocated.list_projects()["projects"]) == 1
    deny_writes.assert_not_called()
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
