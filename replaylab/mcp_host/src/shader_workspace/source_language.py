"""Bounded source-language facts for organization, never replay authority."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .flow_graph import _function_records, _masked_source, _tokens


SOURCE_LANGUAGES = frozenset({"hlsl", "glsl"})
SECTION_COMMENT_LINE_INTERVAL = 120
_GLSL_TYPE = re.compile(r"^(?:(?:[biud]?vec)[234]|d?mat[234](?:x[234])?|(?:[iu]?sampler|[iu]?image)\w*|atomic_uint)$")
_GLSL_RESERVED = frozenset("""
attribute varying layout uniform buffer shared coherent volatile restrict readonly writeonly
centroid flat smooth noperspective patch sample invariant precise highp mediump lowp precision
subroutine in out inout const struct void bool int uint float double true false
if else for while do switch case default break continue return discard
binding location component index set offset align std140 std430 packed row_major column_major
local_size_x local_size_y local_size_z early_fragment_tests vertices invocations
points lines triangles lines_adjacency triangles_adjacency triangle_strip line_strip max_vertices
mix fract mod inversesqrt texture textureLod textureGrad textureProj textureGather texelFetch
textureSize textureQueryLod textureQueryLevels imageLoad imageStore imageSize
dFdx dFdy dFdxFine dFdyFine dFdxCoarse dFdyCoarse fwidthFine fwidthCoarse
barrier memoryBarrier memoryBarrierBuffer memoryBarrierShared memoryBarrierImage groupMemoryBarrier
EmitVertex EndPrimitive interpolateAtCentroid interpolateAtSample interpolateAtOffset
""".split())
_LOCAL_RENAME_SELECTOR = re.compile(
    r"^(?P<function>[A-Za-z_]\w*)::(?P<local>[A-Za-z_]\w*)$"
)
_FIELD_RENAME_SELECTOR = re.compile(
    r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$"
)


def source_language(source: str) -> str:
    """Classify source from syntax which survives decompilation/export.

    ``#version`` is optional in embedded OpenGL sources and some RenderDoc
    exports.  Prefer strong HLSL markers first, then accept a small collection
    of declarations and built-ins that are specific to GLSL.  This remains a
    bounded language route choice, not a claim that the source compiles.
    """

    clean = _masked_source(source)
    if re.search(
        r"(?im)(?:^\s*#\s*(?:version|extension)\b|"
        r"\blayout\s*\([^)]*\)\s*(?:(?:flat|smooth|noperspective|centroid|sample|patch|"
        r"invariant|readonly|writeonly|coherent|restrict|volatile|highp|mediump|lowp)\s+)*"
        r"(?:uniform|buffer|in|out)\b)",
        clean,
    ):
        return "glsl"
    if re.search(
        r"(?i)\b(?:cbuffer|tbuffer)\b|\bregister\s*\(|\bpackoffset\s*\(|"
        r"\bSV_[A-Za-z0-9_]+\b|\b(?:RW)?Texture(?:1D|2D|3D|Cube)\w*\b|"
        r"\bSampler(?:Comparison)?State\b|\bConstantBuffer\s*<",
        clean,
    ):
        return "hlsl"
    if re.search(
        r"(?im)(?:^|[;}])\s*(?:(?:flat|smooth|noperspective|centroid|sample|patch|"
        r"invariant|readonly|writeonly|coherent|restrict|volatile|highp|mediump|lowp)\s+)*"
        r"(?:uniform|buffer|in|out)\s+(?:[A-Za-z_]\w*\s+)?[A-Za-z_]\w*\s*(?:\[|;|\{)|"
        r"\bvoid\s+main\s*\(|\bgl_(?:Position|FragCoord|FragDepth|FrontFacing|VertexID|InstanceID)\b|"
        r"\b(?:[biud]?vec[234]|d?mat[234](?:x[234])?|[iu]?sampler\w*|[iu]?image\w*)\b",
        clean,
    ):
        return "glsl"
    return "hlsl"


def organization_capabilities(language: str, *, compile_eligible: bool = True) -> dict[str, Any]:
    """Describe available editing operations without prescribing an AI workflow."""

    supported = language in SOURCE_LANGUAGES
    hlsl = supported and language == "hlsl"
    return {
        "sourceFormat": language,
        "supported": supported,
        "rename": supported,
        "functionComments": supported,
        "structureExtraction": hlsl,
        "boundedRewrite": hlsl,
        "functionLedger": supported,
        "draftMerge": supported,
        "finalCompileAbiAvailable": bool(supported and compile_eligible),
        "functionCallGraph": supported,
        "reason": "source-language-supported" if supported else "source-language-unavailable",
    }


def glsl_declarations(source: str) -> dict[str, list[str]]:
    """Read ordinary explicit GLSL interface declarations; omit unknown syntax."""
    clean = _masked_source(source)
    # Function parameters and local `out` qualifiers are not stage interfaces.
    chars = list(clean)
    for record in _function_records(source):
        # Mask only the body. Declaration matches below are anchored at a line
        # or statement start and require a storage qualifier, not parameters.
        for index in range(record["bodyStartOffset"], record["bodyEndOffset"]):
            if chars[index] != "\n":
                chars[index] = " "
    clean = "".join(chars)
    resources, blocks, inputs, outputs = [], [], [], []
    qualifiers = r"(?:(?:layout\s*\([^)]*\)|flat|smooth|noperspective|centroid|sample|patch|invariant|readonly|writeonly|coherent|restrict|volatile|highp|mediump|lowp)\s+)*"
    simple = re.compile(r"(?:^|[;}])\s*" + qualifiers + r"(uniform|buffer|in|out)\s+" + qualifiers + r"\w+\s+(\w+)\s*(?:\[[^]]*\]\s*)?;", re.MULTILINE)
    for match in simple.finditer(clean):
        target = inputs if match[1] == "in" else outputs if match[1] == "out" else resources
        target.append(match[2])
    block = re.compile(r"(?:^|[;}])\s*" + qualifiers + r"(uniform|buffer|in|out)\s+(\w+)\s*\{[^{}]*\}\s*(\w+)?\s*(?:\[[^]]*\]\s*)?;", re.MULTILINE)
    for match in block.finditer(clean):
        target = inputs if match[1] == "in" else outputs if match[1] == "out" else blocks
        target.append(match[3] or match[2])
    for name in ("gl_Position", "gl_FragDepth", "gl_FragColor", "gl_FragData", "gl_SampleMask"):
        if re.search(r"\b" + name + r"\b", _masked_source(source)):
            outputs.append(name)
    return {"resources": sorted(set(resources)), "cbuffers": sorted(set(blocks)),
            "inputs": sorted(set(inputs)), "outputs": sorted(set(outputs))}


def validate_language_renames(source: str, renames: Mapping[str, str], language: str,
                             entry_point: str) -> None:
    """GLSL's initial lane edits only unambiguous local/function identifiers.

    Global names can be part of OpenGL name-based linkage even without explicit
    locations. Keep every global/interface name, preprocessor token and built-in
    unchanged. This is stricter than compilation, which cannot prove linkage.
    """
    if language != "glsl":
        return
    tokens, records = _tokens(source), _function_records(source)
    function_names = {record["name"] for record in records}
    allowed, function_indexes = set(function_names), set()
    locals_by_function: dict[str, set[str]] = {}
    for record in records:
        local_names = {item["name"] for item in record["parameters"]}
        function_indexes.update(range(record["bodyStart"] + 1, record["bodyEnd"]))
        body = tokens[record["bodyStart"] + 1:record["bodyEnd"]]
        for index, token in enumerate(body[:-2]):
            if (re.fullmatch(r"[A-Za-z_]\w*", token.text) and
                    re.fullmatch(r"[A-Za-z_]\w*", body[index + 1].text) and
                    body[index + 2].text in {"=", ";", "[", ","} and
                    token.text not in {"return", "case"}):
                local_names.add(body[index + 1].text)
        allowed.update(local_names)
        locals_by_function.setdefault(record["name"], set()).update(local_names)
    # An identifier shared by a local and an interface member is ambiguous for
    # the existing token rename; refuse it instead of touching the interface.
    global_identifiers = {t.text for index, t in enumerate(tokens)
                          if index not in function_indexes}
    parameter_names = {item["name"] for record in records for item in record["parameters"]}
    protected = global_identifiers - function_names - parameter_names
    # Parameters may shadow global/member names. Detect their non-function
    # occurrences independently by masking complete function definitions.
    top = _masked_source(source)
    for record in reversed(records):
        name_index = next(index for index, token in enumerate(tokens)
                          if token.line == record["line"] and token.column == record["column"])
        first = name_index - 1
        while first > 0 and tokens[first - 1].text not in {";", "}", "{"}:
            first -= 1
        header_start = tokens[first].offset
        top = top[:header_start] + " " * (record["bodyEndOffset"] + 1 - header_start) + top[record["bodyEndOffset"] + 1:]
    protected.update(re.findall(r"[A-Za-z_]\w*", top))
    preprocessing, continuation = [], False
    for line in source.splitlines():
        if continuation or line.lstrip().startswith("#"):
            preprocessing.append(line)
            continuation = line.rstrip().endswith("\\")
    protected.update(re.findall(r"[A-Za-z_]\w*", "\n".join(preprocessing)))
    called = {tokens[i].text for i in range(len(tokens) - 1) if tokens[i + 1].text == "("}
    intrinsic_names = called - function_names
    owner_aliases = {
        str(target): str(selector)
        for selector, target in renames.items()
        if re.fullmatch(r"[A-Za-z_]\w*", str(selector)) and
        re.fullmatch(r"[A-Za-z_]\w*", str(target))
    }
    for selector, new in renames.items():
        old = str(selector)
        scoped = _LOCAL_RENAME_SELECTOR.fullmatch(old)
        if scoped:
            owner = scoped.group("function")
            seen: set[str] = set()
            while owner in owner_aliases and owner not in seen:
                seen.add(owner)
                owner = owner_aliases[owner]
            old = owner + "::" + scoped.group("local")
            scoped = _LOCAL_RENAME_SELECTOR.fullmatch(old)
        if _FIELD_RENAME_SELECTOR.fullmatch(old):
            raise ValueError("GLSL rename must preserve interfaces and fields: %s" % old)
        if ("::" in old or "." in old) and scoped is None:
            raise ValueError("unsupported GLSL rename selector: %s" % old)
        local_function = scoped.group("function") if scoped else ""
        old_name = scoped.group("local") if scoped else old
        if old_name == new:
            continue
        if scoped:
            matching_functions = [record for record in records if record["name"] == local_function]
            if (len(matching_functions) != 1 or
                    old_name not in locals_by_function.get(local_function, set())):
                raise ValueError("GLSL Function::local selector is not uniquely source-bound: %s" % old)
        elif (old_name not in allowed or old_name in protected or old_name == entry_point or
              old_name.startswith("gl_") or old_name in _GLSL_RESERVED or _GLSL_TYPE.fullmatch(old_name)):
            raise ValueError("GLSL rename must preserve interfaces, built-ins and entry point: %s" % old_name)
        if re.fullmatch(r"[stpq]{1,4}", old_name) and re.search(
                r"\.\s*" + re.escape(old_name) + r"\b", source):
            raise ValueError("GLSL texture-coordinate component selection cannot be renamed: %s" % old_name)
        if (new.startswith("gl_") or "__" in new or new in _GLSL_RESERVED or
                new in intrinsic_names or _GLSL_TYPE.fullmatch(new)):
            raise ValueError("GLSL rename target is reserved: %s" % new)
