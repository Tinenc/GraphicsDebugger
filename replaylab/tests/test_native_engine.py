"""Vendor-native ACP/Codex execution leaves method selection to the model."""

from __future__ import annotations

import json
import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest

from replaylab_agent.acp_session import PromptResult
from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.agent_diagnostics import close_logging
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.native_mcp import NativeMCPRoute, build_native_mcp_route, native_codex_argv
from replaylab_agent.policy import AGENT_LOCAL_TOOLS, CORE_DIRECT_TOOLS
from replaylab_agent.profiles import Profile


@pytest.fixture(autouse=True)
def scripted_sessions_do_not_require_installed_vendor_cli(monkeypatch):
    """Session factories are local fakes; never resolve a developer's real CLI."""
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["offline-agent"])


@pytest.fixture(autouse=True)
def completed_general_review_for_existing_protocol_cases(monkeypatch):
    """These cases isolate transport/Shader rules; general review has its own integration suite."""
    from replaylab_agent.task_review import completion_review_state
    def reviewed(store, tracker):
        store.record_task_review("complete", [{"requirement": "Existing protocol test outcome", "status": "satisfied", "evidence": "Scripted test outcome"}], [], [])
        return completion_review_state(store, tracker)
    monkeypatch.setattr("replaylab_agent.native_engine.completion_review_state", reviewed)


SKILL_MD = """---
name: renderdoc-explain-event
description: Analyze one RenderDoc event from capture evidence.
metadata:
  replaylab-category: event-pipeline
---
Use inspect_event and cite exact values.
"""

CORE_MD = """---
name: replaylab-core
description: Standing Host evidence rules.
metadata:
  replaylab-category: core
  replaylab-always: "true"
---
Copy identifiers exactly and distinguish observations from inference.
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["acp", "codex"])
@pytest.mark.parametrize("ending", ["complete", "stalled", "blocked", "unfinished", "unfinished_complete"])
async def test_shader_self_review_reuses_native_session_and_honest_terminal_state(tmp_path, monkeypatch, protocol, ending):
    holder = {"sessions": 0, "prompts": []}
    row = {"workspacePath": "shader", "organizationId": "org", "sourceSha256": "a" * 64,
           "changed": True, "saved": True, "fresh": True, "review": {}}
    if ending == "unfinished_complete":
        row["review"] = {"verdict": "complete"}

    class Session:
        def __init__(self, *args, **kwargs):
            holder["sessions"] += 1
            self.cwd = Path(kwargs["cwd"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def close(self):
            pass

        async def prompt(self, text, **kwargs):
            holder["prompts"].append(text)
            if ending == "stalled":
                (self.cwd / "unfinished.hlsl").write_text("float4 RecoverableColor;", encoding="utf-8")
            if len(holder["prompts"]) == 2:
                assert "explicit Agent self-review" in text
                assert "Loaded Skill" not in text
                if ending == "complete":
                    row["review"] = {"verdict": "complete"}
                elif ending == "blocked":
                    row["review"] = {"verdict": "blocked", "blocker": "Required function body is unavailable"}
            if ending.startswith("unfinished"):
                kwargs["on_timeline"]({"activityId": "pending-write", "activityKind": "toolCall", "status": "running"})
            return PromptResult("Done", ["Done"], "end_turn", 1)

        async def run(self, text, **kwargs):
            assert kwargs["keep_thread"] is True
            return await self.prompt(text, **kwargs)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr("replaylab_agent.native_engine.review_state", lambda store: {"targets": [row]})
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["offline-agent", "app-server"])
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs", workspace_base=tmp_path / "turns"),
        acp_session_factory=Session, codex_connector_factory=Session)
    try:
        result = await engine.run(RunRequest(prompt="Organize this Shader", task_id="review-native",
            profile=_profile("codex" if protocol == "codex" else "claude-code-acp")))
    finally:
        close_logging()
    assert holder["sessions"] == 1
    assert len(holder["prompts"]) == (1 if ending.startswith("unfinished") else 3 if ending == "blocked" else 2)
    assert result.terminal_status == ("completed" if ending == "complete" else "partial"), result.error
    if ending != "complete":
        assert result.text != "Done"
    if ending == "blocked":
        assert "Required function body" in result.text
    if ending == "stalled":
        assert "draft recovery" in result.text
        assert list((tmp_path / "turns" / "recovered-shader-drafts").glob("*/manifest.json"))


def _catalog(tmp_path: Path) -> CatalogView:
    skills = tmp_path / "skills"
    for name, content in (
        ("renderdoc-explain-event", SKILL_MD),
        ("replaylab-core", CORE_MD),
    ):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return CatalogView.load(skill_roots=[skills])


def _profile(preset: str = "claude-code-acp") -> Profile:
    options = {"allow_unsandboxed_agent": True} if preset.endswith("-acp") else {}
    return Profile(id="native", preset=preset, title="Native", options=options)


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
        "events": {"currentEventId": 42, "selectedEventId": 42},
        "action": {"eventId": 42, "name": "DrawIndexed"},
        "shaders": {"bound": [], "focused": {
            "resourceId": "1000000000000000042", "stage": "PS",
            "entryPoint": "MainPS", "rawSha256": "c" * 64,
        }},
        "texture": {"available": False},
    }


def _route(workspace: Path, event_root: Path, task_id: str) -> NativeMCPRoute:
    return NativeMCPRoute(
        command="replaylab-ai",
        args=("-m", "replaylab_agent.mcp_proxy"),
        env={
            "REPLAYLAB_TASK_ID": task_id,
            "REPLAYLAB_TASK_EVENT_ROOT": str(event_root),
        },
        allowed_tools=frozenset({"inspect_event"}),
        effects=frozenset(),
        registry_path=workspace / "registry.json",
    )








@pytest.mark.asyncio
async def test_acp_native_agent_ends_after_one_turn_without_report(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    holder: dict[str, Any] = {"sessions": []}
    host = tmp_path / "BreadMCP.exe"
    host.write_bytes(b"host")

    def fake_route(context, *, workspace, event_root, allowed_effects, **options):
        assert context.conversation_id == "conversation-native-acp"
        route = build_native_mcp_route(
            context,
            workspace=workspace,
            event_root=event_root,
            allowed_effects=allowed_effects,
            **options,
        )
        holder["route"] = route
        return route

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.prompts: list[str] = []
            assert kwargs["mcp_stdio_env"]["REPLAYLAB_CONVERSATION_ID"] == "conversation-native-acp"
            holder["sessions"].append(self)

        async def __aenter__(self):
            holder["settings"] = (Path(self.kwargs["cwd"]) / ".claude" / "settings.json").is_file()
            holder["mcp_json"] = (Path(self.kwargs["cwd"]) / ".mcp.json").is_file()
            holder["skill"] = (
                Path(self.kwargs["cwd"])
                / ".claude"
                / "skills"
                / "renderdoc-explain-event"
                / "SKILL.md"
            ).is_file()
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            self.prompts.append(text)
            assert "## Loaded Skill: renderdoc-explain-event" in text
            assert "Use inspect_event and cite exact values." in text
            assert "report_task_result" not in text
            answer = "I inspected EID 42."
            callback = kwargs.get("on_delta")
            if callback:
                callback(answer)
            return PromptResult(answer, [answer], "end_turn", 1)

    monkeypatch.setattr(
        "replaylab_agent.native_engine.build_native_mcp_route", fake_route
    )
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=event_root,
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
            heartbeat_seconds=0.05,
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(
            RunRequest(
                prompt="解释 EID 42",
                profile=_profile(),
                task_id="native-acp",
                context=_context(),
                instance_id="instance-1",
                conversation_id="conversation-native-acp",
                mcp_executable=host,
                # The user picked this Skill in the panel; a picked Skill is pre-loaded.
                skill_name="renderdoc-explain-event",
            )
        )
    finally:
        close_logging()

    assert len(holder["sessions"]) == 1
    assert len(holder["sessions"][0].prompts) == 1
    assert holder["settings"] and holder["mcp_json"] and holder["skill"]
    assert holder["route"].env["REPLAYLAB_CONVERSATION_ID"] == "conversation-native-acp"
    assert result.core == "vendor-native-agent/v1"
    assert result.terminal_status == "completed"








@pytest.mark.asyncio
async def test_native_progress_counts_repeated_host_calls_from_receipts(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    task_id = "native-call-count"
    receipts = TaskReportStore(task_id, event_root)

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            receipts.record_result("inspect_event", {"eventId": 42})
            receipts.record_result("inspect_event", {"eventId": 43})
            receipts.record_result("inspect_shader", {"stage": "PS"}, failed=True)
            return PromptResult("Done.", ["Done."], "end_turn", 3)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=event_root, log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(RunRequest(prompt="Inspect two events", profile=_profile(), task_id=task_id))
    finally:
        close_logging()

    events = [json.loads(line) for line in (event_root / f"{task_id}.jsonl").read_text(encoding="utf-8").splitlines()]
    completed = [
        item for item in events
        if item.get("data", {}).get("phase") == "main"
        and item.get("data", {}).get("status") == "completed"
    ]
    assert result.terminal_status == "completed"
    assert len(completed) == 1
    assert "3 recorded Bread Host tool call(s)" in completed[0]["summary"]


@pytest.mark.asyncio
async def test_codex_progress_uses_same_durable_host_call_count(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    task_id = "codex-call-count"
    receipts = TaskReportStore(task_id, event_root)

    class FakeConnector:
        def __init__(self, argv: Any, **kwargs: Any) -> None:
            pass

        async def run(self, prompt: str, **kwargs: Any) -> Any:
            receipts.record_result("read_shader_workspace_file", {"ok": True})
            receipts.record_result("read_shader_workspace_file", {"ok": True})
            return type("Result", (), {"text": "Read both regions."})()

        async def close(self) -> None:
            pass

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["codex", "app-server"])
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=event_root, log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
        ),
        codex_connector_factory=FakeConnector,
    )
    try:
        result = await engine.run(RunRequest(prompt="Read shader", profile=_profile("codex"), task_id=task_id))
    finally:
        close_logging()

    events = [json.loads(line) for line in (event_root / f"{task_id}.jsonl").read_text(encoding="utf-8").splitlines()]
    completed = [
        item for item in events
        if item.get("data", {}).get("phase") == "main"
        and item.get("data", {}).get("status") == "completed"
    ]
    assert result.terminal_status == "completed"
    assert len(completed) == 1
    assert "2 recorded Bread Host tool call(s)" in completed[0]["summary"]


@pytest.mark.asyncio
async def test_codex_callbacks_reach_durable_activity_and_public_message_streams(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    task_id = "codex-observation-projection"

    class FakeConnector:
        def __init__(self, argv: Any, **kwargs: Any) -> None:
            pass

        async def run(self, prompt: str, **kwargs: Any) -> Any:
            timeline = kwargs["on_timeline"]
            message = kwargs["on_assistant_message"]
            timeline({"activityId": "tool-1", "activityKind": "mcpToolCall",
                      "activityTarget": "Bread.inspect_shader", "argumentNames": ["event_id", "stage"],
                      "status": "running", "lifecycle": "started", "elapsedSeconds": 2,
                      "untrustedExtra": "api_key=never-store-this"})
            message({"phase": "commentary", "itemId": "public-1", "text": "正在读取着色器。 api_key=private-key-value",
                     "lifecycle": "updated", "reset": False})
            timeline({"activityId": "public-1", "activityKind": "progressNote",
                      "activityDetail": "正在读取着色器。 api_key=private-key-value",
                      "status": "running", "lifecycle": "updated"})
            timeline({"activityId": "private-1", "activityKind": "reasoning",
                      "activityDetail": "Private reasoning must not become a public activity.",
                      "status": "running", "lifecycle": "updated"})
            timeline({"activityId": "public-1", "activityKind": "progressNote",
                      "status": "completed", "lifecycle": "completed"})
            timeline({"activityId": "tool-1", "activityKind": "mcpToolCall",
                      "activityTarget": "Bread.inspect_shader", "status": "completed",
                      "lifecycle": "completed", "durationMs": 45})
            kwargs["on_delta"]("最终答案。")
            return type("Result", (), {"text": "最终答案。"})()

        async def close(self) -> None:
            pass

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["codex", "app-server"])
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=event_root, log_root=tmp_path / "logs",
                                  workspace_base=tmp_path / "workspaces"),
        codex_connector_factory=FakeConnector,
    )
    try:
        result = await engine.run(RunRequest(prompt="Read Shader", profile=_profile("codex"), task_id=task_id))
    finally:
        close_logging()
    events = [json.loads(line) for line in (event_root / f"{task_id}.jsonl").read_text(encoding="utf-8").splitlines()]
    activities = [item for item in events if item["data"].get("activityKind") == "mcpToolCall"]
    assert [item["data"]["status"] for item in activities] == ["running", "completed"]
    assert all(item["tool"] == "Bread.inspect_shader" and item["data"]["hostVerified"] is False for item in activities)
    assert activities[0]["data"]["argumentNames"] == ["event_id", "stage"]
    assert activities[1]["data"]["durationMs"] == 45
    messages = [item for item in events if item["kind"] == "agent-message-delta"]
    assert len(messages) == 2  # One exact delta and one lifecycle-only close; no cumulative duplicate.
    assert messages[0]["data"]["phase"] == "commentary"
    assert messages[1]["text"] == "" and messages[1]["data"]["activityLifecycle"] == "completed"
    public_data = json.dumps(events, ensure_ascii=False)
    assert "private-key-value" not in public_data and "never-store-this" not in public_data
    assert "Private reasoning" not in public_data
    assert "[REDACTED]" in messages[0]["text"]
    assert result.text == "最终答案。" and result.terminal_status == "completed"
    diagnostic_log = (tmp_path / "logs" / "agent-core.log").read_text(encoding="utf-8")
    assert "mcpToolCall" in diagnostic_log
    assert "progressNote" not in diagnostic_log
    main_completed = next(item for item in events if item["data"].get("phase") == "main"
                          and item["data"].get("status") == "completed")
    assert "0 recorded Bread Host tool call(s)" in main_completed["summary"]


@pytest.mark.asyncio
async def test_acp_callbacks_reach_durable_activity_and_keep_text_streams_separate(tmp_path, monkeypatch) -> None:
    event_root = tmp_path / "events"
    task_id = "acp-observation-projection"

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            timeline = kwargs["on_timeline"]
            timeline({"activityId": "acp-tool-1", "activityKind": "mcpToolCall",
                      "activityTarget": "ReplayLab.inspect_shader", "argumentNames": ["event_id", "stage"],
                      "status": "running", "lifecycle": "started", "elapsedSeconds": 0,
                      "untrustedExtra": "api_key=never-store-this"})
            timeline({"activityId": "acp-plan-1", "activityKind": "plan",
                      "status": "running", "lifecycle": "started"})
            kwargs["on_delta"]("正在读取着色器。")
            kwargs["on_reasoning"]({"text": "Provider reasoning.", "itemId": "acp-reasoning-1"})
            timeline({"activityId": "acp-tool-1", "activityKind": "mcpToolCall",
                      "activityTarget": "ReplayLab.inspect_shader", "status": "completed",
                      "lifecycle": "completed", "elapsedSeconds": 0})
            timeline({"activityId": "acp-tool-2", "activityKind": "toolCall",
                      "status": "failed", "lifecycle": "completed",
                      "activityDetail": "Provider diagnostics contain api_key=private-key-value"})
            return PromptResult("正在读取着色器。", ["正在读取着色器。"], "end_turn", 2)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=event_root, log_root=tmp_path / "logs",
                                  workspace_base=tmp_path / "workspaces"),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(RunRequest(prompt="Read Shader", profile=_profile(), task_id=task_id))
    finally:
        close_logging()
    events = [json.loads(line) for line in (event_root / f"{task_id}.jsonl").read_text(encoding="utf-8").splitlines()]
    activities = [item for item in events if item["data"].get("phase") == "native"]
    assert [item["data"]["activityKind"] for item in activities] == ["mcpToolCall", "plan", "mcpToolCall", "toolCall"]
    assert [item["data"]["status"] for item in activities] == ["running", "running", "completed", "failed"]
    assert all(item["data"]["source"] == "acp" and item["data"]["hostVerified"] is False for item in activities)
    assert activities[0]["tool"] == "ReplayLab.inspect_shader"
    assert activities[0]["data"]["argumentNames"] == ["event_id", "stage"]
    assert activities[0]["data"]["elapsedSeconds"] == 0
    assert activities[-1]["tool"] == ""  # A generic ACP tool is not a Bread Host receipt.
    assert any(item["kind"] == "message-delta" and item["text"] == "正在读取着色器。" for item in events)
    assert any(item["kind"] == "reasoning-delta" and item["text"] == "Provider reasoning." for item in events)
    assert not any(item["kind"] == "agent-message-delta" for item in events)
    public_data = json.dumps(events, ensure_ascii=False)
    assert "private-key-value" not in public_data and "never-store-this" not in public_data
    assert result.text == "正在读取着色器。" and result.terminal_status == "completed"
    main_completed = next(item for item in events if item["data"].get("phase") == "main"
                          and item["data"].get("status") == "completed")
    assert "0 recorded Bread Host tool call(s)" in main_completed["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unfinished", [False, True])
@pytest.mark.parametrize("empty_retry", [False, True, "empty-end"])
@pytest.mark.parametrize("reason", ["max_tokens", "max_turn_requests"])
async def test_acp_output_limit_is_partial_even_without_host_calls(tmp_path, monkeypatch, unfinished, empty_retry, reason) -> None:
    prompts = []

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            prompts.append(text)
            if unfinished:
                kwargs["on_timeline"]({"activityId": "still-writing", "activityKind": "toolCall", "status": "running"})
            answer = "" if empty_retry and len(prompts) > 1 else "First half of answer"
            stop = "end_turn" if empty_retry == "empty-end" and len(prompts) > 1 else reason
            return PromptResult(answer, [answer], stop, 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=tmp_path / "events", log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(RunRequest(prompt="Continue the shader", profile=_profile(), task_id="native-max-tokens"))
    finally:
        close_logging()

    assert result.terminal_status == "partial"
    assert "First half of answer" in result.text
    if reason == "max_tokens":
        assert "output limit" in result.text
    assert len(prompts) == (1 if unfinished else 2 if empty_retry == "empty-end" else 4)


@pytest.mark.asyncio
async def test_acp_new_tool_invalidates_truncated_answer_fallback(tmp_path, monkeypatch) -> None:
    prompts = []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prompt(self, text, **kwargs):
            prompts.append(text)
            if len(prompts) == 1:
                return PromptResult("Pre-tool progress", ["Pre-tool progress"], "max_tokens", 0)
            if len(prompts) == 2:
                for status in ("running", "completed"):
                    kwargs["on_timeline"]({"activityId": "new-tool", "activityKind": "toolCall", "status": status})
            return PromptResult("", [""], "max_tokens", 1 if len(prompts) == 2 else 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs", workspace_base=tmp_path / "turns"),
        acp_session_factory=Session)
    try:
        result = await engine.run(RunRequest(prompt="Inspect event 42", profile=_profile(), task_id="acp-tool-boundary"))
    finally:
        close_logging()
    assert result.terminal_status == "partial"
    assert "Pre-tool progress" not in result.text
    assert len(prompts) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["max_tokens", "max_turn_requests"])
@pytest.mark.parametrize("progress", ["answer", "work"])
async def test_acp_productive_task_spans_many_turn_limits(tmp_path, monkeypatch, reason, progress) -> None:
    prompts, sessions, effects = [], [], []
    store = TaskReportStore("acp-productive-limits", tmp_path / "events")

    class Session:
        def __init__(self, **kwargs):
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prompt(self, text, **kwargs):
            prompts.append(text)
            index = len(prompts)
            if index == 6:
                return PromptResult("All five stages completed", ["All five stages completed"], "end_turn", 0)
            if index > 1:
                assert "do not restart" in text
                assert "smaller independently useful steps" in text
                assert kwargs["attachments"] == ()
            if progress == "work":
                effects.append(index)
                store.record_task_activity("native-file-edit", f"saved-revision-{index}", occurrence_id=f"edit-{index}")
            answer = f"New answer section {index}\n" if progress == "answer" else ""
            return PromptResult(answer, [answer], reason, 1 if progress == "work" else 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs", workspace_base=tmp_path / "turns"),
        acp_session_factory=Session)
    try:
        result = await engine.run(RunRequest(prompt="Complete five stages", profile=_profile(), task_id="acp-productive-limits"))
    finally:
        close_logging()
    assert result.terminal_status == "completed", result.error
    assert result.text == ("".join(f"New answer section {index}\n" for index in range(1, 6))
                           if progress == "answer" else "") + "All five stages completed"
    assert len(prompts) == 6 and len(sessions) == 1
    assert effects == (list(range(1, 6)) if progress == "work" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["recover", "unsupported", "still-too-large", "authentication"])
async def test_acp_context_overflow_uses_supported_compaction_without_restarting_task(tmp_path, monkeypatch, failure):
    from replaylab_agent.errors import ProviderRequestError
    prompts, sessions, compact_calls = [], [], []

    class Session:
        def __init__(self, **kwargs):
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def compact_context(self):
            compact_calls.append(True)
            return failure != "unsupported"

        async def prompt(self, text, **kwargs):
            prompts.append(text)
            if len(prompts) == 1 or failure == "still-too-large":
                raise ProviderRequestError("Input did not fit", error_code=
                    "provider-authentication" if failure == "authentication" else "provider-context")
            assert "do not restart" not in text or "repeat completed" in text
            assert "Do not restart the task" in text
            assert "ORIGINAL_TASK_MARKER" not in text
            return PromptResult("Completed after context recovery", ["Completed after context recovery"], "end_turn", 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs", workspace_base=tmp_path / "turns"),
        acp_session_factory=Session)
    try:
        result = await engine.run(RunRequest(prompt="ORIGINAL_TASK_MARKER", profile=_profile(), task_id="acp-input-limit"))
    finally:
        close_logging()
    assert len(sessions) == 1
    assert len(compact_calls) == (0 if failure == "authentication" else 1)
    assert len(prompts) == (2 if failure in {"recover", "still-too-large"} else 1)
    assert result.terminal_status == ("completed" if failure == "recover" else "failed")


@pytest.mark.asyncio
async def test_acp_output_limit_continues_same_session_without_restarting_tools(tmp_path, monkeypatch) -> None:
    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.prompts = []
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prompt(self, text, **kwargs):
            self.prompts.append(text)
            if len(self.prompts) == 1:
                return PromptResult("Saved the first edit.", ["Saved the first edit."], "max_tokens", 1)
            assert "do not restart" in text
            assert "repeat completed tools" in text
            assert kwargs["attachments"] == ()
            return PromptResult("The shader has been organized and saved.",
                                ["The shader has been organized and saved."], "end_turn", 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs",
                                  workspace_base=tmp_path / "workspaces"),
        acp_session_factory=FakeSession)
    try:
        result = await engine.run(RunRequest(prompt="Organize the shader", profile=_profile(),
                                             task_id="native-output-continued"))
    finally:
        close_logging()

    assert result.terminal_status == "completed"
    assert result.text == "Saved the first edit.The shader has been organized and saved."
    assert len(sessions) == 1
    assert len(sessions[0].prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", ["", "I will inspect the shader."])
async def test_acp_missing_final_answer_is_not_completed(tmp_path, monkeypatch, progress) -> None:
    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prompt(self, text, **kwargs):
            return PromptResult(progress, [progress, ""], "end_turn", 1 if progress else 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs",
                                  workspace_base=tmp_path / "workspaces"),
        acp_session_factory=FakeSession)
    try:
        result = await engine.run(RunRequest(prompt="Inspect the shader", profile=_profile(),
                                             task_id="native-no-final-answer"))
    finally:
        close_logging()

    assert result.terminal_status == "failed"
    assert result.text == "The task returned no displayable result."


@pytest.mark.asyncio
@pytest.mark.parametrize("did_work", [False, True])
async def test_codex_missing_answer_preserves_partial_host_work(tmp_path, monkeypatch, did_work) -> None:
    task_id = "codex-empty-answer"
    receipts = TaskReportStore(task_id, tmp_path / "events")

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            if did_work:
                receipts.record_result("write_shader_workspace_file", {"ok": True})
            return type("Result", (), {"text": ""})()

        async def close(self):
            pass

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["codex", "app-server"])
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs",
                                  workspace_base=tmp_path / "workspaces"),
        codex_connector_factory=FakeConnector)
    try:
        result = await engine.run(RunRequest(prompt="Organize the shader", profile=_profile("codex"),
                                             task_id=task_id))
    finally:
        close_logging()

    assert result.terminal_status == ("partial" if did_work else "failed")
    assert result.text == "The task returned no displayable result."


@pytest.mark.asyncio
async def test_exhausted_acp_transport_retains_work_and_explains_interruption(tmp_path, monkeypatch) -> None:
    from replaylab_agent.errors import ProviderIncidentError

    task_id = "acp-exhausted-transport"
    sessions = []
    receipts = TaskReportStore(task_id, tmp_path / "events")

    class FakeSession:
        def __init__(self, **kwargs):
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prompt(self, text, **kwargs):
            if len(sessions) == 1:
                receipts.record_result("write_shader_workspace_file", {"ok": True})
            else:
                assert "never repeat a mutation" in text
                assert "write_shader_workspace_file" in text
            raise ProviderIncidentError("connection lost", incident_code="http2-cancel",
                                        partial_text="The edit was saved.")

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr("replaylab_agent.native_engine._DURABLE_RECEIPT_SETTLE_SECONDS", 0.0)
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs",
                                  workspace_base=tmp_path / "workspaces"),
        acp_session_factory=FakeSession)
    try:
        result = await engine.run(RunRequest(prompt="Organize the shader", profile=_profile(), task_id=task_id))
    finally:
        close_logging()

    assert len(sessions) == 3
    assert result.terminal_status == "partial"
    assert result.text.startswith("The edit was saved.")
    assert "interrupted after retrying" in result.text


@pytest.mark.asyncio
async def test_acp_elicitation_becomes_panel_clarification(tmp_path, monkeypatch) -> None:
    def fake_route(context, *, workspace, event_root, allowed_effects, **options):
        return _route(workspace, event_root, context.task_id)

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            kwargs["on_elicitation"] if "on_elicitation" in kwargs else None
            self.kwargs["on_elicitation"](
                "Which shader?",
                {
                    "sessionId": "s",
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
            return PromptResult("waiting", ["waiting"], "end_turn", 0)

    monkeypatch.setattr(
        "replaylab_agent.native_engine.build_native_mcp_route", fake_route
    )
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=tmp_path / "events",
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(
            RunRequest(
                prompt="解释着色器",
                profile=_profile(),
                task_id="native-question",
                context=_context(),
                instance_id="instance-1",
            )
        )
    finally:
        close_logging()
    assert result.terminal_status == "needs-user-input"
    assert result.clarification is not None
    assert [item.label for item in result.clarification.options] == [
        "Vertex shader",
        "Pixel shader",
    ]








def test_native_route_uses_filtered_proxy_and_codex_hardening(tmp_path, monkeypatch) -> None:
    host = tmp_path / "BreadMCP.exe"
    host.write_bytes(b"host")
    profile = _profile("codex")
    context = ProviderContext(
        profile=profile,
        model="",
        instance_id="instance-1",
        context=_context(),
        intent=None,
        mcp_executable=host,
        task_id="route-task",
        conversation_id="conversation-route",
    )
    route = build_native_mcp_route(
        context,
        workspace=tmp_path,
        event_root=tmp_path / "events",
        allowed_effects=("workspace",),
    )
    assert route is not None
    assert route.visible_tools == (
        AGENT_LOCAL_TOOLS | {"read_evidence_file"} |
        (set(CORE_DIRECT_TOOLS) & route.allowed_tools)
    )
    assert "--full-host-catalog" not in route.args
    assert route.args[:3] == ("-I", "-m", "replaylab_agent.mcp_proxy")
    assert "--guard" not in route.args
    assert "--allow" in route.args and "report_task_result" not in route.args
    assert "--effect" in route.args and "workspace" in route.args
    assert route.env["REPLAYLAB_TASK_ID"] == "route-task"
    assert route.env["REPLAYLAB_CONVERSATION_ID"] == "conversation-route"
    assert "REPLAYLAB_SHADER_CONTINUATION_TARGET" not in route.env

    argv = native_codex_argv(["codex", "app-server"], route)
    rendered = " ".join(argv)
    assert "mcp_servers.Bread=" in rendered
    assert "features.shell_tool=true" in rendered
    assert "features.unified_exec=true" in rendered
    assert "features.apps=false" in rendered
    assert argv[-1] == "app-server"

    task_server = tomllib.loads(route.codex_config_override())["mcp_servers"]["Bread"]
    assert task_server["command"] == route.command
    assert task_server["args"] == list(route.args)
    assert task_server["env"] == route.env
    assert task_server["required"] is True
    assert task_server["startup_timeout_sec"] == 60
    assert task_server["tool_timeout_sec"] == 3600
    assert task_server["default_tools_approval_mode"] == "approve"
    capture_free = " ".join(native_codex_argv(["codex", "app-server"], None))
    assert "default_tools_approval_mode" not in capture_free

    skill_catalog = _catalog(tmp_path)
    lazy_route = build_native_mcp_route(
        context,
        workspace=tmp_path,
        event_root=tmp_path / "events",
        allowed_effects=("workspace",),
        skill_roots=skill_catalog.roots,
    )
    assert lazy_route is not None
    assert lazy_route.visible_tools == (
        AGENT_LOCAL_TOOLS | {"read_evidence_file"} |
        (set(CORE_DIRECT_TOOLS) & lazy_route.allowed_tools)
    )
    assert "inspect_event" in lazy_route.allowed_tools
    assert "inspect_event" in lazy_route.visible_tools


def test_native_route_stays_available_when_optional_skill_root_is_missing(
    tmp_path: Path,
) -> None:
    host = tmp_path / "BreadMCP.exe"
    host.write_bytes(b"host")
    context = ProviderContext(
        profile=_profile("codex"), model="", instance_id="instance-1",
        context=_context(), intent=None, mcp_executable=host,
        task_id="route-invalid-skills",
    )
    route = build_native_mcp_route(
        context,
        workspace=tmp_path,
        event_root=tmp_path / "events",
        allowed_effects=(),
        skill_roots=(tmp_path / "missing",),
    )
    assert route is not None
    assert "list_bread_capabilities" in route.allowed_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("intent_kind", ["", "explain-current-event"])
async def test_unpicked_request_loads_no_skill_and_is_told_to_choose(
    tmp_path, monkeypatch, intent_kind
) -> None:
    """A UI target alone does not silently choose an Agent Skill."""

    event_root = tmp_path / "events"
    prompts: list[str] = []

    def fake_route(context, *, workspace, event_root, allowed_effects, **options):
        assert options["skill_roots"], "the task MCP filter needs the Skill roots"
        return _route(workspace, event_root, context.task_id)

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            prompts.append(text)
            return PromptResult("Hello.", ["Hello."], "end_turn", 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", fake_route)
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=event_root,
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
            heartbeat_seconds=0.05,
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(
            RunRequest(prompt="1?", profile=_profile(), task_id="native-plain",
                       context=_context(), instance_id="instance-1",
                       intent={"kind": intent_kind, "arguments": {}} if intent_kind else None)
        )
    finally:
        close_logging()

    assert prompts, "the Agent still gets its turn"
    system = prompts[0]
    assert "## Loaded Skill: renderdoc-explain-event" not in system
    assert "Use inspect_event and cite exact values." not in system
    assert "list_bread_capabilities" in system
    # The directory is still listed, so the Agent can decide for itself.
    assert "renderdoc-explain-event" in system
    assert result.terminal_status == "completed"


@pytest.mark.asyncio
async def test_disabled_skill_is_neither_advertised_nor_copied_to_claude(tmp_path, monkeypatch) -> None:
    host = tmp_path / "BreadMCP.exe"
    host.write_bytes(b"host")
    observed: dict[str, Any] = {}

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.workspace = Path(kwargs["cwd"])

        async def __aenter__(self):
            observed["skill_copied"] = (
                self.workspace / ".claude" / "skills" / "renderdoc-explain-event" / "SKILL.md"
            ).exists()
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def prompt(self, text: str, **kwargs: Any) -> PromptResult:
            observed["prompt"] = text
            return PromptResult("done", ["done"], "end_turn", 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=tmp_path / "events",
            log_root=tmp_path / "logs",
            workspace_base=tmp_path / "workspaces",
            disabled_skills=("renderdoc-explain-event",),
        ),
        acp_session_factory=FakeSession,
    )
    try:
        result = await engine.run(
            RunRequest(prompt="hello", profile=_profile(), task_id="disabled-skill", mcp_executable=host)
        )
    finally:
        close_logging()

    assert result.terminal_status == "completed"
    assert observed["skill_copied"] is False
    assert "renderdoc-explain-event" not in observed["prompt"]


@pytest.mark.asyncio
async def test_outer_runtime_deadline_cancels_once_and_reports_provider_timeout(tmp_path, monkeypatch) -> None:
    executions = 0
    cancelled = False

    async def stalled_execution(self, request, **kwargs):
        nonlocal executions, cancelled
        executions += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    monkeypatch.setattr(NativeAgentEngine, "_execute", stalled_execution)
    event_root = tmp_path / "events"
    engine = NativeAgentEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(main_timeout=0.01, event_root=event_root, log_root=tmp_path / "logs"),
    )
    try:
        result = await engine.run(RunRequest(prompt="synthetic timeout", profile=_profile(), task_id="deadline"))
    finally:
        close_logging()

    assert executions == 1 and cancelled
    assert result.terminal_status == "failed"
    assert "configured runtime limit" in result.error
    events = [json.loads(line) for line in (event_root / "deadline.jsonl").read_text(encoding="utf-8").splitlines()]
    terminals = [event for event in events if event["kind"] in {"failed", "completed", "cancelled"}]
    assert len(terminals) == 1
    assert terminals[0]["kind"] == "failed"
    assert terminals[0]["data"]["errorCode"] == "provider-timeout"
    assert terminals[0]["data"]["errorMessageId"] == "task.error.provider-timeout"
