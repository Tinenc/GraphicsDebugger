"""Configured external MCP with large UTF-8 results and an invocation count."""
import asyncio
import json
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

server = Server("Large external fixture", version="1")
calls = 0

@server.list_tools()
async def tools():
    return [Tool(name="large", description="Large UTF-8 result.", inputSchema={"type": "object"})]

@server.call_tool(validate_input=False)
async def call(name, arguments):
    global calls
    calls += 1
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({
        "calls": calls, "source": "参数😀\"\n" * 12000,
        "nextStep": "external data stays exact", "strategy": "an external field",
    }, ensure_ascii=False))])

async def main():
    options = InitializationOptions(server_name="external-large", server_version="1",
        capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}))
    async with stdio_server() as streams:
        await server.run(*streams, options)

if __name__ == "__main__":
    asyncio.run(main())
