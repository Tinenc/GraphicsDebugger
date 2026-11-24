"""Whole-source scope evidence is structural; the Agent still judges quality."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.shader_organization_review import (
    CHECKS, make_review, publication_review, review_state, source_scope_catalog, source_snapshot,
    track_result,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "shader"
    folder = root / "ai/organization-sessions" / ("a" * 32)
    folder.mkdir(parents=True)
    baseline = b"float Shade(float x) { if(x > 0) return x; return 0; }\nfloat MainPS(float x) { return Shade(x); }\n"
    (root / "source.hlsl").write_bytes(baseline)
    (folder / "baseline.hlsl").write_bytes(baseline)
    (folder / "proposal.hlsl").write_bytes(baseline + b"// named paths reviewed\n")
    (folder / "session.json").write_text(json.dumps({
        "organizationId": "a" * 32, "state": "drafting", "sourcePath": "source.hlsl",
        "baselineSourcePath": str((folder / "baseline.hlsl").relative_to(root)),
        "proposalSourcePath": str((folder / "proposal.hlsl").relative_to(root)),
        "baselineSourceSha256": hashlib.sha256(baseline).hexdigest(),
    }))
    return source_snapshot(str(root), "a" * 32)


def assessment(source):
    scopes = {"baseline_source_sha256": source["baselineSourceSha256"], "plan": [
        {"id": "lighting", "requirement": "Organize positive and fallback light paths", "functions": ["Shade"]},
        {"id": "entry", "requirement": "Keep the captured entry coherent", "functions": ["MainPS"]},
    ], "results": [
        {"id": "lighting", "status": "satisfied", "source_functions": ["Shade"],
         "evidence": "The light factor has one named input and output",
         "branches": [{"path": "positive", "status": "satisfied", "evidence": "Returns the input light factor"},
                      {"path": "fallback", "status": "satisfied", "evidence": "Clamps the negative input to zero"}],
         "retained_intermediates": "No aliases or scalar copies remain", "remaining_work": []},
        {"id": "entry", "status": "satisfied", "source_functions": ["MainPS"],
         "evidence": "The entry forwards its input to the same lighting calculation",
         "branches": [{"path": "entry", "status": "satisfied", "evidence": "The single return calls Shade"}],
         "retained_intermediates": "Entry arguments preserve the captured contract", "remaining_work": []},
    ]}
    scopes["observation_dispositions"] = retained_dispositions(source, scopes["results"])
    return scopes


def retained_dispositions(source, results):
    from src.source_review_observations import review_candidates, source_review_catalog
    from src.shader_workspace.flow_graph import _function_records, _tokens
    text = Path(source["sourcePath"]).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    records = _function_records(text)
    tokens = _tokens(text)
    def anchor(row):
        locations = row["locations"]
        for record in records:
            if record["name"] not in row["functions"]:
                continue
            body = tokens[record["bodyStart"] + 1:record["bodyEnd"]]
            matching = [token for token in body if any(
                location.get("function") == record["name"]
                and location.get("startLine", token.line) <= token.line <= location.get("endLine", token.line)
                for location in locations)]
            token = next((token for token in matching if token.text not in {"{", "}", ";"}), body[0])
            return {"start_line": token.line, "end_line": token.line, "text": lines[token.line - 1]}
        raise AssertionError("Fixture candidate has no source function")
    return [{"candidate_id": row["id"],
             "scope_ids": [scope["id"] for scope in results
                           if set(scope["source_functions"]) & set(row["functions"])],
             "status": "retained",
             "evidence": "The small fixture's " + row["category"] + " was inspected in " + ", ".join(row["functions"]),
             "alternative": "Replace this fixture calculation with a named helper; its existing one-expression body already states the purpose",
             "source_anchor": anchor(row)}
            for row in review_candidates(source_review_catalog(source))]


def review(source, scopes=None, verdict="complete", **kwargs):
    from test_shader_semantic_overview_review import prepared_fixture
    # Validate scope shape first so these tests still isolate source-review errors.
    if scopes and verdict == "complete":
        make_review(source, source["sourceSha256"], verdict, {key: "scope evidence" for key in CHECKS},
                    [], [], "", scope_review=scopes, require_scope=True)
    overview = prepared_fixture(source, scopes) if scopes and verdict == "complete" else None
    return make_review(source, source["sourceSha256"], verdict,
                       {key: "The source-specific assessment is in the matching scope evidence" for key in CHECKS},
                       kwargs.get("findings", []), kwargs.get("actions", []), kwargs.get("blocker", ""),
                       scope_review=scopes, require_scope=True, prepared_overview=overview, require_overview=True)


def test_four_generic_texts_alone_no_longer_admit_a_new_complete_review(source):
    with pytest.raises(ValueError, match="scope_review is required"):
        review(source)


def test_initial_function_or_plan_result_cannot_be_omitted(source):
    scopes = assessment(source)
    scopes["plan"].pop(); scopes["results"].pop()
    with pytest.raises(ValueError, match="omits initial.*MainPS"):
        review(source, scopes)
    scopes = assessment(source)
    scopes["results"].pop()
    with pytest.raises(ValueError, match="exactly one result"):
        review(source, scopes)


def test_complete_cannot_hide_unmet_fallback_inside_a_satisfied_scope(source):
    scopes = assessment(source)
    scopes["results"][0]["branches"][1]["status"] = "unmet"
    with pytest.raises(ValueError, match="satisfied scope cannot retain unmet"):
        review(source, scopes)
    scopes["results"][0].update(status="unmet", remaining_work=["Organize the fallback light calculation"])
    with pytest.raises(ValueError, match="complete review cannot retain unmet"):
        review(source, scopes)
    receipt = review(source, scopes, "needs_work", findings=["Fallback path still opaque"], actions=["Group the fallback calculation"])
    assert receipt["scopeReview"]["results"][0]["remaining_work"]


def test_new_helper_bodies_need_coverage_and_real_current_function_identity(source):
    path = Path(source["sourcePath"])
    path.write_bytes(path.read_bytes() + b"float ExtraHelper(float x) { return x*x; }\n")
    source = source_snapshot(source["workspacePath"], source["organizationId"])
    scopes = assessment(source)
    with pytest.raises(ValueError, match="omits current.*ExtraHelper"):
        review(source, scopes)
    scopes["results"][0]["source_functions"].append("ExtraHelper")
    scopes["observation_dispositions"] = retained_dispositions(source, scopes["results"])
    assert review(source, scopes)["verdict"] == "complete"
    scopes["results"][0]["source_functions"].append("InventedFunction")
    with pytest.raises(ValueError, match="absent from the current source"):
        review(source, scopes)


def test_complete_is_source_quality_before_compile_and_later_edits_invalidate_it(source):
    receipt = review(source, assessment(source))
    Path(source["reviewPath"]).write_text(json.dumps(receipt))
    # Neither compilation nor apply is present or required for this admission.
    assert not source["saved"] and publication_review(source["workspacePath"], source["organizationId"])["ready"]
    Path(source["sourcePath"]).write_bytes(Path(source["sourcePath"]).read_bytes() + b"// changed\n")
    assert not publication_review(source["workspacePath"], source["organizationId"])["ready"]


def test_legacy_review_remains_readable_but_cannot_admit_final_effects(source):
    legacy = make_review(source, source["sourceSha256"], "complete", {key: "Legacy evidence" for key in CHECKS}, [], [], "")
    legacy.pop("scopeReview")
    Path(source["reviewPath"]).write_text(json.dumps(legacy))
    state = review_state({"legacy": {**source, "initialSourceSha256": source["baselineSourceSha256"], "review": legacy}})["targets"][0]
    assert state["fresh"] and not state["scopeCoverageComplete"]
    assert state["review"]["checks"] == legacy["checks"]
    assert not publication_review(source["workspacePath"], source["organizationId"])["ready"]


def test_complete_source_scopes_without_independent_overview_remain_pending(source):
    receipt = make_review(source, source["sourceSha256"], "complete",
                          {key: "Current source was fully inspected" for key in CHECKS}, [], [], "",
                          scope_review=assessment(source), require_scope=True)
    Path(source["reviewPath"]).write_text(json.dumps(receipt))
    row = review_state({"current": {**source, "initialSourceSha256": source["baselineSourceSha256"],
                                    "review": receipt}})["targets"][0]
    assert row["fresh"] and row["scopeCoverageComplete"] and not row["semanticOverviewComplete"]
    assert "overview" in row["diagnostic"]
    assert not publication_review(source["workspacePath"], source["organizationId"])["ready"]


def test_scope_binding_cannot_be_transferred_to_another_initial_source(source):
    scopes = assessment(source)
    scopes["baseline_source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="initial whole source"):
        review(source, scopes)


def test_related_unchanged_functions_can_share_a_scope_without_cosmetic_edits(source):
    scopes = assessment(source)
    scopes["plan"] = [{"id": "all", "requirement": "Trace the existing complete two-function program", "functions": ["Shade", "MainPS"]}]
    result = copy.deepcopy(scopes["results"][0])
    result.update(id="all", source_functions=["Shade", "MainPS"], retained_intermediates="The scalar argument is meaningful and remains unchanged")
    scopes["results"] = [result]
    scopes["observation_dispositions"] = retained_dispositions(source, scopes["results"])
    assert review(source, scopes)["hostSemanticApproval"] is False
    assert source_scope_catalog(source)["initialFunctions"] == ["MainPS", "Shade"]


def test_generic_scope_claims_cannot_skip_current_counterexamples_before_publication(source):
    path = Path(source["sourcePath"])
    path.write_text("""float Shade(float x) {
    float2 adjusted = float2(x, x);
    float detailWeightX = clamp(abs(adjusted.x) * 3.33333325f, 0.0f, 1.0f);
    float detailWeightY = clamp(abs(adjusted.y) * 3.33333325f, 0.0f, 1.0f);
    float3 halfSum = float3(x, x, x);
    float halfInverseLength = rsqrt(dot(halfSum, halfSum));
    float3 halfVector = float3(halfInverseLength * halfSum.x,
        halfInverseLength * halfSum.y, halfInverseLength * halfSum.z);
    return dot(halfVector, halfVector) + detailWeightX + detailWeightY;
}
float MainPS(float x) { return Shade(x); }
""", encoding="utf-8")
    source = source_snapshot(source["workspacePath"], source["organizationId"])
    scopes = assessment(source)
    dispositions = scopes.pop("observation_dispositions")
    with pytest.raises(ValueError, match="observation_dispositions is required"):
        review(source, scopes)
    # Old evidence remains inspectable, but cannot admit compilation/apply.
    legacy = make_review(source, source["sourceSha256"], "complete",
                         {key: "All four decisions are claimed in prose" for key in CHECKS},
                         [], [], "", scope_review=scopes)
    Path(source["reviewPath"]).write_text(json.dumps(legacy))
    row = review_state({"legacy": {**source, "initialSourceSha256": source["baselineSourceSha256"],
                                   "review": legacy}})["targets"][0]
    assert row["fresh"] and not row["scopeCoverageComplete"]
    assert not publication_review(source["workspacePath"], source["organizationId"])["ready"]
    scopes["observation_dispositions"] = dispositions[:-1]
    with pytest.raises(ValueError, match="omits current catalog candidates"):
        review(source, scopes)
    from src.source_review_observations import review_candidates, source_review_catalog
    categories = {row["category"] for row in review_candidates(source_review_catalog(source))}
    assert {"channelExpressions", "expandedVectorProducts"} <= categories


def test_retained_candidates_need_their_actual_scope_and_cannot_claim_removed(source):
    scopes = assessment(source)
    assert scopes["observation_dispositions"]
    candidate = scopes["observation_dispositions"][0]
    candidate["scope_ids"] = ["entry" if candidate["scope_ids"] == ["lighting"] else "lighting"]
    with pytest.raises(ValueError, match="do not cover"):
        review(source, scopes)
    scopes = assessment(source)
    scopes["observation_dispositions"][0]["status"] = "resolved"
    with pytest.raises(ValueError, match="still present"):
        review(source, scopes)
    scopes = assessment(source)
    scopes["observation_dispositions"][0]["evidence"] = " "
    with pytest.raises(ValueError, match="observation-specific evidence"):
        review(source, scopes)


def test_actionable_current_candidate_keeps_scope_unmet_until_repaired(source):
    scopes = assessment(source)
    candidate = scopes["observation_dispositions"][0]
    candidate["status"] = "needs_work"
    with pytest.raises(ValueError, match="source observation needing work"):
        review(source, scopes)
    with pytest.raises(ValueError, match="unmet or blocked scope"):
        review(source, scopes, "needs_work", findings=["Function purpose needs explanation"], actions=["Add the missing purpose summary"])
    scope = next(row for row in scopes["results"] if row["id"] == candidate["scope_ids"][0])
    scope.update(status="unmet", remaining_work=["Add the missing purpose summary"])
    receipt = review(source, scopes, "needs_work", findings=["Function purpose needs explanation"], actions=["Add the missing purpose summary"])
    assert receipt["scopeReview"]["observation_dispositions"][0]["status"] == "needs_work"


def test_candidate_ids_cannot_be_reused_for_different_current_bytes(source):
    scopes = assessment(source)
    Path(source["sourcePath"]).write_bytes(Path(source["sourcePath"]).read_bytes() + b"// later edit\n")
    current = source_snapshot(source["workspacePath"], source["organizationId"])
    with pytest.raises(ValueError, match="unique current surfaced"):
        review(current, scopes)


def test_full_catalog_coverage_cannot_be_replaced_with_the_inline_preview(source):
    lines = ["float Shade(float x) {"]
    for index in range(20):
        lines.extend([f"    float weight{index}X = x * 2.0f;", f"    float weight{index}Y = x * 2.0f;"])
    lines.extend(["    return weight0X + weight0Y;", "}", "float MainPS(float x) { return Shade(x); }"])
    Path(source["sourcePath"]).write_text("\n".join(lines), encoding="utf-8")
    current = source_snapshot(source["workspacePath"], source["organizationId"])
    scopes = assessment(current)
    from src.source_review_observations import review_candidates, source_review_observations
    preview_ids = {row["id"] for row in review_candidates(source_review_observations(current))}
    complete = scopes["observation_dispositions"]
    assert len(complete) > len(preview_ids)
    scopes["observation_dispositions"] = [row for row in complete if row["candidate_id"] in preview_ids]
    with pytest.raises(ValueError, match="omits current catalog candidates"):
        review(current, scopes)
    scopes["observation_dispositions"] = complete
    assert review(current, scopes)["verdict"] == "complete"


@pytest.fixture
def local_candidate(source):
    text = """float Shade(float x)
{
    // This note is not executable evidence.
    float weightX = x * 2.0f;
    float weightY = x * 2.0f;
    float unrelated = x + 1.0f;
    return weightX + weightY + unrelated;
}
float MainPS(float x) { return Shade(x); }
"""
    Path(source["sourcePath"]).write_text(text, encoding="utf-8")
    current = source_snapshot(source["workspacePath"], source["organizationId"])
    scopes = assessment(current)
    from src.source_review_observations import review_candidates, source_review_catalog
    catalog = review_candidates(source_review_catalog(current))
    key = next(row["id"] for row in catalog if row["category"] == "channelExpressions")
    item = next(row for row in scopes["observation_dispositions"] if row["candidate_id"] == key)
    return current, scopes, item


@pytest.mark.parametrize("missing", ["alternative", "source_anchor"])
def test_generic_retention_without_rejected_alternative_and_local_source_is_insufficient(local_candidate, missing):
    current, scopes, item = local_candidate
    item["evidence"] = "Retain everything for precision."
    item.pop(missing)
    with pytest.raises(ValueError, match="alternative|source_anchor"):
        review(current, scopes)


@pytest.mark.parametrize("line,text", [
    (1, "float Shade(float x)"),
    (3, "// This note is not executable evidence."),
    (4, "weightX"),
    (4, "float weightX = inventedValue * 2.0f;"),
    (6, "float unrelated = x + 1.0f;"),
    (9, "float MainPS(float x) { return Shade(x); }"),
])
def test_retention_anchor_cannot_cite_only_a_name_comment_signature_or_unrelated_expression(local_candidate, line, text):
    current, scopes, item = local_candidate
    item["source_anchor"] = {"start_line": line, "end_line": line, "text": text}
    with pytest.raises(ValueError, match="source_anchor"):
        review(current, scopes)


def test_downstream_use_alone_cannot_replace_the_candidate_expression_anchor(local_candidate):
    current, scopes, item = local_candidate
    item["source_anchor"] = {"start_line": 7, "end_line": 7,
                             "text": "return weightX + weightY + unrelated;"}
    item["alternative"] = "Combine weightX and weightY into float2 weight before this return expression."
    item["evidence"] = "These named channels are retained here for the fixture's distinct caller convention."
    with pytest.raises(ValueError, match="source_anchor"):
        review(current, scopes)
    item["source_anchor"] = {"start_line": 4, "end_line": 4, "text": "float weightX = x * 2.0f;"}
    receipt = review(current, scopes)
    assert receipt["hostSemanticApproval"] is False
    assert receipt["scopeReview"]["observation_dispositions"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_retention_anchor_binds_exact_constructor_not_shared_input_or_same_line_neighbor(source, newline):
    lines = ["float Shade(float x)", "{", "    float scale = x;", "    float3 v = float3(x, x, x);",
             "    float unrelated = scale + 100.0f;",
             "    float3 first = float3(scale * v.x, scale * v.y, scale * v.z); float3 second = float3(scale * v.x, scale * v.y);",
             "    return first.x + second.x + unrelated;", "}", "float MainPS(float x) { return Shade(x); }"]
    # Use distinct constructors on one line; the fixture is lexical source, so
    # return types need not be compiled to test source citation identity.
    lines[5] = lines[5].replace("float3 second = float3", "float2 second = float2")
    Path(source["sourcePath"]).write_bytes((newline.join(lines) + newline).encode("utf-8"))
    current = source_snapshot(source["workspacePath"], source["organizationId"])
    scopes = assessment(current)
    from src.source_review_observations import review_candidates, source_review_catalog
    candidates = [row for row in review_candidates(source_review_catalog(current))
                  if row["category"] == "expandedVectorProducts"]
    assert len(candidates) == 2
    first = min(candidates, key=lambda row: row["locations"][0]["startOffset"])
    item = next(row for row in scopes["observation_dispositions"] if row["candidate_id"] == first["id"])
    for line, text in [(5, "float unrelated = scale + 100.0f;"),
                       (6, "float2(scale * v.x, scale * v.y)")]:
        item["source_anchor"] = {"start_line": line, "end_line": line, "text": text}
        with pytest.raises(ValueError, match="source_anchor"):
            review(current, scopes)
    item["source_anchor"] = {"start_line": 6, "end_line": 6,
                             "text": "float3(scale * v.x, scale * v.y, scale * v.z)"}
    assert review(current, scopes)["verdict"] == "complete"


def test_zero_candidates_are_explicit_and_do_not_require_cosmetic_edits(source):
    path = Path(source["sourcePath"])
    path.write_text("// Keep the nonnegative light contribution.\nfloat Shade(float x) { return max(x, 0); }\n"
                    "// Evaluate the output light.\nfloat MainPS(float x) { return Shade(x); }\n", encoding="utf-8")
    current = source_snapshot(source["workspacePath"], source["organizationId"])
    scopes = assessment(current)
    assert scopes["observation_dispositions"] == []
    assert review(current, scopes)["verdict"] == "complete"


def test_begin_only_provenance_remains_read_only_and_does_not_hide_actual_source_edits(source):
    targets = {}
    payload = {"workspacePath": source["workspacePath"], "organizationId": source["organizationId"],
               "proposalSourceSha256": source["sourceSha256"]}
    track_result(targets, "begin_shader_workspace_organization", payload, {}, False)
    row = review_state(targets)["targets"][0]
    assert row["sourceWorkObserved"] is False and not row["changed"]
    Path(source["sourcePath"]).write_bytes(Path(source["sourcePath"]).read_bytes() + b"// external edit\n")
    changed = review_state(targets)["targets"][0]
    assert changed["sourceWorkObserved"] is False and changed["changed"]


@pytest.mark.parametrize("tool,arguments", [
    ("write_shader_workspace_organization_file", {"kind": "proposal"}),
    ("restructure_shader_workspace_source", {"operation": "extract"}),
    ("finish_shader_workspace_organization", {}),
    ("record_shader_organization_review", {}),
])
def test_source_work_provenance_is_sticky_after_success_and_not_created_by_failures(source, tool, arguments):
    targets = {}
    payload = {"workspacePath": source["workspacePath"], "organizationId": source["organizationId"],
               "proposalSourceSha256": source["sourceSha256"]}
    track_result(targets, "begin_shader_workspace_organization", payload, {}, False)
    track_result(targets, tool, payload, arguments, True)
    assert next(iter(targets.values()))["sourceWorkObserved"] is False
    track_result(targets, tool, payload, arguments, False)
    track_result(targets, "begin_shader_workspace_organization", payload, {}, False)
    assert next(iter(targets.values()))["sourceWorkObserved"] is True
    # Restarting/adopting a different draft cannot erase source work already
    # undertaken for this workspace in the same task ledger.
    track_result(targets, "begin_shader_workspace_organization",
                 {**payload, "organizationId": "b" * 32}, {}, False)
    assert next(iter(targets.values()))["sourceWorkObserved"] is True


def test_old_provenance_stays_unknown_and_metadata_writes_are_not_source_work(source):
    payload = {"workspacePath": source["workspacePath"], "organizationId": source["organizationId"],
               "proposalSourceSha256": source["sourceSha256"]}
    key = str(Path(source["workspacePath"]).resolve()).replace("\\", "/").casefold()
    targets = {key: {"workspacePath": source["workspacePath"], "organizationId": source["organizationId"]}}
    track_result(targets, "begin_shader_workspace_organization", payload, {}, False)
    assert "sourceWorkObserved" not in targets[key]
    track_result(targets, "begin_shader_workspace_organization",
                 {**payload, "organizationId": "b" * 32}, {}, False)
    assert "sourceWorkObserved" not in targets[key]
    targets = {}
    track_result(targets, "begin_shader_workspace_organization", payload, {}, False)
    track_result(targets, "write_shader_workspace_organization_file", payload, {"kind": "analysis"}, False)
    track_result(targets, "restructure_shader_workspace_source", payload, {"operation": "list"}, False)
    assert next(iter(targets.values()))["sourceWorkObserved"] is False
