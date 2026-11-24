from __future__ import annotations

import copy

import pytest

from src.draw_workflow import ACTIONS, build_draw_workflow_plan


def _context():
    return {
        "capture_identity": {
            "captureFingerprint": "capture-bytes-v1", "eventId": 123, "stage": "PS",
            "shaderSha256": "original-shader", "sourceSha256": "current-source",
            "bindingsHash": "bindings-v1", "resourceHash": "resources-v1",
            "constantsHash": "constants-v1", "namingHash": "names-v1",
            "organizationSettingsHash": "organization-v1",
        },
        "source_engine": "unreal",
        "asset_context": {
            "roots": ["D:/Game/Content"], "indexRevision": "index-v1",
            "readerStatus": "available", "readerId": "uex",
        },
        "unity_context": {
            "pipeline": "urp",
            "projectPath": "D:/RestoredProject", "editorVersion": "6000.0.1f1",
            "pipelineVersion": "17.0.1", "providerVersion": "pinned-v1",
            "translationSettingsHash": "translation-v1", "transferSpecHash": "spec-v1",
            "settingsHash": "import-settings-v1", "exportPackageHash": "package-v1",
            "shaderTransferHash": "translated-source-v1", "importHash": "import-v1",
            "referenceHash": "reference-v1", "validationSettingsHash": "comparison-v1",
        },
    }


def _steps(plan):
    return {step["id"]: step for step in plan["steps"]}


def _completed(action, context):
    """Simulate Host-observed tool receipts, not an execution by the planner."""
    records = []
    for _ in range(20):
        plan = build_draw_workflow_plan(action, artifacts=records, **context)
        candidates = [step for step in plan["steps"]
                      if step["status"] == "run" and step["inputFingerprint"]]
        if not candidates:
            return records
        for step in candidates:
            records.append({"step": step["id"], "inputFingerprint": step["inputFingerprint"],
                            "receiptRef": "observed:" + step["id"], "status": "complete",
                            "outputFingerprint": "output:" + step["id"],
                            "outputsPresent": True})
    raise AssertionError("the plan did not settle")


@pytest.mark.parametrize("reader_status", ["missing", "failed", "unsupported", "unknown"])
def test_missing_reader_never_blocks_shader_and_never_starts_unity(reader_status):
    context = _context()
    context["asset_context"]["readerStatus"] = reader_status
    plan = build_draw_workflow_plan("organize_shader", **context)
    steps = _steps(plan)
    assert set(steps) == {"capture_evidence", "asset_lookup", "shader_source", "shader_organization"}
    assert steps["asset_lookup"]["status"] in {"optional_skip", "optional"}
    assert steps["shader_organization"]["status"] == "run"
    assert plan["target"] is None
    assert plan["nameProvenance"] == "inferred_until_evidenced"
    assert plan["execution"] == "not_started"


def test_unreal_assets_and_unknown_engine_can_export_to_unity_independently():
    context = _context()
    plan = build_draw_workflow_plan("export_unity", **context)
    assert plan["sourceEngine"] == "unreal"
    assert plan["target"] == {"engine": "unity", "pipeline": "urp"}
    context.update(source_engine="custom_game_engine", asset_context={"readerStatus": "missing"})
    unknown = build_draw_workflow_plan("export_unity", **context)
    assert unknown["sourceEngine"] == "custom_game_engine"
    assert _steps(unknown)["resource_export"]["status"] == "run"
    assert not unknown["policy"]["missingReaderBlocksCaptureExport"]


def test_operation_scope_does_not_expand_to_whole_workflow():
    context = _context()
    names = _steps(build_draw_workflow_plan("resolve_names", **context))
    assert set(names) == {"capture_evidence", "asset_lookup", "name_resolution"}
    validation = _steps(build_draw_workflow_plan("validate_unity", **context))
    assert set(validation) == {"unity_connection", "unity_validation"}
    update = _steps(build_draw_workflow_plan("update_unity_shader", **context))
    assert "resource_export" not in update
    assert "asset_lookup" not in update
    assert "shader_organization" not in update
    assert "unity_shader_update" in update


def test_functional_export_prefers_bread_preparation_without_blocking_fallback():
    steps = _steps(build_draw_workflow_plan("export_unity", **_context()))
    assert steps["shader_translation"]["dependsOn"] == ["shader_source"]
    preparation = steps["shader_organization"]
    assert preparation["preferred"] is True and preparation["blocking"] is False
    assert preparation["fallback"]["destination"] == "unity"
    assert preparation["fallback"]["preservePartialEvidence"] is True


def test_changing_target_pipeline_reuses_capture_but_not_translated_source():
    context = _context()
    receipts = _completed("export_unity", context)
    context["unity_context"]["pipeline"] = "hdrp"
    result = build_draw_workflow_plan("export_unity", artifacts=receipts, **context)
    assert result["target"]["pipeline"] == "hdrp"
    steps = _steps(result)
    assert steps["shader_source"]["status"] == "reuse"
    assert steps["resource_export"]["status"] == "reuse"
    assert steps["shader_translation"]["status"] == "run"


def test_existing_project_without_observed_pipeline_cannot_reuse_urp_translation():
    context = _context()
    receipts = _completed("export_unity", context)
    del context["unity_context"]["pipeline"]
    result = build_draw_workflow_plan("export_unity", artifacts=receipts, **context)
    assert result["target"]["pipeline"] == "unverified"
    assert "pipeline" in _steps(result)["shader_translation"]["requiredEvidence"]
    assert _steps(result)["shader_translation"]["status"] == "run"


def test_matching_receipts_reuse_without_claiming_execution():
    context = _context()
    receipts = _completed("export_unity", context)
    plan = build_draw_workflow_plan("export_unity", artifacts=receipts, **context)
    assert all(step["status"] == "reuse" for step in plan["steps"])
    assert plan["execution"] == "not_started"
    assert plan["advisoryOnly"] is True
    assert not plan["policy"]["planProvesExecution"]


def test_changed_shader_invalidates_shader_and_import_but_preserves_capture_export():
    context = _context()
    receipts = _completed("export_unity", context)
    context["capture_identity"]["sourceSha256"] = "edited-source"
    steps = _steps(build_draw_workflow_plan("export_unity", artifacts=receipts, **context))
    assert steps["capture_evidence"]["status"] == "reuse"
    assert steps["resource_export"]["status"] == "reuse"
    assert steps["shader_source"]["status"] == "run"
    assert steps["shader_translation"]["status"] == "run"
    assert steps["unity_import"]["status"] == "run"
    assert steps["unity_validation"]["status"] == "run"


@pytest.mark.parametrize("changed", ["captureFingerprint", "eventId", "resourceHash", "constantsHash"])
def test_changed_draw_invalidates_export_and_current_names(changed):
    context = _context()
    export_receipts = _completed("export_unity", context)
    name_receipts = _completed("resolve_names", context)
    context["capture_identity"][changed] = 456 if changed == "eventId" else "changed"
    exports = _steps(build_draw_workflow_plan("export_unity", artifacts=export_receipts, **context))
    names = _steps(build_draw_workflow_plan("resolve_names", artifacts=name_receipts, **context))
    assert exports["resource_export"]["status"] == "run"
    assert names["name_resolution"]["status"] == "run"
    assert exports["shader_source"]["status"] == "reuse"
    assert exports["unity_connection"]["status"] == "reuse"


def test_new_unity_project_preserves_source_work_and_export():
    context = _context()
    receipts = _completed("export_unity", context)
    context["unity_context"]["projectPath"] = "D:/AnotherProject"
    steps = _steps(build_draw_workflow_plan("export_unity", artifacts=receipts, **context))
    for step in ("capture_evidence", "shader_source", "shader_translation", "resource_export"):
        assert steps[step]["status"] == "reuse"
    for step in ("unity_connection", "unity_import", "unity_validation"):
        assert steps[step]["status"] == "run"


def test_dead_connection_is_reconnected_without_reimporting_unchanged_files():
    context = _context()
    receipts = _completed("export_unity", context)
    next(record for record in receipts if record["step"] == "unity_connection")["outputsPresent"] = False
    steps = _steps(build_draw_workflow_plan("export_unity", artifacts=receipts, **context))
    assert steps["unity_connection"]["status"] == "run"
    assert steps["unity_import"]["status"] == "reuse"
    assert steps["unity_validation"]["status"] == "reuse"


def test_hash_bound_negative_asset_lookup_does_not_retry_until_index_changes():
    context = _context()
    receipts = _completed("resolve_names", context)
    record = next(record for record in receipts if record["step"] == "asset_lookup")
    record["status"] = "no_match"
    plan = build_draw_workflow_plan("resolve_names", artifacts=receipts, **context)
    assert _steps(plan)["asset_lookup"]["reason"] == "cached_no_match"
    assert _steps(plan)["name_resolution"]["status"] == "reuse"
    context["asset_context"]["indexRevision"] = "index-v2"
    changed = _steps(build_draw_workflow_plan("resolve_names", artifacts=receipts, **context))
    assert changed["asset_lookup"]["status"] == "run"


def test_missing_source_hash_never_reuses_path_or_eid_only_artifact():
    context = _context()
    receipts = _completed("organize_shader", context)
    del context["capture_identity"]["shaderSha256"]
    steps = _steps(build_draw_workflow_plan("organize_shader", artifacts=receipts, **context))
    assert steps["shader_source"]["status"] == "run"
    assert steps["shader_source"]["inputFingerprint"] is None
    assert "shaderSha256" in steps["shader_source"]["requiredEvidence"]


@pytest.mark.parametrize("invalid", ["outputsPresent", "receiptRef", "outputFingerprint", "status"])
def test_unconfirmed_receipts_are_not_reused(invalid):
    context = _context()
    receipts = _completed("organize_shader", context)
    record = next(record for record in receipts if record["step"] == "shader_source")
    record[invalid] = False if invalid == "outputsPresent" else ""
    steps = _steps(build_draw_workflow_plan("organize_shader", artifacts=receipts, **context))
    assert steps["shader_source"]["status"] == "run"
    assert steps["shader_organization"]["status"] == "run"


def test_repaired_output_invalidates_consumers_even_when_inputs_are_identical():
    context = _context()
    receipts = _completed("export_unity", context)
    record = next(record for record in receipts if record["step"] == "resource_export")
    record["outputFingerprint"] = "repaired-package"
    steps = _steps(build_draw_workflow_plan("export_unity", artifacts=receipts, **context))
    assert steps["resource_export"]["status"] == "reuse"
    assert steps["unity_import"]["status"] == "run"


def test_windows_project_path_normalization_and_input_immutability():
    context = _context()
    original = copy.deepcopy(context)
    receipts = _completed("validate_unity", context)
    assert context == original
    context["unity_context"]["projectPath"] = "d:\\restoredproject\\"
    plan = build_draw_workflow_plan("validate_unity", artifacts=receipts, **context)
    assert all(step["status"] == "reuse" for step in plan["steps"])


@pytest.mark.parametrize("action", ACTIONS)
def test_no_context_advises_missing_evidence_without_fabricated_execution(action):
    plan = build_draw_workflow_plan(action)
    assert plan["execution"] == "not_started"
    assert all(step["status"] in {"run", "optional_skip"} for step in plan["steps"])
    assert all(step["inputFingerprint"] is None for step in plan["steps"])


def test_rejects_malformed_target_without_coercing_true_into_eid_one():
    with pytest.raises(ValueError, match="eventId"):
        build_draw_workflow_plan("organize_shader", capture_identity={"eventId": True})
    with pytest.raises(ValueError, match="asset roots"):
        build_draw_workflow_plan("organize_shader", asset_context={"roots": "D:/Game"})
