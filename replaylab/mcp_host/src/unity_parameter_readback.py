"""Compare authoritative captured parameters with typed Unity readback.

Pure data validation: no file, Editor, tolerance, color-space or matrix-space
conversion. System mappings need a separate Unity-interface semantic review.
Only padding explicitly added by Unity's Vector4 storage is synthesized here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import struct
from collections.abc import Mapping
from typing import Any


_SCALARS = {"float": "f", "half": "e", "double": "d", "int": "i", "uint": "I",
            "bool": "I", "short": "h", "ushort": "H", "byte": "b", "ubyte": "B",
            "long": "q", "ulong": "Q"}
_INTEGER = set(_SCALARS) - {"float", "half", "double"}
_MAX_VALUES = 1_048_576
_MAX_BYTES = 64 * 1024 * 1024


class _Invalid(ValueError):
    pass


def _identity(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _authoritative(item: Mapping) -> bool:
    if item.get("kind") == "system" or item.get("classification") == "system":
        return False
    source = _identity(item.get("sourceIdentity"))
    return bool((source.get("fieldId") or source.get("physicalId")) and
                item.get("capturedValue", item.get("value")) is not None
                or source.get("resourceId") is not None and source.get("sha256") and
                item.get("bufferBase64", item.get("dataBase64")) is not None)


def _positive(item: Mapping, key: str, default: int = 1) -> int:
    value = item.get(key, default)
    if type(value) is not int or value < 1 or value > _MAX_VALUES:
        raise _Invalid("invalid " + key)
    return value


def _flat(value: Any) -> list:
    if not isinstance(value, list):
        return [value]
    result = []
    for item in value:
        result.extend(item if isinstance(item, list) else [item])
        if len(result) > _MAX_VALUES:
            raise _Invalid("parameter exceeds the value limit")
    if any(isinstance(item, (list, Mapping)) for item in result):
        raise _Invalid("unexpected nested dimensions")
    return result


def _float_bytes(values: list) -> bytes:
    try:
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise _Invalid("Float32 values must be finite numbers")
        # IEEE bits deliberately distinguish +0/-0 and adjacent Float32 values.
        return struct.pack("<%df" % len(values), *values)
    except (OverflowError, struct.error, TypeError) as error:
        raise _Invalid("value is outside Float32 storage") from error


def _integer_bits(value: Any, scalar: str, *, observed: bool = False) -> int:
    if not observed and scalar == "bool":
        if type(value) is not bool:
            raise _Invalid("captured bool must be boolean")
        value = int(value)
    if type(value) is not int:
        raise _Invalid("integer readback must retain integer storage")
    limits = {"int": (-2**31, 2**31 - 1), "uint": (0, 2**32 - 1), "bool": (0, 1),
              "short": (-2**15, 2**15 - 1), "ushort": (0, 2**16 - 1),
              "byte": (-128, 127), "ubyte": (0, 255)}
    lower, upper = (-2**31, 2**32 - 1) if observed else limits.get(scalar, (1, 0))
    if value < lower or value > upper:
        raise _Invalid("integer is outside its captured storage width")
    return value & 0xFFFFFFFF


def _expected(item: Mapping, scope: str) -> dict:
    source = _identity(item.get("sourceIdentity"))
    rows, columns, elements = (_positive(item, key) for key in ("rows", "columns", "elements"))
    if rows * columns * elements > _MAX_VALUES:
        raise _Invalid("parameter dimensions exceed the value limit")
    scalar = item.get("scalarType")
    if scalar is not None and not isinstance(scalar, str):
        raise _Invalid("captured scalar type must be a name")
    captured_type = re.fullmatch(r"([a-z]+)([1-4])?(?:x([1-4]))?(?:\[([1-9][0-9]*)\])?", str(source.get("type", "")))
    if captured_type:
        source_rows, source_columns = ((int(captured_type[2]), int(captured_type[3])) if captured_type[3]
                                       else (1, int(captured_type[2] or 1)))
        same_dimensions = ((rows, columns) == (source_rows, source_columns) if captured_type[3]
                           else (rows == 1 or columns == 1) and rows * columns == source_columns)
        if scalar != captured_type[1] or not same_dimensions or elements != int(captured_type[4] or 1):
            raise _Invalid("target dimensions/type disagree with the captured field")
    kind = item.get("type", "float")
    if not isinstance(kind, str):
        raise _Invalid("target readback type must be a name")
    if item.get("kind") == "array":
        kind = "matrix-array" if rows > 1 else "vector-array" if columns > 1 else "float-array"
    elif kind == "array":
        kind = "matrix-array" if item.get("elementType") == "matrix" else "vector-array" if _positive(item, "components", columns) > 1 else "float-array"
    kind = {"uint": "int", "bool": "int"}.get(kind, kind)
    if kind == "buffer":
        if scope != "runtime":
            raise _Invalid("GPU buffers require runtime bindings")
        encoded = item.get("bufferBase64", item.get("dataBase64"))
        try:
            if not isinstance(encoded, str) or len(encoded) > (_MAX_BYTES + 2) // 3 * 4:
                raise _Invalid("buffer payload is missing or too large")
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise _Invalid("invalid captured buffer payload") from error
        stride = _positive(item, "stride", 4)
        if not payload or len(payload) > _MAX_BYTES or stride < 4 or stride > 2048 or stride % 4 or len(payload) % stride:
            raise _Invalid("buffer stride/length is invalid")
        digest = hashlib.sha256(payload).hexdigest()
        for expected_sha in (item.get("valueSha256"), source.get("sha256")):
            if expected_sha is not None and expected_sha != digest:
                raise _Invalid("captured buffer hash disagrees with its bytes")
        if source.get("byteSize", len(payload)) != len(payload):
            raise _Invalid("captured buffer size disagrees with its bytes")
        if source.get("fieldId") or source.get("physicalId"):
            values = _flat(item.get("capturedValue", item.get("value")))
            if scalar not in _SCALARS or len(values) != rows * columns * elements:
                raise _Invalid("raw CBuffer shape/type is unavailable")
            try:
                if scalar == "bool" and any(type(value) is not bool for value in values):
                    raise _Invalid("captured bool must be boolean")
                packed = struct.pack("<%d%s" % (len(values), _SCALARS[scalar]), *values)
            except (struct.error, OverflowError, TypeError) as error:
                raise _Invalid("captured raw CBuffer values are invalid") from error
            if packed != payload or stride != rows * columns * struct.calcsize(_SCALARS[scalar]):
                raise _Invalid("raw buffer does not preserve captured typed lanes")
        expected_value = {"sha256": digest, "byteLength": len(payload)}
        if item.get("bufferType") in {"append", "counter"}:
            counter = item.get("initialCounter")
            if type(counter) is not int or not 0 <= counter <= len(payload) // stride:
                raise _Invalid("append/counter buffer requires a bounded explicit initial counter")
            provenance = item.get("counterProvenance")
            if (not isinstance(provenance, Mapping) or provenance.get("kind") not in {"captured", "inferred", "unity-native"}
                    or not isinstance(provenance.get("reason"), str) or not provenance["reason"].strip()):
                raise _Invalid("counter initialization must retain its captured or inferred provenance")
            expected_value["counter"] = counter
        return {"type": kind, "scalar": scalar, "value": expected_value}
    if scalar not in _SCALARS:
        raise _Invalid("captured scalar type is missing")
    values = _flat(item.get("capturedValue", item.get("value")))
    if len(values) != rows * columns * elements:
        raise _Invalid("captured value dimensions disagree")
    if kind == "auto":
        kind = "float" if len(values) == 1 else "vector"
    if kind == "int":
        if scalar not in _INTEGER or len(values) != 1:
            raise _Invalid("integer target requires one captured integer/bool lane")
        return {"type": kind, "scalar": scalar, "value": _integer_bits(values[0], scalar)}
    if scalar not in ("float", "half"):
        raise _Invalid("this captured type requires a typed raw buffer, not Float32 storage")
    if kind in ("float", "vector", "color"):
        if elements != 1 or (rows > 1 and columns > 1):
            raise _Invalid("a material scalar/vector cannot store a matrix or array")
        if kind == "float" and len(values) != 1 or kind in ("vector", "color") and not 1 <= len(values) <= 4:
            raise _Invalid("scalar/vector dimensions disagree")
        if kind == "color" and len(values) != 4:
            raise _Invalid("captured Color requires four explicit lanes")
        if kind in ("vector", "color"):
            values += [0.0] * (4 - len(values))
        shaped = values[0] if kind == "float" else values
    elif kind in ("matrix", "matrix-array"):
        if scope != "runtime" or (rows, columns) != (4, 4) or kind == "matrix" and elements != 1:
            raise _Invalid("Unity matrix storage requires explicit row-major 4x4 runtime bindings")
        if item.get("valueOrder", "row-major-logical") != "row-major-logical":
            raise _Invalid("matrix values must use logical row-major order")
        shaped = values if kind == "matrix" else [values[i:i + 16] for i in range(0, len(values), 16)]
    elif kind == "float-array":
        if scope != "runtime" or (rows, columns) != (1, 1):
            raise _Invalid("float array dimensions disagree")
        shaped = values
    elif kind == "vector-array":
        components = _positive(item, "components", columns)
        if scope != "runtime" or rows != 1 or not 1 <= columns <= 4 or components != columns:
            raise _Invalid("vector array dimensions disagree")
        shaped = [values[i:i + columns] + [0.0] * (4 - columns) for i in range(0, len(values), columns)]
    else:
        raise _Invalid("unsupported target readback type: " + str(kind))
    _float_bytes(_flat(shaped))
    return {"type": kind, "scalar": scalar, "value": shaped}


def _same_shape(expected: Any, observed: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(observed, list) and len(expected) == len(observed) and all(
            _same_shape(left, right) for left, right in zip(expected, observed))
    return not isinstance(observed, (list, Mapping))


def verify_parameter_readback(request: Mapping, observation: Mapping) -> list[dict]:
    """Return blocking gaps for missing, mistyped or changed captured parameters.

    Material readback needs ``material.propertyTypes`` using ShaderPropertyType
    names. Runtime readback must carry the original sourceIdentity and scalarType.
    Additional parameters without captured authority are deliberately not judged.
    """
    gaps = []
    material = observation.get("material")
    material = material if isinstance(material, Mapping) else {}
    properties = material.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    property_types = material.get("propertyTypes")
    property_types = property_types if isinstance(property_types, Mapping) else {}
    bindings = observation.get("runtimeBindings")
    effect = observation.get("effect") or {}
    effect_upload = (request.get("executionKind") == "effect"
        and observation.get("executionKind") == "effect"
        and isinstance(effect, Mapping) and effect.get("kind") == "executable-effect"
        and effect.get("uploadsVerified") is True and not effect.get("error"))
    readbacks = bindings.get("readback") if isinstance(bindings, Mapping) else []
    readbacks = readbacks if isinstance(readbacks, list) else []
    runtime = {}
    for row in readbacks:
        if isinstance(row, Mapping) and isinstance(row.get("name"), str):
            runtime.setdefault(row.get("name"), []).append(row)
    seen = set()
    for scope, key in (("material", "properties"), ("runtime", "runtimeBindings")):
        for item in request.get(key, []):
            if not isinstance(item, Mapping) or not _authoritative(item):
                continue
            name = item.get("name", item.get("target"))
            source = _identity(item.get("sourceIdentity"))

            def gap(code, detail):
                gaps.append({"code": "unity-parameter-" + code, "blocking": True, "scope": scope,
                             "name": name, "fieldId": source.get("fieldId"), "detail": detail})

            try:
                if not isinstance(name, str) or not name:
                    raise _Invalid("captured binding needs a target name")
                if name in seen:
                    raise _Invalid("duplicate captured target binding")
                seen.add(name)
                expected = _expected(item, scope)
            except _Invalid as error:
                gap("request-invalid", str(error))
                continue
            if scope == "material":
                if name not in properties:
                    gap("missing", "captured material property is absent")
                    continue
                allowed = {"float": {"Float", "Range"}, "int": {"Int"}, "vector": {"Vector"}, "color": {"Color"}}
                observed_type = property_types.get(name)
                if not isinstance(observed_type, str) or observed_type not in allowed.get(expected["type"], set()):
                    gap("type-mismatch", "material Shader property type is missing or incompatible")
                    continue
                if name in runtime:
                    gap("shadowed", "a renderer property block overrides the authoritative material property")
                    continue
                value = properties[name]
            else:
                matches = runtime.get(name, [])
                if len(matches) != 1:
                    gap("missing" if not matches else "ambiguous", "expected exactly one runtime readback")
                    continue
                row = matches[0]
                if row.get("type") != expected["type"] or row.get("scalarType") != expected["scalar"]:
                    gap("type-mismatch", "runtime target or captured scalar type changed")
                    continue
                # Unity has no ComputeShader.GetFloat/GetMatrix API. The Editor
                # records successful typed Set* uploads, separately from GPU
                # buffer readback. Completion also requires measured execution
                # of this effect and a current image/algorithm review.
                expected_scope = "gpu-buffer-data" if expected["type"] == "buffer" else (
                    "compute-upload" if effect_upload else "renderer-property-block")
                if row.get("scope") != expected_scope:
                    gap("type-mismatch", "runtime readback did not observe the expected storage")
                    continue
                if expected_scope == "compute-upload" and row.get("uploadVerified") is not True:
                    gap("upload-unverified", "the typed effect input was not successfully uploaded")
                    continue
                if _identity(row.get("sourceIdentity")) != source:
                    gap("source-mismatch", "runtime readback belongs to a different captured source")
                    continue
                value = row.get("value")
            try:
                if expected["type"] == "buffer":
                    matches = (isinstance(value, Mapping) and value.get("hashCoversAllBytes") is True
                               and type(value.get("byteLength")) is int and value.get("byteLength") == expected["value"]["byteLength"]
                               and value.get("sha256") == expected["value"]["sha256"])
                    if "counter" in expected["value"]:
                        matches = (matches and type(value.get("counter")) is int
                                   and value["counter"] == expected["value"]["counter"])
                elif expected["type"] == "int":
                    matches = _integer_bits(value, expected["scalar"], observed=True) == expected["value"]
                else:
                    matches = (_same_shape(expected["value"], value) and
                               _float_bytes(_flat(expected["value"])) == _float_bytes(_flat(value)))
                if not matches:
                    gap("value-mismatch", "readback shape or complete storage bits differ from the captured binding")
            except _Invalid as error:
                gap("value-mismatch", str(error))
    return gaps
