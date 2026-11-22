"""Read-only filesystem Skill catalog used by both ReplayLab engines.

Agent topology belongs to the selected framework/provider and tool authority
belongs to the task-scoped ReplayLab MCP server. This module deliberately does
not support runtime Agent templates, Skill overlays, or external MCP
registrations: keeping those here would create a second, ineffective source of
truth beside deepagents and the vendor-native Agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .errors import ConfigurationError
from .skills import Skill, SkillCatalog, SkillQuarantineSummary


@dataclass(frozen=True, slots=True)
class SkillInfo:
    id: str
    title: str
    description: str
    category: str
    instructions: str
    path: Path
    # Directory metadata, so the Agent can judge relevance without loading the body.
    version: str = ""
    when: str = ""

    def metadata(self) -> dict[str, str]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
        }


@dataclass
class CatalogView:
    skills: dict[str, SkillInfo] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    # The roots this view was loaded from, so a task-scoped MCP filter can build the
    # same directory in its own process.
    roots: tuple[Path, ...] = ()
    _quarantine: SkillQuarantineSummary = field(
        default_factory=lambda: SkillQuarantineSummary(0, ()),
        repr=False,
    )

    @classmethod
    def load(cls, *, skill_roots: Iterable[Path | str]) -> "CatalogView":
        roots = tuple(Path(root) for root in skill_roots)
        catalog = SkillCatalog(list(roots)).load_runtime()
        view = cls(_quarantine=catalog.quarantine_summary(), roots=roots)
        for diagnostic in catalog.quarantine_diagnostics:
            detail = (
                f"skill quarantined: {diagnostic.code} "
                f"(root {diagnostic.root_index}, entry {diagnostic.entry_index})"
            )
            if diagnostic.skill_id:
                detail += f" [{diagnostic.skill_id}]"
            view.diagnostics.append(detail)
        for skill in catalog.skills.values():
            view.skills[skill.name] = _skill_from_filesystem(skill)
        return view

    def skill(self, skill_id: str) -> SkillInfo:
        try:
            return self.skills[skill_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown skill '{skill_id}'") from exc

    def quarantine_summary(self) -> SkillQuarantineSummary:
        return self._quarantine


def _skill_from_filesystem(skill: Skill) -> SkillInfo:
    return SkillInfo(
        id=skill.name,
        title=skill.name.replace("-", " ").title(),
        description=skill.description,
        category=skill.category,
        instructions=skill.instructions,
        path=skill.path,
        version=str(skill.metadata.get("replaylab-version", "") or "").strip()[:32],
        when=str(skill.metadata.get("replaylab-when", "") or "").strip()[:512],
    )


__all__ = ["CatalogView", "SkillInfo"]
