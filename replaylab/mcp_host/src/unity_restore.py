"""Prepare a deterministic Unity import from an existing Bread export package.

No capture IPC, Editor startup or asset guessing occurs here. The caller supplies
an already finalized package; optional mappings remain optional and missing
inputs become explicit gaps instead of restarting capture or asset extraction.
"""
from __future__ import annotations

import hashlib
import base64
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Any, Mapping

SCHEMA = "bread-unity-import/v1"
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_VERTICES = 4_000_000
UNITY_PACKAGE_NAME = "com.bread.renderdoc"


class UnityRestoreError(ValueError):
    pass


def _json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise UnityRestoreError("import metadata exceeds the size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise UnityRestoreError("invalid import metadata JSON") from error
    if not isinstance(value, dict):
        raise UnityRestoreError("import metadata must be a JSON object")
    return value


def _not_redirected(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise UnityRestoreError("redirected import paths are unsupported")


def _integer(value: Any, label: str, minimum: int = 0, maximum: int = MAX_FILE_BYTES) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise UnityRestoreError("invalid " + label)
    return value


def _file(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise UnityRestoreError("package path must be a relative POSIX path")
    path = root.joinpath(relative)
    if Path(relative).is_absolute() or ":" in relative or ".." in Path(relative).parts:
        raise UnityRestoreError("package path escapes its root")
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise UnityRestoreError("redirected package files are unsupported")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise UnityRestoreError("package file is absent or exceeds the size limit")
    return path


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _buffer_bindings(entries: list, bundle: Mapping[str, Any], root: Path,
                     known: Mapping[str, str]) -> list[dict[str, Any]]:
    """Resolve an explicit GPU input window from manifested pre-action bytes."""
    def subset(selector, actual):
        return isinstance(selector, Mapping) and isinstance(actual, Mapping) and all(
            key in actual and (subset(value, actual[key]) if isinstance(value, Mapping) else value == actual[key])
            for key, value in selector.items())
    result = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise UnityRestoreError("runtime bindings must be objects")
        source = entry.get("bufferSource")
        if source is None:
            result.append(dict(entry))
            continue
        if not isinstance(source, Mapping):
            raise UnityRestoreError("bufferSource must select captured bytes")
        if "vertexAttributeIndex" in source:
            index = source["vertexAttributeIndex"]
            attributes = (bundle.get("mesh") or {}).get("attributes", [])
            if type(index) is not int or not 0 <= index < len(attributes):
                raise UnityRestoreError("bufferSource vertexAttributeIndex is outside the captured layout")
            attribute = attributes[index]
            representation = source.get("representation", "raw")
            if representation not in {"raw", "canonical"}:
                raise UnityRestoreError("vertex buffer representation must be raw or canonical")
            raw = attribute.get(representation) or {}
            relative = "source/" + str(raw.get("path", ""))
            if raw.get("status") != "captured" or relative not in known or raw.get("sha256") != known[relative]:
                raise UnityRestoreError("vertex input is absent from the finalized manifest")
            data = _file(root, relative).read_bytes()
            if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != known[relative]:
                raise UnityRestoreError("vertex input changed or exceeds its buffer size limit")
            stride = entry.get("stride", 4)
            if type(stride) is not int or not 4 <= stride <= 2048 or stride % 4 or not data or len(data) % stride:
                raise UnityRestoreError("vertex buffer adapter needs an aligned stride matching its captured bytes")
            result.append({**entry, "type": "buffer", "stride": stride,
                "bufferBase64": base64.b64encode(data).decode("ascii"),
                "sourceIdentity": {"vertexAttributeIndex": index, "representation": representation,
                    "sha256": raw["sha256"], "byteSize": len(data), "layout": {
                        key: attribute.get(key) for key in ("name", "location", "sourceFormat", "byteOffset", "byteStride")},
                    "elementOrder": raw.get("elementOrder", "compact-vertex-order")}})
            continue
        if "resourceId" not in source:
            raise UnityRestoreError("bufferSource requires resourceId or vertexAttributeIndex")
        phase = source.get("phase", "before-action")
        if phase != "before-action":
            raise UnityRestoreError("runtime input buffers require before-action bytes; output snapshots are reference evidence")
        matches = []
        for buffer in bundle.get("buffers", []):
            if str(buffer.get("resourceId")) != str(source["resourceId"]) or buffer.get("status") != "captured":
                continue
            for binding in buffer.get("bindings", []):
                if source.get("sourceUse") is None or subset(source["sourceUse"], binding):
                    matches.append((buffer, binding))
        if len(matches) != 1:
            raise UnityRestoreError("bufferSource must identify exactly one captured input window/use")
        buffer, binding = matches[0]
        if buffer.get("capturePhase") != "before-action":
            raise UnityRestoreError("the captured buffer has no pre-action input evidence")
        raw = buffer.get("raw") or {}
        relative = "source/" + str(raw.get("path", ""))
        if relative not in known or raw.get("sha256") != known[relative]:
            raise UnityRestoreError("buffer input is absent from the finalized manifest")
        data = _file(root, relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != known[relative]:
            raise UnityRestoreError("buffer input changed while preparing the request")
        if len(data) != raw.get("size") or len(data) != buffer.get("byteSize") or len(data) > 64 * 1024 * 1024:
            raise UnityRestoreError("captured buffer window length is invalid")
        stride = entry.get("stride", (binding.get("descriptor") or {}).get("elementByteSize", 0))
        if type(stride) is not int or stride < 4 or stride > 2048 or stride % 4 or len(data) % stride:
            raise UnityRestoreError("buffer input needs an explicit compatible four-byte-aligned target stride")
        result.append({**entry, "type": "buffer", "stride": stride, "bufferBase64": base64.b64encode(data).decode("ascii"),
                       "sourceIdentity": {"resourceId": str(source["resourceId"]), "byteOffset": buffer.get("byteOffset"),
                           "byteSize": len(data), "capturePhase": phase, "sha256": raw["sha256"], "binding": binding}})
    for entry in result:
        if entry.get("type") != "buffer":
            continue
        storage = entry.get("bufferType", "structured")
        if storage not in {"structured", "raw", "append", "counter"}:
            raise UnityRestoreError("bufferType must be structured, raw, append or counter")
        if storage in {"append", "counter"}:
            try:
                data = base64.b64decode(entry.get("bufferBase64", entry.get("dataBase64", "")), validate=True)
            except (ValueError, TypeError) as error:
                raise UnityRestoreError("counted buffer needs explicit initial bytes") from error
            stride, counter = entry.get("stride", 4), entry.get("initialCounter")
            if (type(stride) is not int or stride < 4 or stride > 2048 or stride % 4 or not data or len(data) % stride
                    or type(counter) is not int or not 0 <= counter <= len(data) // stride):
                raise UnityRestoreError("append/counter buffer needs a bounded initialCounter within its capacity")
            provenance = entry.get("counterProvenance")
            if (not isinstance(provenance, Mapping) or provenance.get("kind") not in {"captured", "inferred", "unity-native"}
                    or not isinstance(provenance.get("reason"), str) or not provenance["reason"].strip()):
                raise UnityRestoreError("initialCounter requires explicit captured/inferred/native counterProvenance")
    return result


def _sampler(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Only forward settings the Unity importer can actually apply."""
    if not isinstance(value, Mapping):
        return {}, ["sampler"]
    supported: dict[str, Any] = {}
    unsupported = []
    modes = {"filterMode": ("Point", "Bilinear", "Trilinear"),
             "wrapU": ("Repeat", "Clamp", "Mirror", "MirrorOnce"),
             "wrapV": ("Repeat", "Clamp", "Mirror", "MirrorOnce")}
    for key, setting in value.items():
        if key in modes and isinstance(setting, str):
            canonical = next((mode for mode in modes[key] if mode.casefold() == setting.casefold()), None)
            if canonical is not None:
                supported[key] = canonical
                continue
        elif key == "anisoLevel" and type(setting) is int and 0 <= setting <= 16:
            supported[key] = setting
            continue
        unsupported.append(str(key))
    return supported, sorted(unsupported)


def _format_color_space(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    component = str(value.get("compType", "")).removeprefix("CompType.").lower()
    if value.get("srgbCorrected") is True or component == "unormsrgb":
        return "sRGB"
    if component in {"float", "double", "unorm", "snorm", "uint", "sint", "uscaled", "sscaled", "depth"}:
        return "linear"
    # An absent/typeless view must not inherit the resource's sampling interpretation.
    return "unknown"


def _texture_uv_transform(mapping: Mapping[str, Any], mesh_policy: str,
                          gaps: list[dict[str, Any]]) -> dict[str, Any] | None:
    if "uvTransform" not in mapping:
        return None
    value = mapping["uvTransform"]
    identity = str(mapping["sourceResourceId"])
    if (not isinstance(value, Mapping) or set(value) - {"scale", "offset", "space"}
            or any(not isinstance(value.get(key), list) or len(value[key]) != 2
                   or not all(_number(number) for number in value[key]) for key in ("scale", "offset"))
            or not isinstance(value.get("space", "captured-uv"), str)
            or value.get("space", "captured-uv") not in {"captured-uv", "exported-uv"}):
        gaps.append({"code": "texture_uv_transform_unsupported", "resourceId": identity})
        return None
    space = value.get("space", "captured-uv")
    if space == "captured-uv" and mesh_policy not in {"preserve", "flip-v"}:
        gaps.append({"code": "texture_uv_transform_unresolved", "resourceId": identity,
                     "reason": "captured UV mapping requires the canonical mesh UV bake policy"})
        return None
    scale, offset = list(value["scale"]), list(value["offset"])
    if space == "captured-uv" and mesh_policy == "flip-v":
        # Apply requested(capturedUV) to a mesh containing baked(capturedUV).
        # The target ST is requested composed with inverse(baked), not a second flip.
        scale[1] = -scale[1]
        offset[1] += value["scale"][1]
    if not all(_number(number) for number in (*scale, *offset)):
        gaps.append({"code": "texture_uv_transform_unsupported", "resourceId": identity})
        return None
    return {"scale": scale, "offset": offset,
            "provenance": {"inputSpace": space, "meshUvPolicy": mesh_policy,
                           "operation": "explicit-per-texture"}}


def _accessor(gltf: Mapping[str, Any], binary: bytes, index: int) -> dict[str, Any]:
    try:
        index = _integer(index, "accessor index", maximum=len(gltf["accessors"]) - 1)
        accessor = gltf["accessors"][index]
        view_index = _integer(accessor["bufferView"], "buffer view index", maximum=len(gltf["bufferViews"]) - 1)
        view = gltf["bufferViews"][view_index]
        components = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[accessor["type"]]
        code, width = {5126: ("f", 4), 5125: ("I", 4), 5123: ("H", 2),
                       5121: ("B", 1)}[accessor["componentType"]]
        count = accessor["count"]
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= MAX_VERTICES * 6:
            raise UnityRestoreError("invalid accessor count")
        if (accessor.get("sparse") or accessor.get("normalized")
                or type(view.get("buffer", 0)) is not int or view.get("buffer", 0) != 0):
            raise UnityRestoreError("unsupported accessor encoding")
        view_start = _integer(view.get("byteOffset", 0), "buffer view offset")
        offset = _integer(accessor.get("byteOffset", 0), "accessor offset")
        start = view_start + offset
        stride = _integer(view.get("byteStride", components * width), "accessor stride")
        end = start + (count - 1) * stride + components * width if count else start
        view_end = view_start + _integer(view["byteLength"], "buffer view length")
        if (stride < components * width or start % width or stride % width
                or view_end > len(binary) or end > view_end):
            raise UnityRestoreError("accessor exceeds its buffer view")
        values = [list(struct.unpack_from("<" + code * components, binary, start + i * stride))
                  for i in range(count)]
        if any(not _number(value) for row in values for value in row):
            raise UnityRestoreError("non-finite mesh data")
        return {"components": components, "componentType": accessor["componentType"], "data": values}
    except (KeyError, IndexError, TypeError, struct.error) as error:
        raise UnityRestoreError("invalid mesh accessor") from error


def _mesh(root: Path, gaps: list[dict[str, Any]], channel_map: Mapping[str, Any],
          known: Mapping[str, str]) -> dict[str, Any]:
    gltf = _json(_file(root, "asset.gltf"))
    if not isinstance(channel_map, Mapping) or (gltf.get("asset") or {}).get("version") != "2.0":
        raise UnityRestoreError("invalid canonical mesh or vertex mapping")
    # Bread's canonical export is one mesh and one draw, with its basis already applied.
    if len(gltf.get("meshes", [])) != 1 or len(gltf["meshes"][0]["primitives"]) != 1:
        raise UnityRestoreError("expected one canonical captured draw")
    if len(gltf.get("buffers", [])) != 1:
        raise UnityRestoreError("expected one canonical mesh buffer")
    buffer = gltf["buffers"][0]
    if buffer.get("uri") not in known:
        raise UnityRestoreError("mesh buffer is absent from the package manifest")
    binary = _file(root, buffer["uri"]).read_bytes()
    if _integer(buffer.get("byteLength"), "mesh buffer length") != len(binary):
        raise UnityRestoreError("mesh buffer length disagrees with the canonical document")
    primitive = gltf["meshes"][0]["primitives"][0]
    attributes: dict[str, Any] = {}
    provenance = ((gltf.get("extras") or {}).get("replaylab") or {})
    for missing in provenance.get("omittedAttributes", []):
        gaps.append({"code": "vertex_channel_not_imported", "source": missing,
                     "recovery": "use preserved source bytes with a matching vertex/buffer adapter"})
    if any(item.get("assumed") for item in provenance.get("passthroughAttributes", []) if isinstance(item, Mapping)):
        gaps.append({"code": "vertex_role_unverified", "reason": "confirm assumed POSITION against the vertex Shader"})
    receipts = provenance.get("vertexChannels", [])
    channels = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or receipt.get("gltfSemantic") in channels:
            raise UnityRestoreError("invalid or duplicate vertex channel provenance")
        channels[receipt.get("gltfSemantic")] = receipt
    shader_bindings = []
    for semantic, index in primitive["attributes"].items():
        receipt = channels.get(semantic, {})
        if receipt and receipt.get("accessorIndex") != index:
            raise UnityRestoreError("vertex channel provenance disagrees with its accessor")
        # glTF TEXCOORD is only VEC2; Unity accepts one through four components.
        # Restore a packed channel's explicit/declared target from the export
        # receipt, never from the spelling or ordinal of an anonymous _ATTR slot.
        target = str(channel_map.get(semantic, receipt.get("requestedSemantic", semantic)))
        supported = target in {"POSITION", "NORMAL", "TANGENT", "COLOR_0"} or bool(
            re.fullmatch(r"TEXCOORD_[0-7]", target))
        if not supported:
            gaps.append({"code": "unmapped_vertex_channel", "semantic": semantic})
            continue
        if target in attributes:
            raise UnityRestoreError("multiple channels map to the same Unity semantic")
        attributes[target] = _accessor(gltf, binary, index)
        dimensions = attributes[target]["components"]
        if receipt and receipt.get("components") != dimensions:
            raise UnityRestoreError("vertex channel provenance disagrees with its component count")
        if ((target in {"POSITION", "NORMAL"} and dimensions != 3)
                or (target == "TANGENT" and dimensions != 4)
                or (target == "COLOR_0" and dimensions not in {3, 4})):
            raise UnityRestoreError("invalid component count for " + target)
        unity_semantic = target.replace("TEXCOORD_", "TEXCOORD").replace("COLOR_0", "COLOR")
        shader_bindings.append({"sourceAttributeIndex": receipt.get("attributeIndex"),
            "gltfSemantic": semantic, "unityAttribute": target, "shaderSemantic": unity_semantic,
            "components": dimensions, "storageFormat": "Float32", "source": receipt.get("source", {}),
            "shaderType": "float" + (str(dimensions) if dimensions > 1 else ""),
            "mappingBasis": "explicit-unity-map" if semantic in channel_map else "export-semantic"})
    if "POSITION" not in attributes or attributes["POSITION"]["components"] != 3:
        raise UnityRestoreError("Unity mesh requires a three-component position mapping")
    count = len(attributes["POSITION"]["data"])
    if not 0 < count <= MAX_VERTICES or any(len(a["data"]) != count for a in attributes.values()):
        raise UnityRestoreError("mesh channel lengths disagree")
    index_data = _accessor(gltf, binary, primitive["indices"])
    if index_data["components"] != 1 or index_data["componentType"] not in {5121, 5123, 5125}:
        raise UnityRestoreError("mesh indices must be unsigned scalar integers")
    indices = [row[0] for row in index_data["data"]]
    if any(not isinstance(i, int) or i < 0 or i >= count for i in indices):
        raise UnityRestoreError("mesh index is outside the vertex range")
    mode = _integer(primitive.get("mode", 4), "primitive mode", maximum=6)
    if mode == 5:  # triangle strip; keep winding and skip degenerate primitives
        indices = [x for i in range(len(indices) - 2)
                   for x in ((indices[i + 1], indices[i], indices[i + 2]) if i % 2
                             else (indices[i], indices[i + 1], indices[i + 2]))
                   if len(set(indices[i:i + 3])) == 3]
        mode = 4
    elif mode == 6:
        indices = [x for i in range(1, len(indices) - 1) for x in (indices[0], indices[i], indices[i + 1])]
        mode = 4
    elif mode == 2:
        indices = indices + indices[:1]
        mode = 3
    if mode not in (0, 1, 3, 4) or (mode in (1, 4) and len(indices) % (2 if mode == 1 else 3)):
        raise UnityRestoreError("unsupported or malformed primitive topology")
    return {"attributes": attributes, "indices": indices, "mode": mode,
            "shaderBindings": shader_bindings,
            "coordinatePolicy": "already-applied-by-transfer-spec",
            "sourceExtras": gltf.get("extras", {})}


def prepare_unity_import(package_root: str | Path, project_path: str | Path) -> dict[str, Any]:
    """Validate a published package and write an immutable Editor request in Library.

    This does not alter Assets or install/start Unity. Source evidence remains in
    the export package. No URP/Lit substitution is implicit for an unknown Shader.
    """
    from .resource_export import BUNDLE_SCHEMA, FINAL_MANIFEST_SCHEMA, TRANSFER_SCHEMA, _json_sha256

    root, project = Path(package_root).absolute(), Path(project_path).absolute()
    _not_redirected(root)
    _not_redirected(project)
    root, project = root.resolve(), project.resolve()
    _file(project, "ProjectSettings/ProjectVersion.txt")
    _file(project, "Packages/manifest.json")
    _not_redirected(project / "Assets")
    if not (project / "Assets").is_dir():
        raise UnityRestoreError("target is not a Unity project with an Assets directory")
    manifest_path = _file(root, "package-manifest.json")
    manifest = _json(manifest_path)
    if manifest.get("schema") != FINAL_MANIFEST_SCHEMA or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
        raise UnityRestoreError("not a finalized Bread resource export")
    if not re.fullmatch(r"[a-f0-9]{32}", str(manifest.get("operationId", ""))):
        raise UnityRestoreError("invalid resource export operation identity")
    records = manifest.get("files")
    if not isinstance(records, list) or not 1 <= len(records) <= 16384:
        raise UnityRestoreError("invalid package file manifest")
    known: dict[str, str] = {}
    case_paths = set()
    for record in records:
        if (not isinstance(record, Mapping) or not isinstance(record.get("path"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", "")))):
            raise UnityRestoreError("invalid package file record")
        if record["path"].casefold() in case_paths:
            raise UnityRestoreError("duplicate package file path")
        case_paths.add(record["path"].casefold())
        path = _file(root, record["path"])
        if path.stat().st_size != _integer(record.get("size"), "package file size") or _sha(path) != record["sha256"]:
            raise UnityRestoreError("export package changed: " + record["path"])
        known[record["path"]] = record["sha256"]
    if not {"asset.gltf", "asset.bin", "transfer-spec.json"}.issubset(known):
        raise UnityRestoreError("export manifest omits required files")
    spec = _json(root / "transfer-spec.json")
    if spec.get("schema") != TRANSFER_SCHEMA or spec.get("schemaVersion") != 1:
        raise UnityRestoreError("unsupported transfer specification")
    if (manifest.get("provenance") or {}).get("transferSpecSha256") != _json_sha256(spec):
        raise UnityRestoreError("transfer specification disagrees with the finalized manifest")
    options = spec.get("unity", {})
    if not isinstance(options, dict):
        raise UnityRestoreError("unity settings must be an object")
    gaps: list[dict[str, Any]] = []
    gltf = _json(root / "asset.gltf")
    effect_route = ((gltf.get("extras", {}).get("replaylab", {}).get("executionKind") == "effect"
                     and gltf.get("extras", {}).get("replaylab", {}).get("geometryApplicable") is False)
                    or options.get("executionKind") == "effect")
    mesh = ({"sourceExtras": gltf.get("extras", {}), "shaderBindings": [], "geometryApplicable": False}
            if effect_route else _mesh(root, gaps, options.get("vertexChannels", {}), known))
    source_bundles = [p for p in known if p.startswith("source/") and p.endswith(".json")]
    textures: dict[str, Any] = {}
    bundles = []
    for relative in source_bundles:
        if known[relative] != manifest.get("captureBundleSha256"):
            continue
        candidate = _json(root / relative)
        if candidate.get("schema") == BUNDLE_SCHEMA and candidate.get("schemaVersion") == 1:
            bundles.append(candidate)
    if len(bundles) != 1:
        raise UnityRestoreError("package must contain its exact captured source bundle")
    source_bundle = bundles[0]
    mesh_uv_policy = (mesh["sourceExtras"].get("replaylab") or {}).get("uvPolicy", "unknown")
    if not effect_route and mesh_uv_policy != spec.get("uvPolicy", "preserve"):
        gaps.append({"code": "mesh_uv_bake_policy_unverified", "meshUvPolicy": mesh_uv_policy,
                     "requestedUvPolicy": spec.get("uvPolicy", "preserve")})
        mesh_uv_policy = "unknown"
    platform_contract = {
        "sourceApi": source_bundle.get("api") or "unknown", "meshUvPolicy": mesh_uv_policy,
        "automaticApiFlip": False, "imagePixelsRewritten": False,
        "textureUvPolicy": "explicit-transform-after-canonical-mesh-bake",
        "orientationVerified": False, "targetGraphicsApi": "query-unity-editor",
        "targetColorSpace": "query-unity-editor", "shaderPlatformAdaptation": "requires-existing-target-pipeline-shader-validation"}
    for texture in bundles[0].get("textures", []):
        if not isinstance(texture, Mapping) or "resourceId" not in texture:
            raise UnityRestoreError("invalid captured texture record")
        identity = str(texture["resourceId"])
        if identity in textures:
            raise UnityRestoreError("ambiguous captured texture resource")
        textures[identity] = texture
    imported_textures = []
    role_properties = {"baseColor": "_BaseMap", "normal": "_BumpMap", "emission": "_EmissionMap"}
    shader = spec.get("shader", {})
    strategy = shader.get("strategy", "evidence-only")
    for mapping in spec.get("textures", []):
        texture = textures.get(str(mapping["sourceResourceId"]))
        if texture is None:
            raise UnityRestoreError("texture mapping has no captured resource")
        prop = mapping.get("targetProperty") or mapping.get("target")
        if not prop and strategy == "target-native-pbr":
            prop = role_properties.get(mapping.get("role"))
        if not prop:
            gaps.append({"code": "texture_property_unmapped", "resourceId": str(mapping["sourceResourceId"])})
            continue
        candidates = [a for a in texture.get("artifacts", []) if isinstance(a, Mapping)
                      and a.get("status") == "captured" and a.get("encoding") in {"exr", "hdr", "tga", "png"}
                      and a.get("scope", "mip 0, slice 0") in {"mip 0, slice 0", "mip 0, slice 0, sample 0"}]
        def subset(selector: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
            return all(key in binding and (subset(value, binding[key])
                       if isinstance(value, Mapping) and isinstance(binding[key], Mapping)
                       else value == binding[key]) for key, value in selector.items())
        bindings = texture.get("bindings") or []
        use = mapping.get("sourceUse")
        selected = [binding for binding in bindings if isinstance(binding, Mapping)
                    and (use is None or subset(use, binding))]
        if len(selected) != 1:
            raise UnityRestoreError("texture mapping must select exactly one captured use")
        descriptor = selected[0].get("descriptor") or {}
        resource = texture.get("resource") or texture  # legacy flat evidence remains readable
        if any(_integer(descriptor.get(k, 0) or 0, "texture view " + k) != 0
               for k in ("firstMip", "firstSlice")):
            candidates = []
        if (resource.get("cubemap") or _integer(resource.get("arraySize", resource.get("arraysize", 0)), "texture array size") != 1
                or _integer(resource.get("depth", 0), "texture depth") != 1
                or _integer(resource.get("sampleCount", 0), "texture sample count") != 1
                or _integer(resource.get("dimension", 0), "texture dimension") != 2):
            candidates = []
        if not candidates:
            gaps.append({"code": "texture_requires_conversion", "resourceId": str(mapping["sourceResourceId"]),
                         "reason": "no importable image for the requested view; retain DDS/source evidence"})
            continue
        artifact = candidates[0]
        relative = "source/" + artifact["path"]
        if relative not in known or artifact.get("sha256") != known.get(relative):
            raise UnityRestoreError("texture artifact is absent from the package manifest")
        if Path(relative).suffix.lower() != "." + artifact["encoding"]:
            raise UnityRestoreError("texture artifact extension disagrees with its encoding")
        source_sampler = mapping.get("sampler", {})
        sampler, unsupported_sampler = _sampler(source_sampler)
        if unsupported_sampler:
            gaps.append({"code": "sampler_mapping_unsupported", "resourceId": str(mapping["sourceResourceId"]),
                         "unsupportedFields": unsupported_sampler})
        sampling_space = _format_color_space(descriptor.get("viewFormat"))
        requested_space = mapping.get("colorSpace", "raw")
        normal_map = bool(mapping.get("unityNormalMap", False))
        effective_space = "linear" if normal_map else requested_space
        identity = str(mapping["sourceResourceId"])
        if sampling_space == "unknown":
            gaps.append({"code": "texture_sampling_color_space_unknown", "resourceId": identity})
        elif ("sRGB" if effective_space == "sRGB" else "linear") != sampling_space:
            gaps.append({"code": "texture_sampling_color_space_mismatch", "resourceId": identity,
                         "sourceSamplingSpace": sampling_space, "requestedColorSpace": requested_space,
                         "effectiveColorSpace": effective_space})
        if normal_map:
            gaps.append({"code": "normal_map_decode_unverified", "resourceId": identity,
                         "reason": "Unity normal-map repacking requires a matching Unity normal decode in the Shader"})
        data_role = mapping.get("role") in {"normal", "metallic", "roughness", "occlusion", "mask", "data", "height"}
        if requested_space == "sRGB" and (normal_map or data_role):
            gaps.append({"code": "texture_data_color_space_conflict", "resourceId": identity,
                         "requestedColorSpace": requested_space, "effectiveColorSpace": effective_space})
        imported = {**mapping, "property": str(prop), "sourcePath": str(_file(root, relative)),
                                  "sha256": known[relative], "extension": Path(relative).suffix,
                                  "sourceView": descriptor, "sourceResource": resource,
                                  "mipPolicy": "base-level-image", "sampler": sampler,
                                  "sourceSampler": source_sampler,
                                  "capturedSamplerEvidence": {
                                      "combinedSampler": selected[0].get("combinedSampler"),
                                      "stageCandidates": [entry for entry in source_bundle.get("samplers", [])
                                                          if isinstance(entry, Mapping) and entry.get("stage") == selected[0].get("stage")],
                                      "separateSamplerAssociationInferred": False},
                                  "colorSpace": effective_space, "normalMap": normal_map,
                                  "colorSpaceContract": {"authority": "explicit-transfer-mapping",
                                      "requested": requested_space, "effective": effective_space,
                                      "sourceSamplingSpace": sampling_space,
                                      "resourceFormatSpace": _format_color_space(resource.get("format")),
                                      "normalRepackingRequiresUnityDecode": normal_map},
                                  "orientationVerified": False}
        uv_transform = _texture_uv_transform(mapping, mesh_uv_policy, gaps)
        if uv_transform is not None:
            parameter = str(prop) + "_ST"
            if any(entry.get("classification") == "material" and entry.get("target") == parameter
                   for entry in spec.get("constantBuffers", [])):
                gaps.append({"code": "texture_uv_parameter_conflict", "resourceId": identity,
                             "target": parameter, "preservedSource": "constant-buffer-mapping",
                             "skippedSource": "texture-uv-transform"})
            else:
                imported["unityUvTransform"] = uv_transform
        imported_textures.append(imported)
        mip_count = _integer(resource.get("mips", 0) or 0, "texture mip count")
        if mip_count > 1:
            gaps.append({"code": "source_mips_not_preserved", "resourceId": str(mapping["sourceResourceId"])})
        elif mip_count == 0:
            gaps.append({"code": "source_mip_count_unknown", "resourceId": identity})
        if mapping.get("samplerPolicy") != "target-default" and not mapping.get("sampler"):
            gaps.append({"code": "sampler_mapping_required", "resourceId": str(mapping["sourceResourceId"])})
    primary_shader = options.get("shaderAssetPath")
    if primary_shader is not None and (not isinstance(primary_shader, str) or not any(
            isinstance(asset, Mapping) and asset.get("path") == primary_shader and asset.get("kind") == "shader"
            for asset in options.get("assets", []))):
        raise UnityRestoreError("unity.shaderAssetPath must select a manifested Shader asset")
    shader_file = "UnityURP/" + primary_shader if primary_shader else "UnityURP/Shaders/BreadExport.shader"
    shader_source = None
    if shader_file in known:
        shader_source = {"sourcePath": str(root / shader_file), "sha256": known[shader_file]}
    elif not effect_route and strategy != "target-native-pbr":
        gaps.append({"code": "unity_shader_required", "reason": "adapt the captured Shader to the observed target pipeline before visual acceptance"})
    from .cbuffer_transfer import resolve_cbuffer_transfer
    constants = resolve_cbuffer_transfer(source_bundle, spec, root / "source")
    properties = constants["properties"]
    gaps.extend(constants["gaps"])
    runtime_bindings = list(constants["runtimeBindings"])
    explicit_bindings = options.get("runtimeBindings", [])
    if not isinstance(explicit_bindings, list):
        raise UnityRestoreError("unity.runtimeBindings must be a list")
    runtime_bindings.extend(_buffer_bindings(explicit_bindings, source_bundle, root, known))
    if effect_route:
        # Compute/fullscreen graph parameters use the same physical capture
        # identities, but are uploaded to the actual implementation pass.
        runtime_bindings = [*properties, *runtime_bindings]
        properties = []
    buffer_backed_attributes = []
    for gap in list(gaps):
        if gap.get("code") != "vertex_channel_not_imported" or not isinstance(gap.get("source"), Mapping):
            continue
        omitted = gap["source"]
        candidates = [attribute for attribute in (source_bundle.get("mesh") or {}).get("attributes", [])
                      if isinstance(attribute, Mapping) and attribute.get("name") == omitted.get("name")]
        if len(candidates) != 1 or not candidates[0].get("perInstance"):
            continue
        attribute = candidates[0]
        bound = attribute.get("buffer") or {}
        for entry in runtime_bindings:
            identity = entry.get("sourceIdentity") or {}
            binding = identity.get("binding") or {} if isinstance(identity, Mapping) else {}
            if (entry.get("type") == "buffer" and binding.get("kind") == "per-instance-stream"
                    and (binding.get("access") or {}).get("attributeIndex") == attribute.get("index")
                    and str(identity.get("resourceId")) == str(bound.get("resourceId"))
                    and type(bound.get("byteOffset")) is int and type(bound.get("byteSize")) is int and bound["byteSize"] > 0
                    and type(identity.get("byteOffset")) is int and type(identity.get("byteSize")) is int
                    and identity["byteOffset"] <= bound["byteOffset"]
                    and identity["byteOffset"] + identity["byteSize"] >= bound["byteOffset"] + bound["byteSize"]):
                buffer_backed_attributes.append({"attributeIndex": attribute["index"], "source": attribute,
                    "targetBuffer": entry.get("name", entry.get("target")), "binding": identity,
                    "shaderAdapterValidation": "required"})
                gaps.remove(gap)
                break
    mesh["bufferBackedAttributes"] = buffer_backed_attributes
    covered_buffers = {(str((entry.get("sourceIdentity") or {}).get("resourceId")),
                        json.dumps((entry.get("sourceIdentity") or {}).get("binding"), sort_keys=True))
                       for entry in runtime_bindings if isinstance(entry.get("sourceIdentity"), Mapping)
                       and entry.get("type") == "buffer"}
    for buffer in source_bundle.get("buffers", []):
        for binding in buffer.get("bindings", []):
            if (str(buffer.get("resourceId")), json.dumps(binding, sort_keys=True)) not in covered_buffers:
                gaps.append({"code": "captured_buffer_binding_unmapped", "resourceId": str(buffer.get("resourceId")),
                             "binding": binding, "capturePhase": buffer.get("capturePhase")})
    for missing in source_bundle.get("unexportedResources", []):
        gaps.append({"code": "capture_resource_unexported", "source": missing})
    if len(runtime_bindings) > 4096:
        raise UnityRestoreError("Unity runtime bindings exceed the supported limit")
    assets = []
    implementation_assets = options.get("assets", [])
    if not isinstance(implementation_assets, list) or len(implementation_assets) > 1024:
        raise UnityRestoreError("unity.assets must be a bounded list")
    for asset in implementation_assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("path"), str):
            raise UnityRestoreError("Unity assets require a relative path")
        relative = "UnityURP/" + asset["path"]
        if relative not in known:
            raise UnityRestoreError("Unity implementation asset is absent from the finalized manifest")
        assets.append({"path": asset["path"], "kind": asset.get("kind", "data"),
                       "sourcePath": str(_file(root, relative)), "sha256": known[relative]})
    if effect_route:
        from .unity_effects import validate_effect
        if options.get("effect") is None:
            gaps.append({"code": "effect_implementation_required", "nextTool": "fork_resource_export",
                         "routes": ["compute", "fullscreen", "custom"], "reason": "Create the captured algorithm and its inferred/native inputs in unity.effect"})
        else:
            try:
                options = {**options, "effect": validate_effect(options["effect"], assets)}
            except ValueError as error:
                raise UnityRestoreError(str(error)) from error
        if "scene" not in options:
            options = {**options, "scene": {"camera": {"projection": "orthographic", "orthographicSize": 1,
                "backgroundColor": [0, 0, 0, 1], "allowMSAA": False}, "lights": [],
                "cameraPurpose": "effect-readback-only"}}
    pipeline_policy = options.get("pipelinePolicy", {})
    if not isinstance(pipeline_policy, dict):
        raise UnityRestoreError("unity.pipelinePolicy must be an object")
    if pipeline_policy.get("targetPipeline", "") not in ("", "urp", "hdrp", "built-in", "custom"):
        raise UnityRestoreError("unknown target pipeline")
    if type(pipeline_policy.get("allowCreateUrp", False)) is not bool:
        raise UnityRestoreError("allowCreateUrp must be an explicit boolean")
    if pipeline_policy.get("allowCreateUrp") and pipeline_policy.get("targetPipeline") != "urp":
        raise UnityRestoreError("URP creation requires targetPipeline=urp")
    for setting in ("scene", "renderState", "renderPlan"):
        if setting in options and not isinstance(options[setting], dict):
            raise UnityRestoreError("unity." + setting + " must be an object")
    operation = str(manifest.get("operationId", ""))
    manifest_hash = _sha(manifest_path)
    from .material_inspector import build_material_inspector
    request = {"schema": SCHEMA, "projectPath": str(project), "operationId": operation,
               "packageManifestSha256": manifest_hash, "packageRoot": str(root),
               "assetName": str(spec.get("assetName") or "BreadDraw"), "mesh": None if effect_route else mesh,
               "executionKind": "effect" if effect_route else "draw", "geometryApplicable": not effect_route,
               "textures": imported_textures, "material": spec.get("material", {}),
               "platformContract": platform_contract,
               "properties": properties, "shader": shader_source, "shaderStrategy": strategy,
               "materialInspector": build_material_inspector(spec, properties, imported_textures),
               "assets": assets, "runtimeBindings": runtime_bindings,
               "constantBufferCoverage": constants.get("coverage", {}),
               "pipelinePolicy": pipeline_policy,
               "options": options, "gaps": gaps, "visualEquivalenceVerified": False}
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request_hash = hashlib.sha256(encoded).hexdigest()
    request["requestSha256"] = request_hash
    request["destination"] = "Assets/BreadImports/" + request_hash[:24]
    request_root = project / "Library" / "Bread" / "Requests"
    _not_redirected(request_root)
    request_root.mkdir(parents=True, exist_ok=True)
    # A project may redirect Library intentionally; never write through such a path.
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
           for p in (request_root, request_root.parent, project / "Library")):
        raise UnityRestoreError("Unity request directory is redirected")
    request_path = request_root / (request_hash + ".json")
    _not_redirected(request_path)
    output = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(output) > MAX_FILE_BYTES:
        raise UnityRestoreError("Unity import request exceeds the size limit")
    idempotent = request_path.exists()
    if idempotent:
        if not request_path.is_file() or request_path.read_bytes() != output:
            raise UnityRestoreError("an existing Unity import request changed")
    else:
        with tempfile.NamedTemporaryFile(dir=request_root, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(output)
        try:
            _not_redirected(request_root)
            os.replace(temporary, request_path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"requestPath": str(request_path), "requestSha256": request_hash,
            "destination": request["destination"], "gaps": gaps,
            "vertexBindings": mesh["shaderBindings"],
            "packageManifestSha256": manifest_hash, "operationId": operation, "idempotent": idempotent}
