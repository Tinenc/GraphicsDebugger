"""Capture-bound resource export helpers for the built-in ReplayLab bridge.

This module intentionally depends only on Python's standard library.  The
embedded qrenderdoc runtime is Python 3.6 and conversion libraries belong in
the external Host, not in the replay process.  RenderDoc objects and enums are
therefore passed in by :mod:`renderdoc_mcp_bridge`.

The bridge writes an evidence bundle, not a guessed DCC asset.  Vertex input
bytes are always retained.  A deterministic float32 canonical stream is added
only for regular formats that can be decoded without ambiguity.  Unsupported
packed/special formats stay explicit in the manifest instead of being replaced
with zeroes.
"""

from __future__ import print_function

import hashlib
import json
import math
import os
import re
import stat
import struct
import uuid


REQUEST_SCHEMA = "replaylab-resource-export-request/v1"
TICKET_SCHEMA = "replaylab-resource-export-ticket/v1"
BUNDLE_SCHEMA = "replaylab-resource-export-bundle/v1"
CAPTURE_SCHEMA = "replaylab-resource-export-capture/v1"
INSPECT_SCHEMA = "replaylab-resource-export-inspect/v1"

MAX_TICKET_BYTES = 64 * 1024
MAX_ACTIONS_SCANNED = 1000000
MAX_VERTEX_REFERENCES = 4000000
MAX_VERTEX_ATTRIBUTES = 128
MAX_VERTEX_SPAN_BYTES = 512 * 1024 * 1024
MAX_TEXTURES = 512
MAX_TEXTURE_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_BOUND_BUFFER_BYTES = 64 * 1024 * 1024
MAX_BOUND_BUFFER_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RESOURCE_TEXT = 512

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_INDEX = re.compile(r"^(.*?)([0-9]+)$")


class UnsupportedVertexFormat(ValueError):
    """Raised when canonical decoding would require guessing."""


def _safe_int(value, default=0):
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except Exception:
        return default


def _resource_id(value):
    if value is None:
        return 0
    try:
        return int(value)
    except Exception:
        pass
    for name in ("resource", "resourceId", "id"):
        try:
            return int(getattr(value, name))
        except Exception:
            pass
    return 0


def _enum_text(value):
    if value is None:
        return None
    name = getattr(value, "name", None)
    text = str(name if name is not None else value)
    return text.replace("\x00", "")[:MAX_RESOURCE_TEXT]


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _method_bool(value, name, default=False):
    method = getattr(value, name, None)
    if not callable(method):
        return bool(default)
    try:
        return bool(method())
    except Exception:
        return bool(default)


def _method_int(value, name, default=0):
    method = getattr(value, name, None)
    if not callable(method):
        return int(default)
    try:
        return _safe_int(method(), default)
    except Exception:
        return int(default)


def canonical_json_bytes(value):
    """Return the stable UTF-8 representation used for all bundle hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_safe(value, depth=0):
    if depth > 64:
        return "<maximum-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    return str(value).replace("\x00", "")[:4096]


def _request_document(capture_id, capture_session_id, event_id, instance, view):
    return {
        "schema": REQUEST_SCHEMA,
        "captureId": str(capture_id or ""),
        "captureSessionId": str(capture_session_id or ""),
        "eventId": int(event_id),
        "instance": int(instance),
        "view": int(view),
    }


def canonical_request_sha256(capture_id, capture_session_id, event_id, instance, view):
    return hashlib.sha256(canonical_json_bytes(_request_document(
        capture_id, capture_session_id, event_id, instance, view))).hexdigest()


def _require_hex(value, expression, name):
    if not isinstance(value, str) or expression.match(value) is None:
        raise ValueError("%s has an invalid hexadecimal identity" % name)
    return value


def _normal_path(path):
    return os.path.abspath(os.path.normpath(path))


def _same_path(left, right):
    return os.path.normcase(_normal_path(left)) == os.path.normcase(_normal_path(right))


def _is_redirected(path):
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _require_plain_directory(path, name):
    if not os.path.isdir(path) or _is_redirected(path):
        raise ValueError("%s is unavailable or redirected" % name)
    if not _same_path(os.path.realpath(path), path):
        raise ValueError("%s resolves through a redirected path" % name)


def _contained(root, candidate):
    try:
        return os.path.commonpath([_normal_path(root), _normal_path(candidate)]) == _normal_path(root)
    except (AttributeError, ValueError):
        root_key = os.path.normcase(_normal_path(root)).rstrip("\\/") + os.sep
        candidate_key = os.path.normcase(_normal_path(candidate))
        return candidate_key.startswith(root_key)


def _relative_path(value, name):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("%s must be a non-empty relative path" % name)
    if os.path.isabs(value) or os.path.splitdrive(value)[0]:
        raise ValueError("%s must be relative" % name)
    pieces = [piece for piece in re.split(r"[\\/]", value) if piece not in ("", ".")]
    if not pieces or any(piece == ".." for piece in pieces):
        raise ValueError("%s escapes its managed root" % name)
    if any(":" in piece for piece in pieces):
        raise ValueError("%s contains an invalid path component" % name)
    result = os.path.join(*pieces)
    if result in ("", ".", os.curdir):
        raise ValueError("%s must name a child directory" % name)
    return result


def _hash_regular_file(path, maximum_bytes=None, allow_empty=True):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or _is_redirected(path):
        raise RuntimeError("resource export artifact is not a regular file")
    if before.st_size < 0 or (before.st_size == 0 and not allow_empty):
        raise RuntimeError("resource export artifact is empty")
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise RuntimeError("resource export artifact exceeds its byte budget")
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as stream:
        while True:
            data = stream.read(1024 * 1024)
            if not data:
                break
            total += len(data)
            if maximum_bytes is not None and total > maximum_bytes:
                raise RuntimeError("resource export artifact exceeds its byte budget")
            digest.update(data)
    after = os.lstat(path)
    if (after.st_size != before.st_size or total != before.st_size or
            getattr(after, "st_mtime_ns", None) != getattr(before, "st_mtime_ns", None)):
        raise RuntimeError("resource export artifact changed while it was hashed")
    return total, digest.hexdigest()


def _reject_duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("resource export ticket contains a duplicate JSON key")
        result[key] = value
    return result


def _ticket_value(ticket, snake_name, camel_name, default=None):
    if snake_name in ticket:
        return ticket.get(snake_name)
    return ticket.get(camel_name, default)


def _load_ticket_file(ticket_path, ticket_sha256):
    _require_hex(ticket_sha256, _HEX_64, "ticket_sha256")
    size, digest = _hash_regular_file(ticket_path, MAX_TICKET_BYTES, allow_empty=False)
    if digest != ticket_sha256:
        raise ValueError("ticket_sha256 does not match ticket.json")
    with open(ticket_path, "rb") as stream:
        raw = stream.read(size + 1)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("ticket.json is not strict UTF-8 JSON: %s" % str(exc))
    if not isinstance(document, dict) or document.get("schema") != TICKET_SCHEMA:
        raise ValueError("ticket.json has an unsupported schema")
    return document


def _validate_selection(params, capture_id, capture_session_id):
    event_id = _safe_int(params.get("event_id"), -1)
    instance = _safe_int(params.get("instance", 0), -1)
    view = _safe_int(params.get("view", 0), -1)
    if event_id < 1 or event_id > 0xFFFFFFFF:
        raise ValueError("event_id must be a positive 32-bit EID")
    if instance < 0 or instance > 0x7FFFFFFF:
        raise ValueError("instance must be a non-negative 31-bit integer")
    if view < 0 or view > 0x7FFFFFFF:
        raise ValueError("view must be a non-negative 31-bit integer")
    if view != 0:
        raise ValueError(
            "view must be 0 until RenderDoc exposes an exact multiview vertex-input selector")
    _require_hex(capture_session_id, _HEX_32, "expected_capture_session_id")
    if capture_id:
        _require_hex(capture_id, _HEX_64, "expected_capture_id")
    selection_sha256 = canonical_request_sha256(
        capture_id, capture_session_id, event_id, instance, view)
    supplied = params.get("request_sha256")
    if supplied is not None:
        _require_hex(supplied, _HEX_64, "request_sha256")
    formats = params.get("texture_formats")
    if formats is None:
        formats = ("auto",)
    if not isinstance(formats, (list, tuple)) or len(formats) > 6:
        raise ValueError("texture_formats must be a short list of encodings")
    cleaned = []
    for item in formats:
        text = str(item or "").strip().lower()
        if text and text not in cleaned:
            if text not in ("auto", "dds", "tga", "exr", "png", "jpg", "hdr", "bmp"):
                raise ValueError("unsupported texture format %r" % text)
            cleaned.append(text)
    return {
        "captureId": capture_id,
        "captureSessionId": capture_session_id,
        "textureFormats": tuple(cleaned) or ("auto",),
        "eventId": event_id,
        "instance": instance,
        "view": view,
        # requestSha256 is the Host's complete operation binding.  The bridge
        # cannot reconstruct fields such as assetName/api that intentionally
        # stay out of the replay request, so a hashed ticket is authoritative.
        "requestSha256": supplied or selection_sha256,
        "selectionSha256": selection_sha256,
    }


def validate_inspect_request(params, capture_id, capture_session_id):
    return _validate_selection(params, capture_id, capture_session_id)


def validate_capture_request(params, capture_id, capture_session_id):
    """Validate a Host-issued export ticket and return its exact artifact root.

    The low-level bridge accepts no free output path.  The Host must prepare an
    empty directory below an existing managed root and bind it to a ticket.  A
    hashed ``ticket.json`` is optional for the initial Host integration; when
    supplied every field is checked against the in-memory ticket and frozen
    capture selection.
    """

    request = _validate_selection(params, capture_id, capture_session_id)
    ticket = params.get("export_ticket")
    if not isinstance(ticket, dict):
        raise ValueError("export_ticket must be a Host-issued object")
    ticket_id = _require_hex(
        _ticket_value(ticket, "ticket_id", "ticketId"), _HEX_32, "ticket_id")
    managed_root = _ticket_value(ticket, "managed_root", "managedRoot")
    relative_dir = _ticket_value(ticket, "relative_dir", "relativeDir")
    if (not isinstance(managed_root, str) or not managed_root or
            "\x00" in managed_root or not os.path.isabs(managed_root)):
        raise ValueError("managed_root must be an absolute Host-owned directory")
    managed_root = _normal_path(managed_root)
    if os.path.dirname(managed_root) == managed_root:
        raise ValueError("managed_root cannot be a filesystem root")
    _require_plain_directory(managed_root, "managed_root")
    relative_dir = _relative_path(relative_dir, "relative_dir")
    artifact_dir = _normal_path(os.path.join(managed_root, relative_dir))
    if not _contained(managed_root, artifact_dir) or _same_path(managed_root, artifact_dir):
        raise ValueError("relative_dir escaped managed_root")
    _require_plain_directory(artifact_dir, "resource export artifact directory")

    relative_walk = os.path.relpath(artifact_dir, managed_root)
    current = managed_root
    for piece in relative_walk.split(os.sep):
        current = os.path.join(current, piece)
        _require_plain_directory(current, "resource export path component")

    for snake_name, camel_name, expected in (
            ("event_id", "eventId", request["eventId"]),
            ("instance", "instance", request["instance"]),
            ("view", "view", request["view"])):
        value = _ticket_value(ticket, snake_name, camel_name)
        if value is not None and _safe_int(value, -1) != expected:
            raise ValueError("export_ticket %s does not match the frozen request" % snake_name)
    supplied_request = _ticket_value(ticket, "request_sha256", "requestSha256")
    _require_hex(supplied_request, _HEX_64, "export_ticket request_sha256")
    if supplied_request != request["requestSha256"]:
        raise ValueError("export_ticket request_sha256 does not match the Host request")

    ticket_path = _ticket_value(ticket, "ticket_path", "ticketPath", params.get("ticket_path"))
    ticket_sha256 = _ticket_value(
        ticket, "ticket_sha256", "ticketSha256", params.get("ticket_sha256"))
    allowed_entries = set()
    if ticket_path is None or ticket_sha256 is None:
        raise ValueError("capture export requires a hashed Host ticket.json")
    if ticket_path is not None or ticket_sha256 is not None:
        if (not isinstance(ticket_path, str) or not ticket_path or
                "\x00" in ticket_path or not os.path.isabs(ticket_path)):
            raise ValueError("ticket_path must be an absolute ticket.json path")
        ticket_path = _normal_path(ticket_path)
        operation_dir = _normal_path(os.path.join(
            managed_root, "operations", ticket_id))
        expected_artifact_dir = _normal_path(os.path.join(operation_dir, "capture"))
        expected_ticket_path = _normal_path(os.path.join(operation_dir, "ticket.json"))
        if (not _same_path(artifact_dir, expected_artifact_dir) or
                not _same_path(ticket_path, expected_ticket_path)):
            raise ValueError(
                "ticket and artifact paths must use operations/<ticket_id>/{ticket.json,capture}")
        _require_plain_directory(operation_dir, "resource export operation directory")
        if not _contained(managed_root, ticket_path) or _is_redirected(ticket_path):
            raise ValueError("ticket_path escaped or redirected outside managed_root")
        ticket_document = _load_ticket_file(ticket_path, ticket_sha256)
        checks = (
            ("ticketId", ticket_id),
            ("managedRoot", managed_root),
            ("relativeDir", relative_dir.replace(os.sep, "/")),
            ("requestSha256", request["requestSha256"]),
            ("captureId", request["captureId"]),
            ("captureSessionId", request["captureSessionId"]),
            ("eventId", request["eventId"]),
            ("instance", request["instance"]),
            ("view", request["view"]),
        )
        for name, expected in checks:
            actual = ticket_document.get(name)
            if name == "managedRoot":
                matches = isinstance(actual, str) and _same_path(actual, expected)
            elif name == "relativeDir":
                matches = str(actual or "").replace("\\", "/") == expected
            else:
                matches = actual == expected
            if not matches:
                raise ValueError("ticket.json %s does not match the frozen request" % name)
        request["ticketPath"] = ticket_path
        request["ticketSha256"] = ticket_sha256

    entries = set(os.listdir(artifact_dir))
    if entries != allowed_entries:
        raise ValueError("resource export artifact directory must be new and empty")
    request.update({
        "ticketId": ticket_id,
        "managedRoot": managed_root,
        "relativeDir": relative_dir.replace(os.sep, "/"),
        "artifactDir": artifact_dir,
    })
    return request


def normalise_resource_format(value):
    if value is None:
        return {
            "name": "",
            "type": None,
            "compType": None,
            "compCount": 0,
            "compByteWidth": 0,
            "flags": 0,
            "special": True,
            "bgraOrder": False,
            "srgbCorrected": False,
            "elementByteSize": None,
        }
    name_method = getattr(value, "Name", None)
    try:
        name = str(name_method()) if callable(name_method) else str(
            getattr(value, "name", "") or value)
    except Exception:
        name = str(value)
    element_method = getattr(value, "ElementSize", None)
    try:
        element_size = int(element_method()) if callable(element_method) else None
    except Exception:
        element_size = None
    comp_count = max(0, _safe_int(getattr(value, "compCount", 0), 0))
    comp_width = max(0, _safe_int(getattr(value, "compByteWidth", 0), 0))
    special = _method_bool(value, "Special", False)
    if not element_size and not special and comp_count and comp_width:
        element_size = comp_count * comp_width
    return {
        "name": name.replace("\x00", "")[:MAX_RESOURCE_TEXT],
        "type": _enum_text(getattr(value, "type", None)),
        "compType": _enum_text(getattr(value, "compType", None)),
        "compCount": comp_count,
        "compByteWidth": comp_width,
        "flags": _safe_int(getattr(value, "flags", 0), 0),
        "special": special,
        "bgraOrder": _method_bool(value, "BGRAOrder", False),
        "srgbCorrected": _method_bool(value, "SRGBCorrected", False),
        "blockFormat": _method_bool(value, "BlockFormat", False),
        "yuvSubsampling": _method_int(value, "YUVSubsampling", 0),
        "yuvPlaneCount": _method_int(value, "YUVPlaneCount", 1),
        "elementByteSize": element_size if element_size and element_size > 0 else None,
    }


def _component_kind(value):
    text = str(value or "").lower().replace("comptype.", "")
    text = re.sub(r"[^a-z]", "", text)
    aliases = {
        "uint": "uint", "unsignedint": "uint",
        "sint": "sint", "int": "sint", "signedint": "sint",
        "float": "float", "double": "float",
        "unorm": "unorm", "unormsrgb": "unormsrgb",
        "snorm": "snorm", "uscaled": "uscaled", "sscaled": "sscaled",
    }
    return aliases.get(text, text)


def decode_regular_vertex_element(format_info, data):
    """Decode one unambiguous regular element into canonical Python floats."""

    if not isinstance(format_info, dict):
        raise UnsupportedVertexFormat("format metadata is unavailable")
    if format_info.get("special"):
        raise UnsupportedVertexFormat("packed or special vertex formats are not decoded")
    count = _safe_int(format_info.get("compCount"), 0)
    width = _safe_int(format_info.get("compByteWidth"), 0)
    element_size = _safe_int(format_info.get("elementByteSize"), 0)
    if count < 1 or count > 16 or width not in (1, 2, 4, 8):
        raise UnsupportedVertexFormat("unsupported component count or byte width")
    if element_size != count * width or len(data) != element_size:
        raise UnsupportedVertexFormat("vertex element size is inconsistent")
    kind = _component_kind(format_info.get("compType"))
    if kind == "float":
        chars = {2: "e", 4: "f", 8: "d"}
        if width not in chars:
            raise UnsupportedVertexFormat("unsupported floating-point width")
        values = struct.unpack("<%d%s" % (count, chars[width]), data)
        values = [float(value) for value in values]
    elif kind in ("uint", "unorm", "uscaled"):
        chars = {1: "B", 2: "H", 4: "I", 8: "Q"}
        values = list(struct.unpack("<%d%s" % (count, chars[width]), data))
        if kind == "unorm":
            maximum = float((1 << (width * 8)) - 1)
            values = [float(value) / maximum for value in values]
        else:
            values = [float(value) for value in values]
    elif kind in ("sint", "snorm", "sscaled"):
        chars = {1: "b", 2: "h", 4: "i", 8: "q"}
        values = list(struct.unpack("<%d%s" % (count, chars[width]), data))
        if kind == "snorm":
            maximum = float((1 << (width * 8 - 1)) - 1)
            values = [max(-1.0, float(value) / maximum) for value in values]
        else:
            values = [float(value) for value in values]
    else:
        raise UnsupportedVertexFormat(
            "unsupported component type: %s" % str(format_info.get("compType")))
    if format_info.get("bgraOrder") and len(values) >= 4:
        values[0], values[2] = values[2], values[0]
    return values


def encode_canonical_float32(format_info, elements):
    """Decode source elements and concatenate deterministic ``float32le`` bytes."""

    output = bytearray()
    components = _safe_int(format_info.get("compCount"), 0)
    for element in elements:
        values = decode_regular_vertex_element(format_info, element)
        if len(values) != components:
            raise UnsupportedVertexFormat("decoded component count changed")
        try:
            encoded = struct.pack("<%df" % components, *values)
        except (OverflowError, struct.error):
            raise UnsupportedVertexFormat("vertex values exceed the canonical float32 range; raw source is preserved")
        if (_component_kind(format_info.get("compType")) == "float"
                and _safe_int(format_info.get("compByteWidth"), 0) == 8
                and any(decoded != original for decoded, original in
                        zip(struct.unpack("<%df" % components, encoded), values))):
            raise UnsupportedVertexFormat("float64 vertex data requires a precision-preserving target; float32 would lose bits")
        if _component_kind(format_info.get("compType")) in ("uint", "sint"):
            width = _safe_int(format_info.get("compByteWidth"), 0)
            # IDs/bitfields are not approximate colors: rounding a UInt32 to
            # float32 changes Shader behavior. Retain raw evidence instead.
            chars = ({1: "B", 2: "H", 4: "I", 8: "Q"} if _component_kind(format_info.get("compType")) == "uint"
                     else {1: "b", 2: "h", 4: "i", 8: "q"})
            original = list(struct.unpack("<%d%s" % (components, chars[width]), element))
            if format_info.get("bgraOrder") and components >= 4:
                original[0], original[2] = original[2], original[0]
            if any(decoded != integer for decoded, integer in
                   zip(struct.unpack("<%df" % components, encoded), original)):
                raise UnsupportedVertexFormat("integer vertex data requires an exact integer/raw-buffer target; float32 would lose bits")
        output.extend(encoded)
    return bytes(output)


def _format_semantic(name):
    text = str(name or "")
    match = _SEMANTIC_INDEX.match(text)
    if match and match.group(1):
        return match.group(1), int(match.group(2))
    return text, 0


def _pixel_value(value):
    if value is None:
        return None
    result = {}
    for source, target in (
            ("floatValue", "float"), ("uintValue", "uint"),
            ("intValue", "int"), ("f32v", "float"),
            ("u32v", "uint"), ("s32v", "int")):
        raw = getattr(value, source, None)
        if raw is None:
            continue
        try:
            values = list(raw[:4])
        except Exception:
            continue
        if values:
            result[target] = values
    return result or None


def _generic_canonical(value, format_info, count):
    pixel = _pixel_value(value) or {}
    kind = _component_kind(format_info.get("compType"))
    candidates = (
        pixel.get("float") if kind == "float" else
        pixel.get("uint") if kind in ("uint", "unorm", "uscaled") else
        pixel.get("int") if kind in ("sint", "snorm", "sscaled") else None
    )
    components = _safe_int(format_info.get("compCount"), 0)
    if not candidates or components < 1 or len(candidates) < components:
        raise UnsupportedVertexFormat("generic vertex value is unavailable")
    values = [float(item) for item in candidates[:components]]
    try:
        row = struct.pack("<%df" % components, *values)
    except (OverflowError, struct.error):
        raise UnsupportedVertexFormat("generic vertex values exceed the canonical float32 range")
    if kind in ("uint", "sint") and any(decoded != original for decoded, original in
            zip(struct.unpack("<%df" % components, row), candidates[:components])):
        raise UnsupportedVertexFormat("generic integer vertex data cannot be represented exactly as float32")
    if (kind == "float" and _safe_int(format_info.get("compByteWidth"), 0) == 8
            and any(decoded != original for decoded, original in
                    zip(struct.unpack("<%df" % components, row), values))):
        raise UnsupportedVertexFormat("generic float64 vertex data cannot be represented exactly as float32")
    return row * count


def _descriptor_access(value):
    if value is None:
        return None
    result = {
        "stage": _enum_text(getattr(value, "stage", None)),
        "type": _enum_text(getattr(value, "type", None)),
        "bindingIndex": _safe_int(getattr(value, "index", -1), -1),
        "arrayElement": _safe_int(getattr(value, "arrayElement", 0), 0),
        "descriptorStoreId": _resource_id(getattr(value, "descriptorStore", None)),
        "descriptorByteOffset": _safe_int(getattr(value, "byteOffset", 0), 0),
        "descriptorByteSize": _safe_int(getattr(value, "byteSize", 0), 0),
    }
    statically_unused = getattr(value, "staticallyUnused", None)
    if isinstance(statically_unused, bool):
        result["staticallyUnused"] = statically_unused
    result["directAccess"] = result["bindingIndex"] == 0xFFFF
    return result


def _swizzle(value):
    if value is None:
        return None
    result = {}
    for component in ("red", "green", "blue", "alpha", "r", "g", "b", "a"):
        raw = getattr(value, component, None)
        if raw is not None:
            result[component] = _enum_text(raw)
    return result or _enum_text(value)


def _descriptor(value):
    if value is None:
        return None
    result = {
        "type": _enum_text(getattr(value, "type", None)),
        "flags": _safe_int(getattr(value, "flags", 0), 0),
        "viewFormat": normalise_resource_format(getattr(value, "format", None)),
        "resourceId": _resource_id(getattr(value, "resource", None)),
        "secondaryResourceId": _resource_id(getattr(value, "secondary", None)),
        "viewResourceId": _resource_id(getattr(value, "view", None)),
        "byteOffset": _safe_int(getattr(value, "byteOffset", 0), 0),
        "byteSize": _safe_int(getattr(value, "byteSize", 0), 0),
        "counterByteOffset": _safe_int(getattr(value, "counterByteOffset", 0), 0),
        "bufferStructCount": _safe_int(getattr(value, "bufferStructCount", 0), 0),
        "elementByteSize": _safe_int(getattr(value, "elementByteSize", 0), 0),
        "minLODClamp": _finite_number(getattr(value, "minLODClamp", None)),
        "firstMip": _safe_int(getattr(value, "firstMip", 0), 0),
        "numMips": _safe_int(getattr(value, "numMips", 0), 0),
        "firstSlice": _safe_int(getattr(value, "firstSlice", 0), 0),
        "numSlices": _safe_int(getattr(value, "numSlices", 0), 0),
        "swizzle": _swizzle(getattr(value, "swizzle", None)),
        "textureType": _enum_text(getattr(value, "textureType", None)),
    }
    return result


def _sampler(value):
    if value is None:
        return None
    result = {
        "samplerResourceId": _resource_id(getattr(value, "object", None)),
        "type": _enum_text(getattr(value, "type", None)),
        "addressU": _enum_text(getattr(value, "addressU", None)),
        "addressV": _enum_text(getattr(value, "addressV", None)),
        "addressW": _enum_text(getattr(value, "addressW", None)),
        "compareFunction": _enum_text(getattr(value, "compareFunction", None)),
        "filter": _enum_text(getattr(value, "filter", None)),
        "srgbBorder": bool(getattr(value, "srgbBorder", False)),
        "seamlessCubemaps": bool(getattr(value, "seamlessCubemaps", False)),
        "unnormalized": bool(getattr(value, "unnormalized", False)),
        "maxAnisotropy": _finite_number(getattr(value, "maxAnisotropy", None)),
        "minLOD": _finite_number(getattr(value, "minLOD", None)),
        "maxLOD": _finite_number(getattr(value, "maxLOD", None)),
        "mipBias": _finite_number(getattr(value, "mipBias", None)),
        "borderColor": _pixel_value(getattr(value, "borderColorValue", None)),
        "borderColorType": _enum_text(getattr(value, "borderColorType", None)),
        "swizzle": _swizzle(getattr(value, "swizzle", None)),
        "ycbcrModel": _enum_text(getattr(value, "ycbcrModel", None)),
        "ycbcrRange": _enum_text(getattr(value, "ycbcrRange", None)),
        "xChromaOffset": _enum_text(getattr(value, "xChromaOffset", None)),
        "yChromaOffset": _enum_text(getattr(value, "yChromaOffset", None)),
        "chromaFilter": _enum_text(getattr(value, "chromaFilter", None)),
        "forceExplicitReconstruction": bool(
            getattr(value, "forceExplicitReconstruction", False)),
        "creationTimeConstant": bool(getattr(value, "creationTimeConstant", False)),
        "ycbcrSamplerId": _resource_id(getattr(value, "ycbcrSampler", None)),
    }
    return result


def _texture_description(value):
    return {
        "resourceId": _resource_id(getattr(value, "resourceId", None)),
        "format": normalise_resource_format(getattr(value, "format", None)),
        "dimension": _safe_int(getattr(value, "dimension", 0), 0),
        "type": _enum_text(getattr(value, "type", None)),
        "width": _safe_int(getattr(value, "width", 0), 0),
        "height": _safe_int(getattr(value, "height", 0), 0),
        "depth": _safe_int(getattr(value, "depth", 0), 0),
        "cubemap": bool(getattr(value, "cubemap", False)),
        "mips": _safe_int(getattr(value, "mips", 0), 0),
        "arraySize": _safe_int(getattr(value, "arraysize", 0), 0),
        "creationFlags": _enum_text(getattr(value, "creationFlags", None)),
        "sampleQuality": _safe_int(getattr(value, "msQual", 0), 0),
        "sampleCount": _safe_int(getattr(value, "msSamp", 0), 0),
        "byteSize": _safe_int(getattr(value, "byteSize", 0), 0),
    }


def _bound_buffer(value):
    descriptor = getattr(value, "descriptor", None)
    candidates = [descriptor, value] if descriptor is not None else [value]
    result = {"resourceId": 0, "byteOffset": 0, "byteSize": 0, "byteStride": None}
    for candidate in candidates:
        if candidate is None:
            continue
        resource_id = _resource_id(getattr(candidate, "resource", None))
        if not resource_id:
            resource_id = _resource_id(getattr(candidate, "resourceId", None))
        if resource_id:
            result["resourceId"] = resource_id
        for field in ("byteOffset", "byteSize"):
            raw = getattr(candidate, field, None)
            if raw is not None:
                result[field] = max(0, _safe_int(raw, 0))
        stride = getattr(candidate, "byteStride", None)
        if isinstance(stride, int) and not isinstance(stride, bool) and stride >= 0:
            result["byteStride"] = int(stride)
        if result["resourceId"]:
            break
    return result


def _controller_resource_object(ctrl, resource_id, include_textures=False):
    sources = []
    try:
        sources.extend(list(ctrl.GetBuffers() or []))
    except Exception:
        pass
    if include_textures:
        try:
            sources.extend(list(ctrl.GetTextures() or []))
        except Exception:
            pass
    try:
        sources.extend(list(ctrl.GetResources() or []))
    except Exception:
        pass
    for description in sources:
        candidate = getattr(description, "resourceId", description)
        if _resource_id(candidate) == resource_id:
            return candidate
    return None


def _find_action(actions, event_id):
    remaining = [MAX_ACTIONS_SCANNED]

    def walk(values):
        for action in values or []:
            remaining[0] -= 1
            if remaining[0] < 0:
                raise RuntimeError("action tree exceeds the resource export safety bound")
            if _safe_int(getattr(action, "eventId", 0), 0) == event_id:
                return action
            found = walk(getattr(action, "children", None) or [])
            if found is not None:
                return found
        return None

    return walk(actions)


def _action_record(action):
    result = {
        "eventId": _safe_int(getattr(action, "eventId", 0), 0),
        "name": str(getattr(action, "customName", "") or getattr(action, "name", "") or "")[:1024],
        "flags": _safe_int(getattr(action, "flags", 0), 0),
        "indexCount": _safe_int(getattr(action, "numIndices", 0), 0),
        "vertexCount": _safe_int(
            getattr(action, "numVertices", getattr(action, "numIndices", 0)), 0),
        "instanceCount": _safe_int(getattr(action, "numInstances", 0), 0),
        "indexOffset": _safe_int(getattr(action, "indexOffset", 0), 0),
        "vertexOffset": _safe_int(getattr(action, "vertexOffset", 0), 0),
        "instanceOffset": _safe_int(getattr(action, "instanceOffset", 0), 0),
        "baseVertex": _safe_int(getattr(action, "baseVertex", 0), 0),
    }
    draw_index = getattr(action, "drawIndex", None)
    result["drawIndex"] = _safe_int(draw_index, -1) if draw_index is not None else None
    return result


class _ArtifactWriter(object):
    def __init__(self, artifact_dir):
        self.artifact_dir = artifact_dir
        self.files = []
        self.created_files = []
        self.created_directories = []

    def _absolute(self, relative):
        relative = _relative_path(relative, "artifact path")
        absolute = _normal_path(os.path.join(self.artifact_dir, relative))
        if not _contained(self.artifact_dir, absolute):
            raise RuntimeError("artifact path escaped the export directory")
        return relative.replace(os.sep, "/"), absolute

    def _parents(self, absolute):
        parent = os.path.dirname(absolute)
        pending = []
        while not _same_path(parent, self.artifact_dir):
            if os.path.lexists(parent):
                _require_plain_directory(parent, "artifact path parent")
                break
            pending.append(parent)
            parent = os.path.dirname(parent)
            if not _contained(self.artifact_dir, parent):
                raise RuntimeError("artifact parent escaped the export directory")
        for directory in reversed(pending):
            os.mkdir(directory)
            self.created_directories.append(directory)

    def write_bytes(self, relative, data, kind):
        relative, absolute = self._absolute(relative)
        self._parents(absolute)
        if os.path.lexists(absolute):
            raise RuntimeError("resource export artifact already exists: %s" % relative)
        temporary = os.path.join(
            os.path.dirname(absolute), ".tmp-%s" % uuid.uuid4().hex)
        try:
            with open(temporary, "xb") as stream:
                stream.write(data)
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
            os.replace(temporary, absolute)
        finally:
            if os.path.lexists(temporary):
                os.remove(temporary)
        self.created_files.append(absolute)
        size, digest = _hash_regular_file(absolute)
        record = {
            "path": relative,
            "size": size,
            "byteLength": size,
            "sha256": digest,
            "kind": kind,
        }
        self.files.append(record)
        return dict(record)

    def write_json(self, relative, value, kind):
        return self.write_bytes(relative, canonical_json_bytes(value), kind)

    def texture_destination(self, relative, suffix="dds"):
        relative, absolute = self._absolute(relative)
        self._parents(absolute)
        if os.path.lexists(absolute):
            raise RuntimeError("resource export texture already exists: %s" % relative)
        temporary = os.path.join(
            os.path.dirname(absolute), ".tmp-%s.%s" % (uuid.uuid4().hex, suffix))
        return relative, absolute, temporary

    def adopt_texture(self, relative, absolute, temporary, encoding="dds"):
        size, digest = _hash_regular_file(
            temporary, MAX_TEXTURE_FILE_BYTES, allow_empty=False)
        os.replace(temporary, absolute)
        self.created_files.append(absolute)
        record = {
            "path": relative,
            "size": size,
            "byteLength": size,
            "sha256": digest,
            "kind": "texture-%s" % encoding,
        }
        self.files.append(record)
        return dict(record)

    def cleanup(self):
        for path in reversed(self.created_files):
            try:
                if os.path.isfile(path) and not _is_redirected(path):
                    os.remove(path)
            except OSError:
                pass
        for path in reversed(self.created_directories):
            try:
                os.rmdir(path)
            except OSError:
                pass


def _action_is_indexed(action, rd_module):
    """Return the indexed-draw fact without consulting possibly stale IA state."""

    flags = getattr(action, "flags", None)
    for enum_name in ("ActionFlags", "DrawFlags"):
        enum_type = getattr(rd_module, enum_name, None)
        indexed_flag = getattr(enum_type, "Indexed", None)
        if indexed_flag is None or flags is None:
            continue
        try:
            return bool(flags & indexed_flag)
        except Exception:
            mask = _safe_int(indexed_flag, 0)
            value = _safe_int(flags, -1)
            if mask and value >= 0:
                return bool(value & mask)

    # This fallback is only for adapters which provide an explicit boolean
    # action fact instead of RenderDoc's ActionFlags enum.  Never infer the
    # draw kind from a bound index buffer: that binding can legally be stale.
    for name in ("indexed", "isIndexed"):
        if not hasattr(action, name):
            continue
        value = getattr(action, name)
        try:
            value = value() if callable(value) else value
        except Exception:
            continue
        if isinstance(value, bool):
            return value
    raise RuntimeError("indexed draw state is unavailable from the action flags")


def _read_indices(ctrl, pipeline, action, rd_module, writer, capture):
    count = _safe_int(getattr(action, "numIndices", 0), 0)
    if count < 0 or count > MAX_VERTEX_REFERENCES:
        raise RuntimeError("draw index count exceeds the resource export safety bound")
    indexed = _action_is_indexed(action, rd_module)
    if indexed:
        binding = _bound_buffer(pipeline.GetIBuffer())
        stride = _safe_int(binding.get("byteStride"), 0)
        if not binding["resourceId"]:
            raise RuntimeError("indexed draw has no bound index buffer")
        if stride not in (1, 2, 4):
            raise RuntimeError("indexed draw has an unavailable index byte stride")
    else:
        # Deliberately do not query or consume the IA index binding.  A
        # non-indexed action may retain a stale buffer from an earlier draw.
        binding = {
            "resourceId": 0, "byteOffset": 0, "byteSize": 0,
            "byteStride": None,
        }
        stride = 0
    restart_enabled = False
    restart_index = None
    if indexed:
        try:
            restart_enabled = bool(pipeline.IsRestartEnabled())
        except Exception:
            restart_enabled = False
        if restart_enabled:
            try:
                restart_index = _safe_int(pipeline.GetRestartIndex(), 0) & (
                    (1 << (stride * 8)) - 1)
            except Exception:
                restart_enabled = False
                restart_index = None
    source_raw = None
    if indexed:
        byte_offset = binding["byteOffset"] + (
            _safe_int(getattr(action, "indexOffset", 0), 0) * stride)
        byte_length = count * stride
        if binding["byteSize"] and byte_offset + byte_length > (
                binding["byteOffset"] + binding["byteSize"]):
            raise RuntimeError("index buffer range is truncated")
        resource_object = _controller_resource_object(ctrl, binding["resourceId"])
        if resource_object is None:
            raise RuntimeError("index buffer ResourceId is unavailable")
        source_raw = bytes(ctrl.GetBufferData(
            resource_object, byte_offset, byte_length) or b"")
        if len(source_raw) != byte_length:
            raise RuntimeError("RenderDoc returned a truncated index buffer")
        chars = {1: "B", 2: "H", 4: "I"}
        source_indices = list(struct.unpack(
            "<%d%s" % (count, chars[stride]), source_raw)) if count else []
        base_vertex = _safe_int(getattr(action, "baseVertex", 0), 0)
        logical = [
            None if restart_enabled and value == restart_index else value + base_vertex
            for value in source_indices
        ]
        source_record = {
            "status": "captured" if capture else "available",
            "resourceId": binding["resourceId"],
            "byteOffset": byte_offset,
            "byteLength": byte_length,
            "encoding": "uint%dle" % (stride * 8),
            "restartEnabled": restart_enabled,
            "restartIndex": restart_index,
        }
        if capture:
            raw_file = writer.write_bytes(
                "mesh/indices.raw.bin", source_raw, "mesh-index-source")
            source_record.update(raw_file)
    else:
        first_vertex = _safe_int(getattr(action, "vertexOffset", 0), 0)
        logical = list(range(first_vertex, first_vertex + count))
        source_record = {
            "status": "not-applicable",
            "resourceId": 0,
            "byteOffset": 0,
            "byteLength": 0,
            "encoding": None,
            "restartEnabled": False,
            "restartIndex": None,
        }

    compact = []
    compact_lookup = {}
    canonical_indices = []
    for value in logical:
        if value is None:
            canonical_indices.append(0xFFFFFFFF)
            continue
        if value < 0 or value > 0xFFFFFFFF:
            raise RuntimeError("draw references an invalid logical vertex index")
        if value not in compact_lookup:
            compact_lookup[value] = len(compact)
            compact.append(value)
        canonical_indices.append(compact_lookup[value])
    canonical_bytes = bytearray()
    for value in canonical_indices:
        canonical_bytes.extend(struct.pack("<I", value))
    logical_bytes = bytearray()
    for value in compact:
        logical_bytes.extend(struct.pack("<I", value))
    canonical = {
        "status": (
            "unsupported" if restart_enabled else
            "captured" if capture else "available"),
        "count": len(canonical_indices),
        "components": 1,
        "encoding": "uint32le",
        "restartIndex": 0xFFFFFFFF if restart_enabled else None,
    }
    logical_record = {
        "status": "captured" if capture else "available",
        "count": len(compact),
        "components": 1,
        "encoding": "uint32le",
    }
    if restart_enabled:
        canonical["path"] = None
        canonical["sha256"] = None
        canonical["reason"] = (
            "primitive restart requires topology-aware primitive splitting; source indices are preserved")
    elif capture:
        canonical.update(writer.write_bytes(
            "mesh/indices.uint32le.bin", bytes(canonical_bytes),
            "mesh-index-canonical"))
        logical_record.update(writer.write_bytes(
            "mesh/logical-vertex-indices.uint32le.bin", bytes(logical_bytes),
            "mesh-logical-vertex-indices"))
    return {
        "indexed": indexed,
        "sourceRaw": source_record,
        "canonical": canonical,
    }, compact, logical_record


def _attribute_metadata(attribute, index, vertex_buffers):
    name = str(getattr(attribute, "name", "") or "")[:MAX_RESOURCE_TEXT]
    semantic, semantic_index = _format_semantic(name)
    location_value = getattr(attribute, "location", None)
    location = None
    if isinstance(location_value, int) and not isinstance(location_value, bool):
        if 0 <= location_value <= 0x7FFFFFFF:
            location = int(location_value)
    slot = _safe_int(getattr(attribute, "vertexBuffer", -1), -1)
    binding = _bound_buffer(vertex_buffers[slot]) if 0 <= slot < len(vertex_buffers) else {
        "resourceId": 0, "byteOffset": 0, "byteSize": 0, "byteStride": None}
    source_format = normalise_resource_format(getattr(attribute, "format", None))
    return {
        "index": index,
        "location": location,
        "locationStatus": "observed" if location is not None else "unavailable",
        "name": name,
        "semantic": semantic,
        "semanticIndex": semantic_index,
        "semanticStatus": "inferred",
        "semanticSource": "attribute.name",
        "vertexBuffer": slot,
        "byteOffset": _safe_int(getattr(attribute, "byteOffset", 0), 0),
        "byteStride": binding.get("byteStride"),
        "perInstance": bool(getattr(attribute, "perInstance", False)),
        "instanceStepRate": _safe_int(getattr(attribute, "instanceRate", 0), 0),
        "genericEnabled": bool(getattr(attribute, "genericEnabled", False)),
        "genericValue": _pixel_value(getattr(attribute, "genericValue", None)),
        "used": getattr(attribute, "used", None),
        "floatCastWrong": getattr(attribute, "floatCastWrong", None),
        "sourceFormat": source_format,
        "buffer": binding,
    }


def _capture_attribute(ctrl, attribute, record, compact_vertices, action, request, writer):
    count = len(compact_vertices)
    components = _safe_int(record["sourceFormat"].get("compCount"), 0)
    record["canonical"] = {
        "status": "unavailable",
        "path": None,
        "count": count,
        "components": components,
        "encoding": "float32le",
        "sha256": None,
    }
    record["raw"] = {
        "status": "unavailable",
        "path": None,
        "count": count,
        "elementByteSize": record["sourceFormat"].get("elementByteSize"),
        "encoding": "source-bytes",
        "sha256": None,
    }
    if record["genericEnabled"]:
        try:
            canonical = _generic_canonical(
                getattr(attribute, "genericValue", None), record["sourceFormat"], count)
            artifact = writer.write_bytes(
                "mesh/attributes/%03d.float32le.bin" % record["index"],
                canonical, "mesh-attribute-canonical")
            record["canonical"].update(artifact)
            record["canonical"]["status"] = "captured"
            record["raw"]["status"] = "not-applicable-generic-value"
        except UnsupportedVertexFormat as exc:
            record["canonical"]["reason"] = str(exc)
            record["raw"]["status"] = "not-applicable-generic-value"
        return

    binding = record["buffer"]
    stride = binding.get("byteStride")
    element_size = _safe_int(record["sourceFormat"].get("elementByteSize"), 0)
    # A Vulkan stride of zero reads the same bound element at every vertex.
    constant_stride = stride == 0 and request.get("api") in ("Vulkan", "GraphicsAPI.Vulkan")
    if not binding["resourceId"] or stride is None or (stride <= 0 and not constant_stride) or element_size <= 0:
        record["canonical"]["reason"] = "vertex buffer binding or element size is unavailable"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    if not constant_stride and element_size > stride:
        record["canonical"]["reason"] = "vertex element exceeds its bound stride"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return

    if record["perInstance"]:
        rate = max(0, record["instanceStepRate"])
        first_instance = _safe_int(getattr(action, "instanceOffset", 0), 0)
        # A zero step rate is a constant per-draw attribute selected at
        # firstInstance.  Positive divisors advance relative to the requested
        # draw-local instance, then add firstInstance.
        source_index = first_instance if rate == 0 else (
            first_instance + request["instance"] // rate)
        source_indices = [source_index] * count
    else:
        source_indices = list(compact_vertices)
    if any(value < 0 for value in source_indices):
        record["canonical"]["reason"] = "attribute references a negative source element"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    if not source_indices:
        source_indices = []
        minimum = 0
        maximum = -1
        span = 0
    else:
        minimum = min(source_indices)
        maximum = max(source_indices)
        span = (maximum - minimum) * stride + element_size
    if span > MAX_VERTEX_SPAN_BYTES:
        record["canonical"]["reason"] = "attribute source span exceeds the safety bound"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    base = binding["byteOffset"] + record["byteOffset"] + minimum * stride
    if base < binding["byteOffset"]:
        record["canonical"]["reason"] = "attribute source offset underflowed its binding"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    if binding["byteSize"] and base + span > binding["byteOffset"] + binding["byteSize"]:
        record["canonical"]["reason"] = "attribute source span exceeds its bound vertex buffer"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    resource_object = _controller_resource_object(ctrl, binding["resourceId"])
    if resource_object is None and span:
        record["canonical"]["reason"] = "vertex buffer ResourceId is unavailable"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    source = bytes(ctrl.GetBufferData(resource_object, base, span) or b"") if span else b""
    if len(source) != span:
        record["canonical"]["reason"] = "RenderDoc returned a truncated vertex buffer span"
        record["raw"]["reason"] = record["canonical"]["reason"]
        return
    elements = []
    for source_index in source_indices:
        offset = (source_index - minimum) * stride
        element = source[offset:offset + element_size]
        if len(element) != element_size:
            record["canonical"]["reason"] = "vertex element is truncated"
            record["raw"]["reason"] = record["canonical"]["reason"]
            return
        elements.append(element)
    raw = b"".join(elements)
    raw_artifact = writer.write_bytes(
        "mesh/attributes/%03d.raw.bin" % record["index"], raw,
        "mesh-attribute-source")
    record["raw"].update(raw_artifact)
    record["raw"].update({
        "status": "captured",
        "sourceSpanByteOffset": base,
        "sourceSpanByteLength": span,
        "elementOrder": "compact-vertex-order",
    })
    try:
        canonical = encode_canonical_float32(record["sourceFormat"], elements)
    except (UnsupportedVertexFormat, OverflowError, struct.error) as exc:
        record["canonical"]["status"] = "unsupported"
        record["canonical"]["reason"] = str(exc)
        return
    canonical_artifact = writer.write_bytes(
        "mesh/attributes/%03d.float32le.bin" % record["index"], canonical,
        "mesh-attribute-canonical")
    record["canonical"].update(canonical_artifact)
    record["canonical"]["status"] = "captured"


def _mesh(ctrl, pipeline, action, request, rd_module, writer, capture):
    try:
        topology = _enum_text(pipeline.GetPrimitiveTopology())
    except Exception:
        topology = None
    vertex_buffers = list(pipeline.GetVBuffers() or [])
    attributes = list(pipeline.GetVertexInputs() or [])
    if len(attributes) > MAX_VERTEX_ATTRIBUTES:
        raise RuntimeError("vertex input layout exceeds the resource export safety bound")
    indices, compact_vertices, logical_record = _read_indices(
        ctrl, pipeline, action, rd_module, writer, capture)
    records = []
    for index, attribute in enumerate(attributes):
        record = _attribute_metadata(attribute, index, vertex_buffers)
        if capture:
            _capture_attribute(
                ctrl, attribute, record, compact_vertices, action, request, writer)
        else:
            supported = not record["sourceFormat"].get("special") and (
                _component_kind(record["sourceFormat"].get("compType")) in
                ("float", "uint", "sint", "unorm", "snorm", "uscaled", "sscaled"))
            record["canonical"] = {
                "status": "available" if supported else "unsupported",
                "path": None,
                "count": len(compact_vertices),
                "components": record["sourceFormat"].get("compCount", 0),
                "encoding": "float32le",
                "sha256": None,
            }
            record["raw"] = {
                "status": "available" if record["buffer"]["resourceId"] else "unavailable",
                "path": None,
                "count": len(compact_vertices),
                "elementByteSize": record["sourceFormat"].get("elementByteSize"),
                "encoding": "source-bytes",
                "sha256": None,
            }
        records.append(record)
    return {
        "source": "VSIn",
        "topology": topology,
        "coordinateTransformApplied": False,
        "coordinateConvention": "capture-native-application-basis-unknown",
        "instance": request["instance"],
        "view": request["view"],
        "indexed": indices["indexed"],
        "compactVertexCount": len(compact_vertices),
        "logicalVertexIndices": logical_record,
        "indices": indices,
        "attributes": records,
    }


def _stage_resources(pipeline, stages, texture_lookup):
    textures = {}
    samplers = []
    bound_shader_stages = []
    unexported = []
    for stage_name, stage in stages:
        try:
            shader_id = _resource_id(pipeline.GetShader(stage))
        except Exception:
            shader_id = 0
        if shader_id:
            bound_shader_stages.append(stage_name)
        method = getattr(pipeline, "GetReadOnlyResources", None)
        if callable(method):
            try:
                try:
                    bindings = list(method(stage, False) or [])
                except TypeError:
                    bindings = list(method(stage) or [])
            except Exception as exc:
                bindings = []
                if shader_id:
                    unexported.append({"stage": stage_name, "kind": "read-only-resources",
                        "status": "unavailable", "reason": str(exc)[:1024]})
            for item in bindings:
                descriptor_value = getattr(item, "descriptor", item)
                descriptor = _descriptor(descriptor_value)
                resource_id = descriptor.get("resourceId", 0) if descriptor else 0
                if resource_id not in texture_lookup:
                    access = _descriptor_access(getattr(item, "access", None))
                    if resource_id and not (access or {}).get("staticallyUnused"):
                        unexported.append({"stage": stage_name, "kind": "read-only-buffer",
                            "resourceId": resource_id, "access": access, "descriptor": descriptor,
                            "status": "content-not-exported"})
                    continue
                entry = textures.setdefault(resource_id, {
                    "resourceId": resource_id,
                    "resource": _texture_description(texture_lookup[resource_id]),
                    "bindings": [],
                })
                binding = {
                    "stage": stage_name,
                    "access": _descriptor_access(getattr(item, "access", None)),
                    "descriptor": descriptor,
                }
                combined = _sampler(getattr(item, "sampler", None))
                if combined and any(value not in (None, 0, False, "") for value in combined.values()):
                    binding["combinedSampler"] = combined
                entry["bindings"].append(binding)
        sampler_method = getattr(pipeline, "GetSamplers", None)
        if callable(sampler_method):
            try:
                try:
                    stage_samplers = list(sampler_method(stage, False) or [])
                except TypeError:
                    stage_samplers = list(sampler_method(stage) or [])
            except Exception:
                stage_samplers = []
            for item in stage_samplers:
                sampler_value = getattr(item, "sampler", item)
                samplers.append({
                    "stage": stage_name,
                    "access": _descriptor_access(getattr(item, "access", None)),
                    "sampler": _sampler(sampler_value),
                })
        writable_method = getattr(pipeline, "GetReadWriteResources", None)
        if callable(writable_method):
            try:
                try:
                    writable = list(writable_method(stage, False) or [])
                except TypeError:
                    writable = list(writable_method(stage) or [])
            except Exception as exc:
                writable = []
                if shader_id:
                    unexported.append({"stage": stage_name, "kind": "read-write-resources",
                        "status": "unavailable", "reason": str(exc)[:1024]})
            for item in writable:
                descriptor = _descriptor(getattr(item, "descriptor", item))
                resource_id = (descriptor or {}).get("resourceId", 0)
                access = _descriptor_access(getattr(item, "access", None))
                if resource_id and not (access or {}).get("staticallyUnused"):
                    unexported.append({"stage": stage_name, "kind": "read-write-resource",
                        "resourceId": resource_id, "access": access, "descriptor": descriptor,
                        "status": "content-not-exported"})
    if len(textures) > MAX_TEXTURES:
        raise RuntimeError("bound texture count exceeds the resource export safety bound")
    return list(textures.values()), samplers, bound_shader_stages, unexported


# What a DCC actually opens. DDS stays as the exact evidence copy (all mips, all
# slices, no conversion); these are the readable copies beside it.
_TEXTURE_FILE_TYPES = {
    "tga": "TGA",
    "exr": "EXR",
    "png": "PNG",
    "jpg": "JPG",
    "hdr": "HDR",
    "bmp": "BMP",
}
_FLOAT_FORMAT_MARKERS = ("FLOAT", "SFLOAT", "R16G16B16A16", "R32G32B32A32", "R11G11B10", "R9G9B9E5")


def _auto_texture_formats(texture):
    """LDR surfaces become TGA, floating-point surfaces become EXR."""

    resource = texture.get("resource") or {}
    name = str((resource.get("format") or {}).get("name", "") or
               resource.get("formatName", "") or "").upper()
    if any(marker in name for marker in _FLOAT_FORMAT_MARKERS):
        return ("exr",)
    return ("tga", "png")


def _save_texture_copy(ctrl, rd_module, texture, writer, resource_object, encoding, sample_count):
    """Save one readable copy of a bound texture next to its DDS evidence."""

    file_types = getattr(rd_module, "FileType", None)
    texture_save_type = getattr(rd_module, "TextureSave", None)
    member = _TEXTURE_FILE_TYPES.get(encoding)
    file_type = getattr(file_types, member, None) if (file_types and member) else None
    if file_type is None or texture_save_type is None:
        return {"encoding": encoding, "status": "unavailable",
                "reason": "RenderDoc cannot write %s in this build" % encoding}
    relative = "textures/resource-%d.%s" % (texture["resourceId"], encoding)
    try:
        relative, absolute, temporary = writer.texture_destination(relative, encoding)
        save = texture_save_type()
        save.resourceId = resource_object
        save.destType = file_type
        # Single-image formats carry one mip and one slice; say so rather than
        # pretending the copy is complete evidence.
        if hasattr(save, "mip"):
            save.mip = 0
        if hasattr(save, "slice") and hasattr(save.slice, "sliceIndex"):
            save.slice.sliceIndex = 0
        sample_mapping = getattr(save, "sample", None)
        if sample_mapping is not None and hasattr(sample_mapping, "sampleIndex"):
            if hasattr(sample_mapping, "mapToArray"):
                sample_mapping.mapToArray = False
            sample_mapping.sampleIndex = 0 if sample_count > 1 else 0
        ctrl.SaveTexture(save, temporary)
        artifact = writer.adopt_texture(relative, absolute, temporary, encoding=encoding)
        return dict(artifact, status="captured", encoding=encoding,
                    scope="mip 0, slice 0" if sample_count <= 1 else "mip 0, slice 0, sample 0")
    except Exception as exc:
        try:
            if "temporary" in locals() and os.path.lexists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        return {"encoding": encoding, "status": "unavailable", "reason": str(exc)[:1024]}


def _save_texture_dds(ctrl, rd_module, texture, writer, extra_formats=()):
    resource_id = texture["resourceId"]
    relative = "textures/resource-%d.dds" % resource_id
    sample_count = _safe_int(
        texture.get("resource", {}).get("sampleCount", 0), 0)
    if sample_count > 1:
        sample_policy = {
            "status": "pending",
            "mode": "all-samples-as-array",
            "sampleCount": sample_count,
            "lossy": False,
        }
    elif sample_count == 1:
        sample_policy = {
            "status": "exact",
            "mode": "single-sample",
            "sampleCount": 1,
            "lossy": False,
        }
    else:
        sample_policy = {
            "status": "unknown",
            "mode": "renderdoc-default",
            "sampleCount": sample_count,
            "lossy": None,
            "reason": "texture sample count is unavailable",
        }
    texture["samplePolicy"] = sample_policy
    try:
        file_type = getattr(getattr(rd_module, "FileType", None), "DDS", None)
        texture_save_type = getattr(rd_module, "TextureSave", None)
        if file_type is None or texture_save_type is None:
            raise RuntimeError("RenderDoc DDS export is unavailable")
        resource_object = None
        for description in list(ctrl.GetTextures() or []):
            candidate = getattr(description, "resourceId", None)
            if _resource_id(candidate) == resource_id:
                resource_object = candidate
                break
        if resource_object is None:
            raise RuntimeError("bound texture ResourceId is unavailable")
        relative, absolute, temporary = writer.texture_destination(relative)
        save = texture_save_type()
        save.resourceId = resource_object
        save.destType = file_type
        if hasattr(save, "mip"):
            save.mip = -1
        if hasattr(save, "slice") and hasattr(save.slice, "sliceIndex"):
            save.slice.sliceIndex = -1
        if sample_count > 1:
            sample_mapping = getattr(save, "sample", None)
            if (sample_mapping is None or
                    not hasattr(sample_mapping, "mapToArray") or
                    not hasattr(sample_mapping, "sampleIndex")):
                raise RuntimeError(
                    "RenderDoc cannot preserve all MSAA samples as DDS array slices")
            # The default sampleIndex is the resolve sentinel. Make the exact
            # all-samples representation explicit before publishing the DDS.
            sample_mapping.mapToArray = True
            sample_mapping.sampleIndex = 0
            sample_policy["status"] = "exact"
        elif sample_count == 1:
            sample_mapping = getattr(save, "sample", None)
            if sample_mapping is not None:
                if hasattr(sample_mapping, "mapToArray"):
                    sample_mapping.mapToArray = False
                if hasattr(sample_mapping, "sampleIndex"):
                    sample_mapping.sampleIndex = 0
        comp_type = getattr(getattr(rd_module, "CompType", None), "Typeless", None)
        if comp_type is not None and hasattr(save, "typeCast"):
            save.typeCast = comp_type
        ctrl.SaveTexture(save, temporary)
        artifact = writer.adopt_texture(relative, absolute, temporary)
        texture["artifact"] = dict(artifact, status="captured", encoding="dds")
        wanted: list[str] = []
        for encoding in extra_formats:
            if encoding == "auto":
                wanted.extend(_auto_texture_formats(texture))
            elif encoding != "dds":
                wanted.append(encoding)
        copies = []
        for encoding in list(dict.fromkeys(wanted)):
            copies.append(_save_texture_copy(
                ctrl, rd_module, texture, writer, resource_object, encoding, sample_count))
        if copies:
            texture["artifacts"] = copies
    except Exception as exc:
        try:
            if "temporary" in locals() and os.path.lexists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        texture["artifact"] = {
            "status": "unavailable",
            "encoding": "dds",
            "path": None,
            "sha256": None,
            "reason": str(exc)[:2048],
        }
        if sample_count > 1:
            sample_policy["status"] = "unavailable"
            sample_policy["lossy"] = None
            sample_policy["reason"] = str(exc)[:2048]


def _pipeline_snapshot(ctrl, pipeline, stages):
    texture_lookup = {}
    try:
        for texture in list(ctrl.GetTextures() or []):
            texture_lookup[_resource_id(getattr(texture, "resourceId", None))] = texture
    except Exception:
        pass
    textures, samplers, bound_shader_stages, unexported = _stage_resources(
        pipeline, stages, texture_lookup)
    try:
        api = _enum_text(getattr(ctrl.GetAPIProperties(), "pipelineType", None))
    except Exception:
        api = None
    return api, textures, samplers, bound_shader_stages, unexported


def _dispatch_only(action, rd_module):
    flags = getattr(action, "flags", 0)
    dispatch_flag = getattr(getattr(rd_module, "ActionFlags", None), "Dispatch", 0)
    try:
        return bool(dispatch_flag and (int(flags) & int(dispatch_flag)))
    except (TypeError, ValueError):
        return False


def _action_stages(action, rd_module, stages):
    """Bound state from an idle pipeline is not an executed stage of this EID."""
    flags = getattr(action, "flags", 0)
    enum = getattr(rd_module, "ActionFlags", None)

    def has(name):
        value = getattr(enum, name, 0)
        try:
            return bool(value and int(flags) & int(value))
        except (TypeError, ValueError):
            return False

    if has("DispatchRay") or has("RayDispatch"):
        allowed = {"raygen", "intersection", "anyhit", "closesthit", "miss", "callable"}
    elif has("MeshDispatch"):
        allowed = {"task", "mesh", "fragment"}
    elif has("Dispatch"):
        allowed = {"compute"}
    else:
        allowed = {"vertex", "hull", "domain", "geometry", "fragment"}
    return [(name, stage) for name, stage in stages if name in allowed]


def _action_mesh(ctrl, pipeline, action, request, rd_module, writer, capture):
    if _dispatch_only(action, rd_module):
        # IA state can still describe a previous draw at a compute EID. It is
        # not this dispatch's geometry and must never become a fake Mesh.
        return {"source": "not-applicable-dispatch", "topology": None, "attributes": [],
                "indexed": False, "instance": request["instance"], "view": request["view"],
                "managedMeshImport": {"supported": False, "reasonId": "dispatch-requires-render-plan",
                    "requiredRoute": "unity-effect-graph", "effectImportSupported": True,
                    "nextTools": ["finalize_resource_export", "validate_unity_render_plan", "bind_unity_restoration"]}}
    return _mesh(ctrl, pipeline, action, request, rd_module, writer, capture)


def _capture_bound_buffers(ctrl, requests, mesh, event_id, writer, rd_module=None):
    """Export exact selected buffer-view windows before the action, plus UAV outputs.

    ReplayFrameEventForAnalysis is the common controller API: replay through
    EID-1 while retaining the selected EID's cached bindings, then restore in a
    finally block. Never label a post-action snapshot as an input.
    """
    descriptions = {}
    try:
        for item in ctrl.GetBuffers() or []:
            descriptions[_resource_id(item.resourceId)] = item
    except Exception:
        pass
    candidates = list(requests)
    for requested in requests:
        descriptor = requested.get("descriptor") or {}
        secondary = descriptor.get("secondaryResourceId", 0)
        if secondary and requested.get("kind") == "read-write-resource":
            candidates.append({"kind": "read-write-counter", "stage": requested.get("stage"),
                "resourceId": secondary, "access": requested.get("access"),
                "parentResourceId": requested.get("resourceId"), "descriptor": {
                    "resourceId": secondary, "byteOffset": descriptor.get("counterByteOffset", 0),
                    "byteSize": 4, "elementByteSize": 4}, "status": "content-not-exported"})
    for attribute in mesh.get("attributes", []):
        if attribute.get("perInstance") and attribute.get("used") is not False:
            bound = attribute.get("buffer") or {}
            candidates.append({"kind": "per-instance-stream", "stage": "vertex-input",
                "resourceId": bound.get("resourceId", 0), "access": {
                    "attributeIndex": attribute.get("index"), "instanceStepRate": attribute.get("instanceStepRate"),
                    "instance": mesh.get("instance", 0)}, "descriptor": {
                    "resourceId": bound.get("resourceId", 0), "byteOffset": bound.get("byteOffset", 0),
                    "byteSize": bound.get("byteSize", 0), "elementByteSize": bound.get("byteStride", 0)},
                "status": "content-not-exported"})
    windows, gaps, total = {}, [], 0
    for candidate in candidates:
        rid = candidate.get("resourceId", 0)
        descriptor = candidate.get("descriptor") or {}
        description = descriptions.get(rid)
        if not rid or description is None:
            gaps.append(candidate)
            continue
        flag_type = getattr(rd_module, "DescriptorFlags", None)
        counter_mask = (_safe_int(getattr(flag_type, "AppendBuffer", 2), 2) |
                        _safe_int(getattr(flag_type, "CounterBuffer", 4), 4))
        if (candidate.get("kind") == "read-write-resource" and
                _safe_int(descriptor.get("flags", 0)) & counter_mask and
                not descriptor.get("secondaryResourceId")):
            gaps.append({"resourceId": rid, "stage": candidate.get("stage"),
                "kind": "hidden-uav-counter", "status": "initial-counter-unavailable",
                "blocking": True, "access": candidate.get("access"),
                "outputCounterValue": descriptor.get("bufferStructCount"),
                "outputCounterPhase": "after-action",
                "reason": "Pre-action buffer bytes do not include the hidden append/consume counter. Reconstruct its producer or obtain an exact pre-action descriptor counter before restoring this dispatch."})
        offset = _safe_int(descriptor.get("byteOffset"), -1)
        length = _safe_int(getattr(description, "length", getattr(description, "byteSize", 0)), 0)
        count = _safe_int(descriptor.get("byteSize"), 0)
        if count == 0:
            count = length - offset
        key = (rid, offset, count)
        if offset < 0 or count <= 0 or offset + count > length or count > MAX_BOUND_BUFFER_BYTES:
            gaps.append(dict(candidate, status="unavailable", reason="bound buffer window is invalid or exceeds 64 MiB"))
            continue
        if key not in windows:
            # Budget includes before and after snapshots; the replay stays bounded.
            if total + count * 2 > MAX_BOUND_BUFFER_TOTAL_BYTES:
                gaps.append(dict(candidate, status="unavailable", reason="bound buffer export exceeds 256 MiB"))
                continue
            total += count * 2
            windows[key] = {"resourceId": rid, "byteOffset": offset, "byteSize": count,
                "resourceByteSize": length, "bindings": [], "status": "pending"}
        windows[key]["bindings"].append({key: candidate.get(key) for key in ("stage", "kind", "access", "descriptor", "parentResourceId")})
    if not windows:
        return [], gaps
    replay = getattr(ctrl, "ReplayFrameEventForAnalysis", None)
    restore = getattr(ctrl, "RestoreFrameEventAfterAnalysis", None)
    select = getattr(ctrl, "SetFrameEvent", None)
    current = getattr(ctrl, "GetCurrentEventID", None)
    if not all(callable(method) for method in (replay, restore, select, current)) or event_id <= 0:
        for window in windows.values():
            gaps.append(dict(window, status="unavailable", reason="exact pre-action buffer replay is unavailable"))
        return [], gaps
    if int(current()) != event_id:
        raise RuntimeError("selected event changed before buffer snapshot")
    def healthy():
        getter = getattr(ctrl, "GetFatalErrorStatus", None)
        if callable(getter):
            status = getter()
            code = getattr(status, "code", None)
            succeeded = getattr(getattr(rd_module, "ResultCode", None), "Succeeded", None)
            if ((succeeded is not None and code != succeeded) or
                    (succeeded is None and str(code).rsplit(".", 1)[-1] != "Succeeded")):
                raise RuntimeError("native replay is unavailable during buffer snapshot: %s" % status)
    healthy()
    try:
        replay(event_id - 1)
        healthy()
        for index, (key, window) in enumerate(sorted(windows.items())):
            rid, offset, count = key
            try:
                raw = bytes(ctrl.GetBufferData(descriptions[rid].resourceId, offset, count) or b"")
                if len(raw) != count:
                    raise RuntimeError("RenderDoc returned a truncated buffer view")
                artifact = writer.write_bytes("buffers/%03d.before.bin" % index, raw, "bound-buffer-before-action")
                window.update(status="captured", capturePhase="before-action", replayEventId=event_id - 1,
                              selectedEventId=event_id, raw=dict(artifact, status="captured"))
            except Exception as exc:
                window.update(status="unavailable", reason=str(exc)[:1024])
                gaps.append(dict(window))
    finally:
        try:
            restore()
        finally:
            # A failed analysis restore must not let unrelated replay use its
            # temporary bytes. Force the exact selected EID before returning.
            select(event_id, True)
            if int(current()) != event_id:
                raise RuntimeError("selected EID could not be restored after buffer snapshot")
            healthy()
    for window in windows.values():
        window["replayRestoration"] = {"selectedEventId": event_id, "restored": True,
            "nativeHealthChecked": callable(getattr(ctrl, "GetFatalErrorStatus", None))}
    for index, (key, window) in enumerate(sorted(windows.items())):
        if window["status"] != "captured" or not any(str(binding.get("kind", "")).startswith("read-write-") for binding in window["bindings"]):
            continue
        rid, offset, count = key
        try:
            raw = bytes(ctrl.GetBufferData(descriptions[rid].resourceId, offset, count) or b"")
            if len(raw) != count:
                raise RuntimeError("RenderDoc returned a truncated UAV output view")
            artifact = writer.write_bytes("buffers/%03d.after.bin" % index, raw, "bound-buffer-after-action")
            window["postRaw"] = dict(artifact, status="captured", capturePhase="after-action", replayEventId=event_id)
        except Exception as exc:
            window["postRaw"] = {"status": "unavailable", "reason": str(exc)[:1024]}
            gaps.append({"resourceId": rid, "kind": "read-write-output", "status": "unavailable", "reason": str(exc)[:1024]})
    healthy()
    return list(windows.values()), gaps


def inspect_controller(ctrl, rd_module, request, stages):
    action = _find_action(ctrl.GetRootActions(), request["eventId"])
    if action is None:
        raise ValueError("event_id does not identify an action")
    stages = _action_stages(action, rd_module, stages)
    instance_count = _safe_int(getattr(action, "numInstances", 0), 0)
    if (instance_count > 0 and request["instance"] >= instance_count) or (
            instance_count <= 0 and request["instance"] != 0):
        raise ValueError("instance is outside the selected action")
    pipeline = ctrl.GetPipelineState()
    writer = None
    api, textures, samplers, bound_stages, unexported = _pipeline_snapshot(ctrl, pipeline, stages)
    return {
        "schema": INSPECT_SCHEMA,
        "schemaVersion": 1,
        "requestSha256": request["requestSha256"],
        "selectionSha256": request["selectionSha256"],
        "captureId": request["captureId"],
        "captureSessionId": request["captureSessionId"],
        "eventId": request["eventId"],
        "instance": request["instance"],
        "view": request["view"],
        "api": api,
        "action": _action_record(action),
        "mesh": _action_mesh(ctrl, pipeline, action, request, rd_module, writer, False),
        "textures": textures,
        "samplers": samplers,
        "boundShaderStages": bound_stages,
        "unexportedResources": unexported,
        "evidenceContract": {
            "observed": ["action", "mesh", "textures", "samplers", "boundShaderStages"],
            "inferred": [],
            "generated": [],
        },
    }


def capture_controller(ctrl, rd_module, request, stages):
    writer = _ArtifactWriter(request["artifactDir"])
    try:
        action = _find_action(ctrl.GetRootActions(), request["eventId"])
        if action is None:
            raise ValueError("event_id does not identify an action")
        stages = _action_stages(action, rd_module, stages)
        instance_count = _safe_int(getattr(action, "numInstances", 0), 0)
        if instance_count > 0 and request["instance"] >= instance_count:
            raise ValueError("instance is outside the selected action")
        if instance_count <= 0 and request["instance"] != 0:
            raise ValueError("a non-instanced action only supports instance 0")
        pipeline = ctrl.GetPipelineState()
        # Which readable copies to write beside the DDS evidence. "auto" resolves per
        # texture: TGA (plus PNG) for LDR surfaces, EXR for floating-point ones.
        texture_formats = request.get("textureFormats") or ("auto",)
        texture_formats = tuple(
            str(item).strip().lower() for item in texture_formats
            if str(item or "").strip())[:6]
        api, textures, samplers, bound_stages, unexported = _pipeline_snapshot(
            ctrl, pipeline, stages)
        mesh = _action_mesh(ctrl, pipeline, action, dict(request, api=api), rd_module, writer, True)
        buffers, unexported = _capture_bound_buffers(ctrl, unexported, mesh, request["eventId"], writer, rd_module)
        for texture in textures:
            _save_texture_dds(ctrl, rd_module, texture, writer, extra_formats=texture_formats)
        bundle = {
            "schema": BUNDLE_SCHEMA,
            "schemaVersion": 1,
            "ticketId": request["ticketId"],
            "requestSha256": request["requestSha256"],
            "selectionSha256": request["selectionSha256"],
            "captureId": request["captureId"],
            "captureSessionId": request["captureSessionId"],
            "eventId": request["eventId"],
            "instance": request["instance"],
            "view": request["view"],
            "api": api,
            "action": _action_record(action),
            "mesh": mesh,
            "textures": textures,
            "samplers": samplers,
            "buffers": buffers,
            "boundShaderStages": bound_stages,
            "unexportedResources": unexported,
            "shaders": [],
            "constantBuffers": [],
            "files": writer.files,
            "diagnostics": [],
            "evidenceContract": {
                "observed": [
                    "action", "mesh.source bytes", "mesh.canonical streams",
                    "textures.resource", "textures.bindings", "samplers"],
                "inferred": ["mesh.attributes.semantic", "mesh.attributes.semanticIndex"],
                "generated": ["mesh canonical compact indexing"],
                "coordinateTransformApplied": False,
            },
        }
        return {"request": request, "bundle": bundle, "writer": writer}
    except Exception:
        writer.cleanup()
        raise


def condense_shader_evidence(value):
    if not isinstance(value, dict):
        return {"status": "unavailable", "reason": "shader evidence is not an object"}
    if value.get("error"):
        return {
            "status": "unavailable",
            "errorId": value.get("errorId"),
            "reason": str(value.get("error"))[:2048],
        }
    keys = (
        "shaderId", "pipelineId", "api", "stage", "entryPoint", "entryPoints", "rawArtifact",
        "entryPointSelection", "encoding", "rawSha256", "compiledInterface",
        "inputs", "outputs", "boundTextures", "constantBuffers", "samplers",
        "sectionStatus", "evidenceContract", "diagnostics", "unknowns",
    )
    result = {"status": "captured"}
    for key in keys:
        if key in value:
            result[key] = value[key]
    return _json_safe(result)


def attach_raw_evidence(state, shader_evidence, raw_evidence):
    """Persist exact shader blobs and buffer-backed CBuffer windows.

    ``raw_evidence`` is produced inside the replay transaction by the bridge
    wrapper.  Keeping file publication here makes every emitted path pass the
    same ticket-bound writer and hash checks as mesh and texture artifacts.
    """

    writer = state["writer"]
    shader_raw = raw_evidence.get("shaderRaw", {}) if isinstance(
        raw_evidence, dict) else {}
    cbuffer_raw = raw_evidence.get("cbufferRaw", {}) if isinstance(
        raw_evidence, dict) else {}
    for stage_name, data in shader_raw.items():
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        artifact = writer.write_bytes(
            "shaders/%s.native.bin" % stage_name, bytes(data),
            "shader-native-binary")
        evidence = shader_evidence.setdefault(stage_name, {})
        evidence["rawArtifact"] = dict(artifact, status="captured")
        digest = evidence.get("rawSha256")
        if digest and digest != artifact["sha256"]:
            raise RuntimeError("shader raw bytes do not match reflected rawSha256")
        evidence["rawSha256"] = artifact["sha256"]

    for stage_name, records in cbuffer_raw.items():
        evidence = shader_evidence.setdefault(stage_name, {})
        blocks = evidence.get("constantBuffers", [])
        for raw_record in records or []:
            index = _safe_int(raw_record.get("constantBlockIndex"), -1)
            if index < 0 or index >= len(blocks) or not isinstance(blocks[index], dict):
                continue
            block = blocks[index]
            data = raw_record.get("data")
            if isinstance(data, (bytes, bytearray)):
                artifact = writer.write_bytes(
                    "cbuffers/%s-%03d.raw.bin" % (stage_name, index),
                    bytes(data), "constant-buffer-raw")
                block["raw"] = dict(
                    artifact,
                    status="captured",
                    exact=True,
                    bufferResourceId=raw_record.get("bufferResourceId", 0),
                    byteOffset=raw_record.get("byteOffset", 0),
                    byteSize=len(data),
                )
            else:
                block["raw"] = {
                    "status": "unavailable",
                    "exact": False,
                    "path": None,
                    "sha256": None,
                    "bufferResourceId": raw_record.get("bufferResourceId", 0),
                    "byteOffset": raw_record.get("byteOffset", 0),
                    "byteSize": raw_record.get("byteSize", 0),
                    "reason": str(raw_record.get(
                        "reason", "raw bound byte window is unavailable"))[:2048],
                }


def _constant_buffer_physical_identity(stage_name, block_index, block):
    """Return a stable identity for one physical stage/binding byte window."""

    identity = {
        "stage": str(stage_name),
        "constantBlockIndex": int(block_index),
        "bind": block.get("bind"),
        "registerSpace": block.get("registerSpace", block.get("space")),
        "bufferResourceId": block.get(
            "bufferResourceId", block.get("bufferId", 0)),
        "byteOffset": block.get("byteOffset"),
    }
    return identity, hashlib.sha256(canonical_json_bytes(
        _json_safe(identity))).hexdigest()


def finalize_capture(state, shader_evidence):
    request = state["request"]
    bundle = state["bundle"]
    writer = state["writer"]
    try:
        shaders = []
        constant_buffers = []
        for stage_name in sorted(shader_evidence):
            evidence = condense_shader_evidence(shader_evidence[stage_name])
            blocks = evidence.get("constantBuffers", []) or []
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                identity, physical_id = _constant_buffer_physical_identity(
                    stage_name, block_index, block)
                block["physicalIdentity"] = identity
                block["physicalId"] = physical_id
                requires_data = _safe_int(block.get("byteSize", block.get("size", 0)), 0) > 0
                block["layoutStatus"] = (
                    "captured" if isinstance(block.get("layoutFields"), list) and
                    (block["layoutFields"] or not requires_data) else "unavailable")
                live_status = block.get("variablesStatus", "unavailable")
                if requires_data and not block.get("variables"):
                    live_status = "unavailable"
                block["liveValuesStatus"] = live_status
                raw = block.get("raw")
                if isinstance(raw, dict):
                    raw.setdefault("exact", raw.get("status") == "captured")
            artifact = writer.write_json(
                "shaders/%s.evidence.json" % stage_name, evidence,
                "shader-evidence")
            shader_record = {
                "stage": stage_name,
                "status": evidence.get("status", "unavailable"),
                "evidencePath": artifact["path"],
                "evidenceSha256": artifact["sha256"],
                "shaderId": evidence.get("shaderId"),
                "entryPoint": evidence.get("entryPoint"),
                "encoding": evidence.get("encoding"),
                "rawSha256": evidence.get("rawSha256"),
                "rawArtifact": evidence.get("rawArtifact"),
            }
            if evidence.get("reason"):
                shader_record["reason"] = evidence["reason"]
            shaders.append(shader_record)
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                constant_buffers.append({
                    "stage": stage_name,
                    "constantBlockIndex": block_index,
                    "name": block.get("name"),
                    "bind": block.get("bind"),
                    "registerSpace": block.get("registerSpace", block.get("space")),
                    "bufferResourceId": block.get(
                        "bufferResourceId", block.get("bufferId")),
                    "byteOffset": block.get("byteOffset"),
                    "byteSize": block.get("byteSize", block.get("size")),
                    "valueStatus": block.get(
                        "valueStatus", block.get("variablesStatus")),
                    "layoutStatus": block.get("layoutStatus"),
                    "liveValuesStatus": block.get("liveValuesStatus"),
                    "physicalIdentity": block.get("physicalIdentity"),
                    "physicalId": block.get("physicalId"),
                    "raw": block.get("raw"),
                    "evidencePath": artifact["path"],
                    "evidenceSha256": artifact["sha256"],
                })
        bundle["shaders"] = shaders
        bundle["constantBuffers"] = constant_buffers
        bundle["files"] = list(writer.files)

        incomplete = []
        for resource in bundle.get("unexportedResources", []):
            incomplete.append("resource.%s.%s:%s" % (
                resource.get("stage", "unknown"), resource.get("resourceId", resource.get("kind", "unknown")),
                resource.get("status", "unavailable")))
        for attribute in bundle.get("mesh", {}).get("attributes", []):
            status = attribute.get("canonical", {}).get("status")
            if status != "captured":
                incomplete.append("mesh.attribute.%d.canonical:%s" % (
                    attribute.get("index", -1), status))
        for texture in bundle.get("textures", []):
            status = texture.get("artifact", {}).get("status")
            if status != "captured":
                incomplete.append("texture.%d:%s" % (texture.get("resourceId", 0), status))
        for shader in shaders:
            if shader.get("status") != "captured":
                incomplete.append("shader.%s:%s" % (
                    shader.get("stage", "unknown"), shader.get("status")))
        for block in constant_buffers:
            label = "cbuffer.%s.%d" % (
                block.get("stage", "unknown"), block.get("constantBlockIndex", -1))
            if block.get("layoutStatus") != "captured":
                incomplete.append(label + ".layout:unavailable")
            # Root/push/inline constants need not have a buffer-backed byte
            # artifact. Their captured live values are an independent source.
            if (block.get("liveValuesStatus") not in ("available", "captured") and
                    (block.get("raw") or {}).get("status") != "captured"):
                incomplete.append(label + ".values:unavailable")
        bundle["completion"] = {
            "status": "complete" if not incomplete else "partial",
            "incomplete": incomplete,
        }

        manifest_record = writer.write_json(
            "manifest.json", bundle, "resource-export-manifest")
        response_files = []
        for record in writer.files:
            response = dict(record)
            response["absolutePath"] = _normal_path(os.path.join(
                request["artifactDir"], record["path"].replace("/", os.sep)))
            response_files.append(response)
        return {
            "schema": CAPTURE_SCHEMA,
            "schemaVersion": 1,
            "status": bundle["completion"]["status"],
            "ticketId": request["ticketId"],
            "requestSha256": request["requestSha256"],
            "selectionSha256": request["selectionSha256"],
            "captureId": request["captureId"],
            "captureSessionId": request["captureSessionId"],
            "eventId": request["eventId"],
            "instance": request["instance"],
            "view": request["view"],
            "manifestPath": _normal_path(os.path.join(
                request["artifactDir"], manifest_record["path"].replace("/", os.sep))),
            "manifestSha256": manifest_record["sha256"],
            "files": response_files,
            "bundle": bundle,
        }
    except Exception:
        writer.cleanup()
        raise


def cleanup_capture(state):
    writer = state.get("writer") if isinstance(state, dict) else None
    if writer is not None:
        writer.cleanup()
