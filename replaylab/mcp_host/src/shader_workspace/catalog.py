"""Strict loading of bridge-produced capture shader catalogs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..symbols.dxbc import ShaderContainer
from .abi import ShaderAbi, parse_shader_abi
from .binary_formats import (
    PortableShaderExecutable,
    api_semantic_format,
    executable_format,
    fingerprint_shader_executable,
)
from .errors import AbiValidationError, CatalogUseNotCoveredError, CatalogValidationError
from .io import is_within, read_json_object

CATALOG_SCHEMA = "renderdoc-ultra-capture-symbols/v1"
CATALOG_VERSION = 1
MAX_CATALOG_BYTES = 256 * 1024 * 1024
MAX_SHADER_BYTES = 512 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ABI_STATUS_UNAVAILABLE = "unavailable"
ABI_STATUS_CAPTURED_COMPLETE = "captured-reflection-complete"
ABI_STATUS_DXIL_ACCESS_REQUIRED = (
    "captured-reflection-dxil-access-signature-required"
)
_DXIL_LAYOUT_EVIDENCE_KIND = "dxc-dxil-buffer-definition"

# Keep in step with renderdoc_mcp_bridge._STAGE_ALIASES: the stable capture
# identity hashes the normalised stage, so one spelling both sides do not agree
# on is two identities for one Shader.
_STAGE_ALIASES = {
    "pixel": "fragment",
    "ps": "fragment",
    "fs": "fragment",
    "vs": "vertex",
    "gs": "geometry",
    "hs": "hull",
    "ds": "domain",
    "cs": "compute",
    "as": "task",
    "amplification": "task",
    "ms": "mesh",
    "raygeneration": "raygen",
    "ray generation": "raygen",
    "ray-generation": "raygen",
    "ray_generation": "raygen",
    "tess_ctrl": "hull",
    "tess_eval": "domain",
}


def normalize_stage(value: Any) -> str:
    stage = str(value or "").strip().lower().replace("shaderstage.", "")
    return _STAGE_ALIASES.get(stage, stage)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CatalogValidationError("%s must be a positive integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CatalogValidationError("%s must be a positive integer" % field) from exc
    if result <= 0:
        raise CatalogValidationError("%s must be a positive integer" % field)
    return result


def _sha256(value: Any, field: str) -> str:
    result = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(result):
        raise CatalogValidationError("%s must be a SHA-256 hex digest" % field)
    return result


def _optional_sha256(value: Any, field: str) -> str:
    result = str(value or "").strip().lower()
    if not result:
        return ""
    if not SHA256_RE.fullmatch(result):
        raise CatalogValidationError("%s must be empty or a SHA-256 hex digest" % field)
    return result


@dataclass(frozen=True)
class ShaderKey:
    """The complete key required to select exactly one captured shader."""

    resource_id: int
    stage: str
    entry_point: str
    raw_sha256: str

    @classmethod
    def from_value(cls, value: "ShaderKey | Mapping[str, Any]") -> "ShaderKey":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise CatalogValidationError(
                "shader_key must be a ShaderKey, mapping, or exact catalog "
                "shaderKey string"
            )
        resource_id = _positive_int(
            value.get("resourceId", value.get("shaderResourceId")),
            "shader_key.resourceId",
        )
        stage = normalize_stage(value.get("stage"))
        if not stage:
            raise CatalogValidationError("shader_key.stage is required")
        entry = str(value.get("entryPoint", value.get("entry", "")) or "")
        raw_hash = _sha256(value.get("rawSha256"), "shader_key.rawSha256")
        return cls(resource_id, stage, entry, raw_hash)

    @property
    def catalog_key(self) -> str:
        return "%d:%s:%s:%s" % (
            self.resource_id,
            self.stage,
            self.entry_point,
            self.raw_sha256 or "no-raw-bytes",
        )

    def as_dict(self) -> dict:
        return {
            "resourceId": self.resource_id,
            "stage": self.stage,
            "entryPoint": self.entry_point,
            "rawSha256": self.raw_sha256,
            "shaderKey": self.catalog_key,
        }


@dataclass(frozen=True)
class SelectedShader:
    catalog_path: Path
    capture_id: str
    capture_path: str
    api: str
    key: ShaderKey
    blob_path: Path
    blob: bytes
    container: ShaderContainer | PortableShaderExecutable
    encoding: str
    abi: ShaderAbi | None
    abi_status: str


def _dxil_ambiguity_only_abi(
    record: Mapping[str, Any], key: ShaderKey
) -> ShaderAbi | None:
    """Project one otherwise complete stripped-DXIL interface, fail closed.

    The bridge deliberately lowers ``interfaceComplete`` when old/stripped DXIL
    reflection cannot prove CBuffer member fields. That single uncertainty can
    still be represented by :class:`ShaderAbi`: the physical block envelope is
    retained and later comparison requires independently extracted DXIL byte
    access signatures for both binaries. Nothing else is inferred here.

    Parsing a copy with only the completeness bit raised gives every ordinary
    signature, resource, sampler, topology, thread-group and physical CBuffer
    field the existing strict ABI validation. We then accept only complete or
    ambiguous CBuffer layouts, require at least one ambiguity, and reject block
    shapes the access-signature comparator cannot prove.
    """

    projected = dict(record)
    projected["interfaceComplete"] = True
    try:
        abi = parse_shader_abi(
            projected,
            expected_stage=key.stage,
            expected_entry_point=key.entry_point,
        )
    except AbiValidationError:
        return None

    blocks = abi.constant_blocks
    if not any(block.layout_completeness == "ambiguous" for block in blocks):
        return None
    if any(
        block.layout_completeness not in {"complete", "ambiguous"}
        or block.byte_size <= 0
        or (
            block.layout_completeness == "ambiguous"
            and block.array_size != 1
        )
        for block in blocks
    ):
        return None
    return abi


def _lower_unproven_dxil_cbuffer_layouts(
    record: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Apply Bridge-owned DXC layout evidence as a lower-only projection.

    CBuffer and member names are intentionally irrelevant here.  A block is
    lowered only when the fixed DXC ``Buffer Definitions`` evidence says its
    type annotation is absent, or when that evidence lane was unavailable.
    The latter is deliberately conservative.  Either state routes the block
    through the independent original/candidate DXIL access-signature gate;
    this function never manufactures a replacement member type.
    """

    blocks_value = record.get("constantBlocks")
    if not isinstance(blocks_value, list):
        return record
    projected_blocks: list[Any] = []
    lowered = False
    for index, value in enumerate(blocks_value):
        if not isinstance(value, Mapping):
            projected_blocks.append(value)
            continue
        evidence = value.get("layoutEvidence")
        if evidence is None:
            projected_blocks.append(value)
            continue
        if not isinstance(evidence, Mapping):
            raise CatalogValidationError(
                "constantBlocks[%d].layoutEvidence must be an object" % index
            )
        if str(evidence.get("kind", "") or "") != _DXIL_LAYOUT_EVIDENCE_KIND:
            raise CatalogValidationError(
                "constantBlocks[%d].layoutEvidence kind is unsupported" % index
            )
        status = str(evidence.get("typeAnnotation", "") or "").strip().casefold()
        if status not in {"absent", "unavailable"}:
            raise CatalogValidationError(
                "constantBlocks[%d].layoutEvidence typeAnnotation is invalid"
                % index
            )
        source_hash = str(evidence.get("sourceSha256", "") or "").strip().lower()
        access_hash = str(
            evidence.get("accessSignatureSha256", "") or ""
        ).strip().lower()
        if bool(source_hash) != bool(access_hash):
            raise CatalogValidationError(
                "constantBlocks[%d].layoutEvidence hashes are incomplete" % index
            )
        if source_hash:
            if not SHA256_RE.fullmatch(source_hash) or not SHA256_RE.fullmatch(
                access_hash
            ):
                raise CatalogValidationError(
                    "constantBlocks[%d].layoutEvidence hashes are invalid" % index
                )
        elif status == "absent":
            raise CatalogValidationError(
                "constantBlocks[%d] absent type annotation has no DXC evidence hash"
                % index
            )
        block = dict(value)
        block["layoutCompleteness"] = "ambiguous"
        projected_blocks.append(block)
        lowered = True
    if not lowered:
        return record
    projected = dict(record)
    projected["constantBlocks"] = projected_blocks
    projected["interfaceComplete"] = False
    return projected


def _format_hint(record: Mapping[str, Any]) -> str:
    value = str(
        record.get(
            "encoding",
            record.get("binaryFormat", record.get("format", "")),
        )
        or ""
    ).strip().lower().replace("_", "-")
    # Native RenderDoc reflection serializes the enum as ShaderEncoding.GLSL
    # (and similarly DXBC/DXIL/SPIRV). Preserve that explicit format instead of
    # silently falling back to binary detection, which cannot identify GLSL.
    if value.startswith("shaderencoding."):
        value = value[len("shaderencoding."):]
    aliases = {
        "directxbytecode": "dxbc",
        "directx-bytecode": "dxbc",
        "dxil-container": "dxil",
        "spv": "spirv",
        "spir-v": "spirv",
        "vulkan-spirv": "spirv",
        "openglspirv": "opengl-spirv",
        "opengl-spv": "opengl-spirv",
    }
    return aliases.get(value, value) if value in aliases or value in {
        "dxbc",
        "dxil",
        "spirv",
        "opengl-spirv",
        "glsl",
    } else ""


def capture_identity(catalog: Mapping[str, Any], keys: list[ShaderKey]) -> str:
    material = {
        "capturePath": str(catalog.get("capturePath", "") or ""),
        "api": str(catalog.get("api", "") or ""),
        "shaders": sorted(
            (
                {
                    "resourceId": key.resource_id,
                    "stage": key.stage,
                    "entryPoint": key.entry_point,
                    "rawSha256": key.raw_sha256,
                }
                for key in keys
            ),
            key=lambda item: (
                item["resourceId"],
                item["stage"],
                item["entryPoint"],
                item["rawSha256"],
            ),
        ),
    }
    payload = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_key(record: Mapping[str, Any], index: int) -> ShaderKey:
    resource_a = record.get("shaderResourceId")
    resource_b = record.get("resourceId")
    if resource_a is not None and resource_b is not None:
        if _positive_int(
            resource_a, "shaders[%d].shaderResourceId" % index
        ) != _positive_int(resource_b, "shaders[%d].resourceId" % index):
            raise CatalogValidationError(
                "shaders[%d] has conflicting resource identifiers" % index
            )
    resource_id = _positive_int(
        resource_a if resource_a is not None else resource_b,
        "shaders[%d].resourceId" % index,
    )
    stage = normalize_stage(record.get("stage"))
    if not stage:
        raise CatalogValidationError("shaders[%d].stage is required" % index)
    entry = str(record.get("entryPoint", record.get("entry", "")) or "")
    raw_hash = _optional_sha256(
        record.get("rawSha256"), "shaders[%d].rawSha256" % index
    )
    return ShaderKey(resource_id, stage, entry, raw_hash)


def load_selected_shader(
    catalog_path: str | Path,
    shader_key: ShaderKey | Mapping[str, Any] | str,
) -> SelectedShader:
    """Validate the full catalog and return a physically verified shader blob."""

    path = Path(catalog_path).expanduser().resolve()
    try:
        catalog = read_json_object(path, MAX_CATALOG_BYTES, "capture catalog")
    except ValueError as exc:
        raise CatalogValidationError(str(exc)) from exc

    if catalog.get("schema") != CATALOG_SCHEMA:
        raise CatalogValidationError(
            "unsupported capture catalog schema: %s" % catalog.get("schema")
        )
    if catalog.get("schemaVersion") != CATALOG_VERSION:
        raise CatalogValidationError(
            "unsupported capture catalog schemaVersion: %s"
            % catalog.get("schemaVersion")
        )
    records = catalog.get("shaders")
    if not isinstance(records, list) or not records:
        raise CatalogValidationError(
            "capture catalog shaders must be a non-empty array"
        )

    keys: list[ShaderKey] = []
    keyed_records: list[tuple[ShaderKey, Mapping[str, Any]]] = []
    seen: set[ShaderKey] = set()
    seen_strings: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise CatalogValidationError("shaders[%d] must be an object" % index)
        key = _record_key(record, index)
        if key in seen:
            raise CatalogValidationError(
                "duplicate capture shader key: %s" % key.catalog_key
            )
        seen.add(key)
        keys.append(key)
        declared_key = record.get("shaderKey")
        if declared_key is not None:
            if not isinstance(declared_key, str) or declared_key != key.catalog_key:
                raise CatalogValidationError(
                    "shaders[%d].shaderKey does not match its fields" % index
                )
            if declared_key in seen_strings:
                raise CatalogValidationError("duplicate shaderKey: %s" % declared_key)
            seen_strings.add(declared_key)
        keyed_records.append((key, record))

    expected_capture_id = capture_identity(catalog, keys)
    declared_capture_id = _sha256(catalog.get("captureId"), "captureId")
    if declared_capture_id != expected_capture_id:
        raise CatalogValidationError(
            "captureId does not match the catalog's canonical capture identity"
        )

    if isinstance(shader_key, str):
        candidates = [
            (key, record)
            for key, record in keyed_records
            if record.get("shaderKey") == shader_key
        ]
        if not candidates:
            raise CatalogValidationError("shaderKey is not present in the catalog")
    else:
        requested = ShaderKey.from_value(shader_key)
        candidates = [item for item in keyed_records if item[0] == requested]
        if not candidates:
            raise CatalogValidationError("shader key is not present in the catalog")
    if len(candidates) != 1:
        raise CatalogValidationError("shader key does not select exactly one shader")

    key, record = candidates[0]
    if not key.raw_sha256:
        raise CatalogValidationError("selected shader has no exported raw bytes")
    raw_path_value = record.get("rawPath")
    if not isinstance(raw_path_value, str) or not raw_path_value.strip():
        raise CatalogValidationError("selected shader rawPath is required")
    raw_path = Path(raw_path_value)
    if raw_path.is_absolute():
        raw_path = raw_path.resolve()
    else:
        raw_path = (path.parent / raw_path).resolve()
    if not is_within(path.parent, raw_path):
        raise CatalogValidationError(
            "selected shader rawPath escapes the catalog directory"
        )
    if not raw_path.is_file():
        raise CatalogValidationError(
            "selected shader rawPath is not a file: %s" % raw_path
        )
    actual_size = raw_path.stat().st_size
    if actual_size <= 0 or actual_size > MAX_SHADER_BYTES:
        raise CatalogValidationError(
            "selected shader size must be between 1 and %d bytes" % MAX_SHADER_BYTES
        )
    try:
        declared_size = int(record.get("rawSize"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise CatalogValidationError("selected shader rawSize is required") from exc
    if declared_size != actual_size:
        raise CatalogValidationError(
            "selected shader rawSize mismatch: declared %d, actual %d"
            % (declared_size, actual_size)
        )
    blob = raw_path.read_bytes()
    actual_hash = hashlib.sha256(blob).hexdigest()
    if actual_hash != key.raw_sha256:
        raise CatalogValidationError("selected shader rawSha256 does not match rawPath")
    declared_format = _format_hint(record)
    if declared_format == "glsl":
        from .native_glsl import api as is_opengl
        if not is_opengl(catalog.get("api")):
            raise CatalogValidationError("captured GLSL source requires the OpenGL API")
        if key.entry_point != "main" or key.stage not in {
                "vertex", "hull", "domain", "geometry", "fragment", "compute"}:
            raise CatalogValidationError("captured GLSL source requires an OpenGL pipeline stage and main entry")
    container = fingerprint_shader_executable(
        blob, str(raw_path), format_hint=declared_format or None
    )
    if container is None:
        raise CatalogValidationError(
            "selected shader is not exactly one supported executable or explicitly captured OpenGL GLSL source"
        )
    detected_format = executable_format(container)
    semantic_format = api_semantic_format(
        str(catalog.get("api", "") or ""), detected_format
    )
    if semantic_format != detected_format:
        rebound = fingerprint_shader_executable(
            blob, str(raw_path), format_hint=semantic_format
        )
        if rebound is None:
            raise CatalogValidationError(
                "selected shader bytes contradict the capture API environment"
            )
        container = rebound
        detected_format = semantic_format

    abi_record = (
        _lower_unproven_dxil_cbuffer_layouts(record)
        if detected_format == "dxil"
        else record
    )
    abi = None
    abi_status = ABI_STATUS_UNAVAILABLE
    if abi_record.get("interfaceComplete") is True:
        try:
            abi = parse_shader_abi(
                abi_record,
                expected_stage=key.stage,
                expected_entry_point=key.entry_point,
            )
        except AbiValidationError as exc:
            raise CatalogValidationError(
                "selected shader declares an incomplete or invalid compiled interface: %s"
                % exc
            ) from exc
        abi_status = ABI_STATUS_CAPTURED_COMPLETE
        if detected_format == "dxil" and any(
            block.layout_completeness == "ambiguous"
            for block in abi.constant_blocks
        ):
            # Even an explicit producer completeness claim cannot turn an
            # ambiguous member layout into ordinary recursive ABI evidence.
            abi_status = ABI_STATUS_DXIL_ACCESS_REQUIRED
    elif detected_format == "dxil":
        abi = _dxil_ambiguity_only_abi(abi_record, key)
        if abi is not None:
            abi_status = ABI_STATUS_DXIL_ACCESS_REQUIRED

    return SelectedShader(
        catalog_path=path,
        capture_id=declared_capture_id,
        capture_path=str(catalog.get("capturePath", "") or ""),
        api=str(catalog.get("api", "") or ""),
        key=key,
        blob_path=raw_path,
        blob=blob,
        container=container,
        encoding=detected_format,
        abi=abi,
        abi_status=abi_status,
    )


def select_unique_shader_for_use(
    catalog_path: str | Path,
    event_id: int,
    stage: str,
) -> SelectedShader:
    """Select exactly one physically verified shader used by an event/stage.

    This is intentionally stricter than selecting the only shader record in a
    file: the bridge-produced ``uses`` row must bind the requested event and
    normalized stage to one internally consistent shader key. The selected
    record and bytes are then revalidated by :func:`load_selected_shader`.
    """

    if isinstance(event_id, bool):
        raise CatalogValidationError("event_id must be a positive integer")
    try:
        event_value = int(event_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CatalogValidationError("event_id must be a positive integer") from exc
    if event_value <= 0:
        raise CatalogValidationError("event_id must be a positive integer")
    requested_stage = normalize_stage(stage)
    if not requested_stage:
        raise CatalogValidationError("stage is required")

    path = Path(catalog_path).expanduser().resolve()
    try:
        catalog = read_json_object(path, MAX_CATALOG_BYTES, "capture catalog")
    except ValueError as exc:
        raise CatalogValidationError(str(exc)) from exc
    if (
        catalog.get("schema") != CATALOG_SCHEMA
        or catalog.get("schemaVersion") != CATALOG_VERSION
    ):
        raise CatalogValidationError("unsupported capture catalog schema/version")
    uses = catalog.get("uses")
    if not isinstance(uses, list):
        raise CatalogValidationError("capture catalog uses must be an array")

    matches: dict[str, ShaderKey] = {}
    for index, use in enumerate(uses):
        if not isinstance(use, Mapping):
            raise CatalogValidationError("uses[%d] must be an object" % index)
        raw_event = use.get("eventId")
        if isinstance(raw_event, bool):
            raise CatalogValidationError("uses[%d].eventId is invalid" % index)
        try:
            use_event = int(raw_event)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CatalogValidationError("uses[%d].eventId is invalid" % index) from exc
        if (
            use_event != event_value
            or normalize_stage(use.get("stage")) != requested_stage
        ):
            continue
        key = ShaderKey.from_value(use)
        declared_key = use.get("shaderKey")
        if not isinstance(declared_key, str) or declared_key != key.catalog_key:
            raise CatalogValidationError(
                "uses[%d].shaderKey does not match its fields" % index
            )
        matches[key.catalog_key] = key

    if len(matches) != 1:
        failure = CatalogUseNotCoveredError if not matches else CatalogValidationError
        raise failure(
            "event %d stage %s selected %d shaders; exactly one is required"
            % (event_value, requested_stage, len(matches))
        )
    selected_key = next(iter(matches))
    return load_selected_shader(path, selected_key)


def list_exact_shader_family_uses(
    catalog_path: str | Path,
    shader_key: ShaderKey | Mapping[str, Any] | str,
    *,
    require_complete_abi: bool = True,
) -> list[int]:
    """Return every catalog event using one exact captured Shader identity.

    Family identity deliberately includes the capture-local ResourceId. Equal
    bytecode in a distinct ResourceId is not merged. The full shader catalog,
    selected blob are validated before any use rows are accepted; duplicate
    rows collapse to one EventId without truncation. Executable callers also
    require a compiled ABI by default. Identity-only navigation and display
    naming may omit that gate and validate their own live reflected fields.
    """
    selected = load_selected_shader(catalog_path, shader_key)
    if require_complete_abi and selected.abi is None:
        raise CatalogValidationError(
            "selected Shader family has no complete compiled ABI"
        )
    path = Path(catalog_path).expanduser().resolve()
    try:
        catalog = read_json_object(path, MAX_CATALOG_BYTES, "capture catalog")
    except ValueError as exc:
        raise CatalogValidationError(str(exc)) from exc
    if (
        catalog.get("schema") != CATALOG_SCHEMA
        or catalog.get("schemaVersion") != CATALOG_VERSION
    ):
        raise CatalogValidationError("unsupported capture catalog schema/version")
    uses = catalog.get("uses")
    if not isinstance(uses, list):
        raise CatalogValidationError("capture catalog uses must be an array")

    event_ids: set[int] = set()
    keys_by_event_stage: dict[tuple[int, str], dict[str, ShaderKey]] = {}
    for index, use in enumerate(uses):
        if not isinstance(use, Mapping):
            raise CatalogValidationError("uses[%d] must be an object" % index)
        key = ShaderKey.from_value(use)
        declared_key = use.get("shaderKey")
        if not isinstance(declared_key, str) or declared_key != key.catalog_key:
            raise CatalogValidationError(
                "uses[%d].shaderKey does not match its fields" % index
            )
        raw_event = use.get("eventId")
        if isinstance(raw_event, bool):
            raise CatalogValidationError("uses[%d].eventId is invalid" % index)
        try:
            event_id = int(raw_event)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CatalogValidationError("uses[%d].eventId is invalid" % index) from exc
        if event_id <= 0:
            raise CatalogValidationError("uses[%d].eventId is invalid" % index)
        by_key = keys_by_event_stage.setdefault((event_id, key.stage), {})
        by_key[key.catalog_key] = key
        if key == selected.key:
            event_ids.add(event_id)

    # Validate unique event/stage selection from the one bounded catalog read.
    # This is equivalent to repeatedly calling select_unique_shader_for_use,
    # without re-reading and re-hashing a popular Shader thousands of times.
    result = []
    for event_id in sorted(event_ids):
        matches = keys_by_event_stage.get(
            (event_id, selected.key.stage), {})
        if len(matches) != 1 or selected.key.catalog_key not in matches:
            raise CatalogValidationError(
                "event %d does not preserve the selected Shader family" % event_id
            )
        result.append(event_id)
    return result
