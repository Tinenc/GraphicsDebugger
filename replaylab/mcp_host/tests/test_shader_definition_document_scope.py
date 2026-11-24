"""Native definition locations must select the exact Shader document."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace


def panel():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/project_shader_workspace.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_definition_requested")
    namespace = {"os": os, "_format_message": lambda message, value: message.replace("%1", value),
                 "QtCore": SimpleNamespace(QCoreApplication=SimpleNamespace(translate=lambda _, text: text))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(path), "exec"), namespace)
    value = SimpleNamespace(_documents={}, _workspaces={}, _pending_open={}, selected=[], jumped=[], errors=[])
    for index, name in enumerate(("first", "second")):
        source = os.path.abspath(os.path.join("workspaces", name, "source", "shader.hlsl"))
        workspace = {"sourcePath": source, "sourceRelativePath": "source/shader.hlsl", "fileName": "shader.hlsl"}
        value._documents[name] = {"available": True, "sourcePath": source, "fileName": "shader.hlsl",
                                  "workspace": workspace,
                                  "editor": SimpleNamespace(GoToLine=lambda line, column, name=name: value.jumped.append((name, line, column)))}
        value._workspaces[name] = workspace
    value._tab_index = lambda key: ("first", "second").index(key)
    value.tab_bar = SimpleNamespace(setCurrentIndex=value.selected.append)
    value.status_label = SimpleNamespace(setText=value.errors.append)
    value._open_workspace = lambda key: False
    value._open_related_definition = lambda *args: False
    value.navigate = lambda name, owner=None: namespace["_definition_requested"](value, name, 9, 3, owner)
    return value


def test_absolute_definition_does_not_match_other_shader_relative_suffix():
    value = panel()
    value.navigate(value._documents["second"]["sourcePath"], "second")
    assert value.jumped == [("second", 9, 3)]
    assert value.selected == [1]


def test_relative_definition_is_resolved_only_in_native_target_owner():
    value = panel()
    value.navigate("source/shader.hlsl", "second")
    assert value.jumped == [("second", 9, 3)]


def test_unknown_path_or_ownerless_basename_never_opens_first_matching_shader():
    value = panel()
    value.navigate("shader.hlsl")
    value.navigate(os.path.abspath("unrelated/source/shader.hlsl"), "second")
    assert not value.jumped
    assert not value.selected
    assert value.errors
