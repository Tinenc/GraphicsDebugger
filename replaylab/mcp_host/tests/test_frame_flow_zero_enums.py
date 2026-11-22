import ast
from pathlib import Path


def test_zero_valued_blend_enum_keeps_its_name():
    bridge = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"
    tree = ast.parse(bridge.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_frame_flow_text")
    scope = {"MAX_FRAME_FLOW_TEXT": 2048}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(bridge), "exec"), scope)

    class ZeroEnum(int):
        def __new__(cls, name):
            value = int.__new__(cls, 0)
            value.name = name
            return value

        def __str__(self):
            return self.name

    convert = scope["_frame_flow_text"]
    assert convert(ZeroEnum("Add")) == "Add"
    assert convert(ZeroEnum("Zero")) == "Zero"
    assert convert(0) == "0"
    assert convert(None) == ""
    assert convert(" A\x00dd ", 2) == "Ad"
