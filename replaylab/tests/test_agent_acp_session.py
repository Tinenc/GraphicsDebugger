"""Vendor-native ACPMainAgentSession against an in-process fake ACP SDK.

Covers: session lifecycle, direct task-scoped stdio MCP registration,
streaming deltas, tool-call segment boundaries, the fail-closed permission
policy (only ``ReplayLab`` tools from the grant are allowed) and abort on
policy violations.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from replaylab_agent.connectors.acp import _ACPSDK
import replaylab_agent.acp_session as acp_session
import replaylab_agent.native_vendor as vendor
from replaylab_agent.errors import ConnectorError, PolicyError, ProviderIncidentError, ProviderCapabilityError, LifecycleCleanupError


class _Option:
    def __init__(self, option_id: str, name: str, kind: str) -> None:
        self.option_id = option_id
        self.name = name
        self.kind = kind


class _Connection:
    """Fake ACP agent connection driven by a script."""

    def __init__(self, client: Any, script: Any) -> None:
        self.client = client
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session_id = "sess-1"
        self.mcp_servers: list[Any] = []

    http_mcp = False
    config_options: tuple[Any, ...] = ()

    async def initialize(self, **kwargs: Any) -> Any:
        self.calls.append(("initialize", kwargs))
        return SimpleNamespace(
            agent_capabilities=SimpleNamespace(mcp_capabilities=SimpleNamespace(http=self.http_mcp, sse=False))
        )

    async def authenticate(self, **kwargs: Any) -> Any:
        self.calls.append(("authenticate", kwargs))

    async def new_session(self, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
        self.calls.append(("new_session", {"cwd": cwd, "mcp_servers": mcp_servers, **kwargs}))
        self.mcp_servers = list(mcp_servers or [])
        self.meta = kwargs or None
        return SimpleNamespace(session_id=self.session_id, config_options=list(self.config_options))

    async def prompt(self, **kwargs: Any) -> Any:
        self.calls.append(("prompt", kwargs))
        return await self.script(self)

    async def cancel(self, **kwargs: Any) -> None:
        self.calls.append(("cancel", kwargs))

    # helpers used by scripts ---------------------------------------------
    async def say(self, text: str) -> None:
        await self.client.session_update(
            self.session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
        )

    async def think(self, text: str) -> None:
        await self.client.session_update(
            self.session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}
        )

    async def tool(self, call_id: str, title: str, *, server: str, tool: str, status: str = "completed") -> Any:
        permission = await self.client.request_permission(
            self.session_id,
            {"toolCallId": call_id, "title": title, "kind": "other", "rawInput": {"server": server, "tool": tool}},
            [_Option("allow", "Allow", "allow_once"), _Option("reject", "Reject", "reject_once")],
        )
        await self.client.session_update(
            self.session_id,
            {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": title, "kind": "other",
             "status": "in_progress", "rawInput": {"server": server, "tool": tool}},
        )
        await self.client.session_update(
            self.session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "title": title, "status": status},
        )
        return permission


class _Manager:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False

    async def __aenter__(self) -> tuple[Any, Any]:
        return self.connection, None

    async def __aexit__(self, *exc: Any) -> None:
        self.closed = True


def _fake_sdk(script: Any, holder: dict[str, Any]) -> _ACPSDK:
    def spawn_agent_process(client: Any, *command: str, transport_kwargs: Any = None, **kwargs: Any) -> _Manager:
        holder["command"] = command
        holder["spawn_kwargs"] = kwargs
        holder["transport_kwargs"] = transport_kwargs
        connection = _Connection(client, script)
        holder["connection"] = connection
        manager = _Manager(connection)
        holder["manager"] = manager
        return manager

    class Client:  # the SDK base class the connector subclasses
        pass

    return _ACPSDK(
        protocol_version=1,
        spawn_agent_process=spawn_agent_process,
        text_block=lambda text: {"type": "text", "text": text},
        image_block=None,
        embedded_blob_resource=None,
        resource_block=None,
        client_type=Client,
        mcp_server_stdio_type=lambda **kw: ("stdio", kw),
        mcp_server_http_type=lambda **kw: ("http", kw),
        http_header_type=lambda **kw: kw,
        request_permission_response_type=None,
        allowed_outcome_type=None,
        denied_outcome_type=None,
        decline_elicitation_response_type=None,
    )


class ACPMainAgentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_without_followup_answer_does_not_reuse_progress(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("I will inspect the shader.")
            await conn.tool("call-1", "inspect_event", server="Bread", tool="inspect_event")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                result = await session.prompt("Inspect the shader")

        self.assertEqual(result.text, "I will inspect the shader.")
        self.assertEqual(result.segments, ["I will inspect the shader.", ""])
        self.assertEqual(result.final_text, "")

    def tearDown(self) -> None:
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def _session(self, holder: dict[str, Any], **overrides: Any) -> acp_session.ACPMainAgentSession:
        self.tmp = tempfile.TemporaryDirectory()
        cwd = Path(self.tmp.name)
        params: dict[str, Any] = dict(
            command=("acp-claude-code",),
            cwd=cwd,
            env={"PATH": "/usr/bin"},
            mcp_stdio_command="replaylab-ai",
            mcp_stdio_args=("-I", "-m", "replaylab_agent.mcp_proxy"),
            mcp_stdio_env={"REPLAYLAB_TASK_ID": "task-1"},
            allowed_tools=frozenset({"inspect_event", "report_task_result"}),
            timeout=30.0,
            authentication_method="claude-login",
            provider_id="claude-code-acp",
            stderr_path=cwd / "stderr.log",
            session_meta=vendor.claude_session_meta(),
        )
        params.update(overrides)
        return acp_session.ACPMainAgentSession(**params)

    async def test_lifecycle_streaming_and_segments(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.think("planning")
            await conn.say("Let me inspect. ")
            outcome = await conn.tool("call-1", "inspect_event", server="Bread", tool="inspect_event")
            holder["outcome"] = outcome
            await conn.say("The answer is 42.")
            return {"stopReason": "end_turn"}

        deltas: list[str] = []
        timeline: list[dict[str, Any]] = []
        reasoning: list[dict[str, Any]] = []
        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            session = self._session(holder)
            async with session:
                connection: _Connection = holder["connection"]
                names = [name for name, _ in connection.calls]
                self.assertEqual(names, ["initialize", "authenticate", "new_session"])
                # Every vendor receives the direct task-scoped stdio filter.
                kind, mcp = connection.mcp_servers[0]
                self.assertEqual(kind, "stdio")
                self.assertEqual(mcp["name"], "Bread")
                self.assertEqual(mcp["args"], ["-I", "-m", "replaylab_agent.mcp_proxy"])
                env = {item["name"]: item["value"] for item in mcp["env"]}
                self.assertEqual(env["REPLAYLAB_TASK_ID"], "task-1")
                self.assertEqual(session.mcp_transport, "stdio")
                # Only project Skills remain; AskUserQuestion is bridged through
                # ACP form elicitation when the native engine enables it.
                self.assertNotIn("disableBuiltInTools", connection.meta)
                options = connection.meta["claudeCode"]["options"]
                self.assertEqual(options["tools"], ["Skill"])
                self.assertIn("Bash", options["disallowedTools"])
                self.assertNotIn("AskUserQuestion", options["disallowedTools"])
                self.assertEqual(options["allowedTools"], ["mcp__Bread"])
                self.assertEqual(options["settingSources"], ["project"])
                self.assertTrue(options["strictMcpConfig"])
                result = await session.prompt(
                    "hello",
                    on_delta=deltas.append,
                    on_timeline=timeline.append,
                    on_reasoning=reasoning.append,
                )
            self.assertTrue(holder["manager"].closed)
        self.assertEqual(result.text, "Let me inspect. The answer is 42.")
        self.assertEqual(result.final_text, "The answer is 42.")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual("".join(deltas), result.text)
        self.assertEqual(holder["outcome"], {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.assertTrue(any(event["activityId"] == "acp-tool-1" for event in timeline))
        self.assertTrue(any("planning" in event.get("text", "") for event in reasoning))
        self.assertEqual(holder["spawn_kwargs"]["cwd"], str(session.cwd))
        self.assertEqual(holder["spawn_kwargs"]["env"], {"PATH": "/usr/bin"})
        self.assertIsNot(holder["transport_kwargs"]["stderr"], asyncio.subprocess.DEVNULL)

    async def test_handshake_logs_only_bounded_redacted_provider_identity(self) -> None:
        holder: dict[str, Any] = {}

        async def script(_connection: _Connection) -> Any:
            return {"stopReason": "end_turn"}

        async def initialize(_connection: _Connection, **_arguments: Any) -> Any:
            return {
                "protocolVersion": 1,
                "agentInfo": {
                    "name": "claude-agent-acp\ntoken=private-access-value",
                    "version": "0.88.0",
                },
                "privateVendorData": "do-not-log-the-initialize-payload",
            }

        logger = mock.Mock()
        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "initialize", initialize),
            mock.patch.object(acp_session, "get_logger", return_value=logger),
        ):
            async with self._session(holder):
                pass
        opening = next(call for call in logger.info.call_args_list if "agent=%s" in call.args[0])
        rendered = opening.args[0] % opening.args[1:]
        self.assertIn("version=0.88.0 protocol=1", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("private-access-value", rendered)
        self.assertNotIn("do-not-log", rendered)
        self.assertNotIn("\n", rendered)
        self.assertLessEqual(len(acp_session._identity_text("x" * 1000)), 128)
        self.assertEqual(acp_session._identity_text(None), "unknown")

    async def test_provider_http2_cancel_chunk_is_not_assistant_text(self) -> None:
        holder: dict[str, Any] = {}
        diagnostic = (
            "Error: RetriableError: [canceled] http/2 stream closed with "
            "error code CANCEL (0x8)"
        )

        async def script(conn: _Connection) -> Any:
            await conn.say("The last mutation was saved.\n")
            await conn.say(diagnostic)
            return {"stopReason": "end_turn"}

        deltas: list[str] = []
        with mock.patch.object(
            acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)
        ):
            session = self._session(holder)
            async with session:
                with self.assertRaises(ProviderIncidentError) as raised:
                    await session.prompt("continue", on_delta=deltas.append)

        self.assertEqual(raised.exception.incident_code, "http2-cancel")
        self.assertEqual(
            raised.exception.partial_text, "The last mutation was saved.\n"
        )
        self.assertEqual("".join(deltas), "The last mutation was saved.\n")
        self.assertNotIn("RetriableError", "".join(deltas))

    async def test_provider_ping_timeout_update_is_typed_incident(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.client.session_update(
                conn.session_id,
                {
                    "sessionUpdate": "error",
                    "message": "transport PING timeout while waiting for provider",
                },
            )
            return {"stopReason": "end_turn"}

        with mock.patch.object(
            acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)
        ):
            session = self._session(holder)
            async with session:
                with self.assertRaises(ProviderIncidentError) as raised:
                    await session.prompt("continue")

        self.assertEqual(
            raised.exception.incident_code, "provider-ping-timeout"
        )
        self.assertEqual(raised.exception.partial_text, "")

    async def test_shader_error_text_is_not_misclassified_as_provider_failure(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("Error: undeclared identifier 'worldNormal'")
            return {"stopReason": "end_turn"}

        with mock.patch.object(
            acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)
        ):
            session = self._session(holder)
            async with session:
                result = await session.prompt("compile")

        self.assertEqual(result.final_text, "Error: undeclared identifier 'worldNormal'")

    async def test_native_stdio_route_and_form_elicitation(self) -> None:
        holder: dict[str, Any] = {}
        captured: list[tuple[str, Any]] = []

        async def script(conn: _Connection) -> Any:
            response = await conn.client.create_elicitation(
                "Which shader?",
                {
                    "sessionId": conn.session_id,
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "question_0": {
                                "type": "string",
                                "oneOf": [
                                    {"const": "VS", "title": "Vertex shader"},
                                    {"const": "PS", "title": "Pixel shader"},
                                ],
                            }
                        },
                    },
                },
            )
            holder["elicitation_response"] = response
            await conn.say("waiting")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(
                holder,
                mcp_stdio_command="replaylab-ai",
                mcp_stdio_args=("-m", "replaylab_agent.mcp_proxy"),
                mcp_stdio_env={"REPLAYLAB_TASK_ID": "task-1"},
                on_elicitation=lambda message, mode: captured.append((message, mode)),
            ) as session:
                kind, declaration = holder["connection"].mcp_servers[0]
                self.assertEqual(kind, "stdio")
                self.assertEqual(declaration["command"], "replaylab-ai")
                self.assertEqual(
                    declaration["args"], ["-m", "replaylab_agent.mcp_proxy"]
                )
                self.assertEqual(
                    declaration["env"],
                    [{"name": "REPLAYLAB_TASK_ID", "value": "task-1"}],
                )
                initialize = holder["connection"].calls[0][1]
                self.assertEqual(
                    initialize["client_capabilities"], {"elicitation": {"form": {}}}
                )
                result = await session.prompt("ask")
        self.assertEqual(result.text, "waiting")
        self.assertEqual(captured[0][0], "Which shader?")
        self.assertEqual(holder["elicitation_response"], {"action": "decline"})

    async def _assert_model_selection_failure(self, setter: Any, message: str) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("must not use the default model")
            return {"stopReason": "end_turn"}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "config_options", ({"id": "model", "category": "model"},)),
            mock.patch.object(_Connection, "set_config_option", setter, create=True),
        ):
            session = self._session(holder, model="requested-model", timeout=0.1)
            with self.assertRaisesRegex((ConnectorError, ProviderCapabilityError), message):
                async with session:
                    await session.prompt("hello")
            self.assertTrue(holder["manager"].closed)
            # A failed open must also reject a later direct prompt on this object.
            with self.assertRaisesRegex(ConnectorError, "session is not open"):
                await session.prompt("retry")

        self.assertNotIn("prompt", [name for name, _ in holder["connection"].calls])
        if setter is not None:
            selected = [args for name, args in holder["connection"].calls if name == "set_config_option"]
            self.assertEqual(selected, [{"config_id": "model", "session_id": "sess-1", "value": "requested-model"}])

    async def test_rejected_model_selection_never_enters_prompt(self) -> None:
        async def reject(conn: _Connection, **kwargs: Any) -> None:
            conn.calls.append(("set_config_option", kwargs))
            raise RuntimeError("requested model is unavailable")

        await self._assert_model_selection_failure(reject, "requested model is unavailable")

    async def test_model_selection_timeout_never_enters_prompt(self) -> None:
        async def stall(conn: _Connection, **kwargs: Any) -> None:
            conn.calls.append(("set_config_option", kwargs))
            await asyncio.Event().wait()

        await self._assert_model_selection_failure(stall, "ACP model selection timed out")

    async def test_advertised_model_selection_requires_sdk_setter(self) -> None:
        await self._assert_model_selection_failure(None, "session/set_config_option")

    async def test_exact_current_model_enters_prompt_without_redundant_setter(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("requested model")
            return {"stopReason": "end_turn"}

        setter = mock.AsyncMock(side_effect=AssertionError("redundant model mutation"))
        selected = "composer-2.5[fast=true]"
        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "config_options", (
                {"id": "model", "category": "model", "currentValue": selected},)),
            mock.patch.object(_Connection, "set_config_option", setter, create=True),
        ):
            async with self._session(holder, model=selected) as session:
                result = await session.prompt("hello")
        self.assertEqual(result.text, "requested model")
        setter.assert_not_called()

    async def test_current_model_with_different_option_suffix_still_requires_selection(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            raise AssertionError("wrong model must not receive prompt")

        setter = mock.AsyncMock(side_effect=RuntimeError("model selection refused"))
        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "config_options", (
                SimpleNamespace(id="model", category="model", current_value="composer-2.5[fast=false]"),)),
            mock.patch.object(_Connection, "set_config_option", setter, create=True),
        ):
            with self.assertRaisesRegex(ConnectorError, "model selection refused"):
                async with self._session(holder, model="composer-2.5[fast=true]") as session:
                    await session.prompt("hello")
        setter.assert_awaited_once()
        self.assertNotIn("prompt", [name for name, _ in holder["connection"].calls])

    async def test_vendor_environment_model_without_config_interface_is_preserved(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        environment = {"PATH": "/usr/bin", "ANTHROPIC_MODEL": "requested-model"}
        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder, model="requested-model", env=environment) as session:
                result = await session.prompt("hello")

        self.assertEqual(result.text, "ok")
        self.assertEqual(holder["spawn_kwargs"]["env"], environment)
        self.assertNotIn("set_config_option", [name for name, _ in holder["connection"].calls])

    async def test_cursor_question_extension_becomes_form_elicitation(self) -> None:
        holder: dict[str, Any] = {}
        captured: list[tuple[str, Any]] = []

        async def script(conn: _Connection) -> Any:
            holder["question_response"] = await conn.client.ext_method(
                "cursor/ask_question",
                {
                    "toolCallId": "question-1",
                    "title": "Need a shader",
                    "questions": [
                        {
                            "id": "stage",
                            "prompt": "Which shader?",
                            "options": [
                                {"id": "vs", "label": "Vertex shader"},
                                {"id": "ps", "label": "Pixel shader"},
                            ],
                            "allowMultiple": False,
                        }
                    ],
                },
            )
            await conn.say("waiting")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(
                holder,
                command=("cursor-agent", "acp"),
                authentication_method="cursor_login",
                provider_id="cursor-acp",
                session_meta=None,
                on_elicitation=lambda message, mode: captured.append((message, mode)),
            ) as session:
                self.assertEqual(
                    [name for name, _ in holder["connection"].calls],
                    ["initialize", "new_session"],
                )
                result = await session.prompt("ask")

        self.assertEqual(result.text, "waiting")
        self.assertEqual(captured[0][0], "Need a shader")
        properties = captured[0][1]["requestedSchema"]["properties"]
        self.assertEqual(
            properties["stage"]["oneOf"],
            [
                {"const": "vs", "title": "Vertex shader"},
                {"const": "ps", "title": "Pixel shader"},
            ],
        )
        self.assertEqual(
            holder["question_response"]["outcome"]["outcome"], "skipped"
        )

    async def test_cursor_plan_does_not_require_host_approval(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            holder["plan_response"] = await conn.client.ext_method(
                "cursor/create_plan",
                {
                    "toolCallId": "plan-1",
                    "plan": "Inspect the capture, then edit the chosen shader.",
                    "todos": [],
                },
            )
            await conn.say("I can continue with the available tools.")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(
                holder,
                command=("cursor-agent", "acp"),
                authentication_method="cursor_login",
                provider_id="cursor-acp",
                session_meta=None,
            ) as session:
                result = await session.prompt("organize this shader")

        self.assertEqual(holder["plan_response"], {"outcome": {"outcome": "accepted"}})
        self.assertEqual(result.text, "I can continue with the available tools.")

    async def test_permission_for_foreign_tool_is_denied_not_fatal(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            denied = await conn.tool("call-1", "Bash", server="", tool="bash")
            holder["denied"] = denied
            allowed = await conn.tool("call-2", "inspect_event", server="Bread", tool="inspect_event")
            holder["allowed"] = allowed
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                result = await session.prompt("hello")
        self.assertEqual(result.text, "ok")
        self.assertEqual(holder["denied"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(holder["allowed"]["outcome"]["outcome"], "selected")

    async def test_session_new_does_not_drop_required_metadata(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        async def without_meta(conn, cwd, mcp_servers):
            raise AssertionError("required vendor metadata must not be silently omitted")

        with (mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
              mock.patch.object(_Connection, "new_session", without_meta)):
            with self.assertRaisesRegex(ProviderCapabilityError, "required session metadata"):
                await self._session(holder).open()
        self.assertTrue(holder["manager"].closed)

    async def test_close_surfaces_failed_cleanup_and_clears_session(self) -> None:
        holder: dict[str, Any] = {}

        async def fail_cleanup(manager, *exc):
            raise OSError("transport cleanup failed")

        with (mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(lambda conn: None, holder)),
              mock.patch.object(_Manager, "__aexit__", fail_cleanup)):
            session = self._session(holder)
            await session.open()
            with self.assertRaises(LifecycleCleanupError):
                await session.close()
            self.assertIsNone(session._stack)
            self.assertIsNone(session._connection)
            self.assertIsNone(session._stderr_handle)
            self.assertEqual(session._session_id, "")

    async def test_cancel_during_close_still_closes_process(self) -> None:
        holder: dict[str, Any] = {}
        entered_cancel = asyncio.Event()

        async def wait_cancel(conn, **kwargs):
            entered_cancel.set()
            await asyncio.Event().wait()

        with (mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(lambda conn: None, holder)),
              mock.patch.object(_Connection, "cancel", wait_cancel)):
            session = self._session(holder)
            await session.open()
            task = asyncio.create_task(session.close())
            await entered_cancel.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(holder["manager"].closed)
            self.assertIsNone(session._stderr_handle)

    async def test_no_meta_for_vendors_without_one(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder, session_meta=None, provider_id="copilot-acp") as session:
                await session.prompt("hello")
        connection: _Connection = holder["connection"]
        _name, params = [call for call in connection.calls if call[0] == "new_session"][0]
        self.assertEqual(set(params), {"cwd", "mcp_servers"})

    def test_write_claude_settings_denies_builtins(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            vendor.write_claude_settings(Path(tmp))
            payload = json.loads((Path(tmp) / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash", payload["permissions"]["deny"])
        self.assertIn("Task", payload["permissions"]["deny"])
        self.assertEqual(
            payload["permissions"]["allow"],
            ["Skill", "AskUserQuestion", "mcp__Bread"],
        )

    async def test_permission_by_title_when_raw_input_is_arguments(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            # Claude Code style: rawInput carries the tool arguments, the title names the tool.
            allowed = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c1", "title": "mcp__Bread__inspect_event", "kind": "other",
                 "rawInput": {"task": "explain EID 5"}},
                [_Option("allow", "Allow", "allow_once")],
            )
            denied = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c2", "title": "Write", "kind": "edit", "rawInput": {"path": "x"}},
                [_Option("allow", "Allow", "allow_once")],
            )
            # Gemini CLI style: only a display title, no rawInput at all.
            gemini = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c3", "title": "inspect_event (Bread MCP Server)", "kind": "other"},
                [_Option("proceed_once", "Allow", "allow_once")],
            )
            # A shell command that merely mentions a tool name is not a tool call.
            shell = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c4", "title": "echo mcp__Bread__inspect_event", "kind": "execute",
                 "rawInput": {"command": "echo"}},
                [_Option("allow", "Allow", "allow_once")],
            )
            holder["allowed"], holder["denied"] = allowed, denied
            holder["gemini"], holder["shell"] = gemini, shell
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                await session.prompt("hello")
        self.assertEqual(holder["allowed"]["outcome"]["outcome"], "selected")
        self.assertEqual(holder["denied"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(holder["gemini"]["outcome"]["optionId"], "proceed_once")
        self.assertEqual(holder["shell"], {"outcome": {"outcome": "cancelled"}})

    async def test_repeated_unavailable_native_calls_do_not_cancel_authorized_work(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            for index in range(20):
                denied = await conn.client.request_permission(
                    conn.session_id,
                    {"toolCallId": f"denied-{index}", "title": "Write", "kind": "edit"},
                    [_Option("allow", "Allow", "allow_once")],
                )
                self.assertEqual(denied, {"outcome": {"outcome": "cancelled"}})
                with self.assertRaises(PolicyError):
                    await conn.client.read_text_file(conn.session_id, "unavailable-file")
            granted = await conn.tool("authorized-after-denials", "inspect_event",
                                      server="Bread", tool="inspect_event")
            self.assertEqual(granted["outcome"]["outcome"], "selected")
            await conn.say("Authorized work continued.")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                result = await session.prompt("inspect the authorized event")
                self.assertIsNone(session._state.policy_error)
                self.assertFalse(session._state.abort_event.is_set())
                self.assertNotIn("cancel", [name for name, _values in holder["connection"].calls])
        self.assertEqual(result.text, "Authorized work continued.")

    async def test_turn_limits_return_results_for_same_session_continuation(self) -> None:
        for reason in ("max_tokens", "max_turn_requests"):
            with self.subTest(reason=reason):
                holder: dict[str, Any] = {}

                async def script(conn: _Connection) -> Any:
                    await conn.say("Saved partial work")
                    return {"stopReason": reason}

                with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
                    async with self._session(holder) as session:
                        result = await session.prompt("continue the work")
                        self.assertEqual(result.stop_reason, reason)
                        self.assertEqual(result.final_text, "Saved partial work")
                        self.assertNotIn("cancel", [name for name, _ in holder["connection"].calls])

    async def test_compaction_requires_advertisement_and_keeps_maintenance_text_private(self) -> None:
        holder: dict[str, Any] = {}
        visible = []

        async def script(conn: _Connection) -> Any:
            await conn.say("PRIVATE_COMPACTION_SUMMARY")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                self.assertFalse(await session.compact_context())
                connection = holder["connection"]
                await connection.client.session_update("other-session", {
                    "sessionUpdate": "available_commands_update", "availableCommands": [{"name": "compact"}]})
                self.assertFalse(await session.compact_context())
                await connection.client.session_update("sess-1", {
                    "sessionUpdate": "available_commands_update", "availableCommands": [{"name": "compact"}]})
                session._state.on_delta = visible.append
                self.assertTrue(await session.compact_context())
                self.assertEqual(visible, [])
                await connection.client.session_update("sess-1", {
                    "sessionUpdate": "available_commands_update", "availableCommands": []})
                self.assertFalse(await session.compact_context())
                self.assertEqual(len([name for name, _ in connection.calls if name == "prompt"]), 1)

    async def test_unexpected_stop_reason_raises(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("partial")
            return {"stopReason": "refusal"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                with self.assertRaises(ConnectorError) as raised:
                    await session.prompt("hello")
        self.assertIn("refusal", str(raised.exception))
        self.assertIn(("cancel", {"session_id": "sess-1"}), holder["connection"].calls)

    async def test_missing_executable_is_reported(self) -> None:
        holder: dict[str, Any] = {}

        def spawn(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("acp-claude-code")

        sdk = _fake_sdk(lambda conn: None, holder)
        sdk.spawn_agent_process = spawn
        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: sdk):
            session = self._session(holder)
            with self.assertRaises(ConnectorError) as raised:
                await session.open()
        self.assertIn("acp-claude-code", str(raised.exception))

    async def test_session_new_timeout_retries_once_with_fresh_process(self) -> None:
        holder: dict[str, Any] = {}
        spawned: list[_Connection] = []
        original = _Connection.new_session

        async def flaky_new_session(conn: _Connection, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
            if conn not in spawned:
                spawned.append(conn)
            if len(spawned) == 1:
                await asyncio.Event().wait()  # first vendor process never answers
            return await original(conn, cwd, mcp_servers, **kwargs)

        async def script(conn: _Connection) -> Any:
            await conn.say("ready")
            return {"stopReason": "end_turn"}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_RETRY_TIMEOUT", 0.5),
            mock.patch.object(_Connection, "new_session", flaky_new_session),
        ):
            session = self._session(holder)
            async with session:
                result = await session.prompt("hello")
        self.assertEqual(len(spawned), 2, "the retry must use a fresh Agent process")
        self.assertIsNot(spawned[0], spawned[1])
        self.assertEqual(result.final_text, "ready")

    async def test_session_new_timeout_twice_names_the_stalled_side(self) -> None:
        holder: dict[str, Any] = {}

        async def stall(conn: _Connection, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
            await asyncio.Event().wait()

        async def healthy_route(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "seconds": 1.9, "tools": 144, "error": ""}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(lambda conn: None, holder)),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_RETRY_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_diagnose_local_mcp_route", healthy_route),
            mock.patch.object(_Connection, "new_session", stall),
        ):
            session = self._session(holder)
            with self.assertRaises(ConnectorError) as raised:
                await session.open()
        message = str(raised.exception)
        self.assertIn("ACP session/new timed out twice", message)
        self.assertIn("local Bread MCP route is healthy", message)
        self.assertIn("144 tools", message)
        self.assertTrue(holder["manager"].closed)

    async def test_session_new_timeout_reports_a_broken_local_route(self) -> None:
        holder: dict[str, Any] = {}

        async def stall(conn: _Connection, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
            await asyncio.Event().wait()

        async def broken_route(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "seconds": 0.4, "tools": 0,
                    "error": "ConfigurationError: Bread MCP Host is missing allowed tool(s): x"}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(lambda conn: None, holder)),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_RETRY_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_diagnose_local_mcp_route", broken_route),
            mock.patch.object(_Connection, "new_session", stall),
        ):
            session = self._session(holder)
            with self.assertRaises(ConnectorError) as raised:
                await session.open()
        self.assertIn("local Bread MCP route FAILED", str(raised.exception))
        self.assertIn("missing allowed tool(s): x", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
