"""No-effect MCP endpoint used only by the explicit end-to-end connection check."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def main():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    server = FastMCP("Bread connection check")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def bread_connection_probe(challenge: str) -> dict:
        """Echo a connection-check challenge. No capture, project or Unity changes."""
        if len(challenge) != 64 or any(c not in "0123456789abcdef" for c in challenge):
            raise ValueError("Invalid connection-check challenge")
        proof = {"challengeSha256": hashlib.sha256(challenge.encode()).hexdigest(), "tool": "bread_connection_probe"}
        receipt = Path(os.environ["BREAD_CONNECTION_PROBE_RECEIPT"])
        pending = receipt.with_suffix(".pending")
        pending.write_text(json.dumps(proof), encoding="utf-8")
        pending.replace(receipt)
        return proof

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
