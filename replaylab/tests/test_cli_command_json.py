from __future__ import annotations

import json

import pytest

from replaylab_agent.cli import _parse_agent_command, _parser, _profile_command
from replaylab_agent.profiles import ProfileStore


def test_command_json_preserves_vendor_flags_and_spaces(tmp_path) -> None:
    command = [
        "C:/Program Files/Vendor/agent.exe",
        "--stdio",
        "--label=RenderDoc Agent",
    ]
    args = _parser().parse_args(
        [
            "profile",
            "add",
            "custom-agent",
            "--preset",
            "custom-acp",
            "--command-json",
            json.dumps(command),
        ]
    )
    store = ProfileStore(tmp_path / "profiles.json")

    assert _profile_command(args, store) == 0
    assert store.profiles["custom-agent"].command == command


@pytest.mark.parametrize(
    "encoded",
    [
        "not-json",
        json.dumps({"command": ["agent"]}),
        json.dumps(["agent", 7]),
    ],
)
def test_command_json_rejects_non_argv_values(encoded) -> None:
    args = _parser().parse_args(
        [
            "profile",
            "add",
            "custom-agent",
            "--preset",
            "custom-acp",
            "--command-json",
            encoded,
        ]
    )
    with pytest.raises(ValueError, match="command-json"):
        _parse_agent_command(args)
