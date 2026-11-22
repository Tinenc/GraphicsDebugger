from pathlib import Path
import json
import os
import pytest

from replaylab_agent.acp_session import _main_agent_client, _owned_file_permission
from replaylab_agent.connectors.acp import _ACPClientState, _load_acp_sdk
from replaylab_agent.errors import PolicyError
from replaylab_agent.owned_workspace import OwnedWorkspace, AGENT_WORKSPACE_ROOT_ENV
from replaylab_agent.process_env import _cursor_permissions


@pytest.mark.asyncio
async def test_acp_native_file_callbacks_edit_only_owned_copy_and_keep_terminal_denied(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    original = tmp_path / "canonical.hlsl"
    original.write_text("original", encoding="utf-8")
    state = _ACPClientState(on_delta=None)
    state.session_id = "session"
    client = _main_agent_client(_load_acp_sdk(), state, frozenset(),
                               native_workspace=OwnedWorkspace(root, writable=True))
    text = "float3 surfaceColor; // 中文\n" * 20000
    await client.write_text_file("session", str(root / "draft.hlsl"), text)
    assert (await client.read_text_file("session", str(root / "draft.hlsl")))["content"] == text
    assert (await client.read_text_file("session", str(root / "draft.hlsl"), line=2, limit=1))["content"] == text.splitlines(keepends=True)[1]
    for value in (str(original), "../canonical.hlsl", "settings.json", "AGENTS.md.", "AGENTS.md ", ".cursor/cli.json", "safe.hlsl:stream"):
        with pytest.raises(PolicyError):
            await client.write_text_file("session", value, "wrong")
    with pytest.raises(PolicyError):
        await client.read_text_file("other-session", str(root / "draft.hlsl"))
    with pytest.raises(PolicyError):
        await client.create_terminal("session", "python", cwd=str(root))
    assert original.read_text() == "original"
    assert not state.abort_event.is_set()


@pytest.mark.asyncio
async def test_read_only_acp_native_file_scope_and_permission_paths(tmp_path):
    path = tmp_path / "draft.hlsl"
    path.write_text("source", encoding="utf-8")
    scope = OwnedWorkspace(tmp_path)
    state = _ACPClientState(on_delta=None); state.session_id = "session"
    client = _main_agent_client(_load_acp_sdk(), state, frozenset(), native_workspace=scope)
    assert (await client.read_text_file("session", str(path)))["content"] == "source"
    with pytest.raises(PolicyError):
        await client.write_text_file("session", str(path), "new")
    assert _owned_file_permission({"kind": "read", "rawInput": {"path": str(path)}}, scope)
    assert not _owned_file_permission({"kind": "edit", "rawInput": {"path": str(path)}}, scope)
    assert not _owned_file_permission({"kind": "execute", "rawInput": {"path": str(path)}}, OwnedWorkspace(tmp_path, writable=True))
    assert not _owned_file_permission({"kind": "read", "locations": [{"path": str(path)}, {"path": str(tmp_path.parent / "elsewhere")}]}, scope)


@pytest.mark.parametrize("line,limit,expected", [(None, None, "中文\r\nsecond😀\nlast"),
    (2, 1, "second😀\n"), (3, None, "last"), (99, 1, "")])
def test_owned_read_distinguishes_whole_file_hash_from_returned_bytes(tmp_path, line, limit, expected):
    import hashlib
    raw = "中文\r\nsecond😀\nlast".encode("utf-8")
    path = tmp_path / "proposal.hlsl"
    path.write_bytes(raw)
    observation = {}
    result = OwnedWorkspace(tmp_path).read(path.name, line=line, limit=limit, observation=observation)
    assert result == expected
    assert observation["byteSize"] == len(raw) and observation["sha256"] == hashlib.sha256(raw).hexdigest()
    start = 0 if line is None else len(b"".join(raw.splitlines(keepends=True)[:line - 1]))
    assert observation["coverage"] == {"byteOffset": start, "returnedBytes": len(expected.encode("utf-8")),
                                       "complete": line is None}


@pytest.mark.parametrize("name", ["AGENTS.md", "agents.md.", "AGENTS.md ", "settings.json.", "settings.json ",
                                 "config.toml.", ".claude/settings.json", "data/.hidden/result.hlsl", "one/../two.hlsl"])
def test_windows_config_aliases_and_dot_components_stay_protected(tmp_path, name):
    with pytest.raises(PolicyError):
        OwnedWorkspace(tmp_path, writable=True).path(name, write=True)


def test_hardlink_and_symlink_never_grant_other_file_authority(tmp_path):
    root = tmp_path / "own"; root.mkdir()
    outside = tmp_path / "credential.json"; outside.write_text("private synthetic marker")
    linked = root / "fake.hlsl"
    os.link(outside, linked)
    scope = OwnedWorkspace(root, writable=True)
    with pytest.raises(PolicyError): scope.read(str(linked))
    with pytest.raises(PolicyError): scope.write(str(linked), "edit")
    linked.unlink()
    try: linked.symlink_to(outside)
    except OSError: return
    with pytest.raises(PolicyError): scope.read(str(linked))
    with pytest.raises(PolicyError): scope.write(str(linked), "edit")


def test_cursor_native_file_rules_pin_root_and_exclude_launch_policy(tmp_path):
    rules = _cursor_permissions(tmp_path, writable=True)
    root = tmp_path.as_posix()
    assert rules["allow"] == ["Mcp(Bread:*)", "Read(%s/**)" % root, "Write(%s/**)" % root]
    assert "Shell(*)" in rules["deny"]
    assert "Write(%s/**/settings.json*)" % root in rules["deny"]
    assert "Write(**)" in _cursor_permissions(tmp_path)["deny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,writable", [("workspace-native", True), ("unverified-write-scope", False),
    ("workspace-profile-denied", False), ("ambient-tmp-write-scope", False),
    ("legacy-write-ignored", False), ("custom-profile-unsupported", False)])
async def test_codex_native_workspace_scope_is_verified_or_keeps_readonly(tmp_path, mode, writable):
    import sys
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector
    fixture = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"
    connector = CodexAppServerConnector([sys.executable, str(fixture), mode], cwd=tmp_path, workspace_write=True, timeout=5)
    turn_inputs = []
    timeline = []
    thread_inputs = []
    start_turn = connector._start_turn
    start_thread = connector._start_thread

    async def record_start_thread(params):
        thread_inputs.append(dict(params))
        return await start_thread(params)

    async def record_start_turn(params):
        turn_inputs.append(params["input"][0]["text"])
        return await start_turn(params)

    connector._start_turn = record_start_turn
    connector._start_thread = record_start_thread
    try:
        result = await connector.run("inspect staged source", on_timeline=timeline.append, keep_thread=True)
        assert result.text == "evidence answer"
        assert connector.workspace_write is writable
        assert bool(connector.workspace_capability_note) is (not writable)
        if writable:
            assert len(turn_inputs) == 1
            assert turn_inputs[0].startswith("inspect staged source\n\n## Effective native workspace capability\n")
            assert thread_inputs[0]["permissions"].startswith("bread-owned-")
            # A self-review continuation reuses the same verified named scope;
            # it must not replay the initial thread or reset to legacy sandbox.
            await connector.run("review the edited copy", on_timeline=timeline.append, keep_thread=True)
            assert len(thread_inputs) == 1
            assert len(turn_inputs) == 2
            assert turn_inputs[1].startswith("review the edited copy\n\n## Effective native workspace capability\n")
            assert "inspect staged source" not in turn_inputs[1]
            for text in turn_inputs:
                assert "agent_file_name" in text
                assert "source_file=agentFile.relativePath" in text
                assert "Native file permissions and Bread MCP effect grants are separate" in text
        else:
            assert connector.workspace_capability_note in turn_inputs[0]
            assert "list_bread_capabilities" in turn_inputs[0]
            assert "invoke_bread_tool" in turn_inputs[0]
            assert "Codex has verified native write access" not in turn_inputs[0]
            assert "source_file=agentFile.relativePath" not in turn_inputs[0]
        capability = [event for event in timeline if event.get("activityKind") == "nativeCapability"]
        assert len(capability) == 1
        assert capability[0]["workspaceWrite"] is writable
        assert capability[0]["runtimeWorkspaceRoots"] == [str(tmp_path)]
        assert capability[0]["networkAccess"] is False
        if writable:
            for text in turn_inputs:
                facts = json.loads(text.split("## Effective native workspace capability\n", 1)[1].splitlines()[0])
                assert facts["verification"] == "providerHandshake"
                for key in ("permissionProfile", "workspaceWrite", "runtimeWorkspaceRoots", "networkAccess"):
                    assert facts[key] == capability[0][key]
        if not writable:
            assert connector.workspace_capability_note in capability[0]["activityDetail"]
    finally:
        await connector.close()


def test_codex_native_write_cannot_add_other_roots(tmp_path):
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector
    from replaylab_agent.errors import ConfigurationError
    own, other = tmp_path / "own", tmp_path / "other"
    own.mkdir(); other.mkdir()
    with pytest.raises(ConfigurationError, match="exact owned cwd"):
        CodexAppServerConnector(cwd=own, readable_roots=[own, other], workspace_write=True)
