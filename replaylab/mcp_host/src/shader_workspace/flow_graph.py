"""Source-bound function graphs; navigation metadata grants no authority.

Published graphs describe function interfaces and calls from the final source.
Detailed computation ranges supplied by an Agent remain separate review evidence
and are never promoted into visible graph nodes.
"""

from __future__ import annotations

from bisect import bisect_right
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .io import is_within, read_json_object


FLOW_GRAPH_SCHEMA = "replaylab-shader-flow-graph/v1"
FLOW_GRAPH_VERSION = 1
MODULE_DESCRIPTIONS_SCHEMA = "replaylab-shader-module-descriptions/v1"
MODULE_DESCRIPTIONS_VERSION = 1
MAX_FLOW_GRAPH_BYTES = 1024 * 1024
MAX_MODULE_DESCRIPTIONS_BYTES = 256 * 1024
MAX_GRAPH_NODES = 256
MAX_GRAPH_EDGES = 1024
MAX_MODULE_DESCRIPTION_ENTRIES = 2048
MAX_DESCRIPTION_CHARS = 512
MAX_INTERFACE_ITEMS = 24
MAX_INTERFACE_TEXT_CHARS = 128
MAX_SIGNATURE_CHARS = 512
MAX_TOKEN_COUNT = 750000

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NODE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TOKEN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|(?:\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)|"
    r"::|->|==|!=|<=|>=|&&|\|\||<<|>>|\+\+|--|"
    r"[{}()\[\],;:.<>*=+\-/&|!?%^~]"
)
_NON_FUNCTION_NAMES = frozenset(
    {
        "asm",
        "break",
        "case",
        "cbuffer",
        "continue",
        "discard",
        "do",
        "else",
        "for",
        "if",
        "namespace",
        "packoffset",
        "register",
        "return",
        "sizeof",
        "static_assert",
        "struct",
        "switch",
        "tbuffer",
        "technique",
        "technique10",
        "technique11",
        "while",
    }
)


class ShaderFlowGraphError(RuntimeError):
    """The structured Shader navigation graph is invalid or unsafe."""


class _TokenValue:
    __slots__ = ("text", "offset", "line", "column")

    def __init__(self, text: str, offset: int, line: int, column: int) -> None:
        self.text = text
        self.offset = offset
        self.line = line
        self.column = column


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShaderFlowGraphError("Shader flow graph is not JSON serializable") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_text(
    value: Any,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ShaderFlowGraphError("%s must be text" % label)
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum or "\x00" in text:
        raise ShaderFlowGraphError("%s is empty or oversized" % label)
    if any(ord(character) < 32 for character in text):
        raise ShaderFlowGraphError("%s must be single-line plain text" % label)
    return text


def _contains_implementation_text(text: str) -> bool:
    """True when a supposed summary looks like an executable statement."""

    # Earlier Host versions emitted this exact prose as the entry description.
    # Its punctuation does not turn an otherwise source-bound graph into code;
    # keep the exception exact so arbitrary statements retain the strict gate.
    if text == "Shader entry point; the stage result is produced here.":
        return False
    return bool(
        any(marker in text for marker in (";", "{", "}", "```"))
        or re.search(r"(?<![=!<>])=(?!=)|\b(?:return|discard)\s+", text)
        or re.search(r"(?:\+\+|--|->|&&|\|\||\?)", text)
        or re.search(r"(?:^|\s)#(?:define|if|ifdef|ifndef|include|pragma)\b", text)
        or re.search(r"\b(?:if|for|while|switch)\s*\(", text)
        # A no-space identifier/constructor call is a source snippet even when
        # an Agent omits the trailing semicolon or wraps it in prose.
        or re.search(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?\(", text)
        or re.search(r"\b[A-Za-z_]\w*\s*\[[^\]\r\n]+\]", text)
        or re.search(
            r"\b(?:float|half|double|int|uint|bool|min16float|min16int|min16uint)"
            r"(?:[1-4](?:x[1-4])?)?\s+[A-Za-z_]\w*\b",
            text,
        )
    )


def _normalized_relative_path(value: Any, label: str) -> str:
    text = _bounded_text(value, label, 4096).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ShaderFlowGraphError("%s must stay within its Shader workspace" % label)
    normalized = str(path)
    if normalized != text:
        raise ShaderFlowGraphError("%s is not normalized" % label)
    return normalized


def _masked_source(source: str) -> str:
    """Replace comments, strings, and preprocessor text without moving offsets."""

    characters = list(source)
    length = len(source)
    index = 0
    while index < length:
        current = source[index]
        following = source[index + 1] if index + 1 < length else ""
        if current == "/" and following == "/":
            characters[index] = characters[index + 1] = " "
            index += 2
            while index < length and source[index] not in "\r\n":
                characters[index] = " "
                index += 1
            continue
        if current == "/" and following == "*":
            characters[index] = characters[index + 1] = " "
            index += 2
            while index < length:
                if index + 1 < length and source[index] == "*" and source[index + 1] == "/":
                    characters[index] = characters[index + 1] = " "
                    index += 2
                    break
                if source[index] not in "\r\n":
                    characters[index] = " "
                index += 1
            continue
        if current in ('"', "'"):
            quote = current
            characters[index] = " "
            index += 1
            escaped = False
            while index < length:
                item = source[index]
                if item not in "\r\n":
                    characters[index] = " "
                index += 1
                if escaped:
                    escaped = False
                    continue
                if item == "\\":
                    escaped = True
                elif item == quote:
                    break
            continue
        index += 1

    masked = characters
    start = 0
    continuation = False
    while start < length:
        end = source.find("\n", start)
        if end < 0:
            end = length
        visible = "".join(masked[start:end])
        directive = continuation or visible.lstrip(" \t\r").startswith("#")
        if directive:
            for position in range(start, end):
                if source[position] not in "\r\n":
                    masked[position] = " "
            continuation = source[start:end].rstrip(" \t\r").endswith("\\")
        else:
            continuation = False
        start = end + 1
    return "".join(masked)


def _tokens(source: str) -> list[_TokenValue]:
    masked = _masked_source(source)
    line_starts = [0]
    line_starts.extend(index + 1 for index, value in enumerate(source) if value == "\n")
    result: list[_TokenValue] = []
    for match in _TOKEN.finditer(masked):
        offset = match.start()
        line_index = bisect_right(line_starts, offset) - 1
        result.append(
            _TokenValue(
                match.group(0),
                offset,
                line_index + 1,
                offset - line_starts[line_index] + 1,
            )
        )
        if len(result) > MAX_TOKEN_COUNT:
            raise ShaderFlowGraphError("organized HLSL contains too many lexical tokens")
    return result


def _matching_pairs(
    tokens: list[_TokenValue], opening: str, closing: str
) -> dict[int, int]:
    stack: list[int] = []
    result: dict[int, int] = {}
    for index, token in enumerate(tokens):
        if token.text == opening:
            stack.append(index)
        elif token.text == closing and stack:
            begin = stack.pop()
            result[begin] = index
    return result


def _function_records(source: str) -> list[dict[str, Any]]:
    tokens = _tokens(source)
    parentheses = _matching_pairs(tokens, "(", ")")
    braces = _matching_pairs(tokens, "{", "}")
    records: list[dict[str, Any]] = []
    depth = 0
    for index, token in enumerate(tokens):
        if token.text == "}":
            depth = max(0, depth - 1)
        if (
            depth == 0
            and _IDENTIFIER.fullmatch(token.text) is not None
            and token.text.lower() not in _NON_FUNCTION_NAMES
            and index + 1 < len(tokens)
            and tokens[index + 1].text == "("
        ):
            close_parenthesis = parentheses.get(index + 1)
            if close_parenthesis is not None:
                suffix = close_parenthesis + 1
                if suffix < len(tokens) and tokens[suffix].text == ":":
                    suffix += 1
                    while suffix < len(tokens) and tokens[suffix].text not in ("{", ";"):
                        suffix += 1
                previous = tokens[index - 1].text if index > 0 else ""
                return_semantic = ""
                if close_parenthesis + 1 < len(tokens) and tokens[close_parenthesis + 1].text == ":":
                    return_semantic = next(
                        (
                            item.text
                            for item in tokens[close_parenthesis + 2:suffix]
                            if _IDENTIFIER.fullmatch(item.text)
                        ),
                        "",
                    )
                return_prefix = (
                    _IDENTIFIER.fullmatch(previous) is not None
                    or previous in (">", "]", "*", "&")
                )
                if (
                    return_prefix
                    and suffix < len(tokens)
                    and tokens[suffix].text == "{"
                    and suffix in braces
                ):
                    records.append(
                        {
                            "name": token.text,
                            "line": token.line,
                            "declarationLine": tokens[index - 1].line,
                            "column": token.column,
                            "bodyStart": suffix,
                            "bodyEnd": braces[suffix],
                            "parameters": _parameter_records(tokens[index + 2:close_parenthesis]),
                            "returnType": previous if _IDENTIFIER.fullmatch(previous) else "",
                            "returnSemantic": return_semantic,
                            "calls": [],
                        }
                    )
        if token.text == "{":
            depth += 1

    by_name: dict[str, list[int]] = {}
    for record_index, record in enumerate(records):
        by_name.setdefault(record["name"], []).append(record_index)
    for record in records:
        calls: list[str] = []
        known: set[str] = set()
        for token_index in range(record["bodyStart"] + 1, record["bodyEnd"]):
            token = tokens[token_index]
            if (
                token.text not in by_name
                or token_index + 1 >= len(tokens)
                or tokens[token_index + 1].text != "("
                or (token_index > 0 and tokens[token_index - 1].text in (".", "::", "->"))
                or token.text in known
            ):
                continue
            known.add(token.text)
            calls.append(token.text)
        record["calls"] = calls
        body = tokens[record["bodyStart"] + 1:record["bodyEnd"]]
        record["bodyTokens"] = [item.text for item in body]
        record["returnsValue"] = any(
            item.text == "return"
            and token_index + 1 < len(body)
            and body[token_index + 1].text != ";"
            for token_index, item in enumerate(body)
        )
        record["callSites"] = _call_sites(record["bodyTokens"], set(by_name))
        statements = _computation_statements(body)
        record["statements"] = statements
        record["bodyStartLine"] = tokens[record["bodyStart"]].line
        record["bodyEndLine"] = tokens[record["bodyEnd"]].line
        record["bodyStartOffset"] = tokens[record["bodyStart"]].offset + 1
        record["bodyEndOffset"] = tokens[record["bodyEnd"]].offset
        record["navigationLine"] = statements[0]["startLine"] if statements else record["line"]
        record["navigationColumn"] = statements[0]["column"] if statements else record["column"]
    return records


def _argument_groups(words: list[str]) -> list[list[str]]:
    result, pending, depth = [], [], 0
    for word in words:
        if word == "," and depth == 0:
            result.append(pending)
            pending = []
            continue
        pending.append(word)
        if word in ("(", "[", "{"):
            depth += 1
        elif word in (")", "]", "}"):
            depth -= 1
    if pending:
        result.append(pending)
    return result


def _parameter_records(tokens: list[_TokenValue]) -> list[dict[str, str]]:
    result = []
    for raw_words in _argument_groups([item.text for item in tokens]):
        semantic = ""
        if ":" in raw_words:
            colon = raw_words.index(":")
            semantic = next(
                (word for word in raw_words[colon + 1:] if _IDENTIFIER.fullmatch(word)),
                "",
            )
            words = raw_words[:colon]
        else:
            words = list(raw_words)
        words = words[:words.index("=")] if "=" in words else words
        identifiers = [word for word in words if _IDENTIFIER.fullmatch(word)]
        if len(identifiers) < 2:
            continue
        name = identifiers[-1]
        name_index = len(words) - 1 - list(reversed(words)).index(name)
        type_words = [
            word for word in words[:name_index]
            if word not in ("in", "out", "inout", "const", "precise", "uniform")
        ]
        type_name = " ".join(type_words).strip()
        result.append(
            {
                "name": name,
                "mode": "inout" if "inout" in words else "out" if "out" in words else "in",
                "type": type_name,
                "semantic": semantic,
            }
        )
    return result


def _function_interface(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded declaration-only summary for one parsed function."""

    parameters = list(record.get("parameters", []))[:MAX_INTERFACE_ITEMS]
    inputs: list[str] = []
    outputs: list[str] = []
    signature_parameters: list[str] = []
    for parameter in parameters:
        name = str(parameter.get("name", "")).strip()
        if _IDENTIFIER.fullmatch(name) is None:
            continue
        mode = str(parameter.get("mode", "in") or "in")
        type_name = " ".join(str(parameter.get("type", "") or "").split())
        if len(type_name) > MAX_INTERFACE_TEXT_CHARS:
            type_name = type_name[:MAX_INTERFACE_TEXT_CHARS]
        signature_parameters.append(
            " ".join(item for item in (mode, type_name, name) if item)
        )
        if mode in ("in", "inout") and name not in inputs:
            inputs.append(name)
        if mode in ("out", "inout") and name not in outputs:
            outputs.append(name)

    return_type = " ".join(str(record.get("returnType", "") or "").split())
    return_semantic = str(record.get("returnSemantic", "") or "").strip()
    if (return_type and return_type.lower() != "void") or record.get("returnsValue") is True:
        output = return_semantic if _IDENTIFIER.fullmatch(return_semantic) else "return"
        if output not in outputs:
            outputs.append(output)
    name = str(record.get("name", "") or "")
    signature = "%s%s(%s)" % (
        (return_type + " ") if return_type else "",
        name,
        ", ".join(signature_parameters),
    )
    if return_semantic and _IDENTIFIER.fullmatch(return_semantic):
        signature += " : " + return_semantic
    if len(signature) > MAX_SIGNATURE_CHARS:
        signature = signature[:MAX_SIGNATURE_CHARS - 3].rstrip() + "..."
    return {"signature": signature, "inputs": inputs, "outputs": outputs}


def _call_sites(words: list[str], functions: set[str]) -> list[dict[str, Any]]:
    result = []
    for index, word in enumerate(words[:-1]):
        if word not in functions or words[index + 1] != "(" or (index and words[index - 1] in (".", "::")):
            continue
        depth, end = 1, index + 2
        while end < len(words) and depth:
            if words[end] == "(": depth += 1
            elif words[end] == ")": depth -= 1
            end += 1
        if depth == 0:
            result.append({"function": word, "arguments": _argument_groups(words[index + 2:end - 1])})
    return result


def _lvalue(words: list[str]) -> str:
    text = "".join(words)
    return text if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", text) else ""


def _direct_parameter_effects(words: list[str], parameters: set[str]):
    reads, writes = set(), set()
    text = " ".join(words)
    for match in re.finditer(r"\b([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)+)\b", text):
        path = re.sub(r"\s+", "", match.group(1))
        if path.split(".")[0] not in parameters:
            continue
        tail = text[match.end():]
        assigned = re.match(r"\s*(?:\[[^;=]*?\]\s*)?([+*/%&|^-]?\s*)=(?!=)", tail)
        if assigned:
            writes.add(path)
        if not assigned or assigned.group(1).strip():
            reads.add(path)
    return reads, writes


def _parameter_effects(records: Mapping[str, Mapping[str, Any]], function: str, seen=(), memo=None):
    memo = {} if memo is None else memo
    if function in seen or len(seen) >= 12 or function not in records:
        return set(), set()
    if function in memo:
        return memo[function]
    record = records[function]
    names = {item["name"] for item in record["parameters"]}
    reads, writes = _direct_parameter_effects(record["bodyTokens"], names)
    for call in record["callSites"]:
        child = records.get(call["function"])
        if not child:
            continue
        child_reads, child_writes = _parameter_effects(records, call["function"], (*seen, function), memo)
        for parameter, argument in zip(child["parameters"], call["arguments"]):
            actual = _lvalue(argument)
            if not actual or actual.split(".")[0] not in names:
                continue
            prefix = parameter["name"] + "."
            reads.update(actual + value[len(parameter["name"]):] for value in child_reads if value.startswith(prefix))
            if parameter["mode"] in ("out", "inout"):
                writes.update(actual + value[len(parameter["name"]):] for value in child_writes if value.startswith(prefix))
    memo[function] = (reads, writes)
    return reads, writes


def called_parameter_effects(source: str, words: list[str], *, records=None, memo=None):
    """Propagate struct-field effects through actual out/inout argument bindings."""
    records = _unique_function_records(source) if records is None else records
    memo = {} if memo is None else memo
    reads, writes = set(), set()
    for call in _call_sites(words, set(records)):
        child = records[call["function"]]
        child_reads, child_writes = _parameter_effects(records, call["function"], memo=memo)
        for parameter, argument in zip(child["parameters"], call["arguments"]):
            actual = _lvalue(argument)
            if not actual:
                continue
            prefix = parameter["name"] + "."
            reads.update(actual + value[len(parameter["name"]):] for value in child_reads if value.startswith(prefix))
            if parameter["mode"] in ("out", "inout"):
                writes.update(actual + value[len(parameter["name"]):] for value in child_writes if value.startswith(prefix))
    return reads, writes


def _unique_function_records(source):
    return _unique_records(_function_records(source))


def _unique_records(records):
    return {record["name"]: record for record in records if sum(item["name"] == record["name"] for item in records) == 1}


def _value_origin(records, function, value):
    """Bind same-named helper parameters only through a unique actual call chain."""
    origin_function, origin_value = function, value
    for _ in range(12):
        record = records.get(origin_function)
        base = origin_value.split(".")[0]
        parameter = next((item for item in (record or {}).get("parameters", []) if item["name"] == base), None)
        if not parameter:
            # Two block-scoped variables may have the same spelling. Without a
            # full symbol table their identity is ambiguous; omit data edges.
            declarations = re.findall(r"\b([A-Za-z_]\w*)\s+" + re.escape(base) + r"(?=\s*(?:[;=\[]))",
                                      " ".join((record or {}).get("bodyTokens", [])))
            if sum(item not in _NON_FUNCTION_NAMES for item in declarations) > 1:
                return None
            return origin_function, origin_value
        sites = [(caller["name"], call) for caller in records.values() for call in caller["callSites"]
                 if call["function"] == origin_function]
        if len(sites) != 1 or parameter["mode"] not in ("out", "inout"):
            return function, value
        caller, call = sites[0]
        index = record["parameters"].index(parameter)
        actual = _lvalue(call["arguments"][index]) if index < len(call["arguments"]) else ""
        if not actual:
            return function, value
        origin_function, origin_value = caller, actual + origin_value[len(base):]
    return function, value


def _computation_statements(tokens: list[_TokenValue]) -> list[dict[str, Any]]:
    """Recognize executable lexical spans, excluding uninitialized declarations."""
    result = []
    pending: list[_TokenValue] = []
    for token in tokens:
        if token.text not in ("{", "}"):
            pending.append(token)
        if token.text != ";":
            continue
        words = [item.text for item in pending]
        executable = (any(word in words for word in ("=", "++", "--", "return", "discard", "break", "continue"))
                      or "(" in words)
        if pending and executable:
            result.append({"startLine": pending[0].line, "endLine": token.line,
                           "column": pending[0].column, "tokens": words})
        pending = []
    return result


def computation_ranges(source: str) -> list[dict[str, Any]]:
    """Host-owned navigation/complexity hints, without AI semantic categories."""
    result = []
    for record in _function_records(source):
        statements = record["statements"]
        if not statements:
            continue
        result.append({"function": record["name"],
                       "sourceRange": {"startLine": statements[0]["startLine"], "endLine": statements[-1]["endLine"]},
                       "statementCount": len(statements),
                       "anonymousTemporaries": len({word for item in statements for word in item["tokens"]
                                                    if re.fullmatch(r"_\d+(?:_\d+)*", word)})})
    return result


def external_value_hints(source: str, entry_point: str) -> list[str]:
    """Compact global/entry input hints; local values stay discoverable in source."""
    names, pending, depth = set(), [], 0
    for token in _tokens(source):
        if token.text == "{":
            depth += 1
            pending = []
        elif token.text == "}":
            depth = max(0, depth - 1)
            pending = []
        elif depth == 0:
            pending.append(token.text)
            if token.text == ";":
                words = pending
                for delimiter in ("=", ":", "["):
                    if delimiter in words:
                        words = words[:words.index(delimiter)]
                identifiers = [word for word in words if _IDENTIFIER.fullmatch(word)]
                if "(" not in words and len(identifiers) >= 2:
                    names.add(identifiers[-1])
                pending = []
    for record in _function_records(source):
        if record["name"] == entry_point:
            names.update(item["name"] for item in record["parameters"])
    return sorted(names)


def bind_computation_range(source: str, function: str, requested: Any = None, *, records=None) -> dict[str, Any]:
    """Resolve one computation span inside an actual function body."""
    records = [record for record in (_function_records(source) if records is None else records) if record["name"] == function]
    if requested is not None:
        if (not isinstance(requested, Mapping) or set(requested) != {"startLine", "endLine"}
                or any(type(requested.get(key)) is not int or requested[key] < 1 for key in ("startLine", "endLine"))
                or requested["endLine"] < requested["startLine"]):
            raise ShaderFlowGraphError("sourceRange needs positive startLine/endLine in source order")
        records = [record for record in records if record["bodyStartLine"] <= requested["startLine"]
                   and requested["endLine"] <= record["bodyEndLine"]]
    if len(records) != 1:
        raise ShaderFlowGraphError("sourceRange must identify one actual function body, not parameters or resource declarations")
    record = records[0]
    statements = record["statements"]
    if requested is not None:
        statements = [item for item in statements if requested["startLine"] <= item["startLine"]
                      and item["endLine"] <= requested["endLine"]]
    if not statements:
        raise ShaderFlowGraphError("sourceRange contains no complete executable calculation; declarations/comments are not computation nodes")
    return {"sourceRange": {"startLine": statements[0]["startLine"], "endLine": statements[-1]["endLine"]},
            "navigationLine": statements[0]["startLine"], "navigationColumn": statements[0]["column"],
            "statementCount": len(statements), "tokens": [word for item in statements for word in item["tokens"]]}


def _default_description(name: str, is_entry: bool) -> str:
    """What the Host can say about a function it has only parsed.

    It can say that a function is the entry point, because that is a fact about
    the program. It cannot say what ``ApplyFoo`` does: reading meaning out of an
    identifier is guessing from a naming convention, and the guess then travels
    into the diagram as if someone had established it. So an undescribed
    function says it is undescribed, and the description is the Agent's to write
    (``module_description_template`` / ``describe_shader_modules``) once it has
    read the code and, where the draw resolves to one, the unpacked material.
    """

    if is_entry:
        return "Shader entry point that coordinates the final stage result."
    return "Not yet described."


def _source_function_comment(source: str, record: Mapping[str, Any]) -> str:
    """Read only a plain-language comment immediately before a declaration.

    An older module description can disagree with an edited source comment.
    The final source is the diagram's authority; absent or code-like comments
    leave the reviewed description available as a fallback.
    """

    lines = source.splitlines()
    index = int(record.get("declarationLine", record.get("line", 0))) - 2
    while index >= 0 and re.fullmatch(r"\[[^\[\]]+\]", lines[index].strip()):
        index -= 1  # HLSL attributes between the comment and function.
    if index < 0:
        return ""
    current = lines[index].strip()
    parts: list[str] = []
    if current.startswith("//"):
        while index >= 0 and lines[index].strip().startswith("//"):
            parts.append(lines[index].strip()[2:].strip())
            index -= 1
        parts.reverse()
    elif current.endswith("*/"):
        found_start = False
        while index >= 0:
            raw = lines[index].strip()
            if "/*" in raw:
                before, after = raw.split("/*", 1)
                if before:
                    break
                parts.append(after.replace("*/", "").lstrip("* ").strip())
                found_start = True
                break
            if raw and not raw.startswith("*"):
                break
            parts.append(raw.lstrip("* ").replace("*/", "").strip())
            index -= 1
        if not found_start:
            return ""
        parts.reverse()
    else:
        return ""
    comment = " ".join(part for part in parts if part).strip()
    if not comment or len(comment) > MAX_DESCRIPTION_CHARS or _contains_implementation_text(comment):
        return ""
    return comment


def _fallback_entry_record(source: str, entry_point: str) -> dict[str, Any] | None:
    if not entry_point or _IDENTIFIER.fullmatch(entry_point) is None:
        return None
    match = re.search(
        r"\b%s\b" % re.escape(entry_point),
        _masked_source(source),
    )
    if match is None:
        return None
    offset = match.start()
    line_start = source.rfind("\n", 0, offset) + 1
    return {
        "name": entry_point,
        "line": source.count("\n", 0, offset) + 1,
        "column": offset - line_start + 1,
        "bodyStart": 0,
        "bodyEnd": 0,
        "parameters": [],
        "returnType": "",
        "returnSemantic": "",
        "returnsValue": False,
        "calls": [],
    }


def _module_function_names(source: str) -> list[str]:
    records = _function_records(source)
    names: list[str] = []
    seen: set[str] = set()
    for record in records:
        name = record["name"]
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def module_description_template(source: str) -> dict[str, Any]:
    """Return the first bounded page of the editable description template.

    The final document may cover more functions than the visible graph. Begin
    returns a separate paging record so an Agent can read later function pages
    from the outline and merge their descriptions into the same draft.
    """

    names = _module_function_names(source)[:MAX_GRAPH_NODES]
    return {
        "schema": MODULE_DESCRIPTIONS_SCHEMA,
        "schemaVersion": MODULE_DESCRIPTIONS_VERSION,
        "modules": [
            {"name": name, "description": ""}
            for name in names
        ],
    }


def module_description_paging(source: str) -> dict[str, Any]:
    """Describe complete function coverage without expanding the begin result."""

    names = _module_function_names(source)
    returned = min(len(names), MAX_GRAPH_NODES)
    return {
        "schema": "replaylab-shader-module-description-paging/v1",
        "totalFunctions": len(names),
        "templateEntries": returned,
        "hasMore": returned < len(names),
        "nextOutlineOffset": returned if returned < len(names) else None,
        "maximumDescriptionEntries": MAX_MODULE_DESCRIPTION_ENTRIES,
        "maximumDocumentBytes": MAX_MODULE_DESCRIPTIONS_BYTES,
        "mergeKey": "modules[].name",
    }


def validate_module_descriptions(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schemaVersion",
        "modules",
    }:
        received = sorted(str(key) for key in value) if isinstance(value, Mapping) else type(value).__name__
        raise ShaderFlowGraphError(
            "module description fields are invalid: expected exactly schema, schemaVersion, modules "
            "(schema=%r, schemaVersion=%r, modules=[{name, description}]); received %s"
            % (MODULE_DESCRIPTIONS_SCHEMA, MODULE_DESCRIPTIONS_VERSION, received)
        )
    if (
        value.get("schema") != MODULE_DESCRIPTIONS_SCHEMA
        or isinstance(value.get("schemaVersion"), bool)
        or value.get("schemaVersion") != MODULE_DESCRIPTIONS_VERSION
    ):
        raise ShaderFlowGraphError("module description schema is invalid")
    modules = value.get("modules")
    if not isinstance(modules, list) or len(modules) > MAX_MODULE_DESCRIPTION_ENTRIES:
        raise ShaderFlowGraphError("module descriptions are invalid or oversized")
    result: dict[str, str] = {}
    for raw in modules:
        if not isinstance(raw, Mapping) or set(raw) != {"name", "description"}:
            raise ShaderFlowGraphError(
                "module description entry is invalid: each entry is exactly {name, description}; received %s"
                % (sorted(str(key) for key in raw) if isinstance(raw, Mapping) else type(raw).__name__)
            )
        name = _bounded_text(raw.get("name"), "module name", 256)
        if _IDENTIFIER.fullmatch(name) is None or name in result:
            raise ShaderFlowGraphError("module description name is invalid or duplicated")
        description = _bounded_text(
            raw.get("description"),
            "module description",
            MAX_DESCRIPTION_CHARS,
            allow_empty=True,
        )
        if _contains_implementation_text(description):
            raise ShaderFlowGraphError(
                "module description must explain the function without source-code implementation text"
            )
        result[name] = description
    return result


def load_module_descriptions(path: Path) -> dict[str, str]:
    try:
        value = read_json_object(
            path,
            MAX_MODULE_DESCRIPTIONS_BYTES,
            "Shader module descriptions",
        )
    except ValueError as exc:
        raise ShaderFlowGraphError(str(exc)) from exc
    return validate_module_descriptions(value)


def _ordered_record_indexes(
    records: list[dict[str, Any]], entry_point: str
) -> list[int]:
    by_name: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        by_name.setdefault(record["name"], []).append(index)
    queue = list(by_name.get(entry_point, []))
    if not queue:
        # No verified entry declaration: retain the legacy bounded outline.
        # With an entry, only its reachable call chain belongs in this graph.
        return list(range(len(records)))
    ordered: list[int] = []
    queued = set(queue)
    cursor = 0
    while cursor < len(queue):
        index = queue[cursor]
        cursor += 1
        if index in ordered:
            continue
        ordered.append(index)
        for called_name in records[index]["calls"]:
            for called_index in by_name.get(called_name, []):
                if called_index not in queued:
                    queued.add(called_index)
                    queue.append(called_index)
    return ordered


def build_shader_flow_graph(
    source: str,
    *,
    workspace_id: str,
    source_path: str,
    source_sha256: str,
    source_revision: int,
    stage: str,
    entry_point: str,
    generated_at: str,
    origin: str,
    descriptions: Mapping[str, str] | None = None,
    flow_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic function-call graph bound to final HLSL bytes."""

    if not isinstance(source, str) or "\x00" in source:
        raise ShaderFlowGraphError("organized HLSL must be NUL-free text")
    if len(source.encode("utf-8")) > 4 * 1024 * 1024:
        raise ShaderFlowGraphError("organized HLSL exceeds the flow graph source limit")
    workspace_id = str(workspace_id or "").strip().lower()
    source_sha256 = str(source_sha256 or "").strip().lower()
    if _HEX32.fullmatch(workspace_id) is None:
        raise ShaderFlowGraphError("flow graph workspaceId is invalid")
    if _SHA256.fullmatch(source_sha256) is None:
        raise ShaderFlowGraphError("flow graph source SHA-256 is invalid")
    if _sha256(source.encode("utf-8")) != source_sha256:
        raise ShaderFlowGraphError("flow graph source bytes do not match sourceSha256")
    if (
        isinstance(source_revision, bool)
        or not isinstance(source_revision, int)
        or source_revision < 0
        or (source_revision == 0 and origin != "editable-draft")
    ):
        raise ShaderFlowGraphError("flow graph source revision is invalid")
    source_path = _normalized_relative_path(source_path, "flow graph source path")
    stage = _bounded_text(stage, "flow graph stage", 64)
    entry_point = _bounded_text(
        entry_point, "flow graph entry point", 512, allow_empty=True
    )
    generated_at = _bounded_text(generated_at, "flow graph generation time", 128)
    if origin not in ("organized-proposal", "recovered-baseline", "editable-draft"):
        raise ShaderFlowGraphError("flow graph origin is invalid")
    clean_descriptions = dict(descriptions or {})
    for name, description in clean_descriptions.items():
        if _IDENTIFIER.fullmatch(str(name)) is None:
            raise ShaderFlowGraphError("flow graph description name is invalid")
        clean_description = _bounded_text(
            description,
            "flow graph description",
            MAX_DESCRIPTION_CHARS,
            allow_empty=True,
        )
        if _contains_implementation_text(clean_description):
            raise ShaderFlowGraphError(
                "flow graph description must explain the function without source-code implementation text"
            )

    records = _function_records(source)
    if entry_point and all(record["name"] != entry_point for record in records):
        fallback_entry = _fallback_entry_record(source, entry_point)
        if fallback_entry is not None:
            records.append(fallback_entry)
    ordered_indexes = _ordered_record_indexes(records, entry_point)
    selected_indexes = ordered_indexes[:MAX_GRAPH_NODES]
    selected = set(selected_indexes)
    node_ids: dict[int, str] = {}
    nodes: list[dict[str, Any]] = []
    for index in selected_indexes:
        record = records[index]
        identity = "%s:%s:%d:%d" % (
            workspace_id,
            record["name"],
            record["line"],
            record["column"],
        )
        node_id = "fn-" + _sha256(identity.encode("utf-8"))[:24]
        node_ids[index] = node_id
        is_entry = record["name"] == entry_point
        supplied = clean_descriptions.get(record["name"], "").strip()
        source_comment = _source_function_comment(source, record)
        interface = _function_interface(record)
        nodes.append(
            {
                "id": node_id,
                "name": record["name"],
                "label": record["name"],
                "kind": "entry-point" if is_entry else "function",
                "description": (source_comment or supplied or
                                _default_description(record["name"], is_entry)),
                "signature": interface["signature"],
                "inputs": interface["inputs"],
                "outputs": interface["outputs"],
                "source": {
                    "workspaceId": workspace_id,
                    "documentId": workspace_id,
                    "path": source_path,
                    "line": record.get("navigationLine", record["line"]),
                    "column": record.get("navigationColumn", record["column"]),
                },
            }
        )

    by_name: dict[str, list[int]] = {}
    for index in ordered_indexes:
        by_name.setdefault(records[index]["name"], []).append(index)
    all_edge_indexes: list[tuple[int, int]] = []
    seen_edges: set[tuple[int, int]] = set()
    for caller_index in ordered_indexes:
        for called_name in records[caller_index]["calls"]:
            for callee_index in by_name.get(called_name, []):
                edge_key = (caller_index, callee_index)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                all_edge_indexes.append(edge_key)
    selected_edges = [
        edge for edge in all_edge_indexes if edge[0] in selected and edge[1] in selected
    ]
    edges = [
                    {
                        "source": node_ids[edge[0]],
                        "target": node_ids[edge[1]],
                        "kind": "calls",
                    }
        for edge in selected_edges[:MAX_GRAPH_EDGES]
    ]
    # ``flow_spec`` remains in this public API for existing callers. Its stage
    # ranges are review evidence stored as ``flow-spec.json`` by organization;
    # they must never replace the published function/call graph.
    del flow_spec
    material = {
        "schema": FLOW_GRAPH_SCHEMA,
        "schemaVersion": FLOW_GRAPH_VERSION,
        "workspaceId": workspace_id,
        "stage": stage,
        "entryPoint": entry_point,
        "sourcePath": source_path,
        "sourceSha256": source_sha256,
        "sourceRevision": source_revision,
        "origin": origin,
        "nodes": nodes,
        "edges": edges,
        "limits": {
            "maxNodes": MAX_GRAPH_NODES,
            "maxEdges": MAX_GRAPH_EDGES,
        },
        "truncated": {
            "nodes": len(ordered_indexes) > len(nodes),
            "edges": len(all_edge_indexes) > len(edges),
            "omittedNodes": max(0, len(ordered_indexes) - len(nodes)),
            "omittedEdges": max(0, len(all_edge_indexes) - len(edges)),
        },
        "policy": {
            "navigationOnly": True,
            "physicalEvidence": False,
            "compileAuthorization": False,
            "applyAuthorization": False,
            "callEdgesDerivedFromFinalSource": True,
            "descriptionsMayBeAiAuthored": True,
        },
    }
    graph = dict(material)
    graph["graphId"] = _sha256(_canonical_bytes(material))[:32]
    graph["generatedAt"] = generated_at
    return validate_shader_flow_graph(
        graph,
        expected_workspace_id=workspace_id,
        expected_source_sha256=source_sha256,
        expected_source_revision=source_revision,
        expected_source_path=source_path,
    )


def _algorithm_nodes(source: str, spec: Mapping[str, Any], workspace_id: str, source_path: str,
                     *, records=None, descriptions: Mapping[str, str] | None = None):
    """Build the legacy stage shape for compatibility inspection only.

    Organization stores ``spec`` itself as internal evidence. Published graph
    generation intentionally never calls this helper.
    """

    nodes, edges = [], []
    record_list = _function_records(source) if records is None else records
    records = _unique_records(record_list)
    for stage in spec["stages"]:
        bound = bind_computation_range(source, stage["function"], stage.get("sourceRange"), records=record_list)
        description = str((descriptions or {}).get(stage["function"]) or stage["label"])
        nodes.append({"id": "stage-" + stage["id"], "name": stage["function"],
                      "label": stage["label"], "kind": "computation", "description": description,
                      "source": {"workspaceId": workspace_id, "documentId": workspace_id, "path": source_path,
                                 "line": bound["navigationLine"], "column": bound["navigationColumn"]},
                      "sourceRange": bound["sourceRange"], "reads": list(stage.get("reads", [])),
                      "writes": list(stage.get("writes", [])), "inferred": True})
    by_id = {stage["id"]: stage for stage in spec["stages"]}
    indexed_edges = {}
    producers = {}
    for index, stage in enumerate(spec["stages"]):
        for value in stage.get("reads", []):
            key = value if value.endswith(".return") else _value_origin(records, stage["function"], value)
            if key is None:
                continue
            producer = producers.get(key)
            if not producer and isinstance(key, tuple):
                parent_value = key[1]
                while "." in parent_value and not producer:
                    parent_value = parent_value.rsplit(".", 1)[0]
                    producer = producers.get((key[0], parent_value))
            if producer:
                edge_key = ("stage-" + producer, "stage-" + stage["id"])
                edge = indexed_edges.setdefault(edge_key, {"source": edge_key[0], "target": edge_key[1], "kind": "data", "values": []})
                edge["kind"] = "data"
                edge.setdefault("values", [])
                if value not in edge["values"]:
                    edge["values"].append(value)
        for value in stage.get("writes", []):
            key = value if value.endswith(".return") else _value_origin(records, stage["function"], value)
            if key is None:
                continue
            producers[key] = stage["id"]
        for target_id in stage.get("next", []):
            if target_id not in by_id:
                raise ShaderFlowGraphError("algorithm edge points to an unknown computation stage")
            edge_key = ("stage-" + stage["id"], "stage-" + target_id)
            indexed_edges.setdefault(edge_key, {"source": edge_key[0], "target": edge_key[1], "kind": "control"})
    edges = list(indexed_edges.values())
    if len(nodes) > MAX_GRAPH_NODES or len(edges) > MAX_GRAPH_EDGES:
        raise ShaderFlowGraphError("algorithm graph exceeds the fixed node/edge limit")
    return nodes, edges


def validate_shader_flow_graph(
    value: Mapping[str, Any],
    *,
    expected_workspace_id: str = "",
    expected_source_sha256: str = "",
    expected_source_revision: int | None = None,
    expected_source_path: str = "",
) -> dict[str, Any]:
    expected_fields = {
        "schema",
        "schemaVersion",
        "graphId",
        "workspaceId",
        "stage",
        "entryPoint",
        "sourcePath",
        "sourceSha256",
        "sourceRevision",
        "generatedAt",
        "origin",
        "nodes",
        "edges",
        "limits",
        "truncated",
        "policy",
    }
    if (not isinstance(value, Mapping) or not expected_fields.issubset(value)
            or set(value) - expected_fields - {"graphKind", "layoutDirection"}):
        raise ShaderFlowGraphError("Shader flow graph fields are invalid")
    graph = copy.deepcopy(dict(value))
    if (
        graph.get("schema") != FLOW_GRAPH_SCHEMA
        or graph.get("schemaVersion") != FLOW_GRAPH_VERSION
    ):
        raise ShaderFlowGraphError("Shader flow graph schema is invalid")
    graph_id = str(graph.get("graphId", "") or "").lower()
    workspace_id = str(graph.get("workspaceId", "") or "").lower()
    source_sha256 = str(graph.get("sourceSha256", "") or "").lower()
    if _HEX32.fullmatch(graph_id) is None or _HEX32.fullmatch(workspace_id) is None:
        raise ShaderFlowGraphError("Shader flow graph identity is invalid")
    if _SHA256.fullmatch(source_sha256) is None:
        raise ShaderFlowGraphError("Shader flow graph source SHA-256 is invalid")
    revision = graph.get("sourceRevision")
    if (isinstance(revision, bool) or not isinstance(revision, int)
            or revision < 0
            or (revision == 0 and graph.get("origin") != "editable-draft")):
        raise ShaderFlowGraphError("Shader flow graph source revision is invalid")
    _bounded_text(graph.get("stage"), "Shader flow graph stage", 64)
    _bounded_text(
        graph.get("entryPoint"),
        "Shader flow graph entry point",
        512,
        allow_empty=True,
    )
    graph_source_path = _normalized_relative_path(
        graph.get("sourcePath"), "Shader flow graph source path"
    )
    _bounded_text(graph.get("generatedAt"), "Shader flow graph generation time", 128)
    if graph.get("origin") not in ("organized-proposal", "recovered-baseline", "editable-draft"):
        raise ShaderFlowGraphError("Shader flow graph origin is invalid")
    if graph.get("limits") != {
        "maxNodes": MAX_GRAPH_NODES,
        "maxEdges": MAX_GRAPH_EDGES,
    }:
        raise ShaderFlowGraphError("Shader flow graph limits are invalid")
    algorithm = graph.get("graphKind") == "algorithm-dataflow"
    if ("graphKind" in graph or "layoutDirection" in graph) and (not algorithm or graph.get("layoutDirection") != "TD"):
        raise ShaderFlowGraphError("Shader algorithm graph must use top-down data flow")
    expected_policy = {
        "navigationOnly": True,
        "physicalEvidence": False,
        "compileAuthorization": False,
        "applyAuthorization": False,
        "callEdgesDerivedFromFinalSource": True,
        "descriptionsMayBeAiAuthored": True,
    }
    if algorithm:
        expected_policy.update(callEdgesDerivedFromFinalSource=False,
                               computationRangesBoundToFinalSource=True, dataEdgesAreLexicallyBound=True)
    if graph.get("policy") != expected_policy:
        raise ShaderFlowGraphError("Shader flow graph policy is invalid")
    truncated = graph.get("truncated")
    if not isinstance(truncated, Mapping) or set(truncated) != {
        "nodes",
        "edges",
        "omittedNodes",
        "omittedEdges",
    }:
        raise ShaderFlowGraphError("Shader flow graph truncation metadata is invalid")
    if type(truncated.get("nodes")) is not bool or type(truncated.get("edges")) is not bool:
        raise ShaderFlowGraphError("Shader flow graph truncation flags are invalid")
    for field in ("omittedNodes", "omittedEdges"):
        count = truncated.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ShaderFlowGraphError("Shader flow graph truncation count is invalid")

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or len(nodes) > MAX_GRAPH_NODES:
        raise ShaderFlowGraphError("Shader flow graph nodes are invalid or oversized")
    if not isinstance(edges, list) or len(edges) > MAX_GRAPH_EDGES:
        raise ShaderFlowGraphError("Shader flow graph edges are invalid or oversized")
    node_ids: set[str] = set()
    for node in nodes:
        required_node_fields = {
            "id",
            "name",
            "label",
            "kind",
            "description",
            "source",
        }
        allowed_node_fields = set(required_node_fields)
        if algorithm:
            required_node_fields.update(("sourceRange", "reads", "writes", "inferred"))
            allowed_node_fields = set(required_node_fields)
        else:
            # Interface fields were added without changing the v1 identifier so
            # already-published function graphs remain readable. New Host graphs
            # always carry all three fields.
            allowed_node_fields.update(("signature", "inputs", "outputs"))
        if (
            not isinstance(node, Mapping)
            or not required_node_fields.issubset(node)
            or set(node) - allowed_node_fields
        ):
            raise ShaderFlowGraphError("Shader flow graph node fields are invalid")
        node_id = _bounded_text(node.get("id"), "Shader flow graph node id", 128)
        name = _bounded_text(node.get("name"), "Shader flow graph node name", 256)
        label = _bounded_text(node.get("label"), "Shader flow graph node label", 256)
        if (
            _NODE_ID.fullmatch(node_id) is None
            or _IDENTIFIER.fullmatch(name) is None
            or (not algorithm and label != name)
            or node_id in node_ids
            or node.get("kind") not in (("computation",) if algorithm else ("entry-point", "function"))
        ):
            raise ShaderFlowGraphError("Shader flow graph node identity is invalid")
        node_ids.add(node_id)
        _bounded_text(
            node.get("description"),
            "Shader flow graph node description",
            MAX_DESCRIPTION_CHARS,
        )
        if not algorithm and _contains_implementation_text(str(node.get("description", ""))):
            raise ShaderFlowGraphError(
                "Shader flow graph description contains implementation text"
            )
        if not algorithm:
            if "signature" in node:
                signature = _bounded_text(
                    node.get("signature"),
                    "Shader flow graph function signature",
                    MAX_SIGNATURE_CHARS,
                )
                if any(marker in signature for marker in (";", "{", "}", "```")):
                    raise ShaderFlowGraphError(
                        "Shader flow graph signature contains implementation text"
                    )
            for key in ("inputs", "outputs"):
                if key not in node:
                    continue
                values = node[key]
                if not isinstance(values, list) or len(values) > MAX_INTERFACE_ITEMS:
                    raise ShaderFlowGraphError("Shader flow graph function interface is invalid")
                seen_values: set[str] = set()
                for item in values:
                    value_name = _bounded_text(
                        item,
                        "Shader flow graph function interface value",
                        MAX_INTERFACE_TEXT_CHARS,
                    )
                    if _IDENTIFIER.fullmatch(value_name) is None or value_name in seen_values:
                        raise ShaderFlowGraphError("Shader flow graph function interface is invalid")
                    seen_values.add(value_name)
        location = node.get("source")
        if not isinstance(location, Mapping) or set(location) != {
            "workspaceId",
            "documentId",
            "path",
            "line",
            "column",
        }:
            raise ShaderFlowGraphError("Shader flow graph source location is invalid")
        location_path = _normalized_relative_path(
            location.get("path"), "Shader flow graph node path"
        )
        if (
            location.get("workspaceId") != workspace_id
            or location.get("documentId") != workspace_id
            or location_path != graph_source_path
        ):
            raise ShaderFlowGraphError("Shader flow graph node workspace binding differs")
        for field in ("line", "column"):
            number = location.get(field)
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise ShaderFlowGraphError("Shader flow graph source location is invalid")
        if algorithm:
            span = node.get("sourceRange")
            if (not isinstance(span, Mapping) or set(span) != {"startLine", "endLine"}
                    or any(type(span.get(key)) is not int for key in ("startLine", "endLine"))
                    or not 1 <= span["startLine"] <= location["line"] <= span["endLine"]
                    or node.get("inferred") is not True):
                raise ShaderFlowGraphError("Shader computation source range is invalid")
            for key in ("reads", "writes"):
                if not isinstance(node[key], list) or len(node[key]) > 32:
                    raise ShaderFlowGraphError("Shader computation values are invalid")
                for item in node[key]:
                    _bounded_text(item, "Shader computation value", 72)
    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        if (not isinstance(edge, Mapping) or not {"source", "target", "kind"}.issubset(edge)
                or set(edge) - {"source", "target", "kind"} - ({"values"} if algorithm else set())):
            raise ShaderFlowGraphError("Shader flow graph edge fields are invalid")
        edge_key = (str(edge.get("source", "")), str(edge.get("target", "")))
        if (
            edge.get("kind") not in (("data", "control") if algorithm else ("calls",))
            or edge_key[0] not in node_ids
            or edge_key[1] not in node_ids
            or edge_key in seen_edges
        ):
            raise ShaderFlowGraphError("Shader flow graph edge identity is invalid")
        seen_edges.add(edge_key)
        if "values" in edge:
            if edge["kind"] != "data" or not isinstance(edge["values"], list) or not 1 <= len(edge["values"]) <= 32:
                raise ShaderFlowGraphError("Shader data edge values are invalid")
            for item in edge["values"]:
                _bounded_text(item, "Shader data edge value", 72)

    material = dict(graph)
    material.pop("graphId", None)
    material.pop("generatedAt", None)
    if _sha256(_canonical_bytes(material))[:32] != graph_id:
        raise ShaderFlowGraphError("Shader flow graph graphId does not match its contents")
    if expected_workspace_id and workspace_id != expected_workspace_id:
        raise ShaderFlowGraphError("Shader flow graph belongs to a different workspace")
    if expected_source_sha256 and source_sha256 != expected_source_sha256:
        raise ShaderFlowGraphError("Shader flow graph belongs to different source bytes")
    if expected_source_revision is not None and revision != expected_source_revision:
        raise ShaderFlowGraphError("Shader flow graph belongs to a different source revision")
    if expected_source_path:
        expected_path = _normalized_relative_path(
            expected_source_path, "expected Shader flow graph source path"
        )
        if (
            graph_source_path != expected_path
            or any(node["source"]["path"] != expected_path for node in nodes)
        ):
            raise ShaderFlowGraphError("Shader flow graph node path differs from its source")
    return graph


def load_workspace_flow_graph_status(
    workspace_root: Path,
    *,
    workspace_id: str,
    source_sha256: str,
    source_revision: int,
    source_path: str,
) -> dict[str, Any]:
    """Load optional graph metadata without making it workspace authority."""

    root = workspace_root.resolve()
    path = root / "ai" / "flow-graph.json"
    unavailable = {
        "available": False,
        "status": "unavailable",
        "stale": False,
        "artifactPath": str(path),
    }
    if not path.exists() and not path.is_symlink():
        return unavailable
    try:
        if path.is_symlink() or not is_within(root, path.resolve()):
            raise ShaderFlowGraphError("workspace Shader flow graph escapes its workspace")
        if not path.is_file():
            raise ShaderFlowGraphError(
                "workspace Shader flow graph must be a regular file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_FLOW_GRAPH_BYTES:
            raise ShaderFlowGraphError(
                "workspace Shader flow graph is empty or oversized")
        raw = path.read_bytes()
        if len(raw) != size:
            raise ShaderFlowGraphError(
                "workspace Shader flow graph changed while it was read")
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ShaderFlowGraphError(
                "workspace Shader flow graph is invalid JSON") from exc
        graph = validate_shader_flow_graph(value)
        stale = any(
            (
                graph["workspaceId"] != workspace_id,
                graph["sourceSha256"] != source_sha256,
                graph["sourceRevision"] != source_revision,
                graph["sourcePath"] != source_path,
            )
        )
        graph.update(
            {
                "available": True,
                "status": "stale" if stale else "current",
                "stale": stale,
                "artifactPath": str(path),
                "artifactSha256": _sha256(raw),
            }
        )
        return graph
    except (ShaderFlowGraphError, ValueError, OSError) as exc:
        invalid = dict(unavailable)
        invalid.update({"status": "invalid", "error": str(exc)[:1024]})
        return invalid


__all__ = [
    "FLOW_GRAPH_SCHEMA",
    "FLOW_GRAPH_VERSION",
    "MAX_FLOW_GRAPH_BYTES",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_NODES",
    "MAX_MODULE_DESCRIPTION_ENTRIES",
    "MAX_MODULE_DESCRIPTIONS_BYTES",
    "MODULE_DESCRIPTIONS_SCHEMA",
    "MODULE_DESCRIPTIONS_VERSION",
    "ShaderFlowGraphError",
    "build_shader_flow_graph",
    "load_module_descriptions",
    "load_workspace_flow_graph_status",
    "module_description_paging",
    "module_description_template",
    "validate_module_descriptions",
    "validate_shader_flow_graph",
]
