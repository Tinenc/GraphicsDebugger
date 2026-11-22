from __future__ import annotations

import json
from unittest import mock

from src import server


class TextureIPC:
    def __init__(self, *, selected: int = 11834, current: int = 11834) -> None:
        self.selected = selected
        self.current = current
        self.calls: list[tuple[str, dict, float | None]] = []

    def call(self, method, params=None, timeout=None):
        payload = dict(params or {})
        self.calls.append((method, payload, timeout))
        if method == "get_current_renderdoc_context":
            return {
                "schema": "renderdoc-ultra-current-context/v1",
                "schemaVersion": 1,
                "capture": {
                    "loaded": True,
                    "captureId": "",
                    "captureSessionId": "a" * 32,
                    "path": "D:/captures/frame.rdc",
                    "api": "D3D12",
                },
                "events": {
                    "selectedEventId": self.selected,
                    "currentEventId": self.current,
                },
            }
        if method == "get_bound_textures":
            return [{"resourceId": 7, "bindPoint": 0, "space": 0}]
        raise AssertionError("unexpected IPC method: %s" % method)


def test_missing_event_uses_current_selected_event() -> None:
    fake = TextureIPC()
    with mock.patch.object(server, "ipc", fake):
        result = json.loads(server.get_bound_textures())

    assert result[0]["resourceId"] == 7
    assert fake.calls[-1][0] == "get_bound_textures"
    assert fake.calls[-1][1]["event_id"] == 11834


def test_explicit_event_does_not_read_ui_context() -> None:
    fake = TextureIPC()
    with mock.patch.object(server, "ipc", fake):
        result = json.loads(server.get_bound_textures(77, "pixel"))

    assert result[0]["resourceId"] == 7
    assert [item[0] for item in fake.calls] == ["get_bound_textures"]
    assert fake.calls[0][1] == {"event_id": 77, "stage": "pixel"}


def test_missing_event_reports_clear_error_when_ui_has_no_event() -> None:
    fake = TextureIPC(selected=0, current=0)
    with mock.patch.object(server, "ipc", fake):
        result = json.loads(server.get_bound_textures())

    assert result["type"] == "ValueError"
    assert "no positive selected/current event" in result["error"]
    assert all(item[0] != "get_bound_textures" for item in fake.calls)
