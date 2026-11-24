"""The Host as an MCP client, so a tool that speaks MCP needs no configuration.

``tools/list`` is a typed, self-describing answer: it says what the tool can do and
what each operation takes. That is the whole reason MCP is the preferred connection
here — nothing has to be guessed, and nothing has to be written down by hand.

Two things this module exists to get right:

* **The event loop is never blocked.** FastMCP 1.x runs a synchronous tool function
  inline on the loop, so a synchronous call that waits is a frozen Host — that is
  exactly how a 30-minute freeze happened before. Everything here is awaited, and the
  subprocess fallback for command-line tools is pushed to a worker thread.
* **Sessions are reused.** Identifying ten thousand assets must not start ten thousand
  processes. One session per tool is opened on first use and kept until the connection
  it was opened for changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import ipaddress
import os
from contextlib import AsyncExitStack
from typing import Any, Mapping
from urllib.parse import urlsplit

CONNECT_TIMEOUT_SECONDS = 30.0
CALL_TIMEOUT_SECONDS = 60.0
LIST_TIMEOUT_SECONDS = 30.0


class McpToolError(RuntimeError):
    """The tool could not be reached or refused the call."""

    def __init__(self, message: str, *, transport_failure: bool = False, request_sent: bool | None = None):
        super().__init__(message)
        self.transport_failure = transport_failure
        self.request_sent = request_sent


def _transport_exception(error: BaseException) -> bool:
    # JSON-RPC parse/method/parameter/internal errors are replies from a live
    # server. A connection-closed error (-32000), timeout or broken stream is
    # different: only that case needs a new transport.
    code = getattr(getattr(error, "error", None), "code", None)
    return code not in {-32700, -32600, -32601, -32602, -32603}


def validate_http_endpoint(url: str) -> str:
    """Allow local MCP bridges without accepting remote cleartext or URL credentials."""
    try:
        endpoint = urlsplit(url)
        host = endpoint.hostname or ""
        port = endpoint.port
        loopback = host.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or "\\" in url or any(char.isspace() for char in url)
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.fragment or port is not None and not 1 <= port <= 65535
                or endpoint.scheme not in {"https", "http"}
                or endpoint.scheme == "http" and (not loopback or endpoint.query)):
            raise ValueError("invalid endpoint")
    except ValueError:
        raise McpToolError("MCP requires HTTPS or query-free loopback HTTP, without URL credentials or fragments") from None
    return url


def _http_client_without_redirects(**kwargs: Any) -> Any:
    from mcp.shared._httpx_utils import create_mcp_http_client

    client = create_mcp_http_client(**kwargs)
    # A local HTTP endpoint must not redirect a request (and its credentials) to
    # another host. Apply the same rule to HTTPS so identity stays endpoint-bound.
    client.follow_redirects = False
    return client


def connection_fingerprint(connection: Mapping[str, Any]) -> str:
    """Changing a connection must retire the session opened for the old one."""

    return hashlib.sha256(json.dumps(connection, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _http_headers(connection: Mapping[str, Any]) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in (connection.get("headers") or {}).items()}
    for name, variable in (connection.get("headerEnv") or {}).items():
        value = os.environ.get(str(variable), "")
        if value:
            headers[str(name)] = value
    bearer = os.environ.get(str(connection.get("bearerEnv") or ""), "")
    if bearer:
        headers["Authorization"] = "Bearer %s" % bearer
    return headers


class _SessionOwner:
    """Keep transport cancel scopes in one task across separate Host tool calls."""

    def __init__(self, client: Any, tool: Mapping[str, Any]) -> None:
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._run(client, tool), name="bread-mcp-" + str(tool.get("id", "tool")))

    async def _run(self, client: Any, tool: Mapping[str, Any]) -> None:
        stack = None
        try:
            stack, session = await client._open(tool)
            self.ready.set_result(session)
            await self.stop.wait()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(McpToolError(_flatten(error), transport_failure=_transport_exception(error), request_sent=False))
        finally:
            if stack is not None:
                try:
                    await stack.aclose()
                except BaseException:
                    pass

    async def close(self) -> None:
        self.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class McpToolClient:
    """One live MCP session per registered tool, opened on demand."""

    def __init__(self) -> None:
        self._owners: dict[str, _SessionOwner] = {}
        self._sessions: dict[str, Any] = {}
        self._fingerprints: dict[str, str] = {}
        self._guard = asyncio.Lock()

    async def _open(self, tool: Mapping[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        connection = tool.get("connection") or {}
        kind = str(connection.get("kind", ""))
        stack = AsyncExitStack()
        try:
            if kind == "mcp-stdio":
                environment = dict(os.environ)
                environment.update(
                    {str(k): str(v) for k, v in (connection.get("env") or {}).items()})
                parameters = StdioServerParameters(
                    command=str(connection.get("command", "")),
                    args=[str(item) for item in (connection.get("args") or [])],
                    env=environment)
                streams = await stack.enter_async_context(stdio_client(parameters))
            elif kind == "mcp-http":
                try:
                    from mcp.client.streamable_http import streamablehttp_client
                except ImportError as error:  # pragma: no cover - depends on the runtime
                    raise McpToolError(
                        "this MCP runtime has no streamable HTTP client") from error
                opened = await stack.enter_async_context(
                    streamablehttp_client(validate_http_endpoint(str(connection.get("url", ""))),
                                          headers=_http_headers(connection),
                                          httpx_client_factory=_http_client_without_redirects))
                # streamablehttp_client yields (read, write, get_session_id).
                streams = opened[:2]
            else:
                raise McpToolError(f"{tool.get('id', 'tool')} is not an MCP connection")
            session = await stack.enter_async_context(ClientSession(*streams))
            await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT_SECONDS)
        except BaseException:
            await stack.aclose()
            raise
        return stack, session

    async def session(self, tool: Mapping[str, Any]) -> Any:
        identifier = str(tool.get("id", ""))
        fingerprint = connection_fingerprint(tool.get("connection") or {})
        async with self._guard:
            if self._fingerprints.get(identifier) != fingerprint:
                await self._close(identifier)
            existing = self._sessions.get(identifier)
            owner = self._owners.get(identifier)
            if existing is not None and owner is not None and not owner.task.done():
                return existing
            await self._close(identifier)
            owner = _SessionOwner(self, tool)
            try:
                session = await asyncio.wait_for(asyncio.shield(owner.ready), timeout=CONNECT_TIMEOUT_SECONDS)
            except BaseException as error:  # noqa: BLE001 - reported, never raised raw
                await owner.close()
                if owner.ready.done() and not owner.ready.cancelled():
                    owner.ready.exception()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise McpToolError(_flatten(error), transport_failure=getattr(error, "transport_failure", True), request_sent=False) from error
            self._owners[identifier] = owner
            self._sessions[identifier] = session
            self._fingerprints[identifier] = fingerprint
            return session

    async def list_tools(self, tool: Mapping[str, Any]) -> list[dict[str, Any]]:
        session = await self.session(tool)
        try:
            listing = await asyncio.wait_for(session.list_tools(), timeout=LIST_TIMEOUT_SECONDS)
        except BaseException as error:  # noqa: BLE001
            await self.close(str(tool.get("id", "")))
            if isinstance(error, asyncio.CancelledError):
                raise
            raise McpToolError(_flatten(error), transport_failure=True) from error
        described: list[dict[str, Any]] = []
        for item in list(getattr(listing, "tools", ()) or ())[:256]:
            schema = getattr(item, "inputSchema", None) or {}
            properties = schema.get("properties") if isinstance(schema, Mapping) else None
            described.append({
                "name": str(getattr(item, "name", "")),
                "description": str(getattr(item, "description", "") or "")[:512],
                "arguments": sorted(properties.keys()) if isinstance(properties, Mapping) else [],
                "required": list(schema.get("required") or []) if isinstance(schema, Mapping)
                else [],
            })
        return described

    async def call(self, tool: Mapping[str, Any], name: str,
                   arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Call one operation and return its text/structured payload."""

        session = await self.session(tool)
        try:
            result = await asyncio.wait_for(
                session.call_tool(str(name), dict(arguments)), timeout=CALL_TIMEOUT_SECONDS)
        except BaseException as error:  # noqa: BLE001
            if _transport_exception(error):
                await self.close(str(tool.get("id", "")))
            if isinstance(error, asyncio.CancelledError):
                raise
            raise McpToolError(_flatten(error), transport_failure=_transport_exception(error), request_sent=True) from error
        if getattr(result, "isError", False):
            raise McpToolError(_result_text(result)[:400] or "the tool reported an error")
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, Mapping) and structured:
            return dict(structured)
        text = _result_text(result)
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise McpToolError("the tool did not answer with JSON") from error
        if not isinstance(payload, Mapping):
            raise McpToolError("the tool answered with JSON that is not an object")
        return dict(payload)

    async def close(self, tool_id: str) -> None:
        async with self._guard:
            await self._close(tool_id)

    async def _close(self, tool_id: str) -> None:
        owner = self._owners.pop(tool_id, None)
        self._sessions.pop(tool_id, None)
        self._fingerprints.pop(tool_id, None)
        if owner is not None:
            await owner.close()

    async def aclose(self) -> None:
        for tool_id in list(self._owners):
            await self.close(tool_id)


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for item in list(getattr(result, "content", ()) or ()):
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _flatten(error: BaseException) -> str:
    nested = getattr(error, "exceptions", None)
    if nested:
        return "; ".join(_flatten(item) for item in nested)
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


_CLIENT: McpToolClient | None = None


def shared_client() -> McpToolClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = McpToolClient()
    return _CLIENT


__all__ = ["McpToolClient", "McpToolError", "connection_fingerprint", "shared_client"]
