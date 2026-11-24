"""PyInstaller entry point for the first-party ReplayLab MCP sidecar."""

from __future__ import annotations

import os
import sys

from src.replaylab_host import main


def _configure_process() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass


if __name__ == "__main__":
    _configure_process()
    raise SystemExit(main())
