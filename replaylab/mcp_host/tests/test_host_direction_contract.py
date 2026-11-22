import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from src import server


_ROUTE_KEYS = {
    "autonomousNext",
    "next",
    "nextCalls",
    "nextStep",
    "recommendedActions",
    "requiredAction",
    "strategy",
}


def _assert_no_route_keys(value):
    if isinstance(value, dict):
        assert not (_ROUTE_KEYS & set(value)), value
        for child in value.values():
            _assert_no_route_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_route_keys(child)


def test_asset_not_ready_receipt_exposes_capabilities_not_a_plan():
    payload = json.loads(
        server._active_shader_asset_evidence_not_ready(
            "shader-catalog-not-prepared",
            "catalog is not prepared",
            event_id=11384,
            stage="fragment",
        )
    )

    assert payload["state"] == "not-ready"
    assert payload["requestedShader"] == {
        "event_id": 11384,
        "stage": "fragment",
    }
    assert "get_active_shader_workspace_path" in payload["availableCapabilities"]
    assert payload["availabilityScope"] == "active-shader-asset-evidence-read"
    assert "requiredChecks" not in payload
    assert "blockingConditions" not in payload
    _assert_no_route_keys(payload)


def test_asset_debug_reports_limitations_without_follow_up_steps():
    debug = server._shader_asset_debug(
        {
            "boundTextureNames": [],
            "textureFingerprintStatus": {"status": "unavailable"},
            "boundTextureFingerprints": [],
            "constantBufferValueStatus": {"status": "unavailable"},
            "constantBufferLanes": [],
            "drawGeometry": {"available": False},
            "codeHints": {"unavailable": True},
        },
        {"candidates": []},
    )

    assert debug["limitations"]
    _assert_no_route_keys(debug)
