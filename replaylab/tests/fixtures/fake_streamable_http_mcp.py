from __future__ import annotations

import argparse
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
arguments = parser.parse_args()

server = FastMCP(
    "ReplayLab test HTTP MCP",
    host="127.0.0.1",
    port=arguments.port,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    log_level="ERROR",
)


@server.tool()
def search_docs(query: str) -> dict[str, str]:
    return {
        "documentId": "http-doc-" + query.casefold().replace(" ", "-"),
        "summary": "HTTP fixture result",
    }


@server.tool()
def oversized_result() -> dict[str, str]:
    return {"payload": "x" * (4 * 1024 * 1024)}


class _ExpectedHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        expected_authorization = os.environ.get(
            "REPLAYLAB_TEST_HTTP_EXPECT_AUTHORIZATION", ""
        )
        expected_custom = os.environ.get("REPLAYLAB_TEST_HTTP_EXPECT_CUSTOM", "")
        if expected_authorization and request.headers.get("authorization") != expected_authorization:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if expected_custom and request.headers.get("x-replaylab-test") != expected_custom:
            return JSONResponse({"error": "missing test header"}, status_code=401)
        return await call_next(request)


application = server.streamable_http_app()
application.add_middleware(_ExpectedHeadersMiddleware)
uvicorn.run(application, host="127.0.0.1", port=arguments.port, log_level="critical")
