"""MCP transport ownership survives Host calls in different asyncio tasks."""
import asyncio
import socket
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest

from src.mcp_tool_client import McpToolClient, _http_client_without_redirects, validate_http_endpoint, McpToolError, connection_fingerprint


def test_session_is_opened_and_closed_by_the_same_background_task(monkeypatch):
    owners = []
    client = McpToolClient()

    @asynccontextmanager
    async def transport():
        owners.append(asyncio.current_task())
        yield
        owners.append(asyncio.current_task())

    class Session:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=False, structuredContent={"ok": True}, content=[])

    async def open_connection(tool):
        stack = AsyncExitStack()
        await stack.enter_async_context(transport())
        return stack, Session()

    monkeypatch.setattr(client, "_open", open_connection)

    async def run():
        tool = {"id": "unity", "connection": {"kind": "mcp-http", "url": "http://localhost/mcp"}}
        assert (await asyncio.create_task(client.call(tool, "first", {})))["ok"]
        assert (await asyncio.create_task(client.call(tool, "second", {})))["ok"]
        await asyncio.create_task(client.close("unity"))
        assert len(owners) == 2 and owners[0] is owners[1]
        assert not client._sessions and not client._owners
    asyncio.run(run())


def test_connection_identity_does_not_truncate_long_configurations_or_store_secrets():
    before = {"args": ["x" * 5000], "url": "https://one.example.test/mcp", "headers": {"Authorization": "private-secret"}}
    after = {**before, "url": "https://two.example.test/mcp"}
    assert connection_fingerprint(before) != connection_fingerprint(after)
    assert "private-secret" not in connection_fingerprint(before)


def test_http_redirects_are_disabled_to_keep_endpoint_identity_and_headers_bound():
    async def run():
        async with _http_client_without_redirects(headers={"Authorization": "Bearer test"}) as client:
            assert client.follow_redirects is False
    asyncio.run(run())


def test_user_cancellation_closes_the_owned_transport_and_propagates(monkeypatch):
    async def run():
        client = McpToolClient()
        entered, closed = asyncio.Event(), asyncio.Event()

        class Session:
            async def call_tool(self, name, arguments):
                entered.set()
                await asyncio.Event().wait()

        async def open_connection(tool):
            stack = AsyncExitStack()
            stack.callback(closed.set)
            return stack, Session()

        monkeypatch.setattr(client, "_open", open_connection)
        task = asyncio.create_task(client.call({"id": "unity", "connection": {}}, "pending", {}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not client._owners
    asyncio.run(run())


@pytest.mark.parametrize("url", ["http://192.168.1.2/mcp", "http://localhost.example.test/mcp", "https://user:secret@example.test/mcp", "ws://localhost/mcp"])
def test_host_mcp_rejects_unsafe_endpoints(url):
    with pytest.raises(McpToolError) as error:
        validate_http_endpoint(url)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", ["http://localhost:8080/mcp", "http://127.0.0.1:8080/mcp", "http://[::1]:8080/mcp", "https://example.test/mcp"])
def test_host_mcp_accepts_loopback_and_https(url):
    assert validate_http_endpoint(url) == url


def test_real_local_http_mcp_reuses_session_across_host_tasks(tmp_path, monkeypatch):
    from src import mcp_tool_client, unity_integration

    project = tmp_path / "Unity project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.55f1\n")
    (project / "Packages").mkdir()
    (project / "Packages/manifest.json").write_text('{"dependencies": {}}')
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    script = tmp_path / "unity_mcp_fixture.py"
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        f"mcp = FastMCP('Unity fixture', host='127.0.0.1', port={port}, stateless_http=True)\n"
        f"@mcp.tool()\ndef identity() -> dict:\n    return {{'projectPath': {str(project)!r}, 'unityVersion': '6000.0.55f1'}}\n"
        "mcp.run(transport='streamable-http')\n", encoding="utf-8")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    with (tmp_path / "server.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, str(script)], stdout=log, stderr=log, **flags)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        pytest.fail("Local MCP test server did not start")
                    time.sleep(0.05)

            async def run():
                client = McpToolClient()
                tool = {"id": "local-unity", "connection": {"kind": "mcp-http", "url": f"http://127.0.0.1:{port}/mcp"}}
                try:
                    listed = await asyncio.create_task(client.list_tools(tool))
                    assert [item["name"] for item in listed] == ["identity"]
                    owner = client._owners[tool["id"]]
                    assert (await asyncio.create_task(client.call(tool, "identity", {})))["projectPath"] == str(project)
                    assert (await asyncio.create_task(client.call(tool, "identity", {})))["unityVersion"] == "6000.0.55f1"
                    assert client._owners[tool["id"]] is owner
                    monkeypatch.setattr(mcp_tool_client, "shared_client", lambda: client)
                    service = unity_integration.UnityIntegrationService(tmp_path / "cache")
                    connected = await service.connect_existing_mcp(str(project), tool["connection"], "identity", required_tools=["identity"])
                    assert connected["identityVerified"] is True and connected["packagesModified"] is False
                    assert (await asyncio.create_task(service.call_tool(str(project), "identity")))["projectPath"] == str(project)
                    assert not (project / "Library").exists()
                finally:
                    await asyncio.create_task(client.aclose())
            asyncio.run(run())
        finally:
            process.terminate()
            process.wait(timeout=10)
