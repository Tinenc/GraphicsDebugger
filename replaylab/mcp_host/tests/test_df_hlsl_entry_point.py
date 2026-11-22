import pytest
from src.shader_workspace.adapters.hlsl_decompiler_adapter import bind_entry_point


def test_named_captured_entry_compiles_under_its_original_name():
    text = "// 3DMigoto\nvoid main(\n  out float4 color : SV_TARGET) { color = 1; }\n"
    assert "void PS(" in bind_entry_point(text, "PS")
    assert bind_entry_point(text, "main") == text


@pytest.mark.parametrize("text,entry", [
    ("void main() {}", "x; injected"),
    ("float PS;\nvoid main() {}", "PS"),
    ("void another() {}", "PS"),
    ("void main() {}\nvoid main() {}", "PS"),
])
def test_ambiguous_or_invalid_entry_is_rejected(text, entry):
    with pytest.raises(ValueError):
        bind_entry_point(text, entry)
