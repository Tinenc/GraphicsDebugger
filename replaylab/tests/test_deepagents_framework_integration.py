from __future__ import annotations

import json
import functools
import importlib.util
from types import SimpleNamespace
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

import deepagents
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.deep_engine import BreadMCPTools, DeepAgentsEngine, _langchain_tool
from replaylab_agent.profiles import Profile
from replaylab_agent.skill_gateway import SkillGateway


@pytest.mark.asyncio
async def test_real_sdk_internal_model_metadata_survives_event_stream():
    from langchain.agents.middleware.internal_call_transformer import internal_call_metadata
    from langchain_core.runnables import RunnableLambda
    from replaylab_agent.deep_engine import _internal_model_event

    model = FakeMessagesListChatModel(responses=[AIMessage(content="Internal compaction note"),
                                                AIMessage(content="Public answer")])
    async def calls(_):
        await model.ainvoke("compact", config={"metadata": {
            "lc_source": "summarization", **internal_call_metadata()}})
        return await model.ainvoke("answer")
    events = [event async for event in RunnableLambda(calls).astream_events("test", version="v2")]
    endings = [event for event in events if event['event'] == 'on_chat_model_end']
    assert len(endings) == 2
    assert _internal_model_event(endings[0])
    assert not _internal_model_event(endings[1])
    assert _internal_model_event({'metadata': internal_call_metadata()})
    assert not _internal_model_event({'metadata': {'lc_internal_call': 'untrusted-marker'}})


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True, "empty-end"])
async def test_api_responses_stream_failure_after_tool_preserves_completed_work(tmp_path, monkeypatch, exhausted):
    import httpx
    from langchain_openai import ChatOpenAI
    from replaylab_agent.connectors.openai_compatibility import compatible_chat_model

    requests, effects = [], []
    function = dict(type="function_call", id="fc-evidence", call_id="call-evidence",
                    name="inspect_event", arguments='{"event_id":5832}', status="completed")

    def handler(request):
        requests.append(json.loads(request.content))
        index = len(requests)
        assert request.url.path == "/v1/responses" and requests[-1]["stream"] is True
        if index == 1:
            events = [dict(type="response.output_item.added", output_index=0, item=function)]
            output = [function]
        elif index == 2:
            events = [dict(type="response.output_text.delta", output_index=0, content_index=0,
                           item_id="msg-abandoned", delta="Abandoned draft"),
                      dict(type="error", error={"message": "You have no credits remaining." if exhausted
                                                else "An error occurred while processing the request."})]
            output = None
        else:
            events = [dict(type="response.output_text.delta", output_index=0, content_index=0,
                           item_id="msg-final", delta="Verified result")]
            output = [dict(type="message", id="msg-final", role="assistant", status="completed",
                           content=[dict(type="output_text", text="Verified result", annotations=[])])]
        if output is not None:
            events.append(dict(type="response.completed", response=dict(id=f"resp-{index}", object="response",
                               model="offline", created_at=1, status="completed", output=output)))
        stream = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        return "Saved evidence from event 5832"

    tool = _langchain_tool("inspect_event", inspect_event, description="Save verified event evidence.",
        args_schema={"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]})

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": tool}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = compatible_chat_model(ChatOpenAI, dict(model="offline", api_key="fixture-key",
            base_url="https://api.invalid/v1", use_responses_api=True, max_retries=2, http_async_client=client))
        engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
            config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
            deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
        result = await engine.run(RunRequest(prompt="Inspect event 5832", task_id="api-sse-recovery", instance_id="offline",
            profile=Profile(id="offline", preset="openai", model="offline")))
        model.root_client.close()
    assert effects == [5832], result.error
    assert len(requests) == (2 if exhausted else 3)
    evidence = [item for item in requests[-1]["input"] if item.get("type") == "function_call_output"]
    assert len(evidence) == 1 and evidence[0]["call_id"] == "call-evidence"
    events = [json.loads(line) for line in (tmp_path / "events/api-sse-recovery.jsonl").read_text(encoding="utf-8").splitlines()]
    if exhausted:
        assert result.terminal_status == "failed"
        assert events[-1]["data"]["errorCode"] == "provider-quota"
    else:
        assert requests[1] == requests[2]
        assert result.terminal_status == "completed" and result.text == "Verified result", result.error
        provider_text = ""
        for event in events:
            if event["kind"] == "message-delta" and event["data"].get("stream") == "provider":
                provider_text = ("" if event["data"].get("reset") else provider_text) + event["text"]
        assert provider_text == "Verified result"


@pytest.fixture(autouse=True)
def completed_general_review_for_framework_regressions(monkeypatch):
    """Keep these Skill/Shader/framework tests independent of generic review policy."""
    from replaylab_agent.task_review import completion_review_state
    def reviewed(store, tracker):
        store.record_task_review("complete", [{"requirement": "Framework regression outcome", "status": "satisfied", "evidence": "Scripted framework outcome"}], [], [])
        return completion_review_state(store, tracker)
    monkeypatch.setattr("replaylab_agent.deep_engine.completion_review_state", reviewed)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_review", [True, False])
async def test_real_api_shader_review_continues_checkpoint_without_replaying_tools(tmp_path, monkeypatch, complete_review):
    row = {"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True,
           "saved": True, "fresh": True, "review": {}}
    calls = []
    workspace = {}
    original_create = deepagents.create_deep_agent
    @functools.wraps(original_create)
    def create(*args, **kwargs):
        workspace["path"] = kwargs["backend"].cwd
        return original_create(*args, **kwargs)
    monkeypatch.setattr(deepagents, "create_deep_agent", create)

    async def inspect_event(event_id: int) -> str:
        calls.append(event_id)
        if not complete_review:
            (workspace["path"] / "draft.hlsl").write_text("float4 RecoverableColor;", encoding="utf-8")
        if len(calls) == 2 and complete_review:
            row["review"] = {"verdict": "complete"}
        return json.dumps({"eventId": event_id})

    tool = _langchain_tool("inspect_event", inspect_event, description="Inspect an event.",
        args_schema={"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]})

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": tool}

    model = ToolCallingFakeModel(responses=[
        _message("inspect_event", {"event_id": 1}, "before-review"), AIMessage(content="Organization done."),
        _message("inspect_event", {"event_id": 2}, "during-review"), AIMessage(content="Reviewed conclusion."),
    ])
    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", lambda store: {"targets": [row]})
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Organize the Shader", task_id="api-review", instance_id="offline",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert calls == [1, 2], result.error
    assert result.terminal_status == ("completed" if complete_review else "partial"), result.error
    final_context = model.seen_messages[-1]
    assert sum(isinstance(message, ToolMessage) and message.tool_call_id == "before-review" for message in final_context) == 1
    assert any(isinstance(message, HumanMessage) and "explicit Agent self-review" in str(message.content) for message in final_context)
    if complete_review:
        assert result.text == "Reviewed conclusion."
    else:
        assert "no new progress" in result.text
        assert "draft recovery" in result.text
        assert list((tmp_path / "turns" / "recovered-shader-drafts").glob("*/manifest.json"))
    events = [json.loads(line) for line in (tmp_path / "events" / "api-review.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = [event for event in events if event["kind"] == "completed"]
    final = [event for event in events if event["kind"] == "message-delta" and event["data"].get("stream") == "final"]
    assert len(terminal) == 1 and sum(event["data"].get("reset") is True for event in final) == 1
    assert "".join(event["text"] for event in final) == result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatcher", [False, True])
async def test_real_api_complete_shader_file_survives_framework_eviction(tmp_path, monkeypatch, dispatcher):
    import hashlib
    source = "float4 MeaningfulColor;\n" * 5000
    raw = source.encode("utf-8")
    text = json.dumps({"schema": "renderdoc-ai-shader-workspace-file/v1", "mode": "complete",
        "offset": 0, "text": source, "chars": len(source), "totalChars": len(source), "sizeBytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True},
        "truncated": False, "applied": False})

    calls = []
    async def read_shader_workspace_file(workspace_path: str, mode: str) -> str:
        calls.append(mode)
        return text if mode == "complete" else "Small independent observation"

    tool = _langchain_tool("read_shader_workspace_file", read_shader_workspace_file, description="Read Shader source.",
        args_schema={"type": "object", "properties": {"workspace_path": {"type": "string"}, "mode": {"type": "string"}},
                     "required": ["workspace_path", "mode"]})

    @asynccontextmanager
    async def session(*args):
        yield {"read_shader_workspace_file": tool}

    args = {"workspace_path": "shader", "mode": "complete"}
    responses = [
        _message("invoke_bread_tool" if dispatcher else "read_shader_workspace_file",
                 {"name": "read_shader_workspace_file", "arguments": args} if dispatcher else args, "complete-source"),
    ]
    if dispatcher:
        responses.extend(_message("read_shader_workspace_file", {"workspace_path": "shader", "mode": "range"}, f"small-{index}") for index in range(24))
    responses.extend([AIMessage(content="Read the entire source."), AIMessage(content="The earlier complete source remains available.")])
    review_reads = []
    def review_state(store):
        review_reads.append(True)
        return {"targets": [{"workspacePath": "shader", "changed": True, "saved": True, "fresh": True,
                             "review": {} if len(review_reads) == 1 else {"verdict": "complete"}}]}
    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", review_state)
    model = ToolCallingFakeModel(responses=responses)
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Read this Shader source", task_id="fullfile-api", instance_id="offline",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert result.terminal_status == "completed", result.error
    message = next(item for item in model.seen_messages[-1] if isinstance(item, ToolMessage) and item.tool_call_id == "complete-source")
    assert json.loads(message.content)["text"] == source
    assert calls.count("complete") == 1
    assert len(calls) == (25 if dispatcher else 1)
    assert len(review_reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_error", [False, True])
@pytest.mark.parametrize("review_complete", [False, True])
async def test_api_review_never_appends_turn_to_unsettled_checkpoint(tmp_path, monkeypatch, checkpoint_error, review_complete):
    from types import SimpleNamespace
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def create(*args, **kwargs):
        graph = original_create(*args, **kwargs)

        class UnsettledGraph:
            def astream_events(self, *args, **kwargs):
                return graph.astream_events(*args, **kwargs)

            async def aget_state(self, config):
                if checkpoint_error:
                    raise OSError("checkpoint unavailable")
                return SimpleNamespace(next=("tools",))

        return UnsettledGraph()

    monkeypatch.setattr(deepagents, "create_deep_agent", create)
    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", lambda store: {"targets": [{"workspacePath": "shader",
        "changed": True, "saved": True, "fresh": True, "review": {"verdict": "complete"} if review_complete else {}}]})
    model = ToolCallingFakeModel(responses=[AIMessage(content="Done")])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Organize the Shader", task_id="unsafe-checkpoint",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert result.terminal_status == "partial", result.error
    assert "Some operations are still running" in result.text
    assert len(model.seen_messages) == 1


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Scripted local model with the tool-binding surface deepagents requires."""

    bound_tool_names: list[tuple[str, ...]] = Field(default_factory=list)
    seen_messages: list[list[Any]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ToolCallingFakeModel":
        del tool_choice, kwargs
        self.bound_tool_names.append(tuple(
            tool.get("name", "") if isinstance(tool, dict) else tool.name
            for tool in tools
        ))
        return self

    def _generate(self, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"},  # OpenAI Chat and compatible endpoints / Azure
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    {"stop_reason": "max_tokens"},  # Anthropic
    {"finish_reason": "MAX_TOKENS"},  # Gemini / Vertex
])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_api_output_limit_continues_checkpoint_without_replaying_effects(
    tmp_path, metadata, exhausted,
):
    effects = []

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        return "Already saved verified evidence"

    tool = _langchain_tool("inspect_event", inspect_event, description="Save event evidence.")

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": tool}

    # Empty text is legitimate when a reasoning model spends its entire output
    # allowance before starting the answer. Later retries may also be empty.
    def limited(content):
        return AIMessage(content=content, response_metadata=metadata)
    responses = [_message("inspect_event", {"event_id": 42}, "saved-once"), limited("First partial answer\n\n")]
    responses += ([AIMessage(content="")] if exhausted == "empty-end" else
                  [limited("") for _ in range(3)] if exhausted else [AIMessage(content="Verified final answer")])
    model = ToolCallingFakeModel(responses=responses)
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Save evidence for event 42", task_id="api-output-recovery",
        instance_id="offline", profile=Profile(id="offline", preset="openai", model="offline")))

    assert effects == [42], result.error
    assert len(model.seen_messages) == (5 if exhausted is True else 3)
    messages = model.seen_messages[-1]
    assert len([item for item in messages if isinstance(item, ToolMessage) and item.tool_call_id == "saved-once"]) == 1
    user_messages = [item for item in messages if isinstance(item, HumanMessage)]
    assert len(user_messages) == (4 if exhausted is True else 2)
    assert "do not restart" in user_messages[-1].content
    if exhausted:
        assert result.terminal_status == "partial"
        assert "First partial answer" in result.text
        assert "output limit" in result.text
    else:
        assert result.terminal_status == "completed", result.error
        assert result.text == "First partial answer\n\nVerified final answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", ["answer", "work"])
async def test_api_productive_task_spans_more_than_two_output_continuations(tmp_path, progress):
    effects = []
    store = TaskReportStore("long-productive-output", tmp_path / "events")

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        # The fake Host must record the same durable work facts as the real
        # first-party MCP Host; a tool event alone is not proof of new work.
        store.record_task_activity("inspect_event", f"event-{event_id}", occurrence_id=f"observe-{event_id}")
        return json.dumps({"eventId": event_id, "saved": True})

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": _langchain_tool("inspect_event", inspect_event, description="Inspect event.")}

    responses = []
    for index in range(5):
        if progress == "work":
            responses.append(_message("inspect_event", {"event_id": index + 1}, f"unique-{index}"))
        responses.append(AIMessage(content=f"Answer section {index}\n" if progress == "answer" else "",
                                   response_metadata={"finish_reason": "length"}))
    responses.append(AIMessage(content="All five sections completed"))
    model = ToolCallingFakeModel(responses=responses)
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Inspect events 1 through 5", task_id="long-productive-output",
        instance_id="offline", profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "completed", result.error
    assert result.text == ("".join(f"Answer section {index}\n" for index in range(5))
                           if progress == "answer" else "") + "All five sections completed"
    assert len(model.seen_messages) == (11 if progress == "work" else 6)
    assert effects == (list(range(1, 6)) if progress == "work" else [])
    assert len([item for item in model.seen_messages[-1] if isinstance(item, HumanMessage)]) == 6


@pytest.mark.asyncio
async def test_api_reasoning_only_output_limit_recovers_visible_answer(tmp_path):
    model = ToolCallingFakeModel(responses=[
        AIMessage(content=[{"type": "reasoning", "reasoning": "Private deliberation"}],
                  response_metadata={"finish_reason": "length"}, usage_metadata={"input_tokens": 10,
                      "output_tokens": 512, "total_tokens": 522, "output_token_details": {"reasoning": 512}}),
        AIMessage(content="Visible conclusion"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Explain this", task_id="reasoning-only",
        profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "completed", result.error
    assert result.text == "Visible conclusion"
    assert len(model.seen_messages) == 2
    log = (tmp_path / "logs/agent-core.log").read_text(encoding="utf-8")
    assert "outputTokens=512 reasoningTokens=512" in log
    assert "Private deliberation" not in log


@pytest.mark.asyncio
async def test_api_reasoning_only_exhaustion_reports_original_chinese_symptom(tmp_path):
    model = ToolCallingFakeModel(responses=[AIMessage(content="", response_metadata={
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
    }) for _ in range(3)])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="请用中文解释这个问题", task_id="empty-output-exhausted",
        profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "partial", result.error
    assert "主 Agent 没有返回文本答案。" in result.text
    assert "模型达到本轮输出上限" in result.text
    assert len(model.seen_messages) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal", ["invalid", "valid", "mixed"])
async def test_api_truncated_tool_proposal_recovers_without_executing_partial_calls(tmp_path, proposal):
    effects = []

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        return "Verified event " + str(event_id)

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": _langchain_tool("inspect_event", inspect_event, description="Inspect event.")}

    cut = AIMessage(content="", response_metadata={"finish_reason": "length"},
        tool_calls=[{"name": "inspect_event", "args": {"event_id": 999}, "id": "cut-valid"}] if proposal != "invalid" else [],
        invalid_tool_calls=[{"name": "inspect_event", "args": '{"event_id":', "id": "cut-off", "error": "truncated"}]
                          if proposal != "valid" else [])
    model = ToolCallingFakeModel(responses=[cut,
        _message("inspect_event", {"event_id": 42}, "small-complete-call"),
        AIMessage(content="Recovered with a complete bounded call")])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model, tool_session_factory=session)
    result = await engine.run(RunRequest(prompt="Inspect event 42", task_id="invalid-tool-truncation",
        instance_id="offline", profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "completed", result.error
    assert effects == [42]
    assert len(model.seen_messages) == 3
    assert result.text == "Recovered with a complete bounded call"
    prior = next(message for message in model.seen_messages[-1]
                 if message.response_metadata.get("bread_unexecuted_truncated_tools"))
    assert not prior.tool_calls and not prior.invalid_tool_calls
    assert "None of the tools in this response were executed" in prior.content


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["pending", "unavailable", "unreadable", "missing-state"])
async def test_api_truncation_never_resumes_unverified_or_pending_checkpoint(tmp_path, monkeypatch, checkpoint):
    create = deepagents.create_deep_agent

    @functools.wraps(create)
    def guarded_create(*args, **kwargs):
        graph = create(*args, **kwargs)
        async def state(config):
            if checkpoint == "unreadable":
                raise OSError("checkpoint unavailable")
            if checkpoint == "missing-state":
                return None
            return SimpleNamespace(next=("tools",))
        methods = {"astream_events": graph.astream_events}
        if checkpoint != "unavailable":
            methods["aget_state"] = state
        return SimpleNamespace(**methods)

    monkeypatch.setattr(deepagents, "create_deep_agent", guarded_create)
    model = ToolCallingFakeModel(responses=[AIMessage(content="Partial answer",
        response_metadata={"finish_reason": "length"}), AIMessage(content="Must not run")])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Explain this", task_id="pending-checkpoint",
        profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "partial", result.error
    assert "Partial answer" in result.text
    assert len(model.seen_messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("cut_tool", [False, True])
async def test_real_sdk_sse_reasoning_only_truncation_recovers_after_tool(tmp_path, surface, cut_tool):
    """Exercise actual provider parsers, graph checkpoints, receipts and final events."""
    import httpx
    from langchain_openai import ChatOpenAI
    from langchain_anthropic import ChatAnthropic
    from replaylab_agent.connectors.openai_compatibility import compatible_chat_model

    if surface == "anthropic":
        # Use the SDK's actual transport type (some releases use httpx2).
        from anthropic import _base_client
        httpx = getattr(_base_client, "httpx2", httpx)

    requests, effects = [], []
    answer = "Verified transport answer"

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        assert body["model"] == "offline" and body["stream"] is True
        if surface == "responses":
            assert body["max_output_tokens"] == 512
            function = dict(type="function_call", id="fc-evidence", call_id="saved-once",
                            name="inspect_event", arguments='{"event_id":42}', status="completed")
            output = ([function] if index == 1 else [] if index == 2 else [dict(type="message",
                id="msg-final", role="assistant", status="completed",
                content=[dict(type="output_text", text=answer, annotations=[])])])
            events = ([dict(type="response.output_item.added", output_index=0, item=function)] if index == 1 else [])
            if index == 2 and cut_tool:
                cut = {**function, "id": "fc-cut", "call_id": "cut-proposal", "arguments": '{"event_id":999}'}
                output = [cut]
                events.append(dict(type="response.output_item.added", output_index=0, item=cut))
            if index > 2:
                events.append(dict(type="response.output_text.delta", output_index=0, content_index=0,
                                   item_id="msg-final", delta=answer))
            terminal = "incomplete" if index == 2 else "completed"
            response = dict(id=f"resp-{index}", object="response", model="offline", created_at=1,
                            status=terminal, output=output)
            if index == 2:
                response["incomplete_details"] = {"reason": "max_output_tokens"}
            events.append(dict(type="response." + terminal, response=response))
        elif surface == "chat":
            assert body["max_completion_tokens"] == 512
            delta = ({"role": "assistant", "tool_calls": [{"index": 0, "id": "saved-once", "type": "function",
                "function": {"name": "inspect_event", "arguments": '{"event_id":42}'}}]} if index == 1 else
                {"role": "assistant", "content": "" if index == 2 else answer})
            if index == 2 and cut_tool:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "cut-proposal", "type": "function",
                         "function": {"name": "inspect_event", "arguments": '{"event_id":'}}]}
            chunk = dict(id=f"chat-{index}", object="chat.completion.chunk", model="offline", created=1,
                choices=[dict(index=0, delta=delta, finish_reason="tool_calls" if index == 1 else "length" if index == 2 else "stop")])
            wire = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)
        else:
            assert body["max_tokens"] == 512
            events = [dict(type="message_start", message=dict(id=f"msg-{index}", type="message", role="assistant",
                model="offline", content=[], stop_reason=None, stop_sequence=None,
                usage=dict(input_tokens=10, output_tokens=0)))]
            if index != 2 or cut_tool:
                calling = index == 1 or index == 2 and cut_tool
                block = (dict(type="tool_use", id="saved-once" if index == 1 else "cut-proposal", name="inspect_event", input={})
                         if calling else dict(type="text", text=""))
                delta = (dict(type="input_json_delta", partial_json='{"event_id":42}' if index == 1 else '{"event_id":')
                         if calling else dict(type="text_delta", text=answer))
                events += [dict(type="content_block_start", index=0, content_block=block),
                           dict(type="content_block_delta", index=0, delta=delta), dict(type="content_block_stop", index=0)]
            events += [dict(type="message_delta", delta=dict(stop_reason="tool_use" if index == 1 else
                "max_tokens" if index == 2 else "end_turn", stop_sequence=None), usage=dict(output_tokens=20)), dict(type="message_stop")]
        wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        return "Verified saved evidence"

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": _langchain_tool("inspect_event", inspect_event, description="Inspect event.")}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if surface == "anthropic":
            import anthropic
            model = ChatAnthropic(model="offline", api_key="fixture-key", max_tokens=512, max_retries=0)
            original_client = model._async_client
            model._async_client = anthropic.AsyncAnthropic(api_key="fixture-key", base_url="https://api.invalid", http_client=client)
            await original_client.close()
        else:
            model = compatible_chat_model(ChatOpenAI, dict(model="offline", api_key="fixture-key", max_tokens=512,
                base_url="https://api.invalid/v1", use_responses_api=surface == "responses", max_retries=0, http_async_client=client))
        engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
            config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
            deepagents_module=deepagents, tool_session_factory=session, model_factory=lambda *args: model)
        result = await engine.run(RunRequest(prompt="Inspect event 42", task_id="api-wire-truncation", instance_id="offline",
            profile=Profile(id="offline", preset="anthropic" if surface == "anthropic" else "openai", model="offline",
                            options={"max_output_tokens": 512})))
        if surface == "anthropic":
            model._client.close()
        else:
            model.root_client.close()
    assert result.terminal_status == "completed", result.error
    assert result.text == answer
    assert effects == [42]
    assert len(requests) == 3
    context = requests[-1].get("messages", requests[-1].get("input"))
    assert "saved-once" in json.dumps(context)
    events = [json.loads(line) for line in (tmp_path / "events/api-wire-truncation.jsonl").read_text(encoding="utf-8").splitlines()]
    final = [event for event in events if event["kind"] == "message-delta" and event["data"].get("stream") == "final"]
    assert "".join(event["text"] for event in final) == answer
    assert events[-1]["data"]["terminalStatus"] == "completed"


@pytest.mark.asyncio
async def test_long_initial_request_is_exactly_archived_and_searchable_before_first_model_call(tmp_path):
    class ArchiveReadingModel(ToolCallingFakeModel):
        def _generate(self, messages, *args, **kwargs):
            if not self.seen_messages:
                human = next(item for item in messages if isinstance(item, HumanMessage))
                assert len(human.content) < 6000
                assert "/.bread_context/request-" in human.content
                assert "REQUIRED_TARGET=法线纹理" not in human.content
            return super()._generate(messages, *args, **kwargs)

    original = ("Find REQUIRED_TARGET in this input; preserve its exact Unicode value.\n"
                + "ordinary input data\n" * 12000 + "REQUIRED_TARGET=法线纹理\n" + "more data\n" * 12000)
    model = ArchiveReadingModel(responses=[
        _message("grep", {"pattern": "REQUIRED_TARGET=", "path": "/.bread_context", "output_mode": "content"}, "find-original"),
        AIMessage(content="The exact target is 法线纹理"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt=original, task_id="long-initial-input",
        profile=Profile(id="offline", preset="openai", model="offline", model_context_windows={"offline": 65536})))
    assert result.terminal_status == "completed", result.error
    assert result.text == "The exact target is 法线纹理"
    tool_result = next(item for item in model.seen_messages[-1] if isinstance(item, ToolMessage))
    assert "REQUIRED_TARGET=法线纹理" in tool_result.content


@pytest.mark.asyncio
async def test_child_truncated_tool_proposal_cannot_execute_partial_effects(tmp_path):
    effects = []

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        return "Observed " + str(event_id)

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": _langchain_tool("inspect_event", inspect_event, description="Inspect event.")}

    model = ToolCallingFakeModel(responses=[
        _message("task", {"description": "Inspect only event 42", "subagent_type": "general-purpose"}, "delegate-once"),
        AIMessage(content="", response_metadata={"finish_reason": "length"}, tool_calls=[{
            "name": "inspect_event", "args": {"event_id": 999}, "id": "child-cut-off"}]),
        _message("inspect_event", {"event_id": 42}, "main-recovery"),
        AIMessage(content="The parent recovered using event 42"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model, tool_session_factory=session)
    result = await engine.run(RunRequest(prompt="Inspect event 42", task_id="child-output-quarantine", instance_id="offline",
        profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "completed", result.error
    assert effects == [42]
    assert len(result.children) == 1
    assert "None of the tools in this response were executed" in result.children[0].report


@pytest.mark.asyncio
async def test_truncated_large_file_proposal_recovers_with_incremental_output(tmp_path, monkeypatch):
    workspace = {}
    create = deepagents.create_deep_agent

    @functools.wraps(create)
    def capture(*args, **kwargs):
        workspace["path"] = kwargs["backend"].cwd
        return create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture)
    first = "第一部分：已验证的数据\n" * 40 + "NEXT_SECTION\n"
    second = "第二部分：完整结论\n" * 40
    model = ToolCallingFakeModel(responses=[
        AIMessage(content="", response_metadata={"finish_reason": "length"}, tool_calls=[{
            "name": "write_file", "args": {"file_path": "/report.md", "content": "DO_NOT_EXECUTE_PARTIAL"}, "id": "cut-write"}]),
        _message("write_file", {"file_path": "/report.md", "content": first}, "bounded-write"),
        _message("edit_file", {"file_path": "/report.md", "old_string": "NEXT_SECTION\n", "new_string": second}, "bounded-edit"),
        AIMessage(content="The complete two-section report was saved."),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Save the full report in two sections", task_id="incremental-long-output",
        profile=Profile(id="offline", preset="openai", model="offline")))
    assert result.terminal_status == "completed", result.error
    path = workspace["path"] / "report.md"
    assert path.read_text(encoding="utf-8") == first.replace("NEXT_SECTION\n", second)
    assert "DO_NOT_EXECUTE_PARTIAL" not in path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in (tmp_path / "events/incremental-long-output.jsonl").read_text(encoding="utf-8").splitlines()]
    retained = next(item["data"]["recovery"] for item in events
                    if item["data"].get("phase") == "task-output-retention")
    assert any(item["path"] == str(path) for item in retained["files"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["proactive", "provider-overflow", "empty-summary", "inflated-summary", "truncated-summary", "archive-failure", "media-archive-failure"])
async def test_real_compaction_preserves_original_task_archives_and_completed_tool_effects(tmp_path, monkeypatch, mode):
    from langchain_core.outputs import ChatGeneration, ChatResult
    effects, histories = [], []
    original_cleanup = __import__("replaylab_agent.deep_engine", fromlist=["_DeepRun"])._DeepRun._cleanup_workspace

    def cleanup(run):
        if run._workspace_root:
            histories.extend(path.read_text(encoding="utf-8") for path in
                             (run._workspace_root / "conversation_history").glob("*.md"))
        return original_cleanup(run)

    monkeypatch.setattr("replaylab_agent.deep_engine._DeepRun._cleanup_workspace", cleanup)
    if mode == "archive-failure":
        async def fail_archive(*args, **kwargs):
            return None
        monkeypatch.setattr("deepagents.middleware.summarization.SummarizationMiddleware._aoffload_to_backend", fail_archive)
    if mode == "media-archive-failure":
        async def fail_media_archive(self, backend, messages):
            return messages, 1
        monkeypatch.setattr("deepagents.middleware.summarization.SummarizationMiddleware._aoffload_inline_media", fail_media_archive)

    class CompactionModel(ToolCallingFakeModel):
        summary_calls: int = 0
        overflow_raised: bool = False

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            metadata = getattr(run_manager, "metadata", {}) or {}
            if metadata.get("lc_source") == "summarization":
                self.summary_calls += 1
                if mode == "empty-summary":
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
                if mode == "inflated-summary":
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content="UNTRUSTED_INFLATION " * 10000))])
                if mode == "truncated-summary":
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content="UNTRUSTED_TRUNCATED_SUMMARY",
                        response_metadata={"finish_reason": "length"}))])
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=
                    "Private summary: prior events were observed; retain the task archive and inspect remaining events."))])
            if mode == "provider-overflow" and len(self.seen_messages) == 5 and not self.overflow_raised:
                from google.genai.errors import ClientError
                self.overflow_raised = True
                raise ClientError(400, {"error": {"code": 400, "message":
                    "The input token count (70000) exceeds the maximum number of tokens allowed (65536)"}})
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def inspect_event(event_id: int) -> str:
        effects.append(event_id)
        return json.dumps({"eventId": event_id, "evidence": f"EXACT_EVIDENCE_{event_id}:" + "x" * 14000})

    @asynccontextmanager
    async def session(*args):
        yield {"inspect_event": _langchain_tool("inspect_event", inspect_event, description="Inspect event.")}

    model = CompactionModel(responses=[*[_message("inspect_event", {"event_id": index}, f"event-{index}") for index in range(1, 9)],
                                      AIMessage(content="All eight events were inspected")])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model, tool_session_factory=session)
    result = await engine.run(RunRequest(prompt="Inspect events 1 through 8. NEVER_APPLY_REPLAY; keep exact ResourceId::1011.",
        task_id="compaction-retains-task", instance_id="offline", profile=Profile(id="offline", preset="openai", model="offline",
        model_context_windows={"offline": 65536} if mode != "provider-overflow" else {})))
    if mode in {"archive-failure", "media-archive-failure"}:
        assert result.terminal_status == "failed"
        assert ("recoverable original archive" if mode == "archive-failure" else "original media") in result.error
        assert effects == list(range(1, len(effects) + 1)) and len(effects) < 8
        return
    assert result.terminal_status == "completed", result.error
    assert result.text == "All eight events were inspected"
    assert effects == list(range(1, 9))
    assert model.summary_calls >= 1
    assert bool(model.overflow_raised) == (mode == "provider-overflow")
    assert any("EXACT_EVIDENCE_1" in history for history in histories)
    final_system = json.dumps(model.seen_messages[-1][0].content, ensure_ascii=False)
    assert "NEVER_APPLY_REPLAY" in final_system
    assert "ResourceId::1011" in final_system
    assert any(isinstance(item, ToolMessage) and item.tool_call_id == "event-8" for item in model.seen_messages[-1])
    if mode in {"empty-summary", "inflated-summary", "truncated-summary"}:
        final_input = json.dumps([item.content for item in model.seen_messages[-1]], ensure_ascii=False)
        assert "Recovery required" in final_input
        assert "UNTRUSTED_" not in final_input
    task_events = (tmp_path / "events/compaction-retains-task.jsonl").read_text(encoding="utf-8")
    assert "Private summary" not in task_events


def _message(tool: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool, "args": arguments, "id": call_id}],
    )


def _catalog(tmp_path: Path) -> CatalogView:
    skill = tmp_path / "skills" / "renderdoc-explain-event"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: renderdoc-explain-event\n"
        "description: Inspect one RenderDoc event from Host evidence.\n"
        "metadata:\n"
        "  replaylab-category: event-pipeline\n"
        "---\n\n"
        "Call inspect_event and copy the exact EID into the report.\n",
        encoding="utf-8",
    )
    core = tmp_path / "skills" / "replaylab-core"
    core.mkdir()
    canonical = Path(__file__).resolve().parents[2] / ".agents" / "skills" / "replaylab-core" / "SKILL.md"
    (core / "SKILL.md").write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
    return CatalogView.load(skill_roots=[tmp_path / "skills"])


@pytest.mark.asyncio
async def test_real_deepagents_runs_skill_subagent_host_tool_without_report_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPLAYLAB_VERIFY_MAX_ROUNDS", "1")
    inspected: list[int] = []
    loaded_skills: list[str] = []
    original_load = SkillGateway.load

    def record_load(self: SkillGateway, skill_id: str, section: str = "", offset: int = 0):
        loaded_skills.append(skill_id)
        return original_load(self, skill_id, section, offset)

    monkeypatch.setattr(SkillGateway, "load", record_load)
    task_store = TaskReportStore("framework-integration", tmp_path / "events")

    async def inspect_event(event_id: int) -> str:
        """Return deterministic event evidence."""

        inspected.append(event_id)
        result = json.dumps({"eventId": event_id, "name": "DrawIndexed"})
        task_store.record_tool("inspect_event")
        task_store.record_result("inspect_event", result)
        return result

    host_tool = _langchain_tool(
        "inspect_event",
        inspect_event,
        description="Inspect one event by EID.",
        args_schema={
            "type": "object",
            "properties": {"event_id": {"type": "integer"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
    )

    @asynccontextmanager
    async def tool_session(*_args: Any):
        yield {"inspect_event": host_tool}

    model = ToolCallingFakeModel(
        responses=[
            _message(
                "load_bread_skill",
                {"skill_id": "renderdoc-explain-event", "reason": "Explain the selected event."},
                "skill-1",
            ),
            _message(
                "task",
                {
                    "description": "Inspect EID 42 and report its exact event name.",
                    "subagent_type": "general-purpose",
                },
                "task-1",
            ),
            _message(
                "list_bread_capabilities",
                {"tool": "inspect_event"},
                "inspect-contract-1",
            ),
            _message(
                "invoke_bread_tool",
                {"name": "inspect_event", "arguments": {"event_id": 42}},
                "inspect-1",
            ),
            AIMessage(content="inspect_event returned eventId=42, name=DrawIndexed.\nSTATUS: DONE"),
            AIMessage(content="EID 42 is a DrawIndexed event."),
        ]
    )
    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(
            event_root=tmp_path / "events",
            workspace_base=tmp_path / "workspaces",
            log_root=tmp_path / "logs",
        ),
        deepagents_module=deepagents,
        tool_session_factory=tool_session,
        model_factory=lambda _profile, _model: model,
    )
    request = RunRequest(
        prompt="Explain EID 42.",
        profile=Profile(
            id="offline-deepagents",
            preset="openai",
            model="offline-model",
            api_key_env="UNUSED_OFFLINE_KEY",
        ),
        task_id="framework-integration",
        instance_id="0123456789abcdef0123456789abcdef",
        context={
            "capture": {"loaded": True, "path": "D:/capture.rdc", "api": "D3D12"},
            "events": {"currentEventId": 42, "selectedEventId": 42},
        },
    )

    result = await engine.run(request)

    assert inspected == [42]
    assert loaded_skills == ["renderdoc-explain-event"]
    assert result.terminal_status == "completed"
    assert len(result.children) == 1
    assert result.children[0].agent_id == "general-purpose"
    assert result.children[0].tools_used == ["inspect_event"]
    assert result.children[0].status == "completed"


@pytest.mark.asyncio
async def test_real_api_child_reloads_run_loaded_skill_missing_own_context(tmp_path, monkeypatch):
    """Default isolated children receive their description, not the parent's Skill text."""

    from replaylab_agent.agent_prompts import SKILL_TASK_GUIDANCE

    catalog = _catalog(tmp_path)
    skill_id = "renderdoc-explain-event"
    full_entry = catalog.skill(skill_id).instructions.strip()
    description = "Review EID 42 using the applicable event Skill."
    created = []
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def capture_create(*args, **kwargs):
        created.append(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture_create)

    @asynccontextmanager
    async def offline_tool_session(*_args):
        yield {}

    model = ToolCallingFakeModel(responses=[
        _message("load_bread_skill", {"skill_id": skill_id, "reason": "Prepare event analysis."}, "main-skill"),
        _message("task", {"description": description, "subagent_type": "general-purpose"}, "delegate"),
        _message("list_bread_capabilities", {}, "child-directory"),
        _message("load_bread_skill", {"skill_id": skill_id, "reason": "My context has no Skill body."}, "child-skill"),
        AIMessage(content="I received the applicable Skill in my own context."),
        AIMessage(content="The delegated review received its own Skill instructions."),
    ])
    engine = DeepAgentsEngine(
        catalog=catalog,
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "workspaces", log_root=tmp_path / "logs"),
        deepagents_module=deepagents,
        tool_session_factory=offline_tool_session,
        model_factory=lambda _profile, _model: model,
    )
    result = await engine.run(RunRequest(
        prompt="Review EID 42.", task_id="isolated-child-skill-context", instance_id="offline-instance",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY"),
    ))
    assert result.terminal_status == "completed", result.error
    assert len(result.children) == 1
    child_spec = created[0]["subagents"][0]
    assert child_spec.get("mode") != "fork"
    assert full_entry not in child_spec["system_prompt"]
    assert SKILL_TASK_GUIDANCE in created[0]["system_prompt"]
    assert SKILL_TASK_GUIDANCE in child_spec["system_prompt"]
    assert "your own current context" in child_spec["system_prompt"]

    child_batches = [messages for messages in model.seen_messages
                     if any(isinstance(message, HumanMessage) and message.content == description for message in messages)]
    assert child_batches
    assert not any(full_entry in str(message.content) for message in child_batches[0])
    assert not any(isinstance(message, ToolMessage) and message.tool_call_id == "main-skill"
                   for messages in child_batches for message in messages)
    child_messages = {message.tool_call_id: message for messages in child_batches for message in messages
                      if isinstance(message, ToolMessage)}
    directory = json.loads(child_messages["child-directory"].content)
    assert directory["loadedSkills"] == [skill_id]
    assert next(row for row in directory["skills"] if row["id"] == skill_id)["loaded"] is True
    assert "instructions" not in directory
    assert full_entry not in child_messages["child-directory"].content
    child_loaded = json.loads(child_messages["child-skill"].content)
    assert child_loaded["alreadyLoaded"] is True
    assert child_loaded["instructions"] == full_entry
    assert child_loaded["readReceipt"]["entryComplete"] is True
    main_loaded = next(message for messages in model.seen_messages for message in messages
                       if isinstance(message, ToolMessage) and message.tool_call_id == "main-skill")
    assert json.loads(main_loaded.content)["alreadyLoaded"] is False
    assert json.loads(main_loaded.content)["readReceipt"]["instructionsSha256"] == child_loaded["readReceipt"]["instructionsSha256"]
    assert TaskReportStore("isolated-child-skill-context", tmp_path / "events").tools_used() == ()


@pytest.mark.asyncio
async def test_real_capture_free_general_child_has_no_preloaded_skill_or_main_tools(tmp_path, monkeypatch):
    created = []
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def capture_create(*args, **kwargs):
        created.append(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture_create)
    model = ToolCallingFakeModel(responses=[
        _message("task", {"description": "Explain what a vertex shader does.", "subagent_type": "general-purpose"}, "general-1"),
        AIMessage(content="A vertex shader transforms vertices.\nSTATUS: DONE"),
        AIMessage(content="A vertex shader transforms vertices."),
    ])
    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "workspaces", log_root=tmp_path / "logs"),
        deepagents_module=deepagents,
        model_factory=lambda _profile, _model: model,
    )
    request = RunRequest(prompt="Explain vertex shaders.", task_id="no-capture-framework",
                         profile=Profile(id="offline", preset="openai", model="offline-model", api_key_env="UNUSED_OFFLINE_KEY"))
    result = await engine.run(request)
    assert result.terminal_status == "completed"
    assert len(result.children) == 1
    spec = created[0]["subagents"][0]
    assert spec["name"] == "general-purpose"
    assert spec["tools"] == []
    assert "Standing rules" not in spec.get("system_prompt", spec.get("prompt", ""))
    assert result.children[0].tools_used == []
    assert TaskReportStore(request.task_id, tmp_path / "events").tools_used() == ()


def _host_file_helpers():
    """Load the actual Host file admission implementation without its runtime."""

    path = Path(__file__).resolve().parents[1] / "mcp_host" / "src" / "agent_workspace_files.py"
    spec = importlib.util.spec_from_file_location("offline_host_agent_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_api_framework_edits_host_copy_and_imports_same_physical_root(tmp_path, monkeypatch):
    helpers = _host_file_helpers()
    original = "float4 Shade(float3 normalWS) { return float4(normalWS, 1); }\n"
    revised = "float4 Shade(float3 normalWS) { return float4(normalWS * 0.5 + 0.5, 1); }\n"
    user_source = tmp_path / "user-project" / "source" / "shader.hlsl"
    user_source.parent.mkdir(parents=True)
    user_source.write_text(original, encoding="utf-8")
    delivered = tmp_path / "host-saved-source.hlsl"
    observed = {}
    model = ToolCallingFakeModel(responses=[])
    # This fixture uses the real turn workspace lifecycle and Host file helper,
    # without starting a RenderDoc process or granting access to user roots.
    monkeypatch.setattr("replaylab_agent.deep_engine.build_native_mcp_route", lambda *_a, **_k: None)

    class Session(BreadMCPTools):
        async def __aenter__(self):
            await super().__aenter__()
            assert self.workspace_root is not None
            observed["root"] = self.workspace_root
            monkeypatch.setenv(helpers.AGENT_WORKSPACE_ROOT_ENV, str(self.workspace_root))
            agent_file = helpers.copy_text_to_agent_workspace("proposal.hlsl", user_source.read_bytes())
            observed["file"] = agent_file

            async def read_shader_workspace_file(workspace_path: str, relative_path: str) -> str:
                """Export a managed Shader source into the current Agent directory."""
                assert workspace_path == "managed-workspace"
                assert relative_path == "source/shader.hlsl"
                return json.dumps({"agentFile": agent_file, "applied": False})

            async def write_shader_workspace_organization_file(organization_id: str, source_file: str) -> str:
                """Import an Agent-owned proposal through Host physical file admission."""
                assert organization_id == "test-organization"
                assert Path(source_file).parent == self.workspace_root
                text, receipt = helpers.read_agent_text_file(source_file)
                assert text == revised
                delivered.write_text(text, encoding="utf-8")
                observed["import"] = receipt
                return json.dumps({"sourceSha256": receipt["sha256"], "saved": True, "applied": False})

            model.responses = [
                _message("read_shader_workspace_file", {"workspace_path": "managed-workspace", "relative_path": "source/shader.hlsl"}, "export"),
                _message("task", {"description": "Read and revise /proposal.hlsl; report the edit.", "subagent_type": "general-purpose"}, "edit-child"),
                _message("read_file", {"file_path": agent_file["virtualPath"]}, "child-read"),
                _message("edit_file", {"file_path": agent_file["virtualPath"], "old_string": original.strip(), "new_string": revised.strip()}, "child-edit"),
                AIMessage(content="Working proposal revised. Host import remains required.\nSTATUS: DONE"),
                _message("read_file", {"file_path": agent_file["virtualPath"]}, "main-read"),
                _message("write_shader_workspace_organization_file", {"organization_id": "test-organization", "source_file": agent_file["path"]}, "import"),
                AIMessage(content="Host saved the revised Source; replay was not applied."),
            ]
            return {function.__name__: _langchain_tool(function.__name__, function)
                    for function in (read_shader_workspace_file, write_shader_workspace_organization_file)}

    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        tool_session_factory=lambda request, config, event_root: Session(request, config, event_root),
        model_factory=lambda _profile, _model: model,
    )
    result = await engine.run(RunRequest(
        prompt="Reorganize this Shader.", task_id="physical-api-import", instance_id="offline-instance",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY"),
    ))
    assert result.terminal_status == "completed", result.error
    assert delivered.read_text(encoding="utf-8") == revised
    assert user_source.read_text(encoding="utf-8") == original
    assert observed["import"]["path"] == observed["file"]["path"]
    assert not observed["root"].parent.exists()
    tool_messages = {message.tool_call_id: message for messages in model.seen_messages
                     for message in messages if isinstance(message, ToolMessage)}
    assert original.strip() in str(tool_messages["child-read"].content)
    assert revised.strip() in str(tool_messages["main-read"].content)
    assert all("execute" not in names for names in model.bound_tool_names)


@pytest.mark.asyncio
async def test_capture_free_api_file_tools_confine_reads_and_writes_to_owned_root(tmp_path, monkeypatch):
    outside = tmp_path / "user-source.hlsl"
    outside.write_text("PRIVATE USER SOURCE", encoding="utf-8")
    created = []
    original_create = deepagents.create_deep_agent

    @functools.wraps(original_create)
    def capture_create(*args, **kwargs):
        created.append(kwargs)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", capture_create)
    model = ToolCallingFakeModel(responses=[
        _message("read_file", {"file_path": str(outside)}, "outside-read"),
        _message("edit_file", {"file_path": "../user-source.hlsl", "old_string": "PRIVATE", "new_string": "CHANGED"}, "escape-edit"),
        _message("write_file", {"file_path": "../escaped.txt", "content": "escape"}, "escape-write"),
        _message("write_file", {"file_path": "/notes/owned.txt", "content": "OWNED PHYSICAL FILE"}, "owned-write"),
        _message("read_file", {"file_path": "/notes/owned.txt"}, "owned-read"),
        AIMessage(content="Only the temporary working file was created."),
    ])
    engine = DeepAgentsEngine(
        catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        model_factory=lambda _profile, _model: model,
    )
    result = await engine.run(RunRequest(prompt="Make a temporary note.", task_id="confined-api-files",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED_OFFLINE_KEY")))
    assert result.terminal_status == "completed", result.error
    messages = {message.tool_call_id: message for batch in model.seen_messages
                for message in batch if isinstance(message, ToolMessage)}
    assert "PRIVATE USER SOURCE" not in str(messages["outside-read"].content)
    assert messages["outside-read"].status == "error"
    assert messages["escape-edit"].status == "error"
    assert messages["escape-write"].status == "error"
    assert "OWNED PHYSICAL FILE" in str(messages["owned-read"].content)
    assert outside.read_text(encoding="utf-8") == "PRIVATE USER SOURCE"
    assert not list(tmp_path.rglob("escaped.txt"))
    backend = created[0]["backend"]
    assert backend.virtual_mode is True
    assert backend.cwd.parent.parent == (tmp_path / "turns").resolve()
    assert (backend.cwd / "notes" / "owned.txt").read_text(encoding="utf-8") == "OWNED PHYSICAL FILE"
    assert "Local task outputs are retained" not in result.text
    assert all("execute" not in names for names in model.bound_tool_names)
