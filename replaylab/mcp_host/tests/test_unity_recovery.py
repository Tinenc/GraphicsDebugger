"""Real asset writes and capability-aware alternatives preserve evidence checks."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from src.unity_recovery import plan_recovery, write_recovery_assets


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Unity project"
    (root / "Assets").mkdir(parents=True)
    (root / "Packages").mkdir()
    (root / "ProjectSettings").mkdir()
    (root / "Packages/manifest.json").write_text('{"dependencies":{}}')
    (root / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.55f1\n")
    return root


def state(*codes):
    return {"restorationId": "a" * 32, "projectPath": "Unity project", "revision": 3,
            "gaps": [{"code": code} for code in codes], "inspection": {"stateFingerprint": "observed"}}


def test_compute_unavailable_still_exposes_raster_and_cpu_execution_routes():
    result = plan_recovery(state("dispatch-requires-render-plan"), {
        "renderPipeline": "urp", "unityVersion": "6000.0.55f1", "graphicsDeviceType": "OpenGLES3",
        "supportsComputeShaders": False})
    routes = {row["id"]: row for row in result["routes"]}
    assert routes["native-compute-adapter"]["state"] == "capability-unavailable"
    assert routes["raster-equivalent-adapter"]["state"] == "available"
    assert routes["cpu-staged-adapter"]["state"] == "available"
    assert result["suggestedRouteId"] == "raster-equivalent-adapter"
    assert not result["terminal"] and not result["hostSemanticApproval"]
    assert "apply_unity_recovery_assets" in {step["tool"] for step in routes["cpu-staged-adapter"]["steps"]}


def test_history_and_hidden_counter_offer_real_input_reconstruction():
    result = plan_recovery(state("export-configuration-gap", "hidden-uav-counter", "history-initialization-required"),
        {"renderPipeline": "hdrp", "unityVersion": "6000.0.55f1"})
    routes = {row["id"]: row for row in result["routes"]}
    assert "history-and-inputs" in result["gapFamilies"]
    assert routes["reconstruct-input-history"]["requiredArtifacts"]
    assert any(step["tool"] == "record_unity_reconstruction" for step in routes["reconstruct-input-history"]["steps"])
    assert not result["acceptanceChanged"]


def test_nested_error_codes_and_unknown_gaps_do_not_make_tools_a_capability_ceiling():
    value = state("new-unknown-failure")
    value["lastToolFailure"] = {"code": "operation_failed", "detail": {"code": "compute-unsupported"}}
    result = plan_recovery(value, {"renderPipeline": "custom", "supportsComputeShaders": False})
    assert "compute-and-platform" in result["gapFamilies"]
    assert any(row["id"] == "custom-versioned-adapter" and row["applicable"] for row in result["routes"])


def test_identity_gap_routes_to_rebinding_instead_of_inferred_success():
    result = plan_recovery(state("restoration-input-changed"), {"renderPipeline": "urp"})
    assert result["suggestedRouteId"] == "refresh-exact-evidence"
    assert "never inferred away" in result["routes"][0]["reason"]
    assert result["terminal"] is False


def test_attempts_need_current_context_and_actual_evidence_before_changing_route():
    value = state("compute-unsupported")
    caps = {"renderPipeline": "urp", "supportsComputeShaders": True}
    first = plan_recovery(value, caps)
    attempt = {"routeId": "native-compute-adapter", "contextFingerprint": first["contextFingerprint"], "outcome": "unsupported"}
    narrative_only = plan_recovery(value, caps, [attempt])
    assert narrative_only["suggestedRouteId"] == "native-compute-adapter"
    attempt["observationFingerprint"] = "real-observation-hash"
    second = plan_recovery(value, caps, [attempt])
    assert second["suggestedRouteId"] == "raster-equivalent-adapter"
    attempt["contextFingerprint"] = "old-context"
    assert plan_recovery(value, caps, [attempt])["suggestedRouteId"] == "native-compute-adapter"


def test_failure_invalidation_revision_alone_does_not_reset_recovery_budget():
    value = state("compute-unsupported")
    first = plan_recovery(value, {"renderPipeline": "urp"})
    value["revision"] += 20
    second = plan_recovery(value, {"renderPipeline": "urp"})
    assert first["contextFingerprint"] == second["contextFingerprint"]
    value["sources"] = [{"stage": "compute", "sourceSha256": "a" * 64}]
    assert plan_recovery(value, {"renderPipeline": "urp"})["contextFingerprint"] != first["contextFingerprint"]


def test_target_api_and_package_changes_reset_context_without_claiming_player_execution():
    value = state("compute-unsupported")
    caps = {"renderPipeline": "urp", "supportsComputeShaders": True,
        "activeBuildTarget": "Android", "graphicsDeviceType": "Direct3D11",
        "graphicsCapabilityScope": "active-editor-device; target-player-hardware-not-executed",
        "activeBuildTargetInspection": {"target": "Android", "configuredGraphicsAPIs": ["Vulkan"]},
        "packages": {"com.unity.render-pipelines.universal": "17.0.4"}}
    first = plan_recovery(value, caps)
    assert first["target"]["graphicsAPI"] == "Direct3D11"
    assert first["target"]["buildTargetInspection"]["configuredGraphicsAPIs"] == ["Vulkan"]
    assert first["target"]["targetPlayerVerified"] is False
    caps["activeBuildTargetInspection"]["configuredGraphicsAPIs"] = ["OpenGLES3"]
    second = plan_recovery(value, caps)
    assert second["contextFingerprint"] != first["contextFingerprint"]
    caps["packages"]["com.unity.render-pipelines.universal"] = "17.2.0"
    assert plan_recovery(value, caps)["contextFingerprint"] != second["contextFingerprint"]


def test_authoring_writes_multifile_project_assets_and_is_idempotent(project):
    assets = [{"path": "Runtime/History.cs", "content": "public class History {}"},
              {"path": "Shaders/History.compute", "content": "#pragma kernel Seed\n[numthreads(1,1,1)] void Seed() {}"},
              {"path": "Data/Seed.bytes", "contentBase64": base64.b64encode(bytes(range(32))).decode()}]
    result = write_recovery_assets(str(project), "a" * 32, assets, "Reconstruct a bounded previous-frame input",
        provenance={"classification": "history-reconstruction", "rationale": "Kernel reads a previous-frame moment buffer"})
    assert result["changedFiles"] == 3
    assert result["compiled"] is False and result["executed"] is False and result["captureDataModified"] is False
    for row in result["files"]:
        assert Path(row["path"]).is_file()
        assert row["assetPath"] == "Assets/BreadRecovery/" + "a" * 32 + "/" + row["relativePath"]
        assert hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() == row["sha256"]
    again = write_recovery_assets(str(project), "a" * 32, assets, "Reconstruct a bounded previous-frame input",
        provenance={"classification": "history-reconstruction", "rationale": "Kernel reads a previous-frame moment buffer"})
    assert again["idempotent"] and again["changedFiles"] == 0 and again["revision"] == result["revision"]


def test_revision_and_file_preconditions_preserve_external_edits(project):
    first = write_recovery_assets(str(project), "a" * 32, [{"path": "Effect.shader", "content": "old Shader"}], "Initial adapter")
    path = Path(first["files"][0]["path"])
    with pytest.raises(ValueError, match="expected_revision"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "Effect.shader", "content": "new Shader"}], "Fix computation")
    path.write_text("external edit")
    with pytest.raises(ValueError, match="external edits"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "Effect.shader", "content": "new Shader"}], "Fix computation", first["revision"])
    assert path.read_text() == "external edit"
    updated = write_recovery_assets(str(project), "a" * 32,
        [{"path": "Effect.shader", "content": "new Shader", "expectedSha256": hashlib.sha256(path.read_bytes()).hexdigest()}],
        "Repair the inspected external edit", first["revision"])
    assert updated["changedFiles"] == 1 and path.read_text() == "new Shader"


@pytest.mark.parametrize("relative", ["../Other.cs", "/Other.cs", "Runtime/../../Other.cs", "C:/Other.cs", "a\\Other.cs", "CON.cs", "a/../B.cs", "bread-recovery-manifest.json", "a.txt", "a\n.cs"])
def test_authoring_rejects_escape_and_reserved_paths_before_any_write(project, relative):
    with pytest.raises(ValueError):
        write_recovery_assets(str(project), "a" * 32, [{"path": relative, "content": "code"}], "Repair")
    assert not (project / "Assets/BreadRecovery").exists()


def test_all_assets_are_preflighted_before_replacing_any(project):
    first = write_recovery_assets(str(project), "a" * 32, [{"path": "First.cs", "content": "first"}], "Initial")
    root = project / "Assets/BreadRecovery" / ("a" * 32)
    (root / "Second.cs").write_text("user file")
    with pytest.raises(ValueError, match="Unmanaged"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "First.cs", "content": "changed"},
            {"path": "Second.cs", "content": "overwrite"}], "Change", first["revision"])
    assert (root / "First.cs").read_text() == "first"
    assert (root / "Second.cs").read_text() == "user file"


def test_source_file_hash_and_manifest_integrity_are_checked(project, tmp_path):
    source = tmp_path / "seed.bytes"
    source.write_bytes(b"seed")
    with pytest.raises(ValueError, match="source file changed"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "Seed.bytes", "sourcePath": str(source), "sha256": "0" * 64}], "Seed")
    result = write_recovery_assets(str(project), "a" * 32,
        [{"path": "Seed.bytes", "sourcePath": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}], "Seed")
    manifest = Path(result["manifestPath"])
    tampered = json.loads(manifest.read_text())
    tampered["purpose"] = "changed"
    manifest.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="manifest content changed"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "New.cs", "content": "new"}], "New", result["revision"])


def test_generated_input_cannot_be_labelled_as_observed_capture(project):
    with pytest.raises(ValueError, match="provenance"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "Seed.bytes", "content": "generated"}], "Seed",
                              provenance={"classification": "captured"})


@pytest.mark.parametrize("declared,canonical", [("inferred-history", "history-reconstruction"),
    ("inferred-layout", "inferred-input"), ("unity-native", "platform-adapter")])
def test_reconstruction_classification_can_be_reused_for_authoring(project, declared, canonical):
    result = write_recovery_assets(str(project), "a" * 32, [{"path": "Input.bytes", "content": "derived"}], "Adapter",
        provenance={"classification": declared, "rationale": "Derived from observed use"})
    assert result["provenance"]["classification"] == canonical
    assert result["provenance"]["declaredClassification"] == declared
    repeat = write_recovery_assets(str(project), "a" * 32, [{"path": "Input.bytes", "content": "derived"}], "Adapter",
        provenance={"classification": declared, "rationale": "Derived from observed use"})
    assert repeat["idempotent"]


def test_file_directory_conflicts_are_rejected_before_writing_first_asset(project):
    with pytest.raises(ValueError, match="file and directory"):
        write_recovery_assets(str(project), "a" * 32, [
            {"path": "History.cs", "content": "first"},
            {"path": "History.cs/Next.compute", "content": "second"}], "Conflicting paths")
    assert not (project / "Assets/BreadRecovery").exists()


def test_existing_non_directory_parent_is_preflighted_before_any_replace(project):
    first = write_recovery_assets(str(project), "a" * 32, [{"path": "First.cs", "content": "first"}], "Initial")
    root = project / "Assets/BreadRecovery" / ("a" * 32)
    (root / "Folder").write_bytes(b"external file")
    with pytest.raises(ValueError, match="parent is not a directory"):
        write_recovery_assets(str(project), "a" * 32, [
            {"path": "First.cs", "content": "changed"},
            {"path": "Folder/Next.compute", "content": "second"}], "Change", first["revision"])
    assert (root / "First.cs").read_text() == "first"


def test_case_aliases_cannot_create_platform_dependent_manifest_entries(project):
    first = write_recovery_assets(str(project), "a" * 32, [{"path": "Effect.shader", "content": "original"}], "Initial")
    with pytest.raises(ValueError, match="differs only by case"):
        write_recovery_assets(str(project), "a" * 32, [{"path": "effect.shader", "content": "edited"}], "Fix", first["revision"])
    assert Path(first["files"][0]["path"]).read_text() == "original"
