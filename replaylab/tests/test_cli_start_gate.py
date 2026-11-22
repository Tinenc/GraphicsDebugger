from __future__ import annotations

import importlib.util
import os
from io import BytesIO, StringIO
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest


_PACKAGED_ENTRY = (
    Path(__file__).resolve().parents[1] / "packaging" / "agent_host_entry.py"
)


def _load_packaged_entry():
    name = "_replaylab_start_gate_entry_test_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, _PACKAGED_ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "record",
    (
        b"",
        b"start",
        b"start\r\n",
        b"START\n",
        b" start\n",
        b"start \n",
        b"start-extra\n",
    ),
)
def test_native_start_gate_fails_closed_on_any_non_exact_record(
    monkeypatch: pytest.MonkeyPatch, record: bytes
) -> None:
    import replaylab_agent.cli as cli

    monkeypatch.setenv(cli._NATIVE_START_GATE_ENV, "1")
    monkeypatch.setattr(
        cli.sys, "stdin", SimpleNamespace(buffer=BytesIO(record + b"private-payload"))
    )

    with pytest.raises(ValueError, match="exact start record"):
        cli._consume_native_start_gate(SimpleNamespace(start_gated=False))

    assert cli._NATIVE_START_GATE_ENV not in os.environ


def test_native_start_gate_preserves_binary_stdin_payload_and_is_not_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import replaylab_agent.cli as cli

    payload = b"private prompt or API key\nwith another line"
    source = BytesIO(b"start\n" + payload)
    monkeypatch.setenv(cli._NATIVE_START_GATE_ENV, "1")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=source))

    cli._consume_native_start_gate(SimpleNamespace(start_gated=False))

    assert source.read() == payload
    assert cli._NATIVE_START_GATE_ENV not in os.environ


def test_native_start_gate_supports_text_stdin_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import replaylab_agent.cli as cli

    source = StringIO("start\nremaining text")
    monkeypatch.setenv(cli._NATIVE_START_GATE_ENV, "1")
    monkeypatch.setattr(cli.sys, "stdin", source)

    cli._consume_native_start_gate(SimpleNamespace(start_gated=False))

    assert source.read() == "remaining text"


@pytest.mark.parametrize(
    "argv",
    (
        [
            "profile",
            "subscription",
            "connect",
            "--profile",
            "profile-a",
            "--start-gated",
        ],
        [
            "profile",
            "subscription",
            "test",
            "--profile",
            "profile-a",
            "--start-gated",
        ],
    ),
)
def test_command_owned_start_gate_keeps_its_control_record(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    import replaylab_agent.cli as cli

    source = BytesIO(b"start\ncancel\n")
    monkeypatch.setenv(cli._NATIVE_START_GATE_ENV, "1")
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=source))

    cli._consume_native_start_gate(cli._parser().parse_args(argv))

    assert source.read() == b"start\ncancel\n"
    assert cli._NATIVE_START_GATE_ENV not in os.environ


def test_invalid_native_start_gate_setting_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import replaylab_agent.cli as cli

    dispatched = False

    def print_presets(_as_json: bool) -> None:
        nonlocal dispatched
        dispatched = True

    monkeypatch.setenv(cli._NATIVE_START_GATE_ENV, "true")
    monkeypatch.setattr(cli, "_print_presets", print_presets)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"start\n")))
    monkeypatch.setattr(cli.sys, "stderr", StringIO())

    assert cli.main(["profile", "presets", "--json"]) == 2
    assert dispatched is False
    assert cli._NATIVE_START_GATE_ENV not in os.environ


def test_packaged_entry_dispatches_only_after_gate_and_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import replaylab_agent.cli as cli

    module = _load_packaged_entry()
    payload = b"payload-after-native-gate"
    source = BytesIO(b"start\n" + payload)
    observed: dict[str, object] = {}

    def print_presets(as_json: bool) -> int:
        observed["json"] = as_json
        observed["payload"] = cli.sys.stdin.buffer.read()
        observed["gate_in_environment"] = cli._NATIVE_START_GATE_ENV in os.environ
        return 0

    monkeypatch.setenv(cli._NATIVE_START_GATE_ENV, "1")
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=source))
    monkeypatch.setattr(cli, "_print_presets", print_presets)

    assert module._entry_main(["profile", "presets", "--json"]) == 0
    assert observed == {
        "json": True,
        "payload": payload,
        "gate_in_environment": False,
    }


def test_python_module_entry_honours_native_start_gate(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(source_root), environment.get("PYTHONPATH", ""))
        if value
    )
    environment["REPLAYLAB_START_GATED"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "replaylab_agent",
            "profile",
            "presets",
            "--json",
        ],
        cwd=tmp_path,
        env=environment,
        input=b"start\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert b'"id"' in completed.stdout
