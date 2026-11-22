"""Full-manifest MCP stub used to prove one-session concurrent proxy calls."""

from __future__ import annotations

import asyncio
import json

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from replaylab_agent.policy import EFFECTFUL_TOOLS, READ_ONLY_TOOLS, ordered_tool_names


server = Server("Bread concurrent test Host", version="1")
_effect_calls = 0
_active_effects = 0
_max_active_effects = 0


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name=name,
            description="Concurrent proxy test operation.",
            inputSchema={"type": "object", "additionalProperties": True},
        )
        for name in ordered_tool_names(READ_ONLY_TOOLS | EFFECTFUL_TOOLS)
    ]


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    global _active_effects, _effect_calls, _max_active_effects
    if name == "compile_shader_workspace":
        _effect_calls += 1
        _active_effects += 1
        _max_active_effects = max(_max_active_effects, _active_effects)
        try:
            if arguments.get("large_receipt") is True:
                payload = {
                    "schema": "test-large-receipt/v1",
                    "operationId": "large-operation-1",
                    "status": "done",
                    "applied": True,
                    "accepted": True,
                    "invocationCount": _effect_calls,
                    "receipt": "数据" * 50_000,
                }
            else:
                delay = float(arguments.get("delay_seconds", 1.5))
                await asyncio.sleep(max(0.01, min(delay, 2.0)))
                payload = {
                    "schema": "test-long-call/v1",
                    "finished": True,
                    "maxActive": _max_active_effects,
                }
        finally:
            _active_effects -= 1
    elif name == "get_host_runtime_status":
        payload = {
            "schema": "test-runtime-status/v1",
            "toolStatus": "busy",
            "effectCalls": _effect_calls,
            "activeEffects": _active_effects,
            "maxActive": _max_active_effects,
        }
    else:
        payload = {"schema": "test-call/v1", "tool": name}
    encoded = json.dumps(payload)
    return CallToolResult(
        content=[TextContent(type="text", text=encoded)],
        structuredContent={"result": encoded},
        isError=False,
    )


async def _run() -> None:
    options = InitializationOptions(
        server_name="bread-concurrent-test-host",
        server_version="1",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    async with stdio_server() as streams:
        await server.run(*streams, options)


if __name__ == "__main__":
    asyncio.run(_run())
