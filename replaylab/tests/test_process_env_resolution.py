"""Regression tests for Windows executable resolution in ``process_env``.

These guard the npm-shim launch contract for ACP agents. npm drops both a bare
POSIX shim (an extensionless shell script) and a real ``.cmd`` launcher of the
same name into one PATH directory; launching the bare shim raises
``[WinError 193] %1 is not a valid Win32 application``. The resolver must prefer
the PATHEXT launcher. The tests force the Windows branch with a small ``os``
shim so they run on any host without needing WindowsPath support.
"""

import os as _real_os
import pathlib
import tempfile

import replaylab_agent.process_env as pe


class _WindowsOSShim:
    """Report ``name == 'nt'`` while delegating everything else to real ``os``.

    ``pathlib`` keeps its own ``import os`` reference, so patching only
    ``process_env.os`` exercises the Windows resolution branch while paths stay
    on the host's real (posix) flavour.
    """

    name = "nt"

    def __getattr__(self, attribute):
        return getattr(_real_os, attribute)


def _write_npm_layout(directory, command):
    root = pathlib.Path(directory)
    (root / command).write_text("#!/bin/sh\nexec node cli\n")  # bare POSIX shim
    (root / f"{command}.cmd").write_text("@echo off\r\nnode cli %*\r\n")
    (root / f"{command}.ps1").write_text("node cli @args\n")


def _resolve_windows(func, *args, environ):
    saved = pe.os
    pe.os = _WindowsOSShim()
    try:
        return func(*args, environ=environ)
    finally:
        pe.os = saved


def test_generic_resolution_prefers_cmd_over_bare_npm_shim():
    with tempfile.TemporaryDirectory() as npmdir:
        _write_npm_layout(npmdir, "acp-claude-code")
        environ = {"PATH": npmdir, "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
        resolved = _resolve_windows(
            pe.resolve_executable, "acp-claude-code", environ=environ
        )
    assert resolved.endswith("acp-claude-code.cmd"), resolved


def test_known_vendor_resolution_prefers_cmd():
    with tempfile.TemporaryDirectory() as npmdir:
        _write_npm_layout(npmdir, "acp-claude-code")
        environ = {
            "PATH": npmdir,
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "APPDATA": str(pathlib.Path(npmdir).parent),
        }
        resolved = _resolve_windows(
            pe.resolve_known_vendor_executable,
            "claude-code-acp",
            "acp-claude-code",
            environ=environ,
        )
    assert resolved.endswith("acp-claude-code.cmd"), resolved


def test_bare_command_still_resolves_plain_executable():
    with tempfile.TemporaryDirectory() as exedir:
        (pathlib.Path(exedir) / "tool.exe").write_text("MZ")
        environ = {"PATH": exedir, "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
        resolved = _resolve_windows(pe.resolve_executable, "tool", environ=environ)
    assert resolved.endswith("tool.exe"), resolved


def test_explicit_suffix_is_honoured_verbatim():
    with tempfile.TemporaryDirectory() as exedir:
        (pathlib.Path(exedir) / "tool.exe").write_text("MZ")
        environ = {"PATH": exedir, "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
        resolved = _resolve_windows(
            pe.resolve_executable, "tool.exe", environ=environ
        )
    assert resolved.endswith("tool.exe"), resolved


def test_claude_acp_registered_as_known_npm_vendor():
    # The official adapter was renamed from @zed-industries/claude-code-acp to
    # @agentclientprotocol/claude-agent-acp (binary ``claude-agent-acp``).
    assert pe._KNOWN_VENDOR_COMMANDS.get("claude-code-acp") == "claude-agent-acp"
    assert pe._NPM_SHIMS.get("claude-code-acp") == "claude-agent-acp.cmd"
