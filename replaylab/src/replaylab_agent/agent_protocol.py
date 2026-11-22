"""Small wire contracts shared by ReplayLab's two Agent engines."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CHILD_EFFECTS: tuple[str, ...] = ("workspace", "project", "ui", "replay")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ChildResult(BaseModel):
    """Bounded Host-authored record for one deepagents sub-agent run."""

    model_config = ConfigDict(extra="forbid")

    child_id: str
    status: Literal["completed", "failed", "cancelled"]
    report: str = ""
    error: str = ""
    agent_id: str = ""
    skills: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    tools_used: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64, pattern=_ID_RE.pattern)
    label: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=600)


class ClarificationRequest(BaseModel):
    """A question for the user; ends the run with ``needs-user-input``."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2400)
    options: list[ClarificationOption] = Field(default_factory=list, max_length=6)
    selection_mode: Literal["single", "multiple"] = "single"
    allow_other: bool = True
    clarification_id: str = Field(default="", max_length=64)

    def event_payload(self) -> dict[str, Any]:
        # Minted once and kept. This is called at least twice per run -- for the panel
        # event and again for the machine-readable result -- so generating it each time
        # gave one question two different ids, and the answer that comes back names an
        # id neither side recognises.
        if not self.clarification_id:
            self.clarification_id = "q-" + uuid.uuid4().hex[:24]
        clarification_id = self.clarification_id
        options = [option.model_dump() for option in self.options]
        if len(options) < 2:
            existing = {option["id"] for option in options}
            for candidate in ("answer", "skip"):
                if len(options) >= 2:
                    break
                if candidate in existing:
                    continue
                options.append(
                    {
                        "id": candidate,
                        "label": (
                            "I'll type the answer"
                            if candidate == "answer"
                            else "Skip"
                        ),
                        "description": "",
                    }
                )
        return {
            "id": clarification_id,
            "question": self.question,
            "selectionMode": self.selection_mode,
            "options": options,
            "allowOther": True if len(self.options) < 2 else self.allow_other,
        }


__all__ = [
    "CHILD_EFFECTS",
    "ChildResult",
    "ClarificationOption",
    "ClarificationRequest",
]
