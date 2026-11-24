"""DeepAgentsEngine projected through a scripted deepagents event stream."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def completed_general_review_for_existing_api_cases(monkeypatch):
    """Isolate existing API contracts; real review calls are covered separately."""
    from replaylab_agent.task_review import completion_review_state
    def reviewed(store, tracker):
        store.record_task_review("complete", [{"requirement": "Existing API test outcome", "status": "satisfied", "evidence": "Scripted test outcome"}], [], [])
        return completion_review_state(store, tracker)
    monkeypatch.setattr("replaylab_agent.deep_engine.completion_review_state", reviewed)

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from replaylab_agent import deep_engine
from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_diagnostics import close_logging
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.deep_engine import DeepAgentsEngine
from replaylab_agent.profiles import Profile


SKILL_MD = """---
name: renderdoc-explain-event
description: Analyze one RenderDoc event from capture evidence.
metadata:
  replaylab-category: event-pipeline
---
Use inspect_event and cite exact values.
"""

SHADER_MD = """---
name: renderdoc-shader-workbench
description: Reconstruct one capture-bound shader.
metadata:
  replaylab-category: shader-workbench
---
Inspect the shader and choose useful tools for the requested outcome.
"""


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_task_cleanup_never_infers_replay_rollback_from_terminal_status(
    terminal_status: str,
) -> None:
    calls: list[dict[str, bool]] = []

    async def release(arguments: dict[str, bool]) -> None:
        calls.append(arguments)

    tool = types.SimpleNamespace(ainvoke=release)
    asyncio.run(deep_engine._maybe_release_task_shader_occupancy(
        {"release_task_shader_occupancy": tool}, terminal_status=terminal_status,
    ))
    assert calls == [{"rollback_incomplete_replacement": False}]

CORE_MD = """---
name: replaylab-core
description: Standing Host evidence rules.
metadata:
  replaylab-category: core
  replaylab-always: "true"
---
Copy identifiers exactly. Mark observed and inferred facts.
"""


def _context() -> dict[str, Any]:
    return {
        "schema": "renderdoc-ultra-current-context/v1",
        "capture": {
            "loaded": True,
            "path": "D:/capture.rdc",
            "api": "D3D12",
            "captureSessionId": "a" * 32,
            "captureId": "b" * 64,
        },
        "events": {"currentEventId": 13353, "selectedEventId": 13353},
        "action": {"eventId": 13353, "name": "DrawIndexed"},
        "shaders": {"bound": [], "focused": {
            "resourceId": "1000000000000013353", "stage": "PS",
            "entryPoint": "MainPS", "rawSha256": "c" * 64,
        }},
        "texture": {"available": False},
    }


def _envelope(text: str, history: list[tuple[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "schema": "replaylab-conversation/v5",
            "messages": [
                {"role": role, "content": content}
                for role, content in (history or [])
            ],
            "request": {"role": "user", "content": text},
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_deep_request_conversation_identity_reaches_route_and_host_state(
    tmp_path, monkeypatch
) -> None:
    event_root = tmp_path / "events"
    host = tmp_path / "BreadMCP.exe"
    host.write_bytes(b"host")
    observed: dict[str, Any] = {}
    real_build_route = deep_engine.build_native_mcp_route

    def capture_route(context, **kwargs):
        assert context.conversation_id == "conversation-deep-api"
        # The API process consumes the full definitions privately and builds its
        # own lazy model-facing gateway; asking the proxy for its native-vendor
        # compact view here would leave no Host schemas to dispatch.
        assert kwargs["skill_roots"] == ()
        assert kwargs["full_host_catalog"] is True
        route = real_build_route(context, **kwargs)
        assert route is not None
        assert "--full-host-catalog" in route.args
        observed["route"] = route
        observed["task_env"] = dict(route.env)
        # This contract test only needs admission and environment projection; avoiding
        # the subprocess keeps it independent of an installed RenderDoc MCP runtime.
        return None

    monkeypatch.setattr(deep_engine, "build_native_mcp_route", capture_route)
    config = OrchestratorConfig(
        event_root=event_root,
        log_root=tmp_path / "logs",
        workspace_base=tmp_path / "workspaces",
        skill_roots=(tmp_path / "skills",),
    )
    request = RunRequest(
        prompt="organize this shader",
        profile=Profile(id="model-api", preset="openai", model="gpt-x"),
        task_id="deep-conversation-route",
        context=_context(),
        instance_id="instance-1",
        conversation_id="conversation-deep-api",
        mcp_executable=host,
    )

    session = deep_engine.BreadMCPTools(request, config, event_root)
    async with session as tools:
        assert tools == {}
        physical_root = session.workspace_root
        assert physical_root is not None and physical_root.is_dir()
        assert observed["task_env"]["REPLAYLAB_AGENT_WORKSPACE_ROOT"] == str(physical_root)

    assert session.workspace_root is None
    assert not physical_root.exists()

    assert observed["route"].env["REPLAYLAB_CONVERSATION_ID"] == "conversation-deep-api"
    assert observed["task_env"]["REPLAYLAB_CONVERSATION_ID"] == "conversation-deep-api"


class Msg:
    def __init__(self, content: Any, tool_calls: list[Any] | None = None, **extra: Any) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.additional_kwargs = extra


class FakeDeepagents:
    """Record create_deep_agent inputs and replay one scripted event stream."""

    class SubAgent:
        __annotations__ = {
            "name": str,
            "description": str,
            "system_prompt": str,
            "tools": list,
        }

    def __init__(self, script: Any) -> None:
        self.script = script
        self.created: list[dict[str, Any]] = []

    def create_deep_agent(
        self,
        *,
        model: Any,
        tools: list[Any],
        subagents: list[dict[str, Any]],
        system_prompt: str,
        backend: Any,
        checkpointer: Any = None,
        middleware: Any = None,
    ) -> Any:
        record = {
            "model": model,
            "tools": tools,
            "subagents": subagents,
            "system_prompt": system_prompt,
            "backend": backend,
            "checkpointer": checkpointer,
            "middleware": middleware,
        }
        self.created.append(record)
        script = self.script

        class Agent:
            async def astream_events(self, payload: Any, *, config: Any, version: str):
                record["payload"] = payload
                record["config"] = config
                record["version"] = version
                for event in await script(record):
                    # A framework event alone is not Host evidence. Execute
                    # the fixture Host tool so its task ledger records reality.
                    if event["event"] == "on_tool_start" and event["name"] != "task":
                        known = {tool.name: tool for subagent in subagents for tool in subagent["tools"]}
                        actual = known.get(event["name"])
                        if actual is not None:
                            await actual.coroutine(**event.get("data", {}).get("input", {}))
                        elif "invoke_bread_tool" in known:
                            await known["list_bread_capabilities"].coroutine(
                                tool=event["name"]
                            )
                            await known["invoke_bread_tool"].coroutine(
                                name=event["name"],
                                arguments=event.get("data", {}).get("input", {}),
                            )
                    yield event

        return Agent()


def ev(
    kind: str,
    name: str,
    run_id: str,
    *,
    parents: list[str] = (),
    data: Any = None,
) -> dict[str, Any]:
    return {
        "event": kind,
        "name": name,
        "run_id": run_id,
        "parent_ids": list(parents),
        "data": data or {},
    }


class FakeToolSession:
    def __init__(self, holder: dict[str, Any], request: Any, event_root: Path) -> None:
        self.holder = holder
        del request, event_root

    async def __aenter__(self) -> dict[str, Any]:
        async def inspect_event(event_id: int) -> str:
            """Inspect one exact RenderDoc event."""
            if event_id < 1:
                return "ERROR: event_id must be positive"
            return json.dumps({"eventId": event_id})

        async def inspect_shader(event_id: int = 1) -> str:
            """Inspect one capture-bound shader."""
            return json.dumps({"eventId": event_id})

        async def prepare_shader_workspace(workspace_path: str = "") -> str:
            """Prepare one shader workspace."""
            return json.dumps({"workspace": workspace_path})

        async def apply_shader_workspace_candidate(workspace_path: str = "") -> str:
            """Apply one reviewed shader workspace."""
            return json.dumps({"workspace": workspace_path})

        tools = {
            function.__name__: deep_engine._langchain_tool(function.__name__, function)
            for function in (
                inspect_event,
                inspect_shader,
                prepare_shader_workspace,
                apply_shader_workspace_candidate,
            )
        }
        self.holder["tools"] = tools
        return tools

    async def __aexit__(self, *_args: Any) -> None:
        self.holder["closed"] = True


# unittest's isolated loop keeps the scripted async generators deterministic on Windows.
class DeepEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        skills = root / "skills"
        for name, text in (
            ("renderdoc-explain-event", SKILL_MD),
            ("renderdoc-shader-workbench", SHADER_MD),
            ("replaylab-core", CORE_MD),
        ):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(text, encoding="utf-8")
        self.catalog = CatalogView.load(skill_roots=[skills])
        self.events_root = root / "ai-tasks"
        self.config = OrchestratorConfig(
            event_root=self.events_root,
            workspace_base=root / "workspaces",
            log_root=root / "logs",
            heartbeat_seconds=0.05,
        )
        self.tool_holder: dict[str, Any] = {}

    def tearDown(self) -> None:
        close_logging()
        self.tmp.cleanup()

    def _engine(self, script: Any) -> tuple[DeepAgentsEngine, FakeDeepagents]:
        fake = FakeDeepagents(script)
        engine = DeepAgentsEngine(
            catalog=self.catalog,
            config=self.config,
            deepagents_module=fake,
            tool_session_factory=lambda request, config, event_root: FakeToolSession(self.tool_holder, request, event_root),
            model_factory=lambda profile, model: f"model:{model or profile.model}",
        )
        return engine, fake

    def _request(self, text: str, **kwargs: Any) -> RunRequest:
        return RunRequest(
            prompt=_envelope(text),
            profile=Profile(id="m", preset="openai", model="gpt-x"),
            task_id=kwargs.pop("task_id", "t-deep"),
            context=_context(),
            instance_id="inst",
            **kwargs,
        )

    def test_api_prompt_carries_target_without_replaying_old_shader_checkpoint(self) -> None:
        run = deep_engine._DeepRun(
            self._engine(None)[0],
            self._request(
                "Explain the selected shader.",
                intent={"kind": "reverse-edit-selected-shader", "arguments": {}},
            ),
            mock.Mock(),
            event_root=self.events_root,
        )
        prompt, _language = run.user_prompt()
        self.assertIn("Selected Shader action", str(prompt))
        self.assertIn("MainPS", str(prompt))
        self.assertNotIn("report_task_result", str(prompt))

    def test_api_explicit_skill_is_guidance_without_forced_delegation(self) -> None:
        engine, fake = self._engine(None)
        run = deep_engine._DeepRun(
            engine,
            self._request(
                "Explain the selected shader.",
                skill_name="renderdoc-shader-workbench",
            ),
            mock.Mock(),
            event_root=self.events_root,
        )
        run.build_agent()
        self.addCleanup(run._cleanup_workspace)
        system_prompt = str(fake.created[-1]["system_prompt"])
        self.assertIn("## Selected Skill: renderdoc-shader-workbench", system_prompt)
        self.assertIn("choose useful tools", system_prompt)
        user_prompt, _language = run.user_prompt()
        self.assertNotIn("use that sub-agent", str(user_prompt))

    def test_subagent_effects_preserve_configured_replay_and_respect_ceiling(self) -> None:
        engine, unused_fake = self._engine(None)
        self.assertIn("replay", engine.subagent_effects())
        self.config.allowed_effects = frozenset({"workspace", "project"})
        self.assertEqual(engine.subagent_effects(), ("project", "workspace"))
        self.config.allowed_effects = frozenset()
        self.assertEqual(engine.subagent_effects(), ())

    async def test_lazy_invoker_checks_parameterized_effects_per_call(self) -> None:
        engine, unused_fake = self._engine(None)
        run = deep_engine._DeepRun(
            engine,
            self._request("classify passes"),
            mock.Mock(),
            event_root=self.events_root,
        )
        calls: list[dict[str, Any]] = []

        async def classify_current_frame_passes(
            publish_to_ui: bool = True,
            persist_project: bool = False,
        ) -> str:
            """Classify frame passes with optional publication effects."""

            calls.append({
                "publish_to_ui": publish_to_ui,
                "persist_project": persist_project,
            })
            return "ok"

        name = "classify_current_frame_passes"
        run.renderdoc_tool_map[name] = deep_engine._langchain_tool(
            name, classify_current_frame_passes
        )
        self.assertFalse(run.skill_gateway().tool_contract_loaded(name))
        invoke = run.invoke_tool(("ui",)).coroutine

        self.assertEqual(
            await invoke(
                name=name,
                arguments={"publish_to_ui": False, "persist_project": False},
            ),
            "ok",
        )
        self.assertEqual(
            await invoke(
                name=name,
                arguments={"publish_to_ui": True, "persist_project": False},
            ),
            "ok",
        )
        denied = await invoke(
            name=name,
            arguments={"publish_to_ui": False, "persist_project": True},
        )
        self.assertIn("Host tool needs effect grant ['project']", denied)
        for coercible in ("true", 1):
            invalid = await invoke(
                name=name,
                arguments={
                    "publish_to_ui": False,
                    "persist_project": coercible,
                },
            )
            self.assertIn("must be a JSON boolean", invalid)
        self.assertEqual(len(calls), 2)

    async def test_core_host_tool_is_direct_and_retains_its_schema(self) -> None:
        engine, unused_fake = self._engine(None)
        run = deep_engine._DeepRun(
            engine, self._request("inspect EID 7"), mock.Mock(),
            event_root=self.events_root,
        )

        async def inspect_event(event_id: int) -> str:
            """Inspect one exact event."""
            return json.dumps({"eventId": event_id})

        run.renderdoc_tool_map["inspect_event"] = deep_engine._langchain_tool(
            "inspect_event", inspect_event
        )
        direct = {tool.name: tool for tool in run.direct_host_tools(())}
        self.assertIn("inspect_event", direct)
        self.assertIn("event_id", deep_engine._tool_input_schema(direct["inspect_event"])["properties"])
        self.assertEqual(
            json.loads(await direct["inspect_event"].coroutine(event_id=7)),
            {"eventId": 7},
        )
        self.assertTrue(run.skill_gateway().tool_contract_loaded("inspect_event"))

    async def test_main_can_invoke_configured_mcp_without_delegation_or_schema_flood(self) -> None:
        engine, fake = self._engine(None)
        run = deep_engine._DeepRun(
            engine, self._request("Inspect a matched Unreal asset"), mock.Mock(),
            event_root=self.events_root,
        )
        calls: list[int] = []

        async def preview_asset(limit: int) -> str:
            """Preview a matched Unreal asset with a bounded object limit."""

            calls.append(limit)
            return json.dumps({"objectCount": limit})

        name = "uex-mcp__preview_asset"
        run.renderdoc_tool_map[name] = deep_engine._langchain_tool(name, preview_asset)
        run.user_mcp_tool_names = frozenset({name})
        run.build_agent()
        self.addCleanup(run._cleanup_workspace)
        # Configured tool schemas stay on demand for the main Agent. It can
        # call its single existing dispatcher, without task()/delegation.
        main_tools = {tool.name: tool for tool in fake.created[-1]["tools"]}
        self.assertNotIn(name, main_tools)
        directory = main_tools["list_bread_capabilities"].coroutine
        contract = json.loads(await directory(tool=name))["tool"]
        self.assertEqual(contract["authority"], "user-configured-mcp-server")
        self.assertIn("limit", contract["inputSchema"]["properties"])
        self.assertNotIn("effects", contract)
        found = json.loads(await directory(query="Unreal asset"))["tools"]
        self.assertIn(name, [tool["name"] for tool in found])
        self.assertNotIn("inputSchema", next(tool for tool in found if tool["name"] == name))
        # The already-enabled external server grant remains independent of
        # Bread's effect classes, just as on the native and child surfaces.
        invoke = run.invoke_tool(()).coroutine
        self.assertEqual(json.loads(await invoke(name=name, arguments={"limit": 7})), {"objectCount": 7})
        self.assertIn("must be integer", await invoke(name=name, arguments={"limit": "7"}))
        self.assertIn("no granted", await invoke(name="disabled-mcp__preview_asset", arguments={"limit": 7}))
        self.assertEqual(calls, [7])
        self.assertEqual(run.children, [])

    async def test_capability_routes_follow_each_actors_actual_direct_tools(self) -> None:
        engine, unused_fake = self._engine(None)
        run = deep_engine._DeepRun(
            engine, self._request("organize a Shader"), mock.Mock(),
            event_root=self.events_root,
        )
        calls: list[str] = []

        async def compile_shader_workspace(workspace_path: str) -> str:
            """Compile the requested workspace."""
            calls.append(workspace_path)
            return json.dumps({"workspacePath": workspace_path})

        name = "compile_shader_workspace"
        run.renderdoc_tool_map[name] = deep_engine._langchain_tool(name, compile_shader_workspace)
        main = {tool.name: tool for tool in run.local_skill_tools(("workspace",))}
        child = {tool.name: tool for tool in run.local_skill_tools(())}
        self.assertIn(name, main)
        self.assertNotIn(name, child)
        # Ordinary execution needs no directory call before the typed tool.
        self.assertEqual(json.loads(await main[name].coroutine(workspace_path="owned")),
                         {"workspacePath": "owned"})
        main_contract = json.loads(await main["list_bread_capabilities"].coroutine(tool=name))
        child_contract = json.loads(await child["list_bread_capabilities"].coroutine(tool=name))
        self.assertTrue(main_contract["directlyAvailable"])
        self.assertEqual(main_contract["invokeWith"], name)
        self.assertFalse(child_contract["directlyAvailable"])
        self.assertEqual(child_contract["invokeWith"], "invoke_bread_tool")
        self.assertEqual(main_contract["tool"]["inputSchema"], child_contract["tool"]["inputSchema"])
        denied = await child["invoke_bread_tool"].coroutine(name=name, arguments={"workspace_path": "blocked"})
        self.assertIn("effect grant", denied)
        self.assertEqual(calls, ["owned"])

    def _log(self, task_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (self.events_root / f"{task_id}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    async def test_general_subagent_uses_same_granted_tools_without_preloading_skills(self) -> None:
        self.config.allowed_effects = frozenset({"workspace", "project", "ui"})
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                ev("on_chat_model_stream", "m", "r1", data={"chunk": Msg("直接回答")}),
                ev("on_chat_model_end", "m", "r1", data={"output": Msg("直接回答")}),
            ]

        engine, fake = self._engine(script)
        result = await engine.run(self._request("你是什么模型"))

        self.assertEqual(result.terminal_status, "completed")
        created = fake.created[0]
        self.assertEqual(created["model"], "model:gpt-x")
        self.assertEqual(
            [tool.name for tool in created["tools"]],
            ["inspect_event", "inspect_shader", "prepare_shader_workspace", "list_bread_capabilities",
             "load_bread_skill", "invoke_bread_tool", "record_task_review", "ask_user"],
        )
        names = [item["name"] for item in created["subagents"]]
        self.assertEqual(names, ["general-purpose"])
        general = created["subagents"][0]
        self.assertIn("Granted effects: project, ui, workspace", general["system_prompt"])
        self.assertNotIn("Standing rules", general["system_prompt"])
        self.assertNotIn("## Loaded Skill", general["system_prompt"])
        self.assertEqual(
            {tool.name for tool in general["tools"]},
            {"inspect_event", "inspect_shader", "prepare_shader_workspace", "list_bread_capabilities",
             "load_bread_skill", "invoke_bread_tool"},
        )
        self.assertNotIn("apply_shader_workspace_candidate", {tool.name for tool in general["tools"]})
        self.assertNotIn("report_task_result", {tool.name for tool in general["tools"]})
        # Every sub-agent also carries the three gateway tools its prompt tells it
        # to use when a task crosses into another domain -- exactly once. The task
        # proxy advertises them too, and a provider rejects a request whose tool
        # array repeats a function name.
        for agent in (*created["subagents"], {"tools": created["tools"]}):
            names = [tool.name for tool in agent["tools"]]
            self.assertEqual(len(names), len(set(names)), names)
        self.assertEqual(created["version"], "v2")
        self.assertTrue(self.tool_holder["closed"])

    async def test_no_capture_means_no_mcp_session_or_subagents(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("hello")})]

        engine, fake = self._engine(script)
        request = self._request("hi")
        request.instance_id = ""
        request.context = {}
        result = await engine.run(request)

        self.assertEqual(result.text, "hello")
        general = fake.created[0]["subagents"]
        self.assertEqual([item["name"] for item in general], ["general-purpose"])
        self.assertEqual(general[0]["tools"], [])
        self.assertNotIn("Standing rules", general[0]["system_prompt"])
        self.assertNotIn("tools", self.tool_holder)






    async def test_event_mapping_tracks_children_tools_reasoning_and_final_text(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                ev("on_tool_start", "task", "task-1", data={"input": {"description": "Inspect", "subagent_type": "general-purpose"}}),
                ev("on_chat_model_stream", "m", "r2", parents=["task-1"], data={"chunk": Msg([{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Looking"}])}),
                ev("on_tool_start", "inspect_event", "tool-1", parents=["task-1", "r2"], data={"input": {"event_id": 13353}}),
                ev("on_tool_end", "inspect_event", "tool-1", parents=["task-1", "r2"], data={"output": Msg('{"eventId":13353}')}),
                ev("on_tool_end", "task", "task-1", data={"output": Msg("Findings\nSTATUS: DONE")}),
                ev("on_chat_model_end", "m", "r3", data={"output": Msg("The draw renders 1200 vertices.")}),
            ]

        engine, _fake = self._engine(script)
        result = await engine.run(self._request("解释事件", task_id="t-child"))

        self.assertEqual(result.text, "The draw renders 1200 vertices.")
        self.assertEqual(len(result.children), 1)
        child = result.children[0]
        self.assertEqual(child.agent_id, "general-purpose")
        self.assertEqual(child.tools_used, ["inspect_event"])
        kinds = [item["kind"] for item in self._log("t-child")]
        self.assertIn("reasoning-delta", kinds)
        self.assertIn("tool-started", kinds)
        self.assertIn("tool-completed", kinds)

    async def test_progress_before_last_tool_is_not_a_final_answer(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                ev("on_chat_model_stream", "m", "r1", data={"chunk": Msg("I will inspect it.")}),
                ev("on_chat_model_end", "m", "r1", data={"output": Msg("I will inspect it.")}),
                ev("on_tool_start", "inspect_event", "tool-1", data={"input": {"event_id": 13353}}),
                ev("on_tool_end", "inspect_event", "tool-1", data={"output": Msg('{"eventId":13353}')}),
            ]

        engine, _fake = self._engine(script)
        result = await engine.run(self._request("Inspect the event", task_id="t-no-final-answer"))

        self.assertEqual(result.terminal_status, "partial")
        self.assertEqual(result.text, "The main Agent returned no text answer.")
        self.assertTrue(any(event["text"] == "I will inspect it." for event in self._log("t-no-final-answer")))

    async def test_compaction_text_does_not_leak_or_replace_main_and_child_answers(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            def summary(kind, run, *, parents=()):
                payload = {"chunk" if kind.endswith("stream") else "output": Msg("INTERNAL CONTEXT: source paths and task notes")}
                return {**ev(kind, "m", run, parents=parents, data=payload),
                        "metadata": {"lc_source": "summarization"}}
            return [
                ev("on_tool_start", "task", "task-1", data={"input": {"description": "Inspect", "subagent_type": "general-purpose"}}),
                summary("on_chat_model_stream", "child-summary", parents=["task-1"]),
                summary("on_chat_model_end", "child-summary", parents=["task-1"]),
                ev("on_chat_model_stream", "m", "child-model", parents=["task-1"], data={"chunk": Msg("Useful child progress")}),
                ev("on_tool_end", "task", "task-1", data={"output": Msg("Useful finding")}),
                ev("on_chat_model_stream", "m", "answer", data={"chunk": Msg("Public explanation")}),
                ev("on_chat_model_end", "m", "answer", data={"output": Msg("Public explanation")}),
                summary("on_chat_model_stream", "main-summary"),
                summary("on_chat_model_end", "main-summary"),
            ]

        engine, _fake = self._engine(script)
        result = await engine.run(self._request("Explain", task_id="t-summary-private"))
        self.assertEqual(result.text, "Public explanation")
        log = json.dumps(self._log("t-summary-private"))
        self.assertNotIn("INTERNAL CONTEXT", log)
        self.assertIn("Useful child progress", log)
        self.assertIn("Public explanation", log)

    async def test_provider_output_limit_does_not_report_completed(self) -> None:
        for metadata in (
            {"finish_reason": "length"},
            {"stop_reason": "max_tokens"},
            {"finish_reason": "MAX_TOKENS"},
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        ):
            with self.subTest(metadata=metadata):
                async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
                    message = Msg("The first part of the answer")
                    message.response_metadata = metadata
                    return [ev("on_chat_model_end", "m", "r1", data={"output": message})]

                engine, _fake = self._engine(script)
                result = await engine.run(self._request("Explain the shader", task_id="t-output-limit"))

                self.assertEqual(result.terminal_status, "partial")
                self.assertIn("The first part", result.text)
                self.assertIn("output limit", result.text)

    async def test_main_answer_is_not_rejected_after_optional_child_failure(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                ev("on_tool_start", "task", "task-1", data={"input": {
                    "description": "Try independent evidence", "subagent_type": "general-purpose",
                }}),
                ev("on_tool_error", "task", "task-1", data={"error": "unavailable"}),
                ev("on_chat_model_end", "m", "r1", data={"output": Msg(
                    "I used the available evidence and explain the remaining limitation."
                )}),
            ]

        engine, _fake = self._engine(script)
        result = await engine.run(self._request("Explain EID 13353", task_id="t-child-failed"))

        self.assertEqual(result.terminal_status, "completed")
        self.assertEqual(result.children[0].status, "failed")
        self.assertIn("unavailable", result.children[0].error)
        self.assertIn("remaining limitation", result.text)


    async def test_a_cancelled_run_still_removes_its_turn_workspace(self) -> None:
        """A cancel is a BaseException; the cleanup must not depend on it being one."""

        import contextlib as _contextlib
        from replaylab_agent.deep_engine import BreadMCPTools

        removed: list[Any] = []

        class _Stack:
            async def aclose(self) -> None:
                raise asyncio.CancelledError()

        session = BreadMCPTools.__new__(BreadMCPTools)
        session._stack = _Stack()
        session._output_store = types.SimpleNamespace(close=lambda: removed.append("owned-output"))
        session._turn_root = "turn-root"
        session._context = types.SimpleNamespace(
            cleanup_turn_workspace=lambda root: removed.append(root))

        with _contextlib.suppress(asyncio.CancelledError):
            await session.__aexit__(None, None, None)
        self.assertEqual(removed, ["turn-root", "owned-output"])

    async def test_capture_free_cancel_removes_framework_physical_workspace(self) -> None:
        owned_roots: list[Path] = []

        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            root = record["backend"].cwd
            self.assertTrue(root.is_dir())
            owned_roots.append(root)
            raise asyncio.CancelledError()

        engine, _fake = self._engine(script)
        request = self._request("Make a temporary note", task_id="cancel-api-workspace")
        request.instance_id = ""
        request.context = {}
        with self.assertRaises(asyncio.CancelledError):
            await engine.run(request)
        self.assertEqual(len(owned_roots), 1)
        self.assertFalse(owned_roots[0].parent.exists())
        self.assertNotIn("tools", self.tool_holder)

    async def test_ask_user_ends_with_needs_user_input(self) -> None:
        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            ask = next(tool for tool in record["tools"] if tool.name == "ask_user").coroutine
            record["first"] = await ask(
                question="Which shader?",
                options=[{"id": "vs", "label": "MainVS"}, {"id": "ps", "label": "MainPS"}],
            )
            record["second"] = await ask(
                question="Again?",
                options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            )
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("pick")})]

        engine, fake = self._engine(script)
        result = await engine.run(self._request("decompile", task_id="t-ask"))

        self.assertIn("Question recorded", fake.created[0]["first"])
        self.assertTrue(fake.created[0]["second"].startswith("ERROR:"))
        self.assertEqual(result.terminal_status, "needs-user-input")
        self.assertEqual(result.text, "Which shader?")

    async def test_ask_user_accepts_free_text_question_without_fake_choices(self) -> None:
        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            ask = next(tool for tool in record["tools"] if tool.name == "ask_user")
            schema = deep_engine._tool_input_schema(ask)
            self.assertEqual(schema["required"], ["question"])
            record["answer"] = await ask.coroutine(question="Which project directory?")
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("Please answer.")})]

        engine, fake = self._engine(script)
        result = await engine.run(self._request("Use my project", task_id="t-ask-free-text"))

        self.assertIn("Question recorded", fake.created[0]["answer"])
        self.assertEqual(result.terminal_status, "needs-user-input")
        self.assertEqual(result.clarification.options, [])
        self.assertTrue(result.clarification.event_payload()["allowOther"])


    async def test_clarification_answer_and_intent_reach_prompt(self) -> None:
        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("ok")})]

        engine, fake = self._engine(script)
        envelope = json.loads(_envelope("continue", [("user", "decompile"), ("assistant", "Which shader?")]))
        envelope["clarificationResponse"] = {
            "clarificationId": "q-1",
            "optionIds": ["ps"],
            "otherText": "",
        }
        request = self._request(
            "x",
            task_id="t-answer",
            intent={"kind": "analyze-shader", "arguments": {}},
            skill_name="renderdoc-shader-workbench",
        )
        request.prompt = json.dumps(envelope, ensure_ascii=False)
        await engine.run(request)
        prompt = fake.created[0]["payload"]["messages"][0]["content"]
        self.assertIn("replaylab-provider-conversation/v1", prompt)
        self.assertIn("decompile", prompt)
        self.assertIn("selected option(s) = [ps]", prompt)
        self.assertIn("analyze-shader", prompt)

    async def test_framework_failure_and_timeout_are_failed_runs(self) -> None:
        async def fail(_record: dict[str, Any]) -> list[dict[str, Any]]:
            raise RuntimeError("model refused: invalid api key")

        engine, _fake = self._engine(fail)
        result = await engine.run(self._request("hi", task_id="t-err"))
        self.assertEqual(result.terminal_status, "failed")
        self.assertIn("invalid api key", result.error)

        async def hang(_record: dict[str, Any]) -> list[dict[str, Any]]:
            await asyncio.sleep(5)
            return []

        self.config.main_timeout = 0.05
        engine, _fake = self._engine(hang)
        result = await engine.run(self._request("hi", task_id="t-timeout"))
        self.assertEqual(result.terminal_status, "failed")
        self.assertIn("Model API task exceeded configured runtime limit", result.error)

    async def test_custom_chat_retries_responses_only_on_provider_tool_reasoning_error(self) -> None:
        class BadRequest(RuntimeError):
            status_code = 400

        async def script(record: dict[str, Any]) -> list[dict[str, Any]]:
            if record["model"] == "chat":
                raise BadRequest(
                    "Function tools with reasoning_effort are not supported "
                    "for this model in /v1/chat/completions. "
                    "Use /v1/responses or set reasoning_effort to 'none'."
                )
            return [ev("on_chat_model_end", "m", "r1", data={"output": Msg("done")})]

        engine, fake = self._engine(script)
        engine._model_factory = lambda profile, _model: profile.options.get("api", "chat")
        request = self._request("hi", task_id="t-chat-responses-fallback")
        request.profile = Profile(
            id="custom", preset="custom-openai", model="model",
            base_url="https://api.example.test/v1",
        )
        result = await engine.run(request)
        self.assertEqual(result.terminal_status, "completed")
        self.assertEqual(result.text, "done")
        self.assertEqual([item["model"] for item in fake.created], ["chat", "responses"])
        self.assertIsNot(fake.created[0]["checkpointer"], fake.created[1]["checkpointer"])
        self.assertEqual(request.profile.options, {})

        # An explicit Chat choice belongs to the user, not the compatibility retry.
        explicit = self._request("hi", task_id="t-chat-explicit")
        explicit.profile = Profile(
            id="custom", preset="custom-openai", model="model",
            base_url="https://api.example.test/v1", options={"api": "chat"},
        )
        result = await engine.run(explicit)
        self.assertEqual(result.terminal_status, "failed")
        self.assertEqual([item["model"] for item in fake.created][-1], "chat")

    async def test_chat_compatibility_retry_never_replays_after_a_tool_event(self) -> None:
        class BadRequest(RuntimeError):
            status_code = 400

        async def script(_record: dict[str, Any]) -> list[dict[str, Any]]:
            raise BadRequest(
                "Function tools with reasoning_effort are not supported "
                "in /v1/chat/completions; use /v1/responses."
            )

        engine, fake = self._engine(script)
        request = self._request("hi", task_id="t-chat-no-replay")
        request.profile = Profile(
            id="custom", preset="custom-openai", model="model",
            base_url="https://api.example.test/v1",
        )
        run = deep_engine._DeepRun(engine, request, mock.Mock(), event_root=self.events_root)
        run._main_tool_calls = 1
        with self.assertRaisesRegex(Exception, "deepagents run failed"):
            try:
                await run._execute_with_tools()
            finally:
                run._cleanup_workspace()
        self.assertEqual(len(fake.created), 1)

    def test_missing_dependency_message_matches_required_runtime(self) -> None:
        with mock.patch.dict(sys.modules, {"deepagents": None}):
            with self.assertRaises(Exception) as raised:
                deep_engine.import_deepagents()
        self.assertIn("required deepagents runtime is missing", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
