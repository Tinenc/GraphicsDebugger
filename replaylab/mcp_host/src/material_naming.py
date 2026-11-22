"""Names for a draw's constant-buffer members and textures, taken from its material.

Once a draw has been resolved to a material, the material knows what its numbers
*are*: a cooked ``MaterialInstanceConstant`` carries ``ScalarParameterValues``,
``VectorParameterValues`` and ``TextureParameterValues``, each with the artist's
own parameter name. Matching those values against the constant-buffer floats the
capture holds turns "float _302 at b2 lane 17" into "WetnessIntensity" -- a name
somebody at the studio chose, not one an AI invented.

Three kinds of claim come out of that, and they are never mixed:

``matched``
    The value at this lane equals the value of that parameter, and the value is
    distinctive enough that the equality means something. This is evidence.

``aligned``
    This lane's value agrees with an anchored candidate layout. Array order is
    only a hypothesis: cooking, parent overrides and preshaders can reorder or
    change material parameters. Every proposed lane is checked against its value.

``ordered``
    One anchor only, with the same per-lane value check. The correspondence is
    provisional, not proof of a physical material layout.

Array alignments and matched vector tuples remain provisional candidates. Even
four equal floats do not establish a compiled vector's width: a material float2
can be followed by unrelated scalars with the same values as its unused Z/W.
These candidates carry ``autoApplyEligible: false`` and are withheld from ready
rename and alias rows until independent parameter-to-output evidence is read.
A source member's packoffset establishes its physical location, not the semantic
parameter or its component width.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

SCHEMA = "replaylab-material-naming/v1"
# A lane's value has to be distinctive for equality to be evidence; 0, 1, 0.5 and
# friends are everywhere. This is the same test the asset catalog indexes by.
from .symbols.engines.unreal_package import is_distinctive_value, value_key

MAX_PARAMETERS = 512
MAX_BLOCK_FLOATS = 4096
MAX_PROPOSALS = 512
# Material storage components are not necessarily a compiled GPU vector width.
VECTOR_COMPONENTS = ("R", "G", "B", "A")
# Appended to a name that was read from the layout rather than proved by a value.
# It is a marker, not a naming convention: callers may pass their own.
DEFAULT_PROVISIONAL_SUFFIX = "_u"


class MaterialNamingError(RuntimeError):
    """The material document or the constant-buffer snapshot was not usable."""


PARAMETER_SOURCES = ("unreal", "unity", "generic")


def parse_material_parameters(document: Any, *, source: str = "") -> dict[str, Any]:
    """Pull the parameter list out of whatever the unpacker printed.

    Every engine keeps the same three things -- named scalars, named vectors,
    named texture bindings -- and every unpacker spells them differently. The
    matching downstream does not care which engine it came from, so this is where
    the spellings end: each reader below produces the same three lists.

    **Which reader was right is not decided by a priority order.** All of them are
    run, every one that found anything is reported in ``parameterSourceCandidates``
    with what it found, and the default choice is simply the one that read the most
    -- stated in ``parameterSource``, with ``parameterSourceDecided`` saying whether
    it was a clear win or a tie. Pass ``source=`` to overrule it. A document that
    two readers both parse is a real thing (an export that carries both an Unreal
    block and a plain one, an Agent's hand-assembled hand-off), and quietly taking
    whichever was written first in this file would be this module deciding
    something it has no way to know.

    Readers: Unreal (uex / FModel ``ScalarParameterValues`` &c.), Unity
    (AssetStudio ``m_SavedProperties``), and a plain ``{scalars, vectors,
    textures}`` shape -- which is also how an Agent hands over a material it read
    out of a format nothing here knows.
    """

    readers = {"unreal": _parse_unreal, "unity": _parse_unity, "generic": _parse_generic}
    if isinstance(document, Mapping) and document.get("materialsTruncated") is True:
        raise MaterialNamingError(
            "the reader returned a truncated material inventory; select the exact material "
            "with a scoped reader filter before assigning names")
    wanted = str(source or "").strip().lower()
    if wanted and wanted not in readers:
        raise MaterialNamingError(
            "unknown parameter source %r; pass one of %s, or leave it empty to let the "
            "readers compete" % (source, ", ".join(PARAMETER_SOURCES)))

    # A bundle/package can export several materials. Engine selection cannot
    # decide which object belongs to this draw; require a bounded object read.
    exports = _exports(document)
    if len(exports) > 1:
        material_exports = []
        for export in exports[:MAX_PARAMETERS]:
            for name, reader in readers.items():
                if wanted and wanted != name:
                    continue
                parsed = reader(export)
                if parsed and any(parsed.get(key) for key in ("scalars", "vectors", "textures")):
                    material_exports.append(str(parsed.get("objectName") or "(unnamed)"))
                    break
        if len(material_exports) > 1:
            raise MaterialNamingError(
                "multiple material objects were returned; select one object with the reader's "
                "filter and provide its JSON: " + ", ".join(material_exports[:8]))

    found: list[dict[str, Any]] = []
    for name in PARAMETER_SOURCES:
        if wanted and name != wanted:
            continue
        try:
            parsed = readers[name](document)
        except Exception:  # noqa: BLE001 - a reader that chokes simply did not match
            parsed = None
        if parsed is None:
            continue
        parsed["scalars"] = parsed["scalars"][:MAX_PARAMETERS]
        parsed["vectors"] = parsed["vectors"][:MAX_PARAMETERS]
        parsed["textures"] = parsed["textures"][:MAX_PARAMETERS]
        if parsed["scalars"] or parsed["vectors"] or parsed["textures"]:
            found.append(parsed)

    if not found:
        raise MaterialNamingError(
            "no material parameters found%s: this document carries no Unreal "
            "ScalarParameterValues/VectorParameterValues/TextureParameterValues, no Unity "
            "m_SavedProperties, and no plain {scalars, vectors, textures}. If the unpacker prints "
            "another shape, hand it over as {\"scalars\": {name: value}, \"vectors\": "
            "{name: [r,g,b,a]}, \"textures\": {name: path}}"
            % (" with source=%s" % wanted if wanted else ""))

    def total(item: Mapping[str, Any]) -> int:
        return len(item["scalars"]) + len(item["vectors"]) + len(item["textures"])

    found.sort(key=total, reverse=True)
    chosen = dict(found[0])
    chosen["parameterSourceCandidates"] = [
        {"source": item["parameterSource"], "objectName": item["objectName"],
         "scalars": len(item["scalars"]), "vectors": len(item["vectors"]),
         "textures": len(item["textures"])}
        for item in found]
    if wanted:
        chosen["parameterSourceDecided"] = "you asked for %s" % wanted
    elif len(found) == 1:
        chosen["parameterSourceDecided"] = "only %s read this document" % chosen["parameterSource"]
    elif total(found[0]) > total(found[1]):
        chosen["parameterSourceDecided"] = (
            "%s read %d parameters and %s read %d; the larger reading was taken. Pass source= "
            "if the other is the right one"
            % (found[0]["parameterSource"], total(found[0]),
               found[1]["parameterSource"], total(found[1])))
    else:
        chosen["parameterSourceDecided"] = (
            "%s and %s both read %d parameters -- nothing here can tell which is the material, "
            "so the first was taken. Look at parameterSourceCandidates and pass source="
            % (found[0]["parameterSource"], found[1]["parameterSource"], total(found[0])))
    return chosen


def _exports(document: Any) -> list[Mapping[str, Any]]:
    if isinstance(document, Mapping):
        if isinstance(document.get("exports"), Sequence) and not isinstance(
                document.get("exports"), (str, bytes)):
            return [item for item in document["exports"] if isinstance(item, Mapping)]
        return [document]
    if isinstance(document, Sequence) and not isinstance(document, (str, bytes)):
        return [item for item in document if isinstance(item, Mapping)]
    return []


def _parse_unreal(document: Any) -> dict[str, Any] | None:
    """uex / FModel: the export array, a single export, or {"exports": [...]}"""

    for export in _exports(document):
        properties = export.get("Properties")
        if not isinstance(properties, Mapping):
            properties = export
        if not any(key in properties for key in
                   ("ScalarParameterValues", "VectorParameterValues", "TextureParameterValues")):
            cached = _parse_unreal_cached_defaults(export, properties)
            if cached is not None:
                return cached
            continue
        scalars: list[dict[str, Any]] = []
        vectors: list[dict[str, Any]] = []
        textures: list[dict[str, Any]] = []
        for entry in _as_list(properties.get("ScalarParameterValues")):
            name = _parameter_name(entry)
            value = entry.get("ParameterValue")
            if name and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                scalars.append({"name": name, "value": float(value)})
        for entry in _as_list(properties.get("VectorParameterValues")):
            name = _parameter_name(entry)
            value = entry.get("ParameterValue")
            if not name or not isinstance(value, Mapping):
                continue
            components = _components_of(value)
            if len(components) >= 2:
                vectors.append({"name": name, "value": components})
        for entry in _as_list(properties.get("TextureParameterValues")):
            name = _parameter_name(entry)
            value = entry.get("ParameterValue")
            path = ""
            if isinstance(value, Mapping):
                path = str(value.get("ObjectPath") or value.get("ObjectName") or "")
            elif isinstance(value, str):
                path = value
            if name:
                textures.append({"name": name, "objectPath": path})
        return {
            "objectName": str(export.get("Name") or ""),
            "className": str(export.get("Type") or export.get("Class") or ""),
            "parameterSource": "unreal",
            "scalars": scalars, "vectors": vectors, "textures": textures,
        }
    return None


def _parse_unreal_cached_defaults(export: Mapping[str, Any],
                                  properties: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read UMaterial's named default cache, never infer a GPU layout from it.

    CUE4Parse UMaterialInterface.ParseCachedData pairs RuntimeEntries[0/1/3]
    ParameterInfoSet with ScalarValues/VectorValues/TextureValues. The older
    Parameters wrapper uses ParameterInfos and texture entry 2 instead.
    """
    cached = export.get("CachedExpressionData", properties.get("CachedExpressionData"))
    if not isinstance(cached, Mapping):
        return None
    legacy = isinstance(cached.get("Parameters"), Mapping)
    values = cached["Parameters"] if legacy else cached
    info_key = "ParameterInfos" if legacy else "ParameterInfoSet"
    warnings: list[dict[str, Any]] = []
    scoped: list[dict[str, Any]] = []
    parsed: dict[str, list[dict[str, Any]]] = {"scalars": [], "vectors": [], "textures": []}
    runtime = values.get("RuntimeEntries")

    def entry_at(index: int) -> Mapping[str, Any]:
        if isinstance(runtime, (list, tuple)):
            entry = runtime[index] if index < len(runtime) else None
        else:
            entry = values.get("RuntimeEntries" if index == 0 else "RuntimeEntries[%d]" % index)
        return entry if isinstance(entry, Mapping) else {}

    for kind, value_key_name, entry_index in (("scalars", "ScalarValues", 0),
                                              ("vectors", "VectorValues", 1),
                                              ("textures", "TextureValues", 2 if legacy else 3)):
        raw_values = values.get(value_key_name)
        infos = entry_at(entry_index).get(info_key)
        if raw_values is None and infos is None:
            continue
        if not isinstance(raw_values, (list, tuple)) or not isinstance(infos, (list, tuple)) \
                or len(raw_values) != len(infos):
            warnings.append({"field": value_key_name, "reason": "parameter names and values have different shapes or lengths"})
            continue
        if len(infos) > MAX_PARAMETERS:
            warnings.append({"field": value_key_name, "reason": "parameter group exceeds the bounded reader limit"})
            continue
        for info, raw in zip(infos, raw_values):
            if not isinstance(info, Mapping) or not isinstance(info.get("Name"), str) or not info["Name"].strip():
                warnings.append({"field": value_key_name, "reason": "parameter has no declared name"})
                continue
            parameter = {"name": info["Name"], "parameterInfo": dict(info)}
            if kind == "scalars":
                if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw):
                    warnings.append({"field": value_key_name, "parameter": info["Name"], "reason": "non-finite or non-numeric scalar"})
                    continue
                parameter["value"] = float(raw)
            elif kind == "vectors":
                components = _components_of(raw)
                if len(components) != 4:
                    warnings.append({"field": value_key_name, "parameter": info["Name"], "reason": "incomplete RGBA vector"})
                    continue
                parameter["value"] = components
            else:
                path = (raw.get("ObjectPath") or raw.get("AssetPathName") or "") if isinstance(raw, Mapping) else raw
                if not isinstance(path, str):
                    warnings.append({"field": value_key_name, "parameter": info["Name"], "reason": "texture has no declared object path"})
                    continue
                parameter["objectPath"] = "" if path == "None" else path
            # Layer parameters can repeat a name with different associations.
            # Retain their identity, but do not flatten them into global names.
            if info.get("Association", "GlobalParameter") != "GlobalParameter" or info.get("Index", -1) != -1:
                scoped.append({"kind": kind, **parameter})
            else:
                parsed[kind].append(parameter)
    return {
        "objectName": str(export.get("Name") or ""),
        "className": str(export.get("Type") or export.get("Class") or ""),
        "parameterSource": "unreal", **parsed,
        "parameterStorage": "unreal-cached-expression-defaults",
        "defaultValuesOnly": True, "gpuLayoutKnown": False,
        "scopedParameters": scoped, "parameterReadWarnings": warnings[:32],
    }


def _parse_unity(document: Any) -> dict[str, Any] | None:
    """AssetStudio and friends: m_SavedProperties with m_Floats / m_Colors / m_TexEnvs."""

    for export in _exports(document):
        saved = export.get("m_SavedProperties") or export.get("savedProperties")
        if not isinstance(saved, Mapping):
            continue
        scalars: list[dict[str, Any]] = []
        vectors: list[dict[str, Any]] = []
        textures: list[dict[str, Any]] = []
        for name, value in _pairs(saved.get("m_Floats") or saved.get("floats")):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                scalars.append({"name": name, "value": float(value)})
        for key in ("m_Colors", "colors", "m_Vectors", "vectors"):
            for name, value in _pairs(saved.get(key)):
                components = _components_of(value)
                if len(components) >= 2:
                    vectors.append({"name": name, "value": components})
        for name, value in _pairs(saved.get("m_TexEnvs") or saved.get("texEnvs")):
            path = ""
            reference: dict[str, Any] = {}
            if isinstance(value, Mapping):
                texture = value.get("m_Texture", value.get("texture", value))
                if isinstance(texture, Mapping):
                    # A Unity PPtr is (source serialized file, file ID, path ID),
                    # not a globally unique path ID or an asset filename.
                    file_id = texture.get("m_FileID", texture.get("fileID"))
                    path_id = texture.get("m_PathID", texture.get("pathID"))
                    if isinstance(file_id, int) and not isinstance(file_id, bool) and \
                            isinstance(path_id, int) and not isinstance(path_id, bool):
                        reference = {"fileId": file_id, "pathId": path_id,
                                     "isNull": path_id == 0 or file_id < 0}
                        source_file = export.get("sourceFile") or export.get("serializedFile")
                        if isinstance(source_file, str) and source_file:
                            reference["sourceFile"] = source_file
                    if not reference.get("isNull"):
                        path = str(texture.get("objectPath") or texture.get("path")
                                   or texture.get("Name") or texture.get("m_Name") or "")
                elif isinstance(texture, str):
                    path = str(texture)
            elif isinstance(value, str):
                path = value
            if name:
                entry: dict[str, Any] = {"name": name, "objectPath": path}
                if reference:
                    entry["objectReference"] = reference
                if isinstance(value, Mapping):
                    for source_key, target_key in (("m_Scale", "scale"), ("m_Offset", "offset")):
                        components = _components_of(value.get(source_key))
                        if len(components) == 2:
                            entry[target_key] = components
                textures.append(entry)
        if not (scalars or vectors or textures):
            continue
        return {
            "objectName": str(export.get("m_Name") or export.get("Name") or ""),
            "className": str(export.get("m_Type") or export.get("Type") or "Material"),
            "parameterSource": "unity",
            "scalars": scalars, "vectors": vectors, "textures": textures,
            # Preserve integer storage without comparing its numeric value to a
            # float interpretation of the captured bytes.
            "integerParameters": [{"name": name, "value": value} for name, value in
                                  _pairs(saved.get("m_Ints"))
                                  if isinstance(value, int) and not isinstance(value, bool)],
        }
    return None


def _parse_generic(document: Any) -> dict[str, Any] | None:
    """{scalars, vectors, textures} -- any engine, or an Agent handing one over."""

    for export in _exports(document):
        if not any(key in export for key in ("scalars", "floats", "vectors", "colors", "textures")):
            continue
        scalars: list[dict[str, Any]] = []
        vectors: list[dict[str, Any]] = []
        textures: list[dict[str, Any]] = []
        for key in ("scalars", "floats"):
            for name, value in _pairs(export.get(key)):
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    scalars.append({"name": name, "value": float(value)})
        for key in ("vectors", "colors"):
            for name, value in _pairs(export.get(key)):
                components = _components_of(value)
                if len(components) >= 2:
                    vectors.append({"name": name, "value": components})
        for name, value in _pairs(export.get("textures")):
            if isinstance(value, Mapping):
                path = str(value.get("objectPath") or value.get("path") or value.get("name")
                           or value.get("texture") or "")
            else:
                path = str(value)
            if name:
                texture = {"name": name, "objectPath": path}
                if isinstance(value, Mapping):
                    for key in ("objectReference", "scale", "offset"):
                        if key in value:
                            texture[key] = value[key]
                textures.append(texture)
        if not (scalars or vectors or textures):
            continue
        return {
            "objectName": str(export.get("name") or export.get("objectName") or ""),
            "className": str(export.get("class") or export.get("className") or ""),
            "parameterSource": "generic",
            "scalars": scalars, "vectors": vectors, "textures": textures,
        }
    return None


def _pairs(value: Any) -> list[tuple[str, Any]]:
    """(name, value) out of a mapping, or a list of {first, second} / {Key, Value} / {name, value}."""

    out: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for name, item in list(value.items())[:MAX_PARAMETERS]:
            text = str(name).strip()[:128]
            if text:
                out.append((text, item))
        return out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for entry in list(value)[:MAX_PARAMETERS]:
            if isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[0], str):
                if entry[0].strip():
                    out.append((entry[0].strip()[:128], entry[1]))
                continue
            if not isinstance(entry, Mapping):
                continue
            name = ""
            for key in ("first", "Key", "key", "name", "Name"):
                candidate = entry.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    name = candidate.strip()[:128]
                    break
            if not name:
                continue
            for key in ("second", "Value", "value"):
                if key in entry:
                    out.append((name, entry[key]))
                    break
    return out


def _components_of(value: Any) -> list[float]:
    """RGBA (or XYZW, or a plain list) as floats, stopping at the first gap."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        # Never compact a missing/invalid component into the next physical lane.
        components = []
        for item in value[:4]:
            if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item):
                return []
            components.append(float(item))
        return components
    if not isinstance(value, Mapping):
        return []
    for keys in (VECTOR_COMPONENTS, ("r", "g", "b", "a"), ("X", "Y", "Z", "W"),
                 ("x", "y", "z", "w")):
        components: list[float] = []
        for key in keys:
            if key not in value:
                break
            item = value.get(key)
            if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item):
                return []
            components.append(float(item))
        if len(components) >= 2:
            return components
    return []


def _as_list(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)][:MAX_PARAMETERS]
    return []


def _parameter_name(entry: Mapping[str, Any]) -> str:
    info = entry.get("ParameterInfo")
    if isinstance(info, Mapping):
        name = info.get("Name")
        if isinstance(name, str) and name.strip():
            return name.strip()[:128]
    name = entry.get("ParameterName") or entry.get("Name")
    return str(name).strip()[:128] if isinstance(name, str) and name.strip() else ""


def hlsl_identifier(name: str, taken: Any = ()) -> str:
    """Turn an artist's parameter name into something HLSL will accept.

    Real parameter names are written for people: ``Drop Tilling&Offset``,
    ``SDF Shadow HighLight Intensity Light``, ``Wet Mask``. None of those is an
    identifier. The name itself stays in the proposal -- it is what the material
    calls the thing and what the reader should see in a report -- and this is the
    spelling that can actually be written into the source next to it.
    """

    import re as _re

    cleaned = _re.sub(r"[^0-9A-Za-z_]+", "_", str(name or "")).strip("_")
    if not cleaned:
        cleaned = "param"
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    cleaned = cleaned[:96]
    existing = set(taken or ())
    if cleaned not in existing:
        return cleaned
    for suffix in range(2, 64):
        candidate = "%s_%d" % (cleaned, suffix)
        if candidate not in existing:
            return candidate
    return cleaned


LANES_PER_ROW = 4


def alias_rows(proposals: Sequence[Mapping[str, Any]], *, max_chars: int = 64) -> list[dict[str, Any]]:
    """Group named lanes into the rows a constant-buffer panel can actually label.

    Two different things are being produced from one set of proposals, and they
    have different reach.

    A *rename* changes an identifier in the organized HLSL. It only exists for a
    lane this shader reads, because a cooked shader declares nothing else -- and
    it is what makes the code readable.

    An *alias* is a display name in RenderDoc's constant-buffer panel. It is keyed
    by the block's layout and a byte offset, so it reaches **every** lane of the
    block whether this shader reads it or not, and every other shader and capture
    of the game that binds the same block. That is the one that turns a wall of
    anonymous floats into a material you can read.

    The panel's granularity is the row, not the component: a stripped shader --
    every shipped game -- reflects a block as ``unknown[N]`` placeholders, one row
    per 16 bytes, and a row is what the user can type a name on. So four scalars
    sharing a register share one label, and this composes it from whatever the
    row holds. A row that is one vector says the vector's name; a row of scalars
    lists them.
    """

    rows: dict[tuple[Any, Any, int], list[Mapping[str, Any]]] = {}
    for item in proposals:
        if item.get("autoApplyEligible") is False:
            continue
        row = int(item["lane"]) // LANES_PER_ROW
        rows.setdefault((item.get("bind"), item.get("space"), row), []).append(item)
    out: list[dict[str, Any]] = []
    for (bind, space, row), members in sorted(
            rows.items(), key=lambda entry: (str(entry[0][0]), str(entry[0][1]), entry[0][2])):
        members = sorted(members, key=lambda item: int(item["lane"]))
        names: list[str] = []
        for item in members:
            name = str(item["markedName"])
            if name not in names:
                names.append(name)
        # One vector filling the row is one name, not four repetitions of it.
        label = names[0] if len(names) == 1 else " | ".join(names)
        if len(label) > max_chars:
            label = label[:max_chars - 1].rstrip(" |") + "…"
        out.append({
            "bind": bind, "space": space, "row": row,
            "byteOffset": row * LANES_PER_ROW * 4,
            "alias": label,
            "lanes": [int(item["lane"]) for item in members],
            "parameters": [str(item["name"]) for item in members],
            "provisional": any(bool(item.get("provisional")) for item in members),
            "readByThisShader": any(item.get("workspaceSymbol") for item in members),
        })
    return out


def _distinctive_vector(values: Sequence[float]) -> bool:
    """A complete compound value can be informative when each number is round."""
    if any(is_distinctive_value(value) for value in values):
        return True
    # Cooked UV parameters such as (300, 220, 0, 0) are real evidence as a
    # complete, uniquely placed tuple. All four components must still match
    # below. Uniform fills and basis vectors remain too common to name a lane.
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        return False
    nontrivial = {value_key(value) for value in values
                  if value not in (-1.0, -0.5, 0.0, 0.5, 1.0)}
    return len(nontrivial) >= 2


def propose_member_names(material: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]],
                         *, fill_gaps: bool = True,
                         provisional_suffix: str = DEFAULT_PROVISIONAL_SUFFIX,
                         anchors: Any = (),
                         candidate_lanes: Sequence[int] | None = None,
                         candidate_lanes_by_block: Mapping[str, Sequence[int] | None] | None = None,
                         packings: Sequence[str] | None = None,
                         relations: Sequence[str] | None = None) -> dict[str, Any]:
    """Name constant-buffer lanes from a material's parameters.

    ``blocks`` are the capture's constant buffers: ``{bind, space, floats: [...]}``.
    ``fill_gaps`` extends the anchored lanes across the run they establish; turn it
    off to see only what the values themselves prove. ``anchors`` are the Agent's
    own placements -- ``{"fixes": "cbuffer-lane", "lane": 12, "parameter": "Roughness",
    "why": "..."}`` -- which join the Host's on equal terms.

    The Host does not assume how the cook laid the parameters out. It tries each
    plausible order (:func:`shader_anchors.parameter_layouts`), and reports which
    one the lanes actually agree with; readings that agree wherever they overlap
    are merged, and readings that contradict each other are handed back as a
    choice rather than resolved by picking one.
    """

    from . import shader_anchors

    scalars = list(material.get("scalars") or [])
    vectors = list(material.get("vectors") or [])
    supplied = shader_anchors.normalize_anchors(
        anchors, scalar_names=[item["name"] for item in scalars],
        texture_names=[item["name"] for item in material.get("textures") or []])
    supplied_lanes = [item for item in supplied if item["fixes"] == "cbuffer-lane"]
    chosen_packings = tuple(packings) if packings else shader_anchors.DEFAULT_PACKINGS
    chosen_relations = tuple(relations) if relations else shader_anchors.DEFAULT_RELATIONS
    layouts = shader_anchors.parameter_layouts(material, packings=chosen_packings)

    common_allowed = None if candidate_lanes is None else {int(lane) for lane in candidate_lanes}
    block_candidates: dict[str, Any] = {}
    proposals: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    all_anchors: list[dict[str, Any]] = []
    all_alignments: list[dict[str, Any]] = []
    narrowing_gaps: list[dict[str, Any]] = []
    value_conflicts: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []

    for block in blocks:
        floats = shader_anchors.decode_float_lanes((block.get("floats") or [])[:MAX_BLOCK_FLOATS])
        label = "b%s:s%s" % (block.get("bind"), block.get("space"))
        block_lanes = (candidate_lanes_by_block.get(label)
                       if candidate_lanes_by_block is not None else candidate_lanes)
        allowed = None if block_lanes is None else {int(lane) for lane in block_lanes}
        # Missing/non-finite values cannot anchor or inherit a numeric name.
        # Keep their offsets, so the next finite member stays at its real lane.
        finite_lanes = {lane for lane, value in enumerate(floats) if math.isfinite(value)}
        if len(finite_lanes) != len(floats):
            allowed = finite_lanes if allowed is None else allowed & finite_lanes
        block_candidates[label] = (None if allowed is None else
                                   {"count": len(allowed), "lanes": sorted(allowed)[:64]})
        if not floats:
            # Not every constant block is a buffer somebody can read. OpenGL's
            # default uniforms, D3D12 root constants and Vulkan push constants are
            # values in the command stream, not memory -- there is nothing to
            # compare a material against. Saying which block and why beats the
            # Agent concluding the material was wrong.
            unreadable.append({
                "block": label, "bind": block.get("bind"), "space": block.get("space"),
                "byteSize": block.get("byteSize"),
                "status": block.get("status") or "no values",
                "reason": str(block.get("reason")
                              or "this block carried no readable float values; a block that is "
                                 "not buffer-backed (OpenGL default uniforms, D3D12 root "
                                 "constants, Vulkan push constants) has no memory to read"),
            })
            continue
        # lane -> (parameter, component); component is "" for a scalar.
        taken: dict[int, tuple[str, str]] = {}

        # A uniquely placed tuple is useful correspondence evidence, but is not
        # proof that the compiler emitted all of its material storage components.
        indexes = shader_anchors.lane_indexes(floats, allowed=allowed)
        vector_hits: list[tuple[Mapping[str, Any], int, list[tuple[int, list[str]]]]] = []
        for vector in vectors:
            wanted = vector["value"]
            width = len(wanted)
            if not _distinctive_vector(wanted):
                continue
            per_component = [dict(shader_anchors.lanes_for_value(component, indexes,
                                                                 chosen_relations))
                             for component in wanted]
            hits = []
            for start in range(0, max(0, len(floats) - width + 1)):
                found = [per_component[index].get(start + index) for index in range(width)]
                if any(relation is None for relation in found):
                    continue
                hits.append((start, sorted({relation for relation in found if relation})))
            vector_hits.append((vector, width, hits))
        for vector, width, hits in vector_hits:
            if len(hits) != 1:
                if hits:
                    ambiguous.append({"block": label, "parameter": vector["name"],
                                      "lanes": [start for start, unused in hits[:8]],
                                      "reason": "the full vector occurs at several lanes; equality cannot choose one"})
                continue
            start, how = hits[0]
            overlaps = [other["name"] for other, other_width, other_hits in vector_hits
                        if other is not vector and any(
                            start < other_start + other_width and other_start < start + width
                            for other_start, unused in other_hits)]
            if overlaps:
                ambiguous.append({"block": label, "parameter": vector["name"], "lanes": [start],
                                  "parameters": overlaps, "reason": "several material vectors fit the same captured lanes"})
                continue
            if not any(lane in taken for lane in range(start, start + width)):
                for offset, component in enumerate(VECTOR_COMPONENTS[:width]):
                    taken[start + offset] = (vector["name"], component)
                    proposals.append({
                        "block": label, "bind": block.get("bind"), "space": block.get("space"),
                        "lane": start + offset, "byteOffset": (start + offset) * 4,
                        "parameter": vector["name"], "component": component,
                        "basis": "matched", "kind": "vector", "relations": how,
                        "provisional": True,
                        "evidence": "lane %d..%d equal this parameter's %s%s"
                                    % (start, start + width - 1,
                                       "".join(VECTOR_COMPONENTS[:width]),
                                       "" if how == ["exact"] else
                                       " (through %s)" % ", ".join(how)),
                    })

        # Scalars: a distinctive value that appears exactly once in the block.
        scalar_hits: list[tuple[Mapping[str, Any], list[tuple[int, str]]]] = []
        for scalar in scalars:
            if not is_distinctive_value(scalar["value"]):
                continue
            found = [(lane, relation) for lane, relation
                     in shader_anchors.lanes_for_value(scalar["value"], indexes, chosen_relations)
                     if lane not in taken]
            scalar_hits.append((scalar, found))
        for scalar, found in scalar_hits:
            key = value_key(scalar["value"])
            hits = [lane for lane, unused in found]
            if len(hits) == 1:
                lane, relation = found[0]
                competing = [other["name"] for other, other_hits in scalar_hits
                             if other is not scalar and any(other_lane == lane for other_lane, unused in other_hits)]
                if competing:
                    ambiguous.append({"block": label, "parameter": scalar["name"], "lanes": [lane],
                                      "parameters": competing, "reason": "several material scalars fit the same captured lane"})
                    continue
                taken[lane] = (scalar["name"], "")
                proposals.append({
                    "block": label, "bind": block.get("bind"), "space": block.get("space"),
                    "lane": lane, "byteOffset": lane * 4, "parameter": scalar["name"],
                    "basis": "matched", "kind": "scalar", "relations": [relation],
                    "evidence": "lane %d equals this parameter (%s)%s"
                                % (lane, key, "" if relation == "exact"
                                   else " through %s -- %s"
                                   % (relation,
                                      shader_anchors.TRANSFORM_NOTES.get(
                                          relation.split("+")[0], "a conversion"))),
                })
            elif len(hits) > 1:
                ambiguous.append({
                    "block": label, "parameter": scalar["name"], "value": scalar["value"],
                    "lanes": hits[:8],
                    "reason": "this value appears at %d lanes in the block; which one is this "
                              "parameter cannot be decided by equality alone" % len(hits),
                })

        readings: list[dict[str, Any]] = []
        for layout in layouts:
            entries = list(layout["entries"])
            index_of: dict[tuple[str, str], int] = {}
            for index, entry in enumerate(entries):
                index_of.setdefault((entry["parameter"], entry["component"]), index)
            here: list[dict[str, Any]] = []
            for lane, (name, component) in sorted(taken.items()):
                index = index_of.get((name, component))
                if index is None:
                    continue
                here.append({
                    "kind": "vector" if component else "value", "fixes": "cbuffer-lane",
                    "source": "host", "block": label, "layout": layout["layout"],
                    "lane": lane, "parameter": name, "parameterIndex": index,
                    "offset": lane - int(entries[index]["laneOffset"]), "strength": "strong",
                    "evidence": "lane %d holds this parameter's value%s" % (
                        lane, " (%s component)" % component if component else ""),
                })
            here.extend(shader_anchors.window_anchors(layout, floats, label=label,
                                                      allowed_lanes=allowed,
                                                      relations=chosen_relations))
            for anchor in supplied_lanes:
                if anchor.get("block") and anchor["block"] != label:
                    continue
                index = index_of.get((anchor["parameter"], ""))
                if index is None:
                    continue
                here.append({**anchor, "block": label, "layout": layout["layout"],
                             "parameterIndex": index,
                             "offset": int(anchor["lane"]) - int(entries[index]["laneOffset"])})
            grouped = shader_anchors.alignments(
                here, fixes="cbuffer-lane",
                register_tiebreak=bool(layout.get("startsWithVector")))
            for entry in grouped:
                entry["block"] = label
                entry["layout"] = layout["layout"]
            all_anchors.extend(here)
            all_alignments.extend(grouped)
            if not grouped:
                continue
            if not grouped[0].get("decisive"):
                ambiguous.append({
                    "block": label, "layout": layout["layout"],
                    "reason": "the anchors imply %d different parameter offsets (%s) and none is "
                              "better supported than the next; nothing is named from order until "
                              "one of them is chosen -- pass the one you believe as an anchor"
                              % (len(grouped), ", ".join(str(item["offset"]) for item in grouped[:4])),
                })
                continue
            if not fill_gaps:
                continue
            naming, skipped = _name_from_alignment(label, block, floats, entries, taken,
                                                   grouped[0], allowed, chosen_relations)
            value_conflicts.extend({"block": label, "layout": layout["layout"], **item}
                                   for item in skipped if item.get("reason") == "value-conflict")
            if naming:
                readings.append({"layout": layout, "alignment": grouped[0],
                                 "naming": naming, "skipped": skipped})

        merged, gaps = _merge_readings(readings, ambiguous, label)
        proposals.extend(merged)
        narrowing_gaps.extend(gaps)

    proposals.sort(key=lambda item: (str(item["block"]), int(item["lane"])))
    used: set[str] = set()
    for item in proposals:
        item.setdefault("provisional", item["basis"] != "matched")
        # This matcher reads values, not compiled uniform-expression outputs.
        # Retain useful candidates without presenting speculative semantic width
        # or material-array packing as an automatically publishable name.
        item["autoApplyEligible"] = item["basis"] == "matched" and item["kind"] == "scalar"
        item["semanticWidthVerified"] = False
        item["parameterBindingEvidence"] = (
            "unique-scalar-value" if item["autoApplyEligible"] else
            "unique-vector-value-tuple" if item["basis"] == "matched" else "candidate-array-layout")
        if not item["autoApplyEligible"]:
            item["withheldReason"] = (
                "Matching material values does not prove the compiled parameter's component width "
                "or packing. Read parameter-to-output fields/preshader evidence before applying this candidate.")
        item["name"] = item["parameter"]
        item["markedName"] = (item["parameter"] + provisional_suffix if item["provisional"]
                              else item["parameter"])
        base = item["parameter"] + ("_" + item["component"] if item.get("component") else "")
        item["identifier"] = hlsl_identifier(base, used)
        used.add(item["identifier"])
        item["markedIdentifier"] = hlsl_identifier(
            item["identifier"] + provisional_suffix if item["provisional"] else item["identifier"])
    counts: dict[str, int] = {}
    for item in proposals:
        counts[item["basis"]] = counts.get(item["basis"], 0) + 1
    from .material_parameter_localization import parameter_localization

    return {
        "schema": SCHEMA,
        "material": material.get("objectName", ""),
        "parameterLocalization": parameter_localization(
            material, blocks, relations=chosen_relations, member_candidates=proposals),
        "proposals": proposals[:MAX_PROPOSALS],
        "matchedCount": counts.get("matched", 0),
        "alignedCount": counts.get("aligned", 0),
        "orderedCount": counts.get("ordered", 0),
        "autoApplyCount": sum(item["autoApplyEligible"] for item in proposals[:MAX_PROPOSALS]),
        "withheldCount": sum(not item["autoApplyEligible"] for item in proposals[:MAX_PROPOSALS]),
        "compiledLayoutRead": False,
        "gpuLayoutKnown": False,
        "provisionalSuffix": provisional_suffix,
        "ambiguous": ambiguous[:64],
        "anchors": [shader_anchors._summarize(item) | {"offset": item.get("offset"),
                                                       "block": item.get("block", "")}
                    for item in all_anchors[:64]],
        "alignments": all_alignments[:32],
        "layoutsTried": [{"layout": item["layout"], "packings": item["packings"],
                          "laneSpan": item["laneSpan"]} for item in layouts],
        "relationsTried": list(chosen_relations),
        "candidateLanes": (None if common_allowed is None or candidate_lanes_by_block is not None else
                           {"count": len(common_allowed), "lanes": sorted(common_allowed)[:64]}),
        "candidateLanesByBlock": block_candidates,
        "narrowingGaps": narrowing_gaps[:32],
        "valueConflicts": value_conflicts[:64],
        "blocksUnreadable": unreadable[:16],
        "parameterSource": material.get("parameterSource", ""),
        "parameterCounts": {"scalars": len(scalars), "vectors": len(vectors),
                            "textures": len(material.get("textures") or [])},
        "note": ("names carrying provisional=true agree with a candidate layout and their own "
                 "captured values, but the physical parameter layout remains unverified; "
                 "markedName carries the suffix so the uncertainty reaches the HLSL. "
                 "valueConflicts lists array-order candidates rejected by their actual values. "
                 "'narrowingGaps' are lanes the chosen run wanted that the lane filter had "
                 "removed -- check those before trusting the filter, since two materials that "
                 "share a value at one lane make it look like a frame constant. "
                 "'anchors' is every observation that placed a run and 'alignments' every offset "
                 "they implied -- read them before accepting a run, and send back an anchor of "
                 "your own to settle an undecided one"),
    }


def _name_from_alignment(label: str, block: Mapping[str, Any], floats: Sequence[float],
                         entries: Sequence[Mapping[str, Any]],
                         taken: Mapping[int, tuple[str, str]],
                         alignment: Mapping[str, Any],
                         allowed: set[int] | None = None,
                         relations: Sequence[str] = ()
                         ) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Use array alignment as a hypothesis, checking every proposed lane's value."""

    from . import shader_anchors

    offset = int(alignment["offset"])
    basis = "aligned" if (int(alignment.get("anchorCount") or 0) >= 2
                          or int(alignment.get("independentKinds") or 0) >= 2) else "ordered"
    claimed = set(taken.values())
    named: dict[int, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    indexes = shader_anchors.lane_indexes(floats)
    for entry in entries:
        lane = int(entry["laneOffset"]) + offset
        if lane < 0 or lane >= len(floats) or lane in taken:
            continue
        if allowed is not None and lane not in allowed:
            # The narrowing removed a lane this run wants. That is worth saying:
            # the eliminations are sound in general and wrong in particular --
            # two materials that happen to share a value at one lane make it look
            # like a frame constant -- and a silent gap inside a named run is
            # exactly the kind of miss nobody goes looking for.
            skipped.append({"lane": lane, "parameter": entry["parameter"],
                            "component": entry["component"]})
            continue
        key = (entry["parameter"], entry["component"])
        if key in claimed:
            continue
        matches = dict(shader_anchors.lanes_for_value(
            entry["value"], indexes, relations or shader_anchors.DEFAULT_RELATIONS))
        if lane not in matches:
            skipped.append({"lane": lane, "parameter": entry["parameter"],
                            "component": entry["component"], "reason": "value-conflict",
                            "materialValue": entry["value"], "capturedValue": floats[lane]})
            continue
        named[lane] = {
            "block": label, "bind": block.get("bind"), "space": block.get("space"),
            "lane": lane, "byteOffset": lane * 4, "parameter": entry["parameter"],
            "basis": basis, "kind": "vector" if entry["component"] else "scalar",
            "provisional": True, "value": float(floats[lane]), "relations": [matches[lane]],
            "evidence": ("lane %d agrees with this parameter's value in a candidate %s layout, "
                         "offset %d, supported by %d anchor(s): %s; physical layout is unverified"
                         % (lane, alignment.get("layout", "?"), offset,
                            int(alignment.get("anchorCount") or 0),
                            ", ".join(sorted(alignment.get("kinds") or [])))),
        }
        if entry["component"]:
            named[lane]["component"] = entry["component"]
    return named, skipped


def _merge_readings(readings: Sequence[Mapping[str, Any]], ambiguous: list[dict[str, Any]],
                    label: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Combine the layouts that agree; hand back the ones that do not.

    Two candidate layouts usually describe the same buffer from different starting
    assumptions, so where they name the same lane the same thing they are one
    reading, not two, and the longer one simply reaches further. Where they name a
    lane differently they are rival claims, and if neither is better supported the
    honest result is no name and both readings on the table.
    """

    if not readings:
        return [], []

    def rank(item: Mapping[str, Any]) -> tuple[int, int, int, int]:
        alignment = item["alignment"]
        return (int(alignment.get("strongAnchorCount") or 0),
                int(alignment.get("independentKinds") or 0),
                int(alignment.get("anchorCount") or 0),
                len(item["naming"]))

    ordered = sorted(readings, key=rank, reverse=True)
    winner = ordered[0]
    merged: dict[int, dict[str, Any]] = dict(winner["naming"])
    rejected: list[str] = []
    for other in ordered[1:]:
        # Two readings conflict if they put different parameters on one lane *or*
        # the same parameter on different lanes. The second half is the one that
        # bites: a rival layout can explain the same vectors at the same lanes and
        # then place the scalars somewhere else entirely, and without this it
        # merges in a whole run of wrong names without ever touching a lane the
        # winner claimed.
        where = {item["parameter"] + (item.get("component") or ""): lane
                 for lane, item in merged.items()}
        disagrees = [lane for lane, item in other["naming"].items()
                     if (lane in merged and merged[lane]["parameter"] != item["parameter"])
                     or where.get(item["parameter"] + (item.get("component") or ""), lane) != lane]
        if not disagrees:
            for lane, item in other["naming"].items():
                merged.setdefault(lane, item)
            continue
        if rank(other) == rank(winner):
            ambiguous.append({
                "block": label,
                "reason": "two layouts are equally well supported and disagree about %d lane(s) "
                          "(%s at %s, %s at %s); nothing is named from order until you say which "
                          "layout this block is"
                          % (len(disagrees), winner["layout"]["layout"],
                             winner["alignment"]["offset"], other["layout"]["layout"],
                             other["alignment"]["offset"]),
                "layouts": [winner["layout"]["layout"], other["layout"]["layout"]],
                "disagreeingLanes": sorted(disagrees)[:8],
            })
            return [], []
        rejected.append("%s at offset %s (less supported)" % (other["layout"]["layout"],
                                                              other["alignment"]["offset"]))
    ambiguous.append({
        "block": label, "informational": True,
        "reason": "%d lane(s) named from the %s layout at offset %s, fixed by %d anchor(s) (%s)%s. "
                  "Check the ones you rely on"
                  % (len(merged), winner["layout"]["layout"], winner["alignment"]["offset"],
                     int(winner["alignment"].get("anchorCount") or 0),
                     ", ".join(sorted(winner["alignment"].get("kinds") or [])),
                     "; set aside: " + "; ".join(rejected[:3]) if rejected else ""),
    })
    return [merged[lane] for lane in sorted(merged)], list(winner.get("skipped") or ())


def propose_texture_names(material: Mapping[str, Any], bound: Sequence[Mapping[str, Any]],
                          links: Sequence[Mapping[str, Any]] = (), *,
                          provisional_suffix: str = DEFAULT_PROVISIONAL_SUFFIX,
                          anchors: Any = ()) -> dict[str, Any]:
    """Name slots with individual asset/binding evidence; array order is not binding evidence.

    An Agent may state an explicit content match, but publication is eligible only
    after the claimed resource/bind/space tuple is found exactly once in ``bound``
    and the named asset identifies exactly one material parameter. This keeps the
    semantic decision with the Agent while the Host enforces current physical
    identity and ambiguity checks.
    """

    from . import shader_anchors

    parameters = list(material.get("textures") or [])
    slots = [item for item in bound if isinstance(item, Mapping)]
    supplied = shader_anchors.normalize_anchors(
        anchors, scalar_names=[item["name"] for item in material.get("scalars") or []],
        texture_names=[item["name"] for item in parameters])
    found = shader_anchors.texture_anchors(parameters, links, same_asset=_same_asset,
                                           slot_count=len(slots))
    found.extend(item for item in supplied if item["fixes"] == "texture-slot")
    # A stored reader link is useful context, but an explicit current Agent
    # content-match decision for the same slot/parameter must win the dedupe.
    found.sort(key=lambda item: item.get("contentMatchClaimed") is not True)

    proposals: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    accepted_evidence_kinds = {
        "same-image", "pixel-same-image", "resolved-reference-chain",
    }
    for anchor in found:
        slot = int(anchor["slot"])
        if not 0 <= slot < len(slots):
            continue
        competing = {item["parameter"] for item in found if int(item["slot"]) == slot}
        if len(competing) > 1:
            ambiguous.append({"slot": slot, "parameters": sorted(competing),
                              "reason": "conflicting evidence names several parameters for this slot"})
            continue
        if any(item["slot"] == slot and item["parameter"] == anchor["parameter"]
               for item in proposals):
            continue
        bound_slot = slots[slot]
        current_resource = bound_slot.get("resourceId")
        current_bind = bound_slot.get("bindPoint", bound_slot.get("bind"))
        current_space = bound_slot.get("space", 0)
        claimed_resource = anchor.get("resourceId")
        claimed_bind = anchor.get("bindPoint")
        claimed_space = anchor.get("space")
        evidence_kind = str(anchor.get("evidenceKind") or "").strip().lower()
        asset = str(anchor.get("asset") or "").strip()
        physical_shape_valid = (
            type(claimed_resource) is int and claimed_resource > 0
            and type(claimed_bind) is int and claimed_bind >= 0
            and type(claimed_space) is int and claimed_space >= 0
            and type(current_resource) is int and current_resource > 0
            and type(current_bind) is int and current_bind >= 0
            and type(current_space) is int and current_space >= 0
        )
        physical_match = bool(
            physical_shape_valid
            and (claimed_resource, claimed_bind, claimed_space)
            == (current_resource, current_bind, current_space)
        )
        live_tuple_count = sum(
            1 for item in slots
            if isinstance(item, Mapping)
            and (
                item.get("resourceId"),
                item.get("bindPoint", item.get("bind")),
                item.get("space", 0),
            ) == (claimed_resource, claimed_bind, claimed_space)
        ) if physical_shape_valid else 0
        matching_parameters = [
            item["name"] for item in parameters
            if asset and (
                _same_asset(item.get("objectPath"), asset)
                or _same_asset(item.get("objectReference"), asset)
            )
        ]
        parameter_match = (
            len(matching_parameters) == 1
            and matching_parameters[0] == anchor["parameter"]
        )
        content_match_eligible = bool(
            anchor.get("contentMatchClaimed") is True
            and evidence_kind in accepted_evidence_kinds
            and physical_match
            and live_tuple_count == 1
            and parameter_match
        )
        if isinstance(anchor, dict):
            anchor["contentMatchEligible"] = content_match_eligible

        withheld_reason = ""
        if not content_match_eligible:
            if anchor.get("contentMatchClaimed") is not True:
                withheld_reason = (
                    "This is a provisional observation, not an explicit content match. "
                    "Supply contentMatch=true plus the matched asset, evidenceKind, and the "
                    "current resourceId/bindPoint/space tuple."
                )
            elif evidence_kind not in accepted_evidence_kinds:
                withheld_reason = (
                    "The content match needs evidenceKind same-image, pixel-same-image, or "
                    "resolved-reference-chain."
                )
            elif not physical_shape_valid:
                withheld_reason = (
                    "The content match needs positive integer resourceId and non-negative "
                    "integer bindPoint/space values from the current event."
                )
            elif not physical_match:
                withheld_reason = (
                    "The claimed resourceId/bindPoint/space tuple does not match this slot's "
                    "current live binding."
                )
            elif live_tuple_count != 1:
                withheld_reason = (
                    "The claimed resourceId/bindPoint/space tuple is not unique in the current "
                    "live bindings."
                )
            else:
                withheld_reason = (
                    "The matched asset does not identify exactly this one material texture "
                    "parameter, so the semantic name is ambiguous."
                )
        proposals.append({
            "slot": int(anchor["slot"]), "parameter": anchor["parameter"],
            "asset": asset or next(
                (item.get("objectPath", "") for item in parameters
                 if item["name"] == anchor["parameter"]), ""),
            "resourceId": current_resource,
            "basis": "matched" if (
                anchor.get("source") == "host" or content_match_eligible
            ) else "inferred",
            "anchorKind": anchor["kind"], "evidence": anchor["evidence"],
            "evidenceKind": evidence_kind,
            # Compatibility spelling for current publication manifests. It is
            # evidence provenance, not Host workflow authorization.
            "hostEvidenceKind": evidence_kind,
            "contentMatchEligible": content_match_eligible,
            "withheldReason": withheld_reason,
        })

    grouped = shader_anchors.alignments(found, fixes="texture-slot")
    if grouped and not grouped[0].get("decisive"):
        ambiguous.append({
            "reason": "the linked slots imply different parameter offsets; array order is not a binding layout",
            "offsets": [item["offset"] for item in grouped[:4]],
        })
    proposals.sort(key=lambda item: int(item["slot"]))
    used_names: set[str] = set()
    for item in proposals:
        item["provisional"] = item["basis"] != "matched"
        item["autoApplyEligible"] = bool(item["contentMatchEligible"])
        if not item["autoApplyEligible"]:
            item["withheldReason"] = item.get("withheldReason") or (
                "Texture semantics need an explicit, unambiguous content match that binds "
                "the named asset to the current resourceId/bindPoint/space tuple."
            )
        item["name"] = item["parameter"]
        item["markedName"] = (item["parameter"] + provisional_suffix if item["provisional"]
                              else item["parameter"])
        item["identifier"] = hlsl_identifier(item["parameter"], used_names)
        used_names.add(item["identifier"])
        item["markedIdentifier"] = hlsl_identifier(
            item["identifier"] + provisional_suffix if item["provisional"] else item["identifier"])
        bound_slot = slots[int(item["slot"])] if 0 <= int(item["slot"]) < len(slots) else {}
        item["bindPoint"] = bound_slot.get("bindPoint", bound_slot.get("bind"))
        item["space"] = bound_slot.get("space", 0)
    unnamed = [index for index, unused in enumerate(slots)
               if not any(item["slot"] == index for item in proposals)]
    return {
        "schema": SCHEMA,
        "provisionalSuffix": provisional_suffix,
        "proposals": proposals[:MAX_PROPOSALS],
        "matchedCount": sum(1 for item in proposals if item["basis"] == "matched"),
        "autoApplyCount": sum(1 for item in proposals if item["autoApplyEligible"]),
        "withheldCount": sum(1 for item in proposals if not item["autoApplyEligible"]),
        "alignedCount": sum(1 for item in proposals if item["basis"] == "aligned"),
        "orderedCount": sum(1 for item in proposals if item["basis"] == "ordered"),
        "anchors": [shader_anchors._summarize(item) for item in found[:32]],
        "alignments": grouped[:8],
        "ambiguous": ambiguous,
        "unnamedSlots": unnamed,
        "note": ("slots without individual binding/content evidence remain unnamed; material "
                 "array order and matching texture counts do not establish GPU bindings"
                 if unnamed else ""),
    }


def _same_asset(left: Any, right: Any) -> bool:
    """Compare complete references; shared basenames or unscoped PPtrs are not identity."""

    def path(text: str) -> str:
        import posixpath

        cleaned = str(text or "").replace("\\", "/").strip()
        # Unreal exports may include a type wrapper around an object path.
        if "'" in cleaned and cleaned.endswith("'"):
            cleaned = cleaned.split("'", 1)[1][:-1]
        if "/" not in cleaned:
            return ""  # a display name alone cannot identify an asset
        folder, leaf = cleaned.rsplit("/", 1)
        stem, separator, suffix = leaf.rpartition(".")
        if separator and (suffix.casefold() in {"uasset", "umap", "png", "dds", "tga", "jpg", "jpeg"}
                          or suffix == stem):
            leaf = stem
        return posixpath.normpath(folder + "/" + leaf).casefold()

    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        for entry in (left, right):
            if entry.get("isNull") or not isinstance(entry.get("fileId"), int) or \
                    isinstance(entry.get("fileId"), bool) or entry["fileId"] < 0 or \
                    not isinstance(entry.get("pathId"), int) or \
                    isinstance(entry.get("pathId"), bool) or entry["pathId"] == 0:
                return False
        source = path(str(left.get("sourceFile") or ""))
        return bool(source) and source == path(str(right.get("sourceFile") or "")) and \
            (left["fileId"], left["pathId"]) == (right["fileId"], right["pathId"])
    a, b = path(str(left or "")), path(str(right or ""))
    return bool(a) and a == b


__all__ = [
    "DEFAULT_PROVISIONAL_SUFFIX",
    "PARAMETER_SOURCES",
    "alias_rows",
    "MaterialNamingError",
    "SCHEMA",
    "hlsl_identifier",
    "parse_material_parameters",
    "propose_member_names",
    "propose_texture_names",
]
