"""Older/newer Host fixture: one supported operation plus one undeclared one."""

from __future__ import annotations

import asyncio
import json

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

server = Server("Bread partial test Host", version="1")
calls = 0


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=name, description="Partial Host test operation.",
                 inputSchema={"type": "object", "additionalProperties": True})
            for name in ("inspect_event", "new_host_tool")]


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    global calls
    calls += 1
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(
        {"tool": name, "calls": calls}))], isError=False)


async def main() -> None:
    options = InitializationOptions(
        server_name="bread-partial-test-host", server_version="1",
        capabilities=server.get_capabilities(notification_options=NotificationOptions(),
                                            experimental_capabilities={}),
    )
    async with stdio_server() as streams:
        await server.run(*streams, options)


if __name__ == "__main__":
    asyncio.run(main())
