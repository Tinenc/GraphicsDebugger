from __future__ import annotations

import inspect
import json
from unittest import mock

from replaylab_agent.policy import READ_ONLY_TOOLS
from src import server


COMPOSITES = frozenset(("inspect_event", "analyze_pixel", "inspect_shader"))
UNSUPPORTED_RAW_TOOL_IDS = frozenset(
    ("export_drawcall", "export_to_unity", "get_shader_info", "reverse_shader", "save_texture")
)


class RecordingIPC:
    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict, float | None]] = []

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, dict(params or {}), timeout))
        if self.error is not None:
            raise self.error
        return self.result


def test_composites_delegate_to_the_same_bridge_methods_and_keep_result_schema() -> None:
    cases = (
        (
            server.inspect_event,
            {"event_id": 41},
            "inspect_event",
            {"event_id": 41},
            60.0,
            "renderdoc-internal-inspect-event/v1",
        ),
        (
            server.analyze_pixel,
            {
                "texture_id": 99,
                "x": 12,
                "y": 34,
                "event_id": 41,
                "mip": 1,
                "slice": 2,
                "sample": 3,
                "prefer_geometry": False,
            },
            "analyze_pixel",
            {
                "texture_id": 99,
                "x": 12,
                "y": 34,
                "event_id": 41,
                "mip": 1,
                "slice": 2,
                "sample": 3,
                "prefer_geometry": False,
            },
            120.0,
            "renderdoc-internal-analyze-pixel/v1",
        ),
        (
            server.inspect_shader,
            {"event_id": 41, "stage": "pixel"},
            "inspect_shader",
            {"event_id": 41, "stage": "pixel"},
            120.0,
            "renderdoc-internal-inspect-shader/v2",
        ),
    )

    for function, arguments, method, params, timeout, schema in cases:
        bridge_result = {"schema": schema, "evidence": {"eventId": 41}}
        ipc = RecordingIPC(result=bridge_result)
        with mock.patch.object(server, "ipc", ipc):
            result = json.loads(function(**arguments))
        assert result == bridge_result
        assert ipc.calls == [(method, params, timeout)]


def test_composites_keep_the_common_json_error_envelope() -> None:
    ipc = RecordingIPC(error=RuntimeError("bridge unavailable"))
    with mock.patch.object(server, "ipc", ipc):
        result = json.loads(server.inspect_event(41))

    assert result == {"error": "bridge unavailable", "type": "RuntimeError"}


def test_composite_signatures_match_the_internal_agent_contract() -> None:
    assert str(inspect.signature(server.inspect_event)) == "(event_id: 'int') -> 'str'"
    assert str(inspect.signature(server.inspect_shader)) == (
        "(event_id: 'int', stage: 'str' = 'fragment') -> 'str'"
    )
    assert str(inspect.signature(server.analyze_pixel)) == (
        "(texture_id: 'int', x: 'int', y: 'int', event_id: 'int', mip: 'int' = 0, "
        "slice: 'int' = 0, sample: 'int' = 0, prefer_geometry: 'bool' = True) -> 'str'"
    )


def test_host_admitted_composites_are_available_on_task_mcp() -> None:
    exported = {name for name in COMPOSITES if callable(getattr(server, name, None))}
    assert COMPOSITES <= READ_ONLY_TOOLS
    assert exported == COMPOSITES


def test_raw_sidecar_manifest_does_not_register_retired_agent_tools() -> None:
    registered = {
        function.__name__ for function, _args, _kwargs in server.mcp._registrations
    }

    assert "inspect_shader" in registered
    assert UNSUPPORTED_RAW_TOOL_IDS.isdisjoint(registered)
