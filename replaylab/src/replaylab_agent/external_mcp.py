"""Actual, lazily exposed connections to explicitly configured MCP servers.

External operations retain their server contract and provenance. They are not
first-party Host tools and do not inherit a fabricated Bread effect receipt.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

from .agent_diagnostics import get_logger, redact


_STARTUP_TIMEOUT_SECONDS = 10.0
_STARTUP_TOTAL_SECONDS = 30.0
_FAILED_CLOSE_SECONDS = 5.0
_TOOL_TIMEOUT_SECONDS = 60.0
_DIAGNOSTIC_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")


def _http_client_without_redirects(**kwargs: Any) -> Any:
    from mcp.shared._httpx_utils import create_mcp_http_client

    client = create_mcp_http_client(**kwargs)
    client.follow_redirects = False
    return client


def _safe_external_error(server: Mapping[str, Any], error: BaseException) -> str:
    """Keep useful connection failures without exposing configured credentials.

    External transports may echo URLs or entire headers, with credential names
    that the generic diagnostic redactor cannot know. Scrub before truncating,
    including escaped values and the exception chain.
    """
    secrets: set[str] = set()
    try:
        resolved_headers = http_headers(server)
    except (TypeError, ValueError, AttributeError):
        resolved_headers = {}
    for values in (server.get("env"), server.get("headers"), server.get("http_headers"),
                   resolved_headers):
        if isinstance(values, Mapping):
            secrets.update(str(value) for value in values.values() if value)
    header_env = server.get("headerEnv") or {}
    for variable in (*list(header_env.values() if isinstance(header_env, Mapping) else ()),
                     server.get("bearerEnv", "")):
        value = os.environ.get(str(variable), "")
        if value:
            secrets.add(value)
    try:
        endpoint = urlsplit(str(server.get("url") or ""))
        secrets.update(unquote(value) for value in (endpoint.username, endpoint.password) if value)
        secrets.update(value for _, value in parse_qsl(endpoint.query) if value)
    except ValueError:
        pass
    variants = set()
    for value in secrets:
        variants.update((value, json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1],
                         quote(value, safe=""), quote_plus(value)))

    def safe_url(match: re.Match[str]) -> str:
        try:
            endpoint = urlsplit(match.group())
            # Paths, queries and fragments can themselves be capability tokens.
            host = endpoint.hostname or "[REDACTED]"
            if ":" in host:
                host = "[" + host + "]"
            port = ":" + str(endpoint.port) if endpoint.port is not None else ""
            return endpoint.scheme + "://" + host + port + "/[REDACTED]"
        except ValueError:
            return "[REDACTED URL]"

    parts, seen = [], set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        message = str(current).strip()
        if len(message) > 1024 * 1024:
            message = "External server diagnostic exceeds the display limit"
        else:
            message = _DIAGNOSTIC_URL.sub(safe_url, message)
            for value in sorted(variants, key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            message = redact(message)
        parts.append(type(current).__name__ + ": " + message)
        current = current.__cause__ or current.__context__
    text = " <- ".join(parts).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= 600 else text[:599] + "…"


def _is_unscoped_bread_host(specs: Sequence[Any], initialization: Any = None) -> bool:
    info = getattr(initialization, "serverInfo", None)
    product = str(getattr(info, "name", "") or "").casefold()
    instructions = str(getattr(initialization, "instructions", "") or "")
    if (product in {"bread task filter", "replaylab-task-filter"}
            or product == "bread" and "Capture-bound GPU replay analysis" in instructions):
        return True
    names = {str(spec if isinstance(spec, str) else
                 spec.get("name", "") if isinstance(spec, Mapping) else
                 getattr(spec, "name", "") or "") for spec in specs}
    # Product-specific inventory, not generic graphics terminology or a server
    # nickname. An unrelated MCP can expose a capture/status tool legitimately.
    return ("get_application_ui_state" in names
            and bool(names & {"get_capture_status", "get_current_capture_status"})
            and bool(names & {"prepare_frame_render_flow", "prepare_shader_workspace",
                              "get_shader_workspace_ui_state"}))


def _startup_timeout(server: Mapping[str, Any]) -> float:
    value = server.get("startup_timeout_sec", _STARTUP_TIMEOUT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return _STARTUP_TIMEOUT_SECONDS
    return max(0.1, min(float(value), _STARTUP_TOTAL_SECONDS))


def _tool_timeout(server: Mapping[str, Any]) -> float:
    value = server.get("tool_timeout_sec", _TOOL_TIMEOUT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return _TOOL_TIMEOUT_SECONDS
    return max(0.1, min(float(value), 3600.0))


def http_headers(server: Mapping[str, Any]) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    for name, variable in (server.get("headerEnv") or {}).items():
        value = os.environ.get(str(variable), "")
        if value:
            headers[str(name)] = value
    bearer = os.environ.get(str(server.get("bearerEnv") or ""), "")
    if bearer:
        headers["Authorization"] = "Bearer %s" % bearer
    return headers


class _MCPConnection:
    """Own a transport's AnyIO scopes in a dedicated task for its whole life.

    An MCP transport's background task may cancel the task which entered it.
    Keeping several transports open in the agent task lets an optional server
    cancel unrelated tools. Only this owner enters and exits its contexts.
    """

    def __init__(self, server: Mapping[str, Any], *, timeout: float,
                 failed_servers: dict[str, str]) -> None:
        self.server = server
        self.identifier = str(server["id"])
        self.timeout = timeout
        self.failed_servers = failed_servers
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.stopping = False
        self.session: Any = None
        self.specs: list[Any] = []
        self.failure = ""
        self.task = asyncio.create_task(self._run(), name="bread-external-mcp-" + self.identifier)

    def fail(self, message: str) -> None:
        if not self.failure:
            self.failure = message
            self.failed_servers[self.identifier] = message
            get_logger().warning("Configured MCP server %s unavailable: %s", self.identifier, message)

    async def _run(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server = self.server
        error_sink = None
        own_portable = server.get("id") == "bread-portable" and server.get("source") == "application-bundle"
        try:
            async with AsyncExitStack() as connection:
                try:
                    async with asyncio.timeout(self.timeout):
                        if str(server.get("transport", "stdio")) == "http":
                            from mcp.client.streamable_http import streamablehttp_client
                            from .agent_tools_config import validate_mcp_server

                            # Imported desktop definitions follow the same endpoint
                            # rules as saved settings; neither may redirect secrets.
                            endpoint = validate_mcp_server({"id": "http-endpoint", "transport": "http",
                                                            "url": server.get("url", "")}).url
                            opened = await connection.enter_async_context(streamablehttp_client(
                                endpoint, headers=http_headers(server) or None,
                                httpx_client_factory=_http_client_without_redirects,
                            ))
                            streams = opened[:2]
                        else:
                            if server.get("source") == "bundled-provider-client":
                                from .portable_capabilities import prepare_server
                                await asyncio.to_thread(prepare_server, server)
                            error_sink = connection.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8")
                                if own_portable else open(os.devnull, "w"))
                            streams = await connection.enter_async_context(stdio_client(StdioServerParameters(
                                command=str(server.get("command", "")),
                                args=[str(value) for value in server.get("args", ()) or ()],
                                env={str(k): str(v) for k, v in (server.get("env") or {}).items()} or None,
                                cwd=str(server["cwd"]) if server.get("cwd") else None,
                            ), errlog=error_sink))
                        session = await connection.enter_async_context(ClientSession(*streams))
                        initialization = await session.initialize()
                        listing = await session.list_tools()
                except BaseException as error:
                    if not self.stopping:
                        detail = _safe_external_error(server, error)
                        if own_portable and error_sink is not None:
                            try:
                                error_sink.seek(0)
                                hint = error_sink.read(4096).strip()
                                if hint:
                                    detail += " | " + _safe_external_error(server, RuntimeError(hint))
                            except (OSError, ValueError):
                                pass
                        self.fail(detail[:1200])
                    if not self.ready.done():
                        self.ready.set_result(False)
                    raise
                self.specs = list(getattr(listing, "tools", ()) or ())
                if _is_unscoped_bread_host(self.specs, initialization):
                    self.fail("Duplicate Bread Host connection excluded; use this task's capture-scoped Bread tools")
                    self.ready.set_result(False)
                    return
                self.session = session
                self.ready.set_result(True)
                await self.stop.wait()
        except BaseException as error:
            # Transport-internal cancellation belongs to this owner. A user's
            # cancellation is propagated by attach/call in the calling task.
            if not self.stopping:
                self.fail(_safe_external_error(server, error))
        finally:
            if not self.ready.done():
                self.ready.set_result(False)


async def _close_connections(connections: Sequence[_MCPConnection]) -> None:
    tasks = []
    for connection in connections:
        connection.stopping = True
        connection.stop.set()
        if not connection.task.done():
            if not connection.ready.done():
                connection.task.cancel()
            tasks.append(connection.task)
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=_FAILED_CLOSE_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        # Context teardown still runs in its owner task, including subprocess
        # termination. asyncio.wait never cancels the caller or the other owners.
        await asyncio.wait(pending, timeout=_FAILED_CLOSE_SECONDS)


@dataclass(slots=True)
class ExternalTool:
    server_id: str
    raw_name: str
    spec: Any
    session: Any
    approval_mode: str = "approve"

    def contract(self, name: str, *, include_schema: bool = True) -> dict[str, Any]:
        result = {
            "name": name,
            "family": "external-mcp",
            "server": self.server_id,
            "authority": "user-configured-mcp-server",
            "description": str(getattr(self.spec, "description", "") or ""),
            "approvalMode": self.approval_mode,
        }
        if include_schema:
            result["inputSchema"] = dict(getattr(self.spec, "inputSchema", None)
                                         or getattr(self.spec, "input_schema", None) or {})
        else:
            result["description"] = result["description"][:240]
        return result


class ExternalMCPPool:
    def __init__(self) -> None:
        self.tools: dict[str, ExternalTool] = {}
        self.failed_servers: dict[str, str] = {}
        self._server_configs: dict[str, Mapping[str, Any]] = {}
        self._connections: dict[str, _MCPConnection] = {}

    async def attach(self, servers: Sequence[Mapping[str, Any]], stack: AsyncExitStack) -> None:
        deadline = asyncio.get_running_loop().time() + _STARTUP_TOTAL_SECONDS
        connections = []
        for server in servers[:32]:
            identifier = str(server.get("id", "") or "").strip()
            transport = str(server.get("transport", "stdio"))
            if not identifier:
                continue
            if transport not in {"stdio", "http"}:
                self.failed_servers[identifier] = "unsupported MCP transport: %s" % transport
                continue
            if identifier in self._connections:
                continue
            connection = _MCPConnection(server, timeout=min(_STARTUP_TOTAL_SECONDS, _startup_timeout(server)),
                                        failed_servers=self.failed_servers)
            self._connections[identifier] = connection
            connections.append(connection)
        stack.push_async_callback(_close_connections, connections)
        if not connections:
            return
        try:
            await asyncio.wait([connection.ready for connection in connections],
                               timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        except asyncio.CancelledError:
            await _close_connections(connections)
            raise
        failed = []
        for connection in connections:
            if not connection.ready.done():
                connection.fail("Optional MCP startup budget exhausted; the scoped Bread tools remain available")
                failed.append(connection)
                continue
            if not connection.ready.result() or connection.failure or connection.task.done():
                failed.append(connection)
                continue
            identifier, server = connection.identifier, connection.server
            self._server_configs[identifier] = server
            for spec in connection.specs[:128]:
                raw_name = str(getattr(spec, "name", "") or "")
                if not raw_name:
                    continue
                enabled = server.get("enabled_tools")
                if enabled is not None and raw_name not in enabled:
                    continue
                if raw_name in (server.get("disabled_tools") or ()):
                    continue
                rules = server.get("approvalRules") or {}
                rule = (rules.get("tools") or {}).get(raw_name) or {}
                if rule.get("enabled") is False:
                    continue
                approval_mode = str(rule.get("approval_mode") or rules.get("default") or "approve")
                name = f"{identifier}__{raw_name}"[:96]
                if name not in self.tools:
                    self.tools[name] = ExternalTool(identifier, raw_name, spec, connection.session, approval_mode)
        await _close_connections(failed)

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        from mcp.types import TextContent

        tool = self.tools[name]
        connection = self._connections.get(tool.server_id)
        if connection is not None and (connection.failure or connection.task.done() or connection.stopping):
            raise RuntimeError(connection.failure or "Configured MCP connection is closed")
        if tool.approval_mode != "approve":
            from mcp.types import CallToolResult
            # Imported desktop MCP approvals belong to that provider. This
            # bridge has no live approval broker; never fabricate consent.
            return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps({
                "error": "external-tool-approval-required" if tool.approval_mode == "prompt" else "external-tool-disabled",
                "server": tool.server_id, "tool": tool.raw_name, "approvalMode": tool.approval_mode,
                "message": "The configured provider approval is required. Use the native provider connection or configure approval for this exact tool; no operation was executed.",
            }, ensure_ascii=False))])
        pending_call = asyncio.create_task(tool.session.call_tool(tool.raw_name, dict(arguments)))
        try:
            async with asyncio.timeout(_tool_timeout(self._server_configs.get(tool.server_id, {}))):
                if connection is not None:
                    completed, _pending = await asyncio.wait((pending_call, connection.task),
                                                            return_when=asyncio.FIRST_COMPLETED)
                    if pending_call not in completed:
                        raise RuntimeError(connection.failure or "Configured MCP connection closed during tool call")
                result = await pending_call
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RuntimeError(_safe_external_error(
                self._server_configs.get(tool.server_id, {}), error)) from None
        finally:
            if not pending_call.done():
                pending_call.cancel()
                await asyncio.wait((pending_call,), timeout=_FAILED_CLOSE_SECONDS)
        provenance = TextContent(type="text", text=json.dumps({"externalMcp": {
            "server": tool.server_id, "tool": tool.raw_name,
            "authority": "user-configured-mcp-server",
        }}, ensure_ascii=False, separators=(",", ":")))
        # Keep the server's structured result and media intact; the small prefix
        # identifies the authority for every route, including text-only APIs.
        return result.model_copy(update={"content": [provenance, *result.content]})


__all__ = ["ExternalMCPPool", "ExternalTool", "http_headers"]
