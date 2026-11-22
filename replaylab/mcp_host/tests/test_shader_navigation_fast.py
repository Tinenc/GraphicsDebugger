import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests import test_shader_workspace_panel as panel_tests


def test_known_binding_opens_even_while_background_refresh_failed_or_is_busy(tmp_path):
    module = panel_tests._load_without_qt()
    tree = ast.parse(panel_tests.MODULE_PATH.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "navigate_shader_target")
    namespace = {"shader_target_workspace_ids": module.shader_target_workspace_ids, "_normal_path": module._normal_path}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "navigation", "exec"), namespace)
    binding = {"capturePath": str(tmp_path / "frame.rdc"), "captureSessionId": "a"*32}
    workspace = dict(binding, resourceId=123, stage="fragment", entryPoint="main")
    panel = SimpleNamespace(_alive=True, _shutting_down=False, _capture_binding=binding,
        _current_capture_binding=lambda: binding, _workspaces={"saved": workspace},
        _current_workspace_id=lambda: "saved", navigate_source_link=Mock(return_value=True),
        refresh=Mock(side_effect=AssertionError("must reuse bound document")),
        _snapshot_running=True, _target_refresh_error=True)
    result = namespace["navigate_shader_target"](panel, dict(workspace, refresh=True))
    assert result["accepted"] is True
    panel.refresh.assert_not_called()
    panel.navigate_source_link.assert_called_once_with({"workspaceId": "saved"})


def test_native_replacement_rid_routes_to_its_current_session_original():
    tree = ast.parse(panel_tests.BRIDGE_PATH.read_text(encoding="utf-8"))
    method = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "navigate_native_shader_source_json")
    ui = SimpleNamespace(navigate_shader_target=Mock(return_value={"accepted": True}))
    namespace = {"json": json, "_shader_workspace_ui": ui,
        "_shader_workspace_capture_binding": lambda: {"captureSessionId": "a"*32},
        "_shared_shader_replacements": {"one": {"captureSessionId": "a"*32,
            "targetResourceId": 900, "expectedOriginalResourceId": 123}}}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "navigation", "exec"), namespace)
    namespace[method.name](json.dumps({"target": True, "resourceId": "900"}))
    assert ui.navigate_shader_target.call_args.args[0]["resourceId"] == "123"
