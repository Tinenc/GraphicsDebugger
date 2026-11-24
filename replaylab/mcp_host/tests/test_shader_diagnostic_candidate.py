"""Numeric-localization trials cannot publish unfinished source or revisions."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.shader_workspace import workspace as workspace_module
from src.shader_workspace.errors import ManifestValidationError
from tests import test_shader_abi as abi_fixture
from tests import test_shader_workspace_routes as route_fixture


def test_diagnostic_tool_requires_workspace_and_replay_effects():
    from replaylab_agent.policy import CORE_DIRECT_TOOLS, tool_call_effects, tool_admissible_at_effect_ceiling
    from src.host_runtime import LONG_TOOLS, REPLAY_TOUCH_TOOLS
    name = "diagnose_shader_organization_candidate"
    assert name in CORE_DIRECT_TOOLS and name in LONG_TOOLS and name in REPLAY_TOUCH_TOOLS
    assert tool_call_effects(name, {}) == frozenset({"workspace", "replay"})
    assert not tool_admissible_at_effect_ceiling(name, {"workspace"})
    assert tool_admissible_at_effect_ceiling(name, {"workspace", "replay"})


def test_recovery_contract_does_not_blame_candidate_for_unstable_baseline():
    from src.shader_workspace.diagnostic_candidate import numeric_recovery_contract
    result = numeric_recovery_contract("D:/workspace", "a" * 64,
                                      {"baselineStability": {"withinTolerance": False}})
    assert result["nextAction"] == "stabilize-replay-and-localize-difference"
    assert result["fixedTolerances"] is True
    assert result["retryUnchangedSource"] is False


@pytest.fixture
def trial_workspace():
    fixture = abi_fixture.ShaderWorkspaceAbiGateTests()
    fixture.setUp()
    try:
        fixture._add_complete_catalog_interface()
        service, root, unused = fixture.fixture._create("numeric-trial")
        source = Path(service.decompile(root)["sourcePath"])
        yield service, root, source
    finally:
        fixture.tearDown()


def test_trial_compiles_unsaved_text_without_candidate_or_source_publication(trial_workspace):
    service, root, source = trial_workspace
    before = {p: p.read_bytes() for p in (source, root / "manifest.json", root / "history.jsonl") if p.is_file()}
    trial = source.read_text() + "\n// Diagnostic half, deliberately not reviewed.\n"
    result = service.compile_diagnostic_candidate(root, trial, hashlib.sha256(source.read_bytes()).hexdigest())
    assert result["ok"] is True, result
    assert result["diagnosticOnly"] is True and result["sourcePreserved"] is True
    assert result["applied"] is False and result["published"] is False
    assert Path(result["candidateBlobPath"]).is_file()
    assert Path(result["sourcePath"]).read_text() == trial
    assert result["sourceSha256"] == hashlib.sha256(trial.encode()).hexdigest()
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert service.status(root)["latestCompiled"] is None


def test_trial_rejects_stale_canonical_hash_before_running_compiler(trial_workspace, monkeypatch):
    service, root, source = trial_workspace
    monkeypatch.setattr(workspace_module, "run_operation", lambda *a, **k: pytest.fail("compiled stale trial"))
    with pytest.raises(ManifestValidationError, match="canonical source changed"):
        service.compile_diagnostic_candidate(root, source.read_text(), "0" * 64)


def test_trial_compiler_failure_keeps_diagnostics_without_changing_source(trial_workspace, monkeypatch):
    service, root, source = trial_workspace
    before = (root / "manifest.json").read_bytes()
    run = workspace_module.run_operation

    def failed(operation, values, job, **kwargs):
        result = run(operation, values, job, **kwargs)
        result.stderr_path.write_text("trial.hlsl:2:3: error: undeclared identifier 'broken'\n")
        return replace(result, return_code=1)

    monkeypatch.setattr(workspace_module, "run_operation", failed)
    result = service.compile_diagnostic_candidate(root, "broken", hashlib.sha256(source.read_bytes()).hexdigest())
    assert result["ok"] is False
    assert "undeclared identifier" in result["diagnosticText"]
    assert result["diagnostics"][0]["line"] == 2
    assert result["sourcePreserved"] is True
    assert (root / "manifest.json").read_bytes() == before


def test_trial_compilation_uses_route_chain_and_frozen_include_closure():
    fixture = route_fixture.ShaderWorkspaceRouteChainTests()
    fixture.setUp()
    try:
        fixture._install_interface(fixture.catalog_path,fixture._complete_dxil_interface())
        service = fixture.service()
        root = fixture.root / "trial-route"
        service.create(fixture.catalog_path, fixture.key.catalog_key, root, editable_format="hlsl")
        source = Path(service.decompile(root)["sourcePath"])
        (source.parent / "lighting.hlsli").write_text("float4 Lighting() { return 0.5; }\n")
        trial = '#include "lighting.hlsli"\nfloat4 main() : SV_Target { return Lighting(); }\n'
        before = (root / "manifest.json").read_bytes()
        result = service.compile_diagnostic_candidate(root, trial, hashlib.sha256(source.read_bytes()).hexdigest())
        assert result["ok"] is True, result
        assert result["compileHops"]
        assert len(result["sourceDependencies"]["documents"]) == 2
        assert (root / "manifest.json").read_bytes() == before
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("unsupported", [False, True])
def test_host_trial_compares_original_without_publication_gate(trial_workspace, monkeypatch, unsupported):
    from src import server
    service, root, source = trial_workspace
    original_manifest = (root / "manifest.json").read_bytes()
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_require_organization_source_review",
                        lambda *a: pytest.fail("diagnostic trial required final quality review"))
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity",
                        lambda *a, **k: {"captureSessionId": "a" * 32})
    monkeypatch.setattr(server, "_shader_organization_anchor_event", lambda status: 61408)
    calls = []

    def call(method, params, **kwargs):
        calls.append((method, params))
        assert params["baseline"]["rawSha256"] == service.status(root)["baseline"]["rawSha256"]
        assert Path(params["candidate"]["path"]).is_file()
        return {"captureId": params["expected_capture_id"], "captureSessionId": "a" * 32,
                "eventId": 61408, "accepted": False, "replayRestored": True,
                "sampleCount": 0 if unsupported else 100,
                "metrics": {"coverage": "unsupported" if unsupported else "all-stored-output-data"}}

    monkeypatch.setattr(server.ipc, "call", call)
    result = json.loads(server.diagnose_shader_organization_candidate(
        str(root), source.read_text() + "\n// changed diagnostic\n",
        hashlib.sha256(source.read_bytes()).hexdigest()))
    assert result["ok"] is True, result
    assert result["accepted"] is False and result["applied"] is False
    assert result["replayAbiChecked"] is (not unsupported)
    assert calls[0][0] == "compare_shader_organization_candidates"
    assert (root / "manifest.json").read_bytes() == original_manifest


def test_comparison_exception_updates_retained_compile_receipt_to_failure(trial_workspace, monkeypatch):
    from src import server
    service, root, source = trial_workspace
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity",
                        lambda *a, **k: {"captureSessionId": "a" * 32})
    monkeypatch.setattr(server, "_shader_organization_anchor_event", lambda status: 61408)
    monkeypatch.setattr(server.ipc, "call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture session changed")))
    result = json.loads(server.diagnose_shader_organization_candidate(
        str(root), source.read_text(), hashlib.sha256(source.read_bytes()).hexdigest()))
    assert result["ok"] is False and result["compiled"] is True
    assert result["comparisonCompleted"] is False
    assert result["sourcePreserved"] is True
    assert result["phase"] == "comparison"
    assert result["replayAbiChecked"] is False
    retained = json.loads(Path(result["receiptPath"]).read_text())
    assert retained["ok"] is False and retained["compiled"] is True
    assert "capture session changed" in retained["error"]
