"""Public native identities and actual owned fs observations, without content."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from replaylab_agent.acp_session import ACPMainAgentSession, _main_agent_client, _observe_working_file
from replaylab_agent.connectors.acp import (
    _ACPClientState, _handle_public_timeline_update, _load_acp_sdk,
    _replaylab_tool_for_permission,
)
from replaylab_agent.errors import PolicyError
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.owned_workspace import OwnedWorkspace


def reporter():
    events = []
    return SimpleNamespace(writer=SimpleNamespace(emit=lambda kind, **values: events.append({"kind": kind, **values})),
                           backend="native", profile_id="offline"), events


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kind", [("Read", "read"), ("Write", "edit"), ("Exec", "execute"), ("Task", "other")])
async def test_cursor_technical_name_and_kind_remain_provider_claim_not_host_grant(name, kind):
    target, events = reporter()
    rows = []
    state = _ACPClientState(on_delta=None, provider_id="cursor-acp", allowed_tools=frozenset({"inspect_shader"}),
                            on_timeline=rows.append)
    update = {"toolCallId": "native-1", "kind": "other", "status": "running",
              "rawInput": {"_toolName": name, "command": "never publish command or token=private-native-value",
                           "path": "D:/private/location", "content": "PRIVATE CONTENT"}}
    await _handle_public_timeline_update(state, update, "tool_call")
    await _handle_public_timeline_update(state, {"toolCallId": "native-1", "status": "completed"}, "tool_call_update")
    assert [row["activityTarget"] for row in rows] == [name, name]
    assert all(row["activityKind"] == "toolCall" and row["toolKind"] == kind for row in rows)
    assert not state.tool_targets and state.native_tool_targets == {"native-1": name}
    assert _replaylab_tool_for_permission(update, state.allowed_tools) is None
    for row in rows:
        NativeAgentEngine._project_provider_timeline(target, row, source="acp")
    assert all(row["data"]["verification"] == "agentReported" and row["data"]["hostVerified"] is False for row in events)
    assert events[-1]["tool"] == name and events[-1]["data"]["toolKind"] == kind
    encoded = json.dumps(events)
    assert "private-native-value" not in encoded and "PRIVATE CONTENT" not in encoded and "D:/private" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Exec ../outside.py", "token-private-value", "C:/private/file.py"])
async def test_cursor_nonidentifier_values_are_not_public_tool_names(name):
    rows = []
    state = _ACPClientState(on_delta=None, provider_id="cursor-acp", on_timeline=rows.append)
    await _handle_public_timeline_update(state, {"toolCallId": "native-1", "rawInput": {"_toolName": name}}, "tool_call")
    assert not rows[0].get("activityTarget")
    assert name not in json.dumps(rows)


@pytest.mark.parametrize("kind", ["read", "edit", "execute"])
def test_unnamed_native_activity_preserves_safe_operation_category(kind):
    target, events = reporter()
    NativeAgentEngine._project_provider_timeline(target,
        {"activityId": "acp-tool-1", "activityKind": "toolCall", "toolKind": kind, "status": "running"}, source="acp")
    assert events[0]["summary"] == kind
    assert events[0]["data"]["activityTitleArgs"] == [kind]
    assert events[0]["data"]["toolKind"] == kind and events[0]["data"]["hostVerified"] is False


@pytest.mark.asyncio
async def test_successful_fs_callbacks_emit_actual_utf8_byte_facts_only(tmp_path):
    target, events = reporter()
    state = _ACPClientState(on_delta=None)
    state.session_id = "session"
    state.on_timeline = lambda row: NativeAgentEngine._project_provider_timeline(target, row, source="acp")
    client = _main_agent_client(_load_acp_sdk(), state, frozenset(), native_workspace=OwnedWorkspace(tmp_path, writable=True))
    text = "float3 surfaceColor; // 中文\n" * 20000
    path = tmp_path / "draft.hlsl"
    await client.write_text_file("session", str(path), text)
    read = await client.read_text_file("session", str(path), line=2, limit=1)
    assert read["content"] == text.splitlines(keepends=True)[1]
    raw = path.read_bytes()
    assert len(events) == 2
    assert [row["tool"] for row in events] == ["fs/write_text_file", "fs/read_text_file"]
    observations = [row["data"]["workingFileObservation"] for row in events]
    assert [row["action"] for row in observations] == ["write", "read"]
    assert all(row["basename"] == "draft.hlsl" and row["byteSize"] == len(raw)
               and row["sha256"] == hashlib.sha256(raw).hexdigest()
               and row["workingCopy"] is True and row["replayAuthority"] is False for row in observations)
    assert observations[0]["coverage"] == {"byteOffset": 0, "returnedBytes": len(raw), "complete": True}
    line_bytes = len(text.splitlines(keepends=True)[0].encode("utf-8"))
    assert observations[1]["coverage"] == {"byteOffset": line_bytes, "returnedBytes": line_bytes, "complete": False}
    assert len({row["data"]["activityId"] for row in events}) == 2
    assert all(row["data"]["activityId"].startswith("acp-file-") for row in events)
    assert not state.tool_activity_ids  # File observations cannot consume vendor activity capacity.
    assert all(row["data"]["verification"] == "localFileObserved" and row["data"]["statusSource"] == "local"
               and row["data"]["hostVerified"] is False for row in events)
    encoded = json.dumps(events)
    assert str(tmp_path) not in encoded and "surfaceColor" not in encoded and '"content"' not in encoded
    assert not state.abort_event.is_set()


@pytest.mark.asyncio
async def test_failed_fs_operations_never_emit_success_observation(tmp_path, monkeypatch):
    events = []
    state = _ACPClientState(on_delta=None, on_timeline=events.append, session_id="session")
    scope = OwnedWorkspace(tmp_path, writable=True)
    client = _main_agent_client(_load_acp_sdk(), state, frozenset(), native_workspace=scope)
    with pytest.raises(PolicyError):
        await client.write_text_file("session", "../outside.hlsl", "outside")
    with pytest.raises(FileNotFoundError):
        await client.read_text_file("session", "missing.hlsl")
    path = tmp_path / "draft.hlsl"
    path.write_text("before")
    with pytest.raises(PolicyError):
        await client.read_text_file("session", str(path), line=0)
    def fail_replace(*args):
        raise PermissionError("synthetic save denied")
    monkeypatch.setattr("replaylab_agent.owned_workspace.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        await client.write_text_file("session", str(path), "after")
    assert path.read_text() == "before" and not events and not state.abort_event.is_set()


@pytest.mark.asyncio
async def test_observation_errors_do_not_change_successful_file_execution(tmp_path, monkeypatch, caplog):
    def fail_observer(*args):
        raise RuntimeError("token=PRIVATE CALLBACK DATA")
    state = _ACPClientState(on_delta=None, on_timeline=fail_observer, session_id="session")
    client = _main_agent_client(_load_acp_sdk(), state, frozenset(), native_workspace=OwnedWorkspace(tmp_path, writable=True))
    path = tmp_path / "draft.hlsl"
    await client.write_text_file("session", str(path), "saved")
    assert (await client.read_text_file("session", str(path)))["content"] == "saved"
    assert "PRIVATE CALLBACK DATA" not in caplog.text
    monkeypatch.setattr(OwnedWorkspace, "_observation", staticmethod(fail_observer))
    await client.write_text_file("session", str(path), "saved again")
    assert (await client.read_text_file("session", str(path)))["content"] == "saved again"
    assert state.callback_error is None and not state.abort_event.is_set()


@pytest.mark.asyncio
async def test_vendor_notifications_cannot_supply_local_fs_facts():
    rows = []
    state = _ACPClientState(on_delta=None, provider_id="cursor-acp", on_timeline=rows.append)
    await _handle_public_timeline_update(state,
        {"toolCallId": "native-1", "rawInput": {"_toolName": "Write"}, "status": "completed",
         "workingFileObservation": {"path": "PRIVATE PATH"}, "verification": "localFileObserved", "hostVerified": True},
        "tool_call")
    assert "workingFileObservation" not in rows[0] and rows[0]["hostVerified"] is False


def test_malformed_observation_is_dropped_without_changing_public_activity():
    target, events = reporter()
    NativeAgentEngine._project_provider_timeline(target,
        {"activityId": "acp-tool-1", "activityKind": "toolCall", "toolKind": {"unsafe": "value"},
         "status": "completed", "verification": "localFileObserved",
         "workingFileObservation": {"schema": "replaylab-agent-working-file-observation/v1", "scope": "owned-workspace",
                                    "action": ["write"], "basename": "draft.hlsl", "byteSize": 5, "sha256": "a" * 64,
                                    "workingCopy": True, "replayAuthority": False}}, source="acp")
    assert events[0]["data"]["verification"] == "agentReported"
    assert "workingFileObservation" not in events[0]["data"]


def test_verified_codex_workspace_capability_and_fallback_reason_are_projected(tmp_path):
    target, events = reporter()
    NativeAgentEngine._project_provider_timeline(target, {
        "activityId": "codex-native-workspace-capability", "activityKind": "nativeCapability",
        "activityTarget": "Codex read-only", "status": "completed", "lifecycle": "completed",
        "permissionProfile": ":read-only", "workspaceWrite": False, "networkAccess": False,
        "runtimeWorkspaceRoots": [str(tmp_path)],
        "activityDetail": "Native write profile was denied; Bread tools remain available.",
    }, source="codex-app-server")
    data = events[0]["data"]
    assert data["nativeWorkspaceCapability"] == {
        "permissionProfile": ":read-only", "workspaceWrite": False, "networkAccess": False,
        "runtimeWorkspaceRoots": [str(tmp_path)],
    }
    assert "denied" in data["activityDetailArgs"][0]
    assert data["verification"] == "providerHandshake" and data["hostVerified"] is False


@pytest.mark.asyncio
async def test_local_fs_observation_does_not_change_prompt_segments_or_provider_call_count(tmp_path):
    state = _ACPClientState(on_delta=None, session_id="session")
    session = ACPMainAgentSession(command=("offline",), cwd=tmp_path, env=None, allowed_tools=frozenset())
    session._stack = SimpleNamespace()
    session._sdk = _load_acp_sdk()
    session._state, session._session_id = state, "session"
    rows, boundaries = [], []
    async def prompt(**kwargs):
        await state.on_delta("before")
        await _observe_working_file(state, OwnedWorkspace._observation(tmp_path / "draft.hlsl", b"working", "read"))
        await state.on_delta("after")
        return SimpleNamespace(stop_reason="end_turn")
    session._connection = SimpleNamespace(prompt=prompt)
    result = await session.prompt("offline prompt", on_timeline=rows.append, tool_boundary=lambda: boundaries.append(True))
    assert result.segments == ["beforeafter"] and result.tool_calls == 0
    assert len(rows) == 1 and rows[0]["verification"] == "localFileObserved" and not boundaries
