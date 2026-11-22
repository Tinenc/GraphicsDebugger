"""Persistent value-debug recipes and conservative source anchors.

A snapshot stores a location and display options, never compiled code, a replay
resource, an EID, or an image. Resolution reads the caller's current source and
returns UTF-8 offsets for a fresh inspection/compile. This module has no Qt or
RenderDoc dependency and supports the embedded Python 3.6 runtime.
"""
import copy
import contextlib
import collections
import hashlib
import json
import math
import ntpath
import os
import posixpath
import re
import tempfile
import threading
import time
import uuid
import importlib.util

try:
    from . import shader_structural_anchors as _structural
except (ImportError, ValueError):
    _spec = importlib.util.spec_from_file_location('_bread_snapshot_structural',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shader_structural_anchors.py'))
    _structural = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_structural)


SCHEMA = "bread-shader-visual-snapshots/v1"
SNAPSHOT_SCHEMA = "bread-shader-visual-snapshot/v1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOTS = 128
MAX_FUNCTION_TOKENS = 32768
MAX_EXPRESSION_BYTES = 65536
LOCK_TIMEOUT_SECONDS = 2.0
MAX_CACHED_SOURCE_BYTES = 512 * 1024
MAX_CACHED_TOKENS = 65536
MAX_LEGACY_STORE_FILES = 4096
MAX_LEGACY_STORE_BYTES = 32 * 1024 * 1024
_TOKEN = re.compile(r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|"
                    r'(?P<string>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|'
                    r"(?P<word>[A-Za-z_]\w*)|"
                    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z0-9]*)|"
                    r"(?P<op><<=|>>=|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|&&|\|\||==|!=|<=|>=|<<|>>|.)")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_LEX_CACHE = collections.OrderedDict()
_LEX_CACHE_LOCK = threading.RLock()
_ASSIGNMENTS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


class SnapshotError(ValueError):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, detail
        ValueError.__init__(self, code + (": " + detail if detail else ""))


def _fail(code, detail=""):
    raise SnapshotError(code, detail)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path(value):
    # Do not resolve symlinks or touch the source filesystem. Windows source
    # identities retain Windows path semantics even when a recipe is read on Linux.
    # A remote locator is opaque: POSIX normpath would collapse its :// and
    # silently change the transport identity. Its path remains case-sensitive.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return value
    windows = value.replace("/", "\\")
    if windows.upper().startswith("\\\\?\\UNC\\"):
        value = "\\\\" + windows[8:]
    elif windows.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:\\", windows[4:]):
        value = windows[4:]
    if re.match(r"^[A-Za-z]:[/\\]", value) or value.startswith(("\\\\", "//")):
        return ntpath.normcase(ntpath.normpath(value)).replace("\\", "/")
    return posixpath.normpath(value.replace("\\", "/"))


def _legacy_path(value):
    """The v1 spelling used by existing on-disk keys, never a new authority."""
    if re.match(r"^[A-Za-z]:[/\\]", value) or value.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(value)).replace("\\", "/")
    return posixpath.normpath(value.replace("\\", "/"))


def _identity_key(identity):
    return _hash(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def normalize_identity(identity):
    if not isinstance(identity, dict):
        _fail("snapshot-invalid-identity")
    result = {}
    for key in ("workspace", "path", "stage", "originalShader"):
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 8192:
            _fail("snapshot-invalid-identity", key)
        result[key] = _path(value) if key in {"workspace", "path"} else value
    return result


def _options(options):
    defaults = {"mode": "custom", "rangeMin": 0.0, "rangeMax": 1.0, "channel": -1,
                "targetIndex": -1, "iteration": 0, "independentFloatOutput": False}
    if options is not None:
        if not isinstance(options, dict) or set(options) - set(defaults):
            _fail("snapshot-invalid-options")
        defaults.update(options)
    if not isinstance(defaults["mode"], str) or defaults["mode"] not in {"custom", "raw", "grayscale", "normal", "range", "coverage"}:
        _fail("snapshot-invalid-options", "mode")
    for key, low, high in (("channel", -1, 3), ("targetIndex", -1, 7), ("iteration", 0, 65535)):
        if type(defaults[key]) is not int or not low <= defaults[key] <= high:
            _fail("snapshot-invalid-options", key)
    if type(defaults["independentFloatOutput"]) is not bool:
        _fail("snapshot-invalid-options", "independentFloatOutput")
    for key in ("rangeMin", "rangeMax"):
        if type(defaults[key]) not in (int, float) or not math.isfinite(defaults[key]):
            _fail("snapshot-invalid-options", key)
        defaults[key] = float(defaults[key])
    if defaults["rangeMin"] >= defaults["rangeMax"]:
        _fail("snapshot-invalid-options", "range")
    return defaults


def _text(value, limit, field):
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > limit:
        _fail("snapshot-invalid-metadata", field)
    return value


def _source(source):
    if not isinstance(source, str) or "\0" in source or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        _fail("snapshot-invalid-source")
    return source


def _lex(source):
    tokens, pairs, stack = [], {}, []
    directives = list(re.finditer(r"(?m)^[ \t]*#[^\n]*(?:\\\n[^\n]*)*", source))
    directive = 0
    for match in _TOKEN.finditer(source):
        if match.lastgroup in {"space", "comment"}:
            continue
        while directive < len(directives) and directives[directive].end() <= match.start():
            directive += 1
        if directive < len(directives) and directives[directive].start() <= match.start() < directives[directive].end():
            continue
        value, index = match.group(), len(tokens)
        tokens.append({"value": value, "start": match.start(), "end": match.end()})
        if value in {"(", "[", "{"}:
            stack.append(index)
        elif value in {")", "]", "}"}:
            if not stack or tokens[stack[-1]]["value"] != {")": "(", "]": "[", "}": "{"}[value]:
                _fail("snapshot-anchor-unavailable", "unbalanced source")
            opening = stack.pop()
            pairs[opening], pairs[index] = index, opening
    if stack:
        _fail("snapshot-anchor-unavailable", "unbalanced source")
    return tokens, pairs


def _lex_cached(source):
    # Private, read-only borrowed token tables. Exact source text is the key,
    # not a hash alone; records receive copies of token spellings. Cache neither
    # resolution decisions nor source files on disk, and bound both count/size.
    with _LEX_CACHE_LOCK:
        if source in _LEX_CACHE:
            _LEX_CACHE.move_to_end(source)
            return _LEX_CACHE[source]
        parsed = _lex(source)
        if len(source.encode("utf-8")) <= MAX_CACHED_SOURCE_BYTES and len(parsed[0]) <= MAX_CACHED_TOKENS:
            _LEX_CACHE[source] = parsed
            while len(_LEX_CACHE) > 2:
                _LEX_CACHE.popitem(last=False)
        return parsed


def _functions(tokens, pairs):
    result = []
    for opening, token in enumerate(tokens):
        if token["value"] != "{":
            continue
        cursor = opening - 1
        # A function definition has (...) optionally followed by an HLSL
        # return semantic. Control-flow blocks and aggregate initializers do not.
        while cursor >= 0 and tokens[cursor]["value"] not in {")", ";", "{", "}"}:
            cursor -= 1
        if cursor < 0 or tokens[cursor]["value"] != ")":
            continue
        param_open = pairs[cursor]
        if param_open < 2:
            continue
        name = tokens[param_open - 1]["value"]
        if not re.fullmatch(r"[A-Za-z_]\w*", name) or name in {"if", "for", "while", "switch", "catch", "register", "packoffset", "layout"}:
            continue
        tail = [t["value"] for t in tokens[cursor + 1:opening]]
        if tail and not (len(tail) == 2 and tail[0] == ":"):
            continue
        begin = param_open - 2
        while begin > 0 and tokens[begin - 1]["value"] not in {";", "{", "}"}:
            begin -= 1
        # Nested function definitions are not legal shader syntax. A call
        # followed by a block inside a function must not become a new owner.
        if any(f["open"] < opening < f["close"] for f in result):
            continue
        result.append({"name": name, "signature": [t["value"] for t in tokens[begin:opening]],
                       "open": opening, "close": pairs[opening]})
    return result


def _values(tokens):
    return [token["value"] for token in tokens]


def _occurrences(values, needle):
    if not needle:
        return []
    return [i for i in range(len(values) - len(needle) + 1)
            if values[i:i + len(needle)] == needle]


def _binding_context(values, start, end, include_positions=False):
    """Reject relocation across lexical blocks or newly shadowing declarations.

    This is a conservative identity check, not Shader type inference. Initializer
    math is intentionally absent from a declaration's identity, so updating that
    math still observes the latest value of the same declaration.
    """
    scopes, stack, depths, depth = [], [], [], 0
    for index, value in enumerate(values):
        scopes.append(tuple(stack))
        depths.append(depth)
        if value in {"(", "["}:
            depth += 1
        elif value in {")", "]"}:
            depth -= 1
        if value == "{":
            begin = index
            while begin > 0 and values[begin - 1] not in {";", "{", "}"}:
                begin -= 1
            stack.append(tuple(values[begin:index]))
        elif value == "}" and stack:
            stack.pop()
    names = {values[i] for i in range(start, end)
             if re.fullmatch(r"[A-Za-z_]\w*", values[i]) and
             (i == 0 or values[i - 1] != ".") and
             (i + 1 >= len(values) or values[i + 1] != "(") and values[i] not in {"true", "false"}}
    observed_scope = scopes[start]
    bindings = {}
    keywords = {"return", "case", "else", "break", "continue", "discard", "if", "while", "for", "switch"}
    for index in range(start + 1):
        if values[index] not in names or index == 0 or index + 1 >= len(values):
            continue
        if values[index + 1] not in {"=", ";", ",", "[", ")", ":"}:
            continue
        previous = values[index - 1]
        type_start = index - 1
        type_end = index
        if previous == ",":
            # Later declarators inherit their statement's explicit type. A
            # comma inside a call/array index has a different nesting depth.
            type_start = index - 1
            while type_start > 0 and values[type_start - 1] not in {";", "{", "}"}:
                type_start -= 1
            if values[type_start:type_start + 2] == ["for", "("]:
                type_start += 2
            while type_start < index and values[type_start] in {"const", "static", "precise", "highp", "mediump", "lowp"}:
                type_start += 1
            type_end = type_start + 1
            if type_end < index and values[type_end] == "<":
                angle = 1
                type_end += 1
                while type_end < index and angle:
                    angle += values[type_end].count("<") - values[type_end].count(">")
                    type_end += 1
            if (type_end >= index or not re.fullmatch(r"[A-Za-z_]\w*", values[type_end]) or
                    depths[type_end] != depths[index]):
                continue
        if previous == ">":
            depth = 1
            type_start -= 1
            while type_start >= 0 and depth:
                depth += (values[type_start] == ">") - (values[type_start] == "<")
                type_start -= 1
            if depth:
                continue
        if type_start < 0 or not re.fullmatch(r"[A-Za-z_]\w*", values[type_start]) or values[type_start] in keywords:
            continue
        scope = scopes[index]
        if len(scope) > len(observed_scope) or observed_scope[:len(scope)] != scope:
            continue
        if type_start and values[type_start - 1] in {"unsigned", "signed"}:
            type_start -= 1
        candidate = (tuple(values[type_start:type_end]), scope, index)
        old = bindings.get(values[index])
        if old is None or len(scope) >= len(old[1]):
            bindings[values[index]] = candidate
    return observed_scope, {name: (bindings[name] if include_positions else bindings[name][:2])
                           if name in bindings else None for name in names}


def _declaration_identity(values, position):
    """Return type/scope only when this token declares the value itself."""
    name = values[position]
    binding = _binding_context(values, position, position + 1, include_positions=True)[1].get(name)
    if binding is not None and binding[2] == position:
        return binding[:2]
    return None


def _matching_declarations(values, name, identity):
    return [index for index, token in enumerate(values)
            if token == name and _declaration_identity(values, index) == identity]


def _assignment_form(values):
    return ["<assignment>" if token in _ASSIGNMENTS else token for token in values]


def _selection(source, tokens, start, end):
    first, last = tokens[start]["start"], tokens[end - 1]["end"]
    return {"byteStart": len(source[:first].encode("utf-8")), "byteEnd": len(source[:last].encode("utf-8")),
            "line": source.count("\n", 0, first) + 1, "expression": source[first:last]}


def _selection_syntax(tokens, pairs, start, end):
    values = _values(tokens[start:end])
    if any(value in {";", "{", "}", "return", "break", "continue", "discard", "if", "else", "for",
                     "while", "switch", "struct", "cbuffer"} for value in values):
        _fail("snapshot-invalid-selection", "expression crosses statement syntax")
    for index in range(start, end):
        if tokens[index]["value"] in {"(", ")", "[", "]"} and not start <= pairs[index] < end:
            _fail("snapshot-invalid-selection", "incomplete expression delimiter")
    if values[-1] in _ASSIGNMENTS | {".", ",", "+", "-", "*", "/", "%", "&", "|", "^", "~", "!",
                                         "?", ":", "<", ">", "<=", ">=", "==", "!=", "&&", "||", "<<", ">>"}:
        _fail("snapshot-invalid-selection", "incomplete expression")
    for first, second in zip(values, values[1:]):
        if re.fullmatch(r"[A-Za-z_0-9]+", first) and re.fullmatch(r"[A-Za-z_0-9]+", second):
            _fail("snapshot-invalid-selection", "adjacent expression operands")


def make_snapshot(source, selection, identity, name="", member="", options=None):
    """Create an unsaved recipe from an already inspected original-file span."""
    _source(source)
    identity = normalize_identity(identity)
    if not isinstance(selection, dict):
        _fail("snapshot-invalid-selection")
    raw = source.encode("utf-8")
    start, end = selection.get("byteStart"), selection.get("byteEnd")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
        _fail("snapshot-invalid-selection")
    try:
        first, last = len(raw[:start].decode("utf-8")), len(raw[:end].decode("utf-8"))
    except UnicodeDecodeError:
        _fail("snapshot-invalid-selection", "UTF-8 boundary")
    tokens, pairs = _lex_cached(source)
    selected = [i for i, t in enumerate(tokens) if first <= t["start"] and t["end"] <= last]
    if not selected or tokens[selected[0]]["start"] != first or tokens[selected[-1]]["end"] != last:
        _fail("snapshot-invalid-selection", "token boundary")
    start_token, end_token = selected[0], selected[-1] + 1
    _selection_syntax(tokens, pairs, start_token, end_token)
    owners = [f for f in _functions(tokens, pairs) if f["open"] < start_token < end_token <= f["close"]]
    if len(owners) != 1:
        _fail("snapshot-anchor-unavailable", "function owner")
    owner = owners[0]
    body = _values(tokens[owner["open"] + 1:owner["close"]])
    if len(body) > MAX_FUNCTION_TOKENS:
        _fail("snapshot-size-limit", "function")
    a, b = start_token - owner["open"] - 1, end_token - owner["open"] - 1
    statement_start, statement_end = a, b
    while statement_start > 0 and body[statement_start - 1] not in {";", "{", "}"}:
        statement_start -= 1
    while statement_end < len(body) and body[statement_end] not in {";", "{", "}"}:
        statement_end += 1
    if statement_end < len(body) and body[statement_end] == ";":
        statement_end += 1
    expression = _text(source[first:last], MAX_EXPRESSION_BYTES, "expression")
    return {"schema": SNAPSHOT_SCHEMA, "id": uuid.uuid4().hex,
            "name": _text(name, 1024, "name"), "expression": expression,
            "member": _text(member, 4096, "member"), "identity": identity,
            "sourcePath": identity["path"], "options": _options(options),
            "createdAt": time.time(), "updatedAt": time.time(),
            "anchor": {"sourceSha256": _hash(source), "selection": _selection(source, tokens, start_token, end_token),
                       "function": owner["name"], "signature": owner["signature"], "bodyTokens": body,
                       "startToken": a, "endToken": b, "statementStart": statement_start,
                       "statementEnd": statement_end}}


def _validate(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        _fail("snapshot-invalid-record")
    if set(snapshot) != {"schema", "id", "name", "expression", "member", "identity", "sourcePath",
                         "options", "createdAt", "updatedAt", "anchor"}:
        _fail("snapshot-invalid-record", "fields")
    if not isinstance(snapshot.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot["id"]):
        _fail("snapshot-invalid-record", "id")
    identity = normalize_identity(snapshot.get("identity"))
    if (not isinstance(snapshot.get("sourcePath"), str)
            or _path(snapshot["sourcePath"]) != identity["path"]):
        _fail("snapshot-invalid-record", "source path")
    for key in ("createdAt", "updatedAt"):
        if type(snapshot.get(key)) not in (int, float) or not math.isfinite(snapshot[key]) or snapshot[key] < 0:
            _fail("snapshot-invalid-record", key)
    _text(snapshot.get("name"), 1024, "name")
    _text(snapshot.get("member"), 4096, "member")
    _text(snapshot.get("expression"), MAX_EXPRESSION_BYTES, "expression")
    _options(snapshot.get("options"))
    anchor = snapshot.get("anchor")
    if not isinstance(anchor, dict):
        _fail("snapshot-invalid-record", "anchor")
    if set(anchor) - {"invalidated"} != {"sourceSha256", "selection", "function", "signature", "bodyTokens",
                                       "startToken", "endToken", "statementStart", "statementEnd"}:
        _fail("snapshot-invalid-record", "anchor fields")
    if "invalidated" in anchor and (not isinstance(anchor["invalidated"], str) or anchor["invalidated"] not in {
            "snapshot-expression-deleted", "snapshot-anchor-invalidated"}):
        _fail("snapshot-invalid-record", "invalidation")
    body, signature = anchor.get("bodyTokens"), anchor.get("signature")
    if not isinstance(body, list) or not body or len(body) > MAX_FUNCTION_TOKENS or not all(isinstance(t, str) and t for t in body):
        _fail("snapshot-invalid-record", "tokens")
    if not isinstance(signature, list) or not signature or not all(isinstance(t, str) and t for t in signature):
        _fail("snapshot-invalid-record", "signature")
    indices = [anchor.get(k) for k in ("statementStart", "startToken", "endToken", "statementEnd")]
    if not all(type(i) is int for i in indices) or not 0 <= indices[0] <= indices[1] < indices[2] <= indices[3] <= len(body):
        _fail("snapshot-invalid-record", "token span")
    if not isinstance(anchor.get("sourceSha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", anchor["sourceSha256"]):
        _fail("snapshot-invalid-record", "source hash")
    selection = anchor.get("selection")
    if (not isinstance(selection, dict) or set(selection) != {"byteStart", "byteEnd", "line", "expression"} or
            any(type(selection.get(key)) is not int for key in ("byteStart", "byteEnd", "line")) or
            not 0 <= selection["byteStart"] < selection["byteEnd"] <= MAX_SOURCE_BYTES or selection["line"] < 1 or
            selection.get("expression") != snapshot["expression"]):
        _fail("snapshot-invalid-record", "selection")
    if not isinstance(anchor.get("function"), str) or not re.fullmatch(r"[A-Za-z_]\w*", anchor["function"]):
        _fail("snapshot-invalid-record", "function")
    return snapshot


def resolve_snapshot(snapshot, source, identity):
    """Keep exact legacy matches; recover changed expressions through source trees."""
    result = _resolve_lexical_snapshot(snapshot, source, identity)
    if result.get('status') == 'resolved' or result.get('reason') in {
            'snapshot-identity-mismatch', 'snapshot-expression-deleted',
            'snapshot-source-unparseable'}:
        return result
    anchor = snapshot['anchor']
    if anchor.get('invalidated'):
        return result
    tokens, pairs = _lex_cached(source)
    functions = [dict(row, body=_values(tokens[row['open'] + 1:row['close']]))
                 for row in _functions(tokens, pairs)]
    function = _structural.function_match(anchor['signature'], anchor['bodyTokens'], functions)
    if function is None:
        return result
    mapped = _structural.relocate(anchor['bodyTokens'], function['body'],
        anchor['startToken'], anchor['endToken'], anchor['signature'], function['signature'])
    if mapped is None:
        return result
    if result.get('reason') == 'snapshot-anchor-ambiguous' and mapped.get('evidence') != 'expression-role':
        return result
    body_tokens = tokens[function['open'] + 1:function['close']]
    selection = _selection(source, body_tokens, mapped['start'], mapped['end'])
    return {'status': 'resolved', 'reason': 'snapshot-resolved', 'selection': selection,
            'member': snapshot['member'], 'options': _options(snapshot['options'])}


def _resolve_lexical_snapshot(snapshot, source, identity):
    """Resolve to current bytes; unresolved results never contain a guessed span."""
    _validate(snapshot)
    _source(source)
    def unresolved(reason):
        return {"status": "unresolved", "reason": reason}
    if normalize_identity(identity) != normalize_identity(snapshot["identity"]):
        return unresolved("snapshot-identity-mismatch")
    anchor = snapshot["anchor"]
    if anchor.get("invalidated"):
        return unresolved(anchor["invalidated"])
    try:
        tokens, pairs = _lex_cached(source)
    except SnapshotError:
        return unresolved("snapshot-source-unparseable")
    functions = [f for f in _functions(tokens, pairs) if f["signature"] == anchor["signature"]]
    if len(functions) != 1:
        return unresolved("snapshot-function-missing" if not functions else "snapshot-anchor-ambiguous")
    function = functions[0]
    body_tokens = tokens[function["open"] + 1:function["close"]]
    body, old = _values(body_tokens), anchor["bodyTokens"]
    a, b = anchor["startToken"], anchor["endToken"]
    declaration = _declaration_identity(old, a) if b == a + 1 else None
    if body == old:
        location = a
    elif declaration is not None:
        # A declaration is the durable identity; its initializer is expected
        # to change while debugging. Same-name reads are never candidates, and
        # type/scope changes or copied declarations must not pick an ordinal.
        old_declarations = _matching_declarations(old, old[a], declaration)
        declarations = _matching_declarations(body, old[a], declaration)
        if len(old_declarations) != 1 or len(declarations) > 1:
            return unresolved("snapshot-anchor-ambiguous")
        if not declarations:
            return unresolved("snapshot-expression-rewritten")
        location = declarations[0]
    else:
        sa, sb = anchor["statementStart"], anchor["statementEnd"]
        statement, expression = old[sa:sb], old[a:b]
        # Changing an assignment operator preserves its value location. Match
        # the entire LHS/RHS token structure, not merely the repeated member
        # text: an inserted `result.albedo=0.1` cannot steal `...+=baseSample...`.
        assignment_matches, old_assignment_matches = [], []
        if sum(token in _ASSIGNMENTS for token in statement) == 1:
            form = _assignment_form(statement)
            old_assignment_matches = _occurrences(_assignment_form(old), form)
            assignment_matches = _occurrences(_assignment_form(body), form)
            if len(assignment_matches) > len(old_assignment_matches):
                return unresolved("snapshot-anchor-ambiguous")
        old_statements, statements = _occurrences(old, statement), _occurrences(body, statement)
        if len(statements) > len(old_statements):
            return unresolved("snapshot-anchor-ambiguous")
        if len(statements) < len(old_statements) and statements:
            # One identical statement was deleted; do not silently select a
            # surviving duplicate using its new ordinal or old line number.
            return unresolved("snapshot-anchor-ambiguous")
        if len(old_assignment_matches) == 1 and len(assignment_matches) == 1:
            location = assignment_matches[0] + a - sa
        elif len(statements) == 1:
            location = statements[0] + a - sa
        elif statements:
            # Existing repeated statements require a unique unchanged context.
            matches = set()
            for left, right in ((24, 24), (24, 0), (0, 24)):
                begin, end = max(0, a - left), min(len(old), b + right)
                context = old[begin:end]
                current = _occurrences(body, context)
                if len(_occurrences(old, context)) == 1 and len(current) == 1:
                    position = current[0] + a - begin
                    if position - (a - sa) in statements:
                        matches.add(position)
            if len(matches) != 1:
                return unresolved("snapshot-anchor-ambiguous")
            location = matches.pop()
        else:
            # Edits elsewhere in the same statement may retain the exact
            # expression. Require unique expression plus unchanged contextual
            # tokens; a bare name match alone is never enough.
            old_values, values = _occurrences(old, expression), _occurrences(body, expression)
            if not values:
                return unresolved("snapshot-expression-missing")
            if len(old_values) != 1 or len(values) != 1:
                return unresolved("snapshot-anchor-ambiguous")
            matches = set()
            for left, right in ((12, 0), (0, 12)):
                begin, end = max(0, a - left), min(len(old), b + right)
                if (a - begin) + (end - b) < 3:
                    continue
                current = _occurrences(body, old[begin:end])
                if len(current) == 1:
                    matches.add(current[0] + a - begin)
            if matches != set(values):
                return unresolved("snapshot-expression-rewritten")
            location = values[0]
    if _binding_context(old, a, b) != _binding_context(body, location, location + b - a):
        return unresolved("snapshot-expression-rewritten")
    selection = _selection(source, body_tokens, location, location + b - a)
    return {"status": "resolved", "reason": "snapshot-resolved", "selection": selection,
            "member": snapshot["member"], "options": _options(snapshot["options"])}


def reanchor_snapshot(snapshot, source, selection, identity):
    """Persist a position mapped by a live editor anchor, without text guessing.

    The caller must obtain ``selection`` from its edit-aware range anchor. This
    explicit authority permits known rename/move edits, including an undo which
    revives a deleted range. Unknown edits still use conservative resolve_snapshot.
    """
    _validate(snapshot)
    if normalize_identity(identity) != normalize_identity(snapshot["identity"]):
        _fail("snapshot-identity-mismatch")
    current = make_snapshot(source, selection, identity, name=snapshot["name"],
                            member=snapshot["member"], options=snapshot["options"])
    current["id"], current["createdAt"] = snapshot["id"], snapshot["createdAt"]
    return current


class SnapshotStore(object):
    """One identity's ordered recipes, saved through an atomic replace.

    Mutations reload under both process and OS file locks, so multiple UI
    objects or Bread instances do not overwrite edits. Reads never create files.
    """
    def __init__(self, root_directory, identity):
        self.identity = normalize_identity(identity)
        self._legacy_identity = {key: (_legacy_path(identity[key]) if key in {"workspace", "path"}
                                       else identity[key]) for key in self.identity}
        self._legacy_identities = [self._legacy_identity]
        # Ordinary drive paths did not change in v1. Their old Win32 extended
        # spellings have deterministic lowercase keys and need no history scan.
        for key in ("workspace", "path"):
            if re.match(r"^[a-z]:/", self.identity[key]):
                for variant in list(self._legacy_identities):
                    extended = dict(variant)
                    extended[key] = "//?/" + self.identity[key]
                    if extended not in self._legacy_identities:
                        self._legacy_identities.append(extended)
        self._scan_legacy = any(value.startswith("//") or
                               re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value)
                               for value in (self.identity["workspace"], self.identity["path"]))
        self._legacy_files = {}
        self._locked_legacy_paths = None
        root = os.path.abspath(os.fspath(root_directory))
        key = _identity_key(self.identity)
        self.path = os.path.join(root, "shader-visual-snapshots", key + ".json")
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(self.path), threading.RLock())

    @contextlib.contextmanager
    def _file_lock(self):
        # Old clients lock their old key. Serialize with that lock too when a
        # verified legacy document is upgraded by a normal write operation.
        with self._path_lock(self.path):
            paths = []
            if not os.path.lexists(self.path):
                self._load()
                paths = sorted(self._legacy_files)
            with contextlib.ExitStack() as stack:
                for path in paths:
                    stack.enter_context(self._path_lock(path))
                self._locked_legacy_paths = set(paths)
                try:
                    yield
                finally:
                    self._locked_legacy_paths = None

    @contextlib.contextmanager
    def _path_lock(self, path):
        """OS locks release on close/process exit; the sidecar holds no source."""
        stream, acquired = None, False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            stream = open(path + ".lock", "a+b")
            if os.name == "nt":
                import msvcrt
                def acquire():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while not acquired:
                try:
                    acquire()
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        _fail("snapshot-store-busy")
                    time.sleep(0.025)
            # Windows byte-range locks also cover an empty file. Read/write
            # its sentinel only after ownership: another process's mandatory
            # lock would reject even a read of that byte before acquisition.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            yield
        except OSError as error:
            _fail("snapshot-store-write-failed", str(error))
        finally:
            if stream is not None:
                try:
                    if acquired:
                        release()
                finally:
                    stream.close()

    def _read_payload(self, path):
        try:
            if os.path.islink(path):
                _fail("snapshot-store-invalid", "redirected store")
            with open(path, "rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None, None
        except OSError as error:
            _fail("snapshot-store-read-failed", str(error))
        if len(data) > MAX_FILE_BYTES:
            _fail("snapshot-size-limit", "store")
        try:
            value = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            _fail("snapshot-store-invalid")
        return value, data

    def _matches_identity(self, identity):
        if not isinstance(identity, dict) or set(identity) != set(self.identity):
            return False
        try:
            return (normalize_identity(identity) == self.identity
                    or identity in self._legacy_identities)
        except SnapshotError:
            return False

    def _records(self, value):
        if (not isinstance(value, dict) or value.get("schema") != SCHEMA
                or not self._matches_identity(value.get("identity"))):
            _fail("snapshot-store-invalid")
        records = value.get("snapshots")
        if not isinstance(records, list) or len(records) > MAX_SNAPSHOTS:
            _fail("snapshot-store-invalid")
        identifiers = set()
        normalized = []
        for record in records:
            _validate(record)
            if not self._matches_identity(record["identity"]) or record["id"] in identifiers:
                _fail("snapshot-store-invalid")
            identifiers.add(record["id"])
            current = copy.deepcopy(record)
            current["identity"] = copy.deepcopy(self.identity)
            current["sourcePath"] = self.identity["path"]
            normalized.append(current)
        return normalized

    def _load(self):
        value, data = self._read_payload(self.path)
        if data is not None:
            self._legacy_files = {}
            return self._records(value)
        directory = os.path.dirname(self.path)
        old_names = [_identity_key(value) + ".json" for value in self._legacy_identities]
        try:
            names = (sorted(name for name in os.listdir(directory)
                            if re.fullmatch(r"[0-9a-f]{64}\.json", name))
                     if self._scan_legacy else
                     [name for name in old_names if os.path.lexists(os.path.join(directory, name))])
        except FileNotFoundError:
            self._legacy_files = {}
            return []
        except OSError as error:
            _fail("snapshot-store-read-failed", str(error))
        if len(names) > MAX_LEGACY_STORE_FILES:
            _fail("snapshot-size-limit", "legacy store lookup")
        # The caller's exact old spelling is checked first; the bounded search
        # also finds uppercase forward-slash UNC and extended-path old keys
        # whose spelling cannot be reconstructed from the current UI path.
        names = [name for name in old_names if name in names] + [name for name in names if name not in old_names]
        found, files, total = None, {}, 0
        for name in names:
            path = os.path.join(directory, name)
            try:
                size = os.path.getsize(path)
                total += size
                if total > MAX_LEGACY_STORE_BYTES:
                    _fail("snapshot-size-limit", "legacy store lookup")
                candidate, data = self._read_payload(path)
            except (OSError, SnapshotError):
                if name in old_names:
                    raise
                if total > MAX_LEGACY_STORE_BYTES:
                    raise
                continue  # Unrelated corrupt history cannot authorize migration.
            if (not isinstance(candidate, dict) or candidate.get("schema") != SCHEMA
                    or not self._matches_identity(candidate.get("identity"))):
                if name in old_names and data is not None:
                    _fail("snapshot-store-invalid", "legacy store")
                continue
            # Existing v1 keys hash their literal stored identity. A differently
            # named file is not an automatic migration source.
            if _identity_key(candidate["identity"]) + ".json" != name:
                _fail("snapshot-store-invalid", "legacy key mismatch")
            records = self._records(candidate)
            if found is not None and records != found:
                _fail("snapshot-store-invalid", "conflicting legacy identities")
            found = records
            files[path] = hashlib.sha256(data).hexdigest()
        if self._locked_legacy_paths is not None and set(files) - self._locked_legacy_paths:
            _fail("snapshot-store-busy")
        self._legacy_files = files
        return found if found is not None else []

    def _save(self, records):
        if len(records) > MAX_SNAPSHOTS:
            _fail("snapshot-size-limit", "count")
        records = self._records({"schema": SCHEMA, "identity": self.identity, "snapshots": records})
        legacy_files = dict(self._legacy_files)
        data = json.dumps({"schema": SCHEMA, "identity": self.identity, "snapshots": records},
                          ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            _fail("snapshot-size-limit", "store")
        directory, temporary = os.path.dirname(self.path), None
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".snapshot-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            # Publication is already atomic and complete. Keep the exact old
            # bytes as a non-discoverable backup, never delete a user's recipe.
            # A failed backup rename does not undo the successful current save.
            for old_path, digest in legacy_files.items():
                try:
                    unused, old_data = self._read_payload(old_path)
                    if old_data is not None and hashlib.sha256(old_data).hexdigest() == digest:
                        os.replace(old_path, old_path + ".legacy-" + digest + ".bak")
                except (OSError, SnapshotError):
                    pass
            self._legacy_files = {}
        except OSError as error:
            _fail("snapshot-store-write-failed", str(error))
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def list(self):
        with self._lock:
            return copy.deepcopy(self._load())

    def add(self, snapshot):
        _validate(snapshot)
        if normalize_identity(snapshot["identity"]) != self.identity:
            _fail("snapshot-identity-mismatch")
        with self._lock, self._file_lock():
            records = self._load()
            if any(record["id"] == snapshot["id"] for record in records):
                _fail("snapshot-duplicate-id")
            records.append(copy.deepcopy(snapshot))
            self._save(records)
        return copy.deepcopy(snapshot)

    def update(self, identifier, **changes):
        if not changes or set(changes) - {"name", "member", "options"}:
            _fail("snapshot-invalid-metadata")
        with self._lock, self._file_lock():
            records = self._load()
            record = next((r for r in records if r["id"] == identifier), None)
            if record is None:
                _fail("snapshot-not-found")
            if "name" in changes:
                record["name"] = _text(changes["name"], 1024, "name")
            if "member" in changes:
                record["member"] = _text(changes["member"], 4096, "member")
            if "options" in changes:
                record["options"] = _options(changes["options"])
            record["updatedAt"] = time.time()
            self._save(records)
            return copy.deepcopy(record)

    def rename(self, identifier, name):
        return self.update(identifier, name=name)

    def update_anchors(self, source, changes):
        """Atomically update live mapped ranges; None persists a deleted anchor.

        The current stored records are loaded while locked, preserving concurrent
        rename/options/reorder changes. An incomplete current source aborts the
        batch without replacing any stable anchor; the UI retains its live ranges.
        """
        _source(source)
        if not isinstance(changes, dict) or not changes or not all(isinstance(key, str) for key in changes):
            _fail("snapshot-invalid-selection")
        with self._lock, self._file_lock():
            records = self._load()
            if set(changes) - {record["id"] for record in records}:
                _fail("snapshot-not-found")
            updated = []
            for record in records:
                if record["id"] in changes:
                    selection = changes[record["id"]]
                    if selection is None:
                        record["anchor"]["invalidated"] = "snapshot-expression-deleted"
                        record["updatedAt"] = time.time()
                    else:
                        record = reanchor_snapshot(record, source, selection, self.identity)
                updated.append(record)
            self._save(updated)
            return copy.deepcopy(updated)

    def reanchor(self, identifier, source, selection):
        records = self.update_anchors(source, {identifier: selection})
        return next(record for record in records if record["id"] == identifier)

    def restore_anchors(self, checkpoints):
        """Restore explicit saved-buffer checkpoints without parsing edited text.

        Only location fields are restored, including a checkpoint's tombstone.
        Concurrent metadata/order changes survive, and a removed snapshot is
        never recreated. This is explicit discard/reset authority, not a way to
        infer that an unresolved position was deleted.
        """
        if not isinstance(checkpoints, (list, tuple)):
            _fail("snapshot-invalid-record", "checkpoints")
        if len(checkpoints) > MAX_SNAPSHOTS:
            _fail("snapshot-size-limit", "checkpoints")
        by_id = {}
        for checkpoint in checkpoints:
            _validate(checkpoint)
            if normalize_identity(checkpoint["identity"]) != self.identity:
                _fail("snapshot-identity-mismatch")
            if checkpoint["id"] in by_id:
                _fail("snapshot-duplicate-id")
            by_id[checkpoint["id"]] = copy.deepcopy(checkpoint)
        if not by_id:
            return self.list()
        with self._lock, self._file_lock():
            records = self._load()
            changed = False
            for record in records:
                checkpoint = by_id.get(record["id"])
                if checkpoint is not None:
                    for field in ("expression", "sourcePath", "anchor"):
                        record[field] = copy.deepcopy(checkpoint[field])
                    record["updatedAt"] = time.time()
                    changed = True
            if changed:
                self._save(records)
            return copy.deepcopy(records)

    def invalidate_anchor(self, identifier, reason="snapshot-expression-deleted"):
        if reason not in {"snapshot-expression-deleted", "snapshot-anchor-invalidated"}:
            _fail("snapshot-invalid-record", "invalidation")
        with self._lock, self._file_lock():
            records = self._load()
            record = next((item for item in records if item["id"] == identifier), None)
            if record is None:
                _fail("snapshot-not-found")
            record["anchor"]["invalidated"] = reason
            record["updatedAt"] = time.time()
            self._save(records)
            return copy.deepcopy(record)

    def reorder(self, identifiers):
        with self._lock, self._file_lock():
            records = self._load()
            if (not isinstance(identifiers, (list, tuple)) or not all(isinstance(i, str) for i in identifiers) or
                    len(identifiers) != len(records) or set(identifiers) != {r["id"] for r in records}):
                _fail("snapshot-invalid-order")
            by_id = {record["id"]: record for record in records}
            ordered = [by_id[identifier] for identifier in identifiers]
            self._save(ordered)
            return copy.deepcopy(ordered)

    def remove(self, identifier):
        with self._lock, self._file_lock():
            records = self._load()
            remaining = [record for record in records if record["id"] != identifier]
            if len(remaining) == len(records):
                _fail("snapshot-not-found")
            self._save(remaining)
            return copy.deepcopy(remaining)

    delete = remove
