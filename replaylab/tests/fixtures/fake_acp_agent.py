from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlsplit

from acp import PROTOCOL_VERSION, PromptResponse, run_agent, text_block
from acp import schema
from acp.exceptions import RequestError


class EchoAgent:
    def on_connect(self, connection) -> None:
        self.connection = connection

    async def initialize(self, protocol_version, **kwargs):
        # Like the real vendor agents that accept an HTTP MCP server (Claude
        # Code, Cursor): advertise it, otherwise the connector declares the
        # Host's stdio bridge instead and the "filtered" assertions below
        # would see a StdioMcpServer.
        return schema.InitializeResponse(
            protocolVersion=min(protocol_version, PROTOCOL_VERSION),
            agentInfo=schema.Implementation(name="fake-acp", version="1"),
            agentCapabilities=schema.AgentCapabilities(
                mcpCapabilities=schema.McpCapabilities(http=True, sse=False)
            ),
        )

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        assert cwd
        if "extension-notifications" in sys.argv:
            await self.connection.ext_notification("codebuddy.ai/system_init", {"sessionId": "session-test"})
        if "no-mcp" in sys.argv:
            assert not mcp_servers
            return schema.NewSessionResponse(sessionId="session-test")
        assert mcp_servers and mcp_servers[0].name == "Bread"
        if "filtered" in sys.argv:
            server = mcp_servers[0]
            assert isinstance(server, schema.HttpMcpServer)
            endpoint = urlsplit(server.url)
            assert endpoint.scheme == "http"
            assert endpoint.hostname == "127.0.0.1"
            assert endpoint.port is not None
            assert endpoint.path == "/mcp"
            assert not endpoint.username
            assert not endpoint.password
            assert not endpoint.query
            assert not endpoint.fragment
            assert len(server.headers) == 1
            authorization = server.headers[0]
            assert authorization.name.lower() == "authorization"
            assert authorization.value.startswith("Bearer ")
            assert len(authorization.value.removeprefix("Bearer ")) >= 32
            # The vendor receives only an opaque loopback capability. Host
            # executable, registry, provider and filter argv remain private to
            # AgentHost's sibling proxy process.
            rendered = server.model_dump_json(by_alias=True).lower()
            for private_value in (
                "replaylabmcp.exe",
                "replaylab_agent.mcp_proxy",
                "--host",
                "--host-arg",
                "--allow",
                "--registry-path",
                "--scope-json",
                "--completion-signal",
            ):
                assert private_value not in rendered
        return schema.NewSessionResponse(sessionId="session-test")

    async def prompt(self, session_id, prompt, **kwargs):
        assert session_id == "session-test"
        if "extension-notifications" in sys.argv:
            # SDK adds the wire '_' prefix and removes it before dispatching
            # to the client: _codebuddy.ai/command -> codebuddy.ai/command.
            for method in ("codebuddy.ai/command", "codebuddy.ai/artifact",
                           "codebuddy.ai/checkpoint", "com.example/future_notification"):
                await self.connection.ext_notification(method, {
                    "sessionId": session_id, "command": "unavailable-command",
                    "text": "must not appear in the answer", "status": "completed",
                })
        if "extension-requests" in sys.argv:
            for method in ("codebuddy.ai/delegateTool", "com.example/future_request"):
                try:
                    await self.connection.ext_method(method, {"sessionId": session_id})
                except RequestError as error:
                    assert error.code == -32601, error
                else:
                    raise AssertionError("unknown extension request was accepted")
        if "large-tool-record" in sys.argv:
            await self.connection.session_update(session_id, schema.ToolCallStart(
                toolCallId="large-tool", title="Mcp(Bread:get_pipeline_state)", kind="read",
                status="in_progress", rawInput={"semantic_fields": "x" * (20 * 1024 * 1024)},
                sessionUpdate="tool_call"))
            await self.connection.session_update(session_id, schema.ToolCallProgress(
                toolCallId="large-tool", status="completed",
                rawOutput={"image": {"mimeType": "image/png", "data": "y" * (20 * 1024 * 1024)}},
                sessionUpdate="tool_call_update"))
        if "exit" in sys.argv:
            os._exit(23)
        if "permission-exact" in sys.argv:
            permission = await self.connection.request_permission(
                session_id,
                schema.ToolCallUpdate(
                    toolCallId="permission-call-1",
                    title="Untrusted provider label",
                    kind="other",
                    rawInput={
                        "server": "Bread",
                        "tool": "get_pipeline_state",
                    },
                ),
                [
                    schema.PermissionOption(
                        optionId="allow-once",
                        name="Allow once",
                        kind="allow_once",
                    ),
                    schema.PermissionOption(
                        optionId="reject-once",
                        name="Reject",
                        kind="reject_once",
                    ),
                ],
            )
            assert permission.outcome.outcome == "selected"
            assert permission.outcome.option_id == "allow-once"
        if "timeline" in sys.argv:
            await self.connection.session_update(
                session_id,
                schema.AgentPlanUpdate(
                    entries=[
                        schema.PlanEntry(
                            content=(
                                "Inspect C:/private/capture.rdc with "
                                "token=plan-secret and PrivatePlanner(...)."
                            ),
                            priority="high",
                            status="in_progress",
                        ),
                        schema.PlanEntry(
                            content="Validate the evidence.",
                            priority="medium",
                            status="pending",
                        ),
                    ],
                    sessionUpdate="plan",
                ),
            )
            await self.connection.session_update(
                session_id,
                schema.AgentThoughtChunk(
                    content=text_block(
                        "RAW_PRIVATE_CHAIN_OF_THOUGHT token=thought-secret "
                        "C:/private/reasoning.py"
                    ),
                    sessionUpdate="agent_thought_chunk",
                ),
            )
            await self.connection.session_update(
                session_id,
                schema.ToolCallStart(
                    toolCallId="C:/private/tool-call-token-secret",
                    title="Mcp(Bread:get_pipeline_state)",
                    kind="read",
                    status="in_progress",
                    rawInput={
                        "event_id": 7,
                        "api_key": "sk-private-value",
                        "clientSecret": "private-secret-value",
                        "path": "C:/private/capture.rdc",
                        "command": "python C:/private/tool.py --token private",
                        "safe_name": "PrivatePythonObject(value='secret')",
                    },
                    rawOutput={"secret": "start-output-secret"},
                    content=[
                        schema.FileEditToolCallContent(
                            path="C:/private/internal.py",
                            oldText="old-secret",
                            newText="new-secret",
                            type="diff",
                        )
                    ],
                    locations=[
                        schema.ToolCallLocation(path="C:/private/location.py", line=12)
                    ],
                    sessionUpdate="tool_call",
                ),
            )
            await self.connection.session_update(
                session_id,
                schema.ToolCallProgress(
                    toolCallId="C:/private/tool-call-token-secret",
                    title="PrivateTool(path='C:/private/update.py', token='update-secret')",
                    kind="read",
                    status="in_progress",
                    rawOutput={"secret": "progress-output-secret"},
                    sessionUpdate="tool_call_update",
                ),
            )
            await self.connection.session_update(
                session_id,
                schema.ToolCallProgress(
                    toolCallId="C:/private/tool-call-token-secret",
                    title="PrivateTool(error='private-error-body')",
                    status="completed",
                    rawOutput={
                        "error": "PrivatePythonError at C:/private/final.py",
                        "token": "final-output-secret",
                    },
                    sessionUpdate="tool_call_update",
                ),
            )
            await self.connection.session_update(
                session_id,
                schema.AgentPlanUpdate(
                    entries=[
                        schema.PlanEntry(
                            content="C:/private/completed.py plan-secret",
                            priority="high",
                            status="completed",
                        ),
                        schema.PlanEntry(
                            content="PrivatePlanner(result='secret')",
                            priority="medium",
                            status="completed",
                        ),
                    ],
                    sessionUpdate="plan",
                ),
            )
        text = prompt[0].text
        chunks = ["x" * 8192] * 129 if "many-deltas" in sys.argv else ["ACP: " + text]
        for chunk in chunks:
            await self.connection.session_update(
                session_id,
                schema.AgentMessageChunk(
                    content=text_block(chunk),
                    sessionUpdate="agent_message_chunk",
                ),
            )
        return PromptResponse(stopReason="refusal" if "refusal" in sys.argv else "end_turn")

    async def cancel(self, session_id, **kwargs):
        return None


asyncio.run(run_agent(EchoAgent()))
