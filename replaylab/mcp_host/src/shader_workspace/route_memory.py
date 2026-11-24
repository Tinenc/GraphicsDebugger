"""Project-level memory of Shader recovery routes that worked (or failed).

Every successful decompile -> repair -> compile -> replay-ABI -> apply chain is
a fact about *this game's* shaders: the same capture encoding, stage and target
profile almost always need the same decompiler lane again.  Today the Host
would re-run the catalog default first (and, for DXIL shaders whose cbuffers
are dynamically indexed, watch it fail) before the AI picks the alternative.
That costs one failed external process and one AI decision per shader.

This module keeps a small JSON dictionary inside the active project::

    shared/dictionaries/shaders/route-memory.json

keyed by the route-relevant shader shape.  The Host consults it while planning
a new workspace and simply promotes the remembered candidate to the primary
route (the full candidate list stays available for fallback), records the
outcome of every decompile and every validated apply, and reports what it did
so the AI and the user can see why a lane was chosen.

The memory never introduces routes: a remembered candidate is used only when
the current fixed route catalog still lists it, so tool/hash policy changes
invalidate it naturally.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import threading
from typing import Any

from .io import atomic_write_json

ROUTE_MEMORY_SCHEMA = "renderdoc-ultra-shader-route-memory/v1"
ROUTE_MEMORY_FILENAME = "route-memory.json"
_MAX_KEYS = 512
_MAX_FAILURES_PER_KEY = 16
_MAX_REASON_CHARS = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def route_key(
    *,
    target_api: str | None,
    capture_encoding: str | None,
    stage: str | None,
    target_profile: str | None,
    editable_format: str | None,
    features: str = "",
) -> str:
    """The shape that decides which decompiler lane works.

    ``features`` is the physical-IR signature from :func:`feature_signature`
    (for DXIL: whether constant buffers are indexed dynamically).  A lane that
    fails on dynamically indexed cbuffers is still the best lane for every
    other shader, so failures and successes are remembered per signature.
    """

    parts = [
        str(target_api or "").strip().lower() or "-",
        str(capture_encoding or "").strip().lower() or "-",
        str(stage or "").strip().lower() or "-",
        str(target_profile or "").strip().lower() or "-",
        str(editable_format or "").strip().lower() or "-",
        str(features or "").strip().lower() or "-",
    ]
    return "|".join(parts)


# DXIL constant-buffer loads: ``@dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %h, i32 <row>)``
# and ``@dx.op.cbufferLoad.*``.  A ``%name`` row operand is a runtime value.
_CBUFFER_LOAD = re.compile(
    r"@dx\.op\.cbufferLoad(?:Legacy)?\.[A-Za-z0-9]+\s*\(\s*i32\s+\d+\s*,\s*%dx\.types\.Handle\s+%[^,]+,\s*i32\s+(?P<row>%?[A-Za-z0-9_.]+)"
)


# Dynamically indexed constant-buffer *handles* (``cb[idx]``): SM6.0-6.5
# ``createHandle(i32 57, i8 2 /*CBuffer*/, i32 rangeId, i32 %idx, i1 nonUniform)`` and SM6.6
# ``createHandleFromBinding(i32 217, %dx.types.ResBind { ..., i8 2 }, i32 %idx, i1 ...)``.
_CBUFFER_HANDLE = re.compile(
    r"@dx\.op\.createHandle\s*\(\s*i32\s+57\s*,\s*i8\s+2\s*,\s*i32\s+\d+\s*,\s*i32\s+(?P<index>%?[A-Za-z0-9_.]+)"
)
_CBUFFER_HANDLE_FROM_BINDING = re.compile(
    r"@dx\.op\.createHandleFromBinding\s*\(\s*i32\s+217\s*,\s*%dx\.types\.ResBind\s*\{[^}]*i8\s+2\s*\}\s*,\s*i32\s+(?P<index>%?[A-Za-z0-9_.]+)"
)


def dxil_route_features(disassembly_text: str) -> dict[str, Any]:
    """Facts about one DXIL module that decide decompiler compatibility."""

    loads = 0
    dynamic_rows = 0
    for match in _CBUFFER_LOAD.finditer(disassembly_text):
        loads += 1
        if match.group("row").startswith("%"):
            dynamic_rows += 1
    handles = 0
    dynamic_handles = 0
    for pattern in (_CBUFFER_HANDLE, _CBUFFER_HANDLE_FROM_BINDING):
        for match in pattern.finditer(disassembly_text):
            handles += 1
            if match.group("index").startswith("%"):
                dynamic_handles += 1
    dynamic = dynamic_rows + dynamic_handles
    return {
        "schema": "renderdoc-ultra-shader-route-features/v1",
        "cbufferLoads": loads,
        "dynamicCBufferLoads": dynamic_rows,
        "cbufferHandles": handles,
        "dynamicCBufferHandles": dynamic_handles,
        "dynamicCBufferIndex": dynamic > 0,
        "signature": "dyncb" if dynamic else ("static" if (loads or handles) else "nocb"),
    }


def feature_signature(features: Mapping[str, Any] | None) -> str:
    if not isinstance(features, Mapping):
        return ""
    return str(features.get("signature") or "")


# Lanes known (from their own error output) to reject a feature: consulted
# like a recorded failure even before this project has seen one.
KNOWN_LANE_INCOMPATIBILITIES: dict[tuple[str, str], str] = {
    ("community-renodx-dxil-hlsl", "dyncb"):
        "RenoDX decomp rejects dynamically indexed constant buffers (\"Unexpected dynamic cbuffer offset\")",
}


def route_key_from_manifest(manifest: Mapping[str, Any]) -> str:
    capture = manifest.get("capture") if isinstance(manifest.get("capture"), Mapping) else {}
    plan = manifest.get("routePlan") if isinstance(manifest.get("routePlan"), Mapping) else {}
    baseline = manifest.get("baseline") if isinstance(manifest.get("baseline"), Mapping) else {}
    return route_key(
        target_api=capture.get("api"),
        capture_encoding=plan.get("inputFormat") or baseline.get("kind"),
        stage=manifest.get("stage"),
        target_profile=manifest.get("targetProfile"),
        editable_format=plan.get("editableFormat"),
        features=feature_signature(manifest.get("routeFeatures")),
    )


def plan_route_ids(plan: Mapping[str, Any] | None, direction: str) -> list[str]:
    """Route IDs of one direction of a bound ``routePlan``."""

    if not isinstance(plan, Mapping):
        return []
    hops = plan.get(direction)
    if not isinstance(hops, list):
        return []
    result: list[str] = []
    for hop in hops:
        capability = hop.get("capability") if isinstance(hop, Mapping) else None
        route_id = capability.get("routeId") if isinstance(capability, Mapping) else None
        if isinstance(route_id, str) and route_id:
            result.append(route_id)
    return result


def _ids_key(route_ids: Sequence[str]) -> str:
    return "+".join(str(item) for item in route_ids)


class ShaderRouteMemory:
    """One JSON file; every method is safe to call when the file is absent."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # -------------------------------------------------------------- storage
    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if not isinstance(payload, dict) or payload.get("schema") != ROUTE_MEMORY_SCHEMA:
            payload = {"schema": ROUTE_MEMORY_SCHEMA, "schemaVersion": 1, "keys": {}}
        if not isinstance(payload.get("keys"), dict):
            payload["keys"] = {}
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        keys = payload["keys"]
        if len(keys) > _MAX_KEYS:
            ordered = sorted(keys.items(), key=lambda item: str(item[1].get("updatedAt", "")))
            for stale, _ in ordered[: len(keys) - _MAX_KEYS]:
                keys.pop(stale, None)
        payload["updatedAt"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, payload)

    def _entry(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        entry = payload["keys"].get(key)
        if not isinstance(entry, dict):
            entry = {
                "key": key,
                "preferred": None,
                "successes": [],
                "failures": {},
                "createdAt": _now(),
                "updatedAt": _now(),
            }
            payload["keys"][key] = entry
        entry.setdefault("successes", [])
        entry.setdefault("failures", {})
        return entry

    # --------------------------------------------------------------- reads
    def lookup(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._load()["keys"].get(key)
        return dict(entry) if isinstance(entry, dict) else None

    def failed_routes(self, key: str) -> dict[str, dict[str, Any]]:
        entry = self.lookup(key)
        failures = entry.get("failures") if entry else None
        return dict(failures) if isinstance(failures, dict) else {}

    # -------------------------------------------------------------- writes
    def record_success(
        self,
        key: str,
        *,
        decompile_route_ids: Sequence[str],
        compile_route_ids: Sequence[str],
        workspace_path: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A validated apply happened through these routes: prefer them next time."""

        decompile_ids = [str(item) for item in decompile_route_ids]
        compile_ids = [str(item) for item in compile_route_ids]
        with self._lock:
            payload = self._load()
            entry = self._entry(payload, key)
            now = _now()
            preferred = entry.get("preferred") if isinstance(entry.get("preferred"), dict) else None
            same = (
                preferred is not None
                and preferred.get("decompileRouteIds") == decompile_ids
                and preferred.get("compileRouteIds") == compile_ids
            )
            entry["preferred"] = {
                "decompileRouteIds": decompile_ids,
                "compileRouteIds": compile_ids,
                "successCount": (int(preferred.get("successCount", 0)) + 1) if same and preferred else 1,
                "firstSuccessAt": preferred.get("firstSuccessAt", now) if same and preferred else now,
                "lastSuccessAt": now,
                "lastWorkspace": str(workspace_path or ""),
                "details": dict(details or {}),
            }
            # A route that later succeeded is no longer a known failure.
            entry["failures"].pop(_ids_key(decompile_ids), None)
            successes = entry["successes"]
            successes.append({"at": now, "decompileRouteIds": decompile_ids, "compileRouteIds": compile_ids})
            del successes[:-8]
            entry["updatedAt"] = now
            self._save(payload)
            return dict(entry)

    def record_decompile_failure(
        self,
        key: str,
        *,
        decompile_route_ids: Sequence[str],
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        """One decompiler lane rejected a shader of this shape."""

        ids = _ids_key([str(item) for item in decompile_route_ids])
        if not ids:
            return {}
        with self._lock:
            payload = self._load()
            entry = self._entry(payload, key)
            now = _now()
            failures = entry["failures"]
            record = failures.get(ids) if isinstance(failures.get(ids), dict) else None
            failures[ids] = {
                "decompileRouteIds": [str(item) for item in decompile_route_ids],
                "count": (int(record.get("count", 0)) + 1) if record else 1,
                "firstAt": record.get("firstAt", now) if record else now,
                "lastAt": now,
                "reason": str(reason or "")[:_MAX_REASON_CHARS],
                "category": str(category or ""),
            }
            if len(failures) > _MAX_FAILURES_PER_KEY:
                for stale in sorted(failures, key=lambda item: str(failures[item].get("lastAt", "")))[
                    : len(failures) - _MAX_FAILURES_PER_KEY
                ]:
                    failures.pop(stale, None)
            entry["updatedAt"] = now
            self._save(payload)
            return dict(entry)

    # ------------------------------------------------------------ planning
    def prefer(self, selection: Mapping[str, Any], key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reorder one toolchain selection according to memory.

        Returns ``(selection, memo)``.  The selection is unchanged unless a
        remembered lane is still one of ``decompileCandidates``; ``memo``
        always explains the decision so callers can surface it.
        """

        result = dict(selection)
        memo: dict[str, Any] = {"schema": ROUTE_MEMORY_SCHEMA, "key": key, "applied": False, "source": "none"}
        entry = self.lookup(key) or {}
        candidates = [
            [str(item) for item in candidate]
            for candidate in (selection.get("decompileCandidates") or [])
            if isinstance(candidate, list) and candidate
        ]
        current = [str(item) for item in (selection.get("decompileRouteIds") or [])]
        failures = dict(entry.get("failures")) if isinstance(entry.get("failures"), dict) else {}
        signature = key.rsplit("|", 1)[-1]
        for candidate in candidates or ([current] if current else []):
            for route_id in candidate:
                reason = KNOWN_LANE_INCOMPATIBILITIES.get((route_id, signature))
                if reason and _ids_key(candidate) not in failures:
                    failures[_ids_key(candidate)] = {"decompileRouteIds": candidate, "count": 0,
                                                     "reason": reason, "category": "known-incompatibility"}
        if not entry and not failures:
            memo["reason"] = "no remembered route for this shader shape yet"
            return result, memo
        preferred = entry.get("preferred") if isinstance(entry.get("preferred"), dict) else None
        memo["knownFailures"] = [
            {"decompileRouteIds": item.get("decompileRouteIds"), "count": item.get("count"), "reason": item.get("reason")}
            for item in failures.values()
            if isinstance(item, dict)
        ]

        chosen: list[str] | None = None
        if preferred:
            wanted = [str(item) for item in (preferred.get("decompileRouteIds") or [])]
            if wanted in candidates or (wanted and wanted == current):
                chosen = wanted
                memo["source"] = "validated-success"
                memo["successCount"] = preferred.get("successCount")
                memo["lastSuccessAt"] = preferred.get("lastSuccessAt")
            else:
                memo["reason"] = "the remembered lane is no longer offered by the route catalog"
        if chosen is None and current and _ids_key(current) in failures:
            for candidate in candidates:
                if _ids_key(candidate) not in failures:
                    chosen = candidate
                    skipped = failures[_ids_key(current)]
                    memo["source"] = ("known-incompatibility" if skipped.get("category") == "known-incompatibility"
                                      else "skip-known-failure")
                    memo["skipped"] = {"decompileRouteIds": current, **skipped}
                    break
            if chosen is None:
                memo["reason"] = "every listed lane has failed before; keeping the catalog default"
        if chosen is None or chosen == current:
            if chosen == current and chosen is not None:
                memo["confirmed"] = True
                memo["reason"] = "the catalog default is also the remembered lane"
            return result, memo
        result["decompileRouteIds"] = list(chosen)
        tool_ids: list[str] = []
        by_route = {}
        for capability in selection.get("capabilities") or []:
            if isinstance(capability, Mapping) and capability.get("routeId"):
                by_route[str(capability["routeId"])] = capability
        for route_id in chosen:
            capability = by_route.get(route_id)
            if isinstance(capability, Mapping) and capability.get("toolId"):
                tool_ids.append(str(capability["toolId"]))
        if len(tool_ids) == len(chosen):
            result["decompilerToolIds"] = tool_ids
        else:
            result.pop("decompilerToolIds", None)
        memo["applied"] = True
        memo["catalogDefault"] = current
        memo["decompileRouteIds"] = list(chosen)
        return result, memo


def memory_path_for_project_dictionary(shaders_dictionary: Path | str) -> Path:
    return Path(shaders_dictionary) / ROUTE_MEMORY_FILENAME


__all__ = [
    "KNOWN_LANE_INCOMPATIBILITIES",
    "ROUTE_MEMORY_FILENAME",
    "dxil_route_features",
    "feature_signature",
    "ROUTE_MEMORY_SCHEMA",
    "ShaderRouteMemory",
    "memory_path_for_project_dictionary",
    "plan_route_ids",
    "route_key",
    "route_key_from_manifest",
]
