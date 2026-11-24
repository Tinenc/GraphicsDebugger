"""Turn a physical ABI comparison into named, actionable findings.

``compare_shader_abi`` reports raw path differences (``inputSignature[2].regIndex``)
which shift as soon as one row is missing.  This module re-keys both interfaces
by their stable identities (semantic name, ``space:bind``) and describes each
mismatch in words a repairing Agent or a person can act on: what the capture
declares, what the candidate declares, and the HLSL change that closes the gap.
It is derived evidence only — never a substitute for the comparison verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

EXPLANATION_SCHEMA = "renderdoc-ultra-abi-difference-explanation/v1"
MAX_FINDINGS = 96

_HLSL_TYPE = {"float": "float", "uint": "uint", "int": "int", "half": "half",
              "double": "double", "bool": "bool"}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _type_text(comp_type: Any, comp_count: Any) -> str:
    base = _HLSL_TYPE.get(str(comp_type or "").lower(), str(comp_type or "?"))
    try:
        count = int(comp_count or 1)
    except (TypeError, ValueError):
        count = 1
    return base + (str(count) if count > 1 else "")


def _semantic(row: Mapping[str, Any]) -> str:
    name = str(row.get("semanticIdxName") or row.get("semanticName") or "")
    if not name:
        name = "%s%s" % (row.get("semanticName", "?"), row.get("semanticIndex", ""))
    return name


def _row_text(row: Mapping[str, Any]) -> str:
    return "%s %s at register %s (mask 0x%X%s)" % (
        _type_text(row.get("compType"), row.get("compCount")),
        _semantic(row),
        row.get("regIndex"),
        int(row.get("regChannelMask") or 0),
        ", system value %s" % row["systemValue"]
        if row.get("systemValue") not in (None, "", "undefined") else "",
    )


def _finding(area: str, kind: str, identity: str, captured: Any, candidate: Any,
             message: str, hint: str) -> dict[str, Any]:
    return {
        "area": area,
        "kind": kind,
        "identity": identity,
        "captured": captured,
        "candidate": candidate,
        "message": message,
        "hint": hint,
    }


def _explain_signature(label: str, baseline: Sequence[Mapping[str, Any]],
                       candidate: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    struct_name = "SPIRV_Cross_Input" if label == "input" else "SPIRV_Cross_Output"
    by_key_base = {(_semantic(r).lower(), int(r.get("stream") or 0)): r for r in baseline}
    by_key_cand = {(_semantic(r).lower(), int(r.get("stream") or 0)): r for r in candidate}
    for key, row in by_key_base.items():
        other = by_key_cand.get(key)
        semantic = _semantic(row)
        if other is None:
            findings.append(_finding(
                label + "Signature", "missing-row", semantic, _row_text(row), None,
                "The capture declares %s %s, but the candidate does not declare it."
                % (label, _row_text(row)),
                "Add `%s <name> : %s;` to `%s` (even if the code never reads it) so the "
                "register layout matches; place it so the declaration order reproduces "
                "register %s." % (_type_text(row.get("compType"), row.get("compCount")),
                                  semantic, struct_name, row.get("regIndex")),
            ))
            continue
        if int(row.get("regIndex") or -1) != int(other.get("regIndex") or -1) or \
                int(row.get("regChannelMask") or 0) != int(other.get("regChannelMask") or 0):
            findings.append(_finding(
                label + "Signature", "moved-row", semantic, _row_text(row), _row_text(other),
                "%s %s is captured at register %s (mask 0x%X) but the candidate places it "
                "at register %s (mask 0x%X)." % (
                    label.capitalize(), semantic, row.get("regIndex"),
                    int(row.get("regChannelMask") or 0), other.get("regIndex"),
                    int(other.get("regChannelMask") or 0)),
                "Reorder the fields of `%s` so that register numbers ascend in the captured "
                "order; the compiler assigns registers by declaration order and packing, so "
                "the missing/extra rows above usually cause this shift." % struct_name,
            ))
        if str(row.get("compType")) != str(other.get("compType")) or \
                int(row.get("compCount") or 1) != int(other.get("compCount") or 1):
            findings.append(_finding(
                label + "Signature", "type-mismatch", semantic,
                _type_text(row.get("compType"), row.get("compCount")),
                _type_text(other.get("compType"), other.get("compCount")),
                "%s %s is captured as %s but the candidate declares %s." % (
                    label.capitalize(), semantic,
                    _type_text(row.get("compType"), row.get("compCount")),
                    _type_text(other.get("compType"), other.get("compCount"))),
                "Declare the field as `%s` (a `bool` system value reflects as `uint`)."
                % _type_text(row.get("compType"), row.get("compCount")),
            ))
        if str(row.get("systemValue")) != str(other.get("systemValue")):
            findings.append(_finding(
                label + "Signature", "system-value-mismatch", semantic,
                row.get("systemValue"), other.get("systemValue"),
                "%s %s is captured with system value %s but the candidate reports %s."
                % (label.capitalize(), semantic, row.get("systemValue"), other.get("systemValue")),
                "Use the captured semantic name exactly (system-value semantics are not "
                "interchangeable with TEXCOORDn).",
            ))
    for key, other in by_key_cand.items():
        if key in by_key_base:
            continue
        semantic = _semantic(other)
        findings.append(_finding(
            label + "Signature", "extra-row", semantic, None, _row_text(other),
            "The candidate declares %s %s, which the capture does not have."
            % (label, _row_text(other)),
            "Remove that field from `%s`, or rename it to the captured semantic it stands "
            "for (compare register numbers and types with the missing rows above)."
            % struct_name,
        ))
    return findings


def _field_short(field: Mapping[str, Any]) -> str:
    return "%s%s%s (%s bytes)" % (
        field.get("baseType", "?"),
        "" if int(field.get("columns") or 1) <= 1 else str(field.get("columns")),
        "[%s]" % field.get("elements") if int(field.get("elements") or 1) > 1 else "",
        field.get("byteSize"),
    )


def _field_text(field: Mapping[str, Any]) -> str:
    return "%s at offset %s" % (_field_short(field), field.get("byteOffset"))


def _explain_blocks(baseline: Sequence[Mapping[str, Any]],
                    candidate: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    key = lambda block: (int(block.get("space") or 0), int(block.get("bind") or 0))
    base_by = {key(b): b for b in baseline}
    cand_by = {key(c): c for c in candidate}
    for identity, block in base_by.items():
        name = "cbuffer register(b%d, space%d)" % (identity[1], identity[0])
        other = cand_by.get(identity)
        if other is None:
            findings.append(_finding(
                "constantBlocks", "missing-block", name, block.get("byteSize"), None,
                "The capture binds a %d-byte %s that the candidate does not declare."
                % (int(block.get("byteSize") or 0), name),
                "Declare that cbuffer with the captured register/space and byte size, and "
                "keep at least one member referenced so the compiler does not drop it.",
            ))
            continue
        if int(block.get("byteSize") or 0) != int(other.get("byteSize") or 0):
            captured_size = int(block.get("byteSize") or 0)
            candidate_size = int(other.get("byteSize") or 0)
            findings.append(_finding(
                "constantBlocks", "size-mismatch", name, captured_size, candidate_size,
                "%s is %d bytes in the capture but %d bytes in the candidate."
                % (name, captured_size, candidate_size),
                ("Shrink the last member so the declared size ends at byte %d (for example "
                 "`float3` instead of `float4`, or a smaller trailing array) while keeping "
                 "every earlier member offset." if candidate_size > captured_size else
                 "Pad the block to %d bytes with trailing members (`float pad0;`…) or a "
                 "wider final member, without moving earlier member offsets.") % captured_size,
            ))
        base_fields = [dict(f) for f in block.get("fields") or () if isinstance(f, Mapping)]
        cand_fields = [dict(f) for f in other.get("fields") or () if isinstance(f, Mapping)]
        if block.get("layoutCompleteness") in ("opaque", "ambiguous"):
            # Opaque/ambiguous captured layouts cannot name member differences;
            # the byte size (and the DXIL access proof) is all the capture knows.
            continue
        cand_by_offset = {int(f.get("byteOffset") or 0): f for f in cand_fields}
        for field in base_fields:
            offset = int(field.get("byteOffset") or 0)
            other_field = cand_by_offset.get(offset)
            if other_field is None:
                findings.append(_finding(
                    "constantBlocks", "member-missing", "%s @%d" % (name, offset),
                    _field_text(field), None,
                    "%s has a captured member %s with no candidate member starting there."
                    % (name, _field_text(field)),
                    "Declare a member at offset %d with %d bytes (use `packoffset(c%d.%s)` "
                    "or padding members so the offset is exact)." % (
                        offset, int(field.get("byteSize") or 0), offset // 16,
                        "xyzw"[(offset % 16) // 4]),
                ))
            elif int(field.get("byteSize") or 0) != int(other_field.get("byteSize") or 0) or \
                    str(field.get("baseType")) != str(other_field.get("baseType")):
                findings.append(_finding(
                    "constantBlocks", "member-type-mismatch", "%s @%d" % (name, offset),
                    _field_text(field), _field_text(other_field),
                    "%s member at offset %d is %s in the capture but %s in the candidate."
                    % (name, offset, _field_short(field), _field_short(other_field)),
                    "Match the captured member type/size at that offset.",
                ))
        base_offsets = {int(f.get("byteOffset") or 0) for f in base_fields}
        for field in cand_fields:
            offset = int(field.get("byteOffset") or 0)
            if offset not in base_offsets:
                findings.append(_finding(
                    "constantBlocks", "member-extra", "%s @%d" % (name, offset), None,
                    _field_text(field),
                    "The candidate declares %s in %s where the capture has no member "
                    "boundary." % (_field_text(field), name),
                    "Merge or move that member so member boundaries match the captured "
                    "offsets (the capture is the authority on layout, not the decompiler).",
                ))
    for identity, other in cand_by.items():
        if identity not in base_by:
            findings.append(_finding(
                "constantBlocks", "extra-block",
                "cbuffer register(b%d, space%d)" % (identity[1], identity[0]),
                None, other.get("byteSize"),
                "The candidate declares a cbuffer at b%d space%d that the capture does not "
                "bind." % (identity[1], identity[0]),
                "Remove it or move its members into the captured block they belong to.",
            ))
    return findings


def _explain_bindings(area: str, baseline: Sequence[Mapping[str, Any]],
                      candidate: Sequence[Mapping[str, Any]], prefix: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    key = lambda item: (int(item.get("space") or 0), int(item.get("bind") or 0))
    base_by = {key(b): b for b in baseline}
    cand_by = {key(c): c for c in candidate}

    def describe(item: Mapping[str, Any]) -> str:
        variable = item.get("variableType") or {}
        return "%s%s%s" % (
            item.get("descriptorType") or item.get("textureType") or "binding",
            " of %s%s" % (variable.get("baseType"), variable.get("columns"))
            if isinstance(variable, Mapping) and variable.get("baseType") else "",
            "[%s]" % item.get("arraySize") if int(item.get("arraySize") or 1) != 1 else "",
        )

    for identity, item in base_by.items():
        name = "%s%d space%d" % (prefix, identity[1], identity[0])
        other = cand_by.get(identity)
        if other is None:
            findings.append(_finding(
                area, "missing-binding", name, describe(item), None,
                "The capture binds %s at %s; the candidate does not declare it." % (describe(item), name),
                "Declare the resource at that register/space with the captured type and keep "
                "it referenced (an unreferenced resource is dropped by the compiler).",
            ))
            continue
        if describe(item) != describe(other) or item.get("arraySize") != other.get("arraySize"):
            findings.append(_finding(
                area, "binding-type-mismatch", name, describe(item), describe(other),
                "%s is %s in the capture but %s in the candidate." % (name, describe(item), describe(other)),
                "Declare it with the captured resource class and element type "
                "(e.g. `StructuredBuffer<float2>` rather than `Buffer<uint4>`), adapting the "
                "loads accordingly.",
            ))
    for identity, other in cand_by.items():
        if identity not in base_by:
            name = "%s%d space%d" % (prefix, identity[1], identity[0])
            findings.append(_finding(
                area, "extra-binding", name, None, describe(other),
                "The candidate declares %s at %s, which the capture does not bind." % (describe(other), name),
                "Remove it or move it to the captured register it stands for.",
            ))
    return findings


def explain_abi_differences(baseline: Mapping[str, Any] | None,
                            candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    """Describe every captured-vs-candidate mismatch by stable identity."""

    if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
        return {"schema": EXPLANATION_SCHEMA, "available": False,
                "reason": "baseline or candidate interface unavailable", "findings": []}
    findings: list[dict[str, Any]] = []
    findings += _explain_signature("input", _rows(baseline.get("inputSignature")),
                                   _rows(candidate.get("inputSignature")))
    findings += _explain_signature("output", _rows(baseline.get("outputSignature")),
                                   _rows(candidate.get("outputSignature")))
    findings += _explain_blocks(_rows(baseline.get("constantBlocks")),
                                _rows(candidate.get("constantBlocks")))
    findings += _explain_bindings("readOnlyResources", _rows(baseline.get("readOnlyResources")),
                                  _rows(candidate.get("readOnlyResources")), "t")
    findings += _explain_bindings("readWriteResources", _rows(baseline.get("readWriteResources")),
                                  _rows(candidate.get("readWriteResources")), "u")
    findings += _explain_bindings("samplers", _rows(baseline.get("samplers")),
                                  _rows(candidate.get("samplers")), "s")
    for scalar, label in (("threadGroupSize", "thread group size"),
                          ("outputTopology", "output topology")):
        if baseline.get(scalar) != candidate.get(scalar):
            findings.append(_finding(
                scalar, "changed", scalar, baseline.get(scalar), candidate.get(scalar),
                "The %s differs from the capture." % label,
                "Restore the captured attribute value.",
            ))
    truncated = len(findings) > MAX_FINDINGS
    findings = findings[:MAX_FINDINGS]
    areas: dict[str, int] = {}
    for item in findings:
        areas[item["area"]] = areas.get(item["area"], 0) + 1
    summary = ("; ".join("%d in %s" % (count, area) for area, count in sorted(areas.items()))
               if findings else "no identity-level differences found")
    return {
        "schema": EXPLANATION_SCHEMA,
        "available": True,
        "findingCount": len(findings),
        "truncated": truncated,
        "summary": summary,
        "findings": findings,
        "note": ("Derived from the captured and reflected interfaces for repair guidance; "
                 "the comparison verdict remains the authority."),
    }
