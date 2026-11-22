import hashlib
import json

import pytest

from src.shader_workspace.naming_delivery import semantic_delivery


SOURCE = b"cbuffer Material : register(b5) { float4 tint; };"


def receipt(source=SOURCE):
    return {"schema": "replaylab-cbuffer-name-publication-receipt/v1",
            "semanticSourceSha256": hashlib.sha256(source).hexdigest(),
            "shaderRawSha256": "d" * 64,
            "activeDisplayShaderRawSha256": "a" * 64, "applied": True, "partial": False,
            "counts": {"requestedSemanticFields": 1, "requestedDisplaySemanticFields": 1,
                       "writtenDisplaySemanticFields": 1, "keptUserDisplaySemanticFields": 0},
            "bindingCoverage": {"schema": "bread-cbuffer-binding-coverage/v1", "complete": True,
                       "bindings": [{"bind": 5, "space": 0, "status": "complete", "missingOffsets": []}]},
        "acceptance": {"requestedSemanticFieldsAccountedFor": True,
                           "currentDisplaySemanticFieldsAccountedFor": True}}


def test_new_source_does_not_reuse_old_publication_or_require_compile():
    result = semantic_delivery(SOURCE + b"\n// new organized source", receipt(), "b" * 64)
    assert result["required"] and result["status"] == "pending"
    assert result["reason"] == "publication-source-stale"
    assert result["recompileRequired"] is False


def test_current_source_and_candidate_receipt_satisfies_delivery():
    assert semantic_delivery(SOURCE, receipt(), "a" * 64)["status"] == "complete"
    assert semantic_delivery(SOURCE, receipt(), "b" * 64)["status"] == "complete"


def test_current_replacement_can_keep_original_ui_layout_but_needs_verified_owner():
    saved = receipt()
    saved.update(activeReplacementShaderRawSha256="b" * 64,
                 activeDisplayShaderRawSha256=saved["shaderRawSha256"])
    assert semantic_delivery(SOURCE, saved, "b" * 64)["status"] == "complete"
    assert semantic_delivery(SOURCE, saved, "c" * 64)["status"] == "complete"
    saved["activeReplacementShaderRawSha256"] = ""
    assert semantic_delivery(SOURCE, saved, "b" * 64)["status"] == "complete"


@pytest.mark.parametrize("mutation", [
    {"semanticSourceSha256": ""}, {"partial": True}, {"applied": False},
    {"counts": {"requestedSemanticFields": 0}}, {"acceptance": {}},
])
def test_legacy_empty_or_partial_publication_does_not_complete(mutation):
    saved = receipt()
    saved.update(mutation)
    assert semantic_delivery(SOURCE, saved)["status"] == "pending"


@pytest.mark.parametrize("source", [SOURCE, b"ConstantBuffer<Material> data;",
                                  b"layout(std140) uniform Material { vec4 tint; } data;"])
def test_cbuffer_work_has_default_semantic_delivery(source):
    assert semantic_delivery(source)["required"]


def test_non_cbuffer_shader_does_not_get_a_fake_obligation():
    assert semantic_delivery(b"// cbuffer NotReal { float x; };\nfloat4 main() { return 1; }")["status"] == "not-applicable"


def test_unbound_source_inspection_never_contacts_live_app(monkeypatch):
    from src import server
    def forbidden(**kwargs):
        raise AssertionError("An unbound source cannot request live capture identity")
    monkeypatch.setattr(server, "_active_live_context", forbidden)
    state = server._shader_cbuffer_display_observation({"source": {"path": "shader.hlsl"}})
    assert state["status"] == "refresh-required"
    assert state["refreshTool"] == "publish_cbuffer_aliases"


def test_final_finish_refreshes_actual_display_identity_after_save(monkeypatch):
    from src import server
    from test_shader_organization import ShaderOrganizationTests
    work = ShaderOrganizationTests(methodName="runTest")
    work.setUp()
    calls = []
    try:
        opened = work._begin()
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: work.manager)
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: work.service)
        def observe(status):
            calls.append(status["workspacePath"])
            return {"status": "refresh-required", "refreshTool": "publish_cbuffer_aliases"}
        monkeypatch.setattr(server, "_shader_cbuffer_display_observation", observe)
        response = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"], validate_for_apply=False))
        assert response["ok"] is True
        assert calls == [str(work.root)]
    finally:
        work.tearDown()
        work.doCleanups()


@pytest.mark.parametrize("operation", ["status", "compile"])
def test_workspace_response_exposes_pending_panel_update_without_replay(tmp_path, monkeypatch, operation):
    from src import server

    source = tmp_path / "source/shader.hlsl"
    source.parent.mkdir()
    source.write_bytes(SOURCE)
    saved = tmp_path / "ai/cbuffer-name-publication.json"
    saved.parent.mkdir()
    saved.write_text(json.dumps(receipt()))
    observation = {"status": "current", "originalRawSha256": "d" * 64,
        "activeDisplayShaderRawSha256": "a" * 64, "activeReplacementShaderRawSha256": "a" * 64,
        "captureSessionId": "e" * 32, "replayRead": False, "replayMutation": False}
    monkeypatch.setattr(server, "_active_live_context", lambda **kw: {
        "capture": {"captureId": "f" * 64, "captureSessionId": "e" * 32}})
    calls = []
    def identity_only(method, params, **kwargs):
        assert method == "get_shader_cbuffer_display_identity"
        calls.append(method)
        return dict(observation)
    monkeypatch.setattr(server.ipc, "call", identity_only)
    status = {"workspacePath": str(tmp_path), "source": {"path": str(source)},
              "capture": {"captureId": "f" * 64}, "shader": {"resourceId": 17},
              "baseline": {"rawSha256": "d" * 64},
              "latestCompiled": {"rawSha256": "b" * 64}}
    result = json.loads(server._shader_workspace_response(operation, status))["result"]
    assert result["cbufferSemanticDelivery"]["status"] == "pending"
    assert result["cbufferSemanticDelivery"]["candidateRawSha256"] == "b" * 64
    assert result["cbufferSemanticDelivery"]["recompileRequired"] is False
    saved.write_text(json.dumps({**receipt(), "activeDisplayShaderRawSha256": "b" * 64}))
    observation.update(activeDisplayShaderRawSha256="b" * 64, activeReplacementShaderRawSha256="b" * 64)
    result = json.loads(server._shader_workspace_response(operation, status))["result"]
    assert result["cbufferSemanticDelivery"]["status"] == "pending"
    assert result["cbufferSemanticDelivery"]["resourceNamingDelivery"]["reason"] == "resource-naming-review-missing"
    from src.shader_resource_naming_review import prepare, record
    prepare(tmp_path, {"assetRootConfigured": False})
    record(tmp_path, SOURCE, {"assets": {"status": "not-configured", "evidence": "The project has no unpacked directory", "usage": "The source supplies the parameter roles"},
                             "textures": [], "reconciliation": "Reviewed the material tint and its four semantic color lanes"})
    result = json.loads(server._shader_workspace_response(operation, status))["result"]
    assert result["cbufferSemanticDelivery"]["status"] == "complete"
    assert len(calls) == 3
