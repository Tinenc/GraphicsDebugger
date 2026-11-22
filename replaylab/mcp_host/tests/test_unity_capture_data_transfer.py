"""Unity reference data is independent of captured-pipeline replacement proofs."""
from pathlib import Path

import pytest

from src.unity_shader_sources import collect_organized_sources, verify_source_files, stage_name
from src.unity_recovery import plan_recovery
from tests.test_unity_restoration import prepared, write


@pytest.mark.parametrize("stage", ["vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh", "raygen", "intersection", "anyhit", "closesthit", "miss", "callable"])
def test_uncompiled_reference_sources_work_for_every_stage_and_detect_changed_includes(tmp_path, stage):
    source = tmp_path / "source.hlsl"
    sha = write(source, '#include "input.hlsli"\nfloat4 Main() { return VALUE; }')
    write(tmp_path / "input.hlsli", "#define VALUE 1\n")
    status = {"stage": stage, "baseline": {"rawSha256": "a" * 64}, "capture": {"api": "Vulkan"},
        "source": {"present": True, "path": str(source), "currentSha256": sha}}
    result = collect_organized_sources([str(tmp_path)], lambda _: status, require_replay_validation=False)
    assert result["ready"]
    row = result["sources"][0]
    assert row["validation"]["replayValidated"] is False
    assert verify_source_files([row]) == []
    write(tmp_path / "input.hlsli", "#define VALUE 2\n")
    assert verify_source_files([row])
    assert not collect_organized_sources([str(tmp_path)], lambda _: status)["ready"]


def test_raw_capture_package_can_bind_without_an_organized_replacement(prepared, monkeypatch):
    from src import unity_restoration
    store, task, _, _, bundle, project = prepared
    state = store.load(task)
    monkeypatch.setattr(unity_restoration, "package_bundle", lambda _: (bundle, {"shader": {}}))
    result = store.bind(task, {"status": "finalized", "operationId": "f" * 32,
        "packagePath": state["binding"]["packagePath"]}, {"requestPath": state["binding"]["requestPath"]}, [])
    assert store.load(task)["sources"] == []
    assert not result["complete"]  # Import does not prove a visible restoration.


def test_unbound_export_routes_to_data_preparation_not_compute_or_imaginary_images():
    state = {"restorationId": "a" * 32, "projectPath": "Unity", "binding": None,
        "gaps": [{"code": "export-and-organized-sources-required"}, {"code": "current-render-comparison-required"}],
        "lastToolFailure": {"code": "operation_failed", "detail": "vertex output validation unsupported"}}
    result = plan_recovery(state, {"renderPipeline": "urp", "supportsComputeShaders": True})
    assert result["suggestedRouteId"] == "prepare-capture-data"
    assert [route["id"] for route in result["routes"]] == ["prepare-capture-data"]
