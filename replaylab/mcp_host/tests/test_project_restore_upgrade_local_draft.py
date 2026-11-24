"""Known restore upgrades preserve the native candidate and separate local draft."""

import copy
import hashlib
import json
from unittest import mock

import pytest

from src import project_restore
from tests import test_project_restore as support


@pytest.fixture
def restore_case():
    case = support.ProjectRestoreTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.mark.parametrize("upgrade", ["package-ui", "physical-policy", "legacy-v1-snapshot"])
def test_upgrade_reuses_saved_candidate_and_preserves_independent_local_draft(restore_case, upgrade):
    case = restore_case
    cold, host, replay, unused = case._run(
        index_present=False, auto_prepare=True, source_preparation_only=True,
        host_mutator=case._physical_ticketed_restore_host)
    assert cold["verifiedRdcCache"]["written"], cold
    draft = case.project / "workspaces" / "shaders" / "本地草稿-upgrade"
    draft.mkdir()
    local_source = draft / "shader.hlsl"
    original_text = "// 本地草稿必须在版本升级后保留\nfloat value = 0.1;\n".encode("utf-8")
    local_source.write_bytes(original_text)
    draft_status = copy.deepcopy(host.target_status)
    draft_status.update(workspaceId="5" * 32, workspacePath=str(draft))
    draft_status["source"].update(lane="executable-editable", format="hlsl", present=True,
        currentSha256=hashlib.sha256(original_text).hexdigest(), modifiedSinceDecompile=True)
    draft_status["latestCompiled"] = None
    host.workspace_service.extra_statuses.append(draft_status)
    snapshot_path = project_restore._verified_rdc_restore_path(case.replay_path, case.capture_path)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["shaders"][0]["preservedLocalSource"] = {
        "path": draft.relative_to(case.project).as_posix(), "workspaceId": draft_status["workspaceId"]}
    if upgrade == "legacy-v1-snapshot":
        snapshot["schemaVersion"] = 1
    snapshot.pop("sealSha256")
    snapshot["sealSha256"] = project_restore._canonical_sha256(snapshot)
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    original_candidate = host.target_status["latestCompiled"]["rawSha256"]
    previous_compile_calls = host.compile_calls
    if upgrade == "package-ui":
        host.package_provenance_sha256 = "7" * 64
    elif upgrade == "physical-policy":
        host.physical_validation_binding["abiRulesVersion"] += 1
        host.restore_validation_policy_sha256 = project_restore._canonical_sha256(host.physical_validation_binding)
    with mock.patch.object(host, "_show_saved_shader_workspace_source",
                           return_value=json.dumps({"shown": True}), create=True) as show:
        restored = case._reopen_ticketed_restore(host, replay)
    assert restored["completed"] and restored["shaders"]["applied"] == 1, restored
    assert restored["shaders"]["failed"] == restored["shaders"]["skipped"] == 0, restored
    assert restored["editing"]["deferred"] == 1 and restored["editing"]["enabled"] == 0, restored
    item = restored["shaders"]["items"][0]
    assert item["preservedLocalSource"]["workspaceId"] == draft_status["workspaceId"]
    assert item["preservedLocalSource"]["sourceSha256"] == hashlib.sha256(original_text).hexdigest()
    assert item["preservedLocalSource"]["shown"] is True
    assert show.call_args.args == (str(draft),)
    assert show.call_args.kwargs == {"activate_window": False}
    assert local_source.read_bytes() == original_text
    assert host.target_status["latestCompiled"]["rawSha256"] == original_candidate
    assert host.compile_calls == previous_compile_calls
    if upgrade == "package-ui":
        assert restored["verifiedRdcCache"]["hit"]
    else:
        assert not restored["verifiedRdcCache"]["hit"]
        assert restored["verifiedRdcCache"]["written"], restored
        assert restored["cache"]["reason"].startswith("verified-rdc-cache-miss:"), restored

    # Any one-time migration must leave a usable fast cache and keep identifying
    # the user's independent draft on the next project open.
    counts = host.compile_calls, host.workspace_prepare_calls, host.apply_calls
    previous_verified_applies = host.verified_apply_calls
    with mock.patch.object(host, "_show_saved_shader_workspace_source",
                           return_value=json.dumps({"shown": True}), create=True) as show:
        warm = case._reopen_ticketed_restore(host, replay)
    assert warm["completed"] and warm["shaders"]["applied"] == 1, warm
    assert warm["shaders"]["failed"] == warm["shaders"]["skipped"] == 0, warm
    assert warm["verifiedRdcCache"]["hit"], warm
    assert warm["editing"]["deferred"] == 1 and warm["editing"]["enabled"] == 0, warm
    assert warm["shaders"]["items"][0]["preservedLocalSource"] == item["preservedLocalSource"]
    assert show.call_args.args == (str(draft),)
    assert show.call_args.kwargs == {"activate_window": False}
    assert local_source.read_bytes() == original_text
    assert host.target_status["latestCompiled"]["rawSha256"] == original_candidate
    assert (host.compile_calls, host.workspace_prepare_calls, host.apply_calls) == counts
    assert host.verified_apply_calls == previous_verified_applies + 1
