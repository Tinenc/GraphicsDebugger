"""Container misses must not hide strictly equivalent Shader source."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src import project_restore
from src.shader_workspace.binary_formats import fingerprint_shader_executable
from tests import test_projects as project_fixtures
from tests import test_project_restore as restore_fixtures
from tests.test_project_equivalent_source_advance import make_rig

_dxbc = project_fixtures._dxbc


@pytest.fixture
def code_project():
    case = project_fixtures.ReplayProjectTests(methodName="runTest")
    case.setUp()
    try:
        catalog, shader, capture_id = case._catalog("source-code", _dxbc(metadata=b"debug A"), resource_id=91)
        case.service.import_capture(catalog)
        status = case._workspace_status("source-code", capture_id, shader,
            "float4 main() : SV_Target { return 1; }\n")
        knowledge = case.service.register_workspace(status)["knowledge"]
        yield case, shader, knowledge
    finally:
        case.tearDown()


def test_code_search_routes_only_missing_current_heads_for_same_api(code_project):
    case, shader, knowledge = code_project
    service = case.service
    assert service.restorable_shader_code_hashes([shader["rawSha256"]], "D3D11") == [shader["codeSha256"]]
    assert service.restorable_shader_code_hashes([], "D3D11") == []
    assert service.restorable_shader_code_hashes(["0" * 64], "D3D11") == []
    assert service.restorable_shader_code_hashes([shader["rawSha256"]], "D3D12") == []
    assert service.restorable_shader_code_hashes([shader["rawSha256"]], "OpenGL") == []
    assert service.restorable_shader_code_hashes([shader["rawSha256"]], "Vulkan") == []


def test_newest_equivalent_publication_wins_over_larger_local_head_generation(make_rig):
    r = make_rig(high_generation=True)
    plan = r.f.service.reuse_plan(r.a["capture"]["captureId"])
    candidate, = plan["matches"][0]["workspaceCandidates"]
    assert candidate["knowledgeId"] == r.newest["knowledgeId"]
    assert candidate["matchMode"] == "code-interface-layout-exact"
    assert candidate["activeCandidate"]
    # Missing B must trigger lookup even when raw-exact A is already observed.
    assert r.f.service.restorable_shader_code_hashes([r.b["baseline"]["rawSha256"]], "D3D11")
    # A capture can contain both original containers. It still needs A updated.
    assert r.f.service.restorable_shader_code_hashes([], "D3D11")


def test_old_raw_family_cannot_keep_a_verified_inventory_snapshot_current(make_rig):
    r = make_rig()
    inventory = r.f._shader_inventory(resource_id=r.a["shader"]["resourceId"],
        raw_sha256=r.first["rawSha256"], layout_hash=r.first["layoutHash"],
        interface_hash=r.first["interfaceHash"])
    with pytest.raises(project_fixtures.ReplayProjectError, match="newer equivalent Shader source"):
        r.f.service.reuse_snapshot_for_shader_inventory(inventory)


def test_current_observed_family_does_not_rescan_for_absent_old_container(make_rig):
    r = make_rig()
    assert r.f.service.restorable_shader_code_hashes([r.a["baseline"]["rawSha256"]], "D3D11") == []
    # Once A has been advanced too, either raw container can reopen directly.
    text = Path(r.b["source"]["path"]).read_text()
    r.publish(r.a, text, 22)
    assert r.f.service.restorable_shader_code_hashes([r.b["baseline"]["rawSha256"]], "D3D11") == []
    assert r.f.service.restorable_shader_code_hashes([], "D3D11") == []


@pytest.fixture
def recovery(tmp_path):
    payload = _dxbc(metadata=b"debug B")
    executable = fingerprint_shader_executable(payload, "target")
    old_raw = hashlib.sha256(_dxbc(metadata=b"debug A")).hexdigest()
    identity = dict(resourceId=90, api="d3d11", encoding="dxbc", stage="fragment",
        entryPoint="main", rawSha256=executable.raw_sha256, abiHash="a" * 64,
        layoutHash="b" * 64, stableKeyComplete=True, anchorEventId=11)
    shader = {**project_restore._normal_shader_identity(identity),
        "interfaceComplete": True, "identityAmbiguous": False}
    full = {"shaders": [shader], "rawBytecode": {shader["rawSha256"]: payload}}
    original = {"shaders": [], "targetRawSha256s": [old_raw],
                "unobservedTargetRawSha256s": [old_raw]}
    session, project_id = "c" * 32, "d" * 32
    capture_id, catalog_sha = "e" * 64, "f" * 64
    candidate = dict(knowledgeId="1" * 32, sourceSha256="2" * 64,
        matchMode="code-interface-layout-exact", activeCandidate=True, sourceSeedEligible=True)
    plan = {"matches": [{"shader": identity, "workspaceCandidates": [candidate]}]}
    located = dict(schema="replaylab-active-shader-workspace-path/v1", schemaVersion=1,
        captureId=capture_id, catalogSha256=catalog_sha, captureSessionId=session,
        shaderResourceId=90, rawSha256=shader["rawSha256"], stage="fragment", entryPoint="main", eventId=11)
    source = {"currentSha256": candidate["sourceSha256"], "projectSource": {
        "knowledgeId": candidate["knowledgeId"], "sourceSha256": candidate["sourceSha256"],
        "originalRawSha256": shader["rawSha256"], "projectId": project_id,
        "requiresCompile": True, "requiresLiveAbiRevalidation": True}}
    status = {"source": source, "latestCompiled": None}
    host = SimpleNamespace(get_active_shader_workspace_path=mock.Mock(side_effect=lambda *a, **kw: json.dumps(located)))
    replay = SimpleNamespace(restorable_shader_code_hashes=mock.Mock(return_value=[executable.code_sha256]),
        reuse_plan=mock.Mock(return_value=plan), published_workspace_source_advance=mock.Mock(return_value=None),
        seed_workspace=mock.Mock(), register_workspace=mock.Mock(return_value={"knowledge": {
            "rawSha256": shader["rawSha256"], "sourceSha256": candidate["sourceSha256"]}}))
    active = {"replayProject": {"projectId": project_id}}
    progress, diagnostic = mock.Mock(), {}
    def run():
        return project_restore._recover_equivalent_shader_sources(host, replay, active,
            tmp_path, tmp_path / "capture.rdc", session, "D3D11", original, progress,
            mock.Mock(), diagnostic)
    with (mock.patch.object(project_restore, "_inventory_shader_resources", return_value=full) as scan,
          mock.patch.object(project_restore, "_require_restore_context") as context,
          mock.patch.object(project_restore, "_reuse_workspace_status", side_effect=lambda *a, **kw: status),
          mock.patch.object(project_restore, "_prepare_reuse_workspace", return_value=(
              tmp_path / "shader", status, capture_id, catalog_sha)) as prepare):
        yield SimpleNamespace(**locals())


def test_raw_all_hit_keeps_target_only_fast_path(recovery):
    r = recovery
    r.original["unobservedTargetRawSha256s"] = []
    r.replay.restorable_shader_code_hashes.return_value = []
    assert r.run() is r.original
    r.scan.assert_not_called()
    r.host.get_active_shader_workspace_path.assert_not_called()


def test_debug_only_container_difference_prepares_current_source_without_binary_authority(recovery):
    r = recovery
    assert r.run() is r.full
    assert r.original["targetRawSha256s"][0] != r.shader["rawSha256"]
    assert r.diagnostic["prepared"] == 1
    r.scan.assert_called_once()
    assert "target_raw_sha256s" not in r.scan.call_args.kwargs
    r.replay.reuse_plan.assert_called_once_with(r.capture_id, r.catalog_sha)
    r.replay.register_workspace.assert_called_once_with(r.status, include_analysis=False)
    assert r.prepare.call_args.kwargs["expected_source_sha256"] == r.candidate["sourceSha256"]
    assert r.prepare.call_args.kwargs["expected_anchor_event_id"] == 11
    assert r.status["latestCompiled"] is None


@pytest.mark.parametrize("rejection", ["different-code", "different-interface", "ambiguous-match", "wrong-api"])
def test_code_lookup_never_promotes_coarse_matches(recovery, rejection):
    r = recovery
    if rejection == "different-code":
        r.replay.restorable_shader_code_hashes.return_value = ["0" * 64]
    elif rejection == "different-interface":
        r.plan["matches"] = []
    elif rejection == "ambiguous-match":
        r.plan["matches"][0]["workspaceCandidates"].append(copy.deepcopy(r.candidate))
    else:
        r.replay.restorable_shader_code_hashes.return_value = []
    assert r.run() is r.original
    r.prepare.assert_not_called()
    r.replay.register_workspace.assert_not_called()


def test_capture_switch_during_equivalent_lookup_cannot_publish(recovery):
    r = recovery
    r.located["captureSessionId"] = "0" * 32
    with pytest.raises(project_restore.RestoreContextChanged):
        r.run()
    r.prepare.assert_not_called()
    r.replay.register_workspace.assert_not_called()


def test_unpublished_local_source_is_never_registered_as_equivalent_project_source(recovery):
    r = recovery
    r.status["source"]["currentSha256"] = "0" * 64
    with pytest.raises(project_restore.RestoreLocalEdit, match="unpublished local source"):
        r.run()
    r.replay.register_workspace.assert_not_called()


def test_older_published_family_source_advances_with_transaction_receipt(recovery):
    r = recovery
    r.status["source"].update(currentSha256="0" * 64, modifiedSinceDecompile=True)
    receipt = {"schema": "renderdoc-ultra-published-workspace-source-advance/v1", "generation": 2}
    r.replay.published_workspace_source_advance.return_value = receipt
    def seeded(knowledge_id, status, **kwargs):
        assert kwargs == {"published_source_advance": receipt}
        status["source"]["currentSha256"] = r.candidate["sourceSha256"]
    r.replay.seed_workspace.side_effect = seeded
    assert r.run() is r.full
    r.replay.seed_workspace.assert_called_once()
    assert r.diagnostic["prepared"] == 1


def test_observed_old_family_uses_target_inventory_without_full_scan(recovery):
    r = recovery
    r.original.update(r.full, inventoryScope="target-raw-sha256", unobservedTargetRawSha256s=[])
    assert r.run() is r.original
    r.scan.assert_not_called()
    assert r.diagnostic["prepared"] == 1


def test_recovered_source_uses_normal_compile_apply_and_gui_restore_contract():
    case = restore_fixtures.ProjectRestoreTests(methodName="runTest")
    case.setUp()
    try:
        def recovered(host, replay, active, project, capture, session, api,
                      inventory, progress, checkpoint, diagnostic):
            diagnostic.update(prepared=1, candidateCount=1)
            return project_restore._inventory_shader_resources(
                host, capture, session, api, progress, checkpoint)
        with mock.patch.object(project_restore, "_recover_equivalent_shader_sources", side_effect=recovered):
            result, host, unused_replay, unused_progress = case._run(index_present=False, auto_prepare=True)
        assert result["completed"], result
        assert result["restoreMode"] == "full-inventory"
        assert result["shaders"]["applied"] == 1
        assert host.compile_calls == 1
        assert host.apply_calls == 1
        path = project_restore.Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/project_restore_client.py"
        spec = restore_fixtures.importlib.util.spec_from_file_location("code_source_restore_client", path)
        client = restore_fixtures.importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        assert client.ReplayLabRestoreHostClient._result(
            result, str(case.project), str(case.capture_path), case.session_id)["completed"]
    finally:
        case.tearDown()
