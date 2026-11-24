import importlib.util
import json
from pathlib import Path


def test_diagnostics_records_ui_lifetime_without_request_data(tmp_path, monkeypatch):
    source = Path(__file__).parents[2] / "renderdoc_mcp_bridge/bridge_diagnostics.py"
    spec = importlib.util.spec_from_file_location("bread_test_diagnostics", source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    module.start("test")
    module.ui_event("ui-begin", "show_frame_render_flow", "publish")
    module.ui_event("ui-end", "show_frame_render_flow", "publish", "ok")
    module.ui_event("ui-begin", "irrelevant-poll", "read")
    module.stop(); module.stop()
    rows = [json.loads(s) for s in next((tmp_path / "ReplayLab/logs").glob("*.jsonl")).read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["started", "ui-begin", "ui-end", "stopped"]
    assert rows[2]["outcome"] == "ok"
    assert all(set(r) == {"schema", "pid", "unixMs", "kind", "method", "callback", "outcome"} for r in rows)
