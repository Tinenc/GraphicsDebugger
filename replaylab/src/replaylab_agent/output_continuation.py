"""Continue productive provider turns without imposing a task-wide turn quota."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ACP_TURN_LIMIT_REASONS = frozenset({"max_tokens", "max_turn_requests"})


def merge_answer_text(previous: str, current: str) -> str:
    """Keep exact continuation bytes, allowing a complete republished prefix."""
    if not previous or current.startswith(previous):
        return current
    if not current:
        return previous
    return previous + current


def task_work_fingerprint(store: Any) -> str:
    """Use distinct work facts, never call counters or timestamps, as progress."""
    try:
        state = store.task_review_state()
        value = state.get("workFingerprint") if isinstance(state, Mapping) else None
        return value if isinstance(value, str) else ""
    except Exception:
        # Unknown work state cannot manufacture progress. Session/checkpoint
        # safety is checked separately by the engine before calling the loop.
        return ""


@dataclass
class OutputContinuationLoop:
    work_fingerprint: str = ""
    continuations: int = 0
    no_progress_turns: int = 0
    _answers: set[str] = field(default_factory=set)

    def should_continue(self, text: str, work_fingerprint: str) -> bool:
        answer = " ".join(text.split())
        identity = hashlib.sha256(answer.encode("utf-8", errors="replace")).hexdigest() if answer else ""
        new_answer = bool(identity and identity not in self._answers)
        if identity:
            self._answers.add(identity)
        new_work = bool(work_fingerprint and work_fingerprint != self.work_fingerprint)
        if work_fingerprint:
            self.work_fingerprint = work_fingerprint
        self.no_progress_turns = 0 if new_answer or new_work else self.no_progress_turns + 1
        # These are consecutive empty/repeated turns, not a total continuation
        # count. Any new work or answer resets the stall guard.
        if self.no_progress_turns > 2:
            return False
        self.continuations += 1
        return True

    def normal_turn(self) -> None:
        self.no_progress_turns = 0


def continuation_prompt(stop_reason: str) -> str:
    cause = ("this turn's model-request limit" if stop_reason == "max_turn_requests"
             else "the provider's per-response output limit")
    return (
        "The previous turn stopped at " + cause + ". This is a request boundary, not task completion. "
        "Answer text since the last tool boundary is retained: continue its exact text without repeating it, "
        "including any needed leading whitespace. If further tools change the work state, provide a new "
        "self-contained conclusion after those tools. "
        "Continue the existing request from this session's current state; do not restart, reread unchanged "
        "Skills, or repeat completed tools. Split remaining work into smaller independently useful steps "
        "and bounded tool calls instead of emitting one oversized response or tool argument. "
        "Save large source or deliverables incrementally in the authorized workspace and give a concise "
        "answer linking them. Do not duplicate an effect because its response was cut off. Reuse retained "
        "evidence and avoid repeating prior reasoning. If context is getting full, use the session's "
        "supported compaction instead of restarting the task. Finish the remaining work, perform the "
        "required self-review, and provide a self-contained final answer. If progress is impossible, "
        "state the concrete dependency and preserve completed work."
    )
