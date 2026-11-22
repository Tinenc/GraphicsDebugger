import json

from src.shader_workspace.semantic_overview_storage import publish_overview, restore_overview


def test_project_overview_follows_source_across_capture_workspaces(tmp_path):
    project = tmp_path / "project"
    first, second = tmp_path / "capture-a", tmp_path / "capture-b"
    (first / "ai").mkdir(parents=True)
    second.mkdir()
    project.mkdir()
    overview = {"version": 1, "sourceSha256": "b" * 64, "originalRawSha256": "a" * 64,
                "stages": [{"id": "fog", "title": "Fog", "anchors": []}]}
    (first / "ai/semantic-overview.json").write_text(json.dumps(overview), encoding="utf-8")
    assert publish_overview(project, first, "a" * 64, "b" * 64)
    assert not restore_overview(project, second, "c" * 64, "b" * 64)
    assert not restore_overview(project, second, "a" * 64, "c" * 64)
    assert restore_overview(project, second, "a" * 64, "b" * 64)
    path = second / "ai/semantic-overview.json"
    assert json.loads(path.read_text(encoding="utf-8")) == overview
    path.write_text("local edited overview", encoding="utf-8")
    assert not restore_overview(project, second, "a" * 64, "b" * 64)
    assert path.read_text(encoding="utf-8") == "local edited overview"
    stale = dict(overview, sourceSha256="c" * 64)
    path.write_text(json.dumps(stale), encoding="utf-8")
    assert restore_overview(project, second, "a" * 64, "b" * 64)
    assert json.loads(path.read_text(encoding="utf-8")) == overview


def test_stale_navigation_metadata_is_not_published_as_current(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "ai").mkdir(parents=True)
    (workspace / "ai/semantic-overview.json").write_text(json.dumps({
        "version": 1, "sourceSha256": "c" * 64, "stages": []}), encoding="utf-8")
    assert not publish_overview(tmp_path / "project", workspace, "a" * 64, "b" * 64)
    assert not (tmp_path / "project").exists()


def test_legacy_navigation_retains_original_review_and_source_identity(tmp_path):
    project, origin, target = [tmp_path / name for name in ("project", "origin", "target")]
    project.mkdir()
    (origin / "ai").mkdir(parents=True)
    target.mkdir()
    (origin / "manifest.json").write_text(json.dumps({
        "workspaceId": "d" * 32, "baseline": {"rawSha256": "a" * 64}}), encoding="utf-8")
    overview = {"version": 1, "sourceSha256": "b" * 64, "originalRawSha256": "a" * 64,
                "stages": [{"id": "surface", "title": "Surface", "anchors": []}],
                "analysisReview": {"sourceSha256": "b" * 64}}
    (origin / "ai/semantic-overview.json").write_text(json.dumps(overview), encoding="utf-8")
    assert not restore_overview(project, target, "a" * 64, "c" * 64,
        origin_workspace=origin, origin_workspace_id="e" * 32)
    assert not restore_overview(project, target, "f" * 64, "c" * 64,
        origin_workspace=origin, origin_workspace_id="d" * 32)
    assert restore_overview(project, target, "a" * 64, "c" * 64,
        origin_workspace=origin, origin_workspace_id="d" * 32)
    assert json.loads((target / "ai/semantic-overview.json").read_text("utf-8")) == overview
    assert not (project / "derived").exists()
