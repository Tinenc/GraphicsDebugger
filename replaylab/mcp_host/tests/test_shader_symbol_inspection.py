"""Read-only, source-bound symbol locations for the Shader outline."""

from __future__ import annotations

import json

import pytest

from src.shader_workspace.equivalence import EquivalenceError, inspect_symbol


SOURCE = """cbuffer MaterialCB { float exposure; };
float Main(float input) {
  float _9 = input * exposure;
  _9 = _9 + input;
  return _9;
}
"""


def test_local_inspection_reports_lexical_block_and_bound_references() -> None:
    result = inspect_symbol(SOURCE, "Main::_9")
    assert result["schema"] == "replaylab-shader-symbol-inspection/v1"
    assert result["identityScope"] == "exact-source-only"
    assert result["kind"] == "local"
    assert result["declaration"]["line"] == 3
    assert result["lexicalScope"] == {
        "kind": "block", "function": "Main", "startLine": 2,
        "endLine": 6, "visibleFromLine": 3,
    }
    assert result["functionRange"]["bodyEndLine"] == 6
    assert result["references"]["boundCount"] == 3
    assert [item["line"] for item in result["references"]["locations"]] == [4, 4, 5]
    assert result["dependencies"]["status"] == "unknown"
    assert SOURCE not in json.dumps(result)


def test_parameter_and_cbuffer_member_have_distinct_scopes() -> None:
    parameter = inspect_symbol(SOURCE, "Main::input")
    assert parameter["kind"] == "parameter"
    assert parameter["lexicalScope"]["kind"] == "function"
    assert parameter["references"]["boundCount"] == 2

    member = inspect_symbol(SOURCE, "MaterialCB.exposure")
    assert member["kind"] == "cbuffer-field"
    assert member["owner"] == "MaterialCB"
    assert member["lexicalScope"]["kind"] == "owner-block"
    assert member["references"]["boundCount"] == 1


def test_reference_locations_are_bounded_without_losing_total_count() -> None:
    result = inspect_symbol(SOURCE, "Main::_9", limit=2)
    assert result["references"]["boundCount"] == 3
    assert len(result["references"]["locations"]) == 2
    assert result["references"]["truncated"] is True
    for invalid in (0, 65, True, "4"):
        with pytest.raises(EquivalenceError, match="limit"):
            inspect_symbol(SOURCE, "Main::_9", limit=invalid)


def test_ambiguous_local_reports_candidates_instead_of_guessing() -> None:
    source = """float Main() {
  float _7 = 1.0;
  if (_7 > 0.0) { float _7 = 2.0; }
  return _7;
}
"""
    with pytest.raises(EquivalenceError) as failure:
        inspect_symbol(source, "Main::_7")
    assert "line 2:" in str(failure.value)
    assert "line 3:" in str(failure.value)


def test_unbound_identifier_is_not_presented_as_a_declaration() -> None:
    with pytest.raises(EquivalenceError, match="no uniquely bound declaration"):
        inspect_symbol("float Main() { return unknownValue; }", "unknownValue")
