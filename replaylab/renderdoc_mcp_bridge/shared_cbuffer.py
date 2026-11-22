"""Capture-local ownership of reflected, persistent CBuffer field edits.

Python 3.6 compatible. Only bridge-issued metadata determines native byte ranges;
the full-window digest is an editor revision witness, never the native write size.
"""

import base64
import copy
import hashlib
import math
import struct
import uuid


SNAPSHOT_SCHEMA = "renderdoc-ultra-shared-cbuffer-snapshot/v1"
EDIT_SCHEMA = "renderdoc-ultra-shared-cbuffer-edit/v1"
SCOPE = "shared-buffer-range"
MAX_SHARED_CBUFFER_BYTES = 4 * 1024 * 1024
MAX_SHARED_CBUFFER_RECORD_BYTES = 16 * 1024 * 1024


class SharedCBufferSnapshotExpired(RuntimeError):
    """The caller may reopen a fresh snapshot, but must not retry its stale bytes."""

    snapshot_expired = True


def _integer(value, name, minimum=0, maximum=(1 << 64) - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid %s" % name)
    return value


def _bytes(value, size):
    if not isinstance(value, str):
        raise ValueError("Packed field bytes must be base64 text")
    # Reject an oversized encoding before ASCII conversion or base64 allocation.
    if len(value) != ((size + 2) // 3) * 4:
        raise ValueError("Packed field bytes do not match the reflected size")
    data = base64.b64decode(value.encode("ascii"), validate=True)
    if len(data) != size or base64.b64encode(data).decode("ascii") != value:
        raise ValueError("Packed field bytes do not match the reflected size")
    return data


class SharedCBufferEdits(object):
    """Serialize one owner against the controller's shared configuration generation.

    An external writer invalidates old edit capabilities, not the entire editor.
    A fresh snapshot adopts the current values. Its reset restores that baseline
    rather than clearing an exact range that may predate this editor session.
    """

    def __init__(self, resource_id_int):
        self._resource_id_int = resource_id_int
        self.forget()

    def forget(self):
        self.records = {}
        self._generation = 0
        self._record_bytes = 0
        self._foreign_baseline = False

    def check_snapshot_capacity(self, byte_size):
        """Check before native readback and again before retaining an immutable window."""
        if len(self.records) >= 64:
            raise RuntimeError("The shared CBuffer snapshot limit was reached")
        size = _integer(byte_size, "valid CBuffer byte size", 1, MAX_SHARED_CBUFFER_BYTES)
        # Reserve baseline + a later edited current window + the retained canonical base64.
        # Python object/field metadata overhead is separate; don't permit 64 full-size windows.
        reserved_bytes = size * 2 + ((size + 2) // 3) * 4
        if reserved_bytes > MAX_SHARED_CBUFFER_RECORD_BYTES - self._record_bytes:
            raise RuntimeError("The shared CBuffer snapshot byte budget was reached")
        return reserved_bytes

    def register(self, snapshot, ctrl):
        self._guard_generation(ctrl, fresh_snapshot=True)
        if snapshot["snapshotId"] in self.records:
            raise ValueError("The shared CBuffer snapshot identity is already registered")
        size = _integer(snapshot["validByteSize"], "valid CBuffer byte size", 1,
                        MAX_SHARED_CBUFFER_BYTES)
        _integer(snapshot["byteSize"], "bound CBuffer byte size", size, MAX_SHARED_CBUFFER_BYTES)
        reserved_bytes = self.check_snapshot_capacity(size)
        encoded_size = ((size + 2) // 3) * 4
        if not isinstance(snapshot["rawBase64"], str) or len(snapshot["rawBase64"]) != encoded_size:
            raise ValueError("Packed field bytes do not match the reflected size")
        snapshot = copy.deepcopy(snapshot)
        snapshot["schema"] = SNAPSHOT_SCHEMA
        snapshot["scope"] = SCOPE
        snapshot["resetMode"] = ("snapshot-baseline" if self._foreign_baseline
                                 else "captured-range")
        raw = _bytes(snapshot["rawBase64"], size)
        if hashlib.sha256(raw).hexdigest() != snapshot["rawSha256"]:
            raise ValueError("The CBuffer snapshot bytes do not match their digest")
        base = _integer(snapshot["byteOffset"], "binding byte offset")
        resource = _integer(snapshot["bufferResourceId"], "original buffer resource", 1)
        length = _integer(snapshot["bufferLength"], "original buffer length", 1)
        if base + len(raw) > length:
            raise ValueError("The reflected window exceeds the original buffer")
        fields = {}
        previous_end = 0
        for leaf in sorted(snapshot["fields"], key=lambda item: item["byteOffset"]):
            offset = _integer(leaf["byteOffset"], "reflected member offset")
            count = _integer(leaf["components"], "reflected components", 1, 4)
            size = _integer(leaf["byteSize"], "reflected field length", 4, 16)
            if (not leaf.get("editable") or leaf["baseType"] not in ("Float", "SInt", "UInt") or
                    size != count * 4 or offset < previous_end or offset + size > len(raw) or
                    leaf["leafId"] in fields):
                raise ValueError("The shared CBuffer field layout is unsupported")
            previous_end = offset + size
            fields[leaf["leafId"]] = leaf
        record = {"snapshot": snapshot, "baseline": raw, "current": raw,
                  "fields": fields, "patches": {}, "revision": 0, "overrideId": "",
                  "resource": resource, "base": base, "length": length,
                  "reservedBytes": reserved_bytes,
                  "restoreBaseline": self._foreign_baseline}
        self.records[snapshot["snapshotId"]] = record
        self._record_bytes += reserved_bytes
        return copy.deepcopy(snapshot)

    def _guard_generation(self, ctrl, fresh_snapshot=False):
        generation = int(ctrl.GetSharedBufferPatchGeneration())
        if generation != self._generation:
            # Do not clear native patches: an external writer may have replaced
            # one of our exact ranges. Revoke old snapshots and let the next
            # explicit selection reopen against current bytes without reopening
            # the capture. No stale value is automatically retried.
            self.records.clear()
            self._record_bytes = 0
            self._generation = generation
            self._foreign_baseline = True
            if not fresh_snapshot:
                raise SharedCBufferSnapshotExpired(
                    "The shared buffer configuration changed outside this material editor; "
                    "select the parameter again to edit its current value")

    def _record(self, params):
        record = self.records.get(params.get("snapshot_id"))
        if record is None:
            raise SharedCBufferSnapshotExpired("Unknown or expired shared CBuffer snapshot")
        snapshot = record["snapshot"]
        if (params.get("expected_capture_id") != snapshot["captureId"] or
                params.get("expected_capture_session_id") != snapshot["captureSessionId"]):
            raise RuntimeError("The shared CBuffer snapshot belongs to another capture lifecycle")
        return record

    def _resource(self, record, ctrl):
        for description in ctrl.GetBuffers():
            resource = getattr(description, "resourceId", None)
            if self._resource_id_int(resource) == record["resource"]:
                if int(description.length) != record["length"]:
                    raise RuntimeError("The original CBuffer allocation changed")
                return resource
        raise RuntimeError("The original CBuffer resource is unavailable")

    @staticmethod
    def _range(record, leaf_id):
        leaf = record["fields"][leaf_id]
        return record["base"] + leaf["byteOffset"], leaf["byteSize"]

    def apply(self, params, ctrl):
        record = self._record(params)
        if record.get("clearing"):
            raise RuntimeError("Finish the incomplete shared CBuffer rollback before editing")
        self._guard_generation(ctrl)
        snapshot = record["snapshot"]
        if (params.get("expected_revision") != record["revision"] or
                params.get("new_revision") != record["revision"] + 1 or
                params.get("expected_override_id", "") != record["overrideId"] or
                params.get("expected_layout_sha256") != snapshot["layoutSha256"] or
                params.get("expected_raw_sha256") != hashlib.sha256(record["current"]).hexdigest()):
            raise RuntimeError("The shared CBuffer editor revision is stale")
        patches = {}
        rebuilt = bytearray(record["baseline"])
        for patch in params.get("patches", []):
            if set(patch) != {"leafId", "packedBase64"}:
                raise ValueError("Shared CBuffer edits accept only issued leaf IDs and bytes")
            leaf_id = patch["leafId"]
            leaf = record["fields"].get(leaf_id)
            if leaf is None or leaf_id in patches:
                raise ValueError("Unknown or duplicate shared CBuffer field")
            packed = _bytes(patch["packedBase64"], leaf["byteSize"])
            if leaf["baseType"] == "Float" and any(
                    not math.isfinite(value) for value in struct.unpack("<%df" % leaf["components"], packed)):
                raise ValueError("Float CBuffer edits must be finite")
            patches[leaf_id] = packed
            start = leaf["byteOffset"]
            rebuilt[start:start + leaf["byteSize"]] = packed
        data = bytes(rebuilt)
        if not patches:
            raise ValueError("A shared CBuffer update requires an issued field edit")
        if (_bytes(params.get("data_base64"), len(data)) != data or
                params.get("data_sha256") != hashlib.sha256(data).hexdigest()):
            raise ValueError("The CBuffer revision is not the baseline plus its issued field edits")
        if not set(record["patches"]).issubset(patches):
            raise ValueError("Use explicit rollback to clear shared fields")
        changed = [leaf_id for leaf_id in patches
                   if leaf_id not in record["patches"] or patches[leaf_id] != record["patches"][leaf_id]]
        if len(changed) > 1:
            raise ValueError("One material update must change at most one reflected field")
        if changed:
            leaf_id = changed[0]
            offset, size = self._range(record, leaf_id)
            for other in self.records.values():
                if other is record or other["resource"] != record["resource"]:
                    continue
                for other_leaf in other["patches"]:
                    other_offset, other_size = self._range(other, other_leaf)
                    if offset < other_offset + other_size and other_offset < offset + size:
                        raise RuntimeError("Another material session already owns this shared field range")
            resource = self._resource(record, ctrl)
            error = ctrl.SetSharedBufferPatch(resource, offset, patches[leaf_id])
            if str(error or ""):
                raise RuntimeError("SetSharedBufferPatch failed: %s" % str(error))
            self._generation = int(ctrl.GetSharedBufferPatchGeneration())
        record["patches"] = patches
        record["current"] = data
        record["revision"] += 1
        record["overrideId"] = record["overrideId"] or uuid.uuid4().hex
        return self._response(record)

    def _response(self, record):
        return {"schema": EDIT_SCHEMA, "scope": SCOPE,
                "snapshotId": record["snapshot"]["snapshotId"],
                "overrideId": record["overrideId"], "revision": record["revision"],
                "generation": self._generation,
                "resetMode": record["snapshot"]["resetMode"],
                "currentRawSha256": hashlib.sha256(record["current"]).hexdigest(),
                "configured": bool(record["patches"]), "appliedToCurrentEvent": True}

    def rollback(self, params, ctrl):
        record = self._record(params)
        if params.get("override_id", "") != record["overrideId"]:
            raise RuntimeError("The shared CBuffer edit identity changed")
        if record["patches"]:
            self._guard_generation(ctrl)
            resource = self._resource(record, ctrl)
            record["clearing"] = True
            # The public API clears one exact range at a time. Preserve uncleared
            # ownership on a partial failure so retry clears only remaining fields.
            for leaf_id in list(record["patches"]):
                offset, size = self._range(record, leaf_id)
                if record["restoreBaseline"]:
                    start = record["fields"][leaf_id]["byteOffset"]
                    error = ctrl.SetSharedBufferPatch(
                        resource, offset, record["baseline"][start:start + size])
                else:
                    error = ctrl.ClearSharedBufferPatch(resource, offset, size)
                if str(error or ""):
                    raise RuntimeError("Shared CBuffer reset failed: %s" % str(error))
                self._generation = int(ctrl.GetSharedBufferPatchGeneration())
                del record["patches"][leaf_id]
        response = self._response(record)
        response.update({"snapshotReleased": True, "cleared": bool(record["overrideId"])})
        del self.records[record["snapshot"]["snapshotId"]]
        self._record_bytes -= record["reservedBytes"]
        return response
