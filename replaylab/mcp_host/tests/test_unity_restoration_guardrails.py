"""Independent restoration regression checks through files and the durable ledger."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src import unity_restoration as restoration
from src.task_report import TaskReportStore
from src.task_completion_review import activity_identity
from src.unity_shader_sources import bind_sources_to_bundle, collect_organized_sources, verify_source_files
from test_unity_restoration import prepared  # noqa: F401 - shared pytest fixture
from test_unity_restoration import png, review, write


CRITERIA = [{"requirement": "Restore the selected draw in Unity", "status": "satisfied",
             "evidence": "Current imported files, camera rendering and source review were checked"}]


def ledger(tmp_path, name="restoration-ledger"):
    result = TaskReportStore(name, tmp_path / "events")
    result.begin_task_review("a" * 64)
    return result


@pytest.mark.parametrize("tool", ["check_unity_restoration", "render_unity_restoration", "review_unity_restoration"])
def test_failed_observation_preserves_prior_proof_as_advisory_evidence(prepared, tmp_path, tool):
    store, task, observation, *_ = prepared
    store.compare(task, observation)
    accepted = review(store, task)
    assert accepted["complete"]
    facts = ledger(tmp_path)
    facts.record_result("review_unity_restoration", json.dumps(accepted), arguments={"restoration_id": task})
    facts.record_task_review("complete", CRITERIA, [], [])
    facts.record_result(tool, json.dumps({"error": "Editor disconnected before the new observation"}), arguments={"restoration_id": task})
    after = restoration.RestorationStore(store.root).status(task)
    assert after["complete"]
    assert after["comparison"] == accepted["comparison"]
    assert after["revision"] == accepted["revision"]
    reopened = TaskReportStore(facts.task_id, facts.event_root)
    assert reopened.task_review_state()["unityRestorations"][0]["diagnosticsOnly"]
    assert reopened.record_task_review("complete", CRITERIA, [], [])["verdict"] == "complete"


@pytest.mark.parametrize("tool", ["bread_connection_identity", "bread_inspect_rendering", "bread_inspect_capture", "read_console"])
def test_readonly_editor_observations_preserve_the_validated_render(prepared, tmp_path, tool):
    store, task, observation, _, _, project = prepared
    store.compare(task, observation)
    before = review(store, task)
    facts = ledger(tmp_path)
    facts.record_result("review_unity_restoration", before, arguments={"restoration_id": task})
    facts.record_result("call_unity_project_tool", {"success": True}, arguments={"project_path": str(project), "tool_name": tool})
    after = restoration.RestorationStore(store.root).status(task)
    assert after["complete"] is True
    assert after["revision"] == before["revision"]
    assert after["comparison"] == before["comparison"]


def test_identical_pixels_at_new_paths_do_not_count_as_repair_progress(prepared, tmp_path):
    store, task, observation, *_ = prepared
    first = store.compare(task, observation)
    facts = ledger(tmp_path)
    args = {"restoration_id": task}
    facts.record_result("render_unity_restoration", first, arguments=args)
    before = facts.task_review_state()
    observation["image"] = png(tmp_path / "new-uuid-but-identical.png")
    second = store.compare(task, observation)
    assert second["iterations"][-1]["progress"] == "unchanged"
    assert activity_identity("render_unity_restoration", first, args) == activity_identity("render_unity_restoration", second, args)
    facts.record_result("render_unity_restoration", second, arguments=args)
    after = facts.task_review_state()
    assert after["workFingerprint"] == before["workFingerprint"]
    assert after["workRevision"] > before["workRevision"]


def test_revision_and_reworded_diagnostics_or_review_are_not_new_repair_evidence():
    from src.unity_restoration_requirements import stable_activity
    before = {"restorationId": "a" * 32, "revision": 1,
        "gaps": [{"code": "unity-configuration-gap", "detail": {"code": "missing-history", "diagnostic": "first wording"}}],
        "portingReview": {"revision": 1, "stateFingerprint": "same", "assessments": {"core_algorithm": "reviewed"},
            "decisions": [{"module": "history", "disposition": "approximation", "evidence": "first wording"}]}}
    after = copy.deepcopy(before)
    after["revision"] += 1
    after["gaps"][0]["detail"]["diagnostic"] = "same issue described again"
    after["portingReview"]["revision"] += 1
    after["portingReview"]["assessments"]["core_algorithm"] = "reworded review"
    after["portingReview"]["decisions"][0]["evidence"] = "reworded evidence without implementation change"
    assert stable_activity(before) == stable_activity(after)
    after["inspection"] = {"stateFingerprint": "new-actual-observation"}
    assert stable_activity(before) != stable_activity(after)


def test_old_import_diagnostic_survives_hydration_without_blocking_agent_assessment(tmp_path):
    facts = ledger(tmp_path)
    facts.record_result("import_resource_export_to_unity", json.dumps({
        "status": "completed", "operationId": "b" * 32, "projectPath": str(tmp_path / "Unity"),
        "functionalRestorationRequired": True}), arguments={"operation_id": "b" * 32})
    hydrated = TaskReportStore(facts.task_id, facts.event_root)
    state = hydrated.task_review_state()
    assert state["unityRestorations"][0]["gapCodes"] == ["functional-import-needs-restoration-task"]
    assert state["unityRestorations"][0]["diagnosticsOnly"]
    assert hydrated.record_task_review("complete", CRITERIA, [], [])["verdict"] == "complete"


def test_asset_only_import_does_not_create_a_functional_restoration_obligation(tmp_path):
    facts = ledger(tmp_path)
    facts.record_result("import_resource_export_to_unity", {"status": "completed", "functionalRestorationRequired": False})
    assert not facts.task_review_state()["unityRestorations"]
    assert facts.record_task_review("complete", CRITERIA, [], [])["verdict"] == "complete"


@pytest.mark.parametrize("matching_operation", [True, False])
def test_only_matching_bound_import_removes_its_diagnostic_but_none_locks_the_workflow(prepared, tmp_path, matching_operation):
    store, task, observation, _, _, project = prepared
    bound = store.status(task)
    operation = bound["binding"]["operationId"] if matching_operation else "0" * 32
    facts = ledger(tmp_path)
    facts.record_result("import_resource_export_to_unity", {"status": "completed", "operationId": operation,
        "projectPath": str(project), "functionalRestorationRequired": True})
    facts.record_result("bind_unity_restoration", bound, arguments={"restoration_id": task, "operation_id": bound["binding"]["operationId"]})
    store.compare(task, observation)
    accepted = review(store, task)
    facts.record_result("review_unity_restoration", accepted, arguments={"restoration_id": task})
    hydrated = TaskReportStore(facts.task_id, facts.event_root)
    pending = [row for row in hydrated.task_review_state()["unityRestorations"]
               if "functional-import-needs-restoration-task" in row.get("gapCodes", [])]
    assert bool(pending) is not matching_operation
    assert hydrated.record_task_review("complete", CRITERIA, [], [])["verdict"] == "complete"


def test_a_new_execution_scope_does_not_inherit_prior_unity_import_obligations(tmp_path):
    old = ledger(tmp_path)
    old.record_result("import_resource_export_to_unity", {"status": "completed", "operationId": "b" * 32,
        "projectPath": str(tmp_path / "Unity"), "functionalRestorationRequired": True})
    newer = TaskReportStore(old.task_id, old.event_root)
    scope = newer.begin_task_review("c" * 64)
    assert scope["unityRestorations"] == []
    with pytest.raises(ValueError, match="different execution"):
        old.record_result("import_resource_export_to_unity", {"functionalRestorationRequired": True,
            "operationId": "d" * 32, "projectPath": str(tmp_path / "Unity")})
    current = TaskReportStore(old.task_id, old.event_root).task_review_state()
    assert current["scopeId"] == scope["scopeId"] and current["unityRestorations"] == []


def test_spirv_same_module_bytes_with_a_different_entry_point_is_not_the_same_program():
    source = {"stage": "fragment", "rawSha256": "a" * 64, "entryPoint": "MainB", "api": "Vulkan", "ready": True}
    bundle = {"api": "Vulkan", "shaders": [{"stage": "fragment", "rawSha256": "a" * 64,
        "entryPoint": "MainA", "status": "captured"}]}
    with pytest.raises(ValueError, match="does not match"):
        bind_sources_to_bundle([source], bundle)
    source["entryPoint"] = "MainA"
    assert bind_sources_to_bundle([source], bundle)[0]["entryPoint"] == "MainA"
    source["api"] = "OpenGL"
    with pytest.raises(ValueError, match="different capture graphics API"):
        bind_sources_to_bundle([source], bundle)


def test_unavailable_bound_stage_cannot_disappear_from_all_stage_coverage():
    source = {"stage": "fragment", "rawSha256": "a" * 64, "ready": True}
    bundle = {"shaders": [{"stage": "fragment", "rawSha256": "a" * 64, "status": "captured"},
                          {"stage": "vertex", "status": "unavailable"}]}
    with pytest.raises(ValueError, match="unavailable capture evidence"):
        bind_sources_to_bundle([source], bundle)


def test_compiler_snapshot_external_include_is_accepted_then_stales_on_change(tmp_path, monkeypatch):
    from src import shader_organization_review
    from src.shader_workspace.compile_source_unit import write_snapshot, validate_snapshot
    from src.shader_workspace.source_documents import capture_source_unit

    root = tmp_path / "workspace"
    source = root / "source.hlsl"
    external = tmp_path / "shared/lighting.hlsli"
    write(external, "float Lighting() { return 1; }\n")
    source_sha = write(source, '#include "../shared/lighting.hlsli"\nfloat4 Main() : SV_Target { return Lighting(); }\n')
    unit = capture_source_unit(source)
    input_path = root / "jobs/compile/input.hlsl"
    input_path.parent.mkdir(parents=True)
    proof = write_snapshot(root, input_path, unit)
    validate_snapshot(root, source, source.read_bytes(), proof)
    candidate = {"sourceSha256": source_sha, "rawSha256": "b" * 64, "sourceDependencies": proof}
    write(root / "manifest.json", {"source": {"path": "source.hlsl"}, "latestCompiled": candidate})
    write(root / "ai/organize-code-gate.json", {"finalSourceSha256": source_sha, "organizationId": "c" * 32,
        "wholeSourceValidated": True, "organizedCompiledAndAbiValidated": True,
        "finalCandidateRawSha256": "b" * 64, "publicationContract": "validated-source"})
    monkeypatch.setattr(shader_organization_review, "publication_review", lambda *a: {"ready": True, "sourceSha256": source_sha})
    status = {"workspaceId": "w", "stage": "fragment", "capture": {"api": "Vulkan"}, "entryPoint": "Main",
        "baseline": {"rawSha256": "a" * 64}, "source": {"present": True, "path": str(source),
        "format": "hlsl", "currentSha256": source_sha, "dependenciesDigest": unit["digest"]},
        "latestCompiled": candidate, "abiCompatible": True, "sourceDependenciesCurrent": True}
    collected = collect_organized_sources([str(root)], lambda unused: status)
    assert collected["ready"]
    assert any(Path(row["path"]) == external for row in collected["sources"][0]["dependencyFiles"])
    assert verify_source_files(collected["sources"]) == []
    write(external, "float Lighting() { return 2; }\n")
    assert verify_source_files(collected["sources"])
    assert not collect_organized_sources([str(root)], lambda unused: status)["ready"]


def test_reference_cannot_use_an_unrelated_later_eid(prepared):
    store, task, _, _, _, project = prepared
    with pytest.raises(ValueError, match="selected EID"):
        store.begin(store.load(task)["capture"], 23, str(project), {"event_id": 30})


@pytest.mark.parametrize("field", ["displayMapping", "targetOutput"])
def test_matching_png_cannot_hide_missing_or_different_readback_mapping(prepared, field):
    store, task, observation, *_ = prepared
    wrong = copy.deepcopy(observation)
    wrong.pop(field)
    with pytest.raises(ValueError, match="different output or display mapping"):
        store.compare(task, wrong)
    wrong[field] = ({"channel": "r", "range_mode": "native"} if field == "displayMapping" else
                    {"kind": "global-texture", "name": "_UnrelatedOutput"})
    with pytest.raises(ValueError, match="different output or display mapping"):
        store.compare(task, wrong)
    assert not store.status(task)["complete"]


def test_intermediate_attachment_requires_an_explicit_target_readback(prepared):
    store, task, _, _, _, project = prepared
    capture = store.load(task)["capture"]
    reference = {"event_id": 23, "attachment_kind": "rtv", "attachment_index": 2}
    with pytest.raises(ValueError, match="explicit matching"):
        store.begin(capture, 23, str(project), reference)
    state = store.begin(capture, 23, str(project), reference, target_output={"kind": "global-texture", "name": "_BreadMRT2"})
    assert state["targetOutput"]["name"] == "_BreadMRT2"
