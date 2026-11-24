"""Atomic helper/call drafts compose with the ledger and compile at verification."""
import hashlib
import json
import os
import asyncio
from pathlib import Path

import pytest

from src.shader_workspace.restructure import RestructureError, prepare_rewrite, apply_operations
from src.shader_workspace.organization import ShaderOrganizationError
from src.shader_workspace import organization as organization_module
import test_shader_organization as fixtures

SOURCE = """cbuffer Constants : register(b0)
{
    float Scale : packoffset(c0.x);
};
float Evaluate(float x)
{
    return x * Scale;
}
float4 MainPS(float4 input : TEXCOORD0) : SV_Target
{
    float result = Evaluate(input.x);
    float tail = result + input.y;
    return tail.xxxx;
}
"""
HELPER = "float Evaluate(float x, float scale)\n{\n    return x * scale;\n}\n"
EDITS = [{"startLine": 5, "endLine": 8, "replacement": HELPER},
         {"startLine": 11, "endLine": 11, "replacement": "    float result = Evaluate(input.x, Scale);\n"}]


@pytest.fixture
def owned():
    fixture = fixtures.ShaderOrganizationTests()
    fixture.setUp()
    fixture.root = fixture.root / "atomic"
    fixture.service = fixtures._FakeWorkspaceService(fixture.root, SOURCE.encode())
    fixture.original = SOURCE.encode()
    fixture.opened = fixture._begin()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def rewrite(owned, edits=EDITS, **kwargs):
    proposal = Path(owned.opened["proposalSourcePath"])
    return owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "rewrite",
        name=kwargs.pop("name", "Consolidate"), edits=edits,
        expected_proposal_sha256=kwargs.pop("sha", hashlib.sha256(proposal.read_bytes()).hexdigest()), **kwargs)


def snapshot(owned):
    return {str(p.relative_to(owned.root)): p.read_bytes() for p in owned.root.rglob("*") if p.is_file()}


def test_atomic_signature_and_call_changes_compose_with_renames_extract_and_undo(owned):
    result = rewrite(owned)
    assert result["accepted"], result
    assert result["operations"][0]["editCount"] == 2
    proposal = Path(result["proposalSourcePath"])
    owned.manager.rename(str(owned.root), owned.opened["organizationId"], {"result": "intensity"}, {"Evaluate": "Scale an input value."})
    text = proposal.read_text(encoding="utf-8")
    assert "float intensity = Evaluate(input.x, Scale);" in text
    line = next(i for i, v in enumerate(text.splitlines(), 1) if "float tail =" in v)
    extracted = owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "extract", start_line=line, end_line=line, name="ComputeTail")
    assert extracted["accepted"], extracted
    assert "Evaluate(input.x, Scale)" in proposal.read_text()
    undone = owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "undo")
    assert undone["accepted"] and "ComputeTail" not in proposal.read_text()
    assert "Evaluate(float x, float scale)" in proposal.read_text()
    removed = owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "remove", name="Consolidate")
    assert removed["accepted"], removed
    assert "float intensity = Evaluate(input.x);" in proposal.read_text()
    assert "// [ai] Scale an input value." in proposal.read_text()


@pytest.mark.parametrize("requested_name", ["", "可读计算接口", "Consolidate"])
def test_rewrite_ledger_labels_are_generated_without_administrative_retries(owned, requested_name):
    first = rewrite(owned, name="Consolidate")
    assert first["accepted"], first
    proposal = Path(first["proposalSourcePath"])
    current = proposal.read_text(encoding="utf-8")
    line = next(i for i, text in enumerate(current.splitlines(), 1) if "float tail =" in text)
    second = owned.manager.restructure(
        str(owned.root), owned.opened["organizationId"], "rewrite", name=requested_name,
        edits=[{"startLine": line, "endLine": line,
                "replacement": "    float tail = input.y + result;\n"}],
        expected_proposal_sha256=hashlib.sha256(proposal.read_bytes()).hexdigest(),
        operation_id="automatic-ledger-label", request_sha256="a" * 64,
    )
    assert second["accepted"], second
    returned_name = second["lastReceipt"]["name"]
    assert returned_name.startswith("Rewrite_") and returned_name != "Consolidate"
    saved = snapshot(owned)
    replay = owned.manager.restructure(
        str(owned.root), owned.opened["organizationId"], "rewrite", name=requested_name,
        operation_id="automatic-ledger-label", request_sha256="a" * 64,
    )
    assert replay["lastReceipt"]["name"] == returned_name
    assert snapshot(owned) == saved
    removed = owned.manager.restructure(
        str(owned.root), owned.opened["organizationId"], "remove", name=returned_name,
    )
    assert removed["accepted"], removed
    assert "float tail = result + input.y;" in proposal.read_text(encoding="utf-8")


def test_stale_hash_is_atomic_and_compile_failure_is_deferred_to_finish(owned, monkeypatch):
    before = snapshot(owned)
    with pytest.raises(ShaderOrganizationError, match="stale"):
        rewrite(owned, sha="0" * 64)
    assert snapshot(owned) == before
    compile_source = owned.service.compile
    calls = []

    def compile_only_baseline(workspace):
        source = owned.service.source_path.read_bytes()
        calls.append(source)
        if source != SOURCE.encode():
            raise RuntimeError("signature/call mismatch")
        return compile_source(workspace)

    monkeypatch.setattr(owned.service, "compile", compile_only_baseline)
    result = rewrite(owned)
    assert result["accepted"] and result["validationTier"] == "proposal-draft"
    assert not calls
    finished = owned.manager.finish_draft(str(owned.root), owned.opened["organizationId"])
    assert not finished["ok"] and not finished["promoted"]
    assert "signature/call mismatch" in finished["reason"]
    assert len(calls) == 2
    assert owned.service.source_path.read_bytes() == SOURCE.encode()


def test_long_named_complete_stage_between_24_and_64_kib_remains_atomic(owned, monkeypatch):
    # Real computational statements with readable identifiers, rather than a
    # padded comment, reproduce the size of the production lighting stages.
    statements = []
    previous = "x"
    for index in range(190):
        current = "materialDirectionalLightingAndOcclusionWeightedResponseContribution%03d" % index
        statements.append("    float %s = %s + (scale * 0.0001f);" % (current, previous))
        previous = current
    helper = "float Evaluate(float x, float scale)\n{\n" + "\n".join(statements) + "\n    return %s;\n}\n" % previous
    edits = [{"startLine": 5, "endLine": 8, "replacement": helper}, EDITS[1]]
    transaction_bytes = sum(len("".join(SOURCE.splitlines(keepends=True)[item["startLine"] - 1:item["endLine"]]).encode())
                            + len(item["replacement"].encode()) for item in edits)
    assert 24 * 1024 < transaction_bytes < 64 * 1024
    dll = Path(r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll")
    if os.name == "nt" and dll.is_file():
        from src.shader_workspace.adapters.d3dcompiler_adapter import _compile_bytes
        def check(workspace, text):
            code, unused, diagnostics = _compile_bytes(dll, text.encode(), "long-stage.hlsl", "MainPS", "ps_5_0")
            return {"ok": code >= 0, "diagnostics": diagnostics.decode(errors="replace")}
        monkeypatch.setattr(owned.service, "compile_check", check)
    result = rewrite(owned, edits)
    assert result["accepted"], result
    assert result["operations"][0]["editCount"] == 2
    text = Path(result["proposalSourcePath"]).read_text()
    assert "Evaluate(input.x, Scale)" in text and statements[-1] in text
    if os.name == "nt" and dll.is_file():
        assert owned.service.compile_check(str(owned.root), text)["ok"]


def test_local_rewrite_skips_whole_source_structure_report(owned, monkeypatch):
    def unexpected_report(*_args, **_kwargs):
        raise AssertionError("a local edit should not build a whole-source report")

    monkeypatch.setattr(owned.manager, "_cached_structure_report", unexpected_report)
    result = rewrite(owned)
    assert result["accepted"], result
    assert result["ledger"] is None
    assert result["structure"] == {
        "available": False, "sourceSha256": result["proposalSourceSha256"]
    }
    assert "Evaluate(input.x, Scale)" in Path(result["proposalSourcePath"]).read_text()


def test_local_extract_skips_report_but_list_still_builds_current_summary(owned, monkeypatch):
    line = next(i for i, value in enumerate(SOURCE.splitlines(), 1)
                if "float tail =" in value)
    original_report = owned.manager._cached_structure_report
    calls = []

    def counted_report(*args, **kwargs):
        calls.append(args)
        return original_report(*args, **kwargs)

    monkeypatch.setattr(owned.manager, "_cached_structure_report", counted_report)
    result = owned.manager.restructure(
        str(owned.root), owned.opened["organizationId"], "extract",
        start_line=line, end_line=line, name="ComputeTail",
    )
    assert result["accepted"], result
    assert calls == []
    listed = owned.manager.restructure(
        str(owned.root), owned.opened["organizationId"], "list",
    )
    assert listed["accepted"]
    assert any(row["name"] == "ComputeTail" for row in listed["ledger"])
    assert len(calls) == 1


@pytest.mark.parametrize("cancel", [True, False])
def test_cancellation_or_commit_error_rolls_back_proposal_ledger_and_views(owned, monkeypatch, cancel):
    before = snapshot(owned)
    if cancel:
        from src.shader_workspace import restructure as restructure_module
        def check(*unused, **unused_kwargs):
            raise KeyboardInterrupt("cancelled")
        monkeypatch.setattr(restructure_module, "apply_operations", check)
    else:
        from src.shader_workspace import workspace_mutation_transaction

        original = workspace_mutation_transaction.atomic_write_bytes
        workspace_root = owned.root.resolve()
        live_writes = 0
        injected = False

        def fail(path, content):
            nonlocal live_writes, injected
            candidate = Path(path).resolve()
            if (
                (candidate == workspace_root or workspace_root in candidate.parents)
                and candidate.name in {
                    "organized-proposal.hlsl", "session.json",
                    "aliases.json", "organized.hlsl",
                }
            ):
                live_writes += 1
                if live_writes == 2 and not injected:
                    injected = True
                    raise OSError("simulated artifact commit failure")
            return original(path, content)

        monkeypatch.setattr(
            workspace_mutation_transaction, "atomic_write_bytes", fail
        )
    with pytest.raises((KeyboardInterrupt, OSError)):
        rewrite(owned)
    assert snapshot(owned) == before


def test_concurrent_external_proposal_edit_is_not_overwritten_after_draft_preparation(owned, monkeypatch):
    proposal = Path(owned.opened["proposalSourcePath"])
    changed = SOURCE + "// concurrent writer\n"
    from src.shader_workspace import restructure as restructure_module
    apply_operations = restructure_module.apply_operations
    def check(*args, **kwargs):
        result = apply_operations(*args, **kwargs)
        proposal.write_text(changed, encoding="utf-8")
        return result
    monkeypatch.setattr(restructure_module, "apply_operations", check)
    with pytest.raises(ShaderOrganizationError, match="changed during draft preparation"):
        rewrite(owned)
    assert proposal.read_text() == changed
    session = json.loads((proposal.parent / "session.json").read_text())
    assert session["structureOps"] == []


@pytest.mark.parametrize("edits", [
    [{"startLine": 0, "endLine": 1, "replacement": "x;"}],
    [EDITS[0], EDITS[0]],
    [{"startLine": 3, "endLine": 3, "replacement": "float Other;"}],
    [{"startLine": 9, "endLine": 14, "replacement": "float4 MainPS() : SV_Target { return 1; }"}],
    [{"startLine": 11, "endLine": 11, "replacement": "#define HACK 1\nfloat result = 0;"}],
    [{"startLine": 11, "endLine": 11, "replacement": "}\nfloat Hack;\nvoid Other() {"}],
    [{"startLine": 11, "endLine": 11, "replacement": "//" + "x" * 65536}],
    [{"startLine": 11, "endLine": 11, "replacement": "float result=0;\n" + "\n" * 401}],
    EDITS * 9,
])
def test_bounds_overlap_and_protected_interface_are_rejected_without_writes(owned, edits):
    before = snapshot(owned)
    result = rewrite(owned, edits)
    assert not result["accepted"], result
    assert snapshot(owned) == before


def test_new_pure_internal_struct_is_allowed_but_abi_or_resource_fields_are_not():
    helper = "struct InternalInput { float value; float scale; };\nfloat Evaluate(float x)\n{\n    InternalInput data; data.value = x; data.scale = Scale;\n    return data.value * data.scale;\n}\n"
    op = prepare_rewrite(SOURCE, [{"startLine": 5, "endLine": 8, "replacement": helper}], renames={}, entry_point="MainPS", name="GroupInputs")
    rewritten, unused = apply_operations(SOURCE, [op])
    assert "struct InternalInput" in rewritten
    for field in ("Texture2D value;", "float value : TEXCOORD0;", "float value[2];", "float value : packoffset(c0);"):
        bad = helper.replace("float value;", field)
        with pytest.raises(RestructureError, match="protected"):
            prepare_rewrite(SOURCE, [{"startLine": 5, "endLine": 8, "replacement": bad}], renames={}, entry_point="MainPS", name="Bad")


@pytest.mark.parametrize("directive", [
    "#define Scale 2.0", "/* note */ #define Scale 2.0",
    "/* note */ #define register(x) register(b1)",
    "/* multi\nline */ #undef Scale", "/* note */ #if 1\n#endif",
])
def test_rewrite_detects_directives_after_comments(directive):
    replacement = directive + "\nfloat Evaluate(float x) { return x * Scale; }\n"
    with pytest.raises(RestructureError, match="preprocessor"):
        prepare_rewrite(SOURCE, [{"startLine": 5, "endLine": 8, "replacement": replacement}],
                        renames={}, entry_point="MainPS", name="ProtectedDirective")


def test_directive_detection_preserves_comment_and_string_text():
    from src.shader_workspace.restructure import _has_directive
    assert not _has_directive('// #define Scale 2.0\nfloat x;')
    assert not _has_directive('/*\n#define Scale 2.0\n*/ float x;')
    assert not _has_directive('"#define Scale 2.0"\nfloat x;')


def test_struct_input_output_can_evolve_atomically_with_calls_and_replay(owned):
    helper = ("struct EvaluationInput { float value; };\n"
              "struct EvaluationOutput { float scaled; };\n"
              "EvaluationOutput Evaluate(EvaluationInput input)\n{\n"
              "    EvaluationOutput result; result.scaled = input.value * Scale; return result;\n}\n")
    first = rewrite(owned, [
        {"startLine": 5, "endLine": 8, "replacement": helper},
        {"startLine": 11, "endLine": 11, "replacement":
         "    EvaluationInput data; data.value = input.x; EvaluationOutput evaluated = Evaluate(data); float result = evaluated.scaled;"},
    ])
    assert first["accepted"], first
    path = Path(first["proposalSourcePath"])
    before_evolution = path.read_text(encoding="utf-8")
    lines = before_evolution.splitlines()
    selected = {
        "output": next(n for n, line in enumerate(lines, 1) if line.startswith("struct EvaluationOutput")),
        "body": next(n for n, line in enumerate(lines, 1) if "result.scaled = input.value" in line),
        "caller": next(n for n, line in enumerate(lines, 1) if "EvaluationInput data;" in line),
    }
    second = rewrite(owned, [
        {"startLine": selected["output"], "endLine": selected["output"],
         "replacement": "struct EvaluationOutput { float3 contribution; };"},
        {"startLine": selected["body"], "endLine": selected["body"],
         "replacement": "    EvaluationOutput result; result.contribution = float3(input.value * Scale, 0.0, 0.0); return result;"},
        {"startLine": selected["caller"], "endLine": selected["caller"],
         "replacement": "    EvaluationInput data; data.value = input.x; EvaluationOutput evaluated = Evaluate(data); float result = evaluated.contribution.x;"},
    ], name="EvolveOutput")
    assert second["accepted"], second
    evolved = path.read_text(encoding="utf-8")
    # Exercise the actual compiler and its executable fingerprint, not a mock
    # 'readability score' or only the presence of a struct in generated text.
    dll = Path(r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll")
    if os.name == "nt" and dll.is_file():
        from src.shader_workspace.adapters.d3dcompiler_adapter import _compile_bytes
        from src.shader_workspace.binary_formats import fingerprint_shader_executable
        fingerprints = []
        for text in (SOURCE, before_evolution, evolved):
            code, binary, diagnostic = _compile_bytes(dll, text.encode(), "structured.hlsl", "MainPS", "ps_5_0")
            assert code >= 0, diagnostic
            fingerprints.append(fingerprint_shader_executable(binary, "structured").code_sha256)
        assert len(set(fingerprints)) == 1
    undone = owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "undo")
    assert undone["accepted"] and path.read_text(encoding="utf-8") == before_evolution


@pytest.mark.parametrize("binding", ["cbuffer", "resource", "entry", "nested"])
def test_struct_layout_bound_to_captured_interface_remains_protected(binding):
    prefix = "struct CapturedData { float value; };\n"
    suffix = "float4 MainPS(float4 input : TEXCOORD0) : SV_Target { return input; }\n"
    if binding == "cbuffer":
        source = prefix + "cbuffer C : register(b0) { CapturedData data; };\n" + suffix
    elif binding == "resource":
        source = prefix + "StructuredBuffer<CapturedData> Data : register(t0);\n" + suffix
    elif binding == "entry":
        source = prefix + "float4 MainPS(CapturedData input : TEXCOORD0) : SV_Target { return input.value.xxxx; }\n"
    else:
        source = prefix + "struct CaptureInput { CapturedData data; float2 uv : TEXCOORD0; };\n" + suffix
    with pytest.raises(RestructureError, match="protected"):
        prepare_rewrite(source, [{"startLine": 1, "endLine": 1,
                                 "replacement": "struct CapturedData { float3 value; };"}],
                        renames={}, entry_point="MainPS", name="ChangeABI")


def test_renamed_entry_struct_is_protected_during_preparation():
    from src.shader_workspace.equivalence import apply_renames
    canonical = ("struct CapturedData { float value; };\n"
                 "float4 MainPS(CapturedData input : TEXCOORD0) : SV_Target { return input.value.xxxx; }\n")
    renames = {"MainPS": "ReadableMain"}
    source = apply_renames(canonical, renames)
    with pytest.raises(RestructureError, match="protected"):
        prepare_rewrite(source, [{"startLine": 1, "endLine": 1,
                        "replacement": "struct CapturedData { float3 value; };"}],
                        renames=renames, entry_point="MainPS", name="ChangeRenamedEntryABI",
                        canonical_source=canonical)


def test_rewrite_introduced_local_can_be_renamed_and_survive_restart(owned):
    changed = [{"startLine": 11, "endLine": 12,
                "replacement": "    float fresh = Evaluate(input.x) + input.y;\n    float tail = fresh;"}]
    assert rewrite(owned, changed)["accepted"]
    result = owned.manager.rename(str(owned.root), owned.opened["organizationId"],
                                  {"MainPS::fresh": "combined"})
    text = Path(result["proposalSourcePath"]).read_text()
    assert "float combined =" in text and "tail = combined;" in text
    restarted = organization_module.ShaderOrganizationManager(lambda: owned.service, owned.manager._validate_candidate)
    resumed = restarted.begin(str(owned.root))
    assert resumed["organizationId"] == owned.opened["organizationId"]
    undone = restarted.restructure(str(owned.root), resumed["organizationId"], "undo")
    assert undone["accepted"] and "combined" not in Path(result["proposalSourcePath"]).read_text()
    continued = restarted.rename(str(owned.root), resumed["organizationId"], {"tail": "finalValue"})
    assert "float finalValue" in Path(continued["proposalSourcePath"]).read_text()


def test_remove_prior_extraction_does_not_shift_later_rewrite_to_wrong_line(owned):
    organization = owned.opened["organizationId"]
    first = owned.manager.restructure(str(owned.root), organization, "extract", start_line=11, end_line=11, name="ReadInput")
    assert first["accepted"], first
    path = Path(first["proposalSourcePath"])
    lines = path.read_text().splitlines()
    line = next(i for i, v in enumerate(lines, 1) if "float tail =" in v)
    result = rewrite(owned, [{"startLine": line, "endLine": line, "replacement": "    float tail = result + (input.y);"}])
    assert result["accepted"], result
    removed = owned.manager.restructure(str(owned.root), organization, "remove", name="ReadInput")
    assert removed["accepted"], removed
    assert "ReadInput" not in path.read_text() and "result + (input.y)" in path.read_text()


def test_remove_parent_extraction_of_rewritten_helper_is_refused_without_loss(owned):
    organization = owned.opened["organizationId"]
    extracted = owned.manager.restructure(str(owned.root), organization, "extract", start_line=11, end_line=11, name="ReadInput")
    path = Path(extracted["proposalSourcePath"])
    text = path.read_text()
    line = next(i for i, v in enumerate(text.splitlines(), 1) if "float result = Evaluate" in v)
    assert rewrite(owned, [{"startLine": line, "endLine": line, "replacement": "    float result = (Evaluate(input.x));"}])["accepted"]
    before = snapshot(owned)
    refused = owned.manager.restructure(str(owned.root), organization, "remove", name="ReadInput")
    assert not refused["accepted"] and "scope" in refused["reason"]
    assert snapshot(owned) == before


def test_capability_and_existing_effect_scope_are_explicit():
    from src.shader_workspace.source_language import organization_capabilities
    from replaylab_agent.policy import tool_effects
    assert organization_capabilities("hlsl")["boundedRewrite"] is True
    assert organization_capabilities("glsl")["boundedRewrite"] is False
    assert organization_capabilities("hlsl", compile_eligible=False)["boundedRewrite"] is True
    assert tool_effects("restructure_shader_workspace_source") == frozenset({"workspace"})


def test_old_review_budget_file_does_not_block_rewrite_or_explicit_restart(owned):
    legacy_budget = owned.root / "ai" / "organization-rounds.json"
    legacy_budget.write_text("{broken legacy state", encoding="utf-8")
    assert rewrite(owned)["accepted"]
    owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "undo")
    restarted = owned.manager.begin(str(owned.root), restart=True)
    assert restarted["organizationId"] != owned.opened["organizationId"]
    assert legacy_budget.read_text(encoding="utf-8") == "{broken legacy state"


def test_public_existing_tool_forwards_atomic_arguments_without_new_effect_grant(monkeypatch):
    from types import SimpleNamespace
    from src import server
    captured = {}
    def restructure(*args, **kwargs):
        captured.update(kwargs)
        return {"accepted": True, "applied": False}
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: SimpleNamespace(restructure=restructure))
    result = json.loads(asyncio.run(server.restructure_shader_workspace_source(
        "workspace", "a" * 32, "rewrite", name="Group",
        expected_proposal_sha256="b" * 64, edits=EDITS,
        operation_id="atomic-rewrite-forwarding-test"))
    )
    assert result["accepted"] and not result["applied"]
    assert captured["edits"] == EDITS and captured["expected_proposal_sha256"] == "b" * 64


@pytest.mark.skipif(os.name != "nt", reason="native HLSL compile requires Windows")
def test_real_hlsl_compiler_accepts_only_atomic_signature_and_calls(owned, monkeypatch):
    from src.shader_workspace.adapters.d3dcompiler_adapter import _compile_bytes
    from src.shader_workspace.binary_formats import fingerprint_shader_executable
    dll = Path(r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll")
    if not dll.is_file():
        pytest.skip("Windows SDK compiler unavailable")
    proposal = Path(owned.opened["proposalSourcePath"])

    def native_compiles() -> bool:
        code, unused, unused_diagnostics = _compile_bytes(
            dll, proposal.read_bytes(), "atomic.hlsl", "MainPS", "ps_5_0"
        )
        return code >= 0

    assert rewrite(owned, EDITS[:1])["accepted"]
    assert not native_compiles()
    assert owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "undo")["accepted"]
    assert rewrite(owned, EDITS[1:])["accepted"]
    assert not native_compiles()
    assert owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "undo")["accepted"]
    accepted = rewrite(owned)
    assert accepted["accepted"], accepted
    assert native_compiles()
    fingerprints = []
    for text in (SOURCE, Path(accepted["proposalSourcePath"]).read_text()):
        code, binary, diagnostic = _compile_bytes(dll, text.encode(), "atomic.hlsl", "MainPS", "ps_5_0")
        assert code >= 0, diagnostic
        fingerprints.append(fingerprint_shader_executable(binary, "atomic").code_sha256)
    assert fingerprints[0] == fingerprints[1]
    grouped = "struct InternalInputs { float value; float scale; };\nfloat Evaluate(float x, float scale)\n{\n    InternalInputs data; data.value = x; data.scale = scale;\n    return data.value * data.scale;\n}\n"
    current = Path(accepted["proposalSourcePath"])
    struct_result = owned.manager.restructure(str(owned.root), owned.opened["organizationId"], "rewrite",
        name="GroupInternalInputs", expected_proposal_sha256=hashlib.sha256(current.read_bytes()).hexdigest(),
        edits=[{"startLine": 5, "endLine": 8, "replacement": grouped}])
    assert struct_result["accepted"], struct_result
    code, binary, diagnostic = _compile_bytes(dll, current.read_bytes(), "atomic.hlsl", "MainPS", "ps_5_0")
    assert code >= 0, diagnostic
    assert fingerprint_shader_executable(binary, "atomic").code_sha256 == fingerprints[0]
    before_ambiguous = snapshot(owned)
    with pytest.raises(ShaderOrganizationError, match="multiple declaration identities"):
        owned.manager.rename(str(owned.root), owned.opened["organizationId"], {"scale": "gain"})
    assert snapshot(owned) == before_ambiguous
    renamed = owned.manager.rename(
        str(owned.root), owned.opened["organizationId"],
        {"InternalInputs": "EvaluationInputs",
         "InternalInputs.scale": "inputScale",
         "Evaluate::scale": "gain"},
    )
    renamed_text = current.read_text()
    assert "struct EvaluationInputs" in renamed_text
    assert "float Evaluate(float x, float gain)" in renamed_text
    assert "data.inputScale = gain" in renamed_text
    code, binary, diagnostic = _compile_bytes(dll, current.read_bytes(), "atomic.hlsl", "MainPS", "ps_5_0")
    assert code >= 0, diagnostic
    assert fingerprint_shader_executable(binary, "atomic").code_sha256 == fingerprints[0]


@pytest.mark.skipif(os.name != "nt", reason="native HLSL compile requires Windows")
def test_struct_field_selector_renames_member_without_treating_it_as_swizzle(owned, monkeypatch):
    from src.shader_workspace.adapters.d3dcompiler_adapter import _compile_bytes
    dll = Path(r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll")
    if not dll.is_file():
        pytest.skip("Windows SDK compiler unavailable")
    def check(workspace, text):
        code, unused, diagnostics = _compile_bytes(dll, text.encode(), "fields.hlsl", "MainPS", "ps_5_0")
        return {"ok": code >= 0, "diagnostics": diagnostics.decode(errors="replace")}
    monkeypatch.setattr(owned.service, "compile_check", check)
    helper = "struct InternalInput { float r; };\nfloat Evaluate(float x)\n{\n    InternalInput data; data.r = x;\n    return data.r * Scale;\n}\n"
    assert rewrite(owned, [{"startLine": 5, "endLine": 8, "replacement": helper}])["accepted"]
    result = owned.manager.rename(
        str(owned.root), owned.opened["organizationId"],
        {"InternalInput.r": "redValue"},
    )
    text = Path(result["proposalSourcePath"]).read_text()
    assert "struct InternalInput { float redValue; };" in text
    assert "data.redValue = x" in text and "return data.redValue * Scale" in text


def test_followup_rename_does_not_overwrite_an_edit_before_transaction(owned, monkeypatch):
    assert rewrite(owned)["accepted"]
    proposal = Path(owned.opened["proposalSourcePath"])
    changed = proposal.read_text() + "// other writer\n"
    original_apply = organization_module.apply_workspace_mutation
    def race(**kwargs):
        proposal.write_text(changed, encoding="utf-8")
        return original_apply(**kwargs)
    monkeypatch.setattr(organization_module, "apply_workspace_mutation", race)
    with pytest.raises(RuntimeError, match="changed before its transaction"):
        owned.manager.rename(str(owned.root), owned.opened["organizationId"], {"result": "intensity"})
    assert proposal.read_text() == changed


@pytest.mark.parametrize("qualifier", ["", "const "])
@pytest.mark.parametrize("legacy", [False, True])
def test_renamed_multi_output_extraction_and_whole_helper_rewrite_keep_generated_names(owned, qualifier, legacy, monkeypatch):
    """An unrelated later rename must not re-spell generated out parameters.

    This is the real failure shape: extract under semantic aliases, then edit
    its entire definition, then regenerate the canonical namespace for a new
    rename. Const outputs also need stable caller-side temporary identifiers.
    """
    source = ("cbuffer Constants : register(b0) { float Scale : packoffset(c0.x); };\n"
              "float4 MainPS(float4 input : TEXCOORD0) : SV_Target\n{\n"
              f"    {qualifier}float _first = input.x * Scale;\n"
              f"    {qualifier}float _second = input.y + Scale;\n"
              "    float _tail = _first + _second;\n    return _tail.xxxx;\n}\n")
    owned.root = owned.root / "multiple-output"
    owned.service = fixtures._FakeWorkspaceService(owned.root, source.encode())
    opened = owned._begin()
    oid = opened["organizationId"]
    proposal, session_path = Path(opened["proposalSourcePath"]), Path(opened["sessionPath"])
    owned.manager.rename(str(owned.root), oid, {"_first": "coverage", "_second": "weightedNormal"})
    extraction = owned.manager.restructure(str(owned.root), oid, "extract", start_line=4, end_line=5, name="EvaluateCoverage")
    assert extraction["accepted"], extraction
    from src.shader_workspace.restructure import _function_spans
    text = proposal.read_text()
    helper = next(item for item in _function_spans(text) if item["name"] == "EvaluateCoverage")
    before = "\n".join(text.splitlines()[helper["startLine"] - 1:helper["endLine"]])
    changed = owned.manager.restructure(str(owned.root), oid, "rewrite", name="ClarifyProduct",
        expected_proposal_sha256=hashlib.sha256(proposal.read_bytes()).hexdigest(),
        edits=[{"startLine": helper["startLine"], "endLine": helper["endLine"],
                "replacement": before.replace("input.x * Scale", "input.x * (Scale)")}])
    assert changed["accepted"], changed
    if legacy:
        # A legitimately sealed older Host ledger has no generated-name metadata.
        session = json.loads(session_path.read_bytes())
        for op in session["structureOps"]:
            op.pop("generatedNames", None)
        session_path.write_text(json.dumps(owned.manager._seal_session(session)), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(owned.service, "compile_check", lambda *args: pytest.fail("rename compiled whole draft"))
        renamed = owned.manager.rename(str(owned.root), oid,
                                       {"_tail": "totalCoverage", "_first": "surfaceCoverage"})
    assert renamed["validationTier"] == "proposal-draft"
    assert "deferredFinalChecks" not in renamed
    text = proposal.read_text()
    assert "replaylab_out_coverage" in text
    assert "replaylab_out_surfaceCoverage" not in text
    assert "surfaceCoverage" in text and "totalCoverage" in text
    assert "input.x * (Scale)" in text
    if qualifier:
        assert "replaylab_value_coverage" in text
    session = json.loads(session_path.read_bytes())
    assert session["structureOps"][0]["generatedNames"]["_first"]["parameter"] == "replaylab_out_coverage"
    assert "local" in session["structureOps"][0]["generatedNames"]["_first"]
    canonical, _, _ = owned.manager._regenerate_proposal(source, {}, session["structureOps"], {})
    assert "replaylab_out_coverage" in canonical
    assert "_first" in canonical or "replaylab_local__first" in canonical
    assert owned.manager.restructure(str(owned.root), oid, "undo")["accepted"]
    assert "input.x * (Scale)" not in proposal.read_text()
    assert "surfaceCoverage" in proposal.read_text()
    owned.manager.rename(str(owned.root), oid, {"replaylab_out_coverage": "coverageResult"})
    assert "out float coverageResult" in proposal.read_text()
    dll = Path(r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll")
    if os.name == "nt" and dll.is_file():
        from src.shader_workspace.adapters.d3dcompiler_adapter import _compile_bytes
        from src.shader_workspace.binary_formats import fingerprint_shader_executable
        hashes = []
        for value in (source, text, proposal.read_text()):
            code, binary, diagnostics = _compile_bytes(dll, value.encode(), "outputs.hlsl", "MainPS", "ps_5_0")
            assert code >= 0, diagnostics.decode(errors="replace")
            hashes.append(fingerprint_shader_executable(binary, "outputs").code_sha256)
        assert len(set(hashes)) == 1
