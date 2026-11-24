"""Original/candidate display labels survive RDC-local Shader ID changes."""
import copy
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src.shader_workspace import aliases
from src.shader_workspace.alias_display_mirror import append_display_alias_mirrors, restore_display_alias_mirrors
from src.shader_workspace.alias_display_mirror import DisplayAliasRestoreCache
import test_material_naming_mcp as naming_tests


def block(name="Material", member="tint", scalar="float"):
    return {"block": name, "bind": 4, "space": 0, "byteSize": 32,
            "members": [{"name": member, "offset": 0}, {"name": "extra", "offset": 16}],
            "leaves": [{"path": member, "offset": 0, "byteSize": 16, "baseType": scalar},
                       {"path": "extra", "offset": 16, "byteSize": 16, "baseType": scalar}]}


@pytest.fixture
def case():
    value = naming_tests.CBufferAliasPublicationMCPTests()
    value.setUp()
    value.block = block()
    value.identity.update(rawSha256="a" * 64, blocks=[value.block], shaderResourceId="208035",
        activeDisplayShaderRawSha256="b" * 64, activeDisplayBlocks=[block("MaterialControls", "baseTint")])
    try:
        yield value
    finally:
        value.tearDown()
        value.doCleanups()


def target_scope(case, raw="b" * 64, resource_id="72473"):
    target = case.identity["activeDisplayBlocks"][0]
    return {"shaderRawSha256": raw, "shaderResourceId": resource_id, "stage": "fragment", "bind": 4, "space": 0,
            "layoutSha256": aliases.layout_sha256(target["block"], target["members"])}


def publish(case):
    return case.call_publish([{"bind": 4, "byteOffset": 0, "alias": "baseColor"}], block_alias="Surface")


def test_ordinary_rows_and_block_names_publish_for_exact_active_reflection(case):
    result = publish(case)
    assert "error" not in result, result
    assert not result["partial"] and result["aliasMirrorSkipped"] == []
    assert result["counts"]["writtenRows"] == 1
    assert result["counts"]["writtenDisplayRows"] == 1
    assert result["counts"]["writtenDisplayBlockAliases"] == 1
    assert result["acceptance"]["currentDisplayAliasesAccountedFor"]
    scope = target_scope(case)
    rows = aliases.read_aliases(scope["layoutSha256"], shader_scope=scope)
    assert len(rows) == 1 and rows[0]["path"] == "baseTint" and rows[0]["alias"] == "baseColor"
    assert aliases.read_block_alias(scope["layoutSha256"], shader_scope=scope)["alias"] == "Surface"
    # A recycled live ID cannot make another candidate consume these names.
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope={**scope, "shaderRawSha256": "c" * 64}) == []


def test_mirrored_user_names_win(case):
    target = case.identity["activeDisplayBlocks"][0]
    aliases.publish_aliases(target["block"], target["members"],
        [{"path": "baseTint", "offset": 0, "alias": "userColor"}], "user", block_alias="User Surface")
    result = publish(case)
    assert "error" not in result and not result["partial"], result
    assert result["counts"]["keptUserDisplayRows"] == 1
    assert result["counts"]["keptUserDisplayBlockAliases"] == 1
    assert result["acceptance"]["currentDisplayAliasesAccountedFor"]
    scope = target_scope(case)
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope=scope)[0]["alias"] == "userColor"


@pytest.mark.parametrize("change", ["type", "split", "ambiguous", "missing", "matrix-order"])
def test_alias_mirror_never_guesses_an_incompatible_leaf(case, change):
    target = case.identity["activeDisplayBlocks"][0]
    if change == "type":
        target["leaves"][0]["baseType"] = "uint"
    elif change == "split":
        target["leaves"] = [{"path": "lane%d" % x, "offset": x * 4, "byteSize": 4, "baseType": "float"}
                            for x in range(8)]
    elif change == "ambiguous":
        target["leaves"].append({**target["leaves"][0], "path": "overlap"})
    elif change == "missing":
        target["leaves"] = target["leaves"][1:]
    else:
        case.block["leaves"][0]["componentOffsets"] = [0, 4, 8, 12]
        target["leaves"][0]["componentOffsets"] = [0, 8, 4, 12]
    result = publish(case)
    assert "error" not in result and result["applied"], result
    assert result["partial"] and not result["acceptance"]["currentDisplayAliasesAccountedFor"]
    assert any(row["reason"] == "active-leaf-physical-shape-mismatch" for row in result["aliasMirrorSkipped"])
    scope = target_scope(case)
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope=scope) == []


def test_row_mirror_and_semantic_view_commit_together_without_replacing_each_other(case):
    result = case.call_publish([{"bind": 4, "byteOffset": 0, "alias": "baseColor"}], block_alias="Surface",
        semantic_fields=[{"bind": 4, "name": "baseColor", "type": "float3",
            "components": [{"offset": x} for x in (0, 4, 8)], "status": "inferred", "provenance": "shader",
            "evidence": "RGB color multiplier."}])
    assert "error" not in result and not result["partial"], result
    scope = target_scope(case)
    assert len(aliases.read_aliases(scope["layoutSha256"], shader_scope=scope)) == 1
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)["fields"][0]["name"] == "baseColor"
    assert result["counts"]["writtenSemanticFields"] == 1
    assert result["counts"]["writtenDisplaySemanticFields"] == 1
    assert result["counts"]["writtenDisplayRows"] == 1


def test_original_display_needs_no_mirror(case):
    case.identity["activeDisplayShaderRawSha256"] = "a" * 64
    result = publish(case)
    assert "error" not in result and not result["partial"], result
    assert result["counts"]["requestedDisplayRows"] == 0


@pytest.mark.parametrize("scalar,width", [("float", 4), ("int", 4), ("uint", 4), ("bool", 4),
                                           ("half", 2), ("double", 8)])
def test_mirror_uses_reflected_scalar_shape_for_each_platform_storage_type(scalar, width):
    source, target = block(scalar=scalar), block("Changed", "renamed", scalar)
    for value in (source, target):
        for leaf in value["leaves"]:
            leaf["componentByteSize"] = width
    publications = [{"bind": 4, "space": 0, "block": source["block"], "members": source["members"],
                     "entries": [{"path": "tint", "offset": 0, "alias": "color"}], "blockAlias": "Controls",
                     "shaderScope": {"shaderRawSha256": "a" * 64}}]
    identity = {"rawSha256": "a" * 64, "activeDisplayShaderRawSha256": "b" * 64,
                "blocks": [source], "activeDisplayBlocks": [target], "stage": "fragment", "shaderResourceId": "7"}
    assert append_display_alias_mirrors(identity, publications) == []
    assert publications[-1]["entries"] == [{"path": "renamed", "offset": 0, "alias": "color"}]
    assert publications[-1]["blockAlias"] == "Controls"


@pytest.fixture
def legacy(case, monkeypatch):
    project = Path(case.temporary.name)
    monkeypatch.setattr(aliases, "active_project_root", lambda: project)
    active = {"workspaceId": "project-a", "projectRoot": str(project)}
    service = SimpleNamespace(status=lambda: active, pin_active_project=lambda *args: contextlib.nullcontext())
    case.identity["activeReplacementShaderRawSha256"] = "b" * 64
    loader = mock.Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(case.identity))
    host = SimpleNamespace(_active_project_service=lambda: service,
        _shader_organization_cbuffer_alias_identity=loader,
        _require_same_active_project=mock.Mock())
    status = {"shader": {"rawSha256": "a" * 64}, "latestCompiled": {"rawSha256": "b" * 64},
        "stage": "fragment", "entryPoint": "MainPS", "capture": {"captureId": "capture-a"}}
    source_scope = {**target_scope(case, "a" * 64, "208035"),
        "layoutSha256": aliases.layout_sha256(case.block["block"], case.block["members"])}
    aliases.publish_aliases(case.block["block"], case.block["members"],
        [{"path": "tint", "offset": 0, "alias": "legacyColor"}], block_alias="Legacy Material", shader_scope=source_scope)
    return host, status, project


def test_restore_backfills_old_project_without_alias_knowledge_or_an_origin_capture(case, legacy):
    host, status, project = legacy
    # The old Shader RID and capture are no longer present; only project labels
    # and the target capture's independently verified owner reflection are used.
    case.identity.update(shaderResourceId="72473", eventId=1291)
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert result["status"] == "restored" and result["writtenRows"] == 1 and result["writtenBlocks"] == 1
    scope = target_scope(case)
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope=scope)[0]["alias"] == "legacyColor"
    assert aliases.read_block_alias(scope["layoutSha256"], shader_scope=scope)["alias"] == "Legacy Material"
    assert host._shader_organization_cbuffer_alias_identity.call_count == 1
    second = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert second == {"status": "current", "writtenRows": 0, "writtenBlocks": 0, "skipped": []}
    assert len(list((case.workspace / "ai" / "cbuffer-alias-publications").glob("operation-*"))) == 1


def test_display_metadata_cache_reuses_across_sessions_without_replay_queries(case, legacy):
    host, status, project = legacy
    cache = DisplayAliasRestoreCache(project)
    cache.root = aliases.store_root()
    first = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project,
                                          metadata_cache=cache)
    assert first["status"] == "restored"
    host._shader_organization_cbuffer_alias_identity.reset_mock()
    fresh_cache = DisplayAliasRestoreCache(project)
    fresh_cache.root = cache.root
    second = restore_display_alias_mirrors(host, case.workspace, status, "new-session", project,
                                           metadata_cache=fresh_cache)
    assert second["metadataCacheHit"] and second["writtenRows"] == 0
    assert second["writtenBlocks"] == 0
    host._shader_organization_cbuffer_alias_identity.assert_not_called()


@pytest.mark.parametrize("change", ["source", "candidate", "removed-view", "corrupt-cache"])
def test_display_metadata_cache_invalidates_relevant_inputs(case, legacy, change):
    host, status, project = legacy
    cache = DisplayAliasRestoreCache(project)
    cache.root = aliases.store_root()
    restore_display_alias_mirrors(host, case.workspace, status, "session-a", project, metadata_cache=cache)
    assert cache.read(case.workspace, status)["metadataCacheHit"]
    if change == "source":
        status["source"] = {"currentSha256": "f" * 64}
    elif change == "candidate":
        status["latestCompiled"]["rawSha256"] = "e" * 64
    elif change == "removed-view":
        aliases.layout_alias_path(target_scope(case)["layoutSha256"]).unlink()
    else:
        cache._path(case.workspace).write_text('{"schema":', encoding="utf-8")
    assert cache.read(case.workspace, status) is None


def test_display_metadata_cache_tracks_page_changes_and_ignores_other_shader_names(tmp_path):
    import hashlib
    import json
    import os
    cache = DisplayAliasRestoreCache(tmp_path)
    cache.root.mkdir(parents=True)
    page = b'[]'
    digest = hashlib.sha256(page).hexdigest()
    directory = cache.root / "semantic-fields"
    directory.mkdir()
    target = directory / (digest + ".json")
    target.write_bytes(page)
    view = {"source": "ai", "shaderScope": {"shaderRawSha256": "a" * 64},
            "fieldCount": 1, "fieldChunks": [{"sha256": digest, "count": 1, "sizeBytes": len(page)}]}
    manifest = cache.root / "test.json"
    manifest.write_text(json.dumps({"semanticViews": [view]}), encoding="utf-8")
    status = {"shader": {"rawSha256": "a" * 64}, "latestCompiled": {"rawSha256": "b" * 64}}
    cache.remember(tmp_path / "workspace", status, {"status": "current"})
    assert cache.read(tmp_path / "workspace", status)["metadataCacheHit"]
    (cache.root / "unrelated.json").write_text(json.dumps({"semanticViews": [
        {"source": "ai", "shaderScope": {"shaderRawSha256": "c" * 64}, "fields": []}]}), encoding="utf-8")
    assert cache.read(tmp_path / "workspace", status)["metadataCacheHit"]
    stamp = target.stat()
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    assert cache.read(tmp_path / "workspace", status) is None


def test_restore_reuses_admitted_project_and_only_checks_selection_at_commit(case, legacy):
    host, status, project = legacy
    service = host._active_project_service()
    active = service.status()
    snapshot = object()
    service.status = mock.Mock(side_effect=AssertionError("no complete project scan per Shader"))
    service.pin_active_project = mock.Mock(side_effect=AssertionError("no long selection lock"))
    service.snapshot_active_project = mock.Mock(return_value=contextlib.nullcontext(snapshot))
    service.assert_active_project_snapshot = mock.Mock()
    service.commit_active_project_snapshot = mock.Mock(return_value=contextlib.nullcontext())
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project,
                                           active_project=active)
    assert result["status"] == "restored"
    service.commit_active_project_snapshot.assert_called_once_with(snapshot)
    service.assert_active_project_snapshot.assert_called_with(snapshot)
    host._require_same_active_project.assert_not_called()


@pytest.mark.parametrize("source", ["ai", "user"])
def test_backfill_preserves_newer_candidate_and_user_labels(case, legacy, source):
    host, status, project = legacy
    target = case.identity["activeDisplayBlocks"][0]
    aliases.publish_aliases(target["block"], target["members"],
        [{"path": "baseTint", "offset": 0, "alias": "currentColor"}], source,
        block_alias="Current Material", shader_scope=target_scope(case))
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert result["status"] == "current" and result["writtenRows"] == result["writtenBlocks"] == 0
    scope = target_scope(case)
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope=scope)[0]["alias"] == "currentColor"
    assert aliases.read_block_alias(scope["layoutSha256"], shader_scope=scope)["alias"] == "Current Material"


@pytest.mark.parametrize("change", ["capture", "owner", "project"])
def test_backfill_rejects_changed_current_ownership_before_names_are_written(case, legacy, change):
    host, status, project = legacy
    if change == "capture":
        case.identity["captureSessionId"] = "other-session"
    elif change == "owner":
        case.identity["activeReplacementShaderRawSha256"] = "c" * 64
    else:
        host._require_same_active_project.side_effect = RuntimeError("project changed")
    with pytest.raises(RuntimeError):
        restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    assert aliases.read_aliases(scope["layoutSha256"], shader_scope=scope) == []


def test_backfill_does_not_guess_changed_physical_lane_type(case, legacy):
    host, status, project = legacy
    case.identity["activeDisplayBlocks"][0]["leaves"][0]["baseType"] = "uint"
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert result["status"] == "partial" and result["writtenRows"] == result["writtenBlocks"] == 0
    assert result["skipped"]


@pytest.mark.parametrize("cached_id", ["", "0" * 64])
def test_backfill_observes_promoted_catalog_inside_real_editor_batch_cache(case, legacy, monkeypatch, cached_id):
    from src import server
    host, status, project = legacy
    capture_id, session_id = "d" * 64, "e" * 32
    case.identity.update(captureId=capture_id, captureSessionId=session_id)
    status["capture"]["captureId"] = capture_id
    owner_check = mock.Mock()
    context = {"schema": "renderdoc-ultra-current-context/v1", "schemaVersion": 1,
               "capture": {"loaded": True, "captureId": cached_id, "captureSessionId": session_id}}
    admitted = (context, owner_check)
    host._RESTORED_SHADER_EDITOR_CONTEXT = server._RESTORED_SHADER_EDITOR_CONTEXT
    live = copy.deepcopy(context)
    live["capture"]["captureId"] = capture_id
    transport = mock.Mock(return_value=live)
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=transport))
    def load_after_catalog_promotion(*args, **kwargs):
        # Use the production strict context reader: the old request cache has
        # no catalog or the previous export, while the Bridge now has the new one.
        actual = server._active_live_context()
        assert actual["capture"]["captureId"] == capture_id
        return copy.deepcopy(case.identity)
    host._shader_organization_cbuffer_alias_identity.side_effect = load_after_catalog_promotion
    token = server._RESTORED_SHADER_EDITOR_CONTEXT.set(admitted)
    try:
        result = restore_display_alias_mirrors(host, case.workspace, status, session_id, project)
        assert result["status"] == "restored" and result["writtenRows"] == 1
        assert server._RESTORED_SHADER_EDITOR_CONTEXT.get() is admitted
    finally:
        server._RESTORED_SHADER_EDITOR_CONTEXT.reset(token)
    owner_check.assert_called_once()
    transport.assert_called_once_with("get_current_renderdoc_context", {"identity_only": True}, timeout=60.0)


def test_failed_alias_lookup_restores_editor_cache_without_relaxing_owner_checks(case, legacy):
    from src import server
    host, status, project = legacy
    owner_check = mock.Mock()
    admitted = ({"capture": {"captureId": ""}}, owner_check)
    host._RESTORED_SHADER_EDITOR_CONTEXT = server._RESTORED_SHADER_EDITOR_CONTEXT
    host._shader_organization_cbuffer_alias_identity.side_effect = RuntimeError("capture changed")
    token = server._RESTORED_SHADER_EDITOR_CONTEXT.set(admitted)
    try:
        with pytest.raises(RuntimeError, match="capture changed"):
            restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
        assert server._RESTORED_SHADER_EDITOR_CONTEXT.get() is admitted
        owner_check.side_effect = RuntimeError("owner retired")
        host._shader_organization_cbuffer_alias_identity.reset_mock()
        with pytest.raises(RuntimeError, match="owner retired"):
            restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
        host._shader_organization_cbuffer_alias_identity.assert_not_called()
        assert server._RESTORED_SHADER_EDITOR_CONTEXT.get() is admitted
    finally:
        server._RESTORED_SHADER_EDITOR_CONTEXT.reset(token)


@pytest.fixture
def semantic_legacy(case, legacy):
    from src.shader_workspace.semantic_usage import SCHEMA
    host, status, project = legacy
    status["source"] = {"currentSha256": "f" * 64}
    status["latestCompiled"]["sourceSha256"] = "f" * 64
    source_scope = {**target_scope(case, "a" * 64, "208035"),
        "layoutSha256": aliases.layout_sha256(case.block["block"], case.block["members"])}
    fields = [{"name": "baseColor", "type": "float3", "components": [{"offset": n} for n in (0, 4, 8)],
               "status": "inferred", "provenance": "shader", "evidence": "RGB material multiplier."}]
    usage = {"schema": SCHEMA, "unusedComponents": [{"offset": n, "type": "float"} for n in (12, 16, 20, 24, 28)],
             "evidence": "Remaining typed lanes have no source consumers."}
    aliases.publish_aliases(case.block["block"], case.block["members"], [], shader_scope=source_scope,
        semantic_fields=fields, semantic_source_sha256="f" * 64, semantic_captured_shader_sha256="a" * 64,
        semantic_usage_review=usage)
    return host, status, project, source_scope, fields, usage


def test_same_source_recompile_restores_semantic_groups_and_unused_into_new_candidate(case, semantic_legacy):
    host, status, project, source_scope, fields, usage = semantic_legacy
    # The project has a previous candidate view, but compiling in this capture
    # emits different binary bytes. Never use its live ResourceId as identity.
    old_scope = target_scope(case)
    target = case.identity["activeDisplayBlocks"][0]
    aliases.publish_aliases(target["block"], target["members"], [], shader_scope=old_scope,
        semantic_fields=fields, semantic_source_sha256="f" * 64, semantic_captured_shader_sha256="a" * 64,
        semantic_usage_review=usage)
    case.identity.update(activeDisplayShaderRawSha256="c" * 64, activeReplacementShaderRawSha256="c" * 64,
                         shaderResourceId="72473")
    status["latestCompiled"]["rawSha256"] = "c" * 64
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert result["status"] == "restored"
    scope = target_scope(case, "c" * 64)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    assert current["fields"] == [{**fields[0], "description": ""}]
    assert current["usageReview"] == usage
    assert current["sourceSha256"] == "f" * 64 and current["capturedShaderRawSha256"] == "a" * 64
    assert current["shaderScope"]["shaderRawSha256"] == "c" * 64
    assert aliases.read_semantic_view(old_scope["layoutSha256"], shader_scope=old_scope)["fields"] == current["fields"]
    assert restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)["status"] == "current"


@pytest.mark.parametrize("source", ["ai", "user"])
def test_semantic_restore_preserves_current_candidate_or_user_view(case, semantic_legacy, source):
    host, status, project, unused_scope, fields, usage = semantic_legacy
    target, scope = case.identity["activeDisplayBlocks"][0], target_scope(case)
    aliases.publish_aliases(target["block"], target["members"], [], source=source, shader_scope=scope,
        semantic_fields=[{**fields[0], "name": "currentTint"}], semantic_source_sha256="f" * 64,
        semantic_captured_shader_sha256="a" * 64)
    restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    assert current["fields"][0]["name"] == "currentTint" and current["source"] == source
    assert "usageReview" not in current


@pytest.mark.parametrize("change", ["type", "offset", "matrix-order", "unknown-original", "incomplete"])
def test_semantic_restore_requires_exact_typed_packing(case, semantic_legacy, change):
    host, status, project, unused_scope, fields, usage = semantic_legacy
    target = case.identity["activeDisplayBlocks"][0]
    if change == "type":
        target["leaves"][0]["baseType"] = "uint"
    elif change == "offset":
        target["leaves"][0]["offset"] = 4
    elif change == "matrix-order":
        case.block["leaves"][0]["componentOffsets"] = [0, 4, 8, 12]
        target["leaves"][0]["componentOffsets"] = [0, 8, 4, 12]
    elif change == "unknown-original":
        case.block["leaves"][0].pop("baseType")
    elif change == "incomplete":
        target["memberMappingComplete"] = False
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    assert result["status"] == "partial" and result["skipped"]
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope) is None


@pytest.mark.parametrize("change", ["source", "includes"])
def test_calculation_edit_preserves_parameter_names_but_not_stale_usage_claims(case, semantic_legacy, change):
    host, status, project, original_scope, fields, usage = semantic_legacy
    if change == "source":
        status["source"]["currentSha256"] = status["latestCompiled"]["sourceSha256"] = "e" * 64
    else:
        status["latestCompiled"]["sourceDependencies"] = {"documents": [
            {"sourcePath": "shader.hlsl"}, {"sourcePath": "lighting.hlsli"}]}
    original = aliases.read_semantic_view(original_scope["layoutSha256"], shader_scope=original_scope)
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    current_scope = target_scope(case)
    current = aliases.read_semantic_view(current_scope["layoutSha256"], shader_scope=current_scope)
    assert result["status"] == "restored"
    assert current["fields"] == original["fields"]
    assert current["sourceSha256"] == "f" * 64  # Naming evidence retains its actual source.
    assert "usageReview" not in current
    assert aliases.read_semantic_view(original_scope["layoutSha256"], shader_scope=original_scope) == original


@pytest.mark.parametrize("api,reflected_size", [("D3D11", 16), ("D3D12", 16),
                                                ("OpenGL", 32), ("OpenGLES", 32), ("Vulkan", 16)])
def test_compiler_trim_keeps_names_on_surviving_typed_lanes(case, semantic_legacy, api, reflected_size):
    host, status, project, original_scope, fields, usage = semantic_legacy
    case.identity["api"] = api
    target = case.identity["activeDisplayBlocks"][0]
    target.update(byteSize=reflected_size, members=target["members"][:1], leaves=target["leaves"][:1])
    original = aliases.read_semantic_view(original_scope["layoutSha256"], shader_scope=original_scope)
    restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    assert current["fields"] == original["fields"]
    assert "usageReview" not in current  # Trim is not fresh evidence of non-use.
    assert current["sourceSha256"] == original["sourceSha256"]
    assert aliases.read_semantic_view(original_scope["layoutSha256"], shader_scope=original_scope) == original


def test_partial_vector_is_omitted_whole_while_other_fields_survive_trim(case, semantic_legacy):
    host, status, project, original_scope, fields, usage = semantic_legacy
    fields.append({**fields[0], "name": "roughness", "type": "float", "components": [{"offset": 16}]})
    aliases.publish_aliases(case.block["block"], case.block["members"], [], shader_scope=original_scope,
        semantic_fields=fields, semantic_source_sha256="f" * 64, semantic_captured_shader_sha256="a" * 64)
    target = case.identity["activeDisplayBlocks"][0]
    target["byteSize"] = 20
    target["leaves"][0]["byteSize"] = 8
    target["leaves"][1]["byteSize"] = 4
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    assert [field["name"] for field in current["fields"]] == ["roughness"]
    assert "usageReview" not in current
    assert {"bind": 4, "space": 0, "reason": "semantic-fields-not-fully-reflected", "fieldCount": 1} in result["skipped"]


@pytest.mark.parametrize("change", ["type", "width", "new-lane", "overlap", "extent-growth", "matrix-order"])
def test_compiler_trim_does_not_accept_changed_or_ambiguous_typed_layout(case, semantic_legacy, change):
    host, status, project, original_scope, fields, usage = semantic_legacy
    target = case.identity["activeDisplayBlocks"][0]
    target.update(byteSize=16, members=target["members"][:1], leaves=target["leaves"][:1])
    if change == "type":
        target["leaves"][0]["baseType"] = "uint"
    elif change == "width":
        target["leaves"][0].update(baseType="half", componentByteSize=2)
    elif change == "new-lane":
        case.block["leaves"][0]["byteSize"] = 12  # Candidate now occupies original padding.
    elif change == "overlap":
        target["leaves"].append({**target["leaves"][0], "path": "ambiguous"})
    elif change == "extent-growth":
        target["byteSize"] = 48
    else:
        case.block["leaves"][0]["componentOffsets"] = [0, 4, 8, 12]
        target["leaves"][0]["componentOffsets"] = [0, 8, 4, 12]
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    assert result["status"] == "partial"
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope) is None


@pytest.mark.parametrize("trim_matrix", [False, True])
def test_matrix_metadata_must_survive_for_any_retained_matrix_lane(case, semantic_legacy, trim_matrix):
    host, status, project, original_scope, fields, usage = semantic_legacy
    # The unrelated trailing matrix may disappear entirely, but retaining its
    # bytes without the same logical component order is not a valid projection.
    case.block["leaves"][1]["componentOffsets"] = [0, 8, 4, 12]
    target = case.identity["activeDisplayBlocks"][0]
    if trim_matrix:
        target.update(byteSize=16, members=target["members"][:1], leaves=target["leaves"][:1])
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    if trim_matrix:
        assert current["fields"][0]["name"] == "baseColor"
    else:
        assert current is None and result["status"] == "partial"


def test_ephemeral_debug_never_publishes_names_for_its_instrumented_candidate(case, semantic_legacy):
    host, status, project, original_scope, fields, usage = semantic_legacy
    case.identity.update(activeDisplayEphemeral=True, activeDisplayShaderRawSha256="d" * 64)
    before = {p.name: p.read_bytes() for p in aliases.store_root().glob("*.json")}
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert result["status"] == "not-required" and result["reason"] == "transient-visual-debug"
    assert {p.name: p.read_bytes() for p in aliases.store_root().glob("*.json")} == before


@pytest.mark.parametrize("changed", [None, "project", "code", "abi", "source", "entry", "profile"])
def test_other_rdc_raw_identity_reuses_only_verified_project_program_lineage(case, semantic_legacy, changed):
    host, status, project, original_scope, fields, usage = semantic_legacy
    original_view = aliases.read_semantic_view(original_scope["layoutSha256"], shader_scope=original_scope)
    status["shader"]["rawSha256"] = case.identity["rawSha256"] = "c" * 64
    status["baseline"] = {"rawSha256": "c" * 64, "codeSha256": "7" * 64, "abiSha256": "8" * 64}
    status["targetProfile"] = {"profile": "ps_5_0"}
    receipt = {"schema": "replaylab-verified-project-shader-source/v1", "originalRawSha256": "c" * 64,
               "baselineAbiSha256": "8" * 64, "projectId": "project", "knowledgeId": "knowledge",
               "originWorkspaceId": "old-workspace", "sourceSha256": "f" * 64}
    status["source"]["projectSource"] = receipt
    knowledge = {"rawSha256": "a" * 64, "codeSha256": "7" * 64, "baselineAbiSha256": "8" * 64,
                 "workspaceId": "old-workspace", "sourceSha256": "f" * 64, "stage": "fragment",
                 "entryPoint": status["entryPoint"], "targetProfile": status["targetProfile"]}
    keys = {"code": "codeSha256", "abi": "baselineAbiSha256", "source": "sourceSha256",
            "entry": "entryPoint", "profile": "targetProfile"}
    if changed in keys:
        knowledge[keys[changed]] = "different"
    replay = SimpleNamespace(_load=lambda: {"projectId": "other" if changed == "project" else "project"},
        _workspace_knowledge_from_manifest=lambda manifest, token: knowledge)
    host._active_project_service().replay_service = lambda: replay
    restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    scope = target_scope(case)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    if changed is None:
        assert current["fields"] == original_view["fields"]
        assert current["capturedShaderRawSha256"] == "c" * 64
    else:
        assert current is None
    assert aliases.read_semantic_view(original_scope["layoutSha256"], shader_scope=original_scope) == original_view


def test_wholly_unused_view_reads_and_restores_without_inventing_names(case, semantic_legacy):
    host, status, project, source_scope, unused_fields, usage = semantic_legacy
    usage["unusedComponents"] = [{"offset": n, "type": "float"} for n in range(0, 32, 4)]
    aliases.publish_aliases(case.block["block"], case.block["members"], [], shader_scope=source_scope,
        semantic_fields=[], semantic_source_sha256="f" * 64, semantic_captured_shader_sha256="a" * 64,
        semantic_usage_review=usage)
    assert aliases.read_semantic_view(source_scope["layoutSha256"], shader_scope=source_scope)["fields"] == []
    result = restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert result["status"] == "restored"
    scope = target_scope(case)
    current = aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)
    assert current["fields"] == [] and current["usageReview"] == usage


def test_semantic_backfill_cannot_overwrite_a_review_published_while_it_was_planned(case, semantic_legacy, monkeypatch):
    from src.shader_workspace import cbuffer_alias_publication
    host, status, project, unused_scope, fields, usage = semantic_legacy
    publish_transaction = cbuffer_alias_publication.publish_transaction
    scope, target = target_scope(case), case.identity["activeDisplayBlocks"][0]
    def concurrent_publication(**kwargs):
        aliases.publish_aliases(target["block"], target["members"], [], shader_scope=scope,
            semantic_fields=[{**fields[0], "name": "reviewedTint"}], semantic_source_sha256="f" * 64,
            semantic_captured_shader_sha256="a" * 64)
        return publish_transaction(**kwargs)
    monkeypatch.setattr(cbuffer_alias_publication, "publish_transaction", concurrent_publication)
    with pytest.raises(RuntimeError, match="semantic view changed"):
        restore_display_alias_mirrors(host, case.workspace, status, "session-a", project)
    assert aliases.read_semantic_view(scope["layoutSha256"], shader_scope=scope)["fields"][0]["name"] == "reviewedTint"
