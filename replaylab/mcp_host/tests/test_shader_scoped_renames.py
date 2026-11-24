"""Scoped names may be reused only when they cannot capture another binding."""

from __future__ import annotations

import pytest

from src.shader_workspace import equivalence as eq


def test_two_functions_may_use_the_same_local_target() -> None:
    source = """float First(float input) { float _9 = input; return _9; }
float Second(float input) { float _9 = input * 2.0; return _9; }
"""
    renames = eq.validate_renames(source, {
        "First::_9": "lightFactor",
        "Second::_9": "lightFactor",
    })
    assert renames == {
        "First::_9": "lightFactor",
        "Second::_9": "lightFactor",
    }
    assert eq.rename_targets_can_share(source, "First::_9", "Second::_9")
    proposal = eq.apply_renames(source, renames)
    assert proposal.count("float lightFactor") == 2
    assert eq.rename_only_equivalent(source, proposal, renames)["equal"]


def test_disjoint_blocks_in_one_function_may_reuse_a_local_target() -> None:
    source = """float Main(float input) {
  if (input > 0.0) { float _a = input; input = _a; }
  if (input < 0.0) { float _b = -input; input = _b; }
  return input;
}
"""
    renames = eq.validate_renames(source, {
        "Main::_a": "magnitude", "Main::_b": "magnitude",
    })
    assert renames["Main::_a"] == renames["Main::_b"] == "magnitude"
    assert eq.rename_targets_can_share(source, "Main::_a", "Main::_b")


def test_local_may_reuse_an_unused_outer_name() -> None:
    source = """float globalFactor;
float Helper(float input) { float _1 = input; return _1; }
"""
    renames = eq.validate_renames(source, {"Helper::_1": "globalFactor"})
    assert renames["Helper::_1"] == "globalFactor"
    assert eq.rename_targets_can_share(source, "Helper::_1", "globalFactor")
    proposal = eq.apply_renames(source, renames)
    assert "float globalFactor;" in proposal
    assert "float globalFactor = input; return globalFactor;" in proposal


def test_local_cannot_capture_an_outer_reference() -> None:
    source = """float globalFactor;
float Helper(float input) { float _1 = input; return _1 + globalFactor; }
"""
    assert not eq.rename_targets_can_share(source, "Helper::_1", "globalFactor")
    with pytest.raises(eq.EquivalenceError, match="overlapping scope"):
        eq.validate_renames(source, {"Helper::_1": "globalFactor"})


def test_global_rename_may_share_name_with_unrelated_function_local() -> None:
    source = """float _global;
float Helper(float input) { float intensity = input; return intensity; }
float Main() { return _global; }
"""
    renames = eq.validate_renames(source, {"_global": "intensity"})
    assert renames["_global"] == "intensity"
    proposal = eq.apply_renames(source, renames)
    assert "float intensity;" in proposal
    assert "float Main() { return intensity; }" in proposal


def test_global_rename_cannot_be_captured_by_existing_local() -> None:
    source = """float _global;
float Helper(float input) { float intensity = input; return intensity + _global; }
"""
    with pytest.raises(eq.EquivalenceError, match="overlapping scope"):
        eq.validate_renames(source, {"_global": "intensity"})


def test_ambiguous_local_reports_each_declaration_and_block() -> None:
    source = """float Main(float input) {
  float _7 = input;
  if (_7 > 0.0) {
    float _7 = 2.0;
    input = _7;
  }
  return _7;
}
"""
    with pytest.raises(eq.EquivalenceError) as failure:
        eq.validate_renames(source, {"Main::_7": "value"})
    message = str(failure.value)
    assert "not a unique declaration" in message
    assert "line 2:" in message and "block 1-8" in message
    assert "line 4:" in message and "block 3-6" in message


def test_scoped_name_index_reuses_only_the_exact_source_revision() -> None:
    source = "float Main() { float _1 = 1.0; return _1; }\n"
    first = eq._binding_index(source)
    assert eq._binding_index("".join([source[:12], source[12:]])) is first
    changed = source.replace("1.0", "2.0")
    assert eq._binding_index(changed) is not first
    assert eq._binding_index(source) is first


def test_scoped_name_index_lru_discards_old_revisions() -> None:
    sources = [f"float Main() {{ return {number}.0; }}\n" for number in range(3)]
    first = eq._binding_index(sources[0])
    eq._binding_index(sources[1])
    eq._binding_index(sources[2])
    assert len(eq._RENAME_INDEX_CACHE) <= eq._RENAME_INDEX_CACHE_MAX_ENTRIES
    assert eq._binding_index(sources[0]) is not first
