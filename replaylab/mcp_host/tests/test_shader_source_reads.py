import hashlib
import json
from pathlib import Path

import pytest

from src.shader_source_reads import bounded_read_payload, require_current, state
from src.task_report import TaskReportStore
from src.shader_organization_review import source_snapshot


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "shader"
    folder = root / "ai/organization-sessions" / ("a" * 32)
    folder.mkdir(parents=True)
    raw = "float MainPS() { /* 中文 */ return 1; }\n".encode()
    for name in ("baseline", "proposal"):
        (folder / (name + ".hlsl")).write_bytes(raw)
    (root / "source.hlsl").write_bytes(raw)
    (folder / "session.json").write_text(json.dumps({
        "organizationId": "a" * 32, "state": "drafting", "sourcePath": "source.hlsl",
        "baselineSourcePath": str((folder / "baseline.hlsl").relative_to(root)),
        "proposalSourcePath": str((folder / "proposal.hlsl").relative_to(root)),
        "baselineSourceSha256": hashlib.sha256(raw).hexdigest()}))
    store = TaskReportStore("task-read", tmp_path / "logs")
    store.begin_task_review("e" * 64)
    snapshot = source_snapshot(str(root), "a" * 32)
    store.record_result("begin_shader_workspace_organization", {
        **snapshot, "proposalSourceSha256": snapshot["sourceSha256"]})
    return store, snapshot, raw


def deliver(store, snap, text, start=0, end=None, mode="range"):
    chunk = text[start:end]
    payload = {"mode": mode, "workspacePath": snap["workspacePath"], "text": chunk,
        "sizeBytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "coverage": {"byteOffset": len(text[:start].encode()), "returnedBytes": len(chunk.encode())}}
    store.record_result("read_shader_workspace_file", payload)


def current(store):
    return store.shader_organization_review_state()["targets"][0]


def test_full_current_read_required_and_copy_outline_old_task_do_not_count(setup):
    store, snap, raw = setup
    text = raw.decode()
    with pytest.raises(ValueError, match="full source read"):
        require_current(store, snap["workspacePath"], snap["organizationId"], snap["sourceSha256"])
    deliver(store, snap, text, mode="file")
    store.record_result("get_shader_workspace_outline", {"sourceSha256": snap["sourceSha256"]})
    assert not current(store)["sourceReadState"]["complete"]
    deliver(store, snap, text, end=10)
    assert current(store)["sourceReadState"]["missingByteRanges"] == [[10, len(raw)]]
    deliver(store, snap, text, start=10)
    require_current(store, snap["workspacePath"], snap["organizationId"], snap["sourceSha256"])
    # Same task+organization resume retains coverage; a new request does not.
    store.record_result("begin_shader_workspace_organization", {**snap, "proposalSourceSha256": snap["sourceSha256"]})
    assert current(store)["sourceReadState"]["complete"]
    store.begin_task_review("f" * 64)
    assert current(store)["sourceReadState"]["required"] is False  # historical restore alone
    store.record_result("begin_shader_workspace_organization", {**snap, "proposalSourceSha256": snap["sourceSha256"]})
    assert not current(store)["sourceReadState"]["complete"]


def test_observed_write_chain_reuses_read_baseline_but_external_edit_requires_new_read(setup):
    store, snap, raw = setup
    deliver(store, snap, raw.decode(), mode="complete")
    target = Path(snap["sourcePath"])
    target.write_bytes(raw + b"// authored edit\n")
    store.record_result("write_shader_workspace_organization_file", {
        **snap, "kind": "proposal", "previousProposalSha256": snap["sourceSha256"]}, arguments={"kind": "proposal"})
    assert current(store)["sourceReadState"]["complete"]
    target.write_bytes(raw + b"// external edit\n")
    assert not current(store)["sourceReadState"]["complete"]
    fresh = source_snapshot(snap["workspacePath"], snap["organizationId"])
    deliver(store, fresh, target.read_text(encoding="utf-8"), mode="complete")
    assert current(store)["sourceReadState"]["complete"]


def test_runtime_owned_read_counts_but_write_or_agent_assertion_does_not(setup):
    store, snap, raw = setup
    observation = {"schema": "replaylab-agent-working-file-observation/v1", "scope": "owned-workspace",
        "action": "write", "workingCopy": True, "replayAuthority": False,
        "sha256": snap["sourceSha256"], "byteSize": len(raw),
        "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True}}
    store.record_shader_source_read(observation)
    assert not current(store)["sourceReadState"]["complete"]
    store.record_shader_source_read({**observation, "action": "read"})
    assert current(store)["sourceReadState"]["complete"]


def test_old_store_delayed_native_read_cannot_complete_new_request(setup):
    old, snap, raw = setup
    fresh = TaskReportStore(old.task_id, old.event_root)
    fresh.begin_task_review("f" * 64)
    fresh.record_result("begin_shader_workspace_organization", {**snap, "proposalSourceSha256": snap["sourceSha256"]})
    with pytest.raises(ValueError, match="different execution"):
        old.record_shader_source_read({"schema": "replaylab-agent-working-file-observation/v1",
            "scope": "owned-workspace", "action": "read", "workingCopy": True, "replayAuthority": False,
            "sha256": snap["sourceSha256"], "byteSize": len(raw),
            "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True}})
    assert not current(fresh)["sourceReadState"]["complete"]


def test_new_organization_with_identical_bytes_does_not_inherit_read(setup):
    import shutil
    store, snap, raw = setup
    deliver(store, snap, raw.decode(), mode="complete")
    folder = Path(snap["sourcePath"]).parent
    second = folder.with_name("b" * 32)
    shutil.copytree(folder, second)
    session = json.loads((second / "session.json").read_text())
    session["organizationId"] = "b" * 32
    (second / "session.json").write_text(json.dumps(session))
    store.record_result("begin_shader_workspace_organization", {**snap, "organizationId": "b" * 32,
        "proposalSourceSha256": snap["sourceSha256"]})
    assert not current(store)["sourceReadState"]["complete"]


def test_late_oversized_read_result_is_not_counted_after_begin(setup):
    store, snap, raw = setup
    text = raw.decode() * 1000
    Path(snap["sourcePath"]).write_text(text, encoding="utf-8")
    # Simulate a large result produced before the gate existed and observed
    # after begin: the connector would spill this, so it cannot count as read.
    fresh = source_snapshot(snap["workspacePath"], snap["organizationId"])
    store.record_result("begin_shader_workspace_organization", {**fresh, "proposalSourceSha256": fresh["sourceSha256"]})
    deliver(store, fresh, text, mode="complete")
    assert not current(store)["sourceReadState"]["complete"]


def test_final_requires_both_new_begin_and_new_complete_review(setup):
    store, snap, raw = setup
    deliver(store, snap, raw.decode(), mode="complete")
    with pytest.raises(ValueError, match="new complete source assessment"):
        require_current(store, snap["workspacePath"], snap["organizationId"], snap["sourceSha256"], require_begin=True)
    from test_shader_scope_review import retained_dispositions
    from src.shader_organization_review import CHECKS, make_review
    result = {"id": "entry", "status": "satisfied", "source_functions": ["MainPS"],
        "evidence": "Constant output needs no further helpers.", "branches": [{"path": "straight",
        "status": "satisfied", "evidence": "Returns one constant."}],
        "retained_intermediates": "None", "remaining_work": []}
    scope = {"baseline_source_sha256": snap["baselineSourceSha256"],
        "plan": [{"id": "entry", "requirement": "Review the current whole program", "functions": ["MainPS"]}],
        "results": [result], "observation_dispositions": retained_dispositions(snap, [result])}
    from test_shader_semantic_overview_review import prepared_fixture
    review = make_review(snap, snap["sourceSha256"], "complete", {key: "Constant output inspected" for key in CHECKS},
                         [], [], "", "The constant result already has one clear responsibility", scope, require_scope=True,
                         prepared_overview=prepared_fixture(snap, scope), require_overview=True)
    store.record_result("record_shader_organization_review", review)
    require_current(store, snap["workspacePath"], snap["organizationId"], snap["sourceSha256"], require_begin=True)
    store.begin_task_review("f" * 64)
    with pytest.raises(ValueError, match="Begin this organization"):
        require_current(store, snap["workspacePath"], snap["organizationId"], snap["sourceSha256"], require_begin=True)


def test_gated_physical_action_cannot_use_another_organization_review(setup):
    store, snap, raw = setup
    deliver(store, snap, raw.decode(), mode="complete")
    with pytest.raises(ValueError, match="Begin this organization"):
        require_current(store, snap["workspacePath"], "b" * 32, snap["sourceSha256"], require_review=True)


def test_native_tracker_only_accepts_runtime_observed_reads(setup):
    from replaylab_agent.task_review import TaskActivityTracker
    store, snap, raw = setup
    tracker = TaskActivityTracker(store)
    event = {"activityKind": "toolCall", "status": "completed", "activityTarget": "fs/read_text_file",
        "activityId": "read1", "verification": "agentReported", "workingFileObservation": {
            "schema": "replaylab-agent-working-file-observation/v1", "scope": "owned-workspace",
            "action": "read", "workingCopy": True, "replayAuthority": False,
            "sha256": snap["sourceSha256"], "byteSize": len(raw),
            "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True}}}
    tracker.native_event(event)
    assert not current(store)["sourceReadState"]["complete"]
    tracker.native_event({**event, "activityId": "read2", "verification": "localFileObserved"})
    assert current(store)["sourceReadState"]["complete"]


from test_shader_draft_text_edits import draft, write


def test_first_proposal_write_is_rejected_until_current_source_is_delivered(draft, tmp_path, monkeypatch):
    server, fixture, opened, proposal, raw = draft
    store = TaskReportStore("task-write", tmp_path / "read-log")
    store.begin_task_review("e" * 64)
    store.record_result("begin_shader_workspace_organization", opened)
    monkeypatch.setattr(server, "current_task_report_store", lambda: store)
    result = write(draft, [{"old_text": "source.x", "new_text": "source.z"}])
    assert "full source read" in result["error"] and proposal.read_bytes() == raw
    snapshot = source_snapshot(str(fixture.root), opened["organizationId"])
    deliver(store, snapshot, raw.decode(), mode="complete")
    result = write(draft, [{"old_text": "source.x", "new_text": "source.z"}])
    assert "error" not in result and b"source.z" in proposal.read_bytes()
    store.record_result("write_shader_workspace_organization_file", result, arguments={"kind": "proposal"})
    assert current(store)["sourceReadState"]["complete"]


def test_safe_unicode_pages_reach_actual_end_without_api_output_spill():
    from replaylab_agent.tool_output import guard_tool_output_text
    text = ('// 中文测试😀 \\\"\nfloat v = 1.0;\n' * 2500)
    offset, pieces = 0, []
    while offset < len(text):
        payload = bounded_read_payload({"schema": "renderdoc-ai-shader-workspace-file/v1",
            "offset": offset, "mode": "complete" if offset == 0 else "range", "text": text[offset:]}, text)
        encoded = json.dumps(payload, ensure_ascii=True)
        assert len(encoded.encode()) <= 10 * 1024
        actual = json.loads(guard_tool_output_text("read_shader_workspace_file", encoded,
                                                 allow_complete_shader_inline=False))
        assert actual["schema"] != "replaylab-tool-output-pointer/v1"
        assert actual["coverage"]["byteOffset"] == len(text[:offset].encode())
        pieces.append(actual["text"])
        offset += actual["chars"]
        assert actual["nextOffset"] == (offset if offset < len(text) else None)
    assert "".join(pieces) == text
