from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


_MODULE_PATH = Path(__file__).parents[2] / "util/shader_language_server/deploy_provider.py"
_SPEC = importlib.util.spec_from_file_location("bread_provider_deployment", _MODULE_PATH)
deploy_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy_module)


def fixture_archive(tmp_path, *, bad_license=False):
    archive = tmp_path / "provider.zip"
    binary = b"fixed semantic runtime"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("bin/slangd.exe", binary)
        package.writestr("bin/unneeded-codegen.exe", b"not shipped")
        package.writestr("LICENSE", b"license")
        package.writestr("third-party-notices/dependency.txt", b"notice")
        if bad_license:
            package.writestr("third-party-notices/../../outside.txt", b"not authorized")
    manifest = dict(provider="slangd", version="test", archive_size=archive.stat().st_size,
                    archive_sha256=deploy_module.digest(archive),
                    install_subdirectory="integrations/shader-language-server",
                    files=[dict(member="bin/slangd.exe", destination="slangd.exe", size=len(binary),
                                sha256=hashlib.sha256(binary).hexdigest())],
                    license_members=["LICENSE"], license_prefixes=["third-party-notices/"])
    return archive, manifest


def test_only_pinned_runtime_and_licenses_are_installed_with_verified_receipt(tmp_path):
    archive, manifest = fixture_archive(tmp_path)
    app = tmp_path / "app"
    result = deploy_module.deploy(archive, app, manifest)
    target = Path(result["path"])
    assert (target / "slangd.exe").read_bytes() == b"fixed semantic runtime"
    assert (target / "third-party-notices/dependency.txt").read_bytes() == b"notice"
    assert not (target / "unneeded-codegen.exe").exists()
    receipt = json.loads((target / "provider.json").read_text())
    assert all(deploy_module.digest(target / name) == value
               for name, value in receipt["installed_files"].items())
    assert deploy_module.deploy(archive, app, manifest) == result
    assert deploy_module.verify(app, manifest)["verified"]


def test_modified_archive_cannot_touch_an_existing_installation(tmp_path):
    archive, manifest = fixture_archive(tmp_path)
    app = tmp_path / "app"
    result = deploy_module.deploy(archive, app, manifest)
    target = Path(result["path"]) / "slangd.exe"
    before = target.read_bytes()
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="pinned release"):
        deploy_module.deploy(archive, app, manifest)
    assert target.read_bytes() == before


def test_malicious_license_member_is_rejected_before_any_install_write(tmp_path):
    archive, manifest = fixture_archive(tmp_path, bad_license=True)
    app = tmp_path / "app"
    with pytest.raises(ValueError, match="Invalid license path"):
        deploy_module.deploy(archive, app, manifest)
    assert not app.exists()


@pytest.mark.parametrize("removed", ["slangd.exe", "third-party-notices/dependency.txt"])
def test_packaging_verification_rejects_incomplete_runtime_or_license(tmp_path, removed):
    archive, manifest = fixture_archive(tmp_path)
    app = tmp_path / "app"
    target = Path(deploy_module.deploy(archive, app, manifest)["path"])
    (target / removed).unlink()
    with pytest.raises(FileNotFoundError):
        deploy_module.verify(app, manifest)
