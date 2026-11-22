"""Fallback navigation must never look like an AI-reviewed calculation outline."""
import ast
from types import SimpleNamespace
from unittest import mock

import pytest

from test_shader_workspace_panel import MODULE_PATH, _load_without_qt


def status_method():
    tree = ast.parse(MODULE_PATH.read_text("utf8"))
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef)
                and item.name == "_update_stage_outline_status")
    scope = {"QtCore": SimpleNamespace(QCoreApplication=SimpleNamespace(
        translate=lambda context, text: text))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MODULE_PATH), "exec"), scope)
    return scope[node.name]


@pytest.mark.parametrize("state,expected", [
    ({"state": "reviewed", "complete": True, "source": "semantic-overview"}, "Calculation outline reviewed"),
    ({"state": "reviewed", "complete": False, "source": "semantic-overview"}, "No calculation outline"),
    ({"state": "provisional", "source": "source-derived", "stageCount": 3}, "Temporary navigation"),
    ({"state": "provisional", "source": "legacy-annotation"}, "Temporary navigation"),
    ({"state": "provisional", "source": "semantic-overview"}, "Calculation outline needs review"),
    ({"state": "stale", "source": "semantic-overview"}, "Source changed"),
    ({"state": "missing"}, "No calculation outline"),
])
def test_directory_status_distinguishes_analysis_from_temporary_navigation(state, expected):
    panel = SimpleNamespace(stage_status=mock.Mock())
    status_method()(panel, {"stageOutlineState": state}, True)
    assert panel.stage_status.setText.call_args.args[0].startswith(expected)


@pytest.mark.parametrize("same_capture,expected", [
    (True, "Updating calculation outline…"), (False, "No calculation outline")])
def test_old_review_label_does_not_survive_source_or_capture_transition(same_capture, expected):
    panel = SimpleNamespace(stage_status=mock.Mock(), _current_capture_binding=lambda: "active")
    document = {"available": True, "captureBinding": "active" if same_capture else "old",
                "stageOutlineState": {"state": "reviewed", "complete": True}}
    status_method()(panel, document, False)
    panel.stage_status.setText.assert_called_once_with(expected)


def test_source_projection_exposes_provisional_outline_even_when_only_three_functions():
    import threading
    module = _load_without_qt()
    result = module._build_source_flow_projection({
        "stop": threading.Event(), "sourceText": "float A(){return 1;}\nfloat B(){return 2;}\n"
        "float C(){return 3;}\nfloat Main(){return A()+B()+C();}",
        "sourceIdentity": {}, "entryPoint": "Main"})
    assert result["state"] == "available"
    assert result["stageOutlineState"]["state"] == "provisional"
    assert result["stageOutlineState"]["complete"] is False
    assert result["stageOutlineState"]["source"] == "source-derived"
