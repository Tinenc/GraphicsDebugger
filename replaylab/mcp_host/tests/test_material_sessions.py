from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import math
import os
import struct
import sys
import types
import unittest
from pathlib import Path

from src.material_sessions import (
    CLEAR_ALL_SCHEMA,
    MATERIAL_SCHEMA,
    OVERRIDE_SCHEMA,
    SNAPSHOT_SCHEMA,
    MaterialSessionError,
    MaterialSessionManager,
    material_leaf_id,
)


REPOSITORY = Path(__file__).resolve().parents[2]
CAPTURE_ID = "c" * 64
CAPTURE_SESSION_ID = "8" * 32


def _field(
    path: str,
    base_type: str,
    components: int,
    byte_offset: int,
    *,
    array_index: int = 0,
    display_name: str | None = None,
) -> dict:
    byte_size = components * 4
    leaf_id = material_leaf_id(
        path=path,
        base_type=base_type,
        components=components,
        array_index=array_index,
        byte_offset=byte_offset,
        byte_size=byte_size,
    )
    return {
        "leafId": leaf_id,
        "path": path,
        "displayName": display_name or path,
        "baseType": base_type,
        "components": components,
        "arrayIndex": array_index,
        "byteOffset": byte_offset,
        "byteSize": byte_size,
        "editable": True,
    }


class _MaterialBridge:
    def __init__(self) -> None:
        self.capture_id = CAPTURE_ID
        self.capture_session_id = CAPTURE_SESSION_ID
        self.calls: list[tuple[str, dict]] = []
        self.apply_requests: list[dict] = []
        self.snapshots: dict[str, dict] = {}
        self.override_ids: dict[str, str] = {}
        self.override_state: dict[str, dict] = {}
        self.released_snapshots: dict[str, dict] = {}
        self.snapshot_keys: dict[tuple[int, str, int, int], str] = {}
        self.malformed_field: dict | None = None
        self.malformed_apply = False
        self.malformed_override_id = ""
        self.api = "d3d11"
        self.slot = 2
        self.register_space = 0
        self.lose_next_rollback_response = False

    def call(self, method: str, params: dict, timeout: float | None = None):
        self.calls.append((method, json.loads(json.dumps(params))))
        if method == "get_capture_status":
            return {
                "loaded": True,
                "api": "GraphicsAPI.%s" % self.api.upper(),
                "captureId": self.capture_id,
                "capturePath": "D:/captures/frame.rdc",
                "captureSessionId": self.capture_session_id,
                "knownCatalogCaptureIds": [self.capture_id],
            }
        if method == "snapshot_event_cbuffer":
            event_id = params["event_id"]
            physical_key = (
                event_id, params["stage"], self.slot, self.register_space
            )
            if physical_key in self.snapshot_keys:
                raise RuntimeError("event/stage/slot/register-space is already owned")
            if event_id == 10:
                byte_offset, byte_size, raw = 65536, 64, bytes(range(64))
            else:
                byte_offset, byte_size, raw = 131072, 32, bytes(range(32))
            fields = [
                _field("roughness", "Float", 1, 0),
                _field("tint", "Float", 3, 4),
                _field("mode", "SInt", 1, 16),
                _field("left.value", "UInt", 1, 20, display_name="value"),
                _field("right.value", "UInt", 1, 24, display_name="value"),
            ]
            if len(raw) >= 52:
                fields.extend(
                    [
                        _field("weights[0]", "Float", 1, 32, array_index=0),
                        _field("weights[1]", "Float", 1, 48, array_index=1),
                    ]
                )
            if self.malformed_field is not None:
                fields.append(dict(self.malformed_field))
            fields.append(
                {
                    "path": "worldViewProjection",
                    "displayName": "worldViewProjection",
                    "editable": False,
                    "reason": "matrix fields are intentionally excluded",
                }
            )
            snapshot_id = hashlib.sha256(
                ("%d:%d" % (event_id, params["block_index"])).encode("ascii")
            ).hexdigest()[:32]
            result = {
                "schema": SNAPSHOT_SCHEMA,
                "schemaVersion": 1,
                "snapshotId": snapshot_id,
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "api": self.api,
                "eventId": event_id,
                "stage": params["stage"],
                "blockIndex": params["block_index"],
                "constantBlockPath": "MaterialConstants",
                "constantBlockName": "MaterialConstants",
                "constantBlockDisplayName": "材质参数",
                "slot": self.slot,
                "registerSpace": self.register_space,
                "shaderResourceId": 77,
                "bufferResourceId": 44,
                "byteOffset": byte_offset,
                "byteSize": byte_size,
                "validByteSize": len(raw),
                "rawBase64": base64.b64encode(raw).decode("ascii"),
                "rawSha256": hashlib.sha256(raw).hexdigest(),
                "layoutSha256": "a" * 64,
                "fields": fields,
            }
            self.snapshots[snapshot_id] = result
            self.released_snapshots.pop(snapshot_id, None)
            self.snapshot_keys[physical_key] = snapshot_id
            return result
        if method == "apply_event_cbuffer_override":
            self.apply_requests.append(json.loads(json.dumps(params)))
            snapshot = self.snapshots[params["snapshot_id"]]
            previous = self.override_state.get(params["snapshot_id"])
            expected_current = (
                previous["currentRawSha256"] if previous else snapshot["rawSha256"]
            )
            if params["expected_raw_sha256"] != expected_current:
                raise AssertionError("host sent a stale current CBuffer digest")
            override_id = self.override_ids.setdefault(params["snapshot_id"], "9" * 32)
            self.override_state[params["snapshot_id"]] = {
                "overrideId": override_id,
                "revision": params["new_revision"],
                "currentRawSha256": params["data_sha256"],
            }
            result = {
                "schema": OVERRIDE_SCHEMA,
                "schemaVersion": 1,
                "snapshotId": params["snapshot_id"],
                "overrideId": override_id,
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "api": snapshot["api"],
                "eventId": snapshot["eventId"],
                "stage": snapshot["stage"],
                "slot": snapshot["slot"],
                "registerSpace": snapshot["registerSpace"],
                "revision": params["new_revision"],
                "currentRawSha256": params["data_sha256"],
                "configured": True,
                "appliedToCurrentEvent": True,
            }
            if self.malformed_apply:
                result["schema"] = "malformed-success"
                if self.malformed_override_id:
                    result["overrideId"] = self.malformed_override_id
            return result
        if method == "get_event_cbuffer_override_status":
            snapshot = self.snapshots[params["snapshot_id"]]
            state = self.override_state.get(params["snapshot_id"])
            return {
                "schema": OVERRIDE_SCHEMA,
                "schemaVersion": 1,
                "snapshotId": params["snapshot_id"],
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "api": snapshot["api"],
                "eventId": snapshot["eventId"],
                "stage": snapshot["stage"],
                "slot": snapshot["slot"],
                "registerSpace": snapshot["registerSpace"],
                "overrideId": state["overrideId"] if state else None,
                "revision": state["revision"] if state else 0,
                "currentRawSha256": (
                    state["currentRawSha256"] if state else snapshot["rawSha256"]
                ),
                "configured": state is not None,
            }
        if method == "rollback_event_cbuffer_override":
            snapshot = self.snapshots[params["snapshot_id"]]
            state = self.override_state.get(params["snapshot_id"])
            released = self.released_snapshots.get(params["snapshot_id"])
            requested_id = params["override_id"]
            if (
                requested_id
                and requested_id
                != (
                    state["overrideId"]
                    if state is not None
                    else (released or {}).get("overrideId", "")
                )
            ):
                raise RuntimeError("requested override ID is not active")
            already_released = released is not None
            if not already_released:
                released = {
                    "overrideId": state["overrideId"] if state else None,
                    "revision": state["revision"] if state else 0,
                    "currentRawSha256": (
                        state["currentRawSha256"]
                        if state
                        else snapshot["rawSha256"]
                    ),
                }
                self.released_snapshots[params["snapshot_id"]] = released
                self.override_state.pop(params["snapshot_id"], None)
                self.snapshot_keys.pop(
                    (
                        snapshot["eventId"], snapshot["stage"], snapshot["slot"],
                        snapshot["registerSpace"],
                    ),
                    None,
                )
            result = {
                "schema": OVERRIDE_SCHEMA,
                "schemaVersion": 1,
                "snapshotId": params["snapshot_id"],
                "overrideId": released["overrideId"],
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "api": snapshot["api"],
                "eventId": snapshot["eventId"],
                "stage": snapshot["stage"],
                "blockIndex": snapshot["blockIndex"],
                "slot": snapshot["slot"],
                "registerSpace": snapshot["registerSpace"],
                "revision": released["revision"],
                "currentRawSha256": released["currentRawSha256"],
                "cleared": bool(released["overrideId"]) and not already_released,
                "alreadyReleased": already_released,
                "snapshotReleased": True,
                "configured": False,
            }
            if self.lose_next_rollback_response:
                self.lose_next_rollback_response = False
                raise TimeoutError("rollback response was lost")
            return result
        if method == "clear_all_event_cbuffer_snapshots":
            active = len(self.override_state)
            snapshots = len(self.snapshots)
            self.override_state.clear()
            self.snapshot_keys.clear()
            self.snapshots.clear()
            self.released_snapshots.clear()
            return {
                "schema": "renderdoc-ultra-event-cbuffer-clear-all/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "clearedOverrideCount": active,
                "releasedSnapshotCount": snapshots,
                "cleared": True,
            }
        raise AssertionError("unexpected bridge method: %s" % method)


class MaterialSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = _MaterialBridge()
        self.manager = MaterialSessionManager(self.bridge.call)

    def prepare(self, event_id: int = 10) -> dict:
        return self.manager.prepare(event_id, "fragment", 0, CAPTURE_ID)

    def test_leaf_identity_uses_utf8_not_ascii_escaped_json(self) -> None:
        values = {
            "path": "材质.粗糙度",
            "base_type": "Float",
            "components": 1,
            "array_index": 0,
            "byte_offset": 0,
            "byte_size": 4,
        }
        identity = {
            "arrayIndex": 0,
            "baseType": "Float",
            "byteOffset": 0,
            "byteSize": 4,
            "components": 1,
            "path": "材质.粗糙度",
        }
        expected = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        escaped = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        self.assertEqual(material_leaf_id(**values), expected)
        self.assertNotEqual(expected, escaped)

    def test_ring_offset_and_shared_resource_ranges_are_preserved(self) -> None:
        first = self.prepare(10)
        second = self.prepare(11)
        self.assertEqual(first["constantBlockPath"], "MaterialConstants")
        self.assertEqual(first["constantBlockName"], "MaterialConstants")
        self.assertEqual(first["constantBlockDisplayName"], "材质参数")
        self.assertEqual(first["bufferResourceId"], second["bufferResourceId"])
        self.assertEqual(first["boundByteOffset"], 65536)
        self.assertEqual(first["boundByteSize"], 64)
        self.assertEqual(first["validByteSize"], 64)
        self.assertEqual(second["boundByteOffset"], 131072)
        self.assertEqual(second["boundByteSize"], 32)
        self.assertEqual(second["validByteSize"], 32)
        self.assertNotEqual(first["sessionId"], second["sessionId"])

    def test_d3d12_task_mesh_large_slots_register_spaces_and_clear_all(self) -> None:
        self.bridge.api = "d3d12"
        self.bridge.slot = 70000
        self.bridge.register_space = 3

        mesh = self.manager.prepare(10, "ms", 0, CAPTURE_ID)
        task = self.manager.prepare(11, "amplification", 0, CAPTURE_ID)
        self.assertEqual(mesh["api"], "d3d12")
        self.assertEqual(task["api"], "d3d12")
        self.assertEqual(mesh["stage"], "mesh")
        self.assertEqual(task["stage"], "task")
        self.assertEqual(mesh["slot"], 70000)
        self.assertEqual(mesh["registerSpace"], 3)
        self.assertEqual(mesh["boundByteOffset"] % 256, 0)
        self.assertEqual(task["boundByteOffset"] % 256, 0)

        roughness = next(
            item for item in mesh["parameters"] if item["path"] == "roughness"
        )
        self.manager.update(mesh["sessionId"], roughness["parameterId"], 0.25)
        cleared = self.manager.clear_all(CAPTURE_ID)
        self.assertTrue(cleared["cleared"])
        self.assertEqual(cleared["clearedOverrideCount"], 1)
        self.assertEqual(cleared["releasedSnapshotCount"], 2)
        self.assertEqual(self.manager.list()["count"], 0)

    def test_same_stage_slot_different_register_spaces_create_independent_sessions(self) -> None:
        self.bridge.api = "d3d12"
        self.bridge.register_space = 2
        space2 = self.manager.prepare(10, "fragment", 0, CAPTURE_ID)
        self.bridge.register_space = 9
        space9 = self.manager.prepare(10, "fragment", 1, CAPTURE_ID)

        self.assertEqual(space2["slot"], space9["slot"])
        self.assertEqual(space2["registerSpace"], 2)
        self.assertEqual(space9["registerSpace"], 9)
        self.assertNotEqual(space2["sessionId"], space9["sessionId"])
        self.assertEqual(self.manager.list()["count"], 2)

    def test_typed_update_patches_full_immutable_window_and_replays(self) -> None:
        prepared = self.prepare()
        baseline = bytes(range(64))
        roughness = next(
            item for item in prepared["parameters"] if item["path"] == "roughness"
        )
        updated = self.manager.update(
            prepared["sessionId"], roughness["parameterId"], 0.75
        )
        self.assertTrue(updated["applied"])
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["updatedParameterId"], roughness["parameterId"])
        request = self.bridge.apply_requests[-1]
        payload = base64.b64decode(request["data_base64"])
        self.assertEqual(len(payload), len(baseline))
        self.assertEqual(payload[:4], struct.pack("<f", 0.75))
        self.assertEqual(payload[4:], baseline[4:])
        self.assertEqual(
            request["patches"],
            [
                {
                    "leafId": roughness["parameterId"],
                    "packedBase64": base64.b64encode(struct.pack("<f", 0.75)).decode("ascii"),
                }
            ],
        )

        tint = next(item for item in updated["parameters"] if item["path"] == "tint")
        second = self.manager.update(
            prepared["sessionId"], tint["parameterId"], [1.0, 0.5, 0.25]
        )
        second_payload = base64.b64decode(self.bridge.apply_requests[-1]["data_base64"])
        self.assertEqual(
            self.bridge.apply_requests[-1]["expected_raw_sha256"],
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(second_payload[:4], struct.pack("<f", 0.75))
        self.assertEqual(second_payload[4:16], struct.pack("<fff", 1.0, 0.5, 0.25))
        self.assertEqual(second_payload[16:], baseline[16:])
        self.assertEqual(second["revision"], 2)

    def test_typed_batch_update_validates_then_replays_once(self) -> None:
        prepared = self.prepare()
        by_path = {item["path"]: item for item in prepared["parameters"]}
        before = len(self.bridge.apply_requests)

        updated = self.manager.update_many(
            prepared["sessionId"],
            [
                {
                    "parameter": by_path["roughness"]["parameterId"],
                    "value": 0.625,
                },
                {
                    "parameter": by_path["tint"]["parameterId"],
                    "value": [1.0, 0.5, 0.25],
                },
                {
                    "parameter": by_path["mode"]["parameterId"],
                    "value": 7,
                },
            ],
        )

        self.assertEqual(len(self.bridge.apply_requests), before + 1)
        self.assertEqual(updated["operation"], "update-many")
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["updatedParameterCount"], 3)
        self.assertEqual(
            updated["updatedParameterIds"],
            [
                by_path["roughness"]["parameterId"],
                by_path["tint"]["parameterId"],
                by_path["mode"]["parameterId"],
            ],
        )
        request = self.bridge.apply_requests[-1]
        payload = base64.b64decode(request["data_base64"])
        self.assertEqual(payload[:4], struct.pack("<f", 0.625))
        self.assertEqual(payload[4:16], struct.pack("<fff", 1.0, 0.5, 0.25))
        self.assertEqual(payload[16:20], struct.pack("<i", 7))
        self.assertEqual(len(request["patches"]), 3)

    def test_batch_update_rejects_all_items_before_replay(self) -> None:
        prepared = self.prepare()
        before = len(self.bridge.apply_requests)
        with self.assertRaisesRegex(MaterialSessionError, "exactly 3"):
            self.manager.update_many(
                prepared["sessionId"],
                [
                    {"parameter": "roughness", "value": 0.5},
                    {"parameter": "tint", "value": [1.0, 2.0]},
                ],
            )
        self.assertEqual(len(self.bridge.apply_requests), before)
        self.assertEqual(self.manager.status(prepared["sessionId"])["revision"], 0)

        with self.assertRaisesRegex(MaterialSessionError, "more than once"):
            self.manager.update_many(
                prepared["sessionId"],
                [
                    {"parameter": "roughness", "value": 0.5},
                    {"parameter": "roughness", "value": 0.75},
                ],
            )
        self.assertEqual(len(self.bridge.apply_requests), before)

    def test_nan_overflow_wrong_type_and_wrong_shape_are_rejected_before_bridge(self) -> None:
        prepared = self.prepare()
        by_path = {item["path"]: item for item in prepared["parameters"]}
        before = len(self.bridge.apply_requests)
        invalid = [
            ("roughness", math.nan),
            ("roughness", math.inf),
            ("roughness", 1.0e100),
            ("roughness", [1.0]),
            ("tint", [1.0, 2.0]),
            ("mode", 1.25),
            ("mode", 2147483648),
            ("left.value", -1),
            ("left.value", True),
        ]
        for path, value in invalid:
            with self.subTest(path=path, value=value):
                with self.assertRaises(MaterialSessionError):
                    self.manager.update(
                        prepared["sessionId"], by_path[path]["parameterId"], value
                    )
        self.assertEqual(len(self.bridge.apply_requests), before)

    def test_ambiguous_display_name_requires_parameter_id(self) -> None:
        prepared = self.prepare()
        with self.assertRaisesRegex(MaterialSessionError, "ambiguous"):
            self.manager.update(prepared["sessionId"], "value", 3)
        left = next(
            item for item in prepared["parameters"] if item["path"] == "left.value"
        )
        result = self.manager.update(
            prepared["sessionId"], left["parameterId"], 3
        )
        self.assertEqual(result["updatedParameter"], "left.value")

    def test_out_of_bounds_or_forged_field_identity_rejects_snapshot(self) -> None:
        malformed = _field("bad", "Float", 4, 60)
        self.bridge.malformed_field = malformed
        with self.assertRaisesRegex(MaterialSessionError, "outside"):
            self.prepare()
        rollback_calls = [
            params
            for method, params in self.bridge.calls
            if method == "rollback_event_cbuffer_override"
        ]
        self.assertEqual(len(rollback_calls), 1)
        self.assertEqual(rollback_calls[0]["override_id"], "")

        self.bridge.malformed_field = _field("forged", "Float", 1, 28)
        self.bridge.malformed_field["leafId"] = "f" * 32
        with self.assertRaisesRegex(MaterialSessionError, "identity"):
            self.prepare()

    def test_same_physical_key_has_one_owner(self) -> None:
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first["sessionId"], second["sessionId"])
        snapshot_calls = [
            params
            for method, params in self.bridge.calls
            if method == "snapshot_event_cbuffer"
        ]
        self.assertEqual(len(snapshot_calls), 1)

        with self.assertRaisesRegex(RuntimeError, "already owned"):
            self.manager.prepare(10, "fragment", 1, CAPTURE_ID)
        rollback_calls = [
            params
            for method, params in self.bridge.calls
            if method == "rollback_event_cbuffer_override"
        ]
        self.assertEqual(len(rollback_calls), 0)

        other_manager = MaterialSessionManager(self.bridge.call)
        with self.assertRaisesRegex(RuntimeError, "already owned"):
            other_manager.prepare(10, "fragment", 0, CAPTURE_ID)
        # The second manager was never given the capability and cannot disturb
        # the first manager's inactive snapshot.
        self.assertEqual(self.manager.status(first["sessionId"])["revision"], 0)

    def test_malformed_apply_success_is_rolled_back_and_forgotten(self) -> None:
        prepared = self.prepare()
        self.bridge.malformed_apply = True
        # A syntactically valid but false ID must never be trusted during cleanup.
        self.bridge.malformed_override_id = "8" * 32
        with self.assertRaisesRegex(MaterialSessionError, "mismatched"):
            self.manager.update(prepared["sessionId"], "roughness", 0.5)
        rollback_calls = [
            params
            for method, params in self.bridge.calls
            if method == "rollback_event_cbuffer_override"
        ]
        self.assertEqual(rollback_calls[-1]["override_id"], "")
        self.assertEqual(self.bridge.override_state, {})
        self.assertEqual(self.bridge.snapshot_keys, {})
        with self.assertRaisesRegex(MaterialSessionError, "Unknown or expired"):
            self.manager.status(prepared["sessionId"])

        # A malformed update response must also tear down an already tracked
        # override instead of claiming that its previous revision is still live.
        bridge = _MaterialBridge()
        manager = MaterialSessionManager(bridge.call)
        prepared = manager.prepare(10, "fragment", 0, CAPTURE_ID)
        manager.update(prepared["sessionId"], "roughness", 0.25)
        bridge.malformed_apply = True
        with self.assertRaises(MaterialSessionError):
            manager.update(prepared["sessionId"], "roughness", 0.75)
        rollback = [
            params
            for method, params in bridge.calls
            if method == "rollback_event_cbuffer_override"
        ][-1]
        self.assertEqual(rollback["override_id"], "")
        self.assertEqual(bridge.override_state, {})
        self.assertEqual(bridge.snapshot_keys, {})
        with self.assertRaises(MaterialSessionError):
            manager.status(prepared["sessionId"])

    def test_capture_switch_expires_sessions(self) -> None:
        prepared = self.prepare()
        self.bridge.capture_session_id = "7" * 32
        with self.assertRaisesRegex(MaterialSessionError, "expired capture"):
            self.manager.update(prepared["sessionId"], "roughness", 0.5)
        with self.assertRaisesRegex(MaterialSessionError, "Unknown or expired"):
            self.manager.status(prepared["sessionId"])
        self.assertEqual(self.bridge.apply_requests, [])

    def test_rollback_clears_override_and_releases_snapshot(self) -> None:
        prepared = self.prepare()
        applied = self.manager.update(prepared["sessionId"], "roughness", 0.25)
        result = self.manager.rollback(prepared["sessionId"])
        self.assertEqual(result["schema"], MATERIAL_SCHEMA)
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["operation"], "rollback")
        self.assertFalse(result["applied"])
        self.assertTrue(result["override"]["cleared"])
        self.assertTrue(result["snapshotReleased"])
        self.assertFalse(result["alreadyRestored"])
        self.assertFalse(result["idempotentRollback"])
        self.assertEqual(
            [name for name, _ in self.bridge.calls].count(
                "rollback_event_cbuffer_override"
            ),
            1,
        )
        with self.assertRaises(MaterialSessionError):
            self.manager.status(applied["sessionId"])

        repeated = self.manager.rollback(prepared["sessionId"])
        self.assertEqual(repeated["schema"], MATERIAL_SCHEMA)
        self.assertEqual(repeated["schemaVersion"], 1)
        self.assertEqual(repeated["operation"], "rollback")
        self.assertEqual(repeated["sessionId"], prepared["sessionId"])
        self.assertEqual(repeated["captureId"], CAPTURE_ID)
        self.assertEqual(repeated["captureSessionId"], CAPTURE_SESSION_ID)
        self.assertFalse(repeated["applied"])
        self.assertTrue(repeated["snapshotReleased"])
        self.assertTrue(repeated["alreadyRestored"])
        self.assertTrue(repeated["idempotentRollback"])
        self.assertEqual(repeated["override"], result["override"])
        self.assertEqual(
            [name for name, _ in self.bridge.calls].count(
                "rollback_event_cbuffer_override"
            ),
            1,
        )

    def test_bridge_rollback_response_loss_can_be_retried_strictly(self) -> None:
        prepared = self.prepare()
        self.manager.update(prepared["sessionId"], "roughness", 0.25)
        self.bridge.lose_next_rollback_response = True

        with self.assertRaisesRegex(TimeoutError, "response was lost"):
            self.manager.rollback(prepared["sessionId"])

        # The host retains the exact session capability. The bridge's bounded
        # snapshot tombstone then proves that this same capture-bound rollback
        # already completed; no token or physical identity is reconstructed.
        recovered = self.manager.rollback(prepared["sessionId"])
        self.assertEqual(recovered["schema"], MATERIAL_SCHEMA)
        self.assertEqual(recovered["operation"], "rollback")
        self.assertEqual(recovered["sessionId"], prepared["sessionId"])
        self.assertEqual(recovered["captureId"], CAPTURE_ID)
        self.assertEqual(recovered["captureSessionId"], CAPTURE_SESSION_ID)
        self.assertFalse(recovered["applied"])
        self.assertTrue(recovered["snapshotReleased"])
        self.assertTrue(recovered["alreadyRestored"])
        self.assertTrue(recovered["idempotentRollback"])
        self.assertTrue(recovered["override"]["alreadyReleased"])
        self.assertEqual(
            [name for name, _ in self.bridge.calls].count(
                "rollback_event_cbuffer_override"
            ),
            2,
        )

        repeated = self.manager.rollback(prepared["sessionId"])
        self.assertTrue(repeated["alreadyRestored"])
        self.assertTrue(repeated["idempotentRollback"])
        self.assertEqual(
            [name for name, _ in self.bridge.calls].count(
                "rollback_event_cbuffer_override"
            ),
            2,
        )

    def test_rollback_tombstone_rejects_wrong_binding_and_unknown_token(self) -> None:
        prepared = self.prepare()
        self.manager.rollback(prepared["sessionId"])
        rollback_calls = [
            name for name, unused in self.bridge.calls
            if name == "rollback_event_cbuffer_override"
        ]

        with self.assertRaisesRegex(MaterialSessionError, "Unknown or expired"):
            self.manager.rollback("f" * 32)
        self.assertEqual(
            [
                name for name, unused in self.bridge.calls
                if name == "rollback_event_cbuffer_override"
            ],
            rollback_calls,
        )

        self.bridge.capture_session_id = "7" * 32
        with self.assertRaisesRegex(MaterialSessionError, "expired capture"):
            self.manager.rollback(prepared["sessionId"])
        with self.assertRaisesRegex(MaterialSessionError, "Unknown or expired"):
            self.manager.rollback(prepared["sessionId"])

        other_bridge = _MaterialBridge()
        other_manager = MaterialSessionManager(other_bridge.call)
        other = other_manager.prepare(10, "fragment", 0, CAPTURE_ID)
        other_manager.rollback(other["sessionId"])
        other_bridge.capture_id = "d" * 64
        with self.assertRaisesRegex(MaterialSessionError, "different capture"):
            other_manager.rollback(other["sessionId"])
        with self.assertRaisesRegex(MaterialSessionError, "Unknown or expired"):
            other_manager.rollback(other["sessionId"])

    def test_rollback_tombstones_are_bounded_and_evict_oldest(self) -> None:
        bridge = _MaterialBridge()
        manager = MaterialSessionManager(
            bridge.call, maximum_rollback_tombstones=2
        )
        session_ids = []
        for event_id in (10, 11, 12):
            prepared = manager.prepare(event_id, "fragment", 0, CAPTURE_ID)
            session_ids.append(prepared["sessionId"])
            manager.rollback(prepared["sessionId"])

        with self.assertRaisesRegex(MaterialSessionError, "Unknown or expired"):
            manager.rollback(session_ids[0])
        for session_id in session_ids[1:]:
            repeated = manager.rollback(session_id)
            self.assertTrue(repeated["alreadyRestored"])
            self.assertTrue(repeated["idempotentRollback"])
        self.assertEqual(
            [name for name, unused in bridge.calls].count(
                "rollback_event_cbuffer_override"
            ),
            3,
        )

    def test_status_revalidates_live_bridge_revision_and_hash(self) -> None:
        prepared = self.prepare()
        applied = self.manager.update(prepared["sessionId"], "roughness", 0.25)
        status = self.manager.status(prepared["sessionId"])
        self.assertEqual(status["revision"], applied["revision"])
        self.assertEqual(
            [name for name, _ in self.bridge.calls].count(
                "get_event_cbuffer_override_status"
            ),
            1,
        )

        self.bridge.override_state[next(iter(self.bridge.override_state))][
            "currentRawSha256"
        ] = "0" * 64
        with self.assertRaisesRegex(MaterialSessionError, "no longer matches"):
            self.manager.status(prepared["sessionId"])
        with self.assertRaises(MaterialSessionError):
            self.manager.status(prepared["sessionId"])

    def test_rollback_without_update_releases_snapshot_without_clear(self) -> None:
        prepared = self.prepare()
        result = self.manager.rollback(prepared["sessionId"])
        self.assertFalse(result["override"]["cleared"])
        self.assertTrue(result["override"]["snapshotReleased"])

    def test_explicit_clear_all_recovers_lost_or_foreign_session_state(self) -> None:
        first = self.prepare(10)
        second = self.prepare(11)
        self.manager.update(first["sessionId"], "roughness", 0.5)
        result = self.manager.clear_all(CAPTURE_ID)
        self.assertTrue(result["cleared"])
        self.assertEqual(result["clearedOverrideCount"], 1)
        self.assertEqual(result["releasedSnapshotCount"], 2)
        for session_id in (first["sessionId"], second["sessionId"]):
            with self.assertRaises(MaterialSessionError):
                self.manager.status(session_id)

        # A fresh process can prepare the formerly-owned physical key only
        # after this explicit capture-bound recovery operation.
        other = MaterialSessionManager(self.bridge.call)
        recovered = other.prepare(10, "fragment", 0, CAPTURE_ID)
        self.assertEqual(recovered["eventId"], 10)

    def test_clear_all_confirms_active_and_already_restored_sessions(self) -> None:
        first = self.prepare(10)
        second = self.prepare(11)
        first_receipt = self.manager.rollback(first["sessionId"])

        cleared = self.manager.clear_all(CAPTURE_ID)
        self.assertEqual(cleared["restoredSessionCount"], 1)
        self.assertEqual(cleared["restoredSessionIds"], [second["sessionId"]])
        self.assertEqual(cleared["alreadyRestoredSessionCount"], 1)
        self.assertEqual(
            cleared["alreadyRestoredSessionIds"], [first["sessionId"]]
        )

        first_again = self.manager.rollback(first["sessionId"])
        self.assertEqual(first_again["restoredBy"], "rollback")
        self.assertEqual(first_again["override"], first_receipt["override"])
        second_again = self.manager.rollback(second["sessionId"])
        self.assertEqual(second_again["restoredBy"], "clearAll")
        self.assertEqual(second_again["clearAll"]["schema"], CLEAR_ALL_SCHEMA)
        for receipt in (first_again, second_again):
            self.assertEqual(receipt["schema"], MATERIAL_SCHEMA)
            self.assertEqual(receipt["schemaVersion"], 1)
            self.assertEqual(receipt["operation"], "rollback")
            self.assertFalse(receipt["applied"])
            self.assertTrue(receipt["snapshotReleased"])
            self.assertTrue(receipt["alreadyRestored"])
            self.assertTrue(receipt["idempotentRollback"])

    def test_bridge_source_remains_python_36_parseable(self) -> None:
        source = (
            REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        ).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 6))

    def test_mcp_tools_never_accept_offsets_registers_or_raw_bytes(self) -> None:
        source = (REPOSITORY / "mcp_host" / "src" / "server.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        expected = {
            "prepare_material_session": [
                "event_id",
                "constant_block_index",
                "stage",
                "expected_capture_id",
            ],
            "list_material_sessions": [],
            "get_material_session_status": ["session_id"],
            "update_material_parameter": ["session_id", "parameter", "value"],
            "update_material_parameters": ["session_id", "updates"],
            "rollback_material_session": ["session_id"],
            "clear_all_material_sessions": ["expected_capture_id"],
        }
        for name, arguments in expected.items():
            self.assertIn(name, functions)
            self.assertEqual(
                [argument.arg for argument in functions[name].args.args], arguments
            )
        forbidden = {
            "offset",
            "byte_offset",
            "slot",
            "register",
            "base_type",
            "packed_bytes",
            "raw_bytes",
            "data_base64",
        }
        for name in expected:
            self.assertTrue(
                forbidden.isdisjoint(
                    argument.arg for argument in functions[name].args.args
                )
            )


class BridgeCBufferContractTests(unittest.TestCase):
    def test_real_resource_ids_dxbc_strides_ring_ranges_alias_and_lifecycle(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")

        class ResourceId:
            __slots__ = ("_value",)

            def __init__(self, value=0):
                object.__setattr__(self, "_value", value)

            def __setattr__(self, name, value):
                raise AttributeError("ResourceId is read-only")

            def __int__(self):
                return self._value

        fake_renderdoc.ResourceId = ResourceId
        fake_renderdoc.ShaderStage = types.SimpleNamespace(
            Vertex="Vertex",
            Hull="Hull",
            Domain="Domain",
            Geometry="Geometry",
            Fragment="Fragment",
            Compute="Compute",
        )
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_cbuffer_contract", bridge_path
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        shader_resource = ResourceId(77)
        buffer_resource = ResourceId(44)
        shader_bytes = b"captured DXBC shader"
        shader_hash = hashlib.sha256(shader_bytes).hexdigest()

        def scalar_type(elements=1):
            # These non-zero strides are the real DXBC reflection convention,
            # including for an ordinary non-array scalar/vector.
            return types.SimpleNamespace(
                name="",
                baseType="VarType.Float",
                rows=1,
                columns=1,
                elements=elements,
                arrayByteStride=16,
                matrixByteStride=16,
                flags=0,
                pointerTypeID=0xFFFFFFFF,
                members=[],
            )

        vector_type = types.SimpleNamespace(
            name="",
            baseType="VarType.Float",
            rows=1,
            columns=3,
            elements=1,
            arrayByteStride=16,
            matrixByteStride=16,
            flags=0,
            pointerTypeID=0xFFFFFFFF,
            members=[],
        )
        constants = [
            types.SimpleNamespace(
                name="roughness",
                byteOffset=0,
                bitFieldOffset=0,
                bitFieldSize=0,
                type=scalar_type(),
            ),
            types.SimpleNamespace(
                name="tint",
                byteOffset=4,
                bitFieldOffset=0,
                bitFieldSize=0,
                type=vector_type,
            ),
            types.SimpleNamespace(
                name="weights",
                byteOffset=16,
                bitFieldOffset=0,
                bitFieldSize=0,
                type=scalar_type(elements=2),
            ),
        ]
        block = types.SimpleNamespace(
            name="cbuffer0",
            fixedBindNumber=3,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=48,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=constants,
        )
        reflection = types.SimpleNamespace(
            entryPoint="main",
            rawBytes=shader_bytes,
            interfaces=[],
            constantBlocks=[block],
        )

        class Pipeline:
            def __init__(self, controller):
                self.controller = controller

            def GetShader(self, stage):
                return shader_resource

            def GetShaderEntryPoint(self, stage):
                return "main"

            def GetConstantBlock(self, stage, block_index, array_index):
                self.controller.constant_block_indices.append(block_index)
                if block_index != 0 or array_index != 0:
                    raise AssertionError("reflection block index was replaced by b-slot")
                offset, size = self.controller.ranges[self.controller.current_event]
                descriptor = types.SimpleNamespace(
                    resource=buffer_resource,
                    byteOffset=offset,
                    byteSize=size,
                )
                return types.SimpleNamespace(descriptor=descriptor)

        class Controller:
            def __init__(self):
                self.current_event = 5
                self.ranges = {10: (65536, 48), 11: (131072, 32), 5: (0, 48)}
                self.pipeline = Pipeline(self)
                self.constant_block_indices = []
                self.setter_calls = []
                self.clear_calls = []
                self.clear_all_calls = 0

            def GetAPIProperties(self):
                return types.SimpleNamespace(
                    pipelineType="GraphicsAPI.D3D11", remoteReplay=False
                )

            def SetFrameEvent(self, event_id, force):
                self.current_event = event_id

            def GetRootActions(self):
                # Material editing is authorised only for replayable actions.
                # Model every ring-buffer fixture EID as an actual draw.
                return [
                    types.SimpleNamespace(
                        eventId=event_id, flags=0x0002, children=[]
                    )
                    for event_id in sorted(self.ranges)
                ]

            def GetPipelineState(self):
                return self.pipeline

            def GetShaderEntryPoints(self, resource):
                self.assert_real(resource, shader_resource)
                return [types.SimpleNamespace(name="main")]

            def GetShader(self, pipeline, resource, entry):
                self.assert_real(resource, shader_resource)
                return reflection

            def GetD3D11PipelineState(self):
                return types.SimpleNamespace(
                    pixelShader=types.SimpleNamespace(
                        resourceId=shader_resource, classInstances=[]
                    )
                )

            def GetVulkanPipelineState(self):
                raise RuntimeError("not Vulkan")

            def GetD3D12PipelineState(self):
                raise RuntimeError("not D3D12")

            def GetBuffers(self):
                return [
                    types.SimpleNamespace(
                        resourceId=buffer_resource,
                        length=131104,
                    )
                ]

            def GetBufferData(self, resource, offset, length):
                self.assert_real(resource, buffer_resource)
                return bytes((offset + index) % 251 for index in range(length))

            def SetEventCBufferOverride(
                self,
                event_id,
                stage,
                slot,
                expected_shader,
                expected_buffer,
                byte_offset,
                byte_size,
                data,
                register_space=0,
            ):
                self.assert_real(expected_shader, shader_resource)
                self.assert_real(expected_buffer, buffer_resource)
                if slot != 3:
                    raise AssertionError("setter did not use fixedBindNumber")
                if register_space != 0:
                    raise AssertionError("D3D11 setter did not preserve register space 0")
                self.setter_calls.append(
                    (event_id, stage, slot, byte_offset, byte_size, bytes(data), register_space)
                )
                return ""

            def ClearEventCBufferOverride(self, event_id, stage, slot, register_space=0):
                self.clear_calls.append((event_id, stage, slot, register_space))

            def ClearEventCBufferOverrides(self):
                self.clear_all_calls += 1

            @staticmethod
            def assert_real(actual, expected):
                if actual is not expected:
                    raise AssertionError("bridge fabricated a ResourceId")

        controller = Controller()
        capture_path = "D:/captures/frame.rdc"
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: controller.current_event,
            GetCaptureFilename=lambda: capture_path,
        )
        canonical_path = os.path.normcase(os.path.abspath(capture_path))
        module._known_catalog_capture_ids = {CAPTURE_ID: canonical_path}
        module._capture_session_id = CAPTURE_SESSION_ID
        module._active_capture_token = ("loaded", canonical_path)
        module._replay = lambda callback: callback(controller)
        module._symbol_alias_overlay = {
            "aliases": [
                {
                    "kind": "constantBuffer",
                    "shaderResourceId": 77,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": shader_hash,
                    "bindNumber": 3,
                    "registerSpace": 0,
                    "displayName": "材质参数",
                    "verified": True,
                    "provenance": "program",
                    "metadata": {"programVerified": True},
                },
                {
                    "kind": "constantField",
                    "shaderResourceId": 77,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": shader_hash,
                    "bindNumber": 3,
                    "registerSpace": 0,
                    "byteOffset": 0,
                    "displayName": "真实粗糙度",
                    "verified": True,
                    "provenance": "program",
                    "metadata": {"programVerified": True},
                },
            ]
        }
        base_params = {
            "expected_capture_id": CAPTURE_ID,
            "event_id": 10,
            "stage": "fragment",
            "block_index": 0,
        }
        first = module._snapshot_event_cbuffer(dict(base_params))
        self.assertEqual(first["slot"], 3)
        self.assertEqual(first["byteOffset"], 65536)
        self.assertEqual(first["validByteSize"], 48)
        self.assertEqual(first["constantBlockPath"], "cbuffer0")
        self.assertEqual(first["constantBlockDisplayName"], "材质参数")
        self.assertEqual(
            [field["path"] for field in first["fields"]],
            ["roughness", "tint", "weights[0]", "weights[1]"],
        )
        roughness = first["fields"][0]
        self.assertEqual(roughness["displayName"], "真实粗糙度")
        expected_leaf_id = material_leaf_id(
            path="roughness",
            base_type="Float",
            components=1,
            array_index=0,
            byte_offset=0,
            byte_size=4,
        )
        self.assertEqual(roughness["leafId"], expected_leaf_id)

        with self.assertRaisesRegex(RuntimeError, "already has"):
            module._snapshot_event_cbuffer(dict(base_params))
        self.assertEqual(
            module._get_event_cbuffer_override_status(
                {
                    "snapshot_id": first["snapshotId"],
                    "expected_capture_id": CAPTURE_ID,
                    "expected_capture_session_id": CAPTURE_SESSION_ID,
                }
            )["overrideId"],
            None,
        )

        second_params = dict(base_params)
        second_params["event_id"] = 11
        second = module._snapshot_event_cbuffer(second_params)
        self.assertEqual(second["bufferResourceId"], first["bufferResourceId"])
        self.assertEqual(second["byteOffset"], 131072)
        self.assertEqual(second["byteSize"], 32)

        baseline = base64.b64decode(first["rawBase64"])
        patched = bytearray(baseline)
        packed = struct.pack("<f", 0.75)
        patched[0:4] = packed
        patch_params = {
            "snapshot_id": first["snapshotId"],
            "expected_capture_id": CAPTURE_ID,
            "expected_capture_session_id": CAPTURE_SESSION_ID,
            "expected_raw_sha256": first["rawSha256"],
            "expected_layout_sha256": first["layoutSha256"],
            "expected_override_id": "",
            "expected_revision": 0,
            "new_revision": 1,
            "data_base64": base64.b64encode(bytes(patched)).decode("ascii"),
            "data_sha256": hashlib.sha256(bytes(patched)).hexdigest(),
            "patches": [
                {
                    "leafId": roughness["leafId"],
                    "packedBase64": base64.b64encode(packed).decode("ascii"),
                }
            ],
        }
        applied = module._apply_event_cbuffer_override(patch_params)
        self.assertTrue(applied["appliedToCurrentEvent"])
        self.assertEqual(controller.setter_calls[-1][2], 3)
        self.assertEqual(controller.setter_calls[-1][3], 65536)
        status = module._get_event_cbuffer_override_status(
            {
                "snapshot_id": first["snapshotId"],
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertEqual(status["revision"], 1)
        self.assertEqual(status["overrideId"], applied["overrideId"])

        rollback_params = {
            "snapshot_id": first["snapshotId"],
            "override_id": applied["overrideId"],
            "expected_capture_id": CAPTURE_ID,
            "expected_capture_session_id": CAPTURE_SESSION_ID,
        }
        rolled_back = module._rollback_event_cbuffer_override(rollback_params)
        self.assertTrue(rolled_back["cleared"])
        self.assertTrue(rolled_back["snapshotReleased"])
        repeated = module._rollback_event_cbuffer_override(rollback_params)
        self.assertTrue(repeated["alreadyReleased"])

        cleared = module._clear_all_event_cbuffer_snapshots(
            {
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertEqual(cleared["releasedSnapshotCount"], 1)
        self.assertEqual(cleared["clearedOverrideCount"], 0)
        self.assertEqual(controller.clear_all_calls, 1)


if __name__ == "__main__":
    unittest.main()
