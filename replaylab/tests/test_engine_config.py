from __future__ import annotations

import json
from pathlib import Path

import pytest

from replaylab_agent import native_mcp
from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.engine_config import load_engine_config
from replaylab_agent.profiles import Profile


def _config(tmp_path: Path, values: dict):
    path = tmp_path / "agent-engine.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return load_engine_config(path)


def test_default_runtime_limit_allows_long_tasks_and_remains_configurable(tmp_path) -> None:
    assert _config(tmp_path, {}).main_timeout == 86_400.0
    assert _config(tmp_path, {"main_timeout": 1_800}).main_timeout == 1_800.0
    assert _config(tmp_path, {"main_timeout": 200_000}).main_timeout == 86_400.0


@pytest.mark.parametrize("value", [0, 1, 37, 1024, 1_000_000, -1, 2_000_000, True, 3.5, "37", None])
def test_legacy_tool_budget_setting_is_ignored(tmp_path, value) -> None:
    assert not hasattr(_config(tmp_path, {"max_tool_calls": value}), "max_tool_calls")


def test_removed_child_and_tool_limits_do_not_change_runtime(tmp_path) -> None:
    legacy = {"child_timeout": 1, "max_children": 1, "max_parallel": 1, "max_child_tool_calls": 1}
    config = _config(tmp_path, legacy)
    assert not hasattr(config, "max_tool_calls")
    assert all(not hasattr(config, name) for name in legacy)
    assert not hasattr(_config(tmp_path, {**legacy, "max_tool_calls": 7}), "max_tool_calls")


@pytest.mark.parametrize("preset", ["openai", "claude-code-acp", "codex-app-server"])
@pytest.mark.parametrize("configured_budget", [None, 7])
def test_api_and_native_routes_do_not_forward_legacy_budget(tmp_path, monkeypatch, preset, configured_budget) -> None:
    host = tmp_path / "BreadMCP.exe"
    monkeypatch.setattr(native_mcp, "find_replaylab_mcp", lambda _path: host)
    values = {} if configured_budget is None else {"max_tool_calls": configured_budget}
    _config(tmp_path, values)
    context = ProviderContext(
        profile=Profile(id="budget", preset=preset, title="Budget test"),
        model="", instance_id="budget-test", context={}, intent=None,
    )
    route = native_mcp.build_native_mcp_route(
        context, workspace=tmp_path, event_root=tmp_path / "events", allowed_effects=(),
    )
    assert route is not None
    assert "--max-tool-calls" not in route.args
