"""The Agent writes ``asset-readers.json``; the Host reads it. They must agree.

Two modules validate the same registry file — ``replaylab_agent`` because
Settings edits it, ``mcp_host`` because the Host runs what it finds there. A
reader the UI accepts and the Host then drops would be a registration the user
believes in and the AI never uses, so this holds both to one answer.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from replaylab_agent import agent_asset_readers as agent


def _host_module():
    source = Path(__file__).resolve().parents[1] / "mcp_host" / "src" / "asset_readers.py"
    if not source.is_file():
        pytest.skip("the Host source tree is not checked out beside the Agent")
    spec = importlib.util.spec_from_file_location("_host_asset_readers", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ACCEPTED = (
    {"id": "uex", "command": "C:/tools/uex.exe", "args": ["preview", "{path}"],
     "patterns": ["*.uasset"]},
    {"id": "dds", "command": "reader", "magic": "44445320", "timeoutSeconds": 300},
    {"id": "mixed-1_0.x", "command": "r", "patterns": ["a/**/*.bin"], "magic": "00ff",
     "engines": ["unity"], "probeArgs": ["--probe"], "enabled": False, "note": "n"},
)

REJECTED = (
    {"command": "r", "patterns": ["*.x"]},                       # no id
    {"id": "Bad Id", "command": "r", "patterns": ["*.x"]},       # id shape
    {"id": "a", "patterns": ["*.x"]},                            # no command
    {"id": "a", "command": "r"},                                 # neither patterns nor magic
    {"id": "a", "command": "r", "magic": "zz"},                  # magic is not hex
    {"id": "a", "command": "r", "patterns": ["*.x"], "timeoutSeconds": 0},
    {"id": "a", "command": "r", "patterns": ["*.x"], "timeoutSeconds": 301},
    {"id": "a", "command": "r", "patterns": ["*.x"], "timeoutSeconds": True},
    {"id": "a", "command": "r", "patterns": "*.x"},              # patterns is not a list
    {"id": "a", "command": "r", "patterns": ["*.x"], "args": "one"},
)


def test_both_validators_accept_the_same_registrations() -> None:
    host = _host_module()
    for raw in ACCEPTED:
        assert agent.validate_reader(raw) == host.validate_reader(raw), raw


def test_both_validators_reject_the_same_registrations() -> None:
    host = _host_module()
    for raw in REJECTED:
        with pytest.raises(agent.AssetReaderError):
            agent.validate_reader(raw)
        with pytest.raises(host.AssetReaderError):
            host.validate_reader(raw)


def test_the_registry_file_the_agent_writes_is_the_one_the_host_loads(tmp_path: Path) -> None:
    host = _host_module()
    registry = tmp_path / "asset-readers.json"
    agent.upsert_reader(dict(ACCEPTED[0]), registry)
    agent.upsert_reader({"id": "off", "command": "r", "patterns": ["*.y"], "enabled": False},
                        registry)
    written = json.loads(registry.read_text(encoding="utf-8"))
    assert written["schema"] == host.SCHEMA == agent.SCHEMA
    loaded = host.load_external_readers(registry)
    # The Host runs only enabled readers; Settings still shows the disabled row.
    assert [reader["id"] for reader in loaded] == ["uex"]
    assert [reader["id"] for reader in agent.load_readers(registry)[0]] == ["uex", "off"]


def test_removing_and_re_enabling_a_reader_round_trips(tmp_path: Path) -> None:
    host = _host_module()
    registry = tmp_path / "asset-readers.json"
    agent.upsert_reader(dict(ACCEPTED[0]), registry)
    assert agent.set_reader_enabled("uex", False, registry) is True
    assert host.load_external_readers(registry) == ()
    assert agent.set_reader_enabled("uex", True, registry) is True
    assert len(host.load_external_readers(registry)) == 1
    assert agent.remove_reader("uex", registry) is True
    assert agent.remove_reader("uex", registry) is False
    assert host.load_external_readers(registry) == ()


def test_the_presets_offered_in_settings_are_the_ones_the_host_recommends() -> None:
    host = _host_module()
    assert len(agent.PRESETS) == len(host.PRESETS)
    for mine, theirs in zip(agent.PRESETS, host.PRESETS):
        assert mine == theirs


def test_settings_and_the_host_describe_the_same_built_in_coverage() -> None:
    """"Do I even need a tool for this?" must get one answer, not two."""

    host = _host_module()
    assert agent.BUILTIN_READERS == host.BUILTIN_READERS


def test_both_sides_recognise_the_same_installed_tools() -> None:
    host = _host_module()
    assert agent.KNOWN_TOOLS == host.KNOWN_TOOLS
    for command in ("C:/tools/uex.exe", "/opt/uex", "uex"):
        assert agent.recognise_tool(command) == host.recognise_tool(command)
    assert agent.recognise_tool("totally-unknown-tool.exe") is None


@pytest.mark.parametrize("command", [
    "uex.exe", "umodel.exe", "umodel_64.exe", "ueviewer.exe", "assetstudiocli.exe",
    "fmodel.exe", "assetstudio.exe", "custom-reader.exe",
])
def test_probe_arguments_follow_the_same_headless_contract(command: str) -> None:
    host = _host_module()
    help_text = "  describe  Read one file\n  export  Write files\n"
    assert agent.candidate_invocations(command, help_text) == host.candidate_invocations(
        command, help_text)
    assert agent.help_flags(command) == host.help_flags(command)
    assert () not in agent.help_flags(command)


@pytest.mark.parametrize("command", ["umodel.exe", "umodel_64.exe", "ueviewer.exe"])
def test_non_json_tools_cannot_be_registered_with_a_guessed_viewer_invocation(command) -> None:
    assert agent.candidate_invocations(command, "  preview  Open asset\n") == ()
    with pytest.raises(agent.AssetReaderError, match="declared list/export operations"):
        agent.registration_from_command(command)


def test_discovery_finds_a_known_tool_and_ignores_everything_else(tmp_path) -> None:
    host = _host_module()
    (tmp_path / "uex.exe").write_bytes(b"MZ")
    (tmp_path / "notes.txt").write_text("not a tool", encoding="utf-8")
    (tmp_path / "unrelated.exe").write_bytes(b"MZ")
    found = agent.discover_installed_readers([tmp_path])
    assert [item["id"] for item in found] == ["uex"]
    assert found[0]["command"].endswith("uex.exe")
    assert found[0]["engines"] == ["unreal"]
    assert found == host.discover_installed_readers([tmp_path])


def test_both_registries_default_to_the_same_file(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host_module()
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/example/AppData/Local")
    assert agent.default_readers_path() == host.default_readers_path()


def test_a_preset_completed_with_a_real_command_validates() -> None:
    preset = dict(agent.PRESETS[0])
    preset["command"] = "C:/tools/uex/uex.exe"
    preset["args"] = ["--config", "C:/tools/uex/profiles.json", "preview", "{path}"]
    reader = agent.validate_reader(preset)
    assert reader["id"] == "uex"
    assert "{path}" in reader["args"]
