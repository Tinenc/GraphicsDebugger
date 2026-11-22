"""Light, version-tolerant reader for cooked Unreal packages (.uasset/.uexp).

Reads the package summary, name table, import map and export map — the parts
whose layout has been stable from UE 4.2x through UE 5.5 — and derives:
the object's class, the assets it references (textures, parent materials,
functions, parameter collections) resolved to catalog paths through the
detected content mounts, and the real names the package carries (parameter
names, property names, shader defines).  From the sibling ``.uexp`` it lifts
shader type names and vertex factories by string scanning.

It never parses tagged properties, frozen shader maps or bulk data; those are
provider territory.  Everything here is retrieval evidence, labelled as such.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
import re
import struct
from typing import Any

PACKAGE_TAG = 0x9E2A83C1
PARSER_PACKAGE = "unreal-package-summary/v1"
PARSER_EXPORT = "unreal-export-strings/v1"
MAX_NAMES = 8192
MAX_KEPT_NAMES = 512
MAX_IMPORTS = 16384
MAX_EXPORTS = 4096
MAX_EXPORT_SCAN_BYTES = 2 * 1024 * 1024
MAX_TOKENS = 256

TEXTURE_CLASSES = {
    "Texture2D", "TextureCube", "VolumeTexture", "Texture2DArray", "TextureCubeArray",
    "TextureRenderTarget2D", "TextureRenderTargetCube", "RuntimeVirtualTexture",
    "CurveLinearColorAtlas", "TextureLightProfile", "SparseVolumeTexture",
}
MATERIAL_CLASSES = {"Material", "MaterialInstanceConstant", "MaterialInstanceDynamic", "MaterialInterface"}
FUNCTION_CLASSES = {"MaterialFunction", "MaterialFunctionInstance", "MaterialFunctionMaterialLayer",
                    "MaterialFunctionMaterialLayerBlend"}
COLLECTION_CLASSES = {"MaterialParameterCollection"}
MESH_CLASSES = {"StaticMesh", "SkeletalMesh", "Skeleton", "PhysicsAsset", "AnimSequence", "AnimBlueprintGeneratedClass"}

_NAME_STOP = {
    "None", "Class", "Package", "Object", "Guid", "StateId", "Default", "Struct", "Function",
    "Enum", "Property", "ArrayProperty", "BoolProperty", "ByteProperty", "StructProperty",
    "ObjectProperty", "FloatProperty", "IntProperty", "NameProperty", "StrProperty",
    "SoftObjectProperty", "EnumProperty", "MapProperty", "SetProperty", "TextProperty",
    "Int64Property", "UInt32Property", "DoubleProperty", "ClassProperty", "MulticastDelegateProperty",
    "InterfaceProperty", "LazyObjectProperty", "DelegateProperty", "FieldPathProperty",
    "CoreUObject", "Engine", "Script", "Game", "Content", "Blueprint", "PackageMetaData",
    "AssetImportData", "ThumbnailInfo", "LinearColor", "Vector", "Vector2D", "Vector4",
    "Rotator", "Transform", "Color", "Quat", "Box", "IntPoint", "SoftObjectPath",
}
_SHADER_TYPE = re.compile(
    rb"\b(?:F|T)[A-Z][A-Za-z0-9]{2,80}(?:PS|VS|CS|GS|HS|DS|MS|AS|RGS|CHS_AHS)\b"
    rb"|\bT[A-Z][A-Za-z0-9]{2,60}(?:PS|VS|CS)F[A-Za-z0-9]{2,80}(?:Policy|Factory)\b"
)
_VERTEX_FACTORY = re.compile(rb"\bF[A-Za-z0-9]{2,80}VertexFactory(?:ShaderParameters)?\b")
_DEFINE = re.compile(rb"\b[A-Z][A-Z0-9]{1,40}(?:_[A-Z0-9]{1,40}){1,8}\b")
_LAYOUT_NAME = re.compile(rb"\bF[A-Z][A-Za-z0-9]{3,80}\b")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class UnrealPackageError(ValueError):
    """The bytes are not a readable package summary."""


class _Reader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes, offset: int = 0):
        self.data, self.offset = data, offset

    def _unpack(self, fmt: str) -> Any:
        size = struct.calcsize(fmt)
        if self.offset + size > len(self.data):
            raise UnrealPackageError("package summary is truncated")
        value = struct.unpack_from(fmt, self.data, self.offset)[0]
        self.offset += size
        return value

    def i32(self) -> int:
        return self._unpack("<i")

    def u32(self) -> int:
        return self._unpack("<I")

    def i64(self) -> int:
        return self._unpack("<q")

    def skip(self, count: int) -> None:
        if count < 0 or self.offset + count > len(self.data):
            raise UnrealPackageError("package summary is truncated")
        self.offset += count

    def fstring(self, limit: int = 4096) -> str:
        length = self.i32()
        if length == 0:
            return ""
        if length < 0:
            count = -length
            if count > limit:
                raise UnrealPackageError("package string exceeds its bound")
            raw = self.data[self.offset:self.offset + 2 * count]
            if len(raw) != 2 * count:
                raise UnrealPackageError("package string is truncated")
            self.offset += 2 * count
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        if length > limit:
            raise UnrealPackageError("package string exceeds its bound")
        raw = self.data[self.offset:self.offset + length]
        if len(raw) != length:
            raise UnrealPackageError("package string is truncated")
        self.offset += length
        return raw.decode("utf-8", errors="replace").rstrip("\x00")


def read_summary(data: bytes) -> dict[str, Any]:
    """Parse the fields of FPackageFileSummary needed for names/imports/exports."""

    if len(data) < 64:
        raise UnrealPackageError("package is too small")
    reader = _Reader(data)
    tag = reader.u32()
    if tag != PACKAGE_TAG:
        if tag == 0x9E2A83C1 ^ 0xFFFFFFFF:
            raise UnrealPackageError("byte-swapped package tag (big-endian cook) is not supported")
        raise UnrealPackageError(
            "package tag mismatch (0x%08X): the file is encrypted, still compressed, an IoStore/Zen "
            "package without its .utoc/.ucas context, or not an Unreal package" % tag)
    legacy_version = reader.i32()
    if legacy_version >= 0 or legacy_version < -9:
        raise UnrealPackageError("unsupported legacy package version %d" % legacy_version)
    if legacy_version != -4:
        reader.i32()  # LegacyUE3Version
    version_ue4 = reader.i32()
    version_ue5 = reader.i32() if legacy_version <= -8 else 0
    licensee_version = reader.i32()
    if legacy_version <= -2:
        custom_version_count = reader.i32()
        if custom_version_count < 0 or custom_version_count > 4096:
            raise UnrealPackageError("custom version count is invalid")
        reader.skip(custom_version_count * 20)
    total_header_size = reader.i32()
    folder_name = reader.fstring()
    package_flags = reader.u32()
    name_count = reader.i32()
    name_offset = reader.i32()
    soft_object_paths_count = soft_object_paths_offset = 0
    if version_ue5 >= 1004:  # ADD_SOFTOBJECTPATH_LIST
        soft_object_paths_count = reader.i32()
        soft_object_paths_offset = reader.i32()
    localization_id = ""
    if version_ue4 >= 516 and not (package_flags & 0x80000000):  # PKG_FilterEditorOnly
        localization_id = reader.fstring()
    if version_ue4 >= 459:
        reader.i32()  # GatherableTextDataCount
        reader.i32()  # GatherableTextDataOffset
    export_count = reader.i32()
    export_offset = reader.i32()
    import_count = reader.i32()
    import_offset = reader.i32()
    depends_offset = reader.i32()
    if not (0 <= name_count <= MAX_NAMES) or not (0 <= import_count <= MAX_IMPORTS) or not (0 <= export_count <= MAX_EXPORTS):
        raise UnrealPackageError("package table counts exceed their bounds")
    for offset in (name_offset, import_offset, export_offset):
        if offset < 0 or offset > len(data):
            raise UnrealPackageError("package table offset is outside the file")
    return {
        "legacyVersion": legacy_version,
        "fileVersionUE4": version_ue4,
        "fileVersionUE5": version_ue5,
        "licenseeVersion": licensee_version,
        "totalHeaderSize": total_header_size,
        "folderName": folder_name,
        "packageFlags": package_flags,
        "nameCount": name_count,
        "nameOffset": name_offset,
        "softObjectPathsCount": soft_object_paths_count,
        "softObjectPathsOffset": soft_object_paths_offset,
        "localizationId": localization_id,
        "exportCount": export_count,
        "exportOffset": export_offset,
        "importCount": import_count,
        "importOffset": import_offset,
        "dependsOffset": depends_offset,
    }


def read_names(data: bytes, summary: Mapping[str, Any]) -> list[str]:
    reader = _Reader(data, int(summary["nameOffset"]))
    names = []
    hashed = int(summary["fileVersionUE4"]) >= 504  # NAME_HASHES_SERIALIZED
    for _ in range(int(summary["nameCount"])):
        names.append(reader.fstring(limit=8192))
        if hashed:
            reader.skip(4)
    return names


def _name(names: Sequence[str], index: int) -> str:
    if 0 <= index < len(names):
        return names[index]
    return ""


def read_imports(data: bytes, summary: Mapping[str, Any], names: Sequence[str]) -> list[dict[str, Any]]:
    reader = _Reader(data, int(summary["importOffset"]))
    optional = int(summary["fileVersionUE4"]) >= 520  # bImportOptional (UE 4.26/5.0)
    imports = []
    for _ in range(int(summary["importCount"])):
        class_package = _name(names, reader.i32()); reader.i32()
        class_name = _name(names, reader.i32()); reader.i32()
        outer_index = reader.i32()
        object_name = _name(names, reader.i32()); reader.i32()
        if optional:
            reader.i32()
        imports.append({"classPackage": class_package, "className": class_name,
                        "outerIndex": outer_index, "objectName": object_name})
    return imports


def read_exports(data: bytes, summary: Mapping[str, Any], names: Sequence[str]) -> list[dict[str, Any]]:
    """Read only the leading stable part of each FObjectExport."""

    reader = _Reader(data, int(summary["exportOffset"]))
    exports = []
    for _ in range(int(summary["exportCount"])):
        class_index = reader.i32()
        super_index = reader.i32()
        template_index = reader.i32() if int(summary["fileVersionUE4"]) >= 508 else 0
        outer_index = reader.i32()
        object_name = _name(names, reader.i32()); reader.i32()
        object_flags = reader.u32()
        serial_size = reader.i64()
        serial_offset = reader.i64()
        exports.append({
            "classIndex": class_index, "superIndex": super_index, "templateIndex": template_index,
            "outerIndex": outer_index, "objectName": object_name, "objectFlags": object_flags,
            "serialSize": serial_size, "serialOffset": serial_offset,
        })
        # The remaining per-export fields vary by version; exports are read in
        # order only when the fixed-size remainder can be inferred from the
        # table size, so stop after the first entry otherwise.
        if int(summary["exportCount"]) > 1:
            remainder = _export_remainder(int(summary["fileVersionUE4"]), int(summary["fileVersionUE5"]))
            if remainder is None:
                break
            reader.skip(remainder)
    return exports


def _export_remainder(version_ue4: int, version_ue5: int) -> int | None:
    # bForcedExport, bNotForClient, bNotForServer (3 x int32), PackageGuid (16, removed in UE5 ≥ 1007?)
    # bIsInheritedInstance/PackageFlags(int32), bNotAlwaysLoadedForEditorGame, bIsAsset,
    # bGeneratePublicHash (UE5), FirstExportDependency, SerializationBeforeSerializationDependencies,
    # CreateBeforeSerializationDependencies, SerializationBeforeCreateDependencies,
    # CreateBeforeCreateDependencies (5 x int32), ScriptSerializationStart/EndOffset (UE5 ≥ 1009, 2 x int64)
    if version_ue4 < 507:
        return None
    size = 3 * 4
    if not (version_ue5 >= 1005):  # REMOVE_OBJECT_EXPORT_PACKAGE_GUID
        size += 16
    if version_ue5 >= 1010:  # TRACK_OBJECT_EXPORT_IS_INHERITED
        size += 4
    size += 4  # PackageFlags
    size += 4  # bNotAlwaysLoadedForEditorGame
    size += 4  # bIsAsset
    if version_ue5 >= 1006:  # OPTIONAL_RESOURCES / bGeneratePublicHash
        size += 4
    size += 5 * 4
    if version_ue5 >= 1009:  # SCRIPT_SERIALIZATION_OFFSET
        size += 16
    return size


def object_path(index: int, imports: Sequence[Mapping[str, Any]], exports: Sequence[Mapping[str, Any]], depth: int = 0) -> str:
    """Return ``/Mount/Path/Object`` for an import/export index (0 = none)."""

    if index == 0 or depth > 16:
        return ""
    if index < 0:
        position = -index - 1
        if position >= len(imports):
            return ""
        item = imports[position]
        outer = object_path(int(item["outerIndex"]), imports, exports, depth + 1)
        name = str(item["objectName"])
        if item["className"] == "Package" and not outer:
            return name
        return "%s.%s" % (outer, name) if outer and "." not in outer else ("%s/%s" % (outer, name) if outer else name)
    position = index - 1
    if position >= len(exports):
        return ""
    item = exports[position]
    outer = object_path(int(item["outerIndex"]), imports, exports, depth + 1)
    return "%s.%s" % (outer, item["objectName"]) if outer else str(item["objectName"])


def package_of(index: int, imports: Sequence[Mapping[str, Any]], depth: int = 0) -> str:
    """Return the ``/Mount/Path`` package name owning an import index."""

    if index >= 0 or depth > 16:
        return ""
    position = -index - 1
    if position >= len(imports):
        return ""
    item = imports[position]
    if item["className"] == "Package":
        return str(item["objectName"])
    return package_of(int(item["outerIndex"]), imports, depth + 1)


def detect_mounts(paths: Sequence[PurePosixPath], uprojects: Sequence[PurePosixPath],
                  uplugins: Sequence[PurePosixPath]) -> dict[str, str]:
    """Map ``/Game``, ``/Engine`` and ``/<Plugin>`` mounts to catalog directories."""

    mounts: dict[str, str] = {}
    for project in uprojects:
        content = project.parent / "Content"
        mounts.setdefault("/Game", content.as_posix())
    for plugin in uplugins:
        content = plugin.parent / "Content"
        mounts.setdefault("/" + plugin.stem, content.as_posix())
    for path in paths:
        parts = path.parts
        for position in range(len(parts) - 2):
            if parts[position] == "Engine" and parts[position + 1] == "Content":
                mounts.setdefault("/Engine", PurePosixPath(*parts[: position + 2]).as_posix())
                break
        if "/Game" not in mounts and len(parts) >= 3 and parts[-3] != "Engine" and parts[1] == "Content" and path.suffix.casefold() in {".uasset", ".umap"}:
            # <Project>/Content/... without a shipped .uproject
            mounts.setdefault("/Game", PurePosixPath(parts[0], "Content").as_posix())
    return mounts


def map_object_path(object_name: str, mounts: Mapping[str, str]) -> str:
    """``/Game/A/B/T_X`` → ``<mount>/A/B/T_X.uasset`` (catalog-root relative)."""

    if not object_name.startswith("/"):
        return ""
    parts = object_name.split("/")
    if len(parts) < 3:
        return ""
    mount = "/" + parts[1]
    base = mounts.get(mount)
    if not base:
        return ""
    tail = "/".join(parts[2:])
    if "." in tail:
        tail = tail.split(".", 1)[0]
    if not tail or any(segment in {"", ".", ".."} for segment in tail.split("/")):
        return ""
    return "%s/%s.uasset" % (base, tail)


def _interesting_names(names: Sequence[str]) -> list[str]:
    kept = []
    seen: set[str] = set()
    for name in names:
        if not name or name.startswith("/") or name in _NAME_STOP or name.startswith("Default__"):
            continue
        if not _IDENT.fullmatch(name) or len(name) > 128:
            continue
        if name.endswith("Property") or name.startswith("__"):
            continue
        if name in seen:
            continue
        seen.add(name)
        kept.append(name)
        if len(kept) >= MAX_KEPT_NAMES:
            break
    return kept


def parse_package(data: bytes, relative: str, mounts: Mapping[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(metadata, references)`` for one ``.uasset``/``.umap``."""

    try:
        summary = read_summary(data)
        names = read_names(data, summary)
        imports = read_imports(data, summary, names)
        exports = read_exports(data, summary, names)
    except (UnrealPackageError, struct.error, UnicodeError) as exc:
        return {"engine": "unreal", "parser": PARSER_PACKAGE, "parseStatus": "failed",
                "reason": str(exc)[:256]}, []
    stem = PurePosixPath(relative).stem
    primary = next((item for item in exports if item["objectName"] == stem), exports[0] if exports else None)
    primary_class = ""
    if primary is not None:
        class_index = int(primary["classIndex"])
        if class_index < 0 and -class_index - 1 < len(imports):
            primary_class = str(imports[-class_index - 1]["objectName"])
        elif class_index > 0 and class_index - 1 < len(exports):
            primary_class = str(exports[class_index - 1]["objectName"])
    export_classes = []
    for item in exports:
        class_index = int(item["classIndex"])
        if class_index < 0 and -class_index - 1 < len(imports):
            export_classes.append(str(imports[-class_index - 1]["objectName"]))
    references: list[dict[str, Any]] = []
    textures: list[str] = []
    parents: list[str] = []
    functions: list[str] = []
    collections: list[str] = []
    meshes: list[str] = []
    material_owner = primary_class in MATERIAL_CLASSES or primary_class in FUNCTION_CLASSES
    for item in imports:
        class_name = str(item["className"])
        object_name = str(item["objectName"])
        if object_name.startswith("Default__"):
            continue
        if class_name in TEXTURE_CLASSES:
            relation, bucket = "uses-texture", textures
        elif class_name in MATERIAL_CLASSES:
            # a material instance's parent, or a mesh/blueprint's material slot
            relation, bucket = ("parent-material" if material_owner else "uses-material"), parents
        elif class_name in FUNCTION_CLASSES:
            relation, bucket = "uses-material-function", functions
        elif class_name in COLLECTION_CLASSES:
            relation, bucket = "uses-parameter-collection", collections
        elif class_name in MESH_CLASSES:
            relation, bucket = "references-mesh", meshes
        else:
            continue
        package = package_of(int(item["outerIndex"]), imports)
        full = "%s.%s" % (package, object_name) if package else object_name
        bucket.append(full)
        target = map_object_path(package, mounts) if package else ""
        reference = {
            "relation": relation,
            "catalogRootRelative": True,
            "targetPath": target,
            "evidence": {"objectPath": full, "class": class_name, "engine": "unreal",
                         "mountResolved": bool(target)},
        }
        references.append(reference)
    metadata = {
        "engine": "unreal",
        "parser": PARSER_PACKAGE,
        "parseStatus": "ok",
        "fileVersionUE4": summary["fileVersionUE4"],
        "fileVersionUE5": summary["fileVersionUE5"],
        "packageFlags": summary["packageFlags"],
        "className": primary_class,
        "exportClasses": sorted(set(export_classes))[:32],
        "exportCount": summary["exportCount"],
        "importCount": summary["importCount"],
        "nameCount": summary["nameCount"],
        "names": _interesting_names(names),
        "textures": textures[:MAX_TOKENS],
        "parentMaterials": parents[:16] if material_owner else [],
        "usesMaterials": [] if material_owner else parents[:64],
        "materialFunctions": functions[:MAX_TOKENS],
        "parameterCollections": collections[:16],
        "meshes": meshes[:32],
    }
    if primary is not None:
        metadata["primaryExportSerialSize"] = int(primary["serialSize"])
    return metadata, references


def _tokens(pattern: re.Pattern[bytes], data: bytes, limit: int = MAX_TOKENS) -> list[str]:
    seen: dict[str, int] = {}
    for match in pattern.finditer(data):
        token = match.group(0).decode("ascii", errors="ignore")
        seen[token] = seen.get(token, 0) + 1
    ranked = sorted(seen.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ranked[:limit]]


def value_key(value: float) -> str:
    """Stable key for a float32 value (7 significant digits, sign kept)."""

    return "%.7g" % value


def is_distinctive_value(value: float) -> bool:
    """A number specific enough that sharing it is evidence, not coincidence.

    Rejects non-finite, denormal/huge, and "round" values (few decimals,
    small integers, powers of two) which appear everywhere.
    """

    if value != value or value in (float("inf"), float("-inf")):
        return False
    magnitude = abs(value)
    if magnitude < 1e-4 or magnitude > 1e6:
        return False
    rounded = round(value, 3)
    if abs(value - rounded) <= 1e-6 * max(1.0, magnitude):
        return False
    return True


def numeric_fingerprints(data: bytes, *, max_values: int = 512, max_runs: int = 256,
                         max_repeats: int = 32) -> dict[str, Any]:
    """Distinctive float32 values in a binary blob, with adjacency runs.

    Scanned at every 4-byte alignment; a value repeated more than
    ``max_repeats`` times is treated as filler and dropped.  Runs are
    consecutive distinctive floats — a run of three matching a cbuffer's
    neighbouring lanes is far stronger evidence than three isolated hits.
    """

    import array
    import sys

    if len(data) < 4:
        return {"values": [], "runs": [], "scannedLanes": 0, "distinctiveLaneCount": 0, "fillerValueCount": 0}
    # Serialized property payloads are packed, so a float may start at any
    # byte offset: scan all four alignments and merge.
    per_shift: list[tuple[int, array.array, bytearray]] = []
    counts: dict[str, int] = {}
    total_lanes = 0
    distinctive_total = 0
    for shift in range(4):
        lanes = (len(data) - shift) // 4
        if lanes <= 0:
            continue
        floats = array.array("f")
        floats.frombytes(data[shift: shift + lanes * 4])
        if sys.byteorder != "little":
            floats.byteswap()
        flags = bytearray(lanes)
        for index, value in enumerate(floats):
            if is_distinctive_value(value):
                flags[index] = 1
                key = value_key(value)
                counts[key] = counts.get(key, 0) + 1
        per_shift.append((shift, floats, flags))
        total_lanes += lanes
        distinctive_total += int(sum(flags))
    filler = {key for key, count in counts.items() if count > max_repeats}
    values: list[str] = []
    seen: set[str] = set()
    runs: list[dict[str, Any]] = []
    for shift, floats, flags in per_shift:
        lanes = len(floats)
        index = 0
        while index < lanes:
            if not flags[index] or value_key(floats[index]) in filler:
                index += 1
                continue
            start = index
            run_values: list[str] = []
            while index < lanes and flags[index] and value_key(floats[index]) not in filler and len(run_values) < 16:
                key = value_key(floats[index])
                run_values.append(key)
                if key not in seen and len(values) < max_values:
                    seen.add(key)
                    values.append(key)
                index += 1
            if len(runs) < max_runs:
                runs.append({"offset": start * 4 + shift, "values": run_values})
    runs.sort(key=lambda item: item["offset"])
    return {"values": values, "runs": runs, "scannedLanes": total_lanes,
            "distinctiveLaneCount": distinctive_total, "fillerValueCount": len(filler)}


def parse_export_data(data: bytes, relative: str, sibling_class: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Lift shader type/vertex factory/define names from a cooked ``.uexp``.

    For a texture export the pixel data is fingerprinted instead of scanned
    for numbers (image bytes would only add noise to the value index).
    """

    sibling = PurePosixPath(relative).with_suffix(".uasset").as_posix()
    references = [{
        "relation": "cooked-export-of",
        "catalogRootRelative": True,
        "targetPath": sibling,
        "evidence": {"engine": "unreal", "derivation": "sibling-file-stem"},
    }]
    if sibling_class in TEXTURE_CLASSES:
        from .unreal_texture import texture_fingerprint

        fingerprint = texture_fingerprint(data)
        metadata = {
            "engine": "unreal",
            "parser": PARSER_EXPORT,
            "parseStatus": "ok",
            "scannedBytes": len(data),
            "truncated": False,
            "hasInlineShaderMap": False,
            "textureClass": sibling_class,
            "textureFingerprint": {key: value for key, value in fingerprint.items() if key != "thumbnail"},
        }
        if fingerprint.get("thumbnail"):
            metadata["textureFingerprint"]["thumbnail"] = fingerprint["thumbnail"]
        return metadata, references
    window = data[:MAX_EXPORT_SCAN_BYTES]
    shader_types = _tokens(_SHADER_TYPE, window)
    vertex_factories = _tokens(_VERTEX_FACTORY, window, 64)
    defines = [token for token in _tokens(_DEFINE, window, 512)
               if len(token) >= 6 and not token.startswith("SV_")][:MAX_TOKENS]
    layout_names = set(_tokens(_LAYOUT_NAME, window, 2048))
    has_shader_map = "FMaterialShaderMapContent" in layout_names or "FShaderMapContent" in layout_names
    # Numbers are parameter evidence only for material-like exports; mesh,
    # animation and other bulk exports would flood the value index with
    # geometry floats that look distinctive but mean nothing.
    numeric_ok = (not sibling_class) or sibling_class in MATERIAL_CLASSES or sibling_class in FUNCTION_CLASSES \
        or sibling_class in COLLECTION_CLASSES
    fingerprints = numeric_fingerprints(window) if numeric_ok else {"values": [], "runs": [], "scannedLanes": 0,
                                                                       "distinctiveLaneCount": 0, "fillerValueCount": 0}
    metadata = {
        "engine": "unreal",
        "parser": PARSER_EXPORT,
        "parseStatus": "ok",
        "scannedBytes": len(window),
        "truncated": len(data) > len(window),
        "hasInlineShaderMap": has_shader_map,
        "numericValues": fingerprints["values"],
        "numericRuns": fingerprints["runs"],
        "numericSummary": {key: fingerprints[key] for key in ("scannedLanes", "distinctiveLaneCount", "fillerValueCount")},
        "shaderTypes": shader_types,
        "vertexFactories": vertex_factories,
        "defines": defines,
        "uniformBufferHints": sorted(
            name for name in ("FMaterialUniformPreshaderField", "FMaterialNumericParameterInfo",
                              "FMaterialTextureParameterInfo", "FRHIUniformBufferLayoutInitializer")
            if name in layout_names
        ),
    }
    return metadata, references
