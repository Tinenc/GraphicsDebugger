from __future__ import annotations

import json
from pathlib import Path

from src import server


SESSION = "a" * 32
CAPTURE = "b" * 64


def _flow_receipt() -> dict:
    return {
        "schema": "replaylab-current-frame-flow-mcp/v1",
        "schemaVersion": 1,
        "captureId": CAPTURE,
        "captureSessionId": SESSION,
        "flow": {
            "items": [
                {
                    "eventId": 10,
                    "executionIndex": 0,
                    "name": "Draw",
                    "category": "BasePass",
                    "confidence": "candidate",
                    "actionKind": "graphics",
                    "shaders": [
                        {"stage": "vertex", "shaderId": 1},
                        {"stage": "fragment", "shaderId": 2},
                    ],
                    "targets": {
                        "renderTargets": [
                            {"slot": 0, "resourceId": 70, "format": "RGBA8"}
                        ],
                        "depthTargets": [],
                    },
                }
            ],
            "summary": {
                "sourceTruncated": False,
                "truncated": False,
                "resourceFlowTruncated": False,
                "resourceFlowEdges": [],
            },
        },
        "transport": {"truncated": False},
        "applied": False,
    }


def _install_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RENDERDOC_ULTRA_EVENT_ARTIFACTS_HOME", str(tmp_path))
    monkeypatch.setattr(
        server,
        "_active_live_context",
        lambda **unused: {
            "capture": {
                "captureSessionId": SESSION,
                "captureId": CAPTURE,
                "api": "D3D12",
            }
        },
    )
    monkeypatch.setattr(server, "_require_same_active_capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server,
        "analyze_current_frame_flow",
        lambda max_actions=8000: json.dumps(_flow_receipt()),
    )


def test_server_renders_host_managed_diagram_without_a_path_argument(
    monkeypatch, tmp_path
) -> None:
    _install_context(monkeypatch, tmp_path)

    result = json.loads(server.render_current_frame_flow_diagram("resource-flow"))

    assert "error" not in result, result
    assert result["schema"] == "replaylab-event-flow-artifact/v1"
    assert result["diagramKind"] == "resource-flow"
    assert result["eventId"] is None
    assert Path(result["artifact"]["path"]).is_relative_to(tmp_path)
    assert Path(result["manifestPath"]).is_relative_to(tmp_path)
    assert result["artifact"]["policy"]["physicalEvidence"] is False
    assert result["policy"]["replayMutation"] is False
    source_digest = result["sourceFlowResultSha256"]
    assert result["artifact"]["graph"]["snapshotId"] == source_digest
    assert (
        result["artifact"]["provenance"]["sourceFlowResultSha256"]
        == source_digest
    )


def test_server_pipeline_diagram_is_bound_to_exact_admitted_eid(
    monkeypatch, tmp_path
) -> None:
    _install_context(monkeypatch, tmp_path)

    result = json.loads(
        server.render_current_frame_flow_diagram("pipeline-stage", event_id=10)
    )

    assert "error" not in result, result
    assert result["eventId"] == 10
    assert result["artifact"]["graph"]["nodeCount"] == 6


def test_server_rejects_model_style_path_and_ir_surfaces(monkeypatch, tmp_path) -> None:
    _install_context(monkeypatch, tmp_path)

    invalid_theme = json.loads(
        server.render_current_frame_flow_diagram(theme="model-dark-css")
    )
    invalid_event = json.loads(
        server.render_current_frame_flow_diagram("resource-flow", event_id=10)
    )

    assert invalid_theme["schema"] == "replaylab-event-flow-artifact-error/v1"
    assert "theme must be evidence-neutral-v1" in invalid_theme["error"]
    assert "event_id is only valid" in invalid_event["error"]
    assert "output_path" not in str(server.render_current_frame_flow_diagram.__annotations__)


def test_server_rejects_flow_identity_drift(monkeypatch, tmp_path) -> None:
    _install_context(monkeypatch, tmp_path)
    drifted = _flow_receipt()
    drifted["captureSessionId"] = "c" * 32
    monkeypatch.setattr(
        server,
        "analyze_current_frame_flow",
        lambda max_actions=8000: json.dumps(drifted),
    )

    result = json.loads(server.render_current_frame_flow_diagram())

    assert result["schema"] == "replaylab-event-flow-artifact-error/v1"
    assert "identity is invalid" in result["error"]
