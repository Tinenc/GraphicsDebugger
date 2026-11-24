"""Bounded lexical observations for the Agent's existing whole-source review.

These are navigation and change facts, never a quality verdict or permission to
rewrite an expression. In particular, corresponding channel spelling does not
prove independent evaluation, equivalent precision, or absence of side effects.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, deque
import copy
import hashlib
import json
import re
from threading import Lock
from typing import Any, Mapping

from .shader_workspace.flow_graph import _function_records, _tokens


SCHEMA = "bread-source-review-observations/v1"
CATALOG_SCHEMA = "bread-source-review-catalog/v1"
MAX_OBSERVATION_BYTES = 6 * 1024
MAX_EXAMPLES = 8
_CACHE_ENTRIES = 4
_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_CACHE_LOCK = Lock()
_SCALAR_TYPES = frozenset({"float", "half", "double", "min16float"})
_QUALIFIERS = frozenset({"const", "precise", "static", "uniform"})
_CHANNEL_SUFFIX = re.compile(r"^(.+?)([XYZWRGBA])$")
_UNDERSCORE_CHANNEL = re.compile(r"^(.+_)([xyzwrgba])$")
_PHYSICAL_CHANNEL = re.compile(r"^(.*_)([xyzw])(_(?:f32|i32|u32))$")
_COMMENTS_AND_STRINGS = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\r\n]*|/\*[\s\S]*?\*/')
_VECTOR_CONSTRUCTOR = re.compile(r"(?:float|half|double|min16float)([234])$")
_MEMBER_PATH = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_NUMBER_VALUE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fFhHlLuU]?$", re.ASCII)
_MAX_CONSTRUCTOR_TOKENS = 128
_REVIEW_CANDIDATE_CATEGORIES = (
    "channelExpressions", "namedChannelFamilies", "expandedVectorProducts",
    "duplicateBodies", "assignments", "functionComments")


def _interleave(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Retain every match, spreading navigation examples across functions."""
    pending = deque(iter(group) for group in groups)
    result = []
    while pending:
        group = pending.popleft()
        row = next(group, None)
        if row is not None:
            result.append(row)
            pending.append(group)
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _channel_name(word: str) -> tuple[str, str] | None:
    match = _CHANNEL_SUFFIX.fullmatch(word) or _UNDERSCORE_CHANNEL.fullmatch(word)
    return (match[1], match[2].upper()) if match else None


def _scalar_statement(tokens: list[Any]) -> dict[str, Any] | None:
    """Recognize only a single initialized scalar, not an arbitrary assignment."""
    words = [token.text for token in tokens]
    cursor = 0
    while cursor < len(words) and words[cursor] in _QUALIFIERS:
        cursor += 1
    if (len(words) < cursor + 5 or words[cursor] not in _SCALAR_TYPES
            or words[cursor + 2] != "=" or words[-1] != ";"):
        return None
    variable = words[cursor + 1]
    channel = _channel_name(variable)
    if channel is None:
        return None
    expression = words[cursor + 3:-1]
    # A comma at declaration depth zero introduces another declared variable.
    depth = 0
    for word in expression:
        if word == "," and depth == 0:
            return None
        depth += word in {"(", "["}
        depth -= word in {")", "]"}
    return {"stem": channel[0], "channel": channel[1], "variable": variable,
            "declaration": words[:cursor + 1], "expression": expression,
            "line": tokens[0].line, "endLine": tokens[-1].line,
            "startOffset": tokens[0].offset,
            "endOffset": tokens[-1].offset + len(tokens[-1].text)}


def _varying_token(word: str, channel: str, *, member: bool) -> str | None:
    component = dict(zip("RGBA", "xyzw")).get(channel, channel.lower())
    if member and word in {component, channel.lower()}:
        return "<component>"
    named = _channel_name(word)
    if named is not None and named[1] == channel:
        return named[0] + "<component>"
    physical = _PHYSICAL_CHANNEL.fullmatch(word)
    if physical and physical[2] == component:
        return physical[1] + "<component>" + physical[3]
    return None


def _corresponding(rows: list[dict[str, Any]]) -> bool:
    if (len({tuple(row["declaration"]) for row in rows}) != 1
            or len({len(row["expression"]) for row in rows}) != 1):
        return False
    expressions = [row["expression"] for row in rows]
    for index, words in enumerate(zip(*expressions)):
        if len(set(words)) == 1:
            continue  # A common scalar remains common, including a name ending in X.
        # Function names with channel-like suffixes may denote different work.
        if any(index + 1 < len(expr) and expr[index + 1] == "(" for expr in expressions):
            return False
        normalized = [_varying_token(word, row["channel"],
            member=index > 0 and row["expression"][index - 1] == ".")
            for word, row in zip(words, rows)]
        if None in normalized or len(set(normalized)) != 1:
            return False
    return True


def _channel_candidates(source: str, tokens: list[Any], records: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    count = 0
    groups: list[list[dict[str, Any]]] = []
    for record in records:
        pending: list[Any] = []
        run: list[dict[str, Any]] = []
        function_examples: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal count, run
            if len(run) >= 2 and _corresponding(run):
                # Directives are masked by the parser. Do not join their branches.
                span = source[run[0]["startOffset"]:run[-1]["endOffset"]]
                if not re.search(r"(?m)^\s*#", span):
                    count += 1
                    example = re.sub(r"\s+", " ", span).strip()
                    function_examples.append({"function": record["name"],
                        "startLine": run[0]["line"], "endLine": run[-1]["endLine"],
                        "startOffset": run[0]["startOffset"], "endOffset": run[-1]["endOffset"],
                        "channels": "".join(row["channel"] for row in run),
                        "variables": [row["variable"] for row in run],
                        "example": example[:320], "exampleTruncated": len(example) > 320})
            run = []

        for token in tokens[record["bodyStart"] + 1:record["bodyEnd"]]:
            if token.text in {"{", "}"}:
                flush()
                pending = []
                continue
            pending.append(token)
            if token.text != ";":
                continue
            row = _scalar_statement(pending)
            pending = []
            if row is None:
                flush()
                continue
            family = "XYZW" if row["channel"] in "XYZW" else "RGBA"
            if run and (row["stem"] != run[0]["stem"]
                        or len(run) >= len(family) or row["channel"] != family[len(run)]):
                flush()
            if not run and row["channel"] != family[0]:
                continue
            run.append(row)
        flush()
        if function_examples:
            groups.append(function_examples)
    return count, _interleave(groups)


def _header_start(tokens: list[Any], record: Mapping[str, Any]) -> int:
    """Include qualifiers and attributes, stopping at the preceding declaration."""
    index = record["bodyStart"] - 1
    while index >= 0 and tokens[index].text not in {";", "{", "}"}:
        index -= 1
    return index + 1


def _named_channel_families(tokens: list[Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Find channel spelling in local scalar names, regardless of adjacency.

    Each channel position is considered independently: projectedXFromY can
    belong to both a projected<Component>FromY and a projectedXFrom<Component>
    family. These names say nothing about expression or coordinate equivalence.
    """
    count = 0
    groups = []
    for record in records:
        families: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
        body = tokens[record["bodyStart"] + 1:record["bodyEnd"]]
        for index, token in enumerate(body[:-2]):
            if token.text not in _SCALAR_TYPES or body[index + 2].text not in {"=", ";"}:
                continue
            variable = body[index + 1]
            for match in re.finditer(r"[XYZWRGBA](?=[A-Z_]|$)|(?<=_)[xyzwrgba](?=_|$)", variable.text):
                channel = match.group().upper()
                family = "XYZW" if channel in "XYZW" else "RGBA"
                pattern = variable.text[:match.start()] + "<component>" + variable.text[match.end():]
                families.setdefault((pattern, family), {}).setdefault(channel, []).append(
                    {"variable": variable.text, "line": variable.line, "offset": variable.offset})
        examples = []
        for (pattern, family), members in families.items():
            if not all(channel in members for channel in family[:2]):
                continue
            count += 1
            variables = [row for channel in family for row in members.get(channel, [])]
            examples.append({"function": record["name"], "pattern": pattern,
                "startLine": min(row["line"] for row in variables),
                "endLine": max(row["line"] for row in variables),
                "startOffset": min(row["offset"] for row in variables),
                "channels": "".join(channel for channel in family if channel in members),
                "variables": variables})
        if examples:
            groups.append(examples)
    selected = _interleave(groups)
    return {"basis": "local-scalar-name-2-to-4-channel-XY-or-RG-families-at-suffix-or-infix-independent-of-statement-order",
        "limitations": "Naming facts only; expressions, scope, dependency and precision may differ. No family or zero families proves simplification complete.",
        "candidateFamilyCount": count, "examples": selected, "omittedCount": max(0, count - len(selected))}


def _strip_outer_parentheses(words: list[str]) -> list[str]:
    """Remove grouping only when it surrounds the whole small operand."""
    while len(words) >= 2 and words[0] == "(" and words[-1] == ")":
        depth = 0
        for index, word in enumerate(words):
            depth += word == "("
            depth -= word == ")"
            if depth == 0:
                break
        if index != len(words) - 1:
            break
        words = words[1:-1]
    return words


def _component_product(words: list[str]) -> list[dict[str, Any]]:
    """Recognize only a simple value times a direct .x/.r component path."""
    words = _strip_outer_parentheses(words)
    depth = 0
    products = []
    for index, word in enumerate(words):
        if word == "*" and depth == 0:
            products.append(index)
        depth += word == "("
        depth -= word == ")"
    if len(products) != 1:
        return []
    split = products[0]
    operands = [_strip_outer_parentheses(words[:split]), _strip_outer_parentheses(words[split + 1:])]
    result = []
    for vector_index, vector in enumerate(operands):
        value = operands[1 - vector_index]
        vector_text, value_text = "".join(vector), "".join(value)
        if (len(vector) < 3 or vector[-2] != "." or vector[-1] not in "xyzwrgba" or
                len(vector[-1]) != 1 or not _MEMBER_PATH.fullmatch(vector_text) or
                not (_MEMBER_PATH.fullmatch(value_text) or _NUMBER_VALUE.fullmatch(value_text))):
            continue
        result.append({"vector": tuple(vector[:-2]), "value": tuple(value),
                       "channel": vector[-1], "vectorFirst": vector_index == 0})
    return result


def _expanded_vector_products(source: str, tokens: list[Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Observe one exact repeated constructor pattern, not algebraic equivalence.

    Pair parentheses once and bound each inspected constructor. Deeply nested
    generated constructors must not cause overlapping quadratic rescans.
    """
    closing = {}
    stack = []
    for index, token in enumerate(tokens):
        if token.text == "(":
            stack.append(index)
        elif token.text == ")" and stack:
            closing[stack.pop()] = index
    count = 0
    groups = []
    for record in records:
        examples = []
        for index in range(record["bodyStart"] + 1, record["bodyEnd"] - 1):
            match = _VECTOR_CONSTRUCTOR.fullmatch(tokens[index].text)
            if (match is None or tokens[index + 1].text != "(" or
                    tokens[index - 1].text in {".", "::", "->"}):
                continue
            end = closing.get(index + 1)
            if end is None or end - index > _MAX_CONSTRUCTOR_TOKENS:
                continue
            groups_of_words: list[list[str]] = [[]]
            depth = 0
            for position in range(index + 2, end):
                word = tokens[position].text
                if word == "," and depth == 0:
                    groups_of_words.append([])
                else:
                    groups_of_words[-1].append(word)
                depth += word in {"(", "[", "{"}
                depth -= word in {")", "]", "}"}
            dimension = int(match[1])
            if len(groups_of_words) != dimension:
                continue
            products = [_component_product(words) for words in groups_of_words]
            corresponding = None
            for first in products[0]:
                family = "xyzw" if first["channel"] in "xyzw" else "rgba"
                if first["channel"] != family[0]:
                    continue
                if all(any(row == {**first, "channel": family[offset]} for row in rows)
                       for offset, rows in enumerate(products)):
                    corresponding = first
                    channels = family[:dimension]
                    break
            if corresponding is None:
                continue
            span = source[tokens[index].offset:tokens[end].offset + 1]
            if re.search(r"(?m)^\s*#", span):
                continue
            count += 1
            examples.append({"function": record["name"],
                "startLine": tokens[index].line, "endLine": tokens[end].line,
                "startOffset": tokens[index].offset, "endOffset": tokens[end].offset + 1,
                "constructor": tokens[index].text, "channels": channels,
                "commonValue": "".join(corresponding["value"]),
                "vectorValue": "".join(corresponding["vector"]),
                "operandOrder": "vector-first" if corresponding["vectorFirst"] else "common-first"})
        if examples:
            groups.append(examples)
    selected = _interleave(groups)
    return {"basis": "2-to-4-component-constructors-with-exact-repeated-simple-value-times-corresponding-member-components",
        "limitations": "Lexical pattern only, no type or safe-rewrite proof. Preserves operand order; excludes calls, indexing and arbitrary algebra. Zero is not completion.",
        "candidateCount": count, "examples": selected, "omittedCount": max(0, count - len(selected))}


def _duplicate_bodies(source: str, tokens: list[Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare tokens with only positional parameter names substituted.

    Members, globals, local names, constants and callee names remain exact.
    Do not compare bodies containing directives or literals hidden by the
    navigation parser. This is a reuse candidate, not an equivalence proof.
    """
    groups: dict[str, dict[str, Any]] = {}
    hidden_literals = [(match.start(), match.end()) for match in _COMMENTS_AND_STRINGS.finditer(source)
                       if match.group().startswith(('"', "'"))]
    for record in records:
        header_start = _header_start(tokens, record)
        begin = tokens[header_start].offset
        end = record["bodyEndOffset"]
        if (re.search(r"(?m)^\s*#", source[begin:end]) or
                any(begin <= start < end for start, _ in hidden_literals)):
            continue
        parameters = {item["name"]: "<parameter:%d>" % index
                      for index, item in enumerate(record["parameters"])}
        words = []
        selected = tokens[header_start:record["bodyEnd"] + 1]
        for index, token in enumerate(selected):
            previous = selected[index - 1].text if index else ""
            following = selected[index + 1].text if index + 1 < len(selected) else ""
            if token.line == record["line"] and token.column == record["column"]:
                words.append("<function>")
            elif token.text in parameters and previous not in {".", "->", "::"} and following != "(":
                words.append(parameters[token.text])
            else:
                words.append(token.text)
        key = _digest(words)
        group = groups.setdefault(key, {"bodyTokenCount": len(record["bodyTokens"]),
            "functionCount": 0, "functions": [], "omittedFunctions": 0})
        group["functionCount"] += 1
        group["functions"].append({"function": record["name"],
            "startLine": record["line"], "endLine": record["bodyEndLine"],
            "startOffset": begin, "endOffset": end})
    repeated = sorted((group for group in groups.values() if group["functionCount"] > 1),
                      key=lambda group: -group["bodyTokenCount"])
    return {"basis": "exact-declaration-and-body-tokens-with-positional-parameter-renaming-only",
        "candidateGroupCount": len(repeated),
        "candidateFunctionCount": sum(group["functionCount"] for group in repeated),
        "examples": repeated, "omittedCount": 0}


def _assignment_observations(tokens: list[Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Locate the longest lexical assignment right-hand sides, without grading."""
    examples = []
    count = max_tokens = max_lines = max_depth = 0
    for record in records:
        body = tokens[record["bodyStart"] + 1:record["bodyEnd"]]
        # Scan backwards once. Every assignment in a chain shares its next
        # delimiter, so a = b = c must not rescan overlapping RHS ranges.
        # A frame retains both the current expression peak and the entire
        # bracket's peak: commas reset the former, while an enclosing RHS
        # still includes parentheses from all arguments in the bracket.
        depth = 0
        frames = [{"end": len(body), "peak": 0, "wholePeak": 0}]
        for index in range(len(body) - 1, -1, -1):
            token = body[index]
            word = token.text
            if word in {")", "]", "}"}:
                depth += word == ")"
                frames.append({"end": index, "peak": depth, "wholePeak": depth})
                continue
            if word in {"(", "[", "{"}:
                nested_peak = frames.pop()["wholePeak"] if len(frames) > 1 else depth
                depth -= word == "("
                frames[-1]["peak"] = max(frames[-1]["peak"], nested_peak)
                frames[-1]["wholePeak"] = max(frames[-1]["wholePeak"], nested_peak)
                continue
            frame = frames[-1]
            if word in {",", ";"}:
                frame["end"], frame["peak"] = index, depth
                continue
            if word != "=":
                continue
            start, end = index + 1, frame["end"]
            if start >= end:
                continue
            token_count = end - start
            peak = frame["peak"] - depth
            count += 1
            lines = body[end - 1].line - body[start].line + 1
            max_tokens, max_lines, max_depth = max(max_tokens, token_count), max(max_lines, lines), max(max_depth, peak)
            examples.append({"function": record["name"], "assignmentLine": token.line,
                "startOffset": getattr(token, "offset", index),
                "endOffset": getattr(body[end - 1], "offset", end - 1) + len(body[end - 1].text),
                "target": body[index - 1].text if index and re.fullmatch(r"[A-Za-z_]\w*", body[index - 1].text) else "",
                "startLine": body[start].line, "endLine": body[end - 1].line,
                "tokenCount": token_count, "lineSpan": lines, "parenthesisDepth": peak})
            examples.sort(key=lambda row: (-row["tokenCount"], -row["lineSpan"], row["startLine"]))
            del examples[MAX_EXAMPLES:]
    return {"basis": "assignment-RHS-parser-tokens-excluding-comments-strings-and-directives",
        "selection": "top-8-by-RHS-token-count-then-line-span-then-source-line; navigation-sample-not-all-assignments-or-a-quality-threshold",
        "assignmentCount": count, "maximumTokenCount": max_tokens, "maximumLineSpan": max_lines,
        "maximumParenthesisDepth": max_depth, "examples": examples,
        "omittedCount": max(0, count - len(examples))}


def _comment_observations(source: str, tokens: list[Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Observe adjacency/non-emptiness only; comment usefulness is the Agent's job."""
    comments = [match for match in _COMMENTS_AND_STRINGS.finditer(source)
                if match.group().startswith(("//", "/*"))]
    missing = []
    cursor = 0
    for record in records:
        start = tokens[_header_start(tokens, record)].offset
        while cursor < len(comments) and comments[cursor].end() <= start:
            cursor += 1
        preceding = comments[cursor - 1] if cursor else None
        has_comment = False
        if preceding is not None:
            gap = source[preceding.end():start]
            prefix = source[source.rfind("\n", 0, preceding.start()) + 1:preceding.start()]
            text = preceding.group()[2:]
            if preceding.group().startswith("/*"):
                text = text[:-2]
            has_comment = (not gap.strip() and gap.count("\n") <= 2 and
                           not prefix.strip() and bool(text.strip(" \r\n\t*/")))
        if not has_comment:
            missing.append((len(record["bodyTokens"]),
                {"function": record["name"], "startLine": record["line"],
                 "startOffset": start,
                 "endLine": record["bodyEndLine"]}))
    missing.sort(key=lambda item: (-item[0], item[1]["startLine"]))
    return {"basis": "nonempty-standalone-comment-before-declaration-with-at-most-one-blank-line",
        "exampleOrder": "descending-body-token-count-then-source-line-not-quality",
        "missingLeadingCommentCount": len(missing), "examples": [row for _, row in missing],
        "omittedCount": 0}


def _source_facts(raw: bytes, sha: str) -> dict[str, Any]:
    with _CACHE_LOCK:
        cached = _CACHE.get(sha)
        if cached is not None:
            _CACHE.move_to_end(sha)
            return cached
    source = raw.decode("utf-8-sig", errors="strict")
    records = _function_records(source)
    tokens = _tokens(source)
    overloads: Counter = Counter()
    functions = []
    for record in records:
        signature = (record["name"], record.get("returnType"), tuple(
            (parameter.get("type"), parameter.get("mode")) for parameter in record.get("parameters", [])))
        occurrence = overloads[signature]
        overloads[signature] += 1
        functions.append({"identity": (*signature, occurrence), "name": record["name"],
            "line": record["line"], "endLine": record["bodyEndLine"],
            "bodySha256": _digest(record["bodyTokens"]), "calls": list(record["calls"])})
    count, examples = _channel_candidates(source, tokens, records)
    facts = {"functions": functions, "channelCount": count, "channelExamples": examples,
             "namedChannelFamilies": _named_channel_families(tokens, records),
             "expandedVectorProducts": _expanded_vector_products(source, tokens, records),
             "duplicateBodies": _duplicate_bodies(source, tokens, records),
             "assignments": _assignment_observations(tokens, records),
             "functionComments": _comment_observations(source, tokens, records)}
    with _CACHE_LOCK:
        _CACHE[sha] = facts
        _CACHE.move_to_end(sha)
        while len(_CACHE) > _CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    return facts


def _function_location(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"function": str(row["name"]), "startLine": row["line"], "endLine": row["endLine"]}


def _body_comparison(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    before = {row["identity"]: row for row in baseline["functions"]}
    after = {row["identity"]: row for row in current["functions"]}
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key in ("changed", "unchanged", "added", "removed")}
    for identity, row in after.items():
        old = before.get(identity)
        category = "added" if old is None else "unchanged" if old["bodySha256"] == row["bodySha256"] else "changed"
        groups[category].append(_function_location(row))
    groups["removed"] = [_function_location(row) for key, row in before.items() if key not in after]
    return {"basis": "exact-parser-body-tokens-excluding-comments-whitespace-and-preprocessor-directives",
        "currentCount": len(after), "baselineCount": len(before),
        "counts": {key: len(rows) for key, rows in groups.items()},
        "examples": groups, "omittedCounts": {key: 0 for key in groups}}


def _call_observations(current: dict[str, Any], entry: str) -> dict[str, Any]:
    rows = current["functions"]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)
    incoming = {callee for row in rows for callee in row["calls"]}
    named = [row for row in rows if row["name"] == entry] if entry else []
    named = named or [row for row in rows if re.fullmatch(r"main(?:ps|vs|cs|gs|hs|ds)?", row["name"], re.IGNORECASE)]
    roots = named or [row for row in rows if row["name"] not in incoming]
    selected = list(roots)
    chosen_ids = {row["identity"] for row in selected}
    cursor = 0
    while cursor < len(selected):
        for callee in selected[cursor]["calls"]:
            for row in by_name.get(callee, []):
                if row["identity"] not in chosen_ids:
                    selected.append(row)
                    chosen_ids.add(row["identity"])
        cursor += 1
    reachable_count = len(selected)
    selected.extend(row for row in rows if row["identity"] not in chosen_ids)
    return {"basis": "lexical-user-function-call-names-not-responsibility-or-overload-proof",
        "entryReachableCount": reachable_count,
        "edgeCount": sum(len(row["calls"]) for row in rows),
        "nodes": [{**_function_location(row), "calls": row["calls"],
                   "omittedCallees": 0} for row in selected], "omittedFunctions": 0}


def _bound_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Prefer a small sample of every fact class over many from any one class."""
    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    comparison = result["functionBodies"]
    lists = [(comparison["examples"][key], comparison["omittedCounts"], key)
             for key in ("unchanged", "removed", "added", "changed")]
    lists.append((result["callGraph"]["nodes"], result["callGraph"], "omittedFunctions"))
    lists.extend((result[key]["examples"], result[key], "omittedCount") for key in _REVIEW_CANDIDATE_CATEGORIES)
    while size() > MAX_OBSERVATION_BYTES:
        candidates = [item for item in lists if len(item[0]) > 1]
        if candidates:
            rows, omissions, key = max(candidates, key=lambda item: len(json.dumps(item[0], ensure_ascii=False)))
            rows.pop()
            omissions[key] += 1
            continue
        # A duplicate still needs two locations to be useful; a call node can
        # keep one callee. Trim these nested lists before losing a whole class.
        nested = [(row["functions"], row, "omittedFunctions", 2)
                  for row in result["duplicateBodies"]["examples"]]
        nested.extend((row["calls"], row, "omittedCallees", 1) for row in result["callGraph"]["nodes"])
        candidates = [item for item in nested if len(item[0]) > item[3]]
        if candidates:
            rows, omissions, key, _ = max(candidates, key=lambda item: len(json.dumps(item[0], ensure_ascii=False)))
            rows.pop()
            omissions[key] += 1
            continue
        sample = next((row for row in result["channelExpressions"]["examples"] if row.get("example")), None)
        if sample is not None:
            sample["example"] = ""
            sample["exampleTruncated"] = True
            continue
        # Exceptional oversized names may force a class to lose its example;
        # keep complete counts and explicit omissions even in that case.
        candidates = [item for item in lists if item[0]]
        if not candidates:
            raise ValueError("Source observation metadata exceeds its bounded response size")
        rows, omissions, key = max(candidates, key=lambda item: len(json.dumps(item[0], ensure_ascii=False)))
        rows.pop()
        omissions[key] += 1
    return result


def review_candidates(observations: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read IDs from either the full catalog or its bounded display preview.

    Completion admission must pass the catalog: the inline preview is only a
    navigation sample. IDs live on existing rows, without a duplicate table.
    """
    result = []
    for category in _REVIEW_CANDIDATE_CATEGORIES:
        for row in observations.get(category, {}).get("examples", []):
            if category == "duplicateBodies":
                locations = row.get("functions", [])
                symbols = [location["function"] for location in locations]
            elif category == "namedChannelFamilies":
                locations = [{"function": row["function"], "startLine": variable["line"],
                    "endLine": variable["line"], "startOffset": variable["offset"],
                    "endOffset": variable["offset"] + len(variable["variable"])}
                    for variable in row["variables"]]
                symbols = [variable["variable"] for variable in row["variables"]]
            else:
                locations = [row]
                symbols = (row["variables"] if category == "channelExpressions" else
                    [row["commonValue"], row["vectorValue"]] if category == "expandedVectorProducts" else
                    [row["target"]] if category == "assignments" else [row["function"]])
            result.append({"id": row["candidateId"], "category": category,
                "functions": list(dict.fromkeys(location["function"] for location in locations)),
                "symbols": list(dict.fromkeys(symbol for symbol in symbols if symbol)),
                "locations": [{key: location[key] for key in ("function", "startLine", "endLine", "startOffset", "endOffset")
                               if key in location} for location in locations]})
    return result


def source_review_catalog(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Observe the exact source_snapshot bytes, without tools or state mutation.

    The caller may optionally provide entryPoint. A source read/hash mismatch is
    a diagnostic; callers must not turn it into failure of an already saved edit.
    Only parsed facts are cached, keyed by the exact source SHA-256.
    """
    from pathlib import Path
    from .shader_organization_review import _MAX_SOURCE, _read_bytes

    root = Path(str(snapshot["workspacePath"]))
    _, current = _read_bytes(root, str(snapshot["sourcePath"]), _MAX_SOURCE)
    current_sha = hashlib.sha256(current).hexdigest()
    if current_sha != snapshot["sourceSha256"]:
        raise ValueError("Current source changed before its review observations")
    baseline_path = str(snapshot.get("baselineSourcePath") or "")
    baseline = _read_bytes(root, baseline_path, _MAX_SOURCE)[1] if baseline_path else current
    baseline_sha = hashlib.sha256(baseline).hexdigest()
    if baseline_sha != snapshot["baselineSourceSha256"]:
        raise ValueError("Baseline source changed before its review observations")
    current_facts = _source_facts(current, current_sha)
    baseline_facts = _source_facts(baseline, baseline_sha)
    result = {"schema": CATALOG_SCHEMA, "sourceSha256": current_sha, "baselineSourceSha256": baseline_sha,
        "semanticAssessment": False, "complete": True,
        "completenessBasis": "all-matches-of-declared-lexical-detectors; assignments-top-8-only; not-semantic-or-parser-coverage-proof",
        "selection": "all-matched-lexical-patterns-and-functions; assignments-explicitly-sample-top-8",
        "interpretation": "Navigation facts, not quality verdicts. Review every actual function and the full catalog in the existing scope review; retained candidates need source-specific evidence. Counts, changes and comments never prove quality.",
        "functionBodies": _body_comparison(baseline_facts, current_facts),
        "channelExpressions": {"basis": "consecutive-corresponding-2-to-4-channel-XY-or-RG-scalar-declarations",
            "limitations": "Only consecutive initialized scalar declarations with matching token patterns; zero does not cover interleaved channels or other simplifications.",
            "candidateCount": current_facts["channelCount"], "examples": current_facts["channelExamples"],
            "omittedCount": max(0, current_facts["channelCount"] - len(current_facts["channelExamples"]))},
        "namedChannelFamilies": current_facts["namedChannelFamilies"],
        "expandedVectorProducts": current_facts["expandedVectorProducts"],
        "duplicateBodies": current_facts["duplicateBodies"],
        "assignments": current_facts["assignments"],
        "functionComments": current_facts["functionComments"],
        "callGraph": _call_observations(current_facts, str(snapshot.get("entryPoint") or ""))}
    # Bind before display sorting/trimming. Offset-bearing facts distinguish
    # same-line occurrences; IDs never depend on an example's list position.
    result = copy.deepcopy(result)
    for category in _REVIEW_CANDIDATE_CATEGORIES:
        for row in result[category]["examples"]:
            row["candidateId"] = "src-" + _digest({"sourceSha256": current_sha,
                "category": category, "fact": row})[:20]
    return result


def source_review_observations(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compatible 6 KiB display sample and full-catalog identity.

    The caller can persist source_review_catalog through the existing local
    working-copy path. Its full candidates, not this preview, drive review.
    Reading either API shares the same exact-source parse cache.
    """
    catalog = source_review_catalog(snapshot)
    result = copy.deepcopy(catalog)
    result["schema"] = SCHEMA
    result["complete"] = False
    result.pop("completenessBasis")
    result["selection"] = "bounded-preview-only; read-full-catalog-for-coverage"
    result["catalog"] = {"schema": CATALOG_SCHEMA,
        "complete": True,
        "sourceSha256": catalog["sourceSha256"], "sha256": _digest(catalog),
        "candidateCount": sum(len(catalog[key]["examples"]) for key in _REVIEW_CANDIDATE_CATEGORIES),
        "selection": catalog["selection"]}

    def trim(rows: list[Any], owner: dict[str, Any], count_key: str, limit: int = MAX_EXAMPLES) -> None:
        owner[count_key] += max(0, len(rows) - limit)
        del rows[limit:]

    comparison = result["functionBodies"]
    for key, rows in comparison["examples"].items():
        trim(rows, comparison["omittedCounts"], key)
    graph = result["callGraph"]
    trim(graph["nodes"], graph, "omittedFunctions", min(MAX_EXAMPLES, graph["entryReachableCount"]))
    for row in graph["nodes"]:
        trim(row["calls"], row, "omittedCallees")
    for key in _REVIEW_CANDIDATE_CATEGORIES:
        trim(result[key]["examples"], result[key], "omittedCount")
    for row in result["duplicateBodies"]["examples"]:
        trim(row["functions"], row, "omittedFunctions")
    return _bound_payload(result)
