"""Resource names cover the compiled unit and stale when header bytes change."""
import hashlib
import json
from unittest import mock

import pytest

from src.shader_workspace.compile_source_unit import capture_compilation_unit, write_snapshot
from src.shader_workspace.naming_delivery import source_context, semantic_delivery
from src import shader_resource_naming_review as naming
from test_cbuffer_naming_delivery import receipt


def compiled_unit(root, main=None):
    source = root / "source/shader.hlsl"
    source.parent.mkdir(exist_ok=True)
    header = source.parent / "params.hlsli"
    if not header.exists():
        header.write_bytes(b"cbuffer Material:register(b5){float4 tint;}; Texture2D BaseColor:register(t2);")
    raw = main or b'#include "params.hlsli"\nfloat4 main():SV_Target{return tint;}'
    source.write_bytes(raw)
    job = root / "jobs" / ("compile-" + hashlib.sha256(header.read_bytes() + raw).hexdigest()[:12])
    job.mkdir(parents=True)
    proof = write_snapshot(root, job / "input.hlsl", capture_compilation_unit(source, raw))
    (root / "manifest.json").write_text(json.dumps({"source": {"path": "source/shader.hlsl"},
        "latestCompiled": {"sourceSha256": hashlib.sha256(raw).hexdigest(), "rawSha256": "a" * 64,
                           "sourceDependencies": proof}}))
    return source, header, raw, proof


def review():
    return {"assets": {"status": "not-configured", "evidence": "No unpacked root configured",
                       "usage": "Use source roles without claiming asset matches"},
            "textures": [{"selector": "resource:BaseColor", "status": "retained",
                          "evidence": "The sampled color supplies surface tint"}],
            "reconciliation": "Reviewed header resources and the material tint binding"}


def test_headers_require_naming_and_dependency_bound_receipts(tmp_path, monkeypatch):
    from src import server
    source, header, raw, proof = compiled_unit(tmp_path)
    naming.prepare(tmp_path, {"assetRootConfigured": False})
    monkeypatch.setattr(server.ipc, "call", lambda *a, **kw: pytest.fail("Naming must not query replay"))
    context = source_context(tmp_path, raw)
    assert context["complete"] and context["documentCount"] == 2
    assert naming.inventory(raw, context)["textures"][0]["name"] == "BaseColor"
    assert semantic_delivery(raw, context=context)["required"]
    old = receipt(raw)
    assert semantic_delivery(raw, old, context=context)["reason"] == "publication-source-dependencies-stale"
    old["sourceDependenciesDigest"] = proof["digest"]
    assert semantic_delivery(raw, old, context=context)["status"] == "complete"
    assert naming.record(tmp_path, raw, review())["status"] == "complete"
    (tmp_path / "ai/cbuffer-name-publication.json").write_text(json.dumps(old))
    # Consistent include/naming evidence does not invent a live display target.
    assert server._shader_cbuffer_semantic_delivery(str(tmp_path))["status"] == "pending"
    from src.shader_semantic_delivery import delivery_state
    assert delivery_state(str(tmp_path), hashlib.sha256(raw).hexdigest(), {},
                          source_path="source/shader.hlsl")["status"] == "pending"

    header.write_bytes(header.read_bytes() + b"\n// Reinterpreted tint semantics")
    assert source.read_bytes() == raw
    assert not source_context(tmp_path, raw)["complete"]
    pending = server._shader_cbuffer_semantic_delivery(str(tmp_path))
    assert pending["status"] == "pending" and pending["recompileRequired"]
    assert pending["nextActions"][-1]["tool"] == "compile_shader_workspace"
    assert delivery_state(str(tmp_path), hashlib.sha256(raw).hexdigest(), {},
                          source_path="source/shader.hlsl")["status"] == "pending"
    with pytest.raises(ValueError, match="include snapshot"):
        naming.record(tmp_path, raw, review())

    compiled_unit(tmp_path)
    current = source_context(tmp_path, raw)
    assert current["complete"] and current["sourceDependenciesDigest"] != proof["digest"]
    renamed = semantic_delivery(raw, old, context=current)
    assert renamed["status"] == "pending" and not renamed["recompileRequired"]
    assert naming.delivery_state(tmp_path, raw)["reason"] == "resource-naming-source-dependencies-stale"


@pytest.mark.parametrize("failure", ["uncompiled", "missing-header", "corrupt-frozen", "malformed-proof"])
def test_incomplete_closure_cannot_be_not_applicable_or_complete(tmp_path, failure):
    source, header, raw, proof = compiled_unit(tmp_path)
    if failure == "missing-header":
        header.unlink()
    elif failure == "corrupt-frozen":
        (tmp_path / proof["documents"][1]["originalPath"]).write_bytes(b"// invalid")
    else:
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        manifest["latestCompiled"]["sourceDependencies"] = None if failure == "uncompiled" else {"documents": [None]}
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    context = source_context(tmp_path, raw)
    assert not context["complete"]
    assert semantic_delivery(raw, context=context)["status"] == "pending"
    assert naming.delivery_state(tmp_path, raw)["status"] == "pending"


def test_proven_inactive_include_does_not_create_an_obligation(tmp_path):
    _, _, raw, _ = compiled_unit(tmp_path, b'#if 0\n#include "absent.hlsli"\n#endif\nfloat4 main():SV_Target{return 1;}')
    context = source_context(tmp_path, raw)
    assert context["complete"]
    assert semantic_delivery(raw, context=context)["status"] == "not-applicable"


def test_closure_only_reads_already_recorded_paths(tmp_path, monkeypatch):
    from src.shader_workspace import source_documents
    _, header, raw, _ = compiled_unit(tmp_path)
    header.write_bytes(b'#include "unrecorded.hlsli"\n')
    original = source_documents._read
    seen = []
    def read(path):
        seen.append(path.name)
        return original(path)
    monkeypatch.setattr(source_documents, "_read", read)
    assert not source_context(tmp_path, raw)["complete"]
    assert seen == ["params.hlsli"]


def test_publication_persists_closure_digest_and_keeps_historical_replay_honest():
    from src import server
    from test_material_naming_mcp import CBufferAliasPublicationMCPTests
    case = CBufferAliasPublicationMCPTests()
    case.setUp()
    try:
        source, header, raw, proof = compiled_unit(case.workspace)
        status = {"workspacePath": str(case.workspace), "workspaceId": "workspace-a",
                  "source": {"path": str(source)}}
        arguments = {"semantic_source_sha256": hashlib.sha256(raw).hexdigest(),
                     "operation_id": "include-naming-publication", "binding_reviews": [{"bind": 4,
                         "unresolvedOffsets": list(range(0, 32, 4)),
                         "evidence": "The declared physical lanes have no confirmed source roles"}]}
        with mock.patch.object(server, "_alias_publication_workspace_status", return_value=status):
            result = case.call_publish([], **arguments)
            assert "error" not in result, result
            assert result["sourceDependenciesDigest"] == proof["digest"]
            receipt_path = case.workspace / "ai/cbuffer-name-publication.json"
            saved = json.loads(receipt_path.read_bytes())
            assert saved["sourceDependenciesDigest"] == proof["digest"]
            header.write_bytes(header.read_bytes() + b"\n// changed")
            historical = case.call_publish([], **arguments)
            assert historical["idempotentReplay"]
            assert historical["sourceDependenciesDigest"] == proof["digest"]
            assert server._shader_cbuffer_semantic_delivery(str(case.workspace))["status"] == "pending"
            fresh = case.call_publish([], **{**arguments, "operation_id": "include-naming-new"})
            assert "include snapshot" in fresh.get("error", ""), fresh
            assert json.loads(receipt_path.read_bytes()) == saved
    finally:
        case.tearDown()
        case.doCleanups()
