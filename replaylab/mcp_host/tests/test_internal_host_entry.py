"""The frozen save owner must use the same pinned tools as the MCP caller."""
import importlib.util
import os
from pathlib import Path
from unittest import mock

from src import internal_host, replaylab_tools


def test_internal_host_configures_tool_scope_before_dispatch():
    path = Path(__file__).resolve().parents[2] / "packaging/internal_host_entry.py"
    spec = importlib.util.spec_from_file_location("tested_internal_host_entry", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    events = []

    def configure(directory):
        events.append(("configure", directory))
        os.environ[replaylab_tools.TOOLS_ENV] = "scoped/tools.json"
        os.environ[replaylab_tools.ROUTES_ENV] = "scoped/routes.json"
        return {"source": "existing-managed-config"}

    def dispatch(argv):
        events.append(("dispatch", argv))
        assert os.environ[replaylab_tools.TOOLS_ENV] == "scoped/tools.json"
        assert os.environ[replaylab_tools.ROUTES_ENV] == "scoped/routes.json"
        return 0

    with mock.patch.dict(os.environ, {}, clear=False), \
            mock.patch.object(replaylab_tools, "configure_environment", side_effect=configure), \
            mock.patch.object(internal_host, "main", side_effect=dispatch):
        assert entry.main(["status"]) == 0
    assert [item[0] for item in events] == ["configure", "dispatch"]
