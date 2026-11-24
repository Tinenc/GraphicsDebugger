import json
from pathlib import Path

from replaylab_agent import portable_capabilities as capabilities


def test_installed_built_in_skill_paths_survive_the_catalog_process(monkeypatch, tmp_path):
    from replaylab_agent import cli
    installed = tmp_path / "skills"; installed.mkdir()
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "replaylab-ai.exe"))
    monkeypatch.setattr(cli.sys, "_MEIPASS", str(tmp_path / "disposable"), raising=False)
    assert cli.default_skill_root() == installed
    installed.rmdir()
    assert cli.default_skill_root() == tmp_path / "disposable/.agents/skills"


def test_core_upgrade_does_not_auto_load_leftover_extensions(tmp_path, monkeypatch):
    root = tmp_path / "old-caps"; root.mkdir()
    (root / "bread-capabilities.json").write_text(json.dumps({"schema": "bread-portable-capabilities/v1", "plugins": [{"id": "old"}]}))
    (tmp_path / "bread-distribution.json").write_text(json.dumps({"profile": "core"}))
    monkeypatch.delenv("BREAD_CAPABILITIES_ROOT", raising=False)
    monkeypatch.setattr(capabilities, "app_root", lambda: tmp_path)
    monkeypatch.setattr(capabilities, "bundle_root", lambda: root)
    assert capabilities.manifest() == {}
    assert capabilities.server_definition() is None
    assert capabilities.skill_roots() == ()
    assert (root / "bread-capabilities.json").exists()  # Preserve rollback/user files.
    monkeypatch.setenv("BREAD_CAPABILITIES_ROOT", str(root))
    assert capabilities.manifest()["plugins"] == [{"id": "old"}]


def test_legacy_installs_remain_readable_without_a_distribution_marker(tmp_path, monkeypatch):
    (tmp_path / "bread-capabilities.json").write_text(json.dumps({"schema": "bread-portable-capabilities/v1", "plugins": []}))
    monkeypatch.delenv("BREAD_CAPABILITIES_ROOT", raising=False)
    monkeypatch.setattr(capabilities, "app_root", lambda: tmp_path)
    monkeypatch.setattr(capabilities, "bundle_root", lambda: tmp_path)
    assert capabilities.manifest() == {"schema": "bread-portable-capabilities/v1", "plugins": []}


def test_rdc_skill_bundle_does_not_advertise_removed_document_or_browser_servers(tmp_path, monkeypatch):
    folder = tmp_path / "custom-skills"; folder.mkdir()
    value = {"schema": "bread-portable-capabilities/v1", "distributionProfile": "rdc",
             "plugins": [], "skillRoots": ["custom-skills"], "retainedCompatibilitySkills": ["renderdoc-analyzer"],
             "features": {"portableTools": False, "browser": False}}
    (tmp_path / "bread-capabilities.json").write_text(json.dumps(value))
    monkeypatch.setenv("BREAD_CAPABILITIES_ROOT", str(tmp_path))
    assert capabilities.server_definition() is None
    assert capabilities.public_status()["ready"]
    assert capabilities.skill_roots() == (folder,)
