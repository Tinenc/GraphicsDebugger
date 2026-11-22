import importlib.util
import sys
import types
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("cap_bundle", Path(__file__).parents[1] / "prepare_capabilities_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def test_plugin_dependency_copy_preserves_code_but_excludes_accounts(tmp_path):
    source = tmp_path / "source"
    names = ["docs/API.md", "src/runner.py", "LICENSE", "config/defaults.json", "auth.json", ".mcp.json",
             "logs/private.log", "__pycache__/x.pyc"]
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sample")
    output = tmp_path / "bundle"
    bundle.copy_code(source, output)
    assert sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()) == [
        "LICENSE", "config/defaults.json", "docs/API.md", "src/runner.py"]


def test_sealed_bundle_detects_unrecorded_files(tmp_path):
    bundle.seal(tmp_path, {"schema": "bread-portable-capabilities/v1"})
    (tmp_path / "auth.json").write_text("private")
    with pytest.raises(ValueError, match="inventory"):
        bundle.verify(tmp_path)


@pytest.mark.parametrize("selected", [[], ["approved"]])
def test_desktop_import_requires_explicit_skills_and_keeps_runtime_dependencies(tmp_path, monkeypatch, selected):
    fake = types.ModuleType("replaylab_agent.desktop_capabilities")
    fake.discover_desktop_capabilities = lambda _: types.SimpleNamespace(plugins={})
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    desktop = tmp_path / "desktop"
    for name in ("approved", "personal"):
        for relative in ("SKILL.md", "references/guide.md", "scripts/node_modules/private/index.js"):
            path = desktop / "skills" / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("example")
    dependencies = tmp_path / "dependencies"
    for relative in ("python/python.exe", "node/bin/node.exe", "node/lib/node_modules/sdk/index.js",
                     *["python/Lib/site-packages/" + name + "/__init__.py" for name in ("docx", "openpyxl", "pptx", "pypdf")]):
        path = dependencies / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dependency")
    output = tmp_path / "output"
    metadata = bundle.import_desktop(output, desktop, dependencies, custom_skills=selected)
    assert metadata["customSkillSelection"] == selected
    assert not (output / "custom-skills/personal").exists()
    assert not (output / "custom-skills/approved/scripts/node_modules").exists()
    assert (output / "node/lib/node_modules/sdk/index.js").is_file()
    if selected:
        assert (output / "custom-skills/approved/references/guide.md").is_file()


def test_imported_skill_audit_rejects_dependency_cache_even_when_sealed(tmp_path):
    path = tmp_path / "custom-skills/approved/scripts/node_modules/dependency.js"
    path.parent.mkdir(parents=True)
    path.write_text("unexpected")
    bundle.seal(tmp_path, {"schema": "bread-portable-capabilities/v1", "customSkillSelection": ["approved"]})
    with pytest.raises(ValueError, match="dependency cache"):
        bundle.verify(tmp_path)
