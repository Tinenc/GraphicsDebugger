from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path
import uuid

import pytest


ENTRY_PATH = Path(__file__).resolve().parents[1] / "packaging" / "agent_host_entry.py"


def _load_entry_module():
    name = "_replaylab_agent_host_entry_test_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_entry_pins_the_copackaged_mcp_host(monkeypatch, tmp_path) -> None:
    module = _load_entry_module()
    agent = tmp_path / "replaylab-ai.exe"
    mcp = tmp_path / "BreadMCP.exe"
    agent.write_bytes(b"agent")
    mcp.write_bytes(b"mcp")

    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "executable", str(agent))
    # Use a tracked empty value: deleting an already-absent key would not let
    # pytest restore the value that _configure_process subsequently creates.
    monkeypatch.setenv("BREAD_MCP_HOST", "")
    monkeypatch.setenv("REPLAYLAB_MCP_HOST", "")

    module._configure_process()

    assert module.os.environ["BREAD_MCP_HOST"] == str(mcp.resolve())
    assert module.os.environ["REPLAYLAB_MCP_HOST"] == str(mcp.resolve())


def test_frozen_entry_preserves_an_explicit_mcp_host(monkeypatch, tmp_path) -> None:
    module = _load_entry_module()
    agent = tmp_path / "replaylab-ai.exe"
    (tmp_path / "BreadMCP.exe").write_bytes(b"mcp")
    agent.write_bytes(b"agent")
    explicit = str((tmp_path / "operator-selected.exe").resolve())

    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "executable", str(agent))
    monkeypatch.setenv("REPLAYLAB_MCP_HOST", explicit)

    module._configure_process()

    assert module.os.environ["REPLAYLAB_MCP_HOST"] == explicit


def test_frozen_entry_dispatches_the_task_filtered_mcp_proxy(monkeypatch) -> None:
    module = _load_entry_module()
    from replaylab_agent import mcp_proxy

    received: list[str] = []

    def fake_main(arguments):
        received.extend(arguments)
        return 17

    monkeypatch.setattr(mcp_proxy, "main", fake_main)

    result = module._dispatch(
        ["-I", "-m", "replaylab_agent.mcp_proxy", "--host", "BreadMCP.exe"]
    )

    assert result == 17
    assert received == ["--host", "BreadMCP.exe"]


def test_frozen_entry_rejects_unknown_internal_modules() -> None:
    module = _load_entry_module()

    with pytest.raises(SystemExit, match="unsupported internal Bread AgentHost module"):
        module._dispatch(["-I", "-m", "replaylab_agent.not_allowed"])


def test_frozen_entry_hides_unexpected_bootstrap_exception(monkeypatch) -> None:
    module = _load_entry_module()

    class BinaryStderr:
        def __init__(self) -> None:
            self.buffer = BytesIO()

    output = BinaryStderr()
    private_path = r"C:\private\agent_host_entry.py"
    secret = "sk-frozen-secret-value"

    def fail_dispatch(_arguments):
        raise NameError(
            f"bootstrap failed at {private_path}; argv=--token={secret}"
        )

    monkeypatch.setattr(module.sys, "stderr", output)
    monkeypatch.setattr(module, "_dispatch", fail_dispatch)

    result = module._entry_main(["run", "--prompt", "private prompt"])

    assert result == 2
    terminal = output.buffer.getvalue().decode("utf-8")
    assert terminal == (
        "replaylab-ai: error[agent-host-bootstrap-failure]: "
        "The local AI Host could not start. "
        "Check RenderDoc's diagnostic log for technical details.\n"
    )
    for forbidden in (
        "Traceback",
        "NameError",
        private_path,
        secret,
        "argv",
        "private prompt",
    ):
        assert forbidden not in terminal


def test_frozen_entry_does_not_reflect_string_system_exit(monkeypatch) -> None:
    module = _load_entry_module()

    class BinaryStderr:
        def __init__(self) -> None:
            self.buffer = BytesIO()

    output = BinaryStderr()
    secret = "sk-system-exit-secret-value"
    monkeypatch.setattr(module.sys, "stderr", output)
    monkeypatch.setattr(
        module,
        "_dispatch",
        lambda _arguments: (_ for _ in ()).throw(SystemExit(f"argv --token={secret}")),
    )

    assert module._entry_main(["run"]) == 2
    terminal = output.buffer.getvalue().decode("utf-8")
    assert "error[agent-host-bootstrap-failure]" in terminal
    assert secret not in terminal
    assert "SystemExit" not in terminal
    assert "argv" not in terminal
