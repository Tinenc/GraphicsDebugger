"""Bounded, source-revision-bound symbol views for Shader Workspace clients.

This is a projection of the rename binding index, not a second HLSL compiler.
It gives the editor and the Agent the same declaration identities and safe
rename selectors that Host mutations use.  Reads, writes and dependencies are
lexical hints; control flow, preprocessing and callee effects are not proven.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from .equivalence import EquivalenceError, _BoundDeclaration, _RenameBindingIndex, _binding_index


SYMBOL_TABLE_SCHEMA = "replaylab-shader-source-symbols/v1"
_MAX_PAGE_SIZE = 256
_MAX_CACHED_SOURCE_BYTES = 1024 * 1024
_MAX_CACHED_GRAPHS = 2
_ASSIGNMENTS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
_CACHE: OrderedDict[bytes, tuple[str, "_SourceSymbolGraph"]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


class _SourceSymbolGraph:
    def __init__(self, source: str, digest: bytes, index: _RenameBindingIndex) -> None:
        self.source = source
        self.source_sha256 = digest.hex()
        self.index = index
        self.global_ids = sorted(
            (identity for identity, declaration in index.declarations.items()
             if declaration.kind not in {"local", "parameter"}),
            key=lambda identity: index.declarations[identity].sig_index,
        )
        self.owners: dict[str, str] = {}
        for mapping in (index.fields, index.cbuffer_fields):
            for (owner, unused_member), identities in mapping.items():
                for identity in identities:
                    self.owners[identity] = owner
        self._function_rows: dict[int, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def location(self, sig_index: int) -> dict[str, int]:
        token = self.index.tokens[self.index.sig_full[sig_index]]
        offset = int(token["start"])
        return {"line": int(token["line"]),
                "column": offset - self.source.rfind("\n", 0, offset),
                "characterOffset": offset}

    def _selector(self, declaration: _BoundDeclaration) -> str | None:
        if not declaration.reliable or declaration.protected:
            return None
        owner = self.owners.get(declaration.identity)
        if declaration.kind in {"local", "parameter"}:
            candidate = declaration.function_name + "::" + declaration.name
        elif declaration.kind in {"field", "cbuffer-field"} and owner:
            candidate = owner + "." + declaration.name
        elif declaration.kind == "type" and declaration.constant_buffer:
            candidate = "cbuffer:" + declaration.name
        else:
            candidate = declaration.name
        try:
            identity, unused_occurrences, unused_leaf = self.index.resolve(candidate)
        except EquivalenceError:
            return None
        return candidate if identity == declaration.identity else None

    def _scope(self, declaration: _BoundDeclaration) -> dict[str, Any]:
        if declaration.kind == "local" and not declaration.reliable:
            return {"kind": "unresolved", "function": declaration.function_name,
                    "startLine": None, "endLine": None, "visibleFromLine": None,
                    "reason": "control-flow-local scope is not reliably bound"}
        if declaration.kind in {"local", "parameter"}:
            return {"kind": "function" if declaration.kind == "parameter" else "block",
                    "function": declaration.function_name,
                    "startLine": self.location(declaration.scope_start)["line"],
                    "endLine": self.location(declaration.scope_end)["line"],
                    "visibleFromLine": (self.location(declaration.scope_start)["line"]
                                        if declaration.kind == "parameter"
                                        else self.location(declaration.sig_index)["line"])}
        if declaration.kind in {"field", "cbuffer-field"} and declaration.scope_start >= 0:
            return {"kind": "owner-block", "function": None,
                    "startLine": self.location(declaration.scope_start)["line"],
                    "endLine": self.location(declaration.scope_end)["line"],
                    "visibleFromLine": self.location(declaration.sig_index)["line"]}
        return {"kind": "translation-unit", "function": None,
                "startLine": 1, "endLine": len(self.source.splitlines()),
                "visibleFromLine": None}

    def _base_row(self, identity: str) -> dict[str, Any]:
        declaration = self.index.declarations[identity]
        references = self.index.occurrences[identity] - {declaration.sig_index}
        return {"identity": identity, "identityScope": "exact-source-only",
                "name": declaration.name, "kind": declaration.kind,
                "owner": self.owners.get(identity), "type": declaration.type_name or None,
                "declaration": self.location(declaration.sig_index),
                "lexicalScope": self._scope(declaration),
                "renameSelector": self._selector(declaration),
                "boundReferenceCount": len(references),
                "bindingReliable": bool(declaration.reliable and not declaration.protected),
                "unresolvedSameName": declaration.name in self.index.unbound_names}

    def global_row(self, identity: str) -> dict[str, Any]:
        row = self._base_row(identity)
        functions = {
            str(record["name"])
            for position in self.index.occurrences[identity]
            if (record := self.index._function_for(position)) is not None
        }
        row["usedByFunctionCount"] = len(functions)
        row["usedByFunctions"] = sorted(functions)[:16]
        row["usedByFunctionsTruncated"] = len(functions) > 16
        return row

    def _function_symbol_rows(self, record: dict[str, int | str]) -> list[dict[str, Any]]:
        function_index = int(record["index"])
        with self._lock:
            cached = self._function_rows.get(function_index)
        if cached is not None:
            return cached

        opening, closing = int(record["body_open"]), int(record["body_close"])
        bound_by_position: dict[int, str] = {}
        selected: dict[str, list[int]] = {}
        for identity, occurrences in self.index.occurrences.items():
            declaration = self.index.declarations[identity]
            if declaration.kind in {"function", "type"}:
                continue
            positions = sorted(position for position in occurrences if opening < position < closing)
            if (declaration.kind in {"local", "parameter"}
                    and declaration.function_index == function_index):
                selected[identity] = positions
            elif positions and declaration.kind not in {"local", "parameter"}:
                selected[identity] = positions
            for position in positions:
                if position != declaration.sig_index:
                    bound_by_position[position] = identity

        # A direct `value = expression` yields a useful lexical dependency
        # candidate.  Nested assignments and for-header assignments are left
        # unknown, since this view does not build a CFG or expand macros.
        assignments: dict[str, list[set[str]]] = {}
        words = self.index.words
        paren_depth = 0
        call_argument_positions: set[int] = set()
        call_stack: list[bool] = []
        for position in range(opening + 1, closing):
            word = words[position]
            if word == "(":
                previous = words[position - 1] if position else ""
                is_call = bool(re.fullmatch(r"[A-Za-z_]\w*", previous)
                               and previous not in {"if", "for", "while", "switch"})
                call_stack.append(is_call or (call_stack[-1] if call_stack else False))
            elif word == ")":
                if call_stack:
                    call_stack.pop()
            elif call_stack and call_stack[-1]:
                call_argument_positions.add(position)
        for position in range(opening + 1, closing):
            word = words[position]
            if word == "(":
                paren_depth += 1
            elif word == ")":
                paren_depth = max(0, paren_depth - 1)
            if word not in _ASSIGNMENTS or paren_depth:
                continue
            lhs = position - 1
            target = bound_by_position.get(lhs)
            if target is None:
                # A declaration with an initializer is intentionally absent
                # from bound_by_position but still names a direct target.
                target = self.index.declaration_at.get(lhs)
            if target not in selected:
                continue
            cursor = position + 1
            while cursor < closing and words[cursor] not in {";", "}"}:
                cursor += 1
            if (cursor >= closing or words[cursor] != ";"
                    or any(item in _ASSIGNMENTS for item in words[position + 1:cursor])):
                continue
            inputs = {bound_by_position[item] for item in range(position + 1, cursor)
                      if item in bound_by_position and bound_by_position[item] in selected}
            if word != "=":
                inputs.add(target)
            assignments.setdefault(target, []).append(inputs)

        rows: list[dict[str, Any]] = []
        for identity, positions in selected.items():
            declaration = self.index.declarations[identity]
            direct_read = direct_write = uncertain = initializer_write = 0
            for position in positions:
                if position == declaration.sig_index:
                    if position + 1 < len(words) and words[position + 1] == "=":
                        initializer_write += 1
                    continue
                previous = words[position - 1] if position else ""
                following = words[position + 1] if position + 1 < len(words) else ""
                if following in _ASSIGNMENTS:
                    direct_write += 1
                    if following != "=":
                        direct_read += 1
                elif previous in {"++", "--"} or following in {"++", "--"}:
                    direct_read += 1
                    direct_write += 1
                elif position in call_argument_positions or following in {".", "->", "["}:
                    uncertain += 1
                else:
                    direct_read += 1
            candidates = assignments.get(identity, [])
            inputs = sorted(set().union(*candidates) if candidates else set(),
                            key=lambda item: self.index.declarations[item].sig_index)
            references = [position for position in positions
                          if position != declaration.sig_index]
            row = self._base_row(identity)
            row["functionReferences"] = {
                "boundCount": len(references),
                "simpleReadSites": direct_read,
                "simpleWriteSites": direct_write,
                "initializerWriteSites": initializer_write,
                "uncertainAccessSites": uncertain,
                "firstLine": self.location(references[0])["line"] if references else None,
                "lastLine": self.location(references[-1])["line"] if references else None,
                "locations": [self.location(item) for item in references[:8]],
                "locationsTruncated": len(references) > 8,
                "interpretation": "lexical sites only; calls, aliases and runtime paths are not proven",
            }
            row["candidateDependencies"] = {
                "directAssignmentCount": len(candidates),
                "inputSymbolCount": len(inputs),
                "inputSymbols": [{"identity": item,
                                  "name": self.index.declarations[item].name}
                                 for item in inputs[:16]],
                "inputSymbolsTruncated": len(inputs) > 16,
                "interpretation": "direct assignment expression mentions, not data-flow proof",
            }
            rows.append(row)
        rows.sort(key=lambda row: (row["declaration"]["characterOffset"], row["identity"]))
        with self._lock:
            return self._function_rows.setdefault(function_index, rows)


def _graph(source: str) -> _SourceSymbolGraph:
    raw = source.encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(raw).digest()
    if len(raw) > _MAX_CACHED_SOURCE_BYTES:
        return _SourceSymbolGraph(source, digest, _binding_index(source))
    with _CACHE_LOCK:
        cached = _CACHE.get(digest)
        if cached is not None and cached[0] == source:
            _CACHE.move_to_end(digest)
            return cached[1]
    graph = _SourceSymbolGraph(source, digest, _binding_index(source))
    with _CACHE_LOCK:
        cached = _CACHE.get(digest)
        if cached is not None and cached[0] == source:
            _CACHE.move_to_end(digest)
            return cached[1]
        _CACHE[digest] = (source, graph)
        while len(_CACHE) > _MAX_CACHED_GRAPHS:
            _CACHE.popitem(last=False)
    return graph


def inspect_source_symbol_table(source: str, *, function: str | None = None,
                                offset: int = 0, limit: int = 128) -> dict[str, Any]:
    """Page the global table or one function's bound lexical symbol view.

    Identities and selectors apply only to the exact source revision.  This
    API never computes runtime liveness, control-flow reachability or SSA.
    """

    if not isinstance(source, str):
        raise EquivalenceError("source must be text")
    if type(offset) is not int or offset < 0:
        raise EquivalenceError("symbol table offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise EquivalenceError("symbol table limit must be an integer from 1 to 256")
    if function is not None and (not isinstance(function, str)
                                 or not re.fullmatch(r"[A-Za-z_]\w*", function)):
        raise EquivalenceError("function must be one declared identifier")

    graph = _graph(source)
    result: dict[str, Any] = {
        "schema": SYMBOL_TABLE_SCHEMA,
        "schemaVersion": 1,
        "sourceSha256": graph.source_sha256,
        "identityScope": "exact-source-only",
        "analysis": "conservative-lexical",
        "runtimeLivenessKnown": False,
        "dataFlowProven": False,
        "offset": offset,
        "limit": limit,
    }
    if function is None:
        identities = graph.global_ids
        rows = [graph.global_row(identity) for identity in identities[offset:offset + limit]]
        result.update({"view": "global-declarations", "total": len(identities),
                       "rows": rows,
                       "nextOffset": offset + limit if offset + limit < len(identities) else None,
                       "functionCount": len(graph.index.functions)})
        return result

    records = [record for record in graph.index.functions if record["name"] == function]
    if len(records) != 1:
        lines = [graph.location(int(record["name_index"]))["line"] for record in records[:8]]
        raise EquivalenceError(
            "function '%s' does not identify exactly one definition; definition lines: %s" %
            (function, ", ".join(map(str, lines)) or "none")
        )
    record = records[0]
    rows = graph._function_symbol_rows(record)
    result.update({"view": "function-symbols", "total": len(rows),
                   "rows": deepcopy(rows[offset:offset + limit]),
                   "nextOffset": offset + limit if offset + limit < len(rows) else None,
                   "function": {"name": function,
                                "declaration": graph.location(int(record["name_index"])),
                                "bodyStartLine": graph.location(int(record["body_open"]))["line"],
                                "bodyEndLine": graph.location(int(record["body_close"]))["line"]},
                   "limitations": ["preprocessor not expanded", "callee effects not analyzed",
                                   "control-flow and runtime liveness not analyzed"]})
    return result


__all__ = ["SYMBOL_TABLE_SCHEMA", "inspect_source_symbol_table"]
