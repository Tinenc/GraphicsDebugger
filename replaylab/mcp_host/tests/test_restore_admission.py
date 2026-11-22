import json
from unittest.mock import Mock

import pytest

from src import server, internal_tool_service
from src.ipc_client import bind_direct_tool_call
from src.shader_restore_admission import (
    artifact_status, local_dispatcher, restore_admission, restored_snapshot,
)
from tests.test_shader_editor_status_snapshot import workspace


def test_restore_validation_is_reused_and_changed_item_is_independent(workspace):
    root, service = workspace
    validate = Mock(wraps=service.status)
    with restore_admission():
        first = artifact_status(service, root, validate)
        assert artifact_status(service, root, validate) == first
        assert validate.call_count == 1
        snapshot = restored_snapshot(service, root)
        assert snapshot is not None
        # An admission snapshot can seed an execution owner but does not claim
        # compiler validation, and cannot survive a changed source.
        source = root / "source/shader.hlsl"
        source.write_text(source.read_text() + "// edit\n")
        assert restored_snapshot(service, root) is None
        artifact_status(service, root, validate)
        assert validate.call_count == 2
    assert restored_snapshot(service, root) is None


def test_restore_dispatcher_is_private_and_reuses_closed_payload(monkeypatch):
    capture = {"captureSessionId": "b" * 32, "path": "C:/test/frame.rdc"}
    monkeypatch.setattr(server, "_active_live_context", lambda **kw: {"capture": capture})
    http = Mock(return_value={"transport": "http"})
    monkeypatch.setattr("src.shader_live_edit_client.request", http)
    def dispatch(payload):
        value = internal_tool_service.parse_shader_live_edit_operation(payload)
        return 200, {**value, "result": {"transport": "owner", "operation": value["operation"]}}
    with bind_direct_tool_call("123-" + "a" * 32, "b" * 32):
        assert server._shader_live_edit_remote("list", {})["transport"] == "http"
        with restore_admission(dispatch):
            assert server._shader_live_edit_remote("list", {})["transport"] == "owner"
            assert http.call_count == 1
        assert local_dispatcher() is None


def test_failed_owner_start_is_not_retried_through_http(monkeypatch):
    monkeypatch.setattr(server, "_active_live_context", lambda **kw: {
        "capture": {"captureSessionId": "b" * 32, "path": "C:/test/frame.rdc"}})
    http = Mock(side_effect=AssertionError("must not redispatch"))
    monkeypatch.setattr("src.shader_live_edit_client.request", http)
    with bind_direct_tool_call("123-" + "a" * 32, "b" * 32), restore_admission(
            lambda payload: (409, {"error": "changed capture"})):
        with pytest.raises(RuntimeError, match="changed capture"):
            server._shader_live_edit_remote("list", {})
    http.assert_not_called()


def test_file_preparation_threads_share_the_restore_validation_pool(workspace):
    from src.restore_file_preparation import artifact_statuses
    from src.shader_restore_admission import statistics
    root, service = workspace
    class Reader:
        def status(self, path):
            return artifact_status(service, path, service.status)
    with restore_admission():
        results = artifact_statuses(Reader, [root] * 8)
        assert len(results) == 8
        assert restored_snapshot(service, root) is not None
        assert statistics()["retainedSnapshots"] == 1
        assert statistics()["snapshotReuses"] >= 6


def test_partial_restore_only_recovers_missing_resource_even_with_shared_bytes(monkeypatch, tmp_path):
    from src import project_restore as restore
    identity = {"resourceId": 1, "stage": "fragment", "entryPoint": "main", "rawSha256": "a" * 64}
    matches = [{"shader": dict(identity, resourceId=i)} for i in range(1, 20)]
    inventory = Mock(return_value={"shaders": [item["shader"] for item in matches]})
    monkeypatch.setattr(restore, "_inventory_shader_resources", inventory)
    monkeypatch.setattr(restore, "_prepare_inventory_response", lambda *a: {"reusePlan": {"matches": list(matches)}})
    recovered_ids = []
    def recover(*args, **kw):
        selected = args[6]["reusePlan"]["matches"]
        recovered_ids.extend(item["shader"]["resourceId"] for item in selected)
        return [{"identity": item["shader"]} for item in selected], [], {}
    monkeypatch.setattr(restore, "_restore_from_reuse_plan", recover)
    result = {"shaders": {}, "materials": {}, "realizedReceipts": {}, "verifiedRdcCache": {}}
    records = restore._recover_verified_restore_exceptions(None, None, {}, tmp_path, tmp_path/"frame.rdc", "b"*32,
        {"capture": {"api": "vulkan"}}, {"matches": matches}, [{"identity": identity}], result, None, None, [], [])
    assert recovered_ids == list(range(2, 20))
    assert len(records) == 18
    # Eighteen missing ResourceIds share bytes. They form one lookup, while
    # the already successful original ResourceId never gets processed twice.
    assert inventory.call_count == 1
    assert inventory.call_args.kwargs["target_raw_sha256s"] == ["a"*64]
