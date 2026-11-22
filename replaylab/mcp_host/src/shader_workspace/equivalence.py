"""Name-agnostic equivalence, Host-applied renames, and factual verdicts.

The organization review used to accept a proposal only when the compiled
executable block hashed identically to the baseline.  DXIL keeps resource,
cbuffer-field and entry names in its metadata, so exactly the renames that
make a decompiled shader readable were rejected.  This module adds:

* ``apply_renames`` / ``insert_function_comments`` – the Host edits the HLSL
  text itself from a rename map and a comment map, at the token level, so a
  rename can never touch arithmetic or control flow.
* ``rename_only_equivalent`` – proves that a proposal differs from the
  baseline by identifiers, comments and whitespace only (the *rename-only*
  equivalence level), with a located diff when it does not.
* ``build_verdict`` – report accepted/level/reasons without deciding the
  agent's next action or imposing a review budget.
* ``mermaid_from_flow_graph`` – a fixed-style Mermaid rendering of the
  function-call graph so diagrams look the same whichever agent produced them.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..review_contract import REVIEW_VERDICT_SCHEMA, reason

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(
    r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:\\.|[^"\\\n])*")
  | (?P<pp>^[ \t]*\#[^\n]*)
  | (?P<number>(?:0[xX][0-9a-fA-F]+[uUlL]*|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFhHuUlL]*))
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct><<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|->|::|[-+*/%<>=!&|^~?:;,.(){}\[\]])
  | (?P<ws>\s+)
  | (?P<other>.)
    """,
    re.DOTALL | re.VERBOSE | re.MULTILINE,
)

# Identifiers the model may never rename: keywords, storage classes, object
# types, intrinsics/methods, semantics, register syntax and swizzles.
_KEYWORDS = frozenset(
    """
    if else for while do switch case default break continue return discard struct cbuffer tbuffer
    typedef register packoffset static const uniform in out inout precise groupshared row_major
    column_major nointerpolation linear centroid noperspective sample snorm unorm globallycoherent
    extern shared volatile void true false export interface class namespace template this
    unroll loop branch flatten numthreads earlydepthstencil maxvertexcount domain partitioning
    outputtopology patchconstantfunc outputcontrolpoints instance forcecase call allow_uav_condition
    fastopt point line triangle lineadj triangleadj vertices indices payload raypayload
    RWTexture1D RWTexture2D RWTexture3D RWTexture1DArray RWTexture2DArray RWBuffer RWByteAddressBuffer
    RWStructuredBuffer Texture1D Texture2D Texture3D TextureCube Texture1DArray Texture2DArray
    TextureCubeArray Texture2DMS Texture2DMSArray Buffer ByteAddressBuffer StructuredBuffer
    AppendStructuredBuffer ConsumeStructuredBuffer SamplerState SamplerComparisonState sampler
    ConstantBuffer TextureBuffer RaytracingAccelerationStructure InputPatch OutputPatch PointStream
    LineStream TriangleStream FeedbackTexture2D FeedbackTexture2DArray RasterizerOrderedTexture2D
    Sample SampleLevel SampleGrad SampleBias SampleCmp SampleCmpLevelZero SampleCmpLevel Load Load2
    Load3 Load4 Store Store2 Store3 Store4 Gather GatherRed GatherGreen GatherBlue GatherAlpha
    GatherCmp GatherCmpRed GetDimensions CalculateLevelOfDetail CalculateLevelOfDetailUnclamped
    Append Consume IncrementCounter DecrementCounter GetSamplePosition mips Operator
    abs acos all any asdouble asfloat asint asuint atan atan2 ceil clamp clip cos cosh countbits
    cross ddx ddx_coarse ddx_fine ddy ddy_coarse ddy_fine degrees determinant distance dot dst
    exp exp2 f16tof32 f32tof16 faceforward firstbithigh firstbitlow floor fma fmod frac frexp fwidth
    isfinite isinf isnan ldexp length lerp lit log log10 log2 mad max min modf msad4 mul noise
    normalize pow radians rcp reflect refract reversebits round rsqrt saturate sign sin sincos sinh
    smoothstep sqrt step tan tanh tex1D tex2D tex3D texCUBE tex2Dlod tex2Dproj tex2Dbias tex2Dgrad
    transpose trunc InterlockedAdd InterlockedAnd InterlockedCompareExchange InterlockedCompareStore
    InterlockedExchange InterlockedMax InterlockedMin InterlockedOr InterlockedXor
    GroupMemoryBarrier GroupMemoryBarrierWithGroupSync DeviceMemoryBarrier DeviceMemoryBarrierWithGroupSync
    AllMemoryBarrier AllMemoryBarrierWithGroupSync WaveIsFirstLane WaveGetLaneIndex WaveGetLaneCount
    WaveActiveAnyTrue WaveActiveAllTrue WaveActiveBallot WaveReadLaneAt WaveReadLaneFirst
    WaveActiveAllEqual WaveActiveCountBits WaveActiveSum WaveActiveProduct WaveActiveBitAnd
    WaveActiveBitOr WaveActiveBitXor WaveActiveMin WaveActiveMax WavePrefixCountBits WavePrefixSum
    WavePrefixProduct QuadReadAcrossX QuadReadAcrossY QuadReadAcrossDiagonal QuadReadLaneAt
    EvaluateAttributeAtSample EvaluateAttributeCentroid EvaluateAttributeSnapped
    GetRenderTargetSampleCount GetRenderTargetSamplePosition CheckAccessFullyMapped
    NonUniformResourceIndex TraceRay ReportHit IgnoreHit AcceptHitAndEndSearch CallShader
    DispatchMesh SetMeshOutputCounts printf errorf abort
    """.split()
)
_TYPE = re.compile(
    r"^(?:float|int|uint|bool|half|double|min16float|min10float|min16int|min12int|min16uint|"
    r"int16_t|uint16_t|int32_t|uint32_t|int64_t|uint64_t|float16_t|float32_t|float64_t|dword|"
    r"vector|matrix)(?:[1-4](?:x[1-4])?)?$"
)
_SEMANTIC = re.compile(
    r"^(?:SV_\w+|TEXCOORD\d*|POSITION\d*|POSITIONT|NORMAL\d*|COLOR\d*|TANGENT\d*|BINORMAL\d*|"
    r"BLENDINDICES\d*|BLENDWEIGHT\d*|PSIZE\d*|FOG|DEPTH|VFACE|VPOS|TESSFACTOR\d*)$"
)
_REGISTER = re.compile(r"^(?:[tsbuc]\d+|space\d+)$")
_SWIZZLE = re.compile(r"^(?:[xyzw]{1,4}|[rgba]{1,4})$")


class EquivalenceError(ValueError):
    """A rename request that cannot be applied safely."""


# ------------------------------------------------------------------ tokens
def tokenize(source: str) -> list[dict[str, Any]]:
    """Tokens with kind/text/line; comments and whitespace are kept for editing."""

    tokens: list[dict[str, Any]] = []
    line = 1
    for match in _TOKEN.finditer(source):
        kind = match.lastgroup or "other"
        text = match.group(0)
        tokens.append({"kind": kind, "text": text, "line": line, "start": match.start(), "end": match.end()})
        line += text.count("\n")
    return tokens


def code_tokens(source: str) -> list[dict[str, Any]]:
    """Tokens that matter for code identity (no comments, no whitespace)."""

    return [t for t in tokenize(source) if t["kind"] not in {"comment", "ws"}]


def is_reserved(identifier: str, *, as_target: bool = False) -> bool:
    """Keywords, types, intrinsics and semantics are never renamed.

    Register-looking names (``t0``, ``s1``, ``b2``, ``space0``) are legal
    variable names — decompilers use them for resources — so they may be
    renamed *from*; inside ``register(...)``/``packoffset(...)`` they are left
    alone by ``apply_renames``.  They are refused as *new* names.
    """

    return (
        identifier in _KEYWORDS
        or _TYPE.match(identifier) is not None
        or _SEMANTIC.match(identifier) is not None
        or (as_target and _REGISTER.match(identifier) is not None)
    )


def _is_swizzle_access(tokens: Sequence[dict[str, Any]], index: int) -> bool:
    """``.xy`` / ``.rgb`` after a dot: component selection, never an identifier.

    Renaming one changes which components the expression reads, which is a change
    in behaviour that still compiles, still has an identical ABI, and -- because the
    same map is applied to the baseline when the two are compared -- still proves
    "rename-only equivalent". ``uv.xy.x`` renamed with ``{"xy": "xz"}`` samples a
    different channel and every gate accepts it.

    A struct field named exactly ``xy`` is indistinguishable from a swizzle here, so
    it cannot be renamed either. That is the correct trade: refusing a rare rename
    costs a name, accepting this one costs the equivalence proof.
    """

    return _is_member_access(tokens, index) and _SWIZZLE.match(tokens[index]["text"]) is not None


def _is_member_access(tokens: Sequence[dict[str, Any]], index: int) -> bool:
    previous = index - 1
    while previous >= 0 and tokens[previous]["kind"] in {"ws", "comment"}:
        previous -= 1
    return previous >= 0 and tokens[previous]["text"] in {".", "->"}


_FIELD_SELECTOR = re.compile(r"^(?P<owner>[A-Za-z_]\w*)\.(?P<member>[A-Za-z_]\w*)$")
_LOCAL_SELECTOR = re.compile(r"^(?P<function>[A-Za-z_]\w*)::(?P<local>[A-Za-z_]\w*)$")
_DECL_SELECTOR = re.compile(
    r"^(?P<kind>cbuffer|resource):(?P<name>[A-Za-z_]\w*)$"
)
_DECL_QUALIFIERS = frozenset(
    "const static precise volatile uniform shared groupshared extern row_major column_major "
    "snorm unorm in out inout nointerpolation linear centroid noperspective sample "
    "globallycoherent readonly writeonly coherent restrict highp mediump lowp flat smooth "
    "patch invariant layout struct".split()
)
_GLSL_INTERFACE_STORAGE = frozenset({"uniform", "buffer", "in", "out"})
_CONTROL_WORDS = frozenset({"if", "else", "for", "while", "do", "switch", "case", "default", "return"})


def _rename_owner_aliases(renames: Mapping[str, Any]) -> dict[str, str]:
    """Map a current owner spelling back to its sealed baseline spelling."""

    aliases: dict[str, str] = {}
    for selector, target in renames.items():
        target_name = str(target).strip()
        if _IDENTIFIER.fullmatch(target_name) is None:
            continue
        selector_name = str(selector).strip()
        declaration = _DECL_SELECTOR.fullmatch(selector_name)
        if declaration and declaration.group("kind") == "cbuffer":
            aliases[target_name] = declaration.group("name")
        elif _IDENTIFIER.fullmatch(selector_name):
            aliases[target_name] = selector_name
    return aliases


@dataclass
class _BoundDeclaration:
    """One declaration identity used by the source-bound rename planner."""

    identity: str
    name: str
    kind: str
    sig_index: int
    type_name: str = ""
    function_index: int = -1
    function_name: str = ""
    scope_start: int = -1
    scope_end: int = -1
    scope_depth: int = 0
    protected: bool = False
    reliable: bool = True
    constant_buffer: bool = False


class _RenameBindingIndex:
    """A conservative lexical symbol index for Host-applied shader renames.

    This intentionally implements a small, auditable subset of HLSL/GLSL
    declaration binding instead of pretending to be a compiler.  When an
    explicit selector cannot be bound completely it refuses the edit.  That is
    preferable to globally changing every same-spelled token in a shader.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.sig_full = [index for index, token in enumerate(self.tokens)
                         if token["kind"] not in {"comment", "ws", "string", "pp"}]
        self.words = [self.tokens[index]["text"] for index in self.sig_full]
        self.parens, self.braces, self.brackets = (
            self._pairs("(", ")"), self._pairs("{", "}"), self._pairs("[", "]")
        )
        self.reverse_brackets = {close: opening for opening, close in self.brackets.items()}
        self.brace_depth, self.nearest_scope, self.scope_depth = self._scope_maps()
        self.binding_full = _binding_token_indexes(self.tokens)
        self.declarations: dict[str, _BoundDeclaration] = {}
        self.declaration_at: dict[int, str] = {}
        self.occurrences: dict[str, set[int]] = {}
        self.by_name: dict[str, list[str]] = {}
        self.fields: dict[tuple[str, str], list[str]] = {}
        self.cbuffer_fields: dict[tuple[str, str], list[str]] = {}
        self.globals: dict[str, list[str]] = {}
        self.functions: list[dict[str, int | str]] = []
        self.function_ids: dict[str, list[str]] = {}
        self.special_spans: list[tuple[int, int]] = []
        self.known_types: set[str] = set()
        self.unresolved_members: dict[str, list[int]] = {}
        self._find_functions()
        self.function_starts = [int(record["body_open"]) for record in self.functions]
        self._find_structs_and_cbuffers()
        self._find_glsl_blocks()
        self._find_globals()
        self._find_parameters_and_locals()
        self._bind_occurrences()
        bound = set(self.declaration_at)
        for occurrences in self.occurrences.values():
            bound.update(occurrences)
        self.unbound_names = {
            word for index, word in enumerate(self.words)
            if index not in bound and _IDENTIFIER.fullmatch(word)
        }

    def _pairs(self, opening: str, closing: str) -> dict[int, int]:
        stack: list[int] = []
        result: dict[int, int] = {}
        for index, word in enumerate(self.words):
            if word == opening:
                stack.append(index)
            elif word == closing and stack:
                result[stack.pop()] = index
        return result

    def _scope_maps(self) -> tuple[list[int], list[int], dict[int, int]]:
        depth, stack = 0, []
        depths, nearest, scope_depth = [], [], {}
        for index, word in enumerate(self.words):
            depths.append(depth)
            nearest.append(stack[-1] if stack else -1)
            if word == "{":
                stack.append(index)
                scope_depth[index] = len(stack)
                depth += 1
            elif word == "}":
                depth = max(0, depth - 1)
                if stack:
                    stack.pop()
        return depths, nearest, scope_depth

    def _add(self, name: str, kind: str, sig_index: int, **values: Any) -> str:
        identity = "%s:%s@%d" % (kind, name, sig_index)
        declaration = _BoundDeclaration(identity, name, kind, sig_index, **values)
        self.declarations[identity] = declaration
        self.declaration_at[sig_index] = identity
        self.occurrences[identity] = {sig_index}
        self.by_name.setdefault(name, []).append(identity)
        if kind in {"global", "interface", "cbuffer-field", "function", "type"}:
            self.globals.setdefault(name, []).append(identity)
        return identity

    def _find_functions(self) -> None:
        identifier = re.compile(r"^[A-Za-z_]\w*$")
        for index, word in enumerate(self.words[:-1]):
            if self.brace_depth[index] != 0 or identifier.fullmatch(word) is None:
                continue
            if self.words[index + 1] != "(" or index + 1 not in self.parens:
                continue
            close = self.parens[index + 1]
            suffix = close + 1
            if suffix < len(self.words) and self.words[suffix] == ":":
                suffix += 1
                while suffix < len(self.words) and self.words[suffix] not in {"{", ";"}:
                    suffix += 1
            previous = self.words[index - 1] if index else ""
            if (previous not in {">", "]", "*", "&"} and
                    identifier.fullmatch(previous) is None):
                continue
            if suffix >= len(self.words) or self.words[suffix] != "{" or suffix not in self.braces:
                continue
            identity = self._add(word, "function", index)
            record: dict[str, int | str] = {
                "name": word, "identity": identity, "name_index": index,
                "params_open": index + 1, "params_close": close,
                "body_open": suffix, "body_close": self.braces[suffix],
            }
            function_index = len(self.functions)
            record["index"] = function_index
            self.functions.append(record)
            self.function_ids.setdefault(word, []).append(identity)
            self.special_spans.append((index, self.braces[suffix]))

    def _direct_segments(self, start: int, end: int) -> list[list[int]]:
        result: list[list[int]] = []
        pending: list[int] = []
        brace = paren = bracket = angle = 0
        for index in range(start, end):
            word = self.words[index]
            if word == "{" : brace += 1
            elif word == "}" : brace = max(0, brace - 1)
            elif word == "(" : paren += 1
            elif word == ")" : paren = max(0, paren - 1)
            elif word == "[" : bracket += 1
            elif word == "]" : bracket = max(0, bracket - 1)
            elif word == "<" : angle += 1
            elif word == ">" and angle: angle -= 1
            if word == ";" and brace == paren == bracket == angle == 0:
                if pending:
                    result.append(pending)
                pending = []
            else:
                pending.append(index)
        return result

    def _split_top_level(self, indexes: Sequence[int], delimiter: str) -> list[list[int]]:
        result, pending = [], []
        paren = bracket = brace = angle = 0
        for index in indexes:
            word = self.words[index]
            if word == delimiter and paren == bracket == brace == angle == 0:
                result.append(pending)
                pending = []
                continue
            pending.append(index)
            if word == "(" : paren += 1
            elif word == ")" : paren = max(0, paren - 1)
            elif word == "[" : bracket += 1
            elif word == "]" : bracket = max(0, bracket - 1)
            elif word == "{" : brace += 1
            elif word == "}" : brace = max(0, brace - 1)
            elif word == "<" : angle += 1
            elif word == ">" and angle: angle -= 1
        if pending:
            result.append(pending)
        return result

    def _declarators(self, indexes: Sequence[int]) -> list[tuple[int, str, bool]]:
        """Return declaration-name index, value type and ConstantBuffer flag."""

        groups = self._split_top_level(indexes, ",")
        result: list[tuple[int, str, bool]] = []
        declared_type, constant_buffer = "", False
        for group_index, raw_group in enumerate(groups):
            group: list[int] = []
            paren = bracket = brace = angle = 0
            for index in raw_group:
                word = self.words[index]
                if word in {":", "="} and paren == bracket == brace == angle == 0:
                    break
                group.append(index)
                if word == "(" : paren += 1
                elif word == ")" : paren = max(0, paren - 1)
                elif word == "[" : bracket += 1
                elif word == "]" : bracket = max(0, bracket - 1)
                elif word == "{" : brace += 1
                elif word == "}" : brace = max(0, brace - 1)
                elif word == "<" : angle += 1
                elif word == ">" and angle: angle -= 1
            top_identifiers: list[int] = []
            angle = paren = bracket = 0
            for index in group:
                word = self.words[index]
                if word == "<": angle += 1; continue
                if word == ">" and angle: angle -= 1; continue
                if word == "(": paren += 1; continue
                if word == ")": paren = max(0, paren - 1); continue
                if word == "[": bracket += 1; continue
                if word == "]": bracket = max(0, bracket - 1); continue
                if angle == paren == bracket == 0 and _IDENTIFIER.fullmatch(word):
                    top_identifiers.append(index)
            if not top_identifiers:
                continue
            name_index = top_identifiers[-1]
            if group_index == 0:
                type_indexes = [item for item in top_identifiers[:-1]
                                if self.words[item] not in _DECL_QUALIFIERS]
                if not type_indexes:
                    continue
                declared_type = self.words[type_indexes[-1]]
                constant_buffer = self.words[type_indexes[0]] in {"ConstantBuffer", "TextureBuffer"}
                if constant_buffer:
                    opening = next((item for item in group if self.words[item] == "<"), -1)
                    closing = max((item for item in group if self.words[item] == ">"), default=-1)
                    inner = [self.words[item] for item in group
                             if opening < item < closing and _IDENTIFIER.fullmatch(self.words[item])]
                    if inner:
                        declared_type = inner[-1]
            if not declared_type or self.words[name_index] in _DECL_QUALIFIERS:
                continue
            result.append((name_index, declared_type, constant_buffer))
        return result

    def _find_structs_and_cbuffers(self) -> None:
        for index, word in enumerate(self.words):
            if self.brace_depth[index] != 0 or word not in {"struct", "cbuffer", "tbuffer"}:
                continue
            if index + 2 >= len(self.words) or _IDENTIFIER.fullmatch(self.words[index + 1]) is None:
                continue
            name, opening = self.words[index + 1], index + 2
            while opening < len(self.words) and self.words[opening] not in {"{", ";"}:
                opening += 1
            if opening >= len(self.words) or self.words[opening] != "{" or opening not in self.braces:
                continue
            closing = self.braces[opening]
            owner_kind = "cbuffer" if word in {"cbuffer", "tbuffer"} else "struct"
            self._add(
                name, "type", index + 1,
                constant_buffer=owner_kind == "cbuffer",
                scope_start=opening, scope_end=closing,
            )
            self.known_types.add(name)
            self.special_spans.append((index, closing))
            for segment in self._direct_segments(opening + 1, closing):
                for member_index, type_name, unused_constant in self._declarators(segment):
                    member = self.words[member_index]
                    identity = self._add(
                        member, "cbuffer-field" if owner_kind == "cbuffer" else "field",
                        member_index, type_name=type_name,
                        scope_start=opening, scope_end=closing,
                    )
                    target = self.cbuffer_fields if owner_kind == "cbuffer" else self.fields
                    target.setdefault((name, member), []).append(identity)

    def _find_glsl_blocks(self) -> None:
        occupied = list(self.special_spans)
        last_boundary = 0
        for opening, word in enumerate(self.words):
            if self.brace_depth[opening] != 0 or word != "{" or opening not in self.braces:
                continue
            if any(start <= opening <= end for start, end in occupied):
                continue
            prefix = self.words[last_boundary:opening]
            storage_offsets = [offset for offset, item in enumerate(prefix)
                               if item in _GLSL_INTERFACE_STORAGE]
            if not storage_offsets or opening == 0 or _IDENTIFIER.fullmatch(self.words[opening - 1]) is None:
                continue
            storage_index = last_boundary + storage_offsets[-1]
            block_name, closing = self.words[opening - 1], self.braces[opening]
            self._add(block_name, "type", opening - 1, protected=True,
                      scope_start=opening, scope_end=closing)
            self.known_types.add(block_name)
            self.special_spans.append((storage_index, closing))
            for segment in self._direct_segments(opening + 1, closing):
                for member_index, type_name, unused_constant in self._declarators(segment):
                    member = self.words[member_index]
                    identity = self._add(member, "field", member_index,
                                         type_name=type_name, protected=True,
                                         scope_start=opening, scope_end=closing)
                    self.fields.setdefault((block_name, member), []).append(identity)
            cursor = closing + 1
            if cursor < len(self.words) and _IDENTIFIER.fullmatch(self.words[cursor]):
                self._add(self.words[cursor], "interface", cursor, type_name=block_name, protected=True)
            occupied.append((storage_index, closing))
            last_boundary = closing + 1

    def _overlaps_special(self, start: int, end: int) -> bool:
        return any(start <= right and left <= end for left, right in self.special_spans)

    def _find_globals(self) -> None:
        start = 0
        for index, word in enumerate(self.words):
            if word == "}" and self.brace_depth[index] == 1:
                start = index + 1
                continue
            if word != ";" or self.brace_depth[index] != 0:
                continue
            segment = list(range(start, index))
            start = index + 1
            if not segment or self._overlaps_special(segment[0], index):
                continue
            storage_positions = [position for position in segment
                                 if self.words[position] in _GLSL_INTERFACE_STORAGE]
            protected = bool(storage_positions)
            declaration_segment = segment[segment.index(storage_positions[-1]) + 1:] if protected else segment
            for name_index, type_name, constant_buffer in self._declarators(declaration_segment):
                name = self.words[name_index]
                self._add(name, "interface" if protected else "global", name_index,
                          type_name=type_name, protected=protected, constant_buffer=constant_buffer)

    def _function_for(self, sig_index: int) -> dict[str, int | str] | None:
        position = bisect_right(self.function_starts, sig_index) - 1
        if position >= 0:
            record = self.functions[position]
            if int(record["body_open"]) < sig_index < int(record["body_close"]):
                return record
        return None

    def _is_type(self, word: str) -> bool:
        return bool(
            word in self.known_types or _TYPE.fullmatch(word) or
            re.fullmatch(r"(?:[biud]?vec)[234]|d?mat[234](?:x[234])?|atomic_uint", word) or
            re.fullmatch(r"(?:RW)?(?:Texture|Buffer|ByteAddressBuffer|StructuredBuffer|"
                         r"AppendStructuredBuffer|ConsumeStructuredBuffer|Sampler)\w*", word)
        )

    def _parameter_declarator(self, indexes: Sequence[int]) -> tuple[int, str] | None:
        group: list[int] = []
        paren = bracket = angle = 0
        for index in indexes:
            word = self.words[index]
            if word in {":", "="} and paren == bracket == angle == 0:
                break
            group.append(index)
            if word == "(" : paren += 1
            elif word == ")" : paren = max(0, paren - 1)
            elif word == "[" : bracket += 1
            elif word == "]" : bracket = max(0, bracket - 1)
            elif word == "<" : angle += 1
            elif word == ">" and angle: angle -= 1
        identifiers = [index for index in group if _IDENTIFIER.fullmatch(self.words[index])]
        identifiers = [index for index in identifiers if self.words[index] not in _DECL_QUALIFIERS]
        if len(identifiers) < 2:
            return None
        name_index = identifiers[-1]
        type_name = self.words[identifiers[-2]]
        return name_index, type_name

    def _find_parameters_and_locals(self) -> None:
        for function_index, record in enumerate(self.functions):
            groups = self._split_top_level(
                range(int(record["params_open"]) + 1, int(record["params_close"])), ","
            )
            for group in groups:
                parsed = self._parameter_declarator(group)
                if parsed is None:
                    continue
                name_index, type_name = parsed
                self._add(
                    self.words[name_index], "parameter", name_index, type_name=type_name,
                    function_index=function_index, function_name=str(record["name"]),
                    scope_start=int(record["body_open"]), scope_end=int(record["body_close"]),
                    scope_depth=self.scope_depth.get(int(record["body_open"]), 1),
                )

            statement_start = int(record["body_open"]) + 1
            for cursor in range(statement_start, int(record["body_close"])):
                word = self.words[cursor]
                if word == "{":
                    statement_start = cursor + 1
                    continue
                if word != ";":
                    continue
                segment = list(range(statement_start, cursor))
                statement_start = cursor + 1
                type_start = -1
                for offset, position in enumerate(segment):
                    if not self._is_type(self.words[position]):
                        continue
                    before = [self.words[item] for item in segment[:offset]]
                    if "=" in before or "return" in before:
                        continue
                    type_start = offset
                    break
                if type_start < 0:
                    continue
                declaration_segment = segment[type_start:]
                for name_index, type_name, constant_buffer in self._declarators(declaration_segment):
                    scope = self.nearest_scope[name_index]
                    if scope < 0 or scope not in self.braces:
                        continue
                    prefix = {self.words[item] for item in segment[:type_start]}
                    reliable = "for" not in prefix
                    self._add(
                        self.words[name_index], "local", name_index, type_name=type_name,
                        function_index=function_index, function_name=str(record["name"]),
                        scope_start=scope, scope_end=self.braces[scope],
                        scope_depth=self.scope_depth.get(scope, 1), reliable=reliable,
                        constant_buffer=constant_buffer,
                    )

    def _variable_declaration(self, sig_index: int) -> _BoundDeclaration | None:
        identity = self._resolve_bare(sig_index, variables_only=True)
        return self.declarations.get(identity) if identity else None

    def _resolve_bare(self, sig_index: int, *, variables_only: bool = False) -> str | None:
        if sig_index in self.declaration_at:
            declaration = self.declarations[self.declaration_at[sig_index]]
            if not variables_only or declaration.kind in {"global", "interface", "parameter", "local", "cbuffer-field"}:
                return declaration.identity
        name = self.words[sig_index]
        record = self._function_for(sig_index)
        if record is not None:
            matches = []
            for identity in self.by_name.get(name, []):
                declaration = self.declarations[identity]
                if declaration.kind not in {"local", "parameter"} or declaration.function_index != int(record["index"]):
                    continue
                if declaration.scope_start < sig_index < declaration.scope_end and (
                        declaration.kind == "parameter" or declaration.sig_index <= sig_index):
                    matches.append(declaration)
            if matches:
                matches.sort(key=lambda item: (item.scope_depth, item.sig_index), reverse=True)
                if len(matches) == 1 or (matches[0].scope_depth, matches[0].sig_index) != (
                        matches[1].scope_depth, matches[1].sig_index):
                    return matches[0].identity
                return None
        allowed = {"global", "interface", "cbuffer-field"}
        if not variables_only:
            allowed.update({"function", "type"})
        matches = [identity for identity in self.globals.get(name, [])
                   if self.declarations[identity].kind in allowed]
        if len(matches) == 1:
            return matches[0]
        return None

    def _access_path(self, member_index: int) -> tuple[int, list[str]] | None:
        dot = member_index - 1
        if dot < 1 or self.words[dot] not in {".", "->"}:
            return None
        cursor, reversed_parts = dot - 1, []
        while cursor >= 0:
            if self.words[cursor] == "]":
                opening = self.reverse_brackets.get(cursor)
                if opening is None or opening == 0:
                    return None
                cursor = opening - 1
            if cursor < 0 or _IDENTIFIER.fullmatch(self.words[cursor]) is None:
                return None
            reversed_parts.append(self.words[cursor])
            root = cursor
            cursor -= 1
            if cursor >= 0 and self.words[cursor] in {".", "->"}:
                cursor -= 1
                continue
            return root, list(reversed(reversed_parts))
        return None

    def _field_for_member(self, member_index: int) -> str | None:
        path = self._access_path(member_index)
        if path is None:
            return None
        root_index, owners = path
        if len(owners) == 1:
            cbuffer = self.cbuffer_fields.get((owners[0], self.words[member_index]), [])
            if len(cbuffer) == 1:
                return cbuffer[0]
        declaration = self._variable_declaration(root_index)
        if declaration is None or not declaration.type_name:
            return None
        type_name = declaration.type_name
        # Every part after the root is a field whose declared type feeds the
        # next access. This also handles cbuffer fields containing a struct.
        for part in owners[1:]:
            candidates = self.fields.get((type_name, part), [])
            if len(candidates) != 1:
                return None
            type_name = self.declarations[candidates[0]].type_name
        candidates = self.fields.get((type_name, self.words[member_index]), [])
        return candidates[0] if len(candidates) == 1 else None

    def _bind_occurrences(self) -> None:
        # Member accesses need type/owner binding; a plain token replacement is
        # never used for them.
        for index, word in enumerate(self.words):
            if index in self.declaration_at or _IDENTIFIER.fullmatch(word) is None:
                continue
            full_index = self.sig_full[index]
            if full_index in self.binding_full or not _is_member_access(self.tokens, full_index):
                continue
            identity = self._field_for_member(index)
            if identity:
                self.occurrences[identity].add(index)
            elif _SWIZZLE.fullmatch(word):
                # A typed struct member named ``r``/``xy`` is a field; only an
                # unresolved component access is protected as a swizzle.
                continue
            else:
                self.unresolved_members.setdefault(word, []).append(index)

        for index, word in enumerate(self.words):
            if index in self.declaration_at or _IDENTIFIER.fullmatch(word) is None:
                continue
            full_index = self.sig_full[index]
            if full_index in self.binding_full or _is_member_access(self.tokens, full_index):
                continue
            identity = self._resolve_bare(index)
            if identity:
                self.occurrences[identity].add(index)

    @staticmethod
    def _local_lifetime(declaration: _BoundDeclaration) -> tuple[int, int]:
        start = (declaration.scope_start + 1 if declaration.kind == "parameter"
                 else declaration.sig_index)
        return start, declaration.scope_end

    def target_conflicts(self, identity: str, other_identity: str) -> bool:
        """Whether giving two declarations one name could change a binding.

        Locals in separate functions or disjoint lexical lifetimes may share a
        spelling.  A local may also reuse an outer name if no reference to the
        outer declaration falls within its lifetime.  Unknown bindings stay
        conservative; the caller checks ``unbound_names`` separately.
        """

        if identity == other_identity:
            return False
        declaration = self.declarations[identity]
        other = self.declarations[other_identity]
        local = declaration.kind in {"local", "parameter"}
        other_local = other.kind in {"local", "parameter"}
        if local and other_local:
            if declaration.function_index != other.function_index:
                return False
            begin, end = self._local_lifetime(declaration)
            other_begin, other_end = self._local_lifetime(other)
            return max(begin, other_begin) < min(end, other_end)
        if local or other_local:
            inner = declaration if local else other
            outer = other if local else declaration
            # Struct fields are always accessed through an owner; a local
            # with the same spelling cannot capture an ``owner.member`` use.
            if outer.kind == "field":
                return False
            begin, end = self._local_lifetime(inner)
            return any(begin <= position < end
                       for position in self.occurrences[outer.identity])
        if declaration.kind == "field" or other.kind == "field":
            # The two owners might be different, but conservatively retain
            # existing field-to-field collision protection.
            return declaration.kind == other.kind
        return True

    @staticmethod
    def selector_leaf(selector: str) -> str:
        declaration = _DECL_SELECTOR.fullmatch(selector)
        if declaration:
            return declaration.group("name")
        field = _FIELD_SELECTOR.fullmatch(selector)
        if field:
            return field.group("member")
        local = _LOCAL_SELECTOR.fullmatch(selector)
        if local:
            return local.group("local")
        return selector

    @staticmethod
    def canonical_selector(selector: str, owner_aliases: Mapping[str, str]) -> str:
        """Resolve a selector written against the already-renamed proposal.

        Sessions regenerate from their sealed baseline.  After ``Main`` has
        become ``Shade`` an Agent naturally asks for ``Shade::_9``; store and
        replay that selector as ``Main::_9`` so later rounds remain stable.
        """

        field = _FIELD_SELECTOR.fullmatch(selector)
        local = _LOCAL_SELECTOR.fullmatch(selector)
        if _DECL_SELECTOR.fullmatch(selector):
            return selector
        if not (field or local):
            return selector
        separator = "." if field else "::"
        owner = (field or local).group("owner" if field else "function")
        leaf = (field or local).group("member" if field else "local")
        seen: set[str] = set()
        while owner in owner_aliases and owner not in seen:
            seen.add(owner)
            replacement = owner_aliases[owner]
            if _IDENTIFIER.fullmatch(replacement) is None:
                break
            owner = replacement
        return owner + separator + leaf

    def resolve(self, selector: str, *, swizzle_noop: bool = False) -> tuple[str, set[int], str]:
        declaration_selector = _DECL_SELECTOR.fullmatch(selector)
        field = _FIELD_SELECTOR.fullmatch(selector)
        local = _LOCAL_SELECTOR.fullmatch(selector)
        if ("." in selector or ":" in selector) and not (
            declaration_selector or field or local
        ):
            raise EquivalenceError(
                "unsupported rename selector '%s'; use cbuffer:name, resource:name, "
                "cbuffer.member, owner.member, or Function::local" % selector
            )
        if declaration_selector:
            kind = declaration_selector.group("kind")
            name = declaration_selector.group("name")
            if kind == "cbuffer":
                candidates = [
                    identity for identity in self.by_name.get(name, [])
                    if self.declarations[identity].constant_buffer
                    and self.declarations[identity].kind in {"type", "global"}
                ]
            else:
                candidates = [
                    identity for identity in self.by_name.get(name, [])
                    if self.declarations[identity].kind == "global"
                    and not self.declarations[identity].constant_buffer
                    and re.fullmatch(
                        r"(?:(?:RasterizerOrdered)?(?:RW)?Texture\w*|FeedbackTexture\w*|"
                        r"(?:RasterizerOrdered)?(?:RW)?(?:StructuredBuffer|ByteAddressBuffer|Buffer)|"
                        r"AppendStructuredBuffer|ConsumeStructuredBuffer|SamplerState|"
                        r"SamplerComparisonState|RaytracingAccelerationStructure)",
                        self.declarations[identity].type_name,
                    )
                ]
            if len(candidates) != 1:
                raise EquivalenceError(
                    "selector '%s' does not identify one %s declaration"
                    % (selector, "constant-buffer" if kind == "cbuffer" else "resource")
                )
            bound = self.declarations[candidates[0]]
            if bound.protected:
                raise EquivalenceError("selector '%s' names a protected GLSL interface" % selector)
            return bound.identity, set(self.occurrences[bound.identity]), name
        if local:
            function, name = local.group("function"), local.group("local")
            records = [record for record in self.functions if record["name"] == function]
            candidates = [identity for identity in self.by_name.get(name, [])
                          if self.declarations[identity].kind in {"local", "parameter"}
                          and self.declarations[identity].function_name == function]
            if len(records) != 1:
                lines = [self.tokens[self.sig_full[int(record["name_index"])]]["line"]
                         for record in records[:6]]
                raise EquivalenceError(
                    "selector '%s' is not a unique declaration in one function scope; "
                    "function definitions: %s" %
                    (selector, ", ".join("line %d" % line for line in lines) or "none")
                )
            if len(candidates) != 1:
                locations = []
                for identity in candidates[:6]:
                    declaration = self.declarations[identity]
                    token = self.tokens[self.sig_full[declaration.sig_index]]
                    start = int(token["start"])
                    column = start - self.source.rfind("\n", 0, start)
                    block_start = self.tokens[self.sig_full[declaration.scope_start]]["line"]
                    block_end = self.tokens[self.sig_full[declaration.scope_end]]["line"]
                    locations.append(
                        "line %d:%d (block %d-%d)" %
                        (token["line"], column, block_start, block_end)
                    )
                if len(candidates) > 6:
                    locations.append("%d more" % (len(candidates) - 6))
                raise EquivalenceError(
                    "selector '%s' is not a unique declaration in one function scope; "
                    "local or parameter declarations: %s" %
                    (selector, ", ".join(locations) or "none")
                )
            declaration = self.declarations[candidates[0]]
            if not declaration.reliable:
                raise EquivalenceError("selector '%s' has a control-flow scope the Host cannot bind reliably" % selector)
            return declaration.identity, set(self.occurrences[declaration.identity]), name
        if field:
            owner, member = field.group("owner"), field.group("member")
            candidates = list(self.cbuffer_fields.get((owner, member), []))
            # A pure/internal struct type is also a stable owner.  This keeps
            # ``Type.member`` distinct from an unrelated local or another
            # struct with the same member spelling.
            candidates.extend(self.fields.get((owner, member), []))
            owner_declarations = [self.declarations[identity] for identity in self.globals.get(owner, [])
                                  if self.declarations[identity].kind == "global"
                                  and self.declarations[identity].constant_buffer]
            if owner_declarations:
                if len(owner_declarations) != 1:
                    candidates.extend("" for unused in owner_declarations)
                else:
                    candidates.extend(self.fields.get((owner_declarations[0].type_name, member), []))
            candidates = list(dict.fromkeys(identity for identity in candidates if identity))
            if len(candidates) != 1:
                raise EquivalenceError(
                    "selector '%s' does not identify one cbuffer, ConstantBuffer<T>, or struct field" % selector
                )
            declaration = self.declarations[candidates[0]]
            if declaration.protected:
                raise EquivalenceError("selector '%s' names a protected GLSL interface" % selector)
            if declaration.kind == "field" and self.unresolved_members.get(member):
                raise EquivalenceError(
                    "selector '%s' has member accesses whose owner type cannot be bound reliably" % selector
                )
            return declaration.identity, set(self.occurrences[declaration.identity]), member

        if _IDENTIFIER.fullmatch(selector) is None:
            raise EquivalenceError("'%s' is not an identifier or supported source-bound selector" % selector)
        declarations = list(self.by_name.get(selector, []))
        if len(declarations) > 1:
            raise EquivalenceError(
                "plain identifier '%s' has multiple declaration identities; use cbuffer:name, "
                "resource:name, cbuffer.member, owner.member, or Function::local" % selector
            )
        if len(declarations) == 1:
            declaration = self.declarations[declarations[0]]
            if declaration.protected:
                raise EquivalenceError("'%s' is a protected GLSL interface and cannot be renamed" % selector)
            if not declaration.reliable:
                raise EquivalenceError("'%s' has a scope the Host cannot bind reliably" % selector)
            if declaration.kind == "field" and self.unresolved_members.get(selector):
                raise EquivalenceError("'%s' has member accesses whose owner type cannot be bound reliably" % selector)
            return declaration.identity, set(self.occurrences[declaration.identity]), selector

        occurrences = {index for index, word in enumerate(self.words) if word == selector
                       and self.sig_full[index] not in self.binding_full
                       and not _is_member_access(self.tokens, self.sig_full[index])}
        if occurrences:
            return "unbound:%s" % selector, occurrences, selector
        swizzles = [index for index, word in enumerate(self.words) if word == selector
                    and _is_member_access(self.tokens, self.sig_full[index])
                    and _SWIZZLE.fullmatch(word)]
        if swizzles and swizzle_noop:
            return "swizzle:%s" % selector, set(), selector
        if swizzles:
            raise EquivalenceError("'%s' only occurs as a swizzle; component selection cannot be renamed" % selector)
        if self.unresolved_members.get(selector):
            raise EquivalenceError("'%s' is an unbound member access; use a supported owner.member selector" % selector)
        raise EquivalenceError("'%s' does not occur in the baseline source" % selector)


_RENAME_INDEX_CACHE_MAX_SOURCE = 1024 * 1024
_RENAME_INDEX_CACHE_MAX_ENTRIES = 2
_RENAME_INDEX_CACHE: OrderedDict[bytes, tuple[str, _RenameBindingIndex]] = OrderedDict()
_RENAME_INDEX_CACHE_LOCK = threading.Lock()


def _binding_index(source: str) -> _RenameBindingIndex:
    """Reuse an immutable lexical index only for the exact same source text.

    A digest makes lookup bounded; equality is still checked before reuse.
    Larger programs bypass this small LRU so a long-running Host cannot keep
    arbitrarily large token tables alive between unrelated workspaces.
    """

    if len(source) > _RENAME_INDEX_CACHE_MAX_SOURCE:
        return _RenameBindingIndex(source)
    digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).digest()
    with _RENAME_INDEX_CACHE_LOCK:
        cached = _RENAME_INDEX_CACHE.get(digest)
        if cached is not None and cached[0] == source:
            _RENAME_INDEX_CACHE.move_to_end(digest)
            return cached[1]
    index = _RenameBindingIndex(source)
    with _RENAME_INDEX_CACHE_LOCK:
        cached = _RENAME_INDEX_CACHE.get(digest)
        if cached is not None and cached[0] == source:
            _RENAME_INDEX_CACHE.move_to_end(digest)
            return cached[1]
        _RENAME_INDEX_CACHE[digest] = (source, index)
        _RENAME_INDEX_CACHE.move_to_end(digest)
        while len(_RENAME_INDEX_CACHE) > _RENAME_INDEX_CACHE_MAX_ENTRIES:
            _RENAME_INDEX_CACHE.popitem(last=False)
    return index


def rename_targets_can_share(source: str, first_selector: str,
                             second_selector: str, target: str | None = None) -> bool:
    """Prove that two source-bound declarations may safely share one name.

    This is also used by structural-ledger validation.  If either selector
    is missing, ambiguous or protected, the answer is conservatively false.
    """

    index = _binding_index(source)
    if target is not None and (
        _IDENTIFIER.fullmatch(target) is None
        or is_reserved(target, as_target=True)
        or target.startswith("gl_")
        or "__" in target
        or target in index.unbound_names
    ):
        return False
    try:
        first, unused_occurrences, unused_leaf = index.resolve(first_selector)
        second, unused_occurrences, unused_leaf = index.resolve(second_selector)
    except EquivalenceError:
        return False
    return first != second and not index.target_conflicts(first, second)


def inspect_symbol(source: str, selector: str, *, limit: int = 32) -> dict[str, Any]:
    """Describe one lexical symbol without claiming semantic data flow.

    Declaration identities and character offsets are valid only for this exact
    source revision.  A caller exposing this across a process boundary must
    include the source hash.  Bound references are a conservative subset of
    lexical occurrences, not a proof of every runtime read or write.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
        raise EquivalenceError("symbol reference limit must be an integer from 1 to 64")
    if not isinstance(selector, str) or not selector.strip():
        raise EquivalenceError("symbol selector must be nonempty text")
    selector = selector.strip()
    index = _binding_index(source)
    identity, unused_occurrences, unused_leaf = index.resolve(selector)
    declaration = index.declarations.get(identity)
    if declaration is None:
        raise EquivalenceError(
            "selector '%s' has no uniquely bound declaration to inspect" % selector
        )

    def location(sig_index: int) -> dict[str, int]:
        token = index.tokens[index.sig_full[sig_index]]
        offset = int(token["start"])
        return {
            "line": int(token["line"]),
            "column": offset - source.rfind("\n", 0, offset),
            "characterOffset": offset,
        }

    declaration_location = location(declaration.sig_index)
    owner = None
    if declaration.kind in {"field", "cbuffer-field"}:
        lookup = index.fields if declaration.kind == "field" else index.cbuffer_fields
        owners = [name for (name, member), identities in lookup.items()
                  if member == declaration.name and identity in identities]
        owner = owners[0] if len(owners) == 1 else None

    function_range = None
    if declaration.function_index >= 0:
        function = index.functions[declaration.function_index]
        function_range = {
            "name": str(function["name"]),
            "headerLine": location(int(function["name_index"]))["line"],
            "bodyStartLine": location(int(function["body_open"]))["line"],
            "bodyEndLine": location(int(function["body_close"]))["line"],
        }

    if declaration.kind in {"local", "parameter"}:
        scope_kind = "function" if declaration.kind == "parameter" else "block"
        scope_start = location(declaration.scope_start)["line"]
        scope_end = location(declaration.scope_end)["line"]
        visible_from = (
            scope_start if declaration.kind == "parameter"
            else declaration_location["line"]
        )
    elif declaration.kind in {"field", "cbuffer-field"} and declaration.scope_start >= 0:
        scope_kind = "owner-block"
        scope_start = location(declaration.scope_start)["line"]
        scope_end = location(declaration.scope_end)["line"]
        visible_from = declaration_location["line"]
    else:
        scope_kind = "translation-unit"
        scope_start = 1
        scope_end = source.count("\n") + int(not source.endswith("\n"))
        visible_from = None

    references = sorted(index.occurrences[identity] - {declaration.sig_index})
    return {
        "schema": "replaylab-shader-symbol-inspection/v1",
        "schemaVersion": 1,
        "selector": selector,
        "identity": identity,
        "identityScope": "exact-source-only",
        "name": declaration.name,
        "kind": declaration.kind,
        "owner": owner,
        "declaration": declaration_location,
        "lexicalScope": {
            "kind": scope_kind,
            "function": declaration.function_name or None,
            "startLine": scope_start,
            "endLine": scope_end,
            "visibleFromLine": visible_from,
        },
        "functionRange": function_range,
        "references": {
            "boundCount": len(references),
            "locations": [location(position) for position in references[:limit]],
            "truncated": len(references) > limit,
            "unresolvedSameName": declaration.name in index.unbound_names,
        },
        "dependencies": {
            "status": "unknown",
            "reason": "lexical binding does not prove complete expression or runtime data flow",
        },
    }


def _rename_replacements(source: str, renames: Mapping[str, str], *,
                         swizzle_noop: bool = False) -> tuple[_RenameBindingIndex, dict[int, str]]:
    index = _binding_index(source)
    replacements: dict[int, str] = {}
    selected: dict[str, str] = {}
    owner_aliases = _rename_owner_aliases(renames)
    for raw_selector, raw_target in renames.items():
        selector = index.canonical_selector(str(raw_selector).strip(), owner_aliases)
        target = str(raw_target).strip()
        # Selector-valued targets occur only in the internal inverse map used
        # to bind structure anchors. Restore the declaration's leaf token.
        target_leaf = index.selector_leaf(target)
        identity, occurrences, old_leaf = index.resolve(selector, swizzle_noop=swizzle_noop)
        if old_leaf == target_leaf:
            continue
        previous = selected.get(identity)
        if previous is not None and previous != target_leaf:
            raise EquivalenceError("multiple rename selectors target the same declaration differently")
        selected[identity] = target_leaf
        for sig_index in occurrences:
            full_index = index.sig_full[sig_index]
            previous_target = replacements.get(full_index)
            if previous_target is not None and previous_target != target_leaf:
                raise EquivalenceError("overlapping rename selectors request different targets")
            replacements[full_index] = target_leaf
    return index, replacements


# ----------------------------------------------------------------- renames
# One submission stays small so a malformed request cannot become a huge edit.
MAX_RENAMES_PER_REQUEST = 512
# The accumulated map of a session is a different thing: it grows by design, one
# batch per round, until every anonymous local has a name. A machine-generated
# shader can have thousands, so bounding the *total* at the per-request number
# froze exactly the shaders this feature exists for -- the ledger filled up and no
# further rename was possible. The real bound is the source itself: a map can
# never usefully hold more entries than the source has distinct identifiers.
MAX_RENAMES_TOTAL = 20000


def validate_renames(source: str, renames: Mapping[str, Any], *,
                     limit: int = MAX_RENAMES_PER_REQUEST,
                     known_old_leaves: Sequence[str] = ()) -> dict[str, str]:
    """Return a clean selector -> name map, bound to source declarations.

    Plain identifiers remain convenient when exactly one declaration owns the
    spelling.  Ambiguous fields/locals require ``cbuffer.member``,
    ``owner.member`` or ``Function::local`` so a rename cannot leak into an
    unrelated declaration with the same decompiler-generated name.

    Global declarations and function-local declarations are indexed separately.
    A target already used elsewhere is allowed only when its lexical lifetime
    does not overlap and no outer reference would be captured.  A same-scope
    local collision (the extract out-param / local trap) receives a free
    ``<target>_N`` instead of an invalid rename ledger.
    """

    if not isinstance(renames, Mapping):
        raise EquivalenceError("renames must be an object mapping old identifier to new identifier")
    if len(renames) > max(1, int(limit)):
        raise EquivalenceError("at most %d renames per request" % max(1, int(limit)))
    binding_index = _binding_index(source)
    present = {token["text"] for token in binding_index.tokens if token["kind"] == "ident"}
    problems: list[str] = []
    clean: dict[str, str] = {}
    claimed_targets: dict[str, list[str]] = {}
    selected_identities: dict[str, str] = {}
    owner_aliases = _rename_owner_aliases(renames)
    old_leaves = ({binding_index.selector_leaf(str(key or "").strip()) for key in renames} |
                  {str(name) for name in known_old_leaves})
    # Existing names selected for a simultaneous rename are vacated.  A name
    # mentioned in known_old_leaves alone is not proof that its declaration is
    # being changed; the source-bound identity is what makes a swap safe.
    planned: dict[str, str] = {}
    for old, new in renames.items():
        selector = binding_index.canonical_selector(str(old or "").strip(), owner_aliases)
        target = str(new or "").strip()
        if (_IDENTIFIER.fullmatch(target) is None or is_reserved(target, as_target=True)
                or target.startswith("gl_") or "__" in target):
            continue
        try:
            identity, unused_occurrences, old_leaf = binding_index.resolve(selector)
        except EquivalenceError:
            continue
        if old_leaf != target:
            planned[identity] = target

    def _conflicts(identity: str, candidate: str) -> tuple[bool, bool]:
        """Return (collision, local/parameter collision eligible for suffix)."""

        collision = local_collision = False
        for other_identity in binding_index.by_name.get(candidate, []):
            if other_identity == identity or planned.get(other_identity, candidate) != candidate:
                continue
            if not binding_index.target_conflicts(identity, other_identity):
                continue
            collision = True
            declaration = binding_index.declarations.get(identity)
            other = binding_index.declarations.get(other_identity)
            local_collision |= bool(
                declaration and other
                and declaration.kind in {"local", "parameter"}
                and other.kind in {"local", "parameter"}
            )
        for other_identity in claimed_targets.get(candidate, []):
            if other_identity == identity or not binding_index.target_conflicts(identity, other_identity):
                continue
            collision = True
            declaration = binding_index.declarations.get(identity)
            other = binding_index.declarations.get(other_identity)
            local_collision |= bool(
                declaration and other
                and declaration.kind in {"local", "parameter"}
                and other.kind in {"local", "parameter"}
            )
        # Unknown tokens may be macros, builtins or declarations outside this
        # conservative parser; no scope proof is available for them.
        return collision or candidate in binding_index.unbound_names, local_collision

    def _disambiguate_target(base: str) -> str:
        candidate, suffix = base, 1
        occupied = set(present) | set(claimed_targets) | set(old_leaves)
        while (
            candidate in occupied
            or is_reserved(candidate, as_target=True)
            or candidate.startswith("gl_")
            or "__" in candidate
        ):
            candidate = "%s_%d" % (base, suffix)
            suffix += 1
            if suffix > 10000:
                raise EquivalenceError("could not allocate a unique rename target for '%s'" % base)
        return candidate

    for old, new in renames.items():
        requested_name = str(old or "").strip()
        old_name = binding_index.canonical_selector(requested_name, owner_aliases)
        new_name = str(new or "").strip()
        old_leaf = binding_index.selector_leaf(old_name)
        if _IDENTIFIER.fullmatch(new_name) is None:
            problems.append(f"'{requested_name}' -> '{new_name}': target must be a plain identifier")
            continue
        if old_leaf == new_name:
            continue
        if is_reserved(old_leaf) or old_leaf.startswith("gl_"):
            problems.append(f"'{old_leaf}' is a keyword/type/intrinsic/semantic and cannot be renamed")
            continue
        if is_reserved(new_name, as_target=True):
            problems.append(f"'{new_name}' is a reserved HLSL word; pick another name for '{requested_name}'")
            continue
        if new_name.startswith("gl_") or "__" in new_name:
            problems.append(f"'{new_name}' is a reserved shader name; pick another name for '{requested_name}'")
            continue
        try:
            identity, unused_occurrences, unused_leaf = binding_index.resolve(old_name)
        except EquivalenceError as exc:
            problems.append(str(exc))
            continue
        if identity in selected_identities and selected_identities[identity] != new_name:
            problems.append(
                f"'{selected_identities[identity]}' and '{new_name}' are different targets for one declaration"
            )
            continue
        collision, local_collision = _conflicts(identity, new_name)
        if collision:
            if local_collision and new_name not in binding_index.unbound_names:
                new_name = _disambiguate_target(new_name)
            else:
                problems.append(
                    f"'{new_name}' already exists in an overlapping scope or has unbound uses; "
                    f"renaming '{requested_name}' to it would collide"
                )
                continue
        selected_identities[identity] = new_name
        if old_name in clean and clean[old_name] != new_name:
            problems.append(f"selector '{requested_name}' duplicates '{old_name}' with a different target")
            continue
        clean[old_name] = new_name
        claimed_targets.setdefault(new_name, []).append(identity)
    if problems:
        raise EquivalenceError("; ".join(problems[:12]))
    return clean


def split_bound_renames(source: str, renames: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Split a ledger into selectors bound now and declarations not introduced yet.

    Structural rewrite ledgers are replayed from the original source.  A name
    introduced by a later rewrite cannot be applied to that baseline, while an
    already-present name must bind before extraction can duplicate its logical
    value into helper and caller declarations.  Ambiguous or protected names
    are errors immediately; only selectors with no declaration in this source
    are deferred.
    """

    if not isinstance(renames, Mapping):
        raise EquivalenceError("renames must be an object mapping old identifier to new identifier")
    index = _binding_index(source)
    owner_aliases = _rename_owner_aliases(renames)
    bound: dict[str, str] = {}
    deferred: dict[str, str] = {}
    for raw_selector, raw_target in renames.items():
        requested = str(raw_selector).strip()
        selector = index.canonical_selector(requested, owner_aliases)
        target = str(raw_target).strip()
        leaf = index.selector_leaf(selector)
        if leaf == target:
            continue
        field = _FIELD_SELECTOR.fullmatch(selector)
        local = _LOCAL_SELECTOR.fullmatch(selector)
        try:
            index.resolve(selector, swizzle_noop=True)
        except EquivalenceError:
            absent = False
            if local:
                function, name = local.group("function"), local.group("local")
                records = [record for record in index.functions if record["name"] == function]
                candidates = [identity for identity in index.by_name.get(name, [])
                              if index.declarations[identity].kind in {"local", "parameter"}
                              and index.declarations[identity].function_name == function]
                absent = not records or not candidates
            elif field:
                owner, member = field.group("owner"), field.group("member")
                candidates = list(index.cbuffer_fields.get((owner, member), []))
                candidates.extend(index.fields.get((owner, member), []))
                owner_declarations = [index.declarations[identity]
                                      for identity in index.globals.get(owner, [])
                                      if index.declarations[identity].kind == "global"
                                      and index.declarations[identity].constant_buffer]
                if len(owner_declarations) == 1:
                    candidates.extend(index.fields.get((owner_declarations[0].type_name, member), []))
                candidates = list(dict.fromkeys(candidates))
                absent = not candidates and len(owner_declarations) <= 1
            elif _IDENTIFIER.fullmatch(selector):
                declarations = list(index.by_name.get(selector, []))
                raw_occurrences = [word for word in index.words if word == selector]
                absent = not declarations and not raw_occurrences
            if absent:
                deferred[selector] = target
                continue
            raise
        if selector in bound and bound[selector] != target:
            raise EquivalenceError("selector '%s' requests two different targets" % selector)
        bound[selector] = target
    return bound, deferred


def apply_renames(source: str, renames: Mapping[str, str]) -> str:
    """Apply declaration-bound renames without touching unrelated same names."""

    if not renames:
        return source
    binding_index, replacements = _rename_replacements(source, renames, swizzle_noop=True)
    out: list[str] = []
    for token_index, token in enumerate(binding_index.tokens):
        out.append(replacements.get(token_index, token["text"]))
    return "".join(out)


def _binding_token_indexes(tokens: Sequence[dict[str, Any]]) -> set[int]:
    """Indexes of tokens inside ``register(...)`` / ``packoffset(...)``."""

    protected: set[int] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token["kind"] == "ident" and token["text"] in {"register", "packoffset"}:
            look = index + 1
            while look < len(tokens) and tokens[look]["kind"] in {"ws", "comment"}:
                look += 1
            if look < len(tokens) and tokens[look]["text"] == "(":
                depth = 0
                cursor = look
                while cursor < len(tokens):
                    protected.add(cursor)
                    if tokens[cursor]["text"] == "(":
                        depth += 1
                    elif tokens[cursor]["text"] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    cursor += 1
                index = cursor
        index += 1
    return protected


_FUNCTION_HEADER = re.compile(
    r"^(?P<indent>[ \t]*)(?P<head>(?:\[[^\]\n]*\][ \t]*)*[A-Za-z_][\w<>,: ]*?\s+(?P<name>[A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)


def insert_function_comments(source: str, comments: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Put ``// text`` lines above each named function definition.

    Returns the new source and the names that were not found.  Existing
    Host-inserted comment blocks (marked ``// [ai]``) for the same function are
    replaced so repeated calls stay idempotent.
    """

    if not isinstance(comments, Mapping):
        raise EquivalenceError("comments must be an object mapping function name to text")
    missing: list[str] = []
    text = source
    for name, raw in comments.items():
        function = str(name or "").strip()
        body = " ".join(str(raw or "").split())
        if _IDENTIFIER.fullmatch(function) is None or not body:
            missing.append(function or "?")
            continue
        if len(body) > 600:
            body = body[:600]
        target = None
        for match in _FUNCTION_HEADER.finditer(text):
            if match.group("name") == function:
                # a definition, not a prototype: the header is followed by ')' ... '{'
                tail = text[match.end() :]
                close = tail.find(")")
                if close >= 0 and "{" in tail[close : close + 200].split(";")[0]:
                    target = match
                    break
        if target is None:
            missing.append(function)
            continue
        indent = target.group("indent")
        block_start = target.start()
        # drop a previous [ai] block directly above
        before = text[:block_start]
        marker = f"{indent}// [ai] "
        lines = before.split("\n")
        while len(lines) >= 2 and lines[-2].startswith(marker):
            lines.pop(-2)
        before = "\n".join(lines)
        comment = "".join(f"{indent}// [ai] {line}\n" for line in _wrap(body, 96))
        text = before + comment + text[block_start:]
    return text, missing


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# ------------------------------------------------------------- equivalence
def rename_only_equivalent(
    baseline: str, proposal: str, renames: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Is *proposal* the baseline plus renames, comments and whitespace?

    Returns ``{"equal": bool, "differences": [...], "level": "rename-only"}``;
    each difference names the baseline/proposal lines and the tokens around
    the first divergence so the model knows exactly what to revert.
    """

    mapping = {str(k): str(v) for k, v in (renames or {}).items()}
    left = code_tokens(apply_renames(baseline, mapping)) if mapping else code_tokens(baseline)
    right = code_tokens(proposal)
    differences: list[dict[str, Any]] = []
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index]["text"] == right[index]["text"]:
        index += 1
    if index == len(left) == len(right):
        return {"equal": True, "level": "rename-only", "differences": []}
    # report up to 5 divergent spots by resynchronising on line boundaries
    cursor_left, cursor_right = index, index
    for _ in range(5):
        if cursor_left >= len(left) and cursor_right >= len(right):
            break
        differences.append(
            {
                "baselineLine": left[cursor_left]["line"] if cursor_left < len(left) else None,
                "proposalLine": right[cursor_right]["line"] if cursor_right < len(right) else None,
                "baseline": _context(left, cursor_left),
                "proposal": _context(right, cursor_right),
            }
        )
        line_left = left[cursor_left]["line"] if cursor_left < len(left) else None
        line_right = right[cursor_right]["line"] if cursor_right < len(right) else None
        while cursor_left < len(left) and left[cursor_left]["line"] == line_left:
            cursor_left += 1
        while cursor_right < len(right) and right[cursor_right]["line"] == line_right:
            cursor_right += 1
        while (
            cursor_left < len(left)
            and cursor_right < len(right)
            and left[cursor_left]["text"] == right[cursor_right]["text"]
        ):
            cursor_left += 1
            cursor_right += 1
    return {"equal": False, "level": "rename-only", "differences": differences}


def _context(tokens: Sequence[dict[str, Any]], index: int, span: int = 8) -> str:
    if index >= len(tokens):
        return "<end of file>"
    line = tokens[index]["line"]
    parts = [t["text"] for t in tokens if t["line"] == line]
    text = " ".join(parts)
    return text if len(text) <= 160 else text[:157] + "..."


# ---------------------------------------------------------------- verdict
def build_verdict(
    *,
    accepted: bool,
    level: str,
    reasons: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Report physical review evidence without choosing the Agent's workflow."""

    issues = []
    for item in reasons:
        issue = {
            key: value for key, value in dict(item).items()
            if key not in {"direction", "target", "example", "nextStep"}
        }
        issue.setdefault("severity", "blocker")
        if issue["severity"] not in {"blocker", "warning"}:
            raise ValueError("review reason severity must be blocker or warning")
        issues.append(issue)
    if accepted and any(item["severity"] == "blocker" for item in issues):
        raise ValueError("blocking review reasons cannot be accepted")
    return {
        "schema": REVIEW_VERDICT_SCHEMA,
        "accepted": bool(accepted),
        "level": str(level),
        "reasons": issues,
    }


def differences_to_reasons(differences: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    reasons_list: list[dict[str, str]] = []
    for item in differences:
        b_line = item.get("baselineLine")
        p_line = item.get("proposalLine")
        reasons_list.append(
            reason(
                "code-changed",
                f"proposal line {p_line} differs from baseline line {b_line}: "
                f"baseline `{item.get('baseline')}` vs proposal `{item.get('proposal')}`",
                location=f"proposal.hlsl:{p_line}",
                severity="warning",
                direction=(
                    "keep the baseline statement byte-for-byte (only identifiers, comments and whitespace "
                    "may change); if this is a rename, declare it through rename_shader_workspace_symbols"
                ),
            )
        )
    return reasons_list


# ---------------------------------------------------------------- mermaid
_MERMAID_STYLE = (
    "  classDef entry fill:#2b6cb0,stroke:#1a365d,color:#ffffff,font-weight:bold;\n"
    "  classDef fn fill:#edf2f7,stroke:#4a5568,color:#1a202c;\n"
)


def mermaid_from_flow_graph(graph: Mapping[str, Any]) -> str:
    """Deterministic, function-only ``flowchart TD`` for publication.

    Stage ranges and statements belong to the separately stored flow-spec review
    evidence. Even a legacy algorithm graph cannot leak those details into the
    published Mermaid artifact.
    """

    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    edges = graph.get("edges") if isinstance(graph, Mapping) else None
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []
    lines = ["flowchart TD"]
    ids: dict[str, str] = {}
    for node in nodes:
        if (
            not isinstance(node, Mapping)
            or node.get("kind") not in ("entry-point", "function", "function-call", "function-summary")
        ):
            continue
        node_id = f"n{len(ids)}"
        ids[str(node.get("id", ""))] = node_id
        name = " ".join(str(node.get("name") or "function").split())[:160]
        description = " ".join(str(node.get("description") or "").split())
        if (
            any(marker in description for marker in (";", "{", "}", "```"))
            or re.search(r"(?<![=!<>])=(?!=)|\b(?:return|discard)\b", description)
        ):
            description = ""
        if len(description) > 200:
            description = description[:197] + "..."
        parts = [name if not description or description == name else f"{name} — {description}"]

        io = node.get("io") if isinstance(node.get("io"), Mapping) else {}
        raw_inputs = node.get("inputs") if isinstance(node.get("inputs"), list) else (
            list(io.get("reads", [])) + list(io.get("resources", []))
        )
        raw_outputs = node.get("outputs") if isinstance(node.get("outputs"), list) else list(io.get("writes", []))

        def interface_text(values: Sequence[Any]) -> str:
            clean: list[str] = []
            for value in values:
                item = " ".join(str(value or "").split())
                if (
                    not item
                    or len(item) > 128
                    or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", item) is None
                    or item in clean
                ):
                    continue
                clean.append(item)
                if len(clean) == 6:
                    break
            return ", ".join(clean)

        inputs = interface_text(raw_inputs)
        outputs = interface_text(raw_outputs)
        interface_known = (isinstance(node.get("io"), Mapping) or
                           isinstance(node.get("inputs"), list) and
                           isinstance(node.get("outputs"), list))
        parts.append("in: " + (inputs or ("—" if interface_known else "?")))
        parts.append("out: " + (outputs or ("—" if interface_known else "?")))
        label = _mermaid_escape(" | ".join(parts))
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} {'entry' if node.get('kind') == 'entry-point' else 'fn'};")
    for edge in edges:
        if not isinstance(edge, Mapping) or edge.get("kind", "calls") != "calls":
            continue
        source = ids.get(str(edge.get("source", "")))
        target = ids.get(str(edge.get("target", "")))
        if source and target:
            lines.append(f"  {source} --> {target}")
    if len(lines) == 1:
        entry = str(graph.get("entryPoint") or "main") if isinstance(graph, Mapping) else "main"
        lines.append(f'  n0["{_mermaid_escape(entry)}"]')
        lines.append("  class n0 entry;")
    return "\n".join(lines) + "\n" + _MERMAID_STYLE


def _mermaid_escape(text: str) -> str:
    return text.replace("\\", "/").replace('"', "'").replace("<", "‹").replace(">", "›").replace("\n", " ")


# -------------------------------------------------------------- flow spec
FLOW_SPEC_SCHEMA = "replaylab-shader-flow-spec/v1"
_FLOW_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FLOW_TEXT = re.compile(r'^[^"<>`\n\r\t]{1,72}$')
_DECL_RESOURCE = re.compile(
    r"^\s*(?P<type>(?:RW)?Texture\w*|(?:RW)?(?:Structured|ByteAddress|Append|Consume)?Buffer\w*|"
    r"SamplerState|SamplerComparisonState|ConstantBuffer|TextureBuffer)\s*(?:<[^>\n]*>)?\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[[^\]]*\])?\s*:\s*register",
    re.MULTILINE,
)
_DECL_CBUFFER = re.compile(r"^\s*cbuffer\s+(?P<name>[A-Za-z_]\w*)", re.MULTILINE)
_OUTPUT_SEMANTIC = re.compile(r":\s*(SV_Target\d*|SV_Depth\w*|SV_Position|SV_Coverage|SV_StencilRef)\b")
MAX_FLOW_STAGES = 40


def declared_symbols(source: str) -> dict[str, list[str]]:
    """Resources, cbuffers and output semantics declared in the HLSL (for templates and validation)."""

    from .source_language import glsl_declarations, source_language
    if source_language(source) == "glsl":
        return glsl_declarations(source)
    source = _source_without_comments(source)
    resources = []
    for match in _DECL_RESOURCE.finditer(source):
        resources.append(match.group("name"))
    cbuffers = [m.group("name") for m in _DECL_CBUFFER.finditer(source)]
    outputs: list[str] = []
    for match in _OUTPUT_SEMANTIC.finditer(source):
        if match.group(1) not in outputs:
            outputs.append(match.group(1))
    return {"resources": resources, "cbuffers": cbuffers, "outputs": outputs}


def _source_without_comments(source: str) -> str:
    return re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', lambda m: " " * len(m.group()), source, flags=re.DOTALL)


def flow_value_bindings(source: str) -> dict[str, list[str]]:
    """Bound explanatory input/value names to declarations, never model lists."""
    source = _source_without_comments(source)
    declarations = re.compile(r"\b[A-Za-z_]\w*(?:\s*<[^;{}\n]+>)?\s+([A-Za-z_]\w*)\s*(?:\[[^\]\n]*\]\s*)?(?=[:;,=)])")
    values = set(declarations.findall(source))
    fields = set()
    for block in re.finditer(r"\bcbuffer\s+(\w+)[^{]*\{([^{}]*)\}", source, re.DOTALL):
        fields.update(block.group(1) + "." + name for name in declarations.findall(block.group(2)))
    # Explicit struct-member accesses are permitted only when they actually
    # occur in source. A model cannot invent CB.field by naming only CB.
    fields.update(re.findall(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b", source))
    fields.update(".".join(field.split(".")[:2]) for field in list(fields) if field.count(".") > 1)
    return {"inputs": sorted(values), "fields": sorted(fields)}


def flow_spec_template(source: str, functions: Sequence[str], entry_point: str) -> dict[str, Any]:
    """A skeleton the agent fills in: fixed shape, its own stage labels/edges."""

    symbols = declared_symbols(source)
    bindings = flow_value_bindings(source)
    from .flow_graph import computation_ranges, external_value_hints
    ranges = computation_ranges(source)
    by_function = {item["function"]: item for item in ranges}
    stages: list[dict[str, Any]] = []
    # The template is one editable example, not an automatically prescribed
    # node per function/resource. Large helper libraries would swamp the UI.
    ordered = [entry_point] if entry_point in functions or not functions else list(functions[:1])
    for index, function in enumerate(ordered[:MAX_FLOW_STAGES]):
        stages.append(
            {
                "id": f"stage_{index + 1}",
                "label": f"<what {function} computes, <= 72 chars>",
                "function": function,
                "sourceRange": by_function.get(function, {}).get("sourceRange", {"startLine": 1, "endLine": 1}),
                "inferred": True,
                "reads": [],
                "writes": [],
                "next": [f"stage_{index + 2}"] if index + 1 < len(ordered[:MAX_FLOW_STAGES]) else [],
            }
        )
    if not stages:
        stages.append({"id": "stage_1", "label": "<main computation>", "function": entry_point, "reads": [], "writes": [], "next": []})
    return {
        "schema": FLOW_SPEC_SCHEMA,
        "inputs": [],
        "resources": (symbols["resources"] + symbols["cbuffers"])[:32],
        "outputs": symbols["outputs"],
        "stages": stages,
        "rules": (
            "ids: lowercase [a-z0-9_]; labels <= 72 chars, no quotes/angle brackets; every 'next' must be a "
            "stage id later in this list; 'function' and sourceRange identify actual executable calculations, not declarations. "
            "Use a few cohesive stages, not each temporary or resource. reads/writes may name source-local intermediate values; "
            "Function.return denotes a real helper return/call. A shared value must actually occur in each stage. "
            "Use inout/out struct fields naturally; Host connects them only through a unique actual caller argument chain. "
            "At most 40 stages. Input/field lists below are bounded hints, not an exhaustive allowlist: actual local fields "
            "in a chosen source range remain valid and all claims are checked against the full proposal. "
            "Labels describe inferred meaning, not physical evidence or semantic proof."
        ),
        "allowedInputs": external_value_hints(source, entry_point)[:32],
        "allowedFields": bindings["fields"][:32],
        "computationRanges": ranges,
    }


def validate_flow_spec(
    spec: Any, *, functions: Iterable[str], resources: Iterable[str], outputs: Iterable[str],
    inputs: Iterable[str] = (), fields: Iterable[str] = (), source: str | None = None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return (clean spec, reasons). Reasons carry location + direction for the agent."""

    reasons: list[dict[str, str]] = []
    if not isinstance(spec, Mapping):
        return {}, [reason("flow-spec-shape", "flow spec must be a JSON object", location="flow_spec", direction="start from the template in the begin result")]
    allowed_keys = {"schema", "inputs", "resources", "outputs", "stages", "rules", "allowedInputs", "allowedFields", "computationRanges"}
    if spec.get("schema") != FLOW_SPEC_SCHEMA or set(spec) - allowed_keys:
        reasons.append(reason("flow-spec-schema", "Use the exact flowSpecTemplate schema and fields.", location="flow_spec", direction="Remove unknown fields and copy the schema from flowSpecTemplate.", target="flowSpecTemplate.schema", example=FLOW_SPEC_SCHEMA))
    known_functions = set(functions)
    known_resources = set(resources)
    known_outputs = set(outputs)
    known_inputs = set(inputs)
    known_fields = set(fields)
    source_records, parameter_records, effect_memo = None, None, {}
    if source is not None:
        from .flow_graph import _function_records, _unique_records
        source_records = _function_records(source)
        parameter_records = _unique_records(source_records)
    clean: dict[str, Any] = {"schema": FLOW_SPEC_SCHEMA, "inputs": [], "resources": [], "outputs": [], "stages": []}

    def texts(key: str, limit: int) -> list[str]:
        raw = spec.get(key, [])
        if not isinstance(raw, list):
            reasons.append(reason("flow-spec-shape", f"'{key}' must be a list of strings", location=f"flow_spec.{key}", direction="use a JSON array"))
            return []
        if len(raw) > limit:
            reasons.append(reason("flow-spec-size", f"'{key}' exceeds {limit} entries", location="flow_spec." + key, direction="Remove or group minor entries; nothing is silently truncated.", target=str(limit)))
        out: list[str] = []
        for item in raw[:limit]:
            if not isinstance(item, str):
                reasons.append(reason("flow-spec-type", "Names must be strings.", location="flow_spec." + key, direction="Use a declared name as a JSON string.", example='"MainPS"'))
                continue
            text = item.strip()
            if not text:
                reasons.append(reason("flow-spec-text", "Names cannot be empty.", location="flow_spec." + key, direction="Remove the empty entry or use an actual source name.", example='"MainPS"'))
                continue
            if text and _FLOW_TEXT.match(text) and text not in out:
                out.append(text)
            elif text:
                reasons.append(reason("flow-spec-text", f"'{text[:40]}' in '{key}' has forbidden characters or is too long", location=f"flow_spec.{key}", direction="plain text, <= 72 chars, no quotes or angle brackets"))
        return out

    clean["inputs"] = [t for t in texts("inputs", 32) if not t.startswith("<")]
    clean["resources"] = [t for t in texts("resources", 64) if not t.startswith("<")]
    clean["outputs"] = [t for t in texts("outputs", 16) if not t.startswith("<")]
    for key, known in (("inputs", known_inputs), ("resources", known_resources), ("outputs", known_outputs)):
        for item in clean[key]:
            if item not in known:
                reasons.append(reason("flow-spec-" + key, f"'{item}' is not a source-declared {key} name.", location="flow_spec." + key,
                                      direction="Use only names supplied by the source-bound template; do not authorize a name by listing it yourself.",
                                      target=", ".join(sorted(known)[:16]), example=next(iter(sorted(known)), "[]")))
    raw_stages = spec.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        reasons.append(reason("flow-spec-shape", "'stages' must be a non-empty list", location="flow_spec.stages", direction="one stage per meaningful computation step, in execution order"))
        return clean, reasons
    if len(raw_stages) > MAX_FLOW_STAGES:
        reasons.append(reason("flow-spec-size", f"{len(raw_stages)} stages exceed the limit of {MAX_FLOW_STAGES}", location="flow_spec.stages", direction="merge minor steps"))
        raw_stages = raw_stages[:MAX_FLOW_STAGES]
    ids: list[str] = []
    for index, raw in enumerate(raw_stages):
        where = f"flow_spec.stages[{index}]"
        if not isinstance(raw, Mapping):
            reasons.append(reason("flow-spec-shape", "stage must be an object", location=where, direction="{id,label,function,reads,writes,next}"))
            continue
        if set(raw) - {"id", "label", "function", "sourceRange", "inferred", "reads", "writes", "next"} or raw.get("inferred", True) is not True:
            reasons.append(reason("flow-spec-stage-schema", "Stage fields are fixed and explanatory labels must be inferred.", location=where, direction="Use id,label,function,inferred,reads,writes,next; inferred must be true.", example='"inferred": true'))
        if any(not isinstance(raw.get(key), str) for key in ("id", "label", "function")):
            reasons.append(reason("flow-spec-stage-type", "Stage id, label and function must be strings.", location=where, direction="Use the string fields from flowSpecTemplate.", example='"function": "MainPS"'))
        stage_id = str(raw.get("id") or "").strip()
        label = " ".join(str(raw.get("label") or "").split())
        function = str(raw.get("function") or "").strip()
        if not _FLOW_ID.match(stage_id) or stage_id in ids:
            reasons.append(reason("flow-spec-id", f"stage id '{stage_id[:40]}' is missing, invalid or duplicated", location=where, direction="lowercase letters, digits, underscores; unique"))
            continue
        if not label or label.startswith("<") or not _FLOW_TEXT.match(label):
            reasons.append(reason("flow-spec-text", f"stage '{stage_id}' needs a plain label (<= 72 chars, no quotes/angle brackets)", location=where + ".label", direction="describe what this step computes"))
            continue
        if function not in known_functions:
            reasons.append(reason("flow-spec-function", f"stage '{stage_id}' names unknown function '{function[:40]}'", location=where + ".function", direction="use one of: " + ", ".join(sorted(known_functions)[:12])))
            continue
        for key, limit in (("reads", 16), ("writes", 8), ("next", 8)):
            raw_list = raw.get(key, [])
            if not isinstance(raw_list, list) or len(raw_list) > limit or any(not isinstance(x, str) or not x.strip() for x in raw_list):
                reasons.append(reason("flow-spec-stage-list", f"{key} must be an array of at most {limit} non-empty names.", location=where + "." + key, direction="Use a bounded JSON array of source names or stage IDs.", example='["MainPS"]'))
        reads = [str(x).strip() for x in (raw.get("reads") or []) if str(x).strip()][:16] if isinstance(raw.get("reads"), list) else []
        writes = [str(x).strip() for x in (raw.get("writes") or []) if str(x).strip()][:8] if isinstance(raw.get("writes"), list) else []
        for name in reads + writes:
            if not _FLOW_TEXT.match(name):
                reasons.append(reason("flow-spec-text", f"'{name[:40]}' in stage '{stage_id}' has forbidden characters", location=where, direction="plain identifiers"))
        for name in writes:
            if name not in known_outputs and not (source is not None and (name in known_inputs | known_fields or name == function + ".return")):
                reasons.append(reason("flow-spec-output", f"stage '{stage_id}' writes unknown output '{name}'", location=where + ".writes", direction="use a declared output: " + ", ".join(sorted(known_outputs))))
        ids.append(stage_id)
        stage = {"id": stage_id, "label": label, "function": function, "inferred": True, "reads": reads, "writes": writes, "next": [str(x).strip() for x in (raw.get("next") or []) if str(x).strip()][:8] if isinstance(raw.get("next"), list) else []}
        if source is not None:
            from .flow_graph import bind_computation_range, ShaderFlowGraphError
            try:
                bound = bind_computation_range(source, function, raw.get("sourceRange"), records=source_records)
                stage["sourceRange"] = bound["sourceRange"]
                reasons.extend(_stage_value_issues(stage, bound["tokens"], known_outputs, known_functions, where, source,
                                                  parameter_records, effect_memo))
            except ShaderFlowGraphError as error:
                reasons.append(reason("flow-spec-source-range", str(error), location=where + ".sourceRange",
                                      direction="Use a complete calculation span inside this function body; read the current proposal after edits.",
                                      target="Actual executable statements, excluding parameters, resources and uninitialized declarations.",
                                      example='"sourceRange": {"startLine": 20, "endLine": 45}'))
        clean["stages"].append(stage)
    for stage in clean["stages"]:
        for target in stage["next"]:
            if target not in ids:
                reasons.append(reason("flow-spec-edge", f"stage '{stage['id']}' points to unknown stage '{target[:40]}'", location=f"flow_spec.stages[{stage['id']}].next", direction="next must list existing stage ids"))
            elif source is not None and ids.index(target) <= ids.index(stage["id"]):
                reasons.append(reason("flow-spec-order", "Computation graph edges must follow top-down stage order.", location=f"flow_spec.stages[{stage['id']}].next",
                                      direction="Order data preparation before its consumers. Represent loop internals within one computation stage.",
                                      target="An acyclic overview of the algorithm's data flow.", example='"next": ["output"]'))
    known_reads = known_inputs | known_resources
    for stage in clean["stages"]:
        for name in stage["reads"]:
            base = name.split(".", 1)[0]
            if name not in known_reads and name not in known_fields and not (source is not None and name.endswith(".return") and name[:-7] in known_functions):
                reasons.append(reason("flow-spec-read", f"stage '{stage['id']}' reads '{name}' which is not declared in source", location=f"flow_spec.stages[{stage['id']}].reads", direction="Use a source-declared input/resource/field; self-declared names grant no evidence.", target=", ".join(sorted(known_reads)[:12]), example=next(iter(sorted(known_reads)), "[]")))
    return clean, reasons


def _stage_value_issues(stage, tokens, outputs, functions, where, source, records, effect_memo):
    """Check actual lexical occurrences and writes, without inferring math meaning."""
    issues = []
    text = " ".join(tokens)
    from .flow_graph import called_parameter_effects
    called_reads, called_writes = called_parameter_effects(source, tokens, records=records, memo=effect_memo)
    for kind in ("reads", "writes"):
        for name in stage[kind]:
            if name.endswith(".return"):
                function = name[:-7]
                returned = tokens if kind == "writes" else (records.get(function) or {}).get("bodyTokens", [])
                has_value = any(word == "return" and index + 1 < len(returned) and returned[index + 1] != ";" for index, word in enumerate(returned))
                present = has_value and ((function == stage["function"]) if kind == "writes" else function in functions and re.search(r"\b" + re.escape(function) + r"\s*\(", text))
            elif name in outputs:
                present = kind == "writes" and ("return" in tokens or "=" in tokens)
            else:
                pattern = r"(?<![\w])" + r"\s*\.\s*".join(re.escape(part) for part in name.split(".")) + r"(?![\w])"
                if kind == "writes":
                    pattern += r"(?:\s*\[[^;=]*?\]|\s*\.\s*\w+)*\s*(?:[+*/%&|^-]\s*)?=(?!=)"
                present = re.search(pattern, text) or name in (called_reads if kind == "reads" else called_writes)
            if not present:
                issues.append(reason("flow-spec-value-binding", "The claimed %s value '%s' is not supported by this calculation range." % (kind, name),
                                     location=where + "." + kind,
                                     direction="Use exact values computed/read in this span; Function.return must correspond to a real return or call.",
                                     target="Source-bound algorithm edges; listing a name is not evidence.", example='"writes": ["sampledColor"]'))
    return issues


def mermaid_from_flow_spec(spec: Mapping[str, Any]) -> str:
    """Fixed-layout data-flow diagram: Inputs | Resources -> Stages -> Outputs."""

    lines = ["flowchart TD"]
    ids: dict[str, str] = {}
    def node(kind: str, name: str) -> str:
        key = f"{kind}:{name}"
        if key not in ids:
            ids[key] = f"{kind}{len(ids)}"
        return ids[key]
    inputs = list(spec.get("inputs") or [])
    resources = list(spec.get("resources") or [])
    outputs = list(spec.get("outputs") or [])
    stages = list(spec.get("stages") or [])
    if inputs:
        lines.append("  subgraph IN[Inputs]")
        for name in inputs:
            lines.append(f'    {node("in", name)}(["{_mermaid_escape(name)}"])')
        lines.append("  end")
    if resources:
        lines.append("  subgraph RES[Resources]")
        for name in resources:
            lines.append(f'    {node("res", name)}[("{_mermaid_escape(name)}")]')
        lines.append("  end")
    lines.append("  subgraph FLOW[Computation]")
    for stage in stages:
        label = stage["label"] + (f" · {stage['function']}()" if stage.get("function") else "")
        lines.append(f'    {node("st", stage["id"])}["{_mermaid_escape(label)}"]')
    lines.append("  end")
    if outputs:
        lines.append("  subgraph OUT[Outputs]")
        for name in outputs:
            lines.append(f'    {node("out", name)}[["{_mermaid_escape(name)}"]]')
        lines.append("  end")
    for stage in stages:
        target = node("st", stage["id"])
        for name in stage.get("reads", []):
            base = name.split(".", 1)[0]
            if name in inputs:
                lines.append(f'  {node("in", name)} --> {target}')
            elif name in resources or base in resources:
                lines.append(f'  {node("res", name if name in resources else base)} -. "{_mermaid_escape(name)}" .-> {target}')
        for name in stage.get("writes", []):
            if name in outputs:
                lines.append(f'  {target} ==> {node("out", name)}')
        for nxt in stage.get("next", []):
            lines.append(f'  {target} --> {node("st", nxt)}')
    lines.append("  classDef stage fill:#edf2f7,stroke:#4a5568,color:#1a202c;")
    lines.append("  classDef io fill:#ebf8ff,stroke:#2b6cb0,color:#1a365d;")
    lines.append("  classDef res fill:#fffaf0,stroke:#c05621,color:#7b341e;")
    for key, ident in ids.items():
        kind = key.split(":", 1)[0]
        lines.append(f"  class {ident} {'stage' if kind == 'st' else ('res' if kind == 'res' else 'io')};")
    return "\n".join(lines) + "\n"


ANALYSIS_TEMPLATE = (
    "# Shader analysis\n\n"
    "## Overview\n(one paragraph: what this shader is for, which pass/draw uses it)\n\n"
    "## Inputs and resources\n(stage inputs, cbuffers with field meanings, textures/samplers/buffers by register)\n\n"
    "## Computation\n(numbered steps in execution order; name the functions from the flow graph)\n\n"
    "## Outputs\n(render targets / UAVs written, semantics, blend/depth assumptions)\n\n"
    "## Observed facts\n(bullet list; each fact names the tool result it came from)\n\n"
    "## Inferences\n(bullet list; each with a confidence: high / medium / low)\n"
)


__all__ = [
    "FLOW_SPEC_SCHEMA",
    "MAX_FLOW_STAGES",
    "declared_symbols",
    "flow_spec_template",
    "mermaid_from_flow_spec",
    "validate_flow_spec",
    "ANALYSIS_TEMPLATE",
    "EquivalenceError",
    "REVIEW_VERDICT_SCHEMA",
    "apply_renames",
    "build_verdict",
    "code_tokens",
    "differences_to_reasons",
    "insert_function_comments",
    "is_reserved",
    "mermaid_from_flow_graph",
    "reason",
    "rename_only_equivalent",
    "tokenize",
    "MAX_RENAMES_PER_REQUEST",
    "MAX_RENAMES_TOTAL",
    "split_bound_renames",
    "validate_renames",
]
