"""Completed naming revisions replace obsolete AI history within one lineage."""

import copy
import hashlib
import json

import pytest

from src.shader_workspace import aliases, alias_field_storage as storage


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def scope(raw="current", **changes):
    return dict(shaderRawSha256=digest(raw), stage="fragment", bind=3, space=0,
                layoutSha256=digest("layout"), **changes)


def view(raw, *, captured="original", source="ai", updated="", **changes):
    record = {"shaderScope": {**scope(raw), **changes},
              "capturedShaderRawSha256": digest(captured), "source": source,
              "updatedAt": updated, "fields": []}
    if captured is None:
        record.pop("capturedShaderRawSha256")
    return record


def prune(views, **options):
    return aliases.prune_semantic_history(
        views, shader_scope=scope(), captured_shader_sha256=digest("original"),
        protected_shader_sha256=[digest("pinned")], **options)


def test_retention_preserves_original_current_user_pinned_and_other_lineages():
    records = [view("old-1"), view("old-2"), view("original"), view("current"),
               view("pinned"), view("user-version", source="user"),
               view("another-game", captured="another-original"),
               view("legacy", captured=None), view("vertex", stage="vertex"),
               view("other-slot", bind=4), view("other-space", space=1),
               view("other-layout", layoutSha256=digest("other-layout"))]
    before = copy.deepcopy(records)
    kept, removed = prune(records)
    assert removed == records[:2]
    assert kept == records[2:]
    assert records == before


def test_optional_rollback_is_bounded_with_same_second_revisions():
    records = [view("old-1", updated="2026-10-27T10:00:00Z"),
               view("old-2", updated="2026-10-27T10:00:00Z"), view("pinned")]
    kept, removed = prune(records, keep_previous=1)
    assert kept == records[1:]
    assert removed == records[:1]
    # The rollback cannot grow by repeatedly reviewing the same revision.
    kept_again, removed_again = prune(kept, keep_previous=1)
    assert kept_again == kept
    assert removed_again == []


def setup(tmp_path, monkeypatch):
    root = tmp_path / "aliases"
    root.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: root)
    monkeypatch.setattr(aliases, "user_store_root", lambda: tmp_path / "user")
    members = [{"name": "lights", "offset": 0}]
    key = aliases.layout_sha256("Lights", members)
    return root, members, key


def publish(root, members, key, raw, *, retention=None, source="ai", fields=None):
    if fields is None:
        fields = [{"name": "lightIntensity", "type": "float", "components": [{"offset": 0}],
                   "status": "inferred", "provenance": "shader", "description": str(raw),
                   "evidence": "source/shader.glsl:12 multiplies the light intensity"}]
    return aliases.publish_aliases(
        "Lights", members, [{"path": "lights", "offset": 0, "alias": "Intensity " + str(raw)}],
        source, root=root, block_alias="Lights " + str(raw),
        shader_scope={**scope(raw), "layoutSha256": key}, semantic_fields=fields,
        semantic_source_sha256=digest("source-" + str(raw)),
        semantic_captured_shader_sha256=digest("original"), semantic_retention=retention)


def test_repeated_full_publication_bounds_views_and_matching_aliases(tmp_path, monkeypatch):
    root, members, key = setup(tmp_path, monkeypatch)
    for raw in ("original", "pinned", "old-1", "old-2"):
        publish(root, members, key, raw)
    policy = {"protectedShaderRawSha256": [digest("pinned")], "keepPrevious": 0}
    path = root / (key + ".json")
    for raw in range(20):
        result = publish(root, members, key, raw, retention=policy)
        state = json.loads(path.read_bytes())
        assert {v["shaderScope"]["shaderRawSha256"] for v in state["semanticViews"]} == {
            digest("original"), digest("pinned"), digest(raw)}
        assert len(state["aliases"]) == len(state["blockAliases"]) == 3
        assert result["semanticHistoryCleanup"]["removedViews"] == (2 if raw == 0 else 1)
        assert result["semanticHistoryCleanup"]["removedAliases"] == (2 if raw == 0 else 1)
        assert result["semanticHistoryCleanup"]["remainingViews"] == 3
    assert aliases.read_semantic_view(key, shader_scope={**scope(19), "layoutSha256": key})["fields"][0]["description"] == "19"


def test_partial_and_standalone_publications_do_not_prune_history(tmp_path, monkeypatch):
    root, members, key = setup(tmp_path, monkeypatch)
    for raw in range(5):
        publish(root, members, key, raw)
    path = root / (key + ".json")
    before = path.read_bytes()
    assert len(json.loads(before)["semanticViews"]) == 5
    with pytest.raises(ValueError, match="non-empty"):
        publish(root, members, key, "new", fields=[],
                retention={"protectedShaderRawSha256": []})
    assert path.read_bytes() == before


def test_retained_user_and_other_lineage_views_protect_shared_scoped_alias_rows(tmp_path, monkeypatch):
    root, members, key = setup(tmp_path, monkeypatch)
    publish(root, members, key, "shared")
    publish(root, members, key, "user-view", source="user")
    path = root / (key + ".json")
    payload = json.loads(path.read_bytes())
    shared = copy.deepcopy(payload["semanticViews"][0])
    shared["capturedShaderRawSha256"] = digest("other-original")
    payload["semanticViews"].append(shared)
    path.write_bytes(storage.json_bytes(payload))
    result = publish(root, members, key, "new", retention={"protectedShaderRawSha256": []})
    payload = json.loads(path.read_bytes())
    assert result["semanticHistoryCleanup"]["removedViews"] == 1
    assert result["semanticHistoryCleanup"]["removedAliases"] == 0
    assert result["semanticHistoryCleanup"]["removedBlockAliases"] == 0
    assert any(row["source"] == "user" for row in payload["aliases"])
    assert any(row["source"] == "user" for row in payload["semanticViews"])
    assert any(row["shaderScope"]["shaderRawSha256"] == digest("shared") for row in payload["aliases"])


def test_pruning_frees_ordinary_entry_budget_before_new_names_are_merged(tmp_path, monkeypatch):
    root, members, key = setup(tmp_path, monkeypatch)
    for raw in range(3):
        publish(root, members, key, raw)
    monkeypatch.setattr(aliases, "MAX_ENTRIES", 3)
    result = publish(root, members, key, "new", retention={"protectedShaderRawSha256": []})
    assert result["rejected"] == []
    assert result["written"] == ["Lights.lights"]
    assert result["semanticHistoryCleanup"]["removedViews"] == 3
    assert result["semanticHistoryCleanup"]["remainingViews"] == 1


def test_missing_page_in_obsolete_revision_does_not_need_loading_for_retention(tmp_path, monkeypatch):
    root, members, key = setup(tmp_path, monkeypatch)
    fields = [{"name": "light%d" % i, "type": "float", "components": [{"offset": i * 4}],
               "status": "inferred", "provenance": "shader", "evidence": "Shader light calculation"}
              for i in range(40)]
    publish(root, members, key, "old", fields=fields)
    path = root / (key + ".json")
    page = json.loads(path.read_bytes())["semanticViews"][0]["fieldChunks"][0]["sha256"]
    (root / storage.DIRECTORY / (page + ".json")).unlink()
    result = publish(root, members, key, "new", retention={"protectedShaderRawSha256": []})
    assert result["semanticHistoryCleanup"]["removedViews"] == 1


@pytest.mark.parametrize("policy", [
    {}, {"protectedShaderRawSha256": "not-a-list"},
    {"protectedShaderRawSha256": ["invalid"]},
    {"protectedShaderRawSha256": [], "keepPrevious": 2},
    {"protectedShaderRawSha256": [], "keepPrevious": True},
    {"protectedShaderRawSha256": [], "unexpected": True},
])
def test_invalid_retention_policy_never_changes_persisted_names(tmp_path, monkeypatch, policy):
    root, members, key = setup(tmp_path, monkeypatch)
    publish(root, members, key, "old")
    path = root / (key + ".json")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        publish(root, members, key, "new", retention=policy)
    assert path.read_bytes() == before


def test_invalid_replacement_fields_leave_history_intact(tmp_path, monkeypatch):
    root, members, key = setup(tmp_path, monkeypatch)
    publish(root, members, key, "old")
    path = root / (key + ".json")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        publish(root, members, key, "new", fields=[{"name": "broken", "type": "bad"}],
                retention={"protectedShaderRawSha256": []})
    assert path.read_bytes() == before
