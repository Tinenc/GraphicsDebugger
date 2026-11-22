"""Anchors: the observations that place a draw's data against an asset's.

An **anchor** is one observation that fixes a single correspondence -- this lane
is that parameter, this texture slot is that texture parameter -- and an
**alignment** is the offset a group of anchors agrees on, which is what lets the
rest of a run be named. Nothing here decides anything on its own: every anchor is
reported with the observation it rests on and how much it actually constrains, and
the Agent is free to accept one, reject one, or add its own.

The point of having several kinds is that a cooked frame and an unpacked tree
agree in different places depending on the draw, and the kind that survives
translation best is rarely the one that is easiest to compute:

``value``
    A parameter's value equals one lane's, and the value is distinctive enough
    that the equality is not a coincidence. Cheap and strong -- when it exists.
    Most materials are mostly 0, 1 and 0.5, so most of the time it does not.

``window``
    A *run* of lanes equals a *run* of parameters, in order. This is the one that
    reaches the round numbers: `1, 0.5, 0, 1, 0.35` proves nothing element by
    element, but as an ordered window of five it is rare, and rarity is measured
    here rather than assumed -- the same block is searched at every offset and the
    anchor carries how many other offsets did equally well. A window of one
    repeated value is not a pattern and is never an anchor.

``texture``
    A bound texture's pixels were matched to an unpacked texture asset, and the
    material references that asset as a named texture parameter. Pixels survive
    repacking, recompression and engine upgrades, so this is usually the anchor
    that still works when the numbers have all drifted -- and because the asset
    tree knows which material references which texture, one identified texture
    reaches the material, the material reaches its other textures, and their
    order anchors the slots.

``agent``
    The Agent looked at something the Host cannot -- the picture, the mesh, the
    marker path, the code -- and says this lane is that parameter. It enters the
    same machinery with the same weight, because a reader who has compared two
    images is not making a weaker claim than a float comparison.

Anchors that disagree are not an error and are not silently averaged: each offset
they imply becomes its own alignment, ranked, and if the best one is not strictly
better than the next, nothing is named from order and both are handed back. That
turns a dead end into a question the Agent can answer with one more anchor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from .symbols.engines.unreal_package import is_distinctive_value, value_key

SCHEMA = "replaylab-shader-anchors/v1"

MAX_LANES = 4096
MAX_PARAMETERS = 512
MAX_ANCHORS = 256
# A run has to be this long before its order means anything on its own.
DEFAULT_MIN_RUN = 3
# ... and hold this many different values: eight lanes of 1.0 are not a pattern.
DEFAULT_MIN_DISTINCT = 2
# A value occurring at more lanes than this says nothing about where the run
# starts, so it does not get to vote on an offset.
DEFAULT_MAX_REPEATS = 32
DEFAULT_MAX_OFFSETS = 12

FIXES = ("cbuffer-lane", "texture-slot")
KINDS = ("value", "vector", "window", "texture", "agent")


class AnchorError(ValueError):
    """An anchor that could not be read, or that names something the material has not."""


def decode_float_lanes(values: Sequence[Any]) -> list[float]:
    """Keep offsets for null/non-numeric lanes in strict bridge JSON.

    NaN remains internal: matching excludes it and variance reports it as
    unavailable. The bridge's uint lanes retain the original bits.
    """
    return [float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            else math.nan for value in values[:MAX_LANES]]


# ---------------------------------------------------------------------------
# Candidate layouts
# ---------------------------------------------------------------------------

LANES_PER_REGISTER = 4

# How a platform lays a uniform block out. Every one of these is a published
# rule, not a guess -- but which one a given cook used is a fact about the build,
# so the Host computes all of them and reports which the data agrees with.
#
# The entry is the *base alignment in lanes* for a value of that many lanes, plus
# whether the rule also forbids straddling a 16-byte row. For the value shapes a
# material actually exposes -- single floats and float4 colours -- every one of
# these produces the same layout, and the Host says so by merging them into one
# candidate instead of handing back five identical ones. They diverge on float2
# and float3, which is where a rule copied from the wrong platform goes wrong.
PACKING_RULES: dict[str, dict[str, Any]] = {
    "d3d-cbuffer": {
        "alignment": {1: 1, 2: 1, 3: 1, 4: 1},
        "noStraddle": True,
        "description": "HLSL / D3D11 / D3D12 constant buffer, and DXC's default for SPIR-V: "
                       "four-byte alignment, but nothing may cross a 16-byte row",
    },
    "std140": {
        "alignment": {1: 1, 2: 2, 3: 4, 4: 4},
        "noStraddle": False,
        "description": "GLSL std140 uniform block (OpenGL, Vulkan): scalars align to 4 bytes, "
                       "float2 to 8, float3 and float4 to 16",
    },
    "std430": {
        "alignment": {1: 1, 2: 2, 3: 4, 4: 4},
        "noStraddle": False,
        "description": "GLSL std430 storage block: the same alignment as std140 for scalars and "
                       "vectors (they differ only on arrays and structs)",
    },
    "metal": {
        "alignment": {1: 1, 2: 2, 3: 4, 4: 4},
        "noStraddle": False,
        "description": "Metal Shading Language / C alignment: float2 to 8 bytes, float3 and "
                       "float4 to 16",
    },
    "tight": {
        "alignment": {1: 1, 2: 1, 3: 1, 4: 1},
        "noStraddle": False,
        "description": "no padding at all -- an engine that hand-packs its parameters into a "
                       "float4 array and indexes them itself",
    },
}

DEFAULT_PACKINGS = tuple(PACKING_RULES)


def _pack(entries: Sequence[Mapping[str, Any]], rule: Mapping[str, Any],
          start: int = 0) -> list[dict[str, Any]]:
    """Place a run of parameters in a uniform block under one platform's rule.

    Values arrive already expanded to one entry per component, so the entry that
    decides the placement is the first component of each -- the rest follow it.
    The padding this leaves is the whole reason it exists: a model that assumes
    the parameters sit end to end reads every lane after the first vector one slot
    early, and then names the entire run wrong while looking perfectly consistent.
    """

    alignment = rule.get("alignment") or {}
    no_straddle = bool(rule.get("noStraddle"))
    placed: list[dict[str, Any]] = []
    lane = start
    for entry in entries:
        width = int(entry.get("width") or 1)
        first = not entry.get("component") or entry.get("component") == "R"
        if first:
            step = int(alignment.get(width, 1))
            if step > 1 and lane % step:
                lane += step - (lane % step)
            if no_straddle and width > 1 and \
                    (lane % LANES_PER_REGISTER) + width > LANES_PER_REGISTER:
                lane += LANES_PER_REGISTER - (lane % LANES_PER_REGISTER)
        placed.append({**entry, "laneOffset": lane})
        lane += 1
    return placed


def _components(count: int) -> tuple[str, ...]:
    return ("R", "G", "B", "A")[:max(0, min(4, count))]


def parameter_layouts(material: Mapping[str, Any], *,
                      packings: Sequence[str] = DEFAULT_PACKINGS) -> list[dict[str, Any]]:
    """The orders and packings this material's parameters might occupy in a block.

    These are candidates, not a rule about any engine. Two things vary and neither
    is safe to assume.

    *Order*: a cook decides whether the vector parameters come before the scalars,
    and engines differ -- and change between their own versions. Nothing here
    encodes a particular engine's answer.

    *Packing*: every platform pads, and they do not pad identically. D3D's
    constant buffer aligns everything to four bytes but forbids crossing a 16-byte
    row; std140, std430 and Metal align a float2 to eight bytes and a float3 or
    float4 to sixteen. For the shapes a material exposes -- single floats and
    float4 colours -- those rules all agree, so the Host merges them into one
    candidate and says which rules it stands for rather than handing back five
    identical layouts; they diverge on float2 and float3, and a material that has
    one gets a candidate per distinct result.

    Each candidate is searched, ``layoutsTried`` reports them, and the data
    chooses. What the Host never does is pick one and name from it.
    """

    scalars = list(material.get("scalars") or [])[:MAX_PARAMETERS]
    vectors = list(material.get("vectors") or [])[:MAX_PARAMETERS]
    wanted = [name for name in packings if name in PACKING_RULES] or list(DEFAULT_PACKINGS)

    def scalar_entries() -> list[dict[str, Any]]:
        return [{"parameter": item["name"], "value": float(item["value"]), "component": "",
                 "width": 1} for item in scalars]

    def vector_entries() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in vectors:
            values = list(item["value"])
            for component, value in zip(_components(len(values)), values):
                out.append({"parameter": item["name"], "value": float(value),
                            "component": component, "width": len(values)})
        return out

    orders: list[tuple[str, bool, list[dict[str, Any]]]] = []
    if scalars:
        orders.append(("scalars", False, scalar_entries()))
    if vectors and scalars:
        orders.append(("vectors-then-scalars", True, vector_entries() + scalar_entries()))
        orders.append(("scalars-then-vectors", False, scalar_entries() + vector_entries()))
    elif vectors:
        orders.append(("vectors", True, vector_entries()))

    layouts: list[dict[str, Any]] = []
    for order, starts_with_vector, entries in orders:
        # One candidate per *distinct* placement, not per rule: where the rules
        # agree -- which is every material made of floats and float4s -- saying so
        # once is the honest report and five copies is noise.
        by_shape: dict[tuple[int, ...], list[str]] = {}
        placed_by_shape: dict[tuple[int, ...], list[dict[str, Any]]] = {}
        for name in wanted:
            placed = _pack(entries, PACKING_RULES[name])
            shape = tuple(item["laneOffset"] for item in placed)
            by_shape.setdefault(shape, []).append(name)
            placed_by_shape.setdefault(shape, placed)
        for shape, names in by_shape.items():
            placed = placed_by_shape[shape]
            label = order if len(by_shape) == 1 else "%s/%s" % (order, names[0])
            layouts.append({
                "layout": label,
                "order": order,
                "packings": names,
                "description": "%s; placement agreed by %s" % (
                    {"scalars": "the scalar parameters in declaration order",
                     "vectors": "the vector parameters in declaration order",
                     "vectors-then-scalars": "the vectors, then the scalars",
                     "scalars-then-vectors": "the scalars, then the vectors"}[order],
                    ", ".join(names)),
                "startsWithVector": starts_with_vector,
                "entries": placed[:MAX_PARAMETERS * 4],
            })
    for entry in layouts:
        entry["laneSpan"] = (entry["entries"][-1]["laneOffset"] + 1) if entry["entries"] else 0
    return layouts[:12]


# ---------------------------------------------------------------------------
# How a value can differ between the asset and the frame
# ---------------------------------------------------------------------------

def _to_half(value: float) -> float:
    import struct

    try:
        return float(struct.unpack("<e", struct.pack("<e", value))[0])
    except (OverflowError, struct.error):
        return value


def _srgb_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> float:
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (abs(value) ** (1.0 / 2.4)) - 0.055


# Ways the number in the frame can differ from the number in the asset while still
# being the same parameter. Each is a real thing that happens on the way from a
# cooked file to a GPU buffer -- and each is *reported by name* on the anchor it
# produced, because "equal after an sRGB conversion" is a different claim from
# "equal", and which one it was changes what the reader should believe.
VALUE_TRANSFORMS: dict[str, Any] = {
    "exact": lambda value: value,
    "float16": _to_half,
    "srgb-to-linear": _srgb_to_linear,
    "linear-to-srgb": _linear_to_srgb,
    "one-minus": lambda value: 1.0 - value,
}
TRANSFORM_NOTES = {
    "exact": "the same float32",
    "float16": "the same number after a trip through half precision, which is what a mobile or "
               "console buffer stores",
    "srgb-to-linear": "the same colour with the asset's sRGB value converted to linear, which is "
                      "what a renderer uploads",
    "linear-to-srgb": "the same colour converted the other way",
    "one-minus": "one minus the asset's value -- engines disagree about roughness against "
                 "smoothness, gloss against blur",
}
# Defaults: the conversions that happen to a value on its way to the GPU without
# anyone deciding anything. 'one-minus' is a semantic guess about what a parameter
# *means*, so it stays off until asked for; 'near' is on because a float that went
# through a preshader comes back a few ulp away.
DEFAULT_RELATIONS = ("exact", "float16", "near")
RELATIONS = tuple(VALUE_TRANSFORMS) + ("near",)
# 'near' means "within a few ulp", so it buckets on the float32 bit pattern rather
# than on a printed string: a fixed number of decimal places is a tighter window
# for a big number than for a small one, and it splits any pair that happens to
# straddle a bucket edge. Shifting the bit pattern right by six gives a bucket 64
# ulps wide -- about 4e-6 relative, at every magnitude -- and the neighbouring
# buckets are probed too, so nothing is lost at an edge.
_NEAR_ULP_SHIFT = 6


def _near_key(value: float) -> str:
    import struct

    try:
        bits = struct.unpack("<i", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        return "x"
    return str(bits >> _NEAR_ULP_SHIFT)


def _near_neighbours(value: float) -> list[str]:
    import struct

    try:
        bits = struct.unpack("<i", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        return ["x"]
    base = bits >> _NEAR_ULP_SHIFT
    return [str(base), str(base - 1), str(base + 1)]


def lane_indexes(floats: Sequence[float], *, allowed: Any = None) -> dict[str, dict[str, list[int]]]:
    """Lanes indexed by their exact key and by a coarser one, for near matching."""

    exact: dict[str, list[int]] = {}
    near: dict[str, list[int]] = {}
    for lane, value in enumerate(floats):
        if allowed is not None and lane not in allowed:
            continue
        if value is None or not math.isfinite(value):
            continue
        exact.setdefault(value_key(value), []).append(lane)
        near.setdefault(_near_key(value), []).append(lane)
    return {"exact": exact, "near": near}


def parameter_lookups(value: float, relations: Sequence[str]) -> list[tuple[str, str, str]]:
    """(index, key, relation) triples to try for one parameter value."""

    enabled = [name for name in relations if name in RELATIONS] or list(DEFAULT_RELATIONS)
    use_near = "near" in enabled
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name in enabled:
        transform = VALUE_TRANSFORMS.get(name)
        if transform is None:
            continue
        try:
            transformed = float(transform(value))
        except (ValueError, OverflowError, ZeroDivisionError):
            continue
        if transformed != transformed:
            continue
        candidates: list[tuple[str, str, str]] = [("exact", value_key(transformed), name)]
        if use_near:
            label = "near" if name == "exact" else "%s+near" % name
            for key in _near_neighbours(transformed):
                candidates.append(("near", key, label))
        for index, key, label in candidates:
            if (index, key) in seen:
                continue
            seen.add((index, key))
            out.append((index, key, label))
    # Exact first, so a lane that matches outright is never reported as a near miss.
    out.sort(key=lambda item: (0 if item[2] == "exact" else 1, item[2]))
    return out


def lanes_for_value(value: float, indexes: Mapping[str, Mapping[str, list[int]]],
                    relations: Sequence[str]) -> list[tuple[int, str]]:
    """Every lane that could be this parameter, with the relation that says so."""

    found: dict[int, str] = {}
    for index, key, relation in parameter_lookups(value, relations):
        for lane in (indexes.get(index) or {}).get(key, ()):  # type: ignore[union-attr]
            found.setdefault(lane, relation)
    return sorted(found.items())


def implausible_float_lanes(floats: Sequence[float],
                            uints: Sequence[int] = ()) -> dict[str, Any]:
    """Lanes whose bits are not a number a material parameter could be.

    A constant buffer is not all floats. The same 32 bits are an index, a bitfield,
    a packed pair of halves or a handle, and read as a float those come out as a
    denormal, an enormous exponent or a NaN. Such a lane cannot hold a scalar
    parameter, so excluding it shortens the search and makes what survives rarer.
    A lane that is plainly zero stays a candidate: zero is a perfectly good
    parameter value and an empty integer looks the same.
    """

    lanes: list[int] = []
    reasons: dict[str, int] = {}
    for lane, value in enumerate(floats):
        number = math.nan if value is None else float(value)
        magnitude = abs(number)
        why = ""
        if not math.isfinite(number):
            why = "not a number"
        elif 0.0 < magnitude < 1e-30:
            why = "denormal -- these bits are a small integer or a bitfield, not a parameter"
        elif magnitude > 1e20:
            why = "an exponent no material parameter carries"
        if why:
            lanes.append(lane)
            reasons[why] = reasons.get(why, 0) + 1
    return {
        "lanes": lanes,
        "count": len(lanes),
        "reasons": [{"reason": why, "lanes": count} for why, count in sorted(reasons.items())],
        "uintsSeen": len(uints or ()),
    }


# ---------------------------------------------------------------------------
# Window anchors
# ---------------------------------------------------------------------------

def _entry_matches(entries: Sequence[Mapping[str, Any]], floats: Sequence[float],
                   relations: Sequence[str], allowed: Any) -> list[dict[int, str]]:
    """For each parameter, the lanes that could be it and the relation that says so."""

    indexes = lane_indexes(floats, allowed=allowed)
    return [dict(lanes_for_value(float(entry["value"]), indexes, relations))
            for entry in entries]


def _offset_votes(entries: Sequence[Mapping[str, Any]], matches: Sequence[Mapping[int, str]],
                  *, max_repeats: int) -> dict[int, int]:
    """How many (parameter, lane) pairs each candidate offset explains."""

    votes: dict[int, int] = {}
    for entry, lanes in zip(entries, matches):
        if not lanes or len(lanes) > max_repeats:
            continue
        for lane in lanes:
            offset = lane - int(entry["laneOffset"])
            votes[offset] = votes.get(offset, 0) + 1
    return votes


def _runs_at_offset(entries: Sequence[Mapping[str, Any]],
                    matches: Sequence[Mapping[int, str]], offset: int) -> list[dict[str, Any]]:
    """Maximal stretches where consecutive parameters equal consecutive lanes.

    Consecutive means consecutive *lanes*: a padding gap the packing rules left
    behind ends the run, because the lanes on the far side of it are not evidence
    that the near side is placed correctly.
    """

    runs: list[dict[str, Any]] = []
    index = 0
    total = len(entries)
    while index < total:
        if (int(entries[index]["laneOffset"]) + offset) not in matches[index]:
            index += 1
            continue
        start = index
        values: list[str] = []
        relations: set[str] = set()
        previous: int | None = None
        while index < total:
            entry = entries[index]
            lane = int(entry["laneOffset"]) + offset
            if lane not in matches[index]:
                break
            if previous is not None and int(entry["laneOffset"]) != previous + 1:
                break
            values.append(value_key(float(entry["value"])))
            relations.add(matches[index][lane])
            previous = int(entry["laneOffset"])
            index += 1
        runs.append({
            "parameterIndex": start, "lane": int(entries[start]["laneOffset"]) + offset,
            "length": len(values), "distinctValues": len(set(values)),
            "distinctiveValues": sum(1 for key in set(values) if is_distinctive_value(float(key))),
            "relations": sorted(relations),
        })
    return runs


def window_anchors(layout: Mapping[str, Any], floats: Sequence[float], *, label: str = "",
                   min_run: int = DEFAULT_MIN_RUN, min_distinct: int = DEFAULT_MIN_DISTINCT,
                   max_repeats: int = DEFAULT_MAX_REPEATS,
                   max_offsets: int = DEFAULT_MAX_OFFSETS,
                   allowed_lanes: Any = None,
                   relations: Sequence[str] = DEFAULT_RELATIONS) -> list[dict[str, Any]]:
    """Offsets at which a run of this layout's values reappears in the block, in order.

    The rarity of a window is measured, not assumed: the whole block is searched
    at every offset a value pair suggests, and each anchor carries how many other
    offsets produced a run as long. One offset with a long varied run is a strong
    claim; five offsets tied at three lanes is the block telling you its numbers
    are too ordinary to place anything, and the anchors say so.

    ``relations`` decides what counts as "the same value" -- see
    :data:`VALUE_TRANSFORMS`. A run matched through a conversion says which one on
    the anchor, because "equal after sRGB" is a different claim from "equal".
    """

    entries = list(layout.get("entries") or [])[:MAX_PARAMETERS * 4]
    lanes = decode_float_lanes(floats)
    if len(entries) < min_run or len(lanes) < min_run:
        return []
    matches = _entry_matches(entries, lanes, relations, allowed_lanes)

    votes = _offset_votes(entries, matches, max_repeats=max_repeats)
    if not votes:
        return []
    ordered = sorted(votes.items(), key=lambda item: (-item[1], item[0]))[:max_offsets * 4]

    found: list[dict[str, Any]] = []
    for offset, unused in ordered:
        runs = [run for run in _runs_at_offset(entries, matches, offset)
                if run["length"] >= min_run and run["distinctValues"] >= min_distinct]
        if not runs:
            continue
        best = max(runs, key=lambda run: (run["length"], run["distinctValues"],
                                          run["distinctiveValues"]))
        found.append({"offset": offset, "best": best, "runs": runs,
                      "agreeingLanes": sum(run["length"] for run in runs)})
    if not found:
        return []

    found.sort(key=lambda item: (-item["best"]["length"], -item["best"]["distinctValues"],
                                 -item["agreeingLanes"], item["offset"]))
    anchors: list[dict[str, Any]] = []
    for item in found[:max_offsets]:
        best = item["best"]
        competing = sum(1 for other in found
                        if other is not item and other["best"]["length"] >= best["length"])
        exact_only = best["relations"] == ["exact"]
        strength = "strong" if (competing == 0 and best["length"] >= min_run + 1
                                and best["distinctValues"] >= 3) else \
                   "strong" if (competing == 0 and best["distinctiveValues"] >= 1) else "weak"
        if strength == "strong" and not exact_only and best["distinctiveValues"] == 0:
            strength = "weak"
        entry = entries[best["parameterIndex"]]
        anchors.append({
            "kind": "window", "fixes": "cbuffer-lane", "source": "host",
            "block": label, "layout": layout.get("layout", ""),
            "packings": list(layout.get("packings") or ()),
            "offset": item["offset"], "lane": best["lane"],
            "parameterIndex": best["parameterIndex"],
            "parameter": entry["parameter"],
            "length": best["length"], "distinctValues": best["distinctValues"],
            "distinctiveValues": best["distinctiveValues"],
            "relations": best["relations"],
            "agreeingLanes": item["agreeingLanes"], "competingOffsets": competing,
            "registerAligned": item["offset"] % LANES_PER_REGISTER == 0,
            "strength": strength,
            "evidence": (
                "lanes %d..%d hold parameters %d..%d of the %s layout in order "
                "(%d in a row, %d different values, %d of them distinctive%s); "
                "%s"
                % (best["lane"], best["lane"] + best["length"] - 1,
                   best["parameterIndex"], best["parameterIndex"] + best["length"] - 1,
                   layout.get("layout", "?"), best["length"], best["distinctValues"],
                   best["distinctiveValues"],
                   "" if exact_only else "; matched through %s" % ", ".join(best["relations"]),
                   "no other offset in this block produces a run this long"
                   if not competing else
                   "%d other offset(s) in this block produce a run this long, so the order "
                   "alone does not choose between them" % competing)),
        })
    return anchors


# ---------------------------------------------------------------------------
# What the frame itself says about a block, without any asset at all
# ---------------------------------------------------------------------------

def lane_variance(samples: Sequence[Mapping[str, Any]], *,
                  max_samples: int = 16) -> dict[str, Any]:
    """Which lanes of a block change between draws, and which do not.

    This needs no unpacked asset and no material: it is the frame describing its
    own buffer. A constant buffer bound to many draws holds several kinds of thing
    side by side -- view constants that never change, material parameters that
    change when the material does, per-object data that changes every draw -- and
    they occupy different lanes. Comparing the same block across a handful of
    events separates them by behaviour, which is exactly the region a material's
    parameters have to live in.
    """

    rows: list[tuple[Any, list[float]]] = []
    for sample in list(samples)[:max_samples]:
        if not isinstance(sample, Mapping):
            continue
        floats = decode_float_lanes(sample.get("floats") or [])
        if floats:
            rows.append((sample.get("eventId"), floats))
    if len(rows) < 2:
        return {"sampleCount": len(rows), "laneCount": len(rows[0][1]) if rows else 0,
                "lanes": [], "constantLanes": [], "varyingLanes": [], "regions": [],
                "note": "at least two draws are needed before anything can be said to vary"}

    width = min(len(floats) for unused, floats in rows)
    lanes: list[dict[str, Any]] = []
    for lane in range(width):
        available = all(math.isfinite(floats[lane]) for unused, floats in rows)
        keys = [value_key(floats[lane]) for unused, floats in rows if math.isfinite(floats[lane])]
        distinct = sorted(set(keys))
        lanes.append({
            "lane": lane, "byteOffset": lane * 4, "distinctValues": len(distinct),
            "constant": len(distinct) == 1 if available else None,
            "available": available, "values": distinct[:6],
        })
    regions: list[dict[str, Any]] = []
    for entry in lanes:
        if regions and regions[-1]["constant"] == entry["constant"] \
                and regions[-1]["to"] == entry["lane"] - 1:
            regions[-1]["to"] = entry["lane"]
            regions[-1]["lanes"] += 1
            continue
        regions.append({"from": entry["lane"], "to": entry["lane"], "lanes": 1,
                        "constant": entry["constant"]})
    return {
        "sampleCount": len(rows),
        "eventIds": [event for event, unused in rows],
        "laneCount": width,
        "lanes": lanes[:MAX_LANES],
        "constantLanes": [entry["lane"] for entry in lanes if entry["constant"] is True],
        "varyingLanes": [entry["lane"] for entry in lanes if entry["constant"] is False],
        "unavailableLanes": [entry["lane"] for entry in lanes if not entry["available"]],
        "regions": regions[:64],
        "note": ("what a region means is yours to read, and it depends on what these draws "
                 "have in common: across draws of the same material, a lane that changes is "
                 "per-object data and cannot be a material parameter; across draws of "
                 "different materials, a lane that does *not* change is a view or frame "
                 "constant and cannot be one either"),
    }


def material_lane_candidates(same_material: Mapping[str, Any] | None = None,
                             other_material: Mapping[str, Any] | None = None,
                             *, lane_count: int = 0,
                             implausible: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Narrow a block to the lanes a material parameter could actually occupy.

    Three eliminations, each from a different observation and each sound on its
    own: a lane that changes between draws of the *same* material is per-object
    data; a lane that stays the same between draws of *different* materials is a
    frame or view constant; and a lane whose bits are not a number a parameter
    could be is an index, a bitfield or a packed pair, whatever the block calls
    itself. None of them says what the surviving lanes *are* -- only that the
    others are not it. Searching the survivors makes an ordered window far rarer,
    because most of the block's ordinary numbers were never candidates.
    """

    width = int(lane_count or 0)
    for source in (same_material, other_material):
        if isinstance(source, Mapping):
            width = max(width, int(source.get("laneCount") or 0))
    if not width:
        return {"lanes": [], "applied": False,
                "reason": "no comparison draws were given, so every lane stays a candidate"}
    candidates = set(range(width))
    excluded: list[dict[str, Any]] = []
    if isinstance(same_material, Mapping) and int(same_material.get("sampleCount") or 0) >= 2:
        varying = set(int(lane) for lane in same_material.get("varyingLanes") or ())
        if varying:
            candidates -= varying
            excluded.append({
                "lanes": sorted(varying)[:64], "count": len(varying),
                "reason": "changes between draws of the same material, so it is per-object data "
                          "(a transform, an instance value, time) rather than a parameter",
            })
    if isinstance(other_material, Mapping) and int(other_material.get("sampleCount") or 0) >= 2:
        constant = set(int(lane) for lane in other_material.get("constantLanes") or ())
        if constant:
            candidates -= constant
            excluded.append({
                "lanes": sorted(constant)[:64], "count": len(constant),
                "reason": "identical across draws of different materials, so it is a frame or "
                          "view constant rather than something this material sets",
            })
    if isinstance(implausible, Mapping) and implausible.get("lanes"):
        bits = set(int(lane) for lane in implausible["lanes"])
        if bits:
            candidates -= bits
            excluded.append({
                "lanes": sorted(bits)[:64], "count": len(bits),
                "reason": "the bits at this lane are not a number a material parameter could "
                          "be (%s), so the shader reads it as something else"
                          % "; ".join(item["reason"] for item in
                                      (implausible.get("reasons") or [])[:3]),
            })
    return {
        "lanes": sorted(candidates),
        "laneCount": width,
        "excludedCount": width - len(candidates),
        "excluded": excluded,
        "applied": bool(excluded),
        "reason": ("%d of %d lanes remain after removing what the frame itself rules out"
                   % (len(candidates), width)) if excluded else
                  "the comparison draws ruled nothing out, so every lane stays a candidate",
    }


# ---------------------------------------------------------------------------
# Texture anchors
# ---------------------------------------------------------------------------

def texture_anchors(parameters: Sequence[Mapping[str, Any]], links: Sequence[Mapping[str, Any]],
                    *, same_asset: Any, slot_count: int) -> list[dict[str, Any]]:
    """Slots whose bound pixels were matched to a texture this material names."""

    position = {item["name"]: index for index, item in enumerate(parameters)}
    anchors: list[dict[str, Any]] = []
    for link in links or ():
        if not isinstance(link, Mapping):
            continue
        slot = link.get("slot")
        asset = str(link.get("asset") or link.get("assetPath") or "")
        reference = link.get("objectReference")
        if not isinstance(slot, int) or isinstance(slot, bool) or not (asset or reference):
            continue
        if not 0 <= slot < slot_count:
            continue
        names = [item["name"] for item in parameters
                 if ((asset and item.get("objectPath") and same_asset(str(item["objectPath"]), asset))
                     or (reference and item.get("objectReference") and
                         same_asset(item["objectReference"], reference)))]
        # A texture reused by two material parameters cannot establish which
        # semantic parameter a captured slot represents.
        name = names[0] if len(names) == 1 else ""
        if not name or name not in position:
            continue
        anchors.append({
            "kind": "texture", "fixes": "texture-slot", "source": "host",
            "slot": slot, "parameter": name, "parameterIndex": position[name],
            "offset": slot - position[name], "asset": asset, "strength": "strong",
            # A stored resolution/reader receipt can suggest this correspondence,
            # but it does not decide the semantic name for the Agent. The direct
            # Agent anchor path below carries that explicit decision.
            "contentMatchClaimed": False,
            "evidenceKind": str(link.get("hostEvidenceKind") or ""),
            "resourceId": link.get("resourceId"),
            "bindPoint": link.get("bindPoint", link.get("bind")),
            "space": link.get("space", 0),
            "evidence": "a recorded resolution links slot %d to %s, which this material binds "
                        "as %s" % (slot, asset, name),
        })
    return anchors


# ---------------------------------------------------------------------------
# Agent-supplied anchors
# ---------------------------------------------------------------------------

def normalize_anchors(raw: Any, *, scalar_names: Sequence[str],
                      texture_names: Sequence[str]) -> list[dict[str, Any]]:
    """Read anchors the Agent supplies, and refuse the ones that name nothing.

    Agent observations can guide investigation and alignment. A texture anchor
    may additionally describe an explicit content match, but that is only a
    claim here: ``propose_texture_names`` must still re-bind its resource, bind
    point and register space to the current event and reject ambiguity before a
    semantic source/panel name can be published. Every anchor must name a real
    parameter so a typo cannot create an apparent alignment.
    """

    if raw in (None, "", []):
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise AnchorError("anchors must be a list of {fixes, lane|slot, parameter, why}")
    scalar_position = {name: index for index, name in enumerate(scalar_names)}
    texture_position = {name: index for index, name in enumerate(texture_names)}
    out: list[dict[str, Any]] = []
    for item in list(raw)[:MAX_ANCHORS]:
        if not isinstance(item, Mapping):
            raise AnchorError("each anchor is an object: {fixes, lane|slot, parameter, why}")
        fixes = str(item.get("fixes") or ("texture-slot" if "slot" in item else "cbuffer-lane"))
        if fixes not in FIXES:
            raise AnchorError("anchor 'fixes' is %r; expected one of %s" % (fixes, ", ".join(FIXES)))
        parameter = str(item.get("parameter") or "").strip()
        where = item.get("lane") if fixes == "cbuffer-lane" else item.get("slot")
        if not isinstance(where, int) or isinstance(where, bool) or where < 0:
            raise AnchorError("anchor for %r needs a non-negative %s"
                              % (parameter, "lane" if fixes == "cbuffer-lane" else "slot"))
        position = scalar_position if fixes == "cbuffer-lane" else texture_position
        if parameter not in position:
            raise AnchorError(
                "this material has no %s parameter called %r; its %s parameters are: %s"
                % ("scalar" if fixes == "cbuffer-lane" else "texture", parameter,
                   "scalar" if fixes == "cbuffer-lane" else "texture",
                   ", ".join(sorted(position)[:20]) or "(none)"))
        index = position[parameter]
        record: dict[str, Any] = {
            "kind": "agent", "fixes": fixes, "source": "agent", "parameter": parameter,
            "parameterIndex": index, "offset": where - index, "strength": "provisional",
            "evidence": str(item.get("why") or item.get("evidence")
                            or "supplied by the Agent without a stated reason")[:400],
        }
        if fixes == "cbuffer-lane":
            record["lane"] = where
            record["block"] = str(item.get("block") or "")
        else:
            record["slot"] = where
            record["contentMatchClaimed"] = item.get("contentMatch") is True
            record["asset"] = str(item.get("asset") or item.get("assetPath") or "").strip()
            record["evidenceKind"] = str(
                item.get("evidenceKind") or item.get("hostEvidenceKind") or ""
            ).strip().lower()
            record["resourceId"] = item.get("resourceId")
            record["bindPoint"] = item.get("bindPoint", item.get("bind"))
            record["space"] = item.get("space")
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Alignments
# ---------------------------------------------------------------------------

def alignments(anchors: Sequence[Mapping[str, Any]], *, fixes: str,
               register_tiebreak: bool = False) -> list[dict[str, Any]]:
    """Group anchors by the offset they imply, best first.

    ``decisive`` is the whole point of the ranking: it is true only when the top
    group is strictly better supported than the next one. A tie is not resolved by
    picking the lower offset -- it is handed back as a question, because one more
    anchor from the Agent settles it and a guess does not.
    """

    relevant = [anchor for anchor in anchors
                if anchor.get("fixes") == fixes and isinstance(anchor.get("offset"), int)]
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for anchor in relevant:
        grouped.setdefault(int(anchor["offset"]), []).append(anchor)

    def evidence_rank(group: Sequence[Mapping[str, Any]]) -> tuple[int, int, int, int]:
        strong = sum(1 for anchor in group if anchor.get("strength") == "strong")
        kinds = len({str(anchor.get("kind")) for anchor in group})
        reach = max((int(anchor.get("length") or 1) for anchor in group), default=1)
        return (strong, kinds, len(group), reach)

    # A run that begins with a four-component value has to begin on a 16-byte row
    # boundary, because that is how a constant buffer is packed -- so for such a
    # layout an unaligned offset is not merely less likely, it is impossible. That
    # is an argument about memory, not an observation about this draw, so it is
    # opt-in: it says nothing about a run of scalars (they pack into whatever row
    # is open) and nothing at all about texture slots, which are not memory.
    def possible(offset: int) -> bool:
        return not register_tiebreak or offset % LANES_PER_REGISTER == 0

    out: list[dict[str, Any]] = []
    ranked = sorted(grouped.items(),
                    key=lambda item: (0 if possible(item[0]) else 1,
                                      tuple(-value for value in evidence_rank(item[1])),
                                      item[0]))
    for position, (offset, group) in enumerate(ranked):
        strong, kinds, count, reach = evidence_rank(group)
        entry = {
            "offset": offset,
            "anchorCount": count,
            "strongAnchorCount": strong,
            "kinds": sorted({str(anchor.get("kind")) for anchor in group}),
            "independentKinds": kinds,
            "longestRun": reach,
            "registerAligned": offset % LANES_PER_REGISTER == 0,
            "possible": possible(offset),
            "supporting": [_summarize(anchor) for anchor in group][:16],
            "rank": position,
            "decisive": False,
        }
        if not entry["possible"]:
            entry["reason"] = ("this layout starts with a four-component value, which cannot "
                               "begin off a 16-byte register boundary, so the buffer cannot hold "
                               "it at this offset")
        out.append(entry)

    live = [entry for entry in out if entry["possible"]]
    if not live:
        return out
    orphaned = [entry for entry in out
                if not entry["possible"] and entry["strongAnchorCount"] > 0]
    best = evidence_rank(grouped[live[0]["offset"]])
    runner = evidence_rank(grouped[live[1]["offset"]]) if len(live) > 1 else None
    if orphaned:
        # Packing ruled an offset out, but the anchors that pointed at it are still
        # there and this layout cannot explain them. Something is wrong -- the
        # layout, or one of the matches -- and naming over it would bury that.
        live[0]["reason"] = (
            "%d anchor(s) place this run at offset(s) %s, which this layout's packing makes "
            "impossible; that contradiction is not resolved by naming from the offset that "
            "survived. Either this is not the layout, or one of those matches is a coincidence -- "
            "say which with an anchor"
            % (sum(entry["strongAnchorCount"] for entry in orphaned),
               ", ".join(str(entry["offset"]) for entry in orphaned[:4])))
    elif runner is None or best > runner:
        live[0]["decisive"] = True
    else:
        live[0]["reason"] = (
            "this offset is not better supported than offset %d; the anchors do not choose "
            "between them, so nothing is named from order. Look at what each one implies and "
            "pass the one you believe back as an anchor" % live[1]["offset"])
    return out


def _summarize(anchor: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("kind", "source", "strength", "parameter", "parameterIndex", "lane", "slot",
            "length", "distinctValues", "competingOffsets", "layout", "evidence",
            "contentMatchClaimed", "contentMatchEligible", "evidenceKind", "asset",
            "resourceId", "bindPoint", "space")
    return {key: anchor[key] for key in keep if key in anchor}


__all__ = [
    "SCHEMA",
    "AnchorError",
    "DEFAULT_PACKINGS",
    "DEFAULT_RELATIONS",
    "PACKING_RULES",
    "RELATIONS",
    "TRANSFORM_NOTES",
    "alignments",
    "implausible_float_lanes",
    "lane_indexes",
    "lane_variance",
    "lanes_for_value",
    "material_lane_candidates",
    "normalize_anchors",
    "parameter_layouts",
    "texture_anchors",
    "window_anchors",
]
