from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import deepagents
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from replaylab_agent.acp_session import PromptResult
from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.deep_engine import DeepAgentsEngine
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.native_mcp import build_native_mcp_route
from replaylab_agent.profiles import Profile
from test_deepagents_framework_integration import ToolCallingFakeModel, _catalog, _message


def assessment(verdict="complete"):
    return {"verdict": verdict, "criteria": [{"requirement": "Answer the user's question", "status": "satisfied", "evidence": "The answer contains the requested explanation."}],
            "findings": [], "next_actions": [], "blocker": ""}


@pytest.mark.asyncio
async def test_native_visual_review_continues_with_real_inventory_without_replaying_work(tmp_path,monkeypatch):
    task_id='native-visual-presentation'
    store=TaskReportStore(task_id,tmp_path/'events');prompts=[]
    class Session:
        def __init__(self,*args,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def close(self):pass
        async def prompt(self,text,**kwargs):
            prompts.append(text)
            if len(prompts)==1:
                store.record_task_review(**assessment())
                store.register_reply_visual('a'*64,presentation=dict(cards=[dict(type='diagram',title='Flow',nodes=[
                    dict(id='calculation',imageCount=0,codeBlockCount=1,hasDetails=True)])]))
                return PromptResult('First answer',[],'end_turn',0)
            assert 'visual_review' in text and 'imageCount' in text
            store.record_task_review(**assessment(),visual_review='Reviewed the diagram and key code against the question.')
            return PromptResult('Reviewed visual explanation',[],'end_turn',0)
    monkeypatch.setattr('replaylab_agent.native_engine.build_native_mcp_route',lambda *args,**kwargs:None)
    monkeypatch.setattr(ProviderContext,'agent_argv',lambda self:['fake-agent'])
    engine=NativeAgentEngine(catalog=_catalog(tmp_path),config=OrchestratorConfig(event_root=tmp_path/'events',
        workspace_base=tmp_path/'turns',log_root=tmp_path/'logs'),acp_session_factory=Session)
    result=await engine.run(RunRequest(prompt='Explain this result.',task_id=task_id,
        profile=Profile(id='native',preset='claude-code-acp',options={'allow_unsandboxed_agent':True})))
    assert result.terminal_status=='completed' and result.text=='Reviewed visual explanation'
    assert len(prompts)==2


@pytest.mark.asyncio
async def test_api_accepts_current_visual_assessment_without_mandatory_extra_views(tmp_path,monkeypatch):
    begin=TaskReportStore.begin_task_review
    def begin_with_publication(self,*args,**kwargs):
        result=begin(self,*args,**kwargs)
        self.register_reply_visual('a'*64,presentation=dict(cards=[dict(type='image',title='One requested image',nodes=[])]))
        return result
    monkeypatch.setattr(TaskReportStore,'begin_task_review',begin_with_publication)
    model=ToolCallingFakeModel(responses=[_message('record_task_review',dict(assessment(),
        visual_review='The requested image and caption are sufficient; no geometry or code is relevant.'),'review'),
        AIMessage(content='Reviewed image answer')])
    engine=DeepAgentsEngine(catalog=_catalog(tmp_path),config=OrchestratorConfig(event_root=tmp_path/'events',
        workspace_base=tmp_path/'turns',log_root=tmp_path/'logs'),deepagents_module=deepagents,model_factory=lambda *args:model)
    result=await engine.run(RunRequest(prompt='Show that image.',task_id='api-visual',
        profile=Profile(id='offline',preset='openai',model='offline',api_key_env='UNUSED')))
    assert result.terminal_status=='completed'and result.text=='Reviewed image answer'
    state=TaskReportStore('api-visual',tmp_path/'events').task_review_state()
    assert state['fresh']and state['review']['visualReview']['visualId']=='a'*64


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['api', 'native'])
async def test_transports_forward_the_full_layered_visual_review(tmp_path, monkeypatch, transport):
    from src.task_completion_review import VISUAL_CHECKS

    begin = TaskReportStore.begin_task_review

    def begin_with_layered_publication(self, fingerprint, **_):
        result = begin(self, fingerprint, marked_render_target=True)
        self.register_reply_visual('a'*64, dict(cards=[dict(type='diagram')], shaderCodeGaps=[],
            markedEffect=dict(needsFinalReplay=[], needsDiagram=False, missingLayerViews=[],
                              missingFinalLayerComparisons=[], missingInputTextures=[],
                              relatedEvents=[21, 22], combinedBaselineMismatch=False)))
        return result

    monkeypatch.setattr(TaskReportStore, 'begin_task_review', begin_with_layered_publication)
    checks = {key: f'Reviewed {key} evidence' for key in VISUAL_CHECKS}
    answer = dict(overview='Two layers create the effect in this capture.',
                  steps=[dict(text='The first layer defines the shape.',events=[21]),
                         dict(text='The second adds a soft veil.',events=[22])],
                  combined='Both blend over the same background.')
    review = dict(assessment(), visual_review='Checked the complete layered answer',
                  visual_checks=checks, reviewed_answer=answer)
    status = 'The final review is complete.'
    task_id = transport + '-layered'
    config = OrchestratorConfig(event_root=tmp_path/'events', workspace_base=tmp_path/'turns',
                               log_root=tmp_path/'logs')
    store = TaskReportStore(task_id, config.event_root)
    if transport == 'api':
        model = ToolCallingFakeModel(responses=[_message('record_task_review', review, 'review'),
                                                AIMessage(content=status)])
        engine = DeepAgentsEngine(catalog=_catalog(tmp_path), config=config,
                                  deepagents_module=deepagents, model_factory=lambda *args: model)
        profile = Profile(id='offline', preset='openai', model='offline', api_key_env='UNUSED')
    else:
        class Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def close(self):
                pass

            async def prompt(self, text, **kwargs):
                store.record_task_review(**review)
                return PromptResult(status, [], 'end_turn', 0)

        monkeypatch.setattr('replaylab_agent.native_engine.build_native_mcp_route', lambda *args, **kwargs: None)
        monkeypatch.setattr(ProviderContext, 'agent_argv', lambda self: ['fake-agent'])
        engine = NativeAgentEngine(catalog=_catalog(tmp_path), config=config, acp_session_factory=Session)
        profile = Profile(id='native', preset='claude-code-acp', options={'allow_unsandboxed_agent': True})
    result = await engine.run(RunRequest(prompt='How is this effect made?', task_id=task_id, profile=profile))
    assert result.terminal_status == 'completed', result.error
    assert result.text.startswith(answer['overview']) and '2. The second' in result.text
    assert 'final review is complete' not in result.text
    state = store.task_review_state()
    assert state['fresh'] and state['review']['visualReview']['checks'] == checks


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_first", [False, True])
async def test_plain_api_answer_uses_local_review_without_host_or_extra_work(tmp_path, missing_first):
    script = ([AIMessage(content="Initial answer")] if missing_first else []) + [
        _message("record_task_review", assessment(), "review"), AIMessage(content="Reviewed answer")]
    model = ToolCallingFakeModel(responses=script)
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Explain a simple concept.", task_id="generic-api",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED")))
    assert result.terminal_status == "completed", result.error
    assert result.text == "Reviewed answer"
    assert len(model.seen_messages) == (3 if missing_first else 2)
    final = model.seen_messages[-1]
    assert sum(isinstance(item, ToolMessage) for item in final) == 1
    assert sum(isinstance(item, HumanMessage) and "completion self-review" in str(item.content) for item in final) == int(missing_first)


@pytest.mark.asyncio
async def test_api_operation_after_review_requires_fresh_review_without_repeating_operation(tmp_path):
    model = ToolCallingFakeModel(responses=[
        _message("record_task_review", assessment(), "too-early"),
        _message("write_file", {"file_path": "/note.txt", "content": "ONE WRITE"}, "write-once"),
        AIMessage(content="Done"),
        _message("record_task_review", assessment(), "current"), AIMessage(content="Current reviewed answer"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Write a temporary note.", task_id="generic-after-write",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED")))
    assert result.terminal_status == "completed", result.error
    retained = list((tmp_path / "turns").glob("*/agent-workspace/note.txt"))
    assert len(retained) == 1 and retained[0].read_text(encoding="utf-8") == "ONE WRITE"
    assert "Local task outputs are retained" not in result.text and "Shader draft" not in result.text
    final = model.seen_messages[-1]
    assert sum(isinstance(item, ToolMessage) and item.tool_call_id == "write-once" for item in final) == 1
    assert any(isinstance(item, HumanMessage) and "completion self-review" in str(item.content) for item in final)


@pytest.mark.asyncio
async def test_native_completed_report_survives_cleanup_and_has_output_location(tmp_path, monkeypatch):
    task_id = "native-report"
    store = TaskReportStore(task_id, tmp_path / "events")
    paths = []

    class Session:
        def __init__(self, *args, **kwargs):
            self.cwd = Path(kwargs["cwd"])
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def close(self):
            pass
        async def prompt(self, text, **kwargs):
            report = self.cwd / "report.md"
            report.write_text("Completed report", encoding="utf-8")
            paths.append(report)
            store.record_task_review(**assessment())
            return PromptResult("Saved report: " + str(report), [], "end_turn", 0)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["fake-agent"])
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        acp_session_factory=Session)
    result = await engine.run(RunRequest(prompt="Create a report.", task_id=task_id,
        profile=Profile(id="native", preset="claude-code-acp", options={"allow_unsandboxed_agent": True})))
    assert result.terminal_status == "completed", result.error
    assert len(paths) == 1 and paths[0].read_text(encoding="utf-8") == "Completed report"
    assert str(paths[0]) in result.text and "Local task outputs are retained" not in result.text
    assert "Shader draft" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"extra": True}, {"verdict": "complete"}])
async def test_api_malformed_review_revokes_earlier_pass_and_can_be_repaired(tmp_path, invalid):
    payload = {**assessment(), **invalid} if "extra" in invalid else invalid
    model = ToolCallingFakeModel(responses=[
        _message("record_task_review", assessment(), "earlier-pass"),
        _message("record_task_review", payload, "malformed"), AIMessage(content="Unreviewed answer"),
        _message("record_task_review", assessment(), "repaired"), AIMessage(content="Repaired review"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Explain a concept.", task_id="malformed-api",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED")))
    assert result.terminal_status == "completed" and result.text == "Repaired review", result.error
    assert any(isinstance(item, HumanMessage) and "completion self-review" in str(item.content) for item in model.seen_messages[-1])


@pytest.mark.asyncio
async def test_plain_text_needs_work_is_repaired_without_inventing_work_tools(tmp_path):
    initial = assessment("needs_work")
    initial.update(findings=["Missing the example"], next_actions=["Add the example to the answer"])
    model = ToolCallingFakeModel(responses=[
        _message("record_task_review", initial, "gap"), AIMessage(content="Initial explanation"),
        _message("record_task_review", assessment(), "repaired"), AIMessage(content="Explanation and example"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Explain with an example.", task_id="plain-repair",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED")))
    assert result.terminal_status == "completed" and result.text == "Explanation and example", result.error
    assert {item.name for item in model.seen_messages[-1] if isinstance(item, ToolMessage)} == {"record_task_review"}


@pytest.mark.asyncio
async def test_api_blocked_review_continues_same_graph_to_untried_recovery(tmp_path):
    initial = assessment("blocked")
    initial.update(blocker="An alternate evidence path has not been checked", next_actions=["Use the available alternate path"])
    model = ToolCallingFakeModel(responses=[
        _message("record_task_review", initial, "initial-blocked"), AIMessage(content="Incomplete result"),
        _message("write_file", {"file_path": "/recovered.md", "content": "Recovered delivery"}, "recovery-once"),
        _message("record_task_review", assessment(), "recovered-review"), AIMessage(content="Recovered completed result"),
    ])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="Create the requested delivery.", task_id="api-blocker-recovery",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED")))
    assert result.terminal_status == "completed" and "Recovered completed result" in result.text, result.error
    messages = model.seen_messages[-1]
    assert sum(isinstance(item, ToolMessage) and item.tool_call_id == "recovery-once" for item in messages) == 1
    assert sum(isinstance(item, HumanMessage) and "Reassess the reported blocker" in str(item.content) for item in messages) == 1
    assert sum(isinstance(item, HumanMessage) and "Create the requested delivery." in str(item.content) for item in messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["acp", "codex"])
async def test_native_blocked_review_recovers_without_restarting_agent(tmp_path, monkeypatch, protocol):
    task_id = "native-recovery"
    store = TaskReportStore(task_id, tmp_path / "events")
    prompts, instances = [], []

    class Session:
        def __init__(self, *args, **kwargs):
            instances.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def close(self):
            pass
        async def prompt(self, text, **kwargs):
            prompts.append(text)
            if len(prompts) == 1:
                payload = assessment("blocked")
                payload.update(blocker="Untried authorized alternative", next_actions=["Inspect alternate evidence"])
                store.record_task_review(**payload)
            else:
                assert "Reassess the reported blocker" in text
                kwargs["on_timeline"]({"activityId": "recovery-work", "activityKind": "commandExecution",
                    "status": "completed", "workIdentity": "b" * 64})
                store.record_task_review(**assessment())
            return PromptResult("Recovered answer", ["Recovered answer"], "end_turn", 0)
        async def run(self, text, **kwargs):
            assert kwargs["keep_thread"] is True
            return await self.prompt(text, **kwargs)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["fake", "app-server"])
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        acp_session_factory=Session, codex_connector_factory=Session)
    result = await engine.run(RunRequest(prompt="Complete this task.", task_id=task_id,
        profile=Profile(id="native", preset="codex" if protocol == "codex" else "claude-code-acp",
                        options={} if protocol == "codex" else {"allow_unsandboxed_agent": True})))
    assert result.terminal_status == "completed", result.error
    assert len(instances) == 1 and len(prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["acp", "codex"])
async def test_native_complete_source_continues_only_missing_panel_publication(tmp_path, monkeypatch, protocol):
    task_id = "native-semantic-delivery"
    store = TaskReportStore(task_id, tmp_path / "events")
    prompts, instances, publications = [], [], []
    target = {"workspacePath": "shader", "sourceSha256": "a" * 64,
              "changed": True, "fresh": True, "saved": True,
              "review": {"verdict": "complete"},
              "cbufferSemanticDelivery": {"required": True, "status": "pending", "sourceSha256": "a" * 64,
                  "bindingCoverage": {"bindingCount": 5, "reviewedBindingCount": 1},
                  "resourceNamingDelivery": {"required": True, "status": "pending", "sourceSha256": "a" * 64}}}

    class Session:
        def __init__(self, *args, **kwargs):
            instances.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def close(self):
            pass
        async def prompt(self, text, **kwargs):
            prompts.append(text)
            if len(prompts) > 1:
                assert "publish_cbuffer_aliases" in text
                assert "do not rewrite source, repeat source review, compile" in text
                assert "record_shader_resource_naming_review" in text
                publications.append("a" * 64)
                target["cbufferSemanticDelivery"]["bindingCoverage"]["reviewedBindingCount"] = min(5, len(prompts) * 2 - 1)
                if len(prompts) == 4:
                    target["cbufferSemanticDelivery"]["status"] = "complete"
                    target["cbufferSemanticDelivery"]["resourceNamingDelivery"]["status"] = "complete"
            store.record_task_review(**assessment())
            return PromptResult("Source and panel delivery complete", [], "end_turn", 0)
        async def run(self, text, **kwargs):
            assert kwargs["keep_thread"] is True
            return await self.prompt(text, **kwargs)

    monkeypatch.setattr("replaylab_agent.native_engine.review_state", lambda _store: {"targets": [target]})
    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["fake", "app-server"])
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        acp_session_factory=Session, codex_connector_factory=Session)
    result = await engine.run(RunRequest(prompt="Organize this Shader.", task_id=task_id,
        profile=Profile(id="native", preset="codex" if protocol == "codex" else "claude-code-acp",
                        options={} if protocol == "codex" else {"allow_unsandboxed_agent": True})))
    assert result.terminal_status == "completed", result.error
    assert len(instances) == 1 and len(prompts) == 4
    assert publications == ["a" * 64] * 3


@pytest.mark.asyncio
async def test_api_same_graph_finishes_all_binding_naming_without_replaying_source(tmp_path, monkeypatch):
    target = {"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True,
              "fresh": True, "saved": True, "review": {"verdict": "complete"}}
    pending_counts = iter([1, 3, 5, 5])
    reads = []

    def current_state(_store):
        count = next(pending_counts)
        reads.append(count)
        complete = len(reads) == 4
        return {"targets": [{**target, "cbufferSemanticDelivery": {
            "required": True, "status": "complete" if complete else "pending", "sourceSha256": "a" * 64,
            "bindingCoverage": {"bindingCount": 5, "reviewedBindingCount": count},
            "resourceNamingDelivery": {"required": True, "status": "complete" if complete else "pending",
                                       "sourceSha256": "a" * 64}}}]}

    monkeypatch.setattr("replaylab_agent.deep_engine.review_state", current_state)
    model = ToolCallingFakeModel(responses=[item for index in range(4) for item in (
        _message("record_task_review", assessment(), "review-" + str(index)), AIMessage(content="Delivery updated"))])
    engine = DeepAgentsEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        deepagents_module=deepagents, model_factory=lambda *args: model)
    result = await engine.run(RunRequest(prompt="整理 shader", task_id="api-all-binding-naming",
        profile=Profile(id="offline", preset="openai", model="offline", api_key_env="UNUSED")))
    assert result.terminal_status == "completed", result.error
    assert reads == [1, 3, 5, 5]
    final = model.seen_messages[-1]
    continuations = [str(item.content) for item in final if isinstance(item, HumanMessage)
                     and "record_shader_resource_naming_review" in str(item.content)]
    assert len(continuations) == 3
    assert all("do not rewrite source, repeat source review, compile" in text for text in continuations)
    assert {item.name for item in final if isinstance(item, ToolMessage)} == {"record_task_review"}


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["acp", "codex"])
@pytest.mark.parametrize("mode", ["complete", "missing", "stale", "blocked"])
async def test_native_generic_review_reuses_session_and_reports_honest_outcome(tmp_path, monkeypatch, protocol, mode):
    task_id = "native-general"
    calls = []
    instances = []
    store = TaskReportStore(task_id, tmp_path / "events")

    class Session:
        def __init__(self, *args, **kwargs):
            instances.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def close(self):
            pass
        async def prompt(self, text, **kwargs):
            calls.append(text)
            if len(calls) == 1 and mode == "stale":
                store.record_task_review(**assessment())
                kwargs["on_timeline"]({"activityId": "actual-work", "activityKind": "commandExecution", "status": "completed", "workIdentity": "a" * 64})
            if len(calls) == 2 and mode != "missing":
                payload = assessment()
                if mode == "blocked":
                    payload.update(verdict="blocked", blocker="Required user input is unavailable")
                store.record_task_review(**payload)
            return PromptResult("Provider answer", ["Provider answer"], "end_turn", 0)
        async def run(self, text, **kwargs):
            assert kwargs["keep_thread"] is True
            return await self.prompt(text, **kwargs)

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(ProviderContext, "agent_argv", lambda self: ["fake", "app-server"])
    engine = NativeAgentEngine(catalog=_catalog(tmp_path),
        config=OrchestratorConfig(event_root=tmp_path / "events", workspace_base=tmp_path / "turns", log_root=tmp_path / "logs"),
        acp_session_factory=Session, codex_connector_factory=Session)
    result = await engine.run(RunRequest(prompt="Answer this question.", task_id=task_id,
        profile=Profile(id="native", preset="codex" if protocol == "codex" else "claude-code-acp", options={} if protocol == "codex" else {"allow_unsandboxed_agent": True})))
    assert len(instances) == 1 and len(calls) == (3 if mode == "blocked" else 2), result.error
    assert result.terminal_status == ("partial" if mode in {"missing", "blocked"} else "completed"), result.error
    if mode == "blocked":
        # The public answer keeps the honest status, provider answer and cause;
        # the review evidence stays in the record instead of becoming a second
        # generated answer in both native transports.
        assert result.text == (
            "The task is incomplete.\n\nProvider answer\n\n"
            "Reason: Required user input is unavailable"
        )
        state = store.task_review_state()
        assert state["fresh"] is True
        assert state["review"]["verdict"] == "blocked"
        assert state["review"]["blocker"] == "Required user input is unavailable"
        assert state["review"]["criteria"] == assessment()["criteria"]
        assert "requested explanation" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"extra": True}, {"verdict": "complete"}])
async def test_native_local_only_proxy_exposes_and_records_review_without_capture(tmp_path, invalid):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    task_id = "proxy-general"
    store = TaskReportStore(task_id, tmp_path / "events")
    store.begin_task_review("a" * 64)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ProviderContext(profile=Profile(id="native", preset="codex"), task_id=task_id,
                              model="", instance_id="", context={}, intent=None)
    route = build_native_mcp_route(context, workspace=workspace, event_root=tmp_path / "events", allowed_effects=())
    assert route is not None and "--host" not in route.args
    assert "record_task_review" in route.visible_tools
    async with stdio_client(StdioServerParameters(command=route.command, args=list(route.args), env={**os.environ, **route.env})) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            assert "record_task_review" in {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("record_task_review", assessment())
            assert not result.isError, result
            assert store.task_review_state()["fresh"] is True
            payload = {**assessment(), **invalid} if "extra" in invalid else invalid
            failed = await client.call_tool("record_task_review", payload)
            assert failed.isError
            invalidated = store.task_review_state()
            assert not invalidated["fresh"] and invalidated["review"] is None
            assert invalidated["reviewError"]
            repaired = await client.call_tool("record_task_review", assessment())
            assert not repaired.isError
    state = store.task_review_state()
    assert state["fresh"] is True and state["review"]["verdict"] == "complete"
