"""Structured diagnostic issues for shader source operations."""

from __future__ import annotations

REVIEW_VERDICT_SCHEMA = "replaylab-review-verdict/v1"


def reason(
    code: str,
    message: str,
    *,
    location: str = "",
    direction: str = "",
    severity: str = "blocker",
    target: str = "",
    example: str = "",
) -> dict[str, str]:
    """Return facts only; historical remediation arguments are ignored."""

    if severity not in {"blocker", "warning"}:
        raise ValueError("diagnostic severity must be blocker or warning")
    return {
        "code": code,
        "message": message,
        "location": location,
        "severity": severity,
    }
