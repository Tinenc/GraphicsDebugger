"""Bounded interpretation of explicitly selected cooked material evidence.

The supported codec is the UE5 legacy floating preshader representation exposed
by Bread's UEX reader. Other engines, Preshader2, virtual-texture preambles and
unknown opcodes are not inferred from a material's parameter-array order.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from typing import Any, Mapping, Sequence

SCHEMA = "bread-material-output-evidence/v1"
MAX_VARIANTS = 64
MAX_FIELDS = 512
MAX_PARAMETERS = 512
MAX_CODE_BYTES = 65536
SUPPORTED_OPCODES = {3: "Parameter", 36: "ComponentSwizzle", 37: "AppendVector"}


def value_sequence_evidence(rows: Sequence[Mapping[str, Any]], capture_values: Sequence[Any]) -> dict:
    """Locate complete float32 windows without treating a value hit as a name.

    This diagnostic deliberately retains ordinary values (1, 0, 1, 0.5).
    It compares adjacent *physical* lanes within one candidate layout and one
    captured block, never parameter-array order or concatenated CBuffers.
    A relocated window is a lead for resolving the variant/layout, not an
    offset correction that may be applied to aliases automatically.
    """
    def bits(value):
        try:
            return struct.pack("<f", _float(value))
        except (ValueError, TypeError):
            return None

    captured = [bits(value) for value in capture_values[:16384]]
    declared = {}
    for row in rows[:MAX_FIELDS]:
        offset = row.get("byteOffset")
        components = row.get("components")
        if type(offset) is not int or offset < 0 or offset % 4 or not isinstance(components, list):
            continue
        for component, value in enumerate(components[:4]):
            lane = offset // 4 + component
            if lane in declared:
                return {"status": "overlapping-layout", "matches": [], "identityVerified": False}
            declared[lane] = bits(value.get("value")) if isinstance(value, Mapping) else None
    index = {}
    for lane in range(max(0, len(captured) - 3)):
        window = tuple(captured[lane:lane + 4])
        # Uniform fills do not locate a parameter. Mixed common numbers can.
        if None not in window and len(set(window)) > 1:
            index.setdefault(window, []).append(lane)
    matches, checked, truncated = [], 0, len(capture_values) > len(captured)
    seen = set()
    for lane in sorted(declared):
        window = tuple(declared.get(lane + i) for i in range(4))
        if None in window or len(set(window)) < 2:
            continue
        locations = index.get(window, ())
        for target in locations[:64]:
            checked += 1
            if checked > 8192:
                truncated = True
                break
            # Return one maximal run instead of overlapping four-lane hits.
            first, begin = lane, target
            while first > 0 and begin > 0 and declared.get(first - 1) is not None and declared[first - 1] == captured[begin - 1]:
                first -= 1
                begin -= 1
            key = (first, begin)
            if key in seen:
                continue
            seen.add(key)
            length = 4 + lane - first
            while begin + length < len(captured) and declared.get(first + length) is not None and declared[first + length] == captured[begin + length]:
                length += 1
            matches.append({
                "declaredByteOffset": first * 4, "captureByteOffset": begin * 4,
                "captureRegister": "c%d.%s" % (begin // 4, "xyzw"[begin % 4]),
                "byteDelta": (begin - first) * 4, "laneCount": length,
                "values": [struct.unpack("<f", captured[begin + i])[0] for i in range(min(length, 8))],
                "seedOccurrenceCount": len(locations), "float32Exact": True,
            })
        if len(locations) > 64:
            truncated = True
        if checked > 8192:
            break
    matches.sort(key=lambda item: (-item["laneCount"], item["declaredByteOffset"], item["captureByteOffset"]))
    return {"status": "compared", "matches": matches[:16],
            "truncated": truncated or len(matches) > 16,
            "capturedLaneCount": len(capture_values), "comparedCaptureLanes": len(captured),
            "identityVerified": False, "autoApplyEligible": False,
            "scope": "candidate-layout-versus-one-captured-block",
            "note": "Value windows retain common numbers and exact lane positions; repeated or shifted windows require independent material/variant/layout evidence before naming."}


def _integer(value: Any, label: str, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("%s is outside the supported integer range" % label)
    return value


def _float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("parameter is not a finite float")
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as error:
        raise ValueError("parameter exceeds float32 range") from error


def _parameter_key(info: Any) -> tuple[str, str, int]:
    if not isinstance(info, Mapping) or not isinstance(info.get("Name"), str) or not info["Name"] or len(info["Name"]) > 1024:
        raise ValueError("numeric parameter identity is missing")
    association = info.get("Association")
    # UEX material previews spell the same UE enum as
    # ``EMaterialParameterAssociation::GlobalParameter`` while cooked ShaderMap
    # numeric parameters use the short ``GlobalParameter`` token.  They are one
    # physical parameter identity, so canonicalise only the three explicitly
    # supported enum values instead of rejecting a valid parent-chain override.
    if isinstance(association, str) and association.startswith(
        "EMaterialParameterAssociation::"
    ):
        association = association.rsplit("::", 1)[-1]
    if association not in ("GlobalParameter", "LayerParameter", "BlendParameter"):
        raise ValueError("unsupported parameter association")
    return info["Name"], association, _integer(info.get("Index"), "parameter Index", 4096, -1)


def _reference(value: str, base_asset: str) -> str:
    value = value.replace("\\", "/").rsplit(".", 1)[0]
    if value.startswith("/Game/") and "/Content/" in base_asset:
        value = base_asset.split("/Content/", 1)[0] + "/Content/" + value[len("/Game/"):]
    if value.startswith("/Engine/"):
        value = "Engine/Content/" + value[len("/Engine/"):]
    return value.casefold()


def _embedded_override_document(document: Any, asset: str) -> Any:
    """Select one material preview embedded in a ShaderMap evidence receipt."""

    if not isinstance(document, Mapping) or document.get("schema") != "bread-material-shader-evidence/v1":
        return document
    stem = asset.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
    matches = []
    for item in document.get("materials") or []:
        if not isinstance(item, Mapping):
            continue
        preview = item.get("materialPreview")
        if (
            isinstance(preview, Mapping)
            and str(preview.get("Name") or item.get("objectName") or "").casefold() == stem
        ):
            matches.append(preview)
    if len(matches) != 1:
        raise ValueError("ShaderMap evidence has no unique embedded material preview")
    return matches[0]


def material_overrides(chain: Sequence[Mapping[str, Any]], base_asset: str) -> dict:
    """Leaf first, explicit file chain; validate exact parent references."""
    if len(chain) > 8:
        raise ValueError("material override chain exceeds 8 documents")
    overrides: dict = {}
    for index, entry in enumerate(chain):
        asset = str(entry.get("assetPath") or "")
        document = _embedded_override_document(entry.get("document"), asset)
        if isinstance(document, list):
            if len(document) != 1:
                raise ValueError("select one material export per override document")
            document = document[0]
        if not asset or not isinstance(document, Mapping):
            raise ValueError("material chain needs an assetPath and exact material document")
        if document.get("Type") not in ("MaterialInstanceConstant", "MaterialInstance"):
            raise ValueError("override document is not a supported Unreal material instance")
        if str(document.get("Name") or "").casefold() != asset.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold():
            raise ValueError("override material name differs from its selected asset path")
        props = document.get("Properties")
        if not isinstance(props, Mapping):
            raise ValueError("override document is not supported Unreal material properties")
        parent = props.get("Parent")
        next_asset = str(chain[index + 1].get("assetPath") or "") if index + 1 < len(chain) else base_asset
        if not isinstance(parent, Mapping) or _reference(str(parent.get("ObjectPath") or ""), base_asset) != _reference(next_asset, base_asset):
            raise ValueError("override documents do not follow the exact parent chain to the ShaderMap material")
        seen: set = set()
        for field, kind in (("ScalarParameterValues", "Scalar"), ("VectorParameterValues", "Vector")):
            values = props.get(field) or []
            if not isinstance(values, list) or len(values) > MAX_PARAMETERS:
                raise ValueError("material override array is unsupported or exceeds its bound")
            for item in values:
                if not isinstance(item, Mapping):
                    raise ValueError("invalid material override entry")
                key = _parameter_key(item.get("ParameterInfo"))
                if key in seen:
                    raise ValueError("material contains competing overrides for one parameter identity")
                seen.add(key)
                overrides.setdefault(key, (kind, item.get("ParameterValue"), asset))
    return overrides


def _program(program: bytes, parameters: list, overrides: dict) -> list[dict]:
    cursor, stack = 0, []
    while cursor < len(program):
        opcode = program[cursor]
        cursor += 1
        if opcode == 3:
            if cursor + 2 > len(program):
                raise ValueError("truncated Parameter")
            index = struct.unpack_from("<H", program, cursor)[0]
            cursor += 2
            if index >= len(parameters):
                raise ValueError("Parameter index is outside numericParameters")
            parameter = parameters[index]
            key = _parameter_key(parameter.get("ParameterInfo"))
            kind = parameter.get("ParameterType")
            if kind not in ("Scalar", "Vector"):
                raise ValueError("unsupported non-floating parameter")
            override_kind, value, origin = overrides.get(key, (kind, parameter.get("Value"), "compiled-default"))
            if override_kind != kind:
                raise ValueError("override type conflicts with compiled parameter type")
            if kind == "Vector":
                if not isinstance(value, Mapping) or not all(c in value for c in "RGBA"):
                    raise ValueError("compiled vector/default has incomplete components")
                values = [_float(value[c]) for c in "RGBA"]
            else:
                values = [_float(value)]
            stack.append([{"parameterInfo": {"Name": key[0], "Association": key[1], "Index": key[2]}, "parameterType": kind,
                           "parameterComponent": component, "value": value, "valueSource": origin}
                          for component, value in enumerate(values)])
        elif opcode == 36:
            if cursor + 5 > len(program) or not stack:
                raise ValueError("truncated/underflow ComponentSwizzle")
            count, *indices = program[cursor:cursor + 5]
            cursor += 5
            if count not in (1, 2, 3, 4):
                raise ValueError("unsupported swizzle width")
            previous = stack.pop()
            if len(previous) == 1:
                indices = [0 if i != 255 else 255 for i in indices]
            if any(i != 255 and i >= len(previous) for i in indices[:count]):
                raise ValueError("swizzle selects an unavailable component")
            stack.append([previous[i] if i != 255 else {"constant": 0.0, "value": 0.0}
                          for i in indices[:count]])
        elif opcode == 37:
            if len(stack) < 2:
                raise ValueError("AppendVector stack underflow")
            right, left = stack.pop(), stack.pop()
            if len(left) + len(right) > 4:
                raise ValueError("AppendVector exceeds floating vector width")
            stack.append(left + right)
        else:
            raise ValueError("unsupported opcode 0x%02x; no semantic name inferred" % opcode)
        if len(stack) > 64:
            raise ValueError("preshader stack exceeds 64 values")
    if len(stack) != 1:
        raise ValueError("preshader did not produce one output")
    return stack[0]


def _variant(variant: Mapping[str, Any], overrides: dict) -> tuple[list, list]:
    layout = variant.get("uniformLayout") or {}
    size = _integer(layout.get("preshaderByteSize"), "preshaderByteSize", 65536)
    bindings = variant.get("resourceBindings") or {}
    if bindings.get("virtualTextureStacks") or any((bindings.get("textures") or [])[1:]):
        raise ValueError("virtual/other texture uniform preamble offsets are not supported")
    data = variant.get("preshaderCode") or {}
    meta = data.get("metadata") or {}
    if meta.get("bPreshader2") is not False or meta.get("bPreFixup") is not False:
        raise ValueError("only the explicit UE5 legacy non-Preshader2/non-pre-fixup encoding is supported")
    code = base64.b64decode(data.get("base64") or "", validate=True)
    if len(code) > MAX_CODE_BYTES or len(code) != data.get("byteSize") or hashlib.sha256(code).hexdigest() != data.get("sha256"):
        raise ValueError("preshader code size/SHA256 differs from its receipt")
    parameters = variant.get("numericParameters")
    headers, fields = variant.get("preshaderHeaders"), variant.get("preshaderFields")
    for name, value, maximum in (("numericParameters", parameters, MAX_PARAMETERS), ("preshaderHeaders", headers, MAX_FIELDS), ("preshaderFields", fields, MAX_FIELDS)):
        if not isinstance(value, list) or len(value) > maximum or not all(isinstance(v, Mapping) for v in value):
            raise ValueError("%s is not a supported bounded array" % name)
    rows, unknown, occupied = [], [], set()
    for header in headers:
        field_validated = False
        try:
            first = _integer(header.get("FieldIndex"), "FieldIndex", len(fields))
            count = _integer(header.get("NumFields"), "NumFields", MAX_FIELDS, 1)
            start = _integer(header.get("OpcodeOffset"), "OpcodeOffset", len(code))
            length = _integer(header.get("OpcodeSize"), "OpcodeSize", len(code))
            if first + count > len(fields) or start + length > len(code):
                raise ValueError("preshader header exceeds serialized arrays")
            if count != 1:
                raise ValueError("multi-field output mapping is not supported")
            field = fields[first]
            kind = field.get("Type")
            if kind not in ("Float1", "Float2", "Float3", "Float4") or field.get("ComponentIndex") != 0:
                raise ValueError("non-floating or nonzero output ComponentIndex is not supported")
            width = int(kind[-1])
            offset = _integer(field.get("BufferOffset"), "BufferOffset", size // 4) * 4
            if offset + width * 4 > size:
                raise ValueError("output field exceeds the numeric uniform region")
            claimed = set(range(offset, offset + width * 4, 4))
            if claimed & occupied:
                # An overlapping field invalidates prior names too; do not keep
                # whichever header happened to appear first.
                return [], [{"reason": "overlapping preshader output fields", "namesInferred": False}]
            occupied.update(claimed)
            field_validated = True
            program = code[start:start + length]
            value = _program(program, parameters, overrides)
            # UE5 legacy float fast path copies min(source,destination) and
            # zero-fills a wider destination (MaterialUniformExpressions.cpp).
            value = value[:width] + [{"constant": 0.0, "value": 0.0}] * max(0, width - len(value))
            rows.append({"byteOffset": offset, "byteSize": width * 4, "outputType": kind,
                         "components": value, "programHex": program.hex(),
                         "serializedField": {k: field[k] for k in ("Type", "ComponentIndex", "BufferOffset")},
                         "semanticWidthEvidence": "decoded-compiled-output-field", "rawBytecodeIdentityVerified": False})
        except (ValueError, TypeError, KeyError, struct.error) as error:
            diagnostic = {"header": {k: header.get(k) if type(header.get(k)) is int else None
                                       for k in ("FieldIndex", "NumFields", "OpcodeOffset", "OpcodeSize")},
                          "reason": str(error), "namesInferred": False}
            if not field_validated:
                # Unsupported physical fields may write over an otherwise
                # decoded location. Only unknown *programs* with validated,
                # disjoint destinations permit partial semantic recovery.
                return [], [diagnostic]
            unknown.append(diagnostic)
    return rows, unknown


def decode_outputs(document: Mapping[str, Any], chain: Sequence[Mapping[str, Any]] = (), *,
                   shader_platform: str = "", capture_values: Sequence[Any] | None = None) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("material metadata must be a JSON object")
    if not isinstance(shader_platform, str) or not shader_platform or len(shader_platform) > 128:
        raise ValueError("select the exact shader_platform from the ShaderMap receipt")
    if document.get("schema") != "bread-material-shader-evidence/v1" or (document.get("reader") or {}).get("id") != "uex":
        raise ValueError("unsupported material metadata schema/reader; this codec does not decode Unity or arbitrary UE versions")
    if (document.get("readShaderMaps") or {}).get("completed") is not True or (document.get("coverage") or {}).get("complete") is not True:
        raise ValueError("material ShaderMap read is incomplete")
    materials = document.get("materials")
    if not isinstance(materials, list) or len(materials) != 1 or not isinstance(materials[0], Mapping):
        raise ValueError("select exactly one material ShaderMap document")
    variants = materials[0].get("variants")
    if not isinstance(variants, list) or len(variants) > MAX_VARIANTS or not all(isinstance(v, Mapping) for v in variants):
        raise ValueError("unsupported variant array")
    overrides = material_overrides(chain, str((document.get("asset") or {}).get("path") or ""))
    results, candidates = [], []
    for index, variant in enumerate(variants):
        if shader_platform and variant.get("shaderPlatform") != shader_platform:
            continue
        summary = {"variantIndex": index, **{k: str(variant.get(k) or "")[:256]
                   for k in ("shaderPlatform", "qualityLevel", "featureLevel", "cookedShaderMapHash", "resourceHash")}}
        try:
            rows, unknown = _variant(variant, overrides)
            if capture_values is not None:
                for row in rows:
                    first = row["byteOffset"] // 4
                    actual = list(capture_values[first:first + len(row["components"])])
                    expected = [c["value"] for c in row["components"]]
                    try:
                        exact = len(actual) == len(expected) and all(struct.pack("<f", _float(a)) == struct.pack("<f", b) for a, b in zip(actual, expected))
                    except ValueError:
                        exact = False
                    # Strict JSON: unavailable/non-finite capture lanes retain
                    # a failed comparison, never emit NaN/Infinity tokens.
                    actual = [v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None for v in actual]
                    row["captureComparison"] = {"float32Exact": exact, "actual": actual, "expected": expected}
            matched = sum(r.get("captureComparison", {}).get("float32Exact", False) for r in rows)
            consistent = bool(rows) and (capture_values is None or matched == len(rows))
            summary.update(status="decoded" if not unknown else "partial", decodedOutputs=len(rows), unknownProgramCount=len(unknown), unknownPrograms=unknown[:8],
                           matchedOutputs=matched if capture_values is not None else None,
                           valueConsistent=consistent if capture_values is not None else None)
            if capture_values is not None:
                summary["valueSequenceEvidence"] = value_sequence_evidence(rows, capture_values)
            if consistent:
                candidates.append((index, rows))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            summary.update(status="unsupported", reason=str(error), decodedOutputs=0, valueConsistent=False)
        results.append(summary)
    common = []
    if candidates:
        maps = [{r["byteOffset"]: r for r in rows} for unused, rows in candidates]
        for offset in sorted(set.intersection(*(set(m) for m in maps))):
            rows = [m[offset] for m in maps]
            identities = [json.dumps({"type": r["outputType"], "components": r["components"]}, sort_keys=True) for r in rows]
            if len(set(identities)) == 1:
                common.append(rows[0])
    return {"schema": SCHEMA, "codec": "ue5-legacy-float-preshader-v1", "supportedOpcodes": SUPPORTED_OPCODES,
            "material": str(materials[0].get("objectName") or "")[:1024], "variants": results, "outputs": common,
            "overrideChainProvided": bool(chain), "captureComparisonAvailable": capture_values is not None,
            "shaderPlatform": shader_platform, "selectedVariantCount": len(results),
            "candidateVariantIndexes": [i for i, unused in candidates],
            "valueConsistentVariantIndexes": [i for i, unused in candidates] if capture_values is not None else [],
            "outputCount": len(common),
            "rawBytecodeIdentityVerified": False, "samePlatformMaterialRecompiled": False, "applied": False,
            "limitations": ["Output names are inferred from explicit asset evidence and decoded fields; capture Shader archive identity is not proven.",
                            "Only output semantics shared by all selected value-consistent variants are returned; unknown programs are never named.",
                            "No Unity, Preshader2, pre-fixup, virtual-texture preamble or arbitrary UE-version support is implied.",
                            "Match output byte offsets to actual capture-bound workspace declarations before proposing names; material array indices are not offsets."]}
