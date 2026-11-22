"""Agent Skills compatible discovery for reusable ReplayLab procedures.

Filesystem Skills are instruction, evidence, and output-contract bundles. They
never grant tools, transports, or workflow effects; those are derived from the
frozen Host task and the live provider/tool catalogs at execution time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ConfigurationError

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REFERENCE_RE = re.compile(
    r"^references/[a-z0-9][a-z0-9._/-]*\.md$", re.IGNORECASE
)
_MAX_SKILL_FILE_BYTES = 256 * 1024
_MAX_SKILL_BODY_CHARS = 128 * 1024
_MAX_SKILL_INSTRUCTION_CHARS = 256 * 1024
_MAX_METADATA_ENTRIES = 64
_MAX_METADATA_KEY_CHARS = 128
_MAX_METADATA_VALUE_CHARS = 16 * 1024
_MAX_REQUIRED_REFERENCES = 32
_MAX_REFERENCE_BYTES = 64 * 1024
_MAX_REQUIRED_REFERENCE_BYTES = 128 * 1024
_MAX_RUNTIME_QUARANTINE_DIAGNOSTICS = 64
_MAX_DIAGNOSTIC_SKILL_ID_LENGTH = 96
_MAX_SKILL_VERSION = 1_000_000


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    instructions: str
    metadata: dict[str, str]

    @property
    def category(self) -> str:
        return self.metadata.get("replaylab-category", "other")

def _positive_metadata_version(skill: Skill, key: str) -> None:
    raw = skill.metadata.get(key)
    if raw is None:
        return
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"skill '{skill.name}' has an invalid {key} value"
        ) from exc
    if not 1 <= value <= _MAX_SKILL_VERSION:
        raise ConfigurationError(
            f"skill '{skill.name}' {key} must be between 1 and "
            f"{_MAX_SKILL_VERSION}"
        )


def validate_skill_semantics(skill: Skill) -> Skill:
    """Validate runtime-consumed Skill semantics without filesystem access.

    Both strict and resilient discovery call this function before admission.
    It deliberately validates procedure metadata only; no Skill field grants
    tools, transports, or effects.
    """

    if not skill.instructions.strip():
        raise ConfigurationError(f"skill '{skill.name}' instructions are empty")
    if len(skill.instructions) > _MAX_SKILL_INSTRUCTION_CHARS:
        raise ConfigurationError(
            f"skill '{skill.name}' instructions exceed 256 Ki characters"
        )
    if len(skill.metadata) > _MAX_METADATA_ENTRIES:
        raise ConfigurationError(
            f"skill '{skill.name}' metadata may contain at most "
            f"{_MAX_METADATA_ENTRIES} entries"
        )
    for key, value in skill.metadata.items():
        if not key or len(key) > _MAX_METADATA_KEY_CHARS:
            raise ConfigurationError(
                f"skill '{skill.name}' has an empty or oversized metadata key"
            )
        if len(value) > _MAX_METADATA_VALUE_CHARS:
            raise ConfigurationError(
                f"skill '{skill.name}' metadata value for '{key}' is too long"
            )

    category = skill.category
    if len(category) > _MAX_DIAGNOSTIC_SKILL_ID_LENGTH or not _NAME_RE.fullmatch(
        category
    ):
        raise ConfigurationError(
            f"skill '{skill.name}' has an invalid replaylab-category value"
        )

    # Skill metadata is descriptive procedure metadata only.  The retired
    # ``replaylab-min-successful-tools``/``required-*-tools`` keys are
    # deliberately not interpreted here: old packages may still contain them,
    # but they cannot grant, filter, order, or validate runtime tools.
    _positive_metadata_version(skill, "replaylab-version")
    _positive_metadata_version(skill, "replaylab-registry-version")

    registry_virtual = skill.metadata.get("replaylab-registry-virtual")
    if registry_virtual is not None:
        # This marker is injected only after Registry validation. A packaged
        # instruction file cannot declare itself to be a Registry projection.
        raise ConfigurationError(
            f"skill '{skill.name}' must not define replaylab-registry-virtual"
        )
    return skill


@dataclass(frozen=True, slots=True)
class SkillQuarantineDiagnostic:
    """Bounded, content-free diagnostic for one runtime discovery failure."""

    code: str
    root_index: int
    entry_index: int
    skill_id: str = ""

    def as_dict(self) -> dict[str, str | int]:
        value: dict[str, str | int] = {
            "code": self.code,
            "rootIndex": self.root_index,
            "entryIndex": self.entry_index,
        }
        if self.skill_id:
            value["skillId"] = self.skill_id
        return value


@dataclass(frozen=True, slots=True)
class SkillQuarantineSummary:
    """Safe runtime/doctor view of quarantined packaged definitions."""

    quarantine_count: int
    diagnostics: tuple[SkillQuarantineDiagnostic, ...]

    @property
    def truncated(self) -> bool:
        return self.quarantine_count > len(self.diagnostics)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "replaylab-skill-quarantine-summary/v1",
            "quarantineCount": self.quarantine_count,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "truncated": self.truncated,
        }

    def doctor_detail(self) -> str:
        counts: dict[str, int] = {}
        for diagnostic in self.diagnostics:
            counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
        codes = ", ".join(
            f"{code}:{count}" for code, count in sorted(counts.items())
        )
        detail = f"{self.quarantine_count} skill definition(s) quarantined"
        if codes:
            detail += f"; bounded codes: {codes}"
        # Which folders, so the user can open the right SKILL.md: the folder
        # name is the only coordinate a person can act on. rootIndex 0 is the
        # bundled set, anything later is one of the user's own Skill folders.
        named = [
            "%s (%s)" % (item.skill_id, "bundled" if item.root_index == 0
                         else "user folder %d" % item.root_index)
            for item in self.diagnostics if item.skill_id
        ]
        if named:
            detail += "; folders: " + ", ".join(named)
        if self.truncated:
            detail += "; diagnostic list truncated"
        return detail


class SkillCatalog:
    def __init__(self, roots: list[Path | str]) -> None:
        self.roots = [Path(root) for root in roots]
        self.skills: dict[str, Skill] = {}
        self.quarantine_diagnostics: tuple[SkillQuarantineDiagnostic, ...] = ()
        self.quarantined_skill_ids: frozenset[str] = frozenset()
        self.quarantine_count = 0

    def load(self) -> "SkillCatalog":
        discovered: dict[str, Skill] = {}
        origin: dict[str, Path] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for file in sorted(root.glob("*/SKILL.md")):
                skill = validate_skill_semantics(self._read(file))
                # Same rule as load_runtime: a later root overriding an earlier one is
                # how an edited copy replaces the bundled Skill; two copies inside one
                # root are ambiguous because nothing orders them.
                if skill.name in discovered and origin.get(skill.name) == root:
                    raise ConfigurationError(f"duplicate skill name: {skill.name}")
                origin[skill.name] = root
                discovered[skill.name] = skill
        self.skills = discovered
        self.quarantine_diagnostics = ()
        self.quarantined_skill_ids = frozenset()
        self.quarantine_count = 0
        return self

    def load_runtime(self) -> "SkillCatalog":
        """Load independent Skills while quarantining invalid package entries.

        This is deliberately weaker than :meth:`load` only in availability:
        an invalid entry is never partially admitted or executed. Diagnostics
        contain fixed codes and bounded coordinates, not exception text, file
        content, or filesystem paths. Duplicate identities are quarantined as
        a whole so discovery order cannot choose executable instructions.
        """

        discovered: dict[str, Skill] = {}
        diagnostics: list[SkillQuarantineDiagnostic] = []
        quarantined_ids: set[str] = set()
        quarantine_count = 0

        def record(
            code: str,
            root_index: int,
            entry_index: int,
            skill_id: str = "",
        ) -> None:
            nonlocal quarantine_count
            quarantine_count += 1
            if len(diagnostics) >= _MAX_RUNTIME_QUARANTINE_DIAGNOSTICS:
                return
            diagnostics.append(
                SkillQuarantineDiagnostic(
                    code=code,
                    root_index=root_index,
                    entry_index=entry_index,
                    skill_id=skill_id,
                )
            )

        # Which root each accepted Skill came from, so a later root can override an
        # earlier one while two copies in the same root stay ambiguous.
        origin: dict[str, int] = {}
        for root_index, root in enumerate(self.roots):
            try:
                if not root.is_dir():
                    continue
                files = sorted(root.glob("*/SKILL.md"))
            except (OSError, RuntimeError):
                record("skill-root-unreadable", root_index, -1)
                continue
            for entry_index, file in enumerate(files):
                directory_name = file.parent.name
                skill_hint = (
                    directory_name
                    if len(directory_name) <= _MAX_DIAGNOSTIC_SKILL_ID_LENGTH
                    and _NAME_RE.fullmatch(directory_name)
                    else ""
                )
                try:
                    skill = validate_skill_semantics(self._read(file))
                except UnicodeDecodeError:
                    code = "skill-file-not-utf8"
                except OSError:
                    code = "skill-file-unreadable"
                except (ConfigurationError, RuntimeError, ValueError):
                    code = "skill-definition-invalid"
                else:
                    if skill.name in quarantined_ids:
                        record(
                            "duplicate-skill-name",
                            root_index,
                            entry_index,
                            skill.name,
                        )
                        continue
                    if skill.name in discovered:
                        # Roots are ordered: the bundled one first, then the folders
                        # the user added, and Settings writes an edited copy into one
                        # of those. A later root overriding an earlier one is that
                        # feature, not an ambiguity -- quarantining both used to make
                        # editing a Skill delete it, and editing replaylab-core stop
                        # every task. Two copies inside ONE root really is ambiguous,
                        # because nothing orders them.
                        if origin.get(skill.name, root_index) == root_index:
                            discovered.pop(skill.name, None)
                            origin.pop(skill.name, None)
                            quarantined_ids.add(skill.name)
                            record(
                                "duplicate-skill-name",
                                root_index,
                                entry_index,
                                skill.name,
                            )
                            continue
                    discovered[skill.name] = skill
                    origin[skill.name] = root_index
                    continue

                # The directory name is only a bounded diagnostic hint. When it
                # collides with a previously valid definition, quarantine that
                # identity entirely rather than allowing discovery order to
                # select instructions from one side of an ambiguous package.
                if skill_hint:
                    discovered.pop(skill_hint, None)
                    quarantined_ids.add(skill_hint)
                record(code, root_index, entry_index, skill_hint)

        self.skills = discovered
        self.quarantine_diagnostics = tuple(diagnostics)
        self.quarantined_skill_ids = frozenset(quarantined_ids)
        self.quarantine_count = quarantine_count
        return self

    def quarantine_summary(self) -> SkillQuarantineSummary:
        """Return bounded diagnostics that contain no paths or source text."""

        return SkillQuarantineSummary(
            quarantine_count=self.quarantine_count,
            diagnostics=self.quarantine_diagnostics,
        )

    def get(self, name: str) -> Skill:
        try:
            return self.skills[name]
        except KeyError as exc:
            if name in self.quarantined_skill_ids:
                raise ConfigurationError(
                    f"Bread skill '{name}' is unavailable because its packaged "
                    "definition was quarantined"
                ) from exc
            raise ConfigurationError(f"unknown Bread skill: {name}") from exc

    def summaries(self) -> str:
        return "\n".join(
            f"- {skill.name} [{skill.category}]: {skill.description}"
            for skill in sorted(self.skills.values(), key=lambda item: item.name)
        )

    @staticmethod
    def _read(path: Path) -> Skill:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_SKILL_FILE_BYTES + 1)
        if len(payload) > _MAX_SKILL_FILE_BYTES:
            raise ConfigurationError(f"skill file exceeds 256 KiB: {path}")
        text = payload.decode("utf-8", errors="strict")
        # Match Path.read_text/open(newline=None) semantics used by the prior
        # loader while retaining the pre-decode byte bound on every platform.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.startswith("---\n"):
            raise ConfigurationError(f"skill frontmatter is missing: {path}")
        try:
            _, header, body = text.split("---", 2)
            value = yaml.safe_load(header) or {}
        except (ValueError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"invalid skill frontmatter in {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigurationError(f"skill frontmatter must be a mapping: {path}")
        raw_name = value.get("name", "")
        raw_description = value.get("description", "")
        if isinstance(raw_name, int) and not isinstance(raw_name, bool):
            # `name: 2` is an integer to YAML; the folder is still called "2".
            raw_name = str(raw_name)
        if not isinstance(raw_name, str) or not isinstance(raw_description, str):
            raise ConfigurationError(f"skill name and description must be strings: {path}")
        # Some compatible publishers use a display-cased frontmatter name.
        # Admit only a case difference from the canonical directory identity;
        # do not silently rename unrelated or invalid definitions.
        name = raw_name.casefold() if raw_name.casefold() == path.parent.name else raw_name
        description = raw_description.strip()
        if not _NAME_RE.fullmatch(name) or path.parent.name != name:
            raise ConfigurationError(f"skill name must match its directory: {path}")
        if not description or len(description) > 1024:
            raise ConfigurationError(f"skill description is empty or too long: {path}")
        raw_metadata = value.get("metadata", {}) or {}
        if not isinstance(raw_metadata, dict):
            raise ConfigurationError(f"skill metadata must be a mapping: {path}")
        metadata: dict[str, str] = {}
        for key, item in raw_metadata.items():
            if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
                raise ConfigurationError(
                    f"skill metadata keys and values must be scalar: {path}"
                )
            metadata[key] = str(item)
        # Agent Skills may contain an implementation-specific ``allowed-tools``
        # field. ReplayLab deliberately ignores it: instructional files are not
        # an execution-authority source.
        instructions = body.strip()
        if len(instructions) > _MAX_SKILL_BODY_CHARS:
            raise ConfigurationError(f"skill body exceeds 128 Ki characters: {path}")
        reference_names = tuple(
            item
            for item in metadata.get("replaylab-required-references", "").split()
            if item
        )
        if len(reference_names) != len(set(reference_names)):
            raise ConfigurationError(
                f"skill '{name}' required references must be unique"
            )
        if len(reference_names) > _MAX_REQUIRED_REFERENCES:
            raise ConfigurationError(
                f"skill '{name}' may require at most {_MAX_REQUIRED_REFERENCES} references"
            )
        skill_root = path.parent.resolve()
        reference_sections: list[str] = []
        reference_bytes = 0
        for relative_name in reference_names:
            normalized = relative_name.replace("\\", "/")
            if not _REFERENCE_RE.fullmatch(normalized) or ".." in normalized.split("/"):
                raise ConfigurationError(
                    f"skill '{name}' has an invalid required reference: {relative_name}"
                )
            raw_reference = path.parent / Path(normalized)
            if raw_reference.is_symlink():
                raise ConfigurationError(
                    f"skill '{name}' required reference must not be a symlink: "
                    f"{relative_name}"
                )
            try:
                reference_path = raw_reference.resolve(strict=True)
                reference_path.relative_to(skill_root)
                with reference_path.open("rb") as stream:
                    payload = stream.read(_MAX_REFERENCE_BYTES + 1)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ConfigurationError(
                    f"skill '{name}' required reference is unavailable: {relative_name}"
                ) from exc
            if len(payload) > _MAX_REFERENCE_BYTES:
                raise ConfigurationError(
                    f"skill '{name}' required reference exceeds 64 KiB: {relative_name}"
                )
            reference_bytes += len(payload)
            if reference_bytes > _MAX_REQUIRED_REFERENCE_BYTES:
                raise ConfigurationError(
                    f"skill '{name}' required references exceed 128 KiB"
                )
            try:
                reference_text = payload.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError as exc:
                raise ConfigurationError(
                    f"skill '{name}' required reference is not UTF-8: {relative_name}"
                ) from exc
            if not reference_text:
                raise ConfigurationError(
                    f"skill '{name}' required reference is empty: {relative_name}"
                )
            reference_sections.append(
                f"## Mandatory reference: {normalized}\n\n{reference_text}"
            )
        if reference_sections:
            instructions += "\n\n" + "\n\n".join(reference_sections)
        return Skill(
            name,
            description,
            path.resolve(),
            instructions,
            metadata,
        )
