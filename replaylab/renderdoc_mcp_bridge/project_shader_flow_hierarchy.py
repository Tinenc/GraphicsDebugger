"""Bounded, source-derived Shader graph projections (Python 3.6 compatible).

This parser builds navigation aids, not compiler or equivalence evidence. It
never reads files and never changes the accepted project graph. The workspace
owns the current buffer and attaches native bookmark anchors to returned nodes.
"""

from __future__ import print_function

import bisect
import copy
import hashlib
import re


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOKENS = 250000
MAX_FUNCTIONS = 512
MAX_GRAPH_NODES = 256
MAX_LOCAL_NODES = 128
MAX_CONTROL_DEPTH = 48
MAX_LABEL_LENGTH = 160
MAX_DESCRIPTION_LENGTH = 640
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[uUfFlL]*|==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|<<|>>|[^\s]")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CONTROLS = frozenset(("if", "for", "while", "switch", "catch"))
# Data-flow summary helpers (which fields/resources a span reads and writes).
_ASSIGN_ONE = frozenset(("=", "+=", "-=", "*=", "/="))
_ASSIGN_TWO = frozenset(("%", "&", "|", "^", "<<", ">>"))  # followed by "="
_PARAM_MODIFIERS = frozenset(("in", "out", "inout", "uniform", "const", "precise", "nointerpolation",
                              "linear", "centroid", "noperspective", "sample", "static", "volatile",
                              "row_major", "column_major", "snorm", "unorm", "globallycoherent"))
_RESOURCE_PREFIXES = ("Texture", "RWTexture", "Sampler", "Buffer", "RWBuffer", "StructuredBuffer",
                      "RWStructuredBuffer", "ByteAddressBuffer", "RWByteAddressBuffer", "ConstantBuffer",
                      "AppendStructuredBuffer", "ConsumeStructuredBuffer", "RaytracingAccelerationStructure",
                      "FeedbackTexture", "RasterizerOrdered")
_SWIZZLE = re.compile(r"^(?:[xyzw]{1,4}|[rgba]{1,4}|_m\d\d(?:_m\d\d){0,3}|_\d\d(?:_\d\d){0,3})$")
_IO_LIMIT = 24


def _bounded_text(value, limit, one_line=False):
    if value is None:
        return ""
    text = _CONTROL_RE.sub("", str(value)).strip()
    if one_line:
        text = " ".join(text.split())
    if len(text) > int(limit):
        text = text[:max(0, int(limit) - 1)].rstrip() + "\u2026"
    return text


def _masked_source(source):
    characters = list(source)
    pattern = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    for match in pattern.finditer(source):
        for position in range(match.start(), match.end()):
            if characters[position] not in "\r\n":
                characters[position] = " "
    continuation = False
    offset = 0
    for line in "".join(characters).splitlines(True):
        if continuation or line.lstrip().startswith("#"):
            continuation = line.rstrip().endswith("\\")
            for position in range(offset, offset + len(line)):
                if characters[position] not in "\r\n":
                    characters[position] = " "
        else:
            continuation = False
        offset += len(line)
    return "".join(characters)


def _pairs(words, opening, closing):
    stack, result = [], {}
    for index, word in enumerate(words):
        if word == opening:
            stack.append(index)
        elif word == closing:
            if not stack:
                raise ValueError("Shader source has unmatched delimiters")
            result[stack.pop()] = index
    if stack:
        raise ValueError("Shader source has unmatched delimiters")
    return result


class SourceFlowHierarchy(object):
    """One immutable buffer index; construct a fresh index when text changes."""

    def __init__(self, source, source_identity=None):
        if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("Shader source exceeds the graph projection limit")
        self.source = source
        self.source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        self.identity = {}
        for key in ("workspaceId", "documentId", "fileId", "path", "relativePath", "file", "sourceRevision"):
            value = (source_identity or {}).get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                self.identity[key] = str(value)[:1024]
        self.identity["sourceSha256"] = self.source_sha256
        self.tokens = list(_TOKEN.finditer(_masked_source(source)))
        if len(self.tokens) > MAX_TOKENS:
            raise ValueError("Shader source exceeds the graph token limit")
        self.words = [token.group(0) for token in self.tokens]
        self.parens = _pairs(self.words, "(", ")")
        self.braces = _pairs(self.words, "{", "}")
        self.brackets = _pairs(self.words, "[", "]")
        self.struct_types = {self.words[i + 1] for i, word in enumerate(self.words[:-1])
                             if word == "struct" and _IDENTIFIER.match(self.words[i + 1])}
        self.lines = [0] + [index + 1 for index, char in enumerate(source) if char == "\n"]
        self.functions = {}
        self.by_name = {}
        self.function_annotations = {}
        self.helper_function_ids = set()
        depth = 0
        for index, word in enumerate(self.words):
            if word == "}":
                depth -= 1
            if (depth == 0 and _IDENTIFIER.match(word) and word not in _CONTROLS and
                    index > 0 and index + 1 in self.parens and
                    (_IDENTIFIER.match(self.words[index - 1]) or self.words[index - 1] in (">", "]", "*", "&"))):
                close = self.parens[index + 1]
                suffix = close + 1
                if suffix < len(self.words) and self.words[suffix] == ":":
                    while suffix < len(self.words) and self.words[suffix] not in ("{", ";"):
                        suffix += 1
                if suffix in self.braces:
                    signature = word + "(" + " ".join(self.words[index + 2:close]) + ")"
                    function_id = "function-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
                    record = {"id": function_id, "name": word, "signature": signature,
                              "start": suffix + 1, "end": self.braces[suffix],
                              "declaration": index}
                    # Duplicate signatures (e.g. unresolved preprocessor arms)
                    # are ambiguous; no declaration is silently preferred.
                    self.by_name.setdefault(word, []).append(record)
                    self.functions[function_id] = record if function_id not in self.functions else None
                    if sum(len(items) for items in self.by_name.values()) > MAX_FUNCTIONS:
                        raise ValueError("Shader source has too many functions for graph navigation")
            if word == "{":
                depth += 1

    # ------------------------------------------------------------------
    # Data-flow summaries ("what does this node read / write")
    # ------------------------------------------------------------------
    def file_scope(self):
        """Names declared at file scope: {name: typeWord}. Cached per index.

        Covers globals (``static float4 g``), resources (``Texture2D<float4> t14``)
        and members of cbuffer/tbuffer blocks, which HLSL exposes as globals.
        """
        cached = getattr(self, "_file_scope_cache", None)
        if cached is not None:
            return cached
        words, result = self.words, {}
        depth, paren, in_cbuffer = 0, 0, None
        statement_start = 0
        for i, word in enumerate(words):
            if word == "(":
                paren += 1
                continue
            if word == ")":
                paren = max(0, paren - 1)
                continue
            if word == "{":
                depth += 1
                # "cbuffer Name [: register(bN)] {" -> members are globals.
                if depth == 1 and statement_start < i and words[statement_start] in ("cbuffer", "tbuffer"):
                    in_cbuffer = depth
                statement_start = i + 1
                continue
            if word == "}":
                if in_cbuffer == depth:
                    in_cbuffer = None
                depth -= 1
                statement_start = i + 1
                continue
            if word == ";":
                statement_start = i + 1
                continue
            if paren > 0:
                continue  # inside a parameter list / call / register(...)
            member_scope = in_cbuffer is not None and depth == in_cbuffer
            if depth != 0 and not member_scope:
                continue
            if not _IDENTIFIER.match(word) or word in _PARAM_MODIFIERS or word in ("struct", "cbuffer", "tbuffer", "return", "typedef"):
                continue
            # word is a type candidate when followed by [<...>] NAME and then ; = : [ ,
            j = i + 1
            if j < len(words) and words[j] == "<":
                k = j
                while k < len(words) and words[k] != ">":
                    k += 1
                j = k + 1
            if j < len(words) and _IDENTIFIER.match(words[j]) and j + 1 < len(words) and words[j + 1] in (";", "=", ":", "[", ","):
                if words[j] not in result and i > 0 and words[i - 1] not in (".", "::"):
                    result[words[j]] = word
                    # comma-separated globals: static const float a = 1, b = 2;
                    k, inner = j + 1, 0
                    while k < len(words) and not (words[k] == ";" and inner == 0):
                        if words[k] in ("(", "[", "{"):
                            inner += 1
                        elif words[k] in (")", "]", "}"):
                            inner -= 1
                            if inner < 0:
                                break
                        elif words[k] == "," and inner == 0 and k + 1 < len(words) and _IDENTIFIER.match(words[k + 1]):
                            result.setdefault(words[k + 1], word)
                        k += 1
        self._file_scope_cache = result
        return result

    @staticmethod
    def _is_resource_type(type_word):
        return any(str(type_word or "").startswith(prefix) for prefix in _RESOURCE_PREFIXES)

    def struct_fields(self, type_name):
        """[(type, name, semantic)] for ``struct type_name { ... }`` or []."""
        words = self.words
        for i, word in enumerate(words[:-2]):
            if word == "struct" and words[i + 1] == type_name and words[i + 2] == "{" and (i + 2) in self.braces:
                fields, j, close = [], i + 3, self.braces[i + 2]
                while j < close:
                    if _IDENTIFIER.match(words[j]) and words[j] not in _PARAM_MODIFIERS:
                        k = j + 1
                        if k < close and words[k] == "<":
                            while k < close and words[k] != ">":
                                k += 1
                            k += 1
                        if k < close and _IDENTIFIER.match(words[k]):
                            name, semantic, m = words[k], "", k + 1
                            if m < close and words[m] == "[":
                                m = self.brackets.get(m, m) + 1
                            if m < close and words[m] == ":" and m + 1 < close:
                                semantic = words[m + 1]
                            fields.append((words[j], name, semantic))
                            while j < close and words[j] != ";":
                                j += 1
                    j += 1
                return fields
        return []

    def function_params(self, record):
        """[(modifier, type, name, semantic)] parsed from the function signature."""
        words = self.words
        open_paren = record["declaration"] + 1
        close = self.parens.get(open_paren, open_paren)
        params, group, depth = [], [], 0
        for i in range(open_paren + 1, close + 1):
            word = words[i] if i < close else ","
            if word in ("(", "<", "["):
                depth += 1
            elif word in (")", ">", "]"):
                depth -= 1
            if word == "," and depth <= 0:
                if group:
                    modifier = " ".join(w for w in group if w in _PARAM_MODIFIERS)
                    rest = [w for w in group if w not in _PARAM_MODIFIERS]
                    semantic = ""
                    if ":" in rest:
                        colon = rest.index(":")
                        semantic = rest[colon + 1] if colon + 1 < len(rest) else ""
                        rest = rest[:colon]
                    if "=" in rest:
                        rest = rest[:rest.index("=")]
                    if len(rest) >= 2:
                        params.append((modifier, rest[0], rest[-1] if rest[-1] not in (">", "]") else rest[-2], semantic))
                group = []
                continue
            if word not in ("<", ">", ",") or depth > 0:
                if not (word in ("<", ">") ):
                    group.append(word)
        return params

    def span_io(self, start, end, params=None):
        """Reads/writes/resources touched in tokens [start, end).

        Field accesses on params or globals (``data.normal``) are reported as
        ``base.field``; swizzles collapse to the base. Plain identifiers count
        only when they are params, globals or resources, so locals do not flood
        the summary. Assignment-shaped uses are writes, everything else reads.
        """
        words, scope = self.words, self.file_scope()
        params = params or {}
        reads, writes, resources, local_writes = [], [], [], []

        def add(bucket, name):
            if name and name not in bucket and len(bucket) < _IO_LIMIT:
                bucket.append(name)

        def is_write_after(k):
            if k >= len(words):
                return False
            if words[k] in _ASSIGN_ONE or words[k] in ("++", "--"):
                return True
            return words[k] in _ASSIGN_TWO and k + 1 < len(words) and words[k + 1] == "="

        i = start
        while i < end:
            word = words[i]
            if not _IDENTIFIER.match(word) or (i > 0 and words[i - 1] in (".", "::")):
                i += 1
                continue
            interesting = word in params or word in scope
            # follow a member chain
            j, field = i, ""
            while j + 2 < end and words[j + 1] == "." and _IDENTIFIER.match(words[j + 2]):
                if not field and not _SWIZZLE.match(words[j + 2]):
                    field = words[j + 2]
                j += 2
            # skip array subscripts inside the chain end
            k = j + 1
            while k < end and words[k] == "[" and k in self.brackets:
                k = self.brackets[k] + 1
            if interesting:
                name = word + ("." + field if field else "")
                is_write = is_write_after(k) or (i > 0 and words[i - 1] in ("++", "--"))
                if self._is_resource_type(scope.get(word)):
                    add(resources, word)
                elif is_write:
                    add(writes, name)
                else:
                    add(reads, name)
            elif j == i and i > 0 and _IDENTIFIER.match(words[i - 1]) and words[i - 1] not in _CONTROLS and is_write_after(k):
                # local declaration with initializer: "float3 n = ..." -> written local
                add(local_writes, word)
            elif j == i and is_write_after(k) and word not in _CONTROLS:
                add(local_writes, word)
            i = max(k, i + 1)
        return {"reads": reads, "writes": writes, "resources": resources, "locals": local_writes}

    def function_io(self, function_id):
        """Data-flow summary of a whole function: params in/out plus body I/O."""
        record = self.functions.get(function_id)
        if not record:
            return {"reads": [], "writes": [], "resources": []}
        params = {}
        for modifier, type_word, name, unused_semantic in self.function_params(record):
            params[name] = (modifier, type_word)
        io = self.span_io(record["start"], record["end"], params)
        # Reading an out parameter after assigning it inside the function does
        # not make the caller supply an input value for that parameter.
        out_only = [name for name, (modifier, unused_type) in params.items()
                    if "out" in modifier.split() and "inout" not in modifier.split()]
        io["reads"] = [value for value in io["reads"] if not any(
            value == name or value.startswith(name + ".") for name in out_only)]
        # A field never written inside the callee but passed via "out" is still an
        # output from the caller's point of view; keep declared out params visible.
        for name, (modifier, unused_type) in params.items():
            if ({"out", "inout"}.intersection(modifier.split()) and
                    not any(w == name or w.startswith(name + ".") for w in io["writes"])):
                io["writes"].append(name)
        # A return is part of a function's interface even when its expression
        # uses only locals. Display that interface, never the return statement.
        inputs, outputs = self.entry_interface(record)
        for value in inputs:
            if value not in io["reads"]:
                io["reads"].append(value)
        for value in outputs:
            if value not in io["writes"]:
                io["writes"].append(value)
        return io

    def entry_interface(self, record):
        """(inputs, outputs) as display strings for a function's boundary.

        Struct-typed params/returns are expanded to their fields so an entry
        point shows real semantics (SV_Position, TEXCOORD0 ...) instead of a
        wrapper struct name.
        """
        inputs, outputs = [], []

        def describe(type_word, name, semantic):
            fields = self.struct_fields(type_word) if type_word in self.struct_types else []
            if fields:
                return [("%s%s" % (f_name, (" : " + f_sem) if f_sem else "")) for unused_t, f_name, f_sem in fields[:_IO_LIMIT]]
            return ["%s%s" % (name or type_word, (" : " + semantic) if semantic else "")]

        for modifier, type_word, name, semantic in self.function_params(record):
            mods = modifier.split()
            if "out" in mods or "inout" in mods:
                outputs.extend(describe(type_word, name, semantic))
            if "inout" in mods or "out" not in mods:
                inputs.extend(describe(type_word, name, semantic))
        words = self.words
        return_type = words[record["declaration"] - 1] if record["declaration"] > 0 else "void"
        if return_type != "void":
            semantic = ""
            close = self.parens.get(record["declaration"] + 1, record["declaration"] + 1)
            if close + 1 < len(words) and words[close + 1] == ":" and close + 2 < len(words):
                semantic = words[close + 2]
            outputs.extend(describe(return_type, "return", semantic))
        return inputs[:_IO_LIMIT], outputs[:_IO_LIMIT]

    def leading_comment(self, token_index, limit=200):
        """The `//` comment line(s) directly above a token's line, or ''.

        Organized shaders explain each stage/step in a comment right above it;
        that sentence is the best human label for the node. A blank line or any
        code line between comment and token breaks the association.
        """
        if token_index < 0 or token_index >= len(self.tokens):
            return ""
        position = self.tokens[token_index].start()
        line_no = bisect.bisect_right(self.lines, position)  # 1-based
        collected = []
        current = line_no - 1  # index of previous line in 0-based terms == line_no-2
        while current >= 1:
            start = self.lines[current - 1]
            end = self.lines[current] - 1 if current < len(self.lines) else len(self.source)
            text = self.source[start:end].strip()
            if text.startswith("//"):
                collected.append(text[2:].strip())
            elif text.startswith("/*") and text.endswith("*/"):
                collected.append(text[2:-2].strip())
            else:
                break
            current -= 1
        collected.reverse()
        joined = " ".join(item for item in collected if item and
                          not re.match(r"^@(?:stage|phase|helper|overview)\b", item))
        return joined[:limit]

    def set_function_annotations(self, overview):
        """Keep authored descriptions on verified navigation projections.

        A projected location is not a renewed semantic review. Original review
        hashes remain unchanged, and every carried anchor must bind this buffer.
        """
        self.function_annotations = {}
        self.helper_function_ids = set()
        if not isinstance(overview, dict) or (overview.get("sourceSha256") != self.source_sha256
                and overview.get('navigationSourceSha256') != self.source_sha256):
            return
        def canonical(signature):
            return " ".join(match.group() for match in _TOKEN.finditer(signature))

        signatures = {canonical(record["signature"]): function_id
                      for function_id, record in self.functions.items() if record}
        stages = overview.get("stages", [])
        for stage in (stages if isinstance(stages, list) else [])[:MAX_FUNCTIONS]:
            if not isinstance(stage, dict):
                continue
            helpers = stage.get("helperSignatures", [])
            for signature in (helpers if isinstance(helpers, list) else [])[:MAX_FUNCTIONS]:
                if isinstance(signature, str) and canonical(signature) in signatures:
                    self.helper_function_ids.add(signatures[canonical(signature)])
            anchors = stage.get("anchors", [])
            for anchor in (anchors if isinstance(anchors, list) else [])[:MAX_FUNCTIONS]:
                if not isinstance(anchor, dict) or anchor.get("kind") != "function":
                    continue
                if (overview.get('sourceSha256') != self.source_sha256
                        and anchor.get('sourceSha256') != self.source_sha256):
                    continue
                signature = anchor.get("functionSignature")
                function_id = signatures.get(canonical(signature)) if isinstance(signature, str) else None
                if not function_id:
                    continue
                record = self.functions[function_id]
                if anchor.get("functionName") != record["name"]:
                    continue
                summary = _bounded_text(stage.get("description") or stage.get("title"),
                                        MAX_DESCRIPTION_LENGTH, False)
                if summary:
                    self.function_annotations[function_id] = summary

    def function_description(self, function_id):
        record = self.functions.get(function_id)
        return (self.function_annotations.get(function_id) or
                (self.leading_comment(record["declaration"] - 1) if record else ""))

    def function_path(self, entry_id, target_id):
        """A bounded source-proven caller path; visited graphs are not ancestry."""
        if entry_id not in self.functions or target_id not in self.functions:
            return []
        queue = [[entry_id]]
        visited = set()
        queued = {entry_id}
        while queue:
            path = queue.pop(0)
            current = path[-1]
            if current == target_id:
                return path
            if current in visited:
                continue
            visited.add(current)
            record = self.functions.get(current)
            if not record:
                continue
            for token in range(record["start"], record["end"] - 1):
                if (self.words[token + 1] != "(" or
                        self.words[token - 1] in (".", "::", "->")):
                    continue
                child = self.function_identity(self.words[token])
                if child and child not in queued:
                    queued.add(child)
                    queue.append(path + [child])
        return []

    def _source_location(self, start, end):
        token = self.tokens[start]
        final = self.tokens[max(start, end - 1)]
        line = bisect.bisect_right(self.lines, token.start())
        location = dict(self.identity)
        location.update({"line": line, "column": token.start() - self.lines[line - 1] + 1})
        span = {"startLine": line,
                "endLine": bisect.bisect_right(self.lines, final.end() - 1)}
        return location, span

    def function_identity(self, name):
        records = self.by_name.get(name, [])
        return records[0]["id"] if len(records) == 1 else None

    def call_site(self, name):
        """First invocation of an unambiguous function that is not its own
        declaration/definition, as {line, column, ownerFunctionId}, else None.

        A top-level pipeline stage should navigate to where it is *called* in
        the entry body (its use), not into its definition. A return-type-
        preceded occurrence (``void Foo(`` / ``float3 Foo(``) is a declaration
        or definition and is skipped; the first statement-position ``Foo(`` is
        the call.
        """
        if len(self.by_name.get(name, [])) != 1:
            return None
        for index, word in enumerate(self.words):
            if word != name or index + 1 >= len(self.words) or self.words[index + 1] != "(":
                continue
            previous = self.words[index - 1] if index > 0 else ""
            if _IDENTIFIER.match(previous) or previous in (">", "]", "*", "&"):
                continue
            token = self.tokens[index]
            line = bisect.bisect_right(self.lines, token.start())
            column = token.start() - self.lines[line - 1] + 1
            owner = None
            for function_id, record in self.functions.items():
                if record is not None and record["start"] <= index < record["end"]:
                    owner = function_id
                    break
            return {"line": line, "column": column, "ownerFunctionId": owner}
        return None

    def line_calls(self, line, name):
        """True if the current buffer's 1-based ``line`` still invokes ``name``.

        Safety check for a call-site anchor: if the call line was deleted or no
        longer references the stage, navigation reports the target unavailable
        instead of jumping to unrelated neighbouring code.
        """
        if not isinstance(line, int) or line < 1 or line > len(self.lines):
            return False
        start = self.lines[line - 1]
        end = self.lines[line] if line < len(self.lines) else len(self.source)
        segment = self.source[start:end]
        return bool(re.search(r"\b" + re.escape(name) + r"\s*\(", segment))

    def project_root(self, accepted_graph, functions_only=False):
        """Keep accepted top-level stages; add only source-proven expansion IDs."""
        result = copy.deepcopy(accepted_graph if isinstance(accepted_graph, dict) else {})
        graph = result.get("flowGraph", result)
        if functions_only:
            entry_id = self.function_identity(graph.get("entryPoint", ""))
            if entry_id:
                projected = self.function_graph(
                    entry_id, functions_only=True, expandable_self=True)
                # The accepted module descriptions are reviewed AI evidence.
                # The function-only projection supplies source-derived IO and
                # call relationships; merge the reviewed annotations by exact
                # function name without exposing source snippets/expressions.
                descriptions = {}
                for source_node in graph.get("nodes", [])[:MAX_GRAPH_NODES]:
                    if not isinstance(source_node, dict):
                        continue
                    name = _bounded_text(source_node.get("name"), MAX_LABEL_LENGTH, True)
                    description = _bounded_text(
                        source_node.get("description"), MAX_DESCRIPTION_LENGTH, False)
                    # i18n-ignore: exact legacy Host-generated prose compatibility value.
                    legacy_entry_description = "Shader entry point; the stage result is produced here."
                    if (name and description and name not in descriptions and
                            (description == legacy_entry_description or
                             not any(marker in description for marker in (";", "{", "}", "```")))):
                        descriptions[name] = description
                for node in projected.get("nodes", []):
                    if not isinstance(node, dict) or node.get("kind") in ("input", "output"):
                        continue
                    description = descriptions.get(str(node.get("name") or ""))
                    if description and not node.get("description"):
                        node["description"] = description
                    source = node.get("source")
                    if isinstance(source, dict):
                        # Navigation keeps identity/line metadata only. Actual
                        # implementation belongs in the editor, never the box.
                        source.pop("snippet", None)
                projected["functionName"] = ""
                projected["graphId"] = "root:" + projected["graphId"]
                if "flowGraph" in result:
                    result["flowGraph"] = projected
                    return result
                return projected
        if graph.get("graphKind") != "algorithm-dataflow":
            entry_id = self.function_identity(graph.get("entryPoint", ""))
            if entry_id:
                # Legacy artifacts enumerate definitions. Project the actual
                # entry computation instead; do not rewrite the old artifact.
                projected = self.function_graph(entry_id)
                projected["functionName"] = ""
                projected["graphId"] = "root:" + projected["graphId"]
                if "flowGraph" in result:
                    result["flowGraph"] = projected
                    return result
                return projected
        for node in graph.get("nodes", [])[:256]:
            if not isinstance(node, dict):
                continue
            node.pop("functionId", None)
            function_id = self.function_identity(node.get("name", ""))
            if function_id:
                node["functionId"] = function_id
                record = self.functions[function_id]
                node["io"] = self.function_io(function_id)
                # The box is an interface summary. Source expressions remain
                # navigation evidence, never visible node titles/descriptions.
                call = self.call_site(record["name"])
                comment = ""
                if call is not None and 1 <= int(call.get("line", 0)) <= len(self.lines):
                    line_no = int(call["line"])
                    start = self.lines[line_no - 1]
                    token_at_line = bisect.bisect_left([t.start() for t in self.tokens], start)
                    comment = self.leading_comment(token_at_line)
                if not comment:
                    comment = self.leading_comment(record["declaration"] - 1)
                explanation = str(node.get("description", "") or node.get("label", "") or "")
                node["label"] = record["name"][:160]
                node["description"] = (comment or ("" if functions_only else explanation))[:640]
                location = node.setdefault("source", {})
                if not isinstance(location, dict):
                    continue
                location["symbol"] = record["name"]
                location["functionSignature"] = record["signature"][:1024]
                binding = graph.get("binding", {})
                graph_sha = graph.get("sourceSha256", binding.get("sourceSha256"))
                # Old line numbers cannot relocate a stage after editing.
                # Existing native anchors are resolved by the owning editor.
                if graph_sha and graph_sha != self.source_sha256:
                    continue
                start_line = location.get("line", 0)
                end_line = node.get("sourceRange", {}).get("endLine", start_line)
                if not isinstance(start_line, int) or not isinstance(end_line, int):
                    continue
                selected = [i for i in range(record["start"], record["end"])
                            if start_line <= bisect.bisect_right(self.lines, self.tokens[i].start()) <= end_line]
                if selected:
                    text = self.source[self.tokens[selected[0]].start():self.tokens[selected[-1]].end()]
                    location["snippet"] = text[:640]
                    location["statementId"] = hashlib.sha256(
                        " ".join(self.words[selected[0]:selected[-1] + 1]).encode("utf-8")).hexdigest()[:16]
        self._wrap_accepted_graph(graph)
        return result

    def _wrap_accepted_graph(self, graph):
        """Add Input/Output boundary nodes around an accepted (AI) stage graph."""
        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes:
            return
        if any(isinstance(n, dict) and n.get("kind") in ("input", "output") for n in nodes):
            return
        entry_id = self.function_identity(graph.get("entryPoint", ""))
        record = self.functions.get(entry_id) if entry_id else None
        if record is None:
            return
        inputs, outputs = self.entry_interface(record)
        whole = self.function_io(record["id"])
        if not inputs:
            inputs = list(whole.get("reads", [])) + list(whole.get("resources", []))
        if not outputs:
            outputs = list(whole.get("writes", []))
        in_location, in_span = self._source_location(record["declaration"], record["declaration"] + 1)
        out_location, out_span = self._source_location(record["end"], record["end"] + 1)
        for location in (in_location, out_location):
            location.update({"symbol": record["name"], "functionSignature": record["signature"][:1024]})
        graph_id = str(graph.get("graphId", "root"))
        input_node = {"id": graph_id + ":input", "name": record["name"],
                      "label": "Input \u00b7 " + record["name"], "description": "",
                      "kind": "input", "source": in_location, "sourceRange": in_span,
                      "io": {"reads": list(inputs)[:_IO_LIMIT], "writes": [], "resources": [], "locals": []}}
        output_node = {"id": graph_id + ":output", "name": record["name"],
                       "label": "Output", "description": "",
                       "kind": "output", "source": out_location, "sourceRange": out_span,
                       "io": {"reads": [], "writes": list(outputs)[:_IO_LIMIT], "resources": [], "locals": []}}
        ids = [n.get("id") for n in nodes if isinstance(n, dict)]
        incoming = set(e.get("target") for e in edges if isinstance(e, dict))
        outgoing = set(e.get("source") for e in edges if isinstance(e, dict))
        heads = [i for i in ids if i not in incoming]
        tails = [i for i in ids if i not in outgoing]
        new_edges = [{"source": input_node["id"], "target": h, "kind": "sequence"} for h in heads]
        new_edges += [{"source": t, "target": output_node["id"], "kind": "sequence"} for t in tails]
        nodes.insert(0, input_node)
        nodes.append(output_node)
        edges.extend(new_edges)

    def function_graph(self, function_id, functions_only=False, expandable_self=False,
                       helper_group_id=None):
        record = self.functions.get(function_id)
        if not record:
            raise ValueError("Shader function is missing or ambiguous in the current buffer")
        if functions_only:
            graph = _FunctionSummaryProjection(self, record, expandable_self).build()
            return self._helper_projection(graph, helper_group_id)
        return _FunctionProjection(self, record).build()

    def _helper_projection(self, graph, helper_group_id=None):
        """Fold only explicitly classified helpers along an unbranched sequence.

        This is presentation grouping, never a new rendering-stage inference.
        Expanding a group restores its exact source-proven calls and edges.
        """
        nodes, edges = graph["nodes"], graph["edges"]
        incoming, outgoing = {}, {}
        for edge in edges:
            incoming.setdefault(edge["target"], []).append(edge)
            outgoing.setdefault(edge["source"], []).append(edge)
        runs, run = [], []
        for node in nodes:
            eligible = (node.get("kind") == "function-call" and
                        node.get("functionId") in self.helper_function_ids)
            linear = (run and incoming.get(node["id"]) == [{"source": run[-1]["id"],
                      "target": node["id"], "kind": "sequence"}] and
                      len(outgoing.get(run[-1]["id"], [])) == 1)
            if run and (not eligible or not linear):
                if len(run) > 1:
                    runs.append(run)
                run = []
            if eligible:
                run.append(node)
        if len(run) > 1:
            runs.append(run)
        replacements, groups = {}, {}
        for members in runs:
            digest = hashlib.sha256("|".join(n["id"] for n in members).encode("utf-8")).hexdigest()[:24]
            group_id = "helpers:" + digest
            group = copy.deepcopy(members[0])
            group.update({"id": group_id, "kind": "function-group", "name": "",
                          "label": "", "description": "", "summaryId": "implementation-helpers",
                          "functionId": graph["functionId"], "helperGroupId": group_id,
                          "helperCount": len(members)})
            group["sourceRange"]["endLine"] = members[-1]["sourceRange"]["endLine"]
            groups[group_id] = (group, members)
            for member in members:
                replacements[member["id"]] = group_id
        if helper_group_id:
            if helper_group_id not in groups:
                raise ValueError("Linked helper group was changed or removed")
            group, members = groups[helper_group_id]
            ids = set(n["id"] for n in members)
            graph["nodes"] = [copy.deepcopy(nodes[0])] + members
            graph["edges"] = [{"source": nodes[0]["id"], "target": members[0]["id"], "kind": "sequence"}]
            graph["edges"].extend(edge for edge in edges if edge["source"] in ids and edge["target"] in ids)
            graph["graphId"] += ":" + helper_group_id
            graph["helperGroupId"] = helper_group_id
            return graph
        if not replacements:
            return graph
        result_nodes, emitted = [], set()
        for node in nodes:
            group_id = replacements.get(node["id"])
            if not group_id:
                result_nodes.append(node)
            elif group_id not in emitted:
                emitted.add(group_id)
                result_nodes.append(groups[group_id][0])
        result_edges, seen = [], set()
        for edge in edges:
            source = replacements.get(edge["source"], edge["source"])
            target = replacements.get(edge["target"], edge["target"])
            key = (source, target, edge["kind"])
            if source != target and key not in seen:
                seen.add(key)
                result_edges.append({"source": source, "target": target, "kind": edge["kind"]})
        graph["nodes"], graph["edges"] = result_nodes, result_edges
        return graph


class _FunctionProjection(object):
    def __init__(self, index, record):
        self.index, self.record = index, record
        self.nodes, self.edges = [], []
        self.truncated = False
        self._occurrences = {}
        self._loops = []
        self.params = {}
        for modifier, type_word, name, unused_semantic in index.function_params(record):
            self.params[name] = (modifier, type_word)
        # Local values connect neighboring statement boxes. Include only
        # source-proven declarations; do not promote arbitrary identifiers,
        # called functions or constructor/type names into data inputs.
        for token in range(record["start"], record["end"] - 2):
            type_word, name, following = index.words[token:token + 3]
            is_type = (type_word in index.struct_types or re.match(
                r"^(?:bool|int|uint|half|float|double|dword|min16float|min16int|min16uint)(?:[1-4](?:x[1-4])?)?$",
                type_word))
            if is_type and _IDENTIFIER.match(name) and following in ("=", ";", ",", "["):
                self.params.setdefault(name, ("local", type_word))

    def _node(self, start, end, kind="computation"):
        if start >= end:
            return None
        if len(self.nodes) >= MAX_LOCAL_NODES:
            self.truncated = True
            return None
        index = self.index
        words = index.words[start:end]
        material = " ".join(words)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        occurrence = self._occurrences.get(digest, 0)
        self._occurrences[digest] = occurrence + 1
        node_id = self.record["id"] + ":" + digest + ":" + str(occurrence)
        location, span = index._source_location(start, end)
        excerpt = index.source[index.tokens[start].start():index.tokens[end - 1].end()]
        location.update({"symbol": self.record["name"], "statementId": digest,
                         "functionSignature": self.record["signature"][:1024],
                         "snippet": excerpt[:640]})
        comment = index.leading_comment(start)
        node = {"id": node_id, "name": self.record["name"],
                "label": self.record["name"][:160],
                "description": comment[:640],
                "kind": kind, "source": location, "sourceRange": span}
        callees = []
        for token_index in range(start, end - 1):
            if (index.words[token_index + 1] != "(" or
                    (token_index > start and index.words[token_index - 1] in (".", "::", "->"))):
                continue
            target = index.function_identity(index.words[token_index])
            if target and target not in callees:
                callees.append(target)
        if len(callees) == 1 and kind in ("computation", "return"):
            node["functionId"] = callees[0]
            node["kind"] = "function-call"
            callee = index.functions.get(callees[0]) or {}
            # Call nodes name the callee and show its explanatory comment.
            explanation = comment or (index.leading_comment(callee.get("declaration", -1) - 1)
                                      if callee else "")
            node["label"] = (callee.get("name") or node["label"])[:160]
            node["description"] = explanation[:640]
            node["io"] = index.function_io(callees[0])
        else:
            node["io"] = index.span_io(start, end, self.params)
            if kind == "return" and words != ["return", ";"]:
                node["io"]["writes"].append("return")
        self.nodes.append(node)
        return node_id

    def _connect(self, frontier, node_id):
        if node_id:
            for previous, kind in frontier:
                self.edges.append({"source": previous, "target": node_id, "kind": kind})
            return [(node_id, "sequence")]
        return frontier

    def _simple_end(self, start, end):
        words = self.index.words
        cursor = start
        while cursor < end:
            if cursor in self.index.parens:
                cursor = self.index.parens[cursor] + 1
            elif cursor in self.index.braces:
                cursor = self.index.braces[cursor] + 1
            elif words[cursor] == ";":
                return cursor + 1
            else:
                cursor += 1
        return end

    def _has_call(self, start, end):
        words = self.index.words
        return any(words[i + 1] == "(" and self.index.function_identity(words[i])
                   and (i == start or words[i - 1] not in (".", "::", "->"))
                   for i in range(start, end - 1))

    def _skip_attributes(self, start, end):
        while start < end and start in self.index.brackets:
            start = self.index.brackets[start] + 1
        return start

    def _is_declaration(self, start, end):
        words = self.index.words[start:end]
        while words and words[0] in ("const", "static", "volatile", "precise"):
            words = words[1:]
        if len(words) < 3 or "=" in words or "(" in words:
            return False
        is_type = (words[0] in self.index.struct_types or
                   re.match(r"^(?:bool|int|uint|half|float|double|dword|min16float|min16int|min16uint)(?:[1-4](?:x[1-4])?)?$", words[0]))
        return bool(is_type and _IDENTIFIER.match(words[1]) and words[-1] == ";")

    def _body(self, start, end, frontier, depth):
        start = self._skip_attributes(start, end)
        if start < end and self.index.words[start] == "{":
            close = self.index.braces[start]
            return self._sequence(start + 1, close, frontier, depth), close + 1
        return self._one(start, end, frontier, depth)

    def _one(self, start, end, frontier, depth):
        start = self._skip_attributes(start, end)
        if start >= end:
            return frontier, end
        if depth >= MAX_CONTROL_DEPTH or len(self.nodes) >= MAX_LOCAL_NODES:
            self.truncated = True
            return frontier, end
        words = self.index.words
        word = words[start]
        if word in ("if", "while", "for") and start + 1 in self.index.parens:
            close = self.index.parens[start + 1]
            node_id = self._node(start, close + 1, "condition")
            self._connect(frontier, node_id)
            if word != "if":
                self._loops.append({"break": [], "continue": []})
            body, following = self._body(close + 1, end, [(node_id, "true")], depth + 1)
            if word == "if":
                if following < end and words[following] == "else":
                    other, following = self._body(following + 1, end, [(node_id, "false")], depth + 1)
                else:
                    other = [(node_id, "false")]
                return body + other, following
            exits = self._loops.pop()
            self._connect([(previous, "loop") for previous, unused_kind in body + exits["continue"]], node_id)
            return [(node_id, "false")] + exits["break"], following
        # Switch, do/while, and early loop exits remain honest source-bound
        # blocks instead of inventing a linear order between exclusive cases.
        if word == "switch" and start + 1 in self.index.parens:
            close = self.index.parens[start + 1]
            stop = self.index.braces.get(close + 1, close) + 1
            return self._connect(frontier, self._node(start, stop, "selection")), stop
        if word == "do" and start + 1 in self.index.braces:
            stop = self._simple_end(self.index.braces[start + 1] + 1, end)
            return self._connect(frontier, self._node(start, stop, "loop")), stop
        stop = self._simple_end(start, end)
        node_id = self._node(start, stop, "return" if word in ("return", "discard") else "computation")
        connected = self._connect(frontier, node_id)
        if word in ("break", "continue") and self._loops:
            self._loops[-1][word].extend(connected)
            return [], stop
        return ([] if word in ("return", "discard") else connected), stop

    def _sequence(self, start, end, frontier, depth):
        if depth >= MAX_CONTROL_DEPTH:
            self.truncated = True
            return frontier
        cursor = start
        while cursor < end and len(self.nodes) < MAX_LOCAL_NODES:
            if getattr(self, "_stop_after_exit", False) and not frontier:
                # All paths in this block terminated; its remaining statements
                # are unreachable, not detached stages of the execution graph.
                return []
            cursor = self._skip_attributes(cursor, end)
            if cursor >= end:
                break
            if self.index.words[cursor] == ";":
                cursor += 1
                continue
            if self.index.words[cursor] == "{":
                frontier, cursor = self._body(cursor, end, frontier, depth + 1)
                continue
            word = self.index.words[cursor]
            if word in _CONTROLS or word in ("do", "return", "discard", "break", "continue"):
                frontier, cursor = self._one(cursor, end, frontier, depth)
                continue
            stop = self._simple_end(cursor, end)
            if self._is_declaration(cursor, stop):
                cursor = stop
                continue
            if self._has_call(cursor, stop):
                frontier, cursor = self._one(cursor, end, frontier, depth)
                continue
            # Adjacent plain calculations are one useful block, rather than
            # a separate node for every temporary assignment.
            statements = 1
            while stop < end and statements < 8:
                next_word = self.index.words[stop]
                if next_word in _CONTROLS or next_word in ("[", "{", "do", "return", "discard", "break", "continue"):
                    break
                next_stop = self._simple_end(stop, end)
                if self._has_call(stop, next_stop):
                    break
                stop, statements = next_stop, statements + 1
            frontier = self._connect(frontier, self._node(cursor, stop))
            cursor = stop
        if cursor < end:
            self.truncated = True
        return frontier

    def _boundary_nodes(self):
        """Add an Input node before the first statements and an Output node after the last.

        The graph then reads main -> stages -> output. The entry point shows its
        real interface (semantics such as SV_Position / SV_Target0) because
        struct-typed params and returns are expanded to their fields.
        """
        index, record = self.index, self.record
        inputs, outputs = index.entry_interface(record)
        whole = index.function_io(record["id"])
        if not inputs:
            # A helper without params consumes globals/resources instead.
            inputs = list(whole.get("reads", [])) + list(whole.get("resources", []))
        if not outputs:
            # void helpers without out params: what they leave behind for the
            # rest of the shader is the set of globals/fields they write.
            outputs = list(whole.get("writes", []))
        # Anchor Input at the declaration and Output at the closing brace so the
        # jump arrow still lands somewhere meaningful for both.
        in_location, in_span = index._source_location(record["declaration"], record["declaration"] + 1)
        out_location, out_span = index._source_location(record["end"], record["end"] + 1)
        for location in (in_location, out_location):
            location.update({"symbol": record["name"],
                             "functionSignature": record["signature"][:1024]})
        input_node = {"id": record["id"] + ":input", "name": record["name"],
                      "label": "Input \u00b7 " + record["name"],
                      "description": "", "kind": "input",
                      "source": in_location, "sourceRange": in_span,
                      "io": {"reads": list(inputs)[:_IO_LIMIT], "writes": [], "resources": [], "locals": []}}
        output_node = {"id": record["id"] + ":output", "name": record["name"],
                       "label": "Output", "description": "", "kind": "output",
                       "source": out_location, "sourceRange": out_span,
                       "io": {"reads": [], "writes": list(outputs)[:_IO_LIMIT], "resources": [], "locals": []}}
        incoming = set(edge["target"] for edge in self.edges)
        outgoing = set(edge["source"] for edge in self.edges)
        heads = [node["id"] for node in self.nodes if node["id"] not in incoming]
        tails = [node["id"] for node in self.nodes
                 if node["id"] not in outgoing or node.get("kind") == "return"]
        edges = [{"source": input_node["id"], "target": head, "kind": "sequence"} for head in heads]
        edges += [{"source": tail, "target": output_node["id"], "kind": "sequence"} for tail in tails]
        if not self.nodes:
            edges = [{"source": input_node["id"], "target": output_node["id"], "kind": "sequence"}]
        self.nodes = [input_node] + self.nodes + [output_node]
        self.edges = self.edges + edges

    def build(self):
        self._sequence(self.record["start"], self.record["end"], [], 0)
        self._boundary_nodes()
        return {"schema": "replaylab-shader-flow-graph/v1", "schemaVersion": 1,
                "graphId": self.record["id"] + ":" + self.index.source_sha256,
                "graphKind": "algorithm-dataflow", "layoutDirection": "TD",
                "projectionKind": "current-source-function", "functionId": self.record["id"],
                "functionName": self.record["name"], "sourceSha256": self.index.source_sha256,
                "binding": dict(self.index.identity), "generated": False,
                "nodes": self.nodes, "edges": self.edges,
                "truncated": {"nodes": self.truncated, "edges": False,
                              "omittedNodes": 0, "omittedEdges": 0}}


class _FunctionSummaryProjection(_FunctionProjection):
    """Compact execution flow: calls in statement order, with real control paths.

    Plain assignments stay in the editor. Calls within one expression stay in
    one source-bound group: lexical token order cannot prove evaluation order.
    Repeated calls in separate statements remain separate execution steps.
    """

    def __init__(self, index, record, expandable_self=False):
        super(_FunctionSummaryProjection, self).__init__(index, record)
        self.expandable_self = bool(expandable_self)
        self._stop_after_exit = True

    def _node(self, start, end, kind="computation"):
        index = self.index
        targets = []
        for token in range(start, end - 1):
            if (index.words[token + 1] == "(" and
                    (token == start or index.words[token - 1] not in (".", "::", "->"))):
                target = index.function_identity(index.words[token])
                if target and target not in targets:
                    targets.append(target)
        control = kind in ("condition", "selection", "loop", "return")
        # Do not clutter the overview with scalar assignments or ordinary
        # terminal returns. Keep control exits to avoid connecting dead paths.
        if not targets and not control and index.words[start] not in ("break", "continue"):
            return None
        if not targets and kind == "return":
            # Keep an exit only when it is inside a conditional/loop. A final
            # return contributes no additional functional module to the view.
            if self._control_depth == 0:
                return None
        node_id = super(_FunctionSummaryProjection, self)._node(start, end, kind)
        if not node_id:
            return None
        node = self.nodes[-1]
        node["source"].pop("snippet", None)
        node.pop("functionId", None)
        conditional_expression = any(word in ("?", "&&", "||") for word in index.words[start:end])
        if control and kind != "return" or conditional_expression or index.words[start] in ("break", "continue"):
            node["kind"] = "flow-control"
            node["controlKind"] = ("conditional-expression" if conditional_expression else
                                   index.words[start])
            node["label"] = " / ".join(index.functions[t]["name"] for t in targets)
        elif len(targets) == 1:
            target = targets[0]
            callee = index.functions[target]
            node.update({"kind": "function-call", "name": callee["name"],
                         "label": callee["name"], "signature": callee["signature"][:1024],
                         "description": (index.function_description(target) or
                                         index.leading_comment(start)),
                         "io": index.function_io(target)})
            if target != self.record["id"]:
                node["functionId"] = target
        elif targets:
            node["kind"] = "function-group"
            node["label"] = " / ".join(index.functions[t]["name"] for t in targets)
        else:
            node["kind"] = "flow-control"
            node["controlKind"] = index.words[start]
            node["label"] = ""
        return node_id

    def _body(self, start, end, frontier, depth):
        self._control_depth += 1
        try:
            return super(_FunctionSummaryProjection, self)._body(start, end, frontier, depth)
        finally:
            self._control_depth -= 1

    def build(self):
        index, record = self.index, self.record
        location, span = index._source_location(
            record["declaration"], record["declaration"] + 1)
        location.update({"symbol": record["name"],
                         "functionSignature": record["signature"][:1024]})
        inputs, outputs = index.entry_interface(record)
        caller = {"id": record["id"] + ":summary", "name": record["name"],
                  "label": record["name"], "kind": "function-summary",
                  "signature": record["signature"][:1024],
                  "description": index.function_description(record["id"]),
                  "source": location, "sourceRange": span,
                  "io": {"reads": inputs, "writes": outputs,
                         "resources": [], "locals": []}}
        if self.expandable_self:
            caller["functionId"] = record["id"]
        self.nodes.append(caller)
        self._control_depth = 0
        self._sequence(record["start"], record["end"], [(caller["id"], "sequence")], 0)
        return {"schema": "replaylab-shader-flow-graph/v1", "schemaVersion": 1,
                "graphId": "execution:" + record["id"] + ":" + index.source_sha256,
                "graphKind": "function-execution", "layoutDirection": "TD",
                "projectionKind": "current-source-function", "functionId": record["id"],
                "functionName": record["name"], "sourceSha256": index.source_sha256,
                "binding": dict(index.identity), "generated": False,
                "nodes": self.nodes, "edges": self.edges,
                "truncated": {"nodes": self.truncated, "edges": False,
                              "omittedNodes": 0, "omittedEdges": 0}}


__all__ = ["SourceFlowHierarchy"]
