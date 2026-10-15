"""
File-based IPC Client for communicating with RenderDoc extension.

Protocol:
  1. Write request.json
  2. Create 'ready' signal file (tells extension the request is complete)
  3. Extension reads request, deletes it, processes, writes response.json
  4. We read response.json and delete it

IPC directory: %TEMP%/renderdoc_mcp_ipc/
"""

import json
import os
import time
import tempfile
import uuid
import logging
from typing import Any, Optional

logger = logging.getLogger("renderdoc-mcp")

IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp_ipc")
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
READY_FILE = os.path.join(IPC_DIR, "ready")


class IPCClient:
    """Communicates with RenderDoc extension via file-based IPC."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def is_bridge_alive(self) -> bool:
        try:
            resp = self.call("ping", {}, timeout=3.0)
            return resp is not None and "status" in resp
        except Exception:
            return False

    def call(self, method: str, params: dict, timeout: Optional[float] = None) -> Any:
        if timeout is None:
            timeout = self.timeout

        os.makedirs(IPC_DIR, exist_ok=True)

        # Clean stale files
        for f in [RESPONSE_FILE, REQUEST_FILE, READY_FILE]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

        # Build request
        request_id = str(uuid.uuid4())[:8]
        request = {"id": request_id, "method": method, "params": params}
        request_json = json.dumps(request, ensure_ascii=False, default=str)

        # Step 1: Write request file (atomic with fsync)
        with open(REQUEST_FILE, "w", encoding="utf-8") as f:
            f.write(request_json)
            f.flush()
            os.fsync(f.fileno())

        # Step 2: Create ready signal (tells extension request is complete)
        with open(READY_FILE, "w") as f:
            f.write("1")
            f.flush()
            os.fsync(f.fileno())

        # Wait for response
        start = time.time()
        while time.time() - start < timeout:
            if os.path.exists(RESPONSE_FILE):
                time.sleep(0.05)
                try:
                    with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
                        response = json.load(f)
                    os.remove(RESPONSE_FILE)

                    if "error" in response:
                        err = response["error"]
                        raise RuntimeError("Bridge error [%s]: %s" % (
                            err.get("code", "?"), err.get("message", str(err))))
                    return response.get("result", response)
                except json.JSONDecodeError:
                    time.sleep(0.1)
                    continue
            time.sleep(0.1)

        raise RuntimeError(
            "Timeout (%.0fs). Make sure RenderDoc is running with capture loaded. "
            "Check Tools > RenderDoc MCP > Status." % timeout)
