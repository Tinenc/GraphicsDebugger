"""Compact initial UI target hints for task-scoped Bread MCP routes.

The conversation can select a different target. The Host validates concrete
capture, workspace, source revision and replay effects at the operation itself;
the UI snapshot is never an independent workflow or completion gate.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

from .errors import PolicyError

SUPPORTED_INTENT_KINDS = (
    "explain-current-event",
    "inspect-picked-pixel",
    "reverse-edit-selected-shader",
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 1 <= result <= 0xFFFFFFFFFFFFFFFF else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 0x7FFFFFFF else None


def _stage(value: Any) -> str:
    stage = str(value or "").strip().upper()
    aliases = {
        "VERTEX": "VS",
        "HULL": "HS",
        "DOMAIN": "DS",
        "GEOMETRY": "GS",
        "PIXEL": "PS",
        "FRAGMENT": "PS",
        "COMPUTE": "CS",
        "TASK": "AS",
        "AMPLIFICATION": "AS",
        "MESH": "MS",
        "FS": "PS",
        "VTX": "VS",
        "TCS": "HS",
        "TES": "DS",
        "TESS_CTRL": "HS",
        "TESS_EVAL": "DS",
    }
    return aliases.get(stage, stage)


@dataclass(frozen=True, slots=True)
class ToolCallScope:
    intent_kind: str = ""
    event_id: int | None = None
    resource_id: int | None = None
    stage: str = ""
    x: int | None = None
    y: int | None = None
    mip: int | None = None
    slice: int | None = None
    sample: int | None = None
    entry_point: str = ""
    raw_sha256: str = ""
    capture_session_id: str = ""
    capture_id: str = ""
    capture_path: str = ""

    @classmethod
    def from_request(
        cls, context: dict[str, Any], intent: dict[str, Any] | None
    ) -> "ToolCallScope":
        intent_value = _mapping(intent)
        arguments = _mapping(intent_value.get("arguments"))
        kind = str(intent_value.get("kind", "") or "")
        events = _mapping(context.get("events"))
        action = _mapping(context.get("action"))
        texture = _mapping(context.get("texture"))
        capture = _mapping(context.get("capture"))
        picked = _mapping(arguments.get("pixel")) or _mapping(texture.get("pickedPixel"))
        subresource = _mapping(arguments.get("subresource")) or _mapping(
            texture.get("subresource")
        )
        focused_shader = _mapping(_mapping(context.get("shaders")).get("focused"))

        event_id = _positive_int(_first(arguments, "eventId", "event_id"))
        ui_event_id = _positive_int(
            _first(events, "currentEventId", "selectedEventId", "eventId")
        ) or _positive_int(_first(action, "eventId", "event_id"))
        # An explicitly routed event may differ from the UI snapshot. Its
        # Shader must be observed at that event; the focused Shader belongs to
        # the UI event and cannot supply the other event's identity.
        ui_matches_target = event_id is None or event_id == ui_event_id
        if not ui_matches_target:
            focused_shader = {}
        if event_id is None:
            event_id = ui_event_id
        resource_id = _positive_int(
            _first(
                arguments,
                "resourceId",
                "resource_id",
                "textureId",
                "texture_id",
                "textureResourceId",
                "shaderResourceId",
            )
        )
        if resource_id is None:
            source = focused_shader if kind == "reverse-edit-selected-shader" else texture
            resource_id = _positive_int(_first(source, "resourceId", "resource_id"))
        stage = _stage(
            _first(arguments, "stage", "shaderStage")
            or focused_shader.get("stage")
            or (context.get("focusStage") if ui_matches_target else None)
        )
        x = _nonnegative_int(_first(arguments, "x", "pixelX"))
        y = _nonnegative_int(_first(arguments, "y", "pixelY"))
        if x is None:
            x = _nonnegative_int(picked.get("x"))
        if y is None:
            y = _nonnegative_int(picked.get("y"))
        mip = _nonnegative_int(
            _first(arguments, "mip", "mipLevel")
            if _first(arguments, "mip", "mipLevel") is not None
            else subresource.get("mip")
        )
        slice_index = _nonnegative_int(
            _first(arguments, "slice", "arraySlice")
            if _first(arguments, "slice", "arraySlice") is not None
            else subresource.get("slice")
        )
        sample = _nonnegative_int(
            _first(arguments, "sample", "sampleIndex")
            if _first(arguments, "sample", "sampleIndex") is not None
            else subresource.get("sample")
        )
        entry_point = str(
            _first(arguments, "entryPoint", "entry_point")
            or focused_shader.get("entryPoint", "")
        ).strip()
        raw_sha256 = str(
            _first(arguments, "rawSha256", "raw_sha256")
            or focused_shader.get("rawSha256", "")
        ).strip().lower()
        if raw_sha256 and re.fullmatch(r"[0-9a-f]{64}", raw_sha256) is None:
            raw_sha256 = ""

        if kind == "explain-current-event":
            resource_id = None
            stage = entry_point = raw_sha256 = ""
            x = y = mip = slice_index = sample = None
        elif kind == "inspect-picked-pixel":
            stage = entry_point = raw_sha256 = ""
        elif kind == "reverse-edit-selected-shader":
            x = y = mip = slice_index = sample = None

        value = cls(
            intent_kind=kind,
            event_id=event_id,
            resource_id=resource_id,
            stage=stage,
            x=x,
            y=y,
            mip=mip,
            slice=slice_index,
            sample=sample,
            entry_point=entry_point,
            raw_sha256=raw_sha256,
            capture_session_id=str(capture.get("captureSessionId", "") or "")
            .strip()
            .lower(),
            capture_id=str(capture.get("captureId", "") or "").strip().lower(),
            capture_path=str(capture.get("path", "") or "").strip(),
        )
        return value

    def validate(self, tool: str, arguments: dict[str, Any]) -> None:
        """Validate transport shape; current target is checked by the Host tool.

        The model may follow an explicit EID, resource or pixel from a later user
        message, even when this task began from a different UI selection.
        """

        del tool
        if not isinstance(arguments, dict):
            raise PolicyError("RenderDoc tool arguments must be an object")

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "ToolCallScope":
        if not value:
            return cls()
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PolicyError("Bread tool scope is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise PolicyError("Bread tool scope must be a JSON object")
        allowed = {item.name for item in fields(cls)}
        if set(decoded) - allowed:
            raise PolicyError("Bread tool scope contains unknown fields")
        try:
            scope = cls(**decoded)
        except TypeError as exc:
            raise PolicyError("Bread tool scope has invalid field types") from exc
        return scope


def selected_shader_target_for_intent(
    intent: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a UI-selected Shader as a conversation hint, not a task gate."""
    if not isinstance(intent, Mapping) or intent.get("kind") != "reverse-edit-selected-shader":
        return {}
    scope = ToolCallScope.from_request(dict(context or {}), dict(intent))
    target: dict[str, Any] = {
        "eventId": scope.event_id,
        "stage": scope.stage,
        "resourceId": str(scope.resource_id) if scope.resource_id is not None else "",
        "entryPoint": scope.entry_point,
        "rawSha256": scope.raw_sha256,
        "captureSessionId": scope.capture_session_id,
    }
    if scope.capture_id:
        target["captureId"] = scope.capture_id
    return target


__all__ = ["ToolCallScope", "selected_shader_target_for_intent"]
