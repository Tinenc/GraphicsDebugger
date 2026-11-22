from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src"
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


@pytest.fixture(autouse=True)
def isolated_process_startup_reports(monkeypatch, tmp_path):
    from replaylab_agent import process_diagnostics
    from replaylab_agent.agent_diagnostics import close_logging
    monkeypatch.setattr(process_diagnostics, "default_log_root", lambda: tmp_path / "startup-logs")
    yield
    close_logging()


def _remove_pytest_scratch(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    try:
        path.parent.rmdir()
    except OSError:
        # Another concurrent test process still owns this shared parent.
        pass


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Give each run an isolated OS-temp base and keep it out of the checkout.

    The default numbered-directory pool shares a ``pytest-current`` link across
    processes. Concurrent agent runs on Windows can race while resolving or
    cleaning that link and finish with ``PermissionError`` after every test has
    passed. A unique base avoids both that race and repository-local scratch.
    An explicit base outside the checkout remains the caller's responsibility.
    """

    configured = getattr(config.option, "basetemp", None)
    requested_name = "pytest"
    if configured is not None:
        requested = Path(os.fspath(configured)).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        requested = requested.resolve(strict=False)
        try:
            requested.relative_to(CHECKOUT_ROOT)
        except ValueError:
            return
        requested_name = requested.name or requested_name

    scratch_parent = Path(tempfile.gettempdir()) / "renderdoc-ultra-pytest"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    redirected = scratch_parent / (
        f"{requested_name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    config.option.basetemp = str(redirected)
    atexit.register(_remove_pytest_scratch, redirected)
