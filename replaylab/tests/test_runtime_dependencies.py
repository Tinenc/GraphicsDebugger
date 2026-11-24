from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import sys

import pytest

from replaylab_agent import runtime_dependencies
from replaylab_agent.errors import ConfigurationError


REPLAYLAB_ROOT = Path(__file__).resolve().parents[1]


def _manifest(tmp_path: Path, dependencies: list[str]) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text("[project]\ndependencies = " + json.dumps(dependencies), encoding="utf-8")
    return path


def test_exact_version_is_read_from_manifest_not_a_second_constant(tmp_path, monkeypatch) -> None:
    path = _manifest(tmp_path, ["deepagents==9.8.7"])
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.8.7")
    assert runtime_dependencies.verify_deepagents_version(path) == "9.8.7"


def test_installed_version_mismatch_is_rejected(tmp_path, monkeypatch) -> None:
    path = _manifest(tmp_path, ["deepagents==9.8.7"])
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.8.8")
    with pytest.raises(ConfigurationError, match=r"requires deepagents==9\.8\.7; found 9\.8\.8"):
        runtime_dependencies.verify_deepagents_version(path)


@pytest.mark.parametrize("declarations", [
    [], ["deepagents>=0.3.3"], ["deepagents==0.3.*"],
    ["deepagents==0.3.3", "deepagents==0.3.3"],
])
def test_missing_ambiguous_or_nonexact_pin_is_rejected(tmp_path, declarations) -> None:
    with pytest.raises(ConfigurationError, match="one exact"):
        runtime_dependencies.verify_deepagents_version(_manifest(tmp_path, declarations))


def test_importable_package_without_distribution_metadata_is_rejected(tmp_path, monkeypatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(ConfigurationError, match="distribution metadata is missing"):
        runtime_dependencies.verify_deepagents_version(_manifest(tmp_path, ["deepagents==0.3.3"]))


def test_frozen_runtime_requires_its_embedded_manifest(tmp_path, monkeypatch) -> None:
    package = tmp_path / "bundle" / "replaylab_agent"
    package.mkdir(parents=True)
    monkeypatch.setattr(runtime_dependencies, "__file__", str(package / "runtime_dependencies.py"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _manifest(tmp_path, ["deepagents==9.8.7"])
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.8.7")
    with pytest.raises(ConfigurationError, match="cannot read"):
        runtime_dependencies.verify_deepagents_version()
    _manifest(package, ["deepagents==9.8.7"])
    assert runtime_dependencies.verify_deepagents_version() == "9.8.7"


def test_real_build_probe_rejects_importable_wrong_framework_version(monkeypatch) -> None:
    source = (REPLAYLAB_ROOT / "packaging" / "Build-ReplayLabRuntime.ps1").read_text(encoding="utf-8")
    probe = source.split("$probeCode = @'\n", 1)[1].split("\n'@", 1)[0]
    monkeypatch.setattr(sys, "argv", ["probe", str(REPLAYLAB_ROOT / "src"), str(REPLAYLAB_ROOT / "mcp_host")])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "999.0.0")
    with pytest.raises(ConfigurationError, match="found 999.0.0"):
        exec(compile(probe, "Build-ReplayLabRuntime.ps1:probe", "exec"), {})
