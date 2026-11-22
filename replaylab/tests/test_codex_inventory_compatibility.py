from __future__ import annotations

import pytest

from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector, _AppServerRequestError
from replaylab_agent.errors import ConnectorError, PolicyError


def legacy_server():
    return {"name": "Bread", "tools": {"inspect_event": {}},
            "serverInfo": {"name": "Bread", "version": "1"}}


@pytest.mark.parametrize("status", [None, "starting", "failed", "disabled", "unknown"])
def test_legacy_readiness_never_overrides_explicit_status(status):
    entry = {**legacy_server(), "runtimeStatus": status}
    assert CodexAppServerConnector._mcp_runtime_status(entry) == status


@pytest.mark.parametrize("change", [{}, {"tools": {}}, {"serverInfo": None}])
def test_legacy_readiness_requires_initialized_metadata_and_tools(change):
    entry = {**legacy_server(), **change}
    expected = "connected" if not change else None
    assert CodexAppServerConnector._mcp_runtime_status(entry) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("callable_app", [False, True])
@pytest.mark.parametrize("inherit", [False, True])
async def test_legacy_inventory_checks_every_app_page(tmp_path, callable_app, inherit):
    connector = CodexAppServerConnector(["unused"], cwd=tmp_path, expected_mcp_servers={"Bread"},
                                       inherit_desktop_capabilities=inherit)
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, dict(params)))
        if method == "mcpServerStatus/list":
            return {"data": [legacy_server()], "nextCursor": None}
        if method == "app/installed":
            raise _AppServerRequestError(method, -32600, "unknown variant `app/installed`")
        assert method == "app/list"
        if "cursor" not in params:
            return {"data": [{"id": "disabled", "isAccessible": True, "isEnabled": False}],
                    "nextCursor": "second"}
        return {"data": [{"id": "legacy-app", "isAccessible": callable_app}], "nextCursor": None}

    connector._transport.request = request
    if callable_app and not inherit:
        with pytest.raises(PolicyError, match="legacy-app"):
            await connector._verify_tool_isolation("thread")
    else:
        await connector._verify_tool_isolation("thread")
        if inherit:
            assert connector.native_capabilities["callableApps"] == (["legacy-app"] if callable_app else [])
    assert calls[-1] == ("app/list", {"threadId": "thread", "forceRefresh": False, "cursor": "second"})


@pytest.mark.asyncio
async def test_app_query_errors_do_not_trigger_legacy_fallback(tmp_path):
    connector = CodexAppServerConnector(["unused"], cwd=tmp_path)

    async def request(method, params, **kwargs):
        assert method == "app/installed"
        raise _AppServerRequestError(method, -32603, "backend unavailable")

    connector._transport.request = request
    with pytest.raises(ConnectorError, match="backend unavailable"):
        await connector._callable_apps("thread")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["entry", "cursor"])
async def test_legacy_app_inventory_rejects_malformed_pages(tmp_path, invalid):
    connector = CodexAppServerConnector(["unused"], cwd=tmp_path)

    async def request(method, params, **kwargs):
        if method == "app/installed":
            raise _AppServerRequestError(method, -32601, "Method not found")
        if invalid == "entry":
            return {"data": [{"id": "app", "isAccessible": "true"}]}
        return {"data": [], "nextCursor": "repeated"}

    connector._transport.request = request
    with pytest.raises(ConnectorError, match="invalid " + invalid):
        await connector._callable_apps("thread")


def test_owned_permission_profile_is_pinned_for_process_config_reloads(tmp_path):
    import tomllib

    connector = CodexAppServerConnector(["codex", "app-server"], cwd=tmp_path, workspace_write=True)
    command = connector.command
    config = tomllib.loads("\n".join(command[i + 1] for i, value in enumerate(command) if value == "-c"))
    profile = config["default_permissions"]
    assert config["permissions"][profile] == {
        "filesystem": {":root": "read", ":workspace_roots": "write"}, "network": {"enabled": False}}
    assert profile == connector._native_permission_profile()
    inherited = CodexAppServerConnector(["codex", "app-server"], cwd=tmp_path,
                                       workspace_write=True, inherit_desktop_capabilities=True)
    assert inherited.command == ("codex", "app-server")
