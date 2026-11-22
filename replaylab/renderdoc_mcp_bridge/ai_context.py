"""Deterministic RenderDoc UI context protocol.

This module deliberately has no RenderDoc, Qt, MCP, AI SDK, network, or secret
dependencies. The built-in qrenderdoc integration supplies observed facts and
passes a frozen context directly to its first-party AgentHost. Keeping this
boundary small makes it possible to audit exactly what was selected when a
user clicked an AI command.

The implementation is compatible with the Python 3.6 runtime embedded by the
supported RenderDoc build.
"""

from __future__ import absolute_import

import copy
import hashlib
import json
import re


CONTEXT_SCHEMA = "renderdoc-ultra-current-context/v1"
SCHEMA_VERSION = 1

SHADER_STAGE_ORDER = (
    "vertex",
    "hull",
    "domain",
    "geometry",
    "fragment",
    "compute",
    "task",
    "mesh",
    "raygen",
    "intersection",
    "anyhit",
    "closesthit",
    "miss",
    "callable",
)

_STAGE_ALIASES = {
    "vs": "vertex",
    "hs": "hull",
    "tess_ctrl": "hull",
    "ds": "domain",
    "tess_eval": "domain",
    "gs": "geometry",
    "ps": "fragment",
    "pixel": "fragment",
    "fs": "fragment",
    "cs": "compute",
    "as": "task",
    "amplification": "task",
    "ms": "mesh",
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")

_CLASSIC_GRAPHICS_STAGES = (
    "vertex",
    "hull",
    "domain",
    "geometry",
    "fragment",
)
_MESH_GRAPHICS_STAGES = ("task", "mesh", "fragment")
_COMPUTE_STAGES = ("compute",)
_RAY_TRACING_STAGES = (
    "raygen",
    "intersection",
    "anyhit",
    "closesthit",
    "miss",
    "callable",
)
_DRAW_ACTION_MASK = 0x0002
_DISPATCH_ACTION_MASK = 0x0004
_MESH_DISPATCH_ACTION_MASK = 0x0008
_RAY_DISPATCH_ACTION_MASK = 0x4000


class ContextProtocolError(ValueError):
    """Raised when observed UI facts do not satisfy the public protocol."""


def _text(value, label, maximum=4096):
    if value is None:
        return ""
    value = str(value)
    if len(value) > maximum:
        raise ContextProtocolError("%s exceeds %d characters" % (label, maximum))
    return value


def _integer(value, label, minimum=0, maximum=0xFFFFFFFFFFFFFFFF):
    try:
        value = int(value)
    except Exception:
        raise ContextProtocolError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise ContextProtocolError(
            "%s must be between %d and %d" % (label, minimum, maximum)
        )
    return value


def _resource_id(value, label, minimum=0):
    """Return an exact decimal ResourceId safe for every JSON consumer.

    Qt 5 stores JSON numbers as IEEE-754 doubles, which cannot represent all
    RenderDoc 64-bit ResourceIds.  ResourceIds are opaque identities rather
    than quantities, so the wire protocol carries them as decimal strings.
    """
    return str(_integer(value, label, minimum))


def normalise_stage(value):
    value = _text(value, "shader stage", 64).strip().lower()
    value = value.replace("shaderstage.", "")
    value = _STAGE_ALIASES.get(value, value)
    if value and value not in SHADER_STAGE_ORDER:
        raise ContextProtocolError("unsupported shader stage: %s" % value)
    return value


def _action_flag_names(action):
    if not isinstance(action, dict):
        return set()
    flags = _text(action.get("flags", ""), "action flags", 1024)
    return set(
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", flags)
    )


def _action_flag_mask(action):
    if not isinstance(action, dict):
        return 0
    value = action.get("flags", "")
    if isinstance(value, bool):
        return 0
    try:
        if isinstance(value, int):
            return value
        text = str(value).strip().lower()
        if re.match(r"^(?:0x[0-9a-f]+|[0-9]+)$", text):
            return int(text, 0)
    except Exception:
        pass
    return 0


def compatible_shader_stages(action):
    """Return only stages that can execute for the exact current action.

    RenderDoc can retain independent graphics and compute pipeline bindings at
    the same replay point.  ``PipelineState.GetShader`` therefore answers which
    resources are resident in those state buckets, not by itself which shaders
    execute for a Draw/Dispatch/MeshDispatch/DispatchRay action.  Action flags
    are the fixed-program discriminator.  Unknown and non-work actions fail
    closed so stale state cannot become a positive "current Shader" candidate.
    """

    if not isinstance(action, dict):
        return ()
    # The bridge calls this helper on the exact result of
    # ``_current_action_context(current_event)`` before canonicalisation adds
    # ``matchesCurrentEvent``. Protocol callers do carry that field and must
    # fail closed when it disproves the current-EID relationship.
    if (
        "matchesCurrentEvent" in action
        and action.get("matchesCurrentEvent") is not True
    ):
        return ()
    names = _action_flag_names(action)
    mask = _action_flag_mask(action)
    families = (
        (
            bool("dispatchray" in names or mask & _RAY_DISPATCH_ACTION_MASK),
            _RAY_TRACING_STAGES,
        ),
        (
            bool("meshdispatch" in names or mask & _MESH_DISPATCH_ACTION_MASK),
            _MESH_GRAPHICS_STAGES,
        ),
        (
            bool("dispatch" in names or mask & _DISPATCH_ACTION_MASK),
            _COMPUTE_STAGES,
        ),
        (
            bool("drawcall" in names or mask & _DRAW_ACTION_MASK),
            _CLASSIC_GRAPHICS_STAGES,
        ),
    )
    matched = tuple(stages for present, stages in families if present)
    # Mutually exclusive action families describe different executing shader
    # domains. A malformed combined flag cannot safely select one by priority;
    # fail closed so no stale graphics/compute bucket becomes a UI choice.
    return matched[0] if len(matched) == 1 else ()


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_hash(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalise_capture(value):
    value = value if isinstance(value, dict) else {}
    loaded = bool(value.get("loaded", False))
    session_id = _text(
        value.get("captureSessionId", ""), "capture session ID", 64
    ).lower()
    if loaded and not _SESSION_RE.match(session_id):
        raise ContextProtocolError(
            "a loaded capture requires a 32-character capture session ID"
        )
    capture_id = _text(value.get("captureId", ""), "capture ID", 128).lower()
    if capture_id and not _SHA256_RE.match(capture_id):
        raise ContextProtocolError("capture ID must be an SHA-256 digest when present")
    return {
        "loaded": loaded,
        "captureSessionId": session_id,
        "captureId": capture_id,
        "path": _text(value.get("path", ""), "capture path", 32768),
        "api": _text(value.get("api", ""), "capture API", 128),
        "local": bool(value.get("local", False)),
    }


def _normalise_events(value, loaded):
    value = value if isinstance(value, dict) else {}
    current = _integer(value.get("currentEventId", 0), "current event ID")
    selected = _integer(value.get("selectedEventId", 0), "selected event ID")
    if not loaded and (current or selected):
        raise ContextProtocolError("an unloaded capture cannot have selected events")
    return {
        "currentEventId": current,
        "selectedEventId": selected,
        "selectionDiffersFromReplay": bool(selected != current),
    }


def _normalise_action_item(value, prefix):
    if not isinstance(value, dict):
        raise ContextProtocolError("%s must be an object" % prefix)
    return {
        "eventId": _integer(value.get("eventId", 0), prefix + " event ID"),
        "actionId": _integer(value.get("actionId", 0), prefix + " action ID"),
        "name": _text(value.get("name", ""), prefix + " name", 4096),
        "flags": _text(value.get("flags", ""), prefix + " flags", 1024),
    }


def _normalise_action(value, events):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContextProtocolError("action must be an object")
    action = _normalise_action_item(value, "action")
    marker_path = value.get("markerPath", [])
    if not isinstance(marker_path, list) or len(marker_path) > 128:
        raise ContextProtocolError("marker path must contain at most 128 items")
    action["markerPath"] = [
        _normalise_action_item(item, "marker path item") for item in marker_path
    ]
    action["matchesCurrentEvent"] = bool(
        action["eventId"] == events["currentEventId"]
    )
    return action


def _normalise_shader(value):
    if not isinstance(value, dict):
        raise ContextProtocolError("shader entries must be objects")
    stage = normalise_stage(value.get("stage", ""))
    if not stage:
        raise ContextProtocolError("shader stage is required")
    resource_id = _resource_id(
        value.get("resourceId", value.get("shaderResourceId", 0)),
        "shader resource ID",
        1,
    )
    raw_hash = _text(value.get("rawSha256", ""), "shader raw SHA-256", 64).lower()
    if raw_hash and not _SHA256_RE.match(raw_hash):
        raise ContextProtocolError("shader raw SHA-256 is invalid")
    result = {
        "stage": stage,
        "resourceId": resource_id,
        "entryPoint": _text(value.get("entryPoint", ""), "shader entry point", 4096),
        "rawSha256": raw_hash,
        "exactRawBytesAvailable": bool(raw_hash),
        "displayName": _text(value.get("displayName", ""), "shader display name", 4096),
    }
    original = value.get("capturedOriginalIdentity")
    if original is not None:
        if not isinstance(original, dict):
            raise ContextProtocolError("captured original Shader identity must be an object")
        original_id = _resource_id(original.get("resourceId"), "captured Shader resource ID", 1)
        original_hash = _text(original.get("rawSha256", ""), "captured Shader SHA-256", 64).lower()
        session = _text(original.get("captureSessionId", ""), "captured Shader session", 32)
        if (original_id != resource_id or normalise_stage(original.get("stage", "")) != stage
                or original.get("entryPoint") != result["entryPoint"]
                or not _SHA256_RE.match(original_hash) or not re.match(r"^[0-9a-f]{32}$", session)):
            raise ContextProtocolError("captured original Shader identity differs from its binding")
        result["capturedOriginalIdentity"] = {
            "resourceId": original_id, "stage": stage, "entryPoint": result["entryPoint"],
            "rawSha256": original_hash, "captureSessionId": session}
    return result


def _normalise_shaders(values, focus_stage, focus_source, action):
    if values is None:
        values = []
    if not isinstance(values, list) or len(values) > len(SHADER_STAGE_ORDER):
        raise ContextProtocolError("bound shaders must be a bounded list")
    compatible = compatible_shader_stages(action)
    compatible_set = set(compatible)
    shaders = [
        shader
        for shader in (_normalise_shader(value) for value in values)
        if shader["stage"] in compatible_set
    ]
    shaders.sort(
        key=lambda shader: (
            SHADER_STAGE_ORDER.index(shader["stage"]),
            shader["resourceId"],
            shader["entryPoint"],
            shader["rawSha256"],
        )
    )
    seen_stages = set()
    for shader in shaders:
        if shader["stage"] in seen_stages:
            raise ContextProtocolError(
                "more than one bound shader was reported for stage %s" % shader["stage"]
            )
        seen_stages.add(shader["stage"])

    focus_stage = normalise_stage(focus_stage)
    focus_source = _text(focus_source, "shader focus source", 64).strip().lower()
    if focus_source not in (
            "", "explicit-ui-button", "explicit-native-task",
            "deterministic-default"):
        raise ContextProtocolError("unsupported shader focus source")
    focused = None
    if focus_stage:
        for shader in shaders:
            if shader["stage"] == focus_stage:
                focused = copy.deepcopy(shader)
                break
    if focus_stage and focused is None:
        focus_source = ""
    return {
        "bound": shaders,
        "compatibleStages": list(compatible),
        "compatibilitySource": "current-action-flags",
        "focused": focused,
        "focusSource": focus_source if focused is not None else "none",
        "focusIsExplicit": bool(
            focused is not None and
            focus_source in ("explicit-ui-button", "explicit-native-task")),
    }


def _normalise_texture(value):
    if value is None:
        return {
            "available": False,
            "resourceId": "0",
            "name": "",
            "subresource": None,
            "pickedPixel": None,
        }
    if not isinstance(value, dict):
        raise ContextProtocolError("texture context must be an object")
    resource_id = _resource_id(value.get("resourceId", 0), "texture resource ID")
    if resource_id == "0":
        return {
            "available": False,
            "resourceId": "0",
            "name": "",
            "subresource": None,
            "pickedPixel": None,
        }
    sub = value.get("subresource", {})
    if not isinstance(sub, dict):
        raise ContextProtocolError("texture subresource must be an object")
    pixel = value.get("pickedPixel")
    normalised_pixel = None
    if pixel is not None:
        if not isinstance(pixel, dict):
            raise ContextProtocolError("picked pixel must be an object")
        x = _integer(pixel.get("x", -1), "picked pixel x", -1, 0x7FFFFFFF)
        y = _integer(pixel.get("y", -1), "picked pixel y", -1, 0x7FFFFFFF)
        if x >= 0 and y >= 0:
            normalised_pixel = {"x": x, "y": y}
    return {
        "available": True,
        "resourceId": resource_id,
        "name": _text(value.get("name", ""), "texture name", 4096),
        "subresource": {
            "mip": _integer(sub.get("mip", 0), "texture mip"),
            "slice": _integer(sub.get("slice", 0), "texture slice"),
            "sample": _integer(sub.get("sample", 0), "texture sample"),
        },
        "pickedPixel": normalised_pixel,
        "flipY": value.get("flipY") is True,
    }


def build_context(facts):
    """Validate observed facts and return a canonical, content-addressed snapshot."""
    if not isinstance(facts, dict):
        raise ContextProtocolError("context facts must be an object")
    capture = _normalise_capture(facts.get("capture"))
    events = _normalise_events(facts.get("events"), capture["loaded"])
    action = _normalise_action(facts.get("action"), events)
    shaders = _normalise_shaders(
        facts.get("boundShaders", []),
        facts.get("focusStage", ""),
        facts.get("focusSource", ""),
        action,
    )
    texture = _normalise_texture(facts.get("texture"))
    warnings = facts.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 64:
        raise ContextProtocolError("warnings must contain at most 64 strings")
    warnings = sorted(set(_text(item, "context warning", 4096) for item in warnings))

    payload = {
        "schema": CONTEXT_SCHEMA,
        "schemaVersion": SCHEMA_VERSION,
        "contextGeneration": _integer(
            facts.get("contextGeneration", 0), "context generation"
        ),
        "capture": capture,
        "events": events,
        "action": action,
        "shaders": shaders,
        "texture": texture,
        "focusedResource": (
            {
                "kind": "texture",
                "resourceId": texture["resourceId"],
                "name": texture["name"],
            }
            if texture["available"]
            else None
        ),
        "warnings": warnings,
        "capabilities": {
            "canExplainCurrentEvent": bool(
                capture["loaded"] and events["currentEventId"] > 0
            ),
            "canReverseEditFocusedShader": bool(
                shaders["focused"] is not None
                and shaders["focused"]["exactRawBytesAvailable"]
            ),
            "canInspectPickedPixel": bool(
                texture["available"] and texture["pickedPixel"] is not None
            ),
        },
    }
    payload["snapshotId"] = _content_hash(payload)
    return payload


def validate_context(snapshot):
    """Reject mutated snapshots before they are attached to a UI intent."""
    if not isinstance(snapshot, dict):
        raise ContextProtocolError("context snapshot must be an object")
    supplied = _text(snapshot.get("snapshotId", ""), "context snapshot ID", 64).lower()
    if not _SHA256_RE.match(supplied):
        raise ContextProtocolError("context snapshot ID is invalid")
    body = copy.deepcopy(snapshot)
    body.pop("snapshotId", None)
    if body.get("schema") != CONTEXT_SCHEMA or body.get("schemaVersion") != SCHEMA_VERSION:
        raise ContextProtocolError("unsupported context schema")
    if _content_hash(body) != supplied:
        raise ContextProtocolError("context snapshot content hash does not match")
    return copy.deepcopy(snapshot)


__all__ = [
    "CONTEXT_SCHEMA",
    "ContextProtocolError",
    "build_context",
    "compatible_shader_stages",
    "normalise_stage",
    "validate_context",
]
