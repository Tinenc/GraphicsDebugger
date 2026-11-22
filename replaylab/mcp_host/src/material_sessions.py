"""Deterministic, capture-bound material parameter sessions.

The public MCP layer deliberately accepts only a parameter selector and a
typed JSON value.  Register numbers, byte offsets, scalar encodings and raw
constant-buffer payloads are derived from immutable RenderDoc reflection
evidence and are never caller-controlled.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import struct
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


SNAPSHOT_SCHEMA = "renderdoc-ultra-event-cbuffer-snapshot/v1"
OVERRIDE_SCHEMA = "renderdoc-ultra-event-cbuffer-override/v1"
MATERIAL_SCHEMA = "renderdoc-ultra-material-session/v1"
CLEAR_ALL_SCHEMA = "renderdoc-ultra-event-cbuffer-clear-all/v1"
MAX_MATERIAL_SESSIONS = 128
MAX_MATERIAL_ROLLBACK_TOMBSTONES = 128
MAX_EDITABLE_PARAMETERS = 4096
MAX_CBUFFER_BYTES = 64 * 1024


class MaterialSessionError(RuntimeError):
    """Raised when capture evidence or a requested material edit is unsafe."""


def _hex(value: Any, length: int, name: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != length or any(character not in "0123456789abcdef" for character in text):
        raise MaterialSessionError(
            "%s must be %d lowercase hexadecimal characters" % (name, length)
        )
    return text


def _strict_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MaterialSessionError("%s must be an integer >= %d" % (name, minimum))
    return value


def _decode_base64(value: Any, name: str, maximum: int = MAX_CBUFFER_BYTES) -> bytes:
    if not isinstance(value, str) or not value:
        raise MaterialSessionError("%s must be non-empty base64" % name)
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise MaterialSessionError("%s is not valid canonical base64" % name) from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise MaterialSessionError("%s is not canonical base64" % name)
    if not raw or len(raw) > maximum:
        raise MaterialSessionError(
            "%s must decode to between 1 and %d bytes" % (name, maximum)
        )
    return raw


def _normalise_base_type(value: Any) -> str:
    token = str(value or "").strip().lower().replace("vartype.", "")
    token = token.replace("_", "").replace(" ", "")
    if token in ("float", "float32", "single"):
        return "Float"
    if token in ("sint", "int", "int32", "signedint"):
        return "SInt"
    if token in ("uint", "uint32", "unsignedint"):
        return "UInt"
    raise MaterialSessionError("Unsupported material parameter base type: %s" % value)


def _leaf_identity_payload(
    *,
    path: str,
    base_type: str,
    components: int,
    array_index: int,
    byte_offset: int,
    byte_size: int,
) -> dict:
    return {
        "arrayIndex": array_index,
        "baseType": base_type,
        "byteOffset": byte_offset,
        "byteSize": byte_size,
        "components": components,
        "path": path,
    }


def material_leaf_id(**values: Any) -> str:
    """Return the bridge/host shared deterministic ID for one editable leaf."""

    payload = _leaf_identity_payload(**values)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:32]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _api_token(value: Any) -> str:
    return (
        str(value or "").strip().lower().replace("graphicsapi.", "").replace(
            "direct3d", "d3d")
    )


def _decode_value(raw: bytes, base_type: str, components: int) -> Any:
    formats = {"Float": "f", "SInt": "i", "UInt": "I"}
    values = list(struct.unpack("<" + formats[base_type] * components, raw))
    if base_type == "Float":
        values = [
            (
                "NaN"
                if math.isnan(value)
                else "Infinity"
                if value > 0 and math.isinf(value)
                else "-Infinity"
                if math.isinf(value)
                else value
            )
            for value in values
        ]
    return values[0] if components == 1 else values


def _numeric_components(value: Any, base_type: str, components: int) -> list[Any]:
    if components == 1:
        if isinstance(value, (list, tuple, dict)):
            raise MaterialSessionError("Scalar parameter requires one JSON number")
        values = [value]
    else:
        if not isinstance(value, (list, tuple)) or len(value) != components:
            raise MaterialSessionError(
                "Vector parameter requires exactly %d JSON numbers" % components
            )
        values = list(value)

    checked: list[Any] = []
    if base_type == "Float":
        for component in values:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise MaterialSessionError("Float parameters require JSON numbers")
            converted = float(component)
            if not math.isfinite(converted):
                raise MaterialSessionError("NaN and infinity are not valid material values")
            # struct.pack also rejects values outside finite float32 range.  Decode
            # once after packing below to ensure no finite float64 became infinity.
            checked.append(converted)
    else:
        lower, upper = (
            (-2147483648, 2147483647)
            if base_type == "SInt"
            else (0, 4294967295)
        )
        for component in values:
            if isinstance(component, bool) or not isinstance(component, int):
                raise MaterialSessionError("Integer parameters require JSON integers")
            if component < lower or component > upper:
                raise MaterialSessionError(
                    "%s component is outside the 32-bit range" % base_type
                )
            checked.append(component)
    return checked


def _pack_value(value: Any, base_type: str, components: int) -> bytes:
    values = _numeric_components(value, base_type, components)
    formats = {"Float": "f", "SInt": "i", "UInt": "I"}
    try:
        packed = struct.pack("<" + formats[base_type] * components, *values)
    except (OverflowError, struct.error) as exc:
        raise MaterialSessionError("Value cannot be represented by the reflected type") from exc
    if base_type == "Float" and any(
        not math.isfinite(component)
        for component in struct.unpack("<" + "f" * components, packed)
    ):
        raise MaterialSessionError("Float value overflows finite 32-bit storage")
    return packed


@dataclass(frozen=True)
class MaterialParameter:
    leaf_id: str
    path: str
    display_name: str
    base_type: str
    components: int
    array_index: int
    byte_offset: int
    byte_size: int

    def public(self, raw: bytes) -> dict:
        value = _decode_value(
            raw[self.byte_offset : self.byte_offset + self.byte_size],
            self.base_type,
            self.components,
        )
        return {
            "parameterId": self.leaf_id,
            "path": self.path,
            "displayName": self.display_name,
            "baseType": self.base_type,
            "components": self.components,
            "arrayIndex": self.array_index,
            "value": value,
            "editable": True,
        }


@dataclass
class _MaterialSession:
    session_id: str
    snapshot_id: str
    capture_id: str
    capture_session_id: str
    api: str
    event_id: int
    stage: str
    block_index: int
    block_path: str
    block_name: str
    block_display_name: str
    slot: int
    register_space: int
    shader_resource_id: int
    buffer_resource_id: int
    byte_offset: int
    byte_size: int
    valid_byte_size: int
    baseline_sha256: str
    layout_sha256: str
    baseline: bytes
    current: bytes
    parameters: tuple[MaterialParameter, ...]
    unsupported_fields: tuple[dict, ...]
    revision: int = 0
    override_id: str = ""
    patches: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class _MaterialRollbackTombstone:
    """Bound proof that one host material session was already restored."""

    session_id: str
    capture_id: str
    capture_session_id: str
    receipt: dict


class MaterialSessionManager:
    """Own bounded, in-memory material sessions for one MCP server process."""

    def __init__(
        self,
        bridge_call: Callable[..., Any],
        maximum_sessions: int = MAX_MATERIAL_SESSIONS,
        maximum_rollback_tombstones: int = MAX_MATERIAL_ROLLBACK_TOMBSTONES,
    ) -> None:
        self._bridge_call = bridge_call
        self._maximum_sessions = maximum_sessions
        self._maximum_rollback_tombstones = _strict_int(
            maximum_rollback_tombstones,
            "maximum_rollback_tombstones",
            1,
        )
        self._sessions: dict[str, _MaterialSession] = {}
        self._rollback_tombstones: dict[str, _MaterialRollbackTombstone] = {}
        self._capture_session_id = ""
        self._capture_catalog_id = ""
        self._guard = threading.RLock()

    def _call(self, method: str, params: dict, timeout: float = 60.0) -> Any:
        return self._bridge_call(method, params, timeout=timeout)

    def _release_snapshot_best_effort(
        self,
        *,
        snapshot_id: str,
        override_id: str,
        capture_id: str,
        capture_session_id: str,
    ) -> None:
        """Clear any possibly-live shadow and release one bridge snapshot.

        This is intentionally used after an ambiguous/malformed success.  A
        response validation failure means the host cannot safely keep claiming
        either the old or new replay state.
        """

        try:
            self._call(
                "rollback_event_cbuffer_override",
                {
                    "snapshot_id": snapshot_id,
                    "override_id": override_id,
                    "expected_capture_id": capture_id,
                    "expected_capture_session_id": capture_session_id,
                },
                timeout=10.0,
            )
        except Exception:
            pass

    def _live_capture(self) -> dict:
        response = self._call("get_capture_status", {}, timeout=30.0)
        if not isinstance(response, dict) or response.get("loaded") is not True:
            self._sessions.clear()
            self._rollback_tombstones.clear()
            self._capture_session_id = ""
            self._capture_catalog_id = ""
            raise MaterialSessionError("No capture is loaded in RenderDoc")
        session_id = _hex(response.get("captureSessionId"), 32, "captureSessionId")
        reported_capture_id = str(response.get("captureId", "") or "").strip().lower()
        if reported_capture_id:
            reported_capture_id = _hex(
                reported_capture_id, 64, "active captureId"
            )
        lifecycle_changed = bool(
            self._capture_session_id
            and (
                session_id != self._capture_session_id
                or (
                    self._capture_catalog_id
                    and reported_capture_id
                    and reported_capture_id != self._capture_catalog_id
                )
            )
        )
        if lifecycle_changed:
            self._sessions.clear()
            self._rollback_tombstones.clear()
        self._capture_session_id = session_id
        self._capture_catalog_id = reported_capture_id
        return response

    @staticmethod
    def _capture_id(live: dict, requested: str) -> str:
        if requested:
            capture_id = _hex(requested, 64, "expected_capture_id")
        else:
            capture_id = str(live.get("captureId", "") or "").strip().lower()
            if not capture_id:
                known = list(live.get("knownCatalogCaptureIds", []) or [])
                if len(known) == 1:
                    capture_id = str(known[0]).strip().lower()
            capture_id = _hex(capture_id, 64, "active captureId")
        known_ids = {
            str(value or "").strip().lower()
            for value in list(live.get("knownCatalogCaptureIds", []) or [])
        }
        if capture_id not in known_ids:
            raise MaterialSessionError(
                "Capture catalog identity is not active; export the event/stage catalog first"
            )
        return capture_id

    @staticmethod
    def _parse_parameters(response: dict, baseline: bytes) -> tuple[tuple[MaterialParameter, ...], tuple[dict, ...]]:
        fields = response.get("fields")
        if not isinstance(fields, list):
            raise MaterialSessionError("CBuffer snapshot has no physical field list")
        if len(fields) > MAX_EDITABLE_PARAMETERS:
            raise MaterialSessionError("CBuffer snapshot exposes too many fields")

        parameters: list[MaterialParameter] = []
        unsupported: list[dict] = []
        ids: set[str] = set()
        occupied: list[tuple[int, int]] = []
        for index, source in enumerate(fields):
            if not isinstance(source, dict):
                raise MaterialSessionError("CBuffer field %d is not an object" % index)
            if source.get("editable") is not True:
                unsupported.append(
                    {
                        "path": str(source.get("path", "") or ""),
                        "displayName": str(source.get("displayName", "") or ""),
                        "editable": False,
                        "reason": str(source.get("reason", "unsupported reflected type") or "unsupported reflected type"),
                    }
                )
                continue

            path = str(source.get("path", "") or "")
            if not path or len(path) > 512:
                raise MaterialSessionError("Editable CBuffer field has an invalid path")
            base_type = _normalise_base_type(source.get("baseType"))
            components = _strict_int(source.get("components"), "components", 1)
            if components > 4:
                raise MaterialSessionError("Only scalar through vec4 parameters are supported")
            array_index = _strict_int(source.get("arrayIndex", 0), "arrayIndex", 0)
            byte_offset = _strict_int(source.get("byteOffset"), "byteOffset", 0)
            byte_size = _strict_int(source.get("byteSize"), "byteSize", 1)
            if byte_size != components * 4 or byte_offset + byte_size > len(baseline):
                raise MaterialSessionError("Editable CBuffer field lies outside the bound window")

            expected_id = material_leaf_id(
                path=path,
                base_type=base_type,
                components=components,
                array_index=array_index,
                byte_offset=byte_offset,
                byte_size=byte_size,
            )
            leaf_id = _hex(source.get("leafId"), 32, "leafId")
            if leaf_id != expected_id:
                raise MaterialSessionError("CBuffer field identity does not match its physical layout")
            if leaf_id in ids:
                raise MaterialSessionError("CBuffer snapshot contains a duplicate field identity")
            for start, end in occupied:
                if byte_offset < end and byte_offset + byte_size > start:
                    raise MaterialSessionError("Editable CBuffer fields overlap")
            ids.add(leaf_id)
            occupied.append((byte_offset, byte_offset + byte_size))
            display_name = str(source.get("displayName", "") or path).strip()
            if not display_name or len(display_name) > 512:
                raise MaterialSessionError(
                    "Editable CBuffer field has an invalid display name"
                )
            parameters.append(
                MaterialParameter(
                    leaf_id=leaf_id,
                    path=path,
                    display_name=display_name,
                    base_type=base_type,
                    components=components,
                    array_index=array_index,
                    byte_offset=byte_offset,
                    byte_size=byte_size,
                )
            )
        parameters.sort(key=lambda item: (item.byte_offset, item.path, item.leaf_id))
        if not parameters:
            raise MaterialSessionError(
                "The selected CBuffer has no supported float/int/uint scalar or vector fields"
            )
        return tuple(parameters), tuple(unsupported)

    def _record_from_snapshot(
        self,
        response: Any,
        *,
        capture_id: str,
        event_id: int,
        stage: str,
        block_index: int,
        expected_api: str,
    ) -> _MaterialSession:
        if not isinstance(response, dict) or any(
            (
                response.get("schema") != SNAPSHOT_SCHEMA,
                response.get("schemaVersion") != 1,
                response.get("captureId") != capture_id,
                response.get("captureSessionId") != self._capture_session_id,
                response.get("eventId") != event_id,
                response.get("stage") != stage,
                response.get("blockIndex") != block_index,
            )
        ):
            raise MaterialSessionError(
                "RenderDoc returned mismatched CBuffer snapshot evidence"
            )

        snapshot_id = _hex(response.get("snapshotId"), 32, "snapshotId")
        raw = _decode_base64(response.get("rawBase64"), "rawBase64")
        raw_sha256 = _hex(response.get("rawSha256"), 64, "rawSha256")
        if _sha256(raw) != raw_sha256:
            raise MaterialSessionError("CBuffer snapshot bytes do not match rawSha256")
        valid_size = _strict_int(response.get("validByteSize"), "validByteSize", 1)
        if valid_size != len(raw) or valid_size % 16:
            raise MaterialSessionError(
                "CBuffer snapshot is not a complete aligned binding window"
            )
        byte_offset = _strict_int(response.get("byteOffset"), "byteOffset", 0)
        byte_size = _strict_int(response.get("byteSize"), "byteSize", 1)
        if byte_offset % 16 or byte_size % 16 or byte_size > MAX_CBUFFER_BYTES:
            raise MaterialSessionError("CBuffer binding range is not 16-byte aligned")
        api = _api_token(response.get("api"))
        if api not in ("d3d11", "d3d12"):
            raise MaterialSessionError("CBuffer snapshot has an unsupported or missing API")
        if api != expected_api:
            raise MaterialSessionError("CBuffer snapshot API does not match the live capture")
        if api == "d3d11" and stage in ("task", "mesh"):
            raise MaterialSessionError("Task/mesh CBuffers require a D3D12 capture")
        slot = _strict_int(response.get("slot"), "slot", 0)
        register_space = _strict_int(
            response.get("registerSpace"), "registerSpace", 0)
        if slot > 0xFFFFFFFF or register_space > 0xFFFFFFFF:
            raise MaterialSessionError("CBuffer register identity exceeds uint32")
        if api == "d3d11" and (slot > 13 or register_space != 0):
            raise MaterialSessionError(
                "D3D11 CBuffer binding must be b0-b13 in register space 0")
        if api == "d3d12" and byte_offset % 256:
            raise MaterialSessionError("D3D12 CBV offset is not 256-byte aligned")
        shader_id = _strict_int(
            response.get("shaderResourceId"), "shaderResourceId", 1
        )
        buffer_id = _strict_int(
            response.get("bufferResourceId"), "bufferResourceId", 1
        )
        layout_sha256 = _hex(response.get("layoutSha256"), 64, "layoutSha256")
        block_path = str(response.get("constantBlockPath", "") or "").strip()
        block_name = str(response.get("constantBlockName", "") or "").strip()
        block_display_name = str(
            response.get("constantBlockDisplayName", "") or ""
        ).strip()
        for value, label in (
            (block_path, "constantBlockPath"),
            (block_name, "constantBlockName"),
            (block_display_name, "constantBlockDisplayName"),
        ):
            if not value or len(value) > 512:
                raise MaterialSessionError(
                    "%s must be a non-empty bounded display string" % label
                )
        parameters, unsupported = self._parse_parameters(response, raw)

        return _MaterialSession(
            session_id=uuid.uuid4().hex,
            snapshot_id=snapshot_id,
            capture_id=capture_id,
            capture_session_id=self._capture_session_id,
            api=api,
            event_id=event_id,
            stage=stage,
            block_index=block_index,
            block_path=block_path,
            block_name=block_name,
            block_display_name=block_display_name,
            slot=slot,
            register_space=register_space,
            shader_resource_id=shader_id,
            buffer_resource_id=buffer_id,
            byte_offset=byte_offset,
            byte_size=byte_size,
            valid_byte_size=valid_size,
            baseline_sha256=raw_sha256,
            layout_sha256=layout_sha256,
            baseline=raw,
            current=raw,
            parameters=parameters,
            unsupported_fields=unsupported,
        )

    def prepare(
        self,
        event_id: int,
        stage: str,
        block_index: int,
        expected_capture_id: str = "",
    ) -> dict:
        event_id = _strict_int(event_id, "event_id", 1)
        block_index = _strict_int(block_index, "constant_block_index", 0)
        stage = str(stage or "").strip().lower()
        stage = {
            "pixel": "fragment", "ps": "fragment", "as": "task",
            "amplification": "task", "ms": "mesh",
        }.get(stage, stage)
        if stage not in (
            "vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh"
        ):
            raise MaterialSessionError("Unsupported D3D11/D3D12 shader stage: %s" % stage)

        with self._guard:
            live = self._live_capture()
            capture_id = self._capture_id(live, expected_capture_id)
            live_api = _api_token(live.get("api"))
            if live_api not in ("d3d11", "d3d12"):
                raise MaterialSessionError(
                    "Material sessions require a local D3D11 or D3D12 capture")
            for existing in self._sessions.values():
                if (
                    existing.capture_id == capture_id
                    and existing.event_id == event_id
                    and existing.stage == stage
                    and existing.block_index == block_index
                ):
                    self._verify_bridge_status(existing)
                    return self._public(existing, operation="prepare")
            if len(self._sessions) >= self._maximum_sessions:
                raise MaterialSessionError(
                    "The MCP process reached the %d material-session limit"
                    % self._maximum_sessions
                )
            response = self._call(
                "snapshot_event_cbuffer",
                {
                    "expected_capture_id": capture_id,
                    "event_id": event_id,
                    "stage": stage,
                    "block_index": block_index,
                },
                timeout=60.0,
            )
            try:
                record = self._record_from_snapshot(
                    response,
                    capture_id=capture_id,
                    event_id=event_id,
                    stage=stage,
                    block_index=block_index,
                    expected_api=live_api,
                )
                for existing in self._sessions.values():
                    if (
                        existing.capture_id == capture_id
                        and existing.event_id == event_id
                        and existing.stage == stage
                        and existing.slot == record.slot
                        and existing.register_space == record.register_space
                    ):
                        raise MaterialSessionError(
                            "A material session already owns this event/stage/CBuffer "
                            "slot/register-space"
                        )
            except Exception:
                snapshot_id = str(
                    response.get("snapshotId", "") if isinstance(response, dict) else ""
                ).strip().lower()
                response_capture = (
                    str(response.get("captureId", "") or "").strip().lower()
                    if isinstance(response, dict)
                    else ""
                )
                response_session = (
                    str(response.get("captureSessionId", "") or "").strip().lower()
                    if isinstance(response, dict)
                    else ""
                )
                if (
                    len(snapshot_id) == 32
                    and all(character in "0123456789abcdef" for character in snapshot_id)
                    and response_capture == capture_id
                    and response_session == self._capture_session_id
                ):
                    self._release_snapshot_best_effort(
                        snapshot_id=snapshot_id,
                        override_id="",
                        capture_id=capture_id,
                        capture_session_id=self._capture_session_id,
                    )
                raise
            self._sessions[record.session_id] = record
            return self._public(record, operation="prepare")

    @staticmethod
    def _public(record: _MaterialSession, operation: str = "status") -> dict:
        return {
            "schema": MATERIAL_SCHEMA,
            "schemaVersion": 1,
            "operation": operation,
            "sessionId": record.session_id,
            "snapshotId": record.snapshot_id,
            "captureId": record.capture_id,
            "captureSessionId": record.capture_session_id,
            "api": record.api,
            "eventId": record.event_id,
            "stage": record.stage,
            "constantBlockIndex": record.block_index,
            "constantBlockPath": record.block_path,
            "constantBlockName": record.block_name,
            "constantBlockDisplayName": record.block_display_name,
            "slot": record.slot,
            "registerSpace": record.register_space,
            "shaderResourceId": record.shader_resource_id,
            "bufferResourceId": record.buffer_resource_id,
            "boundByteOffset": record.byte_offset,
            "boundByteSize": record.byte_size,
            "validByteSize": record.valid_byte_size,
            "baselineSha256": record.baseline_sha256,
            "currentSha256": _sha256(record.current),
            "layoutSha256": record.layout_sha256,
            "revision": record.revision,
            "overrideId": record.override_id or None,
            "applied": bool(record.override_id),
            "parameters": [item.public(record.current) for item in record.parameters],
            "unsupportedFields": list(record.unsupported_fields),
            "policy": {
                "acceptedTypes": ["Float", "SInt", "UInt"],
                "acceptedShapes": ["scalar", "vec2", "vec3", "vec4", "explicit array leaves"],
                "callerControlsOffsetsOrRawBytes": False,
                "eventScoped": True,
                "customCBufferSupported": False,
            },
        }

    def _record(self, session_id: str) -> _MaterialSession:
        token = _hex(session_id, 32, "session_id")
        record = self._sessions.get(token)
        if record is None:
            raise MaterialSessionError("Unknown or expired material session ID")
        return record

    def _active_capture_id(self, live: dict) -> str:
        """Resolve the one active catalog identity, never merely a known one."""

        return self._capture_id(live, "")

    def _ensure_capture_binding(
        self, *, capture_id: str, capture_session_id: str
    ) -> None:
        live = self._live_capture()
        if capture_session_id != self._capture_session_id:
            raise MaterialSessionError(
                "Material session belongs to an expired capture lifecycle"
            )
        if self._active_capture_id(live) != capture_id:
            self._sessions.clear()
            self._rollback_tombstones.clear()
            raise MaterialSessionError(
                "Material session belongs to a different capture catalog identity"
            )

    def _ensure_record_capture(self, record: _MaterialSession) -> None:
        self._ensure_capture_binding(
            capture_id=record.capture_id,
            capture_session_id=record.capture_session_id,
        )

    @staticmethod
    def _validate_rollback_response(
        record: _MaterialSession, response: Any
    ) -> bool:
        """Validate the complete bridge rollback identity and return idempotency."""

        already_released = (
            response.get("alreadyReleased") if isinstance(response, dict) else None
        )
        expected_override = record.override_id or None
        expected_cleared = bool(record.override_id) and already_released is False
        if not isinstance(response, dict) or any(
            (
                response.get("schema") != OVERRIDE_SCHEMA,
                response.get("schemaVersion") != 1,
                response.get("snapshotId") != record.snapshot_id,
                response.get("overrideId") != expected_override,
                response.get("captureId") != record.capture_id,
                response.get("captureSessionId") != record.capture_session_id,
                response.get("api") != record.api,
                response.get("eventId") != record.event_id,
                response.get("stage") != record.stage,
                response.get("blockIndex") != record.block_index,
                response.get("slot") != record.slot,
                response.get("registerSpace") != record.register_space,
                response.get("revision") != record.revision,
                response.get("currentRawSha256") != _sha256(record.current),
                type(already_released) is not bool,
                response.get("cleared") is not expected_cleared,
                response.get("snapshotReleased") is not True,
                response.get("configured") is not False,
            )
        ):
            raise MaterialSessionError(
                "RenderDoc returned a mismatched CBuffer rollback result"
            )
        return already_released

    @staticmethod
    def _rollback_receipt(
        record: _MaterialSession,
        *,
        proof: dict,
        restored_by: str,
        already_restored: bool,
        idempotent_rollback: bool,
    ) -> dict:
        result = {
            "schema": MATERIAL_SCHEMA,
            "schemaVersion": 1,
            "operation": "rollback",
            "sessionId": record.session_id,
            "captureId": record.capture_id,
            "captureSessionId": record.capture_session_id,
            "eventId": record.event_id,
            "stage": record.stage,
            "slot": record.slot,
            "registerSpace": record.register_space,
            "applied": False,
            "snapshotReleased": True,
            "restoredBy": restored_by,
            "alreadyRestored": already_restored,
            "idempotentRollback": idempotent_rollback,
        }
        if restored_by == "rollback":
            result["override"] = copy.deepcopy(proof)
        elif restored_by == "clearAll":
            result["clearAll"] = copy.deepcopy(proof)
        else:  # Defensive: receipts must never advertise an unknown proof kind.
            raise MaterialSessionError("Unknown material restoration proof kind")
        return result

    def _remember_rollback(
        self, record: _MaterialSession, receipt: dict
    ) -> None:
        token = record.session_id
        self._rollback_tombstones.pop(token, None)
        self._rollback_tombstones[token] = _MaterialRollbackTombstone(
            session_id=token,
            capture_id=record.capture_id,
            capture_session_id=record.capture_session_id,
            receipt=copy.deepcopy(receipt),
        )
        while (
            len(self._rollback_tombstones)
            > self._maximum_rollback_tombstones
        ):
            oldest = next(iter(self._rollback_tombstones))
            self._rollback_tombstones.pop(oldest, None)

    def _repeat_rollback(self, tombstone: _MaterialRollbackTombstone) -> dict:
        self._ensure_capture_binding(
            capture_id=tombstone.capture_id,
            capture_session_id=tombstone.capture_session_id,
        )
        # _ensure_capture_binding may expire every local capability on a capture
        # transition. Re-fetch before returning any old proof.
        current = self._rollback_tombstones.get(tombstone.session_id)
        if current is not tombstone:
            raise MaterialSessionError("Unknown or expired material session ID")
        result = copy.deepcopy(tombstone.receipt)
        result["alreadyRestored"] = True
        result["idempotentRollback"] = True
        return result


    def _verify_bridge_status(self, record: _MaterialSession) -> None:
        """Require bridge bookkeeping to agree with the host's claimed state."""

        try:
            response = self._call(
                "get_event_cbuffer_override_status",
                {
                    "snapshot_id": record.snapshot_id,
                    "expected_capture_id": record.capture_id,
                    "expected_capture_session_id": record.capture_session_id,
                },
                timeout=30.0,
            )
            expected_override = record.override_id or None
            if not isinstance(response, dict) or any(
                (
                    response.get("schema") != OVERRIDE_SCHEMA,
                    response.get("schemaVersion") != 1,
                    response.get("snapshotId") != record.snapshot_id,
                    response.get("captureId") != record.capture_id,
                    response.get("captureSessionId") != record.capture_session_id,
                    response.get("api") != record.api,
                    response.get("eventId") != record.event_id,
                    response.get("stage") != record.stage,
                    response.get("slot") != record.slot,
                    response.get("registerSpace") != record.register_space,
                    response.get("overrideId") != expected_override,
                    response.get("revision") != record.revision,
                    response.get("currentRawSha256") != _sha256(record.current),
                    response.get("configured") is not bool(record.override_id),
                )
            ):
                raise MaterialSessionError(
                    "RenderDoc CBuffer state no longer matches this material session"
                )
        except Exception:
            self._release_snapshot_best_effort(
                snapshot_id=record.snapshot_id,
                override_id=record.override_id,
                capture_id=record.capture_id,
                capture_session_id=record.capture_session_id,
            )
            self._sessions.pop(record.session_id, None)
            raise

    @staticmethod
    def _select_parameter(record: _MaterialSession, selector: str) -> MaterialParameter:
        selector = str(selector or "").strip()
        if not selector:
            raise MaterialSessionError("parameter must be a parameterId or exact unique name")
        by_id = [item for item in record.parameters if item.leaf_id == selector.lower()]
        if by_id:
            return by_id[0]
        matches = [
            item
            for item in record.parameters
            if item.path == selector or item.display_name == selector
        ]
        if not matches:
            raise MaterialSessionError("Unknown material parameter: %s" % selector)
        if len(matches) != 1:
            raise MaterialSessionError(
                "Material parameter name is ambiguous; use parameterId"
            )
        return matches[0]

    def _update_many_locked(
        self, record: _MaterialSession, updates: Any, *, operation: str
    ) -> dict:
        """Validate all typed leaves, then perform exactly one replay apply.

        Packing every value before constructing the candidate means a bad
        selector, type, shape, NaN, or overflow cannot partially mutate either
        host bookkeeping or replay state. The bridge's immutable snapshot and
        optimistic revision remain the physical rollback/concurrency boundary.
        """

        if not isinstance(updates, list) or not updates:
            raise MaterialSessionError("updates must be a non-empty array")
        if len(updates) > len(record.parameters):
            raise MaterialSessionError(
                "updates contains more entries than editable material parameters"
            )

        selected_updates: list[tuple[MaterialParameter, bytes]] = []
        selected_ids: set[str] = set()
        for index, update in enumerate(updates):
            if not isinstance(update, dict) or set(update) != {"parameter", "value"}:
                raise MaterialSessionError(
                    "updates[%d] must contain only parameter and value" % index
                )
            selected = self._select_parameter(record, update.get("parameter"))
            if selected.leaf_id in selected_ids:
                raise MaterialSessionError(
                    "updates contains the same material parameter more than once"
                )
            selected_ids.add(selected.leaf_id)
            packed = _pack_value(
                update.get("value"), selected.base_type, selected.components
            )
            selected_updates.append((selected, packed))

        candidate_patches = dict(record.patches)
        for selected, packed in selected_updates:
            candidate_patches[selected.leaf_id] = packed
        candidate = bytearray(record.baseline)
        by_id = {item.leaf_id: item for item in record.parameters}
        manifest = []
        for leaf_id in sorted(
            candidate_patches,
            key=lambda token: (by_id[token].byte_offset, token),
        ):
            item = by_id[leaf_id]
            item_bytes = candidate_patches[leaf_id]
            candidate[item.byte_offset : item.byte_offset + item.byte_size] = item_bytes
            manifest.append(
                {
                    "leafId": leaf_id,
                    "packedBase64": base64.b64encode(item_bytes).decode("ascii"),
                }
            )
        candidate_bytes = bytes(candidate)
        next_revision = record.revision + 1
        apply_params = {
            "snapshot_id": record.snapshot_id,
            "expected_capture_id": record.capture_id,
            "expected_capture_session_id": record.capture_session_id,
            # Optimistic concurrency binds this transaction to the bytes from
            # the last accepted revision, not only to the immutable baseline.
            "expected_raw_sha256": _sha256(record.current),
            "expected_layout_sha256": record.layout_sha256,
            "expected_override_id": record.override_id,
            "expected_revision": record.revision,
            "new_revision": next_revision,
            "data_base64": base64.b64encode(candidate_bytes).decode("ascii"),
            "data_sha256": _sha256(candidate_bytes),
            "patches": manifest,
        }
        response: Any = None
        try:
            response = self._call(
                "apply_event_cbuffer_override", apply_params, timeout=60.0
            )
            if not isinstance(response, dict) or any(
                (
                    response.get("schema") != OVERRIDE_SCHEMA,
                    response.get("schemaVersion") != 1,
                    response.get("snapshotId") != record.snapshot_id,
                    response.get("captureId") != record.capture_id,
                    response.get("captureSessionId") != record.capture_session_id,
                    response.get("api") != record.api,
                    response.get("eventId") != record.event_id,
                    response.get("stage") != record.stage,
                    response.get("slot") != record.slot,
                    response.get("registerSpace") != record.register_space,
                    response.get("revision") != next_revision,
                    response.get("currentRawSha256") != _sha256(candidate_bytes),
                    response.get("configured") is not True,
                    response.get("appliedToCurrentEvent") is not True,
                )
            ):
                raise MaterialSessionError(
                    "RenderDoc returned a mismatched CBuffer apply result"
                )
            override_id = _hex(response.get("overrideId"), 32, "overrideId")
            if record.override_id and override_id != record.override_id:
                raise MaterialSessionError(
                    "RenderDoc changed the active CBuffer override identity"
                )
        except Exception:
            # A response that failed any contract check is untrusted, including its
            # overrideId. Roll back by the already-issued snapshot capability only; the
            # bridge interprets an empty override ID as "clear whatever this exact snapshot
            # currently owns". This also handles a lost response after a successful apply.
            self._release_snapshot_best_effort(
                snapshot_id=record.snapshot_id,
                override_id="",
                capture_id=record.capture_id,
                capture_session_id=record.capture_session_id,
            )
            self._sessions.pop(record.session_id, None)
            raise

        record.patches = candidate_patches
        record.current = candidate_bytes
        record.revision = next_revision
        record.override_id = override_id
        result = self._public(record, operation=operation)
        result["updatedParameterIds"] = [
            selected.leaf_id for selected, unused_packed in selected_updates
        ]
        result["updatedParameters"] = [
            selected.path for selected, unused_packed in selected_updates
        ]
        result["updatedParameterCount"] = len(selected_updates)
        return result

    def update(self, session_id: str, parameter: str, value: Any) -> dict:
        with self._guard:
            record = self._record(session_id)
            self._ensure_record_capture(record)
            result = self._update_many_locked(
                record,
                [{"parameter": parameter, "value": value}],
                operation="update",
            )
            result["updatedParameterId"] = result["updatedParameterIds"][0]
            result["updatedParameter"] = result["updatedParameters"][0]
            return result

    def update_many(self, session_id: str, updates: Any) -> dict:
        """Apply a bounded set of typed leaf values as one replay transaction."""

        with self._guard:
            record = self._record(session_id)
            self._ensure_record_capture(record)
            return self._update_many_locked(
                record, updates, operation="update-many"
            )

    def status(self, session_id: str) -> dict:
        with self._guard:
            record = self._record(session_id)
            self._ensure_record_capture(record)
            self._verify_bridge_status(record)
            return self._public(record)

    def list(self) -> dict:
        with self._guard:
            self._live_capture()
            sessions = [
                self._public(record)
                for record in sorted(
                    self._sessions.values(),
                    key=lambda item: (
                        item.event_id, item.stage, item.slot, item.register_space,
                        item.session_id),
                )
            ]
            return {
                "schema": MATERIAL_SCHEMA,
                "schemaVersion": 1,
                "operation": "list",
                "captureSessionId": self._capture_session_id,
                "count": len(sessions),
                "sessions": sessions,
            }

    def rollback(self, session_id: str) -> dict:
        with self._guard:
            token = _hex(session_id, 32, "session_id")
            record = self._sessions.get(token)
            if record is None:
                tombstone = self._rollback_tombstones.get(token)
                if tombstone is None:
                    raise MaterialSessionError(
                        "Unknown or expired material session ID"
                    )
                return self._repeat_rollback(tombstone)
            self._ensure_record_capture(record)
            try:
                response = self._call(
                    "rollback_event_cbuffer_override",
                    {
                        "snapshot_id": record.snapshot_id,
                        "override_id": record.override_id,
                        "expected_capture_id": record.capture_id,
                        "expected_capture_session_id": record.capture_session_id,
                    },
                    timeout=60.0,
                )
                already_released = self._validate_rollback_response(
                    record, response
                )
            except Exception:
                # Retain the exact host capability. If the bridge completed the
                # release but its response was lost, the next exact call reaches
                # the bridge's capture-bound snapshot tombstone. We never turn an
                # unverified best-effort retry into a success receipt.
                raise
            receipt = self._rollback_receipt(
                record,
                proof=response,
                restored_by="rollback",
                already_restored=already_released,
                idempotent_rollback=already_released,
            )
            self._remember_rollback(record, receipt)
            self._sessions.pop(record.session_id, None)
            return receipt

    def clear_all(self, expected_capture_id: str = "") -> dict:
        """Explicitly recover all bridge/host material state for this capture."""

        with self._guard:
            live = self._live_capture()
            capture_id = self._capture_id(live, expected_capture_id)
            active_capture_id = self._active_capture_id(live)
            if active_capture_id != capture_id:
                raise MaterialSessionError(
                    "Requested material recovery identity is not the active capture"
                )
            related_records = sorted(
                (
                    record
                    for record in self._sessions.values()
                    if record.capture_id == capture_id
                    and record.capture_session_id == self._capture_session_id
                ),
                key=lambda record: record.session_id,
            )
            already_restored_ids = sorted(
                tombstone.session_id
                for tombstone in self._rollback_tombstones.values()
                if tombstone.capture_id == capture_id
                and tombstone.capture_session_id == self._capture_session_id
            )
            try:
                response = self._call(
                    "clear_all_event_cbuffer_snapshots",
                    {
                        "expected_capture_id": capture_id,
                        "expected_capture_session_id": self._capture_session_id,
                    },
                    timeout=60.0,
                )
                if not isinstance(response, dict) or any(
                    (
                        response.get("schema") != CLEAR_ALL_SCHEMA,
                        response.get("schemaVersion") != 1,
                        response.get("captureId") != capture_id,
                        response.get("captureSessionId") != self._capture_session_id,
                        response.get("cleared") is not True,
                    )
                ):
                    raise MaterialSessionError(
                        "RenderDoc returned a mismatched CBuffer clear-all result"
                    )
                cleared_count = _strict_int(
                    response.get("clearedOverrideCount"),
                    "clearedOverrideCount",
                    0,
                )
                released_count = _strict_int(
                    response.get("releasedSnapshotCount"),
                    "releasedSnapshotCount",
                    0,
                )
            except Exception:
                # The clear may have completed before a response was lost. Never
                # keep advertising process-local handles after this recovery call.
                self._sessions.clear()
                raise
            restored_ids = [record.session_id for record in related_records]
            for record in related_records:
                receipt = self._rollback_receipt(
                    record,
                    proof=response,
                    restored_by="clearAll",
                    already_restored=True,
                    idempotent_rollback=True,
                )
                self._remember_rollback(record, receipt)
            self._sessions.clear()
            return {
                "schema": MATERIAL_SCHEMA,
                "schemaVersion": 1,
                "operation": "clearAll",
                "captureId": capture_id,
                "captureSessionId": self._capture_session_id,
                "clearAll": copy.deepcopy(response),
                "clearedOverrideCount": cleared_count,
                "releasedSnapshotCount": released_count,
                "restoredSessionCount": len(restored_ids),
                "restoredSessionIds": restored_ids,
                "alreadyRestoredSessionCount": len(already_restored_ids),
                "alreadyRestoredSessionIds": already_restored_ids,
                "cleared": True,
            }

    def forget_all(self) -> None:
        """Forget process-local handles during server shutdown/capture loss."""

        with self._guard:
            self._sessions.clear()
            self._rollback_tombstones.clear()
            self._capture_session_id = ""
            self._capture_catalog_id = ""

    def shutdown(self) -> None:
        """Best-effort rollback of every session owned by this host process."""

        with self._guard:
            records = list(self._sessions.values())
            for record in records:
                try:
                    self._call(
                        "rollback_event_cbuffer_override",
                        {
                            "snapshot_id": record.snapshot_id,
                            "override_id": record.override_id,
                            "expected_capture_id": record.capture_id,
                            "expected_capture_session_id": record.capture_session_id,
                        },
                        timeout=10.0,
                    )
                except Exception:
                    pass
            self._sessions.clear()
            self._rollback_tombstones.clear()
            self._capture_session_id = ""
            self._capture_catalog_id = ""
