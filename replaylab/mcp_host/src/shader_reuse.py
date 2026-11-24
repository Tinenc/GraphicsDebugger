"""Deciding whether work done on one capture's Shader applies to another's.

The same game build, captured twice, contains the same Shaders. Nothing in the old
identity said so: ``captureId`` hashed the capture path and RenderDoc's ResourceId,
both of which change between captures. This module supplies physical matching
evidence for locating prior work, in three tiers of decreasing certainty.

**Tier 1 -- the bytes.** ``rawSha256`` of the *captured* Shader. Identical bytes are
the same Shader program across captures, paths and sessions. This is a code-reuse
key, not a material-instance, texture or draw-intent identity. Organized source is a
candidate subject to the existing compile, ABI and apply validation. Note "captured":
never the organized-then-recompiled candidate,
which differs from the original by construction and would match nothing.

**Tier 2 -- the ABI.** ``interfaceHash``, which the symbol scanner already computes
over signatures, resource bindings, samplers, CBuffer layout, thread group size and
topology, and which is already free of names and ResourceIds. When the game is
rebuilt the bytes can change while this does not. Matching physical layout alone
does not establish names, aliases, material semantics or an equivalent body.

**Tier 3 -- the source shape.** For a rebuild that did change the interface, or to
tell two similar Shaders apart, the decompiled baseline HLSL is compared function by
function, with identifiers normalised away so that the same arithmetic under different
generated names still matches. This produces a similarity number and the names of the
functions that did and did not match. It decides nothing: it is what a reader needs to
decide, and a wrong automatic answer here is more expensive than no answer.

Nothing here applies anything. It reports what matches, at which tier, with what
evidence; whether that is enough is the caller's judgement.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "replaylab-shader-reuse/v1"

# Everything a Shader is, that does not depend on which capture it was seen in.
IDENTITY_FIELDS = ("rawSha256", "codeSha256", "interfaceHash", "layoutHash",
                   "stage", "entryPoint")

TIERS = ("bytes", "interface", "source", "none")

# Candidate work supported by physical matching alone. Existing validation still
# governs code application. Semantic parts remain readable in the library, but their
# use needs evidence from the current assets, resource bindings and draw context.
SUGGESTED_REUSE_AT_TIER: dict[str, tuple[str, ...]] = {
    "bytes": ("organizedSource",),
    "interface": (),
    "source": (),
    "none": (),
}

_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|0[xX][0-9a-fA-F]+|\d+\.?\d*[fFuU]?|\S")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FUNCTION_RE = re.compile(
    r"(?P<ret>[A-Za-z_][A-Za-z0-9_]*(?:\s*<[^>{};]*>)?)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}()]*\)\s*(?::\s*[A-Za-z_][A-Za-z0-9_]*\s*)?\{")

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_FUNCTIONS = 4096

# Kept verbatim so that "mul(a, b)" and "mul(x, y)" normalise alike while "mul" and
# "dot" stay different. Anything not in here is a name, and names are what change.
_HLSL_VOCABULARY = frozenset("""
bool bool1 bool2 bool3 bool4 int int1 int2 int3 int4 uint uint1 uint2 uint3 uint4
half float float1 float2 float3 float4 double min16float min10float
float2x2 float3x3 float4x4 float3x4 float4x3 float2x4 float4x2 float2x3 float3x2
matrix vector void struct cbuffer tbuffer register packoffset static const inline
uniform in out inout return if else for while do switch case default break continue
discard true false groupshared precise centroid nointerpolation noperspective sample
linear row_major column_major numthreads unroll loop branch flatten
Texture1D Texture2D Texture3D TextureCube Texture1DArray Texture2DArray
TextureCubeArray Texture2DMS Texture2DMSArray RWTexture1D RWTexture2D RWTexture3D
Buffer RWBuffer ByteAddressBuffer RWByteAddressBuffer StructuredBuffer
RWStructuredBuffer AppendStructuredBuffer ConsumeStructuredBuffer
SamplerState SamplerComparisonState
abs acos all any asdouble asfloat asin asint asuint atan atan2 ceil clamp clip cos
cosh countbits cross ddx ddy ddx_coarse ddy_coarse ddx_fine ddy_fine degrees
determinant distance dot dst exp exp2 f16tof32 f32tof16 faceforward firstbitlow
firstbithigh floor fma fmod frac frexp fwidth isfinite isinf isnan ldexp length lerp
lit log log10 log2 mad max min modf mul noise normalize pow radians rcp reflect
refract reversebits round rsqrt saturate sign sin sincos sinh smoothstep sqrt step
tan tanh transpose trunc
Load Sample SampleLevel SampleBias SampleGrad SampleCmp SampleCmpLevelZero Gather
GatherRed GatherGreen GatherBlue GatherAlpha GetDimensions Store InterlockedAdd
InterlockedMin InterlockedMax InterlockedOr InterlockedAnd InterlockedXor
GroupMemoryBarrierWithGroupSync DeviceMemoryBarrier AllMemoryBarrier
SV_Position SV_Target SV_Target0 SV_Target1 SV_Target2 SV_Target3 SV_Depth
SV_VertexID SV_InstanceID SV_PrimitiveID SV_IsFrontFace SV_DispatchThreadID
SV_GroupID SV_GroupThreadID SV_GroupIndex SV_SampleIndex SV_Coverage
""".split())

# Swizzles are kept verbatim, but only where a swizzle can appear: after a dot. Listing
# them as vocabulary instead treated a variable named "a" or "r" as a keyword, so
# "a * b" and "q * r" -- the same arithmetic under different names -- hashed
# differently, which is exactly the case this is supposed to see through.
_SWIZZLE_RE = re.compile(r"^[xyzwrgba]{1,4}$")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(value: Any) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")))


def shader_identity(shader: Mapping[str, Any]) -> dict[str, str]:
    """The capture-independent facts about one Shader.

    ``rawSha256`` must be the captured Shader's own bytes. A workspace manifest keeps
    the original under ``shader``/``baseline`` and the recompiled candidate elsewhere;
    handing the latter in here produces an identity that matches nothing, which is
    correct but useless, so callers should take care which one they pass.
    """

    identity = {}
    for field in IDENTITY_FIELDS:
        value = str(shader.get(field, "") or "")
        identity[field] = value.lower() if field.endswith(("Sha256", "Hash")) else value
    if not identity["rawSha256"] and not identity["interfaceHash"]:
        raise ValueError(
            "a Shader identity needs rawSha256 or interfaceHash; neither was given")
    return identity


def component_identity(shader: Mapping[str, Any]) -> dict[str, str]:
    """The ABI broken into the parts a reader actually reasons about.

    ``interfaceHash`` is one opaque digest over signatures, bindings, samplers and
    CBuffer layout: it answers "did the whole ABI survive" and nothing else. When it
    does not survive, the useful question is *which part* changed -- a Shader whose
    resource structure is untouched and whose signatures moved is a different problem
    from one whose CBuffer grew a field.

    Each part is computed with the same scanner helpers that produce ``interfaceHash``,
    so the pieces and the whole can never disagree about what they mean. Every input is
    reflection derived from the bytecode: no names, no ResourceIds, nothing that
    changes because the capture was taken again. Verified on three catalogs of one
    .rdc whose ResourceIds were 853, 860 and 12212 and whose signature, binding and
    layout digests were identical.
    """

    from .symbols.scanner import (  # imported here to keep this module import-light
        _layout_material, _normalise_bindings, _normalise_samplers,
        _normalise_signature,
    )

    signatures = {
        "inputs": _normalise_signature(
            shader.get("inputSignature", shader.get("inputs", [])) or []),
        "outputs": _normalise_signature(
            shader.get("outputSignature", shader.get("outputs", [])) or []),
    }
    # "Resource structure": which slots in which spaces hold what kind of thing. This
    # describes physical slots, not the identities of their bound assets.
    bindings = {
        "readOnly": _normalise_bindings(
            shader.get("readOnlyResources", shader.get("resources", [])) or []),
        "readWrite": _normalise_bindings(shader.get("readWriteResources", []) or []),
        "samplers": _normalise_samplers(shader.get("samplers", []) or []),
    }
    layout = _layout_material(shader)
    return {
        "signatureHash": canonical_hash(signatures),
        "bindingHash": canonical_hash(bindings),
        "layoutHash": canonical_hash(layout) if layout else "",
        "signatureCount": str(len(signatures["inputs"]) + len(signatures["outputs"])),
        "bindingCount": str(len(bindings["readOnly"]) + len(bindings["readWrite"])
                            + len(bindings["samplers"])),
    }


def compare_components(left: Mapping[str, Any],
                       right: Mapping[str, Any]) -> dict[str, Any]:
    """Which parts of two Shaders' ABIs agree, part by part."""

    parts = {}
    for name in ("signatureHash", "bindingHash", "layoutHash"):
        a, b = str(left.get(name, "") or ""), str(right.get(name, "") or "")
        parts[name[:-4]] = "same" if (a and a == b) else (
            "unknown" if not (a and b) else "different")
    return {
        "signatures": parts["signature"],
        "resourceBindings": parts["binding"],
        "cbufferLayout": parts["layout"],
        "agreeing": sorted(name for name, state in parts.items() if state == "same"),
    }


def normalize_source(text: str) -> str:
    """Comments and layout removed, so formatting alone never changes a hash."""

    stripped = _COMMENT_RE.sub(" ", str(text or "")[:MAX_SOURCE_BYTES])
    return " ".join(stripped.split())


def _shape_tokens(body: str) -> list[str]:
    """The body as a token stream with every name replaced by its first-use slot.

    A decompiler names temporaries however it likes, and two builds of one Shader
    rarely agree on them. Keywords, types, intrinsics and system semantics stay as
    themselves; everything else becomes ``id0``, ``id1``… in order of appearance, so
    the same arithmetic under different names produces the same stream.
    """

    slots: dict[str, str] = {}
    tokens: list[str] = []
    previous = ""
    for token in _TOKEN_RE.findall(body):
        after_dot = previous == "."
        if (_IDENTIFIER_RE.match(token)
                and token not in _HLSL_VOCABULARY
                and not (after_dot and _SWIZZLE_RE.match(token))):
            if token not in slots:
                slots[token] = "id%d" % len(slots)
            tokens.append(slots[token])
        else:
            tokens.append(token)
        previous = token
    return tokens


def _functions(text: str) -> list[dict[str, Any]]:
    """Top-level function definitions, by brace matching rather than by guesswork."""

    source = _COMMENT_RE.sub(" ", str(text or "")[:MAX_SOURCE_BYTES])
    found: list[dict[str, Any]] = []
    position = 0
    while len(found) < MAX_FUNCTIONS:
        match = _FUNCTION_RE.search(source, position)
        if match is None:
            break
        depth = 0
        index = match.end() - 1
        end = -1
        while index < len(source):
            character = source[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
            index += 1
        if end < 0:
            break
        body = " ".join(source[match.end():end].split())
        found.append({
            "name": match.group("name"),
            "returnType": " ".join(match.group("ret").split()),
            "bodyHash": _sha256(body),
            "shapeHash": _sha256(" ".join(_shape_tokens(body))),
            "tokenCount": len(_TOKEN_RE.findall(body)),
        })
        position = end + 1
    return found


def source_fingerprint(text: str, entry_point: str = "") -> dict[str, Any]:
    """A per-function fingerprint of one baseline HLSL file.

    Two fingerprints of the same Shader built twice will usually share most function
    shapes even when every generated name differs. Two unrelated Shaders share almost
    none. The entry point is called out separately because a matching main function is
    worth more than a matching helper that half the frame's Shaders also contain.
    """

    functions = _functions(text)
    entry = ""
    wanted = str(entry_point or "").strip()
    for function in functions:
        if wanted and function["name"] == wanted:
            entry = function["shapeHash"]
            break
    return {
        "schema": SCHEMA,
        "entryPoint": wanted,
        "entryShapeHash": entry,
        "functionCount": len(functions),
        "functions": functions,
        "shapeHashes": sorted(function["shapeHash"] for function in functions),
        "normalizedSha256": _sha256(normalize_source(text)),
    }


def compare_source(left: Mapping[str, Any],
                   right: Mapping[str, Any]) -> dict[str, Any]:
    """How much two baseline sources have in common, and which parts.

    The number is a Jaccard ratio over function shapes: shared shapes over all shapes
    either side has. It is a description, not a verdict -- 0.8 between two post-process
    passes that share a big helper library means less than 0.5 between two unique
    material Shaders, and only a reader who has seen both can say which case this is.
    """

    left_shapes = list(left.get("shapeHashes", []) or [])
    right_shapes = list(right.get("shapeHashes", []) or [])
    left_set, right_set = set(left_shapes), set(right_shapes)
    union = left_set | right_set
    shared = left_set & right_set
    by_shape: dict[str, str] = {}
    mass: dict[str, int] = {}
    for side in (left, right):
        for function in list(side.get("functions", []) or []):
            shape = str(function.get("shapeHash", ""))
            by_shape.setdefault(shape, str(function.get("name", "")))
            mass[shape] = max(mass.get(shape, 0), int(function.get("tokenCount") or 0))
    # Counting functions treats a one-line accessor and a 900-token lighting model as
    # equals, and a decompiler emits the same handful of accessors into every file: two
    # entirely unrelated Shaders were measured sharing ten of them. Weighting by token
    # count is what separates "they share the boilerplate" from "they are the same
    # Shader", so it is reported next to the plain count rather than instead of it.
    shared_mass = sum(mass.get(shape, 0) for shape in shared)
    union_mass = sum(mass.get(shape, 0) for shape in union)
    entry_left = str(left.get("entryShapeHash", "") or "")
    entry_right = str(right.get("entryShapeHash", "") or "")
    return {
        "schema": SCHEMA,
        "similarity": round(len(shared) / len(union), 4) if union else 0.0,
        "weightedSimilarity": (
            round(shared_mass / union_mass, 4) if union_mass else 0.0),
        "sharedTokenCount": shared_mass,
        "totalTokenCount": union_mass,
        "sharedFunctionCount": len(shared),
        "leftFunctionCount": len(left_shapes),
        "rightFunctionCount": len(right_shapes),
        # The main function is the question most of the time, so it is answered
        # separately rather than being averaged into the ratio.
        "entryPointMatches": bool(entry_left and entry_left == entry_right),
        "sharedFunctionNames": sorted(
            {by_shape[shape] for shape in shared if shape in by_shape})[:64],
        "onlyInLeft": sorted(
            {by_shape[shape] for shape in (left_set - right_set)
             if shape in by_shape})[:64],
    }


def match(current: Mapping[str, Any], candidate: Mapping[str, Any], *,
          current_source: Mapping[str, Any] | None = None,
          candidate_source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Which physical tier these two Shaders match at, and what work to inspect.

    ``bytes`` proves program identity and locates organized-code candidates.
    ``interface`` describes physical compatibility, without establishing names or
    body equivalence. ``source`` supplies a comparison for the reader. No tier alone
    identifies the current material instance, textures or draw intent.
    """

    left = shader_identity(current)
    right = shader_identity(candidate)
    evidence: dict[str, Any] = {"identity": {"current": left, "candidate": right}}
    # Which parts of the ABI agree, whenever both sides carry component digests. It is
    # reported at every tier, including "bytes", because a reader checking why a reuse
    # was offered should not have to take the tier's word for it.
    if any(key.endswith("Hash") for key in current) and any(
            key.endswith("Hash") for key in candidate):
        evidence["components"] = compare_components(current, candidate)

    if left["stage"] and right["stage"] and left["stage"] != right["stage"]:
        return _verdict("none", "the Shaders are for different pipeline stages", evidence)
    if left["rawSha256"] and left["rawSha256"] == right["rawSha256"]:
        return _verdict("bytes", "the captured Shader bytes are identical", evidence)
    if left["codeSha256"] and left["codeSha256"] == right["codeSha256"]:
        evidence["note"] = ("the container differs but the compiled code is identical, "
                            "which is the same Shader with different debug or "
                            "signing metadata")
        return _verdict("bytes", "the compiled code is identical", evidence)
    if left["interfaceHash"] and left["interfaceHash"] == right["interfaceHash"]:
        return _verdict(
            "interface",
            "the bytes differ but the ABI -- signatures, bindings, samplers, CBuffer "
            "layout -- is identical. This does not establish semantic names, aliases, "
            "material identity or equivalent Shader behavior.",
            evidence)
    if current_source and candidate_source:
        comparison = compare_source(current_source, candidate_source)
        evidence["source"] = comparison
        if comparison["sharedFunctionCount"]:
            return _verdict(
                "source",
                "neither the bytes nor the ABI match; %d of %d function shapes are "
                "shared (%.0f%% by weight, and a decompiler's boilerplate accessors "
                "appear in every file) and the entry point %s. Read both before "
                "reusing anything."
                % (comparison["sharedFunctionCount"],
                   max(comparison["leftFunctionCount"], 1),
                   100.0 * float(comparison["weightedSimilarity"]),
                   "matches" if comparison["entryPointMatches"] else "does not match"),
                evidence)
    return _verdict("none", "nothing in these two Shaders matches", evidence)


def _verdict(tier: str, reason: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "tier": tier,
        "reason": reason,
        # A suggestion and the reasoning behind it, not a ruling. The evidence is in the
        # same object precisely so the reader can reach a different conclusion.
        "suggestedReuse": list(SUGGESTED_REUSE_AT_TIER[tier]),
        # Conclusive only within shader-program matching, never material semantics.
        "evidenceIsConclusive": tier == "bytes",
        "evidenceScope": "shader-physical-match",
        "semanticIdentityVerified": False,
        "semanticEvidenceRequired": ["currentMaterialAsset", "currentBoundResources", "currentDrawContext"],
        "decideYourself": (
            "Read `evidence` for physical ABI and source comparisons. Identical Shader "
            "bytes or code can locate organized-source candidates; existing compile, ABI "
            "and apply validation still governs their use. Before reusing semantic names, "
            "aliases, texture identity or draw intents, check the current material asset, "
            "resource bindings and draw context. Shader and ABI hashes alone do not prove "
            "those semantics. Use the available evidence to decide which prior conclusions "
            "apply and state the supporting evidence."),
        "evidence": dict(evidence),
    }


def best_match(current: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]],
               *, current_source: Mapping[str, Any] | None = None,
               limit: int = 8) -> dict[str, Any]:
    """The strongest tier any candidate reaches, with the runners-up kept.

    Every candidate that matched at all is reported, not only the winner: a second
    entry at the same tier usually means the library has duplicates worth merging,
    and hiding it would make that invisible.
    """

    order = {tier: index for index, tier in enumerate(TIERS)}
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        verdict = match(
            current, candidate, current_source=current_source,
            candidate_source=candidate.get("sourceFingerprint"))
        if verdict["tier"] == "none":
            continue
        verdict["candidate"] = {
            key: candidate.get(key) for key in
            ("entryId", "rawSha256", "interfaceHash", "stage", "entryPoint")
            if candidate.get(key)
        }
        results.append(verdict)
    results.sort(key=lambda item: (
        order[item["tier"]],
        -float(item["evidence"].get("source", {}).get("weightedSimilarity", 0.0)),
    ))
    return {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "tier": results[0]["tier"] if results else "none",
        "matches": results[:max(1, int(limit))],
        "matchCount": len(results),
    }


__all__ = [
    "IDENTITY_FIELDS",
    "compare_components",
    "component_identity",
    "SUGGESTED_REUSE_AT_TIER",
    "SCHEMA",
    "TIERS",
    "best_match",
    "canonical_hash",
    "compare_source",
    "match",
    "normalize_source",
    "shader_identity",
    "source_fingerprint",
]
