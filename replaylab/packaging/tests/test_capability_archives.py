import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from capability_archives import pack
from prepare_capabilities_bundle import seal, verify, stage
from test_finalize_capabilities_archive import base_release


def test_compressed_bundle_keeps_all_dependencies_and_shortens_plugin_roots(tmp_path):
    _, source, metadata = base_release(tmp_path)
    plugin = "plugins/vendor/example/123456/skills/example/SKILL.md"
    path = source / plugin; path.parent.mkdir(parents=True); path.write_text("skill")
    metadata["files"].append({"path": plugin, "size": 5, "sha256": hashlib.sha256(b"skill").hexdigest()})
    metadata["plugins"] = [{"id": "example@vendor", "path": "plugins/vendor/example/123456"}]
    metadata["skillRoots"] = ["plugins/vendor/example/123456/skills"]
    target = tmp_path / "compressed"
    compact = pack(metadata, lambda name: (source / name).open("rb"), target)
    assert compact["plugins"][0]["path"] == "p/example"
    assert (target / "p/example/skills/example/SKILL.md").read_text() == "skill"
    assert compact["skillRoots"] == ["p/example/skills"]
    assert not (target / "python").exists()
    assert (target / "bundles/python.zip").is_file()
    assert verify(target)["componentArchives"] == compact["componentArchives"]
    stage(target, tmp_path / "staged")
    assert verify(tmp_path / "staged")["physicalFiles"] == compact["physicalFiles"]


@pytest.mark.parametrize("private_path", [
    "custom-skills/personal/SKILL.md",
    "custom-skills/.system/personal/SKILL.md",
    "custom-skills/renderdoc-analyzer/scripts/node_modules/personal.js",
    "user-skill-snapshots/example/SKILL.md",
])
def test_public_staging_rejects_private_skills_in_valid_snapshots(tmp_path, private_path):
    _, source, metadata = base_release(tmp_path)
    path = source / private_path
    path.parent.mkdir(parents=True)
    path.write_text("personal procedure", encoding="utf-8")
    seal(source, metadata)
    compressed = tmp_path / "compressed"
    pack(metadata, lambda name: (source / name).open("rb"), compressed)
    assert verify(compressed)
    with pytest.raises(ValueError, match="Unapproved skill in public capabilities"):
        stage(compressed, tmp_path / "staged")
    assert not (tmp_path / "staged").exists()


def test_public_audit_checks_the_contents_of_the_skill_capsule(tmp_path):
    _, source, metadata = base_release(tmp_path)
    public_paths = (
        "custom-skills/renderdoc-analyzer/SKILL.md",
        "custom-skills/.system/imagegen/SKILL.md",
        "node/lib/node_modules/sdk/index.js",
    )
    for relative in public_paths:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("official dependency", encoding="utf-8")
    seal(source, metadata)
    compressed = tmp_path / "compressed"
    compact = pack(metadata, lambda name: (source / name).open("rb"), compressed)
    assert {row["path"] for row in verify(compressed, public=True)["files"]} >= set(public_paths)

    relative = "bundles/custom-skills.zip"
    capsule = compressed / relative
    with zipfile.ZipFile(capsule, "a") as archive:
        archive.writestr("custom-skills/personal/SKILL.md", "unlisted personal skill")
    for record in compact["physicalFiles"]:
        if record["path"] == relative:
            record.update(size=capsule.stat().st_size, sha256=hashlib.sha256(capsule.read_bytes()).hexdigest())
    manifest = compressed / "bread-capabilities.json"
    manifest.write_text(json.dumps(compact), encoding="utf-8")
    with pytest.raises(ValueError, match="Compressed runtime inventory differs"):
        verify(compressed, public=True)

    compact["componentArchives"] = [item for item in compact["componentArchives"] if item["id"] != "custom-skills"]
    manifest.write_text(json.dumps(compact), encoding="utf-8")
    with pytest.raises(ValueError, match="Unregistered capsule"):
        verify(compressed, public=True)
