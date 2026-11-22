import pytest

from src.review_contract import reason


def test_shader_diagnostic_contains_facts_without_host_plan():
    issue = reason(
        "source-conflict",
        "Draft changed since it was read",
        location="ai/draft.hlsl",
        direction="Run a specific tool next",
        target="another draft",
        example="rewrite from scratch",
    )
    assert issue == {
        "code": "source-conflict",
        "message": "Draft changed since it was read",
        "location": "ai/draft.hlsl",
        "severity": "blocker",
    }


def test_shader_diagnostic_rejects_unknown_severity():
    with pytest.raises(ValueError, match="severity"):
        reason("invalid", "bad", severity="critical")
