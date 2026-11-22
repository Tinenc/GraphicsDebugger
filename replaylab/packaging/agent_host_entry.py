"""PyInstaller entry point for the ReplayLab AgentHost runtime."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Sequence


def _is_regular_unlinked_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(details.st_mode)
        and not (getattr(details, "st_file_attributes", 0) & 0x400)
    )


def _configure_process() -> None:
    """Keep JSON and protocol streams independent of the Windows code page."""

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass

    # A frozen AgentHost cannot derive the installed runtime directory from a
    # source-tree __file__.  Vendor-native Agents spawn their task-filtered proxy
    # through sys.executable, and the parent runtime resolves the upstream MCP
    # host before that spawn.  Pin both paths to this exact co-packaged bundle
    # while preserving an explicit operator/qrenderdoc override.
    if getattr(sys, "frozen", False) and not (
        os.environ.get("BREAD_MCP_HOST", "").strip()
        or os.environ.get("REPLAYLAB_MCP_HOST", "").strip()
    ):
        parent = Path(sys.executable).resolve().parent
        for name in ("BreadMCP.exe",):
            sibling = parent / name
            if _is_regular_unlinked_file(sibling):
                os.environ["BREAD_MCP_HOST"] = str(sibling)
                os.environ["REPLAYLAB_MCP_HOST"] = str(sibling)
                break


def _dispatch(argv: Sequence[str]) -> int:
    """Support the isolated child modules spawned by AgentHost itself."""

    selected = list(argv)
    if selected[:1] == ["-I"]:
        selected.pop(0)

    if selected[:2] == ["-m", "replaylab_agent.mcp_proxy"]:
        from replaylab_agent.mcp_proxy import main

        return main(selected[2:])
    if selected[:2] == ["-m", "replaylab_agent.selftest"]:
        from replaylab_agent.selftest import _child_entry

        return _child_entry(selected[2:])
    if selected[:2] == ["-m", "replaylab_agent.connection_probe"]:
        from replaylab_agent.connection_probe import main
        return main()
    if selected[:2] == ["-m", "replaylab_agent.portable_tools"]:
        from replaylab_agent.portable_tools import main
        sys.argv = [sys.argv[0], *selected[2:]]
        return main()
    if selected[:1] == ["-m"]:
        raise SystemExit("unsupported internal Bread AgentHost module")

    from replaylab_agent.cli import main

    return main(selected)


def _write_safe_bootstrap_failure() -> None:
    """Write only a stable public failure if frozen import/dispatch fails."""

    message = (
        "replaylab-ai: error[agent-host-bootstrap-failure]: "
        "The local AI Host could not start. "
        "Check RenderDoc's diagnostic log for technical details.\n"
    ).encode("utf-8")
    try:
        binary = getattr(sys.stderr, "buffer", None)
        if binary is not None:
            binary.write(message)
            binary.flush()
        else:
            sys.stderr.write(message.decode("utf-8"))
            sys.stderr.flush()
    except Exception:
        # Never let a broken diagnostic stream reopen PyInstaller's default
        # unhandled-exception/traceback path.
        pass


def _entry_main(argv: Sequence[str]) -> int:
    try:
        _configure_process()
        return _dispatch(argv)
    except SystemExit as exc:
        if type(exc.code) is int:
            return exc.code
        _write_safe_bootstrap_failure()
        return 2
    except KeyboardInterrupt:
        return 130
    except BaseException:
        _write_safe_bootstrap_failure()
        return 2


if __name__ == "__main__":
    raise SystemExit(_entry_main(sys.argv[1:]))
