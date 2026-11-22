"""Source-preserving Shader value probes for HLSL and GLSL.

This module never compiles, saves, or applies a Shader.  It builds a temporary
translation unit that the capture's real compiler MUST accept before use.  A
bounded token/declaration parser identifies lexical value accesses; unsupported
or ambiguous syntax is rejected instead of guessing a line or a symbol.

The probe observes a value at its expression and returns that same value.  The
entry wrapper replaces only the chosen color result.  Execution after the probe
continues (including derivatives and memory side effects); later discard/clip is
suppressed once the requested hit is recorded.  This is not instruction stepping.
Compatible with the application's embedded Python 3.6.
"""
import hashlib
import math
import re


class VisualDebugError(ValueError):
    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        ValueError.__init__(self, code + (": " + detail if detail else ""))


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|"
    r"(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|"
    r"(?P<word>[A-Za-z_][A-Za-z_0-9]*)|"
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z0-9]*)|"
    r"(?P<op><<=|>>=|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|&&|\|\||==|!=|<=|>=|<<|>>|.)")
_QUALIFIERS = set(("const static inline uniform volatile precise highp mediump lowp in out inout varying attribute signed unsigned snorm unorm "
                   "linear centroid nointerpolation noperspective sample invariant "
                   "flat smooth patch row_major column_major shared groupshared").split())
_ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
_CONTROL = {"if", "for", "while", "switch", "catch"}
_PREFIX = "_bread_visual_"
_HLSL_OBJECT = re.compile(r"^(?:(?:RW|RasterizerOrdered)?Texture(?:1D|2D|3D|Cube)(?:MS)?(?:Array)?|"
                          r"(?:RW|RasterizerOrdered)?(?:Structured|ByteAddress)?Buffer|"
                          r"(?:Append|Consume)StructuredBuffer|ConstantBuffer|InputPatch|OutputPatch)$")


def _opaque(name):
    base = name.split("<", 1)[0]
    return bool(_HLSL_OBJECT.fullmatch(base) or
                base in {"SamplerState", "SamplerComparisonState", "sampler", "sampler_state"} or
                re.fullmatch(r"[iu]?(?:sampler|image|texture)(?:1D|2D|3D|Cube|Buffer|Rect)(?:MS)?(?:Array)?(?:Shadow)?", base) or
                re.fullmatch(r"[iu]?subpassInput(?:MS)?", base))


def _fail(code, detail=""):
    raise VisualDebugError(code, detail)


def _tokens(source, strict=True):
    result = []
    directives = [(m.start(), m.end()) for m in re.finditer(r"(?m)^[ \t]*#[^\n]*(?:\\\n[^\n]*)*", source)]
    directive_index = 0
    for match in _TOKEN.finditer(source):
        kind = match.lastgroup
        value = match.group()
        if kind in {"space", "comment"}:
            continue
        while directive_index < len(directives) and directives[directive_index][1] <= match.start():
            directive_index += 1
        if directive_index < len(directives) and directives[directive_index][0] <= match.start() < directives[directive_index][1]:
            continue
        result.append({"v": value, "s": match.start(), "e": match.end(), "kind": kind})
    compact, i = [], 0
    while i < len(result):
        if (_HLSL_OBJECT.fullmatch(result[i]["v"]) and i + 1 < len(result) and
                result[i + 1]["v"] == "<"):
            depth, end = 1, i + 2
            while end < len(result) and depth and result[end]["v"] not in {";", "{", "}"}:
                depth += result[end]["v"].count("<") - result[end]["v"].count(">")
                end += 1
            if depth == 0:
                spelling = "".join(token["v"] for token in result[i:end])
                compact.append(dict(result[i], v=spelling, e=result[end - 1]["e"]))
                i = end
                continue
        count = 6 if result[i]["v"] == "vector" else 8 if result[i]["v"] == "matrix" else 0
        if count and i + count <= len(result):
            spelling = "".join(token["v"] for token in result[i:i + count])
            if re.fullmatch(r"(?:vector<\w+,[1-4]>|matrix<\w+,[1-4],[1-4]>)", spelling):
                compact.append(dict(result[i], v=spelling, e=result[i + count - 1]["e"]))
                i += count
                continue
        compact.append(result[i])
        i += 1
    result = compact
    pairs, stack = {}, []
    for i, token in enumerate(result):
        value = token["v"]
        if value in {"(", "[", "{"}:
            stack.append(i)
        elif value in {")", "]", "}"}:
            opening = {")": "(", "]": "[", "}": "{"}[value]
            if not stack or result[stack[-1]]["v"] != opening:
                if strict:
                    _fail("unbalanced-source")
                while stack and result[stack[-1]]["v"] != opening:
                    stack.pop()
                if not stack:
                    continue
            left = stack.pop()
            pairs[left], pairs[i] = i, left
    if stack and strict:
        _fail("unbalanced-source")
    return result, pairs


def _parts(tokens, pairs, start, end, delimiter=","):
    result, begin, i = [], start, start
    while i < end:
        value = tokens[i]["v"]
        if value in {"(", "[", "{"}:
            i = pairs[i] + 1
            continue
        if value == delimiter:
            result.append((begin, i))
            begin = i + 1
        i += 1
    if begin < end:
        result.append((begin, end))
    return result


def _statement_end(tokens, pairs, start):
    if start >= len(tokens):
        _fail("incomplete-statement")
    if tokens[start]["v"] == "{":
        return pairs[start] + 1
    if tokens[start]["v"] in {"if", "for", "while", "switch"}:
        if start + 1 >= len(tokens) or tokens[start + 1]["v"] != "(":
            _fail("incomplete-statement")
        end = _statement_end(tokens, pairs, pairs[start + 1] + 1)
        if tokens[start]["v"] == "if" and end < len(tokens) and tokens[end]["v"] == "else":
            return _statement_end(tokens, pairs, end + 1)
        return end
    i = start
    while i < len(tokens):
        if tokens[i]["v"] == ";":
            return i + 1
        if tokens[i]["v"] in {"(", "[", "{"}:
            i = pairs[i] + 1
        else:
            i += 1
    _fail("incomplete-statement")


def _numeric(name, language):
    # (scalar kind, vector width, matrix rows, matrix columns)
    generic = re.fullmatch(r"(vector|matrix)<(\w+),([1-4])(?:,([1-4]))?>", name)
    if generic:
        base = _numeric(generic.group(2), language)
        if base and base[1:] == (1, 0, 0):
            return (base[0], int(generic.group(3)), 0, 0) if generic.group(1) == "vector" else (base[0], 1, int(generic.group(3)), int(generic.group(4) or "1"))
        return None
    if name == "dword":
        return ("uint", 1, 0, 0)
    match = re.fullmatch(r"(bool|int|uint|float|half|double|min16float|min10float|min16int|min12int|min16uint|(?:float|u?int)(?:16|32|64)_t)([1-4])?(?:x([1-4]))?", name)
    if match:
        kind = match.group(1)
        width = int(match.group(2) or "1")
        cols = int(match.group(3) or "0")
        return (kind, 1 if cols else width, width if cols else 0, cols)
    match = re.fullmatch(r"([biud]?vec)([2-4])", name)
    if match:
        return ({"vec": "float", "ivec": "int", "uvec": "uint", "bvec": "bool", "dvec": "double"}[match.group(1)], int(match.group(2)), 0, 0)
    match = re.fullmatch(r"([fiu])(16|32|64)vec([2-4])", name)
    if match:
        return ({"f": "float", "i": "int", "u": "uint"}[match.group(1)] + match.group(2) + "_t", int(match.group(3)), 0, 0)
    match = re.fullmatch(r"(d?mat)([2-4])(?:x([2-4]))?", name)
    if match:
        return ("double" if match.group(1) == "dmat" else "float", 1,
                int(match.group(3) or match.group(2)), int(match.group(2)))
    return None


def _vector(kind, count, language):
    if language == "hlsl":
        return kind + (str(count) if count > 1 else "")
    if count == 1:
        return kind
    extended = re.fullmatch(r"(float|u?int)(16|32|64)_t", kind)
    if extended:
        return {"float": "f", "int": "i", "uint": "u"}[extended.group(1)] + extended.group(2) + "vec" + str(count)
    return {"float": "vec", "int": "ivec", "uint": "uvec", "bool": "bvec", "double": "dvec"}[kind] + str(count)


def _declaration(tokens, pairs, start, end, known_types):
    """Parse one explicit typed declarator, never infer function-return types."""
    i, qualifiers = start, []
    while i < end:
        if tokens[i]["v"] in _QUALIFIERS:
            qualifiers.append(tokens[i]["v"])
            i += 1
        elif tokens[i]["v"] == "layout" and i + 1 < end and tokens[i + 1]["v"] == "(":
            i = pairs[i + 1] + 1
        elif tokens[i]["v"] == "[":
            i = pairs[i] + 1
        else:
            break
    if i + 1 >= end or tokens[i]["v"] not in known_types or not _IDENT.match(tokens[i + 1]["v"]):
        return None
    result = {"type": tokens[i]["v"], "name": tokens[i + 1]["v"],
              "typeToken": i, "nameToken": i + 1, "qualifiers": qualifiers,
              "array": [], "semantic": "", "startToken": start, "endToken": end}
    constant_buffer = re.fullmatch(r"ConstantBuffer<(\w+)>", result["type"])
    if constant_buffer and constant_buffer.group(1) in known_types:
        result["type"] = constant_buffer.group(1)
    if "unsigned" in qualifiers:
        if result["type"] != "int":
            _fail("unsupported-unsigned-type")
        result["type"] = "uint"
    i += 2
    while i < end and tokens[i]["v"] == "[":
        close = pairs[i]
        if close != i + 2 or not tokens[i + 1]["v"].isdigit():
            result["array"].append(None)
        else:
            result["array"].append(int(tokens[i + 1]["v"]))
        i = close + 1
    if i < end and tokens[i]["v"] == ":" and i + 1 < end:
        result["semantic"] = tokens[i + 1]["v"]
    elif i < end and tokens[i]["v"] not in {"=", ";", ","}:
        return None
    return result


def _parse(source, language):
    tokens, pairs = _tokens(source)
    types = {token["v"] for token in tokens if _numeric(token["v"], language) or _opaque(token["v"])}
    types.add("void")
    structures, aliases, functions, prototypes, declarations = {}, {}, [], [], []
    block_instances = []
    for i in range(len(tokens) - 2):
        block = tokens[i]["v"] in {"uniform", "buffer", "in", "out"} and language == "glsl"
        if (tokens[i]["v"] == "struct" or block) and _IDENT.match(tokens[i + 1]["v"]) and tokens[i + 2]["v"] == "{":
            name = tokens[i + 1]["v"]
            if name in structures:
                _fail("ambiguous-struct", name)
            structures[name] = {"open": i + 2, "close": pairs[i + 2], "fields": [],
                                "globalMembers": block and tokens[pairs[i + 2] + 1]["v"] == ";"}
            types.add(name)
            instance = pairs[i + 2] + 1
            if instance < len(tokens) and _IDENT.match(tokens[instance]["v"]):
                dimensions, cursor = [], instance + 1
                while cursor < len(tokens) and tokens[cursor]["v"] == "[":
                    close = pairs[cursor]
                    dimensions.append(int(tokens[cursor + 1]["v"]) if close == cursor + 2 and tokens[cursor + 1]["v"].isdigit() else None)
                    cursor = close + 1
                block_instances.append({"name": tokens[instance]["v"], "type": name, "array": dimensions,
                                        "nameToken": instance, "typeToken": i + 1, "startToken": i,
                                        "endToken": cursor, "qualifiers": [], "semantic": "",
                                        "scope": (-1, len(tokens))})
    for i in range(len(tokens) - 3):
        if (tokens[i]["v"] == "typedef" and tokens[i + 1]["v"] in types and
                _IDENT.match(tokens[i + 2]["v"]) and tokens[i + 3]["v"] == ";"):
            aliases[tokens[i + 2]["v"]] = tokens[i + 1]["v"]
            types.add(tokens[i + 2]["v"])
    # Compound declarators cannot be mistaken for uses of another symbol. A
    # caller can split them structurally before requesting a source probe.
    for name, item in structures.items():
        for start, end in _parts(tokens, pairs, item["open"] + 1, item["close"], ";"):
            field = _declaration(tokens, pairs, start, end, types)
            if field is None:
                _fail("unsupported-struct-declaration", name)
            item["fields"].append(field)
    # Definitions have a known return type, parameter list, optional return
    # semantic and a body. Calls, constructors, control flow and prototypes are
    # deliberately not accepted as definitions.
    for i in range(1, len(tokens) - 1):
        if tokens[i]["v"] != "(" or tokens[i - 1]["kind"] != "word" or i < 2:
            continue
        if tokens[i - 2]["v"] not in types:
            continue
        close = pairs[i]
        body, semantic = close + 1, ""
        if body < len(tokens) and tokens[body]["v"] == ":":
            if body + 1 >= len(tokens):
                continue
            semantic, body = tokens[body + 1]["v"], body + 2
        if body >= len(tokens) or tokens[body]["v"] not in {"{", ";"}:
            continue
        params = []
        for start, end in _parts(tokens, pairs, i + 1, close):
            if end == start + 1 and tokens[start]["v"] == "void":
                continue
            param = _declaration(tokens, pairs, start, end, types)
            if param is None and tokens[body]["v"] == ";":
                # Prototypes (including GL_EXT_spirv_intrinsics declarations)
                # may omit argument names. They still describe overload types.
                type_token = start
                while type_token < end and tokens[type_token]["v"] in _QUALIFIERS:
                    type_token += 1
                if type_token + 1 == end and tokens[type_token]["v"] in types:
                    param = {"type": tokens[type_token]["v"], "name": "",
                             "typeToken": type_token, "nameToken": -1,
                             "qualifiers": [t["v"] for t in tokens[start:type_token]],
                             "array": [], "semantic": "", "startToken": start, "endToken": end}
            if param is None:
                _fail("unsupported-function-parameter", tokens[i - 1]["v"])
            params.append(param)
        definition_start = i - 2
        while definition_start and tokens[definition_start - 1]["v"] in _QUALIFIERS:
            definition_start -= 1
        while definition_start and tokens[definition_start - 1]["v"] == "]":
            definition_start = pairs[definition_start - 1]
        function = {"name": tokens[i - 1]["v"], "nameToken": i - 1,
                          "returnType": tokens[i - 2]["v"], "typeToken": i - 2,
                          "definitionStart": definition_start,
                          "paramOpen": i, "paramClose": close, "bodyOpen": body,
                          "bodyClose": pairs[body] if tokens[body]["v"] == "{" else body,
                          "semantic": semantic, "params": params}
        (functions if tokens[body]["v"] == "{" else prototypes).append(function)
    scopes, global_blocks = [], set()
    for i, token in enumerate(tokens):
        if token["v"] == "{":
            # HLSL constant-buffer fields are injected into global scope.
            j = i - 1
            while j >= 0 and tokens[j]["v"] not in {";", "}", "{"}:
                if tokens[j]["v"] in {"cbuffer", "tbuffer"}:
                    global_blocks.add(i)
                    break
                j -= 1
            if any(item["open"] == i and item.get("globalMembers") for item in structures.values()):
                global_blocks.add(i)
            scopes.append((i, pairs[i]))
        if token["v"] == "for" and i + 1 < len(tokens) and tokens[i + 1]["v"] == "(":
            scopes.append((i + 1, _statement_end(tokens, pairs, pairs[i + 1] + 1)))
    for i in range(len(tokens) - 1):
        if tokens[i]["v"] not in types or not _IDENT.match(tokens[i + 1]["v"]):
            continue
        if i + 2 < len(tokens) and tokens[i + 2]["v"] == "(":
            continue
        # A type/name pair is a declaration only at a grammar boundary.
        previous = tokens[i - 1]["v"] if i else ";"
        if previous not in {";", "{", "}", "(", ","} and previous not in _QUALIFIERS:
            continue
        end = i + 2
        while end < len(tokens) and tokens[end]["v"] not in {";", ",", ")", "}"}:
            if tokens[end]["v"] in {"[", "("}:
                end = pairs[end] + 1
            else:
                end += 1
        declaration_start = i
        while declaration_start and tokens[declaration_start - 1]["v"] in _QUALIFIERS:
            declaration_start -= 1
        decl = _declaration(tokens, pairs, declaration_start, end, types)
        if decl is None:
            continue
        enclosing = [scope for scope in scopes if scope[0] < i < scope[1] and scope[0] not in global_blocks]
        decl["scope"] = max(enclosing, default=(-1, len(tokens)), key=lambda s: s[0])
        declarations.append(decl)
        # Subsequent declarators inherit the explicit type, but retain their
        # own dimensions and declaration positions for lexical shadowing.
        next_token = end
        while next_token < len(tokens) and tokens[next_token]["v"] == ",":
            name_token = next_token + 1
            if name_token >= len(tokens) or not _IDENT.match(tokens[name_token]["v"]):
                break
            cursor, dimensions = name_token + 1, []
            while cursor < len(tokens) and tokens[cursor]["v"] == "[":
                close = pairs[cursor]
                dimensions.append(int(tokens[cursor + 1]["v"]) if close == cursor + 2 and tokens[cursor + 1]["v"].isdigit() else None)
                cursor = close + 1
            if cursor < len(tokens) and tokens[cursor]["v"] not in {"=", ";", ","}:
                break
            while cursor < len(tokens) and tokens[cursor]["v"] not in {";", ",", ")"}:
                cursor = pairs[cursor] + 1 if tokens[cursor]["v"] in {"(", "[", "{"} else cursor + 1
            declarations.append(dict(decl, name=tokens[name_token]["v"], nameToken=name_token,
                                     array=dimensions, endToken=cursor, compound=True))
            next_token = cursor
    for function in functions:
        for param in function["params"]:
            value = dict(param)
            value["scope"] = (function["bodyOpen"], function["bodyClose"])
            value["parameter"] = True
            declarations = [d for d in declarations if d["nameToken"] != param["nameToken"]]
            declarations.append(value)
    declarations.extend(block_instances)
    for item in structures.values():
        if item.get("globalMembers"):
            for field in item["fields"]:
                if not any(d["nameToken"] == field["nameToken"] for d in declarations):
                    declarations.append(dict(field, scope=(-1, len(tokens))))
    if language == "hlsl":
        for i in range(len(tokens) - 4):
            if (tokens[i]["v"] == "ConstantBuffer" and tokens[i + 1]["v"] == "<" and
                    tokens[i + 2]["v"] in types and tokens[i + 3]["v"] == ">" and _IDENT.match(tokens[i + 4]["v"])):
                declarations.append({"name": tokens[i + 4]["v"], "type": tokens[i + 2]["v"],
                                     "array": [], "nameToken": i + 4, "typeToken": i + 2,
                                     "scope": (-1, len(tokens)), "qualifiers": [], "semantic": ""})
    return {"tokens": tokens, "pairs": pairs, "types": sorted(types), "structures": structures,
            "aliases": aliases, "functions": functions, "prototypes": prototypes, "declarations": declarations}


def _canonical(model, name):
    seen = set()
    while name in model["aliases"]:
        if name in seen:
            _fail("cyclic-type-alias")
        seen.add(name)
        name = model["aliases"][name]
    return name


def _resolve(model, name, at):
    candidates = [d for d in model["declarations"] if d["name"] == name and
                  (d["nameToken"] <= at or d.get("parameter")) and
                  d["scope"][0] < at < d["scope"][1]]
    if not candidates:
        _fail("unresolved-value", name)
    candidates.sort(key=lambda d: (d["scope"][0], d["nameToken"]), reverse=True)
    return candidates[0]


def _glsl_builtin(name, token_count):
    types = {"gl_FragCoord": "vec4", "gl_FragColor": "vec4", "gl_FragData": "vec4",
             "gl_FrontFacing": "bool", "gl_PointCoord": "vec2", "gl_PrimitiveID": "int",
             "gl_SampleID": "int", "gl_SamplePosition": "vec2", "gl_HelperInvocation": "bool",
             "gl_FragDepth": "float", "gl_FragDepthEXT": "float"}
    if name not in types:
        return None
    return {"name": name, "type": types[name], "array": [None] if name == "gl_FragData" else [],
            "nameToken": -1, "scope": (-1, token_count), "qualifiers": [], "semantic": ""}


def _value_type(model, base, start, end, language):
    tokens, pairs = model["tokens"], model["pairs"]
    name, arrays, i = base["type"], list(base["array"]), start + 1
    while i < end:
        value = tokens[i]["v"]
        canonical = _canonical(model, name)
        numeric = _numeric(canonical, language)
        if value == "[":
            if arrays:
                arrays.pop(0)
            elif numeric and numeric[2]:
                name = _vector(numeric[0], numeric[3] if language == "hlsl" else numeric[2], language)
            elif numeric and numeric[1] > 1:
                name = numeric[0]
            else:
                _fail("unsupported-index-type", name)
            i = pairs[i] + 1
        elif value == "." and i + 1 < end:
            member = tokens[i + 1]["v"]
            if arrays:
                _fail("array-element-required")
            if canonical in model["structures"]:
                fields = [f for f in model["structures"][canonical]["fields"] if f["name"] == member]
                if len(fields) != 1:
                    _fail("unresolved-member", member)
                name, arrays = fields[0]["type"], list(fields[0]["array"])
            elif numeric and not numeric[2] and re.fullmatch(r"[xyzw]{1,4}|[rgba]{1,4}|[stpq]{1,4}", member):
                alphabet = "xyzw" if member[0] in "xyzw" else "rgba" if member[0] in "rgba" else "stpq"
                if any(alphabet.index(ch) >= numeric[1] for ch in member):
                    _fail("invalid-swizzle")
                name = _vector(numeric[0], len(member), language)
            else:
                _fail("unsupported-member-type", name)
            i += 2
        else:
            _fail("unsupported-value-expression")
    return name, arrays


_PRECEDENCE = {",": 1, "=": 2, "+=": 2, "-=": 2, "*=": 2, "/=": 2, "%=": 2,
               "&=": 2, "|=": 2, "^=": 2, "<<=": 2, ">>=": 2, "?": 3,
               "||": 4, "&&": 5, "|": 6, "^": 7, "&": 8, "==": 9, "!=": 9,
               "<": 10, ">": 10, "<=": 10, ">=": 10, "<<": 11, ">>": 11,
               "+": 12, "-": 12, "*": 13, "/": 13, "%": 13}


class _ExpressionParser(object):
    """Parse only the selected expression; never move its evaluation point."""
    def __init__(self, model, start, end):
        self.model, self.tokens, self.pairs = model, model["tokens"], model["pairs"]
        self.start, self.end, self.at = start, end, start

    def parse(self):
        node = self.expression(1)
        if self.at != self.end:
            _fail("unsupported-value-expression", self.tokens[self.at]["v"])
        return node

    def expression(self, minimum, depth=0):
        if depth > 128 or self.at >= self.end:
            _fail("unsupported-value-expression")
        start, token = self.at, self.tokens[self.at]
        value = token["v"]
        self.at += 1
        if value in {"+", "-", "!", "~", "++", "--"}:
            node = {"kind": "unary", "op": value, "value": self.expression(14, depth + 1), "start": start}
        elif value == "(":
            close = self.pairs[start]
            if (close == start + 2 and self.tokens[start + 1]["v"] in self.model["types"] and
                    close + 1 < self.end):
                self.at = close + 1
                node = {"kind": "cast", "type": self.tokens[start + 1]["v"],
                        "value": self.expression(14, depth + 1), "start": start}
            else:
                node = self.expression(1, depth + 1)
                if self.at >= self.end or self.tokens[self.at]["v"] != ")":
                    _fail("unsupported-value-expression")
                self.at += 1
                node = {"kind": "group", "value": node, "start": start}
        elif token["kind"] in {"word", "number"}:
            node = {"kind": "literal" if token["kind"] == "number" or value in {"true", "false"} else "name",
                    "name": value, "start": start}
        else:
            _fail("unsupported-value-expression", value)
        node["end"] = self.at
        while self.at < self.end:
            value = self.tokens[self.at]["v"]
            if value == "(":
                opening, self.at = self.at, self.at + 1
                if self.pairs[opening] >= self.end:
                    _fail("unsupported-value-expression")
                arguments = []
                while self.at < self.pairs[opening]:
                    arguments.append(self.expression(2, depth + 1))
                    if self.at < self.pairs[opening]:
                        if self.tokens[self.at]["v"] != ",":
                            _fail("unsupported-value-expression")
                        self.at += 1
                if self.at != self.pairs[opening]:
                    _fail("unsupported-value-expression")
                self.at += 1
                node = {"kind": "call", "callee": node, "args": arguments, "start": start}
            elif value == "[":
                opening, self.at = self.at, self.at + 1
                if self.pairs[opening] >= self.end:
                    _fail("unsupported-value-expression")
                index = self.expression(1, depth + 1)
                if self.at != self.pairs[opening]:
                    _fail("unsupported-value-expression")
                self.at += 1
                node = {"kind": "index", "value": node, "index": index, "start": start}
            elif value == "." and self.at + 1 < self.end:
                node = {"kind": "member", "value": node, "name": self.tokens[self.at + 1]["v"], "start": start}
                self.at += 2
            elif value in {"++", "--"}:
                node = {"kind": "postfix", "value": node, "op": value, "start": start}
                self.at += 1
            else:
                break
            node["end"] = self.at
        while self.at < self.end:
            op = self.tokens[self.at]["v"]
            priority = _PRECEDENCE.get(op, 0)
            if priority < minimum:
                break
            self.at += 1
            if op == "?":
                yes = self.expression(1, depth + 1)
                if self.at >= self.end or self.tokens[self.at]["v"] != ":":
                    _fail("unsupported-value-expression")
                self.at += 1
                no = self.expression(priority, depth + 1)
                node = {"kind": "conditional", "condition": node, "yes": yes, "no": no, "start": start}
            else:
                right = self.expression(priority if op in _ASSIGN else priority + 1, depth + 1)
                node = {"kind": "binary", "op": op, "left": node, "right": right, "start": start}
            node["end"] = self.at
        node["end"] = self.at
        return node


def _common_type(model, first, second, language):
    a, aa = first
    b, ba = second
    a, b = _canonical(model, a), _canonical(model, b)
    if (a, aa) == (b, ba):
        return a, aa
    na, nb = _numeric(a, language), _numeric(b, language)
    if aa or ba or not na or not nb:
        _fail("unsupported-value-expression", "incompatible expression types")
    if na[2] or nb[2]:
        if not nb[2] and nb[1] == 1 and na[0] == nb[0]:
            return a, []
        if not na[2] and na[1] == 1 and na[0] == nb[0]:
            return b, []
        _fail("unsupported-value-expression", "matrix expression type")
    if na[1] != nb[1] and 1 not in {na[1], nb[1]}:
        _fail("unsupported-value-expression", "vector expression width")
    order = ["bool", "min12int", "min16int", "min16uint", "int16_t", "uint16_t", "int", "int32_t",
             "uint", "uint32_t", "int64_t", "uint64_t", "min10float", "min16float", "half", "float16_t", "float", "float32_t", "double", "float64_t"]
    integer = {"bool", "min12int", "min16int", "min16uint", "int16_t", "uint16_t", "int", "int32_t", "uint", "uint32_t", "int64_t", "uint64_t"}
    if language == "hlsl" and ((na[0] == "half" and nb[0] in integer) or
                               (nb[0] == "half" and na[0] in integer)):
        return _vector("float", max(na[1], nb[1]), language), []
    if language == "hlsl" and na[0] in integer and nb[0] in integer and any(
            name.startswith("u") or name == "min16uint" for name in (na[0], nb[0])):
        kinds = {na[0], nb[0]}
        kind = ("uint64_t" if kinds & {"int64_t", "uint64_t"} else
                "uint" if kinds & {"int", "uint", "int32_t", "uint32_t"} else
                "uint16_t" if kinds & {"int16_t", "uint16_t"} else "min16uint")
        return _vector(kind, max(na[1], nb[1]), language), []
    kind = max((na[0], nb[0]), key=lambda name: order.index(name))
    return _vector(kind, max(na[1], nb[1]), language), []


def _shape_type(kind, numeric, language):
    if numeric[2]:
        if language == "hlsl":
            return "%s%dx%d" % (kind, numeric[2], numeric[3])
        if kind not in {"float", "double"}:
            _fail("unsupported-value-expression", "matrix scalar type")
        return "%smat%dx%d" % ("d" if kind == "double" else "", numeric[3], numeric[2])
    return _vector(kind, numeric[1], language)


def _expression_type(model, node, language):
    kind = node["kind"]
    def typed(child):
        return _expression_type(model, child, language)
    if kind == "name":
        declaration = _glsl_builtin(node["name"], len(model["tokens"])) if language == "glsl" else None
        declaration = declaration or _resolve(model, node["name"], node["start"])
        return declaration["type"], list(declaration["array"])
    if kind == "literal":
        name = node["name"].lower()
        if name in {"true", "false"}:
            return "bool", []
        if name.startswith("0x"):
            if name.endswith(("ull", "llu")):
                return "uint64_t", []
            if name.endswith("ll"):
                return "int64_t", []
            return ("uint" if name.endswith("u") or int(name.rstrip("ul"), 16) > 0x7fffffff else "int"), []
        if "." in name or "e" in name or name.endswith(("f", "h")):
            return ("double" if name.endswith("l" if language == "hlsl" else "lf") else "half" if name.endswith("h") else "float"), []
        return ("uint64_t" if name.endswith(("ull", "llu")) else "int64_t" if name.endswith("ll") else
                "uint" if name.endswith("u") else "int"), []
    if kind == "cast":
        return node["type"], []
    if kind in {"group", "unary", "postfix"}:
        value = typed(node["value"])
        numeric = _numeric(_canonical(model, value[0]), language)
        if kind == "unary" and node["op"] == "!":
            return _shape_type("bool", numeric, language) if numeric and language == "hlsl" else "bool", []
        if kind == "unary" and node["op"] in {"+", "-", "~"} and numeric and numeric[0] == "bool" and language == "hlsl":
            return _shape_type("int", numeric, language), []
        return value
    if kind == "conditional":
        return _common_type(model, typed(node["yes"]), typed(node["no"]), language)
    if kind == "binary":
        if node["op"] == ",":
            return typed(node["right"])
        if node["op"] in _ASSIGN:
            return typed(node["left"])
        result = (typed(node["left"]) if node["op"] in {"<<", ">>"} else
                  _common_type(model, typed(node["left"]), typed(node["right"]), language))
        numeric = _numeric(_canonical(model, result[0]), language)
        if node["op"] in {"==", "!=", "<", ">", "<=", ">=", "&&", "||"}:
            return _shape_type("bool", numeric, language) if numeric and language == "hlsl" else "bool", []
        if numeric and numeric[0] == "bool" and language == "hlsl":
            return _shape_type("int", numeric, language), []
        return result
    if kind in {"member", "index"}:
        name, arrays = typed(node["value"])
        canonical, numeric = _canonical(model, name), _numeric(_canonical(model, name), language)
        if kind == "index":
            if arrays:
                return name, arrays[1:]
            if numeric:
                return (_vector(numeric[0], numeric[3] if language == "hlsl" else numeric[2], language)
                        if numeric[2] else numeric[0]), []
            resource = re.fullmatch(r"(?:RW|RasterizerOrdered)?(?:Structured)?Buffer<(.+)>|(?:RW|RasterizerOrdered)?Texture\w+<(.+)>", canonical)
            if resource:
                return (resource.group(1) or resource.group(2)).split(",", 1)[0], []
            _fail("unsupported-index-type", canonical)
        if arrays:
            _fail("array-element-required")
        if canonical in model["structures"]:
            fields = [field for field in model["structures"][canonical]["fields"] if field["name"] == node["name"]]
            if len(fields) != 1:
                _fail("unresolved-member", node["name"])
            return fields[0]["type"], list(fields[0]["array"])
        member = node["name"]
        if numeric and not numeric[2] and re.fullmatch(r"[xyzw]{1,4}|[rgba]{1,4}|[stpq]{1,4}", member):
            alphabet = "xyzw" if member[0] in "xyzw" else "rgba" if member[0] in "rgba" else "stpq"
            if any(alphabet.index(ch) >= numeric[1] for ch in member):
                _fail("invalid-swizzle")
            return _vector(numeric[0], len(member), language), []
        _fail("unsupported-member-type", canonical)
    if kind != "call":
        _fail("unsupported-value-expression")
    callee, arguments = node["callee"], node["args"]
    if callee["kind"] == "member":
        resource, arrays = typed(callee["value"])
        method = callee["name"]
        if _opaque(resource) and not arrays:
            if method.startswith("SampleCmp") or method in {"CalculateLevelOfDetail", "CalculateLevelOfDetailUnclamped"}:
                return "float", []
            element = re.search(r"<([^,>]+)", resource)
            if method in {"Load", "Sample", "SampleLevel", "SampleBias", "SampleGrad"}:
                return element.group(1) if element else "uint" if "ByteAddressBuffer" in resource else "float4", []
            if method in {"Load2", "Load3", "Load4"} and "ByteAddressBuffer" in resource:
                return "uint" + method[-1], []
            if method.startswith("Gather") and element:
                scalar = _numeric(element.group(1), language)
                if scalar:
                    return _vector(scalar[0], 4, language), []
        _fail("unsupported-call-type", method)
    if callee["kind"] != "name":
        _fail("unsupported-call-type")
    name = callee["name"]
    if name in model["types"]:
        return name, []
    signatures = [function for function in model["functions"] + model.get("prototypes", [])
                  if function["name"] == name and len(function["params"]) == len(arguments)]
    if signatures:
        returns = {_canonical(model, signature["returnType"]) for signature in signatures}
        if len(returns) == 1:
            return returns.pop(), []
        argument_types = [typed(argument) for argument in arguments]
        exact = [signature for signature in signatures if all(
            (_canonical(model, param["type"]), param["array"]) == (_canonical(model, actual[0]), actual[1])
            for param, actual in zip(signature["params"], argument_types))]
        if len({_canonical(model, signature["returnType"]) for signature in exact}) == 1:
            return exact[0]["returnType"], []
        _fail("ambiguous-call-type", name)
    if not arguments:
        _fail("unsupported-call-type", name)
    first = typed(arguments[0])
    numeric = _numeric(_canonical(model, first[0]), language)
    def floating(value):
        shape = _numeric(_canonical(model, value[0]), language)
        integer = {"int", "uint", "int16_t", "uint16_t", "int32_t", "uint32_t",
                   "int64_t", "uint64_t", "min12int", "min16int", "min16uint"}
        # Desktop GLSL also accepts integer arguments to floating overloads,
        # e.g. sin(1). The returned value is float, never the argument's int.
        if shape and (shape[0] in integer or
                      (language == "hlsl" and shape[0] in {"bool", "double", "float64_t"})):
            return _shape_type("float", shape, language), []
        return value
    same = {"abs", "acos", "acosh", "asin", "asinh", "atan", "atanh", "ceil", "cos", "cosh", "degrees", "ddx", "ddy",
            "ddx_coarse", "ddx_fine", "ddy_coarse", "ddy_fine", "dFdx", "dFdy", "dFdxFine", "dFdyFine", "dFdxCoarse", "dFdyCoarse",
            "exp", "exp2", "floor", "frac", "fract", "fwidth", "log", "log2", "log10", "normalize", "radians", "rcp", "round",
            "rsqrt", "inversesqrt", "saturate", "sign", "sin", "sinh", "sqrt", "tan", "tanh", "trunc", "reflect", "refract", "faceforward"}
    if name in same and numeric:
        if name == "sign" and language == "hlsl":
            return _shape_type("int", numeric, language), []
        if name == "saturate" and numeric[0] in {"double", "float64_t"}:
            return first
        return first if name in {"abs", "sign"} and numeric[0] != "bool" else floating(first)
    if name in {"min", "max", "pow", "fmod", "mod", "atan2", "lerp", "mix", "cross"} and len(arguments) >= 2:
        result = _common_type(model, first, typed(arguments[1]), language)
        if name in {"lerp", "mix"} and len(arguments) > 2:
            # GLSL's boolean selector chooses existing lanes; it does not
            # promote the arithmetic type of mix's first two operands.
            selector = typed(arguments[2])
            if language == "hlsl" or not _numeric(_canonical(model, selector[0]), language)[0] == "bool":
                result = _common_type(model, result, selector, language)
        shape = _numeric(_canonical(model, result[0]), language)
        return floating(result) if name in {"pow", "fmod", "atan2", "lerp", "cross"} or (shape and shape[0] == "bool") else result
    if name in {"clamp", "smoothstep", "step"}:
        result = first
        for argument in arguments[1:]:
            result = _common_type(model, result, typed(argument), language)
        shape = _numeric(_canonical(model, result[0]), language)
        return floating(result) if name in {"smoothstep", "step"} or (shape and shape[0] == "bool") else result
    if name in {"dot", "length", "distance", "determinant"} and numeric:
        if name in {"dot", "distance"} and len(arguments) > 1:
            common = _common_type(model, first, typed(arguments[1]), language)
            numeric = _numeric(_canonical(model, common[0]), language)
        result = numeric[0], []
        return result if name == "dot" and numeric[0] != "bool" else floating(result)
    if name in {"all", "any"}:
        return "bool", []
    if name in {"f16tof32", "f32tof16"} and numeric:
        return _vector("float" if name == "f16tof32" else "uint", numeric[1], language), []
    if name in {"isnan", "isinf", "isfinite", "lessThan", "lessThanEqual", "greaterThan", "greaterThanEqual", "equal", "notEqual"} and numeric:
        return _vector("bool", numeric[1], language), []
    if name in {"asfloat", "asint", "asuint", "floatBitsToInt", "floatBitsToUint", "intBitsToFloat", "uintBitsToFloat"} and numeric:
        scalar = "float" if name in {"asfloat", "intBitsToFloat", "uintBitsToFloat"} else "uint" if name in {"asuint", "floatBitsToUint"} else "int"
        return _vector(scalar, numeric[1], language), []
    if name in {"texture", "textureLod", "textureGrad", "textureProj", "textureProjLod", "textureOffset", "textureLodOffset", "texelFetch", "texelFetchOffset", "textureGather", "texture2D", "textureCube"} and _opaque(first[0]):
        if "Shadow" in first[0] and name != "textureGather":
            return "float", []
        return _vector("uint" if first[0].startswith("usampler") else "int" if first[0].startswith("isampler") else "float", 4, language), []
    _fail("unsupported-call-type", name)


def _choices(model, name, arrays, language, path="", depth=0):
    if depth > 12:
        _fail("type-expansion-limit")
    if arrays:
        if arrays[0] is None or arrays[0] < 1 or arrays[0] > 64:
            _fail("select-array-element", path)
        result = []
        for index in range(arrays[0]):
            result.extend(_choices(model, name, arrays[1:], language, path + "[%d]" % index, depth + 1))
        if len(result) > 256:
            _fail("type-expansion-limit")
        return result
    canonical = _canonical(model, name)
    numeric = _numeric(canonical, language)
    if numeric:
        if numeric[2]:
            count = numeric[2] if language == "hlsl" else numeric[3]
            width = numeric[3] if language == "hlsl" else numeric[2]
            return [{"path": path + "[%d]" % i, "type": _vector(numeric[0], width, language)} for i in range(count)]
        return [{"path": path, "type": canonical}]
    if canonical not in model["structures"]:
        if path and _opaque(canonical):
            return []
        _fail("value-not-numeric", name)
    result = []
    for field in model["structures"][canonical]["fields"]:
        result.extend(_choices(model, field["type"], field["array"], language,
                               path + "." + field["name"], depth + 1))
    if len(result) > 256:
        _fail("type-expansion-limit")
    if not result and not path:
        _fail("value-not-numeric", name)
    return result


def _char_offset(source, byte_offset):
    raw = source.encode("utf-8")
    if not isinstance(byte_offset, int) or byte_offset < 0 or byte_offset > len(raw):
        _fail("invalid-source-position")
    try:
        return len(raw[:byte_offset].decode("utf-8"))
    except UnicodeDecodeError:
        _fail("invalid-utf8-position")


def _expression_end(tokens, pairs, start, end=None, delimiters=None):
    end = len(tokens) if end is None else end
    i = start
    delimiters = {";", ",", ")", "]", "}"} if delimiters is None else delimiters
    while i < end and tokens[i]["v"] not in delimiters:
        if tokens[i]["v"] in {"(", "[", "{"}:
            if i not in pairs:
                _fail("incomplete-statement")
            i = pairs[i] + 1
        else:
            i += 1
    return i


def _normalize_selection(source, byte_start, byte_end, tokens, pairs):
    begin = _char_offset(source, byte_start)
    finish = _char_offset(source, byte_start if byte_end is None else byte_end)
    if finish < begin:
        _fail("invalid-source-position")
    hits = [i for i, token in enumerate(tokens) if token["s"] <= begin < token["e"]]
    if not hits and finish > begin:
        hits = [i for i, token in enumerate(tokens) if begin <= token["s"] < finish]
    if not hits and begin:
        hits = [i for i, token in enumerate(tokens) if token["e"] == begin]
    if not hits:
        _fail("select-value")
    hit = hits[0]
    explicit = finish > tokens[hit]["e"] and source[tokens[hit]["e"]:finish].strip()
    if explicit and tokens[hit]["v"] != "return":
        start = hit
        selected = [i for i in range(start, len(tokens)) if tokens[i]["s"] < finish]
        end = selected[-1] + 1
        if tokens[end - 1]["e"] > finish:
            _fail("select-single-value-access")
    elif tokens[hit]["v"] == "return":
        start = hit + 1
        end = _expression_end(tokens, pairs, start, delimiters={";", "}"})
        if end <= start:
            _fail("select-executable-value")
    else:
        if tokens[hit]["kind"] not in {"word", "number"}:
            _fail("select-value")
        def chain_start(index):
            if tokens[index]["v"] in {")", "]"}:
                opening = pairs.get(index)
                if opening is None:
                    _fail("incomplete-statement")
                if tokens[index]["v"] == "]" or (opening and
                        (tokens[opening - 1]["kind"] == "word" or tokens[opening - 1]["v"] in {")", "]"})):
                    return chain_start(opening - 1)
                return opening
            if index >= 2 and tokens[index - 1]["v"] == ".":
                return chain_start(index - 2)
            return index
        start = chain_start(hit)
        end = start + 1
        if tokens[start]["v"] == "(":
            end = pairs[start] + 1
        while end < len(tokens):
            if tokens[end]["v"] in {"(", "["}:
                if end not in pairs:
                    _fail("incomplete-statement")
                end = pairs[end] + 1
            elif tokens[end]["v"] == "." and end + 1 < len(tokens) and tokens[end + 1]["kind"] == "word":
                end += 2
            else:
                break
    begin, finish = tokens[start]["s"], tokens[end - 1]["e"]
    return {"byteStart": len(source[:begin].encode("utf-8")),
            "byteEnd": len(source[:finish].encode("utf-8")),
            "line": source.count("\n", 0, begin) + 1, "expression": source[begin:finish]}


def prepare_source(source, language="hlsl"):
    """Build one source-bound lexical model for multiple selections in the same editor."""
    if language not in {"hlsl", "glsl"} or not isinstance(source, str) or not source or "\0" in source:
        _fail("unsupported-source-language")
    if len(source.encode("utf-8")) > 8 * 1024 * 1024:
        _fail("source-size-limit")
    if _PREFIX in source:
        _fail("reserved-debug-identifier")
    return {"sourceSha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "language": language, "model": _parse(source, language)}


def _prepared_model(source, prepared, language=None):
    if (prepared.get("sourceSha256") != hashlib.sha256(source.encode("utf-8")).hexdigest() or
            (language is not None and prepared.get("language") != language)):
        _fail("stale-source-plan")
    return prepared["model"]


def normalize_selection(source, byte_start, byte_end=None, prepared=None):
    """Expand a caret/token to its lexical value without parsing unrelated types.

    Explicit multi-token expressions retain their selected range. Tolerant
    delimiter matching allows preprocessing to remove inactive source branches.
    """
    if prepared is not None:
        model = _prepared_model(source, prepared)
        tokens, pairs = model["tokens"], model["pairs"]
    else:
        tokens, pairs = _tokens(source, strict=False)
    return _normalize_selection(source, byte_start, byte_end, tokens, pairs)


def prepare_preprocess(source, byte_start, byte_end=None):
    """Mark a source token before the *real target compiler* preprocesses it.

    Marker identifiers are deliberately not macros. They survive ordinary
    preprocessing without depending on compiler-specific comment/column maps.
    Missing or duplicate markers are rejected after preprocessing; that covers
    inactive conditional branches and multiply-expanded macro arguments.
    """
    selection = normalize_selection(source, byte_start, byte_end)
    begin = _char_offset(source, selection["byteStart"])
    end = _char_offset(source, selection["byteEnd"])
    digest = hashlib.sha256((source + str(begin) + ":" + str(end)).encode("utf-8")).hexdigest()[:20]
    first, last = "BREAD_VISUAL_BEGIN_" + digest, "BREAD_VISUAL_END_" + digest
    if first in source or last in source:
        _fail("reserved-debug-identifier")
    return {"source": source[:begin] + first + " " + source[begin:end] + " " + last + source[end:],
            "beginMarker": first, "endMarker": last, "originalSelection": selection}


def needs_preprocess(source):
    return bool(re.search(r"(?m)^\s*#\s*(?:define|undef|if|ifdef|ifndef|elif|else|endif|include)\b", source))


def finish_preprocess(preprocessed, prepared):
    first, last = prepared["beginMarker"], prepared["endMarker"]
    if preprocessed.count(first) != 1 or preprocessed.count(last) != 1:
        _fail("preprocessed-selection-ambiguous")
    start = preprocessed.index(first)
    end = preprocessed.index(last)
    if end < start:
        _fail("preprocessed-selection-ambiguous")
    selected = preprocessed[start + len(first):end]
    leading = len(selected) - len(selected.lstrip())
    trailing = len(selected.rstrip())
    clean = preprocessed[:start] + selected + preprocessed[end + len(last):]
    return {"source": clean, "byteStart": len(clean[:start + leading].encode("utf-8")),
            "byteEnd": len(clean[:start + trailing].encode("utf-8")),
            "originalSelection": dict(prepared["originalSelection"])}


def _validate_expression_subtree(model, start, end):
    """A selected span must be one original expression, not regroup operands.

    For example, wrapping ``a+b`` in ``2*a+b`` would change both arithmetic
    precedence and, for logical operators, which side effects execute.
    """
    tokens, pairs = model["tokens"], model["pairs"]
    containers = [(o, c) for o, c in pairs.items() if o < start and end <= c]
    opening, closing = max(containers, default=(-1, len(tokens)), key=lambda p: p[0])
    begin, finish = opening + 1, closing
    # At the top level of a block or for header, statements have independent
    # expression trees. Commas split arguments only in an actual call list.
    delimiters = {";"}
    if opening >= 1 and tokens[opening]["v"] == "(" and (
            tokens[opening - 1]["kind"] == "word" and tokens[opening - 1]["v"] not in _CONTROL):
        delimiters.add(",")
    cursor = begin
    while cursor < closing:
        value = tokens[cursor]["v"]
        if value in delimiters or value in {"{", "}"}:
            if cursor < start:
                begin = pairs[cursor] + 1 if value == "{" else cursor + 1
            elif cursor >= end:
                finish = cursor
                break
        if value in {"(", "[", "{"}:
            cursor = pairs[cursor] + 1
        else:
            cursor += 1
    # Remove statement syntax, retaining every operator in the expression.
    while begin < start:
        value = tokens[begin]["v"]
        if value in {"return", "else", "do"} or value in _QUALIFIERS:
            begin += 1
        elif value in _CONTROL and begin + 1 < start and tokens[begin + 1]["v"] == "(" and pairs[begin + 1] < start:
            begin = pairs[begin + 1] + 1
        elif value in model["types"] and begin + 1 < len(tokens) and tokens[begin + 1]["kind"] == "word":
            begin += 1
        else:
            break
    try:
        root = _ExpressionParser(model, begin, finish).parse()
    except VisualDebugError:
        _fail("partial-expression-selection")
    def contains(node):
        if node.get("start") == start and node.get("end") == end:
            return True
        for value in node.values():
            if isinstance(value, dict) and contains(value):
                return True
            if isinstance(value, list) and any(contains(child) for child in value if isinstance(child, dict)):
                return True
        return False
    if not contains(root):
        _fail("partial-expression-selection")


def inspect(source, byte_start, byte_end=None, language="hlsl", entry_point="main", prepared=None):
    """Return a serializable plan bound to exact source bytes and a value access."""
    if language not in {"hlsl", "glsl"} or not isinstance(source, str) or not source or "\0" in source:
        _fail("unsupported-source-language")
    if len(source.encode("utf-8")) > 8 * 1024 * 1024:
        _fail("source-size-limit")
    if _PREFIX in source:
        _fail("reserved-debug-identifier")
    # Macro-expanded positions have no trustworthy source expression identity.
    # A preprocessed unit with a preserved position map can be passed by a host.
    definitions = set(re.findall(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)", source))
    model = _prepared_model(source, prepared, language) if prepared is not None else _parse(source, language)
    tokens, pairs = model["tokens"], model["pairs"]
    entries = [f for f in model["functions"] if f["name"] == entry_point]
    if len(entries) != 1:
        _fail("entry-point-ambiguous", entry_point)
    selection = _normalize_selection(source, byte_start, byte_end, tokens, pairs)
    begin = _char_offset(source, selection["byteStart"])
    finish = _char_offset(source, selection["byteEnd"])
    start = next(i for i, t in enumerate(tokens) if t["s"] == begin)
    end = next(i + 1 for i, t in enumerate(tokens) if t["e"] == finish)
    owner = [f for f in model["functions"] if f["bodyOpen"] < start < end <= f["bodyClose"]]
    if len(owner) != 1:
        _fail("select-executable-value")
    if any(tokens[i]["v"] in definitions for i in range(start, end)):
        _fail("macro-position-unavailable")
    if any(tokens[i]["v"] in definitions and i + 1 < len(tokens) and tokens[i + 1]["v"] == "(" and
           i + 1 < start < pairs[i + 1] for i in range(len(tokens) - 1)):
        _fail("macro-position-unavailable")
    if re.search(r"(?m)^\s*#\s*(?:if|ifdef|ifndef|elif|else|endif|include)\b", source):
        _fail("preprocessed-source-required")
    declarations = [d for d in model["declarations"] if d["nameToken"] == start]
    base = declarations[0] if declarations else None
    declaration = base is not None
    insertion, initializer_type = None, None
    capture_start, capture_end = begin, finish
    kind = "read"
    if declaration:
        value_type, arrays = base["type"], list(base["array"])
        if end >= len(tokens) or tokens[end]["v"] != "=":
            _fail("uninitialized-declaration")
        initializer_end = _expression_end(tokens, pairs, end + 1)
        if initializer_end <= end + 1 or initializer_end >= len(tokens):
            _fail("unsupported-declaration")
        in_header = any(tokens[o]["v"] == "(" and o < start < c for o, c in pairs.items() if o < c)
        compound = base.get("compound") or tokens[initializer_end]["v"] == ","
        if in_header or compound:
            if arrays or tokens[end + 1]["v"] == "{":
                _fail("array-access-needs-element-selection")
            capture_start, capture_end = tokens[end + 1]["s"], tokens[initializer_end - 1]["e"]
            initializer_type = value_type
        else:
            insertion = tokens[initializer_end]["e"]
        kind = "initialized-declaration"
        selection.update(byteEnd=len(source[:tokens[start]["e"]].encode("utf-8")),
                         expression=tokens[start]["v"])
    else:
        node = _ExpressionParser(model, start, end).parse()
        if node["kind"] in {"binary", "conditional", "unary", "group"}:
            _validate_expression_subtree(model, start, end)
        value_type, arrays = _expression_type(model, node, language)
        kind = "call-result" if node["kind"] == "call" else "read" if node["kind"] in {"name", "member", "index", "literal"} else "expression"
        original_begin = _char_offset(source, byte_start)
        if any(t["v"] == "return" and t["s"] <= original_begin <= t["e"] for t in tokens):
            kind = "return-value"
        if end < len(tokens) and tokens[end]["v"] in _ASSIGN:
            assignment_end = _expression_end(tokens, pairs, end + 1)
            if assignment_end == end + 1:
                _fail("incomplete-assignment")
            capture_end, kind = tokens[assignment_end - 1]["e"], "assigned-value"
        elif end < len(tokens) and tokens[end]["v"] in {"++", "--"}:
            capture_end, kind = tokens[end]["e"], "postfix-original-value"
        elif start and tokens[start - 1]["v"] in {"++", "--"}:
            capture_start, kind = tokens[start - 1]["s"], "prefix-updated-value"
        # Only a selected lvalue must remain writable. Selecting a whole call,
        # or an expression used to index an out argument, does not replace it.
        for function in model["functions"] + model["prototypes"]:
            if not any(set(p["qualifiers"]) & {"out", "inout"} for p in function["params"]):
                continue
            for i in range(len(tokens) - 1):
                if tokens[i]["v"] != function["name"] or tokens[i + 1]["v"] != "(" or i == function["nameToken"]:
                    continue
                for n, (arg_start, arg_end) in enumerate(_parts(tokens, pairs, i + 2, pairs[i + 1])):
                    if not (arg_start <= start < end <= arg_end and n < len(function["params"]) and
                            set(function["params"][n]["qualifiers"]) & {"out", "inout"}):
                        continue
                    inside_index = any(tokens[o]["v"] == "[" and arg_start <= o < start < c < arg_end
                                       for o, c in pairs.items() if o < c)
                    if not inside_index:
                        _fail("out-argument-position")
    choices = _choices(model, value_type, arrays, language)
    if arrays and kind != "initialized-declaration":
        if kind != "read" or re.search(r"\+\+|--|\(|=|\?|,", selection["expression"]):
            _fail("array-access-needs-element-selection")
    selection["positionKind"] = kind
    return {"schema": "bread-shader-visual-plan/v1", "source": source,
            "sourceSha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "language": language, "entryPoint": entry_point, "choices": choices,
            "selection": selection, "valueType": value_type, "valueArrays": arrays,
            "captureStart": capture_start, "captureEnd": capture_end, "insertAfter": insertion,
            "initializerType": initializer_type, "model": model, "entry": entries[0], "owner": owner[0]}


def _color_expression(expression, numeric, language, mode, low, high, channel):
    width = numeric[1]
    four = _vector("float", 4, language)
    three = _vector("float", 3, language)
    if channel >= 0:
        if channel >= width:
            _fail("channel-out-of-range")
        expression = "(%s)%s" % (expression, "." + "xyzw"[channel] if width > 1 else "")
        width = 1
    lanes = ["float(%s%s)" % (expression, "." + "xyzw"[i] if width > 1 else "") for i in range(width)]
    if width == 1 and (mode == "custom" or channel >= 0):
        lanes *= 3
    else:
        lanes += ["0.0"] * (3 - width)
    if width < 4:
        lanes.append("1.0")
    raw = four + "(" + ", ".join(lanes) + ")"
    if mode in {"raw", "custom"}:
        return raw
    if mode == "coverage":
        return four + "(1.0, 1.0, 1.0, 1.0)"
    if mode == "grayscale":
        gray = lanes[0] if width == 1 else "dot((%s).xyz, %s(0.2126, 0.7152, 0.0722))" % (raw, three)
        return four + "(%s, %s, %s, 1.0)" % (gray, gray, gray)
    if mode == "normal":
        return four + "((%s).xyz * 0.5 + %s(0.5, 0.5, 0.5), 1.0)" % (raw, three)
    if mode == "range":
        return four + "(((%s).xyz - %s(%s, %s, %s)) / %s, (%s).w)" % (raw, three, _literal(low), _literal(low), _literal(low), _literal(high - low), raw)
    _fail("unsupported-display-mode")


def _literal(value):
    result = format(float(value), ".17g")
    return result if "." in result or "e" in result.lower() else result + ".0"


def _flatten_outputs(model, type_name, path="", depth=0):
    if depth > 12:
        _fail("output-expansion-limit")
    canonical = _canonical(model, type_name)
    if canonical not in model["structures"]:
        return []
    result = []
    for field in model["structures"][canonical]["fields"]:
        field_path = path + "." + field["name"]
        if field["array"]:
            _fail("output-array-layout-unsupported")
        if field["semantic"]:
            result.append(dict(field, path=field_path))
        else:
            result.extend(_flatten_outputs(model, field["type"], field_path, depth + 1))
    return result


def _target_semantic(semantic, index):
    return re.fullmatch(r"(?:SV_Target|COLOR)(%d)?" % index, semantic, re.IGNORECASE) is not None if index == 0 else re.fullmatch(r"(?:SV_Target|COLOR)%d" % index, semantic, re.IGNORECASE) is not None


def _position_input(model, type_name, path="", depth=0):
    """Find a scalar SV_Position input without expanding unrelated input arrays.

    Output flattening has stricter rules because it rewrites the interface. Input
    structures are passed through unchanged, including legal TEXCOORD arrays.
    """
    if depth > 12:
        _fail("output-expansion-limit")
    structure = model["structures"].get(_canonical(model, type_name))
    if structure is None:
        return None
    for field in structure["fields"]:
        if field["array"]:
            continue
        field_path = path + "." + field["name"]
        if field["semantic"].lower() == "sv_position":
            return field_path
        if not field["semantic"]:
            found = _position_input(model, field["type"], field_path, depth + 1)
            if found is not None:
                return found
    return None


def _unreached(output_type, components, position=None, language="hlsl"):
    """Opaque magenta/dark checker, keeping 'not executed' distinct from zero."""
    red = "1"
    if position:
        red = "(%s(floor((%s).x / 8.0) + floor((%s).y / 8.0), 2.0) < 1.0 ? 1 : 0)" % ("fmod" if language == "hlsl" else "mod", position, position)
    lanes = [red, "0", red, "1"][:components]
    return "  if (!_bread_visual_hit) _bread_visual_output = %s(%s);\n" % (output_type, ", ".join(lanes))


def _apply_edits(source, edits):
    ordered = sorted(edits, key=lambda e: (e[0], e[1]), reverse=True)
    boundary = len(source) + 1
    for start, end, replacement in ordered:
        if end > boundary:
            _fail("overlapping-instrumentation")
        source = source[:start] + replacement + source[end:]
        boundary = start
    return source


def _rename_value(text, name, replacement):
    tokens, _ = _tokens(text)
    return _apply_edits(text, [(token["s"], token["e"], replacement)
                              for i, token in enumerate(tokens)
                              if token["v"] == name and (i == 0 or tokens[i - 1]["v"] != ".")])


def instrument(plan, member="", target=None, mode="raw", range_min=0.0,
               range_max=1.0, iteration=0, channel=-1):
    """Build an ephemeral probe; the caller compiles and validates its interface.

    ``iteration`` is zero based (0 records the first executed hit). ``member``
    is one of inspect's choice paths.  ``target`` has index, components and
    componentType (float/sint/uint); widening affects the wrapper output only.
    """
    if plan.get("schema") != "bread-shader-visual-plan/v1":
        _fail("invalid-plan")
    source = plan["source"]
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != plan["sourceSha256"]:
        _fail("stale-source-plan")
    if not isinstance(iteration, int) or not 0 <= iteration <= 65535:
        _fail("invalid-hit-index")
    if not isinstance(channel, int) or not -1 <= channel <= 3:
        _fail("invalid-channel")
    if not math.isfinite(float(range_min)) or not math.isfinite(float(range_max)) or range_max <= range_min:
        _fail("invalid-display-range")
    matches = [choice for choice in plan["choices"] if choice["path"] == member or choice["path"].lstrip(".") == member]
    if len(matches) != 1:
        _fail("member-selection-required")
    choice = matches[0]
    member = choice["path"]
    target = dict(target or {"index": 0, "components": 4, "componentType": "float"})
    index, components, component_type = target.get("index"), target.get("components"), target.get("componentType")
    if not isinstance(index, int) or not 0 <= index < 8 or components not in {1, 2, 3, 4} or component_type not in {"float", "sint", "uint"}:
        _fail("invalid-output-target")
    language, model, entry = plan["language"], plan["model"], plan["entry"]
    tokens = model["tokens"]
    embedded_glsl = language == "glsl" and bool(re.search(r"(?m)^\s*#\s*version\s+(?:100\b|\d+\s+es\b)", source) or
        (not re.search(r"(?m)^\s*#\s*version\b", source) and re.search(r"\bprecision\s+(?:lowp|mediump|highp)\b", source)))
    precision = ("highp " if re.search(r"\bhighp\b", source) or re.search(r"#\s*version\s+[3-9]\d*\s+es\b", source) else "mediump ") if embedded_glsl else ""
    numeric = _numeric(choice["type"], language)
    if not numeric or numeric[2]:
        _fail("value-not-numeric")
    output_type = _vector({"sint": "int"}.get(component_type, component_type), components, language)
    float4 = _vector("float", 4, language)
    # Retain exact untransformed integer lanes, avoiding float32 loss. Custom
    # scalar and explicit channel displays replicate RGB before target slicing.
    array_probe = bool(plan.get("valueArrays"))
    value = "_bread_visual_value" + ("" if array_probe else member)
    display = _color_expression(value, numeric, language, mode, range_min, range_max, channel)
    if component_type in {"sint", "uint"} and (mode in {"raw", "custom"} or
            (mode == "grayscale" and (numeric[1] == 1 or channel >= 0))):
        width = numeric[1]
        if channel >= 0:
            value += "." + "xyzw"[channel] if width > 1 else ""
            width = 1
        kind = "int" if component_type == "sint" else "uint"
        lanes = ["%s(%s%s)" % (kind, value, "." + "xyzw"[i] if width > 1 else "") for i in range(width)]
        if width == 1 and (mode != "raw" or channel >= 0):
            lanes *= 3
        for i in range(len(lanes), 4):
            lanes.append("1" if i == 3 else "0")
        output_capture = output_type + "(" + ", ".join(lanes[:components]) + ")"
    else:
        suffix = "." + "xyzw"[:components] if components < 4 else ""
        output_capture = output_type + "((" + display + ")" + suffix + ")"
    state = ("\n// Bread temporary value visualization; original source is not saved.\n"
             "%sbool _bread_visual_hit = false;\n"
             "%sint _bread_visual_count = 0;\n"
             "%s%s _bread_visual_output = %s(%s);\n") % (
                 "static " if language == "hlsl" else "", "static " if language == "hlsl" else "highp " if embedded_glsl else "",
                 "static " if language == "hlsl" else precision, output_type, output_type, ", ".join(["0"] * components))
    probe_type = choice["type"] if array_probe else plan["valueType"]
    probe_numeric = _numeric(_canonical(model, probe_type), language)
    if precision and probe_numeric and probe_numeric[0] != "bool":
        probe_type = precision + probe_type
    probe = ("\n%s _bread_visual_capture(%s _bread_visual_value)\n{\n"
             "  if (!_bread_visual_hit && _bread_visual_count++ == %d)\n  {\n"
             "    _bread_visual_output = %s;\n    _bread_visual_hit = true;\n  }\n"
             "  return _bread_visual_value;\n}\n") % (probe_type, probe_type, iteration, output_capture)
    edits = []
    # Insert probe immediately before its containing function, after all types
    # and resource declarations that make the value type available.
    owner_start = tokens[plan["owner"]["definitionStart"]]["s"]
    while owner_start > 0 and source[owner_start - 1] in " \t":
        owner_start -= 1
    edits.append((owner_start, owner_start, probe))
    insert = plan["insertAfter"]
    if insert is not None:
        edits.append((insert, insert, "\n_bread_visual_capture(%s);" % (plan["selection"]["expression"] + (member if array_probe else ""))))
    else:
        start, end = plan["captureStart"], plan["captureEnd"]
        expression = source[start:end]
        if plan.get("initializerType"):
            expression = ("(%s)(%s)" if language == "hlsl" else "%s(%s)") % (plan["initializerType"], expression)
        elif "," in expression:
            # A comma expression is one argument, not multiple probe arguments.
            expression = "(" + expression + ")"
        replacement = "_bread_visual_capture(%s)" % expression
        if array_probe:
            replacement = "(_bread_visual_capture(%s%s), %s)" % (source[start:end], member, source[start:end])
        edits.append((start, end, replacement))
    # Suppression is conditional, so discard before the observed hit retains
    # its original meaning. Expressions are otherwise evaluated unchanged.
    for i, token in enumerate(tokens):
        if token["v"] == "discard" and i + 1 < len(tokens) and tokens[i + 1]["v"] == ";":
            edits.append((token["s"], tokens[i + 1]["e"], "{ if (!_bread_visual_hit) discard; }"))
        if language == "hlsl" and token["v"] == "clip" and i + 1 < len(tokens) and tokens[i + 1]["v"] == "(":
            close = model["pairs"][i + 1]
            if close + 1 >= len(tokens) or tokens[close + 1]["v"] != ";":
                _fail("clip-expression-unsupported")
            edits.append((token["s"], tokens[close + 1]["e"], "{ if (!_bread_visual_hit) " + source[token["s"]:tokens[close + 1]["e"]] + " }"))
    original_name = "_bread_visual_original"
    name_token = tokens[entry["nameToken"]]
    edits.append((name_token["s"], name_token["e"], original_name))
    signature = None
    unmatched_position = None
    if language == "hlsl":
        outputs = []
        if entry["semantic"]:
            outputs.append({"type": entry["returnType"], "semantic": entry["semantic"], "path": "", "root": "_bread_visual_result"})
        else:
            outputs.extend(dict(item, root="_bread_visual_result") for item in _flatten_outputs(model, entry["returnType"]))
        params = entry["params"]
        for param in params:
            if "out" not in param["qualifiers"]:
                if param["semantic"].lower() == "sv_position":
                    unmatched_position = param["name"]
                position_path = _position_input(model, param["type"])
                if position_path is not None:
                    unmatched_position = param["name"] + position_path
        for param in params:
            if "inout" in param["qualifiers"]:
                _fail("entry-inout-unsupported")
            if "out" in param["qualifiers"]:
                if param["semantic"]:
                    outputs.append(dict(param, path="", root=param["name"]))
                else:
                    outputs.extend(dict(item, root=param["name"]) for item in _flatten_outputs(model, param["type"]))
        selected = [item for item in outputs if _target_semantic(item["semantic"], index)]
        if len(selected) != 1:
            _fail("output-target-unavailable", str(index))
        chosen = selected[0]
        # Preserve every output semantic while adapting only the debug target.
        fields, copies = [], []
        for n, item in enumerate(outputs):
            is_target = item is chosen
            fields.append("  %s _bread_visual_o%d : %s;" % (output_type if is_target else item["type"], n, item["semantic"]))
            copies.append("  _bread_visual_final._bread_visual_o%d = %s;" % (n, "_bread_visual_output" if is_target else item["root"] + item["path"]))
        wrapper_params, locals_ = [], []
        for param in params:
            if "out" in param["qualifiers"]:
                if param["array"]:
                    _fail("entry-output-array-unsupported")
                locals_.append("  %s %s;" % (param["type"], param["name"]))
            else:
                wrapper_params.append(source[tokens[param["startToken"]]["s"]:tokens[param["endToken"] - 1]["e"]])
        call = original_name + "(" + ", ".join(param["name"] for param in params) + ")"
        invocation = "  " + (call + ";" if entry["returnType"] == "void" else entry["returnType"] + " _bread_visual_result = " + call + ";")
        attributes = source[tokens[entry["definitionStart"]]["s"]:tokens[entry["typeToken"]]["s"]]
        # Entry-only attributes belong on the wrapper as well. Static/inline
        # are harmless on the original callable and need not be copied.
        attributes = " ".join(re.findall(r"\[[^\]]+\]", attributes))
        wrapper = ("\nstruct _bread_visual_Outputs\n{\n%s\n};\n%s\n"
                   "_bread_visual_Outputs %s(%s)\n{\n%s\n%s\n%s"
                   "  _bread_visual_Outputs _bread_visual_final;\n%s\n"
                   "  return _bread_visual_final;\n}\n") % (
                       "\n".join(fields), attributes, plan["entryPoint"], ", ".join(wrapper_params),
                       "\n".join(locals_), invocation, _unreached(output_type, components, unmatched_position), "\n".join(copies))
        signature = {"semantic": chosen["semantic"], "originalType": chosen["type"], "type": output_type}
    else:
        # GLSL global outputs retain their real location/name linkage. The
        # original variable becomes private with its old type, so widening the
        # debug attachment cannot change the original program's arithmetic.
        candidates = []
        requested_name = target.get("outputName") or (target.get("name") if not target.get("format") else None)
        for decl in model["declarations"]:
            if decl["scope"][0] != -1:
                continue
            n = decl["typeToken"]
            begin = n
            while begin and tokens[begin - 1]["v"] not in {";", "}"}:
                begin -= 1
            prefix = source[tokens[begin]["s"]:tokens[n]["s"]]
            if not re.search(r"\bout\b", prefix):
                continue
            location = re.search(r"\blocation\s*=\s*(\d+)", prefix)
            if (location and int(location.group(1)) == index) or (not location and
                    ((requested_name and decl["name"] == requested_name) or (not requested_name and index == 0))):
                candidates.append((decl, begin, prefix))
        builtin = None
        if not candidates:
            builtins = {token["v"] for token in tokens}
            if index == 0 and "gl_FragColor" in builtins:
                builtin = "gl_FragColor"
            elif "gl_FragData" in builtins:
                builtin = "gl_FragData"
        if not builtin and len(candidates) != 1:
            _fail("output-target-unavailable", str(index))
        if builtin:
            if component_type != "float":
                _fail("legacy-glsl-integer-output-unavailable")
            name, private = builtin, "_bread_visual_original_output"
            chosen = {"nameToken": -1, "type": "vec4"}
            state += "%svec4 %s%s;\n" % (precision, private, "[gl_MaxDrawBuffers]" if builtin == "gl_FragData" else "")
        else:
            chosen, begin, prefix = candidates[0]
            if chosen["array"] or chosen["type"] in model["structures"]:
                _fail("glsl-output-block-unsupported")
            end = chosen["endToken"]
            if tokens[end]["v"] != ";":
                _fail("glsl-output-declaration-unsupported")
            name = chosen["name"]
            if any(d["name"] == name and d["nameToken"] != chosen["nameToken"] for d in model["declarations"]):
                _fail("shadowed-output-name")
            private = "_bread_visual_original_output"
            declaration = ("%s %s;\n%s%s %s;" % (chosen["type"], private, prefix, output_type, name))
            edits.append((tokens[begin]["s"], tokens[end]["e"], declaration))
        for i, token in enumerate(tokens):
            if token["v"] == name and i != chosen["nameToken"] and (not i or tokens[i - 1]["v"] != "."):
                if plan["captureStart"] <= token["s"] < plan["captureEnd"]:
                    edits = [(a, b, _rename_value(text, name, private) if a == plan["captureStart"] and b == plan["captureEnd"] else text)
                             for a, b, text in edits]
                    continue
                edits.append((token["s"], token["e"], private))
        unmatched_position = "gl_FragCoord"
        assignment = "_bread_visual_output"
        if builtin and components < 4:
            lanes = ["_bread_visual_output" + ("." + "xyzw"[i] if components > 1 else "") for i in range(components)]
            lanes += ["0.0"] * (3 - components) + ["1.0"]
            assignment = "vec4(" + ", ".join(lanes) + ")"
        destination = name + ("[%d]" % index if builtin == "gl_FragData" else "")
        copy_others = ""
        if builtin == "gl_FragData":
            copy_others = "  for (int _bread_visual_i = 0; _bread_visual_i < gl_MaxDrawBuffers; ++_bread_visual_i) gl_FragData[_bread_visual_i] = _bread_visual_original_output[_bread_visual_i];\n"
        wrapper = "\nvoid main()\n{\n  %s();\n%s%s  %s = %s;\n}\n" % (original_name, _unreached(output_type, components, unmatched_position, "glsl"), copy_others, destination, assignment)
        signature = {"location": index, "name": name, "originalType": chosen["type"], "type": "vec4" if builtin else output_type}
    # Keep #version/#extension and default precision declarations before GLSL
    # state. HLSL global state can precede all user declarations.
    state_at = 0
    if language == "glsl":
        for match in re.finditer(r"(?m)^\s*#(?:version|extension)[^\n]*(?:\r?\n|$)", source):
            state_at = max(state_at, match.end())
        for i in range(len(tokens) - 3):
            if (tokens[i]["v"] == "precision" and tokens[i + 3]["v"] == ";" and tokens[i]["s"] < owner_start and
                    not any(o < i < c and tokens[o]["v"] == "{" for o, c in model["pairs"].items() if o < c)):
                state_at = max(state_at, tokens[i + 3]["e"])
    edits.append((state_at, state_at, state))
    generated = _apply_edits(source, edits) + wrapper
    return {"source": generated, "entryPoint": plan["entryPoint"], "selection": dict(plan["selection"], member=member),
            "output": target, "selectedOutputSignature": signature,
            "semantics": "observe-at-expression", "requiresCompilerValidation": True,
            "sourceSha256": plan["sourceSha256"], "hitIndex": iteration,
            "warnings": ["later-memory-side-effects-preserved", "unreached-output-checkerboard" if unmatched_position else "unreached-output-magenta"]}
