"""Chat citations resolve to exact authorised IDE documents, without replay or file opening."""
from __future__ import annotations

import ast
import ntpath
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/project_shader_workspace.py"


def navigator():
    source = ast.parse(SOURCE.read_text(encoding="utf-8"), feature_version=(3, 6))
    method = next(node for node in ast.walk(source)
                  if isinstance(node, ast.FunctionDef) and node.name == "navigate_source_link")
    normal = lambda path: ntpath.normcase(ntpath.normpath(path))
    scope = {"os": SimpleNamespace(path=ntpath), "_normal_path": normal}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), scope)
    calls = []
    binding = {"captureSessionId": "current"}
    panel = SimpleNamespace(
        _alive=True, _shutting_down=False, _capture_binding=binding,
        _current_capture_binding=lambda: binding, _current_workspace_id=lambda: "b",
        _workspaces={
            "a": {"sourcePath": "D:/project/a/source/shader.hlsl",
                  "workspacePath": "D:/project/a", "sourceRelativePath": "source/shader.hlsl"},
            "b": {"sourcePath": "D:/project/b/source/shader.hlsl",
                  "workspacePath": "D:/project/b", "sourceRelativePath": "source/shader.hlsl"},
        }, _pending_open={},
        _open_workspace=lambda identity, **options: calls.append((identity, options)) or True,
        _definition_requested=lambda *args: calls.append(("include", args)),
        _select_source_link=lambda *args: calls.append(("selection", args)),
        show=lambda: calls.append(("show",)),
    )
    return lambda request: scope["navigate_source_link"](panel, request), panel, calls


def test_exact_workspace_goes_to_requested_location_not_same_named_other_shader():
    navigate, panel, calls = navigator()
    assert navigate({"workspaceId": "a", "file": "source/shader.hlsl", "line": 125, "column": 4})
    assert calls == [("a", {"quiet": True, "select": True, "navigation": (125, 4)}),
                     ("show",), ("selection", ("a", {"workspaceId": "a", "file": "source/shader.hlsl", "line": 125, "column": 4}, 125, 4))]
    assert len(panel._workspaces) == 2


def test_absolute_citation_resolves_only_authorised_exact_source_path():
    navigate, _, calls = navigator()
    assert navigate({"path": r"d:\PROJECT\a\source\shader.hlsl", "line": 32})
    assert calls[0][0] == "a" and calls[0][1]["navigation"] == (32, 1)
    calls.clear()
    assert not navigate({"path": "D:/unrelated/source/shader.hlsl", "line": 32})
    assert not navigate({"path": "shader.hlsl", "line": 32})
    assert not navigate({"workspaceId": "b", "path": "D:/project/a/source/shader.hlsl"})
    assert calls == []


def test_include_uses_existing_owner_scoped_definition_authorization():
    navigate, _, calls = navigator()
    assert navigate({"workspaceId": "a", "file": "include/common.hlsli", "line": 8, "column": 9})
    assert calls[1] == ("include", ("D:/project/a\\include\\common.hlsli", 8, 9, "a"))


@pytest.mark.parametrize("citation", [
    {"workspaceId": "a", "file": "../b/source/shader.hlsl"},
    {"workspaceId": "a", "file": "D:/b/shader.hlsl"},
    {"workspaceId": "a", "file": "source/shader.hlsl:command"},
    {"workspaceId": "a", "line": 0},
    {"workspaceId": "a", "column": 1000001},
    {"workspaceId": "a", "line": "bad"},
    {"workspaceId": "unknown"},
    {"workspaceId": "a", "path": "D:/project/a/source/shader.hlsl\0"},
])
def test_invalid_navigation_is_not_forwarded(citation):
    navigate, _, calls = navigator()
    assert not navigate(citation)
    assert calls == []


def test_capture_transition_rejects_old_navigation_without_rebinding():
    navigate, panel, calls = navigator()
    panel._current_capture_binding = lambda: {"captureSessionId": "next"}
    assert not navigate({"workspaceId": "a", "line": 1})
    assert calls == []


def test_uncached_source_queues_exact_location_until_background_snapshot_is_ready():
    navigate, panel, calls = navigator()

    def pending(identity, **options):
        panel._pending_open[identity] = options
        return False

    panel._open_workspace = pending
    assert navigate({"workspaceId": "a", "line": 62, "column": 2})
    assert panel._pending_open["a"]["navigation"] == (62, 2)
    assert calls == [("show",), ("selection", ("a", {"workspaceId": "a", "line": 62, "column": 2}, 62, 2))]


def test_uncached_include_queues_owner_and_exact_definition_until_source_is_ready():
    navigate, panel, calls = navigator()

    def pending(identity, **options):
        panel._pending_open[identity] = options
        return False

    panel._open_workspace = pending
    assert navigate({"workspaceId": "a", "file": "include/common.hlsli", "line": 62, "column": 2})
    assert panel._pending_open["a"]["definition"] == (
        "D:/project/a\\include\\common.hlsli", 62, 2)
    assert calls == [("show",)]
