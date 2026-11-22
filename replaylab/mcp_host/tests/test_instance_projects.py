import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from src.active_projects import ActiveReplayProjectService
from src.shared_file_io import open_shared_read
from src.shader_workspace.io import _replace_with_retry


def test_instance_environment_keeps_selection_private_and_registry_shared(tmp_path):
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/instance_environment.py"
    spec = importlib.util.spec_from_file_location("instance_environment_test", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    a = {"LOCALAPPDATA": str(tmp_path)}; b = dict(a)
    module.configure(a, "100-a", str(tmp_path / "ipc/a"))
    module.configure(b, "200-b", str(tmp_path / "ipc/b"))
    assert a["REPLAYLAB_INTERNAL_RUNTIME_DIR"] != b["REPLAYLAB_INTERNAL_RUNTIME_DIR"]
    assert a["REPLAYLAB_INSTANCE_ID"] != b["REPLAYLAB_INSTANCE_ID"]
    assert a["RENDERDOC_ULTRA_PROJECT_REGISTRY"] == b["RENDERDOC_ULTRA_PROJECT_REGISTRY"]
    sa = ActiveReplayProjectService(a["RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"], tmp_path / "projects", a["RENDERDOC_ULTRA_PROJECT_REGISTRY"])
    sb = ActiveReplayProjectService(b["RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"], tmp_path / "projects", b["RENDERDOC_ULTRA_PROJECT_REGISTRY"])
    def create(service, name):
        return service.create_project(name=name, game_id=name, build_id="build")
    with ThreadPoolExecutor(max_workers=2) as pool:
        f = pool.submit(create, sa, "First"); g = pool.submit(create, sb, "Second")
        first, second = f.result(), g.result()
    sa.activate(first["projectRoot"]); sb.activate(second["projectRoot"])
    assert sa.status()["workspaceId"] == first["workspaceId"]
    assert sb.status()["workspaceId"] == second["workspaceId"]
    assert len(sa._read_project_registry()["projects"]) == 2


def test_reader_observes_consistent_snapshot_during_atomic_replace(tmp_path):
    path = tmp_path / "workspace.json"; path.write_bytes(b"old")
    replacement = tmp_path / "replacement"; replacement.write_bytes(b"new")
    # Some Windows filesystems reject MoveFileEx while the old handle is open
    # even with share-delete; bounded writer retry outlives a short reader.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with open_shared_read(path) as stream:
            pending = pool.submit(_replace_with_retry, replacement, path)
            assert stream.read() == b"old"
        pending.result(timeout=2)
    with open_shared_read(path) as stream:
        assert stream.read() == b"new"
