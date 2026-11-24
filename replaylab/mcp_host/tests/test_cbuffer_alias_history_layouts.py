"""Renamed reflected layouts do not keep every obsolete AI naming revision."""

import copy
import hashlib
import json

import pytest

from src.shader_workspace import aliases, alias_history, alias_field_storage as storage


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def publication(raw, *, block=None, captured="original", pinned=()):
    name = block or ("Block" + str(raw))
    members = [{"name": "value" + str(raw), "offset": 0}]
    key = aliases.layout_sha256(name, members)
    return {"block": name, "members": members, "entries": [],
            "shaderScope": {"shaderRawSha256": digest(raw), "stage": "fragment", "bind": 2,
                            "space": 0, "layoutSha256": key},
            "semanticFields": [{"name": "intensity", "type": "float", "components": [{"offset": 0}],
                                "status": "inferred", "provenance": "shader", "evidence": "Light calculation"}],
            "semanticCapturedShaderSha256": digest(captured),
            "semanticRetention": {"protectedShaderRawSha256": [digest(v) for v in pinned], "keepPrevious": 0}}


def write(root, item, *, source="ai"):
    return aliases._publish_aliases_unlocked(
        item["block"], item["members"], [{"path": item["members"][0]["name"], "offset": 0, "alias": "Intensity"}],
        source, root=root, store_kind="project", shader_scope=item["shaderScope"],
        semantic_fields=item["semanticFields"], semantic_captured_shader_sha256=item["semanticCapturedShaderSha256"])


def test_cross_layout_cleanup_preserves_user_original_pins_and_unrelated_shader(tmp_path):
    root = tmp_path / "aliases"
    root.mkdir()
    for item in (publication("old-1"), publication("old-2"), publication("original"),
                 publication("pinned"), publication("other-game", captured="other-original")):
        write(root, item)
    user = publication("old-1")
    write(root, user, source="user")
    user_path = root / (user["shaderScope"]["layoutSha256"] + ".json")
    original_user = [v for v in json.loads(user_path.read_bytes())["semanticViews"] if v["source"] == "user"]
    plans = alias_history.discover_history_publications(root, [publication("new", pinned=["pinned"])])
    assert {plan["block"] for plan in plans} == {"Blockold-1", "Blockold-2"}
    for plan in plans:
        result = alias_history.prune_history_file(root / (plan["layoutSha256"] + ".json"), plan)
        assert result["semanticHistoryCleanup"]["removedViews"] == 1
    assert json.loads(user_path.read_bytes())["semanticViews"] == original_user
    for raw in ("original", "pinned", "other-game"):
        key = publication(raw)["shaderScope"]["layoutSha256"]
        assert len(json.loads((root / (key + ".json")).read_bytes())["semanticViews"]) == 1
    assert alias_history.discover_history_publications(root, [publication("new", pinned=["pinned"])]) == []


def test_newly_published_scopes_and_current_layouts_are_not_cleanup_targets(tmp_path):
    current = publication("current")
    mirror = publication("mirror")
    old = publication("old")
    for item in (current, mirror, old):
        write(tmp_path, item)
    # An old-layout view of the same currently published candidate also stays.
    cross = copy.deepcopy(current)
    cross["block"] = "OlderReflection"
    cross["shaderScope"]["layoutSha256"] = aliases.layout_sha256(cross["block"], cross["members"])
    write(tmp_path, cross)
    plans = alias_history.discover_history_publications(tmp_path, [current, mirror])
    assert [p["block"] for p in plans] == [old["block"]]


def test_shared_scope_other_lineage_preserves_ordinary_names_and_unknown_metadata(tmp_path):
    old = publication("old")
    write(tmp_path, old)
    path = tmp_path / (old["shaderScope"]["layoutSha256"] + ".json")
    payload = json.loads(path.read_bytes())
    other = copy.deepcopy(payload["semanticViews"][0])
    other["capturedShaderRawSha256"] = digest("another-original")
    other["customNote"] = "Keep this metadata exactly"
    payload["semanticViews"].append(other)
    payload["extensionData"] = {"retained": True}
    path.write_bytes(storage.json_bytes(payload))
    plan = alias_history.discover_history_publications(tmp_path, [publication("new")])[0]
    result = alias_history.prune_history_file(path, plan)
    updated = json.loads(path.read_bytes())
    assert result["semanticHistoryCleanup"]["removedAliases"] == 0
    assert updated["aliases"] == payload["aliases"]
    assert updated["semanticViews"] == [other]
    assert updated["extensionData"] == payload["extensionData"]


def test_cleanup_only_staging_does_not_touch_original_or_field_pages(tmp_path):
    original = tmp_path / "original"
    stage = tmp_path / "stage"
    original.mkdir()
    stage.mkdir()
    old = publication("old")
    fields = old["semanticFields"]
    old["semanticFields"] = [{**fields[0], "name": "field%d" % i, "components": [{"offset": i * 4}]}
                             for i in range(40)]
    write(original, old)
    plan = alias_history.discover_history_publications(original, [publication("new")])[0]
    path = original / (plan["layoutSha256"] + ".json")
    before = path.read_bytes()
    pages = {p.name: p.read_bytes() for p in (original / storage.DIRECTORY).iterdir()}
    staged = stage / path.name
    staged.write_bytes(before)
    alias_history.prune_history_file(staged, plan)
    assert path.read_bytes() == before
    assert json.loads(staged.read_bytes())["semanticViews"] == []
    assert {p.name: p.read_bytes() for p in (original / storage.DIRECTORY).iterdir()} == pages


def test_discovery_skips_uncertain_manifests_and_still_cleans_other_valid_history(tmp_path):
    valid, bad_user = publication("valid-old"), publication("bad-user-old")
    write(tmp_path, valid)
    write(tmp_path, bad_user)
    bad_user_path = tmp_path / (bad_user["shaderScope"]["layoutSha256"] + ".json")
    payload = json.loads(bad_user_path.read_bytes())
    invalid = copy.deepcopy(payload["semanticViews"][0])
    invalid.update(source="user", fieldChunks=[{}], fieldCount=1)
    invalid.pop("fields")
    payload["semanticViews"].append(invalid)
    bad_user_path.write_bytes(storage.json_bytes(payload))
    bad_before = bad_user_path.read_bytes()
    unreadable = tmp_path / (digest("unreadable") + ".json")
    unreadable.write_text("invalid JSON", encoding="utf-8")
    diagnostics = []
    plans = alias_history.discover_history_publications(tmp_path, [publication("new")], diagnostics=diagnostics)
    assert [plan["block"] for plan in plans] == [valid["block"]]
    assert {row["layoutSha256"] for row in diagnostics} == {bad_user_path.stem, unreadable.stem}
    assert bad_user_path.read_bytes() == bad_before
    assert unreadable.read_text(encoding="utf-8") == "invalid JSON"


@pytest.mark.parametrize("damage", ["layout", "members", "user-page", "records"])
def test_invalid_manifest_data_does_not_get_silently_rewritten(tmp_path, damage):
    old = publication("old")
    write(tmp_path, old)
    path = tmp_path / (old["shaderScope"]["layoutSha256"] + ".json")
    plan = alias_history.discover_history_publications(tmp_path, [publication("new")])[0]
    payload = json.loads(path.read_bytes())
    if damage == "layout":
        payload["layoutSha256"] = digest("wrong")
    elif damage == "members":
        payload["members"][0]["offset"] = 4
    elif damage == "user-page":
        user = copy.deepcopy(payload["semanticViews"][0])
        user.update(source="user", fieldChunks=[{"sha256": "invalid"}], fieldCount=1)
        user.pop("fields")
        payload["semanticViews"].append(user)
    else:
        payload["aliases"].append("invalid")
    path.write_bytes(storage.json_bytes(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        alias_history.prune_history_file(path, plan)
    assert path.read_bytes() == before
