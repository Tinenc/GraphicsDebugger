"""Verify the delivered BreadMCP executable over its real stdio transport."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

parser = argparse.ArgumentParser()
parser.add_argument("--host", type=Path, required=True)
parser.add_argument("--instance", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

async def probe():
    environment = dict(os.environ, REPLAYLAB_INSTANCE_ID=args.instance)
    parameters = StdioServerParameters(command=str(args.host.resolve()), args=[], env=environment)
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listing = await session.list_tools()
            names = [tool.name for tool in listing.tools]
            assert "ping" in names and "get_capture_status" in names
            results = {}
            for name in ("ping", "get_capture_status"):
                response = await session.call_tool(name, {})
                assert not response.isError, response
                results[name] = [item.text for item in response.content if item.type == "text"]
            result = {"status":"passed","toolCount":len(names),"instance":args.instance,"results":results}
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
            print(json.dumps(result,ensure_ascii=False))

asyncio.run(asyncio.wait_for(probe(), timeout=90))
