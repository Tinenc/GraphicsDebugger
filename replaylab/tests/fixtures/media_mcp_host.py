"""Real Host image-returning functions over MCP, with a recorded PNG instead of a GPU.

Used only by transport tests. No model, live capture, project, or user config is read.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP
from src import server


root = Path(sys.argv[1]).resolve()
source = root / "rt.png"
raw = source.read_bytes()
server._evidence_roots = lambda: [root]


def capture_receipt(**arguments):
    payload = {
        "schema": "replaylab-event-texture-evidence-host/v1",
        "eventId": arguments["event_id"], "status": "saved",
        "artifact": {"status": "saved", "output": {
            "path": str(source), "byteSize": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "mimeType": "image/png"}},
    }
    if arguments["event_id"] == 42:
        payload["detail"] = "evidence-" * 10000
    return json.dumps(payload)


server._capture_event_texture_evidence_receipt = capture_receipt
host = FastMCP("Bread media transport test")
host.add_tool(server.capture_event_texture_evidence, structured_output=False)
host.add_tool(server.read_evidence_file, structured_output=False)
host.run(transport="stdio")
