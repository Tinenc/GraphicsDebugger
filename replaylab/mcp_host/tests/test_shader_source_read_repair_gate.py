import hashlib
import json
from unittest import mock

import pytest

from src import server
from src.task_report import TaskReportStore
from src.shader_organization_review import source_snapshot


@pytest.fixture
def organization(tmp_path):
    root = tmp_path / "workspace"
    folder = root / "ai" / "organization-sessions" / ("a" * 32)
    folder.mkdir(parents=True)
    baseline = b"float MainPS() { return 1; }\n"
    proposal = baseline + b"// draft is distinct from the canonical ABI baseline\n"
    (root / "source.hlsl").write_bytes(baseline)
    (folder / "baseline.hlsl").write_bytes(baseline)
    (folder / "proposal.hlsl").write_bytes(proposal)
    (folder / "session.json").write_text(json.dumps({
        "organizationId": "a" * 32, "state": "drafting", "sourcePath": "source.hlsl",
        "baselineSourcePath": str((folder / "baseline.hlsl").relative_to(root)),
        "proposalSourcePath": str((folder / "proposal.hlsl").relative_to(root)),
        "baselineSourceSha256": hashlib.sha256(baseline).hexdigest()}), encoding="utf-8")
    store = TaskReportStore("repair-read", tmp_path / "logs")
    store.begin_task_review("e" * 64)
    snapshot = source_snapshot(str(root), "a" * 32)
    return root, store, snapshot, baseline, proposal


def repair(root, store, baseline):
    manager = mock.Mock()
    manager.repair_baseline_source.return_value = {"accepted": True}
    expected = hashlib.sha256(baseline).hexdigest()
    with mock.patch.object(server, "current_task_report_store", return_value=store), \
         mock.patch.object(server, "_shader_organization_manager", return_value=manager):
        result = json.loads(server.repair_shader_workspace_source_abi(
            str(root), expected, "float MainPS() { return 2; }", True))
    return result, manager, expected


def test_abi_preparation_before_organization_begin_is_not_blocked(organization):
    root, store, snapshot, baseline, proposal = organization
    result, manager, expected = repair(root, store, baseline)
    assert "error" not in result
    manager.repair_baseline_source.assert_called_once()


def test_abi_repair_cannot_bypass_current_organization_full_read(organization):
    root, store, snapshot, baseline, proposal = organization
    store.record_result("begin_shader_workspace_organization", {
        **snapshot, "proposalSourceSha256": snapshot["sourceSha256"]})
    result, manager, expected = repair(root, store, baseline)
    assert "full source read" in result["error"]
    manager.repair_baseline_source.assert_not_called()
    assert (root / "source.hlsl").read_bytes() == baseline


def test_covered_draft_allows_repair_with_independent_canonical_cas(organization):
    root, store, snapshot, baseline, proposal = organization
    store.record_result("begin_shader_workspace_organization", {
        **snapshot, "proposalSourceSha256": snapshot["sourceSha256"]})
    store.record_result("read_shader_workspace_file", {
        "mode": "complete", "workspacePath": str(root), "text": proposal.decode(),
        "sha256": snapshot["sourceSha256"], "sizeBytes": len(proposal),
        "coverage": {"byteOffset": 0, "returnedBytes": len(proposal)}})
    result, manager, expected = repair(root, store, baseline)
    assert "error" not in result
    assert expected != snapshot["sourceSha256"]
    assert manager.repair_baseline_source.call_args.args[1] == expected
