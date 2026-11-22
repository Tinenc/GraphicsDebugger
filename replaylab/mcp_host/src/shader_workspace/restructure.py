"""Host-applied structural refactoring of decompiled HLSL.

The Agent chooses the source design and how much to edit. The optional
mechanical extractor moves a chosen computation and infers a draft interface
from data flow, re-indents, replaces the range with a call, and the caller
checks structural safety before retaining a draft. Operations are recorded in the
organization session and replayed deterministically on top of the renamed
baseline, so renames, extractions and comments compose in a fixed order.

Supported operations: ``extract`` moves complete statements into a helper;
``rewrite`` atomically replaces bounded complete statements/helper definitions,
including related callsites and evolving pure internal structs. Rewrites retain
canonical before/after tokens and exact function scopes; extractions retain code
anchors and their generated output names so later renames or removal cannot
silently target stale line numbers or re-spell an accepted helper interface.
Resource/ABI declarations, entry signatures and directives remain
protected. Unsupported or ambiguous boundaries and dependencies are refused.
The organization manager keeps a source-bound, statically checked draft;
Saving a draft needs no whole-source compile; replay replacement still requires
the current candidate and physical ABI/target checks. Strict equivalence review
is an optional validation route.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
import copy
import hashlib
import json
import re
import threading
import uuid
import zlib
from typing import Any

from .flow_graph import _function_records, _tokens
from .inspection import _Program, inspect_region
from .equivalence import (EquivalenceError, MAX_RENAMES_TOTAL, apply_renames,
                          rename_targets_can_share, split_bound_renames,
                          tokenize, validate_renames)

RESTRUCTURE_SCHEMA = "renderdoc-ultra-shader-restructure/v1"
# Diagnostic threshold only. The Agent chooses a cohesive interface and the
# compiler checks actual language limits; parameter count is not a plan gate.
MAX_REWRITE_BYTES = 64 * 1024
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue", "return",
    "discard", "true", "false", "const", "static", "in", "out", "inout", "uniform", "precise",
    "struct", "cbuffer", "register", "packoffset", "void", "row_major", "column_major", "nointerpolation",
    "linear", "centroid", "sample", "noperspective", "unroll", "loop", "branch", "flatten", "groupshared",
})
_TYPE_LIKE = re.compile(r"^(?:(?:float|half|double|int|uint|bool|min16float|min16int|min16uint|min10float|float16_t|int16_t|uint16_t|int64_t|uint64_t|dword)(?:[1-4](?:x[1-4])?)?|matrix|vector)$")
_ASSIGN_OPS = {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"}
_DECL_QUALIFIERS = frozenset({"const", "precise", "static", "row_major", "column_major"})
_FIELD_SELECTOR = re.compile(r"^(?P<owner>[A-Za-z_]\w*)\.(?P<member>[A-Za-z_]\w*)$")
_LOCAL_SELECTOR = re.compile(r"^(?P<function>[A-Za-z_]\w*)::(?P<local>[A-Za-z_]\w*)$")


class RestructureError(ValueError):
    """The requested structural edit is not provably safe."""


def _renamed_identifier(name: str, renames: Mapping[str, str], *, function: str = "") -> str:
    """Resolve one canonical identifier in its declaration scope."""

    if function:
        scoped = renames.get(function + "::" + name)
        if scoped:
            return str(scoped)
    return str(renames.get(name, name))


def _canonical_identifier(name: str, renames: Mapping[str, str], *, function: str = "") -> str:
    """Invert one current identifier only when its source identity is known."""

    candidates = []
    for selector, target in renames.items():
        if str(target) != name:
            continue
        local = _LOCAL_SELECTOR.fullmatch(str(selector))
        if local:
            if function and local.group("function") == function:
                candidates.append(local.group("local"))
        elif _IDENT.fullmatch(str(selector)):
            candidates.append(str(selector))
    return candidates[0] if len(set(candidates)) == 1 else name


def _inverse_renames(renames: Mapping[str, str], *, current_owners: bool) -> dict[str, str]:
    """Build an inverse map that retains field/local declaration selectors."""

    result: dict[str, str] = {}
    for selector, target in renames.items():
        selector, target = str(selector), str(target)
        field = _FIELD_SELECTOR.fullmatch(selector)
        local = _LOCAL_SELECTOR.fullmatch(selector)
        if field:
            owner = field.group("owner")
            if current_owners:
                owner = str(renames.get(owner, owner))
            result[owner + "." + target] = field.group("member")
        elif local:
            function = local.group("function")
            if current_owners:
                function = str(renames.get(function, function))
            result[function + "::" + target] = local.group("local")
        else:
            result[target] = selector
    return result


def _canonical_source(source: str, renames: Mapping[str, str]) -> str:
    """Invert a rename ledger against a complete source binding context."""

    if not renames:
        return source
    try:
        return apply_renames(source, _inverse_renames(renames, current_owners=True))
    except ValueError as exc:
        raise RestructureError(
            "renamed source cannot be bound back to its canonical declarations: %s" % exc
        ) from exc


def _transform_fragment_in_source(source: str, start: int, end: int, fragment: str,
                                  renames: Mapping[str, str]) -> str:
    """Transform a fragment while it is embedded in a complete source.

    Marker comments preserve the exact fragment boundary without exposing the
    rename engine to a declaration-less snippet.  They never reach the stored
    operation or proposal.
    """

    if not renames or not fragment:
        return fragment
    active: dict[str, str] = {}
    for selector, target in renames.items():
        selector = str(selector)
        field = _FIELD_SELECTOR.fullmatch(selector)
        local = _LOCAL_SELECTOR.fullmatch(selector)
        leaf = (field.group("member") if field else
                local.group("local") if local else selector)
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(leaf) + r"(?![A-Za-z0-9_])", fragment):
            active[selector] = str(target)
    if not active:
        return fragment
    lines = source.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines):
        raise RestructureError("source-bound fragment range is outside the complete source")
    newline = _newline(source)
    serial = 0
    while True:
        marker = "__BREAD_RESTRUCTURE_BIND_%d__" % serial
        if marker not in source and marker not in fragment:
            break
        serial += 1
    begin = "// " + marker + "BEGIN" + newline
    finish = "// " + marker + "END" + newline
    padded = not fragment.endswith(("\n", "\r"))
    payload = fragment + (newline if padded else "")
    probe = "".join(lines[:start - 1]) + begin + payload + finish + "".join(lines[end:])
    try:
        transformed = apply_renames(probe, active)
    except ValueError as exc:
        raise RestructureError(
            "source-bound replacement cannot be resolved in the complete source: %s" % exc
        ) from exc
    begin_at = transformed.find(begin)
    finish_at = transformed.find(finish, begin_at + len(begin))
    if begin_at < 0 or finish_at < 0:
        raise RestructureError("source-bound replacement markers were not preserved")
    result = transformed[begin_at + len(begin):finish_at]
    if padded and result.endswith(newline):
        result = result[:-len(newline)]
    return result


def _identifier_correspondence(current: str, canonical: str,
                               canonical_function: str, *,
                               current_tokens: Sequence[Any] | None = None,
                               canonical_tokens: Sequence[Any] | None = None,
                               canonical_spans: Sequence[Mapping[str, Any]] | None = None,
                               ) -> tuple[dict[str, str], dict[str, str]]:
    """Derive renamed<->canonical spellings from aligned complete functions."""

    current_tokens = current_tokens if current_tokens is not None else _tokens(current)
    canonical_tokens = canonical_tokens if canonical_tokens is not None else _tokens(canonical)
    if len(current_tokens) != len(canonical_tokens):
        raise RestructureError("current and canonical sources no longer have aligned code tokens")
    records = [record for record in (canonical_spans if canonical_spans is not None
                                    else _function_spans(canonical, tokens=canonical_tokens))
               if record["name"] == canonical_function]
    if not records:
        records = [record for record in _struct_spans(canonical, tokens=canonical_tokens)
                   if record["name"] == canonical_function]
    if len(records) != 1:
        raise RestructureError("canonical replacement has no unique function scope")
    record = records[0]
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for index in range(record["firstToken"], record["bodyEnd"] + 1):
        old, new = canonical_tokens[index].text, current_tokens[index].text
        if old == new or not (_IDENT.fullmatch(old) and _IDENT.fullmatch(new)):
            continue
        if old in forward and forward[old] != new:
            raise RestructureError("one canonical identifier has conflicting renamed spellings in its function")
        if new in reverse and reverse[new] != old:
            raise RestructureError("one renamed identifier has conflicting canonical spellings in its function")
        forward[old], reverse[new] = new, old
    return forward, reverse


def _substitute_fragment_identifiers(fragment: str, names: Mapping[str, str]) -> str:
    """Apply an already-bound spelling correspondence to code tokens only."""

    if not names:
        return fragment
    tokens = tokenize(fragment)
    result: list[str] = []
    for index, token in enumerate(tokens):
        text = token["text"]
        previous = index - 1
        while previous >= 0 and tokens[previous]["kind"] in {"ws", "comment"}:
            previous -= 1
        swizzle = (previous >= 0 and tokens[previous]["text"] in {".", "->"} and
                   re.fullmatch(r"(?:[xyzw]{1,4}|[rgba]{1,4})", text))
        if token["kind"] == "ident" and not swizzle:
            text = str(names.get(text, text))
        result.append(text)
    return "".join(result)


def _plain_rename_correspondence(names: Mapping[str, str],
                                 renames: Mapping[str, str], *,
                                 reverse: bool = False) -> dict[str, str]:
    """Keep aligned spellings only for a previously accepted plain selector.

    Explicit field/local selectors can be rebound safely in the complete probe.
    Applying their spelling correspondence to an arbitrary replacement would
    also rename a newly introduced field that merely shares the parameter's
    spelling.
    """

    plain = {str(selector) for selector in renames if _IDENT.fullmatch(str(selector))}
    if reverse:
        return {current: canonical for current, canonical in names.items()
                if canonical in plain}
    return {canonical: current for canonical, current in names.items()
            if canonical in plain}


def _function_spans(source: str, *, tokens: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    tokens = tokens if tokens is not None else _tokens(source)
    result = []
    for record in _function_records(source):
        first = record["bodyStart"] - 1
        while first > 0 and tokens[first - 1].text not in {";", "}"}:
            first -= 1
        result.append({**record, "startLine": tokens[first].line,
                       "endLine": tokens[record["bodyEnd"]].line,
                       "firstToken": first})
    return result


def _struct_spans(source: str, *, tokens: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Top-level complete structs; edit permission is checked against ABI uses."""
    tokens = tokens if tokens is not None else _tokens(source)
    result, depth, index = [], 0, 0
    while index < len(tokens):
        if (depth == 0 and index + 2 < len(tokens) and tokens[index].text == "struct"
                and _IDENT.fullmatch(tokens[index + 1].text) and tokens[index + 2].text == "{"):
            end = _matching(tokens, index + 2, "{", "}")
            if end > index and end + 1 < len(tokens) and tokens[end + 1].text == ";":
                result.append({"name": tokens[index + 1].text,
                               "firstToken": index, "bodyStart": index + 2,
                               "bodyEnd": end, "lastToken": end + 1,
                               "startLine": tokens[index].line,
                               "endLine": tokens[end + 1].line})
                index = end + 2
                continue
        depth += (1 if tokens[index].text == "{" else -1 if tokens[index].text == "}" else 0)
        index += 1
    return result


def _range_scope(source: str, start: int, end: int) -> dict[str, Any]:
    """Accept complete internal definitions or in-body statement ranges."""
    lines, tokens = source.splitlines(), _tokens(source)
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        raise RestructureError("rewrite line range is outside the current proposal")
    selected = [i for i, t in enumerate(tokens) if start <= t.line <= end]
    if not selected:
        raise RestructureError("rewrite must select complete code, not comments or whitespace")
    for record in _struct_spans(source, tokens=tokens):
        if start == record["startLine"] and end == record["endLine"]:
            if selected[0] != record["firstToken"] or selected[-1] != record["lastToken"]:
                raise RestructureError("struct boundary shares a line with other code")
            return {"function": record["name"], "kind": "struct"}
    for record in _function_spans(source):
        if start == record["startLine"] and end == record["endLine"]:
            if selected[0] != record["firstToken"] or selected[-1] != record["bodyEnd"]:
                raise RestructureError("function boundary shares a line with protected code")
            return {"function": record["name"], "kind": "function"}
        if tokens[record["bodyStart"]].line < start <= end < record["endLine"]:
            before = tokens[selected[0] - 1].text
            after = tokens[selected[-1] + 1].text
            if before not in {";", "{", "}"} or tokens[selected[-1]].text not in {";", "}"}:
                raise RestructureError("rewrite range splits a statement")
            if tokens[selected[0]].text == "else" or after == "else":
                raise RestructureError("rewrite range splits an if/else")
            stack = []
            pairs = {"}": "{", ")": "(", "]": "["}
            for i in selected:
                word = tokens[i].text
                if word in {"{", "(", "["}:
                    stack.append(word)
                elif word in pairs:
                    if not stack or stack.pop() != pairs[word]:
                        raise RestructureError("rewrite range splits a block/expression")
            if stack:
                raise RestructureError("rewrite range splits a block/expression")
            return {"function": record["name"], "kind": "statements"}
    raise RestructureError("rewrite cannot alter global declarations or a partial function signature")


def _protected_code(source: str, entry_point: str) -> list[str]:
    tokens = _tokens(source)
    excluded = set()
    for record in _function_spans(source, tokens=tokens):
        # Entry signature is part of the captured interface, helper signatures
        # are internal and may change atomically with every callsite.
        first = record["bodyStart"] if record["name"] == entry_point else record["firstToken"]
        excluded.update(range(first, record["bodyEnd"] + 1))
    return [t.text for i, t in enumerate(tokens) if i not in excluded]


def _check_protected(before: str, after: str, entry_point: str) -> None:
    original, changed = _protected_code(before, entry_point), _protected_code(after, entry_point)
    if original == changed:
        return
    # Internal computation types can evolve with their callers. A type used by
    # the entry signature, resources or globals remains part of that protected
    # declaration, including nested types. No readability score lives here.
    original_names = {t.text for t in _tokens(before)}
    def structures(words):
        rows, index = [], 0
        while index + 3 < len(words):
            if words[index] != "struct" or words[index + 2] != "{":
                index += 1
                continue
            end = index + 3
            while end < len(words) and words[end] != "}":
                end += 1
            if end + 1 < len(words) and words[end + 1] == ";":
                rows.append((index, end + 2, words[index + 1], words[index + 3:end]))
            index = end + 1
        return rows
    def pure_names(rows):
        names = set()
        for _ in rows:
            previous = len(names)
            for unused_start, unused_end, name, fields in rows:
                if bool(fields) and len(fields) % 3 == 0 and all(
                    (_TYPE_LIKE.fullmatch(fields[i]) or fields[i] in names)
                    and _IDENT.fullmatch(fields[i + 1]) and fields[i + 2] == ";"
                    for i in range(0, len(fields), 3)):
                    names.add(name)
            if len(names) == previous:
                break
        return names
    original_rows, changed_rows = structures(original), structures(changed)
    original_pure, changed_pure = pure_names(original_rows), pure_names(changed_rows)
    pure_tokens = {i for start, end, name, fields in original_rows
                   if name in original_pure for i in range(start, end)}
    protected_types = original_pure.intersection(
        w for i, w in enumerate(original) if i not in pure_tokens)
    for _ in original_rows:
        previous = len(protected_types)
        for unused_start, unused_end, name, fields in original_rows:
            if name in protected_types:
                protected_types.update(original_pure.intersection(fields))
        if len(protected_types) == previous:
            break
    internal_before = original_pure - protected_types
    internal_after = {name for name in changed_pure if name in internal_before
                      or name not in original_names}
    remove_before = {i for start, end, name, fields in original_rows
                     if name in internal_before for i in range(start, end)}
    remove_after = {i for start, end, name, fields in changed_rows
                    if name in internal_after for i in range(start, end)}
    if ([w for i, w in enumerate(changed) if i not in remove_after]
            != [w for i, w in enumerate(original) if i not in remove_before]):
        raise RestructureError("rewrite changes protected declarations/entry signature or inserts non-computational globals")


def source_bound_anchor(source: str, start: int, end: int, *,
                        renames: Mapping[str, str],
                        canonical_source: str | None = None) -> dict[str, Any]:
    """Create a canonical anchor from a range in the complete current source."""

    current_scope = _range_scope(source, start, end)
    canonical = canonical_source if canonical_source is not None else _canonical_source(source, renames)
    canonical_scope = _range_scope(canonical, start, end)
    if current_scope["kind"] != canonical_scope["kind"]:
        raise RestructureError("current and canonical source ranges have different structural scopes")
    before = "".join(canonical.splitlines(keepends=True)[start - 1:end])
    return {
        "before": before,
        "scope": canonical_scope["function"],
        "kind": canonical_scope["kind"],
    }


def _has_directive(source: str) -> bool:
    # HLSL treats comments as whitespace before directive recognition. Mask
    # literals too, while retaining line breaks for a '#' on a later line.
    masked = list(source)
    for token in tokenize(source):
        if token["kind"] in {"comment", "string"}:
            for index in range(token["start"], token["end"]):
                if source[index] not in "\r\n":
                    masked[index] = " "
    return re.search(r"(?m)^[ \t\r]*#", "".join(masked)) is not None


def prepare_rewrite(source: str, edits: Sequence[Mapping[str, Any]], *,
                    renames: Mapping[str, str], entry_point: str, name: str = "",
                    canonical_source: str | None = None) -> dict[str, Any]:
    if not isinstance(edits, list) or not 1 <= len(edits) <= 16:
        raise RestructureError("rewrite requires 1..16 complete statement/function/struct edits")
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        name = "Rewrite_" + uuid.uuid4().hex
    lines, stored, intervals = source.splitlines(keepends=True), [], []
    canonical = canonical_source if canonical_source is not None else _canonical_source(source, renames)
    if len(canonical.splitlines()) != len(source.splitlines()):
        raise RestructureError("current and canonical source no longer have aligned line ranges")
    total_bytes = total_lines = 0
    for edit in edits:
        if not isinstance(edit, Mapping):
            raise RestructureError("each rewrite edit must be an object")
        start, end, replacement = edit.get("startLine"), edit.get("endLine"), edit.get("replacement")
        scope = _range_scope(source, start, end)
        if not isinstance(replacement, str) or "\x00" in replacement:
            raise RestructureError("rewrite replacement must be UTF-8 source text without NUL")
        before = "".join(lines[start - 1:end])
        if _has_directive(before) or _has_directive(replacement):
            raise RestructureError("rewrite cannot modify or insert preprocessor directives")
        if scope["kind"] == "statements" and replacement.strip():
            wrapped = "void rewrite_scope()\n{\n" + replacement.rstrip() + "\n}\n"
            _range_scope(wrapped, 3, len(wrapped.splitlines()) - 1)
        total_bytes += len(before.encode("utf-8")) + len(replacement.encode("utf-8"))
        total_lines += max(end - start + 1, len(replacement.splitlines()))
        if total_bytes > MAX_REWRITE_BYTES or total_lines > 400:
            raise RestructureError("rewrite exceeds 64 KiB before+after or 400 affected source lines")
        if any(start <= prior_end and end >= prior_start for prior_start, prior_end in intervals):
            raise RestructureError("rewrite edits overlap")
        intervals.append((start, end))
        anchor = source_bound_anchor(
            source, start, end, renames=renames, canonical_source=canonical
        )
        unused_forward, reverse_names = _identifier_correspondence(
            source, canonical, str(anchor["scope"])
        )
        reverse_names = _plain_rename_correspondence(
            reverse_names, renames, reverse=True
        )
        canonical_replacement = _substitute_fragment_identifiers(
            replacement, reverse_names
        )
        inverse = _inverse_renames(renames, current_owners=True)
        canonical_replacement = _transform_fragment_in_source(
            source, start, end, canonical_replacement, inverse
        )
        stored.append({**anchor, "after": canonical_replacement,
                       "sourceLines": [start, end]})
    canonical_entry = _canonical_identifier(entry_point, renames)
    operation = {"operation": "rewrite", "name": name, "edits": stored,
                 "entryPoint": canonical_entry}
    rewritten = _apply_rewrite(source, operation, renames, canonical_source=canonical)
    _check_protected(source, rewritten, _renamed_identifier(canonical_entry, renames))
    return operation


def _locate(source: str, anchor: Mapping[str, Any], renames: Mapping[str, str], *,
            analysis=None, canonical_source: str | None = None) -> tuple[int, int]:
    """Locate canonical anchor tokens using one complete source binding."""

    canonical = canonical_source if canonical_source is not None else _canonical_source(source, renames)
    wanted = [t.text for t in _tokens(str(anchor["before"]))]
    tokens, spans = analysis if analysis is not None else (
        _tokens(canonical), (_struct_spans(canonical) if anchor.get("kind") == "struct"
                             else _function_spans(canonical))
    )
    scope = str(anchor["scope"])
    if anchor.get("kind") == "struct":
        spans = _struct_spans(canonical, tokens=tokens)
    records = [r for r in spans if r["name"] == scope]
    if len(records) != 1 or not wanted:
        raise RestructureError("rewrite/extract anchor has no unique function scope")
    record = records[0]
    begin = record["firstToken"] if anchor.get("kind") in {"function", "struct"} else record["bodyStart"] + 1
    finish = record.get("lastToken", record["bodyEnd"]) + 1
    matches = []
    # Linear token matching avoids source-size * edit-size work for large MainPS.
    failure = [0] * len(wanted)
    matched = 0
    for i in range(1, len(wanted)):
        while matched and wanted[i] != wanted[matched]:
            matched = failure[matched - 1]
        if wanted[i] == wanted[matched]:
            matched += 1
        failure[i] = matched
    matched = 0
    for i in range(begin, finish):
        while matched and tokens[i].text != wanted[matched]:
            matched = failure[matched - 1]
        if tokens[i].text == wanted[matched]:
            matched += 1
        if matched == len(wanted):
            matches.append((tokens[i - matched + 1].line, tokens[i].line))
            matched = failure[matched - 1]
    if len(matches) != 1:
        raise RestructureError("rewrite/extract anchor is missing or ambiguous; dependent operations must be removed first")
    start, end = matches[0]
    selected = [t.text for t in tokens if start <= t.line <= end]
    if selected != wanted:
        raise RestructureError("rewrite/extract anchor shares a line with other code")
    return start, end


def _apply_rewrite(source: str, operation: Mapping[str, Any], renames: Mapping[str, str], *,
                   canonical_source: str | None = None) -> str:
    replacements = []
    canonical = canonical_source if canonical_source is not None else _canonical_source(source, renames)
    canonical_tokens = _tokens(canonical)
    canonical_spans = _function_spans(canonical, tokens=canonical_tokens)
    analysis = (canonical_tokens, canonical_spans)
    current_tokens = _tokens(source) if renames else canonical_tokens
    correspondences: dict[str, dict[str, str]] = {}
    for edit in operation["edits"]:
        start, end = _locate(
            source, edit, renames, analysis=analysis, canonical_source=canonical
        )
        scope = str(edit["scope"])
        if renames and scope not in correspondences:
            forward_names, unused_reverse = _identifier_correspondence(
                source, canonical, scope,
                current_tokens=current_tokens,
                canonical_tokens=canonical_tokens,
                canonical_spans=canonical_spans,
            )
            correspondences[scope] = _plain_rename_correspondence(forward_names, renames)
        forward_names = correspondences.get(scope, {})
        after = _substitute_fragment_identifiers(str(edit["after"]), forward_names)
        after = _transform_fragment_in_source(
            canonical, start, end, after, renames
        )
        replacements.append((start, end, after))
    lines, newline = source.splitlines(keepends=True), _newline(source)
    for start, end, after in sorted(replacements, reverse=True):
        replacement = newline.join(after.splitlines())
        if replacement and lines[end - 1].endswith("\n"):
            replacement += newline
        lines[start - 1:end] = [replacement] if replacement else []
    return "".join(lines)


def anchor_extractions(source: str, operations: Sequence[Mapping[str, Any]], renames: Mapping[str, str]) -> list[dict[str, Any]]:
    """Upgrade old line ledgers on their exact historical input before editing."""
    text, canonical, result = source, _canonical_source(source, renames), []
    for item in operations:
        operation = dict(item)
        if operation.get("operation") == "extract" and "anchor" not in operation:
            start, end = operation["startLine"], operation["endLine"]
            operation["anchor"] = source_bound_anchor(
                text, start, end, renames=renames, canonical_source=canonical
            )
        text, receipts = apply_operations(
            text, [operation], renames=renames, _canonical_source_override=canonical
        )
        operation = retain_extraction_names([operation], receipts, renames)[0]
        # Seal the names emitted by the accepted/current rendering before the
        # canonical shadow advances.  A legacy extraction otherwise invents
        # different ``replaylab_out_*`` identifiers, so a later whole-helper
        # rewrite cannot find its canonical anchor.
        canonical, unused = apply_operations(canonical, [operation], renames={})
        result.append(operation)
    return result


def retain_extraction_names(operations: Sequence[Mapping[str, Any]], receipts: Sequence[Mapping[str, Any]],
                            renames: Mapping[str, str]) -> list[dict[str, Any]]:
    """Seal generated names from an exact accepted render, before changing its rename map.

    A generated ``replaylab_out_<variable>`` is a distinct identifier, not a
    prefix substitution. Re-inferring it after renaming the variable would
    invalidate later whole-helper rewrite anchors even when the code is valid.
    Legacy operations are upgraded in memory under their old accepted names;
    the manager persists this metadata only with a successful transaction.
    """
    result = []
    for operation, receipt in zip(operations, receipts, strict=True):
        item = dict(operation)
        if item.get("operation") == "extract" and "generatedNames" not in item:
            scope = str((item.get("anchor") or {}).get("scope", ""))
            item["generatedNames"] = {
                _canonical_identifier(variable, renames, function=scope): {
                    role: _canonical_identifier(
                        value, renames,
                        function=(str(item.get("name", "")) if role in {"parameter", "local"} else scope),
                    )
                    for role, value in names.items()
                }
                for variable, names in receipt["generatedNames"].items()}
        result.append(item)
    return result


def _newline(source: str) -> str:
    return "\r\n" if "\r\n" in source else "\n"


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _matching(tokens: Sequence[Any], open_index: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(open_index, len(tokens)):
        text = tokens[index].text
        if text == opening:
            depth += 1
        elif text == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _declarations(tokens: Sequence[Any], start: int, end: int) -> dict[str, dict[str, Any]]:
    """Local declarations ``TYPE name`` inside one function body (token index range)."""

    found: dict[str, dict[str, Any]] = {}
    index = start + 1
    while index < end:
        token = tokens[index]
        previous = tokens[index - 1].text if index > 0 else ""
        statement_start = previous in {";", "{", "}"} or (previous == "(" and index >= 2 and tokens[index - 2].text == "for")
        type_start = index
        qualifiers: list[str] = []
        if statement_start:
            while type_start < end and tokens[type_start].text in _DECL_QUALIFIERS:
                qualifiers.append(tokens[type_start].text)
                type_start += 1
        if (statement_start and type_start < end and _IDENT.fullmatch(tokens[type_start].text)
                and tokens[type_start].text not in _KEYWORDS):
            type_end = type_start
            if type_start + 1 < end and tokens[type_start + 1].text == "<":
                close = _matching(tokens, type_start + 1, "<", ">")
                if close > 0:
                    type_end = close
            cursor = type_end + 1
            if cursor < end and _IDENT.fullmatch(tokens[cursor].text) and tokens[cursor].text not in _KEYWORDS:
                after = tokens[cursor + 1].text if cursor + 1 < end else ""
                if after in {"=", ";", ","} or after == "[":
                    type_text = "".join(t.text for t in tokens[type_start:type_end + 1])
                    is_array = after == "["
                    name = tokens[cursor].text
                    found.setdefault(name, {"type": type_text, "qualifiers": qualifiers, "line": tokens[cursor].line, "array": is_array})
                    # ``float a = 1.0f, b = 2.0f;``
                    scan = cursor + 1
                    while scan < end and tokens[scan].text != ";":
                        if tokens[scan].text == "," and scan + 1 < end and _IDENT.fullmatch(tokens[scan + 1].text):
                            array = scan + 2 < end and tokens[scan + 2].text == "["
                            found.setdefault(tokens[scan + 1].text, {"type": type_text, "qualifiers": qualifiers, "line": tokens[scan + 1].line, "array": array})
                        if tokens[scan].text in {"(", "["}:
                            close = _matching(tokens, scan, tokens[scan].text, ")" if tokens[scan].text == "(" else "]")
                            scan = close if close > scan else scan
                        scan += 1
                    index = cursor + 1
                    continue
        index += 1
    return found


def _parameter_types(source_tokens: Sequence[Any], record: Mapping[str, Any], tokens: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Parameter name -> {type, mode} of the enclosing function signature."""

    # find '(' after the name token
    name_index = next((i for i, t in enumerate(tokens) if t.line == record["line"] and t.column == record["column"]), None)
    result: dict[str, dict[str, Any]] = {}
    if name_index is None or name_index + 1 >= len(tokens) or tokens[name_index + 1].text != "(":
        return result
    close = _matching(tokens, name_index + 1, "(", ")")
    group: list[str] = []
    depth = 0
    for token in tokens[name_index + 2:close]:
        if token.text in {"(", "<", "["}:
            depth += 1
        elif token.text in {")", ">", "]"}:
            depth -= 1
        if token.text == "," and depth == 0:
            _record_parameter(group, result)
            group = []
        else:
            group.append(token.text)
    _record_parameter(group, result)
    return result


def _record_parameter(words: list[str], result: dict[str, dict[str, Any]]) -> None:
    words = list(words)
    if ":" in words:
        words = words[: words.index(":")]
    if "=" in words:
        words = words[: words.index("=")]
    mode = "inout" if "inout" in words else "out" if "out" in words else "in"
    qualifiers = [w for w in words if w in _DECL_QUALIFIERS]
    words = [w for w in words if w not in _DECL_QUALIFIERS | {"in", "out", "inout", "uniform"}]
    identifiers = [w for w in words if _IDENT.fullmatch(w)]
    if len(identifiers) < 2:
        return
    name = identifiers[-1]
    type_text = "".join(words[: len(words) - 1]) if words and words[-1] == name else "".join(words[:-1])
    result[name] = {"type": type_text, "mode": mode, "qualifiers": qualifiers}


def _qualified_type(declaration: Mapping[str, Any], *, mutable: bool = False) -> str:
    qualifiers = [q for q in declaration.get("qualifiers", []) if not (mutable and q == "const")]
    return " ".join(qualifiers + [declaration["type"]])


def extract_function(source: str, start_line: int, end_line: int, name: str, *, comment: str = "",
                     generated_names: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Extract lines ``start_line..end_line`` (1-based, inclusive) into ``name``."""

    if not isinstance(name, str) or _IDENT.fullmatch(name) is None or name in _KEYWORDS or _TYPE_LIKE.fullmatch(name):
        raise RestructureError("function name must be a valid HLSL identifier")
    if re.search(r"\b%s\b" % re.escape(name), source):
        raise RestructureError("function name %r already exists in the source; pick another" % name)
    # Host-repaired sources can mix "\n" (Host declarations) with "\r\n" (SPIRV-Cross body).
    # Index lines exactly like the tokenizer does (every "\n" ends a line) and re-join with the
    # dominant newline; splitting on "\r\n" alone shifted every line number and moved code.
    newline = _newline(source)
    lines = source.split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    trailing_newline = source.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]
    if not (1 <= start_line <= end_line <= len(lines)):
        raise RestructureError("line range %d-%d is outside the file (%d lines)" % (start_line, end_line, len(lines)))
    program = _Program(source)
    tokens, records = program.tokens, program.records
    enclosing = None
    for record in records:
        first = tokens[record["bodyStart"]].line
        last = tokens[record["bodyEnd"]].line
        if first < start_line and end_line < last:
            if enclosing is None or record["bodyStart"] > enclosing["bodyStart"]:
                enclosing = record
    if enclosing is None:
        raise RestructureError("the range must lie strictly inside one function body (not including its braces)")
    body_start, body_end = enclosing["bodyStart"], enclosing["bodyEnd"]
    body_tokens = list(range(body_start + 1, body_end))
    range_indices = [i for i in body_tokens if start_line <= tokens[i].line <= end_line]
    if not range_indices:
        raise RestructureError("the range contains no code")
    before_indices = [i for i in body_tokens if tokens[i].line < start_line]
    after_indices = [i for i in body_tokens if tokens[i].line > end_line]

    # A balanced brace count does not imply a complete C-family statement:
    # ``do { ... }`` still owns its trailing ``while``, and a nested if/else
    # must move as one statement. The read-only region planner already tracks
    # these syntax boundaries and binds same-spelled locals by lexical scope.
    plan = inspect_region(source, start_line, end_line, _extraction_only=True,
                          _program=program)
    if not plan["boundary"]["completeStatements"]:
        suggested = plan["boundary"].get("suggestedRanges", [])
        hint = ("; include %d-%d (%s)" % (
            suggested[0]["startLine"], suggested[0]["endLine"], suggested[0]["kind"]
        )) if suggested else ""
        raise RestructureError("the range splits a complete branch, loop or statement%s" % hint)
    blocking = [item for item in plan["diagnostics"] if item["code"] not in {"region-unknown-call-effects"}]
    if blocking or plan["diagnosticsTruncated"]:
        if blocking and blocking[0]["code"] == "region-control-transfer":
            item = blocking[0]
            raise RestructureError("the range contains %r at line %d; include its owning control flow or choose another range"
                                   % (item["token"], item["line"]))
        problem = blocking[0]["code"] if blocking else "region-diagnostics-truncated"
        raise RestructureError("the extraction boundary or variable scope is ambiguous (%s)" % problem)
    if any(plan["io"][category + "Truncated"] for category in ("inputs", "inouts", "outputs")):
        raise RestructureError("the extraction interface is too large to validate without truncated symbols")

    # ---- statement boundaries and block balance
    depth = 0
    min_depth = 0
    for i in range_indices:
        text = tokens[i].text
        if text == "{":
            depth += 1
        elif text == "}":
            depth -= 1
            min_depth = min(min_depth, depth)
    if depth != 0 or min_depth < 0:
        raise RestructureError("the range must contain complete blocks: braces opened and closed inside it")
    previous_text = tokens[before_indices[-1]].text if before_indices else "{"
    if previous_text not in {";", "{", "}"}:
        raise RestructureError("the range starts in the middle of a statement (line %d); start at a statement boundary"
                               % start_line)
    first_text = tokens[range_indices[0]].text
    last_text = tokens[range_indices[-1]].text
    if first_text == "else":
        raise RestructureError("the range starts with 'else'; include the matching 'if' or start after the else block")
    if last_text not in {";", "}"}:
        raise RestructureError("the range ends in the middle of a statement (line %d); end at ';' or '}'" % end_line)
    next_text = tokens[after_indices[0]].text if after_indices else "}"
    if next_text == "else":
        raise RestructureError("the range ends just before an 'else'; include the else block or stop before the 'if'")
    # a '{' as the previous token that belongs to an if/for header we split
    if previous_text == "{" and before_indices:
        # find the token before the '{': ')' means we are the first statement of a block – allowed
        pass

    # ---- data flow: use declaration identities, not one dictionary slot per
    # spelling across the entire function. Two branches may both declare ``v``;
    # neither one escapes into the other branch or the enclosing scope.
    interface = plan["io"]
    inputs = [row["name"] for row in interface["inputs"]]
    inouts = [row["name"] for row in interface["inouts"]]
    outputs = [row["name"] for row in interface["outputs"]]
    interface_rows = [row for category in ("inputs", "inouts", "outputs") for row in interface[category]]
    if len({row["name"] for row in interface_rows}) != len(interface_rows):
        raise RestructureError("the range uses shadowed same-named locals across its helper interface; choose a smaller scope")
    declarations = {}
    for row in interface_rows:
        if not row.get("type"):
            raise RestructureError("local %r has no proven declaration type at this boundary" % row["name"])
        declarations[row["name"]] = {
            "type": row["type"], "qualifiers": row["qualifiers"],
            "line": row["declarationLine"], "array": row["array"],
        }
    for var in inputs + inouts + outputs:
        if declarations[var].get("array"):
            raise RestructureError("local array %r crosses the range boundary; arrays cannot be passed by the extractor" % var)
        if "static" in declarations[var].get("qualifiers", []):
            raise RestructureError("static local %r crosses the range boundary; choose a range that keeps its storage in one function" % var)
        if var in inouts and "const" in declarations[var].get("qualifiers", []):
            raise RestructureError("const variable %r may be written in the range; choose a range with a provably read-only input" % var)
    return_var = outputs[0] if len(outputs) == 1 else None
    out_params = [] if return_var else outputs
    occupied_names = {t.text for t in tokens if _IDENT.fullmatch(t.text)}

    def fresh_name(prefix: str, variable: str) -> str:
        base = prefix + variable
        candidate, suffix = base, 1
        while candidate in occupied_names:
            candidate = "%s_%d" % (base, suffix)
            suffix += 1
        occupied_names.add(candidate)
        return candidate

    # Keep the original declarations (including const/precise and comma lists)
    # inside the helper. Output parameters must have distinct names, otherwise
    # the retained declarations redeclare those parameters and do not compile.
    # Helper body locals for those outs also get distinct ``replaylab_local_*``
    # names so a later rename ledger cannot make ``Function::local`` ambiguous
    # (local + out-parameter sharing the decompiled id in one function).
    if generated_names is not None and set(generated_names) != set(out_params):
        raise RestructureError("the extraction output interface changed; its accepted generated names cannot be reused")

    def retained_name(variable: str, role: str, prefix: str) -> str:
        if generated_names is None:
            return fresh_name(prefix, variable)
        candidate = str(generated_names[variable].get(role, "") or "")
        if not candidate:
            # Legacy sealed ops omit ``local``; keep the decompiled spelling.
            if role == "local":
                return variable
            return fresh_name(prefix, variable)
        if (not _IDENT.fullmatch(candidate) or candidate in _KEYWORDS or _TYPE_LIKE.fullmatch(candidate)
                or candidate in occupied_names):
            # Prefer a fresh reference over aborting a sealed replay when the
            # retained pipe name collided with a later rename.
            return fresh_name(prefix, variable)
        occupied_names.add(candidate)
        return candidate

    output_names = {v: retained_name(v, "parameter", "replaylab_out_") for v in out_params}
    output_args = {v: (retained_name(v, "argument", "replaylab_value_")
                      if "const" in declarations[v].get("qualifiers", []) else v) for v in out_params}
    local_names = {v: retained_name(v, "local", "replaylab_local_") for v in out_params}
    for variable, local in local_names.items():
        if local != variable and local == output_names.get(variable):
            local_names[variable] = fresh_name("replaylab_local_", variable)
    if generated_names is not None:
        for variable in out_params:
            sealed_argument = generated_names[variable].get("argument")
            if sealed_argument in (None, ""):
                continue
            if sealed_argument != output_args[variable]:
                raise RestructureError(
                    "the extraction's retained output argument no longer matches its declaration"
                )
    signature_parts = ["%s %s" % (_qualified_type(declarations[v]), v) for v in inputs]
    signature_parts += ["inout %s %s" % (_qualified_type(declarations[v]), v) for v in inouts]
    signature_parts += ["out %s %s" % (_qualified_type(declarations[v], mutable=True), output_names[v]) for v in out_params]
    return_type = _qualified_type(declarations[return_var], mutable=True) if return_var else "void"

    base_indent = _indent_of(lines[start_line - 1])
    body_lines: list[str] = []
    for raw in lines[start_line - 1:end_line]:
        stripped = raw[len(base_indent):] if raw.startswith(base_indent) else raw.lstrip()
        body_lines.append(("    " + stripped) if stripped.strip() else "")
    local_rewrites = {old: new for old, new in local_names.items() if old != new}
    if local_rewrites:
        wrapped = "void __replaylab_extract_body()\n{\n" + "\n".join(body_lines) + "\n}\n"
        selectors = {
            "__replaylab_extract_body::%s" % old: new for old, new in local_rewrites.items()
        }
        rewritten = apply_renames(wrapped, selectors)
        # Identifier renames preserve line boundaries. Remove only the synthetic
        # signature and outer braces, retaining every nested block in the body.
        body_lines = rewritten.splitlines()[2:-1]
    function_lines: list[str] = []
    if comment.strip():
        for text in comment.strip().splitlines()[:6]:
            function_lines.append("// " + text.strip())
    function_lines.append("%s %s(%s)" % (return_type, name, ", ".join(signature_parts)))
    function_lines.append("{")
    function_lines.extend(body_lines)
    if return_var:
        function_lines.append("    return %s;" % return_var)
    for var in out_params:
        function_lines.append("    %s = %s;" % (output_names[var], local_names[var]))
    function_lines.append("}")
    function_lines.append("")

    call_args = ", ".join(inputs + inouts + [output_args[v] for v in out_params])
    replacement: list[str] = []
    for var in out_params:
        replacement.append("%s%s %s;" % (base_indent, _qualified_type(declarations[var], mutable=True), output_args[var]))
    if return_var:
        replacement.append("%s%s %s = %s(%s);" % (base_indent, _qualified_type(declarations[return_var]), return_var, name, call_args))
    else:
        replacement.append("%s%s(%s);" % (base_indent, name, call_args))
    for var in out_params:
        if output_args[var] != var:
            replacement.append("%s%s %s = %s;" % (base_indent, _qualified_type(declarations[var]), var, output_args[var]))

    enclosing_line = enclosing["line"]
    # keep attached comment lines above the enclosing function with it
    insert_at = enclosing_line - 1
    while insert_at > 0 and lines[insert_at - 1].lstrip().startswith("//"):
        insert_at -= 1
    new_lines = lines[:insert_at] + function_lines + lines[insert_at:start_line - 1] + replacement + lines[end_line:]
    added = len(function_lines)
    info = {
        "schema": RESTRUCTURE_SCHEMA,
        "operation": "extract",
        "name": name,
        "enclosingFunction": enclosing["name"],
        "sourceLines": [start_line, end_line],
        "extractedLines": end_line - start_line + 1,
        "returnType": return_type,
        "returns": return_var,
        "inputs": inputs,
        "inouts": inouts,
        "outParams": out_params,
        "parameterCount": len(inputs) + len(inouts) + len(out_params),
        "interface": {
            "inputCount": len(inputs),
            "inoutCount": len(inouts),
            "outputCount": len(out_params) + (1 if return_var else 0),
        },
        "generatedNames": {
            v: {
                "parameter": output_names[v],
                "argument": output_args[v],
                "local": local_names[v],
            }
            for v in out_params
        },
        "insertedAtLine": insert_at + 1,
        "newFunctionLines": [insert_at + 1, insert_at + added - 1],
        "callLine": start_line + added,
    }
    rebuilt = newline.join(new_lines) + (newline if trailing_newline else "")
    return {"source": rebuilt, "info": info}


def apply_operations(source: str, operations: Sequence[Mapping[str, Any]], *,
                     renames: Mapping[str, str] | None = None,
                     _canonical_source_override: str | None = None,
                     _canonical_after_override: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Replay recorded operations in order; raises RestructureError on the first failure."""

    receipts: list[dict[str, Any]] = []
    text = source
    renames = renames or {}
    canonical = (_canonical_source_override if _canonical_source_override is not None
                 else _canonical_source(source, renames))
    if _canonical_after_override is not None and len(operations) != 1:
        raise RestructureError("a prevalidated canonical stage requires one operation")
    if len(canonical.splitlines()) != len(source.splitlines()):
        raise RestructureError("current and canonical structural sources have different line ranges")
    for position, operation in enumerate(operations):
        kind = str(operation.get("operation", ""))
        if kind == "extract":
            start, end = (_locate(
                              text, operation["anchor"], renames,
                              canonical_source=canonical,
                          ) if "anchor" in operation else
                          (int(operation["startLine"]), int(operation["endLine"])))
            canonical_name = str(operation["name"])
            current_name = _renamed_identifier(canonical_name, renames)
            scope = str((operation.get("anchor") or {}).get("scope", ""))
            generated = operation.get("generatedNames")
            result = extract_function(
                text,
                start, end,
                current_name,
                comment=str(operation.get("comment", "") or ""),
                generated_names=({
                    _renamed_identifier(variable, renames, function=scope): {
                        role: _renamed_identifier(
                            value, renames,
                            function=(canonical_name if role == "parameter" else scope),
                        )
                        for role, value in names.items()
                    }
                    for variable, names in generated.items()
                } if isinstance(generated, Mapping) else None),
            )
            # A canonical stage has no active renames. Running the same large
            # extraction twice would only rediscover identical data flow.
            canonical_result = (
                result if not renames and text == canonical else
                {"source": _canonical_after_override} if _canonical_after_override is not None else
                extract_function(
                    canonical,
                    start, end,
                    canonical_name,
                    comment=str(operation.get("comment", "") or ""),
                    generated_names=(dict(generated) if isinstance(generated, Mapping) else None),
                )
            )
            text = result["source"]
            canonical = canonical_result["source"]
            receipt = dict(result["info"])
            receipt["position"] = position
            receipts.append(receipt)
        elif kind == "rewrite":
            entry = _renamed_identifier(str(operation["entryPoint"]), renames)
            rewritten = _apply_rewrite(
                text, operation, renames, canonical_source=canonical
            )
            _check_protected(text, rewritten, entry)
            if _canonical_after_override is not None:
                canonical_rewritten = _canonical_after_override
            elif not renames and text == canonical:
                canonical_rewritten = rewritten
            else:
                canonical_rewritten = _apply_rewrite(
                    canonical, operation, {}, canonical_source=canonical
                )
                _check_protected(canonical, canonical_rewritten, str(operation["entryPoint"]))
            text = rewritten
            canonical = canonical_rewritten
            receipts.append({"operation": "rewrite", "position": position, "name": operation["name"],
                             "editCount": len(operation["edits"]), "scopes": sorted({e["scope"] for e in operation["edits"]})})
        else:
            raise RestructureError("unknown structural operation %r" % kind)
    return text, receipts


_CANONICAL_STAGE_CACHE_MAX_BYTES = 24 * 1024 * 1024
_CANONICAL_STAGE_CACHE_MAX_ENTRIES = 8
_CANONICAL_STAGE_CACHE: OrderedDict[
    tuple[bytes, bytes], tuple[tuple[bytes, ...], int]
] = OrderedDict()
_CANONICAL_STAGE_CACHE_LOCK = threading.RLock()


def _clear_canonical_stage_cache() -> None:
    """Test/maintenance hook; no mutation or verification state is cached."""

    with _CANONICAL_STAGE_CACHE_LOCK:
        _CANONICAL_STAGE_CACHE.clear()


_STAGED_APPLY_CACHE_MAX_BYTES = 8 * 1024 * 1024
_STAGED_APPLY_CACHE_MAX_ENTRIES = 64
_STAGED_APPLY_CACHE: OrderedDict[
    tuple[bytes, bytes, bytes], tuple[bytes, tuple[dict[str, Any], ...], int, bytes]
] = OrderedDict()
_STAGED_APPLY_CACHE_LOCK = threading.RLock()


def _clear_staged_apply_cache() -> None:
    """Test/maintenance hook for the exact, read-only replay result cache."""

    with _STAGED_APPLY_CACHE_LOCK:
        _STAGED_APPLY_CACHE.clear()


_STRUCTURE_REPORT_CACHE_MAX_BYTES = 8 * 1024 * 1024
_STRUCTURE_REPORT_CACHE_MAX_ENTRIES = 8
_STRUCTURE_REPORT_CACHE: OrderedDict[
    tuple[bytes, str], tuple[dict[str, Any], int]
] = OrderedDict()
_STRUCTURE_REPORT_CACHE_LOCK = threading.RLock()


def _clear_structure_report_cache() -> None:
    """Test/maintenance hook; reports are only reused for identical source."""

    with _STRUCTURE_REPORT_CACHE_LOCK:
        _STRUCTURE_REPORT_CACHE.clear()


def _canonical_stage_keys(source: str, operations: Sequence[Mapping[str, Any]]) -> list[tuple[bytes, bytes]]:
    """Hash each exact operation prefix so extending the ledger reuses its stages."""

    source_sha = hashlib.sha256(source.encode("utf-8")).digest()
    digest = hashlib.sha256(b"shader-canonical-stage/v1\0")
    keys = [(source_sha, digest.digest())]
    for operation in operations:
        encoded = json.dumps(
            operation, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        keys.append((source_sha, digest.digest()))
    return keys


def _canonical_operation_stages(source: str, operations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return canonical stages, reusing only exact source-and-ledger prefixes.

    Strings are immutable; a bounded cache avoids replaying a 500-KB shader's
    unchanged structural history for every rename or bounded rewrite. Failed
    replays are never cached, and changed operation content gets a new key.
    """

    try:
        keys = _canonical_stage_keys(source, operations)
    except (TypeError, ValueError, OverflowError):
        # Direct callers with non-JSON operation values retain the old replay
        # behavior and its validation errors; only serializable ledgers cache.
        keys = []
    stages: list[str] = [source]
    if keys:
        with _CANONICAL_STAGE_CACHE_LOCK:
            for key in reversed(keys):
                cached = _CANONICAL_STAGE_CACHE.get(key)
                if cached is not None:
                    _CANONICAL_STAGE_CACHE.move_to_end(key)
                    stages = [zlib.decompress(stage).decode("utf-8")
                              for stage in cached[0]]
                    break
    for operation in operations[len(stages) - 1:]:
        canonical, unused = apply_operations(
            stages[-1], [operation], renames={},
            _canonical_source_override=stages[-1],
        )
        stages.append(canonical)
    if keys:
        # A long ledger stores many nearly identical 500-KB stages. Keeping
        # their raw texts hit the 24-MiB ceiling around a few dozen edits,
        # disabling the entire prefix cache and forcing replay from stage 0.
        # Compress only exact immutable stage snapshots; validation, receipts
        # and mutable state are always recomputed from the requested ledger.
        packed = tuple(zlib.compress(stage.encode("utf-8"), level=1)
                       for stage in stages)
        size = sum(map(len, packed))
        if size <= _CANONICAL_STAGE_CACHE_MAX_BYTES:
            with _CANONICAL_STAGE_CACHE_LOCK:
                _CANONICAL_STAGE_CACHE[keys[-1]] = (packed, size)
                _CANONICAL_STAGE_CACHE.move_to_end(keys[-1])
                while (
                    len(_CANONICAL_STAGE_CACHE) > _CANONICAL_STAGE_CACHE_MAX_ENTRIES
                    or sum(row[1] for row in _CANONICAL_STAGE_CACHE.values())
                    > _CANONICAL_STAGE_CACHE_MAX_BYTES
                ):
                    _CANONICAL_STAGE_CACHE.popitem(last=False)
    return stages


def _selector_leaf(selector: str) -> str:
    field = _FIELD_SELECTOR.fullmatch(selector)
    local = _LOCAL_SELECTOR.fullmatch(selector)
    return (field.group("member") if field else
            local.group("local") if local else selector)


def _all_pending_local_owners_absent(stage: str, pending: Mapping[str, str]) -> bool:
    """Skip a stage that cannot yet declare any pending function local.

    An exact local selector cannot bind until its owner function name exists
    even lexically. A text hit merely falls back to the authoritative binding
    index; this shortcut never accepts a rename or hides an ambiguous owner.
    """

    if not pending:
        return False
    owners: set[str] = set()
    for selector in pending:
        local = _LOCAL_SELECTOR.fullmatch(selector)
        if local is None:
            return False
        owners.add(local.group("function"))
    return all(re.search(r"(?<![A-Za-z0-9_])" + re.escape(owner) +
                         r"(?![A-Za-z0-9_])", stage) is None for owner in owners)


def _staged_rename_plan(source: str, operations: Sequence[Mapping[str, Any]],
                        renames: Mapping[str, str]) -> tuple[dict[str, str], list[dict[str, str]], list[str]]:
    """Bind each selector at the first structural stage that declares it."""

    stages = _canonical_operation_stages(source, operations)
    pending = {str(key): str(value) for key, value in dict(renames or {}).items()}
    known_old_leaves = [_selector_leaf(key) for key in pending]
    clean: dict[str, str] = {}
    buckets: list[dict[str, str]] = []
    targets: dict[str, list[str]] = {}
    for stage in stages:
        if not pending:
            # Most session renames bind in the baseline stage. Once every
            # selector is active, later structural stages cannot bind anything
            # else; constructing a full declaration index for each one is
            # wasted work (and grows linearly with the edit history).
            buckets.append({})
            continue
        if _all_pending_local_owners_absent(stage, pending):
            buckets.append({})
            continue
        bound, deferred = split_bound_renames(stage, pending)
        checked = validate_renames(
            stage, bound, limit=MAX_RENAMES_TOTAL, known_old_leaves=known_old_leaves
        ) if bound else {}
        for selector, target in checked.items():
            for previous in targets.get(target, []):
                if previous != selector and not rename_targets_can_share(
                    stage, previous, selector, target=target
                ):
                    raise EquivalenceError(
                        "'%s' and '%s' both map to '%s' in an overlapping or unresolved scope"
                        % (previous, selector, target)
                    )
            targets.setdefault(target, []).append(selector)
            if selector in clean and clean[selector] != target:
                raise EquivalenceError(
                    "selector '%s' requests two different targets" % selector
                )
            clean[selector] = target
        buckets.append(checked)
        pending = deferred
    if pending:
        # Produce the ordinary source-bound diagnostic at the final program,
        # rather than silently dropping a misspelled selector from the ledger.
        validate_renames(
            stages[-1], pending, limit=MAX_RENAMES_TOTAL,
            known_old_leaves=known_old_leaves,
        )
        raise EquivalenceError("rename selectors were not bound by the structural ledger")

    final_identifiers = ({token["text"] for token in tokenize(stages[-1])
                          if token["kind"] == "ident"} if clean else set())
    old_leaves = set(known_old_leaves)
    for selector, target in clean.items():
        leaf = _selector_leaf(selector)
        if target != leaf and target in final_identifiers and target not in old_leaves:
            # A target used by a local in an unrelated function is harmless;
            # a same-function shadow or global collision is not. Rebind the
            # selector against the final source when it still exists there.
            # If extraction moved it away, retain the conservative rejection.
            final_bound, unused_deferred = split_bound_renames(
                stages[-1], {selector: target}
            )
            accepted = (validate_renames(
                stages[-1], final_bound, limit=MAX_RENAMES_TOTAL
            ).get(selector) if final_bound else None)
            if accepted != target:
                raise EquivalenceError(
                    "'%s' already exists in the final structural source in an overlapping or unresolved scope; renaming '%s' to it would collide"
                    % (target, selector)
                )
    return clean, buckets, stages


def validate_staged_renames(source: str, operations: Sequence[Mapping[str, Any]],
                            renames: Mapping[str, str]) -> dict[str, str]:
    """Validate selectors against the first stage where each declaration exists."""

    clean, unused_buckets, unused_stages = _staged_rename_plan(source, operations, renames)
    return clean


def _current_stage_selectors(delta: Mapping[str, str], active: Mapping[str, str]) -> dict[str, str]:
    """Rebase newly active selector owners to the current renamed program."""

    result: dict[str, str] = {}
    for selector, target in delta.items():
        field = _FIELD_SELECTOR.fullmatch(selector)
        local = _LOCAL_SELECTOR.fullmatch(selector)
        if field:
            selector = _renamed_identifier(field.group("owner"), active) + "." + field.group("member")
        elif local:
            selector = _renamed_identifier(local.group("function"), active) + "::" + local.group("local")
        result[selector] = target
    return result


def _staged_renames_bytes(renames: Mapping[str, str]) -> bytes:
    """Encode an exact stage's active selectors for collision-safe cache lookup."""

    return json.dumps(
        dict(renames), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def apply_staged_operations(source: str, operations: Sequence[Mapping[str, Any]],
                            renames: Mapping[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """Replay structure while activating aliases when declarations appear."""

    # An exact completed request can skip planning. A changed rename map must
    # first pass the entire current ledger's validation, but prefixes whose
    # *active* selectors have not changed can reuse their replayed source and
    # receipts. Renames declared by a later extraction cannot affect an
    # earlier stage; a baseline rename invalidates every affected prefix.
    try:
        stage_keys = _canonical_stage_keys(source, operations)
        renames_json = _staged_renames_bytes(renames)
        renames_sha = hashlib.sha256(renames_json).digest()
        exact_key = (*stage_keys[-1], renames_sha)
    except (TypeError, ValueError, OverflowError):
        stage_keys = []
        exact_key = None
    if exact_key is not None:
        with _STAGED_APPLY_CACHE_LOCK:
            cached = _STAGED_APPLY_CACHE.get(exact_key)
            if cached is not None and cached[3] == renames_json:
                _STAGED_APPLY_CACHE.move_to_end(exact_key)
                return zlib.decompress(cached[0]).decode("utf-8"), copy.deepcopy(list(cached[1]))

    clean, buckets, stages = _staged_rename_plan(source, operations, renames)
    cache_keys: list[tuple[bytes, bytes, bytes]] = []
    active_bytes: list[bytes] = []
    if stage_keys:
        planned_active: dict[str, str] = {}
        for position, bucket in enumerate(buckets):
            planned_active.update(bucket)
            encoded = _staged_renames_bytes(planned_active)
            active_bytes.append(encoded)
            cache_keys.append((*stage_keys[position], hashlib.sha256(encoded).digest()))
    current = source
    active: dict[str, str] = {}
    receipts: list[dict[str, Any]] = []
    prefix_position = 0
    cache_hit = False
    # A cache hit is checked against the exact active selector bytes as well
    # as the source and operation-prefix hashes. Nothing is reused until the
    # complete new ledger has passed validation above.
    if cache_keys:
        with _STAGED_APPLY_CACHE_LOCK:
            for position in range(len(operations) - 1, -1, -1):
                cached = _STAGED_APPLY_CACHE.get(cache_keys[position])
                if cached is None or cached[3] != active_bytes[position]:
                    continue
                _STAGED_APPLY_CACHE.move_to_end(cache_keys[position])
                current = zlib.decompress(cached[0]).decode("utf-8")
                receipts = copy.deepcopy(list(cached[1]))
                active = {selector: target
                          for bucket in buckets[:position + 1]
                          for selector, target in bucket.items()}
                prefix_position = position
                cache_hit = True
                break
    completed_snapshots: list[
        tuple[tuple[bytes, bytes, bytes], bytes, tuple[dict[str, Any], ...], int, bytes]
    ] = []
    snapshot_bytes = 0
    for position in range(prefix_position, len(stages)):
        # The cached prefix already applied the delta at its final stage.
        if not (cache_hit and position == prefix_position):
            delta = buckets[position]
            if delta:
                current = apply_renames(current, _current_stage_selectors(delta, active))
                active.update(delta)
        if cache_keys:
            packed = zlib.compress(current.encode("utf-8"), level=1)
            receipt_bytes = json.dumps(
                receipts, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8")
            size = len(packed) + len(receipt_bytes) + len(active_bytes[position])
            if size <= _STAGED_APPLY_CACHE_MAX_BYTES:
                completed_snapshots.append((
                    cache_keys[position], packed, tuple(copy.deepcopy(receipts)),
                    size, active_bytes[position],
                ))
                snapshot_bytes += size
                while (len(completed_snapshots) > _STAGED_APPLY_CACHE_MAX_ENTRIES
                       or snapshot_bytes > _STAGED_APPLY_CACHE_MAX_BYTES):
                    snapshot_bytes -= completed_snapshots.pop(0)[3]
        if position == len(operations):
            break
        current, emitted = apply_operations(
            current, [operations[position]], renames=active,
            _canonical_source_override=stages[position],
            _canonical_after_override=stages[position + 1],
        )
        for receipt in emitted:
            receipt["position"] = position
            receipts.append(receipt)
    if active != clean:
        raise RestructureError("not every validated rename was activated during structural replay")
    if completed_snapshots:
        with _STAGED_APPLY_CACHE_LOCK:
            for key, packed, saved_receipts, size, encoded in completed_snapshots:
                _STAGED_APPLY_CACHE[key] = (packed, saved_receipts, size, encoded)
                _STAGED_APPLY_CACHE.move_to_end(key)
            while (
                len(_STAGED_APPLY_CACHE) > _STAGED_APPLY_CACHE_MAX_ENTRIES
                or sum(row[2] for row in _STAGED_APPLY_CACHE.values())
                > _STAGED_APPLY_CACHE_MAX_BYTES
            ):
                _STAGED_APPLY_CACHE.popitem(last=False)
    return current, receipts


def structure_report(source: str, entry_point: str = "") -> dict[str, Any]:
    """Per-function metrics and heuristic flags for the AI's self-review pass."""

    from .outline import outline

    key = (hashlib.sha256(source.encode("utf-8")).digest(), entry_point)
    with _STRUCTURE_REPORT_CACHE_LOCK:
        cached = _STRUCTURE_REPORT_CACHE.get(key)
        if cached is not None:
            _STRUCTURE_REPORT_CACHE.move_to_end(key)
            return copy.deepcopy(cached[0])

    view = outline(source, entry_point=entry_point)
    tracked: dict[str, str] = {}
    for block in view["declarations"]["cbuffers"]:
        for member in block.get("members", []):
            tracked[member["name"]] = block["name"]
    for resource in view["declarations"]["resources"]:
        tracked[resource["name"]] = "resource"
    lines = source.splitlines()
    functions = []
    for item in view["functions"]:
        body = "\n".join(lines[item["line"] - 1:item["endLine"]])
        words = set(re.findall(r"[A-Za-z_]\w*", body))
        globals_used = sorted(name for name in words if name in tracked)
        item = {**item, "globalsUsed": globals_used[:24], "globalsUsedCount": len(globals_used)}
        flags = []
        params = len(item.get("parameters") or [])
        if item["lines"] > 120:
            flags.append("large: split at the next computation boundary")
        if item["lines"] < 4 and item["name"] != entry_point:
            flags.append("tiny: consider inlining or merging with its caller")
        if params > 8:
            flags.append("many-parameters: the range probably mixes two computations")
        if item.get("anonymousLocals", 0) > 0:
            flags.append("anonymous-locals: %d temporaries still need names" % item["anonymousLocals"])
        if re.fullmatch(r"(?i)(stage|part|section|block|chunk|step|func|helper)\d*", item["name"] or ""):
            flags.append("placeholder-name: name the computation, not its position")
        if len(globals_used) > 12 and item["name"] != entry_point:
            flags.append("many-globals: touches %d cbuffer members/resources; probably several concerns" % len(globals_used))
        functions.append({**item, "flags": flags})
    report = {
        "schema": RESTRUCTURE_SCHEMA,
        "totals": view["totals"],
        "anonymity": view["anonymity"],
        "functions": functions,
        "flagged": sum(1 for item in functions if item["flags"]),
    }
    report_bytes = json.dumps(report, ensure_ascii=False, separators=(",", ":"),
                              default=str).encode("utf-8")
    size = len(report_bytes)
    if size <= _STRUCTURE_REPORT_CACHE_MAX_BYTES:
        with _STRUCTURE_REPORT_CACHE_LOCK:
            _STRUCTURE_REPORT_CACHE[key] = (copy.deepcopy(report), size)
            _STRUCTURE_REPORT_CACHE.move_to_end(key)
            while (
                len(_STRUCTURE_REPORT_CACHE) > _STRUCTURE_REPORT_CACHE_MAX_ENTRIES
                or sum(row[1] for row in _STRUCTURE_REPORT_CACHE.values())
                > _STRUCTURE_REPORT_CACHE_MAX_BYTES
            ):
                _STRUCTURE_REPORT_CACHE.popitem(last=False)
    return report


__all__ = ["RESTRUCTURE_SCHEMA", "MAX_REWRITE_BYTES", "RestructureError", "apply_operations",
           "apply_staged_operations", "validate_staged_renames", "extract_function",
           "prepare_rewrite", "source_bound_anchor", "anchor_extractions",
           "retain_extraction_names", "structure_report"]
