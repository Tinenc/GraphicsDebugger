"""Exact current-head lookup must not revive a historical Shader publication."""
from pathlib import Path
from unittest import mock
import json

import pytest

import test_projects as project_fixtures
from src.projects import ReplayProjectError


@pytest.fixture
def project():
    fixture = project_fixtures.ReplayProjectTests(
        "test_source_only_workspace_draft_skips_compiled_candidate")
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def publish(project, label, *, source="// organized source\n", blob=None, resource_id=91,
            expected_head=None, source_format="hlsl", profile="ps_5_0"):
    catalog, shader, capture_id = project._catalog(
        label + "-capture", blob or project_fixtures._dxbc(), resource_id=resource_id)
    project.service.import_capture(catalog)
    status = project._workspace_status(label + "-workspace", capture_id, shader, source)
    status["targetProfile"]["profile"] = profile
    status["compilerBinding"] = project_fixtures._compiler_binding(status["targetProfile"])
    if source_format != "hlsl":
        original = Path(status["source"]["path"])
        renamed = original.with_suffix("." + source_format)
        original.rename(renamed)
        status["source"].update(format=source_format, path=str(renamed))
    options = {"expected_head": expected_head} if expected_head else {}
    return project.service.register_workspace(status, **options)


def query(knowledge):
    return dict(source_sha256=knowledge["sourceSha256"], raw_sha256=knowledge["rawSha256"],
                stage=knowledge["stage"], entry_point=knowledge["entryPoint"])


def test_current_head_lookup_excludes_history_and_needs_no_capture_match_scan(project):
    old = publish(project, "old", source="// previous source\n")
    current = publish(project, "current", resource_id=61523,
                      expected_head=old["head"], source="// latest source\n")
    with mock.patch.object(project.service, "_build_reuse_mappings",
                           side_effect=AssertionError("lookup must not scan capture matches")):
        assert project.service.find_current_workspace_source_head(**query(old["knowledge"])) is None
        found = project.service.find_current_workspace_source_head(**query(current["knowledge"]))
        assert found["knowledge"] == current["knowledge"]
        assert found["headKey"] == current["head"]["headKey"]
        assert found["generation"] == current["head"]["generation"]
        assert found["projectId"] == project.service._load()["projectId"]
        assert project.service.current_workspace_source_head(
            old["knowledge"]["knowledgeId"])["knowledge"] == current["knowledge"]


@pytest.mark.parametrize("field,replacement", [
    ("source_sha256", "0" * 64),
    ("raw_sha256", "0" * 64),
    ("stage", "vertex"),
    ("entry_point", "OtherMain"),
])
def test_current_head_lookup_requires_every_source_and_shader_identity_field(
        project, field, replacement):
    current = publish(project, "identity")
    target = query(current["knowledge"])
    target[field] = replacement
    assert project.service.find_current_workspace_source_head(**target) is None


def test_current_head_lookup_authenticates_the_stored_source_artifact(project):
    current = publish(project, "authenticated")
    artifact = project.project / current["knowledge"]["sourcePath"]
    artifact.write_text("// changed outside the publication transaction\n", encoding="utf-8")
    with pytest.raises(ReplayProjectError, match="source hash mismatch"):
        project.service.find_current_workspace_source_head(**query(current["knowledge"]))


def test_same_program_source_lanes_select_latest_saved_representation(project):
    hlsl = publish(project, "hlsl-lane")
    glsl = publish(project, "glsl-lane", source_format="glsl")
    assert hlsl["head"]["headKey"] == glsl["head"]["headKey"]
    assert query(hlsl["knowledge"]) == query(glsl["knowledge"])
    found=project.service.find_current_workspace_source_head(**query(hlsl["knowledge"]))
    assert found['knowledge']==glsl['knowledge']


def test_exact_workspace_scope_disambiguates_same_source_and_raw_across_profiles(project):
    first = publish(project, "profile50", profile="ps_5_0")
    second = publish(project, "profile51", profile="ps_5_1")
    assert first["head"]["headKey"] == second["head"]["headKey"]
    assert project.service.find_current_workspace_source_head(**query(first["knowledge"]))['knowledge']==second['knowledge']
    for publication in (second,):
        knowledge = publication["knowledge"]
        scope = dict(workspace_id=knowledge["workspaceId"], capture_id=knowledge["captureId"],
                     target_profile=knowledge["targetProfile"], baseline_abi_sha256=knowledge["baselineAbiSha256"])
        found = project.service.find_current_workspace_source_head(**query(knowledge), **scope)
        assert found["knowledge"] == knowledge
        # A partial exact match must not fall back to the other profile/head.
        for field in scope:
            changed = dict(scope, **{field: {"profile": "wrong"} if field == "target_profile" else "0" * 64})
            assert project.service.find_current_workspace_source_head(**query(knowledge), **changed) is None


def test_saved_source_display_scopes_real_duplicate_project_heads(project, monkeypatch):
    from src import server

    first = publish(project, "display50", profile="ps_5_0")
    selected = publish(project, "display51", profile="ps_5_1")
    knowledge = selected["knowledge"]
    assert query(first["knowledge"]) == query(knowledge)
    assert project.service.find_current_workspace_source_head(**query(knowledge))['knowledge']==knowledge

    workspace = project.root / "display51-workspace"
    manifest = {
        "workspaceId": knowledge["workspaceId"],
        "source": {"path": "source/shader.hlsl"},
        "shader": {"resourceId": 91, "rawSha256": knowledge["rawSha256"]},
        "stage": knowledge["stage"], "entryPoint": knowledge["entryPoint"],
        "targetProfile": knowledge["targetProfile"],
        "baseline": {"abiSha256": knowledge["baselineAbiSha256"]},
        "capture": {"captureId": knowledge["captureId"],
                    "capturePath": str(project.root / "display51-capture.rdc")},
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    project_before = (project.project / "project.json").read_bytes()
    active = mock.Mock()
    active.status.return_value = {"replayProject": {"projectId": project.service._load()["projectId"]}}
    active.replay_service.return_value = project.service
    monkeypatch.setattr(server, "_active_project_service", lambda: active)
    monkeypatch.setattr(server, "_require_managed_active_workspace", lambda *a: workspace)
    monkeypatch.setattr(server, "_shader_organization_anchor_event", lambda *a: 23)
    monkeypatch.setattr(server, "_SCOPED_CAPTURE_SESSION_ID", "f" * 32)
    show = mock.Mock(return_value=json.dumps({"shown": True}))
    monkeypatch.setattr(server, "_show_project_shader_source", show)

    assert json.loads(server.show_shader_workspace_source(str(workspace)))["shown"]
    assert show.call_args.kwargs["knowledge_id"] == knowledge["knowledgeId"]
    assert show.call_args.kwargs["capture_id"] == knowledge["captureId"]
    assert show.call_args.kwargs["identity"]["anchorEventId"] == 23
    assert (project.project / "project.json").read_bytes() == project_before
