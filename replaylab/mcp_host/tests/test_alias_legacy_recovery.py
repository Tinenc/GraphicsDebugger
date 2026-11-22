"""Legacy naming evidence is reusable without reviving stale capture handles."""

import json

import pytest

from src import projects
from src.shader_workspace import aliases
import test_project_alias_rebind as rebind_tests
from test_project_alias_rebind import _dxbc


@pytest.fixture
def alias_store(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAYLAB_ALIAS_ROOT", str(tmp_path))
    monkeypatch.setenv("REPLAYLAB_ALIAS_PROJECT_ROOT", str(tmp_path))
    members = [{"name": "unknown", "offset": 0}]
    key = aliases.layout_sha256("cbuffer5", members)
    scope = {"shaderRawSha256": "a" * 64, "stage": "fragment",
             "bind": 5, "space": 0, "layoutSha256": key}
    return members, key, scope


@pytest.mark.parametrize("old_id", [None, "111"])
def test_original_program_names_survive_missing_or_reassigned_resource_id(alias_store, old_id):
    members, key, scope = alias_store
    old_scope = dict(scope)
    if old_id is not None:
        old_scope["shaderResourceId"] = old_id
    aliases.publish_aliases("cbuffer5", members,
                            [{"path": "unknown[2]", "offset": 32, "alias": "roughness"}],
                            shader_scope=old_scope, block_alias="Material")
    current = dict(scope, shaderResourceId="222")
    assert [item["alias"] for item in aliases.read_aliases(key, shader_scope=current)] == ["roughness"]
    assert aliases.read_block_alias(key, shader_scope=current)["alias"] == "Material"

    # Fresh publication updates the same portable record; it does not append a
    # duplicate every time RenderDoc changes its local ResourceId.
    aliases.publish_aliases("cbuffer5", members,
                            [{"path": "unknown[2]", "offset": 32, "alias": "roughnessValue"}],
                            shader_scope=current, block_alias="Surface")
    saved = json.loads(aliases.layout_alias_path(key).read_text(encoding="utf-8"))
    assert len(saved["aliases"]) == 1
    assert len(saved["blockAliases"]) == 1
    assert saved["aliases"][0]["shaderScope"]["shaderResourceId"] == "222"
    assert aliases.read_aliases(key, shader_scope=dict(current, shaderRawSha256="b" * 64)) == []
    assert aliases.read_aliases(key, shader_scope=dict(current, bind=4)) == []
    assert aliases.read_aliases(key, shader_scope=dict(current, stage="vertex")) == []


def test_organization_reads_published_placeholder_names_using_current_reflection(alias_store):
    members, key, scope = alias_store
    aliases.publish_aliases("cbuffer5", members,
                            [{"path": "unknown[2]", "offset": 32, "alias": "roughness"}],
                            shader_scope=scope, block_alias="Material")
    identity = {
        "rawSha256": scope["shaderRawSha256"], "stage": "fragment",
        "shaderResourceId": 222,
        "blocks": [{"block": "cbuffer5", "bind": 5, "space": 0, "members": members,
                    "memberMappingAvailable": True,
                    "leaves": [{"path": "unknown[2]", "offset": 32, "byteSize": 16}]}],
    }
    source = "cbuffer generated : register(b5) { float4 v2 : packoffset(c2); };"
    evidence = aliases.evidence_for_source(source, identity)["blocks"][0]
    assert evidence["ai"] == [{"path": "v2", "alias": "roughness"}]
    assert evidence["blockAlias"]["alias"] == "Material"
    assert "aiSuppressedReasonId" not in evidence
    identity["rawSha256"] = "b" * 64
    assert aliases.evidence_for_source(source, identity)["blocks"][0]["ai"] == []
    identity.pop("rawSha256")
    evidence = aliases.evidence_for_source(source, identity)["blocks"][0]
    assert evidence["ai"] == []
    assert evidence["aiSuppressedReasonId"] == aliases.UNSCOPED_PLACEHOLDER_LAYOUT


@pytest.fixture
def project_fixture():
    fixture = rebind_tests.ProjectAliasRebindTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_legacy_missing_id_registers_and_rebinds_without_rescanning_assets(project_fixture):
    fixture = project_fixture
    blob = _dxbc(b"legacy-program", b"reflection")
    source, shader, capture = fixture._catalog("source", blob, resource_id=111)
    target, _, _ = fixture._catalog("target", blob, resource_id=222)
    fixture.service.import_capture(source)
    registered = fixture._register_aliases(capture, shader, [{
        "shaderResourceId": None, "bindingKind": "constantBufferVariable",
        "canonicalPath": "Material.Roughness", "alias": "Roughness",
    }])
    assert registered["knowledge"]["aliasCount"] == 1
    assert registered["aliasRecovery"][0]["status"] == "repaired"
    assert registered["aliasRecovery"][0]["shaderResourceId"] == 111
    original = fixture.root / "aliases-0.json"
    before = original.read_bytes()

    materialized = fixture._materialize(fixture.service.import_capture(target))
    alias = materialized["aliasTable"]["aliases"][0]
    assert alias["shaderResourceId"] == 222
    assert alias["alias"] == "Roughness"
    assert materialized["aliasRecovery"][registered["knowledge"]["knowledgeId"]][0]["status"] == "repaired"
    assert original.read_bytes() == before
    assert json.loads(before)["aliases"][0]["shaderResourceId"] is None


def test_unresolvable_legacy_row_does_not_discard_valid_names(project_fixture):
    fixture = project_fixture
    blob = _dxbc(b"legacy-program", b"reflection")
    source, shader, capture = fixture._catalog("source", blob, resource_id=111)
    target, _, _ = fixture._catalog("target", blob, resource_id=222)
    fixture.service.import_capture(source)
    registered = fixture._register_aliases(capture, shader, [
        {"bindingKind": "shader", "alias": "ValidName"},
        {"shaderResourceId": None, "rawSha256": "f" * 64,
         "bindingKind": "shader", "alias": "UnrelatedOldName"},
    ])
    assert registered["knowledge"]["aliasCount"] == 1
    assert registered["aliasRecovery"][0]["status"] == "skipped"
    assert registered["aliasRecovery"][0]["candidateCount"] == 0
    result = fixture._materialize(fixture.service.import_capture(target))
    assert [row["alias"] for row in result["aliasTable"]["aliases"]] == ["ValidName"]
    assert len(json.loads((fixture.root / "aliases-0.json").read_text())["aliases"]) == 2


def test_ambiguous_missing_id_is_reported_without_registering_empty_knowledge(project_fixture):
    fixture = project_fixture
    source, shaders, capture = fixture._catalog_many("source", _dxbc(), [111, 112])
    fixture.service.import_capture(source)
    registered = fixture._register_aliases(capture, shaders[0], [{
        "shaderResourceId": None, "bindingKind": "shader", "alias": "AmbiguousName",
    }])
    assert registered["registered"] is False
    assert registered["knowledge"] is None
    assert registered["aliasRecovery"][0]["candidateCount"] == 2
    assert registered["aliasRecovery"][0]["status"] == "skipped"


def test_explicit_wrong_id_or_capture_is_never_silently_repaired(project_fixture):
    fixture = project_fixture
    source, shader, capture = fixture._catalog("source", _dxbc(), resource_id=111)
    fixture.service.import_capture(source)
    with pytest.raises(projects.ReplayProjectError):
        fixture._register_aliases(capture, shader, [{
            "shaderResourceId": 222, "bindingKind": "shader", "alias": "WrongTarget",
        }])
    with pytest.raises(projects.ReplayProjectError):
        fixture._register_aliases(capture, shader, [{
            "shaderResourceId": None, "captureId": "f" * 64,
            "bindingKind": "shader", "alias": "WrongCapture",
        }])
