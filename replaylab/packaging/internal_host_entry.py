"""PyInstaller entry point for qrenderdoc's private ReplayLab service."""

from __future__ import annotations

import os
import sys

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
    # The singleton also owns compilation after editor saves. Resolve the
    # same installation-scoped registry as MCP before importing its server.
    from src.replaylab_host import _prepare_shader_tools
    _prepare_shader_tools()


def main(argv=None) -> int:
    args=list(sys.argv[1:] if argv is None else argv)
    if args==["project-request"]:
        # Transport helpers neither discover compilers nor instantiate services.
        from src.project_http_helper import main as project_request
        return project_request()
    _configure_process()
    from src.internal_host import main as run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
