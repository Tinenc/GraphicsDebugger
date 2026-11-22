"""Bounded, source-derived readability observations for Shader IDE queries."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Mapping

from .flow_graph import _function_records, _masked_source
from .source_language import source_language

# A numbered generic noun is still a decompiler temporary after a spelling
# change.  In particular, ``_23 -> mainPSinnerValue139`` does not tell a reader
# whether the value is a normal, a dot product, a light direction or something
# else.  Keep this deliberately lexical: quality feedback asks the Agent to
# inspect the definition and uses, rather than inventing a meaning here.
_ANONYMOUS = re.compile(r"^(?:_\d+|(?:temp|tmp|var)\d+|frontier_phi_\w+)$", re.IGNORECASE)
_NUMBERED_GENERIC_LOCAL = re.compile(
    r"^(?:[A-Za-z_]\w*?)?(?:Value|Variable|Intermediate|Temp|Tmp)_?\d+$",
    re.IGNORECASE,
)
_PHYSICAL_NAME = r"(?:_\d+|__replaylab_[A-Za-z0-9_]+)"
_PHYSICAL_PLACEHOLDER = re.compile(
    r"^" + _PHYSICAL_NAME + r"$", re.IGNORECASE
)
_MECHANICAL_STAGE = re.compile(
    r"(?:stage|chunk|part|segment)[_\-\s]*\d+$", re.IGNORECASE
)
_COMMENT = re.compile(r"(?m)^[ \t]+//\s*(\S.*)$")
_PURPOSE_COMMENT = re.compile(r"(?m)^[ \t]*//(?:\s*\[ai\])?\s*(\S.*)$")
_SUPERFICIAL_COMMENT = re.compile(
    r"^(?:"
    r"(?:continue|process|handle|compute|calculate|perform)\b"
    r"(?:\s+(?:the|this|next|captured|source|shader|processing))*"
    r"\s+(?:data|values?|inputs?|outputs?|calculation|work|step|section)"
    r"|(?:stage|step|section|part|chunk)\s*[_#\-]?\s*\d+"
    r"|(?:继续处理|处理(?:这些)?(?:数据|数值|输入|输出)|计算(?:阶段|部分)|第?\s*\d+\s*(?:步|段|部分))"
    r")[.!。！]?$",
    re.IGNORECASE,
)
_GENERATED_NUMERIC = re.compile(r"^_[0-9]+(?:_[0-9]+)*$")
_GENERATED_BLOCK = re.compile(
    r"^(?:cb|cbuffer|constantbuffer)[_0-9]+$", re.IGNORECASE
)
_GENERATED_RESOURCE = re.compile(
    r"^(?:(?:t|s|u|b)\d+|(?:texture|sampler|buffer|resource|rwbuffer)[_0-9]+)$",
    re.IGNORECASE,
)
_HLSL_RESOURCE_DECL = re.compile(
    r"(?<![A-Za-z0-9_])(?P<type>(?:RasterizerOrdered)?(?:RW)?Texture\w*|FeedbackTexture\w*|"
    r"(?:RasterizerOrdered)?(?:RW)?(?:StructuredBuffer|ByteAddressBuffer|Buffer)|"
    r"AppendStructuredBuffer|ConsumeStructuredBuffer|SamplerState|SamplerComparisonState|"
    r"RaytracingAccelerationStructure|ConstantBuffer|TextureBuffer)\s*"
    r"(?:<(?P<template>[^>\n]*)>)?\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[(?P<count>[^\]]*)\])?\s*"
    r"(?::\s*register\((?P<reg>[^)]*)\))?\s*;",
)
_HLSL_CBUFFER_DECL = re.compile(
    r"\b(?:cbuffer|tbuffer)\s+(?P<name>[A-Za-z_]\w*)[^{}]*\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)
_HLSL_STRUCT_DECL = re.compile(
    r"\bstruct\s+(?P<name>[A-Za-z_]\w*)\s*\{(?P<body>[^{}]*)\}\s*;",
    re.DOTALL,
)
_HLSL_TYPED_CBUFFER_DECL = re.compile(
    r"\b(?:ConstantBuffer|TextureBuffer)\s*<\s*(?P<type>[A-Za-z_]\w*)\s*>\s*"
    r"(?P<name>[A-Za-z_]\w*)",
)
_HLSL_MEMBER_DECL = re.compile(
    r"(?:^|;)\s*(?:(?:row_major|column_major)\s+)?"
    r"(?P<type>[A-Za-z_]\w*(?:\s*<[^>]*>)?)\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[[^\]]*\])*\s*(?::\s*packoffset\([^)]*\))?\s*(?=;|$)",
    re.MULTILINE,
)
_BINDING_REGISTER = re.compile(
    r"^\s*(?P<class>[tsubc])(?P<bind>\d+)"
    r"(?:\s*,\s*space(?P<space>\d+))?\s*$",
    re.IGNORECASE,
)


def generated_physical_name(name: str, kind: str) -> bool:
    """Whether one declaration spelling is a known decompiler/ABI placeholder."""

    value = str(name or "")
    if _PHYSICAL_PLACEHOLDER.fullmatch(value) or _GENERATED_NUMERIC.fullmatch(value):
        return True
    if kind == "cbuffer":
        return _GENERATED_BLOCK.fullmatch(value) is not None
    if kind in {"texture", "buffer", "sampler", "resource"}:
        return _GENERATED_RESOURCE.fullmatch(value) is not None
    return False


def _register_parts(register: Any, *, fallback_class: str = "") -> tuple[str, int | None, int]:
    match = _BINDING_REGISTER.fullmatch(str(register or ""))
    if match is None:
        return fallback_class, None, 0
    return match.group("class").lower(), int(match.group("bind")), int(match.group("space") or 0)


def _data_role(type_name: str) -> str:
    value = re.sub(r"\s+", "", str(type_name or "")).lower()
    if "x" in value and re.search(r"[2-4]x[2-4]$", value):
        return "Matrix"
    if re.search(r"(?:float|half|double|min16float|float\d+_t)[2-4]$", value):
        return "Vector"
    if re.search(r"(?:int|int\d+_t|min16int)[2-4]$", value):
        return "IntegerVector"
    if re.search(r"(?:uint|uint\d+_t|min16uint|dword)[2-4]$", value):
        return "UnsignedVector"
    if value.startswith(("float", "half", "double", "min16float")):
        return "Scalar"
    if value.startswith(("uint", "dword", "min16uint")):
        return "Unsigned"
    if value.startswith(("int", "min16int")):
        return "Integer"
    if value.startswith("bool"):
        return "Boolean"
    return "Data"


def _neutral_binding_name(
    *, kind: str, type_name: str, register: Any, line: int,
    byte_offset: Any = None, declaration_index: int = 0,
) -> str:
    register_class, bind, space = _register_parts(register)
    binding = (
        "S%d%s%d" % (space, register_class.upper(), bind)
        if bind is not None
        else "AtLine%dD%d" % (line, max(0, int(declaration_index)))
    )
    if kind == "cbuffer":
        return "CBuffer" + binding
    if kind == "member":
        role = _data_role(type_name)
        location = (
            "AtByte%d" % int(byte_offset)
            if isinstance(byte_offset, int) and not isinstance(byte_offset, bool) and byte_offset >= 0
            else "AtDeclaration%d" % max(0, int(declaration_index))
        )
        # Members are variables, so retain lower camel case while encoding the
        # complete physical owner. This makes the fallback reproducible and
        # carries no material/engine semantic claim.
        return "cbuffer%s%s%s" % (binding, role, location)
    if kind == "texture":
        return "tTexture" + binding
    if kind == "sampler":
        return "sSampler" + binding
    if kind == "buffer":
        return ("rwBuffer" if str(type_name).startswith("RW") else "buffer") + binding
    return "resource" + binding


def declared_binding_inventory(source: str) -> list[dict[str, Any]]:
    """Return every HLSL CBuffer/resource declaration with stable rename facts.

    This is lexical source evidence. It does not claim that a register is bound
    at one EID or that a proposed semantic name came from an asset.
    """

    if source_language(source) != "hlsl":
        return []
    masked = _masked_source(source)
    declarations: list[dict[str, Any]] = []
    from .aliases import blocks_from_source

    member_types: dict[tuple[str, str], str] = {}
    block_lines: dict[str, int] = {}
    struct_members: dict[str, list[tuple[str, str]]] = {}
    for match in _HLSL_CBUFFER_DECL.finditer(masked):
        block_name = match.group("name")
        block_lines.setdefault(block_name, masked.count("\n", 0, match.start()) + 1)
        for member in _HLSL_MEMBER_DECL.finditer(match.group("body")):
            member_types[(block_name, member.group("name"))] = member.group("type")
    for match in _HLSL_STRUCT_DECL.finditer(masked):
        struct_members[match.group("name")] = [
            (member.group("name"), member.group("type"))
            for member in _HLSL_MEMBER_DECL.finditer(match.group("body"))
        ]
    for match in _HLSL_TYPED_CBUFFER_DECL.finditer(masked):
        block_name = match.group("name")
        block_lines.setdefault(block_name, masked.count("\n", 0, match.start()) + 1)
        for member_name, type_name in struct_members.get(match.group("type"), []):
            member_types[(block_name, member_name)] = type_name

    # Formal quality review needs the complete declaration set.  The alias
    # store keeps its own bounded display/read API, but that bound must never
    # become a naming-gate bypass.
    for block in blocks_from_source(source, limit=None):
        block_name = str(block.get("block") or "")
        bind = block.get("bind")
        space = block.get("space")
        register = (
            "b%d, space%d" % (bind, int(space or 0))
            if isinstance(bind, int) and not isinstance(bind, bool) and bind >= 0
            else ""
        )
        line = block_lines.get(block_name, 0)
        declaration_index = len(declarations)
        block_row = {
            "kind": "cbuffer", "name": block_name,
            "renameSelector": "cbuffer:" + block_name,
            "line": line, "type": "ConstantBuffer", "register": register,
            "bind": bind, "space": int(space or 0),
            "generatedPhysicalName": generated_physical_name(block_name, "cbuffer"),
        }
        block_row["neutralName"] = _neutral_binding_name(
            kind="cbuffer", type_name="ConstantBuffer", register=register, line=line,
            declaration_index=declaration_index,
        )
        declarations.append(block_row)
        for index, member in enumerate(block.get("members", [])):
            declaration_index = len(declarations)
            member_name = str(member.get("name") or "")
            row = {
                "kind": "member", "name": member_name, "owner": block_name,
                "renameSelector": block_name + "." + member_name,
                "line": line, "type": member_types.get((block_name, member_name), ""),
                "register": register, "bind": bind, "space": int(space or 0),
                "byteOffset": member.get("offset"), "byteSize": member.get("byteSize"),
                "declarationIndex": declaration_index, "memberIndex": index,
                "generatedPhysicalName": generated_physical_name(member_name, "member"),
            }
            row["neutralName"] = _neutral_binding_name(
                kind="member", type_name=row["type"], register=register, line=line,
                byte_offset=row["byteOffset"], declaration_index=declaration_index,
            )
            declarations.append(row)

    # ``blocks_from_source`` is layout-oriented and therefore has no member row
    # from which to construct an empty block (or a typed block whose struct is
    # absent/malformed).  Naming review is declaration-oriented, so retain the
    # owner declaration even when no layout member can be recovered.
    known_blocks = {str(item.get("name") or "") for item in declarations
                    if item.get("kind") == "cbuffer"}
    fallback_blocks: list[tuple[int, str, str]] = []
    for match in _HLSL_CBUFFER_DECL.finditer(masked):
        header = masked[match.start():match.start("body")]
        register_match = re.search(r"\bregister\s*\(([^)]*)\)", header)
        fallback_blocks.append((
            match.start(), match.group("name"),
            str(register_match.group(1) if register_match else ""),
        ))
    for match in _HLSL_RESOURCE_DECL.finditer(masked):
        if match.group("type") in {"ConstantBuffer", "TextureBuffer"}:
            fallback_blocks.append((match.start(), match.group("name"), str(match.group("reg") or "")))
    for offset, block_name, register in sorted(fallback_blocks):
        if block_name in known_blocks:
            continue
        unused_class, bind, space = _register_parts(register, fallback_class="b")
        line = masked.count("\n", 0, offset) + 1
        declaration_index = len(declarations)
        row = {
            "kind": "cbuffer", "name": block_name,
            "renameSelector": "cbuffer:" + block_name,
            "line": line, "type": "ConstantBuffer", "register": register,
            "bind": bind, "space": space, "declarationIndex": declaration_index,
            "generatedPhysicalName": generated_physical_name(block_name, "cbuffer"),
        }
        row["neutralName"] = _neutral_binding_name(
            kind="cbuffer", type_name="ConstantBuffer", register=register, line=line,
            declaration_index=declaration_index,
        )
        declarations.append(row)
        known_blocks.add(block_name)

    excluded_resource_spans = [
        (match.start("body"), match.end("body"))
        for pattern in (_HLSL_CBUFFER_DECL, _HLSL_STRUCT_DECL)
        for match in pattern.finditer(masked)
    ]
    try:
        from .inspection import _Program
        resource_program = _Program(source)
        excluded_resource_spans.extend(
            (record["bodyStartOffset"], record["bodyEndOffset"])
            for record in resource_program.records
        )
    except (KeyError, TypeError, ValueError):
        # The source parser will report malformed code elsewhere; declaration
        # inventory stays bounded and conservative here.
        pass
    for match in _HLSL_RESOURCE_DECL.finditer(masked):
        if any(start <= match.start() < end for start, end in excluded_resource_spans):
            continue
        type_name = match.group("type")
        if type_name in {"ConstantBuffer", "TextureBuffer"}:
            continue
        name = match.group("name")
        if type_name.startswith("Sampler"):
            kind = "sampler"
        elif "Texture" in type_name:
            kind = "texture"
        elif "Buffer" in type_name:
            kind = "buffer"
        else:
            kind = "resource"
        line = masked.count("\n", 0, match.start()) + 1
        register = str(match.group("reg") or "")
        unused_class, bind, space = _register_parts(register)
        declaration_index = len(declarations)
        row = {
            "kind": kind, "name": name, "renameSelector": "resource:" + name,
            "line": line, "type": type_name, "template": match.group("template"),
            "count": match.group("count"),
            "register": register, "bind": bind, "space": space,
            "declarationIndex": declaration_index,
            "generatedPhysicalName": generated_physical_name(name, kind),
        }
        row["neutralName"] = _neutral_binding_name(
            kind=kind, type_name=type_name, register=register, line=line,
            declaration_index=declaration_index,
        )
        declarations.append(row)
    return declarations


def is_deterministic_neutral_name(binding: Mapping[str, Any], target: str) -> bool:
    """Whether ``target`` is exactly the Host-derived neutral fallback."""

    return str(target or "") == str(binding.get("neutralName") or "")


def _placeholder_members(body: str) -> set[str]:
    """Return generated field declarations from one already-masked block."""
    return {
        name
        for name in re.findall(r"\b[A-Za-z_]\w*\b", body)
        if _PHYSICAL_PLACEHOLDER.fullmatch(name)
    }


def _physical_bindings(masked: str, language: str) -> tuple[set[str], dict[str, set[str]]]:
    """Read only syntactically explicit constant/interface bindings.

    Flat names model HLSL cbuffer members (and uninstanced GLSL blocks).
    Qualified names model ``ConstantBuffer<T> object.member`` and instanced
    GLSL blocks.  The result is lexical review evidence, never an ABI claim.
    """
    flat: set[str] = set()
    qualified: dict[str, set[str]] = {}
    if language == "hlsl":
        for match in re.finditer(r"\b(?:cbuffer|tbuffer)\s+\w+[^{}]*\{([^{}]*)\}", masked):
            flat.update(_placeholder_members(match[1]))

        structs = {
            match[1]: _placeholder_members(match[2])
            for match in re.finditer(r"\bstruct\s+([A-Za-z_]\w*)[^{}]*\{([^{}]*)\}\s*;", masked)
        }
        for match in re.finditer(
            r"\b(?:ConstantBuffer|TextureBuffer)\s*<\s*([A-Za-z_]\w*)\s*>\s*([A-Za-z_]\w*)\b",
            masked,
        ):
            members = structs.get(match[1], set())
            if members:
                qualified.setdefault(match[2], set()).update(members)
        return flat, qualified

    # GLSL interface names may participate in name-based linkage.  We expose
    # unresolved generated names as preserved facts but never demand a source
    # rename that validate_language_renames must reject.
    block_pattern = re.compile(
        r"\b(?:uniform|buffer|in|out)\s+[A-Za-z_]\w*\s*\{([^{}]*)\}\s*"
        r"([A-Za-z_]\w*)?\s*(?:\[[^]]*\])?\s*;"
    )
    for match in block_pattern.finditer(masked):
        members = _placeholder_members(match[1])
        if match[2]:
            qualified.setdefault(match[2], set()).update(members)
        else:
            flat.update(members)
    flat.update(
        match[1]
        for match in re.finditer(
            r"\b(?:uniform|buffer|in|out)\s+(?:[A-Za-z_]\w*\s+)+(" + _PHYSICAL_NAME + r")"
            r"\s*(?:\[[^]]*\])?\s*;",
            masked,
        )
    )
    return flat, qualified


def _function_declarations(program: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return source-bound parameters/locals with lexical scopes.

    This deliberately shares the conservative parser used by region planning.
    It understands custom/template types, comma declarations and nested/loop
    scopes, avoiding the former primitive-type regex which hid real CBuffer
    reads whenever an unrelated local happened to use the same spelling.
    """

    program.parse_body(record)
    scopes = [(record["bodyStart"], record["bodyEnd"])]
    scopes += [
        (index, close)
        for index, close in program.pairs.items()
        if program.word(index) == "{" and record["bodyStart"] < index < close < record["bodyEnd"]
    ]
    declarations: list[dict[str, Any]] = []
    for index in range(record["bodyStart"] + 1, record["bodyEnd"]):
        if (program.word(index - 1) not in {";", "{", "}", ":"}
                and not (program.word(index - 1) == "(" and program.word(index - 2) == "for")):
            continue
        for declaration in program.declaration(index, record["bodyEnd"]):
            containing = [scope for scope in scopes
                          if scope[0] < declaration["index"] < scope[1]]
            if not containing:
                continue
            scope = max(containing, key=lambda item: item[0])
            loop = next((statement for statement in program.statements
                         if statement["kind"] == "for"
                         and record["bodyStart"] < statement["first"] < record["bodyEnd"]
                         and statement["first"] < declaration["index"]
                         < program.pairs.get(statement["controlIndex"] + 1, -1)), None)
            if loop:
                scope = (loop["first"], loop["last"] + 1)
            declarations.append({**declaration, "scopeStart": scope[0],
                                 "scopeEnd": scope[1], "parameter": False})
    # Function records already bind parameter names reliably. Their scope is
    # the whole body; exact declaration token indexes are unnecessary here.
    declarations.extend({"name": item["name"], "index": record["bodyStart"],
                         "scopeStart": record["bodyStart"], "scopeEnd": record["bodyEnd"],
                         "parameter": True}
                        for item in record.get("parameters", []) if item.get("name"))
    return declarations


def _physical_references(
    program: Any,
    record: dict[str, Any],
    flat: set[str],
    qualified: dict[str, set[str]],
    declarations: list[dict[str, Any]],
) -> tuple[dict[str, list[int]], set[int]]:
    """Return exact physical-use token indexes, respecting lexical shadows."""

    def shadowed(name: str, index: int) -> bool:
        return any(item["name"] == name and item["index"] <= index
                   and item["scopeStart"] < index < item["scopeEnd"]
                   for item in declarations)

    references: dict[str, list[int]] = {}
    physical_indexes: set[int] = set()
    for index in range(record["bodyStart"] + 1, record["bodyEnd"]):
        word = program.word(index)
        if word in flat and program.word(index - 1) not in {".", "->"} and not shadowed(word, index):
            references.setdefault(word, []).append(index)
            physical_indexes.add(index)
        if word not in qualified or shadowed(word, index):
            continue
        cursor = index + 1
        # ConstantBuffer<T> materials[2] and GLSL interface arrays retain the
        # same owner identity through one or more index expressions.
        while program.word(cursor) == "[" and cursor in program.pairs:
            cursor = program.pairs[cursor] + 1
        if program.word(cursor) not in {".", "->"}:
            continue
        member_index = cursor + 1
        member = program.word(member_index)
        if member in qualified[word]:
            references.setdefault(word + "." + member, []).append(member_index)
            physical_indexes.add(member_index)
    return references, physical_indexes


def _declaration_has_initializer(
    program: Any, declaration: Mapping[str, Any], limit: int
) -> bool:
    """Whether one parsed local declarator has an initializer.

    Walk only this comma-separated declarator and skip nested calls, indexes
    and aggregate initializers. This keeps complex RHS expressions visible to
    the temporary-chain audit instead of limiting it to short primitive regexes.
    """

    cursor = int(declaration.get("index", -1)) + 1
    while cursor < limit:
        word = program.word(cursor)
        if word == "=":
            return True
        if word in {",", ";"}:
            return False
        if word in {"(", "[", "{"} and cursor in program.pairs:
            cursor = program.pairs[cursor]
        cursor += 1
    return False


def _single_use_local_records(
    program: Any, record: dict[str, Any], declarations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return initialized locals referenced exactly once after declaration.

    This is review advice, never authority to inline a value. Counting parsed
    declarations closes the case where an Agent merely renames a long chain of
    call/expression temporaries and thereby escapes the old short-RHS heuristic.
    """

    uses = Counter(record.get("bodyTokens", []))
    rows: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for declaration in declarations:
        if declaration.get("parameter"):
            continue
        index = int(declaration.get("index", -1))
        name = str(declaration.get("name") or "")
        if index < 0 or index in seen_indexes or uses[name] != 2:
            continue
        if not _declaration_has_initializer(program, declaration, record["bodyEnd"]):
            continue
        seen_indexes.add(index)
        rows.append({"name": name, "line": program.tokens[index].line})
    return rows


def organization_quality(source: str) -> dict[str, Any]:
    """Inspect every executable function, including small extracted helpers."""
    masked = _masked_source(source)
    language = source_language(source)
    declaration_inventory = declared_binding_inventory(source)
    generated_declarations = [
        item for item in declaration_inventory if item.get("generatedPhysicalName") is True
    ]
    flat_physical, qualified_physical = _physical_bindings(masked, language)
    rename_supported = language == "hlsl"
    # Import lazily: inspection calls organization_quality only inside its
    # public function, so this shares one parser without a module-import cycle.
    from .inspection import _Program
    program = _Program(source)
    functions = []
    all_physical_placeholders: set[str] = set()
    all_preserved_placeholders: set[str] = set()
    for record in program.records:
        start, end = record["bodyStartOffset"], record["bodyEndOffset"]
        body = source[start:end]
        masked_body = masked[start:end]
        tokens = record["bodyTokens"]
        parameters = {item["name"] for item in record["parameters"]}
        declarations = _function_declarations(program, record)
        local_names = {declaration["name"] for declaration in declarations}
        names = sorted({
            token for token in [*tokens, *parameters]
            if _ANONYMOUS.fullmatch(token)
            or (token in local_names and _NUMBERED_GENERIC_LOCAL.fullmatch(token))
        })
        physical_references, physical_indexes = _physical_references(
            program, record, flat_physical, qualified_physical, declarations
        )
        # Suppress a generated spelling from the anonymous-local finding only
        # when every occurrence in this body is an identified physical access.
        # A data._2 access or local _2 alongside constants._2 remains visible.
        # A decompiled function can contain thousands of distinct anonymous
        # locals. Searching its full token range once per spelling made every
        # fresh structural report quadratic, even though the extractor itself
        # only takes a fraction of a second. Index the same source-bound token
        # identities once and preserve the old all-physical and no-use cases.
        wanted_names = set(names)
        seen_names: set[str] = set()
        first_nonphysical_line: dict[str, int] = {}
        for index in range(record["bodyStart"] + 1, record["bodyEnd"]):
            spelling = program.word(index)
            if spelling not in wanted_names:
                continue
            seen_names.add(spelling)
            if spelling not in first_nonphysical_line and index not in physical_indexes:
                first_nonphysical_line[spelling] = program.tokens[index].line
        anonymous_locations: dict[str, int] = {}
        retained_names: list[str] = []
        for spelling in names:
            if spelling in seen_names and spelling not in first_nonphysical_line:
                continue
            retained_names.append(spelling)
            anonymous_locations[spelling] = first_nonphysical_line.get(spelling, record["line"])
        names = retained_names
        consumed_labels = sorted(physical_references)
        physical_placeholders = consumed_labels if rename_supported else []
        preserved_placeholders = [] if rename_supported else consumed_labels
        all_physical_placeholders.update(physical_placeholders)
        all_preserved_placeholders.update(preserved_placeholders)
        statements = record["statements"]
        all_comment_matches = [match for match in _COMMENT.finditer(body) if len(match[1]) >= 12]
        comment_matches = [
            match for match in all_comment_matches
            if not re.search(r"(?:Stage\s*\d|连续计算段|unknown\s*$)", match[1])
            and not _SUPERFICIAL_COMMENT.fullmatch(match[1].strip())
        ]
        superficial_comment_matches = [
            match for match in all_comment_matches if match not in comment_matches
        ]
        comment_lines = [record["line"] + body.count("\n", 0, match.start())
                         for match in comment_matches]
        normalized_comments = {
            re.sub(r"\W+", " ", match[1].casefold()).strip()
            for match in comment_matches
        }
        function_end_line = program.tokens[record["bodyEnd"]].line
        body_line_count = max(1, function_end_line - record["line"] + 1)
        section_basis_line_count = sum(
            1 for line in masked_body.splitlines() if line.strip()
        )
        # Purpose comment: Host ``// [ai]`` block or another substantial comment
        # immediately above the function header.
        purpose_comment = ""
        header_start = record["line"] - 1
        preceding = source.splitlines()[max(0, header_start - 6):header_start]
        for raw_line in reversed(preceding):
            match = _PURPOSE_COMMENT.match(raw_line)
            if match is None:
                if raw_line.strip():
                    break
                continue
            text = match.group(1).strip()
            if len(text) >= 12 and not _SUPERFICIAL_COMMENT.fullmatch(text):
                purpose_comment = text
                break
        long_expressions = [item for item in statements if len(item["tokens"]) > 180]
        max_expression_tokens = max((len(item["tokens"]) for item in statements), default=0)
        # This is advice for the model, never an automatic inliner. A complex
        # initializer can depend on sampling, derivatives, mutation or precise
        # rounding, but renaming every one-use decompiler step does not by
        # itself create a readable algorithm.
        single_use_records = _single_use_local_records(program, record, declarations)
        single_use = [item["name"] for item in single_use_records]
        functions.append({
            "function": record["name"], "line": record["line"],
            "parameterCount": len(record["parameters"]),
            "outParameterCount": sum(
                parameter.get("mode") in {"out", "inout"}
                for parameter in record["parameters"]
            ),
            "returnSemantic": record.get("returnSemantic", ""),
            "parameterSemanticCount": sum(
                bool(parameter.get("semantic")) for parameter in record["parameters"]
            ),
            "statementCount": len(statements),
            "bodyLineCount": body_line_count,
            "sectionCommentBasisLineCount": section_basis_line_count,
            "anonymousIdentifierCount": len(names), "anonymousIdentifiers": names[:16],
            "anonymousIdentifierLocations": [
                {"name": name, "line": anonymous_locations[name]}
                for name in names[:16]
            ],
            "physicalPlaceholderCount": len(physical_placeholders),
            "physicalPlaceholders": physical_placeholders[:32],
            "physicalPlaceholderRenameSupported": rename_supported,
            "preservedPhysicalPlaceholderCount": len(preserved_placeholders),
            "preservedPhysicalPlaceholders": preserved_placeholders[:32],
            "mechanicalStageName": bool(_MECHANICAL_STAGE.search(record["name"])),
            "purposeComment": purpose_comment,
            "hasPurposeComment": bool(purpose_comment),
            "sectionCommentCount": len(comment_matches),
            "sectionCommentLines": comment_lines[:16],
            "distinctSectionCommentCount": len(normalized_comments),
            "superficialSectionCommentCount": len(superficial_comment_matches),
            "superficialSectionCommentLines": [
                record["line"] + body.count("\n", 0, match.start())
                for match in superficial_comment_matches[:16]
            ],
            "oversizedExpressionCount": len(long_expressions),
            "oversizedExpressionLines": [item["startLine"] for item in long_expressions[:8]],
            "maxExpressionTokenCount": max_expression_tokens,
            "singleUseShortLocalCount": len(single_use),
            "singleUseShortLocals": single_use[:16],
            "singleUseLocalCount": len(single_use),
            "singleUseLocals": single_use[:16],
            "singleUseLocalLocations": single_use_records[:16],
        })
    all_physical = sorted(all_physical_placeholders)
    all_preserved = sorted(all_preserved_placeholders)
    declared_labels = sorted({*flat_physical,
                              *(owner + "." + member for owner, members in qualified_physical.items()
                                for member in members)})
    return {"schema": "replaylab-shader-organization-quality/v1", "sourceFormat": language,
            "functions": functions,
            "anonymousFunctionCount": sum(bool(item["anonymousIdentifierCount"]) for item in functions),
            "physicalPlaceholderFunctionCount": sum(bool(item["physicalPlaceholderCount"]) for item in functions),
            "physicalPlaceholderCount": len(all_physical),
            "physicalPlaceholders": all_physical[:128],
            "declaredPhysicalPlaceholderCount": len(declared_labels),
            "declaredPhysicalPlaceholders": declared_labels[:256],
            "declaredBindingCount": len(declaration_inventory),
            "declaredBindings": declaration_inventory[:1024],
            "declaredBindingsTruncated": len(declaration_inventory) > 1024,
            "generatedPhysicalDeclarationCount": len(generated_declarations),
            "generatedPhysicalDeclarations": generated_declarations[:512],
            "generatedPhysicalDeclarationsTruncated": len(generated_declarations) > 512,
            "generatedPhysicalDeclarationCounts": dict(Counter(
                str(item.get("kind") or "unknown") for item in generated_declarations
            )),
            "preservedPhysicalPlaceholderFunctionCount": sum(
                bool(item["preservedPhysicalPlaceholderCount"]) for item in functions
            ),
            "preservedPhysicalPlaceholderCount": len(all_preserved),
            "preservedPhysicalPlaceholders": all_preserved[:128],
            "mechanicalFunctionCount": sum(item["mechanicalStageName"] for item in functions)}
