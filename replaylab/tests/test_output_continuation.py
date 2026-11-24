"""Task progress, rather than total turns, determines provider continuation."""
from replaylab_agent.output_continuation import OutputContinuationLoop, merge_answer_text


def test_answer_continuation_preserves_mid_word_whitespace_and_exact_chinese_sections():
    sections = ["第一节：Resource", "Id::1011\n\n第二节：法线", "纹理。\n第三节：保留全部结论。"]
    answer = ""
    for section in sections:
        answer = merge_answer_text(answer, section)
    assert answer == "第一节：ResourceId::1011\n\n第二节：法线纹理。\n第三节：保留全部结论。"
    assert merge_answer_text(answer, "") == answer
    assert merge_answer_text(answer, sections[-1]) == answer + sections[-1]
    assert merge_answer_text(answer, answer + "\n完整复述的新结论。") == answer + "\n完整复述的新结论。"


def test_distinct_work_can_span_many_empty_output_turns():
    loop = OutputContinuationLoop("initial")
    for index in range(20):
        assert loop.should_continue("", f"work-{index}")
    assert loop.continuations == 20
    assert loop.no_progress_turns == 0


def test_distinct_answer_chunks_can_span_many_turns_without_tools():
    loop = OutputContinuationLoop("unchanged")
    for index in range(20):
        assert loop.should_continue(f"New answer section {index}", "unchanged")
    assert loop.no_progress_turns == 0


def test_repeated_text_and_unchanged_tool_results_cannot_reset_stall_guard():
    loop = OutputContinuationLoop("same-work")
    assert loop.should_continue("Repeated answer", "same-work")
    assert loop.should_continue("Repeated\n answer", "same-work")
    assert loop.should_continue("Repeated answer", "same-work")
    assert not loop.should_continue("Repeated answer", "same-work")


def test_new_work_resets_consecutive_empty_turns_instead_of_exhausting_total_quota():
    loop = OutputContinuationLoop("initial")
    for index in range(10):
        assert loop.should_continue("", "")  # Unknown work never manufactures progress.
        assert loop.should_continue("", "")
        assert loop.should_continue("", f"new-work-{index}")
    assert loop.no_progress_turns == 0
    assert loop.should_continue("", "new-work-9")
    assert loop.should_continue("", "new-work-9")
    assert not loop.should_continue("", "new-work-9")


def test_successful_normal_turn_breaks_consecutive_limit_stalls():
    loop = OutputContinuationLoop()
    assert loop.should_continue("", "")
    assert loop.should_continue("", "")
    loop.normal_turn()
    assert loop.should_continue("", "")
    assert loop.no_progress_turns == 1
