"""Bread UI tool registration, effect declarations and UI-thread lifecycle."""
import ast
from pathlib import Path
import sys
import types
from unittest import mock

import pytest

from src import server
from replaylab_agent.policy import (
    CORE_DIRECT_TOOLS, READ_ONLY_TOOLS, EFFECTFUL_TOOLS,
    tool_effects, tool_admissible_at_effect_ceiling,
)


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"


def test_ui_tools_are_registered_directly_with_conservative_effects():
    registered = {kwargs.get("name") or function.__name__: function
                  for function, _args, kwargs in server.mcp._registrations}
    for name in ("get_application_ui_state", "control_application_ui"):
        assert registered[name] is getattr(server, name)
        assert name in CORE_DIRECT_TOOLS
    assert "get_application_ui_state" in READ_ONLY_TOOLS
    assert tool_effects("get_application_ui_state") == frozenset()
    assert "control_application_ui" in EFFECTFUL_TOOLS
    assert tool_effects("control_application_ui") == {"ui", "replay", "workspace", "project"}
    assert not tool_admissible_at_effect_ceiling("control_application_ui", {"ui"})
    assert tool_admissible_at_effect_ceiling("control_application_ui", {"ui", "replay", "workspace", "project"})


def test_host_forwards_ui_arguments_without_replay_or_asset_preparation():
    with mock.patch.object(server, "_call", return_value="receipt") as call:
        assert server.get_application_ui_state("ui-123", "roughness", 20, 40, [2, 3]) == "receipt"
        call.assert_called_once_with("get_application_ui_state", {
            "target_id": "ui-123", "query": "roughness", "offset": 20,
            "limit": 40, "row_path": [2, 3]})
    with mock.patch.object(server, "_call", return_value="queued") as call:
        assert server.control_application_ui("set_value", "ui-123", {"value": 0.8}) == "queued"
        call.assert_called_once_with("control_application_ui", {
            "action": "set_value", "target_id": "ui-123", "arguments": {"value": 0.8}})


def _bridge_functions(monkeypatch):
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    names = {"_application_ui", "_get_application_ui_state", "_control_application_ui",
             "_shutdown_application_ui", "_selection_independent_request"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    thread = {"on_ui": False}
    callbacks = {"workspace": 0, "console": 0}

    class Controller:
        def __init__(self, ctx, window_callbacks):
            assert thread["on_ui"]
            self.ctx, self.window_callbacks, self.closed = ctx, window_callbacks, False

        def inspect(self, params):
            assert thread["on_ui"]
            return {"params": params, "status": "snapshot"}

        def perform(self, params):
            assert thread["on_ui"]
            return {"params": params, "status": "queued"}

        def shutdown(self):
            self.closed = True

    module = types.ModuleType("test_ui_bridge.project_ui_control")
    module.ApplicationUIController = Controller
    monkeypatch.setitem(sys.modules, module.__name__, module)
    action = mock.Mock()
    window = mock.Mock()
    window.findChild.return_value = action
    ctx = mock.Mock()
    ctx.GetMainWindow.return_value.Widget.return_value = window
    panel = types.SimpleNamespace(top=mock.Mock())
    panel.top.isVisible.return_value = True
    scope = {"__package__": "test_ui_bridge", "HAS_UI": True, "_ctx": ctx, "sys":sys,
             "_safe_int":lambda value, default=0: int(value or default),
             "QtWidgets": types.SimpleNamespace(QAction=object),
             "_application_ui_controller": None,
             "_shader_workspace_ui": panel, "_shader_console_ui": panel,
             "_shader_workspace_callbacks": [lambda: callbacks.update(workspace=callbacks["workspace"] + 1)],
             "_shader_console_callbacks": [lambda: callbacks.update(console=callbacks["console"] + 1)]}

    def invoke(callback):
        assert not thread["on_ui"]
        thread["on_ui"] = True
        try:
            return callback()
        finally:
            thread["on_ui"] = False

    scope["_invoke_on_ui_sync"] = invoke
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(BRIDGE), "exec"), scope)
    return scope, callbacks, action


def test_bridge_creates_controller_on_ui_once_routes_callbacks_and_shuts_it_down(monkeypatch):
    scope, callbacks, action = _bridge_functions(monkeypatch)
    assert scope["_get_application_ui_state"]({"query": "color"})["status"] == "snapshot"
    controller = scope["_application_ui_controller"]
    assert scope["_control_application_ui"]({"action": "focus", "target_id": "ui-a"})["status"] == "queued"
    assert scope["_application_ui_controller"] is controller
    for name in ("shader-workspace", "console", "bread-ai"):
        controller.window_callbacks[name]()
    assert callbacks == {"workspace": 1, "console": 1}
    action.trigger.assert_called_once()
    scope["_shutdown_application_ui"]()
    assert controller.closed and scope["_application_ui_controller"] is None
    scope["_shutdown_application_ui"]()  # Idempotent integration teardown.


@pytest.mark.parametrize("failure", ["missing", "hidden"])
def test_window_callback_cannot_report_success_when_panel_creation_was_swallowed(monkeypatch, failure):
    scope, _, _ = _bridge_functions(monkeypatch)
    scope["_get_application_ui_state"]({})
    if failure == "missing":
        scope["_shader_console_ui"] = None
    else:
        scope["_shader_console_ui"].top.isVisible.return_value = False
    with pytest.raises(RuntimeError, match="could not be opened|did not become visible"):
        scope["_application_ui_controller"].window_callbacks["console"]()


def test_ui_queries_and_actions_do_not_become_stale_when_they_select_an_event(monkeypatch):
    scope, _, _ = _bridge_functions(monkeypatch)
    for name in ("get_application_ui_state", "control_application_ui"):
        assert scope["_selection_independent_request"]({"method": name, "params": {}})
    assert not scope["_selection_independent_request"]({"method": "inspect_event", "params": {}})
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    table = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "METHODS" for target in node.targets))
    handlers = {key.value: value.id for key, value in zip(table.keys, table.values)
                if isinstance(key, ast.Constant) and isinstance(value, ast.Name)}
    assert handlers["get_application_ui_state"] == "_get_application_ui_state"
    assert handlers["control_application_ui"] == "_control_application_ui"
