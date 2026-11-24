"""A user pause ends the turn without invoking providers or unfinished workflows."""
import json

import pytest

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_diagnostics import close_logging
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.deep_engine import DeepAgentsEngine
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.profiles import Profile
from replaylab_agent.user_control import requests_pause


@pytest.mark.parametrize("text", ["暂停", "先暂停一下我补齐工具后继续后面内容", "请暂停当前任务。", "停止工作", "暂停一下，等我修复工具后再继续", "先暂停，补齐工具后再继续", "Pause for now", "stop and wait for me"])
def test_explicit_pause(text):
    assert requests_pause(text)


@pytest.mark.parametrize("text", ["我让他暂停之后怎么还在运行？", "不要暂停", "暂停功能有问题，帮我修复", '说明“暂停”的含义', "pause and explain the error", "继续"])
def test_discussion_or_negation_is_not_control(text):
    assert not requests_pause(text)


def test_history_cannot_pause_new_work_or_resume_a_current_pause():
    def envelope(current, previous):
        return json.dumps({"schema": "replaylab-conversation/v5", "messages": [{"role": "user", "content": previous}],
            "request": {"role": "user", "content": current}})
    assert requests_pause(envelope("先暂停一下我补齐工具后继续后面内容", "继续还原 Unity"))
    assert not requests_pause(envelope("继续还原 Unity", "暂停"))


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_pause_has_a_terminal_event_and_does_not_create_a_provider_or_review(tmp_path, native):
    def forbidden(*args, **kwargs):
        raise AssertionError("Pause must not initialize provider, tools or task review")
    engine_type = NativeAgentEngine if native else DeepAgentsEngine
    options = {"codex_connector_factory": forbidden} if native else {"model_factory": forbidden}
    engine = engine_type(catalog=CatalogView(skills={}, roots=()), config=OrchestratorConfig(
        event_root=tmp_path / "events", log_root=tmp_path / "logs", workspace_base=tmp_path / "work"), **options)
    try:
        result = await engine.run(RunRequest(prompt="先暂停一下我补齐工具后继续后面内容", task_id="pause",
            profile=Profile(id="offline", preset="codex" if native else "openai", model="offline")))
    finally:
        close_logging()
    assert result.terminal_status == "completed"
    assert "已暂停" in result.text
    events = [json.loads(line) for line in (tmp_path / "events/pause.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["kind"] == "completed"
    assert not (tmp_path / "events/pause.report-state.json").exists()
