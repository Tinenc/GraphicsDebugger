"""Every connection can ask the user a question, single- or multiple-choice.

The three routes reach the panel by different means -- a tool call on the model
API route, an ACP elicitation form when the vendor has one, and a numbered text
block when it does not -- and each of them has to arrive as the same panel
contract. A route that can only ever ask "pick one" is a route where the Agent
has to guess whenever the real answer is "these two".
"""

from __future__ import annotations

from replaylab_agent.agent_protocol import ClarificationOption, ClarificationRequest
from replaylab_agent.native_engine import (
    clarification_from_elicitation,
    clarification_from_numbered_text,
)


def _payload(request: ClarificationRequest) -> dict:
    assert request is not None
    return request.event_payload()


def test_numbered_fallback_asks_one_of_several() -> None:
    request = clarification_from_numbered_text(
        "I need to know which one.\n"
        "[REPLAYLAB QUESTION]\n"
        "Which shader should be replaced?\n"
        "1. MainVS — the vertex stage\n"
        "2. MainPS — the pixel stage\n"
        "[/REPLAYLAB QUESTION]\n"
    )
    payload = _payload(request)
    assert payload["question"] == "Which shader should be replaced?"
    assert payload["selectionMode"] == "single"
    assert [option["label"] for option in payload["options"]] == ["MainVS", "MainPS"]
    assert payload["options"][1]["description"] == "the pixel stage"


def test_numbered_fallback_can_ask_for_several_answers() -> None:
    """A vendor with no question tool must still be able to say 'pick any'."""

    request = clarification_from_numbered_text(
        "[REPLAYLAB QUESTION multiple]\n"
        "Which formats should the export contain?\n"
        "1. PNG — lossless, no mips\n"
        "2. DDS — keeps the mip chain\n"
        "3. EXR — high dynamic range\n"
        "[/REPLAYLAB QUESTION]\n"
    )
    payload = _payload(request)
    assert payload["selectionMode"] == "multiple"
    assert len(payload["options"]) == 3


def test_a_block_without_two_options_is_not_a_question() -> None:
    assert clarification_from_numbered_text("no block here") is None
    assert clarification_from_numbered_text(
        "[REPLAYLAB QUESTION]\nOnly one\n1. Just this\n[/REPLAYLAB QUESTION]") is None


def test_acp_single_select_form_becomes_one_choice() -> None:
    request = clarification_from_elicitation(
        "Which capture?",
        {
            "requestedSchema": {
                "properties": {
                    "shader": {
                        "description": "Which shader should be replaced?",
                        "oneOf": [
                            {"const": "vs", "title": "MainVS", "description": "vertex"},
                            {"const": "ps", "title": "MainPS", "description": "pixel"},
                        ],
                    }
                }
            }
        },
    )
    payload = _payload(request)
    assert payload["selectionMode"] == "single"
    assert [option["label"] for option in payload["options"]] == ["MainVS", "MainPS"]


def test_acp_array_form_becomes_a_multiple_choice() -> None:
    request = clarification_from_elicitation(
        "Which formats?",
        {
            "requestedSchema": {
                "properties": {
                    "formats": {
                        "description": "Which formats should the export contain?",
                        "items": {
                            "anyOf": [
                                {"const": "png", "title": "PNG"},
                                {"const": "dds", "title": "DDS"},
                            ]
                        },
                    }
                }
            }
        },
    )
    payload = _payload(request)
    assert payload["selectionMode"] == "multiple"
    assert [option["label"] for option in payload["options"]] == ["PNG", "DDS"]


def test_one_option_still_reaches_the_panel_as_a_usable_form() -> None:
    """The panel renders buttons, so a question with nothing to press is useless."""

    payload = ClarificationRequest(
        question="Which shader?",
        options=[ClarificationOption(id="vs", label="MainVS")],
        allow_other=False,
    ).event_payload()
    assert len(payload["options"]) == 2
    assert payload["allowOther"] is True


def test_one_question_keeps_one_id() -> None:
    """The panel event and the machine-readable result must name the same question.

    ``event_payload`` is called at least twice per run -- once for the terminal
    event and once for the run result the CLI prints -- and the answer that comes
    back carries the id the user's panel saw.
    """

    request = ClarificationRequest(
        question="Which shader?",
        options=[ClarificationOption(id="vs", label="MainVS"),
                 ClarificationOption(id="ps", label="MainPS")],
    )
    first = request.event_payload()["id"]
    assert request.event_payload()["id"] == first
    assert request.clarification_id == first

    # An id the caller supplied is never replaced.
    given = ClarificationRequest(
        question="Which shader?", clarification_id="q-fixed",
        options=[ClarificationOption(id="vs", label="MainVS"),
                 ClarificationOption(id="ps", label="MainPS")])
    assert given.event_payload()["id"] == "q-fixed"
