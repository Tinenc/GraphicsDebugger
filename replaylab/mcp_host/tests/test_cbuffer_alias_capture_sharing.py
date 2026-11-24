"""Project CBuffer names are Shader knowledge, never an EID's bound values."""
import copy
import json
import shutil

import pytest

from src.shader_workspace import aliases


@pytest.fixture
def shared_names(tmp_path, monkeypatch):
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    user.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: project)
    monkeypatch.setattr(aliases, "user_store_root", lambda: user)
    members = [{"name": "unknown", "offset": 0}]
    layout = aliases.layout_sha256("Material", members)
    scope = {"shaderRawSha256": "a" * 64, "shaderResourceId": "9007199254740993",
             "stage": "fragment", "bind": 5, "space": 0, "layoutSha256": layout}
    # More than the inline threshold also exercises immutable shared pages.
    fields = [{"name": "parameter%d" % index, "type": "float",
               "components": [{"offset": index * 4}], "status": "inferred",
               "provenance": "shader", "evidence": "source/shader.glsl:%d" % (index + 1),
               "description": "Material control"} for index in range(40)]
    aliases.publish_aliases("Material", members,
        [{"path": "unknown[0]", "offset": 0, "alias": "surfaceControls"}],
        block_alias="Material Controls", shader_scope=scope, semantic_fields=fields,
        semantic_source_sha256="b" * 64, semantic_captured_shader_sha256="a" * 64,
        semantic_usage_review={"schema": "bread-cbuffer-usage-review/v1",
            "unusedComponents": [{"offset": 160, "type": "float"}],
            "evidence": "All current entry consumers reviewed; this declared scalar is not read."})
    return project, scope, fields


def test_same_shader_names_and_channel_groups_survive_capture_resource_id_changes(shared_names):
    project, scope, fields = shared_names
    for resource_id in ("17", "42", "18446744073709551614"):
        rebound = {**scope, "shaderResourceId": resource_id}
        view = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=rebound)
        assert view["fields"] == fields
        assert view["sourceSha256"] == "b" * 64
        assert view["usageReview"]["unusedComponents"] == [{"offset": 160, "type": "float"}]
        assert aliases.read_aliases(scope["layoutSha256"], shader_scope=rebound)[0]["alias"] == "surfaceControls"
        assert aliases.read_block_alias(scope["layoutSha256"], shader_scope=rebound)["alias"] == "Material Controls"
    # EID, bound buffer ResourceId/range and values are intentionally not part
    # of the portable definition. The panel reads those from each current draw.
    saved = json.loads((project / (scope["layoutSha256"] + ".json")).read_bytes())
    assert "fieldChunks" in saved["semanticViews"][0]
    assert "eventId" not in saved["semanticViews"][0]
    assert all(set(component) == {"offset"} for field in fields for component in field["components"])


def test_replacement_view_and_unused_review_follow_exact_candidate_across_capture_draws(shared_names):
    project, original_scope, unused_fields = shared_names
    members = [{"name": "baseTint", "offset": 0}, {"name": "control", "offset": 16}]
    layout = aliases.layout_sha256("MaterialControls", members)
    candidate_scope = {**original_scope, "shaderRawSha256": "d" * 64,
                       "shaderResourceId": "208035", "layoutSha256": layout}
    fields = [{"name": "baseTint", "type": "float3", "components": [{"offset": x} for x in (0, 4, 8)],
               "status": "inferred", "provenance": "shader", "description": "Surface color multiplier.",
               "evidence": "The current calculation consumes only the xyz color lanes."}]
    review = {"schema": "bread-cbuffer-usage-review/v1", "unusedComponents": [{"offset": 12, "type": "float"}],
              "evidence": "The w lane has no consumer in this entry; its material meaning is unknown."}
    aliases.publish_aliases("MaterialControls", members, [], shader_scope=candidate_scope,
        semantic_fields=fields, semantic_usage_review=review,
        semantic_source_sha256="e" * 64, semantic_captured_shader_sha256="a" * 64)
    for event_id, buffer_id, buffer_offset, draw_value in ((1291, 900, 256, 0.2), (1571, 901, 1024, 0.7)):
        # EIDs and bound value ranges are capture-local observations. They never
        # participate in portable name lookup, even for one shared Shader.
        rebound = {**candidate_scope, "shaderResourceId": "72473", "eventId": event_id,
                   "boundBufferId": buffer_id, "boundByteOffset": buffer_offset, "value": draw_value}
        view = aliases.read_semantic_view(layout, shader_scope=rebound)
        assert view["fields"] == fields
        assert view["usageReview"] == review
        assert view["sourceSha256"] == "e" * 64
        assert view["capturedShaderRawSha256"] == "a" * 64
    persisted = json.loads((project / (layout + ".json")).read_text(encoding="utf-8"))
    view = persisted["semanticViews"][0]
    assert not {"eventId", "boundBufferId", "boundByteOffset", "value"} & view.keys()
    assert not {"eventId", "boundBufferId", "boundByteOffset", "value"} & view["shaderScope"].keys()
    # Same original bytes do not grant an unrelated edited program this review.
    assert aliases.read_semantic_view(layout, shader_scope={**candidate_scope, "shaderRawSha256": "f" * 64}) is None


def test_project_file_snapshot_retains_names_pages_and_unused_without_origin_capture(shared_names, tmp_path, monkeypatch):
    project, scope, fields = shared_names
    restored_project = tmp_path / "restored-project"
    shutil.copytree(project, restored_project)
    monkeypatch.setattr(aliases, "project_store_root", lambda: restored_project)
    rebound = {**scope, "shaderResourceId": "72473"}
    view = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=rebound)
    assert view["fields"] == fields
    assert view["usageReview"]["unusedComponents"] == [{"offset": 160, "type": "float"}]
    assert aliases.read_block_alias(scope["layoutSha256"], shader_scope=rebound)["alias"] == "Material Controls"
    # The project dictionary has no origin RDC/workspace/path dependency: its
    # immutable pages and scope are sufficient for another capture's lookup.
    assert "capturePath" not in view and "workspacePath" not in view


@pytest.mark.parametrize("change", [
    {"shaderRawSha256": "c" * 64}, {"stage": "vertex"}, {"bind": 6}, {"space": 1},
])
def test_equal_resource_id_or_layout_cannot_transfer_names_to_a_different_shader_binding(shared_names, change):
    project, scope, fields = shared_names
    unrelated = {**scope, **change}
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=unrelated) is None
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope=unrelated) == []
    assert aliases.read_block_alias(scope["layoutSha256"], shader_scope=unrelated) is None


def test_switching_game_projects_keeps_each_projects_current_names_separate(shared_names, tmp_path, monkeypatch):
    first, scope, fields = shared_names
    second = tmp_path / "other-game"
    second.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: second)
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope) is None
    alternate = copy.deepcopy(fields)
    alternate[0]["name"] = "anotherControl"
    aliases.publish_aliases("Material", [{"name": "unknown", "offset": 0}], [],
        shader_scope=scope, semantic_fields=alternate, semantic_captured_shader_sha256="a" * 64)
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)["fields"][0]["name"] == "anotherControl"
    monkeypatch.setattr(aliases, "project_store_root", lambda: first)
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)["fields"] == fields
