"""Read-only lexical region planning; never compile, edit, or grant replay authority."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from .flow_graph import _function_records, _matching_pairs, _tokens
from .source_language import organization_capabilities, source_language

REGION_SCHEMA = "renderdoc-ultra-shader-region-inspection/v1"
MAX_REGION_LINES = 400
MAX_SYMBOL_ROWS = 256
MAX_REFERENCE_LOCATIONS = 8
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
PHYSICAL_CBUFFER_PLACEHOLDER = re.compile(r"^__replaylab_s\d+_b\d+_c\d+(?:_[A-Za-z0-9]+)*$")
_QUALIFIERS = frozenset("const precise static uniform in out inout highp mediump lowp row_major column_major invariant coherent volatile restrict readonly writeonly shared groupshared".split())
_CONTROL = frozenset("if else for while do switch case default return break continue discard".split())
_PURE_CALLS = frozenset("abs acos acosh all any asin asinh atan atan2 atanh ceil clamp clip cos cosh cross ddx ddy ddx_coarse ddy_coarse ddx_fine ddy_fine degrees determinant distance dot exp exp2 faceforward floor fma fmod frac fract fwidth inversesqrt inverse isfinite isinf isnan length lerp log log2 log10 mad max min mix mod mul normalize pow radians reflect refract round roundEven rsqrt saturate sign sin sinh smoothstep sqrt step tan tanh transpose trunc asfloat asint asuint floatBitsToInt floatBitsToUint intBitsToFloat uintBitsToFloat packHalf2x16 unpackHalf2x16 dFdx dFdy dFdxFine dFdyFine dFdxCoarse dFdyCoarse texture textureLod textureGrad textureProj textureGather texelFetch textureSize textureQueryLod textureQueryLevels".split())
_TYPES = re.compile(r"^(?:(?:float|half|double|int|uint|bool|min16float|min16int|min16uint)(?:[1-4](?:x[1-4])?)?|[biud]?vec[234]|d?mat[234](?:x[234])?)$")
_OUT_INTRINSICS = {"sincos": {1: "out", 2: "out"}, "modf": {1: "out"}, "frexp": {1: "out"}}


class _Program:
    def __init__(self, source: str):
        self.source = source
        self.tokens = _tokens(source)
        self.records = _function_records(source)
        self.pairs = {}
        for opening, closing in [("(", ")"), ("[", "]"), ("{", "}")]:
            self.pairs.update(_matching_pairs(self.tokens, opening, closing))
        self.statements: list[dict[str, Any]] = []

    def word(self, index: int) -> str:
        return self.tokens[index].text if 0 <= index < len(self.tokens) else ""

    def statement(self, start: int, limit: int, parent: int) -> int:
        """Parse complete C-family statements; loop-header semicolons stay inside."""
        first = start
        while self.word(start) == "[" and start in self.pairs:
            start = self.pairs[start] + 1  # HLSL control-flow attributes
        kind = self.word(start)
        cursor = start
        if kind == "{":
            end = self.pairs.get(start, limit)
            cursor = start + 1
            while cursor < end:
                following = self.statement(cursor, end, start)
                if following <= cursor:
                    break
                cursor = following
            cursor = end + 1
        elif kind in {"if", "for", "while", "switch"} and self.word(start + 1) == "(":
            close = self.pairs.get(start + 1)
            if close is None:
                return limit
            cursor = self.statement(close + 1, limit, start)
            if kind == "if" and self.word(cursor) == "else":
                cursor = self.statement(cursor + 1, limit, start)
        elif kind == "do":
            cursor = self.statement(start + 1, limit, start)
            if self.word(cursor) == "while" and self.word(cursor + 1) == "(":
                cursor = self.pairs.get(cursor + 1, limit) + 1
                if self.word(cursor) == ";":
                    cursor += 1
        elif kind in {"case", "default"}:
            while cursor < limit and self.word(cursor) != ":":
                cursor += 1
            return min(limit, cursor + 1)
        else:
            while cursor < limit:
                word = self.word(cursor)
                if word == ";":
                    cursor += 1
                    break
                if word in {"(", "[", "{"}:
                    close = self.pairs.get(cursor)
                    if close is None:
                        return limit
                    cursor = close
                elif word == "}":
                    break
                cursor += 1
        if first < cursor <= limit:
            self.statements.append({"first": first, "last": cursor - 1, "parent": parent,
                                    "controlIndex": start,
                                    "kind": kind if kind in _CONTROL or kind == "{" else "statement"})
        return cursor

    def parse_body(self, record: dict[str, Any]) -> None:
        cursor = record["bodyStart"] + 1
        while cursor < record["bodyEnd"]:
            following = self.statement(cursor, record["bodyEnd"], record["bodyStart"])
            if following <= cursor:
                break
            cursor = following

    def declaration(self, start: int, limit: int) -> list[dict[str, Any]]:
        cursor, qualifiers = start, []
        while self.word(cursor) in _QUALIFIERS:
            qualifiers.append(self.word(cursor)); cursor += 1
        if not _IDENT.fullmatch(self.word(cursor)) or self.word(cursor) in _CONTROL:
            return []
        type_start = cursor
        cursor += 1
        if self.word(cursor) == "<":
            depth = 1; cursor += 1
            while cursor < limit and depth:
                depth += self.word(cursor) == "<"
                depth -= self.word(cursor) == ">"
                cursor += 1
            if depth:
                return []
        type_text = "".join(t.text for t in self.tokens[type_start:cursor])
        type_end = cursor
        if not _IDENT.fullmatch(self.word(cursor)) or self.word(cursor + 1) not in {"=", ";", ",", "[", ":", ")"}:
            return []
        result = []
        while cursor < limit and _IDENT.fullmatch(self.word(cursor)):
            result.append({"name": self.word(cursor), "type": type_text, "qualifiers": qualifiers,
                           "typeStart": type_start, "typeEnd": type_end,
                           "index": cursor, "line": self.tokens[cursor].line,
                           "array": self.word(cursor + 1) == "["})
            cursor += 1
            while cursor < limit and self.word(cursor) not in {";", ","}:
                if self.word(cursor) in {"(", "[", "{"}:
                    cursor = self.pairs.get(cursor, limit)
                cursor += 1
            if self.word(cursor) != ",":
                break
            cursor += 1
        return result


def inspect_region(source: str, start: int, end: int, *,
                   _extraction_only: bool = False,
                   _program: _Program | None = None) -> dict[str, Any]:
    """Plan a bounded region using full enclosing-function reference counts.

    IO is conservative lexical data flow, including declared call argument modes.
    Unknown calls, macros, ambiguous declarations and escaping control flow are
    explicit diagnostics. None of these facts authorize moving resource reads.
    """
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(source.splitlines()):
        raise ValueError("region must name a nonempty line range inside the source")
    if not _extraction_only and end - start + 1 > MAX_REGION_LINES:
        raise ValueError("region inspection is limited to 400 lines; choose complete computation statements")
    # The mutating extractor already needs this exact source's tokens and
    # function records to move the selected statements. Reuse its parse rather
    # than scanning a large decompiled shader twice for one local extraction.
    if _program is not None and _program.source != source:
        raise ValueError("region program does not match the source")
    program = _program if _program is not None else _Program(source)
    tokens = program.tokens
    chosen = [i for i, token in enumerate(tokens) if start <= token.line <= end]
    record = next((r for r in program.records if chosen and r["bodyStart"] < chosen[0] <= chosen[-1] < r["bodyEnd"]), None)
    language = source_language(source)
    result: dict[str, Any] = {"schema": REGION_SCHEMA, "sourceFormat": language,
        "organizationCapabilities": organization_capabilities(language), "requestedRange": {"startLine": start, "endLine": end},
        "applied": False, "semanticProof": False, "syntaxValidated": False, "requiresOriginalEvaluationPoint": True,
        "boundary": {"completeStatements": False}, "diagnostics": [],
        "publicationChecks": ["compile", "abi", "equivalence"]}
    diagnostics = result["diagnostics"]
    if record is None:
        diagnostics.append({"code": "region-function-boundary", "action": "Choose statements wholly inside one function, without its signature or closing brace."})
        return result
    result["function"] = {"name": record["name"], "startLine": record["line"], "endLine": tokens[record["bodyEnd"]].line}
    program.parse_body(record)
    first, last = chosen[0], chosen[-1]
    starts = [s for s in program.statements if s["first"] == first]
    complete = False
    for statement in starts:
        siblings = sorted((s for s in program.statements if s["parent"] == statement["parent"]), key=lambda s: s["first"])
        cursor = first
        for sibling in siblings:
            if sibling["first"] < cursor:
                continue
            if sibling["first"] != cursor:
                break
            cursor = sibling["last"] + 1
            if cursor == last + 1:
                complete = True
                break
    suggestions = sorted((s for s in program.statements if s["first"] <= first and last <= s["last"]), key=lambda s: s["last"] - s["first"])
    result["boundary"] = {"completeStatements": complete, "suggestedRanges": [
        {"startLine": tokens[s["first"]].line, "endLine": tokens[s["last"]].line, "kind": s["kind"]} for s in suggestions[:4]]}
    # A reading window may end inside the next statement. Report the usable
    # prefix and the exact unfinished tail so an Agent can carry that tail into
    # its next window without repeatedly probing arbitrary line counts. These
    # are syntax facts, not a prescribed helper boundary or batch size.
    prefixes = []
    tails = []
    for statement in starts:
        cursor = first
        prefix_last = None
        for sibling in sorted(
            (s for s in program.statements if s["parent"] == statement["parent"]),
            key=lambda s: s["first"],
        ):
            if sibling["first"] < cursor:
                continue
            if sibling["first"] != cursor or sibling["first"] > last:
                break
            if sibling["last"] > last:
                tails.append(sibling)
                break
            prefix_last = sibling["last"]
            cursor = prefix_last + 1
        if prefix_last is not None:
            prefixes.append(prefix_last)
    prefix_last = max(prefixes) if prefixes else None
    tail = min(tails or suggestions, key=lambda s: s["last"] - s["first"], default=None)
    result["boundary"].update({
        "completePrefixRange": (
            {"startLine": tokens[first].line, "endLine": tokens[prefix_last].line}
            if prefix_last is not None else None
        ),
        "carryForwardRange": (
            {"startLine": tokens[tail["first"]].line, "endLine": tokens[tail["last"]].line}
            if not complete and tail is not None else None
        ),
    })
    if not complete:
        diagnostics.append({"code": "region-partial-statement", "action": "Include the complete statement, loop header/body, if/else or do/while shown in suggestedRanges."})
    if any(line.lstrip().startswith("#") or line.rstrip().endswith("\\") for line in source.splitlines()[start - 1:end]):
        diagnostics.append({"code": "region-preprocessor", "action": "Preserve directives and macro context; this lexical analysis does not expand them."})
    conditional_lines, conditional_depth = [], 0
    for line_number, line in enumerate(source.splitlines(), 1):
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b", line)
        if not directive:
            continue
        if tokens[record["bodyStart"]].line <= line_number <= tokens[record["bodyEnd"]].line:
            conditional_lines.append(line_number)
        if line_number < start:
            conditional_depth += directive[1] in {"if", "ifdef", "ifndef"}
            conditional_depth -= directive[1] == "endif"
    if conditional_depth or conditional_lines:
        diagnostics.append({"code": "region-conditional-compilation", "lines": conditional_lines[:16],
                            "action": "Resolve the active preprocessing variant before relying on IO; declarations and references elsewhere in this function may belong to different branches."})
    macros = set(re.findall(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)", source))
    referenced_macros = sorted(macros & {tokens[i].text for i in chosen})
    if referenced_macros:
        diagnostics.append({"code": "region-macro-reference", "names": referenced_macros[:16],
                            "action": "Inspect the preprocessor definitions; lexical IO does not expand macros or establish their effects."})
    for i in chosen:
        if tokens[i].text in {"return", "discard", "break", "continue"}:
            owners = {"for", "while", "do"} if tokens[i].text == "continue" else {"for", "while", "do", "switch"}
            enclosing_control = any(s["kind"] in owners and first <= s["first"] <= i <= s["last"] <= last for s in program.statements)
            if tokens[i].text in {"return", "discard"} or not enclosing_control:
                diagnostics.append({"code": "region-control-transfer", "line": tokens[i].line, "token": tokens[i].text,
                                    "action": "Keep caller control flow at its original scope; include its owning control construct or choose another region."})
        if tokens[i].text in {"case", "default"}:
            owning_switch = any(s["kind"] == "switch" and first <= s["first"] <= i <= s["last"] <= last
                                for s in program.statements)
            if not owning_switch:
                diagnostics.append({"code": "region-control-transfer", "line": tokens[i].line, "token": tokens[i].text,
                                    "action": "Include the owning switch when moving a case/default label."})

    # Keep a declaration identity for each lexical scope, including loop locals.
    scopes = [(r["bodyStart"], r["bodyEnd"]) for r in [record]]
    scopes += [(i, close) for i, close in program.pairs.items() if program.word(i) == "{" and record["bodyStart"] < i < close < record["bodyEnd"]]
    declarations = []
    for i in range(record["bodyStart"] + 1, record["bodyEnd"]):
        if program.word(i - 1) not in {";", "{", "}", ":"} and not (program.word(i - 1) == "(" and program.word(i - 2) == "for"):
            continue
        for declaration in program.declaration(i, record["bodyEnd"]):
            containing = [scope for scope in scopes if scope[0] < declaration["index"] < scope[1]]
            scope = max(containing, key=lambda s: s[0])
            loop = next((s for s in program.statements if s["kind"] == "for" and s["first"] < declaration["index"] < program.pairs.get(s["controlIndex"] + 1, -1)), None)
            if loop:
                scope = (loop["first"], loop["last"] + 1)
            declarations.append({**declaration, "scopeStart": scope[0], "scopeEnd": scope[1], "parameter": False})
    name_index = next(i for i, t in enumerate(tokens) if t.line == record["line"] and t.column == record["column"])
    close = program.pairs[name_index + 1]
    cursor = name_index + 2
    while cursor < close:
        found = program.declaration(cursor, close)
        if found:
            declaration = found[0]
            declarations.append({**declaration, "scopeStart": record["bodyStart"], "scopeEnd": record["bodyEnd"], "parameter": True})
        depth = 0
        while cursor < close:
            if program.word(cursor) == "," and depth == 0:
                cursor += 1; break
            depth += program.word(cursor) in {"(", "[", "<"}
            depth -= program.word(cursor) in {")", "]", ">"}
            cursor += 1
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in declarations:
        by_name[d["name"]].append(d)
    for name, rows in by_name.items():
        if len({(r["scopeStart"], r["scopeEnd"]) for r in rows}) != len(rows):
            diagnostics.append({"code": "region-ambiguous-declaration", "name": name, "action": "Resolve duplicate declarations in the same lexical scope before using inferred IO."})
    declaration_indexes = {d["index"] for d in declarations}
    type_indexes = {index for d in declarations for index in range(d["typeStart"], d["typeEnd"])}
    type_names = {d["type"] for d in declarations} | set(re.findall(r"\bstruct\s+(\w+)", source))
    by_function: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in program.records:
        by_function[r["name"]].append(r)
    call_modes: dict[int, str] = {}
    calls = []
    for i in range(record["bodyStart"] + 1, record["bodyEnd"] - 1):
        name = program.word(i)
        if not _IDENT.fullmatch(name) or program.word(i + 1) != "(" or name in _CONTROL:
            continue
        call_end = program.pairs.get(i + 1)
        if call_end is None:
            continue
        groups, pending, cursor = [], [], i + 2
        while cursor < call_end:
            if program.word(cursor) == ",":
                groups.append(pending); pending = []
            else:
                pending.append(cursor)
                if program.word(cursor) in {"(", "[", "{"}:
                    close_arg = program.pairs.get(cursor, cursor)
                    pending.extend(range(cursor + 1, close_arg + 1)); cursor = close_arg
            cursor += 1
        if pending:
            groups.append(pending)
        candidates = [r for r in by_function[name] if len(r["parameters"]) == len(groups)]
        modes = [p["mode"] for p in candidates[0]["parameters"]] if len(candidates) == 1 else None
        intrinsic = not by_function[name] and program.word(i - 1) not in {".", "::", "->"} and (
            (name in _PURE_CALLS and name != "clip") or name in type_names or bool(_TYPES.fullmatch(name)) or name in _OUT_INTRINSICS)
        unknown = modes is None and not intrinsic
        if first <= i <= last:
            calls.append({"name": name, "line": tokens[i].line, "parameterModesKnown": not unknown,
                          "originalEvaluationPointRequired": True})
            if unknown:
                diagnostics.append({"code": "region-unknown-call-effects", "name": name, "line": tokens[i].line,
                                    "action": "Inspect this call's signature, macro or resource method; possible output arguments are conservatively retained."})
        for ordinal, group in enumerate(groups):
            mode = modes[ordinal] if modes is not None else _OUT_INTRINSICS.get(name, {}).get(ordinal, "inout" if unknown else "in")
            if group and mode != "in" and _IDENT.fullmatch(program.word(group[0])):
                call_modes[group[0]] = mode

    references: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    bindings = {(d["name"], d["index"]): d for d in declarations}
    for i in range(record["bodyStart"] + 1, record["bodyEnd"]):
        name = program.word(i)
        if (i in type_indexes or not _IDENT.fullmatch(name) or name in _CONTROL | _QUALIFIERS | {"true", "false", "void"}
                or name in type_names or _TYPES.fullmatch(name) or program.word(i - 1) in {".", "::"}
                or program.word(i + 1) == "("):
            continue
        if i and program.word(i - 1)[:1].isdigit() and tokens[i - 1].offset + len(program.word(i - 1)) == tokens[i].offset:
            continue  # numeric literal suffixes are not external identifiers
        available = [d for d in by_name[name] if d["index"] <= i and d["scopeStart"] < i < d["scopeEnd"]]
        d = max(available, key=lambda d: (d["scopeStart"], d["index"])) if available else None
        key = (name, d["index"] if d else -1)
        cursor = i + 1
        while program.word(cursor) in {".", "["}:
            cursor = cursor + 2 if program.word(cursor) == "." else program.pairs.get(cursor, cursor) + 1
        nxt, following = program.word(cursor), program.word(cursor + 1)
        write = nxt == "=" or (nxt in {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"} and following == "=") or nxt in {"++", "--"} or program.word(i - 1) in {"++", "--"}
        read = not (nxt == "=" and cursor == i + 1)
        if i in declaration_indexes:
            read = False
        if i in call_modes:
            write = True
            read = call_modes[i] == "inout" or cursor != i + 1
        references[key].append({"line": tokens[i].line, "column": tokens[i].column,
                               "read": read, "write": write, "declaration": i in declaration_indexes})
    io: dict[str, Any] = {"inputs": [], "inouts": [], "outputs": [], "externalReferences": [], "internalLocalCount": 0,
                          "kind": "conservative-lexical", "referenceScope": "entire-enclosing-function",
                          "externalReferencesIncludeCallees": False}
    for key, refs in references.items():
        inside = [r for r in refs if start <= r["line"] <= end]
        if not inside:
            continue
        d = bindings.get(key)
        before, after = [r for r in refs if r["line"] < start], [r for r in refs if r["line"] > end]
        row = {"name": key[0], "type": d["type"] if d else None, "qualifiers": d["qualifiers"] if d else [],
               "declarationLine": d["line"] if d else None, "array": bool(d and d["array"]),
               "readInside": any(r["read"] for r in inside), "writtenInside": any(r["write"] for r in inside),
               "references": {"beforeCount": len(before), "insideCount": len(inside), "afterCount": len(after),
                              "beforeWriteCount": sum(r["write"] for r in before),
                              "insideWriteCount": sum(r["write"] for r in inside),
                              "afterWriteCount": sum(r["write"] for r in after),
                              "firstLine": refs[0]["line"], "lastLine": refs[-1]["line"],
                              "locations": inside[:MAX_REFERENCE_LOCATIONS], "locationsTruncated": len(inside) > MAX_REFERENCE_LOCATIONS,
                              "firstAfter": after[0] if after else None, "countsComplete": True}}
        if not d:
            io["externalReferences"].append(row)
        elif start <= d["line"] <= end and not d["parameter"]:
            if after:
                io["outputs"].append(row)
            else:
                io["internalLocalCount"] += 1
        elif row["writtenInside"]:
            io["inouts"].append(row)
        elif row["readInside"]:
            io["inputs"].append(row)
    for category in ["inputs", "inouts", "outputs", "externalReferences"]:
        rows = io[category]
        io[category + "Count"] = len(rows)
        io[category + "Truncated"] = len(rows) > MAX_SYMBOL_ROWS
        io[category] = rows[:MAX_SYMBOL_ROWS]
    io["complete"] = complete and not diagnostics and not any(io[c + "Truncated"] for c in ["inputs", "inouts", "outputs", "externalReferences"])
    io["interfaceValueCount"] = sum(io[c + "Count"] for c in ["inputs", "inouts", "outputs"])
    result.update({"io": io, "calls": calls[:128], "callsTruncated": len(calls) > 128,
                   "statementCount": sum(start <= s["startLine"] and s["endLine"] <= end for s in record["statements"]),
                   "diagnostics": diagnostics[:64], "diagnosticsTruncated": len(diagnostics) > 64})
    if _extraction_only:
        # The mutating extractor needs the exact statement and lexical-binding
        # result, not the expensive presentation/physical-name projection.
        return result
    from .quality import organization_quality
    metric = next((item for item in organization_quality(source)["functions"]
                   if item["function"] == record["name"] and item["line"] == record["line"]), {})
    physical_labels = [
        *metric.get("physicalPlaceholders", []),
        *metric.get("preservedPhysicalPlaceholders", []),
    ]
    physical = defaultdict(list)
    chosen_indexes = set(chosen)
    reverse_pairs = {close: opening for opening, close in program.pairs.items()
                     if program.word(opening) == "["}
    for label in physical_labels:
        owner, separator, spelling = label.rpartition(".")
        for i in chosen:
            if tokens[i].text != (spelling if separator else label):
                continue
            if separator:
                if i < 2 or tokens[i - 1].text not in {".", "->"}:
                    continue
                owner_index = i - 2
                while owner_index >= 0 and tokens[owner_index].text == "]":
                    opening = reverse_pairs.get(owner_index)
                    if opening is None:
                        owner_index = -1
                        break
                    owner_index = opening - 1
                if owner_index < 0 or tokens[owner_index].text != owner:
                    continue
                if owner_index not in chosen_indexes:
                    continue
            elif i and tokens[i - 1].text == ".":
                continue
            physical[label].append({"line": tokens[i].line, "column": tokens[i].column})
    result["physicalPlaceholderReferences"] = [{"name": name, "referenceCount": len(refs),
        "locations": refs[:MAX_REFERENCE_LOCATIONS], "locationsTruncated": len(refs) > MAX_REFERENCE_LOCATIONS,
        "aliasState": "not-inspected", "semanticBindingProven": False} for name, refs in list(physical.items())[:64]]
    result["physicalPlaceholderReferencesTruncated"] = len(physical) > 64
    return result


__all__ = ["PHYSICAL_CBUFFER_PLACEHOLDER", "REGION_SCHEMA", "inspect_region"]
