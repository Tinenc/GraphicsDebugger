"""Cut every Host test off from the developer machine's real ReplayLab state.

``src.server`` resolves its state locations once, at import time, from
``%LOCALAPPDATA%``: the active project, the Shader tools config, the live-edit
registry, the console-event directory, the IPC root, the unpacked-asset job
root. On a machine that actually runs ReplayLab those point at real data, so a
test that creates a workspace in a temp directory is refused by the *user's*
active project, and a run's result depends on which capture the developer last
opened. That is not a test failing; that is a test reading someone's desktop.

Redirecting the root before any test module imports the server gives every run
the same empty machine. Tests that need an active project patch
``_active_project_service`` themselves, which still works -- this only removes
the ambient one.

``RENDERDOC_ULTRA_TEST_TEMP`` is left alone: it is how a developer aims the
tests' scratch space at a fast disk, and it holds no state of its own.
"""

import atexit
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest


CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


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


# Explicit overrides win over LOCALAPPDATA everywhere they are read, so an
# outer environment that sets them would defeat the redirect below.
_STATE_OVERRIDES = (
    "RENDERDOC_ULTRA_PROJECT_REGISTRY",
    "BREAD_ROUTING_INSTANCE",
    "REPLAYLAB_INTERNAL_RUNTIME_DIR",
    "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE",
    "RENDERDOC_ULTRA_PROJECTS_HOME",
    "RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG",
    "RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG",
    "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY",
    "RENDERDOC_ULTRA_SHADER_REPRESENTATIONS_ROOT",
    "RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT",
    "RENDERDOC_ULTRA_EVENT_ARTIFACTS_HOME",
    "RENDERDOC_ULTRA_EVENT_TEXTURE_EVIDENCE_ROOT",
    "RENDERDOC_ULTRA_UNPACKED_ASSET_JOB_ROOT",
    "REPLAYLAB_CONFIG_DIR",
    "REPLAYLAB_RUNTIME_DIR",
    "REPLAYLAB_HOST_INSTANCE_ID",
    "REPLAYLAB_INSTANCE_ID",
)


def _isolate_machine_state() -> None:
    sandbox = tempfile.TemporaryDirectory(prefix="replaylab-host-tests-")
    def cleanup_sandbox():
        # Test logging can hold agent-core.log until logging.shutdown's earlier
        # atexit hook. Close handlers before removing this test-owned sandbox.
        import logging
        logging.shutdown()
        sandbox.cleanup()
    atexit.register(cleanup_sandbox)
    root = Path(sandbox.name)
    for name in _STATE_OVERRIDES:
        os.environ.pop(name, None)
    # Both branches of the resolver: LOCALAPPDATA on Windows, XDG_DATA_HOME
    # elsewhere. Setting both means the same empty machine on either platform.
    (root / "LocalAppData").mkdir(parents=True, exist_ok=True)
    (root / "share").mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(root / "LocalAppData")
    os.environ["XDG_DATA_HOME"] = str(root / "share")


_isolate_machine_state()
