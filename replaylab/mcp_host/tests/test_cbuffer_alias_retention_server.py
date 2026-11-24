"""The shared Host naming path supplies cleanup authority without blocking writes."""
import copy
import json
from types import SimpleNamespace

import pytest

from src import server
from src.shader_workspace import aliases
from src.shader_workspace import cbuffer_alias_publication as publication
import test_material_naming_mcp as naming_tests


ORIGINAL = "a" * 64
ACTIVE = "b" * 64
HISTORICAL = "c" * 64
CAPTURE = "d" * 64
SESSION = "e" * 32


def field(name="roughness"):
    return {"bind": 4, "space": 0, "name": name, "type": "float",
            "components": [{"offset": 0}], "status": "inferred", "provenance": "shader",
            "evidence": "source/shader.glsl:21 controls the surface reflection width"}


def scope(case, raw):
    return {"shaderRawSha256": raw, "stage": "fragment", "bind": 4, "space": 0,
            "layoutSha256": aliases.layout_sha256(case.block["block"], case.block["members"])}


def saved_scopes(case):
    path = aliases.layout_alias_path(scope(case, ORIGINAL)["layoutSha256"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    return {view["shaderScope"]["shaderRawSha256"] for view in saved.get("semanticViews", [])}


@pytest.fixture
def case(monkeypatch):
    value = naming_tests.CBufferAliasPublicationMCPTests()
    value.setUp()
    value.identity.update(captureId=CAPTURE, captureSessionId=SESSION,
                          activeDisplayShaderRawSha256=ACTIVE,
                          activeReplacementShaderRawSha256=ACTIVE,
                          activeDisplayBlocks=[copy.deepcopy(value.block)])
    (value.workspace / "manifest.json").write_text(json.dumps({
        "schema": "renderdoc-ultra-shader-workspace/v1", "stage": "fragment",
        "shader": {"rawSha256": ORIGINAL}, "latestCompiled": {"rawSha256": ACTIVE},
    }), encoding="utf-8")
    monkeypatch.setattr(server, "_active_project_service",
                        lambda: SimpleNamespace(replay_service=lambda: None))
    aliases.publish_aliases(value.block["block"], value.block["members"], [],
        shader_scope=scope(value, HISTORICAL), semantic_fields=[field("oldRoughness")],
        semantic_source_sha256="f" * 64, semantic_captured_shader_sha256=ORIGINAL)
    value.registry_calls = []

    def registry(method, parameters, **kwargs):
        value.registry_calls.append((method, parameters))
        assert method == "get_shared_shader_replacements"
        return {"schema": "renderdoc-ultra-shared-shader-replacement-list/v1", "schemaVersion": 1,
                "captureId": CAPTURE, "captureSessionId": SESSION, "count": 0,
                "replacements": [], "originalShadersPreserved": True, "replayMutation": False}

    monkeypatch.setattr(server.ipc, "call", registry)
    try:
        yield value
    finally:
        value.tearDown()
        value.doCleanups()


def capture_transaction(monkeypatch):
    requests = []
    actual = publication.publish_transaction

    def transaction(**kwargs):
        requests.append(copy.deepcopy(kwargs["publications"]))
        return actual(**kwargs)

    monkeypatch.setattr(publication, "publish_transaction", transaction)
    return requests


def test_semantic_publication_passes_retention_to_shared_transaction_and_prunes_history(case, monkeypatch):
    requests = capture_transaction(monkeypatch)
    result = case.call_publish([], semantic_fields=[field()])
    assert "error" not in result, result
    assert result["applied"] and result["semanticHistoryRetention"]["complete"]
    assert result["semanticHistoryRetention"]["requested"]
    assert len(requests) == 1 and len(requests[0]) == 2
    for batch in requests[0]:
        assert batch["semanticCapturedShaderSha256"] == ORIGINAL
        assert batch["semanticRetention"] == {
            "protectedShaderRawSha256": [ORIGINAL, ACTIVE], "keepPrevious": 0}
    assert saved_scopes(case) == {ORIGINAL, ACTIVE}
    assert case.registry_calls == [("get_shared_shader_replacements", {
        "expected_capture_id": CAPTURE, "expected_capture_session_id": SESSION,
        "include_all_capture_catalogs": True})]


def test_registry_failure_keeps_history_but_still_publishes_current_names(case, monkeypatch):
    requests = capture_transaction(monkeypatch)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Replay owner registry temporarily unavailable")

    monkeypatch.setattr(server.ipc, "call", unavailable)
    result = case.call_publish([], semantic_fields=[field()])
    assert "error" not in result and result["applied"], result
    assert result["receiptPersistence"]["persisted"]
    assert result["counts"]["writtenSemanticFields"] == 1
    assert result["counts"]["writtenDisplaySemanticFields"] == 1
    assert result["semanticHistoryRetention"]["complete"] is False
    assert "temporarily unavailable" in result["semanticHistoryRetention"]["diagnostic"]
    assert all("semanticRetention" not in batch for batch in requests[0])
    assert saved_scopes(case) == {ORIGINAL, ACTIVE, HISTORICAL}


def test_project_lookup_failure_is_cleanup_only_and_does_not_block_naming(case, monkeypatch):
    def unavailable():
        raise OSError("Project registry locked")
    monkeypatch.setattr(server, "_active_project_service", unavailable)
    result = case.call_publish([], semantic_fields=[field()])
    assert "error" not in result and result["applied"], result
    assert result["semanticHistoryRetention"]["complete"] is False
    assert "Project registry locked" in result["semanticHistoryRetention"]["diagnostic"]
    assert HISTORICAL in saved_scopes(case)


def test_row_only_publication_does_not_query_owners_or_prune_semantic_views(case, monkeypatch):
    requests = capture_transaction(monkeypatch)
    result = case.call_publish([{"bind": 4, "space": 0, "byteOffset": 0, "alias": "surfaceValues"}])
    assert "error" not in result and result["applied"], result
    assert result["semanticHistoryRetention"] == {"complete": False, "requested": False}
    assert case.registry_calls == []
    assert all("semanticRetention" not in batch for batch in requests[0])
    assert saved_scopes(case) == {HISTORICAL}


def test_idempotent_retry_does_not_reobserve_owners_or_repeat_cleanup(case, monkeypatch):
    operation_id = "retention-shared-path-retry"
    first = case.call_publish([], semantic_fields=[field()], operation_id=operation_id)
    assert "error" not in first and first["applied"], first

    def must_not_read(*args, **kwargs):
        raise AssertionError("Committed naming retry must not reobserve replay state")

    monkeypatch.setattr(server.ipc, "call", must_not_read)
    monkeypatch.setattr(publication, "publish_transaction", must_not_read)
    repeated = case.call_publish([], semantic_fields=[field()], operation_id=operation_id)
    assert "error" not in repeated and repeated["idempotentReplay"], repeated
    assert repeated["semanticHistoryRetention"] == first["semanticHistoryRetention"]
    assert saved_scopes(case) == {ORIGINAL, ACTIVE}
