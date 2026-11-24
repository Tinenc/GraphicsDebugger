"""Deterministic RenderDoc action/pass classification and project knowledge.

The module deliberately has no RenderDoc dependency.  The bridge/host supplies
captured action, pipeline and shader-catalog facts; this module turns those
facts into a foldable tree.  Semantic suggestions from a model are carried in
an isolated ``AIProvisional`` overlay and never change the fixed result.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shader_workspace.io import atomic_write_json

CLASSIFICATION_SCHEMA = "replaylab-pass-classification/v1"
CLASSIFICATION_VERSION = 1
KNOWLEDGE_SCHEMA = "replaylab-project-pass-knowledge/v1"
KNOWLEDGE_VERSION = 1

MAX_ACTIONS = 100_000
MAX_PROPOSALS = 100_000
MAX_RESOURCE_FLOW_EDGES = 20_000
MAX_ACTION_IO_RESOURCES = 256
MAX_DRAW_STATE_STAGES = 16
MAX_SAMPLER_BINDINGS_PER_STAGE = 64
DEFAULT_FOLDED_GROUP_SIZE = 32
MAX_TREE_GROUP_LABEL = 512
MAX_PROJECT_BYTES = 64 * 1024 * 1024
MAX_KNOWLEDGE_BYTES = 16 * 1024 * 1024
MAX_TEXT = 4096

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

CATEGORY_ORDER = (
    "Depth",
    "Shadow",
    "BasePass",
    "GBuffer",
    "Lighting",
    "Transparent",
    "Post",
    "UI",
    "Compute",
    "Utility",
    "Unknown",
)
CATEGORIES = frozenset(CATEGORY_ORDER)


# Stable values from RenderDoc's ActionFlags enum.  Symbolic flag names are
# accepted too, so this stays testable without importing renderdoc.dll.
FLAG_CLEAR = 0x0001
FLAG_DRAW = 0x0002
FLAG_DISPATCH = 0x0004
FLAG_MESH_DISPATCH = 0x0008
FLAG_CMD_LIST = 0x0010
FLAG_SET_MARKER = 0x0020
FLAG_PUSH_MARKER = 0x0040
FLAG_POP_MARKER = 0x0080
FLAG_PRESENT = 0x0100
FLAG_MULTI_ACTION = 0x0200
FLAG_COPY = 0x0400
FLAG_RESOLVE = 0x0800
FLAG_GEN_MIPS = 0x1000
FLAG_PASS_BOUNDARY = 0x2000
FLAG_DISPATCH_RAY = 0x4000
FLAG_BUILD_ACCELERATION_STRUCTURE = 0x8000
FLAG_INDIRECT = 0x080000
FLAG_INSTANCED = 0x020000

GRAPHICS_MASK = FLAG_DRAW | FLAG_MESH_DISPATCH
COMPUTE_MASK = FLAG_DISPATCH | FLAG_DISPATCH_RAY
UTILITY_MASK = (
    FLAG_CLEAR
    | FLAG_COPY
    | FLAG_RESOLVE
    | FLAG_GEN_MIPS
    | FLAG_PRESENT
    | FLAG_BUILD_ACCELERATION_STRUCTURE
)
MARKER_MASK = (
    FLAG_CMD_LIST
    | FLAG_SET_MARKER
    | FLAG_PUSH_MARKER
    | FLAG_POP_MARKER
    | FLAG_PASS_BOUNDARY
)


SIGNALS: dict[str, tuple[str, ...]] = {
    "Depth": (
        "depth prepass",
        "depthprepass",
        "pre depth",
        "predepth",
        "z prepass",
        "zprepass",
        "depth only",
        "depthonly",
        "early z",
        "earlyz",
        "hi z",
        "hiz",
    ),
    "Shadow": (
        "shadow",
        "shadowmap",
        "shadow map",
        "shadowdepth",
        "shadow depth",
        "csm",
        "vsm",
        "virtual shadow",
        "sun shadow",
    ),
    "BasePass": (
        "base pass",
        "basepass",
        "forward base",
        "forwardbase",
        "forward opaque",
        "forwardopaque",
        "opaque",
    ),
    "GBuffer": (
        "gbuffer",
        "g buffer",
        "deferred base",
        "deferredbase",
        "deferred geometry",
        "deferredgeometry",
    ),
    "Lighting": (
        "lighting",
        "light pass",
        "lightpass",
        "deferred light",
        "deferredlight",
        "light accumulation",
        "lightaccumulation",
        "ssao",
        "ambient occlusion",
    ),
    "Transparent": (
        "transparent",
        "translucent",
        "translucency",
        "alpha blend",
        "alphablend",
        "particle",
        "water",
    ),
    "Post": (
        "post process",
        "postprocess",
        "post fx",
        "postfx",
        "tonemap",
        "tone map",
        "bloom",
        "taa",
        "fxaa",
        "upscale",
        "upsample",
        "depth of field",
        "dof",
        "motion blur",
        "motionblur",
        "color grade",
        "colorgrade",
    ),
    "UI": (
        "ui",
        "hud",
        "slate",
        "imgui",
        "overlay",
        "canvas",
        "user interface",
    ),
}


class PassClassificationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PassClassificationError("value is not canonical JSON") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bounded_text(value: Any, field: str, *, allow_empty: bool = True) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        value = str(value)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > MAX_TEXT or "\x00" in result:
        raise PassClassificationError(f"{field} is invalid")
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _sha(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if SHA256_RE.fullmatch(text) else ""


def _marker_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:128]:
        if isinstance(item, Mapping):
            text = item.get("name", item.get("label", ""))
        else:
            text = item
        name = _bounded_text(text, "markerPath item")
        if name:
            result.append(name)
    return result


def _marker_event_path(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:128]:
        if not isinstance(item, Mapping):
            continue
        event_id = _safe_int(item.get("eventId", item.get("event_id")), 0)
        name = _bounded_text(item.get("name", item.get("label", "")), "marker event name")
        if event_id > 0 or name:
            result.append({"eventId": max(0, event_id), "name": name})
    return result


def _normal_search_text(value: str) -> tuple[str, str, frozenset[str]]:
    # Preserve word boundaries for short tokens such as UI/CSM, while also
    # supporting common concatenated marker names such as ShadowDepths.
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    normal = re.sub(r"[^a-z0-9]+", " ", spaced.lower()).strip()
    words = frozenset(normal.split())
    return normal, "".join(normal.split()), words


def _match_signals(value: str) -> dict[str, list[str]]:
    normal, compact, words = _normal_search_text(value)
    matched: dict[str, list[str]] = {}
    if not normal:
        return matched
    for category, tokens in SIGNALS.items():
        hits: list[str] = []
        for token in tokens:
            token_normal, token_compact, token_words = _normal_search_text(token)
            if not token_normal:
                continue
            if len(token_words) == 1 and len(token_compact) <= 3:
                found = token_compact in words
            else:
                found = token_normal in normal or token_compact in compact
            if found and token not in hits:
                hits.append(token)
        if hits:
            matched[category] = hits
    return matched


def _flag_names(action: Mapping[str, Any]) -> frozenset[str]:
    raw = action.get("flagNames", action.get("flagsText", []))
    if isinstance(raw, str):
        raw = re.split(r"[,|+ ]+", raw)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(re.sub(r"[^a-z]", "", str(item).lower()) for item in raw)


def _has_named_flag(names: frozenset[str], *values: str) -> bool:
    wanted = {re.sub(r"[^a-z]", "", value.lower()) for value in values}
    return bool(names & wanted)


def _action_kind(action: Mapping[str, Any]) -> str:
    flags = _safe_int(action.get("flags"), 0)
    names = _flag_names(action)
    if flags & GRAPHICS_MASK or _has_named_flag(names, "Drawcall", "MeshDispatch"):
        return "graphics"
    if flags & COMPUTE_MASK or _has_named_flag(names, "Dispatch", "DispatchRay"):
        return "compute"
    if flags & UTILITY_MASK or _has_named_flag(
        names, "Clear", "Copy", "Resolve", "GenMips", "Present", "BuildAccStruct"
    ):
        return "utility"
    if flags & FLAG_MULTI_ACTION or _has_named_flag(names, "MultiAction"):
        return "container"
    if flags & MARKER_MASK or _has_named_flag(
        names, "CmdList", "SetMarker", "PushMarker", "PopMarker", "PassBoundary"
    ):
        return "marker"
    return "unknown"


def _is_indirect(action: Mapping[str, Any]) -> bool:
    flags = _safe_int(action.get("flags"), 0)
    if flags & FLAG_INDIRECT or _has_named_flag(_flag_names(action), "Indirect"):
        return True
    text = " ".join(
        [str(action.get("name", "")), *_marker_names(action.get("markerPath"))]
    )
    _, compact, _ = _normal_search_text(text)
    return (
        "executeindirect" in compact
        or "drawindirect" in compact
        or "dispatchindirect" in compact
    )


def _indirect_container_name(action: Mapping[str, Any]) -> str:
    candidates = [*_marker_names(action.get("markerPath")), str(action.get("name", ""))]
    for candidate in reversed(candidates):
        normal, compact, _ = _normal_search_text(candidate)
        if "executeindirect" in compact or "multi draw" in normal:
            return _bounded_text(candidate, "indirect container")
    return "ExecuteIndirect" if _is_indirect(action) else ""


def _normalise_actions(raw: Any) -> tuple[list[dict[str, Any]], int]:
    if isinstance(raw, Mapping):
        raw = raw.get("actions", [])
    if not isinstance(raw, (list, tuple)):
        raise PassClassificationError("actions must be a list or get_draw_calls result")

    flattened: list[dict[str, Any]] = []

    def walk(
        values: Sequence[Any],
        inherited_markers: Sequence[str] = (),
        inherited_marker_events: Sequence[Mapping[str, Any]] = (),
        parent_event_id: int | None = None,
    ) -> None:
        for raw_action in values:
            if not isinstance(raw_action, Mapping):
                raise PassClassificationError("every action must be an object")
            if len(flattened) >= MAX_ACTIONS:
                raise PassClassificationError("action count exceeds the safety limit")
            action = dict(raw_action)
            event_id = _safe_int(action.get("eventId", action.get("event_id")), -1)
            if event_id < 0:
                raise PassClassificationError("action eventId must be non-negative")
            name = _bounded_text(action.get("name", ""), "action.name")
            children = action.get("children", [])
            if children is None:
                children = []
            if not isinstance(children, (list, tuple)):
                raise PassClassificationError("action.children must be a list")
            marker_path = _marker_names(action.get("markerPath")) or list(
                inherited_markers
            )
            marker_events = _marker_event_path(action.get("markerEventPath")) or [
                dict(item) for item in inherited_marker_events
            ]
            explicit_parent = _safe_int(
                action.get("parentEventId", action.get("parent_event_id")), 0
            )
            resolved_parent = (
                parent_event_id if parent_event_id is not None else explicit_parent
            )
            action.update(
                {
                    "eventId": event_id,
                    "name": name or f"Action {event_id}",
                    "markerPath": marker_path,
                    "markerEventPath": marker_events,
                    "_hasChildren": bool(children),
                    "_parentEventId": max(0, _safe_int(resolved_parent, 0)),
                    "_executionIndex": max(
                        0,
                        _safe_int(action.get("executionIndex"), len(flattened)),
                    ),
                }
            )
            action.pop("children", None)
            flattened.append(action)
            if children:
                child_markers = marker_path
                if not child_markers or child_markers[-1] != action["name"]:
                    child_markers = [*child_markers, action["name"]]
                child_marker_events = marker_events
                marker_identity = {"eventId": event_id, "name": action["name"]}
                if not child_marker_events or child_marker_events[-1] != marker_identity:
                    child_marker_events = [*child_marker_events, marker_identity]
                walk(children, child_markers, child_marker_events, event_id)

    walk(raw)

    # RenderDoc normally gives unique EIDs.  Some adapters provide both a
    # nested tree and a flattened list; collapse those duplicates and prefer
    # the executable child over a marker/container copy.
    by_event: dict[int, dict[str, Any]] = {}
    duplicates = 0
    rank = {
        "graphics": 5,
        "compute": 5,
        "utility": 4,
        "container": 3,
        "marker": 2,
        "unknown": 1,
    }
    zero_event: list[dict[str, Any]] = []
    for action in flattened:
        event_id = action["eventId"]
        if event_id == 0:
            zero_event.append(action)
            continue
        previous = by_event.get(event_id)
        if previous is None:
            by_event[event_id] = action
            continue
        duplicates += 1
        current_rank = rank[_action_kind(action)]
        previous_rank = rank[_action_kind(previous)]
        selected, other = (
            (action, previous) if current_rank > previous_rank else (previous, action)
        )
        if not selected.get("markerPath") and other.get("markerPath"):
            selected["markerPath"] = list(other["markerPath"])
        if not selected.get("markerEventPath") and other.get("markerEventPath"):
            selected["markerEventPath"] = [
                dict(item) for item in other["markerEventPath"]
            ]
        if not _safe_int(selected.get("_parentEventId"), 0):
            selected["_parentEventId"] = _safe_int(
                other.get("_parentEventId"), 0
            )
        selected["_executionIndex"] = min(
            _safe_int(selected.get("_executionIndex"), event_id),
            _safe_int(other.get("_executionIndex"), event_id),
        )
        selected["_deduplicatedCopies"] = (
            _safe_int(selected.get("_deduplicatedCopies"), 0) + 1
        )
        by_event[event_id] = selected
    result = sorted(
        [*by_event.values(), *zero_event],
        key=lambda item: (
            _safe_int(item.get("_executionIndex"), 0),
            item["eventId"],
        ),
    )
    return result, duplicates


def _event_index(value: Any) -> dict[int, Mapping[str, Any]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        if "eventId" in value or "event_id" in value:
            event_id = _safe_int(value.get("eventId", value.get("event_id")), -1)
            return {event_id: value} if event_id >= 0 else {}
        result: dict[int, Mapping[str, Any]] = {}
        for key, item in value.items():
            if isinstance(item, Mapping):
                event_id = _safe_int(item.get("eventId", item.get("event_id", key)), -1)
                if event_id >= 0:
                    result[event_id] = item
        return result
    if isinstance(value, (list, tuple)):
        result = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            event_id = _safe_int(item.get("eventId", item.get("event_id")), -1)
            if event_id >= 0:
                result[event_id] = item
        return result
    raise PassClassificationError("pipeline states must be event-indexed objects")


def _shader_display_name(value: Mapping[str, Any]) -> str:
    for field in ("displayName", "name", "debugName", "entryPoint", "entry"):
        text = _bounded_text(value.get(field, ""), "shader name")
        if text:
            return text
    alias = value.get("alias")
    if isinstance(alias, Mapping):
        for field in ("displayName", "alias", "canonicalPath"):
            text = _bounded_text(alias.get(field, ""), "shader alias")
            if text:
                return text
    return ""


def _catalog_index(
    catalog: Any,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Mapping[str, Any]]]:
    if catalog is None:
        return {}, {}
    if not isinstance(catalog, Mapping):
        raise PassClassificationError("shader_catalog must be an object")
    shaders = catalog.get("shaders", [])
    uses = catalog.get("uses", [])
    if not isinstance(shaders, list) or not isinstance(uses, list):
        raise PassClassificationError("shader_catalog shaders/uses must be lists")
    lookup: dict[str, Mapping[str, Any]] = {}
    resource_lookup: dict[tuple[int, str], Mapping[str, Any]] = {}
    for shader in shaders:
        if not isinstance(shader, Mapping):
            continue
        key = str(shader.get("shaderKey", "") or "")
        if key:
            lookup[key] = shader
        resource = _safe_int(
            shader.get("shaderResourceId", shader.get("resourceId")), 0
        )
        stage = str(shader.get("stage", "") or "").strip().lower()
        if resource and stage:
            resource_lookup[(resource, stage)] = shader
    by_event: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for use in uses:
        if not isinstance(use, Mapping):
            continue
        event_id = _safe_int(use.get("eventId"), -1)
        if event_id < 0:
            continue
        merged = dict(use)
        shader = lookup.get(str(use.get("shaderKey", "") or ""))
        if shader is None:
            resource = _safe_int(use.get("resourceId", use.get("shaderResourceId")), 0)
            stage = str(use.get("stage", "") or "").strip().lower()
            shader = resource_lookup.get((resource, stage))
        if shader is not None:
            for key in (
                "rawSha256",
                "codeSha256",
                "interfaceHash",
                "layoutHash",
                "displayName",
                "alias",
                "entryPoint",
                "entry",
            ):
                if not merged.get(key) and shader.get(key) is not None:
                    merged[key] = shader.get(key)
        by_event[event_id].append(merged)
    return dict(by_event), lookup


def _resource_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:MAX_ACTION_IO_RESOURCES]:
        if isinstance(item, Mapping):
            resource = _safe_int(
                item.get("resourceId", item.get("resource", item.get("id"))), 0
            )
            entry = {
                "resourceId": resource,
                "name": _bounded_text(
                    item.get("displayName", item.get("name", "")), "resource name"
                ),
                "format": _bounded_text(item.get("format", ""), "resource format"),
            }
            for field in ("access", "stage", "role", "certainty"):
                text = _bounded_text(item.get(field, ""), f"resource {field}")
                if text:
                    entry[field] = text
            for field in (
                "sampleCount",
                "slot",
                "byteOffset",
                "byteSize",
                "firstMip",
                "numMips",
                "firstSlice",
                "numSlices",
            ):
                number = _safe_int(item.get(field), -1)
                if number >= 0:
                    entry[field] = number
            if "byteStride" in item:
                stride = item.get("byteStride")
                if stride is None:
                    entry["byteStride"] = None
                else:
                    number = _safe_int(stride, -1)
                    if 0 <= number <= 0xFFFFFFFFFFFFFFFF:
                        entry["byteStride"] = number
            roles = item.get("roles")
            if isinstance(roles, (list, tuple)):
                clean_roles: list[str] = []
                for role in roles[:16]:
                    text = _bounded_text(role, "resource role")
                    if text and text not in clean_roles:
                        clean_roles.append(text)
                if clean_roles:
                    entry["roles"] = clean_roles
            subresource = item.get("subresource")
            if isinstance(subresource, Mapping):
                clean_subresource = {}
                for field in ("mip", "slice", "sample", "plane"):
                    number = _safe_int(subresource.get(field), -1)
                    if number >= 0:
                        clean_subresource[field] = number
                if clean_subresource:
                    entry["subresource"] = clean_subresource
        else:
            entry = {"resourceId": _safe_int(item, 0), "name": "", "format": ""}
        if entry["resourceId"] or entry["name"] or entry["format"]:
            result.append(entry)
    return result


def _unique_resources(
    *values: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for collection in values:
        for raw in collection:
            item = dict(raw)
            key = (
                _safe_int(item.get("resourceId"), 0),
                str(item.get("access", "")),
                str(item.get("stage", "")),
                *(
                    _safe_int(item.get(field), -1)
                    for field in (
                        "byteOffset", "byteSize", "firstMip", "numMips",
                        "firstSlice", "numSlices",
                    )
                ),
                json.dumps(
                    item.get("subresource", {}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result


def _resource_access(
    values: Sequence[Mapping[str, Any]], access: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        item = dict(value)
        item.setdefault("access", access)
        result.append(item)
    return result


def _utility_destination_outputs(
    copy_destinations: Sequence[Mapping[str, Any]],
    depth_destinations: Sequence[Mapping[str, Any]],
    action_outputs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Coalesce utility aliases without losing their fixed-program roles.

    RenderDoc commonly exposes a Copy/Resolve destination both through the
    dedicated ``copyDestination`` field and through ``action.outputs``.  Those
    fields describe one physical write, not two independent writers. Bound
    RTV/DSV fields can also be stale state from an earlier draw. When an exact
    destination is known, retain only aliases of that destination.
    """

    result: list[dict[str, Any]] = []

    def add(raw: Mapping[str, Any], access: str) -> None:
        item = dict(raw)
        item["access"] = access
        item_range = _resource_range(item)
        resource_id = _safe_int(item.get("resourceId"), 0)
        matches = [
            existing
            for existing in result
            if _safe_int(existing.get("resourceId"), 0) == resource_id
            and (
                _resource_range(existing) == item_range
                or not _resource_range(existing)
                or not item_range
            )
        ]
        if matches:
            for existing in matches:
                roles = list(existing.get("roles", []))
                if access not in roles:
                    roles.append(access)
                existing["roles"] = roles
                for field in ("name", "format"):
                    if not existing.get(field) and item.get(field):
                        existing[field] = item[field]
            return
        item["roles"] = [access]
        result.append(item)

    for destination in copy_destinations:
        add(destination, "copy-destination")
    exact_ids = {_safe_int(row.get("resourceId"), 0) for row in copy_destinations} - {0}
    for destination in depth_destinations:
        if not exact_ids or _safe_int(destination.get("resourceId"), 0) in exact_ids:
            add(destination, "action-depth-output")
    for destination in action_outputs:
        if not exact_ids or _safe_int(destination.get("resourceId"), 0) in exact_ids:
            add(destination, "action-output")
    return result


def _bounded_resource_direction(
    values: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    unique = _unique_resources(values)
    if len(unique) <= MAX_ACTION_IO_RESOURCES:
        return unique, 0
    return unique[:MAX_ACTION_IO_RESOURCES], len(unique) - MAX_ACTION_IO_RESOURCES


def _mapping_path(value: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for component in path:
            if not isinstance(current, Mapping) or component not in current:
                current = None
                break
            current = current[component]
        if current is not None:
            return current
    return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normal = value.strip().lower()
        if normal in ("true", "enabled", "yes", "1"):
            return True
        if normal in ("false", "disabled", "no", "0"):
            return False
    return None


def _pipeline_bool(pipeline: Mapping[str, Any], name: str) -> bool | None:
    paths: dict[str, tuple[tuple[str, ...], ...]] = {
        "blendEnabled": (
            ("blendEnabled",),
            ("blend", "enabled"),
            ("blendState", "enabled"),
        ),
        "depthTestEnabled": (
            ("depthTestEnabled",),
            ("depth", "testEnabled"),
            ("depthState", "testEnabled"),
            ("depthStencilState", "depthEnable"),
        ),
        "depthWriteEnabled": (
            ("depthWriteEnabled",),
            ("depth", "writeEnabled"),
            ("depthState", "writeEnabled"),
            ("depthStencilState", "depthWrite"),
        ),
        "depthReadOnly": (("depthReadOnly",),),
        "frontCCW": (("frontCCW",),),
        "stencilTestEnabled": (("stencilTestEnabled",),),
        "stencilReadOnly": (("stencilReadOnly",),),
        "stencilWritePossible": (("stencilWritePossible",),),
        "stencilStateResolved": (("stencilStateResolved",),),
        "colorWriteMasksResolved": (("colorWriteMasksResolved",),),
        "attachmentSubresourcesResolved": (
            ("attachmentSubresourcesResolved",),
        ),
        "rasterStateResolved": (("rasterStateResolved",),),
        "vertexBufferBindingsResolved": (("vertexBufferBindingsResolved",),),
        "vertexInputUsageResolved": (("vertexInputUsageResolved",),),
        "indexBufferBindingResolved": (("indexBufferBindingResolved",),),
    }
    direct = _optional_bool(_mapping_path(pipeline, *paths[name]))
    if direct is not None:
        return direct
    if name == "blendEnabled":
        targets = _mapping_path(
            pipeline, ("blendState", "targets"), ("blend", "targets")
        )
        if isinstance(targets, (list, tuple)):
            values = [
                _optional_bool(item.get("enabled", item.get("blendEnable")))
                for item in targets
                if isinstance(item, Mapping)
            ]
            if any(value is True for value in values):
                return True
            if values and all(value is False for value in values):
                return False
    return None


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _compact_numeric_mapping(
    value: Any, fields: Sequence[str]
) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for field in fields:
        number = _finite_number(value.get(field))
        if number is not None:
            result[field] = number
    return result


def _compact_color_target_states(value: Any) -> list[dict[str, Any]]:
    """Project bridge blend facts without admitting inconsistent channel masks."""

    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    for raw in value[:MAX_ACTION_IO_RESOURCES]:
        if not isinstance(raw, Mapping):
            continue
        slot = _safe_int(raw.get("slot"), -1)
        if (
            slot < 0
            or slot >= MAX_ACTION_IO_RESOURCES
            or slot in seen_slots
        ):
            continue

        projected: dict[str, Any] = {"slot": slot}
        invalid = False
        for field in ("blendEnabled", "logicOperationEnabled"):
            if field not in raw:
                continue
            raw_state = raw.get(field)
            if raw_state is None:
                projected[field] = None
                continue
            state = _optional_bool(raw_state)
            if state is None:
                invalid = True
                break
            projected[field] = state
        if invalid:
            continue

        write_mask: int | None = None
        if "writeMask" in raw:
            raw_mask = raw.get("writeMask")
            if raw_mask is None:
                projected["writeMask"] = None
            else:
                write_mask = _safe_int(raw_mask, -2)
                if write_mask < -1 or write_mask > 0xF:
                    continue
                projected["writeMask"] = write_mask
        if "writeChannels" in raw:
            raw_channels = raw.get("writeChannels")
            if raw_channels is None:
                if write_mask is not None:
                    continue
                projected["writeChannels"] = None
            else:
                expected_channels = (
                    ""
                    if write_mask == -1
                    else "".join(
                        channel
                        for bit, channel in
                        ((1, "R"), (2, "G"), (4, "B"), (8, "A"))
                        if write_mask is not None and write_mask & bit
                    )
                )
                if (not isinstance(raw_channels, str) or
                        write_mask is None or raw_channels != expected_channels):
                    continue
                projected["writeChannels"] = raw_channels

        for field in ("colorBlend", "alphaBlend"):
            if field not in raw:
                continue
            raw_equation = raw.get(field)
            if raw_equation is None:
                projected[field] = None
                continue
            if not isinstance(raw_equation, Mapping):
                invalid = True
                break
            equation: dict[str, str | None] = {}
            for component in ("sourceFactor", "destinationFactor", "operation"):
                if component not in raw_equation:
                    continue
                raw_text = raw_equation.get(component)
                equation[component] = (
                    None
                    if raw_text is None
                    else _bounded_text(
                        raw_text, f"color target {field} {component}")
                )
            projected[field] = equation
        if invalid:
            continue
        if "logicOperation" in raw:
            raw_operation = raw.get("logicOperation")
            projected["logicOperation"] = (
                None
                if raw_operation is None
                else _bounded_text(raw_operation, "color target logic operation")
            )
        seen_slots.add(slot)
        result.append(projected)
    return sorted(result, key=lambda item: item["slot"])


def _compact_vertex_layout(value: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(value, (list, tuple)):
        return [], 0
    result: list[dict[str, Any]] = []
    for raw in value[:MAX_ACTION_IO_RESOURCES]:
        if not isinstance(raw, Mapping):
            continue
        index = _safe_int(raw.get("index"), -1)
        if index < 0 or index > 0x7FFFFFFF:
            continue
        entry: dict[str, Any] = {"index": index}
        for field in ("name", "format"):
            if field not in raw:
                continue
            raw_text = raw.get(field)
            entry[field] = (
                None
                if raw_text is None
                else _bounded_text(raw_text, f"vertex layout {field}")
            )
        for field in ("vertexBuffer", "byteOffset", "instanceRate"):
            if field not in raw:
                continue
            raw_number = raw.get(field)
            if raw_number is None:
                entry[field] = None
                continue
            number = _safe_int(raw_number, -1)
            if 0 <= number <= 0xFFFFFFFFFFFFFFFF:
                entry[field] = number
        for field in ("perInstance", "genericEnabled", "used"):
            if field not in raw:
                continue
            raw_state = raw.get(field)
            if raw_state is None:
                entry[field] = None
                continue
            state = _optional_bool(raw_state)
            if state is not None:
                entry[field] = state
        result.append(entry)
    return result, max(0, len(value) - MAX_ACTION_IO_RESOURCES)


def _compact_stencil_face(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in (
        "function",
        "failOperation",
        "depthFailOperation",
        "passOperation",
    ):
        text = _bounded_text(value.get(field, ""), f"stencil face {field}")
        if text:
            result[field] = text
    for field in ("reference", "compareMask", "writeMask"):
        number = _safe_int(value.get(field), -1)
        if 0 <= number <= 0xFFFFFFFF:
            result[field] = number
    return result


def _compact_sampler_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    direct_access = _optional_bool(value.get("directAccess"))
    runtime_access_proven = _optional_bool(value.get("runtimeAccessProven"))
    if direct_access is None or runtime_access_proven is not False:
        return {}
    result["directAccess"] = direct_access
    result["runtimeAccessProven"] = False
    for field in (
        "arrayElement",
        "descriptorStoreId",
        "descriptorByteOffset",
        "descriptorByteSize",
        "samplerResourceId",
    ):
        number = _safe_int(value.get(field), -1)
        if 0 <= number <= 0xFFFFFFFFFFFFFFFF:
            result[field] = number
    binding_index = _safe_int(value.get("bindingIndex"), -1)
    if direct_access:
        if "bindingIndex" in value:
            return {}
    elif 0 <= binding_index <= 0xFFFE:
        result["bindingIndex"] = binding_index
    else:
        return {}
    for field in (
        "type",
        "filter",
        "addressU",
        "addressV",
        "addressW",
        "compareFunction",
        "accessEvidence",
    ):
        text = _bounded_text(value.get(field, ""), f"sampler binding {field}")
        if text:
            result[field] = text
    for field in ("creationTimeConstant", "staticallyUnused"):
        state = _optional_bool(value.get(field))
        if state is not None:
            result[field] = state
    for field in ("maxAnisotropy", "minLOD", "maxLOD", "mipBias"):
        number = _finite_number(value.get(field))
        if number is not None:
            result[field] = number
    return result


def _compact_workload(action: Mapping[str, Any]) -> dict[str, Any]:
    workload = action.get("workload")
    result: dict[str, Any] = {}
    if isinstance(workload, Mapping):
        for field in (
            "indexCount",
            "vertexCount",
            "instanceCount",
            "groupCountX",
            "groupCountY",
            "groupCountZ",
            "threadCountX",
            "threadCountY",
            "threadCountZ",
            "rayWidth",
            "rayHeight",
            "rayDepth",
            "indexOffset",
            "vertexOffset",
            "instanceOffset",
            "baseVertex",
        ):
            number = _safe_int(workload.get(field), -1)
            if number >= 0:
                result[field] = number
        if "drawIndex" in workload:
            raw_draw_index = workload.get("drawIndex")
            if raw_draw_index is None:
                result["drawIndex"] = None
            else:
                draw_index = _safe_int(raw_draw_index, -1)
                if 0 <= draw_index <= 0xFFFFFFFF:
                    result["drawIndex"] = draw_index
        if "dispatchBase" in workload:
            raw_dispatch_base = workload.get("dispatchBase")
            if raw_dispatch_base is None:
                result["dispatchBase"] = None
            elif (isinstance(raw_dispatch_base, (list, tuple)) and
                    len(raw_dispatch_base) == 3):
                dispatch_base = [
                    _safe_int(component, -1)
                    for component in raw_dispatch_base
                ]
                if all(0 <= component <= 0xFFFFFFFF
                       for component in dispatch_base):
                    result["dispatchBase"] = dispatch_base
    result.setdefault("indexCount", _safe_int(action.get("numIndices"), 0))
    result.setdefault(
        "vertexCount",
        _safe_int(action.get("numVertices", action.get("vertexCount")), 0),
    )
    result.setdefault("instanceCount", _safe_int(action.get("numInstances"), 0))
    if not (_safe_int(action.get("flags"), 0) & (FLAG_INSTANCED | FLAG_INDIRECT)
            or _has_named_flag(_flag_names(action), "Instanced") or _is_indirect(action)):
        # A non-instanced Draw/DrawIndexed executes once even when RenderDoc's
        # unused numInstances field is zero. Preserve zero for instanced and
        # unresolved indirect actions; it must not be normalized away there.
        result["instanceCount"] = 1
    if _is_indirect(action):
        result["indirectArgumentsResolved"] = False
    return result


def _compact_draw_state(pipeline: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    topology = _bounded_text(pipeline.get("topology", ""), "pipeline topology")
    if topology:
        result["topology"] = topology
    viewport = _compact_numeric_mapping(
        pipeline.get("viewport"),
        ("x", "y", "width", "height", "minDepth", "maxDepth"),
    )
    if viewport:
        result["viewport"] = viewport
    scissor = _compact_numeric_mapping(
        pipeline.get("scissor"), ("x", "y", "width", "height")
    )
    if scissor:
        result["scissor"] = scissor
    if "sampleCount" in pipeline:
        raw_sample_count = pipeline.get("sampleCount")
        if raw_sample_count is None:
            result["sampleCount"] = None
        else:
            sample_count = _safe_int(raw_sample_count, -1)
            if 0 <= sample_count <= 0xFFFFFFFF:
                result["sampleCount"] = sample_count
    for field in ("cullMode", "fillMode", "depthFunction"):
        text = _bounded_text(pipeline.get(field, ""), f"pipeline {field}")
        if text:
            result[field] = text
    for field in (
        "blendEnabled",
        "depthTestEnabled",
        "depthWriteEnabled",
        "depthReadOnly",
        "frontCCW",
        "stencilTestEnabled",
        "stencilReadOnly",
        "stencilWritePossible",
        "stencilStateResolved",
        "colorWriteMasksResolved",
        "attachmentSubresourcesResolved",
        "rasterStateResolved",
        "vertexBufferBindingsResolved",
        "vertexInputUsageResolved",
        "indexBufferBindingResolved",
    ):
        state = _pipeline_bool(pipeline, field)
        if state is not None:
            result[field] = state
    for field in (
        "sampleCountResolved",
        "depthStateResolved",
        "stencilWriteOperationsCanWrite",
        "rasterizerDiscardEnabled",
        "rasterOutputApplicable",
        "pipelineStateApplicable",
    ):
        if field not in pipeline:
            continue
        raw_state = pipeline.get(field)
        if raw_state is None:
            result[field] = None
            continue
        state = _optional_bool(raw_state)
        if state is not None:
            result[field] = state
    if "rasterOutputSuppressionReason" in pipeline:
        raw_reason = pipeline.get("rasterOutputSuppressionReason")
        result["rasterOutputSuppressionReason"] = (
            None
            if raw_reason is None
            else _bounded_text(raw_reason, "raster output suppression reason")
        )
    if "rasterizedStream" in pipeline:
        raw_stream = pipeline.get("rasterizedStream")
        if raw_stream is None:
            result["rasterizedStream"] = None
        else:
            stream = _safe_int(raw_stream, -(0x80000001))
            if -0x80000000 <= stream <= 0x7FFFFFFF:
                result["rasterizedStream"] = stream
    if "indexStride" in pipeline:
        raw_stride = pipeline.get("indexStride")
        if raw_stride is None:
            result["indexStride"] = None
        else:
            stride = _safe_int(raw_stride, -1)
            if 0 <= stride <= 0xFFFFFFFFFFFFFFFF:
                result["indexStride"] = stride
    if "vertexLayout" in pipeline:
        vertex_layout, locally_omitted = _compact_vertex_layout(
            pipeline.get("vertexLayout")
        )
        result["vertexLayout"] = vertex_layout
        source_omitted = _safe_int(
            pipeline.get("vertexLayoutOmittedCount"), 0
        )
        omitted = max(0, source_omitted) + locally_omitted
        if omitted:
            result["vertexLayoutOmittedCount"] = min(
                omitted, 0xFFFFFFFFFFFFFFFF
            )
    elif "vertexLayoutOmittedCount" in pipeline:
        omitted = _safe_int(pipeline.get("vertexLayoutOmittedCount"), -1)
        if 0 <= omitted <= 0x7FFFFFFF:
            result["vertexLayoutOmittedCount"] = omitted
    color_target_states = _compact_color_target_states(
        pipeline.get("colorTargetStates")
    )
    if color_target_states or "colorTargetStates" in pipeline:
        result["colorTargetStates"] = color_target_states
    for field in ("stencilFront", "stencilBack"):
        face = _compact_stencil_face(pipeline.get(field))
        if face:
            result[field] = face
    sampler_state = pipeline.get("samplers")
    if isinstance(sampler_state, Mapping):
        samplers: dict[str, dict[str, Any]] = {}
        for raw_stage, raw_counts in list(sampler_state.items())[
            :MAX_DRAW_STATE_STAGES
        ]:
            if not isinstance(raw_counts, Mapping):
                continue
            stage_name = _bounded_text(raw_stage, "sampler stage")
            if not stage_name:
                continue
            counts: dict[str, Any] = {}
            for field in ("declaredCount", "boundCount"):
                count = _safe_int(raw_counts.get(field), -1)
                if count >= 0:
                    counts[field] = count
            bindings_resolved = _optional_bool(raw_counts.get("bindingsResolved"))
            if bindings_resolved is not None:
                counts["bindingsResolved"] = bindings_resolved
            raw_bindings = raw_counts.get("bindings")
            bindings: list[dict[str, Any]] = []
            if isinstance(raw_bindings, (list, tuple)):
                for raw_binding in raw_bindings[:MAX_SAMPLER_BINDINGS_PER_STAGE]:
                    binding = _compact_sampler_binding(raw_binding)
                    if binding:
                        bindings.append(binding)
                if bindings:
                    counts["bindings"] = bindings
                omitted = max(
                    _safe_int(raw_counts.get("bindingOmittedCount"), 0),
                    len(raw_bindings) - MAX_SAMPLER_BINDINGS_PER_STAGE,
                )
            else:
                omitted = _safe_int(raw_counts.get("bindingOmittedCount"), 0)
            if omitted > 0:
                counts["bindingOmittedCount"] = omitted
            if counts:
                samplers[stage_name] = counts
        if samplers:
            result["samplers"] = samplers
    return result


def _action_feature(
    action: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    catalog_uses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    action_kind = _action_kind(action)
    render_targets = _resource_list(pipeline.get("renderTargets", []))
    color_write_targets = _resource_list(
        pipeline.get("colorWriteTargets", [])
    )
    action_outputs = _resource_list(action.get("outputs", []))
    action_depth_outputs = _resource_list(
        [action.get("depthOutput")] if action.get("depthOutput") else []
    )
    depth_raw = pipeline.get("depthTarget")
    if isinstance(depth_raw, Mapping) or depth_raw:
        depth = _resource_list([depth_raw])
    else:
        depth = []

    blend_enabled = _pipeline_bool(pipeline, "blendEnabled")
    depth_test_enabled = _pipeline_bool(pipeline, "depthTestEnabled")
    depth_write_enabled = _pipeline_bool(pipeline, "depthWriteEnabled")
    raster_output_state_present = "rasterOutputApplicable" in pipeline
    raster_output_applicable = (
        _optional_bool(pipeline.get("rasterOutputApplicable"))
        if raster_output_state_present
        else True
    )
    raster_output_definite = raster_output_applicable is True
    raster_output_may_apply = raster_output_applicable is not False

    copy_sources = _resource_list(
        [action.get("copySource")]
        if action.get("copySource")
        else pipeline.get("copySources", [])
    )
    copy_destinations = _resource_list(
        [action.get("copyDestination")]
        if action.get("copyDestination")
        else pipeline.get("copyDestinations", [])
    )
    possible_outputs = _resource_list(pipeline.get("possibleOutputs", []))
    blend_read_targets = _resource_list(
        pipeline.get("blendReadTargets", [])
    )
    action_flags = _safe_int(action.get("flags"), 0)
    present_inputs: list[dict[str, Any]] = []
    utility_has_dedicated_outputs = action_kind == "utility" and bool(action_flags & (
        FLAG_CLEAR | FLAG_COPY | FLAG_RESOLVE | FLAG_GEN_MIPS
    ))
    if utility_has_dedicated_outputs:
        action_definite_outputs = _utility_destination_outputs(
            copy_destinations,
            action_depth_outputs,
            action_outputs,
        )
    elif action_kind == "utility" and action_flags & FLAG_PRESENT:
        action_definite_outputs = []
        present_inputs = _resource_access(action_outputs, "present-source")
    else:
        action_definite_outputs = []
        if raster_output_may_apply:
            possible_outputs = _unique_resources(
                possible_outputs,
                _resource_access(action_outputs, "bound-output-possible"),
            )
        if action_kind == "graphics" and action_depth_outputs:
            if (raster_output_definite and depth_test_enabled is True and
                    depth_write_enabled is True):
                action_definite_outputs = _resource_access(
                    action_depth_outputs, "depth-target"
                )
            elif raster_output_may_apply and (
                depth_test_enabled is not False
                and depth_write_enabled is not False
            ):
                depth_candidates = _resource_access(
                    action_depth_outputs, "depth-target-possible"
                )
                if not raster_output_definite:
                    for candidate in depth_candidates:
                        candidate["certainty"] = "unknown-rasterizer-discard"
                possible_outputs = _unique_resources(
                    possible_outputs, depth_candidates
                )
    read_resources = _unique_resources(
        _resource_access(
            _resource_list(pipeline.get("readResources", pipeline.get("reads", []))),
            "read",
        ),
        _resource_access(copy_sources, "copy-source"),
        present_inputs,
        _resource_access(blend_read_targets, "blend-destination"),
        (
            _resource_access(depth, "depth-test")
            if depth_test_enabled is True
            else []
        ),
    )
    pipeline_write_resources = _resource_access(
        _resource_list(
            pipeline.get("writeResources", pipeline.get("writes", []))
        ),
        "write",
    )
    if raster_output_state_present and not raster_output_definite:
        retained_writes: list[dict[str, Any]] = []
        for resource in pipeline_write_resources:
            access = str(resource.get("access", ""))
            if access not in ("color-target", "depth-target"):
                retained_writes.append(resource)
                continue
            if raster_output_may_apply:
                candidate = dict(resource)
                candidate["access"] = (
                    "color-target-possible"
                    if access == "color-target"
                    else "depth-target-possible"
                )
                candidate["certainty"] = "unknown-rasterizer-discard"
                possible_outputs = _unique_resources(
                    possible_outputs, [candidate]
                )
        pipeline_write_resources = retained_writes
    definite_color_targets = (
        _resource_access(color_write_targets, "color-target")
        if raster_output_definite else []
    )
    if raster_output_may_apply and not raster_output_definite:
        possible_color_targets = _resource_access(
            color_write_targets, "color-target-possible"
        )
        for candidate in possible_color_targets:
            candidate["certainty"] = "unknown-rasterizer-discard"
        possible_outputs = _unique_resources(
            possible_outputs, possible_color_targets
        )
    write_resources = _unique_resources(
        pipeline_write_resources,
        (
            []
            if utility_has_dedicated_outputs
            else _resource_access(copy_destinations, "copy-destination")
        ),
        action_definite_outputs,
        definite_color_targets,
        (
            _resource_access(depth, "depth-target")
            if (raster_output_definite and depth_test_enabled is True and
                depth_write_enabled is True)
            else []
        ),
    )
    if (
        raster_output_may_apply
        and
        depth_test_enabled is not False
        and depth_write_enabled is not False
        and not (
            raster_output_definite
            and depth_test_enabled is True
            and depth_write_enabled is True
        )
        and depth
    ):
        possible_outputs = _unique_resources(
            possible_outputs,
            [
                {
                    **dict(item),
                    "access": "depth-target-possible",
                    "certainty": (
                        "unknown-rasterizer-discard"
                        if not raster_output_definite else "unknown"
                    ),
                }
                for item in depth
            ],
        )
    if (
        (depth_test_enabled is False or depth_write_enabled is False)
        and depth
    ):
        depth_ids = {
            _safe_int(item.get("resourceId"), 0) for item in depth
        }
        write_resources = [
            item
            for item in write_resources
            if not (
                _safe_int(item.get("resourceId"), 0) in depth_ids
                and str(item.get("access", "")) in ("write", "depth-target")
            )
        ]
    read_resources, locally_omitted_inputs = _bounded_resource_direction(
        read_resources
    )
    write_resources, locally_omitted_outputs = _bounded_resource_direction(
        write_resources
    )
    possible_outputs, locally_omitted_possible = _bounded_resource_direction(
        possible_outputs
    )
    locally_omitted_outputs += locally_omitted_possible

    shaders: list[dict[str, Any]] = []
    pipeline_shaders = pipeline.get("shaders", {})
    if isinstance(pipeline_shaders, Mapping):
        for stage, value in pipeline_shaders.items():
            if not isinstance(value, Mapping):
                continue
            shader = {
                "stage": str(stage).strip().lower(),
                "resourceId": _safe_int(
                    value.get("shaderId", value.get("resourceId")), 0
                ),
                "rawSha256": _sha(value.get("rawSha256")),
                "codeSha256": _sha(value.get("codeSha256")),
                "entryPoint": _bounded_text(
                    value.get("entryPoint", value.get("entry", "")),
                    "shader entry point",
                ),
                "name": _shader_display_name(value),
            }
            for count_field in (
                "constantBufferCount",
                "samplerCount",
                "boundSamplerCount",
            ):
                count = _safe_int(value.get(count_field), -1)
                if count >= 0:
                    shader[count_field] = count
            shaders.append(shader)
    for use in catalog_uses:
        stage = str(use.get("stage", "") or "").strip().lower()
        resource = _safe_int(use.get("resourceId", use.get("shaderResourceId")), 0)
        raw = _sha(use.get("rawSha256"))
        match = next(
            (
                shader
                for shader in shaders
                if shader["stage"] == stage
                and (
                    (resource and shader["resourceId"] == resource)
                    or (raw and shader["rawSha256"] == raw)
                )
            ),
            None,
        )
        if match is None:
            match = {
                "stage": stage,
                "resourceId": resource,
                "rawSha256": raw,
                "codeSha256": _sha(use.get("codeSha256")),
                "entryPoint": _bounded_text(
                    use.get("entryPoint", use.get("entry", "")),
                    "shader entry point",
                ),
                "name": _shader_display_name(use),
            }
            shaders.append(match)
        else:
            if raw:
                match["rawSha256"] = raw
            code = _sha(use.get("codeSha256"))
            if code:
                match["codeSha256"] = code
            display = _shader_display_name(use)
            if display:
                match["name"] = display
            entry_point = _bounded_text(
                use.get("entryPoint", use.get("entry", "")),
                "shader entry point",
            )
            if entry_point:
                match["entryPoint"] = entry_point

    unique_shaders: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for shader in shaders:
        key = (
            shader["stage"],
            shader["resourceId"],
            shader["rawSha256"],
            shader.get("entryPoint", ""),
        )
        unique_shaders[key] = shader
    shaders = sorted(
        unique_shaders.values(),
        key=lambda item: (
            item["stage"],
            item["resourceId"],
            item["rawSha256"],
            item.get("entryPoint", ""),
        ),
    )

    num_indices = _safe_int(action.get("numIndices"), 0)
    num_vertices = _safe_int(action.get("numVertices", action.get("vertexCount")), 0)
    explicit_fullscreen = _optional_bool(
        pipeline.get("fullscreen", pipeline.get("isFullscreen"))
    )
    triangle_candidate = num_indices == 3 or num_vertices == 3
    fullscreen = explicit_fullscreen is True or (
        explicit_fullscreen is None and triangle_candidate and not depth
    )

    return {
        "eventId": action["eventId"],
        "actionKind": action_kind,
        "name": action["name"],
        "markerPath": list(action.get("markerPath", [])),
        "markerEventPath": [
            dict(item) for item in action.get("markerEventPath", [])
        ],
        "parentEventId": _safe_int(action.get("_parentEventId"), 0),
        "executionIndex": _safe_int(action.get("_executionIndex"), 0),
        "flags": _safe_int(action.get("flags"), 0),
        "indirect": _is_indirect(action),
        "indirectContainerName": _indirect_container_name(action),
        "renderTargets": render_targets,
        "depthTargets": depth,
        "readResources": read_resources,
        "writeResources": write_resources,
        "possibleOutputs": possible_outputs,
        "blendEnabled": blend_enabled,
        "depthTestEnabled": depth_test_enabled,
        "depthWriteEnabled": depth_write_enabled,
        "fullscreen": fullscreen,
        "fullscreenExplicit": explicit_fullscreen is not None,
        "shaders": shaders,
        "numIndices": num_indices,
        "numVertices": num_vertices,
        "workload": _compact_workload(action),
        "workloadKnown": bool(
            isinstance(action.get("workload"), Mapping)
            or any(
                field in action
                for field in ("numIndices", "numVertices", "numInstances")
            )
        ),
        "drawState": _compact_draw_state(pipeline),
        "inputOmittedCount": max(
            0, _safe_int(pipeline.get("inputOmittedCount"), 0)
        ) + locally_omitted_inputs,
        "outputOmittedCount": max(
            0, _safe_int(pipeline.get("outputOmittedCount"), 0)
        )
        + max(0, _safe_int(action.get("outputOmittedCount"), 0))
        + locally_omitted_outputs,
        "lateFrameFraction": pipeline.get("lateFrameFraction"),
    }


def _pass_fingerprint(feature: Mapping[str, Any]) -> str:
    shaders = [
        {
            "stage": item["stage"],
            "rawSha256": item["rawSha256"],
            "entryPoint": item.get("entryPoint", ""),
        }
        for item in feature.get("shaders", [])
        if item.get("rawSha256")
    ]
    if not shaders:
        return ""
    payload = {
        "actionKind": feature.get("actionKind"),
        "shaders": shaders,
        "renderTargetCount": len(feature.get("renderTargets", [])),
        "hasDepthTarget": bool(feature.get("depthTargets")),
        "targetFormats": [
            item["format"]
            for item in feature.get("renderTargets", [])
            if item.get("format")
        ],
        "depthFormats": [
            item["format"]
            for item in feature.get("depthTargets", [])
            if item.get("format")
        ],
        "blendEnabled": feature.get("blendEnabled"),
        "depthTestEnabled": feature.get("depthTestEnabled"),
        "depthWriteEnabled": feature.get("depthWriteEnabled"),
        # Resource IDs are capture-local.  Counts describe the portable shape
        # without pretending that two numeric IDs match across frames.
        "readResourceCount": len(feature.get("readResources", [])),
        "writeResourceCount": len(feature.get("writeResources", [])),
        "topology": feature.get("drawState", {}).get("topology", ""),
        "sampleCount": feature.get("drawState", {}).get("sampleCount"),
    }
    return _canonical_sha256(payload)


def _evidence(source: str, code: str, detail: str, weight: float) -> dict[str, Any]:
    return {
        "source": source,
        "code": code,
        "detail": detail,
        "weight": round(float(weight), 4),
        "authority": "FixedProgram",
    }


def _add_score(
    scores: dict[str, float],
    evidence: dict[str, list[dict[str, Any]]],
    category: str,
    weight: float,
    item: dict[str, Any],
) -> None:
    if category not in CATEGORIES or category == "Unknown":
        return
    weight = max(0.0, min(float(weight), 1.0))
    scores[category] = 1.0 - (1.0 - scores.get(category, 0.0)) * (1.0 - weight)
    evidence.setdefault(category, []).append(item)


def _subcategory(category: str, text: str, feature: Mapping[str, Any]) -> str:
    normal, compact, words = _normal_search_text(text)
    if category == "Depth":
        if "hiz" in compact:
            return "HiZ"
        if "prepass" in compact or "predepth" in compact or "earlyz" in compact:
            return "DepthPrepass"
        return "DepthOnly"
    if category == "Shadow":
        if "virtualshadow" in compact or "vsm" in words:
            return "VirtualShadowMap"
        if "csm" in words or "cascade" in normal:
            return "CascadedShadow"
        return "ShadowMap"
    if category == "BasePass":
        return "ForwardOpaque" if "forward" in normal else "OpaqueBasePass"
    if category == "GBuffer":
        return "DeferredGeometry"
    if category == "Lighting":
        if "ssao" in words or "ambient occlusion" in normal:
            return "AmbientOcclusion"
        return "DeferredLighting" if "deferred" in normal else "Lighting"
    if category == "Transparent":
        if "particle" in compact:
            return "Particles"
        if "water" in words:
            return "Water"
        return "Translucency"
    if category == "Post":
        tags = (
            ("tonemap", "ToneMap"),
            ("bloom", "Bloom"),
            ("taa", "TAA"),
            ("fxaa", "FXAA"),
            ("upscale", "Upscale"),
            ("upsample", "Upscale"),
            ("depthoffield", "DepthOfField"),
            ("dof", "DepthOfField"),
            ("motionblur", "MotionBlur"),
            ("colorgrade", "ColorGrading"),
        )
        for token, label in tags:
            if token in compact:
                return label
        return "PostProcess"
    if category == "UI":
        if "imgui" in compact:
            return "ImGui"
        if "slate" in compact:
            return "Slate"
        if "hud" in words:
            return "HUD"
        return "UI"
    if category == "Compute":
        if feature.get("flags", 0) & FLAG_DISPATCH_RAY:
            return "RayDispatch"
        if feature.get("flags", 0) & FLAG_MESH_DISPATCH:
            return "MeshDispatch"
        return "ComputeDispatch"
    if category == "Utility":
        flags = _safe_int(feature.get("flags"), 0)
        for mask, label in (
            (FLAG_CLEAR, "Clear"),
            (FLAG_COPY, "Copy"),
            (FLAG_RESOLVE, "Resolve"),
            (FLAG_GEN_MIPS, "GenerateMips"),
            (FLAG_BUILD_ACCELERATION_STRUCTURE, "BuildAccelerationStructure"),
            (FLAG_PRESENT, "Present"),
        ):
            if flags & mask:
                return label
        return "IndirectContainer" if feature.get("indirect") else "Utility"
    return "Unclassified"


def _knowledge_records(value: Any) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PassClassificationError("project_knowledge must be an object")
    if (
        value.get("schema") != KNOWLEDGE_SCHEMA
        or value.get("schemaVersion") != KNOWLEDGE_VERSION
    ):
        raise PassClassificationError(
            "project pass knowledge schema/version is unsupported"
        )
    result: dict[str, Mapping[str, Any]] = {}
    records = value.get("records", [])
    if not isinstance(records, list):
        raise PassClassificationError("project pass knowledge records must be a list")
    for record in records:
        if not isinstance(record, Mapping):
            continue
        fingerprint = _sha(record.get("passFingerprint"))
        authority = str(record.get("authority", ""))
        if (
            fingerprint
            and record.get("reusable") is True
            and record.get("category") in CATEGORIES - {"Unknown"}
            and authority in ("FixedProgram", "AssetVerified")
        ):
            result[fingerprint] = record
    return result


def _classify_feature(
    feature: Mapping[str, Any],
    project_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    kind = str(feature.get("actionKind", "unknown"))
    scores: dict[str, float] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    exact_classification = False

    if kind == "compute":
        _add_score(
            scores,
            evidence,
            "Compute",
            1.0,
            _evidence(
                "actionFlags",
                "dispatch",
                "ActionFlags identifies a Dispatch/DispatchRay",
                1.0,
            ),
        )
        exact_classification = True
    elif kind == "utility":
        _add_score(
            scores,
            evidence,
            "Utility",
            1.0,
            _evidence(
                "actionFlags",
                "utility-action",
                "ActionFlags identifies a non-draw GPU action",
                1.0,
            ),
        )
        exact_classification = True
    elif kind == "container":
        _add_score(
            scores,
            evidence,
            "Utility",
            1.0,
            _evidence(
                "actionFlags",
                "multi-action-container",
                "MultiAction is a container, not an additional draw",
                1.0,
            ),
        )
        exact_classification = True

    source_texts: list[tuple[str, str, float]] = []
    marker_text = " / ".join(feature.get("markerPath", []))
    if marker_text:
        source_texts.append(("markerPath", marker_text, 0.84))
    source_texts.append(("actionName", str(feature.get("name", "")), 0.72))
    shader_text = " / ".join(
        item.get("name", "") for item in feature.get("shaders", []) if item.get("name")
    )
    if shader_text:
        source_texts.append(("shaderName", shader_text, 0.66))
    target_text = " / ".join(
        item.get("name", "")
        for item in [
            *feature.get("renderTargets", []),
            *feature.get("depthTargets", []),
        ]
        if item.get("name")
    )
    if target_text:
        source_texts.append(("targetName", target_text, 0.72))

    signal_categories: set[str] = set()
    # A Dispatch remains in the Compute top-level category.  Its semantic role
    # is still reported below, but marker text cannot turn it into a verified
    # graphics pass.
    if kind not in ("compute", "utility", "container"):
        for source, text, weight in source_texts:
            for category, matches in _match_signals(text).items():
                signal_categories.add(category)
                _add_score(
                    scores,
                    evidence,
                    category,
                    weight,
                    _evidence(
                        source,
                        "semantic-token",
                        f"{source} matched {', '.join(matches[:4])}",
                        weight,
                    ),
                )

    render_target_count = len(feature.get("renderTargets", []))
    has_depth = bool(feature.get("depthTargets"))
    blend = feature.get("blendEnabled")
    depth_write = feature.get("depthWriteEnabled")
    depth_test = feature.get("depthTestEnabled")
    if kind == "graphics":
        if has_depth and render_target_count == 0:
            if "Shadow" in signal_categories:
                _add_score(
                    scores,
                    evidence,
                    "Shadow",
                    0.78,
                    _evidence(
                        "pipelineState",
                        "depth-only-target",
                        "Depth target is bound with no color target",
                        0.78,
                    ),
                )
            else:
                _add_score(
                    scores,
                    evidence,
                    "Depth",
                    0.96,
                    _evidence(
                        "pipelineState",
                        "depth-only-target",
                        "Depth target is bound with no color target",
                        0.96,
                    ),
                )
                exact_classification = True
        if render_target_count >= 3:
            _add_score(
                scores,
                evidence,
                "GBuffer",
                0.68,
                _evidence(
                    "pipelineState",
                    "multiple-color-targets",
                    f"{render_target_count} color targets are bound",
                    0.68,
                ),
            )
        if blend is True:
            _add_score(
                scores,
                evidence,
                "Transparent",
                0.76,
                _evidence(
                    "blendState",
                    "blend-enabled",
                    "At least one color target has blending enabled",
                    0.76,
                ),
            )
            # Blending alone says translucent composition, not whether the
            # layer is world transparency or UI.  An independent UI name/
            # marker plus disabled depth makes the latter the stronger fixed
            # explanation.
            if "UI" in signal_categories and depth_test is False:
                _add_score(
                    scores,
                    evidence,
                    "UI",
                    0.68,
                    _evidence(
                        "pipelineState",
                        "ui-blend-no-depth",
                        "UI semantic token is corroborated by blending with depth testing disabled",
                        0.68,
                    ),
                )
        if render_target_count and depth_write is True and blend is False:
            _add_score(
                scores,
                evidence,
                "BasePass",
                0.50,
                _evidence(
                    "depthState",
                    "opaque-depth-write",
                    "Color output writes depth with blending disabled",
                    0.50,
                ),
            )
        if (
            feature.get("fullscreen")
            and render_target_count
            and depth_write is not True
        ):
            _add_score(
                scores,
                evidence,
                "Post",
                0.48 if not feature.get("fullscreenExplicit") else 0.62,
                _evidence(
                    "geometry",
                    "fullscreen-draw",
                    "Pipeline/geometry describes a fullscreen draw without depth writes",
                    0.48 if not feature.get("fullscreenExplicit") else 0.62,
                ),
            )
        if (
            feature.get("fullscreen")
            and render_target_count == 1
            and len(feature.get("readResources", [])) >= 3
        ):
            _add_score(
                scores,
                evidence,
                "Lighting",
                0.56,
                _evidence(
                    "resourceFlow",
                    "many-inputs-one-output",
                    "Fullscreen draw reads at least three resources into one target",
                    0.56,
                ),
            )
        late_fraction = feature.get("lateFrameFraction")
        if (
            blend is True
            and depth_test is False
            and isinstance(late_fraction, (int, float))
            and not isinstance(late_fraction, bool)
            and float(late_fraction) >= 0.85
        ):
            _add_score(
                scores,
                evidence,
                "UI",
                0.42,
                _evidence(
                    "eventOrder",
                    "late-blended-no-depth",
                    "Late blended draw has depth testing disabled",
                    0.42,
                ),
            )

    fingerprint = _pass_fingerprint(feature)
    knowledge = project_records.get(fingerprint) if fingerprint else None
    if knowledge is not None and kind not in ("utility", "container"):
        authority = str(knowledge.get("authority"))
        category = str(knowledge.get("category"))
        weight = 0.97 if authority == "AssetVerified" else 0.82
        _add_score(
            scores,
            evidence,
            category,
            weight,
            _evidence(
                "projectKnowledge",
                "exact-pass-fingerprint",
                "Exact portable pass fingerprint matched "
                f"{_safe_int(knowledge.get('captureCount'), 1)} prior capture(s)",
                weight,
            ),
        )
        if authority == "AssetVerified":
            exact_classification = True

    ranked = sorted(
        ((category, score) for category, score in scores.items()),
        key=lambda pair: (-pair[1], CATEGORY_ORDER.index(pair[0])),
    )
    if not ranked:
        category = "Unknown"
        score = 0.0
        selected_evidence = [
            _evidence(
                "classifier",
                "insufficient-fixed-evidence",
                "No fixed marker, shader, target, state or project evidence selected a pass",
                0.0,
            )
        ]
        alternatives: list[dict[str, Any]] = []
    else:
        category, score = ranked[0]
        selected_evidence = evidence.get(category, [])
        alternatives = [
            {"category": other, "score": round(other_score, 4)}
            for other, other_score in ranked[1:]
            if other_score >= 0.25
        ][:4]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score - runner_up
    if exact_classification and score >= 0.95 and margin >= 0.10:
        confidence = "confirmed"
    elif score >= 0.64 and margin >= 0.10:
        confidence = "probable"
    else:
        confidence = "candidate"

    combined_text = " / ".join(
        text for unused_source, text, unused_weight in source_texts
    )
    semantic_role = ""
    if category == "Compute":
        semantic_candidates: list[tuple[str, int]] = []
        for unused_source, text, unused_weight in source_texts:
            for role, hits in _match_signals(text).items():
                semantic_candidates.append((role, len(hits)))
        if semantic_candidates:
            semantic_role = min(
                semantic_candidates,
                key=lambda pair: (-pair[1], CATEGORY_ORDER.index(pair[0])),
            )[0]

    limitations: list[str] = []
    if feature.get("indirect"):
        limitations.append(
            "Indirect arguments are GPU-authored and were not resolved; concrete child actions remain inspectable, but workload remains unknown."
        )
    if has_depth and depth_write is None:
        limitations.append(
            "Depth-write enablement was unavailable; the bound depth target is retained only as a possible output."
        )
    input_omitted_count = max(
        0, _safe_int(feature.get("inputOmittedCount"), 0)
    )
    output_omitted_count = max(
        0, _safe_int(feature.get("outputOmittedCount"), 0)
    )
    if input_omitted_count or output_omitted_count:
        limitations.append(
            "Per-action resource evidence was bounded; omitted input/output counts are reported explicitly."
        )
    if category not in ("Compute", "Utility") and confidence != "confirmed":
        limitations.append(
            "Pass semantics are inferred from fixed evidence, not engine-authored annotations."
        )

    inputs = _unique_resources(feature.get("readResources", []))
    outputs = _unique_resources(feature.get("writeResources", []))
    possible_outputs = _unique_resources(feature.get("possibleOutputs", []))
    issued_action = kind in ("graphics", "compute", "utility")
    workload = dict(feature.get("workload", {}))
    non_zero_workload: bool | None
    if feature.get("indirect"):
        non_zero_workload = None
    elif (
        _safe_int(feature.get("flags"), 0) & FLAG_MESH_DISPATCH
        and all(
            field in workload
            for field in ("groupCountX", "groupCountY", "groupCountZ")
        )
    ):
        non_zero_workload = all(
            _safe_int(workload.get(field), 0) > 0
            for field in ("groupCountX", "groupCountY", "groupCountZ")
        )
    elif _safe_int(feature.get("flags"), 0) & FLAG_MESH_DISPATCH:
        non_zero_workload = None
    elif kind == "graphics" and feature.get("workloadKnown"):
        primitive_count = max(
            _safe_int(workload.get("indexCount"), 0),
            _safe_int(workload.get("vertexCount"), 0),
        )
        instance_count = _safe_int(workload.get("instanceCount"), 0)
        non_zero_workload = primitive_count > 0 and instance_count > 0
    elif kind == "compute" and all(
        field in workload for field in ("groupCountX", "groupCountY", "groupCountZ")
    ):
        non_zero_workload = all(
            _safe_int(workload.get(field), 0) > 0
            for field in ("groupCountX", "groupCountY", "groupCountZ")
        )
    elif kind == "utility":
        non_zero_workload = True
    else:
        non_zero_workload = None
    output_candidate = non_zero_workload is not False and bool(
        outputs or possible_outputs
    )

    result = {
        "eventId": feature["eventId"],
        "name": feature["name"],
        "markerPath": list(feature.get("markerPath", [])),
        "markerEventPath": [
            dict(item) for item in feature.get("markerEventPath", [])
        ],
        "parentEventId": _safe_int(feature.get("parentEventId"), 0),
        "executionIndex": _safe_int(feature.get("executionIndex"), 0),
        "category": category,
        "subcategory": _subcategory(category, combined_text, feature),
        "confidence": confidence,
        "confidenceScore": round(score, 4),
        "authority": "FixedProgram",
        "semanticVerified": bool(
            knowledge is not None
            and knowledge.get("authority") == "AssetVerified"
            and category == knowledge.get("category")
        ),
        "evidence": selected_evidence,
        "alternatives": alternatives,
        "actionKind": kind,
        "indirect": bool(feature.get("indirect")),
        "indirectContainerName": feature.get("indirectContainerName", ""),
        "passFingerprint": fingerprint,
        "shaders": [dict(item) for item in feature.get("shaders", [])],
        "inputs": inputs,
        "outputs": outputs,
        "possibleOutputs": possible_outputs,
        "inputOmittedCount": input_omitted_count,
        "outputOmittedCount": output_omitted_count,
        "workload": workload,
        "drawState": dict(feature.get("drawState", {})),
        "actualWorkState": (
            "output-affecting-candidate"
            if output_candidate
            else "not-established"
        ),
        "participation": {
            "issuedAction": issued_action,
            "nonZeroWorkload": non_zero_workload,
            "indirectArgumentsResolved": (
                False if feature.get("indirect") else None
            ),
            "outputAffectingCandidate": output_candidate,
            "observedContributor": False,
            "contributorObservation": (
                "not-tested; no pixel history, resource diff, or contribution counter was run"
            ),
            "basis": (
                "action-flags-and-bound-output"
                if output_candidate
                else "action-flags-only"
                if issued_action
                else "no-executable-action-evidence"
            ),
        },
        "targets": {
            "renderTargets": [dict(item) for item in feature.get("renderTargets", [])],
            "depthTargets": [dict(item) for item in feature.get("depthTargets", [])],
        },
        "stateSummary": {
            "blendEnabled": blend,
            "depthTestEnabled": depth_test,
            "depthWriteEnabled": depth_write,
            "readResourceCount": len(feature.get("readResources", [])),
            "writeResourceCount": len(feature.get("writeResources", [])),
            "possibleOutputCount": len(feature.get("possibleOutputs", [])),
            "fullscreen": bool(feature.get("fullscreen")),
        },
        "limitations": limitations,
    }
    if semantic_role:
        result["semanticRole"] = semantic_role
    return result


def _container_record(action: Mapping[str, Any]) -> dict[str, Any]:
    name = _indirect_container_name(action) or action.get("name", "ExecuteIndirect")
    return {
        "eventId": action["eventId"],
        "name": action["name"],
        "label": name,
        "markerPath": list(action.get("markerPath", [])),
        "markerEventPath": [
            dict(item) for item in action.get("markerEventPath", [])
        ],
        "parentEventId": _safe_int(action.get("_parentEventId"), 0),
        "executionIndex": _safe_int(action.get("_executionIndex"), 0),
        "category": "Utility",
        "subcategory": "IndirectContainer",
        "confidence": "confirmed",
        "confidenceScore": 1.0,
        "authority": "FixedProgram",
        "semanticVerified": False,
        "evidence": [
            _evidence(
                "actionFlags",
                "multi-action-container",
                "ExecuteIndirect/MultiAction parent is represented as a foldable container",
                1.0,
            )
        ],
        "containerOnly": True,
        "indirect": True,
        "actualWorkState": "not-established",
        "participation": {
            "issuedAction": False,
            "nonZeroWorkload": None,
            "outputAffectingCandidate": False,
            "observedContributor": False,
            "containerOnly": True,
            "contributorObservation": (
                "not-tested; the container owns no concrete draw/dispatch workload"
            ),
            "concreteChildrenOwnWork": True,
            "indirectArgumentsResolved": False,
        },
    }


def _tree_id(*parts: Any) -> str:
    label = ":".join(str(part) for part in parts)
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:20]


def _marker_breadcrumb(marker_path: Sequence[Any]) -> str:
    parts = [str(value) for value in marker_path if str(value)]
    joined = " / ".join(parts)
    if len(joined) <= MAX_TREE_GROUP_LABEL:
        return joined
    # Keep both the owning marker and the leaf marker visible. The complete
    # captured path remains in markerPath/markerEventPath and in the identity.
    prefix = joined[: MAX_TREE_GROUP_LABEL // 2 - 3].rstrip()
    suffix = joined[-(MAX_TREE_GROUP_LABEL // 2 - 4) :].lstrip()
    return "%s … %s" % (prefix, suffix)


def _semantic_group_identity(item: Mapping[str, Any]) -> tuple[str, str, str]:
    marker_path = list(item.get("markerPath", []))
    marker_events = [
        dict(value)
        for value in item.get("markerEventPath", [])
        if isinstance(value, Mapping)
    ]
    if item.get("indirect"):
        group_kind = "indirect-container"
        parent_event_id = _safe_int(item.get("parentEventId"), 0)
        indirect_label = str(
            item.get("indirectContainerName") or "ExecuteIndirect"
        )
        group_label = (
            _marker_breadcrumb(marker_path)
            if marker_path
            else indirect_label[:MAX_TREE_GROUP_LABEL]
        )
        identity = {
            "kind": group_kind,
            "parentEventId": parent_event_id,
            "standaloneEventId": (
                0 if parent_event_id else _safe_int(item.get("eventId"), 0)
            ),
            "markerEvents": marker_events,
            "markerPath": marker_path,
            "label": group_label,
        }
    elif marker_path:
        group_kind = "marker"
        group_label = _marker_breadcrumb(marker_path)
        identity = {
            "kind": group_kind,
            "markerEvents": marker_events,
            "markerPath": marker_path,
        }
    else:
        group_kind = "ungrouped"
        group_label = "Ungrouped"
        identity = {"kind": group_kind}
    return group_kind, group_label, _canonical_sha256(identity)


def _build_tree(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[
        str,
        dict[str, dict[tuple[str, str, str, int], list[Mapping[str, Any]]]],
    ] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    previous_identity: tuple[str, str, str] | None = None
    occurrence = 0
    for item in sorted(
        items,
        key=lambda value: (
            _safe_int(value.get("executionIndex"), 0),
            _safe_int(value.get("eventId"), 0),
        ),
    ):
        group_kind, group_label, identity = _semantic_group_identity(item)
        current_identity = (group_kind, group_label, identity)
        if current_identity != previous_identity:
            occurrence += 1
            previous_identity = current_identity
        grouped[str(item["category"])][str(item["subcategory"])][
            (group_kind, group_label, identity, occurrence)
        ].append(item)

    tree: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        if category not in grouped:
            continue
        category_node = {
            "id": _tree_id("category", category),
            "nodeType": "category",
            "label": category,
            "count": 0,
            "children": [],
        }
        for subcategory in sorted(grouped[category]):
            subcategory_node = {
                "id": _tree_id("subcategory", category, subcategory),
                "nodeType": "subcategory",
                "label": subcategory,
                "count": 0,
                "children": [],
            }
            groups = grouped[category][subcategory]
            for (group_kind, group_label, identity, group_occurrence), group_items in sorted(
                groups.items(),
                key=lambda pair: min(
                    (
                        _safe_int(value.get("executionIndex"), 0),
                        _safe_int(value.get("eventId"), 0),
                    )
                    for value in pair[1]
                ),
            ):
                leaves = [
                    {
                        "id": _tree_id("action", item["eventId"]),
                        "nodeType": "action",
                        "eventId": item["eventId"],
                        "label": item["name"],
                        "confidence": item["confidence"],
                        "indirect": item.get("indirect", False),
                    }
                    for item in sorted(
                        group_items,
                        key=lambda value: (
                            _safe_int(value.get("executionIndex"), 0),
                            value["eventId"],
                        ),
                    )
                ]
                group_node = {
                    "id": _tree_id(
                        "group",
                        category,
                        subcategory,
                        group_kind,
                        identity,
                        group_occurrence,
                    ),
                    "nodeType": "container"
                    if group_kind == "indirect-container"
                    else "markerGroup",
                    "groupKind": group_kind,
                    "label": group_label,
                    "count": len(leaves),
                    "foldByDefault": (
                        group_kind == "indirect-container"
                        or category in ("Utility", "Unknown")
                        or len(leaves) >= DEFAULT_FOLDED_GROUP_SIZE
                    ),
                    "children": leaves,
                }
                subcategory_node["children"].append(group_node)
                subcategory_node["count"] += len(leaves)
            category_node["children"].append(subcategory_node)
            category_node["count"] += subcategory_node["count"]
        tree.append(category_node)
    return tree


def _resource_range(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for field in (
        "byteOffset", "byteSize", "firstMip", "numMips",
        "firstSlice", "numSlices",
    ):
        number = _safe_int(value.get(field), -1)
        if number >= 0:
            result[field] = number
    if isinstance(value.get("subresource"), Mapping):
        result["subresource"] = dict(value["subresource"])
    return result


def _resource_range_token(value: Mapping[str, Any]) -> str:
    return json.dumps(
        _resource_range(value), sort_keys=True, separators=(",", ":")
    )


def _resource_range_overlap(
    producer: Mapping[str, Any], consumer: Mapping[str, Any]
) -> str:
    producer_subresource = producer.get("subresource")
    consumer_subresource = consumer.get("subresource")
    if isinstance(producer_subresource, Mapping) and isinstance(
        consumer_subresource, Mapping
    ):
        fields = ("mip", "slice", "sample", "plane")
        producer_fields = {
            field for field in fields if field in producer_subresource
        }
        consumer_fields = {
            field for field in fields if field in consumer_subresource
        }
        for field in producer_fields & consumer_fields:
            if _safe_int(producer_subresource[field], -1) != _safe_int(
                consumer_subresource[field], -1
            ):
                return "disjoint"
        # An exact subresource handoff is established only when both sides
        # describe the same complete coordinate set. A partial coordinate is
        # useful evidence, but cannot prove compatible mip/slice/sample use.
        producer_complete = _complete_subresource(producer)
        consumer_complete = _complete_subresource(consumer)
        if producer_complete is not None and consumer_complete is not None:
            return (
                "overlap"
                if producer_complete == consumer_complete
                else "disjoint"
            )

    producer_size = _safe_int(producer.get("byteSize"), 0)
    consumer_size = _safe_int(consumer.get("byteSize"), 0)
    if producer_size > 0 and consumer_size > 0:
        producer_start = _safe_int(producer.get("byteOffset"), 0)
        consumer_start = _safe_int(consumer.get("byteOffset"), 0)
        if (
            producer_start + producer_size <= consumer_start
            or consumer_start + consumer_size <= producer_start
        ):
            return "disjoint"
        return "overlap"

    if all(
        _safe_int(value.get(field), -1) >= (1 if field.startswith("num") else 0)
        for value in (producer, consumer)
        for field in ("firstMip", "numMips", "firstSlice", "numSlices")
    ):
        producer_mip = _safe_int(producer.get("firstMip"), 0)
        consumer_mip = _safe_int(consumer.get("firstMip"), 0)
        producer_slice = _safe_int(producer.get("firstSlice"), 0)
        consumer_slice = _safe_int(consumer.get("firstSlice"), 0)
        mip_overlap = not (
            producer_mip + _safe_int(producer.get("numMips"), 0)
            <= consumer_mip
            or consumer_mip + _safe_int(consumer.get("numMips"), 0)
            <= producer_mip
        )
        slice_overlap = not (
            producer_slice + _safe_int(producer.get("numSlices"), 0)
            <= consumer_slice
            or consumer_slice + _safe_int(consumer.get("numSlices"), 0)
            <= producer_slice
        )
        return "overlap" if mip_overlap and slice_overlap else "disjoint"
    return "unknown"


def _complete_subresource(value: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    subresource = value.get("subresource")
    fields = ("mip", "slice", "sample", "plane")
    if not isinstance(subresource, Mapping) or not all(
        field in subresource for field in fields
    ):
        return None
    coordinates = tuple(_safe_int(subresource.get(field), -1) for field in fields)
    if any(coordinate < 0 for coordinate in coordinates):
        return None
    return coordinates


def _byte_interval(value: Mapping[str, Any]) -> tuple[int, int] | None:
    byte_offset = _safe_int(value.get("byteOffset"), -1)
    byte_size = _safe_int(value.get("byteSize"), 0)
    if byte_offset < 0 or byte_size <= 0:
        return None
    return byte_offset, byte_offset + byte_size


def _texture_rectangle(
    value: Mapping[str, Any],
) -> tuple[int, int, int, int] | None:
    first_mip = _safe_int(value.get("firstMip"), -1)
    num_mips = _safe_int(value.get("numMips"), 0)
    first_slice = _safe_int(value.get("firstSlice"), -1)
    num_slices = _safe_int(value.get("numSlices"), 0)
    if first_mip < 0 or num_mips <= 0 or first_slice < 0 or num_slices <= 0:
        return None
    return (
        first_mip,
        first_mip + num_mips,
        first_slice,
        first_slice + num_slices,
    )


def _with_byte_interval(
    value: Mapping[str, Any], start: int, end: int
) -> dict[str, Any]:
    result = dict(value)
    result["byteOffset"] = start
    result["byteSize"] = end - start
    return result


def _with_texture_rectangle(
    value: Mapping[str, Any],
    first_mip: int,
    mip_end: int,
    first_slice: int,
    slice_end: int,
) -> dict[str, Any]:
    result = dict(value)
    result["firstMip"] = first_mip
    result["numMips"] = mip_end - first_mip
    result["firstSlice"] = first_slice
    result["numSlices"] = slice_end - first_slice
    return result


def _subtract_terminal_write(
    older: Mapping[str, Any], newer: Mapping[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """Subtract a later known write range from an older terminal candidate.

    The status is ``disjoint``, ``covered``, ``partial``, or ``unknown``.
    Unknown never discards either writer.
    """

    older_subresource = older.get("subresource")
    newer_subresource = newer.get("subresource")
    if isinstance(older_subresource, Mapping) or isinstance(
        newer_subresource, Mapping
    ):
        if not (
            isinstance(older_subresource, Mapping)
            and isinstance(newer_subresource, Mapping)
        ):
            return "unknown", [dict(older)]
        common_fields = set(older_subresource) & set(newer_subresource) & {
            "mip", "slice", "sample", "plane"
        }
        if any(
            _safe_int(older_subresource.get(field), -1)
            != _safe_int(newer_subresource.get(field), -1)
            for field in common_fields
        ):
            return "disjoint", [dict(older)]
        older_complete = _complete_subresource(older)
        newer_complete = _complete_subresource(newer)
        if older_complete is not None and older_complete == newer_complete:
            return "covered", []
        return "unknown", [dict(older)]

    older_bytes = _byte_interval(older)
    newer_bytes = _byte_interval(newer)
    if older_bytes is not None or newer_bytes is not None:
        if older_bytes is None or newer_bytes is None:
            return "unknown", [dict(older)]
        older_start, older_end = older_bytes
        newer_start, newer_end = newer_bytes
        if older_end <= newer_start or newer_end <= older_start:
            return "disjoint", [dict(older)]
        residuals = []
        if older_start < newer_start:
            residuals.append(
                _with_byte_interval(older, older_start, min(older_end, newer_start))
            )
        if newer_end < older_end:
            residuals.append(
                _with_byte_interval(older, max(older_start, newer_end), older_end)
            )
        return ("partial", residuals) if residuals else ("covered", [])

    older_rectangle = _texture_rectangle(older)
    newer_rectangle = _texture_rectangle(newer)
    if older_rectangle is not None or newer_rectangle is not None:
        if older_rectangle is None or newer_rectangle is None:
            return "unknown", [dict(older)]
        om0, om1, os0, os1 = older_rectangle
        nm0, nm1, ns0, ns1 = newer_rectangle
        im0, im1 = max(om0, nm0), min(om1, nm1)
        is0, is1 = max(os0, ns0), min(os1, ns1)
        if im0 >= im1 or is0 >= is1:
            return "disjoint", [dict(older)]
        residuals = []
        if om0 < im0:
            residuals.append(_with_texture_rectangle(older, om0, im0, os0, os1))
        if im1 < om1:
            residuals.append(_with_texture_rectangle(older, im1, om1, os0, os1))
        if os0 < is0:
            residuals.append(_with_texture_rectangle(older, im0, im1, os0, is0))
        if is1 < os1:
            residuals.append(_with_texture_rectangle(older, im0, im1, is1, os1))
        return ("partial", residuals) if residuals else ("covered", [])

    return "unknown", [dict(older)]


def _terminal_writer_fragments(
    history: Sequence[tuple[int, Mapping[str, Any], bool]],
) -> tuple[list[dict[str, Any]], bool]:
    fragments: list[dict[str, Any]] = []
    truncated = False
    for event_id, resource, possible in history:
        retained: list[dict[str, Any]] = []
        new_uncertain = False
        new_range_uncertain = False
        if possible:
            # A possible write cannot erase an earlier terminal writer: either
            # may own the final contents.  It only makes overlapping/unknown
            # fragments candidates until a later definite write supersedes
            # them.
            for fragment in fragments:
                relation, unused_residuals = _subtract_terminal_write(
                    fragment["resource"], resource
                )
                del unused_residuals
                retained_fragment = dict(fragment)
                if relation != "disjoint":
                    retained_fragment["uncertain"] = True
                    retained_fragment["possibleOverwrite"] = True
                    if relation == "unknown":
                        retained_fragment["rangeUncertain"] = True
                retained.append(retained_fragment)
            retained.append(
                {
                    "eventId": event_id,
                    "resource": dict(resource),
                    "uncertain": True,
                    "possible": True,
                    "rangeUncertain": False,
                }
            )
            fragments = retained
            if len(fragments) > MAX_RESOURCE_FLOW_EDGES:
                truncated = True
                fragments = fragments[-MAX_RESOURCE_FLOW_EDGES:]
            continue
        for fragment in fragments:
            relation, residuals = _subtract_terminal_write(
                fragment["resource"], resource
            )
            if relation == "unknown":
                uncertain_fragment = dict(fragment)
                uncertain_fragment["uncertain"] = True
                uncertain_fragment["rangeUncertain"] = True
                retained.append(uncertain_fragment)
                new_uncertain = True
                new_range_uncertain = True
                continue
            for residual in residuals:
                retained.append(
                    {
                        "eventId": fragment["eventId"],
                        "resource": residual,
                        "uncertain": bool(fragment.get("uncertain", False)),
                        "possible": bool(fragment.get("possible", False)),
                        "rangeUncertain": bool(
                            fragment.get("rangeUncertain", False)
                        ),
                        "possibleOverwrite": bool(
                            fragment.get("possibleOverwrite", False)
                        ),
                    }
                )
        retained.append(
            {
                "eventId": event_id,
                "resource": dict(resource),
                "uncertain": new_uncertain,
                "possible": False,
                "rangeUncertain": new_range_uncertain,
            }
        )
        if len(retained) > MAX_RESOURCE_FLOW_EDGES:
            truncated = True
            # Always retain the newest writer; older residuals are the bounded
            # portion that can be omitted safely with an explicit receipt.
            retained = retained[-MAX_RESOURCE_FLOW_EDGES:]
        fragments = retained
    return fragments, truncated


def _build_resource_flow(
    items: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    bool,
]:
    writer_history: dict[
        int, list[tuple[int, Mapping[str, Any], bool]]
    ] = defaultdict(list)
    edges: list[dict[str, Any]] = []
    external_inputs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    seen_external: set[tuple[Any, ...]] = set()
    truncated = False
    ordered = sorted(
        items,
        key=lambda value: (
            _safe_int(value.get("executionIndex"), 0),
            _safe_int(value.get("eventId"), 0),
        ),
    )
    for item in ordered:
        event_id = _safe_int(item.get("eventId"), 0)
        for resource in item.get("inputs", []):
            if not isinstance(resource, Mapping):
                continue
            resource_id = _safe_int(resource.get("resourceId"), 0)
            consumer_access = _bounded_text(
                resource.get("access", "read"), "consumer resource access"
            )
            consumer_stage = _bounded_text(
                resource.get("stage", ""), "consumer resource stage"
            )
            consumer_range = _resource_range(resource)
            consumer_range_token = _resource_range_token(resource)
            candidates: list[
                tuple[tuple[int, Mapping[str, Any], bool], str, bool]
            ] = []
            definite_writer_found = False
            intervening_uncertainty = False
            if resource_id:
                # A later write to one mip/slice/range must not hide an older
                # compatible producer for a different subresource. Possible
                # writes and unknown ranges cannot become barriers: retain
                # every plausible producer until a definite overlapping write
                # establishes where the pre-read contents must originate.
                for candidate in reversed(writer_history.get(resource_id, [])):
                    candidate_overlap = _resource_range_overlap(
                        candidate[1], resource
                    )
                    if candidate_overlap == "disjoint":
                        continue
                    candidates.append(
                        (candidate, candidate_overlap, intervening_uncertainty)
                    )
                    if not candidate[2]:
                        definite_writer_found = True
                        if candidate_overlap == "overlap":
                            break
                    intervening_uncertainty = True

            # Emit candidates in execution order so the graph remains stable
            # even when more than one writer can feed the same read.
            for writer, overlap, has_intervening_uncertainty in reversed(candidates):
                if writer[0] == event_id:
                    continue
                producer_access = _bounded_text(
                    writer[1].get("access", "write"),
                    "producer resource access",
                )
                producer_stage = _bounded_text(
                    writer[1].get("stage", ""), "producer resource stage"
                )
                producer_range = _resource_range(writer[1])
                producer_range_token = _resource_range_token(writer[1])
                key = (
                    writer[0],
                    event_id,
                    resource_id,
                    producer_access,
                    producer_stage,
                    consumer_access,
                    consumer_stage,
                    producer_range_token,
                    consumer_range_token,
                    writer[2],
                )
                if key in seen:
                    continue
                if len(edges) >= MAX_RESOURCE_FLOW_EDGES:
                    truncated = True
                    continue
                seen.add(key)
                resource_name = _bounded_text(
                    resource.get("name", writer[1].get("name", "")),
                    "resource flow name",
                )
                edge_is_candidate = bool(
                    writer[2]
                    or overlap != "overlap"
                    or has_intervening_uncertainty
                )
                edge = {
                    "producerEid": writer[0],
                    "consumerEid": event_id,
                    "resourceId": resource_id,
                    "handoff": "write-to-read",
                    "producerAccess": producer_access,
                    "consumerAccess": consumer_access,
                    "producerWriteCertainty": (
                        "possible" if writer[2] else "definite"
                    ),
                    "subresourceOverlap": overlap,
                    "authority": (
                        "FixedProgramCandidate"
                        if edge_is_candidate
                        else "FixedProgram"
                    ),
                }
                if has_intervening_uncertainty:
                    edge["interveningWriteUncertain"] = True
                if producer_stage:
                    edge["producerStage"] = producer_stage
                if consumer_stage:
                    edge["consumerStage"] = consumer_stage
                if producer_range:
                    edge["producerRange"] = producer_range
                if consumer_range:
                    edge["consumerRange"] = consumer_range
                if resource_name:
                    edge["resourceName"] = resource_name
                edges.append(edge)

            # With no compatible definite writer, possible writes leave the
            # pre-frame resource as another valid candidate source.
            if not definite_writer_found:
                boundary_key = (
                    event_id,
                    resource_id,
                    consumer_access,
                    consumer_stage,
                    consumer_range_token,
                )
                if resource_id and boundary_key not in seen_external:
                    seen_external.add(boundary_key)
                    if len(external_inputs) >= MAX_RESOURCE_FLOW_EDGES:
                        truncated = True
                    else:
                        boundary = {
                            "consumerEid": event_id,
                            "resourceId": resource_id,
                            "access": consumer_access,
                            "authority": (
                                "FixedProgramCandidate"
                                if candidates
                                else "FixedProgram"
                            ),
                        }
                        if consumer_stage:
                            boundary["stage"] = consumer_stage
                        if consumer_range:
                            boundary["consumerRange"] = consumer_range
                        resource_name = _bounded_text(
                            resource.get("name", ""),
                            "external input resource name",
                        )
                        if resource_name:
                            boundary["resourceName"] = resource_name
                        external_inputs.append(boundary)
        definite_ranges: set[tuple[int, str, str, str]] = set()
        for resource in item.get("outputs", []):
            if not isinstance(resource, Mapping):
                continue
            resource_id = _safe_int(resource.get("resourceId"), 0)
            if resource_id:
                writer = (event_id, resource, False)
                writer_history[resource_id].append(writer)
                definite_ranges.add(
                    (
                        resource_id,
                        _resource_range_token(resource),
                        str(resource.get("access", "")),
                        str(resource.get("stage", "")),
                    )
                )
        for resource in item.get("possibleOutputs", []):
            if not isinstance(resource, Mapping):
                continue
            resource_id = _safe_int(resource.get("resourceId"), 0)
            if not resource_id:
                continue
            writer_identity = (
                resource_id,
                _resource_range_token(resource),
                str(resource.get("access", "")),
                str(resource.get("stage", "")),
            )
            if writer_identity in definite_ranges:
                continue
            writer_history[resource_id].append((event_id, resource, True))
    terminal_candidates: list[
        tuple[int, int, Mapping[str, Any], bool, bool, bool, bool]
    ] = []
    for resource_id, history in sorted(writer_history.items()):
        fragments, fragments_truncated = _terminal_writer_fragments(history)
        truncated = truncated or fragments_truncated
        for fragment in fragments:
            terminal_candidates.append(
                (
                    _safe_int(fragment.get("eventId"), 0),
                    resource_id,
                    fragment.get("resource", {}),
                    bool(fragment.get("uncertain", False)),
                    bool(fragment.get("possible", False)),
                    bool(fragment.get("rangeUncertain", False)),
                    bool(fragment.get("possibleOverwrite", False)),
                )
            )
    terminal_candidates.sort(
        key=lambda value: (
            value[0],
            value[1],
            json.dumps(
                _resource_range(value[2]),
                sort_keys=True,
                separators=(",", ":"),
            ),
            str(value[2].get("access", "")),
            str(value[2].get("stage", "")),
            value[4],
        )
    )
    terminal_outputs: list[dict[str, Any]] = []
    for (
        event_id,
        resource_id,
        resource,
        uncertain,
        possible,
        range_uncertain,
        possible_overwrite,
    ) in terminal_candidates:
        if len(terminal_outputs) >= MAX_RESOURCE_FLOW_EDGES:
            truncated = True
            break
        terminal = {
            "producerEid": event_id,
            "resourceId": resource_id,
            "access": _bounded_text(
                resource.get("access", "write"), "terminal resource access"
            ),
            "writeCertainty": "possible" if possible else "definite",
            "authority": (
                "FixedProgramCandidate"
                if uncertain or possible
                else "FixedProgram"
            ),
        }
        resource_range = _resource_range(resource)
        if resource_range:
            terminal["range"] = resource_range
        if range_uncertain:
            terminal["rangeRelationship"] = "unknown"
        if possible_overwrite:
            terminal["possibleOverwrite"] = True
        stage = _bounded_text(
            resource.get("stage", ""), "terminal resource stage"
        )
        if stage:
            terminal["stage"] = stage
        name = _bounded_text(
            resource.get("name", ""), "terminal output resource name"
        )
        if name:
            terminal["resourceName"] = name
        terminal_outputs.append(terminal)
    return edges, external_inputs, terminal_outputs, truncated


def apply_ai_semantic_proposals(
    classification: Mapping[str, Any], proposals: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Attach review-only semantic proposals without changing the fixed tree."""

    if classification.get("schema") != CLASSIFICATION_SCHEMA:
        raise PassClassificationError("classification schema is unsupported")
    result = deepcopy(dict(classification))
    if not proposals:
        result["provisionalOverlayCount"] = 0
        return result
    if not isinstance(proposals, (list, tuple)) or len(proposals) > MAX_PROPOSALS:
        raise PassClassificationError("AI proposals must be a bounded list")
    items = {
        _safe_int(item.get("eventId"), -1): item
        for item in result.get("items", [])
        if isinstance(item, dict)
    }
    seen: set[int] = set()
    count = 0
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            raise PassClassificationError(f"AI proposal {index} must be an object")
        event_id = _safe_int(proposal.get("eventId"), -1)
        if event_id not in items:
            raise PassClassificationError(
                f"AI proposal references unknown eventId {event_id}"
            )
        if event_id in seen:
            raise PassClassificationError(
                f"AI proposals contain duplicate eventId {event_id}"
            )
        seen.add(event_id)
        proposed_category = _bounded_text(
            proposal.get("category", ""), "AI proposal category"
        )
        if proposed_category and proposed_category not in CATEGORIES:
            raise PassClassificationError("AI proposal category is unsupported")
        overlay = {
            "authority": "AIProvisional",
            "verified": False,
            "applied": False,
            "reviewRequired": True,
            "physicalEvidence": False,
            "proposedCategory": proposed_category,
            "proposedSubcategory": _bounded_text(
                proposal.get("subcategory", ""), "AI proposal subcategory"
            ),
            "displayLabel": _bounded_text(
                proposal.get("displayLabel", proposal.get("label", "")),
                "AI proposal displayLabel",
            ),
            "rationale": _bounded_text(
                proposal.get("rationale", ""), "AI proposal rationale"
            ),
        }
        items[event_id]["semanticProposal"] = overlay
        count += 1
    result["provisionalOverlayCount"] = count
    result["policy"]["aiCanChangeFixedTree"] = False
    result["policy"]["aiProposalAuthority"] = "AIProvisional"
    return result


def _compact_resource_metadata_receipt(value: Any) -> dict[str, Any]:
    """Project only the fixed, bounded bridge metadata-budget receipt."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in (
        "sourceRecordCount",
        "retainedEntryCount",
        "omittedSourceRecordCount",
        "recordLimit",
    ):
        if field not in value:
            continue
        number = _safe_int(value.get(field), -1)
        if 0 <= number <= 0x7FFFFFFF:
            result[field] = number
    for field in ("byteBudget", "budgetedBytes"):
        if field not in value:
            continue
        number = _safe_int(value.get(field), -1)
        if 0 <= number <= 0xFFFFFFFFFFFFFFFF:
            result[field] = number
    if "truncated" in value:
        truncated = _optional_bool(value.get("truncated"))
        if truncated is not None:
            result["truncated"] = truncated
    return result


def classify_passes(
    actions: Any,
    *,
    pipeline_states: Any = None,
    shader_catalog: Mapping[str, Any] | None = None,
    project_knowledge: Mapping[str, Any] | None = None,
    ai_proposals: Sequence[Mapping[str, Any]] | None = None,
    capture_id: str = "",
) -> dict[str, Any]:
    """Classify RenderDoc actions into a deterministic, foldable pass tree.

    ``actions`` accepts either the list returned by the bridge or the complete
    ``get_draw_calls`` result.  ``pipeline_states`` may be a list or a mapping
    keyed by EventId.  Missing evidence degrades confidence; it is never
    invented.
    """

    capture_digest = _sha(capture_id)
    if capture_id and not capture_digest:
        raise PassClassificationError("capture_id must be a lowercase SHA-256")
    input_summary = actions.get("summary", {}) if isinstance(actions, Mapping) else {}
    input_truncated = bool(
        isinstance(input_summary, Mapping) and input_summary.get("truncated") is True
    )
    normalised, duplicate_count = _normalise_actions(actions)
    pipelines = _event_index(pipeline_states)
    uses_by_event, _ = _catalog_index(shader_catalog)
    project_records = _knowledge_records(project_knowledge)

    items: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for execution_index, action in enumerate(normalised):
        action["_executionIndex"] = _safe_int(
            action.get("_executionIndex"), execution_index
        )
        if _safe_int(action.get("eventId"), 0) <= 0:
            omitted.append(
                {
                    "eventId": 0,
                    "name": action["name"],
                    "reason": "non-positive-event-context",
                    "executionIndex": action["_executionIndex"],
                }
            )
            continue
        kind = _action_kind(action)
        is_indirect_container = kind == "container" or (
            action.get("_hasChildren")
            and _is_indirect(action)
            and kind not in ("graphics", "compute")
        )
        if is_indirect_container:
            containers.append(_container_record(action))
            continue
        if kind == "marker":
            omitted.append(
                {
                    "eventId": action["eventId"],
                    "name": action["name"],
                    "reason": "marker-used-as-tree-context",
                    "executionIndex": action["_executionIndex"],
                }
            )
            continue
        feature = _action_feature(
            action,
            pipelines.get(action["eventId"], {}),
            uses_by_event.get(action["eventId"], []),
        )
        items.append(_classify_feature(feature, project_records))

    timeline_items = sorted(
        items,
        key=lambda value: (
            _safe_int(value.get("executionIndex"), 0),
            _safe_int(value.get("eventId"), 0),
        ),
    )
    execution_timeline = [
        action["eventId"]
        for action in sorted(
            normalised,
            key=lambda value: (
                _safe_int(value.get("_executionIndex"), 0),
                _safe_int(value.get("eventId"), 0),
            ),
        )
        if _safe_int(action.get("eventId"), 0) > 0
    ]
    (
        resource_flow,
        external_inputs,
        terminal_outputs,
        resource_flow_truncated,
    ) = _build_resource_flow(timeline_items)
    source_omitted = max(
        0,
        _safe_int(
            input_summary.get(
                "omittedActionCountLowerBound",
                input_summary.get("omittedActionCount", 0),
            )
            if isinstance(input_summary, Mapping)
            else 0,
            0,
        ),
    )
    source_reasons = []
    if isinstance(input_summary, Mapping):
        raw_reasons = input_summary.get("truncationReasons", [])
        if isinstance(raw_reasons, (list, tuple)):
            source_reasons = [
                _bounded_text(value, "truncation reason")
                for value in raw_reasons[:16]
                if value
            ]
    input_resource_omitted = sum(
        max(0, _safe_int(item.get("inputOmittedCount"), 0)) for item in items
    )
    output_resource_omitted = sum(
        max(0, _safe_int(item.get("outputOmittedCount"), 0)) for item in items
    )
    resource_metadata_receipt = _compact_resource_metadata_receipt(
        input_summary.get("resourceMetadata")
        if isinstance(input_summary, Mapping)
        else None
    )
    source_resource_facts_truncated = bool(
        isinstance(input_summary, Mapping)
        and input_summary.get("resourceFactsTruncated") is True
    )
    resource_facts_truncated = bool(
        input_resource_omitted
        or output_resource_omitted
        or source_resource_facts_truncated
        or resource_metadata_receipt.get("truncated") is True
    )
    result: dict[str, Any] = {
        "schema": CLASSIFICATION_SCHEMA,
        "schemaVersion": CLASSIFICATION_VERSION,
        "captureId": capture_digest,
        "generatedAt": _utc_now(),
        "items": items,
        "containers": containers,
        "tree": _build_tree(items),
        "omittedEvents": omitted,
        "summary": {
            "inputCount": len(normalised),
            "actionCount": len(items),
            "containerCount": len(containers),
            "markerContextCount": len(omitted),
            "duplicateCount": duplicate_count,
            "categoryCounts": dict(Counter(item["category"] for item in items)),
            "actualWorkCounts": dict(
                Counter(item["actualWorkState"] for item in items)
            ),
            "executionTimeline": execution_timeline,
            "actionExecutionTimeline": [
                item["eventId"]
                for item in timeline_items
                if item.get("actionKind") in ("graphics", "compute", "utility")
            ],
            "resourceFlowEdges": resource_flow,
            "externalInputs": external_inputs,
            "terminalOutputs": terminal_outputs,
            "resourceFlowTruncated": resource_flow_truncated,
            "resourceFlowSubresourcesResolved": False,
            "sourceTruncated": input_truncated,
            "truncated": input_truncated,
            "omittedActionCountLowerBound": source_omitted,
            "truncationReasons": source_reasons,
            "requestedMaxActions": (
                _safe_int(input_summary.get("requestedMaxActions"), len(normalised))
                if isinstance(input_summary, Mapping)
                else len(normalised)
            ),
            "serializedBudgetBytes": (
                _safe_int(input_summary.get("serializedBudgetBytes"), 0)
                if isinstance(input_summary, Mapping)
                else 0
            ),
            "sourceSerializedBytes": (
                _safe_int(input_summary.get("serializedBytes"), 0)
                if isinstance(input_summary, Mapping)
                else 0
            ),
            "inputResourceOmittedCount": input_resource_omitted,
            "outputResourceOmittedCount": output_resource_omitted,
            "resourceFactsTruncated": resource_facts_truncated,
        },
        "policy": {
            "classificationAuthority": "FixedProgram",
            "aiCanChangeFixedTree": False,
            "aiProposalAuthority": "AIProvisional",
            "eventIdsAreCaptureLocal": True,
            "resourceIdsAreCaptureLocal": True,
            "projectReuseRequiresExactPassFingerprint": True,
            "executeIndirectParentsAreContainers": True,
            "concreteIndirectChildrenRemainVisible": True,
        },
    }
    if resource_metadata_receipt:
        result["summary"]["resourceMetadata"] = resource_metadata_receipt
    return apply_ai_semantic_proposals(result, ai_proposals)


_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _operation_lock(root: Path) -> Iterator[None]:
    path = root / ".pass-knowledge-operation.lock"
    token = uuid.uuid4().hex
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except FileExistsError as exc:
        raise PassClassificationError(
            "pass knowledge is locked; a stale lock must be reviewed manually"
        ) from exc
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if path.read_text(encoding="ascii") == token:
                path.unlink()
        except OSError:
            pass


class PassKnowledgeStore:
    """Content-bound pass observations stored beside a ReplayProjectService project.

    The store is intentionally a separate artifact so the current project v1
    manifest does not need a backwards-incompatible key.  It validates the
    owning project ID/scope on every read and never promotes AI overlays into
    reusable records.
    """

    def __init__(self, project_path: str | Path):
        self.root = Path(project_path).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / "knowledge" / "passes" / "pass_knowledge.v1.json"

    def _project(self) -> dict[str, Any]:
        manifest = self.root / "project.json"
        if (
            self.root.is_symlink()
            or not self.root.is_dir()
            or manifest.is_symlink()
            or not manifest.is_file()
        ):
            raise PassClassificationError(
                "project path must contain a regular project.json"
            )
        if manifest.stat().st_size <= 0 or manifest.stat().st_size > MAX_PROJECT_BYTES:
            raise PassClassificationError("project manifest size is invalid")
        try:
            value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PassClassificationError(
                "project manifest is not valid UTF-8 JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != "renderdoc-ultra-replay-project/v1"
            or value.get("schemaVersion") != 1
        ):
            raise PassClassificationError(
                "unsupported Bread project schema/version"
            )
        project_id = str(value.get("projectId", ""))
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise PassClassificationError("projectId is invalid")
        if not isinstance(value.get("scope"), dict) or not isinstance(
            value.get("captures"), list
        ):
            raise PassClassificationError("project scope/captures are invalid")
        return value

    @staticmethod
    def _empty(project: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": KNOWLEDGE_SCHEMA,
            "schemaVersion": KNOWLEDGE_VERSION,
            "projectId": project["projectId"],
            "scopeSha256": _canonical_sha256(project["scope"]),
            "updatedAt": _utc_now(),
            "captureObservations": [],
            "records": [],
            "provisionalOverlays": [],
            "policy": {
                "eventIdsReusableAcrossFrames": False,
                "resourceIdsReusableAcrossFrames": False,
                "exactPassFingerprintRequired": True,
                "aiProposalsReusableAuthority": False,
                "aiProposalsApplied": False,
            },
        }

    def load(self) -> dict[str, Any]:
        project = self._project()
        if not self.path.exists():
            return self._empty(project)
        if self.path.is_symlink() or not self.path.is_file():
            raise PassClassificationError(
                "pass knowledge must be a regular non-symlink file"
            )
        if (
            self.path.stat().st_size <= 0
            or self.path.stat().st_size > MAX_KNOWLEDGE_BYTES
        ):
            raise PassClassificationError("pass knowledge size is invalid")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PassClassificationError(
                "pass knowledge is not valid UTF-8 JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != KNOWLEDGE_SCHEMA
            or value.get("schemaVersion") != KNOWLEDGE_VERSION
            or value.get("projectId") != project["projectId"]
            or value.get("scopeSha256") != _canonical_sha256(project["scope"])
        ):
            raise PassClassificationError(
                "pass knowledge does not belong to this exact project scope"
            )
        for key in ("captureObservations", "records", "provisionalOverlays"):
            if not isinstance(value.get(key), list):
                raise PassClassificationError(f"pass knowledge {key} is invalid")
        # Reuse the classifier validator to ensure an altered record cannot
        # silently become classification authority.
        _knowledge_records(value)
        return value

    @staticmethod
    def _observations(
        classification: Mapping[str, Any], capture_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        overlays: list[dict[str, Any]] = []
        for item in classification.get("items", []):
            if not isinstance(item, Mapping):
                continue
            fingerprint = _sha(item.get("passFingerprint"))
            category = str(item.get("category", ""))
            if not fingerprint or category not in CATEGORIES - {"Unknown"}:
                continue
            grouped[fingerprint].append(item)
            proposal = item.get("semanticProposal")
            if isinstance(proposal, Mapping):
                if (
                    proposal.get("authority") != "AIProvisional"
                    or proposal.get("verified") is not False
                ):
                    raise PassClassificationError(
                        "semantic proposal authority was altered"
                    )
                overlays.append(
                    {
                        "captureId": capture_id,
                        "eventId": _safe_int(item.get("eventId"), 0),
                        "passFingerprint": fingerprint,
                        "authority": "AIProvisional",
                        "verified": False,
                        "applied": False,
                        "reviewRequired": True,
                        "proposedCategory": _bounded_text(
                            proposal.get("proposedCategory", ""), "proposedCategory"
                        ),
                        "proposedSubcategory": _bounded_text(
                            proposal.get("proposedSubcategory", ""),
                            "proposedSubcategory",
                        ),
                        "displayLabel": _bounded_text(
                            proposal.get("displayLabel", ""), "displayLabel"
                        ),
                        "rationale": _bounded_text(
                            proposal.get("rationale", ""), "rationale"
                        ),
                    }
                )
        observations: list[dict[str, Any]] = []
        for fingerprint, items in sorted(grouped.items()):
            categories = Counter(str(item["category"]) for item in items)
            subcategories = Counter(str(item["subcategory"]) for item in items)
            evidence_codes = sorted(
                {
                    str(entry.get("code"))
                    for item in items
                    for entry in item.get("evidence", [])
                    if isinstance(entry, Mapping)
                    and entry.get("authority") == "FixedProgram"
                }
            )
            shader_keys = sorted(
                {
                    f"{shader.get('stage', '')}:{shader.get('rawSha256', '')}"
                    for item in items
                    for shader in item.get("shaders", [])
                    if isinstance(shader, Mapping) and _sha(shader.get("rawSha256"))
                }
            )
            observations.append(
                {
                    "captureId": capture_id,
                    "passFingerprint": fingerprint,
                    "categoryCounts": dict(sorted(categories.items())),
                    "subcategoryCounts": dict(sorted(subcategories.items())),
                    "eventCount": len(items),
                    "shaderKeys": shader_keys,
                    "evidenceCodes": evidence_codes,
                    "authority": "FixedProgram",
                    "semanticVerified": False,
                }
            )
        return observations, overlays

    @staticmethod
    def _records(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for observation in observations:
            fingerprint = _sha(observation.get("passFingerprint"))
            if fingerprint:
                grouped[fingerprint].append(observation)
        records: list[dict[str, Any]] = []
        for fingerprint, values in sorted(grouped.items()):
            category_counts: Counter[str] = Counter()
            subcategory_counts: Counter[str] = Counter()
            evidence_codes: set[str] = set()
            shader_keys: set[str] = set()
            for value in values:
                category_counts.update(
                    {
                        str(key): _safe_int(count, 0)
                        for key, count in dict(value.get("categoryCounts", {})).items()
                        if key in CATEGORIES - {"Unknown"} and _safe_int(count, 0) > 0
                    }
                )
                subcategory_counts.update(
                    {
                        str(key): _safe_int(count, 0)
                        for key, count in dict(
                            value.get("subcategoryCounts", {})
                        ).items()
                        if _safe_int(count, 0) > 0
                    }
                )
                evidence_codes.update(
                    str(item) for item in value.get("evidenceCodes", [])
                )
                shader_keys.update(str(item) for item in value.get("shaderKeys", []))
            reusable = len(category_counts) == 1
            category = next(iter(category_counts)) if reusable else "Unknown"
            if reusable and subcategory_counts:
                matching = [(name, count) for name, count in subcategory_counts.items()]
                subcategory = min(matching, key=lambda pair: (-pair[1], pair[0]))[0]
            else:
                subcategory = "Conflicting" if not reusable else "Unclassified"
            records.append(
                {
                    "passFingerprint": fingerprint,
                    "category": category,
                    "subcategory": subcategory,
                    "categoryCounts": dict(sorted(category_counts.items())),
                    "captureCount": len(
                        {str(item.get("captureId")) for item in values}
                    ),
                    "observationCount": sum(
                        _safe_int(item.get("eventCount"), 0) for item in values
                    ),
                    "shaderKeys": sorted(shader_keys),
                    "evidenceCodes": sorted(evidence_codes),
                    "authority": "FixedProgram",
                    "semanticVerified": False,
                    "reusable": reusable,
                    "conflict": not reusable,
                }
            )
        return records

    def save(
        self,
        classification: Mapping[str, Any],
        *,
        capture_id: str = "",
    ) -> dict[str, Any]:
        if (
            classification.get("schema") != CLASSIFICATION_SCHEMA
            or classification.get("schemaVersion") != CLASSIFICATION_VERSION
        ):
            raise PassClassificationError(
                "classification must come from classify_passes"
            )
        summary = classification.get("summary")
        if isinstance(summary, Mapping) and (
            summary.get("sourceTruncated") is True
            or summary.get("truncated") is True
        ):
            raise PassClassificationError(
                "truncated pass classification cannot become project knowledge"
            )
        digest = _sha(capture_id or classification.get("captureId"))
        if not digest:
            raise PassClassificationError("capture_id must be a lowercase SHA-256")
        project = self._project()
        imported = {
            str(item.get("captureId", ""))
            for item in project.get("captures", [])
            if isinstance(item, Mapping)
        }
        if digest not in imported:
            raise PassClassificationError(
                "capture_id is not imported into this project"
            )
        observations, overlays = self._observations(classification, digest)
        with _store_lock(self.root), _operation_lock(self.root):
            value = self.load()
            value["captureObservations"] = [
                item
                for item in value["captureObservations"]
                if item.get("captureId") != digest
            ] + observations
            value["captureObservations"].sort(
                key=lambda item: (
                    item.get("passFingerprint", ""),
                    item.get("captureId", ""),
                )
            )
            value["provisionalOverlays"] = [
                item
                for item in value["provisionalOverlays"]
                if item.get("captureId") != digest
            ] + overlays
            value["provisionalOverlays"].sort(
                key=lambda item: (
                    item.get("captureId", ""),
                    _safe_int(item.get("eventId"), 0),
                )
            )
            value["records"] = self._records(value["captureObservations"])
            value["updatedAt"] = _utc_now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, value)
        return {
            "schema": KNOWLEDGE_SCHEMA,
            "schemaVersion": KNOWLEDGE_VERSION,
            "projectId": value["projectId"],
            "path": str(self.path),
            "captureId": digest,
            "captureObservationCount": len(observations),
            "recordCount": len(value["records"]),
            "reusableRecordCount": sum(
                1 for item in value["records"] if item["reusable"]
            ),
            "provisionalOverlayCount": len(overlays),
            "aiProposalApplied": False,
        }
