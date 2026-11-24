"""Source-bound symbol pages share Host rename identities without claiming SSA."""

from __future__ import annotations

import hashlib
import json

import pytest

from src.shader_workspace.equivalence import EquivalenceError
from src.shader_workspace.symbol_graph import inspect_source_symbol_table


SOURCE = """cbuffer MaterialCB { float exposure; };
Texture2D tAlbedo : register(t0);
float Main(float inputValue) {
  float value = inputValue * exposure;
  if (value > 0.0) {
    float exposure = 2.0;
    value += exposure;
  }
  return value;
}
"""


def _row(result, name, kind=None):
    rows = [row for row in result["rows"] if row["name"] == name
            and (kind is None or row["kind"] == kind)]
    assert len(rows) == 1
    return rows[0]


def test_global_declarations_are_paged_and_bound_to_exact_source() -> None:
    first = inspect_source_symbol_table(SOURCE, limit=2)
    second = inspect_source_symbol_table(SOURCE, offset=2, limit=2)
    assert first["schema"] == "replaylab-shader-source-symbols/v1"
    assert first["sourceSha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert first["identityScope"] == "exact-source-only"
    assert first["runtimeLivenessKnown"] is False
    assert first["dataFlowProven"] is False
    assert first["view"] == "global-declarations"
    assert first["nextOffset"] == 2
    assert (second["rows"][0]["declaration"]["characterOffset"]
            > first["rows"][-1]["declaration"]["characterOffset"])
    assert first["total"] == second["total"]
    all_rows = inspect_source_symbol_table(SOURCE)["rows"]
    assert _row({"rows": all_rows}, "MaterialCB")["renameSelector"] == "cbuffer:MaterialCB"
    assert _row({"rows": all_rows}, "exposure")["renameSelector"] == "MaterialCB.exposure"
    assert _row({"rows": all_rows}, "exposure")["usedByFunctions"] == ["Main"]
    assert SOURCE not in json.dumps(first)


def test_function_view_separates_shadowed_names_and_lists_only_lexical_dependencies() -> None:
    result = inspect_source_symbol_table(SOURCE, function="Main")
    assert result["view"] == "function-symbols"
    assert result["function"]["bodyEndLine"] == 10
    assert result["limitations"]
    assert result["total"] == 4
    exposure_rows = [row for row in result["rows"] if row["name"] == "exposure"]
    assert len(exposure_rows) == 2
    assert {row["kind"] for row in exposure_rows} == {"cbuffer-field", "local"}
    assert exposure_rows[0]["identity"] != exposure_rows[1]["identity"]
    assert _row(result, "exposure", "local")["lexicalScope"]["kind"] == "block"
    assert _row(result, "exposure", "local")["renameSelector"] == "Main::exposure"
    value = _row(result, "value")
    assert value["functionReferences"]["simpleWriteSites"] == 1
    assert value["functionReferences"]["initializerWriteSites"] == 1
    assert value["functionReferences"]["simpleReadSites"] >= 3
    assert value["candidateDependencies"]["directAssignmentCount"] == 2
    assert {item["name"] for item in value["candidateDependencies"]["inputSymbols"]} == {
        "inputValue", "exposure", "value"
    }
    assert value["candidateDependencies"]["interpretation"].endswith("not data-flow proof")
    assert result["dataFlowProven"] is False


def test_same_name_in_two_blocks_has_no_safe_rename_selector() -> None:
    source = """float Main() {
  float x = 1;
  if (x > 0) { float x = 2; return x; }
  return x;
}
"""
    result = inspect_source_symbol_table(source, function="Main")
    rows = [row for row in result["rows"] if row["name"] == "x"]
    assert len(rows) == 2
    assert all(row["renameSelector"] is None for row in rows)
    assert rows[0]["lexicalScope"]["endLine"] != rows[1]["lexicalScope"]["endLine"]


def test_unknown_call_effects_are_not_simple_reads() -> None:
    source = """void SetValue(out float target) { target = 1; }
float Main(float inputValue) {
  float outputValue = inputValue;
  SetValue(outputValue);
  return outputValue;
}
"""
    result = inspect_source_symbol_table(source, function="Main")
    value = _row(result, "outputValue")
    assert value["functionReferences"]["uncertainAccessSites"] == 1
    assert value["functionReferences"]["simpleReadSites"] == 1
    assert value["functionReferences"]["simpleWriteSites"] == 0
    assert value["functionReferences"]["initializerWriteSites"] == 1
    assert value["candidateDependencies"]["inputSymbols"][0]["name"] == "inputValue"


def test_loop_declaration_does_not_claim_outer_block_lifetime() -> None:
    source = """float Main() {
  float total = 0;
  for (int i = 0; i < 2; ++i) { total += i; }
  return total;
}
"""
    result = inspect_source_symbol_table(source, function="Main")
    loop_variable = _row(result, "i")
    assert loop_variable["renameSelector"] is None
    assert loop_variable["lexicalScope"]["kind"] == "unresolved"
    assert result["runtimeLivenessKnown"] is False


def test_glsl_interface_rows_remain_nonrenameable_while_local_scope_is_visible() -> None:
    source = ("#version 450\n"
              "layout(location=0) in vec2 uv;\n"
              "layout(location=0) out vec4 color;\n"
              "void main() { vec2 sampledUv = uv; color = vec4(sampledUv, 0.0, 1.0); }\n")
    globals_view = inspect_source_symbol_table(source)
    assert _row(globals_view, "uv")["renameSelector"] is None
    assert _row(globals_view, "color")["renameSelector"] is None
    local_view = inspect_source_symbol_table(source, function="main")
    assert _row(local_view, "sampledUv")["renameSelector"] == "main::sampledUv"
    assert local_view["runtimeLivenessKnown"] is False


def test_same_source_uses_cached_graph_and_changed_source_has_different_revision(monkeypatch) -> None:
    import src.shader_workspace.symbol_graph as graph_module

    source = SOURCE + "// unique cache test\n"
    calls = []
    original = graph_module._binding_index

    def count_builds(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(graph_module, "_binding_index", count_builds)
    first = inspect_source_symbol_table(source, function="Main", limit=1)
    second = inspect_source_symbol_table(source, function="Main", offset=1, limit=1)
    changed = inspect_source_symbol_table(source.replace("2.0", "3.0"), function="Main", limit=1)
    assert len(calls) == 2
    assert first["sourceSha256"] == second["sourceSha256"]
    assert first["sourceSha256"] != changed["sourceSha256"]
    assert first["rows"][0]["identity"] != second["rows"][0]["identity"]
    original_name = first["rows"][0]["name"]
    first["rows"][0]["name"] = "callerMutation"
    assert inspect_source_symbol_table(source, function="Main", limit=1)["rows"][0]["name"] == original_name


@pytest.mark.parametrize("arguments", [
    {"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 257},
    {"limit": "4"}, {"function": "Main::x"}, {"function": "Missing"},
])
def test_invalid_or_nonunique_queries_fail_explicitly(arguments) -> None:
    with pytest.raises(EquivalenceError):
        inspect_source_symbol_table(SOURCE, **arguments)
