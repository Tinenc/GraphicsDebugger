from __future__ import annotations

from unittest.mock import patch

from src.shader_workspace import restructure
from src.shader_workspace.equivalence import apply_renames


SOURCE = """float4 MainPS(float4 input : TEXCOORD0) : SV_Target0
{
    float4 sampled = input * 0.5f;
    float4 color = sampled * 2.0f;
    return color;
}
"""


def test_canonical_stage_cache_reuses_exact_ledger_prefix_and_invalidates_changes():
    restructure._clear_canonical_stage_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3, "name": "ReadSample"}
    original_apply = restructure.apply_operations
    with patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        stages = restructure._canonical_operation_stages(SOURCE, [first])
        assert apply.call_count == 1
        assert "ReadSample" in stages[-1]

        assert restructure._canonical_operation_stages(SOURCE, [first]) == stages
        assert apply.call_count == 1

        color_line = next(i for i, line in enumerate(stages[-1].splitlines(), 1)
                          if "float4 color =" in line)
        second = {"operation": "extract", "startLine": color_line,
                  "endLine": color_line, "name": "ScaleColor"}
        extended = restructure._canonical_operation_stages(SOURCE, [first, second])
        assert apply.call_count == 2  # only the new operation was replayed
        assert "ScaleColor" in extended[-1]

        changed = {**first, "name": "ReadSampleAgain"}
        changed_stages = restructure._canonical_operation_stages(SOURCE, [changed])
        assert apply.call_count == 3
        assert "ReadSampleAgain" in changed_stages[-1]
        assert "ReadSample(" not in changed_stages[-1]

        source_changed = SOURCE.replace("0.5f", "0.75f")
        source_stages = restructure._canonical_operation_stages(source_changed, [first])
        assert apply.call_count == 4
        assert "0.75f" in source_stages[-1]
    restructure._clear_canonical_stage_cache()


def test_canonical_stage_replay_matches_uncached_result_for_extract_and_rewrite():
    restructure._clear_canonical_stage_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3, "name": "ReadSample"}
    stage1 = restructure._canonical_operation_stages(SOURCE, [first])[-1]
    return_line = next(i for i, line in enumerate(stage1.splitlines(), 1)
                       if line.strip() == "return color;")
    rewrite = restructure.prepare_rewrite(
        stage1,
        [{"startLine": return_line, "endLine": return_line,
          "replacement": "    return color * 1.0f;"}],
        renames={}, entry_point="MainPS", name="DocumentOutput",
    )
    stages = restructure._canonical_operation_stages(SOURCE, [first, rewrite])
    direct, unused = restructure.apply_operations(SOURCE, [first, rewrite], renames={})
    assert stages[-1] == direct
    assert "return color * 1.0f;" in direct
    restructure._clear_canonical_stage_cache()


def test_staged_result_cache_reuses_exact_rename_and_returns_independent_receipts():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    extraction = {"operation": "extract", "startLine": 3, "endLine": 3,
                  "name": "ReadSample"}
    original_plan = restructure._staged_rename_plan
    with patch.object(restructure, "_staged_rename_plan", wraps=original_plan) as plan:
        source, receipts = restructure.apply_staged_operations(
            SOURCE, [extraction], {"MainPS::sampled": "weightedSample"}
        )
        assert plan.call_count == 1
        assert "weightedSample" in source
        receipts[0]["name"] = "tampered"

        same_source, same_receipts = restructure.apply_staged_operations(
            SOURCE, [extraction], {"MainPS::sampled": "weightedSample"}
        )
        assert plan.call_count == 1
        assert same_source == source
        assert same_receipts[0]["name"] == "ReadSample"

        changed_source, unused = restructure.apply_staged_operations(
            SOURCE, [extraction], {"MainPS::sampled": "materialSample"}
        )
        assert plan.call_count == 2
        assert "materialSample" in changed_source
        assert changed_source != source
    restructure._clear_staged_apply_cache()
    restructure._clear_canonical_stage_cache()


def test_staged_result_cache_reuses_exact_prefix_when_one_extract_is_appended():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3,
             "name": "ReadSample"}
    renames = {"MainPS::sampled": "weightedSample"}
    earlier, first_receipts = restructure.apply_staged_operations(
        SOURCE, [first], renames,
    )
    color_line = next(i for i, line in enumerate(earlier.splitlines(), 1)
                      if "float4 color =" in line)
    second = {"operation": "extract", "startLine": color_line,
              "endLine": color_line, "name": "ScaleColor"}
    original_apply = restructure.apply_operations
    with patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        extended, receipts = restructure.apply_staged_operations(
            SOURCE, [first, second], renames,
        )
    # One canonical and one renamed application of the appended operation;
    # the first renamed operation is not re-applied.
    assert apply.call_count == 2
    assert [receipt["name"] for receipt in receipts] == ["ReadSample", "ScaleColor"]
    assert earlier != extended and first_receipts[0]["name"] == "ReadSample"
    restructure._clear_staged_apply_cache()
    cold, cold_receipts = restructure.apply_staged_operations(
        SOURCE, [first, second], renames,
    )
    assert (extended, receipts) == (cold, cold_receipts)
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()


def test_late_bound_rename_reuses_only_unaffected_staged_prefix():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3,
             "name": "ReadSample"}
    first_stage = restructure._canonical_operation_stages(SOURCE, [first])[-1]
    color_line = next(i for i, line in enumerate(first_stage.splitlines(), 1)
                      if "float4 color =" in line)
    second = {"operation": "extract", "startLine": color_line,
              "endLine": color_line, "name": "ScaleColor"}
    operations = [first, second]
    original, original_receipts = restructure.apply_staged_operations(
        SOURCE, operations, {},
    )
    renames = {"ScaleColor::color": "weightedColor"}
    unused, buckets, unused_stages = restructure._staged_rename_plan(
        SOURCE, operations, renames,
    )
    assert [len(bucket) for bucket in buckets] == [0, 0, 1]

    original_apply = restructure.apply_operations
    with patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        changed, changed_receipts = restructure.apply_staged_operations(
            SOURCE, operations, renames,
        )
    assert apply.call_count == 1  # only the renamed second extraction is replayed
    assert "weightedColor" in changed and changed != original
    assert [item["name"] for item in changed_receipts] == [
        item["name"] for item in original_receipts
    ]

    next_renames = {"ScaleColor::color": "materialColor"}
    with patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        changed_again, changed_again_receipts = restructure.apply_staged_operations(
            SOURCE, operations, next_renames,
        )
    assert apply.call_count == 1  # changing only the late target retains stage 1
    assert "materialColor" in changed_again

    restructure._clear_staged_apply_cache()
    cold, cold_receipts = restructure.apply_staged_operations(
        SOURCE, operations, next_renames,
    )
    assert (changed_again, changed_again_receipts) == (cold, cold_receipts)
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()


def test_baseline_rename_does_not_reuse_old_staged_prefix():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3,
             "name": "ReadSample"}
    first_stage = restructure._canonical_operation_stages(SOURCE, [first])[-1]
    color_line = next(i for i, line in enumerate(first_stage.splitlines(), 1)
                      if "float4 color =" in line)
    second = {"operation": "extract", "startLine": color_line,
              "endLine": color_line, "name": "ScaleColor"}
    operations = [first, second]
    restructure.apply_staged_operations(
        SOURCE, operations, {"MainPS::sampled": "weightedSample"},
    )

    original_apply = restructure.apply_operations
    with patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        changed, unused = restructure.apply_staged_operations(
            SOURCE, operations, {"MainPS::sampled": "materialSample"},
        )
    assert apply.call_count == 2  # the baseline name changes both extractions
    assert "materialSample" in changed
    restructure._clear_staged_apply_cache()
    cold, unused = restructure.apply_staged_operations(
        SOURCE, operations, {"MainPS::sampled": "materialSample"},
    )
    assert changed == cold
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()


def test_changed_extraction_comment_invalidates_affected_operation_prefix():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3,
             "name": "ReadSample"}
    first_stage = restructure._canonical_operation_stages(SOURCE, [first])[-1]
    color_line = next(i for i, line in enumerate(first_stage.splitlines(), 1)
                      if "float4 color =" in line)
    second = {"operation": "extract", "startLine": color_line,
              "endLine": color_line, "name": "ScaleColor"}
    restructure.apply_staged_operations(SOURCE, [first, second], {})

    changed_first = {**first, "comment": "Read the sample before scaling."}
    original_apply = restructure.apply_operations
    with patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        changed, receipts = restructure.apply_staged_operations(
            SOURCE, [changed_first, second], {},
        )
    assert apply.call_count == 4  # both changed canonical and rendered prefixes
    assert "Read the sample before scaling." in changed
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    cold, cold_receipts = restructure.apply_staged_operations(
        SOURCE, [changed_first, second], {},
    )
    assert (changed, receipts) == (cold, cold_receipts)
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()


def test_staged_prefix_snapshots_obey_entry_and_byte_limits():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    first = {"operation": "extract", "startLine": 3, "endLine": 3,
             "name": "ReadSample"}
    first_stage = restructure._canonical_operation_stages(SOURCE, [first])[-1]
    color_line = next(i for i, line in enumerate(first_stage.splitlines(), 1)
                      if "float4 color =" in line)
    second = {"operation": "extract", "startLine": color_line,
              "endLine": color_line, "name": "ScaleColor"}
    with patch.object(restructure, "_STAGED_APPLY_CACHE_MAX_ENTRIES", 2), \
         patch.object(restructure, "_STAGED_APPLY_CACHE_MAX_BYTES", 1600):
        restructure.apply_staged_operations(SOURCE, [first, second], {})
        assert len(restructure._STAGED_APPLY_CACHE) <= 2
        assert sum(row[2] for row in restructure._STAGED_APPLY_CACHE.values()) <= 1600
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()


def test_staged_rename_skips_index_until_extracted_owner_exists():
    restructure._clear_canonical_stage_cache()
    operation = {"operation": "extract", "startLine": 3, "endLine": 3,
                 "name": "ReadSample"}
    original_split = restructure.split_bound_renames
    with patch.object(restructure, "split_bound_renames", wraps=original_split) as split:
        clean = restructure.validate_staged_renames(
            SOURCE, [operation], {"ReadSample::sampled": "weightedSample"},
        )
    assert clean == {"ReadSample::sampled": "weightedSample"}
    assert split.call_count == 1  # no declaration/index in the baseline stage
    restructure._clear_canonical_stage_cache()


def test_staged_renames_allow_same_local_name_across_disjoint_functions():
    source = SOURCE + "\nfloat Extra(float value)\n{\n    float other = value;\n    return other;\n}\n"
    extraction = {"operation": "extract", "startLine": 3, "endLine": 3,
                  "name": "ReadSample"}
    renames = {"Extra::other": "weightedTemp", "ReadSample::sampled": "weightedTemp"}
    assert restructure.validate_staged_renames(source, [extraction], renames) == renames
    rendered, unused = restructure.apply_staged_operations(source, [extraction], renames)
    assert "float4 weightedTemp = input * 0.5f;" in rendered
    assert "float weightedTemp = value;" in rendered


def test_staged_rename_target_may_match_existing_unrelated_function_local():
    source = SOURCE + "\nfloat Extra(float value)\n{\n    float weightedTemp = value;\n    return weightedTemp;\n}\n"
    renames = {"MainPS::sampled": "weightedTemp"}
    assert restructure.validate_staged_renames(source, [], renames) == renames


def test_compressed_stage_cache_keeps_large_exact_prefix_under_memory_limit():
    restructure._clear_canonical_stage_cache()
    # The body is tiny; a long, repeated comment stands in for a large source
    # shared by every structural stage. Its raw stages exceed the test budget.
    source = SOURCE + ("// repeated source text " * 1200) + "\n"
    operation = {"operation": "extract", "startLine": 3, "endLine": 3,
                 "name": "ReadSample"}
    original_apply = restructure.apply_operations
    with patch.object(restructure, "_CANONICAL_STAGE_CACHE_MAX_BYTES", 4096), \
         patch.object(restructure, "apply_operations", wraps=original_apply) as apply:
        first = restructure._canonical_operation_stages(source, [operation])
        assert sum(len(stage.encode("utf-8")) for stage in first) > 4096
        assert len(restructure._CANONICAL_STAGE_CACHE) == 1
        assert restructure._canonical_operation_stages(source, [operation]) == first
        assert apply.call_count == 1
    restructure._clear_canonical_stage_cache()


def test_staged_apply_reuses_prevalidated_canonical_extraction():
    restructure._clear_canonical_stage_cache()
    restructure._clear_staged_apply_cache()
    operation = {"operation": "extract", "startLine": 3, "endLine": 3,
                 "name": "ReadSample"}
    restructure._canonical_operation_stages(SOURCE, [operation])
    original_extract = restructure.extract_function
    with patch.object(restructure, "extract_function", wraps=original_extract) as extract:
        rendered, receipts = restructure.apply_staged_operations(
            SOURCE, [operation], {"MainPS::sampled": "weightedSample"}
        )
    assert len(receipts) == 1
    assert "weightedSample" in rendered
    assert extract.call_count == 1  # current source only; canonical stage was validated already
    directly, direct_receipts = restructure.apply_operations(
        apply_renames(SOURCE, {"MainPS::sampled": "weightedSample"}),
        [operation], renames={"MainPS::sampled": "weightedSample"},
    )
    assert (rendered, receipts) == (directly, direct_receipts)
    restructure._clear_staged_apply_cache()
    restructure._clear_canonical_stage_cache()
