"""Pure-Python coverage for pipeline-stage call-site navigation resolution.

The flow-graph single click must land on where a stage is *invoked* in the
entry body (its use), not inside its own definition. These checks exercise the
Qt-free hierarchy helpers directly.
"""
import os
import sys

BRIDGE = os.path.join(os.path.dirname(__file__), os.pardir, "renderdoc_mcp_bridge")
sys.path.insert(0, os.path.abspath(BRIDGE))

from project_shader_flow_hierarchy import SourceFlowHierarchy  # noqa: E402

SOURCE = (
    "float Square(float x) { return x * x; }\n"          # 1: def
    "float Refine(float x) { return Square(x) + 1.0; }\n"  # 2: def (calls Square)
    "void StageA(inout float v) { v = Refine(v); }\n"     # 3: def (calls Refine)
    "void StageB(inout float v) { v = v * 2.0; }\n"       # 4: def
    "void MainInner(inout float v)\n"                      # 5: entry def
    "{\n"                                                   # 6
    "    StageA(v);\n"                                     # 7: call StageA
    "    StageB(v);\n"                                     # 8: call StageB
    "}\n"                                                   # 9
)


def test_call_site_points_at_use_in_entry_body():
    h = SourceFlowHierarchy(SOURCE, {"documentId": "t"})
    a = h.call_site("StageA")
    assert a is not None and a["line"] == 7, a
    assert h.functions[a["ownerFunctionId"]]["name"] == "MainInner"
    b = h.call_site("StageB")
    assert b is not None and b["line"] == 8, b
    assert h.functions[b["ownerFunctionId"]]["name"] == "MainInner"


def test_call_site_skips_forward_declaration_and_definition():
    # A forward declaration has no body and is not recorded as a function, so
    # Foo is unambiguous; call_site must skip the declaration (line 1) and the
    # definition (line 3) and return the actual call in Bar (line 2).
    src = "void Foo(float x);\nvoid Bar(){ Foo(1.0); }\nvoid Foo(float x){ }\n"
    h = SourceFlowHierarchy(src, {"documentId": "t"})
    call = h.call_site("Foo")
    assert call is not None and call["line"] == 2, call
    assert h.functions[call["ownerFunctionId"]]["name"] == "Bar"


def test_call_site_none_when_never_called():
    h = SourceFlowHierarchy(SOURCE, {"documentId": "t"})
    # MainInner is the entry; it is defined but not called anywhere.
    assert h.call_site("MainInner") is None


def test_line_calls_detects_and_rejects():
    h = SourceFlowHierarchy(SOURCE, {"documentId": "t"})
    assert h.line_calls(7, "StageA") is True
    assert h.line_calls(8, "StageA") is False
    assert h.line_calls(999, "StageA") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
