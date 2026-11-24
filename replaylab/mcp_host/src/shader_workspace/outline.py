"""Structured, read-only views of a (possibly huge) HLSL or GLSL source.

A 300 KB decompiled shader cannot be understood by reading it in 15 000-char
slices, and every slice costs tokens.  The outline gives the AI the skeleton
first — declarations, functions with their line ranges and call graph, where
each cbuffer member / resource is used, how much is still anonymous — and
then exact slices (one function, or a line range) on demand.  Everything is
derived from the file text; nothing here mutates the workspace.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Mapping
import hashlib
import json
import re
import threading
import time
from typing import Any

from .flow_graph import _function_records, _masked_source, _matching_pairs, _tokens

OUTLINE_SCHEMA = "renderdoc-ultra-shader-workspace-outline/v1"
MAX_SLICE_LINES = 400
MAX_USE_SITES = 12
MAX_SYMBOLS = 16
MAX_SYMBOL_CONTEXT_LINES = 96
MAX_SYMBOL_CONTEXT_CHARS = 24000
MAX_ANONYMOUS_EXAMPLES = 64
MAX_OUTLINE_PAGE_ITEMS = 64
DEFAULT_OUTLINE_PAGE_ITEMS = 32
_ANONYMOUS_GLOBAL_NUMERIC = re.compile(r"_\d{2,}")
_ANONYMOUS_MEMBER = re.compile(r"\b__replaylab_\w+\b")
_CBUFFER = re.compile(r"\bcbuffer\s+(?P<name>[A-Za-z_]\w*)\s*(?::\s*register\((?P<reg>[^)]*)\))?\s*\{(?P<body>[^{}]*)\}", re.DOTALL)
_MEMBER = re.compile(r"^\s*(?P<type>[A-Za-z_]\w*(?:\s*<[^>]*>)?)\s+(?P<name>[A-Za-z_]\w*)\s*(?:\[(?P<count>[^\]]*)\])?\s*(?::\s*packoffset\((?P<offset>[^)]*)\))?\s*;", re.MULTILINE)
_RESOURCE = re.compile(
    r"^\s*(?P<type>(?:RW)?Texture\w*|(?:RW)?(?:Structured|ByteAddress|Append|Consume)?Buffer\w*|"
    r"SamplerState|SamplerComparisonState|ConstantBuffer|TextureBuffer)\s*(?:<(?P<template>[^>\n]*)>)?\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[(?P<count>[^\]]*)\])?\s*:\s*register\((?P<reg>[^)]*)\)", re.MULTILINE)
_SEMANTIC = re.compile(r"\b(?P<type>[A-Za-z_]\w*)\s+(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<semantic>[A-Za-z_]\w*)")
_STRUCT = re.compile(r"\bstruct\s+(?P<name>[A-Za-z_]\w*)\s*\{(?P<body>[^{}]*)\}", re.DOTALL)
_SAMPLE = re.compile(r"\b(?P<texture>[A-Za-z_]\w*)\.(?P<method>Sample\w*|Load|Gather\w*|GetDimensions)\s*\(")
_WORD = re.compile(r"[A-Za-z_]\w*")


def _strip_comments(source: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", lambda m: " " * len(m.group()), source, flags=re.DOTALL)


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _interface_member_identifiers(source: str) -> set[str]:
    """Exclude known physical/struct fields from a cheap local-name summary."""

    clean = _strip_comments(source)
    names = {
        member.group("name")
        for pattern in (_CBUFFER, _STRUCT)
        for block in pattern.finditer(clean)
        for member in _MEMBER.finditer(block.group("body"))
    }
    names.update(match.group("name") for match in _RESOURCE.finditer(clean))
    return names


def _compact_anonymous_candidates(
    masked_lines: list[str], start_line: int, end_line: int,
    excluded: set[str], function_name: str, called_names: set[str],
) -> tuple[set[str], list[dict[str, Any]]]:
    """Find bounded lexical hints without a full quality parse per outline page.

    The lexical hints do not establish local declaration scope by themselves.
    This summary deliberately says *candidates* through its evidence limit; it
    must not claim zero while numbered-generic locals remain in a large source.
    """

    from .quality import _ANONYMOUS, _NUMBERED_GENERIC_LOCAL

    names: set[str] = set()
    examples: list[dict[str, Any]] = []
    for line_number in range(max(1, start_line), min(len(masked_lines), end_line) + 1):
        for match in _WORD.finditer(masked_lines[line_number - 1]):
            spelling = match.group()
            if (spelling in names or spelling in excluded or
                    spelling == function_name or spelling in called_names or
                    not (_ANONYMOUS.fullmatch(spelling) or
                         _NUMBERED_GENERIC_LOCAL.fullmatch(spelling))):
                continue
            names.add(spelling)
            if len(examples) < 4:
                examples.append({
                    "name": spelling, "line": line_number,
                    "function": function_name,
                    "renameSelector": function_name + "::" + spelling,
                })
    return names, examples


def _qualified_owner_index(tokens: list[Any], member_index: int,
                           reverse_pairs: Mapping[int, int]) -> int | None:
    """Find ``owner`` in owner.member or owner[index].member."""

    if member_index < 2 or tokens[member_index - 1].text not in {".", "->"}:
        return None
    cursor = member_index - 2
    while cursor >= 0 and tokens[cursor].text == "]":
        opening = reverse_pairs.get(cursor)
        if opening is None:
            return None
        cursor = opening - 1
    return cursor if cursor >= 0 else None


def outline(source: str, *, entry_point: str = "") -> dict[str, Any]:
    """Declarations, source quality, functions and bounded lexical use sites."""

    from .source_language import glsl_declarations, organization_capabilities, source_language
    from .quality import declared_binding_inventory, organization_quality
    language = source_language(source)
    clean = _strip_comments(source)
    lines = source.splitlines()
    declarations: dict[str, Any] = {"cbuffers": [], "resources": [], "structs": [], "entryInputs": [], "entryOutputs": []}
    member_names: dict[str, str] = {}
    for match in _CBUFFER.finditer(clean):
        members = []
        for member in _MEMBER.finditer(match.group("body")):
            members.append({"type": member.group("type").strip(), "name": member.group("name"),
                            "renameSelector": match.group("name") + "." + member.group("name"),
                            "count": member.group("count"), "packoffset": member.group("offset")})
            member_names[member.group("name")] = match.group("name")
        declarations["cbuffers"].append({
            "name": match.group("name"), "register": match.group("reg"), "line": _line_of(clean, match.start()),
            "memberCount": len(members),
            "anonymousMembers": sum(1 for item in members if item["name"].startswith("__replaylab_") or re.fullmatch(r"_\w*\d+", item["name"])),
            "members": members, "membersTruncated": False,
        })
    # Include typed ConstantBuffer<T>/TextureBuffer<T> owners as constant
    # blocks too. Their source selector is the bound owner plus member, not the
    # struct type, and the byte offset is the same evidence used by alias
    # publication.
    from .aliases import blocks_from_source
    parsed_blocks = blocks_from_source(source, limit=None) if language == "hlsl" else []
    existing_blocks = {str(item.get("name")): item for item in declarations["cbuffers"]}
    for block in parsed_blocks:
        name = str(block.get("block") or "")
        parsed_members = [
            {
                "name": str(member.get("name") or ""),
                "renameSelector": name + "." + str(member.get("name") or ""),
                "byteOffset": member.get("offset"),
                "byteSize": member.get("byteSize"),
            }
            for member in block.get("members", [])
        ]
        for member in parsed_members:
            member_names.setdefault(member["name"], name)
        if name in existing_blocks:
            # The lexical outline keeps source spelling/count/packoffset while
            # the layout parser supplies the concrete byte range used by alias
            # publication.  Join by declared order and spelling; never infer a
            # cross-member match from type or value similarity.
            existing_members = existing_blocks[name].get("members") or []
            parsed_by_name = {str(item.get("name") or ""): item for item in parsed_members}
            for existing_member in existing_members:
                parsed = parsed_by_name.get(str(existing_member.get("name") or ""))
                if parsed is not None:
                    existing_member["byteOffset"] = parsed.get("byteOffset")
                    existing_member["byteSize"] = parsed.get("byteSize")
        else:
            row = {
                "name": name,
                "register": ("b%d, space%d" % (block.get("bind"), block.get("space"))
                             if isinstance(block.get("bind"), int) and block.get("bind", -1) >= 0
                             else None),
                "typedBinding": True,
                "memberCount": len(parsed_members),
                "anonymousMembers": sum(
                    1 for item in parsed_members
                    if item["name"].startswith("__replaylab_")
                    or re.fullmatch(r"_\w*\d+", item["name"])
                ),
                "members": parsed_members,
                "membersTruncated": False,
            }
            declarations["cbuffers"].append(row)
            existing_blocks[name] = row
    quality = organization_quality(source)
    # Quality carries bounded examples for UI/status summaries.  The outline is
    # also the Host's source-selector inventory for organization, so declarations
    # must come from the complete inventory and cannot inherit those preview caps.
    binding_inventory = declared_binding_inventory(source) if language == "hlsl" else []
    resource_names: list[str] = []
    if language == "hlsl":
        outlined_blocks = {str(item.get("name") or "") for item in declarations["cbuffers"]}
        for item in binding_inventory:
            if item.get("kind") != "cbuffer" or str(item.get("name") or "") in outlined_blocks:
                continue
            name = str(item.get("name") or "")
            declarations["cbuffers"].append({
                "name": name, "register": item.get("register"), "line": item.get("line"),
                "typedBinding": True, "memberCount": 0, "anonymousMembers": 0,
                "members": [], "membersTruncated": False,
            })
            outlined_blocks.add(name)
        for item in binding_inventory:
            if item.get("kind") not in {"texture", "buffer", "sampler", "resource"}:
                continue
            resource_names.append(str(item.get("name") or ""))
            declarations["resources"].append({
                "type": item.get("type"), "template": item.get("template"),
                "name": item.get("name"), "count": item.get("count"),
                "register": item.get("register"), "line": item.get("line"),
            })
    if language == "glsl":
        glsl = glsl_declarations(source)
        resource_names = glsl["resources"] + glsl["cbuffers"]
        declarations["resources"] = [{"name": name, "interfaceNamePreserved": True}
                                     for name in glsl["resources"]]
        declarations["cbuffers"] = [{"name": name, "interfaceNamePreserved": True}
                                    for name in glsl["cbuffers"]]
        declarations["entryInputs"] = glsl["inputs"]
        declarations["entryOutputs"] = glsl["outputs"]
    for match in _STRUCT.finditer(clean):
        fields = [{"type": m.group("type"), "name": m.group("name"), "semantic": m.group("semantic")}
                  for m in _SEMANTIC.finditer(match.group("body"))]
        declarations["structs"].append({"name": match.group("name"), "line": _line_of(clean, match.start()),
                                        "fields": fields[:48], "fieldCount": len(fields)})
    try:
        records = _function_records(source)
    except Exception:  # noqa: BLE001 - outline must degrade, never fail
        records = []
    tokens = _tokens(source)
    square_pairs = _matching_pairs(tokens, "[", "]")
    reverse_square_pairs = {close: opening for opening, close in square_pairs.items()}
    binding_rows = {
        str(item.get("renameSelector") or ""): item
        for item in binding_inventory
        if isinstance(item, Mapping) and item.get("renameSelector")
    }
    for block in declarations["cbuffers"]:
        if language != "hlsl":
            continue
        selector = "cbuffer:" + str(block.get("name") or "")
        binding = binding_rows.get(selector, {})
        block.update({
            "renameSelector": selector,
            "generatedPhysicalName": bool(binding.get("generatedPhysicalName")),
            "neutralName": binding.get("neutralName"),
            "namingRequirement": (
                "host-bound-asset-name-or-deterministic-neutral"
                if binding.get("generatedPhysicalName") else "preserve-user-or-semantic-name"
            ),
        })
        for member in block.get("members", []):
            member_binding = binding_rows.get(str(member.get("renameSelector") or ""), {})
            member.update({
                "generatedPhysicalName": bool(member_binding.get("generatedPhysicalName")),
                "neutralName": member_binding.get("neutralName"),
                "namingRequirement": (
                    "host-bound-asset-name-or-deterministic-neutral"
                    if member_binding.get("generatedPhysicalName") else "preserve-user-or-semantic-name"
                ),
            })
    for resource in declarations["resources"]:
        if language != "hlsl":
            continue
        selector = "resource:" + str(resource.get("name") or "")
        binding = binding_rows.get(selector, {})
        resource.update({
            "renameSelector": selector,
            "resourceKind": binding.get("kind"),
            "generatedPhysicalName": bool(binding.get("generatedPhysicalName")),
            "neutralName": binding.get("neutralName"),
            "namingRequirement": (
                "host-bound-asset-name-or-deterministic-neutral"
                if binding.get("generatedPhysicalName") else "preserve-user-or-semantic-name"
            ),
        })
    function_quality = {(item["function"], item["line"]): item for item in quality["functions"]}
    callers: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for callee in record.get("calls", []):
            callers[callee].append(record["name"])
    functions = []
    anonymous_examples = []
    anonymous_reference_count = 0
    entry = str(entry_point or "")
    for record in records:
        start_line = record["line"]
        end_line = tokens[record["bodyEnd"]].line if record.get("bodyEnd") is not None and record["bodyEnd"] < len(tokens) else start_line
        body_text = "\n".join(lines[start_line - 1:end_line]) if 0 < start_line <= len(lines) else ""
        samples = Counter(m.group("texture") for m in _SAMPLE.finditer(body_text))
        metric = function_quality.get((record["name"], start_line), {})
        physical = defaultdict(list)
        physical_labels = [
            *metric.get("physicalPlaceholders", []),
            *metric.get("preservedPhysicalPlaceholders", []),
        ]
        for label in physical_labels:
            owner, separator, spelling = label.rpartition(".")
            for token_index in range(record["bodyStart"] + 1, record["bodyEnd"]):
                token = tokens[token_index]
                if token.text != (spelling if separator else label):
                    continue
                if separator:
                    owner_index = _qualified_owner_index(tokens, token_index, reverse_square_pairs)
                    if owner_index is None or tokens[owner_index].text != owner:
                        continue
                elif token_index and tokens[token_index - 1].text == ".":
                    continue
                physical[label].append({"line": token.line, "column": token.column})
        functions.append({
            "name": record["name"], "line": start_line, "endLine": end_line, "lines": end_line - start_line + 1,
            "parameters": [p.get("name") if isinstance(p, Mapping) else str(p) for p in record.get("parameters", [])][:16],
            "calls": record.get("calls", [])[:32], "callers": callers.get(record["name"], [])[:16],
            "isEntry": record["name"] == entry,
            "anonymousLocals": metric.get("anonymousIdentifierCount", 0),
            "textureSamples": dict(samples.most_common(8)),
            "quality": metric,
            "callsTruncated": len(record.get("calls", [])) > 32,
            "parametersTruncated": len(record.get("parameters", [])) > 16,
            "physicalPlaceholderReferences": [
                {"name": name, "owner": (name.rsplit(".", 1)[0] if "." in name else
                  member_names.get(name, "resource" if name in resource_names else "captured-interface")),
                 "renameSelector": (name if "." in name else
                    ((member_names[name] + "." + name) if name in member_names else name)),
                 "referenceCount": len(refs), "locations": refs[:MAX_USE_SITES],
                 "locationsTruncated": len(refs) > MAX_USE_SITES,
                 "aliasState": "not-inspected", "semanticBindingProven": False}
                for name, refs in list(physical.items())[:64]],
            "physicalPlaceholderReferencesTruncated": len(physical) > 64,
            "physicalPlaceholderReferenceCount": sum(map(len, physical.values())),
        })
        # Quality has already identified real function locals/parameters and
        # excluded physical CBuffer accesses. Reuse those exact anchors here;
        # the older numeric-only lexical regex missed Value139-style renames.
        candidate_names = {
            str(candidate.get("name") or "")
            for candidate in metric.get("anonymousIdentifierLocations", [])
            if isinstance(candidate, Mapping)
        }
        first_token = record["bodyStart"]
        while first_token > 0:
            first_token -= 1
            if (tokens[first_token].text == record["name"] and
                    tokens[first_token + 1].text == "("):
                break
        first_lines: dict[str, int] = {}
        for token in tokens[first_token:record["bodyEnd"] + 1]:
            if token.text in candidate_names:
                first_lines.setdefault(token.text, token.line)
        for candidate in metric.get("anonymousIdentifierLocations", []):
            if not isinstance(candidate, Mapping):
                continue
            spelling = str(candidate.get("name") or "")
            if not spelling:
                continue
            anonymous_reference_count += 1
            if len(anonymous_examples) < MAX_ANONYMOUS_EXAMPLES:
                anonymous_examples.append({
                    "name": spelling, "line": first_lines.get(
                        spelling, int(candidate.get("line") or start_line)),
                    "function": record["name"],
                    "renameSelector": record["name"] + "::" + spelling,
                })
    # Preserve discoverability of unused anonymous globals. The quality
    # metric above owns function locals; an unused global has no such anchor.
    local_spellings = {
        spelling for item in quality["functions"]
        for spelling in item.get("anonymousIdentifiers", [])
    }
    global_numeric: set[str] = set()
    for token in tokens:
        if (not _ANONYMOUS_GLOBAL_NUMERIC.fullmatch(token.text) or
                token.text in local_spellings or token.text in global_numeric):
            continue
        global_numeric.add(token.text)
        anonymous_reference_count += 1
        if len(anonymous_examples) < MAX_ANONYMOUS_EXAMPLES:
            anonymous_examples.append({
                "name": token.text, "line": token.line, "function": "",
                "renameSelector": token.text,
            })
    # use sites: cbuffer members and resources -> lines
    tracked = set(member_names) | set(resource_names)
    uses: dict[str, list[int]] = defaultdict(list)
    counts: Counter = Counter()
    if tracked:
        in_declaration_block = False
        for number, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith(("cbuffer", "struct")):
                in_declaration_block = "}" not in stripped
                continue
            if in_declaration_block:
                if stripped.startswith("}"):
                    in_declaration_block = False
                continue
            if stripped.startswith(("Texture", "SamplerState", "RW", "//", "ConstantBuffer", "Buffer", "StructuredBuffer", "ByteAddressBuffer")):
                continue
            for word in set(_WORD.findall(line)):
                if word in tracked:
                    counts[word] += 1
                    if len(uses[word]) < MAX_USE_SITES:
                        uses[word].append(number)
    symbol_use = {
        name: {"owner": member_names.get(name, "resource"),
               "renameSelector": ((member_names[name] + "." + name)
                                  if name in member_names else
                                  ("resource:" + name if language == "hlsl" else name)),
               "uses": counts[name], "lines": uses[name]}
        for name in sorted(tracked, key=lambda item: (-counts[item], item))
    }
    anonymous_locals = {
        (item["function"], spelling)
        for item in quality["functions"]
        for spelling in item.get("anonymousIdentifiers", [])
    }
    anonymous_locals.update(("", spelling) for spelling in global_numeric)
    anonymous_members = set(_ANONYMOUS_MEMBER.findall(clean))
    entry_record = next((f for f in functions if f["isEntry"]), None)
    largest = max((f["lines"] for f in functions), default=0)
    return {
        "schema": OUTLINE_SCHEMA,
        "sourceFormat": language,
        "organizationCapabilities": organization_capabilities(language),
        "totals": {"lines": len(lines), "chars": len(source), "functions": len(functions),
                   "cbuffers": len(declarations["cbuffers"]), "resources": len(declarations["resources"]),
                   "largestFunctionLines": largest},
        "availableCapabilities": {
            "pagedSections": ["declarations", "functions", "symbols"],
            "detailSlices": ["function", "lines", "symbols", "region"],
            "maxSliceLines": MAX_SLICE_LINES,
            "maxSymbolsPerSlice": MAX_SYMBOLS,
        },
        "declarations": declarations,
        "functions": functions,
        "symbolUse": symbol_use,
        "anonymity": {
            "anonymousLocals": len(anonymous_locals),
            "anonymousLocalExamples": anonymous_examples,
            "anonymousLocalExamplesTruncated": anonymous_reference_count > len(anonymous_examples),
            "anonymousCBufferMembers": len(anonymous_members),
            "unusedDeclared": sorted(name for name in tracked if counts[name] == 0)[:64],
        },
        "entry": entry_record,
        "evidenceLimits": {
            "locationSamplesMayBeTruncated": True,
            "lexicalReferencesDoNotProveSemantics": True,
            "physicalPlaceholderAliasStateObserved": False,
        },
    }


MAX_OUTLINE_PAYLOAD_BYTES = 20_000
_OUTLINE_INDEX_CACHE_ENTRIES = 4
_OUTLINE_INDEX_CACHE_BYTES = 1024 * 1024
_OUTLINE_INDEX_CACHE_TTL_SECONDS = 60.0
_OUTLINE_INDEX_CACHE: OrderedDict[
    tuple[str, str, str, str], tuple[float, int, list[dict[str, Any]]]
] = OrderedDict()
_OUTLINE_INDEX_CACHE_LOCK = threading.RLock()


def _clear_outline_index_cache() -> None:
    """Test/maintenance hook; the cache contains compact indices, never sources."""

    with _OUTLINE_INDEX_CACHE_LOCK:
        _OUTLINE_INDEX_CACHE.clear()


def _compact_index(
    source: str,
    language: str,
    section: str,
    entry_point: str,
    declarations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return a small SHA-keyed index shared by successive cursor pages.

    Only the compact rows are retained. The source and the old full-outline rows
    never enter this cache. Both entry count and serialized bytes are bounded, and
    the short TTL prevents a sequence of opened captures from becoming retained RSS.
    """

    source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    scoped_entry = str(entry_point or "") if section == "functions" else ""
    key = (source_sha, scoped_entry, section, language)
    now = time.monotonic()
    with _OUTLINE_INDEX_CACHE_LOCK:
        expired = [
            cached_key for cached_key, (created, unused_size, unused_rows)
            in _OUTLINE_INDEX_CACHE.items()
            if now - created > _OUTLINE_INDEX_CACHE_TTL_SECONDS
        ]
        for cached_key in expired:
            _OUTLINE_INDEX_CACHE.pop(cached_key, None)
        cached = _OUTLINE_INDEX_CACHE.get(key)
        if cached is not None:
            _OUTLINE_INDEX_CACHE.move_to_end(key)
            return cached[2]

    if section == "declarations":
        rows = _compact_declaration_items(source, language)
    elif section == "functions":
        rows = _compact_function_items(source, entry_point)
    elif section == "symbols":
        rows = _compact_symbol_items(source, language, declarations=declarations)
    else:  # pragma: no cover - caller validates the section
        raise ValueError("no compact outline index for %r" % section)
    encoded_size = len(json.dumps(
        rows, ensure_ascii=True, separators=(",", ":"), default=str
    ).encode("utf-8"))
    if encoded_size > _OUTLINE_INDEX_CACHE_BYTES:
        return rows

    with _OUTLINE_INDEX_CACHE_LOCK:
        _OUTLINE_INDEX_CACHE[key] = (now, encoded_size, rows)
        _OUTLINE_INDEX_CACHE.move_to_end(key)
        while (
            len(_OUTLINE_INDEX_CACHE) > _OUTLINE_INDEX_CACHE_ENTRIES
            or sum(item[1] for item in _OUTLINE_INDEX_CACHE.values())
            > _OUTLINE_INDEX_CACHE_BYTES
        ):
            _OUTLINE_INDEX_CACHE.popitem(last=False)
    return rows


def _compact_declaration_items(source: str, language: str) -> list[dict[str, Any]]:
    """Build exactly one section inventory without constructing a full outline."""

    if language == "hlsl":
        from .quality import declared_binding_inventory

        return [dict(item) for item in declared_binding_inventory(source)]
    from .source_language import glsl_declarations

    declarations = glsl_declarations(source)
    rows: list[dict[str, Any]] = []
    for category, key in (
        ("cbuffers", "cbuffers"),
        ("resources", "resources"),
        ("entryInputs", "inputs"),
        ("entryOutputs", "outputs"),
    ):
        for item in declarations.get(key, []):
            row = dict(item) if isinstance(item, Mapping) else {"name": str(item)}
            rows.append({"category": category, **row})
    return rows


def _compact_function_items(source: str, entry_point: str) -> list[dict[str, Any]]:
    """Return ranges/call edges/counts without quality or per-symbol location bags."""

    try:
        records = _function_records(source)
        tokens = _tokens(source)
    except Exception:  # noqa: BLE001 - a malformed source has an empty index
        return []
    lines = source.splitlines()
    masked_lines = _masked_source(source).splitlines()
    interface_names = _interface_member_identifiers(source)
    callers: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for callee in record.get("calls", []):
            callers[str(callee)].append(str(record.get("name") or ""))
    rows: list[dict[str, Any]] = []
    for record in records:
        name = str(record.get("name") or "")
        start = int(record.get("line") or 1)
        body_end = record.get("bodyEnd")
        end = (
            int(tokens[body_end].line)
            if isinstance(body_end, int) and 0 <= body_end < len(tokens)
            else start
        )
        body = "\n".join(lines[start - 1:end]) if 0 < start <= len(lines) else ""
        calls = [str(item) for item in (record.get("calls") or [])]
        incoming = callers.get(name, [])
        parameters = list(record.get("parameters") or [])
        samples = Counter(match.group("texture") for match in _SAMPLE.finditer(body))
        anonymous, anonymous_examples = _compact_anonymous_candidates(
            masked_lines, start, end, interface_names, name, set(calls))
        physical = set(_ANONYMOUS_MEMBER.findall(_strip_comments(body)))
        rows.append({
            "name": name,
            "line": start,
            "endLine": end,
            "lines": end - start + 1,
            "isEntry": name == str(entry_point or ""),
            "calls": calls[:32],
            "callCount": len(calls),
            "callsTruncated": len(calls) > 32,
            "callers": incoming[:16],
            "callerCount": len(incoming),
            "callersTruncated": len(incoming) > 16,
            "parameterCount": len(parameters),
            "anonymousLocals": len(anonymous),
            "anonymousLocalExamples": anonymous_examples,
            "textureResourceCount": len(samples),
            "textureSampleCount": sum(samples.values()),
            "qualityCounts": {
                "statementCount": body.count(";"),
                "bodyLineCount": end - start + 1,
                "anonymousIdentifierCount": len(anonymous),
                "physicalPlaceholderCount": len(physical),
                "sectionCommentCount": sum(
                    1 for line in body.splitlines() if line.lstrip().startswith("//")
                ),
            },
            "detailSlices": {"function": name, "region": "%d-%d" % (start, end)},
        })
    return rows


def _compact_symbol_items(
    source: str,
    language: str,
    declarations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build bounded lexical symbol facts; line samples never exceed eight."""

    inventory = declarations
    if inventory is None:
        inventory = _compact_declaration_items(source, language)
    bindings: list[dict[str, Any]] = []
    if language == "hlsl":
        for item in inventory:
            if item.get("kind") not in {
                "member", "texture", "buffer", "sampler", "resource"
            }:
                continue
            bindings.append({
                "name": str(item.get("name") or ""),
                "owner": str(item.get("owner") or "resource"),
                "renameSelector": str(item.get("renameSelector") or ""),
            })
    else:
        for item in inventory:
            name = str(item.get("name") or "")
            if name:
                bindings.append({
                    "name": name,
                    "owner": str(item.get("category") or "interface"),
                    "renameSelector": name,
                })
    tracked = {item["name"] for item in bindings if item["name"]}
    counts: Counter = Counter()
    locations: dict[str, list[int]] = defaultdict(list)
    if tracked:
        for token in _tokens(_strip_comments(source)):
            if token.text not in tracked:
                continue
            counts[token.text] += 1
            if len(locations[token.text]) < 8:
                locations[token.text].append(int(token.line))
    return [
        {
            **item,
            "lexicalOccurrences": int(counts[item["name"]]),
            "lineSamples": locations[item["name"]],
            "lineSamplesReturned": len(locations[item["name"]]),
            "lineSamplesTruncated": counts[item["name"]] > len(locations[item["name"]]),
        }
        for item in bindings
    ]


def _bounded_page_item(
    item: Mapping[str, Any], section: str, *, maximum_bytes: int = 12_000
) -> dict[str, Any]:
    """Project one pathological row explicitly instead of breaking page bounds."""

    original = json.dumps(
        dict(item), ensure_ascii=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    if len(original) <= maximum_bytes:
        return dict(item)

    truncated_fields: list[str] = []

    def project(value: Any, path: str, string_limit: int, list_limit: int) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): project(
                    child, path + "." + str(key) if path else str(key),
                    string_limit, list_limit,
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            if len(value) > list_limit:
                truncated_fields.append(path + "[%d:%d]" % (list_limit, len(value)))
                value = value[:list_limit]
            return [
                project(child, "%s[%d]" % (path, index), string_limit, list_limit)
                for index, child in enumerate(value)
            ]
        if isinstance(value, str) and len(value.encode("utf-8")) > string_limit:
            truncated_fields.append(path)
            preview = value[: max(1, string_limit // 2)]
            return preview + "...[field preview omitted]"
        return value

    projected = project(dict(item), "", 1024, 24)
    projection = {
        "complete": False,
        "reason": "item-exceeds-response-budget",
        "section": section,
        "originalByteSize": len(original),
        "originalSha256": hashlib.sha256(original).hexdigest(),
        "truncatedFields": truncated_fields[:32],
        "sourceDetailCapabilities": ["lines", "region", "function", "symbols"],
    }
    projected["_projection"] = projection
    encoded = json.dumps(projected, ensure_ascii=True, default=str).encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return projected

    # A row with many individually large fields gets a strict identity view.
    identity_keys = {
        "kind", "category", "name", "owner", "renameSelector", "line", "endLine",
        "register", "bind", "space", "offset", "byteOffset", "byteSize", "isEntry",
    }
    identity = {
        str(key): project(value, str(key), 256, 8)
        for key, value in item.items()
        if str(key) in identity_keys
    }
    identity["_projection"] = projection
    return identity


def paged_outline(
    source: str,
    *,
    entry_point: str = "",
    section: str = "summary",
    offset: int = 0,
    limit: int = DEFAULT_OUTLINE_PAGE_ITEMS,
) -> dict[str, Any]:
    """Return one bounded, stable outline section.

    The formal organization gate reads the complete source inventory directly.
    This paging exists only for the model-facing planning view, where a large
    declaration/function list must remain reachable without one oversized MCP
    result or a silent tail truncation.
    """

    name = str(section or "summary").strip().lower()
    if name not in {"summary", "declarations", "functions", "symbols"}:
        raise ValueError("section must be summary, declarations, functions, or symbols")
    if type(offset) is not int or offset < 0:
        raise ValueError("outline offset must be a non-negative integer")
    if type(limit) is not int or limit <= 0:
        raise ValueError("outline limit must be a positive integer")
    # Oversized read requests need no retry: return the supported bounded page
    # and its actual cursor so every remaining item stays reachable.
    limit = min(limit, MAX_OUTLINE_PAGE_ITEMS)

    from .source_language import organization_capabilities, source_language

    language = source_language(source)

    def metadata(section_name: str, total: int, page_offset: int = 0) -> dict[str, Any]:
        returned = min(limit, max(0, total - page_offset))
        next_offset = page_offset + returned
        return {
            "section": section_name,
            "offset": page_offset,
            "limit": limit,
            "total": total,
            "returned": returned,
            "hasMore": next_offset < total,
            "nextOffset": next_offset if next_offset < total else None,
        }

    if name == "summary":
        declaration_items = _compact_index(
            source, language, "declarations", entry_point
        )
        function_items = _compact_index(source, language, "functions", entry_point)
        symbol_items = _compact_index(
            source, language, "symbols", entry_point,
            declarations=declaration_items,
        )
        largest = max((int(item.get("lines") or 0) for item in function_items), default=0)
        source_lines = source.count("\n") + (1 if source else 0)
        totals = {
            "lines": source_lines,
            "chars": len(source),
            "functions": len(function_items),
            "cbuffers": sum(
                1 for item in declaration_items
                if item.get("kind") == "cbuffer" or item.get("category") == "cbuffers"
            ),
            "resources": sum(
                1 for item in declaration_items
                if item.get("kind") in {"texture", "buffer", "sampler", "resource"}
                or item.get("category") == "resources"
            ),
            "largestFunctionLines": largest,
        }
        pages = {
            "declarations": metadata("declarations", len(declaration_items)),
            "functions": metadata("functions", len(function_items)),
            "symbols": metadata("symbols", len(symbol_items)),
        }
        clean = _strip_comments(source)
        anonymous_candidate_count = sum(
            int(item.get("anonymousLocals") or 0) for item in function_items)
        anonymous_candidate_examples = [
            example
            for item in function_items
            for example in item.get("anonymousLocalExamples", [])
        ]
        result = {
            "schema": OUTLINE_SCHEMA,
            "sourceFormat": language,
            "organizationCapabilities": organization_capabilities(language),
            "totals": totals,
            "anonymity": {
                "anonymousLocals": anonymous_candidate_count,
                "anonymousLocalExamples": anonymous_candidate_examples[:MAX_ANONYMOUS_EXAMPLES],
                "anonymousLocalExamplesTruncated": (
                    anonymous_candidate_count > len(anonymous_candidate_examples[:MAX_ANONYMOUS_EXAMPLES])),
                "basis": "bounded-lexical-candidates",
                "anonymousCBufferMembers": len(set(_ANONYMOUS_MEMBER.findall(clean))),
            },
            "entry": next(
                (dict(item) for item in function_items if item.get("isEntry")), None
            ),
            "outlineSections": pages,
            "requestedSection": name,
            "availableCapabilities": {
                "pagedSections": ["declarations", "functions", "symbols"],
                "detailSlices": ["function", "lines", "symbols", "region"],
            },
        }
        result["page"] = {
            "section": "summary", "offset": 0, "limit": limit,
            "total": 1, "returned": 1, "hasMore": False, "nextOffset": None,
        }
        result["continuation"] = {
            "sections": {
                section_name: {
                    "total": page["total"],
                    "firstOffset": 0 if page["total"] else None,
                }
                for section_name, page in pages.items()
            }
        }
        return result

    if name == "declarations":
        collection = _compact_index(source, language, name, entry_point)
    elif name == "functions":
        collection = _compact_index(source, language, name, entry_point)
    else:
        collection = _compact_index(source, language, name, entry_point)
    result = {
        "schema": OUTLINE_SCHEMA,
        "sourceFormat": language,
        "sourceTotals": {
            "lines": source.count("\n") + (1 if source else 0),
            "chars": len(source),
        },
        "requestedSection": name,
    }
    candidates = [
        _bounded_page_item(candidate, name)
        for candidate in collection[offset:offset + limit]
    ]
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        trial_items = [*items, candidate]
        trial_page = metadata(name, len(collection), offset)
        trial_page.update({
            "returned": len(trial_items),
            "hasMore": offset + len(trial_items) < len(collection),
            "nextOffset": (
                offset + len(trial_items)
                if offset + len(trial_items) < len(collection) else None
            ),
            "items": trial_items,
        })
        trial = {**result, "page": trial_page}
        size = len(json.dumps(trial, ensure_ascii=True, default=str).encode("utf-8"))
        if size > MAX_OUTLINE_PAYLOAD_BYTES:
            break
        items = trial_items
    page = metadata(name, len(collection), offset)
    page.update({
        "returned": len(items),
        "hasMore": offset + len(items) < len(collection),
        "nextOffset": (
            offset + len(items) if offset + len(items) < len(collection) else None
        ),
        "items": items,
        "responseBudgetBytes": MAX_OUTLINE_PAYLOAD_BYTES,
        "truncatedByResponseBudget": len(items) < len(candidates),
    })
    result["outlineSections"] = {name: {
        key: value for key, value in page.items() if key != "items"
    }}
    result["page"] = page
    result["continuation"] = {
        "section": name,
        "nextOffset": page["nextOffset"],
        "hasMore": page["hasMore"],
    }
    return result


def function_slice(source: str, name: str) -> dict[str, Any]:
    records = _function_records(source)
    tokens = _tokens(source)
    lines = source.splitlines()
    for record in records:
        if record["name"] != name:
            continue
        start = record["line"]
        end = tokens[record["bodyEnd"]].line if record["bodyEnd"] < len(tokens) else start
        return line_slice(source, start, end, label="function " + name, lines=lines)
    raise KeyError("no function named %r in this file" % name)


def line_slice(source: str, start: int, end: int, *, label: str = "", lines: list[str] | None = None) -> dict[str, Any]:
    lines = source.splitlines() if lines is None else lines
    start = max(1, int(start))
    end = min(len(lines), int(end))
    if end < start:
        raise ValueError("line range is empty")
    truncated = False
    if end - start + 1 > MAX_SLICE_LINES:
        end = start + MAX_SLICE_LINES - 1
        truncated = True
    text = "\n".join("%6d| %s" % (number, lines[number - 1]) for number in range(start, end + 1))
    return {"schema": OUTLINE_SCHEMA, "slice": label or ("lines %d-%d" % (start, end)),
            "startLine": start, "endLine": end, "truncated": truncated, "text": text}


def symbol_slices(source: str, symbols: list[str]) -> dict[str, Any]:
    """Exact lexical references, not inferred semantics or rename authority."""
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_SYMBOLS or
            any(not isinstance(name, str) or len(name) > 256 or
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in symbols)):
        raise ValueError("symbols must contain 1 to 16 Shader identifiers (at most 256 characters each)")
    names = list(dict.fromkeys(symbols))
    locations = {name: [] for name in names}
    counts = Counter()
    for token in _tokens(source):
        if token.text not in locations:
            continue
        counts[token.text] += 1
        refs = locations[token.text]
        if len(refs) < MAX_USE_SITES:
            refs.append({"line": token.line, "column": token.column})
    lines = source.splitlines()
    selected = set()
    # Round-robin keeps a heavily used first symbol from hiding all the others.
    for occurrence in range(MAX_USE_SITES):
        for name in names:
            refs = locations[name]
            if occurrence >= len(refs):
                continue
            line = refs[occurrence]["line"]
            for number in (line, line - 1, line + 1):
                if 1 <= number <= len(lines) and len(selected) < MAX_SYMBOL_CONTEXT_LINES:
                    selected.add(number)
    snippets, chars = [], 0
    truncated = any(counts[name] > len(locations[name]) for name in names)
    for number in sorted(selected):
        original = lines[number - 1]
        text = "%6d| %s" % (number, original[:600])
        truncated = truncated or len(original) > 600
        if chars + len(text) + 1 > MAX_SYMBOL_CONTEXT_CHARS:
            truncated = True
            break
        snippets.append(text)
        chars += len(text) + 1
    visible = {int(text.split("|", 1)[0]) for text in snippets}
    truncated = truncated or any(ref["line"] not in visible for refs in locations.values() for ref in refs)
    return {"schema": OUTLINE_SCHEMA, "slice": "symbols", "symbols": [
        {"name": name, "occurrences": counts[name], "locations": locations[name],
         "truncated": counts[name] > len(locations[name])} for name in names],
        "truncated": truncated, "text": "\n".join(snippets),
        "evidenceLimits": {
            "locationsAreLexicalOnly": True,
            "semanticMeaningProven": False,
            "contextMayBeTruncated": truncated,
        }}


__all__ = ["MAX_SLICE_LINES", "OUTLINE_SCHEMA", "function_slice", "line_slice", "outline", "symbol_slices"]
