"""ReplayLab native material-panel model for shared CBuffer field edits.

The C++ BufferViewer supplies a selection from its current reflected variable
model.  This module never accepts a byte offset from an MCP/model caller: the
selection is immediately checked against a fresh bridge-owned CBuffer snapshot,
and subsequent edits use only the leaf ID issued by that snapshot.

The controller is deliberately independent from RenderDoc and Qt so its physical
identity, packing, cumulative-patch, revision, and rollback rules can be tested
with fixed data. The localized MiniQt UI lives in ``project_material_panel.py``.

Python 3.6 compatibility is required by the bundled qrenderdoc runtime.
"""

from __future__ import absolute_import

import base64
import collections
import hashlib
import math
import struct
import importlib.util
import os

try:
    from .material_edit_queue import MaterialEditQueue
except (ImportError, ValueError):
    _queue_spec = importlib.util.spec_from_file_location(
        "_replaylab_material_edit_queue", os.path.join(os.path.dirname(__file__), "material_edit_queue.py"))
    _queue_module = importlib.util.module_from_spec(_queue_spec)
    _queue_spec.loader.exec_module(_queue_module)
    MaterialEditQueue = _queue_module.MaterialEditQueue

try:
    from .shared_cbuffer import MAX_SHARED_CBUFFER_BYTES
except (ImportError, ValueError):
    _shared_spec = importlib.util.spec_from_file_location(
        "_replaylab_shared_cbuffer_limits", os.path.join(os.path.dirname(__file__), "shared_cbuffer.py"))
    _shared_module = importlib.util.module_from_spec(_shared_spec)
    _shared_spec.loader.exec_module(_shared_module)
    MAX_SHARED_CBUFFER_BYTES = _shared_module.MAX_SHARED_CBUFFER_BYTES


SELECTION_SCHEMA = "renderdoc-ultra-buffer-variable-selection/v1"
SNAPSHOT_SCHEMA = "renderdoc-ultra-shared-cbuffer-snapshot/v1"
OVERRIDE_SCHEMA = "renderdoc-ultra-shared-cbuffer-edit/v1"
_SHA256_CHARS = set("0123456789abcdef")


class MaterialPanelError(RuntimeError):
    def __init__(self, message, parameter=None):
        super(MaterialPanelError, self).__init__(message)
        self.message_id = message if message.startswith("material-value-") else ""
        self.parameter = parameter


def _integer(value, label, minimum=0, maximum=0xFFFFFFFFFFFFFFFF):
    if isinstance(value, bool):
        raise MaterialPanelError("%s must be an integer" % label)
    try:
        result = int(value)
    except Exception:
        raise MaterialPanelError("%s must be an integer" % label)
    if result < minimum or result > maximum:
        raise MaterialPanelError(
            "%s must be between %d and %d" % (label, minimum, maximum))
    return result


def _text(value, label, maximum=4096):
    result = str(value or "")
    if len(result) > maximum:
        raise MaterialPanelError("%s is too long" % label)
    return result


def _hex(value, label, length):
    result = _text(value, label, length).strip().lower()
    if len(result) != length or any(char not in _SHA256_CHARS for char in result):
        raise MaterialPanelError("%s must be %d lowercase hex characters" % (label, length))
    return result


def _sha256(value, label):
    return _hex(value, label, 64)


def _canonical_base_type(value):
    token = str(value or "").strip().replace("_", "").replace(" ", "")
    token = token.split(".")[-1].lower()
    result = {
        "float": "Float",
        "sint": "SInt",
        "int": "SInt",
        "uint": "UInt",
    }.get(token)
    if not result:
        raise MaterialPanelError("Only Float, SInt, and UInt parameters are supported")
    return result


def native_selection_from_callback(data, resource_id_int, normalise_stage):
    """Convert only qrenderdoc's native callback payload to a strict selection.

    This function is intentionally not an MCP method.  ``resource_id_int`` and
    ``normalise_stage`` are supplied by the bridge, so RenderDoc wrapper objects
    are canonicalised by the same fixed code as every replay mutation.
    """
    # The native callback ABI is rdcarray<rdcpair<rdcstr, PyObject *>>. The bundled
    # SWIG converter delivers a list of pairs; some extension runtimes provide a
    # dict. Accept both without silently dropping duplicate identity fields.
    if isinstance(data, (list, tuple)):
        converted = {}
        for item in data:
            if (not isinstance(item, (list, tuple)) or len(item) != 2 or
                    not isinstance(item[0], str) or item[0] in converted):
                raise MaterialPanelError("The BufferViewer callback has ambiguous identity fields")
            converted[item[0]] = item[1]
        data = converted
    if not isinstance(data, dict):
        raise MaterialPanelError("The BufferViewer callback did not provide an identity object")
    if str(data.get("schema", "")) != SELECTION_SCHEMA:
        raise MaterialPanelError("Unsupported BufferViewer variable-selection schema")

    stage = str(normalise_stage(data.get("stageName", "")) or "")
    if stage not in ("vertex", "hull", "domain", "geometry", "fragment", "compute",
                     "task", "mesh"):
        raise MaterialPanelError("The selected Shader stage is not supported by materials")

    shader_resource_id = _integer(
        resource_id_int(data.get("shaderResourceId")), "shader resource ID", 1)
    buffer_resource_id = _integer(
        resource_id_int(data.get("bufferResourceId")), "buffer resource ID", 1)
    result = {
        "schema": SELECTION_SCHEMA,
        "eventId": _integer(data.get("eventId"), "event ID", 1, 0xFFFFFFFF),
        "stage": stage,
        "blockIndex": _integer(data.get("blockIndex"), "reflection block index", 0, 16383),
        "arrayIndex": _integer(data.get("arrayIndex"), "binding array index", 0, 0xFFFFFFFF),
        "physicalSlot": _integer(data.get("physicalSlot"), "CBuffer slot", 0, 0xFFFFFFFF),
        "registerSpace": _integer(data.get("registerSpace"), "register space", 0, 0xFFFFFFFF),
        "blockName": _text(data.get("blockName"), "constant-block name"),
        "entryPoint": _text(data.get("entryPoint"), "Shader entry point"),
        "shaderResourceId": shader_resource_id,
        "bufferResourceId": buffer_resource_id,
        "bufferByteOffset": _integer(
            data.get("bufferByteOffset"), "bound buffer byte offset", 0, 0xFFFFFFFFFFFFFFFF),
        "bufferByteSize": _integer(
            data.get("bufferByteSize"), "bound buffer byte size", 4, MAX_SHARED_CBUFFER_BYTES),
        "fieldName": _text(data.get("fieldName"), "reflected field name"),
        "fieldByteOffset": _integer(
            data.get("fieldByteOffset"), "reflected field byte offset", 0, MAX_SHARED_CBUFFER_BYTES - 4),
        "fieldBaseType": _canonical_base_type(data.get("fieldBaseType")),
        "fieldComponents": _integer(data.get("fieldComponents"), "field components", 1, 4),
    }
    if result["fieldByteOffset"] + result["fieldComponents"] * 4 > result["bufferByteSize"]:
        raise MaterialPanelError("The selected reflected field lies outside its bound window")
    return result


def _decode_raw_base64(value, expected_size):
    if not isinstance(value, str):
        raise MaterialPanelError("CBuffer snapshot raw bytes are missing")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        raise MaterialPanelError("CBuffer snapshot raw bytes are not canonical base64")
    if base64.b64encode(decoded) != encoded or len(decoded) != expected_size:
        raise MaterialPanelError("CBuffer snapshot raw bytes have the wrong size")
    return bytes(decoded)


def _leaf_values(raw, leaf):
    start = leaf["byteOffset"]
    end = start + leaf["byteSize"]
    data = raw[start:end]
    fmt = {"Float": "f", "SInt": "i", "UInt": "I"}[leaf["baseType"]]
    return list(struct.unpack("<%d%s" % (leaf["components"], fmt), data))


def _pack_values(leaf, value):
    components = leaf["components"]
    if isinstance(value, (list, tuple)):
        values = list(value)
    elif components == 1:
        values = [value]
    else:
        raise MaterialPanelError("A vector parameter requires %d components" % components)
    if len(values) != components:
        raise MaterialPanelError("Expected %d components, received %d" % (components, len(values)))

    base_type = leaf["baseType"]
    packed_values = []
    for item in values:
        if isinstance(item, bool):
            raise MaterialPanelError("Boolean values are not valid for this reflected type")
        if base_type == "Float":
            try:
                number = float(item)
            except Exception:
                raise MaterialPanelError("Float parameters require finite numbers")
            if not math.isfinite(number):
                raise MaterialPanelError("NaN and infinity are not valid material values")
            packed_values.append(number)
        else:
            if isinstance(item, float) and not item.is_integer():
                raise MaterialPanelError("Integer parameters require whole numbers")
            try:
                number = int(item)
            except Exception:
                raise MaterialPanelError("Integer parameters require whole numbers")
            minimum, maximum = ((-0x80000000, 0x7FFFFFFF)
                                if base_type == "SInt" else (0, 0xFFFFFFFF))
            if number < minimum or number > maximum:
                raise MaterialPanelError("Integer value is outside the reflected 32-bit range")
            packed_values.append(number)

    fmt = {"Float": "f", "SInt": "i", "UInt": "I"}[base_type]
    try:
        packed = struct.pack("<%d%s" % (components, fmt), *packed_values)
    except (OverflowError, struct.error):
        raise MaterialPanelError("Value cannot be represented by the reflected 32-bit type")
    if base_type == "Float":
        unpacked = struct.unpack("<%df" % components, packed)
        if any(not math.isfinite(number) for number in unpacked):
            raise MaterialPanelError("Float value overflows finite 32-bit storage")
    return packed


def parse_parameter_text(text, base_type, components):
    pieces = [piece.strip() for piece in str(text or "").split(",")]
    if len(pieces) != components or any(piece == "" for piece in pieces):
        raise MaterialPanelError("material-value-component-count", components)
    if base_type == "Float":
        try:
            return [float(piece) for piece in pieces]
        except Exception:
            raise MaterialPanelError("material-value-float-required")
    try:
        return [int(piece, 10) for piece in pieces]
    except Exception:
        raise MaterialPanelError("material-value-integer-required")


def _stage_abbreviation(stage, api="D3D11"):
    if api in ("OpenGL", "Vulkan"):
        return {"vertex": "VTX", "hull": "TCS", "domain": "TES", "geometry": "GS",
                "fragment": "FS", "compute": "CS", "task": "TS", "mesh": "MS"}.get(stage, stage.upper())
    return {
        "vertex": "VS", "hull": "HS", "domain": "DS", "geometry": "GS",
        "fragment": "PS", "compute": "CS", "task": "AS", "mesh": "MS",
    }.get(stage, stage.upper())


def _context_api_token(value):
    token = str(value or "").strip()
    canonical = token.lower().replace("graphicsapi.", "").replace("direct3d", "d3d")
    return token, canonical


class MaterialPanelController(object):
    """Own UI-selected reflected field edits shared by every buffer consumer."""

    def __init__(self, snapshot_fn, apply_fn, rollback_fn, context_fn, apply_async_fn=None):
        self._snapshot = snapshot_fn
        self._apply = apply_fn
        self._rollback = rollback_fn
        self._context = context_fn
        self._sessions = collections.OrderedDict()
        self._physical_keys = {}
        self._apply_async = apply_async_fn
        self.edit_queue = MaterialEditQueue(self._dispatch_edit)

    def _current_context(self):
        value = self._context()
        if not isinstance(value, dict):
            raise MaterialPanelError("Bread current context is unavailable")
        capture = value.get("capture") if isinstance(value.get("capture"), dict) else {}
        events = value.get("events") if isinstance(value.get("events"), dict) else {}
        if not capture.get("loaded"):
            raise MaterialPanelError("No RenderDoc capture is loaded")
        api_name, api_token = _context_api_token(capture.get("api"))
        if not api_token:
            raise MaterialPanelError(
                "The capture API is unavailable; Bread material editing cannot be enabled "
                "safely")
        if api_token not in ("d3d11", "d3d12", "opengl", "vulkan"):
            raise MaterialPanelError(
                "Current capture API does not support shared CBuffer editing: %s" % api_name)
        if not str(capture.get("captureId", "") or ""):
            raise MaterialPanelError(
                "The capture has no Bread catalog identity yet; run the project's initial "
                "capture analysis before exposing material parameters")
        capture_id = _sha256(capture.get("captureId"), "capture ID")
        capture_session_id = _hex(
            capture.get("captureSessionId"), "capture session ID", 32)
        return {
            "captureId": capture_id,
            "captureSessionId": capture_session_id,
            "eventId": _integer(events.get("currentEventId"), "current event ID", 1),
            "api": {"d3d11": "D3D11", "d3d12": "D3D12", "opengl": "OpenGL",
                    "vulkan": "Vulkan"}[api_token],
        }

    @staticmethod
    def _normalise_field(value):
        if not isinstance(value, dict):
            raise MaterialPanelError("CBuffer snapshot contains an invalid field")
        leaf = {
            "leafId": _hex(value.get("leafId"), "CBuffer leaf ID", 32),
            "path": _text(value.get("path"), "CBuffer field path"),
            "displayName": _text(
                value.get("displayName", value.get("path")), "CBuffer display name"),
            "byteOffset": _integer(value.get("byteOffset"), "field byte offset", 0,
                                   MAX_SHARED_CBUFFER_BYTES - 4),
            "byteSize": _integer(value.get("byteSize"), "field byte size", 4, 16),
            "baseType": _canonical_base_type(value.get("baseType")),
            "components": _integer(value.get("components"), "field components", 1, 4),
            "editable": bool(value.get("editable", False)),
        }
        if not leaf["path"] or leaf["byteSize"] != leaf["components"] * 4:
            raise MaterialPanelError("CBuffer snapshot field layout is inconsistent")
        if not leaf["editable"]:
            raise MaterialPanelError("CBuffer snapshot issued a non-editable field")
        return leaf

    def _release_unadopted_snapshot(self, snapshot):
        try:
            self._rollback({
                "snapshot_id": snapshot["snapshotId"],
                "override_id": "",
                "expected_capture_id": snapshot["captureId"],
                "expected_capture_session_id": snapshot["captureSessionId"],
            })
        except Exception:
            pass

    def expose(self, selection):
        if not isinstance(selection, dict) or selection.get("schema") != SELECTION_SCHEMA:
            raise MaterialPanelError("Only a native BufferViewer variable selection can be exposed")
        current = self._current_context()
        if selection["eventId"] != current["eventId"]:
            raise MaterialPanelError("The BufferViewer selection is stale; select the draw again")

        physical_key = (
            current["captureSessionId"],
            selection["bufferResourceId"], selection["bufferByteOffset"],
            selection["bufferByteSize"],
        )
        session_id = self._physical_keys.get(physical_key)
        session = self._sessions.get(session_id) if session_id else None

        if session is None:
            snapshot = self._snapshot({
                "event_id": selection["eventId"],
                "block_index": selection["blockIndex"],
                "array_index": selection["arrayIndex"],
                "stage": selection["stage"],
                "expected_capture_id": current["captureId"],
            })
            if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
                raise MaterialPanelError("Bread returned an invalid CBuffer snapshot")
            try:
                checks = (
                    ("captureId", _sha256(snapshot.get("captureId"), "snapshot capture ID"),
                     current["captureId"]),
                    ("captureSessionId", _hex(snapshot.get("captureSessionId"),
                                              "snapshot capture session ID", 32),
                     current["captureSessionId"]),
                    ("eventId", _integer(snapshot.get("eventId"), "snapshot event ID", 1),
                     selection["eventId"]),
                    ("stage", str(snapshot.get("stage", "")), selection["stage"]),
                    ("blockIndex", _integer(snapshot.get("blockIndex"),
                                            "snapshot block index"), selection["blockIndex"]),
                    ("arrayIndex", _integer(snapshot.get("arrayIndex", 0),
                                            "snapshot binding array index"), selection["arrayIndex"]),
                    ("slot", _integer(snapshot.get("slot"), "snapshot slot", 0, 0xFFFFFFFF),
                     selection["physicalSlot"]),
                    ("registerSpace", _integer(snapshot.get("registerSpace", 0),
                                                "snapshot register space", 0, 0xFFFFFFFF),
                     selection["registerSpace"]),
                    ("shaderResourceId", _integer(snapshot.get("shaderResourceId"),
                                                  "snapshot shader resource ID", 1),
                     selection["shaderResourceId"]),
                    ("bufferResourceId", _integer(snapshot.get("bufferResourceId"),
                                                  "snapshot buffer resource ID", 1),
                     selection["bufferResourceId"]),
                    ("byteOffset", _integer(snapshot.get("byteOffset"),
                                            "snapshot byte offset"),
                     selection["bufferByteOffset"]),
                    ("byteSize", _integer(snapshot.get("byteSize"),
                                          "snapshot byte size", 4, MAX_SHARED_CBUFFER_BYTES),
                     selection["bufferByteSize"]),
                )
                for label, observed, expected in checks:
                    if observed != expected:
                        raise MaterialPanelError(
                            "The live CBuffer %s no longer matches the native selection" % label)

                valid_size = _integer(
                    snapshot.get("validByteSize"), "valid CBuffer byte size", 4,
                    MAX_SHARED_CBUFFER_BYTES)
                baseline = _decode_raw_base64(snapshot.get("rawBase64"), valid_size)
                raw_sha = _sha256(snapshot.get("rawSha256"), "snapshot raw hash")
                if hashlib.sha256(baseline).hexdigest() != raw_sha:
                    raise MaterialPanelError("CBuffer snapshot bytes do not match their hash")
                fields = [self._normalise_field(field) for field in snapshot.get("fields", [])]
                fields_by_id = dict((field["leafId"], field) for field in fields)
                if len(fields_by_id) != len(fields):
                    raise MaterialPanelError("CBuffer snapshot contains duplicate leaf IDs")

                session_id = _hex(snapshot.get("snapshotId"), "snapshot ID", 32)
                session = {
                    "sessionId": session_id,
                    "snapshotId": session_id,
                    "captureId": current["captureId"],
                    "captureSessionId": current["captureSessionId"],
                    "api": current["api"],
                    "eventId": selection["eventId"],
                    "stage": selection["stage"],
                    "blockIndex": selection["blockIndex"],
                    "constantBlockPath": _text(
                        snapshot.get("constantBlockPath"), "constant-block path"),
                    "constantBlockDisplayName": _text(
                        snapshot.get("constantBlockDisplayName",
                                     snapshot.get("constantBlockPath")),
                        "constant-block display name"),
                    "slot": selection["physicalSlot"],
                    "registerSpace": selection["registerSpace"],
                    "shaderResourceId": selection["shaderResourceId"],
                    "bufferResourceId": selection["bufferResourceId"],
                    "byteOffset": selection["bufferByteOffset"],
                    "byteSize": selection["bufferByteSize"],
                    "validByteSize": valid_size,
                    "baseline": baseline,
                    "baselineRawSha256": raw_sha,
                    "currentRawSha256": raw_sha,
                    "layoutSha256": _sha256(snapshot.get("layoutSha256"),
                                            "snapshot layout hash"),
                    "revision": 0,
                    "overrideId": "",
                    "resetMode": snapshot.get("resetMode", "captured-range"),
                    "fieldsById": fields_by_id,
                    "exposed": collections.OrderedDict(),
                    "patches": collections.OrderedDict(),
                    "physicalKey": physical_key,
                }
            except Exception:
                self._release_unadopted_snapshot(snapshot)
                raise
            self._sessions[session_id] = session
            self._physical_keys[physical_key] = session_id

        candidates = []
        for field in session["fieldsById"].values():
            if (field["byteOffset"] == selection["fieldByteOffset"] and
                    field["byteSize"] == selection["fieldComponents"] * 4 and
                    field["baseType"] == selection["fieldBaseType"] and
                    field["components"] == selection["fieldComponents"]):
                candidates.append(field)
        if len(candidates) != 1:
            if not session["exposed"] and not session["patches"]:
                self.rollback(session["sessionId"])
            raise MaterialPanelError(
                "The selected UI variable is not one unique editable leaf in live reflection")

        leaf = candidates[0]
        session["exposed"][leaf["leafId"]] = True
        return self.status(session["sessionId"])

    @staticmethod
    def _rebuilt_bytes(session, patches):
        data = bytearray(session["baseline"])
        for leaf_id, packed in patches.items():
            leaf = session["fieldsById"][leaf_id]
            start = leaf["byteOffset"]
            data[start:start + leaf["byteSize"]] = packed
        return bytes(data)

    def prepare_update(self, session_id, parameter_id, value):
        session_id = _hex(session_id, "material session ID", 32)
        parameter_id = _hex(parameter_id, "material parameter ID", 32)
        session = self._sessions.get(session_id)
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        current = self._current_context()
        if (current["captureSessionId"] != session["captureSessionId"] or
                current["captureId"] != session["captureId"]):
            raise MaterialPanelError("The material session belongs to an expired capture")
        if parameter_id not in session["exposed"]:
            raise MaterialPanelError("The parameter was not exposed by a native UI selection")
        leaf = session["fieldsById"].get(parameter_id)
        if leaf is None:
            raise MaterialPanelError("The reflected parameter no longer exists")

        packed = _pack_values(leaf, value)
        patches = collections.OrderedDict(session["patches"])
        patches[parameter_id] = packed
        rebuilt = self._rebuilt_bytes(session, patches)
        data_sha = hashlib.sha256(rebuilt).hexdigest()
        patch_values = []
        for leaf_id, leaf_packed in sorted(
                patches.items(), key=lambda item: session["fieldsById"][item[0]]["byteOffset"]):
            patch_values.append({
                "leafId": leaf_id,
                "packedBase64": base64.b64encode(leaf_packed).decode("ascii"),
            })

        params = {
            "snapshot_id": session["snapshotId"],
            "expected_capture_id": session["captureId"],
            "expected_capture_session_id": session["captureSessionId"],
            "expected_raw_sha256": session["currentRawSha256"],
            "expected_layout_sha256": session["layoutSha256"],
            "expected_revision": session["revision"],
            "new_revision": session["revision"] + 1,
            "expected_override_id": session["overrideId"],
            "data_base64": base64.b64encode(rebuilt).decode("ascii"),
            "data_sha256": data_sha,
            "patches": patch_values,
        }
        return {"session": session, "params": params, "patches": patches, "dataSha": data_sha}

    def accept_update(self, prepared, response):
        session = prepared["session"]
        session_id = session["sessionId"]
        if self._sessions.get(session_id) is not session:
            raise MaterialPanelError("The material session belongs to an expired capture")
        data_sha = prepared["dataSha"]
        if session["revision"] != prepared["params"]["expected_revision"]:
            raise MaterialPanelError("The material editor revision changed during replay")
        if not isinstance(response, dict) or response.get("schema") != OVERRIDE_SCHEMA:
            raise MaterialPanelError("Bread returned an invalid CBuffer override response")
        if (_hex(response.get("snapshotId"), "override snapshot ID", 32) !=
                session["snapshotId"] or
                _integer(response.get("revision"), "override revision") != session["revision"] + 1 or
                _sha256(response.get("currentRawSha256"), "override raw hash") != data_sha or
                not response.get("configured") or not response.get("appliedToCurrentEvent")):
            raise MaterialPanelError("CBuffer override response failed its identity check")

        session["patches"] = prepared["patches"]
        session["currentRawSha256"] = data_sha
        session["revision"] += 1
        session["overrideId"] = _hex(response.get("overrideId"), "override ID", 32)
        session["generation"] = response.get("generation", 0)
        session["previewPublished"] = bool(response.get("previewPublished", False))
        session["replayedEventId"] = response.get("replayedEventId")
        return self.status(session_id)

    def update(self, session_id, parameter_id, value):
        if self.edit_queue.busy:
            raise MaterialPanelError("A material edit is already queued")
        prepared = self.prepare_update(session_id, parameter_id, value)
        try:
            return self.accept_update(prepared, self._apply(prepared["params"]))
        except Exception as exc:
            self._discard_expired_session(session_id, exc)
            raise

    def _discard_expired_session(self, session_id, error):
        if not getattr(error, "snapshot_expired", False):
            return
        # Preserve accepted native values. Only forget this stale UI capability;
        # an explicit native selection can then acquire a fresh snapshot.
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self._physical_keys.pop(session["physicalKey"], None)
        self.edit_queue.remove_pending(lambda job: job["sessionId"] == session_id)

    def submit_text(self, session_id, parameter_id, text, complete, owner):
        self.edit_queue.submit(("edit", session_id, parameter_id),
            {"kind": "edit", "sessionId": session_id, "parameterId": parameter_id,
             "text": str(text)}, complete, owner)

    def submit_rollback(self, session_id, complete, owner):
        self.edit_queue.remove_pending(lambda job: job["sessionId"] == session_id)
        self.edit_queue.submit(("clear", session_id),
            {"kind": "clear", "sessionId": session_id}, complete, owner)

    def _dispatch_edit(self, job, cancel, complete):
        if self._apply_async is None:
            raise MaterialPanelError("Asynchronous material replay is unavailable")
        session = self._sessions.get(job["sessionId"])
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        clear = job["kind"] == "clear"
        if clear:
            params = self._rollback_params(session)
            prepared = None
        else:
            leaf = session["fieldsById"].get(job["parameterId"])
            if leaf is None:
                raise MaterialPanelError("Unknown material parameter")
            value = parse_parameter_text(job["text"], leaf["baseType"], leaf["components"])
            prepared = self.prepare_update(job["sessionId"], job["parameterId"], value)
            params = prepared["params"]

        def finished(response, error):
            status = None
            if error is not None:
                self._discard_expired_session(job["sessionId"], error)
            if error is None:
                try:
                    # A closed dock must still retain genuinely committed versions
                    # in the surviving controller. A new capture expires the session.
                    if self._sessions.get(job["sessionId"]) is session:
                        status = self.accept_rollback(session, response) if clear else self.accept_update(prepared, response)
                except Exception as exc:
                    error = exc
            complete(status, error)

        self._apply_async(params, clear, cancel, finished)

    def update_text(self, session_id, parameter_id, text):
        session = self._sessions.get(session_id)
        if session is None or parameter_id not in session.get("exposed", {}):
            raise MaterialPanelError("Unknown or unexposed material parameter")
        leaf = session["fieldsById"][parameter_id]
        return self.update(
            session_id, parameter_id,
            parse_parameter_text(text, leaf["baseType"], leaf["components"]))

    def _parameter_public(self, session, leaf_id):
        leaf = session["fieldsById"][leaf_id]
        current = self._rebuilt_bytes(session, session["patches"])
        absolute_offset = session["byteOffset"] + leaf["byteOffset"]
        physical = ("EID %d · %s binding%d space%d · block[%d] · window +0x%X · "
                    "field +0x%X (buffer +0x%X) · shader %d · buffer %d" % (
                        session["eventId"], _stage_abbreviation(session["stage"], session["api"]),
                        session["slot"], session["registerSpace"], session["blockIndex"],
                        session["byteOffset"],
                        leaf["byteOffset"], absolute_offset, session["shaderResourceId"],
                        session["bufferResourceId"]))
        return {
            "parameterId": leaf_id,
            "capturedName": leaf["path"],
            "displayName": leaf["displayName"],
            "baseType": leaf["baseType"],
            "components": leaf["components"],
            "byteOffset": leaf["byteOffset"],
            "byteSize": leaf["byteSize"],
            "bufferResourceId": session["bufferResourceId"],
            "bindingByteOffset": session["byteOffset"],
            "absoluteByteOffset": absolute_offset,
            "value": _leaf_values(current, leaf),
            "physicalIdentity": physical,
        }

    def status(self, session_id):
        session_id = _hex(session_id, "material session ID", 32)
        session = self._sessions.get(session_id)
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        return {
            "schema": "replaylab-native-material-panel-session/v1",
            "sessionId": session_id,
            "captureId": session["captureId"],
            "captureSessionId": session["captureSessionId"],
            "eventId": session["eventId"],
            "stage": session["stage"],
            "blockIndex": session["blockIndex"],
            "constantBlockPath": session["constantBlockPath"],
            "constantBlockDisplayName": session["constantBlockDisplayName"],
            "slot": session["slot"],
            "registerSpace": session["registerSpace"],
            "shaderResourceId": session["shaderResourceId"],
            "bufferResourceId": session["bufferResourceId"],
            "bufferByteOffset": session["byteOffset"],
            "bufferByteSize": session["byteSize"],
            "revision": session["revision"],
            "overrideId": session["overrideId"],
            "configured": bool(session["overrideId"]),
            "scope": "shared-buffer-range",
            "generation": session.get("generation", 0),
            "resetMode": session.get("resetMode", "captured-range"),
            "previewPublished": session.get("previewPublished", False),
            "replayedEventId": session.get("replayedEventId"),
            "api": session["api"],
            "parameters": [self._parameter_public(session, leaf_id)
                           for leaf_id in session["exposed"]],
        }

    def list_status(self):
        return [self.status(session_id) for session_id in self._sessions]

    def rollback(self, session_id):
        session_id = _hex(session_id, "material session ID", 32)
        session = self._sessions.get(session_id)
        if session is None:
            raise MaterialPanelError("Unknown or expired material-panel session")
        if self.edit_queue.busy:
            raise MaterialPanelError("A material edit is already queued")
        try:
            return self.accept_rollback(session, self._rollback(self._rollback_params(session)))
        except Exception as exc:
            self._discard_expired_session(session_id, exc)
            raise

    @staticmethod
    def _rollback_params(session):
        return {
            "snapshot_id": session["snapshotId"],
            "override_id": session["overrideId"],
            "expected_capture_id": session["captureId"],
            "expected_capture_session_id": session["captureSessionId"],
        }

    def accept_rollback(self, session, response):
        session_id = session["sessionId"]
        if self._sessions.get(session_id) is not session:
            raise MaterialPanelError("The material session belongs to an expired capture")
        if not isinstance(response, dict) or response.get("schema") != OVERRIDE_SCHEMA:
            raise MaterialPanelError("Bread returned an invalid rollback response")
        if not response.get("snapshotReleased"):
            raise MaterialPanelError("Bread did not release the CBuffer snapshot")
        self._sessions.pop(session_id, None)
        self._physical_keys.pop(session["physicalKey"], None)
        return response

    def rollback_all(self):
        results = []
        errors = []
        for session_id in list(self._sessions.keys()):
            try:
                results.append(self.rollback(session_id))
            except Exception as exc:
                errors.append("%s: %s" % (session_id, str(exc)))
        if errors:
            raise MaterialPanelError("; ".join(errors))
        return results

    def forget_all(self):
        """Forget UI state after the bridge has invalidated the capture lifecycle."""
        self.edit_queue.reset()
        self._sessions.clear()
        self._physical_keys.clear()
