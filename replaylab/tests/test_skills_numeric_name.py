"""A Skill whose id is digits only is a Skill, not an integer.

The Settings "new Skill" template wrote ``name: 2`` bare; YAML read it as an
integer, and the loader quarantined the user's own Skill as "name must be a
string" while the panel said it was created.
"""

from __future__ import annotations

from pathlib import Path

from replaylab_agent.skills import SkillCatalog


def _write(root: Path, folder: str, header: str) -> None:
    (root / folder).mkdir(parents=True)
    (root / folder / "SKILL.md").write_text(
        "---\n%s\ndescription: Digits are a name too.\n---\n\n# Steps\n1. Look.\n" % header,
        encoding="utf-8")


def test_a_bare_numeric_name_loads_as_its_folder(tmp_path: Path) -> None:
    _write(tmp_path, "2", "name: 2")
    catalog = SkillCatalog([tmp_path]).load_runtime()
    assert sorted(catalog.skills) == ["2"]
    assert catalog.quarantine_count == 0


def test_a_boolean_name_is_still_refused(tmp_path: Path) -> None:
    _write(tmp_path, "yes", "name: yes")
    catalog = SkillCatalog([tmp_path]).load_runtime()
    assert catalog.skills == {}
    assert catalog.quarantine_count == 1


def test_display_case_uses_directory_identity(tmp_path: Path) -> None:
    _write(tmp_path, "spreadsheets", "name: Spreadsheets")
    _write(tmp_path, "presentations", "name: Presentations")
    _write(tmp_path, "other", "name: Unrelated")
    catalog = SkillCatalog([tmp_path]).load_runtime()
    assert sorted(catalog.skills) == ["presentations", "spreadsheets"]
    assert catalog.quarantine_count == 1


def test_the_cli_template_quotes_the_name(tmp_path: Path) -> None:
    from replaylab_agent import cli

    text = cli._SKILL_TEMPLATE.format(identifier="2", title="2")
    assert 'name: "2"' in text
    (tmp_path / "2").mkdir()
    (tmp_path / "2" / "SKILL.md").write_text(text, encoding="utf-8")
    catalog = SkillCatalog([tmp_path]).load_runtime()
    assert sorted(catalog.skills) == ["2"]
